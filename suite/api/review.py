"""评估助手 · 路由。

上游是 ``thesis-agents`` 的全套检查器（``app/checks`` 六个模块 + 写作质量 40 余条），
它们全部是**纯函数、不联网、不调用模型**（``needs_llm=False``），因此这一块
即使访客不填 API Key 也能跑出真实结果——这是本站最"值钱且零成本"的能力。

三段：

* ``/check``  跑全部本地检查器 → 问题清单 + 评分 + 意见书（**不需要 Key**）
* ``/verify`` 参考文献真伪核验，走 Crossref/OpenAlex（**不需要 Key**）
* ``/review`` 在检查结果之上再让模型写一段审稿意见（**需要 Key**）

评分不是模型打的印象分：``build_review()`` 用问题数量与严重度**机械推导**分数与
"是否建议提交"。模型只负责写评语，不允许改分——这是上游刻意的设计，本站沿用。
"""

from __future__ import annotations

import os
import re
import time
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import credentials as creds_mod
from .. import llm as llm_mod
from ..sessions import STORE

router = APIRouter(prefix="/api/review", tags=["评估助手"])

#: 一次提交的正文上限（字符）。公网服务要挡住超大请求。
MAX_TEXT = 400_000

#: 参考文献核验一次最多查多少条（Crossref 是公益服务，别把人家打疼）。
MAX_LOOKUPS = 30


def _text_of(body: dict, sid: str | None) -> str:
    text = str(body.get("text") or "").strip()
    if text:
        return normalize_headings(text)
    if sid:
        session = STORE.get(sid)
        draft = (session.draft if session else None) or None
        if draft:
            return _render_draft(draft)
    return ""


#: 中文论文里常见的章节标题写法。命中即认为"这一行是章节标题"。
_SECTION_HEADING = re.compile(
    r"^\s*#{1,6}\s*(?:第\s*[0-9一二三四五六七八九十]+\s*[章节篇]|[0-9]+(?:\.[0-9]+)*[\.、]?\s|"
    r"[一二三四五六七八九十]+[、\.]|绪论|引言|结论|参考文献|摘要|致谢|文献综述|研究方法|研究设计|"
    r"结果与分析|讨论|附录)"
)


def normalize_headings(text: str) -> str:
    """把章节标题统一到单个 ``#``，让上游的章节切分器认得出。

    上游 ``split_by_sections`` 只认 ``# 第1章 绪论`` 这一种形式：``##`` 会被当作正文。
    而「正文里的数字必须能在证据里找到」（``check_number_grounding``）这条检查是
    **先取章节、再读证据台账**的——切不出章节就直接返回空，界面上表现为"检查通过"，
    是最危险的那种静默失效（实测踩过：用 ``##`` 时该检查命中数恒为 0）。

    访客从别处粘进来的稿件标题层级五花八门，与其要求他们改格式，
    不如在入口处归一化。只改标题行的层级，不动任何正文。
    """
    out: list[str] = []
    for line in text.split("\n"):
        stripped = line.lstrip("#").strip() if line.lstrip().startswith("#") else line.strip()
        if stripped and _SECTION_HEADING.match(line if line.lstrip().startswith("#") else ("# " + stripped)):
            out.append("# " + stripped)
        else:
            out.append(line)
    return "\n".join(out)


def _render_draft(draft: dict) -> str:
    """把稿件渲染成待检文本。

    **章节标题必须用单个 ``#``。** 上游的章节切分器只认 ``# 第1章 绪论`` 这一种形式
    （``##`` 会被当成正文），而「正文里的数字必须能在证据里找到」
    （``check_number_grounding``）这条检查是**先取章节、再读证据台账**的：
    切不出章节就直接返回空，表现为"检查通过"。实测踩过一次——
    用 ``##`` 时这条最有价值的检查静默失效，命中数恒为 0。
    """
    parts = []
    if draft.get("title"):
        parts.append(f"# {draft['title']}")
    for section in draft.get("sections") or []:
        parts.append(f"# {section.get('heading') or ''}")
        parts.append(str(section.get("body") or ""))
    return "\n\n".join(p for p in parts if p.strip())


def _load_checkers() -> list[dict]:
    """导入检查器包（导入即注册）并返回注册表快照。

    上游注释里专门警告过：**忘了 import 不会报错，只会静默地一个检查器都没跑**。
    所以这里显式 import 并断言注册表非空。
    """
    import app.checks  # noqa: F401 - 导入即注册，副作用是必需的
    import app.format.checkers  # noqa: F401 - 格式检查器也注册在这里

    from app.framework.registry import VERIFIERS

    if not VERIFIERS:
        raise HTTPException(status_code=500, detail="检查器注册表为空，服务端装配有问题。")
    return list(VERIFIERS)


