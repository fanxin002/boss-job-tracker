# -*- coding: utf-8 -*-
"""BOSS直聘获取核心（DrissionPage，改进自 2026boss.py）
主方案：在已登录的列表页内用 fetch() 调 /wapi/zpgeek/search/joblist.json 接口翻页（薪资为明文 salaryDesc）
兜底方案：监听页面自身 joblist 请求 + 模拟人类滚动（原脚本思路）
去重键：encryptJobId（岗位ID跨天稳定）"""
import os
import random
import socket
import threading
import time

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(db.DATA_DIR, "chrome_profile")

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# 在列表页上下文内调接口：携带完整 cookie/wt2 会话，绕开 DOM 字体加密薪资
# 注意：DrissionPage run_js 不支持顶层 await，必须返回 Promise（awaitPromise 自动等待）
API_JS = """
var kw=arguments[0],city=arguments[1],p=arguments[2],deg=arguments[3]||'';
var url='https://www.zhipin.com/wapi/zpgeek/search/joblist.json?query='+encodeURIComponent(kw)
  +'&city='+city+'&page='+p+'&pageSize=30'+(deg?('&degree='+deg):'');
return fetch(url,{credentials:'include'}).then(function(r){return r.text();});
"""

# ---------- 节流参数（真人节奏） ----------
PAGE_DELAY = (8, 20)          # 页间隔（秒）：普通人翻页看列表就是这个节奏
COOLDOWN_EVERY = 3            # 每 N 页插入一次冷却
COOLDOWN_DELAY = (30, 55)     # 常规冷却时长（秒）
LONG_COOLDOWN_EVERY = 6       # 每 N 页插入一次长冷却
LONG_COOLDOWN = (70, 120)     # 长冷却时长（秒）
SOFT_RETRY_WAIT = (60, 110)   # code=37「环境异常」降温等待（秒）
SOFT_RETRY_MAX = 1            # 每轮最多降温重试次数
PAGE_CHUNK = 60               # 累计多少条就落库一次（分段入库，风控也不会丢前面数据）
DAILY_QUOTA = 150             # 每日接口请求预算（一次翻页 = 1 次）


def _get_free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def cleanup_stale_browsers():
    """杀掉所有使用本项目 profile 的残留 Chrome 进程（精确匹配命令行，不碰日常浏览器）"""
    try:
        import subprocess
        q = ("Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
             "Where-Object { $_.CommandLine -like '*boss-job-tracker*' } | "
             "ForEach-Object { $_.ProcessId }")
        out = subprocess.run(["powershell", "-NoProfile", "-Command", q],
                             capture_output=True, text=True, timeout=30).stdout
        for pid in out.split():
            if pid.strip().isdigit():
                subprocess.run(["taskkill", "/F", "/PID", pid.strip()],
                               capture_output=True, timeout=10)
        if out.strip():
            time.sleep(2)
    except Exception:
        pass


_browser = {"page": None, "headless": None}

