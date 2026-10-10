"""能力③「论文写作助手」——写作规划（writer:plan）。

这个智能体回答的问题是：**"这篇论文该分几章，以及每一章现在写得了吗？"**

它最有价值的产出其实不是大纲，而是**证据可得性报告**。学位论文写作最常见的
灾难不是"写得慢"，而是"先写了一章，才发现这一章要用的访谈数据根本拿不到"。
所以规划阶段先替作者把每章的可用证据数查一遍：证据为 0 的章节会明确标出来，
并产出一条 ``writing.missing_evidence``（``fixable=False``）——这句话的意思是
"这一章你现在写不了，因为知识库里没有材料"，而不是"再努力写写看"。

规划只做检索 + 判断，**不写任何正文**：正文里每一句有依据的话都必须当场带着
证据出台，那是 ``writer:draft`` 的职责。职责分开的好处是，作者可以只重跑规划
（便宜、快、可反复），而不牵动已经写好的段落。
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage, new_id
from ..writing import classify, word_share
from .writer_common import (
    SearchFactory,
    clean_title,
    evidence_entries,
    is_author_only,
    section_kind,
)

#: 每章检索多少条证据。6 是"够判断有没有材料"和"别把提示词撑爆"之间的折中：
#: 少于 3 条通常说明这一章材料薄，多于 6 条在规划阶段也看不出更多名堂。
SEARCH_K = 6

#: 单章的字数下限。低于这个数，章节写不出完整的论证（只剩两三句结论）。
MIN_SECTION_WORDS = 900
#: 单章的字数上限：再长就该拆章了。同时也是给"某章占比异常"设的护栏。
MAX_SECTION_WORDS = 12000

#: 默认章节规划。作者没给写作意图时用它，而不是拒绝干活——
#: 一份"基于通用结构的规划 + 每章证据情况"比一句"请先填写写作意图"对作者有用得多。
DEFAULT_SECTIONS: tuple[dict[str, Any], ...] = (
    {
        "title": "绪论",
        "focus": "研究背景、现实与理论问题、研究意义、研究思路与本文结构",
        "needs": "政策文件、行业统计数据、该领域公认的问题界定",
    },
    {
        "title": "文献综述",
        "focus": "梳理既有研究的主要观点、分歧与不足，指出本文的切入点",
        "needs": "近五年的代表性研究及其结论、被反复引用的经典文献",
    },
    {
        "title": "研究设计",
        "focus": "研究方法、数据来源与样本、变量定义与测量、模型或分析框架、研究假设",
        "needs": "所用方法的原始出处、数据来源说明、变量测量的既有依据",
    },
    {
        "title": "实证分析与结果",
        "focus": "描述性统计、主要回归或实验结果、稳健性检验、结果与假设的对照",
        "needs": "可对照的已有实证结论、方法与检验标准",
    },
    {
        "title": "结论与展望",
        "focus": "研究结论、理论与实践启示、局限与未来研究方向",
        "needs": "这一章必须作者自己写，系统不代写",
    },
)

#: 大纲提示词。刻意把"不写结论、不写创新点"写进提示词而不只是在代码里拦：
#: 模型知道边界才不会在大纲里塞一堆"本文的创新之处在于……"的要点。
OUTLINE_SYSTEM = (
    "你是中文学位论文的写作规划助手。你只做规划，不写正文。"
    "你的产出必须严格对齐作者能拿到的资料；资料不足的地方要如实标出来，"
    "不要用看似合理的章节标题掩盖材料缺口。"
)

OUTLINE_USER_TEMPLATE = """请为下面这篇学位论文制定写作大纲。

论文标题：{title}
写作意图（作者提供，可能不完整）：{brief}
目标字数：{word_target}
作者强调的重点：{focus}
{requirement}
**作者已在开题报告里拟定的章节与小节（若下方有内容，必须原样保留章节标题、
并按作者的小节组织，不得替换成你自己的框架）：**
{author_outline}

各章主题的现有资料情况（来自本地论文知识库的检索，这是你唯一可以依据的材料）：
{availability}

