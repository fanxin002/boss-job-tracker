# -*- coding: utf-8 -*-
"""通过 GitHub Contents API 推送本次整合改动的 5 个文件（本机 git smart-http 不通时的备用通道）"""
import base64, json, os, sys, urllib.request

TOKEN = os.environ.get("GH_TOKEN", "")
OWNER, REPO, BRANCH = "fanxin002", "boss-job-tracker", "main"
API = f"https://api.github.com/repos/{OWNER}/{REPO}/contents"
ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["crawler.py", "app.py", "static/index.html", "e2e_test.py", "README.md", "push_gh.py"]
MSG = ("feat(browser): support Edge alongside Chrome - auto-detect (Chrome first, fall back to Edge), "
       "per-browser user-data-dir isolation (*_edge), real-device UA cache (UA Reduction aware), "
       "top-bar browser switcher + /api/browser; drop plugins/hardwareConcurrency spoofing "
       "that never took effect and contradicted real-device values")

# 隐私闸：推送前扫一遍要上传的文件，命中就拒绝推送（仓库是 public）
# 2026-10-23 踩过的坑：e2e 的简历测试样本用了真实姓名+真实公司，从第一个 commit
# 起就在公开仓库里躺着，140 个 commit 全带。教训——推送前必须自动扫。
# 敏感词**不放代码里**（否则脚本自己就成了泄露源），读本地 data/private_terms.txt（已被 .gitignore 排除）
TERMS_FILE = os.path.join(ROOT, "data", "private_terms.txt")
GENERIC_PATTERNS = [
    ("手机号", r"(?<!\d)1[3-9]\d{9}(?!\d)"),
    ("邮箱", r"[\w.+-]+@(?:qq|163|126|gmail|outlook)\.com"),
    ("GitHub token", r"gh[pousr]_[A-Za-z0-9]{20,}"),
    # 只认「像凭据的字面量」：值必须是 12 位以上的 token 字符，且不含引号/换行/括号，
    # 否则容易把代码里的 token="): 这类表达式当凭据（2026-10-26 误报过一次）
    ("Cloud token", r"(?i)(?:token|secret|password|passwd)\s*=\s*[\"'][A-Za-z0-9_\-\.]{12,}[\"']"),
]


def scan_private():
    """返回 [(文件, 规则, 命中片段)]，空 list 表示干净"""
    import re
    rules = list(GENERIC_PATTERNS)
    try:
        with open(TERMS_FILE, encoding="utf-8") as f:
            for ln in f:
                t = ln.strip()
                if t and not t.startswith("#"):
                    rules.append((f"本地敏感词「{t[:2]}**」", re.escape(t)))
    except OSError:
        pass                      # 没配本地词表就只跑通用规则
    hits = []
    for path in FILES:
        full = os.path.join(ROOT, path.replace("/", os.sep))
        try:
            text = open(full, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for label, pat in rules:
            for m in re.finditer(pat, text):
                line = text[:m.start()].count("\n") + 1
                hits.append((path, label, f"L{line}: {m.group(0)[:40]}"))
    return hits



def req(url, data=None, method=None):
    r = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    r.add_header("Authorization", f"token {TOKEN}")
    r.add_header("Accept", "application/vnd.github+json")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTPError {e.code} {url}\nbody: {e.read().decode()[:500]}")
        raise


def main():
    if not TOKEN:
        print("NO_TOKEN"); sys.exit(2)
    hits = scan_private()
    if hits and os.environ.get("ALLOW_PRIVATE") != "1":
        print("⛔ 拒推：检测到疑似个人/凭据信息（如确认无误可 ALLOW_PRIVATE=1 强制推）")
        for path, label, frag in hits:
            print(f"   {path}  [{label}]  {frag}")
        sys.exit(3)
    print(f"PRIVACY_SCAN ok（{len(FILES)} 文件，0 命中）" if not hits else "PRIVACY_SCAN 已放行（强制）")
    ok = 0
    for path in FILES:
        full = os.path.join(ROOT, path.replace("/", os.sep))
        with open(full, "rb") as f:
            content = base64.b64encode(f.read()).decode()
        try:
            cur = req(f"{API}/{path}?ref={BRANCH}")
            sha = cur.get("sha")
        except Exception:
            sha = None
        body = {"message": MSG, "content": content, "branch": BRANCH}
        if sha:
            body["sha"] = sha
        res = req(f"{API}/{path}", json.dumps(body).encode(), method="PUT")
        cc = res["content"]
        match = cc["sha"]
        print(f"PUSHED {path} blob={match}")
        ok += 1
    tip = req(f"{API}/README.md?ref={BRANCH}")
    print(f"DONE {ok}/{len(FILES)} files, README sha now {tip['sha'][:8]}")


if __name__ == "__main__":
    main()
