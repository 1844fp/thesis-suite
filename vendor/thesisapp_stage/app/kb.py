"""本地知识库：查库、只检索不花钱的试查、投料入库、以及**生成前预检**。

## 这一层解决的实际问题

本系统自己不存文献——它连的是 ``thesis-assistant`` 的论文库。那个库默认只有
某一批论文（实测是 20 篇数据资产方向）。于是出现一个很具体的使用障碍：

> 作者换了个题目，点"生成"，系统照样跑完，产出一篇**引用着 20 篇无关文献**的论文。

所以这一层做四件事，顺序就是作者该走的顺序：

1. **查库**（``status`` / ``list_docs``）——先看清楚库里有什么；
2. **试查**（``search``）——用几个检索词看看能不能召回相关材料，纯检索不调用模型；
3. **投料**（``add_files``）——把作者的 PDF 投进知识库的收料目录并触发入库；
4. **预检**（``precheck``）——用题目本身去检索并判相关，**在花钱生成之前**给出结论：
   这个题目在这个库里能不能写。

## 预检为什么必须在生成之前

一次一键生成是几十次模型调用。题目与库不匹配时，那些调用**全部白花**——
而且产出还被闸门拦下、章节全空。所以先把"库够不够"这件事用零成本的检索查清楚，
是省钱也是省时间。界面上"预检知识库"按钮走的就是这条路径。
"""

from __future__ import annotations

import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from .adapters.rag import RAGAdapter
from .config import Settings, load_settings
from .writing.relevance import TopicKeywords, extract_keywords, judge_entries

__all__ = [
    "KBStatus",
    "PrecheckResult",
    "kb_status",
    "list_docs",
    "search",
    "precheck",
    "add_files",
    "peer_inbox",
    "INGESTIBLE_SUFFIXES",
]

#: 能入库的格式。与 ``thesis-assistant`` 的入库器一致——它只吃这三种。
INGESTIBLE_SUFFIXES = {".pdf", ".docx", ".txt", ".md", ".markdown"}

#: 预检用的默认检索词数量上限。题目本身太长时截断，避免一次预检发几十个请求。
MAX_PRECHECK_QUERIES = 5


def peer_inbox(settings: Settings | None = None) -> Path:
    """知识库服务的收料目录（``inbox``）。"""
    s = settings or load_settings()
    return Path(s.peers.rag_inbox)


@dataclass
class KBStatus:
    reachable: bool = False
    base_url: str = ""
    error: str = ""
    docs: int = 0
    chunks: int = 0
    inbox: str = ""
    inbox_files: int = 0

    def as_dict(self) -> dict:
        return {
            "reachable": self.reachable,
            "base_url": self.base_url,
            "error": self.error,
            "docs": self.docs,
            "chunks": self.chunks,
            "inbox": self.inbox,
            "inbox_files": self.inbox_files,
        }


def _count_inbox(path: Path) -> int:
    try:
        return sum(1 for p in path.rglob("*") if p.is_file() and not p.name.startswith("."))
    except Exception:  # noqa: BLE001 - 目录不存在/无权限都只影响这个数字
        return 0


def kb_status(settings: Settings | None = None, *, timeout: float = 20.0) -> KBStatus:
    """知识库概况。服务不可达时如实返回 ``reachable=False`` + 原因。"""
    s = settings or load_settings()
    out = KBStatus(base_url=s.peers.rag_base, inbox=str(peer_inbox(s)))
    out.inbox_files = _count_inbox(Path(out.inbox))
    rag = RAGAdapter(s.peers.rag_base, timeout=timeout)
    try:
        docs = rag.docs()
    except Exception as exc:  # noqa: BLE001 - 连不上是常态，不是异常情况
        out.error = str(exc) or f"{type(exc).__name__}"
        return out
    out.reachable = True
    out.docs = len(docs)
    out.chunks = sum(int(d.get("n_chunks") or 0) for d in docs if isinstance(d, dict))
    return out


def list_docs(settings: Settings | None = None, *, timeout: float = 30.0) -> list[dict]:
    """列出知识库里的全部文献。服务不可达时抛 ``PeerError``（调用方负责显示）。"""
    s = settings or load_settings()
    return RAGAdapter(s.peers.rag_base, timeout=timeout).docs()


