# -*- coding: utf-8 -*-
"""桌面版入口（打包成 exe 后用这个）。

双击 exe 的行为：
  1. 已经在跑  → 直接开浏览器，不重复起服务
  2. 没在跑    → 后台拉起服务（脱离本进程），就绪后开浏览器，然后自己退出
  3. 之后的停止 → BossJobTracker.exe --stop（附带「停止服务.bat」）

其他参数：
  --server-worker  内部用：前台跑 HTTP 服务（被双击流程以 detached 方式拉起）
  --tunnel-worker  内部用：跑隧道。打包后 sys.executable 是 exe 自身，
                   不能再当 python 解释器去执行 tunnel.py，只能走这个分支
  --status         看一眼是否在跑
  --stop           停止服务

为什么要拆 worker：exe 里没有 Python 解释器，`[exe, "tunnel.py"]` 只会再启动一个自己。
"""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

HOST = "127.0.0.1"
PORT = 8765
BASE_URL = "http://%s:%d" % (HOST, PORT)

DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000


def _paths():
    import runtime_paths as rp
    return rp


def _init_console():
    """--noconsole 打包后 stdout/stderr 是 None，任何 print 都会炸；先兜住。

    从命令行（bash/PowerShell）调用时 stdout 存在但编码是 GBK，
    中文会变成乱码，所以顺手切到 UTF-8。
    """
    for s in (sys.stdout, sys.stderr):
        try:
            if s is not None:
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.stdout is None or sys.stderr is None:
        try:
            rp = _paths()
            os.makedirs(rp.LOG_DIR, exist_ok=True)
            f = open(os.path.join(rp.LOG_DIR, "launcher.log"), "a",
                     encoding="utf-8", errors="replace")
            if sys.stdout is None:
                sys.stdout = f
            if sys.stderr is None:
                sys.stderr = f
        except Exception:
            try:
                devnull = open(os.devnull, "w")
                sys.stdout = sys.stdout or devnull
                sys.stderr = sys.stderr or devnull
            except Exception:
                pass


def _say(msg):
    """有控制台才打印（双击时是无窗口的）"""
    try:
        print(msg)
        sys.stdout.flush()
    except Exception:
        pass


def port_busy():
    s = socket.socket()
    try:
        return s.connect_ex((HOST, PORT)) == 0
    finally:
        s.close()


def is_ours():
    try:
        with urllib.request.urlopen(BASE_URL + "/api/status", timeout=2) as r:
            data = r.read().decode("utf-8", "replace")
        return "boss_running" in data
    except Exception:
        return False


def page_ok():
    """服务活着还不够，页面得真能读出来才算健康。

    踩过的坑：单文件版进程常驻时，%TEMP%\\_MEIxxxx 被 Windows 存储感知清理，
    进程还在、/api/status 也正常，但 static/index.html 没了 —— 每个页面都 500。
    原来只查 /api/status 就判「已在运行」，用户双击后打开的就是 Internal Server Error。
    """
    try:
        req = urllib.request.Request(BASE_URL + "/", headers={"User-Agent": "BossJobTracker-health"})
        with urllib.request.urlopen(req, timeout=3) as r:
            body = r.read()
        return r.status == 200 and len(body) > 1024
    except Exception:
        return False


def open_browser():
    try:
        os.startfile(BASE_URL + "/")     # noqa: 仅 Windows
    except Exception:
        import webbrowser
        webbrowser.open(BASE_URL + "/")


