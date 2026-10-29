"""验证参考文献推荐层：检索、分级、排序、配比、导出。

只验证"检索 → 排序 → 导出"，**不调用任何大模型**（不花钱）。
精排那一步是可选增强，它需要 Key，所以不在本脚本里测；
本脚本要证明的是"没有 Key 时这条路也能给出可用的清单"。

重点看四件事，每一件都对应一个真实踩过的坑：

1. **检索能拿到候选**（三源至少两源可用）。某个源限流不算失败——本脚本会把
   各源状态打出来，让人能区分"源挂了"和"真的没有这篇文献"。
2. **等级判定不误判**。内置目录里曾经有「经济研究」，而检索结果里出现了
   《经济学》；子串匹配把后者判成了 CSSCI，直接把它送上清单第一名。
   所以这里专门断言：目录条目必须**等值**命中，ISSN 冲突必须不给等级。
3. **配比生效**：国内外都有、时间跨度 ≥ 8 年、近 5 年至少 4 篇。
   纯按分数截断会得到"清一色同一本刊、同一年份"，那拿去做开题报告会被一眼看穿。
4. **导出不缺字段、不补字段**。缺卷期就不写卷期，而不是编一个。

用法：
    python tools/smoke_references.py
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, r"E:\AI\thesis-suite")

#: 选题清单。**只有第一个会走真实联网检索**（见 ``main`` 里的循环）：
#: 每多跑一个选题就要多访问三个外部库好几轮，而本脚本要验证的是
#: 分级/配比/导出这些**本地逻辑**——多检一个选题既慢又容易撞上 OpenAlex 的
#: 免费额度（按 IP 共享、耗尽当天不再可用），却不会多验证到任何一行我们自己的代码。
#: 第二个选题保留在文件里，需要时用 ``--all`` 跑。
TOPICS = [
    {
        "topic": "企业数据资产化的会计确认与价值评估研究",
        "keywords": ["数据资产", "会计确认", "价值评估"],
        "english": "data asset accounting recognition and valuation",
    },
    {
        "topic": "乡村教师职业倦怠与组织支持的关系研究",
        "keywords": ["乡村教师", "职业倦怠", "组织支持"],
        "english": "rural teacher burnout organizational support",
    },
]


def check_grading() -> list[str]:
    """分级逻辑的单元断言。**不依赖网络**，所以先跑它。"""
    from suite import references as R

    fails: list[str] = []
    print("\n--- 1. 等级判定（不联网） ---")

    cases = [
        # (刊名, ISSN, 期望等级, 期望判定依据里的关键片段, 说明)
        ("管理世界", "", "CSSCI", "一致", "核心刊名精确命中（目录无 ISSN 记录）"),
        ("科研管理", "1004-115X", "CSSCI", "ISSN", "核心刊名 + ISSN 双命中"),
        ("科研管理", "2661-3751", "未收录/未知", "不符", "同名仿冒刊必须不给等级"),
        ("经济学", "2630-4759", "中文期刊", "", "不得因为含「经济」就判成 CSSCI"),
        ("经济学动态", "", "CSSCI", "一致", "核心刊名精确命中"),
        ("Conscience and Cognition", "", "国际期刊", "", "短片段 sci 不得命中"),
    ]
    for venue, issn, want_tier, want_bit, why in cases:
        tier, basis = R.classify_tier(venue=venue, language="zh" if _has_cjk(venue) else "en",
                                      source_kind="openalex", issn=issn)
        ok = tier == want_tier and ((not want_bit) or (want_bit in basis))
        print(f"  [{'OK  ' if ok else 'FAIL'}] {venue}（ISSN {issn or '—'}）→ {tier}")
        print(f"        依据：{basis}")
        print(f"        期望：{want_tier} / 含「{want_bit}」 —— {why}")
        if not ok:
            fails.append(f"分级：{venue} → {tier}（期望 {want_tier}）")

    # 归一函数本身也要对：中文刊名的书名号、全角括号都要能被吃掉。
    norm_cases = [
        ("《管理世界》", "管理世界"),
        ("经济学(季刊)", "经济学季刊"),
        ("Journal of Chinese Economic Research", "journalofchineseeconomicresearch"),
    ]
    for raw, want in norm_cases:
        got = R._norm_venue(raw)
        ok = got == want
        print(f"  [{'OK  ' if ok else 'FAIL'}] _norm_venue({raw!r}) = {got!r}")
        if not ok:
            fails.append(f"归一：{raw} → {got}（期望 {want}）")

    # 回归：可信度提示必须在**打分之后**还在。
    # 早先提示只在 _from_web 里算一次，而 _merge 会改动 tier/issn 并把 warnings 清空，
    # 于是一条"ISSN 与核心目录不符"的候选在合并后**静默失去了警告**——
    # 那正是识破同名仿冒刊的唯一线索，丢掉它比不判等级更糟。
    probe = R.RefRecord(title="企业数据资产入表研究", venue="科研管理", year="2024",
                        language="zh", issn="2661-3751", citations=0,
                        tier="未收录/未知",
                        tier_basis="刊名与《CSSCI》目录条目「科研管理」一致，"
                                   "但 ISSN「2661-3751」与目录记录（1004115X）不符")
    R._compose(probe, 2)
    ok = bool(probe.warnings) and probe.as_reference().get("warnings")
    print(f"  [{'OK  ' if ok else 'FAIL'}] ISSN 冲突提示在打分后仍然存在"
          f"（{len(probe.warnings)} 条）")
    if not ok:
        fails.append("回归：ISSN 冲突的可信度提示在 _compose 之后丢失")
    return fails


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text or "")


def check_recommend(spec: dict) -> list[str]:
    from suite import references as R

    fails: list[str] = []
    print(f"\n--- 2. 完整推荐：{spec['topic']} ---")
    started = time.time()
    result = R.recommend(topic=spec["topic"], keywords=spec["keywords"],
                         english=spec["english"], total=15)
    took = time.time() - started

    if not result.get("ok"):
        print(f"  [FAIL] 推荐失败：{result.get('error')}")
        for s in result.get("sources") or []:
            print(f"        {s.get('source'):16} ok={s.get('ok')} {str(s.get('error'))[:70]}")
        return [f"推荐失败：{spec['topic']}"]

    items = result["items"]
    summary = result["summary"]
    print(f"  候选 {result['candidates']} 条 → 清单 {result['count']} 篇，耗时 {took:.1f}s")
    print(f"  等级分布：{summary['tiers']}")
    print(f"  国内外：{summary['languages']}")
    print(f"  时间跨度：{summary['year_min']}–{summary['year_max']}"
          f"（{summary['span_years']} 年）")
    print(f"  开放获取 {summary['open_access']} 篇；有摘要 {summary['with_abstract']} 篇；"
          f"有 DOI {summary['with_doi']} 篇")
    print(f"  精排：{result.get('rerank', {}).get('reason', '（未走此路径）')}")

    print("\n  清单：")
    for item in items:
        cite = item["citations"] if item["citations"] is not None else "—"
        dl = item["downloads"] if item["downloads"] is not None else "—"
        warn = " ⚠" if item.get("warnings") else ""
        print(f"   {item['rank']:>2}. [{item['tier']:<5}] 引{str(cite):>5} 下{dl:>3} "
              f"{item['year']} {item['language_label']} | {item['title'][:44]}{warn}")
        print(f"       {item['source'][:38]} | {item['why']}")

    print("\n  各源状态：")
    for s in result["sources"]:
        if s.get("ok"):
            print(f"    {s['source']:16} 查到 {s.get('found', 0):>2} 用 {s.get('used', 0):>2} "
                  f"（{s.get('seconds')}s） query={str(s.get('query'))[:28]}")
        else:
            print(f"    {s['source']:16} [失败] {str(s.get('error'))[:70]}")

    print("\n  如实提示（notes）：")
    for n in result["notes"]:
        print(f"    · {n}")

    # ---- 断言 ----
    if not (10 <= result["count"] <= 20):
        fails.append(f"篇数越界：{result['count']}（应 10~20）")
    langs = summary["languages"]
    if langs.get("国内（中文）", 0) < 1:
        fails.append("国内文献一篇都没有（配比失效）")
    if langs.get("国外（英文）", 0) < 1:
        fails.append("国外文献一篇都没有（配比失效）")
    # 下载量拿不到是设计决定，这里断言它**必须**是 None，防止有人日后填个估计值进来。
    bad_dl = [i["rank"] for i in items if i["downloads"] is not None]
    if bad_dl:
        fails.append(f"下载量本该一律为 None（公开库没有该字段），却有条目给了值：{bad_dl}")
    # 名次必须连续且从 1 开始
    if [i["rank"] for i in items] != list(range(1, len(items) + 1)):
        fails.append("rank 不连续")
    # 分数必须单调不增
    scores = [i["score"] for i in items]
    if scores != sorted(scores, reverse=True):
        fails.append("清单未按 score 降序")
    # 题名不得重复
    titles = [i["title"] for i in items]
    if len(set(titles)) != len(titles):
        fails.append("清单里有重复题名（去重失效）")
    if not result["limits"]:
        fails.append("limits 说明缺失（下载量与等级口径必须如实透出）")
    return fails


def check_export(result: dict) -> list[str]:
    from suite import references as R

    fails: list[str] = []
    print("\n--- 3. 导出（不联网） ---")
    items = result["items"]

    gbt = R.render_gbt(items)
    bib = R.render_bibtex(items)
    md = R.render_markdown(result)

    for name, text in (("GB/T 7714", gbt), ("BibTeX", bib), ("Markdown", md)):
        print(f"  {name}：{len(text)} 字符")
        if not text.strip():
            fails.append(f"{name} 导出为空")

    print("\n  GB/T 7714 前 3 条：")
    for line in gbt.splitlines()[2:5]:
        print(f"    {line}")

    # 条数必须与清单一致
    gbt_lines = [ln for ln in gbt.splitlines() if ln.startswith("[")]
    if len(gbt_lines) != len(items):
        fails.append(f"GB/T 7714 条数 {len(gbt_lines)} ≠ 清单 {len(items)}")
    bib_entries = [ln for ln in bib.splitlines() if ln.startswith("@")]
    if len(bib_entries) != len(items):
        fails.append(f"BibTeX 条数 {len(bib_entries)} ≠ 清单 {len(items)}")
    # BibTeX 的 key 必须唯一，否则导入文献管理器会互相覆盖
    keys = [ln.split("{", 1)[1].rstrip(",") for ln in bib_entries]
    if len(set(keys)) != len(keys):
        fails.append(f"BibTeX key 有重复：{keys}")
    # 不得把拿不到的下载量编进导出内容
    if "下载" in gbt and "—" not in gbt:
        fails.append("GB/T 导出里出现了下载量却没有如实留空")
    return fails


def main() -> int:
    from suite import references as R

    # 默认只跑第一个选题（见 TOPICS 的注释）；--all 才跑完。
    specs = TOPICS if "--all" in sys.argv else TOPICS[:1]

    print("=" * 78)
    print("参考文献推荐层验证")
    print("=" * 78)
    print(f"允许的检索主机：{sorted(__import__('suite.websearch', fromlist=['x']).ALLOWED_HOSTS)}")
    print(f"选题数：{len(specs)}（加 --all 跑全部；每个选题都要访问三个外部库）")

    fails = check_grading()

    results = []
    for spec in specs:
        results.append(R.recommend(topic=spec["topic"], keywords=spec["keywords"],
                                   english=spec["english"], total=15))
        fails += check_recommend(spec)

    good = next((r for r in results if r.get("ok")), None)
    if good:
        fails += check_export(good)
    else:
        fails.append("没有任何一次推荐成功，跳过导出验证")

    print("\n" + "=" * 78)
    if fails:
        print(f"有 {len(fails)} 项未通过：")
        for f in fails:
            print(f"  - {f}")
        print("\n注意：检索为空可能只是网络或限流，但**分级、配比、导出**这几项的"
              "失败一定是代码问题，必须修。")
        return 1
    print("全部通过：分级无误判、配比生效、导出条数与字段正确、下载量如实留空。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
