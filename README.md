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
.\run.cmd --port 8878
```

打开 <http://127.0.0.1:8878/>（默认端口是 8848，但它常被别的本地服务占用，
见下面的端口说明）。

> **`run.cmd` 为什么全是 ASCII 注释**：它最初带着中文注释，结果 cmd.exe 用 GBK
> 解码 UTF-8 文件时把一个 `rem` 前缀吃掉了，于是 cmd 把注释当命令执行，
> 报 `'...' is not recognized as an internal or external command`。
> 现在中文逻辑全部放在 `run.ps1`（PowerShell 处理 UTF-8 没问题），
> `run.cmd` 只负责找到并调用它。

`run.ps1` / `run.cmd` 会把工作区里 vendored 的第三方依赖
（`E:\AI\aigc-reducer\libs` 等）追加进 `PYTHONPATH`——
本机没有把 FastAPI 装进 site-packages，沿用既有项目的做法避免重复安装。
公网容器里依赖由 `pip install -r requirements.txt` 提供，这段追加是空操作。

### 端口怎么选

Windows 有一批**排除端口**，绑上去会报 `WinError 10013`（不是代码问题）。
实测踩过：**8853、8890 都在排除段里**。另外 **8848 常被本地别的服务占用**。
选 8878 这类冷门端口最省事；被占用时 `run.cmd` 会打印明确错误而不是静默失败。

单独启动后端（便于接自己的前端）：

```powershell
$env:PYTHONPATH = "E:\AI\thesis-suite;E:\AI\aigc-reducer\libs;E:\AI\thesis-assistant\libs"
python -m uvicorn suite.api.app:create_app --factory --host 127.0.0.1 --port 8878
```

接口文档：<http://127.0.0.1:8878/api/docs>

### 让它"随时打开就有"（常驻服务）

不想每次手动敲命令，用 `service.ps1` 把它做成常驻：

```powershell
cd E:\AI\thesis-suite
.\service.ps1 install     # 装登录自启并立即启动
.\service.ps1 status      # 看状态（含健康检查与密钥封印状态）
.\service.ps1 stop        # 停止
.\service.ps1 restart     # 重启
.\service.ps1 uninstall   # 卸载自启并停止
```

装好之后，**每次登录 Windows 站点就自动在 8878 端口待命**，浏览器打开
<http://127.0.0.1:8878/> 即可，不用再开终端。

如果 `install` 报"两种自启方式都被当前权限拒绝"（受限会话、组策略、或非管理员环境），
在**管理员 PowerShell** 里粘这一行即可，效果相同：

```powershell
Copy-Item 'E:\AI\thesis-suite\start_background.cmd' ([Environment]::GetFolderPath('Startup') + '\ThesisSuite.cmd')
```

三点值得说明：

* **自启有两套兜底**。优先用计划任务；被权限拒绝就改用"启动文件夹"（不需要管理员）。
  两套都被拒时脚本不会失败，而是**打印一条可直接复制执行的命令**。
* **`start_background.cmd` 是幂等的**（内部用 `service.ps1 start -IfIdle`）。
  登录自启和手动启动各跑一次，不会起两个进程抢端口。
* **只监听本机**。要让同一局域网的其他设备（手机、平板）也能访问，用
  `.\service.ps1 start -Host_ 0.0.0.0`，然后用 `http://<本机IP>:8878/`。
  注意那等于把服务暴露给局域网，而本站**没有任何访问鉴权**——只在可信网络里这么做。
* **不在本机时不适用**。这是本机常驻方案，不是公网部署；公网见第八节。

日志在 `logs/service.out.log`（UTF-8，中文可读，由 Python 侧自己写）。
之所以不用启动脚本重定向输出，是因为 `Start-Process` 一旦带
`-RedirectStandardOutput`，.NET 会等子进程退出才返回，而常驻服务永远不退出——
"启动"命令就会永远挂住。实测踩过两次，细节写在 `suite/__main__.py` 的 `_log_config` 里。

### Windows 脚本的编码约束（改脚本前必读）

`tools/check_encodings.py` 把这条固化了。改完脚本跑一下：

```powershell
python tools\check_encodings.py
```

* **`.cmd` / `.bat` 必须纯 ASCII。** `cmd.exe` 按代码页探测解码，UTF-8 中文注释会让它
  吃掉 `rem` 前缀，把注释当命令执行（实测报 `'双击即可启动本机服务' is not recognized…`）。
* **`.ps1` 允许中文，但必须有 UTF-8 BOM。** Windows PowerShell 5.1 对没有 BOM 的脚本
  按 ANSI/GBK 解码，中文注释被解坏后**直接语法错误**，而且报错位置指向参数块，
  完全看不出是编码问题（实测报 `Missing expression after ','`）。

