# -*- coding: utf-8 -*-
"""规则策略 V1：只产交易意图，不碰账户、不碰订单。

输入输出契约（service 组装 ctx、本模块纯计算，便于离线测试）::

    ctx = {
        "signal_date": "20260107",          # 信号日（收盘后数据）
        "exec_date": "20260108" | None,     # 预定执行日（下一交易日）
        "snapshot_date": "20260107",        # 六维快照日期
        "rows": [...],                      # 六维榜行（含 rank/score/name…）
        "bars": {code: {date: row}},        # **已截断 <= signal_date** 的行情
        "ref_dates": ["…"],                 # 窗口交易日升序，末项 == signal_date
        "positions": [sim_position dict],
        "cash": float,
        "pending_buy_codes": set,           # 已有有效买单的标的
        "params": {...normalized...},
        "param_version": "…",
        "risk_state": "NORMAL" | "RISK",
    }

输出::

    {"status": "OK" | "REFUSED", "refusal_reason": str|None,
     "signals": [{"code","name","side","signal_date","exec_date",
                  "reasons":[{"rule","detail"}],"target_qty","alloc_pct",
                  "est_price","rule_version","param_version",
                  "snapshot_date","bar_date"}]}

三条贯穿约束：

1. **不强行给信号** —— 候选不足、行情缺失、数据过期、条件不满足，一律
   HOLD（落行留痕）或 REFUSED（运行级拒绝），绝不硬凑 BUY。
2. **退出规则逐条独立配置、独立记因** —— 一个持仓可同时命中多条，全部
   写进 reasons；没触发就 HOLD 并写明「检查过什么」。
3. **风险状态只禁新开仓**（默认），持仓照常走退出规则；是否强平由
   `risk_force_exit` 显式配置，不藏在代码里。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.trading.params import RULE_VERSION

#: 买入过滤的通过项/拦截项按此顺序求值（先全局风险门，后逐票条件），
#: 顺序固定 = 同一输入必得同一批 reasons，测试才锚得住。
AMOUNT_WINDOW = 5  # 流动性窗口：近 5 日日均成交额


def _bar_of(ctx: Dict[str, Any], code: str, day: str) -> Optional[Dict[str, Any]]:
    return ctx["bars"].get(code, {}).get(day)


def _closes_before(ctx: Dict[str, Any], code: str, day: str,
                   n: int) -> Optional[List[float]]:
    """含 ``day`` 在内、往前数 n 个交易日的收盘序列；任何缺口回 None。

    窗口内有缺日（停牌/数据缺失）就不给 MA —— 拿残窗算出的均线看着能用，
    实际是拿 12 天冒充 20 天，属于替数据做决定。
    """
    ref = ctx["ref_dates"]
    if day not in ref:
        return None
    idx = ref.index(day)
    window = ref[idx - n + 1:idx + 1] if idx - n + 1 >= 0 else ref[0:idx + 1]
    if len(window) < n:
        return None
    closes: List[float] = []
    for d in window:
        bar = _bar_of(ctx, code, d)
        if not bar or bar.get("close") is None:
            return None
        closes.append(float(bar["close"]))
    return closes


def _ma(ctx: Dict[str, Any], code: str, day: str, n: int) -> Optional[float]:
    closes = _closes_before(ctx, code, day, n)
    if closes is None:
        return None
    return sum(closes) / n


def _avg_amount(ctx: Dict[str, Any], code: str, day: str,
                n: int) -> Optional[float]:
    """近 n 日日均成交额；窗口凑不齐回 None（不拿残窗冒充）。"""
    ref = ctx["ref_dates"]
    if day not in ref:
        return None
    idx = ref.index(day)
    window = ref[max(0, idx - n + 1):idx + 1]
    if len(window) < n:
        return None
    total = 0.0
    for d in window:
        bar = _bar_of(ctx, code, d)
        if not bar or bar.get("amount") is None:
            return None
        total += float(bar["amount"])
    return total / n


def _peak_high(ctx: Dict[str, Any], code: str, entry_date: str,
               day: str) -> Optional[float]:
    """入场日（含）到信号日的最高价 —— 移动止盈的峰值基准。"""
    peak: Optional[float] = None
    for d in ctx["ref_dates"]:
        if not (entry_date <= d <= day):
            continue
        bar = _bar_of(ctx, code, d)
        if not bar or bar.get("high") is None:
            continue
        v = float(bar["high"])
        peak = v if peak is None else max(peak, v)
    return peak


def _hold_days(ctx: Dict[str, Any], entry_date: str, day: str) -> int:
    """已持仓交易日 = (entry_date, day] 内的交易日数（入场当日记 0）。"""
    return sum(1 for d in ctx["ref_dates"] if entry_date < d <= day)


def _exit_ref_cost(pos: Dict[str, Any],
                   bar: Dict[str, Any]) -> tuple:
    """退出规则用的入场成本基准（跨除权折算）。

    持仓期间发生除权时，不复权 close 与不复权 avg_cost 直接比会把除权跳空
    当成暴跌。折算 = 成本 × (当日因子 / 入场因子)（与 research 的
    price_adjust 同口径）；任一因子缺失 → 原值直比并附 UNCERTAIN 留痕，
    **绝不静默假装折算过**。
    """
    try:
        cost = float(pos.get("avg_cost") or 0.0)
        f_entry = pos.get("cost_factor")
        f_now = bar.get("adj_factor")
        if f_entry is not None and f_now is not None \
                and float(f_entry) > 0 and float(f_now) > 0:
            return cost * (float(f_now) / float(f_entry)), None
    except (TypeError, ValueError):
        pass
    return cost, "复权因子缺失：止损按不复权价直比（期间有除权可能误判）"


def _mk(code: str, name: Optional[str], side: str,
        reasons: List[Dict[str, Any]], ctx: Dict[str, Any],
        qty: Optional[int] = None, alloc_pct: Optional[float] = None,
        est_price: Optional[float] = None) -> Dict[str, Any]:
    return {
        "code": str(code),
        "name": name,
        "side": side,
        "signal_date": ctx["signal_date"],
        "exec_date": ctx.get("exec_date"),
        "reasons": reasons,
        "target_qty": int(qty) if qty else None,
        "alloc_pct": float(alloc_pct) if alloc_pct is not None else None,
        "est_price": float(est_price) if est_price is not None else None,
        "rule_version": RULE_VERSION,
        "param_version": ctx["param_version"],
        "snapshot_date": ctx.get("snapshot_date"),
        "bar_date": (ctx["signal_date"]
                     if _bar_of(ctx, str(code), ctx["signal_date"]) else None),
    }


# --------------------------------------------------------------- 卖出评估
def _eval_position(pos: Dict[str, Any], ctx: Dict[str, Any],
                   p: Dict[str, Any]) -> Dict[str, Any]:
    """在持仓上求值五条退出规则；命中即整仓 SELL，否则 HOLD。"""
    code = str(pos["code"])
    day = ctx["signal_date"]
    bar = _bar_of(ctx, code, day)
    if not bar or bar.get("close") is None:
        return _mk(code, pos.get("name"), "HOLD",
                   [{"rule": "bar_missing",
                     "detail": "%s 无行情，退出规则不可判，不臆断持仓去留" % day}],
                   ctx, est_price=None)
    close = float(bar["close"])
    hits: List[Dict[str, Any]] = []
    ref_cost, adj_note = _exit_ref_cost(pos, bar)

    # 1) 固定止损
    stop_px = ref_cost * (1.0 - p["stop_loss_pct"])
    if close <= stop_px:
        hits.append({"rule": "stop_loss",
                     "detail": "收盘 %.2f <= 止损线 %.3f（基准成本 %.3f）"
                               % (close, stop_px, ref_cost)})
    # 2) 移动止盈
    peak = _peak_high(ctx, code, str(pos["entry_date"]), day)
    if peak is not None:
        trail_px = peak * (1.0 - p["trail_pct"])
        if close <= trail_px:
            hits.append({"rule": "trailing_stop",
                         "detail": "收盘 %.2f <= 峰值 %.2f 回撤线 %.3f"
                                   % (close, peak, trail_px)})
    # 3) 趋势破坏（窗口不足不算触发：刚建仓是常态，不是卖出理由）
    ma = _ma(ctx, code, day, p["exit_trend_ma"])
    if ma is not None and close < ma:
        hits.append({"rule": "trend_break",
                     "detail": "收盘 %.2f < MA%d %.3f"
                               % (close, p["exit_trend_ma"], ma)})
    # 4) 最大持仓交易日
    held = _hold_days(ctx, str(pos["entry_date"]), day)
    if held >= p["max_hold_days"]:
        hits.append({"rule": "max_hold",
                     "detail": "已持仓 %d 个交易日 >= 上限 %d"
                               % (held, p["max_hold_days"])})
    # 5) 风险状态（默认不强平，配置打开才动）
    if p["risk_force_exit"] and ctx["risk_state"] == "RISK":
        hits.append({"rule": "risk_exit",
                     "detail": "数据风险状态且 risk_force_exit=true，强平"})

    if hits:
        if adj_note:
            hits.append({"rule": "adjust_uncertain", "detail": adj_note})
        return _mk(code, pos.get("name"), "SELL", hits, ctx,
                   qty=int(pos["qty"]), est_price=close)
    return _mk(code, pos.get("name"), "HOLD",
               [{"rule": "hold_ok",
                 "detail": "止损/移动止盈/趋势/超期均未触发（持仓 %d/%d 交易日）"
                           % (held, p["max_hold_days"])}],
               ctx, est_price=close)


# --------------------------------------------------------------- 买入评估
def _buy_gate(row: Dict[str, Any], ctx: Dict[str, Any],
              p: Dict[str, Any]) -> tuple:
    """逐票过滤。返回 (拦截 reasons, 通过的证据 reasons)。

    顺序：风险门 → ST → 行情 → 分数 → 趋势 → 流动性 → 重复（持仓/在途单）。
    fail-fast：命中即停，同一输入永远给出同一组 reasons。
    """
    code = str(row.get("code") or "")
    blockers: List[Dict[str, Any]] = []
    passed: List[Dict[str, Any]] = []
    day = ctx["signal_date"]

    if ctx["risk_state"] == "RISK":
        blockers.append({"rule": "risk_gate",
                         "detail": "数据风险状态（输入域不全），禁止新开仓"})
        return blockers, passed

    name = row.get("name")
    if name and "ST" in str(name).upper():
        blockers.append({"rule": "st", "detail": "ST 股排除（名称 %s）" % name})
        return blockers, passed

    bar = _bar_of(ctx, code, day)
    if not bar:
        blockers.append({"rule": "bar_missing",
                         "detail": "%s 无行情（停牌或数据缺失）" % day})
        return blockers, passed
    if bar.get("close") is None or float(bar.get("close") or 0) <= 0:
        blockers.append({"rule": "key_quote_missing", "detail": "收盘价缺失或非正"})
        return blockers, passed

    score = row.get("score")
    if score is None:
        blockers.append({"rule": "score_missing", "detail": "六维综合分缺失"})
        return blockers, passed
    if float(score) < p["min_score"]:
        blockers.append({"rule": "score_below",
                         "detail": "综合分 %.2f < 下限 %.2f"
                                   % (float(score), p["min_score"])})
        return blockers, passed

    ma = _ma(ctx, code, day, p["trend_ma"])
    if ma is None:
        blockers.append({"rule": "trend_window",
                         "detail": "可交易行情窗口不足 %d 日，趋势不可判"
                                   % p["trend_ma"]})
        return blockers, passed
    close = float(bar["close"])
    if close < ma:
        blockers.append({"rule": "trend_down",
                         "detail": "收盘 %.2f < MA%d %.3f"
                                   % (close, p["trend_ma"], ma)})
        return blockers, passed
    passed.append({"rule": "trend_ok",
                   "detail": "收盘 %.2f >= MA%d %.3f"
                             % (close, p["trend_ma"], ma)})

    avg_amt = _avg_amount(ctx, code, day, AMOUNT_WINDOW)
    if avg_amt is None:
        blockers.append({"rule": "amount_window",
                         "detail": "近 %d 日成交额窗口不完整，流动性不可判"
                                   % AMOUNT_WINDOW})
        return blockers, passed
    if avg_amt < p["min_amount"]:
        blockers.append({"rule": "liquidity",
                         "detail": "近%d日均成交额 %.0f < 下限 %.0f"
                                   % (AMOUNT_WINDOW, avg_amt, p["min_amount"])})
        return blockers, passed
    passed.append({"rule": "liquidity_ok",
                   "detail": "近%d日均成交额 %.0f >= 下限 %.0f"
                             % (AMOUNT_WINDOW, avg_amt, p["min_amount"])})

    if code in ctx["positions_by_code"] and not p["allow_pyramid"]:
        blockers.append({"rule": "already_holding",
                         "detail": "已持有该标的且未开加仓"})
        return blockers, passed
    if code in ctx["pending_buy_codes"]:
        blockers.append({"rule": "duplicate_order",
                         "detail": "已有有效买入订单，不重复下单"})
        return blockers, passed
    return blockers, passed


def generate(ctx: Dict[str, Any]) -> Dict[str, Any]:
    """求值整个信号日：先持仓退出（整仓 SELL/HOLD），再候选买入。"""
    p = ctx["params"]
    day = ctx["signal_date"]
    signals: List[Dict[str, Any]] = []

    # ---- 1) 持仓退出 ----
    expected_proceeds = 0.0
    for pos in ctx["positions"]:
        sig = _eval_position(pos, ctx, p)
        signals.append(sig)
        if sig["side"] == "SELL" and sig["est_price"]:
            qty = int(pos["qty"])
            amount = qty * float(sig["est_price"]) * (1.0 - p["slippage"])
            fee = max(amount * p["commission_rate"], p["min_commission"]) \
                + amount * p["stamp_rate"]
            expected_proceeds += max(0.0, amount - fee)

    # ---- 2) 账户与组合状态 ----
    cash = float(ctx["cash"])
    pos_value = 0.0
    for pos in ctx["positions"]:
        bar = _bar_of(ctx, str(pos["code"]), day)
        if bar and bar.get("close") is not None:
            pos_value += int(pos["qty"]) * float(bar["close"])
        else:
            # 没当日价：用最近可得收盘（不编价格）；连历史都缺才退回成本，
            # 并在组合层留痕 —— 这是估值降级，不是市场价格。
            hist = ctx["bars"].get(str(pos["code"]), {})
            last = max((d for d in hist if d <= day), default=None)
            if last and hist[last].get("close") is not None:
                pos_value += int(pos["qty"]) * float(hist[last]["close"])
            else:
                pos_value += int(pos["qty"]) * float(pos.get("avg_cost") or 0.0)
    equity = cash + pos_value
    exposure_value = pos_value
    available = cash + expected_proceeds
    committed_value = 0.0
    committed_new = 0

    # ---- 3) 候选买入 ----
    rows = [r for r in ctx["rows"]
            if r.get("rank") is not None
            and int(r["rank"]) <= p["candidate_top_n"]]
    rows.sort(key=lambda r: int(r["rank"]))

    for row in rows:
        code = str(row.get("code") or "")
        blockers, passed = _buy_gate(row, ctx, p)
        if blockers:
            signals.append(_mk(code, row.get("name"), "HOLD", blockers, ctx,
                               est_price=row.get("price")))
            continue
        if committed_new >= p["max_positions"] - len(ctx["positions"]):
            signals.append(_mk(code, row.get("name"), "HOLD",
                               [{"rule": "position_cap",
                                 "detail": "持仓数已达上限 %d"
                                           % p["max_positions"]}],
                               ctx, est_price=row.get("price")))
            continue
        room = p["max_total_exposure"] - (
            exposure_value + committed_value) / equity if equity > 0 else 0.0
        if room <= 0:
            signals.append(_mk(code, row.get("name"), "HOLD",
                               [{"rule": "exposure_cap",
                                 "detail": "总仓位已达上限 %.0f%%"
                                           % (p["max_total_exposure"] * 100)}],
                               ctx, est_price=row.get("price")))
            continue

        close = float(_bar_of(ctx, code, day)["close"])
        budget = close * (1.0 + p["slippage"] + p["fee_reserve"])
        cap_alloc = min(equity * p["max_position_pct"], room * equity)
        shares_cap = int(cap_alloc / budget / 100) * 100 if budget > 0 else 0
        shares_cash = int(available / budget / 100) * 100 if budget > 0 else 0
        shares = max(0, min(shares_cap, shares_cash))
        if shares < 100:
            if shares_cap < 100 and shares_cap <= shares_cash:
                reason = {"rule": "position_pct_cap",
                          "detail": "单股仓位上限 %.0f%% 不足一手（预算每股 %.3f，"
                                    "权益 %.0f）"
                                    % (p["max_position_pct"] * 100, budget,
                                       equity)}
            else:
                reason = {"rule": "cash_insufficient",
                          "detail": "可用现金 %.0f 不足一手（预算每股 %.3f）"
                                    % (available, budget)}
            signals.append(_mk(code, row.get("name"), "HOLD", [reason], ctx,
                               est_price=close))
            continue

        alloc_pct = shares * budget / equity if equity > 0 else None
        passed.append({"rule": "size_ok",
                       "detail": "买入 %d 股 ≈ %.0f 元（占权益 %.1f%%，预算每股 %.3f）"
                                 % (shares, shares * budget,
                                    (alloc_pct or 0) * 100, budget)})
        signals.append(_mk(code, row.get("name"), "BUY", passed, ctx,
                           qty=shares, alloc_pct=alloc_pct, est_price=close))
        amount = shares * close * (1.0 + p["slippage"])
        fee = max(amount * p["commission_rate"], p["min_commission"])
        available -= amount + fee
        committed_value += shares * close
        committed_new += 1

    # available 此时已被本 run 的买单扣过 —— 交给风控的必须是**下单前**的
    # 可用量，扣减由风控循环按逐单实时算，否则双重扣减会把正常单误拒。
    portfolio = {
        "cash": cash,
        "position_value": pos_value,
        "equity": equity,
        "exposure": (exposure_value / equity) if equity > 0 else None,
        "available": cash + expected_proceeds,
    }
    return {"status": "OK", "refusal_reason": None, "signals": signals,
            "portfolio": portfolio}
