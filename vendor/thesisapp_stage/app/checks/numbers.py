"""图表与公式编号检查器。

为什么独立成一层：图、表、公式编号是**交叉引用**的锚点。编号一旦断裂，
评审翻回正文核对时找不到图，整段论证的可信度就没了。这类问题有个共同特点：
**纯机械、可双向核对**——"正文提到图 3"与"图 3 的题注存在"是两个可独立枚举
的集合，比对即可，不需要任何语义理解。

这一层刻意不做的判断：

* **不检查图片是否真的存在**（纯文本里没有图片信息，猜就是造假）。
* **不判断题注措辞是否规范**（"图 3-1 系统架构"还是"图 3.1 系统架构图"，
  取决于学校模板，规则无法知道）。
* **无法判断图题上下位置时一律不报**（见 ``check_caption_placement``）。
"""

from __future__ import annotations

import re
from typing import Any

from ..framework.artifact import Issue, Severity
from ..framework.registry import verifier
from ._util import anchor, make_issue, nonempty_index, split_paragraphs

#: 图表编号：图3、图 3、图3-1、图3.1、图 3–1、图 2.3.1
LABEL_NUM = r"\d{1,2}(?:\s*[-–—.．]\s*\d{1,2}){0,3}"
FIG_RE = re.compile(rf"图\s*({LABEL_NUM})")
TAB_RE = re.compile(rf"表\s*({LABEL_NUM})")

#: 题注的判据："以标签开头，且标签后面没有引导动词"。
#: 用引导词而不是长度来收口——"图 3 所示……"、"图 3 中展示了……"是正文，
#: 而"图 3-1 系统架构"是题注，两者的长度可能差不多，只有动词能区分。
_CAPTION_TAIL_BAD = (
    "所示", "中给出", "中展示", "展示了", "显示了", "中可以看出", "可见",
    "对应的", "中的", "为本文", "如下",
)

#: 公式编号：``(3-1)`` / ``（3.1）`` / ``(1)``，须贴在一行末尾。
#: 只认括号式：不认 "式 3-1" 这种写法，因为它和正文里的"第 3-1 节"无法区分。
EQ_RE = re.compile(r"[（(]\s*(\d{1,2}(?:\s*[-–—.]\s*\d{1,2})?)\s*[）)]\s*$")

#: 重排编号用的正则，**导出给修复器复用**。
#:
#: 为什么必须导出：``renumber_sequence`` 只认 ``pattern`` 里的第一个捕获组，
#: 不同的编号体系需要不同的 pattern。如果修复器自己另写一个，就会出现
#: "检查器报了一个它自己认得出、修复器却重排不动的编号"——测试专测这一条。
#: 数字部分都加了 ``(?!\d)``，防止 `12` 被切成 `1`+`2` 这种灾难性错排。
RENUMBER_PATTERNS: dict[str, str] = {
    # 图题注行：行首"图 N"，只匹配行首标签，不动正文里的"图 N 所示"
    "figure": r"(?m)^图\s*(\d{1,2})(?!\d)",
    "table": r"(?m)^表\s*(\d{1,2})(?!\d)",
    # 公式：整行以括号编号结尾（"y = ax + b    (3-1)"）。
    # 锚点同时在 ^ 与 $，所以正文里"结论见(3-1)处的公式"这类句中引用不会被重排——
    # 这是本组 pattern 里最需要防的误改。
    "equation": r"(?m)^[^\n]*[（(]\s*(\d{1,2}(?:\s*[-–—.]\s*\d{1,2})?)(?!\d)\s*[）)]\s*$",
}


def _is_caption_line(line: str) -> bool:
    """整行是否为题注（以"图 X"/"表 X"开头，且不是"图 X 所示"这类正文）。"""
    s = line.strip()
    if not s:
        return False
    if not (s.startswith("图") or s.startswith("表")):
        return False
    m = re.match(rf"^[图表]\s*{LABEL_NUM}\s*(.*)$", s)
    if not m:
        return False
    tail = m.group(1).strip()
    if not tail:
        return False  # 只有"图 3"，没有题注文字 → 不认定为题注
    if any(tail.startswith(bad) for bad in _CAPTION_TAIL_BAD):
        return False
    # 尾巴里带句末标点基本是正文整句（"图 3 是系统架构，由……"）
    return not tail.endswith(("。", "！", "？"))


