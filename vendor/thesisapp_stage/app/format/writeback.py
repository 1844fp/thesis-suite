"""把排版属性**写回**已有的 .docx —— 项目一直记在案的那个缺口。

## 为什么单独一个模块，而不是做成修复算子

``app/format`` 原先**只报不改**，理由写在 README 里：

> 框架的修复算子是文本变换（精确替换、编号重排），改不了 ``w:sz`` / ``w:spacing`` /
> ``w:pgMar``。硬塞进算子体系只会造出"报可修复但实际改不动"的假承诺。

这一层就是那个"单独实现"。它与文本算子的根本区别在于**风险不对称**：

* 改错一句话，作者读一遍就看得出来；
* **排版写坏，作者可能到答辩前才发现**——因为内容看着是对的，只是打开时提示
  "文件已损坏"，或者标题全变成宋体而他不确定原本该是什么。

所以这里把安全性做成机制，而不是靠小心：

## 三条硬保证

1. **文字内容一字不改。** 写回前后把全文抽出来逐字比对，只要有一个字符不同就
   **判定失败并自动还原备份**。这条最要紧——它把"只动排版"从一句承诺变成可验证的
   事实。（验证靠 ``_document_text``，它把段落文本、表格文本、页眉页脚全取出来。）
2. **改完必须能重新打开。** 用 python-docx 重新加载并核对段落数，
   与 ``app/export`` 的导出后自检是同一条纪律。
3. **先备份再动手。** 默认写 ``.bak``，失败自动还原。

## 它改什么、不改什么

改的是**属性**：中文字体、西文字体、字号、行距、首行缩进、段前段后、对齐、
页面尺寸与页边距。目标值来自 ``app/format/rules_default.yaml``（29 条规则里的
属性型那部分），所以**"报告偏差"和"改掉偏差"用的是同一份数据**——
改了规则，两边一起变。

不改的：

* **表格里的文字**。``document.paragraphs`` 天然不含表格单元格，
  而表格通常要五号/单倍（比正文小一号），统一改反而错。
* **页眉页脚**。同理，它们不在 ``document.paragraphs`` 里。
* **文本层面的问题**（半角标点、多余空格、手动换行）。那些是文本算子的事，
  在这一层改会混淆两套职责。

## 标题怎么认（踩过的坑）

**不能只看样式名。** 中文论文的标题十有八九是"手工排版"出来的：样式仍然是
``Normal``，靠段落自己的 ``w:outlineLvl``（或编号定义里的 outlineLvl）表明它是标题。
只在样式名里找 ``Heading 1``，结果是**一个标题都认不出来**——章标题被当正文改成
宋体小四，格式检查器紧接着报出一片 ``h1_cjk_font`` / ``h1_font_size`` 偏差，
**越改越糟**。真实开题报告上实测：0 个标题被识别，15 个标题被改坏。

所以这里的判定**复用收稿阶段的同一套规则**（``app.parsing.docx_reader``：
outlineLvl → 样式名/ID → 编号定义），保证"报偏差的判定"和"改偏差的判定"是同一个。
判定依据会写进报告，作者能核对"你这段为什么被当成标题"。
"""

from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Sequence

__all__ = [
    "FormatScheme",
    "WritebackReport",
    "apply_scheme",
    "scheme_from_ruleset",
    "scheme_from_template",
    "inspect_docx",
    "detect_heading_levels",
    "WRITABLE_RULE_IDS",
]

#: 行距模式的两种写法：倍数（Word 的"1.5 倍行距"）与固定值磅。
#: Word 内部都用 ``w:spacing/@w:line`` 表示，靠 ``w:lineRule`` 区分：
#: ``auto`` 时数值是 1/240 行，``exact``/``atLeast`` 时是二十分之一磅。
MM_PER_INCH = 25.4


@dataclass
class FormatScheme:
    """可写回的排版目标。字段与 ``rules_default.yaml`` 里的属性型规则一一对应。

    **任何字段为 ``None`` 表示"这一项不改"**（规则被停用时就是 None）。
    这个约定必须严格遵守：``None`` 若被当成"沿用默认值"，停用一条规则就会变成
    "把这项改成默认值"——检查不报了，文件却被改了。
    """

    #: 正文
    body_cjk_font: str | None = "宋体"
    body_latin_font: str | None = "Times New Roman"
    body_size_pt: float | None = 12.0
    body_line_mode: str | None = "multiple"   # multiple | fixed
    body_line_value: float | None = 1.5       # multiple 时是倍数，fixed 时是磅
    body_first_line_chars: float | None = 2.0
    #: 正文对齐。**默认 None = 不改**，理由：``body_alignment`` 规则默认关闭
    #: （Word 默认左对齐，"没写 w:jc"会被逐段报成偏差，噪声淹掉真问题），
    #: 所以默认也不该去改它。显式指定（命令行 --align、界面选两端对齐、
    #: 或把规则改成 enabled: true）才动。
    body_align: str | None = None
    #: 一级标题
    h1_cjk_font: str | None = "黑体"
    h1_size_pt: float | None = 16.0
    h1_bold: bool | None = True
    h1_align: str | None = "center"
    h1_before_pt: float | None = 12.0
    h1_after_pt: float | None = 12.0
    #: 二级标题
    h2_cjk_font: str | None = "黑体"
    h2_size_pt: float | None = 14.0
    h2_bold: bool | None = True
    h2_align: str | None = "left"
    #: 三级标题（沿用二级的字体，字号取正文）
    h3_cjk_font: str | None = "黑体"
    h3_size_pt: float | None = 12.0
    h3_bold: bool | None = True
    h3_align: str | None = "left"
    #: 图表题注
    caption_size_pt: float | None = 10.5
    caption_cjk_font: str | None = "宋体"
    #: 页面（厘米）
    page_width_cm: float | None = 21.0
    page_height_cm: float | None = 29.7
    margin_top_cm: float | None = 2.5
    margin_bottom_cm: float | None = 2.5
    margin_left_cm: float | None = 3.0
    margin_right_cm: float | None = 2.5
    #: 页眉/页脚到页面边缘的距离。规则集里有这两项（`page_margin` 的 header_cm/footer_cm），
    #: 不写回的话格式检查会一直报 page_margin 偏差——**报得出、改不动，正是本项目
    #: 最不想留下的那种半成品**。
    header_cm: float = 1.5
    footer_cm: float = 1.75
    #: 要不要顺手把文档默认字体也改掉（影响以后新输入的段落）
    set_doc_default: bool = True
    #: 被停用的规则 id。规则集里 enabled=false 的规则会进这里，
    #: 写回时跳过——**"规则关掉了"必须同时意味着"检查不报"和"写回不改"**。
    disabled: tuple[str, ...] = ()

    def is_disabled(self, rule_id: str) -> bool:
        return rule_id in self.disabled

    def to_dict(self) -> dict:
        d = asdict(self)
        d["disabled"] = list(self.disabled)
        return d

    @classmethod
    def from_dict(cls, raw: dict | None) -> "FormatScheme":
        d = dict(raw or {})
        if isinstance(d.get("disabled"), list):
            d["disabled"] = tuple(str(x) for x in d["disabled"])
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in allowed})

    def describe(self) -> str:
        """一句话概括，用于命令输出。被停用（``None``）的项显示为"不改"。"""
        def g(v: Any, suffix: str = "", fmt: str = "g") -> str:
            if v is None:
                return "不改"
            try:
                return format(v, fmt) + suffix
            except (TypeError, ValueError):  # pragma: no cover - 防御性
                return str(v) + suffix

        if self.body_line_mode == "fixed":
            spacing = f"固定值 {g(self.body_line_value, ' 磅')}"
        elif self.body_line_mode is None:
            spacing = "行距不改"
        else:
            spacing = f"{g(self.body_line_value, ' 倍')}行距"
        return (
            f"正文 {g(self.body_cjk_font)}/{g(self.body_latin_font)} "
            f"{g(self.body_size_pt, ' 磅')}、{spacing}、"
            f"首行缩进 {g(self.body_first_line_chars, ' 字符')}；"
            f"一级标题 {g(self.h1_cjk_font)} {g(self.h1_size_pt, ' 磅')}"
        )


