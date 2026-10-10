"""收稿解析：把外部文件变成系统内部的稿件。

这一层只做一件事——**把文件读成文本，并如实记录从哪儿来**。

刻意不做的事：

* 不猜作者。文件名解析不出来就留空，交给后续环节。
* 不"清洗"文本。空行、全角空格、手动换行都原样保留——**它们是格式检查的对象**，
  在这一层被规整掉，后面的格式检查器就没得查了。
* 不解析 .doc（旧二进制格式）。python-docx 只支持 OOXML，收到 .doc 要明确报错，
  而不是抛一段看不懂的异常。

.docx 的完整结构（字体、字号、行距、页边距）由 ``app.parsing.docx_reader`` 提供，
本模块负责拿到段落文本并建立"第几段"的下标对应关系，这样格式问题能定位回原文。
"""

from __future__ import annotations

import importlib
import re
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path

#: 段落之间用什么连接。用单个换行：格式检查要能按段落切回，
#: 而空段落本身也是有意义的信息（连续空行是常见格式问题）。
PARA_SEP = "\n"

SUPPORTED_SUFFIXES = {".docx", ".txt", ".md", ".markdown"}
#: 看得见但读不了的格式，给出明确指引而不是通用报错
UNSUPPORTED_HINTS = {
    ".doc": "旧版 Word 格式（.doc）不支持。请用 Word 另存为 .docx 后重试。",
    ".caj": "知网 CAJ 格式不支持。请先从知网下载 PDF 版。",
    ".nh": "知网 NH 格式不支持。请先从知网下载 PDF 版。",
    ".pdf": "PDF 不能直接作为你的稿件导入（PDF 是参考资料格式）。若稿件原本是 PDF，请提供 .docx 原稿。",
    ".wps": "WPS 专有格式不支持。请用 WPS 另存为 .docx 后重试。",
    ".rtf": "RTF 不支持。请用 Word 另存为 .docx 后重试。",
}


class IntakeError(RuntimeError):
    """收稿失败。消息面向使用者，必须能直接照着做。"""


@dataclass
class Manuscript:
    """读入的稿件。"""

    text: str = ""
    source_path: str = ""
    title: str = ""
    author: str = ""
    kind: str = "plain"                     # docx | markdown | plain
    paragraphs: int = 0
    chars: int = 0
    cjk_chars: int = 0
    #: docx 的格式事实（dict 形式），供格式检查器重建。
    #: 非 docx 来源为空——格式检查会优雅跳过，不猜。
    format_facts: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def as_dict(self, *, with_text: bool = False) -> dict:
        d = {
            "source_path": self.source_path,
            "title": self.title,
            "author": self.author,
            "kind": self.kind,
            "paragraphs": self.paragraphs,
            "chars": self.chars,
            "cjk_chars": self.cjk_chars,
            "has_format_facts": bool(self.format_facts),
            "warnings": self.warnings,
            "meta": self.meta,
        }
        if with_text:
            d["text"] = self.text
        return d


_CJK = re.compile(r"[\u4e00-\u9fff]")


def count_cjk(text: str) -> int:
    return len(_CJK.findall(text or ""))


