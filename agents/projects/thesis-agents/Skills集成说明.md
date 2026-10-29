<!--
  本文件是从本机项目 thesis-agents 的 Skills集成说明.md **原样转载**的。
  转载目的：把散落在各项目的说明集中到一处索引，便于查阅。
  正文未经修改；如果这里的内容与该项目目录里的 Skills集成说明.md 不一致，
  以项目目录里的为准（那份是随代码演进的）。

  来源：E:\AI\thesis-agents\Skills集成说明.md
  收录时间：2026-10-29
-->

> **本文档为转载**，来源 `E:\AI\thesis-agents\Skills集成说明.md`（论文多智能体框架 · 13 个智能体 · 约 4.5 万行 · 该项目的说明文档）。正文未作修改。

---

# Skills 集成说明（本机技能包是怎么变成能力的）

> 一句话：**技能原文留在原地不动，项目自己写一份提炼表；能进提示词的进提示词，
> 能机检的变成检查器，都不适合的明确写出来——装了多少、能帮到哪一步，一眼可查。**

对应命令：`python -m app.cli skills list`；提炼表在 `app/skills_registry.yaml`。

---

## 一、内置了什么：17 个技能包 + 18 个手册章节 = 35 个资源

技能根目录由 `app/skills.py` 的 `discover()` 扫描，登记表决定"哪些进了提示词"。
当前本机（`E:\AI\.dsh\skills` 与 `E:\AI\Supervisor-Skills`）：

### 技能包（17）

来自 **Supervisor-Skills**（CC-BY-4.0 / CC-BY-NC-SA-4.0，12 个）：

| 技能 id | 中文名 | 绑定阶段 | 已实现检查器 |
|---|---|---|---|
| `idea-evaluator` | 选题体检 | proposal | 无（落成**工具**：`idea --topic`） |
| `tech-paper-template` | 逻辑骨架 | proposal | 5 |
| `benchmark-paper-template` | 基准/评测类论文模板 | proposal | 2 |
| `deep-research` | 综述级文献调研 | proposal | 4 |
| `paper-writer` | 正文写作 | draft, final | 7 |
| `intro-drafter` | 引言六段式 | draft | 6 |
| `figure-designer` | 核心图设计 | draft, final | 3 |
| `pre-submission-reviewer` | 投稿前五维预审 | final | 4 |
| `paper-polish` | 语言打磨 | final | 2 |
| `rebuttal-guidance` | 返修策略 | final | 无（落成**工具**：`rebuttal`／返修指引） |
| `vibe-research-workflow` | AI 辅助研究工作方式 | proposal, draft, final | 1 |
| `drawio-reconstruction` | 图片转 Draw.io | draft, final | 0（**只提示，不代画**） |

来自 **academic-research-skills**（CC BY-NC 4.0，52k star，5 个，用 `skills install` 装的）：

| 技能 id | 中文名 | 绑定阶段 | 已实现检查器 |
|---|---|---|---|
| `academic-paper` | 论文写作流水线（12-agent / 11 模式） | draft, final | 7 |
| `academic-paper-reviewer` | 多视角模拟评审（5 席） | final | 4 |
| `academic-pipeline` | 全流程编排与完整性核查（10 阶段） | final | 4 |
| `sr-screener` | 文献筛选（纳入排除协议） | proposal | 2 |
| `ars-deep-research` | 通用深度调研（13-agent / 8 模式） | proposal | 5 |

> `ars-deep-research` 是**改名后**的：它原名也叫 `deep-research`，与本机上已有的那个
> 同名但内容不同（一个是综述级调研，一个是 13-agent 流水线）。安装器第一次**静默覆盖**了
> 原有的那个，已修（默认不覆盖），并把新的改名共存。详见第三节末尾。

"已实现检查器"一列里有三种情况，都算**已落实**，区别只在于代码落在哪：

* Supervisor-Skills 那批的 14 个在 `app/checks/skills_rules.py`；
* academic-research-skills 那批的 11 个在 `app/checks/skills_rules_ars.py`
  （**单独一个文件**，让"这条规矩出自哪个上游、许可是什么"一眼可见）；
