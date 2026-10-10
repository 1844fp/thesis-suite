"""本地 Web 服务：标准库 http.server，无第三方依赖。

为什么不让网页直接调大模型？因为浏览器里放 API Key 会明文暴露。
所以这里做一层很薄的本地代理：网页 → 本地 127.0.0.1 服务 → 大模型。

接口：
    GET  /                    网页界面
    GET  /api/health          服务与配置状态
    GET  /api/disciplines     内置学科知识库预览
    GET  /api/settings        配置状态（不含 Key 明文）
    POST /api/settings        写回 .env 并热更新
    POST /api/generate        SSE 流式生成
    POST /api/export         生成 Markdown 并落盘，返回下载内容
"""

from __future__ import annotations

import json
import mimetypes
import socket
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

from . import export as export_module
from . import offline
from .config import (ENV_PATH, ENV_TEMPLATE, EXPORT_DIR, WEB_DIR, ensure_dirs,
                     load_settings, parse_env_file)
from .generate import GenerationError, generate_stream

MAX_BODY = 256 * 1024
LOCK = threading.Lock()


def _json_bytes(payload) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "thesis-topic-helper/1.0"
    protocol_version = "HTTP/1.1"

    # ---------------- 基础工具 ----------------
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        sys.stderr.write(f"[{self.log_date_time_string()}] {fmt % args}\n")

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload, status: int = 200, extra: dict | None = None) -> None:
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8", extra)

    def _send_error_json(self, message: str, status: int = 400) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"请求体不是合法 JSON：{exc}") from exc
        return data if isinstance(data, dict) else {}

    # ---------------- 路由 ----------------
    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path in ("/", "/index.html"):
                self._serve_file(WEB_DIR / "index.html")
            elif path == "/api/health":
                settings = load_settings()
                self._send_json({
                    "ok": True,
                    "service": "论文选题助手",
                    "version": "1.0.0",
                    "ready": settings.ready,
                    "config": settings.describe(),
                    "exports_dir": str(EXPORT_DIR),
                })
            elif path == "/api/disciplines":
                self._send_json({"ok": True, "disciplines": offline.list_disciplines()})
            elif path == "/api/settings":
                settings = load_settings()
                self._send_json({
                    "ok": True,
                    "config": settings.describe(),
                    "env_path": str(ENV_PATH),
                    "env_exists": ENV_PATH.exists(),
                })
            elif path.startswith("/static/"):
                self._serve_file((WEB_DIR / path[len("/static/"):]).resolve())
            else:
                self._send_error_json("未知路径：" + path, 404)
        except BrokenPipeError:
            pass
        except Exception as exc:  # noqa: BLE001
            self._send_error_json(f"服务异常：{exc}", 500)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/generate":
                self._handle_generate()
            elif path == "/api/export":
                self._handle_export()
            elif path == "/api/settings":
                self._handle_settings()
            else:
                self._send_error_json("未知路径：" + path, 404)
        except BrokenPipeError:
            pass
        except ValueError as exc:
            self._send_error_json(str(exc), 400)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._send_error_json(f"服务异常：{exc}", 500)

    # ---------------- 静态文件 ----------------
    def _serve_file(self, path: Path) -> None:
        web_root = WEB_DIR.resolve()
        try:
            resolved = path.resolve()
        except OSError:
            self._send_error_json("路径非法", 400)
            return
        if web_root not in resolved.parents and resolved != web_root:
            self._send_error_json("拒绝访问工作目录之外的文件", 403)
            return
        if not resolved.is_file():
            self._send_error_json("文件不存在：" + resolved.name, 404)
            return
        ctype = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, resolved.read_bytes(), ctype)

    # ---------------- SSE 生成 ----------------
    def _sse(self, payload: dict) -> bool:
        data = json.dumps(payload, ensure_ascii=False)
        try:
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _handle_generate(self) -> None:
        params = self._read_body()
        settings = load_settings()
        mode = str(params.pop("mode", "auto") or "auto")
        params.pop("stream", None)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # SSE 没有 Content-Length：必须靠关闭连接告诉客户端"流结束了"，
        # 否则 HTTP/1.1 的 keep-alive 会让浏览器一直等下去。
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        self._sse({"type": "stage", "stage": "start", "message": "已连接，正在准备…", "progress": 0.02})
        try:
            for event in generate_stream(settings, mode=mode, **params):
                if not self._sse(event):
                    return
        except GenerationError as exc:
            self._sse({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._sse({"type": "error", "message": f"生成失败：{exc}"})
        finally:
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    # ---------------- 导出 ----------------
    def _handle_export(self) -> None:
        payload = self._read_body()
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("topics"):
            raise ValueError("没有可导出的内容")
        markdown = export_module.render_markdown(result)
        ensure_dirs()
        with LOCK:
            path = export_module.save_markdown(result, EXPORT_DIR)
        self._send_json({
            "ok": True,
            "path": str(path),
            "filename": path.name,
            "markdown": markdown,
        })

    # ---------------- 设置写回 ----------------
    def _handle_settings(self) -> None:
        payload = self._read_body()
        allowed = {"LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TEMPERATURE",
                   "LLM_MAX_TOKENS", "LLM_TIMEOUT", "LLM_JSON_MODE", "LLM_SYSTEM_PROMPT"}
        updates = {key: str(value) for key, value in payload.items() if key in allowed}
        if not updates:
            raise ValueError("没有可写入的配置项")
        current = parse_env_file(ENV_PATH) if ENV_PATH.exists() else {}
        current.update(updates)
        order = ["LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "LLM_TEMPERATURE",
                 "LLM_MAX_TOKENS", "LLM_TIMEOUT", "LLM_JSON_MODE", "LLM_SYSTEM_PROMPT"]
        lines = [ENV_TEMPLATE.rstrip(), "", "# === 以下为界面保存的配置 ==="]
        for key in order:
            if key in current and current[key] != "":
                lines.append(f"{key}={current[key]}")
        for key, value in current.items():
            if key not in order and value:
                lines.append(f"{key}={value}")
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        settings = load_settings()
        self._send_json({"ok": True, "message": "已保存到 .env", "config": settings.describe()})


def _pick_port(host: str, preferred: int, tries: int = 20) -> int:
    for offset in range(tries):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
                return port
            except OSError:
                continue
    raise SystemExit(f"{preferred}~{preferred + tries - 1} 端口都被占用，请用 --port 指定其他端口。")


def serve(host: str = "127.0.0.1", port: int = 8848, open_browser: bool = True) -> int:
    ensure_dirs()
    if not (WEB_DIR / "index.html").exists():
        print(f"找不到网页文件：{WEB_DIR / 'index.html'}", file=sys.stderr)
        return 2
    actual = _pick_port(host, port)
    httpd = ThreadingHTTPServer((host, actual), Handler)
    httpd.daemon_threads = True
    url = f"http://{host}:{actual}/"
    settings = load_settings()
    print("=" * 62)
    print("  论文选题助手 · 已启动")
    print("=" * 62)
    print(f"  界面地址 : {url}")
    print(f"  当前模型 : {settings.model}  ({settings.provider or settings.base_url})")
    print(f"  API Key  : {'已配置 ' + settings.key_masked if settings.api_key else '未配置（将使用离线模式）'}")
    print(f"  导出目录 : {EXPORT_DIR}")
    print("  按 Ctrl+C 停止服务")
    print("=" * 62, flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


def iter_generate(params: dict, mode: str = "auto") -> Iterator[dict]:
    """给 CLI 复用的生成入口。"""
    return generate_stream(load_settings(), mode=mode, **params)
