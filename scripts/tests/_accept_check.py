# -*- coding: utf-8 -*-
r"""阶段 7 验收：任务书里**此前没有断言**的那几条，2026-10-16（40 条），
2026-10-17 阶段 7 收尾扩到 **61 条**（G/H/I 段）。

逐条对过一遍 `_ingest_check` / `_backfill_check` / `_pipeline_check` /
`_research_check`，把各条分成「已覆盖」和「有实现无断言」两堆 —— 这里只收
后者，不重复造已有的轮子：

  8) **SQLite 并发写不产生不可恢复损坏** —— 此前**全空白**（grep 整个
     `scripts\tests\` 只命中 `_card_frame_check` 里的 `import threading`，
     那是飞书卡片，跟库无关）。任务书点名的重点。
 11) **不新增未经授权的网络访问路径** —— 此前无断言。89 个路由里有 **9 个**
     会朝数据源发包，这个全集没有任何东西钉死 —— 谁哪天加一个 `fetch_*`
     路由，现有测试全绿。
  4) **非交易日不算漏采** —— `coverage.missing_dates` / `gaps()` 本来就只从
     交易日历推（`coverage.py:294,453`），**有实现、没断言**。
  6) **单源失败不污染其它域状态** —— `_ingest_check` 的 J/K 系列测到
     `_run_step` / `_collect` 三元组那一层，没测 `ingest_job` **表层**：
     「pool_zt 挂了，sector_flow_industry 那一行有没有被顺带动到」。
  2) **网络中断后可重试恢复** —— `L` 系列测过「到点的会被跑掉」，但**恢复
     闭环**（成功后状态回 SUCCESS、退避清空、退出重试队列）没测过。

**2026-10-17 阶段 7 收尾补的三条**（G/H/I 段，21 条）：

  1) **按钮与自动调度调用相同任务服务** —— 一处定义、三个入口
     （`scheduled` / `manual` / 飞书）全走 `run_post_close`；`_collect`
     的每个调用点 AST 判宿主，只许在 `run_post_close` / `run_light` 体内
     （无旁路 = 没人绕过 job_run 记账与 ingest 状态登记）。
  2) **同一天重复执行不产生重复记录** —— `ingest_try_start` 的 upsert 与
     `save()` 的 PK 覆盖，各来一次「跑两轮只留一行」。
  8) **不完整数据不会生成误导性的正式选股排名** —— 宽松方案（plan.md §7）：
     `ingest.rank_quality()` 只读 `ingest_job`，任一输入域非 SUCCESS 就给
     整榜 `data_quality.state="incomplete"` + 逐域 missing（**绝不补 0**）；
     打分 `run()` 把它加进 `model`；前端榜顶整块 Alert。不拦输出、不动权重。

全离线：开头封死 TCP + DNS。**绝不碰真实 `market.db`** —— 跑批可能正在写它，
所以一开头就把 `settings.db_path` 切到临时库，中途任何一次意外调用都只会落
在临时库上；收尾再断言一次「全程没指回真实库」。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_accept_check.py
"""
import ast
import json
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
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
        raise AssertionError("BLOCKED-NETWORK: %s" % (addr,))
    return _orig(self, addr)


def _gai_guard(host, port, *a, **k):  # noqa: ANN001
    if str(host) not in _LOOP:
        _BLOCKED.append("dns:%s" % (host,))
        raise AssertionError("BLOCKED-DNS: %s:%s" % (host, port))


socket.socket.connect = _guard
socket.getaddrinfo = _gai_guard

BACKEND = Path(__file__).resolve().parents[2] / "backend"
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
sys.path.insert(0, str(BACKEND))

from app.config import settings                                      # noqa: E402

# 真实库（回填跑批可能正在写它）。**先存下来**，等下 Z2 要断言全程没指回去。
_ORIG_DB = settings.db_path
_ROOT = Path(tempfile.mkdtemp(prefix="accept_check"))
settings.db_path = _ROOT / "market.db"      # Path 而不是 str：db._connect() 要 .parent

from app.store import db                                             # noqa: E402
from app.pipeline import coverage as C                               # noqa: E402
from app.pipeline import ingest as I                                 # noqa: E402
from app import scheduler                                            # noqa: E402

PASS = 0
FAILS = []


def ok(cond, label):  # noqa: ANN001
    global PASS
    if cond:
        PASS += 1
    else:
        FAILS.append(label)


# ---- 夹具：与 _pipeline_check 同一套（日期取真实交易日历，分母才是确定的）----
CALENDAR, _STALE = C.cached_trade_dates()
FULL = [d for d in CALENDAR if d < "20261016"][-8:]

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


def _pool(n):
    return [{"code": "6000%02d" % i, "name": "涨停%d" % i,
             "price": 10.0 + i, "pct_chg": 9.98, "amount": 1e8}
            for i in range(n)]


