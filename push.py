# -*- coding: utf-8 -*-
"""PushPlus 微信推送。token 只读本地文件 data/pushplus.txt（绝不在聊天/接口里传输）"""
import json
import os
import urllib.request

import db

TOKEN_FILE = os.path.join(db.DATA_DIR, "pushplus.txt")


def has_token():
    return os.path.exists(TOKEN_FILE) and bool(open(TOKEN_FILE, encoding="utf-8").read().strip())


def _token():
    return open(TOKEN_FILE, encoding="utf-8").read().strip()


def send(title, content, template="markdown"):
    """返回 (ok, msg)。失败不影响主流程"""
    if not has_token():
        return False, "未配置推送：请把 PushPlus token 写入 data/pushplus.txt"
    body = json.dumps({"token": _token(), "title": title,
                       "content": content, "template": template}).encode("utf-8")
    req = urllib.request.Request("http://www.pushplus.plus/send", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode("utf-8", "ignore"))
        ok = resp.get("code") == 200
        return ok, resp.get("msg", "")
    except Exception as e:
        return False, str(e)


def push_run_summary(run_id, error=None):
    """获取轮结束后推送：抓到几 → 新增几 → 下线几（含日期与配置口径）"""
    import db as _db
    runs = _db.get_runs(50)
    r = next((x for x in runs if x["id"] == run_id), None)
    if not r:
        return False, "no run"
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    if error:
        title = f"⚠️ BOSS岗位追踪 {date} 获取失败"
        content = (f"**获取异常**：{error}\n\n"
                   f"关键词：{r['keywords']}\n城市：{r['cities']}\n"
                   f"如登录态过期，请打开 http://127.0.0.1:8765 点「登录 BOSS」重新登录")
        return send(title, content)
    total, _rows = _db.query_jobs({})
    offline_total, _ = _db.query_jobs({"status": "offline"})
    ct = _db.counts()
    title = f"📊 BOSS岗位日报 {r['started_at'][:10]}"
    content = (
        f"- 口径：关键词「{r['keywords']}」 ｜ 城市：{r['cities']} ｜ 每城市 {r['pages']} 页\n"
        f"- 本轮抓到：**{r['fetched_count']}** 条\n"
        f"- 新增岗位：**{r['new_count']}** 条\n"
        f"- 标记下线：**{r['offline_count']}** 条\n"
        f"- 库内存量：共 {total} 条（其中已下线 {offline_total}）\n")
    # 投递漏斗提醒
    if ct.get("applied"):
        od = ct.get("overdue", 0)
        content += (f"- 累计投递：**{ct['applied']}** 条"
                    + (f" ｜ ⏰ 其中 **{od}** 条投满3天没人理\n" if od else " ｜ 暂无石沉大海\n"))
    content += "\n"
    fresh = _db.new_jobs_of_run(run_id, 5)
    if fresh:
        content += "**本轮新增（薪资优先）**\n"
        for j in fresh:
            sal = j.get("salary") or "薪资未知"
            content += (f"- [{j['job_name']}]({j['job_url']}) ｜ {j['company']} ｜ "
                        f"{sal} ｜ {j.get('city_name') or ''}\n")
        if (r["new_count"] or 0) > len(fresh):
            content += f"- 其余 {r['new_count'] - len(fresh)} 条见看板\n"
        content += "\n"
    content += "[打开看板](http://127.0.0.1:8765)"
    return send(title, content)
