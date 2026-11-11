"""沪深交易所股票列表 —— 上市日期与流通股本。

用处：
  - 上市日期 -> 粗筛剔除次新股（上市不足 N 个交易日）
  - 流通股本 -> 结合最新价得到**精确流通市值**，不依赖东财

接口来自交易所官网（sse.com.cn / szse.cn），带 lru_cache，重复调用零成本。
北交所（约 260 只）无对应接口，其流通市值改用「成交额 / 换手率」反推。
"""
from __future__ import annotations

import bisect
import logging
import re
from datetime import date, datetime
from typing import Dict, Optional

import pandas as pd

from app.fetchers.client import ak, call, norm_code

logger = logging.getLogger(__name__)

_CACHE: Optional[Dict[str, Dict]] = None


def _parse_shares(value) -> Optional[float]:
    """'19,405,684,991' -> 19405684991.0"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[,\s]", "", str(value))
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _load() -> Dict[str, Dict]:
    """合并沪主板 / 科创板 / 深市，返回 {code: {list_date, float_shares, board}}"""
    parts = []

    sh_main = call("上市日期(沪主板)", ak.stock_info_sh_name_code, symbol="主板A股")
    parts.append(pd.DataFrame({
        "code": sh_main["证券代码"].map(norm_code),
        "name": sh_main["证券简称"].astype(str).str.strip(),
        "list_date": sh_main["上市日期"],
        "float_shares": None,
        "board": "sh_main",
    }))

    star = call("上市日期(科创板)", ak.stock_info_sh_name_code, symbol="科创板")
    parts.append(pd.DataFrame({
        "code": star["证券代码"].map(norm_code),
        "name": star["证券简称"].astype(str).str.strip(),
        "list_date": star["上市日期"],
        "float_shares": None,
        "board": "sh_star",
    }))

    sz = call("上市日期(深市)", ak.stock_info_sz_name_code, symbol="A股列表")
    parts.append(pd.DataFrame({
        "code": sz["A股代码"].map(norm_code),
        "name": sz["A股简称"].astype(str).str.strip(),
        "list_date": sz["A股上市日期"],
        "float_shares": sz["A股流通股本"].map(_parse_shares),
        # 形如 "J 金融业"，去掉字母前缀只留中文
        "industry": sz["所属行业"].astype(str)
            .str.replace(r"^[A-Z]\s*", "", regex=True).str.strip(),
        "board": "sz",
    }))

    merged = pd.concat(parts, ignore_index=True)
    merged = merged.drop_duplicates(subset="code", keep="first")

    result: Dict[str, Dict] = {}
    for _, r in merged.iterrows():
        raw = str(r["list_date"] or "")[:10]
        list_date = ""
        for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                list_date = datetime.strptime(raw, fmt).strftime("%Y%m%d")
                break
            except ValueError:
                continue
        industry = r.get("industry")
        result[r["code"]] = {
            "name": r["name"],
            "list_date": list_date,
            "float_shares": r["float_shares"],
            # 沪市该接口不返回行业，留空由前端显示 "-"
            "industry": "" if industry is None or pd.isna(industry) else str(industry),
            "board": r["board"],
        }
    logger.info("股票列表载入 %d 只", len(result))
    return result


def get_listing_info(refresh: bool = False) -> Dict[str, Dict]:
    global _CACHE
    if _CACHE is None or refresh:
        try:
            _CACHE = _load()
        except Exception as exc:  # noqa: BLE001
            logger.error("股票列表获取失败: %s", exc)
            _CACHE = {}
    return _CACHE


def listed_trading_days(list_date: str, on: Optional[str] = None) -> Optional[int]:
    """估算已上市的**交易日**数。

    用交易日历精确计算（约 250 个交易日/年），而非自然日 —— 次新股判断
    要求的是「经历了多少个交易日」，自然日会被假期稀释。

    粗筛会对 4700+ 只股票逐只调用，因此用二分而不是线性扫描
    （线性扫描是 4700 × 10000 次字符串比较，实测多花十几秒）。
    """
    if not list_date or len(list_date) != 8:
        return None
    try:
        from app.fetchers import calendar as trade_calendar
        anchor = on or date.today().strftime("%Y%m%d")
        dates = trade_calendar.trade_dates()
        if not dates:
            return None
        start = bisect.bisect_left(dates, list_date)
        end = bisect.bisect_right(dates, anchor)
        return max(0, end - start)
    except Exception as exc:  # noqa: BLE001
        logger.debug("交易日估算失败: %s", exc)
        return None


def listed_natural_days(list_date: str, on: Optional[str] = None) -> Optional[int]:
    """自然日版本（交易日历不可用时的兜底）。"""
    if not list_date or len(list_date) != 8:
        return None
    try:
        listed = datetime.strptime(list_date, "%Y%m%d").date()
        anchor = (datetime.strptime(on, "%Y%m%d").date() if on else date.today())
        return (anchor - listed).days
    except ValueError:
        return None
