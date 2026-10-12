"""验证 agent 架构：注册表、通用接口、实时状态、事件流。"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, r"E:\AI\thesis-suite")

ok = True


def check(name: str, cond: bool, detail: str = "") -> None:
    global ok
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    ok = ok and cond


def main() -> int:
    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())

    print("=" * 70)
    print("① 注册表：四个 agent 与各自的动作")
    print("=" * 70)
    r = client.get("/api/agents")
    check("GET /api/agents 200", r.status_code == 200)
    data = r.json()
    agents = {a["name"]: a for a in data["agents"]}
    check("注册了四个 agent", set(agents) == {"topic", "gather", "writer", "review"},
          str(sorted(agents)))
    for name, title, stage in (("topic", "题目生成", "topic"), ("gather", "资料获取", "proposal"),
                               ("writer", "论文写作", "draft"), ("review", "评估", "revise")):
        a = agents.get(name) or {}
        check(f"{name}（{title}）绑定阶段 {stage}", a.get("stage") == stage)
        check(f"{name} 有动作", len(a.get("actions") or []) >= 2,
              str([x["name"] for x in a.get("actions") or []]))

    # 新能力的动作必须在表里
    want = {
        "topic": {"generate", "outline"},
        "gather": {"import", "extract", "verify", "export"},
        "writer": {"draft", "revise", "export_markdown", "export_docx"},
        "review": {"check", "verify_refs", "opinion", "duplication", "template"},
    }
    for name, expected in want.items():
        got = {x["name"] for x in (agents.get(name) or {}).get("actions") or []}
        check(f"{name} 的动作齐全", expected <= got, f"缺 {sorted(expected - got)}")

    print("\n" + "=" * 70)
    print("② 动作白名单（不能调没登记的动作）")
    print("=" * 70)
    r = client.post("/api/agents/topic/不存在动作", json={})
    check("未知动作被拒 404", r.status_code == 404, f"HTTP {r.status_code}")
    r = client.post("/api/agents/不存在agent/generate", json={})
    check("未知 agent 被拒 404", r.status_code == 404, f"HTTP {r.status_code}")

    print("\n" + "=" * 70)
    print("③ 通用接口能真正驱动 agent（用离线题目生成，不花钱）")
    print("=" * 70)
    r = client.post("/api/agents/topic/generate",
                    json={"discipline": "教育学", "count": 2, "mode": "offline"})
    check("驱动 topic/generate 成功", r.status_code == 200, f"HTTP {r.status_code}")
    body = r.json()
    check("返回了 sid", bool(body.get("sid")), str(body.get("sid"))[:12])
    check("生成成功", body.get("ok") is True, str(body.get("error") or "")[:80])
    result = body.get("result") or {}
    check("拿到题目", len(result.get("topics") or []) > 0,
          f"{len(result.get('topics') or [])} 个，mode={result.get('mode')}")
    sid = body.get("sid", "")

    print("\n" + "=" * 70)
    print("④ 实时状态与事件流（这就是「实时更新子 agent」）")
    print("=" * 70)
    r = client.get(f"/api/agents/state?sid={sid}")
    check("GET /api/agents/state 200", r.status_code == 200)
    states = {a["name"]: a for a in r.json().get("agents") or []}
    check("返回四个 agent 的状态", len(states) == 4, str(sorted(states)))
    topic_state = states.get("topic") or {}
    check("topic 记录了成功次数", topic_state.get("ok_count", 0) >= 1,
          f"ok={topic_state.get('ok_count')} failed={topic_state.get('failed_count')}")
    check("topic 的 last 有动作与耗时",
          bool((topic_state.get("last") or {}).get("action")),
          json.dumps(topic_state.get("last") or {}, ensure_ascii=False)[:110])
    check("topic 当前不忙", topic_state.get("busy") is False)
    # 没跑过的 agent 应该是零次
    check("未使用的 agent 计数为 0",
          (states.get("writer") or {}).get("total") == 0)

    r = client.get(f"/api/agents/events?sid={sid}")
    check("GET /api/agents/events 200", r.status_code == 200)
    events = r.json().get("events") or []
    check("事件流有记录", len(events) >= 1, f"{len(events)} 条")
    if events:
        e = events[-1]
        check("事件含 agent/action/status/seconds",
              all(k in e for k in ("agent", "action", "status", "seconds", "at")),
              json.dumps(e, ensure_ascii=False)[:130])
        check("事件状态为 ok", e["status"] == "ok", e["status"])

    print("\n" + "=" * 70)
    print("⑤ 不需要 Key 的动作清单（用于前端提示）")
    print("=" * 70)
    for name, a in agents.items():
        free = [x["name"] for x in a["actions"] if not x["needs_key"]]
        print(f"  {name:8} 免 Key 动作：{free or '（无）'}")

    print("\n" + "=" * 70)
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
