# 现有数据链路审计 — 七阶段数据管线·第一阶段

> 2026-10-17 · **只读审计**，未改任何业务代码。所有结论钉到 `文件:行`；没有实测依据的地方显式写「未验证」。
> 证据来自源码 + 本地库只读查询（审计取数 `Temp\opencode\audit_db.txt`、`audit_bars.txt`、`bars_smoke_real.txt`）。

---

## 0. 总结论

任务书预设要"从零建"的东西，**大部分已经存在**：7 张采集相关表、五态逐域状态机、退避重试、覆盖审计、回填分批熔断与断点续跑都已实现且有回归。

真正缺的是**"一个入口 + 状态看得见"**：

| # | 缺口 | 证据 | 打到 |
|---|---|---|---|
| G1 | **6 条触发路径**，2 条完全绕过 `ingest_job`、5 条不写 `job_run` | §1-Q1 表 | 阶段 2 |
| G2 | `ingest_job` **无 `source` 列**（`snapshot`/`daily_bar` 都有） | `db.py:328-352` | 阶段 2（表变更） |
| G3 | 状态词与任务书**命名不同**：`WAITING/PARTIAL` vs `pending/partial_success` | `db.py:332-334` | 阶段 2 |
| G4 | 任务级状态无 `trade_date` 列（藏在 `detail` JSON 里） | `db.py:37-44` | 阶段 2 |
| G5 | 回填/重试**不写 `job_run`**，"这轮谁在跑"只覆盖盘后流水线 | Q1 表 | 阶段 2 |
| G6 | 日线覆盖 **rate 0.3827**（缺 2,813 只）+ `index_daily` 仅 48 行 | Q4 | 阶段 3/5（续跑） |
| G7 | 调度器当前 **两个开关全关**，`start()` 不装任何 job | `scheduler.py:510-515` | 阶段 4 |
| G8 | 打分域数据不全时**照样出正式排名**，只靠 `degraded` 披露 | Q10 | 阶段 7 |

---

## 1. 十问十答

### Q1 网页上的数据更新按钮分别调用哪个 API？

| 页面按钮 | 前端调用 | HTTP | 后端入口 | 写 `job_run` | 写 `ingest_job` |
|---|---|---|---|---|---|
| 总览页 立即执行 | `api.runJob()` `Overview.tsx:211` | `POST /api/jobs/run` | `routes.py:121` → 线程 `run_post_close(kind="manual")` | ✅ | ✅ |
| 定时任务页 立即执行 | `api.runJob(undefined, force)` `Jobs.tsx:178` | 同上 | 同上 | ✅ | ✅ |
| 数据管理 指定日期补跑 | `api.runJob(jobDate)` `DataManage.tsx:379` | 同上 `?date=` | 同上（`date` 参数） | ✅ | ✅ |
| 数据管理 补跑重试 | `api.collectRetry(domain?)` `:359` | `POST /api/pipeline/collect/retry` | `routes.py:2514` → `run_retries` | ❌ | ✅ |
| 数据管理 回填计划 | `api.backfillPlan(...)` `:388` | `GET /api/pipeline/backfill/plan` | `routes.py:2572`（零联网） | ❌ | ❌ |
| 数据管理 开始回填 | `api.backfillRun(...)` `:413` | `POST /api/pipeline/backfill` | `routes.py:2610` → `backfill.backfill` | ❌ | ❌ |
| 主力页 重算 | `api.mainForceRecompute()` `api.ts:1516` | `POST /api/main-force/refresh` | `routes.py:1208` → 线程**直接** `mainforce.run()` | ❌ | ❌ ⚠️**旁路** |
| 涨停池页 刷新 | `api.limit({refresh:true})` `api.ts:1488` | `GET /api/limit?refresh=true` | `routes.py:904` → `scheduler.fetch_pool` | ❌ | ✅ |
| 板块页 刷新 | `api.ts:1501` | `GET /api/sector-fund-flow?refresh=true` | `routes.py:933` → **直接** `sector.get_sector_flow` + `db.save` | ❌ | ❌ ⚠️**旁路** |
| 定时任务页 两个开关 | `api.setSchedule()` `api.ts:1796` | `POST /api/jobs/schedule` | `routes.py:155` → `set_enabled`/`set_push_enabled` | ❌ | ❌ |
| （前端**未接**） | — | `POST /api/refresh` | `routes.py:1956` → `run_light` | ✅ | ✅ |

