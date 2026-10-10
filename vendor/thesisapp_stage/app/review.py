"""模拟审稿意见书：把问题清单整理成评审看得懂的形式，并用代码守住三条诚信规则。

## 来源与它解决的问题

这个模块的输出格式与三条诚信规则，来自港科大（广州）DIAL 实验室 Supervisor-Skills 的
``pre-submission-reviewer`` 技能（<https://github.com/HKUSTDial/Supervisor-Skills>，
许可 CC BY 4.0）。该项目指出了一个真实的失败模式：

> 一份列了十几条 MINOR、却漏掉那一条致命缺陷的评审，会把作者送进投稿，还带着虚假的信心；
> 而把品味问题夸大成 CRITICAL，又会毁掉作者对工具的信任。

本项目已有的 49 个检查器产出的是一份**平铺的问题清单**。清单能说明"有哪些问题"，
但说明不了"这些问题合起来意味着什么、现在能不能交"。审稿意见书补的就是这一层。

## 三条诚信规则由代码强制，不由模型自觉

这是本模块与"让模型写一段评审意见"的根本区别：

1. **每条发现必须引用具体原文。** 没有 ``evidence`` 的问题**不进意见书正文**——
   作者拿不到定位的发现等于空话。被剔除的条数如实报出，不静默丢弃。
2. **每条致命问题必须给出具体修改建议。** 缺 ``fix_hint`` 的逐条标出，
   提示作者这一条需要人工判断。
3. **最终结论必须与发现一致。** 评分与提交建议**由 blocker/major 的数量机械推导**，
   不是模型打的印象分：有致命问题就不可能得到"可以提交"。

第三条尤其重要：模型给自己刚生成的稿子打分会系统性偏高，而"能不能交"这个判断
一旦失真，整份意见书就没有意义了。所以把它做成算式。

## 它不做什么

**它不是一次真正的评审。** 它只是把机器能查的问题按审稿人的读法组织起来。
人类审稿人会判断"这个贡献够不够新""这个问题重不重要"，那些判断这里没有，
意见书末尾会把这句话原样写给作者。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from .framework.artifact import Issue, Severity

__all__ = ["ReviewReport", "build_review", "render_review"]

#: 严重度 → 审稿口径。``info`` 不进意见书（那是"我们跳过了某项检查"这类提示，
#: 不是审稿意见；混进去会把真正需要处理的问题淹掉）。
_SEV_LABEL = {
    Severity.BLOCKER: "CRITICAL",
    Severity.MAJOR: "MAJOR",
    Severity.MINOR: "MINOR",
}

#: 五个审稿维度。键是 issue.kind 的前缀/完整名。
_DIMENSIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "宏观逻辑",
        "论证链条、贡献与章节的对应、各章是否各司其职",
        (
            "writing.intro_", "writing.lit_", "writing.method_", "writing.disc_",
            "writing.conclusion_", "writing.contribution_", "writing.overclaim",
            "writing.intro_challenge", "writing.intro_gap", "writing.missing_evidence",
            "writing.irrelevant_evidence", "structure.",
        ),
    ),
    (
        "写作细节",
        "段落结构、论证是否推进、句长节奏、模板腔",
        (
            "writing.no_analysis", "writing.claim_without_evidence", "writing.flat_",
            "writing.template_", "writing.ai_tone_", "writing.duplicate",
            "paragraph.", "sentence.", "writing.overclaim_",
        ),
    ),
    (
        "语言与格式",
        "标点、空白、术语一致、排版属性",
        ("punct.", "format.", "text.", "whitespace.", "term.", "ai.", "heading."),
    ),
    (
        "引用与文献",
        "引用是否真实存在、编号是否对应、文献表是否完整",
        (
            "citation.", "references.", "ref.", "writing.fabricated_citation",
            "writing.conclusion_new_citation", "writing.needs_source",
            "writing.number_without_source", "writing.conclusion_new_number",
        ),
    ),
    (
        "图表与公式",
        "图表编号、题注、公式引用",
        ("figure.", "table.", "equation.", "caption."),
    ),
)


def _dimension_of(kind: str) -> str:
    k = kind or ""
    for name, _desc, prefixes in _DIMENSIONS:
        for p in prefixes:
            if k.startswith(p):
                return name
    return "其它"


@dataclass
class Finding:
    """一条审稿发现。"""

    dimension: str
    label: str
    kind: str
    message: str
    evidence: str = ""
    fix_hint: str = ""
    issue_id: str = ""
    where: str = ""

    @property
    def has_quote(self) -> bool:
        """诚信规则 1：必须引用具体原文。"""
        return bool((self.evidence or "").strip())

    @property
    def has_fix(self) -> bool:
        """诚信规则 2：致命问题必须给出具体建议。"""
        return bool((self.fix_hint or "").strip())


@dataclass
class ReviewReport:
    """一份模拟审稿意见书。"""

    title: str = ""
    generated_at: float = field(default_factory=time.time)
    counts: dict[str, int] = field(default_factory=dict)
    dimensions: dict[str, list[Finding]] = field(default_factory=dict)
    #: 因缺少原文引用而被挡在正文之外的问题数（诚信规则 1）。
    dropped_no_quote: int = 0
    #: 缺修改建议的致命问题数（诚信规则 2）。
    critical_without_fix: int = 0
    score: float = 0.0
    recommendation: str = ""
    top_fixes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "counts": dict(self.counts),
            "score": self.score,
            "recommendation": self.recommendation,
            "top_fixes": list(self.top_fixes),
            "dropped_no_quote": self.dropped_no_quote,
            "critical_without_fix": self.critical_without_fix,
            "dimensions": {
                k: [
                    {"label": f.label, "kind": f.kind, "message": f.message,
                     "evidence": f.evidence, "fix_hint": f.fix_hint, "where": f.where}
                    for f in v
                ]
                for k, v in self.dimensions.items()
            },
        }


def _where(issue: Issue) -> str:
    loc = issue.location or {}
    bits = []
    if loc.get("section"):
        bits.append(str(loc["section"]))
    if loc.get("paragraph") not in (None, -1):
        bits.append(f"第 {int(loc['paragraph']) + 1} 段")
    if loc.get("line"):
        bits.append(f"第 {loc['line']} 行")
    if loc.get("find"):
        bits.append(f"「{str(loc['find'])[:20]}」")
    return " · ".join(bits)


def build_review(issues: Sequence[Issue], *, title: str = "") -> ReviewReport:
    """把问题清单整理成审稿意见书，并强制执行三条诚信规则。"""
    report = ReviewReport(title=title)
    counts = {"CRITICAL": 0, "MAJOR": 0, "MINOR": 0}

    # 严重度倒序：审稿人先看致命的。
    ordered = sorted(
        (i for i in issues if i.severity in _SEV_LABEL and i.status != "fixed"),
        key=lambda i: (-i.rank, i.kind),
    )

    for issue in ordered:
        label = _SEV_LABEL[issue.severity]
        counts[label] += 1
        f = Finding(
            dimension=_dimension_of(issue.kind),
            label=label,
            kind=issue.kind,
            message=issue.message,
            evidence=(issue.evidence or "").strip(),
            fix_hint=(issue.fix_hint or "").strip(),
            issue_id=issue.id,
            where=_where(issue),
        )
        # 诚信规则 1：没有原文引用的问题不进正文。它们不是"没问题"，
        # 而是"作者拿不到定位、无法据此修改"，所以只计数、不静默丢弃。
        if not f.has_quote:
            report.dropped_no_quote += 1
            continue
        # 诚信规则 2：致命问题必须有具体修改建议，缺的单独计数并提示。
        if f.label == "CRITICAL" and not f.has_fix:
            report.critical_without_fix += 1
        report.dimensions.setdefault(f.dimension, []).append(f)

    report.counts = counts
    report.score, report.recommendation = _score_and_recommend(counts)
    report.top_fixes = _top_fixes(report)
    return report


def _score_and_recommend(counts: dict[str, int]) -> tuple[float, str]:
    """**由数量机械推导**评分与提交建议。

    这是本模块最要紧的一条：模型给自己刚生成的稿子打分会系统性偏高，
    而"能不能交"这个判断一旦失真，整份意见书就没有意义了。所以这里用算式，
    不用印象：

    * 每有一条致命问题扣 3 分，重要扣 1 分，轻微扣 0.2 分；
    * **致命问题存在时，分数上限 6.0，且结论必定是"须修改后再提交"**——
      这条上限不是算式推出来的，是硬规定：一份有致命缺陷的稿子不能因为
      "只有一条"就显得接近及格。
    * 轻微问题再多也不影响结论（它们是打磨项，不是拦截项）。
    """
    crit = counts.get("CRITICAL", 0)
    major = counts.get("MAJOR", 0)
    minor = counts.get("MINOR", 0)

    raw = 10.0 - 3.0 * crit - 1.0 * major - 0.2 * minor
    score = max(1.0, min(10.0, raw))
    if crit > 0:
        score = min(score, 6.0)
    score = round(score, 1)

    if crit > 0:
        rec = f"须修改后再提交：还有 {crit} 条致命问题未处理"
    elif major > 2:
        rec = f"还需 1~2 天修改：{major} 条重要问题，建议逐条处理后重跑检查"
    elif major > 0:
        rec = f"接近可提交：剩 {major} 条重要问题，处理完即可"
    else:
        rec = "机器检查未发现致命或重要问题。**这不等于可以提交**——见文末说明。"
    return score, rec


def _top_fixes(report: ReviewReport, *, limit: int = 3) -> list[str]:
    """最该先改的三件事：致命优先，其次重要。"""
    out: list[str] = []
    for label in ("CRITICAL", "MAJOR"):
        for findings in report.dimensions.values():
            for f in findings:
                if f.label != label:
                    continue
                hint = f.fix_hint or f.message
                if hint not in out:
                    out.append(hint[:120])
                if len(out) >= limit:
                    return out
    return out


_SEV_MARK = {"CRITICAL": "🔴", "MAJOR": "🟠", "MINOR": "🟡"}


def render_review(report: ReviewReport, *, limit_per_dimension: int = 12) -> str:
    """渲染成 Markdown 意见书。"""
    lines: list[str] = [
        f"# 模拟审稿意见书 · {report.title or '（未命名）'}",
        "",
        "> 由本系统的机器检查器汇总而成，**不是人类评审的意见**。",
        "> 它按审稿人的读法把问题组织成五个维度，但不会判断"
        "「这个贡献够不够新」这类需要专业判断的问题。",
        "",
        "## 汇总",
        "",
        f"- 🔴 致命 CRITICAL：**{report.counts.get('CRITICAL', 0)}**",
        f"- 🟠 重要 MAJOR：**{report.counts.get('MAJOR', 0)}**",
        f"- 🟡 轻微 MINOR：**{report.counts.get('MINOR', 0)}**",
        "",
        f"**评分：{report.score} / 10**",
        "",
        f"**提交建议：{report.recommendation}**",
        "",
    ]

    if report.top_fixes:
        lines += ["### 最该先改的三件事", ""]
        lines += [f"{i}. {t}" for i, t in enumerate(report.top_fixes, 1)]
        lines.append("")

    if report.dropped_no_quote:
        lines += [
            f"> 注：另有 {report.dropped_no_quote} 条发现**未列入**本意见书——"
            "它们没有引用稿件里的具体原文，作者拿不到定位就无法据此修改。"
            "这些条目在「问题」页签里仍可查看。",
            "",
        ]

    for name, desc, _prefixes in _DIMENSIONS:
        findings = report.dimensions.get(name) or []
        if not findings:
            continue
        lines += [f"## {name}", "", f"*{desc}*", ""]
        # **按问题类别归并**：同一类问题（例如 10 处「一篇论文半句话」）的修改建议
        # 本来就一样，逐条重复十遍会把表格撑成一堵墙，真正不同的那几条反而看不见。
        # 归并后：每条给出类别、条数、修改建议各一次，再列出各处的原文引用。
        groups: dict[tuple[str, str], list[Finding]] = {}
        for f in findings:
            groups.setdefault((f.label, f.kind), []).append(f)
        lines += ["| # | 问题类别 | 条数 | 严重度 | 修改建议 |", "|---|---|---:|---|---|"]
        for i, ((label, kind), items) in enumerate(
            sorted(groups.items(), key=lambda kv: (-{"CRITICAL": 3, "MAJOR": 2, "MINOR": 1}.get(kv[0][0], 0), kv[0][1])),
            1,
        ):
            fix = (items[0].fix_hint or "（需要人工判断）").replace("|", "｜").replace("\n", " ")[:130]
            lines.append(f"| {i} | `{kind}` | {len(items)} | {_SEV_MARK.get(label, '')} {label} | {fix} |")
        lines.append("")
        # 归并之后必须**保留逐处的原文引用**——否则作者不知道该改哪里，
        # 而"每条发现都引用具体原文"正是这套格式最要紧的一条纪律。
        lines.append("各处原文：")
        lines.append("")
        for i, f in enumerate(findings[:limit_per_dimension], 1):
            quote = (f.evidence or f.message).replace("\n", " ")[:120]
            where = f"（{f.where}）" if f.where else ""
            lines.append(f"{i}. `{f.kind}`{where}：{quote}")
        if len(findings) > limit_per_dimension:
            lines.append(f"… 另有 {len(findings) - limit_per_dimension} 条同类问题，见「问题」页签。")
        lines.append("")

    extra = report.dimensions.get("其它") or []
    if extra:
        lines += ["## 其它", ""]
        lines += [f"- {_SEV_MARK.get(f.label, '')} {f.label}：{f.message[:100]}" for f in extra[:limit_per_dimension]]
        lines.append("")

    if report.critical_without_fix:
        lines += [
            f"> ⚠ 有 {report.critical_without_fix} 条致命问题缺少具体的修改建议，"
            "需要你或导师判断怎么改。",
            "",
        ]

    lines += [
        "## 这份意见书**不能**替代什么",
        "",
        "1. **不能判断贡献是否够新、问题是否重要。** 那是人类审稿人的专业判断。",
        "2. **不能替代查重与 AIGC 检测。** 本系统不做这两件事。",
        "3. **评分只是问题数量的换算，不是学术评价。** 它的用途是让"
        "「还差多少」这件事可比较，不是给论文打分。",
        "4. **没有人替你读过这篇稿子。** 交稿前请自己通读一遍，"
        "并请导师看一遍。",
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"
