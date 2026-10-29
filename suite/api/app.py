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

from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse, Response  # noqa: E402

from . import agents as agents_api  # noqa: E402
from . import gather, kb, paperqa as paperqa_api, proposal as proposal_api, references as refs_api, review, topics, writing  # noqa: E402
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
    # 参考文献推荐 / PaperQA2 都是「开题报告」阶段的能力，路由各自独立
    # （/api/refs/* 与 /api/pqa/*），而不是塞进 gather 的前缀下：
    # 它们的凭据需求与 gather 不同（推荐与导出根本不需要 Key），
    # 前缀相同会让人误以为整个 gather 都要 Key。
    app.include_router(refs_api.router)
    app.include_router(paperqa_api.router)
    # 开题报告生成（/api/proposal/*）：以「参考文献与知识库」阶段存下的资料为依据成稿。
    app.include_router(proposal_api.router)
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
                    "id": "references",
                    "name": "参考文献推荐",
                    "summary": "按选题从公开文献库检索 10~20 篇参考文献，按文献等级、"
                               "被引量、时效与开放获取综合排序，可导出与一键入知识库。",
                    "steps": ["填选题与关键词", "生成推荐清单", "导出 GB/T 7714 或 BibTeX",
                              "选中文献存入知识库"],
                    "needs_key": False,
                    "offline": False,
                    "note": "检索、排序、导出、入库全部不需要 Key；填了 Key 才追加一次"
                            "模型相关性精排。被引量来自 OpenAlex；下载量公开库没有，"
                            "一列如实显示为「—」。",
                },
                {
                    "id": "paperqa",
                    "name": "PaperQA2 文献简报",
                    "summary": "用 PaperQA2 对选中的文献建本地索引，可提问，"
                               "也可一键产出八节文献简报或多篇对比综述。",
                    "steps": ["选文献", "建立索引", "提问 / 出简报"],
                    "needs_key": True,
                    "offline": False,
                    "note": "建索引与提问都要模型（对每篇文献调用一次做摘要与向量化）；"
                            "索引建好后复用缓存。中文与 CNKI PDF 的三处兼容处理见 README。",
                },
                {
                    "id": "report",
                    "name": "开题报告",
                    "summary": "三层依据逐节成稿：本地知识库 + 联网检索到的真实文献 + 模型自身知识；"
                               "可导入任务书/模板自动读出框架与要求；参考文献按正文引用机械汇总。",
                    "steps": ["导入任务书/模板或填题目", "确认小节与写作要求", "选择依据来源",
                              "逐节生成", "导出 Markdown / Word"],
                    "needs_key": True,
                    "offline": False,
                    "note": "「导入任务书读出框架与要求」不需要 Key（本地解析 + 入库）；"
                            "只有「逐节生成」要模型。联网检索在生成时逐节进行，可关闭；"
                            "模型自身知识关不掉，但那部分不会被挂上引用编号。",
                },
                {
                    "id": "writing",
                    "name": "写作助手",
                    "summary": "可从 Word/PDF 导入写作要求与正文并直接出初稿，也可只重写某一章；逐章生成、可回退版本、自动挂引用编号。",
                    "steps": ["导入 Word/PDF 或填题目", "确认章节结构", "逐章生成", "单独重写某章", "导出"],
                    "needs_key": True,
                    "offline": False,
                    "note": "解析上传文档不需要 Key（读要求时需要）；正文里的数字与引用必须能在依据里找到。",
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
    #
    # 为什么前端资源用"整块读进内存"而不是 FileResponse / StaticFiles：
    #
    # FileResponse（以及 StaticFiles 内部）会先 os.stat 拿到文件大小，把它写进
    # Content-Length，然后再打开文件做流式发送。**两次操作之间文件如果被改写变大**，
    # 实际发送的字节就会超过声明的长度，uvicorn 直接抛
    #     RuntimeError: Response content longer than Content-Length
    # 并中止这个响应——浏览器看到的是页面加载失败。
    #
    # 本项目的 web/ 目录是会被频繁改写的（改前端时保存一次就变一次），实测日志里
    # 这条异常出现了 72 次。前端资源都是几十 KB 的小文本，整块读出来再返回，
    # 字节与 Content-Length 天然一致，这个竞态就不存在了。
    #
    # 取舍：读文件与写文件之间若真撞上，可能返回"刚写了一半"的内容（并发写场景），
    # 但那是显示层的一次性异常，刷新即可；**不会让服务端抛错中止连接**。
    # 开发环境这个取舍是划算的。
    _ASSET_TYPES = {
        ".html": "text/html; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
    }

    def _serve_asset(name: str) -> Response:
        path = (WEB_DIR / name).resolve()
        # 目录穿越防护：只允许 WEB_DIR 下的文件。
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            raise HTTPException(status_code=404, detail=f"资源不存在：{name}")
        data = path.read_bytes()
        media = _ASSET_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return Response(content=data, media_type=media)

    @app.get("/", include_in_schema=False)
    def index() -> Response:
        return _serve_asset("index.html")

    # 前端直接引用 /static/app.js 与 /static/theme.css；这里显式列出而不是整目录挂载，
    # 既避开上面的竞态，也顺带收紧了暴露面（vendor/ 之类不会被顺手serve出去）。
    @app.get("/static/{name}", include_in_schema=False)
    def static_asset(name: str) -> Response:
        return _serve_asset(name)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> JSONResponse:
        # 没有图标时返回 204，避免浏览器把 404 记进控制台。
        return JSONResponse({}, status_code=204)

    return app
