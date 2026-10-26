# -*- coding: utf-8 -*-
"""内网穿透：把本机看板暴露成公网 https 地址（手机在外面也能用）。

三种通道，按「地址是否固定」和「要不要花钱」自己选：

| 通道 | 地址 | 花钱 | 需要什么 |
|---|---|---|---|
| ngrok（推荐） | **固定**（账号自带的 xxx.ngrok-free.dev） | 免费 | 注册 ngrok 账号，复制 authtoken |
| Cloudflare 命名隧道 | **固定**（自己的域名，如 jobs.abc.com） | 域名约 ¥10/年 | Cloudflare 账号 + 托管在 CF 的域名 |
| Cloudflare 快速隧道 | 每次重启都变（随机子域名） | 免费 | 什么都不用配 |

自动选择顺序：有 ngrok token → ngrok；有 CF token → CF 命名隧道；都没有 → CF 快速隧道。

用法：
    python tunnel.py                       # 自动选通道并启动
    python tunnel.py --provider ngrok      # 强制用 ngrok
    python tunnel.py --provider cf         # 强制用 Cloudflare（有 token 走命名，没有走快速）
    python tunnel.py --token xxxxx         # 写入当前通道的 token（ngrok authtoken / CF tunnel token）
    python tunnel.py --stop                # 停掉

安全：公网地址谁拿到谁能开 → 必须配访问口令（data/access_key.txt，脚本会自动生成）。
app.py 已有闸门：本机直连免口令，经隧道来的要输口令，cookie 有效 28 天。
"""
import json
import os
import random
import re
import string
import subprocess
import sys
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
BIN_DIR = os.path.join(BASE, "bin")
CF_BIN = os.path.join(BIN_DIR, "cloudflared.exe")
NG_BIN = os.path.join(BIN_DIR, "ngrok.exe")
DATA = os.path.join(BASE, "data")
URL_FILE = os.path.join(DATA, "tunnel_url.txt")
PID_FILE = os.path.join(DATA, "tunnel_pid.txt")
KEY_FILE = os.path.join(DATA, "access_key.txt")
CF_TOKEN_FILE = os.path.join(DATA, "cloudflared_token.txt")
CF_HOST_FILE = os.path.join(DATA, "tunnel_host.txt")
NG_TOKEN_FILE = os.path.join(DATA, "ngrok_token.txt")
NG_DOMAIN_FILE = os.path.join(DATA, "ngrok_domain.txt")
PROVIDER_FILE = os.path.join(DATA, "tunnel_provider.txt")
LOCAL = "http://127.0.0.1:8765"
CF_DOWNLOAD = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
               "cloudflared-windows-amd64.exe")
NG_DOWNLOAD = "https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-windows-amd64.zip"


# ---------------- 公共 ----------------
def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def ensure_key():
    k = _read(KEY_FILE)
    if k:
        return k
    k = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))
    _write(KEY_FILE, k)
    return k


