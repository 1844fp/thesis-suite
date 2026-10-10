"""能力③「论文写作助手」——按作者的要求改写（writer:rewrite）。

## 它和相邻两个能力的分工

| 智能体 | 做什么 | 会不会改稿 |
|---|---|---|
| ``writer:draft`` | 从大纲写新章节 | 会（写的是新内容） |
| **``writer:rewrite``** | **按作者的一条具体要求，重写指定章节或指定段落** | **会** |
| ``writer:polish`` | 对片段给润色建议，作者自己决定采不采纳 | 不会 |

作者最常用的操作不是"从零生成整篇"，而是**"第 3 章写得太虚，帮我把机制分析补实"**
或 **"这一段没有依据，改写一下"**。没有这个能力，作者只能整章重跑——
既可能把已经满意的部分改坏，又费钱。

## 两条安全设计

1. **改写必须在"证据池编号空间"里做。** 稿子若已经定稿过，正文里的 ``[n]``
   是连续编号而不是池编号；直接改写会让 ``[n]`` 查到另一批证据，
   而外观上完全正常。所以开工前先 ``unfinalize_text`` 还原，收尾由
   ``writer:references`` 重新定稿（CLI 的 ``rewrite`` 命令会自动连着跑）。

2. **改写有边界。** 作者说"补强机制分析"，模型不许顺手把整章重写一遍；
   作者说"改写这一段"，模型不许改动别的段落。段落级改写时**整段替换**，
   上下文（前后段）只作为参考给它——这样"改了什么"在版本 diff 里一眼可见。

## 段落级改写为什么要按整段替换

如果允许"局部替换几句话"，改完之后的段落由新旧两种行文拼接而成，
语气与指代常常对不上，而这种问题在成稿里极难发现。整段替换虽然"改动更大"，
但**改动的边界清晰**：作者对比两版就能看出模型到底重写了什么。
"""

from __future__ import annotations

import re
import time
from typing import Any, Sequence

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage, new_id
from ..writing import BODY, INTRODUCTION, LITERATURE_REVIEW, blueprint_for, checkers_for
from ..writing.citations import EvidencePool
from ..writing.examples import fetch_examples, format_examples
from ..writing.relevance import extract_keywords, judge_entries
from .writer_common import (
    SearchFactory,
    clean_title,
    find_gaps,
    is_author_only,
    is_fatal_llm_error,
    unfinalize_text,
    validate_citations,
)

#: 一次改写最多处理多少个目标。改稿是**有人看着的精细活**，不是批量任务。
MAX_TARGETS = 6
#: 段落级改写时，向前后各取多少字作为上下文参考。
CONTEXT_CHARS = 600
#: 单次生成的 token 上限。
MAX_TOKENS = 7000

REWRITE_SYSTEM = (
    "你是中文学位论文的写作助手，正在按作者的具体要求修改他已有的稿件。"
    "你只改被指出的那一部分，不改动其它内容。"
    "你写的每一句实质性论断都必须来自给定的资料，并标注资料编号。"
    "你不替作者下结论，不写创新点，不评价研究价值。"
    "资料不足时你如实留白，不靠常识或印象补全。"
    "你保持原文的术语、变量名、数字口径与行文语气。"
)

SECTION_TMPL = """请按作者的要求改写下面这一章。

论文标题：{topic}
本章标题：{section}
**作者的要求（这是你这次改写的唯一目标）：**
{instruction}

本章现有正文：
---
{original}
---

可用的资料（**这是你唯一可以依据的材料**）：
{evidence}
{examples}
改写要求：
1. **只改作者要求的那件事**。作者没提到的部分，尽量保留原有的表述与结构；
   如果为了行文连贯必须调整，也要保持含义不变。
2. 只使用上面列出的资料。超出这些资料的内容一律不许写。
3. 每写一句事实性论断紧跟角标，编号取自上面对应的资料编号，例如 `……[1]`。
   角标不加圆括号，不写参考文献列表。
4. 需要数据、案例、政策原文而现在没有的地方，就地写
   `【待作者补充：具体说明需要什么】`，不要用常识或记忆填上。
5. 不要写研究结论、创新点，不要评价本文价值。
6. 保留原文已有的角标（除非作者要求删改）；不要引入上面列表以外的编号。
7. 章节内的 `## ` 小节标题沿用原文，不要新增或删改。

直接输出改写后的本章正文（Markdown，不要解释、不要 JSON）。
"""

