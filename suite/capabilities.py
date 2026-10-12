"""补齐的功能：大纲生成、查重风险自查、学校模板识别、按意见修改。

为什么放在一个模块里
--------------------
这四件事看起来不相干，但它们共享同一个特征：**都是围绕"稿件"的一次加工**，
而且都要读写会话状态（证据、稿件、评估结果）。放在一起能让"数据从哪来到哪去"
一眼看清；拆成四个文件反而要来回跳。

一个必须说清的设计原则：**凡是查不出来的，就说查不出来。**
查重与模板识别这两件事最容易滑向"给个看起来很专业的数字"——
那比不做更坏，因为它会让作者误以为已经查过了。所以这里的每个结论都带
``confidence`` 与 ``limits``，把"我到底做了什么比对"写明白。
"""

from __future__ import annotations

import io
import re
from collections import Counter
from typing import Any

# ===========================================================================
# ① 查重风险自查
# ===========================================================================

#: 判定"段落高度重复"的阈值。
#:
#: **0.78 是按中文实测定的，不是从别处抄的。** 早先取 0.82，来源是上游
#: ``ExternalVerifier`` 的标题匹配阈值——那套判据是给**英文标题**用的，
#: 直接搬过来会错配：实测两段只改 3 个字（"关键"到"核心"、"给出"到"提出"）的中文段落，
#: 字符 3-gram 余弦相似度是 **0.8158**，刚好卡在 0.82 之下被判为"不重复"。
#: 而"改几个字的同义改写"恰恰是查重最该抓的情况——**漏报比误报更坏**，
#: 因为它给作者一个"查过了、没问题"的错觉。
#:
#: 取 0.78 的依据：无关的中文段落相似度通常在 0.4 以下（同领域同主题的极端情况约 0.6），
#: 0.78 与它们有明显间隔，同时能覆盖"改几个字"这一档。
DUPLICATE_THRESHOLD = 0.78

#: 段落太短就不参与重复检测：短句（"综上所述。"）本来就会重复，
#: 报出来全是假警报，而假警报会让作者连着真问题一起忽略。
#:
#: 阈值取 **30** 而不是更大的值：中文一个句子常在 30~40 字，
#: 早先设成 60 会把整篇短段落全部过滤掉，于是"0 段落、风险 low"——
#: 一个**假阴性**，而且看起来像"没问题"，比误报危险得多（实测踩过）。
MIN_PARA_CHARS = 30


def _longest_common_run(a: str, b: str) -> int:
    """两段归一化文本的**最长连续公共子串**长度。

    为什么需要它：字符 n-gram 余弦相似度对中文**过于敏感**——
    实测两段只改了 3 个字（"关键"→"核心"、"给出"→"提出"），
    相似度就从 1.0 掉到 0.8158，刚好卡在 0.82 阈值下面被判为"不重复"。
    而"改了几个字的同义改写"正是查重最该抓的情况。

    最长公共子串回答的是另一个问题："这一段里有没有一大块**原样**出现在别处？"
    它对少量替换不敏感，正好补上 n-gram 的短板。两个判据取并集。
    """
    a, b = _normalize(a), _normalize(b)
    if not a or not b:
        return 0
    # 滚动数组做 LCS，O(len(a)*len(b)) 空间 O(min)
    if len(a) < len(b):
        a, b = b, a
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def _normalize(text: str) -> str:
    """归一化：去掉空白与标点，只看实词序列。

    标点不计入比对——两段只差一个逗号不该被判为不同，
    而"完全相同的句子 + 不同标点"在真实稿件里很常见。
    """
    return re.sub(r"[\s\W_]+", "", text or "")


#: 最长公共子串占较短段落的比例达到这个值即算重复。
#: 0.6 的依据：一段 40 字的段落里有 24 字连续原样出现，人工复核时基本都会判为重复。
RUN_RATIO = 0.6

#: n-gram 相似度的**次要**阈值。低于 DUPLICATE_THRESHOLD 但高于这个值时，
#: 才去看最长公共子串——避免对毫不相干的两段白算一次 O(n*m)。
RUN_CHECK_FLOOR = 0.45


def _ngrams(text: str, n: int = 3) -> Counter:
    """字符 n-gram 计数。中文没有词边界，用字符 n-gram 比按空格切词靠谱。"""
    clean = _normalize(text)
    if len(clean) < n:
        return Counter()
    return Counter(clean[i:i + n] for i in range(len(clean) - n + 1))


