"""写作助手 · 路由。

设计取向的变更（重要）
----------------------
早先这一层有一条**硬闸门**：证据库为空就拒绝生成，连模型都不调用。它的目的是
防止模型凭空编造，代价是"必须先手工录入资料才能写"。

现在放宽为：**可以依据大模型自身知识、作者导入的文档、以及互联网检索到的真实文献来写**。
放宽输入就必须补上等价的约束，否则防线的目的就丢了。所以本站的做法不是
"放开闸门"，而是**把"依据"从一个来源扩成四个层级，并且每一层都要能被检查**：

===========  ========================  ==========================  ==========================
层级          来源                      引用编号                    怎么验证
===========  ========================  ==========================  ==========================
D 作者文档    本次上传的 Word/PDF        ``[n]``（从 1 起）          编号在池内；用了材料里的
              正文切出的段落                                         数字却没挂编号会被点名
A 用户证据    访客录入的文献原文        ``[n]``（接在 D 之后）      编号在本层池内
B 联网文献    Crossref/OpenAlex/arXiv   ``[n]``（接在 A 之后）      编号在池内 + 有真实 DOI
              检索到的真实记录
C 模型知识    大模型自身训练所得        无编号                      事后按数字接地复核
===========  ========================  ==========================  ==========================

五条**代码强制**的可靠性约束（不靠提示词自觉）：

1. **引用编号必须在池内。** 正文里每个 ``[n]`` 都必须是 D + A + B 层真实存在的编号；
   越界的记成"编造引用"通报，不静默删掉。
2. **数字必须能在材料里找到。** 正文里的数字需出现在 D + A + B 层原文中（年份除外）。
   这是唯一能机械证伪"这个数从没出现过"的手段。
3. **用了材料却不挂编号，单独点出来。** 实测真模型最容易犯的错是把作者上传的文档
   当成"作者自己的话"：数字照抄、一个编号都不挂。那会让覆盖度得出"没有任何引用依据"
   的**反向结论**。所以这里用数字做指纹机械判定（见 ``_citation_usage``），
   不依赖模型自述。
4. **C 层不得挂引用。** 模型知识不许伪装成有出处；挂了编号就按第 1 条被查出越界。
5. **覆盖度如实上报。** 每一章回传用了几个 D/A/B 层、编造了几个编号、几处数字找不到依据、
   文档依据块用了几条/总共几条，让作者一眼看出哪几段需要自己补依据——
   而不是看到一篇"看起来很有依据"的稿子。

第 5 条尤其要紧：放宽之后最大的风险不是"写不出来"，而是**作者以为都有依据**。
所以宁可把话说难听，也不给一个虚假的安心。

两个后加的入口（request 层，不改变上面这套约束）
------------------------------------------------
* ``POST /api/writing/import-plan`` —— 上传 Word / PDF，先解析出一份**可编辑的写作计划**
  （题目、类型、章节结构、写作要求）。文档正文同时被切成带定位的**材料块**
  存进会话，与 A/B 两层并列成为写作时的依据（即 D 层）。
* ``POST /api/writing/section`` —— 只重写某一章。与整篇生成共用同一套材料表、
  引用校验与数字接地复核，所以"局部重写"不会绕开约束。
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, Body, File, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse, StreamingResponse

from .. import credentials as creds_mod
from .. import fileintake
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

#: 材料表里给 B 层（联网检索到的真实文献）**预留**的条目额度。
#: 为什么是"预留"而不是"先到先得"：作者导入 8 条文档块时，先到先得会把 B 层
#: 挤到一条都进不来——而 B 层是唯一带 DOI、可回查的那一层。宁可少放几条
#: 作者自己已有的材料，也要保证有出处可查的文献进得去。
MAX_WEB_MATERIAL = 4

#: 每章自动联网检索几轮。1 轮用章节标题，另 1 轮用论文题目，够用且不拖时间。
MAX_QUERIES_PER_SECTION = 2

#: 一次最多解析几个上传文件。与 ``fileintake.MAX_FILES`` 一致（那里是解析层的上限）。
MAX_IMPORT_FILES = fileintake.MAX_FILES

#: 文档材料块（``document_material``）的总字符预算。
#:
#: 这个数不是随手定的：材料表整体要塞进模型上下文，而几十万字的中文论文正文
#: 本身就远超任何模型窗口。取 6 万字符——够放下十几页正文的关键段落，
#: 又不会把联网检索回来的 B 层材料挤出去。超出部分**截断并如实标注**，
#: 绝不静默丢弃：作者有权知道"这次写作只用了文档的一部分"。
DOC_CHAR_BUDGET = 60_000

#: 单个文档材料块的**上限**（字符）。太大就把一条材料变成半篇论文，会挤占上下文。
DOC_BLOCK_CHARS = 2_500

#: 单个文档材料块的**目标下限**（字符）。小于它就把下一段并进来。
#: 这条是实测补上的：Word 稿子常是"每段十来字 + 段间空行"，逐段成块切出 58 个碎块，
#: 材料表的条目额度（8 条）瞬间被摘要和前两段占满，正文其余部分一块都进不去。
DOC_MIN_BLOCK = 600

#: 材料表里最多放几个文档材料块。
#:
#: 反直觉之处：**给材料表加料不是越多越好**。文档段落写进提示词会同时抬高
#: 输入 token 与"模型把邻近数字抄串行"的概率。而且材料表是**按条数**截断的
#: （见 ``_build_material``），条目堆得太多，A/B 两层就会被挤出去。
#: 具体剩几条没被用上，会在结果里如实报出来（``document_material_used``）。
MAX_DOC_BLOCKS = 8

#: 单条材料写进材料表时的最大字符数。
MATERIAL_ITEM_CHARS = 1400


def _clip_material(text: str, limit: int = MATERIAL_ITEM_CHARS) -> str:
    """把一条材料裁到 ``limit`` 字符：**留头也留尾**。

    为什么不是简单地取前 1400 字：**结论与结果里的数字往往在段落后半部分**，
    只留头会把它们全部切掉。后果不是"少了一点素材"，而是接地检查会把这些数字
    判成"材料里找不到"——此时报出来的"模型自己编了数字"是**我们自己的截断造成的**，
    一个会冤枉模型、也会误导作者的假警报。留尾是这里唯一合理的选择。
    """
    text = text or ""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head - 12
    return f"{text[:head]} …（中间略）… {text[-tail:]}"

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


# ---------------------------------------------------------------------------
# 文档导入：把一份 Word / PDF 变成"可编辑的写作计划 + 带定位的材料块"
#
# 为什么不是"上传完就直接开写"：文档里的要求（多少字、什么结构、投什么期刊、
# 有没有指定章节）只有作者自己看得懂。让模型猜一个然后直接生成，作者要么接受一份
# 跑偏的稿子，要么重来一遍。所以这里只做**解析 + 提议**，把计划摊在界面上让作者
# 改，改完点"生成初稿"才真的开写。多花十秒，省一次返工。
# ---------------------------------------------------------------------------

#: 从文档里抽章节结构时认的标题形态。
#: 只认"章/节"编号或 Markdown 井号开头的行，**不认任意短行**——把正文里的普通短句
#: 当标题，会造出一份比原文还碎的大纲，那比识别不到更糟。
#: 也刻意**不认**「一、」「（一）」这类中文序号：它们在正文里当分句引导词太常见，
#: 误判的代价（大纲里混进一堆正文片段）远比多识别几个标题大。
_HEADING_RES = (
    re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*[章篇部]\s*[、.．:：]?\s*(.{0,60})$"),
    re.compile(r"^第\s*[0-9一二三四五六七八九十百]+\s*节\s*[、.．:：]?\s*(.{0,60})$"),
    re.compile(r"^(\d{1,2}(?:\.\d{1,2}){0,2})[\s、.．:：]+(\S.{0,60})$"),
    re.compile(r"^#{1,4}\s+(\S.{0,60})$"),
)

#: 参考文献段的标题。找到它之后的内容**全部剔除**：参考文献表是题录，
#: 不是可引用的证据。
#:
#: 这里的取舍值得写下来：把 60 条题录塞进材料表，会让"引用编号在池内"这条检查
#: **看起来**通过（编号确实存在），但那条参考文献根本没有可核对的原文，
#: 于是检查的结论会从"每处引用都有原文可回查"悄悄退化成"编号存在"。
#: 宁可少给模型一点素材，也不要一个会自我欺骗的校验。
_REF_HEADING_RE = re.compile(
    r"^\s*(?:[\[【(（]?\s*(?:参\s*考\s*文\s*献|references?|bibliography|works\s+cited)\s*[\]】)）]?)\s*[:：]?\s*$",
    re.I,
)

#: 文档里出现这些词时的类型推断（模型不可用时的兜底）。
_GENRE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("review", ("文献综述", "研究综述", "系统评价", "meta 分析", "meta分析", "述评")),
    ("experimental", ("实验研究", "对照试验", "随机对照", "实验组", "干预效果")),
    ("empirical", ("实证研究", "问卷调查", "访谈", "样本", "回归", "统计分析")),
    ("case", ("案例研究", "个案研究", "田野调查")),
    ("design", ("系统设计", "系统实现", "原型", "算法设计", "架构")),
)


def _looks_like_reference_heading(line: str) -> bool:
    return bool(_REF_HEADING_RE.match(line or ""))


def _split_document(text: str) -> tuple[list[dict], int]:
    """把整篇文档切成带定位的材料块。返回 ``(块列表, 被剔除的参考文献字符数)``。

    切法：按空行分段，再把小段**并到够大**（``DOC_MIN_BLOCK``）才收一块。
    **不按字数硬切**，因为把一句话从中间劈开会同时破坏可读性与数字接地的核对
    （``-0.34`` 被切成 ``-0.3`` 和 ``4`` 是实测会发生的）。

    并段这一步不是可有可无的：Word 稿子常见每段十来字、段间空行，逐段成块会让
    58 个碎块把材料表的条目额度耗尽，结果只有摘要和前两段进了提示词。
    与其"多给几条短的"，不如"少数几条长的"。

    ``page`` 只在能确定时给出：现在唯一可靠的来源是 PDF 的逐页 chunks，
    所以这里返回的块不带页码，页码由调用方在按页切分时补上。宁可不标，也不标错。
    """
    paragraphs: list[str] = []
    dropped = 0
    in_refs = False
    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if _looks_like_reference_heading(line):
            in_refs = True
            continue
        if in_refs:
            dropped += len(line)
            continue
        if line.strip():
            paragraphs.append(line.strip())
        else:
            paragraphs.append("")

    blocks: list[dict] = []
    buf: list[str] = []
    size = 0

    def flush() -> None:
        nonlocal buf, size
        body = "\n".join(x for x in buf).strip()
        buf, size = [], 0
        if len(body) >= 40:          # 太短的块没有证据价值，只会稀释材料表
            blocks.append({"text": body, "page": None})

    for para in paragraphs:
        if not para:
            # 段落边界：只有攒够了才收块，否则继续并下一段。
            if size >= DOC_MIN_BLOCK:
                flush()
            continue
        if size + len(para) > DOC_BLOCK_CHARS and buf:
            flush()
        buf.append(para)
        size += len(para)
    flush()
    return blocks, dropped


def _document_blocks(session) -> list[dict]:
    """取会话里已导入的文档材料块（最近一次导入的为准）。"""
    blocks = session.meta.get("document_material") or []
    return [b for b in blocks if isinstance(b, dict) and str(b.get("text") or "").strip()]


def _title_from_text(text: str, fallback: str = "") -> str:
    """从文档开头猜一个题名。

    文档的第一行往往就是标题。只认**短且不含句号**的行：把摘要的首句当标题，
    会让整篇写作围着那句话转，比用文件名兜底还差。
    """
    for line in (text or "").split("\n")[:40]:
        s = line.strip().strip("#*　 ")
        if not s or len(s) > 60:
            continue
        if s.endswith(("。", "；", ".", "，", ",")):
            continue
        if re.match(r"^(摘\s*要|abstract|关键词|keywords?|目\s*录)\b", s, re.I):
            continue
        return s
    return fallback


def _headings_from_text(text: str, limit: int = MAX_SECTIONS) -> list[str]:
    """从文档里抽章节结构。抽不到就返回空列表（由调用方决定怎么兜底）。"""
    out: list[str] = []
    seen: set[str] = set()
    #: 这些标题不是"要逐章生成的正文"，不该进章节结构。
    #: 摘要/参考文献/致谢是前后置部分，让模型按"不少于 1200 字"去写一篇摘要
    #: 既不合规范也浪费额度。这里在程序里再兜一道，不只靠提示词自觉。
    _NON_SECTION = re.compile(
        r"^(摘\s*要|abstract|关\s*键\s*词|keywords?|目\s*录|参考文献|references?|"
        r"致\s*谢|acknowledge?ments?|附\s*录|appendix)\s*[:：]?\s*$",
        re.I,
    )

    for line in (text or "").split("\n"):
        s = line.strip().lstrip("#").strip()
        if not s or len(s) > 80 or _looks_like_reference_heading(s) or _NON_SECTION.match(s):
            continue
        for i, pattern in enumerate(_HEADING_RES):
            m = pattern.match(s)
            if not m:
                continue
            if i == 2:
                # 形如 "2.1 研究假设"：编号与标题都要，否则"2.1"会丢掉层级语义。
                title = f"{m.group(1)} {m.group(2)}".strip()
            else:
                title = s                     # 章/节 行原样保留（"第1章 绪论"）
            key = re.sub(r"[\s\W_]+", "", title)
            if key and key not in seen:
                seen.add(key)
                out.append(title)
            break
        if len(out) >= limit:
            break
    return out


def _detect_genre(text: str) -> str:
    """按文档里的关键词推断论文类型。推断不出来就返回空串（用界面上选的那一项）。"""
    head = (text or "")[:4000]
    for genre, hints in _GENRE_HINTS:
        if any(h in head for h in hints):
            return genre
    return ""


def _fallback_plan(text: str, filename: str, genre_ids: list[str]) -> dict:
    """模型不可用时的计划：全部用文档自身的结构与文件名拼出来，并标清来源。"""
    stem = re.sub(r"\.(pdf|docx|doc|md|txt|html?)$", "", filename or "", flags=re.I).strip()
    title = _title_from_text(text, fallback=stem) or stem or "未命名论文"
    sections = _headings_from_text(text) or _fallback_sections()
    genre = _detect_genre(text)
    return {
        "title": title,
        "genre": genre if genre in genre_ids else "",
        "sections": sections,
        "keywords": [],
        "requirements": (
            "（本条由程序从文档结构中推导，未经模型确认）请按文档原有的章节结构写作；"
            "文档正文已作为材料块提供，正文里的数字与引用需要能在其中找到。"
        ),
        "source": "local",
        "notes": [],
    }


def _genre_list() -> list[dict]:
    """论文类型清单。上游 genrics 模块不可用时也要能降级，所以这里兜一层。"""
    try:
        from app.writing.genres import genre_choices

        return [dict(g) for g in genre_choices()]
    except Exception:  # noqa: BLE001 - 导入失败只影响"类型"下拉，不该让导入整个失败
        return []


#: 让模型把文档读成一份写作计划时用的提示词。
#: 关键约束：**只准依据文档**。模型很擅长"补全"一份看起来合理的论文结构，
#: 而那恰恰是最坏的结果——作者会以为文档里写了这些章节。
_PLAN_SYSTEM = (
    "你在帮作者把一份已有的文档（论文初稿、开题报告、任务书、写作要求，或它们的混合）"
    "转成一份可执行的写作计划。\n"
    "硬性规则：\n"
    "1. **只依据文档内容**。文档没写的，一律留空，不要替作者补一个'通常会有'的章节。\n"
    "2. 章节结构优先照抄文档自己的结构；文档只有要求、没有结构时，才按学科惯例提一份，"
    "并在 notes 里说明这是你提议的。\n"
    "3. 题名取文档自己的标题；文档没给标题时，从内容里概括一个，并在 notes 里说明。\n"
    "4. 正文章节才进 sections：摘要、关键词、参考文献、致谢这类**前后置部分不要放进去**"
    "（它们不是需要逐章生成的正文）。\n"
    "5. 只输出 JSON，不要解释，不要 Markdown 代码块。"
)


def _plan_user_prompt(text: str, filenames: list[str], genres: list[dict]) -> str:
    genre_desc = "、".join(f"{g.get('id')}={g.get('name')}" for g in genres if g.get("id"))
    return (
        f"上传的文件：{'、'.join(filenames) or '未命名'}\n"
        f"可选的论文类型 id：{genre_desc or '（无）'}\n\n"
        "请输出这个 JSON 对象：\n"
        "{\n"
        '  "title": "论文题目",\n'
        '  "genre": "上面列出的某个 id，判断不出就空字符串",\n'
        '  "sections": ["第1章 绪论", "第2章 …"],\n'
        '  "keywords": ["关键词"],\n'
        '  "requirements": "文档对这篇论文的写作要求（字数、结构、格式、时间、侧重点等），'
        '用一段话概括；文档没提就写空字符串",\n'
        '  "notes": ["你做的推断，例如：章节结构由你提议、题名由你概括"]\n'
        "}\n\n"
        f"文档内容（可能被截断）：\n{text}"
    )


def _model_plan(chat, text: str, filenames: list[str], genres: list[dict]) -> dict:
    """调模型抽计划。返回值已做形状校验——模型给的字段一个都不能直接信。"""
    raw = chat.ask_json(
        [
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": _plan_user_prompt(text, filenames, genres)},
        ],
        temperature=0.2,
        max_tokens=3000,
    )
    if not isinstance(raw, dict):
        raise ValueError("模型没有返回 JSON 对象")

    genre_ids = [str(g.get("id") or "") for g in genres if g.get("id")]
    sections: list[str] = []
    for item in raw.get("sections") or []:
        title = item if isinstance(item, str) else str((item or {}).get("title") or "")
        title = title.strip()
        if title:
            sections.append(title[:120])
    notes = [str(n).strip() for n in (raw.get("notes") or []) if str(n).strip()]
    keywords = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()]
    genre = str(raw.get("genre") or "").strip()

    return {
        "title": str(raw.get("title") or "").strip()[:200],
        "genre": genre if genre in genre_ids else "",
        "sections": sections[:MAX_SECTIONS],
        "keywords": keywords[:12],
        "requirements": str(raw.get("requirements") or "").strip()[:2000],
        "source": "model",
        "notes": notes[:8],
    }


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


def _build_material(
    user_evidence: list[dict],
    web_records: list[websearch.WebRecord],
    doc_blocks: list[dict] | None = None,
):
    """把三种依据合成一张带统一编号的材料表。

    编号规则：**作者文档**（导入的 Word/PDF 正文块）从 1 开始，接着是 A 层（用户证据），
    最后是 B 层（联网文献）。**每次渲染都重新分配**——提示词里给模型的编号必须与校验时
    用的编号严格一致，否则校验会全盘错位。

    文档块为什么排在最前：它是作者自己这次上传的东西，通常是本次写作最直接的依据；
    而且材料表是**按条数**截断的，排在后面就有整块被挤掉的风险。

    返回值里带上 ``doc_used``：材料表最多只放 ``MAX_DOC_BLOCKS`` 条文档块，
    会话里存的可能远多于此。**用了几条、剩几条没进提示词要如实报出来**，
    否则作者会以为整篇文档都参与了写作。
    """
    index: dict[int, dict] = {}
    lines: list[str] = []

    def add(tier: str, citation: str, text: str, extra: str = "") -> None:
        n = len(index) + 1
        index[n] = {"tier": tier, "citation": citation, "text": text}
        lines.append(f"[{n}]（{tier}）{citation}")
        if extra:
            lines.append(f"    {extra}")
        lines.append(f"    原文：{_clip_material(text)}")

    used_doc = 0
    for block in (doc_blocks or [])[:MAX_DOC_BLOCKS]:
        text = str(block.get("text") or "")
        if not text.strip():
            continue
        where = f"第 {block['page']} 页" if block.get("page") else "导入文档片段"
        add("D 作者文档", f"{block.get('origin') or '导入文档'}（{where}）", text)
        used_doc += 1

    # ---- 条目额度分配 ----
    # 材料表总共只放 MAX_MATERIAL 条，额度**必须先给 B 层留够**：
    # 作者导入 8 条文档块时，如果按"先到先得"分配，B 层（联网检索到的真实文献）
    # 会被挤到一条都进不来——而 B 层恰恰是唯一带 DOI、可回查的那一层。
    # 反过来，D 与 A 都是作者自己已有的东西，少放几条只是少一点素材。
    b_quota = min(MAX_WEB_MATERIAL, max(0, MAX_MATERIAL - used_doc))
    a_quota = max(0, MAX_MATERIAL - used_doc - b_quota)

    for item in user_evidence[:a_quota]:
        add(
            "A 用户提供",
            str(item.get("citation") or item.get("title") or "来源未标注"),
            str(item.get("text") or item.get("abstract") or ""),
            f"年份：{item['year']}" if item.get("year") else "",
        )

    remaining = MAX_MATERIAL - len(index)
    for rec in web_records[: max(0, min(remaining, b_quota))]:
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
        "  · D 作者文档 —— 作者本次上传的论文稿/开题报告/写作要求里切出的正文段落\n"
        "  · A 用户提供 —— 作者自己在证据库里录入的文献资料\n"
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
        "8. 只输出该章正文本身：不要标题、不要解释、不要 Markdown 标题符号。段落间空一行。\n"
        "\n"
        "最容易犯的一个错，单独强调：\n"
        "**D 层是作者上传的材料，它不是'你自己的话'，凡是从它那里取的都必须挂编号。**\n"
        "转录样本量、系数、维度得分、结论、政策表述，都要紧跟它对应的 [n]。"
        "不挂编号就等于把作者的材料冒充成模型的记忆——这会被程序查出来"
        "（正文里出现材料里才有的数字却一个编号都没有），并作为问题通报给作者。"
    )


def _citation_usage(body: str, index: dict[int, dict]) -> dict:
    """统计正文对材料的实际使用情况，专治"用了材料却不挂编号"。

    为什么需要这条**程序化**检查，而不是只靠提示词：实测真模型写 D 层材料时，
    会把样本量、系数直接写进正文**一个 ``[n]`` 都不挂**（它把作者上传的稿子
    当成了"作者自己的话"）。后果不是文风问题，而是覆盖度会得出
    "没有任何引用依据"的结论——作者据此以为整章都是模型编的，而事实恰恰相反。

    这里用数字做指纹：正文里出现了只在某几条材料里才有的数字，却一处编号都没有，
    那就是"用了材料但没标注"。这是一条**可机械判定**的证据，不依赖模型自述。
    """
    used = {int(m) for m in CITE_RE.findall(body or "")}
    if used:
        return {"suspected_uncited": False, "evidence_numbers": 0}
    available: dict[str, set[int]] = {}
    for n, item in index.items():
        for num in _numbers_in(str(item.get("text") or "")):
            available.setdefault(num, set()).add(n)
    hits = sorted(num for num in _numbers_in(body or "") if num in available)
    return {"suspected_uncited": bool(hits), "evidence_numbers": len(hits),
            "matched": hits[:5]}


def _section_prompt(title: str, heading: str, material: str, has_web: bool,
                    is_last: bool, requirements: str = "", task: str = "") -> str:
    coverage = (
        "材料表包含 D/A/B 三层，请充分利用。"
        if has_web
        else "本次没有检索到联网文献；确需通用知识时可按规则 3 用无编号表述。"
    )
    req = f"作者给出的写作要求（最高优先级，与本章有关的部分必须满足）：\n{requirements}\n\n" if requirements.strip() else ""
    doc_note = (
        "材料表里带「D 作者文档」的是**作者上传的材料**，不是你的记忆："
        "凡是从它那里取的样本量、系数、结论、表述，都要紧跟编号，例如 [1]。\n"
        if "D 作者文档" in (material or "") else ""
    )
    extra = f"\n本次的额外要求（作者临时提出，优先级高于上面的通例）：{task.strip()}" if task.strip() else ""
    return (
        f"论文题目：{title}\n"
        f"本次要写的章节：{heading}\n\n"
        f"{req}"
        f"可用材料：\n{material}\n\n"
        f"{coverage}\n"
        f"{doc_note}"
        f"请写出「{heading}」的正文，{MIN_WORDS}~{MAX_WORDS} 字。"
        + ("\n这是最后一章：结论只能汇总前文已论证的内容，不得引入新材料之外的新证据。"
           if is_last else "")
        + extra
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
async def draft(
    request: Request,
    # 进程内调用方（``suite/agents/writer_agent.py`` 经 bridge 复用本函数）用的口子：
    # caller 手里已经有 ``Session`` 对象，把它的证据直接交进来，比让 handler 再按 sid
    # 去会话表里取一次更直接，也避免"一个 sid 对应两个会话"的隐患。
    # 用 Body(..., embed=True) 显式声明，而不是留一个裸默认值：``list`` 类型的裸参数
    # 会被 FastAPI 当成**查询参数**，于是请求体被解析成一个 list，报出
    # "Input should be a valid list" 这种与真正原因毫不相干的错（实测踩过）。
    # embed 是必须的：不嵌一层的话，正常的 [sid, title] 请求体整体塞进这个参数，
    # 校验直接 422。嵌一层之后，请求体里没有这个键就取默认值 None。
    evidence_internal: list[dict] | None = Body(default=None, embed=True,
                                                include_in_schema=False),
) -> StreamingResponse:
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

    # 作者在文档/界面上给出的写作要求。**原样进提示词**，不做"理解与改写"——
    # 模型对作者原话的理解通常比我们转述一遍更准，转述反而会丢掉细节。
    requirements = str(body.get("requirements") or "").strip()[:2000]

    user_evidence = (list(evidence_internal) if evidence_internal is not None
                     else list(session.evidence))
    doc_blocks = _document_blocks(session)
    configured = creds.configured

    def stream():
        yield _sse({"type": "stage", "stage": "start", "progress": 0.02, "message": "已连接。"})

        # ---- 证据来源清点：把"这一版有什么依据"先说清楚 ----
        yield _sse({
            "type": "stage", "stage": "check", "progress": 0.05,
            "message": (
                f"D 层（导入文档）：{len(doc_blocks)} 段；"
                f"A 层（你的资料）：{len(user_evidence)} 条；"
                f"B 层（联网检索）：{'开启' if use_web else '已关闭'}；"
                f"章节：{len(sections)} 章。"
            ),
        })

        if not configured:
            yield _sse({"type": "error", "message": "写作需要模型：请先在页面右上角填入你自己的 API Key。"})
            yield "data: [DONE]\n\n"
            return

        if not user_evidence and not use_web and not doc_blocks:
            # 既没资料又关了检索：仍然允许写（放宽了），但必须强烈提示。
            yield _sse({
                "type": "stage", "stage": "warn", "progress": 0.06,
                "message": (
                    "没有导入文档、证据库为空且联网检索已关闭：本次将完全依据模型自身知识写作，"
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
                    material, index = _build_material(user_evidence, web_records, doc_blocks)

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
                                    title, heading, material, bool(web_records),
                                    i == len(sections), requirements)},
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

                    # ---- 约束 2b：用了材料却没挂编号 ----
                    # 实测真模型最容易犯的错：把作者上传的材料当成"作者自己的话"，
                    # 数字照抄、一个 [n] 都不挂。那会让覆盖度得出"没有任何引用依据"的
                    # 反向结论（作者以为整章都是模型编的，事实正相反），所以必须查出来。
                    usage = _citation_usage(text, index)
                    if usage["suspected_uncited"]:
                        notes.append(
                            f"第 {i} 章用到了材料里的数字（如 {'、'.join(usage['matched'])}）"
                            "却没有标注任何引用编号——这段内容不是模型凭空写的，"
                            "但读者无从回查，请人工补上编号。"
                        )

                    tier_d = sum(1 for n in valid if index[n]["tier"].startswith("D"))
                    tier_a = sum(1 for n in valid if index[n]["tier"].startswith("A"))
                    tier_b = sum(1 for n in valid if index[n]["tier"].startswith("B"))

                    words = _count_cjk(text)
                    produced.append({
                        "heading": heading,
                        "body": text.strip(),
                        "words": words,
                        "citations": {"total": len(valid), "doc": tier_d, "user": tier_a,
                                      "web": tier_b, "invalid": len(invalid)},
                        "grounding": grounding,
                        "uncited_evidence": usage,
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
                cite_doc = sum(s["citations"].get("doc", 0) for s in produced)
                cite_user = sum(s["citations"]["user"] for s in produced)
                cite_web = sum(s["citations"]["web"] for s in produced)
                coverage = {
                    "sections": len(produced),
                    "citations_total": total_cites,
                    "citations_doc": cite_doc,
                    "citations_user": cite_user,
                    "citations_web": cite_web,
                    "citations_invalid": len(invalid_all),
                    "ungrounded_numbers": len(ungrounded_all),
                    "uncited_evidence_sections": sum(
                        1 for s in produced
                        if (s.get("uncited_evidence") or {}).get("suspected_uncited")
                    ),
                    # 用了几条、文档总共有几条。材料表按**条数**截断，两者常常不等，
                    # 所以必须分开报——只报一个数会让作者以为整篇文档都参与了写作。
                    "doc_blocks": min(len(doc_blocks), MAX_DOC_BLOCKS),
                    "doc_blocks_total": len(doc_blocks),
                    "web_records_retrieved": web_total,
                    "web_sources": sorted({s["source"] for s in web_status_all if s.get("ok")}),
                }
                if len(doc_blocks) > MAX_DOC_BLOCKS:
                    notes.append(
                        f"导入文档被切成 {len(doc_blocks)} 段，材料表只放进了前 {MAX_DOC_BLOCKS} 段"
                        f"（余下 {len(doc_blocks) - MAX_DOC_BLOCKS} 段未参与写作）。"
                        "要覆盖更多内容，可把关键章节单独导一次，或改成逐章重写。"
                    )
                uncited_sections = coverage["uncited_evidence_sections"]
                # 结论要区分"依据来自作者自己的文档"与"来自联网检索"：
                # 前者是作者已有的东西，后者是助手新找的，可信度与需要核对的程度都不同。
                #
                # 还有第三种情况要先判掉：**正文里有材料里的数字，却一个编号都没挂**。
                # 那时 citations_* 全是 0，若直接走"没有任何引用依据"就等于说反了——
                # 内容其实有依据，只是没标出来。这个分支就是为它准备的。
                if uncited_sections and not total_cites:
                    coverage["verdict"] = (
                        f"正文用到了材料里的数据，但没有任何引用编号（{uncited_sections} 章）——"
                        "内容有依据、但读者无法回查，需要人工补编号"
                    )
                elif cite_web and (cite_user or cite_doc):
                    coverage["verdict"] = "同时有你提供/导入的材料与联网文献支撑"
                elif cite_web:
                    coverage["verdict"] = "主要依据联网检索的公开文献（建议核对是否贴合你的论证）"
                elif cite_doc and cite_user:
                    coverage["verdict"] = "依据你导入的文档与你录入的资料"
                elif cite_doc:
                    coverage["verdict"] = "仅依据你导入的文档（文档里没有的可回查文献）"
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
                    "scope": {"tiers": ["D 导入文档", "A 用户证据", "B 联网检索", "C 模型知识"],
                              "use_web": use_web,
                              "doc_blocks": min(len(doc_blocks), MAX_DOC_BLOCKS),
                              "doc_blocks_total": len(doc_blocks)},
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


@router.post("/import-plan")
async def import_plan(
    request: Request,
    files: list[UploadFile] = File(default_factory=list),
) -> dict:
    """上传 Word / PDF（也支持 Markdown / 文本 / HTML / CSV），解析出一份**可编辑的写作计划**。

    这一步分成两半，成本与可靠性完全不同：

    * **解析文件**不需要 API Key**（与 ``/api/gather/import`` 同一套 ``fileintake``）；
    * **抽计划**（题名/结构/文体要求）需要模型。模型不可用时**不报错**，
      而是回退成"从文档自身结构推导"的计划，并在 ``plan.source`` 里标出来是 ``local``。

    为什么回退而不是直接失败：用户上传的是他自己写的东西，文档里往往已经有题目和章节。
    因为没填 Key 就把人挡在门外，代价远大于收益；而把"这份计划是程序猜的、不是模型读出来的"
    如实标注出来，用户就不会误以为模型已经理解了他的要求。

    ``?full=1`` 时把解析出的全文一并返回（前端可以给一个"解析结果对不对"的预览）。
    正文同时被切成材料块存进会话，后续 ``/draft`` 会自动把它们作为 D 层依据——
    **不需要用户再手工点一次"抽取证据"**。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件。")
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(
            status_code=413,
            detail=f"一次最多导入 {MAX_IMPORT_FILES} 个文件（收到 {len(files)} 个）。",
        )

    full = str(request.query_params.get("full") or "").strip().lower() in {"1", "true", "yes", "on"}

    parsed: list[fileintake.Extracted] = []
    total_bytes = 0
    notes: list[str] = []
    for upload in files:
        name = upload.filename or "未命名"
        try:
            data = await upload.read()
        except Exception as exc:  # noqa: BLE001 - 读上传流也可能失败
            parsed.append(fileintake.Extracted(filename=name, error=f"读取上传内容失败：{exc}"))
            continue
        finally:
            try:
                await upload.close()
            except Exception:  # noqa: BLE001
                pass

        total_bytes += len(data)
        if total_bytes > fileintake.MAX_FILE_BYTES * 2:
            parsed.append(fileintake.Extracted(
                filename=name,
                error=f"本批文件累计过大（超过 {fileintake.MAX_FILE_BYTES * 2 // 1024 // 1024} MB），已停止处理。",
            ))
            break
        parsed.append(fileintake.extract(name, data))

    ok_items = [r for r in parsed if r.ok]
    if not ok_items:
        # 与 /api/gather/import 同一口径：有文件却一个都没解析出来是**真失败**，
        # 不能以 200 返回一份空计划——那会显示成"导入成功，但没有内容"。
        reasons = "；".join(f"{r.filename}: {r.error or r.warning}" for r in parsed[:4])
        raise HTTPException(status_code=422, detail=f"{len(files)} 个文件都没能解析出文字。{reasons}")

    # ---- 拼一份"给人看、也给模型读"的全文 ----
    blocks: list[dict] = []
    texts: list[str] = []
    for item in ok_items:
        if item.kind == "PDF" and item.chunks:
            page_blocks: list[dict] = []
            for chunk in item.chunks:
                page_text = str(chunk.get("text") or "").strip()
                if len(page_text) < 40:
                    continue
                page_blocks.append({"text": page_text, "page": chunk.get("page"),
                                    "origin": item.filename})
            blocks.extend(page_blocks)
        else:
            doc_blocks, dropped = _split_document(item.text)
            if dropped:
                notes.append(
                    f"{item.filename}：已剔除参考文献段（约 {dropped} 字）——"
                    "题录不含原文，放进材料表会让引用校验变成走过场。"
                )
            for b in doc_blocks:
                b["origin"] = item.filename
            blocks.extend(doc_blocks)
        texts.append(item.text)

    # 预算控制：超出预算的块直接不进会话（而不是存下来再静默丢弃），并如实告知。
    kept: list[dict] = []
    used = 0
    for b in blocks:
        if used + len(b["text"]) > DOC_CHAR_BUDGET:
            break
        used += len(b["text"])
        kept.append(b)
    truncated_doc = len(kept) < len(blocks)
    used = sum(len(b["text"]) for b in kept)
    if truncated_doc:
        notes.append(
            f"文档较长：只把前 {used} 字（{len(kept)}/{len(blocks)} 段）作为写作依据，"
            "其余部分未纳入。需要用到后面的内容时，可缩小上传范围或把关键段落贴进证据库。"
        )

    full_text = "\n\n".join(texts)
    plan_text = full_text[:80_000]
    if len(full_text) > len(plan_text):
        notes.append("读计划时只看了文档前 8 万字，计划可能没覆盖到后半部分。")

    filenames = [item.filename for item in ok_items]
    session = STORE.get(str(request.query_params.get("sid") or "").strip() or None, create=True)

    genres = _genre_list()
    genre_ids = [str(g.get("id") or "") for g in genres if g.get("id")]
    plan: dict
    fallback = _fallback_plan(plan_text, filenames[0] if filenames else "", genre_ids)
    creds = _creds(request)
    if creds.configured:
        try:
            with llm_mod.Chat(creds, timeout=180.0) as chat:
                plan = _model_plan(chat, plan_text, filenames, genres)
            if not plan.get("title"):
                plan["title"] = fallback["title"]
                plan["notes"].append("文档里没找到明确题名，用了开头的标题行/文件名。")
            if not plan.get("sections"):
                plan["sections"] = fallback["sections"]
                plan["notes"].append("文档里没找到章节结构，用了通用结构，请按需修改。")
        except llm_mod.LLMError as exc:
            plan = fallback
            notes.append(f"模型读取文档失败，已改为按文档结构推导计划：{exc}")
        except Exception as exc:  # noqa: BLE001 - 模型返回的 JSON 形状不对也会走到这里
            plan = fallback
            notes.append(f"模型读取文档失败，已改为按文档结构推导计划：{type(exc).__name__}: {exc}")
    else:
        plan = fallback
        notes.append("未配置模型 Key：这份计划由程序按文档结构推导，题名与结构请务必自己核对。")

    # 模型自述的推断（"结构由我提议""题名由我概括"）要带出去：
    # 这些正是作者最需要知道的部分——哪些是文档里写的、哪些是模型补的。
    plan_notes = [str(n).strip() for n in (plan.get("notes") or []) if str(n).strip()]
    plan["notes"] = plan_notes[:8]

    # 存进会话：/draft 会自动把 document_material 当作 D 层依据。
    session.meta["document_material"] = kept
    session.meta["document_plan"] = plan
    session.meta["document_files"] = filenames
    if plan.get("title"):
        session.meta["writing_title"] = plan["title"]
    session.touch()

    payload = []
    for item in parsed:
        view = item.summary()
        view["text"] = item.text if (full and item.ok) else item.text[:1500]
        view["preview_only"] = item.ok and not full and len(item.text) > 1500
        payload.append(view)

    return {
        "ok": True,
        "sid": session.sid,
        "files": payload,
        "parsed": len(ok_items),
        "failed": len(parsed) - len(ok_items),
        "total_bytes": total_bytes,
        "plan": plan,
        # 写进材料表的依据规模。前端据此显示"这次写作手上有多少依据"。
        "material": {
            "blocks": len(kept),
            "chars": used,
            "total_blocks": len(blocks),
            "truncated": truncated_doc,
            "evidence_count": len(session.evidence),
        },
        "notes": notes,
        "next": "计划可以直接改；改完点「逐章生成」即按它写初稿（文档正文会自动作为依据）。",
    }


