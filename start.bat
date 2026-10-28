@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Double click = start in background (keeps running after this window closes).
rem Can also be used from a terminal:
rem   start.bat             start
rem   start.bat status      show status
rem   start.bat logs -f     follow log
rem   start.bat restart     restart
rem All user-facing messages come from serve.py (UTF-8), so this file
rem stays pure ASCII -- cmd.exe parses .bat as ANSI and mangles UTF-8.

call "%~dp0findpy.cmd"
if not defined PY (
  echo.
  echo   Python not found. Install Python 3.10+ and tick
  echo   "Add python.exe to PATH", then run this file again.
  echo.
  pause
  exit /b 1
)

if "%~1"=="" (
  %PY% serve.py start
) else (
  %PY% serve.py %*
)

echo.
pause
endlocal
