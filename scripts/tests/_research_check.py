# -*- coding: utf-8 -*-
r"""阶段 1 —— 数据可回测性审计的**离线**回归，2026-10-15。

对应 docs/backtest/implementation-audit.md 与 `app/research/audit.py`。
每一条都盯住一个当初会露馅的点：

  1) **零联网**：审计是「查库出结论」，不是「去问数据源」。开头封死
     TCP + DNS，收尾断言一条都没漏出去。
  2) **空库不崩**：数据库文件不存在时仍要给出五域全 BLOCKED 的报告，
     而不是抛异常 —— 新装环境第一次点开页面就会走到这里。
  3) **日线缺失必须 BLOCKED**：库里没有带 open/high/low/close 四列的表，
     就不许报告成 PARTIAL/READY。这正是「不得静默填零」的反面。
  4) **PARTIAL 不得自动当 READY**：有冷启动日或事后回补日时，主力净额
     必须停在 PARTIAL，即使窗口凑得齐。
  5) **凑不齐窗口才 BLOCKED**：8 天观测 >= 所需 5 日窗口 => 主力净额 READY，
     大单全 NULL => BLOCKED，域内取最差 —— 两档状态都要能分开报。
  6) **观测值 vs 回补值**：`fetched_at` 转 UTC+8 后与 `trade_date` 同日才算
     当日观测；晚一天即回补，逐日标进 `observed`。
  7) **日线质量口径**：重复 (code,date)、close<=0、amount<=0、high<low、
     OHLCV 缺失率、复权字段是否落库 —— 有则计数，无则 0，不臆造。
  8) **250 日窗口**：日线只有 30 天时 `sufficient_for_price_position` 必须
     False（不得用短窗冒充 250 日）；拉到 260 天才转 True。
  9) **不造假**：报告里出现的每个业务日期都必须在夹具日期集合内，
     否则就是审计自己编出来的。
 10) **信号日逐日判定**：每个信号日都必须有非空 `missing`（当前日线不落库
     时），不得因为「别的日子能算」就放行。
 11) **API 接线**：`/api/research/audit` 已注册且直接返回服务报告。

全离线：开头封死 TCP + DNS，不碰真实 market.db（用临时夹具库）。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_research_check.py
"""
import json
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

# ---- 0) 挡住所有非回环连接与 DNS 查询 ---------------------------------------
_orig = socket.socket.connect
_orig_gai = socket.getaddrinfo
_BLOCKED = []
_LOOP = ("127.0.0.1", "::1", "localhost")


def _guard(self, addr):  # noqa: ANN001
    host = addr[0] if isinstance(addr, tuple) and addr else addr
    if str(host) not in _LOOP:
        _BLOCKED.append(str(host))
        raise AssertionError("BLOCKED-NONLOOPBACK: %s" % (addr,))
    return _orig(self, addr)


def _gai_guard(host, port, *a, **k):  # noqa: ANN001
    if str(host) not in _LOOP:
        _BLOCKED.append("dns:%s" % (host,))
        raise AssertionError("BLOCKED-DNS: %s:%s" % (host, port))
    return _orig_gai(host, port, *a, **k)


socket.socket.connect = _guard
socket.getaddrinfo = _gai_guard

sys.path.insert(0, r"D:\test-kanban\backend")

from app.config import settings                      # noqa: E402, I001

from app.research import audit as A                  # noqa: E402

PASS = 0
FAILS = []


def ok(cond, label):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)


# ---- 夹具 -------------------------------------------------------------------
# 这些日期与代码是**测试夹具**，不是行情。source 一律标 "fixture"，避免与
# 真实快照混淆。
INFLOW_DATES = ["20251222", "20251223", "20251224", "20251225",
                "20251226", "20251229", "20251230", "20251231"]
SIGNAL_DATES = ["20251223", "20260105", "20260106", "20260107"]
CODES = ["600001", "600002", "600003"]

SNAPSHOT_DDL = """
CREATE TABLE IF NOT EXISTS snapshot (
    dataset     TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    payload     TEXT NOT NULL,
    row_count   INTEGER NOT NULL DEFAULT 0,
    source      TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (dataset, trade_date)
);
"""

BAR_DDL = """
CREATE TABLE IF NOT EXISTS daily_bar (
    code        TEXT NOT NULL,
    trade_date  TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    amount      REAL,
    turnover    REAL,
    pct_chg     REAL,
    adj_factor  REAL
);
"""

WEIGHTS = {"inflow_consistency": 30, "volume_up_price_flat": 20,
           "large_order_ratio": 15, "price_position_low": 15,
           "turnover_moderate": 10, "chip_convergence": 10}


def _params():
    """每天完全一致的参数快照 —— 权重稳定是「可重放」的前提。"""
    return {"weights": dict(WEIGHTS), "top_n": 30, "preselect_top": 300,
            "min_listed_days": 60, "max_boards": 1, "max_gain_20d": 40.0,
            "inflow_window_days": 5, "price_position_days": 250,
            "turnover_range": [3.0, 12.0], "chip_threshold": 0.8,
            "float_mv_min": 1500000000, "float_mv_max": 200000000000}


def _all_rows(date):
    rows = []
    for i, code in enumerate(CODES):
        item = {"code": code, "name": "夹具%s" % code[-1],
                "list_date": "20100101", "price": 10.0 + i,
                "pct_chg": 1.0, "turnover": 5.0, "amount": 1e8,
                "float_mv": 5e9, "coverage": 1.0,
                "score": 80.0 - i, "rank": i + 1,
                "dims": {"inflow_consistency": {"score": 70.0}}}
        if i != 1:                      # 故意留一只没有行业，测缺失率口径
            item["industry"] = "制造业"
        rows.append(item)
    return rows


def _main_force_payload(date):
    rows = _all_rows(date)
    return [json.dumps([{
        "date": date, "all_rows": rows, "rows": rows,
        "params": _params(),
        "model": {"degraded": [], "total_weight": 100.0, "used_weights": dict(WEIGHTS)},
        "stats": {}, "elapsed_sec": 1.0, "generated_at": "2026-01-01T00:00:00",
        "disclaimer": "夹具，仅用于测试",
    }], ensure_ascii=False)]


def _inflow_payload(day_index, late=False):
    rows = []
    for i, code in enumerate(CODES):
        lo = 1000.0 if (late and i % 2 == 0) else None   # 大单只有一半非空
        rows.append({"code": code, "amount": 1e8,
                     "net_inflow": 1e6, "large_order_net": lo})
    return rows


def _bar_dates(n):
    base = datetime(2025, 11, 1)
    return [(base + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]


def _mk_db(name, late_inflow=False, bar_days=0, dirty_bar=False):
    """建一个夹具库并返回 (路径, 允许出现的业务日期集合)。"""
    root = Path(tempfile.mkdtemp(prefix=name))
    path = root / "market.db"
    con = sqlite3.connect(path)
    con.executescript(SNAPSHOT_DDL)
    for i, d in enumerate(INFLOW_DATES):
        payload = _inflow_payload(i, late=late_inflow)
        # fetched_at：默认与 trade_date 同日(UTC+8 上午)；late 则晚一天
        if late_inflow and d == INFLOW_DATES[-1]:
            fetched = "2026-01-01T02:00:00+00:00"
        else:
            fetched = "%s-%s-%sT02:00:00+00:00" % (d[:4], d[4:6], d[6:])
        con.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?)",
                    ("stock_net_inflow", d, json.dumps(payload, ensure_ascii=False),
                     len(payload), "fixture", fetched))
    for d in SIGNAL_DATES:
        con.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?)",
                    ("main_force", d, _main_force_payload(d)[0], 1,
                     "fixture", "%s-%s-%sT02:00:00+00:00" % (d[:4], d[4:6], d[6:])))

    allowed = set(INFLOW_DATES) | set(SIGNAL_DATES)
    if bar_days:
        con.executescript(BAR_DDL)
        dates = _bar_dates(bar_days)
        allowed |= set(dates)
        for d in dates:
            for code in CODES:
                con.execute(
                    "INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (code, d, 10.0, 10.5, 9.5, 10.2, 1e6, 1e8, 5.0, 0.5, 1.0))
        if dirty_bar:
            # 重复键 + 收盘价为 0 + 成交额为 0
            con.execute("INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (CODES[0], dates[0], 10.0, 10.5, 9.5, 10.2,
                         1e6, 1e8, 5.0, 0.5, 1.0))
            con.execute("INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        ("600099", dates[0], 1.0, 1.0, 0.5, 0.0, 100.0, 0.0,
                         0.0, 0.0, 1.0))
    con.commit()
    con.close()
    return path, allowed


def _audit(path):
    settings.db_path = path
    return A.BacktestDataAuditService().audit()


def _date_fields(node, acc):
    """收集报告里**业务日期**字段的取值，用来查有没有凭空冒出来的日期。"""
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("first_date", "last_date", "signal_date", "trade_date",
                     "dates", "backfilled_dates"):
                if isinstance(v, list):
                    acc.extend(x for x in v if isinstance(x, str))
                elif isinstance(v, str):
                    acc.append(v)
            else:
                _date_fields(v, acc)
    elif isinstance(node, list):
        for it in node:
            _date_fields(it, acc)


# ==== 1) 空库 ================================================================
empty_dir = Path(tempfile.mkdtemp(prefix="kanban_research_empty"))
r0 = _audit(empty_dir / "market.db")
ok(r0["db_exists"] is False, "A1 空库应标记 db_exists=False")
ok(set(r0["domains"]) == {"daily_bars", "fund_flow", "universe",
                           "benchmark", "scoring_snapshots"},
   "A2 空库仍要给出五个数据域")
ok(all(v.get("status") == A.BLOCKED for v in r0["domains"].values()),
   "A3 空库五域必须全 BLOCKED")
ok(r0["signal_days"] == [], "A4 空库没有信号日")
ok(r0["overall"]["status"] == A.BLOCKED, "A5 空库总评 BLOCKED")
ok(r0["overall"]["strict_full_model_backtest"] is False,
   "A6 空库不得声称可跑严格回测")

# ==== 2) 夹具库：无日线、当日观测、大单全 NULL ================================
p_a, allow_a = _mk_db("kanban_research_a_")
ra = _audit(p_a)
d_a = ra["domains"]

