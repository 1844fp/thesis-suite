"""验证 RAG 知识库的存储模型：整篇入库、无损存取、页映射、批量导入/删除。

这个脚本盯的是**一类只在"有数据之后"才会暴露的错**：接口都 200，
但库里的东西是错的。所以它不测 HTTP 契约，而测"存进去的东西还在不在、
页码对不对、一篇论文是不是还是一篇论文"。

四条断言各自对应一个真实踩过的坑：

1. **取回 == 原文**。早先只存切块，取回时把块间 80 字重叠又拼了一遍
   （实测 1330 字变 1494 字）。PaperQA2 的文献简报吃的就是这份被污染的文本。
2. **一篇文件 = 一篇文献**。早先 PDF 逐页入库，一篇 63 页的论文变成 63 条记录。
3. **页码仍然精确**。整篇入库后页码要靠"块起始偏移 → 页区间"映射，
   跨页的块要写成「第 7–8 页」，不能只报起始页。
4. **批量删除是幂等的**。删不存在的 id 不算失败，但要如实回报。

用法：
    python tools/smoke_rag_storage.py
"""

from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

SANDBOX = Path(r"E:\AI\thesis-suite\.tmp\smoke_rag_storage")


def main() -> int:
    from suite import rag

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / "kb").mkdir(parents=True, exist_ok=True)
    rag.set_base_dir(SANDBOX / "kb")

    fails: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not cond:
            fails.append(name)

    print("=" * 78)
    print("RAG 知识库存储模型验证")
    print("=" * 78)

    # ------------------------------------------------------------------
    print("\n--- 1. 整篇存取必须无损 ---")
    para = "第{i}段。" + "数据资产的会计确认需要满足三项条件。" * 12
    text = "\n\n".join(para.format(i=i) for i in range(1, 7))
    res = rag.add_document(text, title="无损探测")
    doc = rag.get_document(res["doc_id"])
    print(f"  原文 {len(text)} 字 → 取回 {len(doc['text'])} 字")
    check("取回文本与原文逐字相同", doc["text"] == text,
          f"差 {len(doc['text']) - len(text)} 字")
    probe = "数据资产的会计确认需要满足三项条件"
    check("关键句出现次数不变", doc["text"].count(probe) == text.count(probe),
          f"{text.count(probe)} → {doc['text'].count(probe)}")

    # ------------------------------------------------------------------
    print("\n--- 2. 页映射：整篇入库后页码仍要精确 ---")
    p1 = "第一页内容。" * 50
    p2 = "第二页内容。" * 50
    p3 = "第三页内容。" * 50
    full = p1 + "\n\n" + p2 + "\n\n" + p3
    pages = [
        {"page": 1, "start": 0, "end": len(p1)},
        {"page": 2, "start": len(p1) + 2, "end": len(p1) + 2 + len(p2)},
        {"page": 3, "start": len(full) - len(p3), "end": len(full)},
    ]
    res2 = rag.add_document(full, title="三页文献", pages=pages)
    check("整篇入库成功", res2.get("ok"), f"块 {res2.get('chunks')} / 页 {res2.get('pages')}")
    check("页映射被记录", res2.get("pages") == 3)
    hits = rag.search("第二页内容", k=4)
    print("  命中：" + "；".join(h.citation() for h in hits[:3]))
    check("命中项带页码", all(h.page for h in hits), str([h.page for h in hits]))
    # 至少有一个跨页块要报成区间
    check("跨页块报成区间（第 X–Y 页）",
          any("–" in h.citation() for h in hits),
          str([h.citation() for h in hits[:3]]))
    check("取回全文与原文一致", rag.get_document(res2["doc_id"])["text"] == full)

    # ------------------------------------------------------------------
    print("\n--- 3. 一批上传多个文件 → 一篇文件一篇文献 ---")
    from fastapi.testclient import TestClient
    from suite.api.app import create_app

    client = TestClient(create_app())
    before = rag.stats()["documents"]
    files = [
        ("files", (f"批量-{i:02d}.md",
                   (f"# 批量 {i}\n\n" + "这是用于验证批量导入的测试文本。" * 20).encode("utf-8"),
                   "text/markdown"))
        for i in range(1, 13)
    ]
    r = client.post("/api/kb/upload", files=files)
    d = r.json() if r.status_code == 200 else {}
    print(f"  HTTP {r.status_code}：added {len(d.get('added') or [])} / "
          f"failed {len(d.get('failed') or [])}")
    check("一次 12 个文件全部入库（旧上限是 10）",
          r.status_code == 200 and len(d.get("added") or []) == 12)
    check("12 个文件产生 12 篇文献（不是按页拆成更多）",
          rag.stats()["documents"] - before == 12,
          f"新增 {rag.stats()['documents'] - before} 篇")
    check("每篇都记了 doc_id", all(a.get("doc_id") for a in (d.get("added") or [])))

    # ------------------------------------------------------------------
    print("\n--- 4. 批量删除：事务内删完 + 如实回报 ---")
    ids = [a["doc_id"] for a in (d.get("added") or [])]
    r2 = client.post("/api/kb/delete-batch", json={"doc_ids": ids + ["doc-not-exist"]})
    dd = r2.json() if r2.status_code == 200 else {}
    check("批量删除 HTTP 200", r2.status_code == 200)
    check("删掉 12 篇", len(dd.get("removed") or []) == 12, str(len(dd.get("removed") or [])))
    check("不存在的 id 归入 missing 而不是报错",
          dd.get("missing") == ["doc-not-exist"], str(dd.get("missing")))
    check("删除后文献数回到原值", rag.stats()["documents"] == before,
          f"{rag.stats()['documents']} vs {before}")
    check("空 doc_ids 被拒", client.post("/api/kb/delete-batch", json={"doc_ids": []}).status_code == 400)

    # ------------------------------------------------------------------
    print("\n--- 5. 迁移必须幂等 ---")
    m1 = rag.migrate()
    m2 = rag.migrate()
    print(f"  第一次：补全文 {m1.get('text_filled')}，合并 {m1.get('merged_groups')} 组")
    print(f"  第二次：补全文 {m2.get('text_filled')}，合并 {m2.get('merged_groups')} 组")
    check("第二次不再改动任何东西",
          m2.get("text_filled") == 0 and m2.get("merged_groups") == 0)

    # ------------------------------------------------------------------
    print("\n--- 6. PaperQA2 物化必须用完整文献 ---")
    from suite import paperqa

    docs = rag.list_documents()
    biggest = max(docs, key=lambda x: x["chars"]) if docs else None
    if biggest:
        mat = paperqa.materialize([biggest["doc_id"]], rebuild=True)
        check("物化成功", bool(mat.get("ok")), str(mat.get("error") or ""))
        check("没有被截断", not (mat.get("truncated") or []),
              str(mat.get("truncated") or []))
        full_doc = rag.get_document(biggest["doc_id"])["text"]
        files = sorted(Path(mat["dir"]).rglob("*.txt"))
        check("语料里是一整篇（不是若干片段文件）", len(files) == 1,
              f"{len(files)} 个文件")
        if files:
            body = files[0].read_text(encoding="utf-8")
            # 语料文件前面有一段【文献信息】书目头，去掉它再比长度
            tail = body.split("\n\n", 1)[-1] if body.startswith("【文献信息】") else body
            check("语料正文长度 ≈ 整篇长度",
                  abs(len(tail) - len(full_doc)) <= max(200, len(full_doc) // 100),
                  f"语料 {len(tail)} vs 整篇 {len(full_doc)}")
    else:
        fails.append("知识库里没有文献，无法验证物化")

    print("\n" + "=" * 78)
    if fails:
        print(f"有 {len(fails)} 项未通过：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("全部通过：无损存取、页映射（含跨页区间）、一批多文件、批量删除、迁移幂等、"
          "简报吃整篇。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
