# -*- coding: utf-8 -*-
"""盘后日线采集：全市场快照顺带落 `daily_bar`（2026-11-11 拍板的路线 A）。

东财全市场快照一次调用自带今开/最高/最低/成交量（f17/f15/f16/f5），转成
日线行落库 —— 每天 0~1 次额外请求；逐只拉要 4800 次/天，不走那条路。

三道闸门，任一不过就**抛异常**（`scheduler._collect_domain` 落
`record_failure` → 退避重试，17:30 / 次日 09:20 检查点会再来一次），
绝不静默空过：

1. **日期要对得上**：快照永远只反映「最近交易日」。目标日 ≠ 快照所属日
   （比如指定历史日期重跑）写进去就是张冠李戴 —— 历史日线走回填补；
2. **15:01 后才落当日行**：盘中的半日 OHLC 一旦入库就是永久错数据，
   宁可这轮判失败，等收盘后的补跑再来；
3. **源要给得出 OHLC**：东财挂了降级到同花顺时快照没有这些字段（是
   「没有」不是「值为 None」，补不了也猜不了）→ 判失败进重试，等东财恢复。

行级：OHLC 或成交量不全/非正的行（停牌、源给 `-`）**跳过不写** —— 缺数据
显示 `-`，绝不编数。落库走 `bars_upsert`（ON CONFLICT DO NOTHING，不覆盖
已有行情）；完整行不足 `MIN_BAR_ROWS` 判快照分页不全，同样抛进重试 ——
宁可重试一次，也不落半张市场还标 SUCCESS（job_run #18 的教训）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from app.fetchers import calendar as tcal
from app.fetchers import quotes
from app.store import db

logger = logging.getLogger(__name__)

#: 收盘时刻（含）之后才允许落**当日**行。15:00 收盘，留 1 分钟让行情收口。
CLOSE_AT = (15, 1)

#: 全市场约 5400 只；低于这个数说明快照没拉全（东财 clist 是分页拉的）。
#: 与 `tasks.MIN_MARKET_ROWS` 同一个数量级、同一条理由。
MIN_BAR_ROWS = 3000


def _snapshot_day(now: datetime) -> str:
    """这份快照反映的是哪一天。**只读本地日历，零网络。**

    今天是交易日 → 今天；否则 → 日历里早于今天的最后一个交易日（周末/
    节假日快照上就是那个最近交易日的完整数据）。日历缺失或已过期就抛：
    「不知道快照属于哪天」时唯一安全的做法是不落库，猜一个日期写进去
    比失败更坏。
    """
    cal = tcal.cached_trade_dates()
    if not cal:
        raise RuntimeError("交易日历不可用，无法确认快照属于哪天 —— 拒绝落日线")
    today = now.strftime("%Y%m%d")
    if today in set(cal):
        return today
    if today > cal[-1]:
        raise RuntimeError(
            f"交易日历已过期（最晚 {cal[-1]}），无法确认快照属于哪天")
    earlier = [d for d in cal if d < today]
    if not earlier:
        raise RuntimeError("交易日历里没有早于今天的交易日，无法确认快照日期")
    return earlier[-1]


def _to_bar(row: Dict[str, Any], trade_date: str) -> Optional[Dict[str, Any]]:
    """一行快照 → 一条日线；给不出完整 OHLC 的行回 None（跳过，不编数）。"""
    code = row.get("code")
    vals = (row.get("open"), row.get("high"), row.get("low"),
            row.get("price"), row.get("volume"))
    if not code:
        return None
    # 停牌 / 源给 `-` 的行：缺失或非正的数值都不是「当天的一根线」。
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and v > 0 for v in vals):
        return None
    return {
        "code": str(code),
        "trade_date": trade_date,
        "open": vals[0], "high": vals[1], "low": vals[2],
        "close": vals[3],          # 收盘后的最新价就是收盘价
        "volume": vals[4],
        "amount": row.get("amount"),
        "turnover": row.get("turnover"),
        "pct_chg": row.get("pct_chg"),
        # 复权因子这个源拿不到 —— 留 NULL，不填 1.0 充数（backfill 同口径）
        "adj_factor": None,
        "source": row.get("source") or "eastmoney",
    }


def collect(trade_date: str,
            fetch: Optional[Callable[[], List[Dict[str, Any]]]] = None,
            now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    """抓全市场快照 → 落当日日线，返回**交给校验的**完整行。

    :param fetch: 注入替身取数器（测试用）；缺省 `quotes.market_snapshot`
        （会联网）。闸门排在取数**之前** —— 不该落的轮次一个包都不发。
    :param now: 注入当前时刻（测试用）；缺省 `datetime.now()`。

    计数口径：``processed`` = 完整行数（停牌等跳过的不进校验，只进日志）——
    「源只给了半截」这条已由 `MIN_BAR_ROWS` 下限在落库前拦下，不会靠
    processed/stored 的差去事后反推。
    """
    now = now or datetime.now()
    # ---- 闸门 1/2：先判日期与收盘时刻，不过就一个包都不发 ----
    snap_day = _snapshot_day(now)
    if snap_day != trade_date:
        raise RuntimeError(
            f"全市场快照属于 {snap_day}，与目标日 {trade_date} 不符 —— "
            "历史日期的日线走回填补，不许拿今天的快照顶替")
    if snap_day == now.strftime("%Y%m%d") and \
            (now.hour, now.minute) < CLOSE_AT:
        raise RuntimeError(
            f"未收盘（{CLOSE_AT[0]:02d}:{CLOSE_AT[1]:02d} 前不落当日行）—— "
            "盘中的半日 OHLC 不许永久入库")

    rows = (fetch or quotes.market_snapshot)()
    if not rows:
        raise RuntimeError("全市场快照回 0 行（源异常）")
    if not any("open" in r for r in rows):
        # 备源同花顺的行根本没有 open/high/low/volume 这几个键。
        raise RuntimeError("快照源不含 OHLC 字段（同花顺备源），待东财恢复后重试")

    bars = [b for b in (_to_bar(r, trade_date) for r in rows) if b]
    if len(bars) < MIN_BAR_ROWS:
        raise RuntimeError(
            f"OHLC 完整的行仅 {len(bars)} 条，低于下限 {MIN_BAR_ROWS}"
            " —— 快照疑似分页不全，进重试")
    inserted = db.bars_upsert("daily_bar", bars)
    logger.info("日线采集 %s：%d 行可落（快照 %d 行），新插 %d 行",
                trade_date, len(bars), len(rows), inserted)
    return bars
