# -*- coding: utf-8 -*-
"""BOSS岗位追踪 - 本地网站 + 每日增量获取调度 + 快速投递"""
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import parse_qs, urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import apply
import crawler
import db
import liepin
import push
import resume as resume_mod

app = FastAPI(title="BOSS岗位追踪")
db.init_db()
# 启动即备份一次（保留最近 7 份），避免误操作/写坏后无库可回
try:
    _BOOT_BACKUP = db.backup()
except Exception:
    _BOOT_BACKUP = ""

# 本地静态资源（Leaflet 地图库内置，不依赖外网 CDN 的 js/css）
import os as _os
_static_dir = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "static")
if _os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")

_state = {"running": False, "run_id": None}
_state_lock = threading.Lock()
_apply_state = {"running": False, "batch_id": None, "log": [], "done": 0, "total": 0}
_apply_lock = threading.Lock()
_apply_cancel = {"flag": False}
_detail_state = {"running": False, "log": [], "done": 0, "total": 0}
_detail_cancel = {"flag": False}
scheduler = BackgroundScheduler(timezone="Asia/Shanghai")


# ---------- 外网访问口令（内网穿透时启用；本机使用零影响） ----------
# 规则：
#   1) 没配 data/access_key.txt → 完全不启用，行为和以前一样
#   2) 本机直连（127.0.0.1 且没有 Cloudflare 转发头）→ 免口令，自己电脑上不用输
#   3) 其余（经隧道/局域网来的）→ 必须带 cookie 或 ?k=口令，否则只给登录页
# 为什么不能用「来源是 127.0.0.1 就放行」：cloudflared 是在本机把请求转发进来的，
# 隧道来的请求来源同样是 127.0.0.1，只能靠 CF-Connecting-IP / CF-Ray 区分。
_ACCESS_FILE = _os.path.join(db.DATA_DIR, "access_key.txt")


