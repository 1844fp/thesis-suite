"""资料获取与外部核验适配器。

分三块，对应"资料获取"这一能力的两端：

* ``LocalIntake``      —— 本地收稿：扫目录、解析 **题目_作者** 命名、去重、给出待入库清单
* ``CrossrefAdapter``  —— 外部核验主力：按 DOI / 标题查真实文献存在性
* ``OpenAlexAdapter``  —— 外部核验补位：Crossref 偏英文，OpenAlex 覆盖更广且免费无 key
* ``ArxivAdapter``     —— 预印本检索（开放、无需 key，适合补充资料）

**为什么核验要用两个外部源：** 单源查不到不等于文献不存在（Crossref 对中文期刊、
会议论文、书籍章节的覆盖有缺口）。只用一个源会把真实文献误报为"编造"，
这种误报会直接浪费作者的信任。两源都查不到才升级为高严重度问题。
"""

from __future__ import annotations

import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

import httpx

from .base import PeerError, PeerHTTP

# ---------------------------------------------------------------------------
# 本地收稿
# ---------------------------------------------------------------------------

#: 题目与作者之间允许的分隔符。与 thesis-assistant 的命名约定保持一致，
#: 这样两个系统的库能对得上。
_SEPARATORS = ("_", "+", "|", "—", "–", " - ", "-")
_DECORATION = re.compile(r"^[【\[（(][^】\]）)]{0,20}[】\]）)]")
# 注意 `[^】\]）)]`：`）`(U+FF09) 与 `)`(U+0029) 之间隔着 `\]`，是安全的。
# 但若写成紧邻形式 `[^】）)]`，Python 会把 `）)` 当成从 U+FF09 到 U+0029 的
# 字符区间——起点大于终点，结果是「除全角右括号外什么都匹配」，正则静默失效。
# 同理 `[【\[（(]` 里也必须靠 `\[` 隔开。
_COPY_MARK = re.compile(r"[（(]\d+[)）]$|-?\s*副本$|-?\s*copy$", re.I)
_YEAR = re.compile(r"(19|20)\d{2}")


@dataclass
class IntakeItem:
    path: str
    filename: str
    title: str = ""
    author: str = ""
    year: str = ""
    bytes: int = 0
    parse_note: str = ""
    #: 是否可交给知识库入库（PDF / DOCX 才可）
    ingestible: bool = True

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "filename": self.filename,
            "title": self.title,
            "author": self.author,
            "year": self.year,
            "bytes": self.bytes,
            "parse_note": self.parse_note,
            "ingestible": self.ingestible,
        }

    @property
    def label(self) -> str:
        if self.author:
            return f"{self.title}_{self.author}"
        return self.title or self.filename


def parse_filename(stem: str) -> tuple[str, str, str]:
    """Split ``题目_作者`` into ``(title, author, year)``.

    Deliberately conservative: when the shape is not recognised, everything goes
    into the title and the author stays empty. Guessing an author from a stray
    token produces wrong library metadata that is worse than no metadata.
    """
    s = _COPY_MARK.sub("", (stem or "").strip()).strip()
    s = _DECORATION.sub("", s).strip()
    if not s:
        return "", "", ""

    # 作者_年份_题目（旧写法）
    parts = re.split(r"[_+|]", s)
    if len(parts) >= 3 and _YEAR.fullmatch(parts[1].strip()):
        author = parts[0].strip()
        year = parts[1].strip()
        title = "_".join(parts[2:]).strip()
        return title, author, year

    # 题目_作者_年份（常见写法，年份在末尾）。
    # 必须放在通用分隔符规则之前，否则 rpartition 会把作者和年份一起当成作者。
    if len(parts) >= 3 and _YEAR.fullmatch(parts[-1].strip()):
        author = parts[-2].strip()
        year = parts[-1].strip()
        title = "_".join(parts[:-2]).strip()
        if author:
            return title, author, year

    for sep in _SEPARATORS:
        if sep not in s:
            continue
        head, _, tail = s.rpartition(sep)
        head, tail = head.strip(), tail.strip()
        if not head or not tail:
            continue
        # 作者一般不长，且不含句子级标点
        if len(tail) > 20 or any(c in tail for c in "。！？；：，,."):
            continue
        year = ""
        m = _YEAR.search(tail)
        if m:
            year = m.group(0)
            tail = _YEAR.sub("", tail).strip(" _-·")
        return head, tail, year

    return s, "", ""


