"""离线兜底引擎：内置学科知识库 + 随机组合，不需要联网也能出题。

这里的定位要说清楚：离线模式生成的是**题目骨架 + 文献线索 + 述评模板**，
方向、方法、数据来源都是真实的学科套路，但文献条目多为「占位引用」，
一律标注 verified=False 并提示复核。真正有观点、能直接用的文献短评，
请用联网模式（大模型）生成。

好处是：配 Key 之前也能先把界面跑通、把流程演示给同学看。
"""

from __future__ import annotations

import random
import re
from datetime import datetime

from . import validate
from .validate import DIFFICULTIES, TOPIC_TYPES

# ---------------------------------------------------------------------------
# 学科知识库
# ---------------------------------------------------------------------------
DEFAULT_DISCIPLINE = "计算机科学与技术"

DISCIPLINES: dict[str, dict] = {
    "计算机科学与技术": {
        "aliases": ["计算机", "软件工程", "人工智能", "大数据", "cs", "ai", "计算机技术"],
        "hotspots": [
            "大语言模型推理优化", "检索增强生成", "多模态表征学习", "联邦学习",
            "知识图谱补全", "边缘智能", "代码生成", "异常检测", "推荐系统",
            "图神经网络", "模型轻量化部署", "智能体协作",
        ],
        "objects": [
            "面向中小企业的高并发服务", "工业质检流水线", "高校在线教学平台",
            "开源社区代码仓库", "城市交通流量数据", "医疗问答系统",
            "跨机构数据协作场景", "移动端智能应用",
        ],
        "theories": ["注意力机制与Transformer架构", "分布式一致性理论", "统计学习理论", "信息瓶颈理论"],
        "methods": [
            "对比实验 + 消融实验", "公开数据集上的基准测试", "原型系统实现与压力测试",
            "仿真实验与复杂度分析", "真实业务数据上的离线评测",
        ],
        "data": [
            "公开基准数据集（如 GLUE、MS MARCO、MIMIC 等）", "GitHub 开源仓库",
            "平台脱敏日志", "高校教学平台行为数据", "企业生产环境运行数据",
        ],
        "snippets": [
            "以预训练语言模型为代表的技术路线已在多个任务上超过传统方法，但推理成本与领域适应性仍是主要瓶颈。",
            "相关研究多集中在公开基准上的指标提升，对真实业务约束（时延、成本、隐私）的讨论明显不足。",
            "联邦学习类工作普遍假设参与方数据同分布，非独立同分布场景下的性能衰减缺少统一的评估口径。",
        ],
    },
    "教育学": {
        "aliases": ["教育", "教育技术", "教育技术学", "学科教学", "教师教育", "高等教育"],
        "hotspots": [
            "生成式人工智能辅助教学", "学习分析", "项目式学习", "深度学习课堂",
            "乡村教育数字化", "教师数字素养", "深度学习评价", "混合式教学",
            "学习动机与自我效能", "教育公平",
        ],
        "objects": [
            "县域普通高中", "中职学校实训课堂", "高校通识课程", "乡村小学",
            "新入职教师群体", "在线学习平台用户", "课后服务场景",
        ],
        "theories": ["建构主义学习理论", "自我决定理论", "技术接受模型（TAM）", "最近发展区理论"],
        "methods": [
            "准实验设计 + 前后测", "问卷调查 + 结构方程模型", "课堂观察 + 半结构化访谈",
            "教育数据挖掘", "行动研究",
        ],
        "data": ["课堂录像与观察记录", "学习平台日志", "学生问卷与访谈", "区域教育统计数据", "教师教研档案"],
        "snippets": [
            "技术进入课堂的早期研究普遍持「技术乐观主义」，近年来的实证结论则更强调教师中介作用与教学法适配。",
            "学习分析研究多以成绩为结果变量，对学习过程与非认知结果的刻画相对薄弱。",
            "区域推进类研究常止步于经验总结，缺少可比的效应量证据。",
        ],
    },
    "工商管理": {
        "aliases": ["管理", "企业管理", "市场营销", "人力资源", "mba", "会计", "财务管理", "组织管理"],
        "hotspots": [
            "数字化转型", "平台经济治理", "人工智能赋能管理决策", "组织韧性",
            "ESG 表现", "零工经济下的劳动关系", "消费者隐私关切", "供应链协同",
            "员工敬业度", "服务补救",
        ],
        "objects": ["制造业中小企业", "跨境电商企业", "平台型互联网企业", "零售连锁门店", "知识型员工团队"],
        "theories": ["资源基础观", "动态能力理论", "计划行为理论", "社会交换理论", "委托代理理论"],
        "methods": [
            "问卷调研 + 回归分析", "多案例比较研究", "面板数据固定效应模型",
            "结构方程模型与中介效应检验", "纵向跟踪访谈",
        ],
        "data": ["企业年报与公开披露数据", "上市公司数据库（如 CSMAR、Wind）", "企业管理层访谈", "员工问卷", "平台交易数据"],
        "snippets": [
            "数字化转型的绩效影响结论差异较大，原因之一是测量方式与行业情境不一致。",
            "多数研究关注大企业样本，中小企业「不愿转、不会转」的机制仍缺少经验证据。",
            "平台治理研究多采用双边市场框架，对算法权力与劳动过程的讨论仍显不足。",
        ],
    },
    "临床医学": {
        "aliases": ["医学", "临床", "护理", "公共卫生", "药学", "中医", "口腔", "健康管理"],
        "hotspots": [
            "慢病全程管理", "互联网医院服务模式", "人工智能辅助诊断", "老年综合评估",
            "术后快速康复（ERAS）", "患者报告结局（PRO）", "用药依从性", "安宁疗护",
            "医联体分级诊疗",
        ],
        "objects": ["2 型糖尿病患者", "社区老年高血压人群", "肿瘤术后患者", "基层卫生院门诊人群", "ICU 转出患者"],
        "theories": ["健康信念模型", "自我管理理论", "奥马哈系统", "跨理论模型"],
        "methods": [
            "随机对照试验（RCT）", "回顾性队列研究", "倾向性评分匹配", "混合方法研究",
            "系统评价与 Meta 分析",
        ],
        "data": ["医院电子病历（HIS/EMR）", "随访登记数据库", "公开医学数据库（如 MIMIC-IV、NHANES）", "患者问卷与量表评分"],
        "snippets": [
            "同类干预研究结论不完全一致，干预强度、随访时长与结局指标定义差异是主要解释。",
            "以生理指标为唯一结局的研究较多，对生活质量与患者体验的关注不足。",
            "真实世界数据研究正在增加，但混杂控制与缺失值处理仍不规范。",
        ],
    },
    "工程技术": {
        "aliases": ["机械", "电气", "自动化", "土木", "材料", "能源", "环境工程", "车辆工程", "电子"],
        "hotspots": [
            "数字孪生", "预测性维护", "新能源并网", "增材制造", "轻量化设计",
            "智能运维", "低碳工艺", "结构健康监测", "能量管理策略",
        ],
        "objects": ["风电齿轮箱", "锂离子动力电池组", "城市轨道交通车辆", "装配式建筑节点", "工业机器人关节"],
        "theories": ["疲劳损伤累积理论", "多物理场耦合模型", "可靠性理论", "最优控制理论"],
        "methods": [
            "台架试验 + 仿真对比", "有限元与多体动力学联合仿真", "正交试验与响应面优化",
            "加速寿命试验", "现场数据驱动的退化建模",
        ],
        "data": ["试验台采集的振动/温度信号", "设备 SCADA 运行数据", "材料性能测试报告", "公开故障数据集"],
        "snippets": [
            "现有寿命预测模型多在实验室工况下验证，跨工况泛化能力缺乏系统评估。",
            "仿真与试验结果常存在可观的系统偏差，标定方法本身缺少不确定性量化。",
            "工程类研究普遍重视性能指标，对全寿命周期成本与运维可行性的讨论偏少。",
        ],
    },
    "法学": {
        "aliases": ["法律", "民商法", "刑法", "宪法", "国际法", "法律硕士", "知识产权"],
        "hotspots": [
            "生成式人工智能的著作权归属", "算法决策的透明性义务", "个人信息跨境流动",
            "平台责任的边界", "数据财产权", "自动驾驶侵权责任", "未成年人网络保护",
            "刑事合规",
        ],
        "objects": ["司法裁判文书", "平台服务协议与算法说明", "监管处罚决定", "企业数据合规实践", "立法与司法解释文本"],
        "theories": ["利益平衡理论", "风险规制理论", "法教义学体系", "程序正义理论"],
        "methods": ["规范分析 + 案例分析", "裁判文书的大样本实证研究", "比较法研究", "访谈与实地调研"],
        "data": ["中国裁判文书网案例", "北大法宝等数据库", "监管机构公开文件", "域外立法与判例"],
        "snippets": [
            "学界对新兴技术引发的责任分配已形成若干学说，但司法实践中的裁判说理仍显模糊。",
            "现有讨论多停留于应然层面，缺少对裁判文书的大样本实证检验。",
            "域外经验被广泛援引，但制度移植的语境差异常被简化处理。",
        ],
    },
    "新闻传播学": {
        "aliases": ["新闻", "传播", "传播学", "新媒体", "广告", "网络与新媒体"],
        "hotspots": [
            "算法推荐与信息茧房", "生成式人工智能内容标识", "虚假信息治理",
            "平台化新闻生产", "情感传播", "短视频使用与心理影响", "国际传播效能",
            "健康传播",
        ],
        "objects": ["主流媒体客户端", "短视频平台内容生态", "社交平台热点事件", "县级融媒体中心", "Z 世代受众"],
        "theories": ["使用与满足理论", "框架理论", "沉默的螺旋", "媒介化理论", "涵化理论"],
        "methods": ["内容分析法", "计算传播学方法（文本挖掘）", "深度访谈与焦点小组", "网络问卷调查"],
        "data": ["平台公开内容与评论", "平台 API 抓取数据", "受众问卷", "从业者访谈"],
        "snippets": [
            "「信息茧房」的实证结论并不一致，算法效应与用户自选择效应难以分离。",
            "内容分析类研究样本周期普遍偏短，难以刻画长期变化。",
            "对平台机制的黑箱约束讨论较多，对用户能动性的经验研究仍显不足。",
        ],
    },
    "公共管理": {
        "aliases": ["行政管理", "公共政策", "mpa", "社会治理", "应急管理", "社会保障"],
        "hotspots": [
            "基层治理数字化转型", "政务服务「一网通办」", "基层减负", "应急协同",
            "公共服务均等化", "网格化治理", "社会组织参与", "政策执行偏差",
        ],
        "objects": ["区县政务服务中心", "街道社区网格", "省级政务平台", "乡镇基层干部", "社会组织项目"],
        "theories": ["街头官僚理论", "整体性治理", "政策执行理论", "协同治理理论"],
        "methods": ["多案例比较", "问卷调查 + 访谈", "政策文本分析", "准实验评估（如 DID）"],
        "data": ["政府公开数据与年报", "政务平台运行数据", "干部与居民访谈", "政策文件汇编"],
        "snippets": [
            "数字治理研究多聚焦平台建设成效，对基层执行者的实际负担关注不足。",
            "政策评估类研究常用横截面数据，因果识别强度有限。",
            "案例研究普遍来自东部发达地区，中西部与农村情境的经验证据稀缺。",
        ],
    },
    "心理学": {
        "aliases": ["应用心理", "心理", "心理健康", "心理咨询", "认知心理", "发展与教育心理"],
        "hotspots": [
            "青少年抑郁与网络使用", "正念干预", "心理韧性", "情绪调节策略",
            "职场心理资本", "社会支持与孤独感", "考试焦虑", "亲子沟通",
        ],
        "objects": ["大学生群体", "初中生群体", "新入职员工", "慢性病患者照顾者", "留守儿童"],
        "theories": ["认知行为理论", "情绪调节过程模型", "依恋理论", "压力-应对理论", "社会支持缓冲模型"],
        "methods": ["问卷调查 + 中介/调节效应检验", "随机对照干预实验", "经验取样法（ESM）", "纵向追踪设计"],
        "data": ["标准化量表评分", "行为实验数据", "访谈与日志", "手机被动感知数据"],
        "snippets": [
            "横截面中介模型被大量使用，但纵向证据仍不足，因果方向存在争议。",
            "效应量普遍偏小，样本多来自方便取样，外部效度受限。",
            "干预研究对脱落率与依从性的报告不够透明。",
        ],
    },
    "经济学": {
        "aliases": ["金融", "国际贸易", "产业经济", "区域经济", "财政", "统计", "数量经济"],
        "hotspots": [
            "数字经济与就业结构", "共同富裕与收入分配", "绿色金融", "产业政策效果评估",
            "数字化转型与企业生产率", "人口老龄化与消费", "跨境电商", "区域协调发展",
        ],
        "objects": ["制造业上市公司", "地级市面板数据", "县域经济单元", "家庭住户", "中小出口企业"],
        "theories": ["内生增长理论", "交易成本理论", "要素禀赋理论", "激励相容理论"],
        "methods": ["双重差分（DID）", "工具变量法", "断点回归", "面板固定效应", "投入产出分析"],
        "data": ["中国家庭追踪调查（CFPS）等微观数据库", "城市统计年鉴", "上市公司数据库", "海关进出口数据"],
        "snippets": [
            "政策评估类文献高度依赖 DID，平行趋势假设的检验常被弱化处理。",
            "机制分析多停留在中介回归，对作用渠道的识别不够干净。",
            "宏观层面结论与微观企业证据之间存在明显张力。",
        ],
    },
    "外国语言文学": {
        "aliases": ["英语", "翻译", "外语", "日语", "语言学", "汉语言文学", "文学"],
        "hotspots": [
            "机器翻译质量评估", "译者主体性", "多模态话语分析", "中华文化外译",
            "二语写作反馈", "外语学习焦虑", "课程思政融入外语教学", "语料库批评话语分析",
        ],
        "objects": ["政论文本外译", "学术论文摘要语料", "高校英语写作课堂", "短视频字幕翻译", "文学译本"],
        "theories": ["功能对等理论", "生态翻译学", "系统功能语言学", "社会文化理论"],
        "methods": ["语料库对比分析", "个案跟踪研究", "问卷调查 + 访谈", "课堂实验"],
        "data": ["自建平行语料库", "公开翻译语料库", "学生作文语料", "译者访谈"],
        "snippets": [
            "翻译质量评估多依赖人工评分，评分者信度报告不足。",
            "语料库研究以词频与搭配统计为主，与翻译策略的解释衔接不够。",
            "教学类实证研究样本量偏小，干预周期短，效果难以稳定复现。",
        ],
    },
    "艺术与设计": {
        "aliases": ["设计", "艺术", "视觉传达", "工业设计", "环境设计", "数字媒体艺术", "音乐", "美术"],
        "hotspots": [
            "生成式人工智能辅助设计", "非遗活化设计", "适老化交互设计",
            "城市更新中的公共艺术", "国潮视觉语言", "用户体验与可用性", "文化遗产数字化",
        ],
        "objects": ["博物馆数字展陈", "老旧社区公共空间", "移动应用界面", "文创产品", "非遗手工艺品类"],
        "theories": ["情感化设计三层次理论", "可供性理论", "格式塔视觉原理", "参与式设计"],
        "methods": ["设计实践 + 用户测试", "扎根理论访谈", "眼动/行为实验", "案例比较与作品分析"],
        "data": ["用户测试与可用性数据", "设计作品样本", "创作者与用户访谈", "非遗档案资料"],
        "snippets": [
            "设计类研究常以作品展示替代效果验证，用户证据链偏弱。",
            "文化类设计研究对「文化转译」的判定标准主观性较强。",
            "技术工具更新快，研究结论的时效性面临挑战。",
        ],
    },
}