def _similarity(a: str, b: str) -> float:
    """两段文本的 n-gram 余弦相似度，0~1。"""
    ca, cb = _ngrams(a), _ngrams(b)
    if not ca or not cb:
        return 0.0
    common = set(ca) & set(cb)
    dot = sum(ca[g] * cb[g] for g in common)
    na = sum(v * v for v in ca.values()) ** 0.5
    nb = sum(v * v for v in cb.values()) ** 0.5
    if not na or not nb:
        return 0.0
    return dot / (na * nb)


def _paragraphs(text: str) -> list[str]:
    """切成段落。中文论文的段落靠换行分隔，空行更常见。"""
    parts = [p.strip() for p in re.split(r"\n\s*\n|\n", text or "")]
    return [p for p in parts if len(p) >= MIN_PARA_CHARS]


def duplication_check(text: str, *, threshold: float = DUPLICATE_THRESHOLD) -> dict:
    """文档**内部**的重复自查。

    能查什么：同一份稿子里两段高度雷同（复制粘贴、同义改写不彻底）。
    查不了什么：与外部语料的重复。那需要把全文送去比对数据库，
    本机做不到——而**假装做到了**是最坏的结果，所以这里明确写出来。
    """
    paras = _paragraphs(text)
    pairs: list[dict] = []
    for i in range(len(paras)):
        for j in range(i + 1, len(paras)):
            sim = _similarity(paras[i], paras[j])
            run = 0
            # 两个判据取并集：n-gram 抓整体相似，最长公共子串抓"整块原样照搬"。
            # 只用一个都会漏——实测 n-gram 会把"改 3 个字"的段落判成不重复。
            if RUN_CHECK_FLOOR <= sim < threshold:
                run = _longest_common_run(paras[i], paras[j])
                shorter = min(len(_normalize(paras[i])), len(_normalize(paras[j])))
                if shorter and run / shorter >= RUN_RATIO:
                    sim = max(sim, run / shorter)
            if sim >= threshold:
                pairs.append({
                    "a_index": i + 1,
                    "b_index": j + 1,
                    "similarity": round(sim, 3),
                    "longest_common_run": run,
                    "a_preview": paras[i][:100],
                    "b_preview": paras[j][:100],
                })
    pairs.sort(key=lambda p: -p["similarity"])

    # 逐段最长重复片段：找出"这一段里有一大块在别处出现过"
    return {
        "paragraphs": len(paras),
        "threshold": threshold,
        "duplicate_pairs": pairs[:20],
        "duplicate_pair_count": len(pairs),
        "internal_risk": (
            "high" if len(pairs) >= 3 else ("medium" if pairs else "low")
        ),
        "limits": (
            "**只做文档内部自查**：能发现同一份稿子里两段高度雷同。"
            "**不能**检测与外部语料（已发表文献、网络内容、往届论文）的重复——"
            "那需要比对数据库，本机做不到。正式查重请用学校指定系统。"
        ),
    }


def citation_reachability(items: list[dict], *, limit: int = 20) -> dict:
    """参考文献可核验性自查：拿证据库里的条目去公开文献库核对。

    这是"查重风险"里**真的能查**的那一半：一条在 Crossref / OpenAlex 里
    查不到的参考文献，要么是编的，要么题录错得无法定位——两种都值得作者马上看。
    """
    from . import websearch

    checked: list[dict] = []
    for item in (items or [])[:limit]:
        title = str(item.get("title") or "").strip()
        doi = str(item.get("doi") or "").strip()
        if not title and not doi:
            continue
        verdict, detail = "inconclusive", ""
        try:
            if doi:
                recs, _ = websearch.search_literature([doi], limit_per_source=2, max_total=3)
                verdict = "confirmed" if recs else "not_found"
            elif title:
                recs, _ = websearch.search_literature([title], limit_per_source=2, max_total=3)
                if not recs:
                    verdict = "not_found"
                else:
                    best = max(_similarity(title, r.title) for r in recs)
                    # 阈值取 0.62：中文题名的 n-gram 相似度天然偏低，
                    # 用 0.82 会把大量真文献判成"查无此文"，那是更坏的错误方向
                    verdict = "confirmed" if best >= 0.62 else "weak_match"
                    detail = f"最相近 {best:.2f}"
        except Exception as exc:  # noqa: BLE001 - 网络失败一律记为无法确认
            verdict, detail = "inconclusive", f"{type(exc).__name__}"

        checked.append({
            "title": title or doi,
            "doi": doi,
            "verdict": verdict,
            "detail": detail,
        })

    unverifiable = [c for c in checked if c["verdict"] == "not_found"]
    return {
        "checked": len(checked),
        "unverifiable": unverifiable,
        "tally": dict(Counter(c["verdict"] for c in checked)),
        "limits": (
            "核对的是**题录能不能对上真实记录**，不判断文献内容真假；"
            "inconclusive 是网络原因导致无法确认，**不等于通过**。"
        ),
    }


