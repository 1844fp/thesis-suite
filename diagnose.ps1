# 论文助手 · 四合一站点 —— 一键诊断
#
# 用途：站点打不开时，先跑这个，它会一次性把"该看的东西"全列出来，
# 而不是让你逐个去猜（服务没起？端口被占？看门狗死了？启动项没装？）。
#
# 用法：
#   .\diagnose.ps1
#
# 本文件必须带 UTF-8 BOM（见 tools/check_encodings.py 的说明）。

param([int]$Port = 8878)

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ok = 0
$bad = 0

function Section { param([string]$T) Write-Host ""; Write-Host "── $T " -ForegroundColor Cyan; Write-Host ("─" * 60) -ForegroundColor DarkGray }
function Good { param([string]$T) Write-Host "  [OK]   $T" -ForegroundColor Green; $script:ok++ }
function Bad  { param([string]$T) Write-Host "  [!!]   $T" -ForegroundColor Red; $script:bad++ }
function Info { param([string]$T) Write-Host "         $T" -ForegroundColor Gray }

Write-Host ""
Write-Host "论文助手 · 四合一站点 —— 诊断" -ForegroundColor White
Write-Host ("=" * 62) -ForegroundColor DarkGray

# ---- 1. 文件是否齐全 ----
Section "文件"
foreach ($f in @('suite\api\app.py', 'vendor\thesisapp_stage\app\server.py',
                 'vendor\topichelper_stage\core\generate.py', 'web\index.html',
                 'run.ps1', 'service.ps1', 'watch.ps1', 'start_background.cmd')) {
    $p = Join-Path $here $f
    if (Test-Path $p) { Good $f } else { Bad "$f 缺失" }
}

# ---- 2. 依赖能否导入（这是最常见的"起不来"原因）----
Section "运行环境"
$env:PYTHONUTF8 = '1'
# 注意：这里只用 PS 5.1 也有的写法。开发机上默认 shell 是 Windows PowerShell 5.1，
# 用 Join-String（PowerShell 7 才有）会直接报错，而诊断脚本本身报错最讽刺。
$libCandidates = @(
    $here,
    (Join-Path $here 'vendor\thesisapp_stage\libs'),
    'E:\AI\aigc-reducer\libs',
    'E:\AI\thesis-assistant\libs'
) | Where-Object { Test-Path $_ }
$env:PYTHONPATH = ($libCandidates -join ';')

$probe = & python -c "import sys; sys.path.insert(0, r'$here'); from suite import boot; boot.prepare(); d=boot.describe(); print('OK' if all(v['ok'] for v in d['packages'].values()) else 'FAIL')" 2>&1
if ($probe -match 'OK') {
    Good "两套上游代码都能导入"
    $libs = $libCandidates | Where-Object { $_ -like '*libs*' }
    Info ("依赖来源：" + ($libs -join '、'))
} else {
    Bad "上游代码导入失败"
    Info (($probe | Select-Object -Last 3) -join ' / ')
}

# ---- 3. 端口与进程 ----
Section "端口 $Port"
$conns = @()
try { $conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop } catch { }
if (-not $conns) {
    # Get-NetTCPConnection 在部分环境里不可靠，退回 netstat
    $ns = & netstat -ano -p tcp 2>$null | Select-String -Pattern "[:.]$Port\s+.*LISTENING"
    if ($ns) { $conns = @([pscustomobject]@{ OwningProcess = [int](($ns[0].ToString().Trim() -split '\s+')[-1]) }) }
}
if ($conns) { Good "端口 $Port 有进程在听（PID $($conns[0].OwningProcess)）" } else { Bad "端口 $Port 没有进程在听" }

# ---- 4. 健康检查 ----
Section "服务健康"
try {
    $h = Invoke-RestMethod "http://127.0.0.1:$Port/api/health" -TimeoutSec 8
    Good "健康检查通过（运行 $($h.uptime_seconds) 秒，会话 $($h.sessions) 个）"
    foreach ($k in $h.packages.PSObject.Properties) {
        if ($k.Value.ok) { Good "上游包 $($k.Name) 已装载" } else { Bad "上游包 $($k.Name) 装载失败：$($k.Value.error)" }
    }
    if ($h.server_key_present -eq $false) { Good "服务器上不存在可被借用的密钥（BYOK 防线生效）" }
    else { Bad "检测到服务器密钥！BYOK 防线可能失效" }
    Info "已中立化的回退：$($h.key_fallback_neutralized -join '、')"
} catch {
    Bad "健康检查失败：$($_.Exception.Message)"
    Info "站点当前打不开。往下看『怎么启动』。"
}

# ---- 5. 看门狗 ----
Section "看门狗"
$w = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*watch.ps1*' }
if ($w) { Good "看门狗在运行（PID $($w.ProcessId -join ',')）" }
else { Bad "看门狗没在运行（服务崩溃后不会自动恢复）" }
$wl = Join-Path $here 'logs\watchdog.log'
if (Test-Path $wl) {
    Info "最近 3 条："
    Get-Content $wl -Encoding UTF8 -Tail 3 | ForEach-Object { Write-Host "           $_" -ForegroundColor DarkGray }
} else { Info "还没有看门狗日志" }

# ---- 6. 登录自启 ----
Section "登录自启"
$startup = [Environment]::GetFolderPath('Startup')
$entry = Join-Path $startup 'ThesisSuite.cmd'
if (Test-Path $entry) {
    Good "启动文件夹已装：$entry"
} else {
    Bad "启动文件夹未装自启"
    Info "手动装一次（不需要管理员）："
    Info "  Copy-Item '$here\start_background.cmd' ([Environment]::GetFolderPath('Startup') + '\ThesisSuite.cmd')"
}
$task = Get-ScheduledTask -TaskName 'ThesisSuite' -ErrorAction SilentlyContinue
if ($task) { Good "计划任务已装（$($task.State)）" } else { Info "计划任务未装（有启动文件夹就够了）" }

# ---- 7. 结论 ----
Write-Host ""
Write-Host ("=" * 62) -ForegroundColor DarkGray
Write-Host "  通过 $ok 项，问题 $bad 项" -ForegroundColor White
Write-Host ""
Write-Host "  访问地址 : http://127.0.0.1:$Port/" -ForegroundColor Green
Write-Host ""
if ($bad -gt 0) {
    Write-Host "  怎么启动 / 恢复：" -ForegroundColor Yellow
    Write-Host "    cd $here"
    Write-Host "    .\service.ps1 start      # 只启动服务"
    Write-Host "    .\start_background.cmd   # 启动看门狗（推荐，崩溃会自动恢复）"
    Write-Host ""
}
Write-Host "  日志：" -ForegroundColor DarkGray
Write-Host "    logs\service.out.log     服务运行日志（uvicorn）"
Write-Host "    logs\watchdog.log        看门狗日志（重启记录）"
Write-Host ""
