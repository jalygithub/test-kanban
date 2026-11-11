# -*- coding: utf-8 -*-
r"""盘后日线采集（2026-11-11 路线 A：全市场快照顺带落 `daily_bar`）的**离线**回归。

每一条都对应一个当初会露馅的点：

  1) `quotes._snapshot_from_em` 带出 OHLCV：今开/最高/最低/成交量映射对；
     源缺列时回 None 不抛；`source` 如实标 eastmoney（备源同花顺没有这
     几个字段 —— 「没有」不是「值为 None」）。
  2) `_snapshot_day`：交易日 → 当天；周末/节假日 → 日历里最近的交易日；
     日历缺失/过期 → 抛（不知道快照属于哪天时唯一安全的做法是不落库）。
  3) `collect` 三道闸门 + 落库：
     - 未收盘（15:01 前）不落当日行，且**一个包都不发**（闸门在取数之前）；
     - 目标日 ≠ 快照所属日（指定历史日期重跑）→ 抛，不拿今天的快照顶替；
     - 备源无 OHLC / 源回 0 行 / 完整行低于下限 → 抛进重试，
       绝不静默空过（job_run #18 的教训：0 行不许判 SUCCESS）；
     - OHLC 或成交量不全的行（停牌、`-`）跳过不写，缺数据显示 `-` 不编数；
     - 落库只 `bars_upsert`（AST 钉死绝无 `bars_overwrite`），已有行情不覆盖。
  4) scheduler 接线：`_plan` 9 步且日线采集在打分之前；`_collect` 打分轮
     **先日线采集、再打分后取价**；PARTIAL 进 `partial` 不进 `failed`；
     run_light 不装；`run_retries` 认识 daily_bar 并分发到取数器。
  5) `fetch_daily_bar` 端到端（桩掉 `bars_sync.collect`）：成功 →
     `ingest_job` SUCCESS 且 `store=False`（snapshot 表**没有** daily_bar
     数据集，落库归 `bars_upsert` 管）；抛异常 → WAITING + 排了下次重试。
  6) 源码级：`fetch_daily_bar` 给 `_collect_domain` 传了 `store=False`
     （漏传 = 行情被写进 snapshot 表）。

全离线：开头封死 TCP + DNS + `requests.Session` 代理（系统代理会绕过
socket 守卫），`market_snapshot` 一律注入替身，日历读本地文件。
跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_bars_sync_check.py
"""
import ast
import socket
import sys
import types
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

# 系统代理（127.0.0.1:7890）会被 requests 读走：请求绕回环、DNS 由代查，
# socket 守卫一个都拦不到（blocked=0 却在真外发）。__init__ 每次都会把
# trust_env 重置回 True，只改类属性无效 —— 必须包 __init__。
import requests                                          # noqa: E402

_sess_init = requests.Session.__init__


def _no_proxy_init(self, *a, **k):  # noqa: ANN001
    _sess_init(self, *a, **k)
    self.trust_env = False
    self.proxies = {}


requests.Session.__init__ = _no_proxy_init

sys.path.insert(0, r"D:\test-kanban\backend")

from app.config import settings                          # noqa: E402

import sqlite3                                           # noqa: E402
import tempfile                                          # noqa: E402

# 夹具落在真 market.db 上就是污染 —— 临时库
_SCRATCH = Path(tempfile.mkdtemp(prefix="kanban_bars_"))
settings.db_path = _SCRATCH / "market.db"

from app import scheduler as sch                         # noqa: E402
from app.fetchers import calendar as tcal                # noqa: E402
from app.fetchers import quotes                          # noqa: E402
from app.pipeline import bars_sync as bs                 # noqa: E402
from app.pipeline import ingest as ing                   # noqa: E402
from app.store import db                                 # noqa: E402

N = [0]


def ok(cond, msg):
    N[0] += 1
    if not cond:
        print("FAIL #%d  %s" % (N[0], msg))
        sys.stdout.flush()
        sys.exit(1)
    print("ok   #%d  %s" % (N[0], msg))


