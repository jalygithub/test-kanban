# 数据采集链路审计（第一阶段 · 只读）

> **本阶段未修改任何代码、未改动生产库、未调用任何真实数据接口。**
> 库的读取全部通过 `sqlite3` 的 `mode=ro` 只读连接完成。
>
> 审计时间：2026-10-16　│　库：`backend/data/market.db`（7.4 MB，19 张表）　│　代码：当前工作区

---

## 0. 一句话结论

**当前系统是一套「手动触发的当日快照采集」，不是一条数据管线。**

它每天把**当天**的涨停池、板块资金流、候选股资金流抓下来存成 JSON 整条快照，用来支撑当天的页面展示和打分。它**不采集日线、不采集指数、不落股票基础信息、不归档板块历史**。

因此：

| 目标 | 现状 |
|---|---|
| 每日自动增量采集 | ❌ 定时任务**关闭**（`scheduler_enabled=0`），23 次运行**全部是 `kind=manual`** |
| 历史数据补全 | ❌ 无任何回填工具；已过期交易日永久缺失 |
| 失败重试 / 断点续跑 | ⚠️ 只有单次请求级重试（`netfix.with_retry`），无任务级重试、无退避排队 |
| 数据质量检查 | ⚠️ 只有行数，无字段级校验；`row_count=0` 仍会被判 `success` |
| 为回测准备数据基础 | ❌ **无日线表、无指数表、无复权因子表** —— 这正是回测 `BLOCKED` 的根因 |

好消息是：**表结构上没有走错路**。`snapshot(dataset, trade_date)` 复合主键天然幂等，同日重采不会重复；`_collect` 每步独立 try/except，单源失败不会拖垮别的域。这两条正是第三阶段最需要的底子，不用推倒。

---

## 1. 数据源、接口与请求入口

### 1.1 唯一出口

所有外部请求**必须**经 `app/fetchers/client.py:call()`，禁止在别处直接 `import akshare` 发请求。`call()` 依次做：`netfix.with_retry`（重试）→ 分域限速 → 结构化日志。

`client.py` 在 import 时就调 `netfix.install(...)`，装三样东西：
- **IPv4 优先**（`getaddrinfo` 换钩子）
- **分域限速 + 随机抖动**（默认域 `request_interval=0.35s` / 抖动 0.5；敏感域 `1.5s` / 抖动 2.0）
- **总时长闸门**（`request_total_timeout=120s`，装在 socket 读上，防单次请求挂死）

### 1.2 依赖库

| 库 | 用途 |
|---|---|
| `akshare` | 全部行情/资金流/日历/新闻来源，**免费、无认证、无官方配额** |
| `pandas` / `numpy` | 数据整形 |
| `apscheduler` | 定时调度 |
| `requests`（akshare 透传） | 实际 HTTP |

### 1.3 接口清单

| 文件 | 接口 | 用途 | 是否落库 |
|---|---|---|---|
| `ztpool.py` | `stock_zt_pool_em` / `_zbgc_em` / `_dtgc_em` / `_strong_em` | 涨停/炸板/跌停/强势池 | ✅ `pool_*` |
| `sector.py` | `stock_sector_fund_flow_rank` | 板块资金流排名 | ✅ `sector_flow_*` |
| `sector.py` | `stock_board_industry_cons_em` / `_concept_cons_em` | 板块成分股 | ❌ 仅内存 |
| `sector.py` | `stock_sector_fund_flow_hist` / `stock_concept_fund_flow_hist` | **板块资金流历史** | ❌ **取了不存** |
| `quotes.py` | `stock_zh_a_spot_em` | 全市场快照（约 5200 行） | ✅ 经 `main_force` 转成 `stock_net_inflow` |
| `quotes.py` | `stock_individual_fund_flow_rank` | 个股资金流排名（批量） | ✅ 同上 |
| `quotes.py` | `stock_individual_fund_flow` | **单股资金流历史** | ❌ **取了不存** |
| `quotes.py` | `stock_zh_a_hist` / `stock_zh_a_hist_tx` | **日线 OHLC** | ❌ **不落库** |
| `calendar.py` | `tool_trade_date_hist_sina` | 交易日历 | ✅ JSON 文件，缓存 30 天 |
| `listing.py` | 东财/新浪/腾讯 三个列表接口 | 股票列表 | ❌ 仅内存 |
| `track.py` / `research/audit.py` | `stock_zh_index_daily` | **指数日线** | ❌ **不落库** |
| `hk.py` | `stock_hk_spot` / `_hist` / `_daily` | 港股 | 仅内存（页面直读） |
| `fund_nav.py` | `fund_open_fund_info_em` | 基金净值 | ✅ `fund_nav_log` |
| `news.py` | 6 个新闻源 | 财经新闻 | ❌ 仅内存 + 5 分钟缓存 |
| `live.py` | 腾讯资金流 | 实时 | ❌ 仅内存 |

**⚠️ 三个「取了但不存」是本次审计最重要的发现**：`sector_fund_flow_hist`（板块历史）、`stock_individual_fund_flow`（个股资金流历史）、`stock_zh_a_hist`（日线）都已经被调用、都能拿到历史数据，**只是没有落库**。这意味着第四阶段的回填**不需要新增任何上游依赖**，只需要把已经在用的接口接到表上。

### 1.4 认证与费用

- **全部接口免费、无 API Key、无账号体系**（akshare 爬取公开页面）。
- 代价不是钱，是**被限流/被封 IP 的风险**，以及**内网环境下会被公司安全设备拦截并留下记录**（用户既有硬约束）。
- 飞书侧是唯一需要密钥的（`kv` 表存 `feishu_app_id` / `feishu_app_secret`），与行情采集无关。**本报告不记录任何密钥值。**

---

## 2. 数据表与字段

### 2.1 唯一的行情表：`snapshot`

```sql
CREATE TABLE snapshot (
  dataset    TEXT NOT NULL,        -- 数据域
  trade_date TEXT NOT NULL,        -- 交易日 YYYYMMDD，或字面量 'latest'
  payload    TEXT NOT NULL,        -- 整条 JSON（列表或对象）
  row_count  INTEGER NOT NULL,
  source     TEXT,                 -- 如 'em:ztb'
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (dataset, trade_date)
)
```

**8 个 dataset**：`main_force`、`pool_zt`、`pool_zb`、`pool_dt`、`pool_strong`、`sector_flow_industry`、`sector_flow_concept`、`stock_net_inflow`。

**这个设计的两个结构性问题**：

1. **`trade_date` 可以是字面量 `'latest'`** —— `sector_flow_*` 就是这么存的，于是**板块资金流没有任何历史**，永远只能看当天。
2. **整条 JSON 存一个字段** —— 没有行级主键，payload 内部的重复无约束拦截；无法按股票/按日期做 SQL 级增量，只能整条覆盖。实测 243 KB 的 `main_force` 整条读 214 ops/s，按字段取 870 ops/s。

### 2.2 其它有数据语义的表