def to_plain_dict(obj) -> dict:
    """把格式事实对象转成纯 dict。

    收稿层落盘、格式层重建，两边必须对同一套键名达成一致。这里刻意**不依赖**
    格式模块自己的序列化方法：格式层只需实现 ``from_dict``，写出方向由本函数
    统一负责，避免"两个人各写一份 to_dict 结果不一致"这种典型的多智能体接口事故。
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "as_dict"):
        return obj.as_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    raise IntakeError(f"无法把 {type(obj).__name__} 序列化为 dict")


def _finalize(man: Manuscript) -> Manuscript:
    man.chars = len(man.text or "")
    man.cjk_chars = count_cjk(man.text)
    man.paragraphs = len((man.text or "").split(PARA_SEP)) if man.text else 0
    return man


# ---------------------------------------------------------------------------
# .docx
# ---------------------------------------------------------------------------


def read_docx(path: Path, *, with_format_facts: bool = True) -> Manuscript:
    """Read a .docx into text plus (optionally) its structural facts.

    Format facts are read through ``app.parsing.docx_reader`` when available.
    That module is optional at import time on purpose: the writing pipeline stays
    usable even if the format layer is unavailable, and the format checkers
    already know how to skip when facts are missing.
    """
    try:
        from docx import Document  # noqa: PLC0415 - 只在真正读 docx 时才需要
    except ImportError as exc:  # pragma: no cover
        raise IntakeError(
            "缺少 python-docx，无法读取 .docx。请确认运行环境带上了 "
            "aigc-reducer\\libs 的 PYTHONPATH（用 run_python.cmd 运行即可）。"
        ) from exc

    try:
        doc = Document(str(path))
    except Exception as exc:  # noqa: BLE001 - python-docx 对损坏文件抛多种异常
        raise IntakeError(
            f"无法打开 {path.name}：{type(exc).__name__}: {exc}。"
            "若文件加密或已损坏，请先用 Word 打开并另存为新的 .docx。"
        ) from exc

    man = Manuscript(source_path=str(path), kind="docx")

    lines: list[str] = []
    for p in doc.paragraphs:
        # 段内手动换行（<w:br/>）压成空格：它在格式层是独立问题（由
        # check_whitespace 类检查器负责），但在这里保留换行会让"段落数"
        # 与 docx 的段落数对不上，导致定位全错。
        text = re.sub(r"[ \t]*[\r\n]+[ \t]*", " ", p.text or "")
        lines.append(text)
    man.text = PARA_SEP.join(lines)

    # 表格文字单独取出，不混进正文段落——表格是数据，且有独立的下标空间。
    table_cells = 0
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                table_cells += len(cell.paragraphs)
    man.meta["tables"] = len(doc.tables)
    man.meta["table_paragraphs"] = table_cells
    if table_cells:
        man.warnings.append(
            f"文档含 {len(doc.tables)} 个表格（{table_cells} 段文字）。表格内容按数据对待，"
            "不参与写作与改写，仅做格式检查。"
        )

    if with_format_facts:
        try:
            # 动态导入：app\parsing\__init__.py 归格式模块所有，本模块不碰它，
            # 也就不必保证它在包初始化时就导入了 docx_reader。
            reader = importlib.import_module("app.parsing.docx_reader")
            # 函数名以格式模块为准（`read_docx`），同时容忍 `read_facts` 别名。
            #
            # 为什么宁可用 getattr 兜一层：早期版本这里写的是不存在的
            # `read_facts`，结果 docx 结构**一直读不到**，只在收稿警告里留一句话，
            # 格式检查于是静默跳过——**表面一切正常，功能根本没通**。
            # 这是最难发现的一类集成故障，所以名字对不上时直接抛错而不是降级。
            fn = getattr(reader, "read_docx", None) or getattr(reader, "read_facts", None)
            if fn is None:
                raise AttributeError(
                    "app.parsing.docx_reader 里既没有 read_docx 也没有 read_facts"
                )
            facts = fn(path)
            man.format_facts = to_plain_dict(facts)
            man.meta["format_facts_source"] = "docx_reader"
        except ImportError:
            man.warnings.append("格式层（app.parsing.docx_reader）不可用，跳过格式事实读取。")
        except Exception as exc:  # noqa: BLE001 - 格式事实读不到不应阻断收稿
            man.warnings.append(
                f"读取 docx 格式事实失败：{type(exc).__name__}: {exc}。格式检查将被跳过。"
            )

    core = getattr(doc, "core_properties", None)
    if core is not None:
        man.title = (getattr(core, "title", "") or "").strip()
        man.author = (getattr(core, "author", "") or "").strip()

    # 文档属性里的标题常常是空的或就是文件名，这时用文件名兜底。
    if not man.title:
        man.title = path.stem

    return _finalize(man)


# ---------------------------------------------------------------------------
# 纯文本 / Markdown
# ---------------------------------------------------------------------------


def read_text(path: Path) -> Manuscript:
    """Read .txt / .md.

    Encoding is the practical trap here: Chinese manuscripts saved by Windows
    tools are often GBK/GB18030, and decoding those as UTF-8 produces a page of
    replacement characters rather than an error. So UTF-8 is tried first, and
    GB18030 second; a file that decodes cleanly as neither is reported.
    """
    raw = path.read_bytes()
    if not raw.strip():
        raise IntakeError(f"{path.name} 是空文件。")

    text = ""
    used = ""
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(enc)
            used = enc
            break
        except UnicodeDecodeError:
            continue
    if not used:
        raise IntakeError(
            f"{path.name} 的编码无法识别（试过 UTF-8 与 GB18030）。"
            "请用记事本另存为 UTF-8 编码后重试。"
        )

    man = Manuscript(
        source_path=str(path),
        kind="markdown" if path.suffix.lower() in (".md", ".markdown") else "plain",
        title=path.stem,
    )
    man.text = text.replace("\r\n", "\n").replace("\r", "\n")
    man.meta["encoding"] = used
    if used == "gb18030":
        man.warnings.append("文件不是 UTF-8 编码，已按 GB18030 解码。建议另存为 UTF-8。")

    if man.kind == "markdown":
        # Markdown 的 # 标题本身就是结构信息，转成中文论文的编号形式没有意义，
        # 但要让下游知道标题层级从哪里来。保留原文，只记录层级。
        heads = [ln.lstrip("#").strip() for ln in man.text.split("\n") if ln.strip().startswith("#")]
        man.meta["markdown_headings"] = len(heads)

    return _finalize(man)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def read_manuscript(path: str | Path) -> Manuscript:
    """Read any supported file into a Manuscript, or fail with a usable message."""
    p = Path(path)
    if not p.exists():
        raise IntakeError(f"文件不存在：{p}")
    if p.is_dir():
        raise IntakeError(f"{p} 是目录。请指定一个文件，或把稿件放进 inbox\\ 后重试。")

    suffix = p.suffix.lower()
    if suffix in UNSUPPORTED_HINTS:
        raise IntakeError(UNSUPPORTED_HINTS[suffix])
    if suffix == ".docx":
        return read_docx(p)
    if suffix in SUPPORTED_SUFFIXES:
        return read_text(p)
    if suffix == "":
        raise IntakeError(f"{p.name} 没有扩展名，无法判断格式。请改成 .docx 或 .txt。")
    raise IntakeError(
        f"不支持的格式：{suffix}。支持 .docx / .txt / .md。"
    )


def looks_like_manuscript(path: str | Path) -> bool:
    """Cheap check used when scanning inbox/ for candidate manuscripts."""
    p = Path(path)
    return p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
