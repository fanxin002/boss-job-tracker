# -*- coding: utf-8 -*-
"""内网穿透：用 cloudflared 快速隧道把本机看板暴露成一个公网 https 地址。

为什么用 cloudflared 快速隧道：
- 免费、不用注册账号、不用买服务器，出站 443 直连 Cloudflare（公司网络也能穿）
- 自动给 https 证书（手机浏览器不会拦）
- 本机服务仍然只绑 127.0.0.1，不对外开端口

安全：公网地址是「谁拿到谁能开」，所以必须配合访问口令（data/access_key.txt）。
app.py 里已有闸门：本机直连免口令，经隧道来的必须输口令，口令 cookie 有效期 30 天。

用法：
    python tunnel.py            # 起隧道，打印公网地址
    python tunnel.py --stop     # 停掉正在跑的隧道

注意：快速隧道的地址每次重启都会变（随机）。要固定地址得用 Cloudflare 账号的命名隧道。
"""
import os
import random
import re
import string
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(BASE, "bin", "cloudflared.exe")
DATA = os.path.join(BASE, "data")
URL_FILE = os.path.join(DATA, "tunnel_url.txt")
PID_FILE = os.path.join(DATA, "tunnel_pid.txt")
KEY_FILE = os.path.join(DATA, "access_key.txt")
DOWNLOAD = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
            "cloudflared-windows-amd64.exe")
LOCAL = "http://127.0.0.1:8765"


def ensure_binary():
    if os.path.exists(BIN) and os.path.getsize(BIN) > 10 * 1024 * 1024:
        return True
    os.makedirs(os.path.dirname(BIN), exist_ok=True)
    print(f"⬇️  首次运行，正在下载 cloudflared（约 53MB）…")
    try:
        urllib.request.urlretrieve(DOWNLOAD, BIN)
    except Exception as e:
        print(f"❌ 下载失败：{e}\n   可手动下载后放到：{BIN}")
        return False
    return os.path.getsize(BIN) > 10 * 1024 * 1024


def ensure_key():
    """没有口令就生成一个：公网地址不能裸奔"""
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            k = f.read().strip()
        if k:
            return k
    except OSError:
        pass
    os.makedirs(DATA, exist_ok=True)
    k = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    with open(KEY_FILE, "w", encoding="utf-8") as f:
        f.write(k)
    return k


def local_alive():
    try:
        with urllib.request.urlopen(LOCAL + "/api/counts", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def stop_running():
    try:
        with open(PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        print("没有记录的隧道进程")
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=10)
        print(f"🛑 已停止隧道进程 {pid}")
    except Exception as e:
        print(f"停止失败：{e}")
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


def main():
    if "--stop" in sys.argv:
        stop_running()
        return 0
    if not ensure_binary():
        return 1
    if not local_alive():
        print("⚠️  本机服务没起来（127.0.0.1:8765 无响应）")
        print("   先启动看板：双击 start.bat，或 python -m uvicorn app:app --host 127.0.0.1 --port 8765")
        return 1
    key = ensure_key()
    print("🚇 正在建立隧道…（首次连 Cloudflare 需要 5-15 秒）")

    proc = subprocess.Popen(
        [BIN, "tunnel", "--no-autoupdate", "--url", LOCAL],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="ignore", bufsize=1,
    )
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    url, deadline = "", time.time() + 45
    pat = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
    try:
        for line in proc.stdout:
            m = pat.search(line or "")
            if m and not url:
                url = m.group(0)
                with open(URL_FILE, "w", encoding="utf-8") as f:
                    f.write(url)
                print("\n" + "=" * 62)
                print("✅ 隧道已就绪，手机（4G/任意网络）打开这个地址：")
                print(f"\n   {url}\n")
                print(f"   口令：{key}")
                print(f"   一次性直达链接（首次用，之后靠 cookie 免输）：\n   {url}/?k={key}")
                print("\n   手机建议：Safari/浏览器打开 → 添加到主屏幕，像 App 一样用")
                print("   地址每次重启会变，重启后看这个文件：data\\tunnel_url.txt")
                print("=" * 62 + "\n")
            if time.time() > deadline and not url:
                print("⌛ 45 秒没拿到公网地址，检查网络是否能访问 cloudflare")
            if proc.poll() is not None:
                print("隧道进程已退出")
                break
    except KeyboardInterrupt:
        print("\n⏹ 收到中断，正在关闭隧道…")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            os.remove(PID_FILE)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
