# 论文助手 · 四合一站点 —— 本机运行入口
#
# 本机开发时第三方依赖不在 site-packages 里，而是 vendored 在工作区的
# libs\ 目录下（沿用既有项目的做法，避免重复安装）。公网容器里依赖由
# pip 安装，届时本脚本的 PYTHONPATH 追加部分是空操作。
#
# 注意：本文件必须带 UTF-8 BOM。Windows PowerShell 5.1 对没有 BOM 的脚本
# 按 ANSI/GBK 解码，中文注释会被解坏，严重时直接解析失败。实测踩过一次。
#
# 日志：设了 SUITE_LOG 环境变量时，把输出同时写到该文件。
# 之所以让脚本自己写日志、而不是由父进程重定向：Start-Process 一旦带上
# -RedirectStandardOutput 就会持有管道句柄，父进程会一直等到子进程退出——
# 而这是一个常驻服务，永远不退出，于是"启动"命令永远不返回。实测踩过。
param(
    [switch]$NoLog
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$libs = @(
    (Join-Path $here "vendor\thesisapp_stage\libs"),
    "E:\AI\aigc-reducer\libs",
    "E:\AI\thesis-assistant\libs"
) | Where-Object { Test-Path $_ }

$env:PYTHONPATH = (@($here) + $libs) -join ";"

if ($env:SUITE_LOG -and -not $NoLog) {
    $logDir = Split-Path -Parent $env:SUITE_LOG
    if ($logDir -and -not (Test-Path $logDir)) {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    }
    # 2>&1 让 Python 的 stderr（uvicorn 的日志就走 stderr）也进同一个文件
    & python -m suite @args *>&1 | Tee-Object -FilePath $env:SUITE_LOG -Append
    exit $LASTEXITCODE
}

& python -m suite @args
exit $LASTEXITCODE
