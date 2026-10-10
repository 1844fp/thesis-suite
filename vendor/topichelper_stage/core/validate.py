"""把模型返回的自由结构收敛成界面能稳定渲染的结构。

模型（尤其国产模型）经常把列表写成字符串、把对象写成数组、漏掉某个键。
这里统一兜底，保证前端永远拿到同一套字段，不会因为少一个键白屏。
"""

from __future__ import annotations

import re
from typing import Any

TOPIC_TYPES = ("理论探索", "实证研究", "案例研究", "应用设计", "综述与元分析", "比较研究")
DIFFICULTIES = ("偏易（可本科）", "适中（可硕士）", "偏难（可博士/核心期刊）")

_BULLET_RE = re.compile(r"^\s*(?:[-*•·]|\(?\d+[.、)]|第[一二三四五六七八九十]+[、.])\s*")
_ALLOWED_TOPIC_KEYS = (
    "id", "title", "english_title", "type", "difficulty", "summary",
    "research_questions", "theory", "methodology", "data_sources",
    "innovations", "feasibility", "risks", "keywords", "brief",
)


def as_text(value: Any) -> str:
    """把任意值压成一行可读文本。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            piece = as_text(item)
            if piece:
                parts.append(f"{key}：{piece}")
        return "；".join(parts)
    if isinstance(value, (list, tuple)):
        return "；".join(filter(None, (as_text(item) for item in value)))
    return str(value).strip()


def as_list(value: Any) -> list[str]:
    """把字符串/列表/对象统一成字符串列表，会拆掉编号和项目符号。"""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) == 1:
            # 单行里用「；」「1. 」「①」分隔的情况也拆开
            single = lines[0]
            if "；" in single or re.search(r"[①②③④⑤⑥⑦⑧⑨⑩]", single):
                parts = re.split(r"；|(?=[①②③④⑤⑥⑦⑧⑨⑩])", single)
                return [clean for clean in (_strip_bullet(p) for p in parts) if clean]
            return [strip_numbering(single)] if strip_numbering(single) else []
        return [clean for clean in (strip_numbering(line) for line in lines) if clean]
    if isinstance(value, dict):
        return [as_text(value)] if as_text(value) else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                out.extend(as_list(item))
            else:
                text = strip_numbering(as_text(item))
                if text:
                    out.append(text)
        return out
    return []


def strip_numbering(line: str) -> str:
    cleaned = _BULLET_RE.sub("", line.strip())
    return cleaned.strip().strip("；;").strip()


_strip_bullet = strip_numbering


def pick(mapping: dict, *keys: str) -> Any:
    """按顺序取第一个存在且非空的值（兼容模型自创的键名）。"""
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value not in (None, "", [], {}):
                return value
    return None


def normalize_literature(value: Any) -> list[dict]:
    """文献列表 → [{citation, contribution, critique, verified}]。"""
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out: list[dict] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                out.append({"citation": text, "contribution": "", "critique": "", "verified": False})
            continue
        if isinstance(item, dict):
            citation = as_text(pick(item, "citation", "reference", "ref", "文献", "title", "source"))
            if not citation:
                citation = as_text(item)
            contribution = as_text(pick(item, "contribution", "main_idea", "finding", "观点", "贡献"))
            critique = as_text(pick(item, "critique", "limitation", "gap", "局限", "不足", "评述"))
            verified = item.get("verified")
            out.append({
                "citation": citation,
                "contribution": contribution,
                "critique": critique,
                "verified": bool(verified) if isinstance(verified, bool) else False,
            })
    return out[:12]


def normalize_brief(value: Any, topic: dict) -> tuple[dict, bool]:
    """返回 (简报, 是否由模型给出)。模型漏写时用题目自身信息兜底成稿。"""
    if isinstance(value, str):
        value = {"commentary": value}
    if not isinstance(value, dict):
        value = {}
    brief = {
        "background": as_text(pick(value, "background", "research_background", "研究背景", "背景")),
        "literature": normalize_literature(pick(value, "literature", "lit_review", "文献", "文献综述")),
        "commentary": as_text(pick(value, "commentary", "review", "comment", "文献短评", "短评", "述评")),
        "gap": as_text(pick(value, "gap", "research_gap", "研究缺口", "空白")),
        "references": as_list(pick(value, "references", "reference_list", "参考文献")),
    }
    from_model = bool(brief["background"] or brief["commentary"] or brief["literature"])
    if not brief["background"]:
        brief["background"] = (
            f"本选题围绕「{topic.get('title', '')}」展开，"
            f"从该方向当前的研究重心与实践需求出发，梳理已有工作留下的问题，"
            f"并据此确定本文要回答的核心问题与验证路线。"
        )
    if not brief["commentary"]:
        brief["commentary"] = (
            "文献述评：该方向已有研究大致分为理论建构、实证检验与应用落地三条脉络，"
            "共同结论是问题真实存在且值得研究，分歧集中在测量方式、样本范围与因果识别上，"
            "尚未解决的缺口正是本选题的切入点（此处为系统按题目自动生成的占位述评，"
            "建议生成时提供更具体的学科信息以获得真实文献短评）。"
        )
    if not brief["gap"]:
        brief["gap"] = as_text(topic.get("innovations")) or "待结合最新文献进一步明确。"
    if not brief["references"]:
        brief["references"] = [item["citation"] for item in brief["literature"] if item["citation"]]
    return brief, from_model


def normalize_topic(raw: Any, index: int) -> dict | None:
    if isinstance(raw, str):
        raw = {"title": raw}
    if not isinstance(raw, dict):
        return None
    title = as_text(pick(raw, "title", "topic", "题目", "标题"))
    if not title:
        return None
    topic = {
        "id": index + 1,
        "title": title.strip().strip("《》"),
        "english_title": as_text(pick(raw, "english_title", "title_en", "英文题目")),
        "type": as_text(pick(raw, "type", "research_type", "类型")) or "",
        "difficulty": as_text(pick(raw, "difficulty", "level", "难度")) or "",
        "summary": as_text(pick(raw, "summary", "one_liner", "概述", "简介")),
        "research_questions": as_list(pick(raw, "research_questions", "questions", "研究问题")),
        "theory": as_text(pick(raw, "theory", "theoretical_framework", "理论基础", "理论框架")),
        "methodology": as_text(pick(raw, "methodology", "methods", "研究方法", "方法")),
        "data_sources": as_text(pick(raw, "data_sources", "data", "数据来源", "数据")),
        "innovations": as_text(pick(raw, "innovations", "innovation", "创新点")),
        "feasibility": as_text(pick(raw, "feasibility", "可行性")),
        "risks": as_text(pick(raw, "risks", "pitfalls", "风险", "注意事项")),
        "keywords": as_list(pick(raw, "keywords", "关键词")),
    }
    if topic["type"] not in TOPIC_TYPES:
        for known in TOPIC_TYPES:
            if known[:2] in topic["type"]:
                topic["type"] = known
                break
        else:
            topic["type"] = topic["type"] or TOPIC_TYPES[1]
    if topic["difficulty"] not in DIFFICULTIES:
        text = topic["difficulty"]
        if any(word in text for word in ("本科", "易", "基础")):
            topic["difficulty"] = DIFFICULTIES[0]
        elif any(word in text for word in ("博士", "难", "核心", "CSSCI", "SCI")):
            topic["difficulty"] = DIFFICULTIES[2]
        else:
            topic["difficulty"] = DIFFICULTIES[1]
    brief, from_model = normalize_brief(pick(raw, "brief", "简报", "literature_brief", "短评"), topic)
    topic["brief"] = brief
    topic["brief_from_model"] = from_model
    return {key: topic[key] for key in _ALLOWED_TOPIC_KEYS if key in topic} | {
        "brief_from_model": topic["brief_from_model"]
    }


def normalize(raw: Any, discipline: str, requested: int, **meta) -> dict:
    """把模型输出规整成 {topics: [...], ...}。"""
    if isinstance(raw, list):
        raw = {"topics": raw}
    if not isinstance(raw, dict):
        raw = {}
    items = pick(raw, "topics", "items", "results", "题目", "选题")
    if items is None:
        # 模型可能直接返回单个题目对象
        items = [raw] if pick(raw, "title", "题目") else []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, (list, tuple)):
        items = []

    topics: list[dict] = []
    seen: set[str] = set()
    for item in items:
        topic = normalize_topic(item, len(topics))
        if not topic:
            continue
        fingerprint = re.sub(r"[\s《》\"'（）()的了与及和]", "", topic["title"])
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        topic["id"] = len(topics) + 1
        topics.append(topic)

    result = {
        "discipline": discipline,
        "requested": requested,
        "count": len(topics),
        "notice": as_text(pick(raw, "notice", "notes", "提示", "说明")) or "",
        "topics": topics,
    }
    result.update({k: v for k, v in meta.items() if v is not None})
    return result