ok(ra["db_exists"] is True, "B1 夹具库应识别为存在")
ok(d_a["daily_bars"]["status"] == A.BLOCKED, "B2 无日线表 => daily_bars BLOCKED")
ok(d_a["daily_bars"]["tables"] == [], "B3 不得凭空列出日线表")
ok(any("不落库" in x for x in d_a["daily_bars"]["reasons"]),
   "B4 日线缺失原因要写明是不落库，而非笼统报错")

ok(d_a["fund_flow"]["primary"]["status"] == A.READY,
   "B5 8 天 >= 5 日窗口且覆盖均匀、当日观测 => 主力净额 READY")
ok(d_a["fund_flow"]["primary"]["securities_full_window"] == len(CODES),
   "B6 每只券都凑满了窗口")
ok(d_a["fund_flow"]["large_order"]["non_null"] == 0, "B7 大单非空计数须为实测 0")
ok(d_a["fund_flow"]["large_order"]["status"] == A.BLOCKED,
   "B8 大单全 NULL => BLOCKED（0 不等于没问题）")
ok(d_a["fund_flow"]["status"] == A.BLOCKED,
   "B9 域内取最差：主力 READY + 大单 BLOCKED => 域 BLOCKED")
ok(all(p["observed"] == "same_day" for p in d_a["fund_flow"]["primary"]["per_day"]),
   "B10 fetched_at 同日 => 全部标 same_day")
ok(d_a["fund_flow"]["primary"]["backfilled_dates"] == [], "B11 无回补日")

ok(d_a["universe"]["status"] == A.PARTIAL, "B12 股票池无历史状态 => PARTIAL")
ok(d_a["universe"]["historical_membership"] == A.BLOCKED, "B13 无每日快照 => 历史成员 BLOCKED")
ok(d_a["universe"]["with_industry"] < d_a["universe"]["rows"],
   "B14 缺行业的行要如实计数，不补齐")
ok(d_a["universe"]["with_list_date"] == d_a["universe"]["rows"],
   "B15 list_date 每行都有")

ok(d_a["benchmark"]["status"] == A.PARTIAL, "B16 沪深300 未落库 => PARTIAL")
ok("不得填 0" in " ".join(d_a["benchmark"]["reasons"]), "B17 基准缺失不填 0 要写明")
ok(d_a["benchmark"].get("persisted") is False, "B18 persisted 必须显式 False")

ok(d_a["scoring_snapshots"]["status"] == A.READY, "B19 快照可重放 => READY")
ok(d_a["scoring_snapshots"]["replay_ready"] is True, "B20 replay_ready=True")
ok(d_a["scoring_snapshots"]["weight_variants"] == 1, "B21 权重只有一套")
ok(d_a["scoring_snapshots"]["recompute_from_raw"] == A.BLOCKED,
   "B22 从原始行情重算必须 BLOCKED（日线不落库）")

ok(len(ra["signal_days"]) == len(SIGNAL_DATES), "B23 信号日数量对齐")
ok(all(s["status"] == A.BLOCKED for s in ra["signal_days"]),
   "B24 日线缺失时所有信号日都 BLOCKED")
ok(all(s["missing"] for s in ra["signal_days"]), "B25 每个信号日 missing 不得为空")
first = next(s for s in ra["signal_days"] if s["signal_date"] == "20251223")
ok(first["prior_inflow_days"] < 5 and any("主力净额" in m for m in first["missing"]),
   "B26 前置净额不足 5 日要单独点名")
ok(ra["signal_days"][1]["prior_inflow_days"] == len(INFLOW_DATES),
   "B27 第二个信号日前有 8 日净额")

ok(ra["overall"]["status"] == A.BLOCKED, "B28 总评取最差 => BLOCKED")
ok(ra["overall"]["strict_full_model_backtest"] is False, "B29 不得声称可跑严格回测")
ok(ra["overall"]["replay_only_backtest"] is True, "B30 重放能力要单独报 True")
ok(ra["overall"]["signal_days_ready"] == 0, "B31 就绪信号日 = 0")
ok("授权" in (ra["overall"].get("next_step") or ""), "B32 要给出下一步授权路径")

dates_a = []
_date_fields(ra, dates_a)
ok(bool(dates_a) and not (set(dates_a) - allow_a),
   "B33 报告里不得出现夹具之外的日期：%s" % sorted(set(dates_a) - allow_a))
ok(ra["offline"] is True, "B34 报告标记 offline")

# ==== 3) 夹具库：一日晚回补 + 大单半覆盖 => PARTIAL ===========================
p_b, _ = _mk_db("kanban_research_b_", late_inflow=True)
rb = _audit(p_b)
ff_b = rb["domains"]["fund_flow"]
ok(ff_b["primary"]["status"] == A.PARTIAL, "C1 有回补日 => 主力净额 PARTIAL")
ok(ff_b["primary"]["backfilled_dates"] == [INFLOW_DATES[-1]], "C2 回补日只有最后一日")
ok(ff_b["primary"]["per_day"][-1]["observed"] == "backfilled",
   "C3 该日 observed=backfilled")
ok(ff_b["primary"]["per_day"][0]["observed"] == "same_day", "C4 其余仍 same_day")
ok(ff_b["large_order"]["status"] == A.PARTIAL, "C5 大单半覆盖 => PARTIAL")
ok(0.0 < ff_b["large_order"]["coverage"] < 1.0, "C6 大单覆盖率介于 0 与 1 之间")
ok(ff_b["status"] == A.PARTIAL, "C7 域状态取最差 => PARTIAL")
ok(any("回补" in x for x in ff_b["primary"]["reasons"]), "C8 回补要写进理由")

# ==== 4) 夹具库：日线 30 天（不足 250 日）+ 脏数据 =============================
p_c, allow_c = _mk_db("kanban_research_c_", bar_days=30, dirty_bar=True)
rc = _audit(p_c)
dbars_c = rc["domains"]["daily_bars"]
ok(dbars_c["status"] == A.PARTIAL, "D1 30 天 < 250 日 => PARTIAL（不是 READY）")
ok(dbars_c["sufficient_for_price_position"] is False, "D2 250 日窗口不足")
t_c = dbars_c["tables"][0]
ok(t_c["days"] == 30, "D3 日线交易日数 = 30")
ok(t_c["adjust_fields"] == ["adj_factor"], "D4 复权字段被识别出来")
ok(t_c["duplicate_keys"] == 1, "D5 重复 (code,date) 计数 = 1")
ok(t_c["anomalies"].get("close<=0") == 1, "D6 收盘价为 0 计数 = 1")
ok(t_c["anomalies"].get("amount<=0") == 1, "D7 成交额为 0 计数 = 1")
ok("high<low" not in t_c["anomalies"], "D8 正常行不该报 high<low")
ok(set(t_c["null_rates"]) == {"open", "high", "low", "close", "volume", "amount"},
   "D9 OHLCV 缺失率覆盖 6 个字段")
ok(all(v == 0.0 for v in t_c["null_rates"].values()), "D10 夹具无 NULL")
ok(t_c["days_per_security"]["max"] == 30, "D11 单券最大交易日 = 30")

# ==== 5) 夹具库：日线 260 天（够 250 日）=====================================
p_d, allow_d = _mk_db("kanban_research_d_", bar_days=260)
rd = _audit(p_d)
dbars_d = rd["domains"]["daily_bars"]
ok(dbars_d["status"] == A.READY, "E1 260 天 >= 250 日 => READY")
ok(dbars_d["sufficient_for_price_position"] is True, "E2 250 日窗口足够")
ok(dbars_d["tables"][0]["days"] == 260, "E3 日线交易日数 = 260")
ok(dbars_d["tables"][0]["duplicate_keys"] == 0, "E4 干净夹具无重复键")
dates_d = []
_date_fields(rd, dates_d)
ok(not (set(dates_d) - allow_d), "E5 有日线时日期同样不许越界")

# ==== 6) 纯函数口径 ===========================================================
ok(A.worst(A.READY, A.PARTIAL) == A.PARTIAL, "F1 PARTIAL 不被 READY 抹平")
ok(A.worst(A.PARTIAL, A.BLOCKED) == A.BLOCKED, "F2 BLOCKED 压过 PARTIAL")
ok(A.worst(A.READY, A.READY) == A.READY, "F3 全 READY 仍是 READY")
ok(A.BacktestDataAuditService._is_same_day("20251223", "2025-12-23T02:00:00+00:00"),
   "F4 UTC 当日上午 => 同日")
ok(not A.BacktestDataAuditService._is_same_day("20251223", "2025-12-24T02:00:00+00:00"),
   "F5 晚一天 => 非同日（回补）")
ok(not A.BacktestDataAuditService._is_same_day("20251223", None), "F6 无 fetched_at => 非同日")
ok(not A.BacktestDataAuditService._is_same_day("20251223", "not-a-date"),
   "F7 非法时间戳不崩且判非同日")

# ==== 7) API 接线 =============================================================
settings.db_path = p_a
try:
    from app.api import routes as R

    ok(any(getattr(x, "path", "") == "/api/research/audit" for x in R.router.routes),
       "G1 路由 /api/research/audit 已注册")
    rep = R.research_audit()
    ok(isinstance(rep, dict) and rep.get("domains"), "G2 端点返回五域报告")
    ok(rep.get("offline") is True, "G3 端点报告标记 offline")
    ok(rep["domains"]["daily_bars"]["status"] == A.BLOCKED,
       "G4 端点与服务口径一致")
except ImportError as exc:  # pragma: no cover
    FAILS.append("G0 导入 routes 失败：%s" % exc)

# ==== 8) 研究数据模型（阶段 2） ==============================================
p_res = Path(tempfile.mkdtemp(prefix="kanban_research_8_")) / "market.db"
settings.db_path = p_res
from app.store import db as DB                          # noqa: E402

DB._conn = None        # 换了库路径，强制重连（否则会沿用上一段的连接）
DB.init_db()


def _tables_of(path):
    con = sqlite3.connect(path)
    names = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}
    con.close()
    return names


