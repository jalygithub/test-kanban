# -*- coding: utf-8 -*-
r"""收益计算的**复权口径**回归，2026-10-16。

对应 `app/research/engine.py` 的 `price_adjust` / `build_outcome` /
`_warn_adjust`，与 `store/db.py` 的 `research_outcomes` 复权四列。

## 为什么必须有这一层

回填落的是**不复权** OHLC（`backfill.py` 的硬口径）。不复权价在除权日会
跳空下跌 —— 一只 10 元每 10 股派 0.5 元的票，除权那天收盘从 10.00 变成
9.50，看着像 -5%，实际上持有人一分钱没少。**拿不复权价直接跨除权日算收益，
长期回测会被这类「假亏损」系统性拉垮。** 本组测试盯的就是这件事。

后复权因子随除权单调递增：某日的后复权价 = 不复权价 × 因子。两端各乘
自己的因子再比，比值才是真实收益率。

## 三档口径

  RAW                     两端因子都有且不同 => 折算过
  UNADJUSTED              因子相同 => 期间无除权，不复权价直算即正确
  UNADJUSTED_UNCERTAIN    缺 adj_factor => **判不了**，收益可能含除权跳空

## 向后兼容这条硬约束

库里老日线表没有 `adj_factor` 列。折算对它必须**完全不改变数值** ——
只多出几个字段。这条不是「为了少改测试」，而是正确性要求：缺因子时我们
并不知道有没有除权，所以只能按不复权算，同时**把「不知道」写在记录上**。
数值一变，就等于替数据做了决定。

全离线：开头封死 TCP + DNS，用临时夹具库，不碰真实 market.db。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_adjust_check.py
"""
import json
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
        raise AssertionError("BLOCKED-NETWORK: %s" % (addr,))
    return _orig(self, addr)


def _gai_guard(host, port, *a, **k):  # noqa: ANN001
    if str(host) not in _LOOP:
        _BLOCKED.append("dns:%s" % (host,))
        raise AssertionError("BLOCKED-DNS: %s:%s" % (host, port))


socket.socket.connect = _guard
socket.getaddrinfo = _gai_guard

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import settings                                  # noqa: E402

_ROOT = Path(tempfile.mkdtemp(prefix="adjust_check"))
settings.db_path = _ROOT / "market.db"

from app.store import db                                         # noqa: E402
from app.research import engine as E                             # noqa: E402

db._conn = None

PASS = 0
FAILS = []


def ok(cond, label):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)


def close(a, b, tol=1e-9):  # noqa: ANN001
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


# ---- 1) 三档口径的判定 -------------------------------------------------------
ok(E.ADJ_METHODS == (E.ADJ_RAW, E.ADJ_UNADJUSTED, E.ADJ_UNCERTAIN),
   "A1 三档口径常量齐备")

r = E.price_adjust({"adj_factor": 1.0}, {"adj_factor": 1.25})
ok(r["method"] == E.ADJ_RAW, "A2 因子不同 => RAW")
ok(r["entry_factor"] == 1.0 and r["exit_factor"] == 1.25, "A3 系数原样带回")
ok(r["note"] and "adj_factor" in r["note"], "A4 RAW 带说明")

r = E.price_adjust({"adj_factor": 1.05}, {"adj_factor": 1.05})
ok(r["method"] == E.ADJ_UNADJUSTED, "A5 因子相同 => UNADJUSTED（确认无除权）")
ok(r["note"] is None, "A6 UNADJUSTED 不留说明 —— 没折算就是没折算")

r = E.price_adjust({"adj_factor": 1.0}, None)
ok(r["method"] == E.ADJ_UNCERTAIN, "A7 出场 K 线缺失 => UNCERTAIN")
r = E.price_adjust(None, {"adj_factor": 1.0})
ok(r["method"] == E.ADJ_UNCERTAIN, "A8 入场 K 线缺失 => UNCERTAIN")
ok("除权" in r["note"], "A9 UNCERTAIN 的说明点名了风险")

for bad in (0, -1, 0.0, "abc", None):
    ok(E._factor({"adj_factor": bad}) is None,
       "A10 非正/非数因子一律判缺（got %r）" % (bad,))
ok(E._factor({}) is None, "A11 无该字段 => None")
ok(E._factor(None) is None, "A12 无 K 线 => None")

