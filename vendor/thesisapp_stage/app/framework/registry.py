"""消息总线。

三个注册表 + 一个总线，是"多智能体"区别于"一串函数调用"的全部东西：

* ``TOOLS``      —— 工具注册表。智能体声明自己需要什么能力，而不是直接 import。
* ``VERIFIERS``  —— 检查器注册表。**不调用模型也能全部跑完**。
* ``FIXERS``     —— 修复器注册表（见 ``ops`` 里对"算子"的说明）。

注册表用装饰器注册。注册发生在模块 import 时，所以 `app.checks`、`app.format.checkers`
与 `app.agents` 必须被导入一次——这一步由 ``app.runtime.CHECKER_MODULES`` 和
``app.agents.__init__`` 负责。**忘了导入不会报错，只会静默地"一个检查器都没跑"**，
这类失败极难发现，所以 `app.runtime` 对导入失败逐个记账并上报到界面。
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

#: 工具：真正干活的函数。name -> {fn, description, agent}
TOOLS: dict[str, dict[str, Any]] = {}

#: 检查器：文本（或项目）-> list[Issue]。必须是纯函数式的，不产生副作用。
VERIFIERS: list[dict[str, Any]] = []

#: 修复算子：按 name 索引，供 Patch.ops 引用。
FIXERS: dict[str, dict[str, Any]] = {}


def tool(name: str, *, description: str = "", agent: str = "") -> Callable:
    """Register a tool. The name is the stable contract other agents call by."""

    def deco(fn: Callable) -> Callable:
        if name in TOOLS:
            raise ValueError(f"工具名重复注册: {name}")
        TOOLS[name] = {"name": name, "fn": fn, "description": description, "agent": agent}
        return fn

    return deco


def verifier(
    name: str,
    *,
    kinds: Iterable[str] = ("draft",),
    needs_llm: bool = False,
    description: str = "",
    cost: str = "local",
) -> Callable:
    """Register a verifier.

    ``kinds`` declares which artifact kinds it applies to, so the state machine
    can select the right subset without every verifier guarding internally.
    ``needs_llm=True`` marks verifiers that cost money — the pipeline runs the
    free ones first and can stop before spending if a blocker is already found.
    """

    kinds_tuple = tuple(kinds)

    def deco(fn: Callable) -> Callable:
        if any(v["name"] == name for v in VERIFIERS):
            raise ValueError(f"检查器名重复注册: {name}")
        VERIFIERS.append(
            {
                "name": name,
                "fn": fn,
                "kinds": kinds_tuple,
                "needs_llm": needs_llm,
                "description": description,
                "cost": cost,
            }
        )
        return fn

    return deco


def fixer(name: str, *, description: str = "", arg_schema: dict | None = None) -> Callable:
    def deco(fn: Callable) -> Callable:
        if name in FIXERS:
            raise ValueError(f"修复算子名重复注册: {name}")
        FIXERS[name] = {
            "name": name,
            "fn": fn,
            "description": description,
            "arg_schema": arg_schema or {},
        }
        return fn

    return deco


def verifiers_for(kind: str, *, include_llm: bool = True) -> list[dict[str, Any]]:
    out = [v for v in VERIFIERS if kind in v["kinds"]]
    if not include_llm:
        out = [v for v in out if not v["needs_llm"]]
    # Cheap first: a blocker found for free should not wait behind a paid call.
    return sorted(out, key=lambda v: (v["needs_llm"], v["name"]))


def registry_snapshot() -> dict:
    return {
        "tools": [
            {"name": t["name"], "description": t["description"], "agent": t["agent"]}
            for t in TOOLS.values()
        ],
        "verifiers": [
            {
                "name": v["name"],
                "kinds": list(v["kinds"]),
                "needs_llm": v["needs_llm"],
                "cost": v["cost"],
                "description": v["description"],
            }
            for v in VERIFIERS
        ],
        "fixers": [
            {"name": f["name"], "description": f["description"], "arg_schema": f["arg_schema"]}
            for f in FIXERS.values()
        ],
    }