class LocalIntake:
    """扫描本地资料目录，产出待入库清单。

    不做入库本身——入库是对端 RAG 服务的职责（它有解析器、章节重建、
    向量化）。这里只负责"目录里有什么、叫什么、值不值得入库"。
    """

    #: 能交给 RAG 服务解析的扩展名
    INGESTIBLE = {".pdf", ".docx", ".txt", ".md"}
    #: 看得见但不能入库的（知网格式需要先转换）
    KNOWN_UNSUPPORTED = {".caj", ".nh", ".kdh", ".teb"}

    def __init__(self, roots: Iterable[str | Path]) -> None:
        self.roots = [Path(r) for r in roots]

    def scan(self, *, pattern: str = "**/*") -> list[IntakeItem]:
        items: list[IntakeItem] = []
        seen: set[str] = set()
        for root in self.roots:
            if not root.exists():
                continue
            for p in sorted(root.glob(pattern)):
                if not p.is_file():
                    continue
                ext = p.suffix.lower()
                item = IntakeItem(
                    path=str(p),
                    filename=p.name,
                    bytes=p.stat().st_size,
                )
                title, author, year = parse_filename(p.stem)
                item.title = title or p.stem
                item.author = author
                item.year = year

                if ext in self.INGESTIBLE:
                    item.ingestible = True
                    if not author:
                        item.parse_note = "文件名里没有识别出作者，入库后由正文抽取"
                elif ext in self.KNOWN_UNSUPPORTED:
                    item.ingestible = False
                    item.parse_note = f"{ext} 是知网格式，需先转成 PDF 再入库"
                else:
                    item.ingestible = False
                    item.parse_note = f"{ext} 不是可入库的文档格式"

                # 同一路径只收一次（roots 可能嵌套）
                key = str(p.resolve()).lower()
                if key in seen:
                    continue
                seen.add(key)
                items.append(item)
        return items

    def summary(self, items: list[IntakeItem] | None = None) -> dict:
        rows = items if items is not None else self.scan()
        by_ext: dict[str, int] = {}
        for r in rows:
            ext = Path(r.filename).suffix.lower() or "(无扩展名)"
            by_ext[ext] = by_ext.get(ext, 0) + 1
        return {
            "total": len(rows),
            "ingestible": sum(1 for r in rows if r.ingestible),
            "skipped": sum(1 for r in rows if not r.ingestible),
            "with_author": sum(1 for r in rows if r.author),
            "by_ext": by_ext,
            "roots": [str(r) for r in self.roots if r.exists()],
        }


# ---------------------------------------------------------------------------
# 标题归一与相似度（外部核验的判定核心）
# ---------------------------------------------------------------------------


def normalize_title(title: str) -> str:
    """Fold a title for comparison: case, width, punctuation, whitespace.

    Without this, "Deep Learning for NLP: A Survey" and
    "Deep learning for NLP a survey" score as different works and every
    correctly-cited paper gets flagged.
    """
    t = unicodedata.normalize("NFKC", title or "").lower()
    t = re.sub(r"[\s\u3000]+", " ", t)
    t = re.sub(r"[^\w\u4e00-\u9fff ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # 子串包含是很强的信号（副标题、截断、补充说明）。
    # 阈值取 6 而不是更长：中文标题本身很短，「注意力机制研究」这类
    # 7 字标题若要求 12 字才认包含关系，正确的引用会被误判为不存在。
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 6 and shorter in longer:
        return max(ratio, 0.93)
    return ratio


# ---------------------------------------------------------------------------
# Crossref
# ---------------------------------------------------------------------------


#: 低于这个相似度的检索结果视为"不相关"，而不是"弱匹配"。
#:
#: 实测标定（见 tools\\smoke_adapters.py --external）：编造标题的最佳匹配落在
#: 0.27~0.44，真实英文标题 0.93~1.00，真实中文标题 Crossref 0.65 / OpenAlex 0.10。
#: 取 0.55 把"编造"与"真实但中文覆盖差"分开——**不能取到 0.8 以上**，否则把
#: 真实的中文文献误报成编造，这种误报会直接毁掉作者对工具的信任。
MIN_RELEVANCE = 0.55


@dataclass
class WorkRecord:
    """一条外部文献记录。"""

    found: bool
    source: str
    query: str = ""
    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: str = ""
    container: str = ""
    doi: str = ""
    url: str = ""
    score: float = 0.0
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "found": self.found,
            "source": self.source,
            "query": self.query,
            "title": self.title,
            "authors": self.authors,
            "year": self.year,
            "container": self.container,
            "doi": self.doi,
            "url": self.url,
            "score": round(self.score, 3),
            "note": self.note,
        }


