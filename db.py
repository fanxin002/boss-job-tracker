# -*- coding: utf-8 -*-
"""SQLite 存储层：岗位表 / 获取批次表 / 配置表 / 投递记录表
主键 = encryptJobId（BOSS岗位稳定唯一ID，跨天不变，天然去重不重复抓）"""
import json
import os
import re
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
    "boss_enabled": "1",       # BOSS 平台获取开关（默认开；关掉后一键获取/定时只跑猎聘）
    "liepin_enabled": "0",     # 猎聘平台获取开关（默认关，登录一次后可开）
    "liepin_pages": "2",       # 猎聘每关键词×城市翻页数（DOM抓取，页数宜少）
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
    stage TEXT,                              -- 求职阶段：interview面试/offer已录取/rejected挂了（NULL=无）
    stage_at TEXT,                           -- 标记阶段时间
    stage_note TEXT,                         -- 阶段备注（如"10-25 二面"）
    boss_title TEXT,                         -- HR 职位（人事经理/技术总监…）
    brand_stage TEXT,                        -- 融资阶段（未融资/A轮/已上市…）
    job_labels TEXT,                         -- 接口原样标签（1-3年,大专）
    days_per_week TEXT,                      -- 实习到岗天数（兼职/实习岗）
    least_month TEXT,                        -- 最短实习月数
    proxy_job INTEGER DEFAULT 0,             -- 是否代招（代招/派遣岗，慎重投递）
    gold_hunter INTEGER DEFAULT 0,           -- 是否猎头发布
    encrypt_brand_id TEXT,                   -- 公司加密ID（同公司聚合用）
    boss_avatar TEXT,                        -- HR 头像 URL
    source TEXT DEFAULT 'boss'               -- 数据来源平台：boss / liepin
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT, finished_at TEXT, trigger TEXT,
    keywords TEXT, cities TEXT, pages INTEGER,
    status TEXT DEFAULT 'running',
    fetched_count INTEGER DEFAULT 0,
    new_count INTEGER DEFAULT 0,
    offline_count INTEGER DEFAULT 0,
    source TEXT DEFAULT 'boss',
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

# 求职阶段：在「已回复」之后的手动流转，供漏斗看板与日报跟进用
STAGES = ("interview", "offer", "rejected")
STAGE_LABELS = {"interview": "面试", "offer": "Offer", "rejected": "未通过"}

# 跨平台同岗合并：去掉公司名/岗位名里的通用噪声词再拼键，
# 让「瀚天天成电子科技(厦门)股份有限公司」与「瀚天天成电子科技」、
# 「数据分析师」与「数据分析（急聘）」落到同一个 key。
_CO_NOISE = ("股份有限公司", "有限责任公司", "有限公司", "股份", "集团", "控股",
             "科技", "网络", "信息", "技术", "实业", "电子", "（", "）", "(", ")", "·", " ")
_JB_NOISE = ("急聘", "急招", "高薪", "双休", "包吃住", "五险一金", "长期有效",
             "（", "）", "(", ")", "【", "】", "/", "-", "—", " ", "　", "·")


def dedupe_key(company, job_name):
    """同岗合并键：公司名 + 岗位名归一（供 GROUP BY 用）。

    括号里的内容（分公司/门店/城市分支、「急聘」这类前缀）整段丢掉，
    否则「甲公司(厦门)」和「甲公司」会算出两个键。
    """
    def clean(s, noise):
        s = re.sub(r"[（(][^）)]*[）)]", "", s or "")   # 去括号及内容
        s = s.lower()
        for n in noise:
            s = s.replace(n, "")
        return s
    co = clean(company, _CO_NOISE)
    nm = clean(job_name, _JB_NOISE)
    return (co + "|" + nm) if (co or nm) else ""


COLS = ["job_id", "lid", "security_id", "keyword", "city_code", "city_name", "job_name",
        "salary", "company", "scale", "industry", "degree", "experience", "skills",
        "welfare", "boss_name", "area_district", "business_district", "lng", "lat",
        "job_url", "detail_text", "salary_min", "salary_max",
        "first_run_id", "last_run_id", "first_seen",
        "last_seen", "is_new", "status",
        "boss_title", "brand_stage", "job_labels",
        "days_per_week", "least_month", "proxy_job", "gold_hunter",
        "encrypt_brand_id", "boss_avatar", "source", "dedupe_key"]

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
    detail_text=COALESCE(NULLIF(excluded.detail_text,''),jobs.detail_text),
    dedupe_key=excluded.dedupe_key,
    lat=CASE WHEN excluded.lat<>'' THEN excluded.lat ELSE jobs.lat END,
    lng=CASE WHEN excluded.lng<>'' THEN excluded.lng ELSE jobs.lng END,
    coord_approx=CASE WHEN excluded.lat<>'' THEN 0 ELSE COALESCE(jobs.coord_approx,0) END,
    area_district=CASE WHEN excluded.area_district<>'' THEN excluded.area_district ELSE jobs.area_district END,
    welfare=CASE WHEN excluded.welfare<>'' THEN excluded.welfare ELSE jobs.welfare END