def search(
    query: str,
    *,
    k: int = 8,
    settings: Settings | None = None,
    rerank: bool = False,
    timeout: float = 60.0,
) -> list[Any]:
    """纯检索。默认不重排，**不调用模型、不花钱**，可放心反复试查询词。"""
    s = settings or load_settings()
    return RAGAdapter(s.peers.rag_base, timeout=timeout).search(query, k=k, rerank=rerank)


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------


@dataclass
class QueryResult:
    query: str = ""
    hits: int = 0
    relevant: int = 0
    level: str = "none"
    #: 命中的代表文献题名（给作者看"库里到底有什么"）
    titles: list[str] = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "query": self.query,
            "hits": self.hits,
            "relevant": self.relevant,
            "level": self.level,
            "titles": self.titles[:8],
            "error": self.error,
        }


@dataclass
class PrecheckResult:
    topic: str = ""
    keywords: TopicKeywords = field(default_factory=TopicKeywords)
    queries: list[QueryResult] = field(default_factory=list)
    relevant: int = 0
    total: int = 0
    reachable: bool = True
    error: str = ""

    @property
    def ratio(self) -> float:
        return self.relevant / max(1, self.total)

    @property
    def verdict(self) -> str:
        """``strong`` / ``partial`` / ``none`` / ``unreachable``。"""
        if not self.reachable:
            return "unreachable"
        if self.relevant < 2 or self.ratio < 0.25:
            return "none"
        return "strong" if self.ratio >= 0.5 else "partial"

    @property
    def usable(self) -> bool:
        return self.verdict in ("strong", "partial")

    def suggestion(self) -> str:
        """给作者的一句话结论 + 下一步该做什么。"""
        if not self.reachable:
            return (
                f"连不上知识库服务（{self.error}）。请先启动 "
                "E:\\AI\\thesis-assistant\\run.cmd，再回来预检。"
            )
        if self.verdict == "strong":
            return (
                f"知识库里找到了与本题相关的材料（{self.relevant}/{self.total} 条相关，"
                f"涉及 {len({t for q in self.queries for t in q.titles})} 篇文献）。可以开始生成。"
            )
        if self.verdict == "partial":
            return (
                f"知识库里只有部分材料与本题相关（{self.relevant}/{self.total}）。"
                "可以生成，但部分章节会因证据不足而写不出来——建议先补充几篇本主题文献。"
            )
        return (
            f"知识库里**没有**与本题相关的材料（{self.relevant}/{self.total} 条相关）。"
            "直接生成会失败：写作阶段会把与题目无关的检索结果全部剔除，"
            "相关章节会因无证据而拒绝生成。请先把本主题的文献投进知识库（界面「知识库」页签可投料），"
            "再回来预检。"
        )

    def as_dict(self) -> dict:
        return {
            "topic": self.topic,
            "reachable": self.reachable,
            "error": self.error,
            "verdict": self.verdict,
            "usable": self.usable,
            "relevant": self.relevant,
            "total": self.total,
            "ratio": round(self.ratio, 3),
            "keywords": list(self.keywords.phrases),
            "suggestion": self.suggestion(),
            "queries": [q.as_dict() for q in self.queries],
        }


def build_precheck_queries(topic: str, requirement: str = "") -> list[str]:
    """从题目与要求里构造几个预检检索词。

    只用题目本身当检索词会漏掉"库里有材料但表述不同"的情况，所以补两个变体：
    完整题目、以及题目里的核心短语两两组合。
    """
    kw = extract_keywords(topic, requirement)
    out: list[str] = []
    t = (topic or "").strip()
    if t:
        out.append(t)
    phrases = [p for p in kw.phrases if not re.search(r"[A-Za-z]", p)][:6]
    if len(phrases) >= 2:
        out.append(" ".join(phrases[:2]))
        out.append(" ".join(phrases[1:3]))
    elif phrases:
        out.append(phrases[0])
    # 去重保序
    seen: set[str] = set()
    uniq: list[str] = []
    for q in out:
        q = q.strip()
        if q and q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:MAX_PRECHECK_QUERIES]


