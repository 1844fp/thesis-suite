# -*- coding: utf-8 -*-
"""验证前端资源服务的竞态修复：声明长度与实际字节必须永远一致。

要证明的事：把 FileResponse/StaticFiles 换成"整块读进内存"之后，
**即使文件在请求期间被并发改写**，也不会再出现
    RuntimeError: Response content longer than Content-Length

做法：用一个持续追写 index.html 的后台线程制造真实的竞争窗口，
同时不断请求 /，断言每一次响应都满足 len(body) == Content-Length。
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

ROOT = Path(r"E:\AI\thesis-suite")
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from suite.api.app import create_app  # noqa: E402

PAGE = ROOT / "web" / "index.html"
original = PAGE.read_bytes()

stop = threading.Event()
writes = [0]


def churn() -> None:
    """持续改写 index.html：先写大、再写小，制造长度抖动。"""
    while not stop.is_set():
        try:
            PAGE.write_bytes(original + (b"\n<!-- padding -->" * 400))
            writes[0] += 1
            PAGE.write_bytes(original)
            writes[0] += 1
        except OSError:
            pass
        time.sleep(0.001)


bad: list[str] = []
ok_count = 0
thread = threading.Thread(target=churn, daemon=True)
thread.start()

try:
    client = TestClient(create_app())
    for i in range(60):
        try:
            r = client.get("/")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"第 {i} 次请求抛异常：{type(exc).__name__}: {exc}")
            continue
        declared = r.headers.get("content-length")
        actual = len(r.content)
        if declared is not None and int(declared) != actual:
            bad.append(f"第 {i} 次长度不一致：声明 {declared} 实际 {actual}")
        elif r.status_code != 200:
            bad.append(f"第 {i} 次状态码 {r.status_code}")
        else:
            ok_count += 1
finally:
    stop.set()
    thread.join(timeout=5)
    PAGE.write_bytes(original)          # 还原文件
    print(f"已还原 index.html（{len(original)} 字节）")

print(f"\n并发改写次数：{writes[0]}")
print(f"成功且长度一致：{ok_count}/60")
if bad:
    print(f"问题 {len(bad)} 处：")
    for b in bad[:10]:
        print("  " + b)
else:
    print("未发现长度不一致或异常")

print("\n== 结论 ==\n  " + ("竞态已消除" if not bad else "仍有问题"))
raise SystemExit(0 if not bad else 1)
