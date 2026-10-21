"""简历解析与技能抽取。

数据来源两种：
1. BOSS 线上简历页（/web/geek/resume）—— 只读 innerText，不发任何写请求
2. 用户上传的 PDF / DOCX / TXT / MD

解析产出统一画像结构（写入 config.profile_json）：
{
  "skills": ["SQL","Python",...],          # 从原文自动抽取，不再手填
  "skill_detail": [{"name":"SQL","hits":3}],
  "degree": "大专",
  "years": 4,
  "expect_salary": 9,                       # 月薪 K（取期望上限）
  "expect_cities": ["厦门","南京"],
  "expect_titles": ["数据分析师","数据开发"],
  "source": "boss" | "upload:xxx.pdf",
  "updated": "2026-10-21 12:00:00",
  "chars": 3200
}
"""
import io
import os
import re
from datetime import datetime

MAX_TEXT = 24000

# (规范名, 别名列表, 权重) —— 权重用于匹配分，核心技能权重更高
SKILL_DICT = [
    ("SQL", ["sql"], 3),
    ("MySQL", ["mysql"], 2),
    ("ClickHouse", ["clickhouse"], 3),
    ("Hive", ["hive"], 2),
    ("Oracle", ["oracle"], 1),
    ("SQLServer", ["sqlserver", "sql server"], 1),
    ("PostgreSQL", ["postgresql", "postgres", "pgsql"], 1),
    ("Hadoop", ["hadoop"], 1),
    ("CDH", ["cdh"], 1),
    ("Sqoop", ["sqoop"], 1),
    ("HDFS", ["hdfs"], 1),
    ("Spark", ["spark"], 2),
    ("Flink", ["flink"], 1),
    ("Kafka", ["kafka"], 1),
    ("HBase", ["hbase"], 1),
    ("Redis", ["redis"], 1),
    ("Python", ["python"], 3),
    ("Pandas", ["pandas"], 2),
    ("NumPy", ["numpy"], 1),
    ("Java", ["java"], 1),
    ("Shell", ["shell", "bash"], 1),
    ("Linux", ["linux"], 1),
    ("Excel", ["excel"], 2),
    ("Power BI", ["power bi", "powerbi", "power-bi"], 3),
    ("FineBI", ["finebi", "fine bi", "帆软"], 2),
    ("Tableau", ["tableau"], 2),
    ("Superset", ["superset"], 1),
    ("Metabase", ["metabase"], 1),
    ("数据仓库", ["数据仓库", "数仓", "维度建模", "星型模型"], 3),
    ("ETL", ["etl", "数据清洗", "数据抽取"], 2),
    ("数据采集", ["数据采集", "数据抓取", "爬虫", "采集"], 3),
    ("数据分析", ["数据分析", "业务分析", "经营分析"], 3),
    ("报表开发", ["报表", "看板", "大屏", "仪表盘", "dashboard"], 3),
    ("可视化", ["可视化", "图表", "bi工具", "bi 工具"], 2),
    ("指标体系", ["指标体系", "指标", "kpi"], 2),
    ("漏斗分析", ["漏斗"], 2),
    ("RFM", ["rfm"], 1),
    ("AB测试", ["ab测试", "a/b测试", "ab test"], 1),
    ("机器学习", ["机器学习", "模型训练"], 1),
    ("逻辑回归", ["逻辑回归", "logistic"], 1),
    ("随机森林", ["随机森林", "random forest"], 1),
    ("GBDT", ["gbdt", "xgboost", "lightgbm"], 1),
    ("用户画像", ["用户画像", "标签体系"], 2),
    ("自动化", ["自动化", "automa"], 2),
    ("Selenium", ["selenium"], 1),
    ("Playwright", ["playwright"], 1),
    ("DrissionPage", ["drissionpage"], 1),
    ("API对接", ["api", "接口对接", "http接口"], 2),
    ("Docker", ["docker"], 1),
    ("Git", ["git"], 1),
    ("Nginx", ["nginx"], 1),
    ("Go", ["golang"], 1),
    ("Vue", ["vue"], 1),
    ("Power Query", ["power query"], 1),
    ("VBA", ["vba"], 1),
    ("统计学", ["统计学", "统计"], 1),
    ("供应链", ["供应链"], 1),
    ("门店运营", ["门店运营", "连锁门店"], 1),
]

DEGREE_KEYS = [("博士", 5), ("硕士", 4), ("本科", 3), ("大专", 2), ("中专", 1), ("高中", 1), ("中技", 1), ("初中", 0)]

SECTIONS = ["个人优势", "期望职位", "工作经历", "项目经历", "教育经历", "专业技能",
            "资格证书", "自定义添加", "个人作品", "自我评价"]


def _norm(text):
    t = (text or "").replace("\u3000", " ").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    return t