class CrossrefAdapter(PeerHTTP):
    """Crossref REST API。不需要 key，但礼貌池要求带联系邮箱。"""

    label = "Crossref"
    start_hint = "检查网络连接；离线环境可在设置里关闭外部核验。"

    def __init__(self, base_url: str = "https://api.crossref.org", *, timeout: float = 30.0,
                 mailto: str = "") -> None:
        super().__init__(base_url, timeout=timeout)
        self.mailto = (mailto or "").strip()
        self._last = 0.0
        #: 429 退避重试次数。批量核验时并发稍高就会被限流（实测 4 并发即可触发），
        #: 没有重试会把"被限流"误报成"文献不存在"——这个后果比慢几十秒严重得多。
        self.retries = 4

    def _throttle(self, delay: float = 0.5) -> None:
        # 0.5s ≈ 2 请求/秒。Crossref 礼貌池在带 mailto 时更宽松，但实测多线程
        # 并发下仍会 429；取这个值换取"不靠重试也能过"。
        gap = time.time() - self._last
        if gap < delay:
            time.sleep(delay - gap)
        self._last = time.time()

    def _params(self, extra: dict) -> dict:
        p = dict(extra)
        if self.mailto:
            p["mailto"] = self.mailto
        return p

    def _get_json(self, path: str, params: dict) -> dict:
        url = self.base_url + path
        headers = {"User-Agent": f"thesis-agents/0.1 (mailto:{self.mailto or 'unset'})"}

        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout, headers=headers) as client:
                    resp = client.get(url, params=self._params(params))
            except httpx.HTTPError as exc:
                raise PeerError(
                    f"Crossref 请求失败：{type(exc).__name__}: {exc}", kind="network"
                ) from exc

            if resp.status_code == 404:
                return {}
            # 429/5xx 是暂时性失败：退避重试，重试仍失败才抛。
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt < self.retries:
                    time.sleep(2.0 * (2**attempt))
                    continue
                raise PeerError(
                    f"Crossref 返回 HTTP {resp.status_code}（已重试 {self.retries} 次）",
                    kind="rate_limit" if resp.status_code == 429 else "http",
                    status=resp.status_code,
                )
            if resp.status_code >= 400:
                raise PeerError(
                    f"Crossref 返回 HTTP {resp.status_code}：{resp.text[:200]}",
                    kind="http",
                    status=resp.status_code,
                )
            try:
                return resp.json()
            except ValueError as exc:
                raise PeerError("Crossref 返回的不是 JSON", kind="protocol") from exc
        return {}

    @staticmethod
    def _clean_doi(doi: str) -> str:
        d = (doi or "").strip()
        d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d, flags=re.I)
        d = re.sub(r"^doi:\s*", "", d, flags=re.I)
        return d.strip().strip(".,;)")

    def by_doi(self, doi: str) -> WorkRecord:
        d = self._clean_doi(doi)
        if not d:
            return WorkRecord(found=False, source="crossref", query=doi, note="DOI 为空")
        self._throttle()
        # 不捕获 PeerError——理由同 OpenAlex.by_doi：把"查不了"转成"不存在"
        # 会让核验给出反向结论。让调用方区分"查询失败"与"确实没有"。
        data = self._get_json(f"/works/{d}", {})
        msg = (data or {}).get("message")
        if not msg:
            return WorkRecord(found=False, source="crossref", query=d, note="Crossref 无此 DOI")
        return self._from_message(msg, query=d, score=1.0)

    def by_title(self, title: str, *, rows: int = 3) -> WorkRecord:
        """Bibliographic query. Returns the best match with its similarity score."""
        q = (title or "").strip()
        if len(q) < 8:
            return WorkRecord(found=False, source="crossref", query=q, note="标题过短，跳过检索")
        self._throttle()
        # 同样不吞 PeerError：查询失败必须冒泡，由调用方判成 inconclusive。
        data = self._get_json(
            "/works",
            {"query.bibliographic": q, "rows": rows, "select": "DOI,title,author,issued,container-title,URL"},
        )
        items = ((data or {}).get("message") or {}).get("items") or []
        best: WorkRecord | None = None
        for it in items:
            title_it = " ".join(it.get("title") or [])
            rec = self._from_message(it, query=q, score=title_similarity(q, title_it))
            if best is None or rec.score > best.score:
                best = rec
        if best is None:
            return WorkRecord(found=False, source="crossref", query=q, note="Crossref 无结果")
        # 返回了结果 ≠ 匹配上了。相关度过低说明这条结果与引用无关，
        # 把它算作"找到"会让编造的文献蒙混过关。
        if best.score < MIN_RELEVANCE:
            return WorkRecord(
                found=False,
                source="crossref",
                query=q,
                score=best.score,
                note=f"最相似结果为「{best.title[:60]}」，相似度 {best.score:.2f}，低于相关下限 {MIN_RELEVANCE}",
            )
        best.found = True
        return best

    @staticmethod
    def _from_message(msg: dict, *, query: str, score: float) -> WorkRecord:
        title = " ".join(msg.get("title") or []) or ""
        authors: list[str] = []
        for a in msg.get("author") or []:
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
            if not name:
                name = a.get("name") or ""
            if name:
                authors.append(name)
        year = ""
        issued = (msg.get("issued") or {}).get("date-parts") or []
        if issued and issued[0]:
            year = str(issued[0][0])
        container = " ".join(msg.get("container-title") or []) or ""
        return WorkRecord(
            found=True,
            source="crossref",
            query=query,
            title=title,
            authors=authors,
            year=year,
            container=container,
            doi=msg.get("DOI") or "",
            url=msg.get("URL") or "",
            score=score,
        )