def _mk_cov(name, dates):
    """只建 snapshot + job_run，pool_zt 只给 `dates` 这几天。"""
    root = Path(tempfile.mkdtemp(prefix=name))
    path = root / "market.db"
    con = sqlite3.connect(path)
    con.executescript(SNAP_DDL)
    con.executescript(JOB_DDL)
    results = {"pool_zt": 36, "pool_zb": 5, "pool_dt": 2, "pool_strong": 70,
               "sector_industry": 86, "sector_concept": 300, "main_force": 30}
    detail = {"date": FULL[-1], "results": results, "failed": []}
    con.execute("INSERT INTO job_run (kind,status,started_at,ended_at,detail)"
                " VALUES (?,?,?,?,?)",
                ("manual", "success",
                 "%s-%s-%sT02:00:00+00:00" % (FULL[-1][:4], FULL[-1][4:6],
                                              FULL[-1][6:]),
                 None, json.dumps(detail, ensure_ascii=False)))
    for d in dates:
        payload = _pool(36)
        con.execute("INSERT INTO snapshot VALUES (?,?,?,?,?,?)",
                    ("pool_zt", d, json.dumps(payload, ensure_ascii=False),
                     len(payload), "fixture",
                     "%s-%s-%sT02:00:00+00:00" % (d[:4], d[4:6], d[6:])))
    con.commit()
    con.close()
    return path


def _conc_db(name):
    """独立临时库 + 生产同款 PRAGMA。

    并发测试**不能**走 `db._connect()` —— 那是全局单连接单例，两个连接都拿不到
    就谈不上并发；而 SQLite 的并发语义又跟 journal 模式强相关（WAL 允许读写
    并存、delete 是写锁独占），用错模式的「没事」不作数。
    """
    root = Path(tempfile.mkdtemp(prefix="acc_%s_" % name))
    p = root / "c.db"
    c = sqlite3.connect(p, isolation_level=None, timeout=5)
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA journal_mode=DELETE")
    c.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    c.close()
    return p


def _conn(p, busy=5000):
    # Python 的 `timeout` 与 PRAGMA busy_timeout 指同一个 busy handler，
    # 后设置的生效 —— 两个都给成同一份，免得 A5「只肯等 200ms」实际等了 1s。
    c = sqlite3.connect(p, isolation_level=None,
                        timeout=max(0.05, busy / 1000.0))
    c.execute("PRAGMA busy_timeout=%d" % busy)
    return c


def _integrity(p):
    c = sqlite3.connect(str(p), isolation_level=None, timeout=5)
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        c.close()


# ==== A) SQLite 并发写不产生不可恢复损坏（任务书第 8 条）======================
# A1 测试必须与生产同参数，否则下面几条的「并发下没事」根本不作数。
_c = db._connect()
_ok_busy = _c.execute("PRAGMA busy_timeout").fetchone()[0]
_jm = str(_c.execute("PRAGMA journal_mode").fetchone()[0]).lower()
_c.close()
db._conn = None        # 上面关掉的是单例连接，不置空的话 D 组一上来就
                       # 「Cannot operate on a closed database」
ok(_ok_busy == 5000, "A1 生产 busy_timeout=5000（测试同参，got %r）" % _ok_busy)
ok(_jm != "wal", "A1b 生产没开 WAL（journal_mode=%s，delete 才与测试同语义）" % _jm)

# A2 同主键竞争：两个连接同时 INSERT 同一行。生产 `bars_upsert` 走
#    `ON CONFLICT DO NOTHING`，所以两边都该「成功」，最终只留一行。
p2 = _conc_db("samekey")
_bar = threading.Barrier(2, timeout=10)
_r2 = []


def _w_same(v):  # noqa: ANN001
    con = _conn(p2)
    try:
        _bar.wait()
        con.execute("INSERT INTO t(k,v) VALUES('dup',?)"
                    " ON CONFLICT DO NOTHING", (v,))
        _r2.append("ok")
    except Exception as exc:  # noqa: BLE001  并发下抛了什么要原样带出来
        _r2.append("%s:%s" % (type(exc).__name__, exc))
    finally:
        con.close()


_ts = [threading.Thread(target=_w_same, args=(v,)) for v in (1, 2)]
[_t.start() for _t in _ts]
[_t.join(timeout=20) for _t in _ts]
_r2_rows = sqlite3.connect(str(p2)).execute("SELECT COUNT(*) FROM t").fetchone()[0]
ok(len(_r2) == 2 and all(x == "ok" for x in _r2),
   "A2 同键并发两边都成功（ON CONFLICT DO NOTHING），没抛 locked —— %r" % _r2)
ok(_r2_rows == 1, "A2b 同键只留一行 —— got %d" % _r2_rows)
ok(_integrity(p2) == "ok", "A2c 同键并发后 integrity_check=ok")

