# -*- coding: utf-8 -*-
"""模拟撮合与记账 —— 账户、持仓、现金**只能**由本模块的成交流程改写。

边界（任务书第五条的落点）：

- 策略只产交易意图（trade_signal），风控只出结论（risk.review），
  两边都不碰 sim_account / sim_position；
- 成交判定在**下一交易日开盘价**上做：信号日收盘后生成，D+1 执行 ——
  拿当日收盘后才知道的信息假设当日收盘价成交是未来数据泄漏，禁止；
- 涨停买不进 / 跌停卖不出 / 停牌无行情：一律 REJECTED + 原因，
  **不伪造成交**；涨跌停判定直接引用 research.engine 的唯一实现
  （`limit_ratio` / `_at_limit`），不另写一套规则；
- 幂等三道闸：run 唯一键（策略层）、order.signal_id UNIQUE、
  sim_fill.order_id UNIQUE（记账层）—— 重复执行一个字节都多记不了。

执行日行情还没入库时整个执行**拒绝开跑**（ran=False）：没有行情就没有
成交价，这正是「无法成交的订单必须记录原因」的运行级形态。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.research.engine import _at_limit, limit_ratio  # 规则事实唯一实现
from app.store import db

logger = logging.getLogger(__name__)

BAR_TABLE = "daily_bar"


def place(signal: Dict[str, Any], review_res: Dict[str, Any],
          exec_date: Optional[str]) -> Optional[int]:
    """把风控结论落成订单。返回订单 id；该信号已有单 → None（幂等）。"""
    if review_res.get("approved"):
        status, reason = "PENDING", None
    else:
        status = "REJECTED"
        reason = "%s: %s" % (review_res.get("rule"), review_res.get("detail"))
    return db.sim_order_place(
        signal_id=int(signal["id"]), code=str(signal["code"]),
        name=signal.get("name"), side=str(signal["side"]),
        qty=int(signal.get("target_qty") or 0), status=status,
        exec_date=exec_date, est_price=signal.get("est_price"),
        reject_reason=reason)


def execute(exec_date: Optional[str] = None) -> Dict[str, Any]:
    """执行待处理订单：解锁 → 卖单 → 买单，全部在 D+1 开盘价上撮合。

    ``exec_date`` 缺省取**最早的待执行日**（补跑一次即可，不猜今天几号）。
    """
    pending_all = [o for o in db.trading_orders_pending()
                   if o.get("exec_date")]
    if exec_date is None:
        if not pending_all:
            return {"ran": False, "exec_date": None,
                    "reason": "没有待执行订单", "filled": 0, "rejected": 0,
                    "expired": 0, "details": []}
        exec_date = min(o["exec_date"] for o in pending_all)

    exec_date = str(exec_date)
    dates = set(db.bars_dates(BAR_TABLE))
    if exec_date not in dates:
        return {"ran": False, "exec_date": exec_date,
                "reason": "执行日行情未入库，无法确定成交价（不成交、不记账）",
                "filled": 0, "rejected": 0, "expired": 0, "details": []}

    # 错过执行日的单不追补 —— 拿后面的行情补成交就是错价成交。
    expired = [o for o in pending_all
               if o["exec_date"] < exec_date]
    for o in expired:
        db.sim_order_set(int(o["id"]), "EXPIRED",
                         reject_reason="执行日已过仍未执行：错过 D+1 不追补，"
                                       "防止按错误价格成交")

    todo = [o for o in pending_all if o["exec_date"] == exec_date]
    # T+1 日切：条件式解锁（locked_date < exec_date），幂等。
    db.sim_positions_unlock(exec_date)

    prev_date = db.bars_prev_date(BAR_TABLE, exec_date)
    # 卖单在前：释放的资金同一日就能给买单用，与券商资金可用口径一致。
    todo.sort(key=lambda o: (0 if o["side"] == "SELL" else 1, int(o["id"])))

    details: List[Dict[str, Any]] = []
    filled = rejected = 0
    for order in todo:
        res = _match_one(order, exec_date, prev_date)
        details.append(res)
        if res["status"] == "FILLED":
            filled += 1
        elif res["status"] == "REJECTED":
            rejected += 1

    return {"ran": True, "exec_date": exec_date, "filled": filled,
            "rejected": rejected, "expired": len(expired),
            "details": details}


def _match_one(order: Dict[str, Any], exec_date: str,
               prev_date: Optional[str]) -> Dict[str, Any]:
    """单笔撮合。任何异常分支都落 REJECTED + 原因，绝不静默当成交。"""
    order_id = int(order["id"])
    code = str(order["code"])
    qty = int(order["qty"])
    p = order.get("run_params") or {}
    slip = float(p.get("slippage", 0.0))
    comm_rate = float(p.get("commission_rate", 0.0))
    min_comm = float(p.get("min_commission", 0.0))
    stamp_rate = float(p.get("stamp_rate", 0.0))

    def _reject(reason: str) -> Dict[str, Any]:
        db.sim_order_set(order_id, "REJECTED", reject_reason=reason)
        return {"order_id": order_id, "code": code, "side": order["side"],
                "qty": qty, "status": "REJECTED", "price": None, "fee": None,
                "reason": reason}

    bar = db.bars_row(BAR_TABLE, code, exec_date)
    if not bar:
        return _reject("执行日无行情（停牌或数据缺失），不伪造成交")
    open_px = bar.get("open")
    if open_px is None or float(open_px) <= 0:
        return _reject("执行日缺开盘价，无法定价")

    prev_close = None
    if prev_date:
        prev_row = db.bars_row(BAR_TABLE, code, prev_date)
        if prev_row:
            prev_close = prev_row.get("close")

    if order["side"] == "BUY":
        if _at_limit(open_px, prev_close, code, up=True):
            return _reject("涨停开盘无法买入（%s，前收 %s）"
                           % (exec_date, prev_close))
        px = round(float(open_px) * (1.0 + slip), 2)
        amount = px * qty
        fee = max(amount * comm_rate, min_comm)
        cost = amount + fee
        acct = db.sim_account_get()
        if not acct:
            return _reject("账户未初始化，无法记账")
        if float(acct["cash"]) < cost:
            return _reject("现金不足（需 %.2f，有 %.2f），执行时复核拒单"
                           % (cost, float(acct["cash"])))
        old = db.sim_position_get(code)
        if old:
            held = int(old["qty"])
            qty_new = held + qty
            spec = {
                "code": code, "name": order.get("name") or old.get("name"),
                "qty": qty_new,
                "locked_qty": int(old.get("locked_qty") or 0) + qty,
                "locked_date": exec_date,
                "avg_cost": (float(old["avg_cost"]) * held + px * qty) / qty_new,
                "cost_factor": old.get("cost_factor"),
                "entry_date": old.get("entry_date") or exec_date,
            }
        else:
            spec = {
                "code": code, "name": order.get("name"), "qty": qty,
                "locked_qty": qty, "locked_date": exec_date,
                "avg_cost": px, "cost_factor": bar.get("adj_factor"),
                "entry_date": exec_date,
            }
        ok = db.sim_settle(
            order_id,
            {"code": code, "side": "BUY", "qty": qty, "price": px,
             "fee": fee, "cash_delta": -cost, "trade_date": exec_date},
            spec)
        if not ok:
            db.sim_order_set(order_id, "FILLED", fill_price=px, fee=fee)
            return {"order_id": order_id, "code": code, "side": "BUY",
                    "qty": qty, "status": "FILLED", "price": px, "fee": fee,
                    "reason": "已有成交记录（幂等命中），未重复记账"}
        return {"order_id": order_id, "code": code, "side": "BUY", "qty": qty,
                "status": "FILLED", "price": px, "fee": fee, "reason": None}

    # ---- SELL ----
    pos = db.sim_position_get(code)
    if not pos:
        return _reject("无持仓可卖（信号生成后持仓已变化）")
    if str(pos.get("entry_date") or "") >= exec_date:
        return _reject("T+1：入场日 %s 当日买入不可当日卖出"
                       % pos.get("entry_date"))
    sellable = int(pos["qty"]) - int(pos.get("locked_qty") or 0)
    if qty > sellable:
        return _reject("T+1：可卖 %d 股 < 卖出 %d 股（冻结 %d 股）"
                       % (sellable, qty, int(pos.get("locked_qty") or 0)))
    if _at_limit(open_px, prev_close, code, up=False):
        return _reject("跌停开盘无法卖出（%s，前收 %s）"
                       % (exec_date, prev_close))
    px = round(float(open_px) * (1.0 - slip), 2)
    amount = px * qty
    fee = max(amount * comm_rate, min_comm) + amount * stamp_rate
    qty_new = int(pos["qty"]) - qty
    if qty_new <= 0:
        spec = None  # 清仓：sim_settle 里删持仓行
    else:
        locked_new = min(int(pos.get("locked_qty") or 0), qty_new)
        spec = {
            "code": code, "name": pos.get("name"), "qty": qty_new,
            "locked_qty": locked_new,
            "locked_date": pos.get("locked_date") if locked_new else None,
            "avg_cost": float(pos["avg_cost"]),
            "cost_factor": pos.get("cost_factor"),
            "entry_date": pos.get("entry_date"),
        }
    ok = db.sim_settle(
        order_id,
        {"code": code, "side": "SELL", "qty": qty, "price": px,
         "fee": fee, "cash_delta": amount - fee, "trade_date": exec_date},
        spec)
    if not ok:
        db.sim_order_set(order_id, "FILLED", fill_price=px, fee=fee)
        return {"order_id": order_id, "code": code, "side": "SELL",
                "qty": qty, "status": "FILLED", "price": px, "fee": fee,
                "reason": "已有成交记录（幂等命中），未重复记账"}
    return {"order_id": order_id, "code": code, "side": "SELL", "qty": qty,
            "status": "FILLED", "price": px, "fee": fee, "reason": None}


def limit_ratio_of(code: str) -> float:
    """对策略/测试暴露涨跌停幅度（规则事实引用，不复制）。"""
    return limit_ratio(code)
