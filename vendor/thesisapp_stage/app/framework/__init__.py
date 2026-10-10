"""通用多智能体内核。

这一层不认识"论文"。它只认识四样东西：

* **Artifact**  —— 被处理的物件（一篇稿子、一份参考文献表、一个整改计划）
* **Issue**     —— 检查器发现的问题，带证据、定位、可修复性
* **Patch**     —— 针对 Issue 的修复，必须经过算子验证才能落地
* **Message**   —— 智能体之间流转的消息，全部落盘可回放

关键设计：**检查器（Verifier）和修复器（Fixer）分离，且检查器不调用模型也能跑。**
自我更正循环之所以有价值，不是因为有循环，而是因为改完能被同一把尺子复量。
"""

from .artifact import Artifact, Issue, OpResult, Patch, Severity, Stage
from .agent import Agent, AgentContext, AgentRegistry, AgentReport, Ledger, VerifierAgent
from .blackboard import Blackboard, Project
from .bus import Message, MessageBus, MessageLog
from .ops import OPS, OpError, apply_op
from .registry import FIXERS, VERIFIERS, TOOLS, fixer, tool, verifier
from .state import FixRound, Pipeline, StageResult

__all__ = [
    "Agent",
    "AgentContext",
    "AgentRegistry",
    "AgentReport",
    "Artifact",
    "Blackboard",
    "FixRound",
    "FIXERS",
    "Issue",
    "Ledger",
    "Message",
    "MessageBus",
    "MessageLog",
    "OPS",
    "OpError",
    "OpResult",
    "Patch",
    "Pipeline",
    "Project",
    "Severity",
    "Stage",
    "StageResult",
    "TOOLS",
    "VERIFIERS",
    "VerifierAgent",
    "apply_op",
    "fixer",
    "tool",
    "verifier",
]
