"""定向验证：/api/review/check 空跑（不带 text，只给 sid）时，
是否真的把草稿渲染成"每章一个 #"的形式，并让 check_number_grounding 命中。

这一条是本轮最关键的回归：诊断显示链路里这条检查命中恒为 0，
但单独跑检查器又能命中——差别只可能在"送检文本长什么样"。
"""

from __future__ import annotations

import sys

sys.path.insert(0, r"E:\AI\thesis-suite")


def main() -> int:
    from fastapi.testclient import TestClient

    from suite.api.app import create_app
    from suite.api.review import _text_of
    from suite.sessions import STORE

    client = TestClient(create_app())
    session = STORE.create()

    # 造一份"每章一个 #"的草稿 + 联网证据台账
    session.versions.append({
        "title": "乡村教师职业倦怠对教学投入的影响机制研究",
        "sections": [
            {"heading": "第1章 绪论",
             "body": "一项以某省24个县1836名乡村教师为样本的调查显示，回归系数为-0.34，"
                     "覆盖率达到 87.5%[1]。"},
            {"heading": "第2章 文献综述",
             "body": "现有研究共涉及 9999 个观测值，结论并不一致[2]。"},
        ],
    })
    session.meta["writing_evidence"] = {
        "会话证据库": [
            {"text": "本研究以某省 24 个县 1836 名乡村教师为样本，回归系数为 -0.34。",
             "citation": "某调查（用户提供）"},
            {"text": "既有研究结论并不一致。", "citation": "某综述（联网检索）"},
        ]
    }

    print("=== /check 实际使用的文本（_text_of，不带 text 只给 sid）===")
    text = _text_of({}, session.sid)
    for i, line in enumerate(text.split("\n")[:10], 1):
        print(f"  {i}: {line[:70]!r}")

    import app.checks  # noqa: F401
    import app.format.checkers  # noqa: F401
    from app.checks.writing_quality import sections_of_kind

    secs = [t for t, _ in sections_of_kind(
        text, ("introduction", "literature_review", "method", "results",
               "discussion", "conclusion", "body"))]
    print(f"\n切出章节：{secs}")
    ok_sections = len(secs) >= 2

    print("\n=== 走完整 /api/review/check ===")
    r = client.post("/api/review/check", json={"sid": session.sid})
    print("HTTP", r.status_code)
    if r.status_code != 200:
        print(r.text[:300])
        return 1
    data = r.json()
    by = {c["name"]: c["count"] for c in data["by_checker"]}
    ng = by.get("check_number_grounding", 0)
    print(f"check_number_grounding 命中：{ng}")
    print(f"问题总数：{data['issue_count']}")

    # 找出这条检查产生的问题，确认它指出了 87.5 / 9999
    msgs = []
    for dim, items in (data.get("dimensions") or {}).items():
        for it in items:
            if "数字" in str(it.get("message", "")):
                msgs.append(it["message"])
    print("与数字相关的问题：")
    for m in msgs[:4]:
        print("   -", m[:130])

    good = ok_sections and ng > 0 and any("87.5" in m or "9999" in m for m in msgs)
    print("\n结论:", "OK —— 送检文本正确、数字接地真的生效" if good else "仍有问题")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
