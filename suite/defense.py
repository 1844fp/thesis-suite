"""答辩材料生成：答辩稿（陈述稿 + 预计问答）与演示幻灯片提纲。

为什么只有"提示词 + 规整 + 渲染"，没有"一键调用模型"
----------------------------------------------------
本站的模型调用一律走访客自带的 Key（BYOK，见 ``suite/credentials.py``），
而"用哪个 Key、花谁的钱、要不要真的发出去"是**调用方（API 层）**的决定。
所以这个模块只负责三件确定的事：

1. **构造提示词**（``script_prompt`` / ``slides_prompt``）——把纪律写死在系统提示里；
2. **规整模型输出**（``parse_script`` / ``parse_slides``）——模型给什么形状都能接住；
3. **渲染成交付物**（``script_to_markdown`` / ``build_html_deck`` / ``build_pptx``）。

这样它就是一个纯函数模块：可以离线测试，也不会替调用方做花钱的决定。

两条不可让步的纪律
------------------
**一、只能依据给定材料。** 答辩现场被追问一句就会露，所以提示词里把
"不得编造"写成最高优先级，并且**允许模型写「材料不足」**。
不允许说"材料不足"的提示词，等于逼模型编——这是这类功能最容易犯的错。

**二、幻灯片要点不超过 25 字。** 这不是审美偏好，是演示稿与文档的区别：
投到屏幕上的长句子没人读，观众只会低头看手机。25 字是"一眼扫完"的量级。
所以 :data:`BULLET_MAX_CHARS` 同时用于提示词约束和解析后的体检。

生成 .pptx 的能力
-----------------
:func:`build_pptx` 依赖 ``python-pptx``，它**不在本站的依赖清单里**
（``requirements.txt`` 没有它，运行环境实测也导入不到：``ModuleNotFoundError: pptx``）。
沙箱环境装不了包，所以本模块的主路线是**不依赖任何第三方库**的两件产物：

* :func:`build_html_deck` —— 单文件、离线可开的 HTML 演示稿（真正的交付物）；
* :func:`parse_slides` 的输出 —— 结构化幻灯片提纲（JSON，可喂给任何人做 PPT）。

:func:`build_pptx` 仍然实现着：**一旦环境里装上 python-pptx，它就能用**，
不需要改调用方一行代码；装不上时抛一条说清楚该装什么的中文错误，
而不是丢一个 ``ModuleNotFoundError`` 给访客。**这条分支在本机未经验证**（见函数文档）。
"""

from __future__ import annotations

import html
import importlib.util
import io
import json
import re
from typing import Any

# ---------------------------------------------------------------------------
# 常量与纪律
# ---------------------------------------------------------------------------

#: 幻灯片单条要点的字数上限。见模块文档第二条纪律。
BULLET_MAX_CHARS = 25

#: 每页要点条数。
BULLET_MIN, BULLET_MAX = 3, 5

#: 页数区间。
SLIDES_MIN, SLIDES_MAX = 12, 16

#: 陈述稿口播时长与字数。8~10 分钟 ≈ 1500~2000 字（中文口播约 200 字/分钟）。
SPEECH_MINUTES = "8~10"
STATEMENT_MIN_CHARS, STATEMENT_MAX_CHARS = 1500, 2000

#: 材料不足的标注词。**统一成一个词**，前端才能机械地把它高亮出来——
#: 答辩前最该补的就是这些地方，藏在散文里就找不到了。
INSUFFICIENT = "材料不足"

#: 送进提示词的正文上限（字符）。超了要截断，但不能**悄悄**截断，
#: 否则模型会以为自己看到了全文，然后在"主要发现"那页编出后半篇的数字。
MAX_DRAFT_CHARS = 18000

#: 最高纪律。写在两个系统提示里，措辞刻意重复——它比格式要求重要得多。
MATERIAL_RULE = (
    "【最高纪律：只依据给定材料】\n"
    "1. **只依据材料**：只能依据下面给出的论文材料与文献清单说话，"
    "**不得编造**数据、样本量、统计量、结论、文献、人名、机构、时间。\n"
    f"2. 材料里没有、但这一页/这一问又必须有的东西，直接写「{INSUFFICIENT}，需补充：xxx」，"
    "不要用看起来合理的数字或说法把它填满。\n"
    "3. 引用文献只能用材料里出现过的。不确定的文献一律不写，不要凭印象补一条。\n"
    "4. 宁可承认材料不足，也不要写得漂亮却站不住。答辩是会被追问的场合。\n"
)

#: 建议的幻灯片结构。放在提示词里当默认骨架，但允许模型按学科调整。
DECK_SECTIONS = (
    "封面", "研究背景与问题", "文献缺口", "研究设计",
    "主要发现", "结论与贡献", "不足与展望", "致谢",
)

