"""SQLite 存储。

M1 采用「按 (dataset, trade_date) 存 JSON 快照」的方式，字段结构由取数层
决定，避免在数据源还在变动时过早固化表结构。幂等 upsert，重复抓取不产生脏数据。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
    dataset     TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (dataset, trade_date)
);
CREATE TABLE IF NOT EXISTS job_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL DEFAULT 'manual',
    -- 这一轮跑的是哪一天。detail 那个 JSON 里也有一份，但 JSON 里的是「调用方
    -- 说要跑哪天」，这一列是「真正跑的哪天」（`scheduler.target_date()` 解析出来的）。
    -- 页面要按天回看任务流水，靠 LIKE '%20261016%' 去摸 detail 是不能用的。
    trade_date  TEXT,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,
    detail      TEXT
);
CREATE TABLE IF NOT EXISTS watchlist (
    code        TEXT PRIMARY KEY,
    -- 市场：cn=沪深北（代码 6 位）/ hk=港股（代码 5 位）。
    -- 已存在的行靠 _migrate 的 ALTER 补成默认 'cn'，老数据一条都不动。
    market      TEXT NOT NULL DEFAULT 'cn',
    name        TEXT NOT NULL,
    industry    TEXT,
    source_date TEXT NOT NULL,
    rank        INTEGER,
    score       REAL,
    price       REAL,
    snapshot    TEXT,
    added_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fund_watchlist (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    added_at    TEXT NOT NULL
);
-- T+1 误差复盘的两条流水。**不是行情快照**，按 (代码, 日期) 唯一，跨日累积：
--   estimate_log  每次点「查询估值」时，把当场算出来的估算写一行（同日重查 = 覆盖，
--                 因为越晚的估值越接近收盘口径）
--   nav_log       每次查询时，把接口带回的「最新单位净值 + 它的净值日期」写一行
-- 复盘的口径：某天既有估算、又能用前后两个真实净值算出当天实际涨跌幅，才算一个样本。
CREATE TABLE IF NOT EXISTS fund_estimate_log (
    code        TEXT NOT NULL,
    date        TEXT NOT NULL,
    est_pct     REAL,
    est_nav     REAL,
    source      TEXT NOT NULL DEFAULT '',
    coverage    REAL,
    gz_time     TEXT,
    logged_at   TEXT NOT NULL,
    PRIMARY KEY (code, date)
);
CREATE TABLE IF NOT EXISTS fund_nav_log (
    code        TEXT NOT NULL,
    nav_date    TEXT NOT NULL,
    nav         REAL NOT NULL,
    logged_at   TEXT NOT NULL,
    PRIMARY KEY (code, nav_date)
);
-- 订阅制三张表（docs/06 §3）。**刻意不碰 fund_watchlist / watchlist** ——
-- 那两张是「全局关注」（code 主键，网页端语义是「我自己的」），加 open_id 会
-- 把现有页面的含义整个改掉。多用户关注另起一张，两边互不干扰。
CREATE TABLE IF NOT EXISTS subscriber (
    open_id     TEXT PRIMARY KEY,          -- 飞书 open_id
    nickname    TEXT NOT NULL DEFAULT '',
    plan        TEXT NOT NULL DEFAULT '',  -- '' = 未订阅, 'pro'
    paid_at     TEXT,                      -- 本次订阅起算日
    expires_at  TEXT,                      -- paid_at + pro_days，过期即降级
    amount      REAL,                      -- 金额（占位价，订阅管理页可改）
    fund_limit  INTEGER,                   -- NULL = 用全局 fund_watch_limit
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS subscriber_fund (
    open_id   TEXT NOT NULL,
    code      TEXT NOT NULL,               -- 6 位基金代码
    name      TEXT NOT NULL DEFAULT '',
    added_at  TEXT NOT NULL,
    PRIMARY KEY (open_id, code)
);
-- 推送留痕：14:50 发没发、成没成、失败原因。订阅管理页的「消息推送情况」靠它。
CREATE TABLE IF NOT EXISTS push_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    open_id   TEXT NOT NULL,
    kind      TEXT NOT NULL,               -- 'fund_1450'
    sent_at   TEXT NOT NULL,
    ok        INTEGER NOT NULL,
    detail    TEXT NOT NULL DEFAULT ''
);
-- 收银台（docs/06 §2）。**两张都是占位**：没有支付网关、没有回调，订单只在
-- 「管理员照着它点开通」这一步有用。
--
-- pay_token：飞书里发出去的链接带的就是它的 token。**不把 open_id 裸放进 URL**
-- （会进浏览器历史、聊天转发、nginx 日志），token 也不可逆推身份，过期即作废。
CREATE TABLE IF NOT EXISTS pay_token (
    token      TEXT PRIMARY KEY,
    open_id    TEXT NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
-- 邀请链接（docs/06 §11）。与 pay_token **并列而不复用**：收银台那条链路
-- （下单 → 确认开通）已经真机跑通且有回归，把邀请塞进去要给 token 加一个
-- kind 字段再分流 —— 为了省一张表去改坏已验证的东西，不划算。
--
-- 身份是**在生成链接那一刻**就烧进 token 的：点一个裸的 `<a href>` 只发 URL，
-- 浏览器不会附带任何飞书身份（飞书在那条链路上不在场）。所以邀请链接必须
-- 一人一条；群发一条通用的谁点都一样，后端面对的就是匿名请求。
CREATE TABLE IF NOT EXISTS invite_token (
    token      TEXT PRIMARY KEY,
    open_id    TEXT NOT NULL,
    nickname   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
-- pay_order：一次「选了几个月、用什么付」的选择。status 走
-- pending(待人工确认) / done(已开通) / closed(作废)。
CREATE TABLE IF NOT EXISTS pay_order (
    order_no    TEXT PRIMARY KEY,
    open_id     TEXT NOT NULL,
    nickname    TEXT NOT NULL DEFAULT '',
    months      INTEGER NOT NULL,
    price       REAL NOT NULL,             -- 单价快照（下单那一刻，折合每月）
    amount      REAL NOT NULL,             -- 该档金额；没单独定价时 = price * months
    channel     TEXT NOT NULL,             -- 'wechat' / 'alipay'
    status      TEXT NOT NULL DEFAULT 'pending',
    token       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    confirmed_at TEXT,
    note        TEXT NOT NULL DEFAULT ''
);
-- 指令管理（「指令管理」页）。**一张表同时装内置与自定义**：内置指令的默认值
-- 写在 `app/cmdcfg.py` 里，这里只存「用户改过的部分」—— 表里没有这一行 = 用代码
-- 默认，所以「恢复默认」就是把行删掉，不存在两边打架的状态。
--
-- access 对**内置**指令只读（真正的闸仍是 `feishu._ACCESS`，库里的值只做展示）；
-- 自定义指令的 access 才生效，且只允许 any/sub/owner 三档里的后两档（见 cmdcfg）。
CREATE TABLE IF NOT EXISTS cmd_item (
    key         TEXT PRIMARY KEY,        -- 内置=固定 key；自定义=唯一 slug
    kind        TEXT NOT NULL DEFAULT 'custom',   -- 'builtin' / 'custom'
    label       TEXT NOT NULL DEFAULT '',         -- 展示名（按钮上写的就是它）
    text        TEXT NOT NULL DEFAULT '',         -- 主触发文本
    alias       TEXT NOT NULL DEFAULT '',         -- 额外别名，逗号分隔
    access      TEXT NOT NULL DEFAULT 'owner',    -- 仅自定义生效：sub / owner
    reply       TEXT NOT NULL DEFAULT '',         -- 执行后附带的推送文案，可空
    target      TEXT NOT NULL DEFAULT '',         -- 实际执行的指令原文，空=只回 reply
    enabled     INTEGER NOT NULL DEFAULT 1,
    quick       INTEGER NOT NULL DEFAULT 0,       -- 1 = 进快捷指令卡片
    sort        INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL
);

-- ------------------------------------------------------- 研究模块（docs/backtest）
--
-- 历史回测与策略研究。**只新增这五张表，一张既有业务表都不改不删**；迁移前
-- 的备份在 run/backup/。股票池每日快照**复用 snapshot 表**（dataset='universe'）
-- 而不另开表 —— 项目本就有按 (dataset, trade_date) 存 JSON 快照的通用机制，
-- 另建一张只会让两处的保留策略各自漂移。
--
-- SQLite 默认不开外键约束（PRAGMA foreign_keys=off），下面的 REFERENCES 主要
-- 是把关系写进 schema 供人与工具读懂；真正的完整性由 research 的 CRUD 层显式
-- 校验（experiment 不存在就拒写）+ 各表 UNIQUE 约束共同保证。
--
-- 每张表都带一条「缺失必须留空」的口径：excess/bench/收益类字段缺数据一律
-- NULL，**不许填 0** —— 0 是一个真实的收益率，会把「没算」和「没赚」混为一谈。

CREATE TABLE IF NOT EXISTS research_experiments (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    name                 TEXT NOT NULL,
    strategy_version     TEXT NOT NULL,
    config_snapshot_json TEXT NOT NULL,   -- 冻结的评分配置 + 实验参数
    config_hash          TEXT NOT NULL,   -- 上述 JSON 的 sha256，便于复现比对
    data_snapshot_id     TEXT,
    universe_mode        TEXT NOT NULL,   -- current=当前股票池回看 / historical
    start_date           TEXT NOT NULL,
    end_date             TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'created',
    -- created / running / done / failed / cancelled
    created_at           TEXT NOT NULL,
    started_at           TEXT,
    finished_at          TEXT,
    error_summary        TEXT
);
CREATE INDEX IF NOT EXISTS ix_research_exp_status ON research_experiments(status);
CREATE INDEX IF NOT EXISTS ix_research_exp_hash   ON research_experiments(config_hash);

CREATE TABLE IF NOT EXISTS research_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id INTEGER NOT NULL REFERENCES research_experiments(id),
    run_number    INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',
    -- pending / running / done / failed / cancelled
    progress      REAL NOT NULL DEFAULT 0,   -- 百分比 0~100
    stage         TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    error_summary TEXT,
    warnings_json TEXT,
    -- 同一实验的 run_number 唯一：重复点「运行」不会凭空多出一条并行记录
    UNIQUE (experiment_id, run_number)
);
CREATE INDEX IF NOT EXISTS ix_research_runs_exp ON research_runs(experiment_id, status);

CREATE TABLE IF NOT EXISTS research_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  INTEGER NOT NULL REFERENCES research_experiments(id),
    run_id         INTEGER REFERENCES research_runs(id),
    signal_date    TEXT NOT NULL,
    code           TEXT NOT NULL,
    model_version  TEXT NOT NULL,
    score          REAL,                      -- 综合得分
    factors_json   TEXT NOT NULL,             -- 每因子原始值 + 单项分（可追溯）
    rank           INTEGER,
    top5           INTEGER NOT NULL DEFAULT 0,
    top10          INTEGER NOT NULL DEFAULT 0,
    top30          INTEGER NOT NULL DEFAULT 0,
    data_quality   TEXT NOT NULL DEFAULT 'READY',
    missing_json   TEXT,                      -- 不可用因子及原因；无缺失为 NULL
    -- 防重复：同一实验同一天同一只券同一模型版本只留一条
    UNIQUE (experiment_id, signal_date, code, model_version)
);
CREATE INDEX IF NOT EXISTS ix_research_sig_exp
    ON research_signals(experiment_id, signal_date);

CREATE TABLE IF NOT EXISTS research_outcomes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  INTEGER NOT NULL REFERENCES research_experiments(id),
    signal_date    TEXT NOT NULL,
    code           TEXT NOT NULL,
    horizon        INTEGER NOT NULL,          -- T+n，按**交易日**计
    status         TEXT NOT NULL,
    -- PENDING=观察期未走完 / DONE=已完成 / NO_DATA=缺行情
    theory_return  REAL,                      -- 理论收益；非 DONE 一律 NULL
    bench_return   REAL,                      -- 基准收益；缺基准为 NULL（不是 0）
    excess_return  REAL,                      -- 不可算即 NULL，**绝不填 0**
    entry_date     TEXT,                      -- D+1
    entry_price    REAL,
    exit_date      TEXT,
    exit_price     REAL,
    fill_status    TEXT,                      -- 入场：FILLED / UNFILLABLE / UNCONFIRMED / NOT_DUE
    sell_status    TEXT,                      -- 出场：SELLABLE / LIMIT_DOWN / UNCONFIRMED / NOT_DUE
    exec_return    REAL,                      -- 模拟可执行收益（扣成本），与理论分开
    mfe            REAL,                      -- 最大有利波动
    mae            REAL,                      -- 最大不利波动
    -- 复权口径（阶段 5）。收益一律按折算后的可比价算，这里记**怎么算的**：
    --   RAW                     两端因子都有且不同 => 折算过
    --   UNADJUSTED              因子相同 => 期间无除权，不复权价直算即正确
    --   UNADJUSTED_UNCERTAIN    缺 adj_factor => 判不了，收益可能含除权跳空
    -- NULL 只出现在 status != DONE 的行上 —— 没算出收益就谈不上口径。
    adjust_method  TEXT,
    adjust_note    TEXT,                      -- UNCERTAIN 时写明「可能含除权」
    entry_factor   REAL,                      -- 入场日复权系数（取不到给 1.0）
    exit_factor    REAL,                      -- 出场日复权系数（取不到给 1.0）
    data_quality   TEXT,
    missing_reason TEXT,
    UNIQUE (experiment_id, signal_date, code, horizon)
);
CREATE INDEX IF NOT EXISTS ix_research_out_exp
    ON research_outcomes(experiment_id, horizon);

CREATE TABLE IF NOT EXISTS research_metrics (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id  INTEGER NOT NULL REFERENCES research_experiments(id),
    portfolio_rule TEXT NOT NULL,             -- top5 / top10 / top30 / layer:10
    horizon        INTEGER NOT NULL,
    valid_signals  INTEGER NOT NULL DEFAULT 0,
    valid_fills    INTEGER NOT NULL DEFAULT 0,
    avg_return     REAL,
    median_return  REAL,
    win_rate       REAL,
    avg_excess     REAL,
    median_excess  REAL,
    p10            REAL,
    p25            REAL,
    p75            REAL,
    p90            REAL,
    max_drawdown   REAL,                      -- 仅在明确构建组合净值曲线时才填
    trading_cost   REAL,
    unfilled_rate  REAL,
    pending_count  INTEGER NOT NULL DEFAULT 0,  -- 未完成观察期，不计入收益统计
    no_data_count  INTEGER NOT NULL DEFAULT 0,  -- 缺行情
    quality_json   TEXT,                       -- 样本质量警告
    computed_at    TEXT NOT NULL,
    -- 唯一键以 experiment_id 打头，查询按实验过滤时正好走这个索引，
    -- 不再另建一张会重复的单列索引。
    UNIQUE (experiment_id, portfolio_rule, horizon)
);
-- 采集任务的**逐域**状态（阶段 3）。
--
-- 与 job_run 的区别：job_run 是「一轮流水线」的整条记录，域级成败散在
-- detail 那个 JSON 快照里 —— 进程一重启就再也查不出「pool_zt 上周三失败过
-- 几次、下次什么时候重试」。这张表把那一格拆出来存，覆盖审计才能给出
-- 权威的「最近一次尝试 / 最近一次错误」，而不是从 JSON 里推断。
--
-- 唯一键 (domain, trade_date)：同一天重跑是**覆盖**不是新增 —— 这也是幂等
-- 的落点，跟 snapshot 的复合主键同一套思路。
CREATE TABLE IF NOT EXISTS ingest_job (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    domain        TEXT NOT NULL,
    trade_date    TEXT NOT NULL,
    -- WAITING 排队未跑 / RUNNING 正在跑 / SUCCESS 跑完且过校验 /
    -- PARTIAL 跑完但数据不完整（行数偏少、字段缺失）/ FAILED 异常或重试用尽
    status        TEXT NOT NULL DEFAULT 'WAITING',
    -- 已尝试次数与上限。有上限是硬要求：源挂了的时候无限重试会把请求
    -- 打爆，也会让页面上「最近一次错误」永远被下一次失败盖掉。
    attempts      INTEGER NOT NULL DEFAULT 0,
    max_attempts  INTEGER NOT NULL DEFAULT 3,
    -- 退避后的下次重试时刻（UTC ISO）。NULL = 不再重试或从没失败过。
    next_retry_at TEXT,
    -- 三级计数，缺一不可：
    --   processed 源返回了多少行；stored 实际落库多少行；failed_rows 校验没过多少行。
    -- 只记 stored 会分不清「源只给了 30 行」和「源给了 4782 行但只存下 30 行」。
    processed     INTEGER,
    stored        INTEGER,
    failed_rows   INTEGER,
    -- 这一域的数据是从哪个源拿的。一个域常常有多个源（东财、腾讯、同花顺），
    -- 出了问题要能答「这批数据到底是哪儿来的」，不能只写在日志里。
    -- 例：'em:ztb' / 'tencent:hist_tx' / 'sina:hfq-factor' / '10jqka/em'
    source        TEXT,
    -- 覆盖范围与校验结果，两件事各一列：
    --   coverage_rate   0..1 的覆盖率；NULL = **没算过**（绝不是 0 —— 那是
    --                   「算出来一点都没有」，两者差着十万八千里）
    --   coverage_detail JSON：本次的 processed/stored/failed_rows/校验原因，
    --                   外加 `data_quality_check` 补的窗口缺口清单
    -- 刻意不做成一个「综合分」：覆盖少和字段缺是两种病，混成一个数就没法对症。
    coverage_rate    REAL,
    coverage_detail  TEXT,
    error         TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    updated_at    TEXT NOT NULL,
    UNIQUE (domain, trade_date)
);
-- 历史日线（阶段 4 回填）。**落的是不复权 OHLC + 独立复权因子 `adj_factor`**
-- —— 绝不落前复权 `qfq`：前复权是「从今天往回算」的价格，日后配息或除权
-- 会让整段历史跟着变，等于回测里的一个不断漂移的目标（docs/backtest/§3）。
-- `fetchers/quotes.daily_history()` 里写死 `adjust="qfq"`，回填**不走它**，
-- 直接调 akshare 并显式传 `adjust=""`。
CREATE TABLE IF NOT EXISTS daily_bar (
    code        TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    turnover    REAL,          -- 百分数
    pct_chg     REAL,          -- 百分数
    adj_factor  REAL,          -- 复权因子；取不到就 NULL，不填 1.0 充数
    source      TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (code, trade_date)
);
-- 指数日线（沪深300 = sh000300）。表名必须含 `index` —— research/engine 的
-- `_load_bench` 只认「名字里明确写着指数」的表，不猜「某张表里有没有指数代码」。
CREATE TABLE IF NOT EXISTS index_daily (
    code        TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    pct_chg     REAL,
    source      TEXT,
    fetched_at  TEXT,
    PRIMARY KEY (code, trade_date)
);
-- ==== 自动买卖策略 V1（docs/ai_trading_lab/）==============================
-- 策略参数按版本留档。版本 = 归一化 JSON 的 sha256 前 12 位；每次运行实际
-- 使用的参数快照另存 strategy_run.params_json —— 复现历史结果的第一手证据。
CREATE TABLE IF NOT EXISTS strategy_params (
    version     TEXT PRIMARY KEY,
    params      TEXT NOT NULL,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
-- 一次信号生成。UNIQUE(strategy, signal_date) 是幂等的第一道闸：同策略同日
-- 重跑不会产生第二轮信号。status=REFUSED 是「明确的不交易」，理由落列。
CREATE TABLE IF NOT EXISTS strategy_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy        TEXT NOT NULL,
    signal_date     TEXT NOT NULL,
    exec_date       TEXT,
    rule_version    TEXT NOT NULL,
    param_version   TEXT NOT NULL,
    params_json     TEXT NOT NULL,
    status          TEXT NOT NULL,        -- OK / REFUSED
    refusal_reason  TEXT,
    risk_state      TEXT,                 -- NORMAL / RISK（数据风险门）
    quality_state   TEXT,                 -- rank_quality: ok / incomplete
    quality_json    TEXT,
    candidates_n    INTEGER NOT NULL DEFAULT 0,
    signals_n       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE (strategy, signal_date)
);
-- 交易信号。HOLD（明确的不交易结果）也落行 —— 候选不足/条件不满足必须留痕，
-- 否则「没信号」分不清是没触发还是没看见。一个 run 内一只票至多一条。
CREATE TABLE IF NOT EXISTS trade_signal (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL,
    code          TEXT NOT NULL,
    name          TEXT,
    side          TEXT NOT NULL,          -- BUY / SELL / HOLD
    signal_date   TEXT NOT NULL,
    exec_date     TEXT,
    reasons       TEXT NOT NULL,          -- JSON [{"rule","detail"}]
    target_qty    INTEGER,                -- 股数；HOLD 恒 NULL
    alloc_pct     REAL,                   -- 目标仓位（占权益）
    est_price     REAL,                   -- 信号日收盘参考价
    rule_version  TEXT NOT NULL,
    param_version TEXT NOT NULL,
    snapshot_date TEXT,                   -- 依赖的六维快照日期
    bar_date      TEXT,                   -- 依赖的行情日期（缺行情为 NULL）
    created_at    TEXT NOT NULL,
    UNIQUE (run_id, code)
);
-- 模拟账户单行 (id=1)。现金只能由 sim_settle 记账事务改写，策略层无权触碰。
CREATE TABLE IF NOT EXISTS sim_account (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    cash         REAL NOT NULL,
    initial_cash REAL NOT NULL,
    updated_at   TEXT NOT NULL
);
-- 模拟订单。signal_id UNIQUE = 一个信号至多一张单（重复运行不重复下单）。
CREATE TABLE IF NOT EXISTS sim_order (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id     INTEGER NOT NULL UNIQUE,
    code          TEXT NOT NULL,
    name          TEXT,
    side          TEXT NOT NULL,          -- BUY / SELL
    qty           INTEGER NOT NULL,
    status        TEXT NOT NULL,          -- PENDING/FILLED/REJECTED/EXPIRED
    exec_date     TEXT,
    est_price     REAL,
    fill_price    REAL,
    fee           REAL,
    reject_reason TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
-- 成交。order_id UNIQUE = 一个订单至多记一次账（重复执行不重复记账）。
CREATE TABLE IF NOT EXISTS sim_fill (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   INTEGER NOT NULL UNIQUE,
    code       TEXT NOT NULL,
    side       TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    price      REAL NOT NULL,
    fee        REAL NOT NULL,
    cash_delta REAL NOT NULL,
    trade_date TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- 持仓。locked_qty/locked_date = 最近一次买入中 T+1 不可卖的部分及其买入日 ——
-- 解锁条件是「买入日 < 新执行日」，条件式判断天然幂等，重复执行不重复解锁。
-- cost_factor = 入场日 adj_factor，退出规则跨除权折算用。
CREATE TABLE IF NOT EXISTS sim_position (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    qty         INTEGER NOT NULL,
    locked_qty  INTEGER NOT NULL DEFAULT 0,
    locked_date TEXT,
    avg_cost    REAL NOT NULL,
    cost_factor REAL,
    entry_date  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- ==== 实时纸面自动交易（rules.py 规则引擎 + live_loop.py 实时循环）==========
-- 规则实例：id 主键，params JSON。整套规则版本 = rules.rules_version()。
CREATE TABLE IF NOT EXISTS trading_rule (
    id          TEXT PRIMARY KEY,
    category    TEXT NOT NULL,            -- filter / exit
    type        TEXT NOT NULL,            -- rules.py 注册表里的类型键
    name        TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    order_n     INTEGER NOT NULL DEFAULT 0,
    params      TEXT NOT NULL,            -- JSON
    updated_at  TEXT NOT NULL
);
-- 实时纸面账户单行 (id=1)。与 daily 策略的 sim_account **分开**：实时按现价
-- 即时成交，daily 是 D+1 开盘撮合，记账语义不同，不混用一个账户。
CREATE TABLE IF NOT EXISTS live_account (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    cash          REAL NOT NULL,
    initial_cash  REAL NOT NULL,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    updated_at    TEXT NOT NULL
);
-- 实时纸面持仓。peak_price = 入场以来最高价（移动止盈基准）。
CREATE TABLE IF NOT EXISTS live_position (
    code        TEXT PRIMARY KEY,
    name        TEXT,
    qty         INTEGER NOT NULL,
    avg_cost    REAL NOT NULL,
    peak_price  REAL,
    entry_ts    TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
-- 实时纸面成交流水。每笔买卖落一行，页面「今日交易」直接查这里。
CREATE TABLE IF NOT EXISTS live_trade (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    code      TEXT NOT NULL,
    name      TEXT,
    side      TEXT NOT NULL,            -- BUY / SELL
    qty       INTEGER NOT NULL,
    price     REAL NOT NULL,
    amount    REAL NOT NULL,
    fee       REAL NOT NULL,
    pnl       REAL,                     -- 卖出实现盈亏（买入为 NULL）
    rule      TEXT,                     -- 触发规则名
    note      TEXT
);
CREATE INDEX IF NOT EXISTS ix_live_trade_ts ON live_trade(ts);
"""


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(_SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """补历史库缺的列。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表**不做任何改动**，加字段必须
    另跑 ALTER TABLE；幂等，靠 PRAGMA 判断。
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(job_run)")}
    if "kind" not in cols:
        conn.execute("ALTER TABLE job_run ADD COLUMN kind "
                     "TEXT NOT NULL DEFAULT 'manual'")
        logger.info("job_run 已补列 kind")
    if "trade_date" not in cols:
        conn.execute("ALTER TABLE job_run ADD COLUMN trade_date TEXT")
        logger.info("job_run 已补列 trade_date（老行留 NULL）")

    # 逐域采集状态：数据源 + 覆盖范围/校验结果。三列一起补 —— 少一列，
    # `ingest_finish` 的列清单就与表结构对不上，写入直接 no such column。
    jcols = {row[1] for row in conn.execute("PRAGMA table_info(ingest_job)")}
    if jcols and "source" not in jcols:
        for c in ("source", "coverage_rate", "coverage_detail"):
            conn.execute("ALTER TABLE ingest_job ADD COLUMN %s" % c)
        logger.info("ingest_job 已补列 source / coverage_rate / coverage_detail"
                    "（存量行留 NULL —— 没算过就是 NULL，不填 0）")

    wcols = {row[1] for row in conn.execute("PRAGMA table_info(watchlist)")}
    if "market" not in wcols:
        conn.execute("ALTER TABLE watchlist ADD COLUMN market "
                     "TEXT NOT NULL DEFAULT 'cn'")
        logger.info("watchlist 已补列 market（存量按 A 股处理）")

    # 研究模块：出场成交状态单独成列。建表语句里已经带了这一列（新库直接
    # 有），这里只补**先于该列加入而创建的**库 —— PRAGMA 看得到表才有内容，
    # 新库会在上面就被判定为"已有"，两边不会打架。
    ocols = {row[1] for row in conn.execute("PRAGMA table_info(research_outcomes)")}
    if ocols and "sell_status" not in ocols:
        conn.execute("ALTER TABLE research_outcomes ADD COLUMN sell_status TEXT")
        logger.info("research_outcomes 已补列 sell_status")

    # 阶段 5：收益的复权口径。四列一起补 —— 少一列，`research_outcomes_replace`
    # 的列清单就会与表结构对不上，写入直接 no such column。
    if ocols and "adjust_method" not in ocols:
        for c in ("adjust_method", "adjust_note", "entry_factor", "exit_factor"):
            conn.execute(
                "ALTER TABLE research_outcomes ADD COLUMN %s" % c)
        logger.info("research_outcomes 已补列 复权口径四列（存量行留 NULL）")


def init_db() -> None:
    _connect()


def save(dataset: str, trade_date: str, rows: Iterable[Dict[str, Any]],
         source: str = "") -> int:
    """幂等写入。rows 可为 dict 列表。"""
    payload = [dict(r) for r in rows]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO snapshot(dataset, trade_date, payload, row_count, source, fetched_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(dataset, trade_date) DO UPDATE SET
                 payload=excluded.payload, row_count=excluded.row_count,
                 source=excluded.source, fetched_at=excluded.fetched_at""",
            (dataset, trade_date, json.dumps(payload, ensure_ascii=False,
                                              default=str),
             len(payload), source, now),
        )
        conn.commit()
    logger.info("已落库 %s @ %s rows=%d", dataset, trade_date, len(payload))
    return len(payload)


