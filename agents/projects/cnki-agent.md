<!--
  本文件是从本机项目 cnki-agent 的 README.md **原样转载**的。
  转载目的：把散落在各项目的说明集中到一处索引，便于查阅。
  正文未经修改；如果这里的内容与该项目目录里的 README.md 不一致，
  以项目目录里的为准（那份是随代码演进的）。

  来源：E:\AI\cnki-agent\README.md
  收录时间：2026-10-29
-->

> **本文档为转载**，来源 `E:\AI\cnki-agent\README.md`（知网文献采集 Agent · 替你点鼠标下载题录与 PDF）。正文未作修改。

---

# 知网文献采集 Agent · CNKI Agent

一个能在你**不在电脑前**的时候自己干活的小 Agent：按你给的题目和筛选口径去知网检索，
对齐知网的筛选栏（来源类别 / 年度 / 文献类型 / 排序），把结果筛干净，
再逐篇点开详情页、点下载、把 PDF/CAJ 落到本地文件夹，最后生成一份可读的报告。

它不是爬虫，是**替你点鼠标**：用你自己登录好的那个浏览器，按人的节奏点它该点的按钮，
碰到验证码、掉登录、没权限就**立刻停下**等你回来。

```
检索 ──► 对齐知网筛选栏 ──► 翻页抓题录 ──► 本地复核 ──► 排序取前 N ──► 逐篇下载 ──► 报告
              （尽力点）        （表头驱动）   （同样规则再查一遍）  （被引/年份…）  （10–22s/篇）
```

---

## 一、先说清楚边界（这决定了它为什么这么设计）

**它不绕过任何东西。**

| 事情 | Agent 的做法 |
|---|---|
| 登录 | 一次性由你手工登录，Cookie 留在专用浏览器配置里；不存账号密码、不代填 |
| 验证码 / 滑块 | 只检测，检测到就停，写 `NEEDS_HUMAN.md` 等你。知网对可疑会话会直接 302 到 `/verify/home?captchaType=blockPuzzle`，这种跳转也算检测到 |
| 机构权限 | 用你会话里本来就有的权限。没权限的那篇记成「无权限」，不尝试任何旁路 |
| 并发 | 单标签页、串行、10–22 秒一篇，跟人翻文献的节奏一致 |
| 频率 | 硬上限写死在代码里（见第五节），配置写大了也会被夹住 |

**知网的用户协议明确禁止自动化批量下载**，风控会先弹验证码，再限流，严重时封 IP / 封账号。
这个 Agent 是给**个人做一次文献综述、按需取几十篇**用的，不是给你搭镜像站用的：

- 单次默认最多 30 篇，硬顶 80 篇；
- 每小时最多 50 篇，硬顶 120 篇；
- 间隔最短 8 秒，硬顶就是 8 秒；
- 一出现风控信号立刻退出，不会重试、不会换 IP、不会换指纹。

如果这些限制跟你的预期不一样，说明这个工具不是你要的东西——那我不建议你改它们。

---

## 二、快速开始

### 0. 准备

- Windows，Edge 或 Chrome（默认用 Edge）
- Python 3.10+，已装 `requests` / `websocket-client` / `beautifulsoup4`
- 能正常访问知网（校园网 IP、机构代理、或个人账号都行）

### 1. 写任务配置

```powershell
cd E:\AI\cnki-agent
copy task.example.json task.json
notepad task.json
```

至少要改：`name`、`queries`（检索词）、`filters`（年份/来源类别/文献类型/最低被引）、
`select.max_records`（要几篇）。字段含义见第四节。

### 2. 启动专用浏览器（一次）

```powershell
python main.py browser
```

会启动一个**独立配置**的 Edge（`browser_profile/`），和你平时用的浏览器互不干扰。

> 如果你在沙箱 / 受限环境里跑，浏览器进程可能被系统回收，此时用：
> `python main.py browser --print-only`，把打印出来的命令粘到你自己的终端里执行。

