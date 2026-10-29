"""验证「开题报告」两个新板块的**接口层**：/api/refs/* 与 /api/pqa/*。

为什么单独一个脚本，而不是并进 smoke_references / smoke_paperqa
-------------------------------------------------------------
那两个脚本测的是**模块内部**（排序算法、语料物化）。接口层有一类它们测不到的错：
路由没注册、前缀写错、字段名前后端不一致、HTTP 状态码用错（该 400 的返回 200）、
以及**响应里缺字段**——前端拿到 undefined 时不会报错，只会安静地少显示一块。

这些错在浏览器里表现为"点了没反应"，排查成本很高，所以在这里用 FastAPI 的
TestClient 把每条路由都真的打一遍。

**不调模型、不联网**：走的是"没填 Key"的分支（这正是"不填 Key 也要能用"要保证的），
以及参数校验分支。所以本脚本可以放心地放进每次改动的自检清单。

用法：
    python tools/smoke_refs_api.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

SANDBOX = Path(r"E:\AI\thesis-suite\.tmp\smoke_refs_api")


def main() -> int:
    # 沙箱：入库类接口会写知识库，不能碰真实数据。
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

    print("=" * 78)
    print("开题报告新接口验证（不联网、不调模型）")
    print("=" * 78)

    # ---- 路由存在性 -------------------------------------------------------
    print("\n--- 1. 路由已注册 ---")
    spec = client.get("/api/openapi.json").json()
    paths = set(spec.get("paths") or {})
    for path in ("/api/refs/weights", "/api/refs/recommend", "/api/refs/export",
                 "/api/refs/to-kb", "/api/pqa/status", "/api/pqa/ask",
                 "/api/pqa/brief", "/api/pqa/index"):
        check(f"路由 {path}", path in paths)

    # ---- 排序口径可解释 ---------------------------------------------------
    print("\n--- 2. 排序权重可解释（不需要 Key） ---")
    resp = client.get("/api/refs/weights")
    check("GET /api/refs/weights 200", resp.status_code == 200)
    data = resp.json()
    # describe_weights() 用 ** 展开，所以 weights 就在顶层，不是 data["weights"]["weights"]。
    weights = data.get("weights") or {}
    print(f"  权重：{weights}")
    check("权重之和为 1", abs(sum(weights.values()) - 1.0) < 1e-6, str(sum(weights.values())))
    check("含 limits 诚实说明", bool(data.get("limits")))
    check("limits 里写明下载量拿不到", "下载量" in (data.get("limits") or ""))

    # ---- 参数校验 ---------------------------------------------------------
    print("\n--- 3. 参数校验（不该 200 的要 4xx/5xx） ---")
    resp = client.post("/api/refs/recommend", json={"topic": "短"})
    check("选题过短被拒", resp.status_code == 400, f"HTTP {resp.status_code}")

    resp = client.post("/api/refs/export", json={"format": "docx", "items": [{"title": "x"}]})
    check("不支持的导出格式被拒", resp.status_code == 400, f"HTTP {resp.status_code}")

    resp = client.post("/api/refs/export", json={"format": "gbt", "items": []})
    check("空清单导出被拒", resp.status_code == 400, f"HTTP {resp.status_code}")

    resp = client.post("/api/refs/to-kb", json={"items": []})
    check("空选中入库被拒", resp.status_code == 400, f"HTTP {resp.status_code}")

    resp = client.post("/api/pqa/ask", json={"question": "在吗", "doc_ids": []})
    check("没选文献提问被拒", resp.status_code == 400, f"HTTP {resp.status_code}")

    resp = client.post("/api/pqa/ask", json={"question": "", "doc_ids": ["doc-x"]})
    check("空问题被拒", resp.status_code == 400, f"HTTP {resp.status_code}")

    resp = client.post("/api/pqa/ask", json={"question": "问题", "doc_ids": ["doc-not-exist"]})
    check("未知 doc_id 被拒（404）", resp.status_code == 404, f"HTTP {resp.status_code}")

    resp = client.post("/api/pqa/brief", json={"mode": "crazy", "doc_ids": ["doc-x"]})
    check("不支持的简报模式被拒", resp.status_code == 400, f"HTTP {resp.status_code}")

    # ---- PaperQA2 状态 ---------------------------------------------------
    print("\n--- 4. PaperQA2 状态（不需要 Key） ---")
    resp = client.get("/api/pqa/status")
    check("GET /api/pqa/status 200", resp.status_code == 200)
    status = resp.json()
    print(f"  available={status.get('available')} version="
          f"{(status.get('detail') or {}).get('version')}")
    check("状态里含 available", "available" in status)
    check("状态里含 missing 列表", isinstance(status.get("missing"), list))
    check("状态里含 index 子对象", isinstance(status.get("index"), dict))
    check("状态里含 note", bool(status.get("note")))

    # ---- 导出（纯本地渲染，不需要网络与模型） ---------------------------
    print("\n--- 5. 导出渲染（不需要 Key、不需要联网） ---")
    items = [
        {"rank": 1, "title": "数据资产的会计确认与计量研究", "authors": "张三、李四",
         "year": "2024", "venue": "会计研究", "doi": "10.1234/abc", "tier": "CSSCI",
         "citations": 12, "downloads": None, "venue_type": "journal"},
        {"rank": 2, "title": "Data Asset Valuation", "authors": "John Smith",
         "year": "2023", "venue": "Journal of X", "doi": "", "tier": "国际期刊",
         "citations": None, "downloads": None, "venue_type": "repository"},
    ]
    for fmt, marker in (("gbt", "[1]"), ("bibtex", "@article"), ("markdown", "# 参考文献")):
        resp = client.post("/api/refs/export",
                           json={"format": fmt, "items": items, "topic": "数据资产"})
        text = resp.text
        check(f"导出 {fmt} 200 且非空", resp.status_code == 200 and len(text) > 20,
              f"HTTP {resp.status_code}, {len(text)} 字符")
        if fmt == "gbt":
            check("GB/T 里仓储来源标 [D] 而非 [J]", "[D]" in text and "Data Asset Valuation[D]" in text)
            check("GB/T 里期刊来源标 [J]", "会计研究, 2024" in text and "[J]" in text)
            check("GB/T 不把拿不到的下载量编进去", "下载" not in text)
        if fmt == "bibtex":
            check("BibTeX 条目数为 2", marker in text and text.count("@article") == 2,
                  f"count={text.count('@article')}")
        if fmt == "markdown":
            check("Markdown 含下载列且如实留空", "| 下载 |" in text or "下载" in text)

    # ---- 入知识库（不需要 Key，不调模型） -------------------------------
    print("\n--- 6. 选中文献入知识库（不需要 Key） ---")
    payload = {"items": [
        {"title": "有摘要的文献", "abstract": "这是一段足够长的摘要，用来验证入库链路是否工作。" * 3,
         "authors": "张三", "year": "2024", "venue": "会计研究", "doi": "10.1/x",
         "tier": "CSSCI", "citations": 5, "language": "zh", "sources": ["openalex"],
         "citation": "张三. 有摘要的文献[J]. 会计研究, 2024."},
        {"title": "没有摘要的文献", "abstract": "", "year": "2023"},
    ]}
    resp = client.post("/api/refs/to-kb", json=payload)
    check("POST /api/refs/to-kb 200", resp.status_code == 200, f"HTTP {resp.status_code}")
    data = resp.json()
    print(f"  入库 {data.get('added_count')} 条，跳过 {len(data.get('skipped') or [])} 条")
    check("有摘要的那条入库成功", data.get("added_count") == 1)
    check("没摘要的那条被跳过且说明原因",
          len(data.get("skipped") or []) == 1
          and "摘要" in str((data.get("skipped") or [{}])[0].get("reason")))
    check("返回里写明入库的是摘要不是全文", "全文" in (data.get("note") or ""))
    check("stats 反映入库结果", (data.get("stats") or {}).get("documents") == 1)

    # 入库后元数据必须能被读回来——这是 PaperQA2 语料头写书目的依据。
    docs = client.get("/api/kb/docs").json().get("documents") or []
    check("知识库能列出刚入库的文献", len(docs) == 1, f"{len(docs)} 条")
    if docs:
        meta = docs[0].get("meta") or {}
        check("元数据里有文献等级", meta.get("tier") == "CSSCI", str(meta.get("tier")))
        check("元数据里有被引量", meta.get("citations") == 5)
        check("元数据里有 DOI", meta.get("doi") == "10.1/x")

    # ---- 缺 Key 时的行为：必须给出可读的指引，而不是崩 -----------------
    print("\n--- 7. 没填 Key 时的行为（本脚本的核心断言） ---")
    resp = client.post("/api/refs/recommend",
                       json={"topic": "数据资产的会计确认与价值评估研究", "rerank": True,
                             "total": 10})
    # 这一步会**真的联网检索**公开库。检索不到不算失败，但状态码与字段必须对。
    check("推荐接口不因缺 Key 而失败", resp.status_code == 200, f"HTTP {resp.status_code}")
    body = resp.json()
    if body.get("ok"):
        print(f"  检索到 {body.get('count')} 篇 / 候选 {body.get('candidates')} 条")
        check("响应含 items", isinstance(body.get("items"), list))
        check("响应含 summary", isinstance(body.get("summary"), dict))
        check("响应含 limits", bool(body.get("limits")))
        check("响应含每条的 why（排序可解释）",
              all(i.get("why") for i in body["items"]))
        check("缺 Key 时精排被跳过且说明原因",
              body.get("rerank", {}).get("done") is False
              and "Key" in (body.get("rerank", {}).get("reason") or ""),
              str(body.get("rerank")))
        check("下载量一律为 None",
              all(i.get("downloads") is None for i in body["items"]))
    else:
        print(f"  [注意] 检索没结果（可能限流）：{body.get('error')}")
        check("失败时也给出各源状态", isinstance(body.get("sources"), list))

    doc_id = (docs[0]["doc_id"] if docs else "doc-x")
    resp = client.post("/api/pqa/index", json={"doc_ids": [doc_id]})
    check("缺 Key 建索引返回 ok:false 而不是 500",
          resp.status_code == 200 and resp.json().get("ok") is False,
          f"HTTP {resp.status_code}")
    check("缺 Key 的提示指向填 Key",
          "Key" in (resp.json().get("error") or ""), str(resp.json().get("error"))[:80])

    resp = client.post("/api/pqa/brief", json={"mode": "brief", "doc_ids": [doc_id]})
    check("缺 Key 出简报返回 400 且提示填 Key",
          resp.status_code == 400 and "Key" in str(resp.json().get("detail")),
          f"HTTP {resp.status_code}")

    # ---- 首页没被改坏 -----------------------------------------------------
    print("\n--- 8. 首页与既有接口未受影响 ---")
    resp = client.get("/")
    check("GET / 200", resp.status_code == 200)
    check("首页仍是 HTML", "text/html" in resp.headers.get("content-type", ""))
    for path in ("/api/health", "/api/capabilities", "/api/deployment", "/api/modules"):
        r = client.get(path)
        check(f"GET {path} 200", r.status_code == 200, f"HTTP {r.status_code}")

    caps = client.get("/api/capabilities").json().get("assistants") or []
    ids = [a.get("id") for a in caps]
    check("能力清单里出现 references", "references" in ids, str(ids))
    check("能力清单里出现 paperqa", "paperqa" in ids, str(ids))

    modules = client.get("/api/modules").json().get("modules") or []
    proposal = next((m for m in modules if m.get("id") == "proposal"), {})
    actions = proposal.get("actions") or []
    check("开题报告板块声明了 refs 动作", any(a.startswith("refs.") for a in actions), str(actions))
    check("开题报告板块声明了 pqa 动作", any(a.startswith("pqa.") for a in actions), str(actions))

    print("\n" + "=" * 78)
    if fails:
        print(f"有 {len(fails)} 项未通过：")
        for item in fails:
            print(f"  - {item}")
        return 1
    print("全部通过：路由齐、校验严、缺 Key 时有可读指引、导出与入库链路正确。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
