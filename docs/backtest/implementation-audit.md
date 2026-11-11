# 阶段 0 审计报告 — 历史回测与策略研究模块

审计日期：2026-10-15 ｜ 审计方式：读代码 + 只读查询 `backend/data/market.db`（`mode=ro`）
结论标注：**已确认**（有代码/数据佐证）· **待确认**（需业务决策或额外数据）· **当前不支持**（缺关键数据，明确不实现）

---

## 0. 三条必须先说的结论

1. **任务书假设「本地日线历史约 3 个月」经审计不成立。** 库里**没有任何日线表**（15 张表逐一核对，无 OHLC 字段）。日线是打分时现取现算、算完即丢。→ **依赖日线的回测（收益计算、250 日位置、振幅比）当前 `BLOCKED`。**
2. **`main_force` 快照本身可复现。** 每天的 payload 存了完整 `params`（含权重）、`all_rows`（299 只 × 5 维原始值与单项分）、`model`（降级与实际权重）、`stats`。→ **同一份快照内的排名可以确定性重放**，这是首期唯一能诚实交付的「回测」形态。
3. **日线是前复权（`qfq`）。** 价格会随后续除权除息**回溯性改写**。用今天下载的前复权序列去回测历史信号，历史价格已被未来事件污染 —— 这是本项目回测**最严重的未来数据泄漏源**，必须在架构上规避而不是靠注释提醒。

---

## 1. 六维评分的真实实现位置与调用链

**已确认**

| 项 | 位置 |
|---|---|
| 模块 | `backend/app/metrics/mainforce.py`（约 1,340 行） |
| 维度定义 | `DIMENSIONS`（:51）—— 六元组 `(key, 展示名, 展示键, higher_is_better)` |
| 配置 | `SCORER_PATH = backend/config/scorer.yaml`（:47），`load_config()`（:115）缺失项回落 `DEFAULT_CONFIG`（:60） |
| 入口 | `run(date=None, refresh=False)`（:1228） |
| 流水线 | ① `quotes.market_snapshot()` 全市场快照 → ② `listing_api.get_listing_info()` → ③ `ztpool.get_zt_pool/get_dt_pool` → ④ `_coarse_filter`（:183）粗筛 → ⑤ `_collect_inflow` 资金流 → ⑥ `_preselect` 预选 → ⑦ `_fetch_histories`（:1149）逐只日线 → ⑧ `_negative_filter` → ⑨ `_score` → ⑩ `db.save_one("main_force", date, result)` |

**调用链**：`scheduler._collect()` → `mainforce.run()`（完整盘后流水线）；`routes.py` 手动触发与补跑也直接调 `run()`。回归依据 `scripts/tests/_job_progress_check.py`。

> 禁令确认：本模块将**复用** `run()` 产出的快照，**不复制、不改写** `_score` / `_preselect` / `_coarse_filter`，`scorer.yaml` 原权重不动。

## 2. 每个因子实际依赖的数据

**已确认**（依赖项来自 `scorer.yaml` + `_score` 实现）

| 因子 | 权重 | 依赖 | 落库？ |
|---|---:|---|---|
| `inflow_consistency` | 30 | 当日资金流抓取 + `snapshot['stock_net_inflow']` 逐日累积 | **是**（14 天） |
| `volume_up_price_flat` | 20 | 日线 `volume` / `close` | **否** |
| `large_order_ratio` | 15 | 东财大单分解（仅 `push2` 提供） | **否**，且长期覆盖率 0% |
| `price_position_low` | 15 | 日线 250 交易日 `high`/`low` | **否** |
| `turnover_moderate` | 10 | 日线 `turnover` 列 | **否** |
| `chip_convergence` | 10 | 日线近 10 日 / 前 20 日振幅 | **否** |

**六个因子里只有 1 个（30 权重）有历史数据。** 这是回测 `BLOCKED` 的根因。

## 3. 日线覆盖、复权方式、日期口径

