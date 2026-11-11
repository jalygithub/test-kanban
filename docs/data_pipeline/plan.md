# 分阶段实施计划 — 数据管线七阶段

> 2026-10-17 · 与 `current-flow.md` 配套。**任务书原文已到齐**（本文件 §0 起的"原文要点"逐条压缩自你贴的七段）。
> **审核通过前不改任何业务代码、不做全市场请求。** 每阶段收尾交：修改文件清单 / 测试结果 / 表变更 / 未解决问题 / 下一阶段计划。
>
> **2026-10-17 收尾状态**：阶段 **2-7 已全部实施并验收**（各节「已实施」块 + §8 决定 #1-#10），22 份回归 + `tsc`/`oxlint`/`vite`/`compileall` 全绿。真实回填仍按 `audit.md` §12 决策 1 **不在内网执行**；git 零提交。

---

## 0. 执行方式与已定决定

任务书指定顺序：**先统一任务服务 + 数据质量校验 → 再每日增量采集 → 最后历史回填 + 前端管理**。
落到编号：**2 → 3 → 4 → 5 → 6 → 7**（第四阶段调度建立在任务服务之上，排在采集后；5/6 收尾）。

| 已定（2026-10-17） | 内容 |
|---|---|
| 回填路线 | **已定（2026-10-17）：改走腾讯备源** —— 东财日线失败自动回退 `ak.stock_zh_a_hist_tx(adjust="")`，复权因子仍走现有新浪链路（`factor.py:92`，与东财无关）。证据见 §5：腾讯 `adjust=""` 的 close 与东财回填值**逐字段一致**、8 列字段不缺、新浪因子链健康。**整只票一次性同源不混源**，`daily_bar.source` 如实记 `tencent`；东财解封后抽样复核（1 只票 1 请求）。 |
| 本机 16:30 调度 | **保持关闭**，手动跑；代码只保证"开了就按新时间点跑" |
| 状态词 | 库内 `WAITING/RUNNING/SUCCESS/PARTIAL/FAILED` **不动**，API 层做 `pending/partial_success` 等**别名**（改枚举要动 12 份回归，收益只是名字一样） |

---

## 1. 总原则

1. **复用不重建**（任务书原话"禁止无理由重建数据库"）：`ingest.py` 状态机、`coverage.py` 判据、`backfill.py` 分批熔断、12 份仓库回归全部保留。
2. 不改既有基线：18 份回归 + `tsc` 0 + oxlint 恰 6 + `vite build` 0；判据是**条数与文件集合**，不是行号。
3. 缺失不补 0、未验证不标成功、外发只在人点了才发。

---

## 2. 阶段 2 — 统一任务服务

**原文要点**：每任务含 `task_id/task_type/trade_date/status/started_at/finished_at/attempted_count/success_count/failed_count/error_message/retry_count/source/数据覆盖范围及校验结果`；状态至少 `pending/running/success/partial_success/failed`；**网页按钮必须走统一服务、不得单独一套采集逻辑**；**相同任务不得并发重复**；写入幂等。

### 字段对照（原 `ingest_job` `db.py:328` vs 原文）

| 原文 | 现有 | 判定 |
|---|---|---|
| task_id / task_type / trade_date | `id` / `domain` / `trade_date`；`job_run` 另补 `trade_date` | ✅ |
| status 五态 | `WAITING/RUNNING/SUCCESS/PARTIAL/FAILED` + **`status_alias`** | ✅ 库内枚举不动，API 多一层任务书用词 |
| started_at / finished_at | ✅ | ✅ |
| attempted_count / retry_count | `attempts`（含首跑）；`retry_count = attempts-1` 由前端派生 | ⚠️ 见「未解决」 |
| success_count / failed_count | 行级 `stored` / `failed_rows` | ⚠️ 任务级另给（= 本轮是否成功 / 失败步骤数） |
| error_message | `error` | ✅ |
| **source** | **已补列** `ingest_job.source` | ✅ `em:ztb` / `10jqka/em` / `scorer` |
| **数据覆盖范围及校验结果** | **已补列** `coverage_rate` + `coverage_detail` | ✅ 本次校验通过率 + 逐项明细；**日期窗口**那一半留阶段 3 回填进 `coverage_detail.window` |

### 已实施（2026-10-17）