def _access_key():
    try:
        with open(_ACCESS_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _login_html(msg=""):
    tip = f'<p class="err">{msg}</p>' if msg else ""
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>访问口令 · 岗位追踪</title>
<style>
  *{{box-sizing:border-box}}
  body{{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
    background:#f7f7f8;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;color:#262626}}
  form{{background:#fff;border:1px solid #e6e6e6;border-radius:12px;padding:28px 26px;width:min(92vw,340px);
    box-shadow:0 8px 30px rgba(0,0,0,.06)}}
  h1{{margin:0 0 4px;font-size:18px}}
  p{{margin:0 0 16px;font-size:13px;color:#6b6b6b}}
  .err{{color:#c92a2a}}
  input{{width:100%;height:42px;padding:0 12px;border:1px solid #ccc;border-radius:8px;font-size:16px;margin-bottom:12px}}
  input:focus{{outline:none;border-color:#1c69d4;box-shadow:0 0 0 3px rgba(28,105,212,.15)}}
  button{{width:100%;height:42px;border:0;border-radius:8px;background:#1c69d4;color:#fff;font-size:15px;font-weight:600}}
  button:active{{background:#0653b6}}
</style></head><body>
<form method="post" action="/__login">
  <h1>岗位追踪</h1>
  <p>这是私人看板，请输入访问口令</p>
  {tip}
  <input name="k" type="password" inputmode="text" autocomplete="current-password" autofocus placeholder="访问口令">
  <button type="submit">进入</button>
</form></body></html>"""


@app.middleware("http")
async def _access_gate(request: Request, call_next):
    key = _access_key()
    if not key:
        return await call_next(request)
    host = (request.client.host if request.client else "") or ""
    via_proxy = bool(request.headers.get("cf-connecting-ip") or request.headers.get("cf-ray")
                     or request.headers.get("x-forwarded-for") or request.headers.get("ngrok-trace-id"))
    # 判「本机直连」不能只看来源 IP：ngrok / cloudflared 都是在本机把请求转进来的，
    # 来源同样是 127.0.0.1。**必须再看 Host 头**——
    # 2026-10-26 实测：ngrok 转发不带 CF-Connecting-IP（也不带 X-Forwarded-For），
    # 只靠来源 IP + 代理头判断，隧道流量会被当成「自己电脑」直接放行，口令闸形同虚设。
    # 本机访问时 Host 一定是 127.0.0.1 / localhost；走隧道时 Host 是 xxx.ngrok-free.dev 等。
    host_hdr = (request.headers.get("host") or "").split(":")[0].strip().lower().strip("[]")
    loopback_host = host_hdr in ("127.0.0.1", "localhost", "::1")
    if host == "testclient" and host_hdr in ("testserver", "") and not via_proxy:
        # Starlette TestClient：虚拟来源，永远走不到网络上（Host 默认 testserver），
        # 放行让 e2e 能测真实逻辑。但它一旦带了 CF/ngrok 转发头、或把 Host 改成隧道域名，
        # 就必须按隧道处理 —— 否则门禁根本测不出来（2026-10-26 的 ngrok 放行漏洞就是这么漏的）
        return await call_next(request)
    if host in ("127.0.0.1", "::1", "localhost") and not via_proxy and loopback_host:
        return await call_next(request)          # 本机直连：免口令
    path = request.url.path
    if path == "/__login":
        if request.method == "POST":
            # 手动解析 x-www-form-urlencoded：用 request.form() 会强依赖 python-multipart，
            # 为了一个登录框多装一个包不值得
            raw = (await request.body()).decode("utf-8", "ignore")
            given = (parse_qs(raw).get("k") or [""])[0].strip()
            if given and secrets.compare_digest(given, key):
                resp = RedirectResponse("/", status_code=303)
                resp.set_cookie("jbt", key, max_age=30 * 24 * 3600,
                                httponly=True, samesite="lax")
                return resp
            return HTMLResponse(_login_html("口令不对，再试一次"), status_code=401)
        return HTMLResponse(_login_html())
    cookie_ok = secrets.compare_digest(str(request.cookies.get("jbt") or ""), key)
    if cookie_ok:
        return await call_next(request)
    q = str(request.query_params.get("k") or "").strip()
    if q and secrets.compare_digest(q, key):
        # 用 ?k= 首次进来：种 cookie 后跳到去掉口令的干净地址（避免口令留在浏览器历史里）
        rest = [(k2, v) for k2, v in request.query_params.multi_items() if k2 != "k"]
        target = path + ("?" + urlencode(rest) if rest else "")
        resp = RedirectResponse(target, status_code=303)
        resp.set_cookie("jbt", key, max_age=30 * 24 * 3600, httponly=True, samesite="lax")
        return resp
    return HTMLResponse(_login_html(), status_code=401)



# ---------- 获取执行 ----------
def _parse_cfg(cfg):
    keywords = [k.strip() for k in (cfg.get("keywords") or "").replace("，", ",").split(",") if k.strip()]
    try:
        cities = json.loads(cfg.get("cities") or "[]")
    except Exception:
        cities = []
    pages = int(cfg.get("pages") or 10)
    detail = False  # 描述统一走「补抓描述」：获取轮不再插详情页请求（最高风控点）
    mode = str(cfg.get("headless", "2"))
    headless = mode == "1"
    offscreen = mode == "2"
    degree = ",".join(x.strip() for x in (cfg.get("crawl_degrees") or "").split(",") if x.strip())
    return keywords, cities, pages, detail, headless, offscreen, degree


def _risk_block_msg(risk):
    return (f"风控冷却中：code={risk['code']} {risk['msg']}｜"
            f"建议 {risk['until'][11:16]} 后再试（剩余约 {risk['remaining_min']} 分钟）。"
            f"连续重试会延长封控，请先点「🔧 修复登录态」或等待自动解除")


def do_crawl(trigger="manual"):
    with _state_lock:
        if _state["running"]:
            return None
        cfg0 = db.get_config()
        if str(cfg0.get("boss_enabled", "1")) != "1":
            return None  # BOSS 平台开关已关闭
        if crawler.get_login_state().get("login_open"):
            return None  # 登录窗口进行中，避免抢占浏览器
        if crawler.repair_state().get("running"):
            return None  # 修复窗口进行中
        risk = db.get_risk()
        if risk["active"]:
            # 定时轮被冷却锁挡住时明确告知，避免"以为跑了其实没跑"
            if trigger == "daily":
                try:
                    push.send(
                        f"⏸ BOSS岗位追踪 {datetime.now():%Y-%m-%d} 定时获取已跳过",
                        f"- 原因：风控冷却中（code={risk['code']}）\n"
                        f"- 解除时间：{risk['until']}\n"
                        f"- 本轮未发出任何请求，避免加重封控\n\n"
                        f"[打开看板](http://127.0.0.1:8765)")
                except Exception:
                    pass
            return None
        with _apply_lock:
            if _apply_state["running"] or _detail_state["running"]:
                _state["running"] = False
                return None  # 投递/补抓进行中，不抢占浏览器
        _state["running"] = True
    cfg = db.get_config()
    keywords, cities, pages, detail, headless, offscreen, degree = _parse_cfg(cfg)
    if not keywords or not cities:
        with _state_lock:
            _state["running"] = False
        rid = db.create_run(",".join(keywords), json.dumps(cities, ensure_ascii=False), pages, trigger)
        db.append_run_log(rid, "配置为空：请填写关键词和城市")
        db.finish_run(rid, "config_error", 0, 0, 0)
        return rid
    rid = db.create_run(",".join(keywords), ",".join(c["name"] for c in cities), pages, trigger)
    _state["run_id"] = rid
    try:
        db.backup()          # 每轮获取前留一份快照（保留 7 份）
        crawler.clear_last_risk()
        _progress_start("boss", "准备浏览器")
        fetched, new_cnt, off_cnt = crawler.run_crawl(rid, keywords, cities, pages, detail,
                                                      headless, offscreen, degree,
                                                      progress=_progress_cb("boss"))
        _rescore(rid)   # 新岗位入库后立即按画像打分，避免「匹配」列全是 0
        if crawler.LAST_RISK.get("code") is not None:  # 本轮撞风控 → 上冷却锁
            risk = db.set_risk(crawler.LAST_RISK["code"], crawler.LAST_RISK["msg"])
            db.append_run_log(rid, f"🔒 已上风控冷却锁 {risk['remaining_min']} 分钟（{risk['until'][11:16]} 解除），期间不再自动重跑")
        status = "success" if fetched else ("risked" if crawler.LAST_RISK.get("code") is not None else "empty")
        db.finish_run(rid, status, fetched, new_cnt, off_cnt)
        _push_after(rid)
    except Exception as e:
        db.append_run_log(rid, f"❌ 获取异常：{e}")
        db.finish_run(rid, "error", 0, 0, 0)
        _push_after(rid, error=str(e))
    finally:
        with _state_lock:
            _state["running"] = False
        _progress_end()
    return rid


def _push_after(rid, error=None):
    """每日定时轮结束后推送汇总；手动轮默认也推（可在配置关闭）"""
    cfg = db.get_config()
    if str(cfg.get("push_enabled", "1")) != "1":
        return
    ok, msg = push.push_run_summary(rid, error=error)
    db.append_run_log(rid, ("📲 已推送到微信" if ok else f"推送失败：{msg}"))


# ---------- 调度 ----------
_liepin_state = {"running": False, "run_id": None}
_unified_state = {"running": False, "lock": threading.Lock()}  # 统一获取线程防重入

# 获取进度（前端进度条）：Boss/Liepin 各自回调写入，/api/status 读取
_PROGRESS = {"platform": "", "stage": "", "done": 0, "total": 0, "at": 0.0}


def _progress_cb(platform):
    """给 crawler/liepin 的进度回调：done/total 是「页」粒度"""
    def cb(done, total, note=""):
        _PROGRESS.update(platform=platform, done=int(done), total=int(total),
                         stage=(note or _PROGRESS.get("stage", "")), at=time.time())
    return cb


def _progress_start(platform, stage):
    _PROGRESS.update(platform=platform, stage=stage, done=0, total=0, at=time.time())


def _progress_end():
    _PROGRESS.update(platform="", stage="", done=0, total=0, at=0.0)


def do_crawl_liepin(trigger="manual"):
    """猎聘获取轮：独立浏览器 slot/profile，与 BOSS 轮并行安全（但调度错峰）"""
    with _state_lock:
        if _liepin_state["running"]:
            return None
        st = liepin.get_login_state()
        if st.get("login_open"):
            return None  # 猎聘登录窗口进行中
        risk = db.get_risk(source="liepin")
        if risk["active"]:
            if trigger == "daily":
                try:
                    push.send(
                        f"⏸ 猎聘获取 {datetime.now():%Y-%m-%d} 已跳过",
                        f"- 原因：风控/登录态异常（{risk['msg'] or risk['code']}）\n"
                        f"- 解除时间：{risk['until']}\n"
                        f"- 如登录失效请打开 http://127.0.0.1:8765 点「登录猎聘」")
                except Exception:
                    pass
            return None
        cfg = db.get_config()
        if str(cfg.get("liepin_enabled", "0")) != "1":
            return None
        keywords, cities, _p, _d, headless, offscreen, _deg = _parse_cfg(cfg)
        lp_pages = int(cfg.get("liepin_pages") or 2)
        if not keywords or not cities:
            return None
        rid = db.create_run(",".join(keywords), ",".join(c["name"] for c in cities),
                            lp_pages, trigger, source="liepin")
        _liepin_state["running"] = True
        _liepin_state["run_id"] = rid
    try:
        _progress_start("liepin", "准备浏览器")
        fetched, new_cnt, off_cnt = liepin.run_crawl(rid, keywords, cities, lp_pages,
                                                     headless=headless, offscreen=offscreen,
                                                     progress=_progress_cb("liepin"))
        # 猎聘卡片没有经纬度：用同城同区的 BOSS 坐标做区级近似点位，让地图也能看猎聘
        try:
            n_coord = db.fill_approx_coords("liepin")
            if n_coord["district"] or n_coord["city"]:
                db.append_run_log(rid, f"🗺 已补区级近似坐标 {n_coord['district'] + n_coord['city']} 条"
                                       f"（同区 {n_coord['district']} / 同城兜底 {n_coord['city']}）")
        except Exception as e:
            db.append_run_log(rid, f"⚠️ 近似坐标补全失败：{e}")
        _rescore(rid)   # 猎聘卡片没有技能标签，更依赖职位名/行业命中，同样要入库后立刻打分
        db.finish_run(rid, "success" if fetched else "empty", fetched, new_cnt, off_cnt)
        if push.has_token() and str(cfg.get("push_enabled", "1")) == "1":
            ok, msg = push.push_run_summary(rid)
            db.append_run_log(rid, ("📲 已推送到微信" if ok else f"推送失败：{msg}"))
    except Exception as e:
        db.append_run_log(rid, f"❌ 猎聘获取异常：{e}")
        db.finish_run(rid, "error", 0, 0, 0)
    finally:
        with _state_lock:
            _liepin_state["running"] = False
        _progress_end()
    return rid


def _platform_enabled(cfg, name):
    if name == "boss":
        return str(cfg.get("boss_enabled", "1")) == "1"
    return str(cfg.get(f"{name}_enabled", "0")) == "1"


def enabled_platforms(cfg=None):
    cfg = cfg or db.get_config()
    return [p for p in ("boss", "liepin") if _platform_enabled(cfg, p)]


def do_crawl_all(trigger="manual", gap_sec=60):
    """统一获取入口：按配置的平台开关，顺序跑 BOSS → 猎聘（错峰，避免两个离屏浏览器同时抢资源）。
    返回 {ran: [...], skipped: [...], results: {platform: run_id}}"""
    cfg = db.get_config()
    plats = enabled_platforms(cfg)
    ran, skipped, results = [], [], {}
    for i, p in enumerate(plats):
        if i > 0 and results.get(plats[i - 1]):
            # 上一平台确实跑了 → 错峰间隔，避免两个浏览器轮无缝衔接
            time.sleep(max(0, gap_sec))
        if p == "boss":
            rid = do_crawl(trigger)
        else:
            rid = do_crawl_liepin(trigger)
        if rid:
            ran.append(p)
            results[p] = rid
        else:
            skipped.append(p)
    return {"ran": ran, "skipped": skipped, "results": results}


def _apply_schedule():
    cfg = db.get_config()
    for j in scheduler.get_jobs():
        scheduler.remove_job(j.id)
    if str(cfg.get("daily_enabled", "1")) == "1":
        hh, mm = (cfg.get("daily_time", "09:30").split(":") + ["0"])[:2]
        # 统一定时任务：一个 job 内部按平台开关顺序跑（BOSS → 猎聘自动错峰）
        scheduler.add_job(do_crawl_all, "cron", hour=int(hh), minute=int(mm),
                          args=["daily"], id="daily_crawl", misfire_grace_time=3600)


@app.on_event("startup")
def _startup():
    _apply_schedule()
    if not scheduler.running:
        scheduler.start()


# ---------- 数据模型 ----------
class ConfigIn(BaseModel):
    keywords: str = ""
    cities: list = []
    pages: int = 10
    daily_enabled: str = "1"
    daily_time: str = "09:30"
    headless: str = "1"
    push_enabled: str = "1"
    greeting: str = ""
    crawl_degrees: str = ""
    apply_max_batch: int = 20
    apply_interval_min: int = 12
    apply_interval_max: int = 30
    boss_enabled: str = "1"
    liepin_enabled: str = "0"
    liepin_pages: int = 2


class ApplyIn(BaseModel):
    job_ids: list = []


class DetailsIn(BaseModel):
    limit: int = 20
    job_ids: list = []
    filters: dict = {}


class StarIn(BaseModel):
    job_id: str = ""
    star: int = 1


class NoteIn(BaseModel):
    job_id: str = ""
    note: str = ""


class StageIn(BaseModel):
    job_id: str = ""
    stage: str = ""         # interview / offer / rejected，空=清除
    note: str = ""


class ProfileIn(BaseModel):
    skills: str = ""          # 逗号分隔
    salary: int = 0
    degree: str = ""


# ---------- API ----------
@app.get("/api/config")
def api_get_config():
    cfg = db.get_config()
    try:
        cfg["cities"] = json.loads(cfg.get("cities") or "[]")
    except Exception:
        cfg["cities"] = []
    cfg["pages"] = int(cfg.get("pages") or 10)
    return cfg


class BrowserIn(BaseModel):
    name: str = "auto"


def _browser_busy():
    return any(s.get("running") for s in
               (_state, _liepin_state, _unified_state, _detail_state, _apply_state))


@app.get("/api/browser")
def api_browser_get():
    """抓取用的浏览器：本机装了哪些、当前用哪个、偏好是什么"""
    avail = crawler.available_browsers()
    pref = crawler.get_browser_pref()
    name, exe = crawler.pick_browser()
    env = (os.environ.get(crawler.PREF_ENV) or "").strip().lower()
    return {
        "pref": pref,
        "env_locked": env if env in ("chrome", "edge") else "",
        "current": {
            "name": name or "",
            "label": crawler.BROWSER_LABELS.get(name, ""),
            "exe": exe or "",
            "version": next((b["version"] for b in avail if b["name"] == name), ""),
        },
        "available": avail,
    }


@app.post("/api/browser")
def api_browser_set(body: BrowserIn):
    """切换抓取用浏览器（auto 时 Chrome 优先、没装才用 Edge）。
    切换会关掉现有实例，下次获取即用新浏览器；profile 按浏览器分家，
    新浏览器里要重新扫一次码，Cookie 文件会自动注入能救回一部分登录态。"""
    try:
        pref = crawler.set_browser_pref(body.name)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    avail = {b["name"] for b in crawler.available_browsers()}
    if pref != "auto" and pref not in avail:
        return {"ok": False, "msg": f"本机没装 {crawler.BROWSER_LABELS.get(pref, pref)}，无法切换"}
    name, _ = crawler.pick_browser()
    label = crawler.BROWSER_LABELS.get(name, name or "无")
    if _browser_busy():
        return {"ok": True, "msg": f"已保存为 {label}；当前有任务在跑，本轮结束后生效"}
    try:
        crawler.close_all()
    except Exception:
        pass
    return {"ok": True, "msg": f"已切到 {label}" + ("（自动选择 Chrome 优先）" if pref == "auto" else "")}


# ---------- 远程访问（内网穿透）状态 ----------
# 页面里要能看到「隧道开没开、公网地址是多少、口令是什么」，
# 不用再去翻 tunnel.py 的窗口输出。
def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _pid_running(pid):
    if not pid or not str(pid).isdigit():
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid],
                             capture_output=True, text=True, errors="ignore",
                             timeout=8).stdout
        return str(pid) in out
    except Exception:
        return False


def _req_is_local(request):
    """本机直连判据，与 _access_gate 保持同一口径：
    只看来源 IP 会被隧道流量骗过去（ngrok/cloudflared 都从本机转发），必须同时看 Host 头。"""
    host = (request.client.host if request.client else "") or ""
    if host == "testclient":     # e2e 的虚拟来源
        return True
    host_hdr = (request.headers.get("host") or "").split(":")[0].strip().lower().strip("[]")
    via_proxy = bool(request.headers.get("cf-connecting-ip") or request.headers.get("cf-ray")
                     or request.headers.get("x-forwarded-for") or request.headers.get("ngrok-trace-id"))
    return (host in ("127.0.0.1", "::1", "localhost") and not via_proxy
            and host_hdr in ("127.0.0.1", "localhost", "::1"))


@app.get("/api/tunnel")
def api_tunnel(request: Request):
    """远程访问状态：隧道在不在跑、公网地址、口令、是否固定域名。
    只读现有线索，不启动/停止任何进程。"""
    import tunnel as tun

    url = _read_text(tun.URL_FILE)
    try:
        live = tun._ngrok_public_url(wait=0)   # ngrok 在跑时 4040 API 是最新最准的信号
    except Exception:
        live = ""
    if live:
        url = live

    pid = _read_text(tun.PID_FILE)
    pid_alive = _pid_running(pid)

    if live or "ngrok" in url:
        provider = "ngrok"
    elif "cloudflare" in url or "trycloudflare" in url:
        provider = "cloudflare"
    else:
        provider = _read_text(tun.PROVIDER_FILE).lower()
    # 「地址是否固定」：ngrok 只要配了 authtoken，给的域名就是账号专属的，重开隧道不变；
    # cloudflared 只有具名隧道才固定，快速隧道每次随机。
    # （以前只看 ngrok_domain.txt，但那个文件从没被写过，导致固定域名被误标成"会变"）
    fixed = bool((provider == "ngrok" and _read_text(tun.NG_TOKEN_FILE))
                 or (provider == "cloudflare" and _read_text(tun.CF_HOST_FILE)))

    # 「在线」要同时满足：有地址 + 进程还在。
    # 只看到地址文件是不够的：用户关掉隧道窗口后文件还在，地址已经失效了。
    online = bool(live) or bool(url and pid_alive)

    key = _access_key()
    local = _req_is_local(request)
    return {
        "local_url": "http://127.0.0.1:8765",
        "online": online,
        "provider": provider,
        "provider_label": {"ngrok": "ngrok", "cloudflare": "Cloudflare"}.get(provider, "隧道"),
        "url": url,
        "stale": bool(url and not online),   # 地址是上次的，隧道已断
        "fixed": fixed,
        "pid": int(pid) if pid.isdigit() else None,
        "key_set": bool(key),
        # 口令只在本机直连时明文返回；走隧道访问的人已经在门禁内，没必要再暴露一次
        "key": key if local else ("已设置" if key else ""),
        "is_local_request": local,
        "checked_at": datetime.now().strftime("%H:%M:%S"),
    }


_TUNNEL_DIR = _os.path.dirname(_os.path.abspath(__file__))
_TUNNEL_LOG = _os.path.join(_TUNNEL_DIR, "logs", "tunnel.log")
_tunnel_starting = {"flag": False}


@app.post("/api/tunnel/start")
def api_tunnel_start():
    """页面上一键起隧道（后台常驻，不占窗口，关窗口也不断）。

    以前只能双击 tunnel.bat：那个窗口一关隧道就断，公网地址立刻变 3200 离线。
    这里用 DETACHED_PROCESS 拉起，和 serve.py 起服务一个路子。
    """
    import tunnel as tun
    try:
        live = tun._ngrok_public_url(wait=0)
    except Exception:
        live = ""
    if live:
        return {"ok": True, "msg": "隧道已经在运行", "url": live}
    if _tunnel_starting["flag"]:
        return {"ok": False, "msg": "正在启动中，请等几秒再看"}

    _tunnel_starting["flag"] = True
    try:
        _os.makedirs(_os.path.dirname(_TUNNEL_LOG), exist_ok=True)
        env = tun.clean_env()      # 关键：剥掉 HTTP(S)_PROXY，否则 ngrok 直接报 ERR_NGROK_9009
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        flags = 0x00000008 | 0x08000000          # DETACHED_PROCESS | CREATE_NO_WINDOW
        with open(_TUNNEL_LOG, "a", encoding="utf-8", errors="replace") as lf:
            lf.write("\n===== 启动隧道 %s =====\n" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            lf.flush()
            subprocess.Popen([sys.executable, _os.path.join(_TUNNEL_DIR, "tunnel.py")],
                             cwd=_TUNNEL_DIR, stdin=subprocess.DEVNULL, stdout=lf,
                             stderr=subprocess.STDOUT, close_fds=True,
                             creationflags=flags, env=env)
        deadline = time.time() + 40
        url = ""
        while time.time() < deadline:
            try:
                url = tun._ngrok_public_url(wait=1)
            except Exception:
                url = ""
            if url:
                break
        if url:
            return {"ok": True, "msg": "隧道已启动", "url": url}

        tail = ""
        try:
            with open(_TUNNEL_LOG, encoding="utf-8", errors="ignore") as f:
                tail = f.read()[-500:]
        except OSError:
            pass
        low = tail.lower()
        if "9009" in tail or "proxy_url" in low or "http/s proxy" in low:
            return {"ok": False, "msg": "ngrok 免费版不能走 HTTP 代理（ERR_NGROK_9009）："
                                        "检查 %LOCALAPPDATA%\\ngrok\\ngrok.yml 里有没有 proxy_url，"
                                        "或换 Cloudflare 隧道"}
        return {"ok": False, "msg": "启动超时（40 秒没拿到地址），详见 logs/tunnel.log"}
    finally:
        _tunnel_starting["flag"] = False


@app.post("/api/tunnel/stop")
def api_tunnel_stop():
    import tunnel as tun
    pid = _read_text(tun.PID_FILE)
    if not pid.isdigit():
        return {"ok": False, "msg": "没记录到隧道进程；如果它还开着，去那个窗口按 Ctrl+C"}
    ok = False
    try:
        r = subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, timeout=15)
        ok = r.returncode == 0
    except Exception:
        ok = False
    try:
        _os.remove(tun.PID_FILE)
    except OSError:
        pass
    return {"ok": ok, "msg": "隧道已停止" if ok else "停止失败，进程可能已经退出了"}


def _next_run_str():
    j = scheduler.get_job("daily_crawl")
    if not j:
        return None
    for attr in ("next_run_time",):
        try:
            v = getattr(j, attr)
            if v:
                return str(v)
        except Exception:
            pass
    try:  # APScheduler 3.11+: 从调度器取
        ts = scheduler.get_next_job_run_time(j) if hasattr(scheduler, "get_next_job_run_time") else None
        if ts:
            return str(ts)
    except Exception:
        pass
    return "已注册"


@app.post("/api/config")
def api_set_config(body: ConfigIn):
    items = body.dict()
    items["cities"] = json.dumps(items["cities"], ensure_ascii=False)
    items["greeting"] = (items.get("greeting") or "").strip()[:200]
    # 投递参数边界钳制
    items["apply_max_batch"] = max(1, min(int(items.get("apply_max_batch") or 20), 50))
    imin = max(3, min(int(items.get("apply_interval_min") or 12), 120))
    imax = max(imin, min(int(items.get("apply_interval_max") or 30), 300))
    items["apply_interval_min"], items["apply_interval_max"] = imin, imax
    items["detail"] = "0"  # 获取轮固定不抓描述（统一走补抓），顺带纠正历史配置
    items["boss_enabled"] = "0" if str(items.get("boss_enabled")) == "0" else "1"
    items["liepin_enabled"] = "1" if str(items.get("liepin_enabled")) == "1" else "0"
    if items["boss_enabled"] == "0" and items["liepin_enabled"] == "0":
        return {"ok": False, "msg": "至少启用一个平台（BOSS 或 猎聘）"}
    items["liepin_pages"] = max(1, min(int(items.get("liepin_pages") or 2), 5))
    db.set_config(items)
    _apply_schedule()
    return {"ok": True, "next": _next_run_str()}


@app.post("/api/crawl")
def api_crawl(platform: str = "all"):
    plat = (platform or "all").strip().lower()
    if plat == "liepin":
        if _liepin_state["running"]:
            return {"ok": False, "msg": "已有猎聘获取任务在运行"}
        risk = db.get_risk(source="liepin")
        if risk["active"]:
            return {"ok": False, "msg": f"猎聘风控/登录冷却中：{risk['msg'] or risk['code']}，{risk['until'][11:16]} 后再试"}
        if liepin.get_login_state().get("login_open"):
            return {"ok": False, "msg": "猎聘登录窗口进行中，请先完成登录"}
        cfg = db.get_config()
        if str(cfg.get("liepin_enabled", "0")) != "1":
            return {"ok": False, "msg": "猎聘平台未启用：请先在配置里打开「猎聘」开关"}
        t = threading.Thread(target=do_crawl_liepin, args=["manual"], daemon=True)
        t.start()
        return {"ok": True, "msg": "已启动猎聘静默获取，完成后自动入库"}
    if plat == "boss":
        if _state["running"]:
            return {"ok": False, "msg": "已有获取任务在运行"}
        cfg = db.get_config()
        if str(cfg.get("boss_enabled", "1")) != "1":
            return {"ok": False, "msg": "BOSS 平台未启用：请先在配置里打开「BOSS」开关"}
        if not crawler.cookies_have_login():
            return {"ok": False, "need_login": True,
                    "msg": "BOSS 还没真正登录（Cookie 里没有 wt2，当前是游客态）：点顶部「BOSS」胶囊扫码登录后再获取"}
        risk = db.get_risk()
        if risk["active"]:
            return {"ok": False, "msg": _risk_block_msg(risk)}
        with _apply_lock:
            if _apply_state["running"] or _detail_state["running"]:
                return {"ok": False, "msg": "投递/补抓进行中，请等待或取消"}
        if crawler.get_login_state().get("login_open") or crawler.repair_state().get("running"):
            return {"ok": False, "msg": "登录/修复窗口进行中：请在窗口内完成验证，或点横幅上的「✕ 取消修复」"}
        t = threading.Thread(target=do_crawl, args=["manual"], daemon=True)
        t.start()
        return {"ok": True, "msg": "已启动静默获取，完成后自动入库"}
    # ---- platform=all：统一入口，按开关顺序跑（BOSS → 猎聘，自动错峰）----
    cfg = db.get_config()
    plats = enabled_platforms(cfg)
    if not plats:
        return {"ok": False, "msg": "未启用任何平台：请在配置里至少打开 BOSS 或 猎聘"}
    if _state["running"] or _liepin_state["running"] or _unified_state["running"]:
        return {"ok": False, "msg": "已有获取任务在运行，请等待本轮结束"}
    blocked = []
    need_login = False
    if "boss" in plats:
        risk = db.get_risk()
        if risk["active"]:
            blocked.append(f"BOSS 风控冷却中（{risk['until'][11:16]} 解除）")
        if crawler.get_login_state().get("login_open") or crawler.repair_state().get("running"):
            blocked.append("BOSS 登录/修复窗口进行中")
        if not crawler.cookies_have_login():
            blocked.append("BOSS 未真正登录（Cookie 无 wt2），请点顶部「BOSS」胶囊扫码登录")
            need_login = True
        with _apply_lock:
            if _apply_state["running"]:
                blocked.append("投递进行中")
            if _detail_state["running"]:
                blocked.append("描述补抓进行中")
    if "liepin" in plats:
        rl = db.get_risk(source="liepin")
        if rl["active"]:
            blocked.append(f"猎聘风控/登录冷却中（{rl['until'][11:16]} 解除）")
        if liepin.get_login_state().get("login_open"):
            blocked.append("猎聘登录窗口进行中")
    hard = [b for b in blocked if "登录" in b or "修复" in b or "投递" in b or "补抓" in b]
    if hard:
        return {"ok": False, "need_login": need_login,
                "msg": "；".join(hard) + "，请处理后再获取"}
    if blocked and len(blocked) == len(plats):
        return {"ok": False, "msg": "；".join(blocked) + "，本轮没有可跑的平台"}

    def _run():
        with _unified_state["lock"]:
            if _unified_state["running"]:
                return
            _unified_state["running"] = True
        try:
            do_crawl_all("manual")
        finally:
            with _unified_state["lock"]:
                _unified_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "已启动统一获取（" + " → ".join(plats) + "），各轮完成后自动入库"}


@app.get("/api/status")
def api_status():
    runs = db.get_runs(1)
    r = runs[0] if runs else {}
    cfg = db.get_config()
    pg = dict(_PROGRESS)
    pg["pct"] = round(pg["done"] * 100.0 / pg["total"]) if pg.get("total") else 0
    return {"running": _state["running"] or _liepin_state["running"] or _unified_state["running"],
            "boss_running": _state["running"], "liepin_running": _liepin_state["running"],
            "platforms": enabled_platforms(cfg), "last_run": r, "progress": pg}


@app.get("/api/login_state")
def api_login_state(platform: str = "boss"):
    if (platform or "boss") == "liepin":
        return liepin.get_login_state()
    return crawler.get_login_state()


@app.post("/api/login")
def api_login(platform: str = "boss"):
    if (platform or "boss") == "liepin":
        st = liepin.get_login_state()
        if st["login_open"]:
            return {"ok": True, "msg": "猎聘登录窗口已打开，请在弹出窗口完成登录，成功后自动关闭"}
        if st["logged"]:
            return {"ok": True, "msg": "猎聘已是登录状态，无需重复登录"}
        if _liepin_state["running"]:
            return {"ok": False, "msg": "猎聘获取进行中，请等本轮结束再登录"}
        threading.Thread(target=liepin.login_flow, daemon=True).start()
        return {"ok": True, "msg": "已打开猎聘登录窗口：请手机号/扫码登录，成功后窗口自动关闭、Cookie自动保存"}
    st = crawler.get_login_state()
    if st["login_open"]:
        return {"ok": True, "msg": "登录窗口已打开，请在弹出窗口完成登录，登录后自动关闭"}
    if st["logged"]:
        return {"ok": True, "msg": "已是登录状态，无需重复登录；若获取仍被风控，请点「🔧 修复登录态」"}
    if crawler.repair_state()["running"]:
        return {"ok": False, "msg": "修复窗口进行中，请先完成或取消"}
    if _state["running"]:
        return {"ok": False, "msg": "获取进行中，请等本轮结束再登录"}
    with _apply_lock:
        if _apply_state["running"]:
            return {"ok": False, "msg": "投递进行中，请等待或取消"}
    threading.Thread(target=crawler.login_flow, daemon=True).start()
    return {"ok": True, "msg": "已打开登录窗口：请手机号/扫码登录，成功后窗口自动关闭、Cookie自动保存"}


@app.post("/api/cancel_login")
def api_cancel_login(platform: str = "boss"):
    if (platform or "boss") == "liepin":
        if liepin.get_login_state()["login_open"]:
            liepin.cancel_login()
            return {"ok": True, "msg": "已请求关闭猎聘登录窗口"}
        return {"ok": False, "msg": "当前没有猎聘登录窗口"}
    if crawler.get_login_state()["login_open"]:
        crawler.cancel_login()
        return {"ok": True, "msg": "已请求关闭登录窗口"}
    return {"ok": False, "msg": "当前没有登录窗口"}


# ---------- 风控冷却 / 修复登录态 ----------
@app.get("/api/risk")
def api_risk():
    out = db.get_risk()
    out["liepin"] = db.get_risk(source="liepin")
    return out


@app.post("/api/risk_clear")
def api_risk_clear(platform: str = "boss"):
    """手动解除冷却锁（确认风控已过时用）"""
    if (platform or "boss") == "liepin":
        db.clear_risk(source="liepin")
        return {"ok": True, "msg": "已解除猎聘冷却锁，可重新获取（若登录失效请先点「登录猎聘」）"}
    db.clear_risk()
    crawler.clear_last_risk()
    return {"ok": True, "msg": "已解除风控冷却锁，可以重新获取（建议先修复登录态）"}


@app.get("/api/quota")
def api_quota():
    """今日接口请求预算（一次翻页算一次），两平台各算各的"""
    out = db.get_quota(crawler.DAILY_QUOTA)
    out["liepin"] = db.get_quota(liepin.DAILY_QUOTA, source="liepin")
    return out


@app.post("/api/repair")
def api_repair():
    """打开可见窗口：过安全验证 / 重新登录，并轮询直到接口放行"""
    if crawler.repair_state()["running"]:
        return {"ok": True, "msg": "修复窗口已打开，请在窗口中完成滑块验证"}
    if _state["running"]:
        return {"ok": False, "msg": "获取进行中，请等本轮结束"}
    with _apply_lock:
        if _apply_state["running"]:
            return {"ok": False, "msg": "投递进行中，请等待或取消"}
    cfg = db.get_config()
    keywords, cities, _p, _d, _h, _o, _deg = _parse_cfg(cfg)
    kw = keywords[0] if keywords else "数据分析"
    cc = cities[0]["code"] if cities else "101230200"

    def _run():
        res = crawler.repair_flow(kw, cc)
        crawler._repair["last"] = res.get("msg") or ""
        print("修复结果:", res, flush=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "已打开修复窗口：如出现滑块请手动完成，接口放行后窗口自动关闭、Cookie自动刷新"}


@app.get("/api/repair_state")
def api_repair_state():
    st = crawler.repair_state()
    st["login_open"] = crawler.get_login_state().get("login_open", False)
    return st


@app.post("/api/repair_cancel")
def api_repair_cancel():
    if crawler.repair_state()["running"]:
        crawler.cancel_repair()
        return {"ok": True, "msg": "已请求关闭修复窗口"}
    return {"ok": False, "msg": "当前没有修复窗口"}


# ---------- 职位描述补抓 ----------
def _log_detail(line):
    ts = datetime.now().strftime("[%H:%M:%S] ")
    with _apply_lock:
        _detail_state["log"].append(ts + line)
        if len(_detail_state["log"]) > 300:
            _detail_state["log"] = _detail_state["log"][-300:]
    print(line, flush=True)


def _rescore(rid=None):
    """入库/补抓后重算匹配分。

    之前只在「画像更新」时才算分，导致新抓进来的岗位（尤其猎聘）匹配分一直是 0。
    """
    try:
        n = db.recompute_scores()
        if rid:
            db.append_run_log(rid, f"🎯 已按画像重算 {n} 条岗位匹配分")
        return n
    except Exception as e:
        if rid:
            db.append_run_log(rid, f"⚠️ 匹配分重算失败：{e}")
        return 0


def _do_details(job_ids, filters, limit):
    try:
        if job_ids:  # 只补勾选的
            picked = [db.get_job(i) for i in job_ids]
            jobs = [j for j in picked if j and j.get("status") != "offline"
                    and not (j.get("detail_text") or "").strip()]
            total = len(jobs)
            jobs = jobs[:limit]
            scope = "勾选岗位"
        else:        # 按当前筛选条件
            f = dict(filters or {})
            f["has_detail"] = "0"          # 只补缺描述的
            if not f.get("status"):
                f["status"] = "active"
            total, rows = db.query_jobs({**f, "page": 1, "size": limit})
            jobs = rows
            scope = "当前筛选结果"
        # 两个平台的详情页结构完全不同：BOSS 走 crawler，猎聘走 liepin.run_details
        boss_jobs = [j for j in jobs if (j.get("source") or "") != "liepin"]
        lp_jobs = [j for j in jobs if (j.get("source") or "") == "liepin"]
        with _apply_lock:
            _detail_state.update(log=[], total=total, done=0)
        if not boss_jobs and not lp_jobs:
            _log_detail(f"{scope}里没有需要补抓的岗位（已有描述的会跳过）")
            return
        _log_detail(f"📄 {scope}：待补抓 {total} 条（BOSS {len(boss_jobs)} / 猎聘 {len(lp_jobs)}），"
                    f"每条约 3~6 秒，防风控")
        ok = fail = 0
        if boss_jobs:
            crawler.clear_last_risk()
            b_ok, b_fail = crawler.backfill_details(
                boss_jobs, _log_detail,
                should_cancel=lambda: _detail_cancel["flag"],
                on_progress=lambda i, t: _detail_state.update(done=i, total=t))
            ok += b_ok
            fail += b_fail
            if crawler.LAST_RISK.get("code") is not None:
                risk = db.set_risk(crawler.LAST_RISK["code"], crawler.LAST_RISK["msg"])
                _log_detail(f"🔒 已上风控冷却锁 {risk['remaining_min']} 分钟")
        if lp_jobs and not _detail_cancel["flag"]:
            _log_detail(f"🟠 猎聘详情：{len(lp_jobs)} 条（取 JD / 福利 / 区县）")
            l_ok, l_fail = liepin.run_details(
                lp_jobs, _log_detail,
                should_cancel=lambda: _detail_cancel["flag"],
                on_progress=lambda i, t: _detail_state.update(done=i, total=t))
            ok += l_ok
            fail += l_fail
        _log_detail(f"✅ 完成：成功 {ok} 条，失败 {fail} 条")
        # 描述会进入匹配文本，补完必须重算，否则新描述不参与打分
        if ok:
            n = _rescore()
            _log_detail(f"🎯 已按画像重算 {n} 条岗位匹配分")
    except Exception as e:
        _log_detail(f"❌ 补抓异常：{e}")
    finally:
        with _apply_lock:
            _detail_state["running"] = False
            _detail_cancel["flag"] = False


@app.post("/api/details")
def api_details(body: DetailsIn):
    limit = max(1, min(int(body.limit or 20), 40))
    with _apply_lock:
        if _detail_state["running"]:
            return {"ok": False, "msg": "已有补抓任务在运行"}
    if _state["running"]:
        return {"ok": False, "msg": "获取进行中，请等本轮结束"}
    if _apply_state["running"]:
        return {"ok": False, "msg": "投递进行中，请等待或取消"}
    risk = db.get_risk()
    if risk["active"]:
        return {"ok": False, "msg": _risk_block_msg(risk)}
    if crawler.repair_state()["running"] or crawler.get_login_state().get("login_open"):
        return {"ok": False, "msg": "登录/修复窗口进行中，请先完成"}
    ids = [str(x) for x in (body.job_ids or []) if x]
    if not crawler.get_login_state().get("logged") and _pending_has_boss(ids, body.filters or {}):
        return {"ok": False, "msg": "未登录：BOSS 岗位补抓需要先点顶部「BOSS」胶囊登录（只补猎聘的话不需要）"}
    with _apply_lock:
        _detail_state["running"] = True
    threading.Thread(target=_do_details, args=[ids, body.filters or {}, limit], daemon=True).start()
    return {"ok": True, "msg": f"已启动补抓（{'勾选 '+str(len(ids))+' 条' if ids else '当前筛选结果'}，本批最多 {limit} 条）"}


def _pending_has_boss(job_ids, filters):
    """本次补抓范围里是否含 BOSS 岗位（决定要不要 BOSS 登录态）"""
    if job_ids:
        return any((db.get_job(i) or {}).get("source", "boss") != "liepin" for i in job_ids)
    f = dict(filters or {})
    f["has_detail"] = "0"
    if not f.get("status"):
        f["status"] = "active"
    _, rows = db.query_jobs({**f, "page": 1, "size": 40})
    return any((j.get("source") or "boss") != "liepin" for j in rows)


# ---------- 城市库 ----------
_city_state = {"running": False, "last": ""}
_lp_city_state = {"running": False, "last": ""}


@app.get("/api/liepin_cities")
def api_liepin_cities():
    """猎聘支持的城市（配置页用来标注哪些城市能抓猎聘）"""
    m, at = liepin.load_cities()
    return {"cities": sorted(m.keys()), "count": len(m), "synced_at": at,
            "syncing": _lp_city_state["running"]}


@app.post("/api/liepin_cities/sync")
def api_liepin_cities_sync():
    """从猎聘接口拉全量城市码（与 BOSS 城市库同步对等）"""
    if _lp_city_state["running"]:
        return {"ok": True, "msg": "正在同步中，请稍候…"}
    if _state["running"] or _detail_state["running"] or _apply_state["running"]:
        return {"ok": False, "msg": "有任务在跑（获取/补抓/投递），请等它结束再同步城市"}

    def _run():
        _lp_city_state["running"] = True
        try:
            ok, info = liepin.sync_cities()
            _lp_city_state["last"] = f"成功同步 {info} 个城市" if ok else f"同步失败：{info}"
        finally:
            _lp_city_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "已开始同步猎聘城市库（几秒钟）"}


@app.get("/api/liepin_cities/sync_state")
def api_liepin_cities_sync_state():
    m, at = liepin.load_cities()
    return {"running": _lp_city_state["running"], "last": _lp_city_state["last"],
            "count": len(m), "synced_at": at}


@app.get("/api/cities")
def api_cities():
    cities, at = crawler.load_cities()
    return {"cities": cities, "synced_at": at, "count": len(cities),
            "syncing": _city_state["running"]}


@app.post("/api/cities/sync")
def api_cities_sync():
    if _city_state["running"]:
        return {"ok": True, "msg": "正在同步中，请稍候…"}
    if _state["running"] or _detail_state["running"] or _apply_state["running"]:
        return {"ok": False, "msg": "有任务在跑（获取/补抓/投递），请等它结束再同步城市"}
    if crawler.repair_state()["running"] or crawler.get_login_state().get("login_open"):
        return {"ok": False, "msg": "登录/修复窗口进行中，请先完成"}

    def _run():
        _city_state["running"] = True
        try:
            ok, info = crawler.sync_cities()
            _city_state["last"] = f"成功同步 {info} 个城市" if ok else f"同步失败：{info}"
        finally:
            _city_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "已开始同步 BOSS 城市库（几秒钟）"}


@app.get("/api/cities/sync_state")
def api_cities_sync_state():
    cities, at = crawler.load_cities()
    return {"running": _city_state["running"], "last": _city_state["last"],
            "count": len(cities), "synced_at": at}


@app.get("/api/stats")
def api_stats(days: int = 14, source: str = ""):
    """看板聚合：source='' 全部 / 'boss' / 'liepin'"""
    return db.stats(max(7, min(days, 60)), source=(source or "").strip())


@app.get("/api/skills/gap")
def api_skills_gap(source: str = "", min_jobs: int = 3):
    """技能缺口：岗位要什么、我有什么、差什么"""
    return db.skill_gap(source=(source or "").strip(), min_jobs=max(1, min(int(min_jobs), 20)))


@app.get("/api/skills/salary")
def api_skills_salary(source: str = "", min_jobs: int = 5):
    """技能薪资分析：哪些技能在高薪岗位中更常见、薪资溢价多少（市场需求侧）"""
    return db.skill_salary(source=(source or "").strip(),
                           min_jobs=max(2, min(int(min_jobs), 30)))


@app.get("/api/conversion")
def api_conversion(by: str = "source", source: str = ""):
    """投递转化分析：by=source/scale/keyword/city/degree"""
    return db.conversion(by=(by or "source"), source=(source or "").strip())


@app.get("/api/schedule")
def api_schedule(days: int = 14):
    """跟进日程：未来 N 天的面试/沟通安排"""
    d = max(1, min(int(days), 90))
    return {"days": d, "rows": db.schedule(d)}


class NextAtIn(BaseModel):
    job_id: str = ""
    at: str = ""
    note: str = ""


@app.post("/api/next_at")
def api_next_at(body: NextAtIn):
    if not body.job_id:
        return {"ok": False, "msg": "缺少 job_id"}
    n = db.set_next_at(body.job_id, body.at, body.note)
    return {"ok": bool(n), "msg": "已更新跟进时间" if body.at else "已清除跟进时间"}


class BulkIn(BaseModel):
    job_ids: list = []
    action: str = ""
    value: str = ""


@app.post("/api/bulk")
def api_bulk(body: BulkIn):
    """批量操作：收藏/阶段/已回复（不含投递，投递有单独的串行队列）"""
    n = db.bulk_update(body.job_ids, body.action, body.value)
    label = {"star": "收藏", "stage": "求职阶段", "replied": "HR 已回复"}.get(body.action, "操作")
    return {"ok": n > 0, "count": n, "msg": f"已批量更新{label} {n} 条" if n else "没有更新的记录"}


@app.post("/api/backup")
def api_backup():
    p = db.backup()
    return {"ok": bool(p), "msg": f"已备份到 {p}" if p else "备份失败", "path": p}


@app.post("/api/offline/recover")
def api_offline_recover(hours: int = 24, source: str = None):
    """撤销「已下线」判定（抓取不完整导致误杀时回滚）。hours<=0 表示不限时间、全部恢复"""
    hours = int(hours or 24)
    hours = 0 if hours <= 0 else min(hours, 24 * 30)
    n = db.recover_offline(hours=hours, source=source or None)
    scope = "全部" if hours <= 0 else f"最近 {hours} 小时内"
    return {"ok": n > 0, "count": n,
            "msg": f"已恢复 {n} 条为在招" if n else f"{scope}没有被判下线的岗位"}


@app.get("/api/details_status")
def api_details_status():
    with _apply_lock:
        return {"running": _detail_state["running"], "done": _detail_state["done"],
                "total": _detail_state["total"], "log": "\n".join(_detail_state["log"][-60:])}


@app.post("/api/details_cancel")
def api_details_cancel():
    with _apply_lock:
        if not _detail_state["running"]:
            return {"ok": False, "msg": "当前没有补抓任务"}
    _detail_cancel["flag"] = True
    return {"ok": True, "msg": "已请求停止（当前岗位抓完后停止）"}


@app.get("/api/jobs")
def api_jobs(keyword: str = "", city_code: str = "", status: str = "",
             is_new: int = 0, applied: str = "", degree: str = "", experience: str = "",
             scale: str = "", industry: str = "", salary_min: str = "", salary_max: str = "",
             no_salary: str = "", has_detail: str = "", starred: str = "", replied: str = "",
             dedupe: str = "", q: str = "", sort: str = "salary_desc", page: int = 1, size: int = 50,
             overdue: str = "", stage: str = "", source: str = ""):
    total, rows = db.query_jobs({
        "keyword": keyword, "city_code": city_code, "status": status,
        "is_new": is_new, "applied": applied, "degree": degree, "experience": experience,
        "scale": scale, "industry": industry, "salary_min": salary_min, "salary_max": salary_max,
        "no_salary": no_salary, "has_detail": has_detail, "starred": starred, "replied": replied,
        "dedupe": dedupe, "q": q, "sort": sort, "page": page, "size": size, "overdue": overdue,
        "stage": stage, "source": source})
    return {"total": total, "page": page, "size": size, "rows": rows, "counts": db.counts()}


@app.get("/api/job")
def api_job(job_id: str = ""):
    """单条岗位（详情面板用）：不再让前端拉 200 条再本地找，翻页后的岗位也能打开"""
    if not job_id:
        return {"ok": False, "msg": "缺少 job_id"}
    j = db.get_job(job_id)
    if not j:
        return {"ok": False, "msg": "岗位不存在"}
    return {"ok": True, "job": j}


@app.get("/api/counts")
def api_counts():
    return db.counts()


@app.post("/api/star")
def api_star(body: StarIn):
    if not body.job_id:
        return {"ok": False, "msg": "缺少 job_id"}
    n = db.set_star(body.job_id, body.star)
    return {"ok": bool(n), "starred": 1 if body.star else 0}


@app.post("/api/note")
def api_note(body: NoteIn):
    if not body.job_id:
        return {"ok": False, "msg": "缺少 job_id"}
    db.set_note(body.job_id, body.note)
    return {"ok": True, "note": (body.note or "")[:500]}


@app.post("/api/stage")
def api_stage(body: StageIn):
    """标记/流转求职阶段：面试 / Offer / 未通过（传空清除）"""
    if not body.job_id:
        return {"ok": False, "msg": "缺少 job_id"}
    if not db.get_job(body.job_id):
        return {"ok": False, "msg": "岗位不存在"}
    stage = str(body.stage or "").strip().lower()
    if stage and stage not in db.STAGES:
        return {"ok": False, "msg": f"无效阶段：{stage}"}
    n = db.set_stage(body.job_id, stage, body.note)
    label = db.STAGE_LABELS.get(stage, "")
    return {"ok": bool(n), "stage": stage,
            "msg": (f"已标记「{label}」" if stage else "已清除阶段标记")}


@app.get("/api/stages")
def api_stages(source: str = ""):
    """阶段跟进视图数据：面试/Offer/未通过 三组 + 漏斗计数（支持按平台筛选）"""
    data = db.get_staged()
    ct = db.counts((source or "").strip())
    data["funnel"] = {
        "active": ct["active"], "applied": ct["applied"], "replied": ct["replied"],
        "interview": ct["interview"], "offer": ct["offer"], "rejected": ct["rejected"]}
    return data


# ---------- 画像（简历解析） ----------
_profile_state = {"running": False, "last": "", "source": ""}


def _apply_profile(prof, note=""):
    """画像落库并重算匹配分"""
    db.save_profile(prof)
    n = db.recompute_scores(prof)
    core = "、".join((prof.get("core_skills") or [])[:6]) or "无"
    msg = (f"{note}｜识别 {len(prof.get('skills') or [])} 个技能（核心：{core}）"
           f"｜学历 {prof.get('degree') or '未识别'}｜{prof.get('years') or '?'} 年经验"
           f"｜期望 {prof.get('expect_salary') or '?'}K"
           f"｜已重算 {n} 条岗位匹配分")
    return msg


@app.get("/api/profile")
def api_profile_get():
    prof = db.get_profile()
    prof["configured"] = bool(prof.get("skills"))
    return prof


@app.post("/api/profile/sync_boss")
def api_profile_sync_boss():
    """从 BOSS 线上简历只读同步（不发任何写请求）"""
    if _profile_state["running"]:
        return {"ok": True, "msg": "正在同步简历中"}
    if _state["running"] or _detail_state["running"] or _apply_state["running"]:
        return {"ok": False, "msg": "有任务在跑（获取/补抓/投递），稍后再同步简历"}
    if crawler.repair_state()["running"] or crawler.get_login_state().get("login_open"):
        return {"ok": False, "msg": "登录/修复窗口进行中"}
    if not crawler.get_login_state().get("logged"):
        return {"ok": False, "msg": "未登录：请先点「登录 / 修复」"}

    def _run():
        _profile_state["running"] = True
        try:
            text, msg = crawler.fetch_resume_text()
            if not text:
                _profile_state["last"] = f"同步失败：{msg}"
                return
            prof = resume_mod.parse(text, source="boss")
            if not prof:
                _profile_state["last"] = "同步失败：简历正文为空"
                return
            _profile_state["last"] = _apply_profile(prof, "已从 BOSS 简历同步")
            _profile_state["source"] = "boss"
        except Exception as e:
            _profile_state["last"] = f"同步异常：{e}"
        finally:
            _profile_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": "已开始读取 BOSS 线上简历（只读，不会修改简历）"}


@app.get("/api/profile/sync_state")
def api_profile_sync_state():
    return _profile_state


class ResumeIn(BaseModel):
    filename: str = ""
    content_b64: str = ""


@app.post("/api/profile/upload")
def api_profile_upload(body: ResumeIn):
    """上传简历解析：支持 PDF / DOCX / TXT / MD（base64 传输，免 multipart 依赖）"""
    if not body.content_b64:
        return {"ok": False, "msg": "没有收到文件内容"}
    try:
        import base64
        raw = base64.b64decode(body.content_b64.split(",")[-1])
    except Exception as e:
        return {"ok": False, "msg": f"文件解码失败：{e}"}
    if len(raw) > 12 * 1024 * 1024:
        return {"ok": False, "msg": "文件超过 12MB，请压缩后再传"}
    try:
        text = resume_mod.read_upload(body.filename, raw)
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    prof = resume_mod.parse(text, source=f"upload:{body.filename}")
    if not prof:
        return {"ok": False, "msg": "没能从文件里读到有效文本（扫描件 PDF 需先 OCR）"}
    if not prof.get("skills"):
        return {"ok": False, "msg": f"读到 {prof['chars']} 字，但没识别出技能关键词（可能是纯图片或格式特殊）"}
    msg = _apply_profile(prof, f"已解析 {body.filename}")
    _profile_state.update(last=msg, source=prof.get("source", ""))
    return {"ok": True, "msg": msg, "profile": prof}


class ProfilePatchIn(BaseModel):
    expect_salary: int = -1
    degree: str = ""


@app.post("/api/profile/patch")
def api_profile_patch(body: ProfilePatchIn):
    """只允许微调期望薪资/学历（技能始终来自简历解析，不手填）"""
    prof = db.get_profile()
    if not prof.get("skills"):
        return {"ok": False, "msg": "还没有简历画像：先同步 BOSS 简历或上传简历文件"}
    if body.expect_salary >= 0:
        prof["expect_salary"] = body.expect_salary
    if body.degree:
        prof["degree"] = body.degree
    prof["updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    msg = _apply_profile(prof, "画像已更新")
    return {"ok": True, "msg": msg}


# ---------- 回复跟踪 ----------
_reply_state = {"running": False, "last": "", "matched": 0, "parsed": 0, "at": ""}


@app.post("/api/replies/sync")
def api_reply_sync():
    if _reply_state["running"]:
        return {"ok": True, "msg": "正在同步中"}
    if _state["running"] or _detail_state["running"] or _apply_state["running"]:
        return {"ok": False, "msg": "有任务在跑（获取/补抓/投递），稍后再同步回复"}
    if crawler.repair_state()["running"] or crawler.get_login_state().get("login_open"):
        return {"ok": False, "msg": "登录/修复窗口进行中"}
    if not crawler.get_login_state().get("logged"):
        return {"ok": False, "msg": "未登录：请先点「修复登录态」"}

    def _run():
        _reply_state["running"] = True
        try:
            res = crawler.sync_replies()
            _reply_state.update(last=res.get("msg", ""), matched=res.get("matched", 0),
                                parsed=res.get("parsed", 0),
                                at=datetime.now().strftime("%m-%d %H:%M"))
        except Exception as e:
            _reply_state["last"] = f"同步异常：{e}"
            _reply_state["at"] = datetime.now().strftime("%m-%d %H:%M")
        finally:
            _reply_state["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "platform": "boss",
            "msg": "已开始同步 BOSS 会话（只读，不会发消息）；猎聘不支持自动同步，请用「标记 HR 已回复」"}


@app.get("/api/replies/state")
def api_reply_state():
    return _reply_state


@app.get("/api/replies")
def api_replies(limit: int = 200, source: str = ""):
    """投递追踪列表（前端「HR 回复」页）：**投递过就显示**，分「等待回复 / 已回复」两态。

    自动同步只支持 BOSS（读 BOSS 会话列表里的公司名）；猎聘的回复在猎聘站内 IM，
    需要登录态调私有接口，本工具不接入 —— 猎聘岗位只能用「手动标记 HR 已回复」。
    """
    data = db.get_replied(min(max(limit, 1), 500), source=(source or "").strip())
    data["sync"] = _reply_state
    data["sync_supported"] = {"boss": True, "liepin": False}
    return data


class ReplyMarkIn(BaseModel):
    job_id: str = ""
    replied: int = 1
    note: str = ""


@app.post("/api/replies/mark")
def api_reply_mark(body: ReplyMarkIn):
    """手动标记/取消 HR 已回复"""
    if not body.job_id:
        return {"ok": False, "msg": "缺少 job_id"}
    if not db.get_job(body.job_id):
        return {"ok": False, "msg": "岗位不存在"}
    n = db.set_replied(body.job_id, body.replied, body.note)
    return {"ok": bool(n), "replied": 1 if body.replied else 0,
            "msg": "已标记为 HR 已回复" if body.replied else "已取消回复标记"}


@app.get("/api/filters")
def api_filters():
    return db.distinct_filters()


@app.get("/api/runs")
def api_runs():
    return {"runs": db.get_runs(10)}   # 历史列表只展示最近10条


# ---------- 投递 ----------
def _log_apply(line):
    ts = datetime.now().strftime("[%H:%M:%S] ")
    with _apply_lock:
        _apply_state["log"].append(ts + line)
        if len(_apply_state["log"]) > 500:
            _apply_state["log"] = _apply_state["log"][-500:]
    print(line, flush=True)


def _do_apply(job_ids, greeting):
    batch_id = uuid.uuid4().hex[:12]
    cfg = db.get_config()
    max_batch = int(cfg.get("apply_max_batch") or 20)
    interval = (int(cfg.get("apply_interval_min") or 12),
                int(cfg.get("apply_interval_max") or 30))
    with _apply_lock:
        _apply_state.update(running=True, batch_id=batch_id, log=[],
                            done=0, total=len(job_ids))
    try:
        _log_apply(f"📮 开始投递 {len(job_ids)} 条（串行+{interval[0]}~{interval[1]}s随机间隔，防风控）")
        ok_cnt, fail_cnt = apply.run_apply(
            job_ids, greeting, _log_apply, batch_id,
            max_batch=max_batch, interval=interval,
            should_cancel=lambda: _apply_cancel["flag"])
        _log_apply(f"✅ 投递结束：成功 {ok_cnt} ｜ 失败/跳过 {fail_cnt}")
    except Exception as e:
        _log_apply(f"❌ 投递异常：{e}")
    finally:
        with _apply_lock:
            _apply_state["running"] = False
            _apply_cancel["flag"] = False


@app.post("/api/apply")
def api_apply(body: ApplyIn):
    ids = list(dict.fromkeys(str(x) for x in (body.job_ids or []) if x))  # 去重保序
    valid = [jid for jid in ids if db.get_job(jid)]
    skipped_dup = len(ids) - len(valid)
    if not valid:
        return {"ok": False, "msg": "所选岗位不存在或已被删除"}
    cfg = db.get_config()
    max_batch = int(cfg.get("apply_max_batch") or 20)
    valid = valid[:max_batch]
    st = crawler.get_login_state()
    if not st.get("logged"):
        return {"ok": False, "msg": "未登录 BOSS：请先点「🔧 修复登录态」完成登录后再投递"}
    if _state["running"]:
        return {"ok": False, "msg": "获取进行中，请等本轮结束再投递"}
    risk = db.get_risk()
    if risk["active"]:
        return {"ok": False, "msg": _risk_block_msg(risk)}
    with _apply_lock:
        if _apply_state["running"]:
            return {"ok": False, "msg": "已有投递在运行"}
    t = threading.Thread(target=_do_apply, args=[valid, (cfg.get("greeting") or "").strip()], daemon=True)
    t.start()
    extra = "（重复/无效ID已过滤）" if skipped_dup else ""
    return {"ok": True, "msg": f"已启动投递 {len(valid)} 条{extra}（后台静默，日志实时可见）"}


@app.post("/api/apply_retry")
def api_apply_retry():
    """一键重投：把投递失败（applied=2）的岗位重新排队（登录失效/风控失败的都能重投）"""
    st = crawler.get_login_state()
    if not st.get("logged"):
        return {"ok": False, "msg": "未登录 BOSS：请先点顶部「BOSS」胶囊修复登录态，再重投（否则起了批也会秒退）"}
    ids = db.failed_apply_ids(source="boss", limit=int(db.get_config().get("apply_max_batch") or 20))
    if not ids:
        return {"ok": False, "msg": "当前没有投递失败的岗位"}
    if _state["running"]:
        return {"ok": False, "msg": "获取进行中，请等本轮结束再投递"}
    risk = db.get_risk()
    if risk["active"]:
        return {"ok": False, "msg": _risk_block_msg(risk)}
    with _apply_lock:
        if _apply_state["running"]:
            return {"ok": False, "msg": "已有投递在运行"}
    cfg = db.get_config()
    t = threading.Thread(target=_do_apply, args=[ids, (cfg.get("greeting") or "").strip()], daemon=True)
    t.start()
    return {"ok": True, "msg": f"已重新投递 {len(ids)} 条失败岗位（后台静默）"}


@app.get("/api/apply_status")
def api_apply_status():
    with _apply_lock:
        return {"running": _apply_state["running"], "batch_id": _apply_state["batch_id"],
                "log": "\n".join(_apply_state["log"][-80:])}


@app.post("/api/apply_cancel")
def api_apply_cancel():
    with _apply_lock:
        if not _apply_state["running"]:
            return {"ok": False, "msg": "当前没有投递在运行"}
    _apply_cancel["flag"] = True
    return {"ok": True, "msg": "已请求停止（当前岗位操作完成后停止）"}


@app.get("/api/applications")
def api_applications(limit: int = 50, batch_id: str = ""):
    return {"apps": db.get_applications(min(limit, 200), batch_id or None)}


_CSV_SAFE = ("=", "+", "-", "@", "\t", "\r")


def _csv_guard(v):
    """防 CSV 公式注入：以 = + - @ 开头的值加单引号前缀"""
    s = str(v or "")
    return "'" + s if s[:1] in _CSV_SAFE else s


@app.get("/api/export")
def api_export(keyword: str = "", city_code: str = "", is_new: int = 0, status: str = "",
               applied: str = "", degree: str = "", experience: str = "", scale: str = "",
               industry: str = "", salary_min: str = "", salary_max: str = "", no_salary: str = "",
               has_detail: str = "", starred: str = "", replied: str = "", dedupe: str = "",
               q: str = "", sort: str = "salary_desc", overdue: str = "", stage: str = "",
               source: str = ""):
    rows = db.all_job_rows({"keyword": keyword, "city_code": city_code, "is_new": is_new,
                            "status": status, "applied": applied, "degree": degree,
                            "experience": experience, "scale": scale, "industry": industry,
                            "salary_min": salary_min, "salary_max": salary_max,
                            "no_salary": no_salary, "has_detail": has_detail, "starred": starred,
                            "replied": replied, "q": q, "sort": sort, "overdue": overdue,
                            "stage": stage, "source": source})
    fields = ["job_name", "salary", "salary_min", "salary_max", "company", "scale", "industry",
              "degree", "experience", "skills", "welfare", "city_name", "area_district",
              "business_district", "lng", "lat", "match_score", "keyword", "job_url", "detail_text",
              "source",
              "first_seen", "last_seen", "status", "applied", "applied_at", "note",
              "stage", "stage_at", "stage_note",
              "boss_name", "boss_title", "brand_stage", "proxy_job", "gold_hunter"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for r in rows:
        w.writerow([_csv_guard(r.get(f, "")) for f in fields])
    fn = f"boss_jobs_{datetime.now():%Y%m%d_%H%M}.csv"
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode("utf-8-sig")),
        media_type="text/csv", headers={"Content-Disposition": f"attachment; filename={fn}"})


# ---------- 前端 ----------
# ---------- 前端（两份：电脑版 index.html / 手机版 m.html） ----------
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
_MOBILE_UA = re.compile(r"Mobile|Android|iPhone|iPod|Windows Phone|HarmonyOS|MiuiBrowser|MicroMessenger", re.I)


def _serve_html(name):
    """读静态页 + 注入版本戳，并禁止缓存（口令过期后拿到缓存壳会显示「0 条/未登录」的假象）"""
    from datetime import datetime as _dt
    path = os.path.join(_STATIC, name)
    with open(path, encoding="utf-8") as f:
        html = f.read()
    # 版本戳：前端角落显示，用来一眼判断「浏览器是不是还在用旧页面」
    try:
        stamp = _dt.fromtimestamp(os.path.getmtime(path)).strftime("%m-%d %H:%M")
    except OSError:
        stamp = "-"
    html = html.replace("__BUILD__", stamp)
    return HTMLResponse(html, headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/", response_class=HTMLResponse)
def index(request: Request, desktop: int = 0):
    """手机 UA 自动进手机版；`/?desktop=1` 可强制电脑版（手机版页脚也有「切到电脑版」）"""
    ua = request.headers.get("user-agent", "") or ""
    if not desktop and _MOBILE_UA.search(ua):
        return RedirectResponse("/m", status_code=302)
    return _serve_html("index.html")


@app.get("/m", response_class=HTMLResponse)
def mobile_page():
    """手机版：卡片列表 + 底部导航 + 下拉刷新，专为小屏写的独立页面（不是桌面版缩窄）"""
    return _serve_html("m.html")


@app.get("/api/map")
def api_map(source: str = ""):
    """内置地图点位：有坐标的岗位（在线地图瓦片由前端 Leaflet 加载）。
    source 过滤平台；猎聘卡片不带经纬度，所以猎聘通常无点位 —— 用 no_coord 说明原因。"""
    src = (source or "").strip()
    pts = db.map_points(src)
    lngs = [p["lng"] for p in pts]
    lats = [p["lat"] for p in pts]
    return {"points": pts, "source": src,
            "approx": sum(1 for p in pts if p.get("coord_approx")),
            "no_coord": db.no_coord_stats().get(src, 0) if src else sum(db.no_coord_stats().values()),
            "center": [sum(lats) / len(lats), sum(lngs) / len(lngs)] if pts else [24.5, 118.1],
            "bounds": [min(lats), min(lngs), max(lats), max(lngs)] if pts else None}


@app.get("/api/log")
def api_log(run_id: int):
    runs = db.get_runs(30)
    for r in runs:
        if r["id"] == run_id:
            return {"log": r.get("log", ""), "status": r.get("status")}
    return {"log": "", "status": ""}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8765)
