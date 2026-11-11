# -*- coding: utf-8 -*-
r"""历史回填工具的**离线**回归，2026-10-16（阶段 5 扩到 129 条）。

对应 `app/pipeline/backfill.py` 与 `store/db.py` 的 `bars_*` / `bar_gaps`，
对应 `routes.py` 的 `/api/pipeline/backfill*` 三个端点，
对应 `scripts/backfill_run.py` 的日志与暂停（O 段）。

每一条都盯住一个口径，出处在 `docs/data_pipeline/audit.md` §11 阶段 4 与
`docs/data_pipeline/plan.md` §5 阶段 5：

  1) **当日数据必须显式拒绝。** `pool_*` 与 `stock_net_inflow` 是收盘那一刻
     当场采的，事后公开接口**取不回**。返回空结果会让调用方误以为「补过了，
     本来就没有」—— 那是把「不知道」包装成「知道」。
  2) **日线绝不落前复权。** `quotes.daily_history()` 里写死 `adjust="qfq"`，
     前复权是「从今天往回算」的价格，日后配息除权会让整段历史跟着变。
     回填必须绕开它、显式传 `adjust=""`。**两条路径（东财、腾讯备源）都钉死。**
  3) **已有数据不覆盖。** `ON CONFLICT DO NOTHING`。回填最怕的不是漏，而是
     把已核对过的行情用一次重新抓取静默盖掉，而那次抓取恰好只回了半截。
  4) **断点续跑不另建进度表。** 每只票先读已有区间，覆盖即跳过；中断到哪，
     下次就从哪继续。
  5) **拿不到的值不填默认值。** 复权因子本域取不到，`adj_factor` 必须是 NULL，
     **不许填 1.0** —— 填了等于悄悄声明「没有复权」，那是口径不是数据。
  6) **备源如实记账（阶段 5）。** 东财拒了回退腾讯，`daily_bar.source` 记
     `tencent`；**整只票一次性同源**（回退发生在票级，不在票内逐日切换）；
     两源失败计数分开摊出来（K 段）。
  7) **失败区间单独重试（阶段 5）。** `bar_gaps` 挖首末日**之内**的洞，
     `only_gaps` 只按缺口那一段发请求，一天不缺就一次请求都不发；普通模式
     的「按首末日跳过」口径不变（L 段）。
  8) **优雅暂停（阶段 5）。** `should_stop` 在票边界停，落库的一行都不少，
     原样再跑一次就是续跑（M 段）。
  9) **收尾自动重跑覆盖审计（阶段 5）。** `backfill()` 返回值带 `coverage` /
     `factor_coverage`，不必再手动点刷新（N 段）。
 10) **日志不许因为一个字符掀翻跑批。** `backfill_run.log()` 先落文件后打
     控制台；GBK 控制台撞上 emoji 不抛、熔断那行照样进日志（O 段）。

全离线：开头封死 TCP + DNS，用临时夹具库，不碰真实 market.db。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_backfill_check.py
"""
import json
import socket
import sqlite3
import sys
import tempfile
import types
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

_ROOT = Path(tempfile.mkdtemp(prefix="backfill_check"))
settings.db_path = _ROOT / "market.db"

from app.store import db                                         # noqa: E402
from app.pipeline import backfill as B                           # noqa: E402

db._conn = None

PASS = 0
FAILS = []


def ok(cond, label):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)


# ---------------------------------------------------------------- 取数替身
# `fetch_daily` / `fetch_index` 是函数内 `import akshare`，所以装一个假模块进
# sys.modules，再把 `fetchers.client.call` 换成记账版 —— 这样能钉死它们到底
# 用什么参数调的源（口径 2 的唯一可靠抓法），且一次真实外发都没有。

HIST_CALLS = []
HIST_ROWS = {"600001": 5, "600002": 3, "600003": 0}   # 600003 故意回空
FAIL_ONCE = set()
#: 东财对这些票**每次都失败**（模拟被公司安全设备拦）—— 只有备源能救
EM_DOWN = set()
#: 腾讯调用记账（阶段 5 备源）
TX_CALLS = []
#: 腾讯返回行数的单独覆盖；缺省跟东财同（这样「两源都空」只对点名的票发生）
TX_ROWS = {}


