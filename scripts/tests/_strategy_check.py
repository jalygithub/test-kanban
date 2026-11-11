# -*- coding: utf-8 -*-
"""自动买卖策略 V1 —— **离线**回归（docs/ai_trading_lab/）。

覆盖 `app/trading/`（params / strategy / risk / paper / service）与 7 张交易表，
逐条盯住任务书第六条的十个测试要求：

  1) 满足条件时产生买入信号（数量/仓位/版本字段齐全）
  2) 不满足条件时不产生买入（ST / 缺行情 / 趋势 / 流动性 / 榜外 / 重复）
  3) 固定止损、移动止盈、趋势退出、最大持仓日（逐条独立记因；没触发留 HOLD）
  4) 可用现金不足与仓位超限（策略层 / 风控层 / 撮合层三处各钉一次）
  5) 停牌 / 涨跌停 / 风险状态（涨停买不进、跌停卖不出、缺行情不伪造成交）
  6) A 股 T+1（当日买入锁定、当日卖出拒绝、次日解锁可卖）
  7) 行情缺失或过期拒绝交易（快照缺 / 日期不符 / 下一交易日定不了 / 执行日无行情）
  8) 重复运行不重复下单、不重复记账（run 唯一键 / signal_id UNIQUE /
     sim_fill.order_id UNIQUE 三层各验一次）
  9) 历史回放无未来数据泄漏（bars_load_window 上界 + 行为级 + 源码级）
 10) 既有六维选股与回测模块不受影响（冒烟；全量回归另行覆盖）

夹具日期刻意取 **2027 年**（真实日历只到 20261231），并把
`service.cached_trade_dates` 换成受控列表 —— 日历缓存的新鲜度进不了断言。
全离线：开头封死 TCP + DNS，收尾断言零漏出。

跑法：
  D:\\test-kanban\\backend\\.venv\\Scripts\\python.exe D:\\test-kanban\\scripts\\tests\\_strategy_check.py
"""
import ast
import json
import re
import socket
import sqlite3
import sys
import tempfile
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
from app.metrics import mainforce                    # noqa: E402
from app.pipeline import ingest                       # noqa: E402
from app.research import engine                       # noqa: E402
from app.store import db                              # noqa: E402
from app.trading import params as pmod                # noqa: E402
from app.trading import paper, risk, strategy         # noqa: E402
from app.trading import service as tsvc               # noqa: E402

PASS = 0
FAILS = []


def ok(cond, label):
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)
        print("FAIL: %s" % label)


def _dates_in(text):
    r"""抽出文本里真正形如 yyyymmdd 的日期。

    裸 `\d{8}` 会把 8 位金额（如 min_amount 50000000）和 9 位数的截段
    （1e8 = 100000000）误判成日期 —— 所以先收边界，再验月/日合法。
    """
    months = {"01", "02", "03", "04", "05", "06",
              "07", "08", "09", "10", "11", "12"}
    out = []
    for tok in re.findall(r"(?<!\d)\d{8}(?!\d)", str(text)):
        if tok[4:6] in months and 1 <= int(tok[6:]) <= 31:
            out.append(tok)
    return out


# ---- 受控交易日历：真实缓存文件不许进断言 -----------------------------------
DAYS = ["20270104", "20270105", "20270106", "20270107", "20270108",
        "20270111", "20270112", "20270113", "20270114", "20270115"]
tsvc.cached_trade_dates = lambda: list(DAYS)

# 主流程参数：窗口只有 10 个交易日，把 MA / 持仓日拉到夹具窗口内。
P_MAIN = {"trend_ma": 3, "exit_trend_ma": 3, "max_hold_days": 3,
          "max_positions": 10}
CAND_0107 = [
    {"code": "600001", "name": "演示电子", "rank": 1, "score": 80.0,
     "price": 10.30},
    {"code": "600002", "name": "僵尸科技", "rank": 2, "score": 78.0,
     "price": 5.0},
    {"code": "600003", "name": "ST演示", "rank": 3, "score": 77.0,
     "price": 5.0},
    {"code": "600004", "name": "缺行情股", "rank": 4, "score": 76.0,
     "price": 5.0},
    {"code": "600005", "name": "阴跌科技", "rank": 5, "score": 75.0,
     "price": 17.0},
    {"code": "600008", "name": "榜外第六", "rank": 6, "score": 74.0,
     "price": 5.0},
]


# ---- 夹具构造 ---------------------------------------------------------------
def _mk_db(name):
    root = Path(tempfile.mkdtemp(prefix=name))
    path = root / "market.db"
    con = sqlite3.connect(path)
    con.executescript(db._SCHEMA)
    con.executescript(db._SCHEMA)      # 幂等：整套 schema 跑两遍不许报错
    con.commit()
    con.close()
    settings.db_path = path
    db._conn = None                    # 换库必须断开单例连接
    return path


def _bar(code, d, close, open_=None, high=None, low=None, amount=1e8):
    o = close if open_ is None else open_
    h = max(o, close) + 0.1 if high is None else high
    lo = min(o, close) - 0.1 if low is None else low
    return (code, d, o, h, lo, close, 1000000.0, amount, 1.0, 0.0, 1.0,
            "fixture", "2027-01-07T00:00:00+00:00")


def _ins_bars(rows):
    # 暖机日 20270101：流动性闸门要 5 日完整成交额窗口，而信号日最早只到
    # 0107（向前 4 个交易日）—— 每个码复制首根行情当暖机，600002 的 1e6
    # 低量随之保留（流动性判据不变）。
    warm = []
    seen = set()
    for r in rows:
        if r[0] in seen:
            continue
        seen.add(r[0])
        warm.append((r[0], "20270101", r[2], r[3], r[4], r[5], r[6], r[7],
                     r[8], r[9], r[10], r[11], r[12]))
    con = sqlite3.connect(settings.db_path)
    con.executemany(
        "INSERT OR REPLACE INTO daily_bar(code,trade_date,open,high,low,"
        "close,volume,amount,turnover,pct_chg,adj_factor,source,fetched_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", warm + list(rows))
    con.commit()
    con.close()


