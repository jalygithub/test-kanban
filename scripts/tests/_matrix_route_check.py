# -*- coding: utf-8 -*-
r"""上榜矩阵**路由接线**的离线回归，13 条（2026-11-03 重写）。

原版在 Temp 清理时丢失，按 AGENTS「丢了照着重写」的口径依据当前
`routes.py` 重建，计数保持 **12** 条不变；同日加「每天的股价」
（R12 单一出处）后为 **13** 条。分工：`_matrix_check` 钉响应
**内容**（聚合语义），这份钉**接线**（注册、方法、预检顺序、秒拒语义）。

补跑修复（2026-11-03）的核心钉子在这份里：

  · R4/R5 矩阵与 refresh **都**走 `mainforce.pool_unavailable_reason` ——
    单一出处；两边各算一套的话，矩阵亮着按钮、后端却 400 就会复发。
  · R6/R7 预检语句排在 `threading.Thread` 与 `_MF_STATE.update` **之前** ——
    2026-11-02 那次补跑 20260929 烧完 4 分钟取数才死在跌停池 30 天闸门上，
    且状态先置了 running；预检必须在起跑前。
  · R8-R10 真调 `refresh(date=过期日)`：400 且文案说清「永久不可补」，
    线程与打分函数桩成「一调就炸」证明一个都没碰、状态没被污染。

**不测** refresh 的成功路径 —— 那会真起线程去抓全市场快照（联网）。

夹具库（临时目录）+ 开头封死 TCP + DNS，全离线。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_matrix_route_check.py
"""
import ast
import socket
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

# ---- 1) 空夹具库（临时目录）：真实 market.db 只读不碰、更不写 ---------------
settings.db_path = Path(tempfile.mkdtemp(prefix="kanban_matrix_rt_")) / "market.db"
from app.store import db as DB                        # noqa: E402

DB._conn = None
DB.init_db()

from fastapi import HTTPException                    # noqa: E402

from app.api import routes as R                       # noqa: E402

N = [0]


def ok(cond, msg):  # noqa: ANN001
    N[0] += 1
    if not cond:
        print("FAIL #%d  %s" % (N[0], msg))
        sys.exit(1)
    print("ok   #%d  %s" % (N[0], msg))


# ---- 2) 注册与方法 -----------------------------------------------------------
def _methods(path):  # noqa: ANN001
    for r in R.router.routes:
        if getattr(r, "path", "") == path:
            return set(getattr(r, "methods", None) or [])
    return set()


ok("GET" in _methods("/api/main-force/matrix"),
   "R1 /main-force/matrix 注册为 GET —— %s" % sorted(_methods("/api/main-force/matrix")))
ok("POST" in _methods("/api/main-force/refresh"),
   "R2 /main-force/refresh 注册为 POST —— %s" % sorted(_methods("/api/main-force/refresh")))
ok("GET" in _methods("/api/main-force/status"),
   "R3 /main-force/status 注册为 GET —— %s" % sorted(_methods("/api/main-force/status")))

# ---- 3) 源码级接线：可得性预检的单一出处与语句顺序 ---------------------------
_SRC = Path(R.__file__).read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)


def _fn(name):  # noqa: ANN001
    return next(n for n in _TREE.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == name)


def _calls(fn, attr):  # noqa: ANN001
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Attribute) and f.attr == attr:
                return True
            if isinstance(f, ast.Name) and f.id == attr:
                return True
    return False


def _has_400(fn):  # noqa: ANN001
    # HTTPException 在 routes 里是**直接 import 的 Name**，不是 Attribute ——
    # 只认 `x.HTTPException` 会把预检判成不存在（重写时真踩过）。
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call) or not n.args:
            continue
        f = n.func
        is_exc = ((isinstance(f, ast.Attribute) and f.attr == "HTTPException")
                  or (isinstance(f, ast.Name) and f.id == "HTTPException"))
        if is_exc and isinstance(n.args[0], ast.Constant) \
                and n.args[0].value == 400:
            return True
    return False


def _stmt_index(fn, pred):  # noqa: ANN001
    """第一个**顶层语句**里含匹配节点的下标（找 -1 = 没有）。"""
    for i, st in enumerate(fn.body):
        if any(pred(n) for n in ast.walk(st)):
            return i
    return -1


_MX = _fn("main_force_matrix")
_RF = _fn("main_force_refresh")

ok(_calls(_MX, "pool_unavailable_reason"),
   "R4 矩阵路由调 pool_unavailable_reason（unfillable 后端算，前端不猜日期）")