def load(dataset: str, trade_date: str) -> Optional[List[Dict[str, Any]]]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT payload FROM snapshot WHERE dataset=? AND trade_date=?",
            (dataset, trade_date),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["payload"])


def save_one(dataset: str, trade_date: str, obj: Any, source: str = "") -> int:
    """写入单个对象（复用 snapshot 表，payload 为长度 1 的列表）。"""
    return save(dataset, trade_date, [obj], source=source)


def load_one(dataset: str, trade_date: str) -> Optional[Dict[str, Any]]:
    """读取 save_one 写入的对象；没有则返回 None。"""
    rows = load(dataset, trade_date)
    if not rows or not isinstance(rows, list) or not isinstance(rows[0], dict):
        return None
    return rows[0]


def load_one_fields(dataset: str, trade_date: str,
                    obj_fields: Sequence[str] = ("rows",),
                    text_fields: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """只取 payload 里的几个顶层字段，**绕开 Python 侧对整个对象的 `json.loads`**。

    起因是上榜矩阵：`main_force` 的 payload 里 `all_rows` 占 81%（实测每天
    约 200 KB，而矩阵要的 `rows` 只有 22 KB），跨日 90 天会白解析约 20 MB、
    真正用到的不到 10%。改走 SQLite 的 `json_extract`，传输与解析都只碰
    需要的那部分（SQLite 内建 JSON1，实测可用）。

    `obj_fields` 是对象/数组字段 —— `json_extract` 回的是 JSON 文本，要再 loads；
    `text_fields` 是字符串字段 —— 它回的是**已解引号的原文**，再 loads 会炸。
    字段名只来自代码字面量，不进用户输入。

    返回 None 的口径与 `load_one` 对齐：没这条快照，或两个字段都是 NULL。
    """
    exprs = ["json_extract(payload,'$[0].%s') AS %s" % (f, f)
             for f in (*obj_fields, *text_fields)]
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT %s FROM snapshot WHERE dataset=? AND trade_date=?" % ", ".join(exprs),
            (dataset, trade_date),
        ).fetchone()
    if row is None:
        return None
    out: Dict[str, Any] = {f: (json.loads(row[f]) if row[f] is not None else None)
                           for f in obj_fields}
    out.update({f: row[f] for f in text_fields})
    return out if any(v is not None for v in out.values()) else None


def latest_date(dataset: str) -> Optional[str]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT MAX(trade_date) AS d FROM snapshot WHERE dataset=?",
            (dataset,),
        ).fetchone()
    return row["d"] if row and row["d"] else None


def list_dates(dataset: str, limit: int = 30) -> List[str]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT trade_date FROM snapshot WHERE dataset=? "
            "GROUP BY trade_date ORDER BY trade_date DESC LIMIT ?",
            (dataset, limit),
        ).fetchall()
    return [r["trade_date"] for r in rows]


def snapshot_meta(dataset: str) -> Optional[Dict[str, Any]]:
    """该数据集最新一条快照的元信息（不含 payload）。

    总览页要显示「数据新鲜度」——只给 rows 没法说清数据是哪天、什么时候抓的。
    板块资金流的 trade_date 恒为 "latest"，那时看 fetched_at 就够了。
    """
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT trade_date, row_count, source, fetched_at FROM snapshot "
            "WHERE dataset=? ORDER BY trade_date DESC LIMIT 1",
            (dataset,),
        ).fetchone()
    return dict(row) if row else None


# ----------------------------------------------------------------- 任务运行
#
# 曾经的 record_job(status, detail, started_at, finished) 有两处死参数：
#   - started_at 调用方永远传空 -> 永远走 INSERT 分支，UPDATE 逻辑是死代码
#   - finished   参数签名里有，函数体从头到尾没引用过
# 结果 /api/jobs/status 拿到的永远是 status="manual"、finished_at=null。
# 拆成 job_start / job_finish 两个语义明确的函数，配 id 关联，不再靠字符串。


def job_start(kind: str, detail: str = "", trade_date: str = "") -> int:
    """开一条任务记录（status='running'），返回 id。

    ``trade_date`` 与 ``detail`` 是两回事：detail 是自由文本快照（想写什么写
    什么），这一列是**可按天索引**的那一个 —— 页面要回看「10 月 16 日那轮
    跑得怎么样」，靠 LIKE 去摸 detail 是不能用的。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO job_run(kind, trade_date, started_at, status, detail) "
            "VALUES (?,?,?,?,?)",
            (kind, trade_date or None, now, "running", detail),
        )
        conn.commit()
        return int(cur.lastrowid)


def job_finish(job_id: int, status: str, detail: str = "") -> None:
    """收尾任务：写 finished_at 与最终 status。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE job_run SET finished_at=?, status=?, detail=? WHERE id=?",
            (now, status, detail, job_id),
        )
        conn.commit()


