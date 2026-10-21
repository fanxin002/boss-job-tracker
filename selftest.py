# -*- coding: utf-8 -*-
import sys, io, traceback
res = []
try:
    import ast
    for f in ["db.py", "crawler.py", "app.py"]:
        ast.parse(open(f, encoding="utf-8").read())
    res.append("SYNTAX_OK")
    import db
    db.init_db()
    res.append("DB_INIT_OK keys=" + ",".join(sorted(db.get_config().keys())))
    rid = db.create_run("数据分析", "厦门", 2, "test")
    db.append_run_log(rid, "测试日志行")
    rows = [{
        "job_id": "test123", "lid": "l1", "security_id": "s1", "keyword": "数据分析",
        "city_code": "101230200", "city_name": "厦门", "job_name": "数据分析师",
        "salary": "10-15K", "company": "测试公司", "scale": "100-499人", "industry": "互联网",
        "degree": "本科", "experience": "3-5年", "skills": "SQL,Python", "welfare": "五险一金",
        "boss_name": "王HR", "area_district": "思明区", "business_district": "软件园",
        "lng": "118.1", "lat": "24.4", "job_url": "https://x", "detail_text": "",
    }]
    n1, ids = db.upsert_jobs(rows, rid)
    res.append(f"INSERT new={n1}")
    n2, _ = db.upsert_jobs(rows, rid)  # 重复入库应不新增
    res.append(f"REPEAT new={n2}")
    off = db.mark_offline([("数据分析", "101230200")], {"other_id"})
    res.append(f"OFFLINE={off}")
    total, jr = db.query_jobs({"is_new": 0})
    res.append(f"QUERY total={total} status={jr[0]['status']}")
    db.finish_run(rid, "success", 1, 1, 1)
    res.append("RUNS=" + str(len(db.get_runs())))
    # upsert 冲突路径覆盖测试
    rows[0]["salary"] = "11-16K"
    db.upsert_jobs(rows, rid)
    import sqlite3
    c = db.conn()
    r = c.execute("SELECT salary,is_new,status FROM jobs WHERE job_id='test123'").fetchone()
    res.append(f"AFTER_UPDATE salary={r['salary']} is_new={r['is_new']} status={r['status']}")
    c.execute("DELETE FROM jobs WHERE job_id='test123'"); c.execute("DELETE FROM runs WHERE id=?", (rid,)); c.commit(); c.close()
    res.append("CLEANUP_OK")
except Exception:
    res.append("ERROR:\n" + traceback.format_exc())
open("selftest_result.txt", "w", encoding="utf-8").write("\n".join(res))
