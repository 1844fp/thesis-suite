"""RAG 知识库接口 + 网站板块清单。

知识库是"定题目"板块的一部分（参考站的说法是"参考文献查得到出处"），
所以它的接口独立于 agent 动作之外——入库与检索是**纯本地操作**，
不该被 agent 的凭据/事件那套包装挡住，前端也更容易直接用。
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

from .. import fileintake, modules as modules_mod, rag

router = APIRouter(prefix="/api", tags=["知识库与板块"])


# ---------------------------------------------------------------------------
# 板块清单
# ---------------------------------------------------------------------------
@router.get("/modules")
def list_modules() -> dict:
    """六个板块的定义。前端读它渲染导航，改板块结构只改 suite/modules.py。"""
    return {"ok": True, "modules": modules_mod.describe()}


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------
@router.get("/kb/stats")
def kb_stats() -> dict:
    """知识库规模。**不需要 Key**。"""
    return rag.stats()


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


@router.post("/kb/upload")
async def kb_upload(files: list[UploadFile] = File(default_factory=list)) -> dict:
    """上传文件直接入库。**不需要 Key**：解析与索引都是本地的。

    支持 PDF / Word / Markdown / 文本 / HTML / CSV（复用 fileintake 的解析层）。
    PDF 会**按页入库**，这样检索到的片段能带上页码，与"开题报告"板块的证据一致。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件。")
    if len(files) > fileintake.MAX_FILES:
        raise HTTPException(status_code=413,
                            detail=f"一次最多 {fileintake.MAX_FILES} 个文件。")

    added: list[dict] = []
    failed: list[dict] = []
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

        doc = fileintake.extract(name, data)
        if not doc.ok:
            failed.append({"filename": name, "error": doc.error or doc.warning or "解析不出文字"})
            continue

        title = fileintake.guess_title(doc, fallback=name)
        # PDF 逐页入库：页码是引用的一部分，与资料获取板块的取舍保持一致。
        if doc.chunks and doc.kind == "PDF":
            total = 0
            for chunk in doc.chunks:
                page_text = str(chunk.get("text") or "")
                if len(page_text.strip()) < 30:
                    continue
                r = rag.add_document(page_text, title=f"{title}（第 {chunk.get('page')} 页）",
                                     origin=name, page=chunk.get("page"))
                if r.get("ok"):
                    total += 1
            added.append({"filename": name, "kind": doc.kind, "pages": doc.pages,
                          "chunks": total, "chars": len(doc.text)})
        else:
            r = rag.add_document(doc.text, title=title, origin=name)
            if r.get("ok"):
                added.append({"filename": name, "kind": doc.kind, "chunks": r.get("chunks"),
                              "chars": r.get("chars")})
            else:
                failed.append({"filename": name, "error": r.get("error") or "入库失败"})

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
        "limits": info.get("limits"),
    }


@router.delete("/kb/doc")
def kb_delete(doc_id: str) -> dict:
    if not doc_id:
        raise HTTPException(status_code=400, detail="缺少 doc_id。")
    return rag.delete_document(doc_id)


@router.delete("/kb/all")
def kb_clear() -> dict:
    """清空整个知识库。公网服务要允许访客立刻抹掉自己的数据。"""
    return rag.clear()