* `check_overclaim`、`check_number_grounding`、`check_ai_tone_vocabulary`、
  `check_lit_synthesis` 一类**是早先按技能规则手写的**（在 `app/checks/writing_quality.py`），
  现在只是第一次在 `checkers` 字段里**声明了来源**，于是"这条报错从哪来"也能顺着查了。

### 手册章节（18）

`handbook/**/*.md`，**方法论，不进提示词**（`directives: []`），供阅读与出处核对。
按登记表里的阶段分：开题 7 章（如何评价论文质量、Idea 生命周期、更高更快更强、
进阶颠覆式创新、技术类思考模板、Benchmark 思考模板、全流程任务清单）、
初稿 2 章（Introduction 思考模型、三篇顶会案例剖析）、终稿 1 章（写作细节与 Checklist）、
跨阶段 8 章（绘图四章、Vibe Research 两章、案例剖析两章）。
全部用 `skills show <id>` 打开原文。

---

## 二、三种"内置"方式，以及各自什么时候生效

这是整套集成的核心：**同一个技能包可以同时以三种身份进入系统，三种身份的生效时机不同。**

| 落地方式 | 载体 | 什么时候生效 | 生效范围 |
|---|---|---|---|
| ① 进 LLM 提示词 | `prompt_block(phase)` | 规划／起草／结论／润色／改写 | 只注入**当前写作阶段**绑定的技能指令 |
| ② 变成检查器 | `checkers` 字段 → `app/checks` | 跑流水线时自动执行（verify 阶段） | 纯本地、零模型成本 |
| ③ 变成工具 | 选题体检／骨架自洽／预审报告／返修指引 | 你显式调用的那一刻 | 大多零成本，选题体检与骨架需要模型 |

### ① 进提示词：`prompt_block`

阶段从 `board.project.meta["writing_phase"]` 读（由 `app/thesis.py` 与
`app/finalize.py` 在建项目／开终稿时写进去），读不到按初稿处理——历史项目没有这个字段，
不能因此让它们写不出东西。默认上限 **2200 字符**；超出时**截断并明说截断了**
（"技能指令超长，本次未注入：……"），因为"提示词被悄悄削掉一截"是那种
看起来生效、其实少了一半要求的坏法。

失败一律降级为空串：技能目录被删、YAML 写坏、路径不可达，都不该让写论文停摆。

### ② 变成检查器

`app/checks/skills_rules.py` 的每条规则都对应登记表里的 `checkers` 字段，
报错里带 `detected_by="skills:<技能 id>"`。**单独放一个文件，是为了让"这条规则
从哪来"永远查得到**：看到一条报错 → `skills_registry.yaml` → 技能原文，三步核对完。

检查器全部是 `needs_llm=False, cost="local"`——不花钱、可反复跑、结果可复现。
上界类判定还会刻意收敛：条目提取依赖编号或分行形态，少提取一条就会误报"你只写了两条"，
所以 `check_limitations_count` / `check_challenges_count` **只报上界不报下界**。

### ③ 变成工具

| 工具 | 命令 | 来自 | 何时生效 |
|---|---|---|---|
| 选题体检 | `idea --topic` | `idea-evaluator` | 开题门槛之一；结论 `Reject and Pivot` 或校验不过时**非零退出**，便于脚本化"先体检再写作" |
| 逻辑骨架自洽 | `idea --what skeleton` | `tech-paper-template` | 开题门槛之一；四项自洽只要有 `fail` 就判断链 |
| 预审报告 | `final` / `review` | `pre-submission-reviewer` | 终稿阶段自动产出 |
| 返修指引 | `final --comments` / `rebuttal` | `rebuttal-guidance` | 只在给了审稿意见原文时产出 |

