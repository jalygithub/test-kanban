# 自动买卖策略 V1 — 现有代码审计报告

2026-10-18。依据 = **实际代码与实际库**：`backend/app` 下 45 个 `.py`、`backend/data/market.db`
22 张业务表（`sqlite_master` 逐表点过）、`routes.py` 93 条路由。计划文档不作为「已实现」证据。

状态词：**已实现 / 部分实现 / 未实现 / 无法确认**。

## 0. 结论速览

| # | 审计项 | 结论 |
|---|--------|------|
| 1 | 六维选股实现、输入、输出、评分 | **已实现**，直接复用其快照（§2.1） |
| 2 | 历史日线 / 复权 / 指数 / 股票状态 | **已实现**（日线+复权+涨跌停规则）；ST 历史**无法确认**、停牌无显式表（§2.2） |
| 3 | 策略配置 / 交易信号 / 账户 / 订单 / 成交 / 持仓 / 回测 | **未实现**（研究回测是统计不是记账，见 §2.3） |
| 4 | 任务调度、日期处理、交易日历 | **已实现**；日历有两个联网属性、只能用零联网入口（§2.4） |
| 5 | 未来泄漏 / 重复下单 / 重复记账 / 错误成交风险 | 逐条见 §2.5 |

## 1. 已实现（直接复用，不重写）

### 1.1 六维选股（已实现）

- **位置**：`app/metrics/mainforce.py` — 入口 `run()`（L1229），预筛 `_preselect()`
  （L1059），六维加权 `_score()`（L920），配置 `DEFAULT_CONFIG`（L61）/
  `load_config()`（L116），按日截断 `_asof()`（L810）。
- **输入**：`daily_bar` 历史 + `stock_net_inflow` 资金流 + `pool_*` 快照；
  权重（2026-10-16 快照实录）：inflow_consistency 30 / volume_up_price_flat 20 /
  large_order_ratio 15 / price_position_low 15 / turnover_moderate 10 /
  chip_convergence 10（`model.degraded` 会记被降级的维）；预筛：上市 ≥60 天、
  流通市值 15 亿~2000 亿、换手 3~12%、20 日涨幅 ≤40%、`top_n=30`、`preselect_top=300`。
- **输出**：`snapshot` 表 `dataset='main_force'`，payload = `[envelope]`，envelope =
  `{date, generated_at, params, rows[Top30], all_rows[297], model, stats, disclaimer}`；
  行字段 `code,name,industry,price,pct_chg,turnover,float_mv,amount,list_date,score,rank,
  coverage,dims(逐维{score,weight,label,short}),reasons`。`rank` 1..30 连续。
- **落库方式**：`ingest.record(store=False)` 包型 payload（取数方自己落库的域，勿二次 `db.save`）。
- **榜单质量门**：`app/pipeline/ingest.py` `rank_quality(trade_date)`（L461）只读
  `ingest_job`、零联网；输入域 `RANK_INPUT_DOMAINS`（L455）= pool_zt/pool_zb/pool_dt/
  pool_strong/stock_net_inflow/sector_flow_industry/sector_flow_concept；任一域有记录
  且非 SUCCESS → `state="incomplete"`。

### 1.2 行情与规则事实（已实现，附缺口）

- `daily_bar`：**484,374 行 / 2,002 只 / 20020101..20261016**，OHLCV + turnover +
  amount + pct_chg + `adj_factor`（候选票抽样 0 空）+ source/fetched_at；
  PK(code,trade_date)，`ON CONFLICT DO NOTHING` 不覆盖。
- `index_daily`：仅 `sh000300`、48 行 → 指数只够短窗，**不进 V1**。
- 复权：后复权因子落库；`research/engine.py` `price_adjust()`/`_factor()` 三档口径
  （RAW / UNADJUSTED / UNADJUSTED_UNCERTAIN，**缺因子回 None 不回 1.0**）。
- 涨跌停（规则事实）：`engine.limit_ratio()` 主板 10% / 创业板·科创板 20% / 北交所 30%；
  `engine._at_limit()` 四舍五入到分 + 半分容差，返回 `None` = 判不了。
- **停牌**：无显式停牌表；「当日无 bar」是可用替身（部分实现）。
- **ST**：历史 ST 状态不可得（`docs/backtest/implementation-audit.md`
  `historical_st_status=BLOCKED`）→ 只能用**信号日快照的 name 含 ST** 判当日状态。
- 交易日历：`app/fetchers/calendar.py` — `cached_trade_dates()`（L49，**零联网**）；
  `trade_dates()` / `last_trade_date()` 缓存超 30 天会朝新浪发包；缓存
  `DATA_DIR/trade_calendar.json` 覆盖至 20261231。`routes.py` 尾部
  `_latest_trade_date()` 已示范口径：零联网路径只准 `cached_trade_dates()`。