def _ctx_for(evidence: list[dict], meta: dict | None = None) -> SimpleNamespace:
    """给检查器造一个轻量上下文。

    大多数检查器只用 ``ctx`` 取项目级开关（可传 None），但
    ``check_number_grounding``（"正文里的数字必须能在证据里找到"）要读
    ``ctx.board.project.meta["writing_evidence"]``。这是我们最想让公网版跑起来的一条，
    所以这里把证据台账按上游的结构喂给它，让"编造量级"真的能被查出来。

    两个来源都要接上，缺一个这条检查就会**静默失效**（它读不到台账就直接
    ``return []``，表现是"检查通过"，比报错危险得多）：

    * 写作助手写入 ``session.meta["writing_evidence"]`` 的台账；
    * 资料获取助手存在 ``session.evidence`` 里的原始证据（没写过稿也能查）。
    """
    ledger: dict[str, list[dict]] = {}
    for key, entries in (meta or {}).get("writing_evidence", {}).items():
        if isinstance(entries, list) and entries:
            ledger[key] = entries
    if not ledger and evidence:
        # 上游按章存放、检查时跨章合并；公网版没有分章证据，放一个桶即可。
        ledger["会话证据库"] = [
            {"text": str(item.get("text") or item.get("abstract") or ""),
             "citation": str(item.get("citation") or item.get("title") or "")}
            for item in evidence
        ]
    project_meta = {"writing_evidence": ledger}
    project = SimpleNamespace(meta=project_meta)
    board = SimpleNamespace(project=project)
    return SimpleNamespace(board=board, meta=project_meta)


@router.get("/checkers")
def list_checkers() -> dict:
    """可用的本地检查器清单。前端用来展示"这一版到底查了什么"。"""
    items = _load_checkers()
    return {
        "ok": True,
        "total": len(items),
        "checkers": [
            {
                "name": item["name"],
                "description": item.get("description", ""),
                "kinds": list(item.get("kinds") or ()),
                "needs_llm": bool(item.get("needs_llm")),
                "cost": item.get("cost", "local"),
            }
            for item in sorted(items, key=lambda v: v["name"])
        ],
    }