def _snapshot(day, rows, envelope_date=None):
    db.save_one("main_force", day, {
        "date": envelope_date or day, "generated_at": "2027-01-07T00:00:00",
        "rows": rows, "params": {}, "model": {}, "disclaimer": "fixture"})


def _ins_positions(items):
    con = sqlite3.connect(settings.db_path)
    con.executemany(
        "INSERT OR REPLACE INTO sim_position(code,name,qty,locked_qty,"
        "locked_date,avg_cost,cost_factor,entry_date,updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)", items)
    con.commit()
    con.close()


def _ins_ingest(domain, day, status):
    con = sqlite3.connect(settings.db_path)
    con.execute("INSERT INTO ingest_job(domain,trade_date,status,updated_at)"
                " VALUES (?,?,?,?)",
                (domain, day, status, "2027-01-07T00:00:00+00:00"))
    con.commit()
    con.close()


def _build_gen_db(name):
    """主生成夹具：好票/僵尸/ST/缺行情/阴跌/榜外 + 6 个持仓。"""
    _mk_db(name)
    rows = []
    rows += [_bar("600001", d, c) for d, c in zip(DAYS, [
        10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70, 10.80,
        10.90])]
    rows += [_bar("600002", d, 5.00, amount=1e6) for d in DAYS]
    rows += [_bar("600003", d, 5.00) for d in DAYS]
    rows += [_bar("600004", d, 5.00) for d in DAYS[:3]]      # 0107 起缺行情
    rows += [_bar("600005", d, c) for d, c in zip(DAYS, [
        20.0, 19.0, 18.0, 17.0, 16.0, 16.0, 16.0, 16.0, 16.0, 16.0])]
    rows += [_bar("600008", d, 5.00) for d in DAYS]
    rows += [_bar("600009", d, 12.0) for d in DAYS if d != "20270107"]
    rows += [_bar("600010", d, c) for d, c in zip(DAYS, [
        20.00, 19.80, 19.00, 18.00, 18.10, 18.10, 18.10, 18.10, 18.10,
        18.10])]
    rows += [_bar("600011", d, c) for d, c in zip(DAYS, [
        10.00, 10.00, 10.20, 10.70, 10.75, 10.75, 10.75, 10.75, 10.75,
        10.75])]
    rows += [_bar("600012", d, c) for d, c in zip(DAYS, [
        15.20, 15.00, 14.60, 14.00, 14.05, 14.05, 14.05, 14.05, 14.05,
        14.05])]
    rows += [_bar("600013", d, 15.00) for d in DAYS]
    rows += [_bar("600014", d, c) for d, c in zip(DAYS, [
        15.0, 15.0, 15.1, 15.2, 15.2, 15.2, 15.2, 15.2, 15.2, 15.2])]
    # 执行日（0108）专用开盘价：撮合按开盘，卖单在这些价上成交
    rows.append(_bar("600010", "20270108", 18.10, open_=17.95, high=18.2,
                     low=17.90))
    rows.append(_bar("600011", "20270108", 10.75, open_=10.60, high=10.8,
                     low=10.55))
    rows.append(_bar("600011", "20270106", 10.20, high=12.00))  # 峰值
    rows.append(_bar("600012", "20270108", 14.05, open_=13.90, high=14.1,
                     low=13.85))
    rows.append(_bar("600013", "20270108", 15.00, open_=14.95, high=15.05,
                     low=14.90))
    _ins_bars(rows)
    _snapshot("20270107", [dict(r) for r in CAND_0107])
    _snapshot("20270108", [dict(r) for r in CAND_0107])
    _ins_positions([
        ("600010", "持仓止损", 1000, 0, None, 20.0, 1.0, "20270104", "T"),
        ("600011", "持仓止盈", 1000, 0, None, 10.0, 1.0, "20270105", "T"),
        ("600012", "持仓趋势", 500, 0, None, 15.0, 1.0, "20270105", "T"),
        ("600013", "持仓超期", 500, 0, None, 15.0, 1.0, "20270104", "T"),
        ("600014", "持仓安好", 200, 0, None, 15.0, 1.0, "20270105", "T"),
        ("600009", "持仓缺行情", 300, 0, None, 12.0, 1.0, "20270105", "T"),
    ])
    db.sim_account_init(1_000_000.0)
    return settings.db_path


# ==== 1) 参数与版本（A 段）==================================================
d = pmod.normalize()
ok(d["candidate_top_n"] == 5 and d["stop_loss_pct"] == 0.08,
   "A1 默认参数可归一化")
ok(pmod.DEFAULT_PARAMS["trend_ma"] == 20 and d is not pmod.DEFAULT_PARAMS,
   "A2 normalize 不许改到 DEFAULT 本体")
d["trend_ma"] = 99
ok(pmod.DEFAULT_PARAMS["trend_ma"] == 20, "A3 改返回值不影响默认表")
try:
    pmod.normalize({"candidate_topn": 3})
    ok(False, "A4 未知键必须拒绝")
except ValueError as exc:
    ok("candidate_topn" in str(exc), "A4 未知键必须拒绝（报出键名）")
for bad in ({"stop_loss_pct": 0.9}, {"candidate_top_n": 2.5},
            {"trend_ma": 1}, {"allow_pyramid": 1}):
    try:
        pmod.normalize(bad)
        ok(False, "A5 非法取值必须拒绝: %r" % bad)
    except ValueError:
        ok(True, "A5 非法取值必须拒绝: %r" % bad)
