"""把知识库从"只存切块 + PDF 按页拆条"升级到"整篇入库 + 页映射"。

为什么需要这个脚本
------------------
这次改动了存储模型，但**老库里已有的资料不会自动变好**：

1. 早先 ``docs`` 表没有 ``text`` 列，整篇原文要靠"把切块拼回去"重建，
   而切块之间有 80 字重叠，拼回来就把重叠复制了一遍
   （实测一篇 1330 字的资料取回变成 1494 字）。PaperQA2 的文献简报
   吃的就是这份被污染的文本。
2. 早先 PDF 是**每页一篇文档**入库的（标题形如「题名（第 12 页）」），
   于是一篇 63 页的论文在文档库里是 63 条记录。

本脚本做两件幂等的事：补全文、把按页拆开的记录合并回一整篇。
**先备份再改**，所以跑坏了也能退回去。

用法：
    python tools/migrate_kb.py              # 真跑（自动备份）
    python tools/migrate_kb.py --dry-run    # 只看会做什么，不写库
    python tools/migrate_kb.py --no-merge   # 只补全文，不合并按页拆开的记录
"""

from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

ROOT = Path(r"E:\AI\thesis-suite")


def snapshot(label: str) -> Path | None:
    """给知识库做一份带时间戳的备份。**改数据之前先备份**，没有例外。"""
    from suite import rag

    src = Path(rag.KB_PATH)
    if not src.is_file():
        print(f"  知识库不存在：{src}（没什么可备份）")
        return None
    backup_dir = ROOT / "data" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = backup_dir / f"kb-{label}-{stamp}.sqlite3"
    shutil.copy2(src, dst)
    print(f"  备份 → {dst}（{dst.stat().st_size / 1024:.0f} KB）")
    return dst


def describe(tag: str) -> None:
    from suite import rag

    st = rag.stats()
    docs = rag.list_documents()
    print(f"\n[{tag}] 文献 {st.get('documents')} 篇 / 片段 {st.get('chunks')} 条 "
          f"/ {st.get('chars')} 字")
    page_like = [d for d in docs if "（第 " in (d.get("title") or "") and " 页）" in (d.get("title") or "")]
    print(f"  其中「按页拆开」的记录：{len(page_like)} 条")
    for d in docs[:5]:
        print(f"    · {str(d.get('title'))[:52]}（{d.get('chars')} 字 / {d.get('chunks')} 段）")
    if len(docs) > 5:
        print(f"    · …共 {len(docs)} 条")


def main() -> int:
    ap = argparse.ArgumentParser(description="知识库存储模型升级")
    ap.add_argument("--dry-run", action="store_true", help="只看会做什么，不写库")
    ap.add_argument("--no-merge", action="store_true", help="只补全文，不合并按页拆开的记录")
    args = ap.parse_args()

    from suite import rag

    print("=" * 74)
    print("知识库升级：只存切块 → 整篇入库 + 页映射")
    print("=" * 74)
    print(f"库文件：{rag.KB_PATH}")

    describe("升级前")

    if args.dry_run:
        docs = rag.list_documents()
        need_text = 0
        for d in docs:
            full = rag.get_document(d["doc_id"]) or {}
            # 老库里 text 为空时会走"拼回切块"的兜底，所以这里无法直接判断；
            # 用"字符数是否明显大于切块长度之和"来近似没有意义，直接报告计划。
        page_like = [d for d in docs if "（第 " in (d.get("title") or "")]
        print(f"\n[计划] 补全文：{len(docs)} 篇逐一检查（缺 text 的才写）")
        print(f"[计划] 合并按页拆开的记录：{len(page_like)} 条 → 按 origin 分组"
              f"{'（已用 --no-merge 关闭）' if args.no_merge else ''}")
        print("\n这是 dry-run，没有改动任何数据。")
        return 0

    snapshot("before")
    result = rag.migrate(merge_pages=not args.no_merge)
    print(f"\n[升级结果] 补写全文 {result.get('text_filled')} 篇；"
          f"合并 {result.get('merged_groups')} 组，"
          f"回收按页记录 {result.get('page_docs_removed')} 条")
    describe("升级后")
    snapshot("after")

    docs = rag.list_documents()
    left = [d for d in docs if "（第 " in (d.get("title") or "")]
    print(f"\n仍按页拆开的记录：{len(left)} 条（0 表示全部合并完成）")
    print("\n完成。要让 PaperQA2 的索引重新按整篇建，请清一次索引缓存：")
    print("  站点「③ 建立索引」旁点「清空索引缓存」，或 POST /api/pqa/index (DELETE)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
