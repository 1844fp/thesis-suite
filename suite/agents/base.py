"""Agent 底座：共享上下文、事件流、可寻址注册表。

为什么需要这一层
----------------
本站原先的"四个助手"只是**四个 API 路由模块**（topics / gather / writing / review），
它们之间没有任何共同的东西：

* 没有共享状态——每个模块自己去 ``STORE`` 取会话，各取各的；
* 没有事件——前端只能看到自己发起的那个请求的 SSE 流，看不到**别的**助手在做什么；
* 不可寻址——没有办法问"写作 agent 现在是什么状态"，因为它根本不是一个对象。

所以"四个助手"当时只是界面上的命名。这一层把它变成真的：

    Context  —— 一次会话的共享状态（会话 + 事件流 + 黑板）
    Event    —— 每个 agent 的每次动作都写一条，前端可轮询/订阅
    Agent    —— 有名字、有能力声明、有统一动作入口的对象，注册在表里可寻址
    Registry —— 名字 -> agent，供 ``/api/agents`` 这类通用接口驱动

设计上刻意保持**薄**：不引入消息队列、不引入线程调度。四个 agent 都是"请求进来
就干活"的同步函数，事件流只是把"谁在什么时候干了什么、结果如何"记下来。
复杂的事件总线对当前规模没有收益，只会增加出错面。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from ..sessions import STORE, Session

#: 单个会话最多保留多少条事件。超了丢最旧的——事件流是给人看的实时反馈，
#: 不是审计日志，无限增长只会把内存吃掉。
MAX_EVENTS = 300


@dataclass
class Event:
    """一个 agent 的一次动作记录。"""

    id: str
    agent: str
    action: str
    status: str                  # running / ok / failed
    message: str = ""
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    detail: dict = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return round(max(0.0, end - self.started_at), 2)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "agent": self.agent,
            "action": self.action,
            "status": self.status,
            "message": self.message,
            "seconds": self.seconds,
            "at": time.strftime("%H:%M:%S", time.localtime(self.started_at)),
            "detail": self.detail,
        }


@dataclass
class Context:
    """一次会话的共享状态。四个 agent 都读写它。

    ``session`` 是既有的 ``sessions.Session``（存证据、稿件版本、评估历史），
    这里额外挂上事件流与黑板。黑板（``scratch``）用于 agent 之间传递中间产物，
    例如"大纲 agent 生成的结构"给"写作 agent 用"——**不落库、不持久化**，
    因为它是过程数据，会话结束就该消失。
    """

    session: Session
    events: list[Event] = field(default_factory=list)
    scratch: dict = field(default_factory=dict)

    # -- 事件 -------------------------------------------------------------
    def begin(self, agent: str, action: str, message: str = "", **detail: Any) -> Event:
        """记一条"开始"。返回 Event，调用方把它交给 finish/fail 收口。"""
        ev = Event(id=uuid.uuid4().hex[:12], agent=agent, action=action,
                   status="running", message=message, detail=detail)
        self.events.append(ev)
        if len(self.events) > MAX_EVENTS:
            del self.events[: len(self.events) - MAX_EVENTS]
        return ev

    @staticmethod
    def finish(ev: Event, message: str = "", **detail: Any) -> Event:
        ev.status = "ok"
        ev.ended_at = time.time()
        if message:
            ev.message = message
        ev.detail.update(detail)
        return ev

    @staticmethod
    def fail(ev: Event, message: str, **detail: Any) -> Event:
        ev.status = "failed"
        ev.ended_at = time.time()
        ev.message = message
        ev.detail.update(detail)
        return ev

    def recent(self, limit: int = 50) -> list[dict]:
        return [e.as_dict() for e in self.events[-limit:]]

    def running(self) -> list[dict]:
        return [e.as_dict() for e in self.events if e.status == "running"]

    # -- 黑板 -------------------------------------------------------------
    def put(self, key: str, value: Any) -> None:
        self.scratch[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.scratch.get(key, default)


class Registry:
    """agent 注册表：名字 -> agent。让通用接口可以驱动任意 agent。"""

    def __init__(self) -> None:
        self._agents: dict[str, "Agent"] = {}

    def add(self, agent: "Agent") -> "Agent":
        self._agents[agent.name] = agent
        return agent

    def get(self, name: str) -> "Agent | None":
        return self._agents.get(name)

    def all(self) -> list["Agent"]:
        return list(self._agents.values())

    def describe(self) -> list[dict]:
        return [a.describe() for a in self._agents.values()]


REGISTRY = Registry()


class Agent:
    """一个可寻址的 agent。

    子类只需要声明身份与动作表。**动作表是显式的**——通用接口
    ``POST /api/agents/{name}/{action}`` 只允许调用这里登记过的动作，
    避免变成一个"什么都能调"的后门。
    """

    name: str = ""
    title: str = ""
    summary: str = ""
    #: 动作名 -> {"handler": callable, "needs_key": bool, "desc": str}
    actions: dict[str, dict] = {}

    def describe(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "summary": self.summary,
            "actions": [
                {"name": k, "desc": v.get("desc", ""), "needs_key": bool(v.get("needs_key"))}
                for k, v in self.actions.items()
            ],
        }

    def handler(self, action: str) -> Callable | None:
        item = self.actions.get(action)
        return item.get("handler") if item else None

    def needs_key(self, action: str) -> bool:
        item = self.actions.get(action) or {}
        return bool(item.get("needs_key"))


def context_for(sid: str | None, *, create: bool = True) -> Context:
    """取（或新建）一个会话的共享上下文。

    上下文缓存在会话对象上（``session.meta`` 之外的一个属性），这样同一个 sid
    的多次请求看到的是**同一条事件流**，而不是每次请求都从零开始记。
    """
    session = STORE.get(sid, create=create)
    if session is None:
        raise ValueError("会话不存在")
    ctx = getattr(session, "_agent_ctx", None)
    if ctx is None:
        ctx = Context(session=session)
        session._agent_ctx = ctx          # 挂在会话上，随会话一起被内存回收
    return ctx
