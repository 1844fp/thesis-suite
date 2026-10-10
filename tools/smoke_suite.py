"""端到端冒烟测试（不花钱的部分）。

覆盖：
1. 应用能装配起来（说明两个 vendor 树共存的引导层没坏）
2. 题目生成助手**离线模式**能真的出题（不需要任何 API Key）
3. 资料获取助手在没有 Key 时给出可读错误，而不是 500
4. 写作助手的**证据闸门**：没有证据时拒绝生成（这是本站的核心纪律）
5. 评估助手的检查器全部可跑
"""

from __future__ import annotations

import json
import sys

sys.path.insert(0, r"E:\AI\thesis-suite")

from fastapi.testclient import TestClient  # noqa: E402


def line(title: str) -> None:
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


def collect_sse(client: TestClient, path: str, body: dict, headers: dict | None = None) -> list[dict]:
    events: list[dict] = []
    with client.stream("POST", path, json=body, headers=headers or {}) as resp:
        if resp.status_code != 200:
            resp.read()
            raise RuntimeError(f"{path} -> HTTP {resp.status_code}: {resp.text[:400]}")
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
    from suite.api.app import create_app

    client = TestClient(create_app())
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("  [OK]   " if ok else "  [FAIL] ") + name + (f"  {detail}" if detail else ""))
        if not ok:
            failures.append(name)

    line("1. 应用装配 / 路由")
    health = client.get("/api/health").json()
    check("health ok", health.get("ok") is True)
    for pkg, info in (health.get("packages") or {}).items():
        check(f"上游包 {pkg} 可导入", info.get("ok") is True, info.get("error", ""))
    # 服务器密钥封印（BYOK 的结构性保证）：运行态必须能自证这一点。
    check("服务器上不存在可被借用的密钥", health.get("server_key_present") is False,
          f"server_key_present={health.get('server_key_present')}")
    neutralized = health.get("key_fallback_neutralized") or []
    check("回退路径已被中立化", len(neutralized) >= 2, str(neutralized))
    # 用 OpenAPI schema 枚举路由：比遍历 app.routes 可靠（后者在不同 FastAPI 版本里
    # 可能是 Mount / APIRoute 的混合，且带路径参数时 path 会是正则形式）。
    schema = client.get("/api/openapi.json").json()
    routes = set(schema.get("paths") or {})
    for must in ("/api/topics/generate", "/api/gather/extract", "/api/writing/draft",
                 "/api/review/check", "/api/review/verify"):
        check(f"路由存在 {must}", must in routes, f"schema 里共 {len(routes)} 条")
    check("首页路由存在", client.get("/").status_code == 200)
    caps = client.get("/api/capabilities").json()
    check("能力清单 4 个助手", len(caps.get("assistants") or []) == 4)

    line("2. 题目生成 · 离线模式（不需要 Key）")
    opts = client.get("/api/topics/options").json()
    check("服务商预设非空", bool(opts.get("providers")), f"{len(opts.get('providers') or {})} 个")
    check("内置学科非空", bool(opts.get("disciplines")), f"{len(opts.get('disciplines') or [])} 个")
    events = collect_sse(client, "/api/topics/generate",
                         {"discipline": "教育学", "count": 3, "mode": "offline"})
    kinds = [e.get("type") for e in events]
    result = next((e["result"] for e in events if e.get("type") == "result"), None)
    check("离线生成返回 result", result is not None, f"事件类型={kinds}")
    if result:
        n = len(result.get("topics") or [])
        check("题目数量 > 0", n > 0, f"实得 {n} 个，mode={result.get('mode')}")
        print("     首个题目：", (result["topics"][0].get("title") or "")[:60])
        md = client.post("/api/topics/export", json={"result": result})
        check("导出 Markdown", md.status_code == 200 and len(md.text) > 50, f"{len(md.text)} 字")

    line("3. 资料获取 · 无 Key 时的错误可读性")
    resp = client.get("/api/gather/status")
    check("status 可达", resp.status_code == 200, resp.text[:200])
    resp = client.post("/api/gather/extract", json={"sources": [{"text": "测试"}]})
    check("无 Key 抽取被拒绝且可读", resp.status_code == 400 and "Key" in resp.text,
          f"HTTP {resp.status_code} {resp.text[:160]}")

    line("4. 写作助手 · 证据闸门（核心纪律）")
    events = collect_sse(client, "/api/writing/draft",
                         {"title": "测试题目", "sections": ["第1章 绪论"]})
    blocked = any(e.get("stage") == "blocked" for e in events)
    errored = any(e.get("type") == "error" for e in events)
    check("无证据时被拦下", blocked and errored, f"阶段={[e.get('stage') or e.get('type') for e in events]}")
    check("拦下时**没有**调用模型（不该出现 result）",
          not any(e.get("type") == "result" for e in events))

    line("5. 评估助手 · 检查器全量可跑")
    checkers = client.get("/api/review/checkers").json()
    check("检查器已注册", (checkers.get("total") or 0) > 40, f"{checkers.get('total')} 个")
    names = [c["name"] for c in checkers.get("checkers") or []]
    for must in ("check_number_grounding", "check_lit_synthesis", "check_citation_orphans",
                 "check_format_text", "citation_grounding"):
        check(f"含检查器 {must}", must in names)
    sample = ("# 摘要\n本文研究了深度学习。近年来该领域发展迅速，具有重要意义。\n\n"
              "# 第1章 引言\nSmith（2020）提出了模型A。Li（2021）提出了模型B。"
              "本文方法达到 95.3% 准确率，样本量 23008 条[1]。\n\n"
              "# 第2章 研究方法\n本文采用 BERT。\n\n"
              "# 参考文献\n[1] 张三. 某某研究[J]. 学报, 2020.\n[99] 李四. 不存在的研究[J]. 学报, 2021.")
    data = client.post("/api/review/check", json={"text": sample, "title": "冒烟"}).json()
    check("检查全部执行无错", not data.get("failed_checkers"),
          str([f["error"][:60] for f in data.get("failed_checkers") or []])[:200])
    check("检查出问题", (data.get("issue_count") or 0) > 0, f"{data.get('issue_count')} 个")
    check("有评分与建议", data.get("score") is not None and bool(data.get("recommendation")))
    check("有可下载的意见书", len(data.get("markdown") or "") > 100, f"{len(data.get('markdown') or '')} 字")
    print("     评分:", data.get("score"), "| 建议:", data.get("recommendation"))
    print("     严重度:", data.get("counts"))

    line("6. 会话生命周期")
    sess = client.post("/api/session").json()
    check("能建会话", sess.get("ok") and sess["session"]["sid"])
    got = client.get("/api/session/" + sess["session"]["sid"])
    check("能读会话", got.status_code == 200)
    gone = client.get("/api/session/不存在的会话")
    check("不存在的会话返回 404 而非 500", gone.status_code == 404, gone.text[:120])
    check("能删会话", client.delete("/api/session/" + sess["session"]["sid"]).json().get("ok"))

    line("7. 前端静态资源")
    page = client.get("/")
    check("首页可访问", page.status_code == 200 and "题目生成" in page.text, f"{len(page.text)} 字节")
    js = client.get("/static/app.js")
    check("脚本可访问", js.status_code == 200 and len(js.text) > 5000, f"{len(js.text)} 字节")

    line("结果")
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print("  -", item)
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
