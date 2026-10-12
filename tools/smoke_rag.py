"""验证本地 RAG 知识库。

最要紧的一条：**中文能不能检索到**。FTS5 默认分词器按空白切，
一整句中文会变成一个 token，任何查询都匹配不上——如果这一点不成立，
整个"关键词检索 + 模型重排"的方案就是空的。所以这个测试必须先证明它。

用独立的临时库文件跑，不污染真实知识库。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    ok = ok and cond


#: 一份两段主题不同的中文资料，用来验证"能命中对的、不命中错的"
DOC_A = """乡村教师职业倦怠已成为影响教学质量的关键因素。本研究以某省24个县1836名乡村教师为样本，
采用Maslach职业倦怠量表与自编教学投入问卷，考察职业倦怠对教学投入的影响及组织支持的调节作用。
研究发现：乡村教师职业倦怠总体处于中等偏上水平，其中情感耗竭维度得分最高；
职业倦怠对教学投入具有显著负向影响，标准化回归系数为-0.34。

组织支持在职业倦怠与教学投入的关系中起显著调节作用，高组织支持组别的负向效应明显减弱。
研究建议从减轻非教学负担、完善职称晋升通道与加强心理支持三个方面改善乡村教师职业状态。"""

DOC_B = """深度学习模型的注意力机制最早由Transformer架构提出。其核心是用自注意力计算序列内
任意两个位置之间的关联权重，从而摆脱循环网络对时序的依赖。多头注意力让模型在不同子空间
并行学习不同的关联模式，显著提升了长距离依赖的建模能力。后续的BERT与GPT系列均建立在这一
架构之上，并通过大规模预训练获得了通用语言表示能力。"""


def main() -> int:
    # 声明 global：下面 cleanup 里也 import 了 shutil，如果不在函数顶部声明，
    # Python 会把 ok 当成局部变量，最后一行 print 就会 UnboundLocalError（踩过一次）。
    global ok

    # 用工作区内的临时库，避免污染真实数据。
    # **不能用 tempfile.gettempdir()**：沙箱把系统临时目录限成只读，
    # mkdir 会 PermissionError（本项目里这个坑踩过不止一次）。
    tmp = Path(r"E:\AI\_kbtest")
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    from suite import boot

    boot.prepare()

    from suite import rag

    rag.set_base_dir(tmp)

    print("=" * 70)
    print("① 分词（中文必须切成多个 token）")
    print("=" * 70)
    toks = rag.tokenize("乡村教师职业倦怠")
    check("分词结果非空", bool(toks.strip()))
    check("切成了多个 token（不是整句一个）", len(toks.split()) >= 2, f"{len(toks.split())} 个：{toks[:40]}")
    check("jieba 可用（而非降级到单字）", rag._get_jieba() is not None,
          "不可用时会降级为单字切分")

    print("\n" + "=" * 70)
    print("② 分块")
    print("=" * 70)
    # 单份文档短于 CHUNK_CHARS 时本来就该是 1 块——所以用**加长文本**验证多块切分，
    # 而不是拿两份短文拼在一起。1036 字 / 600 上限 = 2 块左右，这是对的。
    long_text = (DOC_A + "\n\n") * 4
    chunks = rag.chunk_text(long_text)
    check("长文切出了多块", len(chunks) >= 2, f"{len(chunks)} 块，总 {len(long_text)} 字")
    check("每块都不过短", all(len(c) >= 30 for c in chunks),
          f"最短 {min(len(c) for c in chunks)} 字")
    check("块长不超过上限太多", all(len(c) <= rag.CHUNK_CHARS + 200 for c in chunks),
          f"最长 {max(len(c) for c in chunks)} 字")
    check("短文本得到 1 块（这也是对的）", len(rag.chunk_text(DOC_A)) == 1,
          f"{len(rag.chunk_text(DOC_A))} 块")
    # 重叠要真的生效：相邻两块应有共同片段。
    # 早先这里有个 bug——重叠只在某个分支才加上，"刚切完一块"这个最常见的情形反而没有，
    # 于是跨块论证会被切断。这条断言专门守住它。
    if len(chunks) >= 2:
        tail_of_first = chunks[0][-rag.CHUNK_OVERLAP:]
        check("相邻块有重叠（跨块论证不会被切断）",
              tail_of_first[:40] in chunks[1],
              f"首块尾部 {len(tail_of_first)} 字出现在次块中：{tail_of_first[:26]!r}")

    print("\n" + "=" * 70)
    print("③ 入库（SQLite FTS5）")
    print("=" * 70)
    r1 = rag.add_document(DOC_A, title="乡村教师职业倦怠调查研究")
    check("第一份入库成功", r1.get("ok") is True, str(r1.get("error") or "")[:60])
    r2 = rag.add_document(DOC_B, title="注意力机制综述")
    check("第二份入库成功", r2.get("ok") is True, str(r2.get("error") or "")[:60])
    st = rag.stats()
    check("统计到 2 份文档", st.get("documents") == 2, str(st))
    check("统计到若干块", st.get("chunks", 0) >= 2, f"{st.get('chunks')} 块")

    print("\n" + "=" * 70)
    print("④ 检索（最关键的一条：中文查得到吗）")
    print("=" * 70)
    cases = [
        ("职业倦怠", "乡村教师职业倦怠调查研究", "注意力机制综述"),
        ("组织支持", "乡村教师职业倦怠调查研究", None),
        ("注意力机制", "注意力机制综述", None),
        ("情感耗竭", "乡村教师职业倦怠调查研究", None),
    ]
    for query, want_title, avoid_title in cases:
        hits = rag.search(query, k=5)
        titles = [h.doc_title for h in hits]
        hit_ok = any(want_title in t for t in titles)
        flag = "OK " if hit_ok else "!! "
        if not hit_ok:
            ok = False
        print(f"  {flag}查「{query}」→ {len(hits)} 块，命中来源：{sorted(set(titles))}")
        check(f"  检索「{query}」命中了目标资料", hit_ok)
        if avoid_title and hits:
            # 不要求绝对不出现（关键词可能恰好在另一份里也有），但排第一的应该是目标
            top = titles[0]
            check(f"  检索「{query}」首位是目标资料", want_title in top,
                  f"首位={top}")
        for h in hits[:1]:
            check(f"  命中块带引用串", "第" in h.citation(), h.citation()[:50])

    print("\n" + "=" * 70)
    print("⑤ 转成写作层可用的证据结构")
    print("=" * 70)
    ev, info = rag.search_as_evidence("职业倦怠 教学投入", k=3)
    check("产出证据条目", len(ev) >= 1, f"{len(ev)} 条")
    if ev:
        e = ev[0]
        need = {"title", "citation", "text", "origin", "doc_id"}
        check("证据字段齐全", need <= set(e), f"缺 {need - set(e)}")
        check("标记为本地知识库来源", e.get("origin") == "kb", str(e.get("origin")))
        check("引用串含来源与位置", "第" in str(e.get("citation")), str(e.get("citation"))[:60])
    check("带上了能力边界说明", "关键词检索" in str(info.get("limits")), "")
    print(f"    边界说明：{info.get('limits', '')[:80]}…")

    print("\n" + "=" * 70)
    print("⑥ 去重、列表、删除")
    print("=" * 70)
    again = rag.add_document(DOC_A, title="乡村教师职业倦怠调查研究")
    check("同一份资料重复入库不新增文档", rag.stats().get("documents") == 2,
          f"documents={rag.stats().get('documents')}")
    docs = rag.list_documents()
    check("列表返回 2 份", len(docs) == 2, str([d["title"] for d in docs]))
    check("列表含字数与块数", all("chars" in d and "chunks" in d for d in docs))
    d = rag.delete_document(r2["doc_id"])
    check("删除成功", d.get("ok") is True, str(d))
    check("删后只剩 1 份", rag.stats().get("documents") == 1)
    after = rag.search("注意力机制", k=5)
    check("删掉的资料检索不到了", not any("注意力机制综述" in h.doc_title for h in after),
          f"{len(after)} 块")

    print("\n" + "=" * 70)
    print("⑦ 边界：空输入、超短输入、FTS 特殊字符")
    print("=" * 70)
    check("空文本入库被拒", rag.add_document("").get("ok") is False)
    check("超短文本入库被拒", rag.add_document("太短。").get("ok") is False)
    weird = rag.search('"*() :^- AND OR', k=3)
    check("特殊字符查询不崩（返回列表）", isinstance(weird, list), f"{len(weird)} 块")
    check("空查询返回空", rag.search("   ") == [])

    # 清理
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
