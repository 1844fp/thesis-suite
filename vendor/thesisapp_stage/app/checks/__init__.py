"""静态一致性检查器集合。

**导入本包即完成全部注册。** 这一层是"离线可跑"的那一半验证能力：不联网、
不调用大模型、不需要 API Key，因此可以在任何阶段、任何次数地重跑。框架的
自我更正循环靠它做到"改完能用同一把尺子复量"。

模块划分（每个文件的取舍都写在各自 docstring 里）：

* ``references``  —— 参考文献：编号对应、重复条目、条目形态、作者-年份孤儿引用
* ``numbers``     —— 图表/公式编号：连续性、题注位置
* ``text_hygiene`` —— 标点、空白、空段、重复段、套话、超长句、配对符号
* ``terminology`` —— 术语异体写法、缩写首次定义
* ``structure``   —— 必需章节、标题层级、摘要长度、关键词
* ``writing_quality`` —— **写作质量**：引言三语步、综述是否逐篇罗列（「一篇论文半句话」）、
  段落有无主题句与分析、论证是否推进。与上面几类的区别：上面查的是**合规性**
  （引用真假、编号、格式），这一类查的是**写得好不好**。两者的判定都产出框架
  ``Issue``，因此能一起进入自我更正循环。

**新增检查器模块时必须在这里 import。** 注册靠装饰器在导入时执行，
漏了 import **不会报错**，只会让检查器静默地不存在——本文件就踩过：
``writing_quality`` 写完后忘了加进来，于是写作助手的自查环节一个写作检查器都没跑到，
表现为"自查通过"，比报错危险得多。

三条统一纪律（写在最显眼的地方，新增检查器时请遵守）：

1. **每条 Issue 的 ``evidence`` 必须是原文里的逐字片段**，``location.find``
   必须是**唯一**的短定位串。框架的复验机制靠这两个锚点判断"问题改完还在不在"；
   写不出唯一锚点时，宁可把 ``fixable`` 设为 False，也不要赌一个会改错地方的 find。
2. **严重度与后果匹配**。引用不存在是 BLOCKER，编号错是 MAJOR，标点空格是
   MINOR/INFO。全都报 MAJOR 会让自我更正循环永远无法收敛，等于把整套机制废掉。
3. **``fixable=True`` 必须给出已注册的 ``fix_op``**（见 ``framework.ops.OPS``）。
   需要作者判断的问题一律 ``fixable=False`` + ``fix_hint``。

导入顺序无所谓（注册表按名字去重），但保持字母序便于人肉核对。
"""

from __future__ import annotations

from . import (  # noqa: F401
    numbers,
    references,
    structure,
    terminology,
    text_hygiene,
    writing_quality,
)

#: 本包注册的检查器名单。测试与 CLI 用它断言"注册确实发生了"，
#: 而不用去猜注册表里有几个是别人的。
CHECK_NAMES: tuple[str, ...] = (
    "check_ref_numbering",
    "check_ref_duplicates",
    "check_ref_format",
    "check_citation_orphans",
    "check_figure_table_numbering",
    "check_caption_placement",
    "check_equation_numbering",
    "check_punctuation_mix",
    "check_whitespace",
    "check_empty_paragraphs",
    "check_duplicate_paragraphs",
    "check_ai_filler",
    "check_sentence_length",
    "check_quote_pairs",
    "check_term_variants",
    "check_abbreviation_first_use",
    "check_required_sections",
    "check_heading_levels",
    "check_abstract_length",
    "check_keywords",
    # 写作质量（不是合规性，是"写得好不好"）
    "check_intro_cars",
    "check_intro_opening",
    "check_intro_gap_specific",
    "check_intro_no_citation_dump",
    "check_lit_synthesis",
    "check_lit_coherence",
    "check_lit_critique",
    "check_paragraph_topic_sentence",
    "check_paragraph_analysis",
    "check_claim_evidence",
    "check_body_progression",
    "check_template_connectives",
    "check_paragraph_variance",
    # 章节级/章间一致性：研究设计、结果、讨论、结论的专属要求。
    # 这些检查需要"整篇稿件 + 章节标题"才能判定，起草阶段的单章自查会自动跳过，
    # 在验证阶段拿到整篇稿件时真正执行（理由见 writing_quality 里 sections_of_kind 的说明）。
    "check_overclaim",
    "check_method_specific",
    "check_method_choice_reason",
    "check_method_no_textbook_dump",
    "check_results_no_new_method",
    "check_results_report_negative",
    "check_disc_contrast",
    "check_disc_no_new_results",
    "check_limitation_specific",
    "check_conclusion_grounding",
    "check_conclusion_traceable",
    "check_conclusion_no_innovation",
    # 技术类引言的「局限—挑战—方案—贡献」闭环 + AI 腔词表 + 数字溯源。
    # 规则来源：HKUST(GZ) DIAL 实验室的 Supervisor-Skills 项目
    # （skills/intro-drafter、tech-paper-template、paper-writer、
    # pre-submission-reviewer；许可 CC BY-NC-SA 4.0 / CC BY 4.0）。
    # 这里只落其中**可机械判定**的部分；链条的语义判断不做，不假装能判。
    "check_limitation_challenge_chain",
    "check_contributions_specific",
    "check_contributions_map_sections",
    "check_ai_tone_vocabulary",
    "check_number_grounding",
)

__all__ = [
    "CHECK_NAMES",
    "numbers",
    "references",
    "structure",
    "terminology",
    "text_hygiene",
    "writing_quality",
]
