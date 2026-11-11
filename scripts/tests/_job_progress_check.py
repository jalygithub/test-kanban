# -*- coding: utf-8 -*-
r"""总览页任务进度（进度条）的**离线**回归，2026-10-12。

覆盖本轮新增的后端链路，每一条都对应一个当初会露馅的点：

  1) `/api/jobs/status` 在**空闲态**就给出 `plan` —— 进度条的分母。
     没有它前端只能算 0%，点「立即执行」后头几秒看着像没反应。
  2) 运行中 `current_step` / `steps` **逐步**可见。原先 `steps` 要等到
     `_finish` 才落，页面上「已完成 N 步」整个任务期间都停在 0。
  3) `state()` 交出去的 `steps` 是**拷贝**：运行中每完成一步就写内部 dict，
     而 `/jobs/status` 每 3 秒被另一个线程读 —— 直接交内部引用会在 JSON
     序列化时撞 `dictionary changed size during iteration`。
  4) `current_step == main_force` 时才透出 `sub`（打分内部进度），否则
     `mainforce.progress()` 的残留值会漏到页面上。
  5) `run_light` 走 `live=False`，**不许**碰 `_state` —— 它和完整流水线
     可以并存，写了就会把正在跑的那轮进度冲掉。
  6) 失败的那一步也必须落进 `steps`（写在 `finally`），否则进度条会永远
     停在失败那步上不动。
  7) `mainforce._stage` / `_count` -> `progress()` 往返。
  8) **接线**：`run_post_close` 调 `_collect` 必须传 `live=True`。
     2026-10-15 真机复现 —— 任务在跑、日志在逐只取日线，页面却恒显
     「已完成 0/7 步」且没有 `sub`。根因就是漏传这一个参数，而上面 3/4/6
     节全都是**直接调 `_collect(..., live=True)`** 测的：组件各自好用，
     接线断了照样全绿。反向同理，`run_light` 必须留在 `live=False`。

   9) **补跑检查触发点只查不跑**（阶段 4）：17:30 / 次日 09:20 两个 cron 点
      是「检查」不是「执行」—— AGENTS.md 的硬口径「任何定时器都不碰会联网的
      补跑入口」2026-10-17 用户拍板保留，与任务书原文「后续时间段**检查**重试」
      取交集。断言分三层：源码里不许出现 `run_retries` / `is_trade_day`、真调
      一次时把它桩成「一调就炸」、结果落 kv（`retry_check`）且 `ran` 恒 False。
      顺带钉住「开了就跑」：开关一开两个检查点跟着装上，两个都关一个 job 都没有。

全离线：开头封死 TCP + DNS，取数函数一律桩掉，一个包都不发。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_job_progress_check.py
"""
import ast
import socket
import sqlite3
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

# 路由测试会碰 job_run 表 —— 落在真 market.db 上就是污染。
_SCRATCH = Path(tempfile.mkdtemp(prefix="kanban_job_"))
settings.db_path = _SCRATCH / "market.db"

from app import scheduler as sch                     # noqa: E402
from app.api.routes import router                    # noqa: E402
from app.metrics import mainforce                    # noqa: E402
from app.pipeline import ingest as ing               # noqa: E402
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


PLAN9 = ["pool_zt", "pool_zb", "pool_dt", "pool_strong",
         "sector_industry", "sector_concept",
         # 日线采集（2026-11-11）：sector 之后、打分之前
         "daily_bar", "main_force", "top5_track"]

# ============================ 1) plan（进度条的分母） ========================
ok(sch._plan(True) == PLAN9,
   "_plan(scoring=True) 就是 _collect 实际会跑的 9 步：%s" % sch._plan(True))
ok(sch._plan(False) == PLAN9[:6],
   "_plan(scoring=False) 少最后三步（run_light 不打分、不取跟踪价、也不落日线）")
ok(len(sch._plan(True)) == 9, "分母是 9，不是写死在前端的数")

# =========================== 2) 空闲态就能拿到 plan ==========================
app = FastAPI()
app.include_router(router)
c = TestClient(app)
r = c.get("/api/jobs/status")
ok(r.status_code == 200, "/jobs/status -> 200（got %d）" % r.status_code)
cur = r.json()["current"]
ok("plan" in cur and cur["plan"] == [], "空闲态 plan 是空数组（got %r）"
   % (cur.get("plan"),))
ok(cur.get("current_step") == "", "空闲态 current_step 为空（got %r）"
   % (cur.get("current_step"),))