@dataclass
class WritebackReport:
    """写回结果。**每一个数字都要能被作者核对。**"""

    ok: bool = False
    source: str = ""
    output: str = ""
    backup: str = ""
    #: 是否原地覆盖源文件。False 表示输出到新文件，源文件未被改动。
    in_place: bool = True
    scheme: dict = field(default_factory=dict)
    #: 统计
    paragraphs_body: int = 0
    paragraphs_heading: int = 0
    paragraphs_caption: int = 0
    #: 识别出的标题数（收稿判定，含 outlineLvl 判出的手工排版标题）
    headings_detected: int = 0
    runs_styled: int = 0
    styles_updated: list[str] = field(default_factory=list)
    sections_updated: int = 0
    skipped_in_tables: int = 0
    #: 验证
    text_unchanged: bool = True
    reopened_ok: bool = False
    paragraphs_before: int = 0
    paragraphs_after: int = 0
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    restored: bool = False

    def as_dict(self) -> dict:
        d = asdict(self)
        d["scheme"] = dict(self.scheme)
        return d


# ---------------------------------------------------------------------------
# 从规则集 / 模板得到方案
# ---------------------------------------------------------------------------

def _expect(rs, rule_id: str, default: Any = None) -> Any:
    for r in getattr(rs, "rules", []) or []:
        if r.id == rule_id:
            return getattr(r, "expected", None)
    return default


def _enabled(rs, rule_id: str) -> bool:
    for r in getattr(rs, "rules", []) or []:
        if r.id == rule_id:
            return bool(getattr(r, "enabled", True))
    return False


#: **写回能改掉的规则 id**。由 :data:`_RULE_TO_FIELDS` **推导**而来，不另立一份。
#:
#: 为什么必须是推导：原先这里是手写的一份平行清单，结果加 ``body_alignment``
#: 时只改了映射、忘了改清单，命令行就把 4 条**能改**的对齐偏差错报成"文本型"，
#: 复核输出成了"文本类 73 → 69"——一个凭空的、解释不清的数字变化。
#: 两份手工清单迟早会分叉，所以只留一份。
#:
#: 用途：命令行复核时把"能改的偏差"和"文本型偏差"分开报（不让作者把
#: "还剩 69 条"误读成"这功能没用"），以及测试里断言"清单每一条写回后都归零"。
WRITABLE_RULE_IDS: tuple[str, ...] = ()  # 见文件末尾赋值（依赖 _RULE_TO_FIELDS 的定义）


#: 规则 id → 它控制的方案字段。用于**停用规则时把字段置为 None**：
#: ``None`` 在写回里表示"这一项不改"。不置 None 的话，``take()`` 拿到 None
#: 会静默沿用方案默认值——于是"关掉 body_font_size"变成"把字号改成 12 磅"：
#: **检查不报了，文件却被改了**，比不关还危险。
_RULE_TO_FIELDS: dict[str, tuple[str, ...]] = {
    "body_cjk_font": ("body_cjk_font",),
    "body_latin_font": ("body_latin_font",),
    "body_font_size": ("body_size_pt",),
    "body_line_spacing": ("body_line_mode", "body_line_value"),
    "body_first_line_indent": ("body_first_line_chars",),
    "body_alignment": ("body_align",),
    "h1_cjk_font": ("h1_cjk_font",),
    "h1_font_size": ("h1_size_pt",),
    "h1_bold": ("h1_bold",),
    "h1_alignment": ("h1_align",),
    "h1_spacing": ("h1_before_pt", "h1_after_pt"),
    "h2_cjk_font": ("h2_cjk_font",),
    "h2_font_size": ("h2_size_pt",),
    "h2_bold": ("h2_bold",),
    "caption_font_size": ("caption_size_pt",),
    "page_size": ("page_width_cm", "page_height_cm"),
    "page_margin": (
        "margin_top_cm", "margin_bottom_cm", "margin_left_cm", "margin_right_cm",
        "header_cm", "footer_cm",
    ),
    "doc_default_font": ("set_doc_default",),
}

