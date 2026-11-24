"""Separate bundled read-only resources from persistent user data."""
import os
import sys

RESOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "BossJobTracker")
else:
    STATE_DIR = RESOURCE_DIR
DATA_DIR = os.environ.get("BOSS_TRACKER_DATA_DIR") or os.path.join(STATE_DIR, "data")
LOG_DIR = os.path.join(os.path.dirname(DATA_DIR), "logs")
BIN_DIR = os.path.join(os.path.dirname(DATA_DIR), "bin")


def resource_path(*parts):
    """Bundled read-only assets live next to the exe when frozen."""
    base = getattr(sys, "_MEIPASS", None) or RESOURCE_DIR
    return os.path.join(base, *parts)


# --windowed 打包后自身没有控制台，任何 console 子进程（tasklist/taskkill/powershell/netstat）
# 都会各自弹一个黑窗一闪而过。所有子进程一律带上这个标志。
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------- 静态页面
# 坑（2026-11-09 踩过）：单文件版把 static/ 放在 %TEMP%\_MEIxxxxx 里读。
# Windows 存储感知/磁盘清理会定期清 Temp，被进程锁住的 .pyd/.dll 删不掉，
# 但 static/、python 库这些没句柄的文件会被删干净。结果服务进程还活着、
# /api/status 正常，可前端页面全部 500：
#   FileNotFoundError: '...\_MEI0001b4482\static\index.html'
# 所以打包版启动时先把 static 复制到持久目录，之后一律读那份。
STATIC_DIR = os.path.join(STATE_DIR, "static")


def collect_static():
    """返回可长期使用的 static 目录（打包版固化到 STATE_DIR/static）。"""
    if not FROZEN:
        return os.path.join(RESOURCE_DIR, "static")

    src = resource_path("static")
    dst = STATIC_DIR
    if os.path.abspath(src) == os.path.abspath(dst):
        return src
    if not os.path.isdir(src):
        # 解包目录已被系统清掉——只要之前复制过，就直接用持久目录，
        # 绝不能退回这个已经不存在的路径（那正是页面 500 的原因）
        return dst if os.path.isdir(dst) else src

    import shutil
    try:
        # 版本戳：exe 换了就整体覆盖，避免升级后还拿旧页面
        stamp = "%s|%s" % (os.path.getmtime(sys.executable), os.path.getsize(sys.executable))
        marker = os.path.join(dst, ".version")
        old = ""
        if os.path.exists(marker):
            try:
                with open(marker, encoding="utf-8") as f:
                    old = f.read().strip()
            except OSError:
                old = ""

        os.makedirs(dst, exist_ok=True)
        fresh = old != stamp
        for root, _dirs, files in os.walk(src):
            rel = os.path.relpath(root, src)
            target = dst if rel == "." else os.path.join(dst, rel)
            os.makedirs(target, exist_ok=True)
            for name in files:
                s = os.path.join(root, name)
                d = os.path.join(target, name)
                if not fresh and os.path.exists(d) and os.path.getsize(d) == os.path.getsize(s):
                    continue
                shutil.copy2(s, d)

        if fresh:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(stamp)
        return dst
    except OSError:
        # 复制失败（比如目录只读）就退回原路径，至少本次能跑
        return src


def static_ok():
    """页面是否真的可读——用来判断「服务活着但已报废」（Temp 被清）。"""
    for name in ("index.html", "m.html"):
        if not os.path.exists(os.path.join(collect_static(), name)):
            return False
    return True


# 首次运行要从打包内补到 DATA_DIR 的种子文件（只补、不覆盖用户已有数据）。
# crawler / liepin 都直接读 DATA_DIR 下的 json，不落盘的话新装用户
# 打开就是空城市列表，得先联网「同步城市库」才能选城市。
SEED_FILES = ("cities.json", "liepin_cities.json")


def bootstrap_seed_files():
    import shutil
    os.makedirs(DATA_DIR, exist_ok=True)
    for name in SEED_FILES:
        dst = os.path.join(DATA_DIR, name)
        if os.path.exists(dst):
            continue
        src = resource_path("data", name)
        if os.path.exists(src) and os.path.abspath(src) != os.path.abspath(dst):
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
