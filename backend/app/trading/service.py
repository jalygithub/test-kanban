# -*- coding: utf-8 -*-
"""策略服务编排 —— 组上下文 → 策略求值 → 落库 → 风控 → 下单 → 执行。

零联网是硬约束：候选走本地 `main_force` 快照，行情走 `daily_bar`，
交易日历**只准** `cached_trade_dates()`（`trade_dates()`/`last_trade_date()`
缓存过期会朝新浪发包），风险状态走 `ingest.rank_quality()`（只读 ingest_job）。
本模块任何路径都不碰 fetchers 里会发包的函数。

时点纪律：信号用 **≤ signal_date** 的数据（`bars_load_window(until=date)`
在 SQL 层卡死上界），订单在**下一交易日开盘价**执行（`paper.execute`）。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from app.fetchers.calendar import cached_trade_dates
from app.pipeline import ingest
from app.store import db
from app.trading import params as pmod
from app.trading import paper, risk, strategy

logger = logging.getLogger(__name__)

STRATEGY = "rule_v1"
BAR_TABLE = "daily_bar"
#: 上下文行情窗口（自然日）。MA/持仓窗口最长 250 交易日 ≈ 370 自然日，
#: 留到 600 保证配置拉满时窗口也够；只读本地库，成本是几万行查询。
WINDOW_DAYS = 600


# --------------------------------------------------------------- 主流程
def run_signals(date: Optional[str] = None,
                params_override: Optional[Dict[str, Any]] = None
                ) -> Dict[str, Any]:
    """生成信号日的交易意图并下单（幂等）。

    幂等语义：同 (strategy, date) 且 status=OK → 直接回既有结果，只补
    「上次没走到的下单」（signal_id UNIQUE 拦重复单）。REFUSED 的 run 允许
    重试 —— 数据补齐后同一键就地升级成 OK，不新开一行。
    """
    try:
        p = pmod.normalize(params_override)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    version = pmod.params_version(params_override)
    p_json = pmod.params_json(params_override)
    db.trading_param_ensure(version, p_json)

    if not date:
        date = db.latest_date("main_force")
        if not date:
            return {"status": "REFUSED", "run_id": None, "idempotent": False,
                    "signal_date": None, "exec_date": None,
                    "refusal_reason": "库里没有任何六维快照，无候选可用"}

    existing = db.trading_run_get(STRATEGY, date)
    if existing and str(existing["status"]) == "OK":
        placed, rejected, total = _place_for_run(existing,
                                                 existing.get("exec_date"))
        return {"status": "OK", "run_id": int(existing["id"]),
                "idempotent": True, "signal_date": date,
                "exec_date": existing.get("exec_date"),
                "risk_state": existing.get("risk_state"),
                "quality_state": existing.get("quality_state"),
                "signals": _side_counts(total),
                "orders": {"placed": placed, "risk_rejected": rejected},
                "refusal_reason": None}

    # ---- 数据就绪检查（任一不过 → 明确 REFUSED，不硬凑信号）----
    snapshot = db.load_one("main_force", date)
    if snapshot is None:
        return _refuse(existing, date, None, p, version, p_json,
                       "信号日 %s 无六维候选快照，拒绝生成" % date)
    snap_date = str(snapshot.get("date") or "")
    if snap_date != date:
        return _refuse(existing, date, None, p, version, p_json,
                       "六维快照日期 %s 与信号日 %s 不符（数据过期），拒绝生成"
                       % (snap_date, date))

    exec_date = _next_trade_day(date)
    if exec_date is None:
        return _refuse(existing, date, None, p, version, p_json,
                       "无法确定下一交易日（库内无更晚行情且日历缓存不可用），"
                       "拒不下单")

    quality = ingest.rank_quality(date)
    risk_state = "NORMAL" if str(quality.get("state")) == "ok" else "RISK"
    rows = [r for r in (snapshot.get("rows") or []) if r.get("code")]
    positions = db.sim_positions_list()
    pos_map = {x["code"]: x for x in positions}
    pending = db.trading_orders_pending()
    pending_buy = {o["code"] for o in pending if o["side"] == "BUY"}

    acct = db.sim_account_get()
    if acct is None:
        acct = db.sim_account_init(p["initial_cash"])
    cash = float(acct.get("cash") or 0.0)

    since = (datetime.strptime(date, "%Y%m%d")
             - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
    codes = [str(r["code"]) for r in rows if int(r.get("rank") or 10 ** 9)
             <= p["candidate_top_n"]]
    codes += [str(c) for c in pos_map]
    bars = db.bars_load_window(BAR_TABLE, codes, since, date)
    ref_dates = db.bars_window_dates(BAR_TABLE, since, date)
    if not ref_dates:
        return _refuse(existing, date, exec_date, p, version, p_json,
                       "窗口 %s..%s 内没有任何日线，行情不可用" % (since, date),
                       risk_state=risk_state, quality=quality)

    ctx = {
        "signal_date": date,
        "exec_date": exec_date,
        "snapshot_date": snap_date,
        "rows": rows,
        "bars": bars,
        "ref_dates": ref_dates,
        "positions": positions,
        "positions_by_code": pos_map,
        "cash": cash,
        "pending_buy_codes": pending_buy,
        "params": p,
        "param_version": version,
        "risk_state": risk_state,
    }
    result = strategy.generate(ctx)
    portfolio = result.get("portfolio") or {}

    run_id, created = db.trading_run_create(
        STRATEGY, date, exec_date, strategy.RULE_VERSION, version, p_json,
        "OK", refusal_reason=None, risk_state=risk_state,
        quality_state=str(quality.get("state")),
        quality_json=json.dumps(quality, ensure_ascii=False),
        candidates_n=len(rows), signals_n=len(result["signals"]))
    if not created:
        run0 = db.trading_run_get(STRATEGY, date)
        if str(run0.get("status") if run0 else "") != "REFUSED":
            # 竞态：另一路径刚建了同日 OK run —— 放弃写入，回幂等口径。
            return {"status": "OK", "run_id": run_id, "idempotent": True,
                    "signal_date": date, "exec_date": exec_date,
                    "risk_state": risk_state,
                    "quality_state": quality.get("state"),
                    "signals": _side_counts(db.trading_signals_by_run(run_id)),
                    "orders": {"placed": 0, "risk_rejected": 0},
                    "refusal_reason": None}
        # REFUSED → OK 升级：就地改写状态，信号照常补进同一 run。
        db.trading_run_update(run_id, {
            "exec_date": exec_date, "rule_version": strategy.RULE_VERSION,
            "param_version": version, "params_json": p_json, "status": "OK",
            "refusal_reason": None, "risk_state": risk_state,
            "quality_state": str(quality.get("state")),
            "quality_json": json.dumps(quality, ensure_ascii=False),
            "candidates_n": len(rows), "signals_n": len(result["signals"]),
        })

    db.trading_signal_add([{**sig, "run_id": run_id}
                           for sig in result["signals"]])
    run = db.trading_run_get(STRATEGY, date)
    placed, rejected, total = _place_for_run(run, exec_date,
                                             portfolio=portfolio,
                                             rows=rows, bars=bars)
    return {"status": "OK", "run_id": run_id, "idempotent": False,
            "signal_date": date, "exec_date": exec_date,
            "risk_state": risk_state, "quality_state": quality.get("state"),
            "signals": _side_counts(total),
            "orders": {"placed": placed, "risk_rejected": rejected},
            "portfolio": portfolio, "refusal_reason": None}


def _refuse(existing: Optional[Dict[str, Any]], date: str,
            exec_date: Optional[str], p: Dict[str, Any], version: str,
            p_json: str, reason: str, risk_state: Optional[str] = None,
            quality: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """落一条 REFUSED run（明确的不交易）并回给调用方。

    同键已存在时就地更新（REFUSED 可重试升级 OK，也可被新理由覆盖），
    绝不因为拒绝而绕开 UNIQUE 幂等键。
    """
    fields = {
        "exec_date": exec_date, "rule_version": strategy.RULE_VERSION,
        "param_version": version, "params_json": p_json,
        "status": "REFUSED", "refusal_reason": reason,
        "risk_state": risk_state,
        "quality_state": (quality or {}).get("state"),
        "quality_json": json.dumps(quality, ensure_ascii=False)
        if quality else None,
        "candidates_n": 0, "signals_n": 0,
    }
    if existing:
        db.trading_run_update(int(existing["id"]), fields)
        run_id, created = int(existing["id"]), False
    else:
        run_id, created = db.trading_run_create(
            STRATEGY, date, exec_date, fields["rule_version"], version,
            p_json, "REFUSED", refusal_reason=reason,
            risk_state=risk_state,
            quality_state=fields["quality_state"],
            quality_json=fields["quality_json"])
    return {"status": "REFUSED", "run_id": run_id, "idempotent": not created,
            "signal_date": date, "exec_date": exec_date,
            "risk_state": risk_state,
            "quality_state": fields["quality_state"],
            "signals": {}, "orders": {"placed": 0, "risk_rejected": 0},
            "refusal_reason": reason}


def _place_for_run(run: Dict[str, Any], exec_date: Optional[str],
                   portfolio: Optional[Dict[str, Any]] = None,
                   rows: Optional[List[Dict[str, Any]]] = None,
                   bars: Optional[Dict[str, Any]] = None
                   ) -> Tuple[int, int, List[Dict[str, Any]]]:
    """风控 → 下单。可重入（order.signal_id UNIQUE），幂等路径也走这里补单。

    **一律用 run 自己的参数快照**（`params_json`）审核 —— 事后改默认参数
    不该把历史 run 的审核口径也改掉，复现要求「当时那套参数」从头用到尾。
    """
    run_id = int(run["id"])
    date = str(run["signal_date"])
    try:
        p_run = pmod.normalize(json.loads(run.get("params_json") or "{}"))
    except (TypeError, ValueError):
        p_run = dict(pmod.DEFAULT_PARAMS)

    signals = db.trading_signals_by_run(run_id)
    positions = db.sim_positions_list()
    pos_map = {x["code"]: x for x in positions}
    pending = db.trading_orders_pending()
    pending_buy = {o["code"] for o in pending if o["side"] == "BUY"}

    if portfolio is None:
        # 幂等补单路径：按 run 的快照重建求值上下文。
        since = (datetime.strptime(date, "%Y%m%d")
                 - timedelta(days=WINDOW_DAYS)).strftime("%Y%m%d")
        snap = db.load_one("main_force", run.get("snapshot_date") or date) or {}
        rows = [r for r in (snap.get("rows") or []) if r.get("code")]
        codes = [str(r["code"]) for r in rows] + list(pos_map)
        bars = db.bars_load_window(BAR_TABLE, codes, since, date)
        ref_dates = db.bars_window_dates(BAR_TABLE, since, date)
        acct0 = db.sim_account_get() or {"cash": 0.0}
        ctx = {
            "signal_date": date, "exec_date": exec_date,
            "snapshot_date": run.get("snapshot_date") or date, "rows": rows,
            "bars": bars, "ref_dates": ref_dates, "positions": positions,
            "positions_by_code": pos_map,
            "cash": float(acct0.get("cash") or 0.0),
            "pending_buy_codes": pending_buy, "params": p_run,
            "param_version": str(run.get("param_version") or ""),
            "risk_state": str(run.get("risk_state") or "NORMAL"),
        }
        portfolio = strategy.generate(ctx).get("portfolio") or {}

    acct = db.sim_account_get() or {"cash": 0.0}
    equity = float(portfolio.get("equity") or float(acct.get("cash") or 0.0))
    exposure = portfolio.get("exposure")
    account = {
        "cash": float(acct.get("cash") or 0.0),
        "available": float(portfolio.get("available")
                           or float(acct.get("cash") or 0.0)),
        "equity": equity, "exposure": float(exposure or 0.0),
        "position_count": len(positions), "positions": pos_map,
        "pending_buy": set(pending_buy),
        "risk_state": str(run.get("risk_state") or "NORMAL"),
        "exec_date": exec_date,
    }

    placed = rejected = 0
    for sig in signals:
        if sig.get("side") not in ("BUY", "SELL"):
            continue
        res = risk.review(sig, account, p_run)
        order_id = paper.place(sig, res, exec_date)
        if order_id is None:
            continue  # 已有单 —— 幂等
        if res.get("approved"):
            placed += 1
            _advance_account(account, sig, p_run, equity)
        else:
            rejected += 1
    return placed, rejected, signals


def _advance_account(account: Dict[str, Any], sig: Dict[str, Any],
                     p: Dict[str, Any], equity: float) -> None:
    """风控通过后同步推进审核状态：后续信号看到的是**已含前单**的账户。

    不推进的话，两单各自都过总仓位/现金检查、合起来却超限 —— 独立审核
    就白独立了。估值按信号日参考价（执行在 D+1，误差在执行层再兜一次）。
    """
    qty = int(sig.get("target_qty") or 0)
    est = float(sig.get("est_price") or 0.0)
    value = qty * est
    if equity > 0:
        delta = value / equity
    else:
        delta = 0.0
    if sig["side"] == "BUY":
        amount = value * (1.0 + p["slippage"])
        fee = max(amount * p["commission_rate"], p["min_commission"])
        account["available"] = float(account["available"]) - amount - fee
        account["position_count"] = int(account["position_count"]) + 1
        account["exposure"] = float(account["exposure"]) + delta
        account["pending_buy"].add(str(sig["code"]))
    else:
        amount = value * (1.0 - p["slippage"])
        fee = max(amount * p["commission_rate"], p["min_commission"]) \
            + amount * p["stamp_rate"]
        account["available"] = float(account["available"]) + amount - fee
        account["position_count"] = max(
            0, int(account["position_count"]) - 1)
        account["exposure"] = max(0.0, float(account["exposure"]) - delta)
        pos = account["positions"].get(str(sig["code"]))
        if pos is not None:
            pos["qty"] = int(pos.get("qty") or 0) - qty  # 本 run 已批准的卖出


def _next_trade_day(day: str) -> Optional[str]:
    """下一交易日：库内有更晚行情优先（有行情 = 确凿的交易日，同
    engine._ref_dates 口径）；实盘没有明天的行情，退回**日历缓存**
    （只用 `cached_trade_dates()`，缓存过期宁可拒绝也不联网）。"""
    nxt = db.bars_next_date(BAR_TABLE, day)
    if nxt:
        return nxt
    try:
        dates = cached_trade_dates()
    except Exception as exc:  # noqa: BLE001
        logger.warning("日历缓存不可用: %s", exc)
        dates = []
    later = [d for d in dates if str(d) > day]
    return str(later[0]) if later else None


def _side_counts(signals: List[Dict[str, Any]]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for s in signals:
        side = str(s.get("side") or "?")
        out[side] = out.get(side, 0) + 1
    return out


# --------------------------------------------------------------- 执行与查询
def execute(exec_date: Optional[str] = None) -> Dict[str, Any]:
    """执行待处理订单（幂等）。缺省从最早的待执行日开始。"""
    if exec_date is not None:
        return paper.execute(exec_date)

    # 自动模式：目标日行情永久缺失（后续日期已有行情）→ 那些单过期，
    # 继续找下一个可执行日 —— 队列不许被一个永不到达的日子卡死。
    total: Dict[str, Any] = {"ran": False, "exec_date": None,
                             "reason": "没有待执行订单", "filled": 0,
                             "rejected": 0, "expired": 0, "details": []}
    guard = 0
    while guard < 30:
        guard += 1
        pending = [o for o in db.trading_orders_pending() if o.get("exec_date")]
        if not pending:
            if total["details"] or total["expired"]:
                total["ran"] = bool(total["details"])
                total["reason"] = total.get("reason") \
                    if not total["details"] else None
            return total
        target = min(o["exec_date"] for o in pending)
        res = paper.execute(target)
        if res.get("ran"):
            total.update({
                "ran": True, "exec_date": res["exec_date"], "reason": None,
                "filled": total["filled"] + res["filled"],
                "rejected": total["rejected"] + res["rejected"],
                "expired": total["expired"] + res["expired"],
                "details": total["details"] + res["details"],
            })
            if res["filled"] or res["rejected"]:
                return total
            # 目标日无可执行单（理论上到不了）→ 继续
            total["ran"] = False
            continue
        # ran=False：执行日行情未入库
        dates = set(db.bars_dates(BAR_TABLE))
        has_later = any(d > target for d in dates)
        if not has_later:
            total["reason"] = res.get("reason")
            total["exec_date"] = target
            return total
        for o in pending:
            if o["exec_date"] == target:
                db.sim_order_set(int(o["id"]), "EXPIRED",
                                 reject_reason="执行日 %s 行情缺失但后续日期已有"
                                               " 行情，判定该日数据不会到达：不追补、"
                                               "不伪造成交" % target)
                total["expired"] += 1
        continue
    return total


def status() -> Dict[str, Any]:
    """账户 / 持仓 / 待执行单 / 最近一次 run —— 全部本地库读取。"""
    acct = db.sim_account_get()
    positions = db.sim_positions_list()
    pending_n = len([o for o in db.trading_orders_pending()
                     if o.get("exec_date") is not None])
    runs = db.trading_run_list(1)
    last = runs[0] if runs else None
    if last:
        last = dict(last)
        last.pop("params_json", None)
        last.pop("quality_json", None)
    return {"account": acct, "positions": positions,
            "pending_orders": pending_n, "last_run": last,
            "initialized": acct is not None}


def runs(limit: int = 20) -> List[Dict[str, Any]]:
    out = []
    for r in db.trading_run_list(limit):
        r = dict(r)
        r.pop("params_json", None)
        r.pop("quality_json", None)
        out.append(r)
    return out


def signals(run_id: int) -> List[Dict[str, Any]]:
    return db.trading_signals_by_run(run_id)


def orders(limit: int = 50) -> List[Dict[str, Any]]:
    return db.trading_orders_list(limit)


def params_info() -> Dict[str, Any]:
    """默认参数 + 版本口径 —— 给前端表单与人读的同一份事实。"""
    return {"strategy": STRATEGY, "rule_version": strategy.RULE_VERSION,
            "defaults": dict(pmod.DEFAULT_PARAMS),
            "version_of_defaults": pmod.params_version({}),
            "notes": {
                "version": "参数版本 = 归一化 JSON 的 sha256 前 12 位；"
                           "每次 run 落实际参数快照",
                "costs": "佣金/印花税/滑点为研究假设费率，非券商真值",
                "sizing": "买入按预算每股价 = 收盘×(1+滑点+费用预留)，"
                          "100 股整手向下取整",
            }}
