@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem Public tunnel: expose the local dashboard over https (usable on phone).
rem   tunnel.bat --token <ngrok_token>    one-time setup, then fixed URL
rem   tunnel.bat --provider cf           zero-config Cloudflare (URL changes)
rem   tunnel.bat --stop                   stop tunnel
rem Requires the dashboard to be running (start.bat in another window).
rem The tunnel is a foreground process: closing this window drops the tunnel,
rem the local dashboard keeps running.
rem Pure ASCII on purpose (see start.bat).

call "%~dp0findpy.cmd"
if not defined PY (
  echo Python not found.
  pause
  exit /b 1
)

%PY% serve.py status
if errorlevel 1 (
  echo.
  echo   [note] dashboard is NOT running - run start.bat first,
  echo          otherwise the public URL will not load.
  echo.
)

%PY% tunnel.py %*
echo.
pause
endlocal