# A3 不同行并发：两行都该在，谁都别丢。
p3 = _conc_db("diffkey")
_bar3 = threading.Barrier(2, timeout=10)
_r3 = []


def _w_diff(k):  # noqa: ANN001
    con = _conn(p3)
    try:
        _bar3.wait()
        con.execute("INSERT INTO t(k,v) VALUES(?,1)", (k,))
        _r3.append("ok")
    except Exception as exc:  # noqa: BLE001
        _r3.append("%s:%s" % (type(exc).__name__, exc))
    finally:
        con.close()


_ts3 = [threading.Thread(target=_w_diff, args=(k,)) for k in ("a", "b")]
[_t.start() for _t in _ts3]
[_t.join(timeout=20) for _t in _ts3]
_c3 = sqlite3.connect(str(p3))
_r3_rows = _c3.execute("SELECT COUNT(*) FROM t").fetchone()[0]
_c3.close()
ok(all(x == "ok" for x in _r3) and _r3_rows == 2,
   "A3 不同行并发两行都在 —— rows=%d res=%r" % (_r3_rows, _r3))
ok(_integrity(p3) == "ok", "A3b 不同行并发后 integrity_check=ok")

# A4 持写锁时，busy_timeout 要**真的等到**（既不立刻失败，也不傻等到超时）。
p4 = _conc_db("wait")
_held = threading.Event()
_el4 = []
_err4 = []


def _holder():
    con = _conn(p4)
    try:
        con.execute("BEGIN IMMEDIATE")          # 独占写锁
        _held.set()
        time.sleep(1.2)
        con.execute("INSERT INTO t(k,v) VALUES('holder',0)")
        con.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        _held.set()
        _err4.append("holder:%s" % exc)
    finally:
        con.close()


def _waiter():
    _held.wait(timeout=10)
    con = _conn(p4)
    t0 = time.time()
    try:
        con.execute("INSERT INTO t(k,v) VALUES('waiter',1)")
        _el4.append(time.time() - t0)
        _err4.append(None)
    except Exception as exc:  # noqa: BLE001
        _el4.append(time.time() - t0)
        _err4.append("%s:%s" % (type(exc).__name__, exc))
    finally:
        con.close()


_th = [threading.Thread(target=_holder), threading.Thread(target=_waiter)]
[_t.start() for _t in _th]
[_t.join(timeout=30) for _t in _th]
ok(not _err4 or _err4 == [None],
   "A4 等锁的一方等到成功，没抛 locked —— %r" % _err4)
ok(_el4 and 0.7 <= _el4[0] < 5.0,
   "A4b 真等了 ~1.2s 且没等到超时 —— elapsed=%r" % (_el4,))
ok(_integrity(p4) == "ok", "A4c 等锁写入后 integrity_check=ok")

# A5 但**不肯等的时候必须明确报错**，而不是挂死 —— 或者说到：要么等到，要么
#    干脆地失败，第三种「无限挂着」是最坏的。
p5 = _conc_db("timeout")
_held5 = threading.Event()
_t5 = []
_e5 = []


def _holder5():
    con = _conn(p5)
    try:
        con.execute("BEGIN IMMEDIATE")
        _held5.set()
        time.sleep(1.5)
        con.execute("COMMIT")
    except Exception as exc:  # noqa: BLE001
        _held5.set()
        _e5.append("holder:%s" % exc)
    finally:
        con.close()


def _waiter5():
    _held5.wait(timeout=10)
    con = _conn(p5, busy=200)                  # 只肯等 200ms，锁要占 1.5s
    t0 = time.time()
    try:
        con.execute("INSERT INTO t(k,v) VALUES('impatient',1)")
        _t5.append(time.time() - t0)
        _e5.append("NO-ERROR")                 # 不该走到这
    except Exception as exc:  # noqa: BLE001
        _t5.append(time.time() - t0)
        _e5.append("%s:%s" % (type(exc).__name__, exc))
    finally:
        con.close()


_th5 = [threading.Thread(target=_holder5), threading.Thread(target=_waiter5)]
[_t.start() for _t in _th5]
[_t.join(timeout=30) for _t in _th5]
ok(_e5 and "locked" in _e5[0].lower(),
   "A5 超时必须明确抛 database is locked —— %r" % (_e5,))
ok(_t5 and _t5[0] < 1.4,
   "A5b 没傻等到锁释放（说明 busy_timeout 真生效）—— elapsed=%r" % (_t5,))
ok(_integrity(p5) == "ok", "A5c 超时失败后库没坏 —— integrity=%s" % _integrity(p5))
try:
    _again = _conn(p5)
    _again.execute("INSERT INTO t(k,v) VALUES('after-timeout',1)")
    _again.close()
    _again_ok = True
