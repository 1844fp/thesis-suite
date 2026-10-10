# 论文助手 · 四合一站点 —— 常驻服务管理
#
# 目的：让站点"随时打开就有"，而不用每次手动敲命令。
#
# 做法：登录时自动启动，**不装 Windows 服务**。
# 为什么不用服务（sc.exe / New-Service）：Windows 服务跑在 Session 0，
# 那里既没有用户环境也没有代理设置，报错还看不见；本项目的依赖有一部分来自
# 工作区里的 vendored libs 目录，用户登录后启动最贴近手动运行的环境，出问题也好查。
#
# 自启在 **install** 动作里落地，有两条路，脚本自己选能用的那条：
#   1) 计划任务（首选，最规范）—— 需要权限，被拒时自动降级；
#   2) 启动文件夹快捷方式（不需要管理员，效果一样是登录即启）。
#
# 用法：
#   .\service.ps1 install     装登录自启并立即启动
#   .\service.ps1 start       立即启动（带 --if-idle 时已是运行状态就什么都不做）
#   .\service.ps1 stop        停止
#   .\service.ps1 restart     重启
#   .\service.ps1 status      查看状态（含健康检查）
#   .\service.ps1 uninstall   卸载登录自启并停止
#   .\service.ps1 url         只打印访问地址
#
# 注意：本文件必须带 UTF-8 BOM。Windows PowerShell 5.1 对没有 BOM 的脚本
# 按 ANSI/GBK 解码，中文注释会被解坏，严重时直接解析失败。实测踩过多次——
# 每次用编辑工具改完这个文件，都要确认 BOM 还在。

param(
    [Parameter(Position = 0)]
    [ValidateSet('install', 'start', 'stop', 'restart', 'status', 'uninstall', 'url')]
    [string]$Action = 'status',

    [int]$Port = 8878,

    # 监听地址。默认只监听本机；改成 0.0.0.0 可让同一局域网的其他设备访问。
    [string]$Host_ = '127.0.0.1',

    # 幂等启动：已在运行就什么都不做。登录自启必须用它，
    # 否则每次登录都会再起一个进程，最后端口冲突、日志互相覆盖。
    [switch]$IfIdle
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = 'ThesisSuite'
$logDir = Join-Path $here 'logs'
$outLog = Join-Path $logDir 'service.out.log'
$errLog = Join-Path $logDir 'service.err.log'
$pidFile = Join-Path $logDir 'service.pid'

function Get-Listening {
    # 判断端口有没有进程在听。优先用 Get-NetTCPConnection（信息最全、能拿到 PID），
    # 它在个别系统/权限下可能不可用，所以退回 netstat 解析——不能因为一个 cmdlet
    # 不可用就把"服务在跑"误判成"没在跑"，进而重复启动。
    param([int]$P)
    try {
        $conns = Get-NetTCPConnection -LocalPort $P -State Listen -ErrorAction Stop
        if ($conns) { return $conns }
    } catch { }

    try {
        $lines = & netstat -ano -p tcp 2>$null | Select-String -Pattern "LISTENING\s+\d+\s*$" |
            Select-String -Pattern "[:.]$P\s"
        foreach ($line in $lines) {
            $parts = ($line.ToString().Trim() -split '\s+')
            $owner = [int]$parts[-1]
            return [pscustomobject]@{ LocalPort = $P; State = 'Listen'; OwningProcess = $owner }
        }
    } catch { }
    return @()
}

function Get-ServicePid {
    if (-not (Test-Path $pidFile)) { return $null }
    $raw = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $raw) { return $null }
    $procId = 0
    if (-not [int]::TryParse(($raw -replace '\D', ''), [ref]$procId)) { return $null }
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) { return $procId }
    return $null
}

function Show-Url {
    $hostForUrl = if ($Host_ -eq '0.0.0.0') { '<本机IP>' } else { $Host_ }
    Write-Host ""
    Write-Host "  访问地址 : http://${hostForUrl}:$Port/" -ForegroundColor Green
    Write-Host "  接口文档 : http://${hostForUrl}:$Port/api/docs"
    Write-Host "  健康检查 : http://${hostForUrl}:$Port/api/health"
    Write-Host ""
}