# 通用题目模板：{hotspot} 研究热点，{object} 研究对象，{method} 方法，{theory} 理论
UNIVERSAL_PATTERNS = [
    "{hotspot}背景下{object}的{aspect}研究",
    "面向{object}的{hotspot}关键问题与实证检验",
    "{object}中{hotspot}的影响机制：基于{method}的分析",
    "{theory}视角下{object}{hotspot}的作用路径研究",
    "{object}{hotspot}的现状、问题与优化策略研究",
    "{hotspot}对{object}{aspect}的影响：一项基于{method}的研究",
    "基于{method}的{object}{hotspot}效果评估",
    "{object}{hotspot}的实践困境与改进路径——基于{method}的考察",
    "{hotspot}赋能{object}的机制、边界与风险研究",
    "{object}应用{hotspot}的适配性与{aspect}评价研究",
]

ASPECTS = [
    "应用效果与优化路径", "影响因素与作用机制", "风险识别与治理策略",
    "能力建设与支持体系", "评价指标体系构建", "实施障碍与推进策略",
    "用户体验与满意度", "绩效表现与改进空间",
]

LEVEL_BY_DIFFICULTY = {
    "偏易（可本科）": ("本科毕业论文", 1),
    "适中（可硕士）": ("硕士学位论文", 2),
    "偏难（可博士/核心期刊）": ("博士学位论文 / 核心期刊投稿", 3),
}

