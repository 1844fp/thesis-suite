"""结构完整性检查器：必需章节、标题层级、摘要长度、关键词。

结构类检查与"文本卫生"刚好相反：单条问题后果很重（缺摘要 = 直接不合格），
但数量少。所以这里严重度整体偏高，而 ``check_abstract_length`` /
``check_keywords`` 这类"规范区间"问题给 INFO/MINOR——区间阈值各校不同，
把它报成 MAJOR 会让一篇合规论文永远收敛不了。

一条贯穿本模块的设计：**只认"整行是标题"**。正文里"本文摘要如下"这种句子
不能触发章节识别，否则摘要长度会把整篇正文算进去。
"""

from __future__ import annotations

import re
from typing import Any

from ..framework.artifact import Issue, Severity
from ..framework.registry import verifier
from ._util import (
    anchor,
    count_cjk,
    heading_kind,
    headings,
    make_issue,
    nonempty_index,
    split_paragraphs,
)

#: 必需章节：``(键, 别名列表, 缺失时的严重度, 说明)``。
#: 别名用"整行标题"匹配（见 _find_section），所以不必担心正文里的同名词。
REQUIRED_SECTIONS: tuple[tuple[str, tuple[str, ...], Severity, str], ...] = (
    ("abstract_cn", ("摘要", "中文摘要", "摘  要"), Severity.MAJOR, "中文摘要"),
    ("keywords_cn", ("关键词", "关键字", "关键词："), Severity.MAJOR, "中文关键词"),
    ("abstract_en", ("Abstract", "ABSTRACT", "英文摘要"), Severity.MAJOR, "英文摘要"),
    ("toc", ("目录", "目  录", "Contents"), Severity.MINOR, "目录"),
    ("conclusion", ("结论", "结论与展望", "总结与展望", "结  论"), Severity.MAJOR, "结论"),
    ("references", ("参考文献", "References"), Severity.BLOCKER, "参考文献"),
)

#: 关键词取值：标题后跟 "：" 或"："，或直接换行。
_KW_HEAD_RE = re.compile(r"^(关键词|关键字|Keywords?)\s*[:：]?\s*(.*)$", re.IGNORECASE)


def _section_bounds(text: str, aliases: tuple[str, ...]) -> tuple[int, int]:
    """找到某章节的正文区间 ``(起始字符偏移, 结束字符偏移)``；找不到返回 ``(-1, -1)``。

    结束位置取"下一个标题"或"下一个空行分段后的标题"，而不是固定行数：
    摘要长度必须量到真正的边界，量错了这条检查就没有任何意义。
    """
    lines = split_paragraphs(text)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    start_idx = -1
    for i, line in enumerate(lines):
        s = line.strip()
        for alias in aliases:
            # 允许"摘要：xxxx"这种把正文写在同一行的写法（不少模板如此）
            if s == alias or s.startswith(alias + "：") or s.startswith(alias + ":"):
                start_idx = i
                break
        if start_idx >= 0:
            break
    if start_idx < 0:
        return -1, -1

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        s = lines[j].strip()
        # 结束边界 = 下一个标题行，或下一个"关键词/Keywords"行。
        # 关键词行必须单独判：它本身不带编号，heading_kind 认不出来，
        # 不排除掉的话它会被算进摘要正文，摘要长度凭空多出十几个字。
        if s and len(s) <= 60 and (heading_kind(s) or _KW_HEAD_RE.match(s)):
            end_idx = j
            break

    body_start = offsets[start_idx]
    body_end = offsets[end_idx] if end_idx < len(lines) else len(text)
    return body_start, body_end


@verifier(
    "check_required_sections",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="必需章节是否齐全（摘要/关键词/Abstract/目录/结论/参考文献）",
)
def check_required_sections(text: str, ctx: Any) -> list[Issue]:
    """必需章节识别。

    判定用的是"整行标题模式"，不是全文包含。这一点很关键：几乎每篇论文的
    正文里都会出现"参考文献"四个字（"见参考文献[3]"），全文包含式判定会让
    "缺参考文献"这条 BLOCKER 永远报不出来。

    严重度差异是有意的：**缺参考文献 = BLOCKER**（等于全文没有出处），
    **缺摘要/关键词/结论 = MAJOR**（形式不合格但内容还在），**缺目录 = MINOR**
    （很多学校由排版工具自动生成）。全部报成 BLOCKER 会让 ``stop_on_blocker``
    在第一次检查后就掐掉所有后续付费检查。
    """
    issues: list[Issue] = []
    for key, aliases, sev, label in REQUIRED_SECTIONS:
        start, _ = _section_bounds(text, aliases)
        if start >= 0:
            continue
        issues.append(
            make_issue(
                kind=f"structure.missing_section.{key}",
                severity=sev,
                message=f"未找到「{label}」章节",
                text=text,
                # 没有原文可截，evidence 必须留空——编一句"证据"比漏报更糟，
                # 框架会因为找不到锚点而把这条判为 unverifiable（这是正确行为）。
                evidence="",
                fixable=False,
                fix_hint=f"按学校模板补上「{label}」章节。",
            )
        )
    return issues


