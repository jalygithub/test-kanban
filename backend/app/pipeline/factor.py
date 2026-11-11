# -*- coding: utf-8 -*-
"""后复权因子取数与落库（数据管线阶段 5 补充）。

## 为什么单独成一个模块

`backfill.py` 有一条 AST 断言（`_backfill_check` 的 B4）：**日线价格域里不许在
调用点写死任何 `adjust` 字面量** —— 只能是常量 ``ADJUST``（= `""`）或空串。
那是「绝不落前复权价格」的护栏。

而复权因子走的是 `stock_zh_a_daily(adjust="hfq-factor")`，它返回的是
**因子序列**（`date` + `hfq_factor` 两列），**不是价格**。两者语义不同：
价格域的护栏管不到因子域，因子域也不该被塞进价格域的护栏里凑数。

所以拆开：价格归 `backfill.py`，因子归本模块。本模块**只写 `adj_factor` 一列**，
碰不到任何价格字段 —— 谁想复核这条边界，读本模块就看完了。

## 为什么不是「每只票自己一套因子」

`stock_zh_a_daily(adjust="hfq-factor")` 回的是**除权事件日**的因子，不是每日都有
（600519 六十多年里只有 33 个事件日）。事件日之间因子恒定，所以要**前向填充**：
某日的因子 = 该日**或之前最近一次**除权事件的因子。

`resolve()` 干的就是这件事。早于所有事件的交易日**留 NULL**，不填 1.0 ——
填了等于悄悄声明「没有复权」，那是口径不是数据（与 `backfill.py` 同一条原则）。

## 因子不是未来数据泄漏

后复权因子 `F(t)` 累乘到 `t` 为止的所有除权事件。回测只用两端**比值**
`F(exit)/F(entry)`，而这个比值恰好等于 **(entry, exit] 区间内**发生的除权事件，
全部 ≤ `exit`。

`qfq` 之所以是泄漏，是因为它拿 `exit` **之后**的除权去改写两端价格 —— 两回事。
详见 `docs/backtest/limitations.md §6`。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.store import db

logger = logging.getLogger(__name__)

# 复权方式。与 `backfill.ADJUST`（价格域的 `""`）是**两个口径**，别混：
# 那个说的是「价格不复权」，这个说的是「取后复权因子」。
ADJUST_FACTOR = "hfq-factor"

# 交易所前缀。akshare 的 `stock_zh_a_daily` 要的是 `sh600519` 这种带前缀的
# symbol，而库里存的是裸 6 位代码 —— 差一个前缀就是另一个标的。
_PREFIX = (("6", "sh"), ("5", "sh"),        # 沪市主板 / 基金
           ("0", "sz"), ("3", "sz"),        # 深市主板 / 创业板
           ("4", "bj"), ("8", "bj"), ("9", "bj"))   # 北交所


def code_to_symbol(code: str) -> Optional[str]:
    """裸代码 -> 带交易所前缀的 symbol。**认不出就回 None**。

    回 None 而不是瞎猜一个前缀：猜错的话接口会返回**别的标的**的因子，
    那份因子被写进本票的 `adj_factor`，收益算出来是错的却完全看不出来。
    宁可不填（留 NULL，口径记成 UNCERTAIN），也不填一份错的。
    """
    c = str(code or "").strip()
    if len(c) != 6 or not c.isdigit():
        return None
    for head, prefix in _PREFIX:
        if c.startswith(head):
            return f"{prefix}{c}"
    return None


def fetch_events(code: str,
                 fetcher: Optional[Callable[..., Any]] = None
                 ) -> Dict[str, float]:
    """取某只票的**除权事件**表，返回 ``{交易日: 因子}``。

    :param fetcher: 注入替身（测试用）。缺省走真实网络取数，经
                    ``fetchers.client.call()``（限速 + 重试 + 熔断）。

    取不到任何事件**回空字典**，不抛异常 —— 因子缺失不是致命错误，它只是让
    收益的口径降级成「判不了有没有除权」，价格本身仍然是好的。
    """
    symbol = code_to_symbol(code)
    if symbol is None:
        logger.info("复权因子：代码 %s 认不出交易所，跳过", code)
        return {}

    if fetcher is None:
        import akshare as ak                                    # noqa: PLC0415
        from app.fetchers.client import call                    # noqa: PLC0415
        fetcher = lambda **kw: call(f"复权因子 {code}",            # noqa: E731
                                   ak.stock_zh_a_daily, **kw)

    try:
        df = fetcher(symbol=symbol, adjust=ADJUST_FACTOR)
    except Exception as exc:  # noqa: BLE001  单票取不到不该连累整批
        logger.warning("复权因子 %s 取数失败：%s", code, exc)
        return {}
    if df is None or len(df) == 0:
        return {}

    events: Dict[str, float] = {}
    for r in df.to_dict("records"):
        raw = str(r.get("date") or "").strip().replace("-", "")[:8]
        if len(raw) != 8 or not raw.isdigit():
            continue
        try:
            f = float(r.get("hfq_factor"))
        except (TypeError, ValueError):
            continue                      # 缺值就是缺值，不填 1.0 充数
        if f > 0:
            events[raw] = f
    return events


def resolve(events: Dict[str, float], dates: List[str]
            ) -> List[Tuple[str, float]]:
    """前向填充：给每个交易日配一个 ``<=`` 它的最近事件因子。

    双指针扫一遍，O(事件数 + 交易日数)。**早于所有事件的交易日不输出** ——
    那天到底有没有除权我们不知道，留 NULL 比填 1.0 诚实。

    :param events: ``{事件日: 因子}``，无序即可，内部会排
    :param dates:  该票在库里的交易日（升序）
    """
    if not events or not dates:
        return []
    ev = sorted(events.items())
    out: List[Tuple[str, float]] = []
    j, cur = -1, None
    for d in sorted(dates):
        while j + 1 < len(ev) and ev[j + 1][0] <= d:
            j += 1
            cur = ev[j][1]
        if cur is not None:
            out.append((d, cur))
    return out


def fill(code: str, events: Dict[str, float], table: str = "daily_bar"
         ) -> int:
    """把因子写进库里该票已有的日线行，返回实际更新的行数。

    与 `bars_upsert` 的「已有数据不覆盖」同一条口径：**只补 `adj_factor`
    为 NULL 的行**。已经填过的不动 —— 因子一旦核对过就不该被一次重跑悄悄
    盖掉，那正是阶段 4 定的第 2 条。
    """
    if not events:
        return 0
    dates = db.bars_dates(table, code)
    if not dates:
        return 0
    pairs = resolve(events, dates)
    if not pairs:
        # 事件日全都在交易日序列之后 —— 等于这段时间内一个除权都没有
        # （或者因子表比行情还老）。不写任何东西，也不算失败。
        return 0
    return db.bars_fill_factors(table, code, pairs)


def backfill_factors(code: str, table: str = "daily_bar",
                     fetcher: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """一条票的因子补写全流程：取事件 -> 前向填充 -> 落库。

    :return: ``{code, updated, skipped, reason}``。`reason` 只在没写成时给，
             且写清楚是「认不出交易所」「取数失败」还是「没有事件」——
             三者的处理路径完全不同，混成一句「没补上」没法排查。
    """
    symbol = code_to_symbol(code)
    if symbol is None:
        return {"code": code, "updated": 0, "skipped": True,
                "reason": "认不出交易所前缀，不猜着发请求"}
    events = fetch_events(code, fetcher=fetcher)
    if not events:
        return {"code": code, "updated": 0, "skipped": True,
                "reason": "源未返回任何除权事件（或取数失败）"}
    n = fill(code, events, table=table)
    return {"code": code, "updated": n, "skipped": n == 0,
            "reason": None if n else "库中没有该票的日线行，无处可写"}