def last_job() -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM job_run ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def jobs(limit: int = 20, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """最近若干次任务记录，新的在前（「定时任务」页的运行历史）。

    ``kind`` 给了就只回那一类（`daily_market_sync` 这种）—— 六个任务各自要能
    答「我上次什么时候跑的、成没成」，让调用方把 20 条捞回来自己筛，等于把
    一次索引查询摊给每次页面刷新。
    """
    with _lock:
        conn = _connect()
        if kind:
            rows = conn.execute(
                "SELECT * FROM job_run WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit)).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM job_run ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------- 逐域采集状态
#
# 存在的理由：job_run.detail 是**整轮**流水线的 JSON 快照，域级的成败、重试
# 次数、下次重试时刻塞在里面既查不动，进程一重启也说不清「上周三 pool_zt
# 失败过几次」。要让覆盖审计给出权威结论（而不是像现在那样从 JSON 推断、
# 老老实实标 inferred），就得有一张能按 (domain, trade_date) 直接查的表。
#
# 一句提醒：`_lock` 不可重入，每个函数**只取一次**锁 —— 里面再调别的 CRUD
# 会自己死锁。sqlite3.Row 也没有 `.get`，读出来一律 `dict(r)`。


def ingest_start(domain: str, trade_date: str) -> None:
    """标记「这一域这一天开跑了」，尝试次数 +1。

    用 UPSERT 而不是先查后插：重跑同一天是**覆盖**，这也正是幂等的落点 ——
    与 snapshot 的复合主键同一套思路。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO ingest_job(domain, trade_date, status, attempts,
                                      started_at, updated_at)
               VALUES (?,?,'RUNNING',1,?,?)
               ON CONFLICT(domain, trade_date) DO UPDATE SET
                 status     = 'RUNNING',
                 attempts   = ingest_job.attempts + 1,
                 started_at = excluded.started_at,
                 finished_at = NULL,
                 error      = NULL,
                 next_retry_at = NULL,
                 updated_at = excluded.updated_at""",
            (domain, trade_date, now, now))
        conn.commit()


#: RUNNING 超过这么久就当「上一轮的进程挂了」，允许接管。
#:
#: 这个上限是**必须**的：没有它，一次崩溃会把那一域那一天永久锁在 RUNNING，
#: 谁也别想再采 —— 页面上写着「正在采集」，却永远不会有下文。30 分钟的依据
#: 是最慢的一步（打分）自己也只要十几分钟，池子/板块都是秒级。
RUNNING_STALE_SEC = 1800


class DomainBusy(RuntimeError):
    """同 (domain, trade_date) 已有一轮在跑，且还没超时。

    抛出来是为了让调用方把「**没跑**」和「**跑挂了**」分开记 —— 前者不该进
    `failed`（那条正在跑的线程正在干活，它没失败），后者才该。任务书的
    「相同任务不得并发重复执行」落在这里。
    """

    def __init__(self, domain: str, trade_date: str, started_at: str = ""):
        self.domain = domain
        self.trade_date = trade_date
        self.started_at = started_at
        super().__init__(
            "%s @ %s 正在采集中%s，不重复执行" % (
                domain, trade_date,
                "（开始于 %s）" % started_at if started_at else ""))


def _stale(row: sqlite3.Row, stale_seconds: int) -> bool:
    """这一行的 RUNNING 是不是已经「陈旧」到可以被接管。

    时间读不出来一律算陈旧 —— 拿不准的时候宁可放过一次重复抓取（无非是
    多发一个请求），也不要因为一个坏时间戳把这域这天锁死到天荒地老。
    """
    raw = row["started_at"] or row["updated_at"]
    try:
        started = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return True
    return (datetime.now(timezone.utc) - started).total_seconds() > stale_seconds


#: 状态枚举（WAITING/RUNNING/…）定义在 `app.pipeline.ingest`，那边 import 了
#: 本模块，这里**不能再 import 回去** —— 循环导入。与 `ingest_start` 里 SQL
#: 写死 `'RUNNING'` 是同一个理由：状态字面量在这一层就得是字面量。
_RUNNING = "RUNNING"


def ingest_busy(domain: str, trade_date: str,
                stale_seconds: int = RUNNING_STALE_SEC) -> Optional[Dict[str, Any]]:
    """挡住这次执行的那一行（RUNNING 且未超时），否则 None。

    给**不该起跑**的地方用（页面上明摆着还在跑就别开线程了）。真正的互斥
    在 `ingest_try_start` —— 这里只是一次只读预判。
    """
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT domain, trade_date, status, attempts, started_at, "
            "updated_at FROM ingest_job "
            "WHERE domain=? AND trade_date=?", (domain, trade_date)).fetchone()
    if not row or row["status"] != _RUNNING or _stale(row, stale_seconds):
        return None
    return dict(row)


def ingest_try_start(domain: str, trade_date: str, source: str = "",
                     stale_seconds: int = RUNNING_STALE_SEC) -> None:
    """`ingest_start` 的**互斥版**，采集路径一律走这条。

    同域同天已有一轮 RUNNING 且未超时 → 抛 `DomainBusy`，**一个字段都不写**
    （不加 attempts、不清 next_retry_at —— 写了就把正在跑的那一轮的退避信息
    冲掉了）。抢到之后行为与 `ingest_start` 完全一致。

    判定与置位必须在**同一个临界区**里做完：先查一眼「没在跑」再回去抓，
    中间那条缝里另一线程也能同时通过检查，两边一起发请求 —— 互斥就白做了。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT status, started_at, updated_at FROM ingest_job "
            "WHERE domain=? AND trade_date=?", (domain, trade_date)).fetchone()
        if (row and row["status"] == _RUNNING
                and not _stale(row, stale_seconds)):
            raise DomainBusy(domain, trade_date, row["started_at"] or "")
        conn.execute(
            """INSERT INTO ingest_job(domain, trade_date, status, attempts,
                                      source, started_at, updated_at)
               VALUES (?,?,'RUNNING',1,?,?,?)
               ON CONFLICT(domain, trade_date) DO UPDATE SET
                 status     = 'RUNNING',
                 attempts   = ingest_job.attempts + 1,
                 source     = COALESCE(NULLIF(excluded.source,''),
                                       ingest_job.source),
                 started_at = excluded.started_at,
                 finished_at = NULL,
                 error      = NULL,
                 next_retry_at = NULL,
                 updated_at = excluded.updated_at""",
            (domain, trade_date, source or None, now, now))
        conn.commit()


def ingest_finish(domain: str, trade_date: str, status: str,
                  processed: Optional[int] = None,
                  stored: Optional[int] = None,
                  failed_rows: Optional[int] = None,
                  error: Optional[str] = None,
                  coverage_rate: Optional[float] = None,
                  coverage_detail: Optional[Dict[str, Any]] = None,
                  source: Optional[str] = None) -> None:
    """收尾一域一天。``processed`` 为 None 表示**没跑成**，不是「跑了 0 行」。

    ``coverage_rate`` / ``coverage_detail`` / ``source`` 不传就**保持原值不动**：
    一次抛异常的重试不该把上一轮算好的覆盖范围抹掉 —— 覆盖率描述的是**数据**，
    不是**这次尝试**。反过来，跑成功了就照实写，旧值自然被覆盖。

    ``source`` 同理走 COALESCE：这轮没取到源信息（比如异常路径）时，保留上一轮
    知道的「这批数据是哪儿来的」，而不是写成 NULL 让人以为来历不明。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    detail = (json.dumps(coverage_detail, ensure_ascii=False,
                         default=str)
              if coverage_detail is not None else None)
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE ingest_job SET status=?, processed=?, stored=?, "
            "failed_rows=?, error=?, finished_at=?, updated_at=?, "
            "coverage_rate = COALESCE(?, coverage_rate), "
            "coverage_detail = COALESCE(?, coverage_detail), "
            "source = COALESCE(?, source) "
            "WHERE domain=? AND trade_date=?",
            (status, processed, stored, failed_rows, error, now, now,
             coverage_rate, detail, source, domain, trade_date))
        conn.commit()


def ingest_set_coverage(domain: str, trade_date: str,
                        coverage_rate: Optional[float],
                        coverage_detail: Optional[Dict[str, Any]]) -> bool:
    """**只**更新覆盖范围两列，不碰 status / processed / stored。

    给 `data_quality_check` 用：它是采集**之后**跑的独立任务，只补充「这个域
    的日期窗口盖了几天、缺哪几天」。要是走 `ingest_finish`，就等于让它替采集
    那一步重写一遍状态 —— 它并不知道那一步实际跑成了什么样。

    ``coverage_rate`` 传 None 表示**没算出来**（不许写 0，也不许把已有的抹掉）。
    返回有没有真的改到行（没这个 (domain, trade_date) 的记录就 False）。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    detail = (json.dumps(coverage_detail, ensure_ascii=False, default=str)
              if coverage_detail is not None else None)
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE ingest_job SET coverage_rate=?, coverage_detail=?, "
            "updated_at=? WHERE domain=? AND trade_date=?",
            (coverage_rate, detail, now, domain, trade_date))
        conn.commit()
        return bool(cur.rowcount)


def ingest_set_retry(domain: str, trade_date: str,
                     next_retry_at: Optional[str]) -> None:
    """排下次重试时刻；传 None 即取消重试（重试用尽或人工放弃）。"""
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE ingest_job SET next_retry_at=?, updated_at=? "
            "WHERE domain=? AND trade_date=?",
            (next_retry_at,
             datetime.now(timezone.utc).isoformat(timespec="seconds"),
             domain, trade_date))
        conn.commit()


def ingest_get(domain: str, trade_date: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM ingest_job WHERE domain=? AND trade_date=?",
            (domain, trade_date)).fetchone()
    return dict(row) if row else None


def ingest_list(domain: Optional[str] = None, limit: int = 50
                ) -> List[Dict[str, Any]]:
    """采集状态，新的在前。不带 payload —— 这张表本来就没有。"""
    with _lock:
        conn = _connect()
        if domain:
            rows = conn.execute(
                "SELECT * FROM ingest_job WHERE domain=? "
                "ORDER BY trade_date DESC, id DESC LIMIT ?", (domain, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ingest_job ORDER BY trade_date DESC, id DESC "
                "LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def ingest_due(now_iso: Optional[str] = None) -> List[Dict[str, Any]]:
    """到点该重试的那些行：``next_retry_at`` 已过，且还没用尽重试次数。

    只回**该重试的**，不回「失败过但还在退避窗口里」的 —— 把后者也捞出来
    等于绕过了退避，源挂着的时候会被反复打。
    """
    now = now_iso or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM ingest_job WHERE next_retry_at IS NOT NULL "
            "AND next_retry_at <= ? AND attempts < max_attempts "
            "ORDER BY next_retry_at ASC", (now,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------ 行情历史
#
# 表名只来自代码字面量，但仍然走白名单 + 参数占位符 —— 表名没法用占位符，
# 只能拼字符串，白名单是拼之前唯一能设的闸。

BAR_TABLES = ("daily_bar", "index_daily")
#: 两张表**共有的**列顺序。实际写入哪些列按表的真实 schema 取交集 ——
#: `index_daily` 没有 turnover / adj_factor，硬按 daily_bar 的清单写会直接
#: `no such column`（2026-10-16 回归测试抓到过）。
_BAR_ORDER = ("code", "trade_date", "open", "high", "low", "close",
              "volume", "amount", "turnover", "pct_chg", "adj_factor",
              "source", "fetched_at")


def _bar_table(table: str) -> str:
    if table not in BAR_TABLES:
        raise ValueError(f"不允许写入的行情表：{table}")
    return '"' + table + '"'


def _bar_cols(conn: sqlite3.Connection, table: str) -> List[str]:
    """该表真实存在、且属于行情列的那些列，按 `_BAR_ORDER` 排序。

    每次调用查一次 PRAGMA：这是批处理入口（一次调用写几百上千行），多一次
    PRAGMA 的开销可以忽略，换来的是「换库/换 schema 不会拿到过期列名」——
    缓存列名在测试频繁换 `db_path` 时会悄悄写错表。
    """
    have = {row[1] for row in conn.execute(f"PRAGMA table_info({_bar_table(table)})")}
    return [c for c in _BAR_ORDER if c in have]


def bars_upsert(table: str, rows: Sequence[Dict[str, Any]]) -> int:
    """批量插入行情，**已有的键一律不覆盖**，返回真正新插入的行数。

    `ON CONFLICT DO NOTHING` 而不是 DO UPDATE，是任务书「不覆盖未经确认的
    历史数据」的落点：回填最怕的是把已经核对过的行情用一次重新抓取静默盖掉，
    而这次抓取恰好源返回了半截数据 —— 那样连追溯的可能都没了。要重写必须
    走 `bars_overwrite()` 并显式传值，留下意图。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    params: List[tuple] = []
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        cols = _bar_cols(conn, table)
        marks = ", ".join("?" for _ in cols)
        for r in rows:
            params.append(tuple(
                now if c == "fetched_at" else r.get(c) for c in cols))
        before = conn.total_changes
        # executemany 而不是 execute：execute 只收**一组**参数，多行会直接
        # ProgrammingError。total_changes 对 executemany 同样累计，
        # 差值就是真正插进去的行数（ON CONFLICT 忽略掉的不计）。
        conn.executemany(
            f"INSERT INTO {T} ({', '.join(cols)}) VALUES ({marks}) "
            "ON CONFLICT(code, trade_date) DO NOTHING", params)
        conn.commit()
        return int(conn.total_changes - before)


