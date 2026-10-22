<div align="center">

# BOSS 岗位追踪

**多关键词批量获取 → 本地沉淀 → 匹配打分 → 一键投递 → 回复跟踪 → 微信日报**

纯本地运行的个人求职效率工具，数据只存自己的 SQLite，不上传任何地方。支持 **BOSS直聘 + 猎聘** 双平台合并入库。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-%E6%9C%AC%E5%9C%B0%E5%AD%98%E5%82%A8-003B57?logo=sqlite&logoColor=white)
![Tests](https://img.shields.io/badge/e2e-214%2F214-brightgreen)
![Platform](https://img.shields.io/badge/platform-Windows-lightgrey)


</div>

---

## 它解决什么问题

在 BOSS 直聘投过岗的都懂：每天手动搜、手动比薪资、投完记不住哪家回了、一周后根本想不起来投过什么。这个工具把整条链路收进**一个本机网页**：

```mermaid
flowchart LR
    A["关键词 × 城市"] -->|定时 / 手动获取| B[("SQLite 本地库")]
    B --> C["筛选 · 匹配打分 · 地图 · 看板"]
    C -->|勾选批量投递| D["官方打招呼"]
    D --> E["HR 回复跟踪"]
    E --> F["微信日报"]
```

## ✨ 核心能力

**获取与防风控**
- 多关键词 × 多城市（内置 373 城字典，可从 BOSS 同步），离屏静默运行不打扰日常用电脑
- 页间随机节流、分段冷却、每日请求预算；触发风控自动熔断上锁，code=37 自动降温重试
- 重复获取只刷新不重复入库；薪资「空值不覆盖」保护，历史数据不会被抹掉
- **双平台统一入口**：BOSS直聘 + 猎聘，配置页「平台开关」各管各的——点一次「立即获取」/ 每日定时都会按开关顺序跑所有启用平台（BOSS 轮结束自动错峰再跑猎聘）。关键词、城市、页数、定时、浏览器模式两平台共用一份配置；登录态 / 浏览器实例 / 风控锁 / 请求预算仍完全隔离

**筛选与匹配**
- 简历画像：上传 PDF / Word / TXT 或一键同步 BOSS 在线简历，本地技能词典解析
- 每个岗位 0–100 匹配分：技能命中 + 薪资 + 学历 + 职位相关 + 城市；**每轮获取、每次补抓描述后都自动重算**（不再只在改画像时才更新）
- 12 类筛选、全列可排序、同岗合并、全文搜索、CSV 导出

**投递与提醒**
- 按筛选或勾选批量投递（模拟官方「打招呼」），结果与失败原因全部留存
- 投递超期红色标记（3 / 7 / 14 天没人理）、独立 HR 回复页、PushPlus 微信日报
- 回复自动同步**只支持 BOSS**（读会话列表公司名）；猎聘回复在其站内 IM，需登录态调私有接口，本工具不接入，猎聘岗位用手动「标记 HR 已回复」

**可视化**
- 内置 Leaflet 地图（已本地打包，不依赖外网 CDN）：单点弹窗、全量分布、热力图按数量或薪资
- 数据看板：KPI 卡、14 天趋势面积图、薪资 / 学历 / 经验 / 城市 / HR 职位分布
- **看板与地图都支持平台筛选**（全部 / BOSS / 猎聘），KPI、漏斗、图表、地图一次切换全跟随；猎聘卡片不含经纬度，地图会直接说明原因并给条数

## 🚀 快速开始

> 需要 Python 3.10+，以及本机装有 Chrome 或 Edge。

```bash
pip install -r requirements.txt
python app.py                    # 或 Windows 双击 start.bat
```

打开 **http://127.0.0.1:8765**，首次使用按这个顺序：

1. 「获取配置」→ 填关键词（逗号分隔）、选城市、设每城页数（建议 3–5）→ 点「立即获取」
2. 第一次会要求登录：顶部点「**BOSS**」胶囊，弹出窗口扫码一次，Cookie 之后自动复用（顶部两个胶囊分别显示 BOSS / 猎聘 是否已登录）
3. 「简历画像」→ 同步 BOSS 简历或上传简历文件 → 全库自动重算匹配分
4. （可选）把 PushPlus token 写入 `data/pushplus.txt`，开启微信日报
5. （可选）想合并猎聘岗位：「获取配置」顶部「平台开关」→ 猎聘选「开启」；免登录即可搜索，点顶部「猎聘」胶囊登录一次（独立登录态）可拿更全结果

<details>
<summary><b>⚠️ 免责声明</b>（点开阅读）</summary>

本项目仅供个人求职效率用途，通过只读接口获取公开岗位列表。请自行承担使用第三方平台自动化的账号风险（项目内置防风控节流），不要用于批量爬取、转售数据等违反平台条款的用途。

</details>

<details>
<summary><b>📁 目录结构</b></summary>

```
boss-job-tracker/
├── app.py              # FastAPI 入口 + 全部 HTTP API + 定时调度
├── crawler.py          # DrissionPage 浏览器池、接口获取、防风控、简历/会话/城市同步
├── apply.py            # 投递（模拟打招呼）执行器
├── db.py               # SQLite 存储层：建表/迁移/UPSERT/筛选查询/排序/统计
├── salary.py           # 薪资文本 → 数值解析
├── resume.py           # 简历解析 + 技能词典 + 匹配分计算
├── push.py             # PushPlus 微信推送
├── e2e_test.py         # 214 项端到端回归测试（离线，monkeypatch 模拟浏览器）
├── selftest.py         # 轻量自检
├── start.bat           # Windows 一键启动
├── requirements.txt
├── static/
│   ├── index.html      # 单文件前端
│   └── vendor/leaflet/ # Leaflet 1.9.4 + leaflet.heat（本地打包）
└── data/               # 运行时生成（.gitignore 排除，绝不上传）
    ├── jobs.db         # 岗位/批次/配置/投递记录
    ├── cities.json     # 城市字典
    ├── chrome_profile/ # 浏览器用户目录（含登录 Cookie）
    ├── boss_cookies.json / login_state.json
    └── pushplus.txt    # PushPlus token（自己创建，仓库只给 .example）
```

</details>

<details>
<summary><b>🔌 API 一览</b>（本机 http://127.0.0.1:8765）</summary>

| 方法+路径 | 用途 |
|---|---|
| `GET /api/jobs` | 岗位列表（keyword/city_code/status/applied/degree/experience/scale/industry/salary_min/salary_max/no_salary/has_detail/starred/replied/overdue/dedupe/q/sort/page/size） |
| `GET /api/counts` | 各筛选口径计数（含投递超期数） |
| `GET /api/stats` | 看板聚合（趋势/分布/地图点位统计），`?source=boss/liepin` 按平台筛选，并返回各平台条数 |
| `GET /api/map` | 地图点位（≤1500，含坐标/薪资/HR 字段，热力权重用），`?source=` 按平台筛选并返回 `no_coord` 缺坐标条数 |
| `POST /api/crawl` | 统一获取入口（默认按平台开关顺序跑 BOSS→猎聘；可 `?platform=boss/liepin` 单跑） |
| `GET /api/status` `/api/runs` | 运行状态 / 最近 10 轮历史（含日志） |
| `POST /api/apply` `GET /api/apply_status` `/api/applications` | 投递执行与记录 |
| `POST /api/details` `/api/details_status` | 按筛选/勾选补抓描述 |
| `POST /api/replies/sync` `GET /api/replies` `/api/replies/state` | HR 回复只读同步与查看（**自动同步仅 BOSS**，响应里 `sync_supported` 标明各平台支持范围） |
| `POST /api/profile/sync_boss` `/api/profile/upload` `/api/profile/patch` | 简历画像三种来源 |
| `POST /api/config` `GET /api/config` | 获取配置读写 |
| `GET /api/export` | 当前筛选结果 CSV 导出（含坐标/HR/匹配分） |
| `POST /api/login` `/api/repair` `/api/unlock` | 登录态维护 |
| `POST /api/cities/sync` | 从 BOSS 同步城市字典 |

</details>

<details>
<summary><b>🧮 匹配分算法与测试</b></summary>

**算法**：`核心技能命中 ×32 + 其他技能 ×1.6（≤45）+ 薪资达标 25 + 学历满足 15 + 职位词相关 10 + 城市加分 5`，封顶 100。技能词典 60 项（数据分析向），英文技能按词边界匹配（避免 sql 误中 mysql）。简历变更、每轮获取、每次补抓描述后都会全库重算。

> 猎聘搜索卡片本身不带技能标签（实测 `job.labels` 恒为空、无 JD 文本），所以它的技能项靠「职位名 + 行业」命中，分数天然比 BOSS 低一档；两平台数字可比，但别拿绝对分值横向对比。

**测试**：

```bash
python e2e_test.py
# ===== 214/214 PASSED =====
```

离线回归：monkeypatch 模拟浏览器与接口返回，覆盖入库/去重/薪资空值保护/筛选/排序/超期口径/匹配分/投递状态机/风控熔断/预算/地图/看板聚合/**平台隔离与平台筛选**等。

</details>

<details>
<summary><b>📌 已知限制</b></summary>

- 岗位数据来自 BOSS 列表接口，单关键词单城市页数有上限（约 30 页 × 30 条）
- 猎聘：仅做获取/入库/看板合并（投递与消息回复请到猎聘官网手动操作）；接口翻页硬上限 400 条/组合；城市需有 dqCode 映射（liepin.py CITY_CODE，已覆盖主流 12 城，缺的城市日志会提示补码）
- 猎聘搜索卡片不含经纬度 → 地图上无点位（看板会提示原因，地理分布请看「城市分布」）；也不含技能标签与 JD，所以「补抓描述」只处理 BOSS，猎聘行会跳过并计数
- 「HR 已回复」按公司名匹配会话，同名多岗可能串标，可在 HR 回复页「取消标记」；猎聘回复无法自动同步，只能手动标记
- 地图瓦片用高德公共免 key 瓦片，仅限个人低频率使用
- PushPlus 为第三方服务，token 需自行申请；不配置则日报功能静默关闭
- Windows + Chrome/Edge 组合验证过；macOS/Linux 未测试（DrissionPage 理论支持）

</details>
