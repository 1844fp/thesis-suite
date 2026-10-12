"""Word (.docx) 导出。

为什么必须有
------------
全站原先只能导 Markdown。但学术写作的交付物就是 .docx——导师批注、学校格式要求、
查重系统上传，走的都是 Word。让人把 Markdown 再手工转一遍，等于把工具链断在最后一步。

设计取舍
--------
* **用 python-docx 直接构造**，不经过 pandoc 之类的外部程序：容器里没有那种依赖，
  而且我们要控制中文字体与标题层级（学校格式检查第一步就是看标题层级）。
* **标题用 Word 内置的 Heading 样式**，不手动设字号。原因：内置样式是"结构化"的，
  学校模板替换样式后能整体生效，手动字号则会变成一堆硬编码属性，模板一换全乱。
* **中西文字体分开设**。中文文档只设 ``font.name`` 不够——那对中文无效，
  必须同时设 ``w:eastAsia``。这是中文 Word 文档最常见的坑。
"""

from __future__ import annotations

import io
import re
from typing import Any

#: 中文字号 -> 磅值。学位论文常见要求（小三 = 15pt 等）。
SIZE_BY_NAME = {
    "初号": 42, "小初": 36, "一号": 26, "小一": 24, "二号": 22, "小二": 18,
    "三号": 16, "小三": 15, "四号": 14, "小四": 12, "五号": 10.5, "小五": 9,
}


def _set_run_font(run, *, ascii_font: str, east_font: str, size_pt: float | None = None,
                  bold: bool | None = None) -> None:
    """设置一段文字的字体。

    **必须同时设 eastAsia**：python-docx 的 ``font.name`` 只作用于西文，
    中文字符会退回 Word 默认字体，于是中文论文导出来字体不对。
    这是中文 Word 生成最常见的一个坑。
    """
    from docx.oxml.ns import qn

    run.font.name = ascii_font
    rpr = run._element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), east_font)
    rfonts.set(qn("w:ascii"), ascii_font)
    rfonts.set(qn("w:hAnsi"), ascii_font)
    if size_pt is not None:
        run.font.size = _pt(size_pt)
    if bold is not None:
        run.bold = bold


def _pt(value: float):
    from docx.shared import Pt

    return Pt(value)


def _style_heading(doc, level: int, *, east_font: str, ascii_font: str) -> None:
    """把 Word 内置 Heading N 样式调成中文论文常见的观感。

    改的是**样式**而不是段落，这样学校模板替换样式时能整体生效。
    """
    try:
        style = doc.styles[f"Heading {level}"]
    except KeyError:
        return
    sizes = {1: 16, 2: 14, 3: 12}
    size = sizes.get(level, 12)
    style.font.name = ascii_font
    style.font.size = _pt(size)
    style.font.bold = True
    # 中文要额外设 eastAsia
    try:
        from docx.oxml.ns import qn

        rpr = style.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rfonts)
        rfonts.set(qn("w:eastAsia"), east_font)
    except Exception:  # noqa: BLE001 - 样式细节失败不该阻断导出
        pass


def _prepare_styles(doc, *, east_font: str, ascii_font: str,
                    heading_font: str, body_size: float) -> None:
    """给一个文档设好正文与标题样式。

    抽成函数是因为"标题 + 正文"两文档拼装的写法需要**在最终文档上也设一遍**样式——
    样式是文档级的，不随内容元素一起搬过去。这个细节如果忘了，
    有标题的导出会变成"标题对的、正文全部落回 Times New Roman"。
    """
    from docx.oxml.ns import qn

    normal = doc.styles["Normal"]
    normal.font.name = ascii_font
    normal.font.size = _pt(body_size)
    try:
        rpr = normal.element.get_or_add_rPr()
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is None:
            rfonts = rpr.makeelement(qn("w:rFonts"), {})
            rpr.append(rfonts)
        rfonts.set(qn("w:eastAsia"), east_font)
    except Exception:  # noqa: BLE001 - 样式细节失败不该阻断导出
        pass
    normal.paragraph_format.line_spacing = 1.5

    for level in (1, 2, 3):
        _style_heading(doc, level, east_font=heading_font, ascii_font=ascii_font)


