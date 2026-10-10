"""格式规则引擎（YAML 驱动）。

设计出发点只有一条：**规则必须是数据，不能是代码里的 if**。

理由有两条，都很实际：

1. 学位论文的格式规范是**按学校、按年份**发的红头文件，版本一年一变。
   把"正文小四宋体、行距 1.5 倍、首行缩进 2 字符"写死在 Python 里，
   换一个学校就得改代码、重新测试、重新部署。
2. 作者和导师看得懂 YAML，看不懂检查器注册表。规则外置之后，"我们学校
   要求的页边距是上下 2.5"这句话可以直接改在文件里并被验证。

规则引擎本身是纯函数：``evaluate(facts, ruleset) -> list[Issue]``。
输入是 ``parsing.docx_reader`` 产出的格式事实，输出是框架的 ``Issue``，
不读文件、不联网、不调模型——所以它能被完整单测，也就能在 CI 里跑。

**一条重要的边界：算子改不了格式属性。**
框架的修复算子（``framework.ops``）都是纯文本变换：替换、删除、重编号、
标点规范化。它们改得了错别字和标点，**改不了字体、字号、行距、页边距、
缩进**——那些是 OOXML 属性，不是文本。所以：
``fixable=True`` 只给纯文本类问题（标点、空格、空行、手动换行），
属性类问题一律 ``fixable=False`` 并给出 ``fix_hint``，交给人工或后续的
格式写回模块。把属性问题标成 fixable 会让修复器造出一个"执行失败"的补丁，
把真正的问题从待办里挤掉——这比不修更糟。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, Iterable

from ..framework.artifact import Issue, Severity, severity_from
from ..parsing.docx_reader import (
    CJK_SPACE_CJK_RE,
    CURLY_QUOTE_RE,
    LATIN_RE,
    MISSING_CJK_LATIN_SPACE_RE,
    REF_NUMBER_RE,
    REF_YEAR_RE,
    DocxFormatFacts,
    Paragraph,
    short_evidence,
)

#: 每条规则在一次检查里最多报几条。一份格式全错的稿子可能命中上千处，
#: 全量上报会把问题清单淹掉，真正重要的几条反而看不见。
MAX_ISSUES_PER_RULE = 20

#: 规则可以作用的范围。与段落 ``kind`` 不是一一对应：``section`` 是页面级，
#: ``caption`` 同时覆盖 ``kind == "caption"`` 的题注段落。
VALID_SCOPES = frozenset(
    {
        "body",
        "heading",
        "heading1",
        "heading2",
        "heading3",
        "caption",
        "figure_caption",
        "table_caption",
        "reference",
        "section",
        "paragraph",
        "text",
        "note",
        "any",
    }
)

#: 只允许用 ``framework.ops`` 里注册过的算子。写错算子名不会在加载时报错，
#: 而是在修复阶段静默失败，所以这里在加载时就校验。
ALLOWED_FIX_OPS = frozenset(
    {
        "replace_span",
        "delete_span",
        "renumber_sequence",
        "normalize_punctuation",
        "strip_filler",
        "unify_term",
        "insert_after",
        "merge_duplicate_paragraphs",
        "apply_edits",
    }
)


# --------------------------------------------------------------------------
# 规则与规则集
# --------------------------------------------------------------------------
@dataclass
class FormatRule:
    """一条格式规则。

    ``expected`` 是**数据**而不是自然语言描述，因为要做机检；人话写在
    ``hint`` 里。两者分开是为了让"机检"和"给人看的说明"互不牵制：
    想改阈值不用改提示语，想改提示语不影响判定。
    """

    id: str
    title: str = ""
    severity: Severity = Severity.MINOR
    scope: str = "body"
    check: str = ""
    #: 期望值。形状由 check 决定，见 ``rules_default.yaml`` 的注释。
    expected: Any = None
    tolerance: float = 0.0
    #: 特殊规则参数（正则、例外清单等）。与 expected 分开，避免把"参数"
    #: 混进"期望值"里导致提示语生成要认识每个 check 的参数含义。
    params: dict[str, Any] = field(default_factory=dict)
    hint: str = ""
    fixable: bool = False
    fix_op: str = ""
    enabled: bool = True
    source: str = ""
    """规则出处，例如「XX 大学研究生院 2023 年格式规定 3.2 条」。

    记出处不是为了好看：学生被要求改格式时第一个问题就是"凭什么"，
    答不上来的规则不该进检查器。
    """

    def as_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, raw: dict) -> "FormatRule":
        """从 dict 构造，未知键忽略、缺省键用默认值。

        YAML 是给人手改的，多写一个字段就报错会让人不敢改。真正的校验留给
        ``RuleSet.validate()``，那里能一次性列出全部问题。
        """
        raw = dict(raw or {})
        raw["severity"] = severity_from(raw.get("severity", "minor"))
        if isinstance(raw.get("params"), dict):
            raw["params"] = dict(raw["params"])
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})


@dataclass
class RuleSet:
    """一组规则 + 元信息。

    ``meta`` 里放学校名、模板版本、来源文件。这些字段不参与判定，但必须
    跟着规则一起走：半年后回头看一份报告，得能回答"当时是按哪一版规范
    判的"。规则文件会改，报告不会。
    """

    name: str = ""
    school: str = ""
    template_version: str = ""
    source: str = ""
    notes: list[str] = field(default_factory=list)
    rules: list[FormatRule] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- 构造 -------------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict) -> "RuleSet":
        raw = dict(raw or {})
        meta = dict(raw.get("meta") or {})
        rules = [FormatRule.from_dict(r) for r in (raw.get("rules") or [])]
        return cls(
            name=str(raw.get("name") or meta.get("name") or ""),
            school=str(raw.get("school") or meta.get("school") or ""),
            template_version=str(
                raw.get("template_version") or meta.get("template_version") or ""
            ),
            source=str(raw.get("source") or meta.get("source") or ""),
            notes=[str(n) for n in (raw.get("notes") or [])],
            rules=rules,
            meta=meta,
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "school": self.school,
            "template_version": self.template_version,
            "source": self.source,
            "notes": list(self.notes),
            "rules": [r.as_dict() for r in self.rules],
        }

    def to_yaml(self) -> str:
        """序列化成 YAML 文本。

        ``allow_unicode=True`` 是必须的：规则里全是中文，转义成 ``\\uXXXX``
        之后这份文件就没法手改了，而"能手改"正是规则外置的全部意义。
        """
        import yaml

        return yaml.safe_dump(
            self.to_dict(), allow_unicode=True, sort_keys=False, default_flow_style=False
        )

    @classmethod
    def from_yaml(cls, path: str | Path) -> "RuleSet":
        """从 YAML 文件加载规则集。

        读不动就抛 ``RuleSetError`` 并带上文件名与原因。静默返回空规则集
        等于"这份稿子格式全对"，是最危险的一种失败。
        """
        import yaml

        p = Path(path)
        if not p.exists():
            raise RuleSetError(f"规则文件不存在：{p}")
        try:
            raw = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise RuleSetError(f"规则文件 {p.name} 的 YAML 语法有误：{exc}") from exc
        except OSError as exc:
            raise RuleSetError(f"无法读取规则文件 {p}: {exc}") from exc
        if not isinstance(raw, dict):
            raise RuleSetError(
                f"规则文件 {p.name} 的顶层结构不是键值映射（读到 {type(raw).__name__}）"
            )
        rs = cls.from_dict(raw)
        rs.meta.setdefault("source_path", str(p))
        return rs

    # -- 查询 -------------------------------------------------------------
    def enabled_rules(self) -> list[FormatRule]:
        return [r for r in self.rules if r.enabled]

    def by_id(self, rule_id: str) -> FormatRule | None:
        for r in self.rules:
            if r.id == rule_id:
                return r
        return None

    def for_scope(self, scope: str) -> list[FormatRule]:
        return [r for r in self.enabled_rules() if r.scope == scope]

    def describe(self) -> list[dict]:
        return [
            {
                "id": r.id,
                "title": r.title,
                "severity": r.severity.value,
                "scope": r.scope,
                "check": r.check,
                "fixable": r.fixable,
                "fix_op": r.fix_op,
                "enabled": r.enabled,
            }
            for r in self.rules
        ]

    # -- 校验 -------------------------------------------------------------
    def validate(self) -> list[str]:
        """返回问题清单（空列表表示这份规则集可用）。

        把校验做成显式方法而不是构造时抛异常，是因为规则文件是手写的：
        应该能一次看到全部错误，而不是改一个跑一次。
        """
        problems: list[str] = []
        seen: set[str] = set()
        if not self.rules:
            problems.append("规则集里没有任何规则")
        for r in self.rules:
            where = f"规则 {r.id or '(缺少 id)'}"
            if not r.id:
                problems.append("有一条规则缺少 id")
            elif r.id in seen:
                problems.append(f"规则 id 重复：{r.id}")
            seen.add(r.id)
            if r.scope not in VALID_SCOPES:
                problems.append(f"{where}: 未知 scope={r.scope!r}，可选 {sorted(VALID_SCOPES)}")
            if r.check not in _CHECKS:
                problems.append(
                    f"{where}: 未实现的 check={r.check!r}，已实现 {sorted(_CHECKS)}"
                )
            if r.fixable and not r.fix_op:
                problems.append(f"{where}: fixable=True 但没有 fix_op")
            if r.fix_op and r.fix_op not in ALLOWED_FIX_OPS:
                problems.append(
                    f"{where}: fix_op={r.fix_op!r} 不在框架已注册的算子里 {sorted(ALLOWED_FIX_OPS)}"
                )
            if r.fixable and r.fix_op in ALLOWED_FIX_OPS:
                props = _PROPERTY_ONLY_CHECKS
                if r.check in props:
                    problems.append(
                        f"{where}: check={r.check!r} 是 OOXML 属性类问题，"
                        "纯文本算子改不了，必须 fixable=False"
                    )
            if not r.fixable and not r.hint:
                problems.append(f"{where}: 不可自动修复的规则必须给 hint（人工修改指引）")
        return problems


class RuleSetError(RuntimeError):
    """规则文件读不了。消息面向使用者，说明是哪个文件、哪里不对。"""


# --------------------------------------------------------------------------
# 检查器实现
# --------------------------------------------------------------------------
_CHECKS: dict[str, Callable[[DocxFormatFacts, FormatRule, "EvalContext"], list[Issue]]] = {}

#: 这些 check 检查的是 OOXML 属性，纯文本算子无法修复。``validate()`` 用它
#: 拦住"把字体问题标成可自动修复"这类错误——一旦标错，修复阶段会产出一个
#: 必然失败的补丁，把真正可修的问题挤出这一轮。
_PROPERTY_ONLY_CHECKS = frozenset(
    {
        "body_cjk_font",
        "body_latin_font",
        "body_font_size",
        "body_line_spacing",
        "body_first_line_indent",
        "heading_cjk_font",
        "heading_latin_font",
        "heading_font_size",
        "heading_bold",
        "heading_alignment",
        "heading_spacing",
        "page_size",
        "page_margin",
        "page_default_font",
        "caption_font_size",
    }
)


@dataclass
class EvalContext:
    """evaluate 过程中共享的一点状态。

    目前只有"已上报条数"这一项统计，但它是必要的：报告要给作者看
    "这一条规则命中了 137 处，只列了前 20 处"，否则作者会以为只错了 20 处。
    """

    per_rule: dict[str, int] = field(default_factory=dict)
    skipped: list[dict] = field(default_factory=list)

    def bump(self, rule_id: str) -> int:
        n = self.per_rule.get(rule_id, 0) + 1
        self.per_rule[rule_id] = n
        return n

    def note_skip(self, rule_id: str, reason: str) -> None:
        self.skipped.append({"rule": rule_id, "reason": reason})


def _check(name: str):
    def deco(fn):
        if name in _CHECKS:
            raise ValueError(f"检查实现名重复: {name}")
        _CHECKS[name] = fn
        return fn

    return deco


def _issue(rule: FormatRule, message: str, *, evidence: str, location: dict) -> Issue:
    """构造框架 Issue。

    ``kind`` 统一前缀 ``format.``，让界面能把格式问题和学术问题分开呈现；
    ``detected_by`` 留 ``rules``，由 ``VerifierAgent`` 覆写成
    ``rules:<检查器名>``，这样报告能回答"这条是哪个检查器报的"。
    """
    return Issue(
        id="",
        kind=f"format.{rule.id}",
        severity=rule.severity,
        message=message,
        location=dict(location),
        evidence=evidence,
        detected_by="rules",
        confidence=1.0,
        fixable=rule.fixable,
        fix_op=rule.fix_op if rule.fixable else "",
        fix_hint=rule.hint,
    )


# -- 取值助手 ---------------------------------------------------------------
def _para_font(p: Paragraph) -> tuple[str, str]:
    """段落级 (ascii, eastAsia) 字体。

    段落级可能为空（字体只写在 run 上），所以调用方要准备 run 级兜底。
    """
    return p.ascii_font, p.east_asia_font


def _run_fonts(p: Paragraph) -> list[tuple[str, str]]:
    """段落内所有非空 run 的 (ascii, eastAsia) 字体，去重。

    用 run 级字体检查是必要的：Word 的实际显示效果由 run 决定。作者手工
    选中几个字改字体是最常见的操作，段落级属性看不出来。
    只对**非空 run** 检查——Word 会在段落末尾留一个空 run，它继承的字体
    常与正文不同，拿它报错就是误报。
    """
    out: list[tuple[str, str]] = []
    for r in p.runs:
        if not (r.text or "").strip():
            continue
        pair = (r.font_name_ascii, r.font_name_eastasia)
        if pair not in out:
            out.append(pair)
    return out


def _font_of(p: Paragraph, which: str) -> str:
    """段落生效的字体：段落级为空时退回 run 级。"""
    ascii_f, ea_f = _para_font(p)
    if which == "ea":
        if ea_f:
            return ea_f
        for _a, e in _run_fonts(p):
            if e:
                return e
        return ""
    if ascii_f:
        return ascii_f
    for a, _e in _run_fonts(p):
        if a:
            return a
    return ""


def _norm(value: Any) -> str:
    """字体名比较用的归一化。

    Word 的字体名带 ``@`` 前缀表示"旋转 90 度"，比较时应视为同一字体；
    前后空格在 XML 里合法但无意义。不做归一化会把"@宋体"报成字体错误。
    """
    return str(value or "").strip().lstrip("@").strip()


def _fnum(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close(actual: float | None, expected: Any, tolerance: float) -> bool:
    exp = _fnum(expected)
    if actual is None or exp is None:
        return False
    return abs(actual - exp) <= max(tolerance, 1e-6)


def _body_paras(facts: DocxFormatFacts) -> list[Paragraph]:
    return facts.body_paragraphs


def _heading_paras(facts: DocxFormatFacts, rule: FormatRule) -> list[Paragraph]:
    """按 scope 取对应级别的标题。

    ``scope='heading'`` 时用 ``params["outline_level"]`` 限定级别；
    没写就取全部标题。级别取 ``outline_level``，取不到时按样式名/ID 里的
    数字兜底（``标题 2`` / ``Heading 2``）。
    """
    heads = facts.headings
    want = rule.params.get("outline_level")
    if rule.scope.startswith("heading") and rule.scope != "heading":
        suffix = rule.scope[len("heading") :]
        if suffix.isdigit():
            want = int(suffix) - 1
    if want is None:
        return heads
    want = int(want)
    return [h for h in heads if _heading_level(h) == want]


def _heading_level(p: Paragraph) -> int | None:
    """标题级别（0 基）。

    outlineLvl 最可靠；没有它就从样式名/ID 里抠数字，例如「标题 2」→ 1。
    """
    if p.outline_level is not None:
        return p.outline_level
    for text in (p.style_name, p.style_id):
        m = re.search(r"(\d+)", text or "")
        if m:
            try:
                return max(0, int(m.group(1)) - 1)
            except ValueError:
                continue
    return None


def _captions(facts: DocxFormatFacts, kind: str) -> list[Paragraph]:
    return [c for c in facts.captions if c.caption_label == kind]


def _nearest_image(facts: DocxFormatFacts, index: int, window: int = 3) -> int | None:
    """段落附近最近的含图段落下标。

    只看 ±window 段：跨半页去找一张图，找到的很可能不是这张图的图题，
    据此报"图题位置不对"就是误报。
    """
    for d in range(1, window + 1):
        for i in (index - d, index + d):
            if 0 <= i < len(facts.paragraphs) and facts.paragraphs[i].has_image:
                return i
    return None


def _nearest_table_boundary(facts: DocxFormatFacts, index: int) -> int | None:
    """预留：定位表格在段落流中的位置。

    目前返回 None 是**有意的**：python-docx 的 ``document.paragraphs``
    跳过表格，表格在段落序列里没有下标，靠"表题前后几段"猜位置会造出
    无法复核的问题。等格式写回模块能给出表格锚点后，这里再接上，
    ``caption_position`` 就能一并检查"表题在表上"。
    """
    return None


# -- 正文规则 ---------------------------------------------------------------
def _body_font_check(facts, rule, ctx, which: str) -> list[Issue]:
    """正文中文字体 / 西文字体。

    两类都按 **run 级优先** 判：段落级属性为空（字体只写在 run 上）时
    退回 run；run 级有多个不同字体时，只要有一个不等于期望就报，
    因为那意味着作者手动改了局部字体，正是评审会圈出来的东西。
    """
    expected = rule.expected or {}
    want = _norm(expected.get("eastAsia" if which == "ea" else "ascii") or expected.get("font"))
    if not want:
        ctx.note_skip(rule.id, "规则没有给出期望字体（expected.eastAsia/ascii）")
        return []

    out: list[Issue] = []
    for p in _body_paras(facts):
        run_fonts = _run_fonts(p)
        offenders = []
        if run_fonts:
            for a, e in run_fonts:
                actual = _norm(e if which == "ea" else a)
                if actual and actual != want:
                    offenders.append(actual)
        else:
            actual = _norm(_font_of(p, which))
            if actual and actual != want:
                offenders.append(actual)
        if not offenders:
            continue
        actual_desc = "、".join(sorted(set(offenders)))
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        label = "中文字体" if which == "ea" else "西文字体"
        out.append(
            _issue(
                rule,
                f"正文{label}为「{actual_desc}」，应为「{want}」",
                evidence=short_evidence(p.text),
                location={
                    "paragraph": p.index,
                    f"{'eastAsia' if which == 'ea' else 'ascii'}_font": actual_desc,
                    "expected": want,
                },
            )
        )
    return out


@_check("body_cjk_font")
def check_body_cjk_font(facts, rule, ctx) -> list[Issue]:
    return _body_font_check(facts, rule, ctx, "ea")


@_check("body_latin_font")
def check_body_latin_font(facts, rule, ctx) -> list[Issue]:
    return _body_font_check(facts, rule, ctx, "ascii")


@_check("body_font_size")
def check_body_font_size(facts, rule, ctx) -> list[Issue]:
    """正文字号。

    段落级字号没读到（None）时**不报**。读不到只有两种可能：文档确实没写
    （此时 Word 用默认值，而我们已经解析过默认值，不该出现 None），或者
    解析有缺口。报"字号为空"对作者毫无价值，还可能掩盖真正的解析 bug，
    所以这里选择跳过并记入 ``ctx.skipped``。
    """
    out: list[Issue] = []
    for p in _body_paras(facts):
        sizes = sorted({r.size_pt for r in p.runs if (r.text or "").strip() and r.size_pt})
        actual = sizes[0] if sizes else p.size_pt
        if actual is None:
            ctx.note_skip(rule.id, f"第 {p.index} 段没有解析到字号")
            continue
        # 段内有多种字号：说明作者局部改过字号，逐个报出来更有用。
        bad = [s for s in sizes if not _close(s, rule.expected, rule.tolerance)]
        if sizes and not bad:
            continue
        if not sizes and _close(actual, rule.expected, rule.tolerance):
            continue
        actual_desc = "、".join(f"{s:g}" for s in (bad or [actual]))
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"正文字号为 {actual_desc} 磅，应为 {_fnum(rule.expected) or 0:g} 磅",
                evidence=short_evidence(p.text),
                location={"paragraph": p.index, "size_pt": actual_desc, "expected": rule.expected},
            )
        )
    return out


@_check("body_line_spacing")
def check_body_line_spacing(facts, rule, ctx) -> list[Issue]:
    """正文行距。

    ``expected`` 支持两种写法，因为学校规定本身就分两种：
    固定值（``{mode: fixed, pt: 22}``，中文论文常用 22 磅）与
    倍数（``{mode: multiple, value: 1.5}``）。只支持一种会让另一种
    整篇误报。
    """
    exp = rule.expected or {}
    mode = str(exp.get("mode") or "multiple")
    value = _fnum(exp.get("value", exp.get("pt")))
    if value is None:
        ctx.note_skip(rule.id, "规则没有给出期望行距（expected.value/pt）")
        return []
    out: list[Issue] = []
    for p in _body_paras(facts):
        if not p.line_spacing_mode:
            ctx.note_skip(rule.id, f"第 {p.index} 段没有解析到行距")
            continue
        if p.line_spacing_mode == mode and _close(p.line_spacing_value, value, rule.tolerance):
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        actual = (
            f"{p.line_spacing_value:g} 倍"
            if p.line_spacing_mode == "multiple" and p.line_spacing_value is not None
            else f"{p.line_spacing_value:g} 磅（{'固定值' if p.line_spacing_mode == 'fixed' else p.line_spacing_mode}）"
            if p.line_spacing_value is not None
            else p.line_spacing_mode
        )
        want = f"{value:g} 倍" if mode == "multiple" else f"{value:g} 磅固定值"
        out.append(
            _issue(
                rule,
                f"正文行距为 {actual}，应为 {want}",
                evidence=short_evidence(p.text),
                location={
                    "paragraph": p.index,
                    "line_spacing": f"{p.line_spacing_mode}:{p.line_spacing_value}",
                    "expected": f"{mode}:{value}",
                },
            )
        )
    return out


@_check("body_first_line_indent")
def check_body_first_line_indent(facts, rule, ctx) -> list[Issue]:
    """正文首行缩进。

    中文规范是"首行缩进 2 字符"。同时接受两种存法：
    ``w:firstLineChars="200"``（百分之一字符）与 ``w:firstLine="480"``
    （twips，按小四 12 磅算 2 字符 ≈ 24 磅）。只认字符会被用磅值实现的
    文档整篇误报。
    """
    want_chars = _fnum((rule.expected or {}).get("chars") if isinstance(rule.expected, dict) else rule.expected)
    if want_chars is None:
        ctx.note_skip(rule.id, "规则没有给出期望缩进（expected.chars）")
        return []
    pt_per_char = _fnum(rule.params.get("pt_per_char")) or (rule.params.get("font_size_pt") or 12.0) * 1.0
    want_pt = want_chars * float(pt_per_char)
    out: list[Issue] = []
    for p in _body_paras(facts):
        chars, pt = p.first_line_indent_chars, p.first_line_indent_pt
        if chars is None and pt is None:
            if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
                continue
            out.append(
                _issue(
                    rule,
                    f"正文段落没有设置首行缩进，应为 {want_chars:g} 字符",
                    evidence=short_evidence(p.text),
                    location={"paragraph": p.index, "first_line_indent": "未设置"},
                )
            )
            continue
        if chars is not None and _close(chars, want_chars, rule.tolerance):
            continue
        if chars is None and pt is not None and _close(pt, want_pt, max(rule.tolerance, 0.5)):
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        actual = f"{chars:g} 字符" if chars is not None else f"{pt:g} 磅"
        out.append(
            _issue(
                rule,
                f"正文首行缩进为 {actual}，应为 {want_chars:g} 字符",
                evidence=short_evidence(p.text),
                location={"paragraph": p.index, "first_line_indent": actual},
            )
        )
    return out


@_check("body_alignment")
def check_body_alignment(facts, rule, ctx) -> list[Issue]:
    """正文对齐方式。

    为什么要有这条：中文论文规范几乎都要求正文**两端对齐**（``w:jc=both``），
    而写回方案里一直有这个字段却**没有对应的检查器**——那就成了"改了却没法验证"
    的静默改动，正是本项目最不想要的。补上检查器，改动才可复现：
    "这一段对齐不对"能被报出来，"改完对不对"能再跑一次确认。

    ``expected`` 可以是单个值或列表（如 ``["justify", "left"]``）。
    """
    raw = rule.expected
    if isinstance(raw, dict):
        raw = raw.get("align")
    allowed = {str(x) for x in raw} if isinstance(raw, (list, tuple, set)) else {str(raw)}
    allowed.discard("")
    if not allowed:
        ctx.note_skip(rule.id, "规则没有给出期望对齐方式（expected.align）")
        return []
    out: list[Issue] = []
    for p in _body_paras(facts):
        if p.align in allowed:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"正文对齐方式为「{p.align}」，应为「{'/'.join(sorted(allowed))}」",
                evidence=short_evidence(p.text),
                location={"paragraph": p.index, "align": p.align, "expected": sorted(allowed)},
            )
        )
    return out


# -- 标题规则 ---------------------------------------------------------------
def _heading_font_check(facts, rule, ctx, which: str) -> list[Issue]:
    expected = rule.expected or {}
    want = _norm(expected.get("eastAsia" if which == "ea" else "ascii") or expected.get("font"))
    if not want:
        ctx.note_skip(rule.id, "规则没有给出期望字体")
        return []
    label = "中文" if which == "ea" else "西文"
    out: list[Issue] = []
    for h in _heading_paras(facts, rule):
        run_fonts = _run_fonts(h)
        actual = ""
        if run_fonts:
            vals = {_norm(e if which == "ea" else a) for a, e in run_fonts}
            vals.discard("")
            if vals and vals == {want}:
                continue
            actual = "、".join(sorted(vals))
        else:
            actual = _norm(_font_of(h, which))
            if actual == want:
                continue
            if not actual:
                ctx.note_skip(rule.id, f"标题段 {h.index} 没有解析到{label}字体")
                continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        level = _heading_level(h)
        level_desc = f"{level + 1} 级标题" if level is not None else "标题"
        out.append(
            _issue(
                rule,
                f"{level_desc}{label}字体为「{actual or '未设置'}」，应为「{want}」",
                evidence=short_evidence(h.text),
                location={"paragraph": h.index, "expected": want, "actual": actual},
            )
        )
    return out


@_check("heading_cjk_font")
def check_heading_cjk_font(facts, rule, ctx) -> list[Issue]:
    return _heading_font_check(facts, rule, ctx, "ea")


@_check("heading_latin_font")
def check_heading_latin_font(facts, rule, ctx) -> list[Issue]:
    return _heading_font_check(facts, rule, ctx, "ascii")


@_check("heading_font_size")
def check_heading_font_size(facts, rule, ctx) -> list[Issue]:
    out: list[Issue] = []
    for h in _heading_paras(facts, rule):
        sizes = sorted({r.size_pt for r in h.runs if (r.text or "").strip() and r.size_pt})
        actual = sizes[0] if sizes else h.size_pt
        if actual is None:
            ctx.note_skip(rule.id, f"标题段 {h.index} 没有解析到字号")
            continue
        if _close(actual, rule.expected, rule.tolerance):
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        level = _heading_level(h)
        level_desc = f"{level + 1} 级标题" if level is not None else "标题"
        out.append(
            _issue(
                rule,
                f"{level_desc}字号为 {actual:g} 磅，应为 {_fnum(rule.expected) or 0:g} 磅",
                evidence=short_evidence(h.text),
                location={"paragraph": h.index, "size_pt": actual, "expected": rule.expected},
            )
        )
    return out


@_check("heading_bold")
def check_heading_bold(facts, rule, ctx) -> list[Issue]:
    """标题加粗。

    只判"该粗而不粗"，不判"不该粗却粗了"——后者极少见，而"标题不粗"
    是评审一眼就能看出来的硬伤。少报一条比多报十条假问题划算。
    """
    want = bool(rule.expected)
    if not want:
        ctx.note_skip(rule.id, "规则 expected 为假，本检查只处理『应当加粗』")
        return []
    out: list[Issue] = []
    for h in _heading_paras(facts, rule):
        run_bold = [r.bold for r in h.runs if (r.text or "").strip()]
        if any(b for b in run_bold) or h.bold:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        level = _heading_level(h)
        level_desc = f"{level + 1} 级标题" if level is not None else "标题"
        out.append(
            _issue(
                rule,
                f"{level_desc}未加粗，应为加粗",
                evidence=short_evidence(h.text),
                location={"paragraph": h.index, "bold": False, "expected": True},
            )
        )
    return out


@_check("heading_alignment")
def check_heading_alignment(facts, rule, ctx) -> list[Issue]:
    """标题对齐。

    ``expected`` 可以是单个值或列表（很多学校规定"居中或左对齐都行"）。
    列表支持是必要的：把"或"写成"且"会让一半合规的稿子被报错。
    """
    raw = rule.expected
    allowed = {str(x) for x in raw} if isinstance(raw, (list, tuple, set)) else {str(raw)}
    out: list[Issue] = []
    for h in _heading_paras(facts, rule):
        if h.align in allowed:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        level = _heading_level(h)
        level_desc = f"{level + 1} 级标题" if level is not None else "标题"
        out.append(
            _issue(
                rule,
                f"{level_desc}对齐方式为「{h.align}」，应为「{'/'.join(sorted(allowed))}」",
                evidence=short_evidence(h.text),
                location={"paragraph": h.index, "align": h.align, "expected": sorted(allowed)},
            )
        )
    return out


@_check("heading_spacing")
def check_heading_spacing(facts, rule, ctx) -> list[Issue]:
    """标题段前段后间距。

    ``expected`` 形如 ``{"before_pt": 12, "after_pt": 12}``；未给出的那一项
    不检查。缺失即跳过而不是当 0：把"没规定"当成"必须为 0"会误报一片。
    """
    exp = rule.expected or {}
    before = _fnum(exp.get("before_pt"))
    after = _fnum(exp.get("after_pt"))
    if before is None and after is None:
        ctx.note_skip(rule.id, "规则没有给出期望间距（expected.before_pt/after_pt）")
        return []
    out: list[Issue] = []
    for h in _heading_paras(facts, rule):
        bad: list[str] = []
        if before is not None and not _close(h.space_before_pt, before, rule.tolerance):
            bad.append(f"段前 {h.space_before_pt:g} 磅（应为 {before:g}）")
        if after is not None and not _close(h.space_after_pt, after, rule.tolerance):
            bad.append(f"段后 {h.space_after_pt:g} 磅（应为 {after:g}）")
        if not bad:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        level = _heading_level(h)
        level_desc = f"{level + 1} 级标题" if level is not None else "标题"
        out.append(
            _issue(
                rule,
                f"{level_desc}间距不符：{'；'.join(bad)}",
                evidence=short_evidence(h.text),
                location={
                    "paragraph": h.index,
                    "space_before_pt": h.space_before_pt,
                    "space_after_pt": h.space_after_pt,
                    "expected": exp,
                },
            )
        )
    return out


# -- 页面规则 ---------------------------------------------------------------
@_check("page_size")
def check_page_size(facts, rule, ctx) -> list[Issue]:
    """纸张大小。

    ``expected`` 形如 ``{"width_cm": 21.0, "height_cm": 29.7, "name": "A4"}``。
    横排页的宽高是反的，所以两个方向都比一次。
    """
    exp = rule.expected or {}
    w, h = _fnum(exp.get("width_cm")), _fnum(exp.get("height_cm"))
    if w is None or h is None:
        ctx.note_skip(rule.id, "规则没有给出期望纸张尺寸")
        return []
    tol = rule.tolerance or 0.2
    name = exp.get("name") or f"{w:g}×{h:g}cm"
    out: list[Issue] = []
    for s in facts.sections:
        ok = (_close(s.page_width_cm, w, tol) and _close(s.page_height_cm, h, tol)) or (
            _close(s.page_width_cm, h, tol) and _close(s.page_height_cm, w, tol)
        )
        if ok:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"第 {s.index + 1} 节纸张为 {s.page_width_cm:g}×{s.page_height_cm:g} cm，应为 {name}",
                evidence=f"节 {s.index + 1}：{s.page_width_cm:g}×{s.page_height_cm:g} cm",
                location={
                    "section": s.index,
                    "paragraph": -1,
                    "page_width_cm": s.page_width_cm,
                    "page_height_cm": s.page_height_cm,
                    "expected": exp,
                },
            )
        )
    return out


@_check("page_margin")
def check_page_margin(facts, rule, ctx) -> list[Issue]:
    """页边距与页眉页脚距离。

    ``expected`` 形如 ``{"top_cm": 2.5, "bottom_cm": 2.5, "left_cm": 3.0,
    "right_cm": 2.5, "header_cm": 1.5, "footer_cm": 1.75}``；未给出的键不检查。
    """
    exp = rule.expected or {}
    keys = [
        ("top_cm", "上边距"),
        ("bottom_cm", "下边距"),
        ("left_cm", "左边距"),
        ("right_cm", "右边距"),
        ("header_cm", "页眉距边界"),
        ("footer_cm", "页脚距边界"),
    ]
    if not any(k in exp for k, _ in keys):
        ctx.note_skip(rule.id, "规则没有给出任何边距期望值")
        return []
    tol = rule.tolerance or 0.1
    out: list[Issue] = []
    for s in facts.sections:
        bad: list[str] = []
        for key, label in keys:
            want = _fnum(exp.get(key))
            if want is None:
                continue
            actual = _fnum(getattr(s, key, None))
            if actual is None or not _close(actual, want, tol):
                bad.append(f"{label} {actual if actual is not None else '?'} cm（应为 {want:g}）")
        if not bad:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"第 {s.index + 1} 节页边距不符：{'；'.join(bad)}",
                evidence=f"节 {s.index + 1}：上{s.top_cm:g} 下{s.bottom_cm:g} 左{s.left_cm:g} 右{s.right_cm:g} cm",
                location={"section": s.index, "paragraph": -1, "expected": exp},
            )
        )
    return out


@_check("page_default_font")
def check_page_default_font(facts, rule, ctx) -> list[Issue]:
    """文档默认值（docDefaults）里的字体字号。

    这条常被忽略但很关键：docDefaults 决定"没显式设格式的地方长什么样"，
    包括作者后续新敲的段落。模板默认值是错的，改正文段落格式只是治标。
    """
    exp = rule.expected or {}
    want_ea = _norm(exp.get("eastAsia"))
    want_ascii = _norm(exp.get("ascii"))
    want_size = _fnum(exp.get("size_pt"))
    bad: list[str] = []
    if want_ea and _norm(facts.doc_default_east_asia) != want_ea:
        bad.append(f"默认中文字体为「{facts.doc_default_east_asia or '未设置'}」（应为「{want_ea}」）")
    if want_ascii and _norm(facts.doc_default_ascii) != want_ascii:
        bad.append(f"默认西文字体为「{facts.doc_default_ascii or '未设置'}」（应为「{want_ascii}」）")
    if want_size is not None and not _close(facts.doc_default_size_pt, want_size, rule.tolerance):
        bad.append(f"默认字号为 {facts.doc_default_size_pt or '未设置'} 磅（应为 {want_size:g}）")
    if not bad:
        return []
    if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
        return []
    return [
        _issue(
            rule,
            f"文档默认样式不符：{'；'.join(bad)}",
            evidence=(
                f"docDefaults: eastAsia={facts.doc_default_east_asia or '空'} "
                f"ascii={facts.doc_default_ascii or '空'} size={facts.doc_default_size_pt}"
            ),
            location={"paragraph": -1, "scope": "docDefaults", "expected": exp},
        )
    ]


# -- 题注规则 ---------------------------------------------------------------
@_check("caption_font_size")
def check_caption_font_size(facts, rule, ctx) -> list[Issue]:
    out: list[Issue] = []
    for c in facts.captions:
        sizes = sorted({r.size_pt for r in c.runs if (r.text or "").strip() and r.size_pt})
        actual = sizes[0] if sizes else c.size_pt
        if actual is None:
            ctx.note_skip(rule.id, f"题注段 {c.index} 没有解析到字号")
            continue
        if _close(actual, rule.expected, rule.tolerance):
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"题注字号为 {actual:g} 磅，应为 {_fnum(rule.expected) or 0:g} 磅",
                evidence=short_evidence(c.text),
                location={"paragraph": c.index, "size_pt": actual, "expected": rule.expected},
            )
        )
    return out


@_check("caption_number_format")
def check_caption_number_format(facts, rule, ctx) -> list[Issue]:
    """题注编号格式（如必须写成「图 2-1」而不是「图 2.1」）。

    ``expected`` 形如 ``{"pattern": "^图\\\\s*\\\\d+-\\\\d+\\\\s"}``。
    不给 pattern 时只检查"题注是否有编号"，不猜格式。
    """
    pattern = str((rule.expected or {}).get("pattern") or rule.params.get("pattern") or "")
    if not pattern:
        ctx.note_skip(rule.id, "规则没有给出编号正则（expected.pattern）")
        return []
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        ctx.note_skip(rule.id, f"编号正则不合法：{exc}")
        return []
    out: list[Issue] = []
    for c in facts.captions:
        if rx.search(c.text.strip()):
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"题注编号格式不符：{short_evidence(c.text, 30)}",
                evidence=short_evidence(c.text),
                location={"paragraph": c.index, "expected": pattern},
            )
        )
    return out


@_check("caption_position")
def check_caption_position(facts, rule, ctx) -> list[Issue]:
    """图表标题位置：图题在图下、表题在表上。

    只在**能确定**位置关系时上报：附近能找到一个含图段落，且它与题注的
    相对位置反了。找不到图就什么都不说——表格在段落序列里没有下标，
    猜位置会造出一批无法复核的问题，而无法复核的问题作者只会当成噪音。
    """
    exp = rule.expected or {}
    fig_below = bool(exp.get("figure_below_image", True))
    out: list[Issue] = []
    for c in facts.captions:
        if c.caption_label != "figure":
            continue
        img_idx = _nearest_image(facts, c.index)
        if img_idx is None:
            continue
        # 图题在下：题注下标应大于图片下标。
        ok = (c.index > img_idx) if fig_below else (c.index < img_idx)
        if ok:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                "图题位置不对：图题应放在图的下方",
                evidence=short_evidence(c.text),
                location={"paragraph": c.index, "image_paragraph": img_idx},
            )
        )
    return out


@_check("figure_caption_present")
def check_figure_caption_present(facts, rule, ctx) -> list[Issue]:
    """有图必须有题注。

    判据是"含图段落的下一个非空段落是不是题注"，而不是"文档里有没有图题"。
    后者会让"5 张图只有 1 个题注"这种最常见的情况被漏掉。
    """
    out: list[Issue] = []
    for p in facts.paragraphs:
        if not p.has_image:
            continue
        nxt = _next_nonempty(facts, p.index)
        if nxt is not None and nxt.caption_label == "figure":
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"第 {p.index} 段的图没有题注",
                evidence=p.text.strip() or f"[图片段落 {p.index}]",
                location={"paragraph": p.index},
            )
        )
    return out


def _next_nonempty(facts: DocxFormatFacts, index: int) -> Paragraph | None:
    for p in facts.paragraphs:
        if p.index > index and p.text.strip():
            return p
    return None


# -- 参考文献规则 -----------------------------------------------------------
def _reference_number_style(text: str) -> str:
    """参考文献条目的编号形态：bracket / paren / dot / none。"""
    t = (text or "").lstrip()
    if re.match(r"^\[\d+", t):
        return "bracket"
    if re.match(r"^[（(]\d+[）)]", t):
        return "paren"
    if re.match(r"^\d+[.、]", t):
        return "dot"
    return "none"


@_check("reference_number_style")
def check_reference_number_style(facts, rule, ctx) -> list[Issue]:
    """参考文献编号形态统一。

    只判"与期望形态不一致"，不判"编号是否连续"——连续性需要把正文引用
    与条目对应起来，那是引用校验模块的职责，放在格式层做只会重复报错。
    """
    want = str(rule.expected or "bracket")
    out: list[Issue] = []
    for r in facts.references:
        style = _reference_number_style(r.text)
        if style == want:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        desc = {"bracket": "方括号 [1]", "paren": "圆括号（1）", "dot": "数字加点 1.", "none": "无编号"}
        out.append(
            _issue(
                rule,
                f"参考文献编号形态为「{desc.get(style, style)}」，应为「{desc.get(want, want)}」",
                evidence=short_evidence(r.text),
                location={"paragraph": r.index, "number_style": style, "expected": want},
            )
        )
    return out


@_check("reference_entry_fields")
def check_reference_entry_fields(facts, rule, ctx) -> list[Issue]:
    """参考文献条目基本信息。

    ``expected`` 形如 ``{"require_year": true, "require_author": true}``。
    这里只做**存在性**判断（有没有年份、编号前有没有作者），不做字段解析：
    把条目拆成作者/题名/刊名/年卷期是引用模块的事，格式层重复实现一遍
    只会得到两份互相矛盾的结论。
    """
    exp = rule.expected or {}
    need_year = bool(exp.get("require_year", True))
    need_author = bool(exp.get("require_author", True))
    out: list[Issue] = []
    for r in facts.references:
        text = r.text.strip()
        bad: list[str] = []
        if need_year and not REF_YEAR_RE.search(text):
            bad.append("看不出年份")
        if need_author:
            body = REF_NUMBER_RE.sub("", text, count=1).strip()
            if body and not re.match(r"^[\u4e00-\u9fffA-Za-z]", body):
                bad.append("编号后不是作者名开头")
            if len(body) < 6:
                bad.append("条目过短，缺少作者或题名")
        if not bad:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"参考文献条目信息不全：{'；'.join(bad)}",
                evidence=short_evidence(text),
                location={"paragraph": r.index, "missing": bad},
            )
        )
    return out


# -- 标点 / 空格 / 空行规则 --------------------------------------------------
#: 半角标点夹在中文之间。`(?<=[\u4e00-\u9fff])` 保证只报"中文语境"里的
#: 半角标点，英文句子和代码片段不受影响。
_HALF_PUNCT_IN_CJK = re.compile(r"(?<=[\u4e00-\u9fff])([,;:!?])")
#: 全角标点夹在拉丁字母之间。
_FULL_PUNCT_IN_LATIN = re.compile(r"(?<=[A-Za-z])[，。；：！？]")

#: 参数里的例外短语：命中的段落整段跳过。规则文件里给的是"误报高发区"，
#: 例如"(c) 2024"这种版权写法。
_DEFAULT_PUNCT_EXCEPTIONS = ("(c)", "(C)", "(r)", "(R)", "(tm)", "(TM)")


def _paragraph_texts(facts: DocxFormatFacts, scope: str) -> Iterable[Paragraph]:
    """按 scope 取待检查段落。

    空格/标点类规则要用 ``paragraph`` scope：它们**必须**覆盖全篇，
    包括参考文献和题注——正文里查标点、参考文献里不查，是自欺欺人。
    格式属性类规则才该用 ``body``/``heading`` 把范围收窄。
    """
    if scope == "body":
        return _body_paras(facts)
    if scope == "heading":
        return facts.headings
    if scope == "reference":
        return facts.references
    if scope == "caption":
        return facts.captions
    return [p for p in facts.paragraphs if p.text.strip()]


@_check("punct_mixed")
def check_punct_mixed(facts, rule, ctx) -> list[Issue]:
    """中英文标点混用 / 全半角混用。

    ``expected`` 给出中文语境里应该用的全角标点，形如
    ``{"，": ",", "；": ";"}``（键=正确，值=被误用的半角）。这个方向
    让规则文件读起来像规范条文，而不是像程序。
    """
    mapping = rule.expected or {",": ",", ";": ";"}
    if not isinstance(mapping, dict) or not mapping:
        ctx.note_skip(rule.id, "规则没有给出标点映射（expected: 全角→半角）")
        return []
    reverse = {str(v): str(k) for k, v in mapping.items()}
    half_to_full = {k: v for k, v in reverse.items() if k}
    exceptions = tuple(rule.params.get("exceptions") or _DEFAULT_PUNCT_EXCEPTIONS)

    out: list[Issue] = []
    for p in _paragraph_texts(facts, rule.scope):
        text = p.text
        if any(ex in text for ex in exceptions):
            continue
        hits = _HALF_PUNCT_IN_CJK.findall(text)
        latin_hits = _FULL_PUNCT_IN_LATIN.findall(text)
        if not hits and not latin_hits:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        bad = sorted(set(hits + latin_hits))
        fixed = "".join(half_to_full.get(ch, ch) for ch in bad)
        out.append(
            _issue(
                rule,
                f"中英文标点混用：出现了半角「{'、'.join(bad)}」，中文语境应用全角「{fixed}」",
                evidence=short_evidence(text),
                location={"paragraph": p.index, "chars": bad},
            )
        )
    return out


@_check("punct_quotes")
def check_punct_quotes(facts, rule, ctx) -> list[Issue]:
    """中文语境里的直引号应改为弯引号。

    只报"直引号两侧存在中文"的情况：纯英文引用（``He said "hi"``）用直引号
    是对的，一起报就是误报。
    """
    out: list[Issue] = []
    for p in _paragraph_texts(facts, rule.scope):
        text = p.text
        if '"' not in text:
            continue
        # 直引号与中文同行出现，才认为该换成弯引号。
        if not re.search(r"[\u4e00-\u9fff][^\"\n]*\"|\"[^\"\n]*[\u4e00-\u9fff]", text):
            continue
        if CURLY_QUOTE_RE.search(text) and '"' not in text:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                "中文语境使用了英文直引号，应为全角弯引号「“”」",
                evidence=short_evidence(text),
                location={"paragraph": p.index, "char": text.find('"')},
            )
        )
    return out


#: 空格规则里允许存在的空格：**前一个字符**命中这些词时，后面的空格不算多余。
#: 例如「第二章 相关工作」「图 3-1 结构」——这个空格是中文排版的常规写法，
#: 删掉反而难看，所以按例外处理而不是报错。判据只看前一个字符，是因为
#: 「章」几乎只出现在"第X章"里，而"文章 内容"这类误判的代价（漏报一次
#: 空格）远小于误报的代价（作者去删一个正确的空格）。
_DEFAULT_SPACE_EXCEPTIONS = ["第", "章", "节", "图", "表", "式", "附录", "附表", "附图"]


@_check("cjk_space_cjk")
def check_cjk_space_cjk(facts, rule, ctx) -> list[Issue]:
    """中文之间多打的空格。

    `中 文` 这种是最典型的"从 PDF 复制粘贴"残留。中文之间的空格没有
    任何排版意义，一律可删——所以这条是**可机械修复**的。

    例外见 ``_DEFAULT_SPACE_EXCEPTIONS``：章节号与图表号后面跟标题的空格
    是规范写法，报了就是误报。
    """
    exceptions = tuple(rule.params.get("exceptions") or _DEFAULT_SPACE_EXCEPTIONS)
    out: list[Issue] = []
    for p in _paragraph_texts(facts, rule.scope):
        m = None
        for cand in CJK_SPACE_CJK_RE.finditer(p.text):
            # 例外看的是空格**前面那个字符**（因为正则的第一个捕获组就是它）。
            # 位置算错会把「第二章 相关工作」判成多余空格，而它其实是规范写法。
            if any(cand.group(1) == ex for ex in exceptions):
                continue
            m = cand
            break
        if m is None:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                "中文之间有多余空格",
                evidence=short_evidence(p.text),
                location={
                    "paragraph": p.index,
                    "char": m.start(),
                    "find": m.group(0),
                },
            )
        )
    return out


@_check("cjk_latin_spacing")
def check_cjk_latin_spacing(facts, rule, ctx) -> list[Issue]:
    """中英文之间缺空格。

    中文排版惯例（也是多数学校规范的要求）是中英文之间留一个空格。
    但**例外很多**：``第3章``、``图2-1``、``3.5倍``、``95%``、``中文English``
    里的英文若是句子开头的整体英文，处理方式不同。这里用三条排除规则
    压掉最常见的误报，剩下的交给人工确认——所以这条 **fixable=False**：
    批量插空格一旦判错，改的是全篇，代价远大于收益。
    """
    out: list[Issue] = []
    exceptions = tuple(rule.params.get("exclude_before") or ("第", "图", "表", "式", "节", "章", "附录"))
    for p in _paragraph_texts(facts, rule.scope):
        text = p.text
        m = None
        for cand in MISSING_CJK_LATIN_SPACE_RE.finditer(text):
            s = cand.start()
            if s > 0 and text[s] in exceptions:
                continue
            m = cand
            break
        if m is None:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                f"中英文之间缺少空格：{short_evidence(m.group(0), 20)}",
                evidence=short_evidence(text),
                location={"paragraph": p.index, "char": m.start(), "find": m.group(0)},
            )
        )
    return out


@_check("trailing_space")
def check_trailing_space(facts, rule, ctx) -> list[Issue]:
    """行尾多余空格。

    纯文本层的问题：docx 里行尾空格不影响排版，但会污染下一环节的
    文本比对（"这段文字到底有没有变"），也会被降 AIGC 的改写管线带进
    新文本。所以是可机械删除的。
    """
    out: list[Issue] = []
    for p in _paragraph_texts(facts, rule.scope):
        m = re.search(r"[ \t\u3000]+$", p.text)
        if not m:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        out.append(
            _issue(
                rule,
                "段落末尾有多余空格",
                evidence=short_evidence(p.text + "␠"),
                location={
                    "paragraph": p.index,
                    "char": m.start(),
                    "find": m.group(0),
                },
            )
        )
    return out


@_check("empty_paragraph_run")
def check_empty_paragraph_run(facts, rule, ctx) -> list[Issue]:
    """连续空段落。

    单个空段是排版手段（图表前后留白），连续多个就该删。删到只剩一个空段
    是可机械完成的，所以 fixable=True。
    """
    limit = int(rule.params.get("max_consecutive", (rule.expected or {}).get("max_consecutive", 1)) or 1)
    out: list[Issue] = []
    run_start: int | None = None
    run: list[Paragraph] = []
    for p in facts.paragraphs:
        if not p.text.strip():
            if run_start is None:
                run_start = p.index
            run.append(p)
            continue
        if run and len(run) > limit:
            if ctx.bump(rule.id) <= MAX_ISSUES_PER_RULE:
                out.append(
                    _issue(
                        rule,
                        f"连续 {len(run)} 个空段落，最多允许 {limit} 个",
                        evidence=f"第 {run_start}–{run[-1].index} 段为空",
                        location={"paragraph": run_start, "count": len(run), "find": "\n"},
                    )
                )
        run_start, run = None, []
    if run and len(run) > limit:
        if run_start is not None and ctx.bump(rule.id) <= MAX_ISSUES_PER_RULE:
            out.append(
                _issue(
                    rule,
                    f"文档末尾连续 {len(run)} 个空段落，最多允许 {limit} 个",
                    evidence=f"第 {run_start}–{run[-1].index} 段为空",
                    location={"paragraph": run_start, "count": len(run), "find": "\n"},
                )
            )
    return out


@_check("manual_line_break")
def check_manual_line_break(facts, rule, ctx) -> list[Issue]:
    """用 ``<w:br>``（Shift+Enter）代替分段。

    排版后果很实际：手动换行的地方不享受段落格式（首行缩进错位、行距不
    一致），而作者往往以为它"就是换了个段落"。检查器只报，不自动改——
    改成真分段的语义是"这一行本来该独立成段"，只有作者能确认。
    """
    out: list[Issue] = []
    for p in _paragraph_texts(facts, rule.scope):
        if not p.has_break:
            continue
        if "\n" not in p.text:
            continue
        if ctx.bump(rule.id) > MAX_ISSUES_PER_RULE:
            continue
        first_line = p.text.split("\n", 1)[0]
        out.append(
            _issue(
                rule,
                "段落里用了手动换行符（Shift+Enter），该处应另起一段",
                evidence=short_evidence(p.text),
                location={"paragraph": p.index, "find": "\n", "first_line": first_line},
            )
        )
    return out


# --------------------------------------------------------------------------
# 引擎
# --------------------------------------------------------------------------
def _default_ruleset() -> RuleSet:
    """通用学位论文规范。

    数值取的是国内高校学位论文格式规定里出现频率最高的一组（A4、
    上下 2.5cm、左 3.0cm、右 2.5cm；正文小四宋体、行距 1.5 倍、
    首行缩进 2 字符；一级标题三号黑体居中……）。

    刻意**不**把它写成"某大学的规范"：通用的意义是开箱能跑，学校模板
    到手后应该复制 ``rules_default.yaml`` 改成 ``rules_<学校>.yaml``，
    而不是在代码里加 if。
    """
    Y = "通用学位论文规范（可在 rules_default.yaml 上按学校改）"
    rules = [
        # -- 正文 --
        FormatRule(
            id="body_cjk_font",
            title="正文中文字体",
            severity=Severity.MAJOR,
            scope="body",
            check="body_cjk_font",
            expected={"eastAsia": "宋体"},
            hint="选中正文 → 开始 → 字体 → 中文字体设为宋体（不要用「宋体-方正超大字符集」这类别名）",
            source=Y,
        ),
        FormatRule(
            id="body_latin_font",
            title="正文西文字体",
            severity=Severity.MINOR,
            scope="body",
            check="body_latin_font",
            expected={"ascii": "Times New Roman"},
            hint="中文字体设为宋体后，把西文字体单独设为 Times New Roman",
            source=Y,
        ),
        FormatRule(
            id="body_font_size",
            title="正文字号",
            severity=Severity.MAJOR,
            scope="body",
            check="body_font_size",
            expected=12.0,
            tolerance=0.01,
            hint="正文字号应为小四（12 磅）",
            source=Y,
        ),
        FormatRule(
            id="body_line_spacing",
            title="正文行距",
            severity=Severity.MAJOR,
            scope="body",
            check="body_line_spacing",
            expected={"mode": "multiple", "value": 1.5},
            tolerance=0.01,
            hint="段落 → 行距 → 1.5 倍行距（若学校要求固定值 22 磅，把 expected 改成 {mode: fixed, pt: 22}）",
            source=Y,
        ),
        FormatRule(
            id="body_first_line_indent",
            title="正文首行缩进 2 字符",
            severity=Severity.MAJOR,
            scope="body",
            check="body_first_line_indent",
            expected={"chars": 2.0},
            tolerance=0.05,
            params={"pt_per_char": 12.0},
            hint="段落 → 特殊格式 → 首行缩进 → 2 字符（用「字符」单位，不要手填磅值）",
            source=Y,
        ),
        FormatRule(
            id="body_alignment",
            title="正文两端对齐",
            severity=Severity.MINOR,
            scope="body",
            check="body_alignment",
            expected={"align": "justify"},
            hint="段落 → 对齐方式 → 两端对齐（正文用左对齐会让右边界参差，多数学校明确要求两端对齐）",
            # **默认关闭**，理由见 rules_default.yaml 里同一条的注释：
            # Word 默认就是左对齐，"没写 w:jc"会被判成对齐偏差，几乎每篇稿子
            # 都会刷出一片 minor 噪声。学校明确要求时把这里改成 True 即可，
            # 检查与写回会同时生效（写回只改 enabled 的规则）。
            enabled=False,
            source=Y,
        ),
        # -- 标题 --
        FormatRule(
            id="h1_cjk_font",
            title="一级标题字体",
            severity=Severity.MAJOR,
            scope="heading1",
            check="heading_cjk_font",
            expected={"eastAsia": "黑体"},
            hint="章标题中文字体应为黑体",
            source=Y,
        ),
        FormatRule(
            id="h1_font_size",
            title="一级标题字号",
            severity=Severity.MAJOR,
            scope="heading1",
            check="heading_font_size",
            expected=16.0,
            tolerance=0.01,
            hint="章标题字号应为三号（16 磅）",
            source=Y,
        ),
        FormatRule(
            id="h1_bold",
            title="一级标题加粗",
            severity=Severity.MINOR,
            scope="heading1",
            check="heading_bold",
            expected=True,
            hint="章标题应加粗（黑体本身较粗，若学校模板不加粗则把本规则 enabled 设为 false）",
            source=Y,
        ),
        FormatRule(
            id="h1_alignment",
            title="一级标题对齐",
            severity=Severity.MINOR,
            scope="heading1",
            check="heading_alignment",
            expected=["center", "left"],
            hint="章标题通常居中；左对齐也常见，按学校模板定",
            source=Y,
        ),
        FormatRule(
            id="h1_spacing",
            title="一级标题段前段后间距",
            severity=Severity.INFO,
            scope="heading1",
            check="heading_spacing",
            expected={"before_pt": 12.0, "after_pt": 12.0},
            tolerance=1.0,
            hint="章标题段前段后各 12 磅（0.5 行）",
            source=Y,
        ),
        FormatRule(
            id="h2_cjk_font",
            title="二级标题字体",
            severity=Severity.MINOR,
            scope="heading2",
            check="heading_cjk_font",
            expected={"eastAsia": "黑体"},
            hint="节标题中文字体应为黑体",
            source=Y,
        ),
        FormatRule(
            id="h2_font_size",
            title="二级标题字号",
            severity=Severity.MINOR,
            scope="heading2",
            check="heading_font_size",
            expected=14.0,
            tolerance=0.01,
            hint="节标题字号应为四号（14 磅）",
            source=Y,
        ),
        FormatRule(
            id="h2_bold",
            title="二级标题加粗",
            severity=Severity.MINOR,
            scope="heading2",
            check="heading_bold",
            expected=True,
            hint="节标题应加粗",
            source=Y,
        ),
        # -- 页面 --
        FormatRule(
            id="page_size",
            title="纸张大小",
            severity=Severity.MAJOR,
            scope="section",
            check="page_size",
            expected={"width_cm": 21.0, "height_cm": 29.7, "name": "A4"},
            tolerance=0.2,
            hint="页面布局 → 纸张大小 → A4",
            source=Y,
        ),
        FormatRule(
            id="page_margin",
            title="页边距与页眉页脚距离",
            severity=Severity.MAJOR,
            scope="section",
            check="page_margin",
            expected={
                "top_cm": 2.5,
                "bottom_cm": 2.5,
                "left_cm": 3.0,
                "right_cm": 2.5,
                "header_cm": 1.5,
                "footer_cm": 1.75,
            },
            tolerance=0.1,
            hint="页面布局 → 页边距 → 自定义边距；左 3.0cm 是为装订留的，别改成 2.5",
            source=Y,
        ),
        FormatRule(
            id="doc_default_font",
            title="文档默认字体字号",
            severity=Severity.MINOR,
            scope="section",
            check="page_default_font",
            expected={"eastAsia": "宋体", "ascii": "Times New Roman", "size_pt": 12.0},
            tolerance=0.01,
            hint="样式 → 管理样式 → 默认；默认值错了会导致新敲的段落全是错的",
            source=Y,
        ),
        # -- 图表题注 --
        FormatRule(
            id="caption_font_size",
            title="图表题注字号",
            severity=Severity.MINOR,
            scope="caption",
            check="caption_font_size",
            expected=10.5,
            tolerance=0.01,
            hint="题注字号应为五号（10.5 磅）",
            source=Y,
        ),
        FormatRule(
            id="caption_number_format",
            title="题注编号格式",
            severity=Severity.MINOR,
            scope="caption",
            check="caption_number_format",
            expected={"pattern": "^(图|表)\\s*\\d+[-–—.]\\d+"},
            hint="题注编号应形如「图 2-1」「表 3-2」（章号-序号），不要用「图 1」「图一」",
            source=Y,
        ),
        FormatRule(
            id="caption_position",
            title="图题在图下方",
            severity=Severity.MINOR,
            scope="figure_caption",
            check="caption_position",
            expected={"figure_below_image": True},
            hint="图题放在图的下面；表题放在表的上面（表题用不着这条规则，但方向别记反）",
            source=Y,
        ),
        FormatRule(
            id="figure_caption_present",
            title="有图必须有题注",
            severity=Severity.MAJOR,
            scope="figure_caption",
            check="figure_caption_present",
            hint="每张图下面都要有编号题注，缺图号是评审必挑的",
            source=Y,
        ),
        # -- 参考文献 --
        FormatRule(
            id="reference_number_style",
            title="参考文献编号形态",
            severity=Severity.MINOR,
            scope="reference",
            check="reference_number_style",
            expected="bracket",
            hint="参考文献条目用 [1] [2] 方括号编号，并与正文引用一一对应",
            source=Y,
        ),
        FormatRule(
            id="reference_entry_fields",
            title="参考文献条目完整性",
            severity=Severity.MAJOR,
            scope="reference",
            check="reference_entry_fields",
            expected={"require_year": True, "require_author": True},
            hint="每条参考文献都要有作者与年份；缺字段的条目要回原文核对补全",
            source=Y,
        ),
        # -- 标点 / 空格 / 空行（这四类是纯文本问题，可机械修复） --
        FormatRule(
            id="punct_halfwidth_in_cjk",
            title="中文语境使用半角标点",
            severity=Severity.MINOR,
            # 只扫正文。参考文献条目用的是另一套标点规范
            # （「刊名, 2020, 43(5): 1-10.」里的半角逗号是 GB/T 7714 的写法），
            # 按中文正文的规则去要求它会报出一片假问题。
            scope="body",
            check="punct_mixed",
            expected={"，": ",", "；": ";", "：": ":", "！": "!", "？": "?"},
            hint="中文语境里的逗号/分号/冒号/叹号/问号用全角",
            fixable=True,
            fix_op="normalize_punctuation",
            source=Y,
        ),
        FormatRule(
            id="punct_straight_quotes",
            title="中文语境使用英文直引号",
            severity=Severity.MINOR,
            scope="paragraph",
            check="punct_quotes",
            hint="中文里的引号用「“”」全角弯引号",
            fixable=True,
            fix_op="normalize_punctuation",
            source=Y,
        ),
        FormatRule(
            id="space_between_cjk",
            title="中文之间存在多余空格",
            severity=Severity.MINOR,
            scope="paragraph",
            check="cjk_space_cjk",
            hint="删除中文之间的空格",
            fixable=True,
            fix_op="replace_span",
            source=Y,
        ),
        FormatRule(
            id="space_cjk_latin_missing",
            title="中英文之间缺空格",
            severity=Severity.MINOR,
            scope="paragraph",
            check="cjk_latin_spacing",
            params={"exclude_before": ["第", "图", "表", "式", "节", "章", "附录", "共"]},
            hint="中英文之间加一个半角空格（例外：「第3章」「图2-1」这类不加）",
            fixable=False,
            source=Y,
        ),
        FormatRule(
            id="trailing_space",
            title="段落末尾多余空格",
            severity=Severity.INFO,
            scope="paragraph",
            check="trailing_space",
            hint="删除行尾空格",
            fixable=True,
            fix_op="replace_span",
            source=Y,
        ),
        FormatRule(
            id="empty_paragraph_run",
            title="连续空段落",
            severity=Severity.INFO,
            scope="paragraph",
            check="empty_paragraph_run",
            expected={"max_consecutive": 1},
            hint="连续空段最多保留一个；多余的直接删",
            fixable=True,
            fix_op="delete_span",
            source=Y,
        ),
        FormatRule(
            id="manual_line_break",
            title="用手动换行符代替分段",
            severity=Severity.MINOR,
            scope="paragraph",
            check="manual_line_break",
            hint="把 Shift+Enter 的手动换行改成回车分段，否则该处不享受段落格式",
            fixable=False,
            source=Y,
        ),
    ]
    return RuleSet(
        name="default",
        school="通用",
        template_version="generic-1.0",
        source="通用学位论文格式规范（GB/T 7713 与多数高校规定的高频值）",
        notes=[
            "这是一份可编辑的基线，不是任何一所学校的官方规范。",
            "拿到学校模板后，复制 rules_default.yaml 为 rules_<学校>.yaml，按红头文件改数值。",
            "字体/字号/行距/边距/缩进属于 OOXML 属性，纯文本算子改不了，一律 fixable=False。",
        ],
        rules=rules,
    )


def default_ruleset_path() -> Path:
    """内置规则文件的位置。

    规则文件与代码同目录：规则随代码发布，但改动不需要改代码。
    """
    return Path(__file__).resolve().parent / "rules_default.yaml"


def load_default_ruleset() -> RuleSet:
    """加载内置规则文件；文件缺失时回退到代码里的默认定义。

    回退而不是报错，是因为"规则文件被误删"不该让整套流程不可用——
    代码里的 ``_default_ruleset()`` 与 YAML 同源，行为一致。
    """
    path = default_ruleset_path()
    if path.exists():
        try:
            return RuleSet.from_yaml(path)
        except RuleSetError:
            return _default_ruleset()
    return _default_ruleset()


# --------------------------------------------------------------------------
# 从学校官方 .docx 模板推断规则集 —— 接口已定，实现待模板到位
# --------------------------------------------------------------------------
def infer_ruleset_from_template(path: str | Path) -> RuleSet:  # pragma: no cover
    """**未实现。** 从学校官方 .docx 模板推断出一份 RuleSet。

    为什么留这个入口、以及为什么现在故意不写实现：

    用户拿到手的是**一份排版好的空模板文档**，不是一张规范数值表。让作者照着
    模板手抄一份 YAML 不现实——他无从知道"正文的中文字体到底写在 docDefaults
    还是 Normal 样式上"。所以最终形态必然是"喂一份 .docx，吐一份 RuleSet"。

    但**模板文件还没到手**。没有真实样本时，推断只能靠猜；而猜出来的规范会被
    当成"已经按学校要求核对过了"，比没有规范更危险——用户以为核对了学校要求，
    实际核对的是一份凭空生成的数值表。所以这里只定接口，不写实现。

    接口契约（模板到位后按此实现，**不需要改动其他模块**）：

    * 输入：学校官方模板的 ``.docx`` 路径。
    * 读出：``app.parsing.docx_reader.read_docx`` 得到 ``DocxFormatFacts``
      ——字体/字号/行距/首行缩进/页边距/标题层级/题注样式都已在其中，
      所以本函数不需要新增解析代码。
    * 推断：每一类对象取**占多数的那个值**作为 ``expected``。这正是
      ``FormatRule.expected`` 被设计成"同一 check 换一个期望值"的原因：
      规则形状不用变，只换数值，``rules_default.yaml`` 可以逐条对照。
    * 产出：``RuleSet``，其 ``school`` 取学校名、``template_version`` 取模板文件
      哈希前缀、``source`` 写明"从 <模板文件名> 推断"，便于日后回答"按哪版规范判的"。
    * 兜底：某一类在模板里找不到可判定的样本时，**沿用 ``DEFAULT_RULESET``
      对应规则的 ``expected``，并在 ``notes`` 里逐条记录"这一项继承自通用规范"**。
      绝不能静默填一个猜出来的数值——那是这份接口最需要防的失败。

    Raises:
        NotImplementedError: 恒抛。等待用户提供学校官方 .docx 模板文件。
    """
    raise NotImplementedError(
        "infer_ruleset_from_template 尚未实现：等待用户提供学校官方 .docx 模板文件。"
        "在没有真实模板的情况下推断出来的规范只是猜测，而猜出来的规范会让作者"
        "误以为格式已按学校要求核对过，比不检查更危险。模板到位后按本函数 docstring "
        "里的接口契约实现即可，无需改动其他模块。"
    )


#: 通用规范。检查器默认用它；学校模板到手后传自己的 RuleSet 即可。
DEFAULT_RULESET = _default_ruleset()


def evaluate(
    facts: DocxFormatFacts,
    ruleset: RuleSet | None = None,
    *,
    only: Iterable[str] | None = None,
    stats: dict | None = None,
) -> list[Issue]:
    """对一份格式事实跑一遍规则集。

    ``only`` 可以只跑指定规则 id，用于"只检查页边距"这类定点调用。
    ``stats`` 是输出参数：回填每条规则命中的条数与因为读不到属性而跳过的
    项。没有它，报告就无法区分"规则通过了"和"规则没读到值"——这两者的
    差别是"稿子对"和"检查器瞎了"。

    单条规则抛异常不会中断整轮：格式检查是批量作业，一条规则写错不该让
    其余 28 条都不跑。异常记进 stats，由上层提示"某条规则需要修"。
    """
    rs = ruleset or DEFAULT_RULESET
    ctx = EvalContext()
    wanted = set(only) if only else None
    issues: list[Issue] = []
    errors: list[dict] = []

    runnable = [
        r for r in rs.enabled_rules() if wanted is None or r.id in wanted
    ]
    for rule in runnable:
        fn = _CHECKS.get(rule.check)
        if fn is None:
            errors.append({"rule": rule.id, "error": f"未实现的 check: {rule.check}"})
            continue
        try:
            found = fn(facts, rule, ctx) or []
        except Exception as exc:  # noqa: BLE001 - 单条规则失败不拖垮整轮
            errors.append({"rule": rule.id, "error": f"{type(exc).__name__}: {exc}"})
            continue
        # 兜底：没有 evidence 或没有 location 的问题在框架里无法复验，
        # 会在 refresh_issue_status 里被当成"不可核验"，宁可在这里补一条空证据
        # 也绝不丢弃——规则说有问题，就不该因为报告不完整而消失。
        for issue in found:
            if not issue.location:
                issue.location = {"paragraph": -1, "rule": rule.id}
            issues.append(issue)

    if stats is not None:
        stats["per_rule"] = ctx.per_rule
        stats["skipped"] = ctx.skipped
        stats["errors"] = errors
        stats["rules_total"] = len(runnable)
        stats["issues"] = len(issues)
    return issues


def summarize_issues(issues: Iterable[Issue]) -> dict:
    """给日志/界面看的汇总。按严重程度和规则分组。"""
    out = {"total": 0, "by_severity": {}, "by_rule": {}, "fixable": 0}
    for i in issues:
        out["total"] += 1
        out["by_severity"][i.severity.value] = out["by_severity"].get(i.severity.value, 0) + 1
        key = i.kind.split(".", 1)[-1]
        out["by_rule"][key] = out["by_rule"].get(key, 0) + 1
        if i.fixable:
            out["fixable"] += 1
    return out
