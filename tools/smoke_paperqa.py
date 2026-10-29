"""验证 PaperQA2 本地 RAG 链路：物化语料 → 建索引 → 提问 → 文献简报。

两种模式：
* ``--offline``（默认）：只验证**不需要模型**的部分——依赖是否就位、
  凭据解析优先级、语料物化（含截断与缺文处理）、子进程的环境变量构造。
  不联网、不花钱。**任何一次改动都应该先跑这个。**
* ``--live``：真的建索引并提一个问题，会调用模型（几篇文献约 ¥0.01~0.05，
  首次建索引 3 篇约 1 分钟）。跑完会把索引留在 ``.tmp/smoke_pqa`` 下，
  所以第二次跑会走"复用索引"这条路径——那正是最该被验证的路径。

**绝不碰真实知识库**：全部数据走 ``rag.set_base_dir(.tmp/smoke_pqa/kb)``，
跑完就删。这个脚本被误当成"生产工具"过一次的代价是用户丢资料，不能冒这个险。

用法：
    python tools/smoke_paperqa.py                # 离线自检
    python tools/smoke_paperqa.py --live         # 联网 + 调模型
    python tools/smoke_paperqa.py --live --key-file E:\\path\\to\\.env
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

SANDBOX = Path(r"E:\AI\thesis-suite\.tmp\smoke_pqa")

#: 三篇同一主题但方法不同的短文。用真实中文论文的节选结构（摘要/引言/方法/结论），
#: 因为要顺带验证分块与"中文长文"相关的处理；纯英文样本测不出中文的那些坑。
PAPERS = [
    ("数据资产的会计确认与计量研究", """摘要：随着数据要素市场化配置改革的推进，企业数据资产的会计确认与计量成为亟待解决的问题。
本文基于资产的定义要件，讨论数据资产是否满足确认条件。研究认为外购数据资产可确认为无形资产；
自行开发的数据资产研发阶段支出应费用化，开发阶段满足资本化条件的应予资本化。
在计量属性上主张以历史成本为主、公允价值为辅的混合计量模式。

一、引言
数据要素已被明确为第五大生产要素。现行准则体系尚无针对数据资产的专门规定，
实务中主要参照《企业会计准则第6号——无形资产》处理。

二、数据资产的特征
数据资产具有协同性、部分排他性、收益性、价值易变性、无形性与物理依附性六项特征。
其中价值易变性是关键：同一份数据在不同场景下的价值可能相差数个数量级。

三、会计确认
确认数据资产需同时满足三个条件：由过去交易或事项形成、由企业拥有或控制、预期带来经济利益。
用户数据的所有权往往不属于企业，企业可能通过用户协议获得使用权，这种控制是否足以支撑确认尚无共识。

四、计量
历史成本法下外购数据按实际支付对价入账；自行采集的数据按直接人工与直接费用归集。
公允价值取得困难，因为数据交易市场尚不成熟。因此主张混合计量。

五、结论与局限
结论建立在准则演绎与案例分析之上，缺乏大样本实证证据，能否推广到互联网平台企业仍有待检验。"""),
    ("企业数据资产的会计处理及信息列报研究", """摘要：本文以A股上市公司2019至2023年年报为样本，手工检索披露了数据资产相关信息的公司，
考察其会计处理与列报方式。研究发现绝大多数公司将数据相关支出计入研发费用或管理费用，
仅有少数公司尝试在资产负债表中单列数据资产科目，列报方式不统一导致信息可比性差。

一、研究背景
财政部2023年发布的《企业数据资源相关会计处理暂行规定》自2024年1月1日起施行，
明确企业可将符合条件的数据资源确认为无形资产或存货。

二、研究方法
采用年报文本分析法。从巨潮资讯网获取全部A股上市公司年报，以数据资产、数据资源、
数据要素为关键词检索，人工复核命中段落，最终得到有效样本428家。

三、主要发现
入表比例极低，428家样本中仅37家在资产负债表中确认了数据资源，占比8.6%。
处理方式高度分散：计入研发支出者占41.4%，计入管理费用者占28.3%。
行业差异显著：信息传输、软件和信息技术服务业入表比例19.2%，制造业仅4.1%。

四、研究局限
样本仅覆盖A股上市公司，未包含非上市企业与互联网平台公司，关键词检索可能遗漏，
存在低估入表比例的风险。"""),
    ("数据资产、产业链联动与企业全要素生产率", """摘要：本文以2011至2022年中国A股上市公司为样本，采用双重差分模型考察数据资产
对企业全要素生产率的影响。研究发现数据资产显著提升了企业全要素生产率，
且这一效应在产业链上下游联动程度高的企业中更为明显。

一、识别策略
利用《企业数据资源相关会计处理暂行规定》作为外生冲击构建双重差分模型。
平行趋势检验显示政策实施前两组企业的全要素生产率走势无显著差异。

