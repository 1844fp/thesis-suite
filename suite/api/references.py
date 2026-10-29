"""参考文献推荐 · 路由。

对应「开题报告」阶段的第一件事：**先有文献，才有开题报告**。

三条接口刻意分开，因为它们的花钱情况完全不同：

* ``/recommend`` —— 不花钱（只查公开库）。**没填 Key 也能用**，这与本站
  "不填 Key 也要能先用起来"的定位一致；填了 Key 才追加一次模型精排。
* ``/export`` —— 不花钱（纯本地渲染）。
* ``/to-kb`` —— 不花钱（本地入库）。把选中的文献摘要与书目存进 RAG 知识库，
  下一步 PaperQA2 的文献简报就建立在这批文献上。

精排失败（限流、Key 无效、模型不认 JSON）**不报错**，而是照常返回按规则排序的
结果并在 ``rerank`` 字段里写明为什么没精排。理由：规则排序本身已经可用，
因为一次可选增强失败而让用户拿不到任何结果，是把可选件的故障放大成了主流程故障。
"""

from __future__ import annotations

import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

from .. import credentials as creds_mod
from .. import llm as llm_mod
from .. import rag
from .. import references as refs

router = APIRouter(prefix="/api/refs", tags=["参考文献推荐"])

#: 导出格式 → (扩展名, MIME)。与 gather 的口径保持一致，前端可以复用同一套下载逻辑。
FORMATS = {
    "gbt": ("txt", "text/plain; charset=utf-8"),
    "bibtex": ("bib", "text/plain; charset=utf-8"),
    "markdown": ("md", "text/markdown; charset=utf-8"),
}

#: 精排时最多把多少条候选送给模型。40 条 × 220 字 ≈ 9k 汉字，是个便宜的规模；
#: 再多既贵又不会改变前 15 名的构成。
RERANK_TOP = 40


def _creds(request: Request) -> creds_mod.Credentials:
    try:
        return creds_mod.from_headers(request.headers)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/weights")
def weights() -> dict:
    """排序权重与口径。前端把这段显示给用户——**排序必须可解释**。"""
    return {"ok": True, **refs.describe_weights(), "limits": refs._limits_note()}


@router.post("/recommend")
async def recommend(request: Request) -> dict:
    """按选题推荐 10~20 篇参考文献。

    请求体::

        {"topic": str, "keywords": [str], "english": str,
         "total": 10~20, "min_cn": int, "min_en": int, "top": 1~20,
         "rerank": bool}

    ``rerank=True`` 且**访客给了 Key** 时才做模型精排；没给 Key 时静默跳过，
    并在响应的 ``rerank`` 字段里说明"没填 Key，按规则排序"。
    """
    body = await request.json()
    topic = str(body.get("topic") or "").strip()
    if len(topic) < 4:
        raise HTTPException(status_code=400, detail="请填写选题或研究方向（至少 4 个字）。")

    keywords = body.get("keywords") or []
    if isinstance(keywords, str):
        # 逗号/顿号分隔的写法也要收：用户不一定知道这里要传数组。
        keywords = [k.strip() for k in keywords.replace("，", ",").replace("、", ",").split(",")]
    keywords = [str(k).strip() for k in keywords if str(k).strip()][:8]

    def _int(name: str, default: int, low: int, high: int) -> int:
        try:
            value = int(body.get(name) if body.get(name) is not None else default)
        except (TypeError, ValueError):
            return default
        return max(low, min(value, high))

    total = _int("total", 15, 10, 20)

    result = refs.recommend(
        topic=topic,
        keywords=keywords,
        english=str(body.get("english") or "").strip(),
        total=total,
        min_cn=_int("min_cn", 4, 0, total),
        min_en=_int("min_en", 4, 0, total),
    )
    if not result.get("ok"):
        # 检索不到不是客户端的错，返回 200 + ok:false 让前端照常渲染说明与各源状态。
        return result

    result["rerank"] = _maybe_rerank(request, body, result, topic)
    return result