# 只在离线模式下使用的占位引用模板
LITERATURE_TEMPLATES = [
    ("{author}. {topic}[J]. {journal}, {year}({issue}): {pages}.", "较早对该问题进行系统讨论，提出了可操作的分析框架，被后续研究广泛引用。", "研究情境较早，结论在新技术条件下的适用性有待重新检验。"),
    ("{author}. {topic}[D]. {city}: {university}, {year}.", "以扎实的一手资料刻画了现实困境，为后续量化研究提供了问题清单。", "样本集中于单一地区，结论的推广需要更多证据。"),
    ("{author}. {topic}[J]. {journal}, {year}({issue}): {pages}.", "采用规范的实证设计检验了核心假设，报告了清晰的效应量。", "未充分处理内生性问题，因果解释需要谨慎。"),
    ("{author}. {topic}[J]. {journal}, {year}({issue}): {pages}.", "把该议题与{hotspot}联系起来，拓展了研究边界。", "测量工具尚不成熟，变量效度存在争议。"),
    ("{author}. {topic}[J]. {journal}, {year}({issue}): {pages}.", "提供了跨学科视角，对政策与管理实践有直接参考价值。", "以描述性分析为主，机制层面的检验相对薄弱。"),
]

JOURNALS = ["中国软科学", "管理世界", "教育研究", "中国管理科学", "现代传播", "中国行政管理", "科研管理", "情报杂志"]
UNIVERSITIES = ["北京：北京大学", "上海：华东师范大学", "武汉：华中科技大学", "广州：中山大学", "杭州：浙江大学"]
SURNAMES = ["王", "李", "张", "刘", "陈", "杨", "黄", "周", "吴", "徐", "孙", "马"]