| 项 | 结论 | 状态 |
|---|---|---|
| 覆盖范围 | **日线表结构已建、当前为空**（`daily_bar` / `index_daily`，2026-10-16 数据管线阶段 4）。`quotes.daily_history()`（`fetchers/quotes.py:386`）每次现取现算，算完即丢，**回填不走它**。**2026-10-17 阶段 5**：回填工具已就绪（东财 + 腾讯备源、`--gaps` 断点续跑），**内网仍未执行真实回填** | **当前无数据** |
| 复权方式 | `adjust="qfq"` **前复权**（A 股东财→腾讯双源；港股 `hk.py:108` 同为 qfq）——**仅供现取现算，不落库** | **已确认** |
| 落库口径 | `ADJUST=""` 不复权 OHLC + 独立 `adj_factor`，AST 断言钉死 | **已确认** |
| 返回列 | `date, open, close, high, low, volume, amount, turnover(百分数), pct_chg(百分数)` | **已确认** |
| 日期字段 | `date`；调用方传 `YYYYMMDD` | **已确认** |
| 250 日前置历史 | `scorer.yaml: history_days: 400`（日历天）供当日打分用，但**不落库** | **当前不支持** |

> ⚠️ **前复权对回测的影响**：前复权价格以「最新交易日」为基准向前折算。若在 T 日下载序列、用于回测 T-100 日的信号，则 T-100 的价格已包含 (T-100, T] 之间发生的除权事件 → **未来数据泄漏**。要诚实回测必须落库**不复权原始 OHLC + 复权因子**，或落库时固定复权基准日。
>
> **2026-10-16 更新（数据管线阶段 4/5）**：这条已按「不复权 OHLC + 独立 `adj_factor`」落成工具与表结构，收益侧也补了折算与口径标注（`price_adjust` 三档）。**表仍是空的** —— 内网环境不授权真实回填，`adj_factor` 全为 NULL，所以新跑的回测仍会把结果标成 `UNADJUSTED_UNCERTAIN`。详见 `data_pipeline/audit.md` §11 阶段 4/5。

## 4. 资金流与大单历史覆盖

**已确认**

| 数据集 | 天数 | 区间 | 结构 |
|---|---:|---|---|
| `stock_net_inflow` | 14 | 20260918 ~ 20261015 | 每天 1 条 payload，内含数千只个股当日净额 |

- **大单净额（`large_order_ratio`）**：只有东财 `push2` 提供，本网络不可用 → 实测 `model.degraded = ['large_order_ratio']`、`total_weight = 85`（而非 100）。**9 天快照全部如此。**
- **能否区分历史观测值 vs 事后重查值**：`snapshot.fetched_at` 记录落库时刻，可区分；但重查历史资金流会得到**今天视角的值**，与当日观测值口径不同 —— **当前不支持**这种区分，因为没有逐日归档的原始观测。
- 冷启动：`stock_net_inflow` 最早几天序列不足 5 日窗口 → `inflow_consistency` 的连续性子证据缺失。README 已记录「按剩余子权重归一化」。

## 5. 股票池 / 上市 / 退市 / 停牌

| 项 | 结论 | 状态 |
|---|---|---|
| 每日股票池快照 | **无此表** | **当前不支持** |
| 上市日期 | `all_rows[i].list_date`（来自 `listing.get_listing_info()`），当日快照 | **已确认** |
| 上市交易日数 | `listing.listed_trading_days()`（:113）用交易日历二分估算 | **已确认** |
| 退市状态 | 仅靠名称含「退」判断（`_coarse_filter:224`），**无退市日期表** | **当前不支持** |
| 停牌 | 靠当日快照 `price<=0 or amount<=0` 推断（:230），**无历史停牌表** | **当前不支持** |
| ST 状态 | `row.is_st or "ST" in name or "退" in name` —— **当前名称**，无历史 ST 变更记录 | **当前不支持** |
| 行业分类 | `all_rows[i].industry`，当日快照 | **已确认** |