BASE_TABLES = {"snapshot", "job_run", "watchlist", "kv", "fund_watchlist",
               "fund_estimate_log", "fund_nav_log", "subscriber",
               "subscriber_fund", "push_log", "pay_token", "invite_token",
               "pay_order", "cmd_item",
               # 阶段 3（采集管线）新增的逐域状态表。放进 BASE_TABLES 是因为
               # I3 问的是「**研究模块**新增了几张表」—— 它由管线建、跟研究
               # 无关，算进 delta 会让这条断言永远偏 1。
               "ingest_job",
               # 阶段 4 新增的两张历史行情表。同上，与研究模块无关。
               # 注意它们**不在 fixture 里**（_mk_db 不会建），所以前面那些
               # 「没有日线表 => BLOCKED」的断言不受影响。
               "daily_bar", "index_daily",
               # 2026-10-18 自动买卖策略域新增的七张表。同上，由交易模块建、
               # 与研究模块无关，算进 delta 会让这条断言永远偏 7 —— I3 问的
               # 仍是「研究模块只建了哪 5 张表」。
               "strategy_params", "strategy_run", "trade_signal",
               "sim_account", "sim_order", "sim_fill", "sim_position"}
names0 = _tables_of(p_res)
ok(BASE_TABLES <= names0, "I1 既有业务表一张都还在：%s" % sorted(BASE_TABLES - names0))
RESEARCH_TABLES = {"research_experiments", "research_runs", "research_signals",
                   "research_outcomes", "research_metrics"}
ok(RESEARCH_TABLES <= names0, "I2 五张研究表已建")
ok(len(names0 - BASE_TABLES) == 5, "I3 只新增 5 张表，没有多建：%s"
   % sorted(names0 - BASE_TABLES))
ok("universe_snapshot" not in names0,
   "I4 股票池快照复用 snapshot 表，不另开新表")
DB.init_db()                       # 幂等：再跑一次不许报错、不许多建表
ok(_tables_of(p_res) == names0, "I5 迁移幂等")

# ---- 配置冻结 ----
cfg0 = {"weights": dict(WEIGHTS), "top_n": 30, "note": "v1"}
eid, h1 = DB.research_experiment_create(
    "基线实验", "sixdim-v1", cfg0, "current", "20260924", "20261015")
ok(eid > 0, "I6 实验创建返回 id")
cfg0["weights"]["inflow_consistency"] = 99   # 建完再改源头，库里必须不动
cfg0["top_n"] = 5
got = DB.research_experiment_get(eid)
ok(got["config_snapshot"]["weights"]["inflow_consistency"] == 30,
   "I7 配置已冻结：事后改源 dict 不回写")
ok(got["config_snapshot"]["top_n"] == 30, "I8 top_n 同样冻结")
h2 = DB.research_experiment_config(eid)[0]
ok(h1 == h2, "I9 config_hash 可复读且稳定")
_, h3 = DB.research_experiment_create(
    "基线实验副本", "sixdim-v1",
    {"weights": dict(WEIGHTS), "top_n": 30, "note": "v1"},
    "current", "20260924", "20261015")
ok(h3 == h1, "I10 同参数 => 同 hash")
ok(len(DB.research_experiment_list()) == 2, "I11 实验列表可回读")

# ---- 运行：重入保护 / 取消 / 进度 ----
rid1 = DB.research_run_create(eid)
ok(rid1 > 0, "I12 运行创建成功")
try:
    DB.research_run_create(eid)
    ok(False, "I13 重复运行必须被拒")
except ValueError as exc:
    ok("已有未完成" in str(exc), "I13 重复运行被拒且原因明确：%s" % exc)
try:
    DB.research_run_create(999999)
    ok(False, "I14 对不存在的实验开运行必须被拒")
except ValueError:
    ok(True, "I14 对不存在的实验开运行被拒")
ok(DB.research_run_cancel(rid1) is True, "I15 未完成可取消")
ok(DB.research_run_cancel(rid1) is False, "I16 已取消不可再取消")
rid2 = DB.research_run_create(eid)
ok(rid2 > 0 and rid2 != rid1, "I17 取消后可再开一次")
DB.research_run_update(rid2, progress=42.5, stage="main_force")
rr = DB.research_run_get(rid2)
ok(rr["progress"] == 42.5 and rr["stage"] == "main_force", "I18 进度可回读")
ok(DB.research_run_get(rid2)["status"] == "running", "I19 新运行为 running")
ok(len(DB.research_runs_of(eid)) == 2, "I20 运行历史两条")

# ---- 信号：唯一约束 + 幂等 ----
SIG = [
    {"signal_date": "20261015", "code": "600001", "model_version": "sixdim-v1",
     "score": 82.0, "rank": 1, "top5": True, "top10": True, "top30": True,
     "data_quality": "PARTIAL",
     "factors": {"inflow_consistency": {"raw": 1.2, "score": 70.0}},
     "missing": ["large_order_ratio: 本网络无大单分解"]},
    {"signal_date": "20261015", "code": "600002", "model_version": "sixdim-v1",
     "score": 80.0, "rank": 2, "top30": True, "data_quality": "READY",
     "factors": {"turnover_moderate": {"raw": 5.1, "score": 66.0}}},
]
try:
    DB.research_signals_replace(999999, rid2, SIG)
    ok(False, "I21 向不存在的实验写信号必须被拒")
except ValueError:
    ok(True, "I21 向不存在的实验写信号被拒")
ok(DB.research_signals_replace(eid, rid2, SIG) == 2, "I22 信号写入 2 条")
ok(DB.research_signals_replace(eid, rid2, SIG) == 2, "I23 重复运行覆盖而非追加")
sigs = DB.research_signals_of(eid)
ok(len(sigs) == 2, "I24 唯一约束拦住了重复信号：%d" % len(sigs))
s1 = next(s for s in sigs if s["code"] == "600001")
ok(s1["score"] == 82.0 and s1["top5"] == 1 and s1["rank"] == 1, "I25 信号字段可回读")
ok(s1["factors"]["inflow_consistency"]["score"] == 70.0, "I26 因子原始值与单项分入档")
ok(s1["missing"] and "大单" in s1["missing"][0], "I27 缺失因子必须留痕")
ok(next(s for s in sigs if s["code"] == "600002")["missing"] is None,
   "I28 无缺失时为 NULL，不是空字符串")

# ---- 结果：PENDING 不许有收益、基准缺失不填 0 ----
OUTS = [
    {"signal_date": "20261015", "code": "600001", "horizon": 5,
     "status": "PENDING", "theory_return": None, "bench_return": None,
     "excess_return": None, "fill_status": "NOT_DUE",
     "missing_reason": "观察期未走完"},
    {"signal_date": "20260924", "code": "600001", "horizon": 5,
     "status": "DONE", "theory_return": 0.032, "bench_return": 0.011,
     "excess_return": 0.021, "fill_status": "FILLED", "exec_return": 0.030},
    {"signal_date": "20260924", "code": "600002", "horizon": 5,
     "status": "NO_DATA", "theory_return": None, "bench_return": None,
     "excess_return": None, "fill_status": None,
     "missing_reason": "缺日线"},
]
DB.research_outcomes_replace(eid, OUTS)
DB.research_outcomes_replace(eid, OUTS)
outs = DB.research_outcomes_of(eid)
ok(len(outs) == 3, "I29 outcomes 幂等：%d" % len(outs))
pend = next(x for x in outs if x["status"] == "PENDING")
ok(pend["theory_return"] is None and pend["excess_return"] is None
   and pend["bench_return"] is None, "I30 未完成观察期三个收益字段全为 NULL")
done = next(x for x in outs if x["status"] == "DONE")
ok(done["theory_return"] == 0.032 and done["excess_return"] == 0.021,
   "I31 已完成才有收益值")
ok(done["exec_return"] == 0.030, "I32 理论收益与可执行收益分开存")
nodata = next(x for x in outs if x["status"] == "NO_DATA")
ok(nodata["theory_return"] is None and nodata["missing_reason"] == "缺日线",
   "I33 缺数据必须有原因且不填 0")

# ---- 指标：upsert + 警告解码 ----
MET = [{"portfolio_rule": "top5", "horizon": 5, "valid_signals": 9,
        "valid_fills": 8, "avg_return": 0.012, "win_rate": 0.667,
        "pending_count": 1, "no_data_count": 1,
        "quality_notes": ["样本量 9 < 30，统计仅作描述性参考"]}]
DB.research_metrics_replace(eid, MET)
DB.research_metrics_replace(eid, MET)
mets = DB.research_metrics_of(eid)
ok(len(mets) == 1, "I34 metrics 按唯一键 upsert")
ok(mets[0]["quality_notes"] and "样本量" in mets[0]["quality_notes"][0],
   "I35 质量警告可回读")
ok(mets[0]["computed_at"], "I36 computed_at 已写入")

# ---- 股票池每日快照走 snapshot 表 ----
DB.save_one("universe", "20261015",
            {"note": "夹具：仅由真实可获得的数据生成"}, source="local")
ok(DB.list_dates("universe") == ["20261015"], "I37 universe 复用 snapshot 表")
ok("universe_snapshot" not in _tables_of(p_res), "I38 仍未多建表")

# ==== 9) 回测引擎：纯函数口径（阶段 3） ========================================
# 这一段只测算法，不碰库、不联网。所有数字都是**手算出来的期望值**，
# 不是拿实现跑一遍再抄回来当基准。
from app.research import engine as E                          # noqa: E402

# ---- J1~J7 板块涨跌幅（交易所规则事实，不是估计）----
ok(E.limit_ratio("600000") == 0.10, "J1 沪市主板 10%")
ok(E.limit_ratio("000001") == 0.10, "J2 深市主板 10%")
ok(E.limit_ratio("300001") == 0.20, "J3 创业板 20%")
ok(E.limit_ratio("688001") == 0.20, "J4 科创板 20%")
ok(E.limit_ratio("830001") == 0.30, "J5 北交所 8 段 30%")
ok(E.limit_ratio("430001") == 0.30, "J6 北交所 4 段 30%")
ok(E.limit_ratio(None) == 0.10 and E.limit_ratio("") == 0.10,
   "J7 脏代码回退主板而不是抛异常")

# ---- J8~J19 收益与成本：逐项算，缺值不填 0 ----
ok(E.theoretical_return(10.0, 11.0) == 0.1, "J8 理论收益 10%")
ok(E.theoretical_return(0.0, 11.0) is None, "J9 入场价非正 => None，不是 0")
ok(E.theoretical_return(None, 11.0) is None, "J10 缺入场价 => None")
ok(E.theoretical_return(11.0, None) is None, "J11 缺出场价 => None")

cost3 = dict(E.DEFAULT_COST)
big = E.apply_costs(10.0, 11.0, 10000.0, cost3)
ok(0 < big["net_return"] < E.theoretical_return(10.0, 11.0),
   "J12 扣费后必低于理论收益（%.6f < 0.1）" % big["net_return"])
