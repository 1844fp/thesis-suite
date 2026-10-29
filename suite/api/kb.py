"""RAG 知识库接口 + 网站板块清单。

知识库是"定题目"板块的一部分（参考站的说法是"参考文献查得到出处"），
所以它的接口独立于 agent 动作之外——入库与检索是**纯本地操作**，
不该被 agent 的凭据/事件那套包装挡住，前端也更容易直接用。
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from .. import fileintake, modules as modules_mod, rag
from .. import embedding as embedding_mod

router = APIRouter(prefix="/api", tags=["知识库与板块"])


# ---------------------------------------------------------------------------
# 板块清单
# ---------------------------------------------------------------------------
@router.get("/modules")
def list_modules() -> dict:
    """六个板块的定义（**只读接口**，供前端/外部程序取用）。

    注意：导航**不是**读这个接口渲染的——``web/app.js`` 里另有一份 ``STAGES`` 常量，
    前端直接用本地常量渲染（避免首屏等一次请求）。两份定义是同一个概念的两个副本，
    改板块名/描述时**必须同时改** ``suite/modules.py`` 与 ``web/app.js``，
    否则会出现"导航显示 A、接口返回 B"的漂移。
    """
    return {"ok": True, "modules": modules_mod.describe()}


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------
@router.get("/kb/stats")
def kb_stats() -> dict:
    """知识库规模。**不需要 Key**。

    除规模外还如实给出"语义索引覆盖情况"与"当前检索模式"——
    界面必须能分清**混合检索**与**纯关键词**，否则用户会按错的方式改写查询词。
    """
    st = rag.stats()
    st["embed"] = embedding_mod.status()
    return st


@router.get("/kb/embed/status")
def kb_embed_status() -> dict:
    """语义检索状态（模型是否就绪、维度、当前模式）。**不需要 Key**。"""
    st = embedding_mod.status()
    st["coverage"] = {
        "vectors": rag.stats().get("vectors", 0),
        "pending": rag.stats().get("vectors_pending", 0),
    }
    return {"ok": True, **st}


@router.post("/kb/embed/warmup")
def kb_embed_warmup() -> dict:
    """触发一次后台预热（幂等）。冷启动要 30~40 秒，所以**不等它**，立刻返回。"""
    return {"ok": True, **embedding_mod.warmup(wait=False),
            "note": "预热在后台进行；完成后检索会自动升级为混合检索。"}


@router.post("/kb/embed/backfill")
async def kb_embed_backfill(request: Request) -> dict:
    """给还没编码的块补向量。请求体可带 ``{"limit": N}`` 只补前 N 块。

    **需要对每块跑一次编码**，一篇 110 段的论文约几秒；全库补齐可能要几十秒。
    """
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 允许空 body
        body = {}
    try:
        limit = int(body.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0
    return rag.backfill_vectors(limit=max(0, limit))


@router.get("/kb/docs")
def kb_docs() -> dict:
    return {"ok": True, "documents": rag.list_documents()}


@router.post("/kb/add")
async def kb_add(request: Request) -> dict:
    """把一段文本加进知识库。"""
    body = await request.json()
    text = str(body.get("text") or "")
    if len(text.strip()) < 30:
        raise HTTPException(status_code=400, detail="内容太短，至少需要 30 个字。")
    result = rag.add_document(text, title=str(body.get("title") or ""),
                              origin=str(body.get("origin") or "手动录入"),
                              page=body.get("page"))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "入库失败")
    return result


#: 单个请求最多收几个文件。**这不是"一次能导入多少"的上限**——
#: 前端会把用户选的一大批文件拆成若干批依次上传，所以用户可以把 200 篇一起拖进来。
#: 这里限制的只是"一批"的大小，用来给单个 HTTP 请求的体积与耗时封顶。
MAX_FILES_PER_BATCH = 25

#: 单批累计字节上限。25 MB × 25 个文件理论上是 625 MB，一次读进内存会撑爆，
#: 所以再加一道总闸：超过就拒绝本批，让前端拆得更小重试。
MAX_BATCH_BYTES = 120 * 1024 * 1024


@router.post("/kb/upload")
async def kb_upload(files: list[UploadFile] = File(default_factory=list)) -> dict:
    """上传文件直接入库。**不需要 Key**：解析与索引都是本地的。

    支持 PDF / Word / Markdown / 文本 / HTML / CSV（复用 fileintake 的解析层）。

    **一篇文件 = 一篇文献**（本次改动的关键）。早先是 PDF **逐页入库**，
    一篇 63 页的论文会变成 63 条文档记录：文档库没法看、没法对整篇做简报、
    也没法"选中这篇论文"再去问它跨页的问题。

    现在整篇入库，页码通过 ``pages`` 映射保留在**每个切块**上——
    所以引用仍然能精确到页（跨页块会写成「第 7–8 页」），
    而文献本身是一个完整对象。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件。")
    if len(files) > MAX_FILES_PER_BATCH:
        raise HTTPException(
            status_code=413,
            detail=f"单批最多 {MAX_FILES_PER_BATCH} 个文件（不是总数上限："
                   f"一次选更多时前端会自动分批上传）。")

    added: list[dict] = []
    failed: list[dict] = []
    total_bytes = 0
    for upload in files:
        name = upload.filename or "未命名"
        try:
            data = await upload.read()
        except Exception as exc:  # noqa: BLE001
            failed.append({"filename": name, "error": f"读取失败：{exc}"})
            continue
        finally:
            try:
                await upload.close()
            except Exception:  # noqa: BLE001
                pass

        total_bytes += len(data)
        if total_bytes > MAX_BATCH_BYTES:
            failed.append({"filename": name,
                           "error": f"本批累计超过 {MAX_BATCH_BYTES // 1024 // 1024} MB，"
                                    f"已停止处理（请把它拆成更小的批次）。"})
            break

        doc = fileintake.extract(name, data)
        if not doc.ok:
            failed.append({"filename": name, "error": doc.error or doc.warning or "解析不出文字"})
            continue

        title = fileintake.guess_title(doc, fallback=name)

        # ---- 整篇入库 + 页映射 ----
        # PDF 的 doc.text 是各页按顺序拼起来的；这里一次性算出"每页在全文里的
        # 字符区间"，交给 add_document，由它把页码记到每个切块上。
        text = doc.text
        pages: list[dict] = []
        if doc.kind == "PDF" and doc.chunks:
            pos = 0
            for chunk in doc.chunks:
                page_text = str(chunk.get("text") or "")
                if not page_text:
                    continue
                start = text.find(page_text, pos)
                if start < 0:
                    # 拼接口径与解析层不完全一致时退回"顺序累加"，页码仍是递增正确的。
                    start = pos
                pages.append({"page": chunk.get("page"), "start": start,
                              "end": start + len(page_text)})
                pos = start + len(page_text)

        r = rag.add_document(text, title=title, origin=name, pages=pages or None)
        if not r.get("ok"):
            failed.append({"filename": name, "error": r.get("error") or "入库失败"})
            continue
        added.append({
            "filename": name, "kind": doc.kind, "pages": doc.pages,
            "chunks": r.get("chunks"), "chars": r.get("chars"),
            "doc_id": r.get("doc_id"),
            # 页码映射生效的页数，前端据此说明"引用能精确到页"。
            "page_map": len(pages),
            "warning": (doc.warning or "") if not doc.warning else doc.warning,
        })

    if not added and failed:
        raise HTTPException(status_code=422,
                            detail="；".join(f"{f['filename']}: {f['error']}" for f in failed[:4]))
    return {"ok": True, "added": added, "failed": failed,
            "stats": rag.stats(),
            "note": "已入库的资料可在「写初稿」阶段作为依据引用；检索走关键词匹配。"}


