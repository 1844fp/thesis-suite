"""参考文献条目抽取。

这是"能力⑥ 资料和数据验证"里**外部核验的输入端**。它存在的唯一理由是：
外部核验的结论只能和它抽出来的字段一样可靠。如果这里把「张三, 李四. 数字化转型
研究[J]. 管理世界, 2020」的题目抽成了「管理世界」或者把年份抽丢了，那么后面
Crossref 会返回 ``not_found``，而系统会告诉你"作者引用了不存在的文献"——
**一个抽取 bug 会被伪装成一条学术不端指控**。这是本模块所有设计取舍的出发点。

三条硬规则：

1. **抽不到就留空，绝不猜测。** 宁可让下游报"无法核验（缺少标题）"，
   也不能编一个看起来像样的标题去检索——那会返回一个真实但不相关的文献，
   把编造的引用洗成"已确认"。
2. **不替作者补全。** 条目缺年份，`year` 就是空字符串；缺 DOI，`doi` 就是空。
   补全是学术不端的另一种形式。
3. **保守切分。** 参考文献区里的换行、悬挂缩进、"1." 与 "[1]" 混排都会干扰
   切分。切错一条 = 两条都核验错，所以切分器宁愿少切也不乱切。

本模块**只依赖标准库**，不 import 框架、不联网：它是纯函数，可以单独单测，
也可以被任何其它模块复用。
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..adapters.research import CrossrefAdapter

__all__ = [
    "CJK_RE",
    "LOOKUP_MIN_TITLE",
    "RefEntry",
    "build_reference_text",
    "document_type",
    "extract_doi",
    "fingerprint",
    "find_reference_heading",
    "parse_entries",
    "split_reference_section",
    "to_gbt",
]


# ---------------------------------------------------------------------------
# 基础正则与常量
# ---------------------------------------------------------------------------

CJK_RE = re.compile(r"[\u4e00-\u9fff]")

#: 参考文献区标题的**单独成行**形态。刻意要求整行只有标题：目录里也会有
#: 「参考文献……12」这样一行，正文里也会出现「本文参考文献包括……」，它们都不是
#: 参考文献区的起点。误在这里会把正文当成文献表，抽出几十条垃圾条目。
_HEADING_LINE_RE = re.compile(
    r"^\s*(?P<pre>[\[【（(]?)\s*(?P<label>"
    r"主要参考文献|参考文献|引用文献|参考书目|"
    r"references|reference|bibliography|works\s+cited|literature\s+cited"
    r")\s*(?P<post>[\]】）)]?)\s*[:：.、]?\s*$",
    re.I,
)
#: 正文里的行内提及（「详见参考文献」「见 References」）。用于切分后复核：
#: 若标题行紧接这种句子，说明它其实在正文中间，不是区标题。
_HEADING_MENTION_BEFORE_RE = re.compile(r"(参见|详见|见|如|据)\s*$")
_HEADING_MENTION_AFTER_RE = re.compile(
    r"^\s*[（(]?\s*(\d{1,3}\s*[)）]|\d{1,3}\s*[-–—]|共\s*\d+\s*[篇条]|列出|如下|包括)"
)

#: 各类编号样式。写成"号内允许空格"是因为 Word 导出的纯文本常把 ``[ 1 ]``
#: 写成带空格的样子；不允许空格会整条漏切。
#:
#: 圆括号/全角括号/纯数字编号后面都用 ``(?=[ \t\u3000]*\S)`` 要求"同一行还有内容"。
#: 少了这个前瞻，一条被截断成
#: ``(1)\n张三. 题目[J].`` 的条目会因为 "1" 自己成行而被切出一个空条目。
_NUM_INNER = (
    r"(?:\[\s*[0-9０-９]{1,3}\s*\]|［\s*[0-9０-９]{1,3}\s*］"
    r"|\(\s*[0-9０-９]{1,3}\s*\)(?=[ \t\u3000]*\S)"
    r"|（\s*[0-9０-９]{1,3}\s*）(?=[ \t\u3000]*\S)"
    r"|[0-9０-９]{1,3}\s*[.、)）](?=[ \t\u3000]*\S))"
)
_AUTHOR_SPLIT_RE = re.compile(r"[,，;；、]|\s+and\s+|&", re.I)

#: GB/T 7714 文献类型标记。**故意只认方括号形态**（``[J]`` / ``［J］`` / ``[J/OL]``）：
#: 加圆括号会让 ``(M)`` 这类正文写法混进来，把条目误判成有类型标记。
_TYPE_MARK_RE = re.compile(r"[\[［]\s*(?P<code>[A-Za-z]{1,2}(?:\s*/\s*[A-Za-z]{2})?)\s*[\]］]")

#: DOI：``10.`` + 4~9 位注册号 + ``/`` + 后缀。
#:
#: 后缀用**贪婪**匹配加"左侧字母数字断言"，而不是"非贪婪 + 右侧断言"。
#: 两种写法看起来等价，实际差得很远：``10.1000/xyz123`` 在后一种写法下会被
#: 截成 ``10.1000/x``（非贪婪先试最短，而 ``(?![A-Za-z0-9])`` 在 ``x`` 后面
#: 因为还有 ``y`` 而不成立——但正则引擎已经记录了那个落点）。**半截 DOI 在
#: Crossref 上必定查不到，于是真实文献被判成编造**，这是最致命的一类误报。
#:
#: 右侧边界交给 :func:`_trim_doi` 处理（剥掉尾随标点）与字符集处理（不含
#: CJK，所以中文句号不会被吃进来）。
_DOI_RE = re.compile(r"(?<![A-Za-z0-9])10\.\d{4,9}/[A-Za-z0-9._;()/:<>\-]+")
#: ``10.`` 前面只有这些东西，才说明这是个**裸 DOI** 而不是正文里的编号片段
#: （``第10.1234/x`` 这种以数字结尾的中文串不该被当成 DOI）。
#: 必须包含 ``/``：``https://doi.org/10.1000/x`` 里 ``10.`` 前面正是斜杠。
_DOI_LEAD_OK = re.compile(r"(?:^|[\s(（\[［,，;；:：/]|doi|DOI)$")
#: 尾随标点：DOI 后面紧跟的句号/逗号/括号都不是 DOI 的一部分。
_DOI_TAIL_STRIP = ".,;:)]}）］」。，；：、 \t"
#: 行内 URL。抽字段前先摘掉，否则 ``https://doi.org/10.x/yyy`` 前面那截会串进期刊名。
_URL_RE = re.compile(r"https?://\S+", re.I)

#: 年份：只认 1900-2099，避免把页码、卷号当成出版年。
_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
#: 卷(期) 形态：``36(4)`` / ``12(C)``。
_VOL_ISSUE_RE = re.compile(r"^\s*(\d{1,4})\s*[（(]\s*([A-Za-z0-9\-–—]{1,6})\s*[)）]")
#: 页码：``45-56`` / ``45–56`` / ``1123``，允许 ``S1`` 这类增刊页码。
_PAGES_RE = re.compile(r"^\s*[:：]?\s*((?:[A-Za-z]?\d{1,6})(?:\s*[-–—]\s*[A-Za-z]?\d{1,6})?)")
#: 纯卷号（没有期号时）：``, 36, 45-56``。
_VOL_ONLY_RE = re.compile(r"^\s*(\d{1,4})\s*[,，]")

#: 期刊/出版社噪声词。抽出来的期刊名撞上这些词，说明切错了位置，宁可留空。
_CONTAINER_NOISE = re.compile(r"^(eds?\.?|et\s+al\.?|in|and|等|编|eds)$", re.I)
_PRESS_HINT_RE = re.compile(
    r"press|publisher|springer|wiley|elsevier|acm|ieee|出版社|学位论文|大学$|学院$|研究所$", re.I
)

#: 抽取质量门槛：短于这个长度的标题不足以拿去检索，也不足以定位。
TITLE_MIN = 4
#: 标题短于这个长度时不送去检索（外部适配器本身要求 8 字符，这里更保守一些，
#: 把"太短所以查不了"与"查了但不存在"分开报告）。
LOOKUP_MIN_TITLE = 6
#: 判定「这里是不是作者/题目分界」时，题目一侧的最小长度。
#:
#: **必须与 ``LOOKUP_MIN_TITLE`` 分开。** 两者回答的是不同问题：
#: 前者问"这个标题值不值得拿去数据库检索"，后者问"这个切分位置是不是真的"。
#: 共用一个阈值会造成真实的解析失败：中文题目很短，``王六. 计量经济学[M]``
#: 的题目只有 5 个字，用 6 去卡会把**完全正确的分界**否掉——作者抽不到，
#: 题目变成 ``王六. 计量经济学``，而带作者名的标题送去检索会显著掉分。
#: 4 个字足以排除 ``Smith J. 2015`` 这类把年份当题目的切分。
BOUNDARY_MIN_TITLE = 4
#: 标题"可信度"达到 high 的长度门槛。见 ``RefEntry.title_quality``：
#: 只有 high 的标题查不到时，才敢说"这条文献疑似编造"。
TITLE_TRUSTED_LEN = 12
#: 短于这个长度的片段不可能是完整文献条目（用于合并误切出来的碎片）。
_FRAGMENT_MIN = 20


def document_type(code: str) -> str:
    """把 ``[J]`` / ``[EB/OL]`` 这类标记翻译成中文类型名，未知时原样返回。"""
    table = {
        "J": "期刊论文",
        "M": "专著",
        "C": "会议论文",
        "D": "学位论文",
        "N": "报纸",
        "P": "专利",
        "S": "标准",
        "R": "报告",
        "Z": "其他",
        "A": "析出文献",
        "DB": "数据库",
        "CP": "计算机程序",
        "EB": "电子公告",
        "OL": "联机文献",
        "G": "汇编",
        "DB/OL": "数据库（联机）",
        "EB/OL": "电子公告（联机）",
        "J/OL": "期刊论文（联机）",
        "M/OL": "专著（联机）",
        "C/OL": "会议论文（联机）",
        "D/OL": "学位论文（联机）",
    }
    return table.get((code or "").upper().replace(" ", ""), code or "")


# ---------------------------------------------------------------------------
# 条目数据结构
# ---------------------------------------------------------------------------


@dataclass
class RefEntry:
    """一条参考文献条目。

    ``raw`` 是**权威副本**：其它字段全部是从它派生的视图。这样做是因为核验结论
    会被回溯审计——半年后有人问"凭什么说这条是编造的"，唯一能回答的证据是原文，
    不是我们抽出来的字段。断点续验的指纹也建立在 ``raw`` 上。
    """

    index: int | None = None
    raw: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    title: str = ""
    container: str = ""
    doi: str = ""
    volume_issue_pages: str = ""
    note: str = ""

    @property
    def type_code(self) -> str:
        """条目里的文献类型标记（``J`` / ``M`` / ``EB/OL``…），没有则为空串。"""
        m = _TYPE_MARK_RE.search(self.raw)
        return m.group("code").replace(" ", "").upper() if m else ""

    @property
    def doc_type(self) -> str:
        return document_type(self.type_code)

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.raw)

    @property
    def search_title(self) -> str:
        """真正送去外部检索的标题串。

        抽到了作者时，条目原文里作者是题目的一部分（``Vaswani A. Attention Is
        All You Need``）。检索时把作者补回去：作者是**强特征**，能显著提高命中率，
        而相似度按序列比计算，多几个词不会把分数拉低到阈值以下。
        """
        t = self.title.strip()
        if not t or not self.authors:
            return t
        return f"{', '.join(self.authors)}. {t}"

    @property
    def lookupable(self) -> bool:
        """能否拿去外部检索。DOI 或足够长的标题，有其一即可。"""
        return bool(self.doi) or len(self.search_title.strip()) >= LOOKUP_MIN_TITLE

    @property
    def title_quality(self) -> str:
        """标题可信度：``"high"`` / ``"low"`` / ``"unusable"``。

        **这个信号是"真实文献被误报成编造"的主要防线。**

        ``not_found`` 的语义是"两个数据库里都没有这个作品"。它成立的前提是
        **我们送去检索的标题确实是作者写的那个标题**。一旦抽取出了偏差（少了一截、
        粘上了刊名、把作者名当成了题目），这个前提就不成立，而结论会从"需要人工确认"
        悄悄变成"你的文献是假的"——这是本功能最严重的失败模式，因为它会直接摧毁
        作者对工具的信任。所以标题质量必须作为**独立信号**暴露出来，让核验层能
        把结论降级，而不是让一个抽取 bug 伪装成学术不端指控。

        判据（有意保守）：

        * ``unusable``：太短（< ``LOOKUP_MIN_TITLE``），或几乎没有实词（字母/汉字
          占比低于一半，基本是数字与标点拼成的碎片）。
        * ``low``：长度 8~11，或数字占比超过 1/5（说明可能混进了卷期页）。
        * ``high``：长度 ≥ 12 且字符构成正常——只有这一档查不到才升级为 BLOCKER。

        顺序有讲究：**先判数字占比，再判实词占比**。反过来的话
        ``Study 2020 12 45 56 pp`` 这种"混进了卷期页"的标题会先撞上"实词不足一半"
        被判成 ``unusable``，于是彻底不检索——而它其实是一个有实词的标题，
        应该降级检索并给作者一条"请人工核对"的提示，而不是静默跳过。
        """
        t = (self.title or "").strip()
        if len(t) < LOOKUP_MIN_TITLE:
            return "unusable"
        cjk = len(CJK_RE.findall(t))
        latin = sum(1 for c in t if c.isascii() and c.isalpha())
        digits = sum(1 for c in t if c.isdigit())
        substantive = cjk + latin
        # 一个实词都没有（纯数字与标点拼成的碎片）：根本没法检索。
        if substantive == 0:
            return "unusable"
        if digits > len(t) / 5:
            return "low"
        if substantive < len(t) / 2:
            return "unusable"
        if len(t) < TITLE_TRUSTED_LEN:
            return "low"
        return "high"

    @property
    def missing(self) -> list[str]:
        """哪些关键字段是空的。**用于如实标注，而不是补全。**"""
        out: list[str] = []
        if not self.authors:
            out.append("authors")
        if not self.year:
            out.append("year")
        if not self.title:
            out.append("title")
        if not self.container:
            out.append("container")
        if not self.doi:
            out.append("doi")
        return out

    def as_dict(self) -> dict[str, Any]:
        d = {
            "index": self.index,
            "raw": self.raw,
            "authors": list(self.authors),
            "year": self.year,
            "title": self.title,
            "container": self.container,
            "doi": self.doi,
            "volume_issue_pages": self.volume_issue_pages,
            "note": self.note,
            "type_code": self.type_code,
            "doc_type": self.doc_type,
            "fingerprint": self.fingerprint,
            "missing": self.missing,
            "title_quality": self.title_quality,
        }
        return d


def fingerprint(raw: str) -> str:
    """条目指纹：``raw`` 的 sha1 前 16 位。

    用 sha1 而不是"第几条"或"标题"，是因为断点续验必须能认出**同一条**文献：
    作者在两次运行之间删掉了前面一条，所有序号会平移，用序号做键会让缓存整体错位
    ——把 A 文献的核验结论套到 B 文献头上，这是比"重新查一遍"严重得多的错误。
    """
    return hashlib.sha1((raw or "").strip().encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 正文 / 参考文献区切分
# ---------------------------------------------------------------------------


def find_reference_heading(text: str) -> int | None:
    """定位参考文献区标题行的起始偏移，找不到返回 ``None``。

    **取最后一个匹配**而不是第一个。中文论文的目录（或"参考文献"这四字出现在
    前置部分）会先出现一次；真正的文献表在后面。取第一个会把大段正文当成文献表。
    """
    best: int | None = None
    offset = 0
    for raw_line in (text or "").splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        m = _HEADING_LINE_RE.match(line)
        if m:
            mentioned = False
            if not m.group("pre"):
                before = text[max(0, offset - 12) : offset]
                if _HEADING_MENTION_BEFORE_RE.search(before):
                    mentioned = True
            if not mentioned:
                best = offset
        offset += len(raw_line)
    return best


def split_reference_section(text: str) -> tuple[str, str]:
    """把稿件切成 ``(正文, 参考文献区)``。

    识别 ``参考文献`` / ``主要参考文献`` / ``REFERENCES`` / ``References`` /
    ``[参考文献]`` 等写法，并且只认**单独成行**的标题（见 ``_HEADING_LINE_RE``）。

    找不到标题时返回 ``(全文, "")``：这是**正常情况**（草稿可能还没写文献表），
    调用方据此跳过外部核验，而不是报错。返回空串而不是抛异常，也让"这一能力
    可以静默缺席"成为调用方的显式选择。
    """
    if not text:
        return "", ""
    pos = find_reference_heading(text)
    if pos is None:
        return text, ""
    line_end = text.find("\n", pos)
    if line_end < 0:
        # 标题行就是最后一行：没有文献表内容，但区标题确实存在。
        return text[:pos], ""
    return text[:pos], text[line_end + 1 :]


# ---------------------------------------------------------------------------
# 条目切分
# ---------------------------------------------------------------------------


def _num_candidates(text: str) -> list[tuple[int, int, int]]:
    """找出所有"行首编号"候选，返回 ``[(编号, 起始偏移, 编号后偏移)]``。

    为什么要限制在行首：文献表里的编号一定在行首（或段首），而**正文引用不是**
    ——``研究表明[3]，该方法…`` 里的 ``[3]`` 前面是汉字。如果允许任意位置的编号
    切分，一段正文就会被切成一堆假条目。悬挂缩进的续行也不会以编号起头。

    项目符号（``-`` / ``•``）只有**后面跟空白**时才算分隔符。这条限制是被一个
    真实案例逼出来的：页码范围 ``45-56`` 里的 ``-`` 后面紧跟数字，若把它当成
    行首分隔符，``56. doi:...`` 就会被切出一条编号 56 的幽灵条目，把一条真实的
    英文文献拦腰截断——核验结果里那条文献的 DOI 只剩半截。
    """
    out: list[tuple[int, int, int]] = []
    for m in re.finditer(_NUM_INNER, text):
        start = m.start()
        if start:
            prev = text[start - 1]
            if prev == "\n":
                pass
            elif prev in " \t\r\n\u3000\u00a0":
                # 悬挂缩进的续行以空格开头，必须再往前看：只有该行**行首**才算。
                line_start = text.rfind("\n", 0, start) + 1
                if text[line_start:start].strip():
                    continue
            elif prev in "•·" or (prev in "-—" and start + 1 < len(text) and text[start] in " \t\u3000"):
                line_start = text.rfind("\n", 0, start) + 1
                if text[line_start:start].strip(" \t\u3000•·-—"):
                    continue
            else:
                continue
        digits = re.search(r"[0-9０-９]{1,3}", m.group(0))
        if not digits:
            continue
        out.append((int(unicodedata.normalize("NFKC", digits.group(0))), start, m.end()))
    return out


def _looks_like_entry_body(text: str) -> bool:
    """一段文本是否像一条完整的文献条目（用于否决过度切分）。

    判据是"**有文献特征** + **不是太短**"，两者必须同时满足：

    * 特征：年份、方括号文献类型标记、或出版社/大学线索。
      只有 DOI / URL 不算——正文段落里出现一个链接也会满足，那会把正文当文献。
    * 长度下限（``_FRAGMENT_MIN``）用来挡住 ``56. doi:10.1000/x``、``2018.`` 这类
      真碎片。

    **长度门槛不能单独使用**，靠它 + 类型标记的早期版本丢掉过一整条合法文献：
    ``[2] 王五. 数据要素市场化配置研究[D]. 北京: 中国人民大学, 2021.`` 只有
    48 个字符，类型标记是 ``[D]``（学位论文常写成"地址: 学校, 年份"而**不带**
    方括号标记），于是被判成碎片并并进了上一条。这不是排版小瑕疵——它让两条
    文献的题目互相污染，核验结论随之全错。
    """
    if len(text.strip()) < _FRAGMENT_MIN:
        return False
    if _YEAR_RE.search(text) or _TYPE_MARK_RE.search(text):
        return True
    return bool(_PRESS_HINT_RE.search(text))


def _trim_marker(body: str) -> str:
    """去掉条目正文开头残留的编号标记（含列表中可能出现的第二个标记）。"""
    s = body.strip()
    s = re.sub(rf"^{_NUM_INNER}\s*", "", s)
    return re.sub(rf"^{_NUM_INNER}\s*", "", s).strip()


def _split_chunks(text: str) -> list[tuple[int | None, str]]:
    """按**全部**行首编号切分，返回 ``[(序号, 条目原文)]``。

    这里刻意不要求编号连续。曾经的做法是"只在候选里挑最长的连续递增段"，
    它在一个真实的混排文献表上直接崩掉：全角 ``［3］`` 不在候选里，于是
    ``[1] [2] ［3］ [4] [5]`` 断成 ``[1,2]`` 与 ``[4,5]`` 两段，选中后一段
    就**把前三条文献整段丢掉了**——核验只覆盖了后半张表，而且毫无提示。
    丢条目比切错条目危险得多：漏掉的编造文献永远不会被报出来。

    所以改成"全切 + 事后合并"：切分宁多勿少，再用 :func:`_merge_broken` 把
    误切出来的碎片并回上一条。方向反过来了——**错误的表现从"丢一条"变成
    "多切一条"，而后者能被合并逻辑兜住。**
    """
    cands = _num_candidates(text)
    if not cands:
        return []

    chunks: list[tuple[int | None, str]] = []
    head = text[: cands[0][1]].strip()
    if head:
        chunks.append((None, head))
    for i, (idx, start, end) in enumerate(cands):
        stop = cands[i + 1][1] if i + 1 < len(cands) else len(text)
        body = text[start:stop].strip()
        if body:
            chunks.append((idx, body))
    return chunks


def _merge_broken(chunks: list[tuple[int | None, str]]) -> list[tuple[int | None, str]]:
    """把"太短且没有任何年份/类型标记"的碎片并回上一条。

    这正是"全切"策略的必要配套：文献表里偶有一行内容恰好以 ``2018.`` 开头
    （跨行断句、或年份起头的句子），会被切成一个碎片。碎片本身无法核验
    （抽不出题目），留着只会污染核验结果，所以并回上一条。

    合并条件收得很紧：**短 + 無年份 + 無类型标记**，三条同时满足才合并。
    宽松一点就会出现"两条都没有年份的条目被粘成一条"，那等于漏掉一条文献。
    """
    merged: list[tuple[int | None, str]] = []
    for idx, body in chunks:
        body = body.strip()
        if not body:
            continue
        if merged and not _looks_like_entry_body(body):
            prev_idx, prev_body = merged[-1]
            merged[-1] = (prev_idx, f"{prev_body} {_trim_marker(body)}".strip())
            continue
        merged.append((idx, body))
    return merged


def _index_from_prefix(prefix: str) -> int | None:
    m = re.search(r"\d{1,3}", prefix or "")
    return int(m.group(0)) if m else None


# ---------------------------------------------------------------------------
# 字段抽取
# ---------------------------------------------------------------------------


def _trim_doi(raw: str) -> str:
    """剥掉 DOI 尾部的标点与括号，再用适配器的归一函数收尾。

    归一的实现只此一份（``CrossrefAdapter._clean_doi``），因为"同一个 DOI 在本地
    缓存里查得到、送去 Crossref 却 404"这种故障极难排查，而它唯一的成因就是
    两处归一规则不一致。
    """
    s = (raw or "").strip().strip(_DOI_TAIL_STRIP)
    # URL 尾部的括号配平：[10.1000/xyz] 里的 ] 不是 DOI 的一部分
    while s and s[-1] in ")]}" and s.count("(") < s.count(")"):
        s = s[:-1]
    return CrossrefAdapter._clean_doi(s)


def extract_doi(text: str) -> str:
    """从条目里抽 DOI，并用 ``CrossrefAdapter._clean_doi`` 归一。

    四种写法都要认：``doi:10.x/y``、``https://doi.org/10.x/y``、
    ``DOI：10.x/y``（全角冒号）、裸 ``10.x/y``。

    先做 NFKC 归一：全角冒号与全角数字会被折叠，所以 ``DOI：`` 与 ``10．1000``
    不需要单独的分支。裸 DOI 额外要求前面是空白/括号/逗号之类的东西，见
    ``_DOI_LEAD_OK``。
    """
    s = unicodedata.normalize("NFKC", text or "")
    if not s:
        return ""
    for m in _DOI_RE.finditer(s):
        start = m.start()
        if not _DOI_LEAD_OK.search(s[:start]):
            continue
        doi = _trim_doi(m.group(0))
        if re.match(r"^10\.\d{4,9}/\S+$", doi):
            return doi
    return ""


def _split_authors(segment: str) -> list[str]:
    """拆作者串。**保守**：拆不出像样的名字就返回空列表，不硬塞。

    姓名格式在真实文献表里极其混乱（``张三, 李四``、``Smith J, Doe A``、
    ``王五 等``、``李雷，韩梅梅，王强``）。这里只做两件事：按分隔符拆开、
    丢掉明显不是人名的片段（``等`` / ``et al`` / 年份 / 过长或含句末标点的串）。
    抽错的作者会写进参考文献表并被作者当成事实，所以宁可空着。
    """
    if not segment:
        return []
    out: list[str] = []
    for piece in _AUTHOR_SPLIT_RE.split(segment):
        name = (piece or "").strip(" .。·、")
        if not name or len(name) > 40:
            continue
        if re.fullmatch(r"(等|et\s*al\.?|others|and|&)", name, re.I):
            continue
        if _YEAR_RE.fullmatch(name):  # 年份不是人名
            continue
        if _PRESS_HINT_RE.search(name):  # 出版社/机构名不是人名
            continue
        if re.search(r"[。！？；]", name):
            continue
        out.append(name)
    return out


#: 单个中文姓名：2~4 个汉字（``王五`` / ``李雷`` / ``欧阳修`` / ``司马相如``）。
_CJK_NAME_RE = re.compile(r"[\u4e00-\u9fff]{2,4}")
#: 西文姓名片段：``Smith`` / ``Smith J`` / ``Smith J.`` / ``J. Smith`` / ``van der Berg``。
_LATIN_NAME_RE = re.compile(r"^(?:[A-Z][A-Za-z'’\-]+|[A-Z]\.?)(?:\s+[A-Z][A-Za-z'’\-]*\.?)*$")


def _looks_like_name(part: str) -> bool:
    """单个片段是否像一个人名。

    只认两种形态：2~4 个汉字，或``首字母大写的西文词``。刻意不接受
    ``数字化转型与企业绩效`` 这类长中文串、也不接受 ``Attention Is All You Need``
    ——它们不以"单个人名"的形态出现（含多个空格分隔的单词且首词较长）。
    """
    p = (part or "").strip(" .,、")
    if not p:
        return False
    if _CJK_NAME_RE.fullmatch(p):
        return True
    return bool(_LATIN_NAME_RE.match(p))


def _strip_author_noise(segment: str) -> str:
    """去掉作者段末尾的截断标记（``et al.`` / ``等人`` / ``等``）。

    必须在拆作者**之前**去掉：``Vaswani A, et al.`` 里的 ``.`` 会被
    ``_trim_marker`` 之外的逻辑保留，而 ``et al.`` 紧跟在逗号后面，会让
    ``Vaswani A, et al`` 被按逗号切成 ``["Vaswani", "A"]``——把一个作者拆成两个，
    同时名字还残缺。这个标记是**作者段的结束符**，先剥掉再拆才正确。
    """
    s = re.sub(r"[,，、]?\s*et\s+al\.?\s*$", "", segment, flags=re.I)
    s = re.sub(r"[,，、]?\s*等\s*(?:人)?\s*$", "", s)
    return s.strip(" .。;；,，")


def _looks_like_authors(segment: str) -> bool:
    """判断一段文本"像不像作者列表"。用于在英文条目里定位作者与题目的分界。

    命中其一即算：

    1. ``et al.`` / ``等`` —— **纯粹的作者截断标记，不可能出现在题目里**，
       是最可靠的信号（比 ``[A-Z].`` 还可靠：题目里也可能出现缩写点）。
    2. ``首字母缩写 + 点`` 形态（``Smith J.`` / ``J. Smith``）。
    3. 用分隔符拆开后**至少两个**片段都像人名（``张三, 李四``）。
    4. 整段本身就是**单个人名**且很短（``王五. 数据要素…``）。

    方向都是"宁可少认"：漏认作者只是少一个字段，**错认作者会把题目切掉一半**，
    而题目是外部核验唯一的检索入口。所以 ``Deep Learning. Nature`` 这种
    ``Deep Learning`` 不会被当成作者（它不以单个人名的形态出现）。
    """
    s = (segment or "").strip()
    if not s or len(s) > 120:
        return False
    if re.search(r"\bet\s+al\.?", s, re.I) or re.search(r"(?:^|[,，、\s])等(?:$|[,，、\s])", s):
        return True
    if re.search(r"\b[A-Z]\.", s):
        return True
    if ":" in s:
        # 有冒号的片段几乎一定是"题目: 副标题"，不是作者。
        return False
    parts = [p.strip() for p in _AUTHOR_SPLIT_RE.split(_strip_author_noise(s)) if p.strip()]
    if len(parts) >= 2 and all(_looks_like_name(p) for p in parts):
        return True
    return len(s) <= 12 and _looks_like_name(_strip_author_noise(s))


def _find_author_title_boundary(base: str) -> int:
    """在 ``base`` 里找"作者列表结束、题目开始"的那个句点位置，找不到返回 ``-1``。

    **必须从左往右找第一个能成立的边界，不能从右往左找最后一个。**
    这个方向问题是一个真实缺陷：``Vaswani A, et al. Attention is all you need.
    NeurIPS, 2017.`` 里 ``rfind(". ")`` 会把题目与刊名之间的句号当成分界，
    ``base[:dot]`` 于是变成 ``'Vaswani A, et al. Attention is all you need'``，
    ``_looks_like_authors()`` 对它返回 False，代码回退到"第一个句号之前"，
    题目退化成 ``'Vaswani A, et al'``、刊名退化成 ``'Vaswani A'``——
    **一正一错，而且错得看不出来**：条目照样排版得整整齐齐。

    返回 -1 时调用方保持原有回退逻辑（整个 head 当题目）。
    """
    pos = 0
    while True:
        i = base.find(". ", pos)
        if i < 0:
            return -1
        head = base[:i]
        tail = base[i + 2 :].strip(" .。;；,，")
        # 候选作者段：``head`` 本身，或它末尾还带着 "et al./等" 时的前一段。
        prev = head.rfind(". ")
        author_part = head[:prev] if prev > 0 else head
        # tail 的长度门槛是必要的：``Smith J. 2015`` 这种会被切出极短的假题目。
        # 用 ``BOUNDARY_MIN_TITLE`` 而不是 ``LOOKUP_MIN_TITLE``——中文题目常常只有
        # 4~5 个字（``计量经济学``），用检索门槛去卡分界会把正确切分否掉。
        if len(tail) >= BOUNDARY_MIN_TITLE and (
            _looks_like_authors(head) or _looks_like_authors(author_part)
        ):
            return i
        pos = i + 2


def _split_title_authors(segment: str) -> tuple[list[str], str, str]:
    """把 ``张三, 李四. 数字化转型与企业绩效[J]...`` 拆成 ``(作者, 题目, 题目之后)``。

    只在 :func:`_find_author_title_boundary` 认定的那个句点处切一刀。
    **不按逗号切**：中文题目里逗号很常见（``数字化转型，还是…``），
    按逗号切会把题目腰斩。

    返回三段而不是两段，是因为调用方还需要"题目之后"那一截去抽刊名/卷期页。
    早期版本让调用方自己用 ``base[:dot]`` 预先截断再把结果塞进来，于是
    ``_split_title_authors`` 收到的字符串里**已经不含题目**，分界自然找不到，
    题目退化成整个前半句、作者变空——一个看不见的接口错误，外部核验却会因此
    拿错误的标题去检索。这里把切分和取值绑在一处，就不会再出现这种错配。
    """
    dot = _find_author_title_boundary(segment)
    if dot < 0:
        return [], segment.strip(" .。;；,，"), ""
    head = segment[:dot].strip()
    after = segment[dot + 1 :].strip()
    title, container = _refine_markerless(after)
    if not head or not title:
        return [], segment.strip(" .。;；,，"), ""
    return _split_authors(_strip_author_noise(head)), title, container


def _refine_markerless(after: str) -> tuple[str, str]:
    """在**没有类型标记**的英文条目里，把 ``题目. 刊名, 年份`` 切成 ``(题目, 尾部)``。

    收到的 ``after`` 形如 ``Attention is all you need. NeurIPS, 2017.``：
    题目与刊名之间没有 ``[J]`` 之类的标记，只能靠"最后一个逗号就是刊名与年份的
    分界"来切。

    为什么要做这件事：不切的话题目会变成
    ``Attention is all you need. NeurIPS``，这个串送去 Crossref 检索出的相似度
    会掉到阈值以下——**一篇被正确引用的真实文献因此被判成"疑似编造"**。
    宁可多花这一步，也不能让这种情况发生。

    第二个返回值是"题目之后的原文"（含刊名与年份），交给
    :func:`_parse_container_and_vip` 继续抽刊名与卷期页。切分失败时返回
    ``(after, "")``——原样保留，不猜。中文条目不走这条路径（中文形态是
    ``题目[J]. 刊名, 年, 卷(期): 页``，由类型标记定位）。
    """
    s = (after or "").strip(" .。;；,，")
    if not s or CJK_RE.search(s):
        return s, ""

    # 分界是「题目与刊名之间的那个 ". "」，**不是逗号**。
    #
    # 实测形态：``after = "Attention is all you need. NeurIPS, 2017"``。
    # 这里的逗号在刊名与年份之间，拿它当分界会把刊名并进题目：
    # 切出来是 ``Attention is all you need. NeurIPS``，送去 Crossref 检索相似度
    # 掉到阈值以下，**一篇被正确引用的真实文献于是被判成"疑似编造"**。
    # 这类错误不报异常，只让核验结论反向。
    #
    # 做法：先用年份把尾部切掉，再在剩下部分的最后一个 ". " 处分开。
    # 刊名里带句号的情况极少，所以这个近似比"取最后一个逗号"可靠得多。
    ym = _YEAR_RE.search(s)
    core = s[: ym.start()].strip(" .。;；,，") if ym else s
    dot = core.rfind(". ")
    if dot > 0:
        title = core[:dot].strip(" .。;；,，")
        container = core[dot + 1 :].strip(" .。;；,，")
        # 题目至少要两个词、且不能是纯数字（说明切在了年份/卷号上）。
        if len(title.split()) >= 2 and not title.replace(" ", "").isdigit() and container:
            return title, container

    # 没有 ". " 可分：退回"年份之前的最后一个逗号"（``题目, 刊名, 年`` 形态）。
    comma = s.rfind(",")
    if ym:
        prev_comma = s[: ym.start()].rfind(",")
        if prev_comma > 0:
            comma = prev_comma
    if comma <= 0:
        return s, ""
    title, rest = s[:comma], s[comma + 1 :]
    # 题目至少要两个词、且不能是纯数字（纯数字说明我们切在了年份上）。
    if len(title.split()) < 2 or title.replace(" ", "").isdigit():
        return s, ""
    return title.strip(" .。;；,，"), rest.strip(" .。;；,，")


def _first_year(text: str) -> str:
    m = _YEAR_RE.search(text or "")
    return m.group(1) if m else ""


def _extract_tail(segment: str) -> str:
    """从"年份之后"的片段里抽 卷(期)页，返回 ``"36(4): 45-56"`` 这样的串。

    只在片段开头找。找不到就返回空——后面还可能有 DOI、URL、访问日期，
    它们不该被误当成卷号。
    """
    t = (segment or "").strip().lstrip(",，;；:：").strip()
    if not t:
        return ""
    for pat, fmt in ((_VOL_ISSUE_RE, "vip"), (_VOL_ONLY_RE, "vol")):
        m = pat.match(t)
        if not m:
            continue
        pm = _PAGES_RE.match(t[m.end() :].lstrip(" ,，:："))
        pages = pm.group(1).replace(" ", "") if pm else ""
        if fmt == "vip":
            return f"{m.group(1)}({m.group(2)})" + (f": {pages}" if pages else "")
        return m.group(1) + (f": {pages}" if pages else "")
    return ""


def _extract_container(segment: str) -> str:
    """从 ``期刊名, 2020, 36(4): 45-56`` 里取出期刊名。

    切法是"从年份往前退"：年份是一行里定位最可靠的锚点，它前面那一截
    （去掉卷期页和分隔符）就是期刊/会议名。这样比"取第一个逗号之前"稳，
    因为期刊名本身可能带逗号，英文刊名尤其常见。
    """
    s = unicodedata.normalize("NFKC", segment or "").strip().strip(" .。;；,，")
    if not s:
        return ""
    # 会议文献的 "In: NIPS" 里 "In:" 是连接词，不属于会议名。
    s = re.sub(r"^(?:in|c\.?f\.?|见)\s*[:：]\s*", "", s, flags=re.I)
    ym = _YEAR_RE.search(s)
    if ym:
        s = s[: ym.start()]
    s = re.sub(r"[,，;；:：\s]+$", "", s)
    s = re.sub(r"\s+\d{1,4}\s*[（(][^)）]{0,10}[)）]\s*$", "", s)
    s = re.sub(r"[,，]\s*\d{1,4}\s*$", "", s)
    # 页码残留（``45-56``）不是刊名。
    s = re.sub(r"[,，:：]?\s*\d{1,6}\s*[-–—]\s*\d{1,6}\s*$", "", s)
    s = s.strip(" .。;；,，:：")
    if not s or _CONTAINER_NOISE.match(s):
        return ""
    if len(s) > 80:  # 太长说明切到了题目/摘要上，宁可不给
        return ""
    return s


def _parse_container_and_vip(after: str) -> tuple[str, str]:
    """从"类型标记之后"的尾巴里分离出 ``(期刊/会议名, 卷期页)``。

    按逗号切段后**从前 2 段里找卷期页、从第 1 段取刊名**，这个顺序对中英文都成立：

    * 中文：``管理世界, 2020, 36(4): 45-56.``
    * 英文：``Journal of AI, 12(3), 45-56, 2019.``

    刻意只在前两段里找卷期页：第 3 段之后常见出版社、页码范围、"in press"
    之类的东西，在那里找一个 ``12(3)`` 形态很容易把地址邮编当成卷期。
    """
    segs = [s.strip() for s in re.split(r"[,，]", (after or "").strip(" .。;；,，")) if s.strip()]
    if not segs:
        return "", ""

    vip = ""
    container = _extract_container(segs[0])
    for i, seg in enumerate(segs[:3]):
        head = seg.split(".")[0]
        if i == 0:
            ym = _YEAR_RE.search(head)
            cand = head[ym.end() :] if ym else ""
            found = _extract_tail(cand)
        else:
            found = _extract_tail(head)
        if found:
            vip = found
            break

    if not container and len(segs) >= 2:
        # 第一段被年份或地址占掉时（``北京: 中国人民大学出版社, 2021``），
        # 刊名可能在第二段之前就已经抽空了，这里不再猜——留空比猜错好。
        container = _extract_container(segs[0].split(":")[0])
    return container, vip


def _extract_fields(entry: RefEntry) -> RefEntry:
    """在 ``entry.raw`` 上原地补齐字段（抽不到就留空）。"""
    raw = re.sub(r"\s+", " ", (entry.raw or "").strip())
    prefix = ""
    body = raw
    if (m := re.match(rf"^{_NUM_INNER}\s*", body)):
        prefix = m.group(0)
        body = body[m.end() :].strip()

    entry.index = _index_from_prefix(prefix) if prefix else entry.index
    entry.doi = extract_doi(body)

    # 摘掉 DOI 与 URL 那一截，后续字段抽取就不会被它们干扰。
    # ``[^A-Za-z0-9]`` 的否定前瞻保证不会从一长串字母数字中间断章取义地切一段。
    core = _URL_RE.sub(" ", f" {body} ")
    core = re.sub(r"(?:doi\s*[:：]\s*)?10\.\d{4,9}/[^\s，。；]*", " ", core, flags=re.I)
    core = re.sub(r"(?i)\bdoi\s*[:：]", " ", core).strip()

    type_m = _TYPE_MARK_RE.search(core)
    if type_m:
        segment = core[: type_m.start()].strip(" .。;；,，")
        after = core[type_m.end() :].strip(" .。;；,，")
        entry.note = type_m.group(0).replace("［", "[").replace("］", "]")
        head, title, _ = _split_title_authors(segment)
    else:
        # 没有类型标记的英文条目：整段交给 _split_title_authors 找"作者|题目"分界，
        # 它内部会用 _refine_markerless 把刊名从题目尾巴上切下来，第三段返回值
        # 就是"题目之后"的原文（刊名 + 年份），供下面继续抽刊名与卷期页。
        #
        # 这里**不做任何预先截断**。早期实现先把"逗号 + 年份"之前的部分截出来，
        # 再交给分界函数——于是分界函数收到的字符串里已经不含题目，分界必然失败，
        # 题目退化成前半句、作者变空。这类"接口错配"不会报错，只会让外部核验
        # 拿到错误的标题去检索。
        head, title, rest = _split_title_authors(core)
        after = rest if head else core.strip(" .。;；,，")

    entry.year = _first_year(core)
    entry.authors = head
    entry.title = title
    title = entry.title or ""
    if title.endswith(".") and not CJK_RE.search(title):
        title = title[:-1]
    if len(title) > 300:  # 明显切到了整段正文，不当作题目
        title = title[:300]
    entry.title = title.strip(" .。")

    # --- 期刊 / 会议名与卷期页 -------------------------------------------
    if after:
        entry.container, entry.volume_issue_pages = _parse_container_and_vip(after)

    if entry.note:
        entry.note = f"{entry.note}（{entry.doc_type}）"
    return entry


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def parse_entries(reference_text: str) -> list[RefEntry]:
    """把参考文献区切成条目并抽取字段。

    切分策略（见 ``_num_candidates`` / ``_best_run``）：
    只用**行首**的编号做切分点，并在所有候选里挑最长的连续递增段。
    这样既能吃下 ``[1]`` / ``1.`` / ``［1］`` / ``(1)`` / ``1、`` 的混排，
    又不会把正文里的 ``[3]`` 当成条目起点。

    没有编号（整段无序号文献表）时退回"按空行分段"；再不行就把整段当成一条。
    返回的列表可能为空——调用方必须处理"抽不出来"这种情况，而不是假设一定有。
    """
    text = (reference_text or "").strip()
    if not text:
        return []
    text = re.sub(r"\f", "\n", text)  # 分页符当成换行，避免跨页条目被粘连

    run = _split_chunks(text)
    if run:
        items: Iterator[tuple[int | None, str]] = iter(_merge_broken(run))
    else:
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
        if len(blocks) > 1:
            items = iter((None, b) for b in blocks)
        else:
            items = iter([(None, text)])

    entries: list[RefEntry] = []
    for idx, body in items:
        if not body.strip():
            continue
        entries.append(_extract_fields(RefEntry(index=idx, raw=body.strip())))
    return entries


def to_gbt(entry: RefEntry, *, number: int | None = None) -> str:
    """把一条条目按 GB/T 7714 顺序重排。

    **不补全任何字段。** 抽到什么就排什么，缺的字段就不出现在结果里。
    例如条目缺年份，输出里就没有年份——而不是拿"2020"顶上。理由很直接：
    参考文献表是要进论文的，凭空的年份就是伪造数据。

    标点按 GB/T 7714 的常规形态收口：作者后用句点、题名后接类型标记、
    ``刊名, 年, 卷(期): 页`` 这一段用逗号串联（**不给年份后面加句点**，
    否则会出现 ``管理世界, 2020. 36(4): 45-56.`` 这种把卷期页切成独立句子的
    写法）。条目原文的写法差异（全半角、多空格、``．`` 与 ``.``）在这里被统一。

    识别失败（作者和题目都没抽到）时**原样返回 raw**——一条排版不规范的文献，
    也好过一条被排版规则改坏的文献。
    """
    num = number if number is not None else entry.index
    prefix = f"[{num}] " if num else ""
    if not entry.authors and not entry.title:
        return prefix + entry.raw.strip()

    segs: list[str] = []
    if entry.authors:
        segs.append(", ".join(entry.authors) + ".")
    if entry.title:
        marker = entry.type_code
        segs.append(f"{entry.title}[{marker}]." if marker else f"{entry.title}.")

    # 刊名 + 年 + 卷期页：一个逗号串，缺项自动跳过，不留空逗号。
    last: list[str] = []
    if entry.container:
        last.append(entry.container)
    if entry.year:
        last.append(entry.year)
    if entry.volume_issue_pages:
        last.append(entry.volume_issue_pages)
    if last:
        segs.append(", ".join(last) + ".")
    if entry.doi:
        segs.append(f"DOI: {entry.doi}.")

    out = re.sub(r"\s+", " ", " ".join(s for s in segs if s))
    # 标点与它前面的词之间不留空格；重复的分隔标点收敛成一个。
    out = re.sub(r"\s+([,;.])", r"\1", out)
    out = re.sub(r"([,;])(?:\s*\1)+", r"\1", out)
    # 收口：先去掉"悬空逗号 + 句点"，再把**全部**尾随标点剥掉，最后统一补一个句点。
    # 必须一次剥干净——只剥一个的话 ``45-56.`` 会变成 ``45-56..``。
    out = re.sub(r",\s*\.", ".", out)
    out = re.sub(r"[,;:.．。、]+\s*$", "", out)
    return (prefix + (out + "." if out else "")).strip()


def build_reference_text(entries: list[RefEntry]) -> str:
    """生成整个参考文献区的文本：编号**重排为连续**，顺序不变。

    为什么要重排编号：作者改文献表时几乎一定会留下 ``[1] [2] [4]`` 这样的断号，
    评审看到会直接质疑严谨性。重排只动编号，不动条目顺序，也不动内容。
    """
    lines = [to_gbt(e, number=i) for i, e in enumerate(entries, start=1)]
    return "参考文献\n\n" + "\n".join(lines) + "\n" if lines else ""
