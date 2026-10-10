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

    _NEUTRALIZED[:] = _seal_against_server_keys()
    return SUITE_ROOT


#: ``_seal_against_server_keys`` 实际改动了什么。给 ``/api/health`` 如实上报，
#: 让"服务器密钥不会被借用"这件事在运行时可见，而不是只写在注释里。
_NEUTRALIZED: list[str] = []


def _seal_against_server_keys() -> list[str]:
    """把上游「借用服务器密钥」的回退彻底关掉，返回实际处理过的模块名。

    本站是 BYOK 的：密钥只从请求头来。但上游两套代码都有**从服务器本地取密钥**的回退：

    * ``thesis-agents``      ``load_settings()``：环境变量 > 本项目 .env > ``E:\\AI\\thesis-assistant\\.env``
    * ``thesis-topic-helper`` ``load_settings()``：``../thesis-assistant/.env`` > 本项目 .env > 环境变量

    本站的请求路径一次都不调用 ``load_settings()``（``tools/smoke_byok.py`` 用 AST
    静态校验这一点）。但"没人调用"是**靠约定**保证的，不是结构性的：将来任何一条路径
    误调一次，就会拿站主机器上的真密钥替访客付费——而且是几十次模型调用的规模。

    这里把回退**数据**置空，而不是去改上游的函数：

    * 清掉 ``os.environ`` 里的 ``LLM_*``，让"环境变量优先"这一档失效；
    * 把两个模块上的回退路径元组换成空元组，让"读邻居项目 .env"这一档失效。

    换掉元组而不是给函数打补丁，是为了**改动面最小**：只动两个模块级常量，
    上游函数体一行不碰。清 ``os.environ`` 里的 key 而不是只把自己钉成空串也很关键——
    早先的版本用 ``os.environ["LLM_API_KEY"] = ""``，实测**没有用**：
    ``thesis-agents`` 写的是 ``os.environ.get("LLM_API_KEY") or env.get(...)``，
    空串为假，于是继续往下读到了邻居项目的真密钥。

    注意：本函数只在**本站自己的进程**里生效，不影响那两个项目独立运行。
    """
    touched: list[str] = []

    # ---- 1. 环境变量这一档 ----
    # 上游都读 LLM_API_KEY / LLM_BASE_URL / LLM_MODEL。本站一律从请求头取，
    # 不依赖环境变量，所以直接清掉最干净（不"钉空串"——空串在某些写法里会被继续穿透）。
    for key in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        if os.environ.pop(key, None) is not None:
            touched.append(f"env:{key}")

    # ---- 2. 邻居项目 .env 这一档 ----
    # 模块级常量，import 之后覆盖即可；两个模块都不重读这两个常量之外的来源。
    try:
        import app.config as thesis_config

        if thesis_config.FALLBACK_ENV_PATHS:
            thesis_config.FALLBACK_ENV_PATHS = ()
            thesis_config.FALLBACK_KEYS = ()
            touched.append("app.config.FALLBACK_ENV_PATHS")
    except Exception:  # noqa: BLE001 - 上游不可用时不该让引导层崩掉
        pass

    try:
        import core.config as topic_config

        if topic_config.SIBLING_ENV_FILES:
            topic_config.SIBLING_ENV_FILES = ()
            # 邻居项目的 models.json 里也可能躺着密钥，一并关掉。
            topic_config.SIBLING_MODELS_JSON = Path("/nonexistent/thesis-suite-disabled")
            touched.append("core.config.SIBLING_ENV_FILES")
    except Exception:  # noqa: BLE001
        pass

    return touched


def describe() -> dict:
    """给界面用的诊断信息：确认两套上游代码确实可导入，且密钥回退确实被关掉。"""
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
    # 把"服务器密钥不会被借用"这件事在运行时暴露出来，而不是只写在注释里。
    out["key_fallback_neutralized"] = list(_NEUTRALIZED)
    out["server_key_present"] = bool(os.environ.get("LLM_API_KEY", "").strip())
    return out