工具与检查器的分工也是刻意的：**选题、骨架这类判断做成"让模型按固定表格填 +
本地校验"，而不是自由文本**——字段名、条目数上限、取值域是硬约束，模型填偏了能被机器发现；
而"这个局限是不是真的"是语义判断，只作为给人看的问题列出，不假装能判。

**同一套规则在三种身份下是"同一份数据"。** 以 `tech-paper-template` 为例：

```yaml
# app/skills_registry.yaml —— 一处声明，三处使用
tech-paper-template:
  phases: [proposal]
  directives:                      # ① 进提示词（proposal 阶段的规划/起草都会看到）
    - 既有工作的局限最多三条；关键思路必须是一句话，不许写成一段。
    - 方法模块的数量要与挑战数一一对应，多出来的模块就是孤儿。
  checkable:                       # 供人读的口径
    - 关键思路为单句（≤2 句即告警）且不含营销词
  checkers:                        # ② 变成检查器（跑 verify 时执行）
    - check_key_idea_single_sentence
  not_builtin:                     # ③ 不做的事（诚实边界）
    - 局限是否真的被关键思路回应，属于语义判断，只能由模型或作者判断
```

`not_builtin` 的那一条，正好就是 `idea` 工具里**由模型填、不由代码判**的那一格——
同一个取舍在两个地方以不同形式体现，这是刻意的，不是遗漏。

---

## 三、为什么不把技能正文复制进本仓库

四条理由，任何一条单独成立都足够：

1. **许可**。两个上游都要求署名、都限制商用：
   * Supervisor-Skills：**CC-BY-4.0 / CC-BY-NC-SA-4.0**
   * academic-research-skills：**CC BY-NC 4.0**（© 2026 Cheng-I Wu）
   把正文复制进这个项目等于**再分发**，既踩许可，也会随上游更新立刻过期
   （上游改了，我们这份还是旧的）。
2. **真正要进提示词的只是短指令，不是几万字的规范全文。** 全文塞进去会
   **挤掉检索到的证据**——上下文被方法论占满，反而写得更差。
   academic-paper 的 SKILL.md 就有 65KB，它的 references 还有 29 个文件。
3. **提炼过的规则才能被检查。** 每条 `directives` 都对应 `checkable`：
   能实现的进 `checkers`，实现不了的进 `not_builtin` 并写明为什么。
   原文照抄做不到这一步。
4. **出处随时可查**。作者想读原文：`skills show <id>`，或直接打开 `path` 指向的文件；
   "你这条指令是从哪来的"永远有答案。

### 出处与署名

| 上游 | 许可 | 本机位置 | 装了哪些技能 |
|---|---|---|---|
| <https://github.com/HKUSTDial/Supervisor-Skills> | CC-BY-4.0 / CC-BY-NC-SA-4.0 | 技能在 `E:\AI\.dsh\skills`；克隆在 `E:\AI\Supervisor-Skills`（**handbook/ 在这里**） | 12 个技能 + 18 篇手册 |
| <https://github.com/Imbad0202/academic-research-skills>（52k star，© 2026 Cheng-I Wu） | **CC BY-NC 4.0** | `E:\AI\.dsh\skills`（`skills install` 装的） | academic-paper、academic-paper-reviewer、academic-pipeline、sr-screener、ars-deep-research |

* 本项目只引用技能名与规则要点，提炼表 `app/skills_registry.yaml` 是原创内容。
* 更早那批按技能写检查器的工作与取舍，见同目录的 `Supervisor-Skills落地说明.md`。

### 一个必须说明的冲突：两个 `deep-research`

两个上游**都有一个叫 `deep-research` 的技能**，但内容完全不同：

* Supervisor-Skills 的：综述级文献调研（问题冻结 → 多视角检索 → 逐篇核验 → MECE 分类）
* academic-research-skills 的：13-agent 流水线，8 种模式（含 PRISMA 系统综述与 meta 分析）

第一次安装时**后者把前者静默覆盖了**——这是安装器的一个真 bug，已修：
`skills install` 现在**默认不覆盖同名技能**（跳过并如实报出跳过了谁，
要覆盖得显式 `--force`，要共存用 `--skill-name` 换目录名）。

