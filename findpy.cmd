@echo off
rem ---------------------------------------------------------------
rem Shared helper: locate a python interpreter on PATH and export PY.
rem Only needs to be able to run serve.py -- serve.py itself picks
rem the interpreter that can actually run the project.
rem Usage inside another bat:  call "%~dp0findpy.cmd"
rem Keep this file pure ASCII: cmd.exe parses .bat/.cmd as ANSI
rem (GBK on zh-CN), UTF-8 Chinese text would be executed as garbage.
rem ---------------------------------------------------------------
set "PY="
where python >nul 2>nul
if %errorlevel%==0 set "PY=python"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py -3"
)
if not defined PY exit /b 1
exit /b 0