def _collect_labels(text: str) -> tuple[dict[str, list[tuple[int, int, str]]], dict[str, list[tuple[int, int, str]]]]:
    """返回 ``(题注, 提及)``，各自是 ``{编号: [(段落下标, 字符偏移, 命中串)]}``。

    区分题注与提及是本层的核心：题注以标签**开头**，提及出现在句子中间。
    这个区分不完美（"图 3 所示"开头的正文会被误判为提及时恰好是对的），
    但它的错误方向是安全的——最坏情况是把正文里的提及算作题注，从而少报，
    而不是凭空造出一条"图不存在"的假问题。
    """
    captions: dict[str, list[tuple[int, int, str]]] = {}
    mentions: dict[str, list[tuple[int, int, str]]] = {}
    for idx, line, off in nonempty_index(text):
        stripped = line.strip()
        lead = len(line) - len(line.lstrip())
        if _is_caption_line(stripped):
            m = re.match(rf"^([图表])\s*({LABEL_NUM})", stripped)
            label = m.group(1) + _norm_num(m.group(2))
            captions.setdefault(label, []).append((idx, off + lead, stripped))
            # 题注本身也计入"提到过"：否则"有题注但正文没提到"会全量误报
            mentions.setdefault(label, []).append((idx, off + lead, stripped))
            continue
        for rx, tag in ((FIG_RE, "图"), (TAB_RE, "表")):
            for m in rx.finditer(line):
                label = tag + _norm_num(m.group(1))
                mentions.setdefault(label, []).append((idx, off + m.start(), m.group(0)))
    return captions, mentions


def _norm_num(raw: str) -> str:
    """编号归一化：去掉空白、统一连接符与全角点，便于跨写法比较。"""
    return re.sub(r"[\s\-–—.．]", "-", raw.strip())


@verifier(
    "check_figure_table_numbering",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="图/表编号连续性与正文提及↔题注的双向一致性",
)
def check_figure_table_numbering(text: str, ctx: Any) -> list[Issue]:
    """图、表编号：重号、断号，以及"提到但没题注"两个方向。

    两项都给 MAJOR 而不是 BLOCKER：图表编号错不会构成学术不端，但评审一定会
    要求改。相反，把这类问题报成 BLOCKER 会让 ``stop_on_blocker`` 提前掐掉
    付费检查，真正的内容问题反而没人看。
    """
    captions, mentions = _collect_labels(text)
    issues: list[Issue] = []

    for tag, rx, op_key in (("图", FIG_RE, "figure"), ("表", TAB_RE, "table")):
        labels = [k for k in captions if k.startswith(tag)]
        if not labels:
            continue

        # --- 1. 重号：同一个编号出现两次题注 ---
        for label in sorted(set(labels)):
            hits = captions[label]
            if len(hits) > 1:
                for pidx, off, snip in hits[1:]:
                    issues.append(
                        make_issue(
                            kind="figure.duplicate_number",
                            severity=Severity.MAJOR,
                            message=f"{label} 编号重复，出现 {len(hits)} 次题注",
                            text=text,
                            paragraph=pidx,
                            char=off,
                            find=anchor(text, snip, off),
                            evidence=snip,
                            fixable=False,
                            fix_hint="按出现顺序重新编号全套图表，并同步修正正文中的交叉引用。",
                        )
                    )

        # --- 2. 断号：编号序列不连续 ---
        # 只对"单段数字"编号（图1、图2…）判断连续性。形如 图3-1 的章节式编号
        # 天然是分章重排的，全文层面本来就"不连续"，硬判会全是假阳性。
        plain = sorted({int(k[1:]) for k in labels if re.fullmatch(rf"{tag}\d+", k)})
        if len(plain) >= 2:
            gaps = sorted(set(range(min(plain), max(plain) + 1)) - set(plain))
            if gaps:
                first = captions[f"{tag}{plain[0]}"][0]
                issues.append(
                    make_issue(
                        kind=f"{op_key}.number_gap",
                        severity=Severity.MAJOR,
                        message=f"{tag}编号不连续，缺少 {[f'{tag}{g}' for g in gaps]}（现有 {tag}{min(plain)}~{tag}{max(plain)}）",
                        text=text,
                        paragraph=first[0],
                        char=first[1],
                        find=anchor(text, first[2], first[1]),
                        evidence=first[2],
                        fixable=True,
                        fix_op="renumber_sequence",
                        fix_hint=(
                            f"把{tag}题注按出现顺序重排为连续编号。pattern 复用"
                            f"RENUMBER_PATTERNS['{op_key}']（只匹配行首的「{tag} N」），"
                            f"不会动正文里的交叉引用；改完仍需人工核对正文引用。"
                        ),
                    )
                )

    # --- 3. 正文提到但没有题注 ---
    for label in sorted(mentions):
        if label in captions:
            continue
        pidx, off, snip = mentions[label][0]
        issues.append(
            make_issue(
                kind="figure.mention_without_caption",
                severity=Severity.MAJOR,
                message=f"正文提到了「{label}」，但全文找不到该{label[0]}的题注",
                text=text,
                paragraph=pidx,
                char=off,
                find=anchor(text, snip, off),
                evidence=snip,
                fixable=False,
                fix_hint=f"补上{label}的{label[0]}与题注，或改正正文中的交叉引用编号。",
            )
        )

    # --- 4. 有题注但正文从未提及 ---
    # 这条只给 MINOR：学位论文里附录图表、实验装置图确实可能不逐一在正文提及。
    for label in sorted(captions):
        if len(mentions.get(label, [])) > 1:  # 题注自身算一次
            continue
        pidx, off, snip = captions[label][0]
        issues.append(
            make_issue(
                kind="figure.caption_never_referenced",
                severity=Severity.MINOR,
                message=f"{label} 有题注但正文中未出现交叉引用",
                text=text,
                paragraph=pidx,
                char=off,
                find=anchor(text, snip, off),
                evidence=snip,
                fixable=False,
                fix_hint=f"在正文中补一句「如{label}所示」，使图表与论述建立引用关系。",
            )
        )

    return issues


