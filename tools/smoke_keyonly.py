"""真实验证：只发 X-LLM-Key（模拟浏览器默认行为），走完整的「联网检索 → 写作 → 评估」。

这是本轮修掉的那个严重缺陷的回归测试：早先只发 Key 时 base_url 解析为空，
写作与评估的模型调用都会失败。所以这个测试**刻意不发 base_url / model**。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

ENV_CANDIDATES = [
    Path(r"E:\AI\thesis-agents\.env"),
    Path(r"E:\AI\thesis-assistant\.env"),
]


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


def main() -> int:
    key = pick_key()
    if not key:
        print("没有可用密钥，跳过。")
        return 2

    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())
    # 只发 Key：不发 X-LLM-Base / X-LLM-Model
    headers = {"X-LLM-Key": key}

    print("\n=== 写作（只发 Key）===")
    with client.stream("POST", "/api/writing/draft", headers=headers,
                       json={"title": "乡村教师职业倦怠对教学投入的影响机制研究",
                             "sections": ["第1章 绪论"],
                             "use_web": True}) as resp:
        result, err = None, ""
        for raw in resp.iter_lines():
            if not raw.startswith("data:"):
                continue
            payload = raw[5:].strip()
            if payload == "[DONE]":
                break
            try:
                ev = json.loads(payload)
            except ValueError:
                continue
            if ev.get("type") == "stage" and ev.get("stage") == "search":
                print(f"  [{ev.get('stage')}] {ev.get('message')}")
            if ev.get("type") == "result":
                result = ev
            if ev.get("type") == "error":
                err = ev.get("message", "")

    if result is None:
        print(f"  !! 失败：{err[:200]}")
        print("     如果报协议缺失，说明 base_url 兜底又坏了。")
        return 1

    draft, cov = result["draft"], result.get("coverage") or {}
    print(f"  成功：{len(draft['sections'])} 章 {draft['words']} 字")
    print(f"  检索到的真实文献：{draft.get('web_records_total')} 条")
    print(f"  引用：总计 {cov.get('citations_total')}"
          f"（用户 {cov.get('citations_user')} / 联网 {cov.get('citations_web')}）"
          f"；编造 {cov.get('citations_invalid')}；无依据数字 {cov.get('ungrounded_numbers')}")
    print(f"  结论：{cov.get('verdict')}")

    # 逐条列出联网文献的题名，证明依据是真实检索来的
    print("\n  联网依据（来自真实文献库，本次共 "
          f"{draft.get('web_records_total')} 条）：")
    for s in draft["sections"]:
        c = s.get("citations") or {}
        print(f"    {s['heading']}：引用 {c.get('total', 0)} 个"
              f"（资料 {c.get('user', 0)} / 联网 {c.get('web', 0)}，编造 {c.get('invalid', 0)}）")

    # 用写作层**同一份**正则，不要在这里另写一个——
    # 本轮在诊断里手写正则可不止一次得出与实现相反的结论（多转义一层就会）。
    from suite.api.writing import CITE_RE

    body = "\n".join(s["body"] for s in draft["sections"])
    cites = sorted({int(m) for m in CITE_RE.findall(body)})
    print(f"  正文引用编号（与写作层同正则）：{cites}")
    if cites and not any(c.get("citations", {}).get("total") for c in draft["sections"]):
        print("  !! 正文有编号但逐章统计为 0，两处口径不一致")
    print(f"  正文片段：{body[:200]}…")

    print("\n=== 评估（同一会话，只发 Key）===")
    r = client.post("/api/review/check", headers=headers,
                    json={"sid": result.get("sid"), "title": draft["title"]})
    print("  HTTP", r.status_code)
    if r.status_code == 200:
        d = r.json()
        by = {c["name"]: c["count"] for c in d["by_checker"]}
        print(f"  检查器 {d['checked']} 项，问题 {d['issue_count']} 个，评分 {d['score']}")
        print(f"  数字接地命中：{by.get('check_number_grounding')}")
        print(f"  建议：{d['recommendation']}")
    else:
        print("  ", r.text[:200])
        return 1

    print("\n结论：只发 API Key 的路径（浏览器默认行为）完全可用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
