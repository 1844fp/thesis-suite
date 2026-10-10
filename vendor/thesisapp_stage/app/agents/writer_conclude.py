"""能力③「论文写作助手」——结论/总结（writer:conclude）。

## 为什么结论要单独一个智能体，而不是交给 writer:draft

结论不是"又一个章节"。它是**对全文已经论证过的判断做汇总**，所以它的正确性判据
与其它章完全不同：其它章看"有没有依据"，结论看"**有没有超出正文**"。
两套判据混在一个智能体里，只会两边都做不干净。

这里守住三条硬边界：

1. **只能汇总正文。** 提示词里给的不是知识库检索结果，而是**从作者已写的正文里
   抽出来的论断清单**。结论里出现正文没有的数字，会被逐句比对抓出来并就地在句中
   标注 ``【待作者补充：…】``——作者一眼就能看到哪里越了界。
2. **不得引入新引用。** 角标的可选范围是"正文里已经引用过的编号"，不是整个证据池。
   结论里冒出一条正文没引过的文献，说明这条结论的支撑不在正文里，那它就不该出现。
3. **不写创新点。** 出现"本文的创新之处""首次提出""填补了空白"即报致命问题。
   创新点是作者的学术主张，任何环节都不代写。

## 产出物是"结构草稿"，不是"定稿"

整章生成后统一标记 ``needs_author``：每一条结论都要作者逐条确认"这确实是我的意思"。
这不是免责声明，而是这套工具唯一诚实的做法——结论是署名者要为之负责的话，
工具可以帮他把话说完整、说准确，但不能替他判断话说得对不对。
"""

from __future__ import annotations

import re
import time
from typing import Any, Sequence

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage, new_id
from ..writing.citations import EvidenceEntry, EvidencePool, fact_numbers
from .writer_common import (
    clean_title,
    find_gaps,
    is_fatal_llm_error,
    is_structure_draft,
    merge_section,
    validate_citations,
)

#: 每章最多抽多少条论断进清单。清单越长，模型越容易把某一条当成"重点"而漏掉别的，
#: 也会挤占提示词预算；六条足以覆盖一章的主要判断。
CLAIMS_PER_CHAPTER = 6
#: 整章结论的目标字数上限（结论通常占全文 8~12%，不该写成长篇复述）。
MAX_CONCLUDE_WORDS = 4000
MAX_TOKENS = 6000

#: 判断句的标记：出现这些，说明这句话是作者下的判断，而不是摆事实。
_JUDGMENT = re.compile(
    r"表明|说明|意味着|显示|可见|因此|因而|据此|得出结论|研究发现|本文发现|"
    r"原因在于|关键在于|表明在|证实|支持了|不支持|存在显著|不存在"
)
_SENT = re.compile(r"(?<=[。！？])")
_PARA = re.compile(r"\n\s*\n|\n")
_CHAPTER_HEAD = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)
_SUB_HEAD = re.compile(r"^##[ \t]+(.+?)[ \t]*$", re.M)

CONCLUDE_SYSTEM = (
    "你是中文学位论文的结论撰写助手。你的任务是把作者**已经写出来的正文**"
    "浓缩成结论，而不是提出任何新的判断。"
    "你绝不引入正文没有出现过的数字、年份、比例或文献引用。"
    "你绝不写创新点声明，也不评价本研究的价值与意义。"
    "你写的每一句话都必须能在正文里找到出处。"
)