ok(big["net_return"] is not None and abs(
    big["net_return"]
    - ((big["sell_amount"] - big["buy_amount"] - big["total_cost"])
       / big["buy_amount"])) < 1e-12,
   "J13 净收益 =（卖出实收 − 买入实付 − 分项成本）/ 买入实付")
ok(abs(big["commission"]
       - (big["buy_amount"] * cost3["commission_rate"]
          + big["sell_amount"] * cost3["commission_rate"])) < 1e-9,
   "J14 佣金买卖双向各计一次")
ok(abs(big["stamp"] - big["sell_amount"] * cost3["stamp_rate"]) < 1e-9,
   "J15 印花税只按卖出成交额计，不是双边都收")
tiny = E.apply_costs(10.0, 10.0, 1.0, cost3)      # 成交额仅约 10 元
ok(abs(tiny["commission"] - 2 * cost3["min_commission"]) < 1e-9,
   "J16 小额交易佣金取单边下限，不按 0.025% 算成几厘")
ok(tiny["stamp"] > 0, "J17 成交额再小印花税也是实际值，不归零")
big2 = E.apply_costs(10.0, 11.0, 100000.0, cost3)
ok(abs(big["net_return"] - big2["net_return"]) < 1e-9,
   "J18 线性费率下净收益与模拟本金无关（notional 只用于展示金额）")
st = E.apply_costs(10.0, 10.0, 10000.0, cost3)    # 价格不动
ok(st["net_return"] < 0, "J19 价格不动时扣成本必为负：成本不是可选项")

# ---- J20~J28 统计：空值不冒充 0 ----
ok(E.percentile([], 50) is None, "J20 空样本分位数为 None 而非 0")
ok(E.percentile([7.0], 99) == 7.0, "J21 单样本分位数即其自身")
ok(abs(E.percentile([1, 2, 3, 4], 50) - 2.5) < 1e-9, "J22 中位数线性插值")
ok(abs(E.percentile(list(range(1, 11)), 50) - 5.5) < 1e-9, "J23 p50 = 5.5")
s0 = E.summarize([])
ok(s0["count"] == 0 and all(s0[k] is None for k in
                            ("avg", "median", "win_rate", "p10", "p25",
                             "p75", "p90")),
   "J24 无有效样本时统计量全为 None，不得用 0 冒充")
ok(any("不可算" in x for x in s0["notes"]), "J25 空样本必须写明不可算")
s1 = E.summarize([0.01, -0.02, 0.03, None], pending=2, no_data=1)
ok(s1["count"] == 3, "J26 None 被剔除且不计成 0 收益")
ok(abs(s1["win_rate"] - 2 / 3) < 1e-9, "J27 胜率只对有效样本算")
ok(s1["pending"] == 2 and s1["no_data"] == 1, "J28 PENDING/NO_DATA 单独记账")

# ---- J29~J32 确定性并列裁决（研究层加，不改 _score）----
ties = [{"code": "600002", "score": 80.0},
        {"code": "600001", "score": 80.0},
        {"code": "300001", "score": 90.0},
        {"code": "000001", "score": 80.0}]
rk = E.rank_signals(ties)
ok([x["rank"] for x in rk] == [1, 2, 3, 4], "J29 rank 从 1 连续编号")
ok([x["code"] for x in rk] == ["300001", "000001", "600001", "600002"],
   "J30 同分按代码升序、高分优先：%s" % [x["code"] for x in rk])
ok([x["code"] for x in E.rank_signals(list(reversed(ties)))]
   == [x["code"] for x in rk],
   "J31 换掉池序结果不变（回测必须与预选顺序无关）")
ok(E.SORT_VERSION == "score-desc-code-asc-v1", "J32 排序规则有版本号可追")

# ---- J33~J44 replay_score 逐位复刻 mainforce._score 的聚合 ----
# 对照 `_score` 原式（mainforce.py:974-982）：
#   weighted = Σ series.fillna(0.0) * (w/total)   <- 缺维按 0 计入
#   coverage = Σ valid        * (w/total)         <- 单独记账
#   final    = weighted.where(coverage > 0)
# **不重归一化** —— 这是回测与线上对得上的关键，也是最容易被"贴心"改坏的地方。
used3 = {"a": 30.0, "b": 20.0, "c": 15.0}
sc, cv = E.replay_score({"dims": {"a": {"score": 100.0}, "b": {"score": 100.0},
                                  "c": {"score": 100.0}}}, used3)
ok(abs(sc - 100.0) < 1e-9 and abs(cv - 1.0) < 1e-9, "J33 全维 => 100 分 / 覆盖率 1")
sc, cv = E.replay_score({"dims": {"a": {"score": 100.0}, "b": {"score": 100.0}}},
                        used3)
exp35 = (100.0 * 30 + 100.0 * 20 + 0.0 * 15) / 65
ok(abs(sc - exp35) < 1e-9,
   "J34 缺维按 0 计入（实际 %.6f，手算 %.6f）" % (sc, exp35))
ok(abs(sc - 100.0) > 1.0,
   "J35 若重归一会虚高到 100 —— 这里必须不是 100，否则回测系统性乐观")
ok(abs(cv - 50.0 / 65.0) < 1e-9, "J36 覆盖率单独记账 = 50/65")
ok(E.replay_score({"dims": {}}, used3) == (None, 0.0),
   "J37 无任何维 => (None, 0.0)，不返回 0 分")
ok(E.replay_score({"dims": {"zzz": {"score": 50.0}}}, used3) == (None, 0.0),
   "J38 所有维都不在 used_weights 里 => 不算分（权重对不上就不硬凑）")
sc, cv = E.replay_score({"dims": {"a": {"score": 70.0, "weight": 30.0}}},
                        {"a": 30.0, "b": 20.0})
ok(abs(sc - 42.0) < 1e-9 and abs(cv - 0.6) < 1e-9,
   "J39 缺的那份权重按 0 计入：70×0.6 + 0×0.4 = 42")
ok(E.replay_score({"dims": {"a": {"score": 70.0}}}, {}) == (None, 0.0),
   "J40 权重全空 => 不给分，避免 0 分冒充真实结果")
# 真实快照抽样（2026-10-15 实库，code 600001 附近那条）：逐维 round(1)、
# 入库分 round(2)，重算偏差必须落在 REPLAY_TOL 内。
real_dims = {"inflow_consistency": {"score": 95.7, "weight": 30.0},
             "volume_up_price_flat": {"score": 95.7, "weight": 20.0},
             "price_position_low": {"score": 87.3, "weight": 15.0},
             "turnover_moderate": {"score": 62.4, "weight": 10.0},
             "chip_convergence": {"score": 27.4, "weight": 10.0}}
sc_real, cv_real = E.replay_score({"dims": real_dims},
                                  {k: v["weight"] for k, v in real_dims.items()})
ok(abs(sc_real - 82.23) <= E.REPLAY_TOL,
   "J41 真实快照重算 82.23 落在容差内（实际 %.4f，偏差 %.4f）"
   % (sc_real, abs(sc_real - 82.23)))
ok(cv_real > 0.99, "J42 该样本覆盖率接近 1（实际 %.4f）" % cv_real)
ok(E.REPLAY_TOL >= 0.05, "J43 容差至少要盖住逐维 round(1) 的 0.05")
ok(abs(cv_real - 1.0) < 1e-9,
   "J44 五维齐全 => 覆盖率恰好 1（%.4f），不是含糊的接近 1" % cv_real)

# ==== 10) build_outcome：成交 / 观察期 / 基准（阶段 3） =======================
def _bars_of(rows):
    b = {}
    for code, d, o, hi, lo, c in rows:
        b.setdefault(code, {})[d] = {"open": o, "high": hi, "low": lo, "close": c}
    return b


REF7 = ["20260105", "20260106", "20260107", "20260108",
        "20260109", "20260112", "20260113"]
BASE_BARS = _bars_of([
    ("600001", "20260105", 10.00, 10.10, 9.90, 10.00),
    ("600001", "20260106", 10.20, 11.00, 10.10, 10.80),   # 入场日
    ("600001", "20260107", 10.80, 10.90, 10.50, 10.70),
    ("600001", "20260108", 10.70, 10.75, 10.40, 10.60),   # T+3 出场日
])
THEORY_K = (10.60 - 10.20) / 10.20

o = E.build_outcome("20260105", "600001", 3, BASE_BARS, REF7, cost3, None)
ok(o["status"] == "DONE", "K1 观察期走完 => DONE")
ok(o["entry_date"] == "20260106" and o["exit_date"] == "20260108",
   "K2 口径：D+1 开盘入场、T+n 收盘出场")
ok(abs(o["theory_return"] - THEORY_K) < 1e-12 and o["entry_price"] == 10.20
   and o["exit_price"] == 10.60, "K3 理论收益按 (exit−entry)/entry 手算对齐")
ok(o["fill_status"] == "FILLED" and o["sell_status"] == "SELLABLE",
   "K4 两端都可成交")
ok(o["exec_return"] is not None and 0 < o["exec_return"] < o["theory_return"],
   "K5 可执行收益单列且低于理论收益")
ok(o["bench_return"] is None and o["excess_return"] is None
   and "基准" in (o["missing_reason"] or ""),
   "K6 缺基准 => 超额留 NULL 并写明原因，不填 0")
ok(o["data_quality"] == "PARTIAL", "K7 有缺失即 PARTIAL，不报 READY")
ok(o["mfe"] is not None and o["mfe"] > 0 and o["mae"] is not None
   and o["mae"] < 0, "K8 MFE/MAE 分别取窗口内最高/最低相对入场价")

op = E.build_outcome("20260105", "600001", 3, BASE_BARS, REF7, cost3,
                     {"20260106": 100.0, "20260108": 105.0})
ok(abs(op["bench_return"] - 0.05) < 1e-12
   and abs(op["excess_return"] - (op["theory_return"] - 0.05)) < 1e-12,
   "K9 基准按入场/出场**同日**对齐")
ok(op["data_quality"] == "READY", "K10 两端成交 + 基准齐全 => READY")

o = E.build_outcome("20260105", "600001", 3, BASE_BARS, REF7, cost3,
                    {"20260106": 100.0})
ok(o["bench_return"] is None and o["excess_return"] is None
   and "基准" in (o["missing_reason"] or ""),
   "K11 基准缺出场日 => 仍不可算，不拿半截数据硬凑")