二、实证结果
基准回归系数为0.086，在1%水平上显著，表明数据资产使企业全要素生产率提升约8.6%。
异质性分析显示产业链联动程度高的子样本系数为0.131，低联动组为0.042。

三、稳健性
进行了安慰剂检验、更换全要素生产率测度方法、剔除疫情年份等检验，结论保持一致。

四、局限
以是否披露数据资产作为处理变量的代理，存在测量误差；政策冲击时点较短，长期效应无法识别。"""),
]


def seed() -> list[str]:
    from suite import rag

    ids = []
    for title, text in PAPERS:
        result = rag.add_document(text, title=title, origin="smoke_paperqa",
                                  meta={"authors": "测试作者", "year": "2024",
                                        "venue": "测试期刊", "tier": "CSSCI",
                                        "citations": 9, "issn": "1000-0000",
                                        "citation": title})
        if not result.get("ok"):
            raise SystemExit(f"入库失败：{result.get('error')}")
        ids.append(result["doc_id"])
    return ids


def check_offline(doc_ids: list[str]) -> list[str]:
    import os

    from suite import credentials as C
    from suite import paperqa as P
    from suite import rag

    fails: list[str] = []
    print("\n--- 1. 组件可用性（不联网） ---")
    st = P.status()
    detail = st["detail"]
    print(f"  可用：{st['available']}")
    for key in ("libs", "version", "pymupdf_reader", "local_embedding",
                "embedding_cached", "max_docs"):
        print(f"    {key:20} = {detail.get(key)}")
    if st["missing"]:
        print(f"  缺失：{st['missing']}")
    if not st["available"]:
        fails.append("PaperQA2 不可用（检查 SUITE_PQA_LIBS）")
    if not detail.get("pymupdf_reader"):
        # 不是硬失败：只有解析 CNKI PDF 时才需要它。但必须说出来。
        print("  [注意] 没有 paper-qa-pymupdf，解析 CNKI PDF 会失败（纯文本语料不受影响）")
    if not detail.get("embedding_cached"):
        print("  [注意] 本地向量模型缓存没找到，首次索引会尝试联网下载（本机多半会失败）")

    print("\n--- 2. 凭据解析优先级（不联网） ---")
    # 访客填了就必须用访客的，绝不能因为"站点也配了"就换成站点的。
    visitor = C.Credentials(api_key="sk-visitor", base_url="https://api.deepseek.com/v1",
                            model="deepseek-chat")
    cfg = P.resolve_config(visitor, provider="deepseek")
    print(f"  访客凭据 → source={cfg.source} model={cfg.litellm_model}")
    if cfg.source != "visitor" or cfg.api_key != "sk-visitor":
        fails.append("访客凭据被覆盖了（BYOK 防线失效）")
    if cfg.litellm_model != "deepseek/deepseek-chat":
        fails.append(f"litellm 模型串不对：{cfg.litellm_model}")

    empty = P.resolve_config(C.Credentials(), provider="deepseek")
    print(f"  空凭据   → source={empty.source} key={'有' if empty.api_key else '无'}")
    if empty.api_key and not os.environ.get(P.LOCAL_KEY_ENV):
        fails.append("没设 SUITE_PQA_LOCAL_KEY_FILE 却拿到了 Key（不该有静默回退）")

    print("\n--- 3. 子进程环境隔离（不联网） ---")
    env = P._child_env(cfg)   # 注意传的是 PqaConfig，不是 Credentials
    leaked = os.environ.get("DEEPSEEK_API_KEY", "")
    print(f"  子进程 env 里 DEEPSEEK_API_KEY = {'已注入' if env.get('DEEPSEEK_API_KEY') else '无'}")
    print(f"  父进程 os.environ 里是否有 Key = {'有（说明本来就设了）' if leaked else '没有'}")
    if env.get("DEEPSEEK_API_KEY") != "sk-visitor":
        fails.append("子进程 env 没拿到访客 Key")
    if os.environ.get("DEEPSEEK_API_KEY") == "sk-visitor":
        fails.append("访客 Key 被写进了父进程 os.environ（并发会互相覆盖）")
    if env.get("SENTENCE_TRANSFORMERS_HOME") != str(P.ST_HOME):
        fails.append("子进程没指向现成的向量模型缓存")

    print("\n--- 4. 语料物化（不联网） ---")
    corpus = P.materialize(doc_ids, rebuild=True)
    print(f"  ok={corpus.get('ok')} docs={corpus.get('docs')} chars={corpus.get('chars')}")
    if not corpus.get("ok"):
        fails.append(f"物化失败：{corpus.get('error')}")
        return fails

    files = sorted(p for p in Path(corpus["dir"]).rglob("*") if p.is_file())
    print(f"  生成 {len(files)} 个文件：")
    for path in files:
        print(f"    {path.relative_to(corpus['dir'])}  {path.stat().st_size} B")
    if len(files) != len(doc_ids):
        fails.append(f"物化文件数 {len(files)} ≠ 文献数 {len(doc_ids)}")

    # 语料目录里**不能**有签名文件之类的东西——实测被 PaperQA2 当成第 4 篇文档索引过。
    strays = [p.name for p in files if p.suffix != ".txt"]
    if strays:
        fails.append(f"语料目录里混进了非正文文件：{strays}")

    # 正文开头必须带【文献信息】，PaperQA2 靠它给引用补书目。
    sample = files[0].read_text(encoding="utf-8")
    if "【文献信息】" not in sample[:200]:
        fails.append("语料正文开头缺少【文献信息】书目头")
    if "题名：" not in sample[:300]:
        fails.append("书目头里没有题名")

    # 复用路径：第二次物化必须命中缓存，否则每次提问都会白白重建索引。
    again = P.materialize(doc_ids, rebuild=False)
    print(f"  第二次物化：reused={again.get('reused')} chars={again.get('chars')}")
    if not again.get("reused"):
        fails.append("第二次物化没有复用缓存（会导致索引反复重建、白花钱）")
    if not again.get("chars"):
        fails.append("复用路径没有报出字符数（前端会显示 0）")

    print("\n--- 5. 边界情形（不联网） ---")
    too_many = P.materialize([f"doc-{i:016d}" for i in range(P.MAX_DOCS + 1)])
    print(f"  超过上限：ok={too_many.get('ok')} error={str(too_many.get('error'))[:60]}")
    if too_many.get("ok"):
        fails.append("超过 MAX_DOCS 没有被拒绝")

    unknown = P.materialize(["doc-0000000000000000"], rebuild=True)
    print(f"  全是未知 id：ok={unknown.get('ok')} error={str(unknown.get('error'))[:60]}")
    if unknown.get("ok"):
        fails.append("未知 doc_id 没有报错")

    empty_sel = P.materialize([])
    if empty_sel.get("ok"):
        fails.append("空选择没有被拒绝")

    # 恢复真实语料目录（第 4 步把它换成沙箱内容了）。
    P.materialize(doc_ids, rebuild=True)
    return fails


def check_live(doc_ids: list[str], key_file: str) -> list[str]:
    from suite import paperqa as P

    fails: list[str] = []
    print("\n--- 6. 真实建索引 + 提问（调模型） ---")
    cfg = P.resolve_config(__import__("suite.credentials", fromlist=["x"]).Credentials(),
                           provider="deepseek")
    if key_file:
        # 允许显式指定凭据文件，省得每次都 export 环境变量。
        import os

        os.environ[P.LOCAL_KEY_ENV] = key_file
        cfg = P.resolve_config(__import__("suite.credentials", fromlist=["x"]).Credentials(),
                               provider="deepseek")
    if not cfg.api_key:
        print("  [跳过] 没有可用的 Key。设 SUITE_PQA_LOCAL_KEY_FILE 或加 --key-file。")
        return fails

    print(f"  凭据来源：{cfg.source}；模型：{cfg.litellm_model}")

    started = time.time()
    result = P.ask(cfg, "这三篇文献在数据资产的会计确认上有什么分歧？", doc_ids,
                   rebuild=False, timeout=1800)
    took = time.time() - started
    print(f"  ok={result.get('ok')} 耗时={took:.1f}s 复用索引={result.get('reused')} "
          f"成本≈{result.get('cost')}")
    if result.get("progress"):
        print("  进度：")
        for line in result["progress"]:
            print(f"    · {line}")

    if not result.get("ok"):
        # 这里**不能**把失败一律当成代码 bug：没 Key、网络不通、限流都会走到这儿。
        # 但要区分"子进程崩了"（严重）与"模型调用失败"（环境问题）。
        err = str(result.get("error") or "")
        print(f"  [失败] {err[:300]}")
        if "子进程异常退出" in err or "TypeError" in err:
            fails.append(f"PaperQA2 子进程异常（多半是依赖或调用方式坏了）：{err[:200]}")
        else:
            print("  （看起来是环境/凭据问题，不算代码失败）")
        return fails

    answer = result.get("answer") or ""
    sources = result.get("sources") or []
    print(f"\n  答案（前 600 字）：\n    {answer[:600].replace(chr(10), chr(10) + '    ')}")
    print(f"\n  来源 {len(sources)} 条：")
    for src in sources[:5]:
        print(f"    [{src.get('key')}] {(src.get('citation') or '')[:70]}")

    if len(answer) < 40:
        fails.append("答案过短，可能没有真正生成")
    if not sources:
        fails.append("没有任何来源（引用无法回查）")
    # 关键：来源必须能映射回语料里的文件名（也就是题名），否则答案里的引用是空话。
    if sources and not any(("数据资产" in (s.get("citation") or "")) for s in sources):
        fails.append("来源 citation 里找不到任何一篇文献的题名（映射失效）")

    print("\n--- 7. 文献简报（调模型） ---")
    started = time.time()
    brief = P.brief(cfg, doc_ids[:1], focus="数据资产的会计确认", mode="brief",
                     rebuild=False, timeout=1800)
    print(f"  ok={brief.get('ok')} 耗时={time.time() - started:.1f}s 成本≈{brief.get('cost')}")
    if not brief.get("ok"):
        print(f"  [失败] {str(brief.get('error'))[:200]}")
        fails.append("文献简报失败")
        return fails

    payload = brief.get("answer") or ""
    # 简报的正文在 ``brief.sections`` 里，**不在 answer 里**。这一条曾经漏过：
    # _collect() 没把子进程的 brief 字段带出来，接口返回一个没有 brief 键的响应，
    # 前端只能渲染空内容——而模型调用已经花掉了。所以这里专门断言它必须在。
    print(f"  answer 长度={len(payload)}（简报模式下应为空）")
    sections = (brief.get("brief") or {}).get("sections") or []
    print(f"  brief.sections 共 {len(sections)} 节：")
    for section in sections:
        text = (section.get("text") or "").strip()
        mark = "OK  " if text else "空  "
        err = section.get("error") or ""
        print(f"    [{mark}] {section.get('section')}："
              f"{(text or err)[:70].replace(chr(10), ' ')}")
    if not sections:
        fails.append("简报响应里没有 brief.sections（_collect 把 brief 字段丢了）")
    filled = [s for s in sections if (s.get("text") or "").strip()]
    if len(filled) < max(1, len(sections) // 2):
        fails.append(f"简报只有 {len(filled)}/{len(sections)} 节有内容，可能整体失败")

    # 输出口径的两条硬断言，对应的都是实测踩过的坑：
    # 1) 开头不能有 `Question:` 回声（PaperQA2 的 formatted_answer 会把它印在第一行）；
    # 2) 不能残留 `xxx chunk 1` 这种内部 chunk 标记——答案里该是 `[n]`，
    #    与下面来源卡片的编号一一对应，否则读者没法按编号回查。
    echoed = [s["section"] for s in sections if (s.get("text") or "").lstrip().startswith("Question:")]
    print(f"  开头带 Question 回声的小节：{echoed or '无'}")
    if echoed:
        fails.append(f"answer/section 里残留 Question 回声：{echoed}")

    chunk_marked = [s["section"] for s in sections if "chunk " in (s.get("text") or "")]
    print(f"  残留内部 chunk 标记的小节：{chunk_marked or '无'}")
    if chunk_marked:
        fails.append(f"answer/section 里残留 chunk 标记（应已归一为 [n]）：{chunk_marked}")
    return fails


def main() -> int:
    live = "--live" in sys.argv
    key_file = ""
    if "--key-file" in sys.argv:
        idx = sys.argv.index("--key-file")
        if idx + 1 < len(sys.argv):
            key_file = sys.argv[idx + 1]

    # 沙箱隔离：**必须在导入 rag 之后、使用之前**重定向，否则会写到真实知识库。
    from suite import paperqa as P
    from suite import rag

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX, ignore_errors=True)
    (SANDBOX / "kb").mkdir(parents=True, exist_ok=True)
    rag.set_base_dir(SANDBOX / "kb")
    # 索引与语料也一起挪进沙箱，避免污染站点真实索引。
    P.PQA_DIR = SANDBOX / "pqa"
    P.INDEX_DIR = P.PQA_DIR / "index"
    P.STAGE_DIR = P.PQA_DIR / "corpus"
    P.STAGE_SIG = P.PQA_DIR / "corpus-signature.json"

    print("=" * 78)
    print(f"PaperQA2 链路验证（{'联网 + 调模型' if live else '离线'}）")
    print("=" * 78)
    print(f"沙箱：{SANDBOX}（真实知识库与索引不受影响）")

    doc_ids = seed()
    print(f"已入库 {len(doc_ids)} 篇测试文献：{doc_ids}")

    fails = check_offline(doc_ids)
    if live:
        fails += check_live(doc_ids, key_file)
    else:
        print("\n（跳过联网部分；加 --live 才会真的建索引并提问）")

    print("\n" + "=" * 78)
    if fails:
        print(f"有 {len(fails)} 项未通过：")
        for item in fails:
            print(f"  - {item}")
        return 1
    print("离线部分全部通过：依赖就位、凭据不越权、语料物化正确、边界被挡住。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
