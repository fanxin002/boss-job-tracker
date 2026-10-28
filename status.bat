@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Show whether the service is running: PID, URL, enabled platforms, log path.
rem Pure ASCII on purpose (see start.bat).

call "%~dp0findpy.cmd"
if not defined PY (
  echo Python not found.
  pause
  exit /b 1
)

%PY% serve.py status
echo.
pause
endlocal