| 表 | 语义 | 关键字段 |
|---|---|---|
| `fund_nav_log` | 基金净值 | `nav_date`，43 行 / 42 天 |
| `fund_estimate_log` | 基金估值 | 6 行 / 6 天 |
| `job_run` | 任务记录 | `kind/started_at/finished_at/status/detail` |
| `kv` | 开关与配置 | `scheduler_enabled`、`pay_pricing` 等 |
| `watchlist` / `fund_watchlist` | 关注列表 | **当前**名单，无历史 |
| `research_*`（5 张） | 回测研究表 | 本周期新增，当前 0 行 |

**不存在的表**：日线、指数、股票基础信息、复权因子、历史行业成分、历史股票池。

---

## 3. 实际历史覆盖范围

### 3.1 逐域覆盖（实测）

| dataset | 天数 | 区间 | 逐日条目数 | 覆盖判定 |
|---|---|---|---|---|
| `pool_zt` | 9 | 20260924..20261015 | 7~69 | **缺 20260929** |
| `pool_zb` | 9 | 同上 | 7~24 | 同上 |
| `pool_dt` | 9 | 同上 | 11~26 | 同上 |
| `pool_strong` | 9 | 同上 | 100~210 | 同上 |
| `main_force` | 9 | 同上 | all_rows 289~299 / rows 30 | 同上 |
| `stock_net_inflow` | 14 | 20260918..20261015 | **17 → 4782** | **覆盖严重不齐** |
| `sector_flow_industry` | **1** | `latest` | 90 | **无历史** |
| `sector_flow_concept` | **1** | `latest` | 387 | **无历史** |

### 3.2 `stock_net_inflow` 的覆盖是**分段**的

```
20260918    17 条     ← 只有关注股/候选股
20260921    36 条
20260922    57 条
20260923    61 条
20260924  4726 条     ← 这一天起才是接近全市场
20260928  4697 条
20260929    30 条     ← 骤降回候选股级，异常
20260930  4726 条
20261008  1143 条     ← 只有全市场的 1/4
20261009  4737 条
20261012  4463 条
20261013  4782 条
20261014  4771 条
20261015  4691 条
```

**同一天的数据，有的是全市场、有的是 17 只候选股，而表里没有任何字段记录这个区别。**
拿它算任何跨日统计，都会把「那天只采了 17 只」误当成「那天只有 17 只有资金流」。

这正是任务书第四阶段要的「不得把缺失数据填成 0 来掩盖问题」的现实版本——**这里连"缺失"这个事实本身都没被记录**。

### 3.3 历史漏采

以本地日历（`trade_calendar.json`，覆盖 1990-12-19 .. 2026-12-31，8797 天，缓存 18.9 天 / 有效期内）为准：

- 2026-09-01 至 2026-10-15 共 **27 个已过期交易日**
- 有 `pool_*` 数据的：**9 个**
- **漏采 18 个**：`20260901 0902 0903 0904 0907 0908 0909 0910 0911 0914 0915 0916 0917 0918 0921 0922 0923 0929`

其中 `20260929` 落在已有区间**内部**（前后都有数据），是真正的漏采；其余是项目启动前的历史空白。

**这些日期永久不可补**：涨停池、当日资金流都是**只在当天可得**的历史数据，事后无法从公开接口取回当日的涨停池快照。这一点必须在报告里讲清楚——第四阶段的回填对 `pool_*` 和 `stock_net_inflow` **无能为力**，能回填的只有日线、指数、股票基础信息这类有历史接口的数据。

---

## 4. 采集范围：全市场还是候选股

**混合的，而且没有标注。**

| 环节 | 范围 | 依据 |
|---|---|---|
| 全市场快照 | ✅ 全市场（约 5200 行） | `ak.stock_zh_a_spot_em` |
| `stock_net_inflow` | ⚠️ **取决于当天调用方式**，17~4782 不等 | 见 §3.2 |
| 粗筛 | 5200 → ~3000 | `scorer.yaml` `coarse_filter` |
| 预选 | ~3000 → **300** | `preselect_top: 300` |
| 打分候选集 | `all_rows` = **289~299** | 实测 9 天全部在此区间 |
| 最终输出 | `rows` = **30** | `output.top_n: 30` |

**结论：打分只对约 290 只候选股，不是全市场。** 落库的 `all_rows` 保留了这 290 只的完整分值，但**被粗筛和预选淘汰掉的约 4900 只不在库里**。

这带来一个直接后果：**「未入选」和「没采到」无法区分**。做回测时无法判断某只股票是当天没进候选集，还是当天数据缺失。

### 4.1 打分长期降级

9 天的 `main_force` 快照里，`model.degraded` **恒为 `['large_order_ratio']`**，`total_weight` 恒为 **85**（满分 100）。

即：**大单净额占比这个因子（权重 15）在全部 9 天里都没有生效。** 任何关于「六维打分」的历史结论，实际都只有五个维度在支撑。这一点在现有页面上没有醒目标注。

---

## 5. 任务调度

### 5.1 入口

`app/scheduler.py`，单一入口 `run_post_close(date_str, force, kind)`，**必须在后台线程里调**（打分一段约 12-15 分钟）。

触发方式三种（落在 `job_run.kind`）：`scheduled`（定时）/ `manual`（页面按钮）/ `feishu`（手机指令）。

### 5.2 当前状态：**定时任务是关闭的**

```
kv: scheduler_enabled = 0
job_run: 23 条，kind 全部为 manual，无一条 scheduled
```

最近 12 次运行：`success` × 9、`partial` × 2、`failed` × 0。

### 5.3 执行顺序（`_collect`）

```
pool_zt → pool_zb → pool_dt → pool_strong → sector_industry → sector_concept → main_force
```

放最后是有意的：「前两段的成果已经落库，这里挂了也不白跑」。

### 5.4 失败处理

- **每步独立 `try/except`**，异常转成 `failed` 列表的一项，**不外抛** —— 单个数据源失败不会拖垮整条流水线。✅ 这条已经满足任务书要求。
- `_finish` 汇总：无失败=`success`，全失败=`failed`，否则=`partial`。
- **⚠️ 但 `success` 的判据只看「这一步有没有抛异常」，不看结果是否有意义。**
  实证：`job_run #18`（20261014 01:00）结果 `{"pool_zt": 0, ...}` 而 `status=success`。
  **0 条涨停被当成成功**——源返回空列表和源返回正常但今天没有涨停，在当前实现里无法区分。
  这直接违反任务书「未经验证的数据不得标记为完整或成功」。

### 5.5 重试机制

| 层级 | 现状 |
|---|---|
| 单次请求 | ✅ `netfix.with_retry(retries=settings.fetch_retries)`，带退避 |
| 单步（一个数据域） | ❌ 失败即失败，不重试 |
| 任务级（整轮流水线） | ❌ 无。失败要靠人手动再点一次 |
| 断点续跑 | ❌ 无 |
| 漏采自动补采 | ❌ 无。`_warn_if_missed_today()` 只是**发一条告警**，不补 |

