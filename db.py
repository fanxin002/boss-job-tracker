# -*- coding: utf-8 -*-
"""SQLite 存储层：岗位表 / 获取批次表 / 配置表 / 投递记录表
主键 = encryptJobId（BOSS岗位稳定唯一ID，跨天不变，天然去重不重复抓）"""
import json
import os
import sqlite3
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "jobs.db")

_lock = threading.Lock()

DEFAULT_CONFIG = {
    "keywords": "数据分析",
    "cities": json.dumps([{"name": "厦门", "code": "101230200"}], ensure_ascii=False),
    "pages": "10",
    "detail": "0",          # 获取轮是否顺带抓描述（已固定为 0，描述统一走「补抓描述」）
    "daily_enabled": "1",   # 每日定时开关
    "daily_time": "09:30",
    "headless": "1",        # 默认无头静默获取（登录用单独的登录窗口完成）
    "push_enabled": "1",    # 每轮获取结束后 PushPlus 推送汇总（token 在 data/pushplus.txt）
    "greeting": "",         # 投递招呼语（留空=用BOSS默认招呼语）
    "crawl_degrees": "",    # 获取学历筛选（BOSS degree 码，逗号分隔，如 202,203）
    "profile_skills": "[]",       # 我的技能关键词（JSON 数组），由简历解析自动生成
    "profile_salary": "0",        # 期望月薪上限 K（来自简历期望）
    "profile_degree": "",         # 我的学历（来自简历）
    "profile_json": "{}",         # 完整画像（技能明细/年限/期望城市与职位/来源）
    "profile_source": "",         # boss 同步 或 上传文件名
    "profile_updated": "",        # 画像更新时间
    "quota_date": "",             # 请求预算计数日期
    "quota_used": "0",            # 当日已用接口请求数
    "apply_max_batch": "20",     # 单批投递上限
    "apply_interval_min": "12",  # 投递间隔下限（秒）
    "apply_interval_max": "30",  # 投递间隔上限（秒）
    "risk_until": "",            # 风控冷却截止时间（空=未锁定）
    "risk_code": "",             # 命中的风控码
    "risk_msg": "",
    "risk_hits": "0",
    "risk_at": "",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,          -- encryptJobId，岗位唯一ID
    lid TEXT, security_id TEXT,
    keyword TEXT, city_code TEXT, city_name TEXT,
    job_name TEXT, salary TEXT, company TEXT, scale TEXT, industry TEXT,
    degree TEXT, experience TEXT, skills TEXT, welfare TEXT, boss_name TEXT,
    area_district TEXT, business_district TEXT, lng TEXT, lat TEXT,
    job_url TEXT, detail_text TEXT,
    salary_min INTEGER, salary_max INTEGER,   -- 月薪K数值区间（便于筛选/排序）
    first_run_id INTEGER, last_run_id INTEGER,
    first_seen TEXT, last_seen TEXT,
    is_new INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active',
    applied INTEGER DEFAULT 0,               -- 投递状态：0未投递 1成功 2失败
    applied_at TEXT,
    starred INTEGER DEFAULT 0,               -- 收藏
    note TEXT,                               -- 备注
    replied INTEGER DEFAULT 0,               -- HR 是否已回复
    reply_note TEXT,                         -- 回复摘要
    reply_at TEXT,                           -- 回复同步时间
    match_score INTEGER DEFAULT 0,           -- JD 匹配分（本地规则）
    boss_title TEXT,                         -- HR 职位（人事经理/技术总监…）
    brand_stage TEXT,                        -- 融资阶段（未融资/A轮/已上市…）
    job_labels TEXT,                         -- 接口原样标签（1-3年,大专）
    days_per_week TEXT,                      -- 实习到岗天数（兼职/实习岗）
    least_month TEXT,                        -- 最短实习月数
    proxy_job INTEGER DEFAULT 0,             -- 是否代招（代招/派遣岗，慎重投递）
    gold_hunter INTEGER DEFAULT 0,           -- 是否猎头发布
    encrypt_brand_id TEXT,                   -- 公司加密ID（同公司聚合用）
    boss_avatar TEXT                         -- HR 头像 URL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, trigger TEXT,
    keywords TEXT, cities TEXT, pages INTEGER,
    status TEXT DEFAULT 'running',
    fetched_count INTEGER DEFAULT 0,
    new_count INTEGER DEFAULT 0,
    offline_count INTEGER DEFAULT 0,
    log TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT, job_name TEXT, company TEXT, job_url TEXT,
    status TEXT,           -- success / failed
    message TEXT,          -- 结果说明（风控/失败原因等）
    batch_id TEXT,
    created_at TEXT
);
"""

COLS = ["job_id", "lid", "security_id", "keyword", "city_code", "city_name", "job_name",
        "salary", "company", "scale", "industry", "degree", "experience", "skills",
        "welfare", "boss_name", "area_district", "business_district", "lng", "lat",
        "job_url", "detail_text", "salary_min", "salary_max",
        "first_run_id", "last_run_id", "first_seen",
        "last_seen", "is_new", "status",
        "boss_title", "brand_stage", "job_labels",
        "days_per_week", "least_month", "proxy_job", "gold_hunter",
        "encrypt_brand_id", "boss_avatar"]

# 薪资「空值不覆盖」：游客态/风控降级时接口会返回空 salaryDesc，
# 若直接覆盖会把之前登录态抓到的真实薪资抹掉。仅当新值非空/非NULL才更新。
UPSERT_SQL = f"""
INSERT INTO jobs ({",".join(COLS)}) VALUES ({",".join("?" * len(COLS))})
ON CONFLICT(job_id) DO UPDATE SET
    lid=excluded.lid, security_id=excluded.security_id,
    last_seen=excluded.last_seen, last_run_id=excluded.last_run_id,
    is_new=0, status='active',
    salary=CASE WHEN excluded.salary<>'' THEN excluded.salary ELSE jobs.salary END,
    salary_min=CASE WHEN excluded.salary_min IS NOT NULL THEN excluded.salary_min ELSE jobs.salary_min END,
    salary_max=CASE WHEN excluded.salary_max IS NOT NULL THEN excluded.salary_max ELSE jobs.salary_max END,
    job_name=excluded.job_name, company=excluded.company,
    boss_title=CASE WHEN excluded.boss_title<>'' THEN excluded.boss_title ELSE jobs.boss_title END,
    brand_stage=CASE WHEN excluded.brand_stage<>'' THEN excluded.brand_stage ELSE jobs.brand_stage END,
    job_labels=CASE WHEN excluded.job_labels<>'' THEN excluded.job_labels ELSE jobs.job_labels END,
    days_per_week=CASE WHEN excluded.days_per_week<>'' THEN excluded.days_per_week ELSE jobs.days_per_week END,
    least_month=CASE WHEN excluded.least_month<>'' THEN excluded.least_month ELSE jobs.least_month END,
    proxy_job=excluded.proxy_job, gold_hunter=excluded.gold_hunter,
    encrypt_brand_id=CASE WHEN excluded.encrypt_brand_id<>'' THEN excluded.encrypt_brand_id ELSE jobs.encrypt_brand_id END,
    boss_avatar=CASE WHEN excluded.boss_avatar<>'' THEN excluded.boss_avatar ELSE jobs.boss_avatar END,
    detail_text=COALESCE(NULLIF(excluded.detail_text,''),jobs.detail_text)
