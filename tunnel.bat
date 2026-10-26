@echo off
chcp 65001 >nul
rem 内网穿透：把本机看板开成公网 https 地址（手机在外面也能用）
rem 前提：看板已经在跑（另开一个窗口 start.bat）
cd /d "%~dp0"
echo.
echo === 内网穿透（cloudflared 快速隧道） ===
echo 关闭这个窗口 = 隧道断开（本机看板不受影响）
echo.
python tunnel.py
pause
