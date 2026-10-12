"""真实链路测试：用真实模型跑「资料获取 → 写作 → 评估」。

密钥从既有项目的 .env 里读出来，**只作为测试客户端通过请求头传入**——
这恰好模拟了真实访客的 BYOK 用法，服务器端不存在任何密钥回退路径。

这个脚本会真的花钱（几次模型调用），所以不放在常规冒烟测试里。

用法::

    $env:PYTHONPATH = "E:\\AI\\thesis-suite;E:\\AI\\aigc-reducer\\libs;E:\\AI\\thesis-assistant\\libs"
    python tools\\live_chain.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

from fastapi.testclient import TestClient  # noqa: E402

ENV_CANDIDATES = [
    Path(r"E:\AI\thesis-agents\.env"),
    Path(r"E:\AI\thesis-assistant\.env"),
    Path(r"E:\AI\thesis-agents\.env.example"),
]

#: 两份真实论文摘要（内容为公开研究的概括性描述，用于验证抽取与写作的引用纪律）。
SOURCE_ONE = """教师职业倦怠对乡村教育质量的影响机制研究——基于某省 24 个县的调查

摘要：本研究以某省 24 个县 1836 名乡村教师为样本，采用 Maslach 职业倦怠量表与自编教学投入问卷，
考察职业倦怠对教学投入的影响及组织支持的调节作用。研究发现：乡村教师职业倦怠总体处于中等偏上水平，
其中情感耗竭维度得分最高；职业倦怠对教学投入具有显著负向影响，标准化回归系数为 -0.34；
组织支持在两者关系中起显著调节作用，高组织支持组别的负向效应明显减弱。
研究建议从减轻非教学负担、完善职称晋升通道与加强心理支持三个方面改善乡村教师职业状态。
关键词：乡村教师；职业倦怠；教学投入；组织支持
"""

SOURCE_TWO = """组织支持感研究述评