### 3. 登录一次

```powershell
python main.py login
```

它会把入口页打开，你在那个窗口里把知网（或机构代理）登录好，回车确认后它会自动验证：
能正常打开检索页、能看到结果列表 → 认为登录态可用。

> 机构代理用户：把 `task.json` 里的 `entry.base_url` / `entry.search_url` 换成代理下的知网地址，
> 并把 `entry.proxy_url` 填成代理首页，Agent 会从这个入口开始。

### 4. 先只抓题录，不下载（强烈建议）

```powershell
python main.py search --task task.json
```

跑完看 `out\<任务名>\runs\<时间>\report.md`：

- 「页面抓到 / 通过筛选」的数字对不对；
- 淘汰原因是不是你想要的（年份、来源类别、被引、题名排除词…）；
- 清单里有没有不该要的、缺不缺该要的。

这一步会走完全部检索和筛选逻辑，**只是不点下载**，所以调筛选口径用它最安全。

### 5. 正式跑

```powershell
python main.py run --task task.json --until 06:30
```

`--until` 是时间上限：到点就不再开始新的下载，已下载的照常落盘、照常出报告。

---

## 三、让它在你不在的时候跑

### 方式 A：计划任务（推荐）

```powershell
# 每天 02:00 开始，最晚跑到 06:30
powershell -ExecutionPolicy Bypass -File install-task.ps1 -Time 02:00 -Until 06:30

# 先看看会注册什么
powershell -ExecutionPolicy Bypass -File install-task.ps1 -DryRun

# 不想要了就删掉
powershell -ExecutionPolicy Bypass -File install-task.ps1 -Uninstall
```

要求：机器开着 + 你处于登录状态（锁屏可以，注销/关机不会执行）。
计划任务用的是 `Interactive` 令牌，因为浏览器需要有桌面会话。

### 方式 B：挂着循环跑

```powershell
# 每次跑 20 篇，跑完等 90 分钟再来一轮，直到 06:30
while ($true) {
  python main.py run --task task.json --yes --until 06:30
  Start-Sleep -Seconds 5400
}
```

### 断点续跑

每处理完一篇就写一次 `out\<任务名>\state.json`。中断后再跑，会按记录 key 跳过：
`downloaded` / `exists` 的不再下载，`no_access` 的也不重复试（除非你 `--fresh`）。

```powershell
python main.py run --task task.json --yes --until 06:30   # 接着上次的进度
python main.py run --task task.json --fresh               # 归档旧进度，从头来
python main.py report --task task.json                    # 只重建报告/导出，不动网页
```

### 回来先看什么