| 文件 | 改动 |
|---|---|
| `app/store/db.py` | `_SCHEMA` + `_migrate` 加 4 列（老行 NULL，不回填历史）；`ingest_finish` 支持 `coverage_rate/coverage_detail`（**失败重试不抹掉**上次算好的覆盖 —— COALESCE）；`job_start(trade_date=)` |
| `app/store/db.py` | **域级互斥**：`ingest_try_start()` 在**同一临界区**里判+置位（先查一眼再回去抓，中间那条缝里两个线程会一起过）；`ingest_busy()` 只读预判；`DomainBusy`；`RUNNING_STALE_SEC=1800` —— 超时即接管，否则一次崩溃会把这域这天永久锁死 |
| `app/pipeline/ingest.py` | `record()` 落 `coverage_rate = 通过校验行占比`（**0 行给 NULL 不给 0.0** —— 「没有可校验的行」≠「覆盖率为零」）与 `coverage_detail`；`API_STATUS` + `api_status()`（**认不出的状态原样回给，不硬说成 failed**） |
| `app/scheduler.py` | `_collect_domain` 改走 `ingest_try_start`（DomainBusy 在任何状态写入**之前**抛，绝不 `record_failure` —— 那会冲掉正在跑那一轮的退避）；`_run_step` 加 busy 分支（**记「跳过」，不进 failed 也不进 partial**）；`run_retries` busy 记 skipped + 起 `job_run`；新增 `score_and_capture()` 供流水线与页面按钮共用 |
| `app/api/routes.py` | 板块端点改走 `scheduler.fetch_sector_flow()`（原来直连 `get_sector_flow + db.save`，抓到空列表也照存、绕过 0 行闸门）；主力端点 worker 改走 `score_and_capture`；`/limit`、板块端点遇 busy 回 **409**（不是 502 —— 那会让人以为源挂了）；`POST /pipeline/backfill` 起 `job_run`（`finished=false` 的续跑各留一条）；`/pipeline/ingest` 每条带 `status_alias`；删掉已无用的 `sector` 导入 |

### 收编暴露的旧漏洞（白名单 6 → 8，是**收紧**）

`_accept_check` 的 AST 联网白名单漏了两条**一直就在联网**的路由：`/sector-fund-flow`（直连 `sector.get_sector_flow`）与 `/main-force/refresh`（直连 `mainforce.run`）—— 判据精确到被调属性名，这两个名字不在判据里，于是「加了不吭声」从未被发现。收编进任务服务后判据才命中，白名单补上并注明来由。

### 顺手修的两处跨日失效（与本次改动无关）

- `_pipeline_check` I5：`today = "20261016"` 写死，跨日后 `gaps()` 窗口右移一天、快照里没有的那天被列成漏采 → 改成按真实日期现算（与 `gaps()` 同口径：UTC、`d < today`）。
- `_search_check` #14：`nav_date == "2026-10-09"` 写死，源已推进到 10-16 → 改成「有日期且在最近 30 天内」。

### 表变更 / 测试 / 未解决

- 表变更：`ingest_job` 加 3 列、`job_run` 加 1 列；**真实 `market.db` 的迁移已执行**（列已存在，老行留 NULL）。
- 测试：`_ingest_check` **85 → 115**（互斥 13 条、覆盖范围 7 条、别名与任务记录 10 条）。全 20 份基线复验通过；`tsc` 0、`oxlint` 恰 6、`vite build` 0。
- 未解决：
  - `source` 取值口径复用 `eastmoney/10jqka/mixed`（不造第二套语义），但 `main_force` 记的是 `scorer`（它是打分产物，不是抓取源）—— 阶段 3 定稿时统一。
  - 任务级 `success_count/failed_count` 目前靠 `job_run.detail` 里的 `failed`/`partial` 列表派生，没有独立列；等阶段 4 页面真要用再定列名。
  - `retry_count = attempts - 1` 仍由调用方派生。

---

## 3. 阶段 3 — 每日增量采集（六个任务）

**原文要点**：`daily_market_sync` / `daily_fund_flow_sync` / `daily_universe_sync` / `data_quality_check` / `recovery_sync` / `historical_backfill`；日线先查完整性再更新、保存复权类型、四类校验、部分空数据不判成功；资金流**优先批量排名**、保存完整快照、只回前 N 必须记覆盖范围、评估逐股成本、缺失 NULL。

### 六任务映射（复用，不重建）

| 原文任务 | 现有 | 缺口 |
|---|---|---|
| `daily_market_sync` | 无独立任务；`quotes.spot()` 全市场快照**不落库** | 新增：落 `snapshot` 新 dataset（**用现有表，不建新表**） |
| `daily_fund_flow_sync` | 现在是 `mainforce.run()` **顺带**落 `stock_net_inflow`（不可单独跑/重跑） | 独立成任务，复用 `fund_flow_rank` 批量 + `_collect_inflow` |
| `daily_universe_sync` | 名单是从 `stock_net_inflow` 并集**推出来**的（plan 的 `scope=all`），无显式任务 | 显式化：名单来源与口径要定（见待拍板） |
| `data_quality_check` | `coverage.py` 五态全套 ✅，但**只算不落、不作为任务** | 包成任务（**零联网**），结果落阶段 2 的列 |
| `recovery_sync` | `run_retries()` ✅ | 包成任务，接进 `job_run` |
| `historical_backfill` | `backfill.py` + `backfill_run.py` ✅ | 包成任务记 `job_run`；补 `end` 日期 |

