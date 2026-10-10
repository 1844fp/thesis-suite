"""OpenAI 兼容的对话客户端。

和 thesis-assistant 的 `llm.py` 是同一个协议，但这里多一个**严格 JSON 模式**：
审稿、核验、结构化抽取这些步骤必须返回可解析的 JSON，而真实模型会把 JSON 包进
``` 围栏、在前面加一段解释、或漏掉一个括号。`chat_json` 把这三类都处理掉，
解析失败时抛 `JSONParseError` 并把原始文本带出来——不然调用方只能靠猜。
"""

from __future__ import annotations

import json
import re
from typing import Iterator, Sequence

import httpx

from .config import LLMConfig


class LLMError(RuntimeError):
    pass


class JSONParseError(LLMError):
    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


_FENCE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.S)


def extract_json(text: str) -> object:
    """Pull one JSON value out of a model reply.

    Handles, in order: a fenced block, a bare value, and the outermost
    ``{...}`` / ``[...]`` span inside surrounding prose. Fails loudly.
    """
    if text is None:
        raise JSONParseError("模型返回为空", "")
    raw = text.strip()
    if not raw:
        raise JSONParseError("模型返回为空", text)

    candidates: list[str] = []
    m = _FENCE.match(raw)
    if m:
        candidates.append(m.group(1))
    candidates.append(raw)

    # Outermost brace span, scanned with string awareness so braces inside
    # string literals do not confuse the depth counter.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = raw.find(opener)
        if start < 0:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(raw)):
            ch = raw[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
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
                    candidates.append(raw[start : i + 1])
                    break

    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue

    raise JSONParseError(f"无法从模型输出中解析出 JSON（{len(raw)} 字符）", raw)


class LLMClient:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    @property
    def configured(self) -> bool:
        return self.cfg.configured

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }

    def _url(self) -> str:
        return self.cfg.base_url.rstrip("/") + "/chat/completions"

    def _check_ready(self) -> None:
        if not self.cfg.configured:
            raise LLMError(
                "未配置 LLM API Key。在界面「模型设置」里填，或写入项目根目录 .env 的 LLM_API_KEY。"
            )

    def _payload(
        self,
        messages: Sequence[dict],
        *,
        stream: bool,
        temperature: float | None,
        max_tokens: int | None,
        json_mode: bool,
    ) -> dict:
        payload = {
            "model": self.cfg.model,
            "messages": list(messages),
            "temperature": self.cfg.temperature if temperature is None else temperature,
            "max_tokens": self.cfg.max_tokens if max_tokens is None else max_tokens,
            "stream": stream,
        }
        if json_mode:
            # Supported by DeepSeek / OpenAI / 智谱 / 通义 compatible modes.
            # A provider that rejects the field is retried once without it.
            payload["response_format"] = {"type": "json_object"}
        return payload

    def chat(
        self,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
        json_mode: bool = False,
    ) -> str:
        self._check_ready()
        last: Exception | None = None
        allow_json_mode = json_mode

        for attempt in range(retries + 1):
            try:
                with httpx.Client(timeout=self.cfg.timeout) as client:
                    resp = client.post(
                        self._url(),
                        headers=self._headers(),
                        json=self._payload(
                            messages,
                            stream=False,
                            temperature=temperature,
                            max_tokens=max_tokens,
                            json_mode=allow_json_mode,
                        ),
                    )
                    if resp.status_code >= 400:
                        body = resp.text[:400]
                        # Some compatible endpoints do not implement
                        # response_format. Drop it once instead of failing.
                        if allow_json_mode and resp.status_code in (400, 404, 422):
                            allow_json_mode = False
                            continue
                        raise LLMError(f"HTTP {resp.status_code}: {body}")
                    data = resp.json()
                    return data["choices"][0]["message"]["content"] or ""
            except Exception as exc:  # noqa: BLE001 - retry any transport error
                last = exc
                if attempt >= retries:
                    break
        if isinstance(last, LLMError):
            raise last
        raise LLMError(f"LLM 调用失败: {type(last).__name__}: {last}")

    def chat_json(
        self,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        retries: int = 2,
    ) -> object:
        """Ask for JSON, and repair the reply before giving up.

        One extra model round is cheaper than failing a 10-minute pipeline,
        and the repair prompt is told exactly what was wrong.
        """
        text = self.chat(
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            retries=retries,
            json_mode=True,
        )
        try:
            return extract_json(text)
        except JSONParseError as first:
            repair = list(messages) + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "上面的回复不是合法 JSON。请只输出合法 JSON，不要解释、"
                        "不要 Markdown 围栏、不要省略号。保持字段名和结构不变。"
                    ),
                },
            ]
            text2 = self.chat(repair, temperature=0.0, max_tokens=max_tokens, json_mode=True)
            try:
                return extract_json(text2)
            except JSONParseError:
                raise first

    def chat_stream(
        self,
        messages: Sequence[dict],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Iterator[str]:
        self._check_ready()
        with httpx.Client(timeout=self.cfg.timeout) as client:
            with client.stream(
                "POST",
                self._url(),
                headers=self._headers(),
                json=self._payload(
                    messages,
                    stream=True,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    json_mode=False,
                ),
            ) as resp:
                if resp.status_code >= 400:
                    body = resp.read().decode("utf-8", "replace")
                    raise LLMError(f"HTTP {resp.status_code}: {body[:400]}")
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
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        yield piece
