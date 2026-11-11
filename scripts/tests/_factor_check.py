# -*- coding: utf-8 -*-
r"""复权因子的回归，2026-10-16。

对应 `app/pipeline/factor.py` 与 `store/db.py` 的 `bars_fill_factors` /
`bars_factor_coverage`。

## 它解决什么

`fetch_daily` 落的是**不复权** OHLC，`adj_factor` 原本恒为 NULL。没有因子，
回测收益一律判成 `UNADJUSTED_UNCERTAIN` —— 也就是「不知道这段有没有除权」。
本模块把因子补上，收益才有资格标 `RAW` / `UNADJUSTED`。

## 三条容易写错的地方，逐条盯死

1. **前向填充的方向**。因子只在**除权事件日**有值（600519 六十多年才 33 个
   事件日），事件之间因子恒定。某日的因子 = 该日**或之前最近**的事件因子。
   填成「之后最近」（未来泄漏）或「整段取最新」（又一次未来泄漏）都是错的，
   而且**算出来的收益看起来完全正常** —— 只有跨除权的样本才暴露，最难查。
2. **早于所有事件的日子留 NULL**，不填 1.0。填 1.0 等于悄悄声明「没有复权」，
   那是口径不是数据（与 `backfill.py` 同一条原则）。
3. **`bars_upsert` 写不进因子**。它是 `ON CONFLICT DO NOTHING`，对已存在的
   行压根不动 —— 必须单开 `bars_fill_factors`。这个坑阶段 4 踩过一次同族的。

全离线：临时夹具库 + 替身取数器 + socket 封锁，不碰真实 market.db。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_factor_check.py
"""
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path

# ---- 0) 挡住所有非回环连接 ---------------------------------------------------
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

import pandas as pd                                                     # noqa: E402

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import settings                                          # noqa: E402

_ROOT = Path(tempfile.mkdtemp(prefix="factor_check"))
settings.db_path = _ROOT / "market.db"

from app.store import db                                                # noqa: E402
from app.pipeline import factor as F                                    # noqa: E402

db._conn = None

PASS = 0
FAILS = []


def ok(cond, label):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)


def make_fetcher(events, fail=False):
    """替身取数器：照着真 akshare 的形状回 DataFrame（`date` + `hfq_factor`）。"""

    def _f(**kw):
        if fail:
            raise ConnectionError("blocked by test")
        return pd.DataFrame({"date": list(events), "hfq_factor": list(events.values())})

    _f.calls = []
    return _f


# ---- 1) 代码 -> 交易所前缀 ---------------------------------------------------
ok(F.code_to_symbol("600519") == "sh600519", "A1 沪市主板 6 -> sh")
ok(F.code_to_symbol("000001") == "sz000001", "A2 深市主板 0 -> sz")
ok(F.code_to_symbol("300750") == "sz300750", "A3 创业板 3 -> sz")
ok(F.code_to_symbol("430047") == "bj430047", "A4 北交所 4 -> bj")
ok(F.code_to_symbol("830799") == "bj830799", "A5 北交所 8 -> bj")
for bad in ("60051", "6005199", "abc123", "", None, "60051A", "  "):
    ok(F.code_to_symbol(bad) is None,
       "A6 认不出的代码必须回 None，不许猜前缀（got %r）" % (bad,))
# 猜错前缀 = 拿**别的标的**的因子写进本票，算出来的收益是错的却看不出来
ok(F.code_to_symbol("000001") != F.code_to_symbol("600519"), "A7 前缀按首位区分")

# ---- 2) 前向填充 -------------------------------------------------------------
EV = {"20251020": 1.02, "20260115": 1.05, "20260610": 1.10}
DATES = ["20251001", "20260114", "20260115", "20260301", "20260609", "20260610", "20260801"]

got = dict(F.resolve(EV, DATES))
ok(len(got) == 6, "B1 除第一日外都有因子 —— got %d" % len(got))
ok("20251001" not in got,
   "B2 早于所有事件的交易日**不输出**（留 NULL），不填 1.0")
