"""② 资料获取 agent。

能力：导入文件、抽取证据、核验文献真伪、导出参考文献表。
"""

from __future__ import annotations

import json

from .base import Agent, Context
from .bridge import make_file_request, make_request

#: 扩展名 -> MIME，供 bridge 拼 multipart。
_MIME = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".txt": "text/plain", ".text": "text/plain",
    ".html": "text/html", ".htm": "text/html",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".json": "application/json", ".bib": "text/plain",
}


def _mime(name: str) -> str:
    low = name.lower()
    for ext, mime in _MIME.items():
        if low.endswith(ext):
            return mime
    return "application/octet-stream"


class GatherAgent(Agent):
    name = "gather"
    title = "资料获取"
    summary = "导入 PDF/Word/Markdown 等文件、抽成可引用证据、核验文献真伪、导出参考文献表。"

    def __init__(self) -> None:
        self.actions = {
            "import": {"handler": self.import_files, "needs_key": False,
                       "desc": "解析上传的文档（PDF/Word/Markdown/文本/HTML/CSV），不需要 Key"},
            "extract": {"handler": self.extract, "needs_key": True,
                        "desc": "把资料抽成结构化证据，片段逐字校验"},
            "verify": {"handler": self.verify, "needs_key": False,
                       "desc": "拿证据去 Crossref 核验真伪，不需要 Key"},
            "export": {"handler": self.export, "needs_key": False,
                       "desc": "导出参考文献表（gbt / bibtex / markdown）"},
        }

    async def import_files(self, ctx: Context, params: dict, creds) -> dict:
        """导入文件。``params['files']`` 是 ``[(文件名, 字节)]``。"""
        from ..api import gather as gather_api

        raw = params.get("files") or []
        files: list[tuple[str, str, bytes, str]] = []
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                name, data = str(item[0]), item[1]
            elif isinstance(item, dict):
                name, data = str(item.get("name") or "未命名"), item.get("data") or b""
            else:
                continue
            if isinstance(data, str):
                data = data.encode("utf-8")
            files.append(("files", name, bytes(data), _mime(name)))

        if not files:
            return {"ok": False, "error": "没有收到文件。"}

        ev = ctx.begin(self.name, "import", f"解析 {len(files)} 个文件")
        try:
            req = make_file_request(files, creds=creds)
            data = await gather_api.import_files(req)
            # 解析出的 sources 放进黑板：下一步"抽取"要用的就是它。
            ctx.put("imported_sources", data.get("sources") or [])
            ctx.finish(ev, f"解析 {data.get('parsed')}/{data.get('received')} 个文件",
                       parsed=data.get("parsed"), failed=data.get("failed"),
                       source_count=data.get("source_count"))
            return {"ok": True, **data}
        except Exception as exc:  # noqa: BLE001
            # FastAPI 的 HTTPException 承载了可读的错误说明，要把它取出来
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def extract(self, ctx: Context, params: dict, creds) -> dict:
        """抽取证据。``params['sources']`` 不给时用上次导入的结果。"""
        from ..api import gather as gather_api

        sources = params.get("sources") or ctx.get("imported_sources") or []
        if not sources:
            return {"ok": False, "error": "没有可抽取的资料：请先导入文件或粘贴文本。"}
        if not getattr(creds, "configured", False):
            return {"ok": False, "error": "抽取证据需要你自己的 API Key。"}

        ev = ctx.begin(self.name, "extract", f"抽取 {len(sources)} 条资料")
        try:
            req = make_request({"sid": ctx.session.sid, "sources": sources}, creds=creds)
            data = await gather_api.extract(req)
            ctx.put("imported_sources", [])
            ctx.finish(ev, f"入池 {data.get('added', 0)} 条证据",
                       added=data.get("added"), total=data.get("total"),
                       dropped=data.get("dropped_passages"))
            return {"ok": True, **data}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def verify(self, ctx: Context, params: dict, creds) -> dict:
        """核验证据库里未核验过的条目。不需要 Key。"""
        from ..api import gather as gather_api

        ev = ctx.begin(self.name, "verify", "核验文献真伪")
        try:
            req = make_request({"sid": ctx.session.sid,
                                "limit": params.get("limit", 15)}, creds=creds)
            data = await gather_api.verify(req)
            tally = data.get("tally") or {}
            ctx.finish(ev, f"核验完成：{tally}", tally=tally)
            return {"ok": True, **data}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}

    async def export(self, ctx: Context, params: dict, creds) -> dict:
        """导出参考文献表。"""
        from ..api import gather as gather_api

        fmt = str(params.get("format") or "markdown")
        ev = ctx.begin(self.name, "export", f"导出 {fmt}")
        try:
            req = make_request({"sid": ctx.session.sid, "format": fmt}, creds=creds)
            resp = await gather_api.export(req)
            text = resp.body.decode("utf-8") if isinstance(resp.body, bytes) else str(resp.body)
            ctx.finish(ev, f"导出 {len(text)} 字", format=fmt)
            return {"ok": True, "format": fmt, "text": text}
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", None) or f"{type(exc).__name__}: {exc}"
            ctx.fail(ev, str(detail))
            return {"ok": False, "error": str(detail)}