def _seed_int(seed) -> int | None:
    if seed in (None, ""):
        return None
    try:
        return int(str(seed).strip())
    except ValueError:
        return abs(hash(str(seed))) % (2**31)


def _fingerprint(title: str) -> str:
    return re.sub(r"[\s，。、：；（）()《》\-—的与及和]", "", title)


def _resolve_discipline(discipline: str) -> tuple[str, dict]:
    name = (discipline or DEFAULT_DISCIPLINE).strip()
    if name in DISCIPLINES:
        return name, DISCIPLINES[name]
    lowered = name.lower()
    for key, value in DISCIPLINES.items():
        if key in name or (lowered and lowered in [a.lower() for a in value["aliases"]]):
            return key, value
        for alias in value["aliases"]:
            if alias.lower() in lowered:
                return key, value
    return name or DEFAULT_DISCIPLINE, DISCIPLINES[DEFAULT_DISCIPLINE]


def _fill_reference(rng: random.Random, hotspot: str, object_: str) -> tuple[str, str, str]:
    citation, contribution, critique = rng.choice(LITERATURE_TEMPLATES)
    citation = citation.format(
        author=f"{rng.choice(SURNAMES)}{rng.choice('某某')}等",
        topic=f"{object_}{hotspot}研究",
        journal=rng.choice(JOURNALS),
        year=rng.randint(2018, 2025),
        issue=rng.randint(1, 12),
        pages=f"{rng.randint(1, 40)}-{rng.randint(41, 96)}",
        city=rng.choice(UNIVERSITIES).split("：")[0],
        university=rng.choice(UNIVERSITIES).split("：")[1],
    )
    return citation + "（待核实）", contribution.replace("{hotspot}", hotspot), critique


