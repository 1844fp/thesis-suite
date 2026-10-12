"""四个真 agent：题目生成 / 资料获取 / 写作 / 评估。

为什么要有这一层
----------------
原先这四个"助手"只是四个 API 路由模块。它们各自去 ``STORE`` 取会话、各自返回结果，
互相不知道对方在做什么，也**无法被单独查询状态**——"四个助手"只是界面上的命名。

现在每个助手是一个 ``Agent`` 对象，注册在 ``REGISTRY`` 里：

* **可寻址**：``GET /api/agents`` 列出它们与各自的能力；
  ``POST /api/agents/{name}/{action}`` 可以单独驱动任意一个动作；
* **共享状态**：都通过 ``Context`` 读写同一个会话（证据、稿件、评估结果、黑板）；
* **留痕**：每次动作写一条事件（谁、做了什么、多久、成不成），前端据此显示实时状态。

设计上的一个克制：**agent 之间不直接互相调用**，而是通过会话状态解耦。
"资料获取"把证据写进会话，"写作"从会话里读——这样任何一个都能单独用，
也能被将来的编排器按任意顺序驱动。直接互相调用会把它们焊死成一条固定流水线。
"""

from __future__ import annotations

from .base import REGISTRY, Agent, Context

__all__ = ["REGISTRY", "Agent", "Context", "register_all"]


def register_all() -> None:
    """把四个 agent 注册进表。可重复调用（``Registry.add`` 按名字覆盖）。"""
    from .gather_agent import GatherAgent
    from .review_agent import ReviewAgent
    from .topic_agent import TopicAgent
    from .writer_agent import WriterAgent

    for cls in (TopicAgent, GatherAgent, WriterAgent, ReviewAgent):
        REGISTRY.add(cls())
