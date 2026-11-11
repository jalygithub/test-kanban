"""断板回看 —— 涨停池里「昨天还在、今天没了」的那几只。

## 为什么需要

涨停池 `pool_zt` 只收**当日**涨停股：连板断了，这只票第二天压根不在池子里，
页面上凭空消失。盯连板梯队的人要的恰恰是这条断掉的尾巴 ——「它断了，断了几天，
断之前连到几板」。所以这里回看已归档的历史快照，把断掉的名单**接回当日池子
尾部**（用户拍板：只算连板 ≥2 的、排在正常行后面打标签）。

## 口径（三条，改之前先想清楚）

1. **只认连板 ≥2**。首板断了的每天几百只，全列出来表格会被冲毁；连板梯队
   （`/overview` 的 `ladder`）本来也是 ≥2 才展示，口径对齐。
2. **断 = 当日不在涨停池**。停牌、退市、开板都算断 —— 本页不解释原因，只陈述
   「今天没在板上」这个事实。
3. **连断 3 个交易日就不再显示**（`miss_limit=3`）。断了三天还挂在页面上，已经
   从「刚刚断的连板」变成历史消息，那是复盘该干的事，不是盯盘该干的。

## 为什么只回看 `miss_limit - 1` 个归档日

`miss` 从今天倒着数：今天缺席算第 1 次。所以 `miss < miss_limit` 意味着这只票的
**最近一次上榜**只能落在紧邻的前 `miss_limit - 1` 格 —— 再往前的日期无论怎么翻
都不可能进名单。回看窗口按这个上界截断，`db.load` 从 30 次降到 2 次。

## 数据诚实

断板行的行情字段（现价、涨跌幅、封单额……）是**断板那天**的，不是今天的。带出来
只会让人拿旧价当现价看，所以一律不带，前端渲染成「—」。留下的只有身份与连板痕迹：
`boards` 冻结在断板前的值、`miss_days`、`last_date`。
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from app.store import db

logger = logging.getLogger(__name__)

__all__ = ["broken_rows", "MISS_LIMIT", "MIN_BOARDS"]

#: 连续缺席多少个交易日就不再显示。用户拍板：断 3 次起不再列。
MISS_LIMIT = 3
#: 只回看连板 ≥2 —— 与总览「连板梯队」同一口径。
MIN_BOARDS = 2

# 断板行只保留身份与连板痕迹，行情字段一律不带（理由见模块 docstring）。
_KEEP = ("code", "name", "zt_stat", "industry")


def _boards(row: Dict) -> int:
    try:
        return int(row.get("boards") or 0)
    except (TypeError, ValueError):
        return 0


def _pretty(date: str) -> str:
    """`20261012` → `2026-10-12`（展示格式统一放后端，前端不再二次拼）。"""
    d = str(date)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def broken_rows(date: Optional[str],
                min_boards: int = MIN_BOARDS,
                miss_limit: int = MISS_LIMIT) -> List[Dict]:
    """`date`（YYYYMMDD）当天的断板名单：**断板前连板数降序，同板级断得越近越靠前**。

    结构与 `pool_zt` 行一致，外加 `broken=True` / `miss_days` / `last_date`。
    取不到历史、只有当天、参数不对，一律回空表 —— 断板是锦上添花，缺席不该让
    `/api/limit` 挂掉。
    """
    if not date or miss_limit < 2 or min_boards < 1:
        return []

    # `list_dates` 返回 **desc**，取 `miss_limit + 2` 留够余量再截断。
    dates = [d for d in db.list_dates("pool_zt", miss_limit + 2) if d <= date]
    if len(dates) < 2:
        return []
    prev = [d for d in dates if d < date][:miss_limit - 1]
    if not prev:
        return []

    today_rows = db.load("pool_zt", date)
    if today_rows is None:
        # 断板是**相对当日池子**说的：那天压根没归档就没有「今日不在池里」这回事，
        # 拿一个不存在的日期去比历史，会凭空造出一整张断板名单。
        return []

    pools: Dict[str, List[Dict]] = {}
    for d in prev:
        loaded = db.load("pool_zt", d)
        if loaded is not None:      # None = 那天没归档，**不是**「那天没人涨停」
            pools[d] = loaded

    today = {str(r.get("code") or "") for r in today_rows}

    # 最近一次上榜（**任意** boards）：desc 遍历下先到先得，拿到的就是最近一次。
    # 首板后隔天断的，最近一次 `boards=1`，被下面的阈值筛掉 —— 那是首板断板。
    last_any: Dict[str, str] = {}
    for d in prev:
        for r in pools.get(d) or []:
            code = str(r.get("code") or "").strip()
            if code and code not in last_any:
                last_any[code] = d

    # `d0` 之后的每个归档日它都不在池里（「最近一次」的定义），再加今天，所以
    # `miss` = 今天的 1 次 + `d0` 之后的归档日数 —— desc 序里正是它的下标 + 1。
    out: List[Dict] = []
    for code, d0 in last_any.items():
        if code in today:
            continue
        row = next((r for r in pools.get(d0) or []
                    if str(r.get("code") or "").strip() == code), None)
        if row is None or _boards(row) < min_boards:
            continue
        miss = prev.index(d0) + 1
        if miss >= miss_limit:
            continue
        item: Dict = {k: row.get(k) for k in _KEEP if row.get(k) is not None}
        item["code"] = code
        item["boards"] = _boards(row)
        item["broken"] = True
        item["miss_days"] = miss
        item["last_date"] = _pretty(d0)
        out.append(item)

    out.sort(key=lambda r: (-(r.get("boards") or 0), r.get("miss_days") or 0))
    logger.info("断板回看 %s: %d 只（连板≥%d、断 1~%d 天）",
                date, len(out), min_boards, miss_limit - 1)
    return out