try:
    pmod.normalize({"max_position_pct": 0.9, "max_total_exposure": 0.5})
    ok(False, "A6 单股上限 > 总上限必须拒绝")
except ValueError:
    ok(True, "A6 单股上限 > 总上限必须拒绝")
v1 = pmod.params_version({"stop_loss_pct": 0.08})
ok(v1 == pmod.params_version({}) and len(v1) == 12,
   "A7 参数版本 = 归一化内容哈希（默认值显式写出同版本）")
ok(pmod.params_version({"stop_loss_pct": 0.09}) != v1, "A8 改一个参数换版本")
ok(json.loads(pmod.params_json())["trail_pct"] == 0.10, "A9 参数快照可回读")

# ==== 2) 风控层纯单测（B 段）=================================================
ACC = {"cash": 1e6, "available": 1e6, "equity": 1e6, "exposure": 0.0,
       "position_count": 0, "positions": {}, "pending_buy": set(),
       "risk_state": "NORMAL", "exec_date": "20270108"}
PD = pmod.normalize()
S_BUY = {"code": "600001", "side": "BUY", "target_qty": 300,
         "est_price": 10.0}
S_SELL = {"code": "600020", "side": "SELL", "target_qty": 300,
          "est_price": 10.0}
ok(risk.review(S_BUY, ACC, PD)["approved"] is True, "B1 合法买单通过")
ok(risk.review({**S_BUY, "side": "HOLD"}, ACC, PD)["rule"] == "not_orderable",
   "B2 HOLD 不产生订单")
ok(risk.review({**S_BUY, "target_qty": None}, ACC, PD)["rule"] == "invalid_qty",
   "B3 数量缺失拒绝")
ok(risk.review({**S_BUY, "target_qty": 150}, ACC, PD)["rule"] == "lot_size",
   "B4 非整百拒绝")
ok(risk.review({**S_BUY, "est_price": None}, ACC, PD)["rule"]
   == "invalid_price", "B5 参考价缺失拒绝")
ok(risk.review(S_BUY, {**ACC, "risk_state": "RISK"}, PD)["rule"] == "risk_gate",
   "B6 风险状态禁新开仓")
held = {**ACC, "positions": {"600001": {"qty": 100, "locked_qty": 0,
                                        "entry_date": "20270104"}},
        "position_count": 1}
ok(risk.review(S_BUY, held, PD)["rule"] == "duplicate_position",
   "B7 已持仓不重复买入")
ok(risk.review(S_BUY, {**ACC, "pending_buy": {"600001"}}, PD)["rule"]
   == "duplicate_order", "B8 有在途买单不重复下单")
ok(risk.review(S_BUY, {**ACC, "position_count": 5}, PD)["rule"]
   == "position_cap", "B9 持仓数达上限拒绝")
ok(risk.review({**S_BUY, "target_qty": 2000}, {**ACC, "exposure": 0.79}, PD)
   ["rule"] == "exposure_cap", "B10 总仓位超限拒绝")
ok(risk.review({**S_BUY, "target_qty": 25000}, ACC, PD)["rule"]
   == "position_pct_cap", "B11 单股仓位超限拒绝")
ok(risk.review(S_BUY, {**ACC, "available": 1000.0}, PD)["rule"]
   == "cash_insufficient", "B12 可用现金不足拒绝")
ok(risk.review(S_SELL, ACC, PD)["rule"] == "no_position", "B13 无持仓不可卖")
pos60020 = {"600020": {"qty": 300, "locked_qty": 0,
                       "entry_date": "20270104"}}
ok(risk.review(S_SELL, {**ACC, "positions": pos60020}, PD)["approved"] is True,
   "B14 合法卖单通过")
ok(risk.review({**S_SELL, "target_qty": 500},
               {**ACC, "positions": pos60020}, PD)["rule"] == "over_position",
   "B15 卖出量超过持有量拒绝")
ok(risk.review(S_SELL, {**ACC, "positions": {"600020": {
    "qty": 300, "locked_qty": 300, "entry_date": "20270104"}}}, PD)["rule"]
   == "t1_locked", "B16 T+1 冻结量拒绝")
ok(risk.review(S_SELL, {**ACC, "positions": {"600020": {
    "qty": 300, "locked_qty": 0, "entry_date": "20270108"}}}, PD)["rule"]
   == "t1_same_day", "B17 入场日当日卖出拒绝")

# ==== 3) 涨跌停规则复用（C 段）==============================================
ok(engine.limit_ratio("600001") == 0.1, "C1 主板 10%")
ok(engine.limit_ratio("300001") == 0.2 and engine.limit_ratio("688001") == 0.2,
   "C2 创业板/科创板 20%")
ok(engine.limit_ratio("830001") == 0.3, "C3 北交所 30%")
ok(engine._at_limit(11.0, 10.0, "600001", up=True) is True, "C4 涨停命中")
ok(engine._at_limit(10.5, 10.0, "600001", up=True) is False, "C5 未涨停")
ok(engine._at_limit(9.0, 10.0, "600001", up=False) is True, "C6 跌停命中")
ok(engine._at_limit(10.0, None, "600001", up=True) is None,
   "C7 前收缺失判不了不臆断")
ok(paper.limit_ratio_of("300001") == 0.2, "C8 撮合层引用同一规则实现")