**熔断**：`quotes.py` 有 `_FAILURES` 计数，连续失败 ≥2 次就 `_mark_down` 冷却。但它存在**进程内存**里，重启即清零，且不落库、不进 `job_run`。

### 5.6 并发互斥

- `run_post_close` 用模块级 `_lock` + `_state["running"]` 做互斥，重复触发抛 `RuntimeError`。
- **⚠️ 这个互斥只在进程内。** 多进程/多实例同时跑会绕过它。
- `run_light`（轻量抓取）与完整流水线**可以并存**，且刻意不写 `_state`（否则会冲掉正在进行的进度）——但这也意味着**两者可以同时写同一张 `snapshot` 表**。

### 5.7 日志

`run\backend.log`（UTF-8；PowerShell 须 `-Encoding UTF8`，中文控制台回显是乱码，用 `read` 工具看）。

`job_run.detail` 是 JSON 字符串，含 `date / force / scoring / results / failed`。**但没有耗时分步明细、没有每步的行数与错误栈** —— 出问题只能回日志翻。

---

## 6. 数据库唯一键、重复与并发风险

| 风险点 | 现状 | 严重度 |
|---|---|---|
| 同日重复采集 | ✅ `snapshot(dataset, trade_date)` 复合主键 + UPSERT，**天然幂等** | 无 |
| payload 内重复股票 | ❌ **无任何约束**，同一 code 可在 payload 里出现多次 | 中 |
| 缺失字段被填 0 | ⚠️ 无约束拦截；`quotes.py` 已有 `_parse_cn_number` 返回 `None` 而非 0 的约定，但**不保证所有路径遵守** | 中 |
| SQLite 并发写 | ⚠️ **单连接 + `threading.Lock`（不可重入）**，无 WAL | 中 |
| 多进程写同一文件 | ❌ 无防护，会直接 `database is locked` | 中（单机单实例下不触发） |
| 自动清理删历史 | ⚠️ `purge_by_retention` 配 `retention_pool_days=60` / `inflow_days=60` / `main_force_days=365`。**目前只由 `/api/data/purge` 手动触发（默认 `dry_run=True`），不自动跑**，但一次全量清理就会抹掉 60 天前的池子与资金流 | **高（对回测）** |

**锁性能实测**（7.4 MB 真库副本、单连接单锁）：轻读 ~3700 ops/s（0.27 ms）、写 upsert ~292 ops/s（3.4 ms）、整条读大 payload 214 ops/s。

**结论：锁不是瓶颈，payload 体积才是。** 新增管线要做的不是换连接池，而是**别把整条 JSON 拉出来**。

---

## 7. 能否还原历史交易日的选股信号

**部分能，且只能覆盖已有的 9 天。**

| 能力 | 状态 |
|---|---|
| 复现当日排名（聚合口径） | ✅ 能。`research` 模块已实现 `replay_score`，容差 0.06 |
| 复现因子原始值 | ❌ **不能**。快照只存每个维度的 `{label, short, score, weight}`，不存原始值 |
| 复现候选集 | ❌ **不能**。粗筛/预选淘汰掉的约 4900 只不在库里 |
| 复现当日行情输入 | ❌ **不能**。日线不落库 |
| 复现六维完整打分 | ❌ **不能**。`large_order_ratio` 9 天全部降级，实际只有五维生效 |
| 复现当时的基准 | ❌ **不能**。沪深300 可现取但不落库，无法与信号日对齐 |

**一句话：能证明「当时是这么排序的」，不能证明「当时的数据支持这个排序」。**

---

## 8. 历史股票池、退市、停牌、复权、行业分类缺口

| 缺口 | 现状 | 影响 |
|---|---|---|
| **历史股票池** | ❌ 只有当前 `watchlist`（5 条），无「当时真实存在的完整名单」 | 按当前名单回看 = **幸存者偏差**，收益系统性偏高，且无法事后修正 |
| **退市股票** | ❌ 无。列表接口只返回在市股票 | 同上，且是最严重的偏差来源 |
| **停牌** | ❌ 无停牌状态字段 | 停牌期间的样本可能被误判有数据 |
| **复权** | ❌ **无复权因子表**。`stock_zh_a_hist` 默认返回**前复权 `qfq`** | **最严重的未来泄漏源**：`qfq` 用未来的除权事件回溯改写历史价格，回测用它等于每个分都被未来信息污染 |
| **行业分类** | ⚠️ 只有当日快照里的 `industry` 字符串，无历史成分表 | 无法回答「当时它属于哪个板块」；板块轮动分析无历史 |
| **ST 历史** | ❌ 只有当日的 `is_st` 派生自名称，无历史 ST 状态 | `*ST` 涨跌幅是 5% 而非 10%，成交判定会出错 |

**复权口径必须显式决策**（任务书第五阶段要求）：建议落**不复权原始 OHLC + 独立复权因子**，收益计算时再按统一规则处理，并在每条记录上标注复权方式。绝不能直接落 `qfq`。

---

## 9. 测试与网络访问约束

### 9.1 测试现状