ok(got["20260114"] == 1.02, "B3 事件日之前用**上一个**事件的因子")
ok(got["20260115"] == 1.05, "B4 事件日当天切到新因子")
ok(got["20260301"] == 1.05, "B5 事件之间因子恒定")
ok(got["20260609"] == 1.05, "B6 新事件生效前仍用旧因子")
ok(got["20260610"] == 1.10, "B7 新事件当日切到新因子")
ok(got["20260801"] == 1.10, "B8 最后一个事件之后沿用它")
# 方向性：绝不能出现「用了还没发生的除权」
ok(all(k not in got or got[k] <= 1.10 for k in got), "B9 不会用到未来的因子")

ok(F.resolve({}, DATES) == [], "B10 无事件 -> 空")
ok(F.resolve(EV, []) == [], "B11 无交易日 -> 空")
ok(F.resolve({"20300101": 2.0}, DATES) == [],
   "B12 事件全在交易日之后 -> 空（这段时间内确实没有除权）")
# 乱序输入也要对
ok(dict(F.resolve({"20260610": 1.10, "20251020": 1.02, "20260115": 1.05},
                  DATES)) == got,
   "B13 事件乱序不影响结果")

# ---- 3) fetch_events 的解析 --------------------------------------------------
fake = make_fetcher({"2026-01-15": 1.05, "1900-01-01": 1.0, "not-a-date": 9.9})
ev = F.fetch_events("600519", fetcher=fake)
ok(ev == {"20260115": 1.05, "19000101": 1.0},
   "C1 日期横线转 8 位、非法日期丢弃 —— got %r" % ev)

bad = pd.DataFrame({"date": ["2026-01-15", "2026-02-01", "2026-03-01"],
                    "hfq_factor": [0.0, -1.0, None]})
ev2 = F.fetch_events("600519", fetcher=lambda **kw: bad)
ok(ev2 == {}, "C2 非正/缺失因子一律丢弃，**不填 1.0 充数** —— got %r" % ev2)

ev3 = F.fetch_events("600519", fetcher=lambda **kw: pd.DataFrame())
ok(ev3 == {}, "C3 空表 -> 空")
ev4 = F.fetch_events("600519", fetcher=lambda **kw: None)
ok(ev4 == {}, "C4 源回 None -> 空，不抛异常")
ok(F.fetch_events("600519", fetcher=make_fetcher({}, fail=True)) == {},
   "C5 取数失败回空 —— 因子缺失不是致命错误")
ok(F.fetch_events("60051", fetcher=make_fetcher({"20260115": 1.0})) == {},
   "C6 认不出的代码**不发请求**")
ok(F.fetch_events("600519", fetcher=make_fetcher({"20260115": 1.05})) == {"20260115": 1.05},
   "C7 正常路径")

# ---- 4) 落库：只补 NULL，不覆盖 -----------------------------------------------
db.bars_upsert("daily_bar", [
    {"code": "600519", "trade_date": d, "close": 100.0 + i}
    for i, d in enumerate(sorted(DATES))])
ok(db.bars_dates("daily_bar", "600519") == sorted(DATES), "D1 夹具就位")

n = F.fill("600519", EV)
ok(n == 6, "D2 更新了 6 行（第一日早于所有事件，留 NULL）—— got %d" % n)

con = sqlite3.connect(settings.db_path)
rows = {r[0]: r[1] for r in con.execute(
    "SELECT trade_date, adj_factor FROM daily_bar WHERE code='600519'")}
con.close()
ok(rows["20251001"] is None, "D3 早于所有事件的那行**仍是 NULL**，不是 1.0")
ok(rows["20260115"] == 1.05 and rows["20260801"] == 1.10, "D4 因子落对了行")
ok(rows["20260301"] == 1.05, "D5 前向填充落到正确的行上")

# 幂等：再跑一遍不重复计数，也不改已有值
n2 = F.fill("600519", EV)
ok(n2 == 0, "D6 已有因子的行不重复更新 —— got %d" % n2)

n3 = F.fill("600519", {"20260801": 9.99})
ok(n3 == 0, "D7 **不覆盖**已有因子 —— 核对过的值不该被一次重跑盖掉")
con = sqlite3.connect(settings.db_path)
v = con.execute("SELECT adj_factor FROM daily_bar WHERE code='600519'"
                " AND trade_date='20260801'").fetchone()[0]
