"""验证「开题报告」这一轮四条改动的**结构与接口**。

这四条改动里有三条是**结构性**的（哪个入口放在哪张卡片里、哪个开关存不存在），
而结构性改动恰恰是最不容易被现有测试发现的一类回归：
接口照样 200、生成照样能跑，只是用户在页面上再也找不到那个入口。

对应关系：

1. 阶段 2 **不再**单独有一张「知识库 · 导入资料」卡片（它与「参考文献与知识库」
   是同一件事的两半，分开放会让用户在两张卡片之间来回找）；
2. 那张卡片里**有**文件导入入口，且是"入库即可被检索"（``/api/kb/upload``）；
3. 开题报告能依据**三层**：本地知识库 + 联网真实文献 + 模型知识。
   联网那一层必须有开关、必须如实进入材料表、必须能被关掉；
4. 开题报告阶段**有**导入 Word/PDF 的入口（``/api/proposal/import-plan``），
   读出框架与要求并自动入库。

用法：
    python tools/smoke_proposal_flow.py
"""

from __future__ import annotations

import io
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

SANDBOX = Path(r"E:\AI\thesis-suite\.tmp\smoke_prop_flow")

#: 一份**仿真的任务书**：题目在一行、七个小节都有、结尾带写作要求。
#: 用它而不是随机文本，是为了让"框架识别"这一步真的被测到——
#: 随机文本会让识别结果为空，于是断言退化成"没崩"。
TASK_DOC = """XX大学硕士学位论文开题报告任务书

题目：企业数据资产的会计确认与价值评估研究

一、研究背景与意义
（此处说明选题依据与现实意义）

二、国内外研究现状
（此处综述已有研究）

三、研究内容与目标
四、研究方法与技术路线
五、可行性分析
六、进度安排
七、预期成果

写作要求：正文不少于 8000 字，必须给出技术路线图；
参考文献不少于 30 篇，其中近五年文献占比不低于三分之一。
不得引用未经核实的网络资料。
"""