- 引擎口径可借鉴：`engine._ref_dates()` **刻意不用日历** —— 交易日 =「库里实际有
  行情的那些天」，避免日历有、行情没有的假观察点。

## 2. 未实现 / 部分实现

### 2.1 账户闭环（未实现 —— 本任务要新建的东西一件都没有）

- **22 张表逐一点名**：cmd_item, daily_bar, fund_estimate_log, fund_nav_log,
  fund_watchlist, index_daily, ingest_job, invite_token, job_run, kv, pay_order,
  pay_token, push_log, research_experiments, research_metrics, research_outcomes,
  research_runs, research_signals, snapshot, subscriber, subscriber_fund, watchlist。
  **无** strategy_params / trade_signal / account / order / fill / position 任何一种。
- **93 条路由**无 `/trading`、`/order`、`/account`、`/position`。
- **`backend/app` 45 个 py** 无 trading/strategy 模块。

**相近但不同**（部分实现，不许冒充闭环）：

| 东西 | 是什么 | 边界 |
|------|--------|------|
| `research/engine.py` `build_outcome()` | 研究回放：D+1 开盘入场、T+n 收盘出场、涨停买不进标 `UNFILLABLE`、停牌标 `NO_DATA` | **统计不是记账**：无账户、无持仓、无下单幂等 |
| `research_signals/outcomes/metrics` 表 | 实验产物 | 非交易指令，没有 BUY/SELL 语义 |
| `watchlist`（rank/score/watch_reason） | UI 自选榜 | 非持仓：没有数量、成本、可卖量 |
| `pay_order` | 订阅收银台订单 | 与股票交易无关 |
| 「T+1」现词义 | 基金估值复盘（`routes.py` T+1 复盘）与 `build_outcome` 的 D+1 入场口径 | **不是**账户可卖量控制 |

### 2.2 任务调度（已实现；本阶段不接入的原因）

`app/pipeline/tasks.py` `run_task()` 单入口六任务、`scheduler._collect` 三元组
`(results, failed, partial)`、`job_run(kind, trade_date)` 记录、`ingest_job` 五态
状态机都在。但六任务注册表被 `_pipeline_check` 钉死（域数上界 = `_SNAP_KINDS`
注册表），**本阶段不加进六任务**，走独立入口（API + 内部服务），避免回归连锁；
接入调度是下一阶段事项。

## 3. 任务要求的四类风险（逐条）

**未来数据泄漏**

- `engine.build_outcome()` 明确 D+1 开盘入场 —— 已实现的正确口径；
- `mainforce._asof()` 有按 `trade_date` 截断机制 —— 部分确认；**未逐维复核**，
  连同 `SURVIVORSHIP_NOTE`（股票池模式 A 幸存者偏差）记为无法确认；
- 研究快照只存单维百分位，因子原始值不可回溯（`engine.py` L1016 自述）；
- **对新代码的约束**：策略输入只能吃 `≤ signal_date`（进实现与测试，§见 strategy-v1）。

**重复下单 / 重复记账 / 错误成交**

- 现状：无下单、无记账功能 → 风险**不存在但也无保护可继承**；`research_runs`
  每次 run 独立成行（研究记录，不构成账户账本）；
- 新建层必须自带三道幂等：`strategy_run UNIQUE(strategy, signal_date)`、
  `trade_signal UNIQUE(run_id, code)`、`sim_order.signal_id UNIQUE` +
  `sim_fill.order_id UNIQUE`（设计见 `strategy-v1.md`）；
- 错误成交的正确示范已存在：涨停买不进不许默认买到、停牌不许当成交 —— 撮合层
  **引用** `engine.limit_ratio/_at_limit`，不另写一套。

**无法确认清单**：六维逐维历史回看的边缘用法（未逐维审计）；历史 ST（BLOCKED）；
券商真实费率（现用 `engine.DEFAULT_COST` 研究假设费率，标注 assumption=True）。

## 4. 本任务的复用 / 新建清单

**复用（引用不重写）**：`db.load_one('main_force', …)` 快照读取、`daily_bar`、
`engine.limit_ratio/_at_limit/price_adjust/DEFAULT_COST`、`ingest.rank_quality`、
`fetchers.calendar.cached_trade_dates`、回归脚本的夹具 + socket 封锁 + `ok()` 模式。

**新建**：7 张表（strategy_params / strategy_run / trade_signal / sim_account /
sim_order / sim_fill / sim_position）、`app/trading/`（params/strategy/
risk/paper/service）、7 条 `/api/trading/*` 路由（全部零联网）、回归脚本
`scripts/tests/_strategy_check.py`。

**前置条件结论**：候选（六维快照）、行情（日线+复权）、规则事实（涨跌停）、
质量门（rank_quality）齐备；缺的正是账户闭环与策略层 —— 与任务书判断一致，
新建表与既有 22 表**零冲突**，可以动手。
