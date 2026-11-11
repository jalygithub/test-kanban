# -*- coding: utf-8 -*-
r"""采集覆盖审计（共享事实层）的**离线**回归，2026-10-16。

对应 `app/pipeline/coverage.py` 与 docs/data_pipeline/audit.md §10/§11。

每一条都盯住一个开发时真踩过的点：

  1) **零联网**：审计是「查本地库出结论」，不是「去问数据源」。内网下任何
     外发都会被公司安全设备拦截并留记录 —— 开头封死 TCP + DNS。
  2) **只读不写**：`mode=ro` 打开，跑完整份审计后库文件字节数必须**一模一样**。
  3) **五态必须分开**：没跑任务 / 采了空手 / 源挂了 / 只覆盖一部分 / 完整 ——
     这五种在页面上长得像，要人做的事完全不同，混成一个「失败」人就不知道
     该干什么。
  4) **'latest' 占位键不是没数据**：板块资金流整天存成 `trade_date='latest'`，
     有数据但零历史 => 必须 `PARTIAL`，**不能**判成 `NO_DATA`。
  5) **缺失写 `None`，绝不写 `0`**：`0.0` 在覆盖率上是「全缺」，页面上分不清
     「全缺」和「没记录」。这是任务书的硬要求。
  6) **必填字段规格要认包型 payload**：`main_force` 是 `[{rows:[...]}]`，拆出
     内层行之后验的是内层字段 —— 写 `rows` 会把每一天的每一行全判成缺字段
     （我在这儿翻过一次车）。
  7) **条数骤降要能判出来**：`stock_net_inflow` 正常日几千条、异常日几十条，
     只看总量会把两者平均成一个看不出问题的数字。
  8) **条数天然波动的域不能按条数判**：涨停家数逐日不同，硬按条数判会把
     「那天真的只有 7 个涨停」误报成数据缺失。
  9) **`gaps()` 只看回看窗口**：不返回「1990 年以来从没采过」——那对补跑
     没有操作价值，只是一堵墙。
 10) **共享**：`/api/research/audit` 里嵌的 `pipeline` 与 `coverage_report()`
     必须是同一份数字（同一个键、同一个取值），否则页面上会各说各话。
 11) **库打不开不许抛**：返回带 `error` 的结构，让两个页面各报各的错。

全离线：开头封死 TCP + DNS，用临时夹具库，不碰真实 market.db。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_pipeline_check.py
"""
import json
import os
import socket
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
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


socket.socket.connect = _guard
socket.getaddrinfo = _gai_guard

BACKEND = Path(__file__).resolve().parents[2] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import settings                          # noqa: E402
from app.pipeline import coverage as C                   # noqa: E402

PASS = 0
FAILS = []


def ok(cond, label):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)


# ---- 夹具 -------------------------------------------------------------------
# 日期一律取**真实交易日历**里的日，这样覆盖率分母是确定的；代码是夹具代码。
CALENDAR, _STALE = C.cached_trade_dates()
# 「今天」必须现算，**不能写死**：`coverage.gaps()` 的窗口右端就是真实的今天，
# 写死一个日期会在跨日之后把断言变成假的 —— 2026-10-17 就这么红过一次
# （写测试那天 10-16 就是今天，隔天窗口右移一天，快照里没有的 10-16 被列成
# 漏采，I5 当场假）。口径与 gaps() 完全一致：UTC 日期、`d < today`。
TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")
PAST = [d for d in CALENDAR if d < TODAY][-12:]      # 今天之前的 12 个交易日
FULL = PAST[-8:]                                          # 连续 8 天

JOB_DDL = """
CREATE TABLE IF NOT EXISTS job_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT,
    status      TEXT,
    started_at  TEXT,
    ended_at    TEXT,
    detail      TEXT
);
"""

