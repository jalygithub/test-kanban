"""盘后调度 —— 交易日收盘后自动跑完整盘后流水线。

一个文件放两样东西，因为它们是同一个职责的两半：

  1. 流水线 ``run_post_close()``：抓股池 -> 抓板块资金流 -> 跑主力建仓打分
  2. APScheduler 定时接线 ``start()`` / ``stop()``

流水线**分段独立 try**：任何一段挂了都不能连累另外两段。股池挂了，板块
资金流和打分照样跑；打分挂了，前两段的成果照样落库。最后汇总成一条
``job_run`` 记录，``/api/jobs/status`` 和总览页读的就是它。

放在 routes 之外是为了避免循环导入 —— routes 要调流水线，流水线不能反过来
import routes，所以股池取数的 ``_POOL_GETTERS`` 也一并搬到这里。

**不自动补跑**：应用启动时若已过 16:30，不会立刻补一轮（那会未经告知就跑 25
分钟打分）。改为打日志提醒，由总览页的「立即执行」手动触发。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional

from app import notify, subscribe
from app.config import settings
from app.fetchers import calendar, sector, ztpool
from app.metrics import mainforce
from app.pipeline import bars_sync, ingest, top5track
from app.store import db

logger = logging.getLogger(__name__)

POOL_KINDS = ("zt", "zb", "dt", "strong")
SECTOR_KINDS = ("industry", "concept")

_POOL_GETTERS: Dict[str, Callable[[str], List[Dict[str, Any]]]] = {
    "zt": ztpool.get_zt_pool,
    "zb": ztpool.get_zb_pool,
    "dt": ztpool.get_dt_pool,
    "strong": ztpool.get_strong_pool,
}

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "running": False, "kind": "", "date": "",
    "started_at": "", "finished_at": "", "error": "", "steps": {},
    # 本轮计划跑哪几步（进度条的分母）与正在跑哪一步（进度条的当前值）。
    # 必须在**运行中**就逐步写进去 —— 只在 `_finish` 里落的话，页面上那句
    # 「已完成 N 步」整个任务期间都会停在 0。
    "plan": [], "current_step": "",
}
_scheduler = None  # apscheduler.BackgroundScheduler | None

# 用户可改的开关，存进 SQLite（见 store.db.kv_set）。默认开。
#: 盘后任务（16:30 抓取 + 打分）的开关。**键名不能改** —— 用户「保持关闭」
#: 这个选择就存在这里，改了名等于把选择重置成默认。
_ENABLED_KEY = "scheduler_enabled"
#: 14:50 估值推送的开关，**与盘后任务各自独立**（2026-10-12 用户拍板：
#: 推送只是发一条消息，盘后打分要跑十几分钟，不该绑在一起开或一起关）。
#: 默认**关** —— 不开就不会自动往飞书发消息。
_PUSH_ENABLED_KEY = "scheduler_push_enabled"

#: 阶段 4「后续时间段检查重试」的触发点：收盘后一轮 + 次日开盘前一轮。
#:
#: 时刻刻意避开 16:30 那轮的打分尾巴（约 25 分钟到 17:00 前后），并赶在
#: 09:30 开盘之前 —— 检查本身是毫秒级本地读，但落在这两个点上人刚好在。
RETRY_CRON: tuple = ((17, 30), (9, 20))
#: 上一次补跑检查的记录（kv 里那条 JSON）。`GET /pipeline/health` 读它答
#: 「上次定时检查是什么时候、当时几条到点」—— 没有这条，「调度开着」和
#: 「调度根本没装」在页面上长得一模一样。
_RETRY_CHECK_KEY = "retry_check"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def state() -> Dict[str, Any]:
    """当前（或最近一次）盘后任务的实时状态。进程内存，重启即清零。

    `steps` 必须**拷一层**：完整流水线跑起来后每完成一步就往这个 dict 里写，
    而 `/jobs/status` 每 3 秒被另一个线程读一次 —— 把内部 dict 直接交出去，
    JSON 序列化会撞上 `dictionary changed size during iteration`。

    `sub` 是 `main_force` 那一步的内部进度。它占整条流水线八成以上的墙钟时间，
    不把它透出去，进度条就会在最后一步上一动不动地卡十几分钟。
    """
    with _lock:
        out = dict(_state, steps=dict(_state.get("steps") or {}))
    if out.get("current_step") == "main_force":
        out["sub"] = mainforce.progress()
    return out


# ------------------------------------------------------------------ 取数段


def _collect_domain(domain: str, trade_date: str, fetch: Callable[[], Any],
                    source: str = "", required: tuple = (),
                    allow_empty: bool = False, store: bool = True) -> int:
    """取数 + 逐域状态登记，一步走完。

    ``db.ingest_try_start`` 必须在**发请求之前**调：置 RUNNING 并 +1 尝试
    次数。等抓完了再记的话，整个抓取期间页面上「正在抓取」那一栏都是空的，
    而且失败时 `record_failure` 会读到 attempts=0，退避永远算不对。

    用互斥版而不是裸 `ingest_start`：同域同天已经在跑就抛 `DomainBusy`，
    **一个请求都不发**。两个入口（页面刷新 + 定时任务）同时要同一天同一域
    的数据是常事，没有这道闸就等于双倍打源。

    抛异常时**原样上抛**（让 `_run_step` 记进 failed），但状态已经先落成
    WAITING/FAILED 了 —— 两件事分开记，异常路径也不会漏掉逐域状态。
    ``DomainBusy`` 例外：它在任何状态写入**之前**就抛了，绝不能去
    `record_failure` —— 那会把正在跑的那一轮的退避信息冲掉。
    """
    db.ingest_try_start(domain, trade_date, source=source)
    try:
        rows = fetch()
    except Exception as exc:  # noqa: BLE001  上抛给 _run_step，这里只管状态
        ingest.record_failure(domain, trade_date, exc)
        raise
    out = ingest.record(domain, trade_date, rows, source=source,
                        required=required, allow_empty=allow_empty,
                        store=store)
    return out["stored"]


def fetch_pool(kind: str, trade_date: str) -> int:
    return _collect_domain(
        f"pool_{kind}", trade_date,
        lambda: _POOL_GETTERS[kind](trade_date),
        source="em:ztb", required=("code", "name", "price"))


def fetch_sector_flow(kind: str, trade_date: str = "latest") -> int:
    # 板块资金流**没有 code**，只有 name —— 必填字段得按它的形状给，
    # 硬要求 code 会让整批被判成缺字段，闸门直接失效。
    return _collect_domain(
        f"sector_flow_{kind}", trade_date,
        lambda: sector.get_sector_flow(kind),
        source="10jqka/em", required=("name",))


def fetch_daily_bar(trade_date: str) -> int:
    """日线采集（2026-11-11）：全市场快照顺带落 `daily_bar`。

    排在打分**之前**（`_plan` 同序）：采集几秒落完当日全市场日线，打分跑
    二十几分钟后落榜 —— 页面上当天列的名次与股价一次齐。三道闸门（日期
    相符 / 15:01 后 / 源带 OHLC）都在 `bars_sync.collect` 里，不过就抛，
    走 `record_failure` 进退避重试。

    ``store=False``：落库由 `bars_sync` 自己走 `bars_upsert`，再走
    `ingest.record` 的落库分支会把行写进 snapshot 表（`daily_bar` 是行情表，
    不是快照域）。``source`` 写死 eastmoney：备源同花顺给不出日线，能成功
    的轮次只可能来自东财。
    """
    return _collect_domain(
        "daily_bar", trade_date,
        lambda: bars_sync.collect(trade_date),
        source="eastmoney",
        required=("code", "open", "high", "low", "close"),
        store=False)


# ------------------------------------------------------------------ 流水线


def target_date() -> Optional[str]:
    """今天是交易日就取今天，否则取最近交易日（周末手动触发也能补跑历史）。

    **公开**给 `pipeline.tasks` 用：六个任务都要先定「跑的是哪一天」，两边各
    写一遍推算逻辑就会各说各话（任务记的日期与流水线跑的日期对不上，是最难
    查的一类不一致）。日历挂了退化到库内最新日期 —— 周末/节假日手动点照样
    能落在一个真实交易日上。
    """
    try:
        today = date.today().strftime("%Y%m%d")
        if calendar.is_trade_day(today):
            return today
        return calendar.last_trade_date()
    except Exception as exc:  # noqa: BLE001  日历走网络，挂了要能降级
        logger.warning("交易日历不可用，改用库内最新日期: %s", exc)
    for kind in POOL_KINDS:
        d = db.latest_date(f"pool_{kind}")
        if d:
            return d
    return None


def _run_step(results: Dict[str, Any], failed: List[str],
              name: str, fn: Callable[[], Any], live: bool = False,
              domain: Optional[str] = None,
              trade_date: Optional[str] = None,
              partial: Optional[List[str]] = None) -> None:
    """单步执行：记录耗时与结果，异常转成失败项，不外抛。

    ``live=True`` 时把「正在跑哪一步」「这一步的结果」**实时**写进 ``_state``。
    不这么做的话 ``steps`` 要等到 ``_finish`` 才落，页面上「已完成 N 步」整个
    任务期间都会停在 0 —— 进度条等于没做。

    ``live`` 默认 False 是有原因的：``run_light`` 跟完整流水线可以**并存**（见
    它的文档串）。轻量抓取若也往 ``_state`` 写，就会把正在跑的那轮完整任务的
    进度冲掉。

    ``domain`` + ``trade_date`` 给了就会在每步跑完后去查逐域采集状态：闸门判
    PARTIAL（源没抛异常但数据不全）的步骤挂进 ``partial``。**这跟 `failed`
    是两回事** —— failed 是「没跑成」，partial 是「跑成了但数据不能信」。
    把后者也塞进 failed 会丢掉这个区别，而这个区别正是本阶段要修的：
    `{"pool_zt": 0}` 过去被判成了 success。
    """
    if live:
        with _lock:
            _state["current_step"] = name
    t0 = time.monotonic()
    busy = False
    try:
        results[name] = fn()
        logger.info("盘后步骤 %-14s %.1fs -> %s", name, time.monotonic() - t0,
                    results[name])
    except db.DomainBusy as exc:
        # **不是失败**：另有一轮正在跑这一域这一天（比如页面手动刷新撞上了
        # 定时任务）。记成「跳过」—— 不进 failed 也不进 partial：数据照样会
        # 由那一轮落库，这一轮只是没份参与。混进 failed 会让人以为断网了。
        busy = True
        results[name] = f"跳过: {exc}"
        logger.info("盘后步骤 %-14s %s", name, exc)
    except Exception as exc:  # noqa: BLE001  单步失败不能拖垮整条流水线
        logger.exception("盘后步骤 %s 失败", name)
        results[name] = f"失败: {type(exc).__name__}: {exc}"
        failed.append(name)
    finally:
        # 放 finally：失败那条也得算「已完成」，否则进度条会停在失败步上不动
        if live:
            with _lock:
                _state["steps"][name] = results.get(name)
                _state["current_step"] = ""
        if (partial is not None and domain and trade_date
                and name not in failed and not busy):
            _check_gate(name, domain, trade_date, partial)


def _check_gate(name: str, domain: str, trade_date: str,
                partial: List[str]) -> None:
    """把逐域采集状态里判成 PARTIAL 的步骤挂进 ``partial``。

    查不到记录就当没闸门（比如 `run_light` 之外的地方直接调了老函数）——
    宁可漏判一次，也不要因为表里没行就把整轮任务判失败。
    """
    try:
        row = db.ingest_get(domain, trade_date)
    except Exception as exc:  # noqa: BLE001  状态表读不到不该拖垮流水线
        logger.warning("读取逐域采集状态失败 %s @ %s: %s",
                       domain, trade_date, exc)
        return
    if not row or row.get("status") != ingest.PARTIAL:
        return
    partial.append(name)
    logger.warning("步骤 %s 数据不完整：%s", name,
                   row.get("error") or "闸门未通过")


def _plan(scoring: bool) -> List[str]:
    """本轮计划跑哪几步 —— 进度条的分母。

    必须和下面 `_collect` 的实际调用**由同一处生成**：两边各写一遍，加了步骤
    忘了改一边，页面上的百分比就会一直算错。
    """
    plan = [f"pool_{k}" for k in POOL_KINDS]
    plan += [f"sector_{k}" for k in SECTOR_KINDS]
    if scoring:
        # 日线采集（2026-11-11）：排在打分之前 —— 打分落库那天列的名次与
        # 采集先落的价同时齐（两张表互不依赖，采集先行只为页面一次完整）。
        # run_light 不装：轻量流水线盘中也跑，15:01 闸门会把它判失败。
        plan.append("daily_bar")
        plan.append("main_force")
        # 前5跟踪取价（2026-11-11）：名单由当天打分推导，必须排在打分之后，
        # 所以同样只在打分轮里装（run_light 不打分，名单也不会变）。
        plan.append("top5_track")
    return plan


def _collect(target: str, force: bool, scoring: bool, live: bool = False
             ) -> tuple[Dict[str, Any], List[str], List[str]]:
    """跑指定步骤，返回 ``(results, failed, partial)``。每步独立 try，失败不外抛。

    ``failed`` = 没跑成（抛异常）；``partial`` = 跑成了但闸门判数据不全。
    两者**不合并**：页面上「失败 2 步」和「3 步数据不完整」要人做的事不同，
    前者查网络，后者查源为什么只回了一半。

    ``live=True`` 时逐步回写 ``_state`` 供页面算进度；``run_light`` 走默认
    False，理由见 `_run_step`。
    """
    results: Dict[str, Any] = {}
    failed: List[str] = []
    partial: List[str] = []

    for k in POOL_KINDS:
        _run_step(results, failed, f"pool_{k}",
                  lambda k=k: fetch_pool(k, target), live=live,
                  domain=f"pool_{k}", trade_date=target, partial=partial)
    for k in SECTOR_KINDS:
        _run_step(results, failed, f"sector_{k}",
                  lambda k=k: fetch_sector_flow(k), live=live,
                  domain=f"sector_flow_{k}", trade_date="latest",
                  partial=partial)

    if scoring:
        # 日线采集：sector 之后、打分之前（与 _plan 同序）。失败/未收盘抛出
        # 只影响这一步 —— 后面的打分照跑，价等补跑再落。
        _run_step(results, failed, "daily_bar",
                  lambda: fetch_daily_bar(target), live=live,
                  domain="daily_bar", trade_date=target, partial=partial)
        # 打分最重，放最后：前两段的成果已经落库，这里挂了也不白跑。
        # run() 自带缓存，但这里显式判断一次，日志里能区分「跳过」与「命中缓存」。
        if not force and db.latest_date("main_force") == target:
            results["main_force"] = "已打分，跳过（force=True 可重算）"
            logger.info("盘后步骤 %-14s 已打分，跳过", "main_force")
        else:
            _run_step(results, failed, "main_force",
                      lambda: _score_and_record(target),
                      live=live, domain="main_force", trade_date=target,
                      partial=partial)
        # 前5跟踪取价：排在打分后（roster 扫当天刚落的快照）；打分被跳过时
        # 照跑 —— 每天都要拉价。没有 ingest 域（派生步骤不是采集域），
        # 不传 domain，闸门不适用；失败留下的缺口第二天照拉，自愈不进重试。
        _run_step(results, failed, "top5_track",
                  lambda: top5track.pull(target), live=live, partial=partial)
    return results, failed, partial


def score_and_capture(target: str, run_date: Optional[str] = None
                      ) -> Dict[str, Any]:
    """跑打分并登记逐域状态，返回 ``mainforce.run()`` 的**完整结果**。

    流水线（`_collect`）和页面上那个「重算打分」按钮必须走**同一个**任务
    服务 —— 各写一套的话，一边带状态登记、一边不带，页面上就会出现「打完
    了但 `ingest_job` 里查无此域」这种没法解释的空档（本阶段要收编的旁路
    就是这么来的）。

    ``target`` 是登记进 `ingest_job` 的那一天，必须是**真实交易日**；
    ``run_date`` 是透给 `mainforce.run(date=...)` 的入参，可以是 None ——
    那样它自己按「今天/最近交易日」推。两者通常相同，只有页面上「按指定
    日期重算」时才不一样。

    ``store=False`` **不能省**：`mainforce.run()` 自己落的是**包型** payload
    （`[{rows, all_rows, params, model}]`），走 `ingest.record` 的落库分支会
    用一个裸行列表把整个包覆盖掉 —— `all_rows`、`params`、`model` 当场没了，
    研究模块的可重放校验（`replay_score`）也就跟着废了。

    必填字段验的是**内层**的 code/score —— 外层行上根本没有这两个键，
    按外层验会把每天每行全判成缺字段（覆盖审计里翻过一次同样的车）。
    """
    db.ingest_try_start("main_force", target, source="scorer")
    try:
        result = mainforce.run(date=run_date, refresh=True)
    except Exception as exc:  # noqa: BLE001  先把状态落掉再上抛
        ingest.record_failure("main_force", target, exc)
        raise
    rows = result.get("rows") or []
    ingest.record("main_force", target, rows, source="scorer",
                  required=("code", "score"), store=False)
    # **不改 result 本身** —— 它可能来自 mainforce 内部缓存，往里塞键会把
    # 缓存对象污染掉。另起一个 dict，代价是零。
    return {**result, "stored": len(rows)}


def _score_and_record(target: str) -> int:
    """流水线里的那一版：只要落库条数，不要完整结果。

    存在的唯一理由是保持 `_collect` / `run_retries` 的调用形状不变 ——
    真正的活儿都在 `score_and_capture` 里。
    """
    return int(score_and_capture(target, target).get("stored", 0))


def _finish(job_id: int, target: str, force: bool, scoring: bool,
            results: Dict[str, Any], failed: List[str],
            partial: Optional[List[str]] = None) -> str:
    """汇总状态并写 job_run 收尾，返回 status。

    ``partial`` 是本阶段新增的第三种情况：**每一步都没抛异常，但有步骤的数据
    没过闸门**（例如源只回了 0 行）。过去这里只看 `failed` 于是判成
    `success` —— 真库 job_run #18 就是 `{"pool_zt": 0}` 却记 success。
    现在它落 `partial`，detail 里也单独记一份 `partial` 列表，与 `failed`
    分开：前者要查源为什么没数据，后者要查网络。
    """
    partial = list(partial or [])
    if failed and len(failed) >= len(results):
        status = "failed"
    elif failed or partial:
        status = "partial"
    else:
        status = "success"
    detail = json.dumps({"date": target, "force": force, "scoring": scoring,
                         "results": results, "failed": failed,
                         "partial": partial},
                        ensure_ascii=False, default=str)
    db.job_finish(job_id, status, detail)
    return status


def run_post_close(date_str: Optional[str] = None, force: bool = False,
                   kind: str = "scheduled") -> Dict[str, Any]:
    """**完整**流水线（股池 + 板块资金流 + 打分）。**必须在后台线程里调** ——
    打分一段就 ~15 分钟，同步调会让 HTTP 请求超时。

    :param date_str: 目标交易日 YYYYMMDD，缺省按「今天是交易日吗」推算
    :param force:    True 时即使当天已打过分也强制重算
    :param kind:     ``scheduled`` / ``manual`` / ``feishu``，落在 job_run.kind 供区分
    """
    target = date_str or target_date()
    if not target:
        raise RuntimeError("无法确定交易日，请显式传入 date_str=YYYYMMDD")

    with _lock:
        if _state["running"]:
            raise RuntimeError("已有一轮盘后任务在进行中")
        _state.update({
            "running": True, "kind": kind, "date": target,
            "started_at": _now(), "finished_at": "",
            "error": "", "steps": {},
            # plan 是进度条的分母，必须在**开跑时**就给出去 —— 否则前端拿不到
            # 总步数，percent 只能是 0，前几秒会看着像没反应
            "plan": _plan(True), "current_step": "",
        })

    job_id = db.job_start(
        kind, json.dumps({"date": target, "force": force}, ensure_ascii=False),
        trade_date=target)
    t0 = time.monotonic()
    results: Dict[str, Any] = {}
    failed: List[str] = []
    partial: List[str] = []
    status = "failed"
    try:
        # live=True **不能省**：`_run_step` 默认不往 `_state` 写，省了它整个任务
        # 期间 `steps` 恒为空、`current_step` 恒为空串 —— 页面上「已完成 0/7 步」
        # 且 `state()` 因为看不到 current_step=='main_force' 而永不透出 `sub`，
        # 打分那十几分钟进度条一动不动。run_light 才走默认 False（见它文档）。
        results, failed, partial = _collect(target, force, scoring=True, live=True)
        status = _finish(job_id, target, force, True, results, failed, partial)
    except BaseException as exc:  # noqa: BLE001  正常情况下 _collect 不外抛
        db.job_finish(job_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    finally:
        # running 必须在 finally 里清，否则任何意外都会把状态永久卡成「进行中」
        with _lock:
            _state.update({
                "running": False, "finished_at": _now(),
                "steps": dict(results), "error": "; ".join(failed),
                # 收尾清掉：留着的话下次页面会把上一轮的 current_step 当成
                # 「正在跑」（虽然 running=false 时不渲染，但留着就是个坑）
                "current_step": "",
            })
        # 飞书推送在**定时触发**和**手机指令触发**时发（手动「立即执行」按
        # 用户选择不发）：人不在电脑前，跑完得有回音；且放在状态已落定之后，
        # 用户先在页面上看到跑完，再收到手机消息。
        # after_job 自己把异常全吞掉，通知出问题绝不能反过来影响任务
        if kind in ("scheduled", "feishu"):
            notify.after_job(target, status, failed=failed,
                             elapsed=time.monotonic() - t0, kind=kind)

    logger.info("盘后任务结束 status=%s date=%s steps=%d failed=%s",
                status, target, len(results), failed)
    return {"date": target, "status": status, "results": results,
            "failed": failed}


def run_light(date_str: Optional[str] = None, kind: str = "manual"
              ) -> Dict[str, Any]:
    """**轻量**流水线（股池 + 板块资金流，不含打分），同步、秒级返回。

    不占用 ``_state``：它跟完整流水线可以并存 —— 两者都是幂等 upsert，
    SQLite 有全局锁，不会写坏数据。
    """
    target = date_str or target_date()
    if not target:
        raise RuntimeError("无法确定交易日，请显式传入 date_str=YYYYMMDD")

    job_id = db.job_start(
        kind, json.dumps({"date": target}, ensure_ascii=False),
        trade_date=target)
    results, failed, partial = _collect(target, force=False, scoring=False)
    status = _finish(job_id, target, False, False, results, failed, partial)
    logger.info("轻量抓取结束 status=%s date=%s steps=%d failed=%s",
                status, target, len(results), failed)
    return {"date": target, "status": status, "results": results,
            "failed": failed}


def run_retries(kind: str = "manual",
                domain: Optional[str] = None) -> Dict[str, Any]:
    """把**到点**的重试逐条跑掉。同步、逐条独立 try。

    只处理 `next_retry_at` 已过且未用尽重试次数的行 —— 没到点的一条都不碰，
    否则退避等于白设。每条重跑走的还是 `_collect_domain`，所以它自己会
    把 attempts 再 +1、重新算下一次退避，不需要这里替它记。

    :param domain: 只跑这**一个**数据域（页面「指定数据域补跑」用），空/缺省
        = 全部。筛选必须发生在 `ingest_due()` **之后** —— 先按退避筛出到点的、
        再从中挑域；反过来先挑域再看退避也没关系，但**绝不能**为了「补这个域」
        而把没到点的行捞出来，那等于绕过退避，源挂着的时候会被反复打。

    `checked` 一律是**筛选之后**的条数：按域筛过还报全部到点条数，页面上
    就会写着「应跑 10 条」而实际只跑了 2 条，看着像漏跑。

    **会联网。** 只在页面上「补跑」被点到时才调，不自动触发。
    """
    due = db.ingest_due()
    if domain:
        due = [r for r in due if r["domain"] == domain]
    if not due:
        return {"checked": 0, "ran": 0, "results": []}

    # 补跑也是一轮任务，同样要进 job_run —— 不记的话，事后想知道「10 月 16
    # 日那次补跑到底跑没跑、跑了哪些」就只能翻日志。多条可能跨不同日期，
    # 那就不硬填一个 trade_date（填错比不填更坏）。
    dates = {r["trade_date"] for r in due}
    job_id = db.job_start(
        "retry",
        json.dumps({"domain": domain or "all", "checked": len(due),
                    "dates": sorted(dates)}, ensure_ascii=False),
        trade_date=dates.pop() if len(dates) == 1 else "")

    results: List[Dict[str, Any]] = []
    ran = 0
    for row in due:
        domain, target = row["domain"], row["trade_date"]
        try:
            if domain.startswith("pool_"):
                out = fetch_pool(domain[len("pool_"):], target)
            elif domain.startswith("sector_flow_"):
                out = fetch_sector_flow(domain[len("sector_flow_"):], target)
            elif domain == "main_force":
                out = _score_and_record(target)
            elif domain == "daily_bar":
                out = fetch_daily_bar(target)
            else:
                results.append({"domain": domain, "trade_date": target,
                                "status": "skipped",
                                "reason": "不认识的数据域，未重跑"})
                continue
            ran += 1
            results.append({"domain": domain, "trade_date": target,
                            "status": "ok", "stored": out})
        except db.DomainBusy as exc:
            # 同域同天已经有一轮在跑（页面刷新撞上定时任务）—— 不算失败，
            # 也不计入实跑条数：这一条根本没发请求。
            results.append({"domain": domain, "trade_date": target,
                            "status": "skipped", "reason": str(exc)})
        except Exception as exc:  # noqa: BLE001  一条挂了不能连累其余
            logger.exception("重试失败 %s @ %s", domain, target)
            results.append({"domain": domain, "trade_date": target,
                            "status": "error",
                            "error": f"{type(exc).__name__}: {exc}"})
    errors = [r for r in results if r.get("status") == "error"]
    status = ("failed" if errors and len(errors) == len(due)
              else "partial" if errors else "success")
    db.job_finish(job_id, status, json.dumps(
        {"checked": len(due), "ran": ran, "errors": len(errors)},
        ensure_ascii=False))
    logger.info("采集重试结束：应跑 %d 条，实跑 %d 条，失败 %d 条 status=%s",
                len(due), ran, len(errors), status)
    return {"checked": len(due), "ran": ran, "results": results,
            "status": status}


# ------------------------------------------------------------------ 定时接线


def _cron_fire() -> None:
    """16:30 触发点。非交易日直接跳过，且**日历不可用时宁可不跑** ——
    盲跑会把非交易日的空/错数据写进快照表。"""
    try:
        if not calendar.is_trade_day():
            logger.info("非交易日，跳过盘后任务")
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易日历不可用，跳过本次盘后任务: %s", exc)
        return

    def _worker() -> None:
        try:
            run_post_close(kind="scheduled")
        except Exception:  # noqa: BLE001
            logger.exception("盘后任务异常")

    threading.Thread(target=_worker, name="post-close", daemon=True).start()


def _push_fire() -> None:
    """14:50 触发点：给订阅者推关注基金的实时估值（docs/06 §6）。

    交易日闸与盘后任务**同一口径**：日历拿不到就一律不发。非交易日推估值
    本身就是错的（源回的是上一交易日的估值），日历不可用时宁可不推。
    """
    try:
        if not calendar.is_trade_day():
            logger.info("非交易日，跳过 %02d:%02d 估值推送",
                        settings.push_hour, settings.push_minute)
            return
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易日历不可用，跳过本次估值推送: %s", exc)
        return

    def _worker() -> None:
        try:
            subscribe.run_push()
        except Exception:  # noqa: BLE001
            logger.exception("估值推送异常")

    threading.Thread(target=_worker, name="fund-push", daemon=True).start()


def _retry_fire() -> None:
    """补跑**检查**触发点（17:30 / 次日 09:20）。**只读，零联网。**

    阶段 4 的原文是「在后续时间段检查重试」，这里就把「检查」做满，**但不跑**：

    - 不调 `run_retries` —— AGENTS.md 的硬口径「`POST /api/pipeline/collect/retry`
      是唯一会联网的采集入口，任何定时器都不碰它」2026-10-17 用户拍板保留，
      任务书与口径取交集 = 定时点负责**发现**，跑仍由人在页面点；
    - **不判交易日** —— 周五收盘后失败的采集，周六也该被看见。「今天是不是
      交易日」跟「那批数据还缺不缺」是两件事，到点与否由退避自己算，这里一个
      多余的判据都不加；
    - 结果写进 kv（`_RETRY_CHECK_KEY`），页面据此显示「有几条到点待补跑」和
      「上次检查是什么时候」。
    """
    try:
        due = db.ingest_due()
    except Exception as exc:  # noqa: BLE001  检查挂了不能连累调度器其余触发点
        logger.warning("补跑检查失败: %s", exc)
        return
    domains = sorted({str(r.get("domain") or "") for r in due})
    payload = {
        "checked_at": _now(),          # 本地时间：页面要算「几分钟前」
        "due": len(due),
        "domains": domains,
        "trade_dates": sorted({str(r.get("trade_date") or "") for r in due}),
        # 恒 False：这个触发点一次请求都不发。留成字段而不是只写在注释里，
        # 是为了让「它到底跑没跑」在接口上是一等公民，而不是要人翻源码。
        "ran": False,
    }
    try:
        db.kv_set(_RETRY_CHECK_KEY, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001  记不上就只少一条页面信息
        logger.warning("补跑检查结果写入失败: %s", exc)
    if due:
        logger.warning(
            "补跑检查：%d 条采集到点待补跑（%s），**不会自动跑** —— "
            "需要补请到数据管理页点「补跑重试」。", len(due), "、".join(domains))
    else:
        logger.info("补跑检查：没有到点的重试")


def last_retry_check() -> Optional[Dict[str, Any]]:
    """上一次定时补跑检查的记录。**零联网**，没跑过就 `None`。"""
    raw = db.kv_get(_RETRY_CHECK_KEY)
    if not raw:
        return None
    try:
        out = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return out if isinstance(out, dict) else None


def start() -> None:
    """启动定时接线（FastAPI lifespan 里调）。重复调用无副作用。

    启动前先看 kv 里**两个各自独立**的开关：关掉哪个就只装另一个的 job，
    两个都关则整个不启 —— 不会自作主张地把用户关掉的东西打开。
    """
    global _scheduler
    if _scheduler is not None:
        return
    # 漏采自检放在**开关判断之前**：它跟定时任务开不开无关 —— 库里缺了几天
    # 数据这件事，无论调度关不关都该在启动时被看见。只打日志、零联网、不补跑。
    try:
        ingest.startup_check()
    except Exception as exc:  # noqa: BLE001  自检不该挡住服务启动
        logger.warning("启动漏采自检失败: %s", exc)
    jobs_on = db.kv_get(_ENABLED_KEY, "1") != "0"
    push_on = db.kv_get(_PUSH_ENABLED_KEY, "0") != "0"
    if not jobs_on and not push_on:
        logger.info("盘后任务与估值推送都按上次的设置保持关闭"
                    "（可在「定时任务」页分别开启）")
        return

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    # 不指定 timezone -> 用系统本地时区，与 calendar 里 date.today() 口径一致
    sched = BackgroundScheduler()
    if jobs_on:
        sched.add_job(
            _cron_fire,
            CronTrigger(hour=settings.fetch_hour, minute=settings.fetch_minute),
            id="post_close",
            replace_existing=True,
            coalesce=True,              # 停机期间积压的多次触发合并成一次
            max_instances=1,
            misfire_grace_time=3600,    # 错过窗口 1 小时内仍补跑
        )
        # 阶段 4：后续时间段的补跑**检查**（只读，不外发，见 `_retry_fire`）。
        # 挂在盘后任务**同一个开关**下 —— 关掉定时任务就一个触发点都不装，
        # 不会出现「页面显示关着、其实还有 job 在跑」。补跑的真正外发仍然只有
        # 人点「补跑重试」那一条路。
        for hh, mm in RETRY_CRON:
            sched.add_job(
                _retry_fire,
                CronTrigger(hour=hh, minute=mm),
                id="retry_check_%02d%02d" % (hh, mm),
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                # 检查晚半小时没有意义，但顺延一次无害（下次还有 09:20 那班）
                misfire_grace_time=1800,
            )
    if push_on:
        # 第二个触发点：盘中估值推送。开关与盘后任务**各自独立**（`_PUSH_ENABLED_KEY`），
        # 但共用这一个 scheduler —— 推送不该为「盘后任务关着」而跑不起来。
        sched.add_job(
            _push_fire,
            CronTrigger(hour=settings.push_hour, minute=settings.push_minute),
            id="fund_push",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            # 盘中估值过点就没什么意义了（15:00 收盘），错过 10 分钟内还值得补
            misfire_grace_time=600,
        )
    sched.start()
    _scheduler = sched
    logger.info(
        "定时接线已启动：%s%s%s",
        ("盘后任务 %02d:%02d" % (settings.fetch_hour, settings.fetch_minute))
        if jobs_on else "盘后任务关闭",
        ("，补跑检查 %s" % "、".join("%02d:%02d" % (hh, mm)
                                    for hh, mm in RETRY_CRON))
        if jobs_on else "",
        ("，估值推送 %02d:%02d" % (settings.push_hour, settings.push_minute))
        if push_on else "，估值推送关闭",
    )
    if jobs_on:
        _warn_if_missed_today()
    if push_on:
        _warn_if_missed_push()


def _warn_if_missed_today() -> None:
    """启动时已过 16:30 且当天没打过分 —— 只提醒，不自动补跑。"""
    now = datetime.now()
    cutoff = now.replace(hour=settings.fetch_hour, minute=settings.fetch_minute,
                         second=0, microsecond=0)
    if now <= cutoff:
        return
    try:
        if not calendar.is_trade_day():
            return
    except Exception:  # noqa: BLE001
        return
    today = now.strftime("%Y%m%d")
    if db.latest_date("main_force") == today:
        return
    logger.warning(
        "现在已过 %02d:%02d，但 %s 尚未打分。调度只会在下一个交易日的该时刻触发；"
        "需要现在补跑请调 POST /api/jobs/run（或总览页的「立即执行」）。",
        settings.fetch_hour, settings.fetch_minute, today,
    )


def _warn_if_missed_push() -> None:
    """启动时已过 14:50、今天一个都还没推成 —— **只提醒，不补发**。

    口径与 `_warn_if_missed_today` 一致：未经告知不主动做事。盘中估值错过
    时点后补推，推的也只是已经过时的数字，反而误导。
    """
    now = datetime.now()
    cutoff = now.replace(hour=settings.push_hour, minute=settings.push_minute,
                         second=0, microsecond=0)
    if now <= cutoff:
        return
    try:
        if not calendar.is_trade_day():
            return
    except Exception:  # noqa: BLE001
        return
    if db.push_log_today(subscribe.PUSH_KIND):
        return
    logger.warning(
        "现在已过 %02d:%02d，但今天的估值推送还没发过。调度会在下一个交易日"
        "的该时刻触发；今天要补发可调订阅管理页的「立即推送」。",
        settings.push_hour, settings.push_minute,
    )


def stop() -> None:
    """关闭调度（FastAPI lifespan 退出时调）。"""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception as exc:  # noqa: BLE001
        logger.warning("调度关闭异常: %s", exc)
    _scheduler = None


def _reconcile() -> None:
    """两个开关与 job 是「多对一」（一个 scheduler 装两个 job），任一边变了
    都要重建 —— 否则会出现「页面上显示开着、其实一个 job 都没挂」，
    或者「关了盘后，推送也跟着没了」。
    """
    stop()
    start()


def set_enabled(enabled: bool) -> Dict[str, Any]:
    """开/关**盘后任务**（16:30 抓取 + 打分）。选择持久化，重启仍生效。"""
    db.kv_set(_ENABLED_KEY, "1" if enabled else "0")
    _reconcile()
    logger.info("盘后任务已%s（每交易日 %02d:%02d）",
                "开启" if enabled else "关闭",
                settings.fetch_hour, settings.fetch_minute)
    return schedule_info()


def set_push_enabled(enabled: bool) -> Dict[str, Any]:
    """开/关 **14:50 估值推送**。与盘后任务**互不影响** —— 推送只是发一条
    消息，不该因为盘后任务关着就连带停掉。"""
    db.kv_set(_PUSH_ENABLED_KEY, "1" if enabled else "0")
    _reconcile()
    logger.info("估值推送已%s（每交易日 %02d:%02d）",
                "开启" if enabled else "关闭",
                settings.push_hour, settings.push_minute)
    return schedule_info()


def schedule_info() -> Dict[str, Any]:
    """给总览页展示：几点触发、有没有开、下一次什么时候。"""
    info: Dict[str, Any] = {
        # 两个开关**各自独立** —— 合并成一个 `enabled` 会让「关掉盘后任务」
        # 顺带把推送的开关状态也显示成关，页面上就对不上了。
        "enabled": _scheduler is not None
        and db.kv_get(_ENABLED_KEY, "1") != "0",
        "push_enabled": _scheduler is not None
        and db.kv_get(_PUSH_ENABLED_KEY, "0") != "0",
        "hour": settings.fetch_hour,
        "minute": settings.fetch_minute,
        "next_run": None,
        # 第二个触发点：盘中估值推送（开关独立，共用同一个 scheduler）
        "push_hour": settings.push_hour,
        "push_minute": settings.push_minute,
        "next_push": None,
        # 阶段 4：补跑**检查**触发点。`retry_points` 恒给（那是设计），下面的
        # `next_retry_check` 只有装上 job 才有值 —— 两个都关时为 None，页面据此
        # 说「这个检查没在跑」，而不是默认它一直在跑。
        "retry_points": ["%02d:%02d" % (hh, mm) for hh, mm in RETRY_CRON],
        "retry_check": last_retry_check(),
        "next_retry_check": None,
    }
    if _scheduler is None:
        return info
    try:
        job = _scheduler.get_job("post_close")
        nxt = getattr(job, "next_run_time", None) if job else None
        info["next_run"] = nxt.isoformat() if nxt else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("取下一次触发时间失败: %s", exc)
    try:
        job = _scheduler.get_job("fund_push")
        nxt = getattr(job, "next_run_time", None) if job else None
        info["next_push"] = nxt.isoformat() if nxt else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("取下一次推送时间失败: %s", exc)
    try:
        # 两个触发点取**较早**的那一个 —— 给一个「下一次检查」就要是真的下一次，
        # 报成 09:20 而 17:30 还在前面，页面上的倒计时就是错的。
        nxts = []
        for hh, mm in RETRY_CRON:
            job = _scheduler.get_job("retry_check_%02d%02d" % (hh, mm))
            t = getattr(job, "next_run_time", None) if job else None
            if t is not None:
                nxts.append(t)
        info["next_retry_check"] = min(nxts).isoformat() if nxts else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("取下一次补跑检查时间失败: %s", exc)
    return info
