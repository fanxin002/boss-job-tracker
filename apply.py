# -*- coding: utf-8 -*-
"""立即投递：复用登录态浏览器，逐条打开岗位详情页 → 点「立即沟通」发起打招呼
设计原则（防风控优先 + 结果可信）：
- 串行执行、每条随机间隔（可在页面配置，默认12~30秒）、单批次上限可配（默认20）
- 连续失败 3 次即停止整批（避免撞风控）
- 点击后轮询验证是否真的进入沟通（URL跳转chat/按钮变「已沟通」/简历弹窗点发送）
- 处理「选择简历」弹窗；已沟通过的岗位识别为跳过而非失败
- 检测到登录失效/安全验证/账号异常 → 立即停止并如实报告
- 全程无头静默，用完关浏览器"""
import random
import time

import crawler
import db


def _page_state(tab):
    """返回 None=正常 / 'blocked:原因' / 'offline' 岗位已下架

    登录失效的识别（实测 2026-10-23）：wt2 在 BOSS 服务端过期后，cookie 文件里的
    wt2 还在（文件不自己删），注入后打开详情页会跳转到
    /web/passport/zp/error.html?code=7，正文「当前登录状态已失效」，
    且页面没有「立即沟通」按钮——不识别就会误报成「岗位已下架」。
    """
    try:
        url = (tab.url or "")
        if "login" in url or "verify" in url or "security-check" in url:
            return "blocked:登录失效或需要安全验证"
        # 登录态过期：BOSS 跳 passport 错误页（code=7 就是登录失效）
        if "passport/zp/error" in url or "error.html" in url:
            return "blocked:BOSS 登录态已失效（页面跳转错误页 code=7，请重新登录）"
        body = tab.ele("tag:body", timeout=2)
        text = (body.text or "")[:1000] if body else ""
        if "当前登录状态已失效" in text or "登录状态已失效" in text:
            return "blocked:BOSS 登录态已失效（页面提示登录状态失效，请重新登录）"
        if any(k in text for k in ("安全验证", "滑块", "请完成验证", "访问异常")):
            return "blocked:触发安全验证"
        if "账户存在异常行为" in text or "环境存在异常" in text:
            return "blocked:账号风控"
        if any(k in text for k in ("该职位已失去收录", "职位已下线", "该职位已下线",
                                    "已关闭招聘", "该职位暂不可聊")):
            return "offline"
        return None
    except Exception as e:
        return f"blocked:页面检测异常 {e}"


def _find_btn(tab, *locators):
    for loc in locators:
        try:
            ele = tab.ele(loc, timeout=2.5)
            if ele:
                return ele
        except Exception:
            continue
    return None


def _already_chatted(tab):
    """已沟通过的岗位按钮会变成「已沟通/继续沟通」"""
    for txt in ("已沟通", "继续沟通"):
        try:
            if tab.ele(f"text():{txt}", timeout=0.8):
                return True
        except Exception:
            continue
    try:
        html = tab.html or ""
        return ("btn-chat" in html and "已沟通" in html)
    except Exception:
        return False


def _click_and_confirm(tab, greeting, logf):
    """点「立即沟通」→ 处理简历弹窗 → 轮询确认发起成功。返回 (ok, msg)"""
    btn = _find_btn(tab, "x://a[contains(@class,'btn-start-talk')]",
                    "x://button[contains(@class,'btn-start-talk')]",
                    "text():立即沟通", "text():沟通")
    if not btn:
        if _already_chatted(tab):
            return True, "已沟通过（跳过，不重复打招呼）"
        time.sleep(1.5)  # 慢加载重试一次
        btn = _find_btn(tab, "x://a[contains(@class,'btn-start-talk')]", "text():立即沟通")
        if not btn:
            # 兜底二次确认：先分清是「未登录/风控」还是「真下架」，别把账号问题
            # 误报成岗位问题（用户 2026-10-23 实际踩过：wt2 过期被报成岗位已下架）
            st = _page_state(tab)
            if st and str(st).startswith("blocked"):
                return False, st
            return False, "未找到「立即沟通」按钮（岗位可能不可聊/已下架）"
    try:
        btn.click(by_js=True)
    except Exception:
        try:
            btn.click()
        except Exception as e:
            return False, f"按钮点击失败：{e}"
    time.sleep(random.uniform(1.5, 3))

    # 「选择简历」弹窗：确认默认简历并发送
    for txt in ("发送", "立即发送", "确定"):
        dlg_btn = _find_btn(tab, f"text():{txt}")
        if dlg_btn:
            try:
                dlg_btn.click(by_js=True)
                logf(f"      已确认简历弹窗（{txt}）")
                time.sleep(1.5)
                break
            except Exception:
                pass

    # 自定义招呼语：若聊天输入框出现则替换/填入
    if greeting:
        for loc in ("x://div[@contenteditable='true']", "tag:textarea@"):
            try:
                inp = tab.ele(loc, timeout=1.5)
                if inp:
                    inp.clear().input(greeting)
                    time.sleep(0.4)
                    send = _find_btn(tab, "text():发送",
                                     "x://button[contains(@class,'btn-send-message')]")
                    if send:
                        send.click()
                        time.sleep(1)
                        return True, "已发送自定义招呼语"
                break
            except Exception:
                continue

    # 轮询确认：URL进入chat页 / 按钮变「已沟通」/ 出现会话列表，任一即成功
    for _ in range(10):
        try:
            if "chat" in (tab.url or ""):
                return True, "已发起沟通（已进入聊天页）"
        except Exception:
            pass
        if _already_chatted(tab):
            return True, "已发起沟通（按钮已变「已沟通」）"
        time.sleep(0.6)
    return True, "已点击沟通（未能二次确认状态，请到BOSS消息列表核实）"


