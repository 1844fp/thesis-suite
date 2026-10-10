"""题目生成助手 · 路由。

上游实现是 ``thesis-topic-helper`` 的 ``core/``（整包隔离在
``vendor/topichelper_stage/core``）。本模块只做三件事：

1. 把请求头里的访客凭据变成上游的 ``Settings`` 对象（**不走上游 load_settings**）；
2. 把上游的事件流转成 SSE 推给浏览器；
3. 导出 Markdown。

生成流程一个字节都没有重写——离线知识库、分批、截断补救、简报补齐
全部沿用上游实现。
"""

from __future__ import annotations

import json
import time

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import credentials as creds_mod

router = APIRouter(prefix="/api/topics", tags=["题目生成"])

#: 访客可选的服务商（前端下拉框直接用）。
DEFAULT_PROVIDER = "deepseek"

#: 一次最多生成多少题。上游上限 30，这里再收一档，避免有人用公网服务刷长文本。
MAX_COUNT = 12


def _creds(request: Request) -> creds_mod.Credentials:
    try:
        return creds_mod.from_headers(request.headers)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@router.get("/options")
def options() -> dict:
    """服务商清单 + 内置学科（离线模式用）。不需要凭据。"""
    from core import offline

    return {
        "ok": True,
        "providers": creds_mod.provider_presets(),
        "default_provider": DEFAULT_PROVIDER,
        "levels": [
            "本科毕业论文",
            "硕士学位论文",
            "博士学位论文 / 核心期刊投稿",
            "课程论文 / 学年论文",
        ],
        "disciplines": [item["name"] for item in offline.list_disciplines()],
        "max_count": MAX_COUNT,
    }


@router.post("/generate")
async def generate(request: Request) -> StreamingResponse:
    """SSE 流式生成题目。访客没给 Key 时上游会自动降级为离线知识库模式。"""
    from core.generate import GenerationError, generate_stream

    body = await request.json()
    creds = _creds(request)

    provider = str(body.get("provider") or DEFAULT_PROVIDER)
    try:
        count = max(1, min(int(body.get("count") or 6), MAX_COUNT))
    except (TypeError, ValueError):
        count = 6

    mode = str(body.get("mode") or "auto").lower()
    if mode not in {"auto", "llm", "offline"}:
        mode = "auto"

    params = {
        "discipline": str(body.get("discipline") or "").strip(),
        "direction": str(body.get("direction") or "").strip(),
        "level": str(body.get("level") or "硕士学位论文").strip(),
        "topic_types": [str(body.get("type"))] if body.get("type") else [],
        "count": count,
        "methodology": str(body.get("methodology") or "").strip(),
        "keywords": str(body.get("keywords") or "").strip(),
        "extra": str(body.get("extra") or "").strip(),
        "seed": str(body.get("seed") or ""),
        "language": "中文",
        "avoid": [str(x) for x in (body.get("avoid") or []) if str(x).strip()],
    }
    if not params["discipline"]:
        raise HTTPException(status_code=400, detail="请先填写学科/专业。")

    try:
        settings = creds_mod.topic_settings(creds, provider)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 访客没给 Key 且没显式要求联网 → 强制离线，绝不回退到服务器的 Key。
    if not creds.configured and mode == "llm":
        raise HTTPException(status_code=400, detail="联网模式需要先填写你自己的 API Key。")

    started = time.time()

    def stream():
        yield _sse({"type": "stage", "stage": "start", "message": "已连接，正在准备…", "progress": 0.02})
        try:
            for event in generate_stream(settings, mode=mode, include_raw=False, **params):
                yield _sse(event)
        except GenerationError as exc:
            yield _sse({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，别让 SSE 断在半路
            yield _sse({"type": "error", "message": f"生成失败：{type(exc).__name__}: {exc}"})
        finally:
            yield _sse({"type": "elapsed", "seconds": round(time.time() - started, 1)})
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            # 反向代理（HF Spaces 前面有 nginx）不加这个会把 SSE 缓冲掉，前端一直空白。
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/export")
async def export_markdown(request: Request) -> PlainTextResponse:
    """把生成结果渲染成 Markdown 下载。纯本地渲染，不调用模型。"""
    from core import export as export_module

    body = await request.json()
    result = body.get("result")
    if not isinstance(result, dict) or not result.get("topics"):
        raise HTTPException(status_code=400, detail="没有可导出的内容。")
    markdown = export_module.render_markdown(result)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"选题-{result.get('discipline') or '未命名'}-{stamp}.md"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={
            # 中文文件名必须用 filename* 的百分号编码，否则浏览器会乱码。
            "Content-Disposition": "attachment; filename*=UTF-8''" + _quote(filename),
        },
    )


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text)