o = E.build_outcome("20260105", "600001", 20, BASE_BARS, REF7, cost3, None)
ok(o["status"] == "PENDING", "K12 信号日之后交易日不够 => PENDING")
ok(all(o[k] is None for k in ("theory_return", "bench_return", "excess_return",
                              "entry_price", "exit_price", "exec_return")),
   "K13 PENDING 所有收益字段全为 NULL")
ok(o["fill_status"] == "NOT_DUE" and o["sell_status"] == "NOT_DUE",
   "K14 PENDING 不做成交判定（还没到那一步）")

ok(E.build_outcome("19990101", "600001", 3, BASE_BARS, REF7, cost3)["status"]
   == "NO_DATA", "K15 信号日不在交易日序列 => NO_DATA")
o = E.build_outcome("20260105", "600001", 3, BASE_BARS, [], cost3)
ok(o["status"] == "NO_DATA" and "交易日序列" in (o["missing_reason"] or ""),
   "K16 无日线 => NO_DATA 且写明原因（不是 0 收益）")

no_entry = _bars_of([r for r in [
    ("600001", "20260105", 10.00, 10.10, 9.90, 10.00),
    ("600001", "20260107", 10.80, 10.90, 10.50, 10.70),
    ("600001", "20260108", 10.70, 10.75, 10.40, 10.60)]])
o = E.build_outcome("20260105", "600001", 3, no_entry, REF7, cost3)
ok(o["status"] == "NO_DATA" and "无行情" in (o["missing_reason"] or ""),
   "K17 入场日缺行情（停牌）=> NO_DATA")

# 入场涨停：前收 10.00，主板涨停价 11.00 => 买不进，绝不默认成交
up_bars = _bars_of([
    ("600003", "20260105", 10.00, 10.10, 9.90, 10.00),
    ("600003", "20260106", 11.00, 11.00, 11.00, 11.00),
    ("600003", "20260107", 11.00, 11.10, 10.80, 10.90),
    ("600003", "20260108", 10.90, 11.00, 10.70, 10.80),
])
o = E.build_outcome("20260105", "600003", 3, up_bars, REF7, cost3)
ok(o["fill_status"] == "UNFILLABLE", "K18 开盘涨停 => UNFILLABLE")
ok(o["exec_return"] is None and o["theory_return"] is not None,
   "K19 理论收益照算，可执行收益必须留空")
ok("涨停" in (o["missing_reason"] or "") and o["sell_status"] == "NOT_DUE",
   "K20 原因写明涨停，且不再做卖出判定")
ok(o["data_quality"] == "PARTIAL", "K21 买不进 => 数据/可执行性 PARTIAL")

no_prev = _bars_of([r for r in [
    ("600001", "20260106", 10.20, 11.00, 10.10, 10.80),
    ("600001", "20260107", 10.80, 10.90, 10.50, 10.70),
    ("600001", "20260108", 10.70, 10.75, 10.40, 10.60)]])
o = E.build_outcome("20260105", "600001", 3, no_prev, REF7, cost3)
ok(o["fill_status"] == "UNCONFIRMED" and o["exec_return"] is None
   and "无法确认" in (o["missing_reason"] or ""),
   "K22 前收缺失 => UNCONFIRMED，不臆断买得进")

DN_ROWS = [
    ("600001", "20260105", 10.00, 10.10, 9.90, 10.00),
    ("600001", "20260106", 10.00, 10.05, 9.80, 10.00),
    ("600001", "20260107", 10.00, 10.05, 9.80, 10.00),
    ("600001", "20260108", 9.20, 9.50, 9.00, 9.00),       # 收在跌停价
]
dn_bars = _bars_of(DN_ROWS)
o = E.build_outcome("20260105", "600001", 3, dn_bars, REF7, cost3)
ok(o["fill_status"] == "FILLED" and o["sell_status"] == "LIMIT_DOWN",
   "K23 出场跌停 => LIMIT_DOWN")
ok(o["exec_return"] is None and "跌停" in (o["missing_reason"] or ""),
   "K24 卖不出 => 可执行收益留空并写明原因")

no_exit_prev = _bars_of([r for r in DN_ROWS if r[1] != "20260107"])
o = E.build_outcome("20260105", "600001", 3, no_exit_prev, REF7, cost3)
ok(o["sell_status"] == "UNCONFIRMED" and o["exec_return"] is None,
   "K25 出场前收缺失 => 卖出也不臆断")

# ==== 11) 引擎端到端（阶段 3，**夹具库**，非真实行情） =========================
# 夹具里的价格序列是测试构造的，只用来验证"口径是否被如实执行"，
# 不代表任何真实收益，也不会写进真实 market.db。
def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


USED5 = {"inflow_consistency": 30.0, "volume_up_price_flat": 20.0,
         "price_position_low": 15.0, "turnover_moderate": 10.0,
         "chip_convergence": 10.0}
ENG_DELTA = {"600001": 0.0, "600002": 10.0, "600003": -10.0}
ENG_LABEL = {"inflow_consistency": ("主力资金持续流入", "流入持续性"),
             "volume_up_price_flat": ("放量不涨", "放量不涨"),
             "price_position_low": ("股价位置低", "低位"),
             "turnover_moderate": ("换手适中", "换手适中"),
             "chip_convergence": ("筹码收敛", "筹码收敛")}
BASE_DIM = {"inflow_consistency": 80.0, "volume_up_price_flat": 70.0,
            "price_position_low": 60.0, "turnover_moderate": 50.0,
            "chip_convergence": 40.0}


def _eng_dims(code):
    """构造一只券的五维百分位分。

    前三只是**同向平移**的画像（delta +10 / 0 / −10），因此任何线性权重变化
    都不会改变它们的相对顺序 —— 那正好证明"消融不会凭空造出差异"。
    第四只 600004 是反例：只在「筹码收敛」一维强、其余都很弱，用来验证消融
    **确实**能改变排序，而不是一段永远跑不出差别的死代码。
    """
    if code == "600004":
        vals = {"inflow_consistency": 10.0, "volume_up_price_flat": 10.0,
                "price_position_low": 10.0, "turnover_moderate": 10.0,
                "chip_convergence": 95.0}
    else:
        d = ENG_DELTA[code]
        vals = {k: BASE_DIM[k] + d for k in USED5}
    dims, acc, wsum = {}, 0.0, 0.0
    for k, w in USED5.items():
        v = vals[k]
        dims[k] = {"label": ENG_LABEL[k][0], "short": ENG_LABEL[k][1],
                   "score": v, "weight": w}
        acc += v * w
        wsum += w
    return dims, round(acc / wsum, 2)


# 夹具股票池：前三只同向平移，第四只单维极端。顺序决定基线排名。
ENG_CODES = ["600001", "600002", "600003", "600004"]


def _eng_payload(date, bad_score=False):
    """夹具快照：逐维分与入库分**故意保持可对拍**（除非 bad_score）。"""
    rows = []
    for i, code in enumerate(ENG_CODES):
        dims, sc = _eng_dims(code)
        if bad_score:
            sc = round(sc + 5.0, 2)      # 故意漂移，用来验证"必须报警"
        rows.append({"code": code, "name": "夹具%s" % code[-1],
                     "score": sc, "rank": i + 1, "coverage": 1.0,
                     "price": 10.0, "industry": "制造业", "dims": dims})
    return json.dumps([{
        "date": date, "all_rows": rows, "rows": rows,
        "params": {"weights": dict(WEIGHTS)},
        "model": {"degraded": ["large_order_ratio"], "total_weight": 85.0,
                  "used_weights": dict(USED5), "coverage_mean": 1.0},
        "stats": {}, "elapsed_sec": 1.0,
        "generated_at": "%s-%s-%sT00:00:00" % (date[:4], date[4:6], date[6:]),
        "disclaimer": "夹具，仅用于测试",
    }], ensure_ascii=False)


def _mk_eng_db(name, bad_score=False):
    root = Path(tempfile.mkdtemp(prefix=name))
    path = root / "market.db"
    con = sqlite3.connect(path)
    con.executescript(SNAPSHOT_DDL)
    con.executescript(BAR_DDL)
    dates = _weekdays(datetime(2025, 12, 1), 80)
    for i, d in enumerate(dates):
        for code in ENG_CODES:
            if code == "600001":
                base = round(10.0 * (1.01 ** i), 2)
            elif code == "600002":
                base = 20.0
            elif code == "600004":
                base = 5.0
            else:
                base = 10.0
            c = round(base, 2)
            o = round(base * 0.995, 2)
            con.execute("INSERT INTO daily_bar VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (code, d, o, round(max(o, c) * 1.005, 2),
                         round(min(o, c) * 0.995, 2), c, 1e6, 1e8, 5.0, 0.5, 1.0))
    sig = [dates[10], dates[11], dates[-3]]
    for d in sig:
        con.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?)",
                    ("main_force", d, _eng_payload(d, bad_score),
                     len(ENG_CODES), "fixture",
                     "%s-%s-%sT02:00:00+00:00" % (d[:4], d[4:6], d[6:])))
    # 涨停夹具：600003 在首个信号日收 10.00，次日开盘 11.00 = 涨停价
    con.execute("UPDATE daily_bar SET open=11.00, high=11.00 WHERE code='600003'"
                " AND trade_date=?", (dates[11],))
    con.commit()
    con.close()
    return path, sig, dates


ENG_CFG = {"weights": dict(WEIGHTS), "horizons": [1, 3, 5, 10, 20],
           "portfolios": ["top5", "top10", "top30"],
           "cost": dict(E.DEFAULT_COST), "note": "阶段3端到端夹具"}

p_eng, sig_eng, dates_eng = _mk_eng_db("kanban_research_eng_")
settings.db_path = p_eng
DB._conn = None
DB.init_db()
eid_e, hash_e = DB.research_experiment_create(
    "阶段3端到端", "sixdim-v1", ENG_CFG, "current", sig_eng[0], sig_eng[-1])
rid_e = DB.research_run_create(eid_e)
res_e = E.BacktestEngine(eid_e, rid_e).run()

ok(res_e["ok"] is True and res_e["status"] == "done", "L1 引擎跑通：%s" % res_e)
run_e = DB.research_run_get(rid_e)
ok(run_e["progress"] == 100.0 and run_e["stage"] == "完成"
   and run_e["finished_at"], "L2 进度/阶段/完成时间都落库")
ok(run_e["status"] == "done" and not run_e["error_summary"],
   "L3 成功运行无错误")
ok(all(w is not None for w in run_e["warnings"]) and isinstance(run_e["warnings"], list),
   "L4 警告可解码回列表")
