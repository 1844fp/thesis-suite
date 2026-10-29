"""把本机各写作项目的文档收录进 agents/projects/，并加来源标注。

为什么用脚本而不是手工复制
--------------------------
1. 手工复制会**走样**——原文可能被顺手"润色"成我的说法，那就不是收录而是改写了；
2. 需要在每个文件顶部加一段统一的来源说明，说明它是**转载**而非本站原创；
3. 顺带做一次体检：编码是否合法 UTF-8、文件是否为空、大小是否异常。

两种收录形态
------------
* **单文件**：项目只有一份 README，收成 ``projects/<名>.md``；
* **目录包**：项目有多份互相链接的说明文档，收成 ``projects/<名>/``，
  README 与各文档**放在同一层**——这样原文里的相对链接（``[三阶段写作说明](三阶段写作说明.md)``）
  能自然解析，**不必改写正文**。改写链接看起来无害，但会让读者分不清
  "这是原文还是被编辑过"。

原则：**正文一字不改**，只在顶部加一段 HTML 注释形式的来源说明。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(r"E:\AI")
OUT = Path(r"E:\AI\thesis-suite\agents\projects")

#: 单文件收录：(项目目录, 输出文件名, 说明)
SINGLE = [
    ("paper-quality-assistant", "paper-quality-assistant.md",
     "论文质量评估助手 · 三层证据链算幻觉率 + 按意见自动改写"),
    ("cnki-agent", "cnki-agent.md",
     "知网文献采集 Agent · 替你点鼠标下载题录与 PDF"),
    ("aigc-reducer", "aigc-reducer.md",
     "降 AIGC 率工作台 · 11 项风格指标 + 29 条规则 + 保真校验闭环"),
    ("thesis-topic-helper", "thesis-topic-helper.md",
     "题目生成助手 · 学科知识库 + 联网辅助"),
    ("thesis-assistant", "thesis-assistant.md",
     "RAG 知识库服务 · 本地上游服务"),
    ("skill-radar", "skill-radar.md",
     "AI 写作 skill / 工具雷达 · 抓 GitHub、排序、生成离线仪表盘"),
]

#: 目录包收录：(项目目录, 输出子目录名, 说明, 要收的 md 文件名列表或 "all")
BUNDLE = [
    ("thesis-agents", "thesis-agents",
     "论文多智能体框架 · 13 个智能体 · 约 4.5 万行",
     "all"),
    ("codex/writing-workspace", "yanxu",
     "研序 · 七阶段新版（modern/ Node 实现），与旧版 Python 六阶段共存",
     ["README.md", "UPGRADE.md"]),
]

HEADER = """<!--
  本文件是从本机项目 {src} 的 {fname} **原样转载**的。
  转载目的：把散落在各项目的说明集中到一处索引，便于查阅。
  正文未经修改；如果这里的内容与该项目目录里的 {fname} 不一致，
  以项目目录里的为准（那份是随代码演进的）。

  来源：{full}
  收录时间：{when}
-->

> **本文档为转载**，来源 `{full}`（{desc}）。正文未作修改。

---

"""


def _write(src_rel: str, fname: str, target: Path, desc: str, when: str) -> int:
    """写一份转载文件，返回字节数（0 表示源缺失）。"""
    src_path = ROOT / src_rel / fname
    if not src_path.exists():
        print(f"  !! 缺文件：{src_path}")
        return 0
    raw = src_path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("gb18030", errors="replace")
        print(f"  !! {src_rel}/{fname} 不是 UTF-8，已按 GB18030 解码（可能有字符损坏）")
    text = text.lstrip("\ufeff")
    header = HEADER.format(src=src_rel, fname=fname, full=f"E:\\AI\\{src_rel}\\{fname}",
                          desc=desc, when=when)
    target.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 **无 BOM**：Markdown 带 BOM 会让某些渲染器把首个标题吃掉
    target.write_text(header + text, encoding="utf-8", newline="\n")
    return target.stat().st_size


def main() -> int:
    import datetime

    OUT.mkdir(parents=True, exist_ok=True)
    when = datetime.date.today().isoformat()
    rows: list[tuple[str, str, int]] = []

    for src, out_name, desc in SINGLE:
        size = _write(src, "README.md", OUT / out_name, desc, when)
        rows.append((src, out_name, size))

    for src, subdir, desc, files in BUNDLE:
        src_dir = ROOT / src
        if files == "all":
            names = sorted(p.name for p in src_dir.glob("*.md")) if src_dir.exists() else []
        else:
            names = list(files)
        if not names:
            rows.append((src, "（没有 md）", 0))
            continue
        bundle_total = 0
        for name in names:
            out_name = subdir + "/" + name
            # README 格外加一段"本包说明"，其余文件只标来源
            size = _write(src, name, OUT / out_name, f"{desc} · 该项目的说明文档", when)
            bundle_total += size
            rows.append((f"{src}/{name}", out_name, size))
        rows.append((f"{src} 小计", subdir + "/", bundle_total))

    print(f"{'源':46} {'输出':40} {'字节':>8}")
    print("-" * 98)
    total = 0
    for src, out_name, size in rows:
        print(f"{src:46} {out_name:40} {size:>8}")
        total += size
    print("-" * 98)
    print(f"{'合计（含小计重复计数前）':46} {'':40} {total:>8}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
