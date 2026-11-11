"""板块资金流。

主源：同花顺（data.10jqka.com.cn）—— 实测不受东财限流影响，字段更全。
备源：东方财富（push2 家族）—— 主源失败时回退。

注意：两个源的板块分类口径不完全一致，同一次复盘内不要混用。
"""

from __future__ import annotations

from typing import Dict, List

import pandas as pd

from app.fetchers.client import ak, call

FLOW_COLUMNS = {
    "序号": "rank", "行业": "name", "行业指数": "index",
    "行业-涨跌幅": "pct_chg", "流入资金": "inflow",
    "流出资金": "outflow", "净额": "net_inflow",
    "公司家数": "stock_count", "领涨股": "leading_stock",
    "领涨股-涨跌幅": "leading_pct_chg", "当前价": "leading_price",
}


def _to_records(df: pd.DataFrame, mapping: Dict[str, str] = FLOW_COLUMNS) -> List[Dict]:
    if df is None or df.empty:
        return []
    rename = {src: dst for src, dst in mapping.items() if src in df.columns}
    return df.rename(columns=rename).to_dict(orient="records")


def _primary(kind: str) -> List[Dict]:
    if kind == "industry":
        df = call("行业板块资金流(同花顺)", ak.stock_fund_flow_industry)
    else:
        df = call("概念板块资金流(同花顺)", ak.stock_fund_flow_concept)
    return _to_records(df)


def _fallback(kind: str) -> List[Dict]:
    indicator = "今日"
    df = call(f"板块资金流(东财,{kind})", ak.stock_sector_fund_flow_rank,
              indicator=indicator)
    return _to_records(df)


def get_sector_flow(kind: str = "industry") -> List[Dict]:
    """kind: industry | concept。主源失败自动回退到东财。"""
    try:
        return _primary(kind)
    except Exception as primary_exc:  # noqa: BLE001
        try:
            return _fallback(kind)
        except Exception as fallback_exc:  # noqa: BLE001
            raise RuntimeError(
                f"板块资金流主备源均失败: 主源={primary_exc}; 备源={fallback_exc}"
            ) from fallback_exc


def get_sector_members(board_name: str, kind: str = "industry") -> List[Dict]:
    """板块成分股，按需加载。"""
    fn = (ak.stock_board_industry_cons_em if kind == "industry"
          else ak.stock_board_concept_cons_em)
    df = call(f"成分股 {board_name}", fn, symbol=board_name)
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


def get_sector_hist(board_name: str, kind: str = "industry") -> pd.DataFrame:
    """板块历史资金流，用于「近 N 日连续流入」。"""
    if kind == "industry":
        df = call(f"板块资金流历史 {board_name}",
                  ak.stock_sector_fund_flow_hist, symbol=board_name)
    else:
        df = call(f"概念资金流历史 {board_name}",
                  ak.stock_concept_fund_flow_hist, symbol=board_name)
    return df
