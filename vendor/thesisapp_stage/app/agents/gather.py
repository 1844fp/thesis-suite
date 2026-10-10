"""资料获取与知识库智能体（能力① 与 ②）。

三个智能体，职责边界必须清楚，否则会互相重复：

* ``LiteratureGatherAgent`` —— **资料获取**：扫本地目录、判定哪些能入库、触发对端入库。
  它不检索、不写作。
* ``KnowledgeRetrieveAgent`` —— **证据检索**：按查询从知识库取回带引用的片段，
  落成 ``evidence`` 工件供写作与验证复用。它不碰文件、不写作。
* ``KnowledgeQAAgent`` —— **知识库问答**：基于全库回答问题并给出来源。
  它是给人用的入口，不参与自动流程（自动流程用检索，因为问答会引入模型的自由发挥）。

为什么把三者分开：写作助手需要的是**检索到的原文片段**（可追责），而不是
问答结果（是模型的转述）。混在一起会让"引用"变成"模型说它引用过"。
"""

from __future__ import annotations

import time
from pathlib import Path

from ..adapters.base import PeerError
from ..adapters.rag import Hit, RAGAdapter
from ..adapters.research import LocalIntake
from ..config import INBOX_DIR
from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage


def _rag(ctx: AgentContext, *, timeout: float = 60.0) -> RAGAdapter:
    return RAGAdapter(ctx.settings.peers.rag_base, timeout=timeout)


def _hits_to_payload(hits: list[Hit], query: str) -> dict:
    return {
        "query": query,
        "count": len(hits),
        "hits": [h.as_dict() for h in hits],
        "citations": [h.citation for h in hits if h.citation],
    }


# ---------------------------------------------------------------------------
# ① 资料获取
# ---------------------------------------------------------------------------


class LiteratureGatherAgent(Agent):
    """Scan local folders and hand them to the knowledge base for ingestion."""

    name = "gather:literature"
    description = "扫描本地资料目录，判定可入库项并触发知识库入库"
    stage = Stage.GATHER.value
    capabilities = ("gather", "ingest")
    uses_model = False

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        meta = ctx.board.project.meta
        roots = [INBOX_DIR, Path(r"E:\AI\thesis-assistant\inbox")]
        extra = meta.get("gather_roots") or []
        roots.extend(Path(r) for r in extra)

        intake = LocalIntake(roots)
        items = intake.scan()
        summary = intake.summary(items)

        # 扫到的东西全部记账，包括不能入库的——作者需要知道"为什么这份材料没进去"。
        meta["gather_scan"] = {
            "summary": summary,
            "items": [i.as_dict() for i in items],
            "scanned_at": time.time(),
        }

        issues: list[Issue] = []
        for item in items:
            if item.ingestible:
                continue
            issues.append(
                Issue(
                    id="",
                    kind="gather.unsupported_format",
                    severity=Severity.INFO,
                    message=f"{item.filename}：{item.parse_note}",
                    location={"file": item.path},
                    evidence=item.filename,
                    detected_by="rules:gather",
                    fixable=False,
                    fix_hint="按提示转换格式后重新扫描；转换后的文件放入 inbox 目录。",
                )
            )
        ctx.board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)

        if not any(i.ingestible for i in items):
            ctx.board.save()
            return self.report(
                ctx,
                ok=True,
                summary=f"扫描到 {summary['total']} 个文件，但没有可入库的文档",
                data={"scan": summary},
                seconds=time.time() - t0,
            )

        # 触发对端入库。这一步会调用对端的解析与向量化管线。
        if not meta.get("gather_ingest", True):
            ctx.board.save()
            return self.report(
                ctx,
                ok=True,
                summary=f"扫描到 {summary['total']} 个文件（已按要求跳过入库）",
                data={"scan": summary, "ingested": False},
                seconds=time.time() - t0,
            )

        try:
            ctx.say("正在让知识库服务入库扫描到的资料…", 0.0)
            results = _rag(ctx, timeout=ctx.settings.peers.timeout).ingest(
                force=bool(meta.get("gather_force", False))
            )
        except PeerError as exc:
            ctx.board.save()
            return self.report(
                ctx,
                ok=False,
                summary="入库失败",
                error=str(exc),
                data={"scan": summary},
                seconds=time.time() - t0,
            )

        ok_n = sum(1 for r in results if str(r.get("status")) in ("ok", "indexed", "added"))
        meta["gather_ingest_result"] = {"count": len(results), "ok": ok_n, "results": results[:50]}
        ctx.board.record(ctx.stage, ctx.round, self.name, "ingest", f"{ok_n}/{len(results)} 成功")
        ctx.board.save()

        return self.report(
            ctx,
            ok=True,
            summary=f"扫描 {summary['total']} 个文件，入库完成 {ok_n}/{len(results)}",
            data={"scan": summary, "ingested": True, "ok": ok_n, "total": len(results)},
            seconds=time.time() - t0,
        )


# ---------------------------------------------------------------------------
# ② 证据检索
# ---------------------------------------------------------------------------


