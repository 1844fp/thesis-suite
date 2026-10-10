"""会话状态：纯内存，进程内共享，带 TTL 淘汰。

为什么是内存而不是磁盘
----------------------
公网部署（Hugging Face Spaces 免费档）的文件系统是**临时的、且多副本不共享**，
把访客的证据库和稿件写到磁盘上，重启即丢、换副本读不到——那比不存更糟。
所以本站的状态只活在内存里：

* 证据库、稿件、历史版本都挂在会话对象上；
* 会话有过期时间，后台线程定期清理，避免内存无限增长；
* 进程重启后会话全部失效，前端会收到 ``session_expired`` 并提示重来。

这是**有意的取舍**：本站定位是「一次性把一份稿子走完整条链路」，
不是长期的论文管理平台。要长期保存请由访客自己下载产物。
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

#: 会话闲置多久后回收（秒）。写作链路一次可能跑十几分钟，给足余量。
DEFAULT_TTL = 6 * 60 * 60

#: 单个会话最多保留多少条证据、多少个稿件版本，防止内存被单个访客吃满。
MAX_EVIDENCE = 400
MAX_VERSIONS = 20


@dataclass
class Session:
    sid: str
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)
    #: 证据库：资料获取助手的产出，写作助手据此引用。
    evidence: list[dict] = field(default_factory=list)
    #: 稿件版本，最新的在最后。每版是 {title, sections:[{heading, body}], note}
    versions: list[dict] = field(default_factory=list)
    #: 评估历史，最新的在最后。
    reviews: list[dict] = field(default_factory=list)
    #: 落盘目录（供导出 docx 用），懒创建。
    workdir: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def draft(self) -> dict | None:
        return self.versions[-1] if self.versions else None

    def touch(self) -> None:
        self.touched_at = time.time()

    def add_evidence(self, items: list[dict]) -> int:
        """追加证据并去重（按 url / doi / title 三个键）。返回新增条数。"""
        seen = {_evidence_key(item) for item in self.evidence}
        added = 0
        for item in items:
            key = _evidence_key(item)
            if key in seen:
                continue
            seen.add(key)
            self.evidence.append(item)
            added += 1
            if len(self.evidence) >= MAX_EVIDENCE:
                break
        return added

    def push_version(self, version: dict) -> int:
        self.versions.append(version)
        if len(self.versions) > MAX_VERSIONS:
            del self.versions[0 : len(self.versions) - MAX_VERSIONS]
        return len(self.versions)

    def summary(self) -> dict:
        draft = self.draft
        return {
            "sid": self.sid,
            "evidence_count": len(self.evidence),
            "version_count": len(self.versions),
            "review_count": len(self.reviews),
            "has_draft": draft is not None,
            "draft_title": (draft or {}).get("title", ""),
            "word_count": _word_count(draft),
            "age_seconds": int(time.time() - self.created_at),
        }


def _evidence_key(item: dict) -> str:
    for key in ("doi", "url", "title"):
        value = str(item.get(key) or "").strip().lower()
        if value:
            return f"{key}:{value}"
    return "raw:" + str(item)[:200]


def _word_count(draft: dict | None) -> int:
    if not draft:
        return 0
    total = 0
    for section in draft.get("sections") or []:
        total += len(str(section.get("body") or ""))
    return total


class SessionStore:
    def __init__(self, ttl: int = DEFAULT_TTL) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, Session] = {}
        self._ttl = ttl

    def create(self) -> Session:
        sid = secrets.token_urlsafe(12)
        session = Session(sid=sid)
        with self._lock:
            self._items[sid] = session
            self._sweep_locked()
        return session

    def get(self, sid: str | None, *, create: bool = False) -> Session | None:
        if not sid:
            return self.create() if create else None
        with self._lock:
            session = self._items.get(sid)
            if session is None:
                if not create:
                    return None
                session = Session(sid=sid)
                self._items[sid] = session
            session.touch()
            return session

    def drop(self, sid: str) -> bool:
        with self._lock:
            return self._items.pop(sid, None) is not None

    def count(self) -> int:
        with self._lock:
            return len(self._items)

    def _sweep_locked(self) -> None:
        now = time.time()
        stale = [sid for sid, item in self._items.items() if now - item.touched_at > self._ttl]
        for sid in stale:
            self._items.pop(sid, None)


#: 进程级唯一实例。多 worker 部署时会各自持有一份——
#: 因此前端必须容忍「会话丢失」，见 api 层的 session_expired 处理。
STORE = SessionStore()