except Exception as exc:  # noqa: BLE001
    _again_ok = False
    _e5.append("after:%s" % exc)
ok(_again_ok, "A5d 事后还能正常写（没留下不可恢复的锁）—— %r" % (_e5[-1:],))

# A6 **写一半被 kill**：这正是今天回填跑批被我 `Stop-Process` 掐掉的场景。
#    已提交的必须在、未提交的必须不在、库里不许有不可恢复损坏。
p6 = _conc_db("kill")
_CHILD = r"""
import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=5)
c.execute("PRAGMA busy_timeout=5000")
c.execute("BEGIN IMMEDIATE")
c.execute("INSERT INTO t(k,v) VALUES('committed',1)")
c.execute("COMMIT")
c.execute("BEGIN IMMEDIATE")
c.execute("INSERT INTO t(k,v) VALUES('torn',2)")
os._exit(1)                      # 不 commit、不 close —— 等价于被 kill
"""
_p6 = subprocess.run([sys.executable, "-c", _CHILD, str(p6)],
                     capture_output=True, text=True, timeout=60)
ok(_p6.returncode == 1, "A6 子进程确实异常终止（returncode=%r）" % _p6.returncode)
_c6 = sqlite3.connect(str(p6), isolation_level=None, timeout=5)
_r6 = dict(_c6.execute("SELECT k, v FROM t").fetchall())
try:
    _c6.execute("INSERT INTO t(k,v) VALUES('after-kill',3)")
    _post_ok = True
except Exception as exc:  # noqa: BLE001
    _post_ok = False
    _r6["after-kill"] = str(exc)
_c6.close()
ok(_integrity(p6) == "ok", "A6b kill 后 integrity_check=ok")
ok(_r6.get("committed") == 1, "A6c 已提交的那批必须在 —— %r" % (_r6,))
ok("torn" not in _r6, "A6d 未提交的那批必须**不在**（事务原子）—— %r" % (_r6,))
ok(_post_ok, "A6e kill 后仍能继续写 —— %r" % (_r6.get("after-kill"),))
ok(not Path(str(p6) + "-journal").exists(),
   "A6f hot journal 已回滚清理，没留残件")


# ==== B) 不新增未经授权的网络访问路径（任务书第 11 条）=======================
# 判据：**精确到被调用的属性名**。不能按模块名一刀切 ——
# `backfill.plan` / `backfill.load_scope` 是读本地库（零联网），而
# `backfill.backfill` 是朝数据源发包；两者同一个模块名，语义完全相反。
_ROUTES = BACKEND / "app" / "api" / "routes.py"
_SRC = _ROUTES.read_text(encoding="utf-8")

_FETCH_RE = re.compile(r"^(fetch_|get_stock|daily_history|net_inflow)")
_NET_ATTR = {"backfill", "with_retry", "run_retries", "run_post_close",
             "run_light", "_collect", "_score_and_record", "_cron_fire",
             # 阶段 2 把两条旁路收编进任务服务之后才被扫出来的：`fetch_`
             # 前缀命中判据，`score_and_capture` 则是显式列进来的（它的名字
             # 不带 fetch，漏掉它等于「按钮抓数不吭声」）。
             "fetch_sector_flow", "score_and_capture",
             # 阶段 3：六个任务的唯一入口。它**可能**联网（六个里四个会），
             # 所以按「会」算 —— 漏掉它等于任务服务可以朝任意数据源发包而不进
             # 白名单，比收编前那两条旁路的口子更大。
             "run_task",
             # 前5跟踪（2026-11-11）：`top5track.pull` 是盘后那一步的联网取价。
             # 端点只准调零联网的 `top5track.roster` —— 先把 pull 这个属性名
             # 进判据，哪天有人在路由里调它，B1 的「全集 == 白名单」当场红。
             "pull",
             # 历史补录（2026-11-11）：`replenish.run_replenish` 是四个补录 job
             # 的触发口（日线/资金流/池子/打分，全部联网）。
             "run_replenish"}
_SAFE_ATTR = {"plan", "load_scope", "coverage_report", "gaps",
              "BACKFILLABLE", "REFUSED", "items"}
_NET_ROOT = {"requests", "akshare", "netfix", "httpx", "urllib"}