def generate(
    discipline: str = DEFAULT_DISCIPLINE,
    direction: str = "",
    level: str = "硕士学位论文",
    topic_types: list[str] | None = None,
    count: int = 6,
    keywords: str = "",
    methodology: str = "",
    seed=None,
    avoid: list[str] | None = None,
    **_: object,
) -> dict:
    """离线生成选题。返回结构与联网模式完全一致。"""
    count = max(1, min(int(count or 6), 30))
    seed_value = _seed_int(seed)
    rng = random.Random(seed_value if seed_value is not None else random.randrange(2**31))
    name, bank = _resolve_discipline(discipline)
    types = topic_types or []

    avoid_set = {_fingerprint(item) for item in (avoid or [])}
    topics: list[dict] = []
    seen: set[str] = set()
    guard = 0
    while len(topics) < count and guard < count * 60:
        guard += 1
        hotspot = rng.choice(bank["hotspots"])
        object_ = rng.choice(bank["objects"])
        theory = rng.choice(bank["theories"])
        method = rng.choice([methodology] if methodology else bank["methods"])
        data_source = rng.choice(bank["data"])
        aspect = rng.choice(ASPECTS)
        pattern = rng.choice(UNIVERSAL_PATTERNS)
        title = pattern.format(hotspot=hotspot, object=object_, method=method.split("+")[0].strip(), theory=theory, aspect=aspect)
        if direction and rng.random() < 0.6:
            title = f"{direction.strip()}：{title}"
        key = _fingerprint(title)
        if key in seen or key in avoid_set:
            continue
        seen.add(key)

        topic_type = rng.choice(types) if types else rng.choice(list(TOPIC_TYPES))
        difficulty = None
        for label, (level_name, _rank) in LEVEL_BY_DIFFICULTY.items():
            if level_name in (level or ""):
                difficulty = label
                break
        difficulty = difficulty or DIFFICULTIES[1]

        contribution_pool = bank["snippets"]
        literature = []
        for _i in range(4):
            citation, contribution, critique = _fill_reference(rng, hotspot, object_)
            literature.append({"citation": citation, "contribution": contribution, "critique": critique, "verified": False})

        commentary = (
            f"围绕「{hotspot}」这一方向，既有研究大致沿三条线索展开：一是概念与框架的建构，"
            f"二是以{rng.choice(bank['methods'])}为代表的实证检验，三是面向{object_}的落地应用。"
            f"{rng.choice(contribution_pool)}"
            f"分歧主要集中在测量口径、样本情境与因果识别策略上，"
            f"而{aspect}恰是现有文献覆盖最薄的一环。"
            f"本选题若能在数据上做到{data_source}的可得性，并用{method}把机制说清楚，"
            f"就有条件在既有结论之外提供新的经验证据。"
        )

        references = [item["citation"] for item in literature]
        for _i in range(2):
            citation, _c, _k = _fill_reference(rng, hotspot, object_)
            references.append(citation)

        topics.append({
            "title": title,
            "english_title": "",
            "type": topic_type,
            "difficulty": difficulty,
            "summary": f"以{object_}为对象，用{method}考察{hotspot}对{aspect}的影响，为实践改进提供依据。",
            "research_questions": [
                f"{object_}在{hotspot}方面的真实现状如何？",
                f"{hotspot}通过什么机制影响{aspect}？",
                f"在什么条件下这种影响更强或更弱？",
            ],
            "theory": theory,
            "methodology": method,
            "data_sources": data_source,
            "innovations": f"把{theory}引入{hotspot}情境，并将{aspect}作为结果变量加以检验，弥补现有研究重现象描述、轻机制识别的不足。",
            "feasibility": f"数据可通过{data_source}获得，工作量与{level or '硕士学位论文'}的周期基本匹配；建议先做小样本预调研确认数据可得性。",
            "risks": "常见风险是把相关性当成因果、样本代表性不足、变量测量效度缺乏检验；建议在设计阶段就固定测量工具与稳健性检验方案。",
            "keywords": [hotspot, object_.replace("面向", "").replace("的", ""), aspect.split("与")[0], name],
            "brief": {
                "background": (
                    f"{hotspot}近年在{name}领域持续升温。" + rng.choice(contribution_pool) +
                    f"与此同时，{object_}在实践中暴露出{aspect}方面的新问题，"
                    f"既有理论解释力有限。本选题正是从这一张力出发，"
                    f"试图在{theory}的框架内给出可检验的回答。"
                ),
                "literature": literature,
                "commentary": commentary,
                "gap": f"现有研究对{aspect}缺乏系统证据，且很少同时交代{methodology or method}的适用边界，这正是本选题的切入位置。",
                "references": references,
            },
        })

    result = validate.normalize(
        {"topics": topics, "notice": (
            "当前为离线内置知识库模式：题目骨架、研究方法和数据来源均按学科套路组合生成，"
            "可直接作为选题灵感；但文献条目为占位引用（已标注「待核实」），"
            "请务必用知网/万方/Web of Science 核对后再写进开题报告。"
            "需要真实的文献短评，请在设置中配置大模型 API Key 后使用联网模式。"
        )},
        discipline=name,
        requested=count,
        mode="offline",
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        model="内置离线知识库",
        notice_kind="warning",
    )
    return result


def list_disciplines() -> list[dict]:
    return [
        {
            "name": key,
            "aliases": value["aliases"],
            "hotspots": value["hotspots"][:8],
            "sample": UNIVERSAL_PATTERNS[0].format(
                hotspot=value["hotspots"][0], object=value["objects"][0], aspect=ASPECTS[0],
                method=value["methods"][0].split("+")[0].strip(), theory=value["theories"][0],
            ),
        }
        for key, value in DISCIPLINES.items()
    ]
