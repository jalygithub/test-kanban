# 自动买卖策略 V1 —— 接口、参数、库表与完整模拟示例

> 实现位置：`backend/app/trading/`（params / strategy / risk / paper / service）+ `store/db.py` 七张表 + `api/routes.py` 七条 `/api/trading/*`。
> 审计报告：`docs/ai_trading_lab/implementation-audit.md`。回归：`scripts\tests\_strategy_check.py`（**156** 断言，全离线）。

## 0. 硬口径

- **零联网**：策略新代码不发任何外网请求；交易日历只准 `service.cached_trade_dates()`（缓存过期宁可 REFUSED 也不联网），行情只读 `daily_bar`，候选只读六维快照 `main_force`。
- **不实盘、不自动调仓**：只有人显式调 `POST /api/trading/signal/run` 与 `POST /api/trading/execute` 才推进；没有任何定时器碰它。
- **信号日收盘后生成，D+1 开盘价成交**；错过执行日 → `EXPIRED` 不追补；执行日行情永不到达 → 过期并自动跳下一个可执行日。
- **不交易必须显式**：每只候选/持仓要么 BUY/SELL 要么 HOLD（逐条 `reasons` 留痕）；整轮不跑则 run 级 `REFUSED` + 理由。
- **账户持仓只能由成交记账改写**：`sim_settle`（单事务：订单→成交→现金→持仓），策略层无权触碰现金。
- 幂等三层：`strategy_run UNIQUE(strategy, signal_date)` / `sim_order.signal_id UNIQUE` / `sim_fill.order_id UNIQUE`。

## 1. HTTP 接口（7 条，前缀 `/api`）

| 方法 | 路径 | 入参 | 返回要点 |
|---|---|---|---|
| POST | `/trading/signal/run` | `{date?: YYYYMMDD, params?: {覆盖项}}` | `{run_id, status: OK\|REFUSED, idempotent, signal_date, exec_date, risk_state, quality_state, signals:{BUY,SELL,HOLD}, orders:{placed,risk_rejected}, portfolio:{cash,position_value,equity,exposure,available}, refusal_reason?}`；参数未知键/越界 → **400** |
| POST | `/trading/execute` | `{exec_date?: YYYYMMDD}` | `{ran, exec_date, reason?, filled, rejected, expired, details:[{order_id,code,side,qty,status,price,fee,reason}]}`；缺省取最早待执行日，队列不卡死 |
| GET | `/trading/status` | — | `{account, positions, pending_orders, last_run, initialized}`（只读本地库） |
| GET | `/trading/runs` | `?limit` | `{items:[…]}`（不含 params_json 快照全文） |
| GET | `/trading/signals` | `?run_id` | `{items:[…]}`，**含 HOLD** |
| GET | `/trading/orders` | `?limit` | `{items:[…]}`（PENDING/FILLED/REJECTED/EXPIRED 全量） |
| GET | `/trading/params` | — | `{strategy, rule_version, defaults, version_of_defaults, notes}`（与表单同一份事实） |

**页面**：`/trading`（`frontend/src/pages/Trading.tsx`，左侧菜单「自动交易」）——
模拟盘操作台：账户/持仓卡、生成信号并下单、执行待成交订单、运行历史/信号明细/订单/参数只读视图。
首屏四个 GET 零联网；两个 POST 只在人的点击里（`_ui_offline_check` T 段钉死这条边界）。

信号行结构（`GET /trading/signals` 单条）：

```json
{"id": 7, "run_id": 1, "code": "600001", "name": "演示电子", "side": "BUY",
 "signal_date": "20270107", "exec_date": "20270108",
 "reasons": [{"rule": "trend_ok",   "detail": "收盘 10.30 >= MA3 10.200"},
             {"rule": "liquidity_ok","detail": "近 5 日日均成交额 100000000.00 >= 50000000"},
             {"rule": "size_ok",    "detail": "…仓位/现金/整手取小 20200 股"}],
 "target_qty": 20200, "alloc_pct": 0.1982, "est_price": 10.30,
 "rule_version": "rule-v1", "param_version": "d09a910a5aa7",
 "snapshot_date": "20270107", "bar_date": "20270107"}
```

`bar_date` 为 `NULL` = 该票在信号日无行情（缺行情不编造）；HOLD 的 `target_qty` 恒 `NULL`。

## 2. 判定顺序（读信号必备）

