"""验证六个板块与六个 agent 的注册。

只验证结构与不需要 Key 的动作；需要 Key 的动作验证"缺 Key 时提示可读"。
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"E:\AI\thesis-suite")

ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    ok = ok and cond


def main() -> int:
    import shutil
    from pathlib import Path

    tmp = Path(r"E:\AI\_kbtest3")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)

    from suite import boot

    boot.prepare()
    from suite import rag

    rag.set_base_dir(tmp)

    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())

    print("=" * 70)
    print("① 六个板块")
    print("=" * 70)
    r = client.get("/api/modules")
    check("GET /api/modules 200", r.status_code == 200)
    mods = r.json()["modules"]
    check("正好六个板块", len(mods) == 6, f"{len(mods)} 个")
    expect = [("topic", "定题目"), ("proposal", "开题报告"), ("draft", "写初稿"),
              ("revise", "修改与评估"), ("integrity", "查重与降 AIGC 率"),
              ("defense", "答辩")]
    for (mid, mname), m in zip(expect, mods):
        check(f"板块 {m['n']} 是「{mname}」", m["id"] == mid and m["name"] == mname,
              f"{m['id']}/{m['name']}")
        check(f"  「{mname}」有面板与动作", bool(m.get("panel")) and len(m.get("actions") or []) >= 3,
              f"panel={m.get('panel')} actions={len(m.get('actions') or [])}")

    print("\n" + "=" * 70)
    print("② 六个 agent")
    print("=" * 70)
    r = client.get("/api/agents")
    agents = {a["name"]: a for a in r.json()["agents"]}
    check("注册了六个 agent",
          set(agents) == {"topic", "gather", "writer", "review", "integrity", "defense"},
          str(sorted(agents)))
    # 每个 agent 的 stage 必须对应一个真实板块
    module_ids = {m["id"] for m in mods}
    for name, a in agents.items():
        check(f"{name} 归属的板块存在", a["stage"] in module_ids, a["stage"])

    print("\n" + "=" * 70)
    print("③ 新板块的动作")
    print("=" * 70)
    for name, expected in (
        ("integrity", {"duplication", "aigc_score", "aigc_reduce", "aigc_export_docx"}),
        ("defense", {"script", "slides", "pptx", "html"}),
    ):
        got = {x["name"] for x in (agents.get(name) or {}).get("actions") or []}
        check(f"{name} 动作齐全", expected <= got, f"缺 {sorted(expected - got)}")
        print(f"    {name}: {sorted(got)}")

    print("\n" + "=" * 70)
    print("④ 知识库接口（定题目板块的一部分）")
    print("=" * 70)
    check("stats 可用", client.get("/api/kb/stats").json().get("ok") is True)
    r = client.post("/api/kb/add", json={
        "text": "乡村教师职业倦怠已成为影响教学质量的关键因素，本研究以某省24个县1836名教师为样本。"
                "研究发现职业倦怠对教学投入具有显著负向影响。" * 2,
        "title": "乡村教师调查"})
    check("入库成功", r.status_code == 200 and r.json().get("ok") is True, str(r.json())[:80])
    r = client.post("/api/kb/search", json={"query": "职业倦怠 教学投入", "k": 3})
    d = r.json()
    check("检索命中", d.get("count", 0) >= 1, f"{d.get('count')} 块")
    check("返回证据结构", len(d.get("evidence") or []) >= 1)
    check("带能力边界说明", "关键词检索" in str(d.get("limits")), "")
    if d.get("hits"):
        h = d["hits"][0]
        check("命中块带来源与位置", "第" in str(h.get("citation")), str(h.get("citation"))[:50])
    # 上传 md 入库
    r = client.post("/api/kb/upload", files=[(
        "files", ("方法笔记.md",
                  "# 抽样方法\n\n本研究采用分层抽样，按教龄与职称两个维度分层，从三个地市抽取样本。".encode("utf-8"),
                  "text/markdown"))])
    check("上传文件入库成功", r.status_code == 200 and r.json().get("ok") is True,
          str(r.json())[:90])
    check("文档数变成 2", client.get("/api/kb/stats").json().get("documents") == 2,
          str(client.get("/api/kb/stats").json().get("documents")))

    print("\n" + "=" * 70)
    print("⑤ 需要 Key 的动作：缺 Key 时提示可读")
    print("=" * 70)
    for name, action in (("defense", "script"), ("defense", "slides"), ("integrity", "aigc_reduce")):
        r = client.post(f"/api/agents/{name}/{action}", json={})
        # aigc_reduce 不需要站点 Key（它转发给 reducer 服务），所以可能不是 400
        if r.status_code == 400:
            check(f"{name}/{action} 缺 Key 提示可读", "API Key" in r.text, r.text[:70])
        else:
            check(f"{name}/{action} 返回结构化结果（不需要站点 Key）",
                  r.status_code == 200, f"HTTP {r.status_code} {r.text[:60]}")

    print("\n" + "=" * 70)
    print("⑥ 不需要 Key 的答辩导出：缺幻灯片时提示可读")
    print("=" * 70)
    for action in ("pptx", "html"):
        r = client.post(f"/api/agents/defense/{action}", json={})
        body = r.json()
        check(f"defense/{action} 缺幻灯片时提示", body.get("ok") is False and "幻灯片" in str(body.get("error")),
              str(body.get("error"))[:70])

    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 70)
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
