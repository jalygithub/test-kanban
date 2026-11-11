"""涨停 / 炸板 / 跌停 / 强势股池。

数据源：东方财富涨停板行情（push2ex 域名，实测比 push2 家族稳定）。
字段以 akshare 1.18.64 实测为准，见 docs/02-数据源与网络排查.md。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd

from app.fetchers.client import ak, call, norm_code

# 中文列名 -> 内部 snake_case 键名
ZT_COLUMNS = {
    "序号": "rank",
    "代码": "code", "名称": "name", "涨跌幅": "pct_chg", "最新价": "price",
    "成交额": "amount", "流通市值": "float_mv", "总市值": "total_mv",
    "换手率": "turnover", "封板资金": "seal_amount",
    "首次封板时间": "first_seal_time", "最后封板时间": "last_seal_time",
    "炸板次数": "break_count", "涨停统计": "zt_stat", "连板数": "boards",
    "所属行业": "industry",
}
ZB_COLUMNS = {
    "序号": "rank",
    "代码": "code", "名称": "name", "涨跌幅": "pct_chg", "最新价": "price",
    "涨停价": "limit_price", "成交额": "amount", "流通市值": "float_mv",
    "总市值": "total_mv", "换手率": "turnover", "涨速": "speed",
    "首次封板时间": "first_seal_time", "炸板次数": "break_count",
    "涨停统计": "zt_stat", "振幅": "amplitude", "所属行业": "industry",
}
DT_COLUMNS = {
    "序号": "rank",
    "代码": "code", "名称": "name", "涨跌幅": "pct_chg", "最新价": "price",
    "成交额": "amount", "流通市值": "float_mv", "总市值": "total_mv",
    "动态市盈率": "pe_ttm", "换手率": "turnover", "封单资金": "seal_amount",
    "最后封板时间": "last_seal_time", "板上成交额": "on_board_amount",
    "连续跌停": "consecutive_dt", "开板次数": "open_count",
    "所属行业": "industry",
}
STRONG_COLUMNS = {
    "序号": "rank",
    "代码": "code", "名称": "name", "涨跌幅": "pct_chg", "最新价": "price",
    "涨停价": "limit_price", "成交额": "amount", "流通市值": "float_mv",
    "总市值": "total_mv", "换手率": "turnover", "涨速": "speed",
    "是否新高": "is_high", "量比": "vol_ratio", "涨停统计": "zt_stat",
    "入选理由": "reason", "所属行业": "industry",
}


def _to_records(df: pd.DataFrame, mapping: Dict[str, str]) -> List[Dict]:
    """中文列 -> 英文键，未映射的列原样保留；代码统一补零到 6 位。"""
    if df is None or df.empty:
        return []
    rename = {src: dst for src, dst in mapping.items() if src in df.columns}
    out = df.rename(columns=rename)
    records = out.to_dict(orient="records")
    if "code" in out.columns:
        for rec in records:
            rec["code"] = norm_code(rec.get("code"))
    return records


def window_start(now: Optional[datetime] = None) -> str:
    """涨/跌停池还能**联网**取到的最早日期（YYYYMMDD，含当天）。

    akshare `stock_zt_pool_dtgc_em` 用 `datetime.now() - timedelta(days=30)`
    做闸门 —— **自然日**，报错文案却写「30 个交易日」；涨停池虽无客户端
    闸门，实测同窗口外东财直接回空（2026-11-02 补跑 20260929：涨停池
    rows=0、跌停池 0.00s ValueError，而这一跳排在 150s 全市场快照之后，
    白烧 4 分钟才死）。照抄 akshare 的判法，好把「拿不到的日期」在**起跑
    之前**就拒掉，而不是烧完取数才死。
    """
    base = now or datetime.now()
    return (base - timedelta(days=30)).strftime("%Y%m%d")


def get_zt_pool(date: str) -> List[Dict]:
    """涨停股池。date 格式 YYYYMMDD。"""
    df = call(f"涨停池 {date}", ak.stock_zt_pool_em, date=date)
    return _to_records(df, ZT_COLUMNS)


def get_zb_pool(date: str) -> List[Dict]:
    """炸板股池（曾涨停、收盘开板）。"""
    df = call(f"炸板池 {date}", ak.stock_zt_pool_zbgc_em, date=date)
    return _to_records(df, ZB_COLUMNS)


def get_dt_pool(date: str) -> List[Dict]:
    """跌停股池。"""
    df = call(f"跌停池 {date}", ak.stock_zt_pool_dtgc_em, date=date)
    return _to_records(df, DT_COLUMNS)


def get_strong_pool(date: str) -> List[Dict]:
    """强势股池。"""
    df = call(f"强势股池 {date}", ak.stock_zt_pool_strong_em, date=date)
    return _to_records(df, STRONG_COLUMNS)