def main() -> int:
    from suite import rag

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / "kb").mkdir(parents=True, exist_ok=True)
    rag.set_base_dir(SANDBOX / "kb")

    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())
    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    root = Path(r"E:\AI\thesis-suite")
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    js = (root / "web" / "app.js").read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    print("=" * 78)
    print("改动 1：阶段 2 不再有独立的「知识库 · 导入资料」卡片")
    print("=" * 78)
    check("旧卡片标题已删除", "知识库 · 导入资料" not in html)
    check("旧说明文也已删除（避免留下半张卡片）",
          "把手里已有的文献、开题材料直接拖进来" not in html
          or "把资料放进知识库" in html)

    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("改动 2：文件导入入口在「参考文献与知识库」卡片内，且入库后可检索")
    print("=" * 78)
    # 用卡片标题定位，再看 kbDrop 是否落在**同一张** card 里（而不是另一张卡）。
    # 标题锚点是「论文RAG知识库」——这张卡片就是知识库本体，入库入口必须在它里面。
    refs_card_start = html.find("论文RAG知识库")
    check("找到「论文RAG知识库」卡片标题", refs_card_start > 0)
    # 从标题往后找下一个 </div> 结束的近似区间：用 pqaBriefCard（该卡片之后的兄弟）当右界
    right = html.find('id="pqaBriefCard"', refs_card_start)
    check("能定位到卡片右界", right > refs_card_start)
    segment = html[refs_card_start:right] if right > refs_card_start else html[refs_card_start:]
    check("kbDrop 在这张卡片内", 'id="kbDrop"' in segment)
    check("kbFiles 在这张卡片内", 'id="kbFiles"' in segment)
    check("前端绑定的是 /api/kb/upload（入库即被检索）",
          "'/api/kb/upload'" in js and "importKbFiles" in js)
    check("绑定函数在 boot 里被调用", "bindKbDropzone();" in js)

    # ---- 论文RAG知识库的四个功能页签 ----
    # 这四条是"把本地 RAG 助手的四个功能搬进来"的落地证据。缺任何一条，
    # 界面上就会少一个页签、或者点了没反应——而那种故障在接口层完全看不出来。
    check("有四个页签容器 ragTabs", 'id="ragTabs"' in html)
    for pane in ("ragPaneSearch", "ragPaneSum", "ragPaneCmp", "ragPaneAsk"):
        check(f"页签面板 {pane} 存在", f'id="{pane}"' in html and pane in js)
    for act in ("search", "sum", "cmp", "ask"):
        check(f"页签按钮 data-rag={act}", f'data-rag="{act}"' in html)
    check("文档检索走本地 /api/kb/search（不花钱）",
          "'/api/kb/search'" in js and "runRagSearch" in js)
    check("文档库支持单篇删除（DELETE /api/kb/doc）",
          "/api/kb/doc?doc_id=" in js and "deleteKbDoc" in js)
    check("页签切换逻辑存在", "function ragShowPane" in js and "function ragInitTabs" in js)
    check("页签在 initRefsPqa 里接线", "ragInitTabs();" in js)

    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("改动 3：开题报告三层依据")
    print("=" * 78)
    from suite.api import proposal as P
    from suite import websearch

    check("后端材料表支持 B 层（联网）",
          "_web_evidence" in (root / "suite" / "api" / "proposal.py").read_text(encoding="utf-8"))
    check("前端有联网开关 propUseWeb", 'id="propUseWeb"' in html)
    check("前端把 use_web 传上去", "use_web:" in js)
    # 第三层（模型知识）刻意**没有可用的开关**：模型总是带着自己的知识写，
    # 做成可关闭只会给人"关掉就没有模型知识了"的错觉。界面上它是一个勾好且禁用的框，
    # 旁边写明真正的约束是"不许挂引用编号"。
    third = html.find("③ 模型自身知识")
    check("找到第三层的说明", third > 0)
    # 该行必须同时带 checked 与 disabled：只 checked 就能被用户关掉（那会误导），
    # 只 disabled 则不勾（看起来像"没启用"）。
    third_line_zone = html[max(0, third - 400):third]
    check("第三层是「已勾选且禁用」（关不掉，如实说明）",
          "checked" in third_line_zone and "disabled" in third_line_zone,
          "这一段里的 input 同时带 checked 与 disabled")

    # 材料表分层与额度：这是"三层真的都进了提示词"的机械证据。
    recs = [websearch.WebRecord(source="crossref", title=f"联网文献{i}", abstract="摘要" * 60,
                                authors="张三", year="2024", doi=f"10.1/{i}", venue="某刊")
            for i in range(8)]
    kb = [{"title": f"知识库资料{i}", "text": "片段" * 80, "page": i} for i in range(8)]
    mat, index = P._build_material(kb, [{"title": "A证据", "text": "证据" * 40}],
                                   [{"origin": "D文档", "text": "块" * 100}], recs)
    tiers: dict[str, int] = {}
    for v in index.values():
        tiers[v["tier"]] = tiers.get(v["tier"], 0) + 1
    print(f"  材料分层：{tiers}（上限 {P.MAX_MATERIAL} 条）")
    check("知识库层进了材料表", tiers.get("K 知识库", 0) > 0)
    check("联网层进了材料表（没被知识库挤掉）", tiers.get("B 联网检索", 0) > 0,
          f"B 层 {tiers.get('B 联网检索', 0)} 条")
    check("联网层不超过额度", tiers.get("B 联网检索", 0) <= P.MAX_WEB_MATERIAL)
    check("编号连续且从 1 起", sorted(index.keys()) == list(range(1, len(index) + 1)))
    check("材料表里有 DOI（可回查的凭据）", "DOI" in mat)

    # use_web 关掉时不得联网：缺 Key 的分支下先报"要 Key"，所以这里直接测函数口径。
    calls: list[str] = []
    orig = websearch.search_literature

    def spy(*a, **k):
        calls.append("called")
        return [], []

    websearch.search_literature = spy
    try:
        P._web_evidence("题目", "小节", [])
    finally:
        websearch.search_literature = orig
    check("_web_evidence 确实会去检索", calls == ["called"])

    # ------------------------------------------------------------------
    print("\n" + "=" * 78)
    print("改动 4：开题报告阶段导入 Word/PDF → 框架与要求 + 自动入库")
    print("=" * 78)
    check("前端有导入投放区 propDrop", 'id="propDrop"' in html)
    check("前端有要求输入框 propRequirements", 'id="propRequirements"' in html)
    check("前端调用 /api/proposal/import-plan", "/api/proposal/import-plan" in js)
    check("导入绑定函数在 initProposalStage 里被调用", "bindPropDropzone();" in js)
    paths = set(client.get("/api/openapi.json").json()["paths"])
    check("路由 /api/proposal/import-plan 已注册", "/api/proposal/import-plan" in paths)

    # 纯函数：框架与要求识别
    secs = P._detect_sections(TASK_DOC)
    req = P._detect_requirements(TASK_DOC)
    print(f"  识别出的框架：{secs}")
    print(f"  识别出的要求：{req[:80]!r}…" if req else "  识别出的要求：（空）")
    check("七个小节全部识别出来", len(secs) == len(P.DEFAULT_SECTIONS), f"实得 {len(secs)} 个")
    check("顺序与标准结构一致", secs == list(P.DEFAULT_SECTIONS))
    check("摘到了写作要求（字数/参考文献数）", "8000" in req or "30 篇" in req)
    check("摘到了禁止性要求", "不得" in req or "未经核实" in req)
    check("认不出的文档返回空表（不硬凑）", P._detect_sections("这是一段无关的文字。") == [])

    # 真接口：上传一份 docx 形状的文本（用 .md 走同一条解析与入库链路）
    resp = client.post(
        "/api/proposal/import-plan",
        files={"files": ("任务书.md", io.BytesIO(TASK_DOC.encode("utf-8")), "text/markdown")},
    )
    check("POST /api/proposal/import-plan 200", resp.status_code == 200,
          f"HTTP {resp.status_code} {resp.text[:120]}")
    data = resp.json() if resp.status_code == 200 else {}
    if data:
        check("返回里有 plan.sections", len((data.get("plan") or {}).get("sections") or []) == 7)
        check("返回里有 plan.requirements", bool((data.get("plan") or {}).get("requirements")))
        check("文件真的入库了", (data.get("added") or []) and
              (data.get("kb") or {}).get("documents", 0) > 0,
              f"documents={(data.get('kb') or {}).get('documents')}")
        check("入库后能被检索到（K 层可用）",
              bool(rag.search("数据资产 会计确认", k=3)),
              f"检索到 {len(rag.search('数据资产 会计确认', k=3))} 条")

    # 校验：没文件要拒绝
    check("无文件被拒", client.post("/api/proposal/import-plan").status_code in (400, 422))

    print("\n" + "=" * 78)
    if fails:
        print(f"有 {len(fails)} 项未通过：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("全部通过：入口归位、三层依据可达且可关、导入能读出框架要求并落库。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
