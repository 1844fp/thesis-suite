"""论文类型（genre）画像：让这套系统能写**任何学科**的论文。

## 为什么这一层是必需的

原先的默认章节结构是写死的六章（绪论—文献综述—研究设计—实证结果—讨论—结论），
那是**经管实证论文**的结构。把它套到别的选题上会立刻出问题：

* 一篇工科的系统设计论文没有"实证结果"章，它的主体是"需求分析—系统设计—实现与测试"；
* 一篇人文学科的论文没有"研究设计"章，它的主体是多章的**理论论证**；
* 一篇医学实验论文的结构是 IMRaD（引言—材料与方法—结果—讨论），"文献综述"独立成章反而少见。

更隐蔽的是**词表耦合**：``app/checks/writing_quality.py`` 原先硬编码了一份方法名词表
（双重差分、PSM、多期超额收益法……）和一批缺口维度词（估值、折现率、收益分割……）。
这些词只对经管/会计有意义，用在别的学科上会静默失效：

* "结果章是否引入了研究设计章未交代的方法"——对一篇材料学论文，它永远报不出来；
* "研究缺口是否具体"——它只认金融维度的词，于是把"该合金在高温下的疲劳寿命缺少实验数据"
  这种非常具体的缺口判成"不具体"。

所以这一层做两件事：**给出该类型的默认章节结构**，以及**提供该类型可用的词表**。
词表跟着类型走，检查器就不再假设学科。

## 类型是模板，不是约束

作者显式给的章节（开题报告里的章节树、``--chapter`` 指定的章节）**永远优先**。
类型画像只在"作者没说"时提供起点。这一点与 ``app/writing/__init__.py`` 里的
章节蓝图是两层不同的东西：

* **蓝图**回答"这一章该怎么写"（修辞任务、语步、段落结构）——与学科无关；
* **画像**回答"这篇论文该有哪几章、这个学科常见哪些方法"——与学科强相关。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

__all__ = [
    "GenreProfile",
    "GENRES",
    "DEFAULT_GENRE",
    "get_genre",
    "genre_choices",
    "profile_from_ctx",
    "guess_genre",
]


@dataclass(frozen=True)
class GenreProfile:
    """一种论文类型的画像。"""

    id: str
    name: str
    summary: str
    #: 该类型的默认章节结构。每项 ``{title, children, focus, needs}``。
    default_sections: tuple[dict[str, Any], ...] = ()
    #: 该类型常见的研究方法名。用于"结果章是否引入了设计章未交代的方法"这一检查。
    #:
    #: 刻意只收**辨识度高**的方法名：宽松的词表会把「回归」「分析」这类通用词收进来，
    #: 于是每一章都在"引入新方法"，检查器变成噪音。**误报会毁掉工具可信度。**
    method_terms: tuple[str, ...] = ()
    #: 该类型的"研究缺口"具体维度词。用于"缺口是否具体到可检验"这一检查。
    gap_dimensions: tuple[str, ...] = ()
    #: 该类型的示例检索词：章节类型 → 查询词。RAG 示例检索用它取同领域真实段落。
    example_queries: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: 该类型的典型字数占比（章节类型 → 占比）。空表示沿用全局默认。
    word_share: dict[str, float] = field(default_factory=dict)
    #: 该类型建议的学位与字数（供界面做默认值）。
    suggested_words: int = 30000

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "summary": self.summary,
            "sections": [dict(s) for s in self.default_sections],
            "suggested_words": self.suggested_words,
        }


# ---------------------------------------------------------------------------
# 各类型的方法词表（共用部分抽出来，避免七份重复）
# ---------------------------------------------------------------------------

#: 计量/统计类方法。经管、社科、公卫、教育都用得上。
_STAT_METHODS: tuple[str, ...] = (
    "双重差分", "三重差分", "DID", "PSM", "倾向得分匹配", "工具变量", "两阶段最小二乘",
    "断点回归", "合成控制", "固定效应", "随机效应", "聚类稳健", "中介效应", "调节效应",
    "结构方程", "广义矩估计", "GMM", "面板门槛", "门槛回归", "事件研究",
    "多元回归", "logit", "probit", "tobit", "生存分析", "Cox", "方差分析", "ANOVA",
    "协方差分析", "卡方检验", "t检验", "非参数检验", "因子分析", "主成分分析",
    "聚类分析", "判别分析", "路径分析", "多层线性模型", "HLM", "倾向值加权",
)

#: 实验与测量类方法。
_EXPERIMENT_METHODS: tuple[str, ...] = (
    "随机对照试验", "RCT", "对照实验", "正交试验", "响应面", "单因素实验", "析因设计",
    "扫描电镜", "SEM", "透射电镜", "TEM", "X射线衍射", "XRD", "红外光谱", "FTIR",
    "核磁共振", "NMR", "质谱", "色谱", "拉曼光谱", "热重分析", "TGA", "差示扫描量热",
    "DSC", "拉伸试验", "疲劳试验", "硬度测试", "电化学测试", "循环伏安",
    "酶联免疫吸附", "ELISA", "PCR", "Western blot", "流式细胞术", "免疫组化",
)

#: 计算与工程类方法。
_ENGINEERING_METHODS: tuple[str, ...] = (
    "有限元", "finite element", "计算流体力学", "CFD", "离散元", "分子动力学",
    "蒙特卡洛", "遗传算法", "粒子群", "蚁群算法", "模拟退火", "差分进化",
    "卷积神经网络", "CNN", "循环神经网络", "RNN", "LSTM", "Transformer",
    "支持向量机", "SVM", "随机森林", "梯度提升", "XGBoost", "聚类算法",
    "强化学习", "迁移学习", "知识图谱", "本体建模", "UML", "数字孪生",
    "微服务", "容器化", "分布式", "边缘计算", "联邦学习",
)

#: 定性与文本类方法。人文、教育、管理学常用。
_QUALITATIVE_METHODS: tuple[str, ...] = (
    "扎根理论", "案例研究", "多案例", "纵向案例", "单案例", "访谈法", "半结构化访谈",
    "焦点小组", "参与式观察", "田野调查", "民族志", "叙事研究", "现象学",
    "内容分析法", "话语分析", "文本分析", "主题分析", "德尔菲法", "专家咨询",
    "行动研究", "比较研究", "历史研究", "文献计量", "CiteSpace", "共词分析",
    "社会网络分析", "Q方法", "关键事件法",
)

#: 通用研究路径词。所有学科都可能提到，放在每个类型的词表里做底。
_COMMON_METHODS: tuple[str, ...] = (
    "问卷调查", "量表", "信度", "效度", "探索性因子分析", "验证性因子分析",
    "指标体系", "层次分析法", "AHP", "熵值法", "TOPSIS", "模糊综合评价",
    "数据包络", "DEA", "耦合协调", "灰色关联", "系统动力学",
)


def _merge(*groups: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for g in groups:
        for x in g:
            if x not in out:
                out.append(x)
    return tuple(out)


# ---------------------------------------------------------------------------
# 七种论文类型
# ---------------------------------------------------------------------------

EMPIRICAL = GenreProfile(
    id="empirical",
    name="实证研究型",
    summary="用数据检验假设。经管、社科量化研究最常见，结构为「假设—数据—回归—稳健性」。",
    suggested_words=35000,
    default_sections=(
        {"title": "绪论", "children": ["研究背景与意义", "研究内容与方法", "研究思路与结构"],
         "focus": "现实与理论背景；研究问题的提出；本文的思路与结构", "needs": "政策文件、行业统计数据"},
        {"title": "文献综述", "children": ["核心概念界定", "国外研究现状", "国内研究现状", "文献述评"],
         "focus": "既有研究的脉络、分歧与不足，推出本文的切入点", "needs": "近五年代表性研究及其结论"},
        {"title": "理论分析与研究假设", "children": ["理论基础", "作用机理分析", "研究假设的提出"],
         "focus": "理论依据、作用机制、逐条假设", "needs": "理论原始出处、机制类文献"},
        {"title": "研究设计", "children": ["数据来源与样本选择", "变量定义与测量", "模型设定"],
         "focus": "数据口径、变量测量、模型形式与选择理由", "needs": "方法原始出处、数据来源说明"},
        {"title": "实证结果与分析", "children": ["描述性统计", "基准回归结果", "稳健性检验", "异质性分析"],
         "focus": "主结果、稳健性、与预期的对照", "needs": "可对照的既有实证结论"},
        {"title": "讨论", "children": ["主要发现", "与既有研究的对照", "机制解释", "研究局限"],
         "focus": "结果说明什么、与谁一致或相反、为什么", "needs": "与本文结论可比的研究"},
        {"title": "结论与展望", "children": ["研究结论", "研究不足", "未来展望"],
         "focus": "由作者确认，系统只做正文汇总", "needs": "—"},
    ),
    method_terms=_merge(_STAT_METHODS, _COMMON_METHODS),
    #: 注意**不要把「数据」收进来**：它在"数据资产""数据分析"里就出现，
    #: 几乎每句话都会命中，于是「相关研究不足」这类空话会被判成"具体缺口"。
    #: 这条是实测踩出来的——收进来之后检查器在本该报的样本上不报了。
    #: 只留真正指向"哪里不足"的维度词。
    gap_dimensions=(
        "识别", "因果", "内生", "外生", "稳健", "异质性", "调节", "中介",
        "面板", "截面", "遗漏变量", "自选择", "反向因果", "安慰剂", "平行趋势",
        "工具变量", "样本区间", "时间跨度", "数据可得性", "数据质量", "样本选择",
    ),
    example_queries={
        "introduction": ("引言 研究背景 研究意义", "研究缺口 研究问题"),
        "literature_review": ("文献综述 研究现状", "研究结论 局限"),
        "method": ("研究设计 数据来源", "变量定义 测量", "模型设定 估计方法"),
        "results": ("实证结果 回归分析", "稳健性检验", "异质性分析"),
        "discussion": ("讨论 机制分析", "研究局限"),
        "conclusion": ("研究结论", "结论与展望"),
    },
)

CASE = GenreProfile(
    id="case",
    name="案例研究型",
    summary="深入一个或几个案例，回答「为什么」与「怎么样」。管理学、教育学、社会工作常用。",
    suggested_words=30000,
    default_sections=(
        {"title": "绪论", "children": ["研究背景与意义", "研究问题", "研究方法与案例选择"],
         "focus": "背景、问题、为什么用案例研究", "needs": "行业数据、案例企业的公开资料"},
        {"title": "文献综述与理论基础", "children": ["核心概念界定", "国内外研究现状", "理论分析框架"],
         "focus": "梳理脉络并搭出本文的分析框架", "needs": "理论与框架类文献"},
        {"title": "研究设计", "children": ["研究方法", "案例选择与资料来源", "数据分析策略"],
         "focus": "案例研究的方法论交代，写到可复现", "needs": "案例研究方法文献、资料清单"},
        {"title": "案例描述", "children": ["案例概况", "发展历程", "关键事件与数据"],
         "focus": "如实呈现事实，不急于解释", "needs": "企业年报、访谈资料、公开报道"},
        {"title": "案例分析", "children": ["基于框架的分析", "跨阶段比较", "作用机制"],
         "focus": "用框架解释案例，抽象出机制", "needs": "可对照的同类案例研究"},
        {"title": "讨论与启示", "children": ["主要发现", "与既有研究的对话", "理论与实践启示", "研究局限"],
         "focus": "发现对既有理论意味着什么", "needs": "与本文结论可比的研究"},
        {"title": "结论与展望", "children": ["研究结论", "研究不足", "未来展望"],
         "focus": "由作者确认，系统只做正文汇总", "needs": "—"},
    ),
    method_terms=_merge(_QUALITATIVE_METHODS, _COMMON_METHODS, ("三角验证", "编码", "范畴化")),
    gap_dimensions=(
        "机制", "过程", "路径", "情境", "情境化", "单案例", "多案例", "纵向", "时间维度",
        "主体", "行动者", "互动", "制度环境", "组织", "实践", "意义建构", "边界条件",
    ),
    example_queries={
        "introduction": ("案例研究 研究背景", "研究问题 案例选择"),
        "literature_review": ("文献综述 理论框架", "研究现状 述评"),
        "method": ("案例研究方法 资料来源", "数据分析 编码"),
        "results": ("案例分析 机制", "案例描述 发展历程"),
        "discussion": ("讨论 理论贡献", "研究局限"),
        "conclusion": ("研究结论 启示",),
    },
)

THEORETICAL = GenreProfile(
    id="theoretical",
    name="理论思辨型",
    summary="以论证与解释为主体。人文、法学、哲学、史学常用，没有数据章只有论证章。",
    suggested_words=30000,
    default_sections=(
        {"title": "绪论", "children": ["问题的提出", "研究意义", "研究思路与方法", "核心概念界定"],
         "focus": "问题从哪来、为什么值得追问、本文怎么论证", "needs": "政策文本、经典文献、现实案例"},
        {"title": "文献综述", "children": ["国内外研究现状", "主要争论与分歧", "既有研究的局限"],
         "focus": "按学术脉络梳理争论，而非罗列观点", "needs": "代表性论著及其核心主张"},
        {"title": "理论基础与分析框架", "children": ["理论渊源", "核心范畴", "分析框架的建构"],
         "focus": "交代理论资源并建构本文的分析框架", "needs": "理论原典、概念史文献"},
        {"title": "主体论证", "children": ["第一重论证", "第二重论证", "第三重论证"],
         "focus": "分层推进的论证，每一章解决一个子问题", "needs": "支撑各重论证的文献与材料"},
        {"title": "结论与展望", "children": ["研究结论", "研究不足", "未来展望"],
         "focus": "由作者确认，系统只做正文汇总", "needs": "—"},
    ),
    method_terms=_merge(_QUALITATIVE_METHODS, ("概念分析", "规范分析", "比较法", "法教义学", "解释学", "谱系学", "思想史")),
    gap_dimensions=(
        "概念", "范畴", "理论前提", "解释力", "适用边界", "论证", "逻辑", "价值立场",
        "规范", "正当性", "语境", "文本", "思想脉络", "范式", "预设",
    ),
    example_queries={
        "introduction": ("问题的提出 研究意义", "核心概念 界定"),
        "literature_review": ("学术争论 研究综述", "研究现状 述评"),
        "body": ("理论框架 论证", "范畴 解释"),
        "conclusion": ("研究结论 展望",),
    },
)

ENGINEERING = GenreProfile(
    id="engineering",
    name="设计开发型",
    summary="做一个东西并证明它可用。工科、信息系统、软件工程常用，主体是设计与测试。",
    suggested_words=30000,
    default_sections=(
        {"title": "绪论", "children": ["研究背景与意义", "国内外研究现状", "研究内容与技术路线"],
         "focus": "工程问题从哪来、本文要做出什么", "needs": "技术标准、行业报告、同类系统"},
        {"title": "相关理论与技术基础", "children": ["核心概念", "关键技术", "现有方案的不足"],
         "focus": "交代技术选型的依据与现有方案的局限", "needs": "技术文献、官方文档、同类工作"},
        {"title": "需求分析", "children": ["功能需求", "非功能需求", "可行性分析"],
         "focus": "需求从哪来（调研/标准）、如何验证", "needs": "调研资料、标准规范"},
        {"title": "系统设计", "children": ["总体架构", "模块设计", "数据与接口设计", "关键算法设计"],
         "focus": "设计决策与取舍理由，写到能照着实现", "needs": "设计模式、算法原始文献"},
        {"title": "实现与测试", "children": ["开发环境与工具", "核心功能实现", "测试方案", "测试结果与分析"],
         "focus": "测试指标、结果数据、与设计目标的对照", "needs": "测试标准、基准数据"},
        {"title": "总结与展望", "children": ["工作总结", "不足与改进", "未来工作"],
         "focus": "由作者确认，系统只做正文汇总", "needs": "—"},
    ),
    method_terms=_merge(_ENGINEERING_METHODS, _EXPERIMENT_METHODS, ("单元测试", "集成测试", "压力测试", "对比实验", "消融实验", "基线对比")),
    gap_dimensions=(
        "性能", "精度", "效率", "开销", "实时性", "鲁棒性", "可扩展性", "兼容性",
        "场景", "数据集", "基线", "部署", "工程化", "成本", "能耗", "时延", "吞吐",
    ),
    example_queries={
        "introduction": ("研究背景 技术现状", "研究内容 技术路线"),
        "literature_review": ("相关技术 研究现状", "现有方案 不足"),
        "method": ("系统设计 架构", "算法设计 关键实现"),
        "results": ("测试结果 性能分析", "对比实验"),
        "conclusion": ("总结与展望",),
    },
)

EXPERIMENTAL = GenreProfile(
    id="experimental",
    name="实验研究型",
    summary="以实验数据说话。理工、农、医常用，主体是材料与方法＋结果分析。",
    suggested_words=30000,
    default_sections=(
        {"title": "绪论", "children": ["研究背景与意义", "国内外研究进展", "研究内容与技术路线"],
         "focus": "科学问题是什么、为什么现在还没解决", "needs": "权威综述、标准、研究现状"},
        {"title": "材料与方法", "children": ["实验材料与试剂", "实验仪器与设备", "实验方案与流程", "数据处理与统计方法"],
         "focus": "写到他人可重复的程度：型号、剂量、条件、重复数", "needs": "仪器型号、试剂规格、方法原始文献"},
        {"title": "实验结果", "children": ["表征结果", "主要实验结果", "结果的可重复性"],
         "focus": "只报事实与数据，解释留给讨论章", "needs": "对照组的既有数据"},
        {"title": "讨论", "children": ["结果的内在规律", "与已有研究的对照", "机理分析", "研究的局限"],
         "focus": "为什么会出现这样的结果", "needs": "同类研究的可比值"},
        {"title": "结论与展望", "children": ["研究结论", "研究不足", "未来展望"],
         "focus": "由作者确认，系统只做正文汇总", "needs": "—"},
    ),
    method_terms=_merge(_EXPERIMENT_METHODS, _STAT_METHODS, ("重复实验", "对照组", "空白对照", "加标回收", "标准曲线")),
    gap_dimensions=(
        "机理", "条件", "参数", "配比", "纯度", "产率", "选择性", "稳定性", "重复性",
        "灵敏度", "检出限", "样本量", "对照", "剂量", "温度", "浓度", "尺度",
    ),
    example_queries={
        "introduction": ("研究背景 研究进展", "科学问题 研究现状"),
        "method": ("实验材料 方法", "测试表征 数据处理"),
        "results": ("实验结果 分析", "表征结果"),
        "discussion": ("讨论 机理分析", "研究局限"),
        "conclusion": ("研究结论 展望",),
    },
)

SURVEY = GenreProfile(
    id="survey",
    name="调查研究型",
    summary="以问卷或访谈描摹现状、找影响因素。教育学、公卫、社会学常用。",
    suggested_words=25000,
    default_sections=(
        {"title": "绪论", "children": ["研究背景与意义", "研究目的与问题", "研究思路与结构"],
         "focus": "现实问题的规模与制度背景，研究问题的提出", "needs": "权威统计、政策文件"},
        {"title": "文献综述", "children": ["核心概念界定", "国内外研究现状", "影响因素的相关研究"],
         "focus": "梳理既有结论与分歧，推出本文关注的因素", "needs": "实证类文献、测量工具来源"},
        {"title": "研究设计", "children": ["研究对象与抽样", "研究工具与量表", "数据收集过程", "信度与效度检验"],
         "focus": "抽样方案、量表来源、信效度指标，写到可复现", "needs": "成熟量表出处、抽样方法文献"},
        {"title": "调查结果与分析", "children": ["样本基本情况", "现状描述", "差异分析", "影响因素分析"],
         "focus": "数据说明了什么，与既有结论是否一致", "needs": "可对照的同类调查数据"},
        {"title": "讨论与建议", "children": ["主要发现", "与既有研究的对照", "对策建议", "研究局限"],
         "focus": "发现意味着什么、建议从哪条结论推出", "needs": "政策文本、干预研究"},
        {"title": "结论与展望", "children": ["研究结论", "研究不足", "未来展望"],
         "focus": "由作者确认，系统只做正文汇总", "needs": "—"},
    ),
    method_terms=_merge(_STAT_METHODS, _COMMON_METHODS, ("抽样", "分层抽样", "整群抽样", "便利抽样", "信度检验", "效度检验", "共同方法偏差")),
    gap_dimensions=(
        "样本", "抽样", "代表性", "测量工具", "量表", "信度", "效度", "普适性",
        "群体", "地区", "学段", "干预", "影响因素", "中介", "调节", "纵向",
    ),
    example_queries={
        "introduction": ("研究背景 现状调查", "研究问题 研究目的"),
        "literature_review": ("文献综述 影响因素", "研究现状 述评"),
        "method": ("问卷设计 抽样", "信度 效度检验"),
        "results": ("调查结果 差异分析", "影响因素 回归"),
        "discussion": ("讨论 建议", "研究局限"),
        "conclusion": ("研究结论 建议",),
    },
)

REVIEW = GenreProfile(
    id="review",
    name="文献综述型",
    summary="以文献为研究对象，系统梳理某个领域的进展与争论。综述类论文。",
    suggested_words=20000,
    default_sections=(
        {"title": "引言", "children": ["研究背景", "综述范围与方法", "综述的贡献"],
         "focus": "为什么现在需要一篇这样的综述", "needs": "领域权威综述、检索策略"},
        {"title": "文献检索与筛选", "children": ["检索策略", "纳入与排除标准", "文献筛选流程"],
         "focus": "可复现的检索方案与筛选结果", "needs": "PRISMA 等综述规范"},
        {"title": "研究进展", "children": ["第一条脉络", "第二条脉络", "第三条脉络"],
         "focus": "按脉络而非按时间组织，每条脉络内部要有承接与评价", "needs": "各脉络的代表性文献"},
        {"title": "争论与分歧", "children": ["主要争论焦点", "分歧的成因", "尚未解决的问题"],
         "focus": "把分歧摆出来并解释成因", "needs": "观点对立的文献"},
        {"title": "研究展望", "children": ["未来研究方向", "方法上的建议"],
         "focus": "给出具体可行的方向", "needs": "领域内的前沿工作"},
    ),
    method_terms=_merge(_QUALITATIVE_METHODS, ("系统综述", "元分析", "meta分析", "PRISMA", "文献计量", "共被引分析")),
    gap_dimensions=(
        "检索策略", "纳入标准", "文献覆盖", "时间跨度", "数据库", "语言偏倚", "发表偏倚",
        "争论焦点", "证据强度", "异质性", "可重复性", "理论整合",
    ),
    example_queries={
        "introduction": ("引言 综述范围", "研究背景 综述贡献"),
        "literature_review": ("研究进展 脉络", "研究现状 综述"),
        "body": ("争论 分歧", "研究展望"),
    },
)

GENRES: dict[str, GenreProfile] = {
    p.id: p for p in (EMPIRICAL, CASE, THEORETICAL, ENGINEERING, EXPERIMENTAL, SURVEY, REVIEW)
}

DEFAULT_GENRE = "empirical"

#: 关键词 → 类型。用于从题目里**猜**一个类型（只是给界面一个更合理的默认值）。
_GENRE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("engineering", ("系统", "设计", "实现", "开发", "平台", "算法", "模型", "架构", "网络", "app", "小程序", "软件")),
    ("experimental", ("实验", "制备", "合成", "表征", "性能", "材料", "合金", "催化剂", "细胞", "小鼠", "临床", "工艺")),
    ("survey", ("调查", "问卷", "现状", "影响因素", "抽样", "访谈", "需求分析", "素养")),
    ("case", ("案例", "个案", "以.*为例", "单案例", "多案例")),
    ("theoretical", ("理论", "思辨", "反思", "哲学", "法学", "伦理", "思想", "谱系", "阐释", "批判")),
    ("review", ("综述", "研究进展", "文献计量", "研究述评", "元分析")),
)


def get_genre(genre_id: str | None) -> GenreProfile:
    """按 id 取画像。id 为空或未知时退回默认，**不抛错**。

    不抛错的理由：类型只是"作者没说时给个起点"。用户传了一个拼错的类型名，
    应当得到一份能用的默认结构 + 一条提示，而不是一条报错。
    """
    if not genre_id:
        return GENRES[DEFAULT_GENRE]
    return GENRES.get(str(genre_id).strip().lower(), GENRES[DEFAULT_GENRE])


def guess_genre(topic: str, requirement: str = "") -> GenreProfile:
    """从题目与要求里猜一个类型。猜不出来就退回默认。"""
    import re

    text = f"{topic or ''} {requirement or ''}"
    if not text.strip():
        return GENRES[DEFAULT_GENRE]
    for gid, keys in _GENRE_HINTS:
        for k in keys:
            if re.search(k, text):
                return GENRES[gid]
    return GENRES[DEFAULT_GENRE]


def genre_choices() -> list[dict]:
    """给界面用的类型清单。"""
    return [p.as_dict() for p in GENRES.values()]


def profile_from_ctx(ctx: Any) -> GenreProfile:
    """从智能体上下文里取当前项目的论文类型。

    **检查器靠它拿词表**，这样同一套检查器在不同学科下用不同的方法名词表与缺口维度词，
    而不是硬编码一套经管词汇、在别的学科静默失效。

    任何一步取不到都退回默认画像——检查器不该因为拿不到配置就崩掉或跳过。
    """
    try:
        meta = getattr(getattr(ctx, "board", None), "project", None)
        meta = getattr(meta, "meta", None) or {}
        brief = meta.get("writing_brief") or {}
        gid = brief.get("genre") or meta.get("genre") or ""
        return get_genre(gid)
    except Exception:  # noqa: BLE001
        return GENRES[DEFAULT_GENRE]