def bars_overwrite(table: str, rows: Sequence[Dict[str, Any]]) -> int:
    """**显式覆盖**已有行情。回填默认不走这里 —— 要重写须调用方自己决定。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        cols = _bar_cols(conn, table)
        marks = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols
                            if c not in ("code", "trade_date"))
        params = [tuple(now if c == "fetched_at" else r.get(c)
                        for c in cols) for r in rows]
        before = conn.total_changes
        # 同 bars_upsert：批量必须 executemany。
        conn.executemany(
            f"INSERT INTO {T} ({', '.join(cols)}) VALUES ({marks}) "
            f"ON CONFLICT(code, trade_date) DO UPDATE SET {updates}",
            params)
        conn.commit()
        return int(conn.total_changes - before)


def bar_span(table: str, code: str) -> Optional[Tuple[str, str]]:
    """某只票在该表里的已有区间 `(首日, 末日)`；没有则 None。断点续跑靠它。"""
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        r = conn.execute(
            f"SELECT MIN(trade_date) AS a, MAX(trade_date) AS b FROM {T} "
            f"WHERE code=?", (code,)).fetchone()
    if r is None or r["a"] is None:
        return None
    return str(r["a"]), str(r["b"])


def bars_coverage(table: str) -> Dict[str, Any]:
    """整表覆盖概况 —— 给 plan() 用，**零联网**。"""
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        r = conn.execute(
            f"SELECT COUNT(*) AS rows, COUNT(DISTINCT trade_date) AS days, "
            f"COUNT(DISTINCT code) AS codes, MIN(trade_date) AS first_date, "
            f"MAX(trade_date) AS last_date FROM {T}").fetchone()
    out = dict(r)
    # 缺失写 None，不写 0：空表的 `first_date` 是「没数据」，与「19700101」
    # 在页面上是两回事（任务书硬要求）。
    if not out.get("rows"):
        out.update(first_date=None, last_date=None, days=0, codes=0)
    return out


def bars_dates(table: str, code: Optional[str] = None) -> List[str]:
    """已有的交易日（去重升序）。可按单只票过滤。"""
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        if code:
            rows = conn.execute(
                f"SELECT DISTINCT trade_date FROM {T} WHERE code=? "
                f"ORDER BY trade_date", (code,)).fetchall()
        else:
            rows = conn.execute(
                f"SELECT DISTINCT trade_date FROM {T} "
                f"ORDER BY trade_date").fetchall()
    return [r[0] for r in rows]


def bar_gaps(table: str, code: str, start: str, end: str,
             universe: Optional[Sequence[str]] = None) -> List[List[str]]:
    """该票在 ``[start, end]`` 里**缺哪几段**，``[[from, to], ...]``。零联网。

    「失败区间单独重试」的判据。`bar_span` 只给首末日 —— 中间断了一截它看不出
    来：区间看着盖住了，其实里面是空的（东财被拦那几天就长这样）。这里逐日
    比对，缺的日子按**连续段**归并：停牌连着缺三天就是一段，一次请求补一段，
    不是一天打一次。

    :param universe: 交易日全集。缺省用**表内已有**的 trade_date 并集 —— 别的
        票有、这票没有 = 缺口。调用方可传交易日历，让「整批都没落库的那天」
        也算缺口（表并集会把那天整个漏掉：那天谁都缺，自然不在并集里）。

    只看票自己 ``[首日, 末日]`` **之内**的洞：上市前、退市后没有数据是正常的，
    拿它们当缺口等于去追一段本来就不存在的历史。库里一天都没有的票回
    ``[[start, end]]``（整段都缺，等价于普通回填）。

    ⚠️ 停牌日会被当成缺口反复补：源对停牌日回空，下次还是会报缺。这是**显式**
    的「只补缺口」入口才有的代价，普通回填不走这里（它按首末日跳过）。
    """
    T = _bar_table(table)
    a, b = str(start), str(end)
    with _lock:
        conn = _connect()
        r = conn.execute(
            f"SELECT MIN(trade_date) AS lo, MAX(trade_date) AS hi FROM {T} "
            f"WHERE code=?", (code,)).fetchone()
        if r is None or r["lo"] is None:
            return [[a, b]]
        # 只在票自己的首末日之内找洞 —— 边界外的「缺」不是缺
        lo, hi = max(a, str(r["lo"])), min(b, str(r["hi"]))
        if lo > hi:
            return []
        have = {str(x[0]) for x in conn.execute(
            f"SELECT DISTINCT trade_date FROM {T} WHERE code=? "
            f"AND trade_date BETWEEN ? AND ?", (code, lo, hi)).fetchall()}
        if universe is None:
            # 同一把锁内取全集：`_lock` 是普通 Lock，嵌套取锁会死锁
            days = [str(x[0]) for x in conn.execute(
                f"SELECT DISTINCT trade_date FROM {T} WHERE trade_date "
                f"BETWEEN ? AND ? ORDER BY trade_date", (lo, hi)).fetchall()]
        else:
            days = sorted({str(d) for d in universe if lo <= str(d) <= hi})

    pos = {d: i for i, d in enumerate(days)}
    runs: List[List[str]] = []
    for d in days:
        if d in have:
            continue
        if runs and pos[d] == pos[runs[-1][1]] + 1:
            runs[-1][1] = d                 # 与上一段连着，并进去
        else:
            runs.append([d, d])
    return runs


def bars_fill_factors(table: str, code: str,
                      pairs: Sequence[Tuple[str, float]]) -> int:
    """把复权因子补进该票已有的日线行，返回**实际更新**的行数。

    只补 `adj_factor IS NULL` 的行 —— 与 `bars_upsert` 的「已有数据不覆盖」
    同一条口径：核对过的因子不该被一次重跑悄悄盖掉。

    为什么不能复用 `bars_upsert`：它是 `ON CONFLICT DO NOTHING`，对**已存在**
    的行压根不动 —— 拿它写因子等于一行都写不进去（阶段 4 的坑同族）。
    所以因子写入单开一条路，把意图留在函数名里。

    :param pairs: ``[(trade_date, 因子)]``，已由 `resolve()` 前向填充过
    """
    if not pairs:
        return 0
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        before = conn.total_changes
        # 批量必须 executemany（与 bars_upsert 同一条教训）。
        conn.executemany(
            f"UPDATE {T} SET adj_factor=? WHERE code=? AND trade_date=? "
            f"AND adj_factor IS NULL",
            [(float(f), code, d) for d, f in pairs])
        conn.commit()
        return int(conn.total_changes - before)


def bars_factor_coverage(table: str) -> Dict[str, Any]:
    """复权因子的落库进度，**零联网**。

    与 `bars_coverage` 刻意分开：那个回答「行情覆盖了几只几天」，这个回答
    「其中有多少行**判得清除权**」。合成一个的话，会出现「覆盖 100%、页面
    显示已完成」而收益其实全是 `UNADJUSTED_UNCERTAIN` —— 那正是最容易骗到人
    的一种「看起来没问题」。
    """
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        # `index_daily` 的 schema 里**没有** adj_factor 列（指数不做因子折算，
        # 见建表语句）。不查就拼 COUNT(adj_factor) 会直接 no such column ——
        # 这不是「数据缺了」，是「这张表压根没有这一列」，得单独说出来。
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({T})")}
        if "adj_factor" not in have:
            r = conn.execute(
                f"SELECT COUNT(*) AS rows, "
                f"COUNT(DISTINCT code) AS codes FROM {T}").fetchone()
            return {
                "rows": int(r["rows"] or 0),
                "with_factor": None,
                "without_factor": None,
                "codes": int(r["codes"] or 0),
                # None 而不是 1.0 —— 没有这一列不是「全部就绪」
                "rate": None,
                "supported": False,
                "note": f"{T} 表没有 adj_factor 列（按 schema 就没有），不谈因子覆盖率。",
            }
        r = conn.execute(
            f"SELECT COUNT(*) AS rows, COUNT(adj_factor) AS with_factor, "
            f"COUNT(DISTINCT code) AS codes FROM {T}").fetchone()
    rows = int(r["rows"] or 0)
    with_factor = int(r["with_factor"] or 0)
    return {
        "rows": rows,
        "with_factor": with_factor,
        # 判不了除权的行数。**写行数而不是百分比**：空表算 100% 会显示成
        # 「全部就绪」，而实际上一行都没有。
        "without_factor": rows - with_factor,
        "codes": int(r["codes"] or 0),
        "rate": (with_factor / rows) if rows else None,
        "supported": True,
    }


def bars_code_counts(table: str) -> Dict[str, int]:
    """每只票在该表有几行：`{code: 行数}`。**零联网。**

    一次 GROUP BY 走 `PRIMARY KEY (code, trade_date)` 的索引，不排序。给
    「回填跑到哪了 / 哪几只缺日子」用 —— 判完成靠行数而不是抽样：一张表的
    交易日是全表共有的集合，某票行数 == 交易日总数就等价于「每天都齐」，
    少一天就是 partial，一行都没有是 absent。
    """
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        rows = conn.execute(f"SELECT code, COUNT(*) FROM {T} "
                            f"GROUP BY code").fetchall()
    return {str(r[0]): int(r[1] or 0) for r in rows}


# ------------------------------------------------------------------- 键值对
#
# 只有零星几个「用户改过的开关」要跨重启存活（现在就一个：调度开不开）。
# 为它单开一张表比往 snapshot 里塞假数据集干净，也比在 SQLite 之外再找地方存
# （配置文件/环境变量）省事 —— 用户在页面上点的开关，本来就该存在库里。


def kv_get(key: str, default: Optional[str] = None) -> Optional[str]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO kv(key, value, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (key, value, now),
        )
        conn.commit()


# --------------------------------------------------------------- 关注列表
#
# 关注是**用户态**数据，不是按 (dataset, trade_date) 的行情快照，所以单开一张表。
# source_date 记下「关注当时是从哪一天的候选里选出来的」，关注页的走势就从这天起算。


def _decode_watch(row: sqlite3.Row) -> Dict[str, Any]:
    item = dict(row)
    raw = item.pop("snapshot", None)
    item["snapshot"] = json.loads(raw) if raw else {}
    return item


def watch_list() -> List[Dict[str, Any]]:
    """全部关注股，最近关注的排前面。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM watchlist ORDER BY added_at DESC, code"
        ).fetchall()
    return [_decode_watch(r) for r in rows]


def watch_get(code: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM watchlist WHERE code=?", (code,)).fetchone()
    return _decode_watch(row) if row else None


def watch_add(item: Dict[str, Any]) -> None:
    """关注。重复关注同一代码时刷新建仓日与打分快照（同一只股可能在
    不同的候选日被再次看中，走势起点应该跟着更新）。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO watchlist(code, market, name, industry, source_date,
                                     rank, score, price, snapshot, added_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(code) DO UPDATE SET
                 market=excluded.market, name=excluded.name,
                 industry=excluded.industry,
                 source_date=excluded.source_date, rank=excluded.rank,
                 score=excluded.score, price=excluded.price,
                 snapshot=excluded.snapshot""",
            (item["code"], item.get("market") or "cn",
             item.get("name") or item["code"],
             item.get("industry") or "", item["source_date"],
             item.get("rank"), item.get("score"), item.get("price"),
             json.dumps(item.get("snapshot") or {}, ensure_ascii=False),
             now),
        )
        conn.commit()
    logger.info("已关注 %s(%s) @ %s", item["code"],
                item.get("market") or "cn", item["source_date"])


def watch_remove(code: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM watchlist WHERE code=?", (code,))
        conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------ 基金关注列表
#
# 与股票关注**分表**：基金代码恒 6 位、与 A 股 6 位代码撞在一起但语义完全不同，
# 共用一张表会让 `/api/watchlist` 把基金当股票去查日线。这张表也刻意只存
# code + name + added_at —— 估值是每次查询现算的，没有「建仓日」可言。


def fund_watch_list() -> List[Dict[str, Any]]:
    """全部关注基金，最近关注的排前面。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM fund_watchlist ORDER BY added_at DESC, code"
        ).fetchall()
    return [dict(r) for r in rows]


def fund_watch_add(code: str, name: str = "") -> None:
    """关注基金。**不联网** —— 名称查不到也照样关注成功，等查询估值时再补。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO fund_watchlist(code, name, added_at) VALUES (?,?,?)
               ON CONFLICT(code) DO UPDATE SET name=excluded.name""",
            (code, (name or "").strip(), now),
        )
        conn.commit()


def fund_watch_remove(code: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM fund_watchlist WHERE code=?", (code,))
        conn.commit()
    return cur.rowcount > 0


def fund_watch_fill_name(code: str, name: str) -> None:
    """估值查到名称时回填。**只补空的**：用户手填的名字优先于接口给的。"""
    if not (name or "").strip():
        return
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE fund_watchlist SET name=? WHERE code=? AND (name='' OR name=code)",
            (name.strip(), code),
        )
        conn.commit()


# ------------------------------------------------- 订阅制（docs/06）
#
# 这一节只管读写三张表。「算不算有效订阅」的判定在 `app/subscribe.py` ——
# 过期是业务规则，不该散进 SQL 里。


def sub_get(open_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM subscriber WHERE open_id=?", (open_id,)).fetchone()
    return dict(row) if row else None


def sub_upsert(open_id: str, nickname: str = "") -> Dict[str, Any]:
    """首次见到这个 open_id 就建一行（`plan` 空 = 未订阅）。

    名字**只在非空时覆盖** —— 有人从没改过昵称时不能拿空串把已有名字抹掉。
    """
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO subscriber(open_id, nickname, created_at) VALUES (?,?,?)
               ON CONFLICT(open_id) DO UPDATE SET
                 nickname = CASE WHEN excluded.nickname <> ''
                                 THEN excluded.nickname
                                 ELSE subscriber.nickname END""",
            (open_id, (nickname or "").strip(), now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM subscriber WHERE open_id=?",
                           (open_id,)).fetchone()
    return dict(row) if row else {}


def sub_activate(open_id: str, amount: Optional[float] = None,
                 days: Optional[int] = None,
                 nickname: str = "") -> Dict[str, Any]:
    """开通 / 续期 30 天。

    **续期从原到期日往后累加，只有已过期才从现在起算** —— 提前续费的天数
    不该被吃掉。金额留空时用全局 `pro_price`（占位价，人工开通可覆盖）。
    """
    today = datetime.now()
    use_days = int(days or settings.pro_days)
    row = sub_upsert(open_id, nickname)

    cur = row.get("expires_at")
    cur_exp: Optional[datetime] = None
    if cur:
        try:
            cur_exp = datetime.fromisoformat(str(cur))
        except ValueError:
            cur_exp = None
    start = cur_exp if cur_exp and cur_exp > today else today
    expires = (start + timedelta(days=use_days)).isoformat(timespec="seconds")
    paid = today.isoformat(timespec="seconds")
    price = settings.pro_price if amount is None else float(amount)

    with _lock:
        conn = _connect()
        conn.execute(
            """UPDATE subscriber
                  SET plan='pro', paid_at=?, expires_at=?, amount=?
                WHERE open_id=?""",
            (paid, expires, price, open_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM subscriber WHERE open_id=?",
                           (open_id,)).fetchone()
    return dict(row) if row else {}


def sub_close(open_id: str) -> bool:
    """手动关闭订阅（订阅管理页）。关注**不清空**，下次开通还在。

    **`expires_at` 一并清成空串**：关闭 = 撤销订阅，剩余天数不留。留着的话
    `sub_activate` 那条「续期从原到期日往后累加」会把没用完的天数接上再加 30
    ——「关闭 → 重新开通」每循环一次就多 30 天。2026-10-14 用户实测这条链已经
    把到期日刷成了 +60（pro_days=30），`invite.activate` 里「连点两下不翻倍」
    的保护正是被这条绕开的。
    """
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE subscriber SET plan='', expires_at='' WHERE open_id=?",
            (open_id,))
        conn.commit()
    return cur.rowcount > 0


def sub_list() -> List[Dict[str, Any]]:
    """全部订阅者（订阅管理页）。过期判定交给调用方，这里只按时间倒序。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM subscriber ORDER BY created_at DESC, open_id"
        ).fetchall()
    return [dict(r) for r in rows]


def sub_set(open_id: str, *, amount: Optional[float] = None,
            fund_limit: Optional[int] = None,
            nickname: Optional[str] = None) -> bool:
    """页面编辑单项。`None` = 这一项不动。

    `fund_limit=0` = **清掉单独设置、回到全局默认**（写成 NULL）。SQLite 里
    没法用 `None` 同时表示「不动」和「清空」，所以约定 0/负数为清空。
    """
    sets, args = [], []
    if amount is not None:
        sets.append("amount=?")
        args.append(float(amount))
    if fund_limit is not None:
        sets.append("fund_limit=?")
        args.append(None if int(fund_limit) <= 0 else int(fund_limit))
    if nickname is not None:
        sets.append("nickname=?")
        args.append(nickname.strip())
    if not sets:
        return False
    args.append(open_id)
    with _lock:
        conn = _connect()
        cur = conn.execute(
            f"UPDATE subscriber SET {', '.join(sets)} WHERE open_id=?", args)
        conn.commit()
    return cur.rowcount > 0


# ---- 关注基金（按用户，与全局 fund_watchlist 无关） ------------------------


def sub_funds(open_id: str) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM subscriber_fund WHERE open_id=? ORDER BY added_at, code",
            (open_id,)).fetchall()
    return [dict(r) for r in rows]


def sub_fund_add(open_id: str, code: str, name: str = "") -> bool:
    """写入关注。返回 False = 本来就关注着（调用方据此回「已关注」）。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            """INSERT INTO subscriber_fund(open_id, code, name, added_at)
               VALUES (?,?,?,?)
               ON CONFLICT(open_id, code) DO NOTHING""",
            (open_id, code, (name or "").strip(), now),
        )
        conn.commit()
    return cur.rowcount > 0


def sub_fund_remove(open_id: str, code: str) -> bool:
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "DELETE FROM subscriber_fund WHERE open_id=? AND code=?",
            (open_id, code))
        conn.commit()
    return cur.rowcount > 0


def sub_fund_fill_name(code: str, name: str) -> None:
    """估值查到名称时回填进所有关注了它的人（只补空的，手填优先）。"""
    if not (name or "").strip():
        return
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE subscriber_fund SET name=? "
            "WHERE code=? AND (name='' OR name=code)",
            (name.strip(), code),
        )
        conn.commit()


# ---- 推送留痕 --------------------------------------------------------------


def push_log_add(open_id: str, kind: str, ok: bool, detail: str = "") -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO push_log(open_id, kind, sent_at, ok, detail) "
            "VALUES (?,?,?,?,?)",
            (open_id, kind, now, 1 if ok else 0, detail or ""),
        )
        conn.commit()


def push_log_tail(open_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM push_log WHERE open_id=? ORDER BY id DESC LIMIT ?",
            (open_id, limit)).fetchall()
    return [dict(r) for r in rows]