@router.post("/kb/search")
async def kb_search(request: Request) -> dict:
    """检索知识库。返回的片段带来源与位置，可直接作为写作依据。"""
    body = await request.json()
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请填写查询词。")
    try:
        k = max(1, min(int(body.get("k") or rag.DEFAULT_K), 20))
    except (TypeError, ValueError):
        k = rag.DEFAULT_K

    evidence, info = rag.search_as_evidence(query, k=k)
    hits = [h.as_evidence() for h in rag.search(query, k=k)]
    return {
        "ok": True,
        "query": query,
        "hits": hits,
        "evidence": evidence,
        "count": len(hits),
        # 前端要据此显示"这次是混合检索还是纯关键词"。**不能默认写成混合**——
        # 语义服务没就绪时那样写就是在骗用户，他会按"语义检索"的方式去改写查询词。
        "mode": info.get("mode") or rag.search_mode(),
        "limits": info.get("limits"),
    }


@router.delete("/kb/doc")
def kb_delete(doc_id: str) -> dict:
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id。")
    return rag.delete_document(doc_id)


@router.post("/kb/delete-batch")
async def kb_delete_batch(request: Request) -> dict:
    """**批量**删除文献。请求体 ``{"doc_ids": [...]}``。

    前端「删除选中」与「清空全部」都走这一个入口：一次请求在一个事务里删完，
    失败时不会留下"删了一半"的状态（前端循环调单删除就会有这个问题）。
    返回里如实给出 removed / missing / remained，前端据此显示准确结果。
    """
    body = await request.json()
    ids = body.get("doc_ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.replace("，", ",").split(",")]
    ids = [str(x).strip() for x in ids if str(x).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="没有指定要删除的文献（doc_ids 为空）。")
    if len(ids) > 500:
        raise HTTPException(status_code=413, detail="一次最多删除 500 篇。")
    return rag.delete_documents(ids)


@router.delete("/kb/all")
def kb_clear() -> dict:
    """清空整个知识库。公网服务要允许访客立刻抹掉自己的数据。"""
    return rag.clear()