ok("sub" not in cur, "空闲态不给 sub（打分残留值不许漏出来）")

# ==================== 3) 运行中 current_step / steps 逐步可见 ===============
# 用桩顶掉取数：既不联网，又能从桩里反查 state() 当时看到什么
_seen_current = []


def _spy_pool(kind, target):  # noqa: ANN001
    _seen_current.append(sch.state()["current_step"])
    return 7


def _boom(kind):  # noqa: ANN001
    raise RuntimeError("simulated fetch failure")


sch.fetch_pool = _spy_pool
sch.fetch_sector_flow = lambda kind: 5
with sch._lock:
    sch._state.update(steps={}, current_step="", plan=sch._plan(False),
                      running=True, kind="manual", date="20261012")

res, failed, partial = sch._collect("20261012", False, False, live=True)
ok(_seen_current and _seen_current[0] == "pool_zt",
   "第一步执行**期间** current_step 已是 pool_zt（got %r）" % _seen_current[:1])
ok(set(res) == set(sch._state["steps"]),
   "每步跑完立刻落进 _state（res=%d, state=%d）"
   % (len(res), len(sch._state["steps"])))
ok(sch._state["current_step"] == "",
   "全部跑完 current_step 清空（got %r）" % sch._state["current_step"])

# ---- 失败步也必须落进 steps（写在 finally），否则进度条会卡在失败步上
sch.fetch_sector_flow = _boom
with sch._lock:
    sch._state.update(steps={}, current_step="")
res, failed, partial = sch._collect("20261012", False, False, live=True)
ok(not partial,
   "stub 的取数器不写逐域状态，闸门查无此行 => partial 为空（got %r）" % partial)
ok("sector_industry" in sch._state["steps"],
   "失败的那步也进了 steps（keys=%s）" % sorted(sch._state["steps"]))
ok(str(sch._state["steps"].get("sector_industry", "")).startswith("失败:"),
   "失败值是说明文字，不是 None（got %r）"
   % sch._state["steps"].get("sector_industry"))
ok("sector_industry" in failed, "failed 列表同时记下失败步")
ok(sch._state["current_step"] == "",
   "失败后 current_step 同样清空（got %r）" % sch._state["current_step"])

# ================= 4) run_light 那条路不许碰 _state ==========================
# run_light 跟完整流水线可以并存 —— 它写 _state 就会把正在跑的进度冲掉
sch.fetch_pool = lambda kind, target: 3
sch.fetch_sector_flow = lambda kind: 5
with sch._lock:
    sch._state.update(running=True, kind="scheduled", date="20261012",
                      current_step="main_force", plan=["main_force"],
                      steps={"keep_me": 1})
before_steps = dict(sch._state["steps"])
sch._collect("20261012", False, False, live=False)
ok(sch._state["steps"] == before_steps,
   "live=False（run_light 路径）没动 steps（got %r）" % sch._state["steps"])
ok(sch._state["current_step"] == "main_force",
   "live=False 也没动 current_step（got %r）" % sch._state["current_step"])

# ======================= 5) state() 的 steps 是拷贝 ==========================
with sch._lock:
    sch._state.update(steps={"pool_zt": 1}, current_step="pool_zt")
st = sch.state()
ok(st["steps"] is not sch._state["steps"],
   "返回的 steps 与内部 dict 不是同一个对象")
with sch._lock:
    sch._state["steps"]["pool_zt"] = "外部线程改了"
ok(st["steps"].get("pool_zt") == 1,
   "之后内部再写，**早先拿到的快照不受影响**（got %r）"
   % st["steps"].get("pool_zt"))
st["steps"]["injected"] = 1
ok("injected" not in sch._state["steps"],
   "反过来改返回值也不会反向污染内部状态")

# ================= 6) 只有打分步才透出 sub ================================
mainforce._stage(5, "预选与日线")
mainforce._count(120, 300)
with sch._lock:
    sch._state.update(current_step="main_force")
st = sch.state()
ok("sub" in st, "current_step=main_force 时给 sub")
ok(st["sub"]["stage"] == "预选与日线" and st["sub"]["stage_no"] == 5
   and st["sub"]["stages"] == 7,
   "sub 带阶段名与 5/7（got %r）" % {k: st["sub"].get(k)
                                     for k in ("stage", "stage_no", "stages")})
ok(st["sub"]["done"] == 120 and st["sub"]["total"] == 300,
   "sub 带逐只日线计数 120/300（got %s/%s）"
   % (st["sub"].get("done"), st["sub"].get("total")))