### 日线任务四条要求对照

| 要求 | 现状 |
|---|---|
| 先查当日完整再决定更新 | ✅ plan `already_done` + `ON CONFLICT DO NOTHING` 不覆盖 |
| 保存明确的复权类型 | ✅ schema 固定为**不复权 OHLC + 独立 `adj_factor`**，`ADJUST=""` AST 钉死 |
| 验证股票数量/交易日期/关键价格/重复记录 | ⚠️ 三态 + 因子率已有；**重复记录校验**与关键价格非零校验需补断言 |
| 部分空数据不得判成功 | ✅ `_backfill_check` 81（源回 0 行计 `failed`；熔断阈值 50%） |

### 资金流五条要求对照

| 要求 | 现状 |
|---|---|
| 优先批量排名 | ✅ `fund_flow_rank` 4 次/轮覆盖全市场（今日/3/5/10 日） |
| 保存完整快照 | ✅ `snapshot` 存全量 rows |
| 只回前 N 必须记覆盖范围 | ⚠️ 现在只靠**行数推断**（正常日 4463~4782，低于峰值 60% 判 PARTIAL）；需在任务状态里**显式记 processed vs 名单数** |
| 评估逐股历史资金流成本 | ✅ 已评估：`ak.stock_individual_fund_flow` 本机恒败 → 改东财 daykline 逐股，`backfill_inflow` 带 budget/冷却/600s |
| 缺失保持 NULL 不填 0 | ✅ `_ingest_check`/`_pipeline_check` 断言 |

### 表变更 / 测试 / 未解决 —— **已实施（2026-10-17）**

- **落点**：新增 `app/pipeline/tasks.py`（六任务注册表 + `run_task` 统一入口 + `status_report`），端点 `GET /api/pipeline/tasks`（零联网）与 `POST /api/pipeline/tasks/{task_id}`（**唯一触发口**）。
- 表变更：**无新表**。`snapshot` 三个新 dataset（`daily_market` / `fund_flow_rank` / `daily_universe`）已同步登记进 `coverage._SNAP_KINDS`·`_REQUIRED`·`_STEP_OF` —— 不登记等于「这个域根本没在采」在页面上根本看不见。
- 六任务落点：`daily_universe_sync` 的名单 = **并集口径**（已定：`stock_net_inflow` 并集 4,813 只）。不用全市场快照名单 —— 快照是打分输入、可能受候选过滤影响，并集才是「实际被采集覆盖过」的名单，与回填 plan 的分母一致。
  | 任务 | 落点 | 闸门 |
  |---|---|---|
  | `daily_market_sync` | `quotes.market_snapshot()` 整份落库 | `min_rows=3000`（低于即「只回了前 N 条」） |
  | `daily_fund_flow_sync` | 四窗口各一次批量请求；**只存「今日」全量** | 四窗口条数 / 名单数 / 覆盖比记进 `coverage_detail`；`min_rows` = 名单数×60% |
  | `daily_universe_sync` | `backfill.load_scope("all")` 并集（已定口径） | 名单为空判 PARTIAL |
  | `data_quality_check` | `coverage.coverage_report()` 回写 `coverage_detail.window` | **不动 `coverage_rate`**（那列是采集的字段校验通过率） |
  | `recovery_sync` | `scheduler.run_retries`（只跑到点的） | 沿用退避，不绕过 |
  | `historical_backfill` | `backfill.backfill()` | 需显式域 + 区间，`end` 缺省最新交易日 |
- **`end` 补上**：plan 与执行两个端点的 `end` 缺省由 `DEFAULT_START`（那是**起始日**）改为**最新交易日**（`_latest_trade_date()`），任务层同口径 —— 这是阶段 5 那条「页面 end 写死空串」的后端一半。
- **页面回填按钮收编**：`POST /pipeline/backfill` 改调 `tasks.run_task("historical_backfill", ...)`，任务记录 kind 由 `backfill` 变成 `historical_backfill` —— 登记口只剩一个，响应形状不变。
- 顺带收紧两处：① `ingest_job.source` 现在由 `record()` 真正落库（原先只写进 `coverage_detail` 的 JSON，页面没法按域筛「哪些是东财来的」）；② `GET` 侧一律走离线推日期（`cached_trade_dates()`），不让零联网端点为了「今天几号」去拉日历。
- 测试：`_ingest_check` **115 → 151**；`_pipeline_check` I4 的域数上界改跟 `_SNAP_KINDS` 注册表走；联网白名单 **8 → 9**。
- 未解决：
  - `daily_market` 一天约 1 MB（5,400 只 × 全字段），一年 ~360 MB。要不要加保留期（如只留 90 天），等阶段 6 页面真用了再定。
  - 名单口径的已知代价：从没上过资金流榜的票进不了 `daily_universe`（属名单来源问题，不是这一层能修的）。
  - `recovery_sync` 与 `/pipeline/collect/retry` 各留一条任务记录（`recovery_sync` + `retry`），两种入口两个粒度，暂不合并。