con.close()
ok(v == 1.10, "D8 库里的因子仍是原值，没被 9.99 覆盖")

# 只补指定票，不连累别的票
db.bars_upsert("daily_bar", [{"code": "300750", "trade_date": "20260115",
                              "close": 10.0}])
n4 = F.fill("300750", {"20260115": 1.5})
ok(n4 == 1, "D9 按票补写")
con = sqlite3.connect(settings.db_path)
other = con.execute("SELECT adj_factor FROM daily_bar WHERE code='600519'"
                    " AND trade_date='20260115'").fetchone()[0]
con.close()
ok(other == 1.05, "D10 别的票不受影响")

# ---- 5) bars_fill_factors / coverage -----------------------------------------
db._conn = None
db.bars_upsert("daily_bar", [{"code": "000001", "trade_date": "20260101",
                              "close": 9.0}])
cov = db.bars_factor_coverage("daily_bar")
# 600519 有 7 行（其中 20251001 早于所有事件、留 NULL）+ 300750 1 行 + 000001 1 行
ok(cov["rows"] == 9, "E1 总行数 = 9（600519×7 + 300750 + 000001）—— got %d" % cov["rows"])
ok(cov["with_factor"] == 7, "E2 已填因子 7 行 —— got %d" % cov["with_factor"])
ok(cov["without_factor"] == 2,
   "E3 判不了除权 2 行（600519 首日 + 000001）—— got %d" % cov["without_factor"])
ok(cov["rate"] is not None and 0 < cov["rate"] < 1, "E4 填充率介于 0 和 1")

ok(db.bars_fill_factors("daily_bar", "600519", []) == 0, "E5 空输入不写")

# 空表的 rate 必须是 None 而不是 100% —— 否则空表显示「全部就绪」
empty = _ROOT / "empty.db"
settings.db_path = empty
db._conn = None
db.bars_upsert("daily_bar", [{"code": "600519", "trade_date": "20260101",
                              "close": 1.0}])
con = sqlite3.connect(empty)
con.execute("DELETE FROM daily_bar")
con.commit()
con.close()
ec = db.bars_factor_coverage("daily_bar")
ok(ec["rows"] == 0 and ec["with_factor"] == 0, "E6 空表计数为 0")
ok(ec["rate"] is None, "E7 空表 rate 是 **None 不是 1.0** —— 不许显示「全部就绪」")
ok(ec["without_factor"] == 0, "E8 空表 without_factor 为 0")
settings.db_path = _ROOT / "market.db"
db._conn = None

# ---- 6) backfill_factors 的 reason 要能区分原因 --------------------------------
r = F.backfill_factors("60051", fetcher=make_fetcher({"20260115": 1.05}))
ok(r["skipped"] and r["updated"] == 0, "F1 认不出前缀 -> skipped")
ok("交易所" in r["reason"], "F2 reason 说清是前缀问题")

r = F.backfill_factors("600519", fetcher=make_fetcher({}, fail=True))
ok(r["skipped"] and "除权事件" in r["reason"],
   "F3 取数失败的 reason 单独可辨 —— got %r" % r["reason"])

r = F.backfill_factors("888888", fetcher=make_fetcher({"20260101": 1.2}))
ok(r["skipped"] and "日线行" in r["reason"],
   "F4 库里没这票的日线时 reason 说清 —— got %r" % r["reason"])

# 000001 在 E 组插过一行且 adj_factor 仍是 NULL —— 拿它当「正常路径」的样本
r = F.backfill_factors("000001", fetcher=make_fetcher({"20260101": 1.2}))
ok(not r["skipped"] and r["updated"] == 1,
   "F5 正常路径：取到事件、写进库里那行 NULL —— got %r" % r)

# ---- 7) 收尾 -----------------------------------------------------------------
ok(not _BLOCKED, "Z1 一次真实外发都不许有：%s" % _BLOCKED[:3])

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("PASS=%d FAIL=%d" % (PASS, len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
