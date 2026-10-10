"""检查器共用的纯文本工具。

为什么单独放一层：段落切分、句子切分、章节识别、参考文献条目粗抽这几件事，
六七个检查器都要用。如果各写一份，同一条文本在不同检查器眼里会切得不一样，
于是"这个检查器说在段落 3、那个说在段落 5"，复验和修复定位就全乱了。

这里所有函数都是**只读**的：输入 str，输出数据，不改任何状态，也不调用模型。

另一个贯穿全文件的约束：**位置必须可复验**。框架的 ``refresh_issue_status``
靠 ``location.find``（优先）和 ``evidence`` 里能否在改后的文本中再找到原文来
判断"这条问题还在不在"。所以这里提供 :func:`anchor`：它不满足于"在原文里找得到"，
而是要求**唯一**——同一段文字出现两次时，任何一处 patch 都可能改错地方，
宁可放弃机械修复（fixable=False）也不能赌。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..framework.artifact import Issue, Severity

# --------------------------------------------------------------------------
# 字符类：把"什么算中文"固定在一处，避免各检查器对边界各持一套标准
# --------------------------------------------------------------------------

CJK = r"\u4e00-\u9fff"
CJK_RE = re.compile(rf"[{CJK}]")


def count_cjk(s: str) -> int:
    """汉字个数。论文字数统计按汉字算，不按 len()——英文摘要不能混进来。"""
    return len(CJK_RE.findall(s or ""))


# --------------------------------------------------------------------------
# 段落
# --------------------------------------------------------------------------


def split_paragraphs(text: str) -> list[str]:
    """按行切段。

    为什么不按空行切：中文论文里"一段一行"和"一段多行"两种写法都常见，
    而空行数量本身又是一个待检查项（``check_empty_paragraphs``）。按行切可以让
    "哪一行是空的"保持可见，调用方要用"非空行"这个概念时自己过滤即可。
    """
    if not text:
        return []
    return text.split("\n")


def nonempty_index(text: str) -> list[tuple[int, str, int]]:
    """返回 ``[(段落下标, 段落文本, 段首字符偏移)]``，只含非空段落。

    字符偏移是给 ``location["char"]`` 用的：段落下标只在"按同样规则切分"时才有
    意义，字符偏移是绝对的，两者都给，界面和修复器各取所需。
    """
    out: list[tuple[int, str, int]] = []
    pos = 0
    for i, line in enumerate(split_paragraphs(text)):
        out.append((i, line, pos)) if line.strip() else None
        pos += len(line) + 1  # +1 是被 split 吃掉的换行符
    return out


def find_paragraph_of(text: str, char_index: int) -> int:
    """由字符偏移反查段落下标；越界时返回 -1。

    检查器统一用 :func:`nonempty_index` 直接拿到段落下标，这个反查函数留给
    消费 Issue 的一侧使用（界面按 ``location["char"]`` 高亮时需要知道在第几段）。
    """
    if char_index < 0:
        return -1
    pos = 0
    for i, line in enumerate(split_paragraphs(text)):
        if pos <= char_index <= pos + len(line):
            return i
        pos += len(line) + 1
    return -1


# --------------------------------------------------------------------------
# 句子
# --------------------------------------------------------------------------

_SENT_END = re.compile(rf"[。！？；!?;](?=[{CJK}“”（(]|$)")


def split_sentences(text: str) -> list[tuple[int, int, str]]:
    """中文句子切分，返回 ``[(start, end, sentence)]``。

    取舍：**句号/问号/叹号/分号**都算句子边界，逗号不算。分号在中文里常常连接
    两个完整分句，长句检查按分号断开能少报一半假阳性；代价是"用分号串起的长句"
    会被漏掉——这是有意接受的漏报，因为没有证据的问题比漏报更有害。

    英文句点不参与切分：``et al.`` / ``Fig. 3`` / ``3.14`` 会让切分碎成一地。
    """
    out: list[tuple[int, int, str]] = []
    text = text or ""
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end()
        piece = text[start:end]
        if piece.strip():
            out.append((start, end, piece.strip()))
        start = end
    tail = text[start:]
    if tail.strip():
        out.append((start, len(text), tail.strip()))
    return out


# --------------------------------------------------------------------------
# 章节
# --------------------------------------------------------------------------

#: 段落本身就是一行标题的几种写法。刻意要求"整行匹配"，
#: 否则正文里"见 1.2 节"这种句子会被当成标题。
HEADING_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("multi", re.compile(r"^\d+(?:\.\d+){1,5}\s*[、.．]?\s*[\u4e00-\u9fff\w]")),
    ("num", re.compile(r"^\d+(?:[、.．]\s*|\s+)[\u4e00-\u9fff]")),
    ("chapter", re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*[章节篇部分]")),
    ("cn_num", re.compile(r"^[一二三四五六七八九十]+\s*[、.．]")),
    ("cn_paren", re.compile(r"^[（(]\s*[一二三四五六七八九十]+\s*[）)]")),
    ("appendix", re.compile(r"^(附录|致谢|参考文献|摘要|目录|结论|展望|Abstract)", re.IGNORECASE)),
)

#: 标题行长度上限：超过这个长度的行更可能是正文而不是标题。
HEADING_MAX_CHARS = 60


def heading_kind(line: str) -> str:
    """判断一行是不是标题，返回模式名；不是标题返回空串。

    为什么加长度上限：``1.1 本文提出了一种基于注意力机制的……（很长的一句话）``
    在正文里很常见，靠前缀识别会把它当标题，进而把整个章节层级算错。
    长度上限是最便宜、最不容易误伤的判据。
    """
    s = (line or "").strip()
    if not s or len(s) > HEADING_MAX_CHARS:
        return ""
    if s.endswith(("。", "，", "；")):
        return ""
    for name, rx in HEADING_PATTERNS:
        if rx.match(s):
            return name
    return ""


def headings(text: str) -> list[tuple[int, int, str, str]]:
    """提取标题：``[(段落下标, 字符偏移, 标题文本, 模式名)]``。"""
    out: list[tuple[int, int, str, str]] = []
    pos = 0
    for i, line in enumerate(split_paragraphs(text)):
        kind = heading_kind(line)
        if kind:
            out.append((i, pos, line.strip(), kind))
        pos += len(line) + 1
    return out


# --------------------------------------------------------------------------
# 表格行
# --------------------------------------------------------------------------

#: 表格行里的连续空白（≥2 个空格/制表符）就是列分隔符。
_TABLE_GAP_RE = re.compile(r"[ \t]{2,}")
#: 单元格里不该出现的标点。出现句末/句中标点说明这一段是句子而不是表格单元格。
_TABLE_PUNCT = "。！？；，、：（）《》“”‘’"

#: 单元格长度上限。表格单元格是短标签；超过这个长度的"字段"其实是句子。
TABLE_CELL_MAX_CHARS = 12


def is_table_row(line: str) -> bool:
    """判断一行是不是"靠空格分列的纯文本表格行"。

    **这个判据必须只有一份。** 纯文本稿件里的表格就是靠连续空格对齐的：

        变量  均值  标准差
        数字化转型  3.42  0.87

    把列分隔空格当成"多余空格"删掉，表格立刻塌成一串字，比不改更糟。而
    "删多余空格"这件检查散落在多个检查器里（文字卫生、格式整理），各写一份
    判据必然漂移——同一条表格行会被一个检查器保护、被另一个改坏。

    判据两条同时成立才判为表格行（**宁可不报，也不改坏表格**）：

    1. 行内有 **2 处及以上**连续空白——正常句子里出现多个双空格极罕见；
    2. 每个连续空白**两侧的字段都短**（≤ ``TABLE_CELL_MAX_CHARS``）且不含标点
       ——表格单元格是短标签，而"……的过程。  这说明……"是句子间隔。

    代价是漏报极少数"一句话里有三处双空格"的排版污染，收益是不会改坏任何一张表。
    这个取舍是明确的：漏报可以人工补，改坏表格会让人不敢再用这个工具。
    """
    if not line:
        return False
    gaps = list(_TABLE_GAP_RE.finditer(line))
    if len(gaps) < 2:
        return False
    prev_end = 0
    for g in gaps:
        field = line[prev_end : g.start()].strip()
        if not field or len(field) > TABLE_CELL_MAX_CHARS:
            return False
        if any(ch in field for ch in _TABLE_PUNCT):
            return False
        prev_end = g.end()
    tail = line[prev_end:].strip()
    if not tail or len(tail) > TABLE_CELL_MAX_CHARS:
        return False
    if any(ch in tail for ch in _TABLE_PUNCT):
        return False
    return True


#: 章节号（1.2.3）的层级深度
_NUM_HEADING = re.compile(r"^(\d+(?:\.\d+)*)")


def heading_depth(line: str) -> int:
    """``1.2.3`` → 3；``第3章`` / ``一、`` → 1；无法判断 → 0。

    中文论文两种编号体系并存，层级只能分别算：阿拉伯数字看点的个数，
    汉字编号（第一章、一、）按中文习惯一律算一级。把两者混在一起比较会
    造出大量假跳级。
    """
    s = (line or "").strip()
    m = _NUM_HEADING.match(s)
    if m:
        return m.group(1).count(".") + 1
    if re.match(r"^第\s*[0-9一二三四五六七八九十百]+\s*[章篇]", s):
        return 1
    if re.match(r"^[一二三四五六七八九十]+\s*[、.．]", s):
        return 1
    if re.match(r"^[（(]\s*[一二三四五六七八九十]+\s*[）)]", s):
        return 2
    return 0


# --------------------------------------------------------------------------
# 参考文献
# --------------------------------------------------------------------------

REF_SECTION_RE = re.compile(r"^\s*(参考文献|References|REFERENCES|Bibliography)\s*[:：]?\s*$")

#: 条目形态一：``[1] 作者. 标题[J]. 期刊, 2020.``
REF_ENTRY_BRACKET = re.compile(r"^\s*[\[【]\s*(\d{1,3})\s*[\]】]\s*(.+)$")
#: 条目形态二：``1. 作者. 标题[J]. 期刊, 2020.``（须有内容跟在后头，避免把"1."当条目）
REF_ENTRY_PLAIN = re.compile(r"^\s*(\d{1,3})\s*[.、．]\s*([\u4e00-\u9fffA-Za-z\u4e00-\u9fff].{4,})$")


def split_reference_section(text: str) -> tuple[int, int, list[tuple[int, int, int | None, str]]]:
    """切出参考文献区。

    返回 ``(区首偏移, 区尾偏移, [(段落下标, 段首偏移, 列表编号或 None, 条目全文)])``。

    为什么以"参考文献"标题为界而不是找文末：有的论文在参考文献之后还有致谢和
    附录，把它们算进参考文献会凭空造出几十条格式错误。
    """
    lines = split_paragraphs(text)
    offsets: list[int] = []
    pos = 0
    for line in lines:
        offsets.append(pos)
        pos += len(line) + 1

    start_idx = -1
    for i, line in enumerate(lines):
        if REF_SECTION_RE.match(line):
            start_idx = i
            break
    if start_idx < 0:
        return -1, -1, []

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        if re.match(r"^\s*(致谢|附录|Acknowledg|Appendix)", lines[j]):
            end_idx = j
            break

    entries: list[tuple[int, int, int | None, str]] = []
    for i in range(start_idx + 1, end_idx):
        raw = lines[i]
        if not raw.strip():
            continue
        m = REF_ENTRY_BRACKET.match(raw) or REF_ENTRY_PLAIN.match(raw)
        if m:
            entries.append((i, offsets[i], int(m.group(1)), raw.strip()))
        elif entries:
            # 条目折行：并入上一条，而不是当成新条目——否则一条被折成三行的
            # 参考文献会被判三次"缺年份"。
            pi, po, pn, ptxt = entries[-1]
            entries[-1] = (pi, po, pn, ptxt + " " + raw.strip())
    body_start = offsets[start_idx]
    body_end = offsets[end_idx] if end_idx < len(lines) else len(text)
    return body_start, body_end, entries


YEAR_RE = re.compile(r"(?<!\d)(1[89]\d{2}|20[0-4]\d)(?!\d)")
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s，。；、）)\]]+)")
_TITLE_STOP = re.compile(r"\[[A-Za-z/]{1,4}\]")
_PAREN = re.compile(r"[（(][^）)]{0,40}[）)]")


def ref_year(entry: str) -> str:
    """抽出版年份。取第一个"像年份"的 4 位数（18xx~204x）。"""
    m = YEAR_RE.search(entry or "")
    return m.group(1) if m else ""


def ref_doi(entry: str) -> str:
    m = DOI_RE.search(entry or "")
    return m.group(1).rstrip(".").lower() if m else ""


def ref_title(entry: str) -> str:
    """粗抽标题：优先取 ``[J]``/``[M]`` 之前那段，否则取第一个句点之后那段。

    这只是给"缺标题/重复检测"用的近似值，不追求还原 GB/T 7714 的字段切分。
    宁可抽得粗糙也不能抽错：抽出来的东西会被当作"证据"显示给作者。
    """
    s = _PAREN.sub(" ", entry or "").strip()
    stop = _TITLE_STOP.search(s)
    head = s[: stop.start()] if stop else s
    head = re.sub(r"^[\[【]?\s*\d{1,3}\s*[\]】.、．]?\s*", "", head).strip()
    # 作者段之后通常能碰到的第一个句点或等宽分隔，就是标题起点
    parts = re.split(r"[.．]\s*|\s{2,}", head, maxsplit=3)
    if len(parts) >= 2:
        cand = parts[1].strip()
        if len(cand) >= 4:
            return cand
    return head.strip()


def ref_authors(entry: str) -> str:
    """粗抽作者段：标题（[J] 之类）之前、去掉编号后最靠前的部分。"""
    s = re.sub(r"^[\[【]?\s*\d{1,3}\s*[\]】.、．]?\s*", "", (entry or "").strip())
    stop = _TITLE_STOP.search(s)
    head = s[: stop.start()] if stop else s
    m = re.match(r"^([^.．]{1,80})", head)
    return (m.group(1) if m else "").strip()


def normalize_title(title: str) -> str:
    """比较用归一化：去掉标点、空白、大小写，只留下实词。"""
    return re.sub(r"[^\w\u4e00-\u9fff]", "", (title or "").lower())


# --------------------------------------------------------------------------
# 定位锚点
# --------------------------------------------------------------------------

#: 唯一锚点向外扩张的窗口宽度序列。先短后长：锚点越短，修复算子越不容易
#: 因为"连带了不想改的内容"而改坏旁边的东西。
_ANCHOR_WINDOWS = (6, 10, 16, 24, 40, 80)


def anchor(text: str, snippet: str, near: int = 0) -> str:
    """为 ``snippet`` 造一个**在全文唯一**的定位串。

    为什么不直接用 snippet：
    * 重复段落、重复图表题这类问题天然会出现两次——直接拿它当 ``find``，
      ``replace_span`` 会因为"出现 2 次，定位不唯一"拒绝执行，修复静默失败。
    * 复验也依赖唯一性：锚点唯一，才能确定"问题还在原地还是已经改掉了"。

    策略：从 ``near`` 处的这一次出现出发，向左右各扩一点上下文；哪一档第一次
    做到全文唯一就用哪一档。实在无法唯一（整段重复且上下文也一样），返回空串，
    由调用方把 ``fixable`` 降为 False —— **宁可不修，也不改错地方**。
    """
    text = text or ""
    snippet = snippet or ""
    if not snippet:
        return ""
    if text.count(snippet) == 1:
        return snippet

    positions = [i for i in range(len(text)) if text.startswith(snippet, i)]
    if not positions:
        return ""
    start = min(positions, key=lambda p: abs(p - near))
    end = start + len(snippet)

    for w in _ANCHOR_WINDOWS:
        a, b = max(0, start - w), min(len(text), end + w)
        cand = text[a:b]
        if cand and text.count(cand) == 1:
            return cand
    return ""


def make_issue(
    *,
    kind: str,
    severity: Severity,
    message: str,
    text: str,
    paragraph: int = -1,
    char: int = -1,
    find: str = "",
    evidence: str = "",
    fixable: bool = False,
    fix_op: str = "",
    fix_hint: str = "",
    confidence: float = 1.0,
) -> Issue:
    """统一构造 Issue，把"位置三件套"的约定固化下来。

    ``location`` 同时带 ``paragraph`` / ``char`` / ``find``：
    前两个给人看（跳到第几段），``find`` 给机器用（复验与修复定位）。
    ``find`` 空着也允许——那说明这条问题无法安全机械修复，但依然值得报给作者。
    """
    loc: dict[str, Any] = {}
    if paragraph >= 0:
        loc["paragraph"] = paragraph
    if char >= 0:
        loc["char"] = char
    if find:
        loc["find"] = find
    return Issue(
        id="",
        kind=kind,
        severity=severity,
        message=message,
        location=loc,
        evidence=evidence,
        confidence=confidence,
        fixable=bool(fixable and fix_op),
        fix_op=fix_op if (fixable and fix_op) else "",
        fix_hint=fix_hint,
    )


def mask_nonbody(text: str) -> str:
    """把参考文献区替换成等长的占位符，供"只看正文"的检查器使用。

    为什么要等长替换而不是删除：所有正则匹配出来的偏移量仍然对得上原稿，
    检查器可以直接用这个偏移去原稿里取 ``evidence``。若直接删掉，后面每个
    offset 都要重算，出错的概率远大于收益。
    """
    s, e, _ = split_reference_section(text)
    if s < 0 or e <= s:
        return text
    return text[:s] + ("\u0000" * (e - s)) + text[e:]


# --------------------------------------------------------------------------
# 修复算子参数
# --------------------------------------------------------------------------


def keep_terms(ctx: Any) -> set[str]:
    """读取 ``ctx.board.project.meta["keep_terms"]``：作者声明必须保留的术语。

    设计理由：术语统一是把双刃剑——有的导师要求统一成"数据库"，有的课题组
    内部一律写"资料库"。这条规则只有作者知道，所以框架留出一个显式白名单，
    白名单里的词**绝不**被任何术语类检查器触碰。

    宽容处理各种脏数据（逗号分隔的字符串、None、非列表）：meta 是自由字段，
    因为 meta 格式不对就崩掉整个检查器是不可接受的。
    """
    meta = {}
    try:
        meta = ctx.board.project.meta or {}
    except Exception:  # noqa: BLE001 - 检查器永不因元数据缺失而失败
        return set()
    raw = meta.get("keep_terms")
    if not raw:
        return set()
    if isinstance(raw, str):
        items: Iterable[str] = re.split(r"[,，、;；\s]+", raw)
    elif isinstance(raw, (list, tuple, set)):
        items = [str(x) for x in raw]
    else:
        return set()
    return {s.strip() for s in items if s and s.strip()}