---

## 4. 阶段 4 — 自动调度与恢复 —— **已实施（2026-10-17）**

**原文要点**：保留按钮；增加收盘后自动任务、有上限重试退避、启动后近期漏采检查、指定日期/数据域补跑、任务互斥与 SQLite 写保护、**最近一次成功交易日及数据质量状态**；建议 16:30 起并在后续时间段检查重试；**先确认现有调度器与进程生命周期，避免重复调度器**。

| 要求 | 落点 |
|---|---|
| 保留按钮 | ✅ 未动 |
| 收盘后自动任务 | ✅ 16:30 `CronTrigger`（`coalesce/max_instances=1/misfire_grace=3600`）；**kv 开关保持关闭**，`start()` 幂等 = 开了就装、关了一个 job 都没有 |
| 有上限重试 + 退避 | ✅ `attempts/max_attempts=3`，15s→×2→封顶 300s（未改） |
| 启动后近期漏采检查 | ✅ `startup_check()`（30 日漏采 + 到点重试，**只报不补**，零联网） |
| 指定日期/数据域补跑 | ✅ `/jobs/run?date=`、`/collect/retry?domain=` |
| 任务互斥 | ✅ 阶段 2 补齐：轮级 `_state` 409 + 域级 `ingest_try_start` |
| SQLite 写入保护 | ✅ `busy_timeout` + WAL；阶段 2 域级锁后闭环 |
| **后续时间段检查重试** | ✅ **新增 `RETRY_CRON = ((17,30),(9,20))`** → `scheduler._retry_fire()` |
| **最近一次成功交易日及数据质量状态** | ✅ **新增 `GET /api/pipeline/health`** → `ingest.health_report()` |
| 避免重复调度器 | ✅ `start()` 幂等；进程生命周期已审计（lifespan 起停、进程内线程、无持久 jobstore） |

**决定（2026-10-17 用户拍板）：触发点「只检查不外发」。** 任务书原文写的是「**检查**重试」，AGENTS.md 硬口径是「任何定时器都不碰会联网的补跑入口」—— 两者取交集：
`_retry_fire()` 只读 `ingest_due()`，结果落 kv（`retry_check`），页面据此显示「有几条到点、上次检查是什么时候」；真跑仍只有人点「补跑重试」一条路。
`ran` 恒 `False` 是**写进接口的字段**，不是只写在注释里。

### 落点 / 表变更 / 测试 / 未解决

- `app/scheduler.py`：`RETRY_CRON`、`_retry_fire()`（零联网）、`last_retry_check()`；`start()` 在 `jobs_on` 下装 `retry_check_1730` / `retry_check_0920` 两个 job —— **与盘后任务同一个开关**，不会出现「页面显示关着、其实还有 job 在跑」；`schedule_info()` 多给 `retry_points` / `next_retry_check` / `retry_check`。
- `app/pipeline/ingest.py`：`health_report()` —— ① 最近一次「**当天有记录的域全 SUCCESS**」的交易日（`PARTIAL` 不算成功，另给 `covered/core/complete`，免得只跑两个域也被当成整轮全绿）；② 十一个核心域各自的**最近一行**（`status`/`status_alias`/`coverage_rate`/`source`/`window`），没采过的是 `collected=false` 而**不是**新造的第六个状态词；③ `retry_due`、`retry_check`。
- `app/api/routes.py`：`GET /api/pipeline/health`（零联网，已进 `_accept_check` 的显式零联网清单 `_zero`）。
- `app/pipeline/tasks.py`：**修真 bug** —— `_status_of` 原先只认 `ingest` 的大写、又去数 `run_retries` **根本不返回**的 `retried` 键，于是**补跑全挂时 `job_run` 恒 `success`**。现在大小写两头都认；回填 `finished/requested` 那条口径不变。
- 表变更：**无新表、无新列**（检查记录进 `kv`）。
- 测试：`_job_progress_check` **33 → 49**、`_ingest_check` **151 → 170**、`_accept_check` **40 不变**（只把 `/pipeline/health` 点进 `_zero`）。
- 未解决：
  - 退避（15s/封顶 300s）在「只检查不外发」下，实际补跑节奏由**人点的时机**决定，不会精确按 15 秒重试 —— 这是该决定的自然结果，不是缺陷。
  - `run_retries(kind=...)` 这个形参**当前没被使用**（`job_run.kind` 恒为 `retry`）；动它会牵动 §3 那条「两种入口暂不合并」的决定，留到合并时一起处理。
  - `GET /pipeline/health` 的 `window` 要等 `data_quality_check` 跑过才有值（`None` = 没查过）—— 页面要不要在 `None` 时主动提示「先跑质量检查」，属阶段 6 展示层。

