"""运行时装配：把框架、适配器、智能体组装成一个可用的系统。

这是唯一一处"知道所有部件"的地方。其他地方都只认识框架的接口。

装配策略是**宽容但不静默**：

* 核心智能体（资料获取、检索、修复）缺失 → 直接报错，因为系统不完整。
* 可选智能体（写作、外部核验、格式）缺失 → 跳过，但把原因记进 ``SKIPPED``，
  由 ``/api/agents`` 和 CLI ``agents`` 暴露。**"某个能力没装上"和"这个能力本来
  就没有"必须能区分**，否则使用者会以为是功能缺陷。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path

from .agents import IMPORT_PROBLEMS
from .config import RUNS_DIR, Settings, ensure_dirs, load_settings
from .framework.agent import Agent, AgentContext, AgentRegistry, Ledger, build_verifier_agents
from .framework.blackboard import Blackboard
from .framework.bus import MessageBus, MessageLog
from .framework.registry import registry_snapshot
from .llm import LLMClient

#: 智能体类所在的模块 -> 期望的类名。
#: 用「模块 + 类名」而不是注册装饰器，是因为智能体需要配置（LLM、路径），
#: 由这里统一注入比让每个模块自己读环境变量更可控。
AGENT_SPECS: tuple[tuple[str, str, bool], ...] = (
    ("app.agents.gather", "LiteratureGatherAgent", True),
    ("app.agents.gather", "KnowledgeRetrieveAgent", True),
    ("app.agents.gather", "KnowledgeQAAgent", False),
    ("app.agents.fixer", "FixerAgent", True),
    ("app.agents.writer_plan", "WriterPlanAgent", False),
    ("app.agents.writer_draft", "WriterDraftAgent", False),
    ("app.agents.writer_conclude", "WriterConcludeAgent", False),
    ("app.agents.writer_references", "WriterReferencesAgent", False),
    ("app.agents.writer_rewrite", "WriterRewriteAgent", False),
    ("app.agents.writer_polish", "WriterPolishAgent", False),
    ("app.agents.verify_external", "ExternalVerifyAgent", False),
    ("app.agents.references_build", "ReferencesBuildAgent", False),
    # 格式整理由 app\format 提供智能体；类名以 FormatApplyAgent 约定。
    ("app.agents.format_apply", "FormatApplyAgent", False),
)


#: 检查器所在的模块。**必须显式导入**：``@verifier`` 是在 import 时执行注册的，
#: 没有人 import 这些模块，注册表就是空的——而且不会报错，只会静默地"一个检查器
#: 都没跑"。这类失败极难发现，所以这里显式列出并对失败原因记账。
CHECKER_MODULES: tuple[str, ...] = (
    "app.checks",
    "app.format.checkers",
    "app.agents.verify_external",
)


@dataclass
class Runtime:
    settings: Settings
    registry: AgentRegistry
    #: 模块/类 -> 未能注册的原因。空表示全部就绪。
    skipped: dict[str, str] = field(default_factory=dict)

    def new_context(
        self,
        board: Blackboard,
        *,
        progress=None,
        run_dir: Path | None = None,
        stage: str = "",
    ) -> AgentContext:
        """Build a fresh per-run context.

        The ledger is new per run on purpose: cost reporting is per run, not
        cumulative across the whole session — a cumulative counter tells you
        nothing about which run was expensive.
        """
        run_dir = run_dir or (RUNS_DIR / board.project.id)
        run_dir.mkdir(parents=True, exist_ok=True)
        bus = MessageBus(MessageLog(run_dir / "messages.jsonl"))
        # 按用途选模型的路由器：起草用强模型、核验与修复用便宜模型，
        # 是配置层面的一件事，不需要改任何智能体。
        router = None
        try:
            from .models import ModelRouter

            router = ModelRouter(self.settings.models, self.settings.llm) if self.settings.models else None
        except Exception:  # noqa: BLE001 - 路由装不上就退回单模型，不让流程起不来
            router = None
        ctx = AgentContext(
            settings=self.settings,
            llm=router.default if router is not None else LLMClient(self.settings.llm),
            router=router,
            board=board,
            bus=bus,
            ledger=Ledger(),
            run_dir=run_dir,
            stage=stage or board.project.stage,
            progress=progress,
        )
        bus.subscribe("*", lambda m: ctx.board.project.history.append(
            {
                "ts": m.ts,
                "stage": m.stage or ctx.stage,
                "round": m.round,
                "actor": m.sender,
                "action": f"bus:{m.topic}",
                "detail": str(m.payload)[:400],
            }
        ))
        return ctx

    def describe(self) -> dict:
        return {
            "agents": self.registry.describe(),
            "skipped": dict(self.skipped),
            "agent_module_problems": dict(IMPORT_PROBLEMS),
            "registry": registry_snapshot(),
            "settings": self.settings.public(),
        }


def _instantiate(module_name: str, class_name: str) -> Agent:
    mod = importlib.import_module(module_name)
    cls = getattr(mod, class_name)
    return cls()


def build_runtime(settings: Settings | None = None) -> Runtime:
    """Assemble the registry. Called once per process."""
    ensure_dirs()
    settings = settings or load_settings()
    registry = AgentRegistry()
    skipped: dict[str, str] = {}

    # --- 先导入检查器模块，触发 @verifier 注册 ---
    for module_name in CHECKER_MODULES:
        try:
            importlib.import_module(module_name)
        except ModuleNotFoundError as exc:
            # 某个检查器模块缺失是正常的（能力还没装上），但必须留痕。
            skipped[f"checkers:{module_name}"] = f"模块未提供：{exc}"
        except Exception as exc:  # noqa: BLE001
            skipped[f"checkers:{module_name}"] = f"{type(exc).__name__}: {exc}"

    # --- 框架注册的检查器：全部包成智能体 ---
    for agent in build_verifier_agents(include_llm=True):
        registry.register(agent)

    # --- 具名智能体 ---
    for module_name, class_name, required in AGENT_SPECS:
        key = f"{module_name}.{class_name}"
        try:
            registry.register(_instantiate(module_name, class_name))
        except ModuleNotFoundError as exc:
            if required:
                raise RuntimeError(
                    f"核心智能体 {key} 缺失（{exc}）。系统不完整，无法启动。"
                ) from exc
            skipped[key] = f"模块未提供：{exc}"
        except AttributeError as exc:
            if required:
                raise RuntimeError(f"核心智能体 {key} 缺失类定义（{exc}）。") from exc
            skipped[key] = f"模块里没有这个类：{exc}"
        except Exception as exc:  # noqa: BLE001 - 可选能力失败不该拦住启动
            if required:
                raise
            skipped[key] = f"{type(exc).__name__}: {exc}"

    return Runtime(settings=settings, registry=registry, skipped=skipped)


def project_root() -> Path:
    from .config import PROJECTS_DIR

    return PROJECTS_DIR
