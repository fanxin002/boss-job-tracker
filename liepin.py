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
LOGIN_URL = "https://www.liepin.com/user/login/"
HOME_URL = "https://www.liepin.com/"

PAGE_DELAY = (5, 11)          # 接口调用间隔（秒）
DAILY_QUOTA = 60              # 每日搜索请求预算（一次翻页 = 1 次）
MAX_PAGES_CAP = 10            # 猎聘接口硬上限 400 条/组合

# BOSS 配置里的中文城市名 → 猎聘 dqCode。
# 带 * 的是公开资料/探针实测验证过的；未知城市会跳过并在日志提示补码。
CITY_CODE = {
    "厦门": "090040", "泉州": "090030", "福州": "090020",   # 探针实测 dqCode
    "北京": "010", "上海": "020", "天津": "030",
    "广州": "050020", "深圳": "050090", "南京": "060020", "杭州": "070020",
    "合肥": "080020", "成都": "280020",
}

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


def cookies_logged(page) -> bool:
    """猎聘登录判据：cookie 含 lt_auth"""
    try:
        names = {c.get("name") if isinstance(c, dict) else getattr(c, "name", None)
                 for c in crawler._cookie_list(page)}
        return "lt_auth" in names
    except Exception:
        return False


def set_login_state(ok, note=""):
    import json as _json
    from datetime import datetime
    _json.dump({"logged": bool(ok), "at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "note": note}, open(STATE_FILE, "w", encoding="utf-8"))


def get_login_state():
    import json as _json
    try:
        st = _json.load(open(STATE_FILE, encoding="utf-8"))
    except Exception:
        st = {"logged": False, "at": "", "note": ""}
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


def run_crawl(run_id, keywords, cities, pages, headless=False, offscreen=True):
    """猎聘整轮获取（后台线程调用）。cities 复用 BOSS 配置 [{code,name}...]，
    按中文 name 映射 dqCode；无映射城市跳过。返回 (fetched, new_cnt, offline_cnt)"""
    logf = lambda s: db.append_run_log(run_id, s)  # noqa: E731
    fetched = new_cnt = offline_cnt = 0
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
                        offline_cnt += db.mark_offline([(kw, dq)], ids, source=SOURCE)
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