function Start-Suite {
    $existing = Get-Listening -P $Port
    if ($existing) {
        if ($IfIdle) {
            # 登录自启路径：已经在跑就静默退出。
            # 必须静默——自启脚本如果每次都弹一段输出，登录时会莫名多出一个窗口。
            Write-Host "已在运行（PID $($existing[0].OwningProcess)），跳过。" -ForegroundColor DarkGray
            exit 0
        }
        Write-Host "端口 $Port 已在监听（PID $($existing[0].OwningProcess)），不重复启动。" -ForegroundColor Yellow
        Show-Url
        return
    }

    New-Item -ItemType Directory -Force -Path $logDir | Out-Null

    # 日志与"一个 PowerShell 管道都不能用"的完整原因见 suite/__main__.py 的 _log_config。
    #
    # 一句话：`Start-Process` 带 `-RedirectStandardOutput` 时，.NET 会等子进程退出才返回
    # （`UseShellExecute=false` 的行为），而常驻服务永远不退出 → "启动"命令永远不返回。
    # 加 `-PassThru` 也一样持有输出句柄。所以这里一个重定向都不用，
    # 改由 Python 侧（uvicorn 的 logging dictConfig）自己写日志文件。
    # 顺带还避开了 PS 5.1 从管道读文本按 ANSI 解码、把中文日志写成乱码的问题。
    $env:SUITE_LOG_FILE = $outLog
    $psArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', (Join-Path $here 'run.ps1'),
        '--port', "$Port", '--host', $Host_, '--no-browser'
    )
    Start-Process -FilePath 'powershell.exe' -ArgumentList $psArgs `
        -WorkingDirectory $here -WindowStyle Hidden | Out-Null
    Remove-Item Env:\SUITE_LOG_FILE -ErrorAction SilentlyContinue

    Write-Host "已启动，等待就绪…" -ForegroundColor Cyan

    # 轮询健康检查：不能只看端口在监听——端口起来到应用就绪之间有一段窗口，
    # 这段时间里访问会得到连接错误，脚本必须等到真正可用再报成功。
    $ok = $false
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 750
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 5
            if ($r.StatusCode -eq 200) { $ok = $true; break }
        } catch { }
    }

    # 记下真正的监听进程 PID，供 stop 使用（没有 -PassThru 就拿不到进程对象）
    foreach ($conn in (Get-Listening -P $Port)) {
        Set-Content -Path $pidFile -Value $conn.OwningProcess -Encoding ascii
        break
    }

    if ($ok) {
        Write-Host "服务已就绪。" -ForegroundColor Green
        Show-Url
    } else {
        Write-Host "服务未能就绪。日志尾部：" -ForegroundColor Red
        foreach ($f in @($errLog, $outLog)) {
            if (Test-Path $f) {
                Write-Host "--- $f ---" -ForegroundColor DarkGray
                Get-Content $f -Tail 15 | ForEach-Object { Write-Host "   $_" }
            }
        }
        Write-Host ""
        Write-Host "常见原因：端口被占用或落在 Windows 排除端口段（会报 WinError 10013）。" -ForegroundColor Yellow
        Write-Host "换一个端口重试，例如：.\service.ps1 start -Port 8901" -ForegroundColor Yellow
    }
}

function Stop-Suite {
    $stopped = $false
    $procId = Get-ServicePid
    if ($procId) {
        # 只杀我们记下来的那棵树，不用 Stop-Process -Name python（会误伤别的 Python 程序）
        try {
            $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $procId" -ErrorAction SilentlyContinue
            foreach ($c in $children) { Stop-Process -Id $c.ProcessId -Force -ErrorAction SilentlyContinue }
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
            $stopped = $true
        } catch { }
    }
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }

    # 兜底：按端口找占用者（PID 文件可能因为强杀而失准）
    foreach ($conn in (Get-Listening -P $Port)) {
        try {
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            $stopped = $true
        } catch { }
    }

    if ($stopped) { Write-Host "已停止。" -ForegroundColor Green } else { Write-Host "本来就没在跑。" -ForegroundColor DarkGray }
}

function Show-Status {
    $conns = Get-Listening -P $Port
    $procId = Get-ServicePid
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue

    Write-Host ""
    Write-Host "  计划任务   : " -NoNewline
    if ($task) { Write-Host "已安装（$($task.State)）" -ForegroundColor Green } else { Write-Host "未安装" -ForegroundColor DarkGray }
    Write-Host "  监听端口   : " -NoNewline
    if ($conns) { Write-Host "$Port 有进程在听（PID $($conns[0].OwningProcess)）" -ForegroundColor Green }
    else { Write-Host "$Port 没有进程在听" -ForegroundColor Yellow }
    Write-Host "  记录的 PID : " -NoNewline
    if ($procId) { Write-Host $procId } else { Write-Host "无（或已退出）" -ForegroundColor DarkGray }

    if ($conns) {
        try {
            $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 8
            Write-Host "  健康检查   : 正常（运行 $($h.uptime_seconds) 秒，会话 $($h.sessions) 个）" -ForegroundColor Green
            Write-Host "  密钥封印   : server_key_present=$($h.server_key_present)  neutralized=$($h.key_fallback_neutralized -join ',')"
        } catch {
            Write-Host "  健康检查   : 失败 —— $($_.Exception.Message)" -ForegroundColor Red
        }
        Show-Url
    } else {
        Write-Host ""
        Write-Host "  启动：.\service.ps1 start" -ForegroundColor Cyan
    }
}

function Install-Autostart {
    # 登录时启动（不是开机）：本项目的依赖与配置都在用户环境里，开机时用户还没登录，
    # 那些 vendored libs 的路径与代理设置都还不存在，起不来。
    #
    # 两条路按可用性依次尝试：
    #   1) 计划任务——最规范，能配重试与"别开两个实例"；
    #   2) 启动文件夹——不需要管理员，效果一样是登录即启。
    # 两条都可能在受限环境里被拒（实测：沙箱/无管理员时 Register-ScheduledTask 报
    # "拒绝访问"，写启动文件夹报"对路径的访问被拒绝"）。全失败也不算白跑：
    # 把可以直接复制执行的命令打出来，用户自己粘一次就完成。
    $launcher = Join-Path $here 'start_background.cmd'

    # ---- 路线 1：计划任务 ----
    try {
        $action = New-ScheduledTaskAction -Execute $launcher -WorkingDirectory $here
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
            -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
            -MultipleInstances IgnoreNew

        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
            -Description '论文助手 · 四合一站点（登录时自动启动，监听本机端口）' -Force | Out-Null

        Write-Host "已安装登录自启（计划任务「$taskName」）。" -ForegroundColor Green
        Start-Suite
        return
    } catch {
        Write-Host "计划任务方式不可用（$($_.Exception.Message.Trim())），改用启动文件夹。" -ForegroundColor Yellow
    }

    # ---- 路线 2：启动文件夹 ----
    try {
        $startup = [Environment]::GetFolderPath('Startup')
        $target = Join-Path $startup 'ThesisSuite.cmd'
        # 放一个转发器而不是复制整个脚本：项目挪了位置、改了端口，只要改这里一处。
        $body = @(
            '@echo off',
            'rem Auto-generated by service.ps1 install. Safe to delete.',
            "call `"$launcher`""
        ) -join "`r`n"
        Set-Content -Path $target -Value $body -Encoding ascii
        Write-Host "已安装登录自启（启动文件夹：$target）。" -ForegroundColor Green
        Start-Suite
        return
    } catch {
        Write-Host "启动文件夹方式也不可用（$($_.Exception.Message.Trim())）。" -ForegroundColor Yellow
    }

    # ---- 都失败：给出可复制的命令，并说明手动做法 ----
    Write-Host ""
    Write-Host "两种自启方式都被当前权限拒绝。手动装一次即可，二选一：" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  A) 启动文件夹（推荐，不需要管理员）" -ForegroundColor White
    Write-Host "     把下面这行粘到一个「以管理员身份运行」的 PowerShell 里执行：" -ForegroundColor Gray
    Write-Host "     Copy-Item '$launcher' ([Environment]::GetFolderPath('Startup') + '\ThesisSuite.cmd')" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  B) 计划任务（管理员 PowerShell）" -ForegroundColor White
    Write-Host "     schtasks /create /tn ThesisSuite /tr `"$launcher`" /sc onlogon /f" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  不做自启也能用：每次开机后手动跑一次 .\service.ps1 start" -ForegroundColor Gray
    Write-Host ""
    Start-Suite
}

function Uninstall-Autostart {
    $removed = $false
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Write-Host "已移除计划任务。" -ForegroundColor Green
        $removed = $true
    }
    try {
        $target = Join-Path ([Environment]::GetFolderPath('Startup')) 'ThesisSuite.cmd'
        if (Test-Path $target) {
            Remove-Item $target -Force
            Write-Host "已移除启动文件夹项。" -ForegroundColor Green
            $removed = $true
        }
    } catch { }
    if (-not $removed) { Write-Host "没有安装登录自启。" -ForegroundColor DarkGray }
    Stop-Suite
}

switch ($Action) {
    'install'   { Install-Autostart }
    'start'     { Start-Suite }
    'stop'      { Stop-Suite }
    'restart'   { Stop-Suite; Start-Sleep -Seconds 2; Start-Suite }
    'status'    { Show-Status }
    'uninstall' { Uninstall-Autostart }
    'url'       { Show-Url }
}

# 显式归零退出码。
#
# 不这么做的话脚本会以 $LASTEXITCODE 退出——那是我们**中途调用的**某个外部命令
# （netstat / schtasks / powershell）留下的值，与本脚本成败无关。实测：
# `install` 明明成功启动了服务、也给出了降级说明，退出码却是 1，
# 会误导任何把它串进脚本的人（也包括我自己写测试的时候）。
exit 0