#: 预计提问必须覆盖的五类。按"答辩委员会真正会问什么"排的序：
#: 先问你是不是真做了（问题与创新），再问做得对不对（方法、数据），
#: 最后问你能推到多远（适用边界、反面文献）。
QA_TOPICS = (
    ("研究问题与创新点", "你的新意到底在哪？与已有研究差在哪？如果别人已经做过类似的，你的增量是什么？"),
    ("方法可靠性", "样本、测量、效度、因果推断的边界。为什么用这个方法而不是另一个？"),
    ("数据来源", "数据怎么来的？能不能公开/复现？有没有伦理审查或知情同意？"),
    ("结论的适用边界", "换一个群体、地区、时间、情境，你的结论还成立吗？为什么？"),
    ("与你结论相左的文献", "有哪些研究得出相反或不一致的结论？你怎么解释这种不一致？"),
)

#: ``python-pptx`` 是否可用。只探测、不导入（导入了就要处理它的副作用）。
PPTX_AVAILABLE = importlib.util.find_spec("pptx") is not None

#: 选定的主路线。调用方可以直接把它展示给用户，说明会拿到什么产物。
DECK_MODE = "pptx" if PPTX_AVAILABLE else "html"

#: python-pptx 缺失时的可读错误。
PPTX_MISSING_HINT = (
    "本机没有安装 python-pptx，无法直接生成 .pptx。"
    "请不要为此在运行时 pip 安装（容器/沙箱里通常装不了），改用另外两条路："
    "① 用 build_html_deck() 产出单文件 HTML 演示稿（离线可开、可直接投屏）；"
    "② 用 parse_slides() 的 JSON 提纲交给 PowerPoint / WPS / Marp 等工具生成。"
    "确实需要本函数时，请在部署环境里 pip install python-pptx。"
)


class DefenseError(RuntimeError):
    """答辩材料生成的失败。消息是中文、可直接展示。"""


def pptx_available() -> bool:
    """当前环境能不能生成 .pptx。"""
    return PPTX_AVAILABLE


# ---------------------------------------------------------------------------
# 提示词构造
# ---------------------------------------------------------------------------
def _format_evidence(evidence_titles: Any, *, limit: int = 40) -> str:
    """把依据库的题名排成清单。空的就明说"暂无"，不要留白让模型自由发挥。"""
    items = _as_str_list(evidence_titles)
    if not items:
        return "（暂无——那么这份材料里**不允许出现任何具体文献**，只能定性表述）"
    return "\n".join(f"- {t}" for t in items[:limit])


def _clip_draft(draft_text: Any) -> tuple[str, str]:
    """截断正文，返回 ``(正文, 截断说明)``。

    截断说明一定要**跟着提示词一起发出去**：模型不知道自己被截断了，
    会以为后面没内容，然后在"主要发现"里编。
    """
    text = str(draft_text or "").strip()
    if len(text) <= MAX_DRAFT_CHARS:
        return text, ""
    note = (
        f"\n\n【注意】以上材料**已被截断**（原文 {len(text)} 字，"
        f"这里只有前 {MAX_DRAFT_CHARS} 字）。"
        f"凡是你没有看到的部分，一律标注「{INSUFFICIENT}」，不要推测。"
    )
    return text[:MAX_DRAFT_CHARS], note


