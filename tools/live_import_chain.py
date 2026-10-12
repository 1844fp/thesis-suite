"""真实链路：导入 PDF → 抽取证据 → 写作引用（只发 API Key，模拟浏览器）。

这是本次改动真正要保证的东西：作者手上有一份 PDF，拖进来之后能用上。
所以测试用**真实模型**跑完整条链路，并且刻意只发 X-LLM-Key（浏览器默认行为）。
"""

from __future__ import annotations

import io
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


def make_pdf() -> bytes:
    """造一份两页的中文"论文摘录"PDF，含可被引用的具体数据。"""
    import pymupdf

    doc = pymupdf.open()
    pages = [
        [
            "乡村教师职业倦怠调查（摘录）",
            "本研究以某省24个县1836名乡村教师为样本，",
            "采用Maslach职业倦怠量表与自编教学投入问卷。",
            "研究发现：乡村教师职业倦怠总体处于中等偏上水平，",
            "其中情感耗竭维度得分最高。",
            "职业倦怠对教学投入具有显著负向影响，",
            "标准化回归系数为-0.34。",
        ],
        [
            "组织支持的调节作用",
            "组织支持在职业倦怠与教学投入的关系中起显著调节作用，",
            "高组织支持组别的负向效应明显减弱。",
            "研究建议从减轻非教学负担、完善职称晋升通道",
            "与加强心理支持三个方面改善乡村教师职业状态。",
        ],
    ]
    for lines in pages:
        page = doc.new_page()
        y = 90
        for line in lines:
            page.insert_text((60, y), line, fontsize=10)
            y += 20
    data = doc.tobytes()
    doc.close()
    return data


def main() -> int:
    key = pick_key()
    if not key:
        print("没有可用密钥，跳过。")
        return 2

    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())
    headers = {"X-LLM-Key": key}      # 只发 Key
    pdf = make_pdf()

    print("\n=== ① 导入 PDF（不需要 Key）===")
    r = client.post("/api/gather/import",
                    files=[("files", ("乡村教师调查.pdf", pdf, "application/pdf"))])
    print("HTTP", r.status_code)
    if r.status_code != 200:
        print(r.text[:300])
        return 1
    data = r.json()
    f = data["files"][0]
    print(f"  {f['filename']}：{f['kind']}，{f['pages']} 页，{f['chars']} 字，ok={f['ok']}")
    print(f"  按页拆成 {data['source_count']} 段（证据将带页码）")
    for s in data["sources"]:
        print(f"    · {s['title']}")
    sid = ""

    print("\n=== ② 抽取证据（真实模型）===")
    r = client.post("/api/gather/extract", headers=headers,
                    json={"sources": data["sources"]})
    print("HTTP", r.status_code)
    if r.status_code != 200:
        print(r.text[:400])
        return 1
    ex = r.json()
    sid = ex.get("sid", "")
    print(f"  新增 {ex['added']} 条证据，逐字校验丢弃 {ex.get('dropped_passages')} 条片段")
    for item in ex.get("items", [])[:3]:
        print(f"    · {str(item.get('title'))[:44]} | 片段 {len(item.get('passages') or [])} 条")

    print("\n=== ③ 写作（同一会话，引用刚导入的证据）===")
    with client.stream("POST", "/api/writing/draft", headers=headers,
                       json={"sid": sid, "title": "乡村教师职业倦怠对教学投入的影响机制研究",
                             "sections": ["第1章 绪论"], "use_web": False}) as resp:
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
            if ev.get("type") == "stage" and ev.get("stage") == "check":
                print(f"  [{ev['stage']}] {ev.get('message')}")
            if ev.get("type") == "result":
                result = ev
            if ev.get("type") == "error":
                err = ev.get("message", "")

    if result is None:
        print(f"  !! 写作失败：{err[:200]}")
        return 1
    draft = result["draft"]
    cov = result.get("coverage") or {}
    print(f"  生成 {draft['words']} 字；引用 {cov.get('citations_total')} 个"
          f"（用户 {cov.get('citations_user')} / 联网 {cov.get('citations_web')}）")
    print(f"  编造引用 {cov.get('citations_invalid')}；无依据数字 {cov.get('ungrounded_numbers')}")
    print(f"  结论：{cov.get('verdict')}")

    body = "\n".join(s["body"] for s in draft["sections"])
    print(f"\n  正文片段：{body[:420]}")

    # 关键断言：导入的 PDF 里的数字要能被正文用上且判定为有依据
    used_user = cov.get("citations_user", 0)
    print(f"\n  用了多少条来自导入文件的证据：{used_user}")
    good = used_user > 0 and cov.get("citations_invalid", 0) == 0
    print("结论:", "导入 → 抽取 → 写作 全链路打通" if good else "链路有问题，请检查")
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(main())
