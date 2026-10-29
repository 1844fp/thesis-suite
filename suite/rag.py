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
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import embedding

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
def _ensure_doc_columns(conn: sqlite3.Connection) -> None:
    """给老库补上 ``text`` / ``pages`` 两列。

    为什么必须有这两列（这条是本次改动的核心）：
    早先**只存切块**，整篇原文要靠"把块拼回去"重建。而切块之间有 80 字重叠，
    拼回来就把重叠复制了一遍——实测一篇 1330 字的资料取回变成 1494 字，
    关键句从 72 次变成 80 次。PaperQA2 的文献简报正是吃这份被污染的文本来生成的。
    所以原文必须**原样存一份**，切块只作为检索用的索引，不再兼任存储。

    用 PRAGMA 判断而不是 try/except：``ALTER TABLE ... ADD COLUMN`` 重复执行会报错，
    而"靠捕获异常来判断列是否存在"会把真正的写失败也一起吞掉。
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(docs)")}
    if "text" not in cols:
        conn.execute("ALTER TABLE docs ADD COLUMN text TEXT")
    if "pages" not in cols:
        conn.execute("ALTER TABLE docs ADD COLUMN pages TEXT")
    chunk_cols = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "page_end" not in chunk_cols:
        # 结束页码。跨页的块用「第 7–8 页」报引用，比只报起始页诚实。
        conn.execute("ALTER TABLE chunks ADD COLUMN page_end INTEGER")
    if "vec" not in chunk_cols:
        # 语义向量（float32 的裸字节）。NULL 表示这一块还没编码——
        # 检索时会退回关键词，等 ``backfill_vectors`` 补上后自动参与混合检索。
        conn.execute("ALTER TABLE chunks ADD COLUMN vec BLOB")


def _normalize(text: str) -> str:
    """入库前的统一归一：压掉 3 个以上连续换行、去首尾空白。

    必须与 :func:`chunk_spans` 用的是**同一份**归一化结果：偏移量是按归一化后的
    文本算的，如果调用方拿原文去对偏移，页码会整体错位。
    """
    return re.sub(r"\n{3,}", "\n\n", (text or "").strip())


def chunk_spans(text: str, *, size: int = CHUNK_CHARS,
                overlap: int = CHUNK_OVERLAP) -> list[tuple[int, str]]:
    """按段落边界切块，**同时回报每块在归一化文本里的起始偏移**。

    切块规则与早先完全一致（段落优先、超长段落硬切、块间留 overlap），
    唯一新增的是偏移量。偏移是给页码用的：PDF 每页在全文里的字符区间已知，
    有了块起点就能把「第几页」记到块上——引用才能精确到页，
    而这在"整篇入库"之后是唯一还能保住页码精度的办法。
    """
    norm = _normalize(text)
    if not norm:
        return []

    # 段落 + 各自在 norm 里的起点。游标只往前走，所以整体是 O(n)。
    paras: list[tuple[int, str]] = []
    cursor = 0
    for raw in re.split(r"\n\s*\n", norm):
        para = raw.strip()
        if not para:
            continue
        idx = norm.find(para, cursor)
        if idx < 0:
            idx = cursor
        paras.append((idx, para))
        cursor = idx + len(para)

    spans: list[tuple[int, str]] = []
    buf = ""
    buf_start = 0

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            spans.append((buf_start, buf.strip()))
        buf = ""

    for idx, para in paras:
        if len(para) > size:
            flush()
            # 超长段落：按长度硬切，仍保留 overlap
            off = idx
            while off < idx + len(para):
                piece = norm[off:off + size].strip()
                if piece:
                    spans.append((off, piece))
                off += size - overlap
            continue
        if len(buf) + len(para) + 2 <= size:
            if not buf:
                buf_start = idx
            buf = f"{buf}\n\n{para}" if buf else para
        else:
            flush()
            # 新块带上上一块的尾部作为重叠，避免跨块论证被切断。
            tail = spans[-1][1][-overlap:] if (spans and overlap) else ""
            if tail:
                # 重叠部分来自上一个块，所以这一块的"起点"要往回退 len(tail)。
                # 页码归属按这个起点判，误差不超过一个 overlap。
                buf_start = max(0, idx - len(tail))
                buf = (tail + "\n\n" + para).strip()
            else:
                buf_start = idx
                buf = para
    flush()

    # 去掉过短的碎块：一二十个字的块检索出来没有引用价值
    return [(s, c) for s, c in spans if len(c) >= 30]


def chunk_text(text: str, *, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落边界把长文切成块，块间留一点重叠。

    优先在段落边界切：学术文本的段落本身就是论证单元，
    从句子中间切开会让检索到的片段难以引用。
    只有单个段落超过 ``size`` 时才退化为按长度切。
    """
    return [c for _, c in chunk_spans(text, size=size, overlap=overlap)]


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
    #: 块结束处的页码。只有**跨页块**才有值且可能不等于 ``page``——
    #: 切块按字符数算，不按页算，所以一个 600 字的块从第 7 页跨到第 8 页很常见。
    #: 引用时按区间报（第 7–8 页），而不是只报起始页。
    page_end: int | None = None

    def citation(self) -> str:
        """引用串：出处 + 定位。

        页码有两种写法：
        * 块落在一页之内 → 「（第 7 页）」
        * 块跨页（切块按字符算，不按页算，跨页很正常）→ 「（第 7–8 页）」

        **跨页时不能只报起始页**：那等于告诉读者"只有第 7 页有这句话"，
        而实际支撑这个论断的内容有一半在第 8 页。宁可报区间，也不要报一个
        看起来精确的错答案。

        另外，**页码已经在标题里时不再补一次**：早先 PDF 是每页一篇入库的，
        标题被写成「题名（第 2 页）」，再补一遍就出现「（第 2 页）（第 2 页）」。
        """
        where = self.doc_title or self.doc_id
        if self.page:
            if f"第 {self.page} 页" in where:
                return where
            if self.page_end and self.page_end != self.page:
                return f"{where}（第 {self.page}–{self.page_end} 页）"
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
            added_at REAL NOT NULL,
            text TEXT,
            pages TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id TEXT NOT NULL,
            idx INTEGER NOT NULL,
            page INTEGER,
            page_end INTEGER,
            text TEXT NOT NULL,
            tokens TEXT NOT NULL,
            vec BLOB
        )""")
    # 文献级元数据（JSON 一行）。**单独一张表而不是给 docs 加列**：
    # 列一旦加了就要处理老库的迁移，而"老库没有这列"在 SQLite 里不会报错、
    # 只会静默拿到 NULL——那比没有还难查。单独一张表则天然向后兼容：
    # 老库没有这张表时 CREATE IF NOT EXISTS 会建出空表，读出来就是 {}。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS docs_meta (
            doc_id TEXT PRIMARY KEY,
            meta TEXT NOT NULL DEFAULT '{}'
        )""")
    # 外部内容表：chunks 存正文，FTS 只存索引，避免正文存两份
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
            tokens, content='chunks', content_rowid='rowid',
            tokenize='unicode61 remove_diacritics 2'
        )""")
    # 补列必须放在**两张表都建好之后**：老库升级与全新库都走这一条路径，
    # 放在 CREATE TABLE chunks 之前会在新库上报 "no such table: chunks"（实测踩过）。
    _ensure_doc_columns(conn)
    conn.commit()
    return conn


def _doc_id(title: str, text: str) -> str:
    """按内容指纹生成 doc_id：同一份资料重复导入会覆盖，而不是堆两份。"""
    h = hashlib.sha1((title + "\n" + text[:5000]).encode("utf-8")).hexdigest()[:16]
    return f"doc-{h}"


def _page_at(pages: list[dict] | None, offset: int) -> int | None:
    """块的起始偏移落在哪一页。``pages`` 是 ``[{"page": n, "start": s, "end": e}]``。

    找不到就退回"最后一页的起始页"——宁可给个近似页码，也不要把页码丢成 None，
    否则引用会退化成「第 N 段」，读者没法回查。
    """
    if not pages:
        return None
    for item in pages:
        try:
            if int(item["start"]) <= offset < int(item["end"]):
                return int(item["page"])
        except (KeyError, TypeError, ValueError):
            continue
    try:
        before = [p for p in pages if int(p.get("start", 0)) <= offset]
        return int(before[-1]["page"]) if before else int(pages[0]["page"])
    except Exception:  # noqa: BLE001
        return None


def add_document(text: str, *, title: str = "", origin: str = "",
                 page: int | None = None, meta: dict | None = None,
                 pages: list[dict] | None = None) -> dict:
    """把**一整篇**资料入库。返回 ``{doc_id, chunks, chars}``。

    同一份内容重复入库时**先删后插**（按内容指纹去重），
    否则作者反复点"入库"会把同一份资料堆很多遍，检索结果全是重复。

    ``meta`` 是可选的文献元数据（作者、年份、期刊、DOI、文献等级、被引…）。
    它有两个用处：一是参考文献推荐里选中的文献入库时把书目带进来，
    二是 PaperQA2 的语料在正文前会写一段"【文献信息】"，让引用有真实依据。

    ``pages`` 是"页码 → 字符区间"的映射，用来把页码记到切块上。
    **整篇入库 + 页映射**取代了早先的"每页当成一篇文档"：
    早先一篇 63 页的论文会变成 63 条文档记录，文档库没法看、也没法对整篇做简报。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "error": "内容是空的。"}

    spans = chunk_spans(text)
    if not spans:
        return {"ok": False, "error": "切不出可索引的段落（内容太短？）"}

    doc_id = _doc_id(title, text)
    now = time.time()
    pages_json = json.dumps(pages, ensure_ascii=False) if pages else None
    #: 本次插入的块 ``(rowid, 文本)``。向量要等事务提交后再补，
    #: 所以先把 rowid 记下来（见函数末尾"在事务之外做"的说明）。
    inserted: list[tuple[int, str]] = []
    conn = _connect()
    try:
        with conn:
            conn.execute("DELETE FROM chunks_fts WHERE rowid IN "
                         "(SELECT rowid FROM chunks WHERE doc_id=?)", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM docs_meta WHERE doc_id=?", (doc_id,))
            conn.execute(
                "INSERT INTO docs(doc_id,title,origin,chars,chunks,added_at,text,pages) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (doc_id, title or "(未命名资料)", origin, len(text), len(spans), now,
                 text, pages_json),
            )
            if meta:
                conn.execute("INSERT INTO docs_meta(doc_id,meta) VALUES(?,?)",
                             (doc_id, json.dumps(meta, ensure_ascii=False)))
            for i, (start, c) in enumerate(spans):
                # 有页映射就按块的字符区间判页；否则退回调用方给的单一页码。
                # 同时算出**结束页码**：块跨页时引用要写成「第 7–8 页」。
                if pages:
                    chunk_page = _page_at(pages, start)
                    chunk_page_end = _page_at(pages, start + max(len(c) - 1, 0))
                else:
                    chunk_page, chunk_page_end = page, page
                cur = conn.execute(
                    "INSERT INTO chunks(doc_id,idx,page,page_end,text,tokens) "
                    "VALUES(?,?,?,?,?,?)",
                    (doc_id, i, chunk_page, chunk_page_end, c, tokenize(c)),
                )
                conn.execute(
                    "INSERT INTO chunks_fts(rowid,tokens) VALUES(?,?)",
                    (cur.lastrowid, tokenize(c)),
                )
                inserted.append((int(cur.lastrowid), c))
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"入库失败：{type(exc).__name__}: {exc}"}
    finally:
        conn.close()

    # ---- 语义向量：**在事务之外**做 ----
    # 编码一篇 110 段的论文要几秒，放在写事务里会长时间持有写锁，
    # 期间并发的检索/入库全被挡住。所以先提交文本，再补向量。
    # 失败不影响入库——那些块的 vec 留 NULL，检索退回关键词，
    # 之后可以用 ``backfill_vectors()`` 补上。
    embedded = 0
    if inserted and embedding.ready():
        vectors = embedding.embed_in_batches([c for _, c in inserted], wait=True)
        if vectors and len(vectors) == len(inserted):
            conn2 = _connect()
            try:
                import struct
                with conn2:
                    for (rid, _), vec in zip(inserted, vectors):
                        blob = struct.pack(f"<{len(vec)}f", *vec)
                        conn2.execute("UPDATE chunks SET vec=? WHERE rowid=?", (blob, rid))
                embedded = len(inserted)
            except sqlite3.Error:
                embedded = 0
            finally:
                conn2.close()
            with _vec_lock:
                _vec_cache["key"] = None      # 新向量进来，矩阵缓存必须失效

    return {"ok": True, "doc_id": doc_id, "chunks": len(spans), "chars": len(text),
            "written": True, "pages": len(pages or []),
            "vectors": embedded,
            "title": title or "(未命名资料)"}


