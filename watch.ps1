# 论文助手 · 四合一站点 —— 看门狗
#
# 为什么需要它：启动项（启动文件夹 / 计划任务）只在**登录时跑一次**。
# 服务如果中途崩溃、被系统回收、或者端口被别的程序抢走，就要等到下次登录才恢复——
# 用户打开浏览器只会看到"无法访问"，而且不知道发生了什么。
#
# 本脚本常驻后台，每隔一段时间做一次健康检查，不健康就重启服务并把原因记进日志。
# 它是**幂等**的：已经在跑的服务不会被重复启动（内部走 service.ps1 start -IfIdle）。
#
# 注意：本文件必须带 UTF-8 BOM。Windows PowerShell 5.1 对没有 BOM 的脚本
# 按 ANSI/GBK 解码，中文注释会被解坏，严重时直接解析失败。
#
# 用法：
#   powershell -File watch.ps1                 # 前台运行（调试用，Ctrl+C 停）
#   powershell -File watch.ps1 -IntervalSec 30 # 改检查间隔
#
# 正常用法是被启动项在登录时以隐藏窗口唤起（见 start_background.cmd）。

param(
    [int]$Port = 8878,
    [int]$IntervalSec = 60,
    # 健康检查连续失败几次才重启。默认 3 次，避免把一次网络抖动当成服务挂了。
    [int]$FailThreshold = 3
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

Write-WatchLog "看门狗启动（端口 $Port，间隔 ${IntervalSec}s，连续失败 $FailThreshold 次才重启）。"

# 启动时先确保服务在跑，不用等第一个检查周期。
try {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $servicePs1 start -IfIdle -Port $Port 2>&1 |
        ForEach-Object { Write-WatchLog "  启动：$_" }
} catch {
    Write-WatchLog "首次启动失败：$($_.Exception.Message)"
}

$fails = 0
while ($true) {
    Start-Sleep -Seconds $IntervalSec

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
    Start-Sleep -Seconds 10
    if (Test-Healthy) {
        Write-WatchLog "重启成功，服务已恢复。"
        $fails = 0
    } else {
        Write-WatchLog "重启后仍不健康，下一轮继续重试。"
        $fails = 0   # 归零，让它按完整阈值重新数，避免无限快速重启
    }
}