@verifier(
    "check_heading_levels",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="标题层级跳级（如 1.1 之后直接 1.1.1.1）",
)
def check_heading_levels(text: str, ctx: Any) -> list[Issue]:
    """标题层级跳级。

    只对**阿拉伯数字编号**判断跳级（``1`` → ``1.1`` → ``1.1.1``）。中文编号
    （``第一章`` / ``一、`` / ``（一）``）与数字编号常常混排，硬放在一条数值轴
    上比较会造出一堆假跳级——这是本检查器最容易误报的地方，所以直接放弃那部分
    覆盖，只做能确证的一半。

    判据：层级只能"上升任意级"或"下降一级以内"。``1.1`` 后面直接出现
    ``1.1.1.1`` 是明确的跳级（中间漏了一层），报 MAJOR；从 ``1.1.1`` 回到
    ``2``（深度从 3 降到 1）是合法的章节切换，不报。
    """
    text = text or ""
    issues: list[Issue] = []
    last_depth = 0
    last_line = ""

    for pidx, off, line, _kind in headings(text):
        m = re.match(r"^(\d+(?:\.\d+)*)", line)
        if not m:
            continue
        depth = m.group(1).count(".") + 1
        if last_depth and depth > last_depth + 1:
            issues.append(
                make_issue(
                    kind="structure.heading_level_jump",
                    severity=Severity.MAJOR,
                    message=(
                        f"标题层级跳级：「{last_line}」（{last_depth} 级）之后直接出现"
                        f"「{line}」（{depth} 级）"
                    ),
                    text=text,
                    paragraph=pidx,
                    char=off,
                    find=anchor(text, line, off),
                    evidence=line,
                    fixable=False,  # 该补一个中间层标题，内容只有作者知道
                    fix_hint=f"在两者之间补上 {last_depth + 1} 级标题，或把「{line}」提升到 {last_depth + 1} 级。",
                )
            )
        last_depth = depth
        last_line = line
    return issues


#: 中文摘要常规区间。区间来自多数高校模板，但**各校不同**，所以只作为提示。
ABSTRACT_MIN_CHARS = 300
ABSTRACT_MAX_CHARS = 600


@verifier(
    "check_abstract_length",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="中文摘要长度（常规 300~600 字）与英文摘要是否存在",
)
def check_abstract_length(text: str, ctx: Any) -> list[Issue]:
    """摘要长度与英文摘要。

    严重度全部压在 INFO/MINOR：摘要长度是**规范区间**而不是硬性错误，
    不同学校要求 200~1000 字都有。把它报成 MAJOR 会让一篇完全合规的论文
    永远无法收敛，而"收敛不了"的代价是整个自我更正循环失去意义。

    长度按**汉字数**统计，不按 ``len()``：否则英文摘要的字母会把中文摘要的
    字数灌到超标。
    """
    text = text or ""
    issues: list[Issue] = []
    start, end = _section_bounds(text, ("摘要", "中文摘要"))
    if start < 0:
        return []  # 缺摘要是 check_required_sections 的职责，这里不重复报

    body = text[start:end]
    # 去掉标题行本身与"摘要："前缀，只留正文
    body = re.sub(r"^\s*摘\s*要\s*[:：]?", "", body.strip())
    n = count_cjk(body)

    if n and n < ABSTRACT_MIN_CHARS:
        issues.append(
            make_issue(
                kind="structure.abstract_too_short",
                severity=Severity.MINOR,
                message=f"中文摘要偏短：{n} 字（常规 {ABSTRACT_MIN_CHARS}~{ABSTRACT_MAX_CHARS} 字）",
                text=text,
                char=start,
                find=anchor(text, body[:40], start) if body[:40] else "",
                evidence=body[:40],
                fixable=False,
                fix_hint="补充研究问题、方法、主要结果与结论四要素；摘要不是引言的缩写。",
            )
        )
    elif n > ABSTRACT_MAX_CHARS:
        issues.append(
            make_issue(
                kind="structure.abstract_too_long",
                severity=Severity.INFO,
                message=f"中文摘要偏长：{n} 字（常规 {ABSTRACT_MIN_CHARS}~{ABSTRACT_MAX_CHARS} 字）",
                text=text,
                char=start,
                find=anchor(text, body[:40], start) if body[:40] else "",
                evidence=body[:40],
                fixable=False,
                fix_hint="精简到 300~600 字；若学校模板另有规定以模板为准。",
            )
        )

    # 英文摘要：只判断"是否存在"，不量长度——英文摘要的字数标准更分散，
    # 量了也只能给 INFO，收益低于噪声成本。
    en_start, _ = _section_bounds(text, ("Abstract", "ABSTRACT", "英文摘要"))
    if en_start < 0:
        issues.append(
            make_issue(
                kind="structure.missing_english_abstract",
                severity=Severity.MINOR,
                message="未找到英文摘要（Abstract）章节",
                text=text,
                evidence="",
                fixable=False,
                fix_hint="补上英文摘要，内容与中文摘要对应，注意专业术语的英文写法一致。",
            )
        )
    return issues


