"""验证答辩材料生成（``suite/defense.py``）。

这条链路上没有模型调用（本模块只构造提示词、规整输出、渲染产物），所以本脚本
**完全离线、不花钱**，可以反复跑。重点看四件事：

1. **残缺输入不崩**：``parse_script`` / ``parse_slides`` 面对缺字段、类型不对、
   空对象、纯文本、``None``、数字，都必须返回**结构恒定**的结果。
   模型返回什么形状都有可能，用户已经等了一次调用，不该在这里失败。
2. **单文件 HTML**：``build_html_deck`` 的产物里**不能出现任何外部引用**
   （没有 http(s)://、没有 //cdn），并且带键盘切换脚本。
   答辩现场经常是没有网络的电脑，这条是硬要求。
3. **pptx 路线**：仅当环境里真的装了 python-pptx 时才验证
   （字节以 PK 开头、能用 pptx 读回、页数正确）。装不上就**如实说没验证**。
4. **提示词纪律**：必须真的包含"不得编造""只依据材料""材料不足"这类约束——
   只写在文档注释里的约束等于没有。

用法：
    python tools/smoke_defense.py
"""

from __future__ import annotations

import os
import re
import sys

for _p in (
    r"E:\AI\thesis-suite",
    r"E:\AI\aigc-reducer\libs",
    r"E:\AI\thesis-assistant\libs",
):
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

FAILURES: list[str] = []

