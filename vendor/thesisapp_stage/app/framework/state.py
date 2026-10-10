"""状态机与自我更正循环。

**这是整个系统唯一值得写这么长的地方。** 自我更正的价值不在"循环"本身，而在
这个循环的三条纪律：

1. **检查器和修复器分离**。修复器不能宣布自己成功，只有检查器能。
2. **修复必须可复验**。问题条目里的 ``evidence`` 是复验的锚点：改完之后如果
   这段原文还在，问题就还是 open，不管修复器怎么说。
3. **不收敛就如实上报**。到轮次上限仍有 blocker/major，返回 ``converged=False``
   并说明卡在哪一类问题上。假装成功比失败更糟——作者会以为稿子没问题了。

另外，**需要作者判断的问题（``fixable=False``）永不自动改写**，只会转成
``needs_author`` 交还给人。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from .agent import AgentContext, AgentRegistry, AgentReport
from .artifact import Issue, Patch, Severity, Stage, new_id, severity_from
from .ops import apply_op

#: 低于这个置信度的模型判断问题不进入自动修复队列（只提示）。
AUTO_FIX_MIN_CONFIDENCE = 0.7


@dataclass
class StageResult:
    stage: str
    ok: bool
    summary: str = ""
    seconds: float = 0.0
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "stage": self.stage,
            "ok": self.ok,
            "summary": self.summary,
            "seconds": round(self.seconds, 2),
            "data": self.data,
        }


@dataclass
class FixRound:
    round: int
    opened: int
    fixed: int
    deferred: int
    patches_applied: int
    patches_failed: int
    census: dict[str, int] = field(default_factory=dict)
    detail: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "round": self.round,
            "opened": self.opened,
            "fixed": self.fixed,
            "deferred": self.deferred,
            "patches_applied": self.patches_applied,
            "patches_failed": self.patches_failed,
            "census": self.census,
            "detail": self.detail,
        }


class Pipeline:
    """Drives agents and the verify→fix loop."""

    def __init__(self, registry: AgentRegistry, ctx: AgentContext) -> None:
        self.registry = registry
        self.ctx = ctx
        self.stages: list[StageResult] = []
        self.rounds: list[FixRound] = []

    # -- helpers ----------------------------------------------------------
    def _stage(self, name: str, fn: Callable[[], AgentReport], ratio: float = -1.0) -> AgentReport:
        self.ctx.stage = name
        self.ctx.say(f"▶ {name}", ratio)
        t0 = time.time()
        try:
            rep = fn()
        except Exception as exc:  # noqa: BLE001 - report, do not crash the whole run
            rep = AgentReport(
                agent=name,
                stage=name,
                ok=False,
                summary=f"{name} 失败",
                error=f"{type(exc).__name__}: {exc}",
                seconds=time.time() - t0,
            )
        self.stages.append(
            StageResult(stage=name, ok=rep.ok, summary=rep.summary, seconds=time.time() - t0, data=rep.data)
        )
        self.ctx.board.record(name, self.ctx.round, rep.agent, "stage", rep.summary)
        self.ctx.board.save()
        self.ctx.bus.publish(
            "proposal" if rep.ok else "error",
            sender=rep.agent,
            payload=rep.as_dict(),
            stage=name,
            round=self.ctx.round,
        )
        return rep

    def run_agent(self, name: str, *, stage: str | None = None, ratio: float = -1.0) -> AgentReport:
        agent = self.registry.get(name)
        target = stage or agent.stage
        return self._stage(target, lambda: agent.run(self.ctx), ratio)

    # -- 验证 -------------------------------------------------------------
    def verify(
        self,
        *,
        include_llm: bool = True,
        only: Iterable[str] | None = None,
        stop_on_blocker: bool = True,
    ) -> list[AgentReport]:
        """Run every applicable verifier.

        Free (local) verifiers run first. ``stop_on_blocker`` short-circuits the
        paid ones when a blocker was already found for free — no reason to pay
        for an LLM review of a document that cites a nonexistent reference.
        """
        wanted = set(only) if only else None
        names = [
            a.name
            for a in self.registry.all()
            if a.capabilities and "verify" in a.capabilities
        ]
        if wanted is not None:
            names = [n for n in names if n.split(":", 1)[1] in wanted or n in wanted]

        def local_first(n: str) -> tuple[int, str]:
            return (1 if self.registry.get(n).uses_model else 0, n)

        reports: list[AgentReport] = []
        for name in sorted(names, key=local_first):
            agent = self.registry.get(name)
            if agent.uses_model and not include_llm:
                continue
            if agent.uses_model and stop_on_blocker and self._has_blocker():
                self.ctx.say(f"⏭ {name}：已有致命问题，跳过付费检查")
                continue
            rep = self._stage(f"verify:{agent.name.split(':', 1)[1]}", lambda a=agent: a.run(self.ctx))
            reports.append(rep)
        return reports

    def _has_blocker(self) -> bool:
        return any(
            i.status == "open" and i.severity is Severity.BLOCKER
            for i in self.ctx.board.project.issues.values()
        )

    # -- 修复 -------------------------------------------------------------
    def fix_once(self, *, round_no: int, fixer_name: str = "fixer") -> FixRound:
        """One verify→fix→re-verify round."""
        board = self.ctx.board
        self.ctx.round = round_no
        self.ctx.stage = Stage.FIX.value

        open_issues = board.open_issues()
        detail: list[dict] = []
        patches_applied = 0
        patches_failed = 0
        fixed_count = 0
        deferred = 0

        candidates: list[Issue] = []
        for issue in open_issues:
            if not issue.fixable:
                # Author-judgement territory. Hand it back, never auto-rewrite.
                # INFO 级例外：那是"我们跳过了某项检查"这类提示，不需要作者行动。
                if issue.severity is not Severity.INFO:
                    issue.status = "needs_author"
                    deferred += 1
                    detail.append(
                        {"issue": issue.id, "kind": issue.kind, "action": "needs_author", "hint": issue.fix_hint}
                    )
                continue
            if issue.confidence < AUTO_FIX_MIN_CONFIDENCE:
                deferred += 1
                detail.append(
                    {
                        "issue": issue.id,
                        "kind": issue.kind,
                        "action": "low_confidence",
                        "confidence": issue.confidence,
                    }
                )
                continue
            if not issue.fix_op:
                deferred += 1
                detail.append({"issue": issue.id, "kind": issue.kind, "action": "no_op"})
                continue
            candidates.append(issue)

        board.save()

        if candidates:
            fixer = self.registry.get(fixer_name)
            self.ctx.say(f"🔧 第 {round_no} 轮：待修复 {len(candidates)} 条", 0.0)
            report = fixer.run(self.ctx)
            self.stages.append(
                StageResult(
                    stage=f"fix:round{round_no}",
                    ok=report.ok,
                    summary=report.summary,
                    data=report.data,
                )
            )
            patches: list[Patch] = report.data.get("patches") or []
            for patch in patches:
                ok = self._apply_patch(patch)
                if ok:
                    patches_applied += 1
                else:
                    patches_failed += 1
                detail.append(
                    {
                        "issue": ",".join(patch.issue_ids),
                        "action": "patch_applied" if ok else "patch_failed",
                        "error": patch.error,
                    }
                )

        # --- 复验：唯一的"改好了"判据 ---
        self.ctx.say(f"🔁 第 {round_no} 轮复验", 0.0)
        text = board.draft_text
        census = board.refresh_issue_status(text)
        # Re-run only the free verifiers: if a defect class still exists in the
        # new text, it must be re-detected rather than assumed fixed.
        pre_existing = set(board.project.issues.keys())
        for name in [
            a.name
            for a in self.registry.all()
            if a.capabilities and "verify" in a.capabilities and not a.uses_model
        ]:
            try:
                self.registry.get(name).run(self.ctx)
            except Exception as exc:  # noqa: BLE001
                self.ctx.bus.publish(
                    "error",
                    sender=name,
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                    stage=Stage.FIX.value,
                    round=round_no,
                )
        newly = [k for k in board.project.issues if k not in pre_existing]
        board.save()

        fixed_count = sum(1 for i in board.project.issues.values() if i.status == "fixed")
        round_rec = FixRound(
            round=round_no,
            opened=len(open_issues),
            fixed=fixed_count,
            deferred=deferred,
            patches_applied=patches_applied,
            patches_failed=patches_failed,
            census={**census, "newly_detected": len(newly)},
            detail=detail,
        )
        self.rounds.append(round_rec)
        self.ctx.bus.publish(
            "review",
            sender="pipeline",
            payload=round_rec.as_dict(),
            stage=Stage.FIX.value,
            round=round_no,
        )
        return round_rec

    def _apply_patch(self, patch: Patch) -> bool:
        """Apply a patch's ops in order, all-or-nothing.

        If op 3 of 5 fails, the first two must not be kept: a half-applied patch
        leaves the document in a state no verifier was run against.
        """
        board = self.ctx.board
        original = board.draft_text
        cur = original
        results: list[dict] = []

        for idx, op in enumerate(patch.ops):
            res = apply_op(cur, op)
            results.append(
                {
                    "op": op.get("op") or op.get("name"),
                    "ok": res.ok,
                    "changed": res.changed,
                    "detail": res.detail,
                    "error": res.error,
                }
            )
            if not res.ok:
                patch.applied = False
                patch.failed_op_index = idx
                patch.error = f"第 {idx+1} 个算子失败：{res.error}"
                board.add_patch(patch)
                board.record(
                    Stage.FIX.value,
                    self.ctx.round,
                    patch.created_by or "fixer",
                    "patch_failed",
                    patch.error,
                )
                return False
            cur = res.text

        if cur == original:
            patch.applied = False
            patch.error = "算子执行后文本没有变化（幂等或无匹配），不计为修复"
            board.add_patch(patch)
            return False

        art = board.put_artifact(
            kind="draft",
            text=cur,
            created_by=patch.created_by or "fixer",
            stage=Stage.FIX.value,
            run_id=self.ctx.run_id,
            note=f"第 {self.ctx.round} 轮修复：{patch.rationale or patch.id}",
        )
        patch.applied = True
        board.add_patch(patch)
        board.mark_fixed(
            patch.issue_ids,
            by=patch.created_by or "fixer",
            stage=Stage.FIX.value,
            note=f"由 patch {patch.id} 修复 → 版本 v{art.version}",
        )
        board.record(
            Stage.FIX.value,
            self.ctx.round,
            patch.created_by or "fixer",
            "patch_applied",
            f"{len(patch.ops)} 个算子 → 稿件 v{art.version}",
        )
        return True

    # -- 循环 -------------------------------------------------------------
    def self_correct(
        self,
        *,
        max_rounds: int = 3,
        fixer_name: str = "fixer",
        include_llm_verify: bool = True,
    ) -> dict:
        """The loop that gives capability ⑦ its name.

        Stops early when no *fixable* issue remains, and reports honestly when
        the round budget runs out with blockers still open.
        """
        t0 = time.time()
        self.ctx.say("🔁 自我更正循环开始", 0.0)
        before = self.ctx.board.severity_counts()

        for r in range(1, max_rounds + 1):
            self.verify(include_llm=include_llm_verify, stop_on_blocker=(r > 1))
            fixable = [i for i in self.ctx.board.open_issues() if i.fixable and i.confidence >= AUTO_FIX_MIN_CONFIDENCE]
            if not fixable:
                self.ctx.say(f"✅ 第 {r} 轮：没有可机械修复的问题，循环结束")
                break
            rec = self.fix_once(round_no=r, fixer_name=fixer_name)
            if rec.patches_applied == 0 and rec.patches_failed == 0:
                self.ctx.say(f"⚠ 第 {r} 轮：修复器没有产出可应用的补丁，提前结束")
                break

        # 收尾：把仍 open 且不可机械修复的问题标记为需要作者处理。
        # **但 INFO 级不在此列**：那是提示性的，不该出现在"需要你判断"的队列里，
        # 否则一个"未提供 docx 结构，跳过格式检查"的提示会和"引用可能不存在"混在
        # 一起，真正需要作者行动的问题就被淹没了。
        for issue in self.ctx.board.open_issues():
            if not issue.fixable and issue.severity is not Severity.INFO:
                issue.status = "needs_author"
        self.ctx.board.save()

        after = self.ctx.board.severity_counts()
        remaining = self.ctx.board.open_issues()
        blockers = [i for i in remaining if i.severity is Severity.BLOCKER]
        majors = [i for i in remaining if i.severity is Severity.MAJOR]

        converged = not blockers and not majors
        summary = {
            "converged": converged,
            "rounds": len(self.rounds),
            "before": before,
            "after": after,
            "remaining_open": len(remaining),
            "remaining_blockers": len(blockers),
            "remaining_majors": len(majors),
            "needs_author": sum(
                1 for i in self.ctx.board.project.issues.values() if i.status == "needs_author"
            ),
            "rounds_detail": [r.as_dict() for r in self.rounds],
            "seconds": round(time.time() - t0, 2),
        }
        if converged:
            self.ctx.say(f"✅ 已收敛：{len(self.rounds)} 轮，无遗留 blocker/major", 1.0)
        else:
            self.ctx.say(
                f"⚠ 未完全收敛：仍有 {len(blockers)} 个致命、{len(majors)} 个重要问题"
                "（需要作者判断，已列出）",
                1.0,
            )
        self.ctx.bus.publish("review", sender="pipeline", payload=summary, stage=Stage.FIX.value)
        self.ctx.board.record(
            Stage.FIX.value, len(self.rounds), "pipeline", "self_correct", str(summary)
        )
        return summary
