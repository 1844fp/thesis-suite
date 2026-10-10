"""全局证据池、引用编号与参考文献表。

## 这个模块解决的真实缺陷

起草阶段原先按**章**检索证据，每章的编号都从 ``[1]`` 开始。单章看没问题，
但整篇论文合并成一份稿件之后：

    绪论：  [1] = 杨东（2024）
    综述：  [1] = 陈荣达（2024）      ← 同一个编号指向了不同文献

一份学位论文只能有一张参考文献表，所以 ``[n]`` 必须是**全篇唯一**的。这不是
排版问题，是引用错误：读者按编号回查文献表会查到另一篇论文。

修法不是"最后重排一下编号"（那只是把错误藏起来），而是**从检索那一刻起就给
每条证据分配全篇唯一的编号**：

* ``EvidencePool`` 是项目级的证据池，编号单调递增、只增不改；
* 同一篇文献的同一段原文被两章分别检索到时，**返回同一个编号**（按 key 去重）；
* 校验角标时仍然只认"本次提示词里给过模型的那几个编号"——编号是全局的，
  但可见范围是局部的，这样模型既编不出池外的号，也编不出它没见过的号。

## 参考文献表

写作产生的角标编号会随写作顺序增长，中间必然有"检索到了但没用上"的空号。
所以定稿前要做一次**确定性重排**：按角标在正文中**首次出现的顺序**重新编成
连续编号，再据此生成参考文献表。这一步是纯文本变换，可复验（见 ``renumber``）。

表里**不补全任何字段**。RAG 的文档元数据里没有期刊名、卷期、文献类型标识，
那就空着并逐条列出来——一份"排版整齐但字段是编的"参考文献表，比一份明确
标出缺什么更危险。（与 ``app/agents/references_build.py`` 同一条纪律。）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# ---------------------------------------------------------------------------
# 角标：唯一的正则定义处
# ---------------------------------------------------------------------------

#: 引用角标：``[1]`` / ``[1,2]`` / ``[1，2]`` / ``[3-5]`` / ``[3–5]``。
#:
#: **这是全系统唯一一份**。起草校验、润色校验、定稿重排如果各写一份正则，
#: 三份不一致时无法判断谁对——与"降 AIGC 指数只能由一个系统测量"同理。
CITE_MARKER = re.compile(r"\[(\d+(?:\s*[-–~]\s*\d+|(?:\s*[,，、]\s*\d+)*))\]")

#: 形如 ``[1] 作者. 题名[J]. 期刊, 2020.`` 的行 = 参考文献条目，不是正文角标。
REF_ENTRY = re.compile(r"^\s*[\[［]?\d{1,3}[\]］]?\s*[.、]?\s*\S")
#: 必须带 ``re.M``：标题行是"整行等于这几个字"，漏掉会让参考文献区永远检测不到。
#:
#: ``#{0,6}`` 这一段是**必须的，不是可选的**：本系统生成的稿件用 Markdown，
#: 文献表标题写成 ``# 参考文献``。原先的正则不认这个 ``#``，后果是连锁的：
#:
#: * 定稿重排时旧文献表没被切掉，于是**追加出第二份参考文献表**；
#: * 文献条目 ``[1] 张三. …`` 被当成正文角标，角标校验把它判成"模型编造引用"；
#: * 结论章的"数字必须在正文出现"检查把文献表里的年份页码也算进正文。
#:
#: 这三条都是**静默**发生的——输出看着还挺正常，只是内容是错的。
#: 所以任何新的稿件格式（比如将来换成 OOXML 抽取）都必须先回到这里确认标题行能被识别。
REF_HEADING = re.compile(
    r"^[ \t]*#{0,6}[ \t]*(参考文献|references|bibliography|引用文献|文献列表)[ \t]*[:：]?[ \t]*$",
    re.I | re.M,
)


def parse_marker(body: str) -> list[int]:
    """把角标内部字符串拆成编号组：``"1,2"`` → [1,2]，``"3-5"`` → [3,4,5]。"""
    out: list[int] = []
    body = body.replace("，", ",").replace("、", ",").replace("–", "-").replace("~", "-")
    for chunk in body.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, _, b = chunk.partition("-")
            try:
                lo, hi = int(a), int(b)
            except ValueError:
                continue
            # 范围上限保护：模型写出 [1-9999] 时不要展开成上千个编号。
            if lo <= hi and hi - lo <= 50:
                out.extend(range(lo, hi + 1))
            elif lo <= hi:
                out.append(lo)
        else:
            try:
                out.append(int(chunk))
            except ValueError:
                continue
    return out


def is_ref_entry(line: str) -> bool:
    """粗判一行是不是参考文献条目（只在"已进入参考文献区"之后使用）。"""
    return bool(REF_ENTRY.match(line))


def find_references_block(text: str) -> tuple[int, int] | None:
    """返回文末参考文献区的 ``(start, end)`` 字符偏移；找不到返回 None。

    参考文献条目本身就是 ``[1] 张三. ……`` 的形式，与正文角标完全同形。不排除这一段，
    角标校验会把每条文献的编号都报成"模型编造引用"。只认**文末**那一段，且必须
    满足"标题行 + 连续编号条目"两个条件，正文里偶然出现"参考文献"四个字不会误判。
    """
    if not text:
        return None
    lines = text.split("\n")
    for idx in range(len(lines) - 1, -1, -1):
        if not REF_HEADING.match(lines[idx].strip()):
            continue
        probe = [ln for ln in lines[idx + 1 :] if ln.strip()][:4]
        if probe and sum(1 for ln in probe if is_ref_entry(ln)) >= max(1, len(probe) // 2):
            start = sum(len(ln) + 1 for ln in lines[:idx])
            return start, len(text)
    return None


def drop_marker(text: str, start: int, end: int) -> str:
    """删除一个角标并顺手清掉它留下的空白/悬空标点。

    只删角标本身，**不改写句子**。留下的轻微不顺畅是作者可见的编辑痕迹：
    相比"让模型再润一句"，作者一眼看出"这里原来有个引用被删了"更安全。
    """
    left = text[:start].rstrip(" \t")
    right = text[end:].lstrip(" \t")
    if left.endswith("，") and right[:1] in "。；，":
        left = left[:-1]
    return left + right


def tidy_line(line: str) -> str:
    """删除角标后清理一行里的残留空格：行内多空格压成一个，并去掉行尾空白。

    **只处理行内**。跨行处理会碰到换行符，而换行符是段落结构的一部分；
    "删掉一个角标"绝不应该改变稿件的分段。
    """
    return re.sub(r"[ \t]{2,}", " ", line).rstrip()


def litter(text: str, *, skip_references: bool = True) -> Iterable[tuple[int, int, list[int]]]:
    """遍历文本里的角标，产出 ``(start, end, 编号列表)``。

    编号大到不像引用的（如 ``[2024]`` 年份）一律跳过：校验器只对"看起来确实是
    引用角标"的东西负责，避免把作者的年份写成越界。
    """
    head = text
    if skip_references:
        block = find_references_block(text)
        if block:
            head = text[: block[0]]
    for m in CITE_MARKER.finditer(head):
        nums = [n for n in parse_marker(m.group(1)) if n > 0]
        if not nums or nums[0] > 9999:
            continue
        yield m.start(), m.end(), nums


# ---------------------------------------------------------------------------
# 证据条目与证据池
# ---------------------------------------------------------------------------


def entry_key(*, doc_id: str, text: str, page: Any = None) -> str:
    """去重键：同一篇文档的同一段原文算同一条证据。

    带上页码是因为同一段文字在不同页出现时（页眉/重复段落）应当分开计——
    合并会让作者按引用回查时翻到错误的一页。
    """
    digest = hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()[:16]
    return f"{doc_id or 'unknown'}#{page if page is not None else '-'}#{digest}"


@dataclass
class EvidenceEntry:
    """一条证据。``index`` 是**全篇唯一**的编号，一经分配不再改变。"""

    index: int
    key: str
    citation: str = ""
    text: str = ""
    doc_id: str = ""
    section: str = ""
    page: int | None = None
    score: float = 0.0
    author: str = ""
    year: str = ""
    title: str = ""
    source_path: str = ""

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "key": self.key,
            "citation": self.citation,
            "text": self.text,
            "doc_id": self.doc_id,
            "section": self.section,
            "page": self.page,
            "score": round(float(self.score or 0.0), 4),
            "author": self.author,
            "year": self.year,
            "title": self.title,
            "source_path": self.source_path,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EvidenceEntry":
        return cls(
            index=int(d.get("index") or 0),
            key=str(d.get("key") or ""),
            citation=str(d.get("citation") or ""),
            text=str(d.get("text") or ""),
            doc_id=str(d.get("doc_id") or ""),
            section=str(d.get("section") or ""),
            page=d.get("page"),
            score=float(d.get("score") or 0.0),
            author=str(d.get("author") or ""),
            year=str(d.get("year") or ""),
            title=str(d.get("title") or ""),
            source_path=str(d.get("source_path") or ""),
        )


#: 证据池在项目 meta 里的键名。跨阶段契约，改名会同时影响起草与定稿。
POOL_META_KEY = "evidence_pool"


class EvidencePool:
    """项目级证据池：编号全篇唯一、单调递增、只增不改。

    为什么坚持"只增不改"：稿子里的角标 ``[7]`` 必须永远指向同一条证据。
    如果允许重排或复用编号，作者昨天看到的 ``[7]`` 和今天看到的就不是一回事了。
    定稿时的连续化重排（``renumber``）是显式的、一次性的、有映射表可查的动作，
    与"池内编号悄悄变化"是两件不同的事。
    """

    def __init__(self, entries: Sequence[EvidenceEntry] | None = None) -> None:
        self.entries: list[EvidenceEntry] = list(entries or [])
        self._by_key: dict[str, int] = {e.key: e.index for e in self.entries}
        self._next = (max((e.index for e in self.entries), default=0) + 1)

    # -- 读 ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.entries)

    def get(self, index: int) -> EvidenceEntry | None:
        for e in self.entries:
            if e.index == index:
                return e
        return None

    def by_key(self, key: str) -> EvidenceEntry | None:
        idx = self._by_key.get(key)
        return self.get(idx) if idx is not None else None

    def indices(self) -> set[int]:
        return {e.index for e in self.entries}

    # -- 写 ---------------------------------------------------------------
    def add_hits(self, hits: Sequence[Any], *, limit: int = 0) -> tuple[list[EvidenceEntry], int]:
        """把检索命中并入池子，返回 ``(本次可用的条目, 重复条数)``。

        返回的是"本次真正新增 + 命中已存在的"全部条目——调用方要的是"这几条证据
        的编号是多少"，不是"池子长了多少"。重复命中不新开编号，这正是跨章引用
        同一篇文献时编号保持一致的原因。
        """
        out: list[EvidenceEntry] = []
        dup = 0
        for h in hits or []:
            if limit and len(out) >= limit:
                break
            text = str(getattr(h, "text", "") or "").strip()
            if not text:
                continue
            doc_id = str(getattr(h, "doc_id", "") or "")
            page = getattr(h, "page", None)
            key = entry_key(doc_id=doc_id, text=text, page=page)
            existing = self.by_key(key)
            if existing is not None:
                dup += 1
                out.append(existing)
                continue
            entry = EvidenceEntry(
                index=self._next,
                key=key,
                citation=str(getattr(h, "citation", "") or "").strip(),
                text=text,
                doc_id=doc_id,
                section=str(getattr(h, "section", "") or "").strip(),
                page=page,
                score=float(getattr(h, "score", 0.0) or 0.0),
            )
            self._next += 1
            self.entries.append(entry)
            self._by_key[key] = entry.index
            out.append(entry)
        return out, dup

    def enrich(self, meta_by_doc: dict[str, dict]) -> int:
        """用知识库的文档元数据补齐作者/年份/题名，返回补齐条数。

        只补**知识库里确实有的字段**。期刊名、卷期、文献类型标识（``[J]``/``[M]``）
        知识库里没有，就不写——参考文献表里宁可缺字段，也不能编。
        """
        filled = 0
        for e in self.entries:
            d = meta_by_doc.get(e.doc_id)
            if not d:
                continue
            for attr, key in (("author", "author"), ("year", "year"), ("title", "title"), ("source_path", "source_path")):
                if not getattr(e, attr) and d.get(key):
                    setattr(e, attr, str(d[key]))
                    filled += 1
        return filled

    # -- 序列化 -----------------------------------------------------------
    def as_dict(self) -> dict:
        return {"next_index": self._next, "entries": [e.as_dict() for e in self.entries]}

    @classmethod
    def from_dict(cls, d: Any) -> "EvidencePool":
        if not isinstance(d, dict):
            return cls()
        raw = d.get("entries") or []
        pool = cls([EvidenceEntry.from_dict(x) for x in raw if isinstance(x, dict)])
        # next_index 以落盘值为准（可能大于 max+1，因为中间可能删过条目）。
        try:
            pool._next = max(pool._next, int(d.get("next_index") or 0))
        except (TypeError, ValueError):
            pass
        return pool

    # -- 与项目黑板对接 ---------------------------------------------------
    @classmethod
    def load(cls, meta: dict) -> "EvidencePool":
        """从项目 meta 读池子。没有就用空池——起草可以从零开始。"""
        return cls.from_dict((meta or {}).get(POOL_META_KEY))

    def save(self, meta: dict) -> None:
        (meta or {})[POOL_META_KEY] = self.as_dict()


# ---------------------------------------------------------------------------
# 参考文献表
# ---------------------------------------------------------------------------

#: 参考文献条目的缺失字段说明。**用于如实告知作者缺什么**，不用于自动补全。
_REQUIRED_FIELDS = ("author", "title", "year")


def format_reference(index: int, entry: EvidenceEntry) -> str:
    """渲染一条参考文献。

    刻意采用"能确定什么就写什么"的最简形式，**不加 ``[J]``/``[M]`` 之类的
    文献类型标识**——知识库里没有这个信息，加上去就是编。作者按学校模板
    补上标识与卷期即可。
    """
    parts: list[str] = []
    if entry.author:
        parts.append(entry.author)
    if entry.title:
        parts.append(entry.title)
    if entry.year:
        parts.append(str(entry.year))
    body = ". ".join(p.strip(" .。") for p in parts if p).strip()
    if not body:
        # 连题名都没有时退回到 RAG 给的 citation 串：它至少是"检索到的那一条"，
        # 而不是一句"（未知文献）"——后者会让作者无从回查。
        body = (entry.citation or "（元数据缺失，请按原文补全）").strip()
    missing = [f for f in _REQUIRED_FIELDS if not getattr(entry, f)]
    if missing:
        body += f"　［缺：{'、'.join(missing)}，请按原文补全］"
    return f"[{index}] {body}."


# ---------------------------------------------------------------------------
# "哪些数字算事实"
# ---------------------------------------------------------------------------
#
# 定稿核对（结论里的数字必须在正文出现过）与结论生成都依赖同一条判定：
# **什么样的数字算"事实"，什么样的不算**。这条规则只能有一处实现——
# 结论生成端按一套口径判断、检查器按另一套判断时，"已就地标注"与"检查通过"
# 会互相矛盾，作者无法判断该信哪个。

#: 引用角标、章节号、图表号——从"事实数字"里排除。
#: 不排除的话，结论里一句「（见第 4 章）」会让 "4" 被当成"正文里没有的数字"，
#: 每一章都会误报。
_NON_FACT_NUMBER = re.compile(
    r"第\s*\d+\s*[章节篇]|\[\s*\d+(?:\s*[-–~,，、]\s*\d+)*\s*\]|表\s*\d+|图\s*\d+"
)

#: 数字的三种"同义写法"。**全部来自 PDF 抽取 + 中文排版**，比对前必须归一化。
#:
#: 这三个是在真实论文上逐个踩出来的，起因是同一条检查：
#: 「正文里的数字必须能在证据里找到」。第一版只做字面比对，结果把**有依据的数字**
#: 报成了编造——而这类误报最伤：作者核对一次发现是假的，之后就会连着真问题一起忽略。
#:
#: 1. **千分位空格**：证据写 ``8 800个观测值`` ``23 008 个样本观测值``，正文写 ``8800``；
#: 2. **小数点后空格**：证据写 ``均值为0. 072``，正文写 ``0.072``；
#: 3. **全角小数点**：证据写 ``标准差为1．166``（U+FF0E），正文写 ``1.166``。
#:
#: 第 3 种最隐蔽——三个字符肉眼几乎一样，只有把两串字符放在一起比对才看得出。
#: 一条真实论文里同时出现了前两种和第三种。
_SPLIT_DIGITS = re.compile(r"(?<=\d)[ \t\u00a0\u2009\u202f]+(?=\d)")
_SPLIT_DECIMAL = re.compile(r"(?<=\d)\.([ \t\u00a0\u2009\u202f]+)(?=\d)")
_UNICODE_DOT = re.compile(r"(?<=\d)[．。｡](?=\d)")
#: 逗号千分位：``1,166`` / ``1，166``。**只在逗号后正好三位数字时**才合并，
#: 否则会把「增长1，2个百分点」这类列举错并成一个数。空格千分位同样有这个问题，
#: 所以 ``_SPLIT_DIGITS`` 只管"数字—空格—数字"（中文里这几乎只出现在千分位），
#: 而逗号形态单独用"正好三位"约束。
_COMMA_THOUSANDS = re.compile(r"(?<=\d)[,，](?=\d{3}(?!\d))")


def fact_numbers(text: str) -> list[str]:
    """抽出文本中作为**事实**出现的数字（含小数），去掉引用角标、章节号与图表号。

    比对前会先归一化数字的三种同义写法（千分位空格、小数点后空格、全角小数点）。
    理由见上面那三行注释：不归一化，有依据的数字会被判成编造，
    **而误报会让作者把真问题一起忽略**。
    """
    t = _NON_FACT_NUMBER.sub(" ", text or "")
    t = _UNICODE_DOT.sub(".", t)
    t = _SPLIT_DECIMAL.sub(".", t)
    t = _SPLIT_DIGITS.sub("", t)
    t = _COMMA_THOUSANDS.sub("", t)
    return [m.group(0).rstrip(".") for m in re.finditer(r"\d+(?:\.\d+)?", t)]


def invert_mapping(mapping: dict[int, int] | dict[str, int]) -> dict[int, int]:
    """把 ``旧→新`` 的映射翻成 ``新→旧``。

    用途只有一个：**让"定稿"这一步可重复**。``build_references`` 会把正文角标从
    证据池编号改写成连续编号，改写后正文里的 ``[n]`` 就不再是池编号了。
    如果此时重跑起草（比如上一次因余额中断），旧章节带着"最终编号"、新章节带着
    "池编号"——**两份编号空间混在同一份稿子里**，同一处角标可能指向两条不同证据。

    所以重跑起草前先用这个逆映射把稿子还原回池编号空间，定稿时会重新编号。
    键统一转 int：落盘 JSON 会把字典键变成字符串，不转就会静默匹配不上而"还原失败"
    （而失败的表现是编号错乱，不是报错——这类静默失败最难查）。
    """
    out: dict[int, int] = {}
    for k, v in (mapping or {}).items():
        try:
            out[int(v)] = int(k)
        except (TypeError, ValueError):
            continue
    return out


#: 被圆括号裹起来的角标：``（[1]）`` / ``([1,2])``。
#:
#: GB/T 7714 的顺序编码制要求引注直接写作 ``[1]``，不加中文括号。而模型极容易写成
#: ``（[1]）``——提示词里的例子怎么写，它就怎么跟。所以这里做一次**机械归一化**：
#: 圆括号里**只有**一个角标时，括号是纯粹的格式冗余，去掉不改变任何语义。
#: 括号里还有别的内容（如 ``（见[1]）``）时不动——那种括号承载了语义。
_WRAPPED_MARKER = re.compile(r"[（(]\s*(\[\s*\d+(?:\s*[-–~,，、]\s*\d+)*\s*\])\s*[)）]")


def normalize_marker_wrapping(text: str) -> tuple[str, int]:
    """把 ``（[1]）`` 归一化成 ``[1]``，返回 ``(新文本, 改动处数)``。

    纯文本变换，且**只动括号、不动编号**。放在定稿这一环是因为它属于引用格式，
    与编号连续化是同一件事的两面——都必须在生成文献表之前完成。
    """
    n = 0

    def _sub(m: re.Match) -> str:
        nonlocal n
        n += 1
        return m.group(1)

    return _WRAPPED_MARKER.sub(_sub, text or ""), n


def used_indices_in_order(text: str) -> list[int]:
    """按**首次出现顺序**列出正文用到的编号（不含参考文献区）。"""
    seen: list[int] = []
    marked: set[int] = set()
    for _, _, nums in litter(text):
        for n in nums:
            if n not in marked:
                marked.add(n)
                seen.append(n)
    return seen


def renumber(text: str, mapping: dict[int, int]) -> tuple[str, int]:
    """按映射重写正文里的角标，返回 ``(新文本, 改动处数)``。

    纯文本变换，不改写任何句子：只把 ``[7]`` 换成 ``[2]``，编号顺序与相对位置
    完全保留。同一条角标里的多个编号会**重新排序并去重**，因为重排后它们的
    大小关系可能反转（原本 ``[9,2]`` 重排成 ``[1,4]``）——不排序会让同一处引用
    在不同版本里时正时倒，作者无法比对。

    映射里没有的编号原样保留：宁可留一个突兀的号，也不要静默删掉一处引用。
    """
    block = find_references_block(text)
    head = text[: block[0]] if block else text
    tail = text[block[0] :] if block else ""
    if block:
        # 参考文献表本身会被整段重建，旧表直接丢掉。
        tail = ""

    pieces: list[str] = []
    pos = 0
    changed = 0
    for start, end, nums in litter(head):
        head_part = head[pos:start]
        pieces.append(head_part)
        new_nums = sorted({mapping.get(n, n) for n in nums})
        rendered = "[" + ",".join(str(n) for n in new_nums) + "]"
        if rendered != head[start:end]:
            changed += 1
        pieces.append(rendered)
        pos = end
    pieces.append(head[pos:])
    out = "".join(pieces).strip()
    return out, changed


@dataclass
class ReferenceBuild:
    """参考文献表生成结果。"""

    text: str = ""
    mapping: dict[int, int] = field(default_factory=dict)
    cited: list[EvidenceEntry] = field(default_factory=list)
    uncited: list[EvidenceEntry] = field(default_factory=list)
    missing_fields: list[dict] = field(default_factory=list)
    renumbered: int = 0
    unwrapped: int = 0
    """被去掉多余圆括号的角标处数（``（[1]）`` → ``[1]``）。"""
    collapsed: int = 0
    """被折叠的重复文献数：同一篇文献被引多次时，只保留一个编号，被合并掉几个。"""

    def as_dict(self) -> dict:
        return {
            "cited": len(self.cited),
            "uncited": len(self.uncited),
            "renumbered": self.renumbered,
            "unwrapped": self.unwrapped,
            "collapsed": self.collapsed,
            "missing_fields": self.missing_fields,
            "mapping": {str(k): v for k, v in sorted(self.mapping.items())},
        }


def _completeness(entry: EvidenceEntry) -> int:
    """书目字段的完整程度，用于在同一篇文献的多条证据里挑"最适合当代表"的一条。"""
    return sum(1 for f in ("author", "title", "year", "source_path") if getattr(entry, f, ""))


def build_references(text: str, pool: EvidencePool, *, heading: str = "参考文献") -> ReferenceBuild:
    """把正文角标连续化，并生成参考文献表附在文末。

    ## 编号的单位是「文献」，不是「证据段落」

    证据池里的每一条是一个**原文片段**（带页码），这是为了让每句论断都能追回具体出处。
    但参考文献表列的是**文献**。两者不是一回事，混淆的后果实测出现过：

        证据池： 1=陈荣达(第1页) 2=陈荣达(第3页) 3=陈荣达(第5页) …
        文献表： [1] 陈荣达…  [2] 陈荣达…  [3] 陈荣达…   ← 同一篇论文列了五遍

    GB/T 7714 的顺序编码制要求**同一篇文献只有一个编号**（被引多次就用同一个号）。
    所以这里按 ``doc_id`` 折叠：同一篇文献的所有片段映射到同一个号，
    文献表每个 ``doc_id`` 只出现一次，代表条目取字段最全的那一条。
    片段级的页码追溯信息保留在 ``meta["writing_evidence"]`` 台账里，不丢。

    两个诚实性要求仍然成立：

    1. **只列真正被引用的文献**。检索到但没用上的留在 ``uncited`` 里报给作者，
       绝不混进文献表——"表里有、正文没引"正是查重与评审会抓的硬伤。
    2. **编号按首次出现顺序**，让文献表顺序与阅读顺序一致。
    """
    body, _ = _strip_reference_section(text)
    # 先去掉多余的圆括号再编号：顺序反过来的话，``（[1]）`` 在重排时会被当成
    # 角标改写，括号被留在原地，于是出现 ``（[3]）`` 这种"重排过了但还没归一化"的形态。
    body, unwrapped = normalize_marker_wrapping(body)
    order = used_indices_in_order(body)

    by_index = {e.index: e for e in pool.entries}
    # 文献键：优先 doc_id；没有 doc_id 的（罕见）退回用该条自己的 key，
    # 这样它自己算一篇文献，不会被错并到别的文献上。
    doc_of: dict[int, str] = {}
    doc_first: dict[str, int] = {}
    doc_order: list[str] = []
    unknown: list[int] = []
    for idx in order:
        e = by_index.get(idx)
        if e is None:
            unknown.append(idx)
            continue
        dk = e.doc_id or e.key
        doc_of[idx] = dk
        if dk not in doc_first:
            doc_first[dk] = idx
            doc_order.append(dk)

    doc_number = {dk: n for n, dk in enumerate(doc_order, start=1)}
    mapping = {idx: doc_number[dk] for idx, dk in doc_of.items()}

    # 代表条目：同一篇文献的多条证据里挑字段最全的那条。
    # 不挑"第一条"是因为第一条可能落在正文中间（元数据抽取不全），
    # 用它当代表会让文献表缺字段，而同一篇文献的另一条证据字段是齐的。
    reps: dict[str, EvidenceEntry] = {}
    for e in pool.entries:
        dk = e.doc_id or e.key
        if dk not in doc_number:
            continue
        cur = reps.get(dk)
        if cur is None or _completeness(e) > _completeness(cur):
            reps[dk] = e
    cited = [reps[dk] for dk in doc_order]

    new_body, changed = renumber(body, mapping)

    missing: list[dict] = []
    for idx in unknown:
        missing.append({"index": idx, "reason": "该编号不在证据池中（疑似模型编造或编号漂移）"})

    lines = [f"# {heading}", ""]
    if cited:
        for i, e in enumerate(cited, start=1):
            lines.append(format_reference(i, e))
            miss = [f for f in _REQUIRED_FIELDS if not getattr(e, f)]
            if miss:
                missing.append(
                    {
                        "index": i,
                        "doc_id": e.doc_id,
                        "reason": f"文献元数据缺字段：{'、'.join(miss)}",
                        "citation": e.citation,
                    }
                )
    else:
        lines.append("（正文中没有任何引用角标，未生成文献条目）")

    used_docs = set(doc_number)
    uncited = [
        e for e in pool.entries if (e.doc_id or e.key) not in used_docs
    ]
    out = new_body.rstrip() + "\n\n" + "\n".join(lines) + "\n"
    return ReferenceBuild(
        text=out,
        mapping=mapping,
        cited=cited,
        uncited=uncited,
        missing_fields=missing,
        renumbered=changed,
        unwrapped=unwrapped,
        collapsed=len(order) - len(cited),
    )


def _strip_reference_section(text: str) -> tuple[str, str]:
    block = find_references_block(text)
    if not block:
        return (text or "").strip(), ""
    return (text or "")[: block[0]].strip(), (text or "")[block[0] :]
