# AI 写作多智能体 · 本机项目清单与智能体索引

这个目录把散落在本机的**AI 写作多智能体**项目集中索引起来：谁有几个智能体、
每个智能体负责什么、它们怎么串成一条流水线。

为什么值得单独索引：这些项目是**分批做出来的**，彼此独立开发、目录分散，
"哪个项目里有哪些智能体"只存在于各自的 README 里。时间一长，
连作者自己都说不清"我到底写了几个智能体、哪个是哪个"——而这个数字正是
判断"这件事做到什么程度"的最直接依据。

---

## 一、盘点结论

本机共有 **19 个写作相关智能体**，分布在 2 个多智能体项目里：

| 项目 | 智能体数 | 定位 | 代码位置 |
|---|---|---|---|
| **thesis-agents** | **13** | 论文多智能体框架（能力①②③⑤⑥） | `E:\AI\thesis-agents` |
| **thesis-suite** | **6** | 七板块整合站点（对外可用） | `E:\AI\thesis-suite`（本仓库） |

另有 **6 个支撑工具**不属于多智能体，但都是这条流水线上的能力单点
（见第五节）。它们的代码不在本仓库，本目录只收录说明文档。

---

## 二、thesis-agents：13 个智能体

自研多智能体框架（Agent 基类 / 工具注册表 / 消息总线 / 黑板 / 状态机），
约 4.5 万行，45 个离线检查器，30 类 Word 格式规则。

**核心设计**：检查器与修复器强制分离（修复器无权宣布自己成功）；
模型只能申请调用确定性算子，不能直接改文本；稿件为不可变版本链；
消息全量落盘可追溯。

| # | 注册名 | 类 | 职责 |
|---|---|---|---|
| 1 | `gather:literature` | `LiteratureGatherAgent` | 资料获取（能力①） |
| 2 | `rag:retrieve` | `KnowledgeRetrieveAgent` | 知识库检索（能力②） |
| 3 | `rag:ask` | `KnowledgeQAAgent` | 知识库问答（能力②） |
| 4 | `writer:plan` | `WriterPlanAgent` | 写作规划 + **证据可得性报告** |
| 5 | `writer:draft` | `WriterDraftAgent` | 分节起草 |
| 6 | `writer:conclude` | `WriterConcludeAgent` | 结论与总结 |
| 7 | `writer:polish` | `WriterPolishAgent` | 局部润色（**只给建议，不改正文**） |
| 8 | `writer:rewrite` | `WriterRewriteAgent` | 按作者要求改写 |
| 9 | `writer:references` | `WriterReferencesAgent` | 参考文献表生成（写作正向） |
| 10 | `references:build` | `ReferencesBuildAgent` | 参考文献表构建（抽取反向） |
| 11 | `verify:external` | `ExternalVerifyAgent` | 外部核验 + 静态数据一致性 |
| 12 | `format:apply` | `FormatApplyAgent` | 格式整理（docx 排版属性） |
| 13 | `fixer` | `FixerAgent` | 修复器（问题清单 → 可复验补丁） |

详细职责见 [AGENTS.md](AGENTS.md)，框架层设计见 [projects/thesis-agents/README.md](projects/thesis-agents/README.md)。

## 三、thesis-suite：6 个智能体

把上述能力重组为对外可用的七板块站点。与原框架的区别：
**原框架的智能体是"能力"，这里是"板块"**——一个板块会调用多个 agent 动作。

| # | 名称 | 类 | 动作数 | 服务的主要板块 |
|---|---|---|---|---|
| 1 | `topic` | `TopicAgent` | 2 | 定题目（含 RAG 知识库） |
| 2 | `gather` | `GatherAgent` | 4 | 参考文献与知识库 |
| 3 | `writer` | `WriterAgent` | 6 | 生成开题报告 + 写初稿 |
| 4 | `review` | `ReviewAgent` | 5 | 修改与评估 |
| 5 | `integrity` | `IntegrityAgent` | 4 | 查重与降 AIGC 率 |
| 6 | `defense` | `DefenseAgent` | 4 | 答辩 |

合计 **25 个动作**。注意 agent 与板块**不是一一对应**：`writer` 同时服务
「生成开题报告」与「写初稿」两个板块，`gather` 的动作分散在板块 1 和 2。
这正是把"能力"与"流程"分开定义的理由——硬绑成一对一，
要么板块被拆碎，要么动作被硬塞。

这 6 个智能体可单独寻址（`POST /api/agents/{name}/{action}`）、共享会话状态、
每次动作写事件流。详见 [projects/thesis-suite.md](projects/thesis-suite.md)。

## 四、版本谱系（重要：本仓库不是最新一代）

这条必须写在前面，否则本索引会把中间一代说成终点：

| 代 | 形态 | 阶段数 | 位置 | 状态 |
|---|---|---|---|---|
| 一代 | Python 单能力脚本集 | — | `thesis-topic-helper` 等 | 已被整合 |
| 二代 | **Python 七板块站点** | 7 | **本仓库** / `E:\AI\thesis-suite` | 可用，本仓库内容 |
| 三代 | **研序 · 七阶段**（`modern/`，Node，无第三方运行依赖） | 7 | `E:\AI\codex\writing-workspace\modern` | **更新**，另有私有在线站点 |

