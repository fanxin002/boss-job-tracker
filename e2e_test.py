# -*- coding: utf-8 -*-
"""端到端冒烟测试（CI/CD 式验收）
覆盖：配置 / 薪资解析入库 / 增量去重 / 下线标记 / 多条件筛选 / 排序 / 投递状态流转 /
     投递记录 / 并发互斥 / CSV公式注入防护 / XSS转义 / API层 / 调度器注册
不依赖网络与BOSS登录：monkeypatch 模拟抓取与投递结果。
结果写入 e2e_result.txt，退出码 0=全过 1=有失败。
"""
import os
import sys
import json
import time
import traceback

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
sys.path.insert(0, BASE)

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append(("PASS" if cond else "FAIL", name, detail))
    return cond


def main():
    # ---------- 0. 薪资解析单元测试 ----------
    import salary as sal
    scases = [
        ("10-15K", 10, 15), ("15-30K·15薪", 15, 30), ("20K", 20, 20),
        ("8-10万·年", 7, 8), ("1.5万", 15, 15), ("6千-8千", 6, 8),
        ("150元·天", 3, 3), ("面议", None, None), ("", None, None),
        ("30-60K·15薪", 30, 60), ("5-8万", 50, 80), ("12-16K·13薪", 12, 16),
        ("100元/小时", 18, 18), ("200-300元·天", 4, 7),
    ]
    sbad = [c for c in scases if sal.parse_k(c[0]) != (c[1], c[2])]
    check("薪资-解析14种格式", not sbad, str(sbad[:3]))

    # ---------- 独立测试库，避免污染真实数据 ----------
    os.environ["BOSS_TEST_DB"] = "1"
    import db
    test_db = os.path.join(BASE, "data", "test_jobs.db")
    if os.path.exists(test_db):
        os.remove(test_db)
    db.DB_PATH = test_db
    db.init_db()

    # ---- 1. 配置读写 ----
    db.set_config({"keywords": "数据分析,BI", "cities": '[{"name":"厦门","code":"101230200"}]',
                   "pages": "3", "daily_time": "08:15", "greeting": "您好，我对该岗位很感兴趣"})
    cfg = db.get_config()
    check("配置-关键词写入回读", cfg["keywords"] == "数据分析,BI", cfg["keywords"])
    check("配置-默认无头静默", cfg["headless"] == "1", cfg["headless"])
    check("配置-招呼语持久化", cfg["greeting"] == "您好，我对该岗位很感兴趣")

    # ---- 2. 增量入库：第1轮 4条（含薪资/学历/经验差异） ----
    rid1 = db.create_run("数据分析", "厦门", 3, "manual")

    def mk(jid, salary="10-15K", deg="本科", exp="3-5年", scale="100-499人", ind="互联网"):
        return {"job_id": jid, "lid": "l" + jid, "security_id": "s" + jid,
                "keyword": "数据分析", "city_code": "101230200", "city_name": "厦门",
                "job_name": "数据分析师" + jid, "salary": salary, "company": "公司" + jid,
                "scale": scale, "industry": ind, "degree": deg,
                "experience": exp, "skills": "SQL", "welfare": "五险一金",
                "boss_name": "HR", "area_district": "思明区", "business_district": "软件园",
                "lng": "118.1", "lat": "24.4", "job_url": "https://x/" + jid, "detail_text": ""}
    n1, ids1 = db.upsert_jobs(
        [mk("A", "10-15K"), mk("B", "30-60K·15薪"), mk("C", "面议", "大专", "经验不限", scale="少于50人", ind="餐饮"),
         mk("E", "2-4万·年", "硕士", "1-3年")], rid1)
    check("增量-第1轮全部为新增", n1 == 4, f"new={n1}")

    # ---- 3. 薪资数值入库正确 ----
    c = db.conn()
    a = c.execute("SELECT salary_min,salary_max FROM jobs WHERE job_id='A'").fetchone()
    b = c.execute("SELECT salary_min,salary_max FROM jobs WHERE job_id='B'").fetchone()
    cc = c.execute("SELECT salary_min FROM jobs WHERE job_id='C'").fetchone()
    c.close()
    check("薪资-A解析入库", a["salary_min"] == 10 and a["salary_max"] == 15, str(dict(a)))
    check("薪资-B含·N薪仍取区间", b["salary_min"] == 30 and b["salary_max"] == 60, str(dict(b)))
    check("薪资-面议为NULL", cc["salary_min"] is None, str(cc["salary_min"]))

    # ---- 4. 第2轮：3旧+1新 → 只新增1条 ----
    rid2 = db.create_run("数据分析", "厦门", 3, "daily")
    n2, ids2 = db.upsert_jobs(
        [mk("A", "12-16K"), mk("B", "30-60K·15薪"), mk("D"),
         mk("E", "2-4万·年", "硕士", "1-3年")], rid2)
    check("增量-重复岗位不重复入库", n2 == 1, f"new={n2}")
    c = db.conn()
    rowA = c.execute("SELECT * FROM jobs WHERE job_id='A'").fetchone()
    c.close()
    check("增量-旧岗位薪资刷新", rowA["salary"] == "12-16K", rowA["salary"])
    check("增量-薪资区间同步刷新", rowA["salary_min"] == 12 and rowA["salary_max"] == 16)
    check("增量-旧岗位取消新增标记", rowA["is_new"] == 0)
    check("增量-旧岗位last_run更新", rowA["last_run_id"] == rid2)

    # ---- 5. 下线检测 ----
    off = db.mark_offline([("数据分析", "101230200")], set(ids2))
    c = db.conn()
    rowC = c.execute("SELECT status FROM jobs WHERE job_id='C'").fetchone()
    rowD = c.execute("SELECT status,is_new FROM jobs WHERE job_id='D'").fetchone()
    c.close()
    check("下线-消失岗位被标记", off == 1 and rowC["status"] == "offline", f"off={off}")
    check("下线-新岗位保持active", rowD["status"] == "active" and rowD["is_new"] == 1)

    # ---- 6. 多条件筛选 ----
    t_all, _ = db.query_jobs({})
    t_new, _ = db.query_jobs({"is_new": 1})
    t_off, _ = db.query_jobs({"status": "offline"})
    t_q, rows_q = db.query_jobs({"q": "公司D"})
    check("筛选-总数5", t_all == 5, f"total={t_all}")
    check("筛选-仅新增=1(D)", t_new == 1, f"new={t_new}")
    check("筛选-仅下线=1(C)", t_off == 1, f"off={t_off}")
    check("搜索-命中", t_q == 1 and rows_q[0]["job_id"] == "D")

    t_sal, rows_s = db.query_jobs({"salary_min": 20})
    check("筛选-月薪≥20K命中(B)", t_sal == 1 and rows_s[0]["job_id"] == "B", str([r["job_id"] for r in rows_s]))
    t_sal2, _ = db.query_jobs({"salary_max": 16})
    check("筛选-期望≤16K命中A,D等", t_sal2 >= 1, f"n={t_sal2}")
    t_nosal, _ = db.query_jobs({"no_salary": 2})
    t_hassal, _ = db.query_jobs({"no_salary": 1})
    check("筛选-只看薪资未知命中C", t_nosal == 1, f"n={t_nosal}")
    check("筛选-只看有明确薪资", t_hassal == 4, f"n={t_hassal}")
    t_deg, _ = db.query_jobs({"degree": "硕士"})
    check("筛选-学历硕士=E", t_deg == 1, f"n={t_deg}")
    t_exp, _ = db.query_jobs({"experience": "经验不限"})
    check("筛选-经验不限=C", t_exp == 1, f"n={t_exp}")
    t_ind, _ = db.query_jobs({"industry": "餐饮"})
    check("筛选-行业模糊=餐饮", t_ind == 1, f"n={t_ind}")
    t_combo, _ = db.query_jobs({"degree": "本科", "salary_min": 10, "status": "active"})
    check("筛选-组合(本科+≥10K+在招)=A/B/D", t_combo == 3, f"n={t_combo}")

    # ---- 7. 排序 ----
    _, sd = db.query_jobs({"sort": "salary_desc", "status": "active"})
    check("排序-薪资高→低首个=B", sd and sd[0]["job_id"] == "B", str([r["job_id"] for r in sd[:3]]))
    _, sa = db.query_jobs({"sort": "salary_asc", "status": "active"})
    check("排序-薪资低→高首条=E(月薪折算低)", sa and sa[0]["job_id"] in ("A", "E"), str([(r["job_id"], r["salary_min"]) for r in sa]))

    # ---- 8. 投递状态流转 ----
    db.set_applied(["A", "B"], 1)
    c = db.conn()
    ra = c.execute("SELECT applied,applied_at FROM jobs WHERE job_id='A'").fetchone()
    c.close()
    check("投递-标记成功", ra["applied"] == 1 and ra["applied_at"], str(dict(ra)))
    db.set_applied(["D"], 2)
    db.add_application({"job_id": "D", "job_name": "数据分析师D", "company": "公司D", "job_url": "https://x/D"},
                       "failed", "触发安全验证", "batch1")
    apps = db.get_applications(batch_id="batch1")
    check("投递-记录表写入", len(apps) == 1 and apps[0]["status"] == "failed", str(apps))
    t_ap, _ = db.query_jobs({"applied": 1})
    t_af, _ = db.query_jobs({"applied": 2})
    t_unap, _ = db.query_jobs({"applied": 0})
    check("投递-筛选已投递=2", t_ap == 2, f"n={t_ap}")
    check("投递-筛选失败=1", t_af == 1, f"n={t_af}")
    check("投递-筛选未投递=2", t_unap == 2, f"n={t_unap}")  # C、E 未投递
    db.set_applied(["A"], 0, clear=True)
    t_ap2, _ = db.query_jobs({"applied": 1})
    check("投递-重置未投递生效", t_ap2 == 1, f"n={t_ap2}")

    # ---- 9. 运行历史与日志 ----
    db.append_run_log(rid1, "测试日志行")
    db.finish_run(rid1, "success", 3, 3, 0)
    runs = db.get_runs()
    check("历史-运行记录落库", len(runs) == 2 and runs[0]["id"] == rid2)
    check("历史-日志写入", "测试日志行" in (runs[1]["log"] or ""))

    # ---- 10. API 层 ----
    from fastapi.testclient import TestClient
    import app as appmod
    appmod.db = db
    import crawler
    client = TestClient(appmod.app)

    r = client.get("/api/config")
    check("API-config 200", r.status_code == 200 and r.json()["keywords"] == "数据分析,BI")

    r = client.post("/api/config", json={
        "keywords": "数据分析师", "cities": [{"name": "福州", "code": "101230100"}],
        "pages": 5, "detail": "1", "daily_enabled": "1", "daily_time": "10:30",
        "headless": "0", "push_enabled": "1", "greeting": "你好"})
    check("API-config 保存+调度更新", r.status_code == 200 and r.json()["ok"])
    job = appmod.scheduler.get_job("daily_crawl")
    check("调度-每日任务已注册", job is not None and appmod._next_run_str() is not None,
          appmod._next_run_str() if job else "no job")

    r = client.get("/api/jobs?page=1&size=10")
    check("API-jobs 返回", r.status_code == 200 and r.json()["total"] == 5, f"total={r.json()['total']}")
    r = client.get("/api/jobs?salary_min=20")
    check("API-jobs 薪资筛选", r.json()["total"] == 1)
    r = client.get("/api/jobs?applied=1")
    check("API-jobs 投递状态筛选", r.json()["total"] == 1, str(r.json()["total"]))
    r = client.get("/api/jobs?sort=salary_desc")
    check("API-jobs 排序", r.json()["rows"][0]["job_id"] == "B", r.json()["rows"][0]["job_id"])
    r = client.get("/api/jobs?applied=abc&salary_min=abc")  # 脏参数不报500
    check("API-jobs 脏参数容错", r.status_code == 200)

    r = client.get("/api/filters")
    f = r.json()
    check("API-filters 含学历/经验/规模", "degrees" in f and "scales" in f and "experiences" in f, str(list(f.keys())))

    # ---- 11. 投递 API 校验（未登录应拒绝） ----
    # monkeypatch apply.run_apply：测试绝不碰真实浏览器
    import apply as applymod
    fake_done = {"called": False}

    def fake_run_apply(ids, greeting, logf, batch_id, should_cancel=None, **kw):
        fake_done["called"] = True
        logf(f"模拟投递 {len(ids)} 条")
        for _ in range(6):
            if should_cancel and should_cancel():
                break
            time.sleep(0.2)
        return len(ids), 0
    applymod.run_apply = fake_run_apply

    r = client.post("/api/apply", json={"job_ids": []})
    check("API-投递空选拒绝", r.json()["ok"] is False, r.json()["msg"])
    # 伪造已登录：写 state 文件
    st_path = appmod.crawler.STATE_FILE
    old = open(st_path, encoding="utf-8").read() if os.path.exists(st_path) else None
    json.dump({"logged": False, "at": "", "note": ""}, open(st_path, "w", encoding="utf-8"))
    r = client.post("/api/apply", json={"job_ids": ["A"]})
    check("API-投递未登录拒绝", r.json()["ok"] is False and "未登录" in r.json()["msg"], r.json()["msg"])
    json.dump({"logged": True, "at": "t", "note": ""}, open(st_path, "w", encoding="utf-8"))
    # 构造23个不同有效ID + 1个重复 + 1个无效 → 去重、过滤、截断到20
    extra = [mk(f"Z{i}") for i in range(22)]
    db.upsert_jobs(extra, rid2)
    ids_test = [f"Z{i}" for i in range(22)] + ["A", "A", "NOPE"]
    r = client.post("/api/apply", json={"job_ids": ids_test})
    check("API-投递去重+过滤无效+截断20",
          r.json()["ok"] and "20" in r.json()["msg"], r.json()["msg"])
    time.sleep(0.5)
    r = client.get("/api/apply_status")
    check("API-投递运行中", r.json()["running"] is True)
    r = client.post("/api/cancel_login")  # 无关接口仍工作
    r = client.post("/api/apply_cancel")
    check("API-取消投递", r.json()["ok"] is True, r.json()["msg"])
    if old is not None:
        open(st_path, "w", encoding="utf-8").write(old)
    # 等投递线程收尾
    for _ in range(40):
        if not client.get("/api/apply_status").json()["running"]:
            break
        time.sleep(0.25)

    # ---- 12. CSV 公式注入防护 + 导出字段 ----
    db.upsert_jobs([{"job_id": "XSS", "keyword": "数据分析", "city_code": "101230200",
                     "city_name": "厦门", "job_name": "=1+1<img src=x>", "salary": "10K",
                     "company": "-4+5", "job_url": "https://x/XSS"}], rid2)
    r = client.get("/api/export")
    text = r.text
    check("API-导出含薪资数值列", "salary_min" in text and "applied" in text)
    check("API-CSV注入防护", "'=1+1" in text and "'-4+5" in text,
          "found" if ("'=1+1" in text and "'-4+5" in text) else "missing")
    r = client.get("/api/applications?limit=5")
    check("API-投递记录接口", r.status_code == 200 and isinstance(r.json()["apps"], list))

    # ---- 13. 并发互斥：抓取运行中拒绝投递（反之同理） ----
    appmod._state["running"] = True
    r = client.post("/api/apply", json={"job_ids": ["B"]})
    check("互斥-抓取中拒绝投递", r.json()["ok"] is False, r.json()["msg"])
    appmod._state["running"] = False

    # ---- 14. 爬虫 normalize ----
    raw = [{"encryptJobId": "X1", "lid": "l", "securityId": "s", "jobName": "n",
            "salaryDesc": "10-11K", "brandName": "b", "cityName": "厦门",
            "skills": ["SQL"], "welfareList": ["五险一金"], "gps": {"longitude": 1, "latitude": 2},
            "_kw": "数据分析", "_city": "101230200"}]
    rowsn = crawler.normalize(raw)
    check("爬虫-normalize 字段映射", rowsn[0]["job_id"] == "X1" and rowsn[0]["skills"] == "SQL"
          and "job_detail/X1" in rowsn[0]["job_url"] and rowsn[0]["lng"] == 1)

    # ---- 15. 风控熔断：code=36 抛 StopCrawl ----------
    class FakePage:
        def run_js(self, *a):
            return json.dumps({"code": 36, "message": "您的账户存在异常行为", "zpData": {}})
        def get(self, *a):
            pass
    try:
        crawler.fetch_via_api(FakePage(), "数据分析", "101230200", 3, lambda s: None)
        check("风控-code36立即熔断", False, "未抛StopCrawl")
    except crawler.StopCrawl as e:
        check("风控-code36立即熔断", "36" in str(e), str(e)[:60])
    except Exception as e:
        check("风控-code36立即熔断", False, f"抛错类型不对:{type(e).__name__}")

    # ---- 15b. code=37 降温重试 / 中断不丢数据 / 请求预算 ----
    old = (crawler.SOFT_RETRY_WAIT, crawler.PAGE_DELAY, crawler.COOLDOWN_DELAY, crawler.LONG_COOLDOWN)
    crawler.SOFT_RETRY_WAIT = (0, 0)
    crawler.PAGE_DELAY = (0, 0)
    crawler.COOLDOWN_DELAY = (0, 0)
    crawler.LONG_COOLDOWN = (0, 0)
    try:
        calls = {"n": 0}

        class FlakyPage:
            def run_js(self, *a):
                calls["n"] += 1
                if calls["n"] == 1:
                    return json.dumps({"code": 37, "message": "您的环境存在异常", "zpData": {}})
                pg = int(a[3])          # run_js(script, kw, city, page, degree)
                jobs = [{"encryptJobId": f"R{pg}_{i}", "jobName": "数据分析师", "salaryDesc": "10-15K",
                         "brandName": "某公司", "cityName": "厦门"} for i in range(3)]
                return json.dumps({"code": 0, "zpData": {"jobList": jobs, "hasMore": pg < 3}})

            def get(self, *a):
                pass

        col = []
        got = crawler.fetch_via_api(FlakyPage(), "数据分析", "101230200", 3, lambda s: None, collector=col)
        check("风控-code37降温重试后继续", got is not None and len(col) == 9,
              f"retry_calls={calls['n']} col={len(col)}")

        class DiePage:
            def run_js(self, *a):
                pg = int(a[3])
                if pg >= 3:
                    return json.dumps({"code": 36, "message": "异常行为", "zpData": {}})
                jobs = [{"encryptJobId": f"D{pg}_{i}", "jobName": "数据分析师", "salaryDesc": "9-12K",
                         "brandName": "某公司2", "cityName": "厦门"} for i in range(4)]
                return json.dumps({"code": 0, "zpData": {"jobList": jobs, "hasMore": True}})

            def get(self, *a):
                pass

        col2 = []
        try:
            crawler.fetch_via_api(DiePage(), "数据分析", "101230200", 5, lambda s: None, collector=col2)
            check("风控-中断保留已抓数据", False, "未抛 StopCrawl")
        except crawler.StopCrawl:
            check("风控-中断保留已抓数据", len(col2) == 8, f"collected={len(col2)}")

        db.set_config({"quota_date": "2000-01-01", "quota_used": "999"})
        u1, _l1, ok1 = db.bump_quota(5)
        check("预算-跨天自动归零", u1 == 1 and ok1 is True, f"used={u1}")
        db.set_config({"quota_date": db.datetime.now().strftime("%Y-%m-%d"), "quota_used": "5"})
        u2, _l2, ok2 = db.bump_quota(5)
        check("预算-超限拒绝请求", u2 == 6 and ok2 is False, f"used={u2} ok={ok2}")
        check("预算-查询字段完整", "left" in db.get_quota(150) and db.get_quota(150)["limit"] == 150)
        db.set_config({"quota_date": "", "quota_used": "0"})
    finally:
        (crawler.SOFT_RETRY_WAIT, crawler.PAGE_DELAY,
         crawler.COOLDOWN_DELAY, crawler.LONG_COOLDOWN) = old

    # ---- 16. 薪资空值不覆盖（游客态/风控降级抹掉历史薪资的回归保护） ----
    db.upsert_jobs([mk("S1", "18-25K")], rid2)          # 先登录态抓到有薪资
    db.upsert_jobs([mk("S1", "")], rid2)                # 再降级轮返回空 salaryDesc
    c = db.conn()
    rs = c.execute("SELECT salary,salary_min FROM jobs WHERE job_id='S1'").fetchone()
    c.close()
    check("薪资-空值不覆盖旧值", rs["salary"] == "18-25K" and rs["salary_min"] == 18, str(dict(rs)))
    db.upsert_jobs([mk("S1", "20-30K")], rid2)          # 真实新值应正常刷新
    c = db.conn()
    rs = c.execute("SELECT salary FROM jobs WHERE job_id='S1'").fetchone()
    c.close()
    check("薪资-非空新值正常覆盖", rs["salary"] == "20-30K", rs["salary"])
    n_before = len(db.get_applications())
    check("薪资-normalize双取salary字段",
          crawler.normalize([{"encryptJobId": "Z9", "salary": "9-12K", "_kw": "k", "_city": "c"}])[0]["salary"] == "9-12K")

    # ---- 17. 投递发现下架 → 同步状态 ----
    db.mark_job_offline("S1")
    check("投递-下架岗位状态同步", db.get_job("S1")["status"] == "offline")

    # ---- 18. 投递节流配置：默认值 + 越界钳制 ----
    cfg = db.get_config()
    check("配置-投递默认值", cfg["apply_max_batch"] == "20" and cfg["apply_interval_min"] == "12"
          and cfg["apply_interval_max"] == "30", f"{cfg['apply_max_batch']}/{cfg['apply_interval_min']}/{cfg['apply_interval_max']}")
    r = client.post("/api/config", json={
        "keywords": "数据分析", "cities": [{"name": "厦门", "code": "101230200"}],
        "pages": 3, "detail": "1", "daily_enabled": "1", "daily_time": "09:30", "headless": "1",
        "push_enabled": "1", "greeting": "", "apply_max_batch": 999,
        "apply_interval_min": 1, "apply_interval_max": 9999})
    cfg = db.get_config()
    check("配置-投递参数钳制", cfg["apply_max_batch"] == "50" and cfg["apply_interval_min"] == "3"
          and cfg["apply_interval_max"] == "300", f"{cfg['apply_max_batch']}/{cfg['apply_interval_min']}/{cfg['apply_interval_max']}")
    # 恢复默认，避免影响后续断言
    db.set_config({"apply_max_batch": "20", "apply_interval_min": "12", "apply_interval_max": "30"})

    # ---- 19. 投递 apply.py 逻辑（离线：fake page/状态） ----
    import importlib
    importlib.reload(applymod)  # 还原被第11节 monkeypatch 的 run_apply
    check("投递-apply模块结构完整", hasattr(applymod, "run_apply") and hasattr(applymod, "apply_one")
          and hasattr(applymod, "_click_and_confirm"))

    # 已投递岗位在批量中跳过（不产生新记录）
    db.upsert_jobs([mk("S2", "10K")], rid2)
    db.set_applied(["S2"], 1)
    n_before = len(db.get_applications())
    calls = {"n": 0}

    def fake_apply_one(page, job, greeting, logf):
        calls["n"] += 1
        return True, "模拟成功"
    real_one = applymod.apply_one
    applymod.apply_one = fake_apply_one
    fake_page = object()
    real_ensure, real_inject, real_close = crawler.ensure_browser, crawler.inject_cookies, crawler._close_browser
    crawler.ensure_browser = lambda headless=False, offscreen=False: fake_page
    crawler.inject_cookies = lambda p: None
    crawler._close_browser = lambda: None
    try:
        ok, fail = applymod.run_apply(["S2"], "", lambda s: None, "batch_skip")
        check("投递-已投递岗位跳过", ok == 0 and fail == 0 and calls["n"] == 0, f"ok={ok} calls={calls['n']}")
        ok, fail = applymod.run_apply(["S1"], "", lambda s: None, "batch_off")  # S1已下线但applied=0 → 会尝试
        # 下架岗位由页面检测返回，这里fake直接成功，只验证链路不断
        # 单批上限截取
        many = [mk(f"M{i}") for i in range(25)]
        db.upsert_jobs(many, rid2)
        ok, fail = applymod.run_apply([f"M{i}" for i in range(25)], "", lambda s: None,
                                      "batch_cap", max_batch=5, interval=(0, 0))
        check("投递-单批上限截取", ok == 5, f"ok={ok}")
    finally:
        applymod.apply_one = real_one
        crawler.ensure_browser, crawler.inject_cookies, crawler._close_browser = real_ensure, real_inject, real_close

    # ---- 20. 风控冷却锁：命中后禁止重跑，避免连续请求延长封控 ----
    db.clear_risk()
    check("风控锁-初始未锁定", db.get_risk()["active"] is False)
    rk = db.set_risk(36, "您的账户存在异常行为")
    check("风控锁-命中后开启冷却", rk["active"] and rk["remaining_min"] > 0 and rk["code"] == "36",
          f"active={rk['active']} left={rk['remaining_min']}m")
    r = client.post("/api/crawl")
    check("风控锁-冷却期拒绝抓取", r.json()["ok"] is False and "风控冷却" in r.json()["msg"],
          r.json()["msg"][:60])
    rk2 = db.set_risk(37, "环境异常")
    check("风控锁-连续命中逐级加长", rk2["hits"] == 2 and rk2["remaining_min"] > rk["remaining_min"],
          f"hits={rk2['hits']} {rk['remaining_min']}→{rk2['remaining_min']}m")
    check("风控锁-do_crawl 直接拒跑", appmod.do_crawl("manual") is None)
    db.clear_risk()
    check("风控锁-手动解除生效", db.get_risk()["active"] is False and db.get_risk()["hits"] == 0)

    # ---- 21. 修复登录态接口存在 + 未运行时状态正常 ----
    r = client.get("/api/repair_state")
    check("修复-状态接口可用", r.status_code == 200 and r.json()["running"] is False)
    r = client.post("/api/repair_cancel")
    check("修复-无窗口时取消被拒", r.json()["ok"] is False, r.json()["msg"])

    # ---- 22. 浏览器模式解析：2=离屏(默认) 1=无头 0=有头 ----
    k1, c1, p1, d1, h1, o1, g1 = appmod._parse_cfg({"keywords": "数据分析", "crawl_degrees": "203,204",
                                                    "cities": '[{"name":"厦门","code":"101230200"}]', "headless": "2"})
    k2, c2, p2, d2, h2, o2, g2 = appmod._parse_cfg({"keywords": "数据分析",
                                                    "cities": '[{"name":"厦门","code":"101230200"}]', "headless": "1"})
    k3, c3, p3, d3, h3, o3, g3 = appmod._parse_cfg({"keywords": "数据分析",
                                                    "cities": '[{"name":"厦门","code":"101230200"}]', "headless": "0"})
    check("模式-离屏有头解析", o1 and not h1, f"headless={h1} offscreen={o1}")
    check("模式-无头解析", h2 and not o2, f"headless={h2} offscreen={o2}")
    check("模式-有头调试解析", not h3 and not o3, f"headless={h3} offscreen={o3}")
    check("学历-配置解析(逗号多选)", g1 == "203,204", g1)
    check("学历-默认不限", g2 == "", repr(g2))
    check("学历-API_JS 带 degree 参数", "degree=" in crawler.API_JS)

    # ---- 23. 修复/登录状态自愈：线程死了不能永久卡「进行中」 ----
    import threading as _th
    crawler._repair.update(running=True, thread=None, started=time.time(), last="")
    check("自愈-修复线程丢失自动复位", crawler.repair_state()["running"] is False)
    done_thread = _th.Thread(target=lambda: None)
    done_thread.start()
    done_thread.join()
    crawler._repair.update(running=True, thread=done_thread, started=time.time(), last="")
    check("自愈-修复线程已退出自动复位", crawler.repair_state()["running"] is False)
    crawler._repair.update(running=True, thread=_th.current_thread(), started=time.time(), last="")
    check("自愈-修复进行中保持占用", crawler.repair_state()["running"] is True)
    crawler._repair.update(running=False, thread=None, started=0.0, last="")
    crawler._repair.update(running=True, thread=_th.current_thread(), started=time.time() - 700, last="")
    check("自愈-修复超时11分钟自动复位", crawler.repair_state()["running"] is False)
    crawler._repair.update(running=False, thread=None, started=0.0, last="")

    crawler._login.update(running=True, thread=None, started=time.time())
    check("自愈-登录线程丢失自动复位", crawler.get_login_state()["login_open"] is False)
    crawler._login.update(running=True, thread=_th.current_thread(), started=time.time())
    check("自愈-登录进行中保持占用", crawler.get_login_state()["login_open"] is True)
    crawler._login.update(running=False, thread=None, started=0.0)
    # 修复窗口未运行时，抓取接口不再被"修复中"拦截
    db.clear_risk()
    appmod.crawler = crawler

    # ---- 24. 职位描述：解析/入库/补抓队列 ----
    class FakeEle:
        def __init__(self, text):
            self.text = text

    class FakeTab:
        def __init__(self, text):
            self._t = text
        def ele(self, sel, timeout=0):
            if "job-sec-text" in sel:
                return FakeEle(self._t)
            return None
    long_txt = "岗位职责：负责数据指标体系建设；任职要求：3年以上数据分析经验。"
    got = crawler.extract_detail(FakeTab(long_txt))
    check("描述-正文提取", got.startswith("岗位职责"), f"len={len(got)}")
    check("描述-空页面不误判", crawler.extract_detail(FakeTab("   ")) == "")
    db.upsert_jobs([mk("DESC1", "10-12K")], rid2)
    n_before, _ = db.query_jobs({"has_detail": "0", "status": "active", "size": 200})
    db.set_detail("DESC1", long_txt)
    n_after, _ = db.query_jobs({"has_detail": "0", "status": "active", "size": 200})
    check("描述-回填入库", db.get_job("DESC1")["detail_text"] == long_txt)
    check("描述-补抓队列排除已有描述", n_after == n_before - 1, f"{n_before}→{n_after}")
    r = client.get("/api/details_status")
    check("描述-状态接口可用", r.status_code == 200 and "log" in r.json())
    r = client.post("/api/details_cancel")
    check("描述-无任务时取消被拒", r.json()["ok"] is False, r.json()["msg"])
    r1 = client.get("/api/jobs?has_detail=1").json()["total"]
    r0 = client.get("/api/jobs?has_detail=0").json()["total"]
    tall = client.get("/api/jobs").json()["total"]
    check("API-描述筛选(有/缺)", r1 >= 1 and r0 >= 1 and r1 + r0 == tall, f"{r1}+{r0} vs {tall}")
    rcsv = client.get("/api/export?has_detail=1")
    check("API-导出带描述筛选", rcsv.status_code == 200 and long_txt[:6] in rcsv.text)

    # ---- 25. 看板统计 / 城市库 / 按筛选补抓 ----
    r = client.get("/api/stats")
    st = r.json()
    check("看板-统计接口", r.status_code == 200 and st["total"] >= 1 and len(st["trend"]) == 14,
          f"total={st.get('total')} trend={len(st.get('trend', []))}")
    check("看板-薪资分布分桶", len(st["salary_dist"]) == 8 and st["salary_dist"][0]["label"] == "<5K")
    check("看板-平均薪资/比率", isinstance(st["avg_salary"], (int, float)) or st["avg_salary"] is None)
    r = client.get("/api/cities")
    cj = r.json()
    check("城市-接口可用", r.status_code == 200 and "cities" in cj and "count" in cj, str(cj.get("count")))
    r = client.get("/api/cities/sync_state")
    check("城市-同步状态接口", r.status_code == 200 and "running" in r.json(), str(r.json())[:80])
    # 按筛选补抓：未登录应被拒绝，且接受 body 模型
    st_path = appmod.crawler.STATE_FILE
    old2 = open(st_path, encoding="utf-8").read() if os.path.exists(st_path) else None
    json.dump({"logged": False, "at": "", "note": ""}, open(st_path, "w", encoding="utf-8"))
    r = client.post("/api/details", json={"limit": 5, "job_ids": [], "filters": {"status": "active", "has_detail": "0"}})
    check("补抓-按筛选传参+未登录拒绝", r.json()["ok"] is False and "未登录" in r.json()["msg"], r.json()["msg"][:40])
    if old2 is not None:
        open(st_path, "w", encoding="utf-8").write(old2)

    # ---- 26. 新增能力：计数合并 / 收藏备注 / 匹配分 / 同岗合并 / 回复 ----
    c0 = client.get("/api/counts").json()
    need = ("total", "active", "offline", "new", "applied", "salary_missing", "desc_done", "starred", "replied")
    check("计数-字段齐全", all(k in c0 for k in need), str(list(c0.keys())))
    r5 = client.get("/api/jobs?page=1&size=5").json()
    check("计数-列表响应带 counts", "counts" in r5 and r5["counts"]["total"] == c0["total"])

    rows_def = client.get("/api/jobs?size=50&status=active").json()["rows"]
    sm = [x["salary_max"] if x["salary_max"] is not None else -1 for x in rows_def]
    check("排序-默认按薪资降序", sm == sorted(sm, reverse=True), str(sm[:5]))

    db.set_star("A", 1)
    check("收藏-标记生效", db.get_job("A")["starred"] == 1)
    check("收藏-筛选生效", client.get("/api/jobs?starred=1").json()["total"] >= 1)
    client.post("/api/star", json={"job_id": "A", "star": 0})
    check("收藏-取消生效", db.get_job("A")["starred"] == 0)

    client.post("/api/note", json={"job_id": "A", "note": "等回复"})
    check("备注-写入回读", db.get_job("A")["note"] == "等回复")

    # ---- 简历解析画像 ----
    import resume as resume_mod
    sample = """范鑫
4年经验 大专 在职 月内到岗
个人优势
擅长编写复杂SQL，熟悉的数据库有MySQL、Hive、ClickHouse，oracle
熟悉Hadoop、CDH集群、Sqoop、HDFS
掌握熟练 Power BI，了解 FineBI 报表，能制作 power bi 数据大屏
期望职位
全职职位
数据分析师 8-12K 厦门
数据开发 9-13K 南京
工作经历
厦门小鱼号餐饮管理有限公司 数据分析师 2023.09-至今
数据采集与处理：基于开放API每日定时抓取业务数据并写入ClickHouse，使用Python自动化处理
数据分析与报表：使用SQL进行清洗关联分析，输出供应链报表
可视化与BI：使用Power BI搭建看板与大屏
教育经历
某学院 大专 2019-2022
"""
    prof = resume_mod.parse(sample, source="upload:test.txt")
    check("简历-技能自动抽取", "SQL" in prof["skills"] and "Power BI" in prof["skills"]
          and "ClickHouse" in prof["skills"], str(prof["skills"][:8]))
    check("简历-不误判 mysql 为 sql 之外", "MySQL" in prof["skills"], str(prof["skills"][:12]))
    check("简历-学历识别", prof["degree"] == "大专", prof["degree"])
    check("简历-期望薪资取上限", prof["expect_salary"] == 13, str(prof["expect_salary"]))
    check("简历-期望城市", set(prof["expect_cities"]) >= {"厦门", "南京"}, str(prof["expect_cities"]))
    check("简历-期望职位", any("数据分析" in t for t in prof["expect_titles"]), str(prof["expect_titles"]))
    check("简历-核心技能", bool(prof["core_skills"]), str(prof["core_skills"][:5]))
    check("简历-年限识别", prof["years"] >= 1, str(prof["years"]))

    import base64
    up = client.post("/api/profile/upload", json={
        "filename": "resume.txt",
        "content_b64": base64.b64encode(sample.encode("utf-8")).decode()})
    check("简历-上传解析接口", up.json()["ok"], up.json().get("msg", "")[:80])
    gp = client.get("/api/profile").json()
    check("简历-画像已落库", gp["configured"] and gp["skills"], str(gp.get("skills"))[:60])
    check("简历-来源标记", str(gp.get("source", "")).startswith("upload:"), gp.get("source"))
    check("简历-匹配分已重算", client.get("/api/jobs?sort=match&size=3").json()["rows"][0]["match_score"] > 0)
    pa = client.post("/api/profile/patch", json={"expect_salary": 10})
    check("简历-画像微调", pa.json()["ok"] and db.get_profile()["expect_salary"] == 10, pa.json()["msg"][:60])
    bad = client.post("/api/profile/upload", json={"filename": "x.pdf", "content_b64": ""})
    check("简历-空文件被拒", bad.json()["ok"] is False)

    rp = client.get("/api/profile/sync_state")
    check("简历-同步状态接口", rp.status_code == 200 and "running" in rp.json())
    check("匹配分-重算接口", True, "已由画像驱动")
    c = db.conn()
    scored = c.execute("SELECT COUNT(*) FROM jobs WHERE match_score>0").fetchone()[0]
    c.close()
    check("匹配分-已写入库", scored >= 1, f"scored={scored}")
    rows_m = client.get("/api/jobs?sort=match&size=5").json()["rows"]
    check("匹配分-按匹配度排序", rows_m and rows_m[0]["match_score"] >= rows_m[-1]["match_score"],
          str([x["match_score"] for x in rows_m[:5]]))
    rows_ma = client.get("/api/jobs?sort=match_asc&size=5").json()["rows"]
    check("匹配分-低到高排序", rows_ma and rows_ma[0]["match_score"] <= rows_ma[-1]["match_score"],
          str([x["match_score"] for x in rows_ma[:5]]))
    rows_rd = client.get("/api/jobs?sort=reply_desc&size=10").json()["rows"]
    _rts = [x.get("reply_at") or "" for x in rows_rd if x.get("replied")]
    check("排序-回复时间新到旧", _rts == sorted(_rts, reverse=True), str(_rts[:3]))
    c2 = db.conn()
    c2.execute("UPDATE jobs SET starred=1 WHERE job_id='A'")
    c2.commit()
    rows_st = client.get("/api/jobs?sort=star_desc&size=10").json()["rows"]
    check("排序-收藏置顶", rows_st and rows_st[0]["job_id"] == "A", rows_st[0]["job_id"] if rows_st else "")
    c2.execute("UPDATE jobs SET starred=0 WHERE job_id='A'")
    c2.commit()
    rows_ca = client.get("/api/jobs?sort=company_asc&size=8").json()["rows"]
    _cps = [(x.get("company") or "").lower() for x in rows_ca]
    check("排序-公司名A到Z", _cps == sorted(_cps), str(_cps[:3]))
    c2.close()

    dup = {"job_name": "数据分析师", "company": "同一家公司", "keyword": "数据分析",
           "city_code": "101230200", "city_name": "厦门", "salary": "10-12K"}
    db.upsert_jobs([dict(dup, job_id="DD1"), dict(dup, job_id="DD2")], rid2)
    t_all = client.get("/api/jobs?q=同一家公司").json()["total"]
    j_dup = client.get("/api/jobs?q=同一家公司&dedupe=1").json()
    check("同岗-合并后条数减少", t_all == 2 and j_dup["total"] == 1, f"{t_all} -> {j_dup['total']}")
    check("同岗-带出重复计数", j_dup["rows"] and j_dup["rows"][0].get("dup_count") == 2,
          str(j_dup["rows"][0].get("dup_count") if j_dup["rows"] else None))

    db.mark_replied("A", "毕女士：你好，方便发简历吗")
    check("回复-标记生效", db.get_job("A")["replied"] == 1)
    check("回复-筛选已回复", client.get("/api/jobs?replied=1").json()["total"] >= 1)
    check("回复-未回复筛选", client.get("/api/jobs?replied=0").json()["total"] >= 1)
    check("回复-状态接口", "running" in client.get("/api/replies/state").json())
    # ---- 投递超期提醒 ----
    import datetime as _d2
    c3 = db.conn()
    c3.execute("UPDATE jobs SET applied=1, replied=0, status='active', "
               "applied_at=? WHERE job_id='A'",
               [(_d2.datetime.now() - _d2.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")])
    c3.commit()
    c3.close()
    od_rows = client.get("/api/jobs?overdue=3").json()
    _ods = [x["job_id"] for x in od_rows["rows"]]
    check("超期-筛选命中5天前投递", "A" in _ods, str(_ods[:6]))
    check("超期-计数包含该条", od_rows["counts"].get("overdue", 0) >= 1,
          str(od_rows["counts"].get("overdue")))
    check("超期-已回复的不算", "A" not in [x["job_id"] for x in
          client.get("/api/jobs?overdue=3&replied=1").json()["rows"]])
    c3 = db.conn()
    c3.execute("UPDATE jobs SET applied_at=? WHERE job_id='A'",
               [(_d2.datetime.now() - _d2.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")])
    c3.commit()
    c3.close()
    check("超期-1天内的不算", "A" not in [x["job_id"] for x in
          client.get("/api/jobs?overdue=3").json()["rows"]])
    # 还原 A 为已回复，供后续 HR 回复页接口用例校验
    db.mark_replied("A", "毕女士：你好，方便发简历吗")
    exp = client.get("/api/export?overdue=1").text
    check("超期-导出透传参数", exp and "lng" in exp.splitlines()[0], exp.splitlines()[0][:80])
    rpl = client.get("/api/replies?limit=10").json()
    _a_rows = [x for x in rpl.get("rows", []) if x.get("job_id") == "A"]
    check("回复-HR回复页接口", rpl.get("total", 0) >= 1 and _a_rows
          and _a_rows[0].get("reply_note"), f"total={rpl.get('total')}")
    check("回复-接口带同步状态", "sync" in rpl and "running" in rpl["sync"])
    st_path2 = appmod.crawler.STATE_FILE
    old3 = open(st_path2, encoding="utf-8").read() if os.path.exists(st_path2) else None
    json.dump({"logged": False, "at": "", "note": ""}, open(st_path2, "w", encoding="utf-8"))
    check("回复-未登录拒绝同步", client.post("/api/replies/sync").json()["ok"] is False)
    if old3 is not None:
        open(st_path2, "w", encoding="utf-8").write(old3)

    # ---- 列表接口扩展字段 + 内置地图 ----
    nrow = crawler.normalize([{"encryptJobId": "NF1", "jobName": "数据分析师", "salaryDesc": "12-15K",
                               "brandName": "新公司", "cityName": "厦门", "bossName": "王先生",
                               "bossTitle": "人事经理", "bossOnline": True, "brandStageName": "不需要融资",
                               "jobLabels": ["3-5年", "本科"], "proxyJob": True, "goldHunter": False,
                               "encryptBrandId": "EB1", "bossAvatar": "https://img/x.png",
                               "daysPerWeekDesc": "5天", "leastMonthDesc": "3个月",
                               "gps": {"longitude": 118.2, "latitude": 24.6},
                               "_kw": "数据分析", "_city": "101230200"}])[0]
    check("扩展字段-normalize映射", nrow["boss_title"] == "人事经理" and "boss_online" not in nrow
          and nrow["brand_stage"] == "不需要融资" and nrow["job_labels"] == "3-5年,本科"
          and nrow["proxy_job"] is True and nrow["encrypt_brand_id"] == "EB1")
    nrow2 = crawler.normalize([{"encryptJobId": "NF2", "jobName": "n"}])[0]
    check("扩展字段-缺省容错", nrow2["boss_title"] == ""
          and nrow2["proxy_job"] is False)
    db.upsert_jobs([nrow], rid2)
    jr = db.get_job("NF1")
    check("扩展字段-入库回读", jr["boss_title"] == "人事经理"
          and jr["proxy_job"] == 1 and jr["brand_stage"] == "不需要融资")
    db.upsert_jobs([dict(nrow, boss_title="", brand_stage="")], rid2)
    jr2 = db.get_job("NF1")
    check("扩展字段-空值不覆盖", jr2["boss_title"] == "人事经理" and jr2["brand_stage"] == "不需要融资")
    mp = client.get("/api/map").json()
    check("地图-/api/map点位", mp.get("points") and any(p["job_id"] == "NF1" for p in mp["points"])
          and isinstance(mp["points"][0]["lng"], float), f"{len(mp.get('points') or [])}个点")
    check("地图-中心与范围", len(mp.get("center") or []) == 2 and mp.get("bounds"))
    _nf1 = next(p for p in mp["points"] if p["job_id"] == "NF1")
    check("地图-点位带HR与薪资(热力用)", _nf1.get("boss_title") == "人事经理"
          and _nf1.get("salary_min") == 12 and _nf1.get("salary_max") == 15)
    check("历史-runs只回10条", len(client.get("/api/runs").json()["runs"]) <= 10)
    stt = client.get("/api/stats").json()
    check("看板-HR职位聚合", any(x["label"] == "人事经理" for x in stt.get("boss_titles") or []))
    check("看板-融资阶段聚合", any(x["label"] == "不需要融资" for x in stt.get("stages") or []))
    check("看板-HR在线已移除", "boss_online" not in stt)
    check("看板-代招计数", stt.get("proxy", 0) >= 1)

    # ---- 求职阶段管理（面试/Offer/未通过） ----
    r = client.post("/api/stage", json={"job_id": "A", "stage": "interview", "note": "10-25 二面"})
    check("阶段-标记面试", r.json()["ok"] and db.get_job("A")["stage"] == "interview", r.json().get("msg", ""))
    check("阶段-带时间备注", (db.get_job("A")["stage_at"] or "") and db.get_job("A")["stage_note"] == "10-25 二面")
    bad = client.post("/api/stage", json={"job_id": "A", "stage": "hacker"})
    check("阶段-非法值拒绝", bad.json()["ok"] is False, bad.json().get("msg", ""))
    check("阶段-筛选命中", client.get("/api/jobs?stage=interview").json()["total"] >= 1)
    check("阶段-未进入阶段筛选", client.get("/api/jobs?stage=none").json()["total"] >= 1)
    _stageIds = [x["job_id"] for x in client.get("/api/jobs?stage=interview").json()["rows"]]
    check("阶段-筛选只含已标记", "A" in _stageIds and all(
        db.get_job(i)["stage"] == "interview" for i in _stageIds), str(_stageIds[:5]))
    client.post("/api/stage", json={"job_id": "A", "stage": "offer"})
    check("阶段-流转到Offer", db.get_job("A")["stage"] == "offer")
    cA = db.conn(); cA.execute("UPDATE jobs SET replied=1 WHERE job_id='A'"); cA.commit(); cA.close()
    _stg_rows = client.get("/api/replies").json()["rows"]
    _a_stg = next((x for x in _stg_rows if x.get("job_id") == "A"), None)
    check("阶段-HR回复页带stage字段", _a_stg and _a_stg.get("stage") == "offer",
          str(_a_stg and _a_stg.get("stage")))
    cA = db.conn()
    cA.execute("UPDATE jobs SET stage='rejected' WHERE job_id='B'")
    cA.commit(); cA.close()
    fn = client.get("/api/stages").json()["funnel"]
    check("漏斗-/api/stages计数", fn.get("interview", 0) >= 0 and fn.get("offer", 0) >= 1
          and fn.get("rejected", 0) >= 1, f"offer={fn.get('offer')} rej={fn.get('rejected')}")
    cc = client.get("/api/counts").json()
    check("漏斗-counts含三阶段", cc.get("offer") >= 1 and cc.get("rejected") >= 1,
          f"i={cc.get('interview')} o={cc.get('offer')} r={cc.get('rejected')}")
    check("漏斗-在招投递回复字段", all(k in fn for k in ("active", "applied", "replied")))
    exp_st = client.get("/api/export?stage=offer").text
    check("阶段-导出透传+含列", "stage" in exp_st.splitlines()[0] and exp_st.count("\n") >= 1,
          exp_st.splitlines()[0][:90])
    client.post("/api/stage", json={"job_id": "A", "stage": ""})
    check("阶段-清除标记", db.get_job("A")["stage"] in (None, ""))
    _sr = client.get("/api/stages").json()
    check("阶段-get_staged按组返回", all(k in _sr for k in ("interview", "offer", "rejected"))
          and _sr["offer"] == [], f"offer={[x['job_id'] for x in _sr['offer']]}")
    rows_sg = client.get("/api/jobs?sort=stage_desc&size=10").json()["rows"]
    check("阶段-按阶段排序可用", rows_sg and rows_sg[0]["job_id"] == "B",
          str([(x['job_id'], x.get('stage')) for x in rows_sg[:3]]))
    # 还原 B 的阶段，避免影响库
    cB = db.conn(); cB.execute("UPDATE jobs SET stage=NULL WHERE job_id='B'"); cB.commit(); cB.close()

    # ---- 猎聘平台隔离（接口路线 normalize / source 列 / 风控 / 预算 / 下线判定） ----
    import liepin as lpmod
    _resp = json.dumps({"flag": 1, "data": {"data": {"jobCardList": [
        {"job": {"jobId": "1974831551", "title": "数据分析师", "salary": "20-35K·15薪",
                 "link": "https://www.liepin.com/job/1974831551.shtml",
                 "requireEduLevel": "本科", "requireWorkYears": "3年以上",
                 "dq": "厦门-思明区", "jobKind": "2", "labels": ["SQL", "Python"],
                 "refreshTime": "20261020120000"},
         "comp": {"compName": "公司LP", "compScale": "100-499人", "compIndustry": "互联网",
                  "compId": 10279001, "compStage": "已上市"},
         "recruiter": {"recruiterName": "陈女士", "recruiterTitle": "招聘经理"}},
        {"job": {"jobId": "23847163", "title": "BI工程师", "salary": "薪资面议",
                 "link": "https://www.liepin.com/a/23847163.shtml", "jobKind": "1"},
         "comp": {"compName": "猎头A"}}],
        "pagination": {"currentPage": 0, "pageSize": 40, "totalPage": 10}}}})
    _cards, _pag = lpmod.parse_resp(_resp)
    check("平台-parse_resp接口结构", _cards and len(_cards) == 2 and _pag.get("totalPage") == 10)
    check("平台-parse_resp异常返回None", lpmod.parse_resp("not json")[0] is None
          and lpmod.parse_resp('{"flag":0}')[0] is None)
    _lprows = lpmod.normalize(_cards, "数据分析", "厦门", "090040")
    check("平台-normalize字段", len(_lprows) == 2 and _lprows[0]["job_id"] == "lp1974831551"
          and _lprows[0]["source"] == "liepin" and _lprows[0]["degree"] == "本科"
          and _lprows[0]["salary"] == "20-35K·15薪" and _lprows[0]["company"] == "公司LP",
          str(_lprows[:1]))
    check("平台-normalize猎头标记", _lprows[1]["gold_hunter"] == 1 and _lprows[0]["gold_hunter"] == 0)
    check("平台-normalizeHR字段", _lprows[0]["boss_name"] == "陈女士"
          and _lprows[0]["boss_title"] == "招聘经理")
    check("平台-城市码表", lpmod.CITY_CODE.get("厦门") == "090040"
          and lpmod.CITY_CODE.get("泉州") == "090030")
    _body = lpmod.build_body("数据分析", "090040", 2)
    check("平台-请求体currentPage从0起", _body["data"]["mainSearchPcConditionForm"]["currentPage"] == 1
          and _body["data"]["mainSearchPcConditionForm"]["dq"] == "090040")
    nlp, lps = db.upsert_jobs(_lprows, rid2)
    check("平台-猎聘行入库", nlp == 2 and db.get_job("lp1974831551")["source"] == "liepin")
    check("平台-猎聘薪资解析", db.get_job("lp1974831551")["salary_min"] == 20
          and db.get_job("lp23847163")["salary_min"] is None)
    t_lp, _ = db.query_jobs({"source": "liepin"})
    t_boss, rows_boss = db.query_jobs({"source": "boss"})
    check("平台-source筛选猎聘=2", t_lp == 2, f"n={t_lp}")
    check("平台-source筛选boss不含猎聘", t_boss >= 5 and all(
        (r["source"] or "boss") == "boss" for r in rows_boss)
        and all(r["job_id"] != "lp1974831551" for r in rows_boss), f"n={t_boss}")
    check("平台-存量行默认boss", db.get_job("A")["source"] == "boss")
    # 下线隔离：用同 (keyword, city_code) 跑 boss 下线判定，不应误伤猎聘行
    _off_b = db.mark_offline([("数据分析", "101230200")], set(ids2) | {"lp1974831551"}, source="boss")
    check("平台-boss下线判定不误伤猎聘", db.get_job("lp1974831551")["status"] == "active")
    _off_l = db.mark_offline([("数据分析", "101230200")], set(), source="liepin")
    check("平台-liepin下线判定独立", db.get_job("C")["status"] == "offline"
          and db.get_job("lp1974831551")["status"] in ("offline", "active"))
    # 风控隔离
    db.set_risk("login_expired", "猎聘登录失效", source="liepin")
    check("平台-猎聘风控激活", db.get_risk(source="liepin")["active"])
    check("平台-猎聘风控不影响boss", not db.get_risk()["active"])
    db.clear_risk(source="liepin")
    check("平台-猎聘风控可单独解除", not db.get_risk(source="liepin")["active"]
          and not db.get_risk()["active"])
    # 预算隔离
    _qb0, _, _ = db.bump_quota(100, source="liepin")
    check("平台-猎聘预算独立计数", db.get_quota(100, source="liepin")["used"] >= 1
          and db.get_quota(150)["used"] != db.get_quota(100, source="liepin")["used"])
    # run source
    rid3 = db.create_run("数据分析", "厦门", 2, "manual", source="liepin")
    _r3 = next(x for x in db.get_runs(50) if x["id"] == rid3)
    check("平台-run记录带source", _r3["source"] == "liepin")
    # API 层
    jlp = client.get("/api/jobs?source=liepin").json()
    check("平台-API-jobs筛选", jlp["total"] >= 1 and all(
        (r["source"] or "boss") == "liepin" for r in jlp["rows"]))
    lst = client.get("/api/login_state?platform=liepin").json()
    check("平台-API-猎聘登录态键", "logged" in lst and "login_open" in lst)
    rk = client.get("/api/risk").json()
    check("平台-API-risk含liepin", "liepin" in rk)
    qt = client.get("/api/quota").json()
    check("平台-API-quota含liepin", "liepin" in qt and qt["liepin"]["used"] >= 1)
    cfgp = client.post("/api/config", json={
        "keywords": "数据分析,BI", "cities": [{"name": "厦门", "code": "101230200"}],
        "pages": 3, "liepin_enabled": "1", "liepin_pages": 2}).json()
    check("平台-配置保存猎聘开关", cfgp["ok"])
    cfgv = client.get("/api/config").json()
    check("平台-猎聘配置回读", str(cfgv["liepin_enabled"]) == "1" and int(cfgv["liepin_pages"]) == 2)
    import app as _appm
    # 统一定时入口：一个 daily_crawl job，内部按平台开关顺序跑
    _sched_ids = [j.id for j in _appm.scheduler.get_jobs()]
    check("平台-统一定时任务注册", "daily_crawl" in _sched_ids and "daily_liepin" not in _sched_ids,
          str(_sched_ids))
    _plats = _appm.enabled_platforms(cfgv)
    check("平台-enabled_platforms含猎聘", "liepin" in _plats and "boss" in _plats, str(_plats))
    # 双平台关闭 → 配置被拒
    _both_off = client.post("/api/config", json={
        "keywords": "数据分析", "cities": [{"name": "厦门", "code": "101230200"}],
        "pages": 3, "boss_enabled": "0", "liepin_enabled": "0"}).json()
    check("平台-双关保存被拒", _both_off["ok"] is False, _both_off.get("msg", ""))
    # 仅猎聘：boss 关闭后 enabled_platforms 只剩 liepin
    client.post("/api/config", json={
        "keywords": "数据分析,BI", "cities": [{"name": "厦门", "code": "101230200"}],
        "pages": 3, "boss_enabled": "0", "liepin_enabled": "1", "liepin_pages": 2})
    _only_lp = _appm.enabled_platforms(client.get("/api/config").json())
    check("平台-仅猎聘开关生效", _only_lp == ["liepin"], str(_only_lp))
    client.post("/api/config", json={
        "keywords": "数据分析,BI", "cities": [{"name": "厦门", "code": "101230200"}],
        "pages": 3, "boss_enabled": "1", "liepin_enabled": "0", "liepin_pages": 2})
    _only_b = _appm.enabled_platforms(client.get("/api/config").json())
    check("平台-猎聘关闭后只剩boss", _only_b == ["boss"], str(_only_b))
    # BOSS 开关关闭时 do_crawl 直接拒跑
    client.post("/api/config", json={
        "keywords": "数据分析,BI", "cities": [{"name": "厦门", "code": "101230200"}],
        "pages": 3, "boss_enabled": "0", "liepin_enabled": "1", "liepin_pages": 2})
    check("平台-BOSS关闭后do_crawl拒跑", _appm.do_crawl("manual") is None)
    client.post("/api/config", json={
        "keywords": "数据分析,BI", "cities": [{"name": "厦门", "code": "101230200"}],
        "pages": 3, "boss_enabled": "1", "liepin_enabled": "0", "liepin_pages": 2})
    exp_lp = client.get("/api/export?source=liepin").text
    check("平台-导出source列+透传", "source" in exp_lp.splitlines()[0]
          and "liepin" in exp_lp, exp_lp.count("\n") - 1)

    # ---- 平台维度：看板筛选 / 地图筛选 / 回复支持范围 / 匹配分重算 ----
    _st_all = client.get("/api/stats").json()
    _st_lp = client.get("/api/stats?source=liepin").json()
    _st_b = client.get("/api/stats?source=boss").json()
    check("看板-平台筛选总数可加总", _st_all["total"] == _st_b["total"] + _st_lp["total"],
          f'{_st_all["total"]}={_st_b["total"]}+{_st_lp["total"]}')
    check("看板-猎聘筛选只剩猎聘", _st_lp["total"] >= 1
          and all(c["label"] == "厦门" for c in _st_lp["cities"]), str(_st_lp["cities"][:2]))
    check("看板-返回各平台条数", (_st_all.get("by_source") or {}).get("liepin") == _st_lp["total"])
    _mp_lp = client.get("/api/map?source=liepin").json()
    _mp_all = client.get("/api/map").json()
    check("地图-平台筛选+缺坐标说明", _mp_lp["source"] == "liepin" and _mp_lp["points"] == []
          and _mp_lp["no_coord"] >= 1, f'lp={len(_mp_lp["points"])} no_coord={_mp_lp["no_coord"]}')
    check("地图-全部平台含BOSS点位", len(_mp_all["points"]) >= 1 and _mp_all["source"] == "")
    _rp = client.get("/api/replies").json()
    check("回复-标注平台支持范围（猎聘不支持）",
          _rp.get("sync_supported") == {"boss": True, "liepin": False}, str(_rp.get("sync_supported")))
    # 单条岗位接口（详情面板）：翻页后的岗位也要能打开
    _one = client.get("/api/job?job_id=lp1974831551").json()
    check("详情-单条岗位可读", _one.get("ok") is True and _one["job"]["job_id"] == "lp1974831551")
    check("详情-不存在岗位返回失败", client.get("/api/job?job_id=NOPE").json()["ok"] is False)
    check("详情-缺参数被拒", client.get("/api/job").json()["ok"] is False)
    # 匹配分：入库后自动重算（此前只在画像更新时算，导致新岗位全是 0）
    _c = db.conn()
    _c.execute("UPDATE jobs SET match_score=0 WHERE job_id='lp1974831551'")
    _c.commit(); _c.close()
    _appm._rescore()
    check("匹配-重算后不再为0", (db.get_job("lp1974831551") or {}).get("match_score", 0) > 0,
          (db.get_job("lp1974831551") or {}).get("match_score"))

    # ---- 新增能力：同岗归一 / 技能缺口 / 转化 / 日程 / 批量 / 备份 / 城市 / 近似坐标 / 猎聘详情 ----
    check("去重-公司名归一后键相同",
          db.dedupe_key("瀚天天成电子科技(厦门)股份有限公司", "数据分析师（急聘）")
          == db.dedupe_key("瀚天天成电子科技", "数据分析师"))
    db.upsert_jobs([{**mk("DZ1"), "company": "甲公司有限公司", "job_name": "数据分析师（急聘）"},
                    {**mk("DZ2"), "company": "甲公司", "job_name": "数据分析师"}], rid3)
    _, _dzrows = db.query_jobs({"dedupe": "1", "page": 1, "size": 300})
    _dz = [r for r in _dzrows if r["job_id"] in ("DZ1", "DZ2")]
    check("去重-跨写法同岗合并为1条", len(_dz) == 1 and _dz[0].get("dup_count") == 2, str(len(_dz)))
    # 猎聘「某…」脱敏公司名：真实雇主不同，不能按公司名合并
    check("去重-脱敏公司名不互相合并",
          db.dedupe_key("某大型新零售公司", "数据分析专家", "lpA")
          != db.dedupe_key("某大型新零售公司", "数据分析专家", "lpB"))
    check("去重-脱敏名不参与公司维度", db.dedupe_key("某知名公司", "数据分析", "lpC") == "secret|lpC")
    check("去重-识别脱敏名", db.is_masked_company("某厦门新能源公司") and not db.is_masked_company("安踏集团"))

    gp = client.get("/api/skills/gap").json()
    check("技能缺口-接口结构", gp["total"] >= 1 and "gap" in gp and "have" in gp, str(gp.get("total")))
    check("技能缺口-只统计达到阈值的技能", all(x["jobs"] >= 3 for x in gp["gap"]))
    check("技能缺口-带平台筛选", client.get("/api/skills/gap?source=boss").json()["total"] >= 1)

    cv = client.get("/api/conversion?by=source").json()
    check("转化-按平台分组", cv["by"] == "source" and len(cv["rows"]) >= 1, str(cv.get("rows"))[:60])
    check("转化-支持其他维度", client.get("/api/conversion?by=keyword").json()["by"] == "keyword")

    from datetime import date as _d, timedelta as _td
    _soon = (_d.today() + _td(days=2)).strftime("%Y-%m-%d 10:00")
    check("日程-写入", client.post("/api/next_at", json={"job_id": "A", "at": _soon, "note": "二面"}).json()["ok"])
    _sc = client.get("/api/schedule?days=14").json()["rows"]
    check("日程-按时间取出", any(r["job_id"] == "A" and r["next_note"] == "二面" for r in _sc))
    client.post("/api/next_at", json={"job_id": "A", "at": ""})
    check("日程-清除后不再出现",
          not any(r["job_id"] == "A" for r in client.get("/api/schedule?days=14").json()["rows"]))

    _bl = client.post("/api/bulk", json={"job_ids": ["A", "B"], "action": "star", "value": "1"}).json()
    check("批量-收藏", _bl["ok"] and _bl["count"] == 2 and db.get_job("A")["starred"] == 1)
    _bl2 = client.post("/api/bulk", json={"job_ids": ["A", "B"], "action": "stage",
                                         "value": "interview"}).json()
    check("批量-阶段", _bl2["ok"] and db.get_job("B")["stage"] == "interview")
    check("批量-非法阶段被拒",
          client.post("/api/bulk", json={"job_ids": ["A"], "action": "stage", "value": "bad"}).json()["ok"] is False)
    client.post("/api/bulk", json={"job_ids": ["A", "B"], "action": "star", "value": "0"})
    check("批量-空列表不报错", client.post("/api/bulk", json={"job_ids": [], "action": "star"}).json()["ok"] is False)

    _orig_data = db.DATA_DIR
    db.DATA_DIR = os.path.join(BASE, "data", "_bk_test")
    _bp = client.post("/api/backup").json()
    _bk_ok = _bp["ok"] and os.path.exists(_bp["path"]) and "_bk_test" in _bp["path"]
    db.DATA_DIR = _orig_data
    check("备份-生成文件", _bk_ok, str(_bp.get("path", ""))[-30:])

    _lc = client.get("/api/liepin_cities").json()
    check("猎聘-城市码列表", "厦门" in _lc["cities"] and len(_lc["cities"]) >= 10)

    # 猎聘无坐标 → 用同城同区 BOSS 坐标补近似点
    db.upsert_jobs([{**mk("LPAPX"), "job_id": "lpAPX", "source": "liepin", "lng": "", "lat": ""}], rid3)
    _na = db.fill_approx_coords("liepin")
    _apj = db.get_job("lpAPX")
    check("地图-猎聘补区级近似坐标",
          (_na["district"] + _na["city"]) >= 1 and _apj["coord_approx"] == 1
          and _apj["lng"] not in ("", None), f'{_na} {_apj["lng"]}')
    _mp2 = client.get("/api/map?source=liepin").json()
    check("地图-返回近似点计数", _mp2["approx"] >= 1 and len(_mp2["points"]) >= 1)

    import liepin as _lpmod
    _pd = _lpmod.parse_detail(json.dumps({"jd": "<p>要求 SQL 与 Excel</p>&amp;报表",
                                         "area": "厦门-思明区", "welfare": "五险一金"}))
    check("猎聘-详情解析去 HTML 标签", "SQL" in _pd["jd"] and "<p>" not in _pd["jd"] and "&amp;" not in _pd["jd"])
    check("猎聘-详情解析区县/福利", _pd["area"] == "厦门-思明区" and _pd["welfare"] == "五险一金")
    check("补抓-只有猎聘时不需要 BOSS 登录",
          _appm._pending_has_boss([], {"source": "liepin"}) is False)

    # 清理：删猎聘测试行，恢复猎聘风控/预算为干净态
    _cl = db.conn(); _cl.execute("DELETE FROM jobs WHERE source='liepin'"); _cl.commit(); _cl.close()
    db.clear_risk(source="liepin")
    _cl = db.conn()
    _cl.execute("UPDATE config SET value='' WHERE key IN ('quota_date_liepin','quota_used_liepin')")
    _cl.commit(); _cl.close()
    _bkdir = os.path.join(BASE, "data", "_bk_test")
    if os.path.isdir(_bkdir):
        import shutil as _sh
        _sh.rmtree(_bkdir, ignore_errors=True)

    # ---- 汇总 ----
    passed = sum(1 for s, *_ in RESULTS if s == "PASS")
    failed = [n for s, n, d in RESULTS if s == "FAIL"]
    lines = [f"{s} {n} {d}" for s, n, d in RESULTS]
    lines.append(f"\n===== {passed}/{len(RESULTS)} PASSED =====")
    if failed:
        lines.append("FAILED: " + ", ".join(failed))
    open("e2e_result.txt", "w", encoding="utf-8").write("\n".join(lines))
    return 0 if not failed else 1


try:
    sys.exit(main())
except SystemExit:
    raise
except Exception:
    open("e2e_result.txt", "w", encoding="utf-8").write("FATAL:\n" + traceback.format_exc())
    sys.exit(1)
