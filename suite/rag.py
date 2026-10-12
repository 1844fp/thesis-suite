"""RAG 知识库：把作者自己的资料变成可检索的依据。

为什么不用向量检索
------------------
"RAG" 的标准做法是 embedding + 向量库，但本站**用不了**，这不是偏好而是环境事实：

* 本机没有 ``sentence_transformers`` / ``faiss`` / ``chromadb``（已实测缺席）；
* 装它们要么下几百 MB 的模型权重、要么现场编译，在公网容器里都不可行；
* 而且 embedding 也要调外部 API——那就把"检索"也变成了花钱的操作，
  与本站"不填 Key 也能先用起来"的定位冲突。

所以这里走**关键词检索 + 模型重排**：

    入库：段落切块 → jieba 分词 → SQLite FTS5 建索引
    检索：查询分词 → FTS5 按 BM25 取候选 → （可选）模型对候选做相关性重排

这条路在"作者自己的几十上百份资料"这个规模上够用，而且有三个实在的好处：
**不需要 Key、不花钱、离线可用**。代价是它做不了语义检索（问"教师心理状态"
不会命中"职业倦怠"，除非分词后恰好共有词），这一点在 ``limits`` 里如实写明。

为什么是 SQLite 而不是内存索引
------------------------------
资料可能很大（一本专著几百页），而进程内存要留给 53 个检查器与写作链路。
SQLite 是标准库自带、有 FTS5、支持增量写入与事务，重启也不用重建整个索引。
代价是它要落盘——所以库文件放在会话的数据目录下，并且**明确告诉访客这是本地文件**。
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

#: 分块目标长度（字符）。学术段落常在 300~800 字，取 600 让一块大致等于一两个自然段。
#:
#: 太大：检索命中后要给模型看很长一段，挤掉生成预算；
#: 太小：一句话被切成几块，检索到的片段没有上下文，引用起来没有意义。
CHUNK_CHARS = 600

#: 相邻块的重叠长度。段落边界切分仍可能切断一个论证，
#: 重叠 80 字能让跨块的论证在两块里都出现。
CHUNK_OVERLAP = 80

#: 单次检索默认返回几块。
DEFAULT_K = 6

#: 知识库文件放在会话数据目录下。用环境变量可重定向（容器里指向可写目录）。
_DATA_DIR = Path(os.environ.get("DSH_THESIS_DATA_DIR") or (Path(__file__).resolve().parent.parent / "data"))
KB_DIR = _DATA_DIR / "knowledge"
KB_PATH = KB_DIR / "kb.sqlite3"


def set_base_dir(path: str | Path) -> None:
    """重定向知识库位置。

    为什么需要它：路径在**模块导入时**就算好了，而测试与容器都可能在导入之后
    才决定数据目录。没有这个入口，就只能靠 reload 模块来改路径——
    那是很容易出错的做法（旧连接可能还指着旧文件）。
    """
    global _DATA_DIR, KB_DIR, KB_PATH
    _DATA_DIR = Path(path)
    KB_DIR = _DATA_DIR / "knowledge"
    KB_PATH = KB_DIR / "kb.sqlite3"


def _ensure_dir() -> None:
    KB_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------
_jieba = None
_jieba_ready = False


def _get_jieba():
    """延迟加载 jieba。

    延迟有两个理由：一是它首次加载要建前缀词典（约 1 秒），
    不该拖慢服务启动；二是它缺席时要能降级到字符切分而不是让整个模块起不来。
    """
    global _jieba, _jieba_ready
    if not _jieba_ready:
        try:
            import jieba

            # 关掉 jieba 的启动日志，否则每次首屏都会往控制台打一堆 "Building prefix dict"
            jieba.setLogLevel(60)
            _jieba = jieba
        except Exception:  # noqa: BLE001 - 缺席就走字符切分
            _jieba = None
        _jieba_ready = True
    return _jieba


def tokenize(text: str) -> str:
    """把文本切成给 FTS5 用的 token 串。

    **中文必须自己做这一步**：FTS5 默认的分词器按空白与标点切，
    一整句中文会被当成一个 token，于是任何查询都匹配不上。
    常见做法是先分词再用空格拼回去。

    jieba 缺席时降级为**单字切分**：单个汉字也是 token，检索仍能工作
    （召回更宽、精度更低），但至少不会整个功能失效。
    """
    text = text or ""
    jb = _get_jieba()
    if jb is not None:
        # cut_for_search 会把长词再切成子词，对检索的召回更友好
        tokens = [t.strip() for t in jb.cut_for_search(text) if t.strip()]
    else:
        tokens = [c for c in text if not c.isspace()]
    # FTS5 的查询语法里 " * ( ) : ^ - 有特殊含义，索引侧要去掉；
    # 否则用户资料里出现一个双引号就会让整个入库失败。
    safe = [re.sub(r'["*():^-]', "", t) for t in tokens]
    return " ".join(t for t in safe if t)


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------
def chunk_text(text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落边界把长文切成块，块间留一点重叠。

    优先在段落边界切：学术文本的段落本身就是论证单元，
    从句子中间切开会让检索到的片段难以引用。
    只有单个段落超过 ``size`` 时才退化为按长度切。
    """
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if not text:
        return []

    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            chunks.append(buf.strip())
        buf = ""

    for para in paras:
        if len(para) > size:
            flush()
            # 超长段落：按长度硬切，但仍保留 overlap
            start = 0
            while start < len(para):
                chunks.append(para[start:start + size].strip())
                start += size - overlap
            continue
        if len(buf) + len(para) + 2 <= size:
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            flush()
            # 新块带上上一块的尾部作为重叠，避免跨块论证被切断。
            # 注意 flush() 之后 buf 是空的，所以这里必须先取 tail 再赋值——
            # 早先把 tail 的赋值写在了 if 分支里，那条分支只在 buf 非空时走到，
            # 于是"刚 flush 完"这个最常见的情形反而拿不到重叠。
            tail = chunks[-1][-overlap:] if (chunks and overlap) else ""
            buf = (tail + "\n\n" + para).strip() if tail else para
    flush()

    # 去掉过短的碎块：一二十个字的块检索出来没有引用价值
    return [c for c in chunks if len(c) >= 30]


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------
@dataclass
class Chunk:
    doc_id: str
    doc_title: str
    index: int
    text: str
    score: float = 0.0
    page: int | None = None

    def citation(self) -> str:
        where = self.doc_title or self.doc_id
        if self.page:
            return f"{where}（第 {self.page} 页）"
        return f"{where}（第 {self.index + 1} 段）"

    def as_evidence(self) -> dict:
        """转成写作层能用的证据结构（与 websearch / gather 保持一致）。"""
        return {
            "title": self.doc_title or self.doc_id,
            "citation": self.citation(),
            # 全文来自作者自己的资料，标 A 层（用户提供）
            "text": self.text,
            "passages": [self.text],
            "claims": [],
            "source": "本地知识库",
            "type": "本地资料",
            "doi": "",
            "url": "",
            "verified": "",
            "verified_detail": "来自本地 RAG 知识库（作者自己导入的资料）",
            "origin": "kb",
            "doc_id": self.doc_id,
            "page": self.page,
            "score": self.score,
        }


