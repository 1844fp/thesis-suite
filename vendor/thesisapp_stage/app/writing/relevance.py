"""知识库相关性判定：判断"这批证据跟我的题目到底有没有关系"。

## 为什么需要这一层

写作链路默认"检索到的就是可用的材料"。这个假设在**题目与知识库同领域**时成立，
换个题目就彻底失效：向量检索**永远会返回 top-k**，哪怕库里一篇相关的都没有。
于是写一篇"遥感图像分割"的论文，会拿到一堆"数据资产估值"的片段当证据——
文末还正儿八经地列着参考文献。**这比检索不到更危险**：它产出的是"看起来完成了"的稿子，
而作者要读很久才会发现引的全是无关文献。

## 为什么不用知识库返回的 score

第一版想直接用检索分数卡阈值，实测发现**分数尺度不可靠**：

    查询「数据资产 会计确认 计量属性」（相关）  首条 0.21
    查询「卷积神经网络 图像分割」（完全无关）  首条 18.76   ← 比相关的高 89 倍

重排模型对跨领域查询会给出量纲完全不同的分数。**用一个自己都不稳定的信号做门槛，
等于没有门槛**（而且会静默放过最该拦的那一类）。所以这里改用本地可解释的口径：

## 口径：题目关键词 × 证据文本的重合

1. 从题目与写作要求里切出**核心概念短语**（去掉"研究/分析/基于/影响"这类学术通用词），
   再展开成 2~4 字的子词；
2. 证据文本里命中**至少一个 ≥3 字的词**，或**至少两个不同的 2 字词**，才算相关；
3. 一条证据都判不出来 → 这一章"知识库里没有相关材料"，拒绝生成。

第 2 条的两个分支是为了同时挡住两类误判：

* 只认 2 字词，会让"固定资产管理"的题目因为命中"资产"而匹配上数据资产库；
* 只认 ≥3 字词，会让"数据资产"这种核心概念只有 4 字的题目在分词边界上漏掉。

判定结果**逐条给出命中了哪些词**，作者能自己核对——相关性判断不该是个黑箱。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

__all__ = [
    "TopicKeywords",
    "RelevanceVerdict",
    "extract_keywords",
    "match_terms",
    "judge_entries",
    "is_relevant",
    "RELEVANCE_META_KEY",
]

#: 项目 meta 里存相关性预检结果的键。
RELEVANCE_META_KEY = "relevance_precheck"

#: 学术通用词。出现在几乎任何题目里，**不具区分度**，必须去掉，
#: 否则"研究""分析"会把无关文献也判成相关。
GENERIC_TERMS: frozenset[str] = frozenset({
    "研究", "分析", "探析", "初探", "探讨", "思考", "综述", "述评", "评述", "进展",
    "基于", "关于", "面向", "针对", "及其", "以及", "以及", "相关", "主要", "重要",
    "方法", "问题", "影响", "作用", "关系", "机制", "机理", "路径", "模式", "策略",
    "对策", "措施", "建议", "启示", "意义", "背景", "现状", "发展", "趋势", "应用",
    "设计", "实现", "开发", "构建", "评价", "优化", "改进", "提升", "创新", "实践",
    "理论", "实证", "定量", "定性", "案例", "调查", "实验", "试验", "探索", "验证",
    "我国", "中国", "国外", "国内", "一个", "一种", "若干", "视角", "维度", "层面",
    "视域", "视野", "进路", "范式", "框架", "体系", "系统", "模型", "算法", "技术",
    "the", "and", "for", "with", "based", "study", "research", "analysis", "review",
})

#: 把题目切成概念短语的连接成分。切完剩下的片段才是"概念"。
_SPLIT_RE = re.compile(
    r"(?:"
    r"研究|分析|探析|初探|探讨|思考|综述|述评|评述|进展|"
    r"基于|关于|面向|针对|及其|以及|"
    r"影响|作用|关系|机制|机理|路径|模式|策略|对策|措施|建议|启示|"
    r"意义|背景|现状|发展|趋势|应用|设计|实现|开发|构建|评价|优化|"
    r"的|之|与|和|及|或|在|对|为|以|从|向|把|被|中|下|上|内|外|间"
    r")"
)

_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_EN_RUN = re.compile(r"[A-Za-z][A-Za-z0-9\-]{2,}")


@dataclass(frozen=True)
class TopicKeywords:
    """从题目与写作要求里抽出的关键词。"""

    phrases: tuple[str, ...] = ()
    """核心概念短语（切分后的整段，如「数据资产化」「企业融资约束」）。"""

    terms: tuple[str, ...] = ()
    """用于匹配的全部词：短语本身 + 其 2~4 字子词 + 英文词。"""

    def as_dict(self) -> dict:
        return {"phrases": list(self.phrases), "terms": list(self.terms)}


def _sub_terms(phrase: str, *, min_len: int = 2, max_len: int = 4) -> list[str]:
    """一个短语的全部 2~4 字连续子串。

    用子串而不是分词结果：中文分词在专业术语上并不可靠（实测 jieba 把
    「数据资产化」切成「化对」），而子串匹配是确定性的、可解释的，
    也不引入额外依赖。
    """
    out: list[str] = []
    n = len(phrase)
    for size in range(min_len, min(max_len, n) + 1):
        for i in range(n - size + 1):
            piece = phrase[i : i + size]
            if piece in GENERIC_TERMS:
                continue
            if piece not in out:
                out.append(piece)
    return out


def extract_keywords(topic: str, requirement: str = "", *, limit: int = 60) -> TopicKeywords:
    """从题目（与写作要求）里抽取关键词。

    要求文本里通常有"采用问卷调查""样本不少于 300 份"这类**方法性**描述，
    它们对判相关有用（说明该题目确实属于实证研究），所以一并抽。
    """
    text = f"{topic or ''}。{requirement or ''}"
    phrases: list[str] = []

    # 切出连续中文片段，再按连接词切成概念
    for run in _CJK_RUN.findall(text):
        for piece in _SPLIT_RE.split(run):
            piece = piece.strip()
            if len(piece) < 2 or piece in GENERIC_TERMS:
                continue
            if piece not in phrases:
                phrases.append(piece)

    for en in _EN_RUN.findall(text):
        low = en.lower()
        if low in GENERIC_TERMS:
            continue
        if en not in phrases:
            phrases.append(en)

    terms: list[str] = []
    for p in phrases:
        if p not in terms:
            terms.append(p)
        if _CJK_RUN.fullmatch(p):
            for t in _sub_terms(p):
                if t not in terms:
                    terms.append(t)

    return TopicKeywords(phrases=tuple(phrases[:limit]), terms=tuple(terms[: limit * 6]))


def match_terms(text: str, keywords: TopicKeywords) -> list[TermHit]:
    """这段文本命中了哪些词（保序、去重），并标出命中的**性质**。"""
    t = text or ""
    phrases = set(keywords.phrases)
    out: list[TermHit] = []
    seen: set[str] = set()
    for term in keywords.terms:
        if term in seen or term not in t:
            continue
        seen.add(term)
        if term in phrases:
            kind = "phrase"
        elif _EN_RUN.fullmatch(term):
            kind = "en"
        else:
            kind = "sub"
        out.append(TermHit(term=term, kind=kind))
    return out


#: 判定一条证据相关：
#:
#: * **整短语命中**（任意长度）—— 最强信号。这也解决了一个真实的失败模式：
#:   题目核心概念只有 2 字（如「桥梁」「绩效」）时，子词也全是 2 字，
#:   只认 ≥3 字子词会让它在自己的领域里被判成无关。
#: * 或 **≥3 字的子词命中** —— 「融资约束」「图像分割」这类不会误撞。
#:
#: **刻意不再接受"两个 2 字子词"**。那一档是误报的来源：实测「遥感图像语义分割」
#: 在数据资产库上命中了「图像」「分割」「语义」（"收益分割"里的"分割"、
#: "临床图像"里的"图像"），于是 10 条无关文献里有 2 条被判成相关。
MIN_STRONG_LEN = 3


def is_relevant(hits: Sequence[TermHit]) -> bool:
    """命中列表 → 是否判为相关（条目级）。"""
    return any(h.kind in ("phrase", "en") or len(h.term) >= MIN_STRONG_LEN for h in hits)


#: 章级闸门：**光有条目级判定不够**。
#:
#: 上面那条规则仍会漏进少量偶然重合的文献（"深度学习"确实可能出现在一篇
#: 数据资产论文里）。所以章级判定再加两个数量条件：
#:
#: * 至少 ``MIN_RELEVANT_ENTRIES`` 条相关；
#: * 相关占比不低于 ``MIN_RELEVANT_RATIO``。
#:
#: 真正干活的是**比例**这一条；条数下限只是挡住"2 条里 1 条相关"这种
#: 比例过关但样本太小的情况（1 条证据撑不起一章）。
#:
#: 实测标定（数据资产库 20 篇，每主题取 10 条）：
#:
#:     相关题目（数据资产 / 数据要素 / 数字化转型）  10/10 相关，占比 1.00   → 放行
#:     遥感图像语义分割                               2/10 相关，占比 0.20   → 拦下
#:     糖尿病胰岛素抵抗                               1/10 相关，占比 0.10   → 拦下
#:
#: 间距足够大（1.00 对 0.20），不是卡在边界上的阈值。
MIN_RELEVANT_ENTRIES = 2
MIN_RELEVANT_RATIO = 0.25


@dataclass(frozen=True)
class TermHit:
    """一次关键词命中。``kind`` 说明这个命中有多可信。"""

    term: str
    #: ``phrase`` 整短语命中（最可信）｜ ``en`` 英文词命中｜ ``sub`` 子词命中（可能偶然重合）
    kind: str


@dataclass
class RelevanceVerdict:
    """一批证据与题目的相关性判定结果。"""

    keywords: TopicKeywords = field(default_factory=TopicKeywords)
    relevant: list[Any] = field(default_factory=list)
    irrelevant: list[Any] = field(default_factory=list)
    #: 每条证据命中的词：``{编号或下标: [词, ...]}``
    matched: dict[int, list[str]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.relevant) + len(self.irrelevant)

    @property
    def ratio(self) -> float:
        return len(self.relevant) / max(1, self.total)

    @property
    def usable(self) -> bool:
        """这一批证据够不够支撑写作。

        **这是决定"能不能写"的唯一判据**：不满足就拒绝调用模型，
        并如实告诉作者"知识库里没有与本题相关的材料"。
        宁可让作者去补资料，也不要拿无关文献凑出一篇看起来完整的稿子。
        """
        return len(self.relevant) >= MIN_RELEVANT_ENTRIES and self.ratio >= MIN_RELEVANT_RATIO

    @property
    def level(self) -> str:
        """``strong`` / ``partial`` / ``none``。``none`` 表示不该据此写作。"""
        if not self.usable:
            return "none"
        return "strong" if self.ratio >= 0.5 else "partial"

    @property
    def matched_terms(self) -> list[str]:
        seen: list[str] = []
        for terms in self.matched.values():
            for t in terms:
                if t not in seen:
                    seen.append(t)
        return seen

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "usable": self.usable,
            "relevant": len(self.relevant),
            "irrelevant": len(self.irrelevant),
            "ratio": round(self.ratio, 3),
            "keywords": list(self.keywords.phrases),
            "matched_terms": self.matched_terms[:20],
        }


def _entry_text(entry: Any) -> str:
    """把一条证据里**可用于判相关**的字段拼起来。

    刻意**包含 citation 与 title**：很多证据片段本身是数据表或公式，
    光看正文判不出主题，而题名一看就知道。反过来，只看题名会漏掉
    "用数据资产做例子但题名不含该词"的文献。
    """
    parts = [
        str(getattr(entry, "text", "") or ""),
        str(getattr(entry, "citation", "") or ""),
        str(getattr(entry, "title", "") or ""),
        str(getattr(entry, "section", "") or ""),
    ]
    return " ".join(p for p in parts if p)


def judge_entries(
    entries: Sequence[Any],
    keywords: TopicKeywords,
    *,
    min_relevant: int = 1,
) -> RelevanceVerdict:
    """逐条判定证据是否与题目相关。

    返回的 ``relevant`` 才是可以进提示词的材料。``irrelevant`` 不丢弃而是报给作者——
    作者需要知道"检索到了 20 条，其中 18 条与本题无关"，才会去补资料，
    而不是以为知识库里已经有材料了。
    """
    verdict = RelevanceVerdict(keywords=keywords)
    if not keywords.terms:
        # 题目里抽不出任何概念词（例如题目是空的）：不做判断，全部当作可用。
        # 这时拦下来只会让用户困惑"为什么写不了"，而问题其实出在题目没填。
        verdict.relevant = list(entries)
        return verdict

    for e in entries:
        hits = match_terms(_entry_text(e), keywords)
        idx = int(getattr(e, "index", 0) or 0)
        if hits:
            verdict.matched[idx] = [h.term for h in hits]
        if is_relevant(hits):
            verdict.relevant.append(e)
        else:
            verdict.irrelevant.append(e)
    return verdict
