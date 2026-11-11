"""基金历史净值 —— 「显示实际涨跌幅」这条链。

## 为什么估值链不够

天天基金估值源只带**一个** `NAV` + `PDATE`（最新一次公布的单位净值），
算某一天的实际涨跌幅要**前后两个**净值。页面要回答的三个问题里有两个
必须看历史：

    盘前      显示**前一交易日**的实际涨跌幅（不是昨天盘中那个估算）
    盘后      当日机构净值**出没出** —— 出了显示实际，没出才退回估值

所以补这第三条链。它**只服务于「实际涨跌幅」**：拿不到就让那一列空着，
估值本身照常工作，两条链都不受影响。

## 源

走 akshare `fund_open_fund_info_em(indicator="单位净值走势")` —— 与本项目
其余取数一样集中在 `fetchers/` 并经过 `client.call()`，限速/超时/退避复用
`netfix` 那一套。底层是天天基金 `pingzhongdata/{code}.js`，返回三列：

    净值日期 | 单位净值 | 日增长率

**日增长率就是现成的实际涨跌幅**，不必自己拿前后两日相除 —— 自己算还要
额外处理跨周末、漏发净值的间隔判定，而源给的是它自己的口径。

## 降级承诺

拿不到、熔断中、结构对不上，一律返回空序列，由调用方让该列显示「—」，
**绝不猜一个数**。熔断与估值链 `em_fundval`、持仓链 `em_fundf10` **各自独立**：
净值历史挂了没理由把能用的估值一起判死。

## 预算

一次「查询估值」最多 `_MAX_CODES` 只基金，逐只拉历史会拖慢整轮查询，
所以整条链共用一个**时间预算**：用尽就停止，剩下的基金本轮不显示实际涨跌幅
并记一条 warning。进程内还有 TTL 缓存 —— 同一轮里重复的代码不会二次外发。
"""
from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

from app.fetchers.client import ak, call

logger = logging.getLogger(__name__)

__all__ = ["histories", "latest", "reset_circuit"]

# ----------------------------------------------------------------- 熔断

_CIRCUIT: Dict[str, float] = {}    # 源名 -> 恢复时刻(monotonic)
_CIRCUIT_TTL = 900.0               # 15 分钟：本页手动触发，不像实时页 5 秒一轮
_FAILURES: Dict[str, int] = {}
_FAIL_THRESHOLD = 3                # 单次抖动不该把一条链判死

_SOURCE = "em_navhist"


def _down() -> bool:
    return time.monotonic() < _CIRCUIT.get(_SOURCE, 0.0)


def _mark_up() -> None:
    _CIRCUIT.pop(_SOURCE, None)
    _FAILURES.pop(_SOURCE, None)


def _fail(exc: Exception) -> None:
    _FAILURES[_SOURCE] = _FAILURES.get(_SOURCE, 0) + 1
    if _FAILURES[_SOURCE] >= _FAIL_THRESHOLD:
        _CIRCUIT[_SOURCE] = time.monotonic() + _CIRCUIT_TTL
        logger.warning("基金净值历史源连续失败 %d 次，熔断 %d 分钟: %s",
                       _FAILURES[_SOURCE], int(_CIRCUIT_TTL / 60), exc)


def reset_circuit() -> None:
    _CIRCUIT.clear()
    _FAILURES.clear()
    _CACHE.clear()


# ----------------------------------------------------------------- 缓存

_CACHE: Dict[str, Tuple[float, List[Dict[str, object]]]] = {}
_CACHE_TTL = 1800.0        # 净值一天只公布一次，半小时足够覆盖一轮查询

# 一条查询里的净值历史总预算（秒）。逐只拉会线性累加，超了就认输 ——
# 「实际涨跌幅」是锦上添花，不该把「查询估值」本身拖到用户以为卡死。
_BUDGET = 8.0

# 只留最近这么多条：够算前后两日、够回填 T+1 复盘，也把回读体积钉死。
_KEEP = 40


