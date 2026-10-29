# -*- coding: utf-8 -*-
"""DOM 引用一致性检查：app.js 里 $('x') 引用的 id 必须在 index.html 里存在。

为什么需要它：这次要删掉一个整块板块（含 49 处引用），而 ``app.js`` 里有大量
**无条件**的 ``$('btnExtract').addEventListener(...)``。一旦某个 id 被删掉而引用还在，
浏览器会在**初始化阶段**抛 TypeError，整个页面变成空白——而这个错误在 Python 侧、
在 HTTP 200 的响应里**完全看不出来**（HTTP 还是 200，只是页面没起来）。

所以把"引用必须存在"变成可执行检查：
  · 扫 index.html 的 id="..."，得到可用集合
  · 扫 app.js 的 $('...') 调用，逐个比对
  · 允许一份显式白名单（确实可选、已写守卫的元素），白名单要写明原因

用法：
    python tools/check_dom_refs.py
    exit 0 = 一致；1 = 有悬空引用
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 允许"被引用但 HTML 里没有"的 id。每一条都要写明为什么可以缺。
ALLOW_MISSING: dict[str, str] = {
    # JS 运行时创建并插入（动态节点），HTML 里当然没有。
    "stageNextStyles": "由 injectNextButtons() 动态 createElement 生成（<style>）",
    "stageNextNote": "由 injectNextButtons() 动态 createElement 生成（提示节点）",
    # 取值后**隔了几行**才判空（不属于"紧邻判空"的自动识别范围，但确实是安全的）。
    "keybox": "LLM.paint() 里 const box = $('keybox'); 之后所有用点都写成 if (box) …",
    # 这个按钮在 index.html 里本就不存在，事件绑定被 && 守卫，
    # 所以 runDuplication() 永远不会被触发（潜在死代码，非本次改动引入）。
    "btnDup": "绑定处 $('btnDup') && …，按钮不存在则不会绑上，函数不会被调用",
}


def html_ids(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8", errors="replace")
    return set(re.findall(r'\bid="([^"]+)"', src))


def _is_guarded(lines: list[str], idx: int, name: str) -> bool:
    """这一处引用是否被存在性守卫保护。

    支持三种真实写法：
      1. 同一行短路：``$('x') && $('x').addEventListener(...)``
      2. 先取值、紧接几行内判空返回：``const box = $('x'); if (!box) return;``
         ——本项目实际用的就是这种，只看同一行会误报。
      3. ``typeof $('x') === 'undefined'`` 之类的显式判断。
    """
    line = lines[idx]
    if re.search(r"""\$\('""" + re.escape(name) + r"""'\)\s*&&""", line):
        return True
    if re.search(r"""typeof\s+\$\(['"]""" + re.escape(name) + r"""['"]\)""", line):
        return True

    # 回头看这一行有没有绑定到变量：const box = $('keybox');
    m = re.search(r"""(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*\$\(\s*['"]""" + re.escape(name) + r"""['"]\s*\)""", line)
    if not m:
        return False
    var = re.escape(m.group(1))
    window = "\n".join(lines[idx: idx + 6])
    return bool(
        # if (warm) warm.addEventListener(...)  —— 真值判断，本项目实际用的就是这种。
        # 早先只认 !warm / warm == null / warm &&，于是把这种写法全判成"无条件引用"，
        # 报了两个假警（btnEmbedWarm / btnEmbedBackfill）。
        # 一个会喊狼来了的检查器比没有检查器更糟：真出问题时没人信它。
        re.search(r"if\s*\(\s*" + var + r"\s*\)", window)
        or re.search(r"if\s*\(\s*!\s*" + var + r"\s*\)", window)
        or re.search(r"if\s*\(\s*" + var + r"\s*==\s*null", window)
        or re.search(var + r"\s*&&", window)
    )


def js_refs(path: Path) -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """返回 (无条件引用, 已守卫引用)。

    注释行与文档里的示例（如 ``$('x')``）一律跳过——它们不参与执行。
    """
    src = path.read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines()
    hard: dict[str, list[int]] = {}
    soft: dict[str, list[int]] = {}
    pattern = re.compile(r"""\$\(\s*['"]([A-Za-z0-9_\-]+)['"]\s*\)|getElementById\(\s*['"]([A-Za-z0-9_\-]+)['"]\s*\)""")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(("*", "//", "/*")):
            continue
        for m in pattern.finditer(line):
            name = m.group(1) or m.group(2)
            if not name:
                continue
            (soft if _is_guarded(lines, i, name) else hard).setdefault(name, []).append(i + 1)
    return hard, soft


def main() -> int:
    ids = html_ids(ROOT / "web" / "index.html")
    # 还有一类 id 不在 index.html 里：**app.js 自己用模板字符串动态生成的**。
    # 例如知识库面板的「启动语义索引」按钮就是运行时才拼出来的。
    # 不把这类算进来，检查器就会持续报假警，而这恰恰是最常见的误报来源。
    js_text = (ROOT / "web" / "app.js").read_text(encoding="utf-8", errors="replace")
    dynamic_ids = set(re.findall(r"""id=\\?["']([A-Za-z0-9_\-]+)\\?["']""", js_text))
    known = ids | dynamic_ids

    hard, soft = js_refs(ROOT / "web" / "app.js")

    missing = {k: v for k, v in hard.items() if k not in known and k not in ALLOW_MISSING}
    missing_soft = {k: v for k, v in soft.items() if k not in known and k not in ALLOW_MISSING}

    print(f"index.html 里 id 共 {len(ids)} 个；app.js 动态生成 {len(dynamic_ids)} 个；"
          f"app.js 引用了 {len(hard) + len(soft)} 个不同的 id")
    print(f"  其中无条件引用 {len(hard)} 个，已守卫引用 {len(soft)} 个")
    if missing_soft:
        print(f"  已守卫但页面里没有的（安全，仅提示）：{', '.join(sorted(missing_soft))}")

    if not missing:
        print("\n== 结论 ==\n  没有悬空的无条件引用。")
        return 0

    print(f"\n== 危险的无条件悬空引用：{len(missing)} 个（会让页面初始化抛异常）==")
    for name, lines in sorted(missing.items()):
        where = "、".join(str(n) for n in lines[:8]) + ("…" if len(lines) > 8 else "")
        print(f"  [危险] $('{name}')  →  页面里没有这个 id（app.js 行：{where}）")
    print("\n请删掉这些引用，或改为带守卫的写法（如 $('x') && $('x').addEventListener(...)）。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