| 文件 | 看什么 |
|---|---|
| `out\<任务名>\report.md` | 一页看完：检索条件、抓到多少、淘汰原因、下载了哪些、哪些要你处理 |
| `out\<任务名>\NEEDS_HUMAN.md` | **只在出事时出现**：卡在风控还是掉登录，怎么办 |
| `download\` | 全文文件，命名 `序号_题名_年份.pdf` |
| `out\<任务名>\state.json` | 单篇粒度的进度，重跑就从这里接着来 |

---

## 四、task.json 字段

```jsonc
{
  "name": "任务名（也用作输出目录名）",
  "research_question": "研究问题（只写进报告，帮助你自己回看）",
  "entry": {
    "base_url": "https://kns.cnki.net",
    "search_url": "https://kns.cnki.net/kns8s/defaultresult/index?kw={kw}&korder={order}",
    "order": "SU",           // 默认检索项
    "proxy_url": ""          // 机构代理首页，没有就留空
  },
  "queries": [ { "kw": "检索词", "order": "SU", "note": "备注" } ],
  "filters": { ... },
  "search": { "pages_max": 3, "sort": "被引", "apply_ui_filters": true },
  "select": { "rank_by": "cited", "max_records": 40 },
  "download": { "enabled": true, "format_prefer": ["pdf","caj"],
                "interval_sec": [10,22], "max_per_run": 30, "max_per_hour": 50 },
  "paths": { "download_dir": "download", "out_dir": "out" }
}
```

### 检索项 `order`（知网的检索字段）

| 值 | 知网叫法 |
|---|---|
| `SU` | 主题 |
| `TI` | 篇名 |
| `KY` | 关键词 |
| `AB` | 摘要 |
| `AU` | 作者 |
| `FI` | 第一作者 |
| `AF` | 作者单位 |

多个检索式会依次执行、结果合并去重。建议主检索式 + 1–2 个补充检索式。

### `filters`（名字就是知网左侧筛选栏的叫法）

| 字段 | 含义 | 示例 |
|---|---|---|
| `year_from` / `year_to` | 发表年度区间 | `2019` / `2026` |
| `source_cats` | 来源类别，可多选 | `["北大核心","CSSCI"]` |
| `doc_types` | 文献类型 | `["期刊"]`、`["博士","硕士"]`、`["期刊","会议"]` |
| `min_cited` | 最低被引 | `10` |
| `min_downloads` | 最低下载量 | `0` |
| `title_include` | 题名必须包含其一 | `["创新绩效"]` |
| `title_exclude` | 题名包含即排除 | `["综述","书评","访谈"]` |
| `title_regex_include` / `title_regex_exclude` | 正则版 | `["^.*案例研究.*$"]` |
| `max_per_source` | 同一来源最多几篇（防止某刊霸榜） | `3`，0 = 不限 |
| `dedupe` | 判重口径 | `title`（默认）/ `doi` / `none` |
| `min_relevance` | 最低相关度 0–100（启发式） | `0`，一般不用开 |

`source_cats` 支持：`北大核心`、`CSSCI`、`CSSCI扩展版`、`CSCD`、`SCI`、`SCIE`、`SSCI`、`EI`、`AMI`、`科技核心`、`A&HCI`。

### `select`

- `rank_by`：`cited`（被引，默认）/ `downloads`（下载）/ `year`（年份）/ `relevance`（相关度）
- `max_records`：最终录取篇数（硬顶 100）

### `download`

- `enabled`：false 时只出题录，适合"先看清单再决定"
- `format_prefer`：优先 `pdf`，其次 `caj`
- `interval_sec`：每篇之间随机等待区间（分钟级下限 8 秒）
- `max_per_run` / `max_per_hour`：单次 / 每小时下载上限

---

## 五、筛选是怎么做的

一句话：**先在知网上点一遍，再在本地用同样的规则复核一遍。**

1. **点知网的筛选栏**：来源类别、年度区间、排序，按文字找可点元素去点，每步等页面刷新。
   点了会写进日志；点不到也继续，不会因为一个筛选项没找到就整趟失败。
2. **翻页抓题录**：表头驱动列映射（`题名/作者/来源/发表时间/数据库/被引/下载`），
   列顺序变了也不影响；一条都解析不出来时会提示你去跑 `probe`。
3. **本地复核**：年份、来源类别、文献类型、最低被引/下载、题名包含/排除、正则、判重、
   单刊上限、相关度——**全部按同一套规则再查一遍**。所以就算知网少给了个筛选项、
   或者点了个没生效，最终清单也不会跑偏。
4. **排序取前 N**：按被引（或下载/年份）降序截断。
5. **逐篇下载**：进详情页 → 抓关键词/摘要/基金/DOI（顺手补全题录）→ 点「PDF下载」
   或「CAJ下载」→ 等文件落盘 → 重命名为可读文件名。

### 两个刻意做严的地方

- **核心标识不混用**：`CSSCI` 不能因为文献标了 `CSSCI扩展版` 而通过；`SCI` 不能因为标了
  `SCIE` 而通过；`Science Bulletin` 这种刊名里含 "SCI" 的不会被误判成 SCI 收录。
- **核心标识全缺失时自动跳过该项**：如果某次改版导致页面一个核心标识都没抓到（覆盖率 < 50%），
  来源类别筛选会被跳过并在报告里警告，而不是把所有文献都判成"来源类别不符"。

---

## 六、输出

```
out\<任务名>\
├─ state.json              单篇粒度进度（断点续跑用）
├─ NEEDS_HUMAN.md          出事了才有：卡在哪、怎么处理
└─ runs\<时间戳>\
   ├─ report.md            人看的报告（条件回显/数量/淘汰原因/清单/待处理）
   ├─ results.csv          Excel 直接开（UTF-8 BOM，中文不乱码）
   ├─ results.json         二次开发用（含全部字段）
   ├─ references_gbt7714.txt  直接贴进论文参考文献表
   ├─ results.bib          BibTeX / Zotero / JabRef
   ├─ run.log              本次运行的完整日志
   └─ console.log          控制台输出