# 关键：判缺时**系数给 1.0（不折算），但口径必须是 UNCERTAIN**。
# 若这里回 None 或把 method 写成 UNADJUSTED，就等于悄悄声明「没有复权」。
r = E.price_adjust({"close": 9.0}, {"close": 9.5})
ok(r["entry_factor"] == 1.0 and r["exit_factor"] == 1.0,
   "A13 缺因子 => 系数 1.0（不折算）")
ok(r["method"] == E.ADJ_UNCERTAIN,
   "A14 缺因子**不许**标成 UNADJUSTED —— 我们并不知道有没有除权")

# ---- 2) 除权造成的「假亏损」必须被折算掉 ------------------------------------
# 10 元每 10 股派 0.5 元：除权参考价 9.50，后复权因子比 = 10/9.5
F = 10.0 / 9.5
SIG, ENTRY, EXIT = "20261010", "20261013", "20261015"


def _bars(with_factor=True, exit_high=10.2, exit_low=9.2):
    """一根贯穿除权日的窗口：入场/中场在除权前，出场在除权后。"""
    def b(o, h, l, c, f):
        row = {"open": o, "high": h, "low": l, "close": c}
        if with_factor:
            row["adj_factor"] = f
        return row
    return {"600001": {
        SIG: b(9.6, 9.9, 9.5, 9.8, 1.0),
        ENTRY: b(10.0, 10.5, 9.8, 10.2, 1.0),
        "20261014": b(10.2, 10.6, 10.0, 10.3, 1.0),
        EXIT: b(9.5, exit_high, exit_low, 9.5, F),
    }}


REF = [SIG, ENTRY, "20261014", EXIT]

# 不复权直算：9.5 / 10.0 - 1 = -5%。这 5% 是**假的** —— 除权那天价格下跳
# 只是因为分红，持有人一分钱没少。
naive = E.build_outcome(SIG, "600001", 3, _bars(with_factor=False), REF, None)
ok(close(naive["theory_return"], 9.5 / 10.0 - 1.0),
   "B1 无因子时按不复权直算（got %r）" % naive["theory_return"])
ok(naive["adjust_method"] == E.ADJ_UNCERTAIN, "B2 无因子 => UNCERTAIN")

adj = E.build_outcome(SIG, "600001", 3, _bars(), REF, None)
ok(adj["adjust_method"] == E.ADJ_RAW, "B3 有因子且跨越除权 => RAW")
ok(close(adj["theory_return"], 0.0, 1e-9),
   "B4 折算后收益归零 —— 除权不是亏损（got %.10f）" % adj["theory_return"])
ok(adj["theory_return"] != naive["theory_return"],
   "B5 有因子时数值必须与不复权直算不同，否则折算等于没生效")

# 向后兼容的硬约束：缺因子时，**收益数值必须与折算前一模一样** ——
# 只多出几个字段。折算只对 theory_return / mfe / mae 生效，其余字段与
# 复权无关，两套夹具下必须完全一致。
for k in ("exec_return", "bench_return", "excess_return",
          "entry_price", "exit_price", "entry_date", "exit_date",
          "fill_status", "sell_status", "data_quality"):
    ok(adj[k] == naive[k], "B6 %s 与复权无关，两套夹具下必须一致" % k)

# 价格字段记的是**真实成交价（不复权）**，不是折算价
ok(adj["entry_price"] == 10.0 and adj["exit_price"] == 9.5,
   "B7 价格字段仍是不复权的真实成交价")
ok(close(adj["entry_factor"], 1.0) and close(adj["exit_factor"], F),
   "B8 折算系数单独存着，谁想复核都能乘回去")
ok(adj["fill_status"] == "FILLED" and adj["sell_status"] == "SELLABLE",
   "B9 成交判定未被折算改动（%s/%s）"
   % (adj["fill_status"], adj["sell_status"]))

# ---- 3) MFE / MAE 必须逐根用自己的因子 --------------------------------------
naive_mfe, naive_mae = naive["mfe"], naive["mae"]
ok(close(naive_mfe, 10.6 / 10.0 - 1.0),
   "C1 无因子时 mfe = 窗口最高/入场（got %r）" % naive_mfe)
ok(close(naive_mae, 9.2 / 10.0 - 1.0),
   "C2 无因子时 mae = 窗口最低/入场（got %r）" % naive_mae)

# 折算后：出场那根（除权后）high 10.2 × 10/9.5 ≈ 10.7368，才是窗口最高 ——
# 不复权口径下它的 10.2 反而被除权前的 10.6 压住，mfe 平白少算一截。
# low 9.2 × 10/9.5 ≈ 9.6842 则抬高了回撤下沿。
ok(close(adj["mfe"], (10.2 * F) / 10.0 - 1.0, 1e-9),
   "C3 mfe 用出场日自己的因子折算（got %.10f）" % adj["mfe"])
