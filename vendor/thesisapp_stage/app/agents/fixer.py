"""修复器智能体：把问题清单变成可复验的补丁。

自我更正循环里最容易被做坏的一环。三条纪律：

1. **能确定性修复的，不要问模型。** 标点规范化、删套话、编号重排、删重复段落
   这些都有唯一正确答案，交给模型只会引入不确定性，还白花钱。只有"改哪一处、
   改成什么"需要理解的，才走模型。
2. **补丁必须先干跑验证。** 一个算子在某个问题上失败，会把整批应用变成
   全有或全无；所以在交付补丁前，先在本地副本上按顺序跑一遍，失败的直接丢弃
   并不交付。这样"判为可修复"与"真的能修"才是同一件事。
3. **补丁之间不能冲突。** 多个补丁都改同一段文本时，按顺序在**同一个工作副本**上
   干跑，后一个补丁看到的是前一个改过的文本。冲突的补丁会自然失败并被丢弃，
   而不是在真正应用时才发现。

修复器**不修改文档**。它只产出 Patch，由状态机在复验前后统一应用——因为只有
状态机知道当前是第几轮、当前稿件是哪个版本。
"""

from __future__ import annotations

import json
from typing import Any

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Patch, Severity, Stage, new_id
from ..framework.ops import OPS, apply_op
from ..framework.registry import FIXERS, fixer

#: 允许模型调用的算子。刻意排除 delete_span/insert_after/merge_duplicate_paragraphs：
#: 它们改动幅度大且难以在提示词里说清边界，让模型用 apply_edits 表达更可审计。
MODEL_ALLOWED_OPS = (
    "apply_edits",
    "replace_span",
    "unify_term",
    "strip_filler",
    "normalize_punctuation",
)

#: 确定性算子：issue.fix_op 落在这些名字里时，直接用固定参数，不问模型。
DETERMINISTIC_OPS = {
    "normalize_punctuation": {"cjk_quotes": True, "fullwidth": True},
    "strip_filler": {},
    "merge_duplicate_paragraphs": {"min_chars": 20},
}

SYSTEM_PROMPT = """你是学位论文的校对修复助手。你的任务不是改写文章，而是**做最小必要的精确修改**。

铁律：
1. 只修被指出的那个问题，不要顺手改别的地方，不要润色，不要调整语序。
2. 每条修改必须给出 `find`（原文里的精确片段）与 `replace`（改后的片段）。
3. `find` 必须在原文中**逐字出现且只出现一次**。做不到唯一时，请把 `find`
   写得长一些（带上前后各几个字）以保证唯一。宁可多带几个字。
4. 数字、引用标注、专业术语、人名、机构名一律**原样保留**。
5. 你无法在不改变原意的前提下修好某条问题，就**不要为它产出补丁**——
   漏掉一条比改坏一处好得多。把理由写在 skipped 里。

输出严格的 JSON，不要解释，不要 Markdown 围栏：
{
  "patches": [
    {
      "issue_ids": ["iss-xxxx"],
      "edits": [{"find": "原文片段", "replace": "改后片段"}],
      "rationale": "一句话说明改了什么"
    }
  ],
  "skipped": [{"issue_id": "iss-yyyy", "reason": "为什么修不了"}]
}

多条问题可以合并在一个 patch 里（用同一个 edits 列表），但只要有一处 find 不唯一，
整个 patch 就会被拒绝。所以**不确定的修改请单独成 patch**。"""


