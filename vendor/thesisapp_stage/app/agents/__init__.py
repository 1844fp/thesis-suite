"""智能体包。

这个 ``__init__`` 承担一个真实存在的工程问题：**一部分智能体是可选依赖**。

* ``fixer`` 与 ``gather`` 是本项目自带的，必须存在。
* ``writer_*`` / ``verify_external`` / ``references_*`` 由其他模块提供。

如果这里用硬 import，任何一个模块的问题都会让整个服务起不来——而"格式检查器
不可用"完全不应该阻止"降 AIGC"照常工作。所以可选部分逐个 try/except，
并把失败原因记在 ``IMPORT_PROBLEMS`` 里，由 ``/api/agents`` 暴露出来。

**静默吞掉 ImportError 才是真正的坑**：那会让人以为某个能力"就是这样"，而不是
"没装上"。所以这里既记录也上报。
"""

from __future__ import annotations

from typing import Any

#: 模块名 -> 失败原因。非空表示对应能力不可用，界面要显示出来。
IMPORT_PROBLEMS: dict[str, str] = {}

REQUIRED_MODULES = ("gather", "fixer")
OPTIONAL_MODULES = (
    "writer_plan",
    "writer_draft",
    "writer_conclude",
    "writer_references",
    "writer_rewrite",
    "writer_polish",
    "verify_external",
    "references_extract",
    "references_build",
)


def _load(names: tuple[str, ...], *, required: bool) -> None:
    import importlib

    for name in names:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:  # noqa: BLE001 - 记下来比抛出去有用
            IMPORT_PROBLEMS[name] = f"{type(exc).__name__}: {exc}"
            if required:
                raise


_load(REQUIRED_MODULES, required=True)
_load(OPTIONAL_MODULES, required=False)


def status() -> dict[str, Any]:
    return {
        "required": list(REQUIRED_MODULES),
        "optional": list(OPTIONAL_MODULES),
        "missing_or_broken": dict(IMPORT_PROBLEMS),
        "ok": not IMPORT_PROBLEMS,
    }


__all__ = ["IMPORT_PROBLEMS", "OPTIONAL_MODULES", "REQUIRED_MODULES", "status"]
