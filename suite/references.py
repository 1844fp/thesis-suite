"""选题驱动的参考文献推荐：多源检索 → 合并去重 → 分级打分 → 排序取 10~20 篇。

为什么单独一层，而不是改 websearch.py
------------------------------------
``websearch`` 回答的是"**这个论点有没有依据**"，它只收 ``usable``（有题名 + ≥80 字
摘要）的记录，因为写作阶段模型看不到内容就只能猜——那是编造的入口。

参考文献推荐回答的是另一个问题："**这个选题该读哪些文献**"。两件事的取舍不同：

* 一篇被引 3000 次但摘要缺失的经典，恰恰是开题报告最该列的；写作阶段该丢，这里该留。
* 参考文献要的是**书目信息完整**（作者、期刊、年卷期、DOI），而不是"能引用的原文片段"。
* 推荐结果是给人**看**的清单，所以排序必须可解释——每条都要说清"凭什么是它"。

所以这里复用 ``websearch`` 的四个源适配器（不重复实现网络层），但走自己的合并、
分级与排序逻辑。**不改 websearch 的任何行为**，写作链路的闸门保持原样。

"文献等级 / 引用量 / 下载量"这三个排序依据，本机到底能拿到什么
-----------------------------------------------------------
这三项里只有一项能在没有账号的前提下真实拿到，剩下两项的**如实处理**如下。
不诚实标注是本模块最需要避免的事——一个把估计值伪装成实测值的排序，
比没有排序更糟。

* **引用量 — 真实**。OpenAlex ``cited_by_count`` / Crossref ``is-referenced-by-count``
  / Semantic Scholar ``citationCount`` 都是公开字段。注意它是**跨库统计**，
  与知网自己的"被引"数字不完全一致，所以界面上写的是"被引（OpenAlex）"。
* **文献等级 — 可核验的映射，不是实测**。南大 CSSCI、北大核心的**目录**没有公开 API，
  本模块的做法是：把期刊名与本机内置的一份核心目录映射表比对（``TIER_DIRECTORY``），
  命中才给等级，命中不了就写「未收录/未知」——**绝不猜**。基础档位另有两条
  不依赖目录的硬证据：是否被 SCI/SSCI/AHCI/ESCI 收录、是否中文期刊。
  映射表可被运维替换（``SUITE_TIER_DIRECTORY`` 指向一个 JSON 文件）。
* **下载量 — 拿不到，明说**。知网/万方的下载量只在它们的页面上，没有公开接口。
  OpenAlex 有 ``counts_by_year``（每年新增被引），**不是**下载量；把它改名成
  "下载量"就是造假。所以界面显示的字段叫 ``downloads``，值一律是 ``None``，
  并在 ``limits`` 里写明"下载量需到知网/万方查阅"。要真下载量只能靠
  ``cnki-helper`` 油猴脚本在**你自己已登录的浏览器**里抓——那是另一条链路，
  不属于本站（本站是服务端，拿不到访客的知网登录态）。

排序怎么算
----------
``SCORE = 0.40·等级分 + 0.22·被引分 + 0.18·相关分 + 0.12·时效分 + 0.08·开放获取分``

权重是**可解释的工程默认值**，不是实验结论，所以在 ``describe_weights()`` 里
原样透出，前端把它显示给用户——让用户能自己判断"这个排序凭什么"。
相关分只在对候选做过模型精排时才有值；没有 Key 时它整列缺失，权重按剩余项归一，
而不是让所有条目一起扣掉 0.18（那会把"没精排"伪装成"都不相关"）。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import websearch

# ---------------------------------------------------------------------------
# 一、文献等级目录
# ---------------------------------------------------------------------------
#: 内置的核心期刊目录映射：``{等级: (条目, ...)}``。
#:
#: 条目可以带 ISSN，写法是 ``"刊名|ISSN"``（可以带多个，用 ``/`` 分隔）。
#: **为什么 ISSN 是可选的，而不是必须的**——这一条是实测结论，不是偷懒：
#:
#: 用 OpenAlex 查了 40 本中文核心刊，绝大多数**根本没有 ISSN 字段**
#: （《管理世界》《会计研究》《金融研究》《中国农村经济》《心理学报》
#: 《中国职业技术教育》… 全是 ``issn_l=None``）。原因是这些刊的题录由 CNKI
#: 侧提供，而 CNKI 不向 OpenAlex 提交 ISSN。反过来，那些"高产的仿冒刊"
#: （《经济学》2630-4759、《教育研究》2630-4686、《科研管理》2661-3751）
#: **都有 ISSN**。所以"没有 ISSN"在这份数据里不是可疑信号，
#: "刊名与核心刊一致、但 ISSN 对不上"才是。
#:
#: 于是匹配分两档，并**如实写进判定依据**：
#:
#: * **ISSN 一致** → 高置信度（``tier_basis`` 会写明"ISSN 一致"）。
#: * **ISSN 未知** → 中置信度（写"ISSN 未知，建议核实"）。
#: * **ISSN 明确不一致** → **不给等级**，降为兜底档并加一条可信度提示。
#:   这是本模块对"同名仿冒刊"唯一有效的防线：刊名可以一样，ISSN 不行。
#:
#: 中文刊名走**等值**匹配（归一后比较）：中文刊名短、区分度低，
#: 《经济学》与《经济学动态》《经济学(季刊)》共享前缀，子串匹配必然误判。
#: 拉丁字母刊名走子串匹配，且片段长度 ≥ 8：英文刊名长、写法多变，
#: 而短片段如 "sci" 会命中 "Conscience"。
TIER_DIRECTORY: dict[str, tuple[str, ...]] = {
    "CSSCI": (
        "中国社会科学", "经济研究", "管理世界", "社会学研究", "教育研究",
        "法学研究", "中国农村经济", "会计研究", "金融研究", "南开管理评论",
        "管理科学学报", "公共管理学报", "中国行政管理", "新闻与传播研究",
        "心理学报", "哲学研究", "历史研究", "文学评论", "外语教学与研究",
        "中国语文", "政治学研究", "世界经济", "统计研究", "中国工业经济",
        "科研管理|1004-115X", "经济学动态", "经济学(季刊)", "中国软科学|1005-0566",
        "journal of chinese", "china economic review",
    ),
    "北大核心": (
        "职业技术教育", "中国职业技术教育", "教育与职业", "职教论坛",
        "中国电化教育", "现代教育技术", "开放教育研究|1007-2179",
        "中国高教研究", "高等工程教育研究", "软科学", "科技管理研究",
        "中国科技论坛", "情报杂志", "图书情报工作", "商业经济研究",
        "财会月刊", "中国注册会计师",
    ),
    "CSCD": (
        "计算机学报", "软件学报|1000-9825", "自动化学报|0254-4156",
        "电子学报", "通信学报", "计算机研究与发展|1000-1239",
        "模式识别与人工智能", "中国科学", "物理学报", "数学学报",
    ),
    "北大科技核心": ("科学技术与工程", "计算机应用研究", "计算机工程与应用", "工程科学"),
}

#: 允许运维用外部 JSON 覆盖整张目录表。**替换而不是合并**：合并会让两份口径
#: 混在一起，出现"同一本期刊命中两个等级"这种没法解释的结果。
DIRECTORY_ENV = "SUITE_TIER_DIRECTORY"

#: 等级 → 分值（0~1）。顺序即优先级，前端按这个顺序分档展示。
TIER_SCORE: dict[str, float] = {
    "SCI/SSCI": 1.00,
    "CSSCI": 0.92,
    "CSCD": 0.86,
    "北大核心": 0.84,
    "北大科技核心": 0.76,
    "ESCI": 0.72,
    "中文期刊": 0.52,
    "国际期刊": 0.50,
    "预印本": 0.34,
    "仓储/未正式发表": 0.22,
    "会议/论文集": 0.58,
    "未收录/未知": 0.30,
}

#: 索引里出现这些词说明该刊被相应数据库收录（OpenAlex 的 ``source.type``/``is_in_doaj``
#: 与 Crossref 的 ``container-title`` 都不直接给收录信息，所以只能靠刊名与索引词判断）。
_INDEX_HINTS: tuple[tuple[str, str], ...] = (
    ("ssci", "SCI/SSCI"),
    ("sci", "SCI/SSCI"),
    ("ahci", "SCI/SSCI"),
    ("esci", "ESCI"),
)


def _load_directory() -> dict[str, tuple[str, ...]]:
    raw = os.environ.get(DIRECTORY_ENV, "").strip()
    if not raw:
        return TIER_DIRECTORY
    path = Path(raw)
    if not path.is_file():
        return TIER_DIRECTORY
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 目录坏了不能把整个推荐功能带停
        return TIER_DIRECTORY
    out: dict[str, tuple[str, ...]] = {}
    for tier, names in (data or {}).items():
        if isinstance(names, list):
            out[str(tier)] = tuple(str(n) for n in names)
    return out or TIER_DIRECTORY


# ---------------------------------------------------------------------------
# 二、记录结构
# ---------------------------------------------------------------------------
@dataclass
class RefRecord:
    """一条**合并后**的参考文献候选。"""

    title: str = ""
    authors: str = ""
    year: str = ""
    venue: str = ""
    doi: str = ""
    url: str = ""
    abstract: str = ""
    citations: int | None = None
    #: 下载量。**本机拿不到真实值**，恒为 None；保留字段是为了前端不必做特判，
    #: 也是为了将来接上能给出下载量的源时不用改结构。
    downloads: int | None = None
    #: 该记录的来源（可能多个，合并自多源）。
    sources: list[str] = field(default_factory=list)
    #: 一次检索里被哪些查询词命中——用来说明"为什么认为它相关"。
    matched_queries: list[str] = field(default_factory=list)
    #: 该条目的语言判定（zh / en / unknown）。
    language: str = "unknown"
    #: 期刊 ISSN（任一来源给出即可）。它既是"正规连续出版物"的交叉证据，
    #: 也让用户能拿它去知网/图书馆精确核实。
    issn: str = ""
    #: 载体类型（journal / repository / conference …）。
    #: **必须记住它**：OpenAlex 把机构知识库、Zenodo 这类仓储也算作"来源"，
    #: 它们的 ``display_name`` 看起来像个期刊名（实测出现过
    #: "Montana State University ScholarWorks"、"Zenodo (CERN…)"），
    #: 不区分就会把一篇学位论文/预印本写成"[J] 期刊论文"。
    venue_type: str = ""
    #: 是否开放获取（能直接下到全文，对开题很实用）。
    open_access: bool = False
    oa_url: str = ""
    #: 文献等级与判定依据。``tier_basis`` 必须写清"凭什么给这个等级"。
    tier: str = "未收录/未知"
    tier_basis: str = ""
    #: 排序分数与各分项（全部 0~1）。
    score: float = 0.0
    parts: dict = field(default_factory=dict)
    #: 模型精排给出的相关分与理由（没精排时为 None）。
    relevance: float | None = None
    reason: str = ""
    #: 为什么入选（可读的一句话，由 ``_explain`` 生成）。
    why: str = ""
    #: 可信度提示（**提示不是过滤**）：例如"该刊无 ISSN 且零被引，请核实"。
    warnings: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return _title_key(self.title)

    def citation(self) -> str:
        """一行参考文献串（GB/T 7714 风味，缺字段不补）。"""
        bits = []
        if self.authors:
            bits.append(self.authors)
        if self.title:
            bits.append(self.title)
        tail = []
        if self.venue:
            tail.append(self.venue)
        if self.year:
            tail.append(self.year)
        if self.doi:
            tail.append(f"DOI: {self.doi}")
        head = "．".join(bits)
        return head + (f"[{'，'.join(tail)}]" if tail else "")

    def as_reference(self) -> dict:
        """给前端的扁平结构。字段名与 gather 的证据保持一致，便于共用渲染。"""
        return {
            "rank": 0,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "source": self.venue or "（来源未标注）",
            "venue": self.venue,
            "doi": self.doi,
            "url": self.url,
            "abstract": self.abstract,
            "citations": self.citations,
            "downloads": self.downloads,
            "tier": self.tier,
            "tier_basis": self.tier_basis,
            "issn": self.issn,
            "venue_type": self.venue_type,
            "language": self.language,
            "language_label": LANGUAGE_LABEL.get(self.language, "未知"),
            "open_access": self.open_access,
            "oa_url": self.oa_url,
            "score": round(self.score, 3),
            "parts": {k: round(float(v), 3) for k, v in (self.parts or {}).items()},
            "relevance": self.relevance,
            "reason": self.reason,
            "why": self.why,
            "sources": list(self.sources),
            "citation": self.citation(),
            "warnings": list(self.warnings),
            "origin": "refrec",
        }

    def as_kb_meta(self) -> dict:
        """入库时写进知识库的元数据（``kb.add`` 的 ``meta`` 字段）。"""
        return {
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "venue": self.venue,
            "doi": self.doi,
            "url": self.url,
            "tier": self.tier,
            "citations": self.citations,
            "language": self.language,
            "issn": self.issn,
            "venue_type": self.venue_type,
            "open_access": self.open_access,
            "citation": self.citation(),
            "sources": list(self.sources),
        }


def _title_key(title: str) -> str:
    """跨源去重指纹。与 websearch 的口径一致，但中文标点也要归一。"""
    text = (title or "").lower()
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    return text[:64]


LANGUAGE_LABEL = {"zh": "国内（中文）", "en": "国外（英文）", "unknown": "语言未知"}


def _looks_chinese(text: str) -> bool:
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text or ""))
    return cjk >= 4


def _classify_language(title: str, abstract: str, venue: str) -> str:
    """判语言。**只看题名与期刊名**，不看摘要。

    理由：中文期刊的摘要常带英文版，英文期刊的题名里也可能有中文作者名的拼音；
    题名 + 期刊名是语言最可靠的指示，用摘要会把二者混起来。
    """
    probe = f"{title} {venue}"
    if _looks_chinese(probe):
        return "zh"
    if re.search(r"[A-Za-z]{4,}", title or ""):
        return "en"
    return "unknown"


# ---------------------------------------------------------------------------
# 三、分级
# ---------------------------------------------------------------------------
def _norm_issn(value: str) -> str:
    """ISSN 归一：只留数字与 X，中间那道连字符去掉。"""
    return re.sub(r"[^0-9xX]", "", value or "").upper()


def _norm_venue(name: str) -> str:
    """期刊名归一：只留字母数字与汉字，其余（书名号、空格、卷期、标点）全去掉。

    归一之后才能做**等值**比较——中文期刊名用子串匹配会大量误判：
    实测检索结果里出现过《经济学》（ISSN 2630-4759，非核心），
    它与内置目录里的《经济学动态》《经济学(季刊)》共享前缀，
    子串匹配会把一本非核心刊判成 CSSCI，它的 0.40 等级分会把它送上清单第一名。
    等值比较能根除这一类错误。
    """
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", (name or "")).lower()


def _split_entry(entry: str) -> tuple[str, tuple[str, ...]]:
    """把目录条目拆成 ``(刊名, (ISSN, ...))``。写法：``"管理世界"`` 或 ``"科研管理|1004-115X"``。"""
    if "|" not in entry:
        return entry.strip(), ()
    name, _, issns = entry.partition("|")
    return name.strip(), tuple(_norm_issn(x) for x in re.split(r"[/、]", issns) if x.strip())


def _directory_match(venue: str, issn: str) -> tuple[str, str, str]:
    """在目录里找这本刊。返回 ``(等级, 判定依据, 状态)``，状态为
    ``hit`` / ``issn_mismatch`` / ``""``（没命中）。

    ISSN 三态的处理见 ``TIER_DIRECTORY`` 的注释：一致=高置信，未知=中置信，
    明确不一致=**不给等级**（同名仿冒刊的唯一有效防线）。
    """
    norm = _norm_venue(venue)
    if not norm:
        return "", "", ""
    mine = _norm_issn(issn)
    directory = _load_directory()

    for tier, entries in directory.items():
        for entry in entries:
            name, issns = _split_entry(entry)
            fnorm = _norm_venue(name)
            if not fnorm:
                continue
            has_cjk = bool(re.search(r"[\u4e00-\u9fff]", name))
            if has_cjk:
                matched = norm == fnorm
            else:
                matched = len(fnorm) >= 8 and fnorm in norm
            if not matched:
                continue

            if issns and mine:
                if mine in issns:
                    return tier, (f"刊名与《{tier}》目录条目「{name}」一致，"
                                  f"且 ISSN {issn} 与目录记录一致"), "hit"
                return "", (f"刊名与《{tier}》目录条目「{name}」一致，"
                           f"但 ISSN「{issn}」与目录记录（{'/'.join(issns)}）不符"), "issn_mismatch"
            if issns and not mine:
                return tier, (f"刊名与《{tier}》目录条目「{name}」一致；"
                              f"检索源未提供 ISSN，**无法用 ISSN 交叉核实同名仿冒刊**，"
                              f"请到知网确认"), "hit"
            return tier, f"刊名与《{tier}》目录条目「{name}」一致（该条目无 ISSN 记录）", "hit"
    return "", "", ""


def classify_tier(*, venue: str, language: str, source_kind: str = "",
                  issn: str = "", venue_type: str = "") -> tuple[str, str]:
    """给出文献等级与**判定依据**。

    判定顺序是刻意安排的：先看外部目录（最权威），再看来源类型（预印本一定不是
    核心期刊），最后才是"中文期刊 / 国际期刊"这个兜底档。兜底档必须如实标注成
    「未收录/未知」的近义档，不能把它说成核心——那正是最容易被误读的地方。
    匹配口径与 ISSN 交叉核实的完整说明见 ``TIER_DIRECTORY``。
    """
    name = (venue or "").strip()
    low = name.lower()

    tier, basis, state = _directory_match(name, issn)
    if state == "hit":
        return tier, basis
    mismatch_basis = basis if state == "issn_mismatch" else ""

    for hint, hint_tier in _INDEX_HINTS:
        if re.search(rf"\b{re.escape(hint)}\b", low):
            return hint_tier, f"期刊名含收录标识「{hint.upper()}」"

    if source_kind == "arxiv" or "arxiv" in low or "预印本" in name or "preprint" in low:
        return "预印本", "arXiv/预印本，未经同行评审"
    # 仓储来源（机构知识库、Zenodo、学位论文库…）必须**在"期刊"之前**判掉：
    # 它们的 display_name 长得像刊名，先判期刊就会把学位论文写成期刊论文。
    from .websearch import normalize_venue_type

    vtype = normalize_venue_type(venue_type)
    if vtype == "repository":
        return "仓储/未正式发表", f"来源是机构仓储或预印本平台（{name or '未具名'}），非期刊论文"
    if vtype in {"conference", "book series", "book"}:
        return "会议/论文集", f"载体类型为 {vtype}（{name or '未具名'}）"
    if not name:
        return "未收录/未知", "无期刊名，无法判等级"

    # 兜底档。**把"有没有 ISSN"写进依据**：这是用户判断"这本刊靠不靠谱"时
    # 最有用的一个线索，而它来自数据本身（OpenAlex 的 source.issn_l / Crossref 的 ISSN），
    # 不是我们的猜测。缺 ISSN 时如实说"源未给"，不说"该刊无 ISSN"。
    issn_bit = ""
    if _norm_issn(issn):
        issn_bit = f"，ISSN {issn.strip()}"
    elif source_kind in {"openalex", "crossref"}:
        issn_bit = "，检索源未提供 ISSN"
    if venue_type and venue_type not in {"journal", "conference", "book series"}:
        issn_bit += f"，载体类型 {venue_type}"

    head = mismatch_basis
    if not head:
        if language == "zh":
            head = "中文期刊，未命中本机核心目录 → 等级未知"
        elif language == "en":
            head = "国际期刊，未命中本机核心目录 → 等级未知"
        else:
            head = "无法判定收录等级"
    if mismatch_basis:
        return "未收录/未知", head + issn_bit
    if language == "zh":
        return "中文期刊", head + issn_bit
    if language == "en":
        return "国际期刊", head + issn_bit
    return "未收录/未知", head + issn_bit


# ---------------------------------------------------------------------------
# 四、各分项打分（全部归一到 0~1）
# ---------------------------------------------------------------------------
#: 检索词命中数按这个上限归一。命中 3 个以上视为"完全贴合选题"。
QUERY_HIT_CAP = 3

#: 被引数按对数归一到 10000 次（人文社科基本到不了，理工科顶刊才会超过）。
CITATION_CAP = 10000

#: 时效窗口：近 ``RECENCY_WINDOW`` 年内线性给分，早于窗口下限给 0。
RECENCY_WINDOW = 10
RECENCY_FLOOR = 25


def _score_tier(tier: str) -> float:
    return TIER_SCORE.get(tier, 0.30)


def _score_citations(citations: int | None) -> float:
    if citations is None or citations <= 0:
        return 0.0
    # log 归一：被引 100 与 200 的差别远小于 0 与 100 的差别，
    # 线性归一会让一篇高被引综述压死整个列表。
    return min(1.0, math.log1p(citations) / math.log1p(CITATION_CAP))


def _score_recency(year: str) -> float:
    try:
        y = int(str(year)[:4])
    except (TypeError, ValueError):
        return 0.0
    now = time.localtime().tm_year
    age = now - y
    if age < 0:
        return 0.5          # 未来年份多半是"在线优先出版"，给中位分
    if age <= RECENCY_WINDOW:
        return 1.0 - (age / RECENCY_WINDOW) * 0.7      # 当年 1.0 → 第 10 年 0.3
    if age <= RECENCY_FLOOR:
        return 0.2
    return 0.05             # 经典老文献通过被引分体现价值，时效分只留一点点


def _score_query_hits(hits: int) -> float:
    return min(1.0, hits / QUERY_HIT_CAP)


def _score_oa(open_access: bool) -> float:
    return 1.0 if open_access else 0.0


WEIGHTS = {
    "tier": 0.40,
    "citations": 0.22,
    "relevance": 0.18,
    "recency": 0.12,
    "open_access": 0.08,
}


def describe_weights() -> dict:
    """把排序权重如实透出，让用户能自己判断排序凭什么。"""
    return {
        "weights": dict(WEIGHTS),
        "caps": {"citation_cap": CITATION_CAP, "query_hit_cap": QUERY_HIT_CAP,
                 "recency_window": RECENCY_WINDOW},
        "tier_scores": dict(TIER_SCORE),
        "note": (
            "权重是工程默认值，不是实验结论。相关分只有在对候选做过模型精排时才存在；"
            "没有 Key 时该项整列缺失，其余权重按比例归一，而不是全体扣掉这 0.18。"
        ),
    }


def _compose(record: RefRecord, query_hits: int) -> None:
    """算总分。**缺项按剩余权重归一**，理由见模块文档。"""
    parts: dict[str, float] = {
        "tier": _score_tier(record.tier),
        "citations": _score_citations(record.citations),
        "recency": _score_recency(record.year),
        "open_access": _score_oa(record.open_access),
        "query_hits": _score_query_hits(query_hits),
    }
    active = {k: w for k, w in WEIGHTS.items() if k != "relevance"}
    if record.relevance is not None:
        parts["relevance"] = max(0.0, min(1.0, float(record.relevance)))
        active["relevance"] = WEIGHTS["relevance"]

    total_weight = sum(active.values()) or 1.0
    score = sum(parts[k] * w for k, w in active.items()) / total_weight
    record.parts = parts
    record.score = score
    # **提示在这里重算一遍**，而不是只在 _from_web 里算一次。
    # 原因是合并会在 `_merge` 里改动 tier/tier_basis/issn：只在建记录时算一次的话，
    # 后面的合并可能把一条"ISSN 与核心目录不符"的提示悄悄弄丢——而那条提示恰恰是
    # 识破同名仿冒刊的唯一线索。让它在最终态重算，就不会与当前判定脱节。
    record.warnings = _credibility_warnings(
        record, issn=record.issn, venue_type=record.venue_type)
    record.why = _explain(record, query_hits)


def _explain(record: RefRecord, query_hits: int) -> str:
    """一句话说清"凭什么排在前面"。这段文字直接显示给用户。"""
    bits: list[str] = []
    if record.tier not in {"未收录/未知"}:
        bits.append(f"等级：{record.tier}")
    if record.citations:
        bits.append(f"被引 {record.citations} 次")
    if query_hits:
        bits.append(f"命中 {query_hits} 个检索词")
    if record.year:
        bits.append(f"{record.year} 年")
    if record.open_access:
        bits.append("开放获取（可直接下载全文）")
    if not bits:
        bits.append("按来源相关度入选")
    return "；".join(bits)


# ---------------------------------------------------------------------------
# 五、多源检索与合并
# ---------------------------------------------------------------------------
def _retrieval_queries(topic: str, keywords: list[str], *, english: str = "") -> list[str]:
    """构造检索词。

    **中英各检一遍**是有意的：只检中文会漏掉所有国际期刊，只检英文会漏掉
    国内文献（OpenAlex 里的中文期刊题录是英文的，中文检索词命中率很低）。
    中文用选题原文，英文用调用方给的译法（没有就退回选题原文——
    宁可少检一源，也不要机器翻译出一道谁也没法复核的检索式）。
    """
    topic = (topic or "").strip()
    kws = [k.strip() for k in (keywords or []) if str(k).strip()]
    out: list[str] = []
    if topic:
        out.append(topic)
    if kws:
        out.append(" ".join(kws[:4]))
        out.append(" ".join(kws))
    if english:
        out.append(english.strip())
    if topic and not english:
        out.append(topic)          # 英文源也会试一次原文，命中不了就算了
    seen: set[str] = set()
    return [q for q in out if q and not (q.lower() in seen or seen.add(q.lower()))]


#: 判断一个检索异常是不是"这个源现在用不了"。
#:
#: 只有这几类才值得把源拉黑（本次不再重试）：限流、连不上、超时、DNS。
#: 其余异常（解析出错、字段形状变了）**不拉黑**——那类问题往往与具体检索词有关，
#: 换个词就可能正常，一拉黑反而把还能用的源也废掉了。
_UNAVAILABLE_MARKERS = (
    "429", "too many requests", "rate limit", "quota", "exceeded",
    "timeout", "timed out", "connection", "connect", "unreachable",
    "name resolution", "getaddrinfo", "ssl", "502", "503", "504",
)


def _is_unavailable(message: str) -> bool:
    low = (message or "").lower()
    return any(m in low for m in _UNAVAILABLE_MARKERS)


def collect(
    *,
    topic: str,
    keywords: list[str] | None = None,
    english: str = "",
    per_source: int = 12,
    max_candidates: int = 90,
) -> tuple[list[RefRecord], list[dict]]:
    """去公开文献库取候选并合并成 ``RefRecord``。

    返回 ``(候选, 各源状态)``。某个源限流或超时**不影响其它源**，失败信息原样
    进入状态列表，由前端如实展示——"这个源没查到"和"没有这篇文献"是两件事。
    """
    queries = _retrieval_queries(topic, keywords or [], english=english)
    merged: dict[str, RefRecord] = {}
    status: list[dict] = []

    #: 已经确定不可用的源，本次不再重试。
    #:
    #: **这是实测逼出来的**：OpenAlex 无 Key 的免费额度是按 IP 共享的
    #: （官方提示"$0.10/天，与同一网络的其他人共享"），一旦耗尽它每次都返回 429。
    #: 早先的实现会对每一个检索词分别重试一遍，于是 3 个检索词 × 3 个源里，
    #: 有 3 次都是在明知会失败的情况下白等超时——一次推荐从 15 秒拖到 2 分钟以上，
    #: 而结果**一条都没多**。同一个源在一次推荐里失败一次就够了。
    dead: set[str] = set()

    #: ``(源, 检索词) -> 记录``。同一个源在**完全相同**的检索词上不必问第二遍。
    #:
    #: 实测收益：中文选题的 ``_retrieval_queries`` 会产出"选题原文"与"关键词拼串"
    #: 两个检索词，而它们对 Crossref 常常给出**同样的 20 条**——两次请求、一份结果。
    #: 这不是小浪费：Crossref 一次 2 秒、三个源就是好几秒，而用户等的是整份清单。
    cache: dict[tuple[str, str], list] = {}

    for query in queries:
        for base in websearch.SOURCES:
            if base.name in dead:
                continue
            key = (base.name, query.strip().lower())
            if key in cache:
                records = cache[key]
                used = sum(1 for raw in records if _from_web(raw) is not None)
                for raw in records:
                    rec = _from_web(raw)
                    if rec is not None:
                        _merge(merged, rec, query)
                status.append({"source": base.name, "query": query, "ok": True,
                               "found": len(records), "used": used, "seconds": 0.0,
                               "cached": True})
                if len(merged) >= max_candidates:
                    break
                continue
            try:
                adapter = base()
            except ValueError as exc:
                status.append({"source": base.name, "query": query, "ok": False,
                               "error": str(exc)})
                dead.add(base.name)
                continue
            started = time.time()
            try:
                records = adapter.search(query, limit=per_source)
                cache[key] = list(records)
            except Exception as exc:  # noqa: BLE001 - 单源失败不能拖垮整次推荐
                message = f"{type(exc).__name__}: {exc}"[:200]
                status.append({
                    "source": base.name, "query": query, "ok": False,
                    "error": message,
                    "seconds": round(time.time() - started, 1),
                })
                # 只有"这个源现在用不了"（限流、连不上、超时）才把它拉黑，
                # **不能一见异常就拉黑**：arXiv 对中文选题会正常返回 0 条，
                # 那要是一次解析异常，拉黑它就会让后面几个检索词连试都不试，
                # 而它本来可能对英文检索词有结果。判据按"是不是可用性故障"来分。
                if _is_unavailable(message):
                    dead.add(base.name)
                if "429" in message or "Too Many Requests" in message:
                    status[-1]["error"] = ("该源限流（HTTP 429）：OpenAlex 无 Key 时"
                                           "免费额度按 IP 共享，耗尽后当天不再可用。"
                                           "本次推荐已改用其余来源。")
                    status[-1]["unavailable"] = True
                continue

            used = 0
            for raw in records:
                rec = _from_web(raw)
                if rec is None:
                    continue
                _merge(merged, rec, query)
                used += 1
            status.append({"source": base.name, "query": query, "ok": True,
                           "found": len(records), "used": used,
                           "seconds": round(time.time() - started, 1)})
            if len(merged) >= max_candidates:
                break
        if len(merged) >= max_candidates:
            break

    return list(merged.values()), status


def _from_web(raw: "websearch.WebRecord") -> RefRecord | None:
    """把源适配器的记录转成候选。

    **这里比 websearch 宽松**：没有摘要也收，但会记下 ``abstract=""``，
    前端据此显示"无摘要"。理由见模块文档——被引很高的经典常常恰恰没有摘要。
    唯一的硬门槛是题名，没有题名就无从谈起是哪篇文献。
    """
    title = (raw.title or "").strip()
    if len(title) < 8:
        return None
    language = _classify_language(title, raw.abstract, raw.venue)
    issn = str(getattr(raw, "issn", "") or "")
    venue_type = str(getattr(raw, "venue_type", "") or "")
    tier, basis = classify_tier(venue=raw.venue, language=language,
                                source_kind=raw.source, issn=issn, venue_type=venue_type)
    record = RefRecord(
        title=title,
        authors=(raw.authors or "").strip(),
        year=str(raw.year or "").strip(),
        venue=(raw.venue or "").strip(),
        doi=(raw.doi or "").strip(),
        url=(raw.url or "").strip(),
        abstract=(raw.abstract or "").strip(),
        citations=raw.citations,
        sources=[raw.source],
        language=language,
        issn=issn,
        venue_type=venue_type,
        open_access=bool(getattr(raw, "open_access", False)),
        oa_url=str(getattr(raw, "oa_url", "") or ""),
        tier=tier,
        tier_basis=basis,
    )
    record.warnings = _credibility_warnings(record, issn=issn, venue_type=venue_type)
    return record


def _credibility_warnings(rec: RefRecord, *, issn: str, venue_type: str) -> list[str]:
    """给"看起来不太靠谱"的条目加提示。

    **这是提示，不是过滤。** 判据全部来自数据本身，不做学科特异的黑名单
    （"某几本刊是水刊"那种表会过期，而一过期就变成误伤）。两条判据：

    1. **ISSN 与核心目录记录冲突**（``classify_tier`` 的 ``issn_mismatch`` 状态）。
       这是唯一能识破"同名仿冒刊"的信号——刊名可以一模一样，ISSN 不行。
       实测遇到过检索结果里出现《经济学》（2630-4759）而非核心的
       《经济学动态》《经济学(季刊)》，仅靠刊名无法区分。
    2. **命中核心目录但检索源没给 ISSN**。这一条**不是"该刊有问题"**，
       而是"本机无法交叉核实"——CNKI 侧的刊绝大多数在 OpenAlex 里就没有 ISSN
       （《管理世界》《会计研究》都是），所以这条提示的措辞必须中性，
       不能写成"可疑期刊"。
    """
    warn: list[str] = []
    basis = rec.tier_basis or ""
    if "ISSN" in basis and "不符" in basis:
        warn.append(
            "⚠ 刊名与本机核心目录中的某本刊一致，但 ISSN 对不上 —— "
            "公开库里存在同名但不同刊的情况（常见于仿冒刊），"
            "**引用前请到知网按刊名 + ISSN 核实**。本条已按「未收录/未知」计分。"
        )
    elif "无法用 ISSN 交叉核实同名仿冒刊" in basis:
        warn.append(
            "本机核心目录里有同名期刊条目，但检索源没有给出 ISSN，"
            "无法交叉核实是不是同一本刊；请到知网确认后再引用。"
        )
    if venue_type == "repository":
        warn.append("来源是机构仓储/预印本平台，**未经同行评审**，引用时按学位论文或"
                    "预印本标注（GB/T 导出已用 [D]），不要写成期刊论文。")
    elif venue_type and venue_type not in {"journal", "conference", "book series", ""}:
        warn.append(f"载体类型是「{venue_type}」，不是期刊论文，引用时注意类型标识。")
    return warn


def _merge(bucket: dict[str, RefRecord], rec: RefRecord, query: str) -> None:
    """按题名指纹合并同一篇文献的多源记录。

    合并规则是"**逐字段取更完整的**"，而不是"保留第一条"：
    同一篇文献在 OpenAlex 有摘要、在 Crossref 有卷期、在 arXiv 有全文链接，
    取第一条会把另外两条的信息丢掉。引用量取**最大值**（不同库统计口径不同，
    取最大是保守做法：不因为某个库漏计就把一篇高被引文献压下去）。
    """
    key = rec.key
    if not key:
        return
    if query and query not in rec.matched_queries:
        rec.matched_queries.append(query)
    hit = bucket.get(key)
    if hit is None:
        bucket[key] = rec
        return

    for field_name in ("title", "authors", "venue", "doi", "url", "abstract", "year"):
        mine = str(getattr(hit, field_name) or "")
        theirs = str(getattr(rec, field_name) or "")
        if len(theirs) > len(mine):
            setattr(hit, field_name, theirs)
    if (rec.citations or 0) > (hit.citations or 0):
        hit.citations = rec.citations
    if rec.open_access and not hit.open_access:
        hit.open_access = True
        hit.oa_url = rec.oa_url
    for src in rec.sources:
        if src not in hit.sources:
            hit.sources.append(src)
    for q in rec.matched_queries:
        if q not in hit.matched_queries:
            hit.matched_queries.append(q)
    # 等级重判一次：合并后期刊名可能变得完整（例如 arXiv 记录合并了 Crossref 的刊名）。
    tier, basis = classify_tier(venue=hit.venue, language=hit.language,
                                source_kind=hit.sources[0] if hit.sources else "")
    if TIER_SCORE.get(tier, 0) > TIER_SCORE.get(hit.tier, 0):
        hit.tier, hit.tier_basis = tier, basis
    # 有一条来源能给出 ISSN，就说明这确实是正规连续出版物——用更完整的那个覆盖，
    # 并**把已有的可信度提示清空重算**。清空是对的：ISSN 冲突的提示在旧 ISSN 下成立，
    # 换了 ISSN 以后必须重新判。但这也意味着提示会在这一步短暂丢失，
    # 所以真正的兜底不在这里，而在 ``_compose`` —— 它每次都按当前 tier_basis 重算提示。
    if rec.issn and not hit.issn:
        hit.issn = rec.issn
        hit.venue_type = rec.venue_type or hit.venue_type
        tier, basis = classify_tier(venue=hit.venue, language=hit.language,
                                    source_kind=hit.sources[0] if hit.sources else "",
                                    issn=hit.issn, venue_type=hit.venue_type)
        if TIER_SCORE.get(tier, 0) >= TIER_SCORE.get(hit.tier, 0):
            hit.tier, hit.tier_basis = tier, basis
        hit.warnings = _credibility_warnings(hit, issn=hit.issn, venue_type=hit.venue_type)


# ---------------------------------------------------------------------------
# 六、配比与排序
# ---------------------------------------------------------------------------
@dataclass
class Quota:
    """最终清单的构成要求。**默认口径来自本任务的要求：10~20 篇、国内外都要有、有时间跨度。**"""

    total: int = 15
    min_cn: int = 4
    min_en: int = 4
    min_span_years: int = 8
    min_recent: int = 4          # 近 5 年至少几篇


def _pick(cands: list[RefRecord], quota: Quota) -> tuple[list[RefRecord], list[str]]:
    """按配额挑选，返回 ``(入选, 说明)``。

    为什么不能只按分数截前 N 名
    --------------------------
    实测（见 ``tools/smoke_references.py`` 的输出）：纯按分数截断时，高分项全落在
    同一本核心期刊、同一个年份区间，而且常常清一色英文——因为 SCI/SSCI 档的
    0.40 权重远大于"中文期刊"档。这样的清单拿去做开题报告会被导师一眼看穿
    "你只查了一个库"。所以先**按档位保底**，再按分数补足，最后才排总序。

    保底失败时（例如中文文献本来就少）**不报错、不硬凑**，而是把差额写进说明里，
    让用户知道"不是你检索错了，是公开库里中文题录就这么多"。
    """
    notes: list[str] = []
    chosen: list[RefRecord] = []
    taken: set[str] = set()

    def add(rec: RefRecord) -> None:
        if rec.key not in taken:
            taken.add(rec.key)
            chosen.append(rec)

    zh = sorted([c for c in cands if c.language == "zh"], key=lambda r: r.score, reverse=True)
    en = sorted([c for c in cands if c.language == "en"], key=lambda r: r.score, reverse=True)
    rest = sorted(cands, key=lambda r: r.score, reverse=True)

    # 1) 国内外保底。**先保底、再补足**，顺序不能反：反过来的话，国内名额会被
    #    总序里已经排满的英文条目挤掉，最后"国内外都要有"就落空了。
    #    保底项仍按分数顺序取，所以保底≠降质。
    for pool, need, label in ((zh, quota.min_cn, "国内"), (en, quota.min_en, "国外")):
        got = 0
        for rec in pool:
            if got >= need or len(chosen) >= quota.total:
                break
            add(rec)
            got += 1
        if got < need:
            notes.append(f"{label}文献只凑到 {got} 篇（目标 {need} 篇）：公开库里该选题的"
                         f"{label}题录不足，建议到知网/万方补充。")

    # 2) 时间跨度保底：挑候选里**最早**的两篇，保证清单不是"清一色近三年"。
    #    注意这里看的是候选全集的跨度，而不是"入选清单的跨度"——后者会自我实现，
    #    永远满足条件，等于没有这道闸门。
    years = sorted({int(c.year[:4]) for c in cands if str(c.year)[:4].isdigit()})
    if len(years) > 1 and years[-1] - years[0] >= quota.min_span_years:
        for rec in sorted([c for c in cands if str(c.year)[:4].isdigit()],
                          key=lambda r: int(r.year[:4]))[:2]:
            if len(chosen) < quota.total:
                add(rec)
    elif years:
        notes.append(f"候选文献的时间跨度只有 {years[0]}–{years[-1]} 年"
                     f"（不足 {quota.min_span_years} 年），这是检索源覆盖的问题，"
                     f"不是被排序筛掉的。")

    # 3) 时效保底：近 5 年至少若干篇（开题报告必须体现"这是当前的研究前沿"）。
    now = time.localtime().tm_year
    got_recent = sum(1 for c in chosen
                     if str(c.year)[:4].isdigit() and now - int(c.year[:4]) <= 5)
    for rec in rest:
        if got_recent >= quota.min_recent or len(chosen) >= quota.total:
            break
        if rec.key not in taken:
            add(rec)
            got_recent += 1

    # 4) 按分数补足到总数。这一步是"其余按分数排"的落点，前面三步只是在保底。
    for rec in rest:
        if len(chosen) >= quota.total:
            break
        add(rec)

    if len(chosen) < quota.total:
        notes.append(f"最终只有 {len(chosen)} 篇（目标 {quota.total} 篇）：候选总量不足。")

    chosen.sort(key=lambda r: r.score, reverse=True)
    return chosen[:quota.total], notes


def recommend(
    *,
    topic: str,
    keywords: list[str] | None = None,
    english: str = "",
    total: int = 15,
    min_cn: int = 4,
    min_en: int = 4,
    per_source: int = 12,
) -> dict:
    """完整的一次推荐：检索 → 合并 → 打分 → 配额挑选 → 排总序。

    **不做任何模型调用**。模型精排是调用方（API 层）可选追加的一步，
    这样"没填 Key 也要能用"这条底线不会被这里破坏。
    """
    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "error": "请填写选题或研究方向。"}

    total = max(10, min(int(total or 15), 20))
    candidates, status = collect(topic=topic, keywords=keywords, english=english,
                                 per_source=per_source)
    if not candidates:
        return {
            "ok": False,
            "error": "没有检索到任何候选文献。",
            "sources": status,
            "limits": _limits_note(),
        }

    for rec in candidates:
        _compose(rec, len(rec.matched_queries))

    quota = Quota(total=total, min_cn=min_cn, min_en=min_en)
    chosen, notes = _pick(candidates, quota)

    items = []
    for index, rec in enumerate(chosen, start=1):
        row = rec.as_reference()
        row["rank"] = index
        items.append(row)

    notes.extend(_coverage_notes(candidates, items))

    return {
        "ok": True,
        "topic": topic,
        "keywords": list(keywords or []),
        "items": items,
        "count": len(items),
        "candidates": len(candidates),
        "sources": status,
        "quota": {"total": total, "min_cn": min_cn, "min_en": min_en,
                  "min_span_years": quota.min_span_years, "min_recent": quota.min_recent},
        "summary": _summarize(items),
        "notes": notes,
        "limits": _limits_note(),
        "weights": describe_weights(),
    }


def _coverage_notes(candidates: list[RefRecord], items: list[dict]) -> list[str]:
    """就"检索源覆盖"这件事如实提醒。

    **这条提示是本模块最重要的一段话。** 实测（见 ``tools/smoke_references.py``）：
    以中文选题检索 OpenAlex / Crossref，返回的中文期刊绝大多数是开放获取的小刊，
    而且**期刊名常常与核心刊完全相同但 ISSN 完全不同**——例如检索结果里出现
    ISSN 2630-4686 的《教育研究》，而真正 CSSCI 的《教育研究》是 ISSN 1002-5731。
    这不是代码 bug，是公开库对中文文献的覆盖与著录质量问题。不把这件事说清楚，
    用户会以为"推荐出来没看到核心刊 = 这个选题没有核心文献"，那比不给推荐更糟。
    """
    notes: list[str] = []
    zh_cands = [c for c in candidates if c.language == "zh"]
    zh_chosen = [i for i in items if i.get("language") == "zh"]

    unverified = [i for i in zh_chosen
                  if any("核实" in w or "对不上" in w for w in (i.get("warnings") or []))]
    named = [i for i in zh_chosen if i.get("tier") not in {"中文期刊", "未收录/未知"}]
    if zh_chosen:
        notes.append(
            f"清单里有 {len(zh_chosen)} 篇中文文献，其中 {len(named)} 篇的期刊名命中了本机"
            f"核心目录条目。但 {len(zh_chosen)} 篇全部来自公开库（OpenAlex / Crossref），"
            "**没有一篇经过知网题录核实**：这两个库对知网主流中文核心刊的收录很有限，"
            "而且存在同名不同刊的情况（实测遇到过 ISSN 2630-4686 的《教育研究》，"
            "与 CSSCI 的《教育研究》ISSN 1002-5731 不是同一本）。"
            "**中文文献请一律用知网复核后再写进开题报告**；"
            "要补核心刊，用「资料获取」阶段或 cnki-helper。"
        )
    if unverified:
        notes.append(
            f"其中 {len(unverified)} 篇带 ⚠ 标记：期刊名与核心目录同名但 ISSN 不符或"
            "无法交叉核实。**这类条目最可能是仿冒刊，请优先核实甚至直接弃用。**"
        )
    if zh_cands and not named:
        notes.append(
            f"本次检索到的 {len(zh_cands)} 篇中文文献没有一篇命中本机核心目录。"
            "**这不代表该选题没有核心文献**，只代表公开库查不到中文核心刊的题录。"
        )
    return notes


def _summarize(items: list[dict]) -> dict:
    """清单的构成画像：等级分布、时间跨度、国内外比例。

    这张画像存在的意义是**让用户一眼看出清单有没有偏**。没有它，一份
    "15 篇全是 2023 年 SCI" 的清单看起来和一份均衡的清单一样正常。
    """
    tiers: dict[str, int] = {}
    langs: dict[str, int] = {}
    years: list[int] = []
    oa = 0
    for item in items:
        tiers[item["tier"]] = tiers.get(item["tier"], 0) + 1
        langs[item["language_label"]] = langs.get(item["language_label"], 0) + 1
        year = str(item.get("year") or "")[:4]
        if year.isdigit():
            years.append(int(year))
        if item.get("open_access"):
            oa += 1
    return {
        "tiers": tiers,
        "languages": langs,
        "year_min": min(years) if years else None,
        "year_max": max(years) if years else None,
        "span_years": (max(years) - min(years)) if years else None,
        "open_access": oa,
        "with_abstract": sum(1 for i in items if (i.get("abstract") or "").strip()),
        "with_doi": sum(1 for i in items if (i.get("doi") or "").strip()),
    }


def _limits_note() -> str:
    """如实说明这三项排序依据各自的可信度。这段文字必须原样展示给用户。"""
    return (
        "被引数来自 OpenAlex/Crossref 的公开字段，是**跨库统计**，与知网自己显示的"
        "「被引」数字不一致（通常知网偏低）；它在界面上标为「被引（OpenAlex）」。"
        "文献等级按本机内置的核心目录映射表匹配期刊名得出，"
        "**未命中就写「未收录/未知」，不猜**——南大 CSSCI、北大核心的目录没有公开接口，"
        "请以学校图书馆的正式目录为准。**下载量本次拿不到**：知网/万方只在页面上给"
        "下载量，没有公开 API，OpenAlex 也没有这个字段，所以「下载」一列显示为「—」；"
        "需要下载量请在知网检索页查看，或用 cnki-helper 脚本在你已登录的浏览器里抓取。"
    )


# ---------------------------------------------------------------------------
# 七、模型精排（可选，需要 Key）
# ---------------------------------------------------------------------------
def rerank_prompt(topic: str, candidates: list[dict], *, top: int = 40) -> list[dict]:
    """构造精排提示词。

    给的是**题名 + 期刊 + 年份 + 摘要前 220 字**，不是全文——精排只需判断主题是否
    贴题，给多了既贵又容易让模型被摘要里的细节带跑。
    """
    lines = []
    for i, item in enumerate(candidates[:top]):
        lines.append(
            f"[{i + 1}] {item.get('title')}｜{item.get('source')}｜{item.get('year')}"
            f"｜被引 {item.get('citations') if item.get('citations') is not None else '未知'}\n"
            f"    摘要：{(item.get('abstract') or '（无摘要）')[:220]}"
        )
    return [
        {
            "role": "system",
            "content": (
                "你在为一个中文开题报告筛参考文献。下面是从公开文献库检索到的候选。\n"
                f"选题：{topic}\n\n"
                "请对每一条给出 0~1 的相关度（1 = 直接支撑该选题的核心文献）与一句入选理由。\n"
                "判据：与选题的研究对象、方法或理论框架是否真正相关；\n"
                "**不要**因为期刊等级高就打高分——等级由程序另算；\n"
                "标题看着像但摘要明显是别的领域的，必须打低分。\n"
                "只输出 JSON：{\"items\":[{\"i\":1,\"rel\":0.9,\"why\":\"一句话\"},...]}\n"
                "不要解释、不要 Markdown 围栏。没有摘要的条目按标题判断，并在 why 里写明"
                "「仅凭标题判断」。"
            ),
        },
        {"role": "user", "content": "候选：\n" + "\n".join(lines)},
    ]


def apply_rerank(result: dict, scores: dict[int, dict]) -> dict:
    """把模型给的相关度并回推荐结果，**重新排序但保持配额不变**。

    这里刻意**不重新挑选成员**，只重排。原因：配额（国内外比例、时间跨度）是
    "清单不偏"的保证，让模型重选会把这份保证换成一个不可复核的判断。
    模型只影响"谁排前面"。
    """
    items = result.get("items") or []
    for index, item in enumerate(items, start=1):
        got = scores.get(index)
        if not got:
            continue
        try:
            item["relevance"] = max(0.0, min(1.0, float(got.get("rel"))))
        except (TypeError, ValueError):
            continue
        item["reason"] = str(got.get("why") or "")[:200]

    # 用同一套权重重算分数。
    for item in items:
        tier = item.get("tier") or "未收录/未知"
        parts = {
            "tier": _score_tier(tier),
            "citations": _score_citations(item.get("citations")),
            "recency": _score_recency(str(item.get("year") or "")),
            "open_access": 1.0 if item.get("open_access") else 0.0,
            "query_hits": float((item.get("parts") or {}).get("query_hits") or 0.0),
        }
        active = dict(WEIGHTS)
        if item.get("relevance") is None:
            active.pop("relevance", None)
        else:
            parts["relevance"] = float(item["relevance"])
        total_weight = sum(active.values()) or 1.0
        item["score"] = round(sum(parts[k] * w for k, w in active.items()) / total_weight, 3)
        item["parts"] = {k: round(v, 3) for k, v in parts.items()}

    items.sort(key=lambda i: i.get("score") or 0, reverse=True)
    for index, item in enumerate(items, start=1):
        item["rank"] = index
    result["items"] = items
    result["reranked"] = True
    return result


# ---------------------------------------------------------------------------
# 八、导出
# ---------------------------------------------------------------------------
def _authors_list(raw: str) -> list[str]:
    """拆作者。中文顿号/逗号/分号/空格都是分隔符，但要避免拆开复姓。

    英文姓名里的逗号是"姓, 名"而不是作者分隔，所以一旦出现拉丁字母就按
    " and " / ";" / "、" 拆，不按逗号拆。
    """
    text = (raw or "").strip()
    if not text:
        return []
    if re.search(r"[A-Za-z]{3,}", text):
        parts = re.split(r"\s*(?:;|、| and |＆|&)\s*", text)
    else:
        parts = re.split(r"\s*[;、,，；]\s*", text)
    return [p.strip() for p in parts if p.strip()]


def _bibtex_key(item: dict, used: set[str]) -> str:
    first = (_authors_list(str(item.get("authors") or "")) or ["anon"])[0]
    surname = re.sub(r"[^A-Za-z]", "", first.split()[-1] if re.search(r"[A-Za-z]", first) else first)
    if not surname:
        surname = "ref"
    year = re.sub(r"[^0-9]", "", str(item.get("year") or ""))[:4] or "n"
    key = f"{surname.lower()}{year}"
    if key in used:
        n = 2
        while f"{key}{chr(ord('a') + n - 2)}" in used:
            n += 1
        key = f"{key}{chr(ord('a') + n - 2)}"
    used.add(key)
    return key


def _gbt_kind(item: dict) -> str:
    """GB/T 7714 的文献类型标识。

    **不能一律写 ``[J]``**：OpenAlex 会把机构知识库、Zenodo、学位论文库也当作
    "来源"，它们的名字看起来像期刊名（实测出现过 "Montana State University
    ScholarWorks"）。一律标 [J] 等于把学位论文写成期刊论文，那是实质错误。
    """
    vtype = str(item.get("venue_type") or "").lower()
    tier = str(item.get("tier") or "")
    if vtype == "repository" or tier in {"仓储/未正式发表", "预印本"}:
        return "[D]"          # 学位论文/未正式发表
    if vtype == "conference" or tier == "会议/论文集":
        return "[C]"
    if vtype == "book":
        return "[M]"
    return "[J]"


def render_gbt(items: list[dict]) -> str:
    """GB/T 7714 风格。**缺字段就不写那一项**，不补年份、不编期刊名。"""
    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        authors = ", ".join(_authors_list(str(item.get("authors") or ""))[:3])
        if authors and len(_authors_list(str(item.get("authors") or ""))) > 3:
            authors += ", 等"
        title = str(item.get("title") or "（无题名）")
        venue = str(item.get("venue") or "")
        year = str(item.get("year") or "")
        doi = str(item.get("doi") or "")
        kind = _gbt_kind(item)
        tail = "".join([f". {venue}" if venue else "", f", {year}" if year else "", "."])
        doi_part = f" DOI: {doi}." if doi else ""
        rank = str(item.get("tier") or "")
        mark = f"  // 文献等级：{rank}" if rank and rank not in {"未收录/未知"} else ""
        lines.append(f"[{index}] {authors + '. ' if authors else ''}{title}{kind}{tail}{doi_part}{mark}")
    return "参考文献\n\n" + "\n".join(lines) + "\n"


def render_bibtex(items: list[dict]) -> str:
    used: set[str] = set()
    blocks: list[str] = []
    for item in items:
        fields: list[tuple[str, str]] = []
        if str(item.get("title") or "").strip():
            fields.append(("title", str(item["title"])))
        authors = _authors_list(str(item.get("authors") or ""))
        if authors:
            fields.append(("author", " and ".join(authors)))
        if str(item.get("year") or "").strip():
            fields.append(("year", str(item["year"])))
        if str(item.get("venue") or "").strip():
            fields.append(("journal", str(item["venue"])))
        if str(item.get("doi") or "").strip():
            fields.append(("doi", str(item["doi"])))
        elif str(item.get("url") or "").strip():
            fields.append(("url", str(item["url"])))
        if item.get("citations") is not None:
            fields.append(("note", f"被引 {item['citations']}（OpenAlex）"))
        lines = [f"  {name} = {{{value.replace('{', '').replace('}', '')}}}," for name, value in fields]
        blocks.append("\n".join([f"@article{{{_bibtex_key(item, used)},"] + lines + ["}"]))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def render_markdown(result: dict) -> str:
    """Markdown 清单。带上构成画像与排序依据——这份文件是要交给人看的。"""
    items = result.get("items") or []
    summary = result.get("summary") or {}
    lines = [
        f"# 参考文献推荐 · {result.get('topic') or '（未填选题）'}",
        "",
        f"共 {len(items)} 篇，由公开文献库（OpenAlex / Crossref / arXiv）检索后按"
        f"文献等级、被引量、时效与开放获取综合排序生成，"
        f"导出于 {time.strftime('%Y-%m-%d %H:%M:%S')}。",
        "",
        "## 清单构成",
        "",
        f"- 时间跨度：{summary.get('year_min') or '?'}–{summary.get('year_max') or '?'}"
        f"（{summary.get('span_years') if summary.get('span_years') is not None else '?'} 年）",
        f"- 等级分布：" + ("、".join(f"{k} {v} 篇" for k, v in (summary.get("tiers") or {}).items()) or "—"),
        f"- 国内外：" + ("、".join(f"{k} {v} 篇" for k, v in (summary.get("languages") or {}).items()) or "—"),
        f"- 开放获取：{summary.get('open_access', 0)} 篇可直接下载全文",
        f"- 有摘要：{summary.get('with_abstract', 0)} 篇；有 DOI：{summary.get('with_doi', 0)} 篇",
        "",
        "## 排序依据",
        "",
        result.get("limits") or "",
        "",
        "## 清单",
        "",
        "| # | 题名 | 作者 | 年份 | 来源 | 等级 | 被引（OpenAlex） | 下载 | 开放获取 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in items:
        cells = [
            str(item.get("rank") or ""),
            str(item.get("title") or "（无题名）"),
            str(item.get("authors") or ""),
            str(item.get("year") or ""),
            str(item.get("source") or ""),
            str(item.get("tier") or ""),
            str(item.get("citations") if item.get("citations") is not None else "—"),
            "—",   # 下载量：拿不到，如实写 —
            "是" if item.get("open_access") else "",
        ]
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")

    lines += ["", "## 逐条入选理由", ""]
    for item in items:
        lines.append(f"### [{item.get('rank')}] {item.get('title')}")
        lines.append("")
        lines.append(f"- 命中理由：{item.get('why') or '—'}")
        if item.get("reason"):
            lines.append(f"- 模型判断：{item['reason']}")
        if item.get("tier_basis"):
            lines.append(f"- 等级依据：{item['tier_basis']}")
        for warn in item.get("warnings") or []:
            lines.append(f"- ⚠ 可信度提示：{warn}")
        if item.get("issn"):
            lines.append(f"- ISSN：{item['issn']}")
        lines.append(f"- 来源库：{'、'.join(item.get('sources') or [])}")
        if item.get("doi"):
            lines.append(f"- DOI：{item['doi']}")
        if item.get("oa_url"):
            lines.append(f"- 全文：{item['oa_url']}")
        if item.get("abstract"):
            lines.append(f"- 摘要：{item['abstract'][:400]}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
