"""极简限流：按 IP 计数，**默认关闭**。

为什么默认关闭
--------------
站主明确选了"不加限制"。这个模块的存在意义不是悄悄拦住访客，
而是**让站主在被刷之后能一行环境变量就止血**，不必改代码、不必重新部署：

    SUITE_RATE_LIMIT=60        # 每个 IP 每分钟最多 60 次
    SUITE_RATE_LIMIT=0         # 或不设 → 关闭（默认）

设计上刻意做得小
----------------
* 只在内存里记计数，不引 Redis；进程重启即清零。
  **代价是它只在单进程内有效**——多 worker 时每个 worker 各算一份。
  这一点写在下面，不假装它是分布式限流。
* 只统计**会花钱或会干活的**接口，不统计静态资源与健康检查——
  否则正常打开页面就会被算进额度。
* 超限时返回 429，并在响应头里给 `Retry-After`。

一个诚实的限制：拿不到真实 IP 时可以退化成"限全局"。
反向代理后面 `request.client.host` 是代理的地址，那样所有访客共用一个计数桶——
那会把所有人一起拦住。所以这里**只在拿得到 X-Forwarded-For 或直连 IP 时才启用**，
拿不到就退化为不限（宁可被刷，也不要把所有正常访客一起挡在门外）。
"""

from __future__ import annotations

import os
import time
from collections import defaultdict, deque

#: 每个 IP 每分钟允许的请求数。**不设或设为 0 表示关闭**（默认）。
_RATE_PER_MIN = int(os.environ.get("SUITE_RATE_LIMIT", "0") or 0)

#: 需要计数的路径前缀。只有这些会有实际开销。
COUNTED_PREFIXES = (
    "/api/topics/generate",
    "/api/gather/extract",
    "/api/gather/import",
    "/api/gather/verify",
    "/api/writing/draft",
    # 单章重写与整篇生成一样要调模型、要联网检索，开销相当，所以一并计数。
    "/api/writing/section",
    "/api/writing/import-plan",
    "/api/review/opinion",
    "/api/kb/upload",
    "/api/kb/add",
    # 开题报告：生成会逐节联网检索 + 调模型；导入会对每个文件做解析与全文索引。
    "/api/proposal/generate",
    "/api/proposal/import-plan",
    # 参考文献推荐：检索三个外部库 + 可选一次模型精排。选中文献入库也要建全文索引。
    "/api/refs/recommend",
    "/api/refs/to-kb",
    # PaperQA2：建索引对每篇文献调一次模型，问答与简报各自是十几次调用，
    # 是本站单次请求里最贵的路径，必须计数。
    "/api/pqa/index",
    "/api/pqa/ask",
    "/api/pqa/brief",
    "/api/agents/",
)

#: 窗口长度（秒）。固定 60 秒。
WINDOW = 60.0

_hits: dict[str, deque[float]] = defaultdict(deque)


def enabled() -> bool:
    return _RATE_PER_MIN > 0


def limit() -> int:
    return _RATE_PER_MIN


def should_count(path: str) -> bool:
    return any(path.startswith(p) for p in COUNTED_PREFIXES)


def client_ip(request) -> str:
    """取客户端 IP。

    代理场景优先用 X-Forwarded-For 的**第一段**（最靠近客户端的那一跳）；
    拿不到就退回直连地址。两者都没有时返回空串——调用方据此决定"不限"。
    """
    fwd = request.headers.get("x-forwarded-for") or ""
    if fwd:
        first = fwd.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("x-real-ip") or ""
    if real.strip():
        return real.strip()
    client = getattr(request, "client", None)
    host = getattr(client, "host", "") if client else ""
    return host or ""


def check(request, path: str) -> tuple[bool, int, int]:
    """记一次并判断是否超限。返回 ``(allowed, remaining, retry_after)``。

    关闭时直接放行（``remaining`` 返回 -1 表示"未启用"）。
    """
    if not enabled() or not should_count(path):
        return True, -1, 0

    ip = client_ip(request)
    if not ip:
        # 拿不到真实 IP —— 宁可不限，也不要把所有访客塞进同一个桶。
        return True, -1, 0

    now = time.time()
    bucket = _hits[ip]
    while bucket and now - bucket[0] > WINDOW:
        bucket.popleft()

    if len(bucket) >= _RATE_PER_MIN:
        retry = int(WINDOW - (now - bucket[0])) + 1
        return False, 0, max(1, retry)

    bucket.append(now)
    return True, max(0, _RATE_PER_MIN - len(bucket)), 0


def stats() -> dict:
    """给自检接口用：当前配置与在跟踪的 IP 数。"""
    now = time.time()
    active = sum(1 for b in _hits.values() if b and now - b[-1] <= WINDOW)
    return {
        "enabled": enabled(),
        "per_minute": _RATE_PER_MIN,
        "tracked_ips": active,
        "note": (
            "默认关闭。站主可设 SUITE_RATE_LIMIT=60 开启（每 IP 每分钟 60 次）。"
            "计数只在内存、只在单进程内有效；多 worker 时每个 worker 各算一份。"
        ),
    }