两个版本现在**共存**：原来的留在 `deep-research`（仍按原样绑定"开题"阶段），
新装的改名 `ars-deep-research`（绑定"开题"阶段作为补充）。
改名只动目录名，技能内部的相对路径（`agents/`、`references/`）不受影响。

---

## 四、技能是怎么被找到的

`skills_roots()` 按**优先级**返回实际存在的目录（先找到的先用），读不到就跳过：

| 顺序 | 目录 | 为什么在这 |
|---|---|---|
| 1 | `<项目>/skills` | 作者把技能放进项目，就能连项目一起带走 |
| 2 | `DSH_THESIS_SKILL_ROOTS` 里的每一项 | 环境变量覆盖，各人技能装在哪不一样（多个用 `os.pathsep` 分隔） |
| 3 | `<项目同级>/.dsh/skills` | 本机技能装在 `E:\AI\.dsh\skills`，项目在 `E:\AI\thesis-agents`；相对位置比写死绝对路径可移植 |
| 4 | `<项目同级>/Supervisor-Skills` | 上游克隆，**handbook/ 在这里** |
| 5 | `~/.dsh/skills` | 用户级安装位置 |
| 6 | `~/.claude/skills` | 复用已有的 Claude 技能目录 |

每个根目录下依次找四种形态：`skills/*/SKILL.md`、`*/SKILL.md`、根目录自己的
`SKILL.md`、以及 `handbook/**/*.md`（手册章节的 id 用**相对 handbook/ 的路径**，
与登记表的键一致——早先用 `handbook/<rel>` 当 id，会让同一章**同时**出现在
"装了但没登记"和"登记了但没装"两个清单里）。

两边都要如实报：登记了但没装（`missing`）、装了但没登记（`unregistered`）。
**只报一半会让作者以为"装了就等于内置了"。**

---

## 五、装新技能

```powershell
python -m app.cli skills list                    # 清单：哪些装了、哪些登记了、哪些没登记
python -m app.cli skills show paper-writer       # 看某条技能的原文（--full 看全文）
python -m app.cli skills install <目录或 owner/repo 或 GitHub URL>
python -m app.cli skills search 学术写作 skill    # 按 star 排序找现成的
```

**为什么 install 不用 git**：这台机器上 `git` 命令不可用（实测"无法识别"），
所以走 GitHub 的 **codeload tarball 接口 + 标准库解压**。能用 git 的机器会更快，
但"装不上"是最糟的结果，所以选谁都能跑的那条路。只装技能目录与 `handbook/`，
不会把整个仓库（含资产图片）倒进来；解压时做路径穿越防护。

### 装完必须登记，否则指令不会进提示词

`skills install` **刻意不改任何登记表**。新技能会被 `discover()` 列为"未登记"，
如实告诉作者"技能在，但我没为它写内置指令"。**只有登记到
`app/skills_registry.yaml` 才会进提示词**，否则它只出现在"已安装"列表里。

为什么故意不自动登记：

* 登记要写的是 `directives`（能进提示词的短指令）、`checkable`（可机检的规则）、
  `checkers`（已实现的检查器名）、`not_builtin`（做不到的部分）——**这些是判断，
  不是搬运**，自动生成只会产生一段没人核对过的提示词；
* 自动登记会让"我到底内置了什么"变得不可审计：作者以为装了就有，
  实际只是多了一堆来路不明的指令。

### 登记一条新技能，要填的字段与对应代码

| 字段 | 填什么 | 谁读它 |
|---|---|---|
| `phases` | proposal / draft / final 的一个或多个 | `prompt_block(phase)` 按阶段筛选；`phases` 命令按阶段列技能 |
| `directives` | 能进提示词的短指令（不是原文段落的搬运） | `prompt_block`，超 2200 字符会截断并说明 |
| `checkable` | 可机检的规则（人读的口径） | `skills show <id>` 打印 |
| `checkers` | `app/checks` 里已注册的检查器名 | `skills list` 的"检查器 N"计数；报错里的 `detected_by` |
| `not_builtin` | 明确做不到的部分 | `skills show <id>` 会单独列一段"没有内置的部分" |
| `use_for` | 一句话用途 | `skills list` 每条技能下面那一行 |