@verifier(
    "check_caption_placement",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="图题应位于图下方、表题应位于表上方（按段落相对顺序推断）",
)
def check_caption_placement(text: str, ctx: Any) -> list[Issue]:
    """图题在图下方、表题在表上方。

    **纯文本里没有图片本身**，所以这里只能做"相对顺序"推断，并且只在能确定
    情形时才报：

    * 题注**两侧紧邻都是正文（无空行隔开）** → 说明它被写进了正文段落流里，
      这几乎总是"图题写在图之前"或"表题跟在表之后"的手工排版事故。
    * 题注相邻任一侧是空行 → 无法判断图片落点，**不报**。

    这个规则看起来弱，但它符合"没有证据不报"的纪律：本检查器的价值在于
    抓住最典型的一类错误（题注夹在正文行中间），而不是穷举所有错位。
    """
    paras = split_paragraphs(text)
    issues: list[Issue] = []

    # 预计算每个非空段落的原下标与偏移
    idx_off: dict[int, int] = {}
    pos = 0
    for i, line in enumerate(paras):
        idx_off[i] = pos
        pos += len(line) + 1

    for i, line in enumerate(paras):
        if not line.strip():
            continue
        kind = _is_caption_line(line)
        if not kind:
            continue
        s = line.strip()
        label = "图" if s.startswith("图") else "表"

        prev_blank = i == 0 or not paras[i - 1].strip()
        next_blank = i == len(paras) - 1 or not paras[i + 1].strip()
        # 题注独占一行且两侧都有空行 → 位置合理（图/表本身占掉其中一侧）
        if prev_blank or next_blank:
            continue
        # 两侧都紧贴正文 → 题注被夹在正文流里，几乎必然是位置错误
        issues.append(
            make_issue(
                kind="caption.suspicious_placement",
                severity=Severity.MINOR,
                message=f"{label}题「{s}」夹在正文段落之间（无法确认它在{label}的上方还是下方）",
                text=text,
                paragraph=i,
                char=idx_off[i],
                find=anchor(text, s, idx_off[i]),
                evidence=s,
                fixable=False,
                fix_hint=(
                    f"按规范：图题置于图下方、表题置于表上方，并与正文之间留出空行；"
                    f"此处上下文无法自动判断，需人工确认。"
                ),
            )
        )
    return issues