→ **模式 A（当前股票池回看）可行但有幸存者偏差**；**模式 B（历史股票池）当前不支持**，不得伪造。

## 6. 沪深300 与行业指数

| 项 | 结论 | 状态 |
|---|---|---|
| 沪深300 | `metrics/track.py:125` 走 `ak.stock_zh_index_daily("sh000300")`（新浪源），**进程内缓存 `_INDEX`，不落库** | **PARTIAL**（可取但不留存，无法保证回测时与信号日一致） |
| 恒生指数 | `HSI` 分键，同上 | **PARTIAL** |
| 行业指数 | `sector_flow_industry` 只有 `latest` 一天，是**资金流**不是指数行情 | **当前不支持** |

## 7. 现有模型能否按历史日期复现

| 形态 | 结论 | 状态 |
|---|---|---|
| **重放快照内排名**（给定 `all_rows` 的 5 维原始值与权重，重算百分位归一化与排名） | **可行且确定** —— 9 天 `params.weights` 完全一致，无规则漂移 | **READY** |
| **从原始数据重算某历史日的分数** | **不可行** —— 5/6 因子依赖的日线不落库 | **BLOCKED** |
| **补算新历史日的分数** | **不可行** —— 同上，且需联网取数 | **BLOCKED** |

**可复现性证据**：`params` 存了 `weights/top_n/preselect_top/min_listed_days/float_mv_min/float_mv_max/max_boards/max_gain_20d/turnover_range/inflow_window_days/price_position_days/chip_threshold` 全套 12 项，`model` 存了 `coverage_mean/degraded/total_weight/used_weights`。

## 8. 未来数据泄漏 / 幸存者偏差 / 重复样本

| 问题 | 说明 | 严重度 |
|---|---|---|
| **前复权回溯污染** | qfq 序列随后续除权改写历史价格（§3） | **高** |
| **当前股票池回看** | `market_snapshot()` 是**今日**全市场快照，已退市股票根本不会出现在任何一天的 `all_rows` 里 → 典型幸存者偏差 | **高** |
| **ST / 停牌用当前值判历史** | `_coarse_filter` 拿当日名称与当日快照判历史日期 | **中** |
| **资金流冷启动** | 最早几天 5 日窗口不足，`inflow_consistency` 被降权归一 → 不同日期的模型实际权重不同（`total_weight` 漂移） | **中** |
| **大单维度长期缺失** | `total_weight=85` 而非 100，跨日比较时口径已不同 | **中** |
| **重复样本** | 快照内 `coarse_dropped.duplicate` 已去重（9 天各 1 只），`all_rows` 按 code 唯一 | **低，已处理** |
| **重叠持有期** | T+20 时相邻信号日的样本高度相关，不能当独立样本做显著性检验 | **中**（统计口径，非数据缺陷） |

## 9. 推荐新增

| 类型 | 名称 | 说明 |
|---|---|---|
| 表 | `research_experiments` / `research_runs` / `research_signals` / `research_outcomes` / `research_metrics` | 阶段 2 五张 |
| 表 | `universe_snapshot` | 每日股票池快照，供未来积累真实历史 |
| 服务 | `app/research/audit.py`（`BacktestDataAuditService`） | 阶段 1 |
| 服务 | `app/research/engine.py`（`BacktestEngine`） | 阶段 3 |
| API | `/api/research/*` | 阶段 7，挂在现有 `routes.py` |
| 页面 | `frontend/src/pages/lab/` 策略实验室 | 阶段 6 |
| 测试 | `scripts/tests/_research_check.py` | 阶段 8，沿用 `ok()` 计数器 + 全离线 |

**不新增日线落库表** —— 首期不落库、不抓取（内网禁止擅自外发），缺口如实上报。

## 10. 状态汇总