exp_e = DB.research_experiment_get(eid_e)
ok(exp_e["status"] == "done" and exp_e["finished_at"], "L5 实验状态同步为 done")
ok((exp_e["data_snapshot_id"] or "").startswith("main_force:"),
   "L6 数据版本已入档：%s" % exp_e["data_snapshot_id"])
ok(str(len(sig_eng)) in (exp_e["data_snapshot_id"] or ""),
   "L7 数据版本写明用了几个信号日")

sigs_e = DB.research_signals_of(eid_e)
ok(len(sigs_e) == 12, "L8 信号 3 日 × 4 只 = 12 条：%d" % len(sigs_e))
ok(all(s["model_version"] == "sixdim-v1" for s in sigs_e), "L9 模型版本入档")
by_date = {}
for s in sigs_e:
    by_date.setdefault(s["signal_date"], []).append(s)
ok(all(sorted(x["rank"] for x in v) == [1, 2, 3, 4]
       for v in by_date.values()), "L10 每个信号日 rank 1..4")
r1 = {x["code"]: x["rank"] for x in by_date[sig_eng[0]]}
ok(r1 == {"600002": 1, "600001": 2, "600003": 3, "600004": 4},
   "L11 基线按分数降序（600002 最高、单维极端的 600004 垫底）：%s" % r1)
BASE_RANKS = dict(r1)
ok(all(x["top5"] == 1 and x["top10"] == 1 and x["top30"] == 1 for x in sigs_e),
   "L12 三只都进 Top5/10/30")
ok(all(s["data_quality"] == "PARTIAL" for s in sigs_e),
   "L13 大单整维降级 => 每条信号如实标 PARTIAL")
ok(all(s["missing"] and "large_order_ratio" in s["missing"][0] for s in sigs_e),
   "L14 降级因子必须留痕")
f0 = sigs_e[0]["factors"]
ok("dims" in f0 and set(f0["dims"]) == set(USED5),
   "L15 逐维入档：%s" % sorted(f0["dims"]))
ok(f0["raw_available"] is False
   and all(v["raw"] is None for v in f0["dims"].values()),
   "L16 快照没有因子原始值 => raw 全 None，不编数")
ok("原始值" in (f0.get("raw_note") or ""), "L17 raw 缺口写进说明")
prov = f0["provenance"]
ok(prov["config_hash"] == hash_e and prov["sort_version"] == E.SORT_VERSION
   and prov["dataset"] == "main_force",
   "L18 信号可追溯到配置哈希/排序版本/数据集")
ok(prov["degraded"] == ["large_order_ratio"], "L19 降级清单入档")

outs_e = DB.research_outcomes_of(eid_e)
ok(len(outs_e) == 60, "L20 结果 12 信号 × 5 观察期 = 60：%d" % len(outs_e))
cnt = {}
for x in outs_e:
    cnt[x["status"]] = cnt.get(x["status"], 0) + 1
ok(cnt.get("DONE") == 44 and cnt.get("PENDING") == 16
   and cnt.get("NO_DATA") is None,
   "L21 完成 44 / 未走完 16 / 缺数据 0：%s" % cnt)
ok(all(x["bench_return"] is None and x["excess_return"] is None
       for x in outs_e),
   "L22 基准不落库 => 超额收益全 NULL，不是 0")
uf = next(x for x in outs_e if x["code"] == "600003"
          and x["signal_date"] == sig_eng[0] and x["horizon"] == 1)
ok(uf["fill_status"] == "UNFILLABLE" and uf["exec_return"] is None
   and uf["theory_return"] is not None,
   "L23 涨停买不进的那条：理论有、可执行无")
pend_e = [x for x in outs_e if x["status"] == "PENDING"]
ok(all(x["signal_date"] == sig_eng[-1] and x["horizon"] in (3, 5, 10, 20)
       for x in pend_e) and len(pend_e) == 16,
   "L24 只有最后一个信号日的长观察期会 PENDING")

mets_e = DB.research_metrics_of(eid_e)
ok(len(mets_e) == 15, "L25 指标 3 组合 × 5 观察期 = 15 行：%d" % len(mets_e))
m1 = next(m for m in mets_e if m["portfolio_rule"] == "top5" and m["horizon"] == 1)
m3 = next(m for m in mets_e if m["portfolio_rule"] == "top5" and m["horizon"] == 3)
ok(m1["valid_signals"] == 12 and m1["pending_count"] == 0,
   "L26 T+1 十二个样本全完成")
ok(m3["valid_signals"] == 8 and m3["pending_count"] == 4,
   "L27 T+3 只有 8 个完成、4 个未走完（不计成 0 收益）")
ok(m1["valid_fills"] == 11 and abs(m1["unfilled_rate"] - 1 / 12) < 1e-9,
   "L28 T+1 有一只涨停买不进 => 11 可成交 / 12 完成")
ok(m3["valid_fills"] == 7, "L29 T+3 同样剔除那一只买不进的")
ok(m1["avg_return"] is not None and m1["win_rate"] is not None
   and m1["trading_cost"] is not None,
   "L30 均值/胜率/成本都算得出来")
ok(m1["avg_excess"] is None and m1["median_excess"] is None,
   "L31 无基准 => 超额收益留 NULL")
ok(all(m["max_drawdown"] is None for m in mets_e),
   "L32 未构建组合净值曲线 => 回撤不填（不能拿个股收益排开算）")
ok(all(m["quality_notes"] for m in mets_e)
   and any("沪深300" in n for n in m1["quality_notes"])
   and any("显著性检验" in n for n in m1["quality_notes"])
   and any("净值曲线" in n for n in m1["quality_notes"]),
   "L33 质量说明必须自带：基准缺失/不做显著性/回撤为何为空")
warn_e = run_e["warnings"]
ok(any("原始值" in w for w in warn_e), "L34 因子原始值缺口有运行级警告")
ok(not any("综合分重算" in w for w in warn_e),
   "L35 逐维分与入库分对得上 => 不该有误报")

# ---- 对拍失配必须报警（而不是照单全收）----
p_bad, sig_bad, _ = _mk_eng_db("kanban_research_bad_", bad_score=True)
settings.db_path = p_bad
DB._conn = None
DB.init_db()
eid_b, _ = DB.research_experiment_create(
    "阶段3对拍失配", "sixdim-v1", ENG_CFG, "current", sig_bad[0], sig_bad[-1])
rid_b = DB.research_run_create(eid_b)
res_b = E.BacktestEngine(eid_b, rid_b).run()
ok(res_b["ok"] is True, "L36 数据质量问题报警但不中断整轮")
ok(any("综合分重算" in w for w in res_b["warnings"]),
   "L37 入库分与逐维分对不上 => 必须报警")
ok(len([w for w in res_b["warnings"] if "综合分重算" in w]) == 12,
   "L38 每条失配信号都要点名（12 条）")

# 回到正常夹具库。bad_score 那个库只用来验证"必须报警"；不切回来的话，
# 后续每一条实验 id 都从 1 重新开始，"复跑""消融"会**碰巧**读到同 id 的
# 另一条实验，静默变成在验证错的对象（N14 就是这么露馅的：读到了 +5 漂移的分）。
settings.db_path = p_eng
DB._conn = None
DB.init_db()
ok(DB.research_experiment_get(eid_e)["name"] == "阶段3端到端",
   "L38b 切库后先确认仍在正确的夹具库上，避免后续断言验证错对象")

# ---- 取消：不许留下半份结果 ----
eid_c, _ = DB.research_experiment_create(
    "阶段3取消", "sixdim-v1", ENG_CFG, "current", sig_bad[0], sig_bad[-1])
rid_c = DB.research_run_create(eid_c)
DB.research_run_cancel(rid_c)
res_c = E.BacktestEngine(eid_c, rid_c).run()
ok(res_c["ok"] is False and res_c["status"] == "cancelled", "L39 取消后返回 cancelled")
ok(DB.research_signals_of(eid_c) == [], "L40 取消不写半份信号")
ok(DB.research_metrics_of(eid_c) == [], "L41 取消不写指标（半成品不冒充结果）")
ok(DB.research_experiment_get(eid_c)["status"] == "cancelled", "L42 实验状态同步取消")

# ---- 区间内没有快照：失败要落到库里 ----
eid_x, _ = DB.research_experiment_create(
    "阶段3无快照", "sixdim-v1", ENG_CFG, "current", "20000101", "20000131")
rid_x = DB.research_run_create(eid_x)
res_x = E.BacktestEngine(eid_x, rid_x).run()
ok(res_x["ok"] is False and "main_force" in (res_x["error"] or ""),
   "L43 无快照 => failed 且原因明确：%s" % res_x["error"])
rr_x = DB.research_run_get(rid_x)
ok(rr_x["status"] == "failed" and rr_x["error_summary"] and rr_x["progress"] < 100,
   "L44 失败状态/原因落库，且不许把进度写成 100")
ok(DB.research_experiment_get(eid_x)["status"] == "failed"
   and DB.research_signals_of(eid_x) == [], "L45 失败实验无信号")

# ---- 完成后可复跑，且覆盖而非追加 ----
rid_e2 = DB.research_run_create(eid_e)
ok(rid_e2 > 0, "L46 已完成的实验可以再开一轮")
res_e2 = E.BacktestEngine(eid_e, rid_e2).run()
ok(res_e2["ok"] is True, "L47 复跑成功")
ok(len(DB.research_signals_of(eid_e)) == 12, "L48 复跑后信号仍是 12 条（覆盖）")
ok(len(DB.research_metrics_of(eid_e)) == 15, "L49 复跑后指标仍 15 行（upsert）")

# ==== 12) 股票池模式 A / B（阶段 4） ==========================================
ok(DB.UNIVERSE_MODE_CURRENT == "current"
   and DB.UNIVERSE_MODE_HISTORICAL == "historical", "M1 模式常量就位")
try:
    DB.research_experiment_create("模式B", "sixdim-v1", ENG_CFG,
                                  DB.UNIVERSE_MODE_HISTORICAL,
                                  sig_eng[0], sig_eng[-1])
    ok(False, "M2 模式 B 必须在建实验时就被拒")
except ValueError as exc:
    ok("幸存者偏差" in str(exc) and "BLOCKED" in str(exc),
       "M2 模式 B 被拒，且原因同时讲清幸存者偏差与 BLOCKED 状态")
