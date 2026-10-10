"""能力③「论文写作助手」——局部润色（writer:polish）。

**这个智能体不改正文。** 它只对作者点名的片段给一份润色建议，写进 Issue 等人看。

为什么"能改也不改"：本项目里已经有两条会真正改写稿件的链路——自我更正循环
（针对已确认的错误）和降 AIGC 环节（针对表达风格）。写作助手再插一手，会出现
三个地方都能改同一段文字的局面，出了问题没人说得清是哪一环改坏的。职责重叠
不是"功能更强"，而是"没法追责"。所以这里只出建议。

润色最容易出的事故是**改着改着把事实改了**：把 3.2% 写成 3.5%，把"2019 年"
写成"2020 年"，或者悄悄删掉一个引用角标。这类改动在成稿里几乎无法靠肉眼发现。
因此每一处润色都必须先过对端降 AIGC 服务的保真校验 ``guard(original, polished)``，
**校验不通过就退回原文**——宁可不润色，也不要一份读起来更顺但数字错掉的稿子。
"""

from __future__ import annotations

import time
from typing import Any

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage, new_id

#: 单个片段润色的字数上限。太长会让模型顺手"重写整段"，
#: 而润色的边界是表达，不是结构。
MAX_CHARS = 1200
#: 一次最多处理多少段。润色建议是给作者看的，几十条堆在报告里等于没有报告。
MAX_TARGETS = 8

POLISH_SYSTEM = (
    "你是中文科技论文的文字编辑。你只改表达，不改内容。"
    "你绝不改动任何数字、单位、年份、百分比、专有名词、变量名，"
    "绝不增加或删除引用角标，绝不给原文添加原文没有的信息。"
)

POLISH_USER_TEMPLATE = """请对下面这段论文文字做语言润色，只改表达。

要求：
1. 保留原意。不许增加原文没有的信息，不许删掉原文有的事实。
2. 所有数字、单位、年份、百分比、变量名、专有名词必须**原样保留**。
3. 所有引用角标（形如 [1]、[1,3]）必须**原样保留在原来的位置**。
4. 去掉套话与空转的过渡语（"综上所述""值得注意的是""不言而喻"），
   把长句拆短，把被动语态改主动。不要为了"看起来学术"而堆砌辞藻。
5. 长度与原文相当，不要扩写成两倍。
6. 不要改变段落的论证结构，不要加小标题。

原文：
{original}

只输出润色后的文字，不要解释，不要 JSON，不要重复要求。"""


def _guard_verdict(reply: Any) -> tuple[bool, str]:
    """把 ``guard`` 的返回值判成"保真 / 不保真"。

    对端接口可能返回 ``ok`` 布尔、可能返回问题清单、也可能只给一个分数。
    这里刻意**没看懂就当作不通过**：保真校验是最后一道闸门，它的失败模式必须
    是"少润一段"，不能是"放过去一段改坏了数字的稿子"。
    """
    if not isinstance(reply, dict) or not reply:
        return False, "保真校验服务没有返回可判读的结果"

    # 显式的问题清单优先：有 issues 就是有问题，不管 ok 写的是什么。
    for key in ("issues", "problems", "violations", "findings", "warnings"):
        items = reply.get(key)
        if isinstance(items, (list, tuple)) and len(items) > 0:
            first = items[0]
            if isinstance(first, dict):
                first = first.get("message") or first.get("detail") or str(first)
            return False, f"保真校验发现 {len(items)} 处问题：{str(first)[:120]}"

    if "ok" in reply:
        return bool(reply["ok"]), "" if reply.get("ok") else str(reply.get("message") or "校验未通过")

    if "passed" in reply:
        return bool(reply["passed"]), "" if reply.get("passed") else str(reply.get("message") or "校验未通过")

    if "safe" in reply:
        return bool(reply["safe"]), "" if reply.get("safe") else str(reply.get("message") or "校验未通过")

    for key in ("fidelity", "score", "similarity"):
        if key in reply:
            try:
                val = float(reply[key])
            except (TypeError, ValueError):
                continue
            # 相似度类指标：1.0 表示完全一致。低于 0.9 视为改动过大。
            threshold = 0.9 if key == "similarity" or val <= 1.0 else 60.0
            return val >= threshold, "" if val >= threshold else f"{key}={val} 低于阈值 {threshold}"

    return False, "保真校验返回的字段无法判读（缺少 ok / passed / issues）"