**不做**：启动自动补跑、跨进程补跑（未经告知不联网是既有设计，原文只要求"能发现"）。

---

## 5. 阶段 5 — 历史数据回填

**原文要点**：指定 start/end/数据类型；分批、已有完整记录跳过、**失败区间单独重试**、**暂停与恢复**、进度与错误查看、**完成后重跑覆盖审计**；先全市场日线、再资金流历史；不得假定各接口历史长度相同。

| 要求 | 现状（2026-10-17 阶段 5 实施完） |
|---|---|
| 指定 start/end/domain | ✅ 页面补了「结束日」输入框（留空 = 最新交易日）；端点缺省已是 `_latest_trade_date()` |
| 分批执行 | ✅ `--batch`，批末熔断 |
| 已有完整记录跳过 | ✅ `already_done` + `ON CONFLICT DO NOTHING` |
| **失败区间单独重试** | ✅ `db.bar_gaps()`（首末日**之内**挖洞，按连续段归并）+ `plan/backfill(only_gaps=True)`：只按缺口那一段发请求，**一天不缺一次请求都不发**；入口 = GET/POST 的 `only_gaps` 参数 + 跑批脚本 `--gaps` |
| **暂停与恢复** | ✅ `backfill(should_stop=…)` 在**票边界**停（`paused=True`、`ran` 如实变小）；跑批脚本 Ctrl+C 第一次 = 优雅暂停、第二次 = 立即走人；`:65` 的 emoji/GBK 崩溃已修（`log()` **先落文件后打印** + `_safe_print` 换编码硬打） |
| 进度和错误查看 | ✅ `/pipeline/bars` 三态 + `/backfill/plan`（含 `holes` 断档明细）+ `run/backfill.log` |
| **完成后重跑覆盖审计** | ✅ `backfill()` 收尾自动算 `coverage`（表域另加 `factor_coverage`）放进返回值 → 跑批日志与 `job_run.detail` 都看得到（经 `_summary` 保留 `coverage`/`factor_coverage`/`sources`） |
| 先日线后资金流 | ✅ 现状即如此（资金流历史未做）；资金流历史属后续，不与日线混跑 |
| 不假定接口历史长度 | ✅ 每只票独立 `ON CONFLICT`，长度差异自然体现 |

**口径偏差（有意为之，记在这里免得后人以为是漏了）**：任务书说「落任务状态（阶段 2 的列）」，但阶段 2 那几列长在 `ingest_job` 上，而那张表按 `(domain, trade_date)` 记**采集** —— 回填不是采集域（一张 `ingest_job` 行都没有）。所以覆盖审计落 `job_run.detail`（阶段 3 的统一任务入口），不动 `ingest_job`。

**另一条已知代价**：`only_gaps` 会把**停牌日**当缺口反复补（源对停牌日回空，下次照样报缺）。这是显式入口才有的代价，普通回填不走它（按首末日跳过，口径没变）；`bar_gaps(universe=交易日历)` 可以把停牌日豁免掉。

### 阶段 5 增补项：腾讯备源（2026-10-17 定）—— 已实施

| 文件 | 改动 |
|---|---|
| `app/pipeline/backfill.py` | `fetch_daily` 东财失败/空 → 回退 `ak.stock_zh_a_hist_tx(adjust=ADJUST)`；字段映射**复用** `quotes._hist_from_tx`（`_tx_rows` 只做列名对齐，turnover 小数 ×100、pct_chg 按收盘推算都在那边） |
| `app/pipeline/backfill.py` | 两源都挂 → 抛 `FetchError(source=...)`，消息里两个源的错都在；`_backfill_check` K1/K2 对**两条路径各钉一次** AST 断言（`adjust` 只能是 `ADJUST`/空串，**绝不 qfq**） |
| `app/pipeline/backfill.py` | `daily_bar.source` / `index_daily.source` 如实记 `eastmoney` / `tencent` / `sina`；**整只票一次性同源**——回退发生在票级，不在票内逐日切换；返回值带 `sources` 分账（items/rows/inserted/failed） |
| `scripts/backfill_run.py` | `_log_sources` 把两源计数摊开打（熔断阈值仍 50%/25%），收尾也打一次；`default_start()` 改回 `DEFAULT_START`（20260801，不再「一年前」） |
| 复核 | ⏸ 东财解封后抽样 1 只票 1 请求，比对腾讯与东财的同日 OHLC（内网不外发，等解封） |

**因子不动**：`factor.py` 的新浪 `stock_zh_a_daily(adjust="hfq-factor")` 与东财无关，2,813 只照样能补因子。

**2026-10-17 探测矩阵**（每次 1-2 个请求，零写库）：

