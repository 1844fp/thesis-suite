"""格式检查器：把规则引擎接到框架的检查器注册表上。

框架的检查器契约是 ``fn(text: str, ctx) -> list[Issue]``——只给纯文本。
但格式问题的证据（字体、字号、行距、页边距）**根本不在纯文本里**。
这里用一条明确的通道解决这个矛盾：

    格式事实由收稿阶段读好后写进 ``ctx.board.project.meta["format_facts"]``
    （``DocxFormatFacts.as_dict()`` 的结果），检查器再从 dict 重建。

为什么不让检查器自己去开 .docx：检查器是纯函数式的（框架要求
"不产生副作用"），而且要能在没有文件的场景（纯文本稿、单测）里跑。
把"读文档"留在收稿阶段，检查器就只需要一份数据。

**没有 facts 时不猜。** 返回一条 INFO 说明"未提供 docx 结构，已跳过格式
检查"，而不是返回空列表。两者的区别很实际：空列表会让报告显示"格式
无问题"，而真相是"格式根本没检查"——在一份关于格式的报告里，
把"没查"说成"没问题"是最坏的一种错误。这条 INFO 的 ``fixable`` 为
假、``evidence`` 为空，因此永远不会驱动自动修改，只会作为一条提示
留在问题清单里。
"""

from __future__ import annotations

import re
from typing import Any

from ..framework.artifact import Issue, Severity, text_hash
from ..framework.registry import verifier
from ..parsing.docx_reader import DocxFormatFacts
from ..checks import _util
from .rules import DEFAULT_RULESET, RuleSet, evaluate, summarize_issues

#: 格式事实在 ``project.meta`` 里的键。写成常量而不是散落的字符串：
#: 收稿阶段写、检查器读，两边必须用同一个键，拼错一次就静默失效。
FORMAT_FACTS_KEY = "format_facts"

#: 纯文本层的「中文之间的多余空格」。
_TEXT_CJK_SPACE = re.compile(r"[\u4e00-\u9fff][ \t]+[\u4e00-\u9fff]")
#: 纯文本层的「半角标点夹在中文之间」。
_TEXT_HALF_PUNCT = re.compile(r"(?<=[\u4e00-\u9fff])([,;:!?])")
#: 纯文本层的「行尾空格」。
_TEXT_TRAILING_SPACE = re.compile(r"[ \t\u3000]+$", re.MULTILINE)


def _facts_from_meta(ctx) -> tuple[DocxFormatFacts | None, str]:
    """从上下文取回格式事实。

    返回 ``(facts, 说明)``：说明在取不到时用来解释原因。**不抛异常**——
    一个检查器不该因为元数据缺失就把整条流水线打断，那是收稿阶段的问题，
    应该在报告里说清楚而不是崩掉。
    """
    board = getattr(ctx, "board", None)
    project = getattr(board, "project", None)
    meta = getattr(project, "meta", None)
    if not isinstance(meta, dict):
        return None, "上下文中没有 project.meta"

    raw = meta.get(FORMAT_FACTS_KEY)
    if raw is None:
        return None, f"project.meta 里没有 {FORMAT_FACTS_KEY!r}（收稿阶段未提供 docx 结构）"
    if isinstance(raw, DocxFormatFacts):
        # 允许上游直接塞对象：省去一次序列化往返，单测里也更好写。
        return raw, ""
    if not isinstance(raw, dict):
        return None, f"{FORMAT_FACTS_KEY!r} 的类型是 {type(raw).__name__}，应为 dict 或 DocxFormatFacts"

    try:
        facts = DocxFormatFacts.from_dict(raw)
    except Exception as exc:  # noqa: BLE001 - 坏元数据不能拖垮整轮检查
        return None, f"{FORMAT_FACTS_KEY!r} 重建失败：{type(exc).__name__}: {exc}"
    if not facts.paragraphs:
        return None, f"{FORMAT_FACTS_KEY!r} 里没有任何段落（可能是空的或残缺的）"
    return facts, ""