try:
    DB.research_experiment_create("未知模式", "sixdim-v1", ENG_CFG,
                                  "whatever", sig_eng[0], sig_eng[-1])
    ok(False, "M3 未知股票池模式必须被拒")
except ValueError as exc:
    ok("未知的股票池模式" in str(exc), "M3 未知模式被拒且点名未知")
eid_m, _ = DB.research_experiment_create("模式A正常", "sixdim-v1", ENG_CFG,
                                         DB.UNIVERSE_MODE_CURRENT,
                                         sig_eng[0], sig_eng[-1])
ok(eid_m > 0, "M4 模式 A（当前股票池回看）正常创建")

# 引擎端二次拦截：即使库里被手工改成 historical，也不许跑完再标成 B
con = DB._connect()
con.execute("UPDATE research_experiments SET universe_mode='historical'"
            " WHERE id=?", (eid_m,))
con.commit()
rid_m = DB.research_run_create(eid_m)
res_m = E.BacktestEngine(eid_m, rid_m).run()
ok(res_m["ok"] is False and "幸存者偏差" in (res_m["error"] or ""),
   "M5 引擎二次拦截：遗留的 historical 行不许跑：%s" % res_m.get("error"))
ok(DB.research_run_get(rid_m)["status"] == "failed"
   and DB.research_signals_of(eid_m) == [], "M6 拦截结果落库且不产信号")
con.execute("UPDATE research_experiments SET universe_mode='current'"
            " WHERE id=?", (eid_m,))
con.commit()

ok(all(any("幸存者偏差" in n for n in m["quality_notes"]) for m in mets_e),
   "M7 幸存者偏差必须写进每一条指标，不能只躺在文档里")
ok(any("幸存者偏差" in w for w in run_e["warnings"]),
   "M8 每一轮运行都要挂上幸存者偏差警告")

# ==== 13) 因子消融（阶段 5） ==================================================
# 归零而不是重归一：与 _score 的缺维口径走同一条路（mainforce.py:979）。
# 一旦改成重归一，消融就会长出第二套聚合规则，结论既不可比也不可复现。
dw = E.drop_weights({"a": 30.0, "b": 20.0, "c": 15.0}, ["b"])
ok(dw == {"a": 30.0, "b": 0.0, "c": 15.0}, "N1 被消融的维归零，其余保持绝对值")
ok(sum(dw.values()) == 45.0, "N2 总权重下降而不是重归一（分母仍是原总和）")
ok(E.drop_weights({"a": 1.0}, []) == {"a": 1.0}, "N3 空消融 => 权重原样不动")

RW = [
    {"code": "600001", "snapshot_score": 65.88,
     "dims": {"a": {"score": 80.0}, "b": {"score": 20.0}}},
    {"code": "600002", "snapshot_score": 60.0,
     "dims": {"a": {"score": 20.0}, "b": {"score": 95.0}}},
    {"code": "600003", "snapshot_score": 55.0, "dims": {}},
]
rw = E.reweight_rank(RW, {"a": 30.0, "b": 20.0})
ok([x["code"] for x in rw] == ["600001", "600002"], "N4 全维权重下仍按分排序")
rw = E.reweight_rank(RW, {"a": 0.0, "b": 20.0})
ok([x["code"] for x in rw] == ["600002", "600001"],
   "N5 消融掉 a 之后，只在 b 上强的 600002 翻上来：%s"
   % [(x["code"], x["score"]) for x in rw])
ok(all(x.get("snapshot_score") is not None for x in rw),
   "N6 重排后仍带原始入库分供对照")
ok(E.BacktestEngine._weights_for({}, {"a": 1.0}) == {"a": 1.0},
   "N7 无消融配置 => 用快照原权重")
ok(E.BacktestEngine._weights_for(None, {"a": 1.0}) == {"a": 1.0}
   and E.BacktestEngine._weights_for("bad", {"a": 1.0}) == {"a": 1.0},
   "N8 ablation 为空或类型不对时退回原权重，不硬解析")

# ---- 端到端：只保留「筹码收敛」一维 ----
ABL_CFG = dict(ENG_CFG)
ABL_CFG["ablation"] = {
    "drop": ["inflow_consistency", "volume_up_price_flat",
             "price_position_low", "turnover_moderate"],
    "note": "只保留筹码收敛"}
eid_a, _ = DB.research_experiment_create(
    "阶段5消融-只留筹码收敛", "sixdim-v1", ABL_CFG, "current",
    sig_eng[0], sig_eng[-1])
rid_a = DB.research_run_create(eid_a)
res_a = E.BacktestEngine(eid_a, rid_a).run()
ok(res_a["ok"] is True, "N9 消融实验跑通：%s" % res_a.get("error"))
sigs_a = DB.research_signals_of(eid_a)
ok(len(sigs_a) == 12, "N10 消融实验信号数与基线相同")
ra = {x["code"]: x["rank"] for x in sigs_a
      if x["signal_date"] == sig_eng[0]}
ok(ra["600004"] == 1,
   "N11 只留筹码收敛 => 单维极端的 600004 冲到第一：%s" % ra)
ok(ra != BASE_RANKS, "N12 排序确实与基线不同（消融不是一段死代码）")
prov_a = next(s for s in sigs_a if s["code"] == "600004"
              and s["signal_date"] == sig_eng[0])["factors"]["provenance"]
ok(isinstance(prov_a["ablation"], dict)
   and "chip_convergence" in prov_a["ablation"]["weights"],
   "N13 实际使用的权重入档：%s" % prov_a["ablation"]["weights"])
ok(prov_a["snapshot_score"] is not None
   and abs(prov_a["snapshot_score"] - 20.0) < 1e-6,
   "N14 原始入库分保留（600004 基线分 %r，应为 20.0）可与基线逐条对照"
   % (prov_a["snapshot_score"],))
ok(not any("综合分重算" in w for w in res_a["warnings"]),
   "N15 消融实验不得拿消融后的分去和入库分对拍（必然不符，是假警报）")
ok(any("幸存者偏差" in w for w in res_a["warnings"]), "N16 幸存者偏差仍挂警告")
ok(len(DB.research_outcomes_of(eid_a)) == 60, "N17 消融实验结果数相同")
m_a = DB.research_metrics_of(eid_a)
ok(len(m_a) == 15 and all(any("因子消融" in n for n in m["quality_notes"])
                          for m in m_a),
   "N18 消融指标必须自带结论边界说明")
ok(all(any("边际贡献" in n for n in m["quality_notes"]) for m in m_a),
   "N19 明确写出不能宣称边际贡献")

# ---- weights 全量指定优先于 drop ----
WCFG = dict(ENG_CFG)
WCFG["ablation"] = {"weights": {"chip_convergence": 1.0},
                    "drop": ["inflow_consistency"]}   # 故意给两个，验优先级
eid_w, _ = DB.research_experiment_create(
    "阶段5权重优先级", "sixdim-v1", WCFG, "current", sig_eng[0], sig_eng[-1])
rid_w = DB.research_run_create(eid_w)
res_w = E.BacktestEngine(eid_w, rid_w).run()
ok(res_w["ok"] is True, "N20 权重指定实验跑通")
prov_w = next(s for s in DB.research_signals_of(eid_w)
              if s["code"] == "600001"
              and s["signal_date"] == sig_eng[0])["factors"]["provenance"]
ok(set(prov_w["ablation"]["weights"]) == {"chip_convergence"},
   "N21 全量 weights 优先于 drop：%s"
   % sorted(prov_w["ablation"]["weights"]))
ok(prov_w["ablation"]["drop"] == ["inflow_consistency"],
   "N22 drop 照原样留档，不静默丢弃")

# ==== 14) 未配置交易成本 —— "先空着"的口径 ===================================
# 没有业务方确认的费率就不许凭空扣一笔。代价是可执行收益全空，但理论收益
# 照常给出，且"没配成本"与"涨停买不进"必须分开记账。
NOCOST_CFG = {k: v for k, v in ENG_CFG.items() if k != "cost"}
eid_n, _ = DB.research_experiment_create(
    "未配成本实验", "sixdim-v1", NOCOST_CFG, "current", sig_eng[0], sig_eng[-1])
rid_n = DB.research_run_create(eid_n)
res_n = E.BacktestEngine(eid_n, rid_n).run()
ok(res_n["ok"] is True, "O1 未配置成本也要能跑完：%s" % res_n.get("error"))
outs_n = DB.research_outcomes_of(eid_n)
ok(len(outs_n) == 60 and all(x["exec_return"] is None for x in outs_n),
   "O2 未配置成本 => 可执行收益一律留空，不做扣费假设")
ok(any(x["theory_return"] is not None for x in outs_n),
   "O3 理论收益不受成本影响，照常给出")
mn1 = next(m for m in DB.research_metrics_of(eid_n)
           if m["portfolio_rule"] == "top5" and m["horizon"] == 1)
ok(mn1["valid_fills"] == 0 and mn1["trading_cost"] is None,
   "O4 有效可执行样本 = 0、成本不可算")
ok(abs(mn1["unfilled_rate"] - 1 / 12) < 1e-9,
   "O5 未配成本不许把 unfilled_rate 抬成 1（那等于说全员买不进）：%r"
   % mn1["unfilled_rate"])
ok(any("未配置" in n for n in mn1["quality_notes"]),
   "O6 质量说明点明是「没配成本」而不是「买不进」")
ok(any("未配置交易成本" in w for w in res_n["warnings"]), "O7 运行警告也点明")

# 配了成本的那一轮必须反过来：警告写明这是研究假设
ok(any("研究假设" in w for w in res_e["warnings"]),
   "O8 配了成本就必须标明是研究假设，不是实际券商费率")

# ==== 15) 研究 API 端点（阶段 7） =============================================
from fastapi import HTTPException                          # noqa: E402

RESEARCH_PATHS = {
    "/api/research/capabilities", "/api/research/audit",
    "/api/research/experiments",
    "/api/research/experiments/{eid}",
    "/api/research/experiments/{eid}/run",
    "/api/research/experiments/{eid}/runs",
    "/api/research/experiments/{eid}/signals",
    "/api/research/experiments/{eid}/outcomes",
    "/api/research/experiments/{eid}/metrics",
    "/api/research/runs/{rid}",
    "/api/research/runs/{rid}/cancel",
}
have = {getattr(x, "path", "") for x in R.router.routes}
ok(RESEARCH_PATHS <= have,
   "P1 11 条研究路径全部注册：%s" % sorted(RESEARCH_PATHS - have))

