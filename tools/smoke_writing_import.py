"""验证本次新增的两项能力（都不需要真实 API Key）。

1. **导入 Word/PDF 直接出初稿**：``/api/writing/import-plan`` 解析文档 → 可编辑的写作计划
   （题目 / 章节结构 / 写作要求）→ 正文切成依据块 → ``/api/writing/draft`` 直接按它写。
2. **单独生成某一部分章节**：``/api/writing/section`` 只重写被点名的那一章。

怎么在没有 Key 的情况下端到端验证
----------------------------------
关键手法：把 ``suite.api.writing.llm_mod.Chat`` **换成一个假客户端**。
接口层（解析、材料表、引用编号校验、数字接地复核、版本推进）全部是真代码，
只有"模型返回什么"是假的。这样验证的是本站自己的逻辑，而不是模型的心情。

``tools/live_writing_import.py`` 用真 Key 跑同一组断言（默认不跑，见那里的说明）。
"""

from __future__ import annotations

import io
import json
import re
import sys

sys.path.insert(0, r"E:\AI\thesis-suite")

PASS = "[OK]  "
FAIL = "[FAIL]"

#: 文档正文里出现的关键数字。假模型只会写能在材料表里找到的数——
#: 让"数字接地"这项检查有一条**真能通过**的路径，否则我们只是在验证它会报错。
DOC_SAMPLE = "1836"
DOC_COEF = "-0.34"
#: 只出现在参考文献段里的数字。它**不应该**进材料表，
#: 所以假模型如果写了它，接地检查必须报出来——这正是"参考文献不算证据"的验收点。
REF_ONLY_NUMBER = "99.99"

#: 填充正文。为什么要把文档造得足够长：材料表对**每一条材料**有 1400 字的写入上限，
#: 文档太短就永远碰不到那个边界，于是"截断会不会把结论里的数字切掉"这类问题测不出来。
#: 这里刻意让数字分处两端：样本量在开头，回归系数在结尾。
_FILLER = "乡村教师队伍建设是当前教育政策的重点，既有研究多聚焦城市教师，对乡村教师的关注相对不足。"


def _filler(n: int) -> str:
    return "\n\n".join(f"{_FILLER}（第 {i + 1} 段补充说明。）" for i in range(n))


DOC_TEXT = f"""乡村教师职业倦怠的影响因素研究

摘要
本研究以某省 24 个县的{DOC_SAMPLE}名乡村教师为样本，考察组织支持感与职业倦怠的关系。

关键词：乡村教师；职业倦怠；组织支持感

第1章 绪论
乡村教师队伍建设是当前教育政策的重点之一。既有研究多聚焦城市教师，对乡村教师的关注不足。
{_filler(14)}

第2章 文献综述
社会交换理论认为，组织支持感会通过互惠规范影响个体的工作投入。
{_filler(12)}
标准化回归系数为 {DOC_COEF}，说明组织支持感与情感耗竭之间存在稳定的负向关联。

第3章 研究设计
采用分层随机抽样，覆盖 24 个县。问卷包含情感耗竭、低成就感两个维度。
{_filler(12)}

第4章 结果与分析
组织支持感对情感耗竭的解释力最强。
{_filler(10)}

第5章 结论与展望
应通过提升组织支持感来缓解乡村教师的职业倦怠。
{_filler(8)}

写作要求
全文不少于 15000 字；第 3 章必须说明抽样与量表来源；不得出现没有出处的百分比。

参考文献
[1] 张三. 乡村教师职业倦怠研究[J]. 教育研究, 2020, 12(3): 45-56. DOI: 10.1000/xyz{REF_ONLY_NUMBER}
[2] 李四. 组织支持感述评[J]. 心理科学, 2021, 44(2): 12-20.
"""


def make_docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_pdf(text: str) -> bytes:
    """造一个两页、带文字层的 PDF。只放短行——insert_text 不会自动换行。"""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    y = 80
    for line in text.split("\n"):
        page.insert_text((60, y), line[:60], fontsize=10)
        y += 16
        if y > 760:
            page = doc.new_page()
            y = 80
    data = doc.tobytes()
    doc.close()
    return data