def push_log_summary() -> Dict[str, Dict[str, Any]]:
    """订阅管理页的「消息推送情况」：按人聚合 成功/失败次数 与 最近一次。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            """SELECT open_id,
                      SUM(ok)                       AS ok,
                      SUM(1 - ok)                   AS fail,
                      MAX(sent_at)                  AS last_at,
                      COUNT(*)                      AS total
                 FROM push_log GROUP BY open_id"""
        ).fetchall()
    return {r["open_id"]: dict(r) for r in rows}


def push_log_today(kind: str) -> int:
    """今天有没有**成功**推过（给启动时的漏推告警当判据）。"""
    day = datetime.now().strftime("%Y-%m-%d")
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT COUNT(*) FROM push_log WHERE kind=? AND ok=1 "
            "AND sent_at LIKE ?",
            (kind, f"{day}%"),
        ).fetchone()
    return int(row[0] or 0)


# ------------------------------------------------------------ 指令管理
#
# 只存**用户改过的**那几行。内置指令的默认值在 `app/cmdcfg.py`，两边以
# 「表里有没有这个 key」合并 —— 删行即恢复默认，没有第二种真相。


_CMD_KINDS = ("builtin", "custom")
#: 自定义指令只允许这两档。**不给 `any`**：自定义文案是管理员写的，
#: 群里任何人点一下就能触发等于把执行权开给所有人，而这段代码没有任何
#: 输入校验之外的防护。真要 any，把它做成内置指令并登记进 `_ACCESS`。
_CMD_ACCESS = ("sub", "owner")


def _cmd_row(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    for k in ("enabled", "quick", "sort"):
        out[k] = int(out.get(k) or 0)
    return out


def cmd_list() -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM cmd_item ORDER BY sort, key").fetchall()
    return [_cmd_row(r) for r in rows]


def cmd_get(key: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM cmd_item WHERE key=?",
                           ((key or "").strip(),)).fetchone()
    return _cmd_row(row) if row else None


def cmd_put(item: Dict[str, Any]) -> Dict[str, Any]:
    """新增或更新一条。**校验在这里做**，调用方（路由）不用各自把一遍关。"""
    key = str(item.get("key") or "").strip()
    if not key:
        raise ValueError("key 不能为空")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", key):
        raise ValueError("key 只能是字母/数字/下划线/中划线，最长 40")
    kind = str(item.get("kind") or "custom")
    if kind not in _CMD_KINDS:
        raise ValueError(f"kind 只能是 {'/'.join(_CMD_KINDS)}")
    access = str(item.get("access") or "owner")
    # 只卡**自定义**：内置指令的 access 是展示字段，真正的闸是 `feishu._ACCESS`
    # （`access_of` 对非 custom 恒回 None），库里写什么都放宽不了权限。
    if kind == "custom" and access not in _CMD_ACCESS:
        raise ValueError(f"access 只能是 {'/'.join(_CMD_ACCESS)}")
    alias = "、".join(x.strip() for x in
                      re.split(r"[,，]", str(item.get("alias") or ""))
                      if x.strip())
    vals = {
        "kind": kind,
        "label": str(item.get("label") or "")[:40],
        "text": str(item.get("text") or "")[:40],
        "alias": alias[:200],
        "access": access,
        "reply": str(item.get("reply") or "")[:500],
        "target": str(item.get("target") or "")[:80],
        "enabled": 1 if item.get("enabled", True) else 0,
        "quick": 1 if item.get("quick") else 0,
        "sort": int(item.get("sort") or 0),
    }
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO cmd_item(key,kind,label,text,alias,access,reply,
                                    target,enabled,quick,sort,updated_at)
               VALUES (:key,:kind,:label,:text,:alias,:access,:reply,
                       :target,:enabled,:quick,:sort,:updated_at)
               ON CONFLICT(key) DO UPDATE SET
                 kind=excluded.kind, label=excluded.label, text=excluded.text,
                 alias=excluded.alias, access=excluded.access, reply=excluded.reply,
                 target=excluded.target, enabled=excluded.enabled,
                 quick=excluded.quick, sort=excluded.sort,
                 updated_at=excluded.updated_at""",
            {"key": key, "updated_at": now, **vals})
        conn.commit()
    return {"key": key, "updated_at": now, **vals}


def cmd_remove(key: str) -> bool:
    """删行 = 这条指令回到代码里的默认值（内置）或彻底消失（自定义）。"""
    key = (key or "").strip()
    with _lock:
        conn = _connect()
        cur = conn.execute("DELETE FROM cmd_item WHERE key=?", (key,))
        conn.commit()
    return bool(cur.rowcount)


# ------------------------------------------------------------ 收银台（占位）
#
# 没有支付网关、没有回调 —— 这一节只回答两件事：这个 token 是谁的、
# 这张订单选了几个月。开通仍然由管理员在订阅管理页照订单点确认。


def _token_new(table: str, open_id: str, nickname: str = "",
               days: int = 30) -> Dict[str, Any]:
    """给这个人的链接发一个 token（`pay_token` 收银台 / `invite_token` 邀请）。

    token 是**随机串**而不是 open_id 本身：URL 会进浏览器历史、聊天转发与
    nginx 日志，裸 open_id 等于把身份标识到处撒，而且链接被转发后谁都能替他
    下单。随机串不可逆推，过期即作废。

    `table` 是**调用方写死的字面量**，不是外部输入 —— 拼进 SQL 只是为了两张
    表共用这一份逻辑。
    """
    now = datetime.now()
    token = secrets.token_urlsafe(18)
    row = {
        "token": token,
        "open_id": (open_id or "").strip(),
        "nickname": (nickname or "").strip(),
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=max(1, int(days)))).isoformat(
            timespec="seconds"),
    }
    with _lock:
        conn = _connect()
        conn.execute(
            f"INSERT INTO {table}(token, open_id, nickname, created_at, "
            "expires_at) VALUES (?,?,?,?,?)",
            (row["token"], row["open_id"], row["nickname"],
             row["created_at"], row["expires_at"]),
        )
        conn.commit()
    return row


def _token_get(table: str, token: str) -> Optional[Dict[str, Any]]:
    """按 token 取绑定的人。**过期一律当不存在** —— 宽放会让链接永久有效。"""
    token = (token or "").strip()
    if not token:
        return None
    with _lock:
        conn = _connect()
        row = conn.execute(f"SELECT * FROM {table} WHERE token=?",
                           (token,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        if datetime.fromisoformat(str(d["expires_at"])) <= datetime.now():
            return None
    except (TypeError, ValueError):
        return None
    return d


def pay_token_new(open_id: str, nickname: str = "", days: int = 30
                  ) -> Dict[str, Any]:
    """收银台链接的 token（逻辑见 `_token_new`）。"""
    return _token_new("pay_token", open_id, nickname, days)


def pay_token_get(token: str) -> Optional[Dict[str, Any]]:
    """收银台链接的 token → 这个人是谁（逻辑见 `_token_get`）。"""
    return _token_get("pay_token", token)


def invite_token_new(open_id: str, nickname: str = "", days: int = 30
                     ) -> Dict[str, Any]:
    """邀请链接的 token（逻辑见 `_token_new`）。"""
    return _token_new("invite_token", open_id, nickname, days)


def invite_token_get(token: str) -> Optional[Dict[str, Any]]:
    """邀请链接的 token → 这个人是谁（逻辑见 `_token_get`）。"""
    return _token_get("invite_token", token)


def pay_order_add(order_no: str, open_id: str, months: int, price: float,
                  channel: str, token: str = "", nickname: str = "",
                  note: str = "", amount: Optional[float] = None) -> Dict[str, Any]:
    """记一张待确认订单。金额在**下单那一刻**快照下来 —— 之后管理员改定价
    不该把历史订单的金额一起改掉。

    `amount` 显式给了就用它：**每档单独定价时 `price × months` 对不回来**
    （3 个月 ¥268、折合每月 89.3333，乘回去是 267.99）。没给才按 `price × months`
    算 —— 老调用方行为一字不变。
    """
    now = datetime.now().isoformat(timespec="seconds")
    row = {
        "order_no": order_no,
        "open_id": (open_id or "").strip(),
        "nickname": (nickname or "").strip(),
        "months": int(months),
        "price": float(price),
        "amount": (round(float(amount), 2) if amount is not None
                   else round(float(price) * int(months), 2)),
        "channel": channel,
        "status": "pending",
        "token": (token or "").strip(),
        "created_at": now,
        "confirmed_at": None,
        "note": (note or "").strip(),
    }
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO pay_order(order_no, open_id, nickname, months, "
            "price, amount, channel, status, token, created_at, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (row["order_no"], row["open_id"], row["nickname"], row["months"],
             row["price"], row["amount"], row["channel"], row["status"],
             row["token"], row["created_at"], row["note"]),
        )
        conn.commit()
    return row


def pay_order_get(order_no: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM pay_order WHERE order_no=?",
                           ((order_no or "").strip(),)).fetchone()
    return dict(row) if row else None


def pay_order_list(limit: int = 100) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM pay_order ORDER BY created_at DESC, order_no "
            "LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def pay_order_confirm(order_no: str, note: str = "") -> Optional[Dict[str, Any]]:
    """把订单标成已开通。**只有 pending 能确认** —— 重复点确认会把订阅
    期间翻倍地累加，这是真会发生的操作事故。"""
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE pay_order SET status='done', confirmed_at=?, "
            "note=CASE WHEN ? <> '' THEN ? ELSE note END "
            "WHERE order_no=? AND status='pending'",
            (now, (note or "").strip(), (note or "").strip(),
             (order_no or "").strip()),
        )
        conn.commit()
        if cur.rowcount <= 0:
            return None
        row = conn.execute("SELECT * FROM pay_order WHERE order_no=?",
                           ((order_no or "").strip(),)).fetchone()
    return dict(row) if row else None


# ------------------------------------------------- T+1 误差复盘：两条流水
#
# 复盘要回答的是「这个估算到底能不能信」，答案只能来自真实发生过的数据，所以
# 这一节只做最朴素的两件事：把当天的估算记下来、把接口带回来的真实净值记下来。
# 对齐与统计放在 `metrics/fund_review.py`（纯函数，离线好断言）。
#
# **真实净值为什么也要单独存一份**：估值源的 `NAV` 是「最新一次公布的单位
# 净值」，它只带**一个**净值日期（`PDATE`）。算某天的实际涨跌幅要前后两个净值，
# 所以每次查询都要把看到的存下来，攒到有一天能对上为止 —— 这也是它叫 T+1 的
# 原因：第二天再点一次查询，前一天的样本才闭合。**换源没有改变这一点**（旧源
# 的 `dwjz`/`jzrq` 同样只有一个日期），一次查询仍然只带回一个净值日期。


def fund_estimate_put(rows: Iterable[Dict[str, Any]]) -> int:
    """按 (code, date) upsert 当天的估算，返回写入条数。

    **同日重查覆盖旧值**：越靠近收盘的估值越准，留着早盘那一版只会让复盘
    多出一个「早盘必然高估」的系统性偏差 —— 那是采样时机的问题，不是估算
    算错了。
    """
    now = datetime.now().isoformat(timespec="seconds")
    done = 0
    with _lock:
        conn = _connect()
        for r in rows:
            code = str(r.get("code") or "").strip()
            date = str(r.get("date") or "").strip()
            if not code or not date:
                continue
            conn.execute(
                """INSERT INTO fund_estimate_log
                       (code, date, est_pct, est_nav, source, coverage,
                        gz_time, logged_at)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(code, date) DO UPDATE SET
                       est_pct=excluded.est_pct, est_nav=excluded.est_nav,
                       source=excluded.source, coverage=excluded.coverage,
                       gz_time=excluded.gz_time, logged_at=excluded.logged_at""",
                (code, date, r.get("est_pct"), r.get("est_nav"),
                 str(r.get("source") or ""), r.get("coverage"),
                 r.get("gz_time"), now),
            )
            done += 1
        conn.commit()
    return done


def fund_nav_put(code: str, nav_date: str, nav: Any) -> None:
    """存一条真实净值。**`nav <= 0` 直接丢** —— 0 不是净值，存进去会让那天的
    实际涨跌幅算成 -100%，比缺一个样本坏得多。bool 同理拒收（`float(True)`
    会变成 1.000 这个看起来完全合法的净值）。"""
    code = (code or "").strip()
    nav_date = (nav_date or "").strip()
    if isinstance(nav, bool):
        return
    try:
        value = float(nav)
    except (TypeError, ValueError):
        return
    if not code or not nav_date or value <= 0:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO fund_nav_log(code, nav_date, nav, logged_at)
               VALUES (?,?,?,?)
               ON CONFLICT(code, nav_date) DO UPDATE SET
                   nav=excluded.nav, logged_at=excluded.logged_at""",
            (code, nav_date, value, now),
        )
        conn.commit()


# 估算归到哪一天：只认 `as_of` 开头的 YYYY-MM-DD。
_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def fund_nav_put_many(pairs: Iterable[Any]) -> int:
    """批量存净值历史，返回写入条数。入参元素 `(code, nav_date, nav)` 或 dict。

    与 `fund_nav_put` **同一套校验**（`<=0` 与 bool 一律拒收）—— 批量不是
    放宽口径的借口，一条假净值会让那天的实际涨跌幅算成 -100%。

    用一次事务写完：净值历史一次就是几十条，逐条 commit 会把「查询估值」
    拖慢好几倍，而这些行彼此独立、没有中途读的必要。
    """
    rows: List[Tuple[str, str, float]] = []
    for item in pairs:
        if isinstance(item, dict):
            code = str(item.get("code") or "").strip()
            nav_date = str(item.get("date") or item.get("nav_date") or "").strip()
            nav = item.get("nav")
        else:
            try:
                code, nav_date, nav = item
            except (TypeError, ValueError):
                continue
            code = str(code or "").strip()
            nav_date = str(nav_date or "").strip()
        if isinstance(nav, bool):
            continue
        try:
            value = float(nav)
        except (TypeError, ValueError):
            continue
        if not code or not nav_date or value != value or value <= 0:
            continue
        rows.append((code, nav_date, value))
    if not rows:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        conn.executemany(
            """INSERT INTO fund_nav_log(code, nav_date, nav, logged_at)
               VALUES (?,?,?,?)
               ON CONFLICT(code, nav_date) DO UPDATE SET
                   nav=excluded.nav, logged_at=excluded.logged_at""",
            [(c, d, v, now) for c, d, v in rows],
        )
        conn.commit()
    return len(rows)


def estimate_date(row: Dict[str, Any]) -> str:
    """这条估算归到哪一天。

    官方估值链的 `as_of` 是源给的 `2026-10-10 14:30`，直接取日期段；持仓穿透
    只有 `HH:MM:SS`（行情时间戳不带日期），退到**本机今天**。认不出日期也用
    今天 —— 归错日期会让复盘把 A 天的估算对到 B 天的净值上，比少一条样本坏
    得多；靠今天归档顶多落进 pending（当天净值对不上），不会算出错的误差。
    """
    m = _DATE_RE.match(str(row.get("as_of") or ""))
    return m.group(1) if m else datetime.now().strftime("%Y-%m-%d")


def record_fund_logs(rows: Iterable[Dict[str, Any]]) -> None:
    """把本次查询的估算与真实净值写进 T+1 复盘流水（本地，零网络）。

    **放在这里而不是 `api/routes.py`**：14:50 的订阅推送（`app/subscribe.py`）
    要写同一份流水，而 `routes` 依赖 `subscribe` —— 落库编排留在存储层才不会
    反过来把 `subscribe` 拉去 import `routes` 造成循环。
    """
    estimates = []
    for r in rows:
        if r.get("estimate_pct") is None:
            continue
        estimates.append({
            "code": r["code"],
            "date": estimate_date(r),
            "est_pct": r.get("estimate_pct"),
            "est_nav": r.get("estimate_nav"),
            "source": r.get("source") or "",
            "coverage": r.get("coverage"),
            "gz_time": r.get("as_of"),
        })
    if estimates:
        fund_estimate_put(estimates)
    for r in rows:
        # 真实净值只有官方估值链带得回来（持仓穿透压根不取它），所以穿透链
        # 单独跑时不会贡献任何净值 —— 复盘样本也就攒不起来，这是已知代价。
        if r.get("nav") and r.get("nav_date"):
            fund_nav_put(r["code"], r["nav_date"], r["nav"])


def fund_estimates() -> List[Dict[str, Any]]:
    """全部估算流水，按 (code, date) 排序。给复盘用，不分页。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT code, date, est_pct, est_nav, source, coverage, gz_time "
            "FROM fund_estimate_log ORDER BY code, date"
        ).fetchall()
    return [dict(r) for r in rows]


