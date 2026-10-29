"""PaperQA2 子进程桥接：在自己的解释器里跑，把结果以 JSONL 打到 stdout。

为什么必须是子进程
------------------
PaperQA2 钉 numpy 2.5.x / pydantic 2.14，而本站主程序跑在 numpy 2.3 / pydantic 2.13 上。
同一个解释器里加载两者，必有一方静默拿到错误的版本——"静默"是关键词：
它不会报导入错误，而是在某个不相干的地方表现出奇怪的行为。所以走子进程 + stdin/stdout。

与 ``thesis-assistant/tools/pqa_bridge.py`` 的关系
-------------------------------------------------
那份只做一件事（对语料提问）。这份多两个动作——**建索引**与**文献简报**——
因为本站的开题报告阶段需要"选中文献 → 看简报/总结"，那是一个和问答不同的提示词。
两者都建立在同一套 PaperQA2 用法与同一组环境结论上（见下），不是各自摸索出来的。

环境结论（都在 thesis-assistant 里实测过，这里照搬，别再踩一遍）
---------------------------------------------------------------
* ``disable_doc_valid_check=True`` **对中文是必须的**。PaperQA2 的
  ``maybe_is_text()`` 只接受每字符熵在 2.5~8.0 之间的文本；这个上限是按字母文字
  校准的（英文上界 log2(25)≈4.7），中文有约 1210 个相异字符，上界 log2(1210)≈10.24，
  所以**任何超过约 5000 字的中文论文都会被判为"不是文本"**。
* ``use_doc_details=False``：CNKI 论文在 Crossref / Semantic Scholar 里查不到记录，
  开着它只会白等网络超时。书目信息由 ``suite/paperqa.py`` 写进正文开头替代。
* **智能体路径是坏的**：``fhlmi 1.0.7`` 把 litellm 垫片定义成只收关键字参数，
  而 ``aviary`` 的 ``ToolSelector`` 把模型名作为第一个位置参数绑定进去，
  于是任何走 agent 的调用都抛 ``TypeError: _acompletion() takes 0 positional
  arguments but 1 was given``。绕开办法是走 ``Docs.aquery`` 手动 API，
  它不经过工具选择器。**不要**改成 ``ask()`` 之类的智能体入口。

协议
----
``argv[1]`` 是一个 JSON 请求::

    {"action": "ask"|"brief", "corpus_dir": str, "index_dir": str,
     "llm": "deepseek/deepseek-chat", "embed": "st-BAAI/bge-small-zh-v1.5",
     "question": str,       # action=ask
     "mode": "brief"|"digest", "focus": str,   # action=brief
     "rebuild": bool}

``stdout`` 每行一个 JSON 对象::

    {"type": "progress", "message": str}
    {"type": "index",    "docs": int, "seconds": float, "reused": bool}
    {"type": "sources",  "sources": [...]}
    {"type": "delta",    "text": str}
    {"type": "done",     "answer": str, "seconds": float, "cost": float, "reused": bool}
    {"type": "error",    "message": str, "detail": str}
"""

from __future__ import annotations

import asyncio
import json
import os
import pickle
import re
import sys
import time
from pathlib import Path

# --------------------------------------------------------------------------
# 环境必须在 import paperqa / huggingface 之前配好。
# 注意：这里**不从任何项目的 .env 里偷密钥**——凭据由父进程通过 env 显式传入
# （见 suite/paperqa.py 的 _child_env）。这个子进程只信环境变量。
# --------------------------------------------------------------------------
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

DEFAULT_EMBED = "st-BAAI/bge-small-zh-v1.5"

#: 问答用的提示词。用英文写是**故意的**：PaperQA2 的 answer 提示词模板本身是英文，
#: 中英混写会让模型在两套格式约定之间摇摆；输出语言用一句话限定即可。
#: 结论来自实测——中英混写时模型有时会把 "Sources" 段漏掉。
ASK_SYSTEM = (
    "You are answering a question about the provided papers. "
    "Answer in **Chinese**. Cite every claim with the context keys given to you, "
    "like [1] or [2]. If the papers disagree, say so explicitly and name which "
    "paper holds which view. If the papers do not contain enough information, say "
    "so plainly instead of guessing. Do not use outside knowledge as if it came "
    "from these papers."
)

