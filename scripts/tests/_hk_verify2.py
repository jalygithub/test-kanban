# -*- coding: utf-8 -*-
r"""港股 P0 回归（**离线**）—— 重写已丢失的 `_hk_verify2.py`（2026-10-12）。

**住在 `scripts\tests\` 而不是临时目录**：上一版之所以丢，就是因为它躺在
`C:\Users\...\Temp\opencode` 里，`docs/04` 的「待跑脚本」随之变成一个永远
跑不掉的悬空引用。这次挪进仓库，路径写死可查。

`docs/04` §1 的 P0 落地记录列明了这个脚本该覆盖的三块，本文件照此断言：

  1) **NaN 修复断言** —— `_num` / `_f` / `_stats`。当年的 bug 是 `sum/len` 求均值
     时 `NaN` 是 truthy，全 NaN 的换手率会算成 `nan%`；修复后必须是 `None`
     （前端显示 `-`），**既不是 `nan` 也不是 `0.00`** —— 显示 0.00 比显示 `-`
     更误导人。
  2) **关注增 → 查 → 删** —— 含「位数即市场」的 400 硬边界与代码归一化。
  3) **A 股回归** —— 港股改动不能把 A 股的 6 位口径带坏。

**与原计划的差别**：`docs/04` 说这个脚本要「连上热点后执行」，那是给**行情接口
复验**用的（东财在公司网被 RST）。本文件默认**全离线**：开头封死 TCP + DNS，
所有外部依赖（腾讯快照、`track.detail`）一律桩掉，所以任何时候都能跑、也不会
替用户朝股票接口发包。真正的联网复验收在 `--live` 后面，需要显式开关。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe <this file>
  D:\test-kanban\backend\.venv\Scripts\python.exe <this file> --live   # 需热点，未跑
"""
import socket
import sys

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

from app.config import settings                      # noqa: E402

import tempfile                                      # noqa: E402
from pathlib import Path                             # noqa: E402

# 路由测试会真写关注表 —— 落在真 market.db 上就是污染。
_SCRATCH = Path(tempfile.mkdtemp(prefix="kanban_hk_"))
settings.db_path = _SCRATCH / "market.db"

import pandas as pd                                  # noqa: E402
from app.api import routes as routes_mod             # noqa: E402
from app.api.routes import router                    # noqa: E402
from app.fetchers import hk as hk_fetch              # noqa: E402
from app.fetchers.client import market_of, norm_code  # noqa: E402
from app.metrics import track                        # noqa: E402
from app.store import db                             # noqa: E402
from fastapi import FastAPI, HTTPException           # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

N = [0]


def ok(cond, msg):
    N[0] += 1
    if not cond:
        print("FAIL #%d  %s" % (N[0], msg))
        sys.stdout.flush()
        sys.exit(1)
    print("ok   #%d  %s" % (N[0], msg))


# =========================== 1) 代码归一化与判市场 ===========================
# 「位数即市场」是港股 P0 唯一的硬边界：A 股恒 6 位、港股恒 5 位。
ok(norm_code("hk00700") == "00700",
   "hk 前缀优先于默认 market=cn（漏传也不会补成 6 位）")
ok(norm_code("00700.HK") == "00700", ".HK 后缀归一化")
ok(norm_code("00700.HK", "cn") == "00700",
   ".HK 后缀**优先于显式 market** —— 调用方写错也拉得回来")
ok(norm_code("700", "hk") == "00700", "缺位补零：700 -> 00700")
ok(norm_code("00700", "hk") == "00700", "港股已成型的 5 位原样")
ok(norm_code("600519", "hk") == "",
   "6 位贴 market=hk -> 空串，这是路由 400 的唯一来源")
ok(norm_code("12345678", "hk") == "", "8 位贴港股 -> 空串")
ok(norm_code(None) == "" and norm_code(float("nan")) == "",
   "None / NaN 归空，不产出 'nan'")
ok(norm_code("600519") == "600519", "A 股回归：6 位原样")
ok(norm_code("000001") == "000001",
   "A 股回归：pandas 掉零的 1 补回 000001")
ok(norm_code("00700", "cn") == "000700",
   "**已记录的不对称**：5 位 + market=cn 会被补成 6 位 000700，"
   "这个方向**不报 400**（只有反方向报）")
ok(market_of("00700") == "hk" and market_of("hk00700") == "hk"
   and market_of("00700.HK") == "hk",
   "market_of 三种写法都判成 hk")
ok(market_of("600519") == "cn", "market_of 6 位判成 cn")
ok(market_of("700") == "hk",
   "3 位数字判成 hk —— 文档警告过的场景：pandas 原始值不能直接拿来判")


# =============================== 2) NaN 修复 =================================
ok(track._num(float("nan")) is None, "_num(NaN) -> None（NaN 是 truthy）")
ok(track._num(None) is None and track._num("") is None
   and track._num("abc") is None,
   "_num 空值/非数 -> None")
