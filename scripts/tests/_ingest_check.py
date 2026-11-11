# -*- coding: utf-8 -*-
r"""逐域采集状态机 / 重试 / 校验闸门的**离线**回归，2026-10-16。

对应 `app/pipeline/ingest.py`、`store/db.py` 的 `ingest_*`、`scheduler.py`
的 `_collect_domain` / `_finish`，与 docs/data_pipeline/audit.md §11 阶段 3。

每一条都盯住一个真出过的点：

  1) **`pool_zt=0` 不许再判 success**：`_finish` 过去只看「有没有步骤抛异常」，
     于是 `{"pool_zt": 0}` 也记成 success（真库 job_run #18 就是这么记的）。
     现在落库 0 行判 `PARTIAL`，整轮判 `partial`。
  2) **字段缺失也是「没采全」**：10 行里 5 行缺 code 必须是 `PARTIAL`。
     这条是我写完状态机、跑冒烟测时才现出来的 bug —— 闸门只看行数，
     `record` 收了 `failed_rows` 却没用它降级，等于没有闸门。
  3) **校验只报不改**：缺字段的行原样落库，`code` 仍是 `None`，**绝不补 0**。
     这是任务书「不得把缺失填成 0」的落地点。
  4) **有上限的重试 + 指数退避**：源挂着时无限重试会把请求打爆，也会让
     「最近一次错误」永远被下一次失败盖掉。
  5) **`ingest_due` 只回到点的**：把没到点的捞出来等于绕过退避。
  6) **`store=False` 不许碰 snapshot**：`mainforce.run()` 自己落的是包型
     payload（`[{rows, all_rows, params, model}]`），再 db.save 一次会用裸行
     列表把整个包覆盖掉，`replay_score` 当场废掉。
  7) **异常路径也要落逐域状态**：`_collect_domain` 抛异常时先记 WAITING/FAILED
     再上抛，否则那一行会永久停在 RUNNING。
  8) **`failed` 与 `partial` 不合并**：前者查网络，后者查源为什么只回了一半。

  9) **同域同天不许并发重复执行**（阶段 2）：拒绝时**一个字段都不写**，
     且「没跑」不许记成「跑挂了」—— 数据照样由正在跑的那一轮落库。
 10) **数据源、覆盖范围、状态词别名**（阶段 2）：出问题要答得出「这批数据
     是哪儿来的、盖了多少、校验过了没」；库内大写枚举不动，对外多一层
     任务书用词。

  11) **六个每日任务只有一个入口**（阶段 3）：交易日由 `run_task` 统一定，任务自己不推算；落库的域先置 RUNNING 再取数、抛异常也要落终态 ——
      **漏了置位这一步，`ingest_finish` 那条 UPDATE 会 0 行命中，状态一个字
      都写不进去**，而任务那边却显示跑过了（这条是真跑出来的，不是想出来的）。
  12) **行级校验补两类**（阶段 3）：同一天同一 code 重复、关键价格 <= 0 都判
      没通过并降 PARTIAL；**价格缺失(None) 不算坏数据**（停牌股就是它），
      「补 0」才是造假。

全离线：开头封死 TCP + DNS，用临时夹具库，不碰真实 market.db。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_ingest_check.py
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

_ROOT = Path(tempfile.mkdtemp(prefix="ingest_check"))
settings.db_path = _ROOT / "market.db"

from app.store import db                                         # noqa: E402
from app.pipeline import ingest as I                             # noqa: E402

PASS = 0
FAILS = []


def ok(cond, label):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)


D = "20261014"
CODES = ("code", "name", "price")


def _rows(n, drop_code=False, drop_name=False):
    out = []
    for i in range(n):
        out.append({"code": None if drop_code else "60000%d" % i,
                    "name": None if drop_name else "夹具%d" % i,
                    "price": 10.0 + i, "pct_chg": 9.9})
    return out


# ---- 1) UPSERT：重跑同一天是覆盖，attempts 累加 -----------------------------
db.ingest_start("pool_zt", D)
r = db.ingest_get("pool_zt", D)
ok(r["status"] == I.RUNNING and r["attempts"] == 1, "A1 首次 start => RUNNING, attempts=1")
ok(r["started_at"] and r["updated_at"], "A2 started_at / updated_at 已填")
db.ingest_start("pool_zt", D)
r = db.ingest_get("pool_zt", D)
ok(r["attempts"] == 2, "A3 重跑同一天 => attempts 累加，不新增行")
ok(r["status"] == I.RUNNING, "A4 重跑时清回 RUNNING")
ok(len(db.ingest_list(domain="pool_zt")) == 1,
   "A5 同一 (domain, trade_date) 只有一行 —— 幂等靠唯一键")

# ---- 2) 闸门：0 行不许判 SUCCESS（修 pool_zt=0 仍 success） ------------------
db.ingest_start("pool_zt", D)
out = I.record("pool_zt", D, [], source="fixture", required=CODES)
ok(out["status"] == I.PARTIAL, "B1 落库 0 行 => PARTIAL，不许 SUCCESS")
ok("落库 0 行" in " ".join(out["reasons"]), "B2 原因写明是 0 行")
r = db.ingest_get("pool_zt", D)
ok(r["status"] == I.PARTIAL and r["stored"] == 0, "B3 状态与 stored 都落了库")
ok("落库 0 行" in (r["error"] or ""), "B4 错误栏写了原因，不是空串")
ok(r["processed"] == 0, "B5 processed 也是 0，不是 NULL")

# ---- 3) allow_empty 显式放行 -------------------------------------------------
db.ingest_start("pool_dt", D)
out = I.record("pool_dt", D, [], source="fixture", allow_empty=True)
ok(out["status"] == I.SUCCESS, "C1 显式 allow_empty 才允许 0 行判 SUCCESS")

# ---- 4) 正常齐整 => SUCCESS --------------------------------------------------
db.ingest_start("pool_strong", D)
ok(I.record("pool_strong", D, _rows(30), source="fixture",
            required=CODES)["status"] == I.SUCCESS, "D1 字段齐整 => SUCCESS")
r = db.ingest_get("pool_strong", D)
ok(r["stored"] == 30 and r["failed_rows"] == 0, "D2 stored=30 / failed_rows=0")
ok(db.load("pool_strong", D) is not None and len(db.load("pool_strong", D)) == 30,
   "D3 确实落库 30 行")

# ---- 5) 字段缺失 => PARTIAL，且**不改数据** ----------------------------------
src = _rows(5, drop_code=True)
snapshot_before = json.dumps(src, sort_keys=True, default=str)
db.ingest_start("pool_zb", D)
out = I.record("pool_zb", D, src, source="fixture", required=CODES)
ok(out["status"] == I.PARTIAL, "E1 字段缺失 => PARTIAL，不是 SUCCESS")
ok(out["failed_rows"] == 5, "E2 5 行全被判缺字段")
ok(any("code" in s for s in out["reasons"]), "E3 原因点名了缺的是哪个字段")
loaded = db.load("pool_zb", D)
ok(loaded[0]["code"] is None, "E4 缺失仍是 None，**没有**被填成 0 或空串")
ok(json.dumps(src, sort_keys=True, default=str) == snapshot_before,
   "E5 调用方传进来的行**没被就地改过**")
ok(len(loaded) == 5, "E6 原样落库 5 行，没被筛掉")

# 部分缺失只算那部分
db.ingest_start("pool_zt", "20261015")
out = I.record("pool_zt", "20261015", _rows(5, drop_name=True),
               source="fixture", required=CODES)
ok(out["failed_rows"] == 5 and out["status"] == I.PARTIAL, "E7 缺 name 也算")

# ---- 6) 必填字段按行形状推（板块资金流没有 code） ----------------------------
ok(I.validate_rows([{"name": "煤炭", "net_inflow": 1}])[1] == 0,
   "F1 只有 name 的行，验 name 不硬要 code")
ok(I.validate_rows([{"name": None}])[1] == 1, "F2 name 为空 => 判缺")
ok(I.validate_rows([{"x": 1}, {"x": 2}])[1] == 0,
   "F3 既没 code 也没 name 的行不强求键名")
ok(I.validate_rows([1, 2, 3])[1] == 3, "F4 非对象行一律判缺")

# ---- 7) 重试链：退避 + 上限 --------------------------------------------------
# 用一个**前面没碰过**的 (domain, trade_date)：attempt 计数是从 1 开始的，
# 拿测试 3 已经 start 过的那对来跑，第 1 次就会读到 attempts=2 直接判 FAILED，
# 整条退避链就测不出来了。
RETRY_D = "20260101"
for attempt in (1, 2, 3):
    db.ingest_start("retry_probe", RETRY_D)
    res = I.record_failure("retry_probe", RETRY_D, ConnectionError("源超时"))
    r = db.ingest_get("retry_probe", RETRY_D)
    ok(r["attempts"] == attempt, "G%d 尝试次数记到 %d" % (attempt, attempt))
    if attempt < 3:
        ok(res["status"] == I.WAITING, "G%d 第 %d 次失败 => WAITING 排重试"
           % (attempt, attempt))
        ok(r["next_retry_at"] is not None, "G%d 排了下次重试时刻" % attempt)
        ok("ConnectionError" in (r["error"] or ""), "G%d 错误原因原样带回" % attempt)
    else:
        ok(res["status"] == I.FAILED, "G%d 第 3 次（用尽）=> FAILED" % attempt)
        ok(r["next_retry_at"] is None, "G%d 用尽后不再排重试" % attempt)
        ok(r["status"] == I.FAILED, "G%d 状态也落成 FAILED" % attempt)

ok([I.backoff_seconds(n) for n in (1, 2, 3, 4, 5)] == [15, 30, 60, 120, 240],
   "G6 指数退避 15/30/60/120/240")
ok(I.backoff_seconds(99) == 300, "G7 退避有上限 300s，不会等到天荒地老")

# ---- 8) ingest_due 只回**到点**的 --------------------------------------------
DUE_D = "20260102"
db.ingest_start("due_probe", DUE_D)
I.record_failure("due_probe", DUE_D, TimeoutError("超时"))     # 排一次退避
ok(db.ingest_due(now_iso="1990-01-01T00:00:00+00:00") == [],
   "H1 没到点的一条都不回")
due = db.ingest_due(now_iso="2999-01-01T00:00:00+00:00")
ok(any(d["domain"] == "due_probe" and d["trade_date"] == DUE_D for d in due),
   "H2 到点的回得出来")
ok(all(d["attempts"] < d["max_attempts"] for d in due),
   "H3 回的都是还没用尽重试次数的")
ok(all(d["next_retry_at"] for d in due), "H4 回的都排过重试时刻")

# ---- 9) store=False 不碰 snapshot（保护包型 payload） ------------------------
ENVELOPE = [{"rows": _rows(3), "all_rows": _rows(4),
             "params": {"top_n": 30}, "model": {"degraded": []}}]
db.save("main_force", D, ENVELOPE, source="mainforce")   # 模拟 mainforce.run 落库
before = json.dumps(db.load("main_force", D), sort_keys=True)
# 打分行自带 score —— 必填字段是 ("code","score")，拿普通行会误判 PARTIAL
_scored = [{"code": "60000%d" % i, "name": "夹具%d" % i, "score": 80.0 - i}
           for i in range(3)]
db.ingest_start("main_force", D)
out = I.record("main_force", D, _scored, store=False, required=("code", "score"))
after = json.dumps(db.load("main_force", D), sort_keys=True)
ok(before == after, "I1 store=False 时 snapshot 一个字节都没被改写")
ok(sorted(db.load("main_force", D)[0].keys()) == ["all_rows", "model", "params", "rows"],
   "I2 包型 payload 的四个顶层键都还在")
ok(out["status"] == I.SUCCESS and out["stored"] == 3, "I3 校验与状态照常登记")

db.ingest_start("main_force", "20261015")
ok(I.record("main_force", "20261015", [{"code": "600001", "name": "x"}],
            store=False, required=("code", "score"))["status"] == I.PARTIAL,
   "I4 内层行缺 score => PARTIAL（不是全判缺字段）")
out = I.record("main_force", "20261016",
               [{"code": "600001", "name": "x", "score": 80.0}],
               store=False, required=("code", "score"))
ok(out["status"] == I.SUCCESS and out["failed_rows"] == 0,
   "I5 内层行有 score => SUCCESS")

# ---- 10) 异常路径也要落逐域状态 ----------------------------------------------
def _boom():
    raise ConnectionError("下游挂了")


from app import scheduler                                        # noqa: E402

# 用全新日期：拿前面已经 PARTIAL 过的 (pool_zt, 20261014) 去测「正常一步不进
# partial」，闸门会照样把它抓出来，断言就永远是假的。
JD = "20260201"
J1 = db.job_start("manual", json.dumps({"date": JD}))
results: dict = {}
failed: list = []
partial: list = []
scheduler._run_step(results, failed, "pool_zt", lambda: 30, live=False,
                    domain="pool_zt", trade_date=JD, partial=partial)
ok(not failed and not partial, "J1 正常一步不进 failed / partial")

# 真正的异常路径：走 _collect_domain，它负责在上抛之前把状态落掉
try:
    scheduler._collect_domain("pool_zb", JD, _boom)
    ok(False, "J2 异常必须原样上抛")
except ConnectionError:
    ok(True, "J2 异常原样上抛给 _run_step")
r = db.ingest_get("pool_zb", JD)
ok(r is not None and r["status"] in (I.WAITING, I.FAILED),
   "J3 异常路径也落了逐域状态，没永久卡在 RUNNING")
ok("ConnectionError" in (r["error"] or ""), "J4 异常原因记进了 error")

# 闸门不过 => 进 partial，与 failed 分开。**得让这一步自己写出 PARTIAL 行**
# —— `_run_step` 只负责读闸门，lambda 返回 0 什么都没落库，闸门自然查无此行。
def _zero_step():
    db.ingest_start("pool_strong", JD)
    I.record("pool_strong", JD, [], required=CODES)
    return 0


scheduler._run_step(results, failed, "pool_strong", _zero_step, live=False,
                    domain="pool_strong", trade_date=JD, partial=partial)
ok("pool_strong" in partial and "pool_strong" not in failed,
   "J5 闸门不过 => 进 partial，**不**进 failed")

# ---- 11) _finish 状态汇总 ----------------------------------------------------
def _fin(failed_, partial_, n):
    jid = db.job_start("manual", json.dumps({"d": n}))
    res = {("pool_zt" if i < 3 else "pool_zb"): i for i in range(n)}
    return scheduler._finish(jid, "20261014", False, True, res, failed_, partial_)

ok(_fin([], [], 7) == "success", "K1 全成 => success")
ok(_fin([], ["pool_zt"], 7) == "partial", "K2 只有闸门不过 => partial（不再 success）")
ok(_fin(["pool_zt"], [], 7) == "partial", "K3 只有异常 => partial")
ok(_fin(["pool_zt"], ["pool_zb"], 7) == "partial", "K4 两者都有 => partial")
ok(_fin(["a", "b", "c"], [], 3) == "failed", "K5 全挂 => failed")

# job_run.detail 里必须单独记 partial，与 failed 分开
last = db.last_job()
detail = json.loads(last["detail"])
ok("partial" in detail and isinstance(detail["partial"], list),
   "K6 detail 里单独记了 partial 列表")
ok(detail["failed"] != detail["partial"] or not detail["failed"],
   "K7 failed 与 partial 是两份，不混写")

# ---- 12) run_retries 的分发 --------------------------------------------------
def _due_now(domain: str, trade_date: str) -> None:
    """只把**指定这一行**推到「已到点」。

    绝不能写成整表 UPDATE —— 前面那些测试留下的 WAITING 行会被一起推到点，
    `run_retries` 会真去调取数器，在内网里留下真实的外发记录（我在第一版
    测试里就这么翻过一次车，是 socket 守卫把它拦下来的）。
    """
    con = sqlite3.connect(settings.db_path)
    con.execute("UPDATE ingest_job SET next_retry_at='1990-01-01T00:00:00+00:00' "
                "WHERE domain=? AND trade_date=?", (domain, trade_date))
    con.commit()
    con.close()


ran = []
for dom in ("pool_zt", "sector_flow_industry"):
    db.ingest_start(dom, "20261013")
    I.record_failure(dom, "20261013", ConnectionError("x"))
    _due_now(dom, "20261013")
db.ingest_start("weird_domain", "20261013")
I.record_failure("weird_domain", "20261013", ConnectionError("x"))
_due_now("weird_domain", "20261013")

# 用完全离线的替身验证分发。**整个 L 段都得在替换期间跑完** —— 任何一次
# 真调 fetch_pool 都是一次真实的内网外发。
_orig = (scheduler.fetch_pool, scheduler.fetch_sector_flow,
         scheduler._score_and_record)
scheduler.fetch_pool = lambda kind, d: ran.append(("pool", kind, d)) or 0
scheduler.fetch_sector_flow = lambda kind, d="latest": ran.append(("sector", kind, d)) or 0
scheduler._score_and_record = lambda d: ran.append(("score", "", d)) or 0
try:
    out = scheduler.run_retries()
    ok(out["checked"] == 3 and out["ran"] == 2,
       "L1 到点 3 条：2 条能跑、1 条跳过")
    ok(("pool", "zt", "20261013") in ran, "L2 pool_zt 分发正确")
    ok(("sector", "industry", "20261013") in ran, "L3 板块分发正确")
    ok(all(r["status"] == "ok" for r in out["results"]
           if r["status"] != "skipped"), "L4 每条能跑的都回了状态")
    skipped = [r for r in out["results"] if r["status"] == "skipped"]
    ok(skipped and "不认识" in skipped[0]["reason"],
       "L5 未知数据域显式跳过并说明原因，不静默当成功")

    # ---- 指定数据域（阶段 6 页面的「只补这个域」入口）----
    # 筛的只是**哪些行**，「只跑到点的」这条不因此改变。断言一律用集合比对、
    # 不写死「1 条」：G/H/J 埋的 15 秒退避若恰好在两次读之间到期，写死数字
    # 就是在拿墙钟赌绿（L7 那段注释记的就是这坑）。
    due_pool = [r for r in db.ingest_due() if r["domain"] == "pool_zt"]
    ran.clear()
    out_zt = scheduler.run_retries(domain="pool_zt")
    ok(bool(due_pool) and out_zt["checked"] == len(due_pool),
       "L8 指定 pool_zt 的 checked = 该域到点条数（%d）" % len(due_pool))
    ok(out_zt["results"] and all(r["domain"] == "pool_zt"
                                 for r in out_zt["results"]),
       "L9 结果里只出现被点名的那一个域")
    ok(all(t[0] == "pool" for t in ran),
       "L10 没碰过别的域的取数器 —— %r" % ran)

    # 点名一个库里没有的域：0 条，且**一个取数器都不许被调** ——
    # 没命中就回落成「全部重跑」是最坏的实现，页面上点一个下拉框就发一批外发。
    ran.clear()
    out_np = scheduler.run_retries(domain="__no_such_domain__")
    ok(out_np["checked"] == 0 and out_np["ran"] == 0,
       "L11 点名没到点的域 => 0/0，不是「那就全部重跑」")
    ok(not ran, "L12 没命中时一个取数器都没被调 —— %r" % ran)
finally:
    scheduler.fetch_pool = _orig[0]
    scheduler.fetch_sector_flow = _orig[1]
    scheduler._score_and_record = _orig[2]

ok(not _BLOCKED, "L6 run_retries 一次真实外发都没有：%s" % _BLOCKED[:3])

# run_retries 处理的必须**正好**是 ingest_due 回的那一批，不多少不少。
# （原先这里想断言「没到点就一条都不跑」，但前面 G/H/J 埋的退避是「现在
# +15 秒」，测试跑够 15 秒就真到点了 —— 而那时取数器已经还原成真的了，
# 于是一次断言变成了真实的内网外发。改成比对两个集合，与墙钟无关。）
ok(len(db.ingest_due()) == out["checked"],
   "L7 run_retries 处理的正好是到点那一批（%d 条）" % out["checked"])

# ---- 13) startup_check 零联网 ------------------------------------------------
out = I.startup_check()
ok(out["offline"] is True, "M1 启动自检声明零联网")
ok("gaps" in out and isinstance(out["gaps"], dict), "M2 给出漏采清单")
ok(not _BLOCKED, "M3 启动自检一次外发都没有：%s" % _BLOCKED[:3])

# ---- 14) 端点接线 ------------------------------------------------------------
from app.api import routes as R                                  # noqa: E402
paths = {getattr(x, "path", "") for x in R.router.routes}
ok("/api/pipeline/ingest" in paths, "N1 逐域采集状态端点已注册")
ok("/api/pipeline/collect/retry" in paths, "N2 重试端点已注册")
ok("/api/pipeline/coverage" in paths, "N3 覆盖审计端点仍在")
ok("/api/jobs/run" in paths, "N4 原有手动补跑入口仍在")

resp = R.pipeline_ingest(limit=100)
ok(resp["offline"] is True and resp["count"] >= 1, "O1 状态端点返回记录")
ok("PARTIAL" in resp["note"] and "FAILED" in resp["note"],
   "O2 端点自带状态含义说明，不让人猜")
due_resp = R.pipeline_ingest(due_only=True)
ok(isinstance(due_resp["items"], list), "O3 due_only 也能调")

for bad_limit in (0, 501):
    try:
        R.pipeline_ingest(limit=bad_limit)
        ok(False, "O4 非法 limit 必须被拒：%d" % bad_limit)
    except Exception:
        ok(True, "O4 limit=%d 被拒" % bad_limit)

# ---- 15) 阶段 2：同域同天的并发互斥 ------------------------------------------
# 页面刷新撞上定时任务是常事。没有这道闸就等于双倍打源；而「没跑」与「跑挂
# 了」必须分开记 —— 混进 failed 会让人以为断网了，实际数据照样在别的线程里
# 落库。
BK = "20260301"
db.ingest_try_start("pool_zt", BK, source="em:ztb")
r = db.ingest_get("pool_zt", BK)
ok(r["source"] == "em:ztb", "P1 数据源落进 ingest_job")
first_started, first_attempts = r["started_at"], r["attempts"]

busy = db.ingest_busy("pool_zt", BK)
ok(busy is not None and busy["status"] == I.RUNNING, "P2 ingest_busy 认得出正在跑")
try:
    db.ingest_try_start("pool_zt", BK, source="tencent:hist_tx")
    ok(False, "P3 同域同天在跑时必须拒绝")
except db.DomainBusy as exc:
    ok(exc.domain == "pool_zt" and exc.trade_date == BK,
       "P3 DomainBusy 点名了是哪个域哪一天")
r = db.ingest_get("pool_zt", BK)
ok(r["attempts"] == first_attempts and r["started_at"] == first_started,
   "P4 拒绝时一个字段都没写（attempts 不加、started_at 不动）")
ok(r["source"] == "em:ztb", "P5 也没把数据源换成后来者")

# 取数器只数调用次数，绝不真发请求
_probed: list = []


def _probe_fetch():
    _probed.append(1)
    return _rows(3)


try:
    scheduler._collect_domain("pool_zt", BK, _probe_fetch, source="tencent")
    ok(False, "P6 busy 必须原样上抛给 _run_step")
except db.DomainBusy:
    ok(True, "P6 busy 原样上抛")
ok(not _probed, "P7 拒绝时一次取数都没发：%r" % _probed[:3])
r = db.ingest_get("pool_zt", BK)
ok(r["attempts"] == first_attempts and r["status"] == I.RUNNING,
   "P8 拒绝时既没加 attempts，也没把状态冲成 WAITING")

# _run_step 遇 busy：记「跳过」，既不进 failed 也不进 partial
res2: dict = {}
failed2: list = []
partial2: list = []
scheduler._run_step(
    res2, failed2, "pool_zt",
    lambda: scheduler._collect_domain("pool_zt", BK, _probe_fetch),
    live=False, domain="pool_zt", trade_date=BK, partial=partial2)
ok("pool_zt" not in failed2 and "pool_zt" not in partial2,
   "P9 busy 既不进 failed 也不进 partial")
ok("跳过" in str(res2.get("pool_zt", "")), "P10 结果里写明是「跳过」")

# RUNNING 超时（上一轮进程挂了）=> 允许接管。没有这一条，一次崩溃就会把
# 这域这天永久锁死，页面上写着「正在采集」却永远不会有下文。
con = sqlite3.connect(settings.db_path)
con.execute("UPDATE ingest_job SET started_at='1970-01-01T00:00:00+00:00' "
            "WHERE domain='pool_zt' AND trade_date=?", (BK,))
con.commit()
con.close()
ok(db.ingest_busy("pool_zt", BK) is None, "P11 陈旧的 RUNNING 不再挡路")
db.ingest_try_start("pool_zt", BK, source="tencent:hist_tx")
r = db.ingest_get("pool_zt", BK)
ok(r["attempts"] == first_attempts + 1 and r["source"] == "tencent:hist_tx",
   "P12 接管时照常 +1 并换上新数据源")
I.record("pool_zt", BK, _rows(3), source="tencent", required=CODES)
ok(db.ingest_busy("pool_zt", BK) is None, "P13 收尾之后就不再 busy")

# ---- 16) 阶段 2：数据源 / 覆盖范围与校验结果 ---------------------------------
CV = "20260302"
db.ingest_start("cov_probe", CV)
I.record("cov_probe", CV, _rows(5, drop_code=True) + _rows(5),
         source="fixture", required=CODES)
r = db.ingest_get("cov_probe", CV)
ok(abs((r["coverage_rate"] or 0) - 0.5) < 1e-9,
   "P14 覆盖率 = 本次通过校验的行占比（%.3f）" % (r["coverage_rate"] or 0))
d = json.loads(r["coverage_detail"])
ok(d["processed"] == 10 and d["stored"] == 10 and d["failed_rows"] == 5
   and d["validated"] == 5,
   "P15 校验结果逐项可查，不是一个笼统的分")
ok(d["reasons"] and "code" in " ".join(d["reasons"]),
   "P16 连缺的是哪个字段都带着")
ok(d["source"] == "fixture", "P17 覆盖明细里也记了数据源")

db.ingest_start("cov_zero", CV)
I.record("cov_zero", CV, [], required=CODES)
ok(db.ingest_get("cov_zero", CV)["coverage_rate"] is None,
   "P18 0 行 => 覆盖率是 NULL 而不是 0.0（没有可校验的行 ≠ 覆盖率为零）")

# 一次抛异常的重试不该把上一轮算好的覆盖范围抹掉：覆盖率描述的是**数据**，
# 不是**这次尝试**。
ok(I.record_failure("cov_probe", CV, ConnectionError("x"))["status"] == I.WAITING,
   "P19 先让它失败一次")
r = db.ingest_get("cov_probe", CV)
ok(abs((r["coverage_rate"] or 0) - 0.5) < 1e-9
   and json.loads(r["coverage_detail"])["validated"] == 5,
   "P20 失败重试不抹掉已算好的覆盖范围")

# ---- 17) 阶段 2：对外状态词别名 + job_run.trade_date -------------------------
ok(I.api_status(I.WAITING) == "pending" and I.api_status(I.RUNNING) == "running"
   and I.api_status(I.SUCCESS) == "success"
   and I.api_status(I.PARTIAL) == "partial_success"
   and I.api_status(I.FAILED) == "failed",
   "Q1 五个状态词都有对外说法")
ok(I.api_status("SOMETHING_NEW") == "SOMETHING_NEW",
   "Q2 认不出的状态原样回给，不硬说成 failed")

jid = db.job_start("manual", json.dumps({"d": 1}), trade_date="20260303")
db.job_finish(jid, "success", "ok")
ok(db.last_job()["trade_date"] == "20260303", "Q3 job_run 记下了跑的是哪一天")
jid2 = db.job_start("manual", "{}")
db.job_finish(jid2, "success", "")
ok(db.last_job()["trade_date"] is None, "Q4 没指定日期时是 NULL，不是空串")

resp = R.pipeline_ingest(limit=300)
ok(resp["items"] and all("status_alias" in it for it in resp["items"]),
   "Q5 状态端点每条都带 status_alias")
ok(any(it.get("status_alias") == "partial_success" for it in resp["items"]),
   "Q6 库内的 PARTIAL 对外叫 partial_success")

# 补跑也要留任务记录 —— 事后要能回答「哪天补的、补了哪些」，而不是翻日志
con = sqlite3.connect(settings.db_path)
rows = con.execute("SELECT kind, status, trade_date FROM job_run "
                   "WHERE kind='retry' ORDER BY id DESC").fetchall()
con.close()
ok(rows and rows[0][1] in ("success", "partial", "failed"),
   "Q7 补跑留了任务记录且状态落定")

# 回填同理：参数不合法时**一次请求都不发**，但任务记录照样留一条 failed
try:
    R.pipeline_backfill({"domain": "__no_such_domain__",
                         "start": "20260101", "end": "20260131"})
    ok(False, "Q8 未知回填域必须被拒")
except Exception:
    ok(True, "Q8 未知回填域被拒了")
con = sqlite3.connect(settings.db_path)
# 阶段 3 起回填改走统一任务入口，任务记录的 kind 也跟着变成任务名 ——
# 「backfill」这个第二套名字不再产生，登记口只剩一个。
brow = con.execute("SELECT status, detail FROM job_run "
                   "WHERE kind='historical_backfill' "
                   "ORDER BY id DESC LIMIT 1").fetchone()
con.close()
ok(brow is not None and brow[0] == "failed", "Q9 回填失败也留了任务记录")
ok(not _BLOCKED, "Q10 以上全程零外发：%s" % _BLOCKED[:3])

# ---- 18) 阶段 3：行级校验补两类（任务书「验证重复记录 / 关键价格」）----------
_dup = _rows(3) + [_rows(1)[0]]        # 600000..600002 各一次 + 600000 再来一次
_total, _bad, _why = I.validate_rows(_dup, CODES)
ok(_bad == 1, "R1 同一天同一 code 出现两次 => 计 1 行没通过 —— %d" % _bad)
ok(any("重复" in w for w in _why), "R2 原因里点名「重复」")
_total, _bad, _ = I.validate_rows(
    [{"code": "600000", "name": "a", "price": 0.0}], CODES)
ok(_bad == 1, "R3 关键价格=0 判没通过（坏数据，不是没采到）")
_total, _bad, _ = I.validate_rows(
    [{"code": "600001", "name": "a", "price": -1.5}], CODES)
ok(_bad == 1, "R4 关键价格为负同样判没通过")
_total, _bad, _ = I.validate_rows(
    [{"code": "600002", "name": "a", "price": None}], ("code",))
ok(_bad == 0, "R5 价格缺失(None)是**合法缺失**，不是坏数据 —— 停牌股就是它"
   "（必填集里没写 price 时不判缺）")
# 判据加严**不改落库口径**：重复的行照样原样存，校验只报不改
out = I.record("pool_zt", "20261016", _dup, source="fixture", required=CODES)
ok(out["status"] == I.PARTIAL and out["stored"] == len(_dup),
   "R6 重复行照常落库且降 PARTIAL —— %s/%s"
   % (out["status"], out["stored"]))

# ---- 19) 阶段 3：六个每日任务 -------------------------------------------------
from app.pipeline import tasks as T                             # noqa: E402
from app.pipeline import backfill as bf                         # noqa: E402

ok(sorted(T.TASKS) == ["daily_fund_flow_sync", "daily_market_sync",
                       "daily_universe_sync", "data_quality_check",
                       "historical_backfill", "recovery_sync"],
   "R7 任务书点名的六个任务，一个不多一个不少")
try:
    T.run_task("__no_such_task__", trade_date=D)
    ok(False, "R8 未注册的任务必须被拒")
except ValueError:
    ok(True, "R8 未注册的任务被拒了")
try:
    T.run_task("daily_universe_sync", trade_date=D, budgt=10)
    ok(False, "R9 拼错的参数不许静默忽略")
except ValueError as exc:
    ok("budgt" in str(exc), "R9 报错点名是哪个参数 —— %s" % exc)

# 名单任务：零联网，口径 = stock_net_inflow 全归档日并集
out = T.run_task("daily_universe_sync", trade_date=D)
ok(out["status"] == "partial" and not (db.load("daily_universe", D) or []),
   "R10 名单为空 => PARTIAL，不许静默成功 —— %s" % out["status"])
I.record("stock_net_inflow", D,
         [{"code": "60000%d" % i, "net_inflow": 1.0} for i in range(5)],
         source="fixture", required=("code", "net_inflow"))
out = T.run_task("daily_universe_sync", trade_date=D)
uni = db.load("daily_universe", D) or []
_ok_row = db.ingest_get("daily_universe", D)
ok(out["status"] == "success" and [u["code"] for u in uni] ==
   ["600000", "600001", "600002", "600003", "600004"],
   "R11 名单落库且代码齐 —— %s" % [u["code"] for u in uni][:3])
ok(_ok_row["status"] == I.SUCCESS and
   _ok_row["source"] == "union:stock_net_inflow",
   "R12 名单域登记成功并如实记来源 —— %s" % _ok_row["source"])

# 全市场快照：条数下限是闸门，不足就是「只回了前 N 条」
def _fake_spot():  # noqa: ANN001, ANN202
    return [{"code": "%06d" % i, "name": "夹具%d" % i, "price": 10.0 + i,
             "pct_chg": 1.0, "turnover": 0.5, "float_mv": 1e8,
             "amount": 1e6, "is_st": False, "source": "eastmoney"}
            for i in range(T.MIN_MARKET_ROWS + 1)]


def _fake_thin_spot():  # noqa: ANN001, ANN202
    return _fake_spot()[:10]


_real_spot, _real_rank = T.quotes.market_snapshot, T.quotes.fund_flow_rank
T.quotes.market_snapshot = _fake_spot
try:
    out = T.run_task("daily_market_sync", trade_date=D)
    ok(out["status"] == "success", "R13 快照过条数下限判 success —— %s"
       % out["status"])
    ok(db.ingest_get("daily_market", D)["source"] == "eastmoney",
       "R14 快照来源如实记（多源混采会记 mixed）")
    ok(len(db.load("daily_market", D) or []) == T.MIN_MARKET_ROWS + 1,
       "R15 全市场快照整份落库")
finally:
    T.quotes.market_snapshot = _real_spot
T.quotes.market_snapshot = _fake_thin_spot
try:
    out = T.run_task("daily_market_sync", trade_date="20261013")
    ok(out["status"] == "partial",
       "R16 只回 10 行（远低于下限）=> PARTIAL，不许 SUCCESS —— %s"
       % out["status"])
finally:
    T.quotes.market_snapshot = _real_spot

# 资金流批量排名：四个窗口各记条数，覆盖范围是显式数字
def _fake_rank(indicator="今日"):  # noqa: ANN001, ANN202
    return [{"code": "%06d" % i, "name": "夹具%d" % i, "main_net": 1.0 * i,
             "source": "eastmoney"} for i in range(4)]


def _fake_thin_rank(indicator="今日"):  # noqa: ANN001, ANN202
    return _fake_rank()[:1]


T.quotes.fund_flow_rank = _fake_rank
try:
    out = T.run_task("daily_fund_flow_sync", trade_date=D)
    # `ingest_get` 回的是**原始行**，coverage_detail 仍是 JSON 文本 —— 别忘了
    # 解析，否则后面 `cov["windows"]` 拿字符串下标去取字典键。
    cov = json.loads(db.ingest_get("fund_flow_rank", D)["coverage_detail"])
    ok(out["status"] == "success",
       "R17 榜 4 只 / 名单 5 只（覆盖 80%%，过 60%% 门槛）判 success —— %s"
       % out["status"])
    ok(cov["windows"] == {"今日": 4, "3日": 4, "5日": 4, "10日": 4},
       "R18 四个窗口的条数都记了 —— %s" % cov["windows"])
    ok(cov["universe"] == 5 and abs(cov["rank_coverage"] - 0.8) < 1e-9,
       "R19 覆盖范围是显式数字 4/5，不是靠行数反推 —— %s"
       % (cov["rank_coverage"],))
finally:
    T.quotes.fund_flow_rank = _real_rank
T.quotes.fund_flow_rank = _fake_thin_rank
try:
    out = T.run_task("daily_fund_flow_sync", trade_date="20261013")
    ok(out["status"] == "partial",
       "R20 榜只回 1/5（覆盖 20%%，低于 60%% 门槛）=> PARTIAL：只回前 N "
       "必须记覆盖 —— %s"
       % out["status"])
finally:
    T.quotes.fund_flow_rank = _real_rank

# 数据质量检查：补窗口覆盖，且**不许动** coverage_rate 那一列
_before_n = len(db.ingest_list())
_before_rate = db.ingest_get("pool_zt", D)["coverage_rate"]
out = T.run_task("data_quality_check", trade_date=D)
_det = json.loads(db.ingest_get("pool_zt", D)["coverage_detail"])
ok(out["status"] == "success" and out["result"]["offline"] is True,
   "R21 数据质量检查零联网且判 success")
ok("window" in _det and isinstance(_det["window"]["missing_dates"], list),
   "R22 日期窗口缺口写进 coverage_detail.window")
ok(db.ingest_get("pool_zt", D)["coverage_rate"] == _before_rate,
   "R23 coverage_rate 不被窗口覆盖率覆盖 —— 一个列不能代表两件事")
ok(len(out["result"]["skipped"]) > 0 and len(db.ingest_list()) == _before_n,
   "R24 没有采集记录的域记 skipped，**不凭空插行**（编造的成功比漏采更坏）"
   " —— %s" % out["result"]["skipped"][:3])

# 补跑任务：接到 scheduler.run_retries，且不外发（socket 守卫在场）
# 先把「到点」的行清干净 —— 前面几节为了测退避留下过 next_retry_at，不清掉
# 这一步就真的会去朝数据源发包（守卫拦得下来，但那是一次真外发的企图）。
con = sqlite3.connect(settings.db_path)
con.execute("UPDATE ingest_job SET next_retry_at=NULL "
            "WHERE next_retry_at IS NOT NULL")
con.commit()
con.close()
out = T.run_task("recovery_sync", trade_date=D)
ok(out["job_id"] and out["status"] in ("success", "partial"),
   "R25 补跑任务接到 run_retries 且状态落定 —— %s（checked=%s）"
   % (out["status"], out["result"].get("checked")))

# 历史回填：end 缺省 = 交易日，不再写死 DEFAULT_START
_SEEN: dict = {}


def _fake_daily(code, start_date, end_date, **kw):  # noqa: ANN001, ANN202
    _SEEN.update(code=code, start=start_date, end=end_date)
    return [{"code": code, "trade_date": "2026-10-14", "open": 10.0,
             "high": 11.0, "low": 9.5, "close": 10.5, "volume": 1000,
             "amount": 10000.0, "pct_chg": 1.2, "turnover": 0.5}]


_real_daily = bf.fetch_daily
bf.fetch_daily = _fake_daily
try:
    out = T.run_task("historical_backfill", trade_date=D, domain="daily_bar",
                     start="20261010", items=["600000"], budget=1)
    ok(out["result"]["inserted"] == 1, "R26 回填任务跑通并落库 —— %s"
       % out["result"].get("inserted"))
    ok(_SEEN.get("end") == D,
       "R27 end 缺省 = 交易日，不是起始日 DEFAULT_START —— %s" % _SEEN.get("end"))
    # 页面按钮走的还是同一个入口：改走任务服务后**响应形状不许变**
    resp = R.pipeline_backfill({"domain": "daily_bar", "start": "20261010",
                                "end": "20261015", "items": ["600001"],
                                "budget": 1})
    ok(resp.get("finished") is True and resp.get("inserted") == 1,
       "R28 页面回填端点响应形状不变（返回任务的 result）—— %s"
       % sorted(resp)[:5])
finally:
    bf.fetch_daily = _real_daily

# 任务记录：每个任务各留一条，失败也留
_rows_job = db.jobs(limit=5, kind="daily_universe_sync")
ok(_rows_job and _rows_job[0]["trade_date"] == D and
   _rows_job[0]["status"] == "success",
   "R29 任务留了 job_run 且 trade_date 落列 —— %s" % _rows_job[0]["status"])


def _boom():  # noqa: ANN202
    raise RuntimeError("源挂了")


T.quotes.market_snapshot = _boom
try:
    try:
        T.run_task("daily_market_sync", trade_date=D)
        ok(False, "R30 任务抛异常必须上抛，不许吞成 success")
    except RuntimeError:
        ok(True, "R30 任务抛异常上抛了")
finally:
    T.quotes.market_snapshot = _real_spot
_bad_job = db.jobs(limit=1, kind="daily_market_sync")[0]
# job_run 没有 error 列（失败文本进的是 detail）—— 拿错列名会当场 KeyError，
# 这正是「记了但没人读得出来」最容易漏掉的地方。
ok(_bad_job["status"] == "failed" and "RuntimeError" in (_bad_job["detail"] or ""),
   "R31 接口报错而库里显示成功，不允许 —— %s" % (_bad_job["status"],))

# 任务清单端点：GET，一次外发都不许有
_rep = T.status_report()
ok(len(_rep["tasks"]) == 6 and _rep["offline"] is True,
   "R32 任务清单六条且标零联网（socket 守卫在场，真联网当场红）")
ok(all(t["last"] is not None for t in _rep["tasks"]),
   "R33 每个任务都答得出「上次什么时候跑的、成没成」")
ok(_rep["trade_date"] is None or len(str(_rep["trade_date"])) == 8,
   "R34 清单里的交易日是离线推的 —— %s" % _rep["trade_date"])
_http = R.pipeline_tasks()
ok(_http["offline"] is True and len(_http["tasks"]) == 6,
   "R35 任务清单端点（GET /pipeline/tasks）可用且零联网")
try:
    R.pipeline_tasks_run("__nope__", {})
    ok(False, "R36 未注册任务经端点必须 400")
except Exception as exc:  # noqa: BLE001
    ok(getattr(exc, "status_code", None) == 400,
       "R36 端点把参数错误映射成 400 —— %s" % getattr(exc, "status_code", None))


# ---- 20) 阶段 4：最近一次成功交易日 + 数据质量状态 + 补跑状态不许抹成成功 ---
#
# 任务书阶段 4 要一个**只读出口**：「最近一次成功交易日及数据质量状态」。
# 造的日期用 2099 开头 —— 聚合答的是「最近」，断言写死在普通日期上，会被前面
# 任何测试新插入的一行推翻（那类红最贵：改的不是它，它却红了）。
from app.pipeline import coverage as C                                  # noqa: E402

S_A, S_B = "20990101", "20990102"
for _dom in ("pool_zt", "pool_zb"):
    db.ingest_start(_dom, S_B)
    I.record(_dom, S_B, _rows(3), required=("code",), store=False)      # SUCCESS
db.ingest_start("pool_zt", S_A)
I.record("pool_zt", S_A, _rows(3), required=("code",), store=False)     # SUCCESS
db.ingest_start("pool_zb", S_A)
I.record("pool_zb", S_A, [], store=False)      # 0 行 => PARTIAL（#18 那条闸门）
# 质量检查那半：正常由 `data_quality_check` 回填，这里直写模拟「跑过」
db.ingest_set_coverage("pool_zt", S_B, 1.0,
                       {"window": {"state": "ok", "coverage_rate": 1.0,
                                   "checked_at": "2099-01-02T00:00:00+00:00"}})

_due_before = len(db.ingest_due())
_h = I.health_report()
by_dom = {d["domain"]: d for d in _h["domains"]}
_ls = _h["last_success"] or {}
ok(_h["offline"] is True, "S1 健康出口声明零联网（socket 守卫在场）")
ok(_h["last_success_date"] == S_B,
   "S2 最近一次成功交易日 = 全域 SUCCESS 的那天（PARTIAL 的 %s 不算）—— got %s"
   % (S_A, _h["last_success_date"]))
ok(_ls.get("covered") == 2 and _ls.get("core") == len(C._SNAP_KINDS)
   and _ls.get("complete") is False,
   "S3 那天只跑了 2 个域就如实说不完整（covered/core/complete=%s/%s/%s）"
   % (_ls.get("covered"), _ls.get("core"), _ls.get("complete")))
ok(len(by_dom) == len(C._SNAP_KINDS),
   "S4 核心域一个不少（%d 个，见 coverage._SNAP_KINDS）" % len(by_dom))
ok(by_dom["pool_zt"]["collected"] is True
   and by_dom["pool_zt"]["trade_date"] == S_B
   and by_dom["pool_zt"]["status"] == "SUCCESS"
   and by_dom["pool_zt"]["status_alias"] == "success",
   "S5 每域给**最近**一行 + status_alias —— %r"
   % {k: by_dom["pool_zt"].get(k)
      for k in ("trade_date", "status", "status_alias")})
_w = by_dom["pool_zt"]["window"]
ok(isinstance(_w, dict) and _w.get("state") == "ok" and bool(_w.get("checked_at")),
   "S6 数据质量状态从 coverage_detail.window 读出（没跑过才是 None）—— %r" % _w)
ok(all(d["collected"] or (d["status"] is None and d["status_alias"] is None)
        for d in _h["domains"]),
   "S7 没采过的域是 collected=false，**不发明第六个状态词**")
ok(_h["retry_due"] == _due_before and isinstance(_h["retry_due"], int),
   "S8 到点待补跑条数与 ingest_due 同源（%s）" % _h["retry_due"])
ok("retry_check" in _h, "S9 上次定时补跑检查的记录也给（没跑过是 None）")
ok("/api/pipeline/health" in paths, "S10 健康端点已注册")
_h2 = R.pipeline_health()
ok(_h2["offline"] is True and "last_success_date" in _h2,
   "S11 GET /pipeline/health 可直接调且零联网")

# ---- 任务状态：补跑跑挂了不许记成 success（阶段 4 修的真 bug） ----------
# `_status_of` 原先只认 `ingest` 那套大写，又去数一个 `run_retries` 根本不返回
# 的 `retried` 键 —— 于是**补跑全挂时任务表里永远是 success**。
ok(T._status_of({"status": "failed", "checked": 3, "ran": 3, "results": []})
   == "failed", "S12 run_retries 的小写 failed 认得出")
ok(T._status_of({"status": "partial", "checked": 3, "ran": 3}) == "partial",
   "S13 小写 partial 同样认得出")
ok(T._status_of({"status": "PARTIAL"}) == "partial"
   and T._status_of({"status": "FAILED"}) == "failed",
   "S14 ingest 那侧的大写照旧认（两套词是同一件事的两种说法）")
ok(T._status_of({"finished": False, "requested": 4, "failed": 1}) == "partial",
   "S15 回填那条口径不变：finished=false => partial")
ok(T._status_of({"checked": 0, "ran": 0, "results": []}) == "success",
   "S16 一条都没到点不叫失败（R25 就靠它）")

# 端到端：真造一条到点的行，取数器全部桩成「抛异常」—— 断言落在 job_run 上，
# 因为「接口报错而库里显示成功」正是这条要防的事。
_con = sqlite3.connect(settings.db_path)
_con.execute("UPDATE ingest_job SET next_retry_at=NULL WHERE next_retry_at IS NOT NULL")
_con.execute("DELETE FROM ingest_job WHERE trade_date='20260901'")
_con.commit()
_con.close()
db.ingest_start("pool_zt", "20260901")
I.record_failure("pool_zt", "20260901", ConnectionError("x"))
_con = sqlite3.connect(settings.db_path)
_con.execute("UPDATE ingest_job SET next_retry_at='1990-01-01T00:00:00+00:00' "
             "WHERE domain='pool_zt' AND trade_date='20260901'")
_con.commit()
_con.close()


def _raise(*_a, **_k):  # noqa: ANN202
    raise ConnectionError("源挂了（桩，不许真发包）")


_o_fetch = (scheduler.fetch_pool, scheduler.fetch_sector_flow,
            scheduler._score_and_record)
scheduler.fetch_pool = _raise
scheduler.fetch_sector_flow = _raise
scheduler._score_and_record = _raise
try:
    out = T.run_task("recovery_sync", trade_date=D)
finally:
    scheduler.fetch_pool = _o_fetch[0]
    scheduler.fetch_sector_flow = _o_fetch[1]
    scheduler._score_and_record = _o_fetch[2]
ok(out["status"] == "failed",
   "S17 补跑全失败 => 任务状态 failed（修之前恒 success）—— %s" % out["status"])
_job_last = db.jobs(limit=1, kind="recovery_sync")[0]
ok(_job_last["status"] == "failed",
   "S18 job_run 那条也是 failed —— 接口跑挂了而库里记成功，不允许 —— %s"
   % _job_last["status"])
ok(not _BLOCKED, "S19 全程一个包都没发：%s" % _BLOCKED[:3])


# ---- 21) 收尾 ----------------------------------------------------------------
ok(not _BLOCKED, "Z1 一次外发都不许有：%s" % _BLOCKED[:3])

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("PASS=%d FAIL=%d" % (PASS, len(FAILS)))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
