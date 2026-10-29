"""开题报告 · 路由。

定位
----
「参考文献与知识库」阶段解决"有文献、能检索"；本模块解决"**据此写出开题报告**"。
两者刻意分开：前者是资料准备，后者是成稿，失败模式与重试粒度都不同——
资料准备失败只需重跑一次检索；成稿失败往往只想重写某一节。

依据从哪来（四层，与 ``suite/api/writing.py`` 同一套口径）
--------------------------------------------------------
本模块**不发明新的依据来源**，只是把已有的四层重新取一遍。之所以要有四层而不是
"知识库 + 模型知识"，是因为放宽依据来源必须同时**保持每一层可检查**，
否则防编造的防线就从"机制"退化成"提示词里的请求"：

===========  ========================  ==========================  ==========================
层级          来源                      引用编号                    怎么验证
===========  ========================  ==========================  ==========================
K 知识库      本地上传的 PDF/Word/摘要  ``[n]``                     编号必须在材料表内
A 用户证据    会话里录入的文献原文      ``[n]``                     编号必须在材料表内
B 联网检索    Crossref/OpenAlex/arXiv   ``[n]``                     编号在表内 + 记录带真实 DOI
C 模型知识    大模型自身训练所得        无编号                      事后按数字接地复核
===========  ========================  ==========================  ==========================

**C 层是允许存在的**（用户明确要"也可以依据大模型本身的知识"），但它有三个不可
让步的约束：不许挂引用编号（挂了就按越界通报为编造引用）、不许编数字（数字接地
复核会点名）、必须如实计入覆盖度——让作者一眼看出哪几节其实是模型自己写的。

三层合成一张**统一编号**的材料表，编号从 1 起；模型只能用材料表里的编号指代出处，
链接与文献条目一律由我们回填——**不采信模型自己写的参考文献**。

三条复用自写作链路的硬约束（不靠提示词自觉）
--------------------------------------------
1. 引用编号必须在材料表内（越界即通报为编造引用）；
2. 正文数字必须能在材料里找到（年份除外），否则记为"无依据数字"；
3. 「参考文献」一节**不由模型生成**，而是按正文实际引用过的材料机械汇总，
   所以列表里不可能出现没被引用过的文献。

参考文献为什么必须机器生成
--------------------------
让模型写参考文献是幻觉最集中的地方：它会把标题、作者、年份拼得像真的。
这里改成"正文引用了谁，列表里就出现谁"，从机制上消灭"编造一条文献"这类错误。
"""

from __future__ import annotations

import re
import time

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from .. import credentials as creds_mod
from .. import fileintake
from .. import llm as llm_mod
from .. import rag
from .. import websearch
from ..sessions import STORE
from .writing import _check_citations, _creds, _document_blocks, _grounding_report, _sse

router = APIRouter(prefix="/api/proposal", tags=["开题报告"])

#: 开题报告的标准结构。这是**开题报告**的骨架，不是论文的骨架——
#: 论文的章节结构在 ``app/writing/genres.py`` 里（绪论/文献综述/研究设计…），
#: 两者的评审关注点不同（开题看重"值不值得做、怎么做、做得到吗"），所以刻意不复用。
DEFAULT_SECTIONS: tuple[str, ...] = (
    "研究背景与意义",
    "国内外研究现状",
    "研究内容与目标",
    "研究方法与技术路线",
    "可行性分析",
    "进度安排",
    "预期成果",
)

#: 一次最多生成几节。公网服务要限制单次请求的模型花费。
MAX_SECTIONS = 8

#: 每节带进提示词的知识库片段数。开题报告一节通常不需要太多材料，
#: 给多了反而挤占正文预算。
KB_K = 6

#: 每节联网检索带回的文献条数上限。与写作链路保持一致：8 条足够撑起
#: "国内外研究现状"，再多就只是堆砌。
WEB_MAX = 8

#: 材料表条目上限（K + A + B 合计）。
MAX_MATERIAL = 16

