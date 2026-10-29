"""把 thesis-suite 推到 GitHub（本机没有 git，只能走 REST API）。

为什么不用 git：这台机器上 `git` 与 `gh` 都不存在，装一套 git 只为了推一次代码
代价过高。GitHub 的 Git Data API 可以在**少数几次请求**里完成整仓库推送：

    1. 创建仓库                        POST /user/repos
    2. 一次建好全部 blob + tree        POST /git/trees   （内联内容，98 个文件一次搞定）
    3. 建提交                          POST /git/commits
    4. 移动分支引用                    PATCH /git/refs/heads/main

token 从命令行参数或环境变量 GITHUB_TOKEN 读，**不写进任何文件**。

用法::

    python tools/push_github.py --repo thesis-suite --check
    python tools/push_github.py --repo thesis-suite
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"

#: 不推送的东西。与根目录 .gitignore 保持同样的口径。
#:
#: ``logs`` 整目录排除，但 ``logs/last_draft.json`` 例外——它是被**有意提交**的
#: 草稿缓存（远端 main 里只有这一个 logs 文件）。其余 logs 内容都是运行期产物：
#: ``service.pid``（PID 每次启动都变）、``*.log``、``watchdog.log``。
#: 早期只按后缀排除了 ``.log``，漏掉了 ``.pid``，于是每次同步都会冒出一个
#: "logs/service.pid 有变化"的假待推送——它永远不该进仓库。
SKIP_DIRS = {"__pycache__", "data", "exports", ".cache", ".tmp", ".git", "logs"}
SKIP_NAMES = {".env", "models.json"}
SKIP_SUFFIX = (".pyc", ".pyo", ".log", ".pid")
#: logs 目录下允许进入仓库的例外（相对仓库根的 POSIX 路径）。
KEEP_PATHS = {"logs/last_draft.json"}


def should_skip(rel_posix: str, name: str, suffix: str) -> bool:
    """统一的排除口径。对比工具（pending/diff）也必须调用它，
    否则会出现"看板说有变化、实际推不上去"的假差异。"""
    if rel_posix in KEEP_PATHS:
        return False
    if any(part in SKIP_DIRS for part in rel_posix.split("/")):
        return True
    if name in SKIP_NAMES or name.startswith("_probe"):
        return True
    return suffix in SKIP_SUFFIX


def collect_files() -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        rel_posix = rel.as_posix()
        if should_skip(rel_posix, path.name, path.suffix):
            continue
        data = path.read_bytes()
        out.append((rel.as_posix(), data))
    return out


class GitHub:
    def __init__(self, token: str) -> None:
        self.token = token

    def call(self, method: str, path: str, payload: dict | None = None,
             *, content_type: str = "application/json") -> tuple[int, dict]:
        url = API + path
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", f"token {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("User-Agent", "thesis-suite-push")
        if body:
            # Content-Type 必须显式给。有些接口（blob）缺了它就报 malformed request，
            # 而错误信息里完全看不出是头部的问题。
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {"raw": raw[:500]}
            return exc.code, parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    # 默认仓库是 1844fp/123。**不要改回 thesis-suite**——那是本项目早期的仓库，
    # 站主已明确"后续都在 123 上进行更改"。默认值写在这里、而不是靠调用方每次传参：
    # 凭惯性推错仓库会让两个仓库分叉，而分叉之后很难判断哪个是最新的。
    parser.add_argument("--repo", default="123")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--private", action="store_true", help="建私有仓库")
    parser.add_argument("--check", action="store_true", help="只做只读检查，不推送")
    args = parser.parse_args()

    if not args.token:
        print("缺少 token：用 --token 或环境变量 GITHUB_TOKEN 传入。", file=sys.stderr)
        return 2

    gh = GitHub(args.token)
    status, user = gh.call("GET", "/user")
    if status != 200:
        print(f"token 不可用：HTTP {status} {user}", file=sys.stderr)
        return 2
    owner = user["login"]
    print(f"账号：{owner}")

    files = collect_files()
    total = sum(len(d) for _, d in files)
    print(f"待推送 {len(files)} 个文件，共 {total / 1024:.0f} KB")
    biggest = max(files, key=lambda item: len(item[1]))
    print(f"最大文件：{biggest[0]}（{len(biggest[1]) / 1024:.0f} KB）")

    # 安全自查：绝不能把密钥推上去
    import re

    secret = re.compile(rb"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
    leaks = [name for name, data in files if secret.search(data)]
    if leaks:
        print(f"!! 检出疑似密钥，已中止：{leaks}", file=sys.stderr)
        return 3
    print("密钥自查：未发现疑似密钥")

    if args.check:
        print("（--check 模式，未推送）")
        return 0

    # ---- 1. 仓库 ----
    status, repo = gh.call("GET", f"/repos/{owner}/{args.repo}")
    if status == 200:
        print(f"仓库已存在：{repo['full_name']}（默认分支 {repo['default_branch']}）")
    else:
        status, repo = gh.call("POST", "/user/repos", {
            "name": args.repo,
            "description": "论文助手四合一站点：题目生成 / 资料获取 / 写作 / 评估，访客自带 API Key",
            "private": bool(args.private),
            "has_issues": True,
            "has_wiki": False,
            "auto_init": False,
        })
        if status not in (200, 201):
            print(f"建仓库失败：HTTP {status} {repo}", file=sys.stderr)
            return 4
        print(f"已创建仓库：{repo['full_name']}")

    branch = repo.get("default_branch") or "main"

    # ---- 1.5 空仓库要先初始化 ----
    #
    # GitHub 的 Git Data API 在**空仓库**上建树会返回 409 "Git Repository is empty"：
    # 树必须挂在一个已有的 ref 上，而空仓库连一个提交都没有。
    # 所以先用 Contents API 提交一个占位文件——它会顺带创建默认分支，
    # 之后建树、建提交、移动 ref 就都能走通了。占位文件随后被真实的树覆盖。
    status, ref = gh.call("GET", f"/repos/{owner}/{args.repo}/git/ref/heads/{branch}")
    if status != 200:
        print("仓库是空的，先做一次初始化提交…")
        status, init = gh.call("PUT", f"/repos/{owner}/{args.repo}/contents/.gitkeep", {
            "message": "chore: 初始化仓库",
            "content": base64.b64encode(b"").decode("ascii"),
            "branch": branch,
        })
        if status not in (200, 201):
            print(f"初始化失败：HTTP {status} {init}", file=sys.stderr)
            return 4
        print("初始化完成")

    # ---- 2. 建树 ----
    #
    # **分批内联**，而不是"一个文件一个 blob"。
    #
    # 两种做法都踩过，所以这里取中间：
    #   · 全部内联成一次请求 → 文件涨到 118 个、约 1.9 MB 时被 GitHub 以
    #     `HTTP 400 malformed request` 拒掉（内联对**单次请求体积**有隐性上限）；
    #   · 一个文件一个 blob → 145 个文件 = 145 次请求，实测**跑到 10 分钟超时还没完**，
    #     而且中途失败要重来。
    #
    # 所以按**累计字节数**分批：每批控制在 400 KB 以内、且不超过 60 个文件，
    # 一批一次 tree 请求（用 `base_tree` 往上一批上叠）。145 个文件约 2.28 MB
    # → 大约 6 批，比 145 次请求快一个数量级，又不会触发体积上限。
    BATCH_BYTES = 400 * 1024
    BATCH_FILES = 60

    batches: list[list[tuple[str, bytes]]] = []
    current: list[tuple[str, bytes]] = []
    size = 0
    for name, data in files:
        if current and (size + len(data) > BATCH_BYTES or len(current) >= BATCH_FILES):
            batches.append(current)
            current, size = [], 0
        current.append((name, data))
        size += len(data)
    if current:
        batches.append(current)

    tree_sha = ""
    for bi, batch in enumerate(batches, start=1):
        entries = []
        for name, data in batch:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                # 内联要求合法 UTF-8。**宁可明确报错，也不要静默替换字符把文件写坏**。
                print(f"!! {name} 不是合法 UTF-8，内联上传会写坏它：{exc}", file=sys.stderr)
                return 5
            entries.append({"path": name, "mode": "100644", "type": "blob", "content": text})
        payload_tree: dict = {"tree": entries}
        if tree_sha:
            payload_tree["base_tree"] = tree_sha
        status, made = gh.call("POST", f"/repos/{owner}/{args.repo}/git/trees", payload_tree)
        if status not in (200, 201):
            print(f"建 tree 失败（第 {bi}/{len(batches)} 批）：HTTP {status} "
                  f"{json.dumps(made, ensure_ascii=False)[:400]}", file=sys.stderr)
            return 5
        tree_sha = made["sha"]
        done = sum(len(b) for b in batches[:bi])
        print(f"  已写入 {done}/{len(files)} 个文件（第 {bi}/{len(batches)} 批）")

    print(f"tree 已建：{tree_sha[:10]}")

    # ---- 3. 提交 ----
    status, ref = gh.call("GET", f"/repos/{owner}/{args.repo}/git/ref/heads/{branch}")
    parents: list[str] = []
    if status == 200:
        parents = [ref["object"]["sha"]]
        print(f"现有分支 {branch} 在 {parents[0][:10]}，将作为父提交")

    payload: dict = {
        "message": "论文助手四合一站点：题目生成 / 资料获取 / 写作 / 评估（访客自带 API Key）",
        "tree": tree_sha,
    }
    if parents:
        payload["parents"] = parents
    status, commit = gh.call("POST", f"/repos/{owner}/{args.repo}/git/commits", payload)
    if status not in (200, 201):
        print(f"建提交失败：HTTP {status} {commit}", file=sys.stderr)
        return 6
    commit_sha = commit["sha"]
    print(f"提交已建：{commit_sha[:10]}")

    # ---- 4. 移动分支引用 ----
    if parents:
        status, res = gh.call("PATCH", f"/repos/{owner}/{args.repo}/git/refs/heads/{branch}",
                              {"sha": commit_sha, "force": False})
    else:
        status, res = gh.call("POST", f"/repos/{owner}/{args.repo}/git/refs",
                              {"ref": f"refs/heads/{branch}", "sha": commit_sha})
    if status not in (200, 201):
        print(f"更新分支失败：HTTP {status} {res}", file=sys.stderr)
        return 7

    print(f"\n推送完成：https://github.com/{owner}/{args.repo}")
    print(f"分支：{branch}  提交：{commit_sha[:10]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
