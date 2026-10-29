<!--
  本文件是从本机项目 thesis-agents 的 README.md **原样转载**的。
  转载目的：把散落在各项目的说明集中到一处索引，便于查阅。
  正文未经修改；如果这里的内容与该项目目录里的 README.md 不一致，
  以项目目录里的为准（那份是随代码演进的）。

  来源：E:\AI\thesis-agents\README.md
  收录时间：2026-10-29
-->

> **本文档为转载**，来源 `E:\AI\thesis-agents\README.md`（论文多智能体框架 · 13 个智能体 · 约 4.5 万行 · 该项目的说明文档）。正文未作修改。

---

# 论文多智能体系统 · thesis-agents

> **一句话**：7 个智能体协作，把"AI 代写论文"改造成"可核查的稿件改进流水线"。
>
> **技术栈**：Python · FastAPI · 自研多智能体框架（Agent 基类 / 工具注册表 / 消息总线 / 黑板 / 状态机）
>
> **规模**：约 4.5 万行 · 7 个智能体 · 45 个离线检查器 · 30 类 Word 格式规则
>
> **核心设计**：检查器与修复器强制分离（修复器无权宣布自己成功）·
> 模型只能申请调用确定性算子，不能直接改文本 · 稿件为不可变版本链 · 消息全量落盘可追溯