- **买入闸门**：风险态 → ST 名称 → 当日行情缺失 → 六维榜/分数 → 趋势（收盘≥MA，窗口不足=不交易）→ 流动性（近 5 日均额，窗口不完整=不可判不臆断）→ 已持仓/在途单重复 → 仓位（数量帽/暴露帽/单股权益帽/整手/现金）→ BUY。
- **退出五条**（任一命中即 SELL，全部写进 reasons；全不触发=HOLD hold_ok；无行情=HOLD bar_missing）：
  `stop_loss`（成本折算后 −8%）/ `trailing_stop`（峰值回撤 10%）/ `trend_break`（收盘<MA10）/ `max_hold`（持仓≥10 交易日）/ `risk_force_exit`（默认 False）。
- **风控**（`risk.review`，逐单）：买入任一 `risk_gate·duplicate_position·duplicate_order·position_cap·exposure_cap·position_pct_cap·cash_insufficient·lot_size·invalid_qty·invalid_price` 即拒单（订单行 REJECTED+原因，不静默丢弃）；卖出 `no_position·over_position·t1_locked·t1_same_day`。
- **撮合**（`paper.execute`，D+1 开盘）：执行日无行情→`ran:false` 不成交；涨/跌停开盘→拒单不伪造；T+1 解锁条件 `locked_date < exec_date`（幂等）；撮合前现金复核；费率来自**订单所属 run 的参数快照**。

## 3. 参数默认值（`GET /api/trading/params`，19 项）

| 键 | 默认 | 含义 |
|---|---|---|
| candidate_top_n | 5 | 六维榜前 N 名进买入候选 |
| trend_ma | 20 | 收盘 ≥ MA(N)，窗口不足不交易 |
| min_amount | 5e7 | 近 5 日日均成交额下限 |
| min_score | 0.0 | 六维综合分下限 |
| max_position_pct | 0.20 | 单股仓位上限（占权益） |
| max_positions | 5 | 最大持仓数 |
| max_total_exposure | 0.80 | 总仓位上限（市值/权益） |
| initial_cash | 1,000,000 | 初始资金（仅首次开户生效） |
| fee_reserve | 0.005 | 买入预算费用+滑点缓冲 |
| allow_pyramid | false | 加仓开关（默认一票一单） |
| stop_loss_pct | 0.08 | 固定止损 |
| trail_pct | 0.10 | 移动止盈回撤 |
| exit_trend_ma | 10 | 趋势破坏 MA 窗口 |
| max_hold_days | 10 | 最大持仓交易日 |
| risk_force_exit | false | 风险态是否强平 |
| slippage | 0.001 | 开盘价滑点（双向） |
| commission_rate | 0.00025 | 佣金率（**研究假设**） |
| min_commission | 5.0 | 佣金最低（**研究假设**） |
| stamp_rate | 0.0005 | 卖出印花税（**研究假设**） |

版本口径：`param_version = sha256(归一化 JSON)[:12]`（键排序、分隔符固定）；`rule_version = "rule-v1"`。
每次 run 把实际参数全文落 `strategy_run.params_json`，同时往 `strategy_params` 落一份版本档（同版本已存在则不动）。
校验：未知键 / 越界 / 非整数（计数类）/ 布尔传数字 / `max_position_pct > max_total_exposure` 一律 `ValueError` → 路由 400，**不静默忽略**。

## 4. DB 七表与迁移 / 回滚

| 表 | 作用 | 幂等键 |
|---|---|---|
| strategy_params | 参数版本档 | PK version |
| strategy_run | 一轮信号生成 | UNIQUE(strategy, signal_date) |
| trade_signal | BUY/SELL/HOLD 逐票留痕 | UNIQUE(run_id, code) |
| sim_account | 单行账户（id=1），现金只由 sim_settle 改 | CHECK(id=1) |
| sim_order | 订单（PENDING/FILLED/REJECTED/EXPIRED） | signal_id UNIQUE |
| sim_fill | 成交（账实对照的现金流） | order_id UNIQUE |
| sim_position | 持仓 + T+1 锁（locked_qty/locked_date） | PK code |

- **迁移**：全部 `CREATE TABLE IF NOT EXISTS`，追加在 `_SCHEMA` 尾部；`db._connect()` 每次启动 `executescript(_SCHEMA)` 自动补建，无版本表、不改既有 22 张表任何列。老库首次启动即完成迁移。
- **回滚**（逆序删，零外键、不影响其他域）：

