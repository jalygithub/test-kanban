"""关注股票：从「疑似主力建仓日」到当前的行情与区间表现。

设计上的三个约束：

1. **硬超时**。日线接口偶发会卡在分块读上 —— netfix 注入的是
   connect/read 超时，read 只约束**单次** recv，对端只要持续滴数据
   就永远不会触发。所以这里所有网络取数都丢进独立的单线程执行器，
   到点就抛 TimeoutError，绝不让一个卡死的请求拖住整个关注页。
   （用单线程而非多线程：数据源卡住时最多泄漏一个线程。）
2. **按 (code, start_date) 缓存**。同一只股在同一起点上一天只拉一次，
   关注页反复进出不会反复打数据源。
3. **指数对比可降级**。沪深300 取不到就返回 None，个股数据照常出。

市场区分（P0 港股支持）：`market="cn"`（默认，行为与从前完全一致）走
A 股日线 + 沪深300；`market="hk"` 走 `fetchers.hk` 的港股日线 + 恒生指数。
缓存键 `_HIST` 用 `(code, start_date)` —— 两个市场 code 位数不同
（6 位 vs 5 位），天然不会串；基准缓存 `_INDEX` 用 `sh000300` / `HSI` 分键。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from app.fetchers import hk as hk_fetch
from app.fetchers import quotes
from app.store import db

logger = logging.getLogger(__name__)

FETCH_TIMEOUT = 30.0      # 单次拉取硬超时（正常 ~1.7s，留 17 倍余量）
CACHE_TTL = 6 * 3600      # 行情缓存 6 小时
FETCH_BUDGET = 40.0       # 批量计算的总预算，超了就先返回已完成的部分
CONTEXT_DAYS = 90         # 建仓日之前多取的自然日数（约 60 根K线的上下文）

_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="watch-fetch")
_LOCK = threading.Lock()
# 键里带 market：虽然两个市场的 code 位数不同已经不会撞，仍然写全，
# 免得以后有人往关注里塞了个 6 位港股代码（或反之）悄悄读到另一市场的K线。
_HIST: Dict[Tuple[str, str, str], Tuple[float, pd.DataFrame]] = {}
_INDEX: Dict[str, Tuple[float, pd.DataFrame]] = {}   # 'sh000300' / 'HSI'


# --------------------------------------------------------------- 取数


def _bounded(fn, what: str):
    """跑在独立线程里并硬超时。超时后原线程仍会占着执行器，但最多占一个。"""
    future = _EXEC.submit(fn)
    try:
        return future.result(timeout=FETCH_TIMEOUT)
    except FuturesTimeout as exc:
        raise TimeoutError(
            f"{what} 超过 {FETCH_TIMEOUT:.0f}s 未返回，已放弃") from exc


def clear_cache(code: Optional[str] = None) -> None:
    """手动刷新时清缓存；传 code 只清这一只。"""
    with _LOCK:
        if code is None:
            _HIST.clear()
            _INDEX.clear()
        else:
            for key in [k for k in _HIST if k[0] == code]:
                _HIST.pop(key, None)


def _load_history(code: str, source_date: str, market: str = "cn") -> pd.DataFrame:
    """取 source_date - CONTEXT_DAYS 到今天的日线。

    不能只从建仓日起取：候选是**刚算出来的**，建仓日几乎必然是最新一个
    交易日，那样图上只有一根K线，看不出任何东西。多取一段上下文才有
    走势可看（区间统计仍在 _stats 里按 source_date 单独截断）。
    """
    key = (code, source_date, market)
    now = time.time()
    with _LOCK:
        hit = _HIST.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    end = datetime.now().strftime("%Y%m%d")
    try:
        base = datetime.strptime(source_date, "%Y%m%d")
    except ValueError:
        logger.warning("建仓日格式异常 %r，按今天回退", source_date)
        base = datetime.now()
    start = (base - timedelta(days=CONTEXT_DAYS)).strftime("%Y%m%d")

    df = _bounded(
        lambda: quotes.daily_history(code, start, end, market=market),
        f"日线 {code}")
    if df is None or df.empty:
        raise RuntimeError(f"{code} 未取到 {start} 以来的日线")
    with _LOCK:
        _HIST[key] = (now, df)
    return df


def _load_index(market: str = "cn") -> pd.DataFrame:
    """基准指数日收盘。

    A 股 = 沪深300（新浪源，与行情源不同，互为备份）；
    港股 = 恒生指数（新浪源，走 `fetchers.hk`）。两者分键缓存。
    """
    key = "HSI" if market == "hk" else "sh000300"
    now = time.time()
    with _LOCK:
        hit = _INDEX.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]

    if market == "hk":
        df = _bounded(hk_fetch.index_daily, "恒生指数")
    else:
        def _fetch() -> pd.DataFrame:
            import akshare as ak  # noqa: PLC0415  按需导入，避免拖慢启动
            raw = ak.stock_zh_index_daily(symbol="sh000300")
            out = pd.DataFrame({
                "date": pd.to_datetime(raw["date"]).dt.strftime("%Y%m%d"),
                "close": pd.to_numeric(raw["close"], errors="coerce"),
            })
            return out.dropna().reset_index(drop=True)

        df = _bounded(_fetch, "沪深300")

    with _LOCK:
        _INDEX[key] = (now, df)
    return df


def _try_index(market: str = "cn") -> Optional[pd.DataFrame]:
    """指数拿不到就当没有，绝不影响个股数据。"""
    try:
        return _load_index(market)
    except Exception as exc:  # noqa: BLE001
        label = "恒生指数" if market == "hk" else "沪深300"
        logger.debug("%s 不可用（对比降级）: %s", label, exc)
        return None


# --------------------------------------------------------------- 统计


def _stats(df: pd.DataFrame, source_date: str,
           idx: Optional[pd.DataFrame]) -> Dict[str, Any]:
    """从建仓日到最新一根的区间表现。

    基准价取**建仓日收盘**：候选是盘后算出来的，能拿到的下一个价就是它。
    """
    if df is None or df.empty:
        return {}

    win = df[df["date"] >= source_date]
    if win.empty:
        return {}
    rows = win.to_dict("records")

    base = _f(rows[0].get("close"))
    if base <= 0:
        return {}
    last = rows[-1]
    last_close = _f(last.get("close"))
    pct = (last_close / base - 1.0) * 100.0

    hi = max(rows, key=lambda r: _f(r.get("high")))
    lo = min(rows, key=lambda r: _f(r.get("low")) or 1e18)

    # 最大回撤：区间内从峰值到其后谷值的最大跌幅（负数）
    peak, mdd = -1e18, 0.0
    for r in rows:
        c = _f(r.get("close"))
        if c > peak:
            peak = c
        if peak > 0:
            mdd = min(mdd, (c - peak) / peak * 100.0)

    index_pct = None
    alpha = None
    if idx is not None and not idx.empty:
        sub = idx[idx["date"] >= source_date]
        ib = _f(sub.iloc[0].get("close")) if len(sub) else 0.0
        if len(sub) >= 1 and ib > 0:
            index_pct = (_f(sub.iloc[-1].get("close")) / ib - 1.0) * 100.0
            alpha = pct - index_pct

    first_day = _day(rows[0]["date"])
    last_day = _day(last["date"])
    calendar_days = (last_day - first_day).days if first_day and last_day else 0

    # 这里必须用 `_num` 而不是 `if r.get(...)`：**NaN 是 truthy**，
    # 新浪港股日线（以及 A 股腾讯备源）没有换手率列、整列是 NaN，
    # 用真值判断会把 NaN 当有效值收进去 —— `_f(NaN)` 归 0 之后，
    # 「没有换手率数据」就会算成「平均换手 0.00%」，比显示 '-' 更误导人。
    turnover = [v for v in (_num(r.get("turnover")) for r in rows) if v is not None]
    amounts = [v for v in (_num(r.get("amount")) for r in rows) if v is not None]

    return {
        "source_date": source_date,
        "start_date": rows[0]["date"],
        "start_close": round(base, 4),
        "last_date": last["date"],
        "last_close": round(last_close, 4),
        "pct": round(pct, 2),
        "high": round(_f(hi.get("high")), 4),
        "high_date": hi["date"],
        "low": round(_f(lo.get("low")), 4),
        "low_date": lo["date"],
        "max_drawdown": round(mdd, 2),
        "trading_days": len(rows),
        "calendar_days": calendar_days,
        "avg_turnover": round(sum(turnover) / len(turnover), 2) if turnover else None,
        "avg_amount": round(sum(amounts) / len(amounts)) if amounts else None,
        "index_pct": round(index_pct, 2) if index_pct is not None else None,
        "alpha": round(alpha, 2) if alpha is not None else None,
    }


def _f(v: Any) -> float:
    try:
        out = float(v)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0  # NaN


def _num(v: Any) -> Optional[float]:
    """转 float；空值/非数/NaN 一律 **None**（区别于 `_f` 的归 0）。

    用在「缺了就应该显示 `-`、而不是 `0.00`」的字段上（换手率、成交额）。
    `float('nan') == float('nan')` 为 False，这一句就把 NaN 拦下来了。
    """
    try:
        out = float(v)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _day(ymd: str):
    try:
        return datetime.strptime(ymd, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def relist(code: str, source_date: str, limit: int = 20,
           market: str = "cn") -> List[Dict[str, Any]]:
    """建仓日**之后**是否又进了 Top30 —— 判断模型是不是持续看好它。

    港股恒为 `[]`：主力建仓打分只跑 A 股全市场，港股根本不会出现在候选里，
    再往下翻库只是白读（而且会把「暂无后续打分」误解成模型不看好它）。
    """
    if market != "cn":
        return []
    hits: List[Dict[str, Any]] = []
    for day in db.list_dates("main_force", limit=limit):
        if day <= source_date:
            continue
        res = db.load_one("main_force", day)
        if not res:
            continue
        for row in res.get("rows") or []:
            if row.get("code") == code:
                hits.append({"date": day, "rank": row.get("rank"),
                             "score": row.get("score")})
                break
    hits.sort(key=lambda x: x["date"])
    return hits


# --------------------------------------------------------------- 对外


def summary(code: str, source_date: str, market: str = "cn") -> Dict[str, Any]:
    """关注列表用：只要区间表现，不带行情明细。"""
    df = _load_history(code, source_date, market)
    return _stats(df, source_date, _try_index(market))


def detail(code: str, source_date: str, refresh: bool = False,
           market: str = "cn") -> Dict[str, Any]:
    """关注详情用：K 线明细 + 区间表现 + 同期指数 + 后续再入选。

    `market` 放在 `refresh` 之后，保持历史上的关键字调用完全兼容。
    """
    if refresh:
        clear_cache(code)
    df = _load_history(code, source_date, market)
    idx = _try_index(market)
    stats = _stats(df, source_date, idx)

    bars = [{
        "date": r["date"],
        "open": _f(r.get("open")) or None,
        "close": _f(r.get("close")) or None,
        "high": _f(r.get("high")) or None,
        "low": _f(r.get("low")) or None,
        "volume": _f(r.get("volume")) or None,
        "amount": _f(r.get("amount")) or None,
        "turnover": _f(r.get("turnover")) or None,
        "pct_chg": _f(r.get("pct_chg")),
        # 非空表示这根在建仓日之前 —— 只是上下文，不计入区间统计
        "pre": r["date"] < source_date,
    } for r in df.to_dict("records")]

    index_rows: List[Dict[str, Any]] = []
    if idx is not None and len(idx) and bars:
        # 指数窗口与 K 线窗口对齐，否则建仓日之前那段没有对比基准
        lo, hi = bars[0]["date"], bars[-1]["date"]
        sub = idx[(idx["date"] >= lo) & (idx["date"] <= hi)]
        index_rows = [{"date": r["date"], "close": round(_f(r["close"]), 4)}
                      for r in sub.to_dict("records")]

    return {
        "bars": bars,
        "stats": stats,
        "index": index_rows,
        "relist": relist(code, source_date, market=market),
    }