def _join_chunks(chunks: list[str]) -> str:
    """把切块拼回整篇，**去掉块间重叠**。

    只用于给"早先只存了切块"的老数据兜底重建。切块时每块开头放着上一块的
    最后 ``CHUNK_OVERLAP`` 个字，直接 ``"\\n\\n".join`` 会把这段重复一遍
    （实测 1330 字变 1494 字）。所以这里找"上一块的后缀 == 本块的前缀"的最长
    公共段并切掉。新数据不再走这条路——原文是原样存下来的。
    """
    out = ""
    for chunk in chunks:
        if not out:
            out = chunk
            continue
        limit = min(len(out), len(chunk), CHUNK_OVERLAP + 10)
        cut = 0
        for length in range(limit, 0, -1):
            if out.endswith(chunk[:length]):
                cut = length
                break
        out += "\n\n" + chunk[cut:]
    return out


def get_document(doc_id: str) -> dict | None:
    """按 doc_id 取回**整篇原文 + 元数据**。

    为什么需要它：检索（``search``）只返回命中的片段，而 PaperQA2 建索引、
    文献简报、跨文档对比都需要整篇文献。拿片段硬拼会让 PaperQA2 以为
    整篇文献只有那几百字，问答与简报的质量会明显下降。

    **优先读 ``docs.text``（入库时原样存下的全文）。** 只有老数据（那一列为空）
    才退回"把切块拼回去"，并且拼接时会去掉块间重叠。
    """
    doc_id = (doc_id or "").strip()
    if not doc_id:
        return None
    conn = _connect()
    try:
        head = conn.execute(
            "SELECT doc_id,title,origin,chars,chunks,added_at,text,pages "
            "FROM docs WHERE doc_id=?",
            (doc_id,),
        ).fetchone()
        if head is None:
            return None
        stored = (head["text"] or "").strip()
        if stored:
            full = stored
        else:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE doc_id=? ORDER BY idx", (doc_id,)
            ).fetchall()
            full = _join_chunks([r["text"] for r in rows])
        meta_row = conn.execute(
            "SELECT meta FROM docs_meta WHERE doc_id=?", (doc_id,)
        ).fetchone()
    finally:
        conn.close()

    try:
        meta = json.loads(meta_row["meta"]) if meta_row else {}
    except (TypeError, ValueError):
        meta = {}
    try:
        pages = json.loads(head["pages"]) if head["pages"] else []
    except (TypeError, ValueError):
        pages = []
    return {
        "doc_id": head["doc_id"],
        "title": head["title"],
        "origin": head["origin"],
        "chars": head["chars"],
        "chunks": head["chunks"],
        "added_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(head["added_at"])),
        "text": full,
        "pages": pages if isinstance(pages, list) else [],
        "meta": meta if isinstance(meta, dict) else {},
    }


