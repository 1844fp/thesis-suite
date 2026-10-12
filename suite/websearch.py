"""互联网文献检索：把"网上能搜到的资料"变成可引用的证据。

为什么需要这一层
----------------
原先写作助手有个硬闸门：**证据库为空就拒绝生成**。理由是防止模型凭空编造。
现在允许"不只看证据库"了，但那条防线的目的不能丢——放宽输入必须同时加一道
**可机械检查**的可靠性约束，否则就退化成"模型随便写"。

这一层就是新的约束来源：不再要求证据必须由用户手工录入，而是由本站**真实地去
公开文献库里检索**。检索回来的记录带 DOI、题名、年份、作者和**真实摘要**，
所以：

* 引用是可以回查的（DOI 直接跳原文）；
* 摘要里的数字是真实存在的数字，不是模型编的；
* 查不到就是查不到，如实标注，不假装有依据。

四个源全部是公开 API、**不需要 API Key**，因此不影响本站的 BYOK 定位：

* Crossref   —— 题录最权威（DOI 注册机构），摘要覆盖率一般
* OpenAlex   —— 覆盖广、摘要多（倒排索引，本模块负责还原成文本）
* arXiv      —— 预印本，摘要一定齐全，理工科好用
* Semantic Scholar —— 补充源，摘要与引用数都较全

**安全性**：``base_url`` 可以由访客通过环境变量覆盖，所以这里对地址做了主机白名单
校验，避免把本站当成任意请求的跳板（与 suite/credentials.py 同样的考虑）。
"""

from __future__ import annotations

import html
import os
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

#: 允许被访问的检索主机。可以整体关掉白名单（自建部署时用）。
ALLOW_ANY_HOST = os.environ.get("SUITE_ALLOW_ANY_SEARCH_HOST", "").strip().lower() in {
    "1", "true", "yes", "on",
}

ALLOWED_HOSTS = {
    "api.crossref.org",
    "api.openalex.org",
    "export.arxiv.org",
    "api.semanticscholar.org",
}

#: 礼貌池邮箱。Crossref / OpenAlex 都建议带，能显著降低被限流的概率。
DEFAULT_MAILTO = os.environ.get("SUITE_CROSSREF_MAILTO", "").strip()

#: 单次检索的网络超时。检索失败不该拖垮整篇写作，所以给得比较短。
TIMEOUT = 25.0


@dataclass
class WebRecord:
    """一条网上检索回来的真实文献记录。"""

    source: str                 # crossref / openalex / arxiv / semanticscholar
    title: str = ""
    abstract: str = ""
    authors: str = ""
    year: str = ""
    doi: str = ""
    url: str = ""
    venue: str = ""
    citations: int | None = None
    score: float = 0.0
    retrieved_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))

    @property
    def usable(self) -> bool:
        """能不能当证据用：必须同时有题名和摘要。

        只有题名的记录**不能**作为写作依据——模型看不到内容，只能靠题名猜，
        那正是"编造"的入口。
        """
        return bool(self.title.strip()) and len(self.abstract.strip()) >= 80

    def citation(self) -> str:
        bits = []
        if self.authors:
            bits.append(self.authors)
        if self.title:
            bits.append(self.title)
        tail = []
        if self.venue:
            tail.append(self.venue)
        if self.year:
            tail.append(self.year)
        if self.doi:
            tail.append(f"DOI: {self.doi}")
        head = "．".join(bits)
        return head + (f"[{ '，'.join(tail) }]" if tail else "")

    def as_evidence(self) -> dict:
        """转成 sessions.Session 能存、写作层能用的证据结构。"""
        return {
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "doi": self.doi,
            "url": self.url,
            "source": self.venue or self.source,
            "type": "期刊论文",
            "citation": self.citation(),
            # 只有摘要被当作可引用的原文——我们不抓全文，也不假装有全文。
            "text": self.abstract,
            "passages": [self.abstract],
            "claims": [],
            "verified": "confirmed" if self.doi else "",
            "verified_detail": f"检索自 {self.source}" + (f"，DOI {self.doi}" if self.doi else ""),
            "origin": "web",
            "web_source": self.source,
        }


def _clean_text(raw: str) -> str:
    """去掉 HTML 标签与多余空白。摘要里常混着 <jats:p> 之类的东西。"""
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _abstract_from_inverted(inv: dict | None) -> str:
    """OpenAlex 的摘要存成倒排索引 ``{词: [位置...]}``，这里还原成正常语序。"""
    if not isinstance(inv, dict) or not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        if not isinstance(idxs, list):
            continue
        for i in idxs:
            if isinstance(i, int):
                positions.append((i, word))
    if not positions:
        return ""
    positions.sort()
    return _clean_text(" ".join(word for _, word in positions))