| 位置 | 份数 | 说明 |
|---|---|---|
| `scripts\tests\` | 6 | `_hk_verify2` / `_job_progress_check` / `_sub_check` / `_cmd_check` / `_card_frame_check` / `_research_check` |
| `Temp\opencode\` | 19 | 含 `_data_mgmt_check` / `_data_route_check` / `_inflow_switch_check` / `_mf_history_check` / `_live_check` 等 |

**没有一份测试覆盖「采集链路本身」**——现有测试覆盖的是评分、订阅、指令、卡片、回测引擎等**消费侧**。采集侧（`_collect` / `fetch_pool` / 重试 / 幂等）目前只靠人工跑任务验证。

回归基线（全绿）：212 / 95 / 33 / 79 / 16 / 12 / 58 / 32 / 258 / 124 / 26 / **294**。

### 9.2 网络访问约束（既有，必须保持）

1. **内网调股票接口会被公司安全设备拦截并留下记录** —— **不许替用户发请求**；已授权时只做少量定向探测，不铺开抓取。
2. 需要联网时用户要**先连手机热点并明确确认**。
3. `calendar.cached_trade_dates()` 明确「**无视有效期、绝不联网**」，专为矩阵视图设计。
4. `_research_check.py` 在测试里**封锁 socket**，任何外发都会让测试红。
5. 任务书第六阶段要求：「初始页面只读取本地数据库，不触发外部网络请求」——**与现有约束方向一致**，不需要额外让步。

### 9.3 其它不可违反的口径

- `tsconfig.app.json`：`strict` 未开；`noUnusedLocals`/`noUnusedParameters`/`verbatimModuleSyntax` 开（类型导入须 `import type`）；`erasableSyntaxOnly`（**禁 enum**）。
- oxlint 必须**恰好 6 条**既有 `react(set-state-in-effect)`；新组件 effect 里 setState 必须落在 timer / `.then` 回调内。
- antd 6.6.5：`Alert` 用 `type` + `showIcon` + `title` + `description`。
- 全站**红涨绿跌**，走 `signColor` / `signType`。
- **免责声明**须在 README、`docs/01`、`App.tsx`、各页面页脚。
- 白名单只认 `ou_4992e950ac9ad062a307d9978864e7de`；服务器定时任务保持关闭（见下「待决策」）。
- 改后端**只提示「该重启了」**，重启由用户自己做。
- git **零提交**（提交/推送须再问）。

---

## 10. 与既有研究模块的关系

本周期已交付 `app/research/audit.py`（五域**可回测性**审计，`GET /api/research/audit`）。它与任务书第二阶段的**数据覆盖审计**有重叠但不等价：

| | `research/audit.py`（已有） | 数据覆盖审计（本任务第二阶段） |
|---|---|---|
| 回答的问题 | 「能不能做回测」 | 「数据采得怎么样」 |
| 粒度 | 五域，READY/PARTIAL/BLOCKED | 每个数据域逐项 |
| 有无「最近一次尝试时间/错误」 | ❌ | ✅ 要求有 |
| 有无「缺失字段数」 | ❌ | ✅ 要求有 |
| 有无「源是否可用」 | ❌ | ✅ 要求有 |
| 有无区分「没跑任务 / 跑了没数据 / 源失败 / 部分股票 / 完整」 | 部分 | ✅ 要求有 |

**已按此实现（2026-10-16）**：`app/pipeline/coverage.py` 作为**共享事实层**落地，`research/audit.py` 不再自己算覆盖事实，只在 `report["pipeline"]` 里原样带上 `coverage.coverage_report()` 的结果，并复用它唯一的 `mode=ro` 连接逻辑。同一个数字只有一处来源 —— 策略实验室页和数据管理页不会再出现「一个说 3 天、一个说 4 天」。

**仍未做到的一项，以及原因**：「数据源是否可用」**现在报不了**。熔断器状态在进程内存里（`fetchers/client.py`），没有落库 —— 进程重启就没了。今天能给的「最近一次错误」是从 `job_run.detail` 这个整轮流水线的 JSON 快照里**推断**出来的，`job_facts()` 已如实标 `inferred: true`。要让这一项变成权威记录，必须等阶段 3 的 `ingest_job` 逐域状态表落地。

新增了一项计划里没写、但审计时发现必须有的判据：**`unreadable_days`** —— `row_count` 声称有条数、payload 却解析出 0 行。这类日子数据在库里但读不出来，比缺数据更该报警：缺数据一眼看得见，坏 payload 会让下游拿到空列表还以为那天没涨停。

---

## 11. 实施计划（待审核）

> 按任务书第九节：**本节先等审核，通过后再动代码。**

### 阶段 2 —— 数据覆盖审计 ✅ 已完成（2026-10-16）

**交付**

| 文件 | 内容 |
|---|---|
| `backend/app/pipeline/__init__.py` | 管线包（采集 / 覆盖审计 / 回填） |
| `backend/app/pipeline/coverage.py` | 共享事实层：五态判定、覆盖率、缺口、薄日、坏 payload |
| `backend/app/api/routes.py` | 3 个只读端点（见下） |
| `backend/app/research/audit.py` | 改为复用共享层，`report["pipeline"]` 带出覆盖事实 |
| `scripts\tests\_pipeline_check.py` | **74** 断言，临时夹具库 + socket 封锁、全离线 |

**端点**（全部 `mode=ro`、零联网）
- `GET /api/pipeline/coverage` —— 全量五态报告
- `GET /api/pipeline/coverage/{dataset}?limit=` —— 单域逐日明细（条数 / 实际解析行数 / 缺字段行数）
- `GET /api/pipeline/gaps?dataset=&lookback=` —— 近期漏采速查

**五态判定**（任务书要求必须区分）：`NO_JOB`（这个域从没被采过）/ `NO_DATA`（这一步执行了但零数据）/ `SOURCE_DOWN`（有错误记录）/ `PARTIAL`（日期缺口 / 条数骤降 / 字段缺失 / payload 读不出来 / 只有 `'latest'` 占位键）/ `OK`。判不出来一律取最差，不往好了猜。

**真库实测结论**（只读，未写任何数据）

| 数据域 | 首日 | 末日 | 天数 | 覆盖率 | 状态 | 原因 |
|---|---|---|---|---|---|---|
| `pool_zt/zb/dt/strong`、`main_force` | 20260924 | 20261015 | 9 | 0.90 | `PARTIAL` | 缺 `20260929` |
| `stock_net_inflow` | 20260918 | 20261015 | 14 | 1.00 | `PARTIAL` | 日期一个不缺，但 **6 天条数骤降**（峰值 4782，最低 17） |
| `sector_flow_industry/concept` | — | — | 0 | — | `PARTIAL` | 只有 `'latest'` 占位键，**零历史** |

**偏离计划之处（如实记）**
1. 「源是否可用」这一项**做不了**，原因见 §10 —— 熔断器在进程内存，未落库。
2. 计划写的是「用全市场快照行数做条数基准」，实际用的是**各日条数的历史峰值**（`_PARTIAL_RATIO = 0.6`）。前者库里没有存全市场行数，硬要就得再发一次请求拿当日快照 —— 违反零联网。实测峰值基准能干净切开（正常日 ≥0.93，异常日 ≤0.24）。
3. 端点是 3 个不是 1 个：逐日明细与漏采速查是前端展示「哪天缺」的必需项，塞进全量报告会让 payload 在域多时膨胀。

### 阶段 3 —— 每日增量采集 ✅ 已完成（2026-10-16）

**在现有 `scheduler.py` 上扩展，没另建调度系统。** 定时任务开关保持关闭（用户自己手动开），本阶段做的是「手动补跑 + 启动时漏采检测 + 有上限重试」。

**交付**

| 文件 | 内容 |
|---|---|
| `backend/app/store/db.py` | `ingest_job` 表 + 7 个 `ingest_*` CRUD |
| `backend/app/pipeline/ingest.py` | 状态机、有上限重试 + 指数退避、校验闸门、漏采自检 |
| `backend/app/scheduler.py` | `_collect_domain` / `_score_and_record` / `run_retries`；`_finish` 收 `partial` |
| `backend/app/api/routes.py` | `GET /api/pipeline/ingest`、`POST /api/pipeline/collect/retry` |
| `scripts\tests\_ingest_check.py` | **80** 断言，临时夹具库 + socket 封锁、全离线 |

**`ingest_job` 表**：`(domain, trade_date)` 唯一 —— 同一天重跑是覆盖不是新增，与 `snapshot` 复合主键同一套思路。列含 `attempts / max_attempts / next_retry_at / processed / stored / failed_rows / error`。三级计数**缺一不可**：只记 `stored` 分不清「源只给了 30 行」和「源给了 4782 行但只存下 30 行」。

**状态机**：`WAITING`（排队或等重试）/ `RUNNING` / `SUCCESS` / `PARTIAL`（跑成了但数据不能信）/ `FAILED`（异常或重试用尽）。退避 `15 → 30 → 60 → 120 → 240 → 300s` 封顶；`ingest_due` 只回**到点且未用尽**的行，把没到点的捞出来等于绕过退避。

**修掉的两个真 bug**
1. **`pool_zt=0` 仍判 success**（真库 `job_run #18`）：`_finish` 过去只看「有没有步骤抛异常」。现在落库 0 行判 `PARTIAL`，整轮判 `partial`，`failed` 与 `partial` 在 `job_run.detail` 里是两份。
2. **字段缺失不降级**：写完状态机跑冒烟测才现出来的 —— `gate()` 只看行数，`record` 收了 `failed_rows` 却没用它降级，10 行里 5 行缺 code 照样 `SUCCESS`，等于没有闸门。

