# -*- coding: utf-8 -*-
"""BOSS岗位追踪 - 统一启动管理（start / stop / restart / status / logs / open）

为什么不直接在 bat 里写 `python app.py`：
  1. 本机 PATH 里的 python 是 3.8，而 app.py 需要 3.10+ 和 pydantic v2，
     双击 start.bat 会直接崩在 import 上，报错还看不懂；
  2. 前台跑会一直占着一个黑窗口，关窗口 = 服务停，容易被误关；
  3. 三份 bat 各自写死解释器路径（tunnel 用裸 python、start_tunnel 写死
     一长串绝对路径），换机器就废。

所以统一收口到这里：bat 只负责用"任意能跑起来的 python"调本脚本，
由本脚本去找真正能跑 app 的解释器、后台拉起、探活、打开浏览器。

用法：
  python serve.py start           后台启动（不留窗口），就绪后自动开浏览器
  python serve.py start --fg      前台启动（调试用，Ctrl+C 停）
  python serve.py start --no-open 后台启动但不打开浏览器
  python serve.py stop            停止
  python serve.py restart         重启
  python serve.py status          查看是否在跑、PID、依赖是否齐
  python serve.py logs [-f]       看日志（-f 持续跟随）
  python serve.py open            打开看板

兼容 Python 3.8 起（本脚本自身只做进程管理，不 import 项目依赖）。
"""
import argparse
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"
PORT = 8765
BASE_URL = "http://%s:%d" % (HOST, PORT)
LOG_DIR = os.path.join(ROOT, "logs")
LOG_FILE = os.path.join(LOG_DIR, "server.log")
PID_FILE = os.path.join(ROOT, "data", "server.pid")
PY_CACHE = os.path.join(ROOT, "data", "python_exe.txt")   # 上次跑通的解释器，优先复用
REQ_FILE = os.path.join(ROOT, "requirements.txt")

# 后台启动：脱离当前控制台（关掉 bat 窗口也不影响），且不弹新窗口
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000   # 父进程被塞进 Job（IDE/CI/终端）时也能脱离

CORE_MODULES = ("fastapi", "uvicorn", "apscheduler")   # 缺了 app 起不来
OPTIONAL_MODULES = {"DrissionPage": "浏览器抓取", "fitz": "PDF 简历解析",
                    "docx": "Word 简历解析", "requests": "推送"}
MIN_PY = (3, 10)


# ---------------- 输出 ----------------
def _init_stdout():
    """Windows 控制台默认 GBK，中文可能乱码；bat 里已 chcp 65001，这里再兜一层"""
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def info(msg):
    print(msg)


def warn(msg):
    print("  ! " + msg)


def die(msg, code=1):
    print("\n  ✗ " + msg)
    sys.exit(code)


# ---------------- 进程 / 端口 ----------------
def find_listener(port=PORT):
    """返回监听该端口的 PID，没有则 None。用 netstat 解析，不依赖 psutil。"""
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, errors="ignore",
                             timeout=15).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                try:
                    return int(parts[4])
                except ValueError:
                    pass
    return None


def http_get(path, timeout=2.0):
    """本机直连（回环地址免口令）。返回解析后的 JSON 或 None。"""
    try:
        with urllib.request.urlopen(BASE_URL + path, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
        return json.loads(raw)
    except Exception:
        return None


def is_ours(port=PORT):
    """确认端口上跑的确实是本项目，而不是刚好占了 8765 的别的程序"""
    st = http_get("/api/status")
    return isinstance(st, dict) and "boss_running" in st and "platforms" in st


def kill_pid(pid):
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, errors="ignore", timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def read_pid():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return None


def write_pid(pid):
    try:
        os.makedirs(os.path.dirname(PID_FILE), exist_ok=True)
        with open(PID_FILE, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except Exception:
        pass


def clear_pid():
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def pid_alive(pid):
    """tasklist 查进程是否还在（不依赖 psutil）"""
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, text=True, errors="ignore",
                             timeout=15).stdout
        return str(pid) in out
    except Exception:
        return False


# ---------------- 解释器探测 ----------------
# 注意顺序：必须先 import 再判版本。之前写成 sys.exit(...) 在前，
# 结果 import 永远执行不到，任何版本够的解释器都被判成"依赖齐全"。
_PROBE = ("import %s;"
          "import sys;"
          "sys.exit(0 if sys.version_info>=%s else 3)") % (",".join(CORE_MODULES), repr(MIN_PY))


def _works(args):
    """这个解释器能跑 app 吗（版本够 + 依赖齐）"""
    try:
        r = subprocess.run(list(args) + ["-c", _PROBE],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def _py_launcher_candidates():
    """`py -0p` 列出已安装的所有 Python 解释器路径（3.13 在前）"""
    out = []
    try:
        r = subprocess.run(["py", "-0p"], capture_output=True, text=True,
                           errors="ignore", timeout=15)
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or not line.lower().endswith(".exe"):
                continue
            path = line.split()[-1]
            if os.path.exists(path):
                out.append([path])
    except Exception:
        pass
    out.sort(key=lambda p: p[0], reverse=True)   # 路径里带版本号，倒序即高版本优先
    return out


def _glob_candidates():
    pats = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Programs\Python\Python3*\python.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Python3*\python.exe"),
        os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), r"Python\python.exe"),
        # WorkBuddy 自带的托管环境（本机实测唯一装齐依赖的就是它），仅作兜底
        os.path.join(os.path.expanduser("~"),
                     r".workbuddy\binaries\python\envs\default\Scripts\python.exe"),
    ]
    out = []
    for p in pats:
        for hit in sorted(glob.glob(p), reverse=True):
            out.append([hit])
    return out


