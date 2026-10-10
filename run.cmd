@echo off
rem ===========================================================================
rem  Thesis Suite launcher (ASCII only, on purpose)
rem
rem  This file is deliberately pure ASCII. Earlier it carried Chinese comments,
rem  and cmd.exe mis-decoded them (the file is UTF-8, cmd probes GBK), which ate
rem  the "rem" prefix on one line and made cmd try to run the comment as a
rem  command:  '...' is not recognized as an internal or external command
rem
rem  All the real logic -- and all Chinese text -- lives in run.ps1, which
rem  handles UTF-8 correctly. This file only locates the script and calls it.
rem ===========================================================================
setlocal
set "HERE=%~dp0"

if not exist "%HERE%run.ps1" (
  echo [ERROR] run.ps1 not found next to this launcher: %HERE%run.ps1
  exit /b 2
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%HERE%run.ps1" %*
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" (
  echo.
  echo [ERROR] launcher exited with code %RC%
  pause
)

endlocal & exit /b %RC%