KEYWORD_SEPARATORS = ("；", ";", "，", ",")


@verifier(
    "check_keywords",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="关键词数量（常规 3~5 个）与分隔符一致性",
)
def check_keywords(text: str, ctx: Any) -> list[Issue]:
    """关键词数量与分隔符。

    为什么数量只报 MINOR：不同模板要求 3~5 个、3~8 个都有。真正值得报的是
    "只有 1 个""有 9 个"这种明显越界，以及**同一行里混用中英文分隔符**——
    后者是排版事故，而且会直接影响格式检查器的解析。

    分隔符一致性按"同一处关键词行的实际用法"判断，不看全文其他位置的逗号，
    因为把逗号误当成关键词分隔符是最常见的误报来源。
    """
    text = text or ""
    issues: list[Issue] = []

    for pidx, line, off in nonempty_index(text):
        m = _KW_HEAD_RE.match(line.strip())
        if not m:
            continue
        payload = m.group(2).strip()
        if not payload:
            continue  # 关键词换行写在下一行，本检查器不跨行取词（避免误抓正文）
        items = [k.strip() for k in re.split(r"[；;，,、]", payload) if k.strip()]
        n = len(items)

        # 分隔符一致性：同一行里同时出现中英文分隔符
        used = {sep for sep in KEYWORD_SEPARATORS if sep in payload}
        cjk_seps = used & {"；", "，", "、"}
        ascii_seps = used & {";", ","}
        if cjk_seps and ascii_seps:
            issues.append(
                make_issue(
                    kind="keywords.mixed_separator",
                    severity=Severity.MINOR,
                    message=f"关键词分隔符混用（同时出现 {'、'.join(sorted(used))}）",
                    text=text,
                    paragraph=pidx,
                    char=off,
                    find=anchor(text, line.strip(), off),
                    evidence=line.strip(),
                    fixable=False,  # 用哪个分隔符取决于模板，算子不该替作者决定
                    fix_hint="按学校模板统一：中文关键词多用分号或逗号，英文关键词用分号。",
                )
            )

        if n and n < 3:
            issues.append(
                make_issue(
                    kind="keywords.too_few",
                    severity=Severity.MINOR,
                    message=f"关键词只有 {n} 个（常规 3~5 个）",
                    text=text,
                    paragraph=pidx,
                    char=off,
                    find=anchor(text, line.strip(), off),
                    evidence=line.strip(),
                    fixable=False,
                    fix_hint="补足到 3~5 个，覆盖研究对象、方法与关键变量。",
                )
            )
        elif n > 8:
            issues.append(
                make_issue(
                    kind="keywords.too_many",
                    severity=Severity.INFO,
                    message=f"关键词有 {n} 个（常规 3~5 个）",
                    text=text,
                    paragraph=pidx,
                    char=off,
                    find=anchor(text, line.strip(), off),
                    evidence=line.strip(),
                    fixable=False,
                    fix_hint="精简到 3~5 个，保留最能定位本文贡献的词。",
                )
            )
    return issues


__all__ = [
    "check_required_sections",
    "check_heading_levels",
    "check_abstract_length",
    "check_keywords",
    "REQUIRED_SECTIONS",
]
