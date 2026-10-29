"""网站板块定义：六个板块，每个板块由若干 agent 动作组成。

为什么把板块单独定义在一处
--------------------------
"板块"是给用户看的组织方式，"agent"是干活的对象，两者不是一一对应：
一个板块可能用到多个 agent（"定题目"要题目 agent + 知识库），
一个 agent 也可能服务多个板块（资料 agent 在"开题报告"和"写初稿"都用得上）。

所以**分别定义、按 id 关联**，而不是把板块信息塞进 agent 里。
前端只要读这一个接口就能把整个导航渲染出来；将来加板块或调顺序也只改这里。
"""

from __future__ import annotations

#: 六个板块。``actions`` 是该板块会调用的 agent 动作（用于前端提示与文档），
#: ``needs_key`` 表示这个板块是否有需要模型的步骤（不是"整个板块都需要"）。
MODULES: tuple[dict, ...] = (
    {
        "id": "topic",
        "n": "1",
        "name": "定题目",
        "desc": "先有一个能写下去的题，再把资料建成可检索的知识库",
        "panel": "topics",
        "actions": ["topic.generate", "topic.outline", "kb.add", "kb.search"],
        "needs_key": False,
        "note": "不填 Key 也能用：出题可走内置学科知识库；知识库入库与检索是纯本地的。",
    },
    {
        "id": "proposal",
        "n": "2",
        "name": "参考文献与知识库",
        "desc": "推荐参考文献；论文RAG知识库：入库 → 文档库 → 检索 / 总结 / 对比 / 问答",
        "panel": "gather",
        "actions": ["refs.recommend", "refs.to_kb", "kb.upload", "kb.search", "kb.doc_delete",
                    "pqa.index", "pqa.ask", "pqa.brief"],
        "needs_key": True,
        "note": "参考文献推荐、导出、入库、文档库与「文档检索」都不需要 Key；"
                "只有总结 / 对比 / 问答要模型（并且要先建索引）。",
    },
    {
        "id": "report",
        "n": "3",
        "name": "生成开题报告",
        "desc": "三层依据逐节成稿：本地知识库 + 联网真实文献 + 模型知识；参考文献按正文引用机械汇总",
        "panel": "proposal",
        "actions": ["proposal.options", "proposal.import_plan", "proposal.generate"],
        "needs_key": True,
        "note": "「导入任务书/模板读出框架与要求」不需要 Key（本地解析 + 入库）；"
                "只有「逐节生成」要模型。生成时联网检索会自动补充真实文献，可关闭。",
    },
    {
        "id": "draft",
        "n": "4",
        "name": "写初稿",
        "desc": "依据三层来源逐章生成正文，数字与引用会被逐条校验",
        "panel": "writing",
        "actions": ["writer.draft", "writer.export_markdown", "writer.export_docx"],
        "needs_key": True,
        "note": "可选联网检索真实文献；不填 Key 时只有导出类动作可用。",
    },
    {
        "id": "revise",
        "n": "5",
        "name": "修改与评估",
        "desc": "53 个检查器 + 文献核验 + 审稿意见书，按意见定点改写",
        "panel": "review",
        "actions": ["review.check", "review.verify_refs", "review.opinion",
                    "review.template", "writer.revise"],
        "needs_key": False,
        "note": "检查、核验、模板比对都不需要 Key；只有「审稿意见书」与「按意见改写」要模型。",
    },
    {
        "id": "integrity",
        "n": "6",
        "name": "查重与降 AIGC 率",
        "desc": "文档内部重复自查 + 参考文献可核验性；本地风格指标体检与改写",
        "panel": "integrity",
        "actions": ["review.duplication", "aigc.score", "aigc.reduce"],
        "needs_key": False,
        "note": "查重自查与 AIGC 体检不需要 Key。两件事都**不能替代学校查重系统**。",
    },
    {
        "id": "defense",
        "n": "7",
        "name": "答辩",
        "desc": "由稿件生成答辩稿（陈述 + 预计提问）与演示幻灯片",
        "panel": "defense",
        "actions": ["defense.script", "defense.slides", "defense.pptx", "defense.html"],
        "needs_key": True,
        "note": "需要模型生成内容；幻灯片的 .pptx 与 HTML 导出本身不需要 Key。",
    },
)

#: 板块 id -> agent 的 stage 归属。agent 的状态牌要挂到对应板块上。
MODULE_OF_AGENT = {
    "topic": "topic",
    "gather": "proposal",
    "writer": "draft",
    "review": "revise",
}


def describe() -> list[dict]:
    return [dict(m) for m in MODULES]


def get(module_id: str) -> dict | None:
    for m in MODULES:
        if m["id"] == module_id:
            return dict(m)
    return None