#: 联网检索最多占几条材料额度。**必须先给 B 层留够**：知识库检索很容易
#: 一次返回 6 条，按"先到先得"分配的话 B 层（唯一带 DOI、可回查的一层）
#: 会被挤到一条都进不来。
MAX_WEB_MATERIAL = 6

#: 每条材料的截断长度（字符）。开题报告不需要整页原文，摘取要点即可。
MATERIAL_ITEM_CHARS = 900

#: 知识库片段在材料表里保留的最小字符数（低于此值的信息量不足以作为依据）。
MIN_KB_CHARS = 40

#: 一次最多导入几个文件到知识库。
MAX_IMPORT_FILES = 10

#: 导入文件时，单篇入库文本的上限（字符）。超出的部分不进知识库——
#: 不是因为存不下，而是因为检索是**按块**取候选，塞进去一篇 20 万字的专著
#: 会让同一个主题下的候选几乎全来自这一篇，把别的资料挤掉。
IMPORT_DOC_CHARS = 120_000

#: 读"要求/框架"时最多看文档前多少字。与写作链路的 8 万字同一取舍：
#: 框架与要求通常写在最前面。
PLAN_READ_CHARS = 80_000

#: 开题报告常见小节的**同义写法**，用来从上传文档里识别框架。
#:
#: 为什么用关键词而不是让模型判断：这一步决定了"用文档里的框架还是用默认框架"，
#: 判错了整篇的结构就错了，而它本来是一个字符串匹配就能做对的事。
#: 只在没配模型时用（有模型时优先让模型读），但它必须自己也是准的。
SECTION_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("研究背景与意义", ("研究背景", "选题背景", "背景与意义", "研究意义", "选题依据")),
    ("国内外研究现状", ("国内外研究现状", "研究现状", "文献综述", "国内外研究动态", "相关研究")),
    ("研究内容与目标", ("研究内容", "研究目标", "研究目的", "主要内容")),
    ("研究方法与技术路线", ("研究方法", "技术路线", "研究方案", "研究思路")),
    ("可行性分析", ("可行性", "研究基础", "条件分析")),
    ("进度安排", ("进度安排", "时间安排", "研究计划", "工作安排")),
    ("预期成果", ("预期成果", "预期目标", "预期结论", "成果形式")),
)


def _detect_sections(text: str) -> list[str]:
    """从文档里认出开题报告的框架。认不出就返回空表，由调用方退回默认结构。

    **识别不出来时不要硬凑**：返回空表让调用方用默认七节，比返回一个
    只认出一半、顺序还乱的框架好——后者会让用户以为"文档里的结构就是这样"。
    """
    found: list[str] = []
    head = text[:PLAN_READ_CHARS]
    for canonical, aliases in SECTION_ALIASES:
        if any(alias in head for alias in aliases):
            found.append(canonical)
    return found


def _detect_requirements(text: str) -> str:
    """从文档里摘出「要求」段落。

    只摘**带要求字样**的行及其后两行：一份任务书里往往混着大量模板文字，
    整篇交给模型会淹没真正的要求。摘不到就返回空串——**不编**。
    """
    lines = [ln.strip() for ln in text[:PLAN_READ_CHARS].splitlines() if ln.strip()]
    picked: list[str] = []
    for i, line in enumerate(lines):
        if any(k in line for k in ("要求", "须", "必须", "不得", "字数", "格式规范")):
            picked.extend(lines[i:i + 3])
        if len(picked) >= 24:
            break
    # 去重但保持顺序，再掐一个长度上限。
    seen: set[str] = set()
    out: list[str] = []
    for ln in picked:
        if ln not in seen:
            seen.add(ln)
            out.append(ln)
    return "\n".join(out)[:3000]