登记完跑 `skills list` 复核：该技能应当从"装了但没登记"的警告里消失，
并且出现"指令 N 检查器 M"的计数。**计数是给作者核对用的**——填了 `directives`
却没进提示词、写了 `checkers` 但检查器名拼错，都会在这里露出来。

---

## 六、中文适配的偏离（最有价值的一节）

技能原文是给**英文顶会论文**写的，照搬到中文学位论文会大面积误报。
总原则三条：**中文规范优先；不可机检的一律不实现；宁可漏报也不误报**（阈值全部取保守值，
只报上界不轻易报下界——条目提取本身可能漏，下界误报会让作者连正确的提示一起忽略）。

| 规则 | 技能原文怎么写 | 中文里怎么改，为什么 |
|---|---|---|
| **破折号** | 正文每出现一次 em-dash 就算 MAJOR | 英文里破折号是口语化插入手段，中文破折号是**合规标点**。只查**密度**：出现 ≥5 次**且**平均每 800 字一处以上才报（`check_dash_density`） |
| **段落形态** | "10 行 / 3~8 句" | 英文学术段落平均 5~7 句，中文段落句子更长、句数更少，所以改按**汉字数**：>600 字算过长；1 句且 >200 字算"一句成段"（`check_paragraph_shape`） |
| **摘要** | 英文五句公式（what/why/challenges/how/results） | 中文学位论文按 GB/T 7713 通行的**四要素**：目的／方法／结果／结论。出发点与技能一致——摘要要能拆成"为什么做—怎么做—做出什么—说明什么"（`check_abstract_elements`） |
| **膨胀词** | 查 "first"／无证据的最高级 | "张三首次提出"是**陈述别人的优先权（合规）**，"本文首次提出"才是需要证据的自夸。所以最高级只在**主语是本文/本研究/我们**时才报，且严重度升为 MAJOR（`check_inflation_words`） |
| **引言六段** | 六段流程 | 按**关键词标记**判而不是按段数判：中文论文常把"背景"与"局限"写在同一个自然段，按段数判必误报。缺哪个要素报哪个，缺「局限／目标／方案／贡献」是 MAJOR（`check_intro_six_segments`） |
| **LaTeX 规则** | `~\cite` 写法、label 命名、矢量图 | **不适用**于 Word 稿，列入 `not_builtin`（`pre-submission-reviewer`），不实现 |
| **drawio／图像类** | 读参考图、操作绘图工具、看图判 chartjunk | 本项目只处理文字稿，列入 `not_builtin`（`drawio-reconstruction`／`figure-designer`），不实现 |

两条**分工**也要写清，否则会显得重复报错：

* `check_overclaim` 管**以本文为主语的夸大断言**（"证明了""填补空白""重大意义"）；
  `check_inflation_words` 管**形容词层面的吹气**（"卓越的""颠覆性的""unprecedented"）。
  后者会**主动跳过**已经含"证明/填补/重大意义"的句子，让两条各管一段。
* `check_caption_has_finding` **只报"空题注"**（题注正文只有"表 3-1 结果"这种词），
  不报"题注没写结论"——后者需要读懂图与正文的关系，规则层判不准。

---

## 七、技能规则的检查器清单（25 个，分两个文件放）

### 第一批：Supervisor-Skills（14 个，`app/checks/skills_rules.py`）

