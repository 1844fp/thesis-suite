"""智能体基类、运行上下文与审计台账。

每个智能体都长一个样：**声明能力 → 接收上下文 → 返回结构化报告**。
不允许智能体自己去 new 一个 LLM 客户端或读环境变量，否则成本统计和模型切换
都会漏掉，测试也没法注入假模型。

``Ledger`` 是所有模型调用的唯一出口。它存在的理由只有一条：**没有成本可见性，
自动化流程就会在你不注意的时候烧钱。**
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from ..config import Settings
from ..llm import LLMClient
from .artifact import Artifact, Issue, Stage, new_id, severity_from
from .blackboard import Blackboard
from .bus import MessageBus
from .registry import verifiers_for


@dataclass
class CallRecord:
    purpose: str
    model: str
    seconds: float
    prompt_chars: int
    reply_chars: int
    ok: bool
    error: str = ""


class Ledger:
    """Wraps model calls so every one of them is counted and attributable."""

    def __init__(self) -> None:
        self.calls: list[CallRecord] = []

    @property
    def total_seconds(self) -> float:
        return sum(c.seconds for c in self.calls)

    @property
    def failures(self) -> int:
        return sum(1 for c in self.calls if not c.ok)

    def snapshot(self) -> dict:
        return {
            "calls": len(self.calls),
            "failures": self.failures,
            "seconds": round(self.total_seconds, 2),
            "prompt_chars": sum(c.prompt_chars for c in self.calls),
            "reply_chars": sum(c.reply_chars for c in self.calls),
            "by_purpose": self._by_purpose(),
            "by_model": self.models_used(),
        }

    def _by_purpose(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for c in self.calls:
            slot = out.setdefault(c.purpose, {"calls": 0, "seconds": 0.0, "failures": 0})
            slot["calls"] += 1
            slot["seconds"] = round(slot["seconds"] + c.seconds, 2)
            if not c.ok:
                slot["failures"] += 1
        return out

    def call(
        self,
        purpose: str,
        fn: Callable[..., str],
        messages: Sequence[dict],
        *,
        model: str = "",
        **kwargs: Any,
    ) -> str:
        """Run one model call, recording duration and failure.

        ``model`` 是**这次调用实际用的模型名**。原先这个字段取自
        ``kwargs.get("model")``——而 ``chat()`` 的签名里根本没有这个参数，
        于是它永远是空串。后果是成本台账只能告诉你"调了 47 次"，
        却说不出"是哪几个模型、各调了多少次"——而按用途分模型之后，
        这正是作者最需要看的那一项。

        Exceptions propagate: silently swallowing a model error would let a
        verifier report "no issues found" when it actually never ran.
        """
        prompt_chars = sum(len(str(m.get("content", ""))) for m in messages)
        t0 = time.time()
        try:
            out = fn(messages, **kwargs)
            self.calls.append(
                CallRecord(
                    purpose=purpose,
                    model=model,
                    seconds=time.time() - t0,
                    prompt_chars=prompt_chars,
                    reply_chars=len(out or ""),
                    ok=True,
                )
            )
            return out
        except Exception as exc:  # noqa: BLE001 - record then re-raise
            self.calls.append(
                CallRecord(
                    purpose=purpose,
                    model=model,
                    seconds=time.time() - t0,
                    prompt_chars=prompt_chars,
                    reply_chars=0,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            raise

    def models_used(self) -> dict[str, int]:
        """按模型汇总调用次数与耗时——成本可见性的一半在这里。"""
        out: dict[str, dict] = {}
        for c in self.calls:
            key = c.model or "（未记录）"
            slot = out.setdefault(key, {"calls": 0, "seconds": 0.0, "failures": 0})
            slot["calls"] += 1
            slot["seconds"] = round(slot["seconds"] + c.seconds, 2)
            if not c.ok:
                slot["failures"] += 1
        return out


def _client_model(client: Any) -> str:
    """取客户端用的模型名，**取不到返回空串而不是抛异常**。

    为什么不直接 ``client.cfg.model``：``AgentContext`` 接受鸭子类型的客户端
    （测试注入假模型就是这么做的），而"这个客户端没有 cfg 属性"是合法的——
    它只是**没有模型名可记**，不该让整次调用失败。

    台账里的模型名是**成本可见性**的凭据，不是调用能否成功的前提。
    把"看得见的成本"与"跑得通"绑在一起，等于让一个统计字段决定功能可用性。
    """
    cfg = getattr(client, "cfg", None)
    return str(getattr(cfg, "model", "") or "")


@dataclass
class AgentContext:
    """Everything an agent is allowed to touch."""

    settings: Settings
    llm: LLMClient
    board: Blackboard
    bus: MessageBus
    ledger: Ledger
    run_id: str = field(default_factory=lambda: new_id("run"))
    run_dir: Path = Path(".")
    stage: str = Stage.VERIFY.value
    round: int = 0
    #: Set by the pipeline so agents can log progress without knowing about SSE.
    progress: Callable[[str, float], None] | None = None
    #: 按用途选模型的路由器。为 None 时所有调用都用 ``llm``（单模型模式）。
    router: Any = None

    def say(self, message: str, ratio: float = -1.0) -> None:
        if self.progress is not None:
            self.progress(message, ratio)
        self.bus.publish("log", sender="pipeline", payload={"message": message}, stage=self.stage, round=self.round)

    def client_for(self, purpose: str) -> LLMClient:
        """这次调用该用哪个客户端。

        按 ``purpose`` 分档（起草/规划/核验/修复…），让"最强模型写正文、
        便宜模型做核验与机械修复"成为配置上的一件事，而不是让作者在
        "全都贵"和"全都差"之间二选一。
        """
        if self.router is not None:
            try:
                return self.router.client_for(purpose)
            except Exception:  # noqa: BLE001 - 路由失败退回默认模型，不让整条流程挂掉
                return self.llm
        return self.llm

    def ask_model(
        self,
        purpose: str,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        client = self.client_for(purpose)
        kwargs: dict[str, Any] = {"model": _client_model(client)}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return self.ledger.call(purpose, client.chat, messages, **kwargs)

    def ask_json(
        self,
        purpose: str,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> object:
        """Ask for JSON. Returns the parsed object, not a string."""
        client = self.client_for(purpose)
        kwargs: dict[str, Any] = {"model": _client_model(client)}
        if temperature is not None:
            kwargs["temperature"] = temperature
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return self.ledger.call(purpose, client.chat_json, messages, **kwargs)

    @property
    def llm_configured(self) -> bool:
        """有没有任何一个可用的模型配置。

        不能只看 ``ctx.llm``：按用途分模型之后，默认档案可能没填 key，
        而某个用途指向的档案填了——那时系统其实是能跑的。
        """
        if self.llm.configured:
            return True
        if self.router is not None:
            try:
                return any(
                    getattr(c, "cfg", None) is not None and c.cfg.configured
                    for c in self.router._clients.values()  # noqa: SLF001 - 同包内部结构
                )
            except Exception:  # noqa: BLE001
                return False
        return False


@dataclass
class AgentReport:
    """What an agent hands back. Never a bare string — the pipeline needs counts."""

    agent: str
    stage: str
    ok: bool = True
    summary: str = ""
    artifacts: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    error: str = ""

    def as_dict(self) -> dict:
        return {
            "agent": self.agent,
            "stage": self.stage,
            "ok": self.ok,
            "summary": self.summary,
            "artifacts": self.artifacts,
            "issues": self.issues,
            "data": self.data,
            "seconds": round(self.seconds, 2),
            "error": self.error,
        }


class Agent:
    """Base class. Subclasses implement ``run``."""

    name: str = "agent"
    description: str = ""
    stage: str = Stage.VERIFY.value
    #: Capability tags other parts of the system route on.
    capabilities: tuple[str, ...] = ()
    #: Does a normal run call the model (and therefore cost money)?
    uses_model: bool = False

    def run(self, ctx: AgentContext) -> AgentReport:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- helpers shared by every agent ------------------------------------
    @property
    def draft_text(self) -> str:
        return ""

    def report(self, ctx: AgentContext, **kwargs: Any) -> AgentReport:
        return AgentReport(agent=self.name, stage=ctx.stage or self.stage, **kwargs)


class AgentRegistry:
    """name -> agent instance. Populated by ``app.bootstrap``."""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> Agent:
        if agent.name in self._agents:
            raise ValueError(f"智能体名重复注册: {agent.name}")
        self._agents[agent.name] = agent
        return agent

    def get(self, name: str) -> Agent:
        try:
            return self._agents[name]
        except KeyError:
            raise KeyError(f"未注册的智能体: {name}；已注册：{sorted(self._agents)}") from None

    def all(self) -> list[Agent]:
        return list(self._agents.values())

    def by_capability(self, cap: str) -> list[Agent]:
        return [a for a in self._agents.values() if cap in a.capabilities]

    def describe(self) -> list[dict]:
        return [
            {
                "name": a.name,
                "description": a.description,
                "stage": a.stage,
                "capabilities": list(a.capabilities),
                "uses_model": a.uses_model,
            }
            for a in sorted(self._agents.values(), key=lambda x: x.name)
        ]


class VerifierAgent(Agent):
    """Runs a registered verifier function.

    Wrapping verifiers as agents means a verifier can be:
    * run alone from the CLI (``verify --only citation``),
    * subscribed to on the bus,
    * measured for cost,
    without duplicating its logic here.
    """

    uses_model = False

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.name = f"verify:{spec['name']}"
        self.description = spec.get("description", "")
        self.uses_model = bool(spec.get("needs_llm"))
        self.capabilities = ("verify", spec["name"], "verify:llm" if self.uses_model else "verify:local")

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        text = ctx.board.draft_text
        try:
            found = self.spec["fn"](text, ctx) or []
        except Exception as exc:  # noqa: BLE001 - one verifier failing must not stop others
            return self.report(
                ctx,
                ok=False,
                summary=f"检查器 {self.spec['name']} 执行失败",
                error=f"{type(exc).__name__}: {exc}",
                seconds=time.time() - t0,
            )

        issues: list[Issue] = []
        for raw in found:
            if isinstance(raw, Issue):
                if not raw.detected_by or raw.detected_by == "rules":
                    raw.detected_by = f"rules:{self.spec['name']}" if not self.uses_model else f"llm:{self.spec['name']}"
                issues.append(raw)
        added = ctx.board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)
        return self.report(
            ctx,
            ok=True,
            summary=f"{self.spec['name']}：发现 {len(issues)} 条，新增 {added} 条",
            issues=[i.id for i in issues],
            data={"verifier": self.spec["name"], "found": len(issues), "added": added},
            seconds=time.time() - t0,
        )


def build_verifier_agents(*, include_llm: bool = True, only: Iterable[str] | None = None) -> list[VerifierAgent]:
    wanted = set(only) if only else None
    specs = verifiers_for("draft", include_llm=include_llm)
    agents = [VerifierAgent(s) for s in specs]
    if wanted is not None:
        agents = [a for a in agents if a.spec["name"] in wanted]
    return agents
