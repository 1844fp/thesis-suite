"""FastAPI 服务。

**这是给人用的那一层，不是给智能体用的。** 智能体通过黑板和消息总线协作，
服务只负责：暴露状态、触发流程、流式回传进度、接受人的决定。

几个刻意的设计：

* **长任务是流式回传的（SSE），不是轮询。** 跑一次完整流水线要几分钟，
  轮询要么太慢要么太密。SSE 让界面能实时显示"现在在跑哪个检查器"。
* **人的决定是显式接口。** ``needs_author`` 状态的问题需要作者标记"已处理"或
  "忽略"，这是流程能真正收尾的前提——工具不能替作者签字。
* **不提供"一键定稿"。** 每个会改动稿件的动作都是单独入口，且返回改了什么。
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from .agents import IMPORT_PROBLEMS
from .config import (
    DATA_DIR,
    ENV_PATH,
    INBOX_DIR,
    PROJECTS_DIR,
    RUNS_DIR,
    Settings,
    ensure_dirs,
    load_settings,
    write_env_updates,
)
from .framework.blackboard import Blackboard
from .framework.registry import registry_snapshot
from .pipeline import STAGE_ORDER, Runner
from .runtime import build_runtime

app = FastAPI(title="论文多智能体系统", version="0.1.0")

#: 运行时只装配一次。每次请求重建会把 @verifier 注册重复执行并报错。
_RUNTIME = None
_RUNTIME_LOCK = threading.Lock()

#: 正在跑的任务：project_id -> {"running": bool, "events": [...], "result": ...}
_JOBS: dict[str, dict[str, Any]] = {}


def runtime():
    global _RUNTIME
    if _RUNTIME is None:
        with _RUNTIME_LOCK:
            if _RUNTIME is None:
                _RUNTIME = build_runtime()
    return _RUNTIME


def settings() -> Settings:
    return runtime().settings


def _refresh_runtime() -> None:
    """Rebuild after a config change so new keys take effect without a restart."""
    global _RUNTIME
    with _RUNTIME_LOCK:
        _RUNTIME = build_runtime(load_settings())


def _project_or_404(pid: str) -> Blackboard:
    try:
        return Blackboard.load(PROJECTS_DIR, pid)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"项目不存在：{pid}") from None


# ---------------------------------------------------------------------------
# 状态与配置
# ---------------------------------------------------------------------------


@app.get("/api/ping")
def api_ping() -> dict:
    """**轻量存活探测**：立刻返回，什么都不查。

    为什么需要它：``/api/status`` 会去健康检查两个对端服务、列出全部项目、
    导出整个注册表快照，实测每次约 **2.1 秒**。拿它当"服务起来了吗"的探针，
    只要客户端超时短于 2 秒，**每一次探测都会超时**——启动器会一直等下去，
    而服务其实早就正常了。（这个坑踩过：启动器等满 60 秒后报"启动失败"，
    日志里 uvicorn 却明明写着 "Uvicorn running"。）

    "我活着吗"和"我的依赖都好吗"是两件事，前者必须是零成本的。
    """
    return {"ok": True, "app": "thesis-agents"}


@app.get("/api/status")
def api_status() -> dict:
    rt = runtime()
    from .adapters.rag import RAGAdapter
    from .adapters.reducer import ReducerAdapter

    peers = [
        RAGAdapter(rt.settings.peers.rag_base, timeout=6).health("/api/status").as_dict(),
        ReducerAdapter(rt.settings.peers.reducer_base, timeout=6).health().as_dict(),
    ]
    return {
        "ok": True,
        "agents": rt.registry.describe(),
        "skipped": dict(rt.skipped),
        "agent_module_problems": dict(IMPORT_PROBLEMS),
        "registry": registry_snapshot(),
        "peers": peers,
        "settings": rt.settings.public(),
        "stage_order": list(STAGE_ORDER),
        "projects": Blackboard.list_projects(PROJECTS_DIR),
    }


@app.get("/api/config")
def api_get_config() -> dict:
    return settings().public()


@app.post("/api/config")
async def api_set_config(payload: dict) -> dict:
    """Save model settings to .env and hot-reload.

    ``api_key`` semantics: absent/``None`` keeps the stored key, ``""`` clears it.
    The UI omits the field when the user did not touch it, so a blank input can
    never silently wipe a working key.
    """
    updates: dict[str, str] = {}
    llm = payload.get("llm") or {}

    if "base_url" in llm:
        updates["LLM_BASE_URL"] = str(llm["base_url"]).strip()
    if "model" in llm:
        updates["LLM_MODEL"] = str(llm["model"]).strip()
    if llm.get("api_key") is not None:
        updates["LLM_API_KEY"] = str(llm["api_key"]).strip()
    if llm.get("temperature") is not None:
        updates["LLM_TEMPERATURE"] = str(llm["temperature"])
    if llm.get("max_tokens") is not None:
        updates["LLM_MAX_TOKENS"] = str(llm["max_tokens"])

    peers = payload.get("peers") or {}
    if "rag_base" in peers:
        updates["RAG_BASE_URL"] = str(peers["rag_base"]).strip()
    if "reducer_base" in peers:
        updates["REDUCER_BASE_URL"] = str(peers["reducer_base"]).strip()

    verify = payload.get("verify") or {}
    if verify.get("external_enabled") is not None:
        updates["VERIFY_EXTERNAL"] = "true" if verify["external_enabled"] else "false"
    if "contact_email" in verify:
        updates["CONTACT_EMAIL"] = str(verify["contact_email"]).strip()
    if verify.get("max_external_lookups") is not None:
        updates["MAX_EXTERNAL_LOOKUPS"] = str(verify["max_external_lookups"])

    if payload.get("pipeline", {}).get("max_rounds") is not None:
        updates["MAX_ROUNDS"] = str(payload["pipeline"]["max_rounds"])

    if updates:
        write_env_updates(ENV_PATH, updates)
    _refresh_runtime()
    return {"ok": True, "saved": sorted(updates.keys()), "config": settings().public()}


@app.post("/api/config/test")
async def api_test_config(payload: dict) -> dict:
    """Send one minimal request to prove the key, URL and model name all work."""
    from .llm import LLMClient, LLMError

    s = settings()
    cfg = s.llm
    base_url = str(payload.get("base_url") or cfg.base_url).strip()
    model = str(payload.get("model") or cfg.model).strip()
    api_key = payload.get("api_key")
    api_key = cfg.api_key if api_key is None else str(api_key).strip()

    probe_cfg = type(cfg)(
        base_url=base_url, api_key=api_key, model=model,
        temperature=0.0, max_tokens=16, timeout=30.0,
    )
    t0 = time.time()
    try:
        out = LLMClient(probe_cfg).chat(
            [{"role": "user", "content": "只回复两个字：通路"}], max_tokens=16
        )
    except LLMError as exc:
        return {"ok": False, "error": str(exc), "seconds": round(time.time() - t0, 2)}
    return {
        "ok": True,
        "seconds": round(time.time() - t0, 2),
        "model": model,
        "reply": (out or "").strip()[:40],
    }


@app.get("/api/health/peers")
def api_peer_health() -> dict:
    from .adapters.rag import RAGAdapter
    from .adapters.reducer import ReducerAdapter

    s = settings()
    rows = [
        RAGAdapter(s.peers.rag_base, timeout=8).health("/api/status"),
        ReducerAdapter(s.peers.reducer_base, timeout=8).health(),
    ]
    return {"ok": all(r.reachable for r in rows), "peers": [r.as_dict() for r in rows]}


# ---------------------------------------------------------------------------
# 项目
# ---------------------------------------------------------------------------


@app.get("/api/projects")
def api_projects() -> dict:
    return {"projects": Blackboard.list_projects(PROJECTS_DIR)}


@app.post("/api/projects")
async def api_create_project(payload: dict) -> dict:
    """Create a project from pasted text or an existing file path."""
    from .pipeline import Runner

    title = str(payload.get("title") or "").strip()
    text = str(payload.get("text") or "")
    file_path = str(payload.get("file") or "").strip()
    meta = dict(payload.get("meta") or {})

    for key in ("retrieve_queries", "gather_roots", "keep_terms"):
        if isinstance(meta.get(key), str):
            meta[key] = [meta[key]] if meta[key].strip() else []

    runner = Runner(runtime())
    try:
        if file_path:
            board = runner.create_from_file(file_path, title=title, meta=meta)
        else:
            if not text.strip():
                raise HTTPException(status_code=400, detail="请提供 text 或 file")
            board = runner.create_project(title=title or "未命名项目", text=text, meta=meta)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 收稿失败要给出可读原因
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc

    return {"ok": True, "project": _project_summary(board)}


@app.post("/api/projects/upload")
async def api_upload_project(file: UploadFile = File(...), title: str = "") -> dict:
    """Upload a .docx/.txt/.md and create a project from it.

    The file lands in ``inbox/`` rather than a temp dir so the author can find it
    later — an uploaded manuscript that vanishes after the request is a support
    burden, not a convenience.
    """
    ensure_dirs()
    name = Path(file.filename or "manuscript.docx").name
    suffix = Path(name).suffix.lower()
    if suffix not in (".docx", ".txt", ".md", ".markdown"):
        raise HTTPException(
            status_code=400,
            detail=f"不支持的格式 {suffix or '(无扩展名)'}。请上传 .docx / .txt / .md。",
        )
    dest = INBOX_DIR / name
    if dest.exists():
        dest = INBOX_DIR / f"{Path(name).stem}_{int(time.time())}{suffix}"
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(raw) > 40 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件超过 40MB，请先精简图片后重试。")
    dest.write_bytes(raw)

    from .pipeline import Runner

    runner = Runner(runtime())
    try:
        board = runner.create_from_file(dest, title=title)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "project": _project_summary(board), "saved_to": str(dest)}


# ---------------------------------------------------------------------------
# 一键生成：开题报告 → 硕士论文初稿
# ---------------------------------------------------------------------------

#: 开题报告存放目录。与稿件分开放：开题是**输入**，不是待修改的稿件，
#: 混在 inbox 里会让"资料获取"把它当成一份参考文献去入库。
PROPOSAL_DIR = INBOX_DIR / "proposals"

#: 同时只允许一次一键生成。这条限制是必要的：一次生成会发起几十次模型调用，
#: 并发两次既烧钱，也会让两个流程同时写 data/runs/ 下的报告文件。
_THESIS_JOB: dict[str, Any] = {"running": False, "started": 0.0}


def _resolve_output(path: str) -> Path | None:
    """把用户给的输出路径限制在数据目录内。

    界面能传任意路径，如果直接写就会变成"通过这个服务往磁盘任意位置写文件"。
    所以只允许落在 ``data/runs`` 或 ``data/projects`` 下——一键生成本来也只需要
    写这两个地方。
    """
    if not path:
        return None
    try:
        p = Path(path).resolve()
    except Exception:  # noqa: BLE001
        return None
    for root in (RUNS_DIR.resolve(), PROJECTS_DIR.resolve()):
        try:
            p.relative_to(root)
            return p
        except ValueError:
            continue
    return None


@app.post("/api/thesis/proposal")
async def api_thesis_proposal(
    file: UploadFile | None = File(None),
    path: str = "",
    title: str = "",
) -> dict:
    """接收开题报告并返回**解析预览**。

    解析是纯规则、不调用模型，所以这个端点可以放心反复调用。先让作者看一眼
    "系统把你的开题读成了什么"，再决定要不要花几十次模型调用去生成——
    结构读错了，生成出来的几万字全部白费。
    """
    ensure_dirs()
    from .parsing.proposal import parse_proposal

    if file is not None and file.filename:
        name = Path(file.filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in (".docx", ".txt", ".md", ".markdown"):
            raise HTTPException(status_code=400, detail=f"不支持的格式 {suffix or '(无扩展名)'}，请用 .docx / .txt / .md。")
        PROPOSAL_DIR.mkdir(parents=True, exist_ok=True)
        dest = PROPOSAL_DIR / name
        if dest.exists():
            dest = PROPOSAL_DIR / f"{Path(name).stem}_{int(time.time())}{suffix}"
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="文件是空的")
        if len(raw) > 40 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="文件超过 40MB，请先精简图片后重试。")
        dest.write_bytes(raw)
        target = dest
    elif path:
        target = Path(path)
        if not target.exists():
            raise HTTPException(status_code=400, detail=f"文件不存在：{target}")
    else:
        raise HTTPException(status_code=400, detail="请上传开题报告文件，或给出它的路径")

    try:
        brief = parse_proposal(str(target), title_hint=title)
    except Exception as exc:  # noqa: BLE001 - 解析失败要给出可读原因
        raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
    return {"ok": True, "path": str(target), "brief": brief.as_dict()}


@app.get("/api/thesis/stream")
async def api_thesis_stream(
    proposal: str = "",
    topic: str = "",
    title: str = "",
    author: str = "",
    genre: str = "",
    requirement: str = "",
    degree: str = "硕士",
    words: int = 0,
    chapters: str = "",
    skip_chapters: str = "",
    model: str = "",
    out: str = "",
    stages: str = "",
    export: bool = True,
    llm_verify: bool = False,
    dedup: bool = False,
    precheck: bool = True,
    force: bool = False,
) -> StreamingResponse:
    """跑一次一键生成并把进度流式回传。

    与 ``/api/projects/{pid}/run/stream`` 同一条原则：**这次请求就是这次运行**，
    关掉页面即中断。区别在于它自己建项目，所以不需要先选项目。

    开题报告**可以不给**——给了就按开题的章节树写，不给就用 ``topic`` +
    ``genre`` 对应的默认结构。两条路都能走通。
    """
    if _THESIS_JOB.get("running"):
        raise HTTPException(status_code=409, detail="已有一次一键生成在进行中，请等它结束或刷新页面。")

    from .thesis import DEFAULT_STAGES, ThesisRequest, generate_thesis

    if not proposal and not (topic or title):
        raise HTTPException(status_code=400, detail="请填写论文题目，或上传一份开题报告")
    wanted = tuple(s for s in stages.split(",") if s) or DEFAULT_STAGES
    if dedup:
        wanted = tuple(dict.fromkeys((*wanted, "dedup")))
    unknown = [s for s in wanted if s not in STAGE_ORDER]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知阶段 {unknown}")

    out_path = _resolve_output(out)
    if out and out_path is None:
        raise HTTPException(
            status_code=400,
            detail=f"输出路径必须落在 {RUNS_DIR} 或 {PROJECTS_DIR} 之内",
        )

    def _split(value: str) -> list[str]:
        return [p.strip() for p in re.split(r"[;；,，\n]+", value or "") if p.strip()]

    events: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def progress(msg: str, ratio: float) -> None:
        asyncio.run_coroutine_threadsafe(
            events.put({"type": "progress", "message": msg, "ratio": ratio}), loop
        )

    def worker() -> None:
        try:
            req = ThesisRequest(
                proposal_path=proposal,
                title=title,
                author=author,
                topic=topic,
                genre=genre,
                requirement=requirement,
                degree=degree or "硕士",
                word_target=max(0, int(words or 0)),
                chapters=_split(chapters),
                skip_chapters=_split(skip_chapters),
                out_path=str(out_path) if out_path else "",
                stages=wanted,
                export=bool(export),
                llm_verify=bool(llm_verify),
                precheck=bool(precheck),
                force=bool(force),
                model=model,
            )
            result = generate_thesis(req, settings=load_settings(), progress=progress)
            asyncio.run_coroutine_threadsafe(
                events.put({"type": "result", "result": result.as_dict()}), loop
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都要送到界面
            asyncio.run_coroutine_threadsafe(
                events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"}), loop
            )
        finally:
            asyncio.run_coroutine_threadsafe(events.put(None), loop)

    async def gen():
        _THESIS_JOB["running"] = True
        _THESIS_JOB["started"] = time.time()
        loop.run_in_executor(None, worker)
        yield f"data: {json.dumps({'type': 'start', 'stages': list(wanted)}, ensure_ascii=False)}\n\n"
        try:
            while True:
                ev = await events.get()
                if ev is None:
                    break
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        finally:
            _THESIS_JOB["running"] = False

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# 模型档案（多模型 API 接入）
# ---------------------------------------------------------------------------


def _model_registry():
    """当前进程的模型档案。每次从磁盘读，保证界面改完立刻生效。"""
    from .models import ModelRegistry

    return ModelRegistry.load()


@app.get("/api/models")
def api_models() -> dict:
    """全部模型档案、当前激活项、用途分配、厂商预设。密钥只出脱敏串。"""
    from .models import preset_list

    reg = _model_registry()
    return {
        "ok": True,
        **reg.public(),
        "presets": preset_list(),
        "routing": _model_routing(reg),
    }


def _model_routing(reg) -> list[dict]:
    """每个用途实际会用哪个模型——让"按用途分模型"的结果看得见。"""
    from .models import ROLES

    out: list[dict] = []
    for role, desc in ROLES:
        prof = reg.resolve(role)
        out.append(
            {
                "role": role,
                "label": desc,
                "profile": (prof.id if prof else ""),
                "profile_label": ((prof.label or prof.id) if prof else ""),
                "model": (prof.model if prof else ""),
                "base_url": (prof.base_url if prof else ""),
                "explicit": bool(reg.roles.get(role)),
            }
        )
    return out


@app.post("/api/models/profile")
async def api_model_upsert(payload: dict | None = None) -> dict:
    """新增或更新一份模型档案。

    ``api_key`` 传空串表示"清空"，传 ``null``/不传表示"保留原来的"——
    "保留旧密钥"是调用方的决定，不该由文件格式层来猜。
    """
    from .models import ModelProfile

    body = payload or {}
    pid = str(body.get("id") or "").strip()
    if not pid:
        raise HTTPException(status_code=400, detail="档案 id 不能为空")
    reg = _model_registry()
    old = reg.profiles.get(pid)
    if old is not None and body.get("api_key") is None:
        body = {**body, "api_key": old.api_key}
    if body.get("api_key") is None:
        body = {**body, "api_key": ""}
    prof = ModelProfile.from_dict({**body, "id": pid})
    if not prof.label:
        prof.label = pid
    reg.upsert(prof)
    reg.save()
    _refresh_runtime()
    return {"ok": True, "profile": prof.public(), "models": reg.public()}


@app.delete("/api/models/profile/{pid}")
def api_model_delete(pid: str) -> dict:
    reg = _model_registry()
    if not reg.remove(pid):
        raise HTTPException(status_code=404, detail=f"没有这个档案：{pid}")
    reg.save()
    _refresh_runtime()
    return {"ok": True, "models": reg.public()}


@app.post("/api/models/active")
async def api_model_active(payload: dict | None = None) -> dict:
    """切换当前激活的档案（所有没单独配用途的调用都用它）。"""
    pid = str((payload or {}).get("id") or "").strip()
    reg = _model_registry()
    if pid not in reg.profiles:
        raise HTTPException(status_code=404, detail=f"没有这个档案：{pid}")
    reg.active = pid
    reg.save()
    _refresh_runtime()
    return {"ok": True, "models": reg.public(), "routing": _model_routing(_model_registry())}


@app.post("/api/models/roles")
async def api_model_roles(payload: dict | None = None) -> dict:
    """设置"用途 → 档案"。值传空串表示该用途回到当前激活的档案。"""
    from .models import ROLES

    body = payload or {}
    roles = body.get("roles") or {}
    if not isinstance(roles, dict):
        raise HTTPException(status_code=400, detail="roles 必须是一个对象")
    valid = {r for r, _ in ROLES}
    reg = _model_registry()
    for role, pid in roles.items():
        role = str(role)
        if role not in valid:
            raise HTTPException(status_code=400, detail=f"未知用途：{role}（可用：{sorted(valid)}）")
        pid = str(pid or "").strip()
        if pid and pid not in reg.profiles:
            raise HTTPException(status_code=404, detail=f"用途 {role} 指向的档案不存在：{pid}")
        reg.set_role(role, pid)
    reg.save()
    _refresh_runtime()
    return {"ok": True, "models": reg.public(), "routing": _model_routing(reg)}


@app.post("/api/models/test")
async def api_model_test(payload: dict | None = None) -> dict:
    """真实调用一次，确认这份档案能用。

    支持三种用法：

    * ``{"id": "..."}`` —— 测已保存的档案；
    * ``{"all": true}`` —— **逐个测试全部档案**，配了多个厂商时一次看完；
    * 直接给 ``{base_url, model, api_key}`` —— 测**还没保存**的填法，
      先测通再保存比"保存了发现不对再改"省事。
    """
    from dataclasses import replace as _replace

    from .llm import LLMConfig
    from .models import ModelProfile, test_profile

    body = payload or {}
    reg = _model_registry()

    if body.get("all"):
        if not reg.profiles:
            raise HTTPException(status_code=400, detail="还没有配置任何档案")
        timeout = float(body.get("timeout") or 30)
        rows: list[dict] = []
        for pid, prof in reg.profiles.items():
            if not prof.configured:
                rows.append({"id": pid, "label": prof.label or pid, "ok": False,
                             "error": "还没填全（缺 base_url / 模型名 / key）", "seconds": 0.0})
                continue
            res = test_profile(prof, timeout=timeout)
            rows.append({"id": pid, "label": prof.label or pid, **res})
        return {"ok": all(r.get("ok") for r in rows), "results": rows}

    pid = str(body.get("id") or "").strip()
    if pid and pid in reg.profiles:
        prof = reg.profiles[pid]
        if body.get("api_key"):
            prof = _replace(prof, api_key=str(body["api_key"]))
    elif body.get("base_url") or body.get("model"):
        prof = ModelProfile.from_dict(
            {
                "id": pid or "adhoc",
                "base_url": body.get("base_url") or "",
                "model": body.get("model") or "",
                "api_key": body.get("api_key") or "",
                "timeout": body.get("timeout") or 30,
            }
        )
    else:
        raise HTTPException(status_code=400, detail="请给出档案 id、all=true，或直接给出 base_url 与模型名")

    base = runtime().settings.llm if reg.profiles else LLMConfig()
    result = test_profile(prof, base=base, timeout=float(body.get("timeout") or 30))
    return {"ok": bool(result.get("ok")), "result": result, "profile": prof.public()}


# ---------------------------------------------------------------------------
# 知识库
# ---------------------------------------------------------------------------


@app.get("/api/genres")
def api_genres() -> dict:
    """可选的论文类型与预置章节结构，供界面填默认值。"""
    from .writing.genres import genre_choices

    return {"genres": genre_choices()}


@app.get("/api/kb/status")
def api_kb_status() -> dict:
    from .kb import kb_status

    return kb_status(runtime().settings).as_dict()


@app.get("/api/kb/docs")
def api_kb_docs() -> dict:
    """知识库馆藏清单。服务不可达时返回 ``ok=False`` 而不是 500——
    界面上"库是空的"和"库连不上"是两件必须分开显示的事。"""
    from .kb import list_docs

    try:
        docs = list_docs(runtime().settings)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc) or f"{type(exc).__name__}", "docs": []}
    return {"ok": True, "docs": docs, "count": len(docs)}


@app.post("/api/kb/search")
async def api_kb_search(payload: dict | None = None) -> dict:
    from .kb import search

    body = payload or {}
    query = str(body.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请填写查询词")
    try:
        hits = search(
            query,
            k=int(body.get("k") or 8),
            settings=runtime().settings,
            rerank=bool(body.get("rerank")),
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc) or f"{type(exc).__name__}") from exc
    return {
        "ok": True,
        "hits": [
            {"index": h.index, "citation": h.citation, "text": h.text[:400],
             "score": round(h.score, 4), "doc_id": h.doc_id}
            for h in hits
        ],
    }


@app.post("/api/kb/check")
async def api_kb_check(payload: dict | None = None) -> dict:
    """**生成前预检**：这个题目在这个知识库里能不能写。零成本。"""
    from .kb import precheck

    body = payload or {}
    topic = str(body.get("topic") or "").strip()
    if not topic:
        raise HTTPException(status_code=400, detail="请填写论文题目")
    r = precheck(
        topic,
        requirement=str(body.get("requirement") or ""),
        settings=runtime().settings,
        k=int(body.get("k") or 8),
    )
    return r.as_dict()


@app.post("/api/kb/add")
async def api_kb_add(files: list[UploadFile] = File(...)) -> dict:
    """上传资料投进知识库并触发入库。"""
    import tempfile

    from .kb import add_files, peer_inbox

    s = runtime().settings
    staging = peer_inbox(s) / "_upload"
    staging.mkdir(parents=True, exist_ok=True)
    saved: list[str] = []
    for f in files:
        name = Path(f.filename or "upload.pdf").name
        raw = await f.read()
        if not raw:
            continue
        if len(raw) > 80 * 1024 * 1024:
            raise HTTPException(status_code=400, detail=f"{name} 超过 80MB，请先精简后重试")
        dest = staging / name
        dest.write_bytes(raw)
        saved.append(str(dest))
    if not saved:
        raise HTTPException(status_code=400, detail="没有收到有效文件")
    try:
        return {"ok": True, **add_files(saved, settings=s, ingest=True)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc) or f"{type(exc).__name__}") from exc


@app.post("/api/kb/ingest")
async def api_kb_ingest(payload: dict | None = None) -> dict:
    from .kb import ingest

    try:
        results = ingest(runtime().settings, force=bool((payload or {}).get("force")))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc) or f"{type(exc).__name__}") from exc
    ok = sum(1 for x in results if x.get("ok"))
    return {"ok": True, "total": len(results), "succeeded": ok, "results": results[:50]}


# ---------------------------------------------------------------------------
# 改写
# ---------------------------------------------------------------------------


@app.post("/api/projects/{pid}/rewrite")
async def api_rewrite(pid: str, payload: dict | None = None) -> dict:
    """按作者的要求改写指定章节或原文片段。

    同步执行而不是 SSE：一次改写是 2~4 次模型调用，几十秒内能返回，
    而"改了什么"需要完整结果才好展示。超长等待由前端给提示。
    """
    from .framework.artifact import Stage
    from .framework.state import Pipeline
    from .pipeline import Runner

    board = _project_or_404(pid)
    body = payload or {}
    targets = body.get("targets")
    if isinstance(targets, dict):
        targets = [targets]
    if not isinstance(targets, list) or not targets:
        raise HTTPException(status_code=400, detail="请给出要改写的内容与改写要求")
    clean = [
        {
            "section": str(t.get("section") or "").strip(),
            "find": str(t.get("find") or "").strip(),
            "instruction": str(t.get("instruction") or "").strip(),
        }
        for t in targets
        if isinstance(t, dict) and str(t.get("instruction") or "").strip()
    ]
    if not clean:
        raise HTTPException(status_code=400, detail="每个改写目标都要有 instruction（改写要求）")

    board.project.meta["rewrite_targets"] = clean
    board.save()
    rt = runtime()
    ctx = rt.new_context(board, stage=Stage.DRAFT.value)
    pipe = Pipeline(rt.registry, ctx)
    rep = pipe.run_agent("writer:rewrite", stage=Stage.DRAFT.value)
    ref = None
    if rep.ok:
        # 改写把稿子拉回了证据池编号空间，必须重新定稿（连续化 + 重建文献表）。
        ref = pipe.run_agent("writer:references", stage=Stage.FINAL.value)
    return {
        "ok": rep.ok,
        "rewrite": rep.as_dict(),
        "references": ref.as_dict() if ref else None,
        "project": _project_summary(_project_or_404(pid)),
    }


@app.get("/api/thesis/file")
def api_thesis_file(path: str, download: bool = True):
    """下载一键生成产出的文件（.docx / 生成报告）。

    路径必须落在 ``data/runs`` 或 ``data/projects`` 之内——这个接口接受用户给的
    路径，不加限制就等于把整个磁盘开放出去。
    """
    p = _resolve_output(path)
    if p is None or not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在，或不在允许的目录内")
    return FileResponse(
        str(p),
        filename=p.name,
        media_type="application/octet-stream" if download else None,
    )


def _project_summary(board: Blackboard) -> dict:
    p = board.project
    return {
        "id": p.id,
        "title": p.title,
        "stage": p.stage,
        "chars": len(board.draft_text),
        "draft_version": board.draft.version if board.draft else 0,
        "meta": {k: v for k, v in p.meta.items() if k != "format_facts"},
        "intake_warnings": p.meta.get("intake_warnings") or [],
        "has_format_facts": bool(p.meta.get("format_facts")),
    }


@app.get("/api/projects/{pid}")
def api_project(pid: str, with_text: bool = False) -> dict:
    board = _project_or_404(pid)
    data = board.project.as_dict(with_text=with_text)
    # format_facts 可能很大，列表接口不需要它。
    data["meta"].pop("format_facts", None)
    data["severity"] = board.severity_counts()
    data["stage_order"] = list(STAGE_ORDER)
    return data


@app.delete("/api/projects/{pid}")
def api_delete_project(pid: str) -> dict:
    import shutil

    d = PROJECTS_DIR / pid
    if not d.exists():
        raise HTTPException(status_code=404, detail=f"项目不存在：{pid}")
    shutil.rmtree(d)
    return {"ok": True}


@app.get("/api/projects/{pid}/versions")
def api_versions(pid: str) -> dict:
    board = _project_or_404(pid)
    return {
        "versions": [
            {
                "version": v.version,
                "id": v.id,
                "chars": len(v.text),
                "created_by": v.created_by,
                "stage": v.created_at_stage,
                "notes": v.notes,
                "parent": v.parent,
                "hash": v.content_hash,
            }
            for v in board.versions("draft")
        ]
    }


@app.get("/api/projects/{pid}/diff")
def api_diff(pid: str, a: int = 1, b: int = 0) -> dict:
    """Paragraph-level diff between two draft versions."""
    import difflib

    board = _project_or_404(pid)
    versions = {v.version: v for v in board.versions("draft")}
    if not versions:
        raise HTTPException(status_code=404, detail="该项目还没有稿件版本")
    latest = max(versions)
    va = versions.get(a) or versions[min(versions)]
    vb = versions.get(b) if b else versions[latest]
    if vb is None:
        raise HTTPException(status_code=404, detail=f"没有 v{b}")

    left = (va.text or "").split("\n")
    right = (vb.text or "").split("\n")
    rows = []
    for line in difflib.unified_diff(left, right, lineterm="", n=1):
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("@@"):
            rows.append({"kind": "hunk", "text": line})
        elif line.startswith("+"):
            rows.append({"kind": "add", "text": line[1:]})
        elif line.startswith("-"):
            rows.append({"kind": "del", "text": line[1:]})
        else:
            rows.append({"kind": "same", "text": line[1:] if line[:1] == " " else line})
    return {"a": va.version, "b": vb.version, "rows": rows,
            "a_id": va.id, "b_id": vb.id}


@app.get("/api/projects/{pid}/issues")
def api_issues(pid: str, status: str = "", severity: str = "", fixable: bool = False) -> dict:
    board = _project_or_404(pid)
    issues = list(board.project.issues.values())
    if status:
        issues = [i for i in issues if i.status == status]
    if severity:
        issues = [i for i in issues if i.severity.value == severity]
    if fixable:
        issues = [i for i in issues if i.fixable]
    issues.sort(key=lambda i: (-i.rank, i.kind, i.id))
    return {"issues": [i.as_dict() for i in issues], "severity": board.severity_counts()}


@app.post("/api/projects/{pid}/issues/{iid}")
async def api_update_issue(pid: str, iid: str, payload: dict) -> dict:
    """Let the author close or ignore an issue.

    Only ``needs_author``/``open`` issues can be decided by hand — a ``fixed``
    issue was closed by re-verification, and letting the UI reopen it would make
    the audit trail lie.
    """
    board = _project_or_404(pid)
    issue = board.project.issues.get(iid)
    if issue is None:
        raise HTTPException(status_code=404, detail=f"问题不存在：{iid}")
    action = str(payload.get("action") or "").strip()
    note = str(payload.get("note") or "")[:300]
    if action not in ("resolved", "wontfix", "reopen"):
        raise HTTPException(status_code=400, detail="action 必须是 resolved / wontfix / reopen")

    if action == "resolved":
        issue.status = "fixed"
        issue.resolved_by = "author"
        issue.resolution_note = note or "作者确认已处理"
    elif action == "wontfix":
        issue.status = "wontfix"
        issue.resolved_by = "author"
        issue.resolution_note = note or "作者确认不处理"
    else:
        issue.status = "open"
        issue.resolved_by = ""
        issue.resolution_note = note or "作者重新打开"

    board.record(board.project.stage, 0, "author", f"issue_{action}", f"{iid} {note}")
    board.save()
    return {"ok": True, "issue": issue.as_dict(), "severity": board.severity_counts()}


@app.post("/api/projects/{pid}/manual_fix")
async def api_manual_fix(pid: str, payload: dict) -> dict:
    """Apply an author-supplied exact replacement.

    Same op machinery the agents use, so a manual edit is just as auditable as an
    automatic one — and it goes through the identical all-or-nothing check.
    """
    from .framework.artifact import Patch, new_id
    from .framework.ops import apply_op

    board = _project_or_404(pid)
    find = str(payload.get("find") or "")
    replace = str(payload.get("replace") or "")
    if not find:
        raise HTTPException(status_code=400, detail="find 不能为空")
    if find not in board.draft_text:
        raise HTTPException(status_code=400, detail="原文中找不到该片段")

    op: dict[str, Any] = {"op": "replace_span", "find": find, "replace": replace}
    # 作者可以显式允许替换多处；不传则沿用算子默认的"必须唯一"。
    if payload.get("count"):
        op["count"] = int(payload["count"])
    res = apply_op(board.draft_text, op)
    if not res.ok:
        raise HTTPException(status_code=400, detail=res.error)
    if not res.changed:
        raise HTTPException(status_code=400, detail="替换后文本没有变化")

    patch = Patch(id=new_id("ptc"), issue_ids=list(payload.get("issue_ids") or []),
                  ops=[op], rationale=str(payload.get("note") or "作者手工修改"),
                  created_by="author", applied=True)
    board.add_patch(patch)
    art = board.put_artifact(kind="draft", text=res.text, created_by="author",
                             stage=board.project.stage, note=patch.rationale)
    if patch.issue_ids:
        board.mark_fixed(patch.issue_ids, by="author", stage=board.project.stage,
                         note=f"手工修改 → v{art.version}")
    board.record(board.project.stage, 0, "author", "manual_fix", f"→ v{art.version}")
    board.save()
    return {"ok": True, "version": art.version, "chars": len(art.text)}


# ---------------------------------------------------------------------------
# 运行流水线（SSE）
# ---------------------------------------------------------------------------


class _JobStream:
    """Bridge a worker thread to an asyncio queue.

    The pipeline is synchronous and can run for minutes; running it in a thread
    keeps the event loop free so the UI stays responsive and other requests are
    served while a run is in flight.
    """

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.done = False

    def emit(self, event: dict) -> None:
        self.q.put(event)

    def close(self) -> None:
        self.done = True
        self.q.put(None)


@app.post("/api/projects/{pid}/run")
async def api_run(pid: str, payload: dict | None = None) -> dict:
    """Describe how to run, without starting anything.

    运行本身就是 ``GET /run/stream`` 那次请求（见下）。这个端点的存在只是为了让
    界面能先问一句"能不能跑、会被跑哪些阶段"，而不是让调用方去猜 SSE 的 URL 参数。
    刻意**不**在这里启动后台任务：那会造成"启动一次 + 轮询一次"的双入口，
    同一个项目被跑两遍，而黑板假定同一时刻只有一个写入者。
    """
    _project_or_404(pid)
    stages = [s for s in ((payload or {}).get("stages") or STAGE_ORDER) if s in STAGE_ORDER]
    if _JOBS.get(pid, {}).get("running"):
        return {"ok": False, "running": True, "detail": "该项目已有一次运行在进行中"}
    return {"ok": True, "running": False, "stages": stages, "stream": f"/api/projects/{pid}/run/stream"}


@app.get("/api/projects/{pid}/run/stream")
async def api_run_stream(
    pid: str,
    stages: str = "",
    max_rounds: int = 0,
    llm_verify: bool = True,
    dry_run: bool = False,
) -> StreamingResponse:
    """Run the pipeline and stream progress as SSE.

    Running *is* this request: closing the tab cancels the run. That is the
    honest behaviour — a "start" endpoint plus a separate poller invites two
    runs on the same project, which the blackboard is not built for.
    """
    board = _project_or_404(pid)
    if _JOBS.get(pid, {}).get("running"):
        raise HTTPException(status_code=409, detail="该项目已有一次运行在进行中")
    wanted = [s for s in (stages.split(",") if stages else []) if s] or list(STAGE_ORDER)
    unknown = [s for s in wanted if s not in STAGE_ORDER]
    if unknown:
        raise HTTPException(status_code=400, detail=f"未知阶段 {unknown}")

    events: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def progress(msg: str, ratio: float) -> None:
        asyncio.run_coroutine_threadsafe(
            events.put({"type": "progress", "message": msg, "ratio": ratio}), loop
        )

    def worker() -> None:
        try:
            runner = Runner(runtime())
            result = runner.run(
                pid, stages=wanted, max_rounds=max_rounds or None,
                include_llm_verify=llm_verify, progress=progress, dry_run=dry_run,
            )
            asyncio.run_coroutine_threadsafe(
                events.put({"type": "result", "result": result.as_dict()}), loop
            )
        except Exception as exc:  # noqa: BLE001 - 任何异常都要送到界面
            asyncio.run_coroutine_threadsafe(
                events.put({"type": "error", "message": f"{type(exc).__name__}: {exc}"}), loop
            )
        finally:
            asyncio.run_coroutine_threadsafe(events.put(None), loop)

    async def gen():
        job = _JOBS.setdefault(pid, {"events": []})
        job["running"] = True
        task = loop.run_in_executor(None, worker)
        yield f"data: {json.dumps({'type': 'start', 'stages': wanted}, ensure_ascii=False)}\n\n"
        try:
            while True:
                ev = await events.get()
                if ev is None:
                    break
                if ev.get("type") == "progress":
                    job["events"].append(ev)
                    if len(job["events"]) > 500:
                        del job["events"][:250]
                elif ev.get("type") == "result":
                    job["result"] = ev["result"]
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
        finally:
            job["running"] = False
            await task

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/projects/{pid}/export")
def api_export(pid: str, kind: str = "draft", version: int = 0, download: bool = False):
    board = _project_or_404(pid)
    if kind == "issues":
        lines = [f"# {board.project.title} · 问题清单", ""]
        counts = board.severity_counts()
        lines.append(f"致命 {counts['blocker']} · 重要 {counts['major']} · 轻微 {counts['minor']} "
                     f"· 提示 {counts['info']} · 已修复 {counts['fixed']} · 待人工 {counts['needs_author']}")
        lines.append("")
        for i in sorted(board.project.issues.values(), key=lambda x: (-x.rank, x.kind)):
            lines.append(f"## [{i.severity.value}] {i.kind} ({i.status})")
            lines.append(i.message)
            if i.evidence:
                lines.append(f"\n> 证据：{i.evidence}")
            if i.fix_hint:
                lines.append(f"\n建议：{i.fix_hint}")
            lines.append(f"\n定位：`{json.dumps(i.location, ensure_ascii=False)}`\n")
        payload = "\n".join(lines)
    else:
        if kind == "version" and version:
            target = next((v for v in board.versions("draft") if v.version == version), None)
            if target is None:
                raise HTTPException(status_code=404, detail=f"没有 v{version}")
            payload = target.text
        else:
            payload = board.draft_text

    if download:
        from urllib.parse import quote

        fname = quote(f"{board.project.title}_{kind}.txt")
        return StreamingResponse(
            iter([payload.encode("utf-8")]),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"},
        )
    return JSONResponse({"kind": kind, "chars": len(payload), "text": payload})


# ---------------------------------------------------------------------------
# 直达能力（不建项目也能用）
# ---------------------------------------------------------------------------


@app.get("/api/projects/{pid}/review")
def api_review(pid: str, open_only: bool = False, download: bool = False):
    """模拟审稿意见书：把问题清单整理成五个维度 + 评分 + 提交建议。

    评分与建议**由问题数量机械推导**，不是模型打的印象分——
    模型给自己刚生成的稿子打分会系统性偏高，而"能不能交"这个判断一旦失真，
    整份意见书就没有意义了。
    """
    from .review import build_review, render_review

    board = _project_or_404(pid)
    issues = list(board.project.issues.values())
    if open_only:
        issues = [i for i in issues if i.status == "open"]
    report = build_review(issues, title=board.project.title)
    text = render_review(report)
    if download:
        safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", board.project.title or pid)[:60] or pid
        out = RUNS_DIR / pid / f"{safe}_审稿意见书.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        return FileResponse(str(out), filename=out.name, media_type="text/markdown")
    return {"ok": True, "markdown": text, **report.as_dict()}


@app.get("/api/projects/{pid}/export_docx")
def api_export_docx(pid: str, download: bool = True):
    """把当前稿件导出为规范排版的 .docx 并直接返回文件。

    与 ``/api/projects/{pid}/export`` 分开：那个返回文本给界面显示，
    这个返回文件给浏览器下载。
    """
    import tempfile

    from .export.docx_writer import export_docx

    board = _project_or_404(pid)
    text = board.draft_text or ""
    if not text.strip():
        raise HTTPException(status_code=400, detail="稿件为空，没有可导出的内容")
    safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", board.project.title or pid)[:60] or pid
    out = RUNS_DIR / pid / f"{safe}.docx"
    out.parent.mkdir(parents=True, exist_ok=True)
    res = export_docx(
        text, out,
        title=board.project.title,
        author=str(board.project.meta.get("author") or ""),
    )
    if not res.ok:
        raise HTTPException(
            status_code=500,
            detail=f"导出失败：{res.verification.get('error') or '未知原因'}",
        )
    return FileResponse(
        str(out), filename=out.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


@app.post("/api/tools/score")
async def api_score(payload: dict) -> dict:
    """Style health check via the peer service (no model cost)."""
    from .adapters.base import PeerError
    from .adapters.reducer import ReducerAdapter

    text = str(payload.get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="文本为空")
    try:
        rep = ReducerAdapter(settings().peers.reducer_base, timeout=60).score(text)
    except PeerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "index": rep.index, "metrics": rep.metrics,
            "rule_hits": len(rep.rules), "top_suspects": rep.top_suspects(5)}


@app.post("/api/tools/search")
async def api_search(payload: dict) -> dict:
    from .adapters.base import PeerError
    from .adapters.rag import RAGAdapter

    q = str(payload.get("query") or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="查询为空")
    try:
        hits = RAGAdapter(settings().peers.rag_base, timeout=120).search(
            q, k=int(payload.get("k") or 8), rerank=False
        )
    except PeerError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "hits": [h.as_dict() for h in hits]}


@app.post("/api/tools/verify_references")
async def api_verify_references(payload: dict) -> dict:
    """Check reference entries against Crossref/OpenAlex, on demand.

    Exposed separately from the pipeline because this is the question authors ask
    most often and most urgently: "did I cite something that does not exist?"
    It works on pasted text, so it can be used before a project exists.
    """
    from .adapters.research import CrossrefAdapter, ExternalVerifier, OpenAlexAdapter
    from .agents.references_extract import parse_entries, split_reference_section

    text = str(payload.get("text") or "")
    if not text.strip():
        raise HTTPException(status_code=400, detail="文本为空")
    s = settings()
    if not s.verify.external_enabled:
        return {"ok": False, "error": "外部核验已在设置中关闭（VERIFY_EXTERNAL=false）"}

    _, ref_text = split_reference_section(text)
    source = ref_text or text
    try:
        entries = parse_entries(source)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"参考文献解析失败：{exc}") from exc
    if not entries:
        return {"ok": True, "entries": 0, "results": [], "note": "没有解析出参考文献条目"}

    limit = min(len(entries), int(payload.get("limit") or s.verify.max_external_lookups))
    verifier = ExternalVerifier(
        crossref=CrossrefAdapter(mailto=s.verify.contact_email, timeout=25),
        openalex=OpenAlexAdapter(mailto=s.verify.contact_email, timeout=25),
        threshold=s.verify.title_match_threshold,
    )
    results = []
    for e in entries[:limit]:
        r = verifier.verify(doi=getattr(e, "doi", "") or "", title=getattr(e, "title", "") or "")
        results.append({"index": getattr(e, "index", None), "raw": (getattr(e, "raw", "") or "")[:200],
                        **r})
        await asyncio.sleep(s.verify.external_delay)
    summary: dict[str, int] = {}
    for r in results:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    return {"ok": True, "entries": len(entries), "checked": len(results),
            "skipped": max(0, len(entries) - len(results)), "summary": summary, "results": results}


@app.get("/api/inbox")
def api_inbox() -> dict:
    ensure_dirs()
    rows = []
    for p in sorted(INBOX_DIR.glob("*")):
        if p.is_file():
            rows.append({"name": p.name, "bytes": p.stat().st_size,
                         "mtime": p.stat().st_mtime, "path": str(p)})
    return {"inbox": rows, "dir": str(INBOX_DIR)}


# ---------------------------------------------------------------------------
# 排版整理：把属性写回已有的 .docx
# ---------------------------------------------------------------------------

#: 整理后的文件放这里。与上传的开题报告分开：那个会被"资料获取"当成素材入库，
#: 排版结果不是素材。
FORMAT_DIR = INBOX_DIR / "formatted"


def _format_kwargs(
    *,
    body_font: str = "",
    latin_font: str = "",
    body_size: float | None = None,
    line_spacing: float | None = None,
    line_fixed: float | None = None,
    indent_chars: float | None = None,
    align: str = "",
    h1_font: str = "",
    h2_font: str = "",
    h1_size: float | None = None,
    h2_size: float | None = None,
    h3_size: float | None = None,
    caption_size: float | None = None,
    margin: str = "",
) -> tuple[Any, list[str]]:
    """把界面/命令行传来的选项拼成 FormatScheme。空值表示"用规则集里的值"。"""
    from .format import FormatScheme, load_default_ruleset, scheme_from_ruleset

    sch: FormatScheme = scheme_from_ruleset(load_default_ruleset())
    notes: list[str] = []
    for value, attr in (
        (body_size, "body_size_pt"), (indent_chars, "body_first_line_chars"),
        (h1_size, "h1_size_pt"), (h2_size, "h2_size_pt"), (h3_size, "h3_size_pt"),
        (caption_size, "caption_size_pt"),
    ):
        if value:
            setattr(sch, attr, float(value))
    if body_font:
        sch.body_cjk_font = body_font
    if latin_font:
        sch.body_latin_font = latin_font
    if h1_font:
        sch.h1_cjk_font = h1_font
    if h2_font:
        sch.h2_cjk_font = h2_font
        sch.h3_cjk_font = h2_font
    if align:
        sch.body_align = align
    if line_fixed:
        sch.body_line_mode, sch.body_line_value = "fixed", float(line_fixed)
    elif line_spacing:
        sch.body_line_mode, sch.body_line_value = "multiple", float(line_spacing)
    if margin:
        for pair in str(margin).split(","):
            key, _, raw = pair.partition("=")
            attr = {
                "top": "margin_top_cm", "bottom": "margin_bottom_cm",
                "left": "margin_left_cm", "right": "margin_right_cm",
                "header": "header_cm", "footer": "footer_cm",
            }.get(key.strip())
            if attr and raw.strip():
                setattr(sch, attr, float(raw))
            elif key.strip():
                notes.append(f"忽略了不认识的页边距写法：{pair}")
    return sch, notes


@app.get("/api/format/scheme")
def api_format_scheme() -> dict:
    """返回默认排版方案，供界面把输入框填成"当前规范值"。"""
    from .format import WRITABLE_RULE_IDS, FormatScheme, load_default_ruleset, scheme_from_ruleset

    rs = load_default_ruleset()
    sch = scheme_from_ruleset(rs)
    return {
        "scheme": sch.to_dict(),
        "describe": sch.describe(),
        "rules": len(rs.rules),
        "writable_rules": len(WRITABLE_RULE_IDS),
        "defaults": FormatScheme().to_dict(),
        "note": "这些值来自 app/format/rules_default.yaml（通用规范基线），"
                "不是任何一所学校的官方规定；拿到学校模板请用「照模板改」。",
    }


@app.post("/api/format/check")
async def api_format_check(
    file: UploadFile | None = File(None),
    path: str = "",
) -> dict:
    """只报告差距，不改文件。先看清单再决定要不要动手。"""
    ensure_dirs()
    from .format import WRITABLE_RULE_IDS, evaluate, inspect_docx, load_default_ruleset
    from .parsing.docx_reader import DocxReadError, read_docx

    target = await _format_target(file, path)
    try:
        facts = read_docx(target)
    except DocxReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    issues = evaluate(facts, load_default_ruleset())
    writable = {f"format.{r}" for r in WRITABLE_RULE_IDS}
    kinds: dict[str, int] = {}
    for i in issues:
        kinds[i.kind] = kinds.get(i.kind, 0) + 1
    return {
        "ok": True,
        "file": str(target),
        "info": inspect_docx(target),
        "total": len(issues),
        "writable": sum(1 for i in issues if i.kind in writable),
        "text_issues": sum(1 for i in issues if i.kind not in writable),
        "kinds": kinds,
        "issues": [i.as_dict() for i in issues[:80]],
    }


@app.post("/api/format/docx")
async def api_format_docx(
    file: UploadFile | None = File(None),
    path: str = "",
    like: UploadFile | None = File(None),
    like_path: str = "",
    body_font: str = "",
    latin_font: str = "",
    body_size: float | None = None,
    line_spacing: float | None = None,
    line_fixed: float | None = None,
    indent_chars: float | None = None,
    align: str = "",
    h1_font: str = "",
    h2_font: str = "",
    h1_size: float | None = None,
    h2_size: float | None = None,
    h3_size: float | None = None,
    caption_size: float | None = None,
    margin: str = "",
    check_only: bool = False,
):
    """把排版写回上传的 .docx，然后**把文件还给浏览器**。

    和其它端点不同，这里必然要传文件回去：作者要的是那份改好的 .docx。
    失败时 400/500 的 detail 里带可读原因（含"已还原备份"的说明）。
    """
    ensure_dirs()
    from .format import apply_scheme, evaluate, inspect_docx, load_default_ruleset, scheme_from_template
    from .parsing.docx_reader import DocxReadError, read_docx

    target = await _format_target(file, path)
    sch, notes = _format_kwargs(
        body_font=body_font, latin_font=latin_font, body_size=body_size,
        line_spacing=line_spacing, line_fixed=line_fixed, indent_chars=indent_chars,
        align=align, h1_font=h1_font, h2_font=h2_font, h1_size=h1_size,
        h2_size=h2_size, h3_size=h3_size, caption_size=caption_size, margin=margin,
    )

    # 模板优先：给了模板就照它改
    if like is not None and like.filename:
        tpl = await _save_upload(like, FORMAT_DIR / "_templates")
        sch, tnotes = scheme_from_template(tpl)
        notes.extend(tnotes)
    elif like_path:
        tpl = Path(like_path)
        if not tpl.exists():
            raise HTTPException(status_code=400, detail=f"模板文件不存在：{tpl}")
        sch, tnotes = scheme_from_template(tpl)
        notes.extend(tnotes)

    if check_only:
        try:
            facts = read_docx(target)
        except DocxReadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        issues = evaluate(facts, load_default_ruleset())
        return {"ok": True, "check_only": True, "total": len(issues),
                "scheme": sch.to_dict(), "issues": [i.as_dict() for i in issues[:80]]}

    # 先确认这份文件读得动，再动它：读不动就 400 说清原因，别等改完才发现。
    # 这一句原先没包 try，坏文件（改名的 PDF、损坏的 ZIP）会变成 500 堆栈，
    # 作者看到的是"服务器内部错误"而不是"这不是 .docx"。
    try:
        before = len(evaluate(read_docx(target), load_default_ruleset()))
    except DocxReadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    out = target.with_name(f"{target.stem}_排版整理{target.suffix}")
    rep = apply_scheme(target, out, scheme=sch, backup=True)
    if not rep.ok:
        raise HTTPException(
            status_code=400,
            detail="排版写回失败，原文件未被改动：" + "；".join(rep.errors or rep.notes),
        )
    # 复验：改完用同一套检查器再量一遍。量不动不影响把文件交给作者，但要说清。
    after: int | None
    try:
        after = len(evaluate(read_docx(out), load_default_ruleset()))
    except Exception:  # noqa: BLE001
        after = None

    # 把报告一并放进响应头，界面不用再请求一次就能显示"改前 117 条 → 改后 69 条"。
    from urllib.parse import quote

    return FileResponse(
        str(out), filename=out.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "X-Format-Summary": quote(json.dumps({
                "before": before, "after": after,
                "body": rep.paragraphs_body, "headings": rep.paragraphs_heading,
                "captions": rep.paragraphs_caption, "runs": rep.runs_styled,
                "headings_detected": rep.headings_detected,
                "text_unchanged": rep.text_unchanged, "reopened_ok": rep.reopened_ok,
                "skipped_in_tables": rep.skipped_in_tables,
                "backup": Path(rep.backup).name if rep.backup else "",
                "notes": notes + rep.notes,
            }, ensure_ascii=False)),
        },
    )


async def _save_upload(upload: UploadFile, folder: Path) -> Path:
    """把上传的文件落盘到指定目录，返回路径。做扩展名、空文件、体积检查。"""
    name = Path(upload.filename or "upload.docx").name
    suffix = Path(name).suffix.lower()
    if suffix != ".docx":
        raise HTTPException(status_code=400, detail=f"只支持 .docx，收到 {suffix or '(无扩展名)'}")
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / name
    if dest.exists():
        dest = folder / f"{Path(name).stem}_{int(time.time())}{suffix}"
    raw = await upload.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(raw) > 60 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="文件超过 60MB，请先精简图片后重试。")
    dest.write_bytes(raw)
    return dest


async def _format_target(file: UploadFile | None, path: str) -> Path:
    """排版整理要处理的文件：上传优先，否则用路径。

    路径版**只允许 data/ 目录内**的文件。别的端点（如开题解析）传路径只是读出来
    看一眼，而这个端点会把整个文件回传——不设限就等于"通过服务下载磁盘上任意
    文件"。要处理别处的文件，请用上传。
    """
    if file is not None and file.filename:
        return await _save_upload(file, FORMAT_DIR)
    if path:
        try:
            target = Path(path).resolve()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"路径无法解析：{path}") from exc
        try:
            target.relative_to(DATA_DIR.resolve())
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"出于安全考虑只处理 {DATA_DIR} 目录内的文件；"
                       f"其它位置请改用「上传文件」。",
            ) from exc
        if not target.exists():
            raise HTTPException(status_code=400, detail=f"文件不存在：{target}")
        if target.suffix.lower() != ".docx":
            raise HTTPException(status_code=400, detail="只支持 .docx 文件")
        return target
    raise HTTPException(status_code=400, detail="请上传一份 .docx，或给出它的路径")


# ---------------------------------------------------------------------------
# 界面
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    page = Path(__file__).parent / "static" / "index.html"
    if not page.exists():
        return HTMLResponse("<h1>界面文件缺失</h1><p>app/static/index.html 不存在。</p>", status_code=500)
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/favicon.ico")
def favicon():
    return JSONResponse({}, status_code=204)


def main() -> None:
    import uvicorn

    ensure_dirs()
    s = load_settings()
    uvicorn.run(app, host=s.host, port=s.port, log_level="info")


if __name__ == "__main__":
    main()