ok(close(adj["mae"], (9.2 * F) / 10.0 - 1.0, 1e-9),
   "C4 mae 用出场日自己的因子折算（got %.10f）" % adj["mae"])
ok(adj["mae"] > naive_mae,
   "C5 折算后回撤没那么深 —— 不复权口径把除权跳空算成了亏损"
   "（%.4f > %.4f）" % (adj["mae"], naive_mae))
ok(adj["mfe"] != naive_mfe, "C6 mfe 同样被折算")

# 窗口中途若缺因子，退回入场日的系数，不整窗一刀切
mid_bars = _bars()
del mid_bars["600001"]["20261014"]["adj_factor"]
part = E.build_outcome(SIG, "600001", 3, mid_bars, REF, None)
ok(part["adjust_method"] == E.ADJ_RAW, "C7 部分缺因子仍判 RAW（两端都有）")
ok(part["mae"] is not None, "C8 窗口里缺因子的那根退回入场系数，不算缺失")

# ---- 4) 不可算骨架的口径是 None，不是 UNADJUSTED ----------------------------
for got in (E.build_outcome("19990101", "600001", 1, _bars(), REF, None),
            E.build_outcome(SIG, "600001", 99, _bars(), REF, None),
            E.build_outcome(SIG, "600001", 1, {}, [], None)):
    ok(got["adjust_method"] is None and got["adjust_note"] is None,
       "D1 不可算的行 adjust_method 是 None（got %r）" % got["adjust_method"])
    ok(got["entry_factor"] is None and got["exit_factor"] is None,
       "D2 不可算的行系数也是 None")

# ---- 5) 落库与回读 ----------------------------------------------------------
eid, _hash = db.research_experiment_create(
    name="复权口径回归", strategy_version="v1",
    config_snapshot={"horizons": [3]}, universe_mode=db.UNIVERSE_MODE_CURRENT,
    start_date=SIG, end_date=EXIT)

# 两行必须是**不同的 (信号日, 代码, horizon)** —— 表上有 UNIQUE 约束，
# 同键的第二行会被 ON CONFLICT 合并掉，那就测不到「两种口径各落各的」。
naive2 = dict(naive)
naive2["horizon"] = 4
n = db.research_outcomes_replace(eid, [adj, naive2])
ok(n == 2, "E1 两行结果都写得进")
con = sqlite3.connect(settings.db_path)
kept = con.execute("SELECT COUNT(*) FROM research_outcomes WHERE experiment_id=?",
                   (eid,)).fetchone()[0]
con.close()
ok(kept == 2, "E2 两行各落各的，没被 UNIQUE 合并掉 —— got %d" % kept)

back = db.research_outcomes_of(eid)
ok(len(back) == 2, "E3 回读条数对得上（got %d）" % len(back))
stored = [r for r in back if r.get("horizon") == 3
          and close(r.get("theory_return"), 0.0, 1e-9)]
ok(stored, "E4 折算那行回读得到")
st = stored[0]
ok(st["adjust_method"] == E.ADJ_RAW, "E5 adjust_method 落库并回读")
ok(st["adjust_note"] and "adj_factor" in st["adjust_note"], "E6 adjust_note 回读")
ok(close(st["entry_factor"], 1.0) and close(st["exit_factor"], F, 1e-9),
   "E7 两个系数回读得到")
unc = [r for r in back if r.get("horizon") == 4]
ok(unc and unc[0]["adjust_method"] == E.ADJ_UNCERTAIN,
   "E8 无因子那行也各落各的口径")

# 老调用方不给这四列也不许炸
db.research_outcomes_replace(eid, [{
    "signal_date": SIG, "code": "600002", "horizon": 3, "status": "DONE",
    "theory_return": 0.01, "data_quality": "READY"}])
legacy = [r for r in db.research_outcomes_of(eid) if r["code"] == "600002"]
ok(legacy and legacy[0].get("adjust_method") is None,
   "E9 老调用方不传新列 => 落 NULL，不炸")
ok(len(db.research_outcomes_of(eid)) == 3, "E10 第三行也进得去")

# 表结构四列齐备，且是**可空**的（存量行留 NULL）
con = sqlite3.connect(settings.db_path)
cols = {r[1]: r[3] for r in con.execute(
    "PRAGMA table_info(research_outcomes)")}