def fund_navs() -> List[Dict[str, Any]]:
    """全部真实净值流水，按 (code, nav_date) 排序。给复盘用，不分页。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT code, nav_date, nav FROM fund_nav_log ORDER BY code, nav_date"
        ).fetchall()
    return [dict(r) for r in rows]


def fund_last_estimates() -> List[Dict[str, Any]]:
    """关注列表里每只基金**最近一次**查到的估算，附上当时可见的最新净值。

    只读、零网络 —— 给 `/api/fund-estimate/last` 用：流水本来每次查询都在写
    （为了 T+1 复盘），只是没人回读过，所以页面每次打开都得重新查一遍。
    三条口径都是为了「回读出来的一行要和实时查出来的一行长一样，但不冒充实时」：

    * **只回关注列表里的 code** —— 取消关注的基金留在流水里（复盘还要用），
      但不该出现在表格里。
    * **每个 code 只取 `MAX(date)` 那条** —— 回读要的是「上次查到的」，不是
      整段历史。
    * **净值取 `nav_date <= 估算日` 的最近一条**，不是「迄今最新」。同一次查询
      同时写入的 (估算, 净值) 本来就是这个关系；真取最新的会把 10-10 的净值
      拼到 10-09 的估算上，两头都对不上。取不到就留空，由前端显示「—」，
      **不补默认值**。
    """
    with _lock:
        conn = _connect()
        est_rows = conn.execute(
            """SELECT e.code, e.date, e.est_pct, e.est_nav, e.source,
                      e.coverage, e.gz_time, e.logged_at
                 FROM fund_estimate_log e
                 JOIN (SELECT code, MAX("date") AS max_date
                         FROM fund_estimate_log GROUP BY code) m
                   ON m.code = e.code AND m.max_date = e."date"
                WHERE e.code IN (SELECT code FROM fund_watchlist)
                ORDER BY e.code"""
        ).fetchall()
        codes = [r["code"] for r in est_rows]
        nav_rows: List[Any] = []
        if codes:
            # 只取这些 code 的净值 —— 流水会跨日累积，整表拉回来是白读
            ph = ",".join("?" * len(codes))
            nav_rows = conn.execute(
                f"SELECT code, nav_date, nav FROM fund_nav_log "
                f"WHERE code IN ({ph}) ORDER BY code, nav_date",
                codes,
            ).fetchall()

    by_code: Dict[str, List[Any]] = {}
    for r in nav_rows:
        by_code.setdefault(r["code"], []).append((r["nav_date"], r["nav"]))

    out: List[Dict[str, Any]] = []
    for r in est_rows:
        nav: Optional[float] = None
        nav_date: Optional[str] = None
        # 流水按 nav_date 升序存，倒着找第一条 <= 估算日的。日期是 ISO 字符串，
        # 字符串比较即日期比较。
        for d, v in reversed(by_code.get(r["code"], [])):
            if d <= r["date"]:
                nav, nav_date = v, d
                break
        item = dict(r)
        item["nav"] = nav
        item["nav_date"] = nav_date
        out.append(item)
    return out


# --------------------------------------------------- 研究模块（docs/backtest）
#
# 五张 research_* 表的读写。全部走 db 的**同一把锁、同一条连接** —— 本项目
# 没开 WAL（见 docs/04 §11.3），另起一条连接写同一个文件，并发时会直接
# `database is locked`。因此这一节每个函数只取一次 _lock，内部不再调用
# 其它带锁函数（_lock 不可重入，嵌套会死锁）。


# 股票池模式（任务书里的「模式 A / 模式 B」）：
#   A = current      当前股票池回看
#   B = historical   历史股票池（含退市/停牌/ST 的历史成员）
# B **当前不可用**，在入口显式拒绝 —— 理由见 UNIVERSE_HISTORICAL_BLOCK_REASON。
UNIVERSE_MODE_CURRENT = "current"
UNIVERSE_MODE_HISTORICAL = "historical"
UNIVERSE_MODES = (UNIVERSE_MODE_CURRENT, UNIVERSE_MODE_HISTORICAL)

UNIVERSE_HISTORICAL_BLOCK_REASON = (
    "股票池模式 B（historical）当前不可用：库里没有退市/停牌/ST 的历史状态，"
    "也没有每日股票池快照（audit §5/§10 结论 BLOCKED）。"
    "若按模式 A 跑再标成 B，已退市证券根本不会出现在任何一天的快照里，"
    "长期收益会被系统性高估 —— 那是把幸存者偏差伪装成结论。"
    "要启用它，需先积累 universe 快照，并由业务方授权落库历史成员。")


def research_experiment_create(
    name: str,
    strategy_version: str,
    config_snapshot: Dict[str, Any],
    universe_mode: str,
    start_date: str,
    end_date: str,
    data_snapshot_id: str = "",
) -> Tuple[int, str]:
    """建实验并**冻结**配置，返回 (id, config_hash)。

    冻结的意义：实验跑到一半有人改了 scorer.yaml，那次运行仍必须按建实验时
    的权重算，否则同一批结果会跨两套口径 —— 这就是任务书「配置一旦开始运行
    不得被后续修改覆盖」的落点。config_hash 用 sha256(规范化 JSON)，两次实验
    的 hash 相同即可断言参数完全一致。

    股票池模式 B（历史股票池）**在此显式拒绝**：库里没有退市/停牌/ST 的
    历史状态，也没有每日股票池快照。若放任它按模式 A 跑，结果会被悄悄标成
    "历史股票池回测"，而实际上已退市证券根本不会出现 —— 那是把幸存者偏差
    伪装成结论。宁可在入口挡住，也不让它产出好看但错的数。
    """
    if universe_mode not in UNIVERSE_MODES:
        raise ValueError("未知的股票池模式：%r，可选 %s"
                         % (universe_mode, list(UNIVERSE_MODES)))
    if universe_mode == UNIVERSE_MODE_HISTORICAL:
        raise ValueError(UNIVERSE_HISTORICAL_BLOCK_REASON)
    blob = json.dumps(config_snapshot, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT INTO research_experiments("
            " name, strategy_version, config_snapshot_json, config_hash,"
            " data_snapshot_id, universe_mode, start_date, end_date,"
            " status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?, 'created', ?)",
            (name, strategy_version, blob, digest, data_snapshot_id,
             universe_mode, start_date, end_date, now),
        )
        conn.commit()
        return int(cur.lastrowid), digest


def research_experiment_list() -> List[Dict[str, Any]]:
    """实验列表，新的在前。**不带** config_snapshot（列表页用不上，
    每条都解一遍 JSON 是白扔的时间）。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT id, name, strategy_version, config_hash, data_snapshot_id,"
            "       universe_mode, start_date, end_date, status, created_at,"
            "       started_at, finished_at, error_summary"
            " FROM research_experiments ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def research_experiment_get(exp_id: int) -> Optional[Dict[str, Any]]:
    """单个实验详情，含解出来的 config_snapshot（复现靠它）。"""
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM research_experiments WHERE id=?",
                           (exp_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["config_snapshot"] = json.loads(out.pop("config_snapshot_json"))
    except (TypeError, ValueError):
        out["config_snapshot"] = None
        out.pop("config_snapshot_json", None)
    return out


def research_experiment_config(exp_id: int) -> Optional[Tuple[str, Dict[str, Any]]]:
    """取冻结的 (config_hash, config_snapshot)；实验不存在返回 None。

    回测运行期间**只准读这里**，不准回头读 scorer.yaml。
    """
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT config_hash, config_snapshot_json FROM research_experiments"
            " WHERE id=?", (exp_id,)).fetchone()
    if row is None:
        return None
    try:
        snap = json.loads(row["config_snapshot_json"])
    except (TypeError, ValueError):
        snap = None
    return row["config_hash"], snap


def research_experiment_set_state(
    exp_id: int,
    status: Optional[str] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    error_summary: Optional[str] = None,
) -> None:
    """只更新给到的字段；一个都没给就是空操作。"""
    sets: List[str] = []
    args: List[Any] = []
    for col, val in (("status", status), ("started_at", started_at),
                     ("finished_at", finished_at), ("error_summary", error_summary)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if not sets:
        return
    args.append(exp_id)
    with _lock:
        conn = _connect()
        conn.execute("UPDATE research_experiments SET %s WHERE id=?"
                     % ", ".join(sets), tuple(args))
        conn.commit()


def research_experiment_set_data_snapshot(exp_id: int, value: str) -> None:
    """记录本次实际用到的数据版本（哪个数据集、哪段区间、最晚抓取时刻）。

    `main_force` 以 (dataset, trade_date) 为唯一键，所以"信号日"本身就是
    数据版本的主键；这里再落一份带抓取时刻的摘要，是为了让**不查库的人**
    也能一眼看出这次实验用的是哪批快照、是不是事后回补的。
    """
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE research_experiments SET data_snapshot_id=? WHERE id=?",
            (value, exp_id))
        conn.commit()


def research_experiment_delete(exp_id: int) -> Dict[str, int]:
    """删实验，**连同**运行/信号/结果/指标一次事务删干净，返回逐表计数。

    关联表的 REFERENCES 是摆设（SQLite 默认不开外键，见建表处注释），级联
    只能自己来，且必须先子后父 —— 中间任何一步失败就整体回滚，不留半删
    状态。逐表计数如实回报，调用方（前端提示、回归断言）能核对「删了几行」，
    而不是只收一个 OK。

    有 pending/running 的运行**先拒**：后台引擎还在往这些表里写，删掉的行
    会被它写回来，留下一批查不到父行的孤儿数据。
    """
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM research_experiments WHERE id=?",
                        (exp_id,)).fetchone() is None:
            raise ValueError(f"实验不存在：{exp_id}")
        if conn.execute(
                "SELECT 1 FROM research_runs WHERE experiment_id=?"
                " AND status IN ('pending','running') LIMIT 1",
                (exp_id,)).fetchone() is not None:
            raise ValueError(f"实验尚有未完成的运行，请先取消再删除：{exp_id}")
        counts: Dict[str, int] = {}
        try:
            for table, key in (("research_signals", "signals"),
                               ("research_outcomes", "outcomes"),
                               ("research_metrics", "metrics"),
                               ("research_runs", "runs")):
                cur = conn.execute(
                    "DELETE FROM %s WHERE experiment_id=?" % table, (exp_id,))
                counts[key] = int(cur.rowcount)
            cur = conn.execute("DELETE FROM research_experiments WHERE id=?",
                               (exp_id,))
            counts["experiment"] = int(cur.rowcount)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return counts


def research_run_create(experiment_id: int) -> int:
    """开一次运行，返回 run id。

    三件事一次做完（同一把锁内）：校验实验存在、**拒绝并发重入**、run_number
    自增。重入保护是任务书「重复启动相同实验时应防止重复执行」的落点 ——
    两次点「运行」不会变成两条并行任务各写一份信号。
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM research_experiments WHERE id=?",
                        (experiment_id,)).fetchone() is None:
            raise ValueError(f"实验不存在：{experiment_id}")
        busy = conn.execute(
            "SELECT run_number FROM research_runs WHERE experiment_id=?"
            " AND status IN ('pending','running') LIMIT 1",
            (experiment_id,)).fetchone()
        if busy is not None:
            raise ValueError(
                f"该实验已有未完成的运行（run #{busy['run_number']}），"
                "等它结束或先取消，避免重复生成信号")
        nrow = conn.execute(
            "SELECT COALESCE(MAX(run_number),0)+1 AS n FROM research_runs"
            " WHERE experiment_id=?", (experiment_id,)).fetchone()
        cur = conn.execute(
            "INSERT INTO research_runs(experiment_id, run_number, status,"
            " progress, stage, started_at) VALUES (?,?, 'running', 0, ?, ?)",
            (experiment_id, int(nrow["n"]), "初始化", now))
        conn.execute(
            "UPDATE research_experiments SET status='running',"
            " started_at=COALESCE(started_at,?) WHERE id=?",
            (now, experiment_id))
        conn.commit()
        return int(cur.lastrowid)


def research_run_get(run_id: int) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM research_runs WHERE id=?",
                           (run_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    try:
        out["warnings"] = (json.loads(out["warnings_json"])
                           if out.get("warnings_json") else [])
    except (TypeError, ValueError):
        out["warnings"] = []
    out.pop("warnings_json", None)
    return out


def research_run_update(
    run_id: int,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    stage: Optional[str] = None,
    error_summary: Optional[str] = None,
    warnings: Optional[Sequence[Any]] = None,
    finished: bool = False,
) -> None:
    """更新运行状态。finished=True 时顺带写 finished_at。"""
    sets: List[str] = []
    args: List[Any] = []
    for col, val in (("status", status), ("progress", progress),
                     ("stage", stage), ("error_summary", error_summary)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if warnings is not None:
        sets.append("warnings_json=?")
        args.append(json.dumps(list(warnings), ensure_ascii=False))
    if finished:
        sets.append("finished_at=?")
        args.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if not sets:
        return
    args.append(run_id)
    with _lock:
        conn = _connect()
        conn.execute("UPDATE research_runs SET %s WHERE id=?"
                     % ", ".join(sets), tuple(args))
        conn.commit()


def research_run_cancel(run_id: int) -> bool:
    """取消**尚未完成**的运行。已完成的返回 False（不可取消）。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE research_runs SET status='cancelled', finished_at=?"
            " WHERE id=? AND status IN ('pending','running')",
            (now, run_id))
        conn.commit()
        return cur.rowcount > 0


def research_runs_of(experiment_id: int) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM research_runs WHERE experiment_id=?"
            " ORDER BY run_number DESC", (experiment_id,)).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------- 信号/结果
#
# 三张明细表统一用「按唯一键 upsert」的方式写入：同一实验重复运行时覆盖
# 上一次的行，而不是追加 —— 唯一约束在表上，重复执行由它兜底。


def research_signals_replace(experiment_id: int, run_id: Optional[int],
                             rows: Sequence[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM research_experiments WHERE id=?",
                        (experiment_id,)).fetchone() is None:
            raise ValueError(f"实验不存在：{experiment_id}")
        n = 0
        for r in rows:
            factors = r.get("factors")
            missing = r.get("missing")
            conn.execute(
                "INSERT INTO research_signals("
                " experiment_id, run_id, signal_date, code, model_version,"
                " score, factors_json, rank, top5, top10, top30,"
                " data_quality, missing_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(experiment_id, signal_date, code, model_version)"
                " DO UPDATE SET run_id=excluded.run_id, score=excluded.score,"
                " factors_json=excluded.factors_json, rank=excluded.rank,"
                " top5=excluded.top5, top10=excluded.top10,"
                " top30=excluded.top30, data_quality=excluded.data_quality,"
                " missing_json=excluded.missing_json",
                (experiment_id, run_id, r["signal_date"], r["code"],
                 r["model_version"], r.get("score"),
                 json.dumps(factors, ensure_ascii=False), r.get("rank"),
                 1 if r.get("top5") else 0, 1 if r.get("top10") else 0,
                 1 if r.get("top30") else 0,
                 r.get("data_quality") or "READY",
                 json.dumps(missing, ensure_ascii=False) if missing else None))
            n += 1
        conn.commit()
        return n


def _decode_signal(row: sqlite3.Row) -> Dict[str, Any]:
    out = dict(row)
    for src, dst in (("factors_json", "factors"), ("missing_json", "missing")):
        raw = out.pop(src, None)
        if raw:
            try:
                out[dst] = json.loads(raw)
            except (TypeError, ValueError):
                out[dst] = None
        else:
            out[dst] = None
    return out


def research_signals_of(experiment_id: int, limit: int = 1000,
                        offset: int = 0) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM research_signals WHERE experiment_id=?"
            " ORDER BY signal_date, rank LIMIT ? OFFSET ?",
            (experiment_id, limit, offset)).fetchall()
    return [_decode_signal(r) for r in rows]