def delete_documents(doc_ids: list[str]) -> dict:
    """**批量**删除文献。返回删掉了哪些、总共移除多少片段。

    单独做批量入口而不是让前端循环调单删除：一次 60 篇的批量操作如果发 60 个请求，
    中间任何一个失败都会留下"删了一半"的状态，而用户看不到哪一半成了。
    这里在一个事务里删完，并且**如实回报每一篇是否真的存在**——
    删不存在的 id 不算失败，但要让调用方知道它本来就不在。
    """
    ids = [d for d in dict.fromkeys(doc_ids or []) if d]
    if not ids:
        return {"ok": False, "error": "没有指定要删除的文献。", "removed": []}
    conn = _connect()
    removed: list[str] = []
    missing: list[str] = []
    chunks_total = 0
    try:
        with conn:
            for doc_id in ids:
                exists = conn.execute(
                    "SELECT COUNT(*) AS n FROM docs WHERE doc_id=?", (doc_id,)
                ).fetchone()["n"]
                if not exists:
                    missing.append(doc_id)
                    continue
                n = conn.execute(
                    "SELECT COUNT(*) AS n FROM chunks WHERE doc_id=?", (doc_id,)
                ).fetchone()["n"]
                conn.execute("DELETE FROM chunks_fts WHERE rowid IN "
                             "(SELECT rowid FROM chunks WHERE doc_id=?)", (doc_id,))
                conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
                conn.execute("DELETE FROM docs_meta WHERE doc_id=?", (doc_id,))
                removed.append(doc_id)
                chunks_total += n
        return {"ok": True, "removed": removed, "missing": missing,
                "removed_chunks": chunks_total, "stats": stats()}
    except sqlite3.Error as exc:
        return {"ok": False, "error": f"删除失败：{type(exc).__name__}: {exc}",
                "removed": removed}
    finally:
        conn.close()