def _has(word, hay):
    """英文技能按词边界匹配（避免 sql 命中 mysql 的误判），中文直接用子串"""
    w = word.lower()
    if re.search(r"[a-z]", w):
        return re.search(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", hay) is not None
    return w in hay


def extract_skills(text):
    hay = _norm(text).lower()
    out = []
    for name, aliases, weight in SKILL_DICT:
        hits = sum(hay.count(a.lower()) if not re.search(r"[a-z]", a) else
                   len(re.findall(r"(?<![a-z0-9])" + re.escape(a.lower()) + r"(?![a-z0-9])", hay))
                   for a in aliases)
        if not hits:
            # 中文别名仍需 _has 兜底（大小写无关）
            hits = 0
        if hits:
            out.append({"name": name, "hits": int(hits), "weight": weight})
    out.sort(key=lambda x: (-x["weight"], -x["hits"], x["name"]))
    return out


def extract_degree(text):
    best, best_rank = "", -1
    for key, rank in DEGREE_KEYS:
        if key in text and rank > best_rank:
            best, best_rank = key, rank
    return best


def extract_years(text):
    m = re.search(r"(\d{1,2})\s*年(?:以上)?(?:工作)?经[验历]", text)
    if m:
        return int(m.group(1))
    # 从工作经历年份跨度推算
    yrs = [int(y) for y in re.findall(r"(20[0-2]\d)\s*[.\-/年]", text)]
    yrs = [y for y in yrs if 2000 <= y <= datetime.now().year]
    if yrs:
        return max(1, datetime.now().year - min(yrs))
    return 0


def extract_expect(text):
    """从「期望职位」区块里取 期望职位名 / 薪资 / 城市"""
    titles, cities, salary = [], [], 0
    sec = _section(text, "期望职位", ["工作经历", "项目经历", "教育经历"])
    if not sec:
        return titles, cities, salary
    # 薪资：6-9K / 8K-12K / 面议
    for a, b in re.findall(r"(\d{1,3})\s*[-~－]\s*(\d{1,3})\s*[Kk千]", sec):
        salary = max(salary, int(b))
    for one in re.findall(r"(\d{1,3})\s*[Kk千]", sec):
        salary = max(salary, int(one))
    # 城市：用已同步城市库匹配
    try:
        import json
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cities.json")
        if os.path.exists(p):
            data = json.load(open(p, encoding="utf-8"))
            flat = data if isinstance(data, list) else data.get("cities", [])
            for c in flat:
                nm = c.get("name") if isinstance(c, dict) else str(c)
                if nm and nm in sec:
                    cities.append(nm)
    except Exception:
        pass
    if not cities:
        for c in ["北京", "上海", "广州", "深圳", "厦门", "杭州", "南京", "福州", "泉州", "成都", "武汉", "西安"]:
            if c in sec:
                cities.append(c)
    # 职位名：形如「数据分析师 6-8K厦门」的行
    for line in sec.split("\n"):
        line = line.strip()
        m = re.match(r"^([\u4e00-\u9fa5A-Za-z/· ]{2,20})[\s]*\d{1,3}\s*[-~－]\s*\d{1,3}\s*[Kk千]", line)
        if m:
            t = m.group(1).strip()
            if t and t not in ("全职职位", "期望职位", "全职", "兼职"):
                titles.append(t)
    return titles[:6], list(dict.fromkeys(cities))[:6], salary


def _section(text, name, stops):
    i = text.find(name)
    if i < 0:
        return ""
    j = len(text)
    for s in stops:
        k = text.find(s, i + len(name))
        if k > 0:
            j = min(j, k)
    return text[i:j]


def parse(text, source="upload"):
    text = _norm(text)[:MAX_TEXT]
    if not text.strip():
        return None
    skills = extract_skills(text)
    titles, cities, salary = extract_expect(text)
    prof = {
        "skills": [s["name"] for s in skills][:36],
        "skill_detail": skills[:36],
        "degree": extract_degree(text),
        "years": extract_years(text),
        "expect_salary": salary,
        "expect_cities": cities,
        "expect_titles": titles,
        "source": source,
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "chars": len(text),
    }
    prof["core_skills"] = [s["name"] for s in skills if s["weight"] >= 3][:12]
    return prof


# ---------- 文件读取 ----------

def read_upload(filename, raw):
    """支持 pdf / docx / txt / md。返回纯文本"""
    ext = (os.path.splitext(filename or "")[1] or "").lower()
    if ext == ".pdf":
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(stream=raw, filetype="pdf")
            return "\n".join(p.get_text() for p in doc)
        except Exception:
            pass
        try:
            from pypdf import PdfReader
            r = PdfReader(io.BytesIO(raw))
            return "\n".join((p.extract_text() or "") for p in r.pages)
        except Exception as e:
            raise RuntimeError(f"PDF 解析失败：{e}")
    if ext in (".docx", ".doc"):
        try:
            import docx
            d = docx.Document(io.BytesIO(raw))
            parts = [p.text for p in d.paragraphs]
            for tb in d.tables:
                for row in tb.rows:
                    parts.append(" ".join(c.text for c in row.cells))
            return "\n".join(parts)
        except Exception as e:
            raise RuntimeError(f"Word 解析失败（.doc 请另存为 .docx 再传）：{e}")
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    raise RuntimeError("无法识别文件编码，请用 UTF-8 文本或 PDF/DOCX")