硬性要求：
1. 每一章给出 3~6 条要点，**每条要点写成一个"小节标题"**（12~25 字的名词性短语，
   如「数据资产确认条件的适用性分析」），不要写成整句话，更不要写正文段落。
   这些小节标题会被逐条展开成正文，所以它们必须彼此不重叠、合起来能覆盖整章。
2. 每一章指出"需要什么证据"（needs），写成作者能去查的东西（数据、文献、标准），
   不要写成"需要更多资料"这种空话。
3. **结论/总结章要保留在大纲里**（它由系统依据正文汇总生成，之后必须由作者逐条确认），
   但不要为它编造新的论断；只要给出它的小节划分即可（如「研究结论」「研究不足」「未来展望」）。
   **创新点、致谢、附录不要出现在大纲里**，它们必须由作者自己撰写。如果标题里隐含了
   这类内容，请省略并在 notes 里说明。
4. 章数控制在 {min_sections}~{max_sections} 章之间。作者已明确列出的章节必须保留。
5. 资料为 0 条的章节仍然保留在大纲里，但在 evidence_gaps 里如实写出缺什么。

只输出 JSON，结构如下：
{{
  "sections": [
    {{"title": "章节标题（不含章节号）",
      "points": ["小节标题1", "小节标题2", "小节标题3"],
      "needs": "这一章需要什么证据",
      "evidence_ok": true}}
  ],
  "evidence_gaps": ["哪一章缺哪一类资料"],
  "notes": "给作者的一句话说明"
}}
"""


def _brief_sections(raw: Any) -> list[dict[str, Any]]:
    """把作者写的 ``sections`` 摊成统一结构。

    作者可能写成 ``["绪论", "文献综述"]``、``[{"title": ..., "focus": ...}]``，
    也可能直接贴一段话。三种都接住：规划阶段拒绝解析作者的输入，只会让作者
    改输入去迁就程序，那是本末倒置。

    ``children``（来自开题报告的"结构框架"）会被保留下来：它是**作者已获通过的
    小节划分**，比模型现编的小节更该被遵守。丢掉它等于让生成结果与开题对不上。
    """
    out: list[dict[str, Any]] = []
    if raw is None:
        return out
    if isinstance(raw, str):
        raw = [p for p in raw.replace("\n", "；").split("；") if p.strip()]
    if isinstance(raw, dict):
        raw = list(raw.keys()) or list(raw.values())
    if not isinstance(raw, (list, tuple)):
        return out
    for item in raw:
        if isinstance(item, dict):
            title = clean_title(item.get("title") or item.get("name") or item.get("section") or "")
            focus = str(item.get("focus") or item.get("desc") or item.get("points") or "").strip()
            needs = str(item.get("needs") or item.get("evidence") or "").strip()
            kids = item.get("children") or item.get("subsections") or item.get("小节") or []
            if isinstance(kids, str):
                kids = [p for p in kids.replace("\n", "；").split("；") if p.strip()]
            children = [str(k).strip() for k in kids if str(k).strip()] if isinstance(kids, (list, tuple)) else []
        else:
            title = clean_title(item)
            focus = needs = ""
            children = []
        if not title:
            continue
        out.append({"title": title, "focus": focus, "needs": needs, "children": children})
    return out


def _dedupe(sections: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """按标题去重并保序：作者列了"绪论"，默认规划里也有"绪论"，只留一份。"""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for s in sections:
        key = clean_title(s.get("title"))
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(dict(s, title=key))
    return out


def _dict_list(value: Any, keys: Sequence[str]) -> list[dict[str, Any]]:
    """把模型返回的 ``list`` 补成 list[dict]，顺带兼容它给字符串的情况。"""
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return out
    for item in value:
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, str) and item.strip():
            out.append({keys[0]: item.strip()} if keys else {"text": item.strip()})
    return out


class WriterPlanAgent(Agent):
    """写作规划：检索每章的证据可得性，再用模型把它整理成可执行的大纲。"""

    name = "writer:plan"
    description = "先查证据再规划：产出带证据可得性报告的大纲，标出写不了的章节"
    stage = Stage.DRAFT.value
    capabilities = ("write", "plan", "outline")
    uses_model = True

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        board = ctx.board
        meta = board.project.meta
        brief = meta.get("writing_brief")
        brief = brief if isinstance(brief, dict) else {}

        topic = str(brief.get("topic") or brief.get("title") or "").strip() or board.project.title or "未命名论文"
        focus = brief.get("focus") or brief.get("emphasis") or ""
        if not isinstance(focus, str):
            focus = "；".join(str(f) for f in focus) if isinstance(focus, (list, tuple)) else str(focus)
        try:
            word_target = int(brief.get("word_target") or 0)
        except (TypeError, ValueError):
            word_target = 0

        # ---- 1) 章节规划：作者给的优先，其余补默认规划 --------------------
        sections = _dedupe(list(_brief_sections(brief.get("sections"))))
        used_default = False
        if not sections:
            used_default = True
            # 有主题没章节时用通用结构兜底。这不是"系统替作者定提纲"，
            # 而是给一个可修改的起点：五章结构改起来远快于从空白开始想。
            sections = _dedupe([dict(s) for s in DEFAULT_SECTIONS])
        if not focus:
            focus = "（作者未指定重点）"

        ctx.say(f"📋 写作规划：{len(sections)} 章，先逐章检索证据", 0.05)

        # ---- 2) 先检索，后写作：每章先查知识库里有没有材料 ----------------
        searcher = SearchFactory(
            ctx.settings.peers.rag_base,
            timeout=min(120.0, float(getattr(ctx.settings.peers, "timeout", 120.0) or 120.0)),
        )
        evidence_report: list[dict[str, Any]] = []
        for s in sections:
            query = f"{topic} {s['title']}"
            hits, error = searcher.search(query, k=SEARCH_K)
            entries = evidence_entries(hits)
            evidence_report.append(
                {
                    "title": s["title"],
                    "query": query,
                    "count": len(entries),
                    "citations": [e["citation"] or "来源未标注" for e in entries],
                    "needs": s.get("needs") or section_kind(s["title"]),
                    "author_only": is_author_only(s["title"]),
                    "error": error,
                }
            )

        unavailable = bool(searcher.failures)
        failure_reason = searcher.failures[0]["error"] if searcher.failures else ""
        zero_evidence = [r for r in evidence_report if r["count"] == 0]
        pro = [r for r in evidence_report if r["count"] > 0]

        # RAG 完全不可用：不花模型的钱去编一份"看起来像大纲"的东西。
        # 此时 AgentReport.ok=False，把适配器的错误原文（含启动提示）交给上游，
        # 由上游决定是等对端起来重跑，还是带着"无证据"继续。
        if unavailable:
            board.record(
                ctx.stage,
                ctx.round,
                self.name,
                "plan_failed",
                f"证据检索失败：{failure_reason}",
            )
            board.save()
            # 也发一条总线消息：界面与日志靠它显示"这一步没跑成"，
            # 只看 AgentReport 的调用方（比如单独跑一个 agent 的 CLI）看不到。
            ctx.bus.publish(
                "error",
                sender=self.name,
                payload={
                    "error": failure_reason,
                    "rag_base": ctx.settings.peers.rag_base,
                    "stage": ctx.stage,
                },
                stage=ctx.stage,
                round=ctx.round,
            )
            return self.report(
                ctx,
                ok=False,
                summary=(
                    "写作规划无法完成：论文知识库（RAG）不可用，"
                    f"未能确认任何一章的证据情况。{failure_reason}"
                ),
                error=failure_reason,
                data={
                    "evidence_unavailable": True,
                    "rag_base": ctx.settings.peers.rag_base,
                    "sections_planned": [r["title"] for r in evidence_report],
                    "evidence_report": evidence_report,
                    "author_only": [r["title"] for r in evidence_report if r["author_only"]],
                },                seconds=time.time() - t0,
            )

        ctx.say(
            f"🔍 证据可得性：{len(pro)} 章有材料、{len(zero_evidence)} 章为 0",
            0.2,
        )

        # ---- 3) 让模型整理大纲（输入里带真实证据条数，不让它凭空规划）----
        availability = "\n".join(
            f"- {r['title']}：检索到 {r['count']} 条资料"
            + (f"；代表来源：{'、'.join(r['citations'][:3])}" if r["citations"] else "；**没有资料**")
            for r in evidence_report
        )
        author_outline = _render_author_outline(sections)
        requirement = str(brief.get("requirement") or "").strip()
        prompt = OUTLINE_USER_TEMPLATE.format(
            title=topic,
            brief=_render_brief(brief) or "（作者未提供）",
            word_target=f"{word_target} 字" if word_target else "未指定",
            focus=focus,
            # 作者的写作要求是**硬约束**，必须进规划：大纲阶段就漏掉它，
            # 后面每一章都会跑偏（例如作者要求"必须做问卷调查"，大纲却排成纯理论研究）。
            requirement=(
                f"\n**作者的写作要求（必须在大纲里落实，不得忽略）：**\n{requirement}\n"
                if requirement
                else ""
            ),
            author_outline=author_outline or "（作者未提供章节结构，请按学科惯例拟定）",
            availability=availability,
            min_sections=max(3, len(sections) - 1),
            max_sections=max(8, len(sections) + 2),
        )

        llm_error = ""
        sections_out: list[dict[str, Any]] = []
        gaps: list[str] = []
        notes = ""
        try:
            data = ctx.ask_json(
                "writer:outline",
                [
                    {"role": "system", "content": OUTLINE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
            )
            if isinstance(data, dict):
                sections_out = _dict_list(data.get("sections"), ("title",))
                gaps = [str(g) for g in (data.get("evidence_gaps") or []) if str(g).strip()]
                notes = str(data.get("notes") or "")
        except Exception as exc:  # noqa: BLE001 - 模型失败要有可读报告，不能让流程崩
            llm_error = f"{type(exc).__name__}: {exc}"

        # ---- 4) 把作者列的章节与模型返回的章节对齐 ------------------------
        # 作者明确要求的章节不能因为模型漏写就消失；模型新增的章节则保留，
        # 但把证据情况补上（模型看不到每个细节，它漏标证据数时以检索结果为准）。
        report_by_title = {r["title"]: r for r in evidence_report}
        merged: list[dict[str, Any]] = []
        for s in sections_out:
            title = clean_title(s.get("title"))
            if not title:
                continue
            src = report_by_title.get(title)
            points = [str(p) for p in (s.get("points") or s.get("要点") or []) if str(p).strip()]
            merged.append(
                {
                    "title": title,
                    "points": points,
                    # 写作单元标题：作者开题里的小节优先，其次用模型给的小节标题。
                    # 这是"生成的论文与已通过的开题一致"的落点。
                    "subsections": _subsections_for(title, points, sections),
                    "needs": str(s.get("needs") or (src or {}).get("needs") or section_kind(title)),
                    "evidence_count": (src or {}).get("count", 0),
                    "citations": (src or {}).get("citations", []),
                    "author_only": is_author_only(title),
                }
            )
        have = {m["title"] for m in merged}
        for s in sections:
            if s["title"] in have:
                continue
            src = report_by_title.get(s["title"], {})
            merged.append(
                {
                    "title": s["title"],
                    "points": [],
                    "subsections": _subsections_for(s["title"], [], sections),
                    "needs": s.get("needs") or src.get("needs") or section_kind(s["title"]),
                    "evidence_count": src.get("count", 0),
                    "citations": src.get("citations", []),
                    "author_only": is_author_only(s["title"]),
                }
            )
        if not merged:
            # 模型什么都没给出：退回"按章节列证据"的最小可用大纲。
            merged = [
                {
                    "title": r["title"],
                    "points": [],
                    "subsections": _subsections_for(r["title"], [], sections),
                    "needs": r["needs"],
                    "evidence_count": r["count"],
                    "citations": r["citations"],
                    "author_only": r["author_only"],
                }
                for r in evidence_report
            ]

        # 证据情况以**检索结果**为权威，不采信模型的自述：
        # 模型说 evidence_ok=true 而检索是 0 条时，必须以 0 条为准。
        plan_ready = [m["title"] for m in merged if m["evidence_count"] > 0]
        plan_blocked = [m["title"] for m in merged if m["evidence_count"] == 0]
        author_only = [m["title"] for m in merged if m["author_only"]]
        word_plan = _word_plan(merged, word_target)
        for m in merged:
            m["word_target"] = word_plan.get(m["title"], 0)
        allocated = sum(word_plan.values())
        # 分配之和与总目标的偏差要如实说明：单章上下限会把它推开一点，
        # 假装"刚好等于目标"会让作者以为字数已经在控制之中。
        word_plan_note = ""
        if word_target and allocated:
            delta = allocated - word_target
            word_plan_note = (
                f"按章节职能分配后共 {allocated} 字"
                + (f"（比目标多 {delta} 字）" if delta > 0 else
                   (f"（比目标少 {-delta} 字）" if delta < 0 else "（与目标一致）"))
                + "。结论与核心分析章的占比不同，不做平均分配。"
            )

        outline_payload = {
            "topic": topic,
            "word_target": word_target,
            "word_plan": word_plan,
            "word_plan_note": word_plan_note,
            "sections": merged,
            "evidence_report": evidence_report,
            "evidence_gaps": gaps or [f"{r['title']}：检索不到可用证据" for r in zero_evidence],
            "notes": notes,
            "model_error": llm_error,
            "used_default_plan": used_default,
            "proposal_derived": bool(brief.get("source") == "开题报告"),
            "rag_failures": searcher.failures,
        }
        art = board.put_artifact(
            kind="outline",
            payload=outline_payload,
            text="\n".join(_render_outline_text(topic, merged)),
            created_by=self.name,
            stage=ctx.stage,
            run_id=ctx.run_id,
            note=f"写作规划 v1（{len(merged)} 章，{len(plan_blocked)} 章无证据）",
        )

        # ---- 5) 证据缺失的章节 → Issue（交还作者，不自动"想办法"）--------
        issues: list[Issue] = []
        zero_titles = {r["title"] for r in zero_evidence}
        for m in merged:
            if m["title"] not in zero_titles:
                continue
            issues.append(
                Issue(
                    id=new_id("iss"),
                    kind="writing.missing_evidence",
                    severity=Severity.MAJOR,
                    message=(
                        f"「{m['title']}」在论文知识库里检索不到可用证据，"
                        "现在写这一章只能凭个人印象，属于学术风险"
                    ),
                    location={"section": m["title"]},
                    evidence=f"检索词：{topic} {m['title']}；命中 0 条",
                    detected_by="rules:writer_plan",
                    confidence=1.0,
                    # 补资料是作者的活：系统既不知道作者手头有什么，也不能编。
                    fixable=False,
                    fix_hint=(
                        f"请补充「{m['title']}」这一章需要的资料后重新入库，再重跑写作规划。"
                        f"该章需要的证据类型：{m['needs']}。"
                        "可用素材包括：相关期刊论文、政策文件、统计年鉴、"
                        "以及你自己的调研数据（这类数据请单独说明，不要混进文献库）。"
                    ),
                    status="needs_author",
                )
            )
        added = board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)

        board.record(
            ctx.stage,
            ctx.round,
            self.name,
            "plan",
            f"大纲 {len(merged)} 章；有证据 {len(plan_ready)} 章；无证据 {len(plan_blocked)} 章",
        )
        board.save()

        summary = (
            f"大纲已生成：{len(merged)} 章，其中有可用证据 {len(plan_ready)} 章、"
            f"证据为 0 共 {len(plan_blocked)} 章"
            + (f"（{('、'.join(plan_blocked))[:60]}）" if plan_blocked else "")
        )
        if llm_error:
            summary += "；大纲要点未能由模型生成，已退回按证据情况列章"
        return self.report(
            ctx,
            ok=not llm_error,
            summary=summary,
            error=llm_error,
            artifacts=[art.id],
            issues=[i.id for i in issues],
            data={
                "outline_id": art.id,
                "sections": [m["title"] for m in merged],
                "sections_with_evidence": plan_ready,
                "sections_without_evidence": plan_blocked,
                "author_only": author_only,
                "evidence_report": evidence_report,
                "used_default_plan": used_default,
                "missing_evidence_issues": added,
                "word_plan": word_plan,
                "word_plan_note": word_plan_note,
                "rag_failures": searcher.failures,
                "evidence_unavailable": False,
            },
            seconds=time.time() - t0,
        )


def _render_brief(brief: dict[str, Any]) -> str:
    keep = ("topic", "sections", "word_target", "focus", "emphasis", "keywords", "discipline", "case", "methods")
    parts = [f"{k}={brief[k]}" for k in keep if brief.get(k)]
    return "；".join(parts)


def _subsections_for(title: str, points: Sequence[str], brief_sections: Sequence[dict[str, Any]]) -> list[str]:
    """该章的写作单元标题。

    优先级：**作者开题里的二级小节 > 模型现编的小节标题**。
    理由很直接——作者的小节是开题时已经通过的，换成模型现编的会让成稿与开题
    对不上，评审第一句话就是"你开题里写的第三节去哪了"。
    """
    for s in brief_sections:
        if s.get("title") == title and s.get("children"):
            kids = [clean_title(k) for k in s["children"]]
            kids = [k for k in kids if k]
            if kids:
                return kids
    return [clean_title(p) for p in points if clean_title(p)]


def _render_author_outline(sections: Sequence[dict[str, Any]]) -> str:
    """把作者在开题里拟定的小节渲染成提示词区块。"""
    lines: list[str] = []
    for s in sections:
        kids = [str(k) for k in (s.get("children") or []) if str(k).strip()]
        if kids:
            lines.append(f"- {s['title']}：" + "；".join(kids))
        else:
            lines.append(f"- {s['title']}：（作者未列出小节）")
    return "\n".join(lines)


def _render_outline_text(topic: str, sections: Sequence[dict[str, Any]]) -> list[str]:
    """给作者直接看的大纲文本（payload 是给程序看的）。"""
    lines = [f"# {topic} — 写作大纲", ""]
    for i, s in enumerate(sections, start=1):
        tag = ""
        if s.get("author_only"):
            tag = "　［必须作者自己写，系统不代写］"
        elif not s.get("evidence_count"):
            tag = "　［⚠ 知识库中暂无证据，现在写不了］"
        else:
            tag = f"　［可用证据 {s['evidence_count']} 条］"
        words = s.get("word_target")
        if words:
            tag += f"　［目标 {words} 字］"
        lines.append(f"{i}. {s['title']}{tag}")
        subs = s.get("subsections") or []
        for p in subs:
            lines.append(f"   - {p}")
        if not subs:
            for p in s.get("points") or []:
                lines.append(f"   - {p}")
        if s.get("needs"):
            lines.append(f"   需要的证据：{s['needs']}")
    return lines


def _word_plan(sections: Sequence[dict[str, Any]], word_target: int) -> dict[str, int]:
    """按**章节职能**分配目标字数，而不是平均分。

    均分是错的：文献综述与核心分析章各占两成以上，结论只占一成左右。平均分会把
    结论撑成一篇啰嗦的复述，同时把最需要篇幅的核心章饿死——而核心章写不充分，
    整篇论文的论证就不成立。占比取自 ``app.writing.WORD_SHARE``（学位论文常见区间）。

    单章上下限（``MIN_SECTION_WORDS`` / ``MAX_SECTION_WORDS``）是为了防止占比
    在小章数下把某一章压到写不出论证，或撑到该拆章的程度。因此**分配之和可能与
    总目标有偏差**，偏差会如实写进 ``word_plan_note``，不假装刚好等于目标。
    """
    if not word_target or not sections:
        return {}
    weights: list[float] = []
    for s in sections:
        try:
            kind = classify(str(s.get("title") or "")).kind
        except Exception:  # noqa: BLE001 - 分类失败不影响分配
            kind = "body"
        weights.append(max(0.02, float(word_share(kind))))
    total = sum(weights) or 1.0
    out: dict[str, int] = {}
    for s, w in zip(sections, weights):
        n = int(round(word_target * w / total))
        out[s["title"]] = max(MIN_SECTION_WORDS, min(MAX_SECTION_WORDS, n))
    return out