@router.post("/section")
async def section(request: Request) -> StreamingResponse:
    """只重写某一章（SSE）。事件与 ``/draft`` 同构：``stage`` / ``section`` / ``result`` / ``error``。

    这是"按需单独生成某一部分章节"的入口。它与整篇生成**共用同一套约束**：

    * 同一张材料表构造函数（D/A/B 三层）；
    * 同一套引用编号校验（越界即通报）；
    * 同一套数字接地复核。

    也就是说，局部重写不会变成"绕过检查的快捷方式"。改完的结果会**推成一个新版本**
    （旧版本保留可回退），而不是就地覆盖——写作是会产生新版本的动作。
    """
    body = await request.json()
    creds = _creds(request)

    sid = str(body.get("sid") or "") or None
    session = STORE.get(sid)
    if session is None:
        raise HTTPException(status_code=404, detail="会话已过期，请重新生成初稿后再重写单章。")
    draft = session.draft
    if not draft or not draft.get("sections"):
        raise HTTPException(status_code=400, detail="还没有稿件：请先生成初稿，再来重写某一章。")

    if not creds.configured:
        raise HTTPException(status_code=400, detail="重写章节需要模型：请先填入你自己的 API Key。")

    sections: list[dict] = list(draft.get("sections") or [])
    heading = str(body.get("heading") or "").strip()
    index_raw = body.get("index")
    index = -1
    if isinstance(index_raw, int) and 0 <= index_raw < len(sections):
        index = index_raw
    elif heading:
        for i, s in enumerate(sections):
            if heading == str(s.get("heading") or "") or heading in str(s.get("heading") or ""):
                index = i
                break
    if index < 0:
        raise HTTPException(
            status_code=400,
            detail="没找到要重写的章节。可用的章节：" + "、".join(str(s.get("heading")) for s in sections),
        )

    target = sections[index]
    target_heading = str(target.get("heading") or heading)
    title = str(body.get("title") or draft.get("title") or session.meta.get("writing_title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="稿件缺少题目，无法重写单章。")
    task = str(body.get("task") or "").strip()[:1500]
    requirements = str(body.get("requirements") or "").strip()[:2000]
    use_web = body.get("use_web")
    use_web = True if use_web is None else bool(use_web)

    user_evidence = list(session.evidence)
    doc_blocks = _document_blocks(session)

    def stream():
        yield _sse({"type": "stage", "stage": "start", "progress": 0.02,
                    "message": f"准备重写「{target_heading}」。"})
        yield _sse({"type": "stage", "stage": "check", "progress": 0.05,
                    "message": (
                        f"D 层（导入文档）：{len(doc_blocks)} 段；"
                        f"A 层（你的资料）：{len(user_evidence)} 条；"
                        f"B 层（联网检索）：{'开启' if use_web else '已关闭'}。"
                    )})
        try:
            web_records: list[websearch.WebRecord] = []
            web_status: list[dict] = []
            if use_web:
                yield _sse({"type": "stage", "stage": "search", "progress": 0.1,
                            "message": "正在检索公开文献库…"})
                try:
                    web_records, web_status = websearch.search_literature(
                        _queries_for(title, target_heading), limit_per_source=3, max_total=8
                    )
                    ok_sources = [s["source"] for s in web_status if s.get("ok")]
                    yield _sse({"type": "stage", "stage": "search", "progress": 0.12,
                                "message": f"检索到 {len(web_records)} 条可用文献"
                                           + (f"（来源：{'、'.join(sorted(set(ok_sources)))}）" if ok_sources else "")})
                except Exception as exc:  # noqa: BLE001 - 检索失败不该让重写停下
                    yield _sse({"type": "stage", "stage": "warn", "progress": 0.12,
                                "message": f"联网检索失败，本章重写降级为不依赖检索：{exc}"})

            material, index_map = _build_material(user_evidence, web_records, doc_blocks)
            yield _sse({"type": "stage", "stage": "writing", "progress": 0.2,
                        "message": f"正在重写「{target_heading}」…"})

            with llm_mod.Chat(creds, timeout=300.0) as chat:
                text = chat.ask(
                    [
                        {"role": "system", "content": _system_prompt()},
                        {"role": "user", "content": _section_prompt(
                            title, target_heading, material, bool(web_records),
                            index == len(sections) - 1, requirements, task)},
                    ],
                    temperature=0.45,
                    max_tokens=6000,
                )

            valid, invalid = _check_citations(text, index_map)
            grounding = _grounding_report(text, index_map)
            usage = _citation_usage(text, index_map)
            tier_d = sum(1 for n in valid if index_map[n]["tier"].startswith("D"))
            tier_a = sum(1 for n in valid if index_map[n]["tier"].startswith("A"))
            tier_b = sum(1 for n in valid if index_map[n]["tier"].startswith("B"))
            words = _count_cjk(text)

            new_section = dict(target)
            new_section.update({
                "body": text.strip(),
                "words": words,
                "citations": {"total": len(valid), "doc": tier_d, "user": tier_a,
                              "web": tier_b, "invalid": len(invalid)},
                "grounding": grounding,
                "uncited_evidence": usage,
                "web_used": len(web_records),
                "regenerated": True,
                "rewrite_task": task,
            })
            new_sections = [dict(s) for s in sections]
            new_sections[index] = new_section

            notes: list[str] = []
            if invalid:
                notes.append(
                    f"本章出现材料表中不存在的引用编号 {sorted(set(invalid))}——"
                    "这些引用是模型编的，请人工核对该段。"
                )
            if grounding["ungrounded_count"]:
                notes.append(
                    f"本章有 {grounding['ungrounded_count']} 处数字在材料里找不到"
                    f"（{'、'.join(grounding['numbers_ungrounded'][:6])}），可能是模型自己算的。"
                )
            if usage["suspected_uncited"]:
                notes.append(
                    f"本章用到了材料里的数字（如 {'、'.join(usage['matched'])}）却没有标注任何引用编号——"
                    "内容有依据，但读者无从回查，请人工补上编号。"
                )
            if task:
                notes.append(f"本次额外要求：{task}")

            cite_doc = sum(int(s.get("citations", {}).get("doc", 0)) for s in new_sections)
            cite_user = sum(int(s.get("citations", {}).get("user", 0)) for s in new_sections)
            cite_web_all = sum(int(s.get("citations", {}).get("web", 0)) for s in new_sections)
            coverage = dict(draft.get("coverage") or {})
            coverage.update({
                "sections": len(new_sections),
                "citations_total": cite_doc + cite_user + cite_web_all,
                "citations_doc": cite_doc,
                "citations_user": cite_user,
                "citations_web": cite_web_all,
                "citations_invalid": int(coverage.get("citations_invalid") or 0) + len(invalid),
                "ungrounded_numbers": int(coverage.get("ungrounded_numbers") or 0)
                                      + grounding["ungrounded_count"],
                # 逐章重算，别累加：重写会把目标章从"没挂编号"变成"挂了编号"，
                # 累加的话这个数只增不减，等于把一个已经修好的问题一直挂在那儿。
                "uncited_evidence_sections": sum(
                    1 for s in new_sections
                    if (s.get("uncited_evidence") or {}).get("suspected_uncited")
                ),
                "doc_blocks": min(len(doc_blocks), MAX_DOC_BLOCKS),
                "doc_blocks_total": len(doc_blocks),
                "web_records_retrieved": int(coverage.get("web_records_retrieved") or 0) + len(web_records),
            })

            version = {
                "title": draft.get("title"),
                "genre": draft.get("genre"),
                "sections": new_sections,
                "citation_count": draft.get("citation_count"),
                "evidence_used": draft.get("evidence_used"),
                "evidence_total": len(user_evidence),
                "web_records_total": coverage.get("web_records_retrieved"),
                "coverage": coverage,
                "scope": draft.get("scope") or {},
                "words": sum(int(s.get("words") or 0) for s in new_sections),
                "notice": f"单独重写了「{target_heading}」" + (f"；{task}" if task else ""),
                "rewritten": target_heading,
                "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            session.push_version(version)
            session.touch()

            yield _sse({"type": "section", "heading": target_heading, "index": index,
                        "section": new_section, "sections": new_sections,
                        "progress": 0.95})
            yield _sse({"type": "stage", "stage": "done", "progress": 1.0,
                        "message": f"「{target_heading}」重写完成，{words} 字。"})
            yield _sse({"type": "result", "sid": session.sid, "draft": version,
                        "coverage": coverage, "notice": " ".join(notes),
                        "rewritten": target_heading, "index": index,
                        "version_count": len(session.versions)})
        except llm_mod.LLMError as exc:
            yield _sse({"type": "error", "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 兜底，别让 SSE 断在半路
            yield _sse({"type": "error", "message": f"重写失败：{type(exc).__name__}: {exc}"})
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
                     f"（导入文档 {coverage.get('citations_doc', 0)}，"
                     f"你的资料 {coverage.get('citations_user', 0)}，"
                     f"联网文献 {coverage.get('citations_web', 0)}）")
        parts.append(f"- 编造的引用编号：{coverage.get('citations_invalid', 0)}")
        parts.append(f"- 找不到依据的数字：{coverage.get('ungrounded_numbers', 0)}")
        parts.append(f"- 导入文档提供的依据块：{coverage.get('doc_blocks', 0)}")
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
