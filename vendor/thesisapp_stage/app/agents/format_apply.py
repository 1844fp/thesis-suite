"""格式整理智能体（能力⑤ 的执行端）。

职责边界要说清楚，因为这个能力最容易被误解：

* **它做**：带着学校规范去检查 docx 的排版属性（字体、字号、行距、页边距、
  缩进、标题层级），把偏差逐条报成 ``Issue``，并给出可执行的修改指引。
* **它不做**：直接改 .docx 的排版属性并交付。原因有两个，都不是偷懒：
  1. 框架的修复算子是**文本变换**（精确替换、编号重排），改不了 OOXML 属性
     （``w:sz`` / ``w:spacing`` / ``w:pgMar``）。硬塞进算子体系只会造出
     "报可修复但实际改不动"的假承诺。
  2. 排版写回必须做"改完文档仍能正常打开"的验证。这类改动一旦写坏，
     作者可能到答辩前才发现，代价远高于"多改几次"。

所以本智能体的产出是**一份带精确定位的整改清单**，而不是一个改好的文件。
这份清单已经足够让人（或未来的写回模块）逐条落地。
"""

from __future__ import annotations

import json
import time
from typing import Any

from ..framework.agent import Agent, AgentContext, AgentReport
from ..framework.artifact import Issue, Severity, Stage

#: 这些规则类别是"排版属性"，只能靠改 OOXML 落地，不能进自动修复队列。
#: 罗列出来是为了在报告里把"要动手改属性"和"改文字就行"分开，
#: 作者按这个分组处理效率高得多。
_OOXML_SCOPES = ("font", "size", "line_spacing", "indent", "spacing", "margin", "align")


def _ruleset_of(meta: dict) -> Any:
    """Resolve the rule set for this project.

    Two entry points, both owned by ``app.format.checkers``:

    * ``meta["format_ruleset"]``      —— 已构造好的 RuleSet（对象或 dict）
    * ``meta["format_ruleset_path"]`` —— 指向一个 YAML 规则集

    默认回落到通用学位论文规范。**学校模板到位后，只需往 meta 里放一个
    RuleSet，不需要改本文件。**
    """
    try:
        from ..format import rules as fmt_rules
    except Exception:  # noqa: BLE001 - 格式层不可用时降级，而不是崩
        return None

    direct = meta.get("format_ruleset")
    if direct is not None:
        if isinstance(direct, dict):
            try:
                return fmt_rules.RuleSet.from_dict(direct)
            except Exception:  # noqa: BLE001
                return None
        return direct

    path = meta.get("format_ruleset_path")
    if path:
        try:
            return fmt_rules.RuleSet.from_yaml(path)
        except Exception:  # noqa: BLE001
            return None

    for name in ("load_default_ruleset", "DEFAULT_RULESET"):
        obj = getattr(fmt_rules, name, None)
        if callable(obj):
            try:
                return obj()
            except Exception:  # noqa: BLE001
                continue
        if obj is not None:
            return obj
    return None


def _facts_of(meta: dict) -> Any:
    """Rebuild DocxFormatFacts from the plain dict stored at intake time.

    收稿阶段把事实序列化成纯 dict 存进 meta（见 ``app/parsing/intake.py`` 的
    ``to_plain_dict``）。这里反向重建。重建失败返回 None —— 检查器会跳过并说明，
    绝不拿残缺的事实去报格式问题。
    """
    raw = meta.get("format_facts")
    if not raw:
        return None
    try:
        from ..parsing import docx_reader
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(docx_reader, "DocxFormatFacts", None)
    if fn is None or not hasattr(fn, "from_dict"):
        return None
    try:
        return fn.from_dict(raw)
    except Exception:  # noqa: BLE001
        return None


def _evaluate(text: str, facts: Any, ruleset: Any) -> list[Issue]:
    try:
        from ..format import rules as fmt_rules
    except Exception:  # noqa: BLE001
        return []
    fn = getattr(fmt_rules, "evaluate", None)
    if fn is None:
        return []
    try:
        return list(fn(facts, ruleset) or [])
    except Exception:  # noqa: BLE001 - 规则集不合法不应让整个阶段崩
        return []