# 数据源联网入口的**全集**。加一个联网路由就必须同步改这行 —— 这条测试的
# 意思就是「加了不吭声是不行的」。
#
# 2026-10-17 由 6 条变 8 条，两条都是**收紧**不是放水：
#   /sector-fund-flow 原先在路由里直连 `sector.get_sector_flow` —— 判据认不出
#     这个属性名，于是一直没进白名单，可它天天在朝数据源发包。收编成
#     `scheduler.fetch_sector_flow` 之后判据才命中。
#   /main-force/refresh 原先直连 `mainforce.run`（同样是联网的），同样漏在
#     白名单外；现在走 `scheduler.score_and_capture`。
# 同日再加第 9 条：`/pipeline/tasks/{task_id}` 是阶段 3 六个任务的唯一触发口，
#   其中四个会联网。GET `/pipeline/tasks` **不在**这里 —— 它只读 job_run。
# 2026-11-11 加第 10 条：`/replenish/start` 是「历史补录」页四个补录 job 的
#   触发口（日线全历史 / 资金流全历史 / 池子补采 / 历史打分），全部联网且
#   是几百到上万次请求量级；`/replenish/status`·`/replenish/stop` 不在
#   这里 —— 前者只读内存状态，后者只置停止标记。
DATA_SRC_NET = {
    "/jobs/run",                          # 盘后任务（含采集）
    "/limit",                             # 实时涨停池
    "/main-force/refresh",                # 重算主力建仓打分（联网抓资金流）
    "/sector-fund-flow",                  # 板块资金流（refresh 或无缓存时抓）
    "/stock/{code}/daily",                # 日线（内部写死 adjust="qfq"）
    "/refresh",                           # 轻量刷新
    "/pipeline/collect/retry",            # 补跑重试
    "/pipeline/backfill",                 # 历史回填
    "/pipeline/tasks/{task_id}",          # 六个每日任务的统一触发口（阶段 3）
    "/replenish/start",                   # 历史补录四个 job 的触发口（2026-11-11）
}


def _chain(node):  # noqa: ANN001
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_net(chain_name):
    last = chain_name.split(".")[-1]
    if last in _SAFE_ATTR:               # 先放行：零联网的读端点判据最要紧
        return False
    if _FETCH_RE.match(last) or last in _NET_ATTR:
        return True
    return chain_name.split(".")[0] in _NET_ROOT


def _scan():
    """返回 (联网路径, 全部路径, GET /pipeline/* 路径)。

    方法要留着 —— B3 断的是「读端点不许联网」，丢了方法就只剩路径、
    过滤出来恒为空集，那条断言就变成了「永远为真」的摆设。
    """
    tree = ast.parse(_SRC)
    net, allp, get_pipe = set(), set(), set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec.func, ast.Attribute):
                continue
            m = _chain(dec.func)
            if not m.endswith(("router.get", "router.post",
                               "router.put", "router.delete")):
                continue
            path = dec.args[0].value if (dec.args and
                                         isinstance(dec.args[0], ast.Constant)) else "?"
            allp.add(path)
            if m.endswith("router.get") and path.startswith("/pipeline/"):
                get_pipe.add(path)
            if any(_is_net(_chain(n.func)) for n in ast.walk(node)
                   if isinstance(n, ast.Call)):
                net.add(path)
    return net, allp, get_pipe


_NET, _ALL, _GET_PIPE = _scan()
ok(_NET == DATA_SRC_NET,
   "B1 数据源联网路由全集 == 白名单 —— 多%s 少%s"
   % (sorted(_NET - DATA_SRC_NET), sorted(DATA_SRC_NET - _NET)))
_zero = {"/pipeline/bars", "/pipeline/backfill/plan",
         "/pipeline/backfill/domains",
         # 阶段 4 的只读出口：最近一次成功交易日 + 质量状态（B3 已经罩着所有
         # GET /pipeline/*，这里显式点名是为了让它哪天混进联网调用时红得更早）
         "/pipeline/health"}
ok(not (_zero & _NET), "B2 零联网端点没混进联网集合（阶段 4/6） —— %r"
   % sorted(_zero & _NET))
ok(not (_GET_PIPE & _NET),
   "B3 所有 GET /pipeline/* 都零联网 —— %r" % sorted(_GET_PIPE & _NET))
ok(not re.search(r"^\s*import\s+(requests|akshare|httpx)\b", _SRC, re.M)
   and not re.search(r"^\s*from\s+(requests|akshare|httpx)\b", _SRC, re.M),
   "B4 路由文件不许直接 import 数据源库（防止绕开上面的调用扫描发包）")
ok(_ALL >= DATA_SRC_NET, "B5 白名单里的路径都真实注册着（没写成过期条目）")


# ==== C) 非交易日不算漏采（任务书第 4 条）====================================
# `missing_dates` 与 `gaps()` 都只从交易日历推 —— 实现早就对了，但没有任何
# 断言盯着：哪天有人改成「比对日历和日历天数」，周末就会被算成漏采。
# 缺口必须挖在**中间**：`missing_dates` 只算 [first_date, last_date] 窗口内
# （coverage.py:294）—— 砍掉尾部等于窗口一起缩，剩下的全在，测了个空。
# FULL 是连续 8 个交易日，中间隔了个周末，正好让「周末会不会被算成漏采」
# 变成一条能被抓住的断言：如果哪天改成按自然日遍历，C3 当场就红。
_pc = _mk_cov("nontrade", FULL[:3] + FULL[4:])   # 故意挖掉 FULL[3]
_rep = C.coverage_report(str(_pc))
_md = sorted({d for facts in _rep["datasets"].values()
              for d in (facts.get("missing_dates") or [])})