with sch._lock:
    sch._state.update(current_step="pool_zt")
ok("sub" not in sch.state(),
   "非打分步**不**给 sub —— 打分残留值不能漏到别的步骤上")

# ---- progress() 也得是拷贝
snap = mainforce.progress()
snap["done"] = -999
ok(mainforce.progress()["done"] != -999, "mainforce.progress() 返回拷贝")

# ================================ 7) 收尾归零 ================================
with sch._lock:
    sch._state.update({
        "running": False, "kind": "", "date": "",
        "started_at": "", "finished_at": "", "error": "",
        "steps": {}, "plan": [], "current_step": "",
    })
ok(sch.state()["current_step"] == "" and sch.state()["plan"] == [],
   "收尾把 current_step/plan 归零")

# ================= 8) 接线：run_post_close 必须传 live=True ==================
# 桩掉 _collect，只看调用方传了什么参数 —— 组件本身的好坏第 3/4/6 节已经测过，
# 这里锁的是**从 run_post_close 到 _collect 那一跳**。
_seen: dict = {}
_orig_collect = sch._collect


def _stub_collect(target, force, scoring, live=False, **kw):  # noqa: ANN001
    _seen.update(target=target, force=force, scoring=scoring, live=live)
    # 阶段 3 起返回三元组：(results, failed, partial)。partial 是「跑成了但
    # 闸门判数据不全」，与 failed 分开 —— 混成一个就把这个区别弄丢了。
    return {"pool_zt": 5}, [], []


with sch._lock:
    sch._state.update(running=False, steps={}, current_step="", plan=[])

sch._collect = _stub_collect
try:
    # kind="manual"：定时/手机触发那两支会调 notify.after_job 真发飞书
    sch.run_post_close(date_str="20261012", kind="manual")
finally:
    sch._collect = _orig_collect

ok(_seen.get("live") is True,
   "run_post_close 给 _collect 传了 live=True（got %r）—— 漏传则整个任务期间 "
   "steps 恒空、current_step 恒空串，页面上进度条一动不动，"
   "state() 也永远透不出 sub" % _seen.get("live"))
ok(_seen.get("scoring") is True, "完整流水线 scoring=True")
ok(sch._state["steps"] == {"pool_zt": 5},
   "收尾把 results 落进 steps（got %r）" % sch._state["steps"])
ok(sch._state["running"] is False, "跑完 running 归 False")

# run_light 反过来：它与完整流水线可以并存，写了 _state 就会把正在跑的那轮进度冲掉
_seen.clear()
sch._collect = _stub_collect
try:
    sch.run_light(date_str="20261012", kind="manual")
finally:
    sch._collect = _orig_collect

ok(_seen.get("live") is False,
   "run_light 走 live=False（不占 _state，可与完整流水线并存）")
ok(_seen.get("scoring") is False, "轻量流水线不打分")

# ============ 9) 阶段 4：补跑**检查**触发点（只查不跑，零外发） ============
# 任务书要「16:30 之后的后续时间段检查重试」；AGENTS.md 的硬口径是「任何定时器
# 都不碰会联网的补跑入口」。两者取交集 = 触发点只做只读检查（2026-10-17 用户拍板）。
ok(len(sch.RETRY_CRON) >= 2,
   "补跑检查有两个触发点：%r" % (sch.RETRY_CRON,))
ok(all(isinstance(p, tuple) and len(p) == 2 and 0 <= p[0] <= 23
       and 0 <= p[1] <= 59 for p in sch.RETRY_CRON),
   "每个触发点都是合法的 (时, 分)")