def apply_one(page, job, greeting, logf):
    """对单条岗位发起沟通。返回 (ok, msg)"""
    url = job.get("job_url") or ""
    if not url:
        return False, "无链接"
    tab = None
    try:
        tab = page.new_tab(url)
        time.sleep(random.uniform(2.5, 4.5))
        state = _page_state(tab)
        if state == "offline":
            return False, "岗位已下架（已跳过，不影响其余）"
        if state:
            return False, state
        return _click_and_confirm(tab, greeting, logf)
    except Exception as e:
        return False, f"异常：{e}"
    finally:
        try:
            if tab is not None and tab is not page:
                tab.close()
        except Exception:
            pass


def run_apply(job_ids, greeting, logf, batch_id, max_batch=20, interval=(12, 30), should_cancel=None):
    """批量投递入口（后台线程调用）。返回 (ok_cnt, fail_cnt)"""
    jobs = []
    skipped = 0
    for jid in job_ids:
        j = db.get_job(jid)
        if not j:
            continue
        if j.get("applied") == 1:
            skipped += 1
            continue
        jobs.append(j)
    if skipped:
        logf(f"⏭ 跳过 {skipped} 条已投递成功的岗位")
    if len(jobs) > max_batch:
        logf(f"⚠️ 本次上限 {max_batch} 条，已截取（实际提交 {max_batch}/{len(jobs)}）")
        jobs = jobs[:max_batch]
    if not jobs:
        return 0, 0

    page = crawler.ensure_browser(offscreen=True)  # 离屏有头：真实浏览器指纹，更不容易被判风控
    crawler.inject_cookies(page)
    # 登录态活性前置探测（关键修复，2026-10-23）：
    # 「cookie 文件里有 wt2」≠ BOSS 服务端认——会话过期时文件里的 wt2 还在，
    # 注入后打开详情页会跳 error.html?code=7，曾被误报成「岗位已下架」。
    # 这里用一次轻量接口探测真实登录态，失效/风控则整批不开始，不留脏失败记录。
    code, pmsg = crawler.probe_api(page, "数据分析", "101230200")
    if code in (401, 403, 36, 37, 38):
        crawler._close_browser()
        _why = {401: "登录态已失效", 403: "登录态已失效", 36: "触发风控",
                37: "触发风控", 38: "触发风控"}.get(code, "异常")
        logf(f"⛔ BOSS {_why}（接口 code={code} {pmsg or ''}），投递未开始。"
             f"请点顶部「BOSS」胶囊重新扫码登录后再投递")
        return 0, len(jobs)
    ok_cnt = fail_cnt = 0
    consecutive_fail = 0
    try:
        total = len(jobs)
        for i, j in enumerate(jobs, 1):
            if should_cancel and should_cancel():
                logf("⏹ 已取消投递，剩余岗位未提交")
                break
            logf(f"  [{i}/{total}] 投递：{j['job_name']} @ {j['company']}")
            ok, msg = apply_one(page, j, greeting, logf)
            db.add_application(j, "success" if ok else "failed", msg, batch_id)
            db.set_applied([j["job_id"]], 1 if ok else 2)
            if ok:
                ok_cnt += 1
                consecutive_fail = 0
                logf(f"      ✅ {msg}")
            else:
                fail_cnt += 1
                if "已下架" not in msg:  # 下架属正常业务情况，不计入风控熔断
                    consecutive_fail += 1
                else:
                    db.mark_job_offline(j["job_id"])
                logf(f"      ❌ {msg}")
                if consecutive_fail >= 3:
                    logf("⛔ 连续3条失败，停止本批（疑似风控/登录失效），未投递的岗位不受影响")
                    break
                if msg.startswith("blocked"):
                    logf("⛔ 账号风控/登录失效，停止本批")
                    break
            if i < total and consecutive_fail < 3:
                wait = random.uniform(*interval)
                logf(f"      ⏳ 节流等待 {wait:.0f}s")
                time.sleep(wait)
    finally:
        crawler._close_browser()
        logf("投递浏览器已静默关闭")
    return ok_cnt, fail_cnt