@router.post("/import-plan")
async def import_plan(
    request: Request,
    files: list[UploadFile] = File(default_factory=list),
) -> dict:
    """上传 Word / PDF（也支持 Markdown / 文本等），读出**框架与要求**，并直接存入知识库。

    这一步同时做三件事，都是"用户不该被要求做第二遍"的事：

    1. **解析**文件（``fileintake``，纯本地，不需要 Key）；
    2. **入库**：正文切块写进 RAG 知识库，于是生成开题报告时它会作为 K 层依据
       被自动检索到——用户不需要再手工点一次"导入资料"；
    3. **抽计划**：从文档里认出开题报告的框架（哪几节）与写作要求。

    第 3 步**不需要模型**（关键词匹配）。与写作链路那个 import-plan 的取舍不同：
    那里的计划要判"论文类型"，语义性更强；而开题报告的框架是固定七节的闭集，
    字符串匹配就足够可靠，也就不必为它单独花一次模型调用、更不必让没配 Key 的用户
    落到一个"计划由程序猜的"降级态上。
    """
    if not files:
        raise HTTPException(status_code=400, detail="没有收到文件。")
    if len(files) > MAX_IMPORT_FILES:
        raise HTTPException(status_code=413,
                            detail=f"一次最多导入 {MAX_IMPORT_FILES} 个文件（收到 {len(files)} 个）。")

    full = str(request.query_params.get("full") or "").strip().lower() in {"1", "true", "yes", "on"}

    added: list[dict] = []
    failed: list[dict] = []
    files_view: list[dict] = []
    texts: list[str] = []
    truncated: list[str] = []
    total_bytes = 0

    for upload in files:
        name = upload.filename or "未命名"
        try:
            data = await upload.read()
        except Exception as exc:  # noqa: BLE001 - 读上传流也可能失败
            failed.append({"filename": name, "error": f"读取失败：{exc}"})
            continue
        finally:
            try:
                await upload.close()
            except Exception:  # noqa: BLE001
                pass

        total_bytes += len(data)
        doc = fileintake.extract(name, data)
        view = doc.summary()
        view["text"] = doc.text if full else doc.text[:1500]
        view["preview_only"] = doc.ok and not full and len(doc.text) > 1500
        files_view.append(view)

        if not doc.ok:
            failed.append({"filename": name, "error": doc.error or doc.warning or "解析不出文字"})
            continue

        title = fileintake.guess_title(doc, fallback=name)
        text = doc.text
        if len(text) > IMPORT_DOC_CHARS:
            truncated.append(f"{name}（保留前 {IMPORT_DOC_CHARS} 字，共 {len(text)} 字）")
            text = text[:IMPORT_DOC_CHARS]
        texts.append(text)

        # 入库。PDF 按页入库（页码是引用的一部分），其它整体入库。
        if doc.chunks and doc.kind == "PDF":
            pages = 0
            for chunk in doc.chunks:
                page_text = str(chunk.get("text") or "")
                if len(page_text.strip()) < 30:
                    continue
                r = rag.add_document(page_text, title=f"{title}（第 {chunk.get('page')} 页）",
                                     origin=name, page=chunk.get("page"),
                                     meta={"from": "开题报告导入", "file": name,
                                           "kind": doc.kind})
                pages += 1 if r.get("ok") else 0
            added.append({"filename": name, "kind": doc.kind, "pages": doc.pages,
                          "chunks": pages, "chars": len(doc.text)})
        else:
            r = rag.add_document(text, title=title, origin=name,
                                 meta={"from": "开题报告导入", "file": name, "kind": doc.kind})
            if r.get("ok"):
                added.append({"filename": name, "kind": doc.kind, "pages": doc.pages,
                              "chunks": r.get("chunks"), "chars": r.get("chars")})
            else:
                failed.append({"filename": name, "error": r.get("error") or "入库失败"})

    if not added:
        reasons = "；".join(f"{f['filename']}: {f['error']}" for f in failed[:4])
        raise HTTPException(status_code=422,
                            detail=f"{len(files)} 个文件都没能入库。{reasons}")

    joined = "\n".join(texts)
    sections = _detect_sections(joined)
    requirements = _detect_requirements(joined)
    # 题名猜法用 fileintake 的同一套（文件名去扩展名），**不发明第二套规则**。
    # 这一阶段不读正文认题名：任务书的题名往往写在文件名上，而从正文里认需要有模型，
    # 那会让"没配 Key"的用户在导入这一步就掉进降级态。
    title_guess = ""
    if added:
        titles = [str(a.get("filename") or "") for a in added]
        title_guess = fileintake.guess_title(
            fileintake.Extracted(filename=titles[0]), fallback=titles[0]) if titles else ""

    notes: list[str] = []
    if not sections:
        notes.append("文档里没认出开题报告的框架小节，已保留默认七节——"
                     "请按你学校的模板自行增删。")
    if not requirements:
        notes.append("文档里没摘到明确的写作要求（字数、格式、创新点要求等），"
                     "可以自己补在下面，或直接把任务书原文贴进去。")
    for t in truncated:
        notes.append(f"文档较长，只入库了前半部分：{t}")

    return {
        "ok": True,
        "files": files_view,
        "added": added,
        "failed": failed,
        "parsed": len(added),
        "total_bytes": total_bytes,
        "plan": {
            "title": title_guess,
            "sections": sections,
            "requirements": requirements,
            "source": "document",
        },
        "kb": rag.stats(),
        "notes": notes,
        "next": "框架与要求已读出，正文也已存入知识库。确认无误后点「逐节生成」；"
                "生成时会自动检索这些内容作为 K 层依据。",
    }


