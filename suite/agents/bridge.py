"""让 agent 复用既有路由 handler 的适配器。

为什么这样做
------------
四个 agent 要做的事，现有路由**已经实现并验证过**（文件导入、证据抽取、逐章写作、
53 个检查器……）。如果 agent 再实现一遍，就是两套逻辑，迟早走样——
而"两套实现不一致"正是本会话反复踩到的静默失效来源。

FastAPI 的 handler 签名是 ``(request: Request)``，而 ``Request`` 可以用**原始 ASGI
scope** 廉价构造出来。所以 agent 侧只需要把"参数 + 访客凭据"打包成一个假 Request，
就能直接调用既有 handler，拿到它返回的模型。

这是**有意的取舍**：多一层适配，换来"业务逻辑只有一份"。
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request

#: 访客凭据的请求头名（与 suite/credentials.py 保持一致）。
HEADER_KEY = "x-llm-key"
HEADER_BASE = "x-llm-base"
HEADER_MODEL = "x-llm-model"


def make_request(body: dict | None = None, *, creds: Any = None,
                 query: dict | None = None) -> Request:
    """构造一个最小可用的 ``Request``，供既有 handler 调用。

    ``creds`` 传 ``suite.credentials.Credentials`` 时，会把三个头填好——
    agent 的调用方（通用接口）拿到的就是凭据对象，不该再让它去拼请求头。
    """
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if creds is not None:
        if getattr(creds, "api_key", ""):
            headers.append((HEADER_KEY.encode(), str(creds.api_key).encode()))
        if getattr(creds, "base_url", ""):
            headers.append((HEADER_BASE.encode(), str(creds.base_url).encode()))
        if getattr(creds, "model", ""):
            headers.append((HEADER_MODEL.encode(), str(creds.model).encode()))

    raw = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": raw, "more_body": False}

    qs = "&".join(f"{k}={v}" for k, v in (query or {}).items()).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "raw_path": b"/",
        "query_string": qs,
        "headers": headers,
        "scheme": "http",
        "server": ("127.0.0.1", 80),
        "client": ("127.0.0.1", 1),
        "http_version": "1.1",
    }
    return Request(scope, receive)


def make_file_request(files: list[tuple[str, str, bytes, str]], *, creds: Any = None,
                      query: dict | None = None) -> Request:
    """构造一个带 multipart 文件上传的 ``Request``。

    用于让 agent 复用既有的 ``/api/gather/import`` handler。
    body 手工拼 multipart：不引第三方库，格式本身很简单。
    """
    import uuid

    boundary = "----suite" + uuid.uuid4().hex
    out = bytearray()
    for field, filename, data, ctype in files:
        out += f"--{boundary}\r\n".encode()
        out += (f'Content-Disposition: form-data; name="{field}"; '
                f'filename="{filename}"\r\n').encode("utf-8")
        out += f"Content-Type: {ctype}\r\n\r\n".encode()
        out += data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()

    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", f"multipart/form-data; boundary={boundary}".encode()),
        (b"content-length", str(len(out)).encode()),
    ]
    if creds is not None and getattr(creds, "api_key", ""):
        headers.append((HEADER_KEY.encode(), str(creds.api_key).encode()))

    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": bytes(out), "more_body": False}

    qs = "&".join(f"{k}={v}" for k, v in (query or {}).items()).encode()
    scope = {
        "type": "http", "method": "POST", "path": "/", "raw_path": b"/",
        "query_string": qs, "headers": headers, "scheme": "http",
        "server": ("127.0.0.1", 80), "client": ("127.0.0.1", 1), "http_version": "1.1",
    }
    return Request(scope, receive)
