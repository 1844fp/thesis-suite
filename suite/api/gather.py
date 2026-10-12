"""资料获取助手 · 路由。

这个助手解决的是「把手里的一堆材料变成可被引用的证据」。它的核心价值**不是**
让模型总结文献，而是**挡住模型编造**：写作助手只依据证据库落笔，如果证据库本身
就掺了模型想象的"原文"，那么下游整条链路（写作 → 评估 → 导出）全部失去意义。

所以本模块有两条刻意的设计：

* ``/extract`` 要求模型给出的 ``passages`` 必须能在访客提供的原文里**逐字找到**，
  找不到的直接丢掉并计入 ``dropped_passages``。提示词里写明纪律是必要的，
  但**不能只靠提示词**——校验必须发生在代码里，模型说什么不算数。
* ``/verify`` 走 Crossref 查真伪，**不需要 API Key**（公共数据库，不是模型），
  而且网络失败一律记 ``inconclusive``，绝不因为"查不到"就说人家编造。
  误告造假的代价远大于漏报，这一点与上游 ``ExternalVerifier`` 的判定规则一致。

不要调用上游的 ``app.config.load_settings()``：那会从服务器 .env 甚至邻居项目借
密钥。本模块只认请求头里的访客凭据（见 ``suite/credentials.py``）。
"""

from __future__ import annotations

import os
import re
import time
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse

from .. import credentials as creds_mod
from .. import fileintake
from .. import llm as llm_mod
from ..sessions import STORE

router = APIRouter(prefix="/api/gather", tags=["资料获取助手"])

#: 单条资料的正文上限（字符）。超长截断而不是拒绝：访客手里往往是一整章 PDF 摘录，
#: 直接 413 会让他白跑一趟；截断 + 如实标注 ``truncated`` 更有用。
MAX_TEXT = 20_000

#: 一次提交最多几条资料。上限之外直接拒绝：每一条都要调一次模型，
#: 放开等于把公网接口变成"批量喂料"通道。
MAX_SOURCES = 20

#: ``/status`` 最多回吐多少条证据视图。前端只需要一屏，多了纯属浪费带宽。
MAX_STATUS_ITEMS = 200

#: 送去模型的一份切片多少字。
#:
#: ``MAX_TEXT`` 是**收料**上限，不等于**一次喂给模型**的上限：20k 汉字在中文
#: 分词下轻易超过 10k token，很多便宜模型一次吃不下。所以长文按切片分多次抽取，
#: 每次都在同一份原文里做逐字校验（校验用全文，不用切片，避免把跨切片的引用
#: 误判成编造）。多花几次调用换取"不因超长而静默丢内容"。
SLICE = 8_000

#: 一次最多核验多少条（Crossref 是公益服务，别把人家打疼）。
MAX_LOOKUPS = 30

#: 逐字校验的最短片段长度。太短的片段（"综上"、"2020"）在原文里到处都是，
#: 校验通过也说明不了什么，反而会让"防编造"这道闸门看起来比实际更严。
MIN_PASSAGE = 4

#: 交给 ``ExternalVerifier`` 的相似度门槛，与上游 ``verify.title_match_threshold``
#: 的默认值保持一致：0.82 只放行"标题基本一致"，低于它的命中记为 ``weak_match``。
#: 不能调高——那会把真实但排版有出入的文献误报成可疑引用。
MATCH_THRESHOLD = 0.82

#: Crossref 单次请求超时（秒）。上游默认 30s，公网同步接口等不起，收到 20s。
CROSSREF_TIMEOUT = 20.0

#: 环境变量名：Crossref 礼貌池的联系邮箱。**不是密钥**（Crossref 无需 key），
#: 但带上它 Crossref 才会给"礼貌池"配额、少被限流。留空照样能用，
#: 只是高并发时更容易落到 ``inconclusive``。
MAILTO_ENV = "GATHER_MAILTO"

#: 允许的文献类型。模型若给出清单外的值，一律归到"其他"，
#: 免得下游按类型分支的代码被脏值打穿。
TYPES = ("期刊论文", "学位论文", "会议论文", "专著", "报告", "其他")

#: 导出格式 → (扩展名, MIME)。BibTeX 用 ``.bib`` 且 MIME 不存在体面的注册值，
#: 浏览器对 ``text/plain`` 最友好。
FORMATS = {
    "gbt": ("txt", "text/plain; charset=utf-8"),
    "bibtex": ("bib", "text/plain; charset=utf-8"),
    "markdown": ("md", "text/markdown; charset=utf-8"),
}