def script_prompt(
    title: str,
    genre_name: str,
    draft_text: str,
    evidence_titles: Any = None,
) -> list[dict]:
    """构造"答辩稿"的提示词。

    产出两样东西：**口播陈述稿**与**预计提问与应答**。

    为什么把提问的五类写死在提示词里（:data:`QA_TOPICS`）：让模型自由发挥，
    它一定只问自己答得上来的问题——那样的"预计提问"没有任何准备价值。
    委员会真正会问的恰恰是方法可靠性、数据来源、适用边界这些**不好答**的地方。

    只有陈述稿没有问答的答辩稿是没用的：现场丢分几乎都在问答环节。
    """
    draft, clipped = _clip_draft(draft_text)
    topics = "\n".join(f"  {chr(96 + i)}）{name}：{hint}" for i, (name, hint) in enumerate(QA_TOPICS, 1))

    system = (
        "你是中文学位论文答辩的准备助手。请为这篇论文产出一份答辩材料："
        "一份**口播陈述稿**，和一组**预计提问与应答**。\n\n"
        + MATERIAL_RULE
        + "\n【陈述稿要求】\n"
        f"- 面向答辩委员会的口播稿，{SPEECH_MINUTES} 分钟，正文 "
        f"{STATEMENT_MIN_CHARS}~{STATEMENT_MAX_CHARS} 字（中文字符）。\n"
        "- 结构：研究背景与问题 → 文献缺口 → 研究设计（对象/方法/数据）"
        "→ 主要发现 → 结论与贡献 → 不足与展望。\n"
        "- 用**口语**，是站在台上说出来的话，不是论文摘要。"
        "不要出现「本文通过……进行了深入探讨」这类书面套话。\n"
        "- 数字必须来自材料；材料里没有数字就定性表述，并标明哪一处缺数据。\n"
        "- 段与段之间用两个换行分隔，方便直接朗读。\n\n"
        "【预计提问与应答要求】\n"
        "- 共 8~10 组，必须覆盖以下五类，每类至少一组：\n"
        f"{topics}\n"
        "- 每组应答 150~300 字：先给结论，再给依据，最后给边界（什么情况下不成立）。\n"
        f"- 材料不足以支撑某个回答时，直接写「{INSUFFICIENT}，建议补充：xxx」"
        "——那正是你答辩前最该补的东西，比编一个答案有用得多。\n\n"
        "【输出格式】\n"
        "只输出 JSON，不要解释、不要 Markdown 围栏：\n"
        '{"statement":"陈述稿全文（段落之间用 \\n\\n 分隔）",'
        '"qa":[{"question":"问题","answer":"应答"}]}'
    )

    user = (
        f"论文题目：{title or '（未提供）'}\n"
        f"论文类型：{genre_name or '未指定'}\n\n"
        f"可直接引用的文献（**只有这些**，不要在清单之外新增任何文献）：\n"
        f"{_format_evidence(evidence_titles)}\n\n"
        f"论文材料（可能是全文、摘要或章节节选）：\n"
        f"----- 材料开始 -----\n{draft or '（材料为空）'}\n----- 材料结束 -----"
        f"{clipped}\n\n"
        "请据此产出答辩稿。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def slides_prompt(title: str, draft_text: str, section_titles: Any = None) -> list[dict]:
    """构造"答辩幻灯片"的提示词。

    硬性要求每页 3~5 条、每条不超过 :data:`BULLET_MAX_CHARS` 字，理由见模块文档。
    另外**强制每页给讲稿备注**：要点是给观众看的提词，备注是自己要说的话，
    两者混在一起就会出现"照着幻灯片念"的答辩。
    """
    draft, clipped = _clip_draft(draft_text)
    sections = _as_str_list(section_titles)
    skeleton = "、".join(sections) if sections else "、".join(DECK_SECTIONS)

    system = (
        "你是中文学位论文答辩的演示稿设计助手。"
        "请把这篇论文压缩成一套**答辩用幻灯片提纲**。\n\n"
        + MATERIAL_RULE
        + "\n【页数与结构】\n"
        f"- 共 {SLIDES_MIN}~{SLIDES_MAX} 页。建议顺序：{' → '.join(DECK_SECTIONS)}。\n"
        "- 页数不够时**不要注水**：宁可把两页合成一页，也不要为了凑页数拆出空页。\n\n"
        "【每页要求】\n"
        f"- 每页 {BULLET_MIN}~{BULLET_MAX} 条要点。\n"
        f"- **每条要点不超过 {BULLET_MAX_CHARS} 个字**。这是硬性要求，不是建议："
        "演示稿不是文档，长句子投到屏幕上没人读，观众只会低头看手机。"
        "要点是提词，不是完整的句子。\n"
        "- 每页必须给 `notes`（讲稿备注，100~200 字）：那是你站在台上真正要说的话，"
        "可以写成完整句子。\n"
        f"- 某一页没有材料支撑时，`bullets` 里就写「{INSUFFICIENT}」，"
        "并在 `notes` 里说明缺什么——**不要编数据来把这一页填满**。\n\n"
        "【输出格式】\n"
        "只输出 JSON，不要解释、不要 Markdown 围栏：\n"
        '{"slides":[{"title":"页标题","bullets":["要点","要点"],"notes":"讲稿备注"}]}'
    )

    user = (
        f"论文题目：{title or '（未提供）'}\n"
        f"建议章节骨架：{skeleton}\n\n"
        f"论文材料（可能是全文、摘要或章节节选）：\n"
        f"----- 材料开始 -----\n{draft or '（材料为空）'}\n----- 材料结束 -----"
        f"{clipped}\n\n"
        "请据此产出幻灯片提纲。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


# ---------------------------------------------------------------------------
# 解析（对残缺输入必须不崩）
# ---------------------------------------------------------------------------
def _as_str_list(value: Any) -> list[str]:
    """把"可能是字符串/列表/None/数字"的东西统一成非空字符串列表。

    模型的输出形状极不稳定（``points`` 有时是列表、有时是一整段字符串、
    有时是 ``None``），所以每个字段都要过一遍这个函数，而不是假设它是列表。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            if isinstance(item, (dict, list, tuple)):
                # 嵌套结构取不到有用文本，转成可读字符串总比丢掉好
                text = json.dumps(item, ensure_ascii=False)
            else:
                text = str(item)
            if text.strip():
                out.append(text.strip())
        return out
    return [str(value).strip()] if str(value).strip() else []


def _first_str(data: dict, keys: tuple[str, ...]) -> str:
    """按顺序取第一个非空的字符串字段。兼容中英文键名。"""
    for key in keys:
        if key in data:
            items = _as_str_list(data.get(key))
            if items:
                return "\n\n".join(items) if len(items) > 1 else items[0]
    return ""


def _as_json(raw: Any) -> Any | None:
    """尽量把 ``raw`` 变成 Python 对象。字符串交给 ``suite.llm.extract_json``。

    复用 ``suite.llm.extract_json`` 而不是另写一份：它已经能处理
    Markdown 围栏、前后散文、以及"JSON 后面还跟着一段解释"这几种常见情况。
    取不到就返回 ``None``，由调用方走纯文本兜底，**不抛异常**。
    """
    if isinstance(raw, (dict, list)):
        return raw
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        from .llm import extract_json
    except Exception:  # noqa: BLE001 - 极端情况下退回标准库
        try:
            return json.loads(text)
        except ValueError:
            return None
    try:
        return extract_json(text)
    except Exception:  # noqa: BLE001
        return None


#: 问答块的正则。同时认中文的「问/答」与英文的 Q/A。
_QA_BLOCK = re.compile(
    r"(?:^|\n)\s*(?:问|问题|Q|Question)\s*[：:]\s*(?P<q>.+?)"
    r"\s*\n\s*(?:答|回答|应答|A|Answer)\s*[：:]\s*(?P<a>.+?)"
    r"(?=\n\s*(?:问|问题|Q|Question)\s*[：:]|\Z)",
    re.S | re.I,
)


def _split_qa_text(text: str) -> tuple[str, list[dict]]:
    """从一段纯文本里切出「陈述稿 + 问答」。

    为什么需要它：模型偶尔不按 JSON 输出，直接给一份带「问：/答：」的讲稿。
    这时候整份丢掉太浪费——它是可用的内容，只是形状不对。
    """
    qa: list[dict] = []
    for m in _QA_BLOCK.finditer(text):
        q = re.sub(r"\s+", " ", m.group("q")).strip()
        a = m.group("a").strip()
        if q and a:
            qa.append({"question": q, "answer": a})
    first = _QA_BLOCK.search(text)
    statement = text[: first.start()].strip() if first else ("" if qa else text.strip())
    return statement, qa


def parse_script(raw: Any) -> dict:
    """把模型输出规整成 ``{"statement": str, "qa": [{"question","answer"}]}``。

    **残缺输入不崩、结构恒定**是这里的首要目标：模型会返回缺字段的对象、
    把 ``qa`` 写成字符串、把答案写成列表、甚至返回一个空对象。
    这些都不该让整条链路失败——用户已经等了一次模型调用。

    多重兜底顺序：JSON 对象 → JSON 数组 → 纯文本（按「问：/答：」切）→ 空结构。
    """
    statement = ""
    qa: list[dict] = []
    data = _as_json(raw)

    if isinstance(data, dict):
        statement = _first_str(
            data,
            ("statement", "陈述稿", "陈述", "speech", "script", "narration", "讲稿", "opening", "body", "text"),
        )
        qa = _coerce_qa(
            data.get("qa") if "qa" in data else
            data.get("QA") if "QA" in data else
            data.get("questions") if "questions" in data else
            data.get("qas") if "qas" in data else
            data.get("问答") if "问答" in data else
            data.get("预计提问") if "预计提问" in data else
            data.get("提问")
        )
    elif isinstance(data, list):
        # 整个数组既可能是问答列表，也可能是陈述稿的段落列表。
        if data and all(_looks_like_qa(item) for item in data):
            qa = _coerce_qa(data)
        else:
            statement, qa = _split_qa_text(_flat_text(data))

    if not statement and not qa:
        # JSON 里什么都没有（或根本不是 JSON）：当纯文本再试一次。
        text = _flat_text(raw, keep_newlines=True)
        statement, qa = _split_qa_text(text)

    return {"statement": statement.strip(), "qa": qa}


def _looks_like_qa(item: Any) -> bool:
    """判断一个元素是不是"问答对"。"""
    if isinstance(item, (list, tuple)):
        return len(item) >= 2
    if isinstance(item, dict):
        keys = {str(k).lower() for k in item}
        return bool(keys & {"question", "q", "问题", "提问", "ask", "answer", "a", "回答", "应答"})
    return False


def _coerce_qa(value: Any) -> list[dict]:
    """把各种形状的问答列表统一成 ``[{"question","answer"}]``。"""
    out: list[dict] = []
    # qa 被写成一个大字符串时，按「问：/答：」再切一次
    if isinstance(value, str):
        _, parsed = _split_qa_text(value)
        return parsed
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return out

    for item in value:
        if isinstance(item, dict):
            q = _first_str(item, ("question", "q", "问题", "提问", "ask", "title", "问"))
            a = _first_str(item, ("answer", "a", "回答", "应答", "reply", "content", "答"))
            if q or a:
                out.append({"question": q, "answer": a})
            continue
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            q, a = str(item[0]).strip(), "\n\n".join(_as_str_list(item[1]))
            if q or a:
                out.append({"question": q, "answer": a})
            continue
        if isinstance(item, str):
            _, parsed = _split_qa_text(item)
            if parsed:
                out.extend(parsed)
            elif item.strip():
                # 只有问题没有答案：保留问题（这本身就是有用的准备清单）
                out.append({"question": item.strip(), "answer": ""})
    return out


def _flat_text(raw: Any, *, keep_newlines: bool = False) -> str:
    """把任意输入压成一段纯文本。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, (list, tuple)):
        return "\n".join(_as_str_list(raw))
    if isinstance(raw, dict):
        parts = _as_str_list(list(raw.values()))
        return "\n".join(parts)
    return str(raw).strip()


def parse_slides(raw: Any) -> dict:
    """把模型输出规整成 ``{"slides": [{"title","bullets","notes"}]}``。

    返回值里额外带一个 ``warnings``（纯附加，不影响 ``slides`` 的结构）：
    用来如实报告"哪几页的要点超了 25 字""哪几页没有要点"。
    这类问题不该被静默修掉——25 字是有意的约束，超了就是要作者自己看一眼。

    对残缺输入（``{}`` / ``None`` / 缺字段 / 类型不对 / 纯文本）一律不崩，输出结构恒定。
    """
    warnings: list[str] = []
    slides: list[dict] = []
    data = _as_json(raw)

    items: Any = None
    if isinstance(data, dict):
        for key in ("slides", "pages", "幻灯片", "deck", "items", "outline"):
            if key in data:
                items = data[key]
                break
        if items is None and ("title" in data or "标题" in data):
            items = [data]                      # 只返回了一页
    elif isinstance(data, list):
        items = data

    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, (list, tuple)):
        items = None

    if items is None:
        # 纯文本兜底：按 Markdown 的标题层级切页。
        flat = _flat_text(raw, keep_newlines=True)
        slides = _slides_from_text(flat)
        if slides:
            warnings.append("模型没有按 JSON 输出，已按标题层级从纯文本里切出幻灯片。")

    for item in items or []:
        if isinstance(item, str):
            item = {"title": item, "bullets": []}
        if not isinstance(item, dict):
            continue
        title = _first_str(item, ("title", "标题", "heading", "name", "页标题"))
        bullets = _split_bullets(
            item.get("bullets") if "bullets" in item else
            item.get("points") if "points" in item else
            item.get("要点") if "要点" in item else
            item.get("items") if "items" in item else
            item.get("content")
        )
        notes = _first_str(item, ("notes", "note", "备注", "讲稿备注", "speaker_notes", "script"))
        if not title and not bullets and not notes:
            continue
        slides.append({"title": title, "bullets": bullets, "notes": notes})

    # 体检：把"超字数""没要点"如实报出来，而不是静默截断。
    long_bullets = sum(1 for s in slides for b in s["bullets"] if len(b) > BULLET_MAX_CHARS)
    if long_bullets:
        warnings.append(
            f"有 {long_bullets} 条要点超过 {BULLET_MAX_CHARS} 字，"
            "投到屏幕上会读不完，建议手工再压一压。"
        )
    empty = [i + 1 for i, s in enumerate(slides) if not s["bullets"]]
    if empty:
        warnings.append(f"第 {'、'.join(map(str, empty))} 页没有要点。")
    if not slides:
        warnings.append("没有解析出任何幻灯片，输入里可能没有 slides 字段。")

    return {"slides": slides, "warnings": warnings}


