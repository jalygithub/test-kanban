# -*- coding: utf-8 -*-
r"""上榜矩阵**聚合**的离线回归，19 条（2026-11-03 重写）。

原版在 Temp 清理时连同运行输出一起丢失 —— 按 AGENTS「丢了照着重写」的
口径，依据当前 `main_force_matrix()` 的语义重建，计数保持 **16** 条不变；
同日加「每天的股价」（M16-M18）后为 **19** 条。
与原版的差别：改用**临时夹具库**而非读真实 market.db —— 聚合断言要钉
「同码跨日 name 取最新」「hits 同分先看 last_date 再看 best_rank」这类
排序细节，真库数据每天在变，夹具才稳；真实库一个字节都不写。

夹具摆放（日期全部取自真实交易日历缓存 `cached_trade_dates()`，相对
`ztpool.window_start()` 定位，随运行日自动漂移）：

    old   = 日历里 < 窗口起点的交易日（超东财 30 天窗口）
    within= >= 窗口起点的交易日
    O3,O2,O = old 最后三个      O3 跑过（打分夹具）、O2 漏跑且**有池子快照**
                                 O  漏跑且**没有**池子
    recent, mid = within 最后两个  recent 跑过（打分夹具）、mid 漏跑（窗口内）

本轮（补跑修复）新增的钉子是 **M15 `unfillable_dates`**：只有「超窗口且
本地无池子快照」的漏跑日才进 —— 这是矩阵上「不可补」标注的唯一数据源，
算错前端要么点得到 400、要么把还能补的列藏掉。

全离线：开头封死 TCP + DNS，日历只读本地缓存文件。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_matrix_check.py
"""
import socket
import sys
import tempfile
from datetime import datetime
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

# ---- 1) 夹具库（临时目录）：真实 market.db 只读不碰、更不写 ------------------
settings.db_path = Path(tempfile.mkdtemp(prefix="kanban_matrix_")) / "market.db"
from app.store import db as DB                        # noqa: E402

DB._conn = None
DB.init_db()

from app.fetchers import calendar as tcal            # noqa: E402
from app.fetchers import ztpool                       # noqa: E402

N = [0]


def ok(cond, msg):  # noqa: ANN001
    N[0] += 1
    if not cond:
        print("FAIL #%d  %s" % (N[0], msg))
        sys.exit(1)
    print("ok   #%d  %s" % (N[0], msg))


# ---- 2) 夹具日期：相对今天与 30 天窗口摆放，随运行日漂移 ---------------------
today = datetime.now().strftime("%Y%m%d")
cal = [d for d in tcal.cached_trade_dates() if d <= today]
win = ztpool.window_start()
old = [d for d in cal if d < win]
within = [d for d in cal if d >= win]
ok(len(old) >= 3 and len(within) >= 2,
   "M1 日历缓存够深：old=%d（>=3） within=%d（>=2）" % (len(old), len(within)))

O3, O2, O = old[-3], old[-2], old[-1]       # 超窗口三连：跑过 / 漏跑有池 / 漏跑没池
recent, mid = within[-1], within[-2]        # 窗口内两连：跑过 / 漏跑

_DISC = "夹具免责声明"
DB.save_one("main_force", O3, {
    "rows": [
        {"code": "600001", "name": "旧名", "industry": "旧行业",
         "rank": 5, "score": 80.1},
        {"code": "600003", "name": "丙", "industry": "化工",
         "rank": 9, "score": 70.2},
    ],
    "all_rows": [], "params": {}, "model": {}, "disclaimer": _DISC,
}, source="fixture")
DB.save_one("main_force", recent, {
    "rows": [
        {"code": "600001", "name": "新名", "industry": "新行业",
         "rank": 2, "score": 90.5},
        {"code": "600002", "name": "乙", "industry": "半导体",
         "rank": 7, "score": 75.0},
        {"code": "600006", "name": "丁", "industry": "军工",
         "rank": 8, "score": 72.0},
    ],
    "all_rows": [], "params": {}, "model": {}, "disclaimer": _DISC,
}, source="fixture")
# O2 超窗口但**本地有池子快照**（可补的例外）；O 什么都没有（不可补）
DB.save("pool_zt", O2, [{"code": "600004", "name": "涨停夹具"}], source="fixture")
DB.save("pool_dt", O2, [{"code": "600005", "name": "跌停夹具"}], source="fixture")
# 股价夹具（daily_bar 不复权收盘价，矩阵 prices 的唯一出处）：
#   600001 两天上榜日 + 一个**漏跑日** mid（漏跑日不许进 prices）
#   600003 只在 O3 上榜，但 recent 跑过 —— 那天没上榜也要给价
#   600002 一根日线都没有 → 前端显 `-`，不许编数
DB.bars_upsert("daily_bar", [
    {"code": "600001", "trade_date": O3, "close": 10.5},
    {"code": "600001", "trade_date": recent, "close": 11.2},
    {"code": "600001", "trade_date": mid, "close": 9.9},
    {"code": "600003", "trade_date": recent, "close": 7.77},
])

