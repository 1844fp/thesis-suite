"""把生成结果渲染成 Markdown / 纯文本，用于导出与剪贴板。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

DISCLAIMER = (
    "> ⚠️ 文献提示：**参考文献不保证真实存在**，大模型会生成看起来很像真的出处。"
    "凡标注「待核实」或 `verified=false` 的条目，请务必在知网、万方、维普或 "
    "Web of Science 中检索核对原文后再引用。题目请结合导师意见、数据可得性与"
    "自身条件最终确定。"
)


def _topic_markdown(topic: dict, index: int) -> str:
    brief = topic.get("brief") or {}
    lines: list[str] = []
    lines.append(f"### {index}. {topic.get('title', '（无题目）')}")
    if topic.get("english_title"):
        lines.append(f"*{topic['english_title']}*")
    meta = [item for item in (topic.get("type"), topic.get("difficulty")) if item]
    if meta:
        lines.append("")
        lines.append(" · ".join(f"`{item}`" for item in meta))
    lines.append("")
    if topic.get("summary"):
        lines.append(f"**一句话概述**：{topic['summary']}")
        lines.append("")

    def block(label: str, value: str) -> None:
        if value:
            lines.append(f"**{label}**：{value}")
            lines.append("")

    questions = topic.get("research_questions") or []
    if questions:
        lines.append("**研究问题**：")
        lines.extend(f"{i}. {q}" for i, q in enumerate(questions, 1))
        lines.append("")

    block("理论框架", topic.get("theory", ""))
    block("研究方法", topic.get("methodology", ""))
    block("数据来源", topic.get("data_sources", ""))
    block("创新点", topic.get("innovations", ""))
    block("可行性", topic.get("feasibility", ""))
    block("风险提示", topic.get("risks", ""))
    if topic.get("keywords"):
        lines.append(f"**关键词**：{'、'.join(topic['keywords'])}")
        lines.append("")

    lines.append("#### 简报")
    lines.append("")
    block("研究背景", brief.get("background", ""))
    literature = brief.get("literature") or []
    if literature:
        lines.append("**文献短评**：")
        lines.append("")
        for item in literature:
            citation = item.get("citation", "").strip()
            if not citation:
                continue
            flag = "" if item.get("verified") else " ⚠️待核实"
            lines.append(f"- {citation}{flag}")
            if item.get("contribution"):
                lines.append(f"  - 贡献：{item['contribution']}")
            if item.get("critique"):
                lines.append(f"  - 局限：{item['critique']}")
        lines.append("")
    block("述评", brief.get("commentary", ""))
    block("研究缺口", brief.get("gap", ""))
    references = brief.get("references") or []
    if references:
        lines.append("**参考文献线索**：")
        lines.append("")
        lines.extend(f"- {item}" for item in references)
        lines.append("")
    if not topic.get("brief_from_model", True):
        lines.append("> 本简报为系统按题目自动补全的占位内容，建议重新生成或补充资料。")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_markdown(result: dict) -> str:
    topics = result.get("topics") or []
    out: list[str] = []
    discipline = result.get("discipline") or "通用"
    out.append(f"# {discipline} · 论文选题建议（{len(topics)} 个）\n")
    meta_bits = []
    if result.get("level"):
        meta_bits.append(f"论文层次：{result['level']}")
    if result.get("direction"):
        meta_bits.append(f"方向：{result['direction']}")
    if result.get("mode"):
        meta_bits.append(f"生成方式：{'大模型' if result['mode'] == 'llm' else '离线知识库'}")
    if result.get("model"):
        meta_bits.append(f"模型：{result['model']}")
    if result.get("seed"):
        meta_bits.append(f"随机种子：{result['seed']}")
    meta_bits.append(f"生成时间：{result.get('generated_at') or datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    out.append("　".join(meta_bits) + "\n")

    if result.get("notice"):
        out.append(f"> {result['notice']}\n")
    out.append(DISCLAIMER + "\n")
    out.append("---\n")

    for index, topic in enumerate(topics, 1):
        out.append(_topic_markdown(topic, index))
        out.append("---\n")
    return "\n".join(out)


def render_plain(result: dict) -> str:
    """去掉 Markdown 记号，方便直接粘进微信/邮件。"""
    lines = []
    for index, topic in enumerate(result.get("topics") or [], 1):
        lines.append(f"{index}. {topic.get('title', '')}")
        if topic.get("summary"):
            lines.append(f"   {topic['summary']}")
        brief = topic.get("brief") or {}
        if brief.get("commentary"):
            lines.append(f"   短评：{brief['commentary'][:160]}")
        lines.append("")
    return "\n".join(lines)


def save_markdown(result: dict, directory: Path, stem: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    safe = _safe_stem(result.get("discipline") or "选题")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = directory / f"{stem or safe}-{stamp}.md"
    path.write_text(render_markdown(result), encoding="utf-8")
    return path


def _safe_stem(text: str) -> str:
    keep = []
    for ch in text:
        if ch.isalnum() or ch in "-_（）()":
            keep.append(ch)
        elif ch.strip():
            keep.append("_")
    name = "".join(keep).strip("_")
    return (name or "选题")[:40]
