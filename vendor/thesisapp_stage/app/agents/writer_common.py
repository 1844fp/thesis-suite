"""写作智能体共用的零件：证据台账、引用角标校验、章节筛选。

为什么这些逻辑要单独放一层，而不是复制到三个写作智能体里：

1. **引用角标校验必须是同一个实现。** 起草时校验一遍、润色后再校验一遍、
   验证环节还要再判一次"论断有没有依据"。如果三处各写一份正则，三份不一致
   时无法判断谁对——这和"降 AIGC 指数只能由一个系统测量"是同一个道理。

2. **证据台账的字段是跨阶段契约。** 规划阶段报"这一章有几条证据"，起草阶段
   把证据原文存进 `meta["writing_evidence"]`，验证阶段拿它判断论断是否有依据。
   字段名一旦分散定义，改名时必然漏掉一处。

3. **"哪些章节不能替作者写"是一条学术规则，不是某个智能体的实现细节。**
   结论、创新点、未来展望这类章节属于作者的判断，任何写作智能体都不许碰。
   规则只有一处定义，才可能被一致地执行。

本模块**不做 IO、不调用模型、不依赖具体适配器**，因此可以被独立断言。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

#: 角标解析与参考文献区识别**只有一处实现**，在 ``app/writing/citations.py``。
#: 原先本模块自己写了一份正则，起草校验、润色校验、定稿重排各写一份——三份一旦
#: 不一致，就无法判断谁对，而"同一个角标在三个阶段被解析成不同编号"会让报告里
#: 所有数字失真。本模块只负责**校验**（该不该保留），连续化重排在 citations 里做。
from ..writing.citations import (
    CITE_MARKER as _CITE_MARKER,
    drop_marker as _drop_marker,
    find_references_block,
    is_ref_entry as _is_ref_entry,
    parse_marker as _parse_marker,
    tidy_line as _tidy_line,
)

# --------------------------------------------------------------------------
# 章节标题的规范化与"禁止代写"清单
# --------------------------------------------------------------------------

#: 章节号前缀：``1`` / ``1.2`` / ``一、`` / ``（三）`` / ``第 3 章``。
#: 规划与检索都只要纯标题，编号留着会让检索词变噪（"3.2 " 对向量召回没有信息量）。
_NUMBER_PREFIX = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百]+\s*[章节篇]|"
    r"[0-9]+(?:\.[0-9]+)*|[一二三四五六七八九十]+)\s*[、.．,，:：)）]?\s+"
)
_ANY_NUMBER_PREFIX = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百]+\s*[章节篇]|"
    r"[0-9]+(?:\.[0-9]+)*|[一二三四五六七八九十]+)\s*[、.．,，:：)）]?\s*"
)

#: **绝不由系统生成的章节。**
#:
#: 理由：学位论文的创新点声明与致谢是作者的学术主张与人际事实，模型写出来的
#: "本文的创新在于……"无论措辞多保守，都是在替作者声称贡献。这类内容一旦被写进
#: 稿子，作者很难发现哪句不是自己的意思，而评审最容易在这里追问。
#: 所以这里不是"生成得差一点"，而是**根本不生成**。
NEVER_SECTIONS: tuple[str, ...] = (
    "创新点",
    "创新之处",
    "拟创新",
    "主要创新",
    "创新性贡献",
    "独创性",
    "致谢",
    "参考文献",
    "作者简介",
    "授权使用声明",
    "声明",
    "附录",
)

#: **生成"结构草稿"，但每一条都必须由作者确认**的章节。
#:
#: 为什么结论和创新点不同级：创新点是**作者的学术主张**（"这个问题上我提出了什么
#: 新东西"），只能由作者声明；而结论的主体是**对本文已经写出的论断做汇总**——
#: 素材来自作者自己的正文，工具的职责是"别漏、别变形"，不是"替作者下判断"。
#:
#: 因此这类章节交给 ``writer:conclude`` 单独处理，并且：
#: * 只能使用正文里已经出现过的数字与引用，出现正文没有的即报致命问题并就地标注；
#: * 出现创新点声明视为越界（``check_conclusion_no_innovation``）；
#: * 整章产出后统一标记 ``needs_author``，提示作者逐条核对。
STRUCTURE_DRAFT_SECTIONS: tuple[str, ...] = (
    "结论",
    "结语",
    "总结",
    "研究结论",
    "结论与展望",
    "总结与展望",
    "结论与启示",
    "研究不足",
    "不足与展望",
    "研究展望",
    "未来展望",
    "展望",
    "研究启示",
)

#: ``writer:draft`` **不得代写**的全部章节。
#:
#: 两个层级在这里合并成一个判断，因为对"逐章起草"这个环节而言，两类章节的处理
#: 是一样的：都不要它写。区别在于第二类随后会由 ``writer:conclude`` 生成结构草稿。
AUTHOR_ONLY_SECTIONS: tuple[str, ...] = NEVER_SECTIONS + STRUCTURE_DRAFT_SECTIONS

#: 章节定位可用的关键词。归到哪一类只影响"默认要什么证据"，不影响能否写。
SECTION_HINTS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("绪论", "引言", "背景", "选题", "研究意义", "问题提出"), "研究背景与问题界定"),
    (("文献", "综述", "研究现状", "述评", "理论基础"), "既有研究的主要观点与分歧"),
    (("设计", "方法", "方法论", "框架", "模型", "假设", "变量"), "研究方法、数据与变量定义"),
    (("实证", "实验", "结果", "分析", "检验", "案例", "调查"), "数据结果与稳健性检验"),
    (("讨论", "机制", "解释"), "结果的机制解释与对照"),
)


def clean_title(raw: Any) -> str:
    """去掉章节号前缀与首尾空白，失败时返回空串。

    去掉编号而不是保留，是因为编号会一路传到检索词里（``"3.2 研究设计"``
    向量召回的效果明显差于 ``"研究设计"``），也会让"同一章节"在不同阶段的
    键名对不上，台账就对不齐。
    """
    if raw is None:
        return ""
    text = str(raw).replace("\n", " ").strip()
    if not text:
        return ""
    text = _ANY_NUMBER_PREFIX.sub("", text, count=1)
    return text.strip(" \t.。:：、-—")


def generation_policy(title: str) -> str:
    """这一章该怎么生成：``auto`` / ``structure_draft`` / ``never``。

    三个档次分别对应三种处理：

    * ``auto``            —— ``writer:draft`` 正常起草（绪论、综述、研究设计、结果、讨论）；
    * ``structure_draft`` —— ``writer:conclude`` 只依据正文汇总，逐条待作者确认（结论/总结）；
    * ``never``           —— 任何环节都不生成（创新点、致谢、参考文献表之外的附录等）。

    做成一个函数而不是两个 bool 判断，是为了让"新增一类章节时到底归哪档"成为
    一次明确的决定，而不是在两个 ``if`` 里各判一次、逐渐漂移。

    **匹配用子串而不是前缀**，这一点是实测踩出来的：开题报告里的章节常写成
    「拟创新之处」「本文的创新点」「5.3 未来展望」这类带修饰的形态，
    而前缀匹配只认「创新点」开头，于是「拟创新之处」被判成 ``auto``——
    也就是说起草环节会**真的去替作者写创新点**。这正是本模块最不能出的错。
    """
    t = clean_title(title)
    if not t:
        return "auto"
    for key in NEVER_SECTIONS:
        if key in t:
            return "never"
    for key in STRUCTURE_DRAFT_SECTIONS:
        if key in t:
            return "structure_draft"
    return "auto"


def is_author_only(title: str) -> bool:
    """这一章是否**不允许 writer:draft 代写**（结论 / 创新点 / 展望 / 致谢 / 参考文献）。

    注意它的含义是"逐章起草环节不要碰"，而不是"永远不会被生成"：
    ``structure_draft`` 那一档随后会由 ``writer:conclude`` 生成需作者确认的结构草稿。
    需要区分时用 :func:`generation_policy`。
    """
    return generation_policy(title) != "auto"


def is_structure_draft(title: str) -> bool:
    """这一章是否属于"可生成结构草稿、但必须作者逐条确认"的类型（结论/总结/展望）。"""
    return generation_policy(title) == "structure_draft"


# --------------------------------------------------------------------------
# 章节选择：让作者只写/只改他关心的那几章
# --------------------------------------------------------------------------


def selected_sections(
    sections: Sequence[dict],
    brief: dict | None = None,
) -> tuple[list[dict], list[dict]]:
    """按作者的选择筛出要写的章节，返回 ``(要写的, 被排除的)``。

    选择来自写作意图（``brief``）里的两个键，**都按"标题包含"匹配**，
    因为作者天然会写「第三章」或「实证结果」这种部分名称：

    * ``chapters`` —— 只写这些章（白名单，空表示全部）；
    * ``skip_chapters`` —— 跳过这些章（黑名单，优先于白名单）。

    为什么要这个功能：作者最常见的用法不是"从零生成整篇"，而是
    **"我只要写第 3 章"** 或 **"第 5 章我自己写，别动它"**。
    没有这个开关时，重跑起草会把已经满意的章节全部重写一遍——既费钱，
    又把作者改好的文字覆盖掉。而且**覆盖是不可逆的焦虑来源**：
    作者会因此不敢重跑，工具也就失去了价值。

    匹配不到任何章节时**不报错、不返回空**，而是退回"全部"：
    作者写错了章名，得到的是"全都写了"这个可预期的结果 + 一条报告说明，
    比"什么都没写"容易理解得多。
    """
    brief = brief if isinstance(brief, dict) else {}
    want = _str_list(brief.get("chapters"))
    skip = _str_list(brief.get("skip_chapters"))
    if not want and not skip:
        return list(sections), []

    def hit(title: str, keys: Sequence[str]) -> bool:
        t = clean_title(title)
        return any(k and (k in t or t in k) for k in keys)

    chosen: list[dict] = []
    dropped: list[dict] = []
    for s in sections:
        title = clean_title(s.get("title"))
        if skip and hit(title, skip):
            dropped.append(s)
            continue
        if want and not hit(title, want):
            dropped.append(s)
            continue
        chosen.append(s)
    if not chosen:
        # 白名单一个都没匹配上：退回全部，并把这件事写进 brief 让报告能说明。
        return list(sections), []
    return chosen, dropped


def _str_list(value: Any) -> list[str]:
    """把 ``["a","b"]`` / ``"a；b"`` / ``"a,b"`` 都摊成字符串列表。"""
    if not value:
        return []
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[;；,，\n]+", value) if p.strip()]
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for x in value:
            out.extend(_str_list(x) if not isinstance(x, str) else [x.strip()])
        return [p for p in out if p]
    return [str(value).strip()] if str(value).strip() else []


# --------------------------------------------------------------------------
# 编号空间：定稿 ↔ 证据池
# --------------------------------------------------------------------------


def unfinalize_text(text: str, meta: dict) -> tuple[str, int]:
    """把"已定稿"的稿子还原回证据池编号空间，返回 ``(文本, 还原处数)``。

    ``writer:references`` 定稿时会把角标从**证据池编号**改写成连续编号。
    在那之后再动稿子（重跑起草、改写某一章），正文里的 ``[n]`` 就已经不是池编号了——
    直接拿去校验会查到**另一批证据**，而外观上完全正常。

    所以任何"改稿"环节开始前都要先调用它。映射表就存在 ``meta["references_final"]``
    里，可核对；还原后该键会被清掉（表示"当前稿子处于池编号空间"）。

    这是**幂等**的：没定稿过的稿子调用它什么都不做。
    """
    from ..writing.citations import invert_mapping, renumber

    final = (meta or {}).get("references_final") or {}
    if not (isinstance(final, dict) and final.get("mapping")):
        return text, 0
    inverted = invert_mapping(final["mapping"])
    meta.pop("references_final", None)
    if not inverted:
        return text, 0
    restored, changed = renumber(text, inverted)
    return restored, changed


def section_kind(title: str) -> str:
    t = clean_title(title)
    for keys, label in SECTION_HINTS:
        if any(k in t for k in keys):
            return label
    return "该章主题相关的直接证据"


def _strings(value: Any) -> list[str]:
    """把 ``["a", "b"]`` / ``"a；b"`` / ``{"k": "v"}`` 都摊平成字符串列表。

    对模型的输出不能做严格类型假设：同一个字段它有时给数组、有时给分号串。
    这里统一摊平，代价是丢掉结构，收益是"模型换个写法不会让整章写不出来"。
    """
    out: list[str] = []
    if value is None or isinstance(value, bool):
        return out
    if isinstance(value, str):
        for piece in re.split(r"[；;\n]|(?<=[。！？])", value):
            piece = piece.strip(" \t-—、,，")
            if piece:
                out.append(piece)
        return out
    if isinstance(value, dict):
        return _strings(list(value.values()))
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if isinstance(item, dict):
                out.extend(_strings(item.get("point") or item.get("text") or item))
            else:
                out.extend(_strings(item))
        return out
    return _strings(str(value))


# --------------------------------------------------------------------------
# 引用角标：``[1]`` / ``[1,2]`` / ``[3-5]``
# --------------------------------------------------------------------------
# 正则与切分逻辑见文件顶部从 ``app.writing.citations`` 引入的那一组名字。


def validate_citations(text: str, valid_indices: Iterable[int]) -> dict:
    """校验文本里的 ``[n]`` 角标是否落在本次提供的证据编号范围内。

    返回:

    ``text``            去掉无效角标后的文本
    ``ok_indices``      实际被保留的编号（升序、去重）
    ``kept``            保留的角标个数
    ``removed``         被删掉的**不同**编号个数（报告里就报这个数）
    ``removed_citations`` 整条被删的角标原文与前后文，供作者核对模型编了什么
    ``suspicious``      编号越界但同一条角标里还有有效编号（仅删越界编号，记一条提示）

    为什么"越界就删"而不是留着让作者判断：角标的作用是让每个论断可追溯。
    一个指向不存在证据的角标**不是漏洞，是伪造的溯源**，它比没有角标更危险——
    作者会以为那句话有依据。留着一个已知无效的数字，等于把核实成本转嫁给作者。
    但删除不是静默的：``removed`` 与 ``removed_citations`` 必须进报告，让作者看到
    模型试图编造引用。
    """
    valid = {int(i) for i in valid_indices}
    removed: list[dict] = []
    suspicious: list[dict] = []
    removed_indices: set[int] = set()
    ok_indices: set[int] = set()

    # 先切掉文末的参考文献区。条目形如 ``[1] 张三. ……``，与正文角标同形，
    # 不排除的话每一条文献编号都会被报成"模型编造引用"。
    block = find_references_block(text)
    if block:
        head_text = text[: block[0]]
    else:
        head_text = text

    pieces: list[str] = []
    pos = 0
    for m in _CITE_MARKER.finditer(head_text):
        nums = [n for n in _parse_marker(m.group(1)) if n > 0]
        # 没有编号、或编号大到不像引用（如 [2024] 年份）时，一律不动它：
        # 校验器只对"看起来确实是引用角标"的东西负责，避免把作者的年份写成越界。
        if not nums or nums[0] > 9999:
            continue
        context = text[max(0, m.start() - 18) : m.end() + 18].replace("\n", " ")
        bad = [n for n in nums if n not in valid]
        good = [n for n in nums if n in valid]

        if not good:
            # 整条角标没有一个编号有依据：删掉（伪造的溯源比没有角标更危险）。
            removed.append({"marker": m.group(0), "indices": nums, "context": context})
            removed_indices.update(nums)
            pieces.append(text[pos : m.start()])
            pos = m.end()
            continue
        if bad:
            # 部分有效：只删无效编号、保留有效的，并单记一条可疑（常见于编号串位）。
            removed_indices.update(bad)
            suspicious.append({"marker": m.group(0), "dropped": bad, "context": context})
            pieces.append(text[pos : m.start()])
            pieces.append("[" + ",".join(str(n) for n in sorted(set(good))) + "]")
            pos = m.end()
            ok_indices.update(good)
            continue
        ok_indices.update(good)

    pieces.append(text[pos:])
    cleaned = "".join(pieces)
    # 清理要**逐行**做，绝不能用 ``re.sub(r"[ \t]{2,}", " ", 全文)``：
    # 那个正则会把 "\n\n" 也吞成一个 "\n"，静默毁掉所有段落分隔
    # （而且毁得很隐蔽——文本看着还是通顺的，只是段落全粘在一起）。
    cleaned = "\n".join(_tidy_line(ln) for ln in cleaned.split("\n")).strip()

    # 保留数只数**正文区**，参考文献条目里的 ``[1] [2]`` 不算引用角标，
    # 否则"保留角标 4 处"这种数字会比作者在正文里看到的引用多，反而误导。
    # 切点用清洗后文本的长度对齐：清洗只增删空白与角标内容，不改变前缀顺序。
    body_len = len(cleaned)
    if block and len(text) > block[0]:
        body_len = max(0, len(cleaned) - (len(text) - block[0]))
    body_text = cleaned[:body_len]

    return {
        "text": cleaned,
        "ok_indices": sorted(ok_indices),
        # 保留数从清洗后的正文里重新数一遍：这样"报告里的数字"和"作者在正文里
        # 看到的角标"必然一致，不依赖上面那个循环有没有记错。
        "kept": sum(1 for m in _CITE_MARKER.finditer(body_text) if m.group(1).strip("0")),
        # 删除数包含"整条角标被删"和"角标里的越界编号被摘掉"两种：
        # 对作者而言这两种都是"模型编了一个引用"，不该在报告里分成两套数字。
        "removed": len(removed_indices),
        "removed_citations": removed,
        "suspicious": suspicious,
    }


# --------------------------------------------------------------------------
# 未完成标记：模型自己承认"这里需要作者补"
# --------------------------------------------------------------------------

#: 模型被告知用这个记号标出它拿不出依据的地方。中英文括号都收。
_GAP_MARKER = re.compile(r"[【\[]\s*待作者补充\s*[:：]?\s*([^】\]]*)[】\]]")
#: 兜底：模型没按约定打记号，但用自然语言承认缺数据。
_GAP_PHRASE = re.compile(r"(尚未获取|尚需补充|有待补充|需要补充(?:数据|资料|文献)|缺乏相关(?:数据|资料))")


def find_gaps(text: str) -> list[str]:
    """抽出所有"需要作者补充"的位置说明（去重、保序）。"""
    gaps: list[str] = []
    for m in _GAP_MARKER.finditer(text or ""):
        note = m.group(1).strip()
        if note:
            gaps.append(note)
    if not gaps:
        for m in _GAP_PHRASE.finditer(text or ""):
            seg = (text or "")[max(0, m.start() - 40) : m.end() + 40].replace("\n", " ")
            gaps.append(seg.strip())
    seen: set[str] = set()
    out: list[str] = []
    for g in gaps:
        if g not in seen:
            seen.add(g)
            out.append(g)
    return out


# --------------------------------------------------------------------------
# 逐章累积：让"版本 N"真的是"前 N 章"
# --------------------------------------------------------------------------


def merge_section(document: str, title: str, body: str) -> str:
    """把一章正文并入整篇稿件：已有同名章节则替换，否则追加。

    为什么要自己管这个，而不是每章直接 ``put_artifact(text=该章正文)``：

    黑板的语义是"新版本 = 上一版的修订"（``parent`` 指向前一版）。如果第 3 章
    那一版里只有第 3 章的文字，父子链就名不副实——作者看到 v3 的 parent 是 v2，
    会以为 v3 包含 v2 的内容，实际不是。所以这里维护一份**累积的正文**，
    每次追加都写入"前几章 + 本章"，让版本链的语义和它的名字一致。

    重跑同一章时替换而不是追加：否则重跑一次起草就会出现两个同名章节，
    而作者无法判断哪个是最新的。

    实现用整段正则而不是逐行拼接：章节的边界由"下一个 ``# `` 标题"唯一确定，
    用正则一次匹配出来，比维护行下标与空行归属少一大类越界错误。
    输出格式固定为"标题 + 空行 + 正文 + 空行"，**同内容写两次结果逐字节相同**
    （起草环节靠这条不变量决定"要不要真的写一个新版本"）。
    """
    marker = f"# {title}".strip()
    body = (body or "").strip()
    block = f"{marker}\n\n{body}".rstrip()

    text = document or ""
    pattern = re.compile(rf"^{re.escape(marker)}[ \t]*$", re.M)
    m = pattern.search(text)
    if m is None:
        return _join_blocks([text, block]) + "\n"

    # 章节区间 = 本标题行开始，到下一个一级标题行之前。区间内的正文与空行都属于
    # 本章，整段替换；区间之后的内容原样保留。
    nxt = re.compile(r"^# .*$", re.M).search(text, m.end())
    end = nxt.start() if nxt else len(text)
    return _join_blocks([text[: m.start()], block, text[end:]]) + "\n"


def _join_blocks(blocks: Sequence[str]) -> str:
    """用"一个空行"把若干块拼起来，并丢掉空块与块首尾的空白。

    统一在这里拼是幂等性的来源：无论某一块是"刚写的章节"还是"原有的前后文"，
    最终形态都是"去掉首尾空白的块，块间恰好一个空行，末尾一个换行"。
    同内容写两次必然得到同一个字符串。
    """
    kept = [b.strip() for b in blocks if (b or "").strip()]
    return "\n\n".join(kept)


def split_sections(document: str) -> list[str]:
    """列出稿件里已有的章节标题（供报告与测试查看累积结果）。"""
    return [ln.strip()[2:].strip() for ln in (document or "").split("\n") if ln.strip().startswith("# ")]


# --------------------------------------------------------------------------
# 证据登记
# --------------------------------------------------------------------------


def evidence_entries(hits: Sequence[Any], *, limit: int = 0) -> list[dict]:
    """把 ``Hit`` 列表压成写给模型看、也写进台账的证据条目。

    编号从 1 开始且**按本段证据重排**，不是直接用检索返回的 ``hit.index``：
    编号必须与提示词里给模型的编号严格一致，否则校验角标时会全校验错。
    只保留一条证据时也要给它编号 [1]，不能因为"只有一条"就省略编号。
    """
    out: list[dict] = []
    for i, h in enumerate(hits or [], start=1):
        if limit and len(out) >= limit:
            break
        text = str(getattr(h, "text", "") or "").strip()
        if not text:
            continue
        out.append(
            {
                "index": i,
                "citation": str(getattr(h, "citation", "") or "").strip(),
                "text": text,
                "section": str(getattr(h, "section", "") or "").strip(),
                "page": getattr(h, "page", None),
                "score": float(getattr(h, "score", 0.0) or 0.0),
            }
        )
    return out


def format_evidence(entries: Sequence[dict]) -> str:
    """渲染成提示词里那段"只能依据这些材料说话"。"""
    if not entries:
        return "（本节没有检索到任何资料）"
    lines: list[str] = []
    for e in entries:
        where = e.get("citation") or "来源未标注"
        extra = []
        if e.get("section"):
            extra.append(f"章节：{e['section']}")
        if e.get("page"):
            extra.append(f"页码：{e['page']}")
        lines.append(f"[{e['index']}] {where}" + (f"（{'，'.join(extra)}）" if extra else ""))
        lines.append(f"    原文：{e['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 供智能体使用的统一检索入口
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 模型服务的致命错误：立刻停下，不要继续重试
# --------------------------------------------------------------------------

#: 这些错误**重试和继续调用都不会成功**：余额不足、密钥无效、权限不够。
#:
#: 为什么值得单独识别：一次一键生成会发起几十次模型调用，逐单元循环。真实跑过一次，
#: 中途账户余额耗尽（HTTP 402），而当时的代码对**每个**剩余单元都再试一次——
#: 白跑 6 次、多花两分钟、报告里堆出 6 条一模一样的失败记录，把真正的信息
#: （"余额用完了"）淹没在噪音里。所以这类错误必须立刻熔断：停下、只说一次、说清楚。
_FATAL_LLM = re.compile(
    r"HTTP\s*(?:401|402|403)\b|Insufficient\s+Balance|insufficient_quota|"
    r"invalid_api_key|Invalid API key|Unauthorized|余额不足|账户余额|欠费|"
    r"authentication|api key.*(?:invalid|not found)",
    re.I,
)


def is_fatal_llm_error(exc: Any) -> bool:
    """这个模型调用异常是否属于"再调也不会成功"的致命错误。

    刻意只认**明确的**致命标志，而不是把所有异常都当致命：网络抖动、限流、超时
    都应该让后续单元继续尝试（换个时刻可能就成功了）。判错方向的代价不对称——
    把致命错误当普通错误，只是多试几次；把普通错误当致命，会让整章的其余小节
    全部不写，而作者只会看到"生成失败"。
    """
    return bool(_FATAL_LLM.search(str(exc) or ""))


class SearchFactory:
    """按需构造 RAG 客户端，并且**每一条证据检索失败都单独报告**。

    三个设计取舍：

    * 延迟构造：构造函数里不连服务。RAG 不可用时是检索那一刻报错，错误消息
      能带上"哪一章、查了什么词"，比启动时统一报错有用得多。
    * 可注入：测试传一个鸭子类型的假客户端进来即可，不需要真实网络，也不需要
      真的启动对端服务。
    * 失败不抛：``search`` 只返回 ``(hits, error)``。检索失败和下笔写作是两件事，
      让调用方决定是"整篇停下来"还是"标成无证据继续"。
    """

    def __init__(self, base_url: str = "", *, timeout: float = 120.0, adapter: Any = None) -> None:
        self.base_url = base_url
        self.timeout = timeout
        self._adapter = adapter
        self._doc_index: dict[str, dict] | None = None
        self.failures: list[dict] = []

    def adapter(self) -> Any:
        if self._adapter is None:
            # 在这里 import 而不是模块顶部：本模块要能被不依赖 httpx 的测试导入。
            from ..adapters.rag import RAGAdapter

            self._adapter = RAGAdapter(self.base_url, timeout=self.timeout)
        return self._adapter

    def doc_index(self) -> dict[str, dict]:
        """知识库的文档元数据：``doc_id -> {author, year, title, source_path, ...}``。

        用途只有一个——**给证据补齐作者/年份/题名**，好让定稿阶段能生成参考文献表。
        RAG 的检索命中里只有一句拼好的 ``citation``，拆字段容易拆错；元数据接口
        给的是结构化的，用它能避免"从一句话里猜作者名"。

        取不到时返回空字典且**不记入 failures**：这不是证据检索失败，
        只是参考文献表会少几个字段（缺字段会被逐条列出，不会被静默补全）。
        结果缓存，避免逐章重复请求。
        """
        if self._doc_index is not None:
            return self._doc_index
        try:
            docs = self.adapter().docs()
        except Exception:  # noqa: BLE001 - 元数据缺失不该阻断写作
            self._doc_index = {}
            return self._doc_index
        out: dict[str, dict] = {}
        for d in docs or []:
            if isinstance(d, dict) and d.get("doc_id"):
                out[str(d["doc_id"])] = d
        self._doc_index = out
        return out

    def search(self, query: str, *, k: int = 6) -> tuple[list[Any], str]:
        """检索证据。返回 ``(hits, error)``，``error`` 非空表示这次没检索到东西。

        ``rerank=False``：重排会对每一段调用模型，逐章循环检索时成本会翻好几倍，
        而规划阶段只需要知道"有没有材料"，不需要最优排序。
        """
        try:
            hits = self.adapter().search(query, k=k, rerank=False)
        except Exception as exc:  # noqa: BLE001 - 对端不可用不能把整条流程带崩
            detail = str(exc) or f"{type(exc).__name__}"
            # 适配器的错误消息里已经含服务名与启动命令，直接透出即可。
            self.failures.append({"query": query, "error": detail})
            return [], detail
        return list(hits or []), ""