#: 见上面的说明：这一份由映射推导，保证"能改的规则清单"和"停用即不改的映射"
#: 永远一致。两份手工清单已经分叉过一次，代价是一个解释不清的数字变化。
WRITABLE_RULE_IDS = tuple(_RULE_TO_FIELDS)


#: 规则 id → (方案字段, 从 expected 取值的方式)。只列**属性型**规则；
#: 文本型规则（标点、空格、换行）不在这里，它们归文本算子。
def scheme_from_ruleset(rs: Any) -> FormatScheme:
    """把规则集里**属性型**的那部分抽成可写回的方案。

    抽不出来的一律沿用方案默认值——规则集是数据，作者可能只写了关心的几条。
    规则被停用时字段置 ``None``（=不改），**不是**沿用默认值。
    """
    s = FormatScheme()
    disabled: list[str] = []

    def take(rule_id: str) -> Any:
        if not _enabled(rs, rule_id):
            disabled.append(rule_id)
            return None
        return _expect(rs, rule_id)

    v = take("body_cjk_font")
    if isinstance(v, dict) and v.get("eastAsia"):
        s.body_cjk_font = str(v["eastAsia"])
    v = take("body_latin_font")
    if isinstance(v, dict) and v.get("ascii"):
        s.body_latin_font = str(v["ascii"])
    v = take("body_font_size")
    if isinstance(v, (int, float)) and v > 0:
        s.body_size_pt = float(v)
    v = take("body_line_spacing")
    if isinstance(v, dict):
        mode = str(v.get("mode") or "multiple")
        s.body_line_mode = "fixed" if mode in ("fixed", "exact", "atLeast") else "multiple"
        s.body_line_value = float(v.get("pt") if s.body_line_mode == "fixed" else v.get("value") or 1.5)
    v = take("body_first_line_indent")
    if isinstance(v, dict) and v.get("chars") is not None:
        s.body_first_line_chars = float(v["chars"])

    v = take("body_alignment")
    if isinstance(v, dict):
        v = v.get("align")
    if isinstance(v, (list, tuple, set)) and v:
        # 规则允许一组可接受值（如 ['justify', 'left']）。写回只能落一个值：
        # 正文优先取 justify（两端对齐是通行做法），否则取排序后的第一个——
        # 排序是为了同一份规则每次都得到同一个结果，不随集合迭代顺序变。
        vals = [str(x) for x in v]
        s.body_align = "both" if "justify" in vals else sorted(vals)[0]
    elif isinstance(v, str) and v:
        # 统一成 OOXML 的词（w:jc=both）。规则里写 justify 也认，
        # 否则同一件事会出现两种写法，界面/命令行/规则三处对不上。
        s.body_align = "both" if v in ("justify", "distribute") else v

    v = take("h1_cjk_font")
    if isinstance(v, dict) and v.get("eastAsia"):
        s.h1_cjk_font = str(v["eastAsia"])
    v = take("h1_font_size")
    if isinstance(v, (int, float)) and v > 0:
        s.h1_size_pt = float(v)
    v = take("h1_bold")
    if isinstance(v, bool):
        s.h1_bold = v
    v = take("h1_alignment")
    if isinstance(v, (list, tuple)) and v:
        # 规则允许一组可接受值（如 ['center', 'left']），写回取第一个。
        s.h1_align = str(v[0])
    v = take("h1_spacing")
    if isinstance(v, dict):
        if v.get("before_pt") is not None:
            s.h1_before_pt = float(v["before_pt"])
        if v.get("after_pt") is not None:
            s.h1_after_pt = float(v["after_pt"])

    v = take("h2_cjk_font")
    if isinstance(v, dict) and v.get("eastAsia"):
        s.h2_cjk_font = str(v["eastAsia"])
    v = take("h2_font_size")
    if isinstance(v, (int, float)) and v > 0:
        s.h2_size_pt = float(v)
    v = take("h2_bold")
    if isinstance(v, bool):
        s.h2_bold = v

    v = take("caption_font_size")
    if isinstance(v, (int, float)) and v > 0:
        s.caption_size_pt = float(v)

    v = take("page_size")
    if isinstance(v, dict):
        if v.get("width_cm"):
            s.page_width_cm = float(v["width_cm"])
        if v.get("height_cm"):
            s.page_height_cm = float(v["height_cm"])
    v = take("page_margin")
    if isinstance(v, dict):
        for src, dst in (
            ("top_cm", "margin_top_cm"), ("bottom_cm", "margin_bottom_cm"),
            ("left_cm", "margin_left_cm"), ("right_cm", "margin_right_cm"),
            ("header_cm", "header_cm"), ("footer_cm", "footer_cm"),
        ):
            if v.get(src) is not None:
                setattr(s, dst, float(v[src]))
    v = take("doc_default_font")
    if isinstance(v, dict) and v.get("size_pt"):
        # 文档默认字体跟着正文走，但不覆盖显式给出的正文字体值。
        s.set_doc_default = True

    s.disabled = tuple(disabled)
    # 停用的规则 → 对应字段置为 None（"不改"）。这一步是**保证**，不是优化：
    # 少了它，"关掉 body_font_size"会静默变成"把字号改成方案默认的 12 磅"——
    # 检查不报了，文件却被改了。
    for rule_id in disabled:
        for attr in _RULE_TO_FIELDS.get(rule_id, ()):
            setattr(s, attr, None)
    return s


