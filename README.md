# BOSS 岗位追踪（boss-job-tracker）

本地运行的 BOSS 直聘岗位聚合追踪工具：**多关键词 × 多城市批量获取岗位 → 本地 SQLite 沉淀 → 筛选/匹配打分/地图/看板 → 一键投递 → HR 回复跟踪 → 微信日报推送**。

单用户本地自用，界面中文，开箱即跑（`python app.py` 打开 http://127.0.0.1:8765）。

> ⚠️ 免责声明：本项目仅供个人求职效率用途，通过只读接口获取公开岗位列表。请自行承担使用第三方平台自动化的账号风险（项目内置了防风控节流），不要用于批量爬取、转售数据等违反平台条款的用途。

## 功能一览

| 模块 | 说明 |
|---|---|
| 岗位获取 | 多关键词 × 多城市（内置 373 个 BOSS 城市，可同步更新）× 每城页数 × 学历筛选；离屏静默运行，不影响日常用电脑 |
| 防风控 | 页间 8-20s 随机节流、每 3 页小冷却、每 6 页长冷却；code=37 自动降温重试；触发风控即熔断 + 60 分钟冷却锁；每日请求预算（默认 150 次） |
| 数据沉淀 | SQLite UPSERT（以 encryptJobId 为主键，重复获取只刷新不重复入库）；薪资「空值不覆盖」保护；岗位下线自动识别（status=offline） |
| 薪资解析 | "8-15K·13薪" → salary_min/salary_max 数值化，支持按薪资区间筛选 |
| JD 匹配分 | 上传简历（PDF/Word/TXT）或一键同步 BOSS 在线简历 → 本地技能词典解析画像 → 每个岗位算 0-100 匹配分；技能按词边界匹配（避免 sql 误中 mysql） |
| 筛选器 | 关键词/城市/状态/薪资区间/学历/经验/规模/行业/描述有无/收藏/HR已回复/投递超期/合并同岗/全文搜索，可排序（薪资、匹配度、回复时间、收藏置顶、公司名等） |
| 投递 | 列表勾选或按筛选批量投递（模拟点击 BOSS 官方"打招呼"），结果记录 + 失败原因留存；投递后超期（3/7/14 天无回复）红色标记 + 筛选 + 日报提醒 |
| HR 回复 | 只读同步 BOSS 聊天会话，按公司名匹配回标岗位；独立「HR 回复」页面（KPI + 明细 + 直达聊天 + 取消误标） |
| 补抓描述 | 先筛选/勾选岗位，再分批补抓详情页 JD（默认每轮 20 条、可随时停、带风险提示） |
| 内置地图 | Leaflet（已本地打包，不依赖外网 CDN）+ 高德免 key 瓦片；岗位坐标 100% 来自 BOSS 接口。列表/详情弹窗单点地图，看板全量点位分布 + 热力图（按数量 / 按薪资格拉分布） |
| 数据看板 | KPI 卡、发现趋势面积图、薪资/学历/经验/城市分布、HR 职位分布、地理热力；7/30/90 天口径切换 |
| 收藏与备注 | 单岗位星标 + 自由备注，收藏置顶排序 |
| 微信日报 | PushPlus 推送：本轮获取/新增/下线/投递漏斗/超期数（token 自配，见下文） |
| 定时任务 | APScheduler 每日定时自动获取（默认 09:30，可配置、可跳过） |
| 登录维护 | 独立「修复登录态」窗口（扫码/验证一次，Cookie 持久复用）；修复卡死自愈 |

## 快速开始

```bash
# 1. 环境（Python 3.10+，需要本机装有 Chrome 或 Edge 浏览器）
pip install -r requirements.txt

# 2. 启动
python app.py          # 或 Windows 双击 start.bat

# 3. 打开 http://127.0.0.1:8765
```

首次使用顺序：

1. 「获取配置」页 → 填关键词（逗号分隔）、选城市、设每城页数（建议 3-5）、可选学历 → 点「立即获取」
2. 第一次获取会要求登录：点右上角「登录/修复」，在弹出浏览器窗口扫码，成功后 Cookie 自动保存
3. 「简历画像」→ 从 BOSS 同步简历或上传简历文件 → 全库重算匹配分
4. （可选）把 PushPlus token 写入 `data/pushplus.txt` 开启微信日报