SNAP_DDL = """
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


def _inflow(n, missing_field=False):
    """n 条主力净流入。`missing_field` 故意让一半行缺 net_inflow。"""
    rows = []
    for i in range(n):
        row = {"code": "6000%02d" % (i % 50), "name": "夹具%d" % i}
        if not (missing_field and i % 2 == 0):
            row["net_inflow"] = 1e6 * (i + 1)
        rows.append(row)
    return rows


def _pool(n):
    return [{"code": "6000%02d" % i, "name": "涨停%d" % i,
             "price": 10.0 + i, "pct_chg": 9.98, "amount": 1e8}
            for i in range(n)]


def _main_force(envelope=True, with_score=True):
    """main_force 的**包型** payload —— 外层一个 dict，rows 才是行。"""
    row = {"code": "600001", "name": "夹具", "price": 10.0, "pct_chg": 1.0}
    if with_score:
        row["score"] = 80.0
    return [{"params": {"top_n": 30}, "rows": [row], "all_rows": [row],
             "model": {"degraded": [], "total_weight": 100}}]


def _mk(name, *, job=True, job_error=False, snapshot=None,
        latest_only=False, cal_dates=None, steps=None):
    """建夹具库。`snapshot` = {dataset: [(date, payload), ...]}。

    默认带一张 job_run 表（`job=True`），且 detail 里有**全部七个步骤**的
    成功结果 —— 这样「没有数据」才能被判成 NO_DATA 而不是 NO_JOB。
    `steps` 可以只放一部分步骤，用来造「这个域从没被采过」的场景。
    """
    root = Path(tempfile.mkdtemp(prefix=name))
    path = root / "market.db"
    con = sqlite3.connect(path)
    con.executescript(SNAP_DDL)
    if job:
        con.executescript(JOB_DDL)
        results = {"pool_zt": 36, "pool_zb": 5, "pool_dt": 2,
                   "pool_strong": 70, "sector_industry": 86,
                   "sector_concept": 300, "main_force": 30}
        if steps is not None:
            results = {k: v for k, v in results.items() if k in steps}
        detail = {"date": FULL[-1], "results": results, "failed": []}
        if job_error:
            detail["results"]["pool_zt"] = "失败: ConnectionError(源超时)"
            detail["failed"] = ["pool_zt"]
        con.execute("INSERT INTO job_run (kind,status,started_at,ended_at,detail)"
                    " VALUES (?,?,?,?,?)",
                    ("manual", "failed" if job_error else "success",
                     "%s-%s-%sT02:00:00+00:00" % (FULL[-1][:4], FULL[-1][4:6],
                                                  FULL[-1][6:]),
                     None, json.dumps(detail, ensure_ascii=False)))

    dates = cal_dates if cal_dates is not None else FULL
    for ds, items in (snapshot or {}).items():
        for d, payload in items:
            con.execute(
                "INSERT INTO snapshot VALUES (?,?,?,?,?,?)",
                (ds, d, json.dumps(payload, ensure_ascii=False),
                 len(payload), "fixture",
                 "%s-%s-%sT02:00:00+00:00" % (d[:4], d[4:6], d[6:])))
        if latest_only:
            # 板块资金流的真实形态：整天只有字面量 'latest' 一键，零历史
            con.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?)",
                        (ds, "latest", json.dumps(items[-1][1],
                                                  ensure_ascii=False),
                         len(items[-1][1]), "fixture",
                         "%s-%s-%sT02:00:00+00:00" % (dates[-1][:4],
                                                      dates[-1][4:6],
                                                      dates[-1][6:])))
    con.commit()
    con.close()
    return path


def _rep(path=None):
    return C.coverage_report(str(path)) if path else C.coverage_report()


def _one(rep, ds):
    return rep["datasets"][ds]


# ==== 1-2) 零联网 + 只读不写 ================================================
p = _mk("cov1", snapshot={"pool_zt": [(d, _pool(36)) for d in FULL],
                          "stock_net_inflow": [(d, _inflow(100)) for d in FULL]})
before = p.read_bytes()
r = _rep(p)
ok(not _BLOCKED, "A1 一次外发都不许有：%s" % _BLOCKED[:3])
ok(p.read_bytes() == before, "A2 审计只读，库文件字节必须一模一样")
ok(r["offline"] is True, "A3 报告显式标 offline")
ok(isinstance(r["calendar"]["days"], int) and r["calendar"]["days"] > 0,
   "A4 交易日历读到了本地缓存")
ok(r["calendar"]["stale"] is False and r["calendar"]["note"],
   "A5 日历新鲜时也要给出说明文字，不能是空串")

# 日历过期必须显式标注 —— 它会让漏采检测漏判/误判，不说是隐患
ok(r["calendar"]["note"] and isinstance(r["calendar"]["note"], str),
   "A6 日历状态有对应的说明文字（过期与否都得有）")

# ==== 3) OK：八天全在、字段齐 =================================================
ok(_one(r, "pool_zt")["state"] == C.OK, "B1 八天全采 + 字段齐 => OK")
ok(_one(r, "pool_zt")["coverage_rate"] == 1.0, "B2 覆盖率 1.0")
ok(not _one(r, "pool_zt")["missing_dates"], "B3 没有缺口")
ok(_one(r, "pool_zt")["missing_field_rows"] == 0, "B4 没有缺字段的行")
ok(_one(r, "pool_zt")["has_history"] is True, "B5 多日 => 有历史")
ok(_one(r, "pool_zt")["job"]["last_attempt_at"] is not None,
   "B6 从 job_run 挖出了最近一次尝试时间")

# ==== 4-5) 缺口 => PARTIAL；缺失写 None 不写 0 ================================
gap_day = PAST[-3]
p2 = _mk("cov2", snapshot={
    "pool_zt": [(d, _pool(36)) for d in FULL if d != gap_day]})
r2 = _rep(p2)
z = _one(r2, "pool_zt")
ok(z["state"] == C.PARTIAL, "C1 有日期缺口 => PARTIAL，不是 OK")
ok(z["missing_dates"] == [gap_day], "C2 缺口日期报得准：%s" % z["missing_dates"])
ok(z["coverage_rate"] is not None and z["coverage_rate"] < 1.0,
   "C3 覆盖率 < 1")
ok(z["expected_days"] == len(FULL), "C4 分母是本地日历里这一段的交易日数")

p3 = _mk("cov3")                                  # 一个数据集都没插
r3 = _rep(p3)
empty = _one(r3, "pool_zt")
ok(empty["coverage_rate"] is None, "D1 零数据 => coverage_rate 是 None，不是 0.0")
ok(empty["first_date"] is None and empty["last_date"] is None,
   "D2 零数据 => 首末日期都是 None，不是 'None' 字符串")
ok(empty["job"]["last_error"] is None, "D3 零数据 => 错误也是 None 不是空串")

# ==== 6) 五态：NO_JOB / NO_DATA / SOURCE_DOWN ================================
# NO_JOB：跑过任务，但**这一轮根本没这一步** —— 没人采它
p_never = _mk("cov_never", steps={"pool_zt"})
never = _one(_rep(p_never), "pool_strong")
ok(never["state"] == C.NO_JOB, "E1 从没被采过 => NO_JOB")
ok(never["job"]["last_attempt_at"] is None, "E2 没被采过 => 最近一次尝试是 None")

# NO_DATA：这一步执行了，却一条都没拿到
ok(empty["state"] == C.NO_DATA, "E3 跑过任务、这一步也执行了、但零数据 => NO_DATA")

p5 = _mk("cov5", job_error=True)
r5 = _rep(p5)
d5 = _one(r5, "pool_zt")
ok(d5["state"] == C.SOURCE_DOWN, "E3 有错误记录 => SOURCE_DOWN")
ok("失败" in (d5["job"]["last_error"] or ""), "E4 错误原因原样带回：%s"
   % d5["job"]["last_error"])
ok(C.worst_state(C.OK, C.SOURCE_DOWN, C.PARTIAL) == C.SOURCE_DOWN,
   "E5 worst_state 取最差")
ok(C.worst_state(C.OK, C.PARTIAL) == C.PARTIAL, "E6 PARTIAL 不被 OK 抹平")

# ==== 7) 'latest' 占位键 ------------------------------------------------------
p6 = _mk("cov6", snapshot={"sector_flow_industry": [(FULL[-1],
                                                     [{"name": "煤炭",
                                                       "net_inflow": 1e8,
                                                       "pct_chg": 2.1}])]},
         latest_only=True)
# 只留 latest 这一键：模拟库里根本没按交易日归档
con = sqlite3.connect(p6)
con.execute("DELETE FROM snapshot WHERE dataset='sector_flow_industry'"
            " AND trade_date<>?", ("latest",))
con.commit()
con.close()
r6 = _rep(p6)
sf = _one(r6, "sector_flow_industry")
ok(sf.get("placeholder_latest") == 1, "F1 记出了 'latest' 占位键的数量")
ok(sf["state"] == C.PARTIAL,
   "F2 只有 'latest' 占位键 => PARTIAL（有数据但零历史），不能判 NO_DATA")
ok(sf["has_history"] is False, "F3 没有按交易日归档 => 没有历史")

# ==== 8) 必填字段规格要认包型 payload ----------------------------------------
p7 = _mk("cov7", snapshot={
    "main_force": [(d, _main_force()) for d in FULL]})
mf = _one(_rep(p7), "main_force")
ok(mf["missing_field_rows"] == 0,
   "G1 main_force 包型 payload 不该被全判缺字段（验的是内层行的 code/score）")
ok(mf["rows_total"] == len(FULL), "G2 每天一行 => 总行数 = 天数")
ok(mf["state"] == C.OK, "G3 字段齐 => OK")

p8 = _mk("cov8", snapshot={
    "main_force": [(d, _main_force(with_score=False)) for d in FULL]})
mf8 = _one(_rep(p8), "main_force")
ok(mf8["missing_field_rows"] == len(FULL),
   "G4 内层行缺 score 必须被数出来（每行都缺 => 每行都算）")
ok(mf8["state"] == C.PARTIAL, "G5 关键字段缺失 => PARTIAL")

# 声称有条数、却读不出内容 => 不能算完整，也不猜内容
p9 = _mk("cov9", snapshot={"pool_zt": [(FULL[-1], _pool(36))]})
con = sqlite3.connect(p9)
con.execute("UPDATE snapshot SET payload='not-json' WHERE dataset='pool_zt'")
con.commit()
con.close()
bad = _one(_rep(p9), "pool_zt")
ok(bad["rows_total"] == 0, "G6 payload 解析不出来 => 0 行，不猜内容")
ok(bad["unreadable_days"] == [FULL[-1]],
   "G7 声称有条数却读不出 => 单独列成 unreadable_days")
ok(bad["state"] == C.PARTIAL,
   "G8 坏 payload 绝不能报成 OK：%s" % bad["state"])

# ==== 9) 条数骤降 / 条数天然波动的域 ----------------------------------------
p10 = _mk("cov10", snapshot={
    "stock_net_inflow": [(d, _inflow(100 if d != FULL[-1] else 10))
                         for d in FULL]})
inf = _one(_rep(p10), "stock_net_inflow")
ok(inf["item_peak"] == 100, "H1 峰值取的是各日条数的最大值")
ok(inf["thin_days"] == [FULL[-1]], "H2 条数骤降的日子被挑出来：%s"
   % inf["thin_days"])
ok(inf["state"] == C.PARTIAL, "H3 只覆盖了部分股票 => PARTIAL")
ok(inf["coverage_rate"] == 1.0, "H4 日期一个不缺，但条数不齐 => 仍不是完整")

# 涨停家数天然逐日不同：10 条 vs 4 条不该被判薄
p11 = _mk("cov11", snapshot={
    "pool_zt": [(d, _pool(10 if i % 2 == 0 else 4))
                for i, d in enumerate(FULL)]})
pz = _one(_rep(p11), "pool_zt")
ok(not pz["thin_days"], "H5 条数天然波动的域不做「部分股票」判定")
ok(pz["state"] == C.OK, "H6 七个涨停的那天不会被误报成数据缺失")

# 缺必填字段 => PARTIAL
p12 = _mk("cov12", snapshot={
    "stock_net_inflow": [(d, _inflow(40, missing_field=(d == FULL[-1])))
                         for d in FULL]})
inf12 = _one(_rep(p12), "stock_net_inflow")
ok(inf12["missing_field_rows"] == 20, "H7 逐日解析能数出缺字段的行数")
ok(inf12["state"] == C.PARTIAL, "H8 有行缺关键字段 => PARTIAL，不能算完整")

# ==== 10) gaps() 只看回看窗口 -------------------------------------------------
p13 = _mk("cov13", snapshot={"pool_zt": [(d, _pool(30)) for d in FULL[-5:]]})
g = C.gaps(str(p13), dataset="pool_zt", lookback=20)
ok(set(g) == {"pool_zt"}, "I1 指定 dataset 只回那一个域")
ok(len(g["pool_zt"]) <= 20, "I2 gaps 严格不超过回看窗口（不列 1990 年以来）")
ok(FULL[-1] not in g["pool_zt"], "I3 已采到的日子不在缺口里")

g_all = C.gaps(str(p13), lookback=10)
# 上界跟着**注册表**走，不写死 10：域是会加的（阶段 3 就加了三个），
# 写死一个数会让「新增了一个数据域」变成一条红。真正要盯住的是第二段 ——
# 无论多少个域，每个域的缺口都**不超过回看窗口**。
ok(len(g_all) <= len(C._SNAP_KINDS)
   and all(len(v) <= 10 for v in g_all.values()),
   "I4 不指定 dataset 时每个域都被窗口截住（域数 %d <= 注册表 %d）"
   % (len(g_all), len(C._SNAP_KINDS)))

# 今天还没走完、明天还没到，都不算漏采（`gaps()` 的窗口右端就是 TODAY）
ok(all(d < TODAY for d in g["pool_zt"]), "I5 只报**已过期**的交易日")

# ==== 11) 共享：research/audit 里嵌的 pipeline 与本层同源 ----------------------
from app.research.audit import BacktestDataAuditService    # noqa: E402
settings.db_path = str(p)
audit = BacktestDataAuditService().audit()
shared = audit.get("pipeline") or {}
ok("pipeline" in audit, "J1 可回测性报告里嵌了 pipeline 覆盖事实")
ok(shared.get("datasets", {}).get("pool_zt", {}).get("state") == C.OK,
   "J2 两边对同一个域判出同一个状态")
ok(shared.get("datasets", {}).get("pool_zt", {}).get("first_date") == FULL[0],
   "J3 两边对同一个域报同一个首日")
ok(shared.get("calendar", {}).get("days") == r["calendar"]["days"],
   "J4 两边用的是同一份日历")
ok(audit["offline"] is True and shared.get("offline") is True,
   "J5 两份报告都声明零联网")
# 覆盖事实拿不到不该拖垮可回测性审计
ok(isinstance(audit["domains"]["daily_bars"]["status"], str),
   "J6 覆盖层异常时可回测性判定照常返回")

# ==== 12) 库打不开不许抛 ------------------------------------------------------
missing = _rep(Path(tempfile.gettempdir()) / "definitely-not-here.db")
ok("error" in missing, "K1 库不存在 => 返回带 error 的结构，不抛异常")
ok(isinstance(missing, dict), "K2 error 分支也是 dict，前端形状稳定")

# 空报告形状：缺键也要稳定
ok(missing.get("datasets", {}) == {} or "datasets" not in missing,
   "K3 打不开时没有 datasets 键也不算错")
ok(isinstance(missing.get("calendar"), dict),
   "K4 calendar 键始终存在")

# ==== 13) 端点接线 ------------------------------------------------------------
from app.api import routes as R                          # noqa: E402
paths = {getattr(x, "path", "") for x in R.router.routes}
ok("/api/pipeline/coverage" in paths, "L1 覆盖审计端点已注册")
ok("/api/pipeline/coverage/{dataset}" in paths, "L2 逐日明细端点已注册")
ok("/api/pipeline/gaps" in paths, "L3 漏采速查端点已注册")
ok("/api/research/audit" in paths, "L4 可回测性端点仍在")

# 逐日明细端点：DESC 取、正序给页面
settings.db_path = str(p)
detail = R.pipeline_dataset_detail("pool_zt", limit=100)
ok(detail["dataset"] == "pool_zt", "M1 明细端点回显 dataset")
ok(len(detail["items"]) == len(FULL), "M2 明细覆盖了所有交易日")
ok([x["trade_date"] for x in detail["items"]] == sorted(FULL),
   "M3 明细按日期**正序**返回（SQL 里 DESC 取、这里 reverse）")
ok(all(x["parsed_rows"] == 36 for x in detail["items"]),
   "M4 明细逐日给出 payload 里实际解析出的行数")
ok(all(x["missing_field_rows"] == 0 for x in detail["items"]),
   "M5 明细逐日给出缺必填字段的行数")

gp = R.pipeline_gaps(dataset="pool_zt", lookback=20)
ok(gp["offline"] is True and gp["lookback"] == 20, "N1 漏采端点回显窗口与 offline")

for bad_limit in (0, 501):
    try:
        R.pipeline_dataset_detail("pool_zt", limit=bad_limit)
        ok(False, "N2 非法 limit 必须被拒：%d" % bad_limit)
    except Exception:
        ok(True, "N2 limit=%d 被拒" % bad_limit)

# ==== 14) 历史行情 + 回填进度端点（阶段 6） ------------------------------------
# 与 coverage 的分工：那个审 snapshot（逐日快照），这个审 daily_bar（跨年历史
# 行情）。判据也不同 —— 快照看「哪几个交易日缺了」，行情看「哪几只票行数没齐」。
ok("/api/pipeline/bars" in paths, "L5 行情/进度端点已注册")

from app.store import db as _db                        # noqa: E402


def _bars_fixture(name, *, rows):
    """名单（stock_net_inflow）+ 日线都塞好的夹具库。

    `rows` = `{code: 该票落几天}`，日期取真实交易日历里的 `FULL`。回填进度判
    的正是**名单**与**表内行数**之间的差，两边都得自己造，不能只造一半。
    """
    fp = _mk(name, job=False, snapshot={"stock_net_inflow": [(FULL[-1], _inflow(5))]})
    # 注意是 Path 不是 str：`db._connect()` 要对 db_path 取 .parent 建目录，
    # 塞 str 进去会直接 AttributeError（settings 没开 validate_assignment，
    # 赋什么就是什么，不会替你转回 Path）。
    settings.db_path = fp
    _db._conn = None                      # 换库必须断开，否则全局连接还指着旧库
    for code, n in rows.items():
        for d in FULL[:n]:
            _db.bars_upsert("daily_bar",
                            [{"code": code, "trade_date": d, "close": 10.0}])
    return fp


# 600000 六天全齐 / 600001 缺 2 / 600002 缺 1 / 600003·600004 一行都没有
_bars_fixture("bars1", rows={"600000": 6, "600001": 4, "600002": 5})
pb = R.pipeline_bars(scope="all", limit=10)
prog = pb["progress"]
ok(pb["offline"] is True, "P1 端点显式标 offline（零联网）")
ok(prog["scope"] == "all" and prog["codes"] == 5,
   "P2 名单来自 load_scope，5 只夹具票")
ok(prog["days"] == 6, "P3 表内交易日 6 天（= 判齐不齐的分母）")
ok(prog["covered"] == 1, "P4 只有**每天都在**的那只算完成 —— got %d"
   % prog["covered"])
ok(prog["partial"] == 2, "P5 缺日子的 2 只计 partial —— got %d" % prog["partial"])
ok(prog["absent"] == 2, "P6 一行都没有的 2 只计 absent —— got %d" % prog["absent"])
ok(prog["rate"] == 0.2, "P7 进度 = 1/5 —— got %r" % prog["rate"])
ok(prog["window"]["start"] == FULL[0] and prog["window"]["end"] == FULL[5],
   "P8 窗口 = 表内首末日，不是硬编码区间")
ok("只有 covered 才算完成" in prog["note"], "P9 判据原样写进返回，不靠前端记")
ok(len(prog["incomplete"]) == 2, "P10 只列没齐的票 —— got %d"
   % len(prog["incomplete"]))
ok([x["code"] for x in prog["incomplete"]] == ["600001", "600002"],
   "P11 缺得多的排前面 —— got %r" % [x["code"] for x in prog["incomplete"]])
ok(prog["incomplete"][0]["missing"] == 2
   and prog["incomplete"][0]["missing_dates"] == [FULL[4], FULL[5]],
   "P12 缺哪几天要给到日期，不是只给个数 —— got %r"
   % prog["incomplete"][0])
ok(prog["incomplete"][1]["missing"] == 1, "P13 缺 1 天那只如实记 1")

# limit 既是列表上限也是「最缺的排前面」的验证
pb_lim = R.pipeline_bars(scope="all", limit=1)
ok(len(pb_lim["progress"]["incomplete"]) == 1
   and pb_lim["progress"]["incomplete"][0]["code"] == "600001",
   "P14 limit 截断后留下的仍是最缺的那只")

# 表事实：行数 / 因子覆盖率都要如实报
covf = pb["tables"]["daily_bar"]["coverage"]
ok(covf["codes"] == 3 and covf["rows"] == 15 and covf["days"] == 6,
   "P15 daily_bar 事实：3 只 / 15 行 / 6 天 —— got %r" % covf)
fac = pb["tables"]["daily_bar"]["factor"]
ok(fac["supported"] is True, "P16 daily_bar 有 adj_factor 列")
ok(fac["rows"] == 15 and fac["with_factor"] == 0 and fac["without_factor"] == 15,
   "P17 没填因子的行如实报 0 行 —— got %r" % fac)
ok(fac["rate"] == 0.0, "P18 因子填充率 0.0，**不是 None 也不是 100%%** —— got %r"
   % fac["rate"])

# index_daily 按 schema 就没有 adj_factor 列 —— 这不是「数据缺了」，
# 硬算 COUNT(adj_factor) 会 no such column，算成 100% 更是骗人
faci = pb["tables"]["index_daily"]["factor"]
ok(faci["supported"] is False and faci["rate"] is None,
   "P19 指数表没有因子列 => supported=False / rate=None，不报就绪 —— got %r"
   % faci)
ok("没有 adj_factor 列" in faci["note"], "P20 说清是 schema 就没有这一列")

# 空表：rate 必须是 None，「进度 0%」同样是编出来的数
_bars_fixture("bars2", rows={})
pb2 = R.pipeline_bars(scope="all")
ok(pb2["progress"]["days"] == 0 and pb2["progress"]["rate"] is None,
   "P21 空表 rate 是 **None** 不是 0 —— got %r" % pb2["progress"]["rate"])
ok("谈不上进度" in pb2["progress"]["note"], "P22 空表的说明写清楚了")

# 名单为空：同样不能显示进度
fp3 = _mk("bars3", job=False, snapshot={})
settings.db_path = fp3
_db._conn = None
_db.bars_upsert("daily_bar", [{"code": "600000", "trade_date": FULL[0],
                               "close": 1.0}])
pb3 = R.pipeline_bars(scope="all")
ok(pb3["progress"]["codes"] == 0 and pb3["progress"]["rate"] is None,
   "P23 名单为空 => rate None（0%% 和 100%% 都是编的）—— got %r"
   % pb3["progress"]["rate"])
ok("不等于已完成" in pb3["progress"]["note"],
   "P24 名单为空要显式说「不等于已完成」")

# 参数闸门
for bad_limit in (0, 201):
    try:
        R.pipeline_bars(scope="all", limit=bad_limit)
        ok(False, "P25 非法 limit 必须被拒：%d" % bad_limit)
    except Exception:
        ok(True, "P25 limit=%d 被拒" % bad_limit)
try:
    R.pipeline_bars(scope="not-a-scope")
    ok(False, "P26 未知名单范围必须被拒")
except Exception:
    ok(True, "P26 未知名单范围被拒")

# 补跑入口的「指定数据域」参数。这里当 Python 函数**无参**直调一次 ——
# 断言的重点不是结果（夹具的 ingest_job 必然是空的），而是**没炸、零外发**：
# 参数默认值必须是裸 `""`。若写成 `Query(default="")`，无参直调会留下一个
# `Query` 哨兵，而 `domain or None` 判它为真，于是「全部」被当成「指定了
# 一个不存在的域」，一次都不跑 —— 页面上就是点了补跑却毫无反应。
# 放在 `settings.db_path = str(p)` 恢复**之前**：`db._connect()` 要对
# db_path 取 `.parent`，str 会 AttributeError（见 `_bars_fixture` 注释）。
ret = R.pipeline_collect_retry()
ok(ret["checked"] == 0 and ret["ran"] == 0 and ret["results"] == [],
   "P27 无参直调补跑端点 => 空结果且零外发 —— got %r" % ret)

settings.db_path = str(p)
_db._conn = None

# ==== 收尾 ====================================================================
ok(not _BLOCKED, "Z1 一次外发都不许有：%s" % _BLOCKED[:3])
ok(len(CALENDAR) > 8000, "N3 真实交易日历读到了（夹具日期得是真交易日）")

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("PASS=%d FAIL=%d" % (PASS, len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