#: 文献简报：**固定小节**。为什么固定而不是让模型自由发挥——
#: 自由发挥的简报每次结构都不一样，用户没法比较两篇文献；而开题报告要的正是
#: "同一套维度看多篇"。八个小节与 thesis-assistant 的单篇总结保持一致，
#: 这样两个工具的输出可以直接对照。
BRIEF_SECTIONS = (
    "基本信息",
    "研究问题",
    "方法与数据",
    "主要结论",
    "创新点",
    "局限与不足",
    "对本文选题的借鉴",
    "关键术语",
)

BRIEF_QUESTIONS = {
    "基本信息": "这篇文献的题目、作者、发表期刊/年份是什么？（只依据文献信息）",
    "研究问题": "这篇文献要解决的核心研究问题是什么？作者为什么要研究它？",
    "方法与数据": "它用了什么研究方法、什么数据（样本、来源、时间跨度）？",
    "主要结论": "它的主要结论有哪些？请给出文献中的具体发现。",
    "创新点": "它相对已有研究的新意在哪里？",
    "局限与不足": "作者自己承认或可以从文中看出的局限是什么？",
    "对本文选题的借鉴": "这篇文献对「{focus}」这个选题有什么可借鉴之处？"
                        "如果关系不大，就直接说关系不大。",
    "关键术语": "这篇文献里最重要的几个专业术语是什么？各自是什么意思？",
}

DIGEST_QUESTIONS = {
    "共同主题": "这几篇文献共同关注的研究主题是什么？",
    "共识": "它们在哪些判断上是一致的？请分别指出是哪几篇。",
    "分歧": "它们之间存在哪些实质分歧或结论冲突？**必须指出具体是哪几篇持哪种观点。**",
    "方法差异": "它们的方法与数据有什么差异？哪些结论因为数据口径不同而**不可直接比较**？",
    "演进脉络": "从时间上看，这个方向的研究脉络是怎样的？",
    "研究空白": "综合来看，还有哪些问题没有被回答？",
    "对本文选题的借鉴": "这批文献对「{focus}」这个选题有什么可借鉴之处？",
}


def emit(**payload) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 设置
# ---------------------------------------------------------------------------
def build_settings(llm: str, embed: str, *, brief: bool = False):
    """构造 PaperQA2 的 Settings。三个中文相关的开关都在这里，理由见模块 docstring。"""
    from paperqa import Settings
    from paperqa.settings import ParsingSettings

    settings = Settings(
        llm=llm,
        summary_llm=llm,
        embedding=embed or DEFAULT_EMBED,
        temperature=0.0,
        verbosity=0,
        parsing=ParsingSettings(
            # 中文长文必须关掉有效性检查，否则整篇被判成"不是文本"。
            disable_doc_valid_check=True,
            # CNKI 论文查不到 Crossref 记录，开着只会白等超时。
            use_doc_details=False,
            multimodal=False,
        ),
    )
    if brief:
        # 简报要"每篇文献都覆盖到"，所以证据条数给得比问答宽：
        # 一条证据大概对应一个段落，证据太少会让后面的小节无话可说。
        settings.answer.evidence_k = 14
        settings.answer.answer_max_sources = 10
    else:
        settings.answer.evidence_k = 10
        settings.answer.answer_max_sources = 6
    return settings


def corpus_signature(corpus_dir: Path) -> dict:
    """语料指纹。**含每个文件的 mtime 与大小**：只比路径列表的话，
    同名文件被改了内容不会触发重建，索引就会静默过期。"""
    files = sorted(p for p in corpus_dir.rglob("*") if p.is_file())
    return {
        "files": [[str(f.relative_to(corpus_dir)), f.stat().st_size, int(f.stat().st_mtime)]
                  for f in files],
    }