def _fake_hist(symbol=None, start_date=None, end_date=None, adjust=None, **kw):
    HIST_CALLS.append({"symbol": symbol, "adjust": adjust,
                       "start": start_date, "end": end_date})
    if symbol in FAIL_ONCE or symbol in EM_DOWN:
        raise ConnectionError("该票取数失败")
    n = HIST_ROWS.get(symbol, 2)
    import pandas as pd
    rows = [{
        "日期": "2026-%02d-%02d" % (8 + i // 20, 1 + i % 20),
        "开盘": 10.0 + i, "收盘": 11.0 + i, "最高": 12.0 + i, "最低": 9.0 + i,
        "成交量": 1000.0 + i, "成交额": 10000.0 + i,
        "换手率": 1.5, "涨跌幅": 0.5,
    } for i in range(n)]
    return pd.DataFrame(rows)


def _fake_tx(symbol=None, start_date=None, end_date=None, adjust=None, **kw):
    """腾讯日线替身。返回**腾讯那套列名**（date/open/close/.../turnover 小数）。"""
    TX_CALLS.append({"symbol": symbol, "adjust": adjust,
                     "start": start_date, "end": end_date})
    if symbol in FAIL_ONCE:
        raise ConnectionError("腾讯也取不到该票")
    n = TX_ROWS.get(symbol, HIST_ROWS.get(symbol, 2))
    import pandas as pd
    rows = [{
        "date": "2026-%02d-%02d" % (8 + i // 20, 1 + i % 20),
        "open": 10.0 + i, "close": 11.0 + i, "high": 12.0 + i, "low": 9.0 + i,
        "volume": 1000.0 + i, "amount": 10000.0 + i,
        # 腾讯的 turnover 是**小数**：0.015 = 1.5%，_hist_from_tx 要 ×100
        "turnover": 0.015,
    } for i in range(n)]
    return pd.DataFrame(rows)


def _fake_call(label, fn, **kw):
    return fn(**kw)


fake_ak = types.ModuleType("akshare")
fake_ak.stock_zh_a_hist = _fake_hist


def _fake_index(symbol=None):  # noqa: ARG001
    import pandas as pd
    return pd.DataFrame([
        {"date": "2026-08-01", "open": 4000.0, "high": 4010.0, "low": 3990.0,
         "close": 4005.0, "volume": 1.0e9},
        {"date": "2026-08-04", "open": 4010.0, "high": 4020.0, "low": 4000.0,
         "close": 4015.0, "volume": 1.1e9},
    ])


fake_ak.stock_zh_index_daily = _fake_index

# 假模块必须**透传其余属性**给真 akshare：后面要 import routes，而
# `fetchers/news.py` 在模块级就会摸 `ak.stock_info_global_cls` 等一堆真接口。
# 整个换成空壳会把 routes 的导入直接炸掉（2026-10-16 第一版就这么翻的车）。
import importlib                                                # noqa: E402

_real_ak = importlib.import_module("akshare")


class _AkProxy(types.ModuleType):
    """只替换三个行情接口（东财日线、腾讯日线、指数日线），其余原样转发。"""

    def __getattr__(self, name):  # noqa: ANN001
        # 正常属性查找失败才会走到这里 —— 替身是直接设成属性的
        return getattr(_real_ak, name)


_ak_proxy = _AkProxy("akshare")
_ak_proxy.stock_zh_a_hist = _fake_hist
_ak_proxy.stock_zh_a_hist_tx = _fake_tx
_ak_proxy.stock_zh_index_daily = _fake_index
sys.modules["akshare"] = _ak_proxy

import app.fetchers.client as _client                            # noqa: E402
_client.call = _fake_call

CODES = ["600001", "600002", "600003"]


# ---- 1) 当日数据必须显式拒绝（口径 1）---------------------------------------
for d in ("pool_zt", "pool_zb", "pool_dt", "pool_strong", "stock_net_inflow"):
    why = B.refuse(d)
    ok(why is not None, "A1 %s 必须在 REFUSED 里" % d)
    ok("不可" in why or "不带日期" in why,
       "A2 %s 的理由说清了为什么补不了" % d)
try:
    B.refuse("listing")
    ok(B.refuse("listing") is not None, "A3 listing 也显式拒绝")
except Exception:
    ok(False, "A3 listing 拒绝异常")

for d in ("pool_zt", "stock_net_inflow"):
    try:
        B.plan(d, "20260801", "20260831", items=[])
        ok(False, "A4 %s 走 plan 必须报错，不许静默返回空" % d)
    except ValueError as exc:
        ok("不可" in str(exc) or "不带日期" in str(exc),
           "A4 %s 被拒且理由带出来了" % d)
    try:
        B.backfill(d, "20260801", "20260831", items=[])
        ok(False, "A5 %s 走 backfill 必须报错" % d)
    except ValueError:
        ok(True, "A5 %s 被拒" % d)

# 未知域报错时要把可选项列出来，否则调用方只能靠猜
try:
    B.plan("sector_flow_zt", "20260801", "20260831")
    ok(False, "A6 未知域必须报错")
except ValueError as exc:
    msg = str(exc)
    ok("daily_bar" in msg and "index" in msg, "A6 列出了可回填的域")
    ok("pool_zt" in msg, "A7 也列出了被拒绝的域，免得人反复试")

# ---- 2) 日线绝不落前复权（口径 2）-------------------------------------------
ok(B.ADJUST == "", "B1 复权口径写死空串（不复权），改它等于改历史价格定义")

import inspect                                                 # noqa: E402
import ast as _ast

# 用 AST 判，不用字符串找 —— 源码的 docstring 里本来就写着「不走
# quotes.daily_history()」当说明，字符串匹配会把自己写的注释当成违规。
_tree = _ast.parse(inspect.getsource(B))
_calls = [n for n in _ast.walk(_tree) if isinstance(n, _ast.Call)]
_targets = []
_kw_adjust = []
for _c in _calls:
    _f = _c.func
    _targets.append(_f.attr if isinstance(_f, _ast.Attribute)
                    else getattr(_f, "id", ""))
    for _k in _c.keywords:
        if _k.arg == "adjust" and isinstance(_k.value, _ast.Constant):
            _kw_adjust.append(_k.value.value)

ok("daily_history" not in _targets,
   "B2 不许调用 quotes.daily_history（它内部写死 qfq）：%s" % _targets)
ok("qfq" not in _kw_adjust, "B3 没有任何一处传 adjust='qfq'（got %r）"
   % _kw_adjust)
# adjust 只能取自常量 ADJUST 或直接写空串 —— 不许在调用点写死任何复权字面量，
# 否则改 ADJUST 也改不到那一处，口径就分裂成两份了。
_adj_kw = [k.value for c in _calls for k in c.keywords if k.arg == "adjust"]
ok(bool(_adj_kw) and all(
    (isinstance(v, _ast.Constant) and v.value == "")
    or (isinstance(v, _ast.Name) and v.id == "ADJUST")
    for v in _adj_kw),
   "B4 adjust 只能取自常量 ADJUST 或空串（got %r）" % _adj_kw)
# 三个取数器必须**签名一致**：backfill() 对所有域都传 (target, start, end)，
# 哪一个多收/少收一个参数，就只会在这只票上炸（第一版 fetch_index 就是）。
_ok3 = all(len(inspect.signature(getattr(B, n)).parameters) == 3
           for n in ("fetch_index", "fetch_daily"))
ok(_ok3, "B5 三个取数器签名一致（都收 target/start/end）")

# 真把 fetch_daily 跑一遍，钉死它传给 akshare 的 adjust
rows = B.fetch_daily("600001", "20260801", "20260831")
ok(HIST_CALLS and HIST_CALLS[-1]["adjust"] == "",
   "B4 fetch_daily 传给 akshare 的 adjust 必须是空串（got %r)"
   % (HIST_CALLS[-1]["adjust"],))
ok(HIST_CALLS[-1]["start"] == "20260801", "B5 起始日原样透传")
ok(len(rows) == 5, "B6 返回 5 行")
ok(rows[0]["trade_date"].isdigit() and len(rows[0]["trade_date"]) == 8,
   "B7 日期归一成 YYYYMMDD")
ok(rows[0]["adj_factor"] is None,
   "B8 复权因子取不到 => NULL，**不填 1.0 充数**")
ok(rows[0]["code"] == "600001" and rows[0]["close"] == 11.0,
   "B9 行结构含 code / close")

# ---- 3) plan() 零联网，且能说清要发多少请求 ---------------------------------
HIST_CALLS.clear()
p = B.plan("index", "20260801", "20261231")
ok(p["offline"] is True, "C1 plan 声明零联网")
ok(p["to_fetch"] == 1 and p["already_done"] == 0, "C2 指数还没落库 => 待回填 1")
ok(p["coverage"]["rows"] in (0, None), "C3 空表的 rows 记 0/None")
ok(p["coverage"]["first_date"] is None,
   "C4 空表首日写 None，**不写 0 也不写 19700101**")
ok(HIST_CALLS == [], "C5 plan 一次外发都没有")

try:
    B.plan("daily_bar", "20260801", "20261231")
    ok(False, "C6 全市场日线不传 codes 必须报错")
except ValueError as exc:
    ok("codes" in str(exc), "C6 明确说缺的是股票列表")

ok(B.plan("daily_bar", "20260801", "20261231", items=CODES)["to_fetch"] == 3,
   "C7 传了 codes 就能给出待办数")

for bad in ("2026-08-32", "202608", "abc"):
    try:
        B.plan("index", bad, "20261231")
        ok(False, "C8 非法日期 %r 必须被拒" % bad)
    except ValueError:
        ok(True, "C8 非法日期 %r 被拒" % bad)
try:
    B.plan("index", "20261231", "20260801")
    ok(False, "C9 结束日早于起始日必须报错")
except ValueError:
    ok(True, "C9 结束日早于起始日被拒")

# ---- 4) 落库：幂等 + 不覆盖（口径 3）----------------------------------------
ok(B.backfill("index", "20260801", "20260831")["inserted"] == 2,
   "D1 指数首轮回填落 2 行")
ok(B.backfill("index", "20260801", "20260831")["inserted"] == 0,
   "D2 原样再跑 inserted=0 —— 幂等靠复合主键")
p = B.plan("index", "20260801", "20260804")
ok(p["already_done"] == 1 and p["to_fetch"] == 0,
   "D3 plan 认得「已经补过了」")
# 但把区间拉长到库里还没到的日子，它必须重新报「要跑」—— 断点续跑的判据是
# **区间被盖住**，不是「有数据就算完」。假装补全了才是危险的。
p = B.plan("index", "20260801", "20260831")
ok(p["already_done"] == 0 and p["to_fetch"] == 1,
   "D3b 区间没盖住 => 仍报待补，不假装补全了")
ok(B.db.bar_span("index_daily", B.INDEX_CODE) == ("20260801", "20260804"),
   "D4 区间读得出来")

# 不覆盖：先落 4005，再用一份改过的数据 upsert，原值必须一动不动
db.bars_upsert("index_daily", [{"code": B.INDEX_CODE, "trade_date": "20260801",
                                "close": 4005.0, "open": 4000.0}])
con = sqlite3.connect(settings.db_path)
before = con.execute("SELECT close FROM index_daily WHERE trade_date='20260801'"
                     ).fetchone()[0]
db.bars_upsert("index_daily", [{"code": B.INDEX_CODE, "trade_date": "20260801",
                                "close": 9999.0}])
after = con.execute("SELECT close FROM index_daily WHERE trade_date='20260801'"
                    ).fetchone()[0]
con.close()
ok(before == 4005.0 and after == 4005.0,
   "D5 upsert **不覆盖**已有行情（got %r -> %r）" % (before, after))

db.bars_overwrite("index_daily", [{"code": B.INDEX_CODE,
                                   "trade_date": "20260801", "close": 9999.0}])
con = sqlite3.connect(settings.db_path)
ok(con.execute("SELECT close FROM index_daily WHERE trade_date='20260801'"
               ).fetchone()[0] == 9999.0, "D6 显式 overwrite 才覆盖")
con.close()

try:
    db.bars_upsert("watchlist", [{"code": "600001"}])
    ok(False, "D7 白名单外的表不许写")
except ValueError:
    ok(True, "D7 非行情表被白名单拦下")

# ---- 5) 断点续跑（口径 4）----------------------------------------------------
# 区间取 **20260801..20260804** —— 与夹具返回的四个交易日一致。「已覆盖」的
# 判据是**区间被盖住**，不是「有数据」。拿一个 0831 的结束日来测，库里再满
# 也永远报未完成，跳过那条分支根本走不到（第一版就是这么测漏的）。
S, E = "20260801", "20260804"
HIST_CALLS.clear()
# 600003 故意只回 2 天 —— 留一只没盖住的，用来验「只补剩下的」
HIST_ROWS.update({"600001": 4, "600002": 4, "600003": 2})
out = B.backfill("daily_bar", S, E, items=CODES)
ok(out["requested"] == 3 and out["ran"] == 3, "E1 首轮三只票都跑")
ok(out["inserted"] == 10, "E2 10 行落库（4+4+2，got %d）" % out["inserted"])
ok(out["finished"] is True, "E3 没限额 => finished")
ok(HIST_CALLS[-1]["symbol"] == "600003" and HIST_CALLS[-1]["adjust"] == "",
   "E4 逐票调用，且每票都传 adjust=''")

# 原样再跑：盖住的两只一次请求都不发，只补没盖住的第三只
HIST_CALLS.clear()
out = B.backfill("daily_bar", S, E, items=CODES)
ok(out["skipped"] == 2 and out["ran"] == 1,
   "E5 已覆盖的跳过，只跑剩下的（skipped=%d ran=%d）"
   % (out["skipped"], out["ran"]))
ok(HIST_CALLS[0]["symbol"] == "600003", "E6 被跳过的两只一次请求都没发")
ok(out["inserted"] == 0, "E7 重跑没补进新行（源本来就只给 2 天）")

# 补齐后重跑：它在**库里**仍只有 2 天（fixture 改的是「下次会回多少」，
# 不是已有数据），所以照跑，并把缺的 2 天补上 —— 这才是断点续跑该有的样子。
HIST_ROWS["600003"] = 4
out = B.backfill("daily_bar", S, E, items=CODES)
ok(out["skipped"] == 2 and out["ran"] == 1 and out["inserted"] == 2,
   "E8 补齐重跑：已盖住的跳过，未盖住的照跑并补上缺的 2 天"
   "（skipped=%d ran=%d inserted=%d）" % (out["skipped"], out["ran"],
                                          out["inserted"]))

# 现在三只都盖住了，再来一次就一只都不跑、一次请求都不发
HIST_CALLS.clear()
out = B.backfill("daily_bar", S, E, items=CODES)
ok(out["skipped"] == 3 and out["ran"] == 0 and out["inserted"] == 0
   and HIST_CALLS == [],
   "E9 全盖住后一只都不跑、一次请求都不发")

# 限额：只跑 1 只，剩下的下次接着干
out = B.backfill("daily_bar", "20260801", "20261231",
                 items=["600008", "600009", "600010"], budget=1)
ok(out["ran"] == 1 and out["finished"] is False, "E10 budget=1 且 finished=False")
ok(out["requested"] == 3, "E11 requested 仍记 3 —— 不被 budget 悄悄改小")

# ---- 6) 空结果与异常都不许算成功（口径 5）------------------------------------
HIST_ROWS["600004"] = 0
out = B.backfill("daily_bar", S, E, items=["600004"])
ok(out["failed"] == 1 and out["inserted"] == 0,
   "F1 源回 0 行 => 计 failed，**不许**记成 inserted=0 的成功")
ok("0 行" in out["problems"][0]["error"], "F2 写明了是源回空")

FAIL_ONCE.add("600005")
out = B.backfill("daily_bar", S, E, items=["600005", "600001"])
ok(out["failed"] == 1 and out["ran"] == 1, "F3 单票抛异常只挂它自己")
ok("ConnectionError" in out["problems"][0]["error"], "F4 异常原样带回")
ok(out["skipped"] == 1, "F5 同批里已补过的照常跳过")

# ---- 7) 进度回调抛异常不影响回填 ---------------------------------------------
def _bad_progress(x):  # noqa: ANN001, ARG001
    raise RuntimeError("页面关了")


# 回调只在「每 10 个」触发，所以这批得凑够 10 只，否则根本走不到回调那一行
_ten = ["61%04d" % i for i in range(10)]
HIST_ROWS.update({c: 2 for c in _ten})
out = B.backfill("daily_bar", "20260801", "20261231", items=_ten,
                 on_progress=_bad_progress)
ok(out["inserted"] == 20 and out["failed"] == 0,
   "G1 进度回调抛异常不影响回填本身（inserted=%d）" % out["inserted"])

_seen = []
out = B.backfill("daily_bar", "20260801", "20261231",
                 items=["62%04d" % i for i in range(10)],
                 on_progress=lambda x: _seen.append(x),
                 fetcher=lambda c, s, e: B.fetch_daily(c, s, e))
ok(_seen and _seen[0]["done"] == 10, "G2 正常回调收得到，且带进度（%r）"
   % (_seen[:1],))

# ---- 8) 覆盖统计 --------------------------------------------------------------
cov = db.bars_coverage("daily_bar")
ok(cov["codes"] >= 4 and cov["days"] >= 1, "H1 覆盖统计读得出来")
ok(cov["first_date"] == "20260804" or cov["first_date"].startswith("2026"),
   "H2 首日是真实日期（got %s）" % cov["first_date"])
ok(len(db.bars_dates("daily_bar", "600001")) == 4, "H3 单只票的交易日列得出来")

# ---- 9) 端点接线 --------------------------------------------------------------
from app.api import routes as R                                  # noqa: E402
paths = {getattr(x, "path", "") for x in R.router.routes}
ok("/api/pipeline/backfill" in paths, "I1 回填端点已注册")
ok("/api/pipeline/backfill/plan" in paths, "I2 计划端点已注册")
ok("/api/pipeline/backfill/domains" in paths, "I3 域清单端点已注册")

dom = R.pipeline_backfill_domains()
ok(dom["offline"] is True, "I4 域清单声明零联网")
ok({x["domain"] for x in dom["refused"]} >= {"pool_zt", "pool_zb", "pool_dt",
                                             "pool_strong", "stock_net_inflow"},
   "I5 被拒的域一个不少")
ok(dom["default_start"] == "20260801", "I6 默认起点是用户拍板的 20260801")
ok(len(dom["backfillable"]) == 3, "I7 三个可回填的域")

pl = R.pipeline_backfill_plan("index", "20260801", "20261231")
ok(pl["offline"] is True, "I8 计划端点零联网")

for bad_domain in ("pool_zt", "nope"):
    try:
        R.pipeline_backfill_plan(bad_domain, "20260801", "20261231")
        ok(False, "I9 %r 必须回 400" % bad_domain)
    except Exception as exc:
        ok(getattr(exc, "status_code", None) == 400,
           "I9 %r 回 400（got %s）" % (bad_domain, getattr(exc, "status_code", None)))

HIST_ROWS["600007"] = 2
resp = R.pipeline_backfill({"domain": "daily_bar", "start": "20260801",
                            "end": "20261231", "items": ["600007"], "budget": 5})
ok(resp["offline"] is False and resp["inserted"] == 2,
   "J1 POST 回填端点可用且如实回 offline=False")
ok(resp["adjust"] == "", "J2 响应里带上复权口径，让人看得见")

# ---- 9b) 端点透传 only_gaps（阶段 5：失败区间单独重试的入口）----------------
HIST_ROWS["600031"] = 4
B.backfill("daily_bar", S, E, items=["600031"])
con = sqlite3.connect(settings.db_path)
con.execute("DELETE FROM daily_bar WHERE code='600031' AND trade_date='20260803'")
con.commit()
con.close()
HIST_CALLS.clear()
resp = R.pipeline_backfill({"domain": "daily_bar", "start": S, "end": E,
                            "items": ["600031"], "only_gaps": True})
ok(resp.get("inserted") == 1 and resp.get("only_gaps") is True,
   "J3 POST only_gaps 只补缺的那天（inserted=%s）" % resp.get("inserted"))
ok(HIST_CALLS and HIST_CALLS[0]["start"] == "20260803"
   and HIST_CALLS[0]["end"] == "20260803",
   "J4 请求区间就是缺口那一段，不是整段重跑 —— %r" % (HIST_CALLS,))
pl = R.pipeline_backfill_plan("daily_bar", S, E, items=["600031"],
                              only_gaps=True)
ok(pl["to_fetch"] == 0 and pl["hole_codes"] == 0 and pl["already_done"] == 1,
   "J5 GET plan 认得 only_gaps，补完后不缺（fetch=%d hole=%d）"
   % (pl["to_fetch"], pl["hole_codes"]))

# ---- K) 腾讯备源：东财拒了才回退，且整只票同源（阶段 5）--------------------
def _calls_of(attr_name):  # noqa: ANN001 -> list
    """把某个 akshare 接口**传进 call(...)** 的那些调用点。

    `adjust` 挂在 `call()` 上而不是挂在接口名上，所以判据要认「参数位上的
    Attribute」—— 只扫 `func.attr` 会一个都数不到（第一版就是这么测漏的）。
    """
    return [c for c in _calls
            if any(isinstance(a, _ast.Attribute) and a.attr == attr_name
                   for a in c.args)]


_tx_ast = _calls_of("stock_zh_a_hist_tx")
ok(len(_tx_ast) == 1, "K1 备源调用点在源码里就一处（多了容易漏改）got %d"
   % len(_tx_ast))
ok(_tx_ast and all((isinstance(k.value, _ast.Constant) and k.value.value == "")
                   or (isinstance(k.value, _ast.Name) and k.value.id == "ADJUST")
                   for c in _tx_ast for k in c.keywords if k.arg == "adjust"),
   "K2 备源路径的 adjust 只能是 ADJUST/空串 —— 绝不 qfq")
ok(len(_calls_of("stock_zh_a_hist")) == 1,
   "K2b 主源（东财）调用点也只有一处 —— 两处口径分头改就裂了")

# 600011：东财**每次都失败**（模拟被安全设备拦）→ 只有备源能救
EM_DOWN.add("600011")
HIST_CALLS.clear()
TX_CALLS.clear()
rows = B.fetch_daily("600011", S, E)
ok(HIST_CALLS and TX_CALLS, "K3 东财失败后**确实**试了腾讯备源")
ok(rows and all(r["source"] == "tencent" for r in rows),
   "K4 备源回来的行整只票同源记 tencent（不在票内逐日切换）")
ok(rows and rows[0]["trade_date"] == "20260801" and len(rows[0]["trade_date"]) == 8,
   "K5 备源路径日期照样归一 YYYYMMDD")
ok(rows and rows[0]["adj_factor"] is None,
   "K6 备源路径同样不填 1.0 充数（那是口径不是数据）")
ok(rows and abs((rows[0]["turnover"] or 0) - 1.5) < 1e-6,
   "K7 turnover 小数×100 —— 复用 quotes._hist_from_tx，没另写一套"
   "（got %r）" % (rows[0]["turnover"] if rows else None,))
ok(TX_CALLS and all(c["adjust"] == "" for c in TX_CALLS),
   "K8 腾讯路径同样传 adjust=''（got %r）"
   % ([c["adjust"] for c in TX_CALLS],))

# 两个源都空 => 回 []（调用方按「源返回 0 行」计 failed，不算成功）
EM_DOWN.add("600012")
TX_ROWS["600012"] = 0
ok(B.fetch_daily("600012", S, E) == [], "K9 两源都回 0 行 => 空列表（不是成功）")
# 两个源都挂 => FetchError，且说得出是哪个源
EM_DOWN.add("600013")
FAIL_ONCE.add("600013")
try:
    B.fetch_daily("600013", S, E)
    ok(False, "K10 两源都挂必须抛错，不许静默回空")
except B.FetchError as exc:
    ok(exc.source == "tencent", "K11 FetchError 说得出最后试的源（got %r）"
       % exc.source)
    ok("ConnectionError" in str(exc) and "eastmoney" in str(exc),
       "K12 两个源的失败原因都在消息里 —— %s" % str(exc)[:90])
except Exception as exc:  # noqa: BLE001
    ok(False, "K10 抛的必须是 FetchError（got %s）" % type(exc).__name__)
for _c in ("600011", "600012", "600013"):
    EM_DOWN.discard(_c)
FAIL_ONCE.discard("600013")
TX_ROWS.pop("600012", None)

# 落库侧：备源救回来的行，source 列如实记
EM_DOWN.add("600014")
HIST_ROWS["600014"] = 2
out = B.backfill("daily_bar", S, E, items=["600014"])
ok(out["inserted"] == 2, "K13 备源照样落库（inserted=%d）" % out["inserted"])
ok(out["sources"].get("tencent", {}).get("inserted") == 2,
   "K14 源计数：这批行记在 tencent 头上 —— %r" % out["sources"])
ok(out["sources"].get("tencent", {}).get("failed") == 0
   and not out["sources"].get("eastmoney", {}).get("failed"),
   "K15 东财拒了但备源救回，**不算失败** —— %r" % out["sources"])
con = sqlite3.connect(settings.db_path)
_srcs = {r[0] for r in con.execute(
    "SELECT source FROM daily_bar WHERE code='600014'")}
con.close()
ok(_srcs == {"tencent"}, "K16 daily_bar.source 落列如实记（got %r）" % (_srcs,))
EM_DOWN.discard("600014")

# 两源都挂：失败记在**备源**头上，看得出谁在拒
EM_DOWN.add("600015")
FAIL_ONCE.add("600015")
out = B.backfill("daily_bar", S, E, items=["600015"])
ok(out["failed"] == 1
   and out["sources"].get("tencent", {}).get("failed") == 1,
   "K17 两源都挂 => 失败记在备源头上 —— %r" % out["sources"])
ok(out["problems"] and out["problems"][0].get("source") == "tencent",
   "K18 problems 里带源，事后查得到是谁在拒")
EM_DOWN.discard("600015")
FAIL_ONCE.discard("600015")

# ---- L) 失败区间单独重试：中间断了要看得见、补得动（阶段 5）----------------
HIST_ROWS["600021"] = 4
B.backfill("daily_bar", S, E, items=["600021"])
con = sqlite3.connect(settings.db_path)
con.execute("DELETE FROM daily_bar WHERE code='600021' AND trade_date='20260802'")
con.commit()
con.close()
_runs = B.db.bar_gaps("daily_bar", "600021", S, E)
ok(_runs == [["20260802", "20260802"]],
   "L1 bar_gaps 把中间那一天挖出来 —— %r" % _runs)
pl = B.plan("daily_bar", S, E, items=["600021"])
ok(pl["to_fetch"] == 0 and pl["already_done"] == 1 and pl["hole_codes"] == 1,
   "L2 普通计划：区间盖住算已补，但断档单列（不混进 todo）"
   "—— fetch=%d done=%d hole=%d" % (pl["to_fetch"], pl["already_done"],
                                     pl["hole_codes"]))
ok(pl["holes"] and pl["holes"][0]["ranges"] == [["20260802", "20260802"]],
   "L3 断档明细带得出是哪几段 —— %r" % (pl["holes"][:1],))
pl = B.plan("daily_bar", S, E, items=["600021"], only_gaps=True)
ok(pl["to_fetch"] == 1 and pl["todo"] == ["600021"] and pl["only_gaps"] is True,
   "L4 只补缺口模式下它就是待办（fetch=%d todo=%r）"
   % (pl["to_fetch"], pl["todo"]))

HIST_CALLS.clear()
TX_CALLS.clear()
out = B.backfill("daily_bar", S, E, items=["600021"], only_gaps=True)
ok(len(HIST_CALLS) == 1 and HIST_CALLS[0]["start"] == "20260802"
   and HIST_CALLS[0]["end"] == "20260802",
   "L5 只补缺口：请求区间就是缺的那一天 —— %r" % (HIST_CALLS,))
ok(out["inserted"] == 1 and out["ran"] == 1,
   "L6 补回那 1 行（inserted=%d ran=%d）" % (out["inserted"], out["ran"]))
ok(B.db.bar_gaps("daily_bar", "600021", S, E) == [], "L7 补完就不缺了")

HIST_CALLS.clear()
out = B.backfill("daily_bar", S, E, items=["600021"], only_gaps=True)
ok(out["skipped"] == 1 and out["ran"] == 0 and HIST_CALLS == [],
   "L8 一天不缺 => 一次请求都不发（skipped=%d ran=%d）"
   % (out["skipped"], out["ran"]))

# 普通模式口径不变：断档的票照样**按首末日跳过**（不许悄悄改语义）
con = sqlite3.connect(settings.db_path)
con.execute("DELETE FROM daily_bar WHERE code='600021' AND trade_date='20260802'")
con.commit()
con.close()
HIST_CALLS.clear()
out = B.backfill("daily_bar", S, E, items=["600021"])
ok(out["skipped"] == 1 and HIST_CALLS == [],
   "L9 普通模式仍按首末日跳过 —— 口径没变（skipped=%d）" % out["skipped"])

# universe（交易日历）能改判据：调用方说那天不是交易日就不算缺口
_runs = B.db.bar_gaps("daily_bar", "600021", S, E,
                      universe=["20260801", "20260802", "20260803", "20260804"])
ok(_runs == [["20260802", "20260802"]],
   "L10 显式 universe 同样挖得出缺口 —— %r" % _runs)
_runs = B.db.bar_gaps("daily_bar", "600021", S, E,
                      universe=["20260801", "20260803", "20260804"])
ok(_runs == [], "L11 调用方说那天是停牌日 => 不算缺口 —— %r" % _runs)
ok(B.db.bar_gaps("daily_bar", "699999", S, E) == [[S, E]],
   "L12 一天都没有的票：整段都是缺口（等价于普通回填）")
HIST_ROWS["600022"] = 4
B.backfill("daily_bar", S, E, items=["600022"])
ok(B.db.bar_gaps("daily_bar", "600022", "20260701", "20260731") == [],
   "L13 区间在票自己首末日之外 => 不算缺口（上市前/退市后不追）")

# ---- M) 优雅暂停：票边界停，落库的一行都不少（阶段 5）----------------------
_M = ["610100", "610101", "610102", "610103"]
HIST_ROWS.update({c: 4 for c in _M})
STOP = {"n": 0}


def _stop_after2():  # noqa: ANN202
    STOP["n"] += 1
    return STOP["n"] > 2          # 第 3 只开跑之前就喊停


out = B.backfill("daily_bar", S, E, items=_M, should_stop=_stop_after2)
ok(out["paused"] is True, "M1 暂停标记回给调用方")
ok(out["finished"] is False, "M2 暂停 => finished=False（还有活没干完）")
ok(out["ran"] == 2 and out["requested"] == 4,
   "M3 停在票边界：第 3 只之前就不发请求了（ran=%d requested=%d）"
   % (out["ran"], out["requested"]))
ok(out["inserted"] == 8, "M4 停之前落的 8 行都在（inserted=%d）" % out["inserted"])
out = B.backfill("daily_bar", S, E, items=_M)
ok(out["skipped"] == 2 and out["inserted"] == 8 and out["finished"] is True,
   "M5 原样再跑一次就是续跑：跳过已补的、补完剩下的"
   "（skipped=%d inserted=%d）" % (out["skipped"], out["inserted"]))

# ---- N) 收尾自动重跑覆盖审计（阶段 5）--------------------------------------
out = B.backfill("daily_bar", S, E, items=["600001"])
ok(isinstance(out.get("coverage"), dict) and (out["coverage"].get("rows") or 0) > 0,
   "N1 回填收尾自动带出覆盖审计（不用再手动点刷新）—— %r"
   % (out.get("coverage"),))
ok(isinstance(out.get("factor_coverage"), dict)
   and "with_factor" in out["factor_coverage"],
   "N2 日线域连因子覆盖一起给 —— %r" % (out.get("factor_coverage"),))
ok({"coverage", "sources", "paused", "only_gaps"} <= set(out),
   "N3 阶段 5 新增键都在返回值里 —— job_run.detail 会留住")
out = B.backfill("index", S, E)
ok("coverage" in out and "factor_coverage" not in out,
   "N4 指数域不硬凑因子覆盖（那张表没有 adj_factor）")

# ---- O) backfill_run：控制台编码装不下 emoji 也不许掀翻日志 ----------------
import importlib.util as _ilu                                   # noqa: E402
import signal as _sig                                           # noqa: E402

_spec = _ilu.spec_from_file_location(
    "backfill_run_under_test",
    Path(__file__).resolve().parents[1] / "backfill_run.py")
BR = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(BR)
_tmp_log = _ROOT / "run" / "backfill.log"
BR.LOG_DIR = _tmp_log.parent
BR.LOG_PATH = _tmp_log
BR._LOG_FP = None


class _BoomStdout:
    """GBK 控制台的化身：见到非 ASCII 就炸，跟真控制台一个脾气。"""

    encoding = "gbk"

    def write(self, s):  # noqa: ANN001
        s.encode("gbk", "strict")
        return len(s)

    def flush(self):  # noqa: ANN201
        return None


BR._PAUSE["asked"] = 0
BR._on_sigint(_sig.SIGINT, None)
ok(BR._PAUSE["asked"] == 1, "O1 第一次 Ctrl+C 只置暂停标记（优雅暂停）")
BR._PAUSE["asked"] = 0

_old_out = sys.stdout
sys.stdout = _BoomStdout()
try:
    BR.log("🛑 单批失败率 80% > 50% —— 熔断停机")
    ok(True, "O2 emoji + GBK 控制台：log() 不再抛异常（:65 那个坑）")
except UnicodeEncodeError:
    ok(False, "O2 emoji + GBK 控制台：log() 仍炸 —— 熔断那行进不了日志")
finally:
    sys.stdout = _old_out
_txt = _tmp_log.read_text(encoding="utf-8")
ok("🛑" in _txt, "O3 熔断那行**进得了日志**（先落文件、后打控制台）")
ok(BR.default_start() == B.DEFAULT_START,
   "O4 跑批缺省起始日 = 拍板的 %s（不再是「一年前」）" % B.DEFAULT_START)

_buf = []
_BR_log = BR.log
BR.log = lambda m: _buf.append(m)          # noqa: E731  只看打了什么
try:
    BR._log_sources({"eastmoney": {"items": 10, "rows": 40, "inserted": 40,
                                   "failed": 0},
                     "tencent": {"items": 2, "rows": 0, "inserted": 0,
                                 "failed": 2}})
finally:
    BR.log = _BR_log
ok(any("eastmoney" in m and "tencent" in m and "失败 2" in m for m in _buf),
   "O5 两源失败计数分开摊出来 —— %r" % (_buf[:1],))

# ---- 10) 收尾 -----------------------------------------------------------------
ok(not _BLOCKED, "Z1 一次真实外发都不许有：%s" % _BLOCKED[:3])

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("PASS=%d FAIL=%d" % (PASS, len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