| # | 检查器 | 查什么（一句话） | 来自哪条技能 |
|---|---|---|---|
| 1 | `check_limitations_count` | 既有工作的局限超过 3 条就报（说明没聚焦）；只报上界 | tech-paper-template |
| 2 | `check_challenges_count` | 关键挑战超过 3 条就报（研究范围失控） | tech-paper-template |
| 3 | `check_contributions_count` | 贡献超过 4 条报"太多"；数到 1~2 条编号贡献时报一条 INFO 级提示 | tech-paper-template |
| 4 | `check_contributions_banned_phrases` | 贡献条目里的空话："大量实验""全面分析""达到了先进水平""we propose a new method" | tech-paper-template / intro-drafter |
| 5 | `check_key_idea_single_sentence` | 关键思路写超过 2 句就报（写不成一句话通常说明还在"什么都想做"） | tech-paper-template |
| 6 | `check_paragraph_shape` | 段落形态：>600 字过长；1 句且 >200 字是一句成段 | pre-submission-reviewer |
| 7 | `check_dash_density` | 破折号密度：≥5 处且平均每 800 字一处以上 | pre-submission-reviewer |
| 8 | `check_inflation_words` | 膨胀词（卓越／完美／碾压／颠覆性…）与**以本文为主语**的无据最高级 | paper-polish |
| 9 | `check_abstract_elements` | 中文摘要是否具备目的／方法／结果／结论四要素（≥150 字才判） | pre-submission-reviewer |
| 10 | `check_intro_six_segments` | 引言六层要素齐备性（≥800 汉字才判；**按论文类型**决定要求哪几层） | intro-drafter |
| 11 | `check_gap_not_resource_excuse` | 研究缺口被写成"数据集太小／需要更多数据"这类资源借口 | benchmark-paper-template |
| 12 | `check_finding_lines` | 明显在做数据分析却全文没有一处"发现/结论"引导行（**只对基准/评测类**） | benchmark-paper-template |
| 13 | `check_process_leak` | 正文残留过程痕迹："作为一个 AI""以下是""根据上文的蓝图""3 CRITICAL" | paper-writer |
| 14 | `check_caption_has_finding` | 只写"图 3-1 结果"的空题注 | figure-designer |

### 第二批：academic-research-skills（11 个，`app/checks/skills_rules_ars.py`）

| # | 检查器 | 查什么（一句话） | 来自哪条技能 |
|---|---|---|---|
| 1 | `check_throat_clearing_openers` | 清嗓子起手语（值得注意的是／众所周知／不难发现）与元评论（"本节将讨论"，路线图句豁免） | academic-paper |
| 2 | `check_wordiness_pairs` | 绕远路的表达："由于…的原因"→因为、"进行了…的研究"→研究了、in order to→to | academic-paper |
| 3 | `check_title_shell_phrases` | 标题空壳："关于…的研究"（可删"关于…的"）、英文 "A study of …" | academic-paper |
| 4 | `check_universal_negative` | 全称否定："尚无研究""从未有人研究""国内外尚属空白""no study has ever" | ars-deep-research |
| 5 | `check_abstract_no_citation` | 摘要里出现引用角标（摘要必须能独立阅读） | academic-paper |
| 6 | `check_citation_density` | 单句引文堆砌：一句挂 5 个以上引用 | academic-paper |
| 7 | `check_word_count_deviation` | 正文字数偏离目标超过 ±10%（目标取自 `writing_brief.word_target`） | academic-paper |
| 8 | `check_temporal_deictic_claim` | 时间指示词（近年来／目前／最新）与因果或排序断言同句 | academic-paper / ars-deep-research |
| 9 | `check_doi_hygiene` | DOI 用旧域名 dx.doi.org、DOI 末尾多一个句点（点开会 404） | academic-paper |
| 10 | `check_claim_strength_vs_sources` | 用了"证实/证明"却只挂 1 篇引用（单篇只能"提示"，两篇以上才谈"证实"） | ars-deep-research |
| 11 | `check_causal_language` | 结果/讨论/结论章用强因果表述（导致／决定了／根本原因）且该句无引用 | academic-paper-reviewer |

### 中文适配：这些检查器偏离了技能原文的地方

上游是给**英文期刊**写的，直接照搬会在中文语料上大面积误报。每一处偏离都写在
对应函数的 docstring 里，这里汇总最关键的几条：