def find_python(verbose=False):
    """按优先级找一个能跑 app 的解释器，返回命令行 list（如 [python.exe] 或 ['py','-3']）

    顺序：项目 .venv → 上次跑通的缓存 → 当前解释器 → py -3 → 已安装解释器列表
    → 常见安装路径。本机实测只有 WorkBuddy 自带的 venv 装齐依赖，它排在最后兜底；
    第一次探测要试几个候选（几秒），成功后写进 data/python_exe.txt，下次直接命中。
    """
    cached = _read_py_cache()
    cands = [[os.path.join(ROOT, r".venv\Scripts\python.exe")]]
    if cached:
        cands.append([cached])
    cands += [[sys.executable],
              ["py", "-3"]]
    cands += _py_launcher_candidates()
    cands += _glob_candidates()
    seen = set()
    for args in cands:
        key = tuple(args)
        if key in seen:
            continue
        seen.add(key)
        if len(args) == 1 and not os.path.exists(args[0]):
            continue
        if _works(args):
            if verbose:
                info("  解释器：%s" % subprocess.list2cmdline(args))
            if args != [os.path.join(ROOT, r".venv\Scripts\python.exe")]:
                _write_py_cache(args)
            return args
    return None


def _read_py_cache():
    try:
        with open(PY_CACHE, encoding="utf-8") as f:
            p = f.read().strip()
        return p if p and os.path.exists(p) else ""
    except OSError:
        return ""


def _write_py_cache(args):
    """只缓存单路径形式（py -3 这类留着每次现算）"""
    try:
        if len(args) != 1:
            return
        os.makedirs(os.path.dirname(PY_CACHE), exist_ok=True)
        with open(PY_CACHE, "w", encoding="utf-8") as f:
            f.write(args[0])
    except OSError:
        pass


def missing_modules(py_args):
    """返回缺失的核心模块列表（用于给出准确的安装指引）"""
    code = ("import importlib.util as u,sys;"
            "print(','.join(m for m in sys.argv[1:] if u.find_spec(m) is None))")
    try:
        r = subprocess.run(list(py_args) + ["-c", code] + list(CORE_MODULES),
                           capture_output=True, text=True, errors="ignore", timeout=30)
        return [m for m in (r.stdout or "").strip().split(",") if m]
    except Exception:
        return list(CORE_MODULES)


def deps_report(py_args):
    """返回 (缺失核心模块, 缺失可选项 dict)"""
    code = ("import importlib.util as u,sys,json;"
            "print(json.dumps([m for m in sys.argv[1:] if u.find_spec(m) is None]))")
    try:
        r = subprocess.run(list(py_args) + ["-c", code] + list(OPTIONAL_MODULES),
                           capture_output=True, text=True, errors="ignore", timeout=30)
        miss = json.loads((r.stdout or "[]").strip() or "[]")
    except Exception:
        miss = []
    return missing_modules(py_args), {m: OPTIONAL_MODULES[m] for m in miss}


# ---------------- 日志 ----------------
def rotate_log(max_bytes=5 * 1024 * 1024):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > max_bytes:
            old = LOG_FILE[:-4] + ".1.log"
            if os.path.exists(old):
                os.remove(old)
            os.replace(LOG_FILE, old)
    except Exception:
        pass


