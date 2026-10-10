"""能力③「论文写作助手」——分节起草（writer:draft）。

这个智能体是本能力里唯一会产出正文的地方，所以它被四道闸门夹住：

1. **先检索再写。** 每一章都先去知识库取证据，把证据**原文连同编号**
   一起给模型。不检索就写，模型只能靠记忆编——那正是本项目要消灭的东西。
2. **角标必须可追溯。** 提示词要求每条事实性论断标注证据编号，但**模型会编造
   编号**，所以这里逐条用代码校验：编号不在本单元证据范围内的一律删除并计数。
   报告里会写"模型试图编造 N 处引用"。留着假角标比没有角标更危险——它会让作者
   以为那句话有依据。
3. **不确定的显式交还。** 模型说不清的地方用 ``【待作者补充：…】`` 标出，
   这里再把它们收成 ``writing.needs_source``（``fixable=False``）交给作者。
   自动"再查查然后补上"是错的：模型补出来的只会是编的。
4. **抄袭式的自我重复会被清掉。** 分单元写作的通病是后一个单元把前一个单元的话
   再说一遍。跨单元逐句比对，**完全相同的长句只保留首次出现**并逐条记账。

## 一章不是一个写作单元

一份硕士论文的核心章常在 6000~12000 字。单次生成写不了这么长——模型的输出上限
与"写得越长越注水"的倾向都会让结果退化。所以这里按**小节**把一章切成一串写作单元，
每个单元 1200~2500 字、单独检索最相关的证据、单独生成，最后拼回本章。

单元的划分优先取**作者开题报告里拟定的小节**（``outline → subsections``）：
作者的小节是开题已通过的，换成模型现编的会让成稿与开题对不上。作者没列小节时，
退回模型在规划阶段给出的要点；再没有，就用蓝图的结构骨架兜底。

## 引用编号是**全篇唯一**的

起草阶段原先按章检索、每章从 ``[1]`` 开始编号。单章看没问题，但整篇合并后
绪论的 ``[1]`` 与综述的 ``[1]`` 指向不同的文献——一份学位论文只有一张参考文献表，
这是实打实的引用错误。现在编号从项目级证据池
（``app/writing/citations.py`` 的 ``EvidencePool``）分配：**同一段原文被两章分别
检索到时拿到同一个编号**，``[n]`` 从此在全篇范围内唯一。

校验时的可见范围仍限于"本单元提示词里给过模型的编号"：编号是全局的，
可见范围是局部的，所以模型既编不出池外的号，也编不出它没见过的号。

## 两条刻意的边界

* **不写结论与创新点。** 大纲里有这类章节时直接跳过并在报告里说明，不偷偷换成
  "简要总结"。（结论由 ``writer:conclude`` 单独处理，且只做正文的汇总。）
* **每章追加一个新版本的 draft。** 起草是逐章增量推进的过程，覆盖式写入会让
  作者没法回退到"只有前两章"的状态。版本链就是写作过程本身。
"""

from __future__ import annotations

import re
import time
from typing import Any, Sequence

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage, new_id
from ..writing import BODY, INTRODUCTION, LITERATURE_REVIEW, Blueprint, blueprint_for, checkers_for
from ..writing.citations import EvidenceEntry, EvidencePool, litter as cite_litter
from ..writing.examples import fetch_examples, format_examples
from ..writing.relevance import extract_keywords, judge_entries
from .writer_common import (
    SearchFactory,
    clean_title,
    find_gaps,
    format_evidence,
    is_author_only,
    is_fatal_llm_error,
    merge_section,
    section_kind,
    selected_sections,
    split_sections,
    unfinalize_text,
    validate_citations,
)

#: 每个检索词取多少条证据。
SEARCH_K = 6
#: 小节字数超过这个上限才拆成多个写作单元。低于它不拆——**用上限而不是目标值
#: 当拆分阈值**是有意的：目标值只是"一章默认多少字"的估计，拿它当阈值会把 2000 字
#: 的小节也切成两半，白白多花一次调用，还容易切出重复内容。
MAX_UNIT_WORDS = 2800
#: 单章未指定字数时，每个小节按这个量估算。
TARGET_UNIT_WORDS = 1800
#: 单元字数的下限。低于下限的单元写不出完整论证，宁可少切几刀。
MIN_UNIT_WORDS = 600
#: 一个小节最多拆成几个单元。再拆下去每一段都会脱离上下文。
MAX_PARTS_PER_SUBSECTION = 3
#: 一章最多几个写作单元。这是成本护栏：单元数直接等于模型调用次数。
MAX_UNITS_PER_CHAPTER = 8
#: 单单元提示词里最多摆几条证据。
MAX_EVIDENCE_PER_UNIT = 8
#: 单次生成的 token 上限护栏。按目标字数换算后再封顶，避免模型写超。
MAX_TOKENS_CAP = 8000
#: 传给下一个单元的"上一单元结尾"字符数。够看出衔接关系即可，多了挤占预算。
TAIL_CHARS = 220

DRAFT_SYSTEM = (
    "你是中文学位论文的写作助手，目标是达到 CSSCI 期刊的写作水准。"
    "你写的每一句实质性论断都必须来自给定的资料，并标注资料编号。"
    "你不替作者下结论，不评价研究价值，不写创新点。"
    "资料不足时你如实留白，不靠常识或印象补全。"
    "\n\n"
    "关于写作水准，你要特别做到三点："
    "（1）文献不是逐篇介绍，而是**归入脉络、先给判断再摆证据**——"
    "同一主题的多篇文献要合并成一句话来支持一个判断，禁止「A 做了什么、B 做了什么」的罗列；"
    "（2）每段都有主题句，段落之间要有显式的承接与推进，不能是并列的段落堆叠；"
    "（3）摆出事实之后必须解释它意味着什么，不能只陈述结果。"
    "\n\n"
    "你在分小节写作，每次只写一个小节。你要与前后小节自然衔接，"
    "不要重复前面已经写过的内容，也不要在结尾做全文总结。"
)