class _Base:
    name = ""
    base_url = ""

    def __init__(self, *, mailto: str = DEFAULT_MAILTO, timeout: float = TIMEOUT) -> None:
        self.mailto = mailto
        self.timeout = timeout
        self.base_url = os.environ.get(f"SUITE_{self.name.upper()}_BASE", self.base_url)
        host = urlparse(self.base_url).hostname or ""
        if not ALLOW_ANY_HOST and host not in ALLOWED_HOSTS:
            raise ValueError(f"{self.name} 的检索地址不在白名单内：{host}")

    def search(self, query: str, *, limit: int = 5) -> list[WebRecord]:
        raise NotImplementedError


class Crossref(_Base):
    name = "crossref"
    base_url = "https://api.crossref.org"

    def search(self, query: str, *, limit: int = 5) -> list[WebRecord]:
        params = {
            "query.bibliographic": query,
            "rows": max(1, min(limit * 2, 20)),
            "select": "DOI,title,author,issued,container-title,abstract,URL,is-referenced-by-count",
        }
        if self.mailto:
            params["mailto"] = self.mailto
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            r = client.get(f"{self.base_url}/works", params=params)
            r.raise_for_status()
            items = (r.json().get("message") or {}).get("items") or []
        out: list[WebRecord] = []
        for item in items:
            titles = item.get("title") or []
            authors = item.get("author") or []
            issued = ((item.get("issued") or {}).get("date-parts") or [[None]])[0]
            out.append(
                WebRecord(
                    source=self.name,
                    title=_clean_text(titles[0] if titles else ""),
                    abstract=_clean_text(item.get("abstract") or ""),
                    authors="、".join(
                        (a.get("family") or "") + (a.get("given") or "") for a in authors[:3]
                    ).strip("、"),
                    year=str(issued[0]) if issued and issued[0] else "",
                    doi=(item.get("DOI") or "").strip(),
                    url=(item.get("URL") or "").strip(),
                    venue=_clean_text((item.get("container-title") or [""])[0]),
                    citations=item.get("is-referenced-by-count"),
                )
            )
        return out


class OpenAlex(_Base):
    name = "openalex"
    base_url = "https://api.openalex.org"

    def search(self, query: str, *, limit: int = 5) -> list[WebRecord]:
        params = {
            "search": query,
            "per-page": max(1, min(limit * 2, 25)),
            "select": "id,doi,title,publication_year,authorships,primary_location,abstract_inverted_index,cited_by_count",
        }
        if self.mailto:
            params["mailto"] = self.mailto
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            r = client.get(f"{self.base_url}/works", params=params)
            r.raise_for_status()
            items = (r.json().get("results") or [])
        out: list[WebRecord] = []
        for item in items:
            loc = item.get("primary_location") or {}
            src = loc.get("source") or {}
            auths = item.get("authorships") or []
            out.append(
                WebRecord(
                    source=self.name,
                    title=_clean_text(item.get("title") or ""),
                    abstract=_abstract_from_inverted(item.get("abstract_inverted_index")),
                    authors="、".join(
                        (a.get("author") or {}).get("display_name", "") for a in auths[:3]
                    ).strip("、"),
                    year=str(item.get("publication_year") or ""),
                    doi=re.sub(r"^https?://doi\.org/", "", str(item.get("doi") or "")),
                    url=str(item.get("id") or ""),
                    venue=_clean_text(src.get("display_name") or ""),
                    citations=item.get("cited_by_count"),
                )
            )
        return out


class Arxiv(_Base):
    name = "arxiv"
    base_url = "https://export.arxiv.org"

    _ENTRY = re.compile(r"<entry>(.*?)</entry>", re.S)

    def search(self, query: str, *, limit: int = 5) -> list[WebRecord]:
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max(1, min(limit * 2, 20)),
            "sortBy": "relevance",
        }
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            r = client.get(f"{self.base_url}/api/query", params=params)
            r.raise_for_status()
            body = r.text
        out: list[WebRecord] = []
        for block in self._ENTRY.findall(body):
            def grab(tag: str) -> str:
                m = re.search(rf"<{tag}>(.*?)</{tag}>", block, re.S)
                return _clean_text(m.group(1)) if m else ""

            title = grab("title")
            summary = grab("summary")
            authors = re.findall(r"<name>(.*?)</name>", block, re.S)
            link = ""
            m = re.search(r'<id>(.*?)</id>', block, re.S)
            if m:
                link = m.group(1).strip()
            published = grab("published")
            out.append(
                WebRecord(
                    source=self.name,
                    title=title,
                    abstract=summary,
                    authors="、".join(_clean_text(a) for a in authors[:3]),
                    year=published[:4] if published else "",
                    doi=_clean_text(grab("arxiv:doi")),
                    url=link,
                    venue="arXiv 预印本",
                )
            )
        return out