def _split_bullets(value: Any) -> list[str]:
    """把要点字段拆成条目列表。

    模型经常把要点写成**一个大字符串**（用换行或分号分隔）。这里两种都拆，
    但只在"拆完确实更多"时才拆分号，避免把一条本来含分号的要点切碎。
    """
    items = _as_str_list(value)
    out: list[str] = []
    for item in items:
        lines = [ln.strip(" -•\t") for ln in item.split("\n")]
        lines = [ln for ln in lines if ln]
        if len(lines) > 1:
            out.extend(lines)
            continue
        if "；" in item and len(item) > BULLET_MAX_CHARS:
            parts = [p.strip() for p in item.split("；") if p.strip()]
            out.extend(parts) if len(parts) > 1 else out.append(item)
            continue
        out.append(item)
    # 去掉残留的 Markdown 列表符号与序号前缀
    cleaned = []
    for item in out:
        text = re.sub(r"^[-*+•]\s*", "", item).strip()
        text = re.sub(r"^\d+[.)、]\s*", "", text).strip()
        if text:
            cleaned.append(text)
    return cleaned


def _slides_from_text(text: str) -> list[dict]:
    """从 Markdown/纯文本里切幻灯片：``#`` 开头当页标题，``-``/``1.`` 当要点。"""
    if not text.strip():
        return []
    slides: list[dict] = []
    current: dict | None = None
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^\s{0,3}(#{1,4})\s+(.*)$", line)
        if m:
            if current:
                slides.append(current)
            current = {"title": m.group(2).strip(), "bullets": [], "notes": ""}
            continue
        if re.match(r"^\s*(?:[-*+•]|\d+[.)])\s+", line):
            if current is None:
                current = {"title": "", "bullets": [], "notes": ""}
            current["bullets"].append(re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line).strip())
            continue
        if current is not None and not current["bullets"]:
            current["notes"] = (current["notes"] + " " + line.strip()).strip()
        elif current is None:
            current = {"title": line.strip(), "bullets": [], "notes": ""}
    if current:
        slides.append(current)
    return [s for s in slides if s["title"] or s["bullets"]]


