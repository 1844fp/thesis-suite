"""写作质量检查器：把 `app/writing` 里的要求变成可检验的判定。

## 为什么这一层是这套助手的关键

只把"要按脉络组织综述""禁止一篇论文半句话"写进提示词，等于给模型一个**愿望**：
它可能照做，也可能不照做，而且**没有任何机制能发现它没照做**。
所以每条要求都配一个检查器，写完就能报出"哪一段、哪一句没达到、实测值是多少"。

## 判定口径的两个原则

1. **能报出数字与位置。** 不说"综述不够连贯"，而说"第 3 段首句直接以文献引注
   开头，与上段无衔接成分"。作者据此能直接改。
2. **宁可漏报也不误报。** 这些是**写作建议**而非硬性规则，误报会让作者
   连同正确的提示一起忽略。所以每个检查器的阈值都取保守值，且把实测值一并给出，
   让作者自己判断。

## 与 `app/checks/` 里那 20 个检查器的分工

那 20 个检查**论文的合规性**（引用是否真实、编号是否连续、格式是否规范）；
这里的检查**写作质量**（论证是否充分、结构是否推进）。两者都产出框架的 `Issue`，
所以能一起进入自我更正循环。
"""

from __future__ import annotations

import re
import statistics
from typing import Any

from ..framework.artifact import Issue, Severity
from ..framework.registry import verifier

# ---------------------------------------------------------------------------
# 文本切分工具
# ---------------------------------------------------------------------------

#: 句末标点。中文句号、问号、叹号、分号（分号在长句里常充当句界）。
_SENT_END = re.compile(r"(?<=[。！？])")
_CJK = re.compile(r"[\u4e00-\u9fff]")

#: 作者（年份）式引注。三种形态：
#:   中文姓名（年份）          —— 李四（2021）
#:   西文姓氏（年份）          —— Jones（2020）
#:   多个作者（年份）          —— 李四和王五（2021）、Li et al.（2020）
#: 刻意不含"数据资产（2020）"这类非人名形态：中文姓名限 2~4 字且首字为常见姓，
#: 否则会把术语误判成人名。
_SURNAME = (
    "李王张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董潘袁蔡蒋余杜叶程魏苏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向常"
)
_AUTHOR_YEAR = re.compile(
    rf"(?:[{_SURNAME}][\u4e00-\u9fff]{{1,3}}|[A-Z][a-zA-Z\-]{{1,20}})"
    rf"(?:\s*(?:和|与|、|,|&)\s*(?:[{_SURNAME}][\u4e00-\u9fff]{{1,3}}|[A-Z][a-zA-Z\-]{{1,20}}))?"
    rf"(?:\s*等)?"
    rf"\s*[（(]\s*(?:19|20)\d{{2}}[a-z]?\s*[)）]"
)

#: 数字型引注 [1] [1,2] [1-3]
_NUM_CITE = re.compile(r"[\[［]\s*\d{1,3}(?:\s*[-–~,，、]\s*\d{1,3})*\s*[\]］]")

#: 段首衔接成分：逻辑连接词，或指代前文的短语。
#: 用于判断"本段是否承接上段"，而不是硬邦邦地另起一个文献。
_COHESION_HEAD = re.compile(
    r"^(?:然而|但是|但|不过|因此|因而|所以|由此|据此|在此基础上|在此背景下|"
    r"与此相对|与此不同|相反|此外|另外|同时|进一步|进而|不仅|不仅如|"
    r"总之|综上|概言之|换言之|这意味着|这说明|可见|值得注意的是|"
    r"就.{0,12}而言|在.{0,12}(?:方面|层面|维度|视角)|"
    r"上述|前述|这一|该类|该类研究|已有研究|既有研究|相关研究|"
    r"首先|其次|再次|最后|其一|其二|其三)"
)

#: 分析性表述：出现这些，说明段落在"解释"而不是"罗列"。
_ANALYTIC = re.compile(
    r"这表明|这说明|说明|意味着|原因在于|其原因|根源在于|机制|机理|"
    r"因此|因而|据此|可以认为|由此可见|究其原因|本质上|实质上是|反映了|"
    r"可能的解释|一个解释是|这解释了|与前文|与此相关"
)

#: 强断言词：出现这些且段落无任何依据，属"无依据的断言"。
#:
#: **刻意不含「有效」。** 实测踩过：加上它之后在真论文上命中 3 条，逐条核对
#: 全是误报——「缺乏**有效**衔接」「缺乏**有效**对话」「验证其**有效**性」，
#: 这里的「有效」是形容词或名词，根本不是断言。中文里「有效」的形容词用法
#: 远多于断言用法，收进来只会制造噪音，而**误报会让作者连同真问题一起忽略**。
_STRONG_CLAIM = re.compile(
    r"显著(?:提升|提高|改善|增强|促进|降低|影响|增加)|"
    r"明显(?:提升|改善|提高|增加)|大幅(?:提升|提高|增加)|"
    r"至关重要|关键作用|决定性作用|充分说明|无可争议|"
    r"必然(?:会|能够|导致)|一定能够|已经证明"
)

#: 依据标记：数字、图表引用、文献引注。
_EVIDENCE = re.compile(
    rf"\d+(?:\.\d+)?\s*%|{_NUM_CITE.pattern}|{_AUTHOR_YEAR.pattern}|"
    r"图\s*\d+|表\s*\d+|第\s*\d+\s*章|见附录|\bp\s*[<>=]\s*0\.\d+"
)

#: 空转起手式：不含信息的套话开头。
_EMPTY_OPENING = re.compile(
    r"^(?:随着[^，。]{0,30}的?(?:快速|不断|持续|迅猛)?(?:发展|推进|深入|普及)|"
    r"在当今(?:时代|社会|经济)|当今(?:时代|社会)|近年来|长期以来|"
    r"众所周知|不言而喻|毋庸置疑|众所周知的是)"
)

#: 缺口标记
_GAP_MARK = re.compile(
    r"尚未|未能|缺乏|缺少|不足|未(?:能)?回答|有待|亟待|亟需|存在争议|"
    r"研究空白|尚未形成|仍未|没有被|缺乏对|忽视了|留下了"
)

#: 缺口维度词（用来判断缺口是否"具体到可检验"）。
#:
#: 这里只放**跨学科通用**的维度词。学科专属的维度词（金融的"估值/折现率/收益分割"、
#: 工科的"性能/时延/吞吐"……）放在 ``app/writing/genres.py`` 的类型画像里，
#: 由 ``_gap_dimensions(ctx)`` 按当前论文类型取。
#:
#: **为什么不直接把所有学科的词并在一起**：并集会让"相关研究不足"这类空话轻易命中
#: 某个学科的维度词，于是检查器在最该报的时候不报了。词表必须跟着论文类型走。
#:
#: **刻意不含「数据」**：这个词在"数据资产""数据分析"里就出现，几乎所有句子都会命中，
#: 于是任何模糊缺口都会被误判成"具体"。实测踩过：把「相关研究不足」判成具体缺口，
#: 检查器形同失效。留下的都是真正指向"哪里不足"的词。
_GAP_DIMENSION_BASE = re.compile(
    r"方法|模型|框架|样本|案例|机制|机理|路径|衔接|适用性|适用条件|"
    r"参数|口径|识别|度量|测度|因果|内生|稳健|边界|时点|"
    r"规模|质量|权属|理论|概念|范畴|过程|情境|证据|变量|指标"
)


def _gap_dimensions(ctx: Any):
    """当前论文类型下的缺口维度词表（通用词 + 该类型专属词）。"""
    from ..writing.genres import profile_from_ctx

    extra = "|".join(re.escape(x) for x in profile_from_ctx(ctx).gap_dimensions)
    if not extra:
        return _GAP_DIMENSION_BASE
    return re.compile(_GAP_DIMENSION_BASE.pattern + "|" + extra)


def _method_terms(ctx: Any) -> set[str]:
    """当前论文类型下的方法名词表。

    用途是"结果章是否引入了研究设计章未交代的方法"。词表跟着类型走，
    才能在人文学科（扎根理论/文本分析）与工科（有限元/消融实验）里都有效。
    """
    from ..writing.genres import profile_from_ctx

    return set(profile_from_ctx(ctx).method_terms)

#: 模板腔连接词（纯结构标记）
_TEMPLATE = re.compile(
    r"综上所述|值得注意的是|总而言之|总之，|不言而喻|众所周知|"
    r"毋庸置疑|不难看出|显而易见|毫无疑问|需要指出的是|"
    r"不仅要.{0,20}还要|既.{0,15}又.{0,15}"
)


def split_sentences(paragraph: str) -> list[str]:
    """按中文句末标点切句。空段返回空列表。"""
    t = re.sub(r"\s+", "", paragraph or "")
    if not t:
        return []
    parts = [p for p in _SENT_END.split(t) if p.strip()]
    return parts


def split_paragraphs(text: str) -> list[str]:
    """按换行切段。

    **单换行也算分段。** 这一点是实测踩出来的：真实论文的段落就是用单换行隔开的
    （Word 导出的纯文本、以及 `read_docx` 的输出都是每段一行）。早期版本按空行切，
    于是把整篇合并成一段——段落级检查（是否有分析、论证是否推进）在真实文本上
    **全部失效**，而单测用空行样本时又是通过的，**测试绿而功能不通**。

    所以：先把连续空行压成一个换行，再按换行切。
    """
    t = re.sub(r"[ \t]*\r?\n[ \t]*", "\n", text or "")
    blocks = [re.sub(r"[ \t]+", " ", b).strip() for b in t.split("\n")]
    return [b for b in blocks if b]