ok(track._num("1.5") == 1.5, "_num 可解析的字符串仍然收下")
ok(track._f(float("nan")) == 0.0,
   "_f(NaN) -> 0.0（与 _num 分工：这个字段缺了该归 0）")


def _df(turnover, amount):
    return pd.DataFrame({
        "date": ["20261001", "20261002", "20261005"],
        "close": [10.0, 11.0, 12.0],
        "high": [10.5, 11.5, 12.5],
        "low": [9.5, 10.5, 11.5],
        "turnover": turnover,
        "amount": amount,
    })


# 新浪港股日线（与 A 股腾讯备源）整列没有换手率 -> 整列 NaN
nan_col = [float("nan")] * 3
s_nan = track._stats(_df(nan_col, nan_col), "20261001", None)
ok(s_nan["avg_turnover"] is None,
   "换手率整列 NaN -> avg_turnover is None（前端显示 '-'）")
ok(s_nan["avg_turnover"] != 0.0 and s_nan["avg_turnover"] == s_nan["avg_turnover"],
   "**不是 0.00、也不是 nan** —— 显示 0.00 比 '-' 更误导人")
ok(s_nan["avg_amount"] is None, "成交额整列 NaN -> None")
ok(s_nan["pct"] == 20.0, "pct 不受 NaN 列影响：(12/10-1)*100 = %s" % s_nan["pct"])

s_ok = track._stats(_df([1.0, 2.0, 3.0], [100.0, 200.0, 300.0]),
                    "20261001", None)
ok(s_ok["avg_turnover"] == 2.0, "有值时均值照算：%s" % s_ok["avg_turnover"])
ok(s_ok["avg_amount"] == 200.0, "成交额均值：%s" % s_ok["avg_amount"])
ok(s_ok["trading_days"] == 3 and s_ok["calendar_days"] == 4,
   "3 根 K 线 / 10-01~10-05 跨 4 个自然日（got %s / %s）"
   % (s_ok["trading_days"], s_ok["calendar_days"]))


# ================================ 3) 路由层 =================================
app = FastAPI()
app.include_router(router)
c = TestClient(app)

SRC = "20261001"

# --- 未关注时 series 必须 404，而不是先去拉一把行情
r = c.get("/watchlist/00700/series")
ok(r.status_code == 404, "未关注 -> 404（got %d），不先发行情请求" % r.status_code)

# --- 港股关注：名字由腾讯补（桩掉，不联网）
_calls = []
hk_fetch.quote = lambda code: (_calls.append(code),
                               {"name": "腾讯控股", "price": 432.0})[-1]
r = c.post("/api/watchlist", json={"code": "00700", "market": "hk",
                                   "source_date": SRC})
ok(r.status_code == 200, "关注 00700 -> 200（got %d）" % r.status_code)
hit = [x for x in r.json()["items"] if x["code"] == "00700"]
ok(len(hit) == 1, "列表里有 00700")
ok(hit and hit[0].get("market") == "hk", "market 存成 hk：%r"
   % (hit[0].get("market") if hit else None))
ok(hit and hit[0].get("name") == "腾讯控股", "没打名字时由快照补上：%r"
   % (hit[0].get("name") if hit else None))
ok(_calls == ["00700"], "补名只问了一次：%s" % _calls)

# --- 归一化：hk00700 / 700 都落成 00700，不会存出一条查不到行情的假关注
r = c.post("/api/watchlist", json={"code": "hk00700", "source_date": SRC})
ok(r.status_code == 200 and any(x["code"] == "00700" for x in r.json()["items"]),
   "hk00700 归一化成 00700（got %d）" % r.status_code)

# --- 位数硬边界：错市场必须 400，且**不入库**
r = c.post("/api/watchlist", json={"code": "600519", "market": "hk",
                                   "source_date": SRC})
ok(r.status_code == 400, "6 位贴 market=hk -> 400（got %d）" % r.status_code)
ok(db.watch_get("600519") is None, "被拒的代码没有落库")

# --- 显式 market 优先：700 + cn 补成 000700（记录在案的不对称行为）
r = c.post("/api/watchlist", json={"code": "700", "market": "cn",
                                   "source_date": SRC})
ok(r.status_code == 200, "700 + market=cn -> 200（got %d）" % r.status_code)
ok(db.watch_get("000700") is not None, "归一化后存的是 000700")

# --- 快照拿不到名字也不能让关注失败（quote 的契约是失败返回 {}）
hk_fetch.quote = lambda code: {}
r = c.post("/api/watchlist", json={"code": "00981", "market": "hk",
                                   "source_date": SRC})
ok(r.status_code == 200, "快照空结果时关注仍成功（got %d）" % r.status_code)
item = db.watch_get("00981")
ok(item is not None and item.get("name") == "00981",
   "名字回落成代码：%r" % (item.get("name") if item else None))