PASSAGE_TMPL = """请按作者的要求改写下面这一段。

论文标题：{topic}
所在章节：{section}
**作者的要求（这是你这次改写的唯一目标）：**
{instruction}

**要改写的段落：**
---
{original}
---

前后文（仅供你保持衔接，**不要改写它们**）：
---
{before}
[这里是要改写的段落]
{after}
---

可用的资料（**这是你唯一可以依据的材料**）：
{evidence}
改写要求：
1. 只输出**改写后的那一段**，不要输出前后文，不要输出章节标题，不要加解释。
2. 段落长度与原文相当（可适度增减），保持它在全章中的论证位置不变。
3. 只使用上面列出的资料；超出这些资料的内容一律不许写。
4. 每写一句事实性论断紧跟角标，编号取自上面对应的资料编号，例如 `……[1]`。
   角标不加圆括号。需要数据而现在没有的地方写 `【待作者补充：…】`。
5. 保留原文中仍有依据的角标；不要引入列表以外的编号。
6. 不要写研究结论、创新点，不要评价本文价值。

直接输出改写后的那一段。
"""


def _split_paragraphs(text: str) -> list[str]:
    """按空行/换行切段（与文本框一致：单换行也算分段）。"""
    t = re.sub(r"[ \t]*\r?\n[ \t]*", "\n", text or "")
    return [p.strip() for p in t.split("\n") if p.strip()]


def _sections(doc: str) -> list[tuple[str, str, int, int]]:
    """切成 ``[(标题, 正文, 起, 止)]``（按 ``^# ``）。"""
    heads = list(re.finditer(r"^#[ \t]+(.+?)[ \t]*$", doc or "", re.M))
    out: list[tuple[str, str, int, int]] = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(doc)
        out.append((m.group(1).strip(), doc[m.end() : end].strip(), m.start(), end))
    return out


def _resolve_section(doc: str, needle: str) -> tuple[str, str, int, int] | None:
    """按标题片段定位章节。唯一匹配才返回；多义时返回 None（由调用方报错）。"""
    n = clean_title(needle)
    if not n:
        return None
    hits = [s for s in _sections(doc) if n in clean_title(s[0]) or clean_title(s[0]) in n]
    return hits[0] if len(hits) == 1 else None


def _resolve_passage(doc: str, needle: str) -> tuple[str, str, int, int, str] | None:
    """定位包含 ``needle`` 的段落，返回 ``(章节, 段落, 段起, 段止, 正文)``。

    在**章节正文内**按段落找，而不是全文查找：同一句话可能在结论里被复述，
    按全文查找会改错地方。
    """
    n = (needle or "").strip()
    if not n:
        return None
    for title, body, s_start, _s_end in _sections(doc):
        pos = 0
        for para in _split_paragraphs(body):
            idx = body.find(para, pos)
            if idx < 0:
                continue
            pos = idx + len(para)
            if n in para:
                return title, para, s_start + idx, s_start + idx + len(para), body
    return None


