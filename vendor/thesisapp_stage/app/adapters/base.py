"""HTTP 基类：统一的超时、错误与健康检查。

``PeerHTTP`` 只做一件事：把"对端服务"这件容易含糊的事情说清楚。连接失败、
服务返回 4xx、返回了 HTML 而不是 JSON——三种情况给出三种不同的错误消息，
而不是笼统的"请求失败"。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx


class PeerError(RuntimeError):
    """对端服务调用失败。消息面向使用者，必须能直接读懂。"""

    def __init__(self, message: str, *, kind: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass
class PeerHealth:
    name: str
    base_url: str
    reachable: bool
    detail: str = ""
    seconds: float = 0.0
    version: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "reachable": self.reachable,
            "detail": self.detail,
            "seconds": round(self.seconds, 3),
            "version": self.version,
        }


class PeerHTTP:
    """Small wrapper over httpx with readable failures."""

    #: 服务的中文名，用于错误消息
    label = "对端服务"
    #: 启动那个服务的提示
    start_hint = ""

    def __init__(self, base_url: str, *, timeout: float = 600.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- 基础请求 ---------------------------------------------------------
    def _request(self, method: str, path: str, *, json_body: dict | None = None) -> Any:
        url = self.base_url + path
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(
                    method,
                    url,
                    json=json_body,
                    headers={"Content-Type": "application/json"},
                )
        except httpx.ConnectError as exc:
            raise PeerError(
                f"无法连接{self.label} {self.base_url}（{exc}）。{self.start_hint}",
                kind="unreachable",
            ) from exc
        except httpx.TimeoutException as exc:
            raise PeerError(
                f"{self.label} 请求超时（{self.timeout:.0f}s）：{path}", kind="timeout"
            ) from exc
        except httpx.HTTPError as exc:
            raise PeerError(f"{self.label} 请求失败：{type(exc).__name__}: {exc}") from exc

        if resp.status_code >= 400:
            detail = resp.text[:400]
            raise PeerError(
                f"{self.label} 返回 HTTP {resp.status_code}：{detail}",
                kind="http",
                status=resp.status_code,
            )

        body = resp.text
        if not body.strip():
            return {}
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise PeerError(
                f"{self.label} 返回的不是 JSON（可能是别的程序占用了这个端口）："
                f"{body[:200]!r}",
                kind="protocol",
            ) from exc

    def get(self, path: str) -> Any:
        return self._request("GET", path)

    def post(self, path: str, body: dict | None = None) -> Any:
        return self._request("POST", path, json_body=body or {})

    def delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def stream_lines(self, path: str, body: dict):
        """Yield raw SSE ``data:`` JSON objects.

        Decoded with an incremental decoder: one Chinese character's UTF-8 bytes
        can straddle two network chunks, and a per-chunk ``decode()`` turns it
        into a replacement character.
        """
        import codecs

        url = self.base_url + path
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream(
                    "POST", url, json=body, headers={"Content-Type": "application/json"}
                ) as resp:
                    if resp.status_code >= 400:
                        text = resp.read().decode("utf-8", "replace")
                        raise PeerError(
                            f"{self.label} 返回 HTTP {resp.status_code}：{text[:300]}",
                            kind="http",
                            status=resp.status_code,
                        )
                    buf = ""
                    for chunk in resp.iter_bytes():
                        buf += decoder.decode(chunk)
                        while "\n\n" in buf:
                            part, buf = buf.split("\n\n", 1)
                            for line in part.split("\n"):
                                if not line.startswith("data:"):
                                    continue
                                payload = line[5:].strip()
                                if not payload:
                                    continue
                                try:
                                    yield json.loads(payload)
                                except json.JSONDecodeError:
                                    continue
        except httpx.ConnectError as exc:
            raise PeerError(
                f"无法连接{self.label} {self.base_url}（{exc}）。{self.start_hint}",
                kind="unreachable",
            ) from exc
        except httpx.TimeoutException as exc:
            raise PeerError(f"{self.label} 流式请求超时：{path}", kind="timeout") from exc

    # -- 健康检查 ---------------------------------------------------------
    def health(self, path: str = "/api/health") -> PeerHealth:
        t0 = time.time()
        try:
            data = self.get(path)
            return PeerHealth(
                name=self.label,
                base_url=self.base_url,
                reachable=True,
                detail="ok",
                seconds=time.time() - t0,
                version=str((data or {}).get("version") or ""),
            )
        except PeerError as exc:
            return PeerHealth(
                name=self.label,
                base_url=self.base_url,
                reachable=False,
                detail=str(exc),
                seconds=time.time() - t0,
            )
