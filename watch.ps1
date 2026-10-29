# 论文助手 · 四合一站点 —— 看门狗
#
# 为什么需要它：启动项（启动文件夹 / 计划任务）只在**登录时跑一次**。
# 服务如果中途崩溃、被系统回收、或者端口被别的程序抢走，就要等到下次登录才恢复——
# 用户打开浏览器只会看到"无法访问"，而且不知道发生了什么。
#
# 本脚本常驻后台，做两件事：
#   1. 健康检查：不健康就重启（**15 秒一轮、1 次失败就动手**，最多丢 15 秒）；
#   2. 源码更新：检测 suite/ 与 tools/ 里 .py 的变化，变了就自动重启服务。
#      —— 这就是"实时更新"：改完代码不用再去敲命令重启，站点自己换到新版本。
#      （前端 web/ 不需要重启：它是每次请求从磁盘读的，改完刷新浏览器就是最新的。）
#
# 注意：本文件必须带 UTF-8 BOM。Windows PowerShell 5.1 对没有 BOM 的脚本
# 按 ANSI/GBK 解码，中文注释会被解坏，严重时直接解析失败。
#
# 用法：
#   powershell -File watch.ps1                  # 前台运行（调试用，Ctrl+C 停）
#   powershell -File watch.ps1 -IntervalSec 30  # 改检查间隔
#
# 正常用法是被启动项在登录时以隐藏窗口唤起（见 start_background.cmd）。

param(
    [int]$Port = 8878,
    # 检查间隔。默认 15 秒：这是"崩了多久用户才会觉得卡"的直接成本。
    [int]$IntervalSec = 15,
    # 健康检查连续失败几次才重启。默认 1 次——服务真的挂了就该立刻救，
    # 早先取 3 次（配合 60 秒间隔）意味着最长 3 分钟打不开，那个体验不可接受。
    [int]$FailThreshold = 1,
    # 是否监听源码变化并自动重启。设 0 可关掉（例如正在调试、不想被打断）。
    [int]$AutoReload = 1
)

$ErrorActionPreference = 'Continue'   # 看门狗不能因为单次检查出错就退出
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$logDir = Join-Path $here 'logs'
$watchLog = Join-Path $logDir 'watchdog.log'
$servicePs1 = Join-Path $here 'service.ps1'

if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

function Write-WatchLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    # 追加写。Add-Content 在 PS 5.1 下对 UTF-8 的处理取决于文件是否已存在，
    # 所以统一用 .NET 的 AppendAllText 并显式指定 UTF-8（无 BOM 追加）。
    try {
        [System.IO.File]::AppendAllText($watchLog, $line + [Environment]::NewLine,
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
    Write-Host $line
}

# 日志文件加个上限，不然长期运行会一直长。超过 1 MB 就截断到最近 200 行。
function Trim-LogIfHuge {
    param([string]$Path, [int]$MaxBytes = 1MB)
    try {
        if ((Test-Path $Path) -and ((Get-Item $Path).Length -gt $MaxBytes)) {
            $tail = Get-Content $Path -Tail 200
            [System.IO.File]::WriteAllLines($Path, $tail, (New-Object System.Text.UTF8Encoding($false)))
            Write-WatchLog "看门狗日志超过上限，已截断到最近 200 行。"
        }
    } catch { }
}

function Test-Healthy {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 8
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

# 后端源码指纹：只统计"改了必须重启才生效"的文件。
# 前端（web/*.js|css|html）**故意不算在内**——它们是每次请求从磁盘读的，
# 放进指纹会导致"改一行 CSS 就重启后端"，白白中断正在跑的生成任务。
function Get-SourceSignature {
    try {
        $roots = @((Join-Path $here 'suite'), (Join-Path $here 'tools'))
        $files = @()
        foreach ($r in $roots) {
            if (Test-Path $r) {
                $files += Get-ChildItem -Path $r -Recurse -File -Filter *.py -ErrorAction SilentlyContinue
            }
        }
        if ($files.Count -eq 0) { return '' }
        $parts = foreach ($f in $files) {
            '{0}:{1}:{2}' -f $f.FullName, $f.LastWriteTimeUtc.Ticks, $f.Length
        }
        $joined = ($parts | Sort-Object) -join '|'
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($joined)
        return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').Substring(0, 16)
    } catch {
        return ''
    }
}

Write-WatchLog "看门狗启动（端口 $Port，间隔 ${IntervalSec}s，连续失败 $FailThreshold 次重启，源码自动更新=$AutoReload）。"

# 启动时先确保服务在跑，不用等第一个检查周期。
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $servicePs1 start -IfIdle -Port $Port 2>&1 |
        ForEach-Object { Write-WatchLog "  启动：$_" }
} catch {
    Write-WatchLog "首次启动失败：$($_.Exception.Message)"
}

$fails = 0
$sig = Get-SourceSignature
Write-WatchLog "后端源码指纹：$sig"

while ($true) {
    Start-Sleep -Seconds $IntervalSec

    # ---- 1) 源码变化 → 自动重启（"实时更新"） ----
    if ($AutoReload -eq 1) {
        $now = Get-SourceSignature
        if ($now -and $sig -and $now -ne $sig) {
            Write-WatchLog "检测到后端源码变化（$sig → $now），自动重启服务以加载新代码。"
            try {
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $servicePs1 restart -Port $Port 2>&1 |
                    ForEach-Object { Write-WatchLog "  $_" }
            } catch {
                Write-WatchLog "自动重启失败：$($_.Exception.Message)"
            }
            # **先重新取指纹再继续**：重启期间文件可能还在写，
            # 若沿用旧基线会在下一轮再触发一次重启，白白多断一次服务。
            Start-Sleep -Seconds 3
            $sig = Get-SourceSignature
            $fails = 0
            continue
        }
        if ($now) { $sig = $now }
    }

    # ---- 2) 健康检查 ----
    if (Test-Healthy) {
        if ($fails -gt 0) { Write-WatchLog "服务已恢复健康（此前连续失败 $fails 次）。" }
        $fails = 0
        Trim-LogIfHuge -Path $watchLog
        continue
    }

    $fails++
    Write-WatchLog "健康检查失败（第 $fails/$FailThreshold 次）。"
    if ($fails -lt $FailThreshold) { continue }

    Write-WatchLog "达到阈值，尝试重启服务。"
    try {
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $servicePs1 restart -Port $Port 2>&1 |
            ForEach-Object { Write-WatchLog "  $_" }
    } catch {
        Write-WatchLog "重启失败：$($_.Exception.Message)"
    }
    # 重启后先观察一轮再计数，避免在服务启动期间误判。
    Start-Sleep -Seconds 6
    if (Test-Healthy) {
        Write-WatchLog "重启成功，服务已恢复。"
        $fails = 0
    } else {
        Write-WatchLog "重启后仍不健康，下一轮继续重试。"
        $fails = 0   # 归零，让它按完整阈值重新数，避免无限快速重启
    }
}