**一个差点写坏数据的坑**：`main_force` 必须走 `record(store=False)`。`mainforce.run()` 自己落的是包型 payload（`[{rows, all_rows, params, model}]`），再 `db.save` 一次会用裸行列表把整个包覆盖掉，`replay_score` 当场废。已在 `ingest.py`、`scheduler.py`、`AGENTS.md` 三处写明。

**库变更**：新增 `ingest_job` 一张表。`CREATE TABLE IF NOT EXISTS`，**下次后端启动时自动建**，不动任何既有表与数据。

**测试结果**：新增 80 ✓；既有 13 份基线全绿（212/95/33/79/58/**33**/258/124/26/16/12/294/74，`_job_progress_check` 由 32 → 33）；`py_compile` 0。本轮**未改任何前端文件**，`tsc` / `oxlint` / `vite build` 不受影响。

**仍未做，以及原因**
- **自动补跑漏采日期没做**：`startup_check()` 只打日志 + 列清单，**不自动跑**。口径与既有的 `_warn_if_missed_today` 一致 —— 自动补历史是一次会打十几分钟网络的批量请求，必须由人发起（页面「补跑重试」或 `POST /api/jobs/run`）。
- **并发互斥仍是进程内的**：沿用 `scheduler._lock` + `_state["running"]`。单实例部署够用；多实例会各跑各的。这条已写进表注释。
- **超时控制没新增**：复用 `netfix` 既有的总时长闸门 120s，没另加一层。

**下阶段计划**（阶段 4，回填工具）：`app/pipeline/backfill.py`，只回填有历史接口的数据（日线 / 指数 / 板块资金流历史）。**当前内网环境下不执行**，验收只做到「工具就绪 + 用模拟数据端到端验证」。

### 阶段 4 —— 历史数据补全 ✅ 已完成（2026-10-16）

**交付的是工具，不是数据。** 内网环境抓股票接口会被公司安全设备拦截并留记录（§12 决策 1），所以本轮**一次真实回填都没跑**，验收只做到「工具就绪 + 用替身取数器端到端验证」。

| 文件 | 内容 |
|---|---|
| `backend/app/pipeline/backfill.py` | `plan()`（零联网）/ `backfill()`（联网）/ 三个取数器 / `REFUSED` 拒绝清单 |
| `backend/app/store/db.py` | `daily_bar` + `index_daily` 两表；`bars_upsert` / `bars_overwrite` / `bar_span` / `bars_coverage` / `bars_dates` |
| `backend/app/api/routes.py` | `GET /api/pipeline/backfill/domains`、`GET .../plan`、`POST .../backfill` |
| `scripts\tests\_backfill_check.py` | **81** 断言，临时夹具库 + 假 akshare 代理透传真模块 + socket 封锁、全离线 |

**四条不可让步的口径**（前两条是本阶段就定死的，后两条是实现时发现必须显式化的）

1. **日线绝不落前复权。** `fetchers/quotes.daily_history()` 内部写死 `adjust="qfq"`，回填**不走它** —— 直接调 akshare 并显式传 `adjust=""` + 独立 `adj_factor` 列。前复权是「从今天往回算」的价格，日后配息除权会让整段历史跟着变，回测等于在追一个不断漂移的目标（`docs/backtest/§3`）。
   测试用 **AST**（不是字符串匹配）钉死这件事：任何一处传 `adjust="qfq"` 都会红。用字符串找会被源码 docstring 里的说明文字误伤 —— 我在 docstring 里写了「不走 `quotes.daily_history()`」，字符串匹配当场把自己写的注释判成违规。
2. **已有数据不覆盖。** `bars_upsert` 走 `ON CONFLICT DO NOTHING`。回填最怕的不是漏，而是把已核对过的行情用一次重新抓取静默盖掉、而那次恰好源只回了半截 —— 那样连追溯的可能都没了。要重写必须显式调 `bars_overwrite`，把意图留在调用处。
3. **当日数据显式拒绝。** `pool_*` / `stock_net_inflow` / `listing` 进 `REFUSED`，`plan()` 与 `backfill()` 直接抛 `ValueError` 并说明为什么。返回空结果会让调用方以为「补过了，本来就没有」—— 那是把「不知道」包装成「知道」。
4. **拿不到的值不填默认值。** 复权因子本域取不到 → `adj_factor` 写 `NULL`，**不填 1.0**。填了等于悄悄声明「没有复权」，那是口径不是数据。

**断点续跑不另建进度表**：每只票先读 `bar_span`（已有首末日），区间盖住 `[start, end]` 就跳过，所以「暂停后原样再跑一次」就是恢复。判据是**区间被盖住**，不是「有数据就算完」—— 用后者的话，库里差一天也永远报「已补全」。

**库变更**：新增 `daily_bar` + `index_daily` 两张表，`CREATE TABLE IF NOT EXISTS`，下次后端启动时自动建，**当前为空**。表名是刻意的 —— `research/engine._load_bench` 只认名字里明确含 `index` 的表，不猜「某张表里有没有指数代码」。

**测试抓到的四个真 bug**（都修在实现里，没在测试里绕）
1. `fetch_index(start, end)` 只收 2 个参数，而 `backfill()` 对所有域都传 3 个 → 只在指数这一条路径上炸。改为三个取数器统一收 `(target, start, end)`，指数忽略首参 —— 分支一多就容易给某一个传错个数。
2. `bars_upsert` 对所有表套同一份列清单，而 `index_daily` 没有 `turnover` / `adj_factor` 列 → `no such column`。改为按表的真实 schema 取交集（PRAGMA）。**不缓存**：测试频繁换 `db_path`，缓存列名会悄悄写错表。
3. 批量写入用 `conn.execute(sql, params)` → `Incorrect number of bindings`。批量必须 `executemany`（`total_changes` 对它同样逐行累计，差值就是真插入的行数）。
4. `_norm_date` 只查形状不查日历：`2026-08-32` 归一后是 8 位数字、形状完全合法，但不是真日期。放过去的后果是「按这个区间去抓，一无所获」，而日志上看着像「那个区间本来就没有数据」。已补 `strptime` 校验。

**测试结果**：新增 81 ✓；既有 14 份基线全绿（212/95/33/79/58/33/258/124/26/16/12/294/74/80）；`py_compile` 0。本轮**未改任何前端文件**。