def build_user_prompt(issues: list[Issue], text: str) -> str:
    blocks: list[str] = []
    for i, issue in enumerate(issues, 1):
        loc = json.dumps(issue.location or {}, ensure_ascii=False)
        blocks.append(
            f"### 问题 {i}\n"
            f"- id: {issue.id}\n"
            f"- 类别: {issue.kind}\n"
            f"- 严重度: {issue.severity.value}\n"
            f"- 描述: {issue.message}\n"
            f"- 证据片段: {issue.evidence[:200]!r}\n"
            f"- 定位: {loc}\n"
            f"- 修复建议: {issue.fix_hint or '（无）'}"
        )
    issue_text = "\n\n".join(blocks)

    # 稿子可能很长。只给问题附近的上下文，既省额度也让模型专注于该改的地方——
    # 把 10 万字全塞进去，模型反而容易改到别处。
    excerpts: list[str] = []
    for issue in issues:
        find = str((issue.location or {}).get("find") or "")
        probe = find or (issue.evidence or "")[:40]
        if not probe:
            continue
        idx = text.find(probe)
        if idx < 0:
            continue
        start = max(0, idx - 300)
        end = min(len(text), idx + len(probe) + 300)
        excerpts.append(f"……{text[start:end]}……")

    context = "\n\n".join(dict.fromkeys(excerpts))  # 去重且保持顺序
    return (
        f"以下是待修复的问题清单：\n\n{issue_text}\n\n"
        f"以下是每个问题附近的原文（用「……」标出截断）：\n\n{context}\n\n"
        "请只针对上述问题产出补丁。"
    )


def auto_op_for_issue(issue: Issue) -> dict[str, Any] | None:
    """Return a fixed op dict when the fix needs no judgement.

    「不需要判断」的标准是：**同一个输入永远对应同一个输出**。
    标点规范化、删套话、删连续重复段落都满足；而"把这句话改成什么"不满足。

    注意这里**没有** ``@fixer`` 装饰器：它不是能被 ``Patch.ops`` 引用的算子，
    只是本模块内部的查找工具。注册成算子会让它出现在界面的"可用修复算子"列表里，
    进而被误用。
    """
    name = (issue.fix_op or "").strip()
    if name in DETERMINISTIC_OPS:
        return {"op": name, **DETERMINISTIC_OPS[name]}

    if name == "renumber_sequence":
        # 编号重排必须限定在参考文献区，否则会把正文里的 [n] 引用角标一起重排，
        # 而引用角标与文献列表是两套编号体系。范围由 description 提供。
        pattern = str((issue.location or {}).get("pattern") or r"\[(\d+)\]")
        out: dict[str, Any] = {"op": "renumber_sequence", "pattern": pattern, "start": 1}
        for key in ("scope_start", "scope_end"):
            if (issue.location or {}).get(key) is not None:
                out[key] = issue.location[key]
        return out

    if name == "unify_term":
        variants = (issue.location or {}).get("variants")
        canonical = (issue.location or {}).get("canonical")
        if variants and canonical:
            return {"op": "unify_term", "variants": list(variants), "canonical": str(canonical)}
        return None

    return None


