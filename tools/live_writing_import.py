"""真实链路：Word 文档 → 写作计划 → 初稿 → 单独重写某一章（用真模型）。

``tools/smoke_writing_import.py`` 用假模型验证本站自己的逻辑（解析、材料表、校验、版本）。
这个脚本补上它测不到的那一半：**真实的模型能不能读懂要求，并按导入的正文写作**。

三个问题必须用真模型才能回答：

1. 模型会不会真的从文档里读出题名与章节结构（而不是套一份通用模板）？
2. 按导入文档写出来的正文，会不会引用文档里的数据（D 层依据真的用上了）？
3. 单章重写会不会**只听这一章的要求**，而不动其它章节？

会花钱，所以默认规模很小：一份短文档、2 章、只写第 1 章。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

PASS = "[OK]  "
FAIL = "[FAIL]"

ENV_CANDIDATES = [
    Path(r"E:\AI\thesis-agents\.env"),
    Path(r"E:\AI\thesis-assistant\.env"),
]

#: 文档里刻意放两个具体数字：如果它们出现在生成的正文里，说明 D 层依据真被用上了。
DOC_N = "1836"
DOC_BETA = "-0.34"

DOC_TEXT = f"""乡村教师职业倦怠的影响因素研究
——基于某省 24 个县的调查

摘要
本研究以某省 24 个县的{DOC_N}名乡村教师为样本，采用 Maslach 职业倦怠量表与自编教学投入问卷，
考察组织支持感对职业倦怠的影响。研究发现，标准化回归系数为 {DOC_BETA}。

第1章 绪论
乡村教师队伍建设是当前教育政策的重点之一。既有研究多聚焦城市教师，
对乡村教师的职业倦怠及其影响机制关注不足。本研究试图补上这一缺口。

第2章 文献综述
社会交换理论认为，组织支持感会通过互惠规范影响个体的工作投入。
已有研究普遍报告组织支持感与情感耗竭之间存在负向关联，但结论的稳定性受样本影响。

写作要求
全文控制在 8000 字以内；第 1 章要说清研究缺口与本文的切入点；
不得写入文档里没有的数据。
"""


def pick_key() -> str:
    for path in ENV_CANDIDATES:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("LLM_API_KEY") and "=" in line:
                key = line.split("=", 1)[1].strip().strip('"').strip("'")
                if key:
                    print(f"（密钥来源：{path}，脱敏 {key[:5]}…{key[-4:]}）")
                    return key
    return ""


def make_docx() -> bytes:
    import docx

    document = docx.Document()
    for line in DOC_TEXT.split("\n"):
        document.add_paragraph(line)
    # 顺带放一张表：Word 稿子里的关键数据常在表里，验证表格没被丢掉。
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "维度"
    table.cell(0, 1).text = "均值"
    table.cell(1, 0).text = "情感耗竭"
    table.cell(1, 1).text = "3.42"
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def sse_events(resp) -> list[dict]:
    out: list[dict] = []
    for line in resp.text.split("\n"):
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload in ("", "[DONE]"):
            continue
        try:
            out.append(json.loads(payload))
        except ValueError:
            continue
    return out


def main() -> int:
    key = pick_key()
    if not key:
        print("没有可用的 LLM_API_KEY（E:\\AI\\thesis-assistant\\.env），跳过真模型验证。")
        return 0

    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())
    headers = {"X-LLM-Key": key}          # 模拟浏览器：只发 Key，不发 base/model

    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))
        print(f"  {PASS if ok else FAIL} {name}" + (f"  {detail}" if detail else ""))

    print("\n=== 1. 导入 Word：模型读文档 ===")
    files = [("files", ("初稿.docx", make_docx(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
    resp = client.post("/api/writing/import-plan", files=files, headers=headers)
    print(f"  HTTP {resp.status_code}")
    if resp.status_code != 200:
        print("  " + resp.text[:400])
        return 1
    data = resp.json()
    sid = data["sid"]
    plan = data["plan"]
    print(f"  source={plan['source']} title={plan['title']!r}")
    print(f"  sections={plan['sections']}")
    print(f"  requirements={plan['requirements'][:120]!r}")
    print(f"  material={json.dumps(data['material'], ensure_ascii=False)}")
    for n in data["notes"]:
        print(f"  note: {n[:140]}")

    check("计划由模型读出", plan["source"] == "model", plan["source"])
    check("题名贴合文档", "职业倦怠" in plan["title"], plan["title"])
    check("章节结构来自文档（不是通用模板）",
          any("绪论" in s for s in plan["sections"]) and any("文献综述" in s for s in plan["sections"]),
          str(plan["sections"]))
    check("读出了写作要求", bool(plan["requirements"].strip()), plan["requirements"][:60])
    check("正文被切成依据块", data["material"]["blocks"] >= 1, str(data["material"]))

    print("\n=== 2. 按导入的计划生成初稿（只写第 1 章，省成本）===")
    sections = [s for s in plan["sections"] if "绪论" in s] or plan["sections"][:1]
    resp = client.post("/api/writing/draft", headers=headers,
                       json={"sid": sid, "title": plan["title"], "sections": sections,
                             "requirements": plan["requirements"], "use_web": False})
    print(f"  HTTP {resp.status_code}")
    events = sse_events(resp)
    errors = [e.get("message") for e in events if e.get("type") == "error"]
    result = next((e for e in events if e.get("type") == "result"), None)
    if errors:
        print(f"  错误：{errors[0][:300]}")
    check("初稿生成成功", result is not None and not errors,
          (errors[0][:200] if errors else ""))
    if result:
        draft = result["draft"]
        cov = result["coverage"]
        body = "\n".join(s.get("body", "") for s in draft["sections"])
        print(f"  words={draft['words']} citations={cov.get('citations_total')} "
              f"doc={cov.get('citations_doc')} invalid={cov.get('citations_invalid')} "
              f"ungrounded={cov.get('ungrounded_numbers')}")
        print(f"  正文节选：{body[:220]!r}")
        check("正文里引用了导入文档（D 层）", cov.get("citations_doc", 0) > 0, str(cov))
        check("没有编造引用编号", cov.get("citations_invalid", 0) == 0, str(cov))
        key_number_used = DOC_N in body or DOC_BETA in body or "3.42" in body
        check("引用了文档里的关键数据", key_number_used,
              "" if key_number_used else "正文里既没有样本量也没有回归系数")

        print("\n=== 3. 单独重写某一章 ===")
        before = draft["sections"][0]["body"]
        resp = client.post("/api/writing/section", headers=headers,
                           json={"sid": sid, "index": 0,
                                 "task": "把研究缺口写得更具体：说明既有研究缺少对乡村教师的关注",
                                 "use_web": False})
        print(f"  HTTP {resp.status_code}")
        events = sse_events(resp)
        errors = [e.get("message") for e in events if e.get("type") == "error"]
        sec_result = next((e for e in events if e.get("type") == "result"), None)
        if errors:
            print(f"  错误：{errors[0][:300]}")
        check("单章重写成功", sec_result is not None and not errors,
              (errors[0][:200] if errors else ""))
        if sec_result:
            after = sec_result["draft"]["sections"][0]["body"]
            print(f"  重写后节选：{after[:220]!r}")
            changed = after.strip() != before.strip()
            check("这一章确实被改写了", changed,
                  "" if changed else "重写前后一模一样，说明额外要求没生效")
            bad_cites = sec_result["draft"]["coverage"].get("citations_invalid", 0)
            check("重写后依然没有编造引用", bad_cites == 0,
                  "" if bad_cites == 0 else str(sec_result["draft"]["coverage"]))

    print("\n" + "=" * 70)
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
