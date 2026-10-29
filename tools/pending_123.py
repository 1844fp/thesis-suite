# -*- coding: utf-8 -*-
"""变更看板：工作区相对 1844fp/123 还有哪些改动、够不够格同步。

统领者的"待办队列"：只看一眼就知道有没有子 agent 的产出等着上线。
只读，不推送。

用法：
    python tools/pending_123.py            # 列出全部差异
    python tools/pending_123.py --quiet     # 只在有差异时输出一行（给脚本用）
    exit code: 0=已同步  1=有差异  2=出错
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROXY = "http://127.0.0.1:7897"

#: 值得单独点名的高价值区域——统领review时优先看这些
WATCH = {
    "web/": "前端（访客直接看到）",
    "suite/api/": "站点接口",
    "suite/agents/": "智能体链路",
    "suite/": "后端内核",
    "tools/": "工具与自检",
}


def watch(interval: int, quiet: bool) -> int:
    """被动守夜：定期报告差异，**绝不自动推送**（发布始终由统领手动触发）。

    只报警不行动，是因为推送是对线上仓库的写操作；自动推送会在子 agent 改到
    一半时把半成品镜像上去。守夜的价值是让统领知道"有产出等着上线"。
    """
    import time
    from datetime import datetime

    last = None
    print(f"[守夜开始] 每 {interval}s 检查一次；仅报告，不推送。Ctrl+C 结束。", flush=True)
    while True:
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--quiet"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                env=os.environ,
            )
            line = (proc.stdout or "").strip()
        except Exception as exc:
            line = f"检查出错: {exc}"
        if line != last:
            print(f"[{stamp}] {line}", flush=True)
            if line.startswith("pending=") and not line.startswith("pending=0"):
                print(f"[{stamp}] ↑ 有子 agent 产出待同步，"
                      f"统领可执行：python tools/sync_123.py", flush=True)
            last = line
        elif not quiet:
            print(f"[{stamp}] {line}（无变化）", flush=True)
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--watch", type=int, default=0, metavar="SECONDS",
                    help="守夜模式：每 N 秒报告一次差异，不推送")
    args = ap.parse_args()

    if args.watch:
        return watch(args.watch, args.quiet)

    os.environ.setdefault("HTTP_PROXY", PROXY)
    os.environ.setdefault("HTTPS_PROXY", PROXY)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("缺少 GITHUB_TOKEN", file=sys.stderr)
        return 2

    import requests

    from tools.push_github import collect_files, should_skip

    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "pending-123/1.0",
    })
    r = s.get("https://api.github.com/repos/1844fp/123/git/trees/main",
              params={"recursive": "1"}, timeout=60)
    if r.status_code != 200:
        print(f"读取远端失败：HTTP {r.status_code}", file=sys.stderr)
        return 2
    remote: dict[str, int] = {}
    for it in r.json()["tree"]:
        if it["type"] != "blob":
            continue
        p = it["path"]
        name = p.rsplit("/", 1)[-1]
        suffix = "." + name.rsplit(".", 1)[1] if "." in name else ""
        if should_skip(p, name, suffix):
            continue
        remote[p] = it.get("size", 0)
    local = {rel: len(data) for rel, data in collect_files()}

    added = sorted(set(local) - set(remote))
    removed = sorted(set(remote) - set(local))
    changed = sorted(p for p in set(local) & set(remote) if local[p] != remote[p])
    total = len(added) + len(changed) + len(removed)

    if args.quiet:
        print(f"pending={total} added={len(added)} changed={len(changed)} deleted={len(removed)}")
        return 0 if total == 0 else 1

    print(f"=== 待同步变更（本地 {len(local)} / 远端 {len(remote)}）===")
    if total == 0:
        print("  已同步，无差异。")
        return 0

    def bucket(items):
        groups: dict[str, list[str]] = {}
        for p in items:
            key = next((k for k in WATCH if p.startswith(k)), "其它")
            groups.setdefault(key, []).append(p)
        return groups

    for label, items in (("新增", added), ("修改", changed), ("将被删除", removed)):
        if not items:
            continue
        print(f"\n-- {label}：{len(items)} --")
        for group, paths in sorted(bucket(items).items()):
            print(f"  [{group}] {WATCH.get(group, '')}")
            for p in paths:
                if label == "修改":
                    print(f"    {p}  {remote[p]} B -> {local[p]} B")
                else:
                    print(f"    {p}")

    print(f"\n合计 {total} 处变更。同步：python tools/sync_123.py"
          + ("（含删除，需 --allow-delete）" if removed else ""))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