| 路径 | 东财日线 `ak.stock_zh_a_hist` | 同花顺 `stock_fund_flow_individual` | **腾讯日线** `stock_zh_a_hist_tx(adjust="")` | **新浪因子** `stock_zh_a_daily(adjust="hfq-factor")` |
|---|---|---|---|---|
| 直连 | ❌ 0.4s `RemoteDisconnected` | ✅ 5216 行 / 9.1s | ✅ 2.1s | ✅ 33 事件 / 0.6s |
| 走系统代理 7890 | ❌ `ProxyError` 同样被掐 | ⚠️ 21.4s 半途失败（非 HTML） | — | — |

**口径交叉验证（决定性证据）**：000001 平安银行 20251016 —— 腾讯 `adjust=""` 的 `close=11.54`，与库里**东财回填的不复权收盘价 11.54 完全一致**，开/高/低/量/额全对得上；`turnover=0.0065` 是小数（`_hist_from_tx:455` 已 ×100）；8 列字段**一个不缺**。
**因子链复核**：库里已回填的因子**不是全 1.0** —— 000001 为 145.92→154.03（窗口内 2 次除权、3 个不同值），全库 3,667 个不同因子、`=1.0` 仅 1,412 行（0.29%）、NULL 98 行 → 新浪 `1900-01-01` 哨兵行**已被正确处理**，前向填充方向没问题。

→ **不必引入国外源**（Yahoo 等无换手率/成交额、覆盖不全、且偏离任务书"使用现有 AKShare 历史日线接口"——腾讯与新浪**本身就是 akshare 接口**）。

**排查要点（别重复踩）**：CPython 的 `getproxies_registry()` **会过滤掉指向本机的系统代理**，所以系统代理开着时 `requests` 读出来仍是空的 —— 后端/回填进程**天然是直连**，不会悄悄走梯子。要走代理必须显式设 `HTTP_PROXY/HTTPS_PROXY`（当前**不设**，保持直连与"外发可审计"一致）。

---

## 6. 阶段 6 — 前端改造（原文已到）

> **原文**：保留原有"立即运行"按钮，增加：自动调度启停状态 / 最近一次成功日期 / 每类数据最新日期 / 数据覆盖率和缺失区间 / 任务进度及错误原因 / 指定日期补跑入口 / 历史回填进度。页面首次打开只读本地库，不触发网络采集；只有用户启动任务才访问外部数据源。
>
> 注：你先前单独贴的"数据管理页面"段与本段基本重叠，另多出「全量数据质量报告」——两条**都做**。

| 要求 | 现状 | 判定 |
|---|---|---|
| 保留"立即运行"按钮 | `Overview.tsx:211` / `Jobs.tsx:178` | ✅ |
| **自动调度启停状态** | 只在「定时任务」页，数据管理页没有 | ❌ 补（只读 `jobs/schedule`） |
| **最近一次成功日期** | 后端已给（阶段 4 `GET /api/pipeline/health`），前端未接 | ⚠️ 阶段 6 接 |
| 每类数据最新日期 | 只有 `first_date ~ last_date` 区间 | ⚠️ 补「最新」列 |
| 覆盖率和缺失区间 | 有覆盖率；缺失是**逐日列表** | ⚠️ 补区间合并（`fmtRanges()` 纯函数，域级 + 个股级） |
| **任务进度及错误原因** | 错误有；进度条只有回填有 | ⚠️ 补任务进度（`/jobs/status` 已有 step 进度） |
| 指定日期补跑入口 | ✅ 两处 | ✅ |
| 历史回填进度 | ✅ Progress + 三态 + 因子率 | ✅ |
| **全量数据质量报告**（前一段原文） | 覆盖五态、日线三态、因子率**分散两卡** | ❌ 补页内汇总卡（复用现有两份数据，**零新请求**） |
| 首次只读本地库 | ✅ `dataUsage` + 4 个 `GET /pipeline/*`，页面明示"全程零外网" | ✅ 硬约束保持 |

**硬约束**：`oxlint` 恰 6 条既有 `set-state-in-effect`，新代码不得新增（effect 里 `setState` 落 timer/`.then`）；红涨绿跌走 `signColor`；页脚免责声明；前端改动不用重启。
**测试**：`tsc` 0 / `oxlint` 恰 6 / `vite build` 0 + 一条**零联网断言**（初始路径不含三个联网按钮）。

### 已实施（2026-10-17）