# ---------------------------------------------------------------------------
# 渲染：答辩稿 Markdown
# ---------------------------------------------------------------------------
def script_to_markdown(script: dict) -> str:
    """把答辩稿渲染成 Markdown。

    与 ``suite/docx_export.markdown_to_docx`` 配合就能直接导出 Word——
    答辩稿是要打印出来念的，Word 比 Markdown 有用。
    """
    data = script if isinstance(script, dict) else {}
    statement = str(data.get("statement") or "").strip()
    qa = data.get("qa") if isinstance(data.get("qa"), list) else []

    parts = ["# 答辩稿", ""]
    parts.append(f"## 一、陈述稿（{SPEECH_MINUTES} 分钟口播）")
    parts.append("")
    parts.append(statement or f"（{INSUFFICIENT}：陈述稿为空）")
    parts.append("")
    parts.append("## 二、预计提问与应答")
    parts.append("")
    if not qa:
        parts.append(f"（{INSUFFICIENT}：没有解析出问答）")
    for i, item in enumerate(qa, 1):
        if not isinstance(item, dict):
            continue
        parts.append(f"### 问 {i}：{str(item.get('question') or '').strip()}")
        parts.append("")
        parts.append(str(item.get("answer") or "").strip() or f"（{INSUFFICIENT}）")
        parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 渲染：自包含 HTML 演示稿