# ==== 4) DB1 主流程：行情 → 信号 → 风控 → 订单（D 段）=======================
_build_gen_db("kanban_strategy_gen_")
res1 = tsvc.run_signals("20270107", P_MAIN)
ok(res1["status"] == "OK" and res1["idempotent"] is False, "D1 首次运行成功")
ok(res1["signal_date"] == "20270107" and res1["exec_date"] == "20270108",
   "D2 信号日 0107 / 执行日 = 下一交易日 0108")
ok(res1["risk_state"] == "NORMAL" and res1["quality_state"] == "ok",
   "D3 无 ingest 记录 = 正常态（没采过不算失败）")
ok(res1["signals"] == {"BUY": 1, "SELL": 4, "HOLD": 6}, "D4 信号构成")
ok(res1["orders"] == {"placed": 5, "risk_rejected": 0}, "D5 风控放行 5 单")
run_id = res1["run_id"]
sigs = {s["code"]: s for s in tsvc.signals(run_id)}
ok(set(sigs) == {"600001", "600002", "600003", "600004", "600005",
                 "600010", "600011", "600012", "600013", "600014",
                 "600009"}, "D6 一只票一条信号（含 HOLD）")
ok("600008" not in sigs, "D7 榜外（rank>top_n）不评估不落行")
ok([r["rule"] for r in sigs["600001"]["reasons"]]
   == ["trend_ok", "liquidity_ok", "size_ok"], "D8 买入信号附全部通过证据")
ok(sigs["600001"]["target_qty"] == 20200 and sigs["600001"]["target_qty"] % 100
   == 0, "D9 买入数量 = 仓位/现金/整手约束的确定解 20200 股")
a10 = sigs["600001"].get("alloc_pct") or 0.0
ok(0.15 < a10 <= 0.201, "D10 单股仓位 ≤ 20% 权益")
ok(sigs["600001"]["rule_version"] == "rule-v1"
   and sigs["600001"]["param_version"] == pmod.params_version(P_MAIN),
   "D11 信号带规则版本与参数版本")
ok(sigs["600001"]["snapshot_date"] == "20270107"
   and sigs["600001"]["bar_date"] == "20270107"
   and sigs["600001"]["exec_date"] == "20270108",
   "D12 信号记录依赖的数据日期")
ok(sigs["600004"]["bar_date"] is None, "D13 缺行情的 bar_date 记 NULL 不编造")
ok([r["rule"] for r in sigs["600003"]["reasons"]] == ["st"], "D14 ST 排除")
ok([r["rule"] for r in sigs["600002"]["reasons"]] == ["liquidity"],
   "D15 流动性不足排除")
ok([r["rule"] for r in sigs["600004"]["reasons"]] == ["bar_missing"],
   "D16 缺行情排除")
ok([r["rule"] for r in sigs["600005"]["reasons"]] == ["trend_down"],
   "D17 趋势向下排除")
ok([r["rule"] for r in sigs["600010"]["reasons"]].__contains__("stop_loss"),
   "D18 固定止损命中")
ok([r["rule"] for r in sigs["600011"]["reasons"]] == ["trailing_stop"],
   "D19 移动止盈命中且仅此一条")
ok([r["rule"] for r in sigs["600012"]["reasons"]] == ["trend_break"],
   "D20 趋势破坏命中且仅此一条")
ok([r["rule"] for r in sigs["600013"]["reasons"]] == ["max_hold"],
   "D21 最大持仓日命中且仅此一条")
ok([r["rule"] for r in sigs["600014"]["reasons"]] == ["hold_ok"]
   and sigs["600014"]["target_qty"] is None,
   "D22 没触发任何退出规则的持仓 = 明确 HOLD 留痕")
ok([r["rule"] for r in sigs["600009"]["reasons"]] == ["bar_missing"],
   "D23 持仓缺行情不臆断去留")
for s in sigs.values():
    ok(all(r.get("rule") and r.get("detail") for r in s["reasons"]),
       "D24 每条原因都有规则名与说明: %s" % s["code"])
    ok(all(dt in DAYS for r in s["reasons"]
           for dt in _dates_in(r["detail"])),
       "D25 原因里的日期全部来自夹具交易日: %s" % s["code"])
run_row = db.trading_run_get("rule_v1", "20270107")
ok(run_row["status"] == "OK" and run_row["candidates_n"] == 6
   and run_row["signals_n"] == 11, "D26 run 记录候选数与信号数")
ok(pmod.params_version(json.loads(run_row["params_json"]))
   == run_row["param_version"], "D27 run 里的参数快照可复现版本号")
ok(db.trading_param_ensure and db.trading_param_ensure(
    pmod.params_version(P_MAIN), pmod.params_json(P_MAIN)) is None
   and True, "D28 参数版本落档不报错")
con = sqlite3.connect(settings.db_path)
n_params = con.execute("SELECT COUNT(*) FROM strategy_params").fetchone()[0]
con.close()
ok(n_params == 1, "D29 同参数只留一条版本档")

# ---- D2 幂等：同日重跑不产生第二轮 ------------------------------------------
res2 = tsvc.run_signals("20270107", {**P_MAIN, "stop_loss_pct": 0.09})
ok(res2["idempotent"] is True and res2["run_id"] == run_id,
   "D30 同日重跑返回既有 run（参数变了也不新开）")
con = sqlite3.connect(settings.db_path)
ok(con.execute("SELECT COUNT(*) FROM strategy_run").fetchone()[0] == 1,
   "D31 幂等：run 只有一行")
ok(con.execute("SELECT COUNT(*) FROM trade_signal").fetchone()[0] == 11,
   "D32 幂等：信号没有翻倍")
ok(con.execute("SELECT COUNT(*) FROM sim_order").fetchone()[0] == 5,
   "D33 幂等：订单没有翻倍")
con.close()
ok(res2["orders"]["placed"] == 0, "D34 幂等重跑不再下新单")

