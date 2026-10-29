"""语义向量：常驻子进程的客户端 + 生命周期管理。

为什么有这一层（一句话）
------------------------
知识库的检索原本是**纯关键词**（jieba + SQLite FTS5）。它能精确命中字面，
但查「组织支持」不会命中「职业倦怠」——而这两件事在文献里几乎是同一段话。
补上这一层之后，检索变成 **FTS5 + 向量 → RRF 融合**，两种召回互为补充。

三个不可让步的工程约束
----------------------
1. **绝不在主进程里 import torch。** 向量模型那一套依赖在 ``libs-pqa`` 里
   （numpy 2.5 / pydantic 2.14），主进程跑的是 numpy 2.3 / pydantic 2.13。
   同进程加载必有一方静默拿到错版本——这正是 PaperQA2 也走子进程的原因。
   所以这里只做**进程管理 + JSONL 通信**，一行 torch 代码都不 import。

2. **不可用时必须优雅降级，而不是报错。** 公网容器里既没有 ``libs-pqa``
   也没有 91 MB 的模型。那条路上检索就退回纯关键词，行为与改动前完全一致，
   并且**如实告诉调用方现在是哪种模式**（``status()["mode"]``）。

3. **冷启动不能挡住请求。** ``import torch`` 约 33 秒、加载模型约 7 秒。
   所以启动是**后台预热**：请求永远不会等它；预热完成前照常走关键词检索，
   完成后自动升级为混合检索。这一点在 ``search`` 里体现为"先试非阻塞取向量"。

一次查询的真实开销：预热后约 80 毫秒（实测 encode 3 条 0.08s）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SUITE_ROOT = Path(__file__).resolve().parent.parent
SERVER = SUITE_ROOT / "tools" / "embed_server.py"

#: 向量模型依赖所在目录。默认指向本机 thesis-assistant 里已有的那份，不复制。
EMBED_LIBS = Path(os.environ.get("SUITE_EMBED_LIBS") or r"E:\AI\thesis-assistant\libs-pqa")

#: 模型缓存目录（复用已下好的 91 MB，不重新下载）。
ST_HOME = Path(os.environ.get("SUITE_ST_HOME") or r"E:\AI\thesis-assistant\.tmp\st_home")

#: 默认模型，与 embed_server 保持一致（status 里会给前端显示）。
DEFAULT_MODEL = os.environ.get("SUITE_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")

#: 一次请求里最多编码几条。入库一篇 110 段的论文会分批发，避免单次请求过大。
MAX_BATCH = int(os.environ.get("SUITE_EMBED_BATCH") or 32)

#: 单次编码请求的等待上限（秒）。预热后实测 80ms，给 120s 是留给"模型刚加载完的
#: 第一批"以及机器被别的任务占满的情况。超时就当作本次不可用，**不阻塞检索**。
CALL_TIMEOUT = float(os.environ.get("SUITE_EMBED_TIMEOUT") or 120)

#: 是否在站点启动时后台预热。默认开；设 0 可完全关掉语义检索（回到纯关键词）。
AUTOSTART = os.environ.get("SUITE_EMBED_AUTOSTART", "1").strip().lower() not in {
    "0", "false", "no", "off",
}


class _State:
    """进程与状态的单一存放点。**全部访问都在 _lock 下**，避免并发启动两个子进程。"""

    proc: subprocess.Popen | None = None
    ready: bool = False
    warming: bool = False
    dim: int = 0
    model: str = ""
    error: str = ""
    started_at: float = 0.0
    ready_at: float = 0.0
    calls: int = 0
    last_call_ms: float = 0.0


_state = _State()
_lock = threading.Lock()


def available() -> bool:
    """依赖目录与桥接脚本都在，才有资格尝试语义检索。**不需要 torch 可导入。**"""
    return SERVER.is_file() and EMBED_LIBS.is_dir()


def ready() -> bool:
    """预热完成、可以立刻编码。"""
    with _lock:
        if _state.proc is not None and _state.proc.poll() is not None:
            # 子进程已死（被系统回收 / OOM）：立刻降级，并允许下次重新预热。
            _reset_locked(f"向量进程已退出（码 {_state.proc.returncode}）")
        return _state.ready


def status() -> dict:
    """给前端与自检看的状态。**必须能分清三种情况**：
    根本没有依赖 / 有依赖但还没预热完 / 已就绪。"""
    with _lock:
        if _state.proc is not None and _state.proc.poll() is not None:
            _reset_locked(f"向量进程已退出（码 {_state.proc.returncode}）")
        mode = "hybrid" if _state.ready else "keyword"
        return {
            "available": available(),
            "ready": _state.ready,
            "warming": _state.warming,
            "mode": mode,
            "model": _state.model or DEFAULT_MODEL,
            "dim": _state.dim,
            "libs": str(EMBED_LIBS),
            "error": _state.error,
            "calls": _state.calls,
            "last_call_ms": round(_state.last_call_ms, 1),
            "note": (
                "语义检索已就绪：检索 = 关键词（FTS5）+ 向量，用 RRF 融合。"
                if _state.ready else
                ("语义检索正在后台预热（首次加载 torch 约 30~40 秒），"
                 "这段时间检索走纯关键词，结果仍然可用。"
                 if _state.warming else
                 ("语义检索不可用（缺少向量依赖），检索走纯关键词。"
                  if not available() else "语义检索尚未启动。")),
            ),
        }


def _reset_locked(reason: str = "") -> None:
    """把状态清回"未就绪"。调用方必须已经持有 _lock。"""
    proc = _state.proc
    _state.proc = None
    _state.ready = False
    _state.warming = False
    _state.dim = 0
    if reason:
        _state.error = reason
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _child_env() -> dict:
    """子进程环境。**只碰这个 dict，不改 os.environ**（并发安全，与 paperqa 一致）。"""
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{EMBED_LIBS}{os.pathsep}{env.get('PYTHONPATH', '')}".rstrip(os.pathsep)
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env["SENTENCE_TRANSFORMERS_HOME"] = str(ST_HOME)
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["TOKENIZERS_PARALLELISM"] = "false"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def warmup(*, wait: bool = False) -> dict:
    """在后台把向量进程拉起来并等它就绪。重复调用是幂等的。

    ``wait=True`` 用于自检脚本（它需要确定性地知道成败）；
    站点启动时用 ``wait=False``，**绝不阻塞启动**。
    """
    if not available():
        with _lock:
            _state.error = f"缺少向量依赖：{EMBED_LIBS}"
        return status()

    with _lock:
        if _state.ready:
            return status()
        if _state.warming:
            already = True
        else:
            already = False
            _state.warming = True
            _state.error = ""

    if already:
        if wait:
            deadline = time.time() + CALL_TIMEOUT + 60
            while time.time() < deadline and not ready():
                time.sleep(0.4)
        return status()

    def runner() -> None:
        try:
            proc = subprocess.Popen(
                [sys.executable, str(SERVER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=_child_env(), text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
        except Exception as exc:  # noqa: BLE001
            with _lock:
                _state.warming = False
                _state.error = f"启动向量进程失败：{type(exc).__name__}: {exc}"
            return

        # stderr 必须持续读掉：torch/transformers 很吵，管道写满会把子进程卡死。
        def drain() -> None:
            assert proc.stderr is not None
            tail: list[str] = []
            for line in proc.stderr:
                tail.append(line)
                if len(tail) > 200:
                    del tail[:100]

        threading.Thread(target=drain, daemon=True).start()

        with _lock:
            _state.proc = proc
            _state.started_at = time.time()

        # 等第一行握手。这一行要等模型加载完，所以这里是整个流程最慢的地方。
        assert proc.stdout is not None
        line = proc.stdout.readline()
        try:
            hello = json.loads(line) if line.strip() else {}
        except json.JSONDecodeError:
            hello = {}

        with _lock:
            _state.warming = False
            if hello.get("ok") and hello.get("ready"):
                _state.ready = True
                _state.dim = int(hello.get("dim") or 0)
                _state.model = str(hello.get("model") or DEFAULT_MODEL)
                _state.ready_at = time.time()
                _state.error = ""
            else:
                _state.error = str(hello.get("error") or "向量进程未返回就绪信号")
                _reset_locked(_state.error)

    thread = threading.Thread(target=runner, daemon=True, name="embed-warmup")
    thread.start()

    if wait:
        deadline = time.time() + CALL_TIMEOUT + 60
        while time.time() < deadline:
            if ready():
                break
            time.sleep(0.4)
    return status()


def embed(texts: list[str], *, wait: bool = False) -> list[list[float]] | None:
    """把若干段文本编码成归一化向量。**不可用时返回 None，绝不抛异常。**

    返回 None 的语义是"这次没有向量可用"——调用方据此退回关键词检索，
    而不是把检索整个失败掉。这是本模块最重要的接口约定。
    """
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return []

    if not ready():
        if not wait:
            return None
        warmup(wait=True)
        if not ready():
            return None

    with _lock:
        proc = _state.proc
        if proc is None or proc.poll() is not None:
            _reset_locked("向量进程不可用")
            return None
        assert proc.stdin is not None and proc.stdout is not None
        try:
            started = time.time()
            proc.stdin.write(json.dumps({"texts": texts}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            elapsed = (time.time() - started) * 1000
        except Exception as exc:  # noqa: BLE001 - 管道断了就降级
            _reset_locked(f"写入向量进程失败：{type(exc).__name__}: {exc}")
            return None

        _state.calls += 1
        _state.last_call_ms = elapsed

    try:
        payload = json.loads(line) if line.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not payload.get("ok"):
        with _lock:
            _state.error = str(payload.get("error") or "向量进程返回失败")
        return None
    vectors = payload.get("vectors")
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        with _lock:
            _state.error = "向量数量与文本数量不一致"
        return None
    return vectors


def embed_in_batches(texts: list[str], *, wait: bool = True) -> list[list[float]] | None:
    """分批编码。入库一篇长论文时一次发 110 段会撑大单次请求，按 MAX_BATCH 切开。

    任何一批失败就整体返回 None——**不做"部分成功"**：留下来一半向量的索引
    会让检索结果出现无法解释的空洞，比干脆没有向量更难查。
    """
    texts = [str(t or "") for t in (texts or [])]
    if not texts:
        return []
    out: list[list[float]] = []
    for i in range(0, len(texts), MAX_BATCH):
        part = embed(texts[i:i + MAX_BATCH], wait=wait)
        if part is None:
            return None
        out.extend(part)
    return out


def shutdown() -> None:
    """让子进程体面退出（测试与关停时用）。"""
    with _lock:
        proc = _state.proc
        _state.proc = None
        _state.ready = False
        _state.warming = False
    if proc is None:
        return
    try:
        if proc.poll() is None and proc.stdin is not None:
            proc.stdin.write(json.dumps({"exit": True}) + "\n")
            proc.stdin.flush()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
