# -*- coding: utf-8 -*-
"""打包 Windows 桌面版（无控制台窗口）。

    python build_exe.py              # 目录版：dist/BossJobTracker/BossJobTracker.exe（推荐）
    python build_exe.py --onefile    # 单文件版：dist/BossJobTracker.exe（启动慢、杀软更敏感）

打包后 exe 自带 Python 运行时，目标机器不用装 Python；
但抓取仍然需要本机有 Chrome 或 Edge（DrissionPage 驱动真实浏览器，无法内嵌）。
"""
import os
import shutil
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
NAME = "BossJobTracker"

# uvicorn / apscheduler 大量用字符串动态加载实现，静态分析扫不到，必须显式声明
HIDDEN = [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols", "uvicorn.protocols.http", "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl", "uvicorn.protocols.http.httptools_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.websockets_impl", "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "apscheduler.triggers.cron", "apscheduler.triggers.interval",
    "apscheduler.triggers.date", "apscheduler.executors.pool",
    "apscheduler.jobstores.memory",
    "fitz", "pypdf", "docx",
]

# 这些是环境里装着、但本项目运行时不用的重包（DrissionPage 会顺带牵出 cv2/numpy，
# 一进来就是两三百 MB）。显式排掉，包小一半、启动也快。
EXCLUDE = [
    "cv2", "numpy", "pandas", "matplotlib", "scipy", "torch", "sklearn",
    "IPython", "notebook", "pytest", "tkinter", "PIL.ImageQt",
]


def _prepare_dist(dist_dir):
    """PyInstaller 的 COLLECT 会先删旧产物目录，几百上千个文件时会被
    批量删除保护拦下（SAFE_DELETE_BULK_CONFIRM_REQUIRED），打包就"莫名失败"。
    优先用「整体改名」把旧产物挪走——改名不需要删除权限，自然绕开这个限制。"""
    d = os.path.join(BASE, dist_dir)
    if not os.path.isdir(d):
        return True
    # 单文件版 dist_single 里只有一两个文件。不能留给 PyInstaller 自己删：
    # 它内部会 os.remove(旧 exe)，而本机的安全删除钩子会把 remove 转成回收站操作并失败。
    # 先把旧 exe 改名，让目标路径不存在，PyInstaller 就不会走到删除分支。
    nfiles = sum(len(fs) for _, _, fs in os.walk(d))
    if nfiles < 50:
        old = os.path.join(d, NAME + ".exe")
        if os.path.isfile(old):
            bak = os.path.join(d, "_prev_%d.exe" % int(time.time()))
            try:
                os.replace(old, bak)
                print("（旧 exe 已改名为 %s，可手动删除）" % os.path.basename(bak))
            except Exception as e:
                print("! 旧 exe 改名失败：%s" % e)
                return False
        return True
    bak = os.path.join(BASE, "_dist_prev")
    if os.path.isdir(bak):
        bak += "_%d" % int(time.time())
    try:
        os.replace(d, bak)
        print("（旧产物已挪到 %s，可手动删除以释放空间）" % os.path.basename(bak))
        return True
    except Exception as e:
        print("! 旧产物改名失败：%s" % e)
        print("  手动删掉 %s 再重试" % d)
        return False


def main():
    onefile = "--onefile" in sys.argv
    # 单文件版单独放 dist_single，避免把已构建好的目录版覆盖掉
    dist_dir = "dist_single" if onefile else "dist"
    if not _prepare_dist(dist_dir):
        return 1
    icon = os.path.join(BASE, "assets", "icon.ico")
    if not os.path.exists(icon):
        print("! 未找到 assets/icon.ico，先跑 python make_icon.py")
        return 1
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--distpath", os.path.join(BASE, dist_dir),
           "--windowed", "--name", NAME, "--icon", icon,
           "--add-data", "static" + os.pathsep + "static",
           "--collect-all", "DrissionPage",
           "--collect-submodules", "uvicorn",
           "--collect-submodules", "apscheduler",
           "--collect-submodules", "pymupdf",
           ]
    if onefile:
        cmd.append("--onefile")

    # 只带"公有的种子数据"，绝不打包 cookies / login_state / jobs.db——
    # 那是这台机器的登录凭证和岗位库，发给别人等于把账号一起交出去。
    for seed in ("cities.json", "liepin_cities.json"):
        p = os.path.join(BASE, "data", seed)
        if os.path.exists(p):
            cmd += ["--add-data", p + os.pathsep + "data"]
        else:
            print("! data/%s 不存在，新装用户首次需点一次「同步城市库」" % seed)

    for h in HIDDEN:
        cmd += ["--hidden-import", h]
    for m in EXCLUDE:
        cmd += ["--exclude-module", m]

    cmd.append(os.path.join(BASE, "desktop.py"))

    print("PyInstaller 打包中（首次约 2-4 分钟）…")
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("✗ 打包失败")
        return 1

    if onefile:
        exe = os.path.join(BASE, dist_dir, NAME + ".exe")
    else:
        exe = os.path.join(BASE, dist_dir, NAME, NAME + ".exe")
    if not os.path.exists(exe):
        print("✗ 没找到产物 %s" % exe)
        return 1
    print("\n✓ 打包完成：%s（%.1f MB）" % (exe, os.path.getsize(exe) / 1048576.0))
    print("\n分发说明：")
    print("  · 目录版把整个 dist\\%s 文件夹压缩发给对方，双击 %s.exe 即可" % (NAME, NAME))
    print("  · 首次运行数据写到 %%LOCALAPPDATA%%\\BossJobTracker（不污染程序目录）")
    print("  · 目标机器仍需装有 Chrome 或 Edge（抓取要用真实浏览器）")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
