"""静态核对：``requirements.txt`` 是否覆盖了源码真正需要的第三方包。

为什么必须核对
--------------
公网容器里依赖**只来自** ``requirements.txt``（pip 安装），而本机开发用的是
vendored 的 ``libs/`` 目录。两者可以长期不一致而没人发现——直到容器构建或启动时
才炸。已经踩过一次：vendored 的 ``anyio 4.15.1`` 需要 ``typing_extensions.sentinel``，
而 ``requirements.txt`` 里没有它。

做法：用 AST 把 ``suite/`` 与 ``vendor/`` 下所有模块的顶层 import 抠出来，
减去标准库与本站自己的包名，剩下的就是"必须由 pip 提供"的第三方包，
再和 ``requirements.txt`` 声明的名字对账。

只做静态分析，不联网、不执行代码。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 本站自己的包名与两套上游的包名——这些由源码树提供，不需要 pip。
INTERNAL = {"suite", "app", "core", "thesisapp", "topiccore"}

#: import 名 -> PyPI 分发名的映射（两者不同的情形）。
#: 每一条都是踩过的：`pptx` 的包名是 `python-pptx`，只写 `pptx` 装不上；
#: `fitz`/`pymupdf` 同属 PyMuPDF；`docx` 来自 python-docx。
DIST_NAME = {
    "docx": "python-docx",
    "pptx": "python-pptx",
    "yaml": "PyYAML",
    "PIL": "Pillow",
    "fitz": "PyMuPDF",
    "pymupdf": "PyMuPDF",
    "sklearn": "scikit-learn",
    "dotenv": "python-dotenv",
    "multipart": "python-multipart",
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
}

#: 有些包在 requirements.txt 里是"由别的包带进来的"，但为了容器稳定仍应显式声明。
#: 这里列出我们**确实直接或间接依赖**、且希望显式钉住的。
KNOWN_TRANSITIVE_OK = set()


def stdlib_names() -> set[str]:
    return set(sys.stdlib_module_names)


def top_imports(path: Path) -> set[str]:
    """抠出一个模块文件里的顶层第三方/内部 import 名字。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except SyntaxError:
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 是相对导入（本站内部），跳过
            if node.level:
                continue
            if node.module:
                found.add(node.module.split(".")[0])
    return found


def declared_requirements() -> tuple[set[str], list[str]]:
    """读 requirements.txt，返回 (规范化后的包名集合, 原始行)。"""
    path = ROOT / "requirements.txt"
    names: set[str] = set()
    raw: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        raw.append(line)
        if not line or line.startswith("#"):
            continue
        name = line.split("[")[0].split("=")[0].split(">")[0].split("<")[0].split("!")[0].strip()
        names.add(name.lower().replace("_", "-"))
    return names, raw


def main() -> int:
    std = stdlib_names()
    files = [
        p for p in (ROOT / "suite").rglob("*.py")
    ] + [
        p for p in (ROOT / "vendor").rglob("*.py")
    ]
    print(f"扫描 {len(files)} 个 Python 文件")

    all_imports: dict[str, set[str]] = {}
    for path in files:
        for name in top_imports(path):
            if name in std or name in INTERNAL:
                continue
            all_imports.setdefault(name, set()).add(path.relative_to(ROOT).as_posix())

    print(f"\n源码里出现的第三方 import 名（{len(all_imports)} 个）：")
    for name in sorted(all_imports):
        where = sorted(all_imports[name])
        sample = where[0] + (f" 等 {len(where)} 处" if len(where) > 1 else "")
        print(f"  {name:22} <- {sample}")

    declared, raw = declared_requirements()
    print(f"\nrequirements.txt 声明（{len(declared)} 个）：{sorted(declared)}")

    print("\n=== 对账 ===")
    missing: list[str] = []
    for name in sorted(all_imports):
        dist = DIST_NAME.get(name, name)
        if dist.lower().replace("_", "-") in declared:
            print(f"  [OK]     {name:22} -> {dist}")
        else:
            # 有些是"运行时可选、缺了也能跑"的（例如只在某个分支 import）。
            # 这里一律报出来，由人来判断，不静默放过。
            missing.append(name)
            print(f"  [MISSING] {name:22} -> {dist}   （源码 import 了，但 requirements.txt 没声明）")

    print("\n=== 声明了但源码里没直接 import 的（可能多余，也可能是运行时必需） ===")
    for dist in sorted(declared):
        used = any(DIST_NAME.get(n, n).lower().replace("_", "-") == dist for n in all_imports)
        if not used:
            print(f"  {dist}")

    if missing:
        print(f"\n结论：有 {len(missing)} 个 import 没有得到 requirements.txt 的保证：{missing}")
        print("容器里 pip 只会装 requirements.txt 里列的包，这些会在构建或启动时炸。")
        return 1

    print("\n结论：requirements.txt 覆盖了源码的全部第三方 import。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
