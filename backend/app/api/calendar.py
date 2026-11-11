"""交易日查询接口。"""

from __future__ import annotations

from datetime import date
from typing import Any, Dict

from fastapi import APIRouter, Query

from app.fetchers import calendar

router = APIRouter(prefix="/api/trade-calendar", tags=["trade-calendar"])


@router.get("")
def get_trade_calendar(limit: int = Query(30, ge=1, le=500)) -> Dict[str, Any]:
    dates = calendar.trade_dates()
    today = date.today().strftime("%Y%m%d")
    recent = [d for d in dates if d <= today][-limit:]
    return {
        "today": today,
        "last_trade_date": calendar.last_trade_date(),
        "is_trade_day": calendar.is_trade_day(),
        "dates": recent,
    }
