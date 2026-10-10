"""降 AIGC 率适配器（对端：aigc-reducer，默认 8770）。

只做两件事，都是调用对端而不是在本地重写算法：

* ``score``  —— 拿权威的 AI 味指数和 11 项子指标（**同一个尺子**）
* ``reduce`` —— 走对端那条已经跑通的管线：体检 → 分段改写 → 保真护栏 → 复检
               → 事实核查 → 收敛

刻意不在本地实现"指数计算"：本项目若自己算一遍，两个数字不一致时无法判断
谁对。**测量必须由被测量的系统之外的东西来做，而且只能有一个。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

from .base import PeerError, PeerHTTP


@dataclass
class ScoreReport:
    """一次风格体检的结果。"""

    index: float = 0.0
    metrics: dict[str, Any] = field(default_factory=dict)
    rules: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ScoreReport":
        metrics = d.get("metrics") or {}
        # 对端可能返回 dict 或 list，两种都接住，避免接口微调就崩。
        if isinstance(metrics, list):
            metrics = {
                str(m.get("key") or m.get("name") or i): m for i, m in enumerate(metrics)
            }
        return cls(
            index=float(d.get("index") or 0.0),
            metrics=metrics,
            rules=list(d.get("rules") or d.get("hits") or []),
            warnings=list(d.get("warnings") or []),
            raw=d,
        )

    @property
    def ok(self) -> bool:
        return bool(self.raw)

    def as_dict(self) -> dict:
        return {
            "index": round(self.index, 1),
            "metrics": self.metrics,
            "rule_hits": len(self.rules),
            "warnings": self.warnings,
        }

    def top_suspects(self, n: int = 5) -> list[tuple[str, float]]:
        """Sub-metrics that look most machine-written, for reporting."""
        rows: list[tuple[str, float]] = []
        for key, m in self.metrics.items():
            if not isinstance(m, dict):
                continue
            susp = m.get("suspicion")
            if susp is None:
                susp = m.get("score")
            try:
                rows.append((str(m.get("label") or key), float(susp)))
            except (TypeError, ValueError):
                continue
        return sorted(rows, key=lambda r: -r[1])[:n]


@dataclass
class ReduceResult:
    text: str = ""
    original_text: str = ""
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    rounds: int = 0
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    fact_check: list[dict] = field(default_factory=list)
    paragraphs: list[dict] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict) -> "ReduceResult":
        before = d.get("before") or {}
        after = d.get("after") or {}
        fc = d.get("fact_check") or []
        if isinstance(fc, dict):
            fc = list(fc.get("findings") or [])
        return cls(
            text=str(d.get("text") or ""),
            original_text=str(d.get("original_text") or ""),
            before=before if isinstance(before, dict) else {},
            after=after if isinstance(after, dict) else {},
            rounds=int(d.get("rounds") or 0),
            stats=dict(d.get("stats") or {}),
            warnings=list(d.get("warnings") or []),
            fact_check=list(fc),
            paragraphs=list(d.get("paragraphs") or []),
            raw=d,
        )

    @property
    def index_before(self) -> float:
        return float(self.before.get("index") or 0.0)

    @property
    def index_after(self) -> float:
        return float(self.after.get("index") or 0.0)

    def as_dict(self) -> dict:
        return {
            "index_before": round(self.index_before, 1),
            "index_after": round(self.index_after, 1),
            "rounds": self.rounds,
            "stats": self.stats,
            "warnings": self.warnings,
            "fact_check_findings": len(self.fact_check),
            "chars": len(self.text),
        }


class ReducerAdapter(PeerHTTP):
    label = "降 AIGC 率工作台（aigc-reducer）"
    start_hint = "请在 E:\\AI\\aigc-reducer 运行 .\\run.cmd 启动它。"

    # -- 体检 -------------------------------------------------------------
    def score(self, text: str) -> ScoreReport:
        """本地风格体检。**不调用大模型，不花钱**，可以反复跑。"""
        if not (text or "").strip():
            raise PeerError("要体检的文本为空", kind="input")
        return ScoreReport.from_dict(self.post("/api/score", {"text": text}))

    def guard(self, original: str, rewritten: str, *, keep_terms: list[str] | None = None) -> dict:
        """保真校验：数字、引用、引文、术语有没有被改坏。

        自我更正循环会用它兜底——**任何对稿件的自动改写都必须先过这一关**，
        包括我们自己生成的补丁。
        """
        return self.post(
            "/api/guard",
            {"original": original, "rewritten": rewritten, "keep_terms": keep_terms or []},
        )

    def rules(self) -> dict:
        return self.get("/api/rules")

    def metrics(self) -> dict:
        return self.get("/api/metrics")

    # -- 改写 -------------------------------------------------------------
    def reduce_stream(
        self,
        text: str,
        *,
        genre: str = "",
        intensity: str = "",
        target_score: float | None = None,
        max_rounds: int | None = None,
        concurrency: int | None = None,
        fact_check: bool | None = None,
        keep_terms: list[str] | None = None,
        style_sample: str = "",
        consistency: bool | None = None,
    ) -> Iterator[dict]:
        body: dict[str, Any] = {"text": text}
        if genre:
            body["genre"] = genre
        if intensity:
            body["intensity"] = intensity
        if target_score is not None:
            body["target_score"] = target_score
        if max_rounds is not None:
            body["max_rounds"] = max_rounds
        if concurrency is not None:
            body["concurrency"] = concurrency
        if fact_check is not None:
            body["fact_check"] = fact_check
        if keep_terms:
            body["keep_terms"] = keep_terms
        if style_sample:
            body["style_sample"] = style_sample
        if consistency is not None:
            body["consistency"] = consistency
        yield from self.stream_lines("/api/reduce/stream", body)

    def reduce(self, text: str, *, on_event=None, **kwargs: Any) -> ReduceResult:
        """走完整降 AIGC 管线（会消耗模型额度）。"""
        result: ReduceResult | None = None
        for ev in self.reduce_stream(text, **kwargs):
            if on_event is not None:
                on_event(ev)
            kind = ev.get("type")
            if kind == "final":
                result = ReduceResult.from_dict(ev.get("result") or {})
            elif kind == "error":
                raise PeerError(f"降 AIGC 失败：{ev.get('message')}", kind="reduce")
        if result is None:
            raise PeerError("降 AIGC 没有返回最终结果（流提前结束）", kind="reduce")
        return result