CONCLUDE_USER_TEMPLATE = """请依据下面这份"正文已确立的内容"，写出论文的结论章。

论文标题：{topic}
本章标题：{section}
本章小节：{subs}
目标字数：约 {words} 字
{requirement}
**本文正文已经确立的内容（这是你唯一可以依据的材料）**：
{digest}

**正文中已经引用过的文献编号（若需引用，只能从中选；结论不应引入新引用）**：
{citations}
{previous}
写作要求（全部是硬性要求）：
1. 只汇总上面列出的正文内容。**不得引入正文没有的数字、年份、比例、文献**。
   如果需要一个新的数字才能把话说清楚，说明这条结论还不成立——请改成不需要
   该数字的表述，而不是把数字写上去。
2. 每条主要结论后面标明它依据的章节，形如「（见第 4 章）」或「（见 3.2 节）」。
3. **不要写创新点**：不得出现「本文的创新之处」「首次提出」「填补了……空白」
   「开创性」这类表述。创新点由作者自己写。
4. 不要用「证明了」「充分说明」「具有重大意义」这类超出证据强度的措辞。
   用「本研究的证据支持」「表明……的可能性」这类与证据相称的说法。
5. 局限要写到具体维度（样本的时间/地域范围、变量度量的近似程度、因果识别的限制、
   结论不能外推到什么情形），不要写「存在一定局限性」「有待完善」这种空话。
6. 展望要具体可行（如「将样本扩展到非上市企业以检验外部效度」），
   不要写「有待进一步研究」。
7. 不要重复摘要与引言的表述；不要出现「综上所述」这类空转的过渡语。
8. 按上面给出的小节顺序组织，每个小节写成一个二级标题（以 `## ` 开头）。
9. 不要写参考文献列表。

直接输出这一章的正文（Markdown，小节用 `## ` 开头）。
"""


def _split_chapters(body: str) -> list[tuple[str, str]]:
    """按 ``# 标题`` 切出正文章节。"""
    matches = list(_CHAPTER_HEAD.finditer(body or ""))
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out.append((m.group(1).strip(), body[m.end() : end].strip()))
    return out


def build_claim_digest(body: str, *, exclude: Sequence[str] = ()) -> tuple[str, dict[str, list[str]]]:
    """从正文里抽出各章的论断清单。

    返回 ``(给模型看的文本, {章节标题: [依据句]})``。第二个返回值用于事后核对
    "结论是不是真能追回正文"。

    抽取口径刻意简单：**每段的第一句**（主题句，按本项目的写作规范它就该是这一段
    的判断）＋**含判断标记的句子**。不用模型去"提炼"——那一步一旦出错，
    后面所有核对都失去基准，而基准必须是逐字来自稿件的原文。
    """
    excl = {clean_title(t) for t in exclude}
    lines: list[str] = []
    by_chapter: dict[str, list[str]] = {}
    for title, chunk in _split_chapters(body):
        if clean_title(title) in excl:
            continue
        subs = [s.strip() for s in _SUB_HEAD.findall(chunk)]
        claims: list[str] = []
        for para in _PARA.split(chunk):
            p = para.strip()
            if not p or p.startswith("#"):
                continue
            sents = [s.strip() for s in _SENT.split(p) if s.strip()]
            if not sents:
                continue
            # 主题句：段落首句。
            claims.append(sents[0])
            for s in sents[1:]:
                if _JUDGMENT.search(s):
                    claims.append(s)
        # 去重保序 + 截断
        seen: set[str] = set()
        picked: list[str] = []
        for c in claims:
            key = c[:40]
            if key in seen or len(c) < 12:
                continue
            seen.add(key)
            picked.append(c)
            if len(picked) >= CLAIMS_PER_CHAPTER:
                break
        by_chapter[title] = picked
        lines.append(f"### {title}")
        if subs:
            lines.append("小节：" + "；".join(subs[:8]))
        for c in picked:
            lines.append(f"- {c}")
        lines.append("")
    return "\n".join(lines).strip(), by_chapter


def _mark_ungrounded(text: str, body_numbers: set[str], *, section: str) -> tuple[str, list[dict]]:
    """给"正文里没有的数字"就地加标注，返回 ``(新文本, 命中明细)``。

    为什么保留句子而只加标注、不像角标那样直接删：角标是**溯源标识**，指错了必须删
    （伪造的溯源比没有溯源更危险）；而数字是**句子内容的一部分**，删掉数字会把句子
    改得不成话，作者反而看不出原本想说什么。所以这里保留原句、就地标注，
    并把命中明细同时收成问题——**既不静默放过，也不擅自改写**。
    """
    hits: list[dict] = []
    out: list[str] = []
    for sent in re.split(r"(?<=[。！？])", text or ""):
        nums = [n for n in fact_numbers(sent) if n not in body_numbers]
        if not nums:
            out.append(sent)
            continue
        hits.append({"section": section, "sentence": sent.strip()[:160], "numbers": nums})
        note = f"【待作者补充：本句的数字「{'、'.join(nums[:3])}」未在正文中出现，请核对或删除】"
        # 插在句末标点之后，保证句子本身一字未改。
        m = re.search(r"[。！？]", sent)
        if m:
            out.append(sent[: m.end()] + note + sent[m.end() :])
        else:
            out.append(sent + note)
    return "".join(out), hits


