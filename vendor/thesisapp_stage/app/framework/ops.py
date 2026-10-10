"""修复算子（fix ops）。

自我更正循环里最危险的一步就是"让模型改稿子"。这个模块的作用是把危险关进笼子：
模型不能直接改文本，只能**申请**调用这里注册过的算子，而每个算子都是：

1. **纯函数**：`(text, args) -> OpResult`，不改外部状态，可单测。
2. **范围受限**：必须给出定位（下标 / 匹配串 / 行号），不允许"全文替换"式的大改。
3. **可验证**：返回改后的文本，能立刻被同一批检查器复量。
4. **幂等**：重复应用同一个算子不会持续变化（`changed=False` 表示无改动）。

刻意不提供"重写这一段"这种算子。段落重写属于降 AIGC 那条链路的职责，
不属于"更正错误"。
"""

from __future__ import annotations

import re
from typing import Any

from .artifact import OpResult
from .registry import fixer


class OpError(RuntimeError):
    pass


def _normalize(text: str) -> str:
    return text or ""


@fixer(
    "replace_span",
    description="替换一处精确匹配的文本。必须提供 find，且可要求出现次数上限。",
    arg_schema={
        "find": "str，要替换的原文（必须精确匹配，含标点）",
        "replace": "str，替换为",
        "count": "int，期望的匹配次数；实际次数不等于它则拒绝执行，默认 1",
        "occurrence": "int，替换第几次出现（1 起）；留空表示要求全文唯一",
    },
)
def op_replace_span(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    find = str(args.get("find", ""))
    replace = str(args.get("replace", ""))
    if not find:
        return OpResult(False, text, error="replace_span 缺少 find 参数")

    actual = text.count(find)
    if actual == 0:
        return OpResult(False, text, error=f"原文中找不到目标文本：{find[:60]!r}")

    # 默认要求唯一：定位不唯一时替换极易改错地方，宁可拒绝。
    if "occurrence" in args and args["occurrence"]:
        occ = int(args["occurrence"])
        if occ < 1 or occ > actual:
            return OpResult(False, text, error=f"occurrence={occ} 超出实际出现次数 {actual}")
    else:
        expected = int(args.get("count", 1))
        if actual != expected:
            return OpResult(
                False,
                text,
                error=f"目标文本出现 {actual} 次，期望 {expected} 次；定位不唯一，拒绝替换",
            )
        occ = 1

    idx = -1
    for _ in range(occ):
        idx = text.find(find, idx + 1)
    new_text = text[:idx] + replace + text[idx + len(find) :]
    return OpResult(True, new_text, changed=new_text != text, detail=f"替换第 {occ} 处")


@fixer(
    "delete_span",
    description="删除一处精确匹配的文本（用于删除多余空行、重复编号等）。",
    arg_schema={"find": "str，要删除的原文", "count": "int，期望匹配次数，默认 1"},
)
def op_delete_span(text: str, args: dict[str, Any]) -> OpResult:
    args = dict(args or {})
    args["replace"] = ""
    return op_replace_span(text, args)


@fixer(
    "renumber_sequence",
    description="把一组编号改成连续。用于参考文献序号、图表编号、章节号的断号与重号。",
    arg_schema={
        "pattern": "str，正则，需含一个捕获组用于匹配编号本身",
        "start": "int，起始编号，默认 1",
        "scope_start": "int，仅在文本的该字符偏移之后生效；留空为全文",
        "scope_end": "int，仅在文本的该字符偏移之前生效；留空为全文",
    },
)
def op_renumber_sequence(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return OpResult(False, text, error="renumber_sequence 缺少 pattern")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return OpResult(False, text, error=f"正则不合法: {exc}")

    start = int(args.get("start", 1))
    s = int(args.get("scope_start") or 0)
    e = int(args.get("scope_end") or len(text))
    s = max(0, min(s, len(text)))
    e = max(s, min(e, len(text)))
    head, body, tail = text[:s], text[s:e], text[e:]

    counter = start

    def sub(m: re.Match) -> str:
        nonlocal counter
        spans = m.groups()
        if not spans or spans[0] is None:
            return m.group(0)
        g_start = m.start(1)
        g_end = m.end(1)
        whole = m.group(0)
        prefix = whole[: g_start - m.start()]
        suffix = whole[g_end - m.start() :]
        value = str(counter)
        counter += 1
        return prefix + value + suffix

    new_body = rx.sub(sub, body)
    new_text = head + new_body + tail
    n = counter - start
    if n == 0:
        return OpResult(False, text, error="该范围内没有匹配到可编号的位置")
    return OpResult(True, new_text, changed=new_text != text, detail=f"重排 {n} 处编号")


@fixer(
    "normalize_punctuation",
    description="统一中英文标点与全半角（不触碰数字、公式、英文术语）。",
    arg_schema={
        "cjk_quotes": "bool，把英文引号改为中文引号，默认 true",
        "fullwidth": "bool，把英文逗号/分号/括号改为全角，默认 true",
    },
)
def op_normalize_punctuation(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    out = text
    if args.get("fullwidth", True):
        out = re.sub(r"(?<=[\u4e00-\u9fff]),(?=[\u4e00-\u9fff])", "，", out)
        out = re.sub(r"(?<=[\u4e00-\u9fff]);(?=[\u4e00-\u9fff])", "；", out)
        out = re.sub(r"(?<=[\u4e00-\u9fff])!(?=[\u4e00-\u9fff])", "！", out)
        out = re.sub(r"(?<=[\u4e00-\u9fff])\?(?=[\u4e00-\u9fff])", "？", out)
    if args.get("cjk_quotes", True):
        # Only touch quote pairs that sit next to CJK text, so English
        # quotations and code fragments keep their straight quotes.
        out = re.sub(r'"([^"\n]{1,80}?)"', lambda m: f"“{m.group(1)}”" if re.search(r"[\u4e00-\u9fff]", m.group(1)) else m.group(0), out)
    return OpResult(True, out, changed=out != text, detail="标点规范化")


@fixer(
    "strip_filler",
    description="删除模板腔套话（如「综上所述」「值得注意的是」「众所周知」）。只删短语，不改写句子。",
    arg_schema={"phrases": "list[str]，要删除的套话；留空用内置清单"},
)
def op_strip_filler(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    phrases = args.get("phrases") or [
        "综上所述，",
        "综上所述,",
        "值得注意的是，",
        "众所周知，",
        "不言而喻，",
        "显而易见，",
        "总而言之，",
        "换言之，",
    ]
    out = text
    hits: list[str] = []
    for p in phrases:
        if p and p in out:
            out = out.replace(p, "")
            hits.append(p)
    if not hits:
        return OpResult(True, text, changed=False, detail="没有命中套话")
    return OpResult(True, out, changed=True, detail=f"删除套话 {len(hits)} 处：{'、'.join(hits[:5])}")


@fixer(
    "unify_term",
    description="把某个术语的异体写法统一为规范写法。",
    arg_schema={"variants": "list[str]，异体写法", "canonical": "str，规范写法"},
)
def op_unify_term(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    canonical = str(args.get("canonical", ""))
    variants = [str(v) for v in (args.get("variants") or []) if v]
    if not canonical or not variants:
        return OpResult(False, text, error="unify_term 需要 variants 与 canonical")
    out = text
    hits = 0
    for v in variants:
        if v == canonical:
            continue
        c = out.count(v)
        if c:
            out = out.replace(v, canonical)
            hits += c
    return OpResult(True, out, changed=out != text, detail=f"统一术语 {hits} 处 → {canonical}")


@fixer(
    "insert_after",
    description="在一处精确匹配的文本之后插入内容（用于补上缺失的参考文献条目、图表标题）。",
    arg_schema={"anchor": "str，锚点原文（须唯一）", "content": "str，插入内容"},
)
def op_insert_after(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    anchor = str(args.get("anchor", ""))
    content = str(args.get("content", ""))
    if not anchor:
        return OpResult(False, text, error="insert_after 缺少 anchor")
    n = text.count(anchor)
    if n != 1:
        return OpResult(False, text, error=f"锚点出现 {n} 次，需唯一才能安全插入")
    i = text.find(anchor) + len(anchor)
    new_text = text[:i] + content + text[i:]
    return OpResult(True, new_text, changed=True, detail="已插入")


@fixer(
    "merge_duplicate_paragraphs",
    description="删除连续重复的段落（复制粘贴造成的整段重出）。空行不中断连续判定。",
    arg_schema={"min_chars": "int，短于该长度不参与去重，默认 20"},
)
def op_merge_duplicate_paragraphs(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    min_chars = int(args.get("min_chars", 20))
    paras = text.split("\n")
    out: list[str] = []
    removed = 0
    # 空行是段落之间最常见的分隔符，不能让它中断"连续重复"的判定，
    # 否则「A。\n\nA。」这种最典型的复制粘贴重出会被漏掉。
    last_content = ""
    for p in paras:
        stripped = p.strip()
        if not stripped:
            out.append(p)
            continue
        if len(stripped) >= min_chars and stripped == last_content:
            removed += 1
            continue
        out.append(p)
        last_content = stripped
    new_text = "\n".join(out)
    if not removed:
        return OpResult(True, text, changed=False, detail="没有连续重复段落")
    return OpResult(True, new_text, changed=True, detail=f"删除连续重复段落 {removed} 处")


@fixer(
    "apply_edits",
    description="批量应用多处互不重叠的精确替换。由模型给出 edits 列表，逐条校验后一次落地。",
    arg_schema={
        "edits": "list[{find, replace}]，每条 find 必须在原文中唯一出现",
    },
)
def op_apply_edits(text: str, args: dict[str, Any]) -> OpResult:
    text = _normalize(text)
    edits = args.get("edits") or []
    if not isinstance(edits, list) or not edits:
        return OpResult(False, text, error="apply_edits 需要非空 edits 列表")

    cur = text
    done = 0
    for i, ed in enumerate(edits):
        if not isinstance(ed, dict):
            return OpResult(False, text, error=f"第 {i+1} 条 edit 不是对象")
        find = str(ed.get("find", ""))
        rep = str(ed.get("replace", ""))
        if not find:
            return OpResult(False, text, error=f"第 {i+1} 条 edit 缺少 find")
        # 逐条校验后再落地：任一条不合法则整批回滚，避免半改状态。
        n = cur.count(find)
        if n == 0:
            return OpResult(False, text, error=f"第 {i+1} 条 find 在原文中不存在：{find[:60]!r}")
        if n > 1:
            return OpResult(
                False, text, error=f"第 {i+1} 条 find 出现 {n} 次，定位不唯一：{find[:60]!r}"
            )
        cur = cur.replace(find, rep, 1)
        done += 1

    return OpResult(True, cur, changed=cur != text, detail=f"应用 {done} 处修改")


#: name -> fn。真正的分发表由 registry 维护，这里只是给调用方一个稳定入口。
OPS = {
    "replace_span": op_replace_span,
    "delete_span": op_delete_span,
    "renumber_sequence": op_renumber_sequence,
    "normalize_punctuation": op_normalize_punctuation,
    "strip_filler": op_strip_filler,
    "unify_term": op_unify_term,
    "insert_after": op_insert_after,
    "merge_duplicate_paragraphs": op_merge_duplicate_paragraphs,
    "apply_edits": op_apply_edits,
}


def apply_op(text: str, op: dict[str, Any]) -> OpResult:
    """Apply one op dict (``{"op": name, ...args}``) to ``text``."""
    if not isinstance(op, dict):
        return OpResult(False, text, error=f"算子描述必须是对象，收到 {type(op).__name__}")
    name = str(op.get("op") or op.get("name") or "").strip()
    if not name:
        return OpResult(False, text, error="算子缺少 op 字段")
    fn = OPS.get(name)
    if fn is None:
        # Fall back to the registry so decorator-only ops stay reachable.
        from .registry import FIXERS

        entry = FIXERS.get(name)
        if entry is None:
            return OpResult(False, text, error=f"未注册的算子: {name}")
        fn = entry["fn"]
    args = {k: v for k, v in op.items() if k not in ("op", "name")}
    try:
        return fn(text, args)
    except Exception as exc:  # noqa: BLE001 - an op must never kill the pipeline
        return OpResult(False, text, error=f"算子 {name} 执行异常: {type(exc).__name__}: {exc}")
