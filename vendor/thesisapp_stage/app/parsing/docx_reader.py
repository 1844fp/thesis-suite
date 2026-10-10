"""Word（.docx）结构与格式事实读取。

这个模块只做一件事：**把 .docx 里"评审真的会看"的排版事实抽成纯数据**，
不做任何判断，也不改文件。判断留给 ``app.format.rules``。

为什么要有这一层（而不是让检查器自己开 docx）：

1. 检查器不能依赖 python-docx 对象。检查器拿到的是文本和上下文，还可能在没有
   docx 的场景（纯文本稿）里跑。事实先落成可 JSON 序列化的 dict 存进
   ``project.meta["format_facts"]``，检查器再从 dict 重建——这样"读文档"和
   "判规则"解耦，规则也就能离线单测。
2. 字体/字号/行距/缩进这些属性无法从纯文本恢复。只读 ``paragraph.text``
   的检查器对格式问题是全盲的，却会给出"没发现问题"的假结论，比报错更糟。

三个容易做错的地方，这里都按最保守的方式处理：

* **中文字体看 ``w:eastAsia``，不看 ascii。** 中文论文的正文字体几乎只写在
  eastAsia 上，只看 ascii 会一律读成 Calibri/Times New Roman 并误报。
* **标题判定三层兜底，并把依据记下来。** Word 中文模板的样式名是「标题 1」，
  英文模板是 "Heading 1"，还有模板干脆用自定义样式名——唯一与命名无关的信号
  是段落自己的 ``<w:outlineLvl>``。只按样式名判定会漏掉大半中文论文。依据
  写进 ``is_heading_basis`` 是为了能向作者解释"你这段为什么被判成标题"。
* **字号做样式继承解析。** OOXML 的字号可以只写在文档默认值里，段落与样式都
  不写。只看段落自身 rPr 会把整篇论文的字号读成 None，字号规则全部静默失效。
  继承顺序固定为 文档默认 → 样式链 → 段落标记 → run。

表格文字、页眉页脚、脚注只计数，不混进正文段落列表：它们的格式规范与正文不同
（表格允许小一号字），混在一起会造出一批假问题。
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.oxml.ns import qn

from ..framework.artifact import text_hash

#: OOXML 长度单位：1 英寸 = 914400 EMU，1 cm = 360000 EMU。
EMU_PER_CM = 360000.0
#: 半磅 → 磅
HALF_POINT = 2.0

#: 正文中文习惯用全角标点；这几个半角标点夹在中文之间几乎总是错。
HALFWIDTH_IN_CJK = "，。；：！？、（）"
#: 行尾空白
TRAILING_SPACE_RE = re.compile(r"[ \t\u3000]+$")
#: 中文之间多打的空格（中文-空格-中文）。中英文之间的空格是规范要求的，不算问题。
CJK_SPACE_CJK_RE = re.compile(r"([\u4e00-\u9fff])[ \t]+([\u4e00-\u9fff])")
#: 中英文之间缺空格。
MISSING_CJK_LATIN_SPACE_RE = re.compile(
    r"[\u4e00-\u9fff][A-Za-z0-9]|[A-Za-z0-9][\u4e00-\u9fff]"
)
#: 弯引号
CURLY_QUOTE_RE = re.compile(r"[\u201c\u201d\u2018\u2019]")
#: 参考文献条目编号：`[1]` / `[1,2]` / `1.` / `1、` / `（1）`
REF_NUMBER_RE = re.compile(r"^\s*(?:\[\d+(?:[-,，]\d+)*\]|\(\d+\)|（\d+）|\d+[.、])")
#: 年份（参考文献条目里必须能看出年份）
REF_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
#: 图表题注编号：图 1 / 图1-1 / 图1.1 / 表2-3
CAPTION_LABEL_RE = re.compile(r"^\s*(图|表|Figure|Table|Fig\.?)\s*\d")
#: 拉丁字母与数字（用于"是否含西文"判定）
LATIN_RE = re.compile(r"[A-Za-z0-9]")

#: .docx 是 zip；旧版 .doc 是 OLE2 复合文档。先看魔数，才能给出有用的报错。
_ZIP_MAGIC = b"PK"
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
#: 加密的 OOXML 也是 OLE2 容器，靠这段流名区分"加密"与"旧版格式"。
_ENCRYPTED_MARK = b"EncryptedPackage"


class DocxReadError(RuntimeError):
    """读不了这个文档。消息面向使用者，必须能直接告诉他怎么办。"""


# --------------------------------------------------------------------------
# 数据类
# --------------------------------------------------------------------------
@dataclass
class RunInfo:
    """一个 run 的格式快照。

    只留检查器真正会比对的属性，不留整个 rPr。rPr 里 90% 是主题色和
    兼容性开关，全存下来会让落盘的 facts 体积失控，而没人会去看。
    """

    text: str = ""
    bold: bool | None = None
    italic: bool | None = None
    size_pt: float | None = None
    font_name_ascii: str = ""
    font_name_eastasia: str = ""
    color_rgb: str = ""
    has_break: bool = False
    has_image: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class Paragraph:
    """正文段落（``document.paragraphs`` 的顶层段落）。

    ``index`` 是**在顶层段落列表里的下标**，与 ``Document.paragraphs`` 一一对应。
    检查器报问题时用它定位，人工也能照着数到第几段。
    """

    index: int
    text: str = ""
    style_name: str = ""
    style_id: str = ""
    outline_level: int | None = None
    is_heading: bool = False
    #: 标题判定依据：outline_lvl / style_name / style_id / none。
    #: 记下来是因为判定结果要能向作者解释——"这段为什么算标题"必须有答案。
    is_heading_basis: str = ""
    align: str = "left"
    kind: str = "body"
    #: 题注编号种类：figure / table / ""。即使 kind 不是 caption 也填，
    #: 因为"图题在下、表题在上"的规则靠它，而 kind 的判定更保守。
    caption_label: str = ""
    runs: list[RunInfo] = field(default_factory=list)
    #: 段落样式链解析后的（中文）字体与字号，供字号/字体规则直接比对。
    east_asia_font: str = ""
    ascii_font: str = ""
    size_pt: float | None = None
    bold: bool | None = None
    line_spacing_mode: str = ""      # multiple | fixed | at_least | ""
    line_spacing_value: float | None = None
    first_line_indent_chars: float | None = None
    first_line_indent_pt: float | None = None
    space_before_pt: float = 0.0
    space_after_pt: float = 0.0
    has_break: bool = False
    has_image: bool = False

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def as_dict(self) -> dict:
        d = asdict(self)
        d["runs"] = [r.as_dict() if isinstance(r, RunInfo) else r for r in self.runs]
        return d


@dataclass
class SectionInfo:
    index: int = 0
    page_width_cm: float = 0.0
    page_height_cm: float = 0.0
    top_cm: float = 0.0
    bottom_cm: float = 0.0
    left_cm: float = 0.0
    right_cm: float = 0.0
    header_cm: float = 0.0
    footer_cm: float = 0.0
    orientation: str = "portrait"
    start_type: str = ""
    header_text: str = ""
    footer_text: str = ""

    @property
    def is_a4(self) -> bool:
        """A4 的容差取 0.2cm：Word 里 21cm×29.7cm 常存成 20.99/29.69。"""
        return (
            abs(self.page_width_cm - 21.0) < 0.2
            and abs(self.page_height_cm - 29.7) < 0.2
        )

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class StyleFact:
    """样式表里的一条记录。

    ``based_on`` 保留样式链是为了让字号/字体的继承解析可复算——facts 落盘后
    检查器只有数据没有 XML，链必须在读取阶段就解析出来并写进来。
    """

    id: str = ""
    name: str = ""
    type: str = ""          # paragraph | character | table | numbering
    based_on: str = ""
    font_ascii: str = ""
    font_east_asia: str = ""
    size_pt: float | None = None
    bold: bool | None = None
    line_spacing_mode: str = ""
    line_spacing_value: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class DocxFormatFacts:
    """一份 .docx 的格式事实全集。

    这是"读"与"判"之间唯一的接口：``rules.evaluate(facts, ruleset)`` 只认它。
    段落列表**只含正文顶层段落**；表格/页眉页脚/脚注的文字只出现在计数与
    ``header_footer_texts`` 里。
    """

    path: str = ""
    paragraphs: list[Paragraph] = field(default_factory=list)
    sections: list[SectionInfo] = field(default_factory=list)
    styles: list[StyleFact] = field(default_factory=list)
    #: 文档默认值（docDefaults）解析后的中文字体/西文字体/字号。继承链的末端。
    doc_default_east_asia: str = ""
    doc_default_ascii: str = ""
    doc_default_size_pt: float | None = None
    header_footer_texts: list[str] = field(default_factory=list)
    table_count: int = 0
    table_paragraph_count: int = 0
    image_count: int = 0
    footnote_ref_count: int = 0
    #: 脚注文本（word/footnotes.xml）。python-docx 不解析脚注，这里是直接读 XML。
    footnote_texts: list[str] = field(default_factory=list)
    total_chars: int = 0
    cjk_char_count: int = 0
    #: 读取这份事实时，文档正文文本的哈希（``framework.artifact.text_hash``）。
    #: 存在的理由：facts 由收稿阶段写入 ``project.meta``，而稿子之后可能被
    #: 修改过。段落下标是按当时的文本编的，稿子一变，下标就对不上了。
    #: 有哈希才能明确地说"这份格式事实已经过期"，而不是把问题指向错误的段落。
    source_text_hash: str = ""
    warnings: list[str] = field(default_factory=list)

    # -- 派生视图（不参与序列化，随用随算） --------------------------------
    @property
    def body_paragraphs(self) -> list[Paragraph]:
        """可供正文格式规则比对的段落。

        排除空段、标题、图表题注、参考文献条目：它们的格式规范与正文不同，
        一起比会造出一批"字号不对"的假问题。
        """
        return [
            p
            for p in self.paragraphs
            if p.kind == "body" and not p.is_empty and p.text.strip()
        ]

    @property
    def headings(self) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.is_heading]

    @property
    def captions(self) -> list[Paragraph]:
        """带编号的图表题注，按段落下标排序。"""
        return [p for p in self.paragraphs if p.caption_label]

    @property
    def references(self) -> list[Paragraph]:
        return [p for p in self.paragraphs if p.kind == "reference"]

    @property
    def styles_by_id(self) -> dict[str, StyleFact]:
        return {s.id: s for s in self.styles if s.id}

    @property
    def styles_by_name(self) -> dict[str, StyleFact]:
        return {s.name: s for s in self.styles if s.name}

    @property
    def first_section(self) -> SectionInfo | None:
        return self.sections[0] if self.sections else None

    def text(self) -> str:
        """正文段落拼成的纯文本。

        空段落**保留**为空行：检查器用下标定位段落，过滤掉空段会让下标整体漂移。
        """
        return "\n".join(p.text for p in self.paragraphs)

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "paragraphs": [p.as_dict() for p in self.paragraphs],
            "sections": [s.as_dict() for s in self.sections],
            "styles": [s.as_dict() for s in self.styles],
            "doc_default_east_asia": self.doc_default_east_asia,
            "doc_default_ascii": self.doc_default_ascii,
            "doc_default_size_pt": self.doc_default_size_pt,
            "header_footer_texts": list(self.header_footer_texts),
            "table_count": self.table_count,
            "table_paragraph_count": self.table_paragraph_count,
            "image_count": self.image_count,
            "footnote_ref_count": self.footnote_ref_count,
            "footnote_texts": list(self.footnote_texts),
            "total_chars": self.total_chars,
            "cjk_char_count": self.cjk_char_count,
            "source_text_hash": self.source_text_hash,
            "warnings": list(self.warnings),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "DocxFormatFacts":
        """从 ``project.meta["format_facts"]`` 里的 dict 重建。

        走这条路而不是叫检查器自己开文档，是为了让检查器保持纯函数式的
        `(text, ctx) -> list[Issue]`，也让规则引擎可以完全离线单测。
        """
        raw = raw or {}

        def _coerce(klass, data: dict):
            """只取数据类认识的键，多余的忽略——facts 是外部写入的，不能盲信。"""
            known = {f.name for f in fields(klass)}
            return klass(**{k: v for k, v in (data or {}).items() if k in known})

        paras: list[Paragraph] = []
        for item in raw.get("paragraphs") or []:
            item = dict(item or {})
            runs = [_coerce(RunInfo, r) for r in (item.get("runs") or [])]
            p = _coerce(Paragraph, item)
            p.runs = runs
            paras.append(p)

        return cls(
            path=str(raw.get("path") or ""),
            paragraphs=paras,
            sections=[_coerce(SectionInfo, s) for s in (raw.get("sections") or [])],
            styles=[_coerce(StyleFact, s) for s in (raw.get("styles") or [])],
            doc_default_east_asia=str(raw.get("doc_default_east_asia") or ""),
            doc_default_ascii=str(raw.get("doc_default_ascii") or ""),
            doc_default_size_pt=raw.get("doc_default_size_pt"),
            header_footer_texts=list(raw.get("header_footer_texts") or []),
            table_count=int(raw.get("table_count") or 0),
            table_paragraph_count=int(raw.get("table_paragraph_count") or 0),
            image_count=int(raw.get("image_count") or 0),
            footnote_ref_count=int(raw.get("footnote_ref_count") or 0),
            footnote_texts=list(raw.get("footnote_texts") or []),
            total_chars=int(raw.get("total_chars") or 0),
            cjk_char_count=int(raw.get("cjk_char_count") or 0),
            source_text_hash=str(raw.get("source_text_hash") or ""),
            warnings=list(raw.get("warnings") or []),
        )


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _attr(el, name: str) -> str:
    """读 ``w:val`` 风格属性。lxml 的 XmlString 不是 str 的子类，必须显式转换。"""
    if el is None:
        return ""
    v = el.get(qn(name))
    return str(v) if v is not None else ""


def _cm(emu: Any) -> float:
    try:
        return round(float(emu) / EMU_PER_CM, 3)
    except (TypeError, ValueError):
        return 0.0


def _twips_to_pt(twips: Any) -> float:
    """``w:spacing`` 用二十分之一磅（twips）。"""
    try:
        return round(float(twips) / 20.0, 2)
    except (TypeError, ValueError):
        return 0.0


def _half_pt_to_pt(value: Any) -> float | None:
    """``w:sz`` / ``w:szCs`` 是半磅。val 缺失（表示"自动"）时返回 None 而不是 0。

    返回 0 会被下游当成"字号是 0 磅"，是个不可能值，只会制造混乱。
    """
    if value is None or value == "":
        return None
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return round(n / HALF_POINT, 2)


def _bool_val(el) -> bool | None:
    """``w:b`` 无 val 或 val 为 true/1/on 都是"开"。"""
    if el is None:
        return None
    v = _attr(el, "w:val").strip().lower()
    if v in ("", "true", "1", "on"):
        return True
    if v in ("false", "0", "off"):
        return False
    return None


def _fonts(rPr) -> tuple[str, str]:
    """返回 (ascii 字体, eastAsia 字体)。

    中文字体必须从 ``w:eastAsia`` 取：中文论文正文字体基本只写在这一项上，
    读 ascii 会一律得到 Calibri/Times New Roman 并据此误报。

    eastAsia 缺失时才退回 ``w:cs``（复杂文种字体）。这个退让是有理由的：
    部分中文模板（尤其是从 PDF 转换来的）只写了 cs，宁可多读一层也不要
    把字体读成空——空值会让字体规则整条静默失效。
    """
    if rPr is None:
        return "", ""
    f = rPr.find(qn("w:rFonts"))
    if f is None:
        return "", ""
    ascii_name = _attr(f, "w:ascii")
    ea_name = _attr(f, "w:eastAsia") or _attr(f, "w:cs")
    return ascii_name, ea_name


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (
        0x3400 <= o <= 0x4DBF
        or 0x4E00 <= o <= 0x9FFF
        or 0xF900 <= o <= 0xFAFF
        or 0xFF00 <= o <= 0xFFEF
    )


def short_evidence(text: str, limit: int = 80) -> str:
    """把证据截到能放进报告的长度。截断加省略号，不做任何清理。"""
    t = (text or "").strip()
    return t if len(t) <= limit else t[: limit - 1] + "…"


# --------------------------------------------------------------------------
# 样式与继承解析
# --------------------------------------------------------------------------
def _theme_fonts(path: Path) -> dict[str, str]:
    """从 ``word/theme/theme1.xml`` 解析主题字体表。

    为什么非做不可：Word 新建的文档里，docDefaults 往往**只写主题引用**
    （``w:asciiTheme="minorHAnsi" w:eastAsiaTheme="minorEastAsia"``），
    真正的字体名在主题里。不解析主题就会把整篇论文的字体读成空字符串，
    字体规则随即静默失效——"检查通过"和"压根没检查"在这里长得一模一样，
    这是最危险的一种 bug。

    主题字体表优先取 ``<a:ea>``（东亚字体）。中小学术模板的主题里
    ``<a:latin>`` 是 Calibri、``<a:ea>`` 才是宋体/等线，取错等于没取。
    """
    try:
        with zipfile.ZipFile(path) as z:
            raw = z.read("word/theme/theme1.xml")
    except (zipfile.BadZipFile, OSError, KeyError):
        return {}

    from lxml import etree

    ns = {
        "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
        "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    }
    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return {}

    out: dict[str, str] = {}
    for scheme in root.findall(".//a:fontScheme", ns):
        scheme_name = scheme.get("name") or ""
        for role_tag, role in (("a:majorFont", "major"), ("a:minorFont", "minor")):
            node = scheme.find(role_tag, ns)
            if node is None:
                continue
            latin = node.find("a:latin", ns)
            ea = node.find("a:ea", ns)
            latin_name = (latin.get("typeface") or "") if latin is not None else ""
            ea_name = (ea.get("typeface") or "") if ea is not None else ""
            # 主题名的字面量：majorEastAsia / minorHAnsi …（Word 按此拼写引用）
            out[f"{role}EastAsia"] = ea_name
            out[f"{role}Ascii"] = latin_name
            out[f"{role}HAnsi"] = latin_name
            out[f"{role}Bidi"] = latin_name
        if scheme_name:
            out.setdefault("scheme", scheme_name)
    return out


def _resolve_theme_fonts(root, theme: dict[str, str]) -> None:
    """就地（在 XML 上）把 ``w:*Theme`` 引用换成真实字体名。

    直接改 XML 而不是在读取时层层判空，是因为"主题引用"会出现在
    styles.xml、段落 pPr、run rPr 三个层级；在解析阶段统一抹平，
    下游的继承逻辑就只需要处理一种情况。
    """
    if not theme or root is None:
        return
    for rf in root.iter(qn("w:rFonts")):
        for attr, theme_key in (
            ("w:asciiTheme", "Ascii"),
            ("w:hAnsiTheme", "HAnsi"),
            ("w:eastAsiaTheme", "EastAsia"),
            ("w:cstheme", "Bidi"),
        ):
            ref = rf.get(qn(attr))
            if not ref:
                continue
            name = theme.get(ref) or theme.get(f"minor{theme_key}") or ""
            if not name:
                continue
            target = {
                "w:asciiTheme": "w:ascii",
                "w:hAnsiTheme": "w:hAnsi",
                "w:eastAsiaTheme": "w:eastAsia",
                "w:cstheme": "w:cs",
            }[attr]
            if not rf.get(qn(target)):
                rf.set(qn(target), name)


class _StyleResolver:
    """解析 OOXML 的样式继承链。

    不做这件事的代价：字号只写在 docDefaults 或样式上的文档，会被读成
    "所有段落都没有字号"，于是字号规则全部静默失效——检查器报告"无问题"，
    而论文其实全篇字号都错。这类假阴性比误报难发现得多。

    继承顺序（后写覆盖前写）：
        文档默认值 → 段落样式链（basedOn）→ 段落自身 pPr → run 自身 rPr
    """

    def __init__(self, doc, *, theme: dict[str, str] | None = None) -> None:
        self._doc = doc
        self._styles_root = None
        try:
            self._styles_root = doc.styles.element
        except Exception:  # pragma: no cover - 极少数文档没有 styles part
            self._styles_root = None
        self._theme = theme or {}
        # 主题字体必须在索引之前抹平：否则继承链读到的是主题引用而不是字体名。
        _resolve_theme_fonts(self._styles_root, self._theme)
        _resolve_theme_fonts(getattr(doc.element, "body", None), self._theme)
        self._style_els: dict[str, Any] = {}
        self._style_names: dict[str, str] = {}
        self._style_types: dict[str, str] = {}
        self._index()

    # -- 索引 -------------------------------------------------------------
    def _index(self) -> None:
        if self._styles_root is None:
            return
        for st in self._styles_root.findall(qn("w:style")):
            sid = _attr(st, "w:styleId")
            if not sid:
                continue
            self._style_els[sid] = st
            nm = st.find(qn("w:name"))
            self._style_names[sid] = _attr(nm, "w:val") if nm is not None else ""
            self._style_types[sid] = _attr(st, "w:type")

    def style_ids(self) -> list[str]:
        return list(self._style_els)

    def style_name(self, sid: str) -> str:
        return self._style_names.get(sid, "")

    def style_type(self, sid: str) -> str:
        return self._style_types.get(sid, "")

    def style_chain(self, sid: str) -> list[str]:
        """``basedOn`` 链，自身在前。

        带环路保护：真实文档里出现过 A→B→A 的坏样式表，无限循环会让整个
        收稿流程挂死在一个格式问题上。
        """
        out: list[str] = []
        seen: set[str] = set()
        cur = sid
        while cur and cur not in seen:
            seen.add(cur)
            out.append(cur)
            el = self._style_els.get(cur)
            if el is None:
                break
            based = el.find(qn("w:basedOn"))
            cur = _attr(based, "w:val") if based is not None else ""
        return out

    # -- 文档默认值 -------------------------------------------------------
    def doc_default_rpr(self):
        if self._styles_root is None:
            return None
        dd = self._styles_root.find(qn("w:docDefaults"))
        if dd is None:
            return None
        rd = dd.find(qn("w:rPrDefault"))
        if rd is None:
            return None
        return rd.find(qn("w:rPr"))

    def doc_default_ppr(self):
        """``pPrDefault``。

        段落默认值也必须看：Word 常常只在这里写 ``w:spacing``（行距），
        段落和样式上都不写。少了这一层，行距会被读成"未设置"，
        而"行距不符合规范"恰恰是评审必扣的一项。
        """
        if self._styles_root is None:
            return None
        dd = self._styles_root.find(qn("w:docDefaults"))
        if dd is None:
            return None
        pd = dd.find(qn("w:pPrDefault"))
        if pd is None:
            return None
        return pd.find(qn("w:pPr"))

    # -- 属性取值 ---------------------------------------------------------
    def effective_outline_level(self, sid: str) -> int | None:
        """从样式链取大纲级别。

        很多学校模板把 outlineLvl 写在「标题 1」样式定义里，段落自身没有；
        只看段落属性会把这些标题整片漏掉。取样式链上**最先出现**的那个，
        与 Word 的"近者优先"一致。
        """
        for chain_id in self.style_chain(sid):
            pPr = self.style_ppr(chain_id)
            if pPr is None:
                continue
            lvl = pPr.find(qn("w:outlineLvl"))
            if lvl is None:
                continue
            try:
                return int(_attr(lvl, "w:val"))
            except (TypeError, ValueError):
                continue
        return None

    def style_rpr(self, sid: str):
        el = self._style_els.get(sid)
        return el.find(qn("w:rPr")) if el is not None else None

    def style_ppr(self, sid: str):
        el = self._style_els.get(sid)
        return el.find(qn("w:pPr")) if el is not None else None

    def effective(
        self,
        key: str,
        *,
        paragraph=None,
        run=None,
        para_style_id: str = "",
    ) -> str:
        """按继承顺序取一个直接写在元素上的属性值（原始字符串）。

        这里刻意只做"元素上写了没有"的查找，不做数值换算：换算规则各异
        （sz 是半磅、spacing 是 twips、ind 是字符的百分之一），放在调用处
        更清楚。
        """
        # 1) run 自身
        if run is not None:
            rPr = run.find(qn("w:rPr")) if run.tag != qn("w:rPr") else run
            if rPr is not None:
                el = rPr.find(qn(key))
                if el is not None:
                    return _attr(el, "w:val") or "__present__"
        # 2) 段落标记属性（pPr/rPr）
        if paragraph is not None:
            pPr = paragraph.find(qn("w:pPr"))
            if pPr is not None:
                rPr = pPr.find(qn("w:rPr"))
                if rPr is not None:
                    el = rPr.find(qn(key))
                    if el is not None:
                        return _attr(el, "w:val") or "__present__"
        # 3) 段落样式链
        for sid in self.style_chain(para_style_id):
            rPr = self.style_rpr(sid)
            if rPr is None:
                continue
            el = rPr.find(qn(key))
            if el is not None:
                return _attr(el, "w:val") or "__present__"
        # 4) 文档默认值
        rPr = self.doc_default_rpr()
        if rPr is not None:
            el = rPr.find(qn(key))
            if el is not None:
                return _attr(el, "w:val") or "__present__"
        return ""

    def effective_font_element(self, *, paragraph=None, run=None, para_style_id: str = ""):
        """返回最先生效的 ``w:rFonts`` 元素（不是值，因为要分开取 ascii/eastAsia）。"""
        if run is not None:
            rPr = run.find(qn("w:rPr")) if run.tag != qn("w:rPr") else run
            if rPr is not None:
                el = rPr.find(qn("w:rFonts"))
                if el is not None:
                    return el
        if paragraph is not None:
            pPr = paragraph.find(qn("w:pPr"))
            if pPr is not None:
                rPr = pPr.find(qn("w:rPr"))
                if rPr is not None:
                    el = rPr.find(qn("w:rFonts"))
                    if el is not None:
                        return el
        for sid in self.style_chain(para_style_id):
            rPr = self.style_rpr(sid)
            if rPr is None:
                continue
            el = rPr.find(qn("w:rFonts"))
            if el is not None:
                return el
        rPr = self.doc_default_rpr()
        if rPr is not None:
            return rPr.find(qn("w:rFonts"))
        return None

    def effective_ppr_value(self, key: str, pPr, para_style_id: str = "") -> str:
        """段落属性（缩进/间距/对齐）取 effective 相同的优先级。"""
        if pPr is not None:
            el = pPr.find(qn(key))
            if el is not None:
                return "__present__"
        for sid in self.style_chain(para_style_id):
            spPr = self.style_ppr(sid)
            if spPr is not None and spPr.find(qn(key)) is not None:
                return "__present__"
        return ""

    def effective_ppr_element(self, key: str, pPr, para_style_id: str = ""):
        """返回最先生效的段落属性子元素（如 ``w:ind`` / ``w:spacing``）。

        最后一层是 ``docDefaults`` 的 pPrDefault：段落与样式都没写时，
        Word 用的是文档默认行距，检查器必须看到同一个值。
        """
        if pPr is not None:
            el = pPr.find(qn(key))
            if el is not None:
                return el
        for sid in self.style_chain(para_style_id):
            spPr = self.style_ppr(sid)
            if spPr is None:
                continue
            el = spPr.find(qn(key))
            if el is not None:
                return el
        dpPr = self.doc_default_ppr()
        if dpPr is not None:
            return dpPr.find(qn(key))
        return None

    def effective_fonts(self, *, paragraph=None, run=None, para_style_id: str = "") -> tuple[str, str]:
        el = self.effective_font_element(
            paragraph=paragraph, run=run, para_style_id=para_style_id
        )
        if el is None:
            return "", ""
        return _attr(el, "w:ascii"), _attr(el, "w:eastAsia")

    def effective_size_pt(self, *, paragraph=None, run=None, para_style_id: str = "") -> float | None:
        """``w:sz`` 优先，缺失时退回 ``w:szCs``。

        szCs 管的是复杂文种（阿拉伯语等），Word 在部分模板里只写 szCs。
        宁可读它也不要返回 None——返回 None 会让字号规则整条失效。
        """
        for key in ("w:sz", "w:szCs"):
            v = self.effective(key, paragraph=paragraph, run=run, para_style_id=para_style_id)
            if v and v != "__present__":
                pt = _half_pt_to_pt(v)
                if pt is not None:
                    return pt
        return None

    def effective_bold(self, *, paragraph=None, run=None, para_style_id: str = "") -> bool | None:
        """加粗判定。

        按 OOXML 规范，toggle 属性（b/i）在继承时是异或合并的；但 Word 实际
        行为是"链上任一处打开即为粗体"。这里跟随 Word 的实际行为——规范条文
        与用户看到的样子不一致时，以用户看到的为准。
        """
        found = False
        for key in ("w:b", "w:bCs"):
            v = self.effective(key, paragraph=paragraph, run=run, para_style_id=para_style_id)
            if not v:
                continue
            if v == "__present__":
                found = True
            else:
                b = _bool_val(_make_val_el(key, v))
                if b:
                    found = True
        return True if found else None

    def effective_line_spacing(self, pPr, para_style_id: str = "") -> tuple[str, float | None]:
        """解析 ``w:spacing/@w:line``，返回 (模式, 数值)。

        **``w:line`` 的单位与 lineRule 无关，永远是二十分之一磅（twips）。**
        只有 ``lineRule="auto"`` 时它才是"单倍行距的倍数 × 240"，此时除以
        240 得到倍数；``exact`` / ``atLeast`` 下它就是字面的高度，除以 20
        得到磅值。这个区分不做对，固定值行距会被读成 11 倍这种荒唐值，
        行距规则随即对整篇论文误报。
        """
        el = self.effective_ppr_element("w:spacing", pPr, para_style_id)
        if el is None:
            return "", None
        line = _attr(el, "w:line")
        rule = _attr(el, "w:lineRule") or "auto"
        if not line:
            return "", None
        try:
            n = float(line)
        except ValueError:
            return "", None
        if rule == "auto":
            return "multiple", round(n / 240.0, 3)
        if rule == "atLeast":
            return "at_least", round(n / 20.0, 2)
        # exact 与其它未知规则都按"字面高度 1/20 磅"处理。
        return ("fixed" if rule == "exact" else rule), round(n / 20.0, 2)

    def space_before_after(self, pPr, para_style_id: str = "") -> tuple[float, float]:
        el = self.effective_ppr_element("w:spacing", pPr, para_style_id)
        if el is None:
            return 0.0, 0.0
        return _twips_to_pt(_attr(el, "w:before")), _twips_to_pt(_attr(el, "w:after"))

    def first_line_indent(self, pPr, para_style_id: str = "") -> tuple[float | None, float | None]:
        """返回（字符数, 磅值）。

        中文规范说"首行缩进 2 字符"，而 OOXML 里既可能是
        ``w:firstLineChars="200"``（百分之一字符），也可能是
        ``w:firstLine="480"``（twips）。两个都解析出来，规则才不用猜单位。
        """
        el = self.effective_ppr_element("w:ind", pPr, para_style_id)
        if el is None:
            return None, None
        chars = _attr(el, "w:firstLineChars") or _attr(el, "w:firstLine_chars")
        first = _attr(el, "w:firstLine")
        chars_f = round(float(chars) / 100.0, 2) if chars else None
        pt_f = _twips_to_pt(first) if first else None
        return chars_f, pt_f

    def build_style_facts(self) -> list[StyleFact]:
        out: list[StyleFact] = []
        for sid in self.style_ids():
            el = self._style_els[sid]
            rPr = el.find(qn("w:rPr"))
            based = el.find(qn("w:basedOn"))
            ascii_f, ea_f = _fonts(rPr)
            mode, value = self.effective_line_spacing(
                el.find(qn("w:pPr")), sid
            )
            out.append(
                StyleFact(
                    id=sid,
                    name=self.style_name(sid),
                    type=self.style_type(sid),
                    based_on=_attr(based, "w:val") if based is not None else "",
                    font_ascii=ascii_f,
                    font_east_asia=ea_f,
                    size_pt=_half_pt_to_pt(_attr(rPr.find(qn("w:sz")), "w:val")) if rPr is not None and rPr.find(qn("w:sz")) is not None else None,
                    bold=_bool_val(rPr.find(qn("w:b"))) if rPr is not None else None,
                    line_spacing_mode=mode,
                    line_spacing_value=value,
                )
            )
        return out


def _make_val_el(tag: str, val: str):
    """把 "true"/"false" 字符串包成一个可被 ``_bool_val`` 解析的假元素。

    这样 ``_bool_val`` 只有一处实现，不用为"来自 effective() 的字符串"
    再写一份等价逻辑。
    """
    from lxml import etree

    el = etree.Element(qn(tag))
    el.set(qn("w:val"), str(val))
    return el


# --------------------------------------------------------------------------
# 段落解析
# --------------------------------------------------------------------------
def _iter_run_text(r) -> str:
    """把一个 ``w:r`` 的可见文字拼出来。

    ``w:br`` 与 ``w:tab`` 用 ``\\n`` / ``\\t`` 占位，是因为
    ``paragraph.text`` 也是这么做的；两者不一致会让"段落文本"和"run 文本"
    对不上，检查器按文本找证据就会找不到。
    """
    parts: list[str] = []
    for child in r.iter():
        tag = child.tag
        if tag == qn("w:t"):
            parts.append(child.text or "")
        elif tag == qn("w:tab"):
            parts.append("\t")
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
    return "".join(parts)


def _is_reference_heading(text: str) -> bool:
    """参考文献章节标题。

    只认短标题（≤20 字）并且**必须以「参考文献」开头**。正文里
    "见参考文献[1]的讨论"这类句子虽然含关键词，但不会被误判成新章节。
    """
    t = (text or "").strip().rstrip("：:")
    if len(t) > 20:
        return False
    if t.startswith("参考文献") or t.startswith("引用文献"):
        return True
    return t.lower() in ("references", "bibliography", "reference")


def _looks_like_body_again(text: str) -> bool:
    """判断参考文献区是否已经结束（后面跟的是致谢/附录/正文）。

    三条依据都是"参考文献条目不会长这样"：条目标注了年份、通常带编号、
    多半在 300 字以内。宁可早一点退出参考文献区，也不要把附录当条目——
    对附录套用条目规则会连着报一堆假问题。
    """
    t = (text or "").strip()
    if not t:
        return False
    if len(t) > 300:
        return True
    has_number = bool(REF_NUMBER_RE.match(t))
    has_year = bool(REF_YEAR_RE.search(t))
    return not has_number and not has_year


def _paragraph_kind(
    *, text: str, is_heading: bool, caption_label: str, in_reference_zone: bool, has_image: bool
) -> str:
    """段落分类。

    顺序即优先级。标题排第一，因为它有权威信号（outlineLvl），不该被文本
    特征覆盖。题注只在文本明显像编号题注时才算 ``caption``——正文里
    "如图 3-2 所示"提到图号的句子不能算题注，否则"图题在下"这条规则会
    对每一句提及图号的正文报错。
    """
    if is_heading:
        return "heading"
    if not text.strip():
        return "empty"
    if has_image and not LATIN_RE.search(text) and len(text.strip()) <= 30:
        return "image"
    if caption_label and len(text.strip()) <= 60:
        return "caption"
    if in_reference_zone or REF_NUMBER_RE.match(text):
        return "reference"
    return "body"


def _caption_label(text: str) -> str:
    m = CAPTION_LABEL_RE.match(text or "")
    if not m:
        return ""
    label = m.group(1)
    if label in ("图",) or label.lower().startswith("fig"):
        return "figure"
    return "table"


def _count_drawings(p_el) -> int:
    """段落里挂了几张图。

    同时数 ``w:drawing``（新式图形容器）与 ``w:pict``（旧式 VML），
    以及 ``w:object``（嵌入 OLE）。只数 drawing 会漏掉从旧模板粘过来的图。
    """
    n = 0
    for tag in ("w:drawing", "w:pict", "w:object"):
        n += len(p_el.findall(f".//{qn(tag)}"))
    return n


def _numbering_outline_levels(path: Path) -> dict[int, int]:
    """从 ``word/numbering.xml`` 解析"用样式产生的标题"。

    国内学位论文模板里很常见的一种做法：标题用的是正文样式 + 多级列表，
    大纲级别只写在 ``w:numPr`` 引用的编号定义里，段落和样式上都没有。
    只看样式与段落的 outlineLvl 会把这类标题整片漏掉，而漏掉标题会连带
    让"标题字体/字号"这类规则全部不生效。

    返回 {numId: outlineLvl}，约定一个 numId 只取它第 0 级的定义。
    """
    try:
        with zipfile.ZipFile(path) as z:
            if "word/numbering.xml" not in z.namelist():
                return {}
            raw = z.read("word/numbering.xml")
    except (zipfile.BadZipFile, OSError, KeyError):
        return {}

    from lxml import etree

    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return {}

    def _int(v: Any) -> int | None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    abstract: dict[str, int] = {}
    for an in root.findall(qn("w:abstractNum")):
        aid = _attr(an, "w:abstractNumId")
        for lvl in an.findall(qn("w:lvl")):
            if _int(_attr(lvl, "w:ilvl")) != 0:
                continue
            ol = lvl.find(qn("w:outlineLvl"))
            if ol is not None:
                v = _int(_attr(ol, "w:val"))
                if v is not None:
                    abstract[aid] = v
            break

    out: dict[int, int] = {}
    for num in root.findall(qn("w:num")):
        nid = _int(_attr(num, "w:numId"))
        ref = num.find(qn("w:abstractNumId"))
        if nid is None or ref is None:
            continue
        v = abstract.get(_attr(ref, "w:val"))
        if v is not None:
            out[nid] = v
    return out


def _paragraph_num_id(p_el) -> int | None:
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        return None
    numPr = pPr.find(qn("w:numPr"))
    if numPr is None:
        return None
    num_id = numPr.find(qn("w:numId"))
    if num_id is None:
        return None
    try:
        return int(_attr(num_id, "w:val"))
    except (TypeError, ValueError):
        return None


def _build_run_info(resolver: _StyleResolver, r, para_style_id: str, p_el) -> RunInfo:
    rPr = r.find(qn("w:rPr"))
    ascii_f, ea_f = resolver.effective_fonts(
        paragraph=p_el, run=r, para_style_id=para_style_id
    )
    color = ""
    if rPr is not None:
        c = rPr.find(qn("w:color"))
        if c is not None:
            color = _attr(c, "w:val")
    return RunInfo(
        text=_iter_run_text(r),
        bold=resolver.effective_bold(paragraph=p_el, run=r, para_style_id=para_style_id),
        italic=(
            _bool_val(rPr.find(qn("w:i"))) if rPr is not None and rPr.find(qn("w:i")) is not None else None
        ),
        size_pt=resolver.effective_size_pt(
            paragraph=p_el, run=r, para_style_id=para_style_id
        ),
        font_name_ascii=ascii_f,
        font_name_eastasia=ea_f,
        color_rgb=color,
        has_break=bool(r.findall(f".//{qn('w:br')}")),
        has_image=bool(r.findall(f".//{qn('w:drawing')}")),
    )


def _outline_level(p_el) -> int | None:
    pPr = p_el.find(qn("w:pPr"))
    if pPr is None:
        return None
    lvl = pPr.find(qn("w:outlineLvl"))
    if lvl is None:
        return None
    raw = _attr(lvl, "w:val")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _heading_basis(style_name: str, style_id: str, outline: int | None) -> tuple[bool, str]:
    """标题三层兜底，返回 (是否标题, 依据)。

    判定优先级与"可靠度"一致，依据取**最可靠的那个命中项**：

    1. ``outlineLvl`` —— 段落自己的大纲级别（0 基）。数值不依赖模板命名，
       中文"标题 1"、英文"Heading 1"、自定义样式名都靠它兜住，最可靠。
    2. 样式名含「标题」/「題目」/``heading``。
    3. 样式 ID 含上述词（样式名被改过、或本地化名丢失时仍能命中）。

    反过来说，"样式名像标题"并不能推翻"没有 outlineLvl"——很多学校的正文
    样式就叫「正文标题」，所以这里只做"任一命中即为标题"，不做否定判断。
    """
    if outline is not None:
        return True, "outline_lvl"
    low_name = (style_name or "").lower()
    if "标题" in (style_name or "") or "題目" in (style_name or "") or "heading" in low_name:
        return True, "style_name"
    low_id = (style_id or "").lower()
    if "标题" in (style_id or "") or "heading" in low_id:
        return True, "style_id"
    return False, "none"


def _align_of(p_el, resolver: _StyleResolver, style_id: str) -> str:
    pPr = p_el.find(qn("w:pPr"))
    el = resolver.effective_ppr_element("w:jc", pPr, style_id)
    if el is None:
        return "left"
    v = _attr(el, "w:val")
    return {"both": "justify", "distribute": "justify"}.get(v, v or "left")


def _parse_paragraph(
    resolver: _StyleResolver,
    idx: int,
    p,
    *,
    in_reference_zone: bool,
    numbering: dict[int, int] | None = None,
) -> Paragraph:
    p_el = p._p
    text = p.text or ""
    style = None
    try:
        style = p.style
    except Exception:  # pragma: no cover - 样式引用损坏
        style = None
    style_name = (getattr(style, "name", "") or "") if style is not None else ""
    style_id = (getattr(style, "style_id", "") or "") if style is not None else ""

    # 大纲级别三层来源：段落自身 → 样式链 → 编号定义（多级列表式标题）。
    outline = _outline_level(p_el)
    if outline is None:
        outline = resolver.effective_outline_level(style_id)
    if outline is None and numbering:
        outline = numbering.get(_paragraph_num_id(p_el) or -1)
    is_heading, basis = _heading_basis(style_name, style_id, outline)
    runs = [_build_run_info(resolver, r, style_id, p_el) for r in p_el.findall(qn("w:r"))]
    caption = _caption_label(text)
    has_image = bool(_count_drawings(p_el))
    kind = _paragraph_kind(
        text=text,
        is_heading=is_heading,
        caption_label=caption,
        in_reference_zone=in_reference_zone,
        has_image=has_image,
    )

    pPr = p_el.find(qn("w:pPr"))
    mode, value = resolver.effective_line_spacing(pPr, style_id)
    before, after = resolver.space_before_after(pPr, style_id)
    ind_chars, ind_pt = resolver.first_line_indent(pPr, style_id)
    # 段落级字体优先看段落标记属性，其次看样式链——run 的字体在 RunInfo 里，
    # 段落级两个都留，检查器按"段落该用什么字体"比对。
    ascii_f, ea_f = resolver.effective_fonts(paragraph=p_el, para_style_id=style_id)

    return Paragraph(
        index=idx,
        text=text,
        style_name=style_name,
        style_id=style_id,
        outline_level=outline,
        is_heading=is_heading,
        is_heading_basis=basis,
        align=_align_of(p_el, resolver, style_id),
        kind=kind,
        caption_label=caption,
        runs=runs,
        east_asia_font=ea_f,
        ascii_font=ascii_f,
        size_pt=resolver.effective_size_pt(paragraph=p_el, para_style_id=style_id),
        bold=resolver.effective_bold(paragraph=p_el, para_style_id=style_id),
        line_spacing_mode=mode,
        line_spacing_value=value,
        first_line_indent_chars=ind_chars,
        first_line_indent_pt=ind_pt,
        space_before_pt=before,
        space_after_pt=after,
        has_break=any(r.has_break for r in runs),
        has_image=has_image,
    )


# --------------------------------------------------------------------------
# 页眉页脚 / 脚注 / 表格
# --------------------------------------------------------------------------
def _header_footer_texts(doc) -> list[str]:
    """页眉页脚文字。只收非空文本，空页眉在学位论文里很常见（首页不显示）。"""
    out: list[str] = []
    for section in doc.sections:
        for part in (section.header, section.footer,
                     getattr(section, "first_page_header", None),
                     getattr(section, "first_page_footer", None),
                     getattr(section, "even_page_header", None),
                     getattr(section, "even_page_footer", None)):
            if part is None:
                continue
            try:
                text = "\n".join(p.text for p in part.paragraphs if p.text.strip())
            except Exception:  # pragma: no cover - 页眉部件损坏
                continue
            if text.strip():
                out.append(text.strip())
    return out


def _footnotes(doc, path: Path) -> tuple[list[str], int]:
    """读脚注。

    python-docx 不解析脚注，所以这里直接开 zip 读 ``word/footnotes.xml``。
    分离式/连续分隔符那两个特殊脚注（id = -1 / 0）要跳过，否则每篇文档都会
    多出两条空记录。
    """
    texts: list[str] = []
    try:
        with zipfile.ZipFile(path) as z:
            if "word/footnotes.xml" not in z.namelist():
                return [], 0
            raw = z.read("word/footnotes.xml")
    except (zipfile.BadZipFile, OSError, KeyError):
        return [], 0

    from lxml import etree

    try:
        root = etree.fromstring(raw)
    except etree.XMLSyntaxError:
        return [], 0
    for fn in root.findall(qn("w:footnote")):
        try:
            fid = int(fn.get(qn("w:id")) or "0")
        except ValueError:
            continue
        if fid <= 0:
            continue
        text = "".join(t.text or "" for t in fn.iter(qn("w:t")))
        if text.strip():
            texts.append(text.strip())

    footnote_refs = 0
    for p_el in doc.element.body.iter(qn("w:footnoteReference")):
        footnote_refs += 1
    return texts, footnote_refs if footnote_refs else len(texts)


def _read_sections(doc) -> list[SectionInfo]:
    out: list[SectionInfo] = []
    for i, s in enumerate(doc.sections):
        start_type = ""
        st = getattr(s, "start_type", None)
        if st is not None:
            start_type = getattr(st, "value", None) or str(st)
        header_text = "\n".join(p.text for p in s.header.paragraphs if p.text.strip())
        footer_text = "\n".join(p.text for p in s.footer.paragraphs if p.text.strip())
        out.append(
            SectionInfo(
                index=i,
                page_width_cm=_cm(s.page_width),
                page_height_cm=_cm(s.page_height),
                top_cm=_cm(s.top_margin),
                bottom_cm=_cm(s.bottom_margin),
                left_cm=_cm(s.left_margin),
                right_cm=_cm(s.right_margin),
                header_cm=_cm(s.header_distance),
                footer_cm=_cm(s.footer_distance),
                orientation="landscape" if s.page_width and s.page_width > s.page_height else "portrait",
                start_type=start_type,
                header_text=header_text.strip(),
                footer_text=footer_text.strip(),
            )
        )
    return out


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------
def _probe_file(path: Path) -> None:
    """开读之前先看文件头，以便把"加密/旧版/损坏"三种情况分开说清。

    统一抛一句"无法解析"没有价值：这三种情况该做的事完全不同
    （解密 / 另存为 .docx / 换文件）。
    """
    if not path.exists():
        raise DocxReadError(f"文件不存在：{path}")
    if path.is_dir():
        raise DocxReadError(f"这是一个目录，不是 .docx 文件：{path}")
    try:
        head = path.open("rb").read(8)
    except OSError as exc:
        raise DocxReadError(f"无法读取文件 {path}：{exc}") from exc

    if head.startswith(_OLE_MAGIC):
        try:
            blob = path.open("rb").read(2 * 1024 * 1024)
        except OSError:
            blob = b""
        if _ENCRYPTED_MARK in blob:
            raise DocxReadError(
                f"{path.name} 是加密的 Office 文档，内容无法读取。"
                "请先用 Word 打开并去掉密码，另存为 .docx 后重试。"
            )
        raise DocxReadError(
            f"{path.name} 是旧版 .doc 格式（或 .docx 被改过后缀），本模块只认 .docx。"
            "请在 Word 里「另存为 → Word 文档 (*.docx)」后重试。"
        )
    if not head.startswith(_ZIP_MAGIC):
        raise DocxReadError(
            f"{path.name} 不是 .docx 文件（文件头不是 ZIP/OOXML）。"
            "如果是扫描件或 PDF 转换来的文档，请先转成 .docx。"
        )


def read_docx(path: str | Path) -> DocxFormatFacts:
    """读取一个 .docx，返回格式事实。

    不抛裸异常：所有可预期的失败都包成 ``DocxReadError`` 并带可读说明。
    上层（收稿阶段）据此提示作者换文件，而不是让整条流水线崩在解析上。
    """
    path = Path(path)
    _probe_file(path)

    try:
        doc = Document(str(path))
    except zipfile.BadZipFile as exc:
        raise DocxReadError(
            f"{path.name} 的 ZIP 结构已损坏，无法解析：{exc}。"
            "可以尝试用 Word 打开后重新另存一份。"
        ) from exc
    except Exception as exc:  # noqa: BLE001 - python-docx 的异常类型不稳定
        raise DocxReadError(
            f"解析 {path.name} 失败：{type(exc).__name__}: {exc}"
        ) from exc

    facts = DocxFormatFacts(path=str(path))
    theme = _theme_fonts(path)
    resolver = _StyleResolver(doc, theme=theme)

    try:
        facts.styles = resolver.build_style_facts()
        dd_fonts = resolver.effective_fonts()
        facts.doc_default_ascii, facts.doc_default_east_asia = dd_fonts
        facts.doc_default_size_pt = resolver.effective_size_pt()
    except Exception as exc:  # noqa: BLE001 - 坏样式表不该让整篇文档读不出来
        facts.warnings.append(f"样式表解析不完整：{type(exc).__name__}: {exc}")

    # 参考文献区判定：用状态机而不是"全文查找参考文献标题"。
    # 进入条件是标题文本命中，退出条件是遇到下一个标题，或者遇到一段
    # 明显是正文的段落（长段落 / 不带编号且没有年份）——很多论文最后
    # 还有"致谢""附录"，它们跟在参考文献后面，不能算参考文献条目。
    paragraphs_all = list(doc.paragraphs)
    total = len(paragraphs_all)
    numbering = _numbering_outline_levels(path)
    in_ref_zone = False
    for idx, p in enumerate(paragraphs_all):
        style_name = ""
        style_id = ""
        try:
            style_name = (p.style.name or "") if p.style is not None else ""
            style_id = (p.style.style_id or "") if p.style is not None else ""
        except Exception:  # pragma: no cover
            style_name, style_id = "", ""
        outline = _outline_level(p._p)
        if outline is None:
            outline = resolver.effective_outline_level(style_id)
        if outline is None and numbering:
            outline = numbering.get(_paragraph_num_id(p._p) or -1)
        is_heading, _ = _heading_basis(style_name, style_id, outline)
        stripped = (p.text or "").strip()

        if is_heading:
            if _is_reference_heading(stripped):
                in_ref_zone = True
            elif in_ref_zone:
                in_ref_zone = False
        elif in_ref_zone and _looks_like_body_again(stripped):
            in_ref_zone = False

        para = _parse_paragraph(
            resolver, idx, p, in_reference_zone=in_ref_zone, numbering=numbering
        )
        # 非标题段落不会触发"重新进入"参考文献区：正文里写一句
        # "与参考文献[1]一致"就把后面全变成参考文献，是典型的误判来源。
        facts.paragraphs.append(para)

    if not facts.references and total:
        facts.warnings.append(
            "没有识别出参考文献条目。若论文的参考文献标题用的是自定义样式，"
            "请在模板规则里显式声明该样式，否则参考文献相关规则不会生效。"
        )

    facts.sections = _read_sections(doc)
    facts.header_footer_texts = _header_footer_texts(doc)

    facts.table_count = len(doc.tables)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                facts.table_paragraph_count += len(cell.paragraphs)

    facts.image_count = sum(p.has_image for p in facts.paragraphs) or sum(
        1 for _ in doc.element.body.iter(qn("w:drawing"))
    )
    facts.footnote_texts, facts.footnote_ref_count = _footnotes(doc, path)

    body_text = "".join(p.text for p in facts.paragraphs)
    facts.total_chars = len(body_text)
    facts.cjk_char_count = sum(1 for ch in body_text if _is_cjk(ch))
    facts.source_text_hash = text_hash(facts.text())

    if not facts.paragraphs:
        facts.warnings.append(
            "文档正文没有任何段落。若是扫描件或内容全在文本框里，格式检查无从下手。"
        )
    if facts.table_count:
        facts.warnings.append(
            f"文档含 {facts.table_count} 个表格（共 {facts.table_paragraph_count} 段文字）；"
            "表格文字未混入正文段落列表，其字号/字体规范与正文不同。"
        )
    if not facts.styles:
        facts.warnings.append("文档没有可读的样式表，字号/字体的继承解析只能依赖段落自身属性。")

    return facts


def summarize(facts: DocxFormatFacts) -> dict:
    """给日志/界面看的一页纸摘要。不参与规则判定。"""
    return {
        "path": facts.path,
        "paragraphs": len(facts.paragraphs),
        "body": len(facts.body_paragraphs),
        "headings": len(facts.headings),
        "captions": len(facts.captions),
        "references": len(facts.references),
        "tables": facts.table_count,
        "images": facts.image_count,
        "footnotes": facts.footnote_ref_count,
        "sections": len(facts.sections),
        "chars": facts.total_chars,
        "cjk_chars": facts.cjk_char_count,
        "warnings": list(facts.warnings),
    }


def iter_paragraph_texts(facts: DocxFormatFacts) -> Iterable[tuple[int, str]]:
    """(下标, 文本)。给需要按下标定位的检查器用，省得各自再写一遍。"""
    for p in facts.paragraphs:
        yield p.index, p.text