def _spawn_self(flag):
    """用自身的 --server-worker 起一个脱离当前进程的服务"""
    rp = _paths()
    os.makedirs(rp.LOG_DIR, exist_ok=True)
    log = open(os.path.join(rp.LOG_DIR, "server.log"), "a", encoding="utf-8", errors="replace")
    log.write("\n===== 启动服务 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log.flush()
    try:
        # 源码模式：sys.executable 是 python.exe，必须把本脚本也传进去；
        # 冻结后 sys.executable 就是 exe 自身，参数直接接在后面。
        argv = [sys.executable]
        if not getattr(sys, "frozen", False):
            argv.append(os.path.abspath(__file__))
        argv.append(flag)
        return subprocess.Popen(argv, cwd=rp.STATE_DIR,
                                stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, close_fds=True,
                                creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW)
    finally:
        log.close()


def cmd_server_worker():
    """前台跑 HTTP 服务（被双击流程 detached 拉起）"""
    rp = _paths()
    os.makedirs(rp.LOG_DIR, exist_ok=True)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    _say("BossJobTracker 服务启动中… 数据目录 %s" % rp.DATA_DIR)
    import uvicorn
    import app as appmod
    uvicorn.run(appmod.app, host=HOST, port=PORT, log_level="info", access_log=False)
    return 0


def cmd_tunnel_worker():
    rp = _paths()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    os.chdir(rp.STATE_DIR)
    import tunnel
    return tunnel.main()


def cmd_status():
    if port_busy():
        _say("运行中：%s" % BASE_URL)
        return 0
    _say("未运行")
    return 1


def cmd_stop():
    """按端口找 PID 结束（不依赖 psutil）"""
    if not port_busy():
        _say("本来就没在运行")
        return 0
    pid = None
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, errors="ignore", timeout=15,
                             creationflags=CREATE_NO_WINDOW).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
                if parts[1].rsplit(":", 1)[-1] == str(PORT):
                    pid = int(parts[4])
                    break
    except Exception:
        pass
    if not pid:
        _say("没找到监听 %d 的进程" % PORT)
        return 1
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=20,
                       creationflags=CREATE_NO_WINDOW)
    except Exception:
        pass
    for _ in range(20):
        if not port_busy():
            break
        time.sleep(0.5)
    _say("已停止" if not port_busy() else "停止失败，去任务管理器结束 BossJobTracker")
    return 0 if not port_busy() else 1


def _is_onefile():
    """onefile 版解压到 %TEMP%\\_MEIxxxxx；目录版是 exe 旁边的 _internal。

    这个区别很重要：onefile 下父进程退出会清理自己的解压目录，
    再 spawn 一个自身当服务进程会互相干扰（子进程跑到一半就挂了），
    所以单文件版改成「当前进程直接跑服务」。
    """
    mp = getattr(sys, "_MEIPASS", "")
    return os.path.basename(mp).startswith("_MEI")


def cmd_launch_onefile():
    """单文件版：本进程常驻跑服务，另开线程等就绪后开浏览器"""
    import threading

    def _open_when_ready():
        for _ in range(90):
            if is_ours():
                open_browser()
                _say("已启动，浏览器已打开。停止：BossJobTracker.exe --stop")
                return
            time.sleep(0.5)
        _say("服务 45 秒内没就绪，看日志 logs\\server.log")

    threading.Thread(target=_open_when_ready, daemon=True).start()
    return cmd_server_worker()


def cmd_launch():
    """双击入口：能复用就复用，否则后台起一份，然后开浏览器"""
    if is_ours():
        if page_ok():
            _say("已在运行，直接打开 %s" % BASE_URL)
            open_browser()
            return 0
        # 服务活着但页面打不开（多半是 %TEMP% 下的 static 被系统清理了）
        # 这时候打开浏览器只会看到 500，先把它停掉重起一份干净的
        _say("检测到服务异常（页面文件缺失），正在自动重启…")
        cmd_stop()
        time.sleep(0.8)
    if port_busy():
        _say("端口 %d 被别的程序占用，无法启动" % PORT)
        return 1

    if _is_onefile():
        return cmd_launch_onefile()

    _say("正在启动…")
    proc = _spawn_self("--server-worker")
    deadline = time.time() + 40
    while time.time() < deadline:
        if is_ours():
            open_browser()
            _say("已启动（PID %d），浏览器已打开。停止：BossJobTracker.exe --stop" % proc.pid)
            return 0
        if proc.poll() is not None:
            rp = _paths()
            _say("启动失败，退出码 %s。日志：%s" % (proc.returncode,
                                                os.path.join(rp.LOG_DIR, "server.log")))
            return 1
        time.sleep(0.5)
    _say("40 秒内服务没就绪，稍等后重试；日志在 logs\\server.log")
    return 1


def main():
    _init_console()
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass

    args = sys.argv[1:]
    if "--server-worker" in args:
        return cmd_server_worker()
    if "--tunnel-worker" in args:
        return cmd_tunnel_worker()
    if "--status" in args:
        return cmd_status()
    if "--stop" in args:
        return cmd_stop()
    return cmd_launch()


if __name__ == "__main__":
    sys.exit(main() or 0)
