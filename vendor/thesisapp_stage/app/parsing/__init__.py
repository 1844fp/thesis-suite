"""Word（.docx）解析层。

与 ``app.adapters`` 的分工：adapters 管"和别的服务说话"，parsing 管
"把作者的文件读成结构化数据"。两者都不做判断——判断在 ``app.format``。

这里目前只有 ``docx_reader``。它产出 ``DocxFormatFacts``（可 JSON 序列化），
是格式检查、格式写回、以及将来"按学校模板导出"三件事的共同输入。
"""

from .docx_reader import (
    DocxFormatFacts,
    DocxReadError,
    Paragraph,
    RunInfo,
    SectionInfo,
    StyleFact,
    iter_paragraph_texts,
    read_docx,
    short_evidence,
    summarize,
)

__all__ = [
    "DocxFormatFacts",
    "DocxReadError",
    "Paragraph",
    "RunInfo",
    "SectionInfo",
    "StyleFact",
    "iter_paragraph_texts",
    "read_docx",
    "short_evidence",
    "summarize",
]
