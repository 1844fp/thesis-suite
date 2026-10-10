"""编排层：把七个能力串成一条可监督的流水线。

这一层回答的问题是：**"从一份 .docx 到一份改好的 .docx，中间发生什么，谁负责，
出错时停在哪。"**

设计取舍：

* **阶段可单独运行。** ``run(stages=["dedup"])`` 能只跑降 AIGC。这是实际使用中
  最需要的——作者通常只缺某一环，不想每次都从头跑。
* **每个阶段的结果都落盘。** 阶段之间通过黑板传递，不通过内存变量。所以一个跑到
  一半崩掉的流程可以接着跑。
* **阶段失败不等于整体失败。** 降 AIGC 服务没启动，不应该让已经完成的验证结果作废。
  失败的阶段被如实记录，流程继续，最后由 summary 汇总。
* **不自动做不可逆的事。** 写作、改写、格式写回都产成工件；导出由人触发。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .config import PROJECTS_DIR, Settings, ensure_dirs, load_settings
from .framework.agent import AgentContext, AgentReport
from .framework.artifact import Issue, Severity, Stage
from .framework.blackboard import Blackboard
from .framework.state import Pipeline
from .parsing.intake import Manuscript, read_manuscript
from .runtime import Runtime, build_runtime

#: 流水线的标准阶段顺序。
STAGE_ORDER: tuple[str, ...] = (
    Stage.GATHER.value,
    Stage.RETRIEVE.value,
    Stage.DRAFT.value,
    Stage.VERIFY.value,
    Stage.FIX.value,
    Stage.DEDUP.value,
    Stage.FORMAT.value,
    Stage.FINAL.value,
)

#: 每个阶段由哪些智能体承担，**按顺序执行**。``verify`` 由状态机展开成一组检查器，
#: 不在这里列出。
#:
#: 为什么 ``draft`` 是四个智能体而不是一个：写作本来就不是一步。
#: 规划产出大纲，起草按大纲逐章写，结论依据已写的正文汇总，最后把角标连续化并
#: 生成参考文献表。**这四个之间是硬依赖**，少跑一个就会出问题：
#:
#: * 没有 ``writer:plan`` → ``writer:draft`` 找不到 outline，直接失败。
#:   这个断点真实存在过：``draft`` 阶段原先只挂 ``writer:draft``，而全项目没有任何
#:   地方调用 ``writer:plan``——于是 README 里写的 ``run -s draft`` 必然报
#:   "没有找到写作大纲"，写作能力从命令行完全走不通，而单元测试各自直接调智能体，
#:   谁也没发现。**"每个部件都测过"不等于"它们被接起来了"**，所以这里把顺序
#:   写成一份显式的清单，并由 ``tools/test_thesis.py`` 端到端验证。
#: * 没有 ``writer:references`` → 稿子只有 ``[1] [2]`` 角标、没有文献表，
#:   而外部核验是靠解析文献表工作的，于是"引用是否真实存在"这条最致命的检查
#:   会被静默跳过。
STAGE_AGENTS: dict[str, tuple[str, ...]] = {
    Stage.GATHER.value: ("gather:literature",),
    Stage.RETRIEVE.value: ("rag:retrieve",),
    Stage.DRAFT.value: ("writer:plan", "writer:draft", "writer:conclude", "writer:references"),
    Stage.FIX.value: ("fixer",),
    Stage.DEDUP.value: ("dedup:reduce",),
    Stage.FORMAT.value: ("format:apply",),
}

#: 向后兼容的单智能体视图（取每个阶段的第一位）。
#: 新增代码请用 ``STAGE_AGENTS``——只取第一位会漏掉写作阶段的其余三步。
STAGE_AGENT: dict[str, str] = {k: v[0] for k, v in STAGE_AGENTS.items()}


@dataclass
class StageOutcome:
    stage: str
    ran: bool
    ok: bool = True
    skipped: bool = False
    reason: str = ""
    summary: str = ""
    seconds: float = 0.0
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "ran": self.ran,
            "ok": self.ok,
            "skipped": self.skipped,
            "reason": self.reason,
            "summary": self.summary,
            "seconds": round(self.seconds, 2),
            "data": self.data,
        }


@dataclass
class RunResult:
    project_id: str
    stages: list[StageOutcome] = field(default_factory=list)
    self_correction: dict = field(default_factory=dict)
    ledger: dict = field(default_factory=dict)
    severity: dict = field(default_factory=dict)
    seconds: float = 0.0
    dry_run: bool = False

    @property
    def failures(self) -> list[StageOutcome]:
        return [s for s in self.stages if s.ran and not s.ok]

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "dry_run": self.dry_run,
            "seconds": round(self.seconds, 2),
            "stages": [s.as_dict() for s in self.stages],
            "stage_failures": [s.stage for s in self.failures],
            "self_correction": self.self_correction,
            "ledger": self.ledger,
            "severity": self.severity,
        }


class Runner:
    """Drives one project through the pipeline."""

    def __init__(self, runtime: Runtime | None = None, *, settings: Settings | None = None) -> None:
        self.runtime = runtime or build_runtime(settings)
        self.settings = self.runtime.settings

    # -- 项目 -------------------------------------------------------------
    def create_project(
        self,
        *,
        title: str,
        text: str = "",
        source_path: str = "",
        manuscript: Manuscript | None = None,
        meta: dict | None = None,
    ) -> Blackboard:
        """Create a project from text or a parsed manuscript."""
        ensure_dirs()
        meta = dict(meta or {})
        if manuscript is not None:
            meta["source_path"] = manuscript.source_path
            meta["manuscript"] = manuscript.as_dict(with_text=False)
            if manuscript.format_facts:
                meta["format_facts"] = manuscript.format_facts
            if manuscript.warnings:
                meta["intake_warnings"] = list(manuscript.warnings)
            text = text or manuscript.text
            title = title or manuscript.title or "未命名项目"
        if source_path:
            meta["source_path"] = source_path

        board = Blackboard.create(PROJECTS_DIR, title=title, text=text, meta=meta)
        for w in meta.get("intake_warnings") or []:
            board.record(Stage.INTAKE.value, 0, "intake", "warning", w)
        board.save()
        return board

    def create_from_file(self, path: str | Path, *, title: str = "", meta: dict | None = None) -> Blackboard:
        man = read_manuscript(path)
        return self.create_project(title=title, manuscript=man, meta=meta)

    def load(self, project_id: str) -> Blackboard:
        return Blackboard.load(PROJECTS_DIR, project_id)

    # -- 阶段实现 ---------------------------------------------------------
    def _stage_dedup(self, ctx: AgentContext) -> AgentReport:
        """降 AIGC：调用既有工作台，不在这里重写改写管线。

        关键的安全设计：改写后的文本必须过对端的保真校验（数字、引用、引文、
        术语没被改坏）。不通过就**退回原文**——降 AIGC 绝不能以牺牲事实为代价。
        """
        from .adapters.base import PeerError
        from .adapters.reducer import ReducerAdapter

        meta = ctx.board.project.meta
        text = ctx.board.draft_text
        if not text.strip():
            return AgentReport(agent="dedup:reduce", stage=ctx.stage, ok=False,
                               summary="稿件为空，无内容可降 AIGC", error="empty draft")

        if not meta.get("dedup_enabled", True):
            return AgentReport(
                agent="dedup:reduce", stage=ctx.stage, ok=True,
                summary="已按要求跳过降 AIGC", data={"skipped": True},
            )

        adapter = ReducerAdapter(self.settings.peers.reducer_base, timeout=self.settings.peers.timeout)
        if not ctx.llm_configured:
            return AgentReport(
                agent="dedup:reduce", stage=ctx.stage, ok=False,
                summary="未配置模型，无法降 AIGC", error="LLM not configured",
            )

        try:
            ctx.say("正在调用降 AIGC 工作台…", 0.0)

            def on_event(ev: dict) -> None:
                if ev.get("type") == "round":
                    ctx.say(f"降 AIGC 第 {ev.get('round')} 轮", -1.0)

            result = adapter.reduce(
                text,
                on_event=on_event,
                genre=str(meta.get("dedup_genre") or ""),
                intensity=str(meta.get("dedup_intensity") or ""),
                target_score=meta.get("dedup_target_score"),
                max_rounds=meta.get("dedup_max_rounds"),
                fact_check=meta.get("dedup_fact_check"),
                keep_terms=list(meta.get("keep_terms") or []),
            )
        except PeerError as exc:
            return AgentReport(
                agent="dedup:reduce", stage=ctx.stage, ok=False,
                summary="降 AIGC 服务调用失败", error=str(exc),
            )

        new_text = result.text or ""
        if not new_text.strip():
            return AgentReport(
                agent="dedup:reduce", stage=ctx.stage, ok=False,
                summary="降 AIGC 返回空文本，已放弃本次改写", error="empty result",
            )

        # --- 保真护栏：改写不得改动事实 ---
        guard_ok = True
        guard_note = ""
        try:
            verdict = adapter.guard(text, new_text, keep_terms=list(meta.get("keep_terms") or []))
            guard_ok = bool(verdict.get("ok", True))
            if not guard_ok:
                guard_note = str(verdict.get("reason") or verdict.get("violations") or "")[:300]
        except PeerError as exc:
            # 护栏本身不可用时**不放行**：没有校验的自动改写风险太高。
            guard_ok = False
            guard_note = f"保真校验不可用，出于安全未采纳改写结果：{exc}"

        if not guard_ok:
            ctx.board.add_issues(
                [
                    Issue(
                        id="",
                        kind="dedup.fidelity_failed",
                        severity=Severity.MAJOR,
                        message="降 AIGC 的改写未通过保真校验，已退回原文",
                        location={"stage": "dedup"},
                        evidence="",
                        detected_by="rules:guard",
                        fixable=False,
                        fix_hint=f"改写可能改动了数字/引用/术语。{guard_note}",
                    )
                ],
                stage=ctx.stage,
                round_no=ctx.round,
            )
            ctx.board.record(ctx.stage, ctx.round, "dedup:reduce", "rejected", guard_note)
            ctx.board.save()
            return AgentReport(
                agent="dedup:reduce", stage=ctx.stage, ok=False,
                summary="改写未过保真校验，已退回原文（原始稿件未被改动）",
                error=guard_note,
                data={"index_before": result.index_before, "index_after": result.index_after},
            )

        art = ctx.board.put_artifact(
            kind="draft",
            text=new_text,
            created_by="dedup:reduce",
            stage=ctx.stage,
            run_id=ctx.run_id,
            note=f"降 AIGC：指数 {result.index_before:.1f} → {result.index_after:.1f}",
        )
        meta["dedup_result"] = result.as_dict()

        # 改写引入的事实性问题（对端的事实核查）要变成 Issue 给作者看。
        issues: list[Issue] = []
        for finding in result.fact_check or []:
            issues.append(
                Issue(
                    id="",
                    kind="dedup.fact_risk",
                    severity=Severity.MAJOR,
                    message=f"改写可能引入了原文没有的内容：{str(finding.get('claim'))[:120]}",
                    location={"paragraph": finding.get("index"), "find": str(finding.get("claim"))[:60]},
                    evidence=str(finding.get("claim"))[:200],
                    detected_by="llm:fact_check",
                    confidence=0.6,
                    fixable=False,
                    fix_hint=f"{finding.get('why') or '请人工核对这一处是否与原文一致'}",
                )
            )
        if issues:
            ctx.board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)

        ctx.board.save()
        return AgentReport(
            agent="dedup:reduce",
            stage=ctx.stage,
            ok=True,
            summary=(
                f"降 AIGC 完成：指数 {result.index_before:.1f} → {result.index_after:.1f}，"
                f"{result.rounds} 轮，事实风险 {len(issues)} 处"
            ),
            artifacts=[art.id],
            data=result.as_dict(),
        )

    def _stage_format(self, ctx: AgentContext) -> AgentReport:
        """格式整理：靠 ``format:apply`` 智能体（若已提供）。"""
        agent = self.runtime.registry.get("format:apply")
        return agent.run(ctx)

    # -- 运行 -------------------------------------------------------------
    def run(
        self,
        project_id: str,
        *,
        stages: Sequence[str] | None = None,
        max_rounds: int | None = None,
        include_llm_verify: bool = True,
        progress: Callable[[str, float], None] | None = None,
        dry_run: bool = False,
    ) -> RunResult:
        """Run the pipeline. ``stages`` selects a subset; order is always STAGE_ORDER."""
        t0 = time.time()
        board = self.load(project_id)
        wanted = tuple(stages) if stages else STAGE_ORDER
        unknown = [s for s in wanted if s not in STAGE_ORDER]
        if unknown:
            raise ValueError(f"未知阶段 {unknown}；可用：{list(STAGE_ORDER)}")

        ctx = self.runtime.new_context(board, progress=progress)
        pipeline = Pipeline(self.runtime.registry, ctx)
        result = RunResult(project_id=project_id, dry_run=dry_run)
        result.self_correction = {}

        for stage in STAGE_ORDER:
            if stage not in wanted:
                result.stages.append(StageOutcome(stage=stage, ran=False, skipped=True, reason="未选中"))
                continue

            st0 = time.time()
            ctx.stage = stage
            board.project.stage = stage

            if stage == Stage.VERIFY.value:
                reports = pipeline.verify(include_llm=include_llm_verify)
                ran = [r for r in reports]
                ok = all(r.ok for r in ran) if ran else True
                result.stages.append(
                    StageOutcome(
                        stage=stage,
                        ran=True,
                        ok=ok,
                        summary=f"运行 {len(ran)} 个检查器，发现 {len(board.open_issues())} 条待处理问题",
                        seconds=time.time() - st0,
                        data={"verifiers": [r.agent for r in ran]},
                    )
                )
                continue

            if stage == Stage.FIX.value:
                if dry_run:
                    result.stages.append(
                        StageOutcome(stage=stage, ran=False, skipped=True, reason="dry-run 不修改稿件")
                    )
                    continue
                summary = pipeline.self_correct(
                    max_rounds=max_rounds or self.settings.pipeline.max_rounds,
                    include_llm_verify=include_llm_verify,
                )
                result.self_correction = summary
                result.stages.append(
                    StageOutcome(
                        stage=stage,
                        ran=True,
                        ok=True,
                        summary=(
                            f"{summary['rounds']} 轮，收敛={summary['converged']}，"
                            f"仍有 {summary['remaining_open']} 条待处理"
                        ),
                        seconds=time.time() - st0,
                        data=summary,
                    )
                )
                continue

            # 其余阶段都对应一个具名智能体
            if stage == Stage.DEDUP.value:
                rep = pipeline._stage(stage, lambda: self._stage_dedup(ctx))
            elif stage == Stage.FORMAT.value:
                if "format:apply" not in [a.name for a in self.runtime.registry.all()]:
                    result.stages.append(
                        StageOutcome(
                            stage=stage,
                            ran=False,
                            skipped=True,
                            reason="格式整理智能体未提供（app.agents.format_apply 缺失）",
                        )
                    )
                    continue
                rep = pipeline._stage(stage, lambda: self._stage_format(ctx))
            elif stage in (Stage.GATHER.value, Stage.RETRIEVE.value, Stage.DRAFT.value):
                names = STAGE_AGENTS[stage]
                available = {a.name for a in self.runtime.registry.all()}
                runnable = [n for n in names if n in available]
                absent = [n for n in names if n not in available]
                if not runnable:
                    result.stages.append(
                        StageOutcome(
                            stage=stage,
                            ran=False,
                            skipped=True,
                            reason=f"智能体 {'、'.join(names)} 均未注册",
                        )
                    )
                    continue
                reports = [pipeline.run_agent(n, stage=stage) for n in runnable]
                # 阶段的成功判据是"每一步都成功"：规划失败而起草侥幸产出点东西，
                # 不能算这一阶段成功——那种"看起来有稿子"的假成功最难发现。
                ok = all(r.ok for r in reports)
                detail = "；".join(f"{r.agent}：{r.summary}" for r in reports)
                if absent:
                    detail += f"；未注册：{'、'.join(absent)}"
                result.stages.append(
                    StageOutcome(
                        stage=stage,
                        ran=True,
                        ok=ok,
                        summary=detail,
                        seconds=time.time() - st0,
                        data={
                            "agents": [r.agent for r in reports],
                            "absent": absent,
                            "errors": {r.agent: r.error for r in reports if r.error},
                        },
                    )
                )
                continue
            else:
                # FINAL 只做收尾标记，不产生新内容
                result.stages.append(
                    StageOutcome(stage=stage, ran=True, ok=True, summary="流程结束", seconds=0.0)
                )
                continue

            result.stages.append(
                StageOutcome(
                    stage=stage,
                    ran=True,
                    ok=rep.ok,
                    summary=rep.summary,
                    seconds=time.time() - st0,
                    data={"error": rep.error} if rep.error else {},
                )
            )

        board.project.stage = Stage.FINAL.value if Stage.FINAL.value in wanted else board.project.stage
        board.save()

        result.ledger = ctx.ledger.snapshot()
        result.severity = board.severity_counts()
        result.seconds = time.time() - t0
        board.record(Stage.FINAL.value, 0, "pipeline", "run_done",
                     f"{result.seconds:.1f}s, {len(result.failures)} 个阶段失败")
        board.save()
        return result


def run_stages(project_id: str, stages: Sequence[str], **kwargs: Any) -> RunResult:
    """Convenience wrapper used by the CLI and the API."""
    return Runner().run(project_id, stages=stages, **kwargs)