# ---------------------------------------------------------------------------
def _h(value: Any) -> str:
    """转义成可安全放进 HTML 的文本。

    除了常规转义，还把 ``://`` 换成 ``&#58;//``。理由不是安全，而是**可验证性**：
    这样"这份 HTML 里不出现任何 http(s):// 字样"就成了一条能用正则机械断言的性质，
    哪怕模型在讲稿备注里写了一个网址也不会破坏它。浏览器渲染出来仍是一模一样的
    ``://``，对阅读没有任何影响。
    """
    text = html.escape(str(value or ""), quote=True)
    return text.replace("://", "&#58;//")


def _deck_slides(slides: Any, title: str) -> list[dict]:
    """把任意输入统一成一份可渲染的幻灯片列表。

    ``build_html_deck`` / ``build_pptx`` 共用它，所以两个产物**内容一定一致**：
    不会出现"HTML 有 14 页、PPTX 有 12 页"这种事。
    """
    if isinstance(slides, (dict, list, str)):
        parsed = parse_slides(slides)
    else:
        parsed = {"slides": []}
    out = [dict(s) for s in parsed.get("slides") or []]
    if not out:
        # 一页都没有时也要给一份能打开的产物，而不是空文件。
        out = [{
            "title": title or "答辩演示稿",
            "bullets": [f"{INSUFFICIENT}：没有解析出幻灯片内容"],
            "notes": "",
        }]
    return out