class FormatApplyAgent(Agent):
    """Report formatting deviations; hand back an actionable list."""

    name = "format:apply"
    description = "按规范检查 docx 排版（字体/字号/行距/页边距/标题层级），出具整改清单"
    stage = Stage.FORMAT.value
    capabilities = ("format", "verify:format")
    uses_model = False

    def run(self, ctx: AgentContext) -> AgentReport:
        t0 = time.time()
        board = ctx.board
        meta = board.project.meta
        text = board.draft_text

        facts = _facts_of(meta)
        ruleset = _ruleset_of(meta)
        source = str(meta.get("source_path") or "")
        is_docx_source = source.lower().endswith(".docx") or bool(facts)

        if not facts:
            # 两种情况要分开说，否则作者会以为"格式没问题"：
            # (a) 稿件本来就不是 docx（比如直接粘的文本）—— 没事可做，正常；
            # (b) 是 docx 但结构没读出来 —— 这是集成故障，必须显眼地报出来。
            if is_docx_source:
                detail = "收稿阶段未能读出 docx 结构（format_facts 缺失），格式检查未执行"
                severity = Severity.MAJOR
                hint = (
                    "这是集成问题而非稿件问题：请确认 app.parsing.docx_reader 可用，"
                    "并查看收稿警告（project.meta.intake_warnings）。"
                )
            else:
                detail = "稿件不是 .docx 来源，无法检查排版属性（字体/字号/行距/页边距需要 docx 结构）"
                severity = Severity.INFO
                hint = "若要检查版式，请提供 .docx 原稿（PDF 与纯文本没有可读的排版属性）。"
            board.add_issues(
                [
                    Issue(
                        id="",
                        kind="format.not_checked" if not is_docx_source else "format.facts_missing",
                        severity=severity,
                        message=detail,
                        location={"stage": "format"},
                        evidence="",
                        detected_by="rules:format",
                        fixable=False,
                        fix_hint=hint,
                    )
                ],
                stage=ctx.stage,
                round_no=ctx.round,
            )
            board.save()
            return self.report(
                ctx,
                ok=not is_docx_source,
                summary=detail,
                data={"checked": False, "is_docx_source": is_docx_source},
                seconds=time.time() - t0,
            )

        if ruleset is None:
            board.add_issues(
                [
                    Issue(
                        id="",
                        kind="format.no_ruleset",
                        severity=Severity.MAJOR,
                        message="docx 结构已读到，但规则集加载失败，格式检查未执行",
                        location={"stage": "format"},
                        evidence="",
                        detected_by="rules:format",
                        fixable=False,
                        fix_hint="检查 app/format/rules_default.yaml 是否可解析，或 meta.format_ruleset_path 指向的文件。",
                    )
                ],
                stage=ctx.stage,
                round_no=ctx.round,
            )
            board.save()
            return self.report(
                ctx,
                ok=False,
                summary="规则集加载失败",
                error="ruleset unavailable",
                data={"checked": False},
                seconds=time.time() - t0,
            )

        ctx.say("正在按规范检查排版…", 0.0)
        issues = _evaluate(text, facts, ruleset)
        added = board.add_issues(issues, stage=ctx.stage, round_no=ctx.round)

        # 按"要不要动手改属性"分组。作者按组处理比逐条翻效率高得多。
        attr_issues = [i for i in issues if not i.fixable]
        text_issues = [i for i in issues if i.fixable]
        ruleset_label = getattr(ruleset, "school", "") or getattr(ruleset, "name", "") or "通用规范"

        art = board.put_artifact(
            kind="format_report",
            payload={
                "ruleset": ruleset_label,
                "total": len(issues),
                "attribute_fixes": [i.as_dict() for i in attr_issues],
                "text_fixes": [i.as_dict() for i in text_issues],
                "by_severity": {
                    s.value: sum(1 for i in issues if i.severity is s)
                    for s in (Severity.BLOCKER, Severity.MAJOR, Severity.MINOR, Severity.INFO)
                },
                "write_back_supported": False,
                "write_back_note": (
                    "排版属性（字体/字号/行距/页边距/缩进/对齐）的写回尚未实现："
                    "框架的修复算子是文本变换，改不了 OOXML 属性；"
                    "且写回必须先做「改完文档仍能打开」的验证，否则风险高于收益。"
                ),
            },
            created_by=self.name,
            stage=ctx.stage,
            run_id=ctx.run_id,
            note=f"按《{ruleset_label}》检查，发现 {len(issues)} 处偏差",
        )
        board.save()

        n_attr = len(attr_issues)
        summary = (
            f"按《{ruleset_label}》发现 {len(issues)} 处偏差"
            f"（新增 {added} 条）：需改排版属性 {n_attr} 处、可改文字 {len(text_issues)} 处"
        )
        return self.report(
            ctx,
            ok=True,
            summary=summary,
            artifacts=[art.id],
            data={
                "checked": True,
                "total": len(issues),
                "added": added,
                "attribute_fixes": n_attr,
                "text_fixes": len(text_issues),
                "ruleset": ruleset_label,
                "write_back_supported": False,
            },
            seconds=time.time() - t0,
        )