# ---- 本地日历（真实文件，零网络）--------------------------------------------
_CAL = tcal.cached_trade_dates()
ok(bool(_CAL) and len(_CAL) > 100, "本地交易日历读得到（%d 天）" % len(_CAL))
_TD = _CAL[-1]                       # 日历里最后一个交易日（≤20261231）
_TD_PREV = _CAL[-2]
_TD_DT = datetime.strptime(_TD, "%Y%m%d")

# 找一个日历里的非交易日（周末/节假日），且确实在日历覆盖范围内
_cset = set(_CAL)
_off_dt = datetime.strptime(_CAL[len(_CAL) // 2], "%Y%m%d")
while _off_dt.strftime("%Y%m%d") in _cset:
    _off_dt += timedelta(days=1)
_OFF = _off_dt.strftime("%Y%m%d")
_OFF_PREV = max(d for d in _CAL if d < _OFF)

# ================== 1) quotes._snapshot_from_em 带出 OHLCV ===================
import pandas as pd                                      # noqa: E402

_df = pd.DataFrame([{
    "代码": "600000", "名称": "浦发银行", "最新价": 10.5, "涨跌幅": 1.23,
    "换手率": 0.45, "流通市值": 1.2e11, "成交额": 3.4e8,
    "今开": 10.2, "最高": 10.8, "最低": 10.1, "成交量": 123456,
}])
_r0 = quotes._snapshot_from_em(_df)[0]
ok(_r0.get("open") == 10.2 and _r0.get("high") == 10.8
   and _r0.get("low") == 10.1 and _r0.get("volume") == 123456
   and _r0.get("price") == 10.5,
   "今开/最高/最低/成交量/最新价全部映射对：%r"
   % {k: _r0.get(k) for k in ("open", "high", "low", "volume", "price")})
ok(_r0.get("source") == "eastmoney", "source 如实标 eastmoney")
_df_no = pd.DataFrame([{"代码": "000001", "名称": "平安银行", "最新价": 11.0}])
_r1 = quotes._snapshot_from_em(_df_no)[0]
ok(_r1.get("open") is None and _r1.get("price") == 11.0,
   "源缺 OHLC 列时回 None 不抛（open=%r price=%r）"
   % (_r1.get("open"), _r1.get("price")))

# ====================== 2) _snapshot_day（只读本地日历）======================
ok(bs._snapshot_day(_TD_DT.replace(hour=16, minute=30)) == _TD,
   "交易日当天 -> 快照属于今天（%s）" % _TD)
ok(bs._snapshot_day(datetime(_off_dt.year, _off_dt.month, _off_dt.day,
                             10, 0)) == _OFF_PREV,
   "非交易日 -> 快照属于最近的交易日（%s -> %s）" % (_OFF, _OFF_PREV))
try:
    bs._snapshot_day(_TD_DT + timedelta(days=40))
    ok(False, "日历过期（now 超出日历覆盖）必须抛")
except RuntimeError as e:
    ok("已过期" in str(e), "日历过期 -> 抛，不猜日期：%s" % e)
_orig_cal = tcal.cached_trade_dates
tcal.cached_trade_dates = lambda: []
try:
    bs._snapshot_day(_TD_DT)
    ok(False, "日历不可用必须抛")
except RuntimeError as e:
    ok("不可用" in str(e), "日历缺失 -> 拒绝落库：%s" % e)
finally:
    tcal.cached_trade_dates = _orig_cal

# ====================== 3) collect 三道闸门 + 落库 ============================
_calls: list = []


def _spy(rows):  # noqa: ANN001
    def _f():
        _calls.append(1)
        return rows
    return _f


# -- 闸门 A：未收盘不落当日行，且一个包都不发
try:
    bs.collect(_TD, fetch=_spy([]), now=_TD_DT.replace(hour=10, minute=0))
    ok(False, "15:01 前落当日行必须抛")
except RuntimeError as e:
    ok("未收盘" in str(e), "未收盘 -> 抛（盘中半日 OHLC 不许入库）：%s" % e)
ok(not _calls, "闸门在取数之前：被拒的轮次一个包都不发（发了 %d 个）"
   % len(_calls))

# -- 闸门 B：目标日 ≠ 快照所属日（指定历史日期重跑）
try:
    bs.collect(_TD_PREV, fetch=_spy([]), now=_TD_DT.replace(hour=16, minute=30))
    ok(False, "目标日与快照日期不符必须抛")
except RuntimeError as e:
    ok("不符" in str(e), "历史日期重跑 -> 抛，不拿今天的快照顶替：%s" % e)
ok(not _calls, "日期不符同样一个包都不发（发了 %d 个）" % len(_calls))


def _mk_rows(n_complete, bad=()):
    """n_complete 条完整行 + 若干坏行（停牌/缺字段）。"""
    rows = []
    for i in range(n_complete):
        rows.append({
            "code": "%06d" % (600000 + i), "name": "桩%d" % i,
            "price": 10.0 + (i % 7), "open": 9.9, "high": 10.5, "low": 9.8,
            "volume": 1000 + i, "amount": 1e6, "turnover": 1.0,
            "pct_chg": 0.5, "source": "eastmoney",
        })
    for kind in bad:
        if kind == "suspended":        # 成交量 0：停牌，不是当天的一根线
            rows.append({"code": "609999", "price": 10.0, "open": 9.9,
                         "high": 10.5, "low": 9.8, "volume": 0,
                         "source": "eastmoney"})
        elif kind == "no_open":        # 源给 `-` -> None
            rows.append({"code": "609998", "price": 10.0, "open": None,
                         "high": 10.5, "low": 9.8, "volume": 100,
                         "source": "eastmoney"})
        elif kind == "no_price":
            rows.append({"code": "609997", "open": 9.9, "high": 10.5,
                         "low": 9.8, "volume": 100, "source": "eastmoney"})
    return rows


def _bar_count(date):
    con = sqlite3.connect(str(settings.db_path))
    try:
        return con.execute("SELECT COUNT(*) FROM daily_bar "
                           "WHERE trade_date=?", (date,)).fetchone()[0]
    finally:
        con.close()


def _bar_close(code, date):
    con = sqlite3.connect(str(settings.db_path))
    try:
        row = con.execute("SELECT close FROM daily_bar WHERE code=? "
                          "AND trade_date=?", (code, date)).fetchone()
        return row[0] if row else None
    finally:
        con.close()


# -- 成功路径：完整行落库、坏行跳过、已有行情不覆盖
db.bars_upsert("daily_bar", [{"code": "600000", "trade_date": _TD,
                              "open": 1.0, "high": 1.0, "low": 1.0,
                              "close": 999.0, "volume": 1,
                              "source": "pre"}])
_ok_rows = _mk_rows(3005, bad=("suspended", "no_open", "no_price"))
_got = bs.collect(_TD, fetch=_spy(_ok_rows),
                  now=_TD_DT.replace(hour=16, minute=30))
ok(len(_got) == 3005,
   "3008 行快照只交出 3005 条完整行（got %d）" % len(_got))
ok(_bar_count(_TD) == 3005,
   "库里该日 3005 行（坏行不落、pre 行占位不重复，got %d）" % _bar_count(_TD))
ok(all(r["code"] not in ("609999", "609998", "609997") for r in _got)
   and _bar_close("609999", _TD) is None,
   "停牌/缺字段的行跳过不写（缺数据显示 -，不编数）")
ok(_bar_close("600000", _TD) == 999.0,
   "已有行情不覆盖（bars_upsert = DO NOTHING，close 仍是 pre 的 999.0）")

# -- 闸门 C1：备源（同花顺）没有 OHLC 字段 -> 抛进重试
_ths = [{"code": "600000", "price": 10.0, "pct_chg": 1.0,
         "source": "10jqka"}]
try:
    bs.collect(_TD, fetch=_spy(_ths), now=_TD_DT.replace(hour=16, minute=30))
    ok(False, "备源无 OHLC 必须抛")
except RuntimeError as e:
    ok("OHLC" in str(e), "备源降级 -> 判失败进重试，不静默空过：%s" % e)

# -- 闸门 C2：源回 0 行
try:
    bs.collect(_TD, fetch=_spy([]), now=_TD_DT.replace(hour=16, minute=30))
    ok(False, "快照 0 行必须抛")
except RuntimeError as e:
    ok("0 行" in str(e), "源回 0 行 -> 抛（0 行不许判 SUCCESS）：%s" % e)

# -- 闸门 C3：完整行低于下限（分页不全），不落半张市场
try:
    bs.collect(_TD, fetch=_spy(_mk_rows(5)),
               now=_TD_DT.replace(hour=16, minute=30))
    ok(False, "完整行低于下限必须抛")
except RuntimeError as e:
    ok("低于下限" in str(e), "分页不全 -> 抛进重试：%s" % e)

# -- 非交易日不看收盘时刻：闸门放行、走到取数才因行数不足被拦
_calls.clear()
try:
    bs.collect(_OFF_PREV, fetch=_spy(_mk_rows(5)),
               now=datetime(_off_dt.year, _off_dt.month, _off_dt.day, 10, 0))
    ok(False, "行数不足应抛")
except RuntimeError as e:
    ok("低于下限" in str(e) and bool(_calls),
       "周末上午跑上一交易日：收盘闸门不适用，取数已发出（%s）" % e)

# ====================== 4) scheduler 接线 ====================================
ok(sch._plan(True) == ["pool_zt", "pool_zb", "pool_dt", "pool_strong",
                       "sector_industry", "sector_concept",
                       "daily_bar", "main_force", "top5_track"],
   "_plan(scoring=True) 9 步且日线采集在打分之前：%s" % sch._plan(True))
ok(sch._plan(False) == ["pool_zt", "pool_zb", "pool_dt", "pool_strong",
                        "sector_industry", "sector_concept"],
   "_plan(False) 不装日线采集（run_light 盘中也跑，15:01 闸门会判失败）：%s"
   % sch._plan(False))

T_ORDER = "20990101"
_order: list = []
_orig_sch = (sch.fetch_pool, sch.fetch_sector_flow, sch.fetch_daily_bar,
             sch._score_and_record, sch.top5track)
sch.fetch_pool = lambda kind, target: 7
sch.fetch_sector_flow = lambda kind: 5
sch.fetch_daily_bar = lambda t: (_order.append("daily_bar"), 4)[1]
sch._score_and_record = lambda t: (_order.append("score"), 3)[1]
sch.top5track = types.SimpleNamespace(
    pull=lambda t: (_order.append("track"), {"tracking": 1})[1])
try:
    # PARTIAL 闸门：跑成了但数据不全 -> 进 partial，不进 failed
    db.ingest_try_start("daily_bar", T_ORDER, source="eastmoney")
    db.ingest_finish("daily_bar", T_ORDER, "PARTIAL", processed=10,
                     stored=10, failed_rows=0, error="桩：闸门判不完整")
    res, failed, partial = sch._collect(T_ORDER, False, True, live=False)
    ok(_order == ["daily_bar", "score", "track"],
       "**先日线采集、再打分后取价**（顺序 %r）" % _order)
    ok("daily_bar" in partial and "daily_bar" not in failed,
       "daily_bar 判 PARTIAL 进 partial 不进 failed（partial=%r failed=%r）"
       % (partial, failed))
    ok(res.get("daily_bar") == 4, "步骤结果落进 res（%r）"
       % res.get("daily_bar"))

    # run_light：不装日线采集
    _order.clear()
    res_l, _, _ = sch._collect(T_ORDER, False, False, live=False)
    ok("daily_bar" not in res_l and not _order,
       "run_light 不跑日线采集也不打分（res=%s）" % sorted(res_l))
finally:
    (sch.fetch_pool, sch.fetch_sector_flow, sch.fetch_daily_bar,
     sch._score_and_record, sch.top5track) = _orig_sch

# -- run_retries 分发：daily_bar 认识且送到取数器
T_RETRY = "20990103"
db.ingest_start("daily_bar", T_RETRY)
ing.record_failure("daily_bar", T_RETRY, ConnectionError("桩"))
_con = sqlite3.connect(str(settings.db_path))
_con.execute("UPDATE ingest_job SET next_retry_at='1990-01-01T00:00:00+00:00' "
             "WHERE domain='daily_bar' AND trade_date=?", (T_RETRY,))
_con.commit()
_con.close()
_spy_bar: list = []
sch.fetch_daily_bar = lambda t: _spy_bar.append(t) or 9
try:
    out = sch.run_retries(domain="daily_bar")
finally:
    sch.fetch_daily_bar = _orig_sch[2]
ok(out.get("ran") == 1 and out.get("checked") == 1,
   "run_retries 认识 daily_bar 并实跑（%r）" % {k: out.get(k)
                                                for k in ("checked", "ran")})
ok(_spy_bar == [T_RETRY], "分发把目标日传给了取数器（%r）" % _spy_bar)

# ============== 5) fetch_daily_bar 端到端（桩掉 bars_sync.collect）==========
T_REG = "20990102"
# 三条**不同 code** 的行：validate_rows 会把同码重复判成坏行（PARTIAL）
_STUB_BARS = [{"code": "%06d" % (600000 + i), "trade_date": T_REG,
               "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
               "volume": 100, "source": "eastmoney"} for i in range(3)]
_orig_collect = bs.collect
bs.collect = lambda t, **k: list(_STUB_BARS)
try:
    n = sch.fetch_daily_bar(T_REG)
finally:
    bs.collect = _orig_collect
row = db.ingest_get("daily_bar", T_REG) or {}
ok(n == 3 and row.get("status") == "SUCCESS",
   "成功 -> SUCCESS 且 stored=3（n=%r status=%r）" % (n, row.get("status")))
ok(row.get("source") == "eastmoney",
   "source 落 eastmoney（%r）" % row.get("source"))
_con = sqlite3.connect(str(settings.db_path))
_snap_n = _con.execute("SELECT COUNT(*) FROM snapshot WHERE dataset=?",
                       ("daily_bar",)).fetchone()[0]
_con.close()
ok(_snap_n == 0,
   "store=False：snapshot 表没有 daily_bar 数据集（落库归 bars_upsert 管，"
   "got %d）" % _snap_n)

bs.collect = lambda t, **k: (_ for _ in ()).throw(
    ConnectionError("桩：源挂了"))
try:
    try:
        sch.fetch_daily_bar(T_REG)
        _raised = False
    except ConnectionError:
        _raised = True
finally:
    bs.collect = _orig_collect
row = db.ingest_get("daily_bar", T_REG) or {}
ok(_raised, "取数器抛异常 -> fetch_daily_bar 原样上抛")
ok(row.get("status") == "WAITING" and bool(row.get("next_retry_at")),
   "异常 -> WAITING 且排了下次重试（status=%r retry=%r）"
   % (row.get("status"), row.get("next_retry_at")))

# ====================== 6) 源码级 =============================================
_tree = ast.parse(Path(bs.__file__).read_text(encoding="utf-8"))
_attrs = {n.attr for n in ast.walk(_tree) if isinstance(n, ast.Attribute)}
ok("bars_upsert" in _attrs and "bars_overwrite" not in _attrs,
   "bars_sync 只 upsert 不 overwrite（attrs 含 overwrite: %s）"
   % ("bars_overwrite" in _attrs))

_stree = ast.parse(Path(sch.__file__).read_text(encoding="utf-8"))
_fn = next(n for n in _stree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "fetch_daily_bar")
_kw = {k.arg: k.value
       for c in ast.walk(_fn) if isinstance(c, ast.Call)
       for k in getattr(c, "keywords", []) if k.arg}
ok(isinstance(_kw.get("store"), ast.Constant)
   and _kw["store"].value is False,
   "fetch_daily_bar 给 _collect_domain 传了 store=False（漏传 = 行情写进 "
   "snapshot 表）")

ok(not _BLOCKED, "全程一个包都没发出去（blocked=%d）" % len(_BLOCKED))

print("ALL %d ASSERTIONS PASSED" % N[0])
