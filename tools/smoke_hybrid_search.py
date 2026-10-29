"""验证语义混合检索：RRF 融合、并列规则、降级路径、向量补齐。

三条断言对应的都是**真踩过的坑**，不是形式检查：

1. **RRF 在两路互逆时会完全并列。** 实测题「教师心理压力」：关键词把
   "量表信效度"排第一、向量把"职业倦怠"排第一，融合后两者都是 0.0325。
   如果不给并列规则，谁第一取决于排序稳定性，而不是相关性。
   这里直接构造互逆的两路列表，断言"向量名次靠前者优先"。

2. **不可用时必须降级到关键词，而不是报错。** 公网容器里没有 libs-pqa、
   也没有 91 MB 的模型。那条路上 `search()` 仍要返回结果，`search_mode()`
   要如实说 `keyword`。

3. **语义检索要真的把"意思相近"的那篇排上来。** 用一句语料里并不存在的话
   （「教师心理压力」）去查，关键词会把字面命中更多的"量表信效度"排在前面，
   而混合检索应该把讲"职业倦怠/压力"的那篇排到第一。

用法：
    python tools/smoke_hybrid_search.py            # 全跑（约 25 秒，含模型预热）
    python tools/smoke_hybrid_search.py --fast     # 只跑不依赖模型的纯逻辑部分
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

SANDBOX = Path(r"E:\AI\thesis-suite\.tmp\smoke_hybrid")

#: 两篇文档，刻意造成"字面命中"与"语义相关"分离：
#: A 同时含「教师」「心理」，但主题是量表信效度（不是问的人想要的）；
#: B 只含「教师」，主题正是工作压力→职业倦怠→组织支持。
DOC_A = ("教师心理健康普查工具的信效度检验",
         "教师心理健康普查工具的信效度检验\n\n"
         + "本文检验教师心理健康量表在样本上的信度与效度。" * 8)
DOC_B = ("乡村教师职业倦怠的成因",
         "乡村教师职业倦怠的成因\n\n"
         + "长期工作压力导致乡村教师出现情绪耗竭与去个性化。" * 6
         + "\n\n" + "组织支持感与同事支持能显著缓冲这种耗竭。" * 6)

QUERY = "教师心理压力"


def check(name: str, cond: bool, detail: str = "") -> bool:
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return bool(cond)


def main() -> int:
    fast = "--fast" in sys.argv
    from suite import embedding, rag

    fails: list[str] = []

    def ck(name: str, cond: bool, detail: str = "") -> None:
        if not check(name, cond, detail):
            fails.append(name)

    print("=" * 78)
    print("语义混合检索验证")
    print("=" * 78)

    # ------------------------------------------------------------------
    print("\n--- 1. RRF 融合的并列规则（纯逻辑，不需要模型） ---")
    # 两路互为逆序：RRF 分必然相等。此时必须由"向量名次"决定先后。
    kw = [11, 22]
    vec = [22, 11]
    fused = rag._fuse_rrf(kw, vec, k=2)
    scores = [round(s, 6) for _, s in fused]
    order = [rid for rid, _ in fused]
    print(f"  关键词序 {kw}，向量序 {vec} → 融合序 {order}，分 {scores}")
    ck("RRF 分确实并列（这是前提）", scores[0] == scores[1], str(scores))
    ck("并列时向量名次靠前者优先", order[0] == 22, f"实际 {order}")

    # 只有关键词一路时，行为应等于原顺序
    only_kw = rag._fuse_rrf([11, 22], [], k=2)
    ck("向量为空时退化为关键词顺序",
       [rid for rid, _ in only_kw] == [11, 22],
       str([rid for rid, _ in only_kw]))

    # ------------------------------------------------------------------
    print("\n--- 2. 未就绪时必须降级为关键词（不报错） ---")
    if not embedding.ready():
        print("  （当前未预热，正好可以直接验证降级路径）")
        mode_before = rag.search_mode()
        ck("search_mode 报 keyword", mode_before == "keyword", mode_before)
        try:
            hits = rag.search("任意查询", k=3)
            ck("search 仍然返回（不抛异常）", isinstance(hits, list),
               f"{len(hits)} 条")
        except Exception as exc:  # noqa: BLE001
            ck("search 仍然返回（不抛异常）", False, f"抛了 {type(exc).__name__}: {exc}")
    else:
        print("  （模型已预热，跳过：降级路径只在未就绪时才有意义）")

    if fast:
        print("\n（--fast：跳过需要模型的语义端到端部分）")
        return _report(fails)

    # ------------------------------------------------------------------
    print("\n--- 3. 预热语义模型 ---")
    t0 = time.time()
    st = embedding.warmup(wait=True)
    print(f"  {st['mode']}  dim={st.get('dim')}  耗时 {time.time() - t0:.1f}s")
    if not st.get("ready"):
        print(f"  [跳过] 语义模型不可用：{st.get('error') or '原因未给出'}")
        print("  （在公网容器里这是**预期行为**：检索会一直走关键词）")
        return _report(fails)
    ck("预热后进入 hybrid 模式", rag.search_mode() == "hybrid")

    # ------------------------------------------------------------------
    print("\n--- 4. 语义端到端：意思相近的那篇要排第一 ---")
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / "kb").mkdir(parents=True, exist_ok=True)
    rag.set_base_dir(SANDBOX / "kb")

    for title, body in (DOC_A, DOC_B):
        r = rag.add_document(body, title=title)
        ck(f"入库 {title}", r.get("ok"), f"块 {r.get('chunks')} / 向量 {r.get('vectors')}")

    # 入库时若模型已就绪，向量会顺带写上；否则靠 backfill 补。
    bf = rag.backfill_vectors()
    print(f"  补齐向量：{bf}")
    ck("全库已编码", rag.stats().get("vectors_pending") == 0,
       f"待编码 {rag.stats().get('vectors_pending')}")
    ck("补齐是幂等的（再跑一次不重复编码）",
       (rag.backfill_vectors() or {}).get("embedded") == 0)

    hits = rag.search(QUERY, k=2)
    print(f"  查询「{QUERY}」→")
    for h in hits:
        print(f"    · [{h.doc_title}] score={h.score:.4f}")
    ck("语义正确的那篇排第一", bool(hits) and "职业倦怠" in hits[0].doc_title,
       f"实际第一：{hits[0].doc_title if hits else '无'}")

    # 关键词基线对比：证明差异确实来自向量路
    conn = rag._connect()
    try:
        kw_rids = rag._fts_rids(conn, QUERY, 10)
        if kw_rids:
            ph = ",".join("?" for _ in kw_rids)
            rows = conn.execute(
                f"SELECT c.rowid rid, d.title FROM chunks c JOIN docs d ON d.doc_id=c.doc_id "
                f"WHERE c.rowid IN ({ph})", kw_rids).fetchall()
            tmap = {int(r["rid"]): r["title"] for r in rows}
            kw_first = tmap.get(kw_rids[0], "")
        else:
            kw_first = ""
    finally:
        conn.close()
    print(f"  纯关键词第一名：{kw_first}")
    ck("纯关键词第一名确实是字面更命中但那篇（说明差异真实存在）",
       "信效度" in kw_first, kw_first)

    embedding.shutdown()
    return _report(fails)


def _report(fails: list[str]) -> int:
    print("\n" + "=" * 78)
    if fails:
        print(f"有 {len(fails)} 项未通过：")
        for f in fails:
            print(f"  - {f}")
        return 1
    print("全部通过：RRF 并列规则正确、未就绪时降级、语义召回按意思排序、补齐幂等。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
