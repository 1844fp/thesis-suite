"""Agent 通用接口 + 实时状态。

这一层让"四个助手"变成真正可寻址、可观察的 agent：

* ``GET  /api/agents``                     —— 列出四个 agent 与各自的能力
* ``GET  /api/agents/state``               —— 每个 agent 的当前状态（在做什么/上次做了什么）
* ``GET  /api/agents/events``              —— 事件流，前端轮询它显示实时进展
* ``POST /api/agents/{name}/{action}``     —— 单独驱动任意一个动作
* ``GET  /api/agents/{name}/actions``      —— 某个 agent 的动作清单

**为什么要有通用驱动接口**：它让四个助手从"四个写死的路由"变成"同一套接口的四个实例"。
将来加第五个 agent 只需要注册，不需要再加一组路由；编排器（或前端）也不必
为每个 agent 写一套调用代码。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import credentials as creds_mod
from ..agents import REGISTRY, register_all
from ..agents.base import context_for

router = APIRouter(prefix="/api/agents", tags=["智能体"])

#: 四个 agent 各自的阶段归属。前端用它在阶段侧栏上显示"这个阶段是谁在干活"。
STAGE_OF_AGENT = {
    "topic": "topic",
    "gather": "proposal",
    "writer": "draft",
    "review": "revise",
}


def _ensure_registered() -> None:
    if not REGISTRY.all():
        register_all()


def _creds(request: Request) -> creds_mod.Credentials:
    try:
        return creds_mod.from_headers(request.headers)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def list_agents() -> dict:
    """列出四个 agent 与各自的动作。"""
    _ensure_registered()
    return {
        "ok": True,
        "agents": [
            {**a.describe(), "stage": STAGE_OF_AGENT.get(a.name, "")}
            for a in REGISTRY.all()
        ],
    }


@router.get("/events")
def events(sid: str = "", limit: int = 50) -> dict:
    """事件流：谁、在什么时候、做了什么、成没成。

    前端轮询这个接口就能显示实时进展——**不需要 SSE**，因为 agent 的动作是
    请求驱动的短过程（秒级到分钟级），轮询足够且实现简单得多。
    """
    _ensure_registered()
    if not sid:
        return {"ok": True, "events": [], "running": [], "sid": ""}
    try:
        ctx = context_for(sid, create=False)
    except ValueError:
        return {"ok": True, "events": [], "running": [], "sid": sid, "expired": True}
    return {
        "ok": True,
        "sid": sid,
        "events": ctx.recent(limit=max(1, min(limit, 200))),
        "running": ctx.running(),
    }


@router.get("/state")
def state(sid: str = "") -> dict:
    """每个 agent 的当前状态：正在做什么、上次做了什么、累计成功/失败次数。

    这是"实时更新子 agent"里"更新"的那一半：前端不必知道谁在跑，
    读这个接口就能把四个 agent 的状态牌全部刷新。
    """
    _ensure_registered()
    if not sid:
        return {"ok": True, "agents": []}
    try:
        ctx = context_for(sid, create=False)
    except ValueError:
        return {"ok": True, "agents": [], "expired": True}

    out: list[dict] = []
    for agent in REGISTRY.all():
        mine = [e for e in ctx.events if e.agent == agent.name]
        running = next((e for e in reversed(mine) if e.status == "running"), None)
        last = mine[-1] if mine else None
        out.append({
            "name": agent.name,
            "title": agent.title,
            "stage": STAGE_OF_AGENT.get(agent.name, ""),
            "busy": running is not None,
            "current": running.as_dict() if running else None,
            "last": last.as_dict() if last else None,
            "ok_count": sum(1 for e in mine if e.status == "ok"),
            "failed_count": sum(1 for e in mine if e.status == "failed"),
            "total": len(mine),
        })
    return {"ok": True, "agents": out}


@router.get("/{name}/actions")
def agent_actions(name: str) -> dict:
    _ensure_registered()
    agent = REGISTRY.get(name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"没有名为「{name}」的 agent。")
    return {"ok": True, **agent.describe()}


@router.post("/{name}/{action}")
async def invoke(name: str, action: str, request: Request) -> dict:
    """单独驱动任意一个 agent 的任意动作。

    动作必须在 agent 的 ``actions`` 表里登记过——**这是有意的白名单**，
    否则这个接口就成了"什么都能调"的后门。
    """
    _ensure_registered()
    agent = REGISTRY.get(name)
    if agent is None:
        raise HTTPException(status_code=404, detail=f"没有名为「{name}」的 agent。")
    handler = agent.handler(action)
    if handler is None:
        raise HTTPException(
            status_code=404,
            detail=f"「{name}」没有动作「{action}」。可用：{', '.join(agent.actions)}",
        )

    creds = _creds(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - 允许空 body
        body = {}
    if not isinstance(body, dict):
        body = {}

    sid = str(body.get("sid") or "") or None
    ctx = context_for(sid, create=True)

    if agent.needs_key(action) and not creds.configured:
        raise HTTPException(status_code=400,
                            detail=f"「{agent.title}」的「{action}」需要你自己的 API Key。")

    result = await handler(ctx, body, creds)
    # 会话 id 一定要回传：前端第一次调用时是自己没有 sid 的，靠这个接上后续调用。
    return {"sid": ctx.session.sid, **result}