def migrate(*, merge_pages: bool = True) -> dict:
    """把"只存切块"的老数据升级到"存整篇 + 页映射"。

    做两件事，都是可重复执行的（幂等）：

    1. **补全文**：``docs.text`` 为空的老记录，用切块去重叠拼回一篇写进去。
       这样 ``get_document`` 立刻能给出干净的整篇文本，PaperQA2 简报不必等到
       用户重新入库才变好。
    2. **合并按页拆开的文档**：早先 PDF 是**每页一篇文档**入库的
       （标题形如「题名（第 12 页）」）。这里按 ``origin`` + 基础题名分组，
       按页号升序拼成一整篇，并生成页映射。一篇 63 页的论文因此从 63 条记录
       收敛成 1 条——文档库才真正可用。

    不做的事：**不改动已有 FTS 索引的 token 结果**（它本来就从切块来，
    合并后重新切块会重建索引）。
    """
    conn = _connect()
    filled = 0
    merged_groups = 0
    merged_docs_removed = 0
    pending_merges: list[dict] = []
    try:
        rows = conn.execute(
            "SELECT doc_id,title,origin,chars,text FROM docs"
        ).fetchall()
        # ---- 1) 补全文 ----
        for row in rows:
            if (row["text"] or "").strip():
                continue
            chunks = [r["text"] for r in conn.execute(
                "SELECT text FROM chunks WHERE doc_id=? ORDER BY idx", (row["doc_id"],)
            ).fetchall()]
            if not chunks:
                continue
            full = _join_chunks(chunks)
            if not full.strip():
                continue
            conn.execute("UPDATE docs SET text=? WHERE doc_id=?", (full, row["doc_id"]))
            filled += 1
        conn.commit()

        if merge_pages:
            # ---- 2) 按页拆开的文档按 origin 分组 ----
            page_re = re.compile(r"^(.*?)（第\s*(\d+)\s*页）\s*$")
            groups: dict[tuple[str, str], list[dict]] = {}
            for row in rows:
                m = page_re.match(row["title"] or "")
                if not m:
                    continue
                base, page_no = m.group(1).strip(), int(m.group(2))
                groups.setdefault((row["origin"] or "", base), []).append(
                    {"doc_id": row["doc_id"], "page": page_no})
            for (origin, base), items in groups.items():
                if len(items) < 2:
                    continue
                items.sort(key=lambda x: x["page"])
                pending_merges.append({"origin": origin, "base": base, "items": items})
    finally:
        conn.close()

    # 合并要写库，单独做（不在上面的读连接里混着写）
    for group in pending_merges:
        parts: list[str] = []
        pages: list[dict] = []
        cursor = 0
        for item in group["items"]:
            doc = get_document(item["doc_id"])
            body = (doc or {}).get("text") or ""
            if not body.strip():
                continue
            if parts:
                parts.append("")
                cursor += 1  # 空行占 1 个字符
            pages.append({"page": item["page"], "start": cursor,
                          "end": cursor + len(body)})
            parts.append(body)
            cursor += len(body)
        merged_text = "\n".join(parts).strip()
        if len(merged_text) < 60:
            continue
        title = group["base"] or group["origin"] or "合并文献"
        res = add_document(merged_text, title=title, origin=group["origin"],
                           pages=pages,
                           meta={"from": "migrate-merge", "merged_pages": len(pages)})
        if not res.get("ok"):
            continue
        delete_documents([item["doc_id"] for item in group["items"]])
        merged_groups += 1
        merged_docs_removed += len(group["items"])

    return {
        "ok": True,
        "text_filled": filled,
        "merged_groups": merged_groups,
        "page_docs_removed": merged_docs_removed,
        "stats": stats(),
    }