**目录**：[架构](#四架构为什么这样分层) · [快速开始](#二快速开始) · [已知限制](#七已知限制请先读这一节) · [命令行速查](#十命令行速查)

---

面向中文学位论文的**多智能体协作系统**。七个能力不是七个按钮，而是七个各司其职的
智能体，共用一套框架（Agent 基类 + 工具注册表 + 消息总线 + 黑板 + 状态机）。

这不是"AI 帮你写论文"。它的定位是：**把你已经写好的稿子，变成一份经得起盲审的稿子**，
并且在这条链路上守住一条底线——**凡是没有依据的话，工具不替你说**。

---

## 一、七个能力与它们的边界

| # | 能力 | 智能体 | 会不会改你的稿子 |
|---|---|---|---|
| ① | 资料获取 | `gather:literature` | 不会。只扫描、判定、触发入库 |
| ② | 论文 RAG 知识库 | `rag:retrieve` / `rag:ask` | 不会。只取回带引用的原文片段 |
| ③ | 写作助手 | `writer:plan` / `writer:draft` / `writer:conclude` / `writer:references` / `writer:polish` | **会**（写作阶段，产生新版本） |
| ④ | 降 AIGC 率 | `dedup:reduce` | **会**（但过不了保真校验就退回原文） |
| ⑤ | 格式整理 | `format:apply` + 30 类规则 + `docx-format` 写回 | **会**改排版属性（文字一字不改，失败自动还原） |
| ⑥ | 资料与数据验证 | `verify:external` + 45 个检查器 | 不会。只产出问题清单 |
| ⑦ | 自我更正 | `fixer` + 状态机循环 | **会**，但只改「可机械修复」的问题 |

### 写作按三个阶段走（开题报告 → 初稿 → 终稿）

三个阶段是**交付单元**，各有自己的门槛、产出结构与通过标准；
`stage`（retrieve/draft/verify/…）是**执行单元**。混用会让「我只想重跑校对」
变成「我把整篇重写一遍」。

```powershell
.\run_python.cmd -m app.cli phases                    # 三个阶段各要什么、门槛是什么、绑了哪些技能
.\run_python.cmd -m app.cli proposal --topic "题目"    # 阶段一：开题报告（固定 7 节，约 8000 字）
.\run_python.cmd -m app.cli thesis   --topic "题目"    # 阶段二：初稿（按开题或论文类型的章节树）
.\run_python.cmd -m app.cli final -p <项目id>          # 阶段三：打磨 → 校对修复 → 预审报告 → 终稿
.\run_python.cmd -m app.cli idea --topic "题目"        # 开题前置：选题体检（skeleton 查逻辑骨架自洽）
```

* **开题**先过选题体检与逻辑骨架自洽——不过就别花那几十次模型调用；
* **初稿**每句话都要能溯源，写作检查器逐条过；
* **终稿不重新起草**：只跑 `verify → fix → final`，加一轮可选的语言打磨与预审报告。
  一个会把你写好的段落重写一遍的「终稿」功能，没人敢用。

界面上：**工具 → 三阶段写作** 可以看到三段的门槛与清单、做选题体检 / 返修指引；
项目页的「跑终稿阶段」按钮执行第三段。详见 [`三阶段写作说明.md`](三阶段写作说明.md)。

### 内置本机的技能包（两个上游）

本机 `E:\AI\.dsh\skills` 里的 **17 个技能包 + 18 篇手册章节**会被读出来，
按阶段**注入提示词**、**落成检查器**、**变成工具**（选题体检／骨架自洽／预审／返修指引）。

两个上游，两套许可，都只**读不抄**：

| 上游 | 许可 | 技能 |
|---|---|---|
| [Supervisor-Skills](https://github.com/HKUSTDial/Supervisor-Skills) | CC-BY-4.0 / CC-BY-NC-SA-4.0 | 12 个（选题体检、逻辑骨架、正文写作、引言六段式、预审、润色、返修…） |
| [academic-research-skills](https://github.com/Imbad0202/academic-research-skills)（52k star） | CC BY-NC 4.0 | 5 个（academic-paper 写作流水线、多视角评审、全流程编排、文献筛选、深度调研） |

```powershell
.\run_python.cmd -m app.cli skills list               # 内置了什么、每条绑了哪个阶段
.\run_python.cmd -m app.cli skills show paper-writer  # 某条的「内置指令」与「没内置的部分」
.\run_python.cmd -m app.cli skills search paper       # 去 GitHub 找高星技能（按 star 排序）
.\run_python.cmd -m app.cli skills install owner/repo  # 装（不用 git，走 codeload + 标准库解压）
```

**不复制技能正文**：两个上游都要求署名、都限制商用，复制进本仓库等于再分发；
而且真正该进提示词的是短指令，不是几万字的规范全文（academic-paper 的 SKILL.md 就有 65KB）。
所以只保留本项目的**原创提炼**（`app/skills_registry.yaml`），每条都注明来源与
「没内置的部分」（联网检索、读图、作者判断这些**没有**内置，别以为装了技能就都有了）。
按技能写的 25 个检查器分放在 `app/checks/skills_rules.py`（Supervisor 那批）
与 `app/checks/skills_rules_ars.py`（academic-research-skills 那批）——
**分开是为了让"这条规矩出自哪个上游、许可是什么"一眼可见**。
详见 [`Skills集成说明.md`](Skills集成说明.md)。

**最重要的分界线**：需要作者判断的问题（结论是否成立、创新点是否够、论证是否充分）
**永远不会被自动改写**。它们会被标成 `needs_author` 交还给你。原因是——自动改写学术论断
是危险的，工具改得越"顺"，你越不会去核对。

### 接不同的模型 API，并按用途分配

一次生成要调用几十次模型，而这些调用性质不同：**起草正文**决定成稿质量，
**核验与自动修复**是判断句和机械改写（实测修复一项就调了 23 次）。
按用途分开配，就能"正文用强模型、核验用便宜模型"，成本降下来而质量不受影响。

```powershell
.\run_python.cmd -m app.cli models presets                              # 看有哪些厂商可选
.\run_python.cmd -m app.cli models add --id deepseek --preset deepseek --key sk-xxx --use
.\run_python.cmd -m app.cli models add --id cheap --preset zhipu --model glm-4-flash --key yyy
.\run_python.cmd -m app.cli models role --set verify=cheap --set fix=cheap   # 核验与修复走便宜档
.\run_python.cmd -m app.cli models test --id cheap                      # 真实调用一次验证连通
.\run_python.cmd -m app.cli thesis --topic "题目" --model cheap          # 只让这一次用指定档案
```

内置 11 个厂商预设（DeepSeek / 智谱 / 通义 / Kimi / 火山方舟 / 百川 / MiniMax /
OpenAI / 本地 Ollama / 自建 vLLM / 自定义），都走 OpenAI 兼容协议。
界面在 **模型** 页签。

配置存在项目根目录 `models.json`（与 `.env` 同级，含明文密钥）。
**没有 models.json 时 `.env` 的 `LLM_*` 就是全部配置——老配置一个字都不用改。**

成本台账现在按模型汇总，报告里写明**每个用途实际用了哪个模型**。

详见 **[多模型接入说明.md](多模型接入说明.md)**。

### 借鉴 Supervisor-Skills：把科研方法论落成可检查的规则

[Supervisor-Skills](https://github.com/HKUSTDial/Supervisor-Skills)（港科大广州 DIAL 实验室开源）
把博导的科研经验蒸馏成了 AI 技能。本项目按自己的核心纪律筛了一遍——
**提示词里提的每一条要求，都必须能被检查出来**——只取其中**可机械判定**的部分：

| 上游规则 | 落到本项目 | 能不能检查 |
|---|---|---|
| 技术类引言六段式（局限→挑战→模块→贡献成链） | 新增「引言（技术类）」蓝图，**按研究方法**分流（工科/实验类走六段式，人文社科仍走 CARS 三语步） | ✅ 蓝图进提示词 |
| 四条自洽性检查 | `check_limitation_challenge_chain` / `check_contributions_specific` / `check_contributions_map_sections` | ✅ 只判形式缺环，不假装能判语义 |
| 不编造量级（"不写用户没给的具体细节"） | **`check_number_grounding`**：正文里的数字必须能在本章证据里找到 | ✅ 本系统有证据台账，这条能真落地 |
| banned AI-tone vocabulary | `check_ai_tone_vocabulary`：中英双语宣传腔词表，按密度判定 | ✅ |
| 审稿意见书的三条诚信规则 | `app/review.py` + `review` 命令：五维度意见书，**评分与提交建议由数量机械推导** | ✅ 由代码强制，不让模型打印象分 |

用法：

```powershell
.\run_python.cmd -m app.cli review -p <项目id> --open-only -o 审稿意见书.md
```

界面在「问题」页签右上角。评分有致命问题时硬性封顶 6.0，结论必定是"须修改后再提交"——
**模型给自己刚生成的稿子打分会系统性偏高**，所以这个判断做成了算式。

**没取的部分也写清楚了**（idea 评估、中译英、科研作图、答辩准备……以及为什么没取）。
详见 **[Supervisor-Skills落地说明.md](Supervisor-Skills落地说明.md)**（含许可与归属）。

### 从任意题目生成整篇论文初稿

**双击 `启动论文助手.cmd`** 即可：服务在后台起来，界面以独立窗口打开（应用模式，没有地址栏）。
界面 **写作** 页签里填题目与写作要求就能开写；**知识库** 页签可以查库、试查、投料。

```powershell
# 先预检：这个题目在本地知识库里有没有材料？（纯检索，不花钱）
.\run_python.cmd -m app.cli kb check --topic "你的论文题目"

# 一键生成（开题报告可选；不给就用所选论文类型的默认章节结构）
.\run_python.cmd -m app.cli thesis --proposal 开题报告.docx --words 35000
.\run_python.cmd -m app.cli thesis --topic "你的论文题目" --genre engineering --words 25000

# 只写某几章 / 跳过某几章
.\run_python.cmd -m app.cli thesis --proposal 开题.docx --chapter 研究设计 --skip-chapter 结论
```

产出一份排版可用的 `.docx` 初稿与一份生成报告。链路是：

```
知识库预检 → 开题解析/类型骨架 → 建项目 → 写作规划 → 逐章起草 → 结论汇总 → 参考文献表 → 验证 → 导出
```

**它不挑学科。** 七种论文类型各有自己的章节骨架与检查口径（实证研究型 / 案例研究型 /
理论思辨型 / 设计开发型 / 实验研究型 / 调查研究型 / 文献综述型），
作者给的开题章节树永远优先。

**知识库里没有的选题，它不会硬写。** 检索永远返回 top-k，所以不加闸门的话，
换个题目照样能"写出"一篇引用着一堆无关文献的论文。现在会先判相关性，
不相关就明确说"写不了，请先补资料"。

详见 **[通用写作说明.md](通用写作说明.md)** 与 **[一键生成说明.md](一键生成说明.md)**。

### 按你的要求改写已有的章节或段落

```powershell
# 改一整章
.\run_python.cmd -m app.cli rewrite -p <项目id> --section "第3章" `
    --instruction "把这一节的机制分析补实，用检索到的文献支撑"

# 改一段（从稿件里逐字复制要改的原文）
.\run_python.cmd -m app.cli rewrite -p <项目id> --find "本文以A股上市公司为样本" `
    --instruction "这一段没有依据，改写成有出处的表述"
```

界面上在「稿件」页签底部。改写同样**只依据证据**、**产生新版本可回退**、
**段落级整段替换**（不拼接新旧行文）。

### 写作助手（能力③）带一套可检验的写作规范

它不是"提示词写得比较长"：内置公认的写作规范框架（引言用 **CARS 三语步**、
文献综述按**研究脉络**组织而非逐篇罗列、正文段落用**主题句—证据—分析**结构、
研究设计写到**可复现粒度**、结果与讨论分开、结论**不引入新证据**），
配 **13 个写作检查器**（写作质量那 13 条）实测，并从 RAG 知识库取同领域真实段落作示例。
**提示词里提的每一条要求，都能被检查出来**——只提要求不检查，等于给模型一个愿望。

其中最要紧的一条：**禁止「一篇论文半句话」**。不得出现
「A（年份）做了…。B（年份）做了…。」这种逐篇罗列；同一主题的多篇文献
必须**合并成一个判断**，先给结论或分歧再把文献并列作证。

### 一章不是一个写作单元

一份硕士论文的核心章常在 6000～12000 字，单次生成写不好这么长。所以按**作者开题里的
小节**把一章切成一串写作单元（每个 1200～2500 字），各自检索最相关的证据、各自生成，
再拼回本章。拆分**不会编造新的小节标题**——目录是评审对照开题检查的第一样东西。

### 引用编号全篇唯一，参考文献表自动生成

原先按章检索、每章从 `[1]` 开始编号，整篇合并后绪论的 `[1]` 与综述的 `[1]` 会指向
**不同文献**——一份学位论文只有一张参考文献表，这是实打实的引用错误。
现在编号从项目级证据池分配（同一段原文跨章复用同一个编号），定稿前按首次出现顺序
连续化，并据此生成文献表。**只列真正被引用的条目**，检索到但没用上的单独报给你。

详见 **[写作助手升级说明.md](写作助手升级说明.md)**。

---

## 二、快速开始

```powershell
cd E:\AI\thesis-agents

# 0. 想当桌面程序用：双击这个（服务后台起，界面以应用窗口打开）
.\启动论文助手.cmd

# 1. 检查依赖环境（本项目不安装任何依赖，复用邻居项目的 libs\）
.\run_python.cmd -m app.cli health

# 2. 看有哪些能力已装载
.\run_python.cmd -m app.cli agents

# 3. 看本地知识库里有什么（写作时检索的就是它）
.\run_python.cmd -m app.cli kb status
.\run_python.cmd -m app.cli kb check --topic "你的论文题目"   # 预检：这个题目写得了吗

# 4. 从任意题目 / 开题报告生成论文初稿
.\run_python.cmd -m app.cli thesis --topic "你的论文题目" --genre empirical --words 30000
.\run_python.cmd -m app.cli thesis --proposal 开题报告.docx --words 35000

# 5. 启动服务（不开桌面客户端时用这个）
.\run.cmd
# 浏览器打开 http://127.0.0.1:8780
```

`health` 会告诉你两个对端服务是否在跑。**它们没启动不影响本系统启动**，
只是对应能力不可用，而且界面会明确标出是哪一个。

### 依赖关系

本系统**不重写**已有的两个项目，而是调用它们：

| 对端服务 | 端口 | 提供的核心能力 | 启动方式 |
|---|---|---|---|
| `thesis-assistant` | 8760 | 论文 RAG 检索、章节级引用、跨文档对比 | `E:\AI\thesis-assistant\run.cmd` |
| `aigc-reducer` | 8770 | AI 味指数、改写管线、保真护栏 | `E:\AI\aigc-reducer\run.cmd` |

Python 依赖直接复用它们的 `libs\`（`run_python.cmd` 已设好 PYTHONPATH），
**一个新包都不用装**。

### 模型配置

留空 `LLM_API_KEY` 时，系统会**复用 `E:\AI\thesis-assistant\.env` 里已配好的 key**
（不复制，只是读取，避免两处密钥逐渐不一致）。想用自己的，写在
`E:\AI\thesis-agents\.env` 即可，本项目优先。

界面「设置」里可以改，保存即生效，**不需要重启**。

---

## 三、一次典型使用

### 从开题报告生成新论文（推荐入口）

```powershell
# 先解析预览：系统把开题读成了什么？章节树对不对？（纯规则，不花钱）
.\run_python.cmd -m app.cli propose 开题报告.docx

# 一键生成：初稿 .docx + 生成报告 .md
.\run_python.cmd -m app.cli thesis --proposal 开题报告.docx --author 你的姓名 --words 35000

# 只跑写作部分（规划 → 起草 → 结论 → 文献表）
.\run_python.cmd -m app.cli thesis --proposal 开题报告.docx -s draft
```

### 修改一份已有的稿子

```powershell
# 建立项目（.docx / .txt / .md）
.\run_python.cmd -m app.cli new --file 我的论文.docx --title "数字化转型与企业韧性"

# 先只跑验证：不花钱、不碰稿件，看清有多少问题
.\run_python.cmd -m app.cli run -p <项目id> -s verify --no-llm-verify

# 看问题清单
.\run_python.cmd -m app.cli issues -p <项目id>

# 跑自我更正（会改稿，每轮都产生新版本，可回退）
.\run_python.cmd -m app.cli run -p <项目id> -s fix

# 导出
.\run_python.cmd -m app.cli export -p <项目id>
```

或者全部在界面里做：`http://127.0.0.1:8780`。

**建议顺序：先 `verify` 看清问题，再决定要不要让工具动手。** 验证阶段零成本、
零风险，而它能告诉你最要紧的那件事——你有没有引用一篇不存在的文献。

---

## 四、架构：为什么这样分层

```
app/framework/   通用多智能体内核。不认识"论文"，只认识 Artifact / Issue / Patch
app/agents/      写作、获取、核验、修复的智能体，各自声明能力
app/writing/     写作方法论（蓝图 + 要求）与引用编号基础设施
                 citations.py 证据池、角标重排、参考文献表
app/checks/      45 个静态检查器（纯函数、离线、不调用模型），其中 13 条查写作质量
app/format/      格式规则引擎（YAML 驱动，等学校模板）
app/parsing/     .docx / .txt / .md 收稿；proposal.py 解析开题报告
app/export/      Markdown 稿件 → 规范排版的 .docx（含导出后自检）
app/thesis.py    一键编排：开题 → 规划 → 起草 → 结论 → 文献表 → 验证 → 导出
app/adapters/    对既有服务的 HTTP 客户端，不复制它们的代码
app/pipeline.py  编排：把能力串成可监督、可单阶段运行的流水线
```

### 4.0 阶段到智能体是一份显式清单，不是隐式约定

`app/pipeline.py` 的 `STAGE_AGENTS` 写明"每个阶段由哪些智能体按什么顺序执行"。
`draft` 阶段是四个：`writer:plan → writer:draft → writer:conclude → writer:references`，
它们之间是硬依赖，少跑一个就出问题。

这条设计是被一个真实故障逼出来的：**`writer:plan` 写好了、单测全绿，
但全项目没有任何地方调用它**——`draft` 阶段原先只挂 `writer:draft`，
于是 README 里写的 `run -s draft` 必然失败，写作能力从命令行完全走不通。
每个智能体的单元测试都直接调自己，谁也没发现。

> **每个部件都测过，不等于它们被接起来了。**

所以 `tools/test_thesis.py` 既断言这份清单的内容与顺序，也真的跑一次完整链路。

### 4.1 检查器与修复器必须分离

自我更正循环的价值**不在循环本身，而在这条纪律**：修复器不能宣布自己成功，
只有检查器能。

具体机制：每条 `Issue` 必须携带 `evidence`（从稿件里**逐字截取**的原文片段）。
改完之后，框架拿这个片段回原文里找——**还在，问题就还是 open**，不管修复器怎么说。
这条设计让"我改好了"变成一个可证伪的陈述。

### 4.2 修复不等于"让模型重写"

模型**不能直接改文本**，它只能申请调用注册过的算子（`app/framework/ops.py`）：

| 算子 | 用途 |
|---|---|
| `replace_span` / `apply_edits` | 精确替换（要求定位唯一） |
| `renumber_sequence` | 编号重排（可限定作用范围） |
| `normalize_punctuation` | 标点规范化（**不碰英文语境**） |
| `strip_filler` | 删模板腔套话 |
| `unify_term` / `insert_after` / `delete_span` | 术语统一、补插、删除 |

每个算子都要求**定位唯一**，定位不唯一时**直接拒绝执行**而不是猜。补丁在交付前
还要在本地副本上**干跑一遍**（`FixerAgent._dry_run`），保证"判为可修复"与
"真的能修"是同一件事。

而且——**能确定性修复的问题不问模型**。标点、套话、编号、连续重复段落都有唯一
正确答案，交给模型只会引入不确定性还白花钱。只有"改成什么"需要理解的才走模型。

### 4.3 稿件是不可变的版本链

每次修改产生新的 `Artifact` 版本，`parent` 串成链。所以：

- 任何时候都能回退对照（界面「版本」页签有逐段 diff）
- 出问题时能回答"哪一轮、谁、基于什么依据改的"
- 多个智能体不会并发写同一份稿子（黑板假定同一时刻只有一个写入者）

### 4.4 消息总线全部落盘

智能体之间不直接调用，而是往总线上发消息。整条流程的消息按时间写进
`data/projects/<id>/messages.jsonl`。这不是为了"架构好看"，是为了能回答
**"第几轮、哪个 agent、基于什么输入做出这个判断"**。

---

## 五、自我更正的诚实性

循环的收敛判据写得很直白（`app/framework/state.py`）：

```python
converged = not blockers and not majors   # 仅看仍 open 且可修的
```

到轮次上限仍有致命/重要问题，它会**返回 `converged=False` 并说明卡在哪一类**。
**不收敛不等于失败**——那通常意味着剩下的问题需要你的判断。

一个真实的运行结果（真实 DeepSeek，占位测试稿）：

```
流程结束：1.1s
  ✅ fix   1 轮，收敛=True，仍有 0 条待处理
自我更正：1 轮，收敛=True，剩余致命 0 · 重要 0 · 待人工 6
模型调用：1 次，失败 0，耗时 1.07s
问题统计：致命 0 · 重要 0 · 轻微 0 · 提示 0 · 已修复 2 · 待人工 6

稿件版本链：
  v1  74 字  by intake   初始稿件
  v2  69 字  by fixer    第 1 轮修复：确定性修复：style.filler_phrase
  v3  68 字  by fixer    第 1 轮修复：删除"章"与"绪"之间的多余空格
```

注意那 6 条 `待人工`——它们是"缺摘要章节""参考文献未被引用"这类**必须你自己处理**
的问题。工具把它们**列出来**，而不是编两段摘要塞进去。

---

## 六、外部核验：唯一能抓出编造文献的办法

这是能力⑥里最要紧的部分。AI 编造的参考文献格式看起来完全正常，**人工自查几乎
不可能发现**。所以这个模块把每条参考文献拿去 Crossref 与 OpenAlex 核实，并区分
四种结论：

| 结论 | 含义 | 严重度 |
|---|---|---|
| `confirmed` | 真实存在且匹配 | 不报（这是正确情况） |
| `weak_match` | 找到相似文献但匹配度不够 | MAJOR，需人工确认 |
| `not_found` | 所有源都查不到 | **BLOCKER**（疑似编造） |
| `inconclusive` | 查询本身失败（断网/限流） | INFO，**绝不当成"不存在"** |

### 两条关键的标定

**① 相关性下限 `MIN_RELEVANCE = 0.55`。** 实测数据：编造标题的最佳匹配落在
0.27~0.44，真实英文标题 0.93~1.00，真实中文标题 Crossref 0.65 / OpenAlex 0.10。
不加这道下限，"返回了结果"就会被当成"找到了"——编造的文献会蒙混过关。
取 0.55 是为了把"编造"与"中文文献收录差"分开；**不能取到 0.8 以上**，否则真实
中文文献会被误报成编造，那种误报会直接毁掉你对工具的信任。

**② 中文文献收录有限，查不到不等于造假。** 判为 `not_found` 且标题含中文时，
消息里会自动附上提醒，建议再用知网/万方核实。**外部核验是可关闭的**
（`VERIFY_EXTERNAL=false`），关掉后完全离线，但这条最致命的检查也就没有了。

可以直接在界面「工具」页签粘贴参考文献列表试一次，不需要先建项目。

---

## 七、已知限制（请先读这一节）

**这套系统不能替你做的事：**

1. **不能判断你的结论是否正确。** 结论章会依据正文汇总成结构草稿，并逐条标出依据章节，
   但"这个结论成不成立"是作者的学术判断，系统不改也不替你签。
2. **不能判断创新点是否足够，也完全不代写创新点。** 创新点是评审的判断，也是作者的
   学术主张。开题报告里声明的创新点会被提取出来供你对照，但绝不进入生成。
3. **不能替代查重。** 它不查知网/维普，也不做 AIGC 检测（对本地的 AI 味指数，
   见下面第 5 条）。
4. **不能核实中文文献的完整性。** Crossref/OpenAlex 对中文期刊覆盖有限。
5. **AI 味指数不是商用检测器的分数。** 它是 `aigc-reducer` 的本地风格启发式，
   与知网/维普/PaperPass 的数值**不可互相换算**，也无法承诺任何检测器的结果。
   它优化的是已被公开研究验证与检测器输出相关的风格信号（句长突发度、模板腔密度等）。
6. **扫描版 PDF 无法入库。** 入库时会给出警告，需要先用 MinerU 做 OCR。
7. **`.caj` / `.nh`（知网格式）不支持**，需先转 PDF。
8. **格式整理能报也能改属性，但有两处明确不碰**：表格与页眉页脚（通常要五号/单倍，
   比正文小一号，统一按正文改反而错，报告里会写明跳过多少段）；
   以及文本层的格式问题（半角标点、中英文之间缺空格、参考文献编号风格）——
   那些归文本算子。**标题要求段落带大纲级别或标题样式**：如果标题是"加粗+手工居中"
   的普通段落且连 `w:outlineLvl` 都没写，工具无法把它和正文区分开，会当正文处理
   （报告里的"识别出 N 个标题"就是这个数，可以先核对再决定）。
9. **降 AIGC 依赖对端服务。** 它没启动时该阶段明确失败，不会静默跳过。
10. **导出的 .docx 用的是通行排版惯例，不是你们学校的模板。** 宋体正文／黑体标题／
    1.5 倍行距／A4 只是一个合理的默认值，交稿前要按学校模板微调。
11. **参考文献表不补全字段。** RAG 元数据里没有期刊名、卷期、`[J]`/`[M]`/`[D]` 标识，
    系统就空着并逐条列出缺什么——补全等于伪造书目信息。
12. **`【待作者补充：…】` 必须处理。** 那是模型明确说"这里我没有依据"的地方。
    在 Word 里它们是红色加粗的，逐一补上真实依据或删掉该句。

**关于学术诚信**：本系统会尽力守住"数字、引用、引文、术语不被改坏"这类可机械
校验的底线，也会在保真校验不通过时**退回原文**。但改写后的立场是否仍是你的、
语气是否合适，需要你自己通读。**涉及学术诚信与署名的判断，由署名人负责。**

---

## 八、验证这套系统本身

```powershell
.\run_python.cmd tools\run_all_tests.py                # 一次跑完下面全部（13 个文件）
.\run_python.cmd tools\smoke_framework.py              # 框架内核：算子安全性、版本链、复验、总线、闭环
.\run_python.cmd tools\smoke_adapters.py               # 适配器：对端服务健康检查
.\run_python.cmd tools\smoke_adapters.py --external    # 加真实网络核验（含"识破编造文献"）
.\run_python.cmd tools\test_fixer.py                   # 修复器与编排（用假模型，断网可跑）
.\run_python.cmd tools\test_checks.py                  # 静态检查器，含正例与反例
.\run_python.cmd tools\test_format.py                  # 格式解析与规则引擎
.\run_python.cmd tools\test_writeback.py               # 排版写回（三条硬保证 + 手工排版标题，67 项）
.\run_python.cmd tools\test_format_api.py              # 排版整理的 HTTP 端点（16 项）
.\run_python.cmd tools\test_writer.py                  # 写作助手（假模型 + 假知识库，97 项）
.\run_python.cmd tools\test_external.py                # 外部核验（stub 注入 + 断点续验）
.\run_python.cmd tools\test_writing_quality.py         # 写作检查器（含好坏写法对照）
.\run_python.cmd tools\test_writer_integration.py      # 写作闭环：蓝图→起草→自查
.\run_python.cmd tools\test_thesis.py                  # 一键生成链路（开题→起草→结论→文献表→.docx，89 项）
.\run_python.cmd tools\test_generic.py                 # 通用性（论文类型/相关性闸门/章节选择/改写定位，52 项）
.\run_python.cmd tools\test_models.py                  # 多模型接入（档案往返/按用途路由/密钥不外泄，63 项）
.\run_python.cmd tools\test_supervisor.py              # Supervisor-Skills 方法论落地（好坏成对断言，64 项）

.\run_python.cmd tools\realcheck.py                    # 在真实论文上评估写作检查器（看误报）
.\run_python.cmd tools\demo.py                         # 端到端演示（隔离目录，不污染真实项目）
.\run_python.cmd tools\inspect_data.py [--clean]       # 查看/清理数据目录
```

### 测试分两类，缺一不可

* `test_writer.py` 测**各个智能体各自的纪律**：用假件精确构造"模型编造引用""知识库检索不到
  材料""保真校验不通过"这些真实服务上不容易稳定复现的分支。
* `test_thesis.py` 测**它们有没有被接起来、顺序对不对**：跑一次真流水线（假模型 + 假 RAG），
  断言产出的稿件里有全部章节、有结论、有连续编号的文献表、有一个能重新打开的 .docx。

这两类测的东西不同。只做前一类，就会漏掉"`writer:plan` 从未被调用"这种故障。

### 测试必须在**进入时**清理，不能只靠退出时清理

测试会**真的建立项目**。数据目录隔离由 `tools/_testtmp.py` 负责（把
`DSH_THESIS_DATA_DIR` 指到 `data\_test\`）。

这条经验是真实故障换来的：**`atexit` 在被 kill 时不会执行**，而开发期进程经常
被中断。残留于是不断堆积——实测一次发现真实 `data\projects\` 里躺了
**227 个测试项目**，界面上的项目列表直接不可用，还得人工清理。所以规则是
**进入时也清**（`_testtmp.cleanup_litter()`），`atexit` 只是第二道保险。

另一个更隐蔽的坑：**环境变量只对 `app.config` 生效**。测试里写
`ROOT / "data" / "projects"` 这种硬编码路径会**绕过重定向**，照样污染真实目录
（一个测试每次运行往真实目录加 17 个项目，就是这么来的）。
`tools/_testtmp.py` 因此额外导出 `DATA_DIR`，测试一律用它。

验证隔离是否生效——每个测试跑完后 `projects` 数量都应**不变**：

```powershell
.\run_python.cmd tools\inspect_data.py
```

> 隔离目录建在**工作区内**（`data\_test\`）而非 `%TEMP%`。受限沙箱只允许写工作区，
> `tempfile.mkdtemp()` 会以 `PermissionError [WinError 5]` 失败。

---

## 九、目录结构

```
thesis-agents\
├── run.cmd / run.ps1            启动服务（8780）
├── run_python.cmd / .ps1        统一 Python 入口（设 PYTHONPATH + UTF-8）
├── app\
├── config.py                .env 解析、密钥回退、数据目录重定向
│   ├── llm.py                   OpenAI 兼容客户端 + 容错 JSON 解析
│   ├── runtime.py               装配：注册检查器与智能体，失败逐个记账
│   ├── pipeline.py              编排八阶段（STAGE_AGENTS 显式列出每阶段的智能体顺序）
│   ├── thesis.py                **一键生成**：开题 → 规划 → 起草 → 结论 → 文献表 → 验证 → 导出
│   ├── server.py                FastAPI + SSE
│   ├── cli.py                   命令行
│   ├── framework\               多智能体内核（不认识"论文"）
│   │   ├── artifact.py          Artifact / Issue / Patch / Severity / Stage
│   │   ├── agent.py             Agent 基类、Ledger（成本台账）、注册表
│   │   ├── bus.py               消息总线 + JSONL 日志
│   │   ├── blackboard.py        项目状态、版本链、问题去重与复验
│   │   ├── ops.py               9 个修复算子（纯函数、定位唯一、幂等）
│   │   ├── registry.py          @tool / @verifier / @fixer 注册表
│   │   └── state.py             状态机 + 自我更正循环
│   ├── agents\                  七个能力的智能体
│   │   ├── writer_plan.py       写作规划（按章节职能分配字数、保留作者小节）
│   │   ├── writer_draft.py      分单元起草（全篇唯一角标、跨单元去重、致命错误熔断）
│   │   ├── writer_conclude.py   结论汇总（只依据正文、越界数字就地标注）
│   │   ├── writer_references.py 角标连续化 + 生成参考文献表
│   │   └── writer_polish.py     局部润色建议（需过保真校验）
│   ├── writing\                 写作方法论与引用编号基础设施
│   │   ├── __init__.py          6 类章节蓝图 + 可检验要求 + 篇幅占比
│   │   ├── citations.py         证据池、角标解析与重排、参考文献表
│   │   └── examples.py          RAG 示例检索（同领域真实段落，禁止照抄）
│   ├── checks\                  45 个静态检查器（13 条查写作质量）
│   ├── format\                  格式规则引擎（YAML）
│   ├── parsing\                 .docx / .txt / .md 收稿
│   │   └── proposal.py          开题报告解析（栏目、章节树、研究方法、创新点备忘）
│   ├── export\                  Markdown 稿件 → 规范排版 .docx（导出后必自检）
│   ├── adapters\                对端服务客户端
│   └── static\index.html        单文件界面（零外部依赖）
├── _boot\sitecustomize.py       沙箱/Windows 启动补丁
├── tools\                       测试与诊断脚本
├── inbox\                       资料投放目录
└── data\                        项目、消息日志、版本
```

---

## 十、命令行速查

| 命令 | 说明 |
|---|---|
| `health` | 检查对端服务与模型配置 |
| `agents` | 列出智能体、检查器、修复算子；显示未装载的能力 |
| `kb status` | 知识库概况（馆藏、收料目录、服务是否可达） |
| `kb list` | 列出知识库里的文献 |
| `kb search "查询"` | 试查（默认不重排，不花钱） |
| `kb check --topic "题目"` | **按题目预检**：这个选题在库里写得了吗（不花钱） |
| `kb add 文件...` / `--dir 目录` | 投料并触发入库 |
| `kb ingest` | 重新触发入库 |
| `models list` | 列出模型档案与按用途分配 |
| `models presets` | 列出内置厂商预设 |
| `models add --id X --preset deepseek --key sk-xxx [--use]` | 新增模型档案 |
| `models role --set draft=strong --set verify=cheap` | 按用途分配模型（留空=回到当前档案） |
| `models use X` / `models remove X` | 切换当前档案 / 删除 |
| `models test --id X` | 真实调用一次验证连通性 |
| `models test --all` | 逐个测试全部档案 |
| `propose X.docx` | **解析开题报告**，显示栏目/章节树/研究方法/创新点（纯规则，不花钱） |
| `thesis --proposal X.docx` | **一键生成**论文初稿（.docx + 生成报告） |
| `thesis --topic "题目" --genre engineering` | 不用开题报告，按题目 + 论文类型生成 |
| `thesis --model cheap` | 只让这一次生成用指定模型档案 |
| `thesis --chapter 研究设计` / `--skip-chapter 结论` | 只写某几章 / 跳过某几章 |
| `rewrite -p <id> --section 第3章 --instruction "…"` | **按要求改写**整章 |
| `rewrite -p <id> --find "原文" --instruction "…"` | **按要求改写**一段 |
| `new --file X.docx` | 建立项目（已有稿子要改时用） |
| `projects` / `status -p <id>` | 列出项目 / 查看项目状态 |
| `issues -p <id> [--open-only] [--fixable]` | 问题清单 |
| `run -p <id> [-s 阶段...] [--dry-run] [--no-llm-verify]` | 跑流水线（`-s draft` = 规划→起草→结论→文献表） |
| `run-agent -p <id> <智能体>` | 只跑一个智能体（如重跑 `writer:references`，零模型成本）；跑会用模型的要加 `--allow-model` |
| `review -p <id>` | 生成模拟审稿意见书（五个维度 + 评分 + 提交建议） |
| `versions -p <id>` | 稿件版本链 |
| `export -p <id> [-o X.docx] [--kind draft\|issues\|version]` | 导出；`-o` 给 `.docx` 后缀时导出规范排版的 Word |
| `search "查询"` / `ask "问题"` | 检索知识库 / 基于知识库问答 |
| `scan` / `peers` | 扫描本地资料目录 / 列出知识库里的论文 |

所有命令都支持 `--json`（放在子命令之前）。

---

## 十一、下一步

**格式整理（⑤）：报偏差与写回属性都已实现。**

* 读 `.docx` 的真实排版属性（中文字体取自 `w:eastAsia`、字号走样式继承链、
  主题字体解析、页边距/行距/缩进/标题层级/对齐），按规范逐条报出偏差，
  并**明确标注哪些要改 OOXML 属性、哪些改文字就行**。
* **把排版属性写回你已有的 .docx**（`docx-format` 命令 / 界面「工具 → 论文排版整理」），
  目标值来自同一份 `rules_default.yaml`，所以"报偏差"和"改偏差"用的是同一个尺子。
  三条硬保证：**文字一字未改**（逐字比对段落+表格+页眉页脚）、改完能重新打开、
  失败自动还原备份。详见 [`排版整理说明.md`](排版整理说明.md)。
* 标题按 **`w:outlineLvl` → 样式名 → 编号定义** 三级判定（和收稿阶段同一套），
  所以手工排版的标题（样式名是 `Normal`、靠大纲级别表明层级）也认得出来。
  真实开题报告实测：**117 条偏差 → 69 条，能改的那 44 条全部归零**，
  文本型（标点/空格/编号）一条不变。
* 还没做的：
  * **表格与页眉页脚不写回**（它们通常要五号/单倍，比正文小一号，统一按正文改反而错）。
    报告里会写明跳过了多少段。
  * **改不了文本层的格式问题**（半角标点、中英文之间缺空格、参考文献编号风格）——
    那些归文本算子，本功能一律不碰。
  * **接入你的学校模板**：把学校官方 `.docx` 模板交给我之后，需要的是一个
    **模板推断器**（读模板 → 产出 RuleSet），因为让你手写 YAML 不现实。
    目前 `app/format/rules.py` 里预留了 `infer_ruleset_from_template()` 入口
    （抛 `NotImplementedError`）。**临时方案已经够用**：`docx-format --like 模板.docx`
    可以直接照模板的样式与页面设置改（见 `scheme_from_template`）。
  > 注意与导出层（`app/export/`）区分：导出是**新建**一份 .docx（写坏了重跑一次即可），
  > 格式写回是**改你已有的** .docx（就地覆盖会先留 `.bak`；输出到新文件则不动原文件）。

**一键生成（能力③）已经打通，下一步可以做的：**

* **用开题里的参考文献反过来指导检索**：开题报告的 143 条参考文献目前只用于外部核验，
  还可以拿来在知识库里做定向检索（"这几篇文献的原文，知识库里有没有"）。
* **跨章论点一致性检查**：目前检查的是"结论有没有超出正文"，还没检查
  "第 3 章说 A 上升，第 5 章说 A 下降"这类**正文内部的相互矛盾**。
  这是纯文本层面可做的（同一指标 + 相反方向词），值得做，但需要先定个不误报的口径。
* **章节篇幅偏差提示**：目标字数与实际字数的偏差已经记在生成报告里，
  可以进一步做成检查器（某章只有目标的 30% 通常意味着材料不够或写不下去）。

---

## 十二、给它换一个选题

框架层不含任何论文领域知识。要用在别的领域（技术报告、专利、项目申报书），
需要换的是：

1. `app/checks/` 里的检查器（领域规则）
2. `app/format/rules_default.yaml`（格式规范）
3. 各智能体的提示词
4. `app/config.py` 里的对端服务地址

框架（版本链、复验机制、算子约束、消息总线）不需要改。

