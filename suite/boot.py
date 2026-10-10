"""运行环境引导：把两套既有代码按「隔离层」布局挂进 sys.path。

为什么需要这一层
----------------
本网站整合了两套各自独立开发的项目：

* ``thesis-agents``     —— 资料获取 / 写作 / 评估（包名 ``app``）
* ``thesis-topic-helper`` —— 题目生成（包名 ``core``）

两套代码都**通过相对项目根的目录深度**来推导自己的数据目录、`.env`、导出目录
（例如 ``PROJECT_ROOT = Path(__file__).parent.parent``）。如果直接把两个包平铺到
同一个目录下，它们会算错根目录、互相覆盖同名模块（``config`` / ``llm`` / ``server``
三处重名），并且会把文件写进代码目录。

所以这里把它们放进**各自的隔离层**，各自算出的根目录正好落在自己的层里：

    vendor/thesisapp_stage/app/...     -> 根 = vendor/thesisapp_stage/
    vendor/topichelper_stage/core/...  -> 根 = vendor/topichelper_stage/

两个导入名（``app`` / ``core``）互不相同，因此可以在同一进程里共存，
**上游代码一行都不用改**——这是本方案能成立的关键。

本模块只做一件事：在导入任何上游模块之前把路径准备好。它不导入上游代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: 本整合项目根目录（thesis-suite/）。
SUITE_ROOT = Path(__file__).resolve().parent.parent

#: 上游代码的隔离层目录。顺序有意义：先 stage 再 vendor。
STAGE_DIRS = (
    SUITE_ROOT / "vendor" / "thesisapp_stage",
    SUITE_ROOT / "vendor" / "topichelper_stage",
)

#: 本机开发时，上游项目里 vendored 的第三方依赖（fastapi / httpx / python-docx …）。
#: 这些目录**只在本机开发时存在**；公网容器里依赖由 pip 从 requirements.txt 安装，
#: 因此这里必须容忍不存在。
LOCAL_LIB_DIRS = (
    SUITE_ROOT / "vendor" / "thesisapp_stage" / "libs",
    SUITE_ROOT / "upstream" / "thesis-agents" / "libs",
    SUITE_ROOT / "upstream" / "aigc-reducer" / "libs",
    SUITE_ROOT / "upstream" / "thesis-assistant" / "libs",
    Path(r"E:\AI\thesis-agents\libs"),
    Path(r"E:\AI\aigc-reducer\libs"),
    Path(r"E:\AI\thesis-assistant\libs"),
)


def _add(path: Path) -> None:
    text = str(path)
    if text not in sys.path and path.is_dir():
        sys.path.insert(0, text)


def prepare() -> Path:
    """准备好导入路径，返回整合项目根目录。可重复调用（幂等）。"""
    # 上游代码必须优先于任何同名包被找到。
    for stage in reversed(STAGE_DIRS):
        _add(stage)
    # 第三方依赖放最后，避免 libs/ 里的东西盖住标准库。
    for lib in LOCAL_LIB_DIRS:
        text = str(lib)
        if lib.is_dir() and text not in sys.path:
            sys.path.append(text)

    # 让上游代码把数据目录 / .env 落在整合项目根，而不是代码目录里。
    # 上游 config 模块读这些环境变量（见 suite/config.py 的说明）。
    os.environ.setdefault("SUITE_ROOT", str(SUITE_ROOT))
    os.environ.setdefault("DSH_THESIS_DATA_DIR", str(SUITE_ROOT / "data"))
    return SUITE_ROOT


def describe() -> dict:
    """给界面用的诊断信息：确认两套上游代码确实可导入。"""
    import importlib

    out: dict = {"suite_root": str(SUITE_ROOT), "packages": {}}
    for name, mod in (("thesisapp", "app.server"), ("topichelper", "core.generate")):
        try:
            importlib.import_module(mod)
            out["packages"][name] = {"module": mod, "ok": True, "error": ""}
        except Exception as exc:  # noqa: BLE001 - 诊断就是要如实报告失败
            out["packages"][name] = {
                "module": mod,
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
            }
    real = [p for p in sys.path if p.startswith(str(SUITE_ROOT))]
    out["resolved_paths"] = real
    return out
