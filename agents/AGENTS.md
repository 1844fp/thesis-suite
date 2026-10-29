# 智能体参考手册

19 个写作智能体的完整清单。每个条目写清**它做什么**与**它的纪律边界**——
后者比前者更重要：这些智能体里有一半的价值来自"它明确不做某事"。

---

# 一、thesis-agents（13 个）

框架层（`app/framework/agent.py`）：`Agent` 基类 + `AgentContext`（上下文）+
`AgentReport`（回报）+ `AgentRegistry`（注册表）+ `Ledger` / `CallRecord`（调用台账）。
每个智能体实现 `run(ctx) -> AgentReport`。

## 1. `gather:literature` — 资料获取

| | |
|---|---|
| 类 | `LiteratureGatherAgent` |
| 文件 | `app/agents/gather.py` |
| 能力编号 | ① |

按题目检索文献并落成结构化题录，供后续证据台账使用。

## 2. `rag:retrieve` — 知识库检索

| | |
|---|---|
| 类 | `KnowledgeRetrieveAgent` |
| 文件 | `app/agents/gather.py` |
| 能力编号 | ② |

从作者自己的资料库检索片段。**返回带出处**，因为写作层要靠出处做引用校验。

## 3. `rag:ask` — 知识库问答

| | |
|---|---|
| 类 | `KnowledgeQAAgent` |
| 文件 | `app/agents/gather.py` |
| 能力编号 | ② |

在检索结果之上回答问题，同样保留出处。

## 4. `writer:plan` — 写作规划

| | |
|---|---|
| 类 | `WriterPlanAgent` |
| 文件 | `app/agents/writer_plan.py` |
| 能力编号 | ③ |

**最有价值的产出不是大纲，而是"证据可得性报告"。**

理由（原文大意）：学位论文写作最常见的灾难不是"写得慢"，而是
**"先写了一章，才发现这一章要用的访谈数据根本拿不到"**。
所以这个智能体回答的核心问题是：*这篇论文该分几章，每一章现在写得了吗？*

## 5. `writer:draft` — 分节起草

| | |
|---|---|
| 类 | `WriterDraftAgent` |
| 文件 | `app/agents/writer_draft.py` |
| 能力编号 | ③ |

按章节逐节生成正文。证据来自证据台账，引用角标会被校验。

## 6. `writer:conclude` — 结论与总结

| | |
|---|---|
| 类 | `WriterConcludeAgent` |
| 文件 | `app/agents/writer_conclude.py` |
| 能力编号 | ③ |

## 7. `writer:polish` — 局部润色

| | |
|---|---|
| 类 | `WriterPolishAgent` |
| 文件 | `app/agents/writer_polish.py` |
| 能力编号 | ③ |

**这个智能体不改正文。** 它只对作者点名的片段给一份润色建议，写进 `Issue` 等人看。

为什么"能改也不改"：项目里已经有两条会**真正改写**稿件的链路——
自我更正循环（针对已确认的错误）和降 AIGC 环节（针对表达风格）。
写作助手再插一手，会出现三条链路互相覆盖同一段文字，
而作者无法判断最终版本是谁改的。

## 8. `writer:rewrite` — 按作者要求改写

| | |
|---|---|
| 类 | `WriterRewriteAgent` |
| 文件 | `app/agents/writer_rewrite.py` |
| 能力编号 | ③ |

与 `writer:polish`、`fixer` 的分工在源码里有专门一节说明（三者都会触及文字，
但触发者不同：作者点名要求 / 作者点名求建议 / 系统已确认的问题）。

## 9. `writer:references` — 参考文献表生成

| | |
|---|---|
| 类 | `WriterReferencesAgent` |
| 文件 | `app/agents/writer_references.py` |
| 能力编号 | ③ |

## 10. `references:build` — 参考文献表构建

| | |
|---|---|
| 类 | `ReferencesBuildAgent` |
| 文件 | `app/agents/references_build.py` |

**与 `writer:references` 做的是相反方向的事**，容易混淆：

* `references:build` —— 把**抽取结果**变成规范的 `references` 工件（自下而上）；
* `writer:references` —— 在**写作过程中**产出 `references`（自上而下）。

## 11. `verify:external` — 外部核验

| | |
|---|---|
| 类 | `ExternalVerifyAgent` |
| 文件 | `app/agents/verify_external.py` |
| 能力编号 | ⑥ |

**为什么存在**：AI 编造参考文献是硕士论文最致命的错误之一，而且**极难自查**——
条目格式看起来完全正常，作者、年份、卷期页一应俱全，作者本人也无从判断。

所以它拿题录去公开文献库核对，并对静态数据做一致性检查。

## 12. `format:apply` — 格式整理

