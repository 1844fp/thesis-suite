"""参考文献表构建：把抽取结果变成规范的 ``references`` 工件。

这个模块很小，但它承担一个容易被忽略的职责：**把"抽到了什么、验成了什么"如实
记录下来**。

为什么值得单独做一件事：外部核验只能核验它看得懂的字段。如果某条文献因为抽取
失败而缺了题目，核验会跳过它（``citation.no_title``），而作者看到的是一份排版
整齐的参考文献表——**整齐的排版会让人以为这一条也已经被核实过了**。所以 payload
里必须带上 ``unverified`` 列表与每条缺失的字段名，让"没验成"这件事无法被排版掩盖。

同理，本模块**不补全任何字段**：缺年份就不写年份，缺刊名就不写刊名。把
``[J]. , 2020.`` 这类缺口暴露出来，比悄悄填一个看起来合理的值安全得多——
后者等于替作者伪造数据。
"""

from __future__ import annotations

import time
from typing import Any

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Stage
from .references_extract import RefEntry, build_reference_text, parse_entries, split_reference_section
from .verify_external import META_KEY, VERDICT_CONFIRMED, load_cached

__all__ = ["ReferencesBuildAgent", "build_payload"]


def _verification_map(ctx: AgentContext) -> dict[str, dict]:
    """读出外部核验结果，键为条目指纹。

    读的是 ``verify:external`` 写进项目 meta 的那份缓存。这样做而不是把结果在
    内存里传给下一个智能体，是为了让两个智能体的执行顺序可以互换、也可以只跑
    其中一个——"生成文献表"不该依赖"核验刚刚跑过"。
    """
    return load_cached(ctx.board)


def build_payload(
    entries: list[RefEntry], verifications: dict[str, dict] | None = None
) -> dict[str, Any]:
    """组装 references 工件的 payload。

    ``unverified`` 的成员是自解释的：每条带上原序号、原文、指纹、缺失字段，
    以及（若有）外部核验的结论。作者据此能一眼看出"这条没验成，原因是缺题目"，
    而不是笼统地收到一个"有 3 条未核验"的计数。

    ``issues_summary`` 汇总各结论的条数，供界面顶部显示。
    """
    vmap = verifications or {}
    counts = {VERDICT_CONFIRMED: 0, "weak_match": 0, "not_found": 0, "inconclusive": 0}
    unverified: list[dict] = []

    for entry in entries:
        row = vmap.get(entry.fingerprint) or {}
        verdict = row.get("verdict") or ""
        if verdict:
            counts[verdict] = counts.get(verdict, 0) + 1
        if verdict != VERDICT_CONFIRMED:
            unverified.append(
                {
                    "index": entry.index,
                    "fingerprint": entry.fingerprint,
                    "title": entry.title,
                    "missing_fields": entry.missing,
                    "verdict": verdict or "not_checked",
                    "raw": entry.raw,
                }
            )

    return {
        "entries": [e.as_dict() for e in entries],
        "count": len(entries),
        "unverified": unverified,
        "issues_summary": {
            "counts": counts,
            "unverified": len(unverified),
            "confirmed": counts.get(VERDICT_CONFIRMED, 0),
        },
    }


class ReferencesBuildAgent(Agent):
    """从稿件里抽出条目，生成规范排版的参考文献工件。"""

    name = "references:build"
    description = (
        "抽取稿件里的参考文献条目，按 GB/T 7714 风格重排编号与字段顺序，"
        "生成 references 工件；不补全任何缺失字段，并把未通过外部核验的条目单独列出。"
    )
    stage = Stage.FORMAT.value
    capabilities = ("references", "build", "format:references")
    uses_model = False

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        text = ctx.board.draft_text or ""
        _, ref_text = split_reference_section(text)
        if not ref_text.strip():
            return self.report(
                ctx,
                ok=True,
                summary="未找到参考文献区，未生成参考文献工件。",
                data={"skipped": True, "reason": "no_reference_section"},
                seconds=time.time() - t0,
            )

        entries = parse_entries(ref_text)
        if not entries:
            return self.report(
                ctx,
                ok=True,
                summary="未能从参考文献区切出条目，未生成参考文献工件。",
                data={"skipped": True, "reason": "no_entries"},
                seconds=time.time() - t0,
            )

        payload = build_payload(entries, _verification_map(ctx))
        artifact = ctx.board.put_artifact(
            kind="references",
            payload=payload,
            text=build_reference_text(entries),
            created_by=self.name,
            stage=ctx.stage or self.stage,
            run_id=ctx.run_id,
            note=f"共 {len(entries)} 条，其中 {len(payload['unverified'])} 条未通过外部核验",
        )

        incomplete = sum(1 for e in entries if e.missing)
        return self.report(
            ctx,
            ok=True,
            summary=(
                f"生成参考文献表：{len(entries)} 条，编号已重排为连续；"
                f"{incomplete} 条存在缺失字段（未补全），"
                f"{len(payload['unverified'])} 条未通过外部核验。"
            ),
            artifacts=[artifact.id],
            data={
                "count": len(entries),
                "incomplete": incomplete,
                "unverified": len(payload["unverified"]),
                "issues_summary": payload["issues_summary"],
                "artifact_id": artifact.id,
            },
            seconds=time.time() - t0,
        )