> ⚠️ 用编辑器/脚本改这两个 `.ps1` 之后**务必确认 BOM 还在**——
> 大多数写入工具默认不保留 BOM，这个坑在开发过程中反复踩过三次。
> `check_encodings.py` 就是为它写的。

---

## 七、测试

```powershell
$env:PYTHONPATH = "E:\AI\thesis-suite;E:\AI\aigc-reducer\libs;E:\AI\thesis-assistant\libs"
$env:PYTHONUTF8 = "1"

python tools\smoke_suite.py         # 端到端：装配 / 离线出题 / 闸门 / 检查器 / 会话 / 前端
python tools\smoke_gather.py        # 资料获取：50 项断言（含真实 Crossref 核验）
python tools\smoke_byok.py          # BYOK 防线：服务器密钥不可被借用（含 AST 静态校验）
python tools\smoke_container.py     # 容器形态：干净环境 + suite/vendor 同级摆放
python tools\check_requirements.py  # 依赖对账：requirements.txt 是否覆盖源码的真实 import
python tools\live_chain.py          # 真实模型跑完整链路（会花钱），密钥从既有项目 .env 读
```

其中 `smoke_container.py` 是最接近"公网会怎样"的一个。它在开发机上模拟容器环境：

* 把 `suite` 与 `vendor` 复制到一个裸目录**同级摆放**（等价于镜像里的 `/app`）；
* 用 `python -S -I` 起子进程——**不读 site-packages、不继承 `PYTHONPATH`**，
  依赖只来自显式指定的目录（等价于容器里 pip 装出来的那份）；
* 把 `E:\AI\thesis-assistant`、`E:\AI\aigc-reducer`、`E:\AI\thesis-agents`
  三个 Windows 绝对路径**打桩成不存在**（等价于 Linux 容器里的情形）；

然后验证离线出题、53 个检查器（含数字接地）、FastAPI 装配在那种形态下都能跑。

> 这个模拟是**必要的**：开发机上跑通不等于容器里跑通。它当场抓出一个容器会炸的问题——
> `anyio` 需要 `typing_extensions.sentinel`，而旧版本没有这个符号，
> 启动直接 `ImportError`。所以 `requirements.txt` 里显式钉了 `typing-extensions>=4.12`。

`live_chain.py` **只把密钥放在请求头里**传给服务器，服务器的代码路径里没有任何密钥回退——
它同时也就验证了 BYOK 链路本身。

---

## 八、部署到 Hugging Face Spaces

### 方式一：一条命令（需要你的 HF write token）

```powershell
# 先做本地自检（不联网、不需要 token）
python tools\push_hf.py --offline-check

# 真部署：自动建 Space 并上传
python tools\push_hf.py --token hf_你的write_token --space thesis-suite
```

脚本会打印公开地址 `https://<用户名>-<space名>.hf.space`。
**token 不会写进任何文件**，用完即可作废。

> 为什么不用 `huggingface_hub`：本机开发不用 `requirements.txt`（依赖是 vendored 的），
> 为部署再装一套工具链不值得。工作区里已有 `requests`，脚本直接用 HF 的
> commit API 提交，不引入新依赖。

### 方式二：手工五步

1. 在 <https://huggingface.co/new-space> 新建 Space：**SDK** 选 `Docker`，
   **Docker template** 选 `Blank`。
2. 把这个 GitHub 仓库的内容推到 Space 的 git 远端：
   `https://huggingface.co/spaces/<用户名>/<space名>`（密码用 HF write token）。
3. Space 会自动构建并启动。
4. 打开 `https://<用户名>-<space名>.hf.space` 验证。
5. 可选：在 Space 设置页填 `SUITE_CROSSREF_MAILTO` 等变量（见下表）。

**两种方式都不需要配置任何模型密钥。** 站点是 BYOK 的：密钥由访客在页面右上角填入，
只存在他自己的浏览器里，随请求头转发给模型服务商。

可选环境变量：

| 变量 | 用途 |
|---|---|
| `SUITE_CROSSREF_MAILTO` | Crossref 礼貌池邮箱，填了更不易被限流（非密钥） |
| `SUITE_ALLOW_CUSTOM_BASE_URL` | 设为 `1` 允许访客自带任意接口地址；不设则只放行内置服务商（防 SSRF） |
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
│   ├── smoke_suite.py          端到端
│   ├── smoke_gather.py         资料获取 50 项
│   ├── smoke_byok.py           BYOK 防线（含 AST 静态校验）
│   ├── smoke_container.py      容器形态模拟（干净环境 + 同级摆放）
│   ├── check_requirements.py   依赖对账（AST 抠 import）
│   ├── live_chain.py           真实模型跑完整链路
│   ├── push_github.py          无 git 环境下用 GitHub API 推仓库
│   └── push_hf.py              部署到 Hugging Face Spaces
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