class SemanticScholar(_Base):
    name = "semanticscholar"
    base_url = "https://api.semanticscholar.org"

    def search(self, query: str, *, limit: int = 5) -> list[WebRecord]:
        params = {
            "query": query,
            "limit": max(1, min(limit * 2, 20)),
            "fields": "title,abstract,year,authors,venue,externalIds,url,citationCount",
        }
        with httpx.Client(timeout=self.timeout, follow_redirects=True) as client:
            r = client.get(f"{self.base_url}/graph/v1/paper/search", params=params)
            r.raise_for_status()
            items = (r.json().get("data") or [])
        out: list[WebRecord] = []
        for item in items:
            ext = item.get("externalIds") or {}
            out.append(
                WebRecord(
                    source=self.name,
                    title=_clean_text(item.get("title") or ""),
                    abstract=_clean_text(item.get("abstract") or ""),
                    authors="、".join((a.get("name") or "") for a in (item.get("authors") or [])[:3]),
                    year=str(item.get("year") or ""),
                    doi=str(ext.get("DOI") or ""),
                    url=str(item.get("url") or ""),
                    venue=_clean_text(item.get("venue") or ""),
                    citations=item.get("citationCount"),
                )
            )
        return out


#: 检索源清单。
#:
#: **Semantic Scholar 默认关闭**：实测它的匿名额度极小，每次都返回 429，
#: 而重试要白等几秒，把一次本来 6 秒的检索拖成 10 秒以上——而它带不来别的源
#: 没有的东西（OpenAlex 已经覆盖了它的绝大部分）。想开启就设
#: ``SUITE_ENABLE_SEMANTIC_SCHOLAR=1``（有 API Key 时值得开）。
SOURCES: tuple[type[_Base], ...] = (OpenAlex, Crossref, Arxiv)

if os.environ.get("SUITE_ENABLE_SEMANTIC_SCHOLAR", "").strip().lower() in {"1", "true", "yes", "on"}:
    SOURCES = (*SOURCES, SemanticScholar)


def _title_key(title: str) -> str:
    """用于跨源去重的题名指纹。"""
    return re.sub(r"[\s\W_]+", "", (title or "").lower())[:60]


def search_literature(
    queries: list[str],
    *,
    limit_per_source: int = 4,
    max_total: int = 12,
    sources: tuple[type[_Base], ...] = SOURCES,
) -> tuple[list[WebRecord], list[dict]]:
    """按一组检索词去公开文献库找资料。

    返回 ``(可用记录, 各源状态)``。**检索失败不影响其它源**——某个库限流或超时，
    其余源的结果照常返回，失败信息进第二个返回值，由调用方如实展示给用户。

    只返回 ``usable`` 的记录（有题名 + 足够长的摘要）。只有题名的记录一律丢掉：
    模型看不到内容就只能猜，那正是编造的入口。
    """
    found: list[WebRecord] = []
    seen: set[str] = set()
    status: list[dict] = []

    for query in queries:
        query = (query or "").strip()
        if not query:
            continue
        for cls in sources:
            try:
                adapter = cls()
            except ValueError as exc:
                status.append({"source": cls.name, "query": query, "ok": False, "error": str(exc)})
                continue
            started = time.time()
            try:
                records = adapter.search(query, limit=limit_per_source)
            except Exception as exc:  # noqa: BLE001 - 单源失败不能拖垮整次检索
                status.append({
                    "source": cls.name, "query": query, "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"[:200],
                    "seconds": round(time.time() - started, 1),
                })
                continue

            used = 0
            for rec in records:
                if not rec.usable:
                    continue
                key = _title_key(rec.title)
                if not key or key in seen:
                    continue
                seen.add(key)
                found.append(rec)
                used += 1
                if len(found) >= max_total:
                    break

            status.append({
                "source": cls.name, "query": query, "ok": True,
                "found": len(records), "used": used,
                "seconds": round(time.time() - started, 1),
            })
            if len(found) >= max_total:
                break
        if len(found) >= max_total:
            break

    # 有 DOI 的优先（可回查），其次按被引数排序——被引多通常意味着更可靠。
    found.sort(key=lambda r: (bool(r.doi), r.citations or 0), reverse=True)
    return found[:max_total], status
