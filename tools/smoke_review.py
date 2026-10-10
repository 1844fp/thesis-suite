"""早期冒烟测试：只验证"检查器契约"这一条我最没把握的假设。

不启动服务器，直接用 TestClient 打 /api/review/check，确认：
1. 检查器能全部导入并注册（上游警告过：忘了 import 会静默零检查器）；
2. 每条检查器都能在 ctx 为轻量对象时正常返回；
3. build_review / render_review 能接受我拼出来的 Issue 列表。
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"E:\AI\thesis-suite")

from fastapi.testclient import TestClient  # noqa: E402

SAMPLE = """# 摘要
本文研究了深度学习在文本分类中的应用。近年来，该领域取得了显著进展。随着人工智能技术的快速发展，
越来越多的研究者开始关注这一问题。本文具有重要的理论意义和实践价值。摘要长度需要符合要求，
这里先凑一些字数以保证检查器能够正常工作起来。

## 关键词
深度学习；文本分类；人工智能

# 第1章 引言
近年来，深度学习在自然语言处理领域取得了显著进展[1]。Smith（2020）提出了一个模型。Li（2021）提出了另一个模型。
Wang（2022）又提出了一个模型。本文的研究问题是：如何提升分类准确率？我们的方法达到了 95.3% 的准确率，
样本量为 23008 条，这一结果具有重要意义。

# 第2章 文献综述
深度学习已经被广泛应用。很多学者做了研究。相关研究很多，此处不再赘述。

# 第3章 研究方法
本文采用 BERT 模型。方法很重要。
"""


def main() -> int:
    from suite.api.app import create_app

    client = TestClient(create_app())

    print("--- /api/health ---")
    health = client.get("/api/health").json()
    print("packages:", health.get("packages"))
    print("uptime:", health.get("uptime_seconds"))

    print("\n--- /api/review/checkers ---")
    checkers = client.get("/api/review/checkers").json()
    print("检查器总数:", checkers.get("total"))
    if not checkers.get("total"):
        print("!! 检查器为零，注册没生效")
        return 1
    names = [c["name"] for c in checkers["checkers"]]
    print("前 5 个:", names[:5])
    for must in ("check_number_grounding", "check_lit_synthesis", "check_citation_orphans"):
        print(f"  含 {must}: {must in names}")

    print("\n--- /api/review/check ---")
    resp = client.post("/api/review/check", json={"text": SAMPLE, "title": "冒烟测试稿"})
    print("HTTP", resp.status_code)
    if resp.status_code != 200:
        print(resp.text[:2000])
        return 1
    data = resp.json()
    print("检查器数:", data["checked"], " 失败:", len(data["failed_checkers"]))
    if data["failed_checkers"]:
        for item in data["failed_checkers"]:
            print("   !!", item["name"], "->", item["error"][:200])
    print("问题数:", data["issue_count"])
    print("评分:", data["score"], " 建议:", data["recommendation"])
    print("严重度分布:", data["counts"])
    print("命中最多的问题源:", [(r["name"], r["count"]) for r in data["by_checker"][:5]])
    print("\n意见书前 700 字:\n", data["markdown"][:700])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