"""


def conn():
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def _migrate(c):
    """列级增量迁移：旧库自动补新列，不丢数据"""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
    added = set()
    for col, typ in [("salary_min", "INTEGER"), ("salary_max", "INTEGER"),
                     ("applied", "INTEGER DEFAULT 0"), ("applied_at", "TEXT"),
                     ("starred", "INTEGER DEFAULT 0"), ("note", "TEXT"),
                     ("replied", "INTEGER DEFAULT 0"), ("reply_note", "TEXT"),
                     ("reply_at", "TEXT"), ("match_score", "INTEGER DEFAULT 0"),
                     ("boss_title", "TEXT"),
                     ("brand_stage", "TEXT"), ("job_labels", "TEXT"),
                     ("days_per_week", "TEXT"), ("least_month", "TEXT"),
                     ("proxy_job", "INTEGER DEFAULT 0"), ("gold_hunter", "INTEGER DEFAULT 0"),
                     ("encrypt_brand_id", "TEXT"), ("boss_avatar", "TEXT"),
                     ("stage", "TEXT"), ("stage_at", "TEXT"), ("stage_note", "TEXT"),
                     ("source", "TEXT DEFAULT 'boss'"),
                     # 地图：坐标是否为「区级近似」（猎聘没有坐标，用同区 BOSS 岗位中位点推断）
                     ("coord_approx", "INTEGER DEFAULT 0"),
                     # 跟进日程：下次面试/沟通时间与备注
                     ("next_at", "TEXT"), ("next_note", "TEXT"),
                     # 跨平台同岗合并键（公司名+岗位名归一后）
                     ("dedupe_key", "TEXT")]:
        if col not in cols:
            c.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
            added.add(col)
    # source 是后加列：存量行补默认值（ALTER 加的列带 DEFAULT 只对新行生效）
    c.execute("UPDATE jobs SET source='boss' WHERE source IS NULL")
    if "dedupe_key" in added:   # 首次加列：给存量行算一遍，否则前端「同岗合并」全是空键
        for r in c.execute("SELECT job_id, company, job_name FROM jobs").fetchall():
            c.execute("UPDATE jobs SET dedupe_key=? WHERE job_id=?",
                      (dedupe_key(r["company"], r["job_name"]), r["job_id"]))
    # 归一算法升级（v2 起连括号内容一起丢）：版本不一致就整体重算一次
    try:
        ver = c.execute("SELECT value FROM config WHERE key='dedupe_key_ver'").fetchone()
        if not ver or ver["value"] != "2":
            for r in c.execute("SELECT job_id, company, job_name FROM jobs").fetchall():
                c.execute("UPDATE jobs SET dedupe_key=? WHERE job_id=?",
                          (dedupe_key(r["company"], r["job_name"]), r["job_id"]))
            c.execute("INSERT INTO config(key,value) VALUES('dedupe_key_ver','2') "
                      "ON CONFLICT(key) DO UPDATE SET value='2'")
    except Exception:
        pass
    rcols = {r["name"] for r in c.execute("PRAGMA table_info(runs)")}
    if "source" not in rcols:
        c.execute("ALTER TABLE runs ADD COLUMN source TEXT DEFAULT 'boss'")
    c.execute("UPDATE runs SET source='boss' WHERE source IS NULL")
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


def create_run(keywords, cities, pages, trigger, source="boss") -> int:
    with _lock:
        c = conn()
        cur = c.execute(
            "INSERT INTO runs(started_at,trigger,keywords,cities,pages,source) VALUES(?,?,?,?,?,?)",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), trigger, keywords, cities, pages, source))
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
                j.get("source") or "boss",
                j.get("dedupe_key") or dedupe_key(j.get("company"), j.get("job_name")),
            ]
            c.execute(UPSERT_SQL, params)
        c.commit()
        c.close()
    return new_cnt, seen


def mark_offline(covered_pairs, run_job_ids, source="boss"):
    """本次健康覆盖的 (keyword, city_code) 范围内：
    - 未再出现的 active 岗位 → 标记下线
    - 范围内本轮未触及的岗位 → 清掉 is_new（新增=本轮首次出现）
    source 隔离：猎聘轮的下线判定不会误伤 BOSS 的行（反之亦然）"""
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
                f"WHERE keyword=? AND city_code=? AND source=? AND status='active' AND job_id NOT IN ({placeholders})",
                [kw, cc, source] + ids)
            offline += cur.rowcount
            c.execute(
                f"UPDATE jobs SET is_new=0 "
                f"WHERE keyword=? AND city_code=? AND source=? AND is_new=1 AND job_id NOT IN ({placeholders})",
                [kw, cc, source] + ids)
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
    "stage_desc": "CASE stage WHEN 'offer' THEN 3 WHEN 'interview' THEN 2 WHEN 'rejected' THEN 1 ELSE 0 END DESC, "
                  "COALESCE(stage_at,'') DESC",
}


def _to_int(v):
    try:
        return int(str(v).strip())
    except Exception:
        return None


def _build_where(filters):
    where, params = [], []
    src = str(filters.get("source") or "").strip().lower()
    if src in ("boss", "liepin"):
        where.append("source=?")
        params.append(src)
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
    stg = str(filters.get("stage") or "").strip().lower()
    if stg in STAGES:
        where.append("stage=?")
        params.append(stg)
    elif stg == "none":       # 只看未进入任何阶段
        where.append("stage IS NULL")
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
    _migrate(c)      # dedupe_key / coord_approx 等新列兜底
    ws, params = _build_where(filters)
    order = _SORTABLE.get(filters.get("sort") or "default", _SORTABLE["salary_desc"])
    size = min(int(filters.get("size") or 50), 200)
    page = max(1, int(filters.get("page") or 1))
    if str(filters.get("dedupe") or "") == "1":
        # 同公司同岗位合并：用归一后的 dedupe_key，跨平台不同写法（有限公司/（急聘））也能合上
        dk = "COALESCE(NULLIF(dedupe_key,''), company || '|' || job_name)"
        base = (f"SELECT *, COUNT(*) OVER (PARTITION BY {dk}) AS dup_count, "
                f"ROW_NUMBER() OVER (PARTITION BY {dk} ORDER BY {order}) AS _rn "
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


def counts(source=""):
    """轻量计数（列表刷新用，替代拉全量 stats）。source='' 全部 / 'boss' / 'liepin'"""
    c = conn()
    src = (source or "").strip()
    base = "WHERE source=?" if src else "WHERE 1=1"
    bp = (src,) if src else ()
    one = lambda s, p=(): c.execute(s, bp + tuple(p)).fetchone()[0]  # noqa: E731
    out = {
        "total": one(f"SELECT COUNT(*) FROM jobs {base}"),
        "active": one(f"SELECT COUNT(*) FROM jobs {base} AND status='active'"),
        "offline": one(f"SELECT COUNT(*) FROM jobs {base} AND status='offline'"),
        "new": one(f"SELECT COUNT(*) FROM jobs {base} AND is_new=1"),
        "applied": one(f"SELECT COUNT(*) FROM jobs {base} AND applied=1"),
        "salary_missing": one(f"SELECT COUNT(*) FROM jobs {base} "
                              "AND (salary_min IS NULL OR salary_min<=0)"),
        "desc_done": one(f"SELECT COUNT(*) FROM jobs {base} "
                         "AND detail_text IS NOT NULL AND detail_text<>''"),
        "starred": one(f"SELECT COUNT(*) FROM jobs {base} AND starred=1"),
        "replied": one(f"SELECT COUNT(*) FROM jobs {base} AND replied=1"),
        "interview": one(f"SELECT COUNT(*) FROM jobs {base} AND stage='interview'"),
        "offer": one(f"SELECT COUNT(*) FROM jobs {base} AND stage='offer'"),
        "rejected": one(f"SELECT COUNT(*) FROM jobs {base} AND stage='rejected'"),
        "overdue": one(
            f"SELECT COUNT(*) FROM jobs {base} AND applied=1 "
            "AND COALESCE(replied,0)=0 AND status='active' "
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


def set_stage(job_id, stage, note=""):
    """标记/流转求职阶段。stage ∈ interview/offer/rejected；传空=清除阶段标记。"""
    from datetime import datetime as _dt
    stage = str(stage or "").strip().lower()
    if stage and stage not in STAGES:
        return 0
    with _lock:
        c = conn()
        if stage:
            cur = c.execute(
                "UPDATE jobs SET stage=?, stage_at=?, stage_note=? WHERE job_id=?",
                (stage, _dt.now().strftime("%Y-%m-%d %H:%M:%S"), (note or "")[:300], job_id))
        else:
            cur = c.execute(
                "UPDATE jobs SET stage=NULL, stage_at=NULL, stage_note='' WHERE job_id=?", (job_id,))
        c.commit()
        c.close()
        return cur.rowcount


def get_staged(limit=500):
    """按阶段分组返回面试/Offer/未通过岗位（阶段跟进页 + 漏斗看板用）"""
    c = conn()
    rows = c.execute(
        "SELECT job_id, job_name, company, salary, city_name, job_url, status, replied, "
        "reply_at, stage, stage_at, stage_note, match_score, applied, applied_at, note "
        "FROM jobs WHERE stage IS NOT NULL "
        "ORDER BY CASE stage WHEN 'offer' THEN 0 WHEN 'interview' THEN 1 ELSE 2 END, "
        "COALESCE(stage_at,'') DESC LIMIT ?", (limit,)).fetchall()
    c.close()
    out = {"interview": [], "offer": [], "rejected": []}
    for r in rows:
        d = dict(r)
        out.setdefault(d["stage"], []).append(d)
    return out


def get_replied(limit=200, source=""):
    """已回复岗位列表（按回复时间倒序，前端「HR 回复」页用）

    回复状态只有 BOSS 能自动同步；猎聘行只能靠手动标记，所以带上 source 让前端区分。
    """
    src = (source or "").strip()
    cond = " AND source=?" if src else ""
    params = ((src,) if src else ())
    c = conn()
    rows = c.execute(
        "SELECT job_id, job_name, company, salary, salary_min, salary_max, city_name, area_district, "
        "COALESCE(source,'boss') source, "
        "job_url, status, applied, applied_at, replied, reply_note, reply_at, note, starred, "
        "match_score, first_seen, stage, stage_at, stage_note FROM jobs WHERE replied=1" + cond +
        " ORDER BY COALESCE(reply_at,'') DESC, COALESCE(match_score,0) DESC LIMIT ?",
        params + (limit,)).fetchall()
    stats = c.execute(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) applied, "
        "SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) active "
        "FROM jobs WHERE replied=1" + cond, params).fetchone()
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
        "SELECT job_id, job_name, skills, welfare, detail_text, industry, salary_min, salary_max, "
        "degree, city_name FROM jobs"
    ).fetchall()
    upd = []
    for r in rows:
        # 猎聘搜索卡片没有 skills/welfare/detail，只能靠职位名 + 行业命中技能；
        # 全部拼进同一个可匹配文本，两个平台用同一套规则，分数才可比。
        hay = " ".join([str(r["job_name"] or ""), str(r["skills"] or ""),
                        str(r["detail_text"] or ""), str(r["welfare"] or ""),
                        str(r["industry"] or "")]).lower()
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


def set_detail(job_id, text, welfare=None, area=None):
    """回填职位描述（可顺带补福利标签与区县）"""
    sets, params = ["detail_text=?"], [text or ""]
    if welfare:
        sets.append("welfare=?")
        params.append(welfare)
    if area:
        sets.append("area_district=?")
        params.append(area)
    params.append(job_id)
    with _lock:
        c = conn()
        cur = c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", params)
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


def _cfg_key(base, source):
    """平台隔离的配置键：boss 沿用无前缀旧键（存量行为零变化），其他平台加 _{source} 后缀"""
    return base if (source or "boss") == "boss" else f"{base}_{source}"


def set_risk(code, msg="", source="boss"):
    """记录一次风控命中并开启冷却窗口，返回冷却信息"""
    from datetime import timedelta
    cfg = get_config()
    k = lambda b: _cfg_key(b, source)  # noqa: E731
    hits = int(cfg.get(k("risk_hits")) or 0) + 1
    minutes = _COOLDOWN_STEPS[min(hits - 1, len(_COOLDOWN_STEPS) - 1)]
    now = datetime.now()
    until = now + timedelta(minutes=minutes)
    set_config({
        k("risk_code"): str(code), k("risk_msg"): str(msg or "")[:200],
        k("risk_hits"): str(hits),
        k("risk_at"): now.strftime("%Y-%m-%d %H:%M:%S"),
        k("risk_until"): until.strftime("%Y-%m-%d %H:%M:%S"),
    })
    return get_risk(source)


def get_risk(source="boss"):
    cfg = get_config()
    k = lambda b: _cfg_key(b, source)  # noqa: E731
    until_s = (cfg.get(k("risk_until")) or "").strip()
    info = {"active": False, "code": cfg.get(k("risk_code")) or "", "msg": cfg.get(k("risk_msg")) or "",
            "hits": int(cfg.get(k("risk_hits")) or 0), "at": cfg.get(k("risk_at")) or "",
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


def clear_risk(source="boss"):
    k = lambda b: _cfg_key(b, source)  # noqa: E731
    set_config({k("risk_until"): "", k("risk_code"): "", k("risk_msg"): "", k("risk_hits"): "0"})


def bump_quota(limit=150, source="boss"):
    """当日接口请求计数（每翻一页 +1）。返回 (used, limit, allowed)。
    跨天自动归零。预算用完不是风控，只表示今天别再请求了。"""
    today = datetime.now().strftime("%Y-%m-%d")
    cfg = get_config()
    k = lambda b: _cfg_key(b, source)  # noqa: E731
    if (cfg.get(k("quota_date")) or "") != today:
        used = 0
    else:
        used = int(cfg.get(k("quota_used")) or 0)
    used += 1
    set_config({k("quota_date"): today, k("quota_used"): str(used)})
    return used, limit, used <= limit


def get_quota(limit=150, source="boss"):
    cfg = get_config()
    k = lambda b: _cfg_key(b, source)  # noqa: E731
    today = datetime.now().strftime("%Y-%m-%d")
    used = int(cfg.get(k("quota_used")) or 0) if (cfg.get(k("quota_date")) or "") == today else 0
    return {"date": today, "used": used, "limit": limit,
            "left": max(0, limit - used), "pct": round(used * 100.0 / limit, 1) if limit else 0}


def stats(days=14, source=""):
    """看板聚合数据（一次查询多组，避免前端多次请求）

    source='' 全部 / 'boss' / 'liepin'：所有指标都按平台过滤，避免两平台数据混在一起看。
    """
    from datetime import date, timedelta
    c = conn()
    src = (source or "").strip()
    base = "WHERE source=?" if src else "WHERE 1=1"
    bp = (src,) if src else ()

    def g(sql, p=()):
        return c.execute(sql, bp + tuple(p)).fetchone()[0]

    out = {
        "total": g(f"SELECT COUNT(*) FROM jobs {base}"),
        "active": g(f"SELECT COUNT(*) FROM jobs {base} AND status='active'"),
        "offline": g(f"SELECT COUNT(*) FROM jobs {base} AND status='offline'"),
        "new_today": g(f"SELECT COUNT(*) FROM jobs {base} AND substr(first_seen,1,10)=?",
                       (date.today().isoformat(),)),
        "applied": g(f"SELECT COUNT(*) FROM jobs {base} AND applied=1"),
        "apply_fail": g(f"SELECT COUNT(*) FROM jobs {base} AND applied=2"),
        "salary_known": g(f"SELECT COUNT(*) FROM jobs {base} "
                          "AND salary_min IS NOT NULL AND salary_min>0"),
        "desc_done": g(f"SELECT COUNT(*) FROM jobs {base} "
                       "AND detail_text IS NOT NULL AND detail_text<>''"),
    }
    out["salary_missing"] = out["total"] - out["salary_known"]
    # 平台维度计数（前端筛选器显示各平台条数，不随当前筛选变化）
    out["by_source"] = {r["source"] or "boss": r["n"] for r in c.execute(
        "SELECT COALESCE(source,'boss') source, COUNT(*) n FROM jobs GROUP BY source")}
    avg = c.execute(f"SELECT AVG((salary_min+salary_max)/2.0) FROM jobs {base} "
                    "AND salary_min IS NOT NULL AND salary_max IS NOT NULL", bp).fetchone()[0]
    out["avg_salary"] = round(avg, 1) if avg else None
    out["apply_rate"] = round(out["applied"] * 100.0 / out["active"], 1) if out["active"] else 0
    out["desc_rate"] = round(out["desc_done"] * 100.0 / out["total"], 1) if out["total"] else 0

    # 近 7 天 vs 前 7 天（看板环比）
    d7 = (date.today() - timedelta(days=6)).isoformat()
    d14 = (date.today() - timedelta(days=13)).isoformat()
    out["new_7d"] = g(f"SELECT COUNT(*) FROM jobs {base} AND substr(first_seen,1,10)>=?", (d7,))
    out["new_prev_7d"] = g(f"SELECT COUNT(*) FROM jobs {base} AND substr(first_seen,1,10)>=? "
                           "AND substr(first_seen,1,10)<?", (d14, d7))
    prev = out["new_prev_7d"]
    out["new_delta"] = round((out["new_7d"] - prev) * 100.0 / prev, 1) if prev else None

    # 近 N 天新增趋势
    start = (date.today() - timedelta(days=days - 1)).isoformat()
    rows = c.execute(f"SELECT substr(first_seen,1,10) d, COUNT(*) n FROM jobs {base} "
                     "AND substr(first_seen,1,10)>=? GROUP BY d", bp + (start,)).fetchall()
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
    for r in c.execute(f"SELECT salary_min FROM jobs {base} "
                       "AND salary_min IS NOT NULL AND salary_min>0", bp):
        v = r["salary_min"]
        for i, (lo, hi, _lb) in enumerate(buckets):
            if lo <= v < hi:
                salary[i]["n"] += 1
                break
    out["salary_dist"] = salary

    def top(col, limit=8, skip_empty=True):
        cond = f"{col} IS NOT NULL AND {col}<>''" if skip_empty else "1=1"
        return [{"label": r[0], "n": r[1]} for r in c.execute(
            f"SELECT {col} label, COUNT(*) n FROM jobs {base} AND {cond} "
            f"GROUP BY {col} ORDER BY n DESC LIMIT ?", bp + (limit,))]
    out["degrees"] = top("degree", 6)
    out["experiences"] = top("experience", 6)
    out["scales"] = top("scale", 6)
    out["cities"] = top("city_name", 8)
    out["industries"] = top("industry", 8)
    out["keywords"] = top("keyword", 8)
    out["stages"] = top("brand_stage", 6)
    out["boss_titles"] = top("boss_title", 8)
    out["proxy"] = g(f"SELECT COUNT(*) FROM jobs {base} "
                     "AND status='active' AND COALESCE(proxy_job,0)=1")
    c.close()
    return out


def map_points(source=""):
    """内置地图用：在招岗位的坐标点位（含薪资/HR在线状态）

    source='' 全部 / 'boss' / 'liepin'。注意：猎聘搜索卡片本身不带经纬度，
    所以 source='liepin' 时通常为空，前端会显示相应说明。
    """
    c = conn()
    src = (source or "").strip()
    cond = " AND source=?" if src else ""
    rows = c.execute(
        "SELECT job_id, job_name, company, salary, salary_min, salary_max, city_name, area_district, "
        "COALESCE(source,'boss') source, COALESCE(coord_approx,0) coord_approx, "
        "business_district, lng, lat, boss_name, boss_title, status, applied, match_score, job_url "
        "FROM jobs WHERE lng IS NOT NULL AND lng<>'' AND lat IS NOT NULL AND lat<>''" + cond +
        " ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END, "
        "COALESCE(match_score,0) DESC LIMIT 1500",
        ((src,) if src else ())).fetchall()

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


def no_coord_stats():
    """各平台缺坐标的岗位数（前端地图提示用）"""
    with _lock:
        c = conn()
        rows = c.execute(
            "SELECT COALESCE(source,'boss') source, COUNT(*) n FROM jobs "
            "WHERE lng IS NULL OR lng='' OR lat IS NULL OR lat='' GROUP BY source").fetchall()
        c.close()
    return {r["source"]: r["n"] for r in rows}


def fill_approx_coords(source="liepin"):
    """给没有坐标的岗位补「区级近似坐标」。

    猎聘搜索卡片与详情页都不带经纬度，所以用**同城同区的 BOSS 岗位坐标中位数**推断，
    写入 coord_approx=1，前端会明确标注「区级近似」，不假装是精确位置。
    同区没有样本时退回同城中位数。返回 {district, city} 命中数。
    """
    from collections import defaultdict
    with _lock:
        c = conn()
        _migrate(c)
        rows = c.execute(
            "SELECT city_name, area_district, lng, lat FROM jobs "
            "WHERE COALESCE(source,'boss')='boss' AND lng<>'' AND lat<>'' "
            "AND COALESCE(coord_approx,0)=0").fetchall()

        def norm_area(city, area):
            area = (area or "").strip()
            if city and area.startswith(city + "-"):
                area = area[len(city) + 1:]
            return "" if area == (city or "") else area

        d_pts = defaultdict(list)
        c_pts = defaultdict(list)
        for r in rows:
            try:
                lng, lat = float(r["lng"]), float(r["lat"])
            except (TypeError, ValueError):
                continue
            key = (r["city_name"] or "", norm_area(r["city_name"], r["area_district"]))
            if key[1]:
                d_pts[key].append((lng, lat))
            if key[0]:
                c_pts[key[0]].append((lng, lat))

        def med(pts):
            lngs = sorted(p[0] for p in pts)
            lats = sorted(p[1] for p in pts)
            return lngs[len(lngs) // 2], lats[len(lats) // 2]

        targets = c.execute(
            "SELECT job_id, city_name, area_district FROM jobs "
            "WHERE COALESCE(source,'boss')=? AND (lng IS NULL OR lng='' OR lat IS NULL OR lat='')",
            (source or "liepin",)).fetchall()
        n_d = n_c = 0
        for t in targets:
            key = (t["city_name"] or "", norm_area(t["city_name"], t["area_district"]))
            if key in d_pts and d_pts[key]:
                lng, lat = med(d_pts[key])
                n_d += 1
            elif key[0] in c_pts and c_pts[key[0]]:
                lng, lat = med(c_pts[key[0]])
                n_c += 1
            else:
                continue
            c.execute("UPDATE jobs SET lng=?, lat=?, coord_approx=1 WHERE job_id=?",
                      (f"{lng:.6f}", f"{lat:.6f}", t["job_id"]))
        c.commit()
        c.close()
    return {"district": n_d, "city": n_c}


def skill_gap(limit=18, source="", min_jobs=3):
    """技能缺口：统计在招岗位里各技能出现频次，对比我的画像，给出「岗位要但我没有」的排序。

    岗位技能文本 = 岗位名 + 技能标签 + 福利 + JD 正文，英文技能走词边界匹配（照搬 resume._has）。
    """
    import resume as resume_mod
    prof = get_profile()
    mine = {str(s).strip().lower() for s in (prof.get("skills") or []) if str(s).strip()}
    src = (source or "").strip()
    cond = " AND source=?" if src else ""
    c = conn()
    rows = c.execute(
        "SELECT job_name, skills, welfare, detail_text FROM jobs WHERE status='active'" + cond,
        ((src,) if src else ())).fetchall()
    c.close()
    total = len(rows)
    if not total:
        return {"total": 0, "have": [], "gap": [], "coverage": 0}
    hits = {name: 0 for name, _al, _w in resume_mod.SKILL_DICT}
    for r in rows:
        hay = " ".join([str(r["job_name"] or ""), str(r["skills"] or ""),
                        str(r["welfare"] or ""), str(r["detail_text"] or "")]).lower()
        for name, aliases, _w in resume_mod.SKILL_DICT:
            if any(resume_mod._has(a, hay) for a in aliases):
                hits[name] += 1
    have, gap = [], []
    for name, _al, weight in resume_mod.SKILL_DICT:
        n = hits[name]
        if n < min_jobs:
            continue
        item = {"skill": name, "jobs": n,
                "pct": round(n * 100.0 / total, 1), "weight": weight,
                "mine": name.lower() in mine}
        (have if item["mine"] else gap).append(item)
    have.sort(key=lambda x: -x["jobs"])
    gap.sort(key=lambda x: -x["jobs"])
    covered = round(sum(1 for x in have) * 100.0 / max(1, len(have) + len(gap)), 1)
    return {"total": total, "have": have[:limit], "gap": gap[:limit], "coverage": covered}


def conversion(by="source", source="", limit=12):
    """投递转化分析：按维度分组看 岗位数 / 已投 / 已回复 / 面试 / Offer 与转化率"""
    dims = {
        "source": "COALESCE(source,'boss')",
        "scale": "CASE WHEN COALESCE(scale,'')='' THEN '未知' ELSE scale END",
        "keyword": "CASE WHEN COALESCE(keyword,'')='' THEN '未知' ELSE keyword END",
        "city": "CASE WHEN COALESCE(city_name,'')='' THEN '未知' ELSE city_name END",
        "degree": "CASE WHEN COALESCE(degree,'')='' THEN '未知' ELSE degree END",
    }
    col = dims.get(by) or dims["source"]
    src = (source or "").strip()
    cond = " AND source=?" if src else ""
    c = conn()
    rows = c.execute(
        f"SELECT {col} AS label, COUNT(*) total, "
        "SUM(CASE WHEN applied=1 THEN 1 ELSE 0 END) applied, "
        "SUM(CASE WHEN replied=1 THEN 1 ELSE 0 END) replied, "
        "SUM(CASE WHEN stage='interview' THEN 1 ELSE 0 END) interview, "
        "SUM(CASE WHEN stage='offer' THEN 1 ELSE 0 END) offered, "
        "SUM(CASE WHEN stage='rejected' THEN 1 ELSE 0 END) rejected "
        f"FROM jobs WHERE 1=1{cond} GROUP BY label ORDER BY total DESC LIMIT ?",
        (((src,) if src else ()) + (limit,))).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        d["reply_rate"] = round((d["replied"] or 0) * 100.0 / d["applied"], 1) if d["applied"] else 0
        d["interview_rate"] = round((d["interview"] or 0) * 100.0 / d["replied"], 1) if d["replied"] else 0
        out.append(d)
    return {"by": by, "rows": out}


def schedule(days=14, include_past=False):
    """跟进日程：next_at 在 [今天, 今天+days] 内的岗位（面试/沟通时间）"""
    from datetime import date, timedelta
    today = date.today()
    start = today.isoformat() if not include_past else "1970-01-01"
    end = (today + timedelta(days=max(1, days))).isoformat()
    c = conn()
    _migrate(c)      # next_at 是新列，老库首次用到时补上
    rows = c.execute(
        "SELECT job_id, job_name, company, city_name, salary, COALESCE(source,'boss') source, "
        "next_at, next_note, stage, status, job_url, boss_name, applied "
        "FROM jobs WHERE COALESCE(next_at,'')<>'' AND substr(next_at,1,10) BETWEEN ? AND ? "
        "ORDER BY next_at ASC LIMIT 200", (start, end)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def set_next_at(job_id, at, note=""):
    """设置跟进时间（空字符串=清除）"""
    with _lock:
        c = conn()
        _migrate(c)
        cur = c.execute("UPDATE jobs SET next_at=?, next_note=? WHERE job_id=?",
                        ((at or "").strip(), (note or "").strip()[:200], job_id))
        c.commit()
        c.close()
    return cur.rowcount


def bulk_update(job_ids, action, value=None):
    """批量改状态：star=1/0、stage=interview/offer/rejected/''、replied=1/0"""
    ids = [i for i in (job_ids or []) if i]
    if not ids:
        return 0
    fields = {"star": "starred", "stage": "stage", "replied": "replied"}
    col = fields.get(action)
    if not col:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ph = ",".join("?" * len(ids))
    with _lock:
        c = conn()
        _migrate(c)
        if action == "stage":
            v = (value or "").strip().lower()
            if v and v not in STAGES:
                c.close()
                return 0
            cur = c.execute(f"UPDATE jobs SET {col}=?, stage_at=? WHERE job_id IN ({ph})",
                            [v, now] + ids)
        elif action == "replied":
            cur = c.execute(f"UPDATE jobs SET {col}=?, reply_at=? WHERE job_id IN ({ph})",
                            [1 if str(value) in ("1", "true", "True") else 0, now] + ids)
        else:
            cur = c.execute(f"UPDATE jobs SET {col}=? WHERE job_id IN ({ph})",
                            [1 if str(value) in ("1", "true", "True") else 0] + ids)
        c.commit()
        n = cur.rowcount
        c.close()
    return n


def backup(keep=7):
    """把当前库备份到 data/backup/，只保留最近 keep 份（防误删/写坏）。

    用 sqlite3 的在线备份 API 而不是直接复制文件，避免刚好在写入时拷到半个库。
    """
    src = DB_PATH
    if not os.path.exists(src):
        return ""
    bdir = os.path.join(DATA_DIR, "backup")
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, f"jobs_{datetime.now():%Y%m%d_%H%M%S}.db")
    try:
        s = sqlite3.connect(src, timeout=30)
        d = sqlite3.connect(dst)
        with d:
            s.backup(d)
        d.close()
        s.close()
    except Exception:
        try:
            os.path.exists(dst) and os.remove(dst)
        except OSError:
            pass
        return ""
    files = sorted(f for f in os.listdir(bdir) if f.startswith("jobs_") and f.endswith(".db"))
    for f in files[:-keep]:
        try:
            os.remove(os.path.join(bdir, f))
        except OSError:
            pass
    return dst


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