**仍未做，以及原因**
- **真实回填没跑** —— 内网环境。切到手机热点等非内网环境后即可执行：先 `GET .../plan` 看要发多少请求，再用 `budget` 分批调 `POST /api/pipeline/backfill`（沿用 `netfix` 分域限速，全市场 5200 只 × 逐只请求约需 30-45 分钟，必须分批 + 可中断）。
- **全市场股票列表不自动取** —— `daily_bar` 必须显式传 `codes`。工具不自己去联网取名单（那是另一次外发，且 `listing` 只给当前名单、不带日期维度，本就在 `REFUSED` 里）。
- **股票基础信息（`listing.py` 三源）没做** —— 它不带日期维度，回填「历史名单」在当前源下无从下手，已在 `REFUSED` 里写明。
- **回填结果没接进覆盖审计** —— `coverage.py` 仍只看 `snapshot`。等日线真有数据了再把 `daily_bar` 纳入五态判定，现在接是空转。

**下阶段计划**（阶段 5，历史股票池与回测约束）：落**不复权 OHLC + 复权因子**两列的口径已在本阶段落地（`daily_bar` 表结构），阶段 5 侧重收益计算的统一规则与记录上的复权方式标注；股票池模式 A/B 沿用研究模块既有口径，不重复实现。

### 阶段 5 —— 历史股票池与回测约束 ✅ 已完成（2026-10-16）

三条任务里，**只有第一条是新代码**；另两条研究模块本来就做对了，本轮确认复用、不重复实现。

| 文件 | 内容 |
|---|---|
| `backend/app/research/engine.py` | `price_adjust()` + 三档常量；`build_outcome` 全部收益字段改按可比价；MFE/MAE 逐根折算；`_warn_adjust()` + `_finish` 的 `adjust` 计数；`_load_bars` 补取 `adj_factor` |
| `backend/app/store/db.py` | `research_outcomes` 加 4 列：`adjust_method` / `adjust_note` / `entry_factor` / `exit_factor`（schema + `_migrate` 补列 + 写入列清单） |
| `docs/backtest/limitations.md` | 新增 **§6 缺复权因子**；修正 §1「没有任何日线表」与 §4「沪深300 不落库」两处已过时的事实（阶段 4 建了表）；原 §6~§10 顺延为 §7~§11 |
| `docs/backtest/usage.md` | 新增「复权口径（每条结果都标着）」小节 |
| `scripts\tests\_adjust_check.py` | **84** 断言，临时夹具库 + socket 封锁、全离线 |

**复权口径**：收益一律按 `exit × exit_factor` / `entry × entry_factor` 算，记录上标三档：

- `RAW` —— 两端 `adj_factor` 都有且不同 → 已折算
- `UNADJUSTED` —— 两端因子相同 → **确认这段没除权**，不复权价直算本就对（**不许标成 RAW**：没折算动作就不该说自己折算过）
- `UNADJUSTED_UNCERTAIN` —— 缺 `adj_factor` → **判不了**，`adjust_note` 写明「若期间发生过除权，该收益不反映真实回报」

**五条实现决策**（每条都有对应的反面后果）

1. **缺因子时数值必须与折算前一模一样。** 系数给 1.0（即不折算），只多出几个字段。这不是「为了少改测试」—— 缺因子时我们**并不知道有没有除权**，数值一变就等于替数据做了决定。`_factor()` 取不到或非正一律回 `None` 而**不是 `1.0`**：回 1.0 等于悄悄声明「没有复权」，那是口径不是数据。
2. **不并进 `data_quality`。** 那条维度回答「行情齐不齐」（缺入场/出场 K 线、缺基准），复权口径回答「这个数可不可信」。混成一个的后果是「行情齐全所以标 READY」、却没人看得出这批收益横跨了除权日 —— 那是最危险的一种「看起来没问题」。改走**独立字段 + 运行 `warnings` + `_finish.adjust` 计数**三条通道，看得到但不污染既有语义。
3. **`entry_price` / `exit_price` 保持不复权的真实成交价。** 价格字段回答「当时多少钱成交的」，收益字段回答「实际赚了多少」，两者口径不同不能混。折算系数另存两列，谁想复核自己乘回去。
4. **MFE/MAE 逐根用自己的因子**，窗口里缺因子的那根才退回入场日系数。整窗一刀切的话，除权落在持仓中途时换算后的高低点会算错 —— 夹具 C 组就盯着这件事：不复权口径把除权跳空算成 -8% 回撤，折算后是 -3.2%。
5. **`adjust_method` 是 `NULL` 而非某档值**，出现在 `status != DONE` 的行上 —— 没算出收益就谈不上口径，给它填 `UNADJUSTED` 会让人以为「算过了，只是没折算」。

**为什么这不算未来数据泄漏**（任务书「禁止用未来数据回填过去信号」的落点）：折算只用两端因子的**比值**，而 `F(exit)/F(entry)` 恰好等于**持仓区间内**发生的除权事件，全部 ≤ `exit`。`qfq` 之所以是泄漏，是因为它用 `exit` **之后**的除权去改写两端价格 —— 两回事，已在 `limitations.md §6` 单独写明。

**另两条任务：确认复用，零新代码**
- 股票池模式 A（当前池回看 + 幸存者偏差标注）/ 模式 B（historical，**建实验与运行两处显式拒绝**）—— `research_experiment_create` 已实现，本轮只验证断言仍在（`_research_check` 294 绿）。
- 因子按当时实际可用数据计算：靠三条既有机制兜底 —— 信号**不重算**（用入库快照）、`_ref_dates` 只用库里实际存在的日期、日线**不落 `qfq`**（阶段 4 的 AST 断言钉死）。

**测试结果**：新增 84 ✓；既有 15 份基线全绿（212/95/33/79/58/33/258/124/26/16/12/294/74/80/81）；`py_compile` 0；`tsc -b` 0；oxlint **恰好 6** 条既有 `react(set-state-in-effect)`（文件集合不变）；`vite build` 0。本轮**未改任何前端文件**。

**仍未做，以及原因**
- **`adj_factor` 恒为 NULL** → 新跑的实验几乎全是 `UNADJUSTED_UNCERTAIN`。工具链已通，卡在非内网环境（§12 决策 1）。
- **`adjust_method` 还没上页面** —— `Lab.tsx` 目前不展示复权口径。属展示层，留给阶段 6 一起做，避免这轮碰前端。→ **阶段 6 已做**：逐条结果表加了「复权口径」列，三档不同色不同字，`UNADJUSTED` 绝不与 `RAW` 同口径；`NULL`（没算出收益）显示 `-`。
- **存量 outcome 行的四列为 NULL** —— `_migrate` 只补列不回填，旧结果按「当年怎么算的」原样留着，不追溯改写。

**下阶段计划**（阶段 6，数据管理页面）：在 `DataManage.tsx` 上加覆盖范围 / 最新有效交易日 / 采集状态与错误 / 回填进度 / 缺失区间 + 指定日期与数据域的补跑入口 + 全量质量报告；首屏只读本地库零联网。`adjust_method` 的展示若自然落在研究页，一并带上。