async def get_docs(corpus_dir: Path, index_dir: Path, settings, rebuild: bool):
    """取索引：能复用就复用，不能就重建，并把过程如实 emit 出去。"""
    from paperqa import Docs

    cache_file = index_dir / "docs.pkl"
    sig_file = index_dir / "signature.json"
    signature = corpus_signature(corpus_dir)

    if not rebuild and cache_file.is_file() and sig_file.is_file():
        try:
            if json.loads(sig_file.read_text(encoding="utf-8")) == signature:
                with cache_file.open("rb") as fh:
                    docs = pickle.load(fh)
                emit(type="progress", message=f"复用已有索引（{len(docs.docs)} 篇）")
                # 冷启动的 10~25 秒主要花在加载本地向量模型上，这一步无法省。
                emit(type="progress", message="加载本地向量模型…")
                # 载入后必须重新对齐 settings：pickle 里存的是旧 Settings，
                # 直接用会让本次改的 evidence_k 等参数失效（实测踩过）。
                docs._settings = settings
                return docs, True
        except Exception as exc:  # noqa: BLE001 - 缓存坏了就重建，不阻断
            emit(type="progress", message=f"索引缓存不可用，将重建（{type(exc).__name__}: {exc}）")

    docs = Docs()
    files = sorted(p for p in corpus_dir.rglob("*") if p.is_file())
    emit(type="progress", message=f"建立 PaperQA2 索引：{len(files)} 个文件")
    started = time.time()
    for i, path in enumerate(files, 1):
        emit(type="progress", message=f"[{i}/{len(files)}] {path.stem[:60]}")
        try:
            # 传子目录名（doc_id）以外的文件名即可；PaperQA2 用 stem 当 citation。
            await docs.aadd(path, settings=settings)
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不该毁掉整次索引
            emit(type="progress",
                 message=f"  跳过 {path.stem[:40]}：{type(exc).__name__}: {str(exc)[:120]}")
    seconds = time.time() - started
    emit(type="index", docs=len(docs.docs), seconds=round(seconds, 1), reused=False)
    try:
        with cache_file.open("wb") as fh:
            pickle.dump(docs, fh)
        sig_file.write_text(json.dumps(signature, ensure_ascii=False), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        emit(type="progress", message=f"索引缓存写入失败（下次会重建）：{exc}")
    return docs, False


def collect_sources(session) -> list[dict]:
    """把 PaperQA2 的 context 列表转成前端能渲染的来源。

    ``index`` 是 1-based 顺序号，**答案里的 ``[1]`` ``[2]`` 就是指向它**；
    ``key`` 是模型与 PaperQA2 内部用的 chunk 标识（实测形如 ``pqac-03426f32``）；
    ``name`` 是人类可读的 chunk 名（实测形如 ``Agyapong2022 chunk 1``），
    答案的 prose 里出现的往往就是这个 name —— 归一化要靠它。
    """
    raw = getattr(session, "contexts", None)
    if not raw:
        raw = getattr(getattr(session, "context", None), "contexts", None) or []
    sources = []
    for i, ctx in enumerate(raw, 1):
        summary = getattr(ctx, "context", "") or ""
        inner = getattr(ctx, "text", None)
        citation = ""
        doc_name = ""
        chunk_name = ""
        if inner is not None:
            doc = getattr(inner, "doc", None)
            citation = str(getattr(doc, "citation", "") or "")
            doc_name = str(getattr(doc, "dockey", "") or "")
            chunk_name = str(getattr(inner, "name", "") or "")
        sources.append({
            "index": i,
            "key": str(getattr(ctx, "id", f"ctx-{i}")),
            # name 与 key 都透出：前者用于把 prose 里的引用改写成 [n]，
            # 后者是 PaperQA2 自己的 Valid Keys，排查时用得上。
            "name": chunk_name,
            "text": str(summary),
            "citation": citation,
            "dockey": doc_name,
            "score": getattr(ctx, "score", None),
        })
    return sources


#: 匹配答案 prose 里的引用标记。**形状来自实测，不是猜的**：
#:
#: * ``(Agyapong2022 chunk 1)`` —— PaperQA2 期望的 ``(pqac-xxxx)`` 形式没被模型照做时
#:   留下的原样，也是我们最常看到的一种；
#: * ``(pqac-03426f32)`` / ``(pqac-03426f32, pqac-0f650d59)`` —— 模型照做时的形式；
#: * ``[1]`` ``[2,3]`` —— 我们自己归一化之后的形式（再次归一化要幂等）。
#:
#: 只匹配**紧邻括号内**的内容，所以正文里的 ``(2023)`` 这类年份不会被误伤。
_CITE_PAREN = re.compile(r"\(([A-Za-z0-9_\-]+(?:[ \t]+chunk[ \t]+\d+)?(?:[ \t]*[,;][ \t]*[A-Za-z0-9_\-]+(?:[ \t]+chunk[ \t]+\d+)?)*)\)")
_CITE_BRACKET = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")


def normalize_citations(text: str, sources: list[dict]) -> str:
    """把答案里的引用标记统一改写成 ``[n]``，与来源清单的顺序号对齐。

    为什么必须做这一步：实测模型**不总是照 PaperQA2 要求的 ``(pqac-xxxx)`` 格式写**，
    它会写成 ``(Agyapong2022 chunk 1)``（chunk 的可读名）。于是同一篇回答里出现两种
    引用形状——prose 里是 ``(Agyapong2022 chunk 1)``，References 里是
    ``1. (Agyapong2022 chunk 1): …`` —— 而下面的来源卡片按 ``[1] [2]`` 编号，
    三者对不上，读者没法回查。

    改写而不是"教模型写对"：提示词是概率的，它在某些模型/某些问题上必然失效；
    而 chunk 名到序号是一张确定的表，改写是确定的。**对不上的标记原样保留**——
    宁可留下一个读者认不出的标记，也不要把它错配到另一篇文献上。
    """
    table: dict[str, int] = {}
    for src in sources or []:
        idx = src.get("index")
        if not isinstance(idx, int):
            continue
        for token in (src.get("name"), src.get("key")):
            token = str(token or "").strip()
            if token:
                table[token.lower()] = idx

    if not table:
        return text

    def repl_paren(match: re.Match) -> str:
        parts = [p.strip() for p in re.split(r"[,;]", match.group(1)) if p.strip()]
        hits = [table.get(p.lower()) for p in parts]
        if all(h is not None for h in hits):
            return "[" + ",".join(str(h) for h in hits) + "]"
        return match.group(0)

    def repl_bracket(match: re.Match) -> str:
        # 已经是 [n] 形式：保持（幂等），只统一分隔符。
        parts = [p.strip() for p in re.split(r"[,;]", match.group(1)) if p.strip()]
        return "[" + ",".join(parts) + "]"

    text = _CITE_PAREN.sub(repl_paren, text)
    return _CITE_BRACKET.sub(repl_bracket, text)


def rewrite_references(references: str, sources: list[dict]) -> str:
    """把 ``References`` 段里的 ``1. (chunkname chunk 1): 书目`` 改写成 ``[1] 书目``。

    PaperQA2 给的这个段其实很有用——它带完整书目（期刊、卷期、页码、DOI），
    比来源卡片里的 citation 更全。但它的编号与括号里那个 chunk 名对读者是噪音，
    而下面的来源卡片已经有 ``[1] [2]``。所以保留书目、换掉前缀。
    """
    table: dict[str, int] = {}
    for src in sources or []:
        idx = src.get("index")
        if not isinstance(idx, int):
            continue
        for token in (src.get("name"), src.get("key")):
            token = str(token or "").strip()
            if token:
                table[token.lower()] = idx

    lines: list[str] = []
    for raw in (references or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(?:\[\d+\]|\d+\.)\s*\(?([^)]*)\)?\s*:?\s*(.*)$", line)
        if m:
            head, body = m.group(1).strip(), m.group(2).strip()
            idx = None
            for token in (head,):
                idx = table.get(token.lower())
                if idx is not None:
                    break
            if idx is not None and body:
                lines.append(f"[{idx}] {body}")
                continue
        lines.append(line)
    return "\n".join(lines)


def split_plain(session, sources: list[dict] | None = None) -> str:
    """取最终答案，把引用统一成 ``[n]``，并把 References 段接在后面。

    实测两种取法的差别（同一篇文献、同一个问题）::

        session.answer            "…研究强调…(Agyapong2022 chunk 1)。"
        session.formatted_answer  "Question: …\\n\\n…(Agyapong2022 chunk 1)。\\n\\nReferences\\n1. (chunk): 书目"

    两者给出的引用标记都**不是** ``[n]``。所以这里：

    1. 正文取自 ``formatted_answer``（它与 ``answer`` 的正文一致，且自带 References），
       但**去掉开头的 ``Question:`` 回声**——那是我们自己问的话，答案区再重复一遍
       只会把正文挤下去。
    2. 正文与 References 里的引用标记统一改写成 ``[n]``（``normalize_citations``）。
    3. 保留 References 段：它带完整书目，比来源卡片的 citation 更全。
    """
    sources = sources or []
    formatted = str(getattr(session, "formatted_answer", "") or "").strip()
    plain = str(getattr(session, "answer", "") or "").strip()
    text = formatted or plain

    # 1) 去掉 Question 回声。**在子进程里做**而不是前端：这里能看到原始格式，
    #    前端只能靠正则猜，而两个地方各猜一次就是在制造两套口径。
    text = re.sub(r"^\s*Question:\s*[^\n]*\n+", "", text)

    # 2) 切出正文与 References。
    body, _, refs = text.partition("\nReferences\n")
    body = normalize_citations(body.strip(), sources)
    refs = rewrite_references(refs, sources)
    return (body + ("\n\n**参考来源**\n\n" + refs if refs else "")).strip()


# ---------------------------------------------------------------------------
# 动作
# ---------------------------------------------------------------------------
async def do_ask(req: dict, docs, settings) -> int:
    question = str(req.get("question") or "").strip()
    emit(type="progress", message="检索并综合证据…")

    # 逐字流式输出：一次问答 10~40 秒，等跑完再返回会让用户以为卡死。
    def on_chunk(chunk: str) -> None:
        emit(type="delta", text=chunk)

    started = time.time()
    session = await docs.aquery(question, settings=settings, callbacks=[on_chunk])
    elapsed = time.time() - started

    sources = collect_sources(session)
    emit(type="sources", sources=sources)
    emit(type="done", answer=split_plain(session, sources), seconds=round(elapsed, 1),
         cost=float(getattr(session, "cost", 0) or 0), reused=bool(req.get("_reused")))
    return 0


async def do_brief(req: dict, docs, settings) -> int:
    """结构化简报：**逐节独立提问**，而不是让模型一次写完整篇。

    为什么逐节问：一次问八个小节，模型会把前面的写得详细、后面的草草收尾
    （尤其是长上下文模型），而且一旦某节跑偏没法单独重来。逐节问每节都拿到
    完整的证据预算，代价是多花几次调用——简报本来就是低频操作，值得。
    """
    mode = str(req.get("mode") or "brief").strip()
    focus = str(req.get("focus") or "").strip() or "（未填写，按文献自身的主题判断）"
    table = DIGEST_QUESTIONS if mode == "digest" else BRIEF_QUESTIONS

    total_cost = 0.0
    all_sources: list[dict] = []
    seen_keys: set[str] = set()
    sections: list[dict] = []
    started = time.time()

    for i, (section, question) in enumerate(table.items(), 1):
        emit(type="progress", message=f"[{i}/{len(table)}] {section}")
        try:
            session = await docs.aquery(question.format(focus=focus), settings=settings)
        except Exception as exc:  # noqa: BLE001 - 单节失败不该毁掉整篇简报
            sections.append({"section": section, "text": "",
                             "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        total_cost += float(getattr(session, "cost", 0) or 0)

        # 每一节都是**独立的一次问答**，所以它自己的来源从 1 开始编号。
        # 直接把各节的编号拼在一起会让 "[1]" 在整篇简报里指向两篇不同文献，
        # 所以这里按已累计的来源数给本节加一个偏移，让 [n] 全篇唯一。
        local = collect_sources(session)
        offset = len(all_sources)
        for src in local:
            if src["key"] in seen_keys:
                # 同一篇文献的同一个片段在多节里都会被检索到。跳过重复项，
                # 代价是全局序号不严格连续——比让 [3] 指向两篇不同文献好。
                continue
            seen_keys.add(src["key"])
            item = dict(src)
            # 全局序号 = 本节起点的全局序号 + 本节内序号。
            item["index"] = offset + int(src["index"])
            all_sources.append(item)
        remap = {int(s["index"]): offset + int(s["index"]) for s in local}
        text = split_plain(session, local)
        text = _CITE_BRACKET.sub(
            lambda m: "[" + ",".join(
                str(remap.get(int(p.strip()), int(p.strip())))
                for p in re.split(r"[,;]", m.group(1)) if p.strip()) + "]",
            text)
        sections.append({"section": section, "text": text, "error": ""})

    elapsed = time.time() - started
    title = "多篇文献对比综述" if mode == "digest" else "文献简报"
    ok_sections = [s for s in sections if (s.get("text") or "").strip()]
    emit(type="sources", sources=all_sources)
    emit(type="done", answer="", seconds=round(elapsed, 1), cost=total_cost,
         reused=bool(req.get("_reused")),
         brief={"title": title, "sections": sections,
                "ok_sections": len(ok_sections), "total_sections": len(sections),
                "mode": mode, "focus": focus})
    return 0


async def run(req: dict) -> int:
    corpus_dir = Path(req["corpus_dir"])
    index_dir = Path(req.get("index_dir") or (corpus_dir.parent / "index"))
    index_dir.mkdir(parents=True, exist_ok=True)

    files = sorted(p for p in corpus_dir.rglob("*") if p.is_file())
    if not files:
        emit(type="error", message=f"语料目录是空的：{corpus_dir}")
        return 1

    llm = str(req.get("llm") or "")
    if not llm or "/" not in llm:
        emit(type="error",
             message=f"模型名必须是 litellm 形式（如 deepseek/deepseek-chat），收到 {llm!r}")
        return 1

    mode = str(req.get("action") or "ask")
    settings = build_settings(llm, str(req.get("embed") or DEFAULT_EMBED),
                              brief=(mode == "brief"))
    docs, reused = await get_docs(corpus_dir, index_dir, settings, bool(req.get("rebuild")))
    req["_reused"] = reused

    if not docs.docs:
        emit(type="error", message="PaperQA2 没有索引到任何文档（解析全部失败？）")
        return 1

    if mode == "ask":
        return await do_ask(req, docs, settings)
    if mode == "brief":
        return await do_brief(req, docs, settings)
    emit(type="error", message=f"不认识的 action：{mode!r}")
    return 2


def main() -> int:
    if len(sys.argv) < 2:
        emit(type="error", message="缺少请求参数")
        return 2
    try:
        req = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        emit(type="error", message=f"参数不是合法 JSON：{exc}")
        return 2
    try:
        return asyncio.run(run(req))
    except Exception as exc:  # noqa: BLE001 - 任何异常都要变成可读事件，不能裸崩
        import traceback

        emit(type="error", message=f"{type(exc).__name__}: {exc}",
             detail=traceback.format_exc()[-2000:])
        return 1


if __name__ == "__main__":
    sys.exit(main())
