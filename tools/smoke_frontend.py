"""验证前端资产与写作接口的新增字段（不花钱、用假 Key）。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(r"E:\AI\thesis-suite")
sys.path.insert(0, str(ROOT))


def main() -> int:
    ok = True

    print("=== 前端语法 ===")
    proc = subprocess.run(
        ["node", "--check", str(ROOT / "web" / "app.js")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode == 0:
        print("  [OK]   app.js 语法正确")
    else:
        print("  [FAIL] app.js 语法错误：")
        print("        " + (proc.stderr or "")[:400])
        ok = False

    print("\n=== index.html 关键元素 ===")
    html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    for need, why in (
        ('id="wUseWeb"', "联网检索开关"),
        ('id="writeCoverage"', "依据覆盖度面板"),
        ('id="wTitle"', "题目输入"),
        ('A 你的资料', "三层依据的说明"),
        ('联网检索真实文献作为依据', "开关文案"),
        ('id="gDrop"', "文件拖放区"),
        ('id="gFiles"', "文件选择框"),
        ('multiple', "支持一次多选"),
        ('accept=".pdf', "声明可接受的扩展名"),
        ('扫描版', "扫描版 PDF 的提示"),
        # 连续工作流改造（布局参考 ThesisMind）
        ('id="tabbar"', "阶段导航容器"),
        ('id="themeToggle"', "暗色模式切换按钮"),
        ('thesis-theme', "主题持久化的 localStorage 键"),
        ('/static/theme.css', "引入设计令牌与布局样式"),
    ):
        hit = need in html
        print(f"  [{'OK ' if hit else 'FAIL'}] {why}：{need[:34]}")
        ok = ok and hit

    # 五个阶段按钮由 app.js 注入到 #tabbar，所以要检查的是 JS 里的阶段定义。
    print("\n=== 连续工作流：五个阶段 ===")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for sid, name in (("topic", "定题目"), ("proposal", "开题报告"), ("draft", "写初稿"),
                      ("revise", "修改与评估"), ("defense", "答辩材料")):
        hit = f"id: '{sid}'" in js and name in js
        print(f"  [{'OK ' if hit else 'FAIL'}] 阶段 {sid}（{name}）")
        ok = ok and hit

    print("\n=== app.js 关键逻辑 ===")
    for need, why in (
        ("use_web:", "把联网开关传给后端"),
        ("renderCoverage", "覆盖度渲染函数"),
        ("citations_invalid", "编造引用计数"),
        ("ungrounded_count", "无依据数字计数"),
        ("headingWithCite", "逐章依据徽标"),
        ("importFiles", "文件导入处理"),
        ("bindDropzone", "拖放事件绑定"),
        ("/api/gather/import", "调用导入接口"),
        ("new FormData()", "用 multipart 上传"),
        ("e.dataTransfer", "读取拖入的文件"),
        # 工作流接线
        ("function selectStage", "阶段切换"),
        ("function buildStages", "阶段导航渲染"),
        ("function injectNextButtons", "阶段间的下一步出口"),
        ("function selectTab", "旧调用点兼容层"),
        ("Progress.markDone", "阶段完成状态"),
        ("Theme.bind", "主题初始化"),
    ):
        hit = need in js
        print(f"  [{'OK ' if hit else 'FAIL'}] {why}：{need}")
        ok = ok and hit

    print("\n=== theme.css：设计令牌与布局 ===")
    css_path = ROOT / "web" / "theme.css"
    if not css_path.is_file():
        print("  [FAIL] theme.css 不存在")
        ok = False
    else:
        css = css_path.read_text(encoding="utf-8")
        for need, why in (
            ("--tm-background", "ThesisMind 背景令牌"),
            ("--tm-primary", "ThesisMind 主色令牌"),
            ("--tm-success", "成功色令牌"),
            ("--tm-warning", "警告色令牌"),
            ("--tm-link", "链接色令牌"),
            (".dark", "暗色模式覆盖"),
            ("--sidebar-w: 300px", "侧栏宽度（与参考站一致）"),
            ("grid-template-columns", "两列布局"),
            (".stage", "阶段项样式"),
            (".stage-dot", "阶段状态点"),
            (".stage-next", "下一步按钮容器"),
            ("@media", "响应式"),
        ):
            hit = need in css
            print(f"  [{'OK ' if hit else 'FAIL'}] {why}：{need}")
            ok = ok and hit

    print("\n=== 写作接口：无 Key 时的行为与新字段 ===")
    from fastapi.testclient import TestClient

    from suite.api.app import create_app

    client = TestClient(create_app())

    # 关键回归：这里刻意只发 X-LLM-Key，**不发 base_url / model**——
    # 这正是浏览器默认的行为（服务商由后端按预设补齐）。早先 Chat 直接用原始凭据，
    # 这条路径上 base_url 是空串，请求地址变成 "/chat/completions"，
    # 报错是 "Request URL is missing an 'http://' or 'https://' protocol"，
    # 完全指不到根因（没选服务商）。所以下面要断言"不是那个协议错误"。
    with client.stream("POST", "/api/writing/draft",
                       headers={"X-LLM-Key": "sk-fake-not-real"},
                       json={"title": "乡村教师职业倦怠研究",
                             "sections": ["第1章 绪论"], "use_web": True}) as resp:
        print(f"  HTTP {resp.status_code}")
        stages, err = [], ""
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
            if ev.get("type") == "stage":
                stages.append(ev.get("stage"))
            if ev.get("type") == "error":
                err = ev.get("message", "")

    print(f"  阶段序列：{stages}")
    used_search = "search" in stages
    print(f"  [{'OK ' if used_search else 'FAIL'}] 确实发起了联网检索（B 层）")
    ok = ok and used_search

    bad_protocol = "UnsupportedProtocol" in err or "protocol" in err
    print(f"  [{'OK ' if not bad_protocol else 'FAIL'}] 接口地址解析正常（没出现协议缺失错误）")
    ok = ok and not bad_protocol

    has_key_err = any(m in err for m in ("API Key", "401", "余额", "HTTP"))
    print(f"  [{'OK ' if has_key_err else 'FAIL'}] 假 Key 报错可读：{err[:100]}")
    ok = ok and has_key_err

    print("\n" + "=" * 60)
    print("全部通过" if ok else "有失败项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