#: 段落开始处**不是可论证的散文**的形态。
#:
#: 这个过滤器是被真实误报逼出来的：在真论文上跑第一版时，
#: `check_lit_coherence` 报出「连续 143 段以文献引注开头」——那 143 段是**参考文献表**；
#: `check_body_progression` 报出「连续 55 段句数均为 1」——那是**结构框架目录与进度计划**。
#: 条目、清单、标题行天然是"一句一段、以编号开头"，把它们纳入论证质量的统计
#: 只会产出噪音，而噪音会让作者连同真问题一起忽略。
_NOT_PROSE = re.compile(
    r"^(?:"
    r"\[\s*\d{1,3}\s*\]"          # 参考文献条目 [1]
    r"|［\s*\d{1,3}\s*］"          # 全角方括号条目
    r"|\d{1,3}\s*[.、]\s*"         # 编号清单 1. / 1、
    r"|\(?\s*\d{1,3}\s*\)"         # (1)
    r"|第\s*[一二三四五六七八九十百\d]+\s*[章节篇]"  # 章标题
    r"|\d+(?:\.\d+)+\s*"           # 小节编号 1.1 / 4.3.2
    r"|[一二三四五六七八九十]{1,3}\s*[、.]"        # 一、 / 二.
    r"|[（(]\s*[一二三四五六七八九十]{1,3}\s*[)）]"  # （一）
    r"|\d{4}\s*年\s*\d{1,2}\s*月"   # 进度计划 2026年09月
    r"|摘\s*要|关键词|参考文献|致\s*谢|附\s*录|目\s*录"
    r")"
)


def is_prose_paragraph(para: str, *, min_chars: int = 0) -> bool:
    """该段是否为"可评估论证质量"的散文段。

    排除：参考文献条目、编号清单、节标题、进度计划、摘要/关键词/目录等。
    保留：真正的论述段落。短于 `min_chars` 的一并排除（短段信息量不足，判定不可靠）。
    """
    p = (para or "").strip()
    if not p or (min_chars and len(p) < min_chars):
        return False
    if _NOT_PROSE.match(p):
        return False
    # 中文字占比过低 → 英文条目、公式、纯数字行。
    #
    # 门槛取 0.25 而不是 0.5：**数据型中文段落**（"2021年为183.14亿元，2022年为…"）
    # 的中文字占比可能只有 0.34，但它正是"只摆数据不分析"这一检查的目标对象。
    # 用 0.5 会把这类段落整段过滤掉，于是检查器对最该抓的情形视而不见——
    # 实测踩过：255 字的数据段因为占比 0.34 被判为非散文。
    if len(_CJK.findall(p)) / max(len(p), 1) < 0.25:
        return False
    return True


def prose_paragraphs(text: str, *, min_chars: int = 0) -> list[tuple[int, str]]:
    """返回 `(原段序号, 段落文本)`，只含可评估的散文段。"""
    out: list[tuple[int, str]] = []
    for i, p in enumerate(split_paragraphs(text)):
        if is_prose_paragraph(p, min_chars=min_chars):
            out.append((i, p))
    return out


def strip_references(text: str) -> str:
    """截掉参考文献区及其之后的内容。

    引用格式的检查必须排除文献表本身：`[1] 张三（2023）……` 既是条目编号、
    又含"作者（年份）"，几乎会被每一条检查器误判。
    """
    from ..agents.references_extract import split_reference_section  # 局部导入，避免循环

    try:
        body, _ = split_reference_section(text or "")
        return body or (text or "")
    except Exception:  # noqa: BLE001 - 抽不出就退回原文，不阻断检查
        return text or ""


def _cite_stats(sentence: str) -> dict[str, int]:
    """一句里的引注统计。"""
    authors = _AUTHOR_YEAR.findall(sentence)
    nums = _NUM_CITE.findall(sentence)
    # 数字引注可能一次含多个编号（[1,2,3]），按逗号折算
    num_count = 0
    for m in nums:
        inner = re.findall(r"\d+", m)
        num_count += max(1, len(inner))
    return {"authors": len(authors), "numbers": num_count,
            "total": len(authors) + num_count}


def _make(kind: str, severity: Severity, message: str, *, text: str, para_index: int,
          evidence: str, find: str, detected_by: str, fix_hint: str = "",
          confidence: float = 1.0) -> Issue:
    loc: dict[str, Any] = {"paragraph": para_index}
    if find:
        loc["find"] = find
    return Issue(
        id="",
        kind=kind,
        severity=severity,
        message=message,
        location=loc,
        evidence=(evidence or "")[:200],
        detected_by=detected_by,
        confidence=confidence,
        fixable=False,   # 写作质量问题需作者改写，不做机械修复
        fix_hint=fix_hint,
    )


def _locate(text: str, para: str) -> tuple[int, str]:
    """返回段落序号与被截短的定位串（`find` 需唯一，故取段落首 12 字）。"""
    idx = text.find(para[:20])
    paras = split_paragraphs(text)
    try:
        pno = paras.index(para)
    except ValueError:
        pno = -1
    return pno, para[:12]


# ===========================================================================
# 引言
# ===========================================================================


