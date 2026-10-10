"""大模型客户端：标准库实现，支持流式输出、重试与容错 JSON 提取。"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Iterator

from .config import Settings


class LLMError(RuntimeError):
    """调用大模型失败。"""


RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    # -- 通用请求 ---------------------------------------------------------
    def _request(self, payload: dict, stream: bool) -> urllib.request.Request:
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "thesis-topic-helper/1.0",
        }
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        return urllib.request.Request(url, data=body, headers=headers, method="POST")

    def _payload(self, messages: list, stream: bool, **kwargs) -> dict:
        payload = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.settings.temperature),
            "stream": stream,
        }
        max_tokens = kwargs.get("max_tokens") or self.settings.max_tokens
        if max_tokens:
            # 新版 OpenAI 接口用 max_completion_tokens，多数国产接口用 max_tokens，
            # 两个都带上，服务端会忽略不认识的那个。
            payload["max_tokens"] = max_tokens
        if self.settings.json_mode and kwargs.get("json_mode", True):
            payload["response_format"] = {"type": "json_object"}
        return payload

    # -- 流式 -------------------------------------------------------------
    def stream_chat(
        self,
        messages: list,
        on_delta: Callable[[str], None] | None = None,
        retries: int = 2,
        **kwargs,
    ) -> str:
        """流式调用，返回完整文本。失败按指数退避重试。"""
        if not self.settings.ready:
            raise LLMError("尚未配置 API Key，无法调用大模型。可在右上角「设置」中填写。")

        last_error: Exception | None = None
        for attempt in range(retries + 1):
            chunks: list[str] = []
            try:
                request = self._request(self._payload(messages, True, **kwargs), stream=True)
                with urllib.request.urlopen(request, timeout=self.settings.timeout) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", errors="replace").strip()
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except ValueError:
                            continue
                        choices = event.get("choices") or []
                        if not choices:
                            continue
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content") or ""
                        if piece:
                            chunks.append(piece)
                            if on_delta:
                                on_delta(piece)
                text = "".join(chunks)
                if not text.strip():
                    raise LLMError("模型返回了空内容，请稍后重试或换一个模型。")
                return text
            except urllib.error.HTTPError as exc:  # 带状态码
                detail = _read_error_body(exc)
                last_error = LLMError(_humanize_http(exc.code, detail))
                if exc.code not in RETRY_STATUS:
                    break
            except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
                last_error = LLMError(f"网络连接失败：{exc}")
            except LLMError as exc:
                last_error = exc
                break
            except Exception as exc:  # noqa: BLE001 - 兜底，避免服务崩掉
                last_error = LLMError(f"未知错误：{exc!r}")
                break
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
        raise last_error or LLMError("调用失败")


def _read_error_body(exc: urllib.error.HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:600]
    except Exception:  # noqa: BLE001
        return ""


def _humanize_http(code: int, detail: str) -> str:
    hints = {
        400: "请求格式不被接受（可能是该模型不支持 JSON 输出，把 LLM_JSON_MODE 改成 0 再试）",
        401: "API Key 无效或已过期，请检查 LLM_API_KEY",
        402: "账户余额不足",
        403: "没有该模型的访问权限",
        404: "接口地址或模型名不对，请检查 LLM_BASE_URL / LLM_MODEL",
        413: "请求过大",
        422: "参数校验失败",
        429: "触发限流，请稍后重试",
        500: "服务端内部错误",
        502: "网关错误",
        503: "服务暂不可用",
        504: "网关超时",
    }
    hint = hints.get(code, "请求失败")
    return f"HTTP {code}：{hint}" + (f"；服务端信息：{detail}" if detail else "")


# ---------------------------------------------------------------------------
# 从自由文本里抠出 JSON
# ---------------------------------------------------------------------------
def strip_code_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def extract_json(text: str) -> dict | None:
    """尽最大努力把模型输出解析成 dict。

    依次尝试：直接解析 → 去代码围栏 → 扫描第一个平衡的 JSON 对象 →
    截断补救（补齐未闭合的括号与字符串）。
    """
    if not text or not text.strip():
        return None
    candidates = [text.strip(), strip_code_fence(text)]
    start = text.find("{")
    if start >= 0:
        balanced = _balanced_slice(text, start)
        if balanced:
            candidates.append(balanced)
            if not _is_balanced(balanced):
                candidates.extend(_truncation_candidates(balanced))
    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _is_balanced(text: str) -> bool:
    """花括号/方括号是否配平（忽略字符串内部）。"""
    in_string = False
    escaped = False
    stack: list[str] = []
    pairs = {"}": "{", "]": "["}
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in pairs:
            if not stack or stack.pop() != pairs[ch]:
                return False
    return not stack and not in_string


def _truncation_candidates(text: str) -> list[str]:
    """输出被 max_tokens 截断时的补救。

    退回到最后一个「结构完整且已闭合」的位置，再补齐缺失的收尾括号。
    这样即使 JSON 的尾巴被切掉，前面已经写完整的题目仍能救回来，
    不会因为一个截断字符让整次生成白费。
    """
    cut = -1
    rest = ""
    stack_at_cut: list[str] = []
    in_string = False
    escaped = False
    stack: list[str] = []
    pairs = {"}": "{", "]": "["}
    for index, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in pairs:
            if stack and stack[-1] == pairs[ch]:
                stack.pop()
            cut = index
            rest = text[index + 1 :]
            # 关键：记下这一刻的栈。循环还会继续扫描后面的字符，
            # 若不做快照，收尾括号会按「已被丢弃的那些对象」来补，结果反而不合法。
            stack_at_cut = list(stack)
    if cut < 0:
        return []
    prefix = text[: cut + 1].rstrip().rstrip(",")
    candidates = [prefix + _closers(stack_at_cut)]
    # 被截断的字符串里可能还夹着引号外的散字，补一个引号再试一次
    if rest.strip().strip(","):
        candidates.append(prefix + '"' + _closers(stack_at_cut))
    # 更好的一种：截断点后面若还有完整对象，直接把它们续上来，能多救回几个题目
    salvaged = _salvage_rest(prefix, rest, stack_at_cut)
    if salvaged:
        candidates.insert(0, salvaged)
    return candidates


def _salvage_rest(prefix: str, rest: str, stack_at_cut: list[str]) -> str | None:
    """把截断点之后的完整 JSON 值接回前缀，跳过写了一半的那一个。"""
    decoder = json.JSONDecoder()
    index = 0
    values: list = []
    while index < len(rest):
        while index < len(rest) and rest[index] in " \t\r\n,":
            index += 1
        if index >= len(rest):
            break
        try:
            value, end = decoder.raw_decode(rest, index)
        except ValueError:
            break
        values.append(value)
        index = end
    if not values:
        return None
    rebuilt = prefix.rstrip().rstrip(",")
    for value in values:
        if not isinstance(value, dict):
            return None
        rebuilt += "," + json.dumps(value, ensure_ascii=False)
    return rebuilt + _closers(stack_at_cut)


def _closers(stack: list[str]) -> str:
    return "".join("}" if opener == "{" else "]" for opener in reversed(stack))


def _balanced_slice(text: str, start: int) -> str | None:
    """从 start 处的 '{' 开始，扫描到配平的 '}'（忽略字符串内的括号）。"""
    depth = 0
    in_string = False
    escaped = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]
    # 没配平：返回剩余部分，交给截断补救逻辑
    return text[start:]