def _ruleset_of(ctx) -> tuple[RuleSet, str]:
    """取这次要用的规则集。

    优先用 ``project.meta["format_ruleset"]``（学校模板规则），没有就用内置
    通用规范。**这就是接入学校官方 .docx 模板的入口**：模板规则由个人/院系
    提供，通过 meta 注入，检查器不需要知道它是哪来的。
    """
    board = getattr(ctx, "board", None)
    project = getattr(board, "project", None)
    meta = getattr(project, "meta", None)
    if isinstance(meta, dict):
        candidate = meta.get("format_ruleset")
        if isinstance(candidate, RuleSet):
            return candidate, "project.meta"
        if isinstance(candidate, dict):
            try:
                return RuleSet.from_dict(candidate), "project.meta"
            except Exception:  # noqa: BLE001 - 规则坏了退回内置规范
                return DEFAULT_RULESET, "内置通用规范（meta 里的规则集无法解析）"
        path = meta.get("format_ruleset_path")
        if isinstance(path, str) and path:
            try:
                return RuleSet.from_yaml(path), str(path)
            except Exception:  # noqa: BLE001
                return DEFAULT_RULESET, "内置通用规范（meta 里的规则文件读不了）"
    return DEFAULT_RULESET, "内置通用规范"


def _info_issue(message: str, *, evidence: str = "", location: dict | None = None) -> Issue:
    """构造一条 INFO 提示。

    ``fixable=False`` + 无 ``fix_op``：这类条目只用来解释"为什么没查"，
    绝不能驱动自动修改或变成需要作者动手的任务。
    """
    return Issue(
        id="",
        kind="format.not_checked",
        severity=Severity.INFO,
        message=message,
        location=dict(location or {"paragraph": -1}),
        evidence=evidence,
        detected_by="rules",
        confidence=1.0,
        fixable=False,
        fix_hint="",
    )


def _stale_issue(facts: DocxFormatFacts, text: str) -> Issue | None:
    """判断 facts 是否还对得上当前稿子。

    段落下标是读取那一刻编的。稿子在那之后被改过（哪怕只是标点规范化），
    下标就会整体漂移，报出来的"第 12 段"可能指向别的地方。与其指错地方，
    不如明确说"这次结果仅供参考"，并提示重新收稿。返回 None 表示没有
    证据表明过期（没有哈希的老数据不判过期——不猜）。
    """
    if not facts.source_text_hash:
        return None
    if facts.source_text_hash == text_hash(text):
        return None
    return _info_issue(
        "格式事实与当前稿件不一致（稿件在读取 docx 之后被修改过），"
        "本次格式检查的段落定位可能偏移；建议重新解析稿件后再查格式。",
        evidence=f"facts 哈希 {facts.source_text_hash} ≠ 当前稿件哈希 {text_hash(text)}",
        location={"paragraph": -1, "scope": "docx_stale"},
    )


@verifier(
    "check_format_docx",
    kinds=("draft",),
    needs_llm=False,
    description="按学校/通用格式规范检查 .docx 排版（字体、字号、行距、缩进、页边距、题注、参考文献形态、标点空格）",
    cost="local",
)
def check_format_docx(text: str, ctx) -> list[Issue]:
    """走完整 RuleSet 的格式检查。

    ``text`` 参数在本检查器里**不用来判格式**（格式事实来自 docx），只用
    来核对 facts 是否过期。这也是为什么本检查器严格依赖收稿阶段写进
    ``project.meta`` 的 facts：没有它就只能报告"未检查"。
    """
    facts, why = _facts_from_meta(ctx)
    if facts is None:
        return [_info_issue(f"未提供 docx 结构，跳过格式检查：{why}")]

    ruleset, source = _ruleset_of(ctx)
    stats: dict[str, Any] = {}
    issues = evaluate(facts, ruleset, stats=stats)

    stale = _stale_issue(facts, text or "")
    if stale is not None:
        issues.insert(0, stale)

    # 一条规则都没跑起来（规则集为空或全部 check 名写错）时必须说出来：
    # 否则"0 条问题"会被读成"格式全对"。
    if stats.get("rules_total") == 0:
        issues.append(
            _info_issue(
                f"规则集（{source}）里没有可执行的规则，格式检查实际未进行",
                location={"paragraph": -1, "scope": "ruleset"},
            )
        )
    for err in stats.get("errors") or []:
        issues.append(
            _info_issue(
                f"规则 {err.get('rule')} 执行失败，该项未检查：{err.get('error')}",
                location={"paragraph": -1, "rule": err.get("rule")},
            )
        )
    return issues


