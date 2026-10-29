@echo off
rem ===========================================================================
rem  Logon launcher for the Thesis Suite site.
rem
rem  Kept pure ASCII on purpose: cmd.exe mis-decodes UTF-8 Chinese comments on a
rem  GBK console and can swallow a "rem" prefix, turning the comment into a
rem  command. That bit us once already in run.cmd.
rem
rem  What it does: starts watch.ps1, which health-checks the site every 15s
rem  (restarts on the first failure) and also restarts the service when backend
rem  .py files change, so code edits take effect without a manual restart.
rem
rem  An earlier version started the service only
rem  once, which left a real gap - if the service died later (crash, port
rem  stolen) nothing brought it back until the next logon, and the user simply
rem  saw "cannot connect".
rem
rem  Why `start /b`: watch.ps1 never exits. Launching it inline made this batch
rem  file block forever (measured: it sat for the full 600s timeout of the
rem  calling shell). `start /b` returns immediately, and because it does not
rem  create a new console the launcher's console is not held open either.
rem
rem  Port is read from SUITE_PORT; the default is pinned to 8848 below.
rem  To stop everything: run service.ps1 stop, then end the watch.ps1 process.
rem ===========================================================================
setlocal

set "HERE=%~dp0"
rem Default pinned to 8848: that is the URL the user actually opens
rem (http://127.0.0.1:8848/). It used to default to 8878, so the logon launcher
rem guarded 8878 while the browser pointed at 8848 - and 8848 had no watchdog at
rem all, so it silently died and looked like "the site keeps disappearing".
rem service.ps1's own default stays 8878; only this launcher is pinned.
if "%SUITE_PORT%"=="" set "SUITE_PORT=8848"

if not exist "%HERE%watch.ps1" (
  echo [ERROR] watch.ps1 not found next to this launcher: %HERE%watch.ps1
  exit /b 2
)

rem Why the redirection to nul matters: watch.ps1 never exits, and a child that
rem inherits this console keeps the launcher's stdout/stderr handles open, so the
rem calling shell waits for it. Measured twice: the command sat until its full
rem timeout (300s/600s) even though the site was already up and serving.
rem Redirecting to nul breaks that inheritance, so the launcher returns at once.
rem watch.ps1 writes its own log file (logs\watchdog.log), so nothing is lost.
start "ThesisSuiteWatchdog" /b powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%HERE%watch.ps1" -Port %SUITE_PORT% >nul 2>&1

endlocal
exit /b 0