class KnowledgeRetrieveAgent(Agent):
    """Retrieve cited evidence from the knowledge base and store it as an artifact."""

    name = "rag:retrieve"
    description = "按查询从论文知识库检索带引用的证据片段"
    stage = Stage.RETRIEVE.value
    capabilities = ("retrieve", "rag", "evidence")
    uses_model = False

    #: 单次检索取回多少片段
    K = 8

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        meta = ctx.board.project.meta
        queries: list[str] = list(meta.get("retrieve_queries") or [])
        if not queries:
            # 没给查询就用项目标题兜底，至少给出一份"相关性总览"。
            title = ctx.board.project.title
            queries = [title] if title else []

        if not queries:
            return self.report(
                ctx,
                ok=False,
                summary="没有可检索的查询",
                error="请在项目 meta 里提供 retrieve_queries，或设置项目标题",
                seconds=time.time() - t0,
            )

        doc_ids = meta.get("retrieve_doc_ids") or None
        k = int(meta.get("retrieve_k") or self.K)
        rag = _rag(ctx)
        board = ctx.board

        collected: dict[str, dict] = {}
        per_query: list[dict] = []
        failures: list[str] = []

        for i, q in enumerate(queries, 1):
            ctx.say(f"检索 {i}/{len(queries)}：{q[:40]}", i / max(len(queries), 1))
            try:
                hits = rag.search(q, k=k, doc_ids=doc_ids, rerank=False)
            except PeerError as exc:
                # 单条查询失败不应中断其余查询；但如果**全部**失败，
                # 说明服务不可用，那时必须显式失败而不是返回空证据集。
                failures.append(f"{q}: {exc}")
                per_query.append({"query": q, "count": 0, "error": str(exc)})
                continue

            per_query.append({"query": q, "count": len(hits), "citations": [h.citation for h in hits]})
            for h in hits:
                key = f"{h.doc_id}|{h.page}|{h.text[:80]}"
                if key not in collected:
                    collected[key] = h.as_dict()

        if failures and not collected:
            return self.report(
                ctx,
                ok=False,
                summary="证据检索全部失败",
                error=failures[0],
                data={"per_query": per_query},
                seconds=time.time() - t0,
            )

        evidence = {
            "queries": queries,
            "per_query": per_query,
            "unique_hits": list(collected.values()),
            "unique_count": len(collected),
            "failures": failures,
        }
        art = board.put_artifact(
            kind="evidence",
            payload=evidence,
            created_by=self.name,
            stage=ctx.stage,
            run_id=ctx.run_id,
            note=f"{len(queries)} 条查询，去重后 {len(collected)} 条证据",
        )

        # 一条证据都没有时要报出来：写作阶段拿不到材料，继续写只会产出没依据的内容。
        if not collected:
            board.add_issues(
                [
                    Issue(
                        id="",
                        kind="evidence.none_found",
                        severity=Severity.MAJOR,
                        message=f"对 {len(queries)} 条查询都没有检索到证据，知识库里可能缺少相关文献",
                        location={"queries": queries},
                        evidence=queries[0][:80] if queries else "",
                        detected_by="rules:rag",
                        fixable=False,
                        fix_hint="先补充相关文献（资料获取），或调整检索查询词。",
                    )
                ],
                stage=ctx.stage,
                round_no=ctx.round,
            )

        board.save()
        return self.report(
            ctx,
            ok=True,
            summary=f"{len(queries)} 条查询，去重后得到 {len(collected)} 条证据"
            + (f"，{len(failures)} 条查询失败" if failures else ""),
            artifacts=[art.id],
            data={"evidence": evidence, "artifact": art.id},
            seconds=time.time() - t0,
        )


class KnowledgeQAAgent(Agent):
    """Answer a question from the whole library, with sources."""

    name = "rag:ask"
    description = "基于论文知识库回答问题并给出来源（面向人使用的入口）"
    stage = Stage.RETRIEVE.value
    capabilities = ("ask", "rag")
    uses_model = True

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        question = str(ctx.board.project.meta.get("ask_question") or "").strip()
        if not question:
            return self.report(
                ctx, ok=False, summary="没有要问的问题", error="meta.ask_question 为空",
                seconds=time.time() - t0,
            )

        try:
            ans = _rag(ctx).ask(
                question,
                k=int(ctx.board.project.meta.get("ask_k") or 8),
                doc_ids=ctx.board.project.meta.get("ask_doc_ids") or None,
            )
        except PeerError as exc:
            return self.report(ctx, ok=False, summary="知识库问答失败", error=str(exc), seconds=time.time() - t0)

        if ans.error:
            return self.report(ctx, ok=False, summary="知识库问答返回错误", error=ans.error, seconds=time.time() - t0)

        art = ctx.board.put_artifact(
            kind="qa",
            text=ans.text,
            payload={"question": question, "sources": ans.sources},
            created_by=self.name,
            stage=ctx.stage,
            run_id=ctx.run_id,
            note=question[:80],
        )
        ctx.board.save()
        return self.report(
            ctx,
            ok=True,
            summary=f"已回答（{len(ans.sources)} 个来源，{len(ans.text)} 字）",
            artifacts=[art.id],
            data={"question": question, "sources": ans.sources, "answer": ans.text},
            seconds=time.time() - t0,
        )