```sql
DROP TABLE IF EXISTS sim_position;
DROP TABLE IF EXISTS sim_fill;
DROP TABLE IF EXISTS sim_order;
DROP TABLE IF EXISTS sim_account;
DROP TABLE IF EXISTS trade_signal;
DROP TABLE IF EXISTS strategy_run;
DROP TABLE IF EXISTS strategy_params;
```

  回滚后 `/api/trading/*` 全部自然失效（读写均落这七表），其余功能不受影响；策略代码文件可整目录删除，routes 里删掉对应 import 与七条路由即可。

## 5. 完整模拟示例（`_strategy_check` 夹具实测回放，数值为落库真值）

参数覆盖：`{trend_ma:3, exit_trend_ma:3, max_hold_days:3, max_positions:10}`（其余默认）；账户 100 万。

**① 信号日 20270107 收盘后** `POST /trading/signal/run {"date":"20270107", …}`

```
status=OK run_id=1 exec_date=20270108 candidates_n=6 signals_n=11
signals: BUY 1（600001）/ SELL 4（600010 止损+回撤+趋势+超期 / 600011 移动止盈 /
         600012 趋势破坏 / 600013 超期）/ HOLD 6（600002 流动性不足、600003 ST、
         600004 缺行情、600005 趋势向下、600014 未触发、600009 持仓缺行情）
orders: placed 5, risk_rejected 0
portfolio: cash 1,000,000  equity 1,049,840  exposure 4.75%  available 1,043,115.22
600001 target_qty=20,200 股（预算/权益/整手取小；20,200×10.30≈20.8 万 ≤ 单股 20% 权益）
```

**② 同日重跑** → `idempotent:true`，run/信号/订单数一条不涨（参数不同也不新开 run）。

**③ 执行日 20270108 开盘** `POST /trading/execute` → `filled 5, rejected 0`：

| 单 | 数量 | 成交价 | 费用 | 现金流 |
|---|---|---|---|---|
| SELL 600010 | 1,000 | 17.93（开盘 17.95×0.999） | 13.965 | +17,916.035 |
| SELL 600011 | 1,000 | 10.59 | 10.295 | +10,579.705 |
| SELL 600012 | 500 | 13.89 | 8.4725 | +6,936.5275 |
| SELL 600013 | 500 | 14.94 | 8.735 | +7,461.265 |
| BUY 600001 | 20,200 | 10.41（开盘 10.40×1.001） | 52.5705 | −210,334.5705 |

现金 → **832,558.962**；新持仓 600001 qty=20,200、`locked_qty=20,200`（T+1 锁）、`avg_cost=10.41`、`entry=20270108`；四个清仓持仓删行。

**④ 次日 20270111**（前一日 run 的 6 张退出单到点）：600010–600013 持仓已清 → **REJECTED「无持仓可卖」不伪造**；600009/600014 成交（+3,590.2015 / +3,029.482）；600001 解锁（locked_date<exec_date）。现金 → **839,178.6455**；再执行 → `ran:false 没有待执行订单`。

账实恒等式（回归钉死）：`sim_account.cash == 初始 1,000,000 + Σ sim_fill.cash_delta`，`sim_fill` 恰 7 笔。

## 6. 拒绝与边界（已验证行为）

- 快照缺失 / 快照日期≠信号日（数据过期）/ 窗口内无任何行情 / **定不了下一交易日**（库内无更晚行情且日历缓存也没有）→ run `REFUSED`+理由，不下单；数据补齐后**同一 run 就地升级 OK** 再补信号与单。
- 涨停开盘买单、跌停开盘卖单、执行日无行情/无开盘价 → 订单 `REJECTED` + 原因；执行日已过 → `EXPIRED`「错过 D+1 不追补」。
- 现金不足：策略层（预算）与撮合层（现金复核）双保险；风控层 `cash_insufficient` 因仓位帽先收紧，实际不可达，由单测与撮合复核兜底。
- 风险态（输入域非 SUCCESS，`rank_quality` 判 incomplete）→ 新买入全 HOLD `risk_gate`，持仓退出照常；`risk_force_exit` 默认 false。
- 已知待办：执行日恰为「库内有行情但日历缓存判非交易日」的组合按库内为准；研究回测（`engine.build_outcome`）是统计，不是记账，二者不混用。
