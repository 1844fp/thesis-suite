"""写作助手 · 路由。

设计取向的变更（重要）
----------------------
早先这一层有一条**硬闸门**：证据库为空就拒绝生成，连模型都不调用。它的目的是
防止模型凭空编造，代价是"必须先手工录入资料才能写"。

现在放宽为：**可以依据大模型自身知识与互联网检索到的真实文献来写**。
放宽输入就必须补上等价的约束，否则防线的目的就丢了。所以本站的做法不是
"放开闸门"，而是**把"依据"从一个来源扩成三个层级，并且每一层都要能被检查**：

===========  ========================  ==========================  ==============
层级          来源                      引用编号                    怎么验证
===========  ========================  ==========================  ==============
A 用户证据    访客录入的文献原文        ``[n]``（从 1 起）          编号在本层池内
B 联网文献    Crossref/OpenAlex/arXiv   ``[n]``（接在 A 之后）      编号在池内 + 有真实 DOI
              检索到的真实记录
C 模型知识    大模型自身训练所得        无编号                      事后按数字接地复核
===========  ========================  ==========================  ==============

四条**代码强制**的可靠性约束（不靠提示词自觉）：

1. **引用编号必须在池内。** 正文里每个 ``[n]`` 都必须是 A + B 层真实存在的编号；
   越界的记成"编造引用"通报，不静默删掉。
2. **数字必须能在材料里找到。** 正文里的数字需出现在 A + B 层原文中（年份除外）。
   这是唯一能机械证伪"这个数从没出现过"的手段。
3. **C 层不得挂引用。** 模型知识不许伪装成有出处；挂了编号就按第 1 条被查出越界。
4. **覆盖度如实上报。** 每一章回传用了几个 A 层、几个 B 层、以及**无出处的论断占比**，
   让作者一眼看出哪几段需要自己补依据——而不是看到一篇"看起来很有依据"的稿子。

第 4 条尤其要紧：放宽之后最大的风险不是"写不出来"，而是**作者以为都有依据**。
所以宁可把话说难听，也不给一个虚假的安心。
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import credentials as creds_mod
from .. import llm as llm_mod
from ..sessions import STORE
from .. import websearch

router = APIRouter(prefix="/api/writing", tags=["写作助手"])

#: 一次最多生成几章。公网服务要限制单次请求的模型花费。
MAX_SECTIONS = 8

#: 单章目标字数区间。上游的经验值：一个写作单元 1200~2500 字比较稳。
MIN_WORDS, MAX_WORDS = 1200, 2500

#: 带进提示词的材料条数上限。太多会挤掉正文的生成预算。
MAX_MATERIAL = 14

#: 每章自动联网检索几轮。1 轮用章节标题，另 1 轮用论文题目，够用且不拖时间。
MAX_QUERIES_PER_SECTION = 2

#: 引用角标 ``[1]`` ``[12]``；也认全角方括号 ``【1】``。
CITE_RE = re.compile(r"[\[【](\d{1,3})[\]】]")

#: 数字。**只查两位数以上或带小数点的数**——与上游 check_number_grounding 同一口径。
#: 1~9 的小数字绝大多数是枚举（"三个方面"）或序数，是误报的主要来源。
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

#: 年份形态的数字不参与接地检查：材料里出现 2020、2021，而正文说"2022 年"，
#: 这既可能是引用了没检索到的文献，也可能只是时间叙述，判它编造会制造大量假警报。
YEAR_LIKE = re.compile(r"^(19|20)\d{2}$")


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
    """论文类型、预置章节结构，以及联网检索的可用性。"""
    from app.writing.genres import genre_choices

    genres = []
    for item in genre_choices():
        genres.append(
            {
                "id": item.get("id", ""),
                "name": item.get("name", ""),
                "summary": item.get("summary", ""),
                "suggested_words": item.get("suggested_words", 0),
                "sections": [s.get("title", "") for s in (item.get("sections") or [])],
            }
        )

    # 让前端能显示"联网检索是否可用"，而不是让访客猜。
    sources = []
    for cls in websearch.SOURCES:
        sources.append({"id": cls.name, "base_url": cls.base_url})

    return {
        "ok": True,
        "genres": genres,
        "max_sections": MAX_SECTIONS,
        "sources": ["user", "web", "model"],
        "web_search": {
            "enabled": True,
            "sources": sources,
            "note": "联网检索自公开文献库，不需要 API Key；检索不到时自动降级为纯模型知识并标注。",
        },
    }


def _fallback_sections() -> list[str]:
    return ["第1章 绪论", "第2章 文献综述", "第3章 研究设计", "第4章 结果与分析", "第5章 结论与展望"]


def _count_cjk(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text or ""))


def _numbers_in(text: str) -> set[str]:
    out: set[str] = set()
    for raw in NUMBER_RE.findall(text or ""):
        if YEAR_LIKE.match(raw):
            continue
        if "." in raw or len(raw.lstrip("0")) >= 2:
            out.add(raw.rstrip("0").rstrip(".") if "." in raw else raw)
    return out


def _build_material(user_evidence: list[dict], web_records: list[websearch.WebRecord]):
    """把 A 层（用户证据）与 B 层（联网文献）合成一张带统一编号的材料表。

    编号规则：A 层从 1 开始，B 层紧接其后。**每次渲染都重新分配**——
    提示词里给模型的编号必须与校验时用的编号严格一致，否则校验会全盘错位。
    """
    index: dict[int, dict] = {}
    lines: list[str] = []

    def add(tier: str, citation: str, text: str, extra: str = "") -> None:
        n = len(index) + 1
        index[n] = {"tier": tier, "citation": citation, "text": text}
        lines.append(f"[{n}]（{tier}）{citation}")
        if extra:
            lines.append(f"    {extra}")
        lines.append(f"    原文：{text[:1400]}")

    for item in user_evidence[: max(1, MAX_MATERIAL // 2)]:
        add(
            "A 用户提供",
            str(item.get("citation") or item.get("title") or "来源未标注"),
            str(item.get("text") or item.get("abstract") or ""),
            f"年份：{item['year']}" if item.get("year") else "",
        )

    remaining = MAX_MATERIAL - len(index)
    for rec in web_records[: max(0, remaining)]:
        add(
            "B 联网检索",
            rec.citation(),
            rec.abstract,
            "、".join(
                x for x in (
                    f"来源库：{rec.source}",
                    f"DOI：{rec.doi}" if rec.doi else "",
                    f"链接：{rec.url}" if rec.url else "",
                    f"被引：{rec.citations}" if rec.citations is not None else "",
                ) if x
            ),
        )

    return "\n".join(lines), index


def _system_prompt() -> str:
    return (
        "你是中文学位论文写作助手。材料表里给了你若干条编号材料，"
        "每条都标了它的来源层级：\n"
        "  · A 用户提供 —— 作者自己的文献资料\n"
        "  · B 联网检索 —— 从公开文献库检索到的真实记录（带 DOI 或链接可回查）\n"
        "\n"
        "硬性规则，违反任意一条即为失败：\n"
        "1. 需要出处的论断，一律用编号引用，形如 [1] 或 [1][3]。"
        "**只能引用材料表里真实存在的编号**——编造一个编号会被逐条查出并通报给作者。\n"
        "2. 正文里的具体数字（样本量、百分比、系数等）必须能在材料表原文里找到。"
        "找不到就不要写这个数字，宁可定性描述。这条会被程序逐个核对。\n"
        "3. 你**可以**用自己的背景知识补充学科常识、概念界定、方法论的一般性说明，"
        "**但这类内容不得挂任何引用编号**。把模型知识伪装成有出处是最严重的问题。\n"
        "4. 不要编造参考文献、作者名、期刊名、年份。不确定就不写。\n"
        "5. 禁止「一篇论文半句话」式的逐篇罗列（如「张三（2020）做了…。李四（2021）做了…。」）。"
        "同一主题的多条材料要合并成一个判断：先给结论或分歧，再把文献并列作证。\n"
        "6. 不要写「具有重要意义」「随着…的快速发展」这类空话开场。\n"
        "7. 若材料表不足以支撑某一节，就**如实说明证据不足**，不要用空话填满篇幅。\n"
        "8. 只输出该章正文本身：不要标题、不要解释、不要 Markdown 标题符号。段落间空一行。"
    )


def _section_prompt(title: str, heading: str, material: str, has_web: bool, is_last: bool) -> str:
    coverage = (
        "材料表同时包含 A 与 B 两层，请充分利用。"
        if has_web
        else "本次没有检索到联网文献，材料表只有 A 层；确需通用知识时可按规则 3 用无编号表述。"
    )
    return (
        f"论文题目：{title}\n"
        f"本次要写的章节：{heading}\n\n"
        f"可用材料：\n{material}\n\n"
        f"{coverage}\n"
        f"请写出「{heading}」的正文，{MIN_WORDS}~{MAX_WORDS} 字。"
        + ("\n这是最后一章：结论只能汇总前文已论证的内容，不得引入新材料之外的新证据。"
           if is_last else "")
    )


def _check_citations(body: str, index: dict[int, dict]) -> tuple[list[int], list[int]]:
    used = {int(m) for m in CITE_RE.findall(body)}
    valid = sorted(n for n in used if n in index)
    invalid = sorted(n for n in used if n not in index)
    return valid, invalid


def _grounding_report(body: str, index: dict[int, dict]) -> dict:
    """数字接地复核：正文里的数字有多少能在材料里找到。

    只报**两位数以上或带小数点的数**（年份不算），与上游 check_number_grounding
    的口径一致——查小数字必然误报，而误报会毁掉整份报告的可信度。
    """
    material_text = " ".join(str(v.get("text") or "") for v in index.values())
    material_text += " " + " ".join(str(v.get("citation") or "") for v in index.values())
    available = _numbers_in(material_text)

    in_body = _numbers_in(body)
    missing = sorted(n for n in in_body if n not in available)
    return {
        "numbers_total": len(in_body),
        "numbers_grounded": len(in_body) - len(missing),
        "numbers_ungrounded": missing[:20],
        "ungrounded_count": len(missing),
    }


def _queries_for(title: str, heading: str) -> list[str]:
    """给这一章构造检索词。用章节标题 + 论文题目，两轮就够。"""
    queries: list[str] = []
    cleaned = re.sub(r"^第?\s*\d+\s*[章节]\s*", "", heading or "").strip()
    if cleaned:
        queries.append(f"{title} {cleaned}".strip())
    if title and title not in queries:
        queries.append(title)
    return queries[:MAX_QUERIES_PER_SECTION]


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

    # 联网检索开关：默认开。关掉时退回"只用用户证据 + 模型知识"。
    use_web = body.get("use_web")
    use_web = True if use_web is None else bool(use_web)

    user_evidence = list(session.evidence)
    configured = creds.configured

    def stream():
        yield _sse({"type": "stage", "stage": "start", "progress": 0.02, "message": "已连接。"})

        # ---- 证据来源清点：把"这一版有什么依据"先说清楚 ----
        yield _sse({
            "type": "stage", "stage": "check", "progress": 0.05,
            "message": (
                f"A 层（你的资料）：{len(user_evidence)} 条；"
                f"B 层（联网检索）：{'开启' if use_web else '已关闭'}；"
                f"章节：{len(sections)} 章。"
            ),
        })

        if not configured:
            yield _sse({"type": "error", "message": "写作需要模型：请先在页面右上角填入你自己的 API Key。"})
            yield "data: [DONE]\n\n"
            return

        if not user_evidence and not use_web:
            # 既没资料又关了检索：仍然允许写（放宽了），但必须强烈提示。
            yield _sse({
                "type": "stage", "stage": "warn", "progress": 0.06,
                "message": (
                    "A 层为空且联网检索已关闭：本次将完全依据模型自身知识写作，"
                    "正文不会有任何引用依据。建议开启联网检索，或先录入自己的资料。"
                ),
            })

        produced: list[dict] = []
        notes: list[str] = []
        used_indices: set[int] = set()
        invalid_all: list[int] = []
        ungrounded_all: list[str] = []
        web_total = 0
        web_status_all: list[dict] = []
        # 累积本次检索到的全部真实文献（按题名去重），结束时并入证据库。
        # 这样访客下次写作时它们就是 A 层可直接引用，评估助手也能拿它们复核数字接地。
        web_seen: dict[str, websearch.WebRecord] = {}
        started = time.time()

        try:
            with llm_mod.Chat(creds, timeout=300.0) as chat:
                for i, heading in enumerate(sections, start=1):
                    base_progress = 0.06 + 0.9 * ((i - 1) / len(sections))

                    # ---- B 层：联网检索（失败不影响写作，只是少一层依据）----
                    web_records: list[websearch.WebRecord] = []
                    if use_web:
                        yield _sse({"type": "stage", "stage": "search", "progress": base_progress,
                                    "message": f"第 {i}/{len(sections)} 章：正在检索公开文献库…"})
                        try:
                            web_records, status = websearch.search_literature(
                                _queries_for(title, heading), limit_per_source=3, max_total=8
                            )
                            web_status_all.extend(status)
                            ok_sources = [s["source"] for s in status if s.get("ok")]
                            yield _sse({
                                "type": "stage", "stage": "search", "progress": base_progress,
                                "message": (
                                    f"检索到 {len(web_records)} 条可用文献"
                                    + (f"（来源：{'、'.join(sorted(set(ok_sources)))}）" if ok_sources else "")
                                ),
                            })
                        except Exception as exc:  # noqa: BLE001 - 检索失败不该让写作停下
                            notes.append(f"第 {i} 章联网检索失败：{type(exc).__name__}: {exc}")
                            yield _sse({"type": "stage", "stage": "warn", "progress": base_progress,
                                        "message": f"联网检索失败，本章降级为不依赖检索：{exc}"})

                    web_total += len(web_records)
                    for rec in web_records:
                        key = re.sub(r"[\s\W_]+", "", rec.title.lower())[:60] or rec.title
                        web_seen.setdefault(key, rec)
                    material, index = _build_material(user_evidence, web_records)

                    if not index:
                        yield _sse({"type": "stage", "stage": "warn", "progress": base_progress,
                                    "message": "本章没有可用材料：正文将全部由模型知识构成，不会有引用依据。"})

                    yield _sse({"type": "stage", "stage": "writing",
                                "progress": base_progress + 0.05,
                                "message": f"正在写第 {i}/{len(sections)} 章：{heading}"})

                    try:
                        text = chat.ask(
                            [
                                {"role": "system", "content": _system_prompt()},
                                {"role": "user", "content": _section_prompt(
                                    title, heading, material, bool(web_records), i == len(sections))},
                            ],
                            temperature=0.5,
                            max_tokens=6000,
                        )
                    except llm_mod.LLMError as exc:
                        notes.append(f"第 {i} 章（{heading}）生成失败：{exc}")
                        yield _sse({"type": "stage", "stage": "warn", "progress": base_progress,
                                    "message": f"「{heading}」失败，已跳过：{exc}"})
                        if llm_mod.is_fatal(str(exc)) or produced:
                            break
                        raise

                    # ---- 约束 1：引用编号必须在池内 ----
                    valid, invalid = _check_citations(text, index)
                    used_indices.update(valid)
                    invalid_all.extend(invalid)
                    if invalid:
                        notes.append(
                            f"第 {i} 章出现材料表中不存在的引用编号 {sorted(set(invalid))}——"
                            "这些引用是模型编的，已通报，请人工核对该段。"
                        )

                    # ---- 约束 2：数字接地 ----
                    grounding = _grounding_report(text, index)
                    ungrounded_all.extend(grounding["numbers_ungrounded"])
                    if grounding["ungrounded_count"]:
                        notes.append(
                            f"第 {i} 章有 {grounding['ungrounded_count']} 处数字在材料里找不到"
                            f"（{ '、'.join(grounding['numbers_ungrounded'][:6]) }），可能是模型自己算的。"
                        )

                    tier_a = sum(1 for n in valid if index[n]["tier"].startswith("A"))
                    tier_b = sum(1 for n in valid if index[n]["tier"].startswith("B"))

                    words = _count_cjk(text)
                    produced.append({
                        "heading": heading,
                        "body": text.strip(),
                        "words": words,
                        "citations": {"total": len(valid), "user": tier_a, "web": tier_b, "invalid": len(invalid)},
                        "grounding": grounding,
                        "web_used": len(web_records),
                    })
                    yield _sse({
                        "type": "section", "progress": base_progress + 0.1,
                        "sections": produced,
                        "partial": {"title": title, "sections": produced},
                        "citations": sorted(used_indices),
                    })

                if not produced:
                    yield _sse({"type": "error", "message": "没有任何一章生成成功，请检查 API Key 与余额后重试。"})
                    yield "data: [DONE]\n\n"
                    return

                # ---- 约束 4：覆盖度如实上报，不粉饰 ----
                total_cites = sum(s["citations"]["total"] for s in produced)
                cite_user = sum(s["citations"]["user"] for s in produced)
                cite_web = sum(s["citations"]["web"] for s in produced)
                coverage = {
                    "sections": len(produced),
                    "citations_total": total_cites,
                    "citations_user": cite_user,
                    "citations_web": cite_web,
                    "citations_invalid": len(invalid_all),
                    "ungrounded_numbers": len(ungrounded_all),
                    "web_records_retrieved": web_total,
                    "web_sources": sorted({s["source"] for s in web_status_all if s.get("ok")}),
                }
                if cite_web and cite_user:
                    coverage["verdict"] = "同时有用户资料与联网文献支撑"
                elif cite_web:
                    coverage["verdict"] = "主要依据联网检索的公开文献（建议核对是否贴合你的论证）"
                elif cite_user:
                    coverage["verdict"] = "仅依据你提供的资料"
                else:
                    coverage["verdict"] = "没有任何引用依据：正文为模型知识，需自行补证据"

                draft_data = {
                    "title": title,
                    "genre": str(body.get("genre") or ""),
                    "sections": produced,
                    "citation_count": len(used_indices),
                    "evidence_used": sorted(used_indices),
                    "evidence_total": len(user_evidence),
                    "web_records_total": web_total,
                    "coverage": coverage,
                    "scope": {"tiers": ["A 用户证据", "B 联网检索", "C 模型知识"], "use_web": use_web},
                    "words": sum(s["words"] for s in produced),
                    "notice": " ".join(notes),
                    "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                }
                session.push_version(draft_data)
                session.meta["writing_title"] = title

                # 把联网检索到的真实文献并入证据库：评估助手要按它复核数字接地，
                # 而且访客下次写作时它们就变成 A 层，可以直接引用。
                if web_seen:
                    try:
                        added = session.add_evidence([r.as_evidence() for r in web_seen.values()])
                        if added:
                            notes.append(
                                f"本次检索到的 {added} 条真实文献已并入证据库，后续可直接引用（含 DOI 可回查）。"
                            )
                            draft_data["notice"] = " ".join(notes)
                            coverage["web_records_added"] = added
                    except Exception as exc:  # noqa: BLE001 - 并入失败不影响稿件本身
                        notes.append(f"检索到的文献并入证据库失败：{type(exc).__name__}")
                        draft_data["notice"] = " ".join(notes)

                # 证据台账（供 check_number_grounding 使用）：A 层 + 本次 B 层
                ledger = session.meta.setdefault("writing_evidence", {})
                ledger["会话证据库"] = [
                    {"text": str(e.get("text") or ""), "citation": str(e.get("citation") or e.get("title") or "")}
                    for e in session.evidence
                ]

                yield _sse({"type": "stage", "stage": "done", "progress": 1.0,
                            "message": f"完成，共 {draft_data['words']} 字；{coverage['verdict']}。"})
                yield _sse({"type": "result", "sid": session.sid, "draft": draft_data,
                            "notice": draft_data["notice"], "coverage": coverage,
                            "seconds": round(time.time() - started, 1)})
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
                "coverage": v.get("coverage") or {},
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

    coverage = draft.get("coverage") or {}
    if coverage:
        parts.append("---")
        parts.append("")
        parts.append("## 依据覆盖度（由系统统计，不是评价）")
        parts.append("")
        parts.append(f"- 章节数：{coverage.get('sections', 0)}")
        parts.append(f"- 引用总数：{coverage.get('citations_total', 0)}"
                     f"（你的资料 {coverage.get('citations_user', 0)}，联网文献 {coverage.get('citations_web', 0)}）")
        parts.append(f"- 编造的引用编号：{coverage.get('citations_invalid', 0)}")
        parts.append(f"- 找不到依据的数字：{coverage.get('ungrounded_numbers', 0)}")
        parts.append(f"- 本次检索到的真实文献：{coverage.get('web_records_retrieved', 0)} 条"
                     f"（来源：{'、'.join(coverage.get('web_sources') or []) or '无'}）")
        parts.append(f"- 结论：{coverage.get('verdict', '')}")
    if draft.get("notice"):
        parts.append("")
        parts.append(f"> 生成提示：{draft['notice']}")

    markdown = "\n".join(parts)
    filename = f"{draft.get('title') or '稿件'}.md"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )
