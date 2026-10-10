---
title: 论文助手 四合一
emoji: 📄
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
license: mit
short_description: 题目生成 / 资料获取 / 写作 / 评估，访客自带 API Key
---

# 论文助手 · 四合一站点

把四个论文助手整合成一个公网可访问的网站：**题目生成 → 资料获取 → 写作 → 评估**。

访客**自带 API Key**（下称 BYOK，Bring Your Own Key）：密钥只存在访客浏览器里，
随每次请求经请求头转发给模型服务商。服务器不保存、不缓存、不回退使用站主的密钥，
也不把它写进日志。

---

## 一、四个助手分别来自哪里

| # | 助手 | 实现来源 | 是否需要 Key |
|---|---|---|---|
| ① | 题目生成 | `thesis-topic-helper` 的 `core/` 整包 | 否（可离线） |
| ② | 资料获取 | `thesis-agents` 的文档解析 + 外部核验 | 抽取需要；核验不需要 |
| ③ | 写作 | 新增的「只依据证据」写作链路 | 是 |
| ④ | 评估 | `thesis-agents` 的 53 个检查器 + 审稿意见书 | 检查不需要；意见书需要 |

上游两套代码**一行都没有改**，以原样隔离在 `vendor/` 下（见第二节）。
本站自己的代码只有 `suite/` 目录，约 2000 行。

---

## 二、关键设计：为什么上游代码能原样复用

两套上游项目各自通过**相对项目根的目录深度**推导数据目录、`.env`、导出目录
（例如 `PROJECT_ROOT = Path(__file__).resolve().parent.parent`），并且包名撞车：

| 上游项目 | 包名 | 与另一套的冲突 |
|---|---|---|
| `thesis-agents` | `app` | `config` / `llm` / `server` 三个模块同名 |
| `thesis-topic-helper` | `core` | 同上 |

直接平铺会同时踩两个坑：算错根目录（把文件写进代码目录）、同名模块互相覆盖。

**解法是给每套代码一个"隔离层"**，让它自己算出的根目录正好落在自己的层里：

```
vendor/
  thesisapp_stage/          <- thesis-agents 的隔离层
    app/                    <- 原样搬运，256 处相对导入一个字没改
  topichelper_stage/        <- thesis-topic-helper 的隔离层
    core/                   <- 原样搬运
```

两个导入名 `app` 与 `core` 互不相同，因此可以在同一进程里共存。
`suite/boot.py` 只做一件事：在导入任何上游模块之前把这两层挂进 `sys.path`。

> 实测验证：两棵树同进程导入成功，`app.config.ROOT` 与 `core.config.PROJECT_ROOT`
> 各自落在自己的隔离层内，`sys.modules` 里不存在裸 `app` / `core`。

---

## 三、BYOK 是怎么做到不回退到站主密钥的

这是本站**最需要保证正确**的一件事。上游两套代码都有"借用邻居项目 `.env`"的回退逻辑：

* `thesis-agents` 的 `load_settings()`：进程环境变量 > 本项目 `.env` > **邻居项目 `.env`**
* `thesis-topic-helper` 的 `load_settings()`：**邻居项目 `.env`** > 本项目 `.env` > 环境变量

在公网上，这意味着两件绝对不能发生的事：访客不填密钥时站主的密钥被拿去替他付费；
以及密钥被写进日志或错误信息里泄露。

所以本站**一次都不调用上游的 `load_settings()`**：

| 场景 | 做法 |
|---|---|
| 题目生成 | 亲手构造 `core.config.Settings` 注入 `generate_stream(settings, ...)` |
| 写作 / 评估 | 用 `suite/llm.py` 的 `Chat(creds)`，只认显式传入的凭据 |
| 访客没填密钥 | 显式构造 `api_key=""` 的配置 → 上游判定"未配置"→ 走离线模式 |

凭据只存在于**请求局部对象**里，不写 `os.environ`、不写任何全局变量。
环境变量是进程级的，两个访客同时请求会互相覆盖——这正是不能用它的原因。

### 还有一道结构性防线（`suite/boot.py`）

"本站不调用 `load_settings()`"是**靠约定**成立的，不是靠结构。将来任何一条路径
误调一次，就会拿站主机器上的真密钥替访客付费——而且是几十次模型调用的规模。

所以 `boot.prepare()` 会主动把上游的回退**数据**置空（只动两个模块级常量，不碰上游函数体）：

| 档位 | 处理 |
|---|---|
| `os.environ` 里的 `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | 直接删除 |
| `app.config.FALLBACK_ENV_PATHS`（指向 `E:\AI\thesis-assistant\.env` 等） | 换成空元组 |
| `core.config.SIBLING_ENV_FILES` / `SIBLING_MODELS_JSON` | 换成空元组 / 不存在的路径 |

> **为什么不能只把 `LLM_API_KEY` 设成空串**：实测无效。`thesis-agents` 写的是
> `os.environ.get("LLM_API_KEY") or env.get("LLM_API_KEY", "")`——空串为假，
> 于是继续往下读，读到了邻居项目 `.env` 里的**真密钥**。第一版就是这么写的，
> 被 `tools/smoke_byok.py` 当场抓出来。必须删掉环境变量、并清空回退路径元组。

`tools/smoke_byok.py` 用三种方式验证这道防线：

1. 往隔离层放一个带假密钥的兜底 `.env`，调上游 `load_settings()`，必须取不到密钥；
2. 用 **AST** 静态校验 `suite/` 里 12 个模块**没有一个真的调用** `load_settings()`
   （用 AST 而不是 grep，因为在注释里解释"为什么不能调它"会被文本匹配误报成调用）；
3. 从上游源码里提取硬编码的回退路径，证明本机上它们**确实躺着真密钥**——
   也就是说这道防线不是多余的，而是必需的本机开发安全措施。

封印的状态会在 `/api/health` 的 `key_fallback_neutralized` 字段里如实上报，
运行时可见，而不是只写在注释里。

请求头：

```
X-LLM-Key:    访客的 API Key
X-LLM-Base:   接口地址（可选，默认按所选服务商）
X-LLM-Model:  模型名（可选）
```

**接口地址白名单**：公网部署时 `base_url` 由访客提供，服务器会照着它发请求。
默认只放行已知服务商，避免本站被当成任意请求的跳板（SSRF）。
自建或本地部署可设 `SUITE_ALLOW_CUSTOM_BASE_URL=1` 解除限制。

---

## 四、写作助手的核心纪律

上游写作链路**硬依赖一个 RAG 知识库进程**做向量检索，而那个能力由对端
`thesis-assistant` 提供——整个 `thesis-agents` 项目里**没有任何 embedding 实现**
（已 grep 确认）。公网容器里没有对端，照搬的结果是：每章都因"检索不到证据"而拒绝生成，
最终产出空稿加一屏 `writing.needs_source`。

本站的做法是**保留上游最核心的那条纪律，换掉检索那一层**：

    无证据 → 不落笔

证据来自「资料获取」助手录入的**访客自己的文献原文**，而不是向量检索。
这让"证据"变成作者可验证的原文，比检索命中更硬。三条约束都在代码里强制，不靠提示词自觉：

1. **证据闸门**：证据库为空时直接拒绝生成，**且不调用模型**（省下访客的钱），
   并告诉他先去录入资料。
2. **引用编号校验**：正文里每个 `[n]` 必须是证据库里真实存在的编号；
   越界引用被记成"编造引用"通报给访客，不静默删掉。
3. **数字接地**：正文里的数字必须能在证据原文里找到。复用上游的
   `check_number_grounding`（它本来就在 `thesis-agents` 里，且是纯离线检查器）。

---

## 五、评估助手的两个坑（都已踩过并修好）

评估能力是四个助手里**唯一完全不依赖模型就有价值**的：53 个检查器全部
`needs_llm=False`、不联网、不花钱。但接入它踩了两个静默失效的坑：

**坑一：章节标题的层级。** 上游 `split_by_sections` **只认单个 `#`**
（`# 第1章 绪论` → `introduction`），`##` 会被当成正文。而 `check_number_grounding`
是"先取章节、再读证据台账"的：切不出章节就直接返回空，界面上表现为**"检查通过"**。
实测确认：用 `##` 时该检查命中数恒为 0。本站因此在评估入口做了**标题层级归一化**，
不要求访客改格式。

**坑二：误告造假与误告真实，两个方向都要防。**

* 解析器会把「参考文献」这个章节标题也当成一条条目，拿去检索必然 `not_found`，
  报告里就多出一条"查无此文"的**假警报**。误告造假比漏报严重得多，
  所以按上游的 `lookupable` + `title_quality == "high"` 两道判据过滤，
  并把跳过数量如实告诉访客。
* 反方向：上游在 DOI 查不到时**退回按标题做词级相似度检索**，
  而这个相似度会高得离谱——上游作者实测一条完全编造的标题以 **0.93**
  命中了一篇真实存在的论文。也就是说**编造的文献可能被盖章成"真实"**。
  本站因此对"没有 DOI、只能靠标题"的条目把 `confirmed` 降级为 `weak_match`。

结论是本站的核验功能保证的是**"题录能不能对上真实记录"**，
**不保证文献结论为真**。界面上也照这个口径说明。

---

## 六、本机运行

```powershell
cd E:\AI\thesis-suite
.\run.cmd
```

打开 <http://127.0.0.1:8848/>。`run.ps1` / `run.cmd` 会把工作区里 vendored 的
第三方依赖（`E:\AI\aigc-reducer\libs` 等）追加进 `PYTHONPATH`——
本机没有把 FastAPI 装进 site-packages，沿用既有项目的做法避免重复安装。
公网容器里依赖由 `pip install -r requirements.txt` 提供，这段追加是空操作。

单独启动后端（便于接自己的前端）：

```powershell
$env:PYTHONPATH = "E:\AI\thesis-suite;E:\AI\aigc-reducer\libs;E:\AI\thesis-assistant\libs"
python -m uvicorn suite.api.app:create_app --factory --host 127.0.0.1 --port 8848
```

接口文档：<http://127.0.0.1:8848/api/docs>

---

## 七、测试

```powershell
$env:PYTHONPATH = "E:\AI\thesis-suite;E:\AI\aigc-reducer\libs;E:\AI\thesis-assistant\libs"
$env:PYTHONUTF8 = "1"

python tools\smoke_suite.py     # 端到端：装配 / 离线出题 / 闸门 / 检查器 / 会话 / 前端
python tools\smoke_gather.py    # 资料获取：50 项断言（含真实 Crossref 核验）
python tools\smoke_byok.py      # BYOK 防线：服务器密钥不可被借用（含 AST 静态校验）
python tools\live_chain.py      # 真实模型跑完整链路（会花钱），密钥从既有项目 .env 读
```

`live_chain.py` **只把密钥放在请求头里**传给服务器，服务器的代码路径里没有任何密钥回退——
它同时也就验证了 BYOK 链路本身。

---

## 八、部署到 Hugging Face Spaces

1. 把本目录推到 GitHub 仓库（本站开发环境没有 `git`，用 GitHub REST API 推送）。
2. 在 Hugging Face 新建 Space，SDK 选 **Docker**，模板选 **Blank**。
3. 把仓库连到 Space（或直接推同一份代码到 Space 的 git 远端）。
4. Space 会读 `README.md` 顶部的 frontmatter（`sdk: docker` / `app_port: 7860`）
   并执行 `Dockerfile`。

容器里**不需要配置任何模型密钥**——这是 BYOK 的意义所在。
可选环境变量：

| 变量 | 用途 |
|---|---|
| `SUITE_CROSSREF_MAILTO` | Crossref 礼貌池邮箱，填了更不易被限流（非密钥） |
| `SUITE_ALLOW_CUSTOM_BASE_URL` | 设为 `1` 允许访客自带任意接口地址 |
| `DSH_THESIS_DATA_DIR` | 数据目录，默认 `/app/data`（容器内是临时的） |

**已知限制**：会话数据只存在内存里。免费档 Space 会休眠，
休眠后会话清空，前端会提示重来。这是有意的取舍——本站定位是
"一次性把一份稿子走完整条链路"，不是长期论文管理平台。要长期保存请自己下载产物。

---

## 九、安全须知

* **不要把任何 API Key 提交进仓库。** `.gitignore` 已排除 `.env` / `models.json`
  以及 `vendor/` 下的 `.env`。
* 上游 `thesis-agents` 的 `PROJECTS_DIR` 是**全局共享单目录**且 `Blackboard.load`
  没有归属校验——任何人猜到项目 id 就能读别人的论文。**本站因此完全没有复用它的
  项目存储**，会话状态自己管，存在进程内存里并按会话 id 隔离。
* 本站不做用户注册与鉴权：任何拿到网址的人都能用，但用的是**他自己的密钥**。
  如需限制访问，请在 Space 设置里改为私有，或在前置反向代理上加访问口令。
* 访客可以随时用 `DELETE /api/session/{sid}` 抹掉自己会话里的全部数据。

---

## 十、目录结构

```
thesis-suite/
├── suite/                      本站自己的代码（只有这一层是新写的）
│   ├── boot.py                 把两套上游代码挂进 sys.path 的引导层
│   ├── credentials.py          BYOK：请求头 -> 上游配置对象，无任何回退
│   ├── llm.py                  直连模型服务的薄客户端（只认显式凭据）
│   ├── sessions.py             内存会话与证据存储
│   ├── __main__.py             python -m suite
│   └── api/
│       ├── app.py              FastAPI 应用工厂
│       ├── topics.py           ① 题目生成
│       ├── gather.py           ② 资料获取
│       ├── writing.py          ③ 写作
│       └── review.py           ④ 评估（含标题归一化与核验降级）
├── vendor/
│   ├── thesisapp_stage/app/    thesis-agents，原样搬运（256 处相对导入未改）
│   └── topichelper_stage/core/ thesis-topic-helper，原样搬运
├── web/                        单页前端（原生 JS，无构建步骤）
├── tools/                      冒烟与真实链路测试
├── deploy/                     部署说明
├── Dockerfile                  HF Spaces 用
└── requirements.txt            公网部署的依赖（本机不用它）
```

---

## 十一、能力边界（如实说明）

* **题目生成**：离线模式的质量取决于内置学科知识库的覆盖，冷门学科会明显偏弱。
* **资料获取**：抽取质量取决于访客贴进来的原文质量；逐字校验只能保证
  "引用的片段确实在原文里"，**不能保证原文本身可信**。
* **写作**：产出的是**初稿**，不是成稿。系统刻意不替作者做需要专业判断的结论；
  参考文献表只列真正被引用的条目，缺字段就留空，不补不编。
* **评估**：53 个检查器查的是**形式问题**（引用编号、格式、数字是否有来源、
  写作结构），**不能判断"这个创新点够不够新"**这类需要同行评议的问题。
  评分由问题数量与严重度机械推导，不让模型打印象分——因为
  **模型给自己刚生成的稿子打分会系统性偏高**。