class FakeChat:
    """假模型：只做两件事——回一份计划 JSON、按材料表写章节正文。

    它刻意**只引用材料表里真实存在的编号**，并**只写材料表里有的数字**：
    这样"引用越界"与"数字找不到依据"两项检查都应当通过；一旦本站的材料表构造错了，
    这两项检查就会立刻报出来，测试也就抓到了问题。
    """

    last_section_prompts: list[str] = []
    last_plan_prompt = ""

    def __init__(self, creds, **kwargs) -> None:  # noqa: ANN001
        self.creds = creds

    def __enter__(self) -> "FakeChat":
        return self

    def __exit__(self, *exc) -> bool:  # noqa: ANN002
        return False

    @staticmethod
    def _material_table(user: str) -> str:
        m = re.search(r"可用材料：\n(.*?)\n\n", user, re.S)
        return m.group(1) if m else ""

    def ask_json(self, messages, **kwargs) -> dict:  # noqa: ANN001
        FakeChat.last_plan_prompt = "\n".join(str(m.get("content")) for m in messages)
        return {
            "title": "乡村教师职业倦怠的影响因素研究",
            "genre": "",
            "sections": ["第1章 绪论", "第2章 文献综述", "第3章 研究设计",
                         "第4章 结果与分析", "第5章 结论与展望"],
            "keywords": ["乡村教师", "职业倦怠", "组织支持感"],
            "requirements": "全文不少于 15000 字；第 3 章必须说明抽样与量表来源；不得出现没有出处的百分比。",
            "notes": [],
        }

    def ask(self, messages, **kwargs) -> str:  # noqa: ANN001
        user = str(messages[-1].get("content") or "")
        FakeChat.last_section_prompts.append(user)
        material = self._material_table(user)
        nums = re.findall(r"\[(\d+)\]（", material)
        # 分别挑一个 D 层编号与一个 B 层编号：只挑第一个的话，
        # "文档与联网文献都被用上"这件事就测不出来了。
        doc_nums = re.findall(r"\[(\d+)\]（D ", material)
        web_nums = re.findall(r"\[(\d+)\]（B ", material)
        cite = f"[{doc_nums[0]}]" if doc_nums else (f"[{nums[0]}]" if nums else "")
        cite2 = f"[{web_nums[0]}]" if web_nums else ""
        heading = re.search(r"本次要写的章节：(.*)", user)
        name = (heading.group(1).strip() if heading else "本章")
        # 带"额外要求"时把要求写进正文：真实模型一定会按新要求改动内容，
        # 假模型若每次都返回同一段文字，就测不出"重写确实动了这一章"。
        task = re.search(r"本次的额外要求[^：]*：\s*(.*)", user)
        task_line = f"（按作者要求补充：{task.group(1).strip()}）" if task else ""
        # 正文里带上文档里的两个数字：它们能在材料表里找到，接地检查应当通过。
        return (
            f"{name}的正文。既有研究对乡村教师的关注不足，本研究以{DOC_SAMPLE}名教师为样本，"
            f"标准化回归系数为 {DOC_COEF}{cite}。社会交换理论提供了基本解释框架{cite2}。"
            f"需要说明的是，本章的证据主要来自作者提供的文档与检索到的公开文献。{task_line}"
        )