- 互斥：`/jobs/run` 先查 `_state.running` 回 **409**（`routes.py:131`）；`/main-force/refresh` 有独立的 `_MF_STATE` 409（`:1213`）。
- `run_light` **故意不占 `_state`**，与完整流水线可并存（`scheduler.py:379`）。
- `POST /api/refresh` 仓库内无调用方：前端 grep 无命中，飞书指令直接调 `scheduler.run_light`（`feishu.py:1116`）、`run_post_close`（`feishu.py:1097`）。
- `GET /api/limit` 库里没数据时会**顺手抓一次**（`routes.py:911-913`），也是隐式联网点。

### Q2 每个 API 最终调用了哪些采集函数？

```
POST /jobs/run ─┐                                （完整流水线，后台线程 12-15 分钟）
POST /refresh ──┤                                （轻量，同步秒级）
cron 16:30 ─────┼→ run_post_close / run_light
feishu ─────────┘      │
                       ▼
        _collect(target, force, scoring)   scheduler.py:227
          ├─ for POOL_KINDS:  fetch_pool(k)          :116 → _collect_domain("pool_zt|zb|dt|strong")
          │      └─ ztpool.get_zt_pool / get_zb_pool / get_dt_pool / get_strong_pool（ak.stock_zt_pool_em 等 4 只）
          ├─ for SECTOR_KINDS: fetch_sector_flow(k)  :123 → _collect_domain("sector_flow_industry|concept")
          │      └─ sector.get_sector_flow()（同花顺主 + 东财备）
          └─ scoring=True: 先查 db.latest_date("main_force")==target 跳过闸门 :255
                 └─ _score_and_record(target) :266 → _collect_domain("main_force", store=False)
                        └─ mainforce.run(date, refresh=True)
                              ├─ quotes.fund_flow_rank()      ← ak.stock_individual_fund_flow_rank ×4 窗口
                              │    └─ 备源 quotes.fund_flow_individual()（同花顺）
                              ├─ quotes.stock_fflow_daykline() ← 东财 daykline，**逐股**
                              ├─ quotes.daily_history() ×300   ← ak.stock_zh_a_hist，**逐股**
                              └─ backfill_inflow()             ← daykline，**逐股**，带 budget/冷却

POST /pipeline/collect/retry → run_retries()  scheduler.py:396
      → db.ingest_due()（先按退避筛到点的）→ 按域分派 fetch_pool / fetch_sector_flow / _score_and_record

POST /main-force/refresh → **直接 mainforce.run()**（routes.py:1228）不经 _collect → 不写 ingest_job
GET  /sector-fund-flow?refresh → **直接 sector.get_sector_flow + db.save**（routes.py:940-941）同上
POST /pipeline/backfill → backfill.backfill()（日线）+ backfill_factors（因子）→ 只写 daily_bar/index_daily
GET  /pipeline/ingest|coverage|gaps|bars|backfill/plan|backfill/domains → 纯读，零联网
```

- `_collect` 每步独立 `try`，失败不外抛，返回三元组 `(results, failed, partial)`（`:229-240`）。
- 主力域的 `store=False` 是硬要求：`mainforce.run()` 落的是包型 payload，走 `ingest.record` 落库分支会被裸行列表覆盖（`scheduler.py:269-275`）。

### Q3 每类数据分别写入哪些数据库表？

采集链路只涉及 **4 张状态/数据表**（全库 22 张，其余 watchlist / fund_* / research_* / cmd_item / push_log / subscriber / pay* / invite_token 与采集无关）：

| 数据 | 表 | 写入处 | 幂等键 |
|---|---|---|---|
| 涨停/炸板/跌停/强势池 | `snapshot`（`dataset=pool_*`） | `ingest.record` → `db.save` `ingest.py:168` | `PRIMARY KEY(dataset, trade_date)` 覆盖 |
| 板块资金流（行业/概念） | `snapshot`（`sector_flow_*`, `trade_date="latest"`） | `_collect_domain` 同上；**旁路**在 `routes.py:941` | 同上 |
| 个股资金流（打分顺带落） | `snapshot`（`stock_net_inflow`） | `mainforce.py:404` / `:485`（`store` 路径外） | 同上 |
| 主力打分结果 | `snapshot`（`main_force`, 包型） | `mainforce.py:1330` `save_one` | 同上 |
| 历史日线 | `daily_bar` | `backfill.py:442` `bars_upsert` = `ON CONFLICT DO NOTHING` | `PRIMARY KEY(code, trade_date)` **不覆盖** |
| 指数日线 | `index_daily` | 同上 | 同上 |
| 逐域采集状态 | `ingest_job` | `db.ingest_start/ingest_finish/record_failure` | `UNIQUE(domain, trade_date)` 覆盖 |
| 整轮任务状态 | `job_run` | `db.job_start/job_finish` | 自增 id，每轮一条 |
| 开关/配置 | `kv` | `set_enabled` / `set_push_enabled` | `key` |