| 技能原文 | 中文适配 | 为什么 |
|---|---|---|
| 标题禁 `A study of X` | 中文的「X研究」**不报**，只报「关于X的研究」 | 中文里"数据资产估值研究"是完全合格的题目 |
| 26 个英文高频词（delve/pivotal…） | 未做中文映射，只在英文段落生效 | 那些词在中文里没有对应物 |
| em-dash 正文每出现一次=MAJOR | 只查**密度**（≥5 处且每 800 字一处以上） | 中文破折号是合规标点 |
| 段落 3~8 句 / >10 行 | 按**汉字数**判（>600 字过长） | 中文段落句少字多 |
| 摘要五句公式 | 中文四要素（目的/方法/结果/结论） | GB/T 7713 的口径 |
| 每段 ≥3 句 | 未实现（中文摘要与引言常有单句段） | 会误报 |
| APA 7 版式（全角括号、顿号、CE 年份） | 只留 DOI 两条 | GB/T 7714 与 APA 细节不同；DOI 两条与任何规范都不冲突 |
| "影响"算因果语言 | **不报"影响"**，只报导致/决定/根本原因 | 中文实证写作里"X 影响 Y"是标准表述 |

### 已有的那批

技能里还有一批检查器早就按规则手写在 `app/checks/writing_quality.py` 里
（如 `check_number_grounding`、`check_ai_tone_vocabulary`、`check_lit_synthesis`），
本次只是把它们在登记表的 `checkers` 字段里**声明了来源**，代码未改动。

---

## 八、诚实边界：`not_builtin` 里列的东西就是没有

**装了技能不等于这些能力就都有了。** 登记表里每个技能的 `not_builtin` 字段
明确列出了做不到的部分，`skills show <id>` 会把它们逐条打印出来。按类型归纳：

| 类型 | 具体做不到的事 | 出处 |
|---|---|---|
| **联网检索** | 新意与"当年 SOTA"比对；真实检索与引用核验（Crossref/OpenAlex）；场地与文献核查；审稿历史案例检索；venue 披露政策核查 | idea-evaluator、deep-research、benchmark-paper-template、rebuttal-guidance、vibe-research-workflow |
| **读图／作图** | 字号、位图痕迹、配色是否色盲友好；chartjunk 判定；实际作图（PPT/Figma/Matplotlib/draw.io）；把参考图重建成 .drawio | figure-designer、drawio-reconstruction |
| **作者判断** | 范式判断、机制可信度、作者能力自评；这个角度是否成立；运行实例的真实性；30 秒读得懂吗；"这次编辑有没有改变科学含义"；作者是否真的核对过 | idea-evaluator、deep-research、intro-drafter、paper-polish、vibe-research-workflow |
| **外部工具** | 抄袭检测；多子代理并行调研；人类基线与标注一致性；真实标注执行 | pre-submission-reviewer、deep-research、benchmark-paper-template |
| **体裁不适用** | LaTeX 编译相关规则（`~\cite`、label 命名、矢量图）不适用于 Word 稿 | pre-submission-reviewer |

---

## 九、代码在哪

| 文件 | 作用 |
|---|---|
| `app/skills.py` | 注册表：`skills_roots`（根目录）／`discover`（扫描 + 合并登记表）／`prompt_block`（提示词块）／`install`（tarball 安装）／`search_github` |
| `app/skills_registry.yaml` | 提炼表：每个技能的 phases／directives／checkable／checkers／not_builtin |
| `app/checks/skills_rules.py` | 14 个检查器；每个 docstring 里写明**与技能原文的偏离及原因** |
| `app/agents/writer_common.py` | `skill_block()`：把当前阶段的技能指令注入提示词 |
| `app/idea.py` | `review_idea`／`check_skeleton`：严格 JSON 契约 + 本地校验 |
| `app/review.py` | 五维预审报告；`Severity.BLOCKER` 在这一层显示为 `CRITICAL` |
| `app/rebuttal.py` | 九类意见 + 策略块 + 语气红线（纯本地） |