from app.api import routes as R                      # noqa: E402

resp = R.main_force_matrix(days=90)

# ---- 3) 断言 ----------------------------------------------------------------
_keys = {"dates", "ran_dates", "missed_dates", "unfillable_dates",
         "rows", "total", "as_of", "days", "disclaimer"}
ok(_keys <= set(resp), "M2 响应键齐全（含 unfillable_dates）—— 缺 %s"
   % sorted(_keys - set(resp)))

_dates = resp["dates"]
ok(_dates == sorted(set(_dates)), "M3 dates 升序且无重复")

_ran, _miss = set(resp["ran_dates"]), set(resp["missed_dates"])
ok(_ran <= set(_dates) and _miss <= set(_dates) and not (_ran & _miss)
   and (_ran | _miss) == set(_dates),
   "M4 列分类不重不漏：ran=%d miss=%d dates=%d" % (len(_ran), len(_miss), len(_dates)))

ok(_ran == {O3, recent} and {O, O2, mid} <= _miss,
   "M5 夹具落位：ran=%s（要 %s/%s），O/O2/mid 都在 missed 里"
   % (sorted(_ran), O3, recent))

_rows = resp["rows"]
ok(resp["total"] == len(_rows) == 4,
   "M6 total == len(rows) == 4 —— %s/%s" % (resp["total"], len(_rows)))

ok(resp["as_of"] == recent, "M7 as_of == 库内最新打分日 —— %s" % resp["as_of"])

ok(resp["disclaimer"] == _DISC,
   "M8 disclaimer 原文透出 —— %r" % resp["disclaimer"])

ok(all(set(r["cells"]) <= _ran and r["hits"] == len(r["cells"])
       for r in _rows),
   "M9 格子只可能在跑过的列里，且 hits == 格子数（逐行）")

ok(all(r["best_rank"] == min(c["rank"] for c in r["cells"].values())
       and r["last_date"] == max(r["cells"]) for r in _rows),
   "M10 best_rank == 最好名次、last_date == 最近格子（逐行）")

_a = next(r for r in _rows if r["code"] == "600001")
ok(abs((_a["best_score"] or 0) - 90.5) < 1e-9,
   "M11 best_score 取历史最大 —— %s" % _a["best_score"])

ok([r["hits"] for r in _rows] == sorted((r["hits"] for r in _rows),
                                        reverse=True),
   "M12 主序 hits 降序 —— %s" % [r["hits"] for r in _rows])

ok([r["code"] for r in _rows] == ["600001", "600002", "600006", "600003"],
   "M13 同分先看 last_date、再看 best_rank —— %s"
   % [r["code"] for r in _rows])

ok(_a["name"] == "新名" and _a["industry"] == "新行业",
   "M14 同码跨日 name/industry 保留最新叫法 —— %s/%s"
   % (_a["name"], _a["industry"]))

ok(set(resp["unfillable_dates"]) == {O},
   "M15 unfillable == 超窗口且无池子的 O（O2 有池子、mid 在窗口内都不进）"
   " —— %s" % resp["unfillable_dates"])

# ---- 4) 每天的股价（prices）-------------------------------------------------
ok(_a["prices"] == {O3: 10.5, recent: 11.2},
   "M16 跑过的列给价、漏跑的 mid 不进 prices —— %s" % _a["prices"])

_b = next(r for r in _rows if r["code"] == "600003")
ok(_b["prices"].get(recent) == 7.77 and recent not in _b["cells"],
   "M17 没上榜那天也给价（值来自 daily_bar，不是打分价）—— %s"
   % _b["prices"])

_c = next(r for r in _rows if r["code"] == "600002")
ok(_c["prices"] == {},
   "M18 缺本地日线的日期不进 map（前端显 `-`，绝不编数）—— %s"
   % _c["prices"])

ok(len(_BLOCKED) == 0,
   "全程一个包都没发出去（blocked=%d）" % len(_BLOCKED))

print("ALL %d ASSERTIONS PASSED" % N[0])