def _maybe_rerank(request: Request, body: dict, result: dict, topic: str) -> dict:
    """可选的模型精排。**任何失败都只是"没精排"，不是请求失败。**"""
    if not body.get("rerank"):
        return {"done": False, "reason": "未开启精排（默认按规则排序：等级 / 被引 / 时效 / 开放获取）。"}

    creds = _creds(request)
    if not creds.configured:
        return {"done": False,
                "reason": "没有填 API Key，已按规则排序（等级 / 被引 / 时效 / 开放获取）。"
                          "填 Key 后可以再做一次相关性精排。"}

    items = result.get("items") or []
    if not items:
        return {"done": False, "reason": "没有候选可精排。"}

    try:
        with llm_mod.Chat(creds) as chat:
            payload = chat.ask_json(refs.rerank_prompt(topic, items, top=RERANK_TOP),
                                    temperature=0.0, max_tokens=3000)
    except Exception as exc:  # noqa: BLE001 - 精排是可选增强，失败不该让推荐失败
        return {"done": False, "reason": f"精排失败（已保留规则排序的结果）：{exc}"}

    scores: dict[int, dict] = {}
    for row in (payload.get("items") if isinstance(payload, dict) else None) or []:
        try:
            index = int(row.get("i"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 1 <= index <= len(items):
            scores[index] = {"rel": row.get("rel"), "why": row.get("why")}

    if not scores:
        return {"done": False, "reason": "精排返回里没有可用的相关度，已保留规则排序的结果。"}

    refs.apply_rerank(result, scores)
    return {"done": True, "scored": len(scores),
            "reason": f"已对前 {len(scores)} 条做模型相关性精排，并据此重排（清单成员不变）。"}


@router.post("/export", response_class=PlainTextResponse)
async def export(request: Request) -> PlainTextResponse:
    """导出参考文献表。**不需要 Key**（纯字符串渲染）。

    直接接收上一次 ``/recommend`` 的 ``items``，而不是在服务端保存一份状态：
    推荐结果本来就是"给用户看的一份清单"，存服务端会引入会话生命周期、
    过期与清理的一整套问题，而用户刷新页面重跑一次只要十几秒。
    """
    body = await request.json()
    _creds(request)  # 提前校验凭据头，地址不合法要在导出前就说清楚
    fmt = str(body.get("format") or "markdown").strip().lower()
    if fmt not in FORMATS:
        raise HTTPException(status_code=400,
                            detail=f"不支持的导出格式 {fmt!r}，可选：{'、'.join(sorted(FORMATS))}。")

    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="没有可导出的条目：请先生成推荐清单。")

    renderers = {"gbt": refs.render_gbt, "bibtex": refs.render_bibtex}
    if fmt == "markdown":
        text = refs.render_markdown(body)
    else:
        text = renderers[fmt](items)

    ext, media = FORMATS[fmt]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label = {"gbt": "参考文献推荐-GBT7714", "bibtex": "参考文献推荐-BibTeX",
             "markdown": "参考文献推荐"}[fmt]
    filename = f"{label}-{stamp}.{ext}"
    return PlainTextResponse(
        text, media_type=media,
        # 文件名带中文：必须用 RFC 5987 的 filename*=UTF-8'' 形式，
        # 直接写 filename= 会被浏览器按 latin-1 解出乱码（与 gather/review 的处理一致）。
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )


@router.post("/to-kb")
async def to_kb(request: Request) -> dict:
    """把选中的文献存进本地 RAG 知识库。**不需要 Key**。

    存进去的是**摘要 + 书目信息**，不是全文——我们本来就没有全文（公开库只给摘要）。
    这一点必须如实告诉用户，否则他会以为知识库里已经有了这些文献的全文，
    到了 PaperQA2 问答那一步才发现答不上来。

    每条按 ``参考文献：<题名>`` 命名，并在 ``meta`` 里带上等级、被引、DOI 等，
    这样 PaperQA2 的语料前缀能写出真实的书目，答案里的引用才有依据。
    """
    body = await request.json()
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="没有选中的条目。")
    if len(items) > 30:
        raise HTTPException(status_code=413, detail="一次最多入库 30 条。")

    added: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        abstract = str(item.get("abstract") or "").strip()
        if not title:
            continue
        if len(abstract) < 30:
            # 没有摘要的条目进知识库只会污染检索（它没有任何可引用的正文），
            # 所以**不静默丢弃**，而是记进 skipped 并说明原因。
            skipped.append({"title": title,
                            "reason": "公开库没有给出摘要，入库后无法作为依据引用。"})
            continue
        meta = {
            "title": title,
            "authors": str(item.get("authors") or ""),
            "year": str(item.get("year") or ""),
            "venue": str(item.get("venue") or item.get("source") or ""),
            "doi": str(item.get("doi") or ""),
            "url": str(item.get("url") or ""),
            "issn": str(item.get("issn") or ""),
            "tier": str(item.get("tier") or ""),
            "citations": item.get("citations"),
            "language": str(item.get("language") or ""),
            "open_access": bool(item.get("open_access")),
            "citation": str(item.get("citation") or title),
            "sources": item.get("sources") or [],
            "from": "参考文献推荐",
        }
        # 正文用「题名 + 书目 + 摘要」而不是只放摘要：检索与 PaperQA2 都会读这段，
        # 题名与期刊名是中文文献最可靠的检索锚点（摘要里未必出现全称）。
        text = (f"题名：{title}\n"
                f"作者：{meta['authors'] or '（未提供）'}\n"
                f"来源：{meta['venue'] or '（未提供）'} {meta['year']}\n"
                f"DOI：{meta['doi'] or '（未提供）'}\n"
                f"文献等级：{meta['tier'] or '未判定'}\n\n"
                f"摘要：\n{abstract}")
        result = rag.add_document(text, title=f"参考文献：{title}",
                                  origin="参考文献推荐", meta=meta)
        if result.get("ok"):
            added.append({"doc_id": result.get("doc_id"), "title": title,
                          "chunks": result.get("chunks"), "chars": result.get("chars")})
        else:
            failed.append({"title": title, "error": result.get("error") or "入库失败"})

    return {
        "ok": True,
        "added": added,
        "added_count": len(added),
        "skipped": skipped,
        "failed": failed,
        "stats": rag.stats(),
        "note": ("入库的是**摘要与书目信息**，不是全文（公开文献库只提供摘要）。"
                 "所以后续用 PaperQA2 问答时，它能回答的是'这几篇讲什么、观点有何不同'，"
                 "而不是'第 3 页的公式是什么'。要看全文请把 PDF 上传到「资料获取」阶段。"),
    }
