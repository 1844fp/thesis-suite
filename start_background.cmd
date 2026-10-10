@echo off
rem ===========================================================================
rem  Background launcher for the Thesis Suite site.
rem
rem  Kept pure ASCII on purpose: cmd.exe mis-decodes UTF-8 Chinese comments on a
rem  GBK console and can swallow a "rem" prefix, turning the comment into a
rem  command. That bit us once already in run.cmd.
rem
rem  Used by:
rem    - Task Scheduler at logon (service.ps1 install)
rem    - the Startup folder fallback
rem  Both need something that returns immediately, so this only starts the
rem  service in the background and exits.
rem
rem  Port is read from SUITE_PORT (default 8878). Override by setting that
rem  variable before calling, or edit the default below.
rem ===========================================================================
setlocal

set "HERE=%~dp0"
if "%SUITE_PORT%"=="" set "SUITE_PORT=8878"

if not exist "%HERE%run.ps1" (
  echo [ERROR] run.ps1 not found next to this launcher: %HERE%run.ps1
  exit /b 2
)

rem --IfIdle keeps this idempotent: running it twice (logon + manual) must not
rem start a second copy that fights over the port.
rem NOTE: it must be -IfIdle (PowerShell syntax), not --if-idle. PowerShell's
rem param binder rejects the double-dash form with "A parameter cannot be found".
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%service.ps1" start -IfIdle -Port %SUITE_PORT%

endlocal
exit /b 0