| | |
|---|---|
| 类 | `FormatApplyAgent` |
| 文件 | `app/agents/format_apply.py` |
| 能力编号 | ⑤ |

**职责边界要说清楚，因为这个能力最容易被误解：**

* **它做**：带着学校规范去检查 docx 的排版属性（字体、字号、行距、页边距、
  缩进、标题层级），把偏差逐条报成 `Issue`，并给出可执行的修改指引。

## 13. `fixer` — 修复器

| | |
|---|---|
| 类 | `FixerAgent` |
| 文件 | `app/agents/fixer.py` |

自我更正循环里**最容易被做坏的一环**。源码里写着三条纪律，第一条是：

> **能确定性修复的，不要问模型。** 标点规范化、删套话、编号重排、删重复段落
> 这些都有唯一正确答案，交给模型只会引入不确定性，还白花钱。
> 只有"改哪一处、改成什么"确实有判断空间时，才值得调用模型。

配套的整体纪律：**检查器与修复器强制分离**——修复器无权宣布自己成功，
成功与否由检查器复验决定。

---

# 二、thesis-suite（6 个）

站点层智能体。与上面的区别：**上面是"能力"，这里是"板块"**。
每个智能体可单独寻址（`POST /api/agents/{name}/{action}`），
共享同一会话状态（证据 / 稿件 / 评估结果 / 黑板），每次动作写一条事件。

| # | 名称 | 类 | 动作 | 职责 |
|---|---|---|---|---|
| 1 | `topic` | `TopicAgent` | 2 | 出题（离线知识库 / 联网）+ 由题目生成章节大纲 |
| 2 | `gather` | `GatherAgent` | 4 | 导入文件、抽成证据、核验真伪、导出参考文献表 |
| 3 | `writer` | `WriterAgent` | 4 | 逐章写初稿、按意见定点改写、导出 Markdown / Word |
| 4 | `review` | `ReviewAgent` | 5 | 53 个检查器、文献核验、审稿意见书、查重、模板比对 |
| 5 | `integrity` | `IntegrityAgent` | 4 | 内部重复自查、AI 味体检、降 AIGC 改写、导出 |
| 6 | `defense` | `DefenseAgent` | 4 | 答辩稿（陈述 + 预计提问）、幻灯片（.pptx / HTML） |

## 动作清单

| 智能体 | 动作 | 需要 Key |
|---|---|---|
| `topic` | `generate` | 否（可走离线） |
| | `outline` | 是 |
| `gather` | `import` | 否 |
| | `extract` | 是 |
| | `verify` | 否 |
| | `export` | 否 |
| `writer` | `draft` | 是 |
| | `revise` | 是 |
| | `export_markdown` | 否 |
| | `export_docx` | 否 |
| `review` | `check` | 否 |
| | `verify_refs` | 否 |
| | `opinion` | 是 |
| | `duplication` | 否 |
| | `template` | 否 |
| `integrity` | `duplication` | 否 |
| | `aigc_score` | 否 |
| | `aigc_reduce` | 否（**默认关闭**，见下） |
| | `aigc_export_docx` | 否 |
| `defense` | `script` | 是 |
| | `slides` | 是 |
| | `pptx` | 否 |
| | `html` | 否 |

**`aigc_reduce` 默认关闭的原因**：它依赖的改写服务只读自己的 `.env`，
不支持转发访客凭据——也就是费用记在**服务提供方**而不是访客身上。
所以默认只开放纯本地的 `aigc_score`（体检不调模型、不花钱）。
站主设 `SUITE_ALLOW_AIGC_REDUCE=1` 才开启。

## 设计纪律（与 thesis-agents 一致）

* **agent 之间不直接互相调用**，通过会话状态解耦——任何一个都能单独用；
* **动作走白名单**，未登记的动作返回 404，不是"什么都能调"的后门；
* **检查器与改写器分离**，改写器无权宣布自己成功；
* 凡是查不出来的，就**说查不出来**（查重只做文档内部自查 + 文献可核验性）。

---

# 三、两份清单的关系

```
thesis-agents（能力层，13 个）
    ├── 资料获取 / RAG 检索 / 问答
    ├── 写作规划 / 起草 / 结论 / 润色 / 改写 / 参考文献 ×2
    ├── 外部核验
    ├── 格式整理
    └── 修复器
                ↓ 能力重组为板块
thesis-suite（板块层，6 个）→ 对外可用站点
```

**为什么两套都要留**：能力层是"零件"，每个都能单独跑、单独测；
板块层是"成品"，面向使用者。把零件直接暴露给用户会让他面对 19 个名字
却不知道从哪开始；把成品当成唯一实现又会让每个能力失去独立可测性。