def scheme_from_template(path: str | Path) -> tuple[FormatScheme, list[str]]:
    """**从一份 .docx 模板里推断方案** —— 手上有学校模板时最省事的用法。

    返回 ``(方案, 说明)``。读的是模板自己的 Normal / Heading 1-3 样式与页面设置，
    所以"把我的论文改成和这份模板一样"就是两行命令。

    推断的诚实边界：**模板里没写的东西不猜**。某个样式没定义字号，就沿用方案默认值，
    并在说明里讲清楚是继承了默认还是读到了模板值——否则作者会以为整套格式都对齐了。
    """
    from docx import Document
    from docx.shared import Cm, Pt

    notes: list[str] = []
    s = FormatScheme()
    doc = Document(str(path))

    def style_of(name: str):
        try:
            return doc.styles[name]
        except KeyError:
            return None

    def read_style(style, label: str, *, font_attr: str, size_attr: str) -> None:
        if style is None:
            notes.append(f"模板里没有「{label}」样式，{label}沿用通用默认值")
            return
        east = None
        try:
            rpr = style.element.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rPr"
            )
            if rpr is not None:
                rf = rpr.find(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}rFonts"
                )
                if rf is not None:
                    east = rf.get(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}eastAsia"
                    )
        except Exception:  # noqa: BLE001 - 读不到就退回默认，不阻断
            east = None
        if east:
            setattr(s, font_attr, east)
        else:
            # **模板里没写中文字体时要明说。** python-docx 的 ``font.name = "仿宋"``
            # 只设 ``w:ascii``/``w:hAnsi``，**不设 ``w:eastAsia``**——所以
            # "用代码设过字体的模板"看起来是仿宋，中文实际仍走默认值。
            # 不说明的话，作者会以为整套格式都对齐了。
            notes.append(
                f"模板的「{label}」样式没有指定**中文字体**（只有西文字体），"
                f"{label}中文字体沿用通用默认值「{getattr(s, font_attr)}」。"
                "若模板来自 Word，请确认它的中文字体确实设过。"
            )
        size = getattr(style.font, "size", None)
        if size is not None:
            setattr(s, size_attr, float(size.pt))
        elif size is None:
            notes.append(f"模板的「{label}」样式没有指定字号，沿用通用默认值 {getattr(s, size_attr):g} 磅")
        if style.font.bold is not None and hasattr(s, font_attr.replace("font", "bold")):
            try:
                setattr(s, font_attr.replace("font", "bold"), bool(style.font.bold))
            except Exception:  # noqa: BLE001
                pass

    read_style(style_of("Normal"), "Normal", font_attr="body_cjk_font", size_attr="body_size_pt")
    read_style(style_of("Heading 1"), "Heading 1", font_attr="h1_cjk_font", size_attr="h1_size_pt")
    read_style(style_of("Heading 2"), "Heading 2", font_attr="h2_cjk_font", size_attr="h2_size_pt")
    read_style(style_of("Heading 3"), "Heading 3", font_attr="h3_cjk_font", size_attr="h3_size_pt")

    normal = style_of("Normal")
    if normal is not None:
        pf = normal.paragraph_format
        ls = pf.line_spacing
        if isinstance(ls, float) and ls > 0:
            s.body_line_mode, s.body_line_value = "multiple", float(ls)
        elif ls is not None and hasattr(ls, "pt"):
            s.body_line_mode, s.body_line_value = "fixed", float(ls.pt)
        fli = pf.first_line_indent
        if fli is not None and s.body_size_pt:
            # 磅换算成"字符数"：Word 里 1 字符 = 当前字号。
            chars = float(fli.pt) / float(s.body_size_pt)
            if chars > 0:
                s.body_first_line_chars = round(chars, 2)
        # 对齐：模板的 Normal 上**显式写了** w:jc 才采纳。
        # 没写就不动（保持 None = 不改），因为 Word 不写 w:jc 就是左对齐，
        # 那是默认行为而不是"模板要求左对齐"——把默认当要求会改错。
        jc = None
        try:
            ppr = normal.element.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}pPr"
            )
            if ppr is not None:
                jc_el = ppr.find(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}jc"
                )
                jc = jc_el.get(
                    "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}val"
                ) if jc_el is not None else None
        except Exception:  # noqa: BLE001
            jc = None
        if jc:
            s.body_align = "both" if jc in ("both", "distribute") else jc
            notes.append(f"模板的 Normal 样式指定了正文对齐：{s.body_align}")
        else:
            notes.append("模板的 Normal 样式没有显式指定对齐方式，**不动正文对齐**（Word 默认左对齐）")

    if doc.sections:
        sec = doc.sections[0]
        try:
            s.page_width_cm = round(Cm(sec.page_width.cm).cm, 2) if sec.page_width else s.page_width_cm
            s.page_height_cm = round(float(sec.page_height.cm), 2) if sec.page_height else s.page_height_cm
            s.margin_top_cm = round(float(sec.top_margin.cm), 2) if sec.top_margin else s.margin_top_cm
            s.margin_bottom_cm = round(float(sec.bottom_margin.cm), 2) if sec.bottom_margin else s.margin_bottom_cm
            s.margin_left_cm = round(float(sec.left_margin.cm), 2) if sec.left_margin else s.margin_left_cm
            s.margin_right_cm = round(float(sec.right_margin.cm), 2) if sec.right_margin else s.margin_right_cm
        except Exception:  # noqa: BLE001
            notes.append("页面设置读取失败，沿用通用默认值")
    else:
        notes.append("模板里没有节（section），页面设置沿用通用默认值")

    notes.insert(0, f"已从模板读取：{s.describe()}")
    return s, notes


# ---------------------------------------------------------------------------
# OOXML 属性写入
# ---------------------------------------------------------------------------

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: 字号换算：OOXML 用半磅。
def _half_points(pt: float) -> str:
    return str(int(round(float(pt) * 2)))


def _twips(pt: float) -> str:
    return str(int(round(float(pt) * 20)))


#: ``w:rPr`` 里子元素的规范顺序（``CT_RPr`` 的 sequence）。OOXML 是**有序**模式，
#: 顺序错了 Word 有时容忍、有时直接忽略那个属性——这类"写进去了但不生效"最难查，
#: 所以插入时就按规范位置放好，而不是一律 ``append``。
_RPR_ORDER: tuple[str, ...] = (
    "w:rStyle", "w:rFonts", "w:b", "w:bCs", "w:i", "w:iCs", "w:caps", "w:smallCaps",
    "w:strike", "w:dstrike", "w:outline", "w:shadow", "w:emboss", "w:imprint",
    "w:noProof", "w:snapToGrid", "w:vanish", "w:webHidden", "w:color", "w:spacing",
    "w:w", "w:kern", "w:position", "w:sz", "w:szCs", "w:highlight", "w:u", "w:effect",
    "w:bdr", "w:shd", "w:fitText", "w:vertAlign", "w:rtl", "w:cs", "w:em", "w:lang",
    "w:eastAsianLayout", "w:specVanish", "w:oMath",
)