_cal = set(CALENDAR)
ok(_md and set(_md) <= _cal,
   "C1 缺口日期全部在交易日历里 —— 越界 %s" % sorted(set(_md) - _cal)[:5])
ok(_md == [FULL[3]],
   "C2 只报真缺的那一天（没数据的域早退不报、窗口外的不算）—— %s" % _md)
ok(not [d for d in _md
        if datetime.strptime(d, "%Y%m%d").weekday() >= 5],
   "C3 缺口里一个周末都不许有 —— %s"
   % [d for d in _md if datetime.strptime(d, "%Y%m%d").weekday() >= 5])
_g = C.gaps(db_path=str(_pc), lookback=30)
_ok_g = all(d in _cal for ds in _g for d in _g[ds])
ok(_ok_g, "C4 gaps() 速查口径同样只看交易日 —— %r"
   % {ds: [d for d in v if d not in _cal] for ds, v in _g.items() if v})


# ==== D) 单源失败不污染其它域状态（任务书第 6 条，表层）======================
def _row(domain, trade_date):
    c = sqlite3.connect(str(settings.db_path), isolation_level=None, timeout=5)
    try:
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT * FROM ingest_job WHERE domain=? AND trade_date=?",
                      (domain, trade_date)).fetchone()
        return dict(r) if r else None
    finally:
        c.close()


D = "20261014"
db._conn = None
db.ingest_start("pool_zt", D)
db.ingest_start("sector_flow_industry", D)
_before_zt = _row("pool_zt", D)
_before_other = _row("sector_flow_industry", D)
I.record_failure("pool_zt", D, ConnectionError("源挂了"))
_after_zt = _row("pool_zt", D)
_after_other = _row("sector_flow_industry", D)
ok(_after_zt["status"] == I.WAITING,
   "D1 失败后落到 WAITING（等重试），不是停在 RUNNING —— got %r"
   % _after_zt["status"])
# attempts 由 `ingest_start` 负责 +1，`record_failure` 只读不写
# （ingest.py:184）—— 两边各加一次会翻倍，退避也就每次都算错。
ok(_after_zt["attempts"] == _before_zt["attempts"],
   "D1b attempts 不重复计数 —— %s -> %s"
   % (_before_zt["attempts"], _after_zt["attempts"]))
ok(bool(_after_zt["next_retry_at"]),
   "D2 失败域设了退避时刻（否则源挂着会被连着打）")
# 「没被顺带动到」要逐字段比，只比 status 会漏掉 attempts / next_retry_at
# 被整表 UPDATE 误伤的情况。
_diff = {}
for _k in set(_before_other or {}) | set(_after_other or {}):
    _a = (_before_other or {}).get(_k)
    _b = (_after_other or {}).get(_k)
    if _a != _b:
        _diff[_k] = (_a, _b)
ok(_after_other is not None and not _diff,
   "D3 另一个域**逐字段没变** —— 变了 %r" % _diff)

# D 这行带着退避时刻；跑到 E 组时若恰好过了退避窗口，它就"到点"了，会被
# `run_retries` 跟 E 组那条一起跑掉，把 `ran == 1` 搅黄。D 的断言已经跑完，
# 这里直接把它清出重试队列 —— 不改被测代码，只改自己造出来的测试数据。
_con_d = sqlite3.connect(str(settings.db_path), isolation_level=None, timeout=5)
_con_d.execute("UPDATE ingest_job SET next_retry_at=NULL "
               "WHERE domain=? AND trade_date=?", ("pool_zt", D))
_con_d.commit()
_con_d.close()


# ==== E) 网络中断后可重试恢复（任务书第 2 条，闭环）==========================
# 只桩掉**最内层取数器**：`_collect_domain` 的状态登记照常跑，所以这条测的
# 是「真实恢复链路通不通」，而不是「桩自己把状态改绿」。桩改回去了才叫测过。
D2 = FULL[-1]
_orig_getter = scheduler._POOL_GETTERS["zt"]


def _due_now(domain, trade_date):
    """只把**指定这一行**推到点。整表 UPDATE 会把别的测试留下的行一起推到点，
    `run_retries` 就会真去调取数器 —— 在内网里那就是一次真实外发。"""
    con = sqlite3.connect(str(settings.db_path), isolation_level=None, timeout=5)
    con.execute("UPDATE ingest_job SET next_retry_at='1990-01-01T00:00:00+00:00'"
                " WHERE domain=? AND trade_date=?", (domain, trade_date))
    con.commit()
    con.close()