download\
└─ 001_数字化转型与企业创新绩效_2023.pdf
```

`results.csv` 的状态列：`已下载` / `无权限` / `需人工` / `失败` / `已跳过` / `待处理`。

---

## 七、安全刹车（写死在代码里）

| 项 | 默认 | 硬上限 |
|---|---|---|
| 单次下载篇数 | 30 | 80 |
| 每小时下载篇数 | 50 | 120 |
| 篇间隔 | 10–22 秒 | 不低于 8 秒 |
| 翻页数 | 3 | 20 |
| 录取篇数 | 40 | 100 |

触发任何一种刹车都会正常收尾、出报告，不会硬跑到断：

- 到 `--until` 时间；
- 单次/每小时配额用完；
- 检测到验证码、安全验证、访问频繁等风控字样 → **立即退出**（退出码 3）；
- 检测到登录墙 → 立即退出（退出码 2）。

退出码：`0` 正常 / `2` 需要登录 / `3` 风控中断 / `4` 没抓到结果 / `5` 其他错误。

---

## 八、知网改版了、选择器失配了怎么办

症状：`report.md` 里"页面抓到 0 条"，或来源类别全是"未找到该筛选项"。

```powershell
python main.py probe --kw 你的检索词
```

它会打印并保存：

- `diag\diagnose_*.json`：表格真实 class、表头文字、首行单元格、下载控件、排序/来源类别/年度候选元素、翻页控件
- `diag\page_*.html`：整页 HTML
- `diag\page_*.png`：截图

改动只集中在 `core/pagekit.js`（页面侧）和 `core/parse.py`（离线解析），两边规则一致。
改完先跑离线测试：

```powershell
python tests\run_all.py              # 解析/筛选/导出/状态
node tests\pagekit_dom_test.mjs      # 页面脚本（jsdom）
```

还可以把抓下来的 HTML 离线复算：

```powershell
python main.py reprocess diag\page_20250101_020000.html
```

---

## 九、常见问题

**下载没反应 / 一直"等待下载超时"**
1. 看那个浏览器窗口：是不是弹了"需要登录"或"购买"页面；
2. 试这一篇手工点「PDF下载」能不能下——不能就是没权限，不是 Agent 的问题；
3. 检查浏览器设置里的下载目录是否被策略改走（Agent 用 CDP 指定了 `download/`，
   但如果被企业策略锁死，文件会落到默认下载目录，此时报告里会记 `failed`）。

**硕士/博士论文只有 CAJ 没有 PDF**
学位论文常常只给 CAJ。Agent 会优先 PDF、退而求其次 CAJ，文件名后缀保留 `.caj`。
转 PDF 用知网研学（CAJViewer）手工转，或者先读 CAJ。

**下了个几十 KB 的"PDF"**
那是权限提示页/错误页。Agent 会检查文件头是不是 `%PDF-`，不是就记成「需人工」并保留文件供你判断。

**中文乱码**
CSV 带 BOM，Excel 双击正常。若仍乱码，用「数据 → 从文本/CSV」导入并选 UTF-8。

**浏览器被回收 / 端口连不上**
用 `python main.py browser --print-only` 打印命令，在自己终端里手动启动并保持窗口开着。

**端口 9222 被占**
`python main.py browser --port 9333`，后续命令都加 `--port 9333`。

**能不能不要浏览器窗口（无头）**
技术上可以（`--headless`），但知网风控对无头更敏感，而且遇到验证码时你没窗口可救。
不建议。

**一开跑就被判风控（退出码 3，NEEDS_HUMAN 说风控拦截）**
知网的会话风控比多数站点紧：新建的浏览器配置、刚登录完就立刻连续检索，都可能被 302 到
`/verify/home?captchaType=blockPuzzle`（安全验证页）。对策：
1. 在那个浏览器窗口里手工把验证过掉，正常检索两三页，再回来跑 Agent；
2. 首次使用别一登录就无人值守，先 `search` 跑一两轮让人味重一点；
3. 真要长期无人值守，`--until` 给它留够时间，让它慢一点、少一点。

**登录态多久失效**
看你机构的策略。失效时退出码是 2，并写 `NEEDS_HUMAN.md`，重跑 `python main.py login` 即可。

---

## 十、目录结构

```
cnki-agent/
├─ main.py                 命令行入口（browser/login/check/probe/search/run/report/reprocess）
├─ run.cmd                 无人值守跑一次
├─ install-task.ps1        注册 Windows 计划任务
├─ test.cmd                跑全部离线测试
├─ task.example.json       任务模板
├─ core/
│  ├─ browser.py           CDP 控制层（启动、标签页、下载目录、文件落盘检测）
│  ├─ pagekit.js           注入页面的工具箱（找表、抓记录、点筛选、点下载、诊断）
│  ├─ cnki.py              主流程（检索→筛选→抓取→下载→刹车）
│  ├─ filters.py           知网口径的筛选规则 + 硬上限
│  ├─ parse.py             离线 HTML 解析（测试与复算）
│  ├─ export.py            CSV/JSON/GB-T 7714/BibTeX/Markdown
│  ├─ state.py             断点续跑状态
│  └─ console.py           控制台 UTF-8
├─ tests/
│  ├─ mock/                仿造的知网结果页 / 详情页
│  ├─ run_all.py           103 项离线断言（含端到端假页面跑通）
│  └─ pagekit_dom_test.mjs 30 项页面脚本断言（jsdom）
├─ browser_profile/        专用浏览器配置（登录态在这里，勿删）
├─ download/               全文落盘目录
├─ out/                    报告与进度
└─ diag/                   诊断产物
```

## 十一、相关项目

- `E:\AI\cnki-helper` —— 同类需求的**油猴脚本**，适合你在电脑前手工操作时用：
  面板式筛选、结果表可排序、导出 7 种题录格式、半自动逐篇下载。两者可以配合：
  用 Agent 无人值守批量取，用油猴脚本现场补单篇。
- `E:\AI\thesis-assistant` —— 把下载下来的 PDF 丢进 `inbox\`，它做总结与跨文档对比。

## 十二、版本

**v1.0.0**
- 检索：多检索式、7 个检索字段、URL 直连 + 页内检索框兜底
- 对齐知网筛选栏：来源类别 / 年度区间 / 排序（尽力点 + 本地复核）
- 抓取：表头驱动列映射、翻页、判重、核心标识边界处理
- 筛选：年份/来源类别/文献类型/被引/下载/题名/正则/单刊上限/相关度
- 下载：详情页 PDF→CAJ 优选、文件落盘检测、PDF 头校验、可读文件名
- 无人值守：断点续跑、时间上限、单次/每小时配额、风控与掉登录自动停、NEEDS_HUMAN
- 诊断：一键 dump 页面结构，配合 `reprocess` 离线复算
