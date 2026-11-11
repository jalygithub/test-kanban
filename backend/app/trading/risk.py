# -*- coding: utf-8 -*-
"""风控层 —— 独立审核信号与数量，只出结论，不碰账户、不碰订单。

与策略层刻意分开：策略说「想买 3000 股」，风控回答「批 / 不批、批多少、
为什么」。审核不通过的信号也会**留痕**（service 落成 REJECTED 订单并写
`reject_reason`），保证每个 BUY/SELL 意图都有去向，不静默丢弃。

复核是**双保险**：策略层已按信号时点算过仓位/现金，这里用同一套约束再过
一遍（参数可能被调用方覆盖），执行时 paper 层还有第三道（实际现金、T+1
可卖量、当日锁定）—— 三层各查各的时点，任何一层失手另外两层兜底。
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def review(signal: Dict[str, Any], account: Dict[str, Any],
           params: Dict[str, Any]) -> Dict[str, Any]:
    """审核一条 BUY/SELL 信号。

    :param account: {
        "cash": 账户现金, "available": 可用现金（含本 run 预计卖出回款）,
        "equity": 权益, "exposure": 当前总仓位占比,
        "position_count": 持仓数, "positions": {code: pos},
        "pending_buy": set(在途买单 code), "risk_state": NORMAL|RISK,
        "exec_date": 预定执行日或 None}
    :return: {"approved": bool, "qty": int|None, "rule": str|None,
              "detail": str|None}  —— 不批时 rule/detail 即拒绝原因。
    """
    side = signal.get("side")
    qty = signal.get("target_qty")
    code = str(signal.get("code") or "")
    exec_date = account.get("exec_date")

    if side not in ("BUY", "SELL"):
        return {"approved": False, "qty": None,
                "rule": "not_orderable",
                "detail": "HOLD 不产生订单"}
    if qty is None or int(qty) <= 0:
        return {"approved": False, "qty": None, "rule": "invalid_qty",
                "detail": "目标股数缺失或非正：%r" % (qty,)}
    qty = int(qty)
    if side == "BUY" and qty % 100 != 0:
        return {"approved": False, "qty": qty, "rule": "lot_size",
                "detail": "A 股买入须为 100 股整数倍，收到 %d" % qty}

    est = signal.get("est_price")
    if est is None or float(est) <= 0:
        return {"approved": False, "qty": qty, "rule": "invalid_price",
                "detail": "参考价缺失或非正：%r" % (est,)}
    est = float(est)

    if side == "BUY":
        return _review_buy(code, qty, est, account, params, signal)
    return _review_sell(code, qty, account, exec_date, signal)


def _review_buy(code: str, qty: int, est: float,
                account: Dict[str, Any], p: Dict[str, Any],
                signal: Dict[str, Any]) -> Dict[str, Any]:
    equity = float(account.get("equity") or 0.0)

    if account.get("risk_state") == "RISK":
        return {"approved": False, "qty": qty, "rule": "risk_gate",
                "detail": "数据风险状态，禁止新开仓"}
    if code in account.get("positions", {}) and not p["allow_pyramid"]:
        return {"approved": False, "qty": qty, "rule": "duplicate_position",
                "detail": "已持有该标的且未开加仓"}
    if code in account.get("pending_buy", set()):
        return {"approved": False, "qty": qty, "rule": "duplicate_order",
                "detail": "已有有效买入订单，不重复下单"}

    pos_count = int(account.get("position_count") or 0)
    if pos_count >= p["max_positions"]:
        return {"approved": False, "qty": qty, "rule": "position_cap",
                "detail": "持仓数 %d 已达上限 %d"
                          % (pos_count, p["max_positions"])}

    buy_value = qty * est
    exposure = float(account.get("exposure") or 0.0)
    if equity > 0 and exposure * equity + buy_value \
            > p["max_total_exposure"] * equity + 1e-6:
        return {"approved": False, "qty": qty, "rule": "exposure_cap",
                "detail": "买入后总仓位 %.0f%% 超上限 %.0f%%"
                          % ((exposure * equity + buy_value) / equity * 100,
                             p["max_total_exposure"] * 100)}
    if equity > 0 and buy_value > p["max_position_pct"] * equity + 1e-6:
        return {"approved": False, "qty": qty, "rule": "position_pct_cap",
                "detail": "单股 %.0f 元超上限 %.0f%% 权益"
                          % (buy_value, p["max_position_pct"] * 100)}

    amount = buy_value * (1.0 + p["slippage"])
    fee = max(amount * p["commission_rate"], p["min_commission"])
    need = amount + fee
    available = float(account.get("available") or account.get("cash") or 0.0)
    if available < need:
        return {"approved": False, "qty": qty, "rule": "cash_insufficient",
                "detail": "可用现金 %.2f < 买入所需 %.2f（含费）"
                          % (available, need)}

    # 单股上限也可能把 qty 卡在「方向对但数量超了」—— 策略已按上限算，
    # 这里兜住外部直接传 qty 的调用路径。
    return {"approved": True, "qty": qty, "rule": None, "detail": None}


def _review_sell(code: str, qty: int, account: Dict[str, Any],
                 exec_date: Optional[str], signal: Dict[str, Any]
                 ) -> Dict[str, Any]:
    pos = account.get("positions", {}).get(code)
    if not pos:
        return {"approved": False, "qty": qty, "rule": "no_position",
                "detail": "无持仓可卖"}
    held = int(pos.get("qty") or 0)
    locked = int(pos.get("locked_qty") or 0)
    if qty > held:
        return {"approved": False, "qty": qty, "rule": "over_position",
                "detail": "卖出 %d 股超过持有 %d 股" % (qty, held)}
    sellable = held - locked
    if qty > sellable:
        return {"approved": False, "qty": qty, "rule": "t1_locked",
                "detail": "T+1：可卖 %d 股 < 卖出 %d 股（冻结 %d 股）"
                          % (sellable, qty, locked)}
    if exec_date and str(pos.get("entry_date") or "") >= str(exec_date):
        return {"approved": False, "qty": qty, "rule": "t1_same_day",
                "detail": "T+1：入场日 %s 不得在 %s 当日卖出"
                          % (pos.get("entry_date"), exec_date)}
    return {"approved": True, "qty": qty, "rule": None, "detail": None}