def _snap_count(dataset, trade_date):
    c = sqlite3.connect(str(settings.db_path), isolation_level=None, timeout=5)
    try:
        return c.execute("SELECT COUNT(*) FROM snapshot "
                         "WHERE dataset=? AND trade_date=?",
                         (dataset, trade_date)).fetchone()[0]
    finally:
        c.close()


scheduler._POOL_GETTERS["zt"] = lambda td: [
    {"code": "600001", "name": "桩", "price": 1.0, "pct_chg": 9.9}]
try:
    db.ingest_start("pool_zt", D2)                 # attempts -> 1
    I.record_failure("pool_zt", D2, ConnectionError("断网了"))   # -> 2 + 退避
    _due_now("pool_zt", D2)
    _due_before = [r for r in db.ingest_due() if r["domain"] == "pool_zt"]
    out = scheduler.run_retries()
    _row2 = _row("pool_zt", D2)
    ok(out["ran"] == 1 and _due_before,
       "E1 到点的那条被重跑 —— ran=%r due=%d" % (out["ran"], len(_due_before)))
    ok(_row2["status"] == I.SUCCESS,
       "E2 成功后状态恢复 SUCCESS —— got %r" % _row2["status"])
    ok(not _row2["next_retry_at"],
       "E3 退避被清空（否则成功了也会被无限重跑）—— got %r"
       % _row2["next_retry_at"])
    ok(not [r for r in db.ingest_due() if r["domain"] == "pool_zt"],
       "E4 成功后退出重试队列（下次 ingest_due 不再回它）")
    ok(_snap_count("pool_zt", D2) >= 1,
       "E5 数据真的落库了（不是只把状态改绿）")
finally:
    scheduler._POOL_GETTERS["zt"] = _orig_getter


# ==== G) 不完整数据不生成误导性排名（任务书第 8 条，宽松方案）================
# 打分自己**实时取数**、不读快照 —— 所以「榜不完整」说的是**当天采集域**缺了
# 什么：缺了涨停池还照常发 Top30，读者会把它当正式选股结果。方案已在
# plan.md §7 拍板：**照出 + 整榜标记**，不拦输出、不动任何权重。
G = FULL[0]


def _mk_ingest(domain, trade_date, status, rate=None):
    db.ingest_start(domain, trade_date)
    db.ingest_finish(domain, trade_date, status, processed=1, stored=1,
                     coverage_rate=rate)


_mk_ingest("pool_zt", G, I.FAILED)
_q = I.rank_quality(G)
ok(_q["state"] == "incomplete",
   "G1 有域非 SUCCESS -> 整榜 incomplete —— got %r" % _q["state"])
ok({m["domain"] for m in _q["missing"]} == {"pool_zt"},
   "G2 missing 只列非 SUCCESS 的域 —— %s"
   % sorted(m["domain"] for m in _q["missing"]))
ok(_q["missing"] and _q["missing"][0]["status"] == I.FAILED
   and _q["missing"][0]["trade_date"] == G,
   "G3 missing 逐项给得出谁、哪天、什么状态 —— %r" % _q["missing"][:1])

# PARTIAL 进 missing（PARTIAL 不是成功），SUCCESS 不进
_mk_ingest("pool_zt", G, I.SUCCESS)
_mk_ingest("sector_flow_industry", G, I.PARTIAL)
_q2 = I.rank_quality(G)
ok({m["domain"] for m in _q2["missing"]} == {"sector_flow_industry"},
   "G4 PARTIAL 进 missing、SUCCESS 不进 —— %s"
   % sorted(m["domain"] for m in _q2["missing"]))
ok(_q2["missing"] and _q2["missing"][0]["coverage_rate"] is None,
   "G5 没算过覆盖率就是 None，**绝不补 0** —— %r"
   % (_q2["missing"][0]["coverage_rate"],))
_mk_ingest("stock_net_inflow", G, I.SUCCESS, rate=0.23)
_q3 = I.rank_quality(G, domains=("stock_net_inflow",))
ok(_q3["state"] == "ok" and not _q3["missing"],
   "G6 全 SUCCESS 的子集单独查是 ok —— %r" % _q3)

# 那天**没记录**的域不算失败（与 health_report 同口径：没采过 ≠ 采失败）
_q4 = I.rank_quality(G, domains=("pool_zt", "pool_zb"))
ok(_q4["state"] == "ok",
   "G7 没记录的域不进 missing —— %r" % _q4["missing"])
_mk_ingest("pool_zt", D2, I.FAILED)
_q5 = I.rank_quality(D2, domains=("pool_zt",))
ok(_q5["state"] == "incomplete" and
   [m["trade_date"] for m in _q5["missing"]] == [D2],
   "G8 只看当天 —— 别的交易日的失败既不串进来也不漏掉")