def documents_meta(doc_ids: list[str]) -> dict[str, dict]:
    """批量取元数据（doc_id → meta），供界面一次渲染多篇而不逐篇查库。"""
    ids = [d for d in dict.fromkeys(doc_ids or []) if d]
    if not ids:
        return {}
    conn = _connect()
    try:
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT doc_id, meta FROM docs_meta WHERE doc_id IN ({placeholders})", ids
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, dict] = {}
    for row in rows:
        try:
            out[row["doc_id"]] = json.loads(row["meta"])
        except (TypeError, ValueError):
            out[row["doc_id"]] = {}
    return out


#: RRF（Reciprocal Rank Fusion）的平滑常数。取 60 是原论文的推荐值：
#: 它让"第 1 名"与"第 2 名"的差距不至于过大，两路候选的贡献也不会被单路垄断。
RRF_K = 60

#: 每一路召回取多少候选再去融合。取 k*4 与纯关键词时的候选量一致，
#: 这样"融合"不是在给向量路额外加宽召回面，而是在同一召回面上换排序依据。
_RECALL_MULT = 4


def _fts_rids(conn: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """关键词路：FTS5 按 BM25 取候选的 rowid。**这一路永远是保底**，没有它就没有召回下限。"""
    tokens = tokenize(query)
    if not tokens.strip():
        return []
    expr = " OR ".join(t for t in dict.fromkeys(tokens.split()) if t)
    if not expr:
        return []
    try:
        rows = conn.execute(
            """SELECT chunks_fts.rowid AS rid
               FROM chunks_fts
               JOIN chunks c ON c.rowid = chunks_fts.rowid
               WHERE chunks_fts MATCH ?
               ORDER BY -bm25(chunks_fts) DESC
               LIMIT ?""",
            (expr, max(1, limit)),
        ).fetchall()
    except sqlite3.Error:
        # 查询语法错误（用户输入里带了 FTS 特殊字符）不该让整个检索崩掉
        return []
    return [int(r["rid"]) for r in rows]


#: 向量矩阵的内存缓存：``(版本, rids, matrix)``。
#: 每次检索都从 SQLite 读 11 万 × 512 的 float32 会慢到不可用，所以缓存成
#: numpy 矩阵；版本号用"已编码块数"，一旦补齐或新增就自动失效重建。
_vec_cache: dict = {"key": None, "rids": None, "mat": None}
_vec_lock = threading.Lock()


def _load_vector_matrix(conn: sqlite3.Connection):
    """把所有已编码块的向量加载成一个归一化矩阵。**没有 numpy 就返回 None。**"""
    try:
        import numpy as np
    except Exception:  # noqa: BLE001 - 缺 numpy 就退回纯关键词，这是预期降级
        return None

    try:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS m FROM chunks WHERE vec IS NOT NULL"
        ).fetchone()
        count = int(row["n"] or 0)
        max_rid = int(row["m"] or 0)
    except sqlite3.Error:
        return None
    if not count:
        return None

    key = (count, max_rid)
    with _vec_lock:
        if _vec_cache["key"] == key and _vec_cache["mat"] is not None:
            return _vec_cache["rids"], _vec_cache["mat"], np

    rows = conn.execute(
        "SELECT rowid AS rid, vec FROM chunks WHERE vec IS NOT NULL ORDER BY rowid"
    ).fetchall()
    rids: list[int] = []
    vectors: list = []
    dim = 0
    for r in rows:
        blob = r["vec"]
        if not blob:
            continue
        arr = np.frombuffer(blob, dtype=np.float32)
        if dim and arr.size != dim:
            # 换了向量模型（维度变了）时，旧向量不能混进同一个矩阵。
            continue
        dim = arr.size
        rids.append(int(r["rid"]))
        vectors.append(arr)
    if not vectors:
        return None
    mat = np.vstack(vectors).astype(np.float32)
    # 归一化：入库时已归一到单位长度，这里再保险一次，避免余弦退化成点积。
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms

    with _vec_lock:
        _vec_cache["key"] = key
        _vec_cache["rids"] = rids
        _vec_cache["mat"] = mat
        return rids, mat, np


