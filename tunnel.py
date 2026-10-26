# -*- coding: utf-8 -*-
"""内网穿透：把本机看板暴露成一个公网 https 地址（手机在外面也能用）。

两种模式（自动判断）：
  A. 快速隧道（默认，零配置）：地址形如 https://xxx-yyy.trycloudflare.com
     —— 免费、不用账号，但**每次重启都会变**
  B. 命名隧道（固定域名）：把 Cloudflare 的 tunnel token 写进 data/cloudflared_token.txt
     —— 地址永不变，但需要 Cloudflare 账号 + 一个托管在 Cloudflare 的域名

为什么用 cloudflared：免费、出站 443 直连（公司网络也能穿）、自带 https、
本机服务仍只绑 127.0.0.1（不对外开端口）。

安全：公网地址谁拿到谁能开，所以必须配合访问口令（data/access_key.txt）。
app.py 已有闸门：本机直连免口令，经隧道来的必须输口令，cookie 有效期 30 天。

用法：
    python tunnel.py            # 起隧道，打印公网地址
    python tunnel.py --stop     # 停掉
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
TOKEN_FILE = os.path.join(DATA, "cloudflared_token.txt")   # 命名隧道 token（固定域名）
HOST_FILE = os.path.join(DATA, "tunnel_host.txt")          # 命名隧道用来显示的域名
DOWNLOAD = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
            "cloudflared-windows-amd64.exe")
LOCAL = "http://127.0.0.1:8765"


def ensure_binary():
    if os.path.exists(BIN) and os.path.getsize(BIN) > 10 * 1024 * 1024:
        return True
    os.makedirs(os.path.dirname(BIN), exist_ok=True)
    print("⬇️  首次运行，正在下载 cloudflared（约 53MB）…")
    try:
        # 用 curl 而不是 urllib：GitHub release 走 urllib 会拿到半截文件（实测 21MB/55MB）
        r = subprocess.run(["curl.exe", "-L", "--fail", "--retry", "2", "-o", BIN, DOWNLOAD],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print(f"❌ 下载失败：{r.stderr[-200:]}\n   可手动下载后放到：{BIN}")
            return False
    except Exception as e:
        print(f"❌ 下载失败：{e}\n   可手动下载后放到：{BIN}")
        return False
    return os.path.getsize(BIN) > 10 * 1024 * 1024


def ensure_key():
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


def read_token():
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def read_host():
    try:
        with open(HOST_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def local_alive():
    try:
        with urllib.request.urlopen(LOCAL + "/api/counts", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def notify_phone(url, key):
    """把新地址推到微信：出门在外时地址变了也能找到。
    用户没配 PushPlus token 就静默跳过，不影响主流程。"""
    try:
        sys.path.insert(0, BASE)
        import push
        if not push.has_token():
            return False
        ok, _ = push.send("🌐 看板公网地址已更新",
                          f"手机打开：{url}\n\n口令：`{key}`\n\n"
                          f"（本机自用免口令；手机首次输一次，之后 30 天免输）")
        return ok
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
    for f in (PID_FILE,):
        try:
            os.remove(f)
        except OSError:
            pass


def banner(url, key, named=False):
    print("\n" + "=" * 62)
    if named:
        print("✅ 命名隧道已启动（固定域名，重启也不会变）")
        print(f"\n   {url or '（域名见 Cloudflare 后台）'}\n")
    else:
        print("✅ 隧道已就绪，手机（4G/任意网络）打开这个地址：")
        print(f"\n   {url}\n")
        print("   ⚠️ 快速隧道的地址每次重启都会变。想固定不变：")
        print("      把 Cloudflare 的 tunnel token 写进 data/cloudflared_token.txt（见 README）")
    print(f"\n   口令：{key}")
    if url:
        print(f"   一次性直达（首次用，之后靠 cookie 免输）：\n   {url}/?k={key}")
        print("\n   手机建议：浏览器打开 → 添加到主屏幕，像 App 一样用")
    print("=" * 62 + "\n")


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
    token = read_token()
    named = bool(token)

    if named:
        cmd = [BIN, "tunnel", "--no-autoupdate", "run", "--token", token]
        print("🚇 正在用命名隧道连接（固定域名）…")
    else:
        cmd = [BIN, "tunnel", "--no-autoupdate", "--url", LOCAL]
        print("🚇 正在建立快速隧道…（首次连 Cloudflare 需要 5-15 秒）")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="ignore", bufsize=1)
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(proc.pid))

    url, shown = "", False
    pat = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
    try:
        for line in proc.stdout:
            if named and not shown:
                # 命名隧道不会打印地址，域名在后台配好了：读 tunnel_host.txt 显示
                url = read_host()
                banner(url, key, named=True)
                if url:
                    with open(URL_FILE, "w", encoding="utf-8") as f:
                        f.write(url)
                    print("📲 已推送到微信" if notify_phone(url, key) else "（未配 PushPlus，跳过微信推送）")
                shown = True
            m = pat.search(line or "")
            if m and not url:
                url = m.group(0)
                with open(URL_FILE, "w", encoding="utf-8") as f:
                    f.write(url)
                banner(url, key)
                print("📲 已推送到微信" if notify_phone(url, key) else "（未配 PushPlus，跳过微信推送）")
                shown = True
            if proc.poll() is not None:
                print("隧道进程已退出（可能网络不通/账号被限）")
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
