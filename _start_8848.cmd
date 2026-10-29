@echo off
REM Thesis Suite launcher for port 8848 (used by the Windows scheduled task).
REM
REM Why this file exists: schtasks /tr accepts at most 261 characters, and the
REM full command (PYTHONPATH + env vars + python -m suite) is much longer.
REM Moving it into a .cmd leaves /tr as a single short path.
REM
REM ASCII-only on purpose: cmd.exe reads .cmd files in the OEM codepage (936 on
REM Chinese Windows), so UTF-8 Chinese comments here would be decoded as garbage.
REM
REM The order of PYTHONPATH entries MUST NOT change: the vendored libs have to
REM come before site-packages, otherwise the outdated typing_extensions in D:\py
REM wins and anyio fails to import.
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "SUITE_LOG_FILE=%~dp0logs\site-8848.log"
set "PYTHONPATH=%~dp0;%~dp0vendor\thesisapp_stage\libs;E:\AI\aigc-reducer\libs;E:\AI\thesis-assistant\libs"
python -m suite --port 8848 --host 127.0.0.1 --no-browser
