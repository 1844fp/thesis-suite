"""PaperQA2 文献简报与问答 · 路由。

这是「开题报告」阶段的第二件事：把选中的文献变成一个**能问、能出简报**的知识库。

三条接口：
* ``GET  /status`` —— 组件是否可用、索引建了没有。**不需要 Key**。
* ``POST /ask``    —— 对选中文献提问，**SSE 流式**。需要 Key。
* ``POST /brief``  —— 选中文献的简报（单篇）或对比综述（多篇）。需要 Key。

为什么 ``/ask`` 是 SSE 而不是普通 JSON
--------------------------------------
一次 PaperQA2 问答要 10~40 秒（逐片段做上下文摘要再重排，这是它比普通 RAG 强的
原因，也是它慢的原因）。普通 JSON 接口在这 40 秒里给不出任何反馈，用户会以为卡死，
然后重复点——而每一次点击都是一次完整的模型调用，等于把"以为卡死"变成"重复花钱"。
流式把进度、检索到的来源、逐字答案都推给前端，这个代价是值得的。

**流式接口的凭据同样只从请求头取**，并且只传给子进程的 env。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .. import credentials as creds_mod
from .. import paperqa as pqa
from .. import rag

router = APIRouter(prefix="/api/pqa", tags=["PaperQA2 文献简报"])

#: 一次最多选几篇文献。上限来自 suite/paperqa.py 的 MAX_DOCS，
#: 这里只是提前拒绝并给出可读的错误，避免把几十篇丢进去让用户白等。
MAX_DOCS = pqa.MAX_DOCS

#: 单次调用的子进程超时（秒）。建索引时每篇文献要调一次模型，
#: 20 篇在慢网络下可能十几分钟，所以给得比较宽。
TIMEOUT = 1800.0


def _creds(request: Request) -> creds_mod.Credentials:
    try:
        return creds_mod.from_headers(request.headers)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


async def _doc_ids(request: Request, body: dict) -> list[str]:
    """取出并校验要处理的文献 id。

    **必须显式指定**，不给"默认全库"这种方便：全库可能有几十篇无关文献，
    一次全索引既慢又贵，而且问答会被无关文献污染。
    """
    ids = body.get("doc_ids") or []
    if isinstance(ids, str):
        ids = [x.strip() for x in ids.replace("，", ",").split(",")]
    ids = [str(x).strip() for x in ids if str(x).strip()]
    if not ids:
        raise HTTPException(status_code=400, detail="请先选择要处理的文献（doc_ids 为空）。")
    if len(ids) > MAX_DOCS:
        raise HTTPException(status_code=413,
                            detail=f"一次最多处理 {MAX_DOCS} 篇文献（当前 {len(ids)} 篇）。")

    known = {d["doc_id"] for d in rag.list_documents()}
    unknown = [d for d in ids if d not in known]
    if unknown:
        raise HTTPException(status_code=404,
                            detail=f"知识库里找不到这些文献（可能已被删除）：{'、'.join(unknown[:5])}")
    return ids


@router.get("/status")
def status() -> dict:
    """PaperQA2 组件状态。**不需要 Key**——它只报告环境与索引状况。"""
    st = pqa.status()
    st["index"] = pqa.stats()
    return st


@router.post("/ask")
async def ask(request: Request) -> StreamingResponse:
    """对选中的文献提问，流式返回。请求体：``{"question", "doc_ids", "rebuild"}``。"""
    body = await request.json()
    question = str(body.get("question") or "").strip()
    if len(question) < 2:
        raise HTTPException(status_code=400, detail="请填写要问的问题。")
    doc_ids = await _doc_ids(request, body)

    creds = _creds(request)
    cfg = pqa.resolve_config(creds, provider=str(body.get("provider") or "deepseek"))

    def gen():
        # 先把"用的是谁的凭据"如实告诉前端。站点自己的 Key 被使用时，
        # 访客有权知道这是花站主的钱（与 /api/deployment 的口径一致）。
        yield _sse({"type": "config", "source": cfg.source, "model": cfg.litellm_model,
                    "note": cfg.note})
        if not cfg.api_key:
            yield _sse({"type": "error",
                        "message": "这一步需要模型：请先在页面顶部填入你自己的 API Key。"})
            return
        for event in pqa.stream(cfg, {"action": "ask", "question": question,
                                      "rebuild": bool(body.get("rebuild"))},
                                timeout=TIMEOUT):
            yield _sse(event)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        # 关掉各级缓冲：SSE 被缓冲住就完全失去流式的意义。
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


@router.post("/brief")
async def brief(request: Request) -> dict:
    """文献简报 / 多篇对比综述。请求体：``{"mode", "doc_ids", "focus", "rebuild"}``。

    这里刻意**不用流式**：简报是"点一下、等一会儿、拿一份文档"的操作，
    而且要等全部小节都跑完才有意义（中途的半份简报没法用）。
    进度通过返回体里的 ``progress`` 数组如实给出。
    """
    body = await request.json()
    mode = str(body.get("mode") or "brief").strip().lower()
    if mode not in {"brief", "digest"}:
        raise HTTPException(status_code=400, detail=f"不支持的简报模式 {mode!r}（brief / digest）。")
    doc_ids = await _doc_ids(request, body)
    if mode == "digest" and len(doc_ids) < 2:
        raise HTTPException(status_code=400, detail="对比综述至少要选 2 篇文献。")

    creds = _creds(request)
    cfg = pqa.resolve_config(creds, provider=str(body.get("provider") or "deepseek"))
    if not cfg.api_key:
        raise HTTPException(status_code=400,
                            detail="这一步需要模型：请先在页面顶部填入你自己的 API Key。")

    result = pqa.brief(cfg, doc_ids, focus=str(body.get("focus") or ""),
                       mode=mode, rebuild=bool(body.get("rebuild")), timeout=TIMEOUT)
    result["config"] = {"source": cfg.source, "model": cfg.litellm_model, "note": cfg.note}
    result["docs"] = [{"doc_id": d, "title": _title_of(d)} for d in doc_ids]
    if not result.get("ok"):
        # 简报失败的原因（缺 Key / 索引不到 / 子进程崩）都要能直接读给用户听。
        return result
    return result


@router.post("/index")
async def build_index(request: Request) -> dict:
    """**只建索引**，不提问题。

    为什么需要一个单独的建索引入口：索引是"每篇文献调一次模型"的重活，
    把它和提问绑在一起会让第一次提问变成 3 分钟的空白等待，用户不知道
    是在建索引还是卡住了。拆出来以后，用户可以主动点"建立索引"，
    看着进度走完，之后的每次提问都是 10~25 秒。

    请求体：``{"doc_ids": [...], "rebuild": bool, "provider": str}``。
    """
    body = await request.json()
    doc_ids = await _doc_ids(request, body)

    creds = _creds(request)
    cfg = pqa.resolve_config(creds, provider=str(body.get("provider") or "deepseek"))
    if not cfg.api_key:
        return {"ok": False, "error": "建立索引需要对每篇文献调用一次模型，请先填入 API Key。"}

    events = pqa.stream(cfg, {"action": "ask", "question": "这篇文献研究什么问题？",
                              "rebuild": True}, timeout=TIMEOUT)
    index_event: dict = {}
    errors: list[str] = []
    progress: list[str] = []
    for event in events:
        kind = event.get("type")
        if kind == "index":
            index_event = event
        elif kind == "error":
            errors.append(str(event.get("message") or ""))
        elif kind == "progress":
            progress.append(str(event.get("message") or ""))

    if errors:
        return {"ok": False, "error": errors[-1], "progress": progress}
    return {
        "ok": True,
        "docs": index_event.get("docs"),
        "seconds": index_event.get("seconds"),
        "progress": progress,
        "note": "索引已建立并缓存；之后的提问会复用它（除非文献集合变化或点了重建）。",
    }


@router.delete("/index")
def clear_index() -> dict:
    """清空 PaperQA2 索引缓存。语料不复用时会自动重建。"""
    return pqa.clear_index()


def _title_of(doc_id: str) -> str:
    doc = rag.get_document(doc_id)
    return (doc or {}).get("title") or doc_id