def research_outcomes_replace(experiment_id: int,
                              rows: Sequence[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    cols = ("signal_date", "code", "horizon", "status", "theory_return",
            "bench_return", "excess_return", "entry_date", "entry_price",
            "exit_date", "exit_price", "fill_status", "sell_status",
            "exec_return", "mfe", "mae",
            # 复权口径四列（阶段 5）。写入方没给就落 None —— 用 `r.get(c)`
            # 而不是下标，老调用方不必一次性改完。
            "adjust_method", "adjust_note", "entry_factor", "exit_factor",
            "data_quality", "missing_reason")
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM research_experiments WHERE id=?",
                        (experiment_id,)).fetchone() is None:
            raise ValueError(f"实验不存在：{experiment_id}")
        n = 0
        for r in rows:
            conn.execute(
                "INSERT INTO research_outcomes(experiment_id, %s)"
                " VALUES (%s)"
                " ON CONFLICT(experiment_id, signal_date, code, horizon)"
                " DO UPDATE SET %s" % (
                    ", ".join(cols),
                    ", ".join("?" for _ in range(len(cols) + 1)),
                    ", ".join(f"{c}=excluded.{c}" for c in cols)),
                (experiment_id, *[r.get(c) for c in cols]))
            n += 1
        conn.commit()
        return n


def research_outcomes_of(experiment_id: int,
                         horizon: Optional[int] = None,
                         limit: int = 5000) -> List[Dict[str, Any]]:
    """按实验回读结果。

    `horizon` / `limit` 是给 API 用的：结果行数 = 信号数 × 观察期数，
    一次全量拉出来会把整条链拖慢（实测读 243 KB payload 只有 214 ops/s，
    而按字段取有 870）。**调用方自己判断是否被截断** —— 这里不静默丢数据。
    """
    sql = "SELECT * FROM research_outcomes WHERE experiment_id=?"
    args: List[Any] = [experiment_id]
    if horizon is not None:
        sql += " AND horizon=?"
        args.append(int(horizon))
    sql += " ORDER BY signal_date, code, horizon LIMIT ?"
    args.append(int(limit))
    with _lock:
        conn = _connect()
        rows = conn.execute(sql, tuple(args)).fetchall()
    return [dict(r) for r in rows]


def research_metrics_replace(experiment_id: int,
                             rows: Sequence[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    cols = ("portfolio_rule", "horizon", "valid_signals", "valid_fills",
            "avg_return", "median_return", "win_rate", "avg_excess",
            "median_excess", "p10", "p25", "p75", "p90", "max_drawdown",
            "trading_cost", "unfilled_rate", "pending_count", "no_data_count")
    with _lock:
        conn = _connect()
        if conn.execute("SELECT 1 FROM research_experiments WHERE id=?",
                        (experiment_id,)).fetchone() is None:
            raise ValueError(f"实验不存在：{experiment_id}")
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        n = 0
        for r in rows:
            notes = r.get("quality_notes")
            conn.execute(
                "INSERT INTO research_metrics(experiment_id, %s, quality_json,"
                " computed_at) VALUES (%s, ?, ?)"
                " ON CONFLICT(experiment_id, portfolio_rule, horizon)"
                " DO UPDATE SET %s, quality_json=excluded.quality_json,"
                " computed_at=excluded.computed_at" % (
                    ", ".join(cols),
                    ", ".join("?" for _ in range(len(cols) + 1)),
                    ", ".join(f"{c}=excluded.{c}" for c in cols)),
                (experiment_id, *[r.get(c) for c in cols],
                 json.dumps(notes, ensure_ascii=False) if notes else None, now))
            n += 1
        conn.commit()
        return n


def research_metrics_of(experiment_id: int) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM research_metrics WHERE experiment_id=?"
            " ORDER BY portfolio_rule, horizon", (experiment_id,)).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw = item.pop("quality_json", None)
        if raw:
            try:
                item["quality_notes"] = json.loads(raw)
            except (TypeError, ValueError):
                item["quality_notes"] = []
        else:
            item["quality_notes"] = []
        out.append(item)
    return out


# ================================================== 数据管理：画像与安全删除
#
# 触发背景：数据每天落一批，但没有任何地方能看清「占了多少、能不能删」。
# 这一节只做两件事：只读画像，以及**带硬下限保护**的删除。默认 dry_run——
# 不确认绝不写库。


# 各 dataset 的**硬下限**：删到比这更少会直接破坏功能，后端拒绝。
#
# 注意这里填的是「**删完之后至少还要剩几天**」，不是「最近 N 天不许碰」。
# 界线划得很保守，只放真会坏的：
#   - stock_net_inflow=5：维度 1（近 5 日净流入/流通市值）与维度 3 要跨日序列，
#     不足 5 天该子信号变 NaN、模型走 degraded —— 这是真·硬需求。
#   - sector_flow_*：只存 trade_date="latest" 一行，删掉板块页直接空。
#   - 其余 =1：少于 1 天意味着该 dataset 空了，页面/矩阵会变成空白。
#
# `main_force` 的 60、`pool_*` 的 30 只是 `list_dates(limit)` 的**显示上限** ——
# 库里只有 5 天时照常显示 5 天，不会崩。把它们当硬下限会锁死用户
# （实测：5 天全被拦，想删哪天都删不掉），所以不进这里，由 retention 管策略。
DATASET_FLOOR: Dict[str, int] = {
    "stock_net_inflow": 5,
    "main_force": 1,
    "pool_zt": 1,
    "pool_zb": 1,
    "pool_dt": 1,
    "pool_strong": 1,
    # 99999 表示「整类不可删」，purge_snapshot 直接返回 error
    "sector_flow_industry": 99999,
    "sector_flow_concept": 99999,
}

# 这些表存的是用户配置与凭据，不参与任何清理
_PROTECTED_TABLES = ("kv", "watchlist")

_TABLES = ("snapshot", "job_run", "watchlist", "kv")


def _size_of(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def usage() -> Dict[str, Any]:
    """只读画像：库文件、各表、按 dataset 分组的占用 + 保留策略。

    零网络、零写入（mode 仍是同一个连接，但只跑 SELECT）。
    `kv` 只给 key 不给 value —— 那里面有飞书密钥与 webhook 签名密钥。
    """
    with _lock:
        conn = _connect()
        db_path = Path(settings.db_path)
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist = conn.execute("PRAGMA freelist_count").fetchone()[0]

        tables = []
        for name in _TABLES:
            rows = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            tables.append({"name": name, "rows": rows})

        datasets = []
        for r in conn.execute(
            "SELECT dataset, COUNT(*) AS dates, COALESCE(SUM(row_count),0) AS entries,"
            "       COALESCE(SUM(LENGTH(CAST(payload AS BLOB))),0) AS bytes,"
            "       MIN(trade_date) AS date_min, MAX(trade_date) AS date_max,"
            "       MAX(fetched_at) AS fetched_at"
            "  FROM snapshot GROUP BY dataset ORDER BY bytes DESC"
        ):
            rec = dict(r)
            rec["floor_days"] = DATASET_FLOOR.get(rec["dataset"], 0)
            rec["undelatable"] = DATASET_FLOOR.get(rec["dataset"], 0) >= 99999
            datasets.append(rec)

        job = dict(conn.execute(
            "SELECT COUNT(*) AS rows,"
            "       COALESCE(SUM(CASE WHEN finished_at IS NULL THEN 1 ELSE 0 END),0)"
            "         AS unfinished,"
            "       MIN(started_at) AS oldest, MAX(started_at) AS newest"
            "  FROM job_run"
        ).fetchone() or {})
        job_bytes = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(CAST(detail AS BLOB))),0) FROM job_run"
        ).fetchone()[0]
        job["bytes"] = int(job_bytes or 0)

        kv_keys = [r[0] for r in conn.execute("SELECT key FROM kv ORDER BY key")]
        watch_rows = conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0]

    files = []
    for label, p in (
        ("market.db", Path(settings.db_path)),
        ("trade_calendar.json", Path(settings.db_path).parent / "trade_calendar.json"),
    ):
        files.append({"name": label, "path": str(p),
                      "bytes": _size_of(p), "kind": "data"})
    for label, p in (("backend.log", settings.run_dir / "backend.log"),
                     ("frontend.log", settings.run_dir / "frontend.log"),
                     ("start.log", settings.run_dir / "start.log")):
        files.append({"name": label, "path": str(p),
                      "bytes": _size_of(p), "kind": "log"})

    auto_vacuum = 0
    with _lock:
        try:
            auto_vacuum = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
        except sqlite3.Error:  # pragma: no cover - 老版本没有该 pragma
            auto_vacuum = 0

    return {
        "db": {
            "path": str(settings.db_path),
            "bytes": _size_of(Path(settings.db_path)),
            "page_size": page_size,
            "page_count": page_count,
            "freelist_pages": freelist,
            # auto_vacuum=0：删完不会自动缩，必须 VACUUM 才真回收
            "reclaimable_bytes": freelist * page_size,
            "auto_vacuum": bool(auto_vacuum),
        },
        "tables": tables,
        "datasets": datasets,
        "job_run": job,
        "files": files,
        "kv_keys": kv_keys,           # 只给键名，值绝不外传
        "watchlist_rows": watch_rows,
        "retention": {
            "main_force_days": settings.retention_main_force_days,
            "inflow_days": settings.retention_inflow_days,
            "pool_days": settings.retention_pool_days,
            "job_run_keep": settings.job_run_keep,
        },
        "floor": DATASET_FLOOR,
        "protected_tables": list(_PROTECTED_TABLES),
    }


def purge_snapshot(dataset: Optional[str] = None, date: Optional[str] = None,
                   before: Optional[str] = None,
                   dry_run: bool = True) -> Dict[str, Any]:
    """删 snapshot 行。**dry_run 默认 True —— 不确认绝不写库。**

    规则：
      - 必须指定 dataset（不接受「全删」这种模糊目标）
      - `date` 删某一天，`before` 删该日期**之前**的所有天（二选一）
      - 低于 `DATASET_FLOOR` 的日期一律拒绝，理由随返回带出
      - `sector_flow_*` 整类不可删（只有 latest 一行）

    返回结构里 `blocked` 说明「想删但被拦下」的原因，前端据此给出可解释的提示。
    """
    if not dataset:
        return {"error": "必须指定 dataset", "dry_run": dry_run, "applied": False}
    if bool(date) == bool(before):
        return {"error": "date 与 before 必须且只能给一个",
                "dry_run": dry_run, "applied": False}

    floor = DATASET_FLOOR.get(dataset, 0)
    if floor >= 99999:
        return {"error": f"{dataset} 只存 latest 一行，删掉板块页会直接空",
                "dataset": dataset, "dry_run": dry_run, "applied": False}

    with _lock:
        conn = _connect()
        known = {r[0] for r in conn.execute(
            "SELECT DISTINCT dataset FROM snapshot")}
        if dataset not in known:
            return {"error": f"库内没有 {dataset}",
                    "known": sorted(known), "dry_run": dry_run, "applied": False}

        if date:
            where, params = "dataset=? AND trade_date=?", (dataset, date)
        else:
            where, params = "dataset=? AND trade_date<?", (dataset, before)

        rows = conn.execute(
            f"SELECT trade_date, LENGTH(CAST(payload AS BLOB)) AS bytes,"
            f"       row_count FROM snapshot WHERE {where} ORDER BY trade_date",
            params,
        ).fetchall()
        matched = [dict(r) for r in rows]

    # 硬下限：保留最近 floor 天（按日期降序数）
    with _lock:
        conn = _connect()
        all_dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM snapshot WHERE dataset=? "
            "ORDER BY trade_date DESC", (dataset,))]
    keep = set(all_dates[:floor])
    blocked = [m for m in matched if m["trade_date"] in keep]
    deletable = [m for m in matched if m["trade_date"] not in keep]

    result = {
        "dataset": dataset,
        "dry_run": dry_run,
        "applied": False,
        "matched": len(matched),
        "deletable": len(deletable),
        "deletable_bytes": sum(m["bytes"] for m in deletable),
        "dates": [m["trade_date"] for m in deletable],
        "blocked": [m["trade_date"] for m in blocked],
        "floor_days": floor,
        "reason": (f"至少保留最近 {floor} 天，{len(blocked)} 天触到下限"
                   if blocked else ""),
    }
    if dry_run or not deletable:
        return result

    with _lock:
        conn = _connect()
        deleted = 0
        for m in deletable:
            cur = conn.execute(
                "DELETE FROM snapshot WHERE dataset=? AND trade_date=?",
                (dataset, m["trade_date"]))
            deleted += cur.rowcount
        conn.commit()
    result.update({"applied": True, "deleted": deleted,
                   "note": "auto_vacuum=0，空间要 VACUUM 才回收"})
    logger.warning("数据清理 %s：删除 %d 行（dry_run=%s）",
                   dataset, deleted, dry_run)
    return result


def purge_by_retention(dry_run: bool = True) -> Dict[str, Any]:
    """按 `settings.retention_*` 逐个 dataset 清理过期快照。**dry_run 默认 True。**

    「保留 N 天」= **按库内已有的日期倒序，最近 N 天留着**，其余删掉。
    只看库里有什么、不推交易日历 —— 推日历会让旧数据随时间自动「过期」，
    冷启动或长期没跑的库会被误清。

    硬下限优先：实际保留数取 `max(retention, DATASET_FLOOR)`，`sector_flow_*`
    只存 latest 一行，直接跳过不进计划。返回里 `floored=True` 表示这次是底线
    抬高了保留数（配置的小于底线），前端要如实说明。
    """
    spec = {
        "main_force": settings.retention_main_force_days,
        "stock_net_inflow": settings.retention_inflow_days,
        "pool_zt": settings.retention_pool_days,
        "pool_zb": settings.retention_pool_days,
        "pool_dt": settings.retention_pool_days,
        "pool_strong": settings.retention_pool_days,
    }

    plan: List[Dict[str, Any]] = []
    # 先把「要删哪些日期」算出来存住：真删时直接用这份，不重算 ——
    # 否则两次读之间若有新数据落库，实际删掉的会和预览的不一致。
    cuts: Dict[str, List[str]] = {}
    total_del = 0
    total_bytes = 0
    with _lock:
        conn = _connect()
        for ds, days in spec.items():
            floor = DATASET_FLOOR.get(ds, 1)
            keep_n = max(int(days), floor)
            rows = conn.execute(
                "SELECT trade_date, LENGTH(CAST(payload AS BLOB)) AS bytes"
                "  FROM snapshot WHERE dataset=? ORDER BY trade_date DESC",
                (ds,),
            ).fetchall()
            dates = [r["trade_date"] for r in rows]
            bytes_of = {r["trade_date"]: r["bytes"] for r in rows}
            cut = dates[keep_n:]
            cuts[ds] = cut
            total_del += len(cut)
            total_bytes += sum(bytes_of[d] for d in cut)
            plan.append({
                "dataset": ds,
                "retention_days": int(days),
                "keep_days": keep_n,
                "floor_days": floor,
                "floored": keep_n != int(days),
                "dates_total": len(dates),
                "deletable": len(cut),
                "bytes": sum(bytes_of[d] for d in cut),
                # 降序：cut[0] 是要删里最新的，cut[-1] 是要删里最老的
                "del_newest": cut[0] if cut else None,
                "del_oldest": cut[-1] if cut else None,
                "kept_newest": dates[0] if dates else None,
                "kept_oldest": dates[keep_n - 1] if len(dates) >= keep_n else None,
            })

    result: Dict[str, Any] = {
        "dry_run": dry_run, "applied": False,
        "plan": plan,
        "total_deletable": total_del,
        "total_bytes": total_bytes,
        "deleted": 0,
    }
    if dry_run or not total_del:
        return result

    # 逐条删而不是 `date NOT IN (...)`：保留集可能上千天，会撞 SQLite
    # 默认 999 个绑定变量的上限；executemany 每条只占 2 个参数。
    with _lock:
        conn = _connect()
        deleted = 0
        for ds, cut in cuts.items():
            if not cut:
                continue
            conn.executemany(
                "DELETE FROM snapshot WHERE dataset=? AND trade_date=?",
                [(ds, d) for d in cut])
            deleted += len(cut)
        conn.commit()
    result.update({"applied": True, "deleted": deleted,
                   "note": "auto_vacuum=0，空间要 VACUUM 才回收"})
    logger.warning("按保留策略清理：删除 %d 行（dry_run=%s）", deleted, dry_run)
    return result


def purge_job_runs(unfinished_only: bool = True,
                   dry_run: bool = True) -> Dict[str, Any]:
    """清理任务日志：未收尾残留 + 超出 keep 的旧记录。

    未收尾（finished_at IS NULL）是进程被杀时留下的，属于真残留；
    已收尾的按 `settings.job_run_keep` 保留最近 N 条。
    """
    keep = max(1, int(settings.job_run_keep))
    with _lock:
        conn = _connect()
        unfinished = [dict(r) for r in conn.execute(
            "SELECT id, kind, started_at, status FROM job_run "
            "WHERE finished_at IS NULL ORDER BY id")]
        total = conn.execute("SELECT COUNT(*) FROM job_run").fetchone()[0]

        # 超出 keep 的最老记录（不包含未收尾的，那批由 unfinished 单独处理）
        over = []
        if total > keep:
            over = [dict(r) for r in conn.execute(
                "SELECT id, kind, started_at, status FROM job_run "
                "WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT -1 "
                "OFFSET ?", (keep,))]

    targets = unfinished if unfinished_only else (unfinished + over)
    result = {
        "dry_run": dry_run, "applied": False,
        "total_rows": total, "keep": keep, "unfinished_only": unfinished_only,
        "unfinished": len(unfinished), "over_limit": len(over),
        "deletable": len(targets),
        "ids": [t["id"] for t in targets],
    }
    if dry_run or not targets:
        return result

    with _lock:
        conn = _connect()
        conn.executemany("DELETE FROM job_run WHERE id=?",
                         [(t["id"],) for t in targets])
        conn.commit()
    result.update({"applied": True, "deleted": len(targets)})
    logger.warning("清理 job_run：%d 行（dry_run=%s）", len(targets), dry_run)
    return result


def vacuum() -> Dict[str, Any]:
    """回收空间。`auto_vacuum=0` 时删除不会缩文件，必须显式 VACUUM。

    VACUUM 不能在事务里跑，先 commit 掉挂起的事务。
    """
    path = Path(settings.db_path)
    before = _size_of(path)
    with _lock:
        conn = _connect()
        conn.commit()
        conn.execute("VACUUM")
        conn.commit()
    after = _size_of(path)
    logger.info("VACUUM：%d -> %d bytes", before, after)
    return {"before": before, "after": after, "freed": before - after}


# ==== 自动买卖策略 V1（docs/ai_trading_lab/）==============================
# 交易表的 SQL **只有这一处**；策略/风控/撮合的业务逻辑在 app/trading/。
# 记账必须走 sim_settle：现金、成交、订单状态、持仓四写在同一把锁内提交，
# 且 sim_fill.order_id UNIQUE 保证重复执行一笔账都多记不了。


def _trade_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def trading_param_ensure(version: str, params_json: str,
                         note: str = "") -> None:
    """参数版本落档（已存在则不动 —— 版本由内容哈希决定，同版本即同参数）。"""
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO strategy_params(version, params, note, created_at)"
            " VALUES (?,?,?,?) ON CONFLICT(version) DO NOTHING",
            (version, params_json, note, _trade_now()))
        conn.commit()


def trading_run_get(strategy: str, signal_date: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT * FROM strategy_run WHERE strategy=? AND signal_date=?",
            (strategy, signal_date)).fetchone()
    return dict(row) if row else None


