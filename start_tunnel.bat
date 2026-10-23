@echo off
chcp 65001 >nul
cd /d %~dp0
echo 正在启动 Cloudflare 隧道（公网访问需要口令，见 data\access_key.txt）...
"C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe" tunnel.py
pause
