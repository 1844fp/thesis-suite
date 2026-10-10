"""论文多智能体系统（thesis-agents）。

七个能力对应七个智能体，共用一套框架：Agent 基类 + 工具注册表 + 消息总线
+ 黑板 + 状态机。框架本身不认识"论文"，它只认识 Artifact / Issue / Patch。

分层（依赖只能自上而下）：

    app/framework/   通用多智能体内核，不含任何论文领域知识
    app/agents/      七个智能体，各自声明能力和工具
    app/checks/      纯函数检查器（可离线、可单测、不调用模型）
    app/adapters/    对既有服务的 HTTP 客户端（8760 RAG / 8770 降 AIGC）
"""

__version__ = "0.1.0"