def trading_run_create(strategy: str, signal_date: str, exec_date: Optional[str],
                       rule_version: str, param_version: str,
                       params_json: str, status: str,
                       refusal_reason: Optional[str] = None,
                       risk_state: Optional[str] = None,
                       quality_state: Optional[str] = None,
                       quality_json: Optional[str] = None,
                       candidates_n: int = 0, signals_n: int = 0
                       ) -> Tuple[int, bool]:
    """建 run。返回 (run_id, created) —— 同键已存在时不插行、回既有 id。

    `created=False` 是幂等判断的第一手依据：调用方据此区分「这次真跑了」
    和「同日已跑过，直接回既有结果」。
    """
    now = _trade_now()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT OR IGNORE INTO strategy_run(strategy, signal_date,"
            " exec_date, rule_version, param_version, params_json, status,"
            " refusal_reason, risk_state, quality_state, quality_json,"
            " candidates_n, signals_n, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (strategy, signal_date, exec_date, rule_version, param_version,
             params_json, status, refusal_reason, risk_state, quality_state,
             quality_json, int(candidates_n), int(signals_n), now))
        created = cur.rowcount == 1
        row = conn.execute(
            "SELECT id FROM strategy_run WHERE strategy=? AND signal_date=?",
            (strategy, signal_date)).fetchone()
        conn.commit()
    return int(row["id"]), created


def trading_run_update(run_id: int, fields: Dict[str, Any]) -> None:
    """REFUSED → OK 升级等状态回写。字段名只来自代码字面量。"""
    if not fields:
        return
    cols = ",".join("%s=?" % k for k in fields)
    with _lock:
        conn = _connect()
        conn.execute("UPDATE strategy_run SET %s WHERE id=?" % cols,
                     (*fields.values(), run_id))
        conn.commit()


def trading_run_list(limit: int = 20) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM strategy_run ORDER BY signal_date DESC, id DESC"
            " LIMIT ?", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def trading_signal_add(rows: Sequence[Dict[str, Any]]) -> int:
    """批量落信号。INSERT OR IGNORE + UNIQUE(run_id, code) = 同轮重复落不进第二条。"""
    if not rows:
        return 0
    now = _trade_now()
    payload = []
    for r in rows:
        reasons = r.get("reasons")
        payload.append((
            int(r["run_id"]), str(r["code"]), r.get("name"), str(r["side"]),
            str(r["signal_date"]), r.get("exec_date"),
            json.dumps(reasons if isinstance(reasons, list) else [],
                        ensure_ascii=False),
            r.get("target_qty"), r.get("alloc_pct"), r.get("est_price"),
            str(r["rule_version"]), str(r["param_version"]),
            r.get("snapshot_date"), r.get("bar_date"), now))
    with _lock:
        conn = _connect()
        cur = conn.executemany(
            "INSERT OR IGNORE INTO trade_signal(run_id, code, name, side,"
            " signal_date, exec_date, reasons, target_qty, alloc_pct,"
            " est_price, rule_version, param_version, snapshot_date,"
            " bar_date, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            payload)
        n = cur.rowcount
        conn.commit()
    return int(n)


def _decode_trade_signal(row: Dict[str, Any]) -> Dict[str, Any]:
    # 注意：不许叫 _decode_signal —— 2474 行已有同名函数（入参 sqlite3.Row，
    # 供 research_signals_of 用），模块级后定义会覆盖先定义、把 Row 打进这版
    # 只认 dict 的实现（2026-10-18 真翻过车，_research_check 当场红）。
    try:
        row["reasons"] = json.loads(row.get("reasons") or "[]")
    except (TypeError, ValueError):
        row["reasons"] = []
    return row


def trading_signals_by_run(run_id: int) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM trade_signal WHERE run_id=? ORDER BY side, code",
            (int(run_id),)).fetchall()
    return [_decode_trade_signal(dict(r)) for r in rows]


def trading_orders_list(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM sim_order ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def trading_orders_pending() -> List[Dict[str, Any]]:
    """待执行订单，**带所属 run 的参数快照**。

    执行用的滑点/费率必须来自「当时生成这批订单的那套参数」，不能取全局默认
    —— 否则事后改参数会把历史订单的成交价改掉，复现就断了。
    """
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT o.*, s.run_id, s.param_version AS signal_param_version,"
            "       r.params_json AS run_params"
            " FROM sim_order o"
            " JOIN trade_signal s ON s.id = o.signal_id"
            " JOIN strategy_run r ON r.id = s.run_id"
            " WHERE o.status='PENDING'"
            " ORDER BY COALESCE(o.exec_date, '99999999'), o.id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["run_params"] = json.loads(d.get("run_params") or "{}")
        except (TypeError, ValueError):
            d["run_params"] = {}
        out.append(d)
    return out


def sim_order_place(signal_id: int, code: str, name: Optional[str],
                    side: str, qty: int, status: str,
                    exec_date: Optional[str], est_price: Optional[float],
                    reject_reason: Optional[str] = None) -> Optional[int]:
    """落一张单（PENDING 或风控当场 REJECTED）。信号已有单 → 回 None（幂等）。"""
    now = _trade_now()
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "INSERT OR IGNORE INTO sim_order(signal_id, code, name, side, qty,"
            " status, exec_date, est_price, reject_reason, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (int(signal_id), code, name, side, int(qty), status, exec_date,
             est_price, reject_reason, now, now))
        created = cur.rowcount == 1
        row = conn.execute(
            "SELECT id FROM sim_order WHERE signal_id=?",
            (int(signal_id),)).fetchone()
        conn.commit()
    return int(row["id"]) if created else None


def sim_order_set(order_id: int, status: str,
                  reject_reason: Optional[str] = None,
                  fill_price: Optional[float] = None,
                  fee: Optional[float] = None) -> None:
    """改单据状态（REJECTED / EXPIRED 用；FILLED 走 sim_settle 一起提交）。"""
    with _lock:
        conn = _connect()
        conn.execute(
            "UPDATE sim_order SET status=?, reject_reason=?, fill_price=?,"
            " fee=?, updated_at=? WHERE id=?",
            (status, reject_reason, fill_price, fee, _trade_now(),
             int(order_id)))
        conn.commit()


def sim_account_get() -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM sim_account WHERE id=1").fetchone()
    return dict(row) if row else None


def sim_account_init(initial_cash: float) -> Dict[str, Any]:
    """开账户（幂等；已存在则原样返回 —— 事后改 initial_cash 不动老账户）。"""
    now = _trade_now()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO sim_account(id, cash, initial_cash, updated_at)"
            " VALUES (1,?,?,?) ON CONFLICT(id) DO NOTHING",
            (float(initial_cash), float(initial_cash), now))
        conn.commit()
        row = conn.execute("SELECT * FROM sim_account WHERE id=1").fetchone()
    return dict(row)


def sim_positions_list() -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM sim_position ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def sim_position_get(code: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM sim_position WHERE code=?",
                           (code,)).fetchone()
    return dict(row) if row else None


def sim_positions_unlock(exec_date: str) -> int:
    """T+1 日切：只解锁「买入日 < 新执行日」的冻结量。

    条件式判断（不看是否「今天第一次执行」）= 天然幂等：同一执行日重复调用
    解锁 0 行，不会把当日刚买的提前放出来。
    """
    with _lock:
        conn = _connect()
        cur = conn.execute(
            "UPDATE sim_position SET locked_qty=0, locked_date=NULL,"
            " updated_at=? WHERE locked_date IS NOT NULL AND locked_date < ?",
            (_trade_now(), str(exec_date)))
        conn.commit()
    return int(cur.rowcount)


def sim_settle(order_id: int, fill: Dict[str, Any],
               position: Optional[Dict[str, Any]]) -> bool:
    """成交记账的唯一入口：现金、成交、订单状态、持仓四写原子提交。

    幂等由 sim_fill(order_id UNIQUE) 保证 —— 重复调用（重试、重复执行）
    直接回 False，一个字节都不多写。`position=None` 表示清仓（删持仓行）。

    账户行不存在时**抛错不静默**：现金更新命中 0 行等于这笔成交没记上账，
    悄悄过去就伪造出一笔「成交了但钱没动」的账。
    """
    now = _trade_now()
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO sim_fill(order_id, code, side, qty, price, fee,"
                " cash_delta, trade_date, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (int(order_id), str(fill["code"]), str(fill["side"]),
                 int(fill["qty"]), float(fill["price"]), float(fill["fee"]),
                 float(fill["cash_delta"]), str(fill["trade_date"]), now))
            cur = conn.execute(
                "UPDATE sim_account SET cash = cash + ?, updated_at=?"
                " WHERE id=1",
                (float(fill["cash_delta"]), now))
            if cur.rowcount != 1:
                raise RuntimeError(
                    "sim_account 未初始化，成交记账中止（不静默丢账）")
            conn.execute(
                "UPDATE sim_order SET status='FILLED', fill_price=?, fee=?,"
                " reject_reason=NULL, updated_at=? WHERE id=?",
                (float(fill["price"]), float(fill["fee"]), now,
                 int(order_id)))
            if position is None:
                conn.execute("DELETE FROM sim_position WHERE code=?",
                             (str(fill["code"]),))
            else:
                conn.execute(
                    "INSERT INTO sim_position(code, name, qty, locked_qty,"
                    " locked_date, avg_cost, cost_factor, entry_date,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(code) DO UPDATE SET"
                    "  name=excluded.name, qty=excluded.qty,"
                    "  locked_qty=excluded.locked_qty,"
                    "  locked_date=excluded.locked_date,"
                    "  avg_cost=excluded.avg_cost,"
                    "  cost_factor=excluded.cost_factor,"
                    "  entry_date=excluded.entry_date,"
                    "  updated_at=excluded.updated_at",
                    (str(position["code"]), position.get("name"),
                     int(position["qty"]), int(position.get("locked_qty", 0)),
                     position.get("locked_date"),
                     float(position["avg_cost"]), position.get("cost_factor"),
                     str(position["entry_date"]), now))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except Exception:
            conn.rollback()
            raise
    return True


# ---- 行情读取（只读、零联网）---------------------------------------------

def bars_row(table: str, code: str, trade_date: str) -> Optional[Dict[str, Any]]:
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        row = conn.execute(
            f"SELECT * FROM {T} WHERE code=? AND trade_date=?",
            (code, str(trade_date))).fetchone()
    return dict(row) if row else None


def bars_next_date(table: str, after: str) -> Optional[str]:
    """表内严格晚于 ``after`` 的最早交易日（撮合日推导用）。"""
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        row = conn.execute(
            f"SELECT MIN(trade_date) AS d FROM {T} WHERE trade_date > ?",
            (str(after),)).fetchone()
    return str(row["d"]) if row and row["d"] else None


def bars_prev_date(table: str, before: str) -> Optional[str]:
    """表内严格早于 ``before`` 的最近交易日（涨跌停判定要前收）。"""
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        row = conn.execute(
            f"SELECT MAX(trade_date) AS d FROM {T} WHERE trade_date < ?",
            (str(before),)).fetchone()
    return str(row["d"]) if row and row["d"] else None


def bars_load_window(table: str, codes: Sequence[str],
                     since: str, until: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """按代码取窗口内行情。**until 是硬上界** —— 信号日之后的行情一个字节
    都不许进策略上下文，这是防未来数据泄漏在 SQL 层的落点。"""
    if not codes:
        return {}
    T = _bar_table(table)
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    seq = list(dict.fromkeys(str(c) for c in codes))
    with _lock:
        conn = _connect()
        for i in range(0, len(seq), 400):
            part = seq[i:i + 400]
            ph = ",".join("?" for _ in part)
            for row in conn.execute(
                    f"SELECT * FROM {T} WHERE code IN ({ph})"
                    f" AND trade_date >= ? AND trade_date <= ?",
                    (*part, str(since), str(until))):
                d = dict(row)
                out.setdefault(str(d["code"]), {})[str(d["trade_date"])] = d
    return out


def bars_window_dates(table: str, since: str, until: str) -> List[str]:
    """窗口内的交易日升序（含两端，同样不越过 until）。"""
    T = _bar_table(table)
    with _lock:
        conn = _connect()
        rows = conn.execute(
            f"SELECT DISTINCT trade_date FROM {T}"
            f" WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
            (str(since), str(until))).fetchall()
    return [str(r[0]) for r in rows]


# ==== 实时纸面自动交易：规则存取 + 账户/持仓/流水（rules.py + live_loop.py）====

def trading_rules_list() -> List[Dict[str, Any]]:
    """全部规则行（params JSON 解析成 dict），按 (category, order_n, id) 稳定序。"""
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM trading_rule ORDER BY category, order_n, id"
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["enabled"] = bool(d.get("enabled"))
        d["order"] = int(d.get("order_n") or 0)
        try:
            d["params"] = json.loads(d.get("params") or "{}")
        except (TypeError, ValueError):
            d["params"] = {}
        out.append(d)
    return out


def trading_rules_save(rules: Sequence[Dict[str, Any]]) -> None:
    """整套替换（调用方已用 rules.normalize_rules 校验过）。单事务原子。"""
    now = _trade_now()
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM trading_rule")
            for r in rules:
                conn.execute(
                    "INSERT INTO trading_rule(id, category, type, name, enabled,"
                    " order_n, params, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                    (str(r["id"]), str(r["category"]), str(r["type"]),
                     str(r["name"]), 1 if r.get("enabled") else 0,
                     int(r.get("order", 0)),
                     json.dumps(r.get("params") or {}, ensure_ascii=False),
                     now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def trading_rules_count() -> int:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT COUNT(*) FROM trading_rule").fetchone()
    return int(row[0]) if row else 0


# ---- 实时纸面账户 ----
def live_account_get() -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM live_account WHERE id=1").fetchone()
    return dict(row) if row else None


def live_account_init(initial_cash: float) -> Dict[str, Any]:
    now = _trade_now()
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO live_account(id, cash, initial_cash, realized_pnl,"
            " updated_at) VALUES (1,?,?,0,?) ON CONFLICT(id) DO NOTHING",
            (float(initial_cash), float(initial_cash), now))
        conn.commit()
        row = conn.execute("SELECT * FROM live_account WHERE id=1").fetchone()
    return dict(row)


# ---- 实时纸面持仓 ----
def live_positions_list() -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM live_position ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def live_position_get(code: str) -> Optional[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        row = conn.execute("SELECT * FROM live_position WHERE code=?",
                           (str(code),)).fetchone()
    return dict(row) if row else None


# ---- 实时成交流水 ----
def live_trades_between(start_ts: str, end_ts: str) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM live_trade WHERE ts >= ? AND ts <= ? ORDER BY ts",
            (str(start_ts), str(end_ts))).fetchall()
    return [dict(r) for r in rows]


def live_trades_recent(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        conn = _connect()
        rows = conn.execute(
            "SELECT * FROM live_trade ORDER BY ts DESC LIMIT ?",
            (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def live_fill(ts: str, code: str, name: Optional[str], side: str, qty: int,
              price: float, fee: float, pnl: Optional[float],
              rule: Optional[str], note: Optional[str],
              cash_delta: float, position: Optional[Dict[str, Any]]) -> int:
    """实时成交原子记账：流水 + 账户 + 持仓三写一次提交。

    ``position`` 为 None 表示卖出后清仓（删持仓行）；dict 为新持仓（upsert，
    qty<=0 自动删）。现金更新命中 0 行 = 账户未初始化，抛错不静默（同 sim_settle）。
    """
    now = _trade_now()
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE live_account SET cash = cash + ?,"
                " realized_pnl = realized_pnl + ?, updated_at=? WHERE id=1",
                (float(cash_delta), float(pnl or 0.0), now))
            if cur.rowcount != 1:
                raise RuntimeError("live_account 未初始化，成交记账中止")
            conn.execute(
                "INSERT INTO live_trade(ts, code, name, side, qty, price,"
                " amount, fee, pnl, rule, note) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (str(ts), str(code), name, str(side), int(qty), float(price),
                 float(qty) * float(price), float(fee), pnl, rule, note))
            if position is not None:
                if int(position.get("qty") or 0) <= 0:
                    conn.execute("DELETE FROM live_position WHERE code=?",
                                 (str(code),))
                else:
                    conn.execute(
                        "INSERT INTO live_position(code, name, qty, avg_cost,"
                        " peak_price, entry_ts, updated_at) VALUES (?,?,?,?,?,?,?)"
                        " ON CONFLICT(code) DO UPDATE SET name=excluded.name,"
                        " qty=excluded.qty, avg_cost=excluded.avg_cost,"
                        " peak_price=excluded.peak_price,"
                        " updated_at=excluded.updated_at",
                        (str(code), position.get("name"),
                         int(position.get("qty") or 0),
                         float(position.get("avg_cost") or 0.0),
                         position.get("peak_price"),
                         str(position.get("entry_ts") or now), now))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return 1
