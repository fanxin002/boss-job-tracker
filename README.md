<div align="center">

# BOSS 岗位追踪

**多关键词批量获取 → 本地沉淀 → 匹配打分 → 一键投递 → 回复跟踪 → 微信日报**

纯本地运行的个人求职效率工具，数据只存自己的 SQLite，不上传任何地方。支持 **BOSS直聘 + 猎聘** 双平台合并入库。

![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-%E6%9C%AC%E5%9C%B0%E5%AD%98%E5%82%A8-003B57?logo=sqlite&logoColor=white)
![Tests](https://img.shields.io/badge/e2e-248%2F248-brightgreen)
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
- **获取进度可观测**：点「立即获取」后弹进度条，显示当前平台 / 关键词·城市·第几页 / 已抓·新增·下线；点「后台运行」收起弹窗后，右下角有常驻进度胶囊可随时点回来；抓完自动收起并提示
- 进度弹窗的层级低于日志/详情弹窗，弹窗内点「查看日志」日志会正常浮在上层；Esc 逐层关闭（先日志、再进度），不会一次全关
- **登录判定以 Cookie 为准**：BOSS 的登录凭据是 `wt2`（游客态能搜列表但拿不到 `wt2`）。状态文件与 Cookie 不一致时自动纠正为「未登录」，并在获取前直接拦截提示，不会白跑一轮浏览器
- **登录态防误擦（合并保存）**：`save_cookies()` 按 (name,domain) 合并写而非全量覆盖，游客态保存不会擦掉已保存的 `wt2`；修复流程在未登录分支不再保存 Cookie；注入时登录凭据以文件为准、其余只补缺。所以「登录老掉」只剩两种正常原因：BOSS 服务端会话到期（重新扫码即可）或被风控踢会话（点「修复」过验证）
- **注入不覆盖更新的会话（2026-10-26 修）**：旧逻辑无条件把文件里的 `wt2` 写进浏览器，而 BOSS 的 JS 会在会话中刷新 `wt2` —— 一注入就把新鲜的冲回旧的，表现为「怎么每次都要重新登录」。现在分三级：profile 里已有凭据就**一律不动**、没有才用文件补、两边都没有就如实报未登录；并且每轮抓完会把浏览器里最新的 Cookie **回写**文件，备份不会越放越旧
- **下线判定防误杀**：只有「连续 2 轮没再出现」才判下线；且本轮在「关键词×城市×平台」范围内重新命中的岗位数不足该范围活跃数 60% 时（翻页变浅 / 排序浮动 / 被风控截断）整段跳过判定并在日志里说明。误杀了可在「更多筛选」里点「撤销误杀」一键回滚（24h 内，或不限时间）
- **双平台统一入口**：BOSS直聘 + 猎聘，配置页「平台开关」各管各的——点一次「立即获取」/ 每日定时都会按开关顺序跑所有启用平台（BOSS 轮结束自动错峰再跑猎聘）。关键词、城市、页数、定时、浏览器模式两平台共用一份配置；登录态 / 浏览器实例 / 风控锁 / 请求预算仍完全隔离

**筛选与匹配**
- 简历画像：上传 PDF / Word / TXT 或一键同步 BOSS 在线简历，本地技能词典解析
- 每个岗位 0–100 匹配分：技能命中 + 薪资 + 学历 + 职位相关 + 城市；**每轮获取、每次补抓描述后都自动重算**（不再只在改画像时才更新）
- 13 类筛选、全列可排序、同岗合并（公司名归一后跨平台也能合）、全文搜索、CSV 导出
- 筛选默认只占一行（关键词/城市/平台/排序/搜索），其余 16 项收进「更多」，不再挤占首屏
- **加载反馈（骨架屏四态）**：点岗位详情立即弹窗 + 骨架占位（尺寸与最终内容一致，切换不跳动），加载完 650ms 柔和淡入；失败保留上下文、给出「重试」+「关闭」（键盘 Tab/Enter 可用）；岗位列表首屏、HR 回复页、投递记录表同样用骨架行。扫光只用 transform/opacity，`prefers-reduced-motion` 下自动关闭扫光与过渡

**技能缺口（求职向最有用的一个）**
- 统计在招岗位 JD/岗位名里各技能出现频次，切成「岗位要 · 我有 / 岗位要 · 我缺」两栏
- 直接回答「下一份简历该补什么」：例如当前 342 条在招里，统计学被 57 个岗位提到但画像里没有

**技能薪资分析（市场需求侧）**
- 回答「哪些技能在高薪岗位里更重要」：每个技能给出 需求岗位数 / 薪资中位 / 溢价 / 高薪岗出现率 / 偏好度
- 三种排序：需求热度（谁提得多）、薪资溢价（要求该技能 vs 不要求的薪资中位数差，单位 K）、高薪偏好（高薪岗出现率 ÷ 整体出现率，>1 说明高薪岗更看重）
- 高薪线 = 全体在招薪资 P75；溢价是组间相关差，不是因果
- 实测结论示例：Flink 仅 7 个岗位但薪资中位 14K、溢价 +4K、高薪岗出现率 57%；而 SQL/Python 岗位量大（70/71）溢价仅 +1.7K/+2.3K——前者是敲门砖，后者是标配

**投递与提醒**
- 按筛选或勾选批量投递（模拟官方「打招呼」），结果与失败原因全部留存
- 批量操作：收藏 / 标阶段 / 标已回复，**两平台通用**（猎聘只是不能批量投递，其它动作照旧）
- 投递超期红色标记（3 / 7 / 14 天没人理）、独立 HR 回复页、PushPlus 微信日报
- **HR 回复页 = 投递追踪页**：投递成功立刻出现在列表，状态分「等待回复」（黄色，时间列显示你的投递时间，方便看投了多久没动静）/「已回复」（绿色，时间列显示回复时间）；未回复的排前面。不用等 HR 回复才有记录
- 跟进日程：岗位详情里设「跟进时间」，看板汇总未来 14 天的面试/复聊安排
- 回复自动同步**只支持 BOSS**（读会话列表公司名）；猎聘回复在其站内 IM，需登录态调私有接口，本工具不接入，猎聘岗位用手动「标记 HR 已回复」

**可视化**
- 内置 Leaflet 地图（已本地打包，不依赖外网 CDN）：单点弹窗、全量分布、热力图按数量或薪资
- 数据看板：KPI 卡、14 天趋势面积图、薪资 / 学历 / 经验 / 城市 / HR 职位分布、投递转化（按平台/规模/关键词）、跟进日程
- **看板与地图都支持平台筛选**（全部 / BOSS / 猎聘），KPI、漏斗、图表、地图一次切换全跟随
- 猎聘卡片不带经纬度：用「同城同区 BOSS 岗位坐标中位数」补**区级近似点位**（半透明虚线圆，弹窗明确标注近似），地图不再是空白

**稳定性**
- 启动时 + 每轮获取前自动备份 SQLite 到 `data/backup/`，保留最近 7 份

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
<summary><b>📱 手机在外面也能看（内网穿透）</b></summary>

电脑上跑着服务，用 Cloudflare 快速隧道把它映射到一个公网 https 地址，手机不用装任何 App，Safari 打开就行。

**1. 先设访问口令**（不设的话任何拿到网址的人都能看你的岗位/备注/简历画像）

```bash
# 生成一个 10 位口令，写进 data/access_key.txt
python -c "import secrets;print(''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(10)))" > data/access_key.txt
```

**2. 起隧道**

```bash
python tunnel.py          # 或 Windows 双击 tunnel.bat
```

要做的事它都自己包了：首次会**自动下载 cloudflared 到 `bin/`**（约 53MB）、没配口令会自动生成一个 10 位写进 `data/access_key.txt`、然后打印公网地址并写入 `data/tunnel_url.txt`。

```
==============================================================
✅ 隧道已就绪，手机（4G/任意网络）打开这个地址：

   https://xxx-xxx-xxx.trycloudflare.com

   口令：abcd1234xy
==============================================================
```

停止：`python tunnel.py --stop`（或直接关掉那个窗口）。

**地址会不会变？三条路自己选：**

| 通道 | 地址 | 花钱 | 需要什么 |
|---|---|---|---|
| **ngrok**（推荐） | **固定**：账号自带的 `xxx.ngrok-free.dev`，重启也不变 | 免费 | 注册 ngrok（邮箱即可，不绑卡）→ 复制 authtoken |
| Cloudflare 命名隧道 | **固定**：自己的域名，如 `jobs.abc.com` | 域名约 ¥10/年 | Cloudflare 账号 + 托管在 CF 的域名 |
| Cloudflare 快速隧道（默认） | 每次重启都变（随机子域名） | 免费 | 什么都不用配 |

**想要固定地址 → 用 ngrok（最省事）**：

```bash
python tunnel.py --token 你的ngrok_authtoken   # 只需一次，写进 data/ngrok_token.txt
python tunnel.py                               # 以后每次都是同一个地址
```

注册地址 https://dashboard.ngrok.com/signup ，登录后 **Your Authtoken** 里复制。

ngrok 免费的代价（知道就行，不影响个人用）：

- 浏览器首次访问会出现一个「Visit Site」提示页，点一下，之后 7 天内不再出现
- 免费额度：**2 万请求/月、1GB 流量/月**。所以页面在后台/锁屏时**不轮询**（已内置，切回来立刻恢复），正常刷几十次完全用不完
- 想换通道：`--provider ngrok` / `--provider cf`；也可以把选择写进 `data/tunnel_provider.txt`
- 如果 ngrok 已经在别的窗口跑着，`python tunnel.py` 会**直接复用它**并打印地址，不会再起第二个（第二个会抢 `127.0.0.1:4040` 然后无声退出）

**想要固定地址 + 自己的域名 → Cloudflare 命名隧道**：

1. Cloudflare 控制台 → Zero Trust → Networks → Tunnels → Create a tunnel（选 Cloudflared）
2. 复制它给的 **token**：`python tunnel.py --token 你的CF_token`
3. 在该隧道的 **Public Hostname** 绑域名 → 服务填 `http://127.0.0.1:8765`
4. 把域名写进 `data/tunnel_host.txt`（只为打印显示）

快速隧道换地址时不用慌：新地址会写进 `data/tunnel_url.txt`，并且**配了 PushPlus 就会自动推微信**，出门在外也能找到新链接。

**3. 手机打开**该地址 → 输口令 → 之后 30 天免输；iOS 用「分享 → 添加到主屏幕」可全屏运行。

**门禁规则**（`app.py` 的 `_access_gate` 中间件）：

| 来源 | 行为 |
|---|---|
| 本机 `127.0.0.1` 直连（**且 Host 是 localhost/127.0.0.1**） | 免口令，自己电脑上不用输 |
| 经隧道（Cloudflare / ngrok）/ 局域网来的请求 | 必须带 `jbt` cookie 或 `?k=口令`，否则只给登录页（401） |
| 没配 `data/access_key.txt` | 门禁整体关闭，行为和以前完全一样 |

> 为什么还要看 Host 头：隧道是把请求**从本机转发进来**的，来源 IP 同样是 `127.0.0.1`。
> Cloudflare 会带 `CF-Connecting-IP` 能区分，但 **ngrok 不带**（实测 2026-10-26）——
> 只看来源 IP 的话，ngrok 隧道流量会被当成「自己电脑」直接放行，口令闸形同虚设。
> 现在判据是「来源是环回地址 **且** Host 也是环回名」，隧道域名一律要口令。

注意：快速隧道地址**每次重启都会变**；地址本身是公网可达的，安全性靠口令这一层，不要发到公开群。
</details>

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
├── e2e_test.py         # 端到端回归测试（离线，monkeypatch 模拟浏览器）
├── selftest.py         # 轻量自检
├── tunnel.py           # Cloudflare 快速隧道（手机外网访问，自动下载 cloudflared / 自动生成口令）
├── start.bat           # Windows 一键启动
├── tunnel.bat          # Windows 一键起隧道
├── requirements.txt
├── static/
│   ├── index.html      # 单文件前端
│   └── vendor/leaflet/ # Leaflet 1.9.4 + leaflet.heat（本地打包）
├── bin/                # cloudflared.exe / ngrok.exe（.gitignore 排除，tunnel.py 首次运行自动下载）
└── data/               # 运行时生成（.gitignore 排除，绝不上传）
    ├── jobs.db         # 岗位/批次/配置/投递记录
    ├── cities.json     # 城市字典
    ├── chrome_profile/ # 浏览器用户目录（含登录 Cookie）
    ├── boss_cookies.json / login_state.json
    ├── liepin_cookies.json / liepin_login_state.json   # 猎聘登录态（用于 lt_auth 交叉校验）
    ├── tunnel_url.txt  # 当前隧道公网地址（每次重启会变）
    ├── access_key.txt  # 外网访问口令（不建=不启用门禁）
    └── pushplus.txt    # PushPlus token（自己创建，仓库只给 .example）
```

</details>

<details>
<summary><b>🔌 API 一览</b>（本机 http://127.0.0.1:8765）</summary>

| 方法+路径 | 用途 |
|---|---|
| `GET /api/jobs` | 岗位列表（keyword/city_code/status/applied/degree/experience/scale/industry/salary_min/salary_max/no_salary/has_detail/starred/replied/overdue/dedupe/q/sort/page/size） |
| `GET /api/job` | 单条岗位详情（详情面板用；不再要求岗位必须在当前页，翻到第 8 页也能打开） |
| `GET /api/skills/gap` | 技能缺口：「岗位要·我有」vs「岗位要·我缺」，按岗位数排序 |
| `GET /api/skills/salary` | 技能薪资分析：需求热度/薪资溢价/高薪偏好（`?source=boss/liepin`） |
| `GET /api/conversion` | 投递转化分析，`by=source/scale/keyword/city/degree` |
| `GET /api/schedule` `/api/next_at` | 跟进日程查询 / 写入（面试、复聊时间） |
| `POST /api/bulk` | 批量收藏 / 阶段 / 已回复（不含投递） |
| `GET /api/liepin_cities` | 猎聘支持的城市列表（配置页用来标注不支持的城市） |
| `POST /api/backup` | 手动备份 SQLite（自动备份在启动与每轮获取前） |
| `POST /api/offline/recover` | 撤销「已下线」判定（误杀回滚），`?hours=24`（默认）/ `hours=0` 不限时间 |
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
| `POST /api/liepin_cities/sync` `GET /api/liepin_cities/sync_state` | 从猎聘同步全量城市码（370+ 城，与 BOSS 城市库对等） |

</details>

<details>
<summary><b>🧮 匹配分算法与测试</b></summary>

**算法**：`核心技能命中 ×32 + 其他技能 ×1.6（≤45）+ 薪资达标 25 + 学历满足 15 + 职位词相关 10 + 城市加分 5`，封顶 100。技能词典 60 项（数据分析向），英文技能按词边界匹配（避免 sql 误中 mysql）。简历变更、每轮获取、每次补抓描述后都会全库重算。

> 猎聘搜索卡片本身不带技能标签（实测 `job.labels` 恒为空、无 JD 文本），所以它的技能项靠「职位名 + 行业」命中，分数天然比 BOSS 低一档；两平台数字可比，但别拿绝对分值横向对比。

**测试**：

```bash
python e2e_test.py
# ===== 248/248 PASSED =====
```

离线回归：monkeypatch 模拟浏览器与接口返回，覆盖入库/去重/薪资空值保护/筛选/排序/超期口径/匹配分/投递状态机/风控熔断/预算/地图/看板聚合/**平台隔离与平台筛选**等。

</details>

<details>
<summary><b>📌 已知限制</b></summary>

- 岗位数据来自 BOSS 列表接口，单关键词单城市页数有上限（约 30 页 × 30 条）
- 猎聘：获取/入库/看板合并 + **详情页补抓 JD/福利/区县**（`补抓描述` 会自动按平台分流：BOSS 走列表页、猎聘走 `schema.org` JobPosting）；仍不支持投递与消息回复（请到猎聘官网手动）；接口翻页硬上限 400 条/组合
- 猎聘搜索卡片与详情页都**不含经纬度** → 地图用「同城同区 BOSS 坐标中位数」推**区级近似点位**（弹窗标注「≈ 区级近似」）；同城没有 BOSS 样本时仍无法上图
- 城市码映射：猎聘通过 `get-all-dq` 接口同步全量城市码（与 BOSS 城市库对等），配置页「同步猎聘城市码」一键刷新；未同步过的城市会标「猎聘不支持」
- 猎聘「猎头代招」类职位会被平台**脱敏**：公司名显示成「某知名公司」「某大型新零售公司」（详情页 `ld+json` 同样是脱敏名，只有猎头公司可见），这类行在列表标「保密 + 猎头」，且**不参与同岗合并**，避免把不同雇主的岗位误合
- 「HR 已回复」按公司名匹配会话，同名多岗可能串标，可在 HR 回复页「取消标记」；猎聘回复无法自动同步，只能手动标记
- 地图瓦片用高德公共免 key 瓦片，仅限个人低频率使用
- PushPlus 为第三方服务，token 需自行申请；不配置则日报功能静默关闭
- Windows + Chrome/Edge 组合验证过；macOS/Linux 未测试（DrissionPage 理论支持）

</details>