@verifier(
    "check_intro_cars",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="引言三语步（确立领域→指出缺口→占据空间）是否齐备",
)
def check_intro_cars(text: str, ctx: Any) -> list[Issue]:
    """CARS 三语步的可检验形式。

    判定方式是从文本特征反推语步，而不是靠模型理解：
    缺口语步必然含缺口标记且指向本研究；定位语步必然出现"本文/本研究 + 动词"。
    两者都检测不到，说明引言只写了背景。
    """
    t = text or ""
    # 只在前 1/3 范围内找引言，避免把正文里的"本文"误当引言定位
    head = t[: max(2000, len(t) // 3)]
    issues: list[Issue] = []

    gap_sents = [s for s in split_sentences(head)
                 if _GAP_MARK.search(s) and not s.startswith(("参考文献",))]
    position_sents = [s for s in split_sentences(head)
                      if re.search(r"本文|本研究|笔者|本文的", s)
                      and re.search(r"构建|提出|分析|考察|检验|回答|探讨|研究|设计", s)]

    if not position_sents:
        issues.append(_make(
            "writing.intro_no_position", Severity.BLOCKER,
            "引言缺少「占据研究空间」语步：通篇未见以「本文/本研究」直陈研究任务的句子，"
            "读者读不出你打算做什么",
            text=t, para_index=0 if t else -1,
            evidence=head[:120], find=head[:12],
            detected_by="rules:check_intro_cars",
            fix_hint="补一段明确的定位陈述，形如「本文以……为对象，构建……，回答……问题」。"
                     "按 CARS 模型，这是引言的第三步，不能省。",
        ))
    if not gap_sents:
        issues.append(_make(
            "writing.intro_no_gap", Severity.BLOCKER,
            "引言缺少「指出研究缺口」语步：未见任何缺口标记（尚未/缺乏/未能/存在争议等），"
            "读者无法判断你的研究补上了什么",
            text=t, para_index=0 if t else -1,
            evidence=head[:120], find=head[:12],
            detected_by="rules:check_intro_cars",
            fix_hint="在背景之后单列一段指出缺口。缺口不成立，全文的立论就不成立。",
        ))
    return issues


@verifier(
    "check_intro_opening",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="引言开篇是否用可核实的事实，而非『随着……的发展』式套话",
)
def check_intro_opening(text: str, ctx: Any) -> list[Issue]:
    paras = split_paragraphs(text)
    if not paras:
        return []
    first = paras[0]
    # 跳过标题行（如"第一章 绪论"）与纯标题
    for p in paras[:3]:
        if len(p) >= 40:
            first = p
            break
    m = _EMPTY_OPENING.match(first)
    has_fact = bool(re.search(r"\d{4}\s*年|《[^》]{4,40}》|\d+(?:\.\d+)?\s*%|"
                              r"\d+(?:\.\d+)?\s*(?:亿|万|个|家|项)", first))
    if m and not has_fact:
        return [_make(
            "writing.intro_empty_opening", Severity.MAJOR,
            f"引言开篇是空转套话「{m.group(0)}」，未提供可核实的事实"
            "（政策文件名、年份、统计数据、既有研究结论）",
            text=text, para_index=0,
            evidence=first[:120], find=first[:12],
            detected_by="rules:check_intro_opening",
            fix_hint="改用一个具体落点开场：引用制度文件（如《……》）、给出关键数字、"
                     "或直接陈述既有研究的公认结论。套话开场会让评审在第一句就降低预期。",
        )]
    return []


@verifier(
    "check_intro_gap_specific",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="研究缺口是否具体到可检验（方法/数据/机制等维度）",
)
def check_intro_gap_specific(text: str, ctx: Any) -> list[Issue]:
    t = text or ""
    head = t[: max(2000, len(t) // 3)]
    dimensions = _gap_dimensions(ctx)
    for s in split_sentences(head):
        if _GAP_MARK.search(s) and not dimensions.search(s):
            # 只有模糊缺口词、没有维度词 → 缺口不具体
            vague = re.search(r"(研究(?:还)?(?:较|不)少|研究(?:还)?不足|尚不深入|"
                              r"有待深入|相关研究(?:较|不)少)", s)
            if vague:
                return [_make(
                    "writing.intro_gap_vague", Severity.MAJOR,
                    f"研究缺口表述不可检验：「{vague.group(0)}」既没说是方法不足、"
                    "数据不足还是解释不足，读者无法判断补上它是否值得",
                    text=t, para_index=0,
                    evidence=s[:120], find=s[:12],
                    detected_by="rules:check_intro_gap_specific",
                    fix_hint="把缺口落到具体维度，例如「现有方法在……情形下不适用」"
                             "「缺少针对……的样本」「尚未解释……的机制」。",
                )]
    return []


@verifier(
    "check_intro_no_citation_dump",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="引言是否用逐篇文献罗列代替了背景陈述",
)
def check_intro_no_citation_dump(text: str, ctx: Any) -> list[Issue]:
    t = text or ""
    head = t[: max(2000, len(t) // 3)]
    sents = split_sentences(head)
    if len(sents) < 6:
        return []
    dumped = [s for s in sents if _cite_stats(s)["total"] >= 2]
    ratio = len(dumped) / len(sents)
    if ratio >= 0.34 and len(dumped) >= 3:
        return [_make(
            "writing.intro_citation_dump", Severity.MINOR,
            f"引言中 {len(dumped)}/{len(sents)} 句含 2 个以上引注，接近文献综述的写法；"
            "引言引文献是为说明现状，逐篇介绍属综述的任务",
            text=t, para_index=0,
            evidence=" ｜ ".join(s[:40] for s in dumped[:3]),
            find=dumped[0][:12],
            detected_by="rules:check_intro_no_citation_dump",
            fix_hint="把成串的文献挪到文献综述；引言里每个判断后留 1~2 篇代表性文献即可。",
        )]
    return []


# ===========================================================================
# 文献综述
# ===========================================================================


@verifier(
    "check_lit_synthesis",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="文献综述是否逐篇罗列（「一篇论文半句话」）",
)
def check_lit_synthesis(text: str, ctx: Any) -> list[Issue]:
    """本助手最要紧的一条检查。

    判定两个信号，任一成立即报：

    * **逐篇罗列**：段落里含引注的句子中，绝大多数只含**一个**引注，
      形如"A（2021）……。B（2022）……。C（2023）……"；
    * **零整合**：整段没有任何一句含 2 个以上引注——说明文献从未被放在一起讨论。

    阈值取保守值（0.8 / 段落句数 ≥3），因为这些是建议而非硬规则，误报代价高。
    """
    issues: list[Issue] = []
    for pno, para in prose_paragraphs(strip_references(text), min_chars=80):
        sents = split_sentences(para)
        if len(sents) < 3:
            continue
        cited = [s for s in sents if _cite_stats(s)["total"] >= 1]
        if len(cited) < 3:
            continue
        single = [s for s in cited if _cite_stats(s)["total"] == 1]
        multi = [s for s in cited if _cite_stats(s)["total"] >= 2]
        ratio_single = len(single) / len(cited)

        if ratio_single >= 0.8:
            issues.append(_make(
                "writing.lit_per_paper", Severity.BLOCKER,
                f"文献综述存在「一篇论文半句话」：本段 {len(cited)} 个带引注的句子中，"
                f"{len(single)} 句只对应 1 篇文献（占比 {ratio_single:.0%}），"
                "等于把综述写成了逐篇摘要的拼接",
                text=text, para_index=pno,
                evidence=" ｜ ".join(s[:38] for s in single[:3]),
                find=single[0][:12],
                detected_by="rules:check_lit_synthesis",
                fix_hint="同一主题下的多篇文献合并成一个判断：先给结论或分歧，"
                         "再把若干文献并列作证。形如「多数研究认为X（A, 2020；B, 2021），"
                         "但C（2022）在Y情形下得到相反结果」。",
            ))
        elif not multi:
            issues.append(_make(
                "writing.lit_no_integration", Severity.MAJOR,
                f"本段 {len(cited)} 个句子各引 1 篇文献，没有任何一句把 2 篇以上文献"
                "放在一起讨论，缺少整合",
                text=text, para_index=pno,
                evidence=" ｜ ".join(s[:38] for s in single[:3]),
                find=single[0][:12],
                detected_by="rules:check_lit_synthesis",
                fix_hint="至少在一处把相关文献合并陈述，例如「A（2020）与B（2021）均发现……，"
                         "而C（2022）持相反看法」。",
            ))
    return issues[:8]


@verifier(
    "check_lit_coherence",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="文献综述段落之间是否有显式衔接（前后连贯）",
)
def check_lit_coherence(text: str, ctx: Any) -> list[Issue]:
    """检测"段落直接以文献引注开头"这一最典型的断裂形态。

    连贯的综述里，新一段通常先交代它要讲的判断或与上段的关系，再摆文献。
    若连续多段都以"某某（年份）"起首，说明是拼接而非论证。
    """
    # 门槛取 40 而非 60：连贯性检测的核心是"段首是否直接以文献引注开头"，
    # 这个判定与段落长短无关。实测踩过：61/53/54/47 字的一组分段样本，
    # 因门槛 60 只通过 1 段，导致"连续多段断裂"这一最典型的综述毛病检不出来。
    pairs = prose_paragraphs(strip_references(text), min_chars=40)
    if len(pairs) < 3:
        return []
    paras = [p for _, p in pairs]
    orig_idx = [i for i, _ in pairs]
    issues: list[Issue] = []
    run: list[int] = []
    for pos, para in enumerate(paras):
        pno = orig_idx[pos]
        sents = split_sentences(para)
        if not sents:
            continue
        head = sents[0]
        # 段首既无衔接词，又直接以引注开头 → 断裂
        starts_with_cite = bool(_AUTHOR_YEAR.match(head) or _NUM_CITE.match(head))
        has_cohesion = bool(_COHESION_HEAD.match(head))
        if starts_with_cite and not has_cohesion:
            run.append(pno)
        else:
            if len(run) >= 2:
                issues.append(_make(
                    "writing.lit_broken_flow", Severity.MAJOR,
                    f"第 {run[0]+1}~{run[-1]+1} 段连续 {len(run)} 段均以文献引注直接开头，"
                    "段间没有承接，读起来是文献的堆叠而非论证的推进",
                    text=text, para_index=run[0],
                    evidence=paras[run[0]][:100], find=paras[run[0]][:12],
                    detected_by="rules:check_lit_coherence",
                    fix_hint="段首补一句主题句或衔接语，说明本段与上段是继承、补充还是对立，"
                             "例如「在方法层面，另一条线索来自……」。",
                ))
            run = []
    if len(run) >= 2:
        issues.append(_make(
            "writing.lit_broken_flow", Severity.MAJOR,
            f"末段起连续 {len(run)} 段均以文献引注直接开头，段间没有承接",
            text=text, para_index=run[0],
            evidence=paras[run[0]][:100], find=paras[run[0]][:12],
            detected_by="rules:check_lit_coherence",
            fix_hint="为这些段落补主题句，把并列的文献改成有关系的论证。",
        ))
    return issues[:6]


@verifier(
    "check_lit_critique",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="文献综述是否只述不评（缺少对局限与分歧的评价）",
)
def check_lit_critique(text: str, ctx: Any) -> list[Issue]:
    t = text or ""
    sents = split_sentences(t)
    # 阈值取 5 句：一段典型的综述小节常在 5~8 句之间，用 8 会漏掉较短的段落。
    # 实测踩过：6 句的"纯罗列"段落因为不达 8 句而被跳过，检查器形同失效。
    if len(sents) < 5:
        return []
    critique = re.compile(
        r"然而|但是|但|不过|局限|未能|尚未|不足以|仅在.{0,15}条件|"
        r"存在争议|分歧|相(?:反|异)|不一致|难以|无法|忽视了|留下"
    )
    n = sum(1 for s in sents if critique.search(s))
    ratio = n / len(sents)
    if ratio < 0.10:
        return [_make(
            "writing.lit_no_critique", Severity.MAJOR,
            f"综述段落共 {len(sents)} 句，仅 {n} 句（{ratio:.0%}）含评价性表述，"
            "接近纯摘要汇编；只有「述」没有「评」就无法推出研究缺口",
            text=t, para_index=-1,
            evidence="", find="",
            detected_by="rules:check_lit_critique",
            fix_hint="每组文献介绍完后补一句评价：共同局限是什么、在什么条件下不适用、"
                     "留下了什么未解问题。",
        )]
    return []


# ===========================================================================
# 正文（通用段落质量）
# ===========================================================================


@verifier(
    "check_paragraph_topic_sentence",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="段落是否结论先行（首句为主题句而非文献引注）",
)
def check_paragraph_topic_sentence(text: str, ctx: Any) -> list[Issue]:
    """段首即引注 = 段落没有主张，只是在复述。

    只报"段首直接是引注"这一种最明确的情形，不判断主题句写得好不好——
    后者需要语义理解，规则做不到，硬做就是造假问题。
    """
    issues: list[Issue] = []
    for pno, para in prose_paragraphs(strip_references(text), min_chars=60):
        sents = split_sentences(para)
        if not sents:
            continue
        head = sents[0]
        if (_AUTHOR_YEAR.match(head) or _NUM_CITE.match(head)) and not _COHESION_HEAD.match(head):
            issues.append(_make(
                "writing.no_topic_sentence", Severity.MAJOR,
                f"第 {pno+1} 段首句直接以文献引注开头，没有主题句：读者不知道这一段要说明什么",
                text=text, para_index=pno,
                evidence=head[:100], find=head[:12],
                detected_by="rules:check_paragraph_topic_sentence",
                fix_hint="把这一段的判断提到首句，例如「在估值方法层面，国内研究已形成以"
                         "收益法为核心的格局」，再摆文献作证。",
            ))
    return issues[:8]


@verifier(
    "check_paragraph_analysis",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="正文段落是否有分析（而非通篇事实罗列）",
)
def check_paragraph_analysis(text: str, ctx: Any) -> list[Issue]:
    """段内没有解释性句子 = 只摆了事实，没说这事实意味着什么。"""
    issues: list[Issue] = []
    for pno, para in prose_paragraphs(strip_references(text), min_chars=120):
        sents = split_sentences(para)
        if len(sents) < 3:
            continue
        if _ANALYTIC.search(para):
            continue
        # 段内出现数字/图表引用，说明是"摆事实"型的段；此时无分析才报
        if re.search(r"\d+(?:\.\d+)?\s*%|图\s*\d+|表\s*\d+|\d{4}\s*年", para):
            issues.append(_make(
                "writing.no_analysis", Severity.MAJOR,
                f"第 {pno+1} 段共 {len(sents)} 句、含数据或图表引用，但没有任何解释性表述："
                "只陈述了事实，未说明它意味着什么",
                text=text, para_index=pno,
                evidence=para[:120], find=para[:12],
                detected_by="rules:check_paragraph_analysis",
                fix_hint="补一句分析：这个结果为什么会出现、与既有研究是否一致、"
                         "对本文的判断意味着什么。",
            ))
    return issues[:8]


@verifier(
    "check_claim_evidence",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="强断言是否都有依据（数字/图表/引注）",
)
def check_claim_evidence(text: str, ctx: Any) -> list[Issue]:
    issues: list[Issue] = []
    for pno, para in prose_paragraphs(strip_references(text), min_chars=40):
        has_evidence = bool(_EVIDENCE.search(para))
        if has_evidence:
            continue
        for s in split_sentences(para):
            m = _STRONG_CLAIM.search(s)
            if m:
                issues.append(_make(
                    "writing.claim_without_evidence", Severity.BLOCKER,
                    f"出现强断言「{m.group(0)}」但所在段落没有任何依据"
                    "（无数字、无图表引用、无文献引注）",
                    text=text, para_index=pno,
                    evidence=s[:120], find=s[:12],
                    detected_by="rules:check_claim_evidence",
                    fix_hint="补上支撑数据或文献，或把断言弱化为「可能」「在本文样本中」"
                             "这类有保留的表述。无依据的强断言是盲审的高频扣分点。",
                ))
                break
    return issues[:8]


@verifier(
    "check_body_progression",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="论证是否推进（段落结构不雷同、有推进性衔接）",
)
def check_body_progression(text: str, ctx: Any) -> list[Issue]:
    """连续多段句数完全一致 = 并列堆砌的信号。

    只报"连续 3 段以上句数相同"这一种可确证的情形：段落结构雷同可能出自
    刻意对称，但对论文而言更可能是模板化写作。给出实测值供作者判断。
    """
    pairs = prose_paragraphs(strip_references(text), min_chars=100)
    if len(pairs) < 4:
        return []
    paras = [p for _, p in pairs]
    counts = [len(split_sentences(p)) for p in paras]
    issues: list[Issue] = []
    run_start = 0
    for i in range(1, len(counts) + 1):
        if i < len(counts) and counts[i] == counts[run_start]:
            continue
        if i - run_start >= 3:
            issues.append(_make(
                "writing.flat_progression", Severity.MAJOR,
                f"第 {run_start+1}~{i} 段连续 {i-run_start} 段句数均为 {counts[run_start]} 句，"
                "结构雷同，读起来是并列罗列而非逐步推进",
                text=text, para_index=run_start,
                evidence=" ｜ ".join(p[:30] for p in paras[run_start:i]),
                find=paras[run_start][:12],
                detected_by="rules:check_body_progression",
                fix_hint="让各段承担不同的论证任务（提出判断→给证据→解释机制→界定边界），"
                         "并在段首用「进一步」「反过来」等显式推进语标出关系。",
            ))
        run_start = i
    return issues[:4]


@verifier(
    "check_template_connectives",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="模板腔连接词密度（纯结构标记，不含信息）",
)
def check_template_connectives(text: str, ctx: Any) -> list[Issue]:
    hits = list(_TEMPLATE.finditer(text or ""))
    cjk = len(_CJK.findall(text or ""))
    # 门槛从 200 降到 120：开题报告的一个自然段常在 120~250 字之间，
    # 用 200 会把"整段都是套话"的短段漏掉。实测踩过：177 字的纯套话样本
    # 因为不达门槛而被整段跳过。
    if cjk < 120:
        return []
    per_k = len(hits) * 1000 / cjk
    if per_k >= 3.0:
        seen: list[str] = []
        for m in hits:
            if m.group(0) not in seen:
                seen.append(m.group(0))
        return [_make(
            "writing.template_connectives", Severity.MINOR,
            f"模板腔连接词密度 {per_k:.1f}/千字（命中 {len(hits)} 处："
            f"{'、'.join(seen[:6])}），这类词不含信息，是 AI 腔的主要来源",
            text=text, para_index=-1,
            evidence="、".join(seen[:8]), find=hits[0].group(0),
            detected_by="rules:check_template_connectives",
            fix_hint="删除或替换为有实义的连接：把「综上所述」换成具体判断，"
                     "把「值得注意的是」换成它到底值得注意在哪。",
        )]
    return []


@verifier(
    "check_paragraph_variance",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="句长是否长短交错（风格提示，非硬性错误）",
)
def check_paragraph_variance(text: str, ctx: Any) -> list[Issue]:
    """句长变异系数过低 = 节奏均匀，读起来像模板。

    这是 info 级提示：短段落天然均匀，不该当错误处理（`tools/aigc_pass.py`
    里记录了这个指标对短文本不适用的实测）。
    """
    issues: list[Issue] = []
    for pno, para in prose_paragraphs(strip_references(text), min_chars=300):
        sents = split_sentences(para)
        if len(sents) < 6:
            continue
        lens = [len(s) for s in sents]
        mean = statistics.fmean(lens)
        if mean <= 0:
            continue
        cv = statistics.pstdev(lens) / mean
        if cv < 0.32:
            issues.append(_make(
                "writing.flat_rhythm", Severity.INFO,
                f"第 {pno+1} 段 {len(sents)} 句的句长变异系数仅 {cv:.2f}"
                f"（均长 {mean:.0f} 字），节奏均匀",
                text=text, para_index=pno,
                evidence=para[:100], find=para[:12],
                detected_by="rules:check_paragraph_variance",
                fix_hint="这是风格提示而非错误。若本段读起来确实像模板，"
                         "可把观点句改短、证据句保留长度，形成长短交错。",
            ))
    return issues[:5]


# ===========================================================================
# 章节级检查的基础设施
# ===========================================================================
#
# 上面这些检查器都只看**一段或整篇**，不需要知道"这是第几章、属于哪一类章节"。
# 而研究设计、实证结果、讨论、结论这四类章节的要求是**章与章之间**的关系：
#   * 结果章提到的方法必须在研究设计章交代过；
#   * 结论里的数字必须在正文里出现过；
#   * 讨论章不能出现结果章没有的表格。
# 这类判定必须先按标题把稿件切成章、判定每章的蓝图类型，再横向比对。
#
# **退化行为很重要**：起草阶段的自查拿到的是"单章正文"，里面没有 `# 标题`，
# 于是 `sections_of_kind` 返回空、检查器直接跳过（返回 []）。这不会漏掉问题——
# 这些检查会在**验证阶段**拿到整篇稿件时真正执行。宁可跳过，也不要拿半篇文本
# 去比对而报出一堆假问题：假问题会让作者连同真问题一起忽略。

_SECTION_HEAD = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.M)


def split_by_sections(text: str) -> list[tuple[str, str]]:
    """按 ``# 标题`` 把稿件切成 ``[(标题, 正文)]``。

    无标题时返回 ``[("", 全文)]``——让调用方能区分"切不出来"与"文本为空"。
    """
    t = text or ""
    matches = list(_SECTION_HEAD.finditer(t))
    if not matches:
        return [("", t.strip())] if t.strip() else []
    out: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(t)
        out.append((m.group(1).strip(), t[m.end() : end].strip()))
    return out


def sections_of_kind(text: str, kinds: tuple[str, ...]) -> list[tuple[str, str]]:
    """取出属于指定蓝图类型的章节。

    只认带标题的章节：标题为空的整篇文本无法判定类型，一律不返回，
    调用方据此跳过。这样"起草阶段的单章自查"不会误触发章间检查。
    """
    from ..writing import classify  # 局部导入：避免 checks ↔ writing 的导入顺序耦合

    out: list[tuple[str, str]] = []
    for title, body in split_by_sections(text):
        if not title:
            continue
        try:
            kind = classify(title).kind
        except Exception:  # noqa: BLE001 - 分类失败就跳过这一章，不阻断整轮检查
            continue
        if kind in kinds:
            out.append((title, body))
    return out


#: 引用角标、章节号、图表号——从"数字"里排除掉，它们不是事实数据。
#: 不排除的话，结论里一句「（见第 4 章）」会让 "4" 被当成"正文里没有的数字"。
#:
#: 实现放在 ``app.writing.citations``：结论生成端与这里的核对必须用**同一条口径**，
#: 否则会出现"已就地标注的问题，检查器却说没问题"这种自相矛盾的结果。
from ..writing.citations import fact_numbers  # noqa: E402  (见上方说明：必须共用一份实现)


#: 方法名词表**不在这里**，见 ``_method_terms(ctx)``：表跟着论文类型走，
#: 定义在 ``app/writing/genres.py`` 的各类型画像里。
#: 硬编码一套经管方法名（双重差分、多期超额收益法……）会让这个检查器
#: 在材料学、教育学、人文学科的论文上**静默失效**——永远报不出问题，
#: 而"永远报不出"看起来像"没有问题"。


# ===========================================================================
# 技术类引言：局限—挑战—方案—贡献 的闭环
# ===========================================================================
#
# 以下四组检查来自 HKUST(GZ) DIAL 实验室的 Supervisor-Skills 项目
# （https://github.com/HKUSTDial/Supervisor-Skills ，skills/intro-drafter 与
# skills/tech-paper-template，许可 CC BY-NC-SA 4.0 / CC BY 4.0）。
#
# 该项目给出的原始形式是**四条自洽性检查**（局限→关键思路→挑战→模块→贡献）
# 与**六段式引言流程**。这里把其中**可机械判定**的部分落成检查器：
#
# * 局限与挑战必须同时存在（只有挑战 = 挑战是凭空造的）；
# * 贡献条目必须具体（不能是"进行了大量实验"）；
# * 贡献条目应当绑定章节（写不出落点的贡献通常本身就没落实）；
# * 不要宣传腔。
#
# **链条的语义部分（关键思路是否真的回应了局限）无法机械判定**，那需要读全文的
# 判断，本项目不做——不做假承诺。所以这里只钉住"形式上缺环"与"条目空转"这两类。

#: 局限语段的标记：出现这些词，说明作者在说"现有做法不行"。
_LIMITATION_MARK = re.compile(
    r"局限|不足|缺陷|未能|无法|难以|受限于|瓶颈|尚不|不能|失效|不适用|"
    r"忽略|忽视|假设过强|依赖.{0,6}前提|仅在.{0,10}条件下"
)
#: 挑战语段的标记。
_CHALLENGE_MARK = re.compile(
    r"挑战|难点|困难(?:在于|之处)|关键问题在于|障碍|不易|棘手|"
    r"如何.{0,12}(?:成|保证|兼顾|权衡|确定|刻画)|需要解决"
)
#: 贡献语段的标记。
_CONTRIBUTION_MARK = re.compile(
    r"本文(?:的)?(?:主要)?贡献|本文贡献|研究贡献|主要工作(?:与贡献)?|"
    r"贡献(?:如下|在于|包括)|本文(?:的)?创新(?:点|之处)"
)
#: 贡献/挑战段落里的编号条目。
_NUMBERED_ITEM = re.compile(
    r"^\s*(?:[（(]\s*\d{1,2}\s*[)）]|\d{1,2}\s*[.、．)]|"
    r"[（(]\s*[一二三四五六七八九十]{1,3}\s*[)）]|[一二三四五六七八九十]{1,3}\s*[、.．])\s*(.+)$"
)
#: 贡献条目里的**空话**：占了条目的位置却什么也没说。
_EMPTY_CONTRIBUTION = re.compile(
    r"大量(?:的)?(?:实验|试验|分析|工作)|充分的?(?:实验|验证)|"
    r"取得了?(?:良好|较好|不错|优异)的?(?:效果|结果|性能)|"
    r"具有(?:重要|一定)的?(?:理论|实践|现实|指导)?(?:意义|价值)|"
    r"深入研究|系统研究|全面(?:分析|研究)|"
    r"extensive experiments|comprehensive (?:experiments|evaluation)|"
    r"good (?:results|performance)|promising results"
)
#: 贡献条目里的章节落点。
_SECTION_POINT = re.compile(r"第\s*[一二三四五六七八九十\d]+\s*[章节篇]|\d+\.\d+\s*节|见\s*\d|Sec(?:tion)?\.?\s*\d")

#: 宣传腔 / AI 腔词表（中英双语）。
#:
#: 与 ``_TEMPLATE``（空转连接词）和 ``_OVERCLAIM``（以本文为主语的夸大断言）**分工不同**：
#: 这两者管的是"没有信息的过渡语"与"没有依据的自我评价"；
#: 这里管的是**宣传性形容词与动词**——它们不构成断言，所以前两个检查器抓不到，
#: 但堆多了整段读起来就像宣传稿。
#:
#: 英文部分取自 Supervisor-Skills 的 banned AI-tone vocabulary 列表
#: （skills/pre-submission-reviewer，CC BY 4.0），中文部分是它的对应表述。
#: 该表**只用于提示，不用于判错**：单独一个"创新性"不是问题，
#: 密度过高才是问题——阈值定得保守，避免误报毁掉工具可信度。
_AI_TONE = re.compile(
    r"革命性|开创性|前所未有|突破性(?:的)?(?:进展|成果|性能)|"
    r"颠覆性|划时代|里程碑(?:式|意义)|卓越|极致|完美(?:地)?(?:解决|契合)|"
    r"赋能|助力|深度(?:融合|赋能)|全方位|多维度(?:地)?(?:赋能|提升)|"
    r"显著(?:地)?(?:赋能|助力)|强大的?(?:能力|性能)|"
    r"innovative|pioneering|revolutionary|transformative|unprecedented|"
    r"breakthrough|remarkable|superior|surpass|excel(?:s|led)?|"
    r"pave the way|underscore|profound challenges",
)


def _paragraphs_by_marker(body: str, marker: re.Pattern) -> list[str]:
    """返回含该标记的段落（散文段，按行切）。"""
    out: list[str] = []
    for para in split_paragraphs(body):
        if marker.search(para):
            out.append(para)
    return out


#: 条目回退切分：没有编号时，按"第一，/其二，"这类序列词或句末标点切。
_ORDINAL_SPLIT = re.compile(r"(?:第[一二三四五六七八九十]|其[一二三四五六七八九]|[（(][一二三四五六七八九十][)）])\s*[，,、]?")


def _items_after_marker(body: str, marker: re.Pattern, *, max_items: int = 8) -> list[str]:
    """取出标记**之后**的条目列表。

    **不能只看含标记的那一段。** 这一点踩过：贡献通常写成

        本文的主要贡献如下：
        1. 提出……
        2. 提出……

    标记在上一行，条目在**后面的行**里。只取含标记的段落，会得到一句
    「本文的主要贡献如下：」然后判定"没有条目"——检查器于是永远不报，
    而"永远不报"看起来像"贡献写得很规范"。所以这里从标记位置往后扫，
    收集连续的编号条目，遇到空行或标题就收尾。
    """
    m = marker.search(body)
    if not m:
        return []
    tail = body[m.start():]
    lines = tail.split("\n")
    items: list[str] = []

    # 标记所在行本身可能就带了第一条（"本文的贡献如下：1. …"）
    head = lines[0]
    for mm in _NUMBERED_ITEM.finditer(head):
        items.append(mm.group(1).strip())

    started = bool(items)
    for raw in lines[1:]:
        s = raw.strip()
        if not s:
            if started:
                break          # 空行 = 列表结束
            continue
        if s.startswith("#"):
            break
        mm = _NUMBERED_ITEM.match(s)
        if mm:
            items.append(mm.group(1).strip())
            started = True
            continue
        if started:
            break              # 列表开始后遇到非编号行，收尾
        # 标记与条目之间夹了一句引导语（"具体而言："），继续往下找

    if items:
        return [x for x in items if x][:max_items]

    # 回退：整段散文式列举（"第一，……第二，……"）按序列词切
    whole = " ".join(ln.strip() for ln in lines if ln.strip())[:1200]
    pieces = [p.strip(" ，,。；;") for p in _ORDINAL_SPLIT.split(whole) if p and len(p.strip()) >= 14]
    return pieces[:max_items]


@verifier(
    "check_limitation_challenge_chain",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="技术类引言里「已有工作的局限」与「关键挑战」是否成链",
)
def check_limitation_challenge_chain(text: str, ctx: Any) -> list[Issue]:
    """只有挑战、没有局限 → 读者会认为挑战是凭空造出来给方法找理由的。

    判定口径刻意只判**形式上缺环**：能识别出挑战语段却识别不出局限语段时才报。
    "这条挑战是否真的回应了那条局限"需要读全文判断，本项目不做——
    不做假承诺比多报一条更重要。

    只在**能识别出引言章节**时运行（验证阶段）；起草阶段的单章文本没有标题，自动跳过。
    """
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("introduction",)):
        if len(body) < 120:
            continue
        challenges = _paragraphs_by_marker(body, _CHALLENGE_MARK)
        limitations = _paragraphs_by_marker(body, _LIMITATION_MARK)
        if challenges and not limitations:
            issues.append(_make(
                "writing.intro_challenge_without_limitation", Severity.MAJOR,
                f"「{title}」列出了关键挑战，却没有交代已有工作的局限："
                "读者会认为这些挑战是为了给后面的方法找理由而编造的",
                text=text, para_index=-1,
                evidence=challenges[0][:140], find=challenges[0][:12],
                detected_by="rules:check_limitation_challenge_chain",
                fix_hint="在挑战之前补一段「已有做法为什么不行」：具体到在什么条件下失效、"
                         "缺哪类证据或哪种能力。挑战与局限成链，引言才立得住。"
                         "（这条链条的做法参考 HKUST(GZ) Supervisor-Skills 的 tech-paper-template）",
            ))
    return issues[:2]


@verifier(
    "check_contributions_specific",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="贡献条目是否具体（而非「进行了大量实验」这类空话）",
)
def check_contributions_specific(text: str, ctx: Any) -> list[Issue]:
    """贡献条目占了位置却没说事，是引言里最常见的浪费。

    「进行了大量实验」「取得了良好效果」这类句子看起来像贡献，
    但评审从中读不出任何可检验的信息。它们的共同特征是**不含具体对象**：
    没有方法名、没有条件、没有指标、没有章节落点。
    """
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("introduction",)):
        items = _items_after_marker(body, _CONTRIBUTION_MARK)
        if not items:
            continue
        bad = [it for it in items if _EMPTY_CONTRIBUTION.search(it) or len(it) < 14]
        if not bad:
            continue
        issues.append(_make(
            "writing.contribution_not_specific", Severity.MAJOR,
            f"「{title}」有 {len(bad)}/{len(items)} 条贡献没有说出具体内容"
            f"（例如「{bad[0][:50]}」）：这类句子占了条目的位置，评审却读不出可检验的信息",
            text=text, para_index=-1,
            evidence="；".join(b[:50] for b in bad[:3]), find=bad[0][:12],
            detected_by="rules:check_contributions_specific",
            fix_hint="每条贡献写成可检验的陈述：提出了什么方法/机制、在什么条件下、"
                     "解决了哪一条挑战、落在哪一章。「进行了大量实验」不是贡献，是工作量。",
        ))
    return issues[:2]


@verifier(
    "check_contributions_map_sections",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="每条贡献是否绑定章节（写不出落点的贡献通常没落实）",
)
def check_contributions_map_sections(text: str, ctx: Any) -> list[Issue]:
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("introduction",)):
        items = _items_after_marker(body, _CONTRIBUTION_MARK)
        if len(items) < 2:
            continue
        marked = [it for it in items if _SECTION_POINT.search(it)]
        if marked:
            continue
        issues.append(_make(
            "writing.contribution_untraceable", Severity.MAJOR,
            f"「{title}」的 {len(items)} 条贡献都没有标明落点：评审无法快速核对"
            "每条贡献对应正文的哪一部分",
            text=text, para_index=-1,
            evidence=items[0][:140], find=items[0][:12],
            detected_by="rules:check_contributions_map_sections",
            fix_hint="在每条贡献后补上落点，形如「（第 4 章）」「见 3.2 节」。"
                     "这既方便评审，也是对作者自己的约束：写不出落点的贡献，"
                     "通常本身就没有落实。",
        ))
    return issues[:2]


@verifier(
    "check_ai_tone_vocabulary",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="宣传腔/AI 腔形容词与动词的密度（革命性、赋能、助力、前所未有…）",
)
def check_ai_tone_vocabulary(text: str, ctx: Any) -> list[Issue]:
    """这类词**不构成断言**，所以 ``check_overclaim`` 抓不到；但它们堆多了就很像宣传稿。

    判定用**密度**而不是出现次数：学术写作里偶尔出现「创新」是正常的，
    单独一个词不是问题。阈值定得保守，宁可漏报也不误报——
    误报会让作者连同真问题一起忽略。
    """
    body = strip_references(text)
    hits = list(_AI_TONE.finditer(body))
    cjk = len(_CJK.findall(body))
    # 门槛取 120 而不是 400：这是**同一个坑踩第二次**。`check_template_connectives`
    # 原先门槛 200，实测一个 177 字的纯套话样本因不达门槛被整段跳过——最该报的样本
    # 反而不报。密度是千分比，本来就与文本长度无关；长度门槛只用来挡掉
    # "短到没有统计意义"的片段，120 字（约 3~4 句）已经够。
    if cjk < 120 or not hits:
        return []
    per_k = len(hits) * 1000 / cjk
    if per_k < 2.0:
        return []
    seen: list[str] = []
    for m in hits:
        if m.group(0) not in seen:
            seen.append(m.group(0))
    return [_make(
        "writing.ai_tone_vocabulary", Severity.MINOR,
        f"宣传腔形容词/动词密度 {per_k:.1f}/千字（命中 {len(hits)} 处：{'、'.join(seen[:8])}）："
        "这类词不提供信息，堆多了整段读起来像宣传稿",
        text=text, para_index=-1,
        evidence="、".join(seen[:10]), find=hits[0].group(0),
        detected_by="rules:check_ai_tone_vocabulary",
        fix_hint="删掉或换成具体事实：「赋能企业转型」改成「使企业的X指标提高Y%」，"
                 "「开创性的方法」改成「该方法此前未被用于Z情形」。"
                 "形容词越少，评审越愿意相信你写的数据。",
    )]


#: 夸大的自我评价。**必须与"本文/本研究"同句**才判定：
#: 「这是我国首次……」「重大政策」是客观陈述，不该被判成夸大；
#: 「本文首次提出……」才是作者的越界声明。
_OVERCLAIM = re.compile(
    r"首次(?:提出|构建|揭示|发现|证实|将)|"
    r"填补了?[^。；]{0,12}空白|"
    r"开创(?:性|了)|突破性(?:进展|成果|贡献)|"
    r"重大(?:理论|实践|学术)?(?:意义|价值|贡献)|"
    r"具有(?:重要|深远)的?(?:理论|现实|指导)?意义|"
    r"证明(?:了|出)|充分(?:说明|证明|表明)|彻底(?:解决|改变)|"
    r"完美(?:契合|诠释)|最优(?:方案|选择|解)"
)
_SELF_REF = re.compile(r"本文|本研究|笔者|本课题")


def _self_referential_claims(text: str) -> list[tuple[int, str, str]]:
    """返回 ``(段号, 原句, 命中词)``，只含以本文为主语的夸大表述。"""
    out: list[tuple[int, str, str]] = []
    for pno, para in prose_paragraphs(strip_references(text), min_chars=20):
        for s in split_sentences(para):
            if not _SELF_REF.search(s):
                continue
            m = _OVERCLAIM.search(s)
            if m:
                out.append((pno, s, m.group(0)))
    return out


@verifier(
    "check_overclaim",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="是否出现「首次/填补空白/重大意义/证明了」这类以本文为主语的夸大断言",
)
def check_overclaim(text: str, ctx: Any) -> list[Issue]:
    """夸大表述是盲审最容易被追问的地方，也是最容易改的地方。

    判定口径刻意收紧到**同一句里出现「本文/本研究」**：这样「本文首次提出……」
    会被报出，而「2024 年该领域首次出现……」这类客观陈述不会——后者是事实，
    删掉反而是错的。
    """
    issues: list[Issue] = []
    for pno, sent, hit in _self_referential_claims(text):
        issues.append(_make(
            "writing.overclaim", Severity.MAJOR,
            f"出现夸大性自我评价「{hit}」：这类断言无法由本文的数据支持，"
            "评审通常会追问「凭什么说首次」",
            text=text, para_index=pno,
            evidence=sent[:160], find=hit,
            detected_by="rules:check_overclaim",
            fix_hint="改成与证据强度相称的表述：把「首次提出」改成「本文尝试……」，"
                     "把「证明了」改成「本研究的证据支持……」。"
                     "创新点的表述请留给作者本人判断，工具不给建议。",
        ))
    return issues[:6]


# ===========================================================================
# 研究设计
# ===========================================================================


@verifier(
    "check_method_specific",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="研究设计是否写到可复现粒度（具体数字与来源，而非『相关数据』）",
)
def check_method_specific(text: str, ctx: Any) -> list[Issue]:
    """可复现性的最粗判定：有没有具体数字与数据来源，有没有用模糊词代替。

    这是**章级**检查：只在能识别出研究设计章时运行（验证阶段）。起草阶段的
    单章文本没有标题，直接跳过。
    """
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("method",)):
        if len(body) < 200:
            continue
        nums = re.findall(r"\d{4}\s*年|\d[\d,]*\s*(?:个|家|条|项|年|期|%|％)", body)
        vague = re.findall(r"相关数据|有关数据|合适的方法|适当的方法|一定的|一定程度上|"
                           r"尽可能地|采取了相应|进行了相关", body)
        if len(nums) < 3 or vague:
            why = []
            if len(nums) < 3:
                why.append(f"全文仅 {len(nums)} 处具体数字（样本量/年份区间/变量数/参数值）")
            if vague:
                why.append(f"出现模糊替代词：{'、'.join(sorted(set(vague))[:5])}")
            issues.append(_make(
                "writing.method_not_reproducible", Severity.MAJOR,
                f"「{title}」未写到可复现粒度：{'；'.join(why)}",
                text=text, para_index=-1,
                evidence=body[:120], find=body[:12],
                detected_by="rules:check_method_specific",
                fix_hint="把「选取相关数据」改成具体口径：数据来源名称、年份区间、"
                         "筛选条件、最终样本量；把「采用合适的方法」改成方法名与选择理由。"
                         "研究设计章的功能就是让读者能重做一遍。",
            ))
    return issues[:3]


@verifier(
    "check_method_choice_reason",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="方法选择是否给出理由（为何这个方法适合本研究的问题）",
)
def check_method_choice_reason(text: str, ctx: Any) -> list[Issue]:
    reason = re.compile(r"之所以|是因为|原因在于|更适合|更适用于|考虑到|鉴于|"
                        r"相较(?:于|之下)|相比之?下|优于|能够(?:较好|有效)?(?:处理|识别|克服)|"
                        r"为(?:了)?(?:解决|克服|避免)|该方法(?:的)?(?:优势|适用)")
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("method",)):
        if len(body) < 200:
            continue
        if reason.search(body):
            continue
        issues.append(_make(
            "writing.method_no_reason", Severity.MAJOR,
            f"「{title}」没有说明为什么选用这个（些）方法：读者无法判断方法与研究问题是否匹配",
            text=text, para_index=-1,
            evidence=body[:120], find=body[:12],
            detected_by="rules:check_method_choice_reason",
            fix_hint="补一句选择理由，要指向本研究的数据特征或问题类型，"
                     "例如「之所以采用双重差分，是因为政策在不同企业间分批实施，"
                     "恰好构成准自然实验」。不要写「因为文献常用」。",
        ))
    return issues[:3]