## 目录结构

```
boss-job-tracker/
├── app.py              # FastAPI 入口 + 全部 HTTP API + 定时调度
├── crawler.py          # DrissionPage 浏览器池、接口获取、防风控、简历/会话/城市同步
├── apply.py            # 投递（模拟打招呼）执行器
├── db.py               # SQLite 存储层：建表/迁移/UPSERT/筛选查询/排序/统计
├── salary.py           # 薪资文本 → 数值解析
├── resume.py           # 简历解析 + 技能词典 + 匹配分计算
├── push.py             # PushPlus 微信推送
├── e2e_test.py         # 160 项端到端回归测试（离线，monkeypatch 模拟浏览器）
├── selftest.py         # 轻量自检
├── start.bat           # Windows 一键启动
├── requirements.txt
├── static/
│   ├── index.html      # 单文件前端（BMW 风格设计系统 + BOSS 字体栈）
│   └── vendor/leaflet/ # Leaflet 1.9.4 + leaflet.heat（本地打包）
└── data/               # 运行时生成（.gitignore 排除，绝不上传）
    ├── jobs.db         # 岗位/批次/配置/投递记录
    ├── cities.json     # 城市字典（可从 BOSS 同步 373 城）
    ├── chrome_profile/ # 浏览器用户目录（含 BOSS 登录 Cookie）
    ├── boss_cookies.json / login_state.json
    └── pushplus.txt    # PushPlus token（自己创建，仓库只给 .example）
```

## 主要 API（本机 http://127.0.0.1:8765）

| 方法+路径 | 用途 |
|---|---|
| `GET /api/jobs` | 岗位列表（keyword/city_code/status/applied/degree/experience/scale/industry/salary_min/salary_max/no_salary/has_detail/starred/replied/overdue/dedupe/q/sort/page/size） |
| `GET /api/counts` | 各筛选口径计数（含投递超期数） |
| `GET /api/stats` | 看板聚合（趋势/分布/地图点位统计） |
| `GET /api/map` | 地图点位（≤1500，含坐标/薪资/HR 字段，热力权重用） |
| `POST /api/crawl` | 启动一轮获取（互斥、冷却锁、预算检查） |
| `GET /api/status` `/api/runs` | 运行状态 / 最近 10 轮历史（含日志） |
| `POST /api/apply` `GET /api/apply_status` `/api/applications` | 投递执行与记录 |
| `POST /api/details` `/api/details_status` | 按筛选/勾选补抓描述 |
| `POST /api/replies/sync` `GET /api/replies` `/api/replies/state` | HR 回复只读同步与查看 |
| `POST /api/profile/sync_boss` `/api/profile/upload` `/api/profile/patch` | 简历画像三种来源 |
| `POST /api/config` `GET /api/config` | 获取配置读写 |
| `GET /api/export` | 当前筛选结果 CSV 导出（含坐标/HR/匹配分） |
| `POST /api/login` `/api/repair` `/api/unlock` | 登录态维护 |
| `POST /api/cities/sync` | 从 BOSS 同步城市字典 |

## 匹配分算法

`核心技能命中 ×32 + 其他技能 ×1.6（≤45）+ 薪资达标 25 + 学历满足 15 + 职位词相关 10 + 城市加分 5`，封顶 100。技能词典 60 项（数据分析向），英文技能按词边界匹配。简历变更后全库自动重算。

## 测试

```bash
python e2e_test.py
# ===== 160/160 PASSED =====
```

离线回归：monkeypatch 模拟浏览器与接口返回，覆盖入库/去重/薪资空值保护/筛选/排序/超期口径/匹配分/投递状态机/风控熔断/预算/地图/看板聚合等。

## 已知限制

- 岗位数据来自 BOSS 列表接口，单关键词单城市页数有上限（约 30 页 × 30 条）
- 「HR 已回复」按公司名匹配会话，同名多岗可能串标，可在 HR 回复页「取消标记」
- 地图瓦片用高德公共免 key 瓦片，仅限个人低频率使用
- PushPlus 为第三方服务，token 需自行申请；不配置则日报功能静默关闭
- Windows + Chrome/Edge 组合验证过；macOS/Linux 未测试（DrissionPage 理论支持）
