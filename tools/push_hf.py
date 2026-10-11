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

#: 代理。**这一条是必须的，不是可选优化。**
#:
#: 实测环境：huggingface.co 直连会超时（12 秒无响应），但走本机代理
#: ``127.0.0.1:7897`` 时 0.3 秒可达。开发机上 PowerShell 会自动用系统代理，
#: 而 Python 的 requests **默认走环境变量**（HTTP_PROXY/HTTPS_PROXY），
#: 环境变量没设时就是直连——于是部署会莫名其妙地卡死或超时。
#: 之前 pip 失败也是同一个原因（没带代理），当时误判成"出网被禁"。
#:
#: 按顺序取第一个可用的：显式参数 > 环境变量 > 常见本地代理端口。
PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
DEFAULT_PROXY_CANDIDATES = (
    "http://127.0.0.1:7897",   # Clash 系默认混合端口（本机实测可用）
    "http://127.0.0.1:7890",   # Clash 传统 HTTP 端口
    "http://127.0.0.1:10809",  # v2rayN 默认
    "http://127.0.0.1:1080",   # 通用 SOCKS/HTTP
)


def probe_proxy(session, candidates=None) -> str:
    """探测哪个代理能连通 Hugging Face。返回可用的代理 URL，都不通则空串。

    先试环境变量（用户可能已经配好），再逐个试常见本地端口。
    每次探测都很短（6 秒），失败就换下一个——比让用户干等一个 300 秒超时好。
    """
    tried: list[str] = []
    env_proxy = next((os.environ.get(k) for k in PROXY_ENV_KEYS if os.environ.get(k)), "")
    ordered = ([env_proxy] if env_proxy else []) + list(candidates or DEFAULT_PROXY_CANDIDATES)

    for proxy in ordered:
        if not proxy or proxy in tried:
            continue
        tried.append(proxy)
        try:
            r = session.get(f"{HF}/api/whoami-v2", headers={"User-Agent": "probe"},
                            proxies={"http": proxy, "https": proxy}, timeout=6)
            # 401 也算"通了"——重点是 TCP/TLS 能到 HF，至于 token 对不对是下一步的事。
            if r.status_code in (200, 401, 403):
                print(f"代理可用：{proxy}（HTTP {r.status_code}）")
                return proxy
        except Exception:
            print(f"  代理不可用：{proxy}")
    return ""


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

    # 端口必须跟随平台的 $PORT：Hugging Face 固定 7860，Render / Railway 用 $PORT 注入。
    # 写死端口会在那些平台上被判为"监听端口不对"，表现为部署失败但日志没有明显报错。
    if "${PORT" not in dockerfile and "$PORT" not in dockerfile:
        print("!! Dockerfile 没有使用平台的 $PORT 环境变量，换平台部署会因端口不对而失败。",
              file=sys.stderr)
        return 8
    print("Dockerfile：监听端口跟随 $PORT（兼容 HF Spaces 与 Render/Railway）")

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
    parser.add_argument("--proxy", default="", help="显式指定代理；默认自动探测")
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

    # 先确定代理。直连在实测环境里会超时，必须显式探测而不是指望环境变量。
    session = requests.Session()
    proxies = {}
    if args.proxy:
        proxies = {"http": args.proxy, "https": args.proxy}
        print(f"使用指定代理：{args.proxy}")
    else:
        found = probe_proxy(session)
        if found:
            proxies = {"http": found, "https": found}
        else:
            print("未探测到可用代理，将直连 Hugging Face（实测环境里通常会超时）。", file=sys.stderr)

    def hdr(json_body: bool = False) -> dict:
        return headers(args.token, json_body)

    # ---- 0. token 与账号 ----
    try:
        r = session.get(f"{HF}/api/whoami-v2", headers=hdr(), proxies=proxies, timeout=40)
    except Exception as exc:  # noqa: BLE001
        print(f"无法连接 Hugging Face：{type(exc).__name__}: {exc}", file=sys.stderr)
        print("提示：用 --proxy http://127.0.0.1:端口 显式指定代理。", file=sys.stderr)
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
    r = session.get(f"{HF}/api/spaces/{space_id}", headers=hdr(), proxies=proxies, timeout=40)
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
        r = session.post(f"{HF}/api/repos/create", headers=hdr(True),
                         data=json.dumps(payload), proxies=proxies, timeout=90)
        if r.status_code == 402:
            # 2026 年 7 月起，Hugging Face 把 Docker / Gradio Space 的免费额度
            # 收到了付费订阅后面，免费账号建 Docker Space 会被 402 拒绝。
            # 这不是脚本或代码的问题，所以要把出路说清楚，而不是只回一个状态码。
            print("建 Space 失败：HTTP 402 —— Hugging Face 已不再向免费账号提供 Docker Space。", file=sys.stderr)
            print("", file=sys.stderr)
            print("服务商原文：Static Spaces are free for everyone, but hosting Gradio and Docker", file=sys.stderr)
            print("Spaces on free cpu-basic requires a PRO subscription.", file=sys.stderr)
            print("", file=sys.stderr)
            print("三条出路：", file=sys.stderr)
            print("  1) 换一个支持 Docker 的免费托管（本站已验证可用）：", file=sys.stderr)
            print("     · Render      —— 750 小时/月、512 MB，不需要信用卡，15 分钟无流量休眠", file=sys.stderr)
            print("     · SnapDeploy  —— 100 小时/月、4 个容器，不需要信用卡", file=sys.stderr)
            print("     用法：把这个 GitHub 仓库连过去，平台会直接读 Dockerfile。", file=sys.stderr)
            print("     注意：容器监听端口必须是平台给的 $PORT，我们的 Dockerfile 目前写死 7860。", file=sys.stderr)
            print("  2) 升级 Hugging Face PRO（约 $9/月），本脚本即可直接部署。", file=sys.stderr)
            print("  3) 只用静态 Space（免费，但**会丢掉后端**）：", file=sys.stderr)
            print("     53 个本地检查器、Crossref 核验、服务端转发都会失效，只剩纯前端。", file=sys.stderr)
            print("     不推荐——那等于把站点最值钱的部分砍掉。", file=sys.stderr)
            return 6
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
        r = session.post(f"{HF}/api/spaces/{space_id}/commit/main",
                         headers=hdr(True),
                         data=json.dumps(payload), proxies=proxies, timeout=600)
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