# ---------------------------------------------------------------------------
# OpenAlex
# ---------------------------------------------------------------------------


class OpenAlexAdapter(PeerHTTP):
    """OpenAlex：免费、无 key、覆盖比 Crossref 更广（含中文期刊与书籍章节）。"""

    label = "OpenAlex"
    start_hint = "检查网络连接。"

    def __init__(self, base_url: str = "https://api.openalex.org", *, timeout: float = 30.0,
                 mailto: str = "") -> None:
        super().__init__(base_url, timeout=timeout)
        self.mailto = (mailto or "").strip()
        self._last = 0.0
        #: 429 退避重试次数。批量核验时并发稍高就会被限流，没有重试会把
        #: "被限流"误报成"文献不存在"——这个后果比慢几秒严重得多。
        self.retries = 3

    def _throttle(self, delay: float = 0.34) -> None:
        # 0.34s ≈ 3 请求/秒。OpenAlex 官方建议不超过 10/s，但实测在多线程
        # 并发下 0.15s 仍会触发 429，取更保守的值换取"不重试也能过"。
        gap = time.time() - self._last
        if gap < delay:
            time.sleep(delay - gap)
        self._last = time.time()

    def _get_json(self, path: str, params: dict) -> dict:
        p = dict(params)
        if self.mailto:
            p["mailto"] = self.mailto
        url = self.base_url + path
        headers = {"User-Agent": f"thesis-agents/0.1 (mailto:{self.mailto or 'unset'})"}

        last_status = 0
        for attempt in range(self.retries + 1):
            try:
                with httpx.Client(timeout=self.timeout, headers=headers) as client:
                    resp = client.get(url, params=p)
            except httpx.HTTPError as exc:
                raise PeerError(f"OpenAlex 请求失败：{type(exc).__name__}: {exc}", kind="network") from exc

            if resp.status_code == 404:
                return {}
            # 429/5xx 是**暂时性**失败，退避后重试；重试仍失败才向上抛。
            if resp.status_code == 429 or resp.status_code >= 500:
                last_status = resp.status_code
                if attempt < self.retries:
                    time.sleep(1.5 * (2**attempt))
                    continue
                raise PeerError(
                    f"OpenAlex 返回 HTTP {last_status}（已重试 {self.retries} 次）",
                    kind="rate_limit" if last_status == 429 else "http",
                    status=last_status,
                )
            if resp.status_code >= 400:
                raise PeerError(
                    f"OpenAlex 返回 HTTP {resp.status_code}", kind="http", status=resp.status_code
                )
            try:
                return resp.json()
            except ValueError as exc:
                raise PeerError("OpenAlex 返回的不是 JSON", kind="protocol") from exc
        return {}

    def by_doi(self, doi: str) -> WorkRecord:
        d = CrossrefAdapter._clean_doi(doi)
        if not d:
            return WorkRecord(found=False, source="openalex", query=doi, note="DOI 为空")
        self._throttle()
        # 刻意**不**捕获 PeerError。早期版本在这里把异常转成 `found=False` 的记录，
        # 结果是限流/断网被静默表达成"这篇文献不存在"——一个把"查不了"伪装成
        # "没有"的错误结论。调用方必须能区分两者，所以让异常冒泡。
        data = self._get_json(f"/works/https://doi.org/{d}", {})
        if not data or not data.get("id"):
            return WorkRecord(found=False, source="openalex", query=d, note="OpenAlex 无此 DOI")
        return self._from_work(data, query=d, score=1.0)

    def by_title(self, title: str, *, per_page: int = 3) -> WorkRecord:
        q = (title or "").strip()
        if len(q) < 8:
            return WorkRecord(found=False, source="openalex", query=q, note="标题过短，跳过检索")
        self._throttle()
        # 同上：不吞 PeerError，让"查询失败"与"确实没有"在调用方分开处理。
        data = self._get_json("/works", {"search": q, "per-page": per_page})
        results = (data or {}).get("results") or []
        best: WorkRecord | None = None
        for it in results:
            rec = self._from_work(
                it, query=q, score=title_similarity(q, it.get("display_name") or "")
            )
            if best is None or rec.score > best.score:
                best = rec
        if best is None:
            return WorkRecord(found=False, source="openalex", query=q, note="OpenAlex 无结果")
        if best.score < MIN_RELEVANCE:
            return WorkRecord(
                found=False,
                source="openalex",
                query=q,
                score=best.score,
                note=f"最相似结果为「{best.title[:60]}」，相似度 {best.score:.2f}，低于相关下限 {MIN_RELEVANCE}",
            )
        best.found = True
        return best

    @staticmethod
    def _from_work(it: dict, *, query: str, score: float) -> WorkRecord:
        authors: list[str] = []
        for a in it.get("authorships") or []:
            name = ((a.get("author") or {}).get("display_name")) or ""
            if name:
                authors.append(name)
        year = str(it.get("publication_year") or "")
        host = ((it.get("primary_location") or {}).get("source") or {}).get("display_name") or ""
        return WorkRecord(
            found=True,
            source="openalex",
            query=query,
            title=it.get("display_name") or "",
            authors=authors,
            year=year,
            container=host,
            doi=CrossrefAdapter._clean_doi(it.get("doi") or ""),
            url=it.get("id") or "",
            score=score,
        )