@verifier(
    "check_equation_numbering",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="公式编号的连续性（括号式编号，如 (3-1)）",
)
def check_equation_numbering(text: str, ctx: Any) -> list[Issue]:
    """公式编号连续性。

    只处理**行尾括号编号**（``…… = f(x)    (3-1)``）：这是学位论文的标准写法，
    也是唯一能凭文本可靠识别的情形。不处理"式 3-1"。

    与图表编号不同，公式编号在很多学校是**按章独立编号**的（3-1, 3-2, 4-1），
    全文合起来看必然"不连续"。所以这里按"前缀 + 序号"分组，只在**同一章内**
    判断连续性；跨章不比较。这是本检查器最重要的取舍。
    """
    text = text or ""
    # (段落, 段内命中起点, 全文字符偏移, 命中原文(带行首空白), 全文编号, 章前缀, 序号)
    hits: list[tuple[int, int, int, str, str, str, int]] = []
    for i, line, off in nonempty_index(text):
        m = EQ_RE.search(line)
        if not m:
            continue
        # 证据/锚点取**整行**。为什么不只取 `(3-1)`：正文里"结论见(3-1)处的公式"
        # 这种句中引用和行尾编号的字面完全相同，只用括号编号当锚点会撞上它，
        # 导致 replace/apply_edits 因"定位不唯一"而拒绝执行。整行作锚点后两者
        # 天然不同（一个有公式主体、一个没有），而且顺带把"这是独立编号行"这一
        # 事实带进了证据里，与 RENUMBER_PATTERNS["equation"] 的 ^...$ 完全对齐。
        hit_start = 0
        snippet = line.rstrip()
        full = _norm_num(m.group(1))
        parts = full.split("-")
        if len(parts) == 1:
            chapter, seq = "", int(parts[0]) if parts[0].isdigit() else 0
        else:
            chapter, seq = "-".join(parts[:-1]), int(parts[-1]) if parts[-1].isdigit() else 0
        hits.append((i, hit_start, off + hit_start, snippet, full, chapter, seq))

    if len(hits) < 2:
        return []

    issues: list[Issue] = []
    groups: dict[str, list[tuple[int, int, int, str, str, str, int]]] = {}
    for h in hits:
        groups.setdefault(h[5], []).append(h)

    for chapter, group in sorted(groups.items()):
        seqs = [h[6] for h in group]
        if len(seqs) < 2:
            continue
        dupes = sorted({s for s in seqs if seqs.count(s) > 1})
        gaps = sorted(set(range(min(seqs), max(seqs) + 1)) - set(seqs))
        for d in dupes:
            h = next(x for x in group if x[6] == d)
            label = f"({h[4]})" if chapter == "" else f"({chapter}-{d})"
            issues.append(
                make_issue(
                    kind="equation.duplicate_number",
                    severity=Severity.MAJOR,
                    message=f"公式编号 {label} 重复",
                    text=text,
                    paragraph=h[0],
                    char=h[2],
                    find=anchor(text, h[3], h[2]),
                    evidence=h[3],
                    fixable=False,
                    fix_hint="按顺序重排本章公式编号，并核对正文中对该公式的引用。",
                )
            )
        if gaps:
            h = group[0]
            scope = f"第 {chapter} 章" if chapter else "全文"
            label = f"({h[4]})"
            issues.append(
                make_issue(
                    kind="equation.number_gap",
                    severity=Severity.MAJOR,
                    message=f"{scope}公式编号不连续，缺少序号 {gaps}（现有 {min(seqs)}~{max(seqs)}）",
                    text=text,
                    paragraph=h[0],
                    char=h[2],
                    find=anchor(text, h[3], h[2]),
                    evidence=h[3],
                    # 只重排"缩进 + 编号") 结尾"这种行尾括号编号。
                    # 不能用宽松的 \(\d+\)：那会把正文里的"（2020）"、
                    # 图表编号甚至公式内部的括号一起重排，属于灾难性误改。
                    fixable=True,
                    fix_op="renumber_sequence",
                    fix_hint=(
                        "将本章公式编号重排为连续序号。pattern 复用 "
                        "RENUMBER_PATTERNS['equation']（只匹配行尾缩进括号编号），"
                        "正文里的引用标注不受影响，但改完仍需人工核对。"
                    ),
                )
            )
    return issues
