"""提示词构造：把用户的选择变成一次高质量的选题请求。

设计要点：
* 强制 JSON，字段名固定，方便解析与渲染；
* 明确要求「题目之间必须差异化」，避免 8 个题目换汤不换药；
* 要求文献尽量给出真实可检索的出处，拿不准的要标注 verified=false，
  绝不允许编造 —— 这一点会同时在界面和导出文件里提示用户复核。
"""

from __future__ import annotations

import textwrap

SCHEMA_BLOCK = textwrap.dedent(
    """
    {
      "notice": "给用户的一句总体提示，例如选题的共同取向、使用建议",
      "topics": [
        {
          "title": "中文题目，不超过 30 字，必须是学术论文题目的写法",
          "english_title": "英文题目（可留空）",
          "type": "研究类型，只能取：理论探索 / 实证研究 / 案例研究 / 应用设计 / 综述与元分析 / 比较研究",
          "difficulty": "难度，只能取：偏易（可本科）/ 适中（可硕士）/ 偏难（可博士/核心期刊）",
          "summary": "一句话说清研究什么、用什么方法、解决什么问题（60 字以内）",
          "research_questions": ["具体可回答的研究问题，2-4 条"],
          "theory": "所依托的理论或分析框架，如计划行为理论、技术接受模型、扎根理论等",
          "methodology": "研究方法，要具体到设计和分析手段，如准实验设计+双重差分、扎根理论编码、结构方程模型、消融实验",
          "data_sources": "数据来源与获取方式，如某平台公开数据、问卷、访谈、公开数据库、自建语料",
          "innovations": "相对已有研究的创新点，1-3 句",
          "feasibility": "可行性判断：工作量、数据可得性、时间成本",
          "risks": "做这个题目最容易踩的坑",
          "keywords": ["关键词", "3-5 个"],
          "brief": {
            "background": "研究背景与问题的由来，150-250 字",
            "literature": [
              {
                "citation": "文献出处，按 GB/T 7714 格式；无法确认真实性时在末尾加「（待核实）」",
                "contribution": "这篇文献做了什么、结论是什么",
                "critique": "它的局限或与你选题的关系",
                "verified": false
              }
            ],
            "commentary": "文献短评：把上面几条文献串成一段有观点的述评，指出共识、分歧与研究缺口，200-350 字",
            "gap": "明确写出尚未解决的问题，也就是本选题的立足点",
            "references": ["再补充 2-4 条参考文献，同样标注是否需要核实"]
          }
        }
      ]
    }
    """
).strip()


def _line(label: str, value: str) -> str:
    return f"- {label}：{value}\n" if value else ""


def build_user_prompt(
    discipline: str,
    direction: str = "",
    level: str = "硕士学位论文",
    topic_types: list[str] | None = None,
    count: int = 6,
    keywords: str = "",
    methodology: str = "",
    language: str = "中文",
    seed: str = "",
    extra: str = "",
    avoid: list[str] | None = None,
) -> str:
    types = "、".join(topic_types) if topic_types else "由你判断，尽量覆盖不同类型"
    parts = [
        "请为下面的需求设计论文选题。\n\n",
        "## 需求\n",
        _line("学科/专业", discipline),
        _line("具体方向或关注点", direction),
        _line("论文层次", level),
        _line("希望的选题类型", types),
        _line("必须涉及的方法或技术", methodology),
        _line("必须包含的关键词", keywords),
        _line("写作语言", language),
        _line("随机种子（影响选题取向，不必理解它）", seed),
        _line("补充要求", extra),
        f"- 题目数量：{count} 个（不少于 {count} 个，一个一个数清楚）\n",
        "\n## 硬性要求\n",
        f"1. 必须给出 **{count} 个** 彼此差异明显的题目：研究对象、数据来源、方法路径至少有一项不同，"
        "不允许只是把标题里的词换来换去。\n",
        "2. 每个题目都要配一份「简报」：研究背景 + 4-8 条带短评的文献 + 一段有观点的文献述评 + 研究缺口。\n",
        "3. 题目要能落地：本科生题目不要把工作量开到博士规模，硕士题目要有明确可交付的数据。\n",
        "4. 文献引用：优先给出你确有把握的经典文献与高被引文献；记不准年份、卷期时宁可写成"
        "「作者. 题名[J]. 期刊名（待核实）」，也绝不要编造一个看起来很真的出处。"
        "拿不准的条目把 verified 设为 false。\n",
        "5. 如果需求里出现了某个具体方法或关键词，每个题目都必须真的用上它，不要只在标题里提一句。\n",
        "6. 不要出现「浅析」「初探」「探析」这类空泛措辞，题目要具体、可检验。\n",
    ]
    if avoid:
        parts.append(
            "\n## 已经给过的题目（必须换一批，不要重复以下任何题目）\n"
            + "\n".join(f"- {item}" for item in avoid[:80])
            + "\n"
        )
    parts.append(
        "\n## 输出格式\n只输出一个 JSON 对象，不要任何解释文字、不要 markdown 代码块，结构如下：\n\n"
        + SCHEMA_BLOCK
        + "\n"
    )
    return "".join(parts)


def build_messages(system_prompt: str, user_prompt: str) -> list[dict]:
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def build_completion_prompt(items: list[dict], discipline: str = "") -> str:
    """题目已经定了，只补齐简报。

    用在「有个别题目的简报没写完」时。比整批重生成便宜得多，
    而且不会把已经定好的题目换掉。
    """
    lines = [
        "下面这些论文题目已经确定，不需要你改题目，只需要为每一个补一份完整简报。\n",
        f"\n学科：{discipline or '（未指定，按题目推断）'}\n",
        "\n## 题目清单\n",
    ]
    for index, item in enumerate(items, 1):
        lines.append(f"{index}. {item.get('title', '')}\n")
        if item.get("methodology"):
            lines.append(f"   研究方法：{item['methodology']}\n")
    lines.append(
        "\n## 要求\n"
        "1. 每个题目都要有：研究背景（150-250 字）、4-8 条带短评的文献、"
        "一段 200-350 字的文献述评、明确的研究缺口、以及参考文献线索。\n"
        "2. 文献优先给出你有把握的经典与高被引文献；记不准卷期年份时标注「（待核实）」，"
        "绝不编造。拿不准的把 verified 设为 false。\n"
        "3. 顺序必须与上面题目清单一致。\n"
        "\n## 输出格式\n只输出 JSON，不要解释文字、不要代码块：\n"
        '{"topics":[{"title":"与清单完全一致的题目","brief":{"background":"…",'
        '"literature":[{"citation":"…","contribution":"…","critique":"…","verified":false}],'
        '"commentary":"…","gap":"…","references":["…"]}}]}\n'
    )
    return "".join(lines)
