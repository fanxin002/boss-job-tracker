# -*- coding: utf-8 -*-
"""通过 GitHub Contents API 推送本次整合改动的 5 个文件（本机 git smart-http 不通时的备用通道）"""
import base64, json, os, sys, urllib.request

TOKEN = os.environ.get("GH_TOKEN", "")
OWNER, REPO, BRANCH = "fanxin002", "boss-job-tracker", "main"
API = f"https://api.github.com/repos/{OWNER}/{REPO}/contents"
ROOT = os.path.dirname(os.path.abspath(__file__))
FILES = ["README.md", "db.py", "e2e_test.py", "static/index.html"]
MSG = "fix: mark liepin masked (headhunter) companies and exclude them from same-job merging"


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