# --- quote 自己吞异常：_qt_text 炸了也要返回 {}，不能往上抛
_real_qt = hk_fetch._qt_text


def _boom(code):  # noqa: ANN001
    raise RuntimeError("simulated network failure")


hk_fetch._qt_text = _boom
try:
    got = hk_fetch.quote("00700")
finally:
    hk_fetch._qt_text = _real_qt
ok(got == {}, "quote 内部吞掉异常 -> {}（got %r），不会把 500 抛给关注" % got)

# --- A 股回归：6 位关注照常，且**不会**去问港股快照
_a_calls = []
hk_fetch.quote = lambda code: (_a_calls.append(code), {"name": "x"})[-1]
r = c.post("/api/watchlist", json={"code": "600519", "market": "cn",
                                   "name": "贵州茅台", "source_date": SRC})
ok(r.status_code == 200, "A 股关注 600519 -> 200（got %d）" % r.status_code)
ok(_a_calls == [], "A 股不碰港股快照：%s" % _a_calls)
a = db.watch_get("600519")
ok(a is not None and a.get("market") == "cn" and a.get("name") == "贵州茅台",
   "A 股的 market/name 都对（got %s / %s）"
   % ((a or {}).get("market"), (a or {}).get("name")))

# --- 增 -> 查 -> 删
r = c.get("/api/watchlist")
codes = {x["code"] for x in r.json()["items"]}
ok({"00700", "000700", "00981", "600519"} <= codes,
   "列表含全部四只：%s" % sorted(codes))
# 「查」这一步走 db —— 路由**没有** `GET /watchlist/{code}`，
# 只有 list / post / delete / series 四条（先 grep 路由表确认过，别再想当然）。
ok((db.watch_get("00700") or {}).get("market") == "hk",
   "按代码查得到且 market=hk：%r" % (db.watch_get("00700") or {}))

# --- series 走 track.detail，market 必须透传过去（决定用恒指还是沪深300）
_seen = {}


def _fake_detail(code, source_date, refresh=False, market="cn"):  # noqa: ANN001
    _seen.update(code=code, source_date=source_date, market=market)
    return {"bars": [], "stats": {}}


track.detail = _fake_detail
r = c.get("/api/watchlist/00700/series")
ok(r.status_code == 200, "已关注 -> 200（got %d）" % r.status_code)
ok(_seen.get("market") == "hk",
   "series 把 market=hk 透传给 track.detail（got %r）" % _seen.get("market"))
ok(_seen.get("source_date") == SRC, "透传建仓日：%r" % _seen.get("source_date"))

r = c.get("/api/watchlist/600519/series")
ok(_seen.get("market") == "cn", "A 股透传 market=cn（got %r）"
   % _seen.get("market"))

# --- 删
r = c.delete("/api/watchlist/00700")
ok(r.status_code == 200 and db.watch_get("00700") is None,
   "删除 -> 200 且库里没了（got %d）" % r.status_code)
r = c.delete("/api/watchlist/00700")
ok(r.status_code == 404, "再删一次 -> 404（got %d）" % r.status_code)

# --- /realtime 的 codes 解析：**直接测纯函数 `_live_items`，不走 HTTP** ——
#     走路由会朝腾讯发包，被 0) 的闸门挡下来，`_BLOCKED` 就不再是 0，
#     最后那条「零外发」断言会自相矛盾。
got = routes_mod._live_items("600519,00700")
ok([x["market"] for x in got] == ["cn", "hk"],
   "6 位 + 5 位混排各判对市场：%s" % got)
ok([x["code"] for x in routes_mod._live_items("hk00700,600519")] == ["00700",
                                                                    "600519"],
   "混排时 hk00700 也归一化成 00700")
ok(len(routes_mod._live_items("600519,600519,00700")) == 2, "重复代码去重")


def _code_of(fn):  # noqa: ANN001
    try:
        fn()
    except HTTPException as exc:
        return exc.status_code
    return None


ok(_code_of(lambda: routes_mod._live_items("")) == 400, "空 codes -> 400")
ok(_code_of(lambda: routes_mod._live_items("1234567")) == 400,
   "7 位 -> 400（位数即市场的硬边界）")
ok(_code_of(lambda: routes_mod._live_items("abc")) == 400, "非数字 -> 400")
ok(routes_mod._live_items("12345")[0]["market"] == "hk",
   "5 位纯数字一律判成港股")
ok(len(_BLOCKED) == 0,
   "离线段一个包都没发出去（blocked=%d）" % len(_BLOCKED))

# ------------------------------- 可选：--live -------------------------------
if "--live" in sys.argv:
    print("LIVE 未实现/未授权：需要手机热点，且代用户朝股票接口发包须先经同意")
    print("     （东财在公司网被 RST，新浪港股日线与腾讯 qt 是公司网通的备选）")
else:
    print("(跳过 --live 联网复验：默认离线，联网需显式开关 + 用户授权)")

print("ALL %d ASSERTIONS PASSED" % N[0])