# ---------------------------------------------------------------------------
# arXiv
# ---------------------------------------------------------------------------

_ATOM = "{http://www.w3.org/2005/Atom}"


class ArxivAdapter(PeerHTTP):
    """arXiv Atom API：开放获取，无需 key。适合补充预印本资料。"""

    label = "arXiv"
    start_hint = "检查网络连接。"

    def __init__(self, base_url: str = "http://export.arxiv.org", *, timeout: float = 30.0) -> None:
        super().__init__(base_url, timeout=timeout)

    def search(self, query: str, *, max_results: int = 10) -> list[dict]:
        url = self.base_url + "/api/query"
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                xml = resp.text
        except httpx.HTTPError as exc:
            raise PeerError(f"arXiv 请求失败：{type(exc).__name__}: {exc}", kind="network") from exc

        try:
            root = ET.fromstring(xml)
        except ET.ParseError as exc:
            raise PeerError("arXiv 返回的不是合法 XML", kind="protocol") from exc

        out: list[dict] = []
        for entry in root.findall(f"{_ATOM}entry"):
            def txt(tag: str) -> str:
                node = entry.find(f"{_ATOM}{tag}")
                return (node.text or "").strip() if node is not None else ""

            authors = [
                (a.find(f"{_ATOM}name").text or "").strip()
                for a in entry.findall(f"{_ATOM}author")
                if a.find(f"{_ATOM}name") is not None
            ]
            out.append(
                {
                    "title": re.sub(r"\s+", " ", txt("title")),
                    "summary": re.sub(r"\s+", " ", txt("summary")),
                    "published": txt("published")[:10],
                    "updated": txt("updated")[:10],
                    "id": txt("id"),
                    "authors": authors,
                    "source": "arxiv",
                }
            )
        return out


