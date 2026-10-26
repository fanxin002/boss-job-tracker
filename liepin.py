# -*- coding: utf-8 -*-
"""猎聘获取核心（DrissionPage）

主方案：先导航到 liepin.com 首页让 security.min.js 过 WAF 拿 acw_tc 通行证，
再在页面上下文里 fetch() POST api-c.liepin.com/pc-search-job（同源带 cookie）。
这与 BOSS 的"列表页内 fetch 接口"同一套路——比 DOM 抓取字段更干净、城市精确。
（SEO 页 /city-xx/zhaopin/ 是推荐流，keyWord/city 参数实测不生效，DOM 抓取已弃用为主路。）

去重键：job.jobId（URL 数字 ID），入库统一加 lp 前缀防与 BOSS encryptJobId 撞车。
城市码：dqCode（厦门 090040 / 泉州 090030 / 福州 090020 已实测；京沪穗等为公开验证码）。
已实测的坑：currentPage 从 0 起；hasNext 第 0 页就 false 不可用，改判 0 条即停；
totalCounts 虚报不可信。
登录判据：cookie 含 lt_auth（免登录也能搜，但登录后结果更全）。
profile：data/chrome_profile_liepin（与 BOSS 完全隔离）。
"""
import json
import os
import re
import threading
import time

import crawler
import db

SOURCE = "liepin"
PROFILE_DIR = os.path.join(db.DATA_DIR, "chrome_profile_liepin")
STATE_FILE = os.path.join(db.DATA_DIR, "liepin_login_state.json")
COOKIE_FILE = os.path.join(db.DATA_DIR, "liepin_cookies.json")
LOGIN_URL = "https://www.liepin.com/user/login/"
HOME_URL = "https://www.liepin.com/"

PAGE_DELAY = (5, 11)          # 接口调用间隔（秒）
DAILY_QUOTA = 60              # 每日搜索请求预算（一次翻页 = 1 次）
MAX_PAGES_CAP = 10            # 猎聘接口硬上限 400 条/组合

# 兜底城市码（同步失败/首次使用前的本地内置表）。完整城市码由 sync_cities()
# 从猎聘 get-all-dq 接口拉取落盘 data/liepin_cities.json，与 BOSS 的 cities.json 对等。
_CITY_FALLBACK = {
    "厦门": "090040", "泉州": "090030", "福州": "090020",   # 探针实测 dqCode
    "北京": "010", "上海": "020", "天津": "030",
    "广州": "050020", "深圳": "050090", "南京": "060020", "杭州": "070020",
    "合肥": "080020", "成都": "280020",
}
CITY_FILE = os.path.join(db.DATA_DIR, "liepin_cities.json")