con.close()
for c in ("adjust_method", "adjust_note", "entry_factor", "exit_factor"):
    ok(c in cols, "E11 表结构含 %s" % c)
    ok(cols.get(c) == 0, "E12 %s 可空 —— 存量行靠 NULL，不能 NOT NULL" % c)

# ---- 6) _migrate：老库补列后能写 --------------------------------------------
# 「老库」= 完整建库后**把这四列 DROP 掉**，比手搓一张瘦表真实 —— 手搓会漏掉
# _SCHEMA 里那些建在 research_experiments 上的索引（它们引用 status 等列），
# 结果测的不是补列而是建表失败。
ADJ_COLS = ("adjust_method", "adjust_note", "entry_factor", "exit_factor")
old_path = _ROOT / "old.db"
settings.db_path = old_path
db._conn = None
db._connect()                       # 先按当前 schema 建出完整库
con = sqlite3.connect(old_path)
for c in ADJ_COLS:
    con.execute("ALTER TABLE research_outcomes DROP COLUMN %s" % c)
con.commit()
before = {r[1] for r in con.execute("PRAGMA table_info(research_outcomes)")}
con.close()
ok(not (set(ADJ_COLS) & before), "F0 老库确实没有复权四列")

db._conn = None
db._connect()                       # 触发 _migrate
con = sqlite3.connect(old_path)
got = {r[1] for r in con.execute("PRAGMA table_info(research_outcomes)")}
con.close()
ok(set(ADJ_COLS) <= got, "F1 _migrate 给老库补上了复权四列")

# 补完列还得**真写得进去**，否则等于只改了结构没验证可用
db._conn = None
eid2, _ = db.research_experiment_create(
    name="老库补列", strategy_version="v1", config_snapshot={},
    universe_mode=db.UNIVERSE_MODE_CURRENT, start_date=SIG, end_date=EXIT)
ok(db.research_outcomes_replace(eid2, [adj]) == 1,
   "F2 补列后的老库写得进复权口径")
db._conn = None
settings.db_path = _ROOT / "market.db"
db._conn = None

# ---- 7) _warn_adjust：不确定性必须看得见 ------------------------------------
eng = E.BacktestEngine(0, 0)
eng._warn_adjust([adj, naive])
ok(eng.adjust_counts.get(E.ADJ_RAW) == 1, "G1 计数里 RAW 有 1 条")
ok(eng.adjust_counts.get(E.ADJ_UNCERTAIN) == 1, "G2 计数里 UNCERTAIN 有 1 条")
ok(any("判不了" in w for w in eng.warnings), "G3 UNCERTAIN 挂了运行警告")
ok(any("adj_factor" in w for w in eng.warnings), "G4 警告点名了缺的是什么")
ok(any("阶段 4" in w for w in eng.warnings), "G5 警告给出了解开的路径")

eng = E.BacktestEngine(0, 0)
eng._warn_adjust([adj])
ok(not any("判不了" in w for w in eng.warnings),
   "G6 全是 RAW 时不许挂「判不了」的警告")
ok(any("折算" in w for w in eng.warnings),
   "G6b RAW 照常挂一条「已折算」的说明，让人知道这批是怎么算的")
ok(eng.adjust_counts.get(E.ADJ_RAW) == 1, "G7 计数照常给出")

eng = E.BacktestEngine(0, 0)
eng._warn_adjust([{"status": "PENDING", "adjust_method": None},
                  {"status": "NO_DATA", "adjust_method": None}])
ok(not eng.adjust_counts, "G8 未完成的行不计入口径统计")
ok(not eng.warnings, "G9 未完成的行不产生口径警告")

eng = E.BacktestEngine(0, 0)
ok(eng._finish("failed")["adjust"] == {}, "G10 _finish 透出 adjust 计数")

# 未折算的 UNADJUSTED **不该**挂警告 —— 否则真 UNCERTAIN 会被噪声淹没
eng = E.BacktestEngine(0, 0)
eng._warn_adjust([{"status": "DONE", "adjust_method": E.ADJ_UNADJUSTED}] * 3)
ok(not eng.warnings, "G11 UNADJUSTED 不挂警告（确认无除权，直算本就对）")

# ---- 8) 收尾 -----------------------------------------------------------------
ok(not _BLOCKED, "Z1 一次真实外发都不许有：%s" % _BLOCKED[:3])

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("PASS=%d FAIL=%d" % (PASS, len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