def _rpr_child(rpr, tag: str):
    """取（必要时新建）``w:rPr`` 下的 ``tag`` 子元素，并放在**规范顺序**的位置上。"""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    el = rpr.find(qn(tag))
    if el is not None:
        return el
    el = OxmlElement(tag)
    rank = _RPR_ORDER.index(tag) if tag in _RPR_ORDER else len(_RPR_ORDER)
    for child in rpr:
        ckey = f"w:{child.tag.split('}')[-1]}"
        crank = _RPR_ORDER.index(ckey) if ckey in _RPR_ORDER else len(_RPR_ORDER)
        if crank > rank:
            child.addprevious(el)
            return el
    rpr.append(el)
    return el


def _set_rpr_font(
    rpr,
    *,
    cn: str | None,
    en: str | None,
    size_pt: float | None,
    bold: bool | None = None,
) -> None:
    """在 ``w:rPr`` 上设字体、字号、加粗。

    ``None`` 表示**这一项不改**——规则被停用时就会走到这里。必须真的什么都不做，
    否则"关掉 body_font_size"的结果是字号被改成方案默认的 12 磅：
    检查不报了，文件却被改了，比不关还糟。

    ``bold`` 只有标题会用。为什么必须能在**段落里逐 run** 设加粗：手工排版的标题
    样式名是 ``Normal``，加粗属于直接格式；只改 ``Heading 1`` 样式的话，
    检查器报的 ``h1_bold`` 偏差**改不掉**——又是"报了却改不动"。
    """
    from docx.oxml.ns import qn

    if cn is not None or en is not None:
        rf = _rpr_child(rpr, "w:rFonts")
        if en is not None:
            rf.set(qn("w:ascii"), en)
            rf.set(qn("w:hAnsi"), en)
            rf.set(qn("w:cs"), en)
        # **中文必须走 eastAsia**：只设 ascii 的话 Word 里中文仍按默认字体渲染，
        # 而 python-docx 读回的 font.name 却是设过的值——看起来成功了，实际没生效。
        if cn is not None:
            rf.set(qn("w:eastAsia"), cn)

    if bold is not None:
        # b 与 bCs 成对写：前者管西文，后者管复杂文种。显式写 "0" 才表示"取消加粗"，
        # 省略属性只是"继承上级"——模板样式里带粗时就取消不掉。
        for tag in ("w:b", "w:bCs"):
            _rpr_child(rpr, tag).set(qn("w:val"), "1" if bold else "0")

    if size_pt is None:
        return
    for tag in ("w:sz", "w:szCs"):
        _rpr_child(rpr, tag).set(qn("w:val"), _half_points(size_pt))


def _style_runs(
    paragraph, *, cn: str | None, en: str | None, size_pt: float | None,
    bold: bool | None = None,
) -> int:
    """给段落**以及段落标记**、还有段内**所有含文字的 run** 设字体，返回处理的 run 数。

    两处都要设，缺一不可：

    * **run 的 rPr** 决定每个字符实际怎么显示；
    * **段落标记的 rPr**（``w:pPr/w:rPr``）决定"光标停在这一段但不选任何文字"时
      Word 状态栏与字体框显示什么，也参与格式检查器的解析链。

    第二处是**在真实论文上才发现的**：那一篇的每个段落都带着段落级直接格式
    （``w:pPr/w:rPr eastAsia="仿宋_GB2312"``），run 属性改成宋体之后，
    格式检查器读到的仍是仿宋——**功能看着跑通了、报告也写"成功"，但偏差一条没减**。
    合成夹具里没有段落级 rPr，所以测试全绿而真实文档无效。
    这与"PDF 数字的三种写法"是同一类教训：**格式层的现实只能靠真实语料发现**。

    为什么要逐个 run 设、而不是只改样式：Word 里**直接格式优先于样式**。
    作者从别处粘贴过来的文字常常带着直接格式（比如仿宋 10.5 磅），
    只改样式的话，这些文字看起来完全没变——作者会以为工具没生效。
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    # 段落标记的 rPr
    ppr = paragraph._p.get_or_add_pPr()
    prpr = ppr.find(qn("w:rPr"))
    if prpr is None:
        prpr = OxmlElement("w:rPr")
        ppr.append(prpr)
    _set_rpr_font(prpr, cn=cn, en=en, size_pt=size_pt, bold=bold)

    n = 0
    for run in paragraph.runs:
        if not (run.text or "").strip():
            # 只含图片/换行的 run 不动它：给它设字体会影响图片所在行的行高。
            continue
        rpr = run._element.get_or_add_rPr()
        _set_rpr_font(rpr, cn=cn, en=en, size_pt=size_pt, bold=bold)
        n += 1
    return n


def _set_para_format(
    paragraph,
    *,
    line_mode: str | None,
    line_value: float | None,
    first_line_chars: float | None,
    align: str | None,
    before_pt: float | None = None,
    after_pt: float | None = None,
) -> None:
    """设段落属性。``None`` 一律表示**这一项不改**（规则被停用时会走到这里）。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt

    pf = paragraph.paragraph_format
    if align:
        pf.alignment = {
            "center": WD_ALIGN_PARAGRAPH.CENTER,
            "left": WD_ALIGN_PARAGRAPH.LEFT,
            "right": WD_ALIGN_PARAGRAPH.RIGHT,
            "both": WD_ALIGN_PARAGRAPH.JUSTIFY,
            "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
        }.get(align, WD_ALIGN_PARAGRAPH.JUSTIFY)
    if before_pt is not None:
        pf.space_before = Pt(before_pt)
    if after_pt is not None:
        pf.space_after = Pt(after_pt)

    ppr = paragraph._p.get_or_add_pPr()
    if line_mode is not None and line_value is not None:
        spacing = ppr.find(qn("w:spacing"))
        if spacing is None:
            spacing = OxmlElement("w:spacing")
            ppr.append(spacing)
        if line_mode == "fixed":
            spacing.set(qn("w:line"), _twips(line_value))
            spacing.set(qn("w:lineRule"), "exact")
        else:
            # 倍数行距：``w:line`` 的单位是 1/240 行，1.5 倍 = 360。
            spacing.set(qn("w:line"), str(int(round(float(line_value) * 240))))
            spacing.set(qn("w:lineRule"), "auto")

    if first_line_chars is None:
        return
    # 首行缩进：磅值给不支持字符单位的阅读器兜底，firstLineChars 让 Word
    # 在作者改字号时自动换算。
    pf.first_line_indent = Pt(first_line_chars * 12.0)
    ind = ppr.find(qn("w:ind"))
    if ind is not None:
        ind.set(qn("w:firstLineChars"), str(int(round(first_line_chars * 100))))


