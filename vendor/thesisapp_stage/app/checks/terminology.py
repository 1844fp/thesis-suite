"""术语一致性检查器。

术语检查是"看起来简单、做起来最容易翻车"的一类。翻车方式很典型：把
「深度学习」和「深度神经网络」判成异体写法，然后自动统一——这两个词在有的
论文里确实混用，在另一些论文里是刻意区分的两个概念，自动改就把学术含义改掉了。

所以这里有两条刻意设计的约束：

1. **异体表是数据驱动的，而且只收真正同义的写法**。判断标准是：把 A 换成 B
   之后，任何一句话的真值都不会改变。做不到这一条的词一律不收。
2. **keep_terms 白名单优先**。``ctx.board.project.meta["keep_terms"]`` 里列出的
   术语及其所有变体，本模块一律不碰——组内约定只有作者知道。

还有一个容易忽略的坑：异体之间**互为子串**。「网络→网路」和
「卷积神经网络→卷积网路」同时存在时，逐条独立替换会级联出错。所以本模块
先枚举所有变体的出现区间，再丢弃被更长匹配包住的短匹配——这是"改坏文本"
与"漏报一处"之间，唯一可接受的取舍方向。

不做的事（有意留有缺口）：

* 不做"中英术语混写"的通用检测（无法判断 ``LSTM`` 与「长短时记忆网络」是否
  指同一个东西，猜错就是造错），只处理内置表里明确同义的那几组。
* 不做词频统计式的"术语漂移"检测（需要全文语义聚类，规则做不到）。
"""

from __future__ import annotations

import re
from typing import Any

from ..framework.artifact import Issue, Severity
from ..framework.registry import verifier
from ._util import anchor, keep_terms, make_issue, mask_nonbody

#: 内置异体表：``规范写法 -> 异体写法列表``。
#:
#: 收录标准（三条都要满足）：
#: * 两词指同一实体，替换后句子含义不变；
#: * 差异来自译者习惯、简繁体或中英简称，而不是概念层次（如"模型/算法"）；
#: * 在学位论文里出现混写会被评审指出。
#:
#: 特意**不收**的例子，写在这里是为了防止后来者"顺手加进去"：
#:   深度学习 / 深度神经网络   —— 前者是领域名，后者是模型类，不是同义词
#:   神经网络 / 人工神经网络   —— 层级不同（人工神经网络含浅层模型）
#:   准确率 / 精度             —— 有的章节刻意区分 accuracy 与 precision
#:   网络 / 网路（单独一条）   —— 会与"卷积神经网络"互为子串，且"网络"太泛
BUILTIN_VARIANTS: dict[str, tuple[str, ...]] = {
    "卷积神经网络": ("卷积网路", "卷积神经网路"),
    "循环神经网络": ("递归神经网络", "循环网路"),
    "数据库": ("资料库",),
    "视频": ("视讯",),
    "算法": ("演算法",),
    "软件": ("软体",),
    "硬件": ("硬体",),
    "信息": ("资讯",),
    "优化": ("最佳化",),
    "用户": ("使用者",),
}

#: 中英混写对：``中文全称 -> 缩写``。
#: 与上一张表分开的理由：缩写要不要统一成中文，取决于作者习惯（有的论文通篇
#: 用缩写），而简繁/译名混写基本没有正当理由。分开后可以只对缩写这一组放宽条件。
ABBREV_VARIANTS: dict[str, tuple[str, ...]] = {
    "卷积神经网络": ("CNN",),
    "循环神经网络": ("RNN",),
    "长短期记忆网络": ("LSTM",),
    "支持向量机": ("SVM",),
    "生成式对抗网络": ("GAN",),
}


def _groups(extra_keep: set[str]) -> list[tuple[str, str, list[str]]]:
    """合并两张表，返回 ``[(规范写法, 来源, 异体列表)]``，剔除被 keep_terms 保护的分组。

    只要规范写法或任一异体出现在白名单里，整组跳过：白名单的粒度是"概念"而不是
    "写法"——作者说"我们组统一写 CNN"，这一组就不该再被报出来。
    """
    out: list[tuple[str, str, list[str]]] = []
    for src, table in (("native", BUILTIN_VARIANTS), ("abbrev", ABBREV_VARIANTS)):
        for canonical, variants in table.items():
            if canonical in extra_keep or any(v in extra_keep for v in variants):
                continue
            out.append((canonical, src, [v for v in variants if v != canonical]))
    return out