class WriterPolishAgent(Agent):
    """对作者指定的片段给润色建议，且每一条都必须过保真校验。"""

    name = "writer:polish"
    description = "对作者指定片段给润色建议：保真校验不通过就退回原文，不改正文"
    stage = Stage.DRAFT.value
    capabilities = ("write", "polish")
    uses_model = True

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        board = ctx.board
        targets = board.project.meta.get("polish_targets") or []
        if not isinstance(targets, (list, tuple)):
            targets = [str(targets)]

        # 去重保序：作者常常把同一段贴两次，重复调用模型没有意义。
        seen: set[str] = set()
        queue: list[str] = []
        for raw in targets:
            text = str(raw or "").strip()
            if not text or text in seen:
                continue
            seen.add(text)
            queue.append(text)

        if not queue:
            return self.report(
                ctx,
                ok=False,
                summary="没有需要润色的片段：请在项目 meta 的 polish_targets 里给出要润色的原文",
                error="polish_targets 为空",
                data={"hint": "polish_targets: list[str]，逐段给出要润色的原文"},
            )

        ctx.say(f"🖋 润色 {min(len(queue), MAX_TARGETS)} 段（每段都要过保真校验）", 0.4)

        # 降 AIGC 适配器只在真正需要时构造：离线环境下不构造就不会抛错，
        # 而是走到 guard 调用处再报"服务不可用"，错误消息里带启动提示。
        from ..adapters.reducer import ReducerAdapter

        guard_client = ReducerAdapter(
            ctx.settings.peers.reducer_base,
            timeout=min(120.0, float(getattr(ctx.settings.peers, "timeout", 120.0) or 120.0)),
        )

        suggestions: list[dict[str, Any]] = []
        reverted: list[dict[str, Any]] = []
        issues: list[Issue] = []
        guard_unavailable = ""

        for i, original in enumerate(queue[:MAX_TARGETS]):
            # 超长片段直接跳过而不是截断：截断后润色的结果无法与原文对齐，
            # 作者拿到的建议会覆盖不到原文的全部内容，反而误导。
            if len(original) > MAX_CHARS:
                reverted.append(
                    {"original_preview": original[:60], "reason": f"片段 {len(original)} 字，超过 {MAX_CHARS} 字上限"}
                )
                continue

            try:
                polished = ctx.ask_model(
                    f"writer:polish:{i}",
                    [
                        {"role": "system", "content": POLISH_SYSTEM},
                        {"role": "user", "content": POLISH_USER_TEMPLATE.format(original=original)},
                    ],
                    temperature=0.3,
                ).strip()
            except Exception as exc:  # noqa: BLE001 - 单段失败不影响其他段
                reverted.append(
                    {"original_preview": original[:60], "reason": f"模型调用失败：{type(exc).__name__}: {exc}"}
                )
                continue

            if not polished or polished == original:
                reverted.append(
                    {"original_preview": original[:60], "reason": "模型没有给出改动（或返回空）"}
                )
                continue

            # ---- 保真校验：这是本智能体存在的意义 ----------------------
            try:
                reply = guard_client.guard(original, polished)
            except Exception as exc:  # noqa: BLE001 - 服务不可用不能崩，也不能放行
                guard_unavailable = str(exc) or f"{type(exc).__name__}"
                ctx.say("⚠ 降 AIGC 服务不可用，润色建议全部退回原文", 0.8)
                break

            passed, reason = _guard_verdict(reply)
            if not passed:
                # 校验不通过就退回原文：不提交任何"读起来更顺但可能改坏了事实"的建议。
                reverted.append(
                    {
                        "original_preview": original[:60],
                        "polished_preview": polished[:60],
                        "reason": reason or "保真校验未通过",
                    }
                )
                continue

            suggestions.append({"original": original, "polished": polished, "guard": _brief(reply)})
            issues.append(
                Issue(
                    id=new_id("iss"),
                    kind="writing.polish_suggestion",
                    severity=Severity.INFO,
                    message="润色建议（保真校验已通过，请作者自行决定是否采纳）",
                    location={"find": original[:40]},
                    evidence=polished[:200],
                    detected_by="llm:writer_polish",
                    confidence=0.7,
                    # False：把建议写成可自动应用的补丁就等于让写作助手改正文，
                    # 那是自我更正循环和降 AIGC 环节的职责，不能重叠。
                    fixable=False,
                    fix_hint=(
                        f"原文：{original[:80]}…\n建议改为：{polished[:80]}…\n"
                        "请人工确认后再改；系统不会自动应用润色。"
                    ),
                    status="open",
                )
            )

        if guard_unavailable:
            issues.append(
                Issue(
                    id=new_id("iss"),
                    kind="writing.polish_suggestion",
                    severity=Severity.INFO,
                    message=f"降 AIGC 保真校验服务不可用，润色建议已全部退回原文。{guard_unavailable}",
                    location={},
                    evidence=guard_unavailable,
                    detected_by="rules:writer_polish",
                    confidence=1.0,
                    fixable=False,
                    fix_hint="启动 aigc-reducer 服务后重跑润色；未过校验的建议一律不提交。",
                    status="needs_author",
                )
            )

        added = board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)
        board.record(
            ctx.stage,
            ctx.round,
            self.name,
            "polish",
            f"采纳建议 {len(suggestions)} 条、退回原文 {len(reverted)} 条",
        )
        board.save()

        ok = bool(suggestions) and not guard_unavailable
        summary = f"润色建议 {len(suggestions)} 条通过保真校验"
        if reverted:
            summary += f"；{len(reverted)} 条退回原文"
        if guard_unavailable:
            summary = f"保真校验服务不可用（{guard_unavailable[:80]}），未提交任何润色建议"
        return self.report(
            ctx,
            ok=ok,
            summary=summary,
            error=guard_unavailable,
            issues=[i.id for i in issues],
            data={
                "suggestions": suggestions,
                "reverted": reverted,
                "guard_unavailable": guard_unavailable,
                "generated_text_changed": False,
                "new_issues": added,
            },
            seconds=time.time() - t0,
        )


def _brief(reply: dict) -> dict:
    """只留保真校验里作者可能想看的字段，避免把整个响应塞进报告。"""
    keep = ("ok", "passed", "safe", "score", "fidelity", "similarity")
    return {k: reply[k] for k in keep if k in reply}