### 阶段 6 —— 数据管理页面 ✅ 已完成（2026-10-16）

在现有 `DataManage.tsx` 上加 4 张卡，不新建页面；后端**只加只读端点**，联网入口仍是原来那两个（人点按钮才调）。

| 文件 | 内容 |
|---|---|
| `backend\app\api\routes.py` | 新增 `GET /api/pipeline/bars`（回填进度 + 因子覆盖，零联网）；`GET /pipeline/backfill/plan` 与 `POST /pipeline/backfill` 加 `scope`（名单取自本地库）；`POST /pipeline/collect/retry` 加 `domain` |
| `backend\app\pipeline\backfill.py` | 新增 `load_scope()`（all/mf/pool/wl）+ `_walk_codes` / `_payloads` / `SCOPES` —— 名单判据从**脚本里搬进来**，页面进度与跑批共用同一份，避免两个数对不上 |
| `backend\app\store\db.py` | `bars_code_counts()`（一次 GROUP BY 走 PK 索引）；`bars_factor_coverage()` 按表 schema 判 `supported` |
| `backend\app\scheduler.py` | `run_retries(domain=...)` —— 指定数据域的补跑 |
| `scripts\backfill_run.py` | 删掉自持的 4 个名单函数，改 `B.load_scope` |
| `frontend\src\lib\api.ts` | 新增 `pipelineCoverage`/`pipelineGaps`/`pipelineIngest`/`pipelineBars`/`backfillPlan`/`collectRetry`/`backfillRun` + 类型；`ResearchOutcome` 补 4 个复权字段 |
| `frontend\src\pages\DataManage.tsx` | 4 张卡：**采集覆盖与缺口** / **历史行情与复权因子**（进度 + 缺日子明细）/ **采集状态与补跑**（含指定数据域下拉）/ **历史回填**（先看计划再动手，20 次请求预算） |
| `frontend\src\pages\Lab.tsx` | 逐条结果表加「复权口径」列（阶段 5 交接项） |
| `scripts\tests\_pipeline_check.py` | 74 → **103** |
| `scripts\tests\_ingest_check.py` | 80 → **85** |
| `AGENTS.md` | 两个断言数同步 |

**四条判据写进返回，不靠前端记**

1. **回填进度三态**：`covered`（该票窗口内每天都在）/ `partial`（缺日子）/ `absent`（一行没有），**只有 covered 算完成**。判据只看行数 —— 表的交易日是全表共有集合，某票行数 == 窗口交易日数就等价于「每天齐」。缺哪几天给到具体日期。
2. **判不了就是 `None`，不是 0**：`daily_bar` 空表、名单为空，`rate` 一律 `None` 并附说明（含「不等于已完成」）。`0%` 与 `100%` 同样是编出来的数。
3. **`index_daily` 按 schema 就没有 `adj_factor` 列** → `supported: false` + `rate: None` + 一句说明。硬算 `COUNT(adj_factor)` 会 `no such column`，算成 100% 更是骗人。
4. **挑域不绕过退避**：`run_retries(domain=...)` 先 `ingest_due()` 再筛域，`checked` 报**筛后**条数；点名一个没到点的域回 `0/0`，且**一个取数器都不许调**（下拉框选错不该变成一批外发）。

**两处踩了又填平的坑**（都在实现里修，没在测试里绕）

- **FastAPI 端点被当普通 Python 函数按位置直调时，`Query()` 默认值是哨兵对象。** 拿它去迭代 → `'Query' object is not iterable`；拿它当域名 → 判真。`pipeline_backfill_plan` 两个参数都做了 `isinstance` 归一，`scope` 最终排在 `items` **之后**（不让它挤掉第 4 个位置参数的原语义）。
- **`Query` 哨兵是真值**：`pipeline_collect_retry(domain=...)` 若写成 `Query(default="")`，无参直调时 `domain or None` 判它为真 →「全部」被当成「指定了一个不存在的域」，**页面点补跑毫无反应**。默认值刻意用裸 `""`，并用 `P27` 钉死。

**测试结果**：`_pipeline_check` 74 → **103**、`_ingest_check` 80 → **85**；全套 **17 份回归全绿**；`py_compile` 0；`tsc -b` 0；oxlint **恰好 6** 条既有 `react(set-state-in-effect)`（文件集合不变，`DataManage.tsx` / `Lab.tsx` 各新增 0 条）；`vite build` 0。

**库变更**：无新表、无新列，四个新端点全部只读本地库、零联网。

**首屏零联网**：四张卡走 `Promise.allSettled` 各自成败互不牵连，后端未重启时新端点 404 也只影响那张卡。**新端点要重启后端才出现**（前端改动热更新即生效）。

**仍未做，以及原因**
- **页面上点「开始回填」在内网跑不通** —— 入口与预算（每次 20 次请求、跑完回读计划）都就位，但外发仍受 §12 决策 1 约束。本轮真实回填走 `scripts\backfill_run.py`（带熔断与断点续跑）。
- **`coverage.py` 仍只看 `snapshot`** —— `daily_bar` 未纳入五态。两套判据回答的是不同的问题（快照「哪几天缺了」、历史行情「哪几只票没齐」），硬并进一张表会让「缺失」出现两种含义，所以历史行情进度由 `/pipeline/bars` 单独回答。
- **`coverage.job_facts` 仍 `inferred: true`** —— 从 `job_run.detail` 推断；权威的逐域表是 `ingest_job`，页面已把它单独列出，前者未改。
- **`sector_hist` 取数器未实现**；`adjust_method` 只逐条展示，没在研究页顶上做汇总口径。

### 阶段 7 —— 测试与验收

