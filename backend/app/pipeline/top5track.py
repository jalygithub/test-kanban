# -*- coding: utf-8 -*-
"""「疑似主力建仓前5」跟踪（2026-11-11 拍板的五个口径）。

1. 一个月 = **30 自然日**，截止 = 最后一次进前5那天 + 30 天；
2. 「前5」= 当日 `main_force` 榜（top30）里 rank 1-5；
3. 到期票**留在名单**标「已到期」，不再拉股价；
4. 新 tab 点代码弹框与矩阵一致（股价 + 上榜名次双轴，前端复用）；
5. 股价落 `daily_bar` 复用单一出处（`bars_upsert` 不复权、不覆盖）。

两个函数，一读一写：

* ``roster()`` —— 扫 ``main_force`` 快照推导名单：首次/最后一次进前5、
  截止日、状态。**零联网**，纯读本地库；重复上榜顺延截止日是推导出来的
  （last_date 变了 deadline 自然变），不落任何状态表 —— 落表就得记「谁
  在什么时候顺延过」，而快照本身就是唯一事实。
* ``pull(target)`` —— 盘后对「跟踪中」的票补拉缺日线（联网，挂在
  ``scheduler._collect`` 的 ``top5_track`` 步，排在打分之后：名单由当天
  打分推导）。只拉本地**缺**的交易日，一天不缺一次请求都不发；单票失败
  计数不拖垮，全部失败才抛（让步骤进 failed，不许静默全绿）。失败留下的
  缺口第二天照拉，天然自愈，不进重试域。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List

from app.fetchers import calendar as tcal
from app.pipeline.backfill import fetch_daily
from app.store import db

logger = logging.getLogger(__name__)

#: 跟踪期：30 **自然日**（与涨跌停池 30 天窗口同口径；不算交易日）
TRACK_DAYS = 30

#: 快照扫多少天。留大数：名单要覆盖「很久以前进过前5、至今还在跟踪」的票，
#: 上限截断会让老票的第一天凭空消失。main_force 每天一条，10 万等于全量。
_SNAP_LIMIT = 100_000


def _deadline(last_date: str) -> str:
    """截止日 = 最后一次进前5那天 + 30 自然日。"""
    d = datetime.strptime(last_date, "%Y%m%d") + timedelta(days=TRACK_DAYS)
    return d.strftime("%Y%m%d")


def roster(as_of: str | None = None) -> List[Dict[str, Any]]:
    """扫 ``main_force`` 快照推导前5跟踪名单（零联网）。

    每行：``code/name/industry``、``first_date/last_date``（**只算 rank≤5**
    的日子）、``hits``（进前5次数）、``best_rank``、``cells``（**全部**上榜
    格，含 6-30 名 —— 弹框与矩阵同口径，进前5前的名次也画）、
    ``deadline``（= last_date + 30 自然日）、``status``（跟踪中 / 已到期）。

    到期票**留在名单**（口径 3），只标状态不删行；``as_of`` 只影响状态判定，
    缺省取今天。排序：跟踪中在前，各组内 last_date 降序。
    """
    as_of = as_of or datetime.now().strftime("%Y%m%d")
    dates = sorted(db.list_dates("main_force", limit=_SNAP_LIMIT))
    out: Dict[str, Dict[str, Any]] = {}
    for d in dates:
        # 与矩阵同口径：只取 rows + disclaimer，绕开 81% 的 all_rows
        block = db.load_one_fields("main_force", d, text_fields=("disclaimer",))
        if not block:
            continue
        for r in block.get("rows") or []:
            if not isinstance(r, dict):
                continue
            code = str(r.get("code") or "").strip()
            rank = r.get("rank")
            if not code or not isinstance(rank, int):
                continue
            rec = out.get(code)
            if rec is None:
                rec = out[code] = {
                    "code": code, "name": "", "industry": "",
                    "first_date": "", "last_date": "",
                    "hits": 0, "best_rank": 0,
                    "cells": {}, "deadline": "", "status": "已到期",
                }
            rec["cells"][d] = {
                "rank": rank,
                "score": round(r["score"], 2)
                if isinstance(r.get("score"), (int, float)) else None,
            }
            if r.get("name"):
                rec["name"] = str(r["name"])
            if r.get("industry"):
                rec["industry"] = str(r["industry"])
            if rank > 5:
                continue  # 进前5之前的名次只进 cells，不进跟踪口径
            rec["hits"] += 1
            if not rec["first_date"] or d < rec["first_date"]:
                rec["first_date"] = d
            if d > rec["last_date"]:
                rec["last_date"] = d
            if not rec["best_rank"] or rank < rec["best_rank"]:
                rec["best_rank"] = rank

    rows = [r for r in out.values() if r["first_date"]]
    for r in rows:
        r["deadline"] = _deadline(r["last_date"])
        # 含等号：截止日当天还在跟踪期内
        r["status"] = "跟踪中" if as_of <= r["deadline"] else "已到期"
    # 与矩阵同款的链式稳定排序：跟踪中在前，组内 last_date 降序
    rows.sort(key=lambda r: r["best_rank"])
    rows.sort(key=lambda r: r["last_date"], reverse=True)
    rows.sort(key=lambda r: 0 if r["status"] == "跟踪中" else 1)
    return rows


def pull(target: str) -> Dict[str, Any]:
    """盘后补拉跟踪票的**缺**日线（联网步骤，挂 ``_collect`` 的 top5_track）。

    只拉「跟踪中」的票（口径 3：到期不再拉）；区间 [first_date, target] 内
    只发**本地缺**的交易日那一段 —— 已有数据靠 ``bars_upsert`` 的
    ON CONFLICT DO NOTHING 兜底，绝不用 ``bars_overwrite``（不覆盖未经确认
    的历史行情）。返回 ``{tracking, expired, fetched, rows, failed}``：
    ``fetched`` = **实际发了请求且取回**的票数（数据本就齐的票不计，否则
    「一个包都没发」也会报取价 2 只）；``rows`` = 新落库行数。全军覆没才抛，
    部分失败只记进 ``failed`` 并打日志。
    """
    all_rows = roster()
    active = [r for r in all_rows if r["status"] == "跟踪中"]
    info: Dict[str, Any] = {
        "tracking": len(active), "expired": len(all_rows) - len(active),
        "fetched": 0, "rows": 0, "failed": [],
    }
    if not active:
        return info

    # 日历只读本地文件（与 matrix 端点同口径）；文件坏了退化成「整段补一次」
    cal = [d for d in tcal.cached_trade_dates() if d <= target]
    fetched = 0     # 真正发了请求且取回的票
    done = 0        # 该票已齐（本就全 or 拉回来了）—— 全败判定用它
    inserted = 0
    for rec in active:
        code, start = rec["code"], rec["first_date"]
        have = set(db.bars_dates("daily_bar", code))
        if cal:
            missing = [d for d in cal if start <= d and d not in have]
            if not missing:
                done += 1
                continue  # 一天不缺，一次请求都不发
            s, e = missing[0], missing[-1]
        else:
            s, e = start, target
        try:
            bars = fetch_daily(code, s, e)  # 不复权（ADJUST="" 在 backfill 钉死）
        except Exception as exc:  # noqa: BLE001  单票失败计数，不拖垮整步
            info["failed"].append(f"{code}: {type(exc).__name__}: {exc}")
            continue
        if not bars:
            info["failed"].append(f"{code}: 源返回 0 行")
            continue
        inserted += db.bars_upsert("daily_bar", bars)
        fetched += 1
        done += 1

    info.update(fetched=fetched, rows=inserted)
    if info["failed"]:
        logger.warning("前5跟踪取价部分失败 %d/%d：%s", len(info["failed"]),
                       len(active), "；".join(info["failed"][:5]))
    if done == 0:
        # 全军覆没必须抛：静默返回会让任务记 success，却一个价都没拉到
        raise RuntimeError(
            f"前5跟踪取价 {len(active)} 只全部失败："
            + "；".join(info["failed"][:5]))
    return info