# ---------------------------------------------------------------------------
# 组合：多源核验
# ---------------------------------------------------------------------------


class ExternalVerifier:
    """查 DOI / 标题，返回**所有源**的结论。

    判定规则（``not_found`` 的门槛刻意设得很严）：

    * 任一源成功且相似度达标 → ``confirmed``
    * 没有达标命中，但**至少一个源成功完成了查询**且它没找到 → ``not_found``
    * **没有任何源查成**（全部限流/断网） → ``inconclusive``

    第三档是这里最要紧的一条，早期版本漏了它：当时只在"一个记录都没有"时才判
    ``inconclusive``，于是**一个源报错、另一个源成功但未命中**的组合会落进
    ``not_found``——等于让一条失败记录覆盖掉一次成功查询，把"查不了"说成"不存在"。
    实测后果：一批客观存在的英文经典文献（Jones & Tonetti 2020 AER、Vial 2019 JSIS）
    在 OpenAlex 被限流时被整批判成"疑似编造"。
    **误告造假比漏报严重得多**，所以拿不准时一律判"无法确认"。

    反过来也不能要求"每个源都答上来"才判 ``not_found``：那样只要有一个源不可用，
    整批核验就全变成"无法确认"，功能等于失效。``not_found`` 的依据是**有源明确
    回复"没有"**——一次成功的查询本身就是证据。
    """

    def __init__(
        self,
        *,
        crossref: CrossrefAdapter | None = None,
        openalex: OpenAlexAdapter | None = None,
        threshold: float = 0.82,
    ) -> None:
        self.sources: list[PeerHTTP] = [a for a in (crossref, openalex) if a is not None]
        self.threshold = threshold

    def verify(self, *, doi: str = "", title: str = "") -> dict:
        records: list[WorkRecord] = []
        errors: list[str] = []
        answered = 0  # 成功完成查询的源数量

        for src in self.sources:
            try:
                if doi:
                    rec = src.by_doi(doi)
                    if not rec.found:
                        # DOI 查不到时用标题兜底：印刷版 DOI 常有错字
                        rec = src.by_title(title) if title else rec
                elif title:
                    rec = src.by_title(title)
                else:
                    rec = WorkRecord(found=False, source=src.label, note="没有可检索的 DOI 或标题")
            except PeerError as exc:
                errors.append(f"{src.label}: {exc}")
                continue
            except Exception as exc:  # noqa: BLE001 - 任何异常都算"这个源没答上来"
                errors.append(f"{src.label}: {type(exc).__name__}: {exc}")
                continue
            answered += 1
            records.append(rec)

        confirmed = [r for r in records if r.found and r.score >= self.threshold]
        weak = [r for r in records if r.found and r.score < self.threshold]
        total = len(self.sources)

        if confirmed:
            best = max(confirmed, key=lambda r: r.score)
            verdict = "confirmed"
        elif weak:
            best = max(weak, key=lambda r: r.score)
            verdict = "weak_match"
        elif answered == 0:
            # 没有任何源查成 → 只能说"没能确认"，不能说"不存在"。
            best = WorkRecord(
                found=False,
                source="none",
                query=doi or title,
                note=(
                    f"{total} 个数据源全部查询失败，无法确认"
                    + (f"（{'; '.join(errors[:2])}）" if errors else "")
                ),
            )
            verdict = "inconclusive"
        else:
            best = WorkRecord(found=False, source="none", query=doi or title, note="所有可用源都未找到")
            verdict = "not_found"
            # 中文文献在 Crossref/OpenAlex 的覆盖率明显偏低。查不到时给出这个
            # 提醒，避免作者把"没收录"误读成"我引了假文献"。
            if re.search(r"[\u4e00-\u9fff]", title or ""):
                best.note += "；该标题含中文，Crossref/OpenAlex 对中文文献收录有限，建议再用知网/万方核实"
            if errors:
                # 有源没答上来时，把这件事如实写进说明——结论仍成立，
                # 但作者有权知道它不是基于全部数据源。
                best.note += f"；另有 {total - answered} 个源查询失败（{errors[0][:60]}）"

        return {
            "verdict": verdict,
            "threshold": self.threshold,
            "best": best.as_dict(),
            "records": [r.as_dict() for r in records],
            "errors": errors,
            "answered": answered,
            "sources_total": total,
            "queried": {"doi": doi, "title": title},
        }
