@echo off
rem 论文助手 · 四合一站点 —— 双击即可启动本机服务
setlocal
set HERE=%~dp0
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set PYTHONPATH=%HERE%
if exist "%HERE%vendor\thesisapp_stage\libs" set PYTHONPATH=%PYTHONPATH%;%HERE%vendor\thesisapp_stage\libs
if exist "E:\AI\aigc-reducer\libs" set PYTHONPATH=%PYTHONPATH%;E:\AI\aigc-reducer\libs
if exist "E:\AI\thesis-assistant\libs" set PYTHONPATH=%PYTHONPATH%;E:\AI\thesis-assistant\libs

python -m suite %*
endlocal