caps = R.research_capabilities()
ok(caps["horizons"] == [1, 3, 5, 10, 20] and caps["portfolios"]
   == ["top5", "top10", "top30"], "P2 可选项来自引擎常量而非前端写死")
ok(caps["sort_version"] == E.SORT_VERSION, "P3 排序规则版本一并给出")
ok(caps["universe_modes"]["historical"]["enabled"] is False
   and "幸存者偏差" in caps["universe_modes"]["historical"]["reason"],
   "P4 模式 B 禁用且**当场给出理由**，不是灰掉不解释")
ok(caps["universe_modes"]["current"]["enabled"] is True
   and "幸存者偏差" in caps["universe_modes"]["current"]["caveat"],
   "P5 模式 A 同样带上它的固有偏差")
ok("研究假设" in caps["cost_preset"]["note"]
   and caps["cost_preset"]["values"]["assumption"] is True,
   "P6 成本预设自带「研究假设」标记")
ok("full_six_dim_backtest" in caps["blocked"] and "日线不落库" in
   caps["blocked"]["full_six_dim_backtest"],
   "P7 能力清单必须写清什么做不了、为什么")

created = R.research_experiment_create(R.ResearchCreateBody(
    name="阶段7API实验", config=ENG_CFG,
    start_date=sig_eng[0], end_date=sig_eng[-1]))
eid_p = created["experiment"]["id"]
ok(eid_p > 0 and created["config_hash"], "P8 建实验返回 id 与配置哈希")

try:
    R.research_experiment_create(R.ResearchCreateBody(
        name="模式B", config=ENG_CFG, universe_mode="historical",
        start_date=sig_eng[0], end_date=sig_eng[-1]))
    ok(False, "P9 模式 B 必须 400")
except HTTPException as exc:
    ok(exc.status_code == 400 and "幸存者偏差" in str(exc.detail),
       "P9 模式 B 的拒绝理由原样回给调用方")

try:
    R.research_experiment_create(R.ResearchCreateBody(
        name="倒挂", config=ENG_CFG,
        start_date=sig_eng[-1], end_date=sig_eng[0]))
    ok(False, "P10 日期倒挂必须 400")
except HTTPException as exc:
    ok(exc.status_code == 400, "P10 日期倒挂在入口拦下")

try:
    R.research_experiment_detail(999999)
    ok(False, "P11 不存在的实验必须 404")
except HTTPException as exc:
    ok(exc.status_code == 404, "P11 不存在的实验返回 404")

det = R.research_experiment_detail(eid_p)
ok(det["experiment"]["id"] == eid_p
   and det["experiment"]["config_snapshot"]["weights"] == WEIGHTS
   and det["runs"] == [], "P12 详情带出冻结配置与运行历史")

rid_p = DB.research_run_create(eid_p)          # 先手工占一个未完成运行
try:
    R.research_experiment_run(eid_p)
    ok(False, "P13 重复启动必须 409")
except HTTPException as exc:
    ok(exc.status_code == 409 and "已有未完成" in str(exc.detail),
       "P13 重复启动返回 409 而不是静默多开一条")
DB.research_run_cancel(rid_p)

started = R.research_experiment_run(eid_p)
rid_p2 = started["run"]["id"]
ok(rid_p2 > 0 and rid_p2 != rid_p, "P14 取消后可再次启动")
ok(len(R.research_experiment_runs(eid_p)["items"]) == 2, "P15 运行历史可回读")
ok(R.research_run_detail(rid_p2)["run"]["id"] == rid_p2, "P16 运行状态可轮询")

# 取消单独用一条手工开的运行：后台线程可能已经跑完了，
# 拿它验取消就是和线程赛跑，红了也说不清是谁的问题。
eid_c7, _ = DB.research_experiment_create("阶段7取消", "sixdim-v1", ENG_CFG,
                                          "current", sig_eng[0], sig_eng[-1])
rid_c7 = DB.research_run_create(eid_c7)
cancelled = R.research_run_cancel(rid_c7)
ok(cancelled["cancelled"] is True
   and cancelled["run"]["status"] == "cancelled", "P17 取消立即反映在状态上")
ok(R.research_run_cancel(rid_c7)["cancelled"] is False, "P17b 已取消不可再取消")

# 分页与过滤：结果行数 = 信号数 × 观察期，不加分页会把整条链拖慢。
# 直接调路由函数时 FastAPI 不会注入 Query 默认值，所以带 Query 的参数
# 必须显式传全 —— 否则拿到的是 Query 哨兵对象本身。
ok(len(R.research_experiment_signals(eid_e, limit=5, offset=0)["items"]) == 5
   and R.research_experiment_signals(eid_e, limit=5, offset=0)["has_more"]
   is True, "P18 信号分页且 has_more 如实回报")
ok(R.research_experiment_signals(eid_e, limit=1000, offset=0)["has_more"]
   is False, "P19 取得够多时 has_more 转 False")
oc = R.research_experiment_outcomes(eid_e, horizon=5, limit=1000)
ok(all(x["horizon"] == 5 for x in oc["items"]) and len(oc["items"]) == 12,
   "P20 结果按观察期过滤：%d" % len(oc["items"]))
ok(R.research_experiment_outcomes(eid_e, horizon=None, limit=10)["truncated"]
   is True, "P21 截断必须显式回报，不静默丢数据")
mtr = R.research_experiment_metrics(eid_e)["items"]
ok(len(mtr) == 15 and all(m["quality_notes"] for m in mtr),
   "P22 指标端点把质量说明和数字一起返回")

# ==== 16) 实验删除：级联清关联数据（2026-10-18） ==============================
# 删实验 = 运行/信号/结果/指标一次事务删干净（关联表的 REFERENCES 是摆设，
# SQLite 默认不开外键，级联只能自己按先子后父删）。有未完成的运行先拒，
# 别删到一半被后台引擎写回来，留下查不到父行的孤儿数据。
ok(any(getattr(x, "path", "") == "/api/research/experiments/{eid}"
       and "DELETE" in (getattr(x, "methods", None) or set())
       for x in R.router.routes),
   "Q1 DELETE 实验端点已注册（与 GET 详情同路径、不同方法）")

# 对照组：与被删的实验同时建，全程不点名删除 —— 最后一行 Q10 靠它验隔离
eid_k, _ = DB.research_experiment_create("删除时留下的", "sixdim-v1", ENG_CFG,
                                          "current", sig_eng[0], sig_eng[-1])
DB.research_signals_replace(eid_k, None, [
    {"signal_date": sig_eng[0], "code": "600001", "model_version": "m1",
     "factors": {}}])

try:
    R.research_experiment_delete(999999)
    ok(False, "Q2 删不存在的实验必须 404")
except HTTPException as exc:
    ok(exc.status_code == 404, "Q2 删不存在的实验返回 404")

# 有 pending 运行 → 409，且**一行都不删**
eid_d, _ = DB.research_experiment_create(
    "删除用", "sixdim-v1", ENG_CFG, "current", sig_eng[0], sig_eng[-1])
rid_d = DB.research_run_create(eid_d)
try:
    R.research_experiment_delete(eid_d)
    ok(False, "Q3 有未完成的运行必须 409")
except HTTPException as exc:
    ok(exc.status_code == 409 and "取消" in str(exc.detail),
       "Q3 未完成运行拒绝删除并告知先取消")
ok(DB.research_experiment_get(eid_d) is not None
   and len(DB.research_runs_of(eid_d)) == 1,
   "Q4 被拒的删除一行都不许少")

# 取消后可删；插入已知行数的四张子表，核对逐表计数
DB.research_run_cancel(rid_d)
DB.research_signals_replace(eid_d, rid_d, [
    {"signal_date": sig_eng[0], "code": "600001", "model_version": "m1",
     "factors": {}},
    {"signal_date": sig_eng[1], "code": "600002", "model_version": "m1",
     "factors": {}}])
DB.research_outcomes_replace(eid_d, [
    {"signal_date": sig_eng[0], "code": "600001", "horizon": 5,
     "status": "DONE"}])
DB.research_metrics_replace(eid_d, [
    {"portfolio_rule": "top5", "horizon": 5, "valid_signals": 2,
     "valid_fills": 0, "pending_count": 0, "no_data_count": 0}])
out_d = R.research_experiment_delete(eid_d)["deleted"]
ok(out_d == {"experiment": 1, "runs": 1, "signals": 2, "outcomes": 1,
             "metrics": 1}, "Q5 级联逐表计数如实：%s" % out_d)
ok(DB.research_experiment_get(eid_d) is None
   and DB.research_runs_of(eid_d) == []
   and DB.research_signals_of(eid_d) == []
   and DB.research_outcomes_of(eid_d) == []
   and DB.research_metrics_of(eid_d) == [],
   "Q6 五张表对该实验一查即空")

# 拿真实引擎产出（eid_e，两轮**同步**运行、无后台线程）验整批数据
n_sig = len(DB.research_signals_of(eid_e, limit=9999))
n_oc = len(DB.research_outcomes_of(eid_e, limit=9999))
n_mt = len(DB.research_metrics_of(eid_e))
n_rn = len(DB.research_runs_of(eid_e))
ok(n_sig > 0 and n_oc > 0 and n_mt > 0 and n_rn > 0,
   "Q7 删前引擎产出四表都有货（信号%d 结果%d 指标%d 运行%d）"
   % (n_sig, n_oc, n_mt, n_rn))
out_e = R.research_experiment_delete(eid_e)["deleted"]
ok(out_e == {"experiment": 1, "runs": n_rn, "signals": n_sig,
             "outcomes": n_oc, "metrics": n_mt},
   "Q8 真实回测数据全级联，报的行数与删前数得的一致")
ok(DB.research_experiment_get(eid_e) is None
   and DB.research_runs_of(eid_e) == []
   and DB.research_signals_of(eid_e) == []
   and DB.research_outcomes_of(eid_e) == []
   and DB.research_metrics_of(eid_e) == [],
   "Q9 真实实验删后五表清空")

# 对照组：与 eid_d 同时建、从没被点名删过 —— 上面两轮删除后必须毫发无损
ok(DB.research_experiment_get(eid_k) is not None
   and len(DB.research_signals_of(eid_k)) == 1,
   "Q10 只删点名的实验，对照组数据不动")

# ==== 收尾 ===================================================================
ok(not _BLOCKED, "H1 一次外发都不许有：%s" % _BLOCKED[:5])

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("PASS=%d FAIL=%d" % (PASS, len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