def precheck(
    topic: str,
    *,
    requirement: str = "",
    settings: Settings | None = None,
    k: int = 8,
    timeout: float = 60.0,
) -> PrecheckResult:
    """**生成前预检**：这个题目在这个知识库里能不能写。

    成本为零（纯检索 + 本地判相关），所以作者可以放心反复试不同题目。
    """
    s = settings or load_settings()
    topics = topic or ""
    result = PrecheckResult(topic=topics, keywords=extract_keywords(topics, requirement))
    queries = build_precheck_queries(topics, requirement)
    if not queries:
        result.reachable = False
        result.error = "题目为空，无法预检"
        return result

    seen_index: dict[str, Any] = {}
    for q in queries:
        row = QueryResult(query=q)
        try:
            hits = search(q, k=k, settings=s, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 服务不可达要给出可读原因
            result.reachable = False
            result.error = str(exc) or f"{type(exc).__name__}"
            row.error = result.error
            result.queries.append(row)
            return result
        verdict = judge_entries(hits, result.keywords)
        row.hits = len(hits)
        row.relevant = len(verdict.relevant)
        row.level = verdict.level
        # 题名去重：同一篇论文的多个片段只列一次
        for e in verdict.relevant:
            key = str(getattr(e, "doc_id", "") or getattr(e, "citation", ""))
            if key and key not in seen_index:
                seen_index[key] = e
                title = str(getattr(e, "title", "") or "").strip()
                if not title:
                    citation = str(getattr(e, "citation", "") or "")
                    m = re.search(r"《([^》]+)》", citation)
                    title = m.group(1) if m else citation[:40]
                if title:
                    row.titles.append(title)
        result.queries.append(row)

    result.total = sum(q.hits for q in result.queries)
    result.relevant = sum(q.relevant for q in result.queries)
    return result


# ---------------------------------------------------------------------------
# 投料入库
# ---------------------------------------------------------------------------


def add_files(
    paths: Sequence[str | Path],
    *,
    settings: Settings | None = None,
    ingest: bool = True,
    force: bool = False,
) -> dict:
    """把资料投进知识库的收料目录，并可选择立刻触发入库。

    两道刻意的限制：

    * **不删除、不移动源文件**，只复制。作者给的是他自己的资料，
      工具不该有"移动你的文件"这种行为。
    * 扩展名不在白名单的直接跳过并**逐条说明原因**，而不是悄悄忽略——
      作者以为自己投进去了、结果检索不到，是最难查的一类问题。
    """
    s = settings or load_settings()
    inbox = peer_inbox(s)
    inbox.mkdir(parents=True, exist_ok=True)

    copied: list[dict] = []
    skipped: list[dict] = []
    for raw in paths:
        p = Path(raw)
        if not p.exists():
            skipped.append({"path": str(p), "reason": "文件不存在"})
            continue
        if p.is_dir():
            skipped.append({"path": str(p), "reason": "是目录；请指定文件（或用 --dir 递归投料）"})
            continue
        suffix = p.suffix.lower()
        if suffix not in INGESTIBLE_SUFFIXES:
            skipped.append(
                {
                    "path": str(p),
                    "reason": f"格式 {suffix or '(无扩展名)'} 不支持入库；支持 "
                              f"{'、'.join(sorted(INGESTIBLE_SUFFIXES))}。"
                              "扫描版 PDF 需要先做 OCR；.caj/.nh 需先转 PDF。",
                }
            )
            continue
        dest = inbox / p.name
        if dest.exists() and not force:
            skipped.append({"path": str(p), "reason": f"收料目录里已有同名文件（{dest}），未覆盖"})
            continue
        try:
            shutil.copy2(p, dest)
            copied.append({"path": str(p), "dest": str(dest), "bytes": dest.stat().st_size})
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不该中断整批
            skipped.append({"path": str(p), "reason": f"复制失败：{type(exc).__name__}: {exc}"})

    out: dict[str, Any] = {
        "inbox": str(inbox),
        "copied": copied,
        "skipped": skipped,
        "ingested": [],
        "ingest_error": "",
    }
    if ingest and copied:
        rag = RAGAdapter(s.peers.rag_base, timeout=s.peers.timeout)
        try:
            out["ingested"] = rag.ingest(force=force)
        except Exception as exc:  # noqa: BLE001 - 服务不可达时文件已经放进去了，如实说
            out["ingest_error"] = (
                f"{exc}（文件已复制到 {inbox}，服务恢复后可用 `kb ingest` 重新触发入库）"
            )
    return out


def ingest(settings: Settings | None = None, *, force: bool = False) -> list[dict]:
    """触发知识库服务扫描它的收料目录并入库。"""
    s = settings or load_settings()
    return RAGAdapter(s.peers.rag_base, timeout=s.peers.timeout).ingest(force=force)