# ===========================================================================
# ② 学校模板识别
# ===========================================================================

#: 从 .docx 里能可靠读出来的格式事实。读不出来的（例如"页眉要求"）不猜。
_DOCX_STYLE_KEYS = ("Heading 1", "Heading 2", "Heading 3", "Normal")


def inspect_template(data: bytes) -> dict:
    """从一份 .docx（通常是学校给的格式模板）里读出可识别的格式事实。

    只报**真读到的**：标题样式字号/字体、正文字号/字体、行距、页边距。
    读不到就留空，不填默认值冒充"识别结果"——模板识别的价值全在于它说的是真话。
    """
    from docx import Document
    from docx.shared import Pt

    out: dict[str, Any] = {"styles": {}, "page": {}, "limits": (
        "只识别 .docx 里的**样式与页面设置**。复杂要求（页眉页脚文字、图表编号规则、"
        "参考文献著录格式）读不出来，需要人工补充。"
    )}
    try:
        doc = Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001
        return {"error": f"无法打开 Word 文档：{type(exc).__name__}: {exc}", "styles": {}, "page": {}}

    for key in _DOCX_STYLE_KEYS:
        try:
            style = doc.styles[key]
        except KeyError:
            continue
        info: dict[str, Any] = {"name": key}
        size = getattr(style.font, "size", None)
        if size is not None:
            info["size_pt"] = round(size.pt, 1)
        if style.font.name:
            info["font"] = style.font.name
        if style.font.bold is not None:
            info["bold"] = bool(style.font.bold)
        # 中文编码（eastAsia）必须单独读，否则中文字体识别不到
        try:
            from docx.oxml.ns import qn

            rpr = style.element.find(qn("w:rPr"))
            if rpr is not None:
                rfonts = rpr.find(qn("w:rFonts"))
                if rfonts is not None:
                    east = rfonts.get(qn("w:eastAsia"))
                    if east:
                        info["east_asia_font"] = east
        except Exception:  # noqa: BLE001
            pass
        para = getattr(style, "paragraph_format", None)
        if para is not None:
            if para.line_spacing is not None:
                info["line_spacing"] = para.line_spacing
            if para.first_line_indent is not None:
                try:
                    info["first_line_indent_pt"] = round(para.first_line_indent.pt, 1)
                except Exception:  # noqa: BLE001
                    pass
        out["styles"][key] = info

    # 页面设置：取第一节
    try:
        section = doc.sections[0]
        from docx.shared import Cm

        def cm(v):
            return round(v.cm, 2) if v is not None else None

        out["page"] = {
            "top_cm": cm(section.top_margin),
            "bottom_cm": cm(section.bottom_margin),
            "left_cm": cm(section.left_margin),
            "right_cm": cm(section.right_margin),
            "page_width_cm": cm(section.page_width),
            "page_height_cm": cm(section.page_height),
        }
    except Exception:  # noqa: BLE001
        pass

    out["paragraph_count"] = len([p for p in doc.paragraphs if p.text.strip()])
    out["table_count"] = len(doc.tables)
    return out