三代的关键升级（照 `UPGRADE.md` 原文）：七阶段项目工作流、持久化、稳定证据编号、
证据快照、检查点与版本恢复；模型密钥只在**页面内存**中使用；
逐句引用与数字检查、结果章缺口处理；答辩讲稿、提纲、问答与本页模拟练习，
Word / PowerPoint / Markdown / 离线演示稿导出。

三代的正确用法（原文）：Windows 运行根目录 `启动新版工作台.cmd`；
跨平台进 `modern/` 跑 `node server.mjs`，打开 `http://127.0.0.1:8799`，需 Node.js 22+。
旧版的 PDF/OCR 与复杂 Word 格式处理**继续可用**——三代导入 DOCX 只提取正文，
公式、图片、复杂表格仍需回原件核对。

说明文档：[projects/yanxu/README.md](projects/yanxu/README.md)、
[projects/yanxu/UPGRADE.md](projects/yanxu/UPGRADE.md)。

**为什么两代都留着**：三代在 Node 侧重写，旧版 Python 的 PDF/OCR 与 Word 格式处理
没有被替代。`UPGRADE.md` 明确写了"新版不覆盖旧版 `/api/writing` 等接口"，
两者通过 `/api/v2` 与 `src/` 并存。所以这不是"新版替换旧版"，是"新旧共存"。

## 五、它们怎么串起来

完整流水线见 [PIPELINE.md](PIPELINE.md)。一句话概括：

```
定题目 → 开题报告 → 写初稿 → 修改与评估 → 查重与降AIGC → 答辩
  ①        ②          ③          ④            ⑤            ⑥
```

（三代把这条链细化为七阶段，见 `projects/yanxu/README.md`。）

## 六、支撑工具（单智能体 / 非智能体）

都是这条流水线上的能力单点，代码在各自项目里，本目录只收录说明：

| 项目 | 做什么 | 说明文档 |
|---|---|---|
| `paper-quality-assistant` | 论文质量评估：三层证据链算幻觉率 + 按意见自动改写 | [projects/paper-quality-assistant.md](projects/paper-quality-assistant.md) |
| `cnki-agent` | 知网文献采集：替你点鼠标，按人的节奏下载题录与 PDF | [projects/cnki-agent.md](projects/cnki-agent.md) |
| `aigc-reducer` | 降 AIGC 率：11 项风格指标 + 29 条规则 → 改写 → 保真校验 → 复检 | [projects/aigc-reducer.md](projects/aigc-reducer.md) |
| `thesis-topic-helper` | 题目生成（学科知识库 + 联网辅助） | [projects/thesis-topic-helper.md](projects/thesis-topic-helper.md) |
| `thesis-assistant` | RAG 知识库服务（本地上游服务） | [projects/thesis-assistant.md](projects/thesis-assistant.md) |
| `skill-radar` | AI 写作 skill / 工具雷达：抓 GitHub、排序、生成离线仪表盘 | [projects/skill-radar.md](projects/skill-radar.md) |

## 七、第三方项目（**只链接，不收录代码**）

本机还存有以下第三方项目。它们**不是**自研成果，代码与文档的版权属于原作者，
所以这里只给链接与用途说明，**不把内容搬到本仓库**：

| 项目 | 来源 | 用途 |
|---|---|---|
| Supervisor-Skills | <https://github.com/HKUSTDial/Supervisor-Skills> | 把博导科研经验做成 AI 副导师 skill 集（12 个 SKILL.md） |
| codex/skills-library | 多个开源 skill 库（含 Orchestra-Research 等） | 学术写作相关 skill 合集（academic-paper / deep-research 等） |

**为什么这条边界要划清**：把别人的仓库内容转推到自己的公开仓库，
即使加了署名也不妥当——许可证要求、版本追溯、以及"看起来像自己的成果"
这三件事都会出问题。要用就直接引上游。

---

## 八、关于转载文档里的链接

`projects/` 下所有文件都是**原样转载**，正文一字未改。因此其中指向原项目
**代码目录**的链接（例如 `projects/yanxu/README.md` 里的 `modern/`）
在本仓库内**不会解析**——我们只收文档，不收代码。

这是有意的：把代码搬过来会让本仓库变成"多个项目的混合体"，
而每个项目的代码都有自己的依赖与运行方式，混在一起对谁都没好处。
要看代码请直接去对应项目目录。全文 29 条相对链接中，只有这 2 条属于这种情况。

## 九、这份索引的维护约定

* 新增智能体 → 更新 [AGENTS.md](AGENTS.md) 的表格，并说明它属于哪个板块；
* 新增项目 → 在第六节加一行，说明文档放进 `projects/`；
* 第三方项目 → 只加成第七节那样的一行链接，**不要把代码或大段文档搬进来**。