@router.post("/check")
async def check(request: Request) -> dict:
    """跑全部本地检查器。不需要 API Key。"""
    body = await request.json()
    sid = str(body.get("sid") or "") or None
    text = _text_of(body, sid)
    if not text.strip():
        raise HTTPException(status_code=400, detail="没有可评估的正文：请粘贴稿件，或先生成一版草稿。")
    if len(text) > MAX_TEXT:
        raise HTTPException(status_code=413, detail=f"正文过长（上限 {MAX_TEXT} 字）。")

    from app.review import build_review, render_review

    session = STORE.get(sid) if sid else None
    evidence = list(session.evidence) if session else []
    ctx = _ctx_for(evidence, session.meta if session else None)

    checkers = _load_checkers()
    results: list[dict] = []
    issues: list = []
    started = time.time()

    for item in checkers:
        fn = item["fn"]
        try:
            found = fn(text, ctx) or []
        except Exception as exc:  # noqa: BLE001 - 单个检查器出错不该毁掉整份报告
            results.append(
                {
                    "name": item["name"],
                    "ok": False,
                    "count": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        found = list(found)
        issues.extend(found)
        results.append({"name": item["name"], "ok": True, "count": len(found), "error": ""})

    title = str(body.get("title") or "提交的稿件")
    report = build_review(issues, title=title)
    markdown = render_review(report)

    if session is not None:
        session.reviews.append(
            {
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "score": report.score,
                "recommendation": report.recommendation,
                "issue_count": len(issues),
                "kind": "check",
            }
        )

    return {
        "ok": True,
        "title": title,
        "seconds": round(time.time() - started, 2),
        "checked": len(checkers),
        "failed_checkers": [r for r in results if not r["ok"]],
        "issue_count": len(issues),
        "counts": report.counts,
        "score": report.score,
        "recommendation": report.recommendation,
        "top_fixes": report.top_fixes,
        "dimensions": {
            name: [
                {
                    "label": f.label,
                    "kind": f.kind,
                    "message": f.message,
                    "evidence": f.evidence,
                    "fix_hint": f.fix_hint,
                    "where": f.where,
                }
                for f in findings
            ]
            for name, findings in report.dimensions.items()
        },
        "markdown": markdown,
        "by_checker": sorted(results, key=lambda r: -r["count"]),
    }


@router.post("/verify")
async def verify_references(request: Request) -> dict:
    """参考文献真伪核验。走 Crossref / OpenAlex，不需要 API Key。

    这是防"AI 编造参考文献"的唯一硬手段：把条目里的 DOI/标题拿去数据库查，
    查不到就是编的。**核验不出来（inconclusive）会如实标注**，不当作通过。
    """
    body = await request.json()
    sid = str(body.get("sid") or "") or None
    text = _text_of(body, sid)
    if not text.strip():
        raise HTTPException(status_code=400, detail="没有参考文献可核验：请粘贴含参考文献表的稿件。")

    try:
        limit = max(1, min(int(body.get("limit") or 15), MAX_LOOKUPS))
    except (TypeError, ValueError):
        limit = 15

    from app.adapters.research import CrossrefAdapter, ExternalVerifier, OpenAlexAdapter
    from app.agents.references_extract import parse_entries

    entries = parse_entries(text)
    if not entries:
        raise HTTPException(
            status_code=400,
            detail="没有从文本里识别出参考文献条目（需要形如「参考文献」的章节或 [1] 编号条目）。",
        )

    # 解析器会把章节标题（如「参考文献」）也当成一条条目。把它拿去检索必然 not_found，
    # 于是报告里会多出一条"查无此文"的假警报——**误告造假比漏报严重得多**，
    # 所以这里按上游的两条判据过滤掉，并把过滤数量如实告诉访客。
    #
    # 为什么必须用 title_quality 而不能只用 lookupable：一旦标题行带上了 ``#`` 前缀
    # （本模块的标题归一化会加上），``search_title`` 就变成 ``# 参考文献``，
    # 长度跨过 LOOKUP_MIN_TITLE，``lookupable`` 反而变成 True。实测确认过这一点——
    # 只看 lookupable 会把标题行放进核验，产出一条假的"疑似编造"。
    # title_quality 是上游专门为"别把抽取误差伪装成学术不端指控"设计的信号，
    # 只有 high 档查不到才允许升级为"编造"。
    def _citable(e: object) -> bool:
        if not bool(getattr(e, "lookupable", False)):
            return False
        if str(getattr(e, "title_quality", "unusable")) != "high":
            return False
        # 兜底：一条真文献的原始条目不会短于 20 字。
        return len(str(getattr(e, "raw", "") or "")) >= 20

    lookupable = [e for e in entries if _citable(e)]
    skipped_uncitable = len(entries) - len(lookupable)
    if not lookupable:
        raise HTTPException(
            status_code=400,
            detail=(
                f"识别出 {len(entries)} 条内容，但没有一条够得上核验门槛"
                "（可能只是章节标题，或参考文献格式不完整）。请确认参考文献表格式。"
            ),
        )

    # 礼貌池邮箱要传给具体适配器（ExternalVerifier 本身只收适配器实例）。
    # 没给邮箱也能查，只是 Crossref 可能更容易限流——所以不强制。
    mailto = str(body.get("mailto") or os.environ.get("SUITE_CROSSREF_MAILTO", "")).strip()
    verifier = ExternalVerifier(
        crossref=CrossrefAdapter(mailto=mailto),
        openalex=OpenAlexAdapter(mailto=mailto),
    )
    checked: list[dict] = []
    started = time.time()

    for entry in lookupable[:limit]:
        raw = getattr(entry, "raw", "") or ""
        doi = getattr(entry, "doi", "") or ""
        title = getattr(entry, "title", "") or ""
        try:
            outcome = verifier.verify(doi=doi, title=title)
        except Exception as exc:  # noqa: BLE001 - 网络问题如实上报，不当作"编造"
            checked.append(
                {
                    "raw": raw[:300],
                    "verdict": "inconclusive",
                    "detail": f"核验请求失败：{type(exc).__name__}",
                    "best": {},
                }
            )
            continue
        checked.append(_shape_verdict(outcome, has_doi=bool(doi), raw=raw))

    tally: dict[str, int] = {}
    for item in checked:
        tally[item["verdict"]] = tally.get(item["verdict"], 0) + 1

    return {
        "ok": True,
        "total_entries": len(entries),
        "lookupable_entries": len(lookupable),
        "skipped_uncitable": skipped_uncitable,
        "checked": len(checked),
        "truncated": max(0, len(lookupable) - len(checked)),
        "tally": tally,
        "seconds": round(time.time() - started, 2),
        "items": checked,
    }


#: 按标题检索时的相似度门槛。低于它就不该给"已确认真实"的结论。
#:
#: 上游在 DOI 查不到时会**退回按标题做词级相似度检索**，而这个相似度会高得离谱：
#: 上游作者实测 `"A totally fabricated survey of nothing at all"` 以 0.93 命中了一篇
#: 真实存在的 *"Nothing At All"*。也就是说，**一条编造的标题可能被盖章成"真实"**。
#: 本站对这个功能的定位是"题录能不能对上真实记录"，一旦放行假阳性，
#: 作者就会以为自己的参考文献已经被核实过了——那比不提供这个功能更坏。
#: 所以对**没有 DOI、只能靠标题**匹配的条目，把 confirmed 降级为弱匹配。
TITLE_CONFIRM_MIN_SCORE = 0.97


def _shape_verdict(outcome: dict, *, has_doi: bool, raw: str) -> dict:
    """整理单条核验结论，必要时把过强的结论降级。"""
    verdict = str(outcome.get("verdict") or "inconclusive")
    best = outcome.get("best") or {}
    detail = str(outcome.get("detail") or outcome.get("reason") or "")

    if verdict == "confirmed" and not has_doi:
        try:
            score = float(best.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if score < TITLE_CONFIRM_MIN_SCORE:
            # 不是判它造假——只是"仅凭标题不足以确认"，如实降级。
            verdict = "weak_match"
            detail = (
                f"仅凭标题匹配（相似度 {score:.2f}），未达确认门槛 "
                f"{TITLE_CONFIRM_MIN_SCORE}。请按 DOI 或原文出处人工核对。"
            )

    return {
        "raw": raw[:300],
        "verdict": verdict,
        "detail": detail,
        "best": best,
    }


@router.post("/opinion")
async def opinion(request: Request) -> dict:
    """在检查结果之上，让模型写一段审稿意见。需要访客自带 Key。"""
    body = await request.json()
    creds = _creds(request)
    if not creds.configured:
        raise HTTPException(status_code=400, detail="生成审稿意见需要你自己的 API Key。")

    sid = str(body.get("sid") or "") or None
    text = _text_of(body, sid)
    if not text.strip():
        raise HTTPException(status_code=400, detail="没有可评审的正文。")
    # 复用本地检查结果，让模型基于"已发现的问题"写意见，而不是凭印象重判一遍。
    check_body = dict(body)
    check_body["text"] = text
    local = await check_body_to_result(check_body, sid)

    digest = _issue_digest(local)
    messages = [
        {
            "role": "system",
            "content": (
                "你是中文学位论文的盲审专家。请**只依据给定的问题清单**写审稿意见，"
                "不要臆造清单之外的问题，也不要重新打分。"
                "输出 Markdown，分三节：总体评价、主要问题（按严重度排序，逐条给出修改建议）、"
                "结论。语言克制、具体，指出问题时必须引用原句片段。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"稿件标题：{local.get('title') or '未命名'}\n"
                f"机械检查得分：{local.get('score')}（由问题数量推导，不得改动）\n"
                f"提交建议：{local.get('recommendation')}\n\n"
                f"已发现的 {local.get('issue_count')} 个问题：\n{digest}\n\n"
                f"稿件正文（节选）：\n{text[:12000]}"
            ),
        },
    ]

    with llm_mod.Chat(creds) as chat:
        try:
            markdown = chat.ask(messages, temperature=0.2, max_tokens=3000)
        except llm_mod.LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "ok": True,
        "model": creds.model,
        "score": local.get("score"),
        "recommendation": local.get("recommendation"),
        "issue_count": local.get("issue_count"),
        "markdown": markdown,
    }


async def check_body_to_result(body: dict, sid: str | None) -> dict:
    """给 ``/opinion`` 复用 ``/check`` 的逻辑，避免重复实现。"""
    from app.review import build_review

    text = str(body.get("text") or "")
    session = STORE.get(sid) if sid else None
    evidence = list(session.evidence) if session else []
    ctx = _ctx_for(evidence, session.meta if session else None)

    issues: list = []
    for item in _load_checkers():
        try:
            issues.extend(list(item["fn"](text, ctx) or []))
        except Exception:  # noqa: BLE001 - 与 /check 一致：单条失败不阻断
            continue

    report = build_review(issues, title=str(body.get("title") or "提交的稿件"))
    return {
        "title": str(body.get("title") or "提交的稿件"),
        "issue_count": len(issues),
        "score": report.score,
        "recommendation": report.recommendation,
        "report": report,
    }


def _issue_digest(local: dict) -> str:
    report = local.get("report")
    if report is None:
        return "（没有发现问题）"
    lines: list[str] = []
    for dimension, findings in report.dimensions.items():
        if not findings:
            continue
        lines.append(f"### {dimension}")
        for finding in findings[:10]:
            where = f"（位置：{finding.where}）" if getattr(finding, "where", "") else ""
            lines.append(f"- [{finding.kind}] {finding.message}{where}")
    return "\n".join(lines) or "（没有发现问题）"


@router.post("/export", response_class=PlainTextResponse)
async def export_report(request: Request) -> PlainTextResponse:
    """把意见书导出成 Markdown 文件。"""
    body = await request.json()
    markdown = str(body.get("markdown") or "").strip()
    if not markdown:
        raise HTTPException(status_code=400, detail="没有可导出的内容。")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    from urllib.parse import quote

    filename = f"审稿意见书-{stamp}.md"
    return PlainTextResponse(
        markdown,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )


def _creds(request: Request) -> creds_mod.Credentials:
    try:
        return creds_mod.from_headers(request.headers)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
