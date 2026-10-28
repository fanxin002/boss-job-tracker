@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Stop the background service. Data lives in data\ and is never touched.
rem Pure ASCII on purpose (see start.bat).

call "%~dp0findpy.cmd"
if not defined PY (
  echo Python not found.
  pause
  exit /b 1
)

%PY% serve.py stop
echo.
pause
endlocal