#: 页面骨架。刻意把 CSS 与 JS 全部内联，**不引任何 CDN、字体文件或图片**，
#: 这样它拷到没有网络的答辩电脑上也能直接打开。
_DECK_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0f1220;--fg:#eef1f8;--dim:#98a2bd;--accent:#6ea8fe;--panel:rgba(15,18,32,.94)}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);overflow:hidden;
font-family:"Microsoft YaHei","PingFang SC","Hiragino Sans GB","Source Han Sans SC","Noto Sans CJK SC","WenQuanYi Micro Hei",sans-serif}
#stage{position:relative;height:100%;width:100%}
.slide{position:absolute;top:0;right:0;bottom:0;left:0;display:none;
flex-direction:column;justify-content:center;padding:6vh 8vw;animation:fade .18s ease-out}
.slide.active{display:flex}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
.slide.cover{align-items:center;justify-content:center;text-align:center}
.slide h1{font-size:clamp(30px,4.4vw,62px);line-height:1.25;margin:0;font-weight:700}
.slide.cover .sub{color:var(--dim);font-size:clamp(14px,1.4vw,22px);margin-top:3vh;line-height:1.7}
.slide h2{font-size:clamp(21px,2.8vw,40px);line-height:1.3;margin:0 0 3.4vh;
border-left:6px solid var(--accent);padding-left:18px;font-weight:700}
.slide ul{list-style:none;margin:0;padding:0}
.slide li{font-size:clamp(17px,2vw,30px);line-height:1.85;margin:1.4vh 0;
padding-left:1.15em;position:relative}
.slide li:before{content:"";position:absolute;left:0;top:.72em;width:.42em;height:.42em;
border-radius:50%;background:var(--accent)}
#hud{position:fixed;right:18px;bottom:14px;display:flex;align-items:center;gap:10px;
color:var(--dim);font-size:14px;font-family:inherit}
#hud button{background:rgba(255,255,255,.08);color:var(--fg);border:1px solid rgba(255,255,255,.16);
border-radius:8px;padding:6px 12px;font-size:14px;cursor:pointer;font-family:inherit}
#hud button:hover{background:rgba(255,255,255,.18)}
#bar{position:fixed;top:0;left:0;height:3px;background:var(--accent);width:0;transition:width .2s}
#notes{position:fixed;left:0;right:0;bottom:0;max-height:34vh;overflow:auto;display:none;
background:var(--panel);border-top:1px solid rgba(255,255,255,.14);padding:16px 22px;
font-size:15px;line-height:1.75;color:var(--fg)}
body.with-notes #notes{display:block}
body.with-notes .slide{padding-bottom:36vh}
#notes .tag{color:var(--accent);font-size:13px;letter-spacing:.08em;margin-bottom:6px}
.hint{position:fixed;left:18px;bottom:16px;color:var(--dim);font-size:12px}
@media print{.slide{display:flex;position:static;page-break-after:always;height:100vh}
#hud,#notes,.hint,#bar{display:none}}
</style>
</head>
<body>
<div id="bar"></div>
<div id="stage">
__SLIDES__
</div>
<div id="notes"><div class="tag">讲稿备注（按 N 显示／隐藏）</div><div id="notesText"></div></div>
<div id="hud">
<button id="prev" type="button" title="上一页">←</button>
<span id="counter">1 / 1</span>
<button id="next" type="button" title="下一页">→</button>
<button id="toggle" type="button" title="显示讲稿备注">备注 N</button>
</div>
<div class="hint">← → 翻页 ｜ 空格下一页 ｜ Home / End 首末页 ｜ N 显示备注</div>
<script>
(function () {
  var slides = Array.prototype.slice.call(document.querySelectorAll('.slide'));
  var counter = document.getElementById('counter');
  var bar = document.getElementById('bar');
  var notesText = document.getElementById('notesText');
  var total = slides.length;
  var idx = 0;
  function clamp(n) { return n < 0 ? 0 : (n > total - 1 ? total - 1 : n); }
  function show(n) {
    idx = clamp(n);
    for (var i = 0; i < slides.length; i++) {
      if (i === idx) { slides[i].classList.add('active'); } else { slides[i].classList.remove('active'); }
    }
    counter.textContent = (idx + 1) + ' / ' + total;
    bar.style.width = (total > 1 ? (idx / (total - 1)) * 100 : 100) + '%';
    notesText.textContent = slides[idx].getAttribute('data-notes') || '（本页没有备注）';
    if (location.hash !== '#p' + (idx + 1)) {
      try { location.replace('#p' + (idx + 1)); } catch (e) { location.hash = '#p' + (idx + 1); }
    }
  }
  function next() { if (idx < total - 1) { show(idx + 1); } }
  function prev() { if (idx > 0) { show(idx - 1); } }
  function toggleNotes() { document.body.classList.toggle('with-notes'); }
  document.addEventListener('keydown', function (e) {
    var k = e.key;
    if (k === 'ArrowRight' || k === 'ArrowDown' || k === 'PageDown' || k === ' ' || k === 'Enter') {
      next(); e.preventDefault();
    } else if (k === 'ArrowLeft' || k === 'ArrowUp' || k === 'PageUp') {
      prev(); e.preventDefault();
    } else if (k === 'Home') { show(0); e.preventDefault(); }
    else if (k === 'End') { show(total - 1); e.preventDefault(); }
    else if (k === 'n' || k === 'N') { toggleNotes(); e.preventDefault(); }
  });
  document.getElementById('next').addEventListener('click', next);
  document.getElementById('prev').addEventListener('click', prev);
  document.getElementById('toggle').addEventListener('click', toggleNotes);
  var m = /^#p(\\d+)$/.exec(location.hash || '');
  show(m ? parseInt(m[1], 10) - 1 : 0);
})();
</script>
</body>
</html>
"""


def _render_slide_html(slide: dict, index: int) -> str:
    """渲染一页。首页当封面：不要要点圆点，标题居中放大。"""
    title = _h(slide.get("title") or "")
    bullets = [_h(b) for b in (slide.get("bullets") or []) if str(b).strip()]
    notes = _h(slide.get("notes") or "")
    is_cover = index == 0

    body: list[str] = []
    if is_cover:
        body.append(f"<h1>{title or '答辩演示稿'}</h1>")
        if bullets:
            body.append('<div class="sub">' + "<br>".join(bullets) + "</div>")
    else:
        body.append(f"<h2>{title}</h2>")
        if bullets:
            body.append("<ul>" + "".join(f"<li>{b}</li>" for b in bullets) + "</ul>")

    cls = "slide cover" if is_cover else "slide"
    return (
        f'<section class="{cls}" data-notes="{notes}">' + "".join(body) + "</section>"
    )


def build_html_deck(slides: Any, title: str = "") -> str:
    """生成**单文件** HTML 演示稿：离线可开、无外部依赖、键盘翻页。

    为什么这条路是默认路线：``python-pptx`` 不在本站的依赖里（见模块文档），
    而答辩现场最需要的是"拷到一台陌生电脑上双击就能投屏"的东西。
    单文件 HTML 恰好满足：不用装 Office、不用联网、不用字体文件，
    而且它同时是**备份**——真 PPT 打不开时可以直接投它。

    键盘：← → 翻页，空格下一页，Home / End 首末页，N 显示/隐藏讲稿备注。
    """
    parsed = _deck_slides(slides, title)
    inner = "\n".join(_render_slide_html(s, i) for i, s in enumerate(parsed))
    return (
        _DECK_TEMPLATE
        .replace("__TITLE__", _h(title or (parsed[0].get("title") if parsed else "") or "答辩演示稿"))
        .replace("__SLIDES__", inner)
    )


# ---------------------------------------------------------------------------
# 渲染：.pptx（需要 python-pptx）
# ---------------------------------------------------------------------------
#: 中文字体。pptx 的字体分 latin / ea 两套，两套都要设，否则中文回退到主题字体。
PPT_EA_FONT = "微软雅黑"
PPT_LATIN_FONT = "Segoe UI"


def _set_ppt_run_font(run: Any, *, size_pt: float, bold: bool = False) -> None:
    """给一段 pptx 文字设字体。

    **必须同时设 ``a:latin`` 与 ``a:ea``。** python-pptx 的 ``run.font.name``
    只写 ``a:latin``（西文），中文字符走 ``a:ea``——只设一半的结果是
    "英文按你指定的字体、中文却是主题默认字体"，中文演示稿里这就是"字体没生效"。
    （与 ``suite/docx_export.py`` 里必须设 ``w:eastAsia`` 是同一个坑。）
    """
    from pptx.oxml.ns import qn
    from pptx.util import Pt

    run.font.size = Pt(size_pt)
    run.font.bold = bold
    run.font.name = PPT_LATIN_FONT            # 这一步会创建 a:latin
    rpr = run.font._rPr
    latin = rpr.find(qn("a:latin"))
    ea = rpr.find(qn("a:ea"))
    if ea is None:
        ea = rpr.makeelement(qn("a:ea"), {})
        # 按 schema 顺序插在 a:latin 之后，避免 PowerPoint 报"文件需要修复"
        if latin is not None:
            latin.addnext(ea)
        else:
            rpr.append(ea)
    ea.set("typeface", PPT_EA_FONT)


def build_pptx(slides: Any, title: str = "") -> bytes:
    """生成真正的 ``.pptx``，返回字节。**需要 python-pptx。**

    .. warning::
       **这条分支在本机未经验证。** 运行环境里没有 ``python-pptx``
       （``import pptx`` 报 ``ModuleNotFoundError``，沙箱也装不了包），
       所以下面的代码是"按 API 写好但没跑过"的。请在有该库的环境里先跑一遍
       ``tools/smoke_defense.py`` 再上线；不要假设它一定能用。
       本模块默认交付的是 :func:`build_html_deck`（已验证）。

    装不上时抛 :class:`DefenseError` 并写明替代方案，不抛 ``ModuleNotFoundError``。
    """
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.util import Inches, Pt
    except ImportError as exc:
        raise DefenseError(PPTX_MISSING_HINT) from exc

    parsed = _deck_slides(slides, title)
    deck_title = title or (parsed[0].get("title") if parsed else "") or "答辩演示稿"

    try:
        prs = Presentation()
        # 16:9。默认模板是 4:3，投到现代投影仪上两侧会有黑边。
        prs.slide_width = Inches(13.333)
        prs.slide_height = Inches(7.5)
        blank = prs.slide_layouts[6]
        accent = RGBColor(0x2F, 0x6F, 0xED)
        ink = RGBColor(0x1A, 0x1D, 0x2B)
        dim = RGBColor(0x5A, 0x62, 0x7A)

        def add_text(slide, text, *, left, top, width, height, size, bold=False, color=ink):
            box = slide.shapes.add_textbox(left, top, width, height)
            frame = box.text_frame
            frame.word_wrap = True
            para = frame.paragraphs[0]
            run = para.add_run()
            run.text = text
            _set_ppt_run_font(run, size_pt=size, bold=bold)
            run.font.color.rgb = color
            return box

        for i, item in enumerate(parsed):
            slide = prs.slides.add_slide(blank)
            bullets = [str(b) for b in (item.get("bullets") or []) if str(b).strip()]
            heading = str(item.get("title") or "")

            if i == 0:
                add_text(slide, heading or deck_title, left=Inches(0.9), top=Inches(2.5),
                         width=Inches(11.5), height=Inches(1.6), size=40, bold=True)
                if bullets:
                    add_text(slide, "\n".join(bullets), left=Inches(0.9), top=Inches(4.3),
                             width=Inches(11.5), height=Inches(1.8), size=18, color=dim)
            else:
                add_text(slide, heading, left=Inches(0.8), top=Inches(0.55),
                         width=Inches(11.7), height=Inches(1.0), size=28, bold=True)
                if bullets:
                    box = slide.shapes.add_textbox(
                        Inches(0.9), Inches(1.9), Inches(11.5), Inches(4.9)
                    )
                    frame = box.text_frame
                    frame.word_wrap = True
                    for j, bullet in enumerate(bullets):
                        para = frame.paragraphs[0] if j == 0 else frame.add_paragraph()
                        para.level = 0
                        run = para.add_run()
                        run.text = "•  " + bullet
                        _set_ppt_run_font(run, size_pt=20)
                        run.font.color.rgb = ink
                        para.space_after = Pt(10)

            notes = str(item.get("notes") or "").strip()
            if notes:
                # 讲稿备注进 Word 备注栏：放映时讲者能看到、观众看不到。
                slide.notes_slide.notes_text_frame.text = notes

        buf = io.BytesIO()
        prs.save(buf)
        return buf.getvalue()
    except DefenseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 不让裸异常冒到接口层
        raise DefenseError(
            f"生成 .pptx 失败：{type(exc).__name__}: {exc}。"
            "可以改用 build_html_deck() 产出单文件 HTML 演示稿，它不依赖任何第三方库。"
        ) from exc


__all__ = [
    "BULLET_MAX_CHARS", "DECK_MODE", "DECK_SECTIONS", "DefenseError", "INSUFFICIENT",
    "MATERIAL_RULE", "PPTX_AVAILABLE", "QA_TOPICS", "build_html_deck", "build_pptx",
    "parse_script", "parse_slides", "pptx_available", "script_prompt",
    "script_to_markdown", "slides_prompt",
]