@verifier(
    "check_method_no_textbook_dump",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="研究设计是否退化成教科书式的方法原理复述",
)
def check_method_no_textbook_dump(text: str, ctx: Any) -> list[Issue]:
    """连续多段不含本研究的具体信息 = 疑似在复述方法原理。

    判据是"这一章里有没有本研究自己的东西"（数字、变量名、本文/本研究），
    而不是"有没有公式"——公式本身是合规的。
    """
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("method",)):
        paras = [p for _, p in prose_paragraphs(body, min_chars=150)]
        if len(paras) < 3:
            continue
        dry = 0
        worst: list[str] = []
        for p in paras:
            if re.search(r"\d", p) or _SELF_REF.search(p) or re.search(r"变量|样本|指标|系数|参数", p):
                dry = 0
                worst = []
                continue
            dry += 1
            worst.append(p)
            if dry >= 3:
                issues.append(_make(
                    "writing.method_textbook_dump", Severity.MINOR,
                    f"「{title}」连续 {dry} 段没有出现本研究的具体信息"
                    "（数据、变量、参数、本文），疑似在复述方法原理",
                    text=text, para_index=-1,
                    evidence=" ｜ ".join(p[:40] for p in worst[:3]),
                    find=worst[0][:12] if worst else "",
                    detected_by="rules:check_method_no_textbook_dump",
                    fix_hint="方法原理给出出处即可，篇幅要花在「本研究怎么用」上："
                             "变量怎么构造、参数怎么取值、样本怎么筛。",
                ))
                break
    return issues[:3]


