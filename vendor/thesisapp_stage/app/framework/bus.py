"""消息总线与消息日志。

智能体之间不直接调用，而是往总线上发消息。这样做的实际收益有三个，都不是
为了"架构好看"：

1. **可回放**：整条流程的消息按时间落盘，出问题时能回答"第几轮、哪个 agent
   基于什么输入做出的这个判断"。自我更正循环尤其需要这个。
2. **可加观察者**：Web UI 的实时进度、耗时统计、成本统计都只是订阅者，
   不需要往业务代码里塞回调。
3. **可裁剪**：同一批智能体既能串成完整流程，也能单独跑一个（只订阅它的输入主题）。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

TOPICS = (
    "plan",       # 计划：状态机决定下一步
    "request",    # 请求：一个 agent 要另一个 agent 干活
    "proposal",   # 产出：agent 交出的物件
    "review",     # 验证结果：Issue 清单
    "fix",        # 修复：Patch 应用结果
    "human",      # 需要人介入
    "log",        # 一般日志
    "error",
)


def _topic_matches(pattern: str, topic: str) -> bool:
    if pattern in ("*", topic):
        return True
    if pattern.endswith(".*"):
        return topic.startswith(pattern[:-1])
    return False


@dataclass
class Message:
    id: str
    topic: str
    sender: str
    recipient: str = "*"
    payload: dict[str, Any] = field(default_factory=dict)
    stage: str = ""
    round: int = 0
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return asdict(self)


class MessageLog:
    """Append-only JSONL log. One project, one log file."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, msg: Message) -> None:
        line = json.dumps(msg.as_dict(), ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def read(self, *, topic: str | None = None, limit: int = 0) -> list[dict]:
        if not self.path.exists():
            return []
        out: list[dict] = []
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if topic and not _topic_matches(topic, obj.get("topic", "")):
                    continue
                out.append(obj)
        if limit and len(out) > limit:
            return out[-limit:]
        return out

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with self.path.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for line in fh if line.strip())


class MessageBus:
    """Synchronous pub/sub with an optional persisted log.

    Synchronous on purpose: an async bus would let two agents touch the same
    artifact concurrently, and the whole point of the blackboard is that exactly
    one writer owns a version at a time.
    """

    def __init__(self, log: MessageLog | None = None) -> None:
        self._subs: list[tuple[str, Callable[[Message], None]]] = []
        self._log = log
        self._stats: dict[str, int] = {}

    def subscribe(self, pattern: str, handler: Callable[[Message], None]) -> None:
        self._subs.append((pattern, handler))

    def unsubscribe(self, pattern: str, handler: Callable[[Message], None]) -> None:
        self._subs = [s for s in self._subs if s != (pattern, handler)]

    def publish(
        self,
        topic: str,
        sender: str,
        payload: dict[str, Any] | None = None,
        *,
        recipient: str = "*",
        stage: str = "",
        round: int = 0,
    ) -> Message:
        msg = Message(
            id=f"msg-{uuid.uuid4().hex[:10]}",
            topic=topic,
            sender=sender,
            recipient=recipient,
            payload=payload or {},
            stage=str(stage),
            round=round,
        )
        self._stats[topic] = self._stats.get(topic, 0) + 1
        if self._log is not None:
            self._log.append(msg)
        for pattern, handler in list(self._subs):
            if not _topic_matches(pattern, topic):
                continue
            if msg.recipient != "*" and pattern not in ("*",) and msg.recipient not in pattern:
                # Recipient filtering is a convenience; wildcard subscribers
                # (the UI, the log) still see everything.
                continue
            try:
                handler(msg)
            except Exception as exc:  # noqa: BLE001 - one bad observer must not stop the run
                if self._log is not None:
                    self._log.append(
                        Message(
                            id=f"msg-{uuid.uuid4().hex[:10]}",
                            topic="error",
                            sender="bus",
                            payload={"observer_error": f"{type(exc).__name__}: {exc}"},
                        )
                    )
        return msg

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def emit_stats(self) -> str:
        return " ".join(f"{k}={v}" for k, v in sorted(self._stats.items()))


def collect(messages: Iterable[Message], topic: str) -> list[Message]:
    return [m for m in messages if _topic_matches(topic, m.topic)]