class WriterRewriteAgent(Agent):
    """按作者的一条具体要求改写指定章节或段落，改写后产生新版本。"""

    name = "writer:rewrite"
    description = (
        "按作者给出的具体要求改写指定章节或指定段落：只依据证据说话、"
        "改完产生新版本、并如实报告改了什么"
    )
    stage = Stage.DRAFT.value
    capabilities = ("write", "rewrite")
    uses_model = True

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        board = ctx.board
        meta = board.project.meta
        targets = meta.get("rewrite_targets") or []
        if isinstance(targets, dict):
            targets = [targets]
        targets = [t for t in targets if isinstance(t, dict)][:MAX_TARGETS]
        if not targets:
            return self.report(
                ctx,
                ok=False,
                summary="没有要改写的内容：请在项目 meta 的 rewrite_targets 里给出"
                        "「章节标题或原文片段」与「改写要求」",
                error="rewrite_targets 为空",
                data={
                    "hint": "rewrite_targets: "
                            "[{section: '第3章', instruction: '把机制分析补实'}] 或 "
                            "[{find: '原文里的一句话', instruction: '改成…'}]"
                },
            )

        doc = board.draft_text or ""
        if not doc.strip():
            return self.report(
                ctx, ok=False,
                summary="稿件为空，没有可改写的内容",
                error="draft 为空",
            )

        # -- 还原编号空间（改写必须在池编号空间里做）--------------------
        doc, restored = unfinalize_text(doc, meta)
        if restored:
            ctx.say(f"♻ 已还原 {restored} 处角标编号到证据池空间（改完后需重新定稿）", 0.1)

        brief = meta.get("writing_brief") if isinstance(meta.get("writing_brief"), dict) else {}
        topic = str(brief.get("topic") or board.project.title or "未命名论文")
        keywords = extract_keywords(topic, str(brief.get("requirement") or ""))

        pool = EvidencePool.load(meta)
        searcher = SearchFactory(
            ctx.settings.peers.rag_base,
            timeout=min(120.0, float(getattr(ctx.settings.peers, "timeout", 120.0) or 120.0)),
        )
        doc_index = searcher.doc_index() if hasattr(searcher, "doc_index") else {}
        if doc_index:
            pool.enrich(doc_index)

        example_blocks: dict[str, str] = {}
        for bp in (INTRODUCTION, LITERATURE_REVIEW, BODY):
            exs, note = fetch_examples(
                ctx.settings.peers.rag_base, bp.example_queries, max_examples=2,
                timeout=min(60.0, float(getattr(ctx.settings.peers, "timeout", 60.0) or 60.0)),
            )
            example_blocks[bp.kind] = format_examples(exs)

        issues: list[Issue] = []
        done: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        current = doc
        fatal = ""

        for i, t in enumerate(targets):
            if fatal:
                break
            instruction = str(t.get("instruction") or t.get("requirement") or "").strip()
            if not instruction:
                failed.append({"target": t, "reason": "缺少 instruction（改写要求）"})
                continue
            sec_needle = str(t.get("section") or t.get("chapter") or "").strip()
            find = str(t.get("find") or t.get("original") or "").strip()

            # ---- 定位 ----------------------------------------------------
            section_title = ""
            original = ""
            span: tuple[int, int] | None = None
            mode = ""
            if sec_needle:
                loc = _resolve_section(current, sec_needle)
                if loc is None:
                    failed.append({"target": sec_needle, "reason": f"定位不到章节「{sec_needle}」（标题需唯一匹配）"})
                    issues.append(self._miss_issue(sec_needle, "section"))
                    continue
                section_title, original, s_start, s_end = loc
                span = (s_start, s_end)
                mode = "section"
            elif find:
                loc2 = _resolve_passage(current, find)
                if loc2 is None:
                    failed.append({"target": find[:40], "reason": "在稿件里找不到这段原文（需逐字匹配）"})
                    issues.append(self._miss_issue(find[:40], "passage"))
                    continue
                section_title, original, p_start, p_end, body = loc2
                span = (p_start, p_end)
                mode = "passage"
            else:
                failed.append({"target": t, "reason": "既没给 section 也没给 find，无法定位"})
                continue

            if is_author_only(section_title) and mode == "section":
                failed.append({"target": section_title, "reason": "结论/创新点/致谢等章节不由工具改写"})
                continue

            ctx.say(f"✏ 改写「{section_title}」（{'整章' if mode == 'section' else '单段'}）", 0.2 + 0.6 * i / max(1, len(targets)))

            # ---- 检索证据（带相关性闸门）--------------------------------
            points = [clean_title(instruction)[:60]]
            queries = [f"{topic} {section_title}"] + [f"{topic} {p}" for p in points]
            entries: list[Any] = []
            search_errors: list[str] = []
            for q in queries:
                hits, err = searcher.search(q, k=6)
                if err:
                    search_errors.append(err)
                    continue
                added, _ = pool.add_hits(hits)
                for e in added:
                    if e.index not in {x.index for x in entries}:
                        entries.append(e)
            relevance = judge_entries(entries, keywords)
            usable = list(relevance.relevant) if entries else []
            if entries and not relevance.usable:
                failed.append(
                    {
                        "target": section_title,
                        "reason": f"知识库里没有与本题相关的证据（{len(relevance.relevant)}/{relevance.total} 相关），"
                                  "无法改写",
                    }
                )
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.irrelevant_evidence",
                        severity=Severity.BLOCKER,
                        message=f"改写「{section_title}」失败：知识库里没有与本题相关的证据",
                        location={"section": section_title},
                        evidence=f"命中词：{'、'.join(relevance.matched_terms[:8]) or '（无）'}",
                        detected_by="rules:writer_rewrite",
                        confidence=1.0,
                        fixable=False,
                        fix_hint="先把本主题的文献投进知识库（`kb add`），再重跑改写。",
                        status="needs_author",
                    )
                )
                continue

            from .writer_common import format_evidence

            evidence_block = format_evidence([e.as_dict() for e in usable])
            brief = meta.get("writing_brief") if isinstance(meta.get("writing_brief"), dict) else {}
            bp = blueprint_for(section_title, str((brief or {}).get("genre") or ""))
            if mode == "passage":
                p_idx = current.find(original)
                before = current[max(0, p_idx - CONTEXT_CHARS) : p_idx].strip()[-CONTEXT_CHARS:]
                after = current[p_idx + len(original) : p_idx + len(original) + CONTEXT_CHARS].strip()
                prompt = PASSAGE_TMPL.format(
                    topic=topic, section=section_title, instruction=instruction,
                    original=original, before=before or "（本章开头）", after=after or "（本章结尾）",
                    evidence=evidence_block,
                )
                purpose = f"writer:rewrite:{section_title}#p{i}"
                maxtok = MAX_TOKENS // 4
            else:
                prompt = SECTION_TMPL.format(
                    topic=topic, section=section_title, instruction=instruction,
                    original=original[:8000], evidence=evidence_block,
                    examples=(f"\n{example_blocks.get(bp.kind, '')}\n" if example_blocks.get(bp.kind) else ""),
                )
                purpose = f"writer:rewrite:{section_title}"
                maxtok = MAX_TOKENS

            try:
                raw = ctx.ask_model(
                    purpose,
                    [{"role": "system", "content": REWRITE_SYSTEM},
                     {"role": "user", "content": prompt}],
                    temperature=0.35,
                    max_tokens=maxtok,
                )
            except Exception as exc:  # noqa: BLE001 - 单个目标失败不该毁掉整次改写
                reason = f"{type(exc).__name__}: {exc}"
                failed.append({"target": section_title, "reason": reason})
                if is_fatal_llm_error(exc):
                    fatal = reason
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.llm_unavailable" if is_fatal_llm_error(exc) else "writing.rewrite_failed",
                        severity=Severity.MAJOR,
                        message=f"改写「{section_title}」失败：{reason}",
                        location={"section": section_title},
                        evidence=reason,
                        detected_by="rules:writer_rewrite",
                        confidence=1.0,
                        fixable=False,
                        fix_hint="检查模型配置与额度后重跑；稿件未被改动。",
                        status="needs_author",
                    )
                )
                continue

            new_text = (raw or "").strip()
            if not new_text:
                failed.append({"target": section_title, "reason": "模型返回空内容"})
                continue

            # ---- 角标校验（改写同样不许引入没有的依据）------------------
            check = validate_citations(new_text, [e.index for e in usable])
            new_text = str(check["text"]).strip()
            for item in check["removed_citations"]:
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.fabricated_citation",
                        severity=Severity.MAJOR,
                        message=f"改写「{section_title}」时模型使用了不存在的引用编号，已删除",
                        location={"section": section_title},
                        evidence=str(item.get("context") or "")[:160],
                        detected_by="rules:writer_rewrite",
                        confidence=1.0,
                        fixable=False,
                        fix_hint="这些位置现在没有引用，请核对是否确有依据。",
                        status="needs_author",
                    )
                )
            for g in find_gaps(new_text):
                issues.append(
                    Issue(
                        id=new_id("iss"),
                        kind="writing.needs_source",
                        severity=Severity.MINOR,
                        message=f"改写后的「{section_title}」有一处需要你补依据：{g[:80]}",
                        location={"section": section_title},
                        evidence=g[:200],
                        detected_by="rules:writer_rewrite",
                        confidence=0.9,
                        fixable=False,
                        fix_hint="补上对应数据/文献，或改写该句——不要保留没有依据的断言。",
                        status="needs_author",
                    )
                )

            # ---- 替换 ----------------------------------------------------
            start, end = span  # type: ignore[misc]
            before_len = len(re.sub(r"\s", "", original))
            after_len = len(re.sub(r"\s", "", new_text))
            current = (current[:start] + new_text + current[end:]).strip() + "\n"

            done.append(
                {
                    "mode": mode,
                    "section": section_title,
                    "instruction": instruction[:120],
                    "chars_before": before_len,
                    "chars_after": after_len,
                    "citations_kept": int(check["kept"]),
                    "citations_removed": int(check["removed"]),
                }
            )
            board.record(
                ctx.stage, ctx.round, self.name, "rewrite",
                f"{section_title}（{'整章' if mode == 'section' else '单段'}）{before_len}→{after_len} 字",
            )

        # ---- 落盘：改写产生新版本（可回退对照）------------------------
        if done and current != doc:
            art = board.put_artifact(
                kind="draft",
                text=current,
                created_by=self.name,
                stage=ctx.stage,
                run_id=ctx.run_id,
                note=(
                    f"按作者要求改写 {len(done)} 处："
                    + "；".join(f"{d['section']}（{d['chars_before']}→{d['chars_after']} 字）" for d in done[:4])
                ),
            )
            ctx.say(f"✅ 已改写 {len(done)} 处，稿件 v{art.version}", 0.85)
        pool.save(meta)
        added = board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)
        board.save()

        ok = bool(done) and not fatal
        parts = [f"已改写 {len(done)} 处"] if done else ["没有改写任何内容"]
        if failed:
            parts.append(f"失败 {len(failed)} 处（{'；'.join(str(f.get('reason'))[:60] for f in failed[:2])}）")
        if fatal:
            parts.append(f"⚠ 模型服务不可用已熔断：{fatal[:80]}")
        if done:
            parts.append("提示：角标已回到证据池空间，请接着跑 writer:references 重新定稿")
        return self.report(
            ctx,
            ok=ok,
            summary="；".join(parts),
            error="" if ok else (fatal or "没有任何目标改写成功"),
            issues=[i.id for i in issues],
            data={
                "rewritten": done,
                "failed": failed,
                "llm_fatal": fatal,
                "needs_refinalize": bool(done),
                "new_issues": added,
            },
            seconds=time.time() - t0,
        )

    def _miss_issue(self, needle: str, kind: str) -> Issue:
        what = "章节" if kind == "section" else "原文片段"
        return Issue(
            id=new_id("iss"),
            kind="writing.rewrite_target_missing",
            severity=Severity.MINOR,
            message=f"改写目标定位失败：稿件里找不到唯一的{what}「{needle}」",
            location={"find": needle[:40]},
            evidence=needle[:160],
            detected_by="rules:writer_rewrite",
            confidence=1.0,
            fixable=False,
            fix_hint=(
                "章节按标题片段匹配，要求唯一命中；段落按原文逐字匹配，"
                "请从稿件里直接复制一段原文。"
            ),
            status="needs_author",
        )
