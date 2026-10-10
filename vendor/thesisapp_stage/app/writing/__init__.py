"""写作方法论库：把"什么算好论文"拆成可检验的结构，而不是形容词。

## 为什么不直接塞一堆"著名论文"进提示词

用户要求"调用知名论文或学者的方法论"。但**"某篇论文说了什么"是不可验证的声明**：
我没有这些论文的原文，凭印象引用等于编造，而且无法核对、会随版本漂移。

所以这里采用另一种做法：**只收录学界公认、可公开查证的写作规范框架**，
每个框架都给出它的惯例名与可检验形式。例如：

* 引言的语步结构 —— 学界通称 **CARS 模型**（Create A Research Space），
  由 John M. Swales 在 *Genre Analysis*（1990）中提出。这是学术英语写作的通用范式，
  在学位论文写作指导中被广泛采用，**无需引用具体页码即可安全使用**。
* 文献综述按**研究脉络/主题**组织而非按作者罗列 —— 这是综述体裁的基本要求，
  而非某人的独有方法。
* 段落采用**主题句—证据—分析**结构 —— 学术段落的通行惯例。

**刻意不写的东西**：具体某篇论文的结论、具体某位学者的主张细节。凡是要写这类内容，
都必须来自检索到的原文（见 `writer_rag_examples.py`），不能凭记忆。

## 每条规则的两种形态

1. `Requirement` —— 给模型看的构造性指令（写的时候照着做）；
2. 对应的 checker（在 `app/checks/writing_quality.py`）—— 给评测看的可检验指标
   （写完能不能过）。

两者一一对应是这套设计的核心：**提示词里提的要求，必须能被检查出来**，
否则只是愿望。

## 与"AI 味"的关系

这里的要求（长短句交错、去模板连接词、论证推进）与降 AIGC 的方向**一致**：
模板腔句子既拉高 AI 味指数，也确实是写作质量缺陷。参考 `tools/aigc_pass.py`
里关于"该指数偏爱长句"的实测说明——**不要为指数牺牲可读性**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

SectionKind = Literal[
    "introduction",
    "literature_review",
    "method",
    "results",
    "discussion",
    "conclusion",
    "body",
]


@dataclass(frozen=True)
class Requirement:
    """一条写作要求。`checker` 指向 `app/checks/writing_quality.py` 里对应的检查器名。"""
    id: str
    title: str
    #: 给模型看的构造性指令（怎么做到）
    instruction: str
    #: 可检验的判定口径（怎么算没做到）
    criterion: str
    #: 违反后的严重度
    severity: str = "major"
    checker: str = ""
    #: 常见反例（模型最容易犯的具体形态）
    anti_pattern: str = ""
@dataclass(frozen=True)
class Blueprint:
    """一个章节类型的写作构造蓝图。"""
    kind: SectionKind
    name: str
    #: 这个章节必须完成的修辞任务（"要做什么"）
    purpose: str
    #: 结构骨架，按顺序
    moves: tuple[tuple[str, str], ...] = ()
    #: 具体写作要求
    requirements: tuple[Requirement, ...] = ()
    #: 体裁惯例来源说明（可公开查证，不含具体文献结论）
    provenance: str = ""
    #: 检索示例时用的查询词（交给 RAG 取真实片段做 few-shot）
    example_queries: tuple[str, ...] = ()

    def as_prompt_block(self) -> str:
        """渲染成提示词里的"写作规范"段落。"""
        lines = [f"## {self.name}写作规范", "", f"本章节的任务：{self.purpose}", ""]
        if self.moves:
            lines.append("### 结构骨架（按此顺序推进）")
            for i, (move, desc) in enumerate(self.moves, 1):
                lines.append(f"{i}. **{move}**：{desc}")
            lines.append("")
        if self.requirements:
            lines.append("### 硬性要求")
            for r in self.requirements:
                lines.append(f"- **{r.title}**：{r.instruction}")
                if r.anti_pattern:
                    lines.append(f"  - 反例（不要这样写）：{r.anti_pattern}")
            lines.append("")
        if self.provenance:
            lines += [f"> 体裁依据：{self.provenance}", ""]
        return "\n".join(lines)


# ===========================================================================
# 引言
# ===========================================================================

INTRODUCTION = Blueprint(
    kind="introduction",
    name="引言",
    purpose=(
        "在有限篇幅内建立起「这个研究值得做、而且只有这样做才对」的论证，"
        "让读者在读完引言后已经知道你要回答什么问题、为什么它没被回答、你打算怎么回答。"
    ),
    provenance=(
        "引言的三语步结构是学术写作的通用范式，学界通称 CARS 模型"
        "（Create A Research Space，John M. Swales, Genre Analysis, 1990）。"
        "此处只采用其公开的结构框架，不引用任何具体文献的结论。"
    ),
    moves=(
        ("确立研究领域（Establishing a territory）",
         "交代研究对象的现实重要性与学术重要性。要让读者认可「这个问题值得研究」，"
         "而不是泛泛地说「具有重要意义」。用可核实的政策、数据或事实开场，"
         "避免「随着……的发展」这类空转起手式。"),
        ("指出研究缺口（Establishing a niche）",
         "由领域现状收窄到未解决问题。缺口的表述必须是**具体的、可检验的**："
         "是方法上的、数据上的、还是解释上的不足？要能让读者判断「补上它是否值得」。"
         "这一段是全篇的枢纽——缺口不成立，全文不成立。"),
        ("占据研究空间（Occupying the niche）",
         "说明本文如何填补缺口：研究问题、思路、方法、预期贡献。"
         "用「本文……」直陈，不要用「笔者试图……」这类弱化表述。"
         "最后给出全文结构预告（各章做什么）。"),
    ),
    requirements=(
        Requirement(
            id="intro.cars",
            title="三语步齐备且顺序正确",
            instruction=(
                "必须依次完成「确立领域 → 指出缺口 → 占据空间」三步。"
                "缺口那一步不能被合并进背景里——它是引言的核心，要单独成段。"
            ),
            criterion="引言中能识别出缺口陈述段（含「尚未」「缺乏」「未回答」「存在争议」等缺口标记，"
                      "且指向本研究）与研究定位段（含「本文」「本研究」起首的陈述句）。",
            severity="blocker",
            checker="check_intro_cars",
            anti_pattern="背景写三段、缺口一句带过，然后直接跳到章节安排。",
        ),
        Requirement(
            id="intro.concrete_opening",
            title="开篇用可核实的事实而非空转句式",
            instruction=(
                "第一段应当出现**可核实的落点**：政策文件名、统计数据、制度年份、"
                "行业现象或既有研究的公认结论。不要用「随着经济社会的快速发展」"
                "「在当今时代背景下」这类不含信息的起手式。"
            ),
            criterion="引言首段含具体信息（制度名称/数字/年份/文献引用）之一，"
                      "且不含空转起手式。",
            severity="major",
            checker="check_intro_opening",
            anti_pattern="「随着数字经济的快速发展，数据资产日益受到重视。」",
        ),
        Requirement(
            id="intro.gap_specific",
            title="缺口必须具体到可检验",
            instruction=(
                "缺口要落到具体维度：是**方法**不足（既有方法不适用于某情形）、"
                "**证据**不足（缺某类数据或案例）、还是**解释**不足（现有理论无法说明某现象）。"
                "避免「研究还不够深入」「相关研究较少」这种不可检验的说法。"
            ),
            criterion="缺口陈述中含具体维度词（方法/数据/样本/机制/衔接/适用性等），"
                      "而非仅「不足」「较少」这类模糊表述。",
            severity="major",
            checker="check_intro_gap_specific",
        ),
        Requirement(
            id="intro.no_preview_dump",
            title="不要用文献罗列代替背景",
            instruction=(
                "引言里可以引文献支撑判断，但**不要逐篇介绍**——那是文献综述的任务。"
                "引言引文献的目的是「说明现状」，不是「清点成果」。"
            ),
            criterion="引言段落中，含 2 个以上文献引注的句子占比不过高"
                      "（阈值见检查器），且不出现「某某（年份）研究了……」的连续罗列。",
            severity="minor",
            checker="check_intro_no_citation_dump",
        ),
    ),
    example_queries=(
        "引言 研究背景 现实意义",
        "研究缺口 研究问题",
        "本文 研究思路 结构安排",
    ),
)


# ===========================================================================
# 引言（技术类：六段式）
# ===========================================================================
#
# **为什么技术类要有另一套引言蓝图。** CARS 三语步（确立领域→指出缺口→占据空间）
# 是通用范式，但它只规定了"三个语步"，没有规定技术类论文里那条更硬的链条：
# **局限 → 挑战 → 方案模块 → 贡献**，而且要求它们一一对应。
#
# 这条链条来自港科大（广州）DIAL 实验室开源的 Supervisor-Skills 项目
# （https://github.com/HKUSTDial/Supervisor-Skills ，`intro-drafter` 与
# `tech-paper-template` 两个技能，许可 CC BY-NC-SA 4.0 / CC BY 4.0）。
# 该项目自己明确写了一条边界，这里原样遵守：
#
# > 六段式是为 STEM 与技术类论文设计的。若研究方法属于人文、社科、经济、法学，
# > **不要**用这套骨架硬套。
#
# 这与本项目的论文类型分流（``app/writing/genres.py``）正好对上：技术类
# （``engineering`` / ``experimental``）走六段式，其余类型走 CARS 三语步。
# **按研究方法分流，按学科名分流是错的**——一篇用问卷做实证的教育学论文
# 不是"技术类"，而一篇做算法改进的社科论文其实要走技术骨架。
#
# 六段式本身**不写进成稿**（它是内部脚手架），成稿读起来仍应是连贯的六段散文。
# 这一点也在提示词里写明了。
INTRODUCTION_TECH = Blueprint(
    kind="introduction",
    name="引言（技术类）",
    purpose=(
        "在一页半到两页里，把整篇论文压缩成一条**闭合的逻辑链**："
        "背景与实例 → 已有工作的局限 → 问题本质与目标 → 关键挑战 → 方案概览 → 贡献。"
        "读者读完引言就应当知道你要解决什么、为什么现有方法不行、你打算怎么做、"
        "以及每条贡献落在哪一章。"
    ),
    provenance=(
        "六段式引言流程与「局限→挑战→模块→贡献」自洽性检查，来自 HKUST(GZ) DIAL 实验室的 "
        "Supervisor-Skills 项目（skills/intro-drafter、skills/tech-paper-template，"
        "CC BY-NC-SA 4.0 / CC BY 4.0）。该项目明确限定它适用于 STEM 与技术类论文，"
        "非 STEM 研究方法应改用通用蓝图。"
    ),
    moves=(
        ("背景、动机与运行实例",
         "交代场景、为什么这件事重要，并**尽可能给一个真实的运行实例**"
         "（来自真实数据、真实部署或真实案例）。实例的作用是让后面所有问题都有具体所指。"
         "**没有真实实例就不要编**——按抽象描述写，并在报告里请作者补一个。"),
        ("已有工作的局限",
         "指出既有做法**具体**不足在哪里，最多三条。"
         "局限要能被逐条检验（在什么条件下失效、缺哪类证据），"
         "不能写成「研究还不够深入」。"),
        ("问题本质与研究目标",
         "把问题收敛成一句话，交代硬约束（必须满足的条件），再给出本文目标。"
         "目标是全篇最该被评审引用的一句话。"),
        ("关键挑战",
         "指出**要达成这个目标为什么不容易**，最多三条。"
         "挑战必须由「实现关键思路」自然引出，不能是为了给后面的模块找理由而编造的。"),
        ("方案概览",
         "概述本文的方法框架，**一个模块对应一条挑战**。"
         "读者应当能在这里看出方案与挑战是一一对应的。"),
        ("贡献",
         "3~4 条，每条绑定一个章节（如「（第 4 章）」）。"
         "禁止「进行了大量实验」「取得了良好效果」这类空话——"
         "这类句子占了贡献条目的位置，却什么也没说。"),
    ),
    requirements=(
        Requirement(
            id="introtech.chain",
            title="局限与挑战必须成链",
            instruction=(
                "引言里若列出了「关键挑战」，就必须先有「已有工作的局限」，"
                "而且**每条挑战都要回应某条局限或某个硬约束**。"
                "只有挑战没有局限，读者会认为这些挑战是凭空造出来给方法找理由的。"
            ),
            criterion="引言中若识别出挑战语段，则必须同时识别出局限语段；"
                      "只有挑战而无局限即报出。",
            severity="major",
            checker="check_limitation_challenge_chain",
            anti_pattern="直接写「本文面临三大挑战」，却没说现有方法哪里不行。",
        ),
        Requirement(
            id="introtech.contrib_specific",
            title="贡献条目必须具体",
            instruction=(
                "每条贡献要写成**可检验的陈述**：提出了什么、在什么条件下、"
                "解决了哪一条挑战。禁止「进行了大量实验」「取得了良好效果」"
                "「具有重要的理论与实践意义」这类占了位置却没说事的句子。"
            ),
            criterion="贡献段落中的编号条目，若命中空话词表或过短（信息量不足），逐条报出。",
            severity="major",
            checker="check_contributions_specific",
            anti_pattern="「第三，本文进行了大量的实验，验证了模型的有效性。」",
        ),
        Requirement(
            id="introtech.contrib_sections",
            title="每条贡献绑定章节",
            instruction=(
                "贡献条目后面标明它的落点（如「（第 4 章）」「见 3.2 节」），"
                "让评审能直接翻回去核对。写不出落点的贡献，通常本身就没落实。"
            ),
            criterion="贡献段落中带章节指向的条目占比过低即报出。",
            severity="major",
            checker="check_contributions_map_sections",
        ),
        Requirement(
            id="introtech.tone",
            title="不许宣传腔",
            instruction=(
                "禁止「革命性的」「开创性的」「前所未有」「突破性的」"
                "「赋能」「助力」「深度融合」这类宣传词。"
                "学术写作靠事实说话，形容词堆得越多，评审越怀疑。"
            ),
            criterion="统计宣传腔词表中词的出现密度，超阈值即逐处报出。",
            severity="major",
            checker="check_ai_tone_vocabulary",
        ),
    ),
    example_queries=(
        "引言 研究背景 动机",
        "现有方法 局限",
        "本文贡献 章节安排",
    ),
)


# ===========================================================================
# 文献综述
# ===========================================================================

LITERATURE_REVIEW = Blueprint(
    kind="literature_review",
    name="文献综述",
    purpose=(
        "不是向读者清点「别人做了什么」，而是**通过梳理既有研究，把研究缺口的必然性论证出来**。"
        "读者读完应当觉得「确实还没有人回答这个问题」，而不是「哦，这些人都做过」。"
    ),
    provenance=(
        "综述按「研究脉络/主题」组织而非按作者或时间罗列，是综述体裁的基本规范；"
        "具体到操作形态（按方法族、按争论焦点、按解释路径分组），各学科惯例略有差异，"
        "这里只采用通用要求，不引用任何具体文献。"
    ),
    moves=(
        ("划定范围与主线",
         "开篇交代本综述覆盖哪些研究、按什么线索组织。"
         "让读者知道接下来会看到几条脉络，而不是一堆并列的段落。"),
        ("按脉络分组评述",
         "把既有研究**归入 2~4 条主线**（例如按方法族、按争论焦点、按解释路径）。"
         "每条主线内部要有承接：先说这一族研究共同关心什么、达成了什么共识，"
         "再指出内部分歧或共同局限。"),
        ("落到缺口",
         "由各条主线的局限汇聚成本文要回答的问题。"
         "综述的结尾必须**推出**缺口，而不是另起一段说「综上所述，研究较少」。"),
    ),
    requirements=(
        Requirement(
            id="lit.no_one_sentence_per_paper",
            title="禁止「一篇论文半句话」",
            instruction=(
                "**这是本助手最要紧的一条要求。** 不得出现「A（年份）做了什么。」"
                "「B（年份）做了什么。C（年份）做了什么。」这种逐篇罗列。"
                "同一主题下的多篇文献应当**合并成一个判断**：先给出结论或分歧，"
                "再把若干文献作为证据一并列出，例如「多数研究认为X（A, 2020; B, 2021），"
                "但C（2022）在Y情形下得到相反结果」。"
            ),
            criterion=(
                "检测「一文一句」模式：统计由单独作者引注开头的句子比例"
                "（如「张三（2023）发现……」），以及每句平均含有的文献引注数。"
                "前者过高或后者过低即判为不合格。"
            ),
            severity="blocker",
            checker="check_lit_synthesis",
            anti_pattern=(
                "「李四（2021）研究了数据资产估值。王五（2022）改进了收益法。"
                "赵六（2023）构建了指标体系。」——三句三篇，彼此无关联。"
            ),
        ),
        Requirement(
            id="lit.coherence",
            title="前后必须连贯（有显式衔接）",
            instruction=(
                "段落之间、文献之间要有逻辑连接词或承接句，说明它们的关系是"
                "**继承、补充、对立还是递进**。不能只是把文献按顺序摆上去。"
                "每引入一条新主线时，要用一句话交代它与前一条的关系。"
            ),
            criterion=(
                "统计综述各段之间的承接情况：段首必须有衔接成分"
                "（逻辑连接词、指代前文的短语，或明确的主题推进句），"
                "禁止连续多段直接以文献引注开头。"
            ),
            severity="major",
            checker="check_lit_coherence",
            anti_pattern="上一段讲方法改进，下一段第一句直接是「张三（2023）提出了……」",
        ),
        Requirement(
            id="lit.topic_sentence",
            title="每段结论先行",
            instruction=(
                "每个自然段的**第一句应当是这一段要说明的判断**（主题句），"
                "而不是某篇文献。好的主题句形如「在估值方法层面，国内研究已形成以收益法"
                "为核心的格局」，后面再摆文献作证。"
            ),
            criterion="段落首句不含「作者（年份）」式引注，且为陈述性判断句。",
            severity="major",
            checker="check_paragraph_topic_sentence",
        ),
        Requirement(
            id="lit.critique_not_summary",
            title="要有评，不能只有述",
            instruction=(
                "介绍完一组研究后，必须给出**评价**：它们的共同局限是什么、"
                "在什么条件下不适用、留下了什么未解问题。只有「述」没有「评」的段落"
                "等于把综述写成了摘要汇编。"
            ),
            criterion=(
                "统计评价性表述的出现密度（「然而」「但」「局限」「未能」「尚未」"
                "「仅在……条件下」等），低于阈值即判为「只述不评」。"
            ),
            severity="major",
            checker="check_lit_critique",
        ),
    ),
    example_queries=(
        "文献综述 研究现状",
        "研究方法 改进",
        "研究结论 局限",
    ),
)


# ===========================================================================
# 正文（分析/论证章节）
# ===========================================================================

BODY = Blueprint(
    kind="body",
    name="正文论证",
    purpose=(
        "把数据、案例或模型结果**转化为论证**。读者要看到的不只是「结果是什么」，"
        "而是「这个结果说明了什么、为什么能说明、在什么范围内成立」。"
    ),
    provenance=(
        "段落采用「主题句—证据—分析」结构、论证逐步推进而非并列堆砌，"
        "是学术写作的通行惯例，不依附于某一具体文献。"
    ),
    moves=(
        ("提出本节的判断",
         "开篇点明本节要论证什么，以及它在全文论证链中的位置。"),
        ("给出证据",
         "摆事实：数据、案例细节、模型输出。证据要能追溯来源，"
         "数字必须与图表或引用对应。"),
        ("分析与解释",
         "解释证据为什么支持前面的判断：机制是什么、与既有研究是否一致、"
         "不一致的可能原因。**这一段最容易被省略，也最能体现写作水平。**"),
        ("界定适用范围",
         "说明结论在什么条件下成立、有什么边界。主动写局限比等着评审指出来好。"),
    ),
    requirements=(
        Requirement(
            id="body.tea_paragraph",
            title="段落须有主题句与分析",
            instruction=(
                "每段遵循「主题句 → 证据 → 分析」结构。至少要有一句是**解释性**的"
                "（说明为什么、意味着什么），不能通篇都是事实罗列。"
            ),
            criterion=(
                "段落中句数 ≥3，首句为不含数据的陈述性判断（主题句），"
                "且段内含分析性表述（「这表明」「原因在于」「说明」「意味着」「因此」等）。"
            ),
            severity="major",
            checker="check_paragraph_analysis",
            anti_pattern="整段都是「2023年为X，2024年为Y，同比增加Z%」，没有任何解释。",
        ),
        Requirement(
            id="body.progressive",
            title="论证要推进，不要并列堆砌",
            instruction=(
                "同一节的各段之间应是**递进或因果**关系，不是平行罗列。"
                "如果几段可以任意调换顺序而不影响阅读，说明论证没有推进。"
                "段落之间用「进一步」「反过来」「这引出」等显式推进语。"
            ),
            criterion="节内相邻段落之间存在推进性衔接（递进/因果/转折标记），"
                      "且不出现连续三段以上结构雷同（句数、句长分布高度一致）。",
            severity="major",
            checker="check_body_progression",
        ),
        Requirement(
            id="body.claim_evidence",
            title="每个论断都要有依据",
            instruction=(
                "凡出现「显著提升」「明显改善」「具有重要作用」这类断言，"
                "必须紧跟证据（数字、图表编号、文献引注）。没有依据的断言宁可删掉。"
            ),
            criterion="含强断言词（显著/明显/大幅/有效/关键）的句子，"
                      "其所在段落须有数字、图表引用或文献引注。",
            severity="blocker",
            checker="check_claim_evidence",
            anti_pattern="「数据资产显著提升了企业绩效。」——不说提升了多少、依据是什么。",
        ),
        Requirement(
            id="body.no_template_connectives",
            title="删掉纯结构标记",
            instruction=(
                "「综上所述」「值得注意的是」「总而言之」「不言而喻」「众所周知」"
                "这类词不含信息，学术文体中应删除或替换为有实义的连接。"
            ),
            criterion="统计模板连接词密度，超阈值即报出并定位。",
            severity="minor",
            checker="check_template_connectives",
        ),
        Requirement(
            id="body.paragraph_variance",
            title="句长要长短交错",
            instruction=(
                "连续多句长度相当会让文字读起来像模板。"
                "观点句可以短，证据句和分析句通常较长，自然形成节奏。"
            ),
            criterion="段落内句长变异系数低于阈值即提示（不是硬性错误，是风格提示）。",
            severity="info",
            checker="check_paragraph_variance",
        ),
    ),
    example_queries=(
        "实证结果 分析",
        "机制 检验",
        "参数 敏感性",
    ),
)


# ===========================================================================
# 研究设计
# ===========================================================================

METHOD = Blueprint(
    kind="method",
    name="研究设计",
    purpose=(
        "让读者（尤其是想重复这项研究的人）能凭这一章**把研究重做一遍**，"
        "并相信所得结果确实能回答引言提出的问题。"
        "这一章的功能不是「显得规范」，而是**替结论担保**：方法与问题不匹配，"
        "后面所有结果都不成立。"
    ),
    provenance=(
        "研究设计章的通行要求是「可复现性」：数据来源、样本筛选、变量测量、"
        "模型设定四件事必须写到他人可照做的粒度。这是科技论文的通用体裁惯例"
        "（IMRaD 结构中的 Methods 部分），不依附于某一具体文献。"
    ),
    moves=(
        ("研究思路总述",
         "用一段交代本章要说明的四件事（数据、变量、模型、方法选择理由），"
         "并把方法与本研究的**具体问题**挂上钩——不要写成教科书式的「方法介绍」。"),
        ("数据来源与样本",
         "数据从哪来、覆盖什么区间、经过哪些筛选、最终样本量是多少。"
         "每一处筛选都要给出理由与影响，不能只写「剔除缺失值后得到 N 个观测」。"),
        ("变量定义与测量",
         "每个变量的含义、度量方式、单位、数据来源。"
         "有既有文献用法的要给出出处；自建指标要说明构造过程与依据。"),
        ("模型设定与方法选择",
         "写出模型形式、关键参数的含义，并**解释为什么这个方法适合本研究的问题**。"
         "估计方法、检验统计量、显著性水平的取法都要交代。"),
        ("稳健性与识别策略",
         "主动说明结果可能受什么干扰、用什么办法排除（替换度量、子样本、"
         "安慰剂检验等）。写在这里比等评审追问更主动。"),
    ),
    requirements=(
        Requirement(
            id="method.reproducible",
            title="写到别人能照着做一遍",
            instruction=(
                "数据区间、样本量、变量口径、模型形式、检验方法这五件事必须**具体可查**："
                "写「2020—2024 年 A 股上市公司，剔除金融业与 ST 样本，最终 3 217 个观测」，"
                "不要写「选取了相关数据」「采用了合适的方法」。"
            ),
            criterion="本章含具体数字（年份区间/样本量/变量数/参数值）与可查的数据来源名称；"
                      "且不含「相关数据」「合适的方法」「一定程度上」这类模糊替代词。",
            severity="major",
            checker="check_method_specific",
            anti_pattern="「本文选取相关数据，采用合适的方法进行了实证分析。」",
        ),
        Requirement(
            id="method.justify_choice",
            title="方法选择必须给理由",
            instruction=(
                "每一种方法、每一个模型的选用都要说明**为什么它适合本研究的问题**，"
                "而不是「因为文献常用」。如果存在竞争性方法，说明为什么不用它。"
            ),
            criterion="本章含方法选择理由的表述（「之所以采用……是因为」「相较于……，"
                      "本方法更适合……」），且理由指向本研究的数据特征或问题类型。",
            severity="major",
            checker="check_method_choice_reason",
        ),
        Requirement(
            id="method.claim_evidence",
            title="方法描述中的每个判断都要有依据",
            instruction=(
                "「该方法能有效处理内生性」「该指标被广泛使用」这类断言必须给出依据"
                "（文献引注或方法学论证）。"
            ),
            criterion="含强断言词的句子所在段落须有引注或论证。",
            severity="blocker",
            checker="check_claim_evidence",
        ),
        Requirement(
            id="method.no_textbook_dump",
            title="不要抄教科书",
            instruction=(
                "方法章不是教材章节。公认的公式与定义给出出处即可，"
                "篇幅要花在**本研究怎么用**上，而不是复述方法原理。"
            ),
            criterion="本章不含大段与本研究无关的通用方法介绍（表现为连续多段无本研究"
                      "数据/变量/参数的具体取值）。",
            severity="minor",
            checker="check_method_no_textbook_dump",
        ),
    ),
    example_queries=(
        "研究设计 数据来源",
        "变量定义 测量",
        "模型设定 估计方法",
    ),
)


# ===========================================================================
# 实证结果（核心内容）
# ===========================================================================

RESULTS = Blueprint(
    kind="results",
    name="实证结果",
    purpose=(
        "把分析产出**如实、有序地摆出来**，让读者能自己看到证据的形状。"
        "这一章的读者预期是「发生了什么」，机制解释与研究对照属于讨论章。"
    ),
    provenance=(
        "IMRaD 结构的一条通行纪律：结果章报告发现、讨论章解释发现，"
        "两者分开。理由是可核查性——结果与解释混在一起时，读者无法分辨"
        "哪些是从数据里看出来的、哪些是作者的推断。这是体裁惯例，不依附于具体文献。"
    ),
    moves=(
        ("描述性发现",
         "先给样本与主要变量的基本面貌（分布、趋势、相关关系），"
         "并指出其中**值得注意的特征**——不是把统计表念一遍。"),
        ("主结果",
         "按假设或研究问题的顺序报告核心结果。每个结果要交代：系数/效应量的"
         "大小与方向、是否显著、对应哪个假设。"),
        ("稳健性检验",
         "换度量、换样本、换设定之后结果是否稳定。"
         "**不稳健的结果必须如实报告**，不能只报稳健的部分。"),
        ("小结与对照",
         "把结果与引言提出的预期做一次对照：哪些得到支持、哪些没有。"
         "只做对照，不做机制解释（那是讨论章的任务）。"),
    ),
    requirements=(
        Requirement(
            id="results.every_number_interpreted",
            title="数字必须说明它意味着什么",
            instruction=(
                "报告完一个系数或一组数据后，要用一句话说明**它的量级意味着什么**"
                "（例如「相当于样本均值的 12%」）。只说「系数为 0.34，在 1% 水平上显著」"
                "等于没写。"
            ),
            criterion="含数据的段落必须含解释性表述（「表明」「意味着」「相当于」"
                      "「说明」等），否则判「只摆数据不分析」。",
            severity="major",
            checker="check_paragraph_analysis",
            anti_pattern="「表 4 显示，系数为 0.34，p<0.01。」——读者不知道 0.34 算大还是小。",
        ),
        Requirement(
            id="results.no_new_method",
            title="结果章不引入新方法",
            instruction=(
                "本章只使用研究设计章已经交代过的方法。中途出现未交代的模型、"
                "变量或样本处理，读者无法判断结果是怎么来的。"
            ),
            criterion="本章提到的方法/变量名应能在研究设计章中找到；"
                      "否则提示读者此处需要回到研究设计章补充交代。",
            severity="major",
            checker="check_results_no_new_method",
        ),
        Requirement(
            id="results.report_negative",
            title="不显著的结果也要报",
            instruction=(
                "假设未得到支持的，要如实写出，并说明它意味着什么。"
                "只报显著结果会让读者高估证据强度——这是评审最常质疑的一点。"
            ),
            criterion="若正文出现假设检验类表述，需同时存在对未支持假设的处理说明。",
            severity="minor",
            checker="check_results_report_negative",
        ),
        Requirement(
            id="results.claim_evidence",
            title="每个论断都要有依据",
            instruction="结果陈述必须与具体数据、表格编号或统计量对应。",
            criterion="含强断言词的句子所在段落须有数字、图表引用或引注。",
            severity="blocker",
            checker="check_claim_evidence",
        ),
    ),
    example_queries=(
        "实证结果 回归分析",
        "稳健性检验",
        "异质性分析",
    ),
)


# ===========================================================================
# 讨论
# ===========================================================================

DISCUSSION = Blueprint(
    kind="discussion",
    name="讨论",
    purpose=(
        "回答「这些结果说明了什么」。讨论章的价值不在于重复结果，"
        "而在于把结果**放回既有研究脉络**里，说明它改变了我们对什么问题的认识。"
    ),
    provenance=(
        "讨论章的通行语步为「重述发现 → 与既有研究对照 → 解释机制 → 意义与局限」，"
        "是 IMRaD 结构中 Discussion 部分的体裁惯例。"
        "此处只采用其公开的结构框架，不引用任何具体文献的结论。"
    ),
    moves=(
        ("重述主要发现",
         "用一段概括本研究发现，措辞要**弱于**结果章：说的是「本研究发现……」"
         "而不是「本文证明了……」。"),
        ("与既有研究对照",
         "与文献综述里梳理过的脉络逐一对照：哪些结论一致、哪些相反、"
         "不一致的可能原因是什么（样本、时期、度量、情境差异）。"
         "**这一段是讨论章的核心，也最容易写成自我表扬。**"),
        ("机制解释",
         "说明结果为什么是这样：背后的作用机制是什么、有哪些竞争性解释、"
         "现有证据能否排除它们。"),
        ("意义与边界",
         "理论与实践层面的含义（谨慎措辞，不夸大），以及结论成立的条件、"
         "外部效度的边界。主动写局限比等着评审指出来好。"),
    ),
    requirements=(
        Requirement(
            id="disc.contrast_literature",
            title="必须与既有研究对照",
            instruction=(
                "讨论章必须出现与既有研究的一致性/差异性对照，并解释差异来源。"
                "只说自己发现了什么、不提别人研究过的，等于把讨论写成了第二个结果章。"
            ),
            criterion="本章含对照性表述（「与……一致」「不同于……」「与……的差异可能源于」）"
                      "且带文献引注。",
            severity="major",
            checker="check_disc_contrast",
        ),
        Requirement(
            id="disc.no_overclaim",
            title="不许夸大",
            instruction=(
                "禁止使用「首次」「填补空白」「开创性」「重大意义」「证明了」这类"
                "无法由数据支持的强评价词。发现的意义由读者判断，不由作者宣布。"
            ),
            criterion="命中强评价词即报出并定位，由作者改弱措辞。",
            severity="major",
            checker="check_overclaim",
        ),
        Requirement(
            id="disc.no_new_results",
            title="讨论章不添新数据",
            instruction=(
                "讨论章不引入结果章没有的新数据、新表格。"
                "分析没做过就说没做过，不要在讨论里顺手补一个回归。"
            ),
            criterion="本章不应出现新的表格编号或未在结果章出现的统计量。",
            severity="major",
            checker="check_disc_no_new_results",
        ),
        Requirement(
            id="disc.limitation_specific",
            title="局限要具体",
            instruction=(
                "局限不能写成「本研究存在一定局限性」这种套话。"
                "要具体到：样本的时间/地域范围、变量度量的近似程度、"
                "因果识别的限制、结论不能外推到什么情形。"
            ),
            criterion="局限表述含具体维度词（样本/时期/地域/度量/识别/外推），"
                      "且不含「存在一定局限」「有待完善」这类空泛说法。",
            severity="major",
            checker="check_limitation_specific",
        ),
    ),
    example_queries=(
        "讨论 机制分析",
        "研究局限",
        "理论贡献 实践启示",
    ),
)


# ===========================================================================
# 结论（总结）
# ===========================================================================

#: 结论章的特殊政策：**可以生成结构草稿，但每一条都必须由作者确认**。
#:
#: 为什么它和"创新点"不同级：创新点是**作者的学术主张**（"这个问题上我提出了
#: 什么新东西"），只能由作者声明；而结论的主体是**对本文已经写出的论断做汇总**，
#: 素材来自作者自己的正文，工具的职责是"别漏、别变形"，不是"替作者下判断"。
#: 因此这一章生成出来的每一句都必须是正文里已经成立的话的浓缩，并且：
#:
#: * 出现正文没有的数字 → 逐条报出（``check_conclusion_grounding``）；
#: * 出现创新点声明、强评价词 → 逐条报出，视为越界；
#: * 整章产出后统一标记 ``needs_author``，提示作者逐条核对。
#:
#: 这条界线写在这里而不是只写在代码里，是为了让"工具改了什么、没改什么"可核对。
CONCLUSION = Blueprint(
    kind="conclusion",
    name="结论",
    purpose=(
        "让读者用最短的时间拿到本文**已经论证成立**的判断，"
        "并知道这些判断在什么范围内成立、下一步该往哪里走。"
        "结论不是摘要的复制：摘要面向「要不要读」，结论面向「读完留下了什么」。"
    ),
    provenance=(
        "结论章的通行语步为「研究问题与做法 → 主要结论 → 启示 → 局限与展望」，"
        "且有一条被反复强调的纪律：**结论章不引入新证据**。"
        "这是学位论文写作的通用体裁规范，不依附于具体文献。"
    ),
    moves=(
        ("回应研究问题",
         "用一段（不超过 200 字）回到引言提出的研究问题，说明本文做了什么、"
         "用什么数据与方法回答它。不要重复引言的背景论述。"),
        ("逐条给出主要结论",
         "每一条结论对应正文某一章已经论证过的判断，形如"
         "「研究发现，……（对应第 4 章）」。**条数控制在 3~5 条**，"
         "每条都要能追回正文的具体章节。"),
        ("理论与实践启示",
         "从已经成立的结论**推**出启示，不要另起新的主张。"
         "措辞用「对……具有参考意义」，不用「具有重大意义」。"),
        ("局限与展望",
         "汇总正文已经写出过的局限，给出**具体可行**的后续研究方向"
         "（例如「将样本扩展到非上市企业以检验外部效度」），"
         "不要写「有待进一步研究」。"),
    ),
    requirements=(
        Requirement(
            id="concl.no_new_facts",
            title="不得引入正文没有的事实",
            instruction=(
                "结论里出现的每一个数字、年份、比例、文献引注，"
                "都必须在正文里已经出现过。**不许出现新的数据或新的引用。**"
                "需要新证据才能说的话，说明它还不该出现在结论里。"
            ),
            criterion="结论中的数字须能在正文（结论之前的部分）中逐一找到；"
                      "出现正文没有的数字即报致命问题并就地标注。",
            severity="blocker",
            checker="check_conclusion_grounding",
            anti_pattern="「本文发现数据资产可使企业绩效提升 23.7%。」——正文里从没算过这个数。",
        ),
        Requirement(
            id="concl.traceable",
            title="每条结论可追溯到正文章节",
            instruction=(
                "每条主要结论后面标明它依据的是哪一章（如「（第 4 章）」），"
                "让评审能直接翻回去核对。"
            ),
            criterion="主要结论条目中带章节指向的比例应较高；过低则提示补充依据指向。",
            severity="major",
            checker="check_conclusion_traceable",
        ),
        Requirement(
            id="concl.no_innovation_claim",
            title="不代写创新点",
            instruction=(
                "**不得出现创新点声明**：「本文的创新之处在于」「首次提出」"
                "「填补了……空白」「开创性地」。创新点是作者对自己学术贡献的主张，"
                "必须由作者自己写。"
            ),
            criterion="命中创新点或强评价词即为越界，报致命问题。",
            severity="blocker",
            checker="check_conclusion_no_innovation",
            anti_pattern="「本文的创新之处在于首次将 A 方法应用于 B 情境。」",
        ),
        Requirement(
            id="concl.no_overclaim",
            title="不许夸大结论",
            instruction=(
                "禁止「证明了」「充分表明」「完全说明」这类把统计关联说成确定性因果的措辞。"
                "用「本研究的证据支持」「表明……的可能性」这类与证据强度相称的表述。"
            ),
            criterion="命中强断言/强评价词即报出。",
            severity="major",
            checker="check_overclaim",
        ),
    ),
    example_queries=(
        "研究结论",
        "结论与展望",
        "研究启示 局限",
    ),
)


#: 章节类型 → 建议篇幅占比。用于把总字数**按章节职能**分配，而不是平均分。
#:
#: 平均分是错的：文献综述与实证/核心分析章通常各占两成以上，而结论只占一成左右。
#: 平均分会让结论被撑成一篇啰嗦的复述，同时把最需要篇幅的核心章饿死。
#: 数值取学位论文的常见区间中值，作者可在写作意图里覆盖。
WORD_SHARE: dict[str, float] = {
    "introduction": 0.10,
    "literature_review": 0.22,
    "method": 0.13,
    "results": 0.26,
    "discussion": 0.17,
    "conclusion": 0.09,
    "body": 0.20,
}


BLUEPRINTS: dict[str, Blueprint] = {
    "introduction": INTRODUCTION,
    "literature_review": LITERATURE_REVIEW,
    "method": METHOD,
    "results": RESULTS,
    "discussion": DISCUSSION,
    "conclusion": CONCLUSION,
    "body": BODY,
}

#: 全部蓝图（含同一 ``kind`` 的多套变体），供遍历用。
ALL_BLUEPRINTS: tuple[Blueprint, ...] = (
    INTRODUCTION,
    INTRODUCTION_TECH,
    LITERATURE_REVIEW,
    METHOD,
    RESULTS,
    DISCUSSION,
    CONCLUSION,
    BODY,
)

#: **对所有章节类型都适用**的检查器。
#:
#: 为什么需要这一层：写作质量检查器如果只挂在某个蓝图的 requirements 上，
#: 就只在"该章节被识别为那种类型"时才运行。实测踩过——文献综述内容常常散落在
#: 「相关概念」「理论基础」「研究现状」等小节里（这些会被归类为正文），
#: 于是本助手最要紧的「一篇论文半句话」检查**根本没跑**，自查形同虚设。
#:
#: 这些检查的共同点是**与章节类型无关**：任何章节都可能逐篇罗列文献、
#: 可能段落无分析、可能有没依据的断言、可能满是模板腔。所以它们必须无条件运行。
UNIVERSAL_CHECKERS: tuple[str, ...] = (
    "check_lit_synthesis",
    "check_lit_coherence",
    "check_lit_critique",
    "check_paragraph_topic_sentence",
    "check_paragraph_analysis",
    "check_claim_evidence",
    "check_overclaim",
    "check_template_connectives",
    "check_paragraph_variance",
)


def checkers_for(kind: str) -> tuple[str, ...]:
    """该章节类型应当运行的全部检查器：蓝图专属 + 通用。

    去重并保持稳定顺序，便于测试与日志比对。
    """
    bp = BLUEPRINTS.get(kind, BODY)
    out: list[str] = []
    for name in [r.checker for r in bp.requirements if r.checker] + list(UNIVERSAL_CHECKERS):
        if name not in out:
            out.append(name)
    return tuple(out)

#: 章节标题 → 蓝图。用关键词匹配中文论文里常见的章节名。
#:
#: **顺序即优先级**：越具体的线索必须排在越靠前。例如「研究结论」既含「研究」
#: 也含「结论」，而「结论」才是它真正的类型；把它排在「研究设计」后面会命中
#: 前者。同理「实证结果分析」应落到 results 而不是 body。
_KIND_HINTS: tuple[tuple[str, str], ...] = (
    # -- 结论类（必须先判，避免被"研究""分析"等泛词抢走）------------------
    ("结论", "conclusion"),
    ("结语", "conclusion"),
    ("总结", "conclusion"),
    ("展望", "conclusion"),
    ("研究启示", "conclusion"),
    # -- 引言类 -----------------------------------------------------------
    ("引言", "introduction"),
    ("绪论", "introduction"),
    ("导论", "introduction"),
    ("研究背景", "introduction"),
    ("问题提出", "introduction"),
    # -- 综述类 -----------------------------------------------------------
    ("文献综述", "literature_review"),
    ("文献回顾", "literature_review"),
    ("文献述评", "literature_review"),
    ("研究现状", "literature_review"),
    ("研究述评", "literature_review"),
    ("理论基础", "literature_review"),
    # -- 研究设计类（要排在"研究"类泛词之前）------------------------------
    ("研究设计", "method"),
    ("研究方案", "method"),
    ("研究假设", "method"),
    ("变量", "method"),
    ("模型构建", "method"),
    ("模型设定", "method"),
    ("研究方法", "method"),
    ("数据来源", "method"),
    ("样本", "method"),
    # -- 讨论类 -----------------------------------------------------------
    ("讨论", "discussion"),
    ("机制分析", "discussion"),
    ("进一步分析", "discussion"),
    ("异质性", "discussion"),
    # -- 结果类 -----------------------------------------------------------
    ("实证", "results"),
    ("结果", "results"),
    ("检验", "results"),
    ("案例分析", "results"),
    ("调查分析", "results"),
    ("分析", "results"),
)


def word_share(kind: str) -> float:
    """该章节类型的建议篇幅占比（0~1）。未知类型退回正文的占比。"""
    return WORD_SHARE.get(kind, WORD_SHARE["body"])


#: 走「技术类六段式引言」的论文类型。判据是**研究方法**，不是学科名。
#:
#: 一个用问卷做实证的教育学论文不是技术类；一个做算法改进的社科论文反而是。
#: 所以这里只列方法上属于"做东西并验证"的类型。
TECH_INTRO_GENRES: frozenset[str] = frozenset({"engineering", "experimental"})


def blueprint_for(title: str, genre_id: str = "") -> Blueprint:
    """按章节标题**和论文类型**选蓝图。

    与 :func:`classify` 的区别：``classify`` 只看标题，因此无法区分
    "技术类引言"与"通用引言"。而这两者的构造要求差别很大（前者要求
    「局限→挑战→模块→贡献」成链，后者要求 CARS 三语步），所以凡是**能拿到
    论文类型**的地方都应该用这个函数。

    ``classify`` 仍然保留给拿不到类型的地方（如逐句检查器按章节标题归类）——
    两者的 ``kind`` 相同，所以检查器不会因为选了哪个蓝图而漏跑。
    """
    bp = classify(title)
    if bp.kind == "introduction" and (genre_id or "").strip().lower() in TECH_INTRO_GENRES:
        return INTRODUCTION_TECH
    return bp


def classify(title: str) -> Blueprint:
    """按章节标题判断该用哪套蓝图。

    默认落到 `body`：绝大多数章节都是"论证某件事"，用正文蓝图不会错得太离谱；
    而误用引言蓝图会让章节被套上"指出缺口"的要求，那才是真正的错。
    """
    t = (title or "").strip()
    for hint, kind in _KIND_HINTS:
        if hint in t:
            return BLUEPRINTS[kind]
    return BODY


def all_requirements() -> list[Requirement]:
    """所有蓝图的要求，供检查器与界面统一遍历。"""
    seen: dict[str, Requirement] = {}
    for bp in ALL_BLUEPRINTS:
        for r in bp.requirements:
            seen.setdefault(r.id, r)
    return list(seen.values())