def markdown_to_docx(
    markdown: str,
    *,
    title: str = "",
    east_font: str = "宋体",
    ascii_font: str = "Times New Roman",
    heading_font: str = "黑体",
    body_size: float = 12.0,        # 小四
) -> bytes:
    """把 Markdown 渲染成 .docx，返回字节。

    只处理学术文档真正会用到的语法：``#``~``####`` 标题、段落、``-`` 列表、
    ``1.`` 有序列表、``>`` 引用、``|`` 表格、``**粗体**``、``[n]`` 引用角标保留原样。
    刻意不做完整 Markdown——那需要引入解析器，而多余语法的渲染差异对本项目没有价值。
    """
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    # 先把正文写进临时文档，最后再拼"标题 + 正文"。
    #
    # 为什么不直接先写标题：python-docx 没有"往文档开头插段落"的 API，只能在末尾追加。
    # 早先我试过往 doc.paragraphs 里 insert XML 元素，既难读又容易写错。
    # 用两个文档拼装最直白，也不依赖任何 XML 细节。
    body_doc = Document()
    _prepare_styles(body_doc, east_font=east_font, ascii_font=ascii_font,
                    heading_font=heading_font, body_size=body_size)
    doc = body_doc

    lines = markdown.split("\n")
    i = 0
    in_code = False

    def add_body(text: str) -> None:
        """写一个正文段落，处理 ``**粗体**`` 与行内 ``code``。"""
        para = doc.add_paragraph()
        para.paragraph_format.line_spacing = 1.5
        para.paragraph_format.first_line_indent = _pt(body_size * 2)   # 首行缩进 2 字
        for piece in re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
            if not piece:
                continue
            if piece.startswith("**") and piece.endswith("**"):
                run = para.add_run(piece[2:-2])
                _set_run_font(run, ascii_font=ascii_font, east_font=heading_font,
                              size_pt=body_size, bold=True)
            elif piece.startswith("`") and piece.endswith("`"):
                run = para.add_run(piece[1:-1])
                _set_run_font(run, ascii_font="Consolas", east_font=east_font, size_pt=body_size)
            else:
                run = para.add_run(piece)
                _set_run_font(run, ascii_font=ascii_font, east_font=east_font, size_pt=body_size)

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            para = doc.add_paragraph()
            run = para.add_run(line)
            _set_run_font(run, ascii_font="Consolas", east_font=east_font, size_pt=body_size - 1)
            i += 1
            continue

        if not stripped:
            i += 1
            continue

        # 表格：连续的 | 行
        if stripped.startswith("|") and stripped.endswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            rows = []
            for row in block:
                cells = [c.strip() for c in row.strip("|").split("|")]
                if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                    continue           # 分隔行
                rows.append(cells)
            if rows:
                table = doc.add_table(rows=0, cols=max(len(r) for r in rows))
                table.style = "Table Grid"
                for r in rows:
                    cells = table.add_row().cells
                    for j, val in enumerate(r[: len(cells)]):
                        cells[j].text = ""
                        run = cells[j].paragraphs[0].add_run(val)
                        _set_run_font(run, ascii_font=ascii_font, east_font=east_font,
                                      size_pt=body_size - 1)
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if m:
            level = min(len(m.group(1)), 3)
            para = doc.add_heading(level=level)
            run = para.add_run(m.group(2))
            _set_run_font(run, ascii_font=ascii_font, east_font=heading_font,
                          size_pt={1: 16, 2: 14, 3: 12}.get(level, 12), bold=True)
            i += 1
            continue

        if stripped.startswith(">"):
            para = doc.add_paragraph()
            para.paragraph_format.left_indent = _pt(body_size * 2)
            run = para.add_run(stripped.lstrip("> ").strip())
            _set_run_font(run, ascii_font=ascii_font, east_font=east_font,
                          size_pt=body_size - 1)
            run.italic = True
            i += 1
            continue

        m = re.match(r"^([-*+])\s+(.*)$", stripped)
        if m:
            para = doc.add_paragraph(style="List Bullet")
            run = para.add_run(m.group(2))
            _set_run_font(run, ascii_font=ascii_font, east_font=east_font, size_pt=body_size)
            i += 1
            continue

        m = re.match(r"^(\d+)[.)]\s+(.*)$", stripped)
        if m:
            para = doc.add_paragraph(style="List Number")
            run = para.add_run(m.group(2))
            _set_run_font(run, ascii_font=ascii_font, east_font=east_font, size_pt=body_size)
            i += 1
            continue

        add_body(stripped)
        i += 1

    if title:
        # 先把标题写进最终文档，再把临时文档的**内容元素**逐个搬过来。
        final = Document()
        # 复用同一套样式设置：直接把样式从 body_doc 复制不现实，
        # 所以标题文档重新设一遍（下面 _prepare_styles 抽成函数）。
        _prepare_styles(final, east_font=east_font, ascii_font=ascii_font,
                        heading_font=heading_font, body_size=body_size)
        para = final.add_paragraph()
        para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = para.add_run(title)
        _set_run_font(run, ascii_font=ascii_font, east_font=heading_font, size_pt=18, bold=True)

        for element in list(body_doc.element.body):
            # 跳过 sectPr：那是节属性（页面设置），搬过去会多出一个空节
            if element.tag.endswith("}sectPr"):
                continue
            final.element.body.append(element)

        buf = io.BytesIO()
        final.save(buf)
        return buf.getvalue()

    buf = io.BytesIO()
    body_doc.save(buf)
    return buf.getvalue()


def draft_to_markdown(draft: dict[str, Any], *, include_coverage: bool = True) -> str:
    """把稿件结构渲染成 Markdown（供转 Word 或直接下载）。

    与 ``api/writing.py`` 的导出保持一致，但抽到这里是因为它现在有两个调用方
    （Markdown 下载与 Word 转换），逻辑重复迟早会走样。
    """
    parts: list[str] = []
    if draft.get("title"):
        parts.append(f"# {draft['title']}")
        parts.append("")
    for section in draft.get("sections") or []:
        parts.append(f"## {section.get('heading') or ''}")
        parts.append("")
        parts.append(str(section.get("body") or "").strip())
        parts.append("")

    coverage = draft.get("coverage") or {}
    if include_coverage and coverage:
        parts.append("---")
        parts.append("")
        parts.append("## 依据覆盖度（由系统统计，不是评价）")
        parts.append("")
        parts.append(f"- 章节数：{coverage.get('sections', 0)}")
        parts.append(f"- 引用总数：{coverage.get('citations_total', 0)}"
                     f"（你的资料 {coverage.get('citations_user', 0)}，"
                     f"联网文献 {coverage.get('citations_web', 0)}）")
        parts.append(f"- 编造的引用编号：{coverage.get('citations_invalid', 0)}")
        parts.append(f"- 找不到依据的数字：{coverage.get('ungrounded_numbers', 0)}")
        parts.append(f"- 结论：{coverage.get('verdict', '')}")
    if draft.get("notice"):
        parts.append("")
        parts.append(f"> 生成提示：{draft['notice']}")
    return "\n".join(parts)
