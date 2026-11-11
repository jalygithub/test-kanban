"""数据覆盖审计 —— **共享事实层**。

本模块只回答「库里有什么、采到哪天、上次什么时候、错在哪、缺多少」，
**不做「能不能回测」的判定**。

分工（2026-10-16 用户拍板：做成共享，不写两套）：

    app/pipeline/coverage.py   ← 事实（本模块）
    app/research/audit.py      ← 在这些事实上做「可回测性」二次判定

这样「最近一次错误」「缺失字段数」这类数字只有一处来源，不会出现
研究页说 3 天、采集页说 4 天这种对不上的事。

两条铁律：

1. **零联网。** 交易日历只读本地缓存文件，**绝不调用接口刷新** ——
   缓存过期就返回过期值并在报告里标注，也不去新浪拉一份新的。
   内网环境下任何外发都会被公司安全设备拦截并留下记录。
2. **缺失写 ``None``，绝不写 ``0``。** ``0`` 在计数上是「一个都没有」，
   在覆盖率上是「全缺」，在字段上是「值就是零」——三件事共用一个数字，
   页面上根本分不清。本模块一律用 ``None`` 表示「不知道 / 没有记录」。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.config import DATA_DIR, settings

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 五态
#
# 任务书要求必须能区分这五种情况。它们在页面上长得像，后果完全不同：
# 「没跑任务」是没人管，「源挂了」是要修网络，「部分股票」是要怀疑数据本身。
# 混成一个「失败」，人就不知道该干什么。

#: 没有采集任务运行过 —— 数据缺失的原因是**没人采**
NO_JOB = "NO_JOB"
#: 任务跑过了，但一条数据都没拿到 —— 源返回空 / 全被过滤
NO_DATA = "NO_DATA"
#: 数据源请求失败（有错误记录）—— 源挂了、被拦、或口径变了
SOURCE_DOWN = "SOURCE_DOWN"
#: 只覆盖了部分日期或部分股票 —— 数据在，但**不完整**
PARTIAL = "PARTIAL"
#: 完整且通过字段校验
OK = "OK"

STATE_RANK = {OK: 0, PARTIAL: 1, NO_DATA: 2, SOURCE_DOWN: 3, NO_JOB: 4}


def worst_state(*states: str) -> str:
    """取最差状态。`OK` 不会盖掉别的 —— 一天缺就是缺。"""
    return max(states, key=lambda s: STATE_RANK.get(s, 3))


# ------------------------------------------------------------ 数据域定义
#
# `required` 是**判完整性用的最小字段集**：缺了它们，这一行就算不上一条
# 有效的该类记录。不是"完整 schema"，是"缺了就不能信"的那几个。

#: 快照里 payload 的形态。`list` = 行列表；`envelope` = 单个包（如 main_force）
_SNAP_KINDS: Dict[str, str] = {
    "pool_zt": "list",
    "pool_zb": "list",
    "pool_dt": "list",
    "pool_strong": "list",
    "stock_net_inflow": "list",
    "sector_flow_industry": "list",
    "sector_flow_concept": "list",
    "main_force": "envelope",
    # 阶段 3 六任务里的三个落库域。登记进来才会进覆盖审计与 `gaps()` ——
    # 不登记的话，「这个域根本没在采」这件事在页面上根本看不见。
    "daily_market": "list",         # daily_market_sync 全市场快照
    "fund_flow_rank": "list",       # daily_fund_flow_sync 批量排名全量
    "daily_universe": "list",       # daily_universe_sync 全市场名单
}

_REQUIRED: Dict[str, Tuple[str, ...]] = {
    "pool_zt": ("code", "name", "price", "pct_chg"),
    "pool_zb": ("code", "name", "price", "pct_chg"),
    "pool_dt": ("code", "name", "price", "pct_chg"),
    "pool_strong": ("code", "name", "price", "pct_chg"),
    "stock_net_inflow": ("code", "net_inflow"),
    "sector_flow_industry": ("name", "net_inflow", "pct_chg"),
    "sector_flow_concept": ("name", "net_inflow", "pct_chg"),
    # main_force 的 payload 是包（`[{rows:[...], all_rows:[...]}]`），
    # 而 `rows_of` 已经把内层 rows 拆出来了 —— 所以这里验的是**内层每行**
    # 该有什么，不能写 `rows`（那键在外层，内层行上永远没有，会全判缺失）。
    "main_force": ("code", "score"),
    # 这三个域的必填只写 `code`，与 ingest 那侧的闸门**必须一字不差**：
    # 这边要求 price/main_net、那边不收，于是「采集判 SUCCESS、审计判 PARTIAL」
    # —— 同一件事两个结论，比缺字段本身更难查。合法缺失（停牌股没有最新价）
    # 不是坏数据，坏数据是「有值却 <=0」，那条由 ingest 的行级校验去抓。
    "daily_market": ("code",),
    "fund_flow_rank": ("code",),
    "daily_universe": ("code",),
}

#: dataset → 生成它的那个采集步骤名（对应 `_collect` 里的 step，
#: 也对应 `job_run.detail.results` 的键）。没有这一步的 dataset 不列。
_STEP_OF: Dict[str, str] = {
    "pool_zt": "pool_zt",
    "pool_zb": "pool_zb",
    "pool_dt": "pool_dt",
    "pool_strong": "pool_strong",
    "sector_flow_industry": "sector_industry",
    "sector_flow_concept": "sector_concept",
    "main_force": "main_force",
    # stock_net_inflow 是 mainforce.run() 顺带落的，没有独立步骤
    "stock_net_inflow": "main_force",
    # 阶段 3 的三个域各自就是一个任务，步骤名 = 任务名（页面点「补这个域」
    # 时要能直接跳到对应任务，不是跳到一个不存在的流水线步骤）
    "daily_market": "daily_market_sync",
    "fund_flow_rank": "daily_fund_flow_sync",
    "daily_universe": "daily_universe_sync",
}

#: 这些域的「每天该有多少条」没有固定基准（涨停数天然逐日不同），
#: 所以**只能按日期缺口判完整性，不能按条数判**。硬按条数判会把
#: 「那天真的只有 7 个涨停」误报成数据缺失。
_COUNT_UNSTABLE = frozenset({
    "pool_zt", "pool_zb", "pool_dt", "pool_strong",
    "sector_flow_industry", "sector_flow_concept", "main_force",
})

#: 逐日条数低于历史峰值的这个比例就算「只覆盖了部分股票」。
#: 0.6 是经验值：实测 stock_net_inflow 正常日在 4463~4782（峰值比 0.93），
#: 而 20261008 只有 1143（0.24）、20260929 只有 30（0.006），
#: 中间没有天然断层，0.6 能干净地切开。
_PARTIAL_RATIO = 0.6

_DATE_COLS = ("trade_date", "date", "day", "candle_date")


# ------------------------------------------------------------- 只读工具


def open_ro(db_path: Optional[str] = None) -> sqlite3.Connection:
    """以 `mode=ro` 打开库。**审计只读，绝不写生产库。**"""
    path = Path(db_path or settings.db_path).resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _q(ident: str) -> str:
    return '"' + ident.replace('"', '""') + '"'


def _tables(conn: sqlite3.Connection) -> List[str]:
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def _iso_to_ymd(value: Optional[str]) -> Optional[str]:
    """`2026-10-15T07:34:11+00:00` → `20261015`；解析不出返回 None。"""
    if not value:
        return None
    try:
        return str(value)[:10].replace("-", "")
    except Exception:  # noqa: BLE001
        return None


def cached_trade_dates() -> Tuple[List[str], bool]:
    """本地交易日历 `(dates, stale)`。

    **绝不联网。** 缓存超过 30 天就算过期（`stale=True`），但**照样返回
    过期数据** —— 宁可用一份旧日历把「漏采检测」做得没那么准，也不要为了
    拿一份新日历在内网里留下一次外发记录。调用方据此在报告上标注。
    """
    path = DATA_DIR / "trade_calendar.json"
    try:
        if not path.exists():
            return [], True
        stale = (time.time() - path.stat().st_mtime) > 30 * 86400
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return [], True
        return sorted({str(d) for d in data}), stale
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取交易日历缓存失败: %s", exc)
        return [], True


# ------------------------------------------------------------ payload 解析


def _rows_of(dataset: str, payload: str) -> List[Dict[str, Any]]:
    """把一条 payload 归一成行列表。

    `main_force` 是包（`[{rows: [...], all_rows: [...]}]`），其余是裸行列表。
    解析不出来返回空列表 —— 调用方据此记 `missing_fields`，**不猜、不补**。
    """
    try:
        obj = json.loads(payload)
    except (TypeError, ValueError):
        return []
    if _SNAP_KINDS.get(dataset) == "envelope":
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            inner = obj[0]
            if isinstance(inner.get("rows"), list):
                return [r for r in inner["rows"] if isinstance(r, dict)]
        return []
    if isinstance(obj, list):
        return [r for r in obj if isinstance(r, dict)]
    return []


def _missing_field_count(dataset: str, rows: Iterable[Dict[str, Any]]
                         ) -> Tuple[int, int]:
    """返回 `(总行数, 缺必填字段的行数)`。"""
    req = _REQUIRED.get(dataset, ())
    total = bad = 0
    for row in rows:
        total += 1
        if any(row.get(k) is None for k in req):
            bad += 1
    return total, bad


# ------------------------------------------------------------ 逐域事实


def dataset_facts(conn: sqlite3.Connection, dataset: str,
                  calendar: Sequence[str]) -> Dict[str, Any]:
    """一个数据域的覆盖事实。**全部字段都可能是 None** —— 没记录就是没记录。"""
    facts: Dict[str, Any] = {
        "dataset": dataset,
        "first_date": None,
        "last_date": None,
        "dates": [],
        "distinct_dates": 0,
        "rows_total": 0,
        "missing_field_rows": 0,
        # 声称有条数、payload 却解析出 0 行 —— 落库后内容坏了 / 格式变了。
        # 这类日子**不能算完整**，但也不属于「日期缺口」，单独列。
        "unreadable_days": [],
        "coverage_rate": None,          # 日期覆盖率
        "expected_days": None,          # 分母
        "missing_dates": [],
        "calendar_stale": None,
        "last_success_at": None,        # 最近一次成功采集
        "per_day_items": {},            # 日期 → 条数（用于「部分股票」判定）
        "item_peak": None,
        "thin_days": [],                # 条数明显偏低的日期
        "has_history": False,
    }

    # 注意这里判的是**表名** `snapshot` 存不存在，不是 dataset 名 ——
    # dataset 是表里的一个值（'pool_zt' 这种），拿它去比表名必然判无。
    if "snapshot" not in _tables(conn):
        return facts

    # 'latest' 是字面量占位键，不是交易日 —— 板块资金流就存成了这样。
    # 把它单独记出来，否则「最新日期 = latest」会让覆盖率算出个鬼数字。
    placeholder = conn.execute(
        "SELECT COUNT(*) FROM snapshot WHERE dataset=? AND trade_date='latest'",
        (dataset,)).fetchone()[0]

    rows = list(conn.execute(
        "SELECT trade_date, row_count, fetched_at, payload FROM snapshot "
        "WHERE dataset=? AND trade_date<>'latest' ORDER BY trade_date", (dataset,)))
    if placeholder:
        facts["placeholder_latest"] = placeholder

    if not rows:
        return facts

    dates = [r["trade_date"] for r in rows]
    facts["first_date"] = dates[0]
    facts["last_date"] = dates[-1]
    facts["dates"] = dates
    facts["distinct_dates"] = len(set(dates))
    # 「有历史」= 不止一个**交易日**。只有 'latest' 占位、或只有一天的，
    # 都算没有历史 —— 单日快照撑不起任何跨日比较。
    facts["has_history"] = len(set(dates)) > 1
    facts["last_success_at"] = max(
        (r["fetched_at"] for r in rows if r["fetched_at"]), default=None)

    # 条数与缺字段：**逐日**统计，不只看总量。
    # 只看总量会把「一天 4782 条、另一天 17 条」平均成 2400，看不出任何问题。
    total_rows = bad_rows = 0
    per_day: Dict[str, int] = {}
    unreadable: List[str] = []
    for r in rows:
        items = _rows_of(dataset, r["payload"])
        n, bad = _missing_field_count(dataset, items)
        total_rows += n
        bad_rows += bad
        per_day[r["trade_date"]] = n
        # row_count 是入库时记的「payload 里有几条」。声称有、却一条都解析
        # 不出来，说明内容在落库之后坏了 —— 数据在库里但读不出来，
        # 跟「没有」在回测眼里一样不可用，必须单独报出来。
        if (r["row_count"] or 0) > 0 and n == 0:
            unreadable.append(r["trade_date"])
    facts["unreadable_days"] = sorted(unreadable)
    facts["rows_total"] = total_rows
    facts["missing_field_rows"] = bad_rows
    facts["per_day_items"] = per_day

    # 日期覆盖率：分母 = 本地日历里 [首日, min(末日, 最新日历日)] 的交易日数
    cal = [d for d in calendar
           if d >= facts["first_date"] and d <= facts["last_date"]]
    facts["expected_days"] = len(cal) or None
    facts["missing_dates"] = [d for d in cal if d not in set(dates)]
    if cal:
        facts["coverage_rate"] = round(len(set(dates)) / len(cal), 4)
    facts["calendar_stale"] = None if calendar else True

    # 「部分股票」判定：只对条数天然稳定的域做
    if dataset not in _COUNT_UNSTABLE and per_day:
        peak = max(per_day.values())
        facts["item_peak"] = peak
        if peak > 0:
            facts["thin_days"] = sorted(
                d for d, n in per_day.items()
                if n < peak * _PARTIAL_RATIO)

    return facts


# ------------------------------------------------------- 任务运行的事实


def job_facts(conn: sqlite3.Connection, dataset: str) -> Dict[str, Any]:
    """从 `job_run` 里挖这个域**最近一次尝试**与**最近一次错误**。

    ⚠️ 这是**推断**，不是权威记录：`job_run.detail` 是整轮流水线的 JSON
    快照，没有逐域的独立状态行。第三阶段的 `ingest_job` 表落地后，
    这里应该改成读那张表。在此之前，本函数如实标注 `inferred=True`。
    """
    step = _STEP_OF.get(dataset)
    out: Dict[str, Any] = {
        "step": step,
        "last_attempt_at": None,
        "last_error": None,
        "last_result": None,
        "inferred": True,
    }
    if step is None or "job_run" not in _tables(conn):
        return out

    for r in conn.execute(
            "SELECT started_at, status, detail FROM job_run "
            "ORDER BY id DESC LIMIT 200"):
        try:
            detail = json.loads(r["detail"] or "{}")
        except (TypeError, ValueError):
            continue
        results = detail.get("results") or {}
        if step not in results:
            continue
        out["last_attempt_at"] = r["started_at"]
        val = results[step]
        out["last_result"] = val if isinstance(val, (int, float)) else None
        if isinstance(val, str) and val.startswith("失败"):
            out["last_error"] = val
        elif step in (detail.get("failed") or []):
            out["last_error"] = f"该步骤被判失败（status={r['status']}）"
        return out          # 只取最近一次提到这个步骤的那轮
    return out


# ------------------------------------------------------------- 五态判定


def verdict(dataset: str, facts: Dict[str, Any], job: Dict[str, Any]) -> str:
    """按事实判五态。**判不出来就取最差**，不往好了猜。"""
    states: List[str] = []

    if job.get("last_error"):
        states.append(SOURCE_DOWN)

    if facts["first_date"] is None:
        # 一行都没有。区分「没人采」和「采了但空手而归」——两者要人做的事不同。
        # 但若只有 'latest' 占位键，说明**采到了、只是没按交易日归档**：
        # 有数据但零历史，那是 PARTIAL 不是没数据。
        if facts.get("placeholder_latest"):
            states.append(PARTIAL)
        else:
            states.append(NO_JOB if job.get("last_attempt_at") is None else NO_DATA)
        return worst_state(*states) if states else NO_DATA

    if facts.get("missing_dates"):
        states.append(PARTIAL)

    # 条数骤降 = 只覆盖了部分股票
    if facts.get("thin_days"):
        states.append(PARTIAL)

    # 关键字段缺失 = 完整性没通过
    if facts.get("missing_field_rows"):
        states.append(PARTIAL)

    # 有条数却读不出内容 = 落库后坏了。数据在、但用不了，比缺数据更该报警：
    # 缺数据一眼看得见，坏 payload 会让下游拿到空列表还以为那天没涨停。
    if facts.get("unreadable_days"):
        states.append(PARTIAL)

    return worst_state(*states) if states else OK


# ------------------------------------------------------------- 总报告


def coverage_report(db_path: Optional[str] = None) -> Dict[str, Any]:
    """全量覆盖审计。**只读本地库，一次外发都没有。**"""
    calendar, stale = cached_trade_dates()
    report: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "offline": True,
        "calendar": {
            "days": len(calendar),
            "first": calendar[0] if calendar else None,
            "last": calendar[-1] if calendar else None,
            "stale": stale,
            # 过期日历会让漏采检测漏判/误判，必须显式说
            "note": ("日历缓存已过期（>30 天），漏采检测结果可能不准；"
                     "本模块不联网刷新，需人工更新 trade_calendar.json。"
                     if stale else "日历缓存在有效期内。"),
        },
        "datasets": {},
        "missing_jobs": [],
    }

    try:
        conn = open_ro(db_path)
    except sqlite3.Error as exc:
        report["error"] = f"库无法只读打开：{type(exc).__name__}: {exc}"
        return report

    try:
        present = {r[0] for r in conn.execute(
            "SELECT DISTINCT dataset FROM snapshot")}
        # 定义里有的、库里没有的，也要报 —— 「表里根本没有这个域」本身就是
        # 最需要看见的结论，不能因为它没数据就不列。
        for dataset in sorted(set(_SNAP_KINDS) | present):
            facts = dataset_facts(conn, dataset, calendar)
            job = job_facts(conn, dataset)
            facts["job"] = job
            facts["state"] = verdict(dataset, facts, job)
            facts["domain_kind"] = _SNAP_KINDS.get(dataset, "unknown")
            facts["step"] = _STEP_OF.get(dataset)
            report["datasets"][dataset] = facts
    finally:
        conn.close()

    report["missing_jobs"] = sorted(
        d for d, f in report["datasets"].items() if f["state"] == NO_JOB)
    return report


def gaps(db_path: Optional[str] = None, dataset: Optional[str] = None,
         lookback: int = 30) -> Dict[str, List[str]]:
    """近期漏采速查：`{dataset: [YYYYMMDD, ...]}`。

    只看**最近 `lookback` 个已过期的交易日** —— 今天还没走完、明天还没到，
    都不算漏采；而「1990 年以来从没采过」对补跑毫无操作价值，列出来只是
    一堵墙。要看完整历史缺口用 `coverage_report()` 的 `missing_dates`。
    """
    calendar, _ = cached_trade_dates()
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    past = [d for d in calendar if d < today][-max(1, int(lookback)):]

    try:
        conn = open_ro(db_path)
    except sqlite3.Error:
        return {}
    try:
        out: Dict[str, List[str]] = {}
        targets = [dataset] if dataset else sorted(_SNAP_KINDS)
        # 同上：判的是 snapshot 表在不在，不是 dataset 名
        if "snapshot" not in _tables(conn):
            return {ds: list(past) for ds in targets}
        for ds in targets:
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT trade_date FROM snapshot "
                "WHERE dataset=? AND trade_date<>'latest'", (ds,))}
            out[ds] = [d for d in past if d not in have]
        return out
    finally:
        conn.close()
