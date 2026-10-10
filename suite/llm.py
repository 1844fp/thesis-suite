"""直连模型服务的薄 HTTP 客户端（访客自带密钥）。

为什么不直接复用 ``app.llm.LLMClient``
--------------------------------------
上游那一个够用，但它把密钥藏在 ``LLMConfig`` 里、且上游还有"从邻居项目 .env
借密钥"的回退。本站在公网运行，**必须保证任何一条路径都不会用到服务器的密钥**，
所以在这里用一份最小的、显式接收 ``Credentials`` 的实现：

* 只认传进来的凭据，没有任何回退；
* 出错信息里绝不带上密钥；
* 同时服务「资料获取 / 写作 / 评估」三个助手。

上游的 ``thesisapp.llm.LLMClient`` 仍会被使用——在少数需要它原生能力的路径上
（例如 JSON 修复重试）。两条路都只接受显式传入的凭据。
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Sequence

import httpx

from .credentials import Credentials


class LLMError(RuntimeError):
    """模型调用失败，消息可直接展示给访客。"""


#: 这些错误再重试也不会成功，应当立刻停下并把话说清楚。
FATAL_MARKERS = (
    "401",
    "402",
    "403",
    "insufficient",
    "invalid_api_key",
    "invalid api key",
    "unauthorized",
    "余额",
    "欠费",
)


def is_fatal(message: str) -> bool:
    low = (message or "").lower()
    return any(marker in low for marker in FATAL_MARKERS)


def _humanize(status: int, body: str) -> str:
    hints = {
        400: "请求被拒绝（可能是该模型不支持 JSON 输出模式）",
        401: "API Key 无效或已过期",
        402: "账户余额不足",
        403: "没有该模型的访问权限",
        404: "接口地址或模型名不对",
        429: "触发限流，请稍后重试",
        500: "服务商内部错误",
        502: "网关错误",
        503: "服务暂不可用，请稍后重试",
        504: "网关超时",
    }
    hint = hints.get(status, "调用失败")
    tail = (body or "").strip()[:300]
    return f"HTTP {status}：{hint}" + (f"；服务商返回：{tail}" if tail else "")


class Chat:
    """一次会话内的模型客户端。用法::

        with Chat(creds) as chat:
            text = chat.ask([{"role": "user", "content": "..."}])
    """

    def __init__(self, creds: Credentials, *, timeout: float = 180.0) -> None:
        self.creds = creds
        self.timeout = timeout
        self._client: httpx.Client | None = None

    # -- 生命周期 ---------------------------------------------------------
    def __enter__(self) -> "Chat":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            finally:
                self._client = None

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            # 注意：不跟随重定向，避免把带密钥的请求转发到别的地址。
            self._client = httpx.Client(timeout=self.timeout, follow_redirects=False)
        return self._client

    # -- 前置检查 ---------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self.creds.configured

    def _require(self) -> None:
        if not self.ready:
            raise LLMError("这一步需要模型：请先在页面顶部填入你自己的 API Key。")

    def _url(self) -> str:
        return self.creds.base_url.rstrip("/") + "/chat/completions"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.creds.api_key}",
        }

    # -- 调用 -------------------------------------------------------------
    def ask(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.3,
        max_tokens: int = 4096,
        json_mode: bool = False,
        retries: int = 2,
    ) -> str:
        """非流式调用，返回文本。"""
        self._require()
        payload: dict[str, Any] = {
            "model": self.creds.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last: Exception | None = None
        allow_json = json_mode
        for attempt in range(retries + 1):
            try:
                resp = self.client.post(self._url(), headers=self._headers(), json=payload)
                if resp.status_code >= 400:
                    body = resp.text[:400]
                    # 部分兼容接口不认 response_format，去掉再试一次。
                    if allow_json and resp.status_code in (400, 404, 422):
                        allow_json = False
                        payload.pop("response_format", None)
                        continue
                    raise LLMError(_humanize(resp.status_code, body))
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError("服务商返回了空结果。")
                return str(choices[0].get("message", {}).get("content") or "")
            except LLMError as exc:
                last = exc
                if is_fatal(str(exc)):
                    raise
            except Exception as exc:  # noqa: BLE001 - 网络类错误可以重试
                last = LLMError(f"网络错误：{type(exc).__name__}: {exc}")
            if attempt >= retries:
                break
        raise last or LLMError("调用失败")

    def ask_json(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        retries: int = 2,
    ) -> Any:
        """要求返回 JSON，并做一次修复重试。"""
        text = self.ask(
            messages, temperature=temperature, max_tokens=max_tokens, json_mode=True, retries=retries
        )
        parsed = extract_json(text)
        if parsed is not None:
            return parsed
        repair = list(messages) + [
            {"role": "assistant", "content": text[:4000]},
            {
                "role": "user",
                "content": "上面的回复不是合法 JSON。只输出合法 JSON，不要解释、不要围栏、不要省略号。",
            },
        ]
        text2 = self.ask(repair, temperature=0.0, max_tokens=max_tokens, json_mode=True, retries=1)
        parsed2 = extract_json(text2)
        if parsed2 is None:
            raise LLMError("模型没有返回可解析的 JSON，请换一个模型或调低温度后重试。")
        return parsed2

    def stream(
        self,
        messages: Sequence[dict],
        *,
        temperature: float = 0.4,
        max_tokens: int = 4096,
    ) -> Iterator[str]:
        """流式调用，逐片产出文本。"""
        self._require()
        payload = {
            "model": self.creds.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        with self.client.stream("POST", self._url(), headers=self._headers(), json=payload) as resp:
            if resp.status_code >= 400:
                body = resp.read().decode("utf-8", "replace")
                raise LLMError(_humanize(resp.status_code, body))
            for line in resp.iter_lines():
                if not line:
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                piece = (choices[0].get("delta") or {}).get("content")
                if piece:
                    yield str(piece)


# ---------------------------------------------------------------------------
# JSON 提取（与上游 topiccore.llm.extract_json 同等强度的实现，独立一份）
# ---------------------------------------------------------------------------
def extract_json(text: str) -> Any | None:
    """尽最大努力从模型输出里取出一个 JSON 对象/数组。"""
    if not text or not text.strip():
        return None
    raw = text.strip()
    candidates = [raw]
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidates.append("\n".join(lines).strip())

    start = raw.find("{")
    if start >= 0:
        span = _balanced_span(raw, start, "{", "}")
        if span:
            candidates.append(span)

    for candidate in candidates:
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


def _balanced_span(text: str, start: int, opener: str, closer: str) -> str | None:
    depth = 0
    in_str = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    return None
