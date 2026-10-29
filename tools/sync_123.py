# -*- coding: utf-8 -*-
"""把工作区同步到 1844fp/123（私有托管仓库）——带守卫的一条命令。

这个脚本解决的问题不只是"推代码"，而是**别把线上推坏**：

    1. 预检    ：加载站点应用、打只读请求。改坏了在这里就拦住，不污染线上。
    2. 反向对比：远端有、本地没有的文件 = 会被镜像推送删掉。默认**中止**，
                因为 push_github.py 是整目录镜像，删除动作是静默的。
    3. 密钥自查：复用 push_github 的口径（sk-xxx / ghp_xxx / AKIA）。
    4. 推送    ：调用 tools/push_github.py（REST API，走本机代理）。
    5. 回读校验：推完重新拉远端 tree，逐文件比对 blob 大小，确认真的上去了。

用法：
    python tools/sync_123.py                 # 预检 + 对比 + 推送 + 校验
    python tools/sync_123.py --dry-run       # 只到"对比"为止，不推送
    python tools/sync_123.py --allow-delete  # 确实要删远端文件时用
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "https://api.github.com"
REPO = "1844fp/123"
BRANCH = "main"
PROXY = "http://127.0.0.1:7897"
BASELINE = Path(r"E:\AI\_sync")

sys.path.insert(0, str(ROOT))


def session():
    import requests

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise SystemExit("缺少 GITHUB_TOKEN 环境变量")
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "sync-123/1.0",
    })
    return s


def remote_tree(s) -> dict[str, int]:
    """远端文件清单，但用与推送**相同**的排除口径过滤。

    为什么要过滤：远端历史提交里可能留着运行期产物（例如先前某次推送带上去的
    logs/service.pid）。若不过滤，它们会被算成"远端有、本地无"，触发一次
    并不需要的删除告警——而真正的意图只是"别再推它"。
    """
    from tools.push_github import should_skip

    r = s.get(f"{API}/repos/{REPO}/git/trees/{BRANCH}", params={"recursive": "1"}, timeout=60)
    if r.status_code != 200:
        raise SystemExit(f"读取远端 tree 失败：HTTP {r.status_code} {r.text[:200]}")
    out: dict[str, int] = {}
    for it in r.json()["tree"]:
        if it["type"] != "blob":
            continue
        p = it["path"]
        name = p.rsplit("/", 1)[-1]
        suffix = "." + name.rsplit(".", 1)[1] if "." in name else ""
        if should_skip(p, name, suffix):
            continue
        out[p] = it.get("size", 0)
    return out


def local_files() -> dict[str, int]:
    from tools.push_github import collect_files

    return {rel: len(data) for rel, data in collect_files()}


def write_baseline(kind: str, tree: dict[str, int]) -> Path:
    BASELINE.mkdir(parents=True, exist_ok=True)
    out = BASELINE / f"{kind}.json"
    out.write_text(json.dumps(tree, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    return out


def step_preflight() -> bool:
    print("\n[1/5] 预检：加载应用 + 只读请求")
    env = dict(os.environ)
    env["PYTHONPATH"] = ";".join([
        str(ROOT),
        r"E:\AI\aigc-reducer\libs",
        r"E:\AI\thesis-assistant\libs",
    ])
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "preflight_123.py")],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    tail = (proc.stdout or "").strip().splitlines()
    for line in tail[-14:]:
        print("      " + line)
    if proc.returncode != 0:
        print("      预检未通过，已中止（线上未被改动）")
        return False
    return True


def sec_check(files: dict[str, int]) -> list[str]:
    import re

    from tools.push_github import collect_files

    pattern = re.compile(rb"sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}")
    return [name for name, data in collect_files() if pattern.search(data)]


def quiescence(threshold_sec: float, window_sec: float) -> bool:
    """静默闸门：确认工作区已经停止写入，再快照。

    为什么必须有：这个工作区会被其他 agent 并发编辑。实测遇到过一次——
    推送与回读之间 README.md 又被改写，导致回读校验失败。把"正在被写的目录"
    当静止目标快照，镜像上去的就是半成品。

    做法：采样两次全量文件指纹（路径+大小+mtime），要求窗口内零变化，
    且最近一次写入已过去 threshold_sec。
    """
    import time

    def snap() -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for p in ROOT.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if any(part in {"__pycache__", "logs", ".tmp", ".cache", "data", ".git"} for part in rel.parts):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            out[rel.as_posix()] = (st.st_size, st.st_mtime_ns)
        return out

    first = snap()
    time.sleep(window_sec)
    second = snap()
    moved = [k for k in set(first) & set(second) if first[k] != second[k]]
    moved += [k for k in set(second) - set(first)]
    if moved:
        print(f"      工作区正在被写入（{len(moved)} 个文件在 {window_sec:.0f}s 内变化）：")
        for k in sorted(moved)[:8]:
            print(f"        {k}")
        return False

    newest = max((v[1] for v in second.values()), default=0) / 1e9
    if newest:
        age = time.time() - newest
        print(f"      最近一次写入距今 {age:.0f}s（阈值 {threshold_sec:.0f}s）")
        if age < threshold_sec:
            print("      工作区刚被写过，等它稳定后再推（避免推上半成品）")
            return False
    return True


def wait_until_quiet(threshold_sec: float, window_sec: float, timeout_sec: float) -> bool:
    """等到工作区静止为止（或超时）。返回是否已静止。

    用途：子 agent 还在收尾时，没必要反复人工重试——等它停下来再快照。
    """
    import time

    deadline = time.time() + timeout_sec
    attempt = 0
    while True:
        attempt += 1
        if quiescence(threshold_sec, window_sec):
            print(f"      第 {attempt} 次探测：工作区已静止")
            return True
        if time.time() >= deadline:
            print(f"      第 {attempt} 次探测：等待超时（{timeout_sec:.0f}s），工作区仍在变动")
            return False
        wait = min(30.0, max(5.0, (deadline - time.time()) / 4))
        print(f"      第 {attempt} 次探测：仍在写入，{wait:.0f}s 后重试…")
        time.sleep(wait)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-delete", action="store_true")
    ap.add_argument("--force", action="store_true", help="跳过静默闸门（不推荐）")
    ap.add_argument("--quiesce-window", type=float, default=6.0,
                    help="静默检测采样窗口秒数，默认 6")
    ap.add_argument("--quiesce-age", type=float, default=5.0,
                    help="最近写入至少过去多少秒才算稳定，默认 5")
    ap.add_argument("--when-stable", type=float, default=0.0, metavar="SECONDS",
                    help="等到工作区静止再同步，最多等 N 秒（0=不等待）")
    args = ap.parse_args()

    os.environ.setdefault("HTTP_PROXY", PROXY)
    os.environ.setdefault("HTTPS_PROXY", PROXY)

    print(f"=== 同步工作区 -> {REPO}:{BRANCH} ===")
    print(f"    本地根目录：{ROOT}")

    print("\n[0/5] 静默闸门：确认工作区不再被写入")
    if args.force:
        print("      --force：已跳过")
    elif args.when_stable:
        print(f"      --when-stable：最多等 {args.when_stable:.0f}s 直到静止")
        if not wait_until_quiet(args.quiesce_age, args.quiesce_window, args.when_stable):
            print("      已中止（线上零改动）。稍后重跑，或加大 --when-stable。")
            return 6
    elif not quiescence(args.quiesce_age, args.quiesce_window):
        print("      已中止（线上零改动）。等编辑停止后重跑，或 --force 强制快照。")
        return 6

    if not step_preflight():
        return 1

    print("\n[2/5] 对比远端与本地")
    s = session()
    remote = remote_tree(s)
    local = local_files()
    before = write_baseline("remote-before", remote)
    write_baseline("local-current", local)
    print(f"      远端 {len(remote)} 个文件 / 本地 {len(local)} 个文件（基线已写 {before}）")

    added = sorted(set(local) - set(remote))
    removed = sorted(set(remote) - set(local))
    changed = sorted(p for p in set(local) & set(remote) if local[p] != remote[p])
    same = len(set(local) & set(remote)) - len(changed)

    for label, items in (("新增（本地有、远端无）", added),
                         ("修改（内容不同）", changed),
                         ("会被删除（远端有、本地无）", removed)):
        print(f"      {label}: {len(items)}")
        for p in items:
            if label.startswith("修改"):
                print(f"        {p}  {remote[p]} B -> {local[p]} B")
            else:
                print(f"        {p}")
    print(f"      未变化: {same}")

    if not (added or changed or removed):
        print("\n结论：远端已是最新，无需同步。")
        return 0

    if removed and not args.allow_delete:
        print("\n!! 远端存在本地没有的文件，镜像推送会把它们删掉。"
              "\n   确认无误会丢文件后，加 --allow-delete 重跑。（本次未推送）")
        return 2

    print("\n[3/5] 密钥自查")
    leaks = sec_check(local)
    if leaks:
        print(f"      !! 检出疑似密钥，已中止：{leaks}")
        return 3
    print("      未发现疑似密钥")

    if args.dry_run:
        print("\n[4/5] --dry-run：跳过推送")
        print("[5/5] --dry-run：跳过回读校验")
        print("\n结论：dry-run 通过，可实际推送。")
        return 0

    print("\n[4/5] 推送（REST API，整目录镜像为一次提交）")
    env = dict(os.environ)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "push_github.py"), "--repo", "123"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for line in (proc.stdout or "").strip().splitlines():
        print("      " + line)
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-8:]
        print("      推送失败：")
        for line in tail:
            print("        " + line)
        return 4

    print("\n[5/5] 回读校验：重新拉远端逐文件比对")
    after = remote_tree(s)
    write_baseline("remote-after", after)
    ok = after == local
    if ok:
        print(f"      一致：远端 {len(after)} 个文件与本地完全相同")
    else:
        only_r = sorted(set(after) - set(local))
        only_l = sorted(set(local) - set(after))
        diff = sorted(p for p in set(after) & set(local) if after[p] != local[p])
        print(f"      !! 不一致：仅远端 {only_r} / 仅本地 {only_l} / 大小不同 {diff}")
        return 5

    commit = s.get(f"{API}/repos/{REPO}/commits/{BRANCH}", timeout=30)
    if commit.status_code == 200:
        c = commit.json()
        print(f"      提交：{c['sha'][:10]}  {c['commit']['author']['date']}")
        print(f"      {c['commit']['message'].splitlines()[0]}")
    print(f"\n结论：同步完成 → https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
