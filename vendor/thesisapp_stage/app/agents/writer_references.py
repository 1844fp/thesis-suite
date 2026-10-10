"""能力③「论文写作助手」——参考文献表生成（writer:references）。

## 与 ``references:build`` 的分工

两个智能体都产出 `references`，但做的是相反方向的事，容易混淆，所以写在这里：

* **本模块（生成）**：稿件里只有 ``[1] [2]`` 这类角标，**还没有文献表**。
  它把全篇角标连续化，再按证据池里记录的来源造出文献表。
* ``references:build``（解析）：稿件里**已经有**作者写的文献表，
  它把那张表切条目、抽字段、并带上外部核验结论，供作者核对。

顺序上本模块先跑（生成的稿子此时才有文献表），``references:build`` 随后才有东西可解析。

## 为什么"连续化重排"必须做，而且必须显式做

起草时编号来自项目级证据池，是**只增不改**的全篇唯一编号。这保证了 ``[7]`` 在任何
时刻都指向同一条证据。但写作过程中"检索到了却没用上"的证据会留下空号，
所以正文里的编号会稀疏、且不按出现顺序排列。

定稿前按**角标在正文中首次出现的顺序**重排成连续编号，一举解决两件事：
文献表顺序与阅读顺序一致，编号没有空洞。

这一步是纯文本变换（``citations.renumber``），不调用模型、不改写任何句子，
而且产出映射表——**可以复验**：改动处数、映射关系、哪些证据被用到、
哪些检索到了却没用上，全部逐条记账。这正是本项目对"机械可修的问题"的一贯处理方式。
"""

from __future__ import annotations

import time

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage
from ..writing.citations import EvidencePool, build_references, invert_mapping, renumber
from .writer_common import SearchFactory

__all__ = ["WriterReferencesAgent"]


