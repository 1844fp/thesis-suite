# -*- coding: utf-8 -*-
"""一致性检查：suite/modules.py 与 web/app.js 的板块定义必须同名同序。

为什么需要这个脚本：导航有两份副本——
  · 后端 ``suite/modules.py`` 的 ``MODULES``（由 GET /api/modules 返回）
  · 前端 ``web/app.js`` 的 ``STAGES``（前端实际用它渲染导航，不读接口）
同一个概念写两遍，改一处漏一处就会出现"导航显示 A、接口返回 B"的漂移。
本脚本把这条纪律变成可执行检查。

用法：
    python tools/check_stage_sync.py
    exit 0 = 一致；1 = 有漂移
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def backend_modules() -> list[tuple[str, str, str]]:
    """从 suite/modules.py 解析 MODULES（用 ast，不 import，避免拉起整个依赖树）。"""
    src = (ROOT / "suite" / "modules.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in tree.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "MODULES":
            continue
        value = ast.literal_eval(node.value)
        return [(m["id"], str(m["n"]), m["name"]) for m in value]
    raise SystemExit("在 suite/modules.py 里没找到 MODULES")


def frontend_stages() -> list[tuple[str, str, str]]:
    """从 web/app.js 的 STAGES 常量里抽取 (id, n, name)。

    只做正则抽取，不执行 JS——这段结构固定为
    { id: 'x', n: '1', name: '...', desc: '...', panel: '...' },
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const STAGES = \[(.*?)\n\];", src, re.S)
    if not m:
        raise SystemExit("在 web/app.js 里没找到 STAGES 常量")
    out: list[tuple[str, str, str]] = []
    for line in m.group(1).splitlines():
        id_m = re.search(r"id:\s*'([^']+)'", line)
        n_m = re.search(r"n:\s*'([^']+)'", line)
        name_m = re.search(r"name:\s*'([^']+)'", line)
        if id_m and n_m and name_m:
            out.append((id_m.group(1), n_m.group(1), name_m.group(1)))
    return out


def main() -> int:
    be = backend_modules()
    fe = frontend_stages()

    print("== 后端 suite/modules.py ==")
    for sid, n, name in be:
        print(f"  {n}. {sid:<10} {name}")
    print("\n== 前端 web/app.js STAGES ==")
    for sid, n, name in fe:
        print(f"  {n}. {sid:<10} {name}")

    ok = True
    print("\n== 逐项比对 ==")
    if len(be) != len(fe):
        ok = False
        print(f"  [FAIL] 板块数量不同：后端 {len(be)} / 前端 {len(fe)}")
    for a, b in zip(be, fe):
        same = a == b
        if not same:
            ok = False
        print(f"  [{'OK ' if same else 'FAIL'}] 后端 {a}  vs  前端 {b}")

    print("\n== 结论 ==")
    print("  一致，没有漂移。" if ok else "  !! 存在漂移：请同时修改 suite/modules.py 与 web/app.js")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