# ---- 「开了就跑」：开关一开，检查点跟着装上；两个都关一个 job 都没有 ----
# 期间把日历桩成本地判定：`start()` 里的 `_warn_if_missed_today` 会去问
# `calendar.is_trade_day()`，日历过期时它会拉一次网（守卫会把那次记成一次
# 真实外发企图）。这里要测的是**接线**，不是日历。
_ORIG_IS_TD = sch.calendar.is_trade_day
sch.calendar.is_trade_day = lambda *a, **k: False
try:
    db.kv_set(sch._ENABLED_KEY, "1")
    db.kv_set(sch._PUSH_ENABLED_KEY, "0")
    sch.start()
    _ids = sorted(j.id for j in sch._scheduler.get_jobs())
    ok("post_close" in _ids, "开定时任务 => 16:30 装上（%s）" % _ids)
    _retry_ids = [i for i in _ids if i.startswith("retry_check_")]
    ok(sorted("retry_check_%02d%02d" % p for p in sch.RETRY_CRON) == _retry_ids,
       "开定时任务 => 补跑检查点也装上（%s）" % _retry_ids)
    _info = sch.schedule_info()
    ok(bool(_info["next_retry_check"]) and bool(_info["next_run"]),
       "下一次检查/下一次盘后都给得出（%s / %s）"
       % (_info["next_retry_check"], _info["next_run"]))
    ok(_info["retry_points"] == ["%02d:%02d" % p for p in sch.RETRY_CRON],
       "retry_points 与设计一致：%s" % _info["retry_points"])
    sch.stop()
    ok(sch._scheduler is None, "stop() 后不留调度器（避免重复调度器）")

    # 只开推送：补跑检查点**不装** —— 它属于「定时任务」这一个开关
    db.kv_set(sch._ENABLED_KEY, "0")
    db.kv_set(sch._PUSH_ENABLED_KEY, "1")
    sch.start()
    _ids2 = sorted(j.id for j in sch._scheduler.get_jobs())
    ok("fund_push" in _ids2
       and not [i for i in _ids2 if i.startswith("retry_check_")],
       "只开推送时不装补跑检查点（%s）" % _ids2)
    sch.stop()

    # 两个都关 = 用户当前的设置：整个调度不启，也不会自作主张打开
    db.kv_set(sch._PUSH_ENABLED_KEY, "0")
    sch.start()
    ok(sch._scheduler is None, "两个开关都关 => 一个 job 都不装")
    sch.stop()
finally:
    sch.calendar.is_trade_day = _ORIG_IS_TD
    db.kv_set(sch._ENABLED_KEY, "0")
    db.kv_set(sch._PUSH_ENABLED_KEY, "0")

# ---- 触发点本体：有到点的也只报不跑 ----
_real_run_retries = sch.run_retries


def _no_run_retries(*a, **k):  # noqa: ANN001, ANN202
    raise AssertionError("定时器不许调 run_retries —— 那是一次内网外发")


db.ingest_start("pool_zt", "20261016")
ing.record_failure("pool_zt", "20261016", ConnectionError("桩"))
_con = sqlite3.connect(str(settings.db_path))
_con.execute("UPDATE ingest_job SET next_retry_at='1990-01-01T00:00:00+00:00' "
             "WHERE domain='pool_zt' AND trade_date='20261016'")
_con.commit()
_con.close()
sch.run_retries = _no_run_retries
try:
    sch._retry_fire()
    ok(True, "到点待补跑时触发点照常返回（没抛 = 没去调 run_retries）")
finally:
    sch.run_retries = _real_run_retries
_ck = sch.last_retry_check()
ok(bool(_ck) and _ck.get("due") == 1 and _ck.get("ran") is False,
   "检查结果落 kv：1 条到点、ran 恒 False —— %r" % _ck)
ok(bool(_ck) and _ck.get("domains") == ["pool_zt"],
   "到点的域点名给出 —— %r" % (_ck or {}).get("domains"))
ok(bool(_ck) and _ck.get("checked_at"),
   "带 checked_at，页面答得出「上次检查是什么时候」")
ok(sch.schedule_info().get("retry_check") == _ck,
   "schedule_info 把这条检查记录透出去（/jobs/status 里看得到）")

# ---- 源码级：这个函数里不许出现会外发的东西 ----
# 只桩住 run_retries 挡的是「这一次」，源码断言挡的是「以后谁改了它」。
_tree = ast.parse(Path(sch.__file__).read_text(encoding="utf-8"))
_fn = next(n for n in _tree.body
           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
           and n.name == "_retry_fire")
_names = {n.id for n in ast.walk(_fn) if isinstance(n, ast.Name)}
_names |= {n.attr for n in ast.walk(_fn) if isinstance(n, ast.Attribute)}
ok("run_retries" not in _names and "is_trade_day" not in _names,
   "_retry_fire 源码里没有 run_retries / is_trade_day —— %s" % sorted(_names))
ok("ingest_due" in _names,
   "_retry_fire 真的去查了到点的行（ingest_due），不是空转")

ok(len(_BLOCKED) == 0,
   "全程一个包都没发出去（blocked=%d）" % len(_BLOCKED))

print("ALL %d ASSERTIONS PASSED" % N[0])
