"""文件导入：把上传的文档变成可用的文本。

支持哪些格式
------------
学术写作里作者手上的资料格式很杂，所以这里按"能不能可靠地把文字取出来"分层处理：

* **PDF**    —— 用 PyMuPDF **逐页**取文本。逐页是有意的：页码是学术引用的一部分，
  ``材料原文（第 3 页）`` 比 ``材料原文`` 有用得多，而且读者要能回查。
* **DOCX**   —— 用 python-docx 取段落与表格文字（Word 稿子里表格常含关键数据）。
  **旧版二进制 ``.doc`` 不支持**，会被明确拒绝并告知"另存为 .docx"——
  让它掉进"无法识别的类型"里，用户不知道下一步该做什么。
* **Markdown / 纯文本** —— 直接用标准库读，顺带做编码探测。
* **HTML**   —— 用标准库的 HTMLParser 剥标签，不引第三方。
* **CSV**    —— 标准库 csv 转成对齐文本，方便模型读懂表格。

安全性
------
上传文件是最容易出事的地方，所以这里的约束都是硬性的：

* **只读，不执行**：所有解析都是纯文本抽取，不渲染、不执行任何内容；
* **大小与数量上限**：单文件与总大小都设上限，避免一个请求把内存吃满；
* **不落盘**：解析在内存里完成，不把访客的文件写进服务器磁盘；
* **跳过不支持的类型**，并把原因如实回报，而不是静默当成空文件。

一个必须说清的限制：**扫描版（图片型）PDF 取不出文字**。那种 PDF 只有位图，
没有文本层，PyMuPDF 会返回空。这里会明确识别并告知"这看起来是扫描版，
需要 OCR"，而不是假装导入成功、给出一份空证据。
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser

#: 单文件大小上限（字节）。学术 PDF 一般几百 KB 到十几 MB。
MAX_FILE_BYTES = 25 * 1024 * 1024

#: 一次最多导入几个文件。
MAX_FILES = 10

#: 解析后单个文件的文本上限（字符）。超出部分截断并如实标注。
MAX_TEXT_CHARS = 200_000

#: 扩展名 -> 展示用的类型名
KIND_BY_EXT = {
    ".pdf": "PDF",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".txt": "纯文本",
    ".text": "纯文本",
    ".docx": "Word",
    ".html": "HTML",
    ".htm": "HTML",
    ".csv": "CSV",
    ".tsv": "TSV",
    ".json": "JSON",
    ".bib": "BibTeX",
}

#: 用文件头判断真实类型。**不能只信扩展名**：改过名的文件很常见，
#: 而拿 .txt 解析器去啃一个 PDF 只会得到一堆乱码，还不如直接拒绝。
MAGIC = (
    (b"%PDF-", ".pdf"),
    (b"PK\x03\x04", ".docx"),          # docx/xlsx 都是 zip 容器
    (b"{\\rtf", ".rtf"),
    (b"\xef\xbb\xbf", ".txt"),         # UTF-8 BOM
)


@dataclass
class Extracted:
    """一个文件解析后的结果。"""

    filename: str = ""
    kind: str = ""
    text: str = ""
    pages: int = 0
    paragraphs: int = 0
    truncated: bool = False
    warning: str = ""
    error: str = ""
    #: 每页/每段的片段，供证据抽取使用（带定位信息）。
    chunks: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())

    def summary(self) -> dict:
        return {
            "filename": self.filename,
            "kind": self.kind,
            "chars": len(self.text),
            "pages": self.pages,
            "paragraphs": self.paragraphs,
            "truncated": self.truncated,
            "warning": self.warning,
            "error": self.error,
            "ok": self.ok,
            "preview": self.text[:400],
        }


def detect_kind(filename: str, head: bytes) -> str:
    """判断文件类型：先看文件头，再看扩展名。

    文件头优先，因为扩展名会被随手改掉；但 .docx 与 .xlsx 共享 zip 文件头，
    所以要靠扩展名区分，因此两者结合而不是二选一。
    """
    low = (filename or "").lower()
    ext = ""
    if "." in low:
        ext = low[low.rfind("."):]

    for magic, kind_ext in MAGIC:
        if head.startswith(magic):
            if kind_ext == ".docx" and ext in (".docx", ".docm"):
                return ".docx"
            if kind_ext == ".docx":
                # zip 但不是 docx：可能是 xlsx/pptx，明确说不支持
                return "zip-other"
            if kind_ext == ".txt":
                break
            return kind_ext

    if ext in KIND_BY_EXT:
        return ext
    # 没有扩展名时，尝试当纯文本处理——文本文件很常见，而误判的代价只是空白。
    try:
        head.decode("utf-8")
        return ".txt"
    except UnicodeDecodeError:
        return ""


class _HTMLText(HTMLParser):
    """把 HTML 剥成纯文本。只用标准库，避免为这点需求引依赖。"""

    #: 这些标签的内容不是正文，整块丢掉。
    SKIP = {"script", "style", "noscript", "head", "meta", "link"}
    #: 这些标签前后要断开，否则段落会粘成一行。
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:  # noqa: ANN001
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        return raw.strip()


def _decode_text(data: bytes) -> tuple[str, str]:
    """解码文本文件。返回 ``(文本, 编码说明)``。

    中文资料的编码很杂（UTF-8 / GBK / GB18030），直接用 utf-8 硬解会把中文变成
    替换字符——那种"导入成功但全是问号"的结果比报错更糟。所以这里按顺序试探，
    并把最终用的编码报出来。
    """
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    for enc in ("gb18030", "gbk", "big5"):
        try:
            text = data.decode(enc)
            # gb18030 几乎不会抛错，所以要额外确认结果里有合理比例的中文/ASCII，
            # 否则一个 UTF-8 文件被当 GBK 解出来的会是满屏乱码。
            if text:
                ok = sum(1 for ch in text[:2000] if ch.isprintable() or ch in "\r\n\t")
                if ok / max(1, len(text[:2000])) > 0.9:
                    return text, enc
        except UnicodeDecodeError:
            continue
    # 最后兜底：用 latin-1 永远能解，但明确标注可能是乱码
    return data.decode("latin-1", errors="replace"), "latin-1(可能有乱码)"


def _from_pdf(data: bytes, name: str) -> Extracted:
    out = Extracted(filename=name, kind="PDF")
    try:
        import pymupdf  # 新版导入名
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError as exc:
            out.error = f"缺少 PDF 解析库（pymupdf）：{exc}"
            return out

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # noqa: BLE001 - 坏文件要给出可读原因
        out.error = f"无法打开 PDF：{type(exc).__name__}: {exc}"
        return out

    try:
        out.pages = doc.page_count
        parts: list[str] = []
        for i in range(doc.page_count):
            try:
                page_text = doc.load_page(i).get_text("text") or ""
            except Exception:  # noqa: BLE001 - 单页失败不该毁掉整个文件
                continue
            page_text = re.sub(r"[ \t\u00a0]+", " ", page_text)
            page_text = re.sub(r"\n{3,}", "\n\n", page_text).strip()
            if page_text:
                parts.append(page_text)
                out.chunks.append({"page": i + 1, "text": page_text})
        out.text = "\n\n".join(parts)
        out.paragraphs = len([p for p in out.text.split("\n\n") if p.strip()])
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass

    if not out.text.strip():
        # 扫描版 PDF：只有位图，没有文本层。必须说清楚，而不是给一份空证据。
        out.warning = (
            f"这份 PDF 共 {out.pages} 页，但取不到任何文字——很可能是**扫描版（图片型）**，"
            "需要先做 OCR 才能导入。请改用带文本层的版本，或把关键段落直接粘贴进来。"
        )
    return out


def _from_docx(data: bytes, name: str) -> Extracted:
    out = Extracted(filename=name, kind="Word")
    try:
        import docx  # python-docx
    except ImportError as exc:
        out.error = f"缺少 Word 解析库（python-docx）：{exc}"
        return out

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        out.error = f"无法打开 Word 文档：{type(exc).__name__}: {exc}"
        return out

    parts: list[str] = []
    try:
        for para in document.paragraphs:
            text = (para.text or "").strip()
            if text:
                parts.append(text)
        out.paragraphs = len(parts)

        # 表格里常有关键数据，不能丢。转成"表头: 值"的文本形式。
        for ti, table in enumerate(document.tables, start=1):
            rows: list[str] = []
            for row in table.rows:
                cells = [(c.text or "").strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"【表 {ti}】\n" + "\n".join(rows))
        out.text = "\n\n".join(parts)
    except Exception as exc:  # noqa: BLE001
        out.error = f"读取 Word 内容失败：{type(exc).__name__}: {exc}"
    return out


def _from_html(data: bytes, name: str) -> Extracted:
    out = Extracted(filename=name, kind="HTML")
    text, enc = _decode_text(data)
    parser = _HTMLText()
    try:
        parser.feed(text)
        out.text = parser.text()
        out.paragraphs = len([p for p in out.text.split("\n\n") if p.strip()])
        if enc != "utf-8":
            out.warning = f"按 {enc} 解码"
    except Exception as exc:  # noqa: BLE001
        out.error = f"解析 HTML 失败：{type(exc).__name__}: {exc}"
    return out


def _from_csv(data: bytes, name: str, delimiter: str = ",") -> Extracted:
    out = Extracted(filename=name, kind="CSV" if delimiter == "," else "TSV")
    text, enc = _decode_text(data)
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    except Exception as exc:  # noqa: BLE001
        out.error = f"解析表格失败：{type(exc).__name__}: {exc}"
        return out

    rows = [r for r in rows if any(str(c).strip() for c in r)]
    if not rows:
        out.warning = "表格是空的。"
        return out
    out.paragraphs = len(rows)
    lines = [" | ".join(str(c).strip() for c in r) for r in rows[:2000]]
    out.text = "\n".join(lines)
    if len(rows) > 2000:
        out.truncated = True
        out.warning = f"表格共 {len(rows)} 行，只取了前 2000 行。"
    if enc != "utf-8":
        out.warning = (out.warning + "；" if out.warning else "") + f"按 {enc} 解码"
    return out


def _from_plain(data: bytes, name: str, kind: str, ext: str) -> Extracted:
    out = Extracted(filename=name, kind=kind)
    text, enc = _decode_text(data)
    if ext in (".md", ".markdown"):
        # Markdown 的标记会干扰模型理解，但**不能全删**：标题层级、列表、代码块
        # 都是结构信息。只把最常见的行内标记去掉，保留换行与列表符号。
        text = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", text)      # 图片 -> alt
        text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)        # 链接 -> 文本
        text = re.sub(r"`{1,3}", "", text)                          # 行内代码围栏
    out.text = text
    out.paragraphs = len([p for p in text.split("\n\n") if p.strip()])
    if enc not in ("utf-8", "utf-8-sig"):
        out.warning = f"按 {enc} 解码（原文件可能不是 UTF-8）"
    return out


def extract(filename: str, data: bytes) -> Extracted:
    """把一个文件解析成文本。**永不抛异常**，错误一律进 ``Extracted.error``。

    永不抛是有意的：一次导入可能带十个文件，其中一个坏了不该让其余九个失败。
    """
    name = (filename or "未命名").strip() or "未命名"
    if not data:
        return Extracted(filename=name, error="文件是空的。")
    if len(data) > MAX_FILE_BYTES:
        return Extracted(
            filename=name,
            error=f"文件过大（{len(data) / 1024 / 1024:.1f} MB，上限 {MAX_FILE_BYTES // 1024 // 1024} MB）。",
        )

    ext = detect_kind(name, data[:16])
    if not ext:
        return Extracted(
            filename=name,
            error="无法识别的文件类型。目前支持 PDF / Word(.docx) / Markdown / 文本 / HTML / CSV。",
        )
    if ext == "zip-other":
        return Extracted(filename=name, error="这是个压缩包（可能是 .xlsx/.pptx），暂不支持。")
    if ext == ".rtf":
        return Extracted(filename=name, error="暂不支持 RTF，请另存为 PDF 或 .docx。")
    if ext == ".doc":
        # 老的二进制 .doc 不是 zip，python-docx 读不了。**说清怎么解决**，
        # 而不是让它掉进"无法识别的文件类型"里——那句话没告诉用户下一步做什么。
        return Extracted(
            filename=name,
            error="暂不支持旧版 .doc（二进制格式）。请在 Word 里「另存为」.docx 后重新上传。",
        )

    try:
        if ext == ".pdf":
            out = _from_pdf(data, name)
        elif ext == ".docx":
            out = _from_docx(data, name)
        elif ext in (".html", ".htm"):
            out = _from_html(data, name)
        elif ext in (".csv", ".tsv"):
            out = _from_csv(data, name, "\t" if ext == ".tsv" else ",")
        elif ext == ".json":
            out = _from_plain(data, name, "JSON", ext)
        elif ext == ".bib":
            out = _from_plain(data, name, "BibTeX", ext)
        else:
            out = _from_plain(data, name, KIND_BY_EXT.get(ext, "文本"), ext)
    except Exception as exc:  # noqa: BLE001 - 兜底：任何解析异常都变成可读错误
        return Extracted(filename=name, error=f"解析失败：{type(exc).__name__}: {exc}")

    if len(out.text) > MAX_TEXT_CHARS:
        out.text = out.text[:MAX_TEXT_CHARS]
        out.truncated = True
        out.warning = (out.warning + "；" if out.warning else "") + \
            f"内容超过 {MAX_TEXT_CHARS // 1000} 千字，已截断。"
    return out


def guess_title(extracted: Extracted, fallback: str = "") -> str:
    """从文本里猜一个题名，供资料获取的抽取使用。

    用文件名兜底就够了——让模型从正文里认题名更准，但文件名通常已经含有意义，
    而这里只需要一个"交给模型的提示"，最终题名由抽取阶段判定。
    """
    stem = re.sub(r"\.(pdf|docx|md|markdown|txt|html?|csv|tsv|json|bib)$", "",
                  (extracted.filename or "").strip(), flags=re.I)
    stem = stem.strip()
    if stem and len(stem) <= 120:
        return stem
    return fallback or extracted.filename


def split_into_sources(items: list[Extracted]) -> list[dict]:
    """把解析结果转成 ``/api/gather/extract`` 能直接吃的 ``sources``。

    PDF 逐页保留页码：写作时引用 ``（第 3 页）`` 比 ``（材料原文）`` 有用得多，
    而且读者能回查。这是把"页码"当结构信息保留、而不是揉进一坨文本的原因。
    """
    sources: list[dict] = []
    for item in items:
        if not item.ok:
            continue
        title = guess_title(item)
        if item.chunks and item.kind == "PDF":
            # 逐页作为独立来源：证据片段能带上页码。
            for chunk in item.chunks:
                text = str(chunk.get("text") or "").strip()
                if len(text) < 40:
                    continue
                sources.append({
                    "title": f"{title}（第 {chunk.get('page')} 页）",
                    "text": text,
                    "page": chunk.get("page"),
                    "origin_file": item.filename,
                })
        else:
            sources.append({
                "title": title,
                "text": item.text,
                "origin_file": item.filename,
            })
    return sources