def _to_series(df: Optional[pd.DataFrame]) -> List[Dict[str, object]]:
    """akshare 的 DataFrame → `[{"date", "nav", "pct"}]`（**升序**，最近在最后）。

    列名对不上就回空表，**不猜列序** —— 源哪天改了列名，宁可这一列空着，
    也不能把净值读成涨跌幅。
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return []
    cols = {str(c).strip(): c for c in df.columns}
    date_col = cols.get("净值日期")
    nav_col = cols.get("单位净值")
    if date_col is None or nav_col is None:
        logger.info("净值历史列名与预期不符：%s", list(cols))
        return []
    pct_col = cols.get("日增长率")

    out: List[Dict[str, object]] = []
    for _, r in df.iterrows():
        d = r[date_col]
        nav = r[nav_col]
        try:
            nav_v = float(nav)
        except (TypeError, ValueError):
            continue
        if nav_v != nav_v or nav_v <= 0:      # NaN / 0 不是净值
            continue
        day = getattr(d, "strftime", None)
        day = day("%Y-%m-%d") if day else str(d)[:10]
        pct: Optional[float] = None
        if pct_col is not None:
            try:
                p = float(r[pct_col])
                pct = p if p == p else None   # NaN -> None
            except (TypeError, ValueError):
                pct = None
        out.append({"date": day, "nav": nav_v, "pct": pct})

    out.sort(key=lambda x: str(x["date"]))
    return out[-_KEEP:]


# ----------------------------------------------------------------- 主入口


def histories(codes: Iterable[str],
              warns: Optional[List[str]] = None,
              budget: float = _BUDGET) -> Dict[str, List[Dict[str, object]]]:
    """批量取净值历史。返回 `{code: 序列}`，**取不到的 code 不在结果里**。

    `warns` 非 None 时把降级原因摊出去（同估值链的口径：静默降级等于让
    「—」没法解释）。失败的 code 不报错、不抛异常 —— 这条链天生可以缺席。
    """
    warns = warns if warns is not None else []
    order: List[str] = []
    seen = set()
    for c in codes:
        c = str(c or "").strip()
        if c and c not in seen:
            seen.add(c)
            order.append(c)
    if not order:
        return {}

    out: Dict[str, List[Dict[str, object]]] = {}
    pending: List[str] = []
    now = time.monotonic()
    for code in order:
        hit = _CACHE.get(code)
        if hit and now - hit[0] < _CACHE_TTL:
            out[code] = hit[1]
        else:
            pending.append(code)

    if not pending:
        return out

    if _down():
        warns.append(f"净值历史源熔断中，{len(pending)} 只基金本轮不显示实际涨跌幅")
        return out

    deadline = time.monotonic() + budget
    failed = 0
    for code in pending:
        if time.monotonic() > deadline:
            warns.append(
                f"净值历史拉取超时（{budget:g}s 预算用尽），"
                f"剩余 {len(pending) - failed} 只本轮不显示实际涨跌幅")
            break
        try:
            # `indicator` 是中文枚举，写死在这一处；period 走默认「全部」
            df = call(f"净值历史 {code}", ak.fund_open_fund_info_em,
                      symbol=code, indicator="单位净值走势")
            _mark_up()
        except Exception as exc:  # noqa: BLE001  这条链天生可以整条缺席
            failed += 1
            _fail(exc)
            logger.info("净值历史 %s 失败：%s: %s",
                        code, type(exc).__name__, str(exc)[:80])
            continue
        series = _to_series(df)
        _CACHE[code] = (time.monotonic(), series)
        out[code] = series

    if failed:
        warns.append(
            f"{failed} 只基金的净值历史没取到，这些只本轮不显示实际涨跌幅"
            f"（估值本身不受影响）")
    return out


def latest(series: List[Dict[str, object]]) -> Tuple[Optional[str], Optional[float]]:
    """序列（升序）的最近一条 → `(净值日期, 日增长率)`；缺任一项回 `(None, None)`。"""
    if not series:
        return None, None
    last = series[-1]
    d = last.get("date")
    p = last.get("pct")
    return (str(d) if d else None), (float(p) if isinstance(p, (int, float)) else None)