def _connect(path: Path | None = None) -> sqlite3.Connection:
    _ensure_dir()
    conn = sqlite3.connect(str(path or KB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL 让"入库"与"检索"能并发，不至于一边投料一边查就锁死。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS docs (
            doc_id TEXT PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            origin TEXT NOT NULL DEFAULT '',
            chars INTEGER NOT NULL DEFAULT 0,
            chunks INTEGER NOT NULL DEFAULT 0,
            added_at REAL NOT NULL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            page INTEGER,
            text TEXT NOT NULL,
            tokens TEXT NOT NULL
        )""")
    # 外部内容表：chunks 存正文，FTS 只存索引，避免正文存两份
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            tokens, content='chunks', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        )""")
    conn.commit()
    return conn


def _doc_id(title: str, text: str) -> str:
    """按内容指纹生成 doc_id：同一份资料重复导入会覆盖，而不是堆两份。"""
    h = hashlib.sha1((title + "\n" + text[:5000]).encode("utf-8")).hexdigest()[:16]
    return f"doc-{h}"


def add_document(text: str, *, title: str = "", origin: str = "",
                 page: int | None = None) -> dict:
    """把一份资料切块入库。返回 ``{doc_id, chunks, chars}``。

    同一份内容重复入库时**先删后插**（按内容指纹去重），
    否则作者反复点"入库"会把同一份资料堆很多遍，检索结果全是重复。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "内容是空的。"}

    chunks = chunk_text(text)
    if not chunks:
        return {"ok": False, "error": "切不出可索引的段落（内容太短？）"}

    doc_id = _doc_id(title, text)
    now = time.time()
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM chunks_fts WHERE rowid IN "
                         "(SELECT rowid FROM chunks WHERE doc_id=?)", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
            conn.execute(
                "INSERT INTO docs(doc_id,title,origin,chars,chunks,added_at) VALUES(?,?,?,?,?,?)",
                (doc_id, title or "(未命名资料)", origin, len(text), len(chunks), now),
            )
            for i, c in enumerate(chunks):
                cur = conn.execute(
                    "INSERT INTO chunks(doc_id,idx,page,text,tokens) VALUES(?,?,?,?,?)",
                    (doc_id, i, page, c, tokenize(c)),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(rowid,tokens) VALUES(?,?)",
                    (cur.lastrowid, tokenize(c)),
                )
        return {"ok": True, "doc_id": doc_id, "chunks": len(chunks), "chars": len(text),
                "title": title or "(未命名资料)"}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"入库失败：{type(exc).__name__}: {exc}"}
    finally:
        conn.close()


def search(query: str, *, k: int = DEFAULT_K) -> list[Chunk]:
    """按关键词检索，返回按 BM25 排序的块。

    FTS5 的 ``bm25()`` 返回的是负数（越小越相关），这里取负让"分数越大越相关"，
    与项目里其它检索结果的方向一致——方向不一致的分数最容易让人误读。
    """
    tokens = tokenize(query)
    if not tokens.strip():
        return []
    conn = _connect()
    try:
        # 把查询词用 OR 连起来并让 FTS5 排序：AND 太严（少一个词就零结果），
        # 而这里的第一层只是**取候选**，精度由后面的重排负责。
        expr = " OR ".join(t for t in dict.fromkeys(tokens.split()) if t)
        if not expr:
            return []
        rows = conn.execute(
            """SELECT c.doc_id, c.idx, c.page, c.text, d.title,
                      -bm25(chunks_fts) AS score
               FROM chunks_fts
               JOIN chunks c ON c.rowid = chunks_fts.rowid
               JOIN docs d ON d.doc_id = c.doc_id
               WHERE chunks_fts MATCH ?
               ORDER BY score DESC
               LIMIT ?""",
            (expr, max(1, min(k * 4, 60))),
        ).fetchall()
    except sqlite3.Error:
        # 查询语法错误（用户输入里带了 FTS 特殊字符）不该让整个检索崩掉
        return []
    finally:
        conn.close()

    out = [Chunk(doc_id=r["doc_id"], doc_title=r["title"] or "", index=r["idx"],
                 text=r["text"], score=float(r["score"] or 0.0), page=r["page"])
           for r in rows]
    return out[:k]


def list_documents() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT doc_id,title,origin,chars,chunks,added_at FROM docs ORDER BY added_at DESC"
        ).fetchall()
        return [{"doc_id": r["doc_id"], "title": r["title"], "origin": r["origin"],
                 "chars": r["chars"], "chunks": r["chunks"],
                 "added_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["added_at"]))}
                for r in rows]
    finally:
        conn.close()


def stats() -> dict:
    conn = _connect()
    try:
        d = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(chars),0) c, "
                         "COALESCE(SUM(chunks),0) k FROM docs").fetchone()
        return {"ok": True, "documents": d["n"], "chars": d["c"], "chunks": d["k"],
                "path": str(KB_PATH),
                "ready": d["n"] > 0}
    finally:
        conn.close()


def delete_document(doc_id: str) -> dict:
    conn = _connect()
    try:
        with conn:
            n = conn.execute("SELECT COUNT(*) n FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()["n"]
            conn.execute("DELETE FROM chunks_fts WHERE rowid IN "
                         "(SELECT rowid FROM chunks WHERE doc_id=?)", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
        return {"ok": True, "removed_chunks": n}
    finally:
        conn.close()


def clear() -> dict:
    conn = _connect()
    try:
        with conn:
            n = conn.execute("SELECT COUNT(*) n FROM docs").fetchone()["n"]
            conn.execute("DELETE FROM chunks_fts")
            conn.execute("DELETE FROM chunks")
            conn.execute("DELETE FROM docs")
        return {"ok": True, "removed_documents": n}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 与写作层对接
# ---------------------------------------------------------------------------
def search_as_evidence(query: str, *, k: int = DEFAULT_K) -> tuple[list[dict], dict]:
    """检索并转成证据结构，供写作阶段直接引用。"""
    hits = search(query, k=k)
    info = {
        "query": query,
        "hits": len(hits),
        "limits": (
            "本地知识库走**关键词检索**（jieba 分词 + SQLite FTS5），不是语义检索："
            "查「教师心理状态」不会命中「职业倦怠」，除非两者共有词。"
            "所以查询词要尽量用资料里真实出现的说法。"
        ),
    }
    return [h.as_evidence() for h in hits], info


def relevance_prompt(query: str, candidates: list[Chunk]) -> list[dict]:
    """构造"让模型从候选里挑相关块"的提示词。

    这是关键词检索的**精度补丁**：FTS5 按词命中取回候选，但词命中不等于语义相关
    （查"组织支持"可能命中一堆只提到"组织"的段落）。让模型看过再挑，
    比单纯加阈值靠谱。它需要 Key，所以是可选的——不填 Key 时直接用检索结果。
    """
    listing = "\n".join(
        f"[{i}] （来源：{c.doc_title or c.doc_id}）{c.text[:400]}"
        for i, c in enumerate(candidates, start=1)
    )
    return [
        {
            "role": "system",
            "content": (
                "你在为一个写作任务筛选资料。下面有一组候选片段，"
                "请挑出**真正能支撑这个写作需求**的那些。\n"
                "只输出 JSON：{\"keep\":[编号,...],\"reason\":\"一句话说明为什么去掉其余\"}\n"
                "不要解释、不要 Markdown 围栏。宁可少留，也不要留只是字面命中但主题无关的片段。"
            ),
        },
        {"role": "user", "content": f"写作需求：{query}\n\n候选片段：\n{listing}"},
    ]
