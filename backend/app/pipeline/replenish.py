# -*- coding: utf-8 -*-
"""历史补录（2026-11-11）—— 把「主力建仓候选」页缺的历史数据补回来。

四个 job，全部由「历史补录」页的按钮触发（``POST /api/replenish/start``），
服务启动、页面刷新、任何定时器都**不碰**它们 —— 与 ``POST /api/pipeline/backfill``
同一条纪律：每个 job 都是几百到上万次朝数据源发包，内网下还会被安全设备
拦截并记录，只能由人明确点了按钮才跑。

* ``bars``   日线全历史（**不复权** OHLCV）+ 复权因子。走统一任务入口
            ``tasks.run_task("historical_backfill")`` 分批跑 —— 不另建第二套
            回填实现（AGENTS.md「统一任务入口」那条）。
* ``inflow`` 逐日主力净流入全历史。东财 ``stock_fflow_daykline`` 一只票一次
            就回**全历史**（大单分解也带），补进 ``stock_net_inflow`` 的
            **缺失格** —— 已有行**不覆盖**：盘后当场采的快照才是当日口径，
            daykline 只当兜底（与 ``mainforce.backfill_inflow`` 同口径）。
* ``pools``  涨/炸/跌停/强势池按日补采。东财只保留最近 30 个**自然日**
            （``ztpool.window_start``），过期日期**永久不可补**（收盘当场采的，
            事后取不回）—— 如实报给页面，不假装补过。
* ``score``  历史打分补跑：逐日跑 ``scheduler.score_and_capture``。池子拿不到
            的日期**跳过并说明原因** —— 「历史模式打分」（用日线反推涨跌停）
            还没上，跳过比拿缺池子的粗筛冒充补过诚实。补数据的顺序是
            先 ``pools`` 再 ``score``。

断点续跑靠判据本身，不另建进度表（与 ``POST /pipeline/backfill`` 的
``finished=false`` 续跑同一口径）：``bars`` 每只票查区间覆盖、``inflow`` 查
kv 完成清单、``pools`` / ``score`` 查库缺哪天补哪天 —— 进程重启后**原样再点
一次就是续跑**。同 job 不并发（Busy），不同 job 可以并行：熔断按域分桶，
日线与资金流不同域（``em_hist`` vs ``em_fflow_hist``），不会互相拖下水。
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.fetchers import ztpool
from app.metrics import mainforce
from app.pipeline import backfill
from app.pipeline import factor as factor_fill
from app.pipeline import tasks as tasksvc
from app.scheduler import fetch_pool, score_and_capture, target_date
from app.store import db

logger = logging.getLogger(__name__)

#: 四个 job。加 job 先想清楚「断点续跑的判据是什么」——判据不清晰的 job
#: 一重启就只能从头再来，等于骗人点按钮。
JOBS = ("bars", "inflow", "pools", "score")

#: 一次 run_task 调用带走的票数（bars）。100 只 ≈ 2-3 分钟，进度粒度够细，
#: job_run 又不至于每 40 只就留一条记录。
_CHUNK = 100

#: inflow 一批攒多少只再统一按日期合并落库。逐只 merge 是 O(只数×日期数)
#: 次整包 load/save（5000 只 × 200 天 = 百万次），按批攒够再按日期合并，
#: 全程只有「批数×日期数」次写库。
_INFLOW_BATCH = 200

#: inflow 完成清单（kv）。判据粒度=票：一只票的全历史一天就取完。
_INFLOW_DONE_KEY = "replenish_inflow_done"

#: 每个 job 的状态。skipped 只留前 100 条（页面也只展示这么多）。
_SKIPPED_CAP = 100


class Busy(RuntimeError):
    """同一个 job 已经在跑。"""


def _blank() -> Dict[str, Any]:
    return {"running": False, "error": "", "started_at": "",
            "finished_at": "", "total": 0, "done": 0, "rows": 0,
            "failed": 0, "skipped": [], "msg": ""}


_STATE: Dict[str, Dict[str, Any]] = {job: _blank() for job in JOBS}
#: 优雅停止：置 True 后 worker 在**当前票/批边界**退出，已落库的都保留。
_STOP: Dict[str, bool] = {job: False for job in JOBS}
_GUARD = threading.Lock()


def snapshot() -> Dict[str, Any]:
    """页面轮询的状态（零联网：只读内存状态）。"""
    return {job: dict(st) for job, st in _STATE.items()}


def active_job() -> Optional[str]:
    """正在跑的 job 名（没跑回 None）。"""
    for job, st in _STATE.items():
        if st["running"]:
            return job
    return None


def stop(job: str) -> Dict[str, Any]:
    """请求优雅停止。不抛 —— 停一个没在跑的 job 是无害操作。"""
    if job not in _STATE:
        raise ValueError(f"不认识的 job：{job!r}，可选 {list(JOBS)}")
    _STOP[job] = True
    return {"stop_requested": _STATE[job]["running"], "job": job}


def run_replenish(job: str, start: Optional[str] = None,
                  end: Optional[str] = None) -> Dict[str, Any]:
    """起一个补录 job（后台守护线程），立即返回状态。

    ``start`` 缺省 ``20260101``（用户拍板的补录起点）；``end`` 缺省最新交易日
    （推不出来就用今天）。**只在人点「历史补录」页按钮时调**。
    """
    if job not in _STATE:
        raise ValueError(f"不认识的 job：{job!r}，可选 {list(JOBS)}")
    with _GUARD:
        if _STATE[job]["running"]:
            raise Busy(f"{job} 已经在跑 —— 等它结束或先点停止")
        st = _STATE[job]
        st.update(_blank())
        st["running"] = True
        st["started_at"] = datetime.now().isoformat(timespec="seconds")
        _STOP[job] = False

    lo = str(start or "20260101")
    hi = str(end or target_date() or datetime.now().strftime("%Y%m%d"))
    worker: Dict[str, Callable[[str, str], None]] = {
        "bars": _run_bars, "inflow": _run_inflow,
        "pools": _run_pools, "score": _run_score,
    }
    threading.Thread(target=_wrap, args=(job, worker[job], lo, hi),
                     name=f"replenish-{job}", daemon=True).start()
    return {"status": "started", "job": job, "start": lo, "end": hi,
            "state": dict(_STATE[job])}


def _wrap(job: str, fn: Callable[[str, str], None], lo: str, hi: str) -> None:
    """统一收尾：状态落定 + 异常只进 error 不掀翻线程。"""
    st = _STATE[job]
    try:
        fn(lo, hi)
    except Exception as exc:  # noqa: BLE01  后台线程的异常只记录，不外抛
        logger.exception("补录 %s 失败", job)
        st["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        st["running"] = False
        st["finished_at"] = datetime.now().isoformat(timespec="seconds")
        if _STOP[job]:
            st["msg"] = (st["msg"] + "（已停止，原样再点一次就是续跑）").strip()
        _STOP[job] = False


def _skip(st: Dict[str, Any], key: str, reason: str) -> None:
    if len(st["skipped"]) < _SKIPPED_CAP:
        st["skipped"].append({"key": key, "reason": reason})


# ------------------------------------------------------------------ bars


def _run_bars(start: str, end: str) -> None:
    """日线全历史 + 复权因子，分批走统一任务入口。

    每只票查区间覆盖的判据在 ``backfill.backfill`` 里面 —— 已覆盖的票不发
    请求，所以「原样再点一次」天然就是续跑，不需要记进度。
    """
    st = _STATE["bars"]
    codes = backfill.load_scope("all")
    st["total"] = len(codes)
    if not codes:
        st["msg"] = "名单为空：库里没有可回填的股票名单（先跑一次盘后任务）"
        return
    for off in range(0, len(codes), _CHUNK):
        if _STOP["bars"]:
            break
        chunk = codes[off:off + _CHUNK]
        try:
            res = tasksvc.run_task(
                "historical_backfill", trade_date=end,
                domain="daily_bar", start=start, end=end,
                items=chunk, budget=len(chunk))["result"]
        except Exception as exc:  # noqa: BLE01  整批挂了停，不往下冲
            st["error"] = f"{type(exc).__name__}: {exc}"
            break
        st["rows"] += int(res.get("inserted") or 0)
        st["failed"] += int(res.get("failed") or 0)
        for p in (res.get("problems") or [])[:5]:
            _skip(st, str(p.get("item")), str(p.get("error")))
        for code in chunk:
            if _STOP["bars"]:
                break
            try:
                factor_fill.backfill_factors(code)
            except Exception as exc:  # noqa: BLE01  单票因子挂了不连累整批
                _skip(st, code, f"因子: {type(exc).__name__}: {exc}")
        st["done"] += len(chunk)
        st["msg"] = ("日线 %d/%d 只 · 落库 %d 行 · 失败 %d"
                     % (st["done"], st["total"], st["rows"], st["failed"]))


# ----------------------------------------------------------------- inflow


def _run_inflow(start: str, end: str) -> None:
    """逐日主力净流入全历史。一只票一次取全历史，按批合并落库。

    只补**缺失格**：某天库里已有这只票（盘后当场采的快照）就跳过，
    daykline 不覆盖当日口径（与 ``mainforce.backfill_inflow`` 同一规矩）。
    """
    st = _STATE["inflow"]
    codes = backfill.load_scope("all")
    done = set(json.loads(db.kv_get(_INFLOW_DONE_KEY) or "[]"))
    done &= set(codes)
    todo = [c for c in codes if c not in done]
    st["total"] = len(codes)
    st["done"] = len(done)
    if not todo:
        st["msg"] = "全部 %d 只已补过（想重来先清 kv 键 %s）" % (
            st["total"], _INFLOW_DONE_KEY)
        return

    from app.fetchers import quotes  # noqa: PLC0415  顶层导会拖一路依赖

    for off in range(0, len(todo), _INFLOW_BATCH):
        if _STOP["inflow"]:
            break
        batch = todo[off:off + _INFLOW_BATCH]
        patches: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for code in batch:
            if _STOP["inflow"]:
                break
            try:
                rows = quotes.stock_fflow_daykline(code)
            except Exception as exc:  # noqa: BLE01  单票失败计数不拖垮
                st["failed"] += 1
                _skip(st, code, f"{type(exc).__name__}: {exc}")
                continue
            rows = [r for r in rows if start <= r["date"] <= end]
            if not rows:
                _skip(st, code, "源无区间内数据")
                continue
            for r in rows:
                patches.setdefault(r["date"], {})[code] = {
                    "code": code,
                    "net_inflow": r.get("net_inflow"),
                    "large_order_net": r.get("large_order_net"),
                    "net_pct": r.get("net_pct"),
                }
        # ---- 按日期合并：一次 load/save 一个日期，缺失格才填 ----
        for date, patch in patches.items():
            old = db.load("stock_net_inflow", date) or []
            by_code = {r.get("code"): r for r in old}
            filled = 0
            for code, row in patch.items():
                if code in by_code:
                    continue          # 已有当日快照，daykline 不覆盖
                by_code[code] = row
                filled += 1
            if filled:
                db.save("stock_net_inflow", date, list(by_code.values()),
                        source="mixed" if old else "eastmoney")
                st["rows"] += filled
        done.update(batch)
        db.kv_set(_INFLOW_DONE_KEY,
                  json.dumps(sorted(done), ensure_ascii=False))
        st["done"] = len(done)
        st["msg"] = ("资金流 %d/%d 只 · 补格 %d · 失败 %d"
                     % (st["done"], st["total"], st["rows"], st["failed"]))


# ------------------------------------------------------------------ pools


def _run_pools(start: str, end: str) -> None:
    """近 30 天池子按日补采。过窗日期永久不可补 —— 如实跳过并写明原因。"""
    st = _STATE["pools"]
    lo = max(start, ztpool.window_start())
    dates = [d for d in _trade_dates() if lo <= d <= end]
    st["total"] = len(dates) * 4
    if lo > start:
        _skip(st, start, "东财池子只保留最近 30 个自然日（今天最早可补 %s）——"
              "更早的日期永久不可补" % ztpool.window_start())
    if not dates:
        st["msg"] = "窗口内没有交易日可补"
        return
    for d in dates:
        for kind in ("zt", "zb", "dt", "strong"):
            if _STOP["pools"]:
                return
            if db.load(f"pool_{kind}", d):
                st["done"] += 1
                continue              # 已有快照不覆盖
            try:
                n = fetch_pool(kind, d)
                st["rows"] += int(n or 0)
            except Exception as exc:  # noqa: BLE01  一天一池挂了不连累其余
                st["failed"] += 1
                _skip(st, f"{kind}@{d}", f"{type(exc).__name__}: {exc}")
            st["done"] += 1
        st["msg"] = "池子 %d/%d（%s 起）" % (st["done"], st["total"], lo)


# ------------------------------------------------------------------- score


def _run_score(start: str, end: str) -> None:
    """历史打分补跑：缺哪天补哪天，池子拿不到的日期跳过并说明原因。"""
    st = _STATE["score"]
    dates = [d for d in _trade_dates() if start <= d <= end]
    st["total"] = len(dates)
    for d in dates:
        if _STOP["score"]:
            return
        if db.load_one("main_force", d):
            st["done"] += 1
            continue                   # 已打过不重跑
        reason = mainforce.pool_unavailable_reason(d)
        if reason:
            _skip(st, d, reason)
            continue
        # 池子在全市场快照**之前**（AGENTS.md 打分阶段顺序）：先补当日池子
        # 再打分，粗筛的涨跌停排除才有依据。
        try:
            for kind in ("zt", "zb", "dt", "strong"):
                if not db.load(f"pool_{kind}", d):
                    fetch_pool(kind, d)
        except Exception as exc:  # noqa: BLE01
            _skip(st, d, f"池子: {type(exc).__name__}: {exc}")
            continue
        try:
            res = score_and_capture(d, d)
            st["rows"] += len(res.get("rows") or [])
        except db.DomainBusy as exc:
            _skip(st, d, f"正在采集中：{exc}")
            continue
        except Exception as exc:  # noqa: BLE01  一天挂了不连累其余
            st["failed"] += 1
            _skip(st, d, f"{type(exc).__name__}: {exc}")
            continue
        st["done"] += 1
        st["msg"] = "打分 %d/%d 天（%s）" % (st["done"], st["total"], d)


def _trade_dates() -> List[str]:
    """交易日历（**只读本地缓存**，不联网 —— 同矩阵列的口径）。"""
    from app.fetchers import calendar as tcal  # noqa: PLC0415
    return [str(d) for d in tcal.cached_trade_dates()]