列定义：`snapshot`(`db.py:28`)、`job_run`(`:37`)、`kv`(`:59`)、`ingest_job`(`:328`)、`daily_bar`(`:358`)、`index_daily`(`:376`)。
覆盖审计**只看 `snapshot`**（`coverage.py:69` 的 `_SNAP_KINDS` 8 个 dataset），`daily_bar` 的覆盖走 `/pipeline/bars` 另算。

### Q4 当前是否保存完整的原始日线历史？—— **否**

| 维度 | 实测（只读真库） |
|---|---|
| 行数 | `daily_bar` **484,374** 行 / **2,002** 只 / **243** 个交易日（20251016..20261016） |
| 覆盖 | 相对 4,813 只名单：**covered 1,842 · partial 158 · absent 2,813**，`rate = 0.3827`（`GET /pipeline/bars`） |
| 计划 | `already_done=1,986` / `to_fetch=2,827`（`GET /pipeline/backfill/plan`, scope=all, start=20251016）；`BLOCKED=[]` |
| 因子 | `adj_factor` **484,276 / 484,374** 已填，**98** 行 NULL（判不了除权，如实留空） |
| 指数 | `index_daily` 仅 **48** 行（20260803..20261015）—— 243 天窗口缺约 195 天，**基本没跑** |
| 进度 | 全市场回填熔断停在 **21/49 批**（源端封禁 `RemoteDisconnected`），**剩 2,813 只一行都没有** |

- **本来就没有**：此前项目根本没有日线历史表 —— `daily_bar` 是上一轮任务书阶段 4 才建的；此前 `quotes.daily_history()` 是按需抓取、内部写死 `adjust="qfq"`、**不落库**（口径见 `db.py:353-357`）。
- **口径已正确**：不复权 OHLC + 独立 `adj_factor`，`ADJUST=""` 常量且 AST 断言钉死（`_backfill_check` 81 断言），已有行情 `ON CONFLICT DO NOTHING` 不覆盖。
- **窗口只有一年**（20251016 起），不是全生命周期历史。

### Q5 资金流向使用哪些 AKShare 接口？

| 接口 | 位置 | 角色 |
|---|---|---|
| `ak.stock_individual_fund_flow_rank` | `quotes.py:257` | **主源·批量排名**，`indicator` 今日/3日/5日/10日 各 1 次 |
| `ak.stock_fund_flow_individual` | `quotes.py:226` | 同花顺备源（东财不可用时） |
| `ak.stock_fund_flow_industry` / `concept` | `sector.py:35/37` | 板块资金流**主源**（同花顺） |
| `ak.stock_sector_fund_flow_rank` | `sector.py:43` | 板块备源（东财） |
| `ak.stock_sector_fund_flow_hist` / `concept_fund_flow_hist` | `sector.py:75/78` | 单板块历史 |
| `ak.stock_individual_fund_flow` | — | **不用**：本机 https 恒 `RemoteDisconnected`（`quotes.py:333`） |
| 东财 push2his daykline（**http 直连**） | `quotes.py:317/324` | 逐日个股明细，替代上面那个不可用的接口 |
| `ak.stock_zh_a_spot_em` | `quotes.py:155` | 全市场快照（打分输入，**不落库**） |

### Q6 是逐股调用还是批量排名？—— **排名批量，明细逐股**

- **批量**：`fund_flow_rank` 1 次请求返回全市场排名 → 4 次/轮（4 个窗口）；板块每类 1 次 → 2 次/轮。代码侧 `df.iterrows()` **不截断**，全量落库。
- **逐股**：① 打分的逐日明细 `stock_fflow_daykline` 每股 1 次（`backfill_inflow`，带 `budget_sec`/`max_codes`/600s 冷却）；② 打分的日线 `daily_history` 每股 1 次，`fetch.preselect_top: 300` 只 ≈ **5-13 分钟**（`scorer.yaml:78-79`）。
- **覆盖范围如何记录**：接口契约**未断言**"返回全市场"，靠行数推断 —— `stock_net_inflow` 正常日 4463~4782 条，低于历史峰值 **60%** 判 `PARTIAL`（`coverage.py:120` `_PARTIAL_RATIO=0.6`；实例：20261008 只有 1143、20260929 只有 30）。其余 7 个 dataset **按日期缺口**判，不按条数（涨停数天然逐日不同，`coverage.py:111-114`）。

### Q7 16:30 调度器是否已实现？由什么进程维护？

