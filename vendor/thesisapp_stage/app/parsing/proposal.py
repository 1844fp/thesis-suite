"""开题报告解析：把一份开题报告变成"论文写作的可执行输入"。

## 为什么这一层值得单独写

作者已经做过一遍开题。那份文档里写着：题目、研究问题、**拟定的章节目录**、
研究方法、案例对象、创新点声明、参考文献。若写作助手把这些丢掉、改用一个通用的
"绪论—综述—研究设计—实证—结论"模板去生成，产出的论文与作者已获通过的开题
**对不上**——评审第一个问题就是"你开题里写的第三章去哪了"。

所以这一层要做的不是"读一段文字"，而是**把开题的承诺提取成结构**，让后续生成
沿着作者自己的框架走。

## 确定性优先，抽不到就如实说

中文开题报告的栏目名高度固定（选题背景、国内外研究现状及述评、基本内容与结构框架、
拟采用的研究方法、重点难点、主要参考文献）。因此这里**用规则解析，不调用模型**：

* 规则解析的结果可以逐条回溯到原文行号，作者能核对"系统理解的第三章是不是我的第三章"；
* 模型解析则会给出一份"看起来对"的结构，错了也看不出来——而结构错了，
  后面几万字的生成全部白费。

抽不到的栏目**写入 ``warnings`` 并留空**，不用默认值填。一份如实标出"没找到研究
方法那一段"的报告，比一份字段齐全但一半是猜的更有用。

## 创新点：只记录，不生成

开题报告里的"拟创新之处"会被原样提取并保存在 ``innovations`` 里，作为**给作者看的
备忘录**。它绝不会进入生成环节——创新点是作者的学术主张（见
``app/agents/writer_common.py`` 的写作政策）。提取它是为了让作者在写结论时能对照，
不是为了替作者写。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "ProposalSection",
    "ProposalBrief",
    "parse_proposal",
    "parse_proposal_text",
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class ProposalSection:
    """开题报告"结构框架"里的一行标题。"""

    title: str
    level: int
    number: str = ""
    line: int = -1
    source: str = ""

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "level": self.level,
            "number": self.number,
            "line": self.line,
            "source": self.source,
        }


@dataclass
class ProposalBrief:
    """从开题报告里提取出的写作输入。"""

    title: str = ""
    author: str = ""
    case: str = ""
    """案例对象（从题目里的"以……为例"提取；提取不到就留空，不猜）。"""

    discipline: str = ""
    keywords: list[str] = field(default_factory=list)

    #: 结构框架解析结果：顶层章 → 其下的节/小节标题。
    #: **这是本模块最有价值的产出**：它让生成的论文与作者已通过的开题一致。
    chapters: list[dict[str, Any]] = field(default_factory=list)
    outline: list[ProposalSection] = field(default_factory=list)

    methods: list[str] = field(default_factory=list)
    #: 开题里声明的创新点。**只记录，不生成**（见模块 docstring）。
    innovations: list[str] = field(default_factory=list)

    background: str = ""
    significance: list[str] = field(default_factory=list)
    content_summary: str = ""
    focus: list[str] = field(default_factory=list)
    difficulties: list[str] = field(default_factory=list)
    feasibility: list[str] = field(default_factory=list)
    schedule: list[str] = field(default_factory=list)

    references_raw: str = ""
    reference_count: int = 0

    source_path: str = ""
    warnings: list[str] = field(default_factory=list)
    #: 命中/未命中的栏目，供界面与报告显示"这份开题读到了什么"。
    regions_found: list[str] = field(default_factory=list)
    regions_missing: list[str] = field(default_factory=list)

    # -- 派生 -------------------------------------------------------------
    @property
    def ok(self) -> bool:
        """能否用于写作规划：至少要有题目，或者有章节结构。"""
        return bool(self.title or self.chapters)

    def chapter_titles(self) -> list[str]:
        return [c["title"] for c in self.chapters if c.get("title")]

    def as_writing_brief(self) -> dict[str, Any]:
        """转成 ``writer:plan`` 读得懂的写作意图。

        ``sections`` 里每一章的 ``focus`` 由它的小节标题拼成：这样规划阶段
        **在提示词里就看到作者自己的章节细分**，而不是只知道一个章名。
        ``children`` 一并带上，供起草阶段切分写作单元用。
        """
        sections: list[dict[str, Any]] = []
        for c in self.chapters:
            kids = [k for k in (c.get("children") or []) if k]
            sections.append(
                {
                    "title": c["title"],
                    "focus": "；".join(kids) if kids else str(c.get("number") or ""),
                    "needs": "",
                    "children": kids,
                }
            )
        brief: dict[str, Any] = {
            "topic": self.title,
            "sections": sections,
            "keywords": list(self.keywords),
            "discipline": self.discipline,
            # 案例对象与研究方法是开题已经确定的东西，直接带给规划与检索，
            # 避免生成阶段又去"猜"这篇论文研究什么。
            "case": self.case,
            "methods": list(self.methods),
            "source": "开题报告",
        }
        if self.background:
            brief["background_digest"] = self.background[:600]
        if self.focus:
            brief["emphasis"] = "；".join(self.focus[:6])
        return brief

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "author": self.author,
            "case": self.case,
            "discipline": self.discipline,
            "keywords": list(self.keywords),
            "chapters": [dict(c) for c in self.chapters],
            "outline": [s.as_dict() for s in self.outline],
            "methods": list(self.methods),
            "innovations": list(self.innovations),
            "background": self.background,
            "significance": list(self.significance),
            "content_summary": self.content_summary,
            "focus": list(self.focus),
            "difficulties": list(self.difficulties),
            "feasibility": list(self.feasibility),
            "schedule": list(self.schedule),
            "reference_count": self.reference_count,
            "source_path": self.source_path,
            "warnings": list(self.warnings),
            "regions_found": list(self.regions_found),
            "regions_missing": list(self.regions_missing),
        }


# ---------------------------------------------------------------------------
# 栏目识别
# ---------------------------------------------------------------------------

#: 开题报告的栏目。``(键, 正则)``，正则匹配"整行基本就是这个栏目名"。
#:
#: 顺序 = 优先级。必须在同一条正则里兼顾"带编号"（``三、选题研究的重点、难点``）
#: 与"不带编号"（``选题拟采用的研究方法、手段``）两种写法——实测的真实开题里
#: 两种混用，只认一种会漏掉一半栏目。
_REGIONS: tuple[tuple[str, str], ...] = (
    # 先判最具体的：``选题背景`` 既是大栏目的一部分（第 17 行），也是子栏目（第 18 行）。
    ("选题背景", r"^[（(]?[一二三四五六七八九十]?[)）]?\s*选题背景\s*$"),
    ("背景与综述", r"选题背景\s*[、,，]\s*国内外相关研究"),
    ("研究现状", r"^(?:[（(]?[一二三四五六七八九十]?[)）]?\s*)?国内外相关研究(?:发展)?现状"),
    ("理论实践价值", r"选题的?理论意义\s*[、,，]?\s*实践价值"),
    ("基本内容与框架", r"选题研究的?基本内容\s*[、,，]?\s*结构框架"),
    ("结构框架", r"^[（(]?[一二三四五六七八九十]?[)）]?\s*结构框架\s*$"),
    ("基本内容", r"^[（(]?[一二三四五六七八九十]?[)）]?\s*基本内容\s*$"),
    ("创新之处", r"拟?创新之处"),
    ("可行性", r"选题研究的?可行性分析"),
    ("可行性分析", r"^[（(]?[一二三四五六七八九十]?[)）]?\s*可行性分析\s*$"),
    ("进度计划", r"^[（(]?[一二三四五六七八九十]?[)）]?\s*进度计划"),
    ("研究方法", r"选题拟?采用的研究?方法"),
    ("重点难点", r"选题研究的?重点\s*[、,，]\s*难点|研究的?重点(?:与|和)?难点"),
    ("参考文献", r"^(?:主要)?参考文献\s*[:：]?\s*$"),
    ("说明", r"^说\s*明$"),
    ("内容清单", r"开题报告内容包括"),
)
_REGION_RES = tuple((key, re.compile(pat)) for key, pat in _REGIONS)

#: 结构框架里的三层标题。
_CH_CHAPTER = re.compile(r"^第\s*([一二三四五六七八九十百\d]+)\s*章[\s、.．:：]*(.*)$")
_SEC = re.compile(r"^(\d{1,2})\s*[.．]\s*(\d{1,2})\s*[.．]?\s*(.*)$")
_SUB = re.compile(r"^(\d{1,2})\s*[.．]\s*(\d{1,2})\s*[.．]\s*(\d{1,2})\s*[.．]?\s*(.*)$")

#: 编号条目：``1.文献研究法`` / ``（1）xxx`` / ``一、xxx``
_ITEM = re.compile(
    r"^\s*(?:[（(]\s*(\d{1,2}|[一二三四五六七八九十]{1,3})\s*[)）]"
    r"|(\d{1,2})\s*[.、．)]"
    r"|([一二三四五六七八九十]{1,3})\s*[、.．])\s*(.+?)\s*$"
)

#: 题目行：``论文题目：xxx`` / ``题目: xxx`` / ``选题名称：xxx``
_TITLE_LABEL = re.compile(r"^\s*(?:论文题目|学位论文题目|题目|选题名称|课题名称)\s*[:：]\s*(.+?)\s*$")
#: 从题目里提案例对象：``……——以科大讯飞为例``
_CASE = re.compile(r"[—\-‑–]{1,2}\s*以\s*([^为，,。]{2,30}?)\s*为(?:例|案例)")
#: 关键词行
_KEYWORDS = re.compile(r"^\s*(?:关\s*键\s*词|关键词)\s*[:：]\s*(.+?)\s*$")
#: 参考文献条目：``[1] 作者. 题名[J]. 期刊, 2020.``
_REF_ITEM = re.compile(r"^\s*[\[［]\s*\d{1,3}\s*[\]］]")


#: Markdown 标题前缀。开题报告常常先写成 .md（或用 Typora/Obsidian 记的草稿）再转
#: 成 .docx，两种形态都要能读。不剥掉 ``#`` 的话，``## 选题背景`` 匹配不上
#: "整行就是选题背景"这类栏目规则，于是栏目识别率骤降。
_MD_HEAD = re.compile(r"^\s*#{1,6}\s*")


def _clean(text: str) -> str:
    t = _MD_HEAD.sub("", (text or "").replace("\r", ""))
    return re.sub(r"[ \t\u3000]+", " ", t).strip()


def _is_heading_noise(line: str) -> bool:
    """整行是说明性文字（"开题报告内容包括"那一段）时，不该被当作结构框架。"""
    return bool(re.match(r"^\d{1,2}\s*[.、]\s*(?:选题|主要参考文献|选题拟采用)", line or ""))


# ---------------------------------------------------------------------------
# 主解析
# ---------------------------------------------------------------------------


def parse_proposal_text(
    text: str,
    *,
    title_hint: str = "",
    author: str = "",
    source_path: str = "",
) -> ProposalBrief:
    """解析开题报告正文。

    ``title_hint`` 由调用方从文件属性或文件名给出，作为"文档里没有题目行"时的兜底。
    **兜底值会被标记**（进 ``warnings``），因为用错题目会让整篇论文跑偏。
    """
    brief = ProposalBrief(title="", author=author, source_path=source_path)
    lines = (_clean(ln) for ln in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    rows = list(lines)

    # ---- 1) 切栏目区间 ------------------------------------------------
    #
    # 做法是"**内容行继承当前栏目**"，而不是只给标题行打标记。这一点踩过：
    # 第一版只标记标题行，于是 `结构框架` 这个区间的正文是空的（区间从标题行
    # 之后开始、又立刻结束），解析结果一份空大纲——而报告里"结构框架"却被列为
    # **已找到**，看起来一切正常。这类"区域找到了但内容是空"的故障最难发现。
    region_of: list[str] = [""] * len(rows)
    heading_lines: set[int] = set()
    order: list[str] = []
    current = ""
    for i, line in enumerate(rows):
        if line and not _is_heading_noise(line):
            for key, rx in _REGION_RES:
                if rx.search(line):
                    current = key
                    heading_lines.add(i)
                    if key not in order:
                        order.append(key)
                    break
        region_of[i] = current
    brief.regions_found = list(order)

    def band_lines(key: str) -> list[tuple[int, str]]:
        """该栏目的内容行（不含标题行本身），保序。"""
        return [
            (i, rows[i])
            for i in range(len(rows))
            if region_of[i] == key and i not in heading_lines and rows[i].strip()
        ]

    def band_text(key: str) -> str:
        return "\n".join(ln for _, ln in band_lines(key)).strip()

    # ---- 2) 题目 -------------------------------------------------------
    for i, line in enumerate(rows[:40]):
        m = _TITLE_LABEL.match(line)
        if m:
            brief.title = m.group(1).strip()
            break
    if not brief.title:
        # 没有"题目："标签时取"说明"栏目之前的第一个足够长、不像栏目名的行。
        for i, line in enumerate(rows[:40]):
            if len(line) >= 6 and not region_of[i] and not re.match(r"^[（(]?\s*[一二三四五六七八九十\d]", line):
                if not _is_heading_noise(line):
                    brief.title = line
                    break
    if not brief.title and title_hint:
        brief.title = title_hint.strip()
        brief.warnings.append(
            f"开题报告里没有找到题目，已改用「{brief.title}」（来自文件属性/文件名）。"
            "若题目不对，请用 --title 指定。"
        )
    if brief.title:
        m = _CASE.search(brief.title)
        if m:
            brief.case = m.group(1).strip()

    # ---- 3) 关键词（没有就留空，不猜）----------------------------------
    for _, line in ((i, rows[i]) for i in range(len(rows))):
        m = _KEYWORDS.match(line)
        if m:
            brief.keywords = [k.strip() for k in re.split(r"[;；,，、\s]+", m.group(1)) if k.strip()]
            break

    # ---- 4) 结构框架 → 章节树（本模块的核心产出）-----------------------
    frame = band_lines("结构框架")
    if not frame:
        brief.regions_missing.append("结构框架")
    outline: list[ProposalSection] = []
    for line_no, raw in frame:
        m_ch = _CH_CHAPTER.match(raw)
        if m_ch:
            outline.append(ProposalSection(
                title=_clean(m_ch.group(2)) or f"第{m_ch.group(1)}章",
                level=1, number=m_ch.group(1), line=line_no, source=raw,
            ))
            continue
        m_sub = _SUB.match(raw)
        if m_sub:
            outline.append(ProposalSection(
                title=_clean(m_sub.group(4)) or raw,
                level=3, number=f"{m_sub.group(1)}.{m_sub.group(2)}.{m_sub.group(3)}",
                line=line_no, source=raw,
            ))
            continue
        m_sec = _SEC.match(raw)
        if m_sec:
            outline.append(ProposalSection(
                title=_clean(m_sec.group(3)) or raw,
                level=2, number=f"{m_sec.group(1)}.{m_sec.group(2)}",
                line=line_no, source=raw,
            ))
            continue
        # 无编号但看起来像标题的短行（如"5.1公司介绍"之外的手写章名）——
        # 只在已经进入章节区、且该行很短时才收，避免把正文句子当成标题。
        if outline and 2 <= len(raw) <= 30 and not raw.endswith(("。", "；", "，", "、")):
            outline.append(ProposalSection(title=raw, level=2, line=line_no, source=raw))
    brief.outline = outline

    chapters: list[dict[str, Any]] = []
    for sec in outline:
        if sec.level == 1:
            chapters.append(
                {"title": sec.title, "number": sec.number, "children": [], "sub_children": [], "line": sec.line}
            )
        elif chapters:
            if sec.level == 2:
                # 二级标题是**写作单元的天然粒度**：一份硕士论文的二级节通常
                # 对应 1500~2500 字，正好是一次生成能写好的量。三级标题太细，
                # 直接用会把一章切成二十个碎片。
                chapters[-1]["children"].append(sec.title)
            elif sec.level == 3:
                # 三级标题作为"这一节要覆盖什么"的细节保留，交给提示词当要点，
                # 不当作写作单元的切分依据。
                chapters[-1]["sub_children"].append(f"{sec.number} {sec.title}".strip())
    brief.chapters = chapters
    if outline and not chapters:
        brief.warnings.append("结构框架里识别到标题行，但没有识别出「第 N 章」一级标题。")

    # ---- 5) 研究方法 ---------------------------------------------------
    methods = _numbered_items(band_lines("研究方法"))
    brief.methods = methods
    if not methods:
        brief.regions_missing.append("研究方法")

    # ---- 6) 创新点（只记录）--------------------------------------------
    brief.innovations = _numbered_items(band_lines("创新之处"))
    if not brief.innovations:
        brief.regions_missing.append("创新之处")

    # ---- 7) 背景 / 意义 / 重点难点 / 可行性 / 进度 ---------------------
    bg = band_text("选题背景") or band_text("背景与综述")
    brief.background = _first_paragraph(bg)
    brief.significance = _numbered_items(band_lines("理论实践价值"))
    brief.content_summary = _first_paragraph(band_text("基本内容"))
    brief.focus = _numbered_items(band_lines("重点难点"))
    brief.difficulties = [m.group(0) for m in re.finditer(r"难点[一二三四五六七八九十\d]?[：:][^\n]{0,120}", band_text("重点难点"))]
    brief.feasibility = _numbered_items(band_lines("可行性分析"))
    brief.schedule = [ln for _, ln in band_lines("进度计划")][:20]

    # ---- 8) 参考文献 ---------------------------------------------------
    ref_lines = band_lines("参考文献")
    if ref_lines:
        brief.references_raw = "\n".join(ln for _, ln in ref_lines)
        brief.reference_count = sum(1 for ln in brief.references_raw.split("\n") if _REF_ITEM.match(ln))
        if brief.reference_count == 0:
            brief.reference_count = sum(1 for ln in brief.references_raw.split("\n") if ln.strip())
    else:
        brief.regions_missing.append("参考文献")
        brief.warnings.append(
            "开题报告里没有找到「主要参考文献」栏目。参考文献是外部核验的输入，"
            "缺了它就无法核对引用是否真实存在。"
        )

    # ---- 9) 汇总警告 ---------------------------------------------------
    if not brief.title:
        brief.warnings.append("没有识别出论文题目，请用 --title 指定后再生成。")
    if not brief.chapters:
        brief.warnings.append(
            "没有从「结构框架」里解析出章节树，将退回系统内置的通用章节结构。"
            "如果你的开题里写了目录，请检查它是否在「结构框架」栏目下、且章节写成"
            "「第一章 ……」「1.1 ……」的形式。"
        )
    if brief.innovations:
        brief.warnings.append(
            f"开题报告里声明了 {len(brief.innovations)} 条创新点，已原样保留供你对照。"
            "**创新点不会由系统生成或改写**——它是作者的学术主张。"
        )
    return brief


def _numbered_items(lines: Iterable[tuple[int, str]], *, max_items: int = 40) -> list[str]:
    """从栏目行里抽出条目。

    **两趟处理**，这一点是实测踩出来的：开题的"研究方法"栏目里，
    每个方法后面都跟着一整段说明（`1.文献研究法` 下面接三百字解释）。
    第一版对未编号的行一律原样收下，于是 ``methods`` 里混进了整段说明文字——
    方法列表本该是五个词，结果变成五段话，提示词里塞满无关内容。

    所以：**只要这一栏里有编号条目，就只收编号条目**；一个编号条目都没有时，
    才退回收短行（≤40 字），因为有些开题就是不编号。
    """
    rows = list(lines)
    numbered: list[str] = []
    plain: list[str] = []
    for _, raw in rows:
        m = _ITEM.match(raw)
        text = _clean(m.group(4)) if m else _clean(raw)
        if len(text) < 3:
            continue
        if m:
            if text not in numbered:
                numbered.append(text)
        elif len(text) <= 40 and text not in plain:
            plain.append(text)
    out = numbered or plain
    return out[:max_items]


def _first_paragraph(text: str, *, limit: int = 500) -> str:
    """取栏目里第一段真正的正文（跳过子标题行）。"""
    for chunk in re.split(r"\n\s*\n|\n", text or ""):
        c = _clean(chunk)
        if len(c) >= 40 and not _ITEM.match(c):
            return c[:limit]
    return ""


def parse_proposal(path: str, *, title_hint: str = "", author_hint: str = "") -> ProposalBrief:
    """读一个开题报告文件（.docx/.txt/.md）并解析成 :class:`ProposalBrief`。"""
    from .intake import read_manuscript  # 局部导入：避免包初始化顺序耦合

    man = read_manuscript(path)
    brief = parse_proposal_text(
        man.text,
        title_hint=title_hint or man.title,
        author=author_hint or man.author,
        source_path=str(path),
    )
    for w in man.warnings:
        brief.warnings.append(f"收稿提示：{w}")
    return brief
