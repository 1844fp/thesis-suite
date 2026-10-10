"""参考文献层检查器。

这一层为什么单独成文件、而且严重度给得最重：参考文献是唯一能"从外部核对"的
部分（DOI、标题都能拿去 Crossref 查），也是 AI 代写翻车最集中的地方——编造一条
不存在的文献，在学术上等同于伪造证据。所以：

* 正文引了、列表里没有 → **BLOCKER**，不是"格式问题"。
* 列表里有、正文从没引用 → MAJOR（"凑参考文献"是评审一眼能看出来的动作）。
* 断号/重号 → MAJOR，且是少数**可以机械修复**的问题（``renumber_sequence``）。

反过来，这里刻意**不做**的事：

* 不判断"这条文献是否存在"（要联网，属于外部核验层）。
* 不判断"引用位置是否支持该论断"（需要读懂语义，规则做不到，只能交给模型+人）。
* 不猜 DOI 是否写错。没有证据的问题会把真问题淹没。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from ..framework.artifact import Issue, Severity
from ..framework.registry import verifier
from ._util import (
    anchor,
    make_issue,
    ref_authors,
    ref_doi,
    ref_title,
    ref_year,
    split_reference_section,
)

#: 正文引用序号。覆盖 ``[1]`` / ``[1,2]`` / ``[1-3]`` / ``[1，2]`` / ``【1】``。
CITE_RE = re.compile(r"[\[【]\s*(\d{1,3}(?:\s*[-,，–—~]\s*\d{1,3})*)\s*[\]】]")
_NUM_IN_GROUP = re.compile(r"\d{1,3}")

#: 重排参考文献编号用的正则，**导出给修复器复用**。
#:
#: 与 ``numbers.RENUMBER_PATTERNS`` 同样的理由：``renumber_sequence`` 只认
#: pattern 里的第一个捕获组，修复器若自己另写一个，就会出现"检查器报得出、
#: 修复器改不动"的编号。pattern 用 ``(?m)^`` 锚在行首，天然只匹配列表条目，
#: 不会碰正文里的 ``[3]`` 引用。
#: 调用方还必须传 ``scope_start``（参考文献区起点），双保险。
RENUMBER_PATTERNS: dict[str, str] = {
    "reference": r"(?m)^\s*[\[【]\s*(\d{1,3})(?!\d)\s*[\]】]",
}


def _body_citations(text: str, body_end: int) -> dict[int, int]:
    """抽出正文里的引用编号 → 出现次数。

    只在参考文献区**之前**扫：参考文献条目自己就长着 ``[1]``，扫进去会让
    "正文引用了"这个判断永远为真，检查器就废了。
    """
    head = text[:body_end] if body_end > 0 else text
    out: dict[int, int] = {}
    for m in CITE_RE.finditer(head):
        # 排除 "式[3]" 之外的情况这里不做额外过滤：正文里 [n] 的语义就是引用，
        # 公式编号用的是全角括号 (3-1)，两者不会撞车。
        for num in _NUM_IN_GROUP.findall(m.group(1)):
            n = int(num)
            out[n] = out.get(n, 0) + 1
    return out


@verifier(
    "check_ref_numbering",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="正文引用序号与参考文献列表编号的一一对应（缺失/未引用/断号重号）",
)
def check_ref_numbering(text: str, ctx: Any) -> list[Issue]:
    """正文 ``[n]`` ↔ 文献表编号的**双向**一致性。

    三项都查，因为三项的后果完全不同：正文引了不存在的编号是致命错误；
    列表里躺着一堆没人引用的条目是评审立刻会问的问题；断号是排版事故。
    只查其中一两项，作者就会以为"编号没问题"。
    """
    sec_start, sec_end, entries = split_reference_section(text)
    if sec_start < 0 or not entries:
        # 没有参考文献区（或空表）时不报编号问题：此时"引用无对应条目"的
        # 根因是"整篇没有参考文献"，那是结构层 check_required_sections 的事，
        # 在这里报会让作者收到几十条重复的 BLOCKER。
        return []

    issues: list[Issue] = []
    listed = [n for _, _, n, _ in entries if n is not None]
    listed_set = set(listed)
    cited = _body_citations(text, sec_start)

    # --- 1. 正文引用但列表没有：BLOCKER ---
    missing = sorted(n for n in cited if n not in listed_set)
    for n in missing:
        m = re.search(rf"[\[【]\s*{n}\s*[\]】]", text[:sec_start])
        snip = m.group(0) if m else f"[{n}]"
        pos = m.start() if m else 0
        f = anchor(text, snip, pos)
        issues.append(
            make_issue(
                kind="citation.missing_in_reflist",
                severity=Severity.BLOCKER,
                message=f"正文引用了 [{n}]，但参考文献列表中没有编号 [{n}] 的条目",
                text=text,
                char=pos,
                find=f,
                evidence=snip,
                fixable=False,
                fix_hint=f"核对 [{n}] 指向的文献：补进参考文献列表，或删除/改正这处引用。",
            )
        )

    # --- 2. 列表有但正文从未引用：MAJOR ---
    orphans = [n for n in listed if n not in cited]
    for n in sorted(set(orphans)):
        entry = next((e for _, _, num, e in entries if num == n), "")
        f = anchor(text, entry, sec_end)
        issues.append(
            make_issue(
                kind="reference.never_cited",
                severity=Severity.MAJOR,
                message=f"参考文献 [{n}] 在正文中从未被引用（疑似凑数或漏标引用）",
                text=text,
                find=f,
                evidence=entry,
                fixable=False,
                fix_hint="在正文合适位置补上引用标注；若该文献确实未使用，删除该条目。",
            )
        )

    # --- 3a. 重号：同一个编号出现在两条条目上 ---
    seen: set[int] = set()
    for _, _, num, entry in entries:
        if num is None:
            continue
        if num in seen:
            f = anchor(text, entry, sec_end)
            issues.append(
                make_issue(
                    kind="reference.duplicate_number",
                    severity=Severity.MAJOR,
                    message=f"参考文献编号 [{num}] 重复出现",
                    text=text,
                    find=f,
                    evidence=entry,
                    fixable=False,  # 需要作者判断哪条该删，算子无法决定
                    fix_hint="删掉重复编号的那一条，或整表重新编号后再核对正文引用。",
                )
            )
        seen.add(num)

    # --- 3b. 断号：编号不连续 ---
    if listed:
        expected = set(range(min(listed), max(listed) + 1))
        gaps = sorted(expected - listed_set)
        if gaps:
            first_entry = entries[0][3]
            f = anchor(text, first_entry, sec_end)
            issues.append(
                make_issue(
                    kind="reference.number_gap",
                    severity=Severity.MAJOR,
                    message=(
                        f"参考文献编号不连续，缺 {[f'[{g}]' for g in gaps]}"
                        f"（现有 {min(listed)}~{max(listed)}）"
                    ),
                    text=text,
                    find=f,
                    evidence=first_entry,
                    # 断号是纯机械问题，可以从列表头开始重排一次。
                    # 注意只重排**列表区**：正文里的 [n] 是引用语义，不能一起改，
                    # 否则会把"引用了哪条文献"改掉——那是学术内容，不是格式。
                    fixable=True,
                    fix_op="renumber_sequence",
                    fix_hint=(
                        "把参考文献列表重新编号为连续序号，pattern 复用 "
                        "RENUMBER_PATTERNS['reference']，并用 scope_start 限定在"
                        "参考文献区起点之后，避免改动正文里的引用标注。"
                    ),
                )
            )

    return issues


@verifier(
    "check_ref_duplicates",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="参考文献列表内的重复条目（DOI 相同或标题高度相似）",
)
def check_ref_duplicates(text: str, ctx: Any) -> list[Issue]:
    """重复条目：DOI 相同 → 确定重复；标题相似度 ≥0.92 且年份相同 → 疑似重复。

    为什么用 0.92 这么高的阈值：参考文献标题动辄三四十字，两篇**不同**论文
    标题相似度上 0.8 很常见（同一个课题组连续发文的标题结构高度雷同）。
    阈值调低会把"同门师兄的两篇论文"报成重复，作者看一眼就不信这个工具了。
    宁可漏报疑似重复，也不误报——这条纪律在"重复检测"这类任务上尤其重要。
    """
    _, sec_end, entries = split_reference_section(text)
    if len(entries) < 2:
        return []

    issues: list[Issue] = []
    reported: set[tuple[int, int]] = set()

    for i in range(len(entries)):
        for j in range(i + 1, len(entries)):
            ei, ej = entries[i][3], entries[j][3]
            di, dj = ref_doi(ei), ref_doi(ej)
            ti, tj = ref_title(ei), ref_title(ej)
            yi, yj = ref_year(ei), ref_year(ej)

            reason = ""
            if di and di == dj:
                reason = f"DOI 相同（{di}）"
            elif ti and tj:
                ratio = SequenceMatcher(None, ti.lower(), tj.lower()).ratio()
                if ratio >= 0.92 and yi and yi == yj:
                    reason = f"标题高度相似（相似度 {ratio:.2f}）且年份相同（{yi}）"
                elif ti == tj and yi == yj:
                    reason = "标题与年份完全相同"
            if not reason:
                continue

            key = (i, j)
            if key in reported:
                continue
            reported.add(key)
            f = anchor(text, ej, sec_end)
            issues.append(
                make_issue(
                    kind="reference.duplicate_entry",
                    severity=Severity.MAJOR,
                    message=f"参考文献 [{entries[i][2]}] 与 [{entries[j][2]}] 疑似同一条：{reason}",
                    text=text,
                    find=f,
                    evidence=ej,
                    fixable=False,  # 删哪条、保留哪个版本是作者的判断
                    fix_hint="确认是否为同一文献：是则删除其中一条并修正正文引用；否则补充区分信息（页码/版次）。",
                )
            )
    return issues


@verifier(
    "check_ref_format",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="参考文献条目形态：缺年份/缺作者/缺标题/编号格式不统一",
)
def check_ref_format(text: str, ctx: Any) -> list[Issue]:
    """条目形态检查。

    只报"确定缺字段"，不做字段级规范化（如"作者应姓在前"）——后者要做对必须
    先判断条目类型（期刊/会议/学位论文/标准），判断错了就是错误建议。
    给出的 fix_hint 是给人看的修复方向，**不标 fixable**：往条目里插入一个
    编造出来的年份比不修更糟。
    """
    _, sec_end, entries = split_reference_section(text)
    if not entries:
        return []

    issues: list[Issue] = []

    for _, _, num, entry in entries:
        label = f"[{num}]" if num is not None else "(无编号)"
        f = anchor(text, entry, sec_end)

        if not ref_year(entry):
            issues.append(
                make_issue(
                    kind="reference.missing_year",
                    severity=Severity.MAJOR,
                    message=f"参考文献 {label} 中未找到出版年份",
                    text=text,
                    find=f,
                    evidence=entry,
                    fixable=False,
                    fix_hint="补上出版年份（GB/T 7714 要求年份紧跟作者或位于出版信息首位）。",
                )
            )

        authors = ref_authors(entry)
        # 作者段至少要有一个汉字或两个拉丁字母，否则认为是"作者缺失"
        if len(re.findall(r"[\u4e00-\u9fff]", authors)) < 2 and \
           len(re.findall(r"[A-Za-z]", authors)) < 2:
            issues.append(
                make_issue(
                    kind="reference.missing_author",
                    severity=Severity.MAJOR,
                    message=f"参考文献 {label} 中未识别出作者",
                    text=text,
                    find=f,
                    evidence=entry,
                    fixable=False,
                    fix_hint="补上作者姓名（中文姓前名后，英文姓前名后并用全大写或首字母缩写保持一致）。",
                )
            )

        title = ref_title(entry)
        # 标题过短基本等于没抽到：可能整条只有作者和年份。
        if len(re.sub(r"\s", "", title)) < 4 and "[J]" not in entry and "[M]" not in entry:
            issues.append(
                make_issue(
                    kind="reference.missing_title",
                    severity=Severity.MAJOR,
                    message=f"参考文献 {label} 中未识别出文献标题",
                    text=text,
                    find=f,
                    evidence=entry,
                    fixable=False,
                    fix_hint="补上文献标题，并带上文献类型标识（[J]/[M]/[D]/[C]）。",
                )
            )

    # 编号格式不统一：同一列表里混用 [1] 与 1. 两种写法。
    # 这属于排版层问题，但仍然值得报：模板检查会因此大面积误判。
    styles = {re.match(r"^\s*([\[【]|\d+\s*[.、．])", e).group(1) for _, _, _, e in entries if re.match(r"^\s*([\[【]|\d+\s*[.、．])", e)}
    bracket_style = {s for s in styles if s in ("[", "【")}
    plain_style = {s for s in styles if s not in ("[", "【")}
    if bracket_style and plain_style:
        first = entries[0][3]
        issues.append(
            make_issue(
                kind="reference.numbering_style_mixed",
                severity=Severity.MINOR,
                message="参考文献列表内编号格式不统一（同时存在 [1] 与 1. 两种写法）",
                text=text,
                find=anchor(text, first, sec_end),
                evidence=first,
                fixable=False,
                fix_hint="按学校模板统一为一种编号写法（多数中文模板要求方括号形式）。",
            )
        )

    return issues


#: 正文"作者(年份)"式引用。作者段限制在 2~12 个非标点字符内并须以汉字或字母开头，
#: 避免把 "（2020）" 这种孤立括号或 "(3)" 公式号吸进来。
AUTHOR_YEAR_RE = re.compile(
    r"([\u4e00-\u9fff]{2,4}(?:等|等人)?|[A-Z][A-Za-z\-]{1,20}(?:\s+et\s+al\.?)?)"
    r"\s*[（(]\s*((?:19|20)\d{2}[a-z]?)\s*[）)]"
)
_YEAR_ANY = re.compile(r"(?:19|20)\d{2}")


@verifier(
    "check_citation_orphans",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="正文「作者(年份)」式引用在参考文献列表中找不到对应条目",
)
def check_citation_orphans(text: str, ctx: Any) -> list[Issue]:
    """正文有 ``作者(年份)``，文献表里却没有这个人/这一年。

    判定规则刻意保守，只在两种**能确证没有对应条目**的情况下报：

    1. 该引用的**年份**在整张文献表里一次都没出现过；
    2. 该引用的**作者姓氏**在整张文献表里一次都没出现过（且年份也不匹配）。

    不报的情况：文献表里存在同年条目但作者不同——此时可能是"该文献在表中但
    姓名写法不同（拼音/缩写）"，规则判定不了，报了就是假阳性。
    """
    sec_start, _, entries = split_reference_section(text)
    if not entries:
        return []

    refs_text = " ".join(e for _, _, _, e in entries)
    ref_years = set(_YEAR_ANY.findall(refs_text))
    # 姓名字面集合：中英文分开比，中文按 2 字连续片段，英文按整词
    ref_names: set[str] = set()
    for _, _, _, e in entries:
        author_seg = ref_authors(e)
        ref_names.update(re.findall(r"[\u4e00-\u9fff]{2,4}", author_seg))
        ref_names.update(w.lower() for w in re.findall(r"[A-Za-z]{3,}", author_seg))

    head = text[:sec_start] if sec_start > 0 else text
    issues: list[Issue] = []
    seen_spans: set[tuple[int, int]] = set()

    for m in AUTHOR_YEAR_RE.finditer(head):
        name, year = m.group(1), m.group(2)
        if (m.start(), m.end()) in seen_spans:
            continue
        seen_spans.add((m.start(), m.end()))

        name_core = re.sub(r"(等|等人)$", "", name)
        name_hit = name_core in refs_text or any(
            name_core.lower() in n or n in name_core.lower() for n in ref_names if len(n) >= 3
        )
        year_hit = year in ref_years
        if name_hit or year_hit:
            continue

        snip = m.group(0)
        issues.append(
            make_issue(
                kind="citation.orphan_author_year",
                severity=Severity.BLOCKER,
                message=f"正文引用「{snip}」在参考文献列表中找不到对应条目",
                text=text,
                char=m.start(),
                find=anchor(text, snip, m.start()),
                evidence=snip,
                fixable=False,
                fix_hint="补上该文献条目，或确认正文引用写法（作者名/年份）与文献表一致。",
            )
        )
    return issues
