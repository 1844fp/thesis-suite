"""把 thesis-suite 部署到 Hugging Face Spaces（Docker SDK）。

为什么单独写一个脚本而不是 `pip install huggingface_hub`：本机开发不用
requirements.txt（依赖都是 vendored 的），为了部署再装一套工具链不值得。
工作区里已有 ``requests``，HF 的 commit API 用它能直接调通。

token 从命令行或环境变量 ``HF_TOKEN`` 读，**不写进任何文件**。
token 需要 **write** 权限。

用法::

    # 只做检查：验证 token、列出将上传的文件、检查必需文件是否齐全
    python tools/push_hf.py --token hf_xxx --space thesis-suite --check

    # 真部署（自动建 Space 并上传）
    python tools/push_hf.py --token hf_xxx --space thesis-suite

部署完成后会打印公开地址：``https://huggingface.co/spaces/<用户名>/<space>``。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HF = "https://huggingface.co"

#: 与 push_github.py 保持同一口径的排除规则。
SKIP_DIRS = {"__pycache__", "data", "exports", ".cache", ".tmp", ".git"}
SKIP_NAMES = {".env", "models.json"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log")

#: 镜像里必须有这些，少了就启动不起来。部署前先自检，别等到 Space 构建失败才发现。
REQUIRED = ("Dockerfile", "requirements.txt", "README.md",
            "suite/api/app.py", "suite/boot.py", "web/index.html",
            "vendor/thesisapp_stage/app/server.py",
            "vendor/topichelper_stage/core/generate.py")


def collect_files() -> list[Path]:
    out: list[Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.name in SKIP_NAMES or path.name.startswith("_probe"):
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        out.append(path)
    return out


def headers(token: str, json_body: bool = False) -> dict:
    h = {"Authorization": f"Bearer {token}", "User-Agent": "thesis-suite-deploy"}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def offline_check() -> int:
    """不需要 token 的自检：文件清单、必需文件、README frontmatter、密钥自查。

    单独拆出来是因为**这些检查最容易出问题，却最容易被"等 token 到手再一起验"拖到最后**。
    真到部署那一刻才发现缺文件或 frontmatter 写错，代价是一次失败的构建排查。
    """
    files = collect_files()
    total = sum(p.stat().st_size for p in files)
    print(f"待上传 {len(files)} 个文件，共 {total / 1024:.0f} KB")

    biggest = max(files, key=lambda p: p.stat().st_size)
    print(f"最大文件：{biggest.relative_to(ROOT).as_posix()}（{biggest.stat().st_size / 1024:.0f} KB）")

    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    if missing:
        print(f"!! 缺少必需文件，Space 会构建失败：{missing}", file=sys.stderr)
        return 3
    print(f"必需文件齐全（{len(REQUIRED)} 项）")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    if "sdk: docker" not in readme[:400]:
        print("!! README.md 顶部缺少 `sdk: docker`，Space 不会按 Dockerfile 构建。", file=sys.stderr)
        return 5
    print("README frontmatter：sdk=docker 已声明")

    import re

    secret = re.compile(rb"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
    leaks = [p.relative_to(ROOT).as_posix() for p in files if secret.search(p.read_bytes())]
    if leaks:
        print(f"!! 检出疑似密钥，已中止：{leaks}", file=sys.stderr)
        return 4
    print("密钥自查：未发现疑似密钥")

    # Dockerfile 里必须有应用工厂的启动命令，否则 Space 起了也是空的。
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    if "suite.api.app:create_app" not in dockerfile or "--factory" not in dockerfile:
        print("!! Dockerfile 的 CMD 不是 `suite.api.app:create_app --factory`，"
              "应用工厂里的 sys.path 引导不会执行。", file=sys.stderr)
        return 6
    print("Dockerfile：启动命令已用 --factory 指向应用工厂")

    # vendor 两棵树都在，否则站点核心能力缺失。
    for tree in ("vendor/thesisapp_stage/app", "vendor/topichelper_stage/core"):
        n = len(list((ROOT / tree).rglob("*.py")))
        if n == 0:
            print(f"!! {tree} 里没有 .py 文件", file=sys.stderr)
            return 7
        print(f"{tree}：{n} 个 Python 文件")

    print("\n离线自检通过（不需要 token 的部分都可交付）。")
    return 0


def main() -> int:
    try:
        import requests
    except ImportError:
        print("需要 requests（工作区里应该已有）。", file=sys.stderr)
        return 2

    parser = argparse.ArgumentParser()
    parser.add_argument("--token", default=os.environ.get("HF_TOKEN", ""))
    parser.add_argument("--space", default="thesis-suite", help="Space 名称")
    parser.add_argument("--private", action="store_true")
    parser.add_argument("--check", action="store_true", help="需要 token：验证 token 与文件后再停")
    parser.add_argument("--offline-check", action="store_true",
                        help="本地自检：文件清单、必需文件、frontmatter、密钥（不联网、不需要 token）")
    args = parser.parse_args()

    if args.offline_check:
        return offline_check()

    if not args.token:
        print("缺少 token：用 --token 或环境变量 HF_TOKEN 传入（需要 write 权限）。", file=sys.stderr)
        print("若只想检查文件，用 --offline-check（不需要 token）。", file=sys.stderr)
        return 2

    # ---- 0. token 与账号 ----
    try:
        r = requests.get(f"{HF}/api/whoami-v2", headers=headers(args.token), timeout=30)
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接 Hugging Face：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if r.status_code != 200:
        print(f"token 不可用：HTTP {r.status_code} {r.text[:300]}", file=sys.stderr)
        return 2
    who = r.json()
    owner = who.get("name") or ""
    print(f"账号：{owner}  （token 类型：{who.get('type', '?')}）")

    # ---- 1. 文件自检 ----
    files = collect_files()
    total = sum(p.stat().st_size for p in files)
    print(f"待上传 {len(files)} 个文件，共 {total / 1024:.0f} KB")

    missing = [name for name in REQUIRED if not (ROOT / name).is_file()]
    if missing:
        print(f"!! 缺少必需文件，Space 会构建失败：{missing}", file=sys.stderr)
        return 3
    print(f"必需文件齐全（{len(REQUIRED)} 项）")

    # ---- 2. 密钥自查 ----
    import re

    secret = re.compile(rb"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
    leaks = [p.relative_to(ROOT).as_posix() for p in files if secret.search(p.read_bytes())]
    if leaks:
        print(f"!! 检出疑似密钥，已中止：{leaks}", file=sys.stderr)
        return 4
    print("密钥自查：未发现疑似密钥")

    # ---- 3. README 的 frontmatter 必须是 Docker SDK ----
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    head = readme[:400]
    if "sdk: docker" not in head:
        print("!! README.md 顶部缺少 `sdk: docker`，Space 不会按 Dockerfile 构建。", file=sys.stderr)
        print("   前 200 字符：" + head[:200], file=sys.stderr)
        return 5
    print("README frontmatter：sdk=docker 已声明")

    space_id = f"{owner}/{args.space}"
    url = f"{HF}/spaces/{space_id}"

    if args.check:
        print(f"\n（--check 模式，未部署。目标：{url}）")
        return 0

    # ---- 4. 建 Space（已存在则复用）----
    r = requests.get(f"{HF}/api/spaces/{space_id}", headers=headers(args.token), timeout=30)
    if r.status_code == 200:
        info = r.json()
        print(f"Space 已存在：{space_id}（sdk={info.get('sdk')}）")
    else:
        payload = {
            "name": args.space,
            "type": "space",
            "sdk": "docker",
            "private": bool(args.private),
            "hardware": "cpu-basic",
        }
        r = requests.post(f"{HF}/api/repos/create", headers=headers(args.token, True),
                          data=json.dumps(payload), timeout=60)
        if r.status_code not in (200, 201):
            print(f"建 Space 失败：HTTP {r.status_code} {r.text[:400]}", file=sys.stderr)
            return 6
        print(f"已创建 Space：{space_id}")

    # ---- 5. 一次提交上传全部文件 ----
    #
    # 用 commit API 的多文件操作：一次请求带上所有文件，避免逐文件上传。
    # 中文内容要 base64（NDJSON 那套接口对非 ASCII 不友好），所以统一走 base64。
    operations = []
    for path in files:
        rel = path.relative_to(ROOT).as_posix()
        operations.append({
            "operation": "add",
            "path": rel,
            "encoding": "base64",
            "content": base64.b64encode(path.read_bytes()).decode("ascii"),
        })

    # 分批：一次提交几千个操作会被拒，这里 40 个一批，稳妥且不至于太慢。
    BATCH = 40
    rev = None
    for start in range(0, len(operations), BATCH):
        chunk = operations[start:start + BATCH]
        payload = {
            "summary": f"部署论文助手四合一站点（{start + 1}-{start + len(chunk)}/{len(operations)}）",
            "operations": chunk,
        }
        if rev:
            payload["parentCommit"] = rev
        r = requests.post(f"{HF}/api/spaces/{space_id}/commit/main",
                          headers=headers(args.token, True),
                          data=json.dumps(payload), timeout=300)
        if r.status_code not in (200, 201):
            print(f"上传失败（第 {start // BATCH + 1} 批）：HTTP {r.status_code} {r.text[:500]}",
                  file=sys.stderr)
            return 7
        rev = (r.json() or {}).get("commitOid") or rev
        print(f"  已上传 {min(start + BATCH, len(operations))}/{len(operations)} 个文件")

    print("\n部署已提交。Space 会开始构建（首次通常 3~8 分钟）。")
    print(f"  Space 页面：{url}")
    print(f"  公开地址  ：https://{owner.lower()}-{args.space.lower().replace('_', '-')}.hf.space")
    print("\n构建状态可看 Space 页面的 Logs 标签；构建完成后打开公开地址即可使用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
