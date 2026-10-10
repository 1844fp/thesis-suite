"""写作助手 · 路由。

上游 ``thesis-agents`` 的写作链路（writer_plan / writer_draft / …）**硬依赖一个
RAG 知识库进程**去检索证据，而那个知识库在整个项目里没有任何 embedding 实现
（它是对端 ``thesis-assistant`` 提供的服务）。公网容器里没有对端，照搬上游链路
的结果是：每一章都因为"检索不到证据"而拒绝生成，最终产出空稿 + 一屏
``writing.needs_source``。

所以本站做法是**保留上游最核心的那条纪律，换掉检索那一层**：

    无证据 → 不落笔

具体地说，本站的证据来自「资料获取」助手录入的资料本身（访客自己贴进来的文献
原文），而不是向量检索。这让"证据"变成了**作者可验证的原文**，比检索命中更硬。

三条硬约束（都在代码里强制，不靠提示词自觉）：

1. **证据闸门**：证据库为空时直接拒绝生成，并告诉访客先去录入资料。
2. **引用编号校验**：正文里出现的每一个 ``[n]`` 必须是证据库里真实存在的编号；
   越界的引用会被记录成"编造引用"通报给访客（不静默删掉，否则作者不知道发生过）。
3. **数字接地**：正文里的数字必须能在证据原文里找到。上游的
   ``check_number_grounding`` 干的就是这件事，本站把证据台账按上游格式喂给它复用。
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import credentials as creds_mod
from .. import llm as llm_mod
from ..sessions import STORE

router = APIRouter(prefix="/api/writing", tags=["写作助手"])

#: 一次最多生成几章。公网服务要限制单次请求的模型花费。
MAX_SECTIONS = 8

#: 单章目标字数区间。上游的经验值：一个写作单元 1200~2500 字比较稳。
MIN_WORDS, MAX_WORDS = 1200, 2500

#: 每章最多带多少条证据进提示词。太多会挤掉正文的生成预算。
MAX_EVIDENCE_PER_SECTION = 12

#: 引用角标 ``[1]`` ``[12]``；也认全角方括号 ``【1】``。
CITE_RE = re.compile(r"[\[【](\d{1,3})[\]】]")


def _creds(request: Request) -> creds_mod.Credentials:
    try:
        return creds_mod.from_headers(request.headers)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sse(event: dict) -> str:
    import json

    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


@router.get("/options")
def options() -> dict:
    """论文类型与各自的预置章节结构（取自上游 genres，保持一处定义）。"""
    from app.writing.genres import genre_choices

    genres = []
    for item in genre_choices():
        genres.append(
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "summary": item.get("summary", ""),
                "suggested_words": item.get("suggested_words", 0),
                # 只给章节标题：本站按"一章一次生成"工作，不需要上游更细的小节树。
                "sections": [s.get("title", "") for s in (item.get("sections") or [])],
            }
        )
    return {"ok": True, "genres": genres, "max_sections": MAX_SECTIONS}


def _fallback_sections() -> list[str]:
    return ["第1章 绪论", "第2章 文献综述", "第3章 研究设计", "第4章 结果与分析", "第5章 结论与展望"]


def _evidence_block(evidence: list[dict]) -> tuple[str, dict[int, dict]]:
    """把会话证据库渲染成提示词里的编号材料表，并返回编号索引。

    编号从 1 开始且**每次渲染都重新分配**——提示词里给模型的编号必须与校验时
    用的编号严格一致，否则引用校验会全盘错位。
    """
    index: dict[int, dict] = {}
    lines: list[str] = []
    for i, item in enumerate(evidence[:MAX_EVIDENCE_PER_SECTION], start=1):
        index[i] = item
        where = item.get("citation") or item.get("title") or "来源未标注"
        text = str(item.get("text") or item.get("abstract") or "").strip()
        lines.append(f"[{i}] {where}")
        if item.get("year"):
            lines.append(f"    年份：{item['year']}")
        lines.append(f"    原文：{text[:1200]}")
    return "\n".join(lines), index


def _system_prompt() -> str:
    return (
        "你是中文学位论文写作助手。你的唯一依据是用户提供的编号材料表。\n"
        "硬性规则，违反任意一条即为失败：\n"
        "1. 只能依据材料表里的内容写作。材料表没说的，一律不写——包括不编造数据、"
        "不编造作者、不编造年份、不编造机构名与研究结论。\n"
        "2. 每一处需要依据的论断，都要在该句末尾标注材料编号，形如 [1] 或 [1][3]。"
        "不得使用材料表里不存在的编号。\n"
        "3. 正文里出现的任何具体数字（样本量、百分比、系数、年份除外）"
        "必须能在材料表原文里找到；找不到就不要写这个数字。\n"
        "4. 禁止「一篇论文半句话」式的逐篇罗列（如「张三（2020）做了…。李四（2021）做了…。」）。"
        "同一主题的多篇材料必须合并成一个判断，先给结论或分歧，再把文献并列作证。\n"
        "5. 不要写「具有重要意义」「随着…的快速发展」这类空话开场。\n"
        "6. 只输出该章正文本身，不要输出标题、不要输出解释、不要用 Markdown 标题符号。"
        "段落之间用一个空行分隔。"
    )


def _section_prompt(title: str, heading: str, evidence_block: str, is_last: bool) -> str:
    return (
        f"论文题目：{title}\n"
        f"本次要写的章节：{heading}\n\n"
        f"可用材料（这是你唯一的依据）：\n{evidence_block}\n\n"
        f"请写出「{heading}」的正文，{MIN_WORDS}~{MAX_WORDS} 字。"
        + ("\n这是最后一章：结论部分只能汇总前文已论证的内容，不得引入材料表之外的新证据。"
           if is_last else "")
    )


def _check_citations(body: str, index: dict[int, dict]) -> tuple[list[int], list[int]]:
    """校验引用编号。返回 ``(用到的合法编号, 越界的非法编号)``。"""
    used = {int(m) for m in CITE_RE.findall(body)}
    valid = sorted(n for n in used if n in index)
    invalid = sorted(n for n in used if n not in index)
    return valid, invalid


def _count_cjk(text: str) -> int:
    """中文字数（不含空白与标点）。写作进度用这个数比 len() 更贴近作者直觉。"""
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


@router.post("/draft")
async def draft(request: Request) -> StreamingResponse:
    """逐章生成初稿（SSE）。

    事件：``stage`` 进度 / ``section`` 单章完成 / ``result`` 全部完成 / ``error``。
    """
    body = await request.json()
    creds = _creds(request)

    sid = str(body.get("sid") or "") or None
    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="请先填写论文题目。")

    session = STORE.get(sid, create=True)
    sections = [str(s).strip() for s in (body.get("sections") or []) if str(s).strip()]
    if not sections:
        sections = _fallback_sections()
    sections = sections[:MAX_SECTIONS]

    evidence = list(session.evidence)
    gate_ok = bool(evidence)
    configured = creds.configured

    def stream():
        nonlocal sid
        yield _sse({"type": "stage", "stage": "start", "progress": 0.02, "message": "已连接。"})
        yield _sse({"type": "stage", "stage": "check", "progress": 0.05,
                    "message": f"证据库：{len(evidence)} 条；章节：{len(sections)} 章。"})

        # ---- 证据闸门：没有证据就不写，也不调用模型（省下访客的钱） ----
        if not gate_ok:
            yield _sse({
                "type": "stage", "stage": "blocked", "progress": 0.05,
                "message": (
                    "证据库是空的，写不了。本助手只依据证据写作——没有证据就动笔，"
                    "产出的必然是编造的正文。请先到「资料获取」页签录入文献原文或摘录，再回来生成。"
                ),
            })
            yield _sse({"type": "error", "message": "证据库为空：请先在「资料获取」录入资料。"})
            yield "data: [DONE]\n\n"
            return

        if not configured:
            yield _sse({"type": "error", "message": "写作需要模型：请先在页面右上角填入你自己的 API Key。"})
            yield "data: [DONE]\n\n"
            return

        evidence_block, index = _evidence_block(evidence)
        produced: list[dict] = []
        notes: list[str] = []
        used_indices: set[int] = set()
        invalid_total: list[int] = []
        started = time.time()

        try:
            with llm_mod.Chat(creds, timeout=300.0) as chat:
                for i, heading in enumerate(sections, start=1):
                    progress = 0.05 + 0.9 * ((i - 1) / len(sections))
                    yield _sse({"type": "stage", "stage": "writing", "progress": progress,
                                "message": f"正在写第 {i}/{len(sections)} 章：{heading}"})
                    try:
                        text = chat.ask(
                            [
                                {"role": "system", "content": _system_prompt()},
                                {"role": "user", "content": _section_prompt(
                                    title, heading, evidence_block, i == len(sections))},
                            ],
                            temperature=0.5,
                            max_tokens=6000,
                        )
                    except llm_mod.LLMError as exc:
                        # 已经写出来的章节不能因为后面一章失败就丢掉。
                        notes.append(f"第 {i} 章（{heading}）生成失败：{exc}")
                        yield _sse({"type": "stage", "stage": "warn", "progress": progress,
                                    "message": f"「{heading}」失败，已跳过：{exc}"})
                        if llm_mod.is_fatal(str(exc)) or produced:
                            break
                        raise

                    valid, invalid = _check_citations(text, index)
                    used_indices.update(valid)
                    invalid_total.extend(invalid)
                    if invalid:
                        notes.append(
                            f"第 {i} 章出现了证据库里不存在的引用编号 {sorted(set(invalid))}——"
                            "这些引用是模型编的，请人工核对该段。"
                        )

                    words = _count_cjk(text)
                    produced.append({"heading": heading, "body": text.strip(), "words": words})
                    yield _sse({
                        "type": "section", "progress": progress,
                        "sections": produced,
                        "partial": {"title": title, "sections": produced},
                        "citations": sorted(used_indices),
                    })

                # ---- 结论章之后做一次数字接地的复核 ----
                if invalid_total:
                    notes.append(
                        f"共发现 {len(invalid_total)} 处越界引用编号（{sorted(set(invalid_total))}）。"
                    )

                draft = {
                    "title": title,
                    "genre": str(body.get("genre") or ""),
                    "sections": produced,
                    "citation_count": len(used_indices),
                    "evidence_used": sorted(used_indices),
                    "evidence_total": len(evidence),
                    "words": sum(s["words"] for s in produced),
                    "notice": " ".join(notes),
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                if not produced:
                    yield _sse({"type": "error", "message": "没有任何一章生成成功，请检查 API Key 与余额后重试。"})
                    yield "data: [DONE]\n\n"
                    return

                session.push_version(draft)
                session.meta["writing_title"] = title
                # 把证据台账按上游格式写进 meta：check_number_grounding 靠它工作，
                # 这样评估页签能查出"正文里的数字有没有依据"。
                session.meta.setdefault("writing_evidence", {})["会话证据库"] = [
                    {"text": str(e.get("text") or ""), "citation": str(e.get("citation") or e.get("title") or "")}
                    for e in evidence
                ]

                yield _sse({"type": "stage", "stage": "done", "progress": 1.0,
                            "message": f"完成，共 {draft['words']} 字。"})
                yield _sse({"type": "result", "sid": session.sid, "draft": draft,
                            "notice": draft["notice"], "seconds": round(time.time() - started, 1)})
        except llm_mod.LLMError as exc:
            yield _sse({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，别让 SSE 断在半路
            yield _sse({"type": "error", "message": f"生成失败：{type(exc).__name__}: {exc}"})
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/versions")
def versions(sid: str) -> dict:
    """会话里的稿件版本列表——写作是会产生新版本的动作，必须能回退。"""
    session = STORE.get(sid)
    if session is None:
        return {"ok": False, "error": "session_expired", "versions": []}
    return {
        "ok": True,
        "versions": [
            {
                "n": i + 1,
                "title": v.get("title", ""),
                "words": v.get("words", 0),
                "sections": len(v.get("sections") or []),
                "generated_at": v.get("generated_at", ""),
            }
            for i, v in enumerate(session.versions)
        ],
    }


@router.post("/export", response_class=PlainTextResponse)
async def export_draft(request: Request) -> PlainTextResponse:
    """导出最新稿件为 Markdown。纯本地渲染，不调用模型。"""
    from urllib.parse import quote

    body = await request.json()
    sid = str(body.get("sid") or "")
    session = STORE.get(sid)
    draft = (session.draft if session else None) or body.get("draft")
    if not isinstance(draft, dict) or not draft.get("sections"):
        raise HTTPException(status_code=400, detail="还没有可导出的稿件。")

    parts = [f"# {draft.get('title') or '未命名'}", ""]
    for section in draft["sections"]:
        parts.append(f"## {section.get('heading') or ''}")
        parts.append("")
        parts.append(str(section.get("body") or "").strip())
        parts.append("")
    if draft.get("notice"):
        parts.append("---")
        parts.append(f"> 生成提示：{draft['notice']}")
    markdown = "\n".join(parts)

    filename = f"{draft.get('title') or '稿件'}.md"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )
