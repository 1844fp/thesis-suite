<!--
  本文件是从本机项目 thesis-assistant 的 README.md **原样转载**的。
  转载目的：把散落在各项目的说明集中到一处索引，便于查阅。
  正文未经修改；如果这里的内容与该项目目录里的 README.md 不一致，
  以项目目录里的为准（那份是随代码演进的）。

  来源：E:\AI\thesis-assistant\README.md
  收录时间：2026-10-29
-->

> **本文档为转载**，来源 `E:\AI\thesis-assistant\README.md`（RAG 知识库服务 · 本地上游服务）。正文未作修改。

---

# 论文助手 · 硕士学位论文 RAG 知识库

> **一句话**：针对中文学位论文结构优化的本地 RAG 系统，引用可精确到章节 + 页码。
>
> **技术栈**：Python · 本地向量检索 · BM25（jieba + 中文二元组）· RRF 融合 · LLM 重排
>
> **规模**：约 7,600 行 · 四级混合检索引擎
>
> **核心设计**：中文字符二元组补 jieba 术语切分 · 检索默认排除参考文献避免污染 ·
> 跨文档对比用统一维度独立抽取后生成矩阵（而非拼接摘要）· 无 Key 时可降级为纯本地检索

**目录**：[快速开始](#快速开始) · [检索设计](#2-检索设计) · [常见问题](#常见问题)

---

面向中文学位论文的本地知识库。三个核心功能：**单篇论文总结**、**文档检索**、**跨文档对比**，外加一个基于全库的**问答**入口。

针对中文学位论文做了专门处理，不是通用文档问答的套壳：
- 按「第X章 / X.Y / 参考文献 / 摘要」重建章节层级，引用能精确到章节 + 页码
- 摘要与参考文献自动分类，检索默认排除参考文献（避免引用列表污染语义结果）
- 混合检索：本地向量 + BM25（jieba 分词 + 中文字符二元组）+ RRF 融合 + LLM 重排
- 跨文档对比用统一维度逐篇独立抽取，再生成对比矩阵，而不是把几段摘要堆在一起

---

## 快速开始

```powershell
cd E:\AI\thesis-assistant

# 1. 安装依赖（装到项目内 libs\，不污染全局 Python）
.\install.cmd

# 2. 启动，然后在界面里接入大模型
.\run.cmd
```

浏览器打开 http://127.0.0.1:8760 ，点侧边栏的 **⚙** 打开「模型设置」，
选服务商 → 粘贴 API Key → 点「测试连接」确认通了 → 「保存并生效」。
**不需要改文件，也不需要重启服务。**

```powershell
# 3. 放入论文，然后入库
#    把 PDF / DOCX / TXT / MD 拷进 inbox\
.\run.cmd ingest
```

> 想改配置文件也可以：`copy .env.example .env` 后填 `LLM_API_KEY`，
> 重启生效。界面里保存的设置会写回这个 `.env`。

> **为什么用 `.cmd` 而不是 `.ps1`？** 很多 Windows 机器默认禁止执行 PowerShell
> 脚本（报 `running scripts is disabled on this system`）。`.cmd` 包装器用
> `-ExecutionPolicy Bypass` 只对本次调用放宽策略，不改系统设置、不需要管理员
> 权限。如果你已经放开了执行策略，直接 `.\run.ps1` 也一样。

> 没有 API Key 也能先用起来：入库和检索完全可用（本地向量 + BM25），
> 只有总结 / 对比 / 问答会提示需要配置 Key。

### 想先看效果？

```powershell
.\run.cmd                      # 先启动
python tools\make_demo.py      # 另外开一个终端：生成 3 篇演示论文到 inbox\
.\run.cmd ingest               # 入库
```

演示语料是 3 篇同领域（绿色低碳）但方法不同的仿真硕士论文：
固定效应模型 / 双重差分 / SBM-DEA + 空间杜宾，正好用来试跨文档对比。

---

## 接入大模型：界面设置入口

### 用 DeepSeek（默认配置）

项目出厂就是 DeepSeek + 本地向量化，`LLM_BASE_URL` / `LLM_MODEL` / `EMBED_PROVIDER`
都已设好，**你只需要填一个 API Key**：

1. 打开 http://127.0.0.1:8760
2. 如果还没配 Key，左侧会有一条橙色提示，点 **「配置 DeepSeek 模型」**
3. 服务商已经是 `DeepSeek`，Base URL 和模型名也已填好
4. 在 **API Key** 里粘贴你的 key → 点 **测试连接** → 出现绿色「连接成功」→
   点 **保存并生效**

保存后总结 / 对比 / 问答立即可用，**不用重启**。

> 想先确认网络通不通？运行 `python tools\probe_deepseek.py`。
> 它会逐层检查 DNS → TCP → TLS → HTTP，并用一个无效 Key 发请求：
> 返回 `401` 就说明链路完全正常，只差有效 Key。

### 界面设置详解

侧边栏 **⚙** 按钮打开「模型设置」。这是接入模型的推荐方式——不用编辑文件、
不用重启进程，保存后下一次请求立即生效。

**生成模型**（总结 / 对比 / 问答必需）
- 服务商下拉：DeepSeek / 智谱 / 通义 / Kimi / OpenAI / 本地 Ollama / 自定义
  选中后自动填好 Base URL 和默认模型，模型输入框带候选列表
- API Key：输入框留空表示**保留已保存的密钥**，不会误清空；「显示」可切换明文
- **测试连接**：发一条最小请求验证 Key、地址、模型名是否都对，返回耗时和模型回显。
  测试用的是表单里当前的值，所以可以**先测再存**
- temperature / max_tokens 可调

**向量模型**（可选，默认本地免费）
- 选 `local` 时相关输入行自动隐藏
- 切换到云端服务商后填 Key，同样可「测试连接」，返回实际维度
- **注意：DeepSeek 没有 embedding 接口**，向量化必须单独选服务商

**切换向量模型时会提示重建索引**：旧向量与新模型不兼容，界面会问你是否立即重建
（调 `POST /api/reindex`），确认后自动完成。

### 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/config` | 当前配置，**密钥已脱敏**（返回 `api_key_masked` 与 `has_api_key`） |
| GET | `/api/config/presets` | 服务商预设，供界面自动填充 |
| POST | `/api/config` | 保存到 `.env` 并热重载所有客户端 |
| POST | `/api/config/test` | 连通性测试，`kind` 为 `llm` 或 `embed` |
| POST | `/api/reindex` | 重建全部向量 |

### 几个设计选择

- **保存即生效，不重启**：单个连接对象被就地重建（`_rebuild_clients`）。
  注意 `Retriever` / `Summarizer` / `Comparator` 持有旧客户端引用，
  所以它们必须一起重建，只换 `llm` 是不够的。
- **密钥永不回显**：`GET /api/config` 只返回 `sk-1••••••••abcd` 形式的脱敏串，
  界面留空即表示不修改。
- **`.env` 逐行改写**：只更新目标 key，保留模板里的全部注释和顺序，
  新 key 追加到文件末尾。
- **`api_key: null` 与 `""` 语义不同**：`null` = 保留原值，`""` = 清除。
  界面始终发 `null`，除非用户真的在输入框里打了字。

> **安全提示**：密钥以明文保存在本机 `.env` 中。服务只监听 `127.0.0.1`，
> 不要把这个文件提交到版本库或分享出去。

---

## 文件名规范（题目+作者）

入库时会把 `inbox\` 里的 PDF **自动重命名为统一的 `题目_作者.pdf`**，让库内命名一致。

**解析规则**——自动识别这些分隔符，不需要配置：

| 写法 | 结果 |
|---|---|
| `题目_作者.pdf` | ✅ |
| `题目+作者.pdf` | ✅ |
| `题目-作者.pdf` | ✅ |
| `题目 作者.pdf`（空格 / 全角空格） | ✅ |
| `题目\|作者.pdf` | ✅ |
| `作者_年份_题目.pdf`（旧写法） | ✅ 兼容 |
| `【期刊】题目_作者_2023.pdf` | ✅ 自动剔除装饰前缀和年份 |
| `题目_作者(1).pdf`、`题目_作者-副本.pdf` | ✅ 自动剔除复制标记 |

分隔符换成 `+`（字面的「题目+作者」）改 `.env` 里 `RENAME_SEPARATOR=+`。

### 标题和作者分别怎么取

两个字段的可靠性不一样，所以用**不同**的规则，而不是统一优先级：

**标题** —— 优先用 PDF 正文的标题页，因为**用户起的文件名常常被截断**
（`——` 后面的副标题是重灾区）。只有当正文标题是期刊页眉噪音时才改用文件名。

**作者** —— 优先用文件名。正文首页的作者抽取很容易抓到页眉（`CNKI`）或导师，
而 `题目_作者` 是你手工命名的，更可信。

实测这批 20 篇语料：**15 篇的正文首页是页眉噪音**（`第40卷第2期`、
`收稿日期: 2023-02-28`、`CNKI`、`简介`），只有靠文件名才能拿到正确元数据；
另外 5 篇正文标题可用，其中 3 篇比文件名更完整（带副标题）。

> 如果反过来"文件名永远优先"，会把
> `企业数据资产化：会计确认与价值评估_罗玫.pdf` 改名成 `CNKI_佚名.pdf`。

### 安全边界

- **只重命名 `inbox\` 里的文件**。`ingest D:\papers\xx.pdf` 这种从别处指定的文件
  不会被改名——库外的文件不归它管。
- **先入库成功，再重命名**。中途失败不会留下"改了名但没入库"的文件。
- **同名冲突**自动加序号：`题目_作者 (2).pdf`。
- **信息不足时不乱改**：文件名里没有作者、正文又是页眉噪音时，保留原名不动。
- 重命名后索引里的 `source_path` 会同步更新。
- 关掉这个功能：`.env` 里设 `RENAME_ON_INGEST=false`。

---

## 调用知识库

三种调用方式，按场景选：

| 方式 | 适合 | 入口 |
|---|---|---|
| Web 界面 | 日常使用、看引用原文 | http://127.0.0.1:8760 |
| 命令行 | 批量处理、脚本里嵌一条命令 | `.\run.cmd <子命令>` |
| Python 客户端 / HTTP API | 集成进你自己的程序 | `client.py` / `http://127.0.0.1:8760/api/*` |

### Python 客户端（推荐）

`client.py` 是零依赖的（只用标准库），可以直接复制进你自己的项目。

```python
from client import ThesisRAG

rag = ThesisRAG()                       # 默认 http://127.0.0.1:8760

# 1. 检索片段 —— 不调用大模型，不花钱，可放心放进循环
for h in rag.search("双重差分 平行趋势检验", k=5, rerank=False):
    print(h["citation"], h["score"])    # 引用含作者/年份/标题/章节/页码
    print(h["text"])

# 2. 问答 —— 答案带 [n] 角标，sources 里是对应原文
ans = rag.ask("这几篇论文的数据来源有什么区别？")
print(ans.text)
print(ans.cite(1)["text"])              # 取回角标 [1] 的原文

# 3. 单篇总结 —— 不加 force 时优先读缓存（不花钱）
s = rag.summarize(rag.resolve("王雪"))   # 可以用作者名、标题片段或 doc_id
print(s.text)                           # s.cached / s.stale / s.model

# 4. 跨文档对比 —— 返回矩阵 + 综述
cmp = rag.compare([d["doc_id"] for d in rag.docs()[:3]], focus="研究方法与数据")
print(cmp.matrix["dimensions"])         # 7 个维度
print(cmp.cell("刘敏", "数据与样本"))    # 按作者取单元格
print(cmp.narrative)                    # 对比综述
```

流式用法（做进度条或边生成边处理）：

```python
for ev in rag.ask_stream("三篇论文的方法差异？"):
    if ev["type"] == "sources":
        show_sources(ev["sources"])
    elif ev["type"] == "delta":
        sys.stdout.write(ev["text"])     # 增量文本
```

### HTTP 接口清单

| 方法 | 路径 | 请求 | 返回 |
|---|---|---|---|
| GET | `/api/status` | — | 配置 + 索引统计 |
| GET | `/api/docs` | — | 论文列表 |
| GET | `/api/docs/{id}/outline` | — | 章节层级 |
| GET | `/api/docs/{id}/summary` | — | 已缓存的摘要 |
| DELETE | `/api/docs/{id}` | — | 删除论文 |
| POST | `/api/search` | `{query, k, doc_ids?, include_refs?, rerank?}` | 命中片段 + 分数 |
| POST | `/api/ask` | `{question, k?, doc_ids?, include_refs?}` | **SSE** |
| POST | `/api/summarize` | `{doc_id, force?}` | **SSE** |
| POST | `/api/compare` | `{doc_ids, focus?}` | **SSE** |
| POST | `/api/ingest` | `{force?}` | 启动入库（异步） |
| GET | `/api/ingest/status` | — | 入库进度 |
| POST | `/api/reindex` | `{}` | 重建向量索引 |
| GET | `/api/config` | — | 当前配置（密钥脱敏） |
| POST | `/api/config` | 见「模型设置」 | 保存并热生效 |
| POST | `/api/config/test` | `{kind, ...}` | 连通性测试 |

**SSE 流式约定**：`ask` / `summarize` / `compare` 返回
`text/event-stream`，每条为 `data: {json}\n\n`，事件类型：

- `progress` — `{message, ratio}` 进度提示
- `sources` — 仅 ask，引用片段列表
- `matrix` — 仅 compare，对比矩阵
- `delta` — `{text}` 增量文本，拼接即完整答案
- `done` — 结束
- `error` — `{message}` 出错

> 解析 SSE 时请用**增量解码器**（Python 用
> `codecs.getincrementaldecoder("utf-8")()`，JS 用
> `TextDecoder(..., {stream: true})`）。一个汉字的 UTF-8 字节可能跨越两个
> 网络分块，逐块 `decode(errors="replace")` 会把它损坏成替换字符。
> `client.py` 已经处理好这一点。

### 命令行

```powershell
.\run.cmd search "空间溢出" -k 5          # 纯检索
.\run.cmd ask "三篇论文的方法差异？"        # 问答
.\run.cmd summarize 王雪                   # 支持作者名
.\run.cmd compare 张伟 刘敏 --focus "数据与样本"
.\run.cmd ingest                           # 入库
.\run.cmd reindex                          # 重建索引
```

`client.py` 自己也能当命令行用（和服务端 CLI 等价，但走 HTTP）：

```powershell
python client.py search "双重差分" -k 3
python client.py ask "数据来源差异？"
python client.py --base http://127.0.0.1:8760 stats
```

### 几个使用要点

- **`search()` 是最省的调用**：默认不开重排就不碰大模型，可以高频调用、
  批量跑。需要更准时再加 `rerank=True`。
- **`doc_ids` 可以限定范围**：传一个 doc_id 列表即可只在选中的论文里检索，
  跨文档对比、单篇深挖都靠它。
- **论文指代支持作者名**：`resolve("王雪")`、`summarize("2022")`、
  `cell("刘敏", "数据与方法")` 都能用。匹配到多篇时会报错并列出候选，
  不会替你猜。
- **`summarize()` 默认读缓存**：不加 `force=True` 不会重复消耗额度。
  缓存结果若由别的模型生成，`s.stale` 会是 `True`。
- **服务只监听 `127.0.0.1`**：仅本机可访问。要让局域网其他机器调用，
  把 `.env` 的 `HOST` 改成 `0.0.0.0`（并自行考虑访问控制，接口没有鉴权）。

---

## 深度问答：PaperQA2（可选组件）

「问答」页签有两种模式：

| 模式 | 引擎 | 特点 |
|---|---|---|
| **快速** | 本项目管线 | 本地混合检索 + DeepSeek，约 2 秒，成本≈0 |
| **深度** | PaperQA2 智能体 | 逐片段按问题做上下文摘要再重排，擅长对比文献分歧，10-40 秒，约 ¥0.03/次 |

安装：

```powershell
.\install-paperqa.cmd      # 装到独立的 libs-pqa\，不影响主程序
.\run.cmd                  # 重启服务
```

首次提问会建立 PaperQA2 索引（20 篇约 2-3 分钟），之后复用缓存，单次约 10-25 秒。

### 它补上了什么

实测同一个问题，深度模式会主动梳理分歧——这是快速模式做不到的：

> 数据资产的会计确认与计量存在多种代表性观点。在确认层面，**一种观点**主张将数据资产确认为无形资产…
> **另一种观点**主张单独设置"数据资产"科目…**还有观点**对单独开发销售的数据资产设置"数据资产"或"存货"科目…

并且会自动解析出完整文献信息（期刊、卷期、页码、CNKI DOI），即使这些论文没有 DOI 注册记录：

> 张俊瑞，危雁麟，宋晓悦. "企业数据资产的会计处理及信息列报研究." *会计与经济研究*, vol. 34, no. 3, May 2020, pp. 3–15. *CNKI*, doi:10.16314/j.cnki.31-2074/f.2020.03.001.

### 它不是替代品

PaperQA2 **只接管问答这一条路径**。解析、分块、文件名规范、检索、单篇总结、跨文档对比
仍然由本项目负责，原因是：

- 它只做 Q&A，没有跨文档对比矩阵
- 它的 PDF 读取器（pypdf）解析不了 CNKI 论文，必须换 `paper-qa-pymupdf`
- 它的文档有效性检查会**拒绝所有中文长文**（见下）

### 接入时必须绕过的三个坑

**1. 中文长文会被判为"不是文本文档"**

PaperQA2 用 `maybe_is_text()` 做校验，只接受每字符熵落在 `2.5 ~ 8.0` 区间的文本。
这个上限是按字母文字校准的——英文只有 25 个相异字符，熵上界 `log2(25)=4.7`；
中文有 1210 个相异字符，上界 `log2(1210)=10.24`，实测：

| 样本长度 | 中文熵 | 判定 | 英文熵 | 判定 |
|---|---|---|---|---|
| 5,000 字 | 7.975 | ✅ | 4.079 | ✅ |
| 10,000 字 | 8.182 | ❌ | 4.077 | ✅ |
| 40,000 字 | 8.283 | ❌ | 4.077 | ✅ |

它取样前 5 个片段（约 25000 字），所以**任何超过约 5000 字的中文论文都会被拒绝**。
必须设 `parsing.disable_doc_valid_check=True`。

**2. 默认解析器读不了 CNKI 的 PDF**

pypdf 在这些文件上报 `Multiple definitions in dictionary ... /MediaBox` 且抽不出文本。
装 `paper-qa-pymupdf` 后 PaperQA2 会优先使用 PyMuPDF 版本（它的加载顺序就是先试 pymupdf）。

**3. 智能体路径在当前依赖组合下是坏的**

`fhlmi 1.0.7` 把自己的 litellm 垫片定义成只收关键字参数：

```python
async def _acompletion(**kw): ...          # lmi/llms.py
```

而 `aviary` 的 `ToolSelector` 把模型名作为**第一个位置参数**绑定进去：

```python
self._bound_acompletion = partial(acompletion, model_name)   # aviary/tools/utils.py
```

结果任何走 agent 的调用都抛 `TypeError: _acompletion() takes 0 positional arguments
but 1 was given`。绕开办法是走 `Docs.aquery` 手动 API，它不经过工具选择器——
`app/paperqa_runner.py` 就是这么做的。

### 隔离方式

PaperQA2 装在 `libs-pqa\`，以**子进程**方式调用（`tools/pqa_bridge.py`），
用 JSONL 通过 stdout 通信。因为它的 numpy/pydantic 版本与主程序不同，
同进程加载会让其中一方静默拿到错误版本。

embedding 用 `BAAI/bge-small-zh-v1.5`（本地，512 维）。这台机器访问不了
huggingface.co，所以 `HF_ENDPOINT` 指向 `hf-mirror.com`。

---

## 三个功能怎么用

### 论文总结
左侧点选一篇论文 → 「论文总结」页签 → 生成。

长论文会自动分批精读（map）再汇总（reduce），输出固定八节：
基本信息 / 研究问题与背景 / 方法与技术路线 / 数据与实验设计 / 主要结论 /
创新点与贡献 / 局限与未来工作 / 关键术语。每个论断后带「（第N页）」依据。

结果会缓存进库，第二次打开直接读缓存，勾选「忽略缓存」可重新生成。

> 缓存只按文档存。如果缓存的摘要是由**别的模型**生成的（例如你换了模型），
> 界面会用黄色提示标出「此摘要由 X 生成，当前模型为 Y」，不会假装它是最新的。
> 系统不会自动重新生成——那会在你不知情的情况下花钱。
> 想批量清理这类过期摘要：`python tools\clear_stale_summaries.py`。

### 文档检索
「文档检索」页签输入关键词或整句话。左侧勾选论文可把检索范围限定在选中的几篇内。

返回的每条结果标注了命中来源（稠密 / 稀疏候选数）以及是否经过 LLM 重排。
点击「显示原文」展开完整片段。

### 跨文档对比
左侧勾选 **2 篇以上** → 「跨文档对比」页签 → 可填对比焦点（留空则默认对比
方法 / 数据 / 结论 / 创新点 / 局限）。

输出两部分：
1. **对比矩阵** —— 每篇论文按 *同一套* 7 个维度独立抽取，横向可读
2. **对比综述** —— 总体概览 / 方法路径差异 / 数据可比性 / 共识与冲突 /
   创新点分布 / 研究空白与选题建议

综述会明确指出哪些论文之间**不可直接比较**（例如企业面板 vs 省级面板）。

### 问答
基于全库或选中论文回答问题，答案中的 `[n]` 角标对应下方片段，点击可跳转。

---

## 命令行

```powershell
.\run.cmd ingest                        # 扫描 inbox\ 入库
.\run.cmd ingest D:\papers\xx.pdf       # 指定文件
.\run.cmd ingest --force                # 强制重建已入库文档

.\run.cmd docs                          # 列出库内论文
.\run.cmd stats                         # 查看配置与索引状态
.\run.cmd search "双重差分 平行趋势检验" -k 5
.\run.cmd search "空间溢出" --doc ea6eda0b0bcddc43 --no-rerank
.\run.cmd ask "这几篇论文的数据来源有什么区别？"
.\run.cmd summarize 王雪                 # doc_id 或标题片段
.\run.cmd summarize 王雪 --force
.\run.cmd compare 张伟 刘敏 王雪 --focus "研究方法与数据"
```

---

## 模型配置参考（`.env`）

界面的「模型设置」会写这些键。也可以手工编辑 `.env`（需重启生效）。

### 生成模型（必须）

任何 OpenAI 兼容接口都行：

| 服务 | LLM_BASE_URL | LLM_MODEL |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| 智谱 | `https://open.bigmodel.cn/api/paas/v4` | `glm-4-plus` |
| 通义 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-32k` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:14b` |

### 向量模型（可选，默认本地）

**注意：DeepSeek 没有 embedding 接口**，所以向量化不能复用上面那把 key。

默认 `EMBED_PROVIDER=local`，用内置的字符 n-gram 哈希向量化（带语料 IDF），
零依赖、零成本、离线可用。配合 BM25 混合检索，定位原文已经够用。

想要更好的语义召回，把 `EMBED_PROVIDER` 换成下面之一并填 `EMBED_API_KEY`：

| 提供商 | 模型 | 说明 |
|---|---|---|
| `siliconflow` | `BAAI/bge-m3` | 中文效果很好，价格极低（推荐） |
| `zhipu` | `embedding-3` | 智谱 |
| `dashscope` | `text-embedding-v3` | 阿里百炼 |
| `openai` | `text-embedding-3-small` | OpenAI |

> 切换向量模型后必须重建索引：删除 `data\index\` 后重新 `ingest`，
> 或在 Web 界面调 `POST /api/reindex`。
> 远程接口调用失败时会自动降级回本地向量化，界面上会标出「已降级」和原因。

---

## 项目结构

```
thesis-assistant\
├── client.py          零依赖 Python 客户端（可复制进你自己的项目）
├── app\
│   ├── paperqa_runner.py  PaperQA2 子进程桥接（可选组件）
│   ├── config.py       .env 读取与配置预设
│   ├── parsing.py      PDF / DOCX / TXT / MD 解析与元数据抽取
│   ├── chunking.py     中文学术分块：章节层级识别、摘要/参考文献分类
│   ├── embedding.py    本地哈希向量化 + 远程 embedding 客户端（带降级）
│   ├── llm.py          OpenAI 兼容客户端（阻塞 + 流式）
│   ├── store.py        SQLite + 向量矩阵 + BM25 内存索引
│   ├── retrieve.py     混合检索：RRF 融合 + LLM 重排 + 去重
│   ├── ingest.py       入库管线
│   ├── summarize.py    单篇总结（map-reduce）
│   ├── compare.py      跨文档对比（统一维度抽取 + 综述）
│   ├── qa.py           RAG 问答
│   ├── server.py       FastAPI + SSE
│   ├── cli.py          命令行
│   └── static\index.html   Web 界面（单文件，无外部依赖）
├── _boot\
│   ├── sitecustomize.py   沙箱/Windows 环境启动补丁（见下）
│   └── install_jieba.py   从 sdist 直接安装 jieba
├── tools\
│   ├── make_demo.py    生成演示论文语料
│   ├── mock_llm.py     本地 mock LLM，用于无 Key 测试
│   ├── test_api.py     接口测试（不需要 LLM）
│   ├── test_llm.py     LLM 链路测试（配合 mock_llm）
│   ├── test_config.py  模型设置入口测试（含 .env 备份还原）
│   ├── test_real.py    真实 DeepSeek 端到端测试（验证解析器）
│   ├── test_filenames.py  文件名解析（23 种命名约定）
│   ├── test_rename.py  入库重命名端到端（沙箱隔离，不碰真实 inbox）
│   ├── demo_client.py  程序化调用的四种用法演示
│   ├── eval_retrieval.py  检索消融评估（答案串金标）
│   ├── eval_rerank.py     重排收益评估
│   ├── audit_sources.py   检查索引里的文件路径是否失效
│   ├── audit_extraction.py 检查标题/作者抽取质量
│   ├── repair_index.py    清理失效条目、修复过期路径
│   ├── restore_inbox.py   从原始资料库恢复 inbox 文件
│   ├── clear_stale_summaries.py  清理非当前模型生成的摘要缓存
│   ├── probe_deepseek.py  DeepSeek 连通性逐层探测
│   ├── check_encoding.py  校验持久化数据无损坏字符
│   ├── clear_stale_summaries.py  清理非当前模型生成的摘要缓存
│   ├── check_ui.py     前端 JS 语法与路由一致性检查
│   └── smoke_parse.py  解析与分块冒烟测试
├── inbox\              论文投放目录
├── data\               SQLite 索引、向量、摘要缓存
├── libs\               项目内依赖
├── install.cmd / install.ps1
├── install-paperqa.cmd / install-paperqa.ps1   可选：PaperQA2 组件
├── run.cmd / run.ps1
└── .env
```

---

## 技术选型说明

**为什么不用 FAISS / Chroma？**
学位论文库的量级是几百篇、几万个片段。一个连续的 float32 矩阵做暴力余弦
相似度是微秒级，引入向量数据库只会带来二进制 wheel 和版本兼容负担。
`store.py` 里就是一个 `@` 运算。

**为什么用 RRF 而不是分数加权？**
余弦相似度和 BM25 分数的量纲完全不可比，而且各自的分布随查询变化。
排名是天然可比的，所以用倒数排名融合（RRF）。

**为什么中文字符二元组？**
查「碳减排」时，只含「碳排放强度」的片段用分词是匹配不上的，加上字符
二元组就能桥接这类词形变化。代价是索引略大。

**为什么入库时全量重编码向量？**
本地向量化按语料 IDF 加权，语料一变所有已有向量就失效，无法增量修补。
远程 embedding 没有这个耦合，所以只编码缺失的行。

---

## 已知限制

- **扫描版 PDF** 没有文字层。入库时会检测并给出警告（提示用 MinerU 做 OCR），
  但本系统不做 OCR。已装的 MinerU 可以先把 PDF 转成 Markdown 再导入。
- **.caj / .nh**（知网格式）不支持，需先转成 PDF。
- **DOCX / TXT 没有真实页码**，引用标为「第N段」而不是「第N页」，避免编造页码。
- 页码来自文字层的位置，个别 PDF 若文字层混乱，页码可能有偏差。
- 同一篇论文的 PDF 版和转换版会各自入库（按文件内容哈希去重）；
  检索时会自动折叠内容相同的片段，但文档列表里仍是两条。
- 摘要缓存以文档为单位，不区分模型。切换模型后旧摘要会被标记为「已过时」
  而不是自动重算，需要手动「忽略缓存」重新生成。

---

## 环境相关：`_boot\sitecustomize.py`

这个文件不是业务代码，是为了让 Python 在受限沙箱 / Windows 下正常工作：

1. **临时目录重定向**：沙箱只允许写项目目录，而 `tempfile.gettempdir()`
   默认指向 `%LOCALAPPDATA%\Temp`，导致所有落盘到临时目录的操作报
   `PermissionError`。
2. **`tempfile.mkdtemp` 补丁**：Windows 上 CPython 把 mode `0o700` 翻译成
   受限安全描述符，在沙箱令牌下**同一进程也无法写入自己刚建的目录**。
   pip 解包每个 wheel 都走 `mkdtemp`，所以 `pip install` 会以
   `Errno 13` 失败。补丁改用 `os.makedirs`（继承权限）。

所以启动必须带 `PYTHONPATH=项目\_boot;项目\libs`——`run.ps1` 已经处理好了。

3. **jieba 单独安装**：jieba 只有 sdist，pip 的构建依赖步骤会卡住；
   `install_jieba.py` 直接从 PyPI 取源码包解压，纯 Python 无运行时依赖。

---

## 测试

```powershell
# 接口测试（不需要 LLM Key，需先启动服务）
python tools\test_api.py

# LLM 链路测试：先用 mock LLM 启动
python tools\mock_llm.py 8799
$env:LLM_BASE_URL='http://127.0.0.1:8799/v1'; $env:LLM_API_KEY='mock'
.\run.cmd
python tools\test_llm.py

python tools\check_ui.py        # 前端语法 + 路由一致性
python tools\smoke_parse.py     # 解析与分块
python tools\test_config.py     # 模型设置入口（自动备份并还原 .env）
python tools\probe_deepseek.py  # DeepSeek 连通性探测（无需 Key）
python tools\test_real.py       # 真实 DeepSeek 端到端（会消耗少量额度）
python tools\check_encoding.py  # 校验持久化数据无损坏字符
python tools\demo_client.py     # 客户端四种调用方式演示（含真实调用）
python tools\eval_retrieval.py  # 检索消融评估（不需 Key，不花钱）
python tools\eval_rerank.py     # 重排收益评估（会调用 LLM）
```

### 检索质量怎么量

`eval_retrieval.py` 用**答案串**做金标：为每条事实型问题指定一个只出现在少数片段里的
答案字符串（如「中介效应占比」→ `35.3%`），再看前 k 个片段里有没有它。好处是客观、
无需人工标注，而且贴近真实需求——能不能把写着那个数字的段落捞出来。

**实测（20 篇 / 1006 片段，14 条事实型问题）：**

| 检索器 | 答案命中@5 | MRR | 与 BM25 片段重合 |
|---|---|---|---|
| BM25 单路 | 92.9% | 0.818 | — |
| 本地向量单路 | 100.0% | 0.839 | 44.3% |
| 混合 RRF | 100.0% | 0.875 | 58.7% |
| 混合 RRF + LLM 重排 | 100.0% | **0.964** | — |

三个结论：

1. **本地向量不是摆设**：与 BM25 的片段重合度只有 44.3%，走的是不同路径，
   片段级确实比 BM25 准（MRR 0.839 vs 0.818）——文档级指标看不出这一点。
2. **RRF 融合有效**：0.875 高于任一路单独使用。
3. **重排值得它的成本**：MRR 再 +0.089，把 BM25 最弱的一条（第 4 名）拉到第 1 名。
   代价是每条查询一次 LLM 调用（约 1.7 秒）。

> **这个评估的局限**：只有 14 条问题、由我自拟、且只测「字面事实检索」，
> 不测概念性提问、跨文档综合、生成答案是否忠实。
> **100% 不代表系统好，只代表它没坏。** 它现在的价值是防止改动导致回归。

> `test_real.py` 与 mock 版的分工：mock 验证**管线**（HTTP 客户端、SSE 分帧、
> 批次切分），`test_real.py` 验证**解析器**。真实模型会把 JSON 包进
> ```` ```json ```` 围栏、在前面加解释性文字、打乱字段顺序——mock 永远返回
> 规范 JSON，所以只有真实调用才能暴露解析问题。

`tools\mock_llm.py` 是一个最小 OpenAI 兼容服务，会按提示词特征返回预置的
中文结果（重排 JSON / 抽取 JSON / 摘要 / 综述）。它让整条 LLM 链路——真实的
httpx 客户端、SSE 分帧、map-reduce 分批、JSON 解析——都能在不消耗 API 额度、
不需要 Key 的情况下被完整验证。