class FixerAgent(Agent):
    """Produces patches for open, fixable issues."""

    name = "fixer"
    description = "把可机械修复的问题转成经过干跑验证的补丁"
    stage = Stage.FIX.value
    capabilities = ("fix",)
    uses_model = True

    #: 一次交给模型的批大小。太大时模型会漏掉其中几条，也会让 find 定位更容易出错。
    BATCH = 4

    def run(self, ctx: AgentContext) -> AgentReport:
        board = ctx.board
        text = board.draft_text
        if not text.strip():
            return self.report(ctx, ok=False, summary="稿件为空，无内容可修复", error="empty draft")

        candidates = [
            i
            for i in board.open_issues()
            if i.fixable
            and i.fix_op
            and i.confidence >= 0.7
        ]
        if not candidates:
            return self.report(
                ctx,
                ok=True,
                summary="没有需要修复的可修复问题",
                data={"patches": [], "auto": 0, "model": 0, "skipped": 0},
            )

        auto_issues: list[Issue] = []
        model_issues: list[Issue] = []
        for issue in candidates:
            if auto_op_for_issue(issue) is not None:
                auto_issues.append(issue)
            else:
                model_issues.append(issue)

        working = text
        patches: list[Patch] = []
        skipped: list[dict] = []

        # --- 确定性部分：不花钱、不走模型 ---
        for issue in auto_issues:
            op = auto_op_for_issue(issue)
            assert op is not None
            patch = Patch(
                id=new_id("ptc"),
                issue_ids=[issue.id],
                ops=[op],
                rationale=f"确定性修复：{issue.kind}",
                created_by=self.name,
            )
            ok, working, why = self._dry_run(working, patch)
            if ok:
                patches.append(patch)
            else:
                skipped.append({"issue_id": issue.id, "reason": why, "mode": "auto"})

        # --- 需要理解的部分：问模型 ---
        model_used = 0
        for batch in _chunks(model_issues, self.BATCH):
            if not ctx.llm_configured:
                for issue in batch:
                    skipped.append(
                        {"issue_id": issue.id, "reason": "未配置模型，无法生成修改内容", "mode": "model"}
                    )
                continue
            try:
                raw = ctx.ask_json(
                    "fixer",
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": build_user_prompt(batch, working)},
                    ],
                    temperature=ctx.settings.llm.review_temperature,
                )
            except Exception as exc:  # noqa: BLE001 - 一批失败不影响其它批
                for issue in batch:
                    skipped.append(
                        {"issue_id": issue.id, "reason": f"模型调用失败：{type(exc).__name__}: {exc}", "mode": "model"}
                    )
                continue

            model_used += 1
            valid_ids = {i.id for i in batch}
            data = raw if isinstance(raw, dict) else {}
            for item in data.get("patches") or []:
                if not isinstance(item, dict):
                    continue
                ids = [str(x) for x in (item.get("issue_ids") or []) if str(x) in valid_ids]
                if not ids:
                    # 模型给了一个不在本批里的 issue id：丢弃，避免补丁挂到别的问题上。
                    skipped.append(
                        {
                            "issue_id": ",".join(str(x) for x in (item.get("issue_ids") or [])) or "(空)",
                            "reason": "补丁没有关联到本批的任何问题，已丢弃",
                            "mode": "model",
                        }
                    )
                    continue
                edits = item.get("edits") or []
                if not isinstance(edits, list) or not edits:
                    skipped.append({"issue_id": ",".join(ids), "reason": "补丁没有给出任何 edits", "mode": "model"})
                    continue
                patch = Patch(
                    id=new_id("ptc"),
                    issue_ids=ids,
                    ops=[{"op": "apply_edits", "edits": edits}],
                    rationale=str(item.get("rationale") or "")[:200],
                    created_by=self.name,
                )
                ok, working, why = self._dry_run(working, patch)
                if ok:
                    patches.append(patch)
                else:
                    skipped.append({"issue_id": ",".join(ids), "reason": why, "mode": "model"})

            for item in data.get("skipped") or []:
                if isinstance(item, dict):
                    skipped.append(
                        {
                            "issue_id": str(item.get("issue_id") or ""),
                            "reason": str(item.get("reason") or "")[:200],
                            "mode": "model",
                        }
                    )

        total_edits = sum(len(p.ops) for p in patches)
        return self.report(
            ctx,
            ok=True,
            summary=(
                f"生成 {len(patches)} 个补丁（确定性 {len(auto_issues)} 条、"
                f"模型处理 {len(model_issues)} 条，调用模型 {model_used} 次），"
                f"丢弃 {len(skipped)} 条"
            ),
            data={
                "patches": patches,
                "auto": len(auto_issues),
                "model_issues": len(model_issues),
                "model_calls": model_used,
                "patches_count": len(patches),
                "ops_count": total_edits,
                "skipped": skipped,
            },
        )

    @staticmethod
    def _dry_run(text: str, patch: Patch) -> tuple[bool, str, str]:
        """Apply the patch to a copy. Returns ``(ok, new_text, why)``.

        干跑的意义：把"判为可修复"和"真的能修"合并成同一件事。
        交付一个必然失败的补丁，只会让状态机记一条 patch_failed，白费一轮。
        """
        cur = text
        for idx, op in enumerate(patch.ops):
            res = apply_op(cur, op)
            if not res.ok:
                return False, text, f"算子 {idx+1} 失败：{res.error}"
            cur = res.text
        if cur == text:
            return False, text, "算子执行后文本无变化（可能已修复或无匹配）"
        return True, cur, ""


def _chunks(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def register_fixers() -> dict[str, Any]:
    """Report which deterministic ops are available, for the UI to display."""
    return {
        "fixers": sorted(FIXERS.keys()),
        "ops": sorted(OPS.keys()),
        "deterministic": sorted(DETERMINISTIC_OPS.keys()),
        "model_allowed": list(MODEL_ALLOWED_OPS),
    }