"""


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _migrate(c):
    """列级增量迁移：旧库自动补新列，不丢数据"""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    for col, typ in [("salary_min", "INTEGER"), ("salary_max", "INTEGER"),
                     ("applied", "INTEGER DEFAULT 0"), ("applied_at", "TEXT"),
                     ("starred", "INTEGER DEFAULT 0"), ("note", "TEXT"),
                     ("replied", "INTEGER DEFAULT 0"), ("reply_note", "TEXT"),
                     ("reply_at", "TEXT"), ("match_score", "INTEGER DEFAULT 0"),
                     ("boss_title", "TEXT"),
                     ("brand_stage", "TEXT"), ("job_labels", "TEXT"),
                     ("days_per_week", "TEXT"), ("least_month", "TEXT"),
                     ("proxy_job", "INTEGER DEFAULT 0"), ("gold_hunter", "INTEGER DEFAULT 0"),
                     ("encrypt_brand_id", "TEXT"), ("boss_avatar", "TEXT")]:
        if col not in cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    return cols


def init_db():
    with _lock:
        c = conn()
        c.executescript(SCHEMA)
        # 迁移：旧结构（主键lid）→ 新结构；新库无损
        cols = [r["name"] for r in c.execute("PRAGMA table_info(jobs)")]
        if cols and "job_id" not in cols:
            c.execute("DROP TABLE IF EXISTS jobs")
            c.executescript(SCHEMA)
        _migrate(c)
        for k, v in DEFAULT_CONFIG.items():
            c.execute("INSERT OR IGNORE INTO config(key,value) VALUES(?,?)", (k, v))
        c.commit()
        c.close()


def get_config() -> dict:
    c = conn()
    rows = {r["key"]: r["value"] for r in c.execute("SELECT * FROM config")}
    c.close()
    merged = dict(DEFAULT_CONFIG)
    merged.update(rows)
    return merged


def set_config(items: dict):
    with _lock:
        c = conn()
        for k, v in items.items():
            c.execute(
                "INSERT INTO config(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, str(v)))
        c.commit()
        c.close()


def create_run(keywords, cities, pages, trigger) -> int:
    with _lock:
        c = conn()
        cur = c.execute(
            "INSERT INTO runs(started_at,trigger,keywords,cities,pages) VALUES(?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), trigger, keywords, cities, pages))
        c.commit()
        rid = cur.lastrowid
        c.close()
        return rid


def append_run_log(run_id, line):
    ts = datetime.now().strftime("[%H:%M:%S] ")
    print(line, flush=True)
    with _lock:
        c = conn()
        c.execute("UPDATE runs SET log = log || ? WHERE id=?", (ts + line + "\n", run_id))
        c.commit()
        c.close()


def finish_run(run_id, status, fetched, new_cnt, offline_cnt):
    with _lock:
        c = conn()
        c.execute(
            "UPDATE runs SET finished_at=?, status=?, fetched_count=?, new_count=?, offline_count=? WHERE id=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), status, fetched, new_cnt, offline_cnt, run_id))
        c.commit()
        c.close()


def upsert_jobs(rows, run_id):
    """增量入库：job_id 已存在→刷新 last_seen/取消新增；不存在→插入并标记 is_new=1"""
    import salary as sal
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_cnt, seen = 0, []
    with _lock:
        c = conn()
        _migrate(c)
        for j in rows:
            if not j.get("job_id"):
                continue
            seen.append(j["job_id"])
            if not c.execute("SELECT 1 FROM jobs WHERE job_id=?", (j["job_id"],)).fetchone():
                new_cnt += 1
            smin, smax = sal.parse_k(j.get("salary", ""))
            params = [
                j.get("job_id"), j.get("lid", ""), j.get("security_id", ""),
                j.get("keyword", ""), j.get("city_code", ""), j.get("city_name", ""),
                j.get("job_name", ""), j.get("salary", ""), j.get("company", ""),
                j.get("scale", ""), j.get("industry", ""), j.get("degree", ""),
                j.get("experience", ""), j.get("skills", ""), j.get("welfare", ""),
                j.get("boss_name", ""), j.get("area_district", ""),
                j.get("business_district", ""), j.get("lng", ""), j.get("lat", ""),
                j.get("job_url", ""), j.get("detail_text", ""),
                smin, smax,
                run_id, run_id, now, now, 1, "active",
                j.get("boss_title", ""),
                j.get("brand_stage", ""), j.get("job_labels", ""),
                j.get("days_per_week", ""), j.get("least_month", ""),
                1 if j.get("proxy_job") else 0, 1 if j.get("gold_hunter") else 0,
                j.get("encrypt_brand_id", ""), j.get("boss_avatar", ""),
            ]
            c.execute(UPSERT_SQL, params)
        c.commit()
        c.close()
    return new_cnt, seen


def mark_offline(covered_pairs, run_job_ids):
    """本次健康覆盖的 (keyword, city_code) 范围内：
    - 未再出现的 active 岗位 → 标记下线
    - 范围内本轮未触及的岗位 → 清掉 is_new（新增=本轮首次出现）"""
    offline = 0
    ids = list(run_job_ids)
    if not ids:
        return 0
    with _lock:
        c = conn()
        placeholders = ",".join("?" * len(ids))
        for kw, cc in covered_pairs:
            cur = c.execute(
                f"UPDATE jobs SET status='offline' "
                f"WHERE keyword=? AND city_code=? AND status='active' AND job_id NOT IN ({placeholders})",
                [kw, cc] + ids)
            offline += cur.rowcount
            c.execute(
                f"UPDATE jobs SET is_new=0 "
                f"WHERE keyword=? AND city_code=? AND is_new=1 AND job_id NOT IN ({placeholders})",
                [kw, cc] + ids)
        c.commit()
        c.close()
    return offline


# ---------- 筛选条件构建（query_jobs / all_job_rows 共用） ----------

_SORTABLE = {  # 前端sort参数 → SQL；白名单防注入
    "new": "is_new DESC, first_seen DESC",
    "salary_desc": "COALESCE(salary_max,-1) DESC, is_new DESC, first_seen DESC",
    "salary_asc": "CASE WHEN salary_min IS NULL THEN 99999 ELSE salary_min END ASC, is_new DESC",
    "seen_desc": "last_seen DESC",
    "first_desc": "first_seen DESC",
    "match": "COALESCE(match_score,0) DESC, COALESCE(salary_max,-1) DESC",
    "match_asc": "COALESCE(match_score,0) ASC, COALESCE(salary_max,-1) ASC",
    "reply_desc": "COALESCE(reply_at,'') DESC, COALESCE(match_score,0) DESC",
    "reply_asc": "COALESCE(reply_at,'9999') ASC, COALESCE(match_score,0) DESC",
    "star_desc": "starred DESC, COALESCE(match_score,0) DESC",
    "first_asc": "first_seen ASC",
    "company_asc": "company COLLATE NOCASE ASC, COALESCE(salary_max,-1) DESC",
    "company_desc": "company COLLATE NOCASE DESC, COALESCE(salary_max,-1) DESC",
}


def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _build_where(filters):
    where, params = [], []
    if filters.get("keyword"):
        where.append("keyword=?")
        params.append(filters["keyword"])
    if filters.get("city_code"):
        where.append("city_code=?")
        params.append(filters["city_code"])
    if filters.get("status"):
        where.append("status=?")
        params.append(filters["status"])
    if str(filters.get("is_new") or "") == "1":
        where.append("is_new=1")
    ap = _to_int(filters.get("applied"))
    if ap in (0, 1, 2):
        where.append("applied=?")
        params.append(ap)
    ns = str(filters.get("no_salary") or "")
    if ns == "1":      # 只看有明确薪资
        where.append("salary_min IS NOT NULL AND salary_min>0")
    elif ns == "2":    # 只看薪资未知
        where.append("(salary_min IS NULL OR salary_min=0)")
    hd = str(filters.get("has_detail") or "")
    if hd == "1":      # 只看已有描述
        where.append("detail_text IS NOT NULL AND detail_text<>''")
    elif hd == "0":    # 只看缺描述
        where.append("(detail_text IS NULL OR detail_text='')")
    st_flag = str(filters.get("starred") or "")
    if st_flag == "1":
        where.append("starred=1")
    rp = str(filters.get("replied") or "")
    if rp == "1":
        where.append("replied=1")
    elif rp == "0":
        where.append("COALESCE(replied,0)=0")
    od = _to_int(filters.get("overdue"))
    if od and od > 0:
        # 投递超期：已投递 + 未回复 + 在招 + 距投递(或发现)≥N天
        where.append(
            "applied=1 AND COALESCE(replied,0)=0 AND status='active' "
            "AND julianday('now','localtime') - "
            "julianday(COALESCE(NULLIF(applied_at,''), first_seen)) >= ?")
        params.append(od)
    if filters.get("degree"):
        where.append("degree=?")
        params.append(filters["degree"])
    if filters.get("experience"):
        where.append("experience=?")
        params.append(filters["experience"])
    if filters.get("scale"):
        where.append("scale=?")
        params.append(filters["scale"])
    if filters.get("industry"):
        where.append("industry LIKE ?")
        params.append(f"%{filters['industry']}%")
    sal_min = _to_int(filters.get("salary_min"))
    sal_max = _to_int(filters.get("salary_max"))
    if sal_min:
        where.append("COALESCE(salary_max,0) >= ?")   # 岗位上限 ≥ 期望下限
        params.append(sal_min)
    if sal_max:
        where.append("(salary_min IS NULL OR salary_min <= ?)")  # 岗位下限 ≤ 期望上限
        params.append(sal_max)
    if filters.get("q"):
        q = f"%{filters['q']}%"
        where.append("(job_name LIKE ? OR company LIKE ? OR skills LIKE ? OR welfare LIKE ? OR detail_text LIKE ?)")
        params += [q] * 5
    ws = ("WHERE " + " AND ".join(where)) if where else ""
    return ws, params


def query_jobs(filters: dict):
    c = conn()
    ws, params = _build_where(filters)
    order = _SORTABLE.get(filters.get("sort") or "default", _SORTABLE["salary_desc"])
    size = min(int(filters.get("size") or 50), 200)
    page = max(1, int(filters.get("page") or 1))
    if str(filters.get("dedupe") or "") == "1":
        # 同公司同岗位名合并展示，保留排序最靠前的一条，并带出重复条数
        base = (f"SELECT *, COUNT(*) OVER (PARTITION BY company, job_name) AS dup_count, "
                f"ROW_NUMBER() OVER (PARTITION BY company, job_name ORDER BY {order}) AS _rn "
                f"FROM jobs {ws}")
        total = c.execute(f"SELECT COUNT(*) FROM ({base}) WHERE _rn=1", params).fetchone()[0]
        rows = c.execute(f"SELECT * FROM ({base}) WHERE _rn=1 ORDER BY {order} LIMIT ? OFFSET ?",
                         params + [size, (page - 1) * size]).fetchall()
    else:
        total = c.execute(f"SELECT COUNT(*) FROM jobs {ws}", params).fetchone()[0]
        rows = c.execute(
            f"SELECT * FROM jobs {ws} ORDER BY {order} LIMIT ? OFFSET ?",
            params + [size, (page - 1) * size]).fetchall()
    c.close()
    return total, [dict(r) for r in rows]


def distinct_filters():
    c = conn()
    kws = [r["keyword"] for r in c.execute(
        "SELECT DISTINCT keyword FROM jobs WHERE keyword IS NOT NULL ORDER BY keyword")]
    cities = c.execute(
        "SELECT DISTINCT city_name, city_code FROM jobs WHERE city_code IS NOT NULL ORDER BY city_name").fetchall()
    degrees = [r["degree"] for r in c.execute(
        "SELECT DISTINCT degree FROM jobs WHERE degree NOT IN ('','不限') AND degree IS NOT NULL ORDER BY degree")]
    exps = [r["experience"] for r in c.execute(
        "SELECT DISTINCT experience FROM jobs WHERE experience NOT IN ('','不限') AND experience IS NOT NULL ORDER BY experience")]
    scales = [r["scale"] for r in c.execute(
        "SELECT DISTINCT scale FROM jobs WHERE scale NOT IN ('','') AND scale IS NOT NULL ORDER BY scale")]
    c.close()
    return {"keywords": kws, "cities": [dict(r) for r in cities],
            "degrees": degrees, "experiences": exps, "scales": scales}


def get_runs(limit=30):
    c = conn()
    rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def all_job_rows(filters: dict):
    c = conn()
    ws, params = _build_where(filters)
    rows = c.execute(f"SELECT * FROM jobs {ws} ORDER BY first_seen DESC", params).fetchall()
    c.close()
    return [dict(r) for r in rows]


def get_job(job_id):
    c = conn()
    r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    c.close()
    return dict(r) if r else None


# ---------- 收藏 / 备注 / 回复 / 匹配分 ----------

def new_jobs_of_run(run_id, limit=5):
    """本轮首次出现的岗位（用于推送摘要）"""
    c = conn()
    rows = c.execute(
        "SELECT job_id, job_name, company, salary, city_name, job_url FROM jobs "
        "WHERE first_run_id=? AND status='active' ORDER BY COALESCE(salary_max,-1) DESC LIMIT ?",
        (run_id, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def counts():
    """轻量计数（列表刷新用，替代拉全量 stats）"""
    c = conn()
    one = lambda s: c.execute(s).fetchone()[0]  # noqa: E731
    out = {
        "total": one("SELECT COUNT(*) FROM jobs"),
        "active": one("SELECT COUNT(*) FROM jobs WHERE status='active'"),
        "offline": one("SELECT COUNT(*) FROM jobs WHERE status='offline'"),
        "new": one("SELECT COUNT(*) FROM jobs WHERE is_new=1"),
        "applied": one("SELECT COUNT(*) FROM jobs WHERE applied=1"),
        "salary_missing": one("SELECT COUNT(*) FROM jobs WHERE salary_min IS NULL OR salary_min<=0"),
        "desc_done": one("SELECT COUNT(*) FROM jobs WHERE detail_text IS NOT NULL AND detail_text<>''"),
        "starred": one("SELECT COUNT(*) FROM jobs WHERE starred=1"),
        "replied": one("SELECT COUNT(*) FROM jobs WHERE replied=1"),
        "overdue": one(
            "SELECT COUNT(*) FROM jobs WHERE applied=1 AND COALESCE(replied,0)=0 AND status='active' "
            "AND julianday('now','localtime') - julianday(COALESCE(NULLIF(applied_at,''), first_seen)) >= 3"),
    }
    c.close()
    return out


def set_star(job_id, star=1):
    with _lock:
        c = conn()
        cur = c.execute("UPDATE jobs SET starred=? WHERE job_id=?", (1 if star else 0, job_id))
        c.commit()
        c.close()
        return cur.rowcount


def set_note(job_id, note):
    with _lock:
        c = conn()
        cur = c.execute("UPDATE jobs SET note=? WHERE job_id=?", ((note or "")[:500], job_id))
        c.commit()
        c.close()
        return cur.rowcount


def mark_replied(job_id, snippet="", when=None):
    from datetime import datetime as _dt
    with _lock:
        c = conn()
        cur = c.execute("UPDATE jobs SET replied=1, reply_note=?, reply_at=? WHERE job_id=?",
                        ((snippet or "")[:300], when or _dt.now().strftime("%Y-%m-%d %H:%M:%S"), job_id))
        c.commit()
        c.close()
        return cur.rowcount


def set_replied(job_id, replied=1, note=""):
    """手动标记 / 取消「HR 已回复」（会话列表匹配不到时可人工纠正）"""
    from datetime import datetime as _dt
    with _lock:
        c = conn()
        if replied:
            cur = c.execute(
                "UPDATE jobs SET replied=1, reply_note=?, reply_at=? WHERE job_id=?",
                ((note or "手动标记")[:300], _dt.now().strftime("%Y-%m-%d %H:%M:%S"), job_id))
        else:
            cur = c.execute(
                "UPDATE jobs SET replied=0, reply_note='', reply_at=NULL WHERE job_id=?", (job_id,))
        c.commit()
        c.close()
        return cur.rowcount


def get_replied(limit=200):
    """已回复岗位列表（按回复时间倒序，前端「HR 回复」页用）"""
    c = conn()
    rows = c.execute(
        "SELECT job_id, job_name, company, salary, salary_min, salary_max, city_name, area_district, "
        "job_url, status, applied, applied_at, replied, reply_note, reply_at, note, starred, "
        "match_score, first_seen FROM jobs WHERE replied=1 "
        "ORDER BY COALESCE(reply_at,'') DESC, COALESCE(match_score,0) DESC LIMIT ?",
        (limit,)).fetchall()
    stats = c.execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) applied, "
        "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active "
        "FROM jobs WHERE replied=1").fetchone()
    c.close()
    return {"total": stats["total"] or 0, "applied": stats["applied"] or 0,
            "active": stats["active"] or 0, "rows": [dict(r) for r in rows]}


_DEG_RANK = {"初中及以下": 1, "中专/中技": 2, "中技": 2, "高中": 3, "大专": 4,
             "本科": 5, "硕士": 6, "博士": 7}


def get_profile():
    """读取当前画像（来自 BOSS 简历同步或上传解析）"""
    import json as _json
    cfg = get_config()
    prof = {}
    try:
        prof = _json.loads(cfg.get("profile_json") or "{}")
    except Exception:
        prof = {}
    if not prof.get("skills"):
        try:
            prof["skills"] = _json.loads(cfg.get("profile_skills") or "[]")
        except Exception:
            prof["skills"] = []
    prof.setdefault("degree", cfg.get("profile_degree") or "")
    prof.setdefault("expect_salary", int(cfg.get("profile_salary") or 0))
    prof.setdefault("expect_cities", [])
    prof.setdefault("expect_titles", [])
    prof.setdefault("source", "")
    prof.setdefault("updated", "")
    prof.setdefault("years", 0)
    prof.setdefault("chars", 0)
    return prof


def save_profile(prof):
    """画像落库（同步写 profile_* 兼容字段 + 完整 profile_json）"""
    import json as _json
    set_config({
        "profile_json": _json.dumps(prof, ensure_ascii=False),
        "profile_skills": _json.dumps(prof.get("skills") or [], ensure_ascii=False),
        "profile_salary": str(int(prof.get("expect_salary") or 0)),
        "profile_degree": prof.get("degree") or "",
        "profile_source": prof.get("source") or "",
        "profile_updated": prof.get("updated") or "",
    })


def recompute_scores(prof=None):
    """本地规则打分（不调用任何模型）：
    技能命中 45 + 薪资 25 + 学历 15 + 职位相关 10 + 城市 5
    技能权重来自简历解析结果，命中的核心技能越多分越高"""
    if prof is None:
        prof = get_profile()
    skills = [s for s in (prof.get("skills") or []) if str(s).strip()]
    core = [s for s in (prof.get("core_skills") or skills) if str(s).strip()]
    salary_expect = int(prof.get("expect_salary") or 0)
    my_rank = _DEG_RANK.get(prof.get("degree") or "", 0)
    cities = [c for c in (prof.get("expect_cities") or []) if c]
    titles = [t for t in (prof.get("expect_titles") or []) if t]

    # 职位相关词：期望职位名切片 + 通用分析岗词
    rel = set()
    for t in titles:
        for chunk in (t, t.replace("师", ""), t.replace("工程师", "")):
            if len(chunk) >= 2:
                rel.add(chunk)
    for w in ("数据分析", "数据开发", "数据运营", "商业分析", "BI", "数据工程", "业务分析"):
        rel.add(w)

    low_skills = [(s.lower(), s) for s in skills]
    c = conn()
    rows = c.execute(
        "SELECT job_id, job_name, skills, welfare, detail_text, salary_min, salary_max, "
        "degree, city_name FROM jobs"
    ).fetchall()
    upd = []
    for r in rows:
        hay = " ".join([str(r["job_name"] or ""), str(r["skills"] or ""),
                        str(r["detail_text"] or ""), str(r["welfare"] or "")]).lower()
        name_low = str(r["job_name"] or "").lower()

        # 1) 技能命中：核心技能命中率 * 32 + 其他技能命中数 * 1.6，上限 45
        core_hit = sum(1 for k, _ in low_skills if k in hay and _ in core)
        other_hit = sum(1 for k, _ in low_skills if k in hay and _ not in core)
        s_skill = 0.0
        if core:
            s_skill += 32.0 * core_hit / len(core)
        s_skill += min(other_hit * 1.6, 13)
        score = min(s_skill, 45)

        # 2) 薪资 25
        if salary_expect:
            smax = r["salary_max"] or 0
            if smax >= salary_expect:
                score += 25
            elif smax >= salary_expect * 0.8:
                score += 13
        else:
            score += 12

        # 3) 学历 15
        need = _DEG_RANK.get(str(r["degree"] or ""), 0)
        if need == 0 or my_rank == 0 or need <= my_rank:
            score += 15

        # 4) 职位相关 10
        if any(w.lower() in name_low for w in rel if w):
            score += 10

        # 5) 城市 5
        if cities and r["city_name"] and any(cy in str(r["city_name"]) for cy in cities):
            score += 5
        elif not cities:
            score += 2

        upd.append((int(min(round(score), 100)), r["job_id"]))
    with _lock:
        c.executemany("UPDATE jobs SET match_score=? WHERE job_id=?", upd)
        c.commit()
    c.close()
    return len(upd)


# ---------- 投递记录 ----------

def set_applied(job_ids, applied, clear=False):
    """更新岗位投递状态。clear=True 时把这些岗位重置为未投递"""
    if not job_ids:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock:
        c = conn()
        _migrate(c)
        ph = ",".join("?" * len(job_ids))
        if clear:
            cur = c.execute(
                f"UPDATE jobs SET applied=0, applied_at=NULL WHERE job_id IN ({ph})", list(job_ids))
        else:
            cur = c.execute(
                f"UPDATE jobs SET applied=?, applied_at=? WHERE job_id IN ({ph})",
                [applied, now] + list(job_ids))
        c.commit()
        c.close()
        return cur.rowcount


def set_detail(job_id, text):
    """回填职位描述"""
    with _lock:
        c = conn()
        cur = c.execute("UPDATE jobs SET detail_text=? WHERE job_id=?", (text or "", job_id))
        c.commit()
        c.close()
        return cur.rowcount


def mark_job_offline(job_id):
    """投递时发现岗位已下架 → 同步状态"""
    with _lock:
        c = conn()
        c.execute("UPDATE jobs SET status='offline' WHERE job_id=?", (job_id,))
        c.commit()
        c.close()


def add_application(job, status, message, batch_id):
    with _lock:
        c = conn()
        c.execute(
            "INSERT INTO applications(job_id,job_name,company,job_url,status,message,batch_id,created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (job.get("job_id"), job.get("job_name"), job.get("company"), job.get("job_url"),
             status, message, batch_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        c.commit()
        c.close()


# ---------- 风控冷却锁 ----------
# 命中 code=36/37/38 后锁定一段时间，禁止手动/定时重跑，避免连续请求延长封控。
# 连续命中逐级加长：60 → 120 → 240 → 720 分钟封顶。
_COOLDOWN_STEPS = [60, 120, 240, 720]


def set_risk(code, msg=""):
    """记录一次风控命中并开启冷却窗口，返回冷却信息"""
    from datetime import timedelta
    cfg = get_config()
    hits = int(cfg.get("risk_hits") or 0) + 1
    minutes = _COOLDOWN_STEPS[min(hits - 1, len(_COOLDOWN_STEPS) - 1)]
    now = datetime.now()
    until = now + timedelta(minutes=minutes)
    set_config({
        "risk_code": str(code), "risk_msg": str(msg or "")[:200],
        "risk_hits": str(hits),
        "risk_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "risk_until": until.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return get_risk()


def get_risk():
    cfg = get_config()
    until_s = (cfg.get("risk_until") or "").strip()
    info = {"active": False, "code": cfg.get("risk_code") or "", "msg": cfg.get("risk_msg") or "",
            "hits": int(cfg.get("risk_hits") or 0), "at": cfg.get("risk_at") or "",
            "until": until_s, "remaining_min": 0}
    if until_s:
        try:
            until = datetime.strptime(until_s, "%Y-%m-%d %H:%M:%S")
            left = (until - datetime.now()).total_seconds() / 60
            if left > 0:
                info["active"] = True
                info["remaining_min"] = int(left) + 1
        except Exception:
            pass
    return info


def clear_risk():
    set_config({"risk_until": "", "risk_code": "", "risk_msg": "", "risk_hits": "0"})


def bump_quota(limit=150):
    """当日接口请求计数（每翻一页 +1）。返回 (used, limit, allowed)。
    跨天自动归零。预算用完不是风控，只表示今天别再请求了。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cfg = get_config()
    if (cfg.get("quota_date") or "") != today:
        used = 0
    else:
        used = int(cfg.get("quota_used") or 0)
    used += 1
    set_config({"quota_date": today, "quota_used": str(used)})
    return used, limit, used <= limit