#: 外部引用的特征。其中 ``https?://`` 是最关键的一条：只要出现，
#: 这份"单文件"HTML 就可能在断网的答辩电脑上变成一片空白。
EXTERNAL_PATTERNS = (
    (r"https?://", "http(s):// 绝对地址"),
    (r"//cdn", "协议相对的 CDN 地址"),
    (r"@import", "CSS @import"),
    (r"<link\b", "<link> 外部资源"),
    (r"<script[^>]+\bsrc\s*=", "外部脚本 src"),
    (r"<img\b", "<img> 外部图片"),
)


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  [{'OK  ' if ok else 'FAIL'}] {label}" + (f"：{detail}" if detail else ""))
    if not ok:
        FAILURES.append(label)


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    from suite import defense

    print("=" * 78)
    print("答辩材料生成（suite/defense.py）验证")
    print("=" * 78)
    print(f"pptx 可用 = {defense.PPTX_AVAILABLE}    选定主路线 DECK_MODE = {defense.DECK_MODE}")
    print(f"要点字数上限 = {defense.BULLET_MAX_CHARS}   每页 {defense.BULLET_MIN}~{defense.BULLET_MAX} 条"
          f"   共 {defense.SLIDES_MIN}~{defense.SLIDES_MAX} 页")

    # ---------------------------------------------------------------- 1
    print("\n--- 1. 提示词纪律：不得编造 / 只依据材料 / 允许说材料不足 ---")
    sp = defense.script_prompt(
        "乡村教师职业倦怠与组织支持的关系研究",
        "硕士学位论文",
        "本研究以某省 12 所乡村小学的 316 名教师为对象，采用问卷调查法。"
        "结果显示组织支持与职业倦怠呈显著负相关（r=-0.42, p<0.01）。",
        ["Wang (2020) 组织支持理论综述", "Li (2021) 乡村教师职业压力研究"],
    )
    lp = defense.slides_prompt(
        "乡村教师职业倦怠与组织支持的关系研究",
        "本研究以某省 12 所乡村小学的 316 名教师为对象，采用问卷调查法。",
        ["第1章 绪论", "第2章 文献综述", "第3章 研究设计", "第4章 结果与分析", "第5章 结论"],
    )
    for name, msgs in (("script_prompt", sp), ("slides_prompt", lp)):
        blob = "\n".join(str(m.get("content") or "") for m in msgs)
        check(isinstance(msgs, list) and len(msgs) >= 2 and all(
            set(m) >= {"role", "content"} for m in msgs), f"{name} 返回标准 chat messages")
        check("不得编造" in blob, f"{name} 含「不得编造」")
        check("只依据材料" in blob, f"{name} 含「只依据材料」")
        check(defense.INSUFFICIENT in blob, f"{name} 允许标注「{defense.INSUFFICIENT}」")
        check("材料开始" in blob and "材料结束" in blob, f"{name} 把材料划出了明确边界")

    sp_blob = "\n".join(str(m.get("content") or "") for m in sp)
    check("1500~2000 字" in sp_blob, "陈述稿给了 1500~2000 字的字数要求")
    check("8~10 分钟" in sp_blob, "陈述稿给了 8~10 分钟的口播时长要求")
    for name, _hint in defense.QA_TOPICS:
        check(name in sp_blob, f"问答覆盖了「{name}」")
    check(defense.INSUFFICIENT in sp_blob and "建议补充" in sp_blob,
          "材料不足时要模型给出「建议补充」而不是编答案")

    lp_blob = "\n".join(str(m.get("content") or "") for m in lp)
    check("不超过 25 个字" in lp_blob, "幻灯片要求每条要点不超过 25 字")
    check("12~16 页" in lp_blob, "幻灯片要求 12~16 页")
    check("notes" in lp_blob and "讲稿备注" in lp_blob, "幻灯片要求每页给讲稿备注")
    check("演示稿不是文档" in lp_blob, "解释了为什么限 25 字（不是审美偏好）")
    for sec in ("封面", "研究背景与问题", "文献缺口", "研究设计", "主要发现",
                "结论与贡献", "不足与展望", "致谢"):
        check(sec in lp_blob, f"幻灯片结构含「{sec}」")

    # 材料超长时必须**明说**被截断了，否则模型会以为自己看到了全文
    long_draft = "这是一段很长的论文材料。" * 4000
    lp_long = defense.slides_prompt("题目", long_draft, [])
    check("已被截断" in lp_long[1]["content"], "超长材料会被截断，且提示词里**明说**已截断")

    # ---------------------------------------------------------------- 2
    print("\n--- 2. parse_script / parse_slides：残缺输入不崩、结构恒定 ---")
    script_cases = [
        ("正常对象", {"statement": "各位老师好。", "qa": [{"question": "创新点？", "answer": "在样本上。"}]}),
        ("空对象", {}),
        ("None", None),
        ("数字", 12345),
        ("缺 qa", {"statement": "只有陈述稿。"}),
        ("缺 statement", {"qa": [{"question": "问？", "answer": "答。"}]}),
        ("qa 是字符串", {"statement": "稿。", "qa": "问：A？\n答：B。"}),
        ("qa 是二元数组列表", {"statement": "稿。", "qa": [["问一？", "答一。"]]}),
        ("qa 里混了垃圾", {"statement": "稿。", "qa": [None, 42, {"question": "问？"}, "孤立的问题？"]}),
        ("类型全不对", {"statement": 123, "qa": 456}),
        ("整段纯文本", "各位老师好，下面开始汇报。\n\n问：创新点在哪？\n答：在样本构成上。"),
        ("JSON 字符串", '```json\n{"statement":"稿。","qa":[{"question":"问？","answer":"答。"}]}\n```'),
        ("中文键名", {"陈述稿": "稿。", "问答": [{"问题": "问？", "回答": "答。"}]}),
        ("列表输入", ["第一段。", "第二段。"]),
    ]
    for label, raw in script_cases:
        try:
            result = defense.parse_script(raw)
            keys_ok = set(result) == {"statement", "qa"}
            type_ok = isinstance(result.get("statement"), str) and isinstance(result.get("qa"), list)
            qa_ok = all(
                isinstance(q, dict) and set(q) >= {"question", "answer"}
                and isinstance(q["question"], str) and isinstance(q["answer"], str)
                for q in result.get("qa") or []
            )
            preview = result["statement"][:22].replace("\n", " ") or "（空）"
            print(f"    {label:16} → statement={preview!r} qa={len(result['qa'])} 组")
            check(keys_ok and type_ok and qa_ok, f"parse_script({label}) 结构恒定")
        except Exception as exc:  # noqa: BLE001
            check(False, f"parse_script({label}) 抛异常", f"{type(exc).__name__}: {exc}")

    # 纯文本里的「问：/答：」必须真的被切出来
    qa_text = defense.parse_script("开场白。\n\n问：创新点？\n答：样本构成。\n\n问：局限？\n答：单省样本。")
    check(len(qa_text["qa"]) == 2 and qa_text["statement"].startswith("开场白"),
          "纯文本兜底：切出 2 组问答且保留了陈述稿",
          f"qa={[q['question'] for q in qa_text['qa']]}")

    slides_cases = [
        ("正常对象", {"slides": [{"title": "封面", "bullets": ["题目", "姓名"], "notes": "开场"}]}),
        ("空对象", {}),
        ("None", None),
        ("slides 是字符串", {"slides": "不是列表"}),
        ("缺 bullets", {"slides": [{"title": "只有标题"}]}),
        ("bullets 是字符串", {"slides": [{"title": "标题", "bullets": "要点一\n要点二"}]}),
        ("整页是字符串", {"slides": ["一页标题"]}),
        ("中文键名", {"幻灯片": [{"标题": "封面", "要点": ["甲", "乙"], "备注": "开场"}]}),
        ("数组里混垃圾", {"slides": [None, 7, {"title": "页", "bullets": [None, 3]}]}),
        ("纯文本 Markdown", "# 封面\n- 题目\n- 姓名\n\n# 研究背景\n- 乡村教师流失\n- 组织支持不足"),
        ("单页对象", {"title": "封面", "bullets": ["题目"]}),
    ]
    for label, raw in slides_cases:
        try:
            result = defense.parse_slides(raw)
            ok = (
                isinstance(result, dict) and isinstance(result.get("slides"), list)
                and isinstance(result.get("warnings"), list)
                and all(
                    isinstance(s, dict) and set(s) >= {"title", "bullets", "notes"}
                    and isinstance(s["title"], str)
                    and isinstance(s["bullets"], list)
                    and all(isinstance(b, str) for b in s["bullets"])
                    and isinstance(s["notes"], str)
                    for s in result["slides"]
                )
            )
            titles = [s["title"] for s in result["slides"]]
            print(f"    {label:16} → {len(result['slides'])} 页 {titles[:4]}"
                  f"{' …' if len(titles) > 4 else ''}")
            check(ok, f"parse_slides({label}) 结构恒定")
        except Exception as exc:  # noqa: BLE001
            check(False, f"parse_slides({label}) 抛异常", f"{type(exc).__name__}: {exc}")

    # 超字数要点要被**如实报出**，而不是静默截断
    long_bullet = "这是一条远远超过二十五个字上限的要点" * 3
    w = defense.parse_slides({"slides": [{"title": "页", "bullets": [long_bullet]}]})
    check(len(w["slides"][0]["bullets"][0]) == len(long_bullet), "要点超长时**不被静默截断**")
    check(any("超过" in x for x in w["warnings"]), "要点超长时进 warnings 如实报告")

    # ---------------------------------------------------------------- 3
    print("\n--- 3. build_html_deck：单文件、无外部引用、键盘可切换 ---")
    demo = defense.parse_slides({
        "slides": [
            {"title": "乡村教师职业倦怠研究", "bullets": ["答辩人：张三", "指导教师：李四"],
             "notes": "各位老师上午好。"},
            {"title": "研究背景与问题", "bullets": ["乡村教师流失率上升", "组织支持是可用抓手"],
             "notes": "先讲背景。"},
            {"title": "主要发现", "bullets": [defense.INSUFFICIENT], "notes": "这一页缺数据。"},
        ]
    })
    deck = defense.build_html_deck(demo, title="乡村教师职业倦怠研究")

    check(deck.lstrip().lower().startswith("<!doctype html>"), "产物是完整 HTML 文档")
    check(isinstance(deck, str) and len(deck) > 1500, "产物非空", f"{len(deck)} 字符")
    for pattern, what in EXTERNAL_PATTERNS:
        hits = re.findall(pattern, deck, flags=re.I)
        check(not hits, f"不含外部引用：{what}", f"命中 {hits[:3]}" if hits else "")
    check("http" not in deck.lower(), "整份文件里连 http 这个字样都没有")
    check("<!--" not in deck, "没有 HTML 注释（避免注释里夹带外部地址）")

    check("addEventListener('keydown'" in deck, "含键盘事件监听")
    check("ArrowLeft" in deck and "ArrowRight" in deck, "← / → 切换已绑定")
    check("PageDown" in deck and "Home" in deck and "End" in deck, "翻页/首末页快捷键已绑定")
    check(".slide.active" in deck or "classList.add('active')" in deck, "含翻页的显示逻辑")
    check("charset=\"utf-8\"" in deck.lower(), "声明了 UTF-8（中文不乱码）")
    check("Microsoft YaHei" in deck and "sans-serif" in deck,
          "中文字体走系统字体栈（不引字体文件）")
    check("<style>" in deck and "<script>" in deck, "CSS 与 JS 都内联")

    # 页面数要与输入一致（封面也在内）
    pages = deck.count('<section class="slide')
    check(pages == 3, "页数与输入一致", f"HTML 里 {pages} 页 / 输入 {len(demo['slides'])} 页")
    check(defense.INSUFFICIENT in deck, "「材料不足」被如实渲染进页面")

    # 恶意/畸形内容：必须转义，不能破坏文档结构
    nasty = defense.parse_slides({
        "slides": [{"title": '</section><script>alert(1)</script>',
                    "bullets": ['<img src=x onerror=alert(1)>'],
                    "notes": "访问 https://example.com 看详情"}]
    })
    nasty_deck = defense.build_html_deck(nasty, title="<b>注入</b>")
    check("<script>alert(1)</script>" not in nasty_deck, "标题里的 <script> 被转义")
    check("<img src=x" not in nasty_deck, "要点里的 <img> 被转义")
    check("&lt;script&gt;" in nasty_deck, "转义确实发生了（能看到 &lt;script&gt;）")
    check("https://example.com" not in nasty_deck,
          "备注里的网址也被中和成 &#58;//，单文件性质可机械断言")
    check("example.com" in nasty_deck, "但网址文本本身仍可读（没有丢内容）")

    # 空输入也要产出一份能打开的东西，而不是空文件
    empty_deck = defense.build_html_deck({}, title="空演示稿")
    check("<section class=\"slide" in empty_deck and len(empty_deck) > 1000,
          "空输入也能产出一份可打开的 HTML")

    # ---------------------------------------------------------------- 4
    print("\n--- 4. build_pptx：仅在装了 python-pptx 时验证 ---")
    if defense.pptx_available():
        try:
            raw = defense.build_pptx(demo, title="乡村教师职业倦怠研究")
            check(isinstance(raw, bytes) and raw[:2] == b"PK", "产物以 PK（zip）开头",
                  f"{len(raw)} 字节，头 {raw[:4]!r}")
            import io

            from pptx import Presentation

            prs = Presentation(io.BytesIO(raw))
            got = len(prs.slides)
            check(got == len(demo["slides"]), "能用 pptx 反向读回且页数正确",
                  f"读回 {got} 页 / 输入 {len(demo['slides'])} 页")
            check(prs.slide_width > prs.slide_height, "是 16:9 横幅版式")
        except Exception as exc:  # noqa: BLE001
            check(False, "build_pptx 失败", f"{type(exc).__name__}: {exc}")
    else:
        print("  [SKIP] 本机没有 python-pptx（import pptx → ModuleNotFoundError），")
        print("         **未能验证 build_pptx**（不假装跑过）。")
        print("         沙箱装不了包，所以本模块主路线改为 HTML（见 DECK_MODE）。")
        try:
            defense.build_pptx(demo, title="x")
            check(False, "没有 pptx 时 build_pptx 竟然成功了")
        except defense.DefenseError as exc:
            check("python-pptx" in str(exc) and "build_html_deck" in str(exc),
                  "没有 pptx 时抛的是可读 DefenseError（含替代方案），不是 ModuleNotFoundError",
                  str(exc)[:80] + "…")
        except Exception as exc:  # noqa: BLE001
            check(False, "抛了非 DefenseError 的异常", f"{type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- 5
    print("\n--- 5. script_to_markdown（答辩稿的可下载产物）---")
    md = defense.script_to_markdown({
        "statement": "各位老师好，我是……",
        "qa": [{"question": "创新点？", "answer": "在样本构成与测量工具上。"}],
    })
    check(md.startswith("# 答辩稿"), "Markdown 以标题开头")
    check("陈述稿" in md and "预计提问与应答" in md, "含陈述稿与问答两部分")
    check("### 问 1：创新点？" in md, "问答被编号渲染")
    md_empty = defense.script_to_markdown({})
    check(defense.INSUFFICIENT in md_empty, "空答辩稿渲染成「材料不足」而不是空白")
    md_junk = defense.script_to_markdown("不是字典")  # type: ignore[arg-type]
    check(isinstance(md_junk, str) and defense.INSUFFICIENT in md_junk,
          "非字典输入不崩，退化成「材料不足」")

    print("\n" + "=" * 78)
    if FAILURES:
        print(f"有 {len(FAILURES)} 项失败：")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