@verifier(
    "check_format_text",
    kinds=("draft",),
    needs_llm=False,
    description="纯文本层格式检查：中英文标点混用、中文间多余空格、行尾空格、连续空行（不依赖 docx）",
    cost="local",
)
def check_format_text(text: str, ctx) -> list[Issue]:
    """纯文本层检查。

    这一层存在的意义是**兜底**：论文可能以 Markdown/txt 形式进来，也可能
    docx 解析失败。标点、空格、空行这些问题只靠文本就能判定，没有理由
    因为拿不到 docx 就一并放弃。

    刻意**不**在这一层做"中英文字体""行距"之类的判断——纯文本里没有这些
    信息，任何判断都是编的。

    **三条必须绕开的坑（都是实测出来的"会改坏稿子"）**：

    1. **表格行**：纯文本里的表格行靠连续空格分列（``变量  均值  标准差``）。
       把列分隔空格当"汉字间多余空格"删掉，三行表格立刻塌成一串字，比不改更糟。
    2. **标题编号后的空格**：``第一章 绪论`` 里的空格是规范写法，删成
       ``第一章绪论`` 是不规范。这里复用 ``app.checks._util.heading_kind``
       ——它已经实现了"整行才算标题 + 长度上限"的判据，不另写一套，
       否则同一条文本在两个检查器眼里会切得不一样。
    3. **参考文献区**：GB/T 7714 的条目本来就写成
       ``刊名, 2020, 43(5): 1-10.``，半角逗号是规范而不是笔误。
       用 ``_util.mask_nonbody`` 把该区遮掉（等长占位，偏移不失真）。

    另外，只有**能保证唯一**的 ``find`` 才允许标 ``fixable``：``章 绪`` 这种
    短串在全文出现多次，``replace_span`` 会因为"定位不唯一"拒绝执行，
    或者被模型自行加长后改到别处。宁可标成需要人工处理，也不给出会改错地方的补丁。
    """
    text = text or ""
    issues: list[Issue] = []
    max_per_kind = 20

    def add(kind: str, severity: Severity, message: str, *, evidence: str, location: dict,
            fixable: bool = False, fix_op: str = "", hint: str = "") -> None:
        if sum(1 for i in issues if i.kind == kind) >= max_per_kind:
            return
        issues.append(
            Issue(
                id="",
                kind=kind,
                severity=severity,
                message=message,
                location=dict(location),
                evidence=evidence,
                detected_by="rules",
                confidence=1.0,
                fixable=fixable,
                fix_op=fix_op if fixable else "",
                fix_hint=hint,
            )
        )

    lines = text.split("\n")
    # 参考文献区用等长占位遮掉：偏移不变，所以后面照旧能用 lines[idx] 取证据。
    masked_lines = _util.mask_nonbody(text).split("\n")

    for idx, raw_line in enumerate(masked_lines, start=1):
        line = raw_line
        if not line.strip():
            continue
        original = lines[idx - 1] if idx - 1 < len(lines) else ""

        # --- 标点：参考文献区已被遮掉，正文里中文语境的半角标点才算问题 ---
        m = _TEXT_HALF_PUNCT.search(line)
        if m:
            add(
                "format.punct_halfwidth_in_cjk",
                Severity.MINOR,
                f"第 {idx} 行中英文标点混用：中文语境出现半角「{m.group(0)}」",
                evidence=_clip(original),
                location={"paragraph": idx - 1, "line": idx, "char": m.start(), "find": m.group(0)},
                fixable=True,
                fix_op="normalize_punctuation",
                hint="把中文语境里的半角标点改为全角",
            )

        # --- 汉字间空格：标题行与表格行都跳过 ---
        if not _is_heading_line(original) and not _is_table_row(original):
            m = _TEXT_CJK_SPACE.search(line)
            if m:
                find = _anchored(text, m.group(0), m.start())
                add(
                    "format.space_between_cjk",
                    Severity.MINOR,
                    f"第 {idx} 行中文之间有多余空格",
                    evidence=_clip(original),
                    location={"paragraph": idx - 1, "line": idx, "char": m.start(), "find": find},
                    fixable=bool(find),
                    fix_op="replace_span" if find else "",
                    hint="删除中文之间的空格"
                    if find
                    else "删除中文之间的空格（该片段在全文出现多次，需人工确认位置）",
                )

        # --- 行尾空格：任何行都可以删，两端无歧义，所以不受上面两条限制 ---
        m = _TEXT_TRAILING_SPACE.search(line)
        if m:
            find = _anchored(text, m.group(0) + "\n", m.start())
            add(
                "format.trailing_space",
                Severity.INFO,
                f"第 {idx} 行末尾有多余空格",
                evidence=_clip(original) + "␠",
                location={"paragraph": idx - 1, "line": idx, "char": m.start(), "find": find},
                fixable=bool(find),
                fix_op="replace_span" if find else "",
                hint="删除行尾空格",
            )

    # 连续空行：3 行以上才算问题。文本稿里两行空行是常见的段落分隔写法，
    # 把它当错误会淹没真正的问题。参考文献区里条目之间的空行也算排版问题，
    # 所以这里用原文而不是遮罩后的文本。
    blank_run = 0
    blank_start = 0
    for idx, line in enumerate(lines, start=1):
        if line.strip():
            if blank_run >= 3:
                add(
                    "format.empty_paragraph_run",
                    Severity.INFO,
                    f"第 {blank_start}–{blank_start + blank_run - 1} 行连续 {blank_run} 个空行",
                    evidence=f"连续 {blank_run} 个空行",
                    location={"paragraph": blank_start - 1, "line": blank_start, "count": blank_run},
                    fixable=False,
                    fix_op="",
                    hint="连续空行最多保留一个；该处定位在全文不唯一，需人工删除",
                )
            blank_run = 0
            continue
        if blank_run == 0:
            blank_start = idx
        blank_run += 1

    return issues