# ---------------------------------------------------------------------------
# 正文 / 标题 / 题注的判定
# ---------------------------------------------------------------------------

#: Word 内置标题样式名 → 级别。中文版 Word 显示为"标题 1"，但样式名是 "Heading 1"。
_HEADING_STYLE = {
    "heading 1": 1, "标题 1": 1, "heading1": 1,
    "heading 2": 2, "标题 2": 2, "heading2": 2,
    "heading 3": 3, "标题 3": 3, "heading3": 3,
}

#: 题注段落的前缀（"图 1-1 ……" / "表 2-3 ……"）。
_CAPTION = None


def _heading_level(paragraph) -> int:
    """**只按样式名**判标题（``Normal`` 一律返回 0）。

    这是兜底路径：拿不到收稿阶段的判定结果时才用。正式判定请走
    :func:`detect_heading_levels`——手工排版的标题样式名也是 ``Normal``。
    """
    try:
        name = (paragraph.style.name or "").strip().lower()
    except Exception:  # noqa: BLE001
        return 0
    return _HEADING_STYLE.get(name, 0)


def detect_heading_levels(path: str | Path) -> tuple[dict[int, int], str]:
    """用**收稿阶段同一套判定**算标题级别，返回 ``({段落序号: 级别(1 起)}, 说明)``。

    判定顺序与 ``app.parsing.docx_reader`` 完全一致（那边是检查器的依据）：

    1. 段落自己的 ``w:outlineLvl``（0 基，+1 变 1 基）；
    2. 样式链上的 outlineLvl，或样式名/ID 里的「标题/heading + 数字」；
    3. 编号定义（``numbering.xml``）里该 numId 的 outlineLvl——中文模板大量
       用"第一章/一、"的多级编号带出标题级别，漏掉这一条会整片漏判。

    **复用而不是重写**，是因为两套判定一旦分叉，"检查器报了偏差、写回却认不出
    这段是标题"就会立刻发生——真实文档上已经发生过一次。

    失败（文件读不动）时返回空表，由调用方退回样式名判定并在报告里说明，
    不假装成功。
    """
    levels: dict[int, int] = {}
    try:
        from ..parsing.docx_reader import DocxReadError, read_docx
    except Exception as exc:  # noqa: BLE001 - 导入失败也不该让写回崩掉
        return {}, f"标题判定未能复用收稿模块（{type(exc).__name__}: {exc}）"

    try:
        facts = read_docx(path)
    except DocxReadError as exc:
        return {}, f"标题判定退回样式名：{exc}"
    except Exception as exc:  # noqa: BLE001
        return {}, f"标题判定退回样式名：{type(exc).__name__}: {exc}"

    by_basis: dict[str, int] = {}
    for p in getattr(facts, "paragraphs", []) or []:
        if not getattr(p, "is_heading", False):
            continue
        outline = getattr(p, "outline_level", None)
        if outline is not None:
            lvl = int(outline) + 1
        else:
            lvl = _HEADING_STYLE.get((getattr(p, "style_name", "") or "").strip().lower(), 0)
        if lvl <= 0:
            continue
        levels[int(p.index)] = lvl
        basis = getattr(p, "is_heading_basis", "") or "unknown"
        by_basis[basis] = by_basis.get(basis, 0) + 1

    if not levels:
        note = (
            "没有识别出任何标题（段落的 outlineLvl、样式名、编号定义三处都没有标题信号）——"
            "本篇按纯正文处理，标题级格式不会应用。"
        )
    else:
        detail = "、".join(f"{k} {v} 个" for k, v in sorted(by_basis.items()))
        note = f"识别出 {len(levels)} 个标题（判定依据：{detail}）"
    return levels, note


def _is_caption(paragraph) -> bool:
    import re

    global _CAPTION
    if _CAPTION is None:
        _CAPTION = re.compile(r"^\s*(图|表|Figure|Table)\s*\d+")
    return bool(_CAPTION.match(paragraph.text or ""))


def _set_doc_defaults(doc, sch: FormatScheme) -> bool:
    """改 ``w:docDefaults`` 里的默认字体与字号，返回是否改动成功。

    这一段决定"没被任何样式或直接格式覆盖的文字"长什么样，也影响作者以后在 Word 里
    新输入的内容。只改 Normal 样式不改它，格式检查仍会报 ``doc_default_font`` 偏差——
    **报了却改不掉**，正是本项目最不想留下的半成品状态。

    ``w:docDefaults`` 是 ``styles.xml`` 的根级子元素，python-docx 没有现成 API，
    所以这里直接操作 XML。改不到就返回 False，由调用方在报告里说明，
    不静默当作成功。
    """
    try:
        from docx.oxml.ns import qn

        styles_el = doc.styles.element  # w:styles
        dd = styles_el.find(qn("w:docDefaults"))
        if dd is None:
            return False
        rpr_default = dd.find(qn("w:rPrDefault"))
        if rpr_default is None:
            return False
        rpr = rpr_default.find(qn("w:rPr"))
        if rpr is None:
            from docx.oxml import OxmlElement

            rpr = OxmlElement("w:rPr")
            rpr_default.append(rpr)
        _set_rpr_font(
            rpr,
            cn=sch.body_cjk_font,
            en=sch.body_latin_font,
            size_pt=sch.body_size_pt,
        )
        return True
    except Exception:  # noqa: BLE001 - 改不到就如实说，不假装成功
        return False


