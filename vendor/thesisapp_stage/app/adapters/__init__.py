"""对既有服务的 HTTP 客户端。

三个原则：

1. **不复制它们的代码。** thesis-assistant 的 RAG 和 aigc-reducer 的改写管线都已经
   在跑、有测试、有 README。这里只做协议适配。
2. **服务不可用要显式失败，不要静默降级。** 检索不到证据却继续生成，会产出一篇
   看起来正常、实则无依据的稿子——这比直接报错危险得多。
3. **同一把尺子。** 降 AIGC 的前后指数由 aigc-reducer 自己计算，不在本地重算一遍，
   否则两个数字对不上时无法判断是谁错了。
"""

from .base import PeerError, PeerHealth, PeerHTTP
from .rag import Hit, RAGAdapter, RAGAnswer
from .reducer import ReduceResult, ReducerAdapter, ScoreReport
from .research import (
    ArxivAdapter,
    CrossrefAdapter,
    ExternalVerifier,
    IntakeItem,
    LocalIntake,
    OpenAlexAdapter,
    WorkRecord,
)

__all__ = [
    "ArxivAdapter",
    "CrossrefAdapter",
    "ExternalVerifier",
    "Hit",
    "IntakeItem",
    "LocalIntake",
    "OpenAlexAdapter",
    "PeerError",
    "PeerHTTP",
    "PeerHealth",
    "RAGAdapter",
    "RAGAnswer",
    "ReduceResult",
    "ReducerAdapter",
    "ScoreReport",
    "WorkRecord",
]
