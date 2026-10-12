"""端到端验证：连续工作流的界面与接口是否都正常。

只验证"能不能打开、结构对不对、接口通不通"，不做视觉断言（我看不到渲染结果）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, r"E:\AI\thesis-suite")

ROOT = Path(r"E:\AI\thesis-suite")


def code_occurrences(path: Path, needle: str) -> list[tuple[int, str]] | None:
    """找出 ``needle`` 出现在**代码**里的位置（注释里的不算）。

    做法是行级注释跟踪：逐行扫描，维护"是否在块注释中"，
    去掉同行注释后再看 needle 是否还在。

    **为什么不用更严谨的写法**：
    * Python 的 ``tokenize`` 不能解析 JavaScript（它会把正则字面量 ``/[&<>"']/g``
      里的 ``/`` 当成除号，进而报"未终止的字符串"——实测在 app.js 第 18 行误报）；
    * 起 Node 子进程也不行：本沙箱对**带管道捕获的子进程**有限制
      （``stdio: 'pipe'`` 会 EPERM，这是环境的既定边界，不是脚本缺陷）。

    行级跟踪对"这行是不是注释掉的"这个判断足够可靠，而本检查要回答的正是这个问题。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    hits: list[tuple[int, str]] = []
    in_block = False
    for i, raw in enumerate(lines, start=1):
        line = raw
        if in_block:
            end = line.find("*/")
            if end < 0:
                continue
            line = line[end + 2:]
            in_block = False
        # 该行的块注释起点
        start = line.find("/*")
        if start >= 0:
            end = line.find("*/", start + 2)
            if end < 0:
                in_block = True
                line = line[:start]
            else:
                line = line[:start] + line[end + 2:]
        # 行注释起点（不做引号内判断：needle 是纯粹的技术标识符，不会出现在字符串里）
        slash = line.find("//")
        if slash >= 0:
            line = line[:slash]
        if needle in line:
            hits.append((i, raw.strip()[:100]))
    return hits


def main() -> int:
    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        ok = ok and cond

    print("=" * 70)
    print("① 静态资源")
    print("=" * 70)
    r = client.get("/")
    html = r.text
    check("首页 200", r.status_code == 200, f"{len(html)} 字节")
    r = client.get("/static/theme.css")
    check("theme.css 200", r.status_code == 200, f"{len(r.text)} 字节")
    r = client.get("/static/app.js")
    check("app.js 200", r.status_code == 200, f"{len(r.text)} 字节")

    print("\n" + "=" * 70)
    print("② 连续工作流的界面结构")
    print("=" * 70)
    check("引入 theme.css", "/static/theme.css" in html)
    check("有主题切换按钮", 'id="themeToggle"' in html)
    check("有防闪白内联主题脚本", "thesis-theme" in html)

    stages = re.findall(r'data-stage="([a-z]+)"', html)
    print(f"  HTML 里的阶段：{stages}")
    check("五个阶段齐全",
          set(stages) >= {"topic", "proposal", "draft", "revise", "defense"},
          str(stages))

    panels = re.findall(r'id="(p-[a-z]+)"', html)
    print(f"  面板容器：{panels}")
    check("四个功能面板都在",
          set(panels) >= {"p-topics", "p-gather", "p-writing", "p-review"},
          str(panels))

    # 旧的四个页签按钮不应该再出现（已经改成阶段）
    check("旧的 .tab 页签已移除", 'class="tab"' not in html)

    print("\n" + "=" * 70)
    print("③ 功能元素未被破坏（抽查各阶段的关键控件）")
    print("=" * 70)
    must = {
        "topic": ["tDiscipline", "btnTopic", "topicList"],
        "proposal": ["gDrop", "gFiles", "gSources", "btnExtract", "btnVerify"],
        "draft": ["wTitle", "wGenre", "wSections", "wUseWeb", "btnDraft", "writeCoverage"],
        "revise": ["rText", "btnCheck", "btnVerifyRefs", "btnOpinion", "reviewDims"],
    }
    for stage, ids in must.items():
        missing = [i for i in ids if f'id="{i}"' not in html]
        check(f"{stage} 阶段的关键控件齐全", not missing, f"缺 {missing}" if missing else "")

    print("\n" + "=" * 70)
    print("④ 接口连通性")
    print("=" * 70)
    for path in ("/api/health", "/api/topics/options", "/api/writing/options",
                 "/api/review/checkers", "/api/gather/status", "/api/capabilities"):
        r = client.get(path)
        check(f"GET {path}", r.status_code == 200, f"HTTP {r.status_code}")

    o = client.get("/api/writing/options").json()
    check("写作依据三层仍在", o.get("sources") == ["user", "web", "model"],
          str(o.get("sources")))
    check("联网检索源仍在",
          [s["id"] for s in o["web_search"]["sources"]] == ["openalex", "crossref", "arxiv"])

    c = client.get("/api/review/checkers").json()
    check("53 个检查器仍在", c.get("total") == 53, f"total={c.get('total')}")

    # 导入接口（multipart）仍然可用
    r = client.post("/api/gather/import",
                    files=[("files", ("测试.md", "# 标题\n\n这是一段正文，用于验证导入接口仍然可用。".encode("utf-8"), "text/markdown"))])
    check("导入接口仍可用", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code == 200:
        d = r.json()
        check("导入返回 source", d.get("source_count", 0) >= 1, str(d.get("source_count")))

    print("\n" + "=" * 70)
    print("⑤ 主题逻辑必须两处同源（这是个容易在后续改动里破掉的约定）")
    print("=" * 70)
    # "跟随系统"的判定同时存在于 index.html（防闪白内联脚本）与 app.js（Theme）。
    # 两处不一致会表现为"首屏闪一下再变色"。所以这里把它当不变量来守。
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    check("app.js 用小时判断 system", "getHours()" in js)
    check("app.js 不再挂 prefers-color-scheme 监听（避免与小时判断冲突）",
          "matchMedia" not in js)
    check("index.html 用小时判断 system", "getHours()" in html)
    # 必须是**代码**里没有 prefers-color-scheme，注释里提到是允许的（而且解释了原因）。
    hits = code_occurrences(ROOT / "web" / "app.js", "prefers-color-scheme")
    check("app.js 代码里不出现 prefers-color-scheme（注释里可以）",
          hits == [], str(hits[:1])[:160])
    check("防闪白脚本在样式表之前",
          html.find("thesis-theme") < html.find("/static/theme.css"))
    check("主题切换按钮存在", 'id="themeToggle"' in html)

    print("\n" + "=" * 70)
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
