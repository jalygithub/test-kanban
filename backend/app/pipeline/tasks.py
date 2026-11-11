# -*- coding: utf-8 -*-
"""六个每日任务（任务书阶段 3）—— 统一入口，复用既有实现。

任务书点名的六个，一个不多一个不少：

==================  ========  ==============  ==================================
任务                  联网      落库            说明
==================  ========  ==============  ==================================
daily_market_sync     是        daily_market     全市场行情快照（整份落库）
daily_fund_flow_sync  是        fund_flow_rank   批量排名全量 + 各窗口覆盖数
daily_universe_sync   否        daily_universe   全市场名单（并集口径，已拍板）
data_quality_check    否        只补 detail      日期窗口覆盖与缺口，零联网
recovery_sync         是        —                到点补跑（scheduler.run_retries）
historical_backfill   是        daily_bar 等     人工确认后的历史回填
==================  ========  ==============  ==================================

**复用，不重建**。本模块不写任何取数逻辑，只做三件事：

1. ``run_task()`` 统一入口：先定交易日 → 起 ``job_run`` → 计时 → 收异常；
2. 统一返回形状 ``{task_id, trade_date, status, ...}``，页面和调度器一份解析；
3. 统一登记：落库的域当天在 ``ingest_job`` 里有没有记录，有确定答案。

**惰性导入 scheduler**：``recovery_sync`` 要用 ``scheduler.run_retries``，而
阶段 4 的调度器反过来要调本模块 —— 模块级互相 import 会成环。函数内导入在本
仓库已有先例（``routes.py`` 里的 ``coverage`` / ``backfill``）。
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.fetchers import calendar as tcal
from app.fetchers import quotes
from app.pipeline import backfill, coverage, ingest
from app.store import db

logger = logging.getLogger(__name__)

#: 批量资金流排名的四个窗口。**只有「今日」整份落库** —— 另外三个窗口是同
#: 一批票的另一种聚合，四份全存等于同一天写 4 倍的量；它们的**条数**记进
#: ``coverage_detail.windows``，够回答「哪个窗口只回了半张榜」。
RANK_INDICATORS = ("今日", "3日", "5日", "10日")

#: 全市场快照条数下限。A 股 5,400 余只；低于 3,000 就不是「今天少了几只」，
#: 而是源只回了前 N 条（`stock_zh_a_spot_em` 在网络差时会截断）。宁可判
#: PARTIAL 让人看一眼，也不拿半份快照去算全市场排名。
MIN_MARKET_ROWS = 3000

#: 资金流榜的覆盖下限：不低于名单数的这个比例才算采全。与覆盖审计「低于峰值
#: 60% 判薄」同一个口径，不另造一个数。
RANK_COVERAGE_FLOOR = 0.6


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _reject_extra(params: Dict[str, Any], allowed: Sequence[str] = ()) -> None:
    """不接受白名单外的参数 —— **不静默忽略**。

    静默吞掉一个拼错的 ``budgt``，等于把「这次只许发 50 次请求」变成「不限量」，
    后果是几千次外发。宁可报错，也不替调用方猜意图。
    """
    extra = sorted(set(params) - set(allowed))
    if extra:
        raise ValueError(f"任务不接受这些参数：{'、'.join(extra)}")


def _source_of(rows: Sequence[Dict[str, Any]]) -> str:
    """这些行是哪儿来的。混采就如实写 ``mixed`` —— 不挑一个好看的。"""
    srcs = sorted({str(r.get("source") or "").strip()
                   for r in rows if isinstance(r, dict)} - {""})
    if not srcs:
        return ""
    return srcs[0] if len(srcs) == 1 else "mixed"


def _universe_size() -> Optional[int]:
    """全市场名单条数。**没跑过 `daily_universe_sync` 就是 None，不是 0** ——

    拿 0 当分母算不出覆盖率，更糟的是拿它当 1 会让任何一份快照都显得「全覆盖」。
    """
    latest = db.latest_date("daily_universe")
    if not latest:
        return None
    rows = db.load("daily_universe", latest)
    return len(rows or [])


# ------------------------------------------------------------ 六个任务本体
#
# 每个任务的签名都是 ``(trade_date, **params)``：交易日由 `run_task` 统一定，
# 任务内部不再各自推算 —— 六个任务各算一遍今天的日期，周末跑必然各说各话。


def _collect(domain: str, trade_date: str,
             fetch: Callable[[], Tuple[List[Dict[str, Any]],
                                       Optional[Dict[str, Any]]]],
             **rec: Any) -> Dict[str, Any]:
    """RUNNING → 取数 → 落终态。**抛异常也要落终态。**

    少了置位这一步，`ingest.record` 里那条 `UPDATE` 会 0 行命中 —— **状态一个
    字都写不进去**，页面上这一域永远空着，而任务那边却显示跑过了（`record`
    刻意不自己置 RUNNING，见它的 docstring：置位必须发生在**发请求之前**，
    抓取期间那一栏才显示「正在抓取」）。

    异常路径同理：取数抛了就记 `record_failure`（按剩余次数退 WAITING 或
    FAILED）再上抛，否则那一行会永久停在 RUNNING，直到 30 分钟后被当成僵死接管。
    """
    db.ingest_start(domain, trade_date)
    try:
        rows, extra = fetch()
    except Exception as exc:  # noqa: BLE001
        ingest.record_failure(domain, trade_date, exc)
        raise
    rec.setdefault("source", _source_of(rows))
    if extra:
        rec["coverage_extra"] = extra
    return ingest.record(domain, trade_date, rows, **rec)


def daily_market_sync(trade_date: str, **params: Any) -> Dict[str, Any]:
    """全市场行情快照 → `snapshot('daily_market')`。**会联网**。

    必填只收 ``code``：停牌股没有最新价是**合法缺失**（补 0 才是造假），
    「有价格却是 0 或负」这类坏数据由 `ingest.validate_rows` 的行级校验去抓。
    """
    _reject_extra(params)
    return _collect("daily_market", trade_date,
                    lambda: (quotes.market_snapshot(), None),
                    required=("code",), min_rows=MIN_MARKET_ROWS)


def daily_fund_flow_sync(trade_date: str, **params: Any) -> Dict[str, Any]:
    """批量资金流排名 → `snapshot('fund_flow_rank')`。**会联网**（4 次请求）。

    任务书两条在这里落地：

    - **优先批量排名**：四次 `fund_flow_rank` 拿全市场，不是逐股请求；
    - **只回前 N 必须记覆盖范围**：`coverage_detail` 里给出名单数、返回数、
      两者之比 —— 是显式数字，不再靠行数反推。
    """
    _reject_extra(params)
    # 名单只读一次：一次任务里读两遍同一件事，第二次的值要是变了（另一个人在
    # 同一秒跑了名单任务），覆盖数与门槛就会来自两个时刻。
    universe = _universe_size()

    def _grab() -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
        counts: Dict[str, int] = {}
        today: List[Dict[str, Any]] = []
        for indicator in RANK_INDICATORS:
            rows = quotes.fund_flow_rank(indicator)
            counts[indicator] = len(rows)
            if indicator == "今日":
                today = rows
        return today, {
            "windows": counts,
            "universe": universe,
            # 名单没跑出来就 None：只有 0 行这一条兜底判据，不凭空设门槛。
            "rank_coverage": (len(today) / universe) if universe else None,
        }

    return _collect("fund_flow_rank", trade_date, _grab,
                    required=("code",),
                    min_rows=int(universe * RANK_COVERAGE_FLOOR)
                    if universe else None)


def daily_universe_sync(trade_date: str, **params: Any) -> Dict[str, Any]:
    """全市场名单 → `snapshot('daily_universe')`。**零联网**，读本地库。

    口径（2026-10-17 已拍板）：`stock_net_inflow` 全部归档日的代码**并集**，
    即 `backfill.load_scope("all")` —— 回填 plan 用的也是它，两处不许各算一遍。

    已知代价：从没上过资金流榜的票不会进名单。这不是这一层能修的（要修得改
    名单来源，属于另一件事），先如实记在这里。
    """
    _reject_extra(params)
    return _collect(
        "daily_universe", trade_date,
        lambda: ([{"code": c} for c in backfill.load_scope("all")],
                 {"scope": "all",
                  "basis": "stock_net_inflow 全部归档日代码并集"}),
        source="union:stock_net_inflow", required=("code",))


def data_quality_check(trade_date: str, **params: Any) -> Dict[str, Any]:
    """数据质量检查 → 把日期窗口的覆盖事实写回 `ingest_job`。**零联网**。

    只补 `coverage_detail.window`，**不碰 `coverage_rate`**：那一列记的是采集
    那一步的字段校验通过率（见 `ingest.record`），日期窗口的覆盖率是另一件
    事，共用一列会让同一个字段在不同时刻代表不同含义。

    没有 (domain, trade_date) 采集记录的域如实记进 ``skipped`` —— 它没有可
    依附的行，凭空插一条「成功」是编造。
    """
    _reject_extra(params)
    report = coverage.coverage_report()
    datasets: Dict[str, Any] = report.get("datasets") or {}
    updated: List[str] = []
    skipped: List[str] = []
    states: Dict[str, Any] = {}
    for name, facts in datasets.items():
        states[name] = facts.get("state")
        row = db.ingest_get(name, trade_date)
        if not row:
            skipped.append(name)
            continue
        try:
            detail = json.loads(row["coverage_detail"]) if row.get(
                "coverage_detail") else {}
        except (TypeError, ValueError):
            detail = {}
        detail["window"] = {
            "coverage_rate": facts.get("coverage_rate"),
            "expected_days": facts.get("expected_days"),
            "missing_dates": facts.get("missing_dates"),
            "thin_days": facts.get("thin_days"),
            "unreadable_days": facts.get("unreadable_days"),
            "state": facts.get("state"),
            "first_date": facts.get("first_date"),
            "last_date": facts.get("last_date"),
            "checked_at": _now(),
        }
        if db.ingest_set_coverage(name, trade_date, row.get("coverage_rate"),
                                  detail):
            updated.append(name)
    return {"datasets": len(datasets), "updated": updated,
            "skipped": skipped, "states": states, "offline": True,
            "calendar_stale": (report.get("calendar") or {}).get("stale")}


def recovery_sync(trade_date: str, domain: str = "",
                  **params: Any) -> Dict[str, Any]:
    """到点补跑 → `scheduler.run_retries`。**会联网**（只跑已到点的）。

    ``trade_date`` 这里**故意不用**：重试各自认自己的交易日（库里 `next_retry_at`
    指哪天补哪天），拿统一入口算出来的日期去盖它，会把「上周三漏的」记成今天。
    形参留着只为六个任务签名一致 —— 这句写在这里，别当成漏用了。
    """
    _reject_extra(params, ("domain",))
    from app import scheduler   # 惰性导入，见模块 docstring
    return scheduler.run_retries(kind="manual", domain=domain or None)


def historical_backfill(trade_date: str, domain: str = "", start: str = "",
                        end: str = "", items: Any = None,
                        budget: Any = None, only_gaps: Any = False,
                        **params: Any) -> Dict[str, Any]:
    """历史回填 → `backfill.backfill()`。**会联网，且是几千次请求量级**。

    只在人明确点了「开始回填」时才走这条（页面按钮与本端点都归到它身上，
    不是第二套实现）。

    ``end`` 的缺省是**最新交易日**，不是 `DEFAULT_START` —— 后者是**起始日**，
    拿它当结束日会让「只填 start」变成一段方向反了的区间。

    ``only_gaps``（阶段 5）= 只补缺口：逐票比对已有交易日，中间断档的段单独
    重试，一天不缺就一次请求都不发。给「东财被拦的那几天」用 —— 整段重跑是
    杀鸡用牛刀，还会白打几千次请求。
    """
    _reject_extra(params, ("domain", "start", "end", "items", "budget",
                           "only_gaps"))
    if not domain:
        raise ValueError("历史回填必须指定数据域（daily_bar / index / sector_hist）")
    codes = [str(c) for c in (items or [])]
    return backfill.backfill(domain, start or backfill.DEFAULT_START,
                             end or trade_date, items=codes, budget=budget,
                             only_gaps=bool(only_gaps))


#: 六个任务的注册表。``offline`` 是**这个任务联网与否**，不是「整个服务」——
#: 联网白名单（`_accept_check`）按「路由可能联网」从严算，两者不要混。
TASKS: Dict[str, Dict[str, Any]] = {
    "daily_market_sync": {
        "fn": daily_market_sync, "offline": False,
        "label": "全市场行情快照",
        "note": "整份快照落库，含流通市值/换手率（日线里没有的字段）。",
    },
    "daily_fund_flow_sync": {
        "fn": daily_fund_flow_sync, "offline": False,
        "label": "资金流批量排名",
        "note": "四个窗口各一次批量请求，全量落「今日」榜并记各窗口条数。",
    },
    "daily_universe_sync": {
        "fn": daily_universe_sync, "offline": True,
        "label": "全市场名单",
        "note": "stock_net_inflow 全归档日并集（已拍板口径），零联网。",
    },
    "data_quality_check": {
        "fn": data_quality_check, "offline": True,
        "label": "数据质量检查",
        "note": "扫日期窗口缺口写回 coverage_detail.window，零联网。",
    },
    "recovery_sync": {
        "fn": recovery_sync, "offline": False,
        "label": "到点补跑",
        "note": "只跑 next_retry_at 已到点的域；走 run_retries 原逻辑。",
    },
    "historical_backfill": {
        "fn": historical_backfill, "offline": False,
        "label": "历史数据回填",
        "note": "需显式域与区间，几千次请求量级，只在人点「开始回填」时跑。",
    },
}


# --------------------------------------------------------------- 统一入口


def _offline_trade_date() -> Optional[str]:
    """今天该跑哪天 —— **只读本地，绝不联网**。给 `GET /pipeline/tasks` 用。

    日历走 `cached_trade_dates()`（名字里就写着 cached：过期也只读，不去新浪
    拉）。GET 端点为了「今天是几号」发一次外发，会被公司安全设备记一笔，而
    「GET /pipeline/* 一律零联网」正是验收 B3 盯着的那条线。

    缓存缺失/过期时退到库内最新日期。**真跑**（POST）那侧另有入口、允许刷新
    日历，所以这里停在哪一天只影响**页面显示**，不影响实际跑的日期。
    """
    today = date.today().strftime("%Y%m%d")
    dates = [d for d in tcal.cached_trade_dates() if d <= today]
    if dates:
        return dates[-1]
    for dataset in ("fund_flow_rank", "stock_net_inflow", "pool_zt"):
        d = db.latest_date(dataset)
        if d:
            return d
    return None


def _resolve_trade_date() -> Optional[str]:
    """真跑时的交易日：交给 `scheduler.target_date()` 一处推算，不在这再写一遍。

    它允许日历过期时去刷新一次 —— POST 侧是人点按钮，与 `run_post_close` /
    `run_light` 同一个口径；GET 侧走 `_offline_trade_date()`，两处刻意不同。
    """
    from app import scheduler   # 惰性导入，见模块 docstring
    return scheduler.target_date()


def _brief(params: Dict[str, Any]) -> Dict[str, Any]:
    """塞进 `job_run.detail` 的参数快照：大名单只记条数。

    把 4,813 个代码写进 detail，那一行 JSON 会膨胀到几十 KB，而它要回答的是
    「那次跑的是什么参数」，不是「名单里第 3000 只是谁」。
    """
    return {k: (f"{len(v)} 项" if isinstance(v, (list, tuple, set, dict))
                else v)
            for k, v in params.items()}


def _summary(result: Any) -> Dict[str, Any]:
    """任务结果里值得进 `job_run.detail` 的数。整份 payload 另有 snapshot 存。"""
    if not isinstance(result, dict):
        return {"result_type": type(result).__name__}
    keep = ("status", "processed", "stored", "failed_rows", "coverage_rate",
            "finished", "requested", "ran", "skipped", "fetched", "inserted",
            "failed", "retried", "success", "updated", "datasets",
            # 阶段 5：回填收尾的覆盖审计、源计数、缺口模式与暂停标记 —— 落进
            # job_run.detail 才能事后回答「那次到底是谁在拒、覆盖到哪了」
            "coverage", "factor_coverage", "sources", "only_gaps", "paused")
    return {k: result[k] for k in keep if k in result}


def _status_of(result: Any) -> str:
    """任务结果 → `job_run.status`。**沿用 `_finish` 那套词**，不另造。

    ``partial`` 的意思是「跑成了但数据不能信」—— 与 `ingest.PARTIAL`、回填的
    ``finished=false``、带 failed 计数是同一个意思。

    **大小写两头都认**：`ingest.record` 回 `PARTIAL`/`FAILED`，`scheduler.
    run_retries` 回 `partial`/`failed`（`job_run` 那套小写词）。原先只认大写，
    又去数一个 `run_retries` **根本不返回**的 `retried` 键，于是补跑全挂在任务
    表里永远显示 success —— 接口跑挂了而库里记成功，不允许（阶段 4 修）。
    """
    if isinstance(result, dict):
        st = result.get("status")
        if st in (ingest.PARTIAL, "partial"):
            return "partial"
        if st in (ingest.FAILED, "failed"):
            return "failed"
        if "finished" in result and "requested" in result:
            return ("success"
                    if result.get("finished") and not result.get("failed")
                    else "partial")
        if "checked" in result and "ran" in result:
            # `run_retries` 一条都没到点时的返回（`{checked:0, ran:0}`）没有
            # status：一个都没跑不叫失败，success 是对的。
            return "partial" if result.get("failed") else "success"
    return "success"


def run_task(task_id: str, **params: Any) -> Dict[str, Any]:
    """跑一个任务，起 `job_run`、计时、收异常。六个任务共用这一个入口。

    :param trade_date: 缺省由 `scheduler.target_date()` 定（今天是交易日取今天，
        否则取最近交易日 —— 周末手动触发也能落在真实交易日上）。
    :return: ``{task_id, trade_date, status, started_at, finished_at,
                elapsed_sec, offline, job_id, result}``

    **异常不吞**：失败照样 `job_finish(failed)` 再抛出去，让调用方（页面/调度器）
    拿到 4xx/5xx；任务状态在库里已经是失败，不会出现「接口报错、页面显示成功」。
    """
    spec = TASKS.get(task_id)
    if spec is None:
        raise ValueError(
            f"未注册的任务 {task_id}（可选：{'、'.join(sorted(TASKS))}）")
    job_params = dict(params)
    trade_date = str(job_params.pop("trade_date", "") or "") or \
        _resolve_trade_date()
    if not trade_date:
        raise RuntimeError(
            "无法确定交易日：交易日历不可用且库内没有可用日期")

    job_id = db.job_start(
        task_id,
        json.dumps({"trade_date": trade_date, **_brief(job_params)},
                   ensure_ascii=False, default=str),
        trade_date=trade_date)
    started = _now()
    t0 = time.monotonic()
    try:
        result = spec["fn"](trade_date, **job_params)   # type: ignore[operator]
    except BaseException as exc:  # noqa: BLE001  要连 KeyboardInterrupt 都记下
        db.job_finish(job_id, "failed", f"{type(exc).__name__}: {exc}")
        raise
    elapsed = round(time.monotonic() - t0, 1)
    status = _status_of(result)
    db.job_finish(job_id, status,
                  json.dumps({"elapsed_sec": elapsed, **_summary(result)},
                             ensure_ascii=False, default=str))
    if status != "success":
        logger.warning("任务 %s @ %s 判 %s（%.1fs）", task_id, trade_date,
                       status, elapsed)
    return {"task_id": task_id, "trade_date": trade_date, "status": status,
            "started_at": started, "finished_at": _now(),
            "elapsed_sec": elapsed, "offline": spec["offline"],
            "job_id": job_id, "result": result}


def status_report() -> Dict[str, Any]:
    """六个任务的定义 + 各自最近一次 `job_run`。**零联网，只读任务表。**"""
    tasks: List[Dict[str, Any]] = []
    for task_id, spec in TASKS.items():
        rows = db.jobs(limit=1, kind=task_id)
        last = rows[0] if rows else None
        tasks.append({
            "task_id": task_id,
            "label": spec["label"],
            "note": spec["note"],
            "offline": spec["offline"],
            "last": ({k: last.get(k) for k in
                      ("id", "status", "trade_date", "started_at",
                       "finished_at", "detail")}
                     if last else None),
        })
    # 交易日用**离线**那一版推（见 `_offline_trade_date`）：本端点是 GET，
    # 一次外发都不许有。
    return {"tasks": tasks, "trade_date": _offline_trade_date(),
            "offline": True}
