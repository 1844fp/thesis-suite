# thesis-suite · 本站自己

这是本仓库（本项目）的自我说明。其余 6 个项目的文档在 `projects/` 下，
是从各自 `README.md` 原样转载的；本站这一份**不复制自身 README**——
根目录的 [README.md](../../README.md) 就是它的完整说明，再复制一份只会产生两份会分叉的文档。

## 一句话

把论文写作的七个环节做成一个连续工作流网站：
定题目 → 参考文献与知识库 → 生成开题报告 → 写初稿 → 修改与评估 → 查重与降 AIGC 率 → 答辩。

## 与 thesis-agents 的关系

| | thesis-agents | thesis-suite（本站） |
|---|---|---|
| 组织维度 | **能力**（13 个智能体，能力①②③⑤⑥） | **流程**（7 个板块） |
| 面向 | 单独能力可测、可复用 | 使用者从哪开始、下一步做什么 |
| 界面 | 命令行 / 单能力 | 网站，七板块连续工作流 |
| 状态 | 会话内共享（证据 / 稿件 / 版本链） | 同左，加事件流 |

本站的两套上游代码（`thesis-agents`、`thesis-topic-helper`）**一行未改**，
靠 `vendor/` 下的隔离层布局共存：两棵上游树的根目录都叫 `app`/`core`，
各自有 `config` / `llm` / `server` 同名模块。直接放一起会互相覆盖导入，
所以用 `vendor/thesisapp_stage/` 与 `vendor/topichelper_stage/` 两个 stage 目录分开加载。

## 6 个智能体、7 个板块

见 [../AGENTS.md](../AGENTS.md) 第二节。每个都可单独寻址：

```
POST /api/agents/{name}/{action}
GET  /api/agents/state?sid=...     # 谁在干活、上次干了什么
GET  /api/agents/events?sid=...    # 事件流
```

## 能力边界（如实说明）

本站有三件事**做不到**，且都写在接口返回里而不只是文档里：

1. **查重不是学校查重**——只做文档内部重复 + 参考文献可核验性，
   检测不了与已发表文献、网络内容、往届论文的重复；
2. **AI 味指数不是商用检测器输出**——是本地风格启发式代理，数值不可互相换算；
3. **本地 RAG 是关键词检索**（jieba + SQLite FTS5），不是语义检索：
   查「教师心理状态」不会命中「职业倦怠」，除非两者共有词。

## 技术要点

* 后端 FastAPI + 原生前端，**无构建步骤**；
* 模型凭据两种形态：访客自带（BYOK，默认）或本站配置（`SUITE_SERVER_LLM_KEY`）；
* 依赖全部 vendored，容器里 `pip install -r requirements.txt` 即可；
* 测试脚本共 **30 个**在 `tools/`（24 个 `smoke_*.py` + 6 个 `check_*.py`）。
  **最近一次全量回归 30/30 通过**——前提是本站与降 AIGC 服务都在运行
  （`check_running.py` 的用途就是验收运行中的站点）。
  早先这里写的是"只跑了 17 套"；补齐后确认全部通过，包括此前漏跑的
  参考文献推荐、PaperQA2、写作导入、审查链路等。

完整说明见根目录 [README.md](../../README.md)。