def main() -> int:
    from suite import boot

    boot.prepare()

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  {PASS if ok else FAIL} {name}" + (f"  {detail}" if detail else ""))

    print("=" * 74)
    print("第一层：解析层（不需要模型，也不需要 Key）")
    print("=" * 74)

    from suite import fileintake
    from suite.api import writing as writing_api

    r = fileintake.extract("初稿.docx", make_docx(DOC_TEXT))
    check("docx 解析成功", r.ok and DOC_SAMPLE in r.text, r.error or f"chars={len(r.text)}")

    blocks, dropped = writing_api._split_document(DOC_TEXT)
    joined = "\n".join(b["text"] for b in blocks)
    check("正文被切成依据块", len(blocks) >= 3, f"blocks={len(blocks)}")
    check("参考文献段被剔除", REF_ONLY_NUMBER not in joined and dropped > 0,
          f"dropped={dropped}")
    check("写作要求段保留在依据里", "15000" in joined)
    check("关键数字完整未被打断", DOC_COEF in joined and DOC_SAMPLE in joined)

    headings = writing_api._headings_from_text(DOC_TEXT)
    check("能从文档抽出章节结构", len(headings) >= 5, "、".join(headings))
    check("章节标题形态正确", headings[0].startswith("第1章"), headings[0] if headings else "")
    check("题名从文档首行取出",
          writing_api._title_from_text(DOC_TEXT, "x") == "乡村教师职业倦怠的影响因素研究")

    print("\n" + "=" * 74)
    print("第二层：接口层（模型换成假客户端，其余全是真代码）")
    print("=" * 74)

    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    original_chat = writing_api.llm_mod.Chat
    writing_api.llm_mod.Chat = FakeChat          # 只替换模型，接口逻辑保持原样
    try:
        client = TestClient(create_app())

        # ---- 导入：没有 Key 时也要能用（回退成程序推导的计划）----
        files = [("files", ("初稿.docx", make_docx(DOC_TEXT),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
        resp = client.post("/api/writing/import-plan", files=files)
        print(f"\n  [无 Key] HTTP {resp.status_code}")
        check("导入接口返回 200", resp.status_code == 200, resp.text[:160])
        data = resp.json()
        sid = data["sid"]
        plan = data["plan"]
        material = data["material"]
        print(f"  plan.source={plan['source']} title={plan['title']!r} "
              f"sections={len(plan['sections'])} blocks={material['blocks']}")
        check("无 Key 时计划降级为程序推导", plan["source"] == "local", plan["source"])
        check("计划仍有题名与结构", bool(plan["title"]) and len(plan["sections"]) >= 4)
        check("计划落在服务端会话里（draft 能直接用它）", material["blocks"] >= 3,
              json.dumps(material, ensure_ascii=False))

        # ---- 导入：带 Key（假模型）时由模型读文档 ----
        fake_headers = {"X-LLM-Key": "sk-fake-for-test", "X-LLM-Base": "http://127.0.0.1:1/v1"}
        resp = client.post("/api/writing/import-plan", files=files, headers=fake_headers)
        print(f"\n  [假模型] HTTP {resp.status_code}")
        check("带 Key 时导入返回 200", resp.status_code == 200, resp.text[:160])
        data = resp.json()
        sid = data["sid"]
        plan = data["plan"]
        check("计划由模型读出", plan["source"] == "model", plan["source"])
        check("章节结构来自模型", len(plan["sections"]) == 5, str(plan["sections"]))
        check("写作要求被读出来", "15000" in plan["requirements"], plan["requirements"][:60])
        # 读计划时给模型看的是**整篇文档**（含参考文献）：要判断"这是论文稿还是任务书"、
        # 有没有指定结构，看全貌比看节选准。真正的约束在下一条：
        # 参考文献不得进材料表，正文引用只能落在有原文的块上。
        check("读计划时给模型的是整篇文档", DOC_SAMPLE in FakeChat.last_plan_prompt)
        check("读计划时喂给模型的正文含关键数字", DOC_COEF in FakeChat.last_plan_prompt)

        # ---- 直接生成初稿：应当自动带上导入文档作为依据 ----
        resp = client.post("/api/writing/draft", headers=fake_headers,
                           json={"sid": sid, "title": plan["title"],
                                 "sections": plan["sections"],
                                 "requirements": plan["requirements"], "use_web": False})
        print(f"\n  [初稿] HTTP {resp.status_code}")
        check("生成初稿返回 200", resp.status_code == 200, resp.text[:160])
        draft = None
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload in ("", "[DONE]"):
                    continue
                try:
                    event = json.loads(payload)
                except ValueError:
                    continue
                if event.get("type") == "result":
                    draft = event.get("draft")
        check("收到 result 事件（含稿件）", bool(draft))
        if draft:
            cov = draft["coverage"]
            print(f"  words={draft['words']} sections={len(draft['sections'])}")
            print(f"  coverage={json.dumps(cov, ensure_ascii=False)}")
            check("初稿按导入的章节数生成", len(draft["sections"]) == 5,
                  "" if len(draft["sections"]) == 5 else f"{len(draft['sections'])} 章")
            check("正文引用的是导入文档（D 层）", cov.get("citations_doc", 0) > 0, str(cov))
            check("引用编号没有越界", cov.get("citations_invalid", 0) == 0, str(cov))
            check("数字都能在依据里找到", cov.get("ungrounded_numbers", 0) == 0, str(cov))
            check("覆盖度如实说明依据只来自导入文档",
                  "导入的文档" in str(cov.get("verdict")), str(cov.get("verdict")))
            check("材料表确实是四层结构",
                  set(draft.get("scope", {}).get("tiers") or [])
                  == {"D 导入文档", "A 用户证据", "B 联网检索", "C 模型知识"},
                  str(draft.get("scope")))
            check("提示词里带了作者给的写作要求",
                  any("15000" in p for p in FakeChat.last_section_prompts))
            check("提示词里的材料表含导入文档原文",
                  any(DOC_COEF in p for p in FakeChat.last_section_prompts))
            # 这一条是"参考文献不算证据"的验收点：题录进了材料表，
            # "引用编号在池内"就会退化成"编号存在"，而那正是会自我欺骗的校验。
            check("参考文献题录没有进材料表（写作提示词里查不到）",
                  not any(REF_ONLY_NUMBER in p for p in FakeChat.last_section_prompts))

        # ---- 单独重写某一章 ----
        before = json.loads(json.dumps(draft))
        resp = client.post("/api/writing/section", headers=fake_headers,
                           json={"sid": sid, "index": 2, "task": "把抽样方法写清楚",
                                 "use_web": False})
        print(f"\n  [单章重写] HTTP {resp.status_code}")
        check("单章重写返回 200", resp.status_code == 200, resp.text[:160])
        event = None
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload in ("", "[DONE]"):
                    continue
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    continue
                if parsed.get("type") == "result":
                    event = parsed
        check("收到 result 事件", bool(event))
        if event and before:
            after = event["draft"]
            same_target = after["sections"][2]["body"] == before["sections"][2]["body"]
            check("只重写了被点名的那一章", not same_target,
                  "" if not same_target else "第 3 章正文重写前后一模一样")
            others_kept = (after["sections"][0]["body"] == before["sections"][0]["body"]
                           and after["sections"][4]["body"] == before["sections"][4]["body"])
            check("其余章节原样保留", others_kept, "" if others_kept else "别的章节被改动了")
            check("重写的那一章被标记", after["sections"][2].get("regenerated") is True)
            check("额外要求进了提示词",
                  any("把抽样方法写清楚" in p for p in FakeChat.last_section_prompts))
            check("重写产生新版本（旧版可回退）", event.get("version_count") == 2,
                  f"version_count={event.get('version_count')}")
            check("重写后引用依然没有越界",
                  after["coverage"].get("citations_invalid", 0) == 0,
                  str(after["coverage"]))

        # ---- 边界：会话不存在 / 章节不存在 / 没有稿件 ----
        resp = client.post("/api/writing/section", headers=fake_headers,
                           json={"sid": "does-not-exist", "index": 0})
        check("会话过期时返回 404", resp.status_code == 404, f"HTTP {resp.status_code}")
        resp = client.post("/api/writing/section", headers=fake_headers,
                           json={"sid": sid, "heading": "第99章 不存在的章节"})
        check("章节不存在时返回 400 并列出可用章节",
              resp.status_code == 400 and "第1章" in resp.text, resp.text[:120])

        # ---- 边界：全部文件都解析不出来 -> 非 200 ----
        resp = client.post("/api/writing/import-plan",
                           files=[("files", ("坏.xlsx", b"PK\x03\x04x", "application/zip"))])
        check("全部文件解析失败返回非 200", resp.status_code == 422, f"HTTP {resp.status_code}")
        resp = client.post("/api/writing/import-plan")
        check("没有文件时返回 400", resp.status_code == 400, f"HTTP {resp.status_code}")

        # ---- PDF 也要能走同一条路（按页切块，带页码）----
        resp = client.post("/api/writing/import-plan",
                           files=[("files", ("初稿.pdf", make_pdf(DOC_TEXT), "application/pdf"))])
        check("PDF 导入返回 200", resp.status_code == 200, resp.text[:160])
        if resp.status_code == 200:
            pdf_data = resp.json()
            print(f"  pdf blocks={pdf_data['material']['blocks']} "
                  f"chars={pdf_data['material']['chars']}")
            check("PDF 正文被切成依据块", pdf_data["material"]["blocks"] >= 1)

        # ---- agent 通用接口上的两个新动作 ----
        agents = client.get("/api/agents").json()["agents"]
        writer = next((a for a in agents if a["name"] == "writer"), None)
        names = {a["name"] for a in (writer or {}).get("actions", [])}
        check("writer agent 注册了 import_plan 与 section",
              {"import_plan", "section"} <= names, str(sorted(names)))
        resp = client.get("/api/agents/writer/actions")
        check("动作清单接口可读", resp.status_code == 200 and "import_plan" in resp.text)

        # ---- "用了材料却不挂编号"必须被查出来 ----
        # 这条是实测真模型踩过的坑：它把作者上传的文档当成"作者自己的话"，
        # 数字照抄、一个 [n] 都不挂。那时 citations_* 全是 0，
        # 覆盖度会得出"没有任何引用依据"的**反向结论**（作者以为整章都是模型编的）。
        class FakeChatNoCite(FakeChat):
            def ask(self, messages, **kwargs) -> str:  # noqa: ANN001
                user = str(messages[-1].get("content") or "")
                return (f"本章以{DOC_SAMPLE}名教师为样本，标准化回归系数为 {DOC_COEF}，"
                        "讨论乡村教师的职业倦怠状况。")

        writing_api.llm_mod.Chat = FakeChatNoCite
        files = [("files", ("初稿.docx", make_docx(DOC_TEXT),
                            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
        sid_nc = client.post("/api/writing/import-plan", files=files,
                             headers=fake_headers).json()["sid"]
        resp = client.post("/api/writing/draft", headers=fake_headers,
                           json={"sid": sid_nc, "title": "乡村教师职业倦怠的影响因素研究",
                                 "sections": ["第1章 绪论"], "use_web": False})
        nc_result = None
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload in ("", "[DONE]"):
                    continue
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    continue
                if parsed.get("type") == "result":
                    nc_result = parsed
        print("\n  [用了材料但没挂编号]")
        check("识别出'用了材料却没挂编号'",
              bool(nc_result) and nc_result["coverage"].get("uncited_evidence_sections") == 1,
              str((nc_result or {}).get("coverage")))
        if nc_result:
            verdict = str(nc_result["coverage"].get("verdict"))
            check("覆盖度结论没有说反（指出了'有依据但没标注'，而不是'没有依据'）",
                  "内容有依据、但读者无法回查" in verdict and verdict.startswith("正文用到了材料里的数据"),
                  verdict)
            check("这一章被标记为 uncited_evidence",
                  (nc_result["draft"]["sections"][0].get("uncited_evidence") or {})
                  .get("suspected_uncited") is True)
        writing_api.llm_mod.Chat = FakeChat

        # ---- 额度分配：导入文档不能把联网文献挤出去 ----
        # 这是实测发现的真问题：材料表总共只放 14 条，若按"先到先得"分配，
        # 8 条文档块会把 B 层（唯一带 DOI、可回查的那一层）挤到一条都进不来。
        # 这里把联网检索换成桩，验证 D 与 B 能同时出现在材料表里。
        fake_records = [
            writing_api.websearch.WebRecord(
                source="crossref", title="Organizational support and teacher burnout",
                abstract="A meta-analysis of 42 studies on organizational support and burnout.",
                authors="Smith J", year="2021", doi="10.1000/fake.2021.1",
                url="https://example.org/fake", venue="Journal of Fake Studies", citations=42,
            )
        ]

        def fake_search(queries, **_kwargs):  # noqa: ANN001
            return fake_records, [{"source": "crossref", "ok": True, "count": 1}]

        original_search = writing_api.websearch.search_literature
        writing_api.websearch.search_literature = fake_search
        try:
            files = [("files", ("初稿.docx", make_docx(DOC_TEXT),
                                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
            sid_mix = client.post("/api/writing/import-plan", files=files,
                                  headers=fake_headers).json()["sid"]
            FakeChat.last_section_prompts = []
            resp = client.post("/api/writing/draft", headers=fake_headers,
                               json={"sid": sid_mix, "title": "乡村教师职业倦怠的影响因素研究",
                                     "sections": ["第1章 绪论"], "use_web": True})
            mix_ok = any(
                "D 作者文档" in p and "B 联网检索" in p for p in FakeChat.last_section_prompts
            )
            mix_cov = {}
            for line in resp.text.split("\n"):
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload in ("", "[DONE]"):
                        continue
                    try:
                        parsed = json.loads(payload)
                    except ValueError:
                        continue
                    if parsed.get("type") == "result":
                        mix_cov = parsed["coverage"]
            print(f"\n  [D+B 额度分配] coverage="
                  f"{json.dumps({k: mix_cov.get(k) for k in ('citations_doc', 'citations_web')}, ensure_ascii=False)}")
            check("导入文档没有把联网文献挤出材料表", mix_ok,
                  "材料表里没有同时出现 D 层与 B 层")
            check("覆盖度同时统计到导入文档与联网文献",
                  mix_cov.get("citations_doc", 0) > 0 and mix_cov.get("citations_web", 0) > 0,
                  str(mix_cov))
        finally:
            writing_api.websearch.search_literature = original_search

        # ---- 长文档：超出预算时必须**如实说明**用了多少、丢了多少 ----
        long_paras = []
        for i in range(40):
            long_paras.append(f"第{i + 1}节 主题{i + 1}")
            long_paras.append("正文内容。" * 400)   # 每节约 2000 字符
        long_text = "长文档测试\n\n" + "\n\n".join(long_paras)
        resp = client.post("/api/writing/import-plan",
                           files=[("files", ("长稿.txt", long_text.encode("utf-8"), "text/plain"))])
        print(f"\n  [长文档] HTTP {resp.status_code} text={len(long_text)} 字")
        check("长文档导入返回 200", resp.status_code == 200, resp.text[:160])
        if resp.status_code == 200:
            lm = resp.json()["material"]
            print(f"  blocks={lm['blocks']}/{lm['total_blocks']} chars={lm['chars']} "
                  f"truncated={lm['truncated']}")
            check("超出预算的部分被截断", lm["truncated"] is True, json.dumps(lm, ensure_ascii=False))
            check("截断后仍保留可用依据", lm["blocks"] >= 8, str(lm))
            check("依据字数不超过预算",
                  lm["chars"] <= writing_api.DOC_CHAR_BUDGET, str(lm))
            check("截断在 notes 里如实说明",
                  any("只把前" in n for n in resp.json()["notes"]),
                  "；".join(resp.json()["notes"])[:120])
    finally:
        writing_api.llm_mod.Chat = original_chat

    print("\n" + "=" * 74)
    failed = [c for c in checks if not c[1]]
    if failed:
        print(f"失败 {len(failed)} 项：")
        for name, _, detail in failed:
            print(f"  - {name}  {detail}")
        return 1
    print(f"全部通过（{len(checks)} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