# ===========================================================================
# 实证结果 / 讨论 / 结论：章间一致性
# ===========================================================================


@verifier(
    "check_results_no_new_method",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="实证结果章是否引入了研究设计章未交代的方法",
)
def check_results_no_new_method(text: str, ctx: Any) -> list[Issue]:
    """结果章出现、研究设计章没有的方法名 = 读者无法判断结果怎么来的。

    只在**两章都存在**时判定。缺研究设计章时跳过——那不是"没问题"，
    而是这份稿子还没写到能判的程度（避免报一条无用的假问题）。
    """
    design = sections_of_kind(text, ("method",))
    results = sections_of_kind(text, ("results",))
    if not design or not results:
        return []
    design_text = " ".join(b for _, b in design)
    issues: list[Issue] = []
    for title, body in results:
        terms = _method_terms(ctx)
        missing = [t for t in terms if t in body and t not in design_text]
        if missing:
            issues.append(_make(
                "writing.results_new_method", Severity.MAJOR,
                f"「{title}」出现了研究设计章未交代的方法：{'、'.join(missing[:5])}；"
                "读者无法判断这些结果是怎么算出来的",
                text=text, para_index=-1,
                evidence="、".join(missing[:5]), find=missing[0],
                detected_by="rules:check_results_no_new_method",
                fix_hint="把该方法的交代补进研究设计章（方法形式、估计量、选择理由），"
                         "或在结果章就地说明它为何在此处才出现。",
            ))
    return issues[:3]