def _document_text(doc) -> str:
    """把文档里**所有**文字取出来，用于"一字未改"的比对。

    刻意不仅取 ``doc.paragraphs``：表格与页眉页脚的文字也要纳入比对——
    "只动排版"这条保证必须覆盖整份文件，否则"表格里的字被改了"会漏检。
    """
    parts: list[str] = [p.text for p in doc.paragraphs]
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                parts.extend(p.text for p in cell.paragraphs)
    for sec in doc.sections:
        for hf in (sec.header, sec.footer, sec.first_page_header, sec.first_page_footer,
                   sec.even_page_header, sec.even_page_footer):
            try:
                parts.extend(p.text for p in hf.paragraphs)
            except Exception:  # noqa: BLE001 - 某些文档没有这些部件
                continue
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def inspect_docx(path: str | Path) -> dict:
    """读一份 .docx 的概况（段落数、标题数、是否含表格），供界面显示。

    标题数用 :func:`detect_heading_levels`（与检查器同一套判定）。若这里退回
    "按样式名数"，手工排版的论文会显示"0 个标题"，作者据此会以为工具看错了文件。
    """
    from docx import Document

    doc = Document(str(path))
    paras = list(doc.paragraphs)
    levels, _ = detect_heading_levels(path)
    return {
        "path": str(path),
        "paragraphs": len(paras),
        "headings": len(levels),
        "tables": len(doc.tables),
        "sections": len(doc.sections),
        "chars": sum(len(p.text) for p in paras),
    }


