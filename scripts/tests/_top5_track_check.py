# -*- coding: utf-8 -*-
r"""「疑似主力建仓前5」跟踪（2026-11-11 拍板五口径）的**离线**回归。

每一条都对应一个当初会露馅的点：

  1) `roster()` 推导：rank 1-5 才进名单；first/last 只算**进前5**的日子；
     cells 保留全部名次（含进前5之前的，弹框要画）；deadline = 最后一次进
     前5 + 30 **自然日**，重复上榜自动顺延；到期**留在名单**只标状态；
     截止日当天仍算跟踪中（含等号）。
  2) `pull()` 取价：只拉跟踪中的票（到期一个请求都不发）；只拉本地**缺**的
     交易日那一段（一天不缺一次请求都不发、补齐后第二天不再发）；单票失败
     计数不拖垮、全部失败才抛（不许静默全绿）；落库只 `bars_upsert`
     （AST 钉死绝无 `bars_overwrite`），取价走 `backfill.fetch_daily`（不复权）。
  3) `GET /api/main-force/top5-track`：200 且形状对齐矩阵（dates/cells/
     prices/状态字段）；缺本地日线的日期不进 prices（前端显 `-` 不编数）；
     漏跑列进 missed_dates 不给价；**零外发**。
  4) scheduler 接线：`_plan` 分母 9 步（2026-11-11 加日线采集）、top5_track
     只装在打分轮；`_collect` 里**先日线采集、再打分后取价**；打分被跳过
     （当天已打过）取价照跑；`live=True` 落 `_state`；run_light 两样都不干。
  5) 源码级：`roster` 零联网；`pull` 的联网属性名 `pull` 已进 `_accept_check`
     的 `_NET_ATTR` 判据 —— 路由里调它，B1 的「全集 == 白名单」当场红。

全离线：开头封死 TCP + DNS，`fetch_daily` / `roster` 一律桩掉，一个包都不发。
跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_top5_track_check.py
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

sys.path.insert(0, r"D:\test-kanban\backend")

from app.config import settings                      # noqa: E402

import tempfile                                      # noqa: E402

# 夹具落在真 market.db 上就是污染 —— 临时库
_SCRATCH = Path(tempfile.mkdtemp(prefix="kanban_top5_"))
settings.db_path = _SCRATCH / "market.db"

from app import scheduler as sch                     # noqa: E402
from app.api.routes import router                    # noqa: E402
from app.fetchers import calendar as tcal            # noqa: E402
from app.pipeline import top5track                   # noqa: E402
from app.store import db                             # noqa: E402
from fastapi import FastAPI                          # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

N = [0]


def ok(cond, msg):
    N[0] += 1
    if not cond:
        print("FAIL #%d  %s" % (N[0], msg))
        sys.stdout.flush()
        sys.exit(1)
    print("ok   #%d  %s" % (N[0], msg))


def _plus30(d):
    return (datetime.strptime(d, "%Y%m%d") + timedelta(days=30)).strftime("%Y%m%d")


def _nextday(d):
    return (datetime.strptime(d, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")


# ===================== 1) 夹具：main_force 快照 + daily_bar =================
# 日期全取**真实日历**：col_dates 走 cached_trade_dates()，夹具日必须是
# 交易日才进轴（2026-10 起有夹具撞日历的前科）。
today = datetime.now().strftime("%Y%m%d")
cal_all = [d for d in tcal.cached_trade_dates() if d <= today]
ok(len(cal_all) >= 60, "本地日历至少 60 个交易日（got %d）" % len(cal_all))
WIN = cal_all[-6:]        # T1/T2 的进前5窗口（最近 6 个交易日）
PRE = cal_all[-50]        # T1 进前5**之前**的一次第 9 名（只该进 cells）
OLD = cal_all[-46]        # E1 的老票日：+30 自然日也早过期


def _row(code, rank, name, score=80.0):
    return {"code": code, "name": name, "industry": "测试行业",
            "rank": rank, "score": score}


_snap = lambda d, rows: db.save_one(  # noqa: E731
    "main_force", d, {"rows": rows, "disclaimer": "研究结论，非投资建议"})

_snap(PRE, [_row("610001", 9, "甲")])                          # 进前5之前
_snap(OLD, [_row("610003", 5, "丙"), _row("610004", 7, "壬")])  # 老票 + 进不了的
_snap(WIN[0], [_row("610001", 1, "甲"), _row("610002", 3, "乙")])
_snap(WIN[2], [_row("610002", 2, "乙")])                        # 重复上榜 → 顺延


def _bar(code, d, close):
    db.bars_upsert("daily_bar", [{"code": code, "trade_date": d,
                                  "open": close, "high": close, "low": close,
                                  "close": close, "volume": 1, "amount": 1.0,
                                  "source": "test"}])


for i, d in enumerate(WIN):
    _bar("610001", d, 10.0 + i)   # T1 六天齐全：一次请求都不该发
for d in WIN[:3]:
    _bar("610002", d, 20.0)       # T2 缺后三天
_bar("610003", OLD, 8.8)          # E1（将到期）只有老票那天的价

# ===================== 2) roster 推导口径 ====================================
ok(top5track.TRACK_DAYS == 30, "一个月 = 30 自然日（TRACK_DAYS=%d）"
   % top5track.TRACK_DAYS)

ros = top5track.roster()
codes = [r["code"] for r in ros]
ok(set(codes) == {"610001", "610002", "610003"},
   "rank≤5 才进名单，610004（第 7）不进：%s" % codes)
t1 = next(r for r in ros if r["code"] == "610001")
t2 = next(r for r in ros if r["code"] == "610002")
e1 = next(r for r in ros if r["code"] == "610003")

ok(t1["first_date"] == WIN[0],
   "first_date 只算进前5的日子（got %s，第 9 名那天 %s 不算）"
   % (t1["first_date"], PRE))
ok(t1["cells"].get(PRE, {}).get("rank") == 9,
   "进前5之前的名次仍进 cells（弹框要画，got %r）" % t1["cells"].get(PRE))
ok(t1["deadline"] == _plus30(WIN[0]) and t1["status"] == "跟踪中",
   "T1 截止 = 首次进前5 + 30 自然日、跟踪中（deadline=%s）" % t1["deadline"])
ok(t2["hits"] == 2 and t2["last_date"] == WIN[2],
   "重复上榜：hits=2、last 推到 %s" % t2["last_date"])
ok(t2["deadline"] == _plus30(WIN[2]) and t2["deadline"] != _plus30(WIN[0]),
   "截止日随后一次进前5顺延（%s）" % t2["deadline"])
ok(e1["status"] == "已到期" and e1["deadline"] == _plus30(OLD)
   and e1["deadline"] < today,
   "到期票留在名单只标状态（deadline=%s < today=%s）" % (e1["deadline"], today))
b1 = {r["code"]: r["status"] for r in top5track.roster(as_of=e1["deadline"])}
b2 = {r["code"]: r["status"] for r in top5track.roster(as_of=_nextday(e1["deadline"]))}
ok(b1.get("610003") == "跟踪中", "截止日**当天**仍在跟踪期（含等号）")
ok(b2.get("610003") == "已到期", "过一天即到期")
ok([r["status"] for r in ros] == ["跟踪中", "跟踪中", "已到期"],
   "跟踪中排前、已到期殿后：%s" % [r["status"] for r in ros])
ok((t1["best_rank"], t2["best_rank"], e1["best_rank"]) == (1, 2, 5),
   "best_rank = 进前5区间的最好名次：%r"
   % {"t1": t1["best_rank"], "t2": t2["best_rank"], "e1": e1["best_rank"]})

# ===================== 3) 端点 /api/main-force/top5-track ===================
c = TestClient(FastAPI())
c.app.include_router(router)
resp = c.get("/api/main-force/top5-track")
ok(resp.status_code == 200,
   "GET /api/main-force/top5-track -> 200（got %d）" % resp.status_code)
b = resp.json()
ok(set(b) == {"dates", "ran_dates", "missed_dates", "rows", "total",
              "as_of", "tracking", "expired", "disclaimer"},
   "形状 keys 对齐矩阵：%s" % sorted(b))
ok((b["total"], b["tracking"], b["expired"]) == (3, 2, 1),
   "总数/跟踪中/已到期 = 3/2/1（got %s/%s/%s）"
   % (b["total"], b["tracking"], b["expired"]))
bt1 = next(r for r in b["rows"] if r["code"] == "610001")
bt2 = next(r for r in b["rows"] if r["code"] == "610002")
be1 = next(r for r in b["rows"] if r["code"] == "610003")
ok(bt1["cells"].get(PRE, {}).get("rank") == 9,
   "端点 rows 同样保留进前5前的名次格")
ok(bt1["prices"].get(WIN[0]) == 10.0,
   "价取 daily_bar 不复权收盘价（got %r）" % bt1["prices"].get(WIN[0]))
ok(WIN[3] not in bt2["prices"],
   "缺本地日线的日期不进 prices、前端显 -：%s" % sorted(bt2["prices"]))
ok(WIN[1] not in bt1["prices"] and WIN[1] in b["missed_dates"],
   "没跑打分的交易日：进 missed_dates 且不给价（与矩阵同口径）")
ok(be1["prices"] == {OLD: 8.8},
   "已到期票的价只有它自己那段（got %r）" % be1["prices"])
ok(all(k in bt1 for k in ("first_date", "deadline", "status")),
   "行带跟踪三字段 first_date/deadline/status")
ok(b["as_of"] == WIN[2], "as_of = 库内最新打分日（got %s）" % b["as_of"])
ok(b["dates"] and b["dates"][0] == OLD,
   "日期轴从最早的「首次进前5」日起（got %s）" % (b["dates"][:1],))
ok("非投资建议" in b["disclaimer"], "带 disclaimer（got %r）" % b["disclaimer"][:20])

# ===================== 4) pull 取价口径 ======================================
calls = []


def _fake_fetch(code, s, e):
    calls.append((code, s, e))
    return [{"code": code, "trade_date": d, "close": 99.0, "open": 99.0,
             "high": 99.0, "low": 99.0, "volume": 1, "amount": 1.0,
             "source": "fake"} for d in cal_all if s <= d <= e]


_orig_fetch = top5track.fetch_daily
_orig_roster = top5track.roster
top5track.fetch_daily = _fake_fetch
try:
    info = top5track.pull(today)
finally:
    top5track.fetch_daily = _orig_fetch

ok((info["tracking"], info["expired"]) == (2, 1),
   "pull 名单：跟踪中 2、已到期 1（got %s/%s）"
   % (info["tracking"], info["expired"]))
ok(calls == [("610002", WIN[3], WIN[5])],
   "只发一次：T1 齐全不发、到期 E1 不发、T2 只补缺段（got %r）" % calls)
ok(info["fetched"] == 1 and info["failed"] == [] and info["rows"] == 3,
   "fetched=1 / failed 空 / 新落 3 行（got %s/%r/%s）"
   % (info["fetched"], info["failed"], info["rows"]))


def _boom(*a, **k):
    raise AssertionError("数据已齐还发请求")


top5track.fetch_daily = _boom
try:
    info2 = top5track.pull(today)
finally:
    top5track.fetch_daily = _orig_fetch
ok(info2["fetched"] == 0 and info2["rows"] == 0 and not info2["failed"],
   "缺日已补齐：两票一个请求都不发（fetched=0，got %r）" % info2)

# ---- 部分失败：一只成、一只拒 —— 计数不拖垮，返回里说清谁失败 ----
_stub = [{"code": c_, "name": "", "industry": "", "first_date": WIN[0],
          "last_date": WIN[0], "hits": 1, "best_rank": 1, "cells": {},
          "deadline": _plus30(WIN[0]), "status": "跟踪中"}
         for c_ in ("619998", "619997")]


def _half(code, s, e):
    if code == "619997":
        raise ConnectionError("源拒")
    return [{"code": code, "trade_date": d, "close": 50.0, "open": 50.0,
             "high": 50.0, "low": 50.0, "volume": 1, "amount": 1.0,
             "source": "fake"} for d in cal_all if s <= d <= e]


top5track.roster = lambda *a, **k: list(_stub)
top5track.fetch_daily = _half
try:
    info3 = top5track.pull(today)
finally:
    top5track.roster = _orig_roster
    top5track.fetch_daily = _orig_fetch
ok(info3["fetched"] == 1 and len(info3["failed"]) == 1
   and "619997" in info3["failed"][0],
   "单票失败计数不拖垮：%r" % info3["failed"])


# ---- 全军覆没：必须抛，静默返回会让任务记 success 却没拉到价 ----
# 用**没落过 bars 的新码**：沿用 #30 的码会因「数据已齐」直接算完成，测不出全败
def _deny(*a, **k):
    raise ConnectionError("全线拒")


_stub2 = [dict(r, code=c) for r, c in zip(_stub, ("619996", "619995"))]
top5track.roster = lambda *a, **k: list(_stub2)
top5track.fetch_daily = _deny
try:
    try:
        top5track.pull(today)
        raised = False
    except RuntimeError:
        raised = True
finally:
    top5track.roster = _orig_roster
    top5track.fetch_daily = _orig_fetch
ok(raised, "全部失败时 pull 抛 RuntimeError 而不是静默成功")

# ---- 空名单：一只跟踪中的都没有 —— 返回 0 且一个请求都不发（_deny 兜底） ----
top5track.roster = lambda *a, **k: [dict(_stub[0], status="已到期")]
top5track.fetch_daily = _deny
try:
    info4 = top5track.pull(today)
finally:
    top5track.roster = _orig_roster
    top5track.fetch_daily = _orig_fetch
ok(info4["tracking"] == 0 and info4["expired"] == 1 and info4["fetched"] == 0,
   "空名单直接返回 0，不发请求（got %r）" % info4)

# ---- 源码级 ----
_tree = ast.parse(Path(top5track.__file__).read_text(encoding="utf-8"))


def _fn(name):
    return next(n for n in _tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _names(fn):
    out = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    out |= {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
    return out


_rn = _names(_fn("roster"))
ok(not ({"fetch_daily", "requests", "urlopen", "getaddrinfo", "tcal"} & _rn),
   "roster 源码零联网（命中即红）：%s"
   % sorted({"fetch_daily", "requests"} & _rn))
_pn = _names(_fn("pull"))
ok("bars_upsert" in _pn and "bars_overwrite" not in _pn,
   "pull 只 upsert 不 overwrite（不覆盖历史行情）")
ok("fetch_daily" in _pn,
   "取价走 backfill.fetch_daily（不复权 ADJUST 钉在 backfill）")

# ===================== 5) scheduler 接线 ====================================
ok(sch._plan(True)[-1] == "top5_track" and "top5_track" not in sch._plan(False),
   "top5_track 是打分轮的最后一步、run_light 不装：%s" % sch._plan(True))
ok(len(sch._plan(True)) == 9, "进度条分母 9 步（got %d）" % len(sch._plan(True)))

_order: list = []
_pulled: list = []
_orig_pool, _orig_sector = sch.fetch_pool, sch.fetch_sector_flow
_orig_score, _orig_tt = sch._score_and_record, sch.top5track
_orig_bar = sch.fetch_daily_bar
sch.fetch_pool = lambda kind, target: 7
sch.fetch_sector_flow = lambda kind: 5
# 日线采集步（2026-11-11 加进打分轮）：不桩会去真抓快照（socket 守卫会红）
sch.fetch_daily_bar = lambda t: (_order.append("daily_bar"), 4)[1]
sch._score_and_record = lambda t: (_order.append("score"), 3)[1]
sch.top5track = types.SimpleNamespace(
    pull=lambda t: (_order.append("track"), _pulled.append(t),
                    {"tracking": 2})[2])
try:
    with sch._lock:
        sch._state.update(steps={}, current_step="", plan=sch._plan(True),
                          running=True, kind="manual", date="20991231")
    res, failed, partial = sch._collect("20991231", False, True, live=True)
    ok(res.get("top5_track") == {"tracking": 2}
       and sch._state["steps"].get("top5_track") == {"tracking": 2},
       "top5_track 结果落进 res 与 _state（%r）"
       % sch._state["steps"].get("top5_track"))
    ok(not failed and not partial,
       "没失败没不完整（failed=%r partial=%r）" % (failed, partial))
    ok(_order == ["daily_bar", "score", "track"],
       "**先日线采集、再打分后取价**（顺序 %r）" % _order)
    ok(_pulled == ["20991231"], "pull 拿到 target（%r）" % _pulled)

    # 打分被跳过（当天已打过）时，取价照跑 —— 每天都要拉价
    res_skip, f_skip, p_skip = sch._collect(WIN[2], False, True, live=True)
    ok(str(res_skip.get("main_force", "")).startswith("已打分")
       and res_skip.get("top5_track") == {"tracking": 2}
       and not f_skip,
       "打分跳过、取价照跑（main_force=%r）" % res_skip.get("main_force"))

    # run_light：不打分也不取价
    res_light, f_light, p_light = sch._collect(WIN[0], False, False,
                                               live=False)
    ok("top5_track" not in res_light and len(_pulled) == 2,
       "run_light 不打分也不取价（pulled=%r）" % _pulled)
finally:
    sch.fetch_pool, sch.fetch_sector_flow = _orig_pool, _orig_sector
    sch._score_and_record, sch.top5track = _orig_score, _orig_tt
    sch.fetch_daily_bar = _orig_bar

ok(len(_BLOCKED) == 0,
   "全程一个包都没发出去（blocked=%d）" % len(_BLOCKED))

print("ALL %d ASSERTIONS PASSED" % N[0])