逐条对着 `scripts\tests\` 找**覆盖证据**，11 条分两堆 —— 已有断言的不重复造轮子：

| # | 条目 | 证据在哪 |
|---|---|---|
| 1 | 同日重复采集不重复 | `_ingest_check` A3（attempts 累加、行不新增）+ `_backfill_check` `ON CONFLICT DO NOTHING` |
| 2 | 中断后可重试恢复 | `_ingest_check` L 系列测「到点的会被跑掉」；**闭环缺** → 新增 `_accept_check` E 组 |
| 3 | 部分缺失 → `PARTIAL` | `_ingest_check` B/C/E 系列 |
| 4 | 非交易日不算漏采 | `coverage.py:294,453` 有实现；**缺断言** → `_accept_check` C 组 |
| 5 | 回填可暂停/恢复/跳过 | `_backfill_check` E6/E7/E10（budget 截断 = 可控暂停，续跑跳过已覆盖） |
| 6 | 单源失败不污染其它域 | `_ingest_check` J/K 测到 `_run_step`/三元组层；**表层缺** → `_accept_check` D 组 |
| 7 | 缺失不被填成 0 | `_ingest_check` E4/F + `_pipeline_check` 缺失写 `None` |
| 8 | SQLite 并发写不损坏 | **此前全空白**（`scripts\tests\` 里唯一命中 `threading` 的是飞书卡片）→ `_accept_check` A 组 |
| 9 | 启动发现近期漏采 | `_ingest_check` M1–M3 |
| 10 | 原有基线全绿 | 全套 17 份跑过一轮，见 §11 收尾记录 |
| 11 | 不新增未授权网络路径 | **此前无断言** → `_accept_check` B 组 |

**新增 `scripts\tests\_accept_check.py`（40 条，全离线 + 临时夹具库，不碰真实 `market.db`）**：

- **A 并发写 20 条（第 8 条）**：`busy_timeout=5000` / 非 WAL 从 `db._connect()` 现场读回（同参数，否则「并发下没事」不作数）；同主键与不同行两线程并发、持写锁时**真等到**（0.7~5s）、**不等就明确抛 `database is locked`** 而不挂死、写一半被 `os._exit(1)` 掐掉后「已提交在 / 未提交不在 / `integrity_check=ok` / hot journal 已清理 / 还能继续写」。
- **B 联网白名单 5 条（第 11 条）**：AST 扫 `routes.py`，数据源联网路由全集**必须等于**白名单 6 条（`/jobs/run`、`/limit`、`/stock/{code}/daily`、`/refresh`、`/pipeline/collect/retry`、`/pipeline/backfill`）—— 加了不吭声就红；阶段 6 三个零联网端点不许混进去；`GET /pipeline/*` 一律零联网；路由文件不许直接 `import requests/akshare/httpx`（防绕开调用扫描）。判据**精确到被调属性名**：`backfill.backfill` 联网、`backfill.plan`/`load_scope` 读本地库，同一个模块名语义相反，按模块名一刀切会全错。
- **C 非交易日 4 条、D 域隔离 4 条、E 恢复闭环 5 条、Z 收尾 2 条**（全程零外发 + 没把 `db_path` 指回真实库）。

写这两组的过程本身各揪出一处口径，都是「有实现、没断言」的代价：
- `record_failure` **不** +1 attempts（`ingest.py:184`：`ingest_start` 已经加过，两边各加会翻倍、退避次次算错）—— 我先按直觉写成 +1，跑出来 `1 -> 1` 才回去读实现。
- `missing_dates` 只算 `[first_date, last_date]` **窗口内**的缺口（`coverage.py:294`）—— 砍掉尾部两天会把窗口一起缩掉，测了个空；缺口得挖在**中间**（顺带让「周末会不会被算成漏采」变成能被抓到的断言）。

---

## 12. 业务方决策记录（2026-10-16 已确认）

> **2026-10-17 实施状态回写**：以下决策均已落地，逐阶段实施记录见 `plan.md` §2-§8。
> ① 日线回填**工具已就绪**（东财主源 + 腾讯备源、`ADJUST=""` 不复权 OHLC + 独立 `adj_factor`、`--gaps` 只补缺口、SIGINT 优雅暂停），**内网仍未执行真实回填** —— §12.1 的结论不变；
> ② 定时任务**仍关闭**（`scheduler_enabled=0`），但调度接线、17:30/次日 09:20 补跑检查（**只查不跑**）、`GET /api/pipeline/health` 均已就位，开关由用户自己开；
> ③ 回填缺省起点 `20260801`（`backfill.DEFAULT_START`）；
> ④ 覆盖审计与 `research/audit.py` 已共享（`app/pipeline/coverage.py`，`_pipeline_check` 与 `_research_check` 双向对账）。
> 另：阶段 6 前端（数据管理页健康总览 / 缺失区间合并）与阶段 7 验收（含「不完整数据不生成误导性排名」的整榜标记，`ingest.rank_quality`）已于 2026-10-17 交付，21 份回归全绿。

1. **日线回填：当前不做，但不是永久排除。**
   现在处于**内网环境**，抓股票接口会被公司安全设备拦截并留下记录 —— 所以
   **本轮不抓日线**。这不是设计上的排除：等切到手机热点等非内网环境时，
   日线回填即可执行，届时按本报告 §8 的口径落库（**不复权 OHLC + 独立复权因子**，
   绝不直接落前复权 `qfq`）。
   实施含义：回填工具**要支持日线这个数据域**，只是当前不授权运行它；
   不是把它从工具里删掉。

2. **定时任务不开。** `scheduler_enabled` 保持 `0`。开关已存在，需要时用户
   **自己手动打开**。第三阶段不追求自动定时，而是把「手动补跑 + 启动时漏采
   检测 + 有上限重试」做扎实。

3. **回填起点：`20260801`。** 与现有快照（最早 `20260918`）对齐后还有约一个
   半月的历史空白可补 —— 但能补的只有有历史接口的数据，见 §12.1 的范围收敛。

4. **覆盖审计与既有 `research/audit.py` 做成共享。** 不再写两套。

### 12.1 回填范围（按上述决策收敛）

| 数据域 | 能否从 20260801 回填 | 说明 |
|---|---|---|
| 指数日线（沪深300） | ✅ 能，**当前环境不执行** | `stock_zh_index_daily` 有完整历史。解开「无基准 → 无超额收益」的关键 |
| 全市场日线 | ✅ 能，**当前环境不执行** | `stock_zh_a_hist`。解开回测 `BLOCKED` 的关键 |
| 板块资金流历史 | ✅ 能，**当前环境不执行** | `stock_sector_fund_flow_hist` |
| 股票基础信息 | ⚠️ 无日期维度 | `listing.py` 三源只给**当前**名单，回填历史无意义 |
| `pool_zt/zb/dt/strong` | ❌ **永久不可补** | 当日数据，事后无法从公开接口取回 |
| `stock_net_inflow` | ❌ **永久不可补** | 同上 |

**结论：当前内网环境下，回填工具可运行的部分为零**（唯一不需网络的是读本地库）。
所以本轮实施顺序调整为：**阶段 2（覆盖审计）→ 阶段 3（增量采集框架，含漏采检测与重试）
→ 阶段 4（回填工具，做好但不执行）→ 阶段 6（前端）→ 阶段 7（回归）**。
阶段 4 的验收只能做到「工具就绪 + 用模拟数据端到端验证」，真实回填等非内网环境。

### 12.2 另两条（按报告建议执行，无异议）

- **`stock_net_inflow` 早期那几天（17~61 条）不修历史数据**，在覆盖审计里标 `PARTIAL`
  并写明原因。改写历史会让「当时到底采了什么」变得不可追溯。
- **保留期暂不放宽**。`retention_pool_days=60` 目前只在手动全量清理时触发（默认
  `dry_run=True`），不自动跑；等真要做超过 60 天的回测时再显式加白名单。

---

## 13. 本阶段未做的事

- ❌ 未修改任何代码。
- ❌ 未改动生产库（读取全部 `mode=ro`）。
- ❌ 未调用任何真实数据接口（akshare 接口清单来自代码静态分析，覆盖范围来自库内既有数据与本地日历缓存）。
- ❌ 未提交任何 git 变更。
