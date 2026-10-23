# -*- coding: utf-8 -*-
"""一键起 Cloudflare 快速隧道（内网穿透），把公网地址打印出来并复制到剪贴板。

用法：双击 start_tunnel.bat，或  python tunnel.py
注意：快速隧道的地址每次重启都会变（xxx.trycloudflare.com 是随机子域名）；
     URL 是公网可访问的，但应用本身有访问口令门禁（data/access_key.txt），没口令打不开。
"""
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(BASE, "tools", "cloudflared.exe")
PORT = int(os.environ.get("PORT", "8765"))


def _key():
    try:
        with open(os.path.join(BASE, "data", "access_key.txt"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def main():
    if not os.path.exists(EXE):
        print("缺少 tools/cloudflared.exe。下载地址：")
        print("  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe")
        return 1
    print(f"正在建立隧道 -> http://127.0.0.1:{PORT}")
    p = subprocess.Popen(
        [EXE, "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="ignore", bufsize=1)
    url = None
    try:
        for line in p.stdout:
            line = line.rstrip()
            if not url:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line or "")
                if m:
                    url = m.group(0)
                    with open(os.path.join(BASE, "data", "tunnel_url.txt"), "w", encoding="utf-8") as f:
                        f.write(url)
                    try:
                        subprocess.run("clip", input=url, text=True, shell=True)
                    except Exception:
                        pass
                    print("\n" + "=" * 64)
                    print("公网地址（已复制到剪贴板）：", url)
                    k = _key()
                    print("访问口令：", k if k else "（未设置，任何人都能打开！建议创建 data/access_key.txt）")
                    print("手机打开该地址 → 输入口令 → 之后 30 天免输")
                    print("按 Ctrl+C 结束隧道")
                    print("=" * 64 + "\n")
                    continue
            # 只保留有意义的行，避免刷屏
            if any(t in line for t in ("ERR", "WRN", "Registered tunnel", "Connection", "Request")):
                print(line)
    except KeyboardInterrupt:
        print("\n正在关闭隧道…")
    finally:
        try:
            p.terminate()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