摘要：组织支持感指员工对组织是否重视其贡献、关心其福祉的总体知觉。本文系统梳理了组织支持感的
理论基础（社会交换理论、组织拟人化思想）、测量工具（SPOS 简短量表）与前因后果变量。
既有研究一致表明，组织支持感能够缓冲工作压力对工作投入的负面影响，但其作用机制在不同文化情境下
存在差异：集体主义文化中，来自同事与家庭的支持可能与组织支持产生替代效应。
现有研究的不足在于：纵向追踪研究较少，且对乡村场域的适用性缺乏检验。
"""

SOURCE_THREE = """参考文献
[1] 李梅, 王强. 乡村教师职业倦怠的现状与影响因素[J]. 教育研究, 2021, 42(6): 88-97.
[2] 陈静. 组织支持感与教师工作投入的关系研究[D]. 北京: 北京师范大学, 2019.
"""


def pick_creds() -> tuple[str, str, str] | None:
    for path in ENV_CANDIDATES:
        if not path.is_file():
            continue
        env: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")
        key = env.get("LLM_API_KEY", "")
        if key:
            print(f"（密钥来源：{path}，脱敏 {key[:5]}…{key[-4:]}）")
            return key, env.get("LLM_BASE_URL", ""), env.get("LLM_MODEL", "")
    return None


def sse(client: TestClient, path: str, body: dict, headers: dict) -> list[dict]:
    events: list[dict] = []
    with client.stream("POST", path, json=body, headers=headers) as resp:
        if resp.status_code != 200:
            resp.read()
            raise RuntimeError(f"{path} HTTP {resp.status_code}: {resp.text[:500]}")
        for raw in resp.iter_lines():
            if not raw or not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            if payload == "[DONE]":
                break
            try:
                events.append(json.loads(payload))
            except ValueError:
                pass
    return events


def main() -> int:
    creds = pick_creds()
    if creds is None:
        print("没有找到可用的 LLM_API_KEY（已找遍 .env 候选），跳过真实链路测试。")
        return 2
    key, base, model = creds
    headers = {"X-LLM-Key": key}
    if base:
        headers["X-LLM-Base"] = base
    if model:
        headers["X-LLM-Model"] = model

    from suite.api.app import create_app

    client = TestClient(create_app())
    sid = ""
    t0 = time.time()

    print("\n=== ① 资料获取：抽取证据（真实模型）===")
    resp = client.post("/api/gather/extract", headers=headers, json={
        "sources": [
            {"text": SOURCE_ONE, "title": "乡村教师职业倦怠调查"},
            {"text": SOURCE_TWO, "title": "组织支持感研究述评"},
            {"text": SOURCE_THREE, "title": "参考文献"},
        ]
    })
    print("HTTP", resp.status_code)
    if resp.status_code != 200:
        print(resp.text[:1500])
        return 1
    data = resp.json()
    sid = data.get("sid", "")
    print(f"新增 {data['added']} 条，共 {data['total']} 条，"
          f"逐字校验丢弃的片段 {data.get('dropped_passages')} 条")
    if data.get("errors"):
        print("失败：", data["errors"])
    for item in (data.get("items") or [])[:4]:
        print(f"  · {str(item.get('title'))[:40]} | {item.get('year')} | "
              f"片段 {len(item.get('passages') or [])} 条 | 结论 {len(item.get('claims') or [])} 条")
        for p in (item.get("passages") or [])[:1]:
            print(f"      原文片段：{str(p)[:70]}")

    if not data.get("added"):
        print("!! 没有抽取到任何证据，后续写作会被闸门拦下")
        return 1

    print("\n=== ② 文献真伪核验（Crossref，真实网络）===")
    refs = ("参考文献\n"
            "[1] 李梅, 王强. 乡村教师职业倦怠的现状与影响因素[J]. 教育研究, 2021.\n"
            "[2] Vaswani A, Shazeer N. Attention is all you need[C]. NeurIPS, 2017. "
            "DOI: 10.48550/arXiv.1706.03762\n"
            "[3] 张某某. 一个完全不存在的编造研究[J]. 虚构学报, 2023. DOI: 10.9999/fake.2023.001\n")
    resp = client.post("/api/review/verify", headers=headers,
                       json={"text": refs, "limit": 3})
    print("HTTP", resp.status_code)
    if resp.status_code == 200:
        v = resp.json()
        print("识别条目", v["total_entries"], "本次核验", v["checked"], "统计", v["tally"])
        for item in v["items"]:
            print(f"  · [{item['verdict']}] {item['raw'][:60]}")
    else:
        print("核验失败（可能是网络）：", resp.text[:300])

    print("\n=== ③ 写作：联网检索 + 分层引用（真实模型 + 真实文献库）===")
    # 本次改动的主角：不再要求先录证据，写作会自己去公开文献库检索。
    # 同时仍保留可靠性约束——引用编号必须在池内、数字必须有依据。
    events = sse(client, "/api/writing/draft", headers=headers, body={
        "sid": sid,
        "title": "乡村教师职业倦怠对教学投入的影响机制研究",
        "genre": "empirical",
        "sections": ["第1章 绪论", "第2章 文献综述"],
        "use_web": True,
    })
    draft = next((e["draft"] for e in events if e.get("type") == "result"), None)
    coverage = next((e.get("coverage") for e in events if e.get("type") == "result"), None)
    for e in events:
        if e.get("type") == "stage":
            print(f"  [{e.get('stage')}] {e.get('message')}")
    if draft is None:
        err = next((e.get("message") for e in events if e.get("type") == "error"), "未知错误")
        print("!! 写作失败：", err)
        return 1

    print(f"\n生成 {len(draft['sections'])} 章，共 {draft['words']} 字")
    print(f"本次检索到的真实文献：{draft.get('web_records_total', 0)} 条")
    if coverage:
        print("依据覆盖度：")
        for k, v in coverage.items():
            print(f"  {k}: {v}")
    if draft.get("notice"):
        print("提示：", draft["notice"])

    body = "\n".join(s["body"] for s in draft["sections"])
    print("\n正文片段：\n" + body[:900])
    import re
    cites = sorted({int(m) for m in re.findall(r"[\[【](\d{1,3})[\]】]", body)})
    print("\n正文里出现的引用编号：", cites)
    print(f"证据库现有条数（含本次并入的联网文献）：{len(client.get('/api/writing/versions?sid=' + sid).json().get('versions') or [])} 个版本")

    print("\n=== ④ 评估：检查器 + 数字接地（真实链路闭环）===")
    # 这里刻意用 `##` 当章节标题：验证评估入口的标题归一化确实生效。
    # 上游切分器只认单个 `#`，所以不归一化的话「数字是否有依据」会静默失效。
    text = (f"# {draft['title']}\n\n" if draft.get("title") else "") + "\n\n".join(
        f"## {s['heading']}\n{s['body']}" for s in draft["sections"])

    # 诊断：评估阶段实际看到的是什么。
    #
    # 诊断必须用**与检查器同一套**数字提取（fact_numbers），不能自己写正则。
    # 先前用 `\d+(\.\d+)?` 把「教龄11～15年」这种**区间**拆出了个 "10"，
    # 于是把"检查器报 0"误判成了失效——而实际那一版稿子的数字全都有依据。
    # 诊断口径与实现不一致，会比没有诊断得出更坏的结论。
    from suite.sessions import STORE as _STORE

    _sess = _STORE.get(sid) if sid else None
    if _sess is not None:
        from app.writing.citations import fact_numbers

        def _nums(blob: str) -> set[str]:
            return {
                n.rstrip("0").rstrip(".") if "." in n else n
                for n in fact_numbers(blob)
                if "." in n or len(n.lstrip("0")) >= 2
            }

        ledger = _sess.meta.get("writing_evidence") or {}
        entries = [e for v in ledger.values() for e in v]
        ledger_chars = sum(len(str(e.get("text") or "")) for e in entries)
        in_body = _nums("\n".join(s["body"] for s in draft["sections"]))
        in_ledger = _nums(" ".join(str(e.get("text") or "") + str(e.get("citation") or "")
                                   for e in entries))
        print(f"  诊断：会话证据 {len(_sess.evidence)} 条 | 台账桶 {list(ledger)} | "
              f"台账条目 {len(entries)} | 台账正文字数 {ledger_chars}")
        print(f"  诊断：正文事实数字 {len(in_body)} 个 → {sorted(in_body)}")
        print(f"  诊断：台账里找不到的 {sorted(in_body - in_ledger)}（与检查器同口径）")

    resp = client.post("/api/review/check", headers=headers,
                       json={"text": text, "sid": sid, "title": draft["title"]})
    print("HTTP", resp.status_code)
    # 把 /check 真正会用的送检文本打出来。此前诊断只打了本地拼的 text，
    # 看不出服务端实际拿到什么——而问题恰恰在那里。
    from suite.api.review import _text_of as _review_text_of

    _actual = _review_text_of({"text": text}, sid)
    print(f"  诊断：/check 实际送检文本的前 3 行：")
    for _line in _actual.split("\n")[:3]:
        print(f"         {_line[:66]!r}")
    if resp.status_code == 200:
        r = resp.json()
        print(f"检查器 {r['checked']} 项（失败 {len(r['failed_checkers'])}），"
              f"问题 {r['issue_count']} 个，评分 {r['score']}")
        print("严重度：", r["counts"])
        grounded = [c for c in r["by_checker"] if c["name"] in
                    ("check_number_grounding", "citation_grounding", "check_claim_evidence")]
        print("关键检查器命中：", [(c["name"], c["count"]) for c in grounded])
        print("建议：", r["recommendation"])
    else:
        print(resp.text[:400])

    print(f"\n总耗时 {time.time() - t0:.1f} 秒")

    # 把草稿与台账存盘，供 tools/_probe_long.py 逐项复核（避免继续靠猜）。
    try:
        import json as _json

        _dump = Path(r"E:\AI\thesis-suite\logs\last_draft.json")
        _dump.parent.mkdir(parents=True, exist_ok=True)
        _sess2 = _STORE.get(sid)
        _dump.write_text(_json.dumps({
            "draft": draft,
            "ledger": (_sess2.meta.get("writing_evidence") if _sess2 else {}) or {},
        }, ensure_ascii=False), encoding="utf-8")
        print(f"草稿样本已存盘：{_dump}")
    except Exception as _exc:  # noqa: BLE001
        print(f"（草稿样本存盘失败：{_exc}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