def compare_with_template(manuscript: bytes, template: bytes) -> dict:
    """把稿件的实际格式与模板识别出的规则对照，列出不一致项。

    这是"模板识别"真正有用的地方：不是识别出一堆数字给人看，
    而是**告诉我哪里不合规范**。
    """
    tpl = inspect_template(template)
    man = inspect_template(manuscript)
    if tpl.get("error") or man.get("error"):
        return {"error": tpl.get("error") or man.get("error"), "diffs": []}

    diffs: list[dict] = []
    for key, rule in (tpl.get("styles") or {}).items():
        actual = (man.get("styles") or {}).get(key)
        if not actual:
            diffs.append({"item": key, "expected": rule, "actual": None,
                          "note": "稿件里没有这个样式"})
            continue
        for field in ("size_pt", "font", "east_asia_font", "line_spacing"):
            exp = rule.get(field)
            got = actual.get(field)
            if exp is None or got is None:
                continue
            if isinstance(exp, float) and isinstance(got, float):
                if abs(exp - got) > 0.5:
                    diffs.append({"item": f"{key}.{field}", "expected": exp, "actual": got})
            elif exp != got:
                diffs.append({"item": f"{key}.{field}", "expected": exp, "actual": got})

    page_diffs = []
    for field, exp in (tpl.get("page") or {}).items():
        got = (man.get("page") or {}).get(field)
        if exp is None or got is None:
            continue
        if abs(exp - got) > 0.2:
            page_diffs.append({"item": f"页面.{field}", "expected": exp, "actual": got})

    return {
        "template": tpl,
        "manuscript": man,
        "diffs": diffs + page_diffs,
        "conform": not (diffs or page_diffs),
        "limits": (
            "只比对**能从文件里读出来的**格式事实（样式、字号、字体、行距、页边距）。"
            "页眉页脚文字、图表编号规则、参考文献著录格式等读不出来，需人工核对。"
        ),
    }


# ===========================================================================
# ③ 大纲生成（需要模型）
# ===========================================================================

def outline_prompt(title: str, genre_name: str, evidence_titles: list[str],
                   section_titles: list[str]) -> list[dict]:
    """构造大纲生成的提示词。

    把"已有章节骨架"与"已有依据"都喂进去：前者让大纲贴合学校/学科惯例，
    后者让大纲知道**自己有哪些材料可用**——否则会规划出写不出来的章节。
    """
    ev = "\n".join(f"- {t}" for t in evidence_titles[:30]) or "（暂无）"
    sk = "、".join(section_titles) or "（未指定）"
    return [
        {
            "role": "system",
            "content": (
                "你是中文学位论文的写作规划助手。请为给定题目产出一份**可执行**的章节大纲。\n"
                "硬性要求：\n"
                "1. 只输出 JSON，不要解释、不要 Markdown 围栏。\n"
                "2. 每节必须给出 `points`（该节要论证的要点）与 `needs`（需要什么材料支撑）。\n"
                "3. `needs` 要具体到「什么样的数据/文献」，不要写「相关资料」这种空话。\n"
                "4. 不要规划依据材料无法支撑的章节；材料不足时在 needs 里说明缺什么。\n"
                "5. 章节标题用本学科通行的说法，不要编造学术概念。\n"
                "输出结构：\n"
                '{"sections":[{"title":"第1章 绪论","focus":"这一章解决什么",'
                '"children":[{"title":"1.1 研究背景","points":["要点1","要点2"],'
                '"needs":["需要什么材料"]}]}]}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"论文题目：{title}\n"
                f"论文类型：{genre_name or '未指定'}\n"
                f"已有章节骨架：{sk}\n\n"
                f"现有可用依据材料：\n{ev}\n\n"
                "请据此产出章节大纲。"
            ),
        },
    ]


def parse_outline(raw: Any) -> dict:
    """把模型返回的大纲 JSON 规整成稳定结构。

    模型的输出总会有些出入（缺 children、points 写成字符串等），
    这里统一成一致形状，让前端不必到处做防御性判断。
    """
    if not isinstance(raw, dict):
        return {"sections": []}
    out: list[dict] = []
    for sec in raw.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        children: list[dict] = []
        for child in sec.get("children") or []:
            if not isinstance(child, dict):
                continue
            points = child.get("points")
            needs = child.get("needs")
            children.append({
                "title": str(child.get("title") or "").strip(),
                "points": [str(x).strip() for x in points] if isinstance(points, list)
                          else ([str(points)] if points else []),
                "needs": [str(x).strip() for x in needs] if isinstance(needs, list)
                         else ([str(needs)] if needs else []),
            })
        out.append({
            "title": str(sec.get("title") or "").strip(),
            "focus": str(sec.get("focus") or "").strip(),
            "children": children,
        })
    return {"sections": [s for s in out if s["title"]]}