def _creds(request: Request) -> creds_mod.Credentials:
    """从请求头取访客凭据。凭据不合法（例如自建地址被 SSRF 名单挡住）直接 400。"""
    try:
        return creds_mod.from_headers(request.headers)
    except creds_mod.CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _as_text(value: Any) -> str:
    """模型字段的宽松归一：任何非字符串（None / 数字 / 嵌套结构）都变成空串。

    宁可丢一个字段，也不要把 ``None`` 或 ``{"text": ...}`` 塞进证据库——
    那些东西会一路流到写作和导出，最后变成排版里的 "None"。
    """
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return str(value).strip()


def _as_list(value: Any) -> list[str]:
    """把模型返回的"列表"变成字符串列表：容忍它返回单个字符串或嵌套。"""
    if isinstance(value, str):
        parts = re.split(r"\n+", value)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        return []
    out: list[str] = []
    for part in parts:
        text = _as_text(part)
        if text:
            out.append(text)
    return out


def _snip(value: str, limit: int) -> str:
    """按长度截断并加省略号。只用于展示，不改变语义。"""
    text = (value or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _squeeze(text: str) -> str:
    """折叠空白，用于逐字校验前的比较。

    模型抄写时把换行/多空格弄丢是常态，为了这个丢掉一条**真实**的原文片段，
    等于自己削弱证据库。但也不做全角/标点归一：那会让"差不多"的改写蒙混过关。
    """
    return re.sub(r"\s+", "", text or "")


def _item_id(item: dict) -> str:
    """给每条证据一个稳定短标识（去重键同源），供前端做删除/定位。"""
    for key in ("doi", "url", "title"):
        value = str(item.get(key) or "").strip()
        if value:
            return f"{key}:{value.lower()[:120]}"
    return "text:" + _squeeze(str(item.get("text") or ""))[:60]


def _is_verified(item: dict) -> bool:
    return bool(str(item.get("verified") or "").strip())


def _view(item: dict) -> dict:
    """证据库条目 → 前端视图。

    刻意比契约多给几个字段（``passages`` / ``claims`` / ``type`` / ``truncated``）：
    契约要求的是**至少**有那几个键，前端要展示"凭什么说我抽到了这句"就得看到原文片段。
    """
    passages = item.get("passages") or []
    claims = item.get("claims") or []
    return {
        "id": _item_id(item),
        "title": str(item.get("title") or ""),
        "authors": str(item.get("authors") or ""),
        "year": str(item.get("year") or ""),
        "doi": str(item.get("doi") or ""),
        "url": str(item.get("url") or ""),
        "source": str(item.get("source") or ""),
        "type": str(item.get("type") or ""),
        "citation": str(item.get("citation") or ""),
        "passage_count": len(passages) if isinstance(passages, list) else 0,
        "passages": list(passages) if isinstance(passages, list) else [],
        "claims": list(claims) if isinstance(claims, list) else [],
        "verified": str(item.get("verified") or ""),
        "verified_detail": str(item.get("verified_detail") or ""),
        "verified_at": str(item.get("verified_at") or ""),
        "origin": str(item.get("origin") or ""),
        "truncated": bool(item.get("truncated")),
    }


def _session_or_new(sid: str | None, *, create: bool = False):
    """取会话。``sid`` 传了但已过期时，``create=True`` 会**透明新建**一个。

    为什么允许"悄悄换号"：会话只活在内存里（见 ``sessions.py``），部署重启或多副本
    都会让 sid 失效。为了这个把访客的抽取结果整批丢掉，比给他一个新 sid 糟糕得多。
    """
    return STORE.get(sid, create=create) if (sid or create) else None


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
@router.get("/status")
def status(sid: str = "") -> dict:
    """当前会话的证据库概览。**没有 sid 也算正常调用**，不报 404。

    前端会在页面加载时先问一次"手头有没有证据"，那时候还没有 sid。
    把这种情况做成 404 只会让前端多写一条无意义的分支。
    """
    session = STORE.get(sid.strip() or None)
    evidence = list(session.evidence) if session else []
    return {
        "ok": True,
        "sid": session.sid if session else "",
        "evidence_count": len(evidence),
        "verified_count": sum(1 for item in evidence if _is_verified(item)),
        "truncated": max(0, len(evidence) - MAX_STATUS_ITEMS),
        "items": [_view(item) for item in evidence[:MAX_STATUS_ITEMS]],
    }


# ---------------------------------------------------------------------------
# 抽取（核心）
# ---------------------------------------------------------------------------
_SYSTEM_PROMPT = (
    "你是学术资料的证据抽取器。用户给你一段文献正文（题录、摘要或原文摘录），"
    "你把它整理成结构化的证据条目。\n"
    "**纪律（违反即作废）：**\n"
    "1. 只依据用户给的原文，不要引入任何外部知识，不要「顺手补全」。\n"
    "2. passages 里的每一条都必须是从原文里**逐字复制**的连续片段，"
    "不得改写、不得合并、不得添加原文没有的字，也不要加引号或编号。\n"
    "3. 作者、年份、DOI、期刊名认不出来就留空字符串，**绝对不要猜**。"
    "宁可交一条字段残缺的记录，也不要交一条看起来完整但是假的记录。\n"
    "4. 原文里没有的内容就不要写；claims 必须是原文能支持的结论。\n"
    "只输出 JSON，不要解释、不要 Markdown 围栏。"
)
_USER_TEMPLATE = (
    "文献信息如下。\n"
    "用户给出的题名（可能为空）：{title}\n"
    "用户给出的 DOI（可能为空）：{doi}\n"
    "用户给出的链接（可能为空）：{url}\n"
    "--- 原文开始 ---\n"
    "{text}\n"
    "--- 原文结束 ---\n\n"
    "请输出 JSON，字段：\n"
    '{{"title": "文献题名", "authors": "作者，多作者用中文顿号或逗号分隔", '
    '"year": "四位年份，识别不到就空串", "doi": "识别不到就空串", '
    '"source": "期刊名/会议名/出版社/学位授予单位，识别不到就空串", '
    '"type": "期刊论文|学位论文|会议论文|专著|报告|其他", '
    '"passages": ["从上面原文里逐字摘出的关键原文片段，3~8 条"], '
    '"claims": ["这篇文献的主要结论，逐条一句话"]}}'
)


def _merge(acc: dict, part: dict) -> None:
    """把一次切片抽取的结果并进累积结果。

    策略：**空值不覆盖已有值**，列表取并集。
    模型有时只在第一片里认出题名、只在第二片里认出年份；后写的空串把它抹掉，
    等于白抽。"后写的非空值覆盖先写的"则用来修正模型自己前后的不一致。
    """
    for key, value in part.items():
        if isinstance(value, list):
            current = acc.setdefault(key, [])
            if not isinstance(current, list):
                continue
            seen = {_squeeze(v) for v in current}
            for entry in value:
                if _squeeze(entry) not in seen:
                    seen.add(_squeeze(entry))
                    current.append(entry)
        elif value and not str(acc.get(key) or "").strip():
            acc[key] = value


def _extract_one(chat, source: dict, text: str) -> tuple[dict, int, bool]:
    """抽取一条资料。返回 ``(原始结果字典, 被丢弃的片段数, 是否截断过)``。

    长文按 ``SLICE`` 切片分次抽取；无 API Key 时 ``Chat.ask_json`` 会抛
    ``LLMError``，由调用方统一收进 ``errors``。
    """
    title_hint = str(source.get("title") or "").strip()
    doi_hint = str(source.get("doi") or "").strip()
    url_hint = str(source.get("url") or "").strip()

    pieces = [text[i : i + SLICE] for i in range(0, len(text), SLICE)] or [""]
    merged: dict = {}
    dropped = 0

    for index, piece in enumerate(pieces):
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": _USER_TEMPLATE.format(
                    title=title_hint or "（未提供）",
                    doi=doi_hint or "（未提供）",
                    url=url_hint or "（未提供）",
                    text=piece
                    + (
                        f"\n…（原文过长，这是第 {index + 1}/{len(pieces)} 段摘录）"
                        if len(pieces) > 1
                        else ""
                    ),
                ),
            },
        ]
        parsed = chat.ask_json(messages, temperature=0.0, max_tokens=3000)
        if not isinstance(parsed, dict):
            # 模型返回了数组/标量：当作这一片没抽出东西，而不是让整条失败。
            continue

        passages = _valid_passages(_as_list(parsed.get("passages")), text)
        dropped += len(_as_list(parsed.get("passages"))) - len(passages)
        claims = [c for c in _as_list(parsed.get("claims")) if c]
        doc_type = _as_text(parsed.get("type"))
        _merge(
            merged,
            {
                "title": _as_text(parsed.get("title")),
                "authors": _as_text(parsed.get("authors")),
                "year": _as_text(parsed.get("year")),
                "doi": _as_text(parsed.get("doi")),
                "source": _as_text(parsed.get("source")),
                "type": doc_type if doc_type in TYPES else ("其他" if doc_type else ""),
                "passages": passages,
                "claims": claims,
            },
        )

    return merged, dropped, len(pieces) > 1