DRAFT_USER_TEMPLATE = """请为下面这一小节写正文。

论文标题：{topic}
本章标题：{section}
本小节标题：{unit}
本小节在本章中的位置：第 {part} / {parts} 部分
{needs}{requirement}
目标字数：约 {words} 字

可用的资料（**这是你唯一可以依据的材料**）：
{evidence}
{examples}
{blueprint}
{previous}
写作要求：
1. 只使用上面列出的资料。凡是超出这些资料的内容，一律不许写。
2. 每写一句事实性论断，紧跟一个角标，编号取自上面对应的资料编号，例如 `……[1]`。
   同一句用到多条资料写成 `[1,3]`。编号只能在上面的编号里选，**不许使用列表以外的编号**。
   角标**不要加圆括号**（不要写成 `（[1]）`），也不要用上标符号，直接写 `[1]`。
3. 需要数据、案例、政策原文支撑而现在没有的地方，就地写
   `【待作者补充：具体说明需要什么】`，不要用常识或记忆填上。
   宁可留白，也不要写一句没有依据的话。
4. **不要写研究结论，不要写创新点，不要评价本文的价值和意义。**
   本小节只做"材料—事实—论证"的铺设；结论留给作者。
5. **不要写小节标题**（系统会自己加），直接从正文第一段开始写。
6. 不要出现"综上所述""值得注意的是"这类空转的过渡语。
7. 不要写参考文献列表，也不要写 [1] 开头的条目行——角标只用于正文引用。
8. 只写本小节，写到目标字数即可收尾，不要在结尾预告下一小节的内容。

直接输出正文（不要 JSON，不要解释）。
"""


