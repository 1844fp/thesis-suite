"""把生成的 Markdown 稿子写成一份可以交出去的 .docx。

## 这一层的产出标准是什么

不是"能打开就行"，而是**打开之后作者只需要按学校模板微调，不需要重新排版**：

* A4 页面、正文宋体小四、1.5 倍行距、首行缩进 2 字符（``w:firstLineChars=200``，
  这样在 Word 里改字号时缩进会跟着变，而不是固定成一个死数值）；
* 一级标题黑体三号居中，二级黑体四号，三级黑体小四——并**使用 Word 的内置标题样式**，
  这样导航窗格与自动目录能直接认出来（用直接格式化的"假标题"在 Word 里做不出目录）；
* 参考文献区另起一页、五号字、条目不加首行缩进；
* ``【待作者补充：…】`` 这类待办标记**加粗标红**。这不是装饰：一份三万字初稿里
  如果待补处和正文长得一样，作者根本找不到自己该补哪里。

## 一条不变的纪律：导出后必须验一遍

写完之后立刻用 python-docx **把文件重新打开**，核对段落数与标题数，并确认能读出
排版属性。这与 ``app/format`` 里"改完文档仍能正常打开"是同一条纪律：
排版写坏了而没人发现，代价远高于多验一次。

## 默认格式来自哪里

来自中文学位论文的通行排版惯例（宋体正文／黑体标题／1.5 倍行距／A4）。
**它不是任何一所学校的具体模板**，所以导出报告里会写明这一点，并提示作者对照
自己的模板调整。真正的模板对齐要在 ``app/format`` 拿到学校官方 .docx 之后做。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["ExportResult", "export_docx", "verify_docx", "DEFAULT_SPEC"]

#: 待办标记：这些是"这句话还没有依据/需要作者处理"的地方，导出时必须显眼。
_PENDING = re.compile(r"【[^】]{0,200}】|［[^］]{0,200}］")
#: 行内粗体标记 ``**x**``。
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HEAD = re.compile(r"^(#{1,4})[ \t]+(.+?)[ \t]*$")
_REF_HEADING = re.compile(r"^#+[ \t]*(参考文献|references)\s*$", re.I)
_REF_ITEM = re.compile(r"^\s*[\[［]\s*\d{1,3}\s*[\]］]")
#: 编号/项目符号行：这些行不该有首行缩进（缩进会让编号看起来错位）。
_LIST_ITEM = re.compile(r"^\s*(?:[-*•]|\d{1,3}\s*[.、)]|[（(]\s*\d{1,3}\s*[)）])\s+")


@dataclass(frozen=True)
class DocxSpec:
    """排版参数。集中在一处，便于将来按学校模板改。"""

    #: 正文
    body_font_cn: str = "宋体"
    body_font_en: str = "Times New Roman"
    body_size_pt: float = 12.0          # 小四
    body_line_spacing: float = 1.5
    #: 标题（一级／二级／三级）
    heading_font: str = "黑体"
    h1_size_pt: float = 16.0            # 三号
    h2_size_pt: float = 14.0            # 四号
    h3_size_pt: float = 12.0            # 小四
    #: 参考文献
    ref_size_pt: float = 10.5           # 五号
    #: 页面（厘米）
    page_width_cm: float = 21.0
    page_height_cm: float = 29.7
    margin_top_cm: float = 2.54
    margin_bottom_cm: float = 2.54
    margin_left_cm: float = 3.17
    margin_right_cm: float = 3.17


DEFAULT_SPEC = DocxSpec()


@dataclass
class ExportResult:
    path: str = ""
    ok: bool = False
    paragraphs: int = 0
    headings: int = 0
    pending_marks: int = 0
    references: int = 0
    chars: int = 0
    notes: list[str] = field(default_factory=list)
    verification: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "paragraphs": self.paragraphs,
            "headings": self.headings,
            "pending_marks": self.pending_marks,
            "references": self.references,
            "chars": self.chars,
            "notes": list(self.notes),
            "verification": self.verification,
        }


def _require_docx():
    try:
        import docx  # noqa: PLC0415 - 只在真正导出时才需要
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "缺少 python-docx，无法导出 .docx。请确认运行环境带上了 "
            "aigc-reducer\\libs 的 PYTHONPATH（用 run_python.cmd 运行即可）。"
        ) from exc
    return docx


def _set_cjk_font(rpr_owner, *, cn: str, en: str, size_pt: float) -> None:
    """同时设置西文字体与中文（eastAsia）字体。

    只设 ``font.name`` 是常见的坑：Word 里中文会走 ``w:eastAsia``，
    不设它就会用默认的宋体渲染中文，于是"黑体标题"看起来根本没生效——
    而这一点在 python-docx 读回属性时也看不出来（读到的 name 是对的）。
    """
    from docx.oxml.ns import qn
    from docx.shared import Pt

    font = rpr_owner.font
    font.name = en
    font.size = Pt(size_pt)
    rpr = rpr_owner.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        from docx.oxml import OxmlElement

        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), en)
    rfonts.set(qn("w:hAnsi"), en)
    rfonts.set(qn("w:eastAsia"), cn)


def _style_section(doc, spec: DocxSpec) -> None:
    from docx.shared import Cm

    for section in doc.sections:
        section.page_width = Cm(spec.page_width_cm)
        section.page_height = Cm(spec.page_height_cm)
        section.top_margin = Cm(spec.margin_top_cm)
        section.bottom_margin = Cm(spec.margin_bottom_cm)
        section.left_margin = Cm(spec.margin_left_cm)
        section.right_margin = Cm(spec.margin_right_cm)


def _style_body(doc, spec: DocxSpec) -> None:
    _set_cjk_font(doc.styles["Normal"], cn=spec.body_font_cn, en=spec.body_font_en, size_pt=spec.body_size_pt)
    pf = doc.styles["Normal"].paragraph_format
    pf.line_spacing = spec.body_line_spacing
    pf.space_before = None
    pf.space_after = None


def _style_headings(doc, spec: DocxSpec) -> None:
    """让内置标题样式既有正确的字体，又保持 Word 可识别的"标题"身份。"""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    for name, size, align, before, after in (
        ("Heading 1", spec.h1_size_pt, WD_ALIGN_PARAGRAPH.CENTER, 18, 12),
        ("Heading 2", spec.h2_size_pt, WD_ALIGN_PARAGRAPH.LEFT, 12, 6),
        ("Heading 3", spec.h3_size_pt, WD_ALIGN_PARAGRAPH.LEFT, 6, 6),
    ):
        try:
            style = doc.styles[name]
        except KeyError:  # pragma: no cover - 模板异常时不该让导出失败
            continue
        _set_cjk_font(style, cn=spec.heading_font, en=spec.body_font_en, size_pt=size)
        style.font.bold = True
        # 内置标题样式默认是主题蓝。论文标题必须是黑色——留着蓝色会被学校模板判为格式错误。
        try:
            from docx.shared import RGBColor

            style.font.color.rgb = RGBColor(0, 0, 0)
        except Exception:  # noqa: BLE001
            pass
        pf = style.paragraph_format
        pf.alignment = align
        pf.space_before = Pt(before)
        pf.space_after = Pt(after)
        pf.line_spacing = 1.5
        pf.first_line_indent = Pt(0)


def _indent_first_line(paragraph, chars: int = 2) -> None:
    """首行缩进 2 字符。

    ``w:firstLineChars`` 是"按字符数缩进"，Word 会随字号自动换算；只设
    ``first_line_indent``（磅值）在作者把字号从 12pt 改成 14pt 后就偏了。
    两个都设：``firstLineChars`` 管 Word 里的正确显示，磅值给不支持字符单位的
    阅读器（LibreOffice 等）兜底。
    """
    from docx.oxml.ns import qn
    from docx.shared import Pt

    pf = paragraph.paragraph_format
    pf.first_line_indent = Pt(DEFAULT_SPEC.body_size_pt * chars)
    pPr = paragraph._p.pPr
    if pPr is None:  # pragma: no cover - 上面必然创建了 pPr
        return
    ind = pPr.find(qn("w:ind"))
    if ind is not None:
        ind.set(qn("w:firstLineChars"), str(chars * 100))


def _add_runs(paragraph, text: str, *, size_pt: float, cn: str, en: str) -> int:
    """按 ``**粗体**`` 与 ``【待办】`` 把一行切成若干 run，返回待办标记个数。

    待办标记加粗标红。**这是本模块最要紧的一处格式**：三万字初稿里的
    「【待作者补充：2024 年数据】」如果和正文长得一样，作者找不到自己该补哪里，
    而"哪里没有依据"正是这份稿子最需要被看见的信息。
    """
    from docx.shared import RGBColor

    pending = 0
    # 先按待办标记切，再在每一段内部处理粗体标记。
    for chunk in re.split(r"(【[^】]{0,200}】|［[^］]{0,200}］)", text):
        if not chunk:
            continue
        is_pending = bool(_PENDING.fullmatch(chunk))
        for piece in re.split(r"(\*\*.+?\*\*)", chunk):
            if not piece:
                continue
            bold_md = bool(_BOLD.fullmatch(piece))
            body = piece[2:-2] if bold_md else piece
            if not body:
                continue
            run = paragraph.add_run(body)
            _set_run_font(run, cn=cn, en=en, size_pt=size_pt)
            if bold_md or is_pending:
                run.bold = True
            if is_pending:
                run.font.color.rgb = RGBColor(0xC0, 0x00, 0x00)
                pending += 1
    return pending


def _set_run_font(run, *, cn: str, en: str, size_pt: float) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = en
    run.font.size = Pt(size_pt)
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        from docx.oxml import OxmlElement

        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
    rfonts.set(qn("w:ascii"), en)
    rfonts.set(qn("w:hAnsi"), en)
    rfonts.set(qn("w:eastAsia"), cn)


def export_docx(
    text: str,
    output_path: str | Path,
    *,
    title: str = "",
    author: str = "",
    spec: DocxSpec | None = None,
    page_break_before_references: bool = True,
) -> ExportResult:
    """把 Markdown 稿子写成 .docx，并自动验证写出的文件能被正常打开。"""
    docx = _require_docx()
    spec = spec or DEFAULT_SPEC
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    doc = docx.Document()
    _style_section(doc, spec)
    _style_body(doc, spec)
    _style_headings(doc, spec)

    result = ExportResult(path=str(out))
    chars = 0
    in_references = False

    # 封面标题：只有作者没在正文里写标题时才补，避免出现两个标题。
    body_lines = [ln.rstrip() for ln in (text or "").split("\n")]
    has_h1_title = any(_HEAD.match(ln) and len(_HEAD.match(ln).group(2)) > 6 for ln in body_lines[:5])
    if title and not has_h1_title:
        h = doc.add_heading(title, level=1)
        h.alignment = 1  # 居中
        if author:
            p = doc.add_paragraph()
            run = p.add_run(author)
            _set_run_font(run, cn=spec.body_font_cn, en=spec.body_font_en, size_pt=spec.body_size_pt)
            p.alignment = 1
        result.notes.append("正文里没有一级标题，已用项目标题补了封面标题。")

    for raw in body_lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        chars += len(re.sub(r"\s", "", line))

        m = _HEAD.match(line)
        if m:
            level = min(len(m.group(1)), 4)
            heading_text = m.group(2).strip()
            if _REF_HEADING.match(line):
                in_references = True
                if page_break_before_references:
                    doc.add_page_break()
            target_level = min(level, 3)
            p = doc.add_heading("", level=target_level)
            _add_runs(p, heading_text, size_pt=(spec.h1_size_pt, spec.h2_size_pt, spec.h3_size_pt)[target_level - 1],
                      cn=spec.heading_font, en=spec.body_font_en)
            continue

        size = spec.ref_size_pt if in_references else spec.body_size_pt
        p = doc.add_paragraph()
        result.pending_marks += _add_runs(p, line.strip(), size_pt=size, cn=spec.body_font_cn, en=spec.body_font_en)

        if in_references and _REF_ITEM.match(line):
            result.references += 1
            # 参考文献条目不缩进，用悬挂缩进让续行对齐。
            from docx.shared import Pt

            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.left_indent = Pt(21)
            p.paragraph_format.line_spacing = 1.25
        elif _LIST_ITEM.match(line):
            p.paragraph_format.first_line_indent = Pt(0)
            p.paragraph_format.left_indent = Pt(0)
        else:
            _indent_first_line(p, 2)

    if not in_references:
        result.notes.append("稿件里没有「参考文献」区。定稿前需要补上文献表（见 CLI 的 references 步骤）。")
    if result.pending_marks:
        result.notes.append(
            f"稿中有 {result.pending_marks} 处待办标记（【待作者补充：…】等），已在 Word 里加粗标红，"
            "请逐一处理后再交稿。"
        )
    result.notes.append(
        "排版采用中文学位论文的通行惯例（宋体正文／黑体标题／1.5 倍行距／A4），"
        "**不是任何一所学校的具体模板**；请对照本校模板微调后再提交。"
    )

    doc.save(str(out))
    result.chars = chars

    # ---- 写完立刻验一遍：能重新打开 + 内容对得上 ----
    verdict = verify_docx(out, expect_paragraphs=None)
    result.verification = verdict
    result.paragraphs = int(verdict.get("paragraphs") or 0)
    result.headings = int(verdict.get("headings") or 0)
    result.ok = bool(verdict.get("ok"))
    if not result.ok:
        result.notes.append(f"⚠ 导出的文件未能通过自检：{verdict.get('error') or verdict}")
    return result


def verify_docx(path: str | Path, *, expect_paragraphs: int | None = None) -> dict:
    """把 .docx 重新打开并核对基本结构。

    **导出的最后一步必须是这个**，而不是"写完了就返回成功"：
    python-docx 在排版属性写错时可能仍然成功保存，但 Word 打开会报错或丢内容。
    作者拿到一份打不开的稿子，比拿到一份排版丑的稿子糟糕得多。
    """
    p = Path(path)
    out: dict = {"path": str(p), "ok": False, "error": "", "paragraphs": 0, "headings": 0,
                 "chars": 0, "body_font": "", "body_size_pt": 0.0, "line_spacing": None}
    if not p.exists():
        out["error"] = "文件不存在"
        return out
    if p.stat().st_size == 0:
        out["error"] = "文件为空"
        return out
    try:
        from docx import Document

        doc = Document(str(p))
    except Exception as exc:  # noqa: BLE001 - 打不开就是失败，不能当成"大概没事"
        out["error"] = f"重新打开失败：{type(exc).__name__}: {exc}"
        return out

    paras = list(doc.paragraphs)
    out["paragraphs"] = len(paras)
    out["headings"] = sum(1 for x in paras if (x.style.name or "").startswith("Heading"))
    out["chars"] = sum(len(x.text) for x in paras)
    try:
        normal = doc.styles["Normal"]
        out["body_font"] = str(normal.font.name or "")
        out["body_size_pt"] = float(normal.font.size.pt) if normal.font.size else 0.0
        out["line_spacing"] = normal.paragraph_format.line_spacing
    except Exception:  # noqa: BLE001 - 属性读不到不算失败，只是报告少几个字段
        pass

    if expect_paragraphs is not None and len(paras) != expect_paragraphs:
        out["error"] = f"段落数不符：期望 {expect_paragraphs}，实际 {len(paras)}"
        return out
    out["ok"] = True
    return out
