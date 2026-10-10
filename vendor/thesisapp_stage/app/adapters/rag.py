"""论文 RAG 知识库适配器（对端：thesis-assistant，默认 8760）。

这个适配器是"证据"的唯一来源。整个系统的可信度都取决于一件事：
**写进稿子的每一句有依据的话，都能追回一个具体的片段 + 章节 + 页码。**

所以这里刻意不提供"帮我编一段"之类的方法，只有检索和问答，且检索结果强制
带上 citation。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from .base import PeerError, PeerHTTP


@dataclass
class Hit:
    """一条检索命中。"""

    index: int
    doc_id: str = ""
    text: str = ""
    score: float = 0.0
    citation: str = ""
    section: str = ""
    page: int | None = None
    kind: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "Hit":
        page = d.get("page")
        try:
            page = int(page) if page is not None and str(page).strip() else None
        except (TypeError, ValueError):
            page = None
        return cls(
            index=int(d.get("index") or 0),
            doc_id=str(d.get("doc_id") or ""),
            text=str(d.get("text") or ""),
            score=float(d.get("score") or 0.0),
            citation=str(d.get("citation") or ""),
            section=str(d.get("section") or ""),
            page=page,
            kind=str(d.get("kind") or ""),
            raw=d,
        )

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "doc_id": self.doc_id,
            "text": self.text,
            "score": round(self.score, 4),
            "citation": self.citation,
            "section": self.section,
            "page": self.page,
            "kind": self.kind,
        }


@dataclass
class RAGAnswer:
    text: str = ""
    sources: list[dict] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def cite(self, index: int) -> dict | None:
        for s in self.sources:
            if s.get("index") == index:
                return s
        return None

    def as_dict(self) -> dict:
        return {"text": self.text, "sources": self.sources, "error": self.error}


class RAGAdapter(PeerHTTP):
    label = "论文 RAG 服务（thesis-assistant）"
    start_hint = "请在 E:\\AI\\thesis-assistant 运行 .\\run.cmd 启动它。"

    # -- 只读 -------------------------------------------------------------
    def status(self) -> dict:
        return self.get("/api/status")

    def stats(self) -> dict:
        return self.get("/api/status").get("stats", {})

    def docs(self) -> list[dict]:
        return self.get("/api/docs").get("docs", [])

    def resolve(self, needle: str) -> str:
        """把作者名 / 标题片段 / 年份解析成 doc_id。

        多篇匹配时**抛错并列出候选**，不猜。猜错会导致整段论证引用错论文，
        而这种错误在成稿里极难发现。
        """
        needle = (needle or "").strip()
        if not needle:
            raise PeerError("要解析的论文指代为空", kind="input")
        documents = self.docs()

        for d in documents:
            if d.get("doc_id") == needle:
                return needle

        def label(d: dict) -> str:
            return f"{d.get('author') or ''}{d.get('year') or ''}{d.get('title') or ''}"

        matchers = (
            lambda d: needle == (d.get("author") or ""),
            lambda d: needle in (d.get("title") or ""),
            lambda d: needle in (d.get("author") or ""),
            lambda d: needle == (d.get("year") or ""),
            lambda d: needle in label(d),
        )
        for match in matchers:
            hits = [d for d in documents if match(d)]
            if len(hits) == 1:
                return hits[0]["doc_id"]
            if len(hits) > 1:
                listing = "\n".join(
                    f"  {d['doc_id']}  {d.get('author') or '佚名'} {d.get('year') or ''}  {d['title']}"
                    for d in hits
                )
                raise PeerError(
                    f"「{needle}」匹配到 {len(hits)} 篇，请用 doc_id 指定：\n{listing}",
                    kind="ambiguous",
                )
        raise PeerError(f"知识库里没有匹配「{needle}」的论文", kind="not_found")

    def outline(self, doc_id: str) -> dict:
        return self.get(f"/api/docs/{doc_id}/outline")

    # -- 检索 -------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        k: int = 8,
        doc_ids: list[str] | None = None,
        include_refs: bool = False,
        rerank: bool | None = None,
    ) -> list[Hit]:
        """纯检索。默认不开重排就不调用模型，可放心循环调用。"""
        body: dict[str, Any] = {"query": query, "k": k, "include_refs": include_refs}
        if doc_ids:
            body["doc_ids"] = doc_ids
        if rerank is not None:
            body["rerank"] = rerank
        data = self.post("/api/search", body)
        return [Hit.from_dict(h) for h in (data.get("hits") or [])]

    # -- 问答 -------------------------------------------------------------
    def ask_stream(
        self, question: str, *, k: int = 8, doc_ids: list[str] | None = None, include_refs: bool = False
    ) -> Iterator[dict]:
        body: dict[str, Any] = {"question": question, "k": k, "include_refs": include_refs}
        if doc_ids:
            body["doc_ids"] = doc_ids
        yield from self.stream_lines("/api/ask", body)

    def ask(
        self, question: str, *, k: int = 8, doc_ids: list[str] | None = None, include_refs: bool = False
    ) -> RAGAnswer:
        out = RAGAnswer()
        for ev in self.ask_stream(question, k=k, doc_ids=doc_ids, include_refs=include_refs):
            kind = ev.get("type")
            if kind == "delta":
                out.text += ev.get("text", "")
            elif kind == "sources":
                out.sources = ev.get("sources") or []
            elif kind == "error":
                out.error = str(ev.get("message") or "")
        return out

    # -- 维护 -------------------------------------------------------------
    def ingest(self, *, force: bool = False, poll: float = 0.7, max_wait: float = 3600.0) -> list[dict]:
        """扫描对端 inbox 入库，阻塞到完成。

        轮询而不是等回调：入库是长任务，卡住时要能超时退出，不能把整个流程挂死。
        """
        import time

        started = self.post("/api/ingest", {"force": force})
        if not started.get("ok"):
            raise PeerError(f"入库启动失败：{started.get('message')}", kind="ingest")
        waited = 0.0
        while waited < max_wait:
            time.sleep(poll)
            waited += poll
            st = self.get("/api/ingest/status")
            if not st.get("running"):
                return st.get("results", [])
        raise PeerError(f"入库超时（{max_wait:.0f}s 未完成）", kind="timeout")

    def ingest_status(self) -> dict:
        return self.get("/api/ingest/status")