def _clip(text: str, limit: int = MATERIAL_ITEM_CHARS) -> str:
    """把一段材料压成一行、并截断到上限。

    不是装饰：材料表要在一节的上下文预算里装下十几条，原文里的换行与连续空格
    在 token 上纯属浪费；而**截断必须显式标注**——悄悄截断会让模型以为材料只有
    这么长，进而把"没写到"当成"没有"。
    """
    text = re.sub(r"[ \t\u3000]+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " …（已截断）"


def _kb_evidence(query: str) -> tuple[list[dict], dict]:
    """按本节主题从知识库取依据。**失败不致命**：知识库为空时返回空表。"""
    try:
        items, extra = rag.search_as_evidence(query, k=KB_K)
    except Exception as exc:  # noqa: BLE001 - 知识库异常不该让整篇生成中断
        return [], {"error": f"{type(exc).__name__}: {exc}", "hits": 0}
    keep = [x for x in (items or []) if len(str(x.get("text") or "")) >= MIN_KB_CHARS]
    return keep, dict(extra or {}, hits=len(keep))


def _queries_for(title: str, heading: str) -> list[str]:
    """给这一节构造检索词：小节名 + 论文题目，两轮就够。

    与 ``writing._queries_for`` 同样的思路，但**本地重写一份**而不是导入上游那份：
    上游的常量（``MAX_QUERIES_PER_SECTION``）与写作链路绑定，这里直接用 2 这个
    开题报告自己的口径，避免"改写作链路顺手改坏了开题报告"。
    """
    queries: list[str] = []
    cleaned = re.sub(r"^第?\s*\d+\s*[章节]\s*", "", heading or "").strip()
    if cleaned:
        queries.append(f"{title} {cleaned}".strip())
    if title and title not in queries:
        queries.append(title)
    return queries[:2]


def _web_evidence(title: str, heading: str, status_out: list[dict]) -> list[websearch.WebRecord]:
    """按本节主题联网检索真实文献（B 层）。

    **检索失败不致命**：网络或限流问题时返回空表，本节降级为只用 K/A 层。
    这与写作链路同一取舍——少一层依据可以接受，因为整篇生成中断的代价更大。
    """
    try:
        records, status = websearch.search_literature(
            _queries_for(title, heading), limit_per_source=3, max_total=WEB_MAX)
        status_out.extend(status or [])
        return list(records or [])
    except Exception:  # noqa: BLE001
        return []


def _build_material(kb_items: list[dict], user_evidence: list[dict],
                    doc_blocks: list[dict],
                    web_records: list[websearch.WebRecord] | None = None
                    ) -> tuple[str, dict[int, dict]]:
    """把 K 知识库 / A 用户证据 / D 导入文档 / B 联网文献合成一张带统一编号的材料表。

    编号每次重新分配，且**提示词与校验用同一份 index**——两者错位会让引用校验全盘失效。
    知识库排在最前：它是本阶段刚存进去的东西，也是开题报告最主要的依据。

    条目额度按"**先给 B 层留够、再给 A 与 D**"的顺序分配，理由见 ``MAX_WEB_MATERIAL``：
    B 层是唯一带 DOI、能回查原文的一层，被知识库片段挤掉就等于丢掉了可核验性。
    """
    index: dict[int, dict] = {}
    lines: list[str] = []
    web_records = list(web_records or [])

    def add(tier: str, citation: str, text: str, extra: str = "") -> None:
        n = len(index) + 1
        index[n] = {"tier": tier, "citation": citation, "text": text}
        lines.append(f"[{n}]（{tier}）{citation}")
        if extra:
            lines.append(f"    {extra}")
        lines.append(f"    原文：{_clip(text)}")

    for it in kb_items:
        if len(index) >= MAX_MATERIAL:
            break
        title = str(it.get("title") or it.get("citation") or "知识库资料")
        page = it.get("page")
        # **页码已经在标题里就不再拼一次**。PDF 是按页入库的，入库时标题已经被写成
        # 「题名（第 11 页）」，再补一次就会出现「（第 11 页）（第 11 页）」——实测见过。
        # 判据用「第 N 页」这个片段，不依赖括号是半角还是全角。
        already = page is not None and f"第 {page} 页" in title
        add("K 知识库",
            f"{title}（第 {page} 页）" if (page is not None and not already) else
            (title if already else f"{title}（知识库片段）"),
            str(it.get("text") or ""))

    # B 层的额度先算出来，剩下的才给 A 与 D。
    b_quota = min(MAX_WEB_MATERIAL, max(0, MAX_MATERIAL - len(index)))
    ad_quota = max(0, MAX_MATERIAL - len(index) - b_quota)

    for it in user_evidence[:ad_quota]:
        add("A 用户提供",
            str(it.get("citation") or it.get("title") or "来源未标注"),
            str(it.get("text") or it.get("abstract") or ""),
            f"年份：{it['year']}" if it.get("year") else "")

    for block in doc_blocks:
        if len(index) >= MAX_MATERIAL - b_quota:
            break
        add("D 导入文档",
            str(block.get("origin") or "导入文档"),
            str(block.get("text") or ""),
            f"位置：第 {block['page']} 页" if block.get("page") else "")

    remaining = MAX_MATERIAL - len(index)
    for rec in web_records[: max(0, min(remaining, b_quota))]:
        add("B 联网检索", rec.citation(), rec.abstract,
            "、".join(x for x in (
                f"来源库：{rec.source}",
                f"DOI：{rec.doi}" if rec.doi else "",
                f"链接：{rec.url}" if rec.url else "",
                f"被引：{rec.citations}" if rec.citations is not None else "",
            ) if x))

    return "\n".join(lines) if lines else "（本次没有任何可用材料）", index


def _material_index(rows: list[dict]) -> dict[int, dict]:
    """把最终采用的编号映射成 {n: {citation, tier, ...}}，用于生成参考文献表。"""
    out: dict[int, dict] = {}
    for r in rows:
        try:
            out[int(r["n"])] = r
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _system_prompt() -> str:
    return (
        "你是论文开题报告的写作助手，服务对象是正在准备学位论文开题的中文作者。\n"
        "硬性要求：\n"
        "1. 只写用户要求的那一节，不要重复其它小节，也不要写整篇。\n"
        "2. 「可用材料」分成四层，用方括号里的编号指代：（K 知识库）是作者导入的"
        "文献资料，（A 用户提供）是作者录入的证据，（B 联网检索）是从公开文献库"
        "检索到的真实记录。\n"
        "3. **只能使用材料表里出现过的编号**。材料表里没有的编号一律不许写——"
        "那等于编造出处。\n"
        "4. 你**可以**依据自己的知识，但必须与材料区分开：凡出自你自己知识而不来自"
        "材料的内容**一律不许挂编号**，并用「一般认为」「既有研究多主张」这类表述"
        "说明它来自常识性认识而非具体文献。不要把模型知识写成有出处的样子。\n"
        "5. 正文里出现的数字必须来自材料；不要自己推算或估计出一个数。"
        "需要给区间或规模时，写成待调研、待验证的表述。\n"
        "6. 不要自己写参考文献列表，参考文献由系统按引用机械生成。\n"
        "7. 用学术书面中文，客观克制，不要「综上所述」式的空话堆砌，不要营销腔。"
    )


def _section_prompt(title: str, heading: str, material: str, has_material: bool,
                    is_last: bool, requirements: str = "") -> str:
    today = time.strftime("%Y-%m-%d")
    req = f"\n写作要求（来自作者，优先遵守）：\n{requirements}\n" if requirements else ""
    return (
        f"论文题目：{title}\n"
        f"今天日期：{today}\n"
        f"{req}\n"
        f"本次要写的小节：{heading}\n\n"
        "可用材料：\n"
        f"{material}\n\n"
        f"请写出「{heading}」这一节的正文。"
        + ("这一节要收束全文，可以给出预期成果与后续工作。" if is_last else "")
        + ("\n材料有限时，宁可写得具体而克制，也不要堆砌没有依据的套话。"
           if not has_material else
           "\n材料使用要贴题：与本小节无关的材料不要硬塞进来。")
        + "\n只输出这一节的正文，第一行用 Markdown 二级标题（## 小节名），不要写其它小节。"
    )


def _reference_lines(index: dict[int, dict], cited: list[int]) -> list[str]:
    """按**正文实际引用过**的编号生成参考文献表。

    这就是"参考文献不由模型生成"的落实点：列表内容完全来自材料表，
    因此不可能出现材料里没有的条目。
    """
    out: list[str] = []
    for n in sorted(set(cited)):
        item = index.get(n)
        if not item:
            continue
        out.append(f"[{n}] {item.get('citation') or '来源未标注'}")
    return out


@router.get("/options")
def options() -> dict:
    """开题报告的标准小节与相关能力开关。**不需要 Key**。"""
    try:
        stats = rag.stats()
    except Exception:  # noqa: BLE001
        stats = {}
    return {
        "ok": True,
        "sections": list(DEFAULT_SECTIONS),
        "kb": stats,
        "note": "开题报告以知识库中的资料为主要依据；知识库为空时也能生成，但会明确提示依据不足。",
    }


@router.post("/generate")
async def generate(request: Request) -> StreamingResponse:
    """依据知识库与证据逐节生成开题报告（SSE）。

    事件：``stage`` 进度 / ``section`` 单节完成 / ``result`` 完成 / ``error``。
    """
    body = await request.json()
    creds = _creds(request)

    title = str(body.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="请先填写论文题目。")

    sections = [str(s).strip() for s in (body.get("sections") or []) if str(s).strip()]
    if not sections:
        sections = list(DEFAULT_SECTIONS)
    sections = sections[:MAX_SECTIONS]

    use_kb = body.get("use_kb")
    use_kb = True if use_kb is None else bool(use_kb)

    # 联网检索默认**开启**：开题报告的「国内外研究现状」一节几乎必然需要真实文献，
    # 而知识库里未必有。但它可以用开关关掉——有些学校不许引用未经核实的题录，
    # 而且它每次要访问三个外部库、会明显变慢。
    use_web = body.get("use_web")
    use_web = True if use_web is None else bool(use_web)

    # 写作要求：来自「导入 Word/PDF」那一步（框架 / 要求 / 任务书）。
    requirements = str(body.get("requirements") or "").strip()[:4000]

    sid = str(body.get("sid") or "") or None
    session = STORE.get(sid, create=True)
    user_evidence = list(session.evidence)
    doc_blocks = _document_blocks(session)

    def stream():
        yield _sse({"type": "stage", "stage": "start", "progress": 0.02,
                    "message": "已连接，正在读取知识库…"})

        if not creds.configured:
            yield _sse({"type": "error",
                        "message": "生成开题报告需要模型：请先在页面右上角填入你自己的 API Key。"})
            yield "data: [DONE]\n\n"
            return

        try:
            kb_stats = rag.stats()
        except Exception:  # noqa: BLE001
            kb_stats = {}

        yield _sse({
            "type": "stage", "stage": "check", "progress": 0.05,
            "message": (f"知识库文档 {kb_stats.get('documents', 0)} 篇 / 片段 {kb_stats.get('chunks', 0)} 条；"
                        f"A 层证据 {len(user_evidence)} 条；D 层文档块 {len(doc_blocks)} 段；"
                        f"B 层联网检索：{'开启' if use_web else '已关闭'}；"
                        f"小节 {len(sections)} 个。"),
        })

        # 三层可用的依据全空时**不拒绝生成**（用户明确要求"也可以依据大模型本身的知识"），
        # 但必须把"这一篇没有可回查的依据"说在最前面——否则作者会以为正文里的引用
        # 是查得到的。
        if not kb_stats.get("chunks") and not user_evidence and not doc_blocks and not use_web:
            yield _sse({
                "type": "stage", "stage": "warn", "progress": 0.06,
                "message": ("知识库为空、也没有录入证据，且联网检索已关闭：本次只能依据"
                            "模型自身知识写，正文不会有任何可回查的引用依据。"
                            "建议先到「参考文献与知识库」阶段导入几篇 PDF / Word 资料。"),
            })

        produced: list[dict] = []
        notes: list[str] = []
        cited_all: list[int] = []
        invalid_all: list[int] = []
        ungrounded_all: list[str] = []
        final_index: dict[int, dict] = {}
        web_seen: dict[str, websearch.WebRecord] = {}
        web_status_all: list[dict] = []
        web_total = 0
        started = time.time()

        try:
            with llm_mod.Chat(creds, timeout=300.0) as chat:
                for i, heading in enumerate(sections, start=1):
                    base = 0.06 + 0.88 * ((i - 1) / len(sections))

                    # ---- K 层：按本小节主题检索知识库 ----
                    kb_items: list[dict] = []
                    if use_kb:
                        yield _sse({"type": "stage", "stage": "search", "progress": base,
                                    "message": f"第 {i}/{len(sections)} 节：正在检索知识库…"})
                        kb_items, kb_extra = _kb_evidence(f"{title} {heading}")
                        if kb_extra.get("error"):
                            notes.append(f"第 {i} 节知识库检索失败：{kb_extra['error']}")

                    # ---- B 层：联网检索（失败不影响成稿，只是少一层依据）----
                    web_records: list[websearch.WebRecord] = []
                    if use_web:
                        yield _sse({"type": "stage", "stage": "search", "progress": base,
                                    "message": f"第 {i}/{len(sections)} 节：正在检索公开文献库…"})
                        web_records = _web_evidence(title, heading, web_status_all)
                        ok_sources = sorted({s["source"] for s in web_status_all if s.get("ok")})
                        yield _sse({
                            "type": "stage", "stage": "search", "progress": base,
                            "message": (f"检索到 {len(web_records)} 条可用文献"
                                        + (f"（来源：{'、'.join(ok_sources)}）" if ok_sources else "")
                                        + ("；检索源均不可用，本节只用知识库与模型知识。"
                                           if not web_records else "")),
                        })

                    web_total += len(web_records)
                    for rec in web_records:
                        key = re.sub(r"[\s\W_]+", "", rec.title.lower())[:60] or rec.title
                        web_seen.setdefault(key, rec)

                    material, index = _build_material(kb_items, user_evidence, doc_blocks,
                                                      web_records)
                    final_index.update(index)
                    has_material = bool(index)

                    if not index:
                        yield _sse({"type": "stage", "stage": "warn", "progress": base,
                                    "message": f"「{heading}」没有可用材料：本节将完全依据模型知识，"
                                               "不会有引用依据。"})

                    yield _sse({"type": "stage", "stage": "writing",
                                "progress": base + 0.05,
                                "message": f"正在写第 {i}/{len(sections)} 节：{heading}"})

                    try:
                        text = chat.ask(
                            [
                                {"role": "system", "content": _system_prompt()},
                                {"role": "user", "content": _section_prompt(
                                    title, heading, material, has_material,
                                    i == len(sections), requirements)},
                            ],
                            temperature=0.5,
                            max_tokens=4000,
                        )
                    except llm_mod.LLMError as exc:
                        notes.append(f"第 {i} 节（{heading}）生成失败：{exc}")
                        yield _sse({"type": "stage", "stage": "warn", "progress": base,
                                    "message": f"「{heading}」失败，已跳过：{exc}"})
                        if llm_mod.is_fatal(str(exc)) or produced:
                            break
                        raise

                    # ---- 约束 1：引用编号必须在材料表内 ----
                    valid, invalid = _check_citations(text, index)
                    cited_all.extend(valid)
                    invalid_all.extend(invalid)
                    if invalid:
                        notes.append(f"第 {i} 节出现材料表中不存在的引用编号 {sorted(set(invalid))}——"
                                     "属编造引用，已通报，请人工核对。")

                    # ---- 约束 2：数字接地 ----
                    grounding = _grounding_report(text, index)
                    ungrounded_all.extend(grounding["numbers_ungrounded"])
                    if grounding["ungrounded_count"]:
                        notes.append(f"第 {i} 节有 {grounding['ungrounded_count']} 处数字在材料里找不到"
                                     f"（{'、'.join(grounding['numbers_ungrounded'][:6])}）。")

                    item = {"heading": heading, "body": text.strip(),
                            "citations": sorted(set(valid)),
                            "materials": len(index)}
                    produced.append(item)
                    yield _sse({"type": "section", "progress": base + 0.09,
                                "partial": {"title": title, "sections": list(produced)}})

        except llm_mod.LLMError as exc:
            yield _sse({"type": "error", "message": str(exc)})
            yield "data: [DONE]\n\n"
            return

        if not produced:
            yield _sse({"type": "error",
                        "message": "没有任何一节生成成功，请检查 API Key 与余额后重试。"})
            yield "data: [DONE]\n\n"
            return

        # ---- 参考文献：按正文实际引用机械汇总，不由模型生成 ----
        refs = _reference_lines(_material_index(
            [{"n": n, **v} for n, v in final_index.items()]), cited_all)

        # B 层的分层计数单独给出来。**不能只报一个"引用总数"**：
        # 作者最需要知道的是"这篇开题报告里有多少条是能回查 DOI 的"，
        # 而 K/A/D 三层都是他自己导入的东西，可核验性完全不同。
        tier_counts: dict[str, int] = {}
        for item in final_index.values():
            tier = str(item.get("tier") or "未知")
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        draft = {
            "title": title,
            "sections": produced,
            "references": refs,
            "words": sum(len(str(s["body"])) for s in produced),
        }
        coverage = {
            "documents": kb_stats.get("documents", 0),
            "chunks": kb_stats.get("chunks", 0),
            "kb_hits": tier_counts.get("K 知识库", 0),
            "user_evidence": len(user_evidence),
            "doc_blocks": len(doc_blocks),
            "web_found": web_total,
            "web_unique": len(web_seen),
            "material_tiers": tier_counts,
            "web_sources": sorted({s["source"] for s in web_status_all if s.get("ok")}),
            "citations_total": len(set(cited_all)),
            "citations_invalid": len(set(invalid_all)),
            "ungrounded_numbers": len(set(ungrounded_all)),
            "references": len(refs),
            "use_kb": use_kb,
            "use_web": use_web,
            "has_requirements": bool(requirements),
            "elapsed_seconds": round(time.time() - started, 1),
        }
        yield _sse({"type": "result", "sid": session.sid, "draft": draft,
                    "coverage": coverage, "notes": notes})
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