def outline_to_text(outline: dict) -> str:
    """把大纲渲染成供写作阶段使用的章节结构文本（每行一个章节）。

    只输出**章标题**：小节信息留给大纲本身查看。原因是上游的章节切分器
    以「第X章」为单位工作，把小节也列进来会让章节数暴涨、每章过短。
    """
    return "\n".join(s["title"] for s in (outline or {}).get("sections") or [] if s.get("title"))


# ===========================================================================
# ④ 按意见自动修改（需要模型）
# ===========================================================================

def revision_prompt(section_heading: str, body: str, issues: list[dict],
                    evidence_block: str) -> list[dict]:
    """构造"按评估意见改写"的提示词。

    关键约束与写作阶段一致：**只能依据给定材料说话**，且改写要**定点**——
    只改被指出问题的地方，不要把整段重写一遍。整段重写会让作者无法核对
    "到底改了什么"，而且很容易把原本有依据的句子改丢。
    """
    issues_text = "\n".join(
        f"- [{i.get('kind', '')}] {i.get('message', '')}"
        + (f"（修改建议：{i.get('fix_hint')}）" if i.get("fix_hint") else "")
        + (f"\n  原文片段：{i.get('evidence')}" if i.get("evidence") else "")
        for i in (issues or [])[:12]
    ) or "（没有具体意见）"

    return [
        {
            "role": "system",
            "content": (
                "你是中文学位论文的修改助手。请**只针对给定的问题**修改这一节正文。\n"
                "硬性要求：\n"
                "1. 只依据材料表里的事实说话。材料没有的内容一律不写，数字不得编造。\n"
                "2. **未涉及问题的句子保持原样**，不要顺手润色、不要改语气、不要重排结构。\n"
                "3. 引用编号只能使用材料表里真实存在的编号；不要新增材料表之外的引用。\n"
                "4. 如果某个问题**无法依据现有材料解决**，不要硬改——在改动说明里写"
                "「材料不足，需补充 X」，正文该处保持原样。\n"
                "5. 输出严格 JSON：{\"body\":\"修改后的完整正文\","
                "\"changes\":[{\"what\":\"改了什么\",\"why\":\"对应哪条意见\"}],"
                "\"unsolved\":[\"无法解决的意见\"]}\n"
                "不要输出解释、不要 Markdown 围栏。"
            ),
        },
        {
            "role": "user",
            "content": (
                f"要修改的章节：{section_heading}\n\n"
                f"需要解决的问题：\n{issues_text}\n\n"
                f"可用材料：\n{evidence_block or '（没有材料，此时不应改动任何需要依据的表述）'}\n\n"
                f"当前正文：\n{body}\n\n"
                "请按上述要求改。"
            ),
        },
    ]

    """构造大纲生成的提示词。

    把"已有章节骨架"与"已有依据"都喂进去：前者让大纲贴合学校/学科惯例，
    后者让大纲知道**自己有哪些材料可用**——否则会规划出写不出来的章节。
    """
    ev = "\n".join(f"- {t}" for t in evidence_titles[:30]) or "（暂无）"
    sk = "、".join(section_titles) or "（未指定）"
    return [
        {
            "role": "system",
            "content": (
                "你是中文学位论文的写作规划助手。请为给定题目产出一份**可执行**的章节大纲。\n"
                "硬性要求：\n"
                "1. 只输出 JSON，不要解释、不要 Markdown 围栏。\n"
                "2. 每节必须给出 `points`（该节要论证的要点）与 `needs`（需要什么类型的材料支撑）。\n"
                "3. `needs` 要具体到「什么样的数据/文献」，不要写「相关资料」这种空话。\n"
                "4. 不要规划出依据材料无法支撑的章节；材料不足时在 needs 里说明缺什么。\n"
                "5. 章节标题不要编造学术概念，用本学科通行的说法。\n"
                "输出结构：\n"
                '{"sections":[{"title":"第1章 绪论","focus":"这一章解决什么",'
                '"children":[{"title":"1.1 研究背景","points":["要点1","要点2"],'
                '"needs":["需要什么材料"]}]}]}'
            ),
        },
        {
            "role": "user",
            "content": (
                f"论文题目：{title}\n"
                f"论文类型：{genre_name or '未指定'}\n"
                f"已有章节骨架：{sk}\n\n"
                f"现有可用依据材料：\n{ev}\n\n"
                "请据此产出章节大纲。"
            ),
        },
    ]