def _anchored(text: str, snippet: str, near: int) -> str:
    """为片段取一个**全文唯一**的锚点；做不到就返回空串。

    空串是"不能安全机械修复"的显式表达：调用方据此把 ``fixable`` 降为 False。
    返回原片段（哪怕它出现两次）比返回空串危险得多——``replace_span`` 会拒改，
    而模型会自己把 find 加长，加长成什么就不可控了。
    """
    return _util.anchor(text or "", snippet or "", near)


def _is_heading_line(line: str) -> bool:
    """整行是不是标题。

    复用 ``app.checks._util`` 的判据：它已经处理好"整行匹配 + 长度上限"，
    另写一份会让同一条文本在两个检查器眼里切成不同的段落。
    """
    return bool(_util.heading_kind(line))


def _is_table_row(line: str) -> bool:
    """整行是不是靠空格分列的表格行。

    实现放在 ``app.checks._util`` 而不是这里：文字卫生检查器（``check_whitespace``）
    也要用同一个判据保护表格。两处各写一份必然漂移，结果是同一张表被一个检查器
    保护、被另一个改坏。
    """
    return _util.is_table_row(line)


def _clip(text: str, limit: int = 80) -> str:
    t = (text or "").strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


def register_format_facts(project_meta: dict, facts: DocxFormatFacts, *, ruleset: RuleSet | None = None) -> dict:
    """把格式事实写进 ``project.meta`` 的辅助函数（供收稿阶段调用）。

    这不是检查器，放在这里是为了让"写入"和"读取"的键与形状由同一处定义：
    上游只要调它一次，检查器就能工作，不用两边各自记住 key 名和字段名。
    """
    project_meta[FORMAT_FACTS_KEY] = facts.as_dict()
    if ruleset is not None:
        project_meta["format_ruleset"] = ruleset.to_dict()
        project_meta["format_ruleset_source"] = ruleset.source or ruleset.name
    return project_meta


__all__ = [
    "FORMAT_FACTS_KEY",
    "check_format_docx",
    "check_format_text",
    "register_format_facts",
    "summarize_issues",
]