def _vector_rids(conn: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """向量路：把查询编码后与全库向量做余弦，取 top-N 的 rowid。

    **不可用时返回空表**，调用方据此退化为纯关键词。这里刻意不抛异常：
    语义检索是增强，不是必须。
    """
    loaded = _load_vector_matrix(conn)
    if loaded is None:
        return []
    rids, mat, np = loaded
    try:
        qvec = embedding.embed([query])
    except Exception:  # noqa: BLE001 - 向量服务异常不能影响检索
        qvec = None
    if not qvec:
        return []
    q = np.asarray(qvec[0], dtype=np.float32)
    if q.size != mat.shape[1]:
        # 查询向量与库存向量维度不一致（换了模型），这一路直接放弃，
        # 而不是给出一个数学上无意义的相似度。
        return []
    n = q.size and (q / (np.linalg.norm(q) or 1.0))
    sims = mat @ n
    top = min(int(limit), sims.shape[0])
    if top <= 0:
        return []
    idx = np.argpartition(-sims, top - 1)[:top]
    idx = idx[np.argsort(-sims[idx])]
    return [rids[int(i)] for i in idx]


def _fuse_rrf(kw: list[int], vec: list[int], *, k: int) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion：``score(d) = Σ 1 / (RRF_K + rank_i(d))``。

    为什么用 RRF 而不是"把 BM25 分数和余弦分数加权平均"：
    两路的分数**量纲完全不同**（BM25 是负数取反后的任意正值，余弦是 0~1），
    归一化方式会直接决定谁主导排序，而那种归一化没有客观依据。
    RRF 只看**名次**，对量纲免疫，这也是它成为检索融合默认做法的原因。

    **并列时必须让语义路优先。** RRF 有一个真实弱点：当两路列表恰好互为逆序时，
    每个条目的名次之和相同，RRF 分会**完全相等**——实测题「教师心理压力」就是这样：
    关键词把"量表信效度"排第一、向量把"职业倦怠"排第一，融合后并列 0.0325，
    于是谁第一由排序算法的稳定性决定，而不是由相关性决定。
    余弦相似度本身是分得开的（0.650 vs 0.563），信息并没有丢，只是被 RRF 抹平了。
    所以这里加一条**确定的**并列规则：RRF 分相同时，向量名次更靠前者优先。
    这不是为了偏向向量，而是因为"加向量路"的整个目的就是让语义参与排序；
    并列时退回关键词，等于白加。
    """
    scores: dict[int, float] = {}
    vec_rank: dict[int, int] = {}
    kw_rank: dict[int, int] = {}
    for rank, rid in enumerate(kw, start=1):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)
        kw_rank[rid] = rank
    for rank, rid in enumerate(vec, start=1):
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (RRF_K + rank)
        vec_rank[rid] = rank

    big = 10 ** 6
    ranked = sorted(
        scores.items(),
        key=lambda kv: (-kv[1], vec_rank.get(kv[0], big), kw_rank.get(kv[0], big)),
    )
    return ranked[:k]


def search(query: str, *, k: int = DEFAULT_K) -> list[Chunk]:
    """检索知识库：**关键词（FTS5）+ 向量（bge）→ RRF 融合**。

    两种模式，行为差异只在**召回与排序**，返回结构与调用方式完全一致：

    * ``hybrid``：向量服务已就绪。两路各取候选后融合，能命中"说法不同但意思相同"的段落
      （例如查「组织支持」命中讲「职业倦怠」的那段）。
    * ``keyword``：向量不可用（公网容器、模型未缓存、或还在预热）。
      退化为纯 FTS5，与改动前完全一致。

    ``search_mode()`` 会告诉调用方当前是哪一种——**界面必须如实显示**，
    否则用户会以为"语义检索开着"，而实际只是关键词匹配。
    """
    if not (query or "").strip():
        return []
    limit = max(1, min(k * _RECALL_MULT, 60))
    conn = _connect()
    try:
        kw = _fts_rids(conn, query, limit)
        vec = _vector_rids(conn, query, limit)
        if vec:
            fused = _fuse_rrf(kw, vec, k=max(k, limit))
            ordered = [rid for rid, _ in fused][:k]
            score_of = {rid: s for rid, s in fused}
        else:
            ordered = kw[:k]
            score_of = {}
        if not ordered:
            return []
        placeholders = ",".join("?" for _ in ordered)
        rows = conn.execute(
            f"""SELECT c.rowid AS rid, c.doc_id, c.idx, c.page, c.page_end, c.text, d.title
                FROM chunks c JOIN docs d ON d.doc_id = c.doc_id
                WHERE c.rowid IN ({placeholders})""",
            ordered,
        ).fetchall()
    finally:
        conn.close()

    by_rid = {int(r["rid"]): r for r in rows}
    out: list[Chunk] = []
    for rid in ordered:
        r = by_rid.get(rid)
        if r is None:
            continue
        out.append(Chunk(
            doc_id=r["doc_id"], doc_title=r["title"] or "", index=r["idx"],
            text=r["text"], page=r["page"], page_end=r["page_end"],
            # 混合模式下给的是 RRF 融合分（越大越好），关键词模式给 BM25 取反值。
            score=float(score_of.get(rid, 0.0)),
        ))
    return out


def search_mode() -> str:
    """当前检索模式：``hybrid`` 或 ``keyword``。界面据此如实显示。"""
    try:
        return "hybrid" if embedding.ready() else "keyword"
    except Exception:  # noqa: BLE001
        return "keyword"


def list_documents() -> list[dict]:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT doc_id,title,origin,chars,chunks,added_at FROM docs ORDER BY added_at DESC"
        ).fetchall()
        metas = {r["doc_id"]: r["meta"] for r in
                 conn.execute("SELECT doc_id, meta FROM docs_meta").fetchall()}
        # 每篇已编码的块数：界面据此显示"这篇参与语义检索了吗"。
        vecs = {r["doc_id"]: int(r["n"]) for r in conn.execute(
            "SELECT doc_id, COUNT(*) AS n FROM chunks WHERE vec IS NOT NULL GROUP BY doc_id"
        ).fetchall()}
        out = []
        for r in rows:
            try:
                meta = json.loads(metas.get(r["doc_id"]) or "{}")
            except (TypeError, ValueError):
                meta = {}
            out.append({"doc_id": r["doc_id"], "title": r["title"], "origin": r["origin"],
                        "chars": r["chars"], "chunks": r["chunks"],
                        "chunks_embedded": vecs.get(r["doc_id"], 0),
                        "meta": meta if isinstance(meta, dict) else {},
                        "added_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(r["added_at"]))})
        return out
    finally:
        conn.close()


def stats() -> dict:
    conn = _connect()
    try:
        d = conn.execute("SELECT COUNT(*) n, COALESCE(SUM(chars),0) c, "
                         "COALESCE(SUM(chunks),0) k FROM docs").fetchone()
        v = conn.execute("SELECT COUNT(*) n FROM chunks WHERE vec IS NOT NULL").fetchone()
        mode = search_mode()
        return {"ok": True, "documents": d["n"], "chars": d["c"], "chunks": d["k"],
                "path": str(KB_PATH),
                # 语义索引的覆盖情况：已编码块数 / 总块数。三项都要给，
                # 否则前端没法区分"全库都编码了"和"一条都没编码"。
                "vectors": int(v["n"] or 0),
                "vectors_pending": max(0, int(d["k"] or 0) - int(v["n"] or 0)),
                "search_mode": mode,
                "ready": d["n"] > 0}
    finally:
        conn.close()


def backfill_vectors(*, limit: int = 0) -> dict:
    """给还没编码的块补向量。**幂等**，可以反复调用（只处理 vec IS NULL 的）。

    ``limit`` 为 0 表示全部补齐；否则只补前 N 块（自检脚本用它控制耗时）。
    """
    if not embedding.ready():
        return {"ok": False, "error": "语义检索未就绪（向量模型不可用或仍在预热）。",
                "embedded": 0, "pending": stats().get("vectors_pending", 0)}

    conn = _connect()
    try:
        sql = "SELECT rowid AS rid, text FROM chunks WHERE vec IS NULL ORDER BY rowid"
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql).fetchall()
    finally:
        conn.close()

    if not rows:
        return {"ok": True, "embedded": 0, "pending": 0, "note": "所有块都已有向量。"}

    texts = [r["text"] for r in rows]
    vectors = embedding.embed_in_batches(texts, wait=True)
    if not vectors or len(vectors) != len(rows):
        return {"ok": False, "error": "编码失败（向量服务返回不完整）。",
                "embedded": 0, "pending": len(rows)}

    import struct
    conn = _connect()
    try:
        with conn:
            for row, vec in zip(rows, vectors):
                conn.execute("UPDATE chunks SET vec=? WHERE rowid=?",
                             (struct.pack(f"<{len(vec)}f", *vec), int(row["rid"])))
    finally:
        conn.close()
    with _vec_lock:
        _vec_cache["key"] = None      # 向量变化 → 缓存失效

    return {"ok": True, "embedded": len(rows),
            "pending": stats().get("vectors_pending", 0)}


def delete_document(doc_id: str) -> dict:
    conn = _connect()
    try:
        with conn:
            n = conn.execute("SELECT COUNT(*) n FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()["n"]
            conn.execute("DELETE FROM chunks_fts WHERE rowid IN "
                         "(SELECT rowid FROM chunks WHERE doc_id=?)", (doc_id,))
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM docs WHERE doc_id=?", (doc_id,))
            conn.execute("DELETE FROM docs_meta WHERE doc_id=?", (doc_id,))
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
            conn.execute("DELETE FROM docs_meta")
        return {"ok": True, "removed_documents": n}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 与写作层对接
# ---------------------------------------------------------------------------
def search_as_evidence(query: str, *, k: int = DEFAULT_K) -> tuple[list[dict], dict]:
    """检索并转成证据结构，供写作阶段直接引用。"""
    hits = search(query, k=k)
    mode = search_mode()
    # 这段说明**必须随模式变**。语义检索没就绪时还写"支持语义"就是骗人；
    # 就绪时还写"只做关键词"又低估了结果，用户会按错的方式去改写查询词。
    limits = (
        "本地知识库走**混合检索**：关键词（jieba + FTS5）与语义向量（bge-small-zh）"
        "两路召回后用 RRF 融合。所以换个说法也能命中意思相同的段落"
        "（查「组织支持」可以命中讲「职业倦怠」的那段）；但它是片段级召回，"
        "不是问答，跨段落的多跳推理仍需「文献简报」或「问答」。"
        if mode == "hybrid" else
        "本地知识库现在走**关键词检索**（jieba 分词 + SQLite FTS5），语义向量未就绪："
        "查「教师心理状态」不会命中「职业倦怠」，除非两者共有词。"
        "所以查询词要尽量用资料里真实出现的说法。"
    )
    info = {
        "query": query,
        "hits": len(hits),
        "mode": mode,
        "limits": limits,
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