# 指纹加固：抹掉自动化痕迹，补齐真人浏览器该有的属性
# 只做「还原成普通浏览器」的伪装，不伪造敏感字段；每个新 document 生效
_STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
if(!window.chrome) window.chrome={};
if(!window.chrome.runtime) window.chrome.runtime={};
Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh','en']});
Object.defineProperty(navigator,'plugins',{get:()=>[
  {name:'Chrome PDF Plugin',filename:'internal-pdf-viewer',description:'Portable Document Format'},
  {name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai',description:''},
  {name:'Native Client',filename:'internal-nacl-plugin',description:''}]});
Object.defineProperty(navigator,'mimeTypes',{get:()=>[{type:'application/pdf'},{type:'application/x-google-chrome-pdf'}]});
Object.defineProperty(navigator,'hardwareConcurrency',{get:()=>8});
Object.defineProperty(navigator,'deviceMemory',{get:()=>8});
if(navigator.permissions&&navigator.permissions.query){
  var _q=navigator.permissions.query.bind(navigator.permissions);
  navigator.permissions.query=function(p){
    if(p&&p.name==='notifications'){
      return Promise.resolve({state:Notification.permission,onchange:null});
    }
    return _q(p);
  };
}
try{
  var gp=WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter=function(p){
    if(p===37445) return 'Intel Inc.';
    if(p===37446) return 'Intel Iris OpenGL Engine';
    return gp.apply(this,arguments);
  };
}catch(e){}
"""


def _page_alive(page):
    try:
        _ = page.title
        return True
    except Exception:
        return False


def ensure_browser(headless=False, offscreen=False):
    """进程内单例：复用同一浏览器；模式变化则重建。
    三种模式：
      headless=True          真无头（最快，但 HeadlessChrome 指纹易被识别 → code=37）
      offscreen=True         离屏有头：真实浏览器窗口挪到屏幕外，用户看不见，指纹是真 Chrome（推荐）
      headless=offscreen=False  可见有头（登录/过验证用）
    注意：无头必须用 DrissionPage 官方 co.headless()，raw 参数 --headless=new 会导致 CDP 连接 404"""
    from DrissionPage import ChromiumPage, ChromiumOptions
    mode = "headless" if headless else ("offscreen" if offscreen else "headed")
    page = _browser["page"]
    if page is not None and _page_alive(page):
        if _browser["headless"] == mode:
            return page
        try:
            page.quit()
        except Exception:
            pass
        _browser["page"] = None
    exe = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
    os.makedirs(PROFILE_DIR, exist_ok=True)
    last_err = None
    for attempt in range(2):
        if attempt:
            cleanup_stale_browsers()
        co = ChromiumOptions()
        if exe:
            co.set_browser_path(exe)
        co.set_local_port(_get_free_port())
        co.set_argument(f"--user-data-dir={PROFILE_DIR}")
        co.set_argument("--disable-blink-features=AutomationControlled")
        co.set_argument("--no-first-run")
        co.set_argument("--no-default-browser-check")
        co.set_argument("--window-size=1280,900")
        co.set_argument("--lang=zh-CN")          # 语言指纹与中文环境一致
        co.set_argument("--accept-lang=zh-CN,zh")
        if mode == "headless":
            co.headless()  # 官方无头开关
            # 无头模式 UA 默认带 "HeadlessChrome" 特征，BOSS 风控直接识别（code=37 环境异常）
            # 用普通 Chrome UA 覆盖，配合下面的 JS 注入抹掉 webdriver 痕迹
            co.set_user_agent(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
        elif mode == "offscreen":
            # 真实有头浏览器，只是窗口放到屏幕外 → 不打扰用户，同时没有无头指纹
            co.set_argument("--window-position=-3200,-3200")
        try:
            page = ChromiumPage(addr_or_opts=co)
            # 三种模式统一做指纹加固：webdriver / chrome 对象 / plugins / languages / permissions
            # code=37「环境存在异常」主要就是这几项被识别出来的
            try:
                page.run_cdp("Page.addScriptToEvaluateOnNewDocument", source=_STEALTH_JS)
            except Exception:
                pass
            _browser["page"], _browser["headless"] = page, mode
            return page
        except Exception as e:
            last_err = e
    raise RuntimeError(f"浏览器启动失败：{last_err}")


def human_scroll(page, times=3):
    for _ in range(times):
        try:
            page.scroll.down(random.randint(100, 500))
            time.sleep(random.uniform(0.4, 1.4))
        except Exception:
            break


def is_logged_in(page):
    try:
        if "login" in (page.url or ""):
            return False
        body = page.ele("tag:body", timeout=3)
        text = (body.text or "") if body else ""
        return not ("手机号登录" in text and "二维码" in text and "搜索职位" not in text)
    except Exception:
        return True


LOGIN_URL = "https://www.zhipin.com/web/user/?ka=header-login"
COOKIE_FILE = os.path.join(db.DATA_DIR, "boss_cookies.json")
STATE_FILE = os.path.join(db.DATA_DIR, "login_state.json")
_login = {"running": False, "thread": None, "started": 0.0}


def _cookie_list(page):
    ck = page.cookies()
    if isinstance(ck, dict):
        return [{"name": k, "value": v} for k, v in ck.items()]
    return list(ck or [])


def cookies_logged(page) -> bool:
    """以 Cookie 为准判断登录态：BOSS 登录后下发 wt2/bsti"""
    try:
        names = set()
        for c in _cookie_list(page):
            if isinstance(c, dict):
                names.add(c.get("name"))
            else:
                names.add(getattr(c, "name", None))
        return ("wt2" in names) or ("bsti" in names)
    except Exception:
        return False


def set_login_state(ok, note=""):
    import json as _json
    from datetime import datetime as _dt
    _json.dump({"logged": bool(ok), "at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"), "note": note},
               open(STATE_FILE, "w", encoding="utf-8"))


def _login_open():
    """自愈：登录线程已退出/超时则复位，避免永久占位"""
    if _login["running"]:
        th = _login.get("thread")
        dead = th is None or not th.is_alive()
        if dead or (bool(_login.get("started")) and time.time() - _login["started"] > 660):
            _login["running"] = False
    return _login["running"]


def get_login_state():
    import json as _json
    try:
        st = _json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {"logged": False, "at": "", "note": ""}
    st["login_open"] = _login_open()
    return st


def save_cookies(page):
    import json as _json
    _json.dump(_cookie_list(page), open(COOKIE_FILE, "w", encoding="utf-8"))


def inject_cookies(page):
    """先落地域名再写 Cookie，写完重新导航一次：
    让 BOSS 自己的 JS 在带 Cookie 的会话里重新生成 __zp_stoken__ 等风控令牌，
    否则沿用旧令牌容易被判定 code=36/37"""
    import json as _json
    if not os.path.exists(COOKIE_FILE):
        return False
    try:
        page.get("https://www.zhipin.com/web/geek/jobs")
        time.sleep(1.2)
        page.set.cookies(_json.load(open(COOKIE_FILE, encoding="utf-8")))
        page.get("https://www.zhipin.com/web/geek/jobs")  # 带 Cookie 重新加载，刷新令牌
        time.sleep(1.2)
        return True
    except Exception:
        return False


def _close_browser():
    p = _browser["page"]
    _browser["page"] = _browser["headless"] = None
    if p:
        try:
            p.quit()
            return
        except Exception:
            pass
    cleanup_stale_browsers()


def login_flow():
    """独立线程：弹有头窗口→手动登录→自动保存Cookie→自动关窗"""
    global _login_cancel
    _login["thread"] = threading.current_thread()
    _login["started"] = time.time()
    _login["running"] = True
    _login_cancel = False
    try:
        if _browser["page"] is not None:
            _close_browser()  # 登录必须有头窗口，先安静地关掉现有实例
        page = ensure_browser(headless=False)
        page.get(LOGIN_URL)
        ok = False
        for _ in range(200):  # 最多等10分钟
            time.sleep(3)
            if _login_cancel:
                break
            if not _page_alive(page):  # 用户手动关窗 → 立即结束
                set_login_state(False, "登录窗口已被关闭")
                break
            try:
                if cookies_logged(page):
                    save_cookies(page)
                    set_login_state(True, "登录成功，Cookie已持久化")
                    ok = True
                    break
            except Exception:
                pass
        if not ok and not _login_cancel:
            set_login_state(False, "登录超时（10分钟），未检测到登录成功")
    except Exception as e:
        set_login_state(False, f"登录流程异常：{e}")
    finally:
        _login["running"] = False
        time.sleep(1)
        _close_browser()  # 登录窗口用完即关（quiet quit，不留僵尸）


_login_cancel = False


def cancel_login():
    global _login_cancel
    _login_cancel = True


_repair_cancel = False


def repair_flow(keyword="数据分析", city_code="101230200"):
    """修复登录态 / 过安全验证：
    打开可见窗口停在 BOSS 搜索页，让你手动处理滑块验证；
    每 15 秒用一次轻量接口探测，接口放行（code=0）即保存 Cookie 并关窗。
    探测本身失败不重试轰炸：单次请求，间隔足够长。
    """
    global _repair_cancel
    _repair["thread"] = threading.current_thread()
    _repair["started"] = time.time()
    _repair["last"] = ""
    _repair["running"] = True  # 先写线程再置 running，避免自愈误判
    _repair_cancel = False
    result = {"ok": False, "msg": ""}
    try:
        if _browser["page"] is not None:
            _close_browser()
        page = ensure_browser(headless=False)  # 可见窗口（要让你能拖滑块）
        page.get(f"https://www.zhipin.com/web/geek/jobs?query={keyword}&city={city_code}")
        time.sleep(3)
        for i in range(40):  # 最多 10 分钟
            if _repair_cancel:
                result["msg"] = "已取消修复"
                break
            if not _page_alive(page):  # 窗口被手动关闭 → 立即结束，不留悬挂状态
                result["msg"] = "修复窗口已被关闭，流程结束"
                break
            code, msg = probe_api(page, keyword, city_code)
            if code == 0:
                save_cookies(page)
                set_login_state(True, "修复成功：接口已放行，Cookie已刷新")
                clear_last_risk()
                result.update(ok=True, msg="接口已放行，Cookie 已刷新，可以重新获取")
                break
            if code in (36, 37, 38, 401):
                _record_risk(code, msg)
                result["msg"] = f"仍被拦（code={code} {msg}）"
            else:
                result["msg"] = f"探测异常 code={code} {msg}"
            time.sleep(15)
        else:
            result["msg"] = "10分钟内接口仍未放行（建议等待更久，别连续点）"
        if not result["ok"]:
            set_login_state(bool(cookies_logged(page)),
                            f"修复未完成：{result['msg']}")
    except Exception as e:
        result["msg"] = f"修复流程异常：{e}"
    finally:
        _repair["running"] = False
        time.sleep(0.5)
        _close_browser()
        if result["ok"]:
            db.clear_risk()
    return result


_repair = {"running": False, "last": "", "thread": None, "started": 0.0}


def repair_state():
    """自愈：线程已退出或超时（>11分钟）则自动复位，避免界面永久卡在「进行中」"""
    if _repair["running"]:
        th = _repair.get("thread")
        timed_out = bool(_repair.get("started")) and (time.time() - _repair["started"] > 660)
        dead = th is None or not th.is_alive()
        if timed_out or dead:
            _repair["running"] = False
            if not _repair["last"]:
                _repair["last"] = "修复流程已结束（窗口被关闭或超时）"
    return {"running": _repair["running"], "last": _repair["last"]}


def cancel_repair():
    global _repair_cancel
    _repair_cancel = True


def probe_api(page, keyword, city_code):
    """单次轻量探测：返回 (code, message)。不抛异常、不重试"""
    try:
        txt = page.run_js(API_JS, keyword, city_code, 1)
        body = _parse_body(txt) or {}
        return body.get("code"), (body.get("message") or "")
    except Exception as e:
        return -1, str(e)


def wait_login(page, logf, max_wait=300):
    """有头模式兜底：提示在窗口登录并等待，成功后存cookie"""
    waited = 0
    while not cookies_logged(page):
        if waited == 0:
            logf("⚠️ 浏览器窗口未登录，请手动登录（仅首次需要）…")
        time.sleep(5)
        waited += 5
        if waited >= max_wait:
            raise RuntimeError("LOGIN_TIMEOUT：5分钟内未完成登录")
    save_cookies(page)
    set_login_state(True, "获取中手动登录成功")
    logf("✅ 检测到登录成功")


def _parse_body(resp_text):
    import json as _json
    try:
        return _json.loads(resp_text)
    except Exception:
        return None


class StopCrawl(RuntimeError):
    """账号风控/登录失效：必须立即停止整轮，继续请求只会加重风控"""


# 最近一次命中风控的记录，供 app 层落「冷却锁」
LAST_RISK = {"code": None, "msg": "", "at": None}


def _record_risk(code, msg=""):
    from datetime import datetime as _dt
    LAST_RISK.update(code=code, msg=str(msg or ""), at=_dt.now().strftime("%Y-%m-%d %H:%M:%S"))


def clear_last_risk():
    LAST_RISK.update(code=None, msg="", at=None)


def fetch_via_api(page, kw, city_code, pages, logf, degree="", collector=None, on_page=None):
    """主方案：页面内 fetch joblist.json（degree 服务端过滤，如 202,203）

    关键改动（防风控 + 防丢数据）：
    1. collector：调用方传入的列表，边抓边存；中途被风控也不会丢已抓数据
    2. on_page：每页抓完立即回调（用于分段入库）
    3. 页间隔 8~20 秒，每 3 页长冷却 25~50 秒，并模拟滚动
    4. code=37「环境异常」先降温 60~110 秒、重进列表页重签令牌再试一次
    5. 每日请求预算（DAILY_QUOTA），用完即停，避免无意义地喂风控
    """
    raw = collector if collector is not None else []
    seen = {j.get("encryptJobId") for j in raw if j.get("encryptJobId")}
    list_url = f"https://www.zhipin.com/web/geek/jobs?query={kw}&city={city_code}"
    if degree:
        list_url += f"&degree={degree}"
    soft_retry = 0
    p = 1
    while p <= pages:
        used, limit, allowed = db.bump_quota(DAILY_QUOTA)
        if not allowed:
            raise StopCrawl(f"今日请求预算 {limit} 次已用完（已用 {used}），明天再跑；"
                            f"已抓到的数据已入库，不会丢")
        try:
            txt = page.run_js(API_JS, kw, city_code, p, degree or "")
        except Exception as e:
            logf(f"  API 第{p}页调用异常：{e}，转兜底方案")
            return None
        body = _parse_body(txt)
        if not body:
            logf(f"  第{p}页接口返回解析失败，转兜底方案")
            return None
        code = body.get("code")
        msg = body.get("message") or ""
        if code not in (0, None):
            if code in (36, 37, 38, 401):
                if code == 37 and soft_retry < SOFT_RETRY_MAX:
                    soft_retry += 1
                    wait = random.uniform(*SOFT_RETRY_WAIT)
                    logf(f"  ⚠️ 第{p}页 code=37（{msg}）：多为翻页太快触发环境校验。"
                         f"已抓 {len(raw)} 条不受影响，降温 {int(wait)} 秒后重进列表页再试一次")
                    time.sleep(wait)
                    try:
                        page.get(list_url)
                        time.sleep(random.uniform(4, 8))
                        human_scroll(page, random.randint(2, 4))
                    except Exception:
                        pass
                    continue  # 重试同一页
                _record_risk(code, msg)
                raise StopCrawl(f"BOSS风控/登录失效 code={code}（{msg}）")
            logf(f"  第{p}页接口 code={code}（{msg}），转兜底方案")
            return None
        lst = (body.get("zpData") or {}).get("jobList") or []
        added = 0
        for j in lst:
            jid = j.get("encryptJobId") or ""
            if not jid or jid in seen:
                continue
            seen.add(jid)
            j["_kw"] = kw
            j["_city"] = city_code
            raw.append(j)
            added += 1
        logf(f"  [{kw}·{city_code}] API 第{p}页 +{added}（累计 {len(raw)}）")
        if on_page:
            try:
                on_page(raw, p)
            except Exception as e:
                logf(f"  ⚠️ 分批入库异常：{e}")
        if not lst or not (body.get("zpData") or {}).get("hasMore", added > 0):
            break
        p += 1
        if p > pages:
            break
        try:
            if p % LONG_COOLDOWN_EVERY == 1 and p > 1:          # 第 7/13… 页前：长停
                cd = random.uniform(*LONG_COOLDOWN)
                logf(f"  ⏸ 已抓 {p-1} 页，长冷却 {int(cd)} 秒（分段执行，降低环境校验概率）")
                time.sleep(cd)
            elif p % COOLDOWN_EVERY == 1 and p > 1:   # 第 4/10… 页前
                cd = random.uniform(*COOLDOWN_DELAY)
                logf(f"  ⏸ 已连续抓 {p-1} 页，冷却 {int(cd)} 秒（模拟真人翻页节奏）")
                time.sleep(cd)
            human_scroll(page, random.randint(1, 3))
        except Exception:
            pass
        time.sleep(random.uniform(*PAGE_DELAY))
    return raw


def fetch_via_listen(page, kw, city_code, pages, logf, degree=""):
    """兜底方案：加载列表页 + 监听 joblist + 滚动翻页（原 2026boss.py 思路）"""
    raw, seen = [], set()
    url = f"https://www.zhipin.com/web/geek/jobs?query={kw}&city={city_code}"
    if degree:
        url += f"&degree={degree}"
    try:
        page.listen.start("joblist")
        page.get(url)
        time.sleep(random.uniform(2.5, 4.5))
        for p in range(1, pages + 1):
            for attempt in range(2):
                try:
                    resp = page.listen.wait(timeout=12)
                    if resp is False:
                        break
                    body = resp.response.body
                    if isinstance(body, (bytes, bytearray)):
                        body = _parse_body(body.decode("utf-8", "ignore"))
                    if isinstance(body, dict) and body.get("code") in (36, 37, 38, 401):
                        _record_risk(body.get("code"), body.get("message"))
                        raise StopCrawl(f"BOSS风控/登录失效 code={body.get('code')}（{body.get('message') or ''}）")
                    lst = ((body or {}).get("zpData") or {}).get("jobList") or []
                    added = 0
                    for j in lst:
                        jid = j.get("encryptJobId") or ""
                        if not jid or jid in seen:
                            continue
                        seen.add(jid)
                        j["_kw"] = kw
                        j["_city"] = city_code
                        raw.append(j)
                        added += 1
                    logf(f"  [{kw}·{city_code}] 监听 第{p}页 +{added}（累计 {len(raw)}）")
                    if added:
                        break
                except StopCrawl:
                    raise
                except Exception:
                    time.sleep(2)
            human_scroll(page, random.randint(3, 5))
            time.sleep(random.uniform(1.5, 3.5))
    finally:
        try:
            page.listen.stop()
        except Exception:
            pass
    return raw


def normalize(raw):
    out = []
    for j in raw:
        jid = j.get("encryptJobId") or ""
        if not jid:
            continue
        gps = j.get("gps") or {}
        out.append({
            "job_id": jid,
            "lid": j.get("lid", ""),
            "security_id": j.get("securityId", ""),
            "keyword": j.get("_kw", ""),
            "city_code": j.get("_city", ""),
            "city_name": j.get("cityName", ""),
            "job_name": j.get("jobName", ""),
            # 薪资双取：salaryDesc 为明文；部分场景（游客/风控降级）返回空但 salary 字段仍有值
            "salary": j.get("salaryDesc") or j.get("salary") or "",
            "company": j.get("brandName", ""),
            "scale": j.get("brandScaleName", ""),
            "industry": j.get("brandIndustry", ""),
            "degree": j.get("jobDegree", ""),
            "experience": j.get("jobExperience", ""),
            "skills": ",".join(j.get("skills") or []),
            "welfare": ",".join(j.get("welfareList") or []),
            "boss_name": j.get("bossDisplayName", "") or j.get("bossName", ""),
            "area_district": j.get("areaDistrict", ""),
            "business_district": j.get("businessDistrict", ""),
            "lng": gps.get("longitude", ""),
            "lat": gps.get("latitude", ""),
            # ---- 列表接口其余可用字段（2026-10-21 探测确认）----
            "boss_title": j.get("bossTitle", ""),
            "brand_stage": j.get("brandStageName", ""),
            "job_labels": ",".join(j.get("jobLabels") or []),
            "days_per_week": j.get("daysPerWeekDesc", ""),
            "least_month": j.get("leastMonthDesc", ""),
            "proxy_job": bool(j.get("proxyJob")),
            "gold_hunter": bool(j.get("goldHunter")),
            "encrypt_brand_id": j.get("encryptBrandId", ""),
            "boss_avatar": j.get("bossAvatar", ""),
            "job_url": f"https://www.zhipin.com/job_detail/{jid}.html?securityId={j.get('securityId','')}",
            "detail_text": "",
        })
    return out


def fetch_details(page, rows, logf, limit=40):
    """逐个打开详情页取职位描述/JD关键词"""
    todo = [r for r in rows if not r["detail_text"]][:limit]
    if not todo:
        return
    try:
        tab = page.latest_tab.new_tab() if hasattr(page, "latest_tab") else page.new_tab()
    except Exception:
        tab = page
    ok = 0
    for r in todo:
        try:
            tab.get(r["job_url"])
            time.sleep(random.uniform(1.5, 3))
            ele = tab.ele("x://div[@class='job-sec-text']", timeout=3)
            if not ele:
                ele = tab.ele("x://div[@class='detail-content-text']", timeout=2)
            if ele:
                r["detail_text"] = (ele.text or "").strip()[:3000]
                ok += 1
            time.sleep(random.uniform(0.8, 2.2))
        except Exception:
            continue
    logf(f"  职位描述：{ok}/{len(todo)} 条获取成功")
    try:
        if tab is not page:
            tab.close()
    except Exception:
        pass


# 学历筛选码（BOSS joblist.json 的 degree 参数，逗号可多选）
DEGREE_LABELS = {"202": "大专", "203": "本科", "204": "硕士", "205": "博士",
                 "206": "高中", "208": "中专/中技", "209": "初中及以下"}

DETAIL_SELECTORS = (
    "x://div[@class='job-sec-text']",
    "x://div[contains(@class,'job-sec-text')]",
    "x://div[contains(@class,'job-detail-section')]//div[contains(@class,'text')]",
)


def extract_detail(tab):
    """从详情页提取岗位描述正文；失败返回空串"""
    for sel in DETAIL_SELECTORS:
        try:
            ele = tab.ele(sel, timeout=2)
            if ele:
                txt = (ele.text or "").strip()
                if len(txt) >= 10:
                    return txt[:3000]
        except Exception:
            continue
    return ""


def _detail_blocked(tab):
    try:
        body = tab.ele("tag:body", timeout=1.5)
        text = (body.text or "")[:800] if body else ""
        if any(k in text for k in ("安全验证", "滑块", "请完成验证", "访问异常", "账户存在异常")):
            return True
        return False
    except Exception:
        return False


def backfill_details(jobs, logf, interval=(3, 6), should_cancel=None, on_progress=None):
    """批量补抓职位描述（限速分批，防风控）。返回 (ok, fail)"""
    if not jobs:
        return 0, 0
    page = ensure_browser(offscreen=True)
    inject_cookies(page)
    ok = fail = 0
    consec_empty = 0
    tab = None
    try:
        total = len(jobs)
        for i, j in enumerate(jobs, 1):
            if should_cancel and should_cancel():
                logf("⏹ 已取消，剩余岗位未抓")
                break
            try:
                if tab is None or not _page_alive(tab):
                    tab = page.new_tab()
                tab.get(j["job_url"])
                time.sleep(random.uniform(2.0, 3.5))
                if _detail_blocked(tab):
                    _record_risk("verify", "补抓描述时触发验证")
                    logf("⛔ 触发安全验证/风控，立即停止补抓")
                    break
                txt = extract_detail(tab)
                if txt:
                    db.set_detail(j["job_id"], txt)
                    ok += 1
                    consec_empty = 0
                    logf(f"  [{i}/{total}] ✅ {j['job_name']}（{len(txt)}字）")
                else:
                    fail += 1
                    consec_empty += 1
                    logf(f"  [{i}/{total}] ⚠️ {j['job_name']}：未取到描述（可能已下架）")
                    if consec_empty >= 3:
                        logf("⛔ 连续3条取不到描述，停止补抓（疑似被限流）")
                        break
            except Exception as e:
                fail += 1
                logf(f"  [{i}/{total}] ❌ 异常：{e}")
            if on_progress:
                try:
                    on_progress(i, total)
                except Exception:
                    pass
            if i < total:
                time.sleep(random.uniform(*interval))
    finally:
        _close_browser()
        logf(f"补抓结束：成功 {ok} ｜ 失败 {fail}")
    return ok, fail


CITY_FILE = os.path.join(db.DATA_DIR, "cities.json")

CITY_JS = """
return fetch('https://www.zhipin.com/wapi/zpCommon/data/city.json',{credentials:'include'})
  .then(function(r){return r.text();});
"""


def sync_cities():
    """从 BOSS 拉全量城市树并落盘 data/cities.json。返回 (ok, 城市数/错误信息)"""
    import json as _json
    page = None
    try:
        page = ensure_browser(offscreen=True)
        inject_cookies(page)
        txt = page.run_js(CITY_JS)
        body = _parse_body(txt) or {}
        if body.get("code") not in (0, None):
            return False, f"接口返回 code={body.get('code')} {body.get('message') or ''}"
        data = body.get("zpData") or {}
        hot_codes = {str(c.get("code")) for c in (data.get("hotCityList") or [])}
        cities = []
        for prov in (data.get("cityList") or []):
            pname = prov.get("name") or ""
            for c in (prov.get("subLevelModelList") or []):
                code = str(c.get("code") or "")
                name = c.get("name") or ""
                if not code or not name:
                    continue
                # 只取「城市」层级（区县层级 cityType=4 跳过）
                if int(c.get("cityType") or 0) == 4:
                    continue
                cities.append({
                    "name": name, "code": code, "province": pname,
                    "first_char": (c.get("firstChar") or "").upper(),
                    "hot": code in hot_codes,
                })
        # 去重（同名同码）
        seen, uniq = set(), []
        for c in cities:
            if c["code"] in seen:
                continue
            seen.add(c["code"])
            uniq.append(c)
        uniq.sort(key=lambda x: (not x["hot"], x["first_char"] or "Z", x["name"]))
        _json.dump({"synced_at": time.strftime("%Y-%m-%d %H:%M:%S"), "count": len(uniq),
                    "cities": uniq}, open(CITY_FILE, "w", encoding="utf-8"),
                   ensure_ascii=False)
        return True, len(uniq)
    except Exception as e:
        return False, str(e)
    finally:
        if page is not None:
            _close_browser()


def load_cities():
    """读本地城市缓存；没有则返回空列表"""
    import json as _json
    try:
        data = _json.load(open(CITY_FILE, encoding="utf-8"))
        return data.get("cities") or [], data.get("synced_at") or ""
    except Exception:
        return [], ""


def sync_replies():
    """只读同步 BOSS 消息列表 DOM，把「已出现在会话里」的岗位标为已回复。
    不发送任何消息，只解析会话列表文本并按公司名匹配。"""
    import json as _json
    page = None
    parsed, matched = 0, 0
    try:
        page = ensure_browser(offscreen=True)
        inject_cookies(page)
        page.get("https://www.zhipin.com/web/geek/chat")
        time.sleep(7)
        js = """
        var out=[];
        document.querySelectorAll('.user-list li').forEach(function(li){
          var t=(li.innerText||'').trim();
          if(t) out.push(t.replace(/\\n/g,' | '));
        });
        return JSON.stringify(out);
        """
        raw = page.run_js(js) or "[]"
        convs = _json.loads(raw)
        parsed = len(convs)
        if not parsed:
            return {"parsed": 0, "matched": 0,
                    "msg": "没读到会话列表（可能未登录，或 BOSS 聊天页改版）"}
        _total, jobs = db.query_jobs({"status": "active", "size": 200})
        for text in convs:
            for j in jobs:
                comp = str(j.get("company") or "").strip()
                if comp and comp in text:
                    db.mark_replied(j["job_id"], text[:200])
                    matched += 1
                    break
        msg = f"读取 {parsed} 个会话，匹配到 {matched} 个在招岗位"
        if parsed and not matched:
            msg += "（会话里的公司名与岗位库对不上，属正常）"
        return {"parsed": parsed, "matched": matched, "msg": msg}
    except Exception as e:
        return {"parsed": parsed, "matched": matched, "msg": f"同步失败：{e}"}
    finally:
        if page is not None:
            _close_browser()


def fetch_resume_text():
    """只读获取 BOSS 本人简历页正文（/web/geek/resume）。不发任何写请求。
    返回 (text, msg)"""
    page = None
    try:
        page = ensure_browser(offscreen=True)
        inject_cookies(page)
        page.get("https://www.zhipin.com/web/geek/resume")
        time.sleep(6)
        if "404" in (page.url or ""):
            return "", "简历页地址失效（BOSS 可能改版）"
        js = """
        var el=document.querySelector('.resume-body,.resume-content,.resume-box,main')||document.body;
        return (el.innerText||'');
        """
        text = page.run_js(js) or ""
        if len(text) < 200:
            text = page.run_js("return document.body.innerText") or ""
        if "登录" in text[:200] and "简历" not in text:
            return "", "读取到登录页，登录态可能已失效，请先点「修复登录态」"
        if len(text) < 120:
            return "", f"简历页正文过短（{len(text)} 字），可能未创建在线简历"
        return text, f"读取简历正文 {len(text)} 字"
    except Exception as e:
        return "", f"读取简历失败：{e}"
    finally:
        if page is not None:
            _close_browser()


def run_crawl(run_id, keywords, cities, pages, want_detail, headless=False, offscreen=False, degree=""):
    """整轮获取入口（后台线程调用）。返回 (fetched, new_cnt, offline_cnt)
    全程静默：浏览器进程用完即关，不留窗口、不留僵尸"""
    logf = lambda s: db.append_run_log(run_id, s)  # noqa: E731
    covered, all_rows, run_ids = [], [], []
    try:
        return _crawl_body(run_id, keywords, cities, pages, want_detail, headless, offscreen,
                           logf, covered, all_rows, run_ids, degree)
    finally:
        _close_browser()
        logf("浏览器已静默关闭（登录态保存在 Cookie，下次获取自动复用）")


def _crawl_body(run_id, keywords, cities, pages, want_detail, headless, offscreen, logf,
                covered, all_rows, run_ids, degree=""):
    page = ensure_browser(headless, offscreen)
    inject_cookies(page)
    mode_txt = "无头静默" if headless else ("离屏有头" if offscreen else "有头")
    logf(f"浏览器就绪（{mode_txt}模式，窗口不会打扰你）")

    stopped = None  # 风控原因：中断后已抓到的数据仍照常入库
    pending = []    # 待落库（normalize 后）
    state = {"new": 0, "processed": 0}

    def _flush(force=False):
        """分段入库：每 PAGE_CHUNK 条落一次，风控中断也不会丢已抓数据"""
        if not pending:
            return
        if not force and len(pending) < PAGE_CHUNK:
            return
        batch = pending[:]
        pending.clear()
        n, _ = db.upsert_jobs(batch, run_id)
        state["new"] += n
        logf(f"  💾 已落库 {len(batch)} 条（本轮累计新增 {state['new']}）")

    def _absorb(raw_all):
        """把 raw_all 里尚未处理的新增部分转成行，进 pending"""
        chunk = raw_all[state["processed"]:]
        state["processed"] = len(raw_all)
        rows = normalize(chunk)
        if not rows:
            return
        pending.extend(rows)
        all_rows.extend(rows)
        run_ids.extend(r["job_id"] for r in rows)
        _flush()

    for kw in keywords:
        if stopped:
            break
        for city in cities:
            cc = city["code"]
            deg_txt = f"，学历筛选 {DEGREE_LABELS.get(degree, degree)}" if degree else ""
            logf(f"▶ 获取：「{kw}」 @ {city['name']}({cc})，目标 {pages} 页{deg_txt}")
            # 先开列表页建立会话，然后优先 API
            list_url = f"https://www.zhipin.com/web/geek/jobs?query={kw}&city={cc}"
            if degree:
                list_url += f"&degree={degree}"
            page.get(list_url)
            time.sleep(random.uniform(3, 6))
            if not cookies_logged(page):
                if headless or offscreen:
                    raise RuntimeError("未登录：请点页面右上角「修复登录态」，在弹出的窗口完成登录/验证（成功后自动保存Cookie），然后重新获取")
                wait_login(page, logf)
            collected = []   # 边抓边存：即使中途抛 StopCrawl 也不丢
            try:
                raw = fetch_via_api(page, kw, cc, pages, logf, degree,
                                    collector=collected, on_page=lambda allr, pno: _absorb(allr))
                if raw is None:
                    logf("  切换到滚动监听兜底方案…")
                    raw = fetch_via_listen(page, kw, cc, pages, logf, degree)
                    _absorb(raw or [])
            except StopCrawl as e:
                stopped = str(e)
                _absorb(collected)          # 关键：把中断前抓到的补进来
                _flush(force=True)
                logf(f"⛔ {stopped}")
                logf(f"   已停止后续获取（继续请求会加重风控）；"
                     f"中断前抓到的 {len(all_rows)} 条已全部落库，不会丢")
                break
            covered.append((kw, cc))
            no_sal = sum(1 for r in all_rows[-90:] if not r["salary"])
            if all_rows:
                logf(f"  本页组采集完成" + (f"（最近 90 条中 {no_sal} 条无薪资）" if no_sal else "（薪资全部有值）"))
            time.sleep(random.uniform(6, 14))

    _flush(force=True)

    if not all_rows:
        logf("❌ 本轮未抓到任何数据" + (f"：{stopped}" if stopped else "（登录态失效或被风控）"))
        return 0, 0, 0

    offline_cnt = db.mark_offline(covered, set(run_ids))
    with_sal = sum(1 for r in all_rows if r["salary"])
    logf(f"✅ 入库完成：共 {len(all_rows)} 条 ｜ 新增 {state['new']} ｜ 标记下线 {offline_cnt} ｜ "
         f"薪资有值 {with_sal}/{len(all_rows)}")
    if stopped:
        logf(f"⚠️ 本轮因风控提前结束：{stopped}")
    return len(all_rows), state["new"], offline_cnt