| 能力 | 状态 | 原因 |
|---|---|---|
| 信号重放（快照内排名复现） | **READY** | 快照含全套参数与逐因子值 |
| 因子覆盖率与分布分析 | **READY** | `all_rows` 含 5 维原始值 |
| 配置快照与 hash 可复现 | **READY** | `params` 全套入档 |
| 基准收益（沪深300） | **PARTIAL** | 可现取但不留存，无法保证与信号日对齐 |
| **完整六维回测（含收益）** | **BLOCKED** | 5/6 因子依赖的日线不落库；且 qfq 有未来泄漏 |
| **历史股票池（模式 B）** | **BLOCKED** | 无退市/停牌/ST 历史，无每日快照 |
| 行业基准 | **BLOCKED** | 只有资金流，无指数行情 |

### 10.1 已落地（2026-10-16）

| 阶段 | 交付物 | 状态 |
|---|---|---|
| 1 | `app/research/audit.py` + `GET /api/research/audit` | 完成 |
| 2 | 5 张 `research_*` 表 + `sell_status` 迁移（迁移前已备份真库） | 完成 |
| 3 | `app/research/engine.py`：`replay_score` / `rank_signals` / `build_outcome` / `BacktestEngine`（四段进度 + 取消落库） | 完成 |
| 4 | 股票池模式 B 在**建实验与运行两处**显式拒绝；幸存者偏差写进每条指标与每次运行警告 | 完成 |
| 5 | 因子消融：`drop_weights` / `reweight_rank`，实验权重与原始入库分双双入档 | 完成 |
| 7 | `/api/research/*` **12 个端点**（含 `/capabilities`：可选项、默认值、禁用项理由一次给全） | 完成 |
| 8 | `docs/backtest/usage.md` + `limitations.md` | 完成 |
| 6 | 前端「策略实验室」四 Tab | 未开始 |

口径要点（都由 `_research_check.py` **294** 条断言钉住）：
信号取自快照不重算；缺数据写 `NULL` 不写 `0`；`PENDING` / `NO_DATA` 不进收益统计；
理论收益与可执行收益分列；涨停买不进记 `UNFILLABLE`、跌停卖不出记 `LIMIT_DOWN`，
两者都不给可执行收益；`max_drawdown` 留空（未构建逐日组合净值曲线，不拿个股收益排开算）；
因子消融只回答「换权重后排名/收益怎么变」，**不宣称因子的边际贡献**（逐维分无法重算）。

**交易成本默认不配**（2026-10-16 用户拍板「先空着」）：没有业务方确认的费率就不凭空
扣一笔，`exec_return` 一律 NULL、理论收益照常给。要扣费必须显式传 `cost`，配了之后运行
`warnings` 里必然出现「研究假设」。相应地 `unfilled_rate` 改按**成交判定**记账，
不拿「有没有可执行收益」算——否则「没配成本」会被记成「涨停买不进」（`O` 段 8 条钉住）。

---

## 必须由业务方决策的口径（决策记录）

1. ~~首期交付形态~~ → **已定**：接受「信号重放 + 因子研究 + 数据审计」这一不依赖日线的能力集。
2. ~~观察周期基准~~ → **已实现**：`T+n` 按**交易日**计，买入参考 `D+1` 开盘价；交易日序列从日线表 `DISTINCT trade_date` 推导，不依赖联网日历。
3. ~~交易成本默认值~~ → **已定（2026-10-16 用户答「先空着」）**：默认**不配**成本，`exec_return` 一律留空。想看扣费口径须显式传 `cost`，且那组值带 `assumption: true`（研究假设，非实际券商费率）。未定的是**正式默认值本身**，等业务方给定后再补。
4. **是否授权抓取并落库日线**：解开 `BLOCKED` 的**唯一路径**，但内网调股票接口会被拦截并记录 —— **必须由用户明确授权**，且需先决策复权口径（建议落不复权原始 OHLC + 复权因子）。
   **2026-10-16 状态**：用户答「抓取数据要连手机热点等我确认」——**未授权，维持 `BLOCKED`**，模块不因缺日线停摆（其余能力照常可用）。