_MF = (BACKEND / "app" / "metrics" / "mainforce.py").read_text(encoding="utf-8")
ok("meta[\"data_quality\"] = pipeline_ingest.rank_quality(trade_date)" in _MF,
   "G9 打分 run() 收尾接了体检")
ok('"model": meta' in _MF,
   "G10 体检加进 model（既有键一个不动，replay_score 重放不受影响）")
ok(_MF.count("data_quality") == 1,
   "G11 只在收尾出现一次，不碰 params/weights —— %d 次"
   % _MF.count("data_quality"))
_MFTS = (FRONTEND / "src" / "pages" / "MainForce.tsx").read_text(
    encoding="utf-8")
ok("本榜基于不完整数据" in _MFTS and "model?.data_quality" in _MFTS,
   "G12 前端榜顶整块 Alert（不在每行重复打标）")
ok("data_quality?" in (FRONTEND / "src" / "lib" / "api.ts").read_text(
    encoding="utf-8"),
   "G13 类型里是可选字段（老快照没这键，不许炸也不许假装 ok）")


# ==== H) 按钮与自动调度调用同一个任务服务（任务书第 1 条）====================
_SCH = (BACKEND / "app" / "scheduler.py").read_text(encoding="utf-8")
_RT = (BACKEND / "app" / "api" / "routes.py").read_text(encoding="utf-8")
ok(_SCH.count("def run_post_close") == 1,
   "H1 任务服务只有一处定义（两处实现迟早分叉）")
ok('run_post_close(kind="scheduled")' in _SCH,
   "H2 定时触发点调它")
ok("scheduler.run_post_close(" in _RT and 'kind="manual"' in _RT,
   "H3 「立即运行」按钮（POST /jobs/run）调的是**同一个**函数")
# 旁路防线：`_collect` 是采集本体，**只许**在两个任务服务函数体内被调 ——
# 谁在外面直接调它，就绕过了 job_run 记账与 ingest 状态登记（阶段 2 收编掉的
# 就是这种旁路）。AST 判宿主，不数字符串次数（注释里出现同名会误报）。
_tree = ast.parse(_SCH)
_fn_calls = {}
for _fn in [n for n in ast.walk(_tree) if isinstance(n, ast.FunctionDef)]:
    _hits = [c.lineno for c in ast.walk(_fn)
             if isinstance(c, ast.Call)
             and getattr(c.func, "id", "") == "_collect"]
    if _hits:
        _fn_calls[_fn.name] = _hits
ok(set(_fn_calls) <= {"run_post_close", "run_light"} and sum(
    len(v) for v in _fn_calls.values()) >= 2,
   "H4 _collect 只在任务服务函数体内被调（无旁路）—— %r" % _fn_calls)
_FU = (BACKEND / "app" / "feishu.py").read_text(encoding="utf-8")
ok("scheduler.run_post_close(" in _FU,
   "H5 飞书指令入口也是同一个函数")


# ==== I) 同一天重复执行不产生重复记录（任务书第 2 条）========================
I2 = FULL[1]                              # 换个干净的日子，别跟 D/E/G 混
db.ingest_try_start("pool_zt", I2)
db.ingest_finish("pool_zt", I2, I.SUCCESS, processed=36, stored=36)
db.ingest_try_start("pool_zt", I2)        # 同一天再来一轮
db.ingest_finish("pool_zt", I2, I.SUCCESS, processed=36, stored=36)
_rows_i = [r for r in db.ingest_list("pool_zt", 50) if r["trade_date"] == I2]
ok(len(_rows_i) == 1,
   "I1 同域同天只留一行（upsert 不是 insert）—— %d 行" % len(_rows_i))
ok(bool(_rows_i) and _rows_i[0]["attempts"] == 2,
   "I2 重跑记在 attempts 上，不是新开一行 —— %r"
   % (_rows_i[0]["attempts"] if _rows_i else None))
db.save_one("main_force", I2, {"rows": [1]}, source="t")
db.save_one("main_force", I2, {"rows": [1, 2]}, source="t")   # 覆盖同一行
ok(_snap_count("main_force", I2) == 1,
   "I3 快照同 (dataset, date) 也是覆盖不新增（PK 兜底）")


# ==== 收尾 ====================================================================
ok(Path(settings.db_path) != Path(_ORIG_DB),
   "Z2 全程没把 db_path 指回真实 market.db —— %s" % (settings.db_path,))
ok(not _BLOCKED, "Z1 一次外发都不许有 —— %s" % (_BLOCKED[:5],))

if FAILS:
    for f in FAILS:
        print("FAIL %s" % f)
    print("FAILED %d (passed %d)" % (len(FAILS), PASS))
    sys.exit(1)
print("ALL %d ASSERTIONS PASSED" % PASS)
