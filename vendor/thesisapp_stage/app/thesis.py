"""一键生成：从一份开题报告到一份硕士论文初稿（.docx + 生成报告）。

## 这条链路做了什么

    开题报告.docx
      → ① 结构化解析（题目／章节树／研究方法／案例对象／创新点备忘／参考文献）
      → ② 建项目（把开题变成写作意图，而不是把开题当成稿件）
      → ③ 写作规划（按开题章节逐章检索证据 + 按章节职能分配字数）
      → ④ 逐章起草（每章按小节切成多个写作单元，只依据检索到的证据说话）
      → ⑤ 结论汇总（只依据已写正文，逐条标注依据章节）
      → ⑥ 参考文献表（角标按出现顺序连续化 + 生成文献表）
      → ⑦ 验证与自我更正（写作质量 + 引用一致性 + 可选的文献外部核验）
      → ⑧ 导出 .docx（规范排版、待办标记标红） + 生成报告

## 三个刻意的克制

1. **开题报告不会被当成稿件。** 它是规划与检索的输入，不是初稿。把开题正文
   塞进稿件会让整篇论文变成开题的扩写——那正是这类工具最常见的失败形态。
2. **创新点与致谢不生成。** 开题里的"拟创新之处"被提取出来供作者对照，
   但绝不进入生成；结论只做"正文已确立判断的汇总"，并整章标记待作者逐条确认。
3. **默认不开需要花钱的模型校验器。** 一次生成已经要几十次模型调用，
   验证阶段再全开会让成本翻倍。默认只跑本地检查器，``--llm-verify`` 才启用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .config import RUNS_DIR, Settings, ensure_dirs, load_settings
from .export.docx_writer import export_docx
from .framework.artifact import Stage
from .framework.blackboard import Blackboard
from .pipeline import STAGE_AGENTS, STAGE_ORDER, Runner, RunResult

__all__ = [
    "ThesisRequest",
    "ThesisResult",
    "DEFAULT_STAGES",
    "generate_thesis",
    "chapter_word_counts",
    "render_report",
]

#: 一键生成默认跑的阶段。
#:
#: 不默认包含的：
#: * ``gather`` —— 会扫描目录并触发入库，是个有副作用的动作，必须显式要求；
#: * ``dedup``  —— 降 AIGC 需要 aigc-reducer 在线，且它优化的是"AI 味指数"而不是
#:   内容本身，属于可选打磨；
#: * ``format`` —— 只报格式问题，没有稿件模板时跑它意义有限。
DEFAULT_STAGES: tuple[str, ...] = (
    Stage.RETRIEVE.value,
    Stage.DRAFT.value,
    Stage.VERIFY.value,
    Stage.FIX.value,
    Stage.FINAL.value,
)

#: 硕士论文的默认目标字数。取值参考中文学术型硕士的常见要求区间（3~5 万字）。
DEFAULT_WORD_TARGET = 35000


@dataclass
class ThesisRequest:
    """一次一键生成的输入。"""

    #: 开题报告路径（.docx/.txt/.md）。与 ``topic`` 至少给一个。
    proposal_path: str = ""
    title: str = ""
    author: str = ""
    topic: str = ""
    discipline: str = ""
    #: 论文类型（``app/writing/genres.py`` 的 id）。留空则按题目自动判一个。
    genre: str = ""
    #: **写作要求**（自由文本）。作者在这里写"采用问卷调查""样本不少于 300 份"
    #: "重点分析作用机制"这类具体指令，会逐条进到规划与起草的提示词里。
    requirement: str = ""
    #: 学位层次，只影响默认字数与提示词的语体期望。
    degree: str = "硕士"
    word_target: int = 0
    #: 作者手工指定的章节，形如 ``["绪论", "文献综述", ...]`` 或
    #: ``[{"title": "绪论", "children": ["研究背景", "研究意义"]}]``。给定时**覆盖**开题解析结果。
    sections: list[Any] = field(default_factory=list)
    #: **只写这几章**（白名单）。空表示全部。按标题片段匹配。
    chapters: list[str] = field(default_factory=list)
    #: **跳过这几章**（黑名单，优先于白名单）。用于"这几章我自己写"。
    skip_chapters: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    #: 输出路径。留空则写到 data/runs/<项目id>_论文初稿.docx
    out_path: str = ""
    #: 生成报告的路径。留空则与 out_path 同目录、同名前缀。
    report_path: str = ""
    stages: tuple[str, ...] = DEFAULT_STAGES
    export: bool = True
    llm_verify: bool = False
    max_rounds: int = 2
    #: 生成前是否先做知识库相关性预检（零成本）。默认做——
    #: 题目与知识库不匹配时，几十次模型调用会全部白花。
    precheck: bool = True
    #: 预检判定为"不相关"时是否仍然继续。默认不继续：宁可让作者先补资料，
    #: 也不要产出一篇引用着无关文献、看起来完整的稿子。
    force: bool = False
    #: 附加到项目 meta 的原始键值（供界面/脚本透传配置）。
    meta: dict[str, Any] = field(default_factory=dict)
    #: 本次使用哪个模型档案（``models.json`` 里的 id）。留空则用按用途分配的模型。
    model: str = ""


@dataclass
class ThesisResult:
    project_id: str = ""
    title: str = ""
    ok: bool = False
    seconds: float = 0.0
    brief: dict = field(default_factory=dict)
    precheck: dict = field(default_factory=dict)
    sections_planned: list[str] = field(default_factory=list)
    chapter_words: dict[str, int] = field(default_factory=dict)
    stage_summaries: list[dict] = field(default_factory=list)
    stage_failures: list[str] = field(default_factory=list)
    severity: dict = field(default_factory=dict)
    ledger: dict = field(default_factory=dict)
    issues_needing_author: list[dict] = field(default_factory=list)
    export: dict = field(default_factory=dict)
    report_path: str = ""
    proposals: list[str] = field(default_factory=list)
    blocked: str = ""
    """被预检闸门拦下时的原因。非空表示"没有开始花钱生成"。"""

    def as_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "title": self.title,
            "ok": self.ok,
            "blocked": self.blocked,
            "seconds": round(self.seconds, 2),
            "brief": self.brief,
            "precheck": self.precheck,
            "sections_planned": self.sections_planned,
            "chapter_words": self.chapter_words,
            "stages": self.stage_summaries,
            "stage_failures": self.stage_failures,
            "severity": self.severity,
            "ledger": self.ledger,
            "issues_needing_author": self.issues_needing_author,
            "export": self.export,
            "report_path": self.report_path,
            "next_steps": self.proposals,
        }


# ---------------------------------------------------------------------------
# 写作意图的构造
# ---------------------------------------------------------------------------

#: 没有开题报告时，各论文类型的默认章节结构在 ``app/writing/genres.py`` 里。
#: 这里只提供取用入口——**不再写死一套六章结构**，那套结构只适用于经管实证论文，
#: 套到工科或人文学科上会立刻不对（见 genres.py 的说明）。
def _fallback_sections(genre_id: str) -> list[dict[str, Any]]:
    from .writing.genres import get_genre

    return [dict(s) for s in get_genre(genre_id).default_sections]


def _clean_sections(raw: Sequence[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in raw or []:
        if isinstance(item, str):
            t = item.strip()
            if t:
                out.append({"title": t, "focus": "", "needs": "", "children": []})
            continue
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            continue
        kids = item.get("children") or item.get("subsections") or []
        if isinstance(kids, str):
            kids = [p for p in kids.replace("\n", "；").split("；") if p.strip()]
        out.append(
            {
                "title": title,
                "focus": str(item.get("focus") or "").strip(),
                "needs": str(item.get("needs") or "").strip(),
                "children": [str(k).strip() for k in kids if str(k).strip()],
            }
        )
    return out


def build_brief(req: ThesisRequest) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """构造写作意图。

    返回 ``(writing_brief, proposal_dict, warnings)``。

    章节结构的优先级：**作者显式指定 > 开题报告解析结果 > 论文类型的默认结构**。
    显式指定的优先，是因为作者临时改提纲是常态，工具不该坚持"开题里怎么写的就
    必须怎么写"。类型默认结构只在作者什么都没给时兜底——**它不是"通用模板"，
    而是该类型论文的通行骨架**。
    """
    from .writing.genres import GENRES, get_genre, guess_genre

    warnings: list[str] = []
    proposal: dict[str, Any] = {}
    brief: dict[str, Any] = {}

    if req.proposal_path:
        from .parsing.proposal import parse_proposal

        pb = parse_proposal(req.proposal_path, title_hint=req.title, author_hint=req.author)
        proposal = pb.as_dict()
        brief = pb.as_writing_brief()
        warnings.extend(pb.warnings)

    # ---- 论文类型 ----
    # 显式给的优先；没给就按题目猜一个（只是让默认结构更贴合，不是替作者决定）。
    if req.genre:
        genre = get_genre(req.genre)
        if genre.id != req.genre.strip().lower():
            warnings.append(f"未知的论文类型「{req.genre}」，已退回「{genre.name}」。")
    else:
        guess_src = str(req.title or brief.get("topic") or req.topic or "")
        genre = guess_genre(guess_src, req.requirement)
        if guess_src:
            warnings.append(
                f"论文类型按题目判定为「{genre.name}」。如果有误，用 --genre 指定"
                f"（可选：{'、'.join(GENRES)}）。"
            )
    brief["genre"] = genre.id
    brief["genre_name"] = genre.name

    explicit = _clean_sections(req.sections)
    if explicit:
        brief["sections"] = explicit
        brief["source"] = "作者指定"
        warnings.append(f"章节结构已按你指定的 {len(explicit)} 章覆盖。")
    elif not brief.get("sections"):
        brief["sections"] = _fallback_sections(genre.id)
        brief["source"] = f"{genre.name}默认结构"
        warnings.append(
            f"没有提供开题报告，已采用「{genre.name}」的默认章节结构（{len(brief['sections'])} 章）。"
            "你可以用 --section 直接指定自己的章节，或提供开题报告让它按你的框架写。"
        )

    # 题目：显式 > 开题 > 主题
    target_title = (req.title or brief.get("topic") or req.topic or "").strip()
    if req.title and req.title != brief.get("topic"):
        warnings.append(f"题目按你指定的「{req.title}」使用（开题里是「{brief.get('topic') or '未识别'}」）。")
    brief["topic"] = target_title
    brief["title"] = target_title

    # ---- 写作要求（自由文本，逐条进提示词）----
    if req.requirement:
        brief["requirement"] = req.requirement

    # ---- 章节选择：只写某几章 / 跳过某几章 ----
    if req.chapters:
        brief["chapters"] = [str(c).strip() for c in req.chapters if str(c).strip()]
        warnings.append(f"只写这些章：{'、'.join(brief['chapters'])}（其余章不生成）。")
    if req.skip_chapters:
        brief["skip_chapters"] = [str(c).strip() for c in req.skip_chapters if str(c).strip()]
        warnings.append(f"跳过这些章（保留稿件中已有内容）：{'、'.join(brief['skip_chapters'])}。")

    # 目标字数：作者给的优先；没给就用该论文类型的常见值。
    word_target = int(req.word_target or 0) or int(genre.suggested_words or DEFAULT_WORD_TARGET)
    brief["word_target"] = word_target
    brief["degree"] = req.degree or "硕士"
    if req.author:
        brief["author"] = req.author
    if req.keywords:
        brief["keywords"] = list(req.keywords)
    if req.discipline:
        brief["discipline"] = req.discipline
    if not target_title:
        warnings.append("没有题目：生成的内容会缺少统一主题，强烈建议指定题目。")
    return brief, proposal, warnings


# ---------------------------------------------------------------------------
# 字数统计与报告
# ---------------------------------------------------------------------------


def chapter_word_counts(text: str) -> dict[str, int]:
    """按 ``# 标题`` 统计每章的**中文字符数**。

    只数中文字符而不是全部字符：论文的字数要求通常按汉字计，
    把标点、数字、英文都算进去会让"已写 X 字"这个数字虚高，作者据此判断
    篇幅是否达标就会误判。
    """
    import re

    out: dict[str, int] = {}
    matches = list(re.finditer(r"^#[ \t]+(.+?)[ \t]*$", text or "", re.M))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[m.end() : end]
        out[m.group(1).strip()] = len(re.findall(r"[\u4e00-\u9fff]", chunk))
    return out


def _issue_rows(board: Blackboard, *, limit: int = 60) -> list[dict]:
    """待作者处理的问题清单（按严重度倒序）。"""
    rows: list[dict] = []
    for issue in board.project.issues.values():
        if issue.status in ("fixed", "wontfix"):
            continue
        rows.append(
            {
                "severity": issue.severity.value,
                "kind": issue.kind,
                "status": issue.status,
                "message": issue.message,
                "fix_hint": issue.fix_hint,
                "location": issue.location,
                "evidence": issue.evidence[:160],
            }
        )
    rows.sort(key=lambda r: (-{"blocker": 3, "major": 2, "minor": 1, "info": 0}.get(r["severity"], 0), r["kind"]))
    return rows[:limit]


_SEV_CN = {"blocker": "致命", "major": "重要", "minor": "轻微", "info": "提示"}


def render_report(
    *,
    board: Blackboard,
    model_routing: dict | None = None,
    req: ThesisRequest,
    brief: dict[str, Any],
    proposal: dict[str, Any],
    precheck: dict[str, Any] | None = None,
    run: RunResult | None,
    export: dict[str, Any],
    warnings: Sequence[str],
    seconds: float,
) -> str:
    """生成"这次生成到底产出了什么、还缺什么"的报告。

    这份报告与稿子同等重要：一份三万字初稿如果作者不知道"哪几章是在证据不足的
    情况下写的""哪几处是模型编过引用被删掉的"，他没法判断该从哪里核对起。
    """
    import time as _time

    title = brief.get("topic") or board.project.title
    counts = chapter_word_counts(board.draft_text)
    lines: list[str] = [
        f"# 生成报告 · {title}",
        "",
        f"- 项目：`{board.project.id}`",
        f"- 论文类型：{brief.get('genre_name') or brief.get('genre') or '（未指定）'}",
        f"- 生成时间：{_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 耗时：{seconds:.1f}s",
        f"- 正文字数（汉字）：{sum(counts.values())} 字，共 {len(counts)} 章",
        f"- 目标字数：{brief.get('word_target')} 字"
        + (
            f"（实际为目标的 {sum(counts.values()) * 100 // max(1, int(brief.get('word_target') or 1))}%）"
            if brief.get("word_target")
            else ""
        ),
        "",
    ]
    if brief.get("requirement"):
        lines += ["> **你的写作要求**（已逐条进到规划与起草的提示词）：", "", f"> {brief['requirement']}", ""]

    if warnings:
        lines += ["## 需要你先看一眼的提示", ""]
        lines += [f"- {w}" for w in warnings]
        lines.append("")

    if precheck:
        lines += [
            "## 知识库预检",
            "",
            f"- 判定：**{precheck.get('verdict')}**　相关 {precheck.get('relevant')}/"
            f"{precheck.get('total')} 条（占比 {precheck.get('ratio')}）",
            f"- 题目关键词：{'、'.join(precheck.get('keywords') or []) or '（未抽取到）'}",
            f"- 结论：{precheck.get('suggestion')}",
            "",
        ]
        for q in precheck.get("queries") or []:
            lines.append(f"  - `{q.get('query')}`：命中 {q.get('hits')} 条，相关 {q.get('relevant')} 条")
        lines.append("")

    if proposal:
        lines += [
            "## 开题报告解析结果",
            "",
            f"- 题目：{proposal.get('title') or '（未识别）'}",
            f"- 案例/研究对象：{proposal.get('case') or '（未识别）'}",
            f"- 章节：{len(proposal.get('chapters') or [])} 章",
            f"- 研究方法：{'、'.join(proposal.get('methods') or []) or '（未识别）'}",
            f"- 识别到的栏目：{'、'.join(proposal.get('regions_found') or []) or '（无）'}",
            f"- 未识别到的栏目：{'、'.join(proposal.get('regions_missing') or []) or '（无）'}",
            f"- 参考文献：{proposal.get('reference_count') or 0} 条（用于外部核验）",
            "",
        ]
        innov = proposal.get("innovations") or []
        if innov:
            lines += [
                f"### 开题里声明的创新点（{len(innov)} 条，**已原样保留，未参与生成**）",
                "",
                "创新点是作者的学术主张，系统不代写。请在写结论时自行对照补写：",
                "",
            ]
            lines += [f"{i}. {x}" for i, x in enumerate(innov, 1)]
            lines.append("")

    lines += ["## 各章写作情况", "", "| 章节 | 汉字数 |", "|---|---:|"]
    for name, n in counts.items():
        lines.append(f"| {name} | {n} |")
    lines.append("")

    if run is not None:
        lines += ["## 流水线各阶段", ""]
        for s in run.stages:
            if not s.ran:
                lines.append(f"- ⏭ `{s.stage}`：跳过（{s.reason}）")
            else:
                mark = "✅" if s.ok else "❌"
                lines.append(f"- {mark} `{s.stage}`：{s.summary}")
        lines.append("")
        led = run.ledger or {}
        if led:
            lines += [
                "## 模型调用与成本",
                "",
                f"- 调用 {led.get('calls', 0)} 次，失败 {led.get('failures', 0)} 次，"
                f"模型耗时 {led.get('seconds', 0)}s",
                f"- 提示 {led.get('prompt_chars', 0)} 字符 → 回复 {led.get('reply_chars', 0)} 字符",
                "",
            ]
            by_model = led.get("by_model") or {}
            if by_model:
                # 按用途分模型之后，"哪个模型被调了多少次"是作者最需要看的一项——
                # 它直接决定账单，也决定"正文是不是真的用了那个强模型"。
                lines += ["| 模型 | 调用次数 | 耗时(s) | 失败 |", "|---|---:|---:|---:|"]
                for name, v in sorted(by_model.items(), key=lambda kv: -kv[1]["calls"]):
                    lines.append(f"| `{name}` | {v['calls']} | {v['seconds']} | {v['failures']} |")
                lines.append("")
            by = led.get("by_purpose") or {}
            if by:
                lines += ["| 用途 | 次数 | 耗时(s) |", "|---|---:|---:|"]
                for k, v in sorted(by.items(), key=lambda kv: -kv[1]["calls"]):
                    lines.append(f"| {k} | {v['calls']} | {v['seconds']} |")
                lines.append("")
        routing = (model_routing or {}) if isinstance(model_routing, dict) else {}
        if routing:
            lines += ["### 本次每个用途使用的模型", ""]
            for role, info in routing.items():
                lines.append(f"- **{role}**（{info.get('label', '')}）：`{info.get('model', '')}` @ {info.get('base_url', '')}")
            lines.append("")

    sev = board.severity_counts()
    lines += [
        "## 问题统计",
        "",
        f"- 致命 {sev.get('blocker', 0)} · 重要 {sev.get('major', 0)} · "
        f"轻微 {sev.get('minor', 0)} · 提示 {sev.get('info', 0)}",
        f"- 已自动修复 {sev.get('fixed', 0)} · 待你处理 {sev.get('needs_author', 0)}",
        "",
    ]

    rows = _issue_rows(board)
    if rows:
        lines += [
            "## 待你处理的问题（按严重度）",
            "",
            "> 这些都是**需要作者判断**的问题，系统不会自动改写学术论断。",
            "",
        ]
        for r in rows:
            lines.append(f"### [{_SEV_CN.get(r['severity'], r['severity'])}] {r['kind']}")
            lines.append(f"{r['message']}")
            if r.get("evidence"):
                lines.append(f"- 位置/证据：{r['evidence']}")
            if r.get("fix_hint"):
                lines.append(f"- 怎么办：{r['fix_hint']}")
            lines.append("")
    else:
        lines += ["## 待你处理的问题", "", "没有遗留问题。**这不等于论文可以直接交**——", 
                  "系统检查的是写作规范与引用一致性，学术观点是否成立仍需你与导师判断。", ""]

    lines += [
        "## 系统没有做的事（请自行完成）",
        "",
        "1. **创新点与致谢**：系统完全不生成，需你本人撰写。",
        "2. **研究结论的确认**：结论章是依据正文汇总的结构草稿，每条都要你确认「这确实是我的意思」。",
        "3. **数据与案例的真实性**：稿中标注 `【待作者补充：…】` 的地方（在 Word 里为红色加粗）"
        "必须补上真实依据，或删除该句。",
        "4. **参考文献字段补全**：文献表缺期刊名、卷期、文献类型标识（[J]/[M]/[D]），需你按原文补全。",
        "5. **格式对齐学校模板**：导出的 .docx 用的是通行排版惯例，不是你们学校的具体模板。",
        "6. **查重与 AIGC 检测**：本系统不做这两件事，也无法预测检测结果。",
        "",
    ]

    if export:
        lines += [
            "## 导出文件",
            "",
            f"- 稿件：`{export.get('path')}`",
            f"- 段落 {export.get('paragraphs', 0)} 段（标题 {export.get('headings', 0)} 个），"
            f"文献 {export.get('references', 0)} 条，待办标记 {export.get('pending_marks', 0)} 处",
            "",
        ]
        for n in export.get("notes") or []:
            lines.append(f"- {n}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def generate_thesis(
    req: ThesisRequest,
    *,
    settings: Settings | None = None,
    runner: Runner | None = None,
    progress: Callable[[str, float], None] | None = None,
) -> ThesisResult:
    """跑完整条一键生成链路。"""
    t0 = time.time()
    ensure_dirs()
    settings = settings or load_settings()
    # `--model <档案id>`：只覆盖本次运行，不写回 models.json——
    # "这次想换个模型试试"和"以后都用这个"是两件事。
    if req.model:
        from .models import apply_override

        problem = apply_override(settings, req.model)
        if problem:
            raise ValueError(problem)
    runner = runner or Runner(settings=settings)

    result = ThesisResult()
    brief, proposal, warnings = build_brief(req)
    result.brief = brief
    result.title = brief.get("topic") or "未命名论文"

    if progress:
        progress(f"📖 写作意图：{len(brief.get('sections') or [])} 章", 0.02)

    # ---- 生成前预检：题目与知识库匹配吗？-----------------------------
    #
    # 一次生成是几十次模型调用。题目与知识库不匹配时那些调用**全部白花**，
    # 而且产出会被起草阶段的相关性闸门逐章拦下、最终得到一份空稿。
    # 预检是纯检索 + 本地判相关，成本为零，所以放在花钱之前。
    if req.precheck and result.title:
        from .kb import precheck as kb_precheck

        if progress:
            progress("🔎 预检知识库…（纯检索，不花钱）", 0.03)
        pre = kb_precheck(
            result.title,
            requirement=str(brief.get("requirement") or ""),
            settings=settings,
        )
        result.precheck = pre.as_dict()
        if not pre.usable and not req.force:
            reason = pre.suggestion()
            result.blocked = reason
            result.proposals = [
                "先把本主题的文献投进知识库：界面「知识库」页签，或用 `kb add 你的文献.pdf`。",
                "投完再跑 `kb check --topic \"你的题目\"` 确认预检通过。",
                "确认知识库已就绪后重新生成。",
                f"确实要用当前知识库生成（不建议），加 --force："
                f"相关章节会因缺证据而写不出来。",
            ]
            result.seconds = time.time() - t0
            if progress:
                progress("⛔ 预检未通过，已停止（未产生模型费用）", 1.0)
            return result
        if progress:
            progress(f"✅ 预检通过：{pre.relevant}/{pre.total} 条检索结果与题目相关", 0.04)

    meta: dict[str, Any] = dict(req.meta or {})
    meta["writing_brief"] = brief
    if result.precheck:
        from .writing.relevance import RELEVANCE_META_KEY

        meta[RELEVANCE_META_KEY] = result.precheck
    if proposal:
        meta["proposal"] = proposal
        if proposal.get("references_raw"):
            # 开题里的参考文献是**外部核验的宝贵输入**：它独立于生成过程，
            # 可以拿来核对"这篇论文引的文献是否真实存在"。
            meta["proposal_references"] = proposal["references_raw"]
    if req.author:
        meta["author"] = req.author

    # 建项目：稿件正文留空。开题报告是**输入**，不是初稿——
    # 把开题正文当作已有稿件，会让生成结果变成开题的扩写。
    board = runner.create_project(title=result.title, text="", meta=meta)
    result.project_id = board.project.id

    # 检索词：用题目 + 各章标题，供 rag:retrieve 阶段使用。
    queries = [result.title] + [str(s.get("title") or "") for s in (brief.get("sections") or [])]
    board.project.meta["retrieve_queries"] = [q for q in queries if q][:12]
    board.save()

    if progress:
        progress(f"🚀 开始生成（{'/'.join(req.stages)}）", 0.05)

    stages = tuple(req.stages or DEFAULT_STAGES)
    unknown = [s for s in stages if s not in STAGE_ORDER]
    if unknown:
        raise ValueError(f"未知阶段 {unknown}；可用：{list(STAGE_ORDER)}")

    run = runner.run(
        board.project.id,
        stages=stages,
        max_rounds=req.max_rounds,
        include_llm_verify=req.llm_verify,
        progress=progress,
    )
    result.stage_summaries = [s.as_dict() for s in run.stages]
    result.stage_failures = [s.stage for s in run.failures]
    result.severity = run.severity
    result.ledger = run.ledger

    # 重新载入：run 过程中稿件与问题都更新了，用最初那份 board 会读到旧状态。
    board = runner.load(board.project.id)
    # 本次每个用途实际会走哪个模型——写进报告，作者才看得见"正文到底用了哪个模型"。
    routing_info: dict = {}
    try:
        from .models import ModelRouter

        if getattr(settings, "models", None):
            routing_info = ModelRouter(settings.models, settings.llm).describe()
    except Exception:  # noqa: BLE001 - 报告缺一节不影响交付
        routing_info = {}
    result.sections_planned = [str(s.get("title") or "") for s in (brief.get("sections") or [])]
    result.chapter_words = chapter_word_counts(board.draft_text)
    result.issues_needing_author = _issue_rows(board)

    # ---- 导出 ----
    export_info: dict[str, Any] = {}
    if req.export and board.draft_text.strip():
        out = Path(req.out_path) if req.out_path else (RUNS_DIR / f"{board.project.id}_论文初稿.docx")
        if progress:
            progress(f"📄 导出 {out.name}", 0.9)
        ex = export_docx(
            board.draft_text,
            out,
            title=result.title,
            author=req.author or str(board.project.meta.get("author") or ""),
        )
        export_info = ex.as_dict()
    elif req.export:
        export_info = {"ok": False, "notes": ["稿件为空，未导出 .docx"], "path": ""}
    result.export = export_info

    # ---- 生成报告 ----
    report = render_report(
        board=board,
        model_routing=routing_info,
        req=req,
        brief=brief,
        proposal=proposal,
        precheck=result.precheck,
        run=run,
        export=export_info,
        warnings=warnings,
        seconds=time.time() - t0,
    )
    if req.report_path:
        rp = Path(req.report_path)
    elif export_info.get("path"):
        rp = Path(export_info["path"]).with_name(Path(export_info["path"]).stem + "_生成报告.md")
    else:
        rp = RUNS_DIR / f"{board.project.id}_生成报告.md"
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(report, encoding="utf-8")
    result.report_path = str(rp)

    result.seconds = time.time() - t0
    result.proposals = _next_steps(board, export_info)
    result.ok = bool(board.draft_text.strip()) and not result.stage_failures
    if progress:
        progress("✅ 完成", 1.0)
    return result


def _next_steps(board: Blackboard, export_info: dict[str, Any]) -> list[str]:
    """把"下一步该做什么"写成可执行的建议，而不是一句"请检查"。"""
    steps: list[str] = []
    counts = board.severity_counts()
    if counts.get("blocker"):
        steps.append(f"先处理 {counts['blocker']} 个致命问题（多为引用与依据缺失），再谈其它。")
    if counts.get("needs_author"):
        steps.append(f"逐条核对 {counts['needs_author']} 条待你处理的问题（见生成报告）。")
    if export_info.get("pending_marks"):
        steps.append(
            f"在 Word 里搜索红色加粗的【…】标记（共 {export_info['pending_marks']} 处），"
            "补上真实依据或删除该句。"
        )
    steps.append("结论章是汇总草稿，请逐条确认后再定稿；创新点与致谢请自行撰写。")
    steps.append("对照本校学位论文模板调整字体、字号、行距、页边距与文献表格式。")
    steps.append("最后自行完成查重与 AIGC 检测——本系统不做这两件事。")
    return steps
