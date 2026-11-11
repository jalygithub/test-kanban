"""交易日历。

任何需要「最近交易日」的场景都走这里，不要用 date.today() 硬算 ——
节假日、调休都会算错。日历缓存到本地，避免每次开局都请求。
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import List, Optional

from app.config import DATA_DIR
from app.fetchers.client import ak, call

logger = logging.getLogger(__name__)

_CACHE = DATA_DIR / "trade_calendar.json"
_MAX_AGE_DAYS = 30


def _load_cache() -> Optional[List[str]]:
    try:
        if not _CACHE.exists():
            return None
        age = datetime.now().timestamp() - _CACHE.stat().st_mtime
        if age > _MAX_AGE_DAYS * 86400:
            return None
        return json.loads(_CACHE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def trade_dates() -> List[str]:
    """全部交易日（YYYYMMDD），带本地缓存。"""
    cached = _load_cache()
    if cached:
        return cached
    df = call("交易日历", ak.tool_trade_date_hist_sina)
    dates = sorted({d.strftime("%Y%m%d") for d in
                    (date.fromisoformat(str(x)) for x in df["trade_date"])})
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(dates), encoding="utf-8")
    logger.info("交易日历已更新 %d 条", len(dates))
    return dates


def cached_trade_dates() -> List[str]:
    """只读本地缓存里的交易日，**无视有效期、绝不联网**。

    `trade_dates()` 的缓存一过 30 天就会去新浪拉日历。矩阵视图只是想把
    「没跑任务的交易日」也列成一列，犯不上为它触发一次外网请求 ——
    内网环境下会被安全设备拦截并留下记录。拿不到就返回空列表，
    调用方退化成只列有数据的日期。
    """
    try:
        if not _CACHE.exists():
            return []
        data = json.loads(_CACHE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return []
        return sorted({str(d) for d in data})
    except Exception:  # noqa: BLE001
        logger.warning("读取交易日历缓存失败，退回只列已有日期", exc_info=True)
        return []


def last_trade_date(today: Optional[date] = None) -> str:
    """最近一个已收盘的交易日（含今天，若今天是交易日）。"""
    today = today or date.today()
    dates = trade_dates()
    eligible = [d for d in dates if d <= today.strftime("%Y%m%d")]
    if not eligible:
        raise RuntimeError("交易日历中没有 <= 今天的交易日")
    return eligible[-1]


def prev_trade_date(today: Optional[date] = None, n: int = 1) -> str:
    """往前数第 n 个交易日。"""
    today = today or date.today()
    dates = [d for d in trade_dates() if d < today.strftime("%Y%m%d")]
    if len(dates) < n:
        raise RuntimeError("交易日历数据不足")
    return dates[-n]


def is_trade_day(day: Optional[str] = None) -> bool:
    day = day or date.today().strftime("%Y%m%d")
    return day in set(trade_dates())