**已实现**，且与手动按钮是**同一个函数**（`kind` 区分 `scheduled/manual/feishu`）：

- APScheduler `BackgroundScheduler` **进程内后台线程**，FastAPI lifespan 里 `scheduler.start()`（`main.py:32` → `scheduler.py:495`）；**无持久化 jobstore**。
- 两个触发点共用一个 scheduler：16:30 `_cron_fire`（`settings.fetch_hour/fetch_minute`）、14:50 `_push_fire` 估值推送；参数 `coalesce=True, max_instances=1, misfire_grace_time=3600`（`:528-530`）。
- 交易日闸：非交易日跳过，**日历不可用也跳过**（宁可不跑，`:455-460`）。
- ⚠️ **当前 kv `scheduler_enabled='0'`（显式关）、推送开关缺省 `0` → 两个 job 都不装，`start()` 打一行日志就 `return`**（`scheduler.py:510-515`）。启动时只有 `ingest.startup_check()` 跑过（零联网、只打日志）。

### Q8 后端停止后任务是否会漏掉？如何发现？是否会自动补跑？

- **会漏**：调度器在后端进程里，进程停了 16:30 不触发；`misfire_grace_time` 只在进程活着时有意义。**当前调度本来就是关的 → 现在完全靠手动**。
- **能发现**：`ingest.startup_check()`（`ingest.py:231`）启动时查 30 日漏采 + 到点重试，**只打日志**；`_warn_if_missed_today()`（`scheduler.py:560`）过点没打分也**只提醒**。
- **不会自动补跑** —— 这是刻意的设计（未经告知不联网）。补跑只有两条路：页面「立即执行」/「补跑重试」，或把调度打开。
- 逐域状态**不丢**：`ingest_job` 是 SQLite 持久表，重启后 `run_retries` 仍按 `next_retry_at` 续跑（但没人点就一直躺在那）。

### Q9 当前任务状态如何记录？是否支持幂等、失败重试、启动恢复？

**两层状态**：

| 层 | 表 | 语义 | 列 |
|---|---|---|---|
| 整轮 | `job_run` | 一次流水线一行 | `id, kind, started_at, finished_at, status, detail(JSON)` — **无 `trade_date` 列**（藏在 detail 里） |
| 逐域 | `ingest_job` | `(domain, trade_date)` 一行 | `id, domain, trade_date, status, attempts, max_attempts, next_retry_at, processed, stored, failed_rows, error, started_at, finished_at, updated_at` — **无 `source` 列** |

- **状态机五态**：`WAITING → RUNNING → SUCCESS / PARTIAL / FAILED`（`db.py:332-334`）。`PARTIAL` = 跑成了但闸门判数据不全（0 行 / 缺字段），`FAILED` = 没跑成或重试用尽；两者**不合并**（`scheduler.py:288-293`）。
- **整轮判定**：`failed ≥ results → failed`；`failed 或 partial → partial`；否则 `success`（`_finish` `scheduler.py:294-300`）—— 修的正是 job_run #18 `{"pool_zt":0}` 却记 success 的坑。
- **三级计数**：`processed`（源回几行）/ `stored`（落库几行）/ `failed_rows`（校验没过几行），缺一不可（`db.py:341-346`）。
- **重试**：`attempts/max_attempts=3`，退避 `15s → ×2 → 封顶 300s`，`next_retry_at` NULL = 不再重试；`run_retries()` **只跑到点的**，先按退避筛再挑域，绝不绕过退避（`scheduler.py:414-417`）。
- **幂等**：`snapshot` 主键覆盖 UPSERT；`daily_bar` `ON CONFLICT DO NOTHING` 不覆盖（要重写必须显式 `bars_overwrite`）；`ingest_job` `UNIQUE(domain, trade_date)` 覆盖。
- **互斥**：`run_post_close` 持 `_lock` + `_state.running` → 409；`run_light` 不占锁；`main-force/refresh` 有独立 `_MF_STATE`；跨进程只靠 SQLite `busy_timeout`。

**任务书字段对照（缺口标 ❌）**：

| 任务书要求 | 现有 | 判定 |
|---|---|---|
| task_id / task_type / trade_date | `ingest_job.id` / `domain` / `trade_date` | ✅ |
| status 五态 | `WAITING/RUNNING/SUCCESS/PARTIAL/FAILED` | ⚠️ 语义齐、**词不同**（G3） |
| attempted_count / retry_count | `attempts`（含首跑，语义略偏） | ✅ |
| success_count / failed_count | 行级 `stored` / `failed_rows`（非任务级） | ⚠️ 近似 |
| error_message | `error` | ✅ |
| **source** | **`ingest_job` 无此列**（只写进 `snapshot.source`） | ❌ G2 |
| 数据覆盖范围及校验结果 | coverage **独立计算，不落 `ingest_job`** | ❌ |
| trade_date（整轮层） | `job_run` 无列，埋在 detail JSON | ❌ G4 |