def local_alive():
    try:
        with urllib.request.urlopen(LOCAL + "/api/counts", timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def curl_download(url, dst, timeout=600):
    """用系统 curl 下载：urllib 拿 GitHub/release 会得到半截文件（实测 21MB/55MB）"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    r = subprocess.run(["curl.exe", "-L", "--fail", "--retry", "2", "-o", dst, url],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0


def ensure_binary(kind):
    """返回可执行文件路径，缺了就下"""
    if kind == "cf":
        if os.path.exists(CF_BIN) and os.path.getsize(CF_BIN) > 10 * 1024 * 1024:
            return CF_BIN
        print("⬇️  下载 cloudflared（约 53MB）…")
        if not curl_download(CF_DOWNLOAD, CF_BIN) or os.path.getsize(CF_BIN) < 10 * 1024 * 1024:
            print(f"❌ cloudflared 下载失败，可手动放到 {CF_BIN}")
            return None
        return CF_BIN
    # ngrok
    if os.path.exists(NG_BIN) and os.path.getsize(NG_BIN) > 5 * 1024 * 1024:
        return NG_BIN
    print("⬇️  下载 ngrok（约 12MB）…")
    zip_path = os.path.join(BIN_DIR, "_ngrok.zip")
    if not curl_download(NG_DOWNLOAD, zip_path):
        print(f"❌ ngrok 下载失败，可手动放到 {NG_BIN}")
        return None
    try:
        with zipfile.ZipFile(zip_path) as z:
            for n in z.namelist():
                if n.lower().endswith("ngrok.exe"):
                    with z.open(n) as src, open(NG_BIN, "wb") as dst:
                        dst.write(src.read())
                    break
        os.remove(zip_path)
    except Exception as e:
        print(f"❌ ngrok 解压失败：{e}")
        return None
    return NG_BIN if os.path.exists(NG_BIN) else None


def notify_phone(url, key):
    """地址一变就推微信：出门在外也能拿到新链接（没配 PushPlus 静默跳过）"""
    try:
        sys.path.insert(0, BASE)
        import push
        if not push.has_token():
            return False
        ok, _ = push.send("🌐 看板公网地址已更新",
                          f"手机打开：{url}\n\n口令：`{key}`\n\n"
                          f"（本机自用免口令；手机首次输一次，之后 28 天免输）")
        return ok
    except Exception:
        return False


def banner(url, key, fixed, provider):
    print("\n" + "=" * 62)
    if fixed:
        print(f"✅ {provider} 已启动 —— 地址固定，重启也不变")
    else:
        print(f"✅ {provider} 已启动 —— ⚠️ 地址每次重启都会变")
    print(f"\n   {url}\n")
    print(f"   口令：{key}")
    print(f"   一次性直达（首次用，之后靠 cookie 免输）：\n   {url}/?k={key}")
    print("\n   手机建议：浏览器打开 → 添加到主屏幕，像 App 一样用")
    if not fixed:
        print("\n   想固定地址：python tunnel.py --token <ngrok authtoken>（见 README）")
    print("=" * 62 + "\n")


def stop_running():
    pid = _read(PID_FILE)
    if not pid.isdigit():
        print("没有记录的隧道进程")
        return
    subprocess.run(["taskkill", "/PID", pid.strip(), "/F"], capture_output=True, timeout=10)
    print(f"🛑 已停止隧道进程 {pid}")
    try:
        os.remove(PID_FILE)
    except OSError:
        pass


# ---------------- ngrok ----------------
def run_ngrok(key):
    token = _read(NG_TOKEN_FILE)
    if not token:
        print("⚠️  还没配 ngrok authtoken，地址就固定不了。三步搞定（免费）：")
        print("   1) 注册 https://dashboard.ngrok.com/signup （邮箱即可，不用绑卡）")
        print("   2) 控制台 Your Authtoken → 复制")
        print("   3) 运行：python tunnel.py --token 你的token")
        print("   之后每次 `python tunnel.py` 都是同一个固定地址。")
        print("\n   （不想注册也行：python tunnel.py --provider cf 用 Cloudflare 快速隧道，但地址会变）")
        return 1
    exe = ensure_binary("ng")
    if not exe:
        return 1
    subprocess.run([exe, "config", "add-authtoken", token], capture_output=True, timeout=60)

    domain = _read(NG_DOMAIN_FILE)
    cmd = [exe, "http", "8765", "--log", "stdout"]
    if domain:
        cmd += ["--domain", domain]
    print("🚇 正在连接 ngrok…（地址固定，首次约 5-10 秒）")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="ignore", bufsize=1)
    _write(PID_FILE, str(proc.pid))

    url, shown = "", False
    try:
        for line in proc.stdout:
            if not shown:
                # 优先问 ngrok 本地 API，比解析日志可靠
                url = _ngrok_public_url() or url
                if url:
                    _write(URL_FILE, url)
                    banner(url, key, True, "ngrok 隧道")
                    print("📲 已推送到微信" if notify_phone(url, key) else "（未配 PushPlus，跳过微信推送）")
                    shown = True
            if proc.poll() is not None:
                if not shown:
                    print("❌ ngrok 启动失败（token 无效/额度用尽/端口占用？）")
                break
    except KeyboardInterrupt:
        print("\n⏹ 正在关闭隧道…")
    finally:
        _shutdown(proc)
    return 0


def _ngrok_public_url():
    for _ in range(10):
        try:
            with urllib.request.urlopen("http://127.0.0.1:4040/api/tunnels", timeout=2) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            for t in data.get("tunnels", []):
                u = t.get("public_url") or ""
                if u.startswith("https://"):
                    return u
        except Exception:
            pass
        time.sleep(1)
    return ""


# ---------------- Cloudflare ----------------
def run_cloudflare(key):
    exe = ensure_binary("cf")
    if not exe:
        return 1
    token = _read(CF_TOKEN_FILE)
    named = bool(token)
    if named:
        cmd = [exe, "tunnel", "--no-autoupdate", "run", "--token", token]
        print("🚇 正在连接 Cloudflare 命名隧道（固定域名）…")
    else:
        cmd = [exe, "tunnel", "--no-autoupdate", "--url", LOCAL]
        print("🚇 正在建立 Cloudflare 快速隧道…（地址会变）")

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="ignore", bufsize=1)
    _write(PID_FILE, str(proc.pid))

    url, shown = "", False
    pat = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
    try:
        for line in proc.stdout:
            if named and not shown:
                url = _read(CF_HOST_FILE)
                if url and not url.startswith("http"):
                    url = "https://" + url
                if url:
                    _write(URL_FILE, url)
                banner(url or "（域名见 Cloudflare 后台）", key, True, "Cloudflare 命名隧道")
                if url:
                    print("📲 已推送到微信" if notify_phone(url, key) else "（未配 PushPlus，跳过微信推送）")
                shown = True
            m = pat.search(line or "")
            if m and not url:
                url = m.group(0)
                _write(URL_FILE, url)
                banner(url, key, False, "Cloudflare 快速隧道")
                print("📲 已推送到微信" if notify_phone(url, key) else "（未配 PushPlus，跳过微信推送）")
                shown = True
            if proc.poll() is not None:
                if not shown:
                    print("❌ Cloudflare 隧道启动失败（网络不通/token 无效？）")
                break
    except KeyboardInterrupt:
        print("\n⏹ 正在关闭隧道…")
    finally:
        _shutdown(proc)
    return 0


def _shutdown(proc):
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


# ---------------- 入口 ----------------
def pick_provider(argv):
    for i, a in enumerate(argv):
        if a == "--provider" and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if a.startswith("--provider="):
            return a.split("=", 1)[1].strip().lower()
    saved = _read(PROVIDER_FILE).lower()
    if saved:
        return saved
    if _read(NG_TOKEN_FILE):
        return "ngrok"
    if _read(CF_TOKEN_FILE):
        return "cf"
    return "cf"


def set_token(argv):
    """--token xxx [--provider ngrok|cf]：把 token 写进对应文件"""
    val = ""
    for i, a in enumerate(argv):
        if a == "--token" and i + 1 < len(argv):
            val = argv[i + 1].strip()
        elif a.startswith("--token="):
            val = a.split("=", 1)[1].strip()
    if not val:
        return None
    prov = pick_provider(argv)
    if prov == "ngrok":
        _write(NG_TOKEN_FILE, val)
        print(f"✅ ngrok authtoken 已保存到 {NG_TOKEN_FILE}")
        print("   固定域名：登录 https://dashboard.ngrok.com/domains 看你的 dev domain，")
        print("   想显式指定就写进 data/ngrok_domain.txt（一般不用，ngrok 会自动用账号那个）")
    else:
        _write(CF_TOKEN_FILE, val)
        print(f"✅ Cloudflare tunnel token 已保存到 {CF_TOKEN_FILE}")
        print("   还要在 Cloudflare 后台给这个隧道配 Public Hostname → http://127.0.0.1:8765")
        print("   并把域名写进 data/tunnel_host.txt（只用于打印显示）")
    _write(PROVIDER_FILE, prov)
    print("   现在运行：python tunnel.py")
    return 0


def main():
    argv = sys.argv[1:]
    if "--stop" in argv:
        stop_running()
        return 0
    if "--token" in " ".join(argv):
        r = set_token(argv)
        if r is not None:
            return r
    if not local_alive():
        print("⚠️  本机服务没起来（127.0.0.1:8765 无响应）")
        print("   先启动看板：双击 start.bat，或 python -m uvicorn app:app --host 127.0.0.1 --port 8765")
        return 1
    key = ensure_key()
    prov = pick_provider(argv)
    if prov in ("ngrok", "ng"):
        return run_ngrok(key)
    if prov in ("cf", "cloudflare", "cf-quick", "cf-named"):
        return run_cloudflare(key)
    print(f"未知通道：{prov}（可用：ngrok / cf）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
