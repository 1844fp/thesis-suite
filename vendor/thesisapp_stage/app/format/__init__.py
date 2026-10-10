"""格式整理智能体（框架的第八个能力）。

分三层，每层职责单一、都能单独测：

* ``rules``    —— 规则引擎。规则是 YAML 数据，引擎是纯函数。改规范不改代码。
* ``checkers`` —— 把规则引擎接到框架的检查器注册表上（``@verifier``）。
* ``rules_default.yaml`` —— 通用学位论文规范基线，也是学校模板规则的样板。

用法（收稿阶段写入格式事实，检查阶段自动生效）：

    from app.parsing import read_docx
    from app.format import register_format_facts, DEFAULT_RULESET

    facts = read_docx("论文.docx")
    register_format_facts(board.project.meta, facts, ruleset=DEFAULT_RULESET)

    # 之后 pipeline.verify() 会自动跑 check_format_docx / check_format_text
导入本包会同时注册两个检查器（``check_format_docx`` / ``check_format_text``）。
检查器名一律以 ``check_`` 开头，以免与其它模块的注册名相撞。

**接入学校官方 .docx 模板的入口**：把学校规范写成 RuleSet（可以照着
``rules_default.yaml`` 改），通过 ``project.meta["format_ruleset"]`` 或
``project.meta["format_ruleset_path"]`` 注入。检查器优先用注入的规则集，
没有才退回内置通用规范——见 ``checkers._ruleset_of``。
"""

from .checkers import (
    FORMAT_FACTS_KEY,
    check_format_docx,
    check_format_text,
    register_format_facts,
)
from .rules import (
    ALLOWED_FIX_OPS,
    DEFAULT_RULESET,
    MAX_ISSUES_PER_RULE,
    FormatRule,
    RuleSet,
    RuleSetError,
    default_ruleset_path,
    evaluate,
    infer_ruleset_from_template,
    load_default_ruleset,
    summarize_issues,
)
from .writeback import (
    WRITABLE_RULE_IDS,
    FormatScheme,
    WritebackReport,
    apply_scheme,
    detect_heading_levels,
    inspect_docx,
    scheme_from_ruleset,
    scheme_from_template,
)

__all__ = [
    "ALLOWED_FIX_OPS",
    "DEFAULT_RULESET",
    "FORMAT_FACTS_KEY",
    "MAX_ISSUES_PER_RULE",
    "WRITABLE_RULE_IDS",
    "FormatRule",
    "FormatScheme",
    "RuleSet",
    "RuleSetError",
    "WritebackReport",
    "apply_scheme",
    "check_format_docx",
    "check_format_text",
    "default_ruleset_path",
    "detect_heading_levels",
    "evaluate",
    "infer_ruleset_from_template",
    "inspect_docx",
    "load_default_ruleset",
    "register_format_facts",
    "scheme_from_ruleset",
    "scheme_from_template",
    "summarize_issues",
]