#: "结果未被支持"的表述。有这些说明作者如实报告了不支持假设的情形。
_NEGATIVE_RESULT = re.compile(
    r"不显著|未(?:能)?通过|未得到支持|不支持|假设\s*\d*\s*(?:未|不)|"
    r"与(?:预期|假设)相反|方向相反|没有(?:发现|观察到)|不存在(?:显著)?影响"
)


@verifier(
    "check_results_report_negative",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="结果章是否只报显著结果（未支持的假设是否也写了）",
)
def check_results_report_negative(text: str, ctx: Any) -> list[Issue]:
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("results",)):
        # 只有在"看起来做过假设检验"的前提下才判：完全没有检验词汇的章节不适用。
        if not re.search(r"显著|假设\s*\d|检验", body):
            continue
        if _NEGATIVE_RESULT.search(body):
            continue
        issues.append(_make(
            "writing.results_only_positive", Severity.MINOR,
            f"「{title}」通篇未见对「未得到支持」情形的报告：只报显著结果会让读者"
            "高估证据强度，也是盲审最常追问的一点",
            text=text, para_index=-1,
            evidence=body[:120], find=body[:12],
            detected_by="rules:check_results_report_negative",
            fix_hint="如实写出未获得支持的假设，并说明它意味着什么——"
                     "是数据不支持，还是度量/样本导致了差异。",
        ))
    return issues[:2]