| 要求 | 落地 |
|---|---|
| 自动调度启停状态 | **不开新端点**：`GET /api/jobs/status` 本来就带 `schedule`（含两个独立开关 + `next_run`/`next_push`），数据管理页只读展示。**口径偏差**：任务书写「只读 `jobs/schedule`」，实际后端只有 POST（改开关）；复用 status 里的同一份 `scheduler.schedule_info()`，零新接口 |
| 最近一次成功日期 | 新接 `api.pipelineHealth()`；总览卡显示 `last_success_date` + 覆盖 `N/M` 核心域，`null` 显示「还没出现过全域 SUCCESS」（PARTIAL 不算成功这句写进 hint） |
| 每类数据最新日期 | 覆盖表加「最新采集」列 = `health.domains` 的 `trade_date` + 状态 tag。**与「区间」里的 `last_date` 是两回事**：那是数据到哪天，这是最近一次采集记录在哪天 |
| 覆盖率和缺失区间 | `fmtRanges()` 进 `lib/format.ts`（纯整数日期算术、渲染期不碰 `new Date`、先排序再按日历日合并、非 YYYYMMDD 条目原样保留）；域级（覆盖表「缺失区间」列）+ 个股级（回填 `incomplete` 的「缺的日期」）共用 |
| 任务进度及错误原因 | `JobProgress` + `STEP_LABEL` 从 `Overview.tsx` 抽到 `components/JobProgress.tsx`，两页同一份实现；总览卡另给「上次运行」状态 tag / 耗时 / 失败步骤（`parseDetail(last.detail).failed`），错误详情指向「任务日志」卡 |
| 全量数据质量报告 | 新「数据质量总览」卡：最近成功日 / 自动调度 / 估值推送 / 到点待补跑 / 覆盖五态分布 / 日线回填 / 复权因子 + 任务进度，**数字全部复用本页已取的六份只读数据，零新请求** |
| 零联网断言 | 新脚本 `scripts\tests\_ui_offline_check.py` **38 条**（纯源码断言）：`loadPipe` 恰六个 GET、三个联网动作只在 `onClick`/`onConfirm`、`setSchedule` 不出现在本页、`fmtRanges`/`JobProgress` 各一份、汇总卡内 `api.` 出现 0 次 |

验证：`tsc -b` 0、`vite build` 0（chunk 提示是既有）、`oxlint` 恰 6（文件集合不变，Overview 行号因抽组件前移属正常）、21 份基线脚本全绿（含新增 `_ui_offline_check`）。

---

## 7. 阶段 7 — 验收（按原文 10 条）

| # | 原文 | 现有断言 | 缺口 |
|---|---|---|---|
| 1 | 按钮与自动调度调用**相同任务服务** | 无 | 阶段 2 收编后补（旁路 2 处是关键） |
| 2 | 同一天重复执行不产生重复记录 | `_ingest_check` / `_pipeline_check` | 补显式断言 |
| 3 | 部分成功不被标成完整成功 | `_ingest_check` 85（0 行闸门） | ✅ |
| 4 | 网络中断可以重试 | `_ingest_check`（退避 + `run_retries`） | ✅ |
| 5 | 重启后可发现近期漏采 | `_pipeline_check` 103 | ✅ |
| 6 | 回填支持断点续跑 | `_backfill_check` 81 | ✅ |
| 7 | 不完整数据不会被静默填成零 | `_ingest_check` / `_pipeline_check` / `_factor_check` | ✅ |
| 8 | **不完整数据不会生成误导性的正式选股排名** | **无** | ⚠️ **G8**，落地方式**已定：宽松方案**（照出 + 整榜标记），见下 |
| 9 | 不影响评分 / 涨停池 / 基金估值 / 数据清理 | 18 份回归 | 全绿复验 |
| 10 | 保留现有网络限制与回归 | `_accept_check` 40（联网路由白名单 **9 条**恒等、`GET /pipeline/*` 含 `/pipeline/health` 一律零联网） | 阶段 6 收编后**重跑白名单断言** |

### 第 8 条落地设计（已定：照出 + 整榜标记）

**原则**：不拦输出，但**让读者一眼看到这榜的数据不全**，且不给"看起来很正式"的错觉。

| 层 | 改动 |
|---|---|
| `app/metrics/mainforce.py` | `run()` 收尾查当天 `ingest_job` 各域状态（阶段 2 的表），任一域非 `SUCCESS` → 结果里加 `model.data_quality = {state:"incomplete", missing:[{domain,trade_date,status}], checked_at}`；全 `SUCCESS` → `{state:"ok"}` |
| `snapshot.main_force` 包型 payload | 新字段**加进 `model`**，不改既有键 → `replay_score` 可重放不受影响 |
| `frontend/src/pages/MainForce.tsx` | 榜顶 `Alert type="warning" showIcon`：「本榜基于不完整数据：缺 pool_zt(20261016)、stock_net_inflow 仅 1,143/4,813 条」；**整榜级标记**，不在每行重复打标 |
| 断言 | 域 `PARTIAL` → `data_quality.state=="incomplete"` 且 `missing` 含该域；全 `SUCCESS` → `"ok"`；**字段缺失时绝不写 0** |

**不做**：不拒绝出榜（会让人以为系统坏了）、不降权打分、不动 `scorer.yaml` 任何权重。

收尾：一次性更新过时文档（`docs/data_pipeline/audit.md` §12、`docs/backtest/implementation-audit.md:51,130`、本文件 §0），完成后向用户**一次性汇报全部结果**（是否重启由用户自己决定，不提醒、不代做）。

