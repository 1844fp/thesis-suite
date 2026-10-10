# 论文助手 · 四合一站点 —— 本机运行入口
#
# 本机开发时第三方依赖不在 site-packages 里，而是 vendored 在工作区的
# libs\ 目录下（沿用既有项目的做法，避免重复安装）。公网容器里依赖由
# pip 安装，届时本脚本的 PYTHONPATH 追加部分是空操作。
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

& python -m suite @args
exit $LASTEXITCODE
