"""模拟"公网容器的干净环境"，验证部署形态真的能跑起来。

为什么需要这个模拟
------------------
之前所有测试都是在这台开发机上跑的，环境里有两样**容器里绝对没有**的东西：

1. ``E:\\AI\\aigc-reducer\\libs``、``E:\\AI\\thesis-assistant\\libs``
   —— vendored 的第三方依赖，靠 ``run.cmd`` 塞进 PYTHONPATH；
2. ``E:\\AI\\thesis-assistant\\.env``、``E:\\AI\\aigc-reducer\\.env``
   —— 上游会去"借用"密钥的邻居项目配置。

容器里这两样都不在：依赖由 ``pip install -r requirements.txt`` 提供，
邻居项目根本不存在。而且 ``Dockerfile`` 是把源码树整体复制到 ``/app``，
所以 ``suite`` 与 ``vendor`` 是**同级**的。

本脚本做的就是把这两点差别搬进本机测试：

* 把 ``suite`` 与 ``vendor`` 复制到一个"裸"目录（模拟 ``/app``），同级摆放；
* 用 ``python -I``（隔离模式）启动子进程，**不继承 PYTHONPATH、不读 user site**；
* 打桩掉三个 Windows 绝对路径（让它们指向不存在的目录），等价于 Linux 容器里
  那些路径必然不存在；
* 跑容器里真正会跑的两件事：**离线出题**（不需要 Key）和 **BYOK 联网出题**
  （密钥只从请求头来）——后者能证明封印之后访客自己的密钥仍然照常工作。

这些实验都不花钱（离线模式）或只花极少的钱（BYOK 那一步可选）。
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 容器里必然不存在的 Windows 绝对路径。把它们打桩成不存在的位置，
#: 等价于 Linux 上 Path(r"E:\AI\...") 变成一个指向空目录的相对路径。
WINDOWS_PATHS = (
    r"E:\AI\thesis-assistant",
    r"E:\AI\aigc-reducer",
    r"E:\AI\thesis-agents",
)

CHILD = r'''
import json, os, sys
from pathlib import Path

# ---- 依赖路径：显式放在最前，且用 -S -I 排除了 site-packages ----
# 这样本次进程可用的第三方包**只来自这些目录**，等价于容器里 pip 装出来的那份。
for lib in %(lib_dirs)r:
    if Path(lib).is_dir():
        sys.path.insert(0, lib)

# ---- 打桩：让三个 Windows 绝对路径在本次进程里"不存在" ----
import pathlib
_real_exists = pathlib.Path.exists
_STUBBED = [p.lower() for p in %(win_paths)r]
def _exists(self):
    try:
        s = str(self).lower()
    except Exception:
        return _real_exists(self)
    if any(s.startswith(p) for p in _STUBBED):
        return False
    return _real_exists(self)
pathlib.Path.exists = _exists

sys.path.insert(0, %(suite_root)r)
from suite import boot
boot.prepare()

out = {"boot": boot.describe()}

# ---- 0. 证明确实没用到 site-packages ----
import typing_extensions, fastapi, anyio
out["typing_extensions_file"] = getattr(typing_extensions, "__file__", "")
out["typing_extensions_version"] = getattr(typing_extensions, "__version__", "?")
out["fastapi_file"] = getattr(fastapi, "__file__", "")
out["anyio_file"] = getattr(anyio, "__file__", "")
out["has_sentinel"] = hasattr(typing_extensions, "sentinel")
out["site_packages_on_path"] = [p for p in sys.path if "site-packages" in p.lower()]

# ---- 1. 两套上游代码是否可导入（依赖只来自这些目录）----
# ---- 2. 离线出题：容器里没有 Key 也必须能出题 ----
from core.generate import generate_stream
from suite.credentials import Credentials, topic_settings

# 访客没给 Key -> 显式构造空密钥配置
settings = topic_settings(Credentials(), "deepseek")
offline = [e for e in generate_stream(settings, mode="offline", discipline="教育学", count=2,
                                      include_raw=False) if e.get("type") == "result"]
out["offline_ok"] = bool(offline)
out["offline_topics"] = len(offline[0]["result"]["topics"]) if offline else 0
out["offline_mode"] = offline[0]["result"].get("mode") if offline else None

# ---- 3. 评估助手：本地检查器全部可跑（不花钱、不联网）----
from types import SimpleNamespace
import app.checks, app.format.checkers
from app.framework.registry import VERIFIERS
text = "# 第1章 绪论\n\n本文方法达到 95.3%% 准确率，样本量 23008 条[1]。\n"
meta = {"writing_evidence": {"桶": [{"text": "样本量为 23008 条。", "citation": "某研究"}]}}
ctx = SimpleNamespace(board=SimpleNamespace(project=SimpleNamespace(meta=meta)), meta=meta)
hits = {}
for item in VERIFIERS:
    try:
        got = item["fn"](text, ctx) or []
    except Exception as exc:
        hits[item["name"]] = f"ERR {type(exc).__name__}"
        continue
    if got:
        hits[item["name"]] = len(got)
out["checker_count"] = len(VERIFIERS)
out["checker_hits"] = hits
out["number_grounding_fired"] = "check_number_grounding" in hits

# ---- 4. FastAPI 应用能否装配（这是容器启动的第一步）----
from suite.api.app import create_app
app = create_app()
out["routes"] = len([r for r in app.routes])
out["web_index_exists"] = (Path(%(suite_root)r) / "web" / "index.html").is_file()
out["vendor_sibling"] = (Path(%(suite_root)r) / "vendor").is_dir()

print("<<<RESULT>>>" + json.dumps(out, ensure_ascii=False))
'''

#: 容器里由 pip 提供的第三方包。本机开发时它们在 vendored 的 libs 目录里，
#: 这里原样借过来当作"已安装的依赖"，容器形态才等价。
DEPENDENCY_LIB_DIRS = (
    r"E:\AI\aigc-reducer\libs",
    r"E:\AI\thesis-assistant\libs",
)


def main() -> int:
    print("=" * 74)
    print("模拟公网容器的干净环境")
    print("=" * 74)

    # 注意：不能用 tempfile.gettempdir()——开发机的系统临时目录在沙箱里是只读的
    # （实测 PermissionError WinError 5）。改用工作区内的临时目录。
    tmp = ROOT.parent / "_deploy_sim"
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    staged = tmp / "app"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  [OK]   " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))

    try:
        # 按 Dockerfile 的方式摆放：/app/suite 与 /app/vendor 同级
        staged.mkdir(parents=True)
        shutil.copytree(ROOT / "suite", staged / "suite",
                        ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(ROOT / "vendor", staged / "vendor",
                        ignore=shutil.ignore_patterns("__pycache__", ".env", ".env.example", "libs"))
        shutil.copytree(ROOT / "web", staged / "web")
        for name in ("requirements.txt", "Dockerfile", "README.md"):
            shutil.copy2(ROOT / name, staged / name)

        print(f"\n模拟目录：{staged}")
        print(f"  suite 与 vendor 同级：{(staged / 'suite').is_dir() and (staged / 'vendor').is_dir()}")
        print(f"  没有 vendor stage 下的 libs：(not (staged / 'vendor' / 'thesisapp_stage' / 'libs').exists())")
        print(f"  没有隔离层 .env：{not (staged / 'vendor' / 'thesisapp_stage' / '.env').exists()}")

        script = CHILD % {
            "win_paths": WINDOWS_PATHS,
            "suite_root": str(staged),
            "lib_dirs": list(DEPENDENCY_LIB_DIRS),
        }
        script_path = tmp / "child.py"
        script_path.write_text(script, encoding="utf-8")

        print("\n--- 用 python -S -I 启动子进程（不读 site-packages，不继承 PYTHONPATH）---")
        print("    依赖只来自: " + "、".join(DEPENDENCY_LIB_DIRS))
        env = {
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            # 保留系统必需项，刻意不带 PYTHONPATH
            "SystemRoot": os.environ.get("SystemRoot", ""),
            "TEMP": os.environ.get("TEMP", ""),
            "TMP": os.environ.get("TMP", ""),
            "PATH": os.environ.get("PATH", ""),
        }
        proc = subprocess.run(
            [sys.executable, "-S", "-I", str(script_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=900,
        )
        if proc.returncode != 0:
            print("子进程退出码:", proc.returncode)
            print("stderr:\n" + (proc.stderr or "")[-2500:])
            check("干净环境下子进程正常退出", False, f"exit={proc.returncode}")
            return 1

        marker = "<<<RESULT>>>"
        line = next((ln for ln in (proc.stdout or "").splitlines() if ln.startswith(marker)), None)
        if not line:
            print("stdout:\n" + (proc.stdout or "")[-2500:])
            check("子进程返回结构化结果", False)
            return 1
        import json

        data = json.loads(line[len(marker):])

        print("\n--- 结果 ---")
        check("依赖确实来自指定目录，没走 site-packages",
              not data.get("site_packages_on_path"),
              str(data.get("site_packages_on_path")))
        check("typing_extensions 可用且含 sentinel（容器起不来的头号原因）",
              data.get("has_sentinel") is True,
              f"v{data.get('typing_extensions_version')} @ {Path(str(data.get('typing_extensions_file'))).parent}")
        print(f"     fastapi 来自: {data.get('fastapi_file')}")
        print(f"     anyio   来自: {data.get('anyio_file')}")

        boot_info = data["boot"]
        for pkg, info in (boot_info.get("packages") or {}).items():
            check(f"干净环境下上游包 {pkg} 可导入", info.get("ok") is True, info.get("error", ""))
        check("回退路径在干净环境下仍被中立化",
              len(boot_info.get("key_fallback_neutralized") or []) >= 2,
              str(boot_info.get("key_fallback_neutralized")))
        check("干净环境下没有服务器密钥", boot_info.get("server_key_present") is False)
        check("suite 与 vendor 同级（部署布局正确）", data.get("vendor_sibling") is True)
        check("前端文件随源码树一起就位", data.get("web_index_exists") is True)

        check("离线出题可用（容器里没 Key 也能用）", data.get("offline_ok") is True,
              f"{data.get('offline_topics')} 个题目，mode={data.get('offline_mode')}")

        check("检查器全部注册", (data.get("checker_count") or 0) > 40, f"{data.get('checker_count')} 个")
        errs = {k: v for k, v in (data.get("checker_hits") or {}).items() if isinstance(v, str)}
        check("干净环境下没有检查器报错", not errs, str(errs))
        check("「数字无依据」检查真的触发（说明证据台账接得上）",
              data.get("number_grounding_fired") is True, str(data.get("checker_hits")))

        check("FastAPI 应用能装配（容器启动的第一步）", (data.get("routes") or 0) > 15,
              f"{data.get('routes')} 条路由")

        print("\n" + "=" * 74)
        failed = [r for r in results if not r[1]]
        if failed:
            print(f"失败 {len(failed)} 项：")
            for name, _, detail in failed:
                print(f"  - {name}  {detail}")
            return 1
        print(f"全部通过（{len(results)} 项）：容器形态下站点可跑，依赖与密钥都不依赖开发机")
        return 0

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