### 已实施（2026-10-17，验收 10 条全达成）

| # | 判定 | 落地 |
|---|---|---|
| 1 | ✅ 断言补上 | `_accept_check` H1-H5：`run_post_close` 一处定义、三入口（定时/按钮/飞书）；**`_collect` 每个调用点 AST 判宿主**，只许在 `run_post_close`/`run_light` 体内 —— 旁路（绕过 job_run 记账与 ingest 登记）以后出现即红 |
| 2 | ✅ 断言补上 | I1-I3：`ingest_try_start` 同域同天跑两轮只留一行（attempts 记 2）、`save()` 同 (dataset,date) 覆盖不新增 |
| 3-7、9、10 | ✅ 原断言全绿 | 21 份基线复验通过（含联网白名单 9 条恒等、`GET /pipeline/*` 零联网） |
| 8 | ✅ 宽松方案落地 | 后端 `ingest.rank_quality()`（见 §8 决定 #9）：任一**输入域**（七个采集域，扣掉打分不消费的三个同步域与榜单域自己）当天非 SUCCESS → `model.data_quality.state="incomplete"` + 逐域 `{domain, trade_date, status, coverage_rate}`；全 SUCCESS 或**没记录** → `ok`（与 `health_report` 同口径，没采过 ≠ 采失败）；`coverage_rate` NULL 原样带出**绝不补 0**。`mainforce.run()` 收尾加进 `model`（既有键不动、`replay_score` 不受影响、老快照无此键前端当没查过）。前端 `MainForce.tsx` 榜顶整块 Alert「本榜基于不完整数据」，**不在每行打标、不拦输出、不动任何权重**。断言 G1-G13 |

验证（阶段 6+7 合并跑）：`tsc -b` 0、`vite build` 0、`oxlint` 恰 6、`compileall` 0、**22 份脚本全绿**（`_accept_check` 40→61、`_ui_offline_check` 新增 38，其余基线数字不变）。

---

## 8. 决定记录（**待拍板项已清零**）

| # | 事项 | 决定（2026-10-17） |
|---|---|---|
| 1 | 回填路线 | **改走腾讯备源**：东财失败回退 `stock_zh_a_hist_tx(adjust="")`；因子仍走新浪链；整只票同源、`source` 如实记 |
| 2 | 本机 16:30 调度 | **保持关闭**，手动跑 |
| 3 | 状态词 | 库内枚举不动，**API 层别名**（`pending`/`partial_success` 等） |
| 4 | `daily_universe_sync` 名单口径 | **并集**（`stock_net_inflow` 并集 4,813 只，维持现状） |
| 5 | 验收第 8 条 | **宽松**：照出 + **整榜**打"数据不全"标记（不拦输出、不动权重） |
| 6 | `backfill_run.py:65` emoji/GBK 崩溃 | **已修（2026-10-17 阶段 5）**：`log()` 先落文件后打印 + `_safe_print` 换编码硬打；回归 `_backfill_check` O 段盯着 |
| 7 | 阶段 4 补跑触发点要不要**自动外发** | **只检查不外发**（2026-10-17 用户拍板）：17:30 / 次日 09:20 只读 `ingest_due()` 落 kv，跑仍由人点 —— 与任务书「后续时间段**检查**重试」和 AGENTS.md「任何定时器都不碰会联网的补跑入口」两头同时成立 |
| 8 | 阶段 5「落任务状态（阶段 2 的列）」落哪 | 落 `job_run.detail`（`_summary` 保留 `coverage`/`factor_coverage`/`sources`），**不动 `ingest_job`** —— 那张表按 `(domain, trade_date)` 记采集，回填不是采集域，硬塞会造出一行永远没有取数动作的「采集记录」 |
| 9 | 验收第 8 条的体检函数落哪 | **`app/pipeline/ingest.py` 的 `rank_quality()`**，不是 §7 设计表里写的 `metrics/mainforce.py` 内联 —— 它读的就是 `ingest_job`，放采集状态的归属地零依赖可测（`_accept_check` 直接调）；`mainforce.run()` 只剩一行接线。输入域取**七个采集域**（`RANK_INPUT_DOMAINS`），扣掉打分不消费的三个同步域与榜单域自己 |
| 10 | 阶段 6「自动调度启停状态」读哪个口 | **不开新端点**：`GET /api/jobs/status` 本来就带 `schedule`（同一份 `scheduler.schedule_info()`）。后端只有 POST `/jobs/schedule`（改开关），数据管理页只要**只读**展示 —— 复用 status，零新接口 |

→ **计划可进入审核**。审核通过后按 `2 → 3 → 4 → 5 → 6 → 7` 开工，每阶段交：修改文件清单 / 测试结果 / 表变更 / 未解决问题 / 下一阶段计划。
