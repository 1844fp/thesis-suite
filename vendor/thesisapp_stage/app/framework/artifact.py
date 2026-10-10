"""框架的四类基本对象：Artifact / Issue / Patch / Stage。

这些是七个智能体之间的公共语言。把它们定义清楚，智能体之间就不需要互相认识。
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    """问题的严重程度。分级不是为了好看，是为了决定"能不能不管它"。"""

    INFO = "info"          # 提示：可忽略
    MINOR = "minor"        # 轻微：影响观感
    MAJOR = "major"        # 重要：会被评审抓住
    BLOCKER = "blocker"    # 致命：学术不端、结论无依据、引用不存在

    @property
    def rank(self) -> int:
        return {"info": 0, "minor": 1, "major": 2, "blocker": 3}[self.value]


class Stage(str, Enum):
    """一次完整流程的阶段。状态机按这个顺序推进，但可以回跳。"""

    INTAKE = "intake"        # 收稿：解析 .docx/.md，建立项目
    GATHER = "gather"        # 资料获取：检索、入库
    RETRIEVE = "retrieve"    # RAG：检索证据
    DRAFT = "draft"          # 写作
    VERIFY = "verify"        # 验证（静态 + 外部）
    FIX = "fix"              # 自我更正
    DEDUP = "dedup"          # 降 AIGC
    FORMAT = "format"        # 格式整理
    FINAL = "final"          # 定稿导出


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def text_hash(text: str) -> str:
    """Content hash used to detect no-op rewrites and to cache verifications."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


@dataclass
class Issue:
    """检查器发现的一个问题。

    这个结构是自我更正循环的燃料：没有 `evidence` 和 `evidence_span` 的问题无法
    验证也无从修改，所以检查器宁可漏报也不要给出没有证据的问题。
    """

    id: str
    kind: str
    """问题类别，例如 `citation.missing_in_reflist`。命名用 `域.具体问题`。"""

    severity: Severity
    message: str
    """给人看的一句话描述。"""

    #: 问题在稿件中的定位。自由结构，但约定用这些键：
    #: ``paragraph``(段落下标) / ``char``(字符偏移) / ``line``(行号)
    #: / ``section``(章节) / ``ref``(参考文献键)
    location: dict[str, Any] = field(default_factory=dict)

    evidence: str = ""
    """支撑这条问题的原文片段。必须来自稿件本身或外部查询结果，不能是模型猜测。"""

    #: 判定依据。"rules:ref_numbering" 表示纯规则命中，"llm:claim_support" 表示
    #: 模型判断。两者可信度不同，界面要分开呈现。
    detected_by: str = "rules"

    #: 模型自己有多确定（0~1）。规则类问题留 1.0。低置信度问题不应驱动自动修改。
    confidence: float = 1.0

    fixable: bool = False
    """是否可机械修复。**只对格式、编号、字面一致性问题为 True。**

    需要作者判断的问题（如"结论过度推断"）即使能想出改法也必须标 False——
    由循环自动改写学术论断是危险的。
    """

    fix_op: str = ""
    """修复算子名（见 ``framework.ops``）。`fixable=True` 时必须有值。"""

    fix_hint: str = ""
    """给修复器的具体建议，或给作者的人工修改指引。"""

    status: str = "open"
    """open | fixed | wontfix | needs_author"""

    resolved_by: str = ""
    resolved_at_stage: str = ""
    resolution_note: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @property
    def rank(self) -> int:
        return self.severity.rank


@dataclass
class Artifact:
    """被处理的物件。

    ``text`` 与 ``payload`` 并存：文本类物件（稿件）用 `text`，结构化物件
    （参考文献表、格式报告）用 `payload`。两者可以同时有。
    """

    id: str
    kind: str
    """draft | references | evidence | verification_report | format_report | fix_plan"""

    text: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    version: int = 1

    created_by: str = ""
    created_at_stage: str = ""
    #: 上一版本 id，构成版本链。自我更正循环靠它回答"改了哪一版、改了什么"。
    parent: str = ""
    #: 生成这个物件的智能体运行 id（runs 日志里的 run_id）
    run_id: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return text_hash(self.text) if self.text else text_hash(repr(sorted(self.payload.items())))

    def as_dict(self, *, with_text: bool = True) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "version": self.version,
            "created_by": self.created_by,
            "created_at_stage": self.created_at_stage,
            "parent": self.parent,
            "run_id": self.run_id,
            "notes": list(self.notes),
            "content_hash": self.content_hash,
            "chars": len(self.text),
            "text_preview": self.text[:400],
            "payload_keys": sorted(self.payload.keys()),
        }
        if with_text:
            d["text"] = self.text
            d["payload"] = self.payload
        return d


@dataclass
class Patch:
    """一次修复动作。

    ``ops`` 里的每个操作都必须在 ``framework.ops`` 里注册过。这样做的理由是：
    修复不能是"模型说它改好了"，必须是**在原文上做了一次可审计的文本变换**，
    且变换前后能被同一批检查器重新验证。
    """

    id: str
    issue_ids: list[str] = field(default_factory=list)
    ops: list[dict[str, Any]] = field(default_factory=list)
    rationale: str = ""
    created_by: str = ""

    applied: bool = False
    error: str = ""
    #: 应用失败的位置，便于把问题原样报给作者
    failed_op_index: int = -1

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class OpResult:
    """算子执行结果。改动前后的文本一起留着，便于展示 diff 和单步回滚。"""

    ok: bool
    text: str
    changed: bool = False
    detail: str = ""
    error: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def severity_from(value: Any) -> Severity:
    if isinstance(value, Severity):
        return value
    try:
        return Severity(str(value).strip().lower())
    except ValueError:
        return Severity.MINOR
