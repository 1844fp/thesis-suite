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
    """取出待检文本，并统一做两件事：归一化标题层级、摘掉论文题目。

    两个分支**都要走这一步**。早先草稿分支直接 ``return _render_draft(...)``，
    跳过了归一化——而它当时把论文题目渲染成 ``#``，于是切分器把题目当成第一章，
    真正的章节被吞掉，「数字无依据」这条检查只覆盖了题目那一行。
    分支间处理不一致是这类静默失效的常见来源。
    """
    text = str(body.get("text") or "").strip()
    if not text and sid:
        session = STORE.get(sid)
        draft = (session.draft if session else None) or None
        if draft:
            text = _render_draft(draft)
    if not text:
        return ""

    normalized = normalize_headings(text)
    without_title, _title = strip_leading_title(normalized)
    return without_title


#: 中文论文里常见的章节标题写法。命中即认为"这一行是章节标题"。
_SECTION_HEADING = re.compile(
    r"^\s*#{1,6}\s*(?:第\s*[0-9一二三四五六七八九十]+\s*[章节篇]|[0-9]+(?:\.[0-9]+)*[\.、]?\s|"
    r"[一二三四五六七八九十]+[、\.]|绪论|引言|结论|参考文献|摘要|致谢|文献综述|研究方法|研究设计|"
    r"结果与分析|讨论|附录)"
)


def normalize_headings(text: str) -> str:
    """整理标题层级，让上游的章节切分器认得出**每一章**。

    上游 ``split_by_sections`` 的行为是：**在第一个 ``#`` 行处切第一刀，但 ``##``
    不算标题（它是正文）**。这个细节决定了怎么做才对，实测踩过三种错法：

    * 稿件全用 ``##`` 时：一章都切不出来，「正文数字必须有依据」
      （``check_number_grounding``）直接返回空，界面显示"检查通过"；
    * 归一化成"每行都 ``#``"时：第一个 ``#`` 是**论文题目**，于是切出来的
      第一"章"是题目，第 1 章反而被它吞进正文——检查只覆盖题目那一行；
    * 归一化成"第一个 ``#``、其余 ``##```"时：``##`` 又变回正文，切得更少。

    所以正确做法是**把每一章都还原成 ``#``，同时不要出现"标题候选行"**。
    题目行本身不是章节，也不该参与检查，从待检文本里去掉即可——
    这样第一个 ``#`` 天然就是「第1章」。
    """
    out: list[str] = []
    for line in text.split("\n"):
        starts_with_hash = line.lstrip().startswith("#")
        stripped = line.lstrip("#").strip() if starts_with_hash else line.strip()
        probe = line if starts_with_hash else ("# " + stripped)
        if stripped and _SECTION_HEADING.match(probe):
            # 统一成单个 #：切分器只认这一种层级。
            out.append("# " + stripped)
        else:
            out.append(line)
    return "\n".join(out)


#: 看起来像"论文题目"而不是"章节标题"的行。
#:
#: 上游切分器在**第一个** ``#`` 处开始切，所以如果论文题目以 ``#`` 出现，
#: 它会被当成第一章，真正的第 1 章被吞进正文。题目又不是需要检查的内容，
#: 所以在送检前把它去掉最省事。
#:
#: 判据：**不是**章节标题（没有「第X章」「一、」「绪论」这类标识），
#: 且长度像题目（4~60 字）。已经在章节里的正常短标题（如「结果与分析」）
#: 会被 ``_SECTION_HEADING`` 先认出来，不会误判成题目。
_TITLE_LINE = re.compile(r"^#{1,6}\s*(?!#)(.{4,60})$")


def strip_leading_title(text: str) -> tuple[str, str]:
    """从待检文本里摘掉开头的论文题目行，返回 ``(去掉题目后的文本, 题目)``。

    只有"第一行看起来像题目而不是章节"时才摘；摘错了也只是少检查一行，
    而论文题目本来就不含需要检查的正文内容（数字、引用都在章节里）。
    """
    lines = text.split("\n")
    for idx, line in enumerate(lines[:5]):        # 题目只可能在最前面几行
        if not line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip("#").strip()
        if not stripped:
            continue
        # 是章节标题 -> 不动它
        if _SECTION_HEADING.match(line):
            return text, ""
        m = _TITLE_LINE.match(line)
        if m:
            rest = lines[:idx] + lines[idx + 1:]
            return "\n".join(rest), stripped
    return text, ""


def _render_draft(draft: dict) -> str:
    """把稿件渲染成待检文本。

    **只有章节标题用单个 ``#``；论文题目不能也做成 ``#``。**
    上游的章节切分器把每一个 ``#`` 行都当成一章，所以如果题目也用 ``#``，
    切出来的第一"章"就是论文题目本身，真正的各章反而落在它后面被吞掉——
    实测表现是「数字无依据」这条检查只覆盖了题目那一行，命中的数字少了一大半。
    题目与章节是两种东西，这里就不该用同一个层级。

    章节用单个 ``#`` 是必须的：``##`` 会被切分器当成正文，切不出章节时
    ``check_number_grounding`` 直接返回空，界面上表现成"检查通过"。
    """
    parts: list[str] = []
    if draft.get("title"):
        # 不用 # 前缀：这不是章节，是稿件题目。
        parts.append(str(draft["title"]).strip())
    for section in draft.get("sections") or []:
        heading = str(section.get("heading") or "").strip()
        parts.append(f"# {heading}" if heading else "")
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