def _load_city_code():
    """城市码表：优先读本地缓存（同步过就用它），否则退回内置兜底表。"""
    try:
        with open(CITY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        m = data.get("map") or {}
        if m:
            return m
    except Exception:
        pass
    return dict(_CITY_FALLBACK)


# 模块级城市码表（每次调用 run_crawl 前由 _load_city_code 刷新，见 run_crawl 开头）
CITY_CODE = _load_city_code()

# 页面上下文里调搜索接口（POST + JSON body，同源自动携带 WAF cookie）。
# 必需请求头（实测缺失时 flag!=1 被拦）：X-Client-Type / X-Fscp-Version /
# X-Fscp-Std-Info(client_id=40108) / X-Fscp-Trace-Id / X-Requested-With
SEARCH_JS = r"""
var body=arguments[0];
return fetch('https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job',{
  method:'POST',credentials:'include',
  headers:{'Content-Type':'application/json',
    'X-Requested-With':'XMLHttpRequest',
    'X-Client-Type':'web',
    'X-Fscp-Version':'1.1',
    'X-Fscp-Std-Info':JSON.stringify({client_id:'40108'}),
    'X-Fscp-Trace-Id':'w'+Date.now()+Math.floor(Math.random()*1e6)},
  body:body}).then(function(r){return r.text();})
  .catch(function(e){return 'FETCHERR '+e;});
"""


def build_body(kw, dq, page_no):
    """请求体：currentPage 从 0 起（外部页码 1 起 → 减 1）"""
    return {"data": {
        "mainSearchPcConditionForm": {
            "city": dq, "dq": dq, "pubTime": "",
            "currentPage": page_no - 1, "pageSize": 40, "key": kw,
            "suggestTag": "", "workYearCode": "0",
            "compId": "", "compName": "", "compTag": "",
            "industry": "", "salary": "", "jobKind": "",
            "compScale": "", "compKind": "", "compStage": "", "eduLevel": ""},
        "passThroughForm": {"scene": "init", "skeyword": kw, "sfrom": "search_job_pc"}}}


# 猎聘全量城市码接口（antd-city 组件同款）：part_1 = 地级市/直辖市（搜索用），
# part_2 = 区县，province = 省级，country = 国外。与 BOSS 的 city.json 对等。
GET_DQ_URL = ("https://api-dok.liepin.com/api/com.liepin.bd.p.v4.get-all-dq"
              "?part=part_1,part_2,country,province&sc=0&from=component")
GET_DQ_JS = r"""
var url=arguments[0];
return fetch(url,{method:'GET',credentials:'include',
  headers:{'X-Requested-With':'XMLHttpRequest','X-Client-Type':'web',
    'X-Fscp-Version':'1.1','X-Fscp-Std-Info':JSON.stringify({client_id:'40108'}),
    'X-Fscp-Trace-Id':'w'+Date.now()+Math.floor(Math.random()*1e6),
    'Referer':'https://www.liepin.com/zhaopin/'}
}).then(function(r){return r.text();}).catch(function(e){return 'FETCHERR '+e;});
"""


def sync_cities():
    """从猎聘 get-all-dq 拉全量城市码落盘 data/liepin_cities.json。
    返回 (ok, 城市数/错误信息)。与 BOSS crawler.sync_cities() 对等。"""
    page = None
    try:
        page = _ensure_liepin_browser(offscreen=True)
        page.get("https://www.liepin.com/zhaopin/")
        time.sleep(6)
        txt = page.run_js(GET_DQ_JS, GET_DQ_URL)
        if not txt or str(txt).startswith("FETCHERR"):
            return False, f"接口请求失败：{(txt or '')[:80]}"
        body = json.loads(txt)
        if body.get("flag") != 1:
            return False, f"接口返回 flag={body.get('flag')} code={body.get('code')} {body.get('msg') or ''}"
        data = body.get("data") or {}
        part1 = (data.get("part_1") or {}).get("list") or {}
        # 只取中国城市：a=="0"（中国）。层级过滤：
        #   ca==3 → 地级市/自治州/地区（搜索城市码，厦门/泉州/福州…）
        #   ca==2 且 l==3 → 直辖市（北京/上海/天津/重庆）
        #   ca==2 其余（l==4/5 省级）→ 排除，省名不是搜索城市码
        city_map = {}
        for code, obj in part1.items():
            if str(obj.get("a")) != "0":
                continue
            name = (obj.get("n") or "").strip()
            if not name or not code:
                continue
            ca = int(obj.get("ca") or 0)
            lv = obj.get("l")
            if ca == 3:
                city_map[name] = code
            elif ca == 2 and str(lv) == "3":
                city_map[name] = code
        payload = {
            "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "count": len(city_map),
            "map": city_map,
        }
        with open(CITY_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        global CITY_CODE
        CITY_CODE = city_map
        return True, len(city_map)
    except Exception as e:
        return False, str(e)
    finally:
        if page is not None:
            try:
                crawler._close_browser(slot="liepin")
            except Exception:
                pass


def load_cities():
    """读本地城市码缓存；没有则返回内置兜底表"""
    try:
        with open(CITY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("map") or {}, data.get("synced_at") or ""
    except Exception:
        return dict(_CITY_FALLBACK), ""


def cookies_logged(page) -> bool:
    """猎聘登录判据：cookie 含 lt_auth"""
    try:
        names = {c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
                 for c in crawler._cookie_list(page)}
        return "lt_auth" in names
    except Exception:
        return False


def save_cookies(page):
    """登录成功后把 cookie 落盘（合并写，同 BOSS 的教训：别用游客态覆盖掉登录凭据）。

    为什么需要这份文件：光靠状态文件说「已登录」是假象 —— 猎聘会话过期后状态文件不会自己变，
    界面就一直显示已登录，点岗位却被跳到登录/中转页（2026-10-26 实测：profile 里 16 个 cookie
    但没有 lt_auth，状态文件却说「手动登录」）。有 cookie 文件才能像 BOSS 那样交叉校验。
    """
    import json as _json
    import tempfile
    try:
        new = crawler._cookie_list(page)
    except Exception:
        return False
    old = []
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            old = _json.load(f) or []
    except Exception:
        old = []

    def _key(c):
        name = c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
        dom = c.get("domain") if isinstance(c, dict) else ""
        return (str(name), str(dom))

    merged = {_key(c): c for c in old}
    for c in new:
        merged[_key(c)] = c
    # 登录凭据保护：新列表没有 lt_auth 时保留旧的（游客态保存不能擦掉登录态）
    if not any(_key(c)[0] == "lt_auth" for c in new):
        keep = [c for c in old if _key(c)[0] == "lt_auth"]
        for c in keep:
            merged.setdefault(_key(c), c)
    try:
        fd, tmp = tempfile.mkstemp(dir=db.DATA_DIR, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(list(merged.values()), f, ensure_ascii=False)
        os.replace(tmp, COOKIE_FILE)
        return True
    except Exception:
        return False


def cookies_have_login() -> bool:
    """从落盘文件判断登录凭据是否存在（不启浏览器，接口调用成本为零）"""
    import json as _json
    try:
        with open(COOKIE_FILE, encoding="utf-8") as f:
            data = _json.load(f) or []
        return any((c.get("name") if isinstance(c, dict) else None) == "lt_auth" for c in data)
    except Exception:
        return False


def set_login_state(ok, note=""):
    """原子写（同 crawler.set_login_state：避免进程被杀时留下空文件误显示未登录）"""
    import json as _json
    import tempfile
    from datetime import datetime
    payload = {"logged": bool(ok), "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "note": note}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(STATE_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            _json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def get_login_state():
    import json as _json
    try:
        st = _json.load(open(STATE_FILE, encoding="utf-8"))
        if not isinstance(st, dict):
            raise ValueError("not a dict")
    except Exception:
        st = {"logged": False, "at": "", "note": "状态文件损坏，点「猎聘」胶囊重新登录", "stale": True}
    # 交叉校验（同 BOSS 的 wt2 教训）：状态文件说已登录，但落盘 cookie 里没有 lt_auth
    # → 猎聘会话早就过期了，如实报未登录，别让界面继续显示「已登录」骗人。
    if st.get("logged") and os.path.exists(COOKIE_FILE) and not cookies_have_login():
        st["logged"] = False
        st["stale"] = True
        st["note"] = "状态文件记录已登录，但 cookie 里没有 lt_auth（会话已过期），请重新登录猎聘"
    th = _login.get("thread")
    st["login_open"] = bool(_login["running"] and th and th.is_alive())
    return st


_login = {"running": False, "thread": None, "started": 0.0}
_login_cancel = False


def _ensure_liepin_browser(headless=False, offscreen=False):
    return crawler.ensure_browser(headless=headless, offscreen=offscreen,
                                  profile=PROFILE_DIR, slot="liepin")


def login_flow():
    """独立线程：弹有头窗口→手动登录猎聘→登录态落 profile→自动关窗"""
    global _login_cancel
    _login["thread"] = threading.current_thread()
    _login["started"] = time.time()
    _login["running"] = True
    _login_cancel = False
    try:
        page = _ensure_liepin_browser(headless=False)
        page.get(LOGIN_URL)
        ok = False
        for _ in range(100):
            if _login_cancel:
                break
            time.sleep(3)
            try:
                if cookies_logged(page):
                    ok = True
                    break
            except Exception:
                pass
        set_login_state(ok, "手动登录" if ok else "未完成登录")
        if ok:
            save_cookies(page)      # 落盘才有得校验，否则状态文件永远是「一言堂」
    except Exception as e:
        set_login_state(False, f"登录窗口异常：{e}")
    finally:
        _login["running"] = False
        try:
            crawler._close_browser(slot="liepin")
        except Exception:
            pass


def cancel_login():
    global _login_cancel
    _login_cancel = True
    return True


def normalize(cards, kw, city_name, dq):
    """接口 jobCardList → 与 BOSS 对齐的公共字段行（source=liepin）"""
    import salary as sal_mod
    rows, seen = [], set()
    for c in cards:
        j = c.get("job") or {}
        comp = c.get("comp") or {}
        rec = c.get("recruiter") or {}
        jid = str(j.get("jobId") or "")
        link = j.get("link") or ""
        if not jid:
            m = re.search(r"liepin\.com/(?:job|a)/(\d+)", link)
            jid = m.group(1) if m else ""
        if not jid or jid in seen:
            continue
        seen.add(jid)
        salary_txt = (j.get("salary") or "").strip()
        # 猎聘原始值是小写 k（如「130-180k」），BOSS 是「13-18K」，列表里两平台并排看会很别扭 → 统一大写
        salary_txt = re.sub(r"(\d)\s*[kK]\b", r"\1K", salary_txt)
        refresh = str(j.get("refreshTime") or "")
        # 猎聘 dq 形如「厦门-翔安区」或只有「厦门」；剥掉城市前缀，避免列表显示「厦门 厦门-翔安区」
        area = (j.get("dq") or "").strip()
        if city_name and area.startswith(city_name + "-"):
            area = area[len(city_name) + 1:]
        if area == city_name:
            area = ""
        rows.append({
            "job_id": f"lp{jid}",
            "lid": "", "security_id": "",
            "keyword": kw, "city_code": dq, "city_name": city_name,
            "job_name": (j.get("title") or "").strip()[:80],
            "salary": salary_txt,
            "company": (comp.get("compName") or "").strip()[:60],
            "scale": comp.get("compScale") or "",
            "industry": comp.get("compIndustry") or "",
            "degree": j.get("requireEduLevel") or "",
            "experience": j.get("requireWorkYears") or "",
            "skills": "|".join(str(x) for x in (j.get("labels") or [])[:6]),
            "welfare": "",
            "boss_name": rec.get("recruiterName") or "",
            "boss_title": rec.get("recruiterTitle") or "",
            "area_district": area[:60],
            "business_district": "", "lng": "", "lat": "",
            "job_url": link,
            "detail_text": "",
            "brand_stage": comp.get("compStage") or "",
            "job_labels": "|".join(str(x) for x in (j.get("labels") or [])[:8]),
            "gold_hunter": 1 if str(j.get("jobKind")) == "1" else 0,   # 1=猎头职位
            "proxy_job": 0,
            "encrypt_brand_id": str(comp.get("compId") or ""),
            "boss_avatar": "",
            "days_per_week": "", "least_month": "",
            "source": SOURCE,
        })
    return rows


def parse_resp(text):
    """接口响应 → (cards, pagination)。异常/风控返回 (None, {})"""
    try:
        d = json.loads(text)
    except Exception:
        return None, {}
    if not isinstance(d, dict) or d.get("flag") != 1:
        return None, {}
    inner = (d.get("data") or {}).get("data") or {}
    cards = inner.get("jobCardList") or []
    pag = inner.get("pagination") or (d.get("data") or {}).get("pagination") or {}
    return cards, pag


def run_crawl(run_id, keywords, cities, pages, headless=False, offscreen=True, progress=None):
    """猎聘整轮获取（后台线程调用）。cities 复用 BOSS 配置 [{code,name}...]，
    按中文 name 映射 dqCode；无映射城市跳过。返回 (fetched, new_cnt, offline_cnt)
    progress: 可选回调 (done, total, note)"""
    logf = lambda s: db.append_run_log(run_id, s)  # noqa: E731
    fetched = new_cnt = offline_cnt = 0
    tot = max(1, len(keywords) * len(cities) * max(1, int(pages or 1)))
    done = {"n": 0}

    # 每次抓取前刷新城市码表（用户可能刚同步过猎聘城市库）
    global CITY_CODE
    CITY_CODE = _load_city_code()

    def _p(note=""):
        if not progress:
            return
        try:
            progress(min(done["n"], tot), tot, note)
        except Exception:
            pass

    _p("准备浏览器")
    try:
        page = _ensure_liepin_browser(headless=headless, offscreen=offscreen)
        # 过 WAF：进搜索页让 security.min.js 完整执行拿 acw_tc 通行证，之后同源 fetch 才通畅
        page.get("https://www.liepin.com/zhaopin/")
        time.sleep(5)
        if "liepin.com" not in (page.url or ""):
            logf("❌ 猎聘页面加载失败（网络/验证页），本轮中断")
            return 0, 0, 0
        if not cookies_logged(page):
            logf("ℹ️ 猎聘未登录（免登录可搜，结果略少）。想更全可点「登录猎聘」，一次登录长期有效")
        for kw in keywords:
            for city in cities:
                name = city.get("name") if isinstance(city, dict) else str(city)
                dq = CITY_CODE.get(name, "")
                if not dq:
                    logf(f"⏭ 猎聘无该城市码，跳过：{name}（可在 liepin.py CITY_CODE 补码）")
                    continue
                batch, seen_ids = [], set()
                covered_ok = True
                p, cap = 1, min(int(pages), MAX_PAGES_CAP)
                while p <= cap:
                    used, limit, allowed = db.bump_quota(DAILY_QUOTA, source=SOURCE)
                    if not allowed:
                        logf(f"⏸ 猎聘当日预算用完（{used}/{limit}），提前结束")
                        covered_ok = False
                        break
                    try:
                        text = page.run_js(SEARCH_JS, json.dumps(build_body(kw, dq, p),
                                                                 ensure_ascii=False))
                    except Exception as e:
                        logf(f"  请求异常：{str(e)[:80]}")
                        covered_ok = False
                        break
                    cards, pag = parse_resp(text or "")
                    if cards is None:
                        # 失败时留原始响应片段，便于区分 网络错误/WAF挑战/参数错
                        snippet = str(text or "")[:180].replace("\n", " ")
                        if snippet.startswith("FETCHERR"):
                            db.set_risk("net", "猎聘接口请求异常（网络/CORS）", source=SOURCE)
                            logf(f"🔒 猎聘接口 fetch 失败：{snippet}")
                        else:
                            db.set_risk("waf", "猎聘接口返回异常（可能验证/频控）", source=SOURCE)
                            logf(f"🔒 猎聘接口异常响应：{snippet}")
                        covered_ok = False
                        break
                    rows = normalize(cards, kw, name, dq)
                    new_ids = [r["job_id"] for r in rows if r["job_id"] not in seen_ids]
                    if not rows:
                        logf(f"  [{kw}·{name}] 第 {p} 页 0 条，停止翻页")
                        break
                    seen_ids.update(new_ids)
                    batch.extend(rows)
                    logf(f"  [{kw}·{name}] 第 {p} 页 {len(rows)} 条（新 {len(new_ids)}）")
                    done["n"] += 1
                    _p(f"{kw} · {name} 第 {p} 页")
                    if not new_ids:
                        logf("  全部重复 ID，停止翻页")
                        break
                    p += 1
                    if p <= cap:
                        import random
                        time.sleep(random.uniform(*PAGE_DELAY))
                if batch:
                    n, ids = db.upsert_jobs(batch, run_id)
                    fetched += len(batch)
                    new_cnt += n
                    if covered_ok:
                        offline_cnt += db.mark_offline([(kw, dq)], ids, source=SOURCE,
                                                       log=logf)
                    logf(f"  入库 {len(batch)} 条（新增 {n}）")
    except Exception as e:
        logf(f"❌ 猎聘获取异常：{e}")
    finally:
        try:
            crawler._close_browser(slot="liepin")
        except Exception:
            pass
    return fetched, new_cnt, offline_cnt


# 详情页取 JD / 福利 / 区县（实测：schema.org JobPosting 的 description 就是完整 JD；
# 详情页没有经纬度，所以坐标只能靠 db.fill_approx_coords 的区级近似）
DETAIL_JS = r"""
var out={jd:'',area:'',welfare:''};
var lds=document.querySelectorAll('script[type="application/ld+json"]');
for(var i=0;i<lds.length;i++){
  try{var o=JSON.parse(lds[i].textContent);
    if(o&&o['@type']==='JobPosting'){out.jd=o.description||'';break;}
  }catch(e){}
}
if(!out.jd){
  var c=document.querySelector('.job-intro-container')||document.querySelector('.job-detail-box');
  out.jd=c?(c.innerText||''):'';
}
var head=document.querySelector('.job-detail-header-box')||document.body;
var t=(head.innerText||'').replace(/\s+/g,' ');
var m=t.match(/[^\s]{2,4}-[^\s]{2,8}(?:区|县|市)/);
out.area=m?m[0]:'';
var tags=document.querySelectorAll('[class*=job-labels] span,[class*=welfare] span,[class*=labels] span');
var ws=[];
for(var k=0;k<tags.length;k++){
  var x=(tags[k].innerText||'').trim();
  if(x&&x.length<=8&&ws.indexOf(x)<0)ws.push(x);
}
out.welfare=ws.slice(0,10).join('|');
return JSON.stringify(out);
"""


def parse_detail(text):
    """详情页 JS 返回 → {jd, area, welfare}；解析失败返回空 dict"""
    try:
        d = json.loads(text or "{}")
    except Exception:
        return {}
    jd = (d.get("jd") or "").strip()
    # schema.org 的 description 可能带 HTML 实体/标签，统一压平
    jd = re.sub(r"<[^>]+>", "\n", jd)
    jd = jd.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    jd = re.sub(r"\n{3,}", "\n\n", jd).strip()
    return {"jd": jd, "area": (d.get("area") or "").strip(),
            "welfare": (d.get("welfare") or "").strip()}


def run_details(jobs, logf, interval=(3, 6), should_cancel=None, on_progress=None):
    """批量补抓猎聘详情（JD + 福利 + 区县）。返回 (ok, fail)。"""
    import random
    ok = fail = 0
    blocked = 0                   # 被猎聘 302 到登录/中转页的次数
    if not jobs:
        return 0, 0
    page = _ensure_liepin_browser(headless=True, offscreen=True)
    total = len(jobs)
    try:
        for i, j in enumerate(jobs, 1):
            if should_cancel and should_cancel():
                logf("⏹ 已取消，剩余猎聘岗位未抓")
                break
            url = j.get("job_url") or ""
            if not url:
                fail += 1
                continue
            try:
                page.get(url)
                time.sleep(random.uniform(2.0, 3.2))
                # 猎聘对未登录/被风控的请求，会 302 到 wow.liepin.com 的中转页（实测 2026-10-26，
                # 换 UA / 带 Referer / 先访问首页取 cookie 都一样）。这种情况一定是拿不到 JD 的，
                # 直接如实报「被拦截」并计数，别每一条都白跑还报成「页面结构变化」。
                final_url = page.url or ""
                if "wow.liepin.com" in final_url or "transit" in final_url:
                    blocked += 1
                    fail += 1
                    logf(f"  [{i}/{total}] ⛔ 被猎聘拦截：详情页跳到中转页（未登录猎聘时必现）")
                    if blocked >= 3 and ok == 0:
                        logf("⛔ 连续 3 条都被跳转 —— 猎聘登录态已失效。"
                             "先点顶部「猎聘」胶囊重新登录，再回来补抓描述；本轮已停止，避免白跑几百次")
                        break
                    continue
                data = parse_detail(page.run_js(DETAIL_JS) or "")
                if data.get("jd"):
                    area = data.get("area") or ""
                    if area.startswith((j.get("city_name") or "") + "-"):
                        area = area[len(j.get("city_name") or "") + 1:]
                    db.set_detail(j["job_id"], data["jd"], data.get("welfare") or None,
                                  area or None)
                    ok += 1
                    logf(f"  [{i}/{total}] ✅ {j.get('job_name','')[:18]}（{len(data['jd'])}字）")
                else:
                    fail += 1
                    logf(f"  [{i}/{total}] ⚠️ {j.get('job_name','')[:18]}：未取到 JD（页面结构变化/已下架）")
            except Exception as e:
                fail += 1
                logf(f"  [{i}/{total}] ⚠️ 抓取异常：{str(e)[:60]}")
            if on_progress:
                on_progress(i, total)
            if i < total:
                time.sleep(random.uniform(*interval))
    finally:
        try:
            crawler._close_browser(slot="liepin")
        except Exception:
            pass
    return ok, fail