@verifier(
    "check_disc_contrast",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="讨论章是否与既有研究做了对照（而非重复结果）",
)
def check_disc_contrast(text: str, ctx: Any) -> list[Issue]:
    contrast = re.compile(r"一致|相符|吻合|不同(?:于|的是)|相反|差异(?:可能|或许|来源)|"
                          r"与[^。；]{0,20}相(?:比|反)|支持了|印证|补充了|这与|不同于")
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("discussion",)):
        if len(body) < 200:
            continue
        if contrast.search(body):
            continue
        issues.append(_make(
            "writing.disc_no_contrast", Severity.MAJOR,
            f"「{title}」没有与既有研究对照：没有出现「与……一致」「不同于……」"
            "这类对照表述，讨论章退化成了第二个结果章",
            text=text, para_index=-1,
            evidence=body[:120], find=body[:12],
            detected_by="rules:check_disc_contrast",
            fix_hint="按文献综述梳理过的脉络逐一对照：哪些结论一致、哪些相反、"
                     "不一致可能是因为样本/时期/度量/情境的差异。这段是讨论章的核心。",
        ))
    return issues[:2]


@verifier(
    "check_disc_no_new_results",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="讨论章是否引入了结果章没有的表格或统计量",
)
def check_disc_no_new_results(text: str, ctx: Any) -> list[Issue]:
    design_and_results = sections_of_kind(text, ("results", "method"))
    discussion = sections_of_kind(text, ("discussion",))
    if not design_and_results or not discussion:
        return []
    known = " ".join(b for _, b in design_and_results)
    known_nums = set(fact_numbers(known))
    issues: list[Issue] = []
    for title, body in discussion:
        extra = sorted({n for n in fact_numbers(body) if n not in known_nums})
        # 只报"看起来是分析结果"的新数字：两位以上小数或带百分号的，才值得提醒。
        notable = [n for n in extra if re.search(r"\.\d", n)]
        if notable:
            issues.append(_make(
                "writing.disc_new_results", Severity.MAJOR,
                f"「{title}」出现了结果章未报告过的统计量（{'、'.join(notable[:5])}）："
                "讨论章不引入新数据，读者会以为这些结果已经过检验",
                text=text, para_index=-1,
                evidence="、".join(notable[:5]), find=notable[0],
                detected_by="rules:check_disc_no_new_results",
                fix_hint="把这些分析移回结果章，或在结果章补做并报告；"
                         "讨论章只解释已经报告过的结果。",
            ))
    return issues[:3]


#: 空泛局限说法。出现这些而没有具体维度 = 局限没写清楚。
_VAGUE_LIMIT = re.compile(r"存在(?:一定的?)?局限|有一定的?局限|尚(?:有|存)不足|有待(?:完善|改进|深入)|不够全面")
#: 局限的具体维度。
_LIMIT_DIM = re.compile(r"样本|时期|年份|区间|地域|区域|行业|度量|测度|口径|指标|"
                        r"识别|内生|因果|外推|外部效度|截面|面板|数据可得|未观测|遗漏")


@verifier(
    "check_limitation_specific",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="局限与展望是否具体（而非『存在一定局限性』）",
)
def check_limitation_specific(text: str, ctx: Any) -> list[Issue]:
    issues: list[Issue] = []
    for kind in ("discussion", "conclusion"):
        for title, body in sections_of_kind(text, (kind,)):
            if not body:
                continue
            for pno_para in prose_paragraphs(body, min_chars=30):
                para = pno_para[1]
                if not _VAGUE_LIMIT.search(para):
                    continue
                if _LIMIT_DIM.search(para):
                    continue
                issues.append(_make(
                    "writing.vague_limitation", Severity.MAJOR,
                    f"「{title}」的局限表述空泛：「{_VAGUE_LIMIT.search(para).group(0)}」"
                    "没有说明局限在哪里、影响什么",
                    text=text, para_index=-1,
                    evidence=para[:140], find=para[:12],
                    detected_by="rules:check_limitation_specific",
                    fix_hint="把局限落到具体维度：样本的时间/地域范围、变量度量的近似程度、"
                             "因果识别的限制、结论不能外推到什么情形。",
                ))
                break
    return issues[:4]