# ---- D3 第二天再跑：600001 已有在途买单 → 重复保护 --------------------------
res3 = tsvc.run_signals("20270108", P_MAIN)
ok(res3["status"] == "OK" and res3["idempotent"] is False
   and res3["run_id"] != run_id, "D35 次日新 run")
sigs3 = {s["code"]: s for s in tsvc.signals(res3["run_id"])}
ok([r["rule"] for r in sigs3["600001"]["reasons"]] == ["duplicate_order"],
   "D36 已有在途买单不重复下单")
ok(res3["orders"]["placed"] == 6, "D37 次日 6 个持仓的退出单照常放行")
res_def = tsvc.run_signals(None, P_MAIN)
ok(res_def["signal_date"] == "20270108" and res_def["idempotent"] is True,
   "D38 缺省信号日 = 最新快照日且幂等")

# ---- D4 未来数据泄漏（行为 + 上界 + 源码）----------------------------------
win = db.bars_load_window("daily_bar", ["600001"], "20270104", "20270107")
ok(max(win["600001"]) == "20270107" and "20270108" not in win["600001"],
   "D39 行情加载上界 = 信号日（0108 的行情就在库里但进不了窗口）")
ref = db.bars_window_dates("daily_bar", "20270104", "20270107")
ok(ref[-1] == "20270107", "D40 交易日窗口不越过信号日")
svc_src = Path(r"D:\test-kanban\backend\app\trading\service.py").read_text(
    encoding="utf-8")
ok("bars_load_window(BAR_TABLE, codes, since, date)" in svc_src,
   "D41 源码级：bars 上界传的是信号日")
