"""文本卫生检查器：标点、空格、空段、重复段、套话、超长句、配对符号。

这一层的共同点是**后果轻微但数量巨大**：一条标点混用不值一提，三百条就说明
作者从没通读过自己的稿子。所以这里有两条纪律：

1. **严重度必须压低**（MINOR/INFO）。如果把标点问题报成 MAJOR，自我更正循环
   的 ``converged`` 判定会永远为 False，真正的内容问题被淹没在噪声里。
2. **中英文边界宁可不报**。``A,B``、``(GPU)``、``et al.`` 这些是英文写法，
   中文论文里出现完全正常。误报一次，作者就不再相信整个工具。

可机械修复的部分（标点规范化、删套话、去多余空格/空段）给出 ``fix_op``；
需要人看一眼的（重复段落、超长句、引号不配对）一律 ``fixable=False``。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from ..framework.artifact import Issue, Severity
from ..framework.registry import verifier
from ._util import (
    CJK,
    anchor,
    heading_kind,
    is_table_row,
    make_issue,
    nonempty_index,
    split_paragraphs,
    split_sentences,
)

# --------------------------------------------------------------------------
# 标点
# --------------------------------------------------------------------------

#: 与 ``ops.normalize_punctuation`` **完全一致**的判定：只有逗号两侧都是汉字
#: 才认定混用。复用同一对前后视断言不是巧合——检查器报出来的问题必须能被
#: 修复算子改掉，否则就会出现"报了却修不动"的死循环。
CJK_COMMA_RE = re.compile(rf"(?<=[{CJK}]),(?=[{CJK}])")
CJK_SEMI_RE = re.compile(rf"(?<=[{CJK}]);(?=[{CJK}])")
CJK_COLON_RE = re.compile(rf"(?<=[{CJK}]):(?=[{CJK}])")
CJK_EXCL_RE = re.compile(rf"(?<=[{CJK}])!(?=[{CJK}])")
CJK_QUES_RE = re.compile(rf"(?<=[{CJK}])\?(?=[{CJK}])")

#: 中文字后紧跟半角左括号，或半角右括号紧跟中文字。这一组**算子不修**，
#: 所以只提示、不标 fixable。
CJK_PAREN_RE = re.compile(rf"(?<=[{CJK}])\(|\)(?=[{CJK}])")

_PUNCT_GROUPS: tuple[tuple[str, re.Pattern[str], str, Severity, str], ...] = (
    ("comma", CJK_COMMA_RE, "，", Severity.MINOR, "normalize_punctuation"),
    ("semicolon", CJK_SEMI_RE, "；", Severity.MINOR, "normalize_punctuation"),
    ("colon", CJK_COLON_RE, "：", Severity.MINOR, "normalize_punctuation"),
    ("exclam", CJK_EXCL_RE, "！", Severity.INFO, "normalize_punctuation"),
    ("quest", CJK_QUES_RE, "？", Severity.INFO, "normalize_punctuation"),
)


@verifier(
    "check_punctuation_mix",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="中文语境下的半角标点（, ; : ! ?），可机械规范化",
)
def check_punctuation_mix(text: str, ctx: Any) -> list[Issue]:
    """中文语境里的半角 ``, ; : ! ?``。

    为什么只报这一组：它们有**确定无歧义**的中文对应物，而且
    ``normalize_punctuation`` 用的就是同一对断言，报出来必定能修掉。
    冒号比逗号更值得报一级：中文里 ``结论:`` 是明显的排版残留，而 ``A,B``
    在英文术语里很正常——判定虽同，语境权重不同，故冒号单独成组。

    半角括号只做 INFO 提示、不标 fixable：``图1(a)`` 这类写法有歧义，
    算子也不敢动它，报了却修不动只会制造"永远修不完"的问题。
    """
    issues: list[Issue] = []
    for name, rx, full, sev, op in _PUNCT_GROUPS:
        for m in rx.finditer(text or ""):
            ch = m.group(0)
            # 证据取"汉字+半角标点+汉字"，让作者一眼看到上下文
            snippet = text[max(0, m.start() - 1): m.end() + 1]
            issues.append(
                make_issue(
                    kind=f"punct.mixed_{name}",
                    severity=sev,
                    message=f"中文语境使用了半角标点 {ch!r}（建议改为全角 {full}）",
                    text=text,
                    char=m.start(),
                    find=anchor(text, ch, m.start()),
                    evidence=snippet,
                    fixable=True,
                    fix_op=op,
                    fix_hint=f"调用 {op} 统一中文标点（算子只处理两侧均为汉字的半角标点，英文术语不受影响）。",
                )
            )

    for m in CJK_PAREN_RE.finditer(text or ""):
        snippet = text[max(0, m.start() - 2): m.end() + 2]
        issues.append(
            make_issue(
                kind="punct.halfwidth_paren",
                severity=Severity.INFO,
                message="中文文本中出现半角括号，请人工确认是否为排版残留",
                text=text,
                char=m.start(),
                find=anchor(text, m.group(0), m.start()),
                evidence=snippet,
                fixable=False,  # 算子不处理括号，报了也不能自动改
                fix_hint="若括号内容为中文，改为全角（）；若为英文缩写或公式，保持半角。",
            )
        )
    return issues


# --------------------------------------------------------------------------
# 空白
# --------------------------------------------------------------------------

_CJK_GAP_RE = re.compile(rf"(?<=[{CJK}])[ \t]+(?=[{CJK}])")
_TRAIL_WS_RE = re.compile(r"[ \t]+(?=\n)")
_MULTI_SPACE_RE = re.compile(r"(?<=[A-Za-z0-9,.;:])[ \t]{2,}(?=[A-Za-z0-9])")


def _line_is_protected(text: str, char_index: int) -> bool:
    """该字符所在的这一行是否属于"不能动空格"的行：标题行或表格行。

    为什么单独抽一个函数：空白类规则都是"删掉多余空格"，而标题里的
    「第一章 绪论」和表格里的列分隔空格都是**有意义的**空格，删了就是改坏稿子。
    两类判断放在一处，新增空白规则时不容易漏掉保护。

    为什么标题判据用 ``heading_kind`` 而不是自己写正则：「第一章 绪论」必须
    整行匹配才算标题，而正文里的"见 1.2 节"不是——这套判据已经由 ``_util``
    统一维护，各检查器各写一份必然漂移。

    **表格判据需要相邻行的佐证。** 单看一行，"An  example  here." 与
    "变量  均值  标准差"在结构上完全同形（三短字段、两处双空格），
    ``is_table_row`` 对两者都返回 True。被误判的代价是**真实排版污染不再被报**——
    实测中这就让"拉丁文本连续空格"整条规则失效。
    真正的表格有**连续多行列对齐**这个额外特征，所以这里要求：要么本行是表格行，
    且相邻行也是表格行（真表格），要么就按普通文本处理（该报就报）。
    单行表格会因此被漏保护，但那比"整条规则失效"代价小得多。
    """
    line = _line_at(text, char_index)
    if not line.strip():
        return False
    if heading_kind(line):
        return True
    if not is_table_row(line):
        return False
    # 相邻行也是表格行 → 这是真表格的列对齐；孤立的"像表格的一行"更可能是
    # 一处排版污染（多处双空格的句子），应当交给规则报出来。
    return _neighbour_is_table_row(text, char_index)


def _neighbour_is_table_row(text: str, char_index: int) -> bool:
    """上一行或下一行是否也是表格行。用于把"真表格"与"恰好含双空格的句子"分开。"""
    start = text.rfind("\n", 0, char_index) + 1
    lines = text.split("\n")
    idx = text.count("\n", 0, start)
    for j in (idx - 1, idx + 1):
        if 0 <= j < len(lines):
            other = lines[j]
            if other.strip() and is_table_row(other):
                return True
    return False


def _line_at(text: str, char_index: int) -> str:
    """取字符偏移所在的整行。越界返回空串（不报，也不崩）。"""
    if char_index < 0 or char_index > len(text or ""):
        return ""
    start = text.rfind("\n", 0, char_index) + 1
    end = text.find("\n", char_index)
    return text[start:] if end < 0 else text[start:end]


@verifier(
    "check_whitespace",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="行尾空格、中文之间的多余空格、拉丁文本中的连续空格",
)
def check_whitespace(text: str, ctx: Any) -> list[Issue]:
    """三类空白问题，全都可机械修复，所以都用 ``replace_span``。

    三类分开报而不是合并成一条：它们对作者的动作不同（删行尾空格是排版清理，
    中文中间的空格往往是复制 PDF 带进来的，需要看上下文确认没删掉英文单词）。
    合并成一条"全文空白问题"会让修复拿到一个巨大的 span，改错范围无法审计。
    """
    text = text or ""
    issues: list[Issue] = []

    for m in _TRAIL_WS_RE.finditer(text):
        snip = m.group(0) + "\n"
        issues.append(
            make_issue(
                kind="whitespace.trailing",
                severity=Severity.INFO,
                message="行尾存在多余空格",
                text=text,
                char=m.start(),
                find=anchor(text, snip, m.start()),
                evidence=snip,
                fixable=True,
                fix_op="replace_span",
                fix_hint="删除行尾空格（保留换行）。",
            )
        )

    for m in _CJK_GAP_RE.finditer(text):
        # 排除真正的空位（如"图 3"这种标签与编号之间的空格由图表检查器负责，
        # 这里只看两个汉字之间的空格——那一定是排版污染）。
        #
        # 两类行必须整体跳过，否则会改坏稿子（实测）：
        # * 标题行「第一章 绪论」：编号与标题文字之间用空格分隔是规范写法，
        #   删成「第一章绪论」反而不规范。
        # * 表格行「变量  均值  标准差」：连续空格就是列分隔符，删掉表格塌成一串字。
        if _line_is_protected(text, m.start()):
            continue
        snip = m.group(0)
        issues.append(
            make_issue(
                kind="whitespace.cjk_gap",
                severity=Severity.MINOR,
                message="两个汉字之间存在多余空格",
                text=text,
                char=m.start(),
                find=anchor(text, text[max(0, m.start() - 1): m.end() + 1], m.start()),
                evidence=text[max(0, m.start() - 1): m.end() + 1],
                fixable=True,
                fix_op="replace_span",
                fix_hint="删除汉字之间的空格（常见于从 PDF/网页复制的文本）。",
            )
        )

    for m in _MULTI_SPACE_RE.finditer(text):
        # 表格行同理：`3.42  0.87` 之间的两个空格是列分隔符，
        # 压缩成一个空格会让这一列对不齐（实测会真的改动表格）。
        if _line_is_protected(text, m.start()):
            continue
        snip = m.group(0)
        issues.append(
            make_issue(
                kind="whitespace.multi_space",
                severity=Severity.INFO,
                message=f"拉丁文本中出现连续 {len(snip)} 个空格",
                text=text,
                char=m.start(),
                find=anchor(text, text[max(0, m.start() - 2): m.end() + 2], m.start()),
                evidence=text[max(0, m.start() - 2): m.end() + 2],
                fixable=True,
                fix_op="replace_span",
                fix_hint="压缩为一个空格。",
            )
        )
    return issues


# --------------------------------------------------------------------------
# 空段落与重复段落
# --------------------------------------------------------------------------


@verifier(
    "check_empty_paragraphs",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="连续空段落（≥3 个空行）",
)
def check_empty_paragraphs(text: str, ctx: Any) -> list[Issue]:
    """连续 3 个以上空行。

    阈值取 3 而不是 2：段落之间空一行是标准写法，空两行虽然不美观但很常见，
    从第 3 个空行起才基本可以确定是"删了内容忘记收空行"或"多次复制粘贴"。
    """
    text = text or ""
    issues: list[Issue] = []
    for m in re.finditer(r"\n{3,}", text):
        n = m.end() - m.start()
        sev = Severity.MINOR if n < 6 else Severity.MAJOR
        issues.append(
            make_issue(
                kind="paragraph.consecutive_blank",
                severity=sev,
                message=f"连续 {n} 个空行（超过 2 个即为排版残留）",
                text=text,
                char=m.start(),
                find=anchor(text, m.group(0), m.start()),
                evidence=m.group(0),
                fixable=True,
                fix_op="replace_span",
                fix_hint="压缩为一个空行（段落之间保留一个空行即可）。",
            )
        )
    return issues


@verifier(
    "check_duplicate_paragraphs",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="复制粘贴导致的整段重复（含非相邻的重复）",
)
def check_duplicate_paragraphs(text: str, ctx: Any) -> list[Issue]:
    """整段重复。

    为什么同时报"相邻重复"和"远距离重复"：
    相邻重复（``merge_duplicate_paragraphs`` 能修）是最典型的复制粘贴；
    远距离重复更常见也更危险——同一段论证在第三章和第五章各出现一次，
    评审会认为作者在灌水。远距离重复无法机械修复（删哪一处要看上下文），
    所以只报不改。

    相似度阈值 0.9 而不是 0.8：学位论文里"综上所述，本文……"这类段落骨架
    本来就相似，阈值低了全是假阳性。
    """
    paras = [line.strip() for _, line, _ in nonempty_index(text)]
    offs = [off for _, _, off in nonempty_index(text)]
    idxs = [i for i, _, _ in nonempty_index(text)]
    issues: list[Issue] = []

    reported: set[tuple[int, int]] = set()
    for a in range(len(paras)):
        for b in range(a + 1, len(paras)):
            pa, pb = paras[a], paras[b]
            if len(pa) < 25 or len(pb) < 25:
                continue
            if pa == pb:
                same, ratio = True, 1.0
            else:
                ratio = SequenceMatcher(None, pa, pb).ratio()
                same = ratio >= 0.9
            if not same:
                continue
            if (a, b) in reported:
                continue
            reported.add((a, b))
            adjacent = b == a + 1
            # 证据与锚点都必须是**唯一**的原文片段：同一段文字出现两次时，
            # 直接拿段落当 evidence 会让复验和 apply_edits 都分不清是哪一处。
            # anchor 会在唯一性做不到时返回空串，那就退化成"报了但修不了"。
            f = anchor(text, pb, offs[b])
            evidence = f or pb[:120]
            issues.append(
                make_issue(
                    kind="paragraph.duplicate",
                    severity=Severity.MAJOR,
                    message=(
                        f"第 {idxs[a] + 1} 段与第 {idxs[b] + 1} 段内容"
                        f"{'完全相同' if pa == pb else f'高度相似（{ratio:.2f}）'}"
                        f"{'（相邻重复）' if adjacent else '（远距离重复，疑似灌水）'}"
                    ),
                    text=text,
                    paragraph=idxs[b],
                    char=offs[b],
                    find=f,
                    evidence=evidence,
                    fixable=adjacent,  # 仅相邻重复有确定的机械修法
                    fix_op="merge_duplicate_paragraphs" if adjacent else "",
                    fix_hint=(
                        "删除重复的那一段（保留首次出现的位置）。"
                        if adjacent
                        else "确认两处是否都在论述同一件事：是则合并保留一处，否则补充各自的差异。"
                    ),
                )
            )
    return issues


# --------------------------------------------------------------------------
# 套话与超长句
# --------------------------------------------------------------------------

#: 与 ``ops.strip_filler`` 内置清单保持一致（且必须带尾随逗号，
#: 因为算子删的就是"套话+逗号"这个整体；只删词会把句子改成病句）。
FILLER_PHRASES: tuple[str, ...] = (
    "综上所述，",
    "值得注意的是，",
    "众所周知，",
    "不言而喻，",
    "显而易见，",
    "总而言之，",
    "换言之，",
)


@verifier(
    "check_ai_filler",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="模板腔套话（可机械删除，属 MINOR）",
)
def check_ai_filler(text: str, ctx: Any) -> list[Issue]:
    """模板腔套话。

    **这不是降 AIGC 检查器的替身，只是排版级提示。** 套话本身不违规，
    学位论文里"综上所述"用一次完全正常；只有一句话里堆了三四个套话，
    或者通篇每隔两段就出现一次，才说明文字是凑出来的。

    所以严重度封顶 MINOR，并且 ``fixable=True``（``strip_filler`` 是纯删除
    短语，不会改写句子，风险可控）——但作者仍应复核删完后句子是否通顺。
    """
    text = text or ""
    issues: list[Issue] = []
    counts: dict[str, int] = {}
    for phrase in FILLER_PHRASES:
        n = text.count(phrase)
        if not n:
            continue
        for k in range(n):
            start = _nth_index(text, phrase, k + 1)
            counts[phrase] = counts.get(phrase, 0) + 1
            issues.append(
                make_issue(
                    kind="style.filler_phrase",
                    severity=Severity.MINOR,
                    message=f"出现模板腔套话「{phrase}」（全文第 {counts[phrase]} 次）",
                    text=text,
                    char=start,
                    # 必须按"第几次出现"取锚点：同一句套话在文中出现两次时，
                    # 默认取最近匹配会让两条问题落到同一个位置，其中一条
                    # 在修复时会因为"定位不唯一"被算子拒绝。
                    find=anchor(text, phrase, start),
                    evidence=phrase,
                    fixable=True,
                    fix_op="strip_filler",
                    fix_hint=f"删除「{phrase}」并检查删除后句子是否仍然通顺。",
                )
            )
    return issues


def _nth_index(text: str, sub: str, n: int) -> int:
    """``sub`` 在 ``text`` 中第 ``n`` 次出现的下标（1 起）；找不到返回 0。"""
    idx = -1
    for _ in range(n):
        idx = text.find(sub, idx + 1)
        if idx < 0:
            return 0
    return idx


#: 超长句阈值：中文论文一句超过 120 字基本读不断。
LONG_SENTENCE_CHARS = 120


@verifier(
    "check_sentence_length",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="超长句（中文超过 120 字且无标点断开）",
)
def check_sentence_length(text: str, ctx: Any) -> list[Issue]:
    """超长句。

    **刻意不自动修**：断句需要理解句义，任何规则式的"在逗号处切开"都可能把
    一个完整的论证关系切断。这类问题只能提示作者。

    阈值 120 字：比常见"长句"标准（60~80 字）宽得多，是有意为之——学位论文里
    方法一节的句子天然长，卡太严会刷屏。宁可漏掉"偏长"的句子，只抓"离谱"的。
    """
    text = text or ""
    issues: list[Issue] = []
    for start, _end, sent in split_sentences(text):
        if len(sent) <= LONG_SENTENCE_CHARS:
            continue
        lead = sent[:40]
        issues.append(
            make_issue(
                kind="sentence.too_long",
                severity=Severity.MINOR,
                message=f"存在超长句（{len(sent)} 字，超过 {LONG_SENTENCE_CHARS} 字），建议拆分",
                text=text,
                char=start,
                # 单句通常唯一；万一正文里出现两次，anchor 会自动补上下文，
                # 补不出唯一串时返回空串（该条问题仍会被报出，只是不能机械修）。
                find=anchor(text, lead, start),
                evidence=lead,
                fixable=False,
                fix_hint="在语义完整的逗号或分号处拆成两句；拆句涉及论证结构，需作者判断。",
            )
        )
    return issues


# --------------------------------------------------------------------------
# 配对符号
# --------------------------------------------------------------------------

_PAIRS = (("“", "”"), ("‘", "’"), ("（", "）"), ("《", "》"), ("【", "】"))


@verifier(
    "check_quote_pairs",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="中文引号/括号不配对",
)
def check_quote_pairs(text: str, ctx: Any) -> list[Issue]:
    """引号与括号配对。

    只数**全角中文配对符号**：ASCII 的 ``"`` / ``(`` 在公式、代码、英文引文里
    成对出现的方式和中文完全不同，混进来数必然误报。

    不自动修：无法确定该补一个还是该删一个（"缺右引号"和"多左引号"在文本上
    长得一模一样），替作者猜一边就是制造新错误。
    """
    text = text or ""
    issues: list[Issue] = []
    for left, right in _PAIRS:
        nl, nr = text.count(left), text.count(right)
        if nl == nr:
            continue
        # 找出第一个落单的符号，作为定位与证据
        if nl > nr:
            pos = _first_unbalanced(text, left, right)
            ch, missing, extra = left, right, "左"
        else:
            pos = _first_unbalanced(text, right, left)
            ch, missing, extra = right, left, "右"
        snip = _snippet(text, pos)
        issues.append(
            make_issue(
                kind="punct.unbalanced_pair",
                severity=Severity.MINOR,
                message=f"{extra}符号「{ch}」不配对（{left}×{nl}，{right}×{nr}）",
                text=text,
                paragraph=-1,
                char=pos,
                find=anchor(text, snip, pos),
                evidence=snip,
                fixable=False,
                fix_hint=f"检查该处，补上或删除对应的「{missing}」，使引号成对。",
            )
        )
    return issues


def _first_unbalanced(text: str, more: str, fewer: str) -> int:
    """找出哪一处 ``more`` 是多余的：一旦右符号先出现，说明左符号多了。

    简化处理：按顺序扫 ``more``，遇到任何 ``fewer`` 就认为这一对闭合了；
    扫完仍没闭合的第一个 ``more`` 就是多余的。位置不准也无妨——它只用于显示。
    """
    pending = -1
    for i, ch in enumerate(text):
        if ch == more:
            if pending < 0:
                pending = i
        elif ch == fewer and pending >= 0:
            pending = -1
    return pending if pending >= 0 else 0


def _snippet(text: str, pos: int, width: int = 12) -> str:
    a, b = max(0, pos - width), min(len(text), pos + width + 1)
    return text[a:b]


__all__ = [
    "check_punctuation_mix",
    "check_whitespace",
    "check_empty_paragraphs",
    "check_duplicate_paragraphs",
    "check_ai_filler",
    "check_sentence_length",
    "check_quote_pairs",
]
