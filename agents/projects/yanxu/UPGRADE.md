<!--
  本文件是从本机项目 codex/writing-workspace 的 UPGRADE.md **原样转载**的。
  转载目的：把散落在各项目的说明集中到一处索引，便于查阅。
  正文未经修改；如果这里的内容与该项目目录里的 UPGRADE.md 不一致，
  以项目目录里的为准（那份是随代码演进的）。

  来源：E:\AI\codex/writing-workspace\UPGRADE.md
  收录时间：2026-10-29
-->

> **本文档为转载**，来源 `E:\AI\codex/writing-workspace\UPGRADE.md`（研序 · 七阶段新版（modern/ Node 实现），与旧版 Python 六阶段共存 · 该项目的说明文档）。正文未作修改。

---

# 七阶段新版与旧版共存

新版私有网站：https://yanxu-writing-1844fp.loftyhake33.chatgpt.site 。仅限当前账号访问，未公开分享。

新版：研序，源码在 modern/，与根目录 Python 程序并存。旧版完整快照保留在 legacy-writing-2026-10-21 分支，基于提交 7e8763ecf25ae71a864abfbc2cdd22fc708a0d9c。

## 使用新版

- Windows：运行根目录的 启动新版工作台.cmd。
- 跨平台：进入 modern/，运行 node server.mjs，打开 http://127.0.0.1:8799 。
- Node.js 22+，无第三方运行依赖。模型生成需要用户自己的模型 API Key。
- 新版项目数据默认写在 modern/data/；请定期导出备份。
- 私有在线网站由 Sites 单独托管，同一核心代码运行在 Worker + R2。在线与本地项目存储彼此独立，通过项目备份迁移。
- Docker：docker build -f Dockerfile.modern -t yanxu-writing . ，运行时挂载 /app/data。此镜像没有账户系统，面向本机或已有认证网关的私有环境。

## 旧版入口

根目录原有 run.cmd、启动网站.cmd、Dockerfile、suite/、web/、vendor/ 保留原内容和原调用路径。新版通过 /api/v2 与 src/ 工作流运行，不覆盖旧版 /api/writing 等接口。

旧版的 PDF/OCR、复杂 Word 格式处理和其他已存在工具继续可用。新版 DOCX 导入提取正文，公式、图片、复杂表格仍需回原件核对；PDF/扫描件可先用旧版解析，再导入文本。

在线新版的“旧版写作助手”入口默认指向本说明。若已有运行中的旧版地址，可为新版配置 LEGACY_ORIGIN；本机新版默认指向旧版的7860端口。保留源码不意味着旧版Python服务已经部署在Sites上。

## 本次交付

1. 七阶段项目工作流、持久化、稳定证据编号、证据快照、检查点与版本恢复。
2. 模型密钥只在页面内存中使用；资料检索与阶段执行有独立工具入口。
3. 逐句引用和数字检查、结果章缺口处理、结论读取正文、当前版本重评估。
4. 本地内部重复、表达改写保护、外部报告版本绑定。
5. 答辩讲稿、提纲、问答与本页模拟练习；Word、PowerPoint、Markdown和离线演示稿导出。
6. 七份阶段技能规范、18项假模型/假检索测试及Node 22/24 CI。

## 验证和后续边界

规则检查不构成完整事实证实或同行评议；模型生成结果需要作者核对。当前不自动执行真实统计研究、不伪造学校查重/AIGC结果。真实模型成稿质量及各学校模板的适配还需要用具体论文做验收。Word/PPTX导出已做结构检查，复杂页面仍需在办公软件中核对排版。
