"""RAG 示例检索：从已入库的论文里取真实段落，作为写作的 few-shot 示例。

## 为什么需要这一层

`app/writing/__init__.py` 给的是**构造性规范**（"应当怎么做"），但规范本身不含语感。
模型看完规范，未必知道"你这个领域里，一段合格的文献综述过渡长什么样"。
所以再从用户的 RAG 知识库里取出**同一学科的真实段落**，摆在提示词里当示例。

这就是用户选择的"两者结合"：**内置公认框架 + 真实领域写法**。

## 一条硬纪律：示例要标注来源，且不得被当作可抄的文本

* 每段示例都带上 `citation`（作者/年份/章节/页码），让作者能追回原文；
* 提示词里明确要求模型**学结构与语气，不得照抄句子**。这不是客套：
  照抄会构成抄袭，是这个功能最严重的失败模式。

## 检索失败时的行为

RAG 服务不可用时返回空示例，**不阻断写作**——规范本身已足以指导写作，
示例只是增强。这与"检索不到证据就不写作"不同：证据是立论的前提，
示例只是风格参考。
"""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import Any, Sequence

from ..adapters.base import PeerError
from ..adapters.rag import RAGAdapter


@dataclass
class Example:
    """一条检索到的真实段落。"""

    text: str
    citation: str
    score: float = 0.0
    doc_id: str = ""
    section: str = ""
    page: int | None = None

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "citation": self.citation,
            "score": round(self.score, 4),
            "doc_id": self.doc_id,
            "section": self.section,
            "page": self.page,
        }


#: 示例段落的最短/最长长度。太短的段落没有结构可学；
#: 太长的会挤占提示词预算，而且模型容易逐句模仿。
MIN_EXAMPLE_CHARS = 120
MAX_EXAMPLE_CHARS = 520

#: 提示词里最多放几条示例。3 条足以体现结构与语气，再多会稀释指令。
MAX_EXAMPLES = 3


def _usable(text: str) -> bool:
    t = (text or "").strip()
    if not (MIN_EXAMPLE_CHARS <= len(t) <= MAX_EXAMPLE_CHARS):
        return False
    # 参考文献条目：对写作示例没有价值，而且会**教模型模仿文献表格式**。
    # 实测踩过：检索"文献综述 研究现状"时返回的第一条就是参考文献区的内容
    # （「（8）毛基业、苏芳：《案例研究的理论贡献》…《管理世界》…」）。
    if t.lstrip().startswith("[") and "]" in t[:8]:
        return False
    if re.match(r"^[（(]?\s*\d{1,3}\s*[)）.、]", t):
        return False
    # 书名号/期刊号密集 = 参考文献段落。正常论述里偶尔引书名，
    # 但连续出现多处几乎只在文献表里。
    if len(re.findall(r"《[^》]{2,60}》", t)) >= 3:
        return False
    # 期刊卷期、页码、"第N期"这类条目特征词密集
    if len(re.findall(r"第\s*\d+\s*期|\(\d{4}\)|,\s*\d{4}\s*[,，]\s*\d+\s*\(", t)) >= 2:
        return False
    # 刊物页眉页脚：作者简介、收稿日期、基金项目。这些是版式残留，
    # 做示例只会让模型学到"署名—日期—单位"的排版格式，不是写作结构。
    if re.match(r"^(?:作者简介|收稿日期|修回日期|基金项目|通讯作者)", t):
        return False
    if re.search(r"作者简介|收稿日期|修回日期|基金项目", t):
        return False
    # OCR 断行碎片：PDF 提取常把一行切成很多短行，读起来是断句。
    # 判据是"行均长度过短"——正常段落每行约 40~60 字，碎片段远低于此。
    lines = [x for x in t.split("\n") if x.strip()]
    if len(lines) >= 4 and len(t) / len(lines) < 22:
        return False
    # 数据表/公式行：中文字占比过低
    cjk = sum(1 for c in t if "\u4e00" <= c <= "\u9fff")
    return cjk / max(len(t), 1) >= 0.5


def fetch_examples(
    base_url: str,
    queries: Sequence[str],
    *,
    k: int = 4,
    max_examples: int = MAX_EXAMPLES,
    timeout: float = 60.0,
    adapter: Any = None,
) -> tuple[list[Example], str]:
    """按查询词检索示例段落。

    返回 `(示例列表, 说明)`。说明用于告诉调用方"为什么没有示例"——
    是服务不可用、还是检索无结果，两者对使用者的含义完全不同。
    """
    rag = adapter
    if rag is None:
        if not base_url:
            return [], "未配置 RAG 服务地址，跳过示例检索"
        rag = RAGAdapter(base_url, timeout=timeout)

    out: list[Example] = []
    seen: set[str] = set()
    errors: list[str] = []

    for q in queries:
        if len(out) >= max_examples:
            break
        try:
            hits = rag.search(q, k=k, rerank=False)
        except PeerError as exc:
            errors.append(f"{q}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - 示例检索失败不该阻断写作
            errors.append(f"{q}: {type(exc).__name__}: {exc}")
            continue
        for h in hits:
            if len(out) >= max_examples:
                break
            text = (getattr(h, "text", "") or "").strip()
            if not _usable(text):
                continue
            key = text[:60]
            if key in seen:
                continue
            seen.add(key)
            out.append(Example(
                text=text,
                citation=getattr(h, "citation", "") or "",
                score=float(getattr(h, "score", 0.0) or 0.0),
                doc_id=getattr(h, "doc_id", "") or "",
                section=getattr(h, "section", "") or "",
                page=getattr(h, "page", None),
            ))

    if out:
        return out, f"取到 {len(out)} 条同领域真实段落作示例"
    if errors:
        return [], "未取到示例：" + errors[0][:120]
    return [], "检索无可用示例（可能知识库里缺少同主题文献）"


def format_examples(examples: Sequence[Example]) -> str:
    """渲染成提示词里的示例区块。

    **必须明确禁止照抄。** 这些是从已发表论文里取的原句，
    照抄构成抄袭——这是整个功能里风险最高的一点，所以措辞不能含糊。
    """
    if not examples:
        return ""
    lines = [
        "## 同领域真实段落（仅供体会结构与语气）",
        "",
        "以下是数据库中**已发表论文**的原文片段，用来说明本领域合格的写法长什么样。",
        "",
        "**严格禁止照抄**：不得复制其中任何句子、短语或句式顺序。",
        "只允许借鉴**组织方式**（如何起句、如何并列多篇文献、如何转入评价）与**语体**。",
        "你写出的内容必须使用你手上的证据，表达你自己的判断。",
        "",
    ]
    for i, e in enumerate(examples, 1):
        lines.append(f"【示例 {i}】来源：{e.citation or '（未标注）'}")
        lines.append(e.text)
        lines.append("")
    return "\n".join(lines)