class WriterReferencesAgent(Agent):
    """把正文角标连续化并生成参考文献表，附在稿件末尾。"""

    name = "writer:references"
    description = (
        "把全篇引用角标按首次出现顺序重排为连续编号，并依据证据池生成参考文献表；"
        "只列真正被引用的条目，检索到但未引用的单独报给作者"
    )
    stage = Stage.DRAFT.value
    capabilities = ("write", "references")
    uses_model = False

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        board = ctx.board
        text = board.draft_text or ""
        if not text.strip():
            return self.report(
                ctx,
                ok=False,
                summary="稿件为空，无法生成参考文献表",
                error="draft 为空",
                data={"hint": "先跑 writer:draft 写出正文"},
            )

        pool = EvidencePool.load(board.project.meta)
        if not len(pool):
            # 没有证据池 = 没有任何带来源的论断。这时**不生成空文献表**：
            # 一份空的"参考文献"章节会被误认为"这篇论文不需要引用"，
            # 而真实情况是"这一稿里没有一条可追溯的引用"。
            return self.report(
                ctx,
                ok=False,
                summary="证据池为空：稿件里没有任何可追溯的引用，未生成参考文献表",
                error="evidence_pool 为空",
                data={"hint": "确认 RAG 服务可用后重跑 writer:draft；起草时会把检索到的证据登记进证据池"},
            )

        # 生成文献表前**自己补齐作者/题名/年份**，不假设起草阶段已经做过。
        #
        # 为什么放在这里：这几个字段的唯一消费者就是文献表。依赖上游做过，
        # 就会出现"上游漏了一步，而下游的 49 条文献全部显示缺字段"这种连锁失效
        # ——实测踩过，而且当场看不出来（缺字段的标记长得像"这条文献元数据本来就没有"）。
        # 补元数据是纯查询、不花钱，放在消费端才是稳的。
        searcher = SearchFactory(
            ctx.settings.peers.rag_base,
            timeout=min(60.0, float(getattr(ctx.settings.peers, "timeout", 60.0) or 60.0)),
        )
        doc_index = searcher.doc_index() if hasattr(searcher, "doc_index") else {}
        filled = pool.enrich(doc_index) if doc_index else 0
        if filled:
            ctx.say(f"ℹ 已为证据补齐 {filled} 个书目字段（来自知识库元数据）", 0.2)

        # -- 幂等：上一次定稿过的稿子必须先还原回池编号空间 -----------------
        #
        # 本智能体把角标从**证据池编号**改写成连续编号。如果直接再跑一次，
        # 它会把正文里那些"已经改写过的编号"当成池编号去查表——查到的是**另一批证据**。
        # 实测踩过：第二次运行后文献表前五条变成了同一篇论文的五个片段，而正文看着正常。
        #
        # 所以先按上次落盘的映射做逆变换还原。映射表就在 meta 里，可核对。
        prev = board.project.meta.get("references_final") or {}
        if isinstance(prev, dict) and prev.get("mapping"):
            inverted = invert_mapping(prev["mapping"])
            if inverted:
                restored, changed = renumber(text, inverted)
                if changed:
                    text = restored
                    ctx.say(f"♻ 已还原上次定稿的 {changed} 处角标编号（幂等重跑）", 0.25)
                    board.record(
                        ctx.stage, ctx.round, self.name, "unfinalize",
                        f"还原 {changed} 处角标编号（上次定稿映射 {len(inverted)} 条）",
                    )
            board.project.meta.pop("references_final", None)

        result = build_references(text, pool)
        if result.uncited:
            ctx.say(f"ℹ {len(result.uncited)} 条证据检索到但未被引用，不进文献表", 0.6)

        art = board.put_artifact(
            kind="draft",
            text=result.text,
            created_by=self.name,
            stage=ctx.stage,
            run_id=ctx.run_id,
            note=(
                f"参考文献表：{len(result.cited)} 条被引用；角标重排 {result.renumbered} 处；"
                f"{len(result.uncited)} 条证据未被引用（未列入）"
            ),
        )

        issues: list[Issue] = []
        if result.missing_fields:
            issues.append(
                Issue(
                    id="",
                    kind="references.missing_fields",
                    severity=Severity.MINOR,
                    message=(
                        f"有 {len(result.missing_fields)} 条文献缺少字段（作者/题名/年份），"
                        "已逐条标出但**未替你补全**：补全等于伪造书目信息"
                    ),
                    location={"section": "参考文献"},
                    evidence="；".join(str(m.get("reason") or "") for m in result.missing_fields[:5]),
                    detected_by="rules:writer_references",
                    confidence=1.0,
                    fixable=False,
                    fix_hint=(
                        "请对照原文补全：期刊名、卷期页码、文献类型标识（[J]/[M]/[D]）。"
                        "RAG 知识库里没有这些字段，所以系统只能留空。"
                    ),
                    status="needs_author",
                )
            )
        if result.uncited:
            issues.append(
                Issue(
                    id="",
                    kind="references.uncited_evidence",
                    severity=Severity.INFO,
                    message=(
                        f"有 {len(result.uncited)} 条证据检索到了但正文一次也没引用，"
                        "已排除在文献表之外（避免出现「表里有、正文没引」的硬伤）"
                    ),
                    location={"section": "参考文献"},
                    evidence="；".join((e.citation or e.title or str(e.index))[:40] for e in result.uncited[:5]),
                    detected_by="rules:writer_references",
                    confidence=1.0,
                    fixable=False,
                    fix_hint="若这些材料本该用上，请回到相应章节补充引用；否则可以忽略这条提示。",
                    status="open",
                )
            )
        added = board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)
        board.project.meta["references_final"] = result.as_dict()
        board.record(
            ctx.stage,
            ctx.round,
            self.name,
            "references",
            f"文献表 {len(result.cited)} 条；重排 {result.renumbered} 处；未引用 {len(result.uncited)} 条",
        )
        board.save()

        return self.report(
            ctx,
            ok=True,
            summary=(
                f"参考文献表已生成：{len(result.cited)} 篇被引用文献，"
                f"角标重排 {result.renumbered} 处"
                + (f"，同一文献的多条证据折叠 {result.collapsed} 处" if result.collapsed else "")
                + (f"，去掉多余括号 {result.unwrapped} 处" if result.unwrapped else "")
                + (f"；{len(result.uncited)} 条证据未被引用（未列入）" if result.uncited else "")
                + (f"；{len(result.missing_fields)} 条缺字段待你补全" if result.missing_fields else "")
            ),
            artifacts=[art.id],
            issues=[i.id for i in issues],
            data={
                "cited": len(result.cited),
                "uncited": len(result.uncited),
                "renumbered": result.renumbered,
                "unwrapped": result.unwrapped,
                "collapsed": result.collapsed,
                "missing_fields": result.missing_fields,
                "mapping": {str(k): v for k, v in sorted(result.mapping.items())},
                "new_issues": added,
            },
            seconds=time.time() - t0,
        )
