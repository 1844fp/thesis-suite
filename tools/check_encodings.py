"""编码约束检查：Windows 脚本的编码坑，固化成测试。

为什么需要这个
--------------
本轮为了做常驻服务，在 Windows 脚本的编码上连踩三次，每次表现都很迷惑：

1. ``run.cmd`` 原本带中文注释。文件是 UTF-8，而 cmd.exe 在中文 Windows 上按 GBK
   探测解码，把一个 ``rem`` 前缀吃掉了，于是 cmd 把注释当命令执行：
   ``'双击即可启动本机服务' is not recognized as an internal or external command``。
2. ``service.ps1`` 带中文注释且**没有 BOM**。Windows PowerShell 5.1 对没有 BOM 的
   脚本按 ANSI/GBK 解码，中文注释被解坏，直接语法错误：
   ``Missing expression after ','``——报错位置还指向参数块，完全看不出是编码问题。
3. 用编辑工具改完 ``service.ps1`` 之后，BOM **又没了**（写入工具不保留 BOM），
   于是同一个错误再犯一次。

这三条都是"改对了但下一次改动又会踩"的类型，所以固化成检查：改完脚本跑一下这个，
比等到双击启动失败再去猜要省事得多。

规则：
* ``.cmd`` / ``.bat`` -- 必须**纯 ASCII**（cmd.exe 的代码页探测不可靠）。
* ``.ps1``         -- 可以带中文，但**必须有 UTF-8 BOM**（PowerShell 5.1 靠它识别 UTF-8）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BOM = b"\xef\xbb\xbf"


def cmd_files() -> list[Path]:
    return sorted(p for p in ROOT.rglob("*.cmd")) + sorted(p for p in ROOT.rglob("*.bat"))


def ps1_files() -> list[Path]:
    return sorted(ROOT.rglob("*.ps1"))


def main() -> int:
    failures: list[str] = []

    print("=== .cmd / .bat 必须纯 ASCII ===")
    for path in cmd_files():
        data = path.read_bytes()
        bad = [(i, b) for i, b in enumerate(data) if b > 127]
        rel = path.relative_to(ROOT).as_posix()
        if bad:
            failures.append(rel)
            print(f"  [FAIL] {rel}  有 {len(bad)} 个非 ASCII 字节，首个在偏移 {bad[0][0]}")
            # 指出具体是哪一行，省得人去数偏移
            line_no = data[: bad[0][0]].count(b"\n") + 1
            line = data.split(b"\n")[line_no - 1][:80]
            print(f"         第 {line_no} 行：{line!r}")
            print("         cmd.exe 会按代码页探测解码，中文注释可能吃掉 rem 前缀")
        else:
            print(f"  [OK]   {rel}")

    print("\n=== .ps1 允许中文，但必须有 UTF-8 BOM ===")
    for path in ps1_files():
        data = path.read_bytes()
        rel = path.relative_to(ROOT).as_posix()
        has_bom = data.startswith(BOM)
        non_ascii = sum(1 for b in data if b > 127)
        if has_bom:
            print(f"  [OK]   {rel}  BOM=True  非ASCII字节={non_ascii}")
        else:
            failures.append(rel)
            print(f"  [FAIL] {rel}  缺 UTF-8 BOM（非ASCII字节={non_ascii}）")
            if non_ascii:
                print("         PowerShell 5.1 会按 ANSI/GBK 解码，中文注释会导致语法错误")
                print("         修法：把 BOM 加回文件头（EF BB BF）")
            else:
                print("         虽然当前没中文，但加上 BOM 更稳（将来加注释不会再踩）")

    print("\n=== 结果 ===")
    if failures:
        print(f"失败 {len(failures)} 个文件：")
        for item in failures:
            print("  -", item)
        return 1
    print(f"全部通过（{len(cmd_files())} 个 cmd/bat + {len(ps1_files())} 个 ps1）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
