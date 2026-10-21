@echo off
rem BOSS岗位追踪 - 一键启动（需先 pip install -r requirements.txt）
cd /d "%~dp0"
echo 正在启动 BOSS岗位追踪 (http://127.0.0.1:8765) ...
python app.py
pause
