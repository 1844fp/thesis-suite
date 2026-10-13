"""FastAPI 应用工厂。

启动顺序很关键：**先** ``boot.prepare()`` 把两套上游代码挂进 sys.path，
**再**导入任何上游模块。本模块顶部就完成这件事，因此别的模块可以放心
``from core import ...`` / ``from app import ...``。
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import boot  # noqa: F401 - 导入即完成 sys.path 准备
from ..boot import SUITE_ROOT

boot.prepare()

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from . import agents as agents_api  # noqa: E402
from . import gather, kb, review, topics, writing  # noqa: E402
from .. import credentials as creds_mod  # noqa: E402
from .. import ratelimit  # noqa: E402

WEB_DIR = SUITE_ROOT / "web"

STARTED_AT = time.time()

DESCRIPTION = """\
把「题目生成 → 资料获取 → 写作 → 评估」串成一条链路的论文助手站点。

**访客自带密钥**：本站不保存、不使用服务器自己的 API Key。
浏览器把 Key 放在请求头里，服务器只在本次请求内转发给模型服务商。
不填 Key 时只有题目生成助手的离线模式可用。
"""


def create_app() -> FastAPI:
    app = FastAPI(
        title="论文助手 · 四合一",
        description=DESCRIPTION,
        version="1.0.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    # 前端与 API 同源部署，本不需要 CORS；保留是为了让访客能在自己的
    # 页面/脚本里调本接口（例如把题目生成接进自己的工具链）。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Content-Disposition"],
    )

    app.include_router(topics.router)
    app.include_router(gather.router)
    app.include_router(writing.router)
    app.include_router(review.router)
    app.include_router(kb.router)
    # 通用 agent 接口放在最后：它的路径带通配段（/{name}/{action}），
    # 先注册别的路由可以避免有人用一个叫 "topics" 的 agent 名去抢真实路由。
    app.include_router(agents_api.router)

    @app.middleware("http")
    async def rate_limit_middleware(request, call_next):
        """限流。**默认关闭**——站主设 SUITE_RATE_LIMIT=60 才生效。

        放在中间件而不是每个路由里：这样"哪些接口要计数"只需维护一处
        （``ratelimit.COUNTED_PREFIXES``），加接口时不容易漏。
        静态资源与健康检查不计入，否则打开一次页面就把额度用掉。
        """
        allowed, remaining, retry = ratelimit.check(request, request.url.path)
        if not allowed:
            return JSONResponse(
                {"detail": f"请求过于频繁，请 {retry} 秒后重试。",
                 "retry_after": retry},
                status_code=429,
                headers={"Retry-After": str(retry)},
            )
        response = await call_next(request)
        if remaining >= 0:
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

    @app.get("/api/deployment", tags=["系统"])
    def deployment() -> dict:
        """部署形态：访客需不需要自己填 Key、有没有限流。

        这个接口存在的唯一理由是**把"谁在付钱"如实告诉访客**。
        站点用自己的 Key 时，访客有权知道这一点——否则他会以为是自己配置的结果，
        而在一个"反正不花我的钱"的预期下大量使用。
        """
        server = creds_mod.server_credentials()
        return {
            "ok": True,
            "server_key": {
                "enabled": creds_mod.server_key_enabled(),
                # 只给脱敏串：前端要显示"当前用的哪个 Key"，但永远不该拿到明文
                "masked": server.masked() if server.configured else "",
                "model": server.model or "",
                "base_url": server.base_url or "",
            },
            "visitor_key": {
                "allowed": True,
                "note": "访客也可以填自己的 Key，填了就用他自己的（优先于本站的）。",
            },
            "rate_limit": ratelimit.stats(),
            "billing_note": (
                "本站配置了自己的模型凭据，访客无需填写即可使用；"
                "若访客填了自己的 Key，则优先使用访客的。"
                if creds_mod.server_key_enabled() else
                "本站未配置模型凭据，需要访客填写自己的 API Key（或使用不花钱的功能）。"
            ),
        }

    @app.get("/api/health", tags=["系统"])
    def health() -> dict:
        """服务自检：确认两套上游代码都导入成功。"""
        from ..sessions import STORE

        info = boot.describe()
        return {
            "ok": True,
            "service": "论文助手 · 四合一",
            "version": "1.0.0",
            "uptime_seconds": int(time.time() - STARTED_AT),
            "sessions": STORE.count(),
            "packages": info["packages"],
            "suite_root": info["suite_root"],
            # BYOK 的结构性保证要在运行态可自证：这两项由 suite/boot.py 的封印产生，
            # 光写在注释里没法验证，所以如实透出到健康检查里。
            "key_fallback_neutralized": info.get("key_fallback_neutralized"),
            # 服务器是不是"自己也在用 Key"。这与上面那项**不是同一件事**：
            # 上面说的是"绝不去磁盘上别处借密钥"，这里说的是"站主显式配置了本站凭据"。
            # 早先这里读的是 boot.describe() 的 server_key_present，而那个字段由
            # boot 的封印逻辑产生（封印会把 LLM_API_KEY 从环境里删掉），
            # 于是在"站主配了 Key"的部署上它反而恒为 False——字段名与实际含义对不上。
            "server_key_configured": creds_mod.server_key_enabled(),
        }

    @app.get("/api/capabilities", tags=["系统"])
    def capabilities() -> dict:
        """四个助手的能力清单与各自的「需要什么」。前端据此渲染页签说明。"""
        return {
            "ok": True,
            "assistants": [
                {
                    "id": "topics",
                    "name": "题目生成助手",
                    "summary": "按学科一次生成多个可落地题目，每题附研究缺口与文献脉络简报。",
                    "steps": ["填学科方向", "生成", "导出 Markdown"],
                    "needs_key": False,
                    "offline": True,
                    "note": "不填 Key 也能用：自动切换到内置离线学科知识库。",
                },
                {
                    "id": "gather",
                    "name": "资料获取助手",
                    "summary": "贴入或上传参考文献/资料，抽取成可被引用的结构化证据，并核验真伪。",
                    "steps": ["录入资料", "抽取证据", "核验 DOI/标题"],
                    "needs_key": True,
                    "offline": False,
                    "note": "抽取需要模型；DOI 核验走 Crossref，不需要 Key。",
                },
                {
                    "id": "writing",
                    "name": "写作助手",
                    "summary": "只依据证据撰写，逐章生成、可回退版本、自动挂引用编号。",
                    "steps": ["确认证据库", "定章节结构", "逐章生成", "导出"],
                    "needs_key": True,
                    "offline": False,
                    "note": "强制「无证据不落笔」：正文里的数字与引用必须能在证据库里找到。",
                },
                {
                    "id": "review",
                    "name": "评估助手",
                    "summary": "机械检查器 + 模型审稿双轨评估，评分由检查结果推导而非模型印象。",
                    "steps": ["提交稿件", "跑检查器", "出意见书"],
                    "needs_key": True,
                    "offline": False,
                    "note": "检查器全部本地运行；只有审稿意见书需要模型。",
                },
            ],
        }

    @app.post("/api/session", tags=["系统"])
    def new_session() -> dict:
        """开一个新会话。会话只存内存，用于串起资料 → 写作 → 评估。"""
        from ..sessions import STORE

        session = STORE.create()
        return {"ok": True, "session": session.summary()}

    @app.get("/api/session/{sid}", tags=["系统"])
    def read_session(sid: str) -> JSONResponse:
        from ..sessions import STORE

        session = STORE.get(sid)
        if session is None:
            return JSONResponse({"ok": False, "error": "session_expired"}, status_code=404)
        return JSONResponse({"ok": True, "session": session.summary()})

    @app.delete("/api/session/{sid}", tags=["系统"])
    def drop_session(sid: str) -> dict:
        """主动清空会话——公网服务应当允许访客立刻抹掉自己的数据。"""
        from ..sessions import STORE

        return {"ok": True, "dropped": STORE.drop(sid)}

    # ---- 前端 -------------------------------------------------------------
    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        page = WEB_DIR / "index.html"
        if not page.is_file():
            raise RuntimeError(f"前端文件缺失：{page}")
        return FileResponse(page, media_type="text/html; charset=utf-8")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> JSONResponse:
        # 没有图标时返回 204，避免浏览器把 404 记进控制台。
        return JSONResponse({}, status_code=204)

    return app