@verifier(
    "check_term_variants",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="同一概念的异体写法（数据驱动 + keep_terms 白名单），可用 unify_term 修复",
)
def check_term_variants(text: str, ctx: Any) -> list[Issue]:
    """异体写法检测与统一。

    每条异体写法单独报一条（而不是"该组术语不统一"一条），理由是修复算子
    ``unify_term`` 接受 ``variants + canonical``，逐条报可以让修复器精确地
    只动一种写法，改完能逐条复验；合并成一条会让"改了三个还剩一个"看不出来。

    只在**正文**里查（用 ``mask_nonbody`` 屏蔽参考文献区）：参考文献保留原文
    译名是学术规范的一部分，替换它就是篡改引文。

    中英混写（``native_abbr``）额外要求：该缩写至少出现 2 次。只出现一次的
    ``CNN`` 很可能本来就是"全称（CNN）"的定义性用法，报出来是噪声。
    """
    body = mask_nonbody(text or "")
    protected = keep_terms(ctx)
    issues: list[Issue] = []

    for canonical, src, variants in _groups(protected):
        # 先把该组所有变体的出现区间收齐，再统一判重叠——
        # 逐条独立处理会让"卷积神经网络"里的"网络"类匹配重复计入。
        spans: list[tuple[int, int, str]] = []
        for v in variants:
            spans.extend((m.start(), m.end(), v) for m in re.finditer(re.escape(v), body))
        spans.extend((m.start(), m.end(), canonical) for m in re.finditer(re.escape(canonical), body))

        for start, end, v in sorted(spans):
            if v == canonical:
                continue
            # 被同组更长的匹配完全包住 → 它是那个更长术语的一部分，不是异体
            if any(o_s <= start and end <= o_e and (o_s, o_e) != (start, end) for o_s, o_e, _ in spans):
                continue
            n = sum(1 for s, e, vv in spans if vv == v)
            if src == "abbrev" and n < 2:
                continue
            f = anchor(text, v, start)
            issues.append(
                make_issue(
                    kind="term.variant_usage",
                    severity=Severity.MINOR,
                    message=f"术语「{v}」与规范写法「{canonical}」混用（「{v}」出现 {n} 次）",
                    text=text,
                    char=start,
                    find=f,
                    evidence=v,
                    # 只在定位唯一时才允许机械修复：术语替换涉及全文一致性，
                    # 定位不唯一时 unify_term 会误伤参考文献或他人引文。
                    fixable=bool(f),
                    fix_op="unify_term",
                    fix_hint=(
                        f"用 unify_term 把「{v}」统一为「{canonical}」；"
                        f"若课题组的约定恰好相反，请在项目 meta.keep_terms 中声明以豁免。"
                    ),
                )
            )
    return issues


#: 候选缩写：2~6 个连续大写字母/数字（CNN、LSTM、BERT4 均可）。
ABBR_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,5})(?![A-Za-z0-9])")

#: 不可能是"自造缩写"的高频通用符号，必须排除，否则会刷一屏假阳性。
ABBR_STOPWORDS = {
    "DOI", "ISBN", "ISSN", "GB", "ISO", "IEEE", "ACM", "AI", "IT", "PDF",
    "URL", "HTTP", "HTML", "XML", "JSON", "CSV", "API", "GPU", "CPU", "RAM",
    "USB", "SQL", "OK", "NO", "AND", "OR", "THE", "OF", "IN", "ON", "FOR",
    "WITH", "FIG", "TAB", "EQ", "ET", "AL",
}


def _has_full_form(text: str, abbr: str) -> bool:
    """全文里是否给过该缩写的全称。

    认定两种标准写法：

    * ``中文全称（ABBR）`` —— 中文论文最常见的定义写法；
    * ``ABBR（中文全称）`` —— 英文摘要里常见。

    中文全称要求 2 个以上汉字且**紧邻括号**。放宽到"同一句里出现就算"会误判：
    同一句里完全可能出现两个互不相干的词。
    """
    if re.search(rf"[\u4e00-\u9fff]{{2,20}}\s*[（(]\s*{re.escape(abbr)}\s*[）)]", text):
        return True
    if re.search(rf"(?<![A-Za-z0-9]){re.escape(abbr)}\s*[（(]\s*[\u4e00-\u9fff]{{2,20}}\s*[）)]", text):
        return True
    return False


@verifier(
    "check_abbreviation_first_use",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="缩写未给全称（同一缩写出现 ≥2 次且全文从未定义）",
)
def check_abbreviation_first_use(text: str, ctx: Any) -> list[Issue]:
    """缩写首次出现是否给了全称。

    刻意用很严的触发条件（出现 ≥2 次 **且** 全文从未定义），因为缩写检查的
    假阳性代价特别高：``CNN`` 只在标题里出现一次、``AI`` 只在关键词里出现一次，
    都不需要全称。只有"反复用却从不解释"才是真问题。

    另一个陷阱：英文摘要与中文摘要各定义一次、正文再出现一次，这种情况**不应报**。
    所以判据是"全文从未定义"，而不是"首次出现附近没有定义"。
    """
    body = mask_nonbody(text or "")
    issues: list[Issue] = []
    seen: set[str] = set()

    for m in ABBR_RE.finditer(body):
        abbr = m.group(1)
        if abbr in seen or abbr in ABBR_STOPWORDS:
            continue
        seen.add(abbr)
        if body.count(abbr) < 2:
            continue
        if _has_full_form(body, abbr):
            continue
        pos = m.start()
        issues.append(
            make_issue(
                kind="term.abbreviation_undefined",
                severity=Severity.MINOR,
                message=f"缩写「{abbr}」出现 {body.count(abbr)} 次，但全文未给出中文全称",
                text=text,
                char=pos,
                find=anchor(text, abbr, pos),
                evidence=abbr,
                fixable=False,  # 全称是什么只有作者知道，编不出来
                fix_hint=f"在「{abbr}」首次出现处补上全称，写成「中文全称（{abbr}）」的形式。",
            )
        )
    return issues


__all__ = [
    "check_term_variants",
    "check_abbreviation_first_use",
    "BUILTIN_VARIANTS",
    "ABBREV_VARIANTS",
]