ok(_calls(_RF, "pool_unavailable_reason") and _has_400(_RF),
   "R5 refresh 起跑前预检：调同一个 pool_unavailable_reason 且有 HTTPException(400)")

_i_reason = _stmt_index(_RF, lambda n: isinstance(n, ast.Call)
                       and ((isinstance(n.func, ast.Attribute)
                             and n.func.attr == "pool_unavailable_reason")
                            or (isinstance(n.func, ast.Name)
                                and n.func.id == "pool_unavailable_reason")))
_i_thread = _stmt_index(_RF, lambda n: isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "Thread")
_i_state = _stmt_index(_RF, lambda n: isinstance(n, ast.Call)
                       and isinstance(n.func, ast.Attribute)
                       and n.func.attr == "update"
                       and isinstance(n.func.value, ast.Name)
                       and n.func.value.id == "_MF_STATE")

ok(0 <= _i_reason < _i_thread,
   "R6 预检语句排在起线程之前（reason@%d < Thread@%d）" % (_i_reason, _i_thread))
ok(0 <= _i_reason < _i_state,
   "R7 预检语句排在状态置位之前（reason@%d < _MF_STATE@%d）"
   % (_i_reason, _i_state))

# ---- 4) 真调：过期日 0 秒秒拒，且一个线程都不起 ------------------------------
import threading                                          # noqa: E402

_real_thread = threading.Thread
_real_sac = R.scheduler.score_and_capture


def _boom(*a, **k):  # noqa: ANN001, ANN201
    raise AssertionError("BOMB: 预检必须在起线程/打分之前")


threading.Thread = _boom
R.scheduler.score_and_capture = _boom
_exc = None
_bomb = None
try:
    try:
        R.main_force_refresh(date="20250101")
    except HTTPException as e:
        _exc = e
    except AssertionError as e:
        _bomb = str(e)
finally:
    threading.Thread = _real_thread
    R.scheduler.score_and_capture = _real_sac

ok(isinstance(_exc, HTTPException) and _exc.status_code == 400,
   "R8 过期日秒拒 400 —— exc=%r bomb=%r"
   % (type(_exc).__name__ if _exc else None, _bomb))
ok(isinstance(_exc, HTTPException) and "永久不可补" in str(_exc.detail),
   "R9 拒绝文案说清「永久不可补」—— %r"
   % (str(_exc.detail)[:60] if _exc else None))
ok(_bomb is None and R._MF_STATE.get("running") is False,
   "R10 线程/打分一个没碰、状态没被污染 —— bomb=%r running=%r"
   % (_bomb, R._MF_STATE.get("running")))

# ---- 5) 空库早退形状与零外发 -------------------------------------------------
resp = R.main_force_matrix(days=90)
ok(isinstance(resp, dict) and resp.get("unfillable_dates") == []
   and "missed_dates" in resp and "dates" in resp,
   "R11 空库早退也回 unfillable_dates: [] 与全套键 —— %s" % sorted(resp))

# ---- 6) 每天的股价的单一出处 -----------------------------------------------
_i_bars = _stmt_index(_MX, lambda n: isinstance(n, ast.Call)
                      and ((isinstance(n.func, ast.Attribute)
                            and n.func.attr == "bars_load_window")
                           or (isinstance(n.func, ast.Name)
                               and n.func.id == "bars_load_window")))
_lit_daily_bar = any(isinstance(n, ast.Constant) and n.value == "daily_bar"
                     for n in ast.walk(_MX))
# 打分 payload 里的 price 是 daily_history(adjust="qfq") 的前复权价，
# 混进同一列会把除权日读成暴跌 —— 只认 daily_bar 的不复权收盘价。
_qfq_price = any(isinstance(n, ast.Subscript)
                 and isinstance(n.slice, ast.Constant)
                 and n.slice.value == "price"
                 for n in ast.walk(_MX))
ok(_i_bars >= 0 and _lit_daily_bar and not _qfq_price,
   "R12 股价单一出处 db.bars_load_window(\"daily_bar\")，不取打分价 qfq"
   " —— bars@%d daily_bar=%s qfq=%s" % (_i_bars, _lit_daily_bar, _qfq_price))

ok(len(_BLOCKED) == 0,
   "R13 全程一个包都没发出去（blocked=%d）" % len(_BLOCKED))

print("ALL %d ASSERTIONS PASSED" % N[0])