class WriterConcludeAgent(Agent):
    """依据正文汇总结论：不引入正文没有的事实，不写创新点，整章交作者确认。"""

    name = "writer:conclude"
    description = (
        "依据已写正文汇总结论/总结章：数字必须能在正文找到，不得引入新引用，"
        "不写创新点，整章标记为需作者逐条确认"
    )
    stage = Stage.DRAFT.value
    capabilities = ("write", "conclude")
    uses_model = True

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        board = ctx.board
        outline = board.head("outline")
        if outline is None:
            return self.report(
                ctx,
                ok=False,
                summary="没有找到写作大纲，请先运行写作规划（writer:plan）",
                error="board 中不存在 kind=outline 的工件",
            )

        sections = [s for s in (outline.payload.get("sections") or []) if isinstance(s, dict)]
        targets = [s for s in sections if is_structure_draft(clean_title(s.get("title")))]
        if not targets:
            return self.report(
                ctx,
                ok=True,
                summary="大纲里没有结论/总结章，无需生成（创新点与致谢本来就不代写）",
                data={"skipped": True, "reason": "no_conclusion_section"},
                seconds=time.time() - t0,
            )

        body = board.draft_text or ""
        if not body.strip():
            return self.report(
                ctx,
                ok=False,
                summary="稿件正文为空，无法汇总结论：结论只能依据正文生成",
                error="draft 为空",
                data={"hint": "先跑 writer:draft 写出正文，再跑 writer:conclude"},
            )

        topic = str(outline.payload.get("topic") or board.project.title or "未命名论文")
        word_plan = outline.payload.get("word_plan") or {}
        brief = board.project.meta.get("writing_brief") if isinstance(board.project.meta.get("writing_brief"), dict) else {}
        requirement = str((brief or {}).get("requirement") or "").strip()
        target_titles = [clean_title(s.get("title")) for s in targets]

        # ---- 1) 论断清单：只从正文里逐字抽取，不做"提炼" ------------------
        digest, by_chapter = build_claim_digest(body, exclude=target_titles)
        ctx.say(
            f"📝 结论汇总：依据 {len(by_chapter)} 章、共 {sum(len(v) for v in by_chapter.values())} 条论断",
            0.2,
        )

        # ---- 2) 可用引用 = 正文里已经用过的编号 ---------------------------
        pool = EvidencePool.load(board.project.meta)
        ledger = board.project.meta.get("writing_evidence") or {}
        body_used: list[EvidenceEntry] = []
        for entries in ledger.values():
            for e in entries or []:
                if not isinstance(e, dict) or not e.get("used"):
                    continue
                entry = pool.get(int(e.get("index") or 0))
                if entry is not None and entry.index not in {x.index for x in body_used}:
                    body_used.append(entry)
        valid_indices = {e.index for e in body_used}
        citations_block = (
            "\n".join(f"[{e.index}] {e.citation or e.title or '（来源未标注）'}" for e in body_used[:40])
            or "（正文里没有引用任何文献，因此结论中也不要出现引用角标）"
        )
        body_numbers = set(fact_numbers(body))

        accumulated = body
        written: list[str] = []
        failed: list[dict[str, str]] = []
        ungrounded: list[dict[str, Any]] = []
        removed_citations: list[dict[str, Any]] = []
        gap_findings: list[dict[str, Any]] = []
        total_words = 0
        issues: list[Issue] = []
        model_error = ""

        for i, sec in enumerate(targets):
            if is_fatal_llm_error(model_error):
                break
            title = clean_title(sec.get("title"))
            subs = [clean_title(s) for s in (sec.get("subsections") or []) if clean_title(s)]
            words = min(MAX_CONCLUDE_WORDS, int((word_plan.get(title) or MAX_CONCLUDE_WORDS // 2)))
            words = max(800, words)

            ctx.say(f"🧾 撰写结论章「{title}」", 0.3 + 0.6 * i / max(1, len(targets)))
            prompt = CONCLUDE_USER_TEMPLATE.format(
                topic=topic,
                section=title,
                subs="；".join(subs) if subs else "（未指定，按「研究结论—研究不足—未来展望」组织）",
                words=words,
                requirement=(
                    f"\n**作者的写作要求（在结论里也要照顾到）：**\n{requirement}\n"
                    if requirement
                    else ""
                ),
                digest=digest or "（正文里没有抽取到明确的论断，请只写研究过程概述，不要下结论）",
                citations=citations_block,
                previous=(
                    "\n注意：本稿已包含其它章节，你只写这一章，不要重复正文里已有的段落。\n"
                    if written
                    else ""
                ),
            )
            try:
                raw = ctx.ask_model(
                    f"writer:conclude:{title}",
                    [
                        {"role": "system", "content": CONCLUDE_SYSTEM},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.3,
                    max_tokens=MAX_TOKENS,
                )
            except Exception as exc:  # noqa: BLE001 - 单章失败不该毁掉整次汇总
                reason = f"{type(exc).__name__}: {exc}"
                model_error = reason
                failed.append({"title": title, "reason": reason})
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.llm_unavailable" if is_fatal_llm_error(exc) else "writing.conclusion_failed",
                        severity=Severity.MAJOR,
                        message=f"「{title}」生成失败：{reason}",
                        location={"section": title},
                        evidence=reason,
                        detected_by="rules:writer_conclude",
                        confidence=1.0,
                        fixable=False,
                        fix_hint="检查模型配置与额度后重跑；结论章必须由作者确认，缺了它请自行补写。",
                        status="needs_author",
                    )
                )
                continue

            text = (raw or "").strip()
            if not text:
                failed.append({"title": title, "reason": "模型返回空内容"})
                continue

            # ---- 闸门一：不得引入正文没有的引用 ------------------------
            check = validate_citations(text, valid_indices)
            text = str(check["text"]).strip()
            for item in check["removed_citations"]:
                removed_citations.append({"section": title, **item})
            for item in check["suspicious"]:
                removed_citations.append(
                    {"section": title, "marker": item["marker"], "indices": item["dropped"],
                     "context": item["context"]}
                )
            if check["removed"]:
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.conclusion_new_citation",
                        severity=Severity.MAJOR,
                        message=(
                            f"「{title}」引用了正文里没有用过的文献编号"
                            f"（{check['removed']} 个），已删除：结论不得引入正文没有的引用"
                        ),
                        location={"section": title},
                        evidence="；".join(c["marker"] for c in check["removed_citations"][:6]),
                        detected_by="rules:writer_conclude",
                        confidence=1.0,
                        fixable=False,
                        fix_hint=(
                            "如果这条结论确有文献依据，应先把该文献引到正文相应位置；"
                            "否则删掉这处引用或整条结论。"
                        ),
                        status="needs_author",
                    )
                )

            # ---- 闸门二：不得引入正文没有的数字 ------------------------
            text, hits = _mark_ungrounded(text, body_numbers, section=title)
            for h in hits:
                ungrounded.append(h)
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.conclusion_new_number",
                        severity=Severity.BLOCKER,
                        message=(
                            f"「{title}」出现正文中没有的数字「{'、'.join(h['numbers'][:3])}」，"
                            "已就地标注待核对：结论不得引入正文没有的事实"
                        ),
                        location={"section": title, "find": h["numbers"][0]},
                        evidence=h["sentence"][:200],
                        detected_by="rules:writer_conclude",
                        confidence=1.0,
                        fixable=False,
                        fix_hint=(
                            "回到正文核对：正文里没有这个数就算错，改回正文的数值或删掉该句。"
                            "如果这条结论本来就需要一个正文没有的数字支撑，说明它还缺一步分析，"
                            "应补在正文而不是结论里。"
                        ),
                        status="needs_author",
                    )
                )

            # ---- 闸门三：创新点与夸大 --------------------------------
            for sent in re.split(r"(?<=[。！？])", text):
                m = re.search(
                    r"创新(?:点|之处)|首次提出|首创|填补(?:了)?[^。；]{0,12}空白|"
                    r"原创性(?:贡献|成果)|主要贡献在于",
                    sent,
                )
                if not m:
                    continue
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.conclusion_innovation_claim",
                        severity=Severity.BLOCKER,
                        message=(
                            f"「{title}」出现创新点声明「{m.group(0)}」：创新点是作者的学术主张，"
                            "必须由作者自己写，系统不代写"
                        ),
                        location={"section": title, "find": m.group(0)},
                        evidence=sent.strip()[:200],
                        detected_by="rules:writer_conclude",
                        confidence=1.0,
                        fixable=False,
                        fix_hint="删掉这句，改为陈述已经论证过的事实；创新点请作者自己补写。",
                        status="needs_author",
                    )
                )

            # ---- 闸门四：模型自认的空白 --------------------------------
            for g in find_gaps(text):
                gap_findings.append({"section": title, "gap": g})

            # ---- 逐章累积 ----------------------------------------------
            accumulated = merge_section(accumulated, title, text)
            chapter_words = len(re.sub(r"\s", "", text))
            total_words += chapter_words
            written.append(title)
            board.put_artifact(
                kind="draft",
                text=accumulated,
                created_by=self.name,
                stage=ctx.stage,
                run_id=ctx.run_id,
                note=(
                    f"结论汇总「{title}」：依据 {len(by_chapter)} 章论断，{chapter_words} 字，"
                    f"越界数字 {len(hits)} 处、越界引用 {check['removed']} 处（已逐条标注/删除）"
                ),
            )
            board.record(ctx.stage, ctx.round, self.name, "conclude_section",
                         f"{title} → 稿件 v{board.draft.version if board.draft else 0}（{chapter_words} 字）")

        # ---- 整章交作者确认 ---------------------------------------------
        if written:
            issues.append(
                Issue(
                    id=new_id("iss"),
                    kind="writing.conclusion_needs_confirm",
                    severity=Severity.MAJOR,
                    message=(
                        f"结论章（{'、'.join(written)}）是**依据正文自动汇总的结构草稿**，"
                        "每一条结论都需要你逐条确认「这确实是我的意思」后才算定稿"
                    ),
                    location={"sections": written},
                    evidence=f"依据 {len(by_chapter)} 章、{sum(len(v) for v in by_chapter.values())} 条正文论断汇总",
                    detected_by="rules:writer_conclude",
                    confidence=1.0,
                    # 结论是否成立是作者的判断，工具不改。
                    fixable=False,
                    fix_hint=(
                        "逐条核对三件事：①这条结论在正文里有没有依据（文中已标出依据章节，"
                        "可直接翻回去）；②措辞是否与证据强度相称（有没有把关联说成因果）；"
                        "③这是不是你想说的意思。确认后把本问题的状态改为已处理。"
                    ),
                    status="needs_author",
                )
            )

        added = board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)
        board.save()

        summary_parts = []
        if written:
            summary_parts.append(f"已汇总结论 {len(written)} 章（{'、'.join(written)}），共 {total_words} 字，待作者逐条确认")
        if failed:
            summary_parts.append(f"未生成 {len(failed)} 章（{'、'.join(f['title'] for f in failed)}）")
        if ungrounded:
            summary_parts.append(f"正文中没有的数字 {len(ungrounded)} 处已就地标注")
        if removed_citations:
            summary_parts.append(f"删除正文未引用的角标 {len(removed_citations)} 处")
        if not summary_parts:
            summary_parts.append("没有生成任何结论章")

        ok = bool(written) or not targets
        return self.report(
            ctx,
            ok=ok,
            summary="；".join(summary_parts),
            error="" if ok else (model_error or "结论章生成失败"),
            issues=[i.id for i in issues],
            data={
                "sections_written": written,
                "sections_failed": failed,
                "words_actual": total_words,
                "claim_digest_chapters": {k: len(v) for k, v in by_chapter.items()},
                "ungrounded_numbers": ungrounded,
                "removed_citations": removed_citations,
                "gaps": gap_findings,
                "body_citations_available": sorted(valid_indices),
                "new_issues": added,
                "needs_author_confirmation": bool(written),
            },
            seconds=time.time() - t0,
        )