**旁路缺口（G1/G5）**：

| 路径 | `job_run` | `ingest_job` |
|---|---|---|
| `/jobs/run`、`/refresh`、cron、飞书 | ✅ | ✅ |
| `/pipeline/collect/retry` | ❌ | ✅ |
| `/limit?refresh=true` | ❌ | ✅ |
| `/main-force/refresh` | ❌ | ❌ **完全旁路** |
| `/sector-fund-flow?refresh=true` | ❌ | ❌ **完全旁路**（还绕过 0 行闸门） |
| `/pipeline/backfill` | ❌ | ❌ |

另：`run_light` 与 `run_post_close` 并存时，同一 `(domain, date)` 会互相覆盖 `attempts/started_at`（没有域级锁）。

### Q10 评分依赖哪些字段？字段缺少时如何处理？

**依赖**（`scorer.yaml`）：六维权重合计 100 —— `inflow_consistency 30 / volume_up_price_flat 20 / large_order_ratio 15 / price_position_low 15 / turnover_moderate 10 / chip_convergence 10`。
输入 = 全市场快照候选（price/pct_chg/amount/float_mv/list_date/turnover）+ 400 日日线（`fetch.history_days:400`，仅预选 300 只）+ `stock_net_inflow` 逐日序列（net_inflow / large_order_net）+ 池子数据（粗筛剔涨停/跌停池）。
粗筛 `coarse_filter`（ST/停牌/上市<60日/流通市值 15 亿~2000 亿/成交额<500 万）+ 负面过滤 `negative_filter`（涨停池、连板≥2、20 日涨幅≥40%）。

**缺字段的四级处理**（逐级兜底，绝不补 0）：

1. **单元格缺失** → 百分位归一化保持 `NaN`，不参与排名（`mainforce.py:143`）。
2. **单维异常** → 该维记 `degraded`，不拖垮整轮（`:947-953`）。
3. **维度覆盖率 < 10%** → **整维弃用 + 其余权重重归一化**（`:958-964`；`scorer.yaml:85`）。
4. **票级**：`weighted += series.fillna(0.0) * (weight/total_weight)`，同时 `coverage += valid * (weight/total_weight)`，`final = weighted.where(coverage > 0)`（`:974-982`）→ 缺失维按 0 计入加权、有效权重占比由 `coverage` 字段披露；**全缺不输出**。

**披露**：结果带 `degraded`（哪些维弃用）与 `used_weights`（实际参与的权重），子证据缺失时按剩余子权重归一化（`mainforce.py:522-523`）。
**采集侧前置闸门**：`_collect_domain(required=(...))` 缺字段 → 计入 `failed_rows` → 域判 `PARTIAL`，**只报不改**。

⚠️ **风险（打到阶段 7）**：域判 `PARTIAL` 时**打分照跑、照出正式排名**，没有"数据不全就不发正式榜"的任务级拦截 —— 只靠 `degraded`/`coverage` 让读者自己发现。

---

## 2. 与旧任务书的边界（不重建）

上一轮任务书（M7 阶段 0-8 + 管线阶段 2-7）已交付的**全部保留**，本任务书只在它之上补缺口：

| 已有资产 | 位置 | 本任务书怎么用 |
|---|---|---|
| 覆盖审计五态 + 端点 | `app/pipeline/coverage.py` + `/pipeline/coverage|gaps` | 阶段 2 把结果**落进任务状态**（G 缺口），不重写判据 |
| 采集状态机 + 退避 | `app/pipeline/ingest.py` | 阶段 2 补 `source` 列、收编旁路 |
| 回填分批/跳过/熔断/断点续跑 | `scripts/backfill_run.py` + `pipeline/backfill.py` | 阶段 3/5 只补指数、失败区间重试、暂停恢复 |
| 十二份仓库回归 | `scripts/tests/_*.py` | 阶段 7 增补断言，不改既有基线 |
| 采集闸门三态 | `_ingest_check` 85 断言 | 原样沿用 |

**待修的既有缺陷（第一阶段未动，需授权窗口）**：`scripts/backfill_run.py:65` 的 `log()` 对 GBK 控制台 `print` 含 emoji（`:157` 🛑）→ `UnicodeEncodeError`，退出码 1，**熔断那行没进日志**（收尾统计已落文件）。回填续跑前应顺手修。