def apply_scheme(
    source: str | Path,
    output: str | Path | None = None,
    *,
    scheme: FormatScheme | None = None,
    backup: bool = True,
) -> WritebackReport:
    """把排版方案写到 ``source``，产出 ``output``（默认覆盖原文件并留 ``.bak``）。

    写完后立刻做两件事：**比对文字是否一字未改**、**确认能重新打开**。
    任一不通过就还原备份并把原因写进报告——绝不留一个"改坏了但看着没事"的文件。
    """
    from docx import Document

    src = Path(source)
    out = Path(output) if output else src
    sch = scheme or FormatScheme()
    rep = WritebackReport(source=str(src), output=str(out), scheme=sch.to_dict())

    if not src.exists():
        rep.errors.append(f"文件不存在：{src}")
        return rep

    # ---- 1) 备份 --------------------------------------------------------
    #
    # **只在原地覆盖时备份**。输出到另一个文件时原文件根本不会被碰，
    # 再生成一个 .bak 只会往作者的目录里丢垃圾文件（而且名字还是
    # "xxx.docx.bak"，看不出是从哪来的）。那种情况下的失败处理是
    # "删掉写坏的输出"，见 ``_restore``。
    in_place = out.resolve() == src.resolve()
    rep.in_place = in_place
    if backup and in_place:
        bak = src.with_suffix(src.suffix + ".bak")
        try:
            shutil.copy2(src, bak)
            rep.backup = str(bak)
        except Exception as exc:  # noqa: BLE001
            rep.errors.append(f"备份失败（{type(exc).__name__}: {exc}），为安全起见未做任何修改")
            return rep
    elif not in_place:
        rep.notes.append(f"输出到新文件，**原文件 {src.name} 不会被改动**（所以不需要备份）。")

    try:
        doc = Document(str(src))
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"无法打开 {src.name}：{type(exc).__name__}: {exc}")
        return rep

    text_before = _document_text(doc)
    rep.paragraphs_before = len(doc.paragraphs)

    #: 标题级别：**必须**用收稿阶段那套判定（见 detect_heading_levels 的说明）。
    #: 退回样式名判定的情况在报告里说明，不让作者以为标题被处理过了。
    reader_levels, level_note = detect_heading_levels(src)
    rep.notes.append(level_note)
    rep.headings_detected = len(reader_levels)
    if not reader_levels:
        rep.notes.append("⚠ 本篇未识别出标题，标题级格式（黑体/三号等）不会被应用。")

    # ---- 2) 样式（让以后新输入的文字也继承正确格式）--------------------
    styles = doc.styles
    for name, cn, size, bold in (
        ("Normal", sch.body_cjk_font, sch.body_size_pt, None),
        ("Heading 1", sch.h1_cjk_font, sch.h1_size_pt, sch.h1_bold),
        ("Heading 2", sch.h2_cjk_font, sch.h2_size_pt, sch.h2_bold),
        ("Heading 3", sch.h3_cjk_font, sch.h3_size_pt, sch.h3_bold),
    ):
        try:
            st = styles[name]
        except KeyError:
            continue
        try:
            rpr = st.element.get_or_add_rPr()
            _set_rpr_font(rpr, cn=cn, en=sch.body_latin_font, size_pt=size)
            from docx.shared import Pt

            if sch.body_latin_font is not None:
                st.font.name = sch.body_latin_font
            if size is not None:
                st.font.size = Pt(size)
            if bold is not None:
                st.font.bold = bool(bold)
            rep.styles_updated.append(name)
        except Exception as exc:  # noqa: BLE001 - 单个样式失败不该中断整篇
            rep.notes.append(f"样式 {name} 更新失败：{type(exc).__name__}: {exc}")

    # ---- 3) 逐段 --------------------------------------------------------
    for idx, para in enumerate(doc.paragraphs):
        text = (para.text or "").strip()
        # 收稿判定优先；它没认出来的再用样式名兜底（例如样式名叫"标题 2"、
        # 但没写 outlineLvl 的段落）。
        level = reader_levels.get(idx) or _heading_level(para)
        if level == 1:
            _set_para_format(para, line_mode=sch.body_line_mode, line_value=sch.body_line_value,
                             first_line_chars=0.0, align=sch.h1_align,
                             before_pt=sch.h1_before_pt, after_pt=sch.h1_after_pt)
            rep.runs_styled += _style_runs(para, cn=sch.h1_cjk_font, en=sch.body_latin_font,
                                           size_pt=sch.h1_size_pt, bold=sch.h1_bold)
            rep.paragraphs_heading += 1
            continue
        if level == 2:
            _set_para_format(para, line_mode=sch.body_line_mode, line_value=sch.body_line_value,
                             first_line_chars=0.0, align=sch.h2_align,
                             before_pt=6.0, after_pt=6.0)
            rep.runs_styled += _style_runs(para, cn=sch.h2_cjk_font, en=sch.body_latin_font,
                                           size_pt=sch.h2_size_pt, bold=sch.h2_bold)
            rep.paragraphs_heading += 1
            continue
        if level >= 3:
            _set_para_format(para, line_mode=sch.body_line_mode, line_value=sch.body_line_value,
                             first_line_chars=0.0, align=sch.h3_align,
                             before_pt=6.0, after_pt=6.0)
            rep.runs_styled += _style_runs(para, cn=sch.h3_cjk_font, en=sch.body_latin_font,
                                           size_pt=sch.h3_size_pt, bold=sch.h3_bold)
            rep.paragraphs_heading += 1
            continue
        if _is_caption(para):
            _set_para_format(para, line_mode="multiple", line_value=1.0,
                             first_line_chars=0.0, align="center")
            rep.runs_styled += _style_runs(para, cn=sch.caption_cjk_font, en=sch.body_latin_font,
                                           size_pt=sch.caption_size_pt)
            rep.paragraphs_caption += 1
            continue
        # 正文（含空段）：空段也设行距，否则段间会突出一块
        _set_para_format(para, line_mode=sch.body_line_mode, line_value=sch.body_line_value,
                         first_line_chars=sch.body_first_line_chars if text else 0.0,
                         align=sch.body_align)
        if text:
            rep.runs_styled += _style_runs(para, cn=sch.body_cjk_font, en=sch.body_latin_font,
                                           size_pt=sch.body_size_pt)
            rep.paragraphs_body += 1

    # ---- 4) 页面 --------------------------------------------------------
    from docx.shared import Cm

    for sec in doc.sections:
        try:
            # 逐个判 None：停用 page_size 就不该动纸张，停用 page_margin 就不该动边距。
            for attr, setter in (
                ("page_width_cm", "page_width"), ("page_height_cm", "page_height"),
                ("margin_top_cm", "top_margin"), ("margin_bottom_cm", "bottom_margin"),
                ("margin_left_cm", "left_margin"), ("margin_right_cm", "right_margin"),
                ("header_cm", "header_distance"), ("footer_cm", "footer_distance"),
            ):
                value = getattr(sch, attr)
                if value is None:
                    continue
                setattr(sec, setter, Cm(value))
            rep.sections_updated += 1
        except Exception as exc:  # noqa: BLE001
            rep.notes.append(f"页面设置更新失败：{type(exc).__name__}: {exc}")

    # ---- 4.5) 文档默认字体 ------------------------------------------------
    #
    # ``w:docDefaults`` 决定"没有显式指定格式的文字"长什么样，也影响 Word 里
    # 以后新输入的内容。只改 Normal 样式不改它，格式检查仍会报 doc_default_font 偏差。
    if sch.set_doc_default:
        if _set_doc_defaults(doc, sch):
            rep.styles_updated.append("docDefaults")

    # 表格里段落数（未处理，如实报出）
    rep.skipped_in_tables = sum(len(c.paragraphs) for t in doc.tables for r in t.rows for c in r.cells)

    # ---- 5) 保存 + 验证 -------------------------------------------------
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(out))
    except Exception as exc:  # noqa: BLE001
        rep.errors.append(f"保存失败：{type(exc).__name__}: {exc}")
        return _restore(rep, src)

    try:
        again = Document(str(out))
    except Exception as exc:  # noqa: BLE001 - 打不开就是失败，不能"大概没事"
        rep.errors.append(f"写回后的文件无法重新打开：{type(exc).__name__}: {exc}")
        return _restore(rep, src)

    rep.reopened_ok = True
    rep.paragraphs_after = len(again.paragraphs)
    text_after = _document_text(again)
    rep.text_unchanged = text_after == text_before
    if not rep.text_unchanged:
        rep.errors.append(
            "**文字内容发生了变化**——写回只应改排版，不该动文字。已还原备份。"
            "若你确实需要改文字，请用文本修复（`run -s fix`）或改写命令。"
        )
        return _restore(rep, src)
    if rep.paragraphs_after != rep.paragraphs_before:
        rep.notes.append(
            f"⚠ 段落数从 {rep.paragraphs_before} 变成 {rep.paragraphs_after}，"
            "文字虽一致但结构可能有变，请打开核对。"
        )

    rep.ok = True
    if rep.skipped_in_tables:
        rep.notes.append(
            f"表格里有 {rep.skipped_in_tables} 段文字**未改动**：表格通常要五号/单倍行距，"
            "比正文小一号，统一按正文改反而错。需要的话请在 Word 里单独调表格样式。"
        )
    rep.notes.append("只改了排版属性，**正文内容一字未改**（已逐字比对通过）。")
    return rep


def _restore(rep: WritebackReport, src: Path) -> WritebackReport:
    """验证失败时回到原样。**失败不能留半个改坏的文件。**

    两种情形分开处理：

    * 原地覆盖：从 ``.bak`` 拷回去（"原样"就是源文件本身）。
    * 输出到新文件：源文件本来就没动，把**写坏的输出删掉**即可——
      不然作者目录里会多出一个看着像成品、其实损坏的 .docx。
    """
    if rep.backup and Path(rep.backup).exists():
        try:
            shutil.copy2(rep.backup, src)
            rep.restored = True
            rep.notes.append(f"已从备份还原：{rep.backup}")
        except Exception as exc:  # noqa: BLE001
            rep.notes.append(f"⚠ 还原失败：{type(exc).__name__}: {exc}；原文件备份在 {rep.backup}")
    elif not rep.in_place:
        out = Path(rep.output)
        try:
            if out.exists() and out.resolve() != Path(rep.source).resolve():
                out.unlink()
                rep.restored = True
                rep.notes.append(f"已删除写坏的输出文件：{out}（原文件未被改动）")
        except Exception as exc:  # noqa: BLE001
            rep.notes.append(f"⚠ 写坏的输出文件删不掉：{type(exc).__name__}: {exc}；请手动删除 {out}")
    rep.ok = False
    return rep