def _valid_passages(passages: list[str], text: str) -> list[str]:
    """只保留**确实逐字出现在原文里**的片段。

    这是本模块最要紧的一段代码。提示词里的纪律是给模型的，不是给攻击者或
    幻觉的——模型（尤其是小模型）在被要求"引用原文"时，编一句读起来很像原文的
    话是常见失败模式。这里用折叠空白后的子串判断做硬校验：**原文是唯一裁判**，
    校验不过的片段一律丢弃，并在返回里如实报数，让作者看得见"模型被拦了几次"。
    """
    haystack = _squeeze(text)
    kept: list[str] = []
    seen: set[str] = set()
    for piece in passages:
        raw = (piece or "").strip()
        if len(raw) < MIN_PASSAGE:
            continue
        key = _squeeze(raw)
        if key not in haystack or key in seen:
            continue
        seen.add(key)
        kept.append(raw)
    return kept


def _build_item(
    source: dict,
    result: dict,
    text: str,
    *,
    truncated: bool,
    dropped: int,
) -> dict:
    """把「访客给的 + 模型抽的」合成一条证据。

    访客自己提供的题名/DOI/URL 优先级**高于**模型的识别结果：他贴的就是他确认过的，
    模型只是在读这段文字。
    """
    title = str(source.get("title") or "").strip() or str(result.get("title") or "").strip()
    doi = str(source.get("doi") or "").strip() or str(result.get("doi") or "").strip()
    url = str(source.get("url") or "").strip()
    authors = str(result.get("authors") or "").strip()
    year = str(result.get("year") or "").strip()
    container = str(result.get("source") or "").strip()
    doc_type = str(result.get("type") or "").strip()

    passages = [str(p) for p in (result.get("passages") or [])]
    claims = [str(c) for c in (result.get("claims") or [])]

    # ``text`` 只放**校验通过**的原文片段，而不是访客粘进来的整段文字。
    # 原因：评估助手的"数字可溯源"检查会拿 evidence[*].text 当证据池，
    # 把用户输入里的数字也算成"有据可查"会让那个检查形同虚设。
    body = "\n".join(passages)[:MAX_TEXT]

    return {
        "title": title,
        "authors": authors,
        "year": year,
        "doi": doi,
        "url": url,
        "source": container,
        "type": doc_type,
        "citation": _citation(title=title, authors=authors, container=container, year=year, doi=doi),
        "text": body,
        "passages": passages,
        "claims": claims,
        "verified": "",
        "verified_detail": "",
        "verified_at": "",
        "origin": "user",
        "truncated": truncated,
        "dropped_passages": dropped,
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def _citation(*, title: str, authors: str, container: str, year: str, doi: str) -> str:
    """拼一个"短到能塞进正文脚注、又足以让人找回原文"的来源串。

    刻意不用 GB/T 7714 的完整格式：那是 ``/export`` 的活。这里缺哪个字段就少哪一段，
    宁可短，不要出现 "，  , undefined" 这种残缺。
    """
    head = "、".join(_authors(authors)[:3])
    if head and len(_authors(authors)) > 3:
        head += " 等"
    parts = [p for p in (head, title, container, year) if p]
    text = "，".join(parts)
    if doi:
        text = f"{text}。DOI: {doi}" if text else f"DOI: {doi}"
    return text


def _authors(raw: str) -> list[str]:
    """拆作者串。中英文的常见分隔（顿号/逗号/分号/``and``）都要认。"""
    return [p.strip() for p in re.split(r"[、,，;；]|\band\b", str(raw or "")) if p.strip()]


@router.post("/import")
async def import_files(
    request: Request,
    files: list[UploadFile] = File(default_factory=list),
) -> dict:
    """上传本地文档（PDF / Word / Markdown / 文本 / HTML / CSV），解析成文本。

    **这一步不需要 API Key**：只做格式解析，不调用模型。所以它可以在访客还没
    决定用哪个模型时就先用起来。解析出的文本由前端交给 ``/extract`` 抽成证据。

    ``?full=1`` 时把解析出的完整文本一起返回；默认只回摘要与前 4000 字预览——
    一章 PDF 的全文回到浏览器没有意义，前端要的是"确认解析对了"，正文随后
    由前端原样转发给 ``/extract``（那里有它自己的长度上限与截断策略）。

    安全约束全部是硬性的，见 ``suite/fileintake.py`` 开头的说明：
    只读、不落盘、限大小限数量、跳过不支持的类型并如实回报原因。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件。")
    if len(files) > fileintake.MAX_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多导入 {fileintake.MAX_FILES} 个文件（收到 {len(files)} 个）。",
        )

    # full=1 时回完整文本。默认不回，避免几 MB 的正文在浏览器与后端之间来回搬。
    full = str(request.query_params.get("full") or "").strip().lower() in {"1", "true", "yes", "on"}

    results: list[fileintake.Extracted] = []
    total_bytes = 0
    for upload in files:
        name = upload.filename or "未命名"
        try:
            data = await upload.read()
        except Exception as exc:  # noqa: BLE001 - 读上传流也可能失败
            results.append(fileintake.Extracted(filename=name, error=f"读取上传内容失败：{exc}"))
            continue
        finally:
            try:
                await upload.close()
            except Exception:  # noqa: BLE001
                pass

        total_bytes += len(data)
        if total_bytes > fileintake.MAX_FILE_BYTES * 2:
            results.append(fileintake.Extracted(
                filename=name,
                error=f"本批文件累计过大（已超过 {fileintake.MAX_FILE_BYTES * 2 // 1024 // 1024} MB），已停止处理。",
            ))
            break
        results.append(fileintake.extract(name, data))

    ok_items = [r for r in results if r.ok]
    sources = fileintake.split_into_sources(ok_items)

    payload: list[dict] = []
    for item in results:
        view = item.summary()
        if full and item.ok:
            view["text"] = item.text
        else:
            view["text"] = item.text[:4000]
            view["preview_only"] = item.ok and len(item.text) > 4000
        view["suggested_source"] = {
            "title": fileintake.guess_title(item),
            "chars": len(item.text),
        } if item.ok else None
        payload.append(view)

    # 全部失败是**真失败**：有文件但一个都没读出来，不能以 200 结束——
    # 否则前端会显示"成功，0 条"，那是最误导人的结果。
    if files and not ok_items:
        reasons = "；".join(f"{r.filename}: {r.error or r.warning}" for r in results[:4])
        raise HTTPException(
            status_code=422,
            detail=f"{len(files)} 个文件都没能解析出文字。{reasons}",
        )

    return {
        "ok": True,
        "received": len(files),
        "parsed": len(ok_items),
        "failed": len(results) - len(ok_items),
        "total_bytes": total_bytes,
        "files": payload,
        # 直接可交给 /extract 的 sources（PDF 逐页拆分，保留页码）。
        "source_count": len(sources),
        "sources": sources if full else [
            {**s, "text": s["text"][:1500]} for s in sources
        ],
        "note": (
            "解析不需要 API Key。把 sources 交给 /api/gather/extract 即可抽成证据；"
            "PDF 已按页拆分，证据会带上页码。"
        ),
    }


@router.post("/extract")
async def extract(request: Request) -> dict:
    """把访客贴进来的资料逐条抽成结构化证据，并入证据库。

    **需要访客自带 API Key**：这一步必须调用模型。服务器不使用自己的密钥。

    单条失败不拖垮整批：错误收进 ``errors``，其余照常入库。全部失败时，
    "有材料但一条都没抽出来"是**真失败**，必须以非 200 结束，否则前端会显示
    "成功，0 条"——那是最误导人的一种结果。
    """
    body = await request.json()
    creds = _creds(request)

    raw_sources = body.get("sources")
    if not isinstance(raw_sources, list) or not raw_sources:
        raise HTTPException(status_code=400, detail="请至少提供一条资料（sources 非空）。")
    if len(raw_sources) > MAX_SOURCES:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多提交 {MAX_SOURCES} 条资料（收到 {len(raw_sources)} 条），请分批提交。",
        )

    sources: list[dict] = []
    for index, raw in enumerate(raw_sources, start=1):
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail=f"第 {index} 条资料格式不对：应为对象。")
        text = str(raw.get("text") or "").strip()
        if not text:
            raise HTTPException(
                status_code=400, detail=f"第 {index} 条资料缺少正文（text 不能为空）。"
            )
        sources.append(
            {
                "title": str(raw.get("title") or "").strip(),
                "doi": str(raw.get("doi") or "").strip(),
                "url": str(raw.get("url") or "").strip(),
                # 截断而非拒绝：超长资料的其余部分仍可能有用，丢掉整条是最差选择。
                "text": text[:MAX_TEXT],
                "truncated": len(text) > MAX_TEXT,
            }
        )

    if not creds.configured:
        raise HTTPException(
            status_code=400, detail="抽取证据需要你自己的 API Key（页面顶部填入后重试）。"
        )

    session = _session_or_new(str(body.get("sid") or "").strip() or None, create=True)

    started = time.time()
    new_items: list[dict] = []
    errors: list[dict] = []
    added = 0
    dropped_total = 0
    truncated_titles: list[str] = []

    with llm_mod.Chat(creds) as chat:
        for source in sources:
            label = source["title"] or _snip(source["text"], 40)
            if source["truncated"]:
                truncated_titles.append(label)
            try:
                result, dropped, too_long = _extract_one(chat, source, source["text"])
            except llm_mod.LLMError as exc:
                errors.append({"title": label, "error": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 - 单条异常不该毁掉整批
                errors.append({"title": label, "error": f"{type(exc).__name__}: {exc}"})
                continue

            dropped_total += dropped
            item = _build_item(
                source,
                result,
                source["text"],
                truncated=bool(source["truncated"] or too_long),
                dropped=dropped,
            )
            # 一条片段都没通过校验、也没有任何结论时，这条文献只有题录价值。
            # 仍然入库（题录本身是真的），但要让前端看得见它"没有可引用原文"。
            added += session.add_evidence([item])
            new_items.append(item)

    if errors and not new_items:
        raise HTTPException(
            status_code=502,
            detail=f"全部 {len(errors)} 条资料都抽取失败：{errors[0]['error']}",
        )

    session.touch()
    return {
        "ok": True,
        "sid": session.sid,
        "model": creds.model,
        "added": added,
        "duplicates": len(new_items) - added,
        "dropped_passages": dropped_total,
        "truncated": truncated_titles,
        "total": len(session.evidence),
        "seconds": round(time.time() - started, 2),
        "items": [_view(item) for item in new_items],
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 外部核验（不需要 Key）
# ---------------------------------------------------------------------------
def _verifier(mailto: str):
    """构造只连 Crossref 的核验器。

    上游的 ``ExternalVerifier(mailto=...)`` 构造签名与它自己的 ``verify()`` 文档
    并不一致（``mailto`` 其实属于 ``CrossrefAdapter``），这里按正确签名显式装配。

    **只用 Crossref 一个源**是有意的取舍：打开 OpenAlex 会让每条证据多打一次外部
    接口（限流下还会退避重试，最坏两分钟），而公网同步请求等不起。代价是中文文献
    覆盖率略降，表现为 ``not_found`` 里多出一批"其实存在但没收录"——所以
    ``ExternalVerifier`` 已经会在中文标题上加一句"建议再用知网/万方核实"，我们照原样透传。
    """
    from app.adapters.research import CrossrefAdapter, ExternalVerifier

    crossref = CrossrefAdapter(mailto=mailto, timeout=CROSSREF_TIMEOUT)
    # 核验超时/限流时不要长时间退避重试：结论记 inconclusive 比让请求挂两分钟好。
    crossref.retries = 1
    return ExternalVerifier(crossref=crossref, threshold=MATCH_THRESHOLD)


def _lookup(item: dict) -> tuple[str, str, dict]:
    """核验单条证据，返回 ``(verdict, detail, best)``。

    异常一律落到 ``inconclusive``：**网络失败不等于文献不存在**。把断网说成
    "疑似编造"是这个功能最严重的失败模式，会直接摧毁作者对工具的信任。
    """
    from app.adapters.research import CrossrefAdapter

    doi = CrossrefAdapter._clean_doi(str(item.get("doi") or ""))
    title = str(item.get("title") or "").strip()
    if not doi and not title:
        return "inconclusive", "这条证据既没有标题也没有 DOI，无从检索。", {}

    mailto = ""
    if not doi:
        # 按标题检索必须带联系邮箱（Crossref 礼貌池），否则高并发下只会拿到限流。
        mailto = os.environ.get(MAILTO_ENV, "").strip()
    try:
        outcome = _verifier(mailto).verify(doi=doi, title=title)
    except Exception as exc:  # noqa: BLE001 - 网络/协议问题如实记 inconclusive
        return "inconclusive", f"核验请求失败：{type(exc).__name__}: {exc}", {}

    verdict = str(outcome.get("verdict") or "inconclusive")
    best = outcome.get("best") or {}
    errors = outcome.get("errors") or []
    reason = str(errors[0]) if errors else ""
    detail = str(best.get("note") or best.get("title") or reason or "")
    return verdict, detail, best if isinstance(best, dict) else {}


@router.post("/verify")
async def verify(request: Request) -> dict:
    """对证据库里**还没核验过**的条目做 DOI/标题核验。不需要 API Key。

    为什么"没核验过"用空串判断而不是布尔：``verified`` 存的是判定值
    （``confirmed`` / ``not_found`` / ``weak_match`` / ``inconclusive``），
    前端要展示的是**为什么**这么判，不是一个是非位。
    """
    body = await request.json()
    sid = str(body.get("sid") or "").strip()
    session = STORE.get(sid or None)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，请重新提交资料。")

    try:
        limit = int(body.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, MAX_LOOKUPS))

    pending = [item for item in session.evidence if not _is_verified(item)]
    if not pending:
        return {
            "ok": True,
            "sid": session.sid,
            "checked": 0,
            "candidates": 0,
            "remaining": 0,
            "tally": {},
            "items": [],
            "note": "证据库里没有待核验的条目。",
        }

    targets = pending[:limit]
    checked: list[dict] = []
    tally: dict[str, int] = {}
    started = time.time()

    for item in targets:
        verdict, detail, best = _lookup(item)
        item["verified"] = verdict
        item["verified_detail"] = detail
        item["verified_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        tally[verdict] = tally.get(verdict, 0) + 1
        checked.append(
            {
                "id": _item_id(item),
                "title": str(item.get("title") or ""),
                "doi": str(item.get("doi") or ""),
                "verdict": verdict,
                "detail": detail,
                "best": best,
            }
        )

    session.touch()
    return {
        "ok": True,
        "sid": session.sid,
        "checked": len(checked),
        "candidates": len(pending),
        "remaining": max(0, len(pending) - len(checked)),
        "skipped_verified": len(session.evidence) - len(pending),
        "seconds": round(time.time() - started, 2),
        "tally": tally,
        "items": checked,
    }


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------
def _bibtex_key(item: dict, used: set[str]) -> str:
    """生成 BibTeX 引用键，保证同一份文件内不重键。

    BibTeX 遇到重复的键会**静默丢掉后一条**——作者不会看到任何报错，
    只会发现参考文献少了一条。所以这里必须去重。
    """
    first = ""
    authors = _authors(str(item.get("authors") or ""))
    if authors:
        # 中文名字取姓（首字），英文名字取姓氏（最后一个词）。
        head = authors[0]
        first = head[0] if re.search(r"[\u4e00-\u9fff]", head) else re.split(r"\s+", head)[-1]
    first = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", first) or "anon"
    year = re.sub(r"\D", "", str(item.get("year") or "")) or "nd"
    title = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(item.get("title") or ""))[:12] or "ref"
    key = f"{first}{year}{title}"
    candidate, suffix = key, 0
    while candidate in used:
        suffix += 1
        candidate = f"{key}{chr(ord('a') + suffix - 1)}"
    used.add(candidate)
    return candidate


def _bibtex_type(item: dict) -> str:
    return {
        "期刊论文": "article",
        "学位论文": "phdthesis",
        "会议论文": "inproceedings",
        "专著": "book",
        "报告": "techreport",
    }.get(str(item.get("type") or ""), "misc")


def _bibtex_escape(value: str) -> str:
    """花括号是 BibTeX 的字段边界，正文里出现必须转义，否则整条记录解析失败。"""
    return (value or "").replace("{", "\\{").replace("}", "\\}").replace("\n", " ").strip()


def _render_bibtex(items: list[dict]) -> str:
    """按 BibTeX 拼条目。**缺字段就不写那个字段**——不补年份、不编期刊名。"""
    used: set[str] = set()
    blocks: list[str] = []
    for item in items:
        fields: list[tuple[str, str]] = []
        if str(item.get("title") or "").strip():
            fields.append(("title", str(item["title"])))
        if str(item.get("authors") or "").strip():
            # BibTeX 用 " and " 分隔作者，中文顿号在这里不是分隔符。
            fields.append(("author", " and ".join(_authors(str(item["authors"])))))
        if str(item.get("year") or "").strip():
            fields.append(("year", str(item["year"])))
        if str(item.get("source") or "").strip():
            key = "school" if str(item.get("type")) == "学位论文" else "journal"
            fields.append((key, str(item["source"])))
        if str(item.get("doi") or "").strip():
            fields.append(("doi", str(item["doi"])))
        if str(item.get("url") or "").strip():
            fields.append(("url", str(item["url"])))

        lines = [f"  {name} = {{{_bibtex_escape(value)}}}," for name, value in fields]
        blocks.append(
            "\n".join([f"@{_bibtex_type(item)}{{{_bibtex_key(item, used)},"] + lines + ["}"])
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _render_markdown(items: list[dict]) -> str:
    """导出 Markdown 表格。核验状态单独一列：作者要能一眼看出哪几条没查实。"""
    lines = [
        "# 参考文献",
        "",
        f"共 {len(items)} 条，导出于 {time.strftime('%Y-%m-%d %H:%M:%S')}。",
        "",
        "| # | 题名 | 年份 | 来源 | 类型 | 核验 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for index, item in enumerate(items, start=1):
        verified = str(item.get("verified") or "") or "未核验"
        if item.get("verified_detail"):
            verified += f"（{_snip(str(item['verified_detail']), 40)}）"
        cells = [
            str(index),
            str(item.get("title") or "（无题名）"),
            str(item.get("year") or ""),
            str(item.get("source") or ""),
            str(item.get("type") or ""),
            verified,
        ]
        # 表格里的竖线会撕开单元格，必须转义。
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")

    # 有原文片段的条目附在表格后面：这是"资料获取"相较普通文献管理器的差别所在。
    with_passages = [i for i in items if i.get("passages")]
    if with_passages:
        lines += ["", "## 可引用原文片段", ""]
        for item in with_passages:
            lines.append(f"### {item.get('title') or '（无题名）'}")
            for passage in item.get("passages") or []:
                lines.append(f"- {passage}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_gbt(items: list[dict]) -> str:
    """复用上游的 GB/T 7714 渲染器，逐条把证据转成 ``RefEntry``。

    上游 ``to_gbt`` 只认 ``RefEntry``；证据库里存的是扁平 dict（写作助手直接用），
    所以这里做一层轻量适配，而不是反过来把证据库改成 RefEntry——后者会让
    会话存储依赖上游数据结构。
    """
    from app.agents.references_extract import RefEntry, to_gbt

    lines: list[str] = []
    for index, item in enumerate(items, start=1):
        entry = RefEntry(
            index=index,
            raw=str(item.get("citation") or item.get("title") or ""),
            authors=_authors(str(item.get("authors") or "")),
            year=str(item.get("year") or ""),
            title=str(item.get("title") or ""),
            container=str(item.get("source") or ""),
            doi=str(item.get("doi") or ""),
        )
        lines.append(to_gbt(entry, number=index))
    return "参考文献\n\n" + "\n".join(lines) + "\n" if lines else "参考文献\n\n（证据库为空）\n"


@router.post("/export", response_class=PlainTextResponse)
async def export(request: Request) -> PlainTextResponse:
    """把证据库导出成参考文献表。三种格式都不需要 API Key（纯本地渲染）。"""
    body = await request.json()
    _creds(request)  # 提前校验凭据头：给了不合法的地址要在导出前就说清楚
    fmt = str(body.get("format") or "markdown").strip().lower()
    if fmt not in FORMATS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的导出格式 {fmt!r}，可选：{'、'.join(sorted(FORMATS))}。",
        )

    session = STORE.get(str(body.get("sid") or "").strip() or None)
    items = list(session.evidence) if session else []
    if not items:
        raise HTTPException(status_code=404, detail="证据库是空的：请先抽取资料再导出。")

    renderers = {"gbt": _render_gbt, "bibtex": _render_bibtex, "markdown": _render_markdown}
    text = renderers[fmt](items)
    ext, media = FORMATS[fmt]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    labels = {"gbt": "参考文献-GBT7714", "bibtex": "参考文献-BibTeX", "markdown": "参考文献"}
    filename = f"{labels[fmt]}-{stamp}.{ext}"

    return PlainTextResponse(
        text,
        media_type=media,
        # 文件名带中文：必须用 RFC 5987 的 ``filename*=UTF-8''`` 形式，
        # 直接写进 filename= 会被浏览器按 latin-1 解出乱码（见 review.py 的导出）。
        headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename)},
    )


# ---------------------------------------------------------------------------
# 清空
# ---------------------------------------------------------------------------
@router.delete("/evidence")
def clear_evidence(sid: str = "") -> dict:
    """清空该会话的证据库。

    公网服务必须让访客能**立刻**抹掉自己贴进来的材料，而不是等 TTL 到期
    （见 ``sessions.py``：会话闲置 6 小时才回收，对"我贴错了"来说太久了）。
    """
    session = STORE.get(sid.strip() or None)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在或已过期，没有可清空的数据。")
    removed = len(session.evidence)
    session.evidence.clear()
    session.touch()
    return {"ok": True, "sid": session.sid, "removed": removed}
