@echo off
REM ============================================================
REM  Thesis Suite - one-click launcher (port 8848)
REM  Double-click this file to start the site.
REM
REM  Content is ASCII-only on purpose: cmd.exe reads .cmd files in
REM  the OEM codepage (936 on Chinese Windows), so UTF-8 Chinese
REM  text inside a .cmd would be decoded as garbage. The Chinese
REM  text you see in Explorer comes from the FILE NAME only.
REM
REM  A console window stays open while the site runs. Close it (or
REM  press Ctrl+C) to stop the site.
REM ============================================================
title Thesis Suite - http://127.0.0.1:8848/

cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONPATH=%~dp0;%~dp0vendor\thesisapp_stage\libs;E:\AI\aigc-reducer\libs;E:\AI\thesis-assistant\libs"

echo.
echo   Starting Thesis Suite...
echo   Site : http://127.0.0.1:8848/
echo   Docs : http://127.0.0.1:8848/api/docs
echo.
echo   Keep this window open. Press Ctrl+C to stop.
echo.

python -m suite --port 8848 --host 127.0.0.1 --no-browser

REM If python exited immediately, this pause keeps the window open
REM so the error message can actually be read.
echo.
echo   The site has stopped. Error output (if any) is above.
pause
