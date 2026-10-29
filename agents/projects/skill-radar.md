<!--
  本文件是从本机项目 skill-radar 的 README.md **原样转载**的。
  转载目的：把散落在各项目的说明集中到一处索引，便于查阅。
  正文未经修改；如果这里的内容与该项目目录里的 README.md 不一致，
  以项目目录里的为准（那份是随代码演进的）。

  来源：E:\AI\skill-radar\README.md
  收录时间：2026-10-29
-->

> **本文档为转载**，来源 `E:\AI\skill-radar\README.md`（AI 写作 skill / 工具雷达 · 抓 GitHub、排序、生成离线仪表盘）。正文未作修改。

---

# AI 写作 Skill / 工具雷达 (skill-radar)

一个本地运行的采集助手：自动从 GitHub 抓取 **AI 写作相关的 skill 包、Agent、提示词库、写作工具**，
补全真实热度信号，按 **星数 / 关注度 / 热度** 排序，生成一个**单文件、离线可用、可排序可筛选**的网页仪表盘。

不依赖任何第三方 Python 包（纯标准库），不联网除 GitHub API 之外的任何服务，页面无 CDN。

---

## 快速开始

```powershell
# 1) 配置 Token（可选但强烈建议；不配也能跑，只是搜索限速 10 次/分钟）
copy .env.example .env
notepad .env          # 填入 GITHUB_TOKEN=...

# 2) 一条命令搞定：采集 + 生成网页 + 打开浏览器
python app\radar.py update
```

之后双击 `run.cmd` 即可一键更新并打开。

---

## 它到底怎么判断“关注度”

`星数` 是总量，`关注度` 是**最近在涨**。本工具不用任何需要额外权限的接口，只用公开只读数据：

| 信号 | 来源 | 说明 |
|---|---|---|
| 星数 | 仓库对象 | 总量人气 |
| **近 30 天涨星** | 本地快照差分 | 每次运行都会存一份星数快照，积累两次以上即为**实测值**（最硬信号） |
| 生涯日均涨星 | 星数 ÷ 仓库年龄 | 首次运行时的兜底势头口径 |
| 近 30 天新增 Fork | `/repos/{o}/{r}/forks?sort=newest` | 真实近期扩散度 |
| 近 30 天提交数 | `/repos/{o}/{r}/commits?since=` | 项目是否还在维护 |
| Watcher 数 | 仓库对象 `subscribers_count` | 深度关注（比星更“贵”） |
| 推送新鲜度 | `pushed_at` | 活跃度衰减 |

> 注：细粒度 PAT 通常拿不到 `starring:read` 权限，所以 GitHub 官方「带时间戳的 stargazers」用不了。
> 本工具改用 **自建快照时间序列**，跑得越久越准；首次运行会明确标注使用的是估算口径。

### 热度分公式（页面里也逐项可视化）

```
热度 = 人气(星数)      28%
     + 势头(涨星)      30%
     + 参与(fork/watch) 14%
     + 活跃(提交/推送)  18%
     + 写作相关度       10%
```

`关注度` 只由 **势头 55% + 活跃 25% + 参与 20%** 构成 —— 回答“现在谁在被关注”，与“历史总量”无关。
两个排序维度都可在表头点击切换。

---

## 常用命令

```powershell
python app\radar.py update                 # 采集 + 生成 + 打开（日常用这个）
python app\radar.py update --no-open       # 同上，不开浏览器
python app\radar.py collect                # 只采集
python app\radar.py collect --min-stars 5  # 放宽门槛，收录更多新项目
python app\radar.py collect --pages 2      # 每个查询翻 2 页（最多 200 条/查询）
python app\radar.py collect --no-enrich    # 快速模式，跳过信号补全（约 1 分钟）
python app\radar.py build --open           # 数据没变，只重新出网页
python app\radar.py top -n 25              # 终端排行（默认按热度）
python app\radar.py top --by attention     # 按关注度排行
python app\radar.py top --by star_delta_30d
python app\radar.py search 论文            # 在已收录数据里搜
python app\radar.py doctor                 # 检查 Token / 配额 / 快照数
```

---

## 网页仪表盘能做什么

- 点表头任意列排序（星数 / 关注度 / 热度 / 30 天涨星 / 日均涨星 / Fork / 提交 / 推送时间 …）
- 按 **分类**（Skill 包、学术论文、去 AI 味、自媒体、小说、MCP、合集 …）点选筛选
- 关键词搜索（仓库名 / 描述 / topic）、语言筛选、最低星数、仅近 30 天活跃、仅中文项目
- 点任意一行展开：**评分分解条形图**、元数据、命中来源查询、一键复制 `git clone`
- 导出 **CSV / JSON**，或一键复制 **Markdown 榜单**（方便贴进笔记 / 报告）
- 完全离线：HTML 单文件，无 CDN、无外部字体、无头像请求

输出位置：

```
site/index.html          # 仪表盘（单文件，可直接分享）
reports/radar-YYYYMMDD.md # 同数据的 Markdown 榜单
data/repos.json          # 结构化数据（喂给别的程序）
data/snapshots/          # 星数快照时间序列
```

---

## 项目结构

```
skill-radar/
├─ app/
│  ├─ gh.py          # GitHub REST 客户端：Token 加载、限速滑动窗口、磁盘缓存、重试
│  ├─ collect.py     # 搜索 → 去重 → 补全 → 快照差分 → 分类评分 → data/repos.json
│  ├─ score.py       # 相关性分类器 + 热度/关注度评分（关键词权重表 + 假朋友词抑制）
│  ├─ build_site.py  # 渲染单文件 HTML + Markdown 报告
│  ├─ template.html  # 仪表盘模板（原生态 JS，无框架）
│  └─ radar.py       # CLI 入口
├─ config/queries.json  # 46 条搜索策略 + 精选种子库 + 门槛
├─ tools/
│  ├─ validate_site.py    # 校验：数据块 JSON、JS 语法、DOM id、分类分布
│  ├─ smoke_dom.js        # 用最小 DOM 桩在 Node 里跑一遍页面渲染，断言输出 HTML
│  └─ inspect_quality.py  # 人工抽查分类/相关度质量
├─ data/                # 数据与缓存
├─ site/                # 生成的仪表盘
└─ reports/             # 生成的 Markdown 榜单
```

### 自检

改完代码后跑一遍，确认没有把页面改坏：

```powershell
python app\radar.py collect --no-enrich    # 快速造数据
python app\build_site.py
python tools\validate_site.py              # 结构校验
node tools\smoke_dom.js                    # 渲染冒烟测试
python tools\inspect_quality.py heat 30    # 抽查分类质量
```

## 自定义

- **加搜索词**：编辑 `config/queries.json` 的 `queries`，支持 GitHub 搜索语法（`topic:`、`in:name,description` 等）。
- **加精选项目**：往 `seeds` 里加 `owner/repo`，一定会被收录并标记来源为「精选种子」。
- **改评分权重**：`app/score.py` 的 `score()` 末尾，权重和可直接调。
- **改分类规则**：`app/score.py` 的 `_CATEGORY_RULES`，自上而下首个命中生效。

## 说明

- 结果只反映 GitHub 公开数据，`★` 高 ≠ 写作质量高；分类为关键词启发式判定，可能有偏差。
- 搜索配额：认证 30 次/分钟、5000 次/小时。默认 30 条查询 × 1 页 ≈ 30 次搜索请求，安全。
  重复运行会命中磁盘缓存（`data/cache/`，TTL 1 小时），基本不消耗配额。
- `.env` 已在 `.gitignore` 中；**请勿把 Token 提交到任何仓库**。