def get_quota(limit=150):
    cfg = get_config()
    today = datetime.now().strftime("%Y-%m-%d")
    used = int(cfg.get("quota_used") or 0) if (cfg.get("quota_date") or "") == today else 0
    return {"date": today, "used": used, "limit": limit,
            "left": max(0, limit - used), "pct": round(used * 100.0 / limit, 1) if limit else 0}


def stats(days=14):
    """看板聚合数据（一次查询多组，避免前端多次请求）"""
    from datetime import date, timedelta
    c = conn()
    g = lambda sql, p=(): c.execute(sql, p).fetchone()[0]  # noqa: E731
    out = {
        "total": g("SELECT COUNT(*) FROM jobs"),
        "active": g("SELECT COUNT(*) FROM jobs WHERE status='active'"),
        "offline": g("SELECT COUNT(*) FROM jobs WHERE status='offline'"),
        "new_today": g("SELECT COUNT(*) FROM jobs WHERE substr(first_seen,1,10)=?", (date.today().isoformat(),)),
        "applied": g("SELECT COUNT(*) FROM jobs WHERE applied=1"),
        "apply_fail": g("SELECT COUNT(*) FROM jobs WHERE applied=2"),
        "salary_known": g("SELECT COUNT(*) FROM jobs WHERE salary_min IS NOT NULL AND salary_min>0"),
        "desc_done": g("SELECT COUNT(*) FROM jobs WHERE detail_text IS NOT NULL AND detail_text<>''"),
    }
    out["salary_missing"] = out["total"] - out["salary_known"]
    avg = c.execute("SELECT AVG((salary_min+salary_max)/2.0) FROM jobs "
                    "WHERE salary_min IS NOT NULL AND salary_max IS NOT NULL").fetchone()[0]
    out["avg_salary"] = round(avg, 1) if avg else None
    out["apply_rate"] = round(out["applied"] * 100.0 / out["active"], 1) if out["active"] else 0
    out["desc_rate"] = round(out["desc_done"] * 100.0 / out["total"], 1) if out["total"] else 0

    # 近 7 天 vs 前 7 天（看板环比）
    d7 = (date.today() - timedelta(days=6)).isoformat()
    d14 = (date.today() - timedelta(days=13)).isoformat()
    out["new_7d"] = g("SELECT COUNT(*) FROM jobs WHERE substr(first_seen,1,10)>=?", (d7,))
    out["new_prev_7d"] = g("SELECT COUNT(*) FROM jobs WHERE substr(first_seen,1,10)>=? "
                           "AND substr(first_seen,1,10)<?", (d14, d7))
    prev = out["new_prev_7d"]
    out["new_delta"] = round((out["new_7d"] - prev) * 100.0 / prev, 1) if prev else None

    # 近 N 天新增趋势
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = c.execute("SELECT substr(first_seen,1,10) d, COUNT(*) n FROM jobs "
                     "WHERE substr(first_seen,1,10)>=? GROUP BY d", (start,)).fetchall()
    m = {r["d"]: r["n"] for r in rows}
    trend = []
    for i in range(days):
        d = (date.today() - timedelta(days=days - 1 - i)).isoformat()
        trend.append({"date": d[5:], "n": m.get(d, 0)})
    out["trend"] = trend

    # 薪资分布（月薪下限 K）
    buckets = [(0, 5, "<5K"), (5, 8, "5-8K"), (8, 10, "8-10K"), (10, 15, "10-15K"),
               (15, 20, "15-20K"), (20, 30, "20-30K"), (30, 50, "30-50K"), (50, 999, "50K+")]
    salary = [{"label": lb, "n": 0} for _, _, lb in buckets]
    for r in c.execute("SELECT salary_min FROM jobs WHERE salary_min IS NOT NULL AND salary_min>0"):
        v = r["salary_min"]
        for i, (lo, hi, _lb) in enumerate(buckets):
            if lo <= v < hi:
                salary[i]["n"] += 1
                break
    out["salary_dist"] = salary

    def top(col, limit=8, skip_empty=True):
        cond = f"{col} IS NOT NULL AND {col}<>''" if skip_empty else "1=1"
        return [{"label": r[0], "n": r[1]} for r in c.execute(
            f"SELECT {col} label, COUNT(*) n FROM jobs WHERE {cond} "
            f"GROUP BY {col} ORDER BY n DESC LIMIT ?", (limit,))]
    out["degrees"] = top("degree", 6)
    out["experiences"] = top("experience", 6)
    out["scales"] = top("scale", 6)
    out["cities"] = top("city_name", 8)
    out["industries"] = top("industry", 8)
    out["keywords"] = top("keyword", 8)
    out["stages"] = top("brand_stage", 6)
    out["boss_titles"] = top("boss_title", 8)
    out["proxy"] = g("SELECT COUNT(*) FROM jobs WHERE status='active' AND COALESCE(proxy_job,0)=1")
    c.close()
    return out


def map_points():
    """内置地图用：在招岗位的坐标点位（含薪资/HR在线状态）"""
    c = conn()
    rows = c.execute(
        "SELECT job_id, job_name, company, salary, salary_min, salary_max, city_name, area_district, "
        "business_district, lng, lat, boss_name, boss_title, status, applied, match_score, job_url "
        "FROM jobs WHERE lng IS NOT NULL AND lng<>'' AND lat IS NOT NULL AND lat<>'' "
        "ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
        "COALESCE(match_score,0) DESC LIMIT 1500").fetchall()

    c.close()
    out = []
    for r in rows:
        try:
            lng, lat = float(r["lng"]), float(r["lat"])
        except (TypeError, ValueError):
            continue
        d = dict(r)
        d["lng"], d["lat"] = lng, lat
        out.append(d)
    return out


def get_applications(limit=50, batch_id=None):
    with _lock:
        c = conn()
        if batch_id:
            rows = c.execute("SELECT * FROM applications WHERE batch_id=? ORDER BY id DESC LIMIT ?",
                             (batch_id, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM applications ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        c.close()
        return [dict(r) for r in rows]
