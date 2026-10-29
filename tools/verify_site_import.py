# -*- coding: utf-8 -*-
"""对**运行中的站点**做端到端验证：真 HTTP + 真 .docx 文件。

与 smoke_writing_import.py 的区别：那个用 TestClient（进程内直调），
这个走 127.0.0.1:8848 的 socket，验证的是"访客真的能上传 Word 并拿到计划"。

用法：
    python tools/verify_site_import.py
    python tools/verify_site_import.py --base http://127.0.0.1:8848
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from suite.api import writing as writing_api  # noqa: E402

DOC_TEXT = """乡村教师职业倦怠的影响因素研究

第1章 绪论
本研究关注乡村教师的职业倦怠问题，抽样覆盖 12 个县 486 名教师。研究问题包括：情绪耗竭的主要来源是什么，工作量与支持感各自的解释力有多大。选择乡村样本的原因是既有文献里乡村教师长期被当作城市样本的附属，缺少独立的测量与解释。

第2章 文献综述
既有研究多集中在城市样本，乡村样本不足。国外研究多用 Maslach 三维模型，国内研究则更关注编制与考核压力。两类研究都提示：工作量、社会支持、职业认同是三个稳定的预测变量，但它们在不同区域样本中的相对权重并不一致。

第3章 研究设计
采用问卷法，样本量 486，回收率 93.2%。问卷包含情绪耗竭、去个性化、成就感三个分量表，均采用五点计分。抽样按县分层，兼顾教龄与学段分布，以保证结论不被单一学段主导。

第4章 结果与分析
情绪耗竭与工作量显著相关，相关系数 0.62。社会支持与情绪耗竭呈负相关，相关系数 -0.41。回归模型解释率为 0.38，说明除工作量与社会支持外仍有未纳入的因素，例如学校治理方式与家庭负担。

第5章 结论与展望
工作量是最主要的可干预因素，社会支持具有缓冲作用。后续研究可引入纵向追踪，观察政策调整前后职业倦怠的变化，并补充质性访谈解释统计结果背后的机制。

写作要求：全文不少于 15000 字，需引用真实文献，结论不得超出数据支持范围。
"""


def make_docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8848")
    args = ap.parse_args()
    base = args.base.rstrip("/")

    print(f"== 端到端验证：{base} ==")
    ok = True

    r = requests.get(f"{base}/api/health", timeout=15)
    print(f"\n[健康] HTTP {r.status_code}  {r.text[:160]}")
    ok &= r.status_code == 200

    docx_bytes = make_docx(DOC_TEXT)
    block_chars = getattr(writing_api, "DOC_BLOCK_CHARS", 2500)
    expected_blocks = max(1, len(DOC_TEXT) // block_chars)
    print(f"\n[构造 Word] 真 .docx {len(docx_bytes)} 字节；"
          f"正文约 {len(DOC_TEXT)} 字，按 {block_chars} 字/块 预期 ≥{expected_blocks} 块")

    files = [("files", ("初稿.docx", docx_bytes,
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))]
    r = requests.post(f"{base}/api/writing/import-plan", files=files, timeout=120)
    print(f"\n[导入] POST /api/writing/import-plan -> HTTP {r.status_code}")
    if r.status_code != 200:
        print(f"  body: {r.text[:400]}")
        return 1

    d = r.json()
    f0 = (d.get("files") or [{}])[0]
    plan = d.get("plan") or {}
    material = d.get("material") or {}
    sid = d.get("sid", "")

    print(f"  文件名   : {f0.get('filename')}")
    print(f"  识别类型 : {f0.get('kind')}")
    print(f"  解析字数 : {f0.get('chars')}  段落 {f0.get('paragraphs')}")
    print(f"  解析错误 : {f0.get('error') or '（无）'}")
    print(f"  会话 sid : {sid}")
    print(f"  计划来源 : {plan.get('source')}  （local=无 Key 时程序推导）")
    print(f"  题名     : {plan.get('title')!r}")
    print(f"  章节     : {plan.get('sections')}")
    print(f"  依据块   : {material.get('blocks')} 块 / {material.get('chars')} 字")

    checks = [
        ("不是 Word 解析失败", f0.get("error") in (None, "")),
        ("识别为 Word 文档", str(f0.get("kind", "")).upper() in {"DOCX", "WORD", "DOC"}),
        ("解析出正文", (f0.get("chars") or 0) > 200),
        ("返回了会话 sid", bool(sid)),
        ("题名从文档首行取出", plan.get("title") == "乡村教师职业倦怠的影响因素研究"),
        ("抽出章节结构", len(plan.get("sections") or []) >= 4),
        # 依据块按 writing_api.DOC_BLOCK_CHARS 字一块聚合，块数取决于文档长度，
        # 所以门槛要随文档规模算，不能写死数字。
        ("正文切成依据块", (material.get("blocks") or 0) >= expected_blocks),
    ]
    print()
    for label, good in checks:
        print(f"  [{'OK ' if good else '失败'}] {label}")
        ok &= bool(good)

    # 依据块内容必须真的来自文档（防止"解析成功但正文是空的"）
    blocks = material.get("items") or material.get("blocks_detail") or []
    if isinstance(blocks, list) and blocks:
        joined = json.dumps(blocks, ensure_ascii=False)
        hit = "486" in joined or "0.62" in joined
        print(f"  [{'OK ' if hit else '失败'}] 依据块里含文档中的原始数字（486 / 0.62）")
        ok &= hit

    print(f"\n== 结论 ==\n  {'站点上的 Word 导入可用' if ok else '存在问题，见上方失败项'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