@verifier(
    "check_conclusion_grounding",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="结论是否引入了正文没有的事实（数字必须能在正文中找到）",
)
def check_conclusion_grounding(text: str, ctx: Any) -> list[Issue]:
    """**结论章最重要的一条机械检查。**

    结论的全部素材必须来自正文。所以结论里出现的每一个数字，都必须能在
    「结论之前的部分」里原样找到。找不到的只有两种可能：模型把数字写错了，
    或者它在结论里新造了一个结果——两种都必须由作者处理。

    这条检查之所以能做成纯规则，是因为数字的**字面比对**是可判定的：
    不需要理解语义，就能证伪"这个数从没在正文里出现过"。
    """
    sections = split_by_sections(text)
    if len(sections) < 2:
        return []
    from ..writing import classify

    concl: list[tuple[str, str]] = []
    body_parts: list[str] = []
    for title, body in sections:
        if not title:
            body_parts.append(body)
            continue
        try:
            kind = classify(title).kind
        except Exception:  # noqa: BLE001
            kind = "body"
        if kind == "conclusion":
            concl.append((title, body))
        else:
            body_parts.append(body)
    if not concl or not body_parts:
        return []

    body_nums = set(fact_numbers(" ".join(body_parts)))
    issues: list[Issue] = []
    for title, body in concl:
        for para in split_paragraphs(body):
            for s in split_sentences(para):
                extra = [n for n in fact_numbers(s) if n not in body_nums]
                if not extra:
                    continue
                issues.append(_make(
                    "writing.conclusion_new_number", Severity.BLOCKER,
                    f"「{title}」出现正文中没有的数字「{'、'.join(extra[:4])}」："
                    "结论不得引入正文没有的事实，请核对这是笔误还是新造的结果",
                    text=text, para_index=-1,
                    evidence=s[:160], find=s[:12],
                    detected_by="rules:check_conclusion_grounding",
                    fix_hint="回到正文核对这个数字：正文里没有就算错，必须删掉或改回正文的数值；"
                             "如果这条结论本来就需要一个正文没有的数字支撑，"
                             "说明它还缺一步分析，应当补在正文而不是结论里。",
                ))
    return issues[:8]


@verifier(
    "check_number_grounding",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="正文里的数字是否能在本章证据中找到（防编造量级）",
)
def check_number_grounding(text: str, ctx: Any) -> list[Issue]:
    """**正文里的数字必须能在本章检索到的证据里找到。**

    这条对应 Supervisor-Skills ``paper-writer`` 的一条硬规则：
    「具体细节不写用户没给的：不编造场景、机制、量级、流程、标识符」，以及
    「措辞强度不得超过证据强度」。其中**量级是唯一能机械验证的一类**——
    数字的字面比对不需要理解语义，就能证伪"这个数从没在证据里出现过"。

    判据刻意保守，只查**两位数以上或带小数点的数**：

    * 1~9 的小数字绝大多数是枚举（"3 个方面"）或序数，是误报的主要来源，一律不查；
    * 年份、百分比、样本量、系数这些通常是两位以上，正好落在检查范围内。

    这么做是因为**误报会毁掉工具可信度**：一条"这个 3 没有依据"的假警报，
    会让作者连同真问题一起忽略。

    只在**验证阶段**运行（需要整篇稿件与分章证据台账）；起草阶段的单章文本
    没有章节标题，`sections_of_kind` 会返回空，检查自动跳过。

    **证据范围取全篇，不取本章。** 这一点是实测踩出来的：第一版只用本章
    检索到的证据，结果把"有研究得到 23008 个观测值[14]"这类**引自已检索文献的
    样本量**全判成了编造——而那些数字确实有出处，只是那条证据是在**另一章**
    检索到的。证据池本来就是全篇共享的，按章切分是错的。
    """
    meta = getattr(getattr(getattr(ctx, "board", None), "project", None), "meta", None) or {}
    ledger = meta.get("writing_evidence") or {}
    if not ledger:
        return []

    def _nums(s: str) -> set[str]:
        out: set[str] = set()
        for n in fact_numbers(s):
            # 只查两位数以上或带小数点的数：小数字多是枚举与序数，查它必然误报。
            if "." in n or len(n.lstrip("0")) >= 2:
                out.add(n.rstrip("0").rstrip(".") if "." in n else n)
        return out

    # 全篇证据池：任何一章检索到的证据都算数。
    evidence_blob = " ".join(
        str(e.get("text") or "") + " " + str(e.get("citation") or "")
        for entries in ledger.values()
        for e in (entries or [])
        if isinstance(e, dict)
    )
    evidence_nums = _nums(evidence_blob)
    if not evidence_nums:
        return []

    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("introduction", "literature_review", "method",
                                               "results", "discussion", "conclusion", "body")):
        missing: list[str] = []
        sample = ""
        for para in split_paragraphs(body):
            for sent in split_sentences(para):
                extra = sorted(n for n in _nums(sent) if n not in evidence_nums)
                if extra:
                    missing.extend(extra)
                    if not sample:
                        sample = sent.strip()[:150]
        if not missing:
            continue
        uniq = sorted(set(missing))
        issues.append(_make(
            "writing.number_without_source", Severity.MAJOR,
            f"「{title}」出现本章证据中没有的数字：{'、'.join(uniq[:6])}"
            f"（共 {len(uniq)} 个不同数值）——可能是模型自己算的或写错的",
            text=text, para_index=-1,
            evidence=sample or "、".join(uniq[:6]), find=uniq[0],
            detected_by="rules:check_number_grounding",
            fix_hint="逐一回到证据核对：数字写错了就改回证据里的值；"
                     "证据里根本没有这个数，说明它是模型自己造的，必须删掉或改成"
                     "不需要该数字的表述。**不要为了「看起来完整」保留一个来源不明的数字。**"
                     "（这条规则参考 HKUST(GZ) Supervisor-Skills 的 paper-writer："
                     "措辞强度不得超过证据强度，不编造量级）",
        ))
    return issues[:6]


@verifier(
    "check_conclusion_traceable",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="结论的主要条目是否标明依据的正文章节",
)
def check_conclusion_traceable(text: str, ctx: Any) -> list[Issue]:
    trace = re.compile(r"第\s*[一二三四五六七八九十\d]+\s*[章节篇]|见\s*第|如前文|参见")
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("conclusion",)):
        if len(body) < 150:
            continue
        items = [p for _, p in prose_paragraphs(body, min_chars=30)]
        if not items:
            continue
        marked = sum(1 for p in items if trace.search(p))
        if marked == 0:
            issues.append(_make(
                "writing.conclusion_untraceable", Severity.MAJOR,
                f"「{title}」的结论条目没有标明依据的章节：评审无法快速核对"
                "每条结论对应正文的哪一部分",
                text=text, para_index=-1,
                evidence=body[:120], find=body[:12],
                detected_by="rules:check_conclusion_traceable",
                fix_hint="在每条主要结论后补上依据指向，形如「（见第 4 章）」或「（见第 5.2 节）」。"
                         "这既是给评审的方便，也是对你自己的约束：写不出指向的结论往往没依据。",
            ))
    return issues[:2]


#: 创新点/首创声明——**只有作者能写**。工具生成出来即为越界。
_INNOVATION_CLAIM = re.compile(
    r"创新(?:点|之处|性)?|首次提出|首创|填补(?:了)?[^。；]{0,12}空白|"
    r"原创性(?:贡献|成果)|主要贡献在于|独到之处"
)


@verifier(
    "check_conclusion_no_innovation",
    kinds=("draft",),
    needs_llm=False,
    cost="local",
    description="结论章是否出现了不该由工具生成的创新点声明",
)
def check_conclusion_no_innovation(text: str, ctx: Any) -> list[Issue]:
    """创新点声明是**作者的学术主张**，工具不代写。

    这条检查存在的意义不是"约束作者"，而是**约束工具自己**：一旦生成环节越界，
    这里必须能抓到。作者自己写创新点是完全正常的，所以它只在结论章里判——
    绪论里出现"本文的创新之处"是作者的选择，不该被报。
    """
    issues: list[Issue] = []
    for title, body in sections_of_kind(text, ("conclusion",)):
        for pno_para in prose_paragraphs(body, min_chars=20):
            para = pno_para[1]
            m = _INNOVATION_CLAIM.search(para)
            if not m:
                continue
            # 「创新」单独出现可能是"技术创新""创新绩效"这类研究对象词，
            # 只有与本文/本研究同句、或出现在"创新点/创新之处"这类短语里才算声明。
            if m.group(0) in ("创新", "创新性") and not (
                _SELF_REF.search(para) or re.search(r"创新(?:点|之处)", para)
            ):
                continue
            issues.append(_make(
                "writing.conclusion_innovation_claim", Severity.BLOCKER,
                f"「{title}」出现创新点声明「{m.group(0)}」：创新点是作者本人的学术主张，"
                "必须由作者自己写，工具不得代写",
                text=text, para_index=-1,
                evidence=para[:160], find=m.group(0),
                detected_by="rules:check_conclusion_no_innovation",
                fix_hint="删掉这句，改为陈述已经论证过的事实；创新点请作者自己补写。"
                         "如果这句话确实是作者的原意，请作者用自己的措辞写进稿子。",
            ))
    return issues[:4]