def _num(value: Any, default: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _max_tokens_for(words: int) -> int:
    """按目标字数换算生成长度上限。

    中文在主流模型里约 0.6~1 token/字，取 1.8 倍留出余量（公式、英文术语更费 token），
    再封顶。**余量不能给太大**：上限越松，模型越倾向于"多写点显得丰富"，
    而每多写一句无依据的话，作者的核对成本就多一分。
    """
    return max(1200, min(MAX_TOKENS_CAP, int(words * 1.8) + 600))


def _writing_checkers() -> dict:
    """写作质量检查器表：名字 → 函数。

    从注册表取而不是 import 具体函数：检查器可能因为依赖问题没装上，
    取不到时自查这一步**跳过而不是崩**——起草不该因为一个风格检查器不可用而失败。
    延迟到调用时取，是为了避免模块导入期的循环依赖（检查器模块会 import 框架）。
    """
    try:
        from ..framework.registry import verifiers_for

        return {v["name"]: v["fn"] for v in verifiers_for("draft")}
    except Exception:  # noqa: BLE001
        return {}


#: 模块级缓存。注册表在导入期填好，运行期不再变化。
_WRITING_CHECKERS: dict = {}


def _summarize_quality(hits: list[dict[str, Any]]) -> dict[str, int]:
    """按检查器类别汇总质量命中，便于报告里一眼看出最普遍的问题是哪一类。"""
    out: dict[str, int] = {}
    for h in hits:
        out[h.get("checker", "?")] = out.get(h.get("checker", "?"), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------------------
# 写作单元的切分
# ---------------------------------------------------------------------------


def plan_units(
    section: dict[str, Any],
    blueprint: Blueprint,
    chapter_words: int,
) -> list[dict[str, Any]]:
    """把一章切成一串写作单元。

    切分依据是**作者的小节划分**（大纲里的 ``subsections``，来自开题报告的"结构框架"）：
    每个小节一个单元，除非这个小节本身超过 ``MAX_UNIT_WORDS``——那时把它按蓝图的结构
    骨架拆成"同一小节的上/下部分"，并在提示词里给每部分一个侧重点。

    **拆出来的部分不会被安上新的小节标题。** 这一点是刻意设计：如果为了切分而编造
    "1.1.1 ……" 这样的标题，成稿里就会出现作者从未同意过的目录项，而目录是评审
    对照开题检查的第一样东西。所以多出来的部分共用同一个小节标题，只靠"侧重"
    在提示词里区分。

    作者没有给小节时，**整章作为一个单元**，蓝图的骨架只作为"要覆盖这几件事"的
    侧重提示——绝不用语步名（「确立研究领域」）当小节标题，那是修辞步骤的名字，
    出现在成稿的小标题里会非常突兀。

    返回的每个单元形如::

        {"heading": "数据资产的确认条件分析", "part": 1, "parts": 2,
         "focus": "数据来源与样本", "words": 1800}
    """
    chapter_title = clean_title(section.get("title"))
    subs = [clean_title(s) for s in (section.get("subsections") or []) if clean_title(s)]
    if not subs:
        subs = [clean_title(p) for p in (section.get("points") or []) if clean_title(p)]
    whole_chapter = not subs
    if whole_chapter:
        subs = [chapter_title]

    chapter_words = _num(chapter_words, TARGET_UNIT_WORDS * len(subs))
    per_sub = max(MIN_UNIT_WORDS, chapter_words // max(1, len(subs)))
    move_names = [name for name, _ in blueprint.moves]

    units: list[dict[str, Any]] = []
    for sub in subs:
        # 超过单单元上限才拆。用上限而不是目标值来判：目标值只是"默认一章多少字"的
        # 估计，用它当拆分阈值会把 2000 字的小节也切成两半，白白多花一次调用。
        parts = max(1, min(MAX_PARTS_PER_SUBSECTION, -(-per_sub // MAX_UNIT_WORDS)))
        part_words = max(MIN_UNIT_WORDS, per_sub // parts)
        for i in range(parts):
            focus = ""
            if parts > 1:
                # 多部分时给每部分一个侧重点，否则模型会把同一件事写两遍
                # （实测：不提示时跨部分重复率极高，去重只能事后补救）。
                focus = move_names[min(i, len(move_names) - 1)] if move_names else ""
            elif whole_chapter and move_names:
                # 整章一个单元：把骨架整体作为"本章要覆盖的事"给出，当结构引导。
                focus = "；".join(move_names)
            units.append(
                {
                    "heading": sub,
                    "part": i + 1,
                    "parts": parts,
                    "focus": focus,
                    "words": part_words,
                    # 单元标题与章标题相同时不另起 ``## `` 小标题，
                    # 否则成稿会出现「# 绪论」下面紧跟「## 绪论」这种重复。
                    "is_chapter_heading": sub == chapter_title,
                }
            )
        if len(units) >= MAX_UNITS_PER_CHAPTER:
            break
    return units[:MAX_UNITS_PER_CHAPTER]


# ---------------------------------------------------------------------------
# 证据挑选
# ---------------------------------------------------------------------------


def _bigrams(text: str) -> set[str]:
    t = re.sub(r"[\s\W_]+", "", text or "")
    return {t[i : i + 2] for i in range(max(0, len(t) - 1))}


def _overlap(query: str, entry: EvidenceEntry) -> float:
    """词面重合度：查询与证据（题名+正文）的二元字组重合比例。

    为什么不用向量检索重排：重排会对每一段调用模型，逐单元循环时成本翻好几倍，
    而这里只需要"在这几条候选里挑最相关的"——候选本来就来自同一章的检索结果，
    词面重合足够把不相关的挑出去。**这一步不产生新证据，只做筛选**，
    所以即使排序不完美也不会引入错误。
    """
    q = _bigrams(query)
    if not q:
        return 0.0
    e = _bigrams((entry.title or "") + (entry.citation or "") + (entry.text or "")[:600])
    if not e:
        return 0.0
    return len(q & e) / len(q)


def pick_evidence(
    entries: Sequence[EvidenceEntry],
    query: str,
    *,
    limit: int = MAX_EVIDENCE_PER_UNIT,
) -> list[EvidenceEntry]:
    """从本章证据里挑出给本单元用的那几条。

    挑不满时**不补**别的章的条目：宁可证据少一点，也不要拿无关材料去撑字数。
    一条证据都没有的单元会在调用模型之前就被拦下（见 ``run``）。
    """
    if len(entries) <= limit:
        return list(entries)
    ranked = sorted(entries, key=lambda e: (-_overlap(query, e), e.index))
    return ranked[:limit]


# ---------------------------------------------------------------------------
# 跨单元去重
# ---------------------------------------------------------------------------

_SENT_SPLIT = re.compile(r"(?<=[。！？；])")
#: 判定"重复句"的最短长度。太短的句子（"因此，这一点很重要。"）重复出现是正常的
#: 行文需要，删掉反而破坏连贯；长句重复才是分单元写作的通病。
DUP_MIN_CHARS = 24


def drop_repeated_sentences(existing: str, new: str) -> tuple[str, list[str]]:
    """删掉 ``new`` 里与 ``existing`` 完全相同的长句，返回 ``(新文本, 被删句子)``。

    只删**逐字相同且长度 ≥ ``DUP_MIN_CHARS``** 的句子。这条口径刻意保守：
    改写过的重复（"该结论表明……"与"这一结论说明……"）不删，因为那是行文的正常变化，
    而机械删除会破坏段落连贯——**唯一没有争议的重复就是一字不差的重复**。
    """
    if not existing or not new:
        return new, []
    seen = {s.strip() for s in _SENT_SPLIT.split(existing) if len(s.strip()) >= DUP_MIN_CHARS}
    if not seen:
        return new, []
    dropped: list[str] = []
    out: list[str] = []
    for sent in _SENT_SPLIT.split(new):
        key = sent.strip()
        if len(key) >= DUP_MIN_CHARS and key in seen:
            dropped.append(key)
            continue
        out.append(sent)
    cleaned = "".join(out)
    # 删句可能留下连续空白或行首标点，压一下；不改动其他任何字符。
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned, dropped


class WriterDraftAgent(Agent):
    """按大纲逐章起草：一章切成多个写作单元，逐条校验角标，证据台账留给验证环节。"""

    name = "writer:draft"
    description = (
        "按大纲逐章起草：每章按小节切成多个写作单元，单元内只用检索到的证据说话，"
        "角标跨全篇唯一、越界即删除并计数"
    )
    stage = Stage.DRAFT.value
    capabilities = ("write", "draft")
    uses_model = True

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        board = ctx.board
        # 自查用的检查器表。放在这里取而不是模块级：注册表的填充发生在
        # app.checks 被导入时，模块级取可能拿到空表。
        global _WRITING_CHECKERS
        _WRITING_CHECKERS = _writing_checkers()

        outline = board.head("outline")
        if outline is None:
            return self.report(
                ctx,
                ok=False,
                summary="没有找到写作大纲，请先运行写作规划（writer:plan）",
                error="board 中不存在 kind=outline 的工件",
                data={"hint": "先跑 writer:plan 生成 outline，再跑 writer:draft"},
            )

        sections = [s for s in (outline.payload.get("sections") or []) if isinstance(s, dict)]
        if not sections:
            return self.report(
                ctx,
                ok=False,
                summary="大纲里没有任何章节，无法起草",
                error="outline.payload['sections'] 为空",
            )

        topic = str(outline.payload.get("topic") or board.project.title or "未命名论文")
        word_plan = outline.payload.get("word_plan") or {}

        meta = board.project.meta
        writing_brief = meta.get("writing_brief") if isinstance(meta.get("writing_brief"), dict) else {}
        # 章节选择：作者可以只写某几章 / 跳过某几章。
        sections, excluded = selected_sections(sections, writing_brief)
        if excluded:
            ctx.say(f"ℹ 按你的选择跳过 {len(excluded)} 章：{'、'.join(clean_title(s.get('title')) for s in excluded)}", 0.28)

        # -- 相关性关键词：题目 + 写作要求 --
        #
        # 用来判断"检索到的证据跟我的题目到底有没有关系"。没有这道闸，
        # 换一个选题继续跑，向量检索**照样会返回 top-k**（库里没有相关文献时
        # 它返回的就是无关文献），于是产出的是"引用着一堆无关文献、看起来完整"的稿子。
        keywords = extract_keywords(
            str(writing_brief.get("topic") or topic),
            str(writing_brief.get("requirement") or ""),
        )
        if keywords.phrases:
            ctx.say(f"🔎 相关性关键词：{'、'.join(keywords.phrases[:6])}", 0.29)

        #: 作者的写作要求。逐单元注入提示词——它常常包含证据无法体现的硬约束
        #: （"必须做敏感性分析""不要用二手数据"），漏掉它整章都会跑偏。
        requirement = str(writing_brief.get("requirement") or "").strip()
        requirement_block = (
            f"\n**作者的写作要求（必须遵守）：**\n{requirement}\n" if requirement else ""
        )

        # 全局证据池：编号跨全篇唯一。这是修复"每章都从 [1] 开始"的关键。
        pool = EvidencePool.load(meta)
        pool_size_before = len(pool)
        ledger: dict[str, list[dict]] = dict(meta.get("writing_evidence") or {})
        base_version = board.draft.version if board.draft else 0
        #: 累积的整篇正文。以黑板现有稿件为起点，这样多轮起草不会丢掉已写的章节。
        accumulated = board.draft_text if board.draft else ""

        # -- 还原编号空间：让"起草 → 定稿"这条链可以重复跑 ------------------
        #
        # ``writer:references`` 定稿时会把角标从**证据池编号**改写成连续编号。
        # 如果在那之后再跑一次起草（上次因余额/网络中断而重跑，是很常见的情形），
        # 旧章节带着"最终编号"、新章节带着"池编号"，一份稿子里混进了两套编号空间——
        # 同一个 ``[7]`` 可能指向两条不同证据，而**外观上完全看不出来**。
        #
        # 所以这里先用 ``references_final.mapping`` 的逆映射把稿子还原回池编号空间，
        # 定稿时会重新编号。还原是纯文本变换，映射表就在 meta 里，可核对。
        accumulated, restored_n = unfinalize_text(accumulated, meta)
        if restored_n:
            ctx.say(f"♻ 已把上次定稿的角标还原回证据池编号（{restored_n} 处），本次起草后重新编号", 0.15)
            board.record(
                ctx.stage,
                ctx.round,
                self.name,
                "unfinalize",
                f"还原 {restored_n} 处角标编号",
            )

        searcher = SearchFactory(
            ctx.settings.peers.rag_base,
            timeout=min(120.0, float(getattr(ctx.settings.peers, "timeout", 120.0) or 120.0)),
        )
        # 补齐证据的作者/年份/题名，供定稿生成参考文献表。
        # 取不到（服务异常）时不阻断写作：文献表会少字段，但会在报告里逐条标出来。
        doc_index = searcher.doc_index() if hasattr(searcher, "doc_index") else {}
        if doc_index:
            pool.enrich(doc_index)

        # 按蓝图取同领域真实段落作示例（"框架 + 真实写法"结合）。
        # 三类章节各取一次，整轮复用——示例的用途是体现语体，不必逐章重取。
        example_blocks: dict[str, str] = {}
        example_notes: dict[str, str] = {}
        for bp in (INTRODUCTION, LITERATURE_REVIEW, BODY):
            exs, note = fetch_examples(
                ctx.settings.peers.rag_base,
                bp.example_queries,
                max_examples=2,
                timeout=min(60.0, float(getattr(ctx.settings.peers, "timeout", 60.0) or 60.0)),
            )
            example_blocks[bp.kind] = format_examples(exs)
            example_notes[bp.kind] = note
        ctx.say("示例检索：" + "；".join(f"{k}→{v}" for k, v in example_notes.items()), 0.25)

        #: 本次真正落盘的章节（内容与上一版一致的不写，避免版本链被空快照撑爆）
        written_done: list[str] = []
        skipped_unchanged: list[str] = []
        skipped: list[dict[str, str]] = []
        failed: list[dict[str, str]] = []
        removed_citations: list[dict[str, Any]] = []
        suspicious_citations: list[dict[str, Any]] = []
        gap_findings: list[dict[str, Any]] = []
        duplicates_dropped: list[dict[str, Any]] = []
        unit_log: list[dict[str, Any]] = []
        #: 写作质量自查命中（提示词里的要求 → 检查器的实测结果）。
        all_quality_hits: list[dict[str, Any]] = []
        total_citations = 0
        total_removed = 0
        total_words = 0
        issues: list[Issue] = []

        candidates = [s for s in sections if clean_title(s.get("title"))]
        #: 模型服务的致命错误（余额/密钥）。一旦命中就停下——继续调用只会白花钱、
        #: 白等时间，还把同一个原因重复记成 N 条失败，把真正的信息淹掉。
        fatal_reason = ""
        #: 进度基数——写作占整条流水线的 0.3~0.8 段。
        for idx, sec in enumerate(candidates):
            if fatal_reason:
                break
            title = clean_title(sec.get("title"))

            # -- 边界一：结论/创新点这类章节不代写 ------------------------
            if is_author_only(title):
                skipped.append(
                    {
                        "title": title,
                        "reason": "结论、创新点、展望属于作者的学术判断，writer:draft 不代写",
                    }
                )
                ctx.say(f"⏭ 跳过「{title}」：须由作者自己写", 0.3 + 0.5 * idx / max(1, len(candidates)))
                continue

            ctx.say(f"✍ 起草「{title}」", 0.3 + 0.5 * idx / max(1, len(candidates)))

            # 蓝图：按章节类型**和论文类型**选构造规范。技术类（工科/实验）的引言
            # 走六段式（局限→挑战→模块→贡献成链），其余走 CARS 三语步——
            # 这条分流是按**研究方法**分的，不是按学科名分（见 writing/__init__.py）。
            bp = blueprint_for(title, str(writing_brief.get("genre") or ""))
            chapter_words = _num(word_plan.get(title), 0)
            units = plan_units(sec, bp, chapter_words)

            # -- 闸门一：先检索证据（按章一次，多检索词合并）--------------
            points = [str(p) for p in (sec.get("points") or []) if str(p).strip()]
            queries = [f"{topic} {title}"]
            for sub in [u["heading"] for u in units][:3]:
                queries.append(f"{topic} {sub}")
            queries.extend(list(bp.example_queries[:2]))
            chapter_indices: list[int] = []
            search_errors: list[str] = []
            for q in queries:
                hits, err = searcher.search(q, k=SEARCH_K)
                if err:
                    search_errors.append(f"{q}: {err}")
                    continue
                added, _dup = pool.add_hits(hits)
                for e in added:
                    if e.index not in chapter_indices:
                        chapter_indices.append(e.index)

            chapter_entries = [e for e in (pool.get(i) for i in chapter_indices) if e is not None]

            # -- 闸门〇：证据必须与本题相关 ---------------------------------
            #
            # 这一步在"有没有证据"之前：**有证据不等于有相关证据**。向量检索永远
            # 返回 top-k，库里没有相关文献时它返回的是无关文献。实测标定：
            # 数据资产库上问「遥感图像语义分割」照样返回 10 条，其中 8 条完全不相关。
            # 不拦的话，产出是一篇引用着无关文献、看起来完整的论文——比写不出来危险得多。
            relevance = judge_entries(chapter_entries, keywords)
            if chapter_entries and not relevance.usable:
                note = (
                    f"「{title}」的知识库检索结果与本题不相关，未生成内容："
                    f"检索到 {relevance.total} 条，其中只有 {len(relevance.relevant)} 条与题目相关"
                    f"（命中词：{'、'.join(relevance.matched_terms[:6]) or '无'}）"
                )
                failed.append({"title": title, "reason": note})
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.irrelevant_evidence",
                        severity=Severity.BLOCKER,
                        message=note,
                        location={"section": title},
                        evidence=f"题目关键词：{'、'.join(keywords.phrases[:8])}；"
                                 f"检索命中词：{'、'.join(relevance.matched_terms[:8]) or '（无）'}",
                        detected_by="rules:writer_draft_relevance",
                        confidence=1.0,
                        # 补资料是作者的事：系统既不知道作者手头有什么，也不能编。
                        fixable=False,
                        fix_hint=(
                            "本地知识库里没有这个选题的材料，所以这一章写不了有依据的内容。"
                            "请把该主题的文献放进知识库后重跑（见 `kb add` 命令）。"
                            "**不要指望系统用无关文献凑出一章**——那正是不该做的事。"
                        ),
                        status="needs_author",
                    )
                )
                ctx.say(f"⛔ 「{title}」证据与本题不相关，跳过（{len(relevance.relevant)}/{relevance.total} 相关）", -1.0)
                continue
            if relevance.irrelevant:
                ctx.say(
                    f"   「{title}」剔除 {len(relevance.irrelevant)} 条与本题无关的检索结果，"
                    f"保留 {len(relevance.relevant)} 条",
                    -1.0,
                )
                chapter_entries = list(relevance.relevant)

            if not chapter_entries:
                # 没有证据就不生成断言。这一条比"多写点内容"重要得多，
                # 所以这里连模型都不调用——调用它只会得到一段看似专业的空话。
                note = (
                    f"「{title}」没有检索到任何可用证据，未生成任何内容"
                    + (f"（检索失败：{search_errors[0]}）" if search_errors else "")
                )
                failed.append({"title": title, "reason": note})
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.needs_source",
                        severity=Severity.MAJOR,
                        message=note,
                        location={"section": title},
                        evidence=f"检索词：{' / '.join(queries)}；命中 0 条",
                        detected_by="rules:writer_draft",
                        confidence=1.0,
                        fixable=False,
                        fix_hint=(
                            f"请为该章补充资料（类型：{sec.get('needs') or section_kind(title)}）"
                            "后重新入库，再重跑规划与起草。没有资料时这一章无法生成有依据的内容。"
                        ),
                        status="needs_author",
                    )
                )
                continue

            ctx.say(
                f"   「{title}」证据 {len(chapter_entries)} 条 → {len(units)} 个写作单元",
                0.3 + 0.5 * idx / max(1, len(candidates)),
            )

            blueprint_block = bp.as_prompt_block()
            examples_block = example_blocks.get(bp.kind, "")

            # -- 逐单元生成 ------------------------------------------------
            chapter_body = ""
            chapter_used: list[EvidenceEntry] = []
            #: 真正被角标引用到的编号。台账的 used 只能从这里来——
            #: "把证据摆进提示词"不等于"这句话有依据"，两者混同会让台账失去意义。
            cited_indices: set[int] = set()
            heading_open = ""
            section_failed = False

            for u_i, unit in enumerate(units):
                unit_heading = unit["heading"]
                evidence = pick_evidence(chapter_entries, f"{unit_heading} {unit.get('focus') or ''}")
                if not evidence:
                    continue
                for e in evidence:
                    if e.index not in {x.index for x in chapter_used}:
                        chapter_used.append(e)

                previous = ""
                if u_i > 0 and chapter_body.strip():
                    previous = (
                        "前文（**不要重复以下内容，只需自然承接**）：\n"
                        + chapter_body.strip()[-TAIL_CHARS:]
                    )
                needs = ""
                if unit.get("focus"):
                    needs = f"本部分侧重：{unit['focus']}\n"

                prompt = DRAFT_USER_TEMPLATE.format(
                    topic=topic,
                    section=title,
                    unit=unit_heading,
                    part=unit["part"],
                    parts=unit["parts"],
                    needs=needs,
                    requirement=requirement_block,
                    words=unit["words"],
                    evidence=format_evidence([e.as_dict() for e in evidence]),
                    examples=(f"\n{examples_block}\n" if examples_block else ""),
                    blueprint=f"\n{blueprint_block}\n",
                    previous=(f"\n{previous}\n" if previous else ""),
                )
                try:
                    raw_text = ctx.ask_model(
                        f"writer:draft:{title}#{u_i + 1}",
                        [
                            {"role": "system", "content": DRAFT_SYSTEM},
                            {"role": "user", "content": prompt},
                        ],
                        temperature=0.4,
                        max_tokens=_max_tokens_for(int(unit["words"])),
                    )
                except Exception as exc:  # noqa: BLE001 - 单单元失败不该毁掉整次起草
                    reason = f"{type(exc).__name__}: {exc}"
                    failed.append({"title": f"{title}／{unit_heading}({unit['part']})", "reason": reason})
                    if is_fatal_llm_error(exc):
                        # 致命错误：记录一次、退出所有循环，不再对剩余单元重试。
                        fatal_reason = reason
                        section_failed = True
                        break
                    issues.append(
                        Issue(
                            id=new_id("iss"),
                            kind="writing.needs_source",
                            severity=Severity.MINOR,
                            message=f"「{title}／{unit_heading}」草稿生成失败：{reason}",
                            location={"section": title, "unit": unit_heading},
                            evidence=reason,
                            detected_by="rules:writer_draft",
                            confidence=1.0,
                            fixable=False,
                            fix_hint="检查模型配置与额度后重跑起草；已生成的章节不会丢失。",
                            status="needs_author",
                        )
                    )
                    section_failed = True
                    continue

                # -- 闸门二：角标校验（模型会编造编号，代码不采信它）-----
                check = validate_citations(raw_text, [e.index for e in evidence])
                unit_text = str(check["text"]).strip()
                cited_indices.update(int(i) for i in check["ok_indices"])
                total_removed += int(check["removed"])
                for item in check["removed_citations"]:
                    removed_citations.append({"section": title, "unit": unit_heading, **item})
                for item in check["suspicious"]:
                    removed_citations.append(
                        {
                            "section": title,
                            "unit": unit_heading,
                            "marker": item["marker"],
                            "indices": item["dropped"],
                            "context": item["context"],
                        }
                    )
                    suspicious_citations.append({"section": title, **item})

                # -- 闸门三：跨单元重复句清理 ---------------------------
                unit_text, dups = drop_repeated_sentences(chapter_body, unit_text)
                for d in dups:
                    duplicates_dropped.append({"section": title, "unit": unit_heading, "sentence": d[:120]})

                # -- 闸门四：模型自认的空白 → 交还作者 -------------------
                for g in find_gaps(unit_text):
                    gap_findings.append({"section": title, "unit": unit_heading, "gap": g})
                    issues.append(
                        Issue(
                            id=new_id("iss"),
                            kind="writing.needs_source",
                            severity=Severity.MINOR,
                            message=f"「{title}／{unit_heading}」有一处内容需要作者补充依据：{g[:80]}",
                            location={"section": title, "find": "【待作者补充"},
                            evidence=g[:200],
                            detected_by="rules:writer_draft",
                            confidence=0.9,
                            fixable=False,
                            fix_hint=(
                                "这是模型明确指出没有依据的地方。请补上对应的数据、文献或案例；"
                                "如果确实拿不到，请改写或删除这句话——不要保留没有依据的断言。"
                            ),
                            status="needs_author",
                        )
                    )

                if not unit_text:
                    continue

                # 同一小节的多个部分拼在同一标题下；新小节另起标题。
                # 单元标题与章标题相同时不另起小标题（见 plan_units 的说明）。
                if unit_heading != heading_open:
                    head = "" if unit.get("is_chapter_heading") else f"## {unit_heading}\n\n"
                    chapter_body = (chapter_body.rstrip() + f"\n\n{head}" + unit_text).strip()
                    heading_open = unit_heading
                else:
                    chapter_body = (chapter_body.rstrip() + "\n\n" + unit_text).strip()

                unit_log.append(
                    {
                        "section": title,
                        "unit": unit_heading,
                        "part": unit["part"],
                        "parts": unit["parts"],
                        "focus": unit.get("focus") or "",
                        "words_target": unit["words"],
                        "words_actual": len(re.sub(r"\s", "", unit_text)),
                        "evidence": [e.index for e in evidence],
                        "citations_kept": int(check["kept"]),
                        "citations_removed": int(check["removed"]),
                    }
                )

            if fatal_reason:
                # 只记一条。逐单元各记一条会让报告里堆满同一个原因，
                # 而作者真正需要知道的是"从哪一刻起写不下去了、为什么"。
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.llm_unavailable",
                        severity=Severity.BLOCKER,
                        message=(
                            f"模型服务不可用（{fatal_reason[:160]}），已于「{title}」处停止后续生成。"
                            "已完成的小节保留在稿件里，未写的小节需要恢复服务后重跑起草。"
                        ),
                        location={"section": title},
                        evidence=fatal_reason[:200],
                        detected_by="rules:writer_draft",
                        confidence=1.0,
                        fixable=False,
                        fix_hint=(
                            "多为账户余额不足或 API Key 失效。请充值或更换密钥后重跑："
                            "`run -p <项目id> -s draft`（已写章节不会丢失，未写的会补齐）。"
                        ),
                        status="needs_author",
                    )
                )

            if not chapter_body.strip():
                if not section_failed:
                    failed.append({"title": title, "reason": "所有写作单元都未产出内容"})
                continue

            # 保留角标的数量**从最终正文里数**，而不是把各单元的数字加起来：
            # 跨单元去重可能整段删掉一个单元的重复内容，加起来会把已经不在稿子里的
            # 角标也算上，于是报告里的数字与作者在正文里看到的对不上。
            chapter_kept = len(list(cite_litter(chapter_body)))
            total_citations += chapter_kept

            # -- 闸门五：写作质量自查 -------------------------------------
            # 写完立刻用**同一套可检验标准**量一遍，把不达标的具体位置报给作者。
            # 这是"提示词里提的要求必须能被检查出来"的落实点：
            # 只提要求不检查，等于给模型一个愿望。
            #
            # 跑的是"蓝图专属 + 通用"两部分。通用部分不能省——文献综述内容
            # 常散落在被归类为"正文"的小节里，若只跑蓝图专属检查器，
            # 「一篇论文半句话」这一最要紧的检查就根本不会执行（实测踩过）。
            quality_hits: list[dict[str, Any]] = []
            for checker_name in checkers_for(bp.kind):
                fn = _WRITING_CHECKERS.get(checker_name)
                if fn is None:
                    continue
                try:
                    found = fn(chapter_body, ctx)
                except Exception:  # noqa: BLE001 - 自查失败不该毁掉起草
                    continue
                for iss in found:
                    quality_hits.append(
                        {
                            "section": title,
                            "checker": checker_name,
                            "kind": iss.kind,
                            "severity": iss.severity.value,
                            "message": iss.message,
                        }
                    )
                    issues.append(
                        Issue(
                            id=new_id("iss"),
                            kind=iss.kind,
                            severity=iss.severity,
                            message=f"「{title}」{iss.message}",
                            location={"section": title, **(iss.location or {})},
                            evidence=iss.evidence,
                            detected_by=iss.detected_by,
                            confidence=iss.confidence,
                            fixable=False,
                            fix_hint=iss.fix_hint,
                            status="needs_author",
                        )
                    )
            if quality_hits:
                all_quality_hits.extend(quality_hits)
                ctx.say(f"⚠ 「{title}」写作质量自查发现 {len(quality_hits)} 项不达标", -1.0)

            # 证据台账：验证环节判断"论断有没有依据"就靠它。
            # 存 text 是有意的——只存 citation 无法判断某句话到底是不是这段材料支持的。
            # ``used`` 只对**真被角标引用**的编号为 True：这个字段会被后续核查当作
            # "这句话有出处"的凭据，把"摆进过提示词"也算成 used 等于伪造依据。
            ledger[title] = [
                {**e.as_dict(), "used": e.index in cited_indices}
                for e in chapter_entries
            ]

            notes = [f"（检索报错：{search_errors[0]}）"] if search_errors else []
            merged_text = merge_section(accumulated, title, chapter_body + ("\n\n" + " ".join(notes) if notes else ""))
            if merged_text == accumulated and accumulated:
                # 内容完全一样就不写新版本：黑板是版本链，重复写入一个"没变化"的
                # 版本会让作者在回退时看到一串无意义的快照。
                skipped_unchanged.append(title)
                written_done.append(title)
                continue
            accumulated = merged_text
            chapter_words_actual = len(re.sub(r"\s", "", chapter_body))
            total_words += chapter_words_actual
            art = board.put_artifact(
                kind="draft",
                text=accumulated,
                created_by=self.name,
                stage=ctx.stage,
                run_id=ctx.run_id,
                note=(
                    f"起草「{title}」：{len(units)} 个写作单元，"
                    f"{len(chapter_entries)} 条证据（摆入提示词 {len(chapter_used)} 条），"
                    f"{chapter_words_actual} 字（目标 {chapter_words or '未定'}），"
                    f"保留角标 {chapter_kept} 处，"
                    f"删除越界角标 {sum(u['citations_removed'] for u in unit_log if u['section'] == title)} 处"
                ),
            )
            written_done.append(title)
            board.record(
                ctx.stage,
                ctx.round,
                self.name,
                "draft_section",
                f"{title} → 稿件 v{art.version}（{chapter_words_actual} 字）",
            )

        # -- 收尾：把池子与台账落盘 --------------------------------------
        #
        # **再补一次元数据**：开头那次 enrich 只覆盖了 retrieve 阶段放进池子的证据，
        # 起草过程中逐章新检索到的条目还没补。漏掉这一步的后果很具体——定稿生成的
        # 49 条文献里 49 条都显示"缺作者/题名/年份"，而知识库里明明有这些字段；
        # 文献表会退回到 RAG 那句拼好的 citation 串，里面还混着作者单位与邮编。
        # enrich 是纯字典查找，重复调用没有代价。
        if doc_index:
            pool.enrich(doc_index)
        pool.save(meta)
        meta["writing_evidence"] = ledger

        if removed_citations:
            issues.append(
                Issue(
                    id=new_id("iss"),
                    kind="writing.fabricated_citation",
                    severity=Severity.MAJOR,
                    message=(
                        f"模型在正文里使用了 {total_removed} 个不存在的引用编号"
                        f"（涉及 {len(removed_citations)} 处角标），"
                        "已全部删除；这些位置现在没有引用，需要作者确认是否有依据"
                    ),
                    location={"sections": sorted({c["section"] for c in removed_citations})},
                    evidence="；".join(c["marker"] for c in removed_citations[:6]),
                    detected_by="rules:writer_draft_citation",
                    confidence=1.0,
                    # 补一条真实引用是作者的判断，不能让循环自动"找一条文献填上"。
                    fixable=False,
                    fix_hint=(
                        "请逐一核对这些位置：如果确有依据，从证据台账里挑正确的编号重新标注；"
                        "如果没有依据，请删掉或改写该句。编造引用属于学术不端，必须人工确认。"
                    ),
                    status="needs_author",
                )
            )

        if duplicates_dropped:
            issues.append(
                Issue(
                    id=new_id("iss"),
                    kind="writing.duplicate_sentence",
                    severity=Severity.MINOR,
                    message=(
                        f"跨写作单元出现 {len(duplicates_dropped)} 处完全相同的长句，"
                        "已只保留首次出现（分单元写作时后一个单元重复前文是常见问题）"
                    ),
                    location={"sections": sorted({d["section"] for d in duplicates_dropped})},
                    evidence="；".join(d["sentence"][:60] for d in duplicates_dropped[:4]),
                    detected_by="rules:writer_draft_repeat",
                    confidence=1.0,
                    fixable=False,
                    fix_hint="若删掉后读起来有断裂，说明该处的承接需要作者重写一句，而不是重复原句。",
                    status="needs_author",
                )
            )

        added = board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)
        board.save()

        ok = bool(written_done) or not candidates
        summary_parts = [
            f"已起草 {len(written_done)} 章"
            + (f"（{'、'.join(written_done)}）" if written_done else "")
            + (f"，共 {total_words} 字" if total_words else ""),
        ]
        if fatal_reason:
            summary_parts.append(f"⚠ 模型服务不可用已熔断，剩余章节未生成：{fatal_reason[:120]}")
        if skipped_unchanged:
            summary_parts.append(f"其中 {len(skipped_unchanged)} 章内容未变化，未新增版本")
        if skipped:
            summary_parts.append(f"跳过 {len(skipped)} 章（{'、'.join(s['title'] for s in skipped)}，须作者自写）")
        if failed:
            summary_parts.append(f"未生成 {len(failed)} 章（{'、'.join(f['title'] for f in failed)}）")
        if removed_citations:
            summary_parts.append(f"删除越界角标 {total_removed} 处")
        if duplicates_dropped:
            summary_parts.append(f"去重长句 {len(duplicates_dropped)} 处")
        if gap_findings:
            summary_parts.append(f"待作者补充 {len(gap_findings)} 处")

        return self.report(
            ctx,
            ok=ok,
            summary="；".join(summary_parts),
            error="" if ok else "没有任何章节生成成功",
            issues=[i.id for i in issues],
            data={
                "sections_written": written_done,
                "sections_unchanged": skipped_unchanged,
                "sections_skipped": skipped,
                "sections_failed": failed,
                "sections_excluded_by_choice": [clean_title(s.get("title")) for s in excluded],
                "relevance_keywords": list(keywords.phrases),
                "llm_fatal": fatal_reason,
                "units": unit_log,
                "units_total": len(unit_log),
                "words_actual": total_words,
                "words_target": sum(_num(v, 0) for v in word_plan.values()) or 0,
                # 报告里报数而不是静默删除：作者必须知道模型试图编造过引用。
                # citations_removed = 被摘掉的**编号**个数（含 [1,9] 里只摘掉 9 的情况），
                # removed_markers = 涉及多少处角标。
                "citations_kept": total_citations,
                "citations_removed": total_removed,
                "removed_markers": len(removed_citations),
                "removed_detail": removed_citations,
                "suspicious_citations": suspicious_citations,
                "duplicates_dropped": len(duplicates_dropped),
                "gaps": gap_findings,
                # 写作质量自查：提示词里提的每一条要求，写完都用检查器量过一遍，
                # 命中明细在这里。作者据此知道"哪一章、哪条标准没达到"。
                "writing_quality_hits": all_quality_hits,
                "writing_quality_summary": _summarize_quality(all_quality_hits),
                "examples_used": example_notes,
                "evidence_pool": {"before": pool_size_before, "after": len(pool)},
                "evidence_ledger_sections": sorted(ledger.keys()),
                "sections_in_draft": split_sections(board.draft_text),
                "draft_version_before": base_version,
                "draft_version_after": board.draft.version if board.draft else base_version,
                "new_issues": added,
                "rag_failures": searcher.failures,
            },
            seconds=time.time() - t0,
        )