def tail(n=60):
    try:
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        info("  还没有日志文件：%s（服务启动后才会有）" % LOG_FILE)
        return
    for line in lines[-n:]:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")


# ---------------- 命令 ----------------
def cmd_status(quiet=False):
    pid = find_listener()
    if not pid:
        if not quiet:
            info("  状态：未运行")
            info("  启动：双击 start.bat，或 python serve.py start")
            py = _read_py_cache()
            if py:
                info("  上次解释器：%s" % py)
            info("  日志：%s" % LOG_FILE)
        return 1
    ours = is_ours()
    if not quiet:
        info("  状态：运行中  (PID %d)" % pid)
        info("  地址：%s" % BASE_URL)
        if not ours:
            warn("该端口上的程序不响应本项目接口，可能被别的程序占用")
        st = http_get("/api/status")
        if isinstance(st, dict):
            plats = st.get("platforms") or []
            info("  平台：%s" % (" + ".join(plats) if plats else "未启用"))
            if st.get("running"):
                info("  任务：正在获取")
        py = _read_py_cache()
        if py:
            info("  解释器：%s" % py)
        info("  日志：%s（python serve.py logs -f 持续查看）" % LOG_FILE)
    return 0


def cmd_start(args):
    pid = find_listener()
    if pid:
        if is_ours():
            info("  已经在运行了（PID %d）：%s" % (pid, BASE_URL))
            if not args.no_open:
                open_browser()
            return 0
        die("端口 %d 被别的程序占用（PID %d）。\n"
            "     先跑 `python serve.py stop` 或关掉它；也可以改 serve.py 里的 PORT。" % (PORT, pid))

    py = find_python(verbose=True)
    if not py:
        miss = None
        for cand in ([sys.executable], [os.path.join(ROOT, r".venv\Scripts\python.exe")]):
            if os.path.exists(cand[0]):
                miss = missing_modules(cand)
                break
        tip = ("\n     缺依赖：%s" % ", ".join(miss)) if miss else ""
        die("找不到能运行本项目的 Python（需要 3.10+ 且装齐依赖）。%s\n"
            "     在本目录执行：\n"
            "       python -m venv .venv\n"
            "       .venv\\Scripts\\python -m pip install -r requirements.txt\n"
            "     之后 start.bat 会自动优先用 .venv。" % tip)

    core_miss, opt_miss = deps_report(py)
    if core_miss:
        die("解释器 %s 缺少核心依赖：%s\n     执行：%s -m pip install -r requirements.txt"
            % (subprocess.list2cmdline(py), ", ".join(core_miss),
               subprocess.list2cmdline(py)))
    for mod, what in opt_miss.items():
        warn("缺少 %s（影响：%s），不影响启动" % (mod, what))

    os.makedirs(LOG_DIR, exist_ok=True)
    rotate_log()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"

    cmd = list(py) + ["-m", "uvicorn", "app:app", "--host", HOST, "--port", str(PORT)]

    if args.fg:
        info("  前台启动（Ctrl+C 停止）：%s" % BASE_URL)
        try:
            return subprocess.call(cmd, cwd=ROOT, env=env)
        except KeyboardInterrupt:
            return 0

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    def _spawn(extra):
        logf = open(LOG_FILE, "a", encoding="utf-8", errors="replace")
        try:
            logf.write("\n===== 启动 %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            logf.flush()
            return subprocess.Popen(cmd, cwd=ROOT, env=env,
                                    stdin=subprocess.DEVNULL, stdout=logf,
                                    stderr=subprocess.STDOUT, close_fds=True,
                                    creationflags=flags | extra)
        finally:
            logf.close()

    try:
        proc = _spawn(CREATE_BREAKAWAY_FROM_JOB)
    except OSError:
        # 有些 Job 不允许 breakaway，退回普通脱离
        proc = _spawn(0)

    # 探活：uvicorn 起得快，但首次要 init_db / 备份库，给 30 秒
    info("  正在启动…")
    deadline = time.time() + 30
    while time.time() < deadline:
        rc = proc.poll()
        # 只在「进程已退出 且 端口仍无人监听」时判失败：
        # launcher 可能先退出把活儿交给真解释器，只看 poll() 会误报。
        if rc is not None and not find_listener():
            info("")
            warn("启动失败，退出码 %s。最后 25 行日志：" % rc)
            tail(25)
            return 1
        if is_ours():
            # 以「真正在监听的 PID」为准，不要拿 Popen 返回的 pid 去比：
            # .venv / 托管环境里的 python.exe 可能是个 launcher，它会派生真正的
            # 解释器进程，Popen.pid 和监听者 pid 天生不是一个（实测差上千）。
            # 启动前已确认端口空闲，所以探到有人监听 = 就是我们起的。
            real = find_listener()
            if not real:
                time.sleep(0.3)
                continue
            write_pid(real)
            info("  ✓ 已启动（PID %d）" % real)
            info("    地址：%s" % BASE_URL)
            info("    日志：%s" % LOG_FILE)
            info("    停止：双击 stop.bat（关掉窗口不会停服务）")
            if not args.no_open:
                open_browser()
            return 0
        time.sleep(0.6)

    warn("30 秒内没等到接口就绪，进程可能在初始化（PID %d）" % proc.pid)
    info("    稍等再执行 python serve.py status，或 python serve.py logs")
    return 1


def cmd_stop(args):
    pid = find_listener()
    stopped = []
    if pid:
        if kill_pid(pid):
            stopped.append(pid)
        else:
            warn("结束进程 %d 失败（可能权限不足）" % pid)
    # 端口没监听但 PID 文件残留（启动失败的情况）也清掉
    stale = read_pid()
    if stale and stale != pid and pid_alive(stale):
        if kill_pid(stale):
            stopped.append(stale)
    clear_pid()
    if not stopped:
        info("  本来就没在运行")
        return 0
    for _ in range(20):
        if not find_listener():
            break
        time.sleep(0.5)
    if find_listener():
        warn("端口仍被占用，可能没杀干净。手动检查：netstat -ano | findstr :%d" % PORT)
        return 1
    info("  已停止（PID %s）" % ", ".join(str(p) for p in stopped))
    return 0


def cmd_restart(args):
    cmd_stop(argparse.Namespace())
    time.sleep(0.8)
    return cmd_start(args)


def cmd_logs(args):
    if args.follow:
        info("  跟随日志（Ctrl+C 退出）：%s" % LOG_FILE)
        try:
            pos = 0
            if os.path.exists(LOG_FILE):
                pos = os.path.getsize(LOG_FILE)
            tail(20)
            while True:
                time.sleep(0.7)
                if not os.path.exists(LOG_FILE):
                    continue
                size = os.path.getsize(LOG_FILE)
                if size < pos:      # 日志轮转过
                    pos = 0
                if size > pos:
                    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                        f.seek(pos)
                        chunk = f.read()
                        pos = f.tell()
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
        except KeyboardInterrupt:
            return 0
    tail(args.lines)
    return 0


def open_browser():
    url = BASE_URL + "/"
    try:
        if os.name == "nt":
            os.startfile(url)          # noqa: 只在 Windows 有
        else:
            import webbrowser
            webbrowser.open(url)
    except Exception:
        info("  打不开浏览器，手动访问：%s" % url)


def cmd_open(args):
    pid = find_listener()
    if not pid:
        warn("服务没在跑，先 python serve.py start")
        return 1
    open_browser()
    return 0


def main():
    _init_stdout()
    if os.name != "nt":
        info("  提示：本脚本的进程管理针对 Windows，其他系统请直接用 uvicorn 前台跑")

    ap = argparse.ArgumentParser(
        prog="serve.py", description="BOSS岗位追踪 启动管理",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python serve.py start | stop | restart | status | logs -f | open")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("start", help="后台启动并打开浏览器")
    p.add_argument("--fg", action="store_true", help="前台运行（调试用）")
    p.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    p = sub.add_parser("stop", help="停止服务")
    p = sub.add_parser("restart", help="重启")
    p.add_argument("--no-open", action="store_true")
    p = sub.add_parser("status", help="查看运行状态")
    p = sub.add_parser("logs", help="查看日志")
    p.add_argument("lines", nargs="?", type=int, default=60)
    p.add_argument("-f", "--follow", action="store_true")
    p = sub.add_parser("open", help="打开看板")

    args = ap.parse_args()
    cmds = {"start": cmd_start, "stop": cmd_stop, "restart": cmd_restart,
            "status": cmd_status, "logs": cmd_logs, "open": cmd_open}
    if not args.cmd:
        info("BOSS岗位追踪")
        cmd_status()
        info("")
        ap.print_help()
        return 0
    fn = cmds[args.cmd]
    if args.cmd == "status":
        return fn()
    if not hasattr(args, "no_open"):
        args.no_open = False
    if not hasattr(args, "fg"):
        args.fg = False
    return fn(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
