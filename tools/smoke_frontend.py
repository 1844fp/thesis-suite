"""验证前端资产与写作接口的新增字段（不花钱、用假 Key）。"""

from __future__ import annotations

import json
import re
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
        ('A 你的资料', "四层依据的说明"),
        ('联网检索真实文献作为依据', "开关文案"),
        # 阶段 2 的文件入口现在是"导入知识库"（旧的 gather 导入整块已删除）
        ('id="kbDrop"', "知识库导入拖放区"),
        ('id="kbFiles"', "知识库文件选择框"),
        ('multiple', "支持一次多选"),
        ('accept=".pdf', "声明可接受的扩展名"),
        ('accept=".pdf,.docx,.doc', "知识库接受 PDF/Word/文本"),
        ('扫描版', "扫描版 PDF 的提示"),
        # 新增：从 Word/PDF 直接生成初稿的入口，以及单章重写的输入区
        ('id="wDrop"', "写初稿阶段的文件拖放区"),
        ('id="wFiles"', "写初稿阶段的文件选择框"),
        ('id="wRequirements"', "写作要求输入框"),
        ('id="wFileList"', "导入结果区"),
        # 阶段 3：生成开题报告
        ('id="p-proposal"', "开题报告阶段面板"),
        ('id="propTitle"', "开题报告题目输入"),
        ('id="propSections"', "开题报告小节输入"),
        ('id="propUseKb"', "知识库检索开关"),
        ('id="btnProposalRun"', "逐节生成按钮"),
        ('id="propResultCard"', "开题报告结果卡片"),
        # 连续工作流改造（布局参考 ThesisMind）
        ('id="tabbar"', "阶段导航容器"),
        ('id="themeToggle"', "暗色模式切换按钮"),
        ('thesis-theme', "主题持久化的 localStorage 键"),
        ('/static/theme.css', "引入设计令牌与布局样式"),
    ):
        hit = need in html
        print(f"  [{'OK ' if hit else 'FAIL'}] {why}：{need[:34]}")
        ok = ok and hit

    # 已删除的东西不该再出现（阶段 2 的旧"资料获取"整块）。
    for gone, why in (
        ('id="gDrop"', "旧资料获取拖放区"),
        ('id="gFiles"', "旧资料获取文件框"),
        ('id="btnExtract"', "旧抽取证据按钮"),
        ('id="gatherResultCard"', "旧证据库卡片"),
        ("把文献与证据攒起来", "旧板块标题"),
    ):
        absent = gone not in html
        print(f"  [{'OK ' if absent else 'FAIL'}] 已移除 {why}")
        ok = ok and absent

    # 阶段按钮由 app.js 注入到 #tabbar，所以要检查的是 JS 里的阶段定义。
    # 阶段结构本轮改为 7 个：阶段 2 只做资料准备，成稿独立成阶段 3「生成开题报告」。
    print("\n=== 连续工作流：七个阶段 ===")
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for sid, name in (("topic", "定题目"), ("proposal", "参考文献与知识库"),
                      ("report", "生成开题报告"), ("draft", "写初稿"),
                      ("revise", "修改与评估"), ("integrity", "查重与降 AIGC 率"),
                      ("defense", "答辩")):
        hit = f"id: '{sid}'" in js and name in js
        print(f"  [{'OK ' if hit else 'FAIL'}] 阶段 {sid}（{name}）")
        ok = ok and hit

    print("\n=== 阶段 2 的标题里不再有旧字眼 ===")
    start2 = html.find('id="p-gather"')
    end2 = html.find('id="p-proposal"')
    panel2 = html[start2:end2] if (start2 != -1 and end2 != -1) else ""
    if not panel2:
        print("  [FAIL] 无法定位阶段 2 面板")
        ok = False
    else:
        # 只查**标题**：正文里出现"生成开题报告"是正确的（那是通往阶段 3 的入口），
        # 要求整段正文都不出现这四个字会把正确的文案判成失败。
        # 「下一步：…」这类**指向性标题**是本阶段通往下一阶段的入口，属合理例外。
        heads = re.findall(r"<h[23][^>]*>(.*?)</h[23]>", panel2, re.S)
        own_heads = [h for h in heads if "下一步" not in h]
        joined = " ".join(own_heads)
        for bad, why in (("第一部分", "「第一部分」"), ("第 1 步", "「第 1 步」"),
                         ("第 2 步", "「第 2 步」"), ("开题报告", "「开题报告」")):
            gone = bad not in joined
            print(f"  [{'OK ' if gone else 'FAIL'}] 阶段 2 的自身标题里已无 {why}")
            ok = ok and gone
        pointer = [h for h in heads if "下一步" in h]
        print(f"  [{'OK ' if pointer else 'FAIL'}] 存在指向下一阶段的入口标题：{pointer}")
        ok = ok and bool(pointer)
        print(f"        （标题 {len(heads)} 个：{' / '.join(h.strip()[:18] for h in heads)}）")

    print("\n=== app.js 关键逻辑 ===")
    for need, why in (
        ("use_web:", "把联网开关传给后端"),
        ("renderCoverage", "覆盖度渲染函数"),
        ("citations_invalid", "编造引用计数"),
        ("ungrounded_count", "无依据数字计数"),
        ("headingWithCite", "逐章依据徽标"),
        ("importWritingFiles", "写初稿阶段的文件导入处理"),
        ("/api/writing/import-plan", "调用写作计划导入接口"),
        # 阶段 2/3 新增：知识库导入 + 开题报告生成
        ("importKbFiles", "知识库文件导入处理"),
        ("bindKbDropzone", "知识库拖放绑定"),
        ("/api/kb/upload", "调用知识库上传接口"),
        ("runProposal", "开题报告生成"),
        ("/api/proposal/generate", "调用开题报告生成接口"),
        ("renderProposal", "开题报告渲染"),
        ("exportProposalMd", "开题报告导出 Markdown"),
        ("mdToHtml", "Markdown 转 HTML"),
        ("runSectionRewrite", "单章重写"),
        ("/api/writing/section", "调用单章重写接口"),
        ("bindSectionTools", "章节工具的点击委托"),
        ("importRequirements", "把写作要求带进提示词"),
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
            (".section-tools", "单章重写工具条"),
            (".rewrite-box", "单章重写面板"),
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