_svc_calls = {n.func.attr for n in ast.walk(ast.parse(svc_src))
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
ok(not ({"trade_dates", "last_trade_date"} & _svc_calls),
   "D42 源码级：不存在会联网的 trade_dates()/last_trade_date() 调用"
   "（注释提到名字不算调用）")
ok("from app.fetchers.calendar import cached_trade_dates" in svc_src,
   "D43 源码级：日历入口唯一")
strat_src = Path(r"D:\test-kanban\backend\app\trading\strategy.py").read_text(
    encoding="utf-8")
ok("signal_date" in strat_src and "ref_dates" in strat_src,
   "D44 策略窗口只认信号日与已截断的交易日序列")

# ---- D5 全链路执行：0108 开盘撮合 → 账实相符 -------------------------------
x1 = tsvc.execute(None)
ok(x1["ran"] is True and x1["exec_date"] == "20270108",
   "D45 自动模式取最早待执行日 0108")
ok(x1["filled"] == 5 and x1["rejected"] == 0 and x1["expired"] == 0,
   "D46 0108 批次：4 卖 + 1 买全部成交")
buy_fill = next(d for d in x1["details"] if d["side"] == "BUY")
ok(buy_fill["price"] == 10.41 and abs(
    buy_fill["fee"] - 10.41 * 20200 * 0.00025) < 1e-9,
   "D47 买入成交价 = 开盘×(1+滑点) 取分，佣金 0.025% 超 5 元门槛")
pos = db.sim_position_get("600001")
ok(pos["qty"] == 20200 and pos["locked_qty"] == 20200
   and pos["locked_date"] == "20270108" and pos["entry_date"] == "20270108"
   and pos["avg_cost"] == 10.41 and pos["cost_factor"] == 1.0,
   "D48 成交记账生成持仓（T+1 锁定 + 入场成本/因子）")
ok(db.sim_position_get("600010") is None and db.sim_position_get("600013")
   is None, "D49 清仓卖单删除持仓行")
x2 = tsvc.execute(None)
ok(x2["ran"] is True and x2["exec_date"] == "20270111"
   and x2["filled"] == 2 and x2["rejected"] == 4,
   "D50 次日批次：老卖单标的已清仓 → 拒单不伪造成交")
ok(db.sim_position_get("600001")["locked_qty"] == 0,
   "D51 T+1 次日解锁（locked_date < 执行日）")
x3 = tsvc.execute(None)
ok(x3["ran"] is False and "没有待执行订单" in x3["reason"],
   "D52 队列清空后再执行 = 无操作")
con = sqlite3.connect(settings.db_path)
cash = con.execute("SELECT cash FROM sim_account WHERE id=1").fetchone()[0]
delta_sum = con.execute("SELECT SUM(cash_delta) FROM sim_fill").fetchone()[0]
n_fills = con.execute("SELECT COUNT(*) FROM sim_fill").fetchone()[0]
left = [r[0] for r in con.execute("SELECT code FROM sim_position ORDER BY code")]
con.close()
ok(abs(cash - (1_000_000.0 + delta_sum)) < 1e-6,
   "D53 账实相符：现金 = 初始 + 每笔成交的现金流合计")
ok(n_fills == 7, "D54 成交恰好 7 笔（5+2，无重复记账）")
ok(left == ["600001"], "D55 最终持仓只剩新建仓的 600001")
st = tsvc.status()
ok(st["account"]["cash"] == cash and st["pending_orders"] == 0
   and st["last_run"]["signal_date"] == "20270108"
   and [p["code"] for p in st["positions"]] == ["600001"],
   "D56 状态端点与库一致（账户/持仓/待执行/最近 run）")
ok(len(tsvc.runs(5)) == 2 and len(tsvc.orders(50)) == 11,
   "D57 runs/orders 列表条数")
ok(tsvc.params_info()["rule_version"] == "rule-v1"
   and tsvc.params_info()["defaults"]["stop_loss_pct"] == 0.08,
   "D58 参数端点给的是同一份默认事实")

# ==== 5) 数据风险状态：禁新买入、持仓照常退出（E 段）========================
_build_gen_db("kanban_strategy_risk_")
_ins_ingest("pool_zt", "20270107", "FAILED")
rr = tsvc.run_signals("20270107", P_MAIN)
ok(rr["risk_state"] == "RISK" and rr["quality_state"] == "incomplete",
   "E1 输入域不全 = 风险态（rank_quality 只读 ingest_job）")
rs = {s["code"]: s for s in tsvc.signals(rr["run_id"])}
ok([r["rule"] for r in rs["600001"]["reasons"]] == ["risk_gate"],
   "E2 风险态下候选一律 HOLD（禁新开仓）")
ok([r["rule"] for r in rs["600011"]["reasons"]] == ["trailing_stop"],
   "E3 风险态下持仓退出规则照常执行")
ok(rr["orders"] == {"placed": 4, "risk_rejected": 0},
   "E4 风险态只放行卖单")

# ==== 6) 仓位约束（F 段）====================================================
_build_gen_db("kanban_strategy_cap1_")
rc = tsvc.run_signals("20270107", {**P_MAIN, "max_positions": 2})
cs = {s["code"]: s for s in tsvc.signals(rc["run_id"])}
ok([r["rule"] for r in cs["600001"]["reasons"]] == ["position_cap"],
   "F1 持仓数达上限不再买入")
ok(rc["orders"]["placed"] == 4, "F2 上限只挡买单不挡卖单")
_build_gen_db("kanban_strategy_cap2_")
rc2 = tsvc.run_signals("20270107", {**P_MAIN, "max_total_exposure": 0.01,
                                   "max_position_pct": 0.01})
cs2 = {s["code"]: s for s in tsvc.signals(rc2["run_id"])}
ok([r["rule"] for r in cs2["600001"]["reasons"]] == ["exposure_cap"],
   "F3 总仓位达上限不再买入")
_mk_db("kanban_strategy_cap3_")
_ins_bars([_bar("600001", d, c) for d, c in zip(DAYS, [
    10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70, 10.80, 10.90])])
_snapshot("20270107", [dict(CAND_0107[0])])
db.sim_account_init(5000.0)
rc3 = tsvc.run_signals("20270107", {"trend_ma": 3})
cs3 = {s["code"]: s for s in tsvc.signals(rc3["run_id"])}
ok(list(cs3) == ["600001"] and [r["rule"] for r in cs3["600001"]["reasons"]]
   == ["position_pct_cap"], "F4 权益太小：单股上限不足一手，不硬凑信号")
ok(rc3["orders"] == {"placed": 0, "risk_rejected": 0}
   and rc3["signals"] == {"HOLD": 1}, "F5 无信号也落 run 与 HOLD 留痕")

# ==== 7) 明确拒绝：快照缺/日期不符/执行日定不了（G 段）======================
_mk_db("kanban_strategy_refuse_")
rf1 = tsvc.run_signals("20270107")
ok(rf1["status"] == "REFUSED" and "无六维候选快照" in rf1["refusal_reason"],
   "G1 无快照拒绝生成")
ok(db.trading_run_get("rule_v1", "20270107")["status"] == "REFUSED",
   "G2 拒绝也落 run 留痕")
rf1b = tsvc.run_signals("20270107")
ok(rf1b["idempotent"] is True, "G3 拒绝 run 重试仍走幂等键")
_ins_bars([_bar("600001", d, c) for d, c in zip(DAYS, [
    10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70, 10.80, 10.90])])
_snapshot("20270107", [dict(CAND_0107[0])])
rf2 = tsvc.run_signals("20270107", P_MAIN)
ok(rf2["status"] == "OK" and rf2["run_id"] == rf1["run_id"]
   and rf2["idempotent"] is False, "G4 数据补齐后同一 run 就地升级 OK")
ok(rf2["orders"]["placed"] == 1, "G5 升级后正常下单")
con = sqlite3.connect(settings.db_path)
ok(con.execute("SELECT COUNT(*) FROM strategy_run").fetchone()[0] == 1,
   "G6 升级不新开 run 行")
con.close()
ok(tsvc.run_signals("20270107", P_MAIN)["idempotent"] is True,
   "G7 升级后重跑幂等")
_snapshot("20270111", [dict(CAND_0107[0])], envelope_date="20270110")
rf3 = tsvc.run_signals("20270111")
ok(rf3["status"] == "REFUSED" and "不符（数据过期）" in rf3["refusal_reason"],
   "G8 快照日期与信号日不符（数据过期）拒绝")
_snapshot("20270201", [dict(CAND_0107[0])])
tsvc.cached_trade_dates = lambda: []
rf4 = tsvc.run_signals("20270201")
tsvc.cached_trade_dates = lambda: list(DAYS)
ok(rf4["status"] == "REFUSED" and "无法确定下一交易日" in rf4["refusal_reason"],
   "G9 定不了下一交易日 → 拒绝且不下单")
ok(tsvc._next_trade_day("20270115") is None, "G10 库内与日历都没有更晚交易日")
tsvc.cached_trade_dates = lambda: ["20270201"]
ok(tsvc._next_trade_day("20270115") == "20270201",
   "G11 库内无行情时退回日历缓存")
tsvc.cached_trade_dates = lambda: list(DAYS)

# ==== 8) 撮合与 T+1（E2 段，独立执行库）======================================
def _order_of(sig_id):
    for o in db.trading_orders_list(500):
        if o["signal_id"] == sig_id:
            return o
    return None


def _mk_run_day(sig_date, exec_day):
    return db.trading_run_create(
        "rule_v1", sig_date, exec_day, "rule-v1", pmod.params_version({}),
        pmod.params_json({}), "OK")[0]


def _mk_signal(run_id, code, side, qty, est, signal_date="20270107",
               exec_date="20270108", name="执行夹具"):
    """手工落一条信号（UNIQUE(run_id, code)，每 run 每码只一条）。"""
    db.trading_signal_add([{
        "run_id": run_id, "code": code, "name": name, "side": side,
        "signal_date": signal_date, "exec_date": exec_date, "reasons": [],
        "target_qty": qty, "alloc_pct": None, "est_price": est,
        "rule_version": "rule-v1", "param_version": pmod.params_version({}),
        "snapshot_date": None, "bar_date": None}])
    for s in db.trading_signals_by_run(run_id):
        if str(s["code"]) == str(code) and s["side"] == side:
            return int(s["id"])
    raise AssertionError("signal missing: run=%s %s %s"
                         % (run_id, side, code))


def _place(signal_id, exec_day, qty):
    con = sqlite3.connect(settings.db_path)
    row = con.execute("SELECT * FROM trade_signal WHERE id=?",
                      (signal_id,)).fetchone()
    cols = [d[0] for d in con.execute(
        "SELECT * FROM trade_signal WHERE 0").description]
    con.close()
    sig = dict(zip(cols, row))
    return paper.place(sig, {"approved": True, "qty": qty, "rule": None,
                             "detail": None}, exec_day)


_mk_db("kanban_strategy_exec_")
rows = [_bar("600001", d, c) for d, c in zip(DAYS, [
    10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70, 10.80, 10.90])]
rows += [_bar("600006", d, 9.00) for d in DAYS]
rows += [_bar("600006", "20270108", 9.50, open_=9.90, high=9.95, low=8.90)]
rows += [_bar("600007", d, 6.00) for d in DAYS if d != "20270108"]
rows += [_bar("600015", d, 10.00) for d in DAYS]
rows += [_bar("600015", "20270108", 9.20, open_=9.00, high=9.10, low=8.95)]
_ins_bars(rows)
_ins_positions([("600015", "跌停卖不出", 500, 0, None, 10.0, 1.0,
                 "20270104", "T")])
db.sim_account_init(1_000_000.0)
E1 = _mk_run_day("20270107", "20270108")
o_buy_ok = _place(_mk_signal(E1, "600001", "BUY", 1000, 10.30), "20270108",
                  1000)
o_buy_up = _place(_mk_signal(E1, "600006", "BUY", 100, 9.00), "20270108", 100)
o_buy_nb = _place(_mk_signal(E1, "600007", "BUY", 100, 6.00), "20270108", 100)
o_sell_dn = _place(_mk_signal(E1, "600015", "SELL", 500, 10.00), "20270108",
                   500)
E2 = _mk_run_day("20270106", "20270108")
o_no_cash = _place(_mk_signal(E2, "600001", "BUY", 1_000_000, 10.30),
                   "20270108", 1_000_000)
xe = paper.execute("20270108")
ok(xe["ran"] is True and xe["filled"] == 1 and xe["rejected"] == 4,
   "H1 执行日批次：1 成交 4 拒单")
ok(_order_of(o_buy_ok)["status"] == "FILLED", "H2 正常买单成交")
ok(_order_of(o_buy_up)["status"] == "REJECTED"
   and "涨停" in _order_of(o_buy_up)["reject_reason"], "H3 涨停开盘不追买")
ok(_order_of(o_buy_nb)["status"] == "REJECTED"
   and "无行情" in _order_of(o_buy_nb)["reject_reason"], "H4 执行日缺行情不伪造成交")
ok(_order_of(o_sell_dn)["status"] == "REJECTED"
   and "跌停" in _order_of(o_sell_dn)["reject_reason"], "H5 跌停开盘不砸卖")
ok(_order_of(o_no_cash)["status"] == "REJECTED"
   and "现金不足" in _order_of(o_no_cash)["reject_reason"], "H6 撮合前现金复核拒单")
con = sqlite3.connect(settings.db_path)
ok(abs(con.execute("SELECT cash FROM sim_account WHERE id=1").fetchone()[0]
       - 989_585.0) < 1e-6, "H7 成交记账：现金 1000000-(10.41×1000+5)")
n_fill_e = con.execute("SELECT COUNT(*) FROM sim_fill").fetchone()[0]
con.close()
xe2 = paper.execute("20270108")
con = sqlite3.connect(settings.db_path)
n_fill_e2 = con.execute("SELECT COUNT(*) FROM sim_fill").fetchone()[0]
lock_e = con.execute("SELECT locked_qty FROM sim_position WHERE code='600001'"
                     ).fetchone()[0]
con.close()
ok(xe2["ran"] is True and xe2["filled"] == 0 and n_fill_e2 == n_fill_e == 1,
   "H8 同日重复执行不重复记账")
ok(lock_e == 1000, "H9 执行日不解除 T+1 锁（locked_date==执行日）")
# T+1：风控层拒（独立 run —— 信号 UNIQUE(run_id, code) 一个码一条）
E3 = _mk_run_day("20270105", "20270108")
sig_t1 = _mk_signal(E3, "600001", "SELL", 1000, 10.41,
                    signal_date="20270105")
sig_row = next(s for s in db.trading_signals_by_run(E3)
               if s["id"] == sig_t1)
pos_map = {x["code"]: x for x in db.sim_positions_list()}
acc_t1 = {**ACC, "positions": pos_map, "position_count": len(pos_map),
          "exec_date": "20270108"}
rev_t1 = risk.review(sig_row, acc_t1, pmod.normalize())
ok(rev_t1["approved"] is False and rev_t1["rule"] == "t1_locked",
   "H10 风控层：当日买入未解锁的持仓不可卖")
oid_t1 = paper.place(sig_row, rev_t1, "20270108")
con = sqlite3.connect(settings.db_path)
r_t1 = con.execute("SELECT status, reject_reason FROM sim_order WHERE id=?",
                   (oid_t1,)).fetchone()
con.close()
ok(r_t1[0] == "REJECTED" and "T+1" in r_t1[1], "H11 风控拒单也留订单行带原因")
# T+1：撮合层兜底（绕过风控直接下单）
E4 = _mk_run_day("20270104", "20270108")
sid_byp = _mk_signal(E4, "600001", "SELL", 1000, 10.41,
                     signal_date="20270104")
oid_byp = db.sim_order_place(signal_id=sid_byp, code="600001",
                             name="执行夹具", side="SELL", qty=1000,
                             status="PENDING", exec_date="20270108",
                             est_price=10.41)
xb = paper.execute("20270108")
con = sqlite3.connect(settings.db_path)
r_byp = con.execute("SELECT status, reject_reason FROM sim_order WHERE id=?",
                    (oid_byp,)).fetchone()
con.close()
ok(xb["filled"] == 0 and r_byp[0] == "REJECTED" and "T+1" in r_byp[1],
   "H12 撮合层兜底：未解锁持仓即使绕过风控也卖不掉")
# 次日解锁可卖
E5 = _mk_run_day("20270111", "20270111")
sid_nd = _mk_signal(E5, "600001", "SELL", 1000, 10.41,
                    signal_date="20270111", exec_date="20270111")
oid_nd = db.sim_order_place(signal_id=sid_nd, code="600001",
                            name="执行夹具", side="SELL", qty=1000,
                            status="PENDING", exec_date="20270111",
                            est_price=10.41)
xn = paper.execute("20270111")
ok(xn["filled"] == 1 and db.sim_position_get("600001") is None,
   "H13 次日解锁后卖出成交并清持仓")
con = sqlite3.connect(settings.db_path)
cash_nd = con.execute("SELECT cash FROM sim_account WHERE id=1").fetchone()[0]
con.close()
_px_nd = round(10.50 * (1.0 - 0.001), 2)          # 次日开盘 10.50 卖出
_amt_nd = _px_nd * 1000
_fee_nd = max(_amt_nd * 0.00025, 5.0) + _amt_nd * 0.0005
ok(abs(cash_nd - (989_585.0 + _amt_nd - _fee_nd)) < 1e-6,
   "H14 卖出回款按开盘价与费率入账")
# 过期：执行日已过（行情已翻篇）→ EXPIRED 不追补
E6 = _mk_run_day("20270113", "20270106")
sid_exp = _mk_signal(E6, "600015", "SELL", 500, 10.00,
                     signal_date="20270113")
oid_exp = db.sim_order_place(signal_id=sid_exp, code="600015",
                             name="执行夹具", side="SELL", qty=500,
                             status="PENDING", exec_date="20270106",
                             est_price=10.00)
xexp = paper.execute("20270112")
con = sqlite3.connect(settings.db_path)
r_exp = con.execute("SELECT status, reject_reason FROM sim_order WHERE id=?",
                    (oid_exp,)).fetchone()
con.close()
ok(xexp["expired"] >= 1 and r_exp[0] == "EXPIRED" and "追补" in r_exp[1],
   "H15 错过执行日 = 过期拒单不追补")

# ==== 9) 自动模式卡队列：执行日无行情自动跳（J 段）===========================
_mk_db("kanban_strategy_queue_")
_ins_bars([_bar("600001", d, c) for d, c in zip(DAYS, [
    10.00, 10.10, 10.20, 10.30, 10.40, 10.50, 10.60, 10.70, 10.80, 10.90])])
db.sim_account_init(1_000_000.0)
QA = _mk_run_day("20270107", "20270108")
QB = _mk_run_day("20270108", "20270111")
sa = _mk_signal(QA, "600001", "BUY", 100, 10.30)
sb = _mk_signal(QB, "600001", "BUY", 200, 10.30)
oid_a = db.sim_order_place(signal_id=sa, code="600001", name="执行夹具",
                           side="BUY", qty=100, status="PENDING",
                           exec_date="20270109", est_price=10.30)
db.sim_order_place(signal_id=sb, code="600001", name="执行夹具", side="BUY",
                   qty=200, status="PENDING", exec_date="20270111",
                   est_price=10.30)
xq = tsvc.execute(None)
con = sqlite3.connect(settings.db_path)
st_a = con.execute("SELECT status, reject_reason FROM sim_order WHERE id=?",
                   (oid_a,)).fetchone()
con.close()
ok(xq["ran"] is True and xq["expired"] == 1 and xq["filled"] == 1
   and xq["exec_date"] == "20270111",
   "J1 执行日（0109 周六）非交易日 → 过期旧单并跳到下一可执行日")
ok(st_a[0] == "EXPIRED" and "行情缺失" in st_a[1],
   "J2 卡住的单过期留因")
ok(tsvc.execute(None)["ran"] is False, "J3 队列处理完再执行 = 无操作")

# ==== 10) 兼容冒烟（K 段）====================================================
ok(callable(mainforce.run) and callable(engine.build_outcome)
   and callable(ingest.rank_quality),
   "K1 既有选股/回测/质量模块可用（全量回归另行覆盖）")
ok(strategy.generate.__doc__ and risk.review.__doc__ and paper.place.__doc__,
   "K2 新模块对外函数均有说明")

# ---- 收尾：零外联 + 汇总 ----------------------------------------------------
ok(not _BLOCKED, "K3 全程零非回环外联（socket 守卫未拦截到任何地址）")
if FAILS:
    print("FAILED %d/%d ASSERTIONS" % (len(FAILS), PASS + len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
sys.exit(0)
