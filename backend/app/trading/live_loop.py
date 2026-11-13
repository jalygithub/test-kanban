# -*- coding: utf-8 -*-
"""实时纸面自动交易循环 —— 取实时价 → 算指标 → 套规则 → 纸面撮合 → 更新面板。

## 与 daily 策略（service.py）的区别

- **数据**：daily 走本地日线、零联网；本模块**联网取实时价**（`fetchers.live`，
  腾讯批量、约 5 秒延迟），因为「实时选股/实时盯盘」离了现价无从谈起。
- **成交**：daily 是 D+1 开盘撮合；本模块**按现价即时纸面成交**。
- **账户**：daily 用 `sim_account`；本模块用 `live_account`（各自独立，语义不同）。
- **规则**：daily 的规则硬编码在 strategy.py；本模块的规则是**数据**
  （`rules.py` + `trading_rule` 表），可选/可改/可加。

两条路互不污染：回测/复现仍然只认 daily 那套离线口径。

## 一轮 tick 做什么

1. 宇宙 = 六维榜前 N 候选 + 当前持仓（持仓必须进来，否则退出规则无的放矢）。
2. `live.quotes()` 批量取实时价（网络，带熔断/校验，拿不到就置 None 留痕）。
3. 算指标：现价/涨跌幅/额来自实时报价；MA/均额/N 日高来自日线历史**并入当日实时价**。
4. 先对持仓跑 **exit 规则** → 卖出信号；再对候选跑 **filter 漏斗** → 买入信号。
5. 纸面撮合（先卖后买，按现价、整手、费后），`db.live_fill` 原子记账。
6. 更新面板状态（漏斗/买卖/持仓盈亏/账户/重点观察），前端轮询 `/trading/live/state`。

## 诚实边界

- 实时报价字段过 `live.py` 的不变量校验，拿不到就 None，**绝不显示猜出来的数**。
- 不是真实下单，全部纸面；不构成投资建议。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.fetchers import live as live_fetch
from app.pipeline import ingest  # 风险状态（沿用 daily 口径）
from app.store import db
from app.trading import params as pmod
from app.trading import rules as rmod

logger = logging.getLogger(__name__)

BAR_TABLE = "daily_bar"
#: 六维榜候选宇宙上限（每轮取前 N 名进漏斗）。
UNIVERSE_TOP_N = 30
#: 日线历史窗口（自然日），算 MA/均额/高点够用。
HIST_DAYS = 400
#: 历史缓存 TTL（秒）—— 每轮都重查 30 只 × 几百日太浪费，几分钟刷一次足够。
HIST_TTL = 300.0
DEFAULT_INTERVAL = 5.0

# 交易成本（研究假设费率，与 params.DEFAULT_PARAMS 同源）。
_COST = {
    "slippage": 0.001, "commission_rate": 0.00025,
    "min_commission": 5.0, "stamp_rate": 0.0005,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _in_session() -> bool:
    """A 股盘中（本地时间）：09:30-11:30 / 13:00-15:00。定时触发模式只在盘中下单。
    只看时钟不看日历（与前端 live.ts 同口径）—— 周末/节假日报价会停在最后一笔。"""
    now = datetime.now()
    cur = now.hour * 60 + now.minute
    return (570 <= cur < 690) or (780 <= cur < 900)


# --------------------------------------------------------------- 历史缓存
class _HistCache:
    """每只票的日线历史（升序 [{date, close, amount, high}]），带 TTL。"""

    def __init__(self) -> None:
        self._data: Dict[str, List[Dict[str, Any]]] = {}
        self._ts: Dict[str, float] = {}

    def get(self, code: str) -> List[Dict[str, Any]]:
        now = time.monotonic()
        if code not in self._data or now - self._ts.get(code, 0.0) > HIST_TTL:
            since = _date_before(HIST_DAYS)
            bars = db.bars_load_window(BAR_TABLE, [code], since,
                                       _today()).get(code) or {}
            rows = [bars[d] for d in sorted(bars)]
            self._data[code] = rows
            self._ts[code] = now
        return self._data[code]


def _date_before(days: int) -> str:
    from datetime import timedelta
    return (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


_HIST = _HistCache()


# --------------------------------------------------------------- 指标计算
def _build_ind(code: str, name: str, q: Dict[str, Any],
               score: Optional[float], risk_state: str) -> Dict[str, Any]:
    """把实时报价 + 日线历史 + 六维分，拼成规则引擎要的 `ind` 指标快照。"""
    price = q.get("price")
    ind: Dict[str, Any] = {
        "code": code, "name": name,
        "price": price,
        "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
        "change_pct": q.get("pct"),
        "volume": q.get("volume"), "amount": q.get("amount"),
        "score": score, "risk_state": risk_state,
        "ma": {}, "avg_amount": {}, "high_n": {},
    }
    hist = _HIST.get(code)  # 升序日线（不含今日）
    # 需要的窗口：MA 用到 250，均额 60，高点 250 → 取历史末端拼当日实时价。
    for n in (5, 10, 20, 30, 60, 120, 250):
        # MA(n) = (近 n-1 日收盘 + 今日现价) / n
        if price is not None and len(hist) >= n - 1:
            closes = [float(b["close"]) for b in hist[-(n - 1):]
                      if b.get("close") is not None]
            if len(closes) == n - 1:
                ind["ma"][str(n)] = (sum(closes) + float(price)) / n
        # 均额(n) = (近 n-1 日额 + 今日额) / n
        amt = q.get("amount")
        if amt is not None and len(hist) >= n - 1:
            amts = [float(b["amount"]) for b in hist[-(n - 1):]
                    if b.get("amount") is not None]
            if len(amts) == n - 1:
                ind["avg_amount"][str(n)] = (sum(amts) + float(amt)) / n
        # 高点(n) = 近 n 日最高（**不含今日**，突破基准）
        if len(hist) >= n:
            highs = [float(b["high"]) for b in hist[-n:]
                     if b.get("high") is not None]
            if highs:
                ind["high_n"][str(n)] = max(highs)
    return ind


# --------------------------------------------------------------- 状态（面板）
class _State:
    def __init__(self) -> None:
        self.running = False
        self.scheduled = False   # True=只在盘中下单（定时触发）
        self.interval = DEFAULT_INTERVAL
        self.last_tick: Optional[str] = None
        self.last_error: Optional[str] = None
        self.tick_count = 0
        self.funnel: List[Dict[str, Any]] = []
        self.buys: List[Dict[str, Any]] = []
        self.sells: List[Dict[str, Any]] = []
        self.watch: List[Dict[str, Any]] = []
        self.warns: List[str] = []

    def snapshot(self) -> Dict[str, Any]:
        return {
            "running": self.running, "interval": self.interval,
            "scheduled": self.scheduled, "in_session": _in_session(),
            "last_tick": self.last_tick, "last_error": self.last_error,
            "tick_count": self.tick_count, "funnel": self.funnel,
            "buys": self.buys, "sells": self.sells, "watch": self.watch,
            "warns": self.warns[-10:],
        }


_STATE = _State()
_LOOP_THREAD: Optional[threading.Thread] = None
_STOP = threading.Event()
_LOCK = threading.Lock()


# --------------------------------------------------------------- 费用与撮合
def _fee(amount: float, side: str) -> float:
    fee = max(amount * _COST["commission_rate"], _COST["min_commission"])
    if side == "SELL":
        fee += amount * _COST["stamp_rate"]
    return fee


def _init_account() -> Dict[str, Any]:
    acct = db.live_account_get()
    if acct is None:
        acct = db.live_account_init(float(pmod.DEFAULT_PARAMS["initial_cash"]))
    return acct


def _do_sell(pos: Dict[str, Any], price: float, rule_name: str,
             detail: str) -> Optional[Dict[str, Any]]:
    qty = int(pos.get("qty") or 0)
    if qty <= 0 or price is None:
        return None
    amount = qty * float(price) * (1.0 - _COST["slippage"])
    fee = _fee(amount, "SELL")
    proceeds = amount - fee
    cost = float(pos.get("avg_cost") or 0.0)
    pnl = proceeds - qty * cost
    db.live_fill(_now_iso(), str(pos["code"]), pos.get("name"), "SELL", qty,
                 float(price), fee, pnl, rule_name, detail, proceeds, None)
    return {"ts": _now_iso(), "code": str(pos["code"]), "name": pos.get("name"),
            "side": "SELL", "qty": qty, "price": float(price),
            "amount": qty * float(price), "fee": fee, "pnl": pnl,
            "rule": rule_name, "detail": detail}


def _do_buy(cand: Dict[str, Any], price: float, equity: float, cash: float,
            rule_name: str) -> Optional[Dict[str, Any]]:
    if price is None or price <= 0:
        return None
    cap = equity * float(pmod.DEFAULT_PARAMS["max_position_pct"])
    budget = min(cap, cash)
    shares = int(budget / (price * (1.0 + _COST["slippage"])) / 100) * 100
    if shares < 100:
        return None
    amount = shares * float(price) * (1.0 + _COST["slippage"])
    fee = _fee(amount, "BUY")
    code = str(cand["code"])
    old = db.live_position_get(code)
    avg_cost = float(price)
    entry_ts = (old or {}).get("entry_ts") or _now_iso()
    db.live_fill(_now_iso(), code, cand.get("name"), "BUY", shares, float(price),
                 fee, None, rule_name, None, -(amount + fee),
                 {"code": code, "name": cand.get("name"), "qty": shares,
                  "avg_cost": avg_cost, "peak_price": float(price),
                  "entry_ts": entry_ts})
    return {"ts": _now_iso(), "code": code, "name": cand.get("name"),
            "side": "BUY", "qty": shares, "price": float(price),
            "amount": shares * float(price), "fee": fee, "pnl": None,
            "rule": rule_name, "detail": "买入 %d 股" % shares}


# --------------------------------------------------------------- 主流程
def tick() -> Dict[str, Any]:
    """跑一轮：取价 → 算指标 → 套规则 → 撮合 → 更新状态。返回状态快照。"""
    with _LOCK:
        rules = _load_rules()
        filter_rules = rmod.enabled_rules(rules, "filter")
        exit_rules = rmod.enabled_rules(rules, "exit")
        _init_account()

        # ---- 1) 宇宙 = 六维榜前 N + 当前持仓 ----
        snap_date = db.latest_date("main_force")
        snap = db.load_one("main_force", snap_date) if snap_date else None
        rows = [r for r in ((snap or {}).get("rows") or []) if r.get("code")]
        rows.sort(key=lambda r: int(r.get("rank") or 10 ** 9))
        cand_rows = rows[:UNIVERSE_TOP_N]
        score_map = {str(r["code"]): r.get("score") for r in rows}
        positions = db.live_positions_list()
        pos_map = {str(p["code"]): p for p in positions}

        universe: List[Dict[str, Any]] = []
        seen = set()
        for r in cand_rows:
            c = str(r["code"])
            if c not in seen:
                seen.add(c)
                universe.append({"code": c, "name": r.get("name")})
        for c in pos_map:
            if c not in seen:
                seen.add(c)
                universe.append({"code": c, "name": pos_map[c].get("name")})

        # ---- 2) 取实时价（联网，带熔断/校验）----
        quotes, warns = live_fetch.quotes(universe)
        qmap = {str(q.get("code")): q for q in quotes}

        # ---- 3) 风险状态（沿用 daily 口径）----
        quality = ingest.rank_quality(snap_date) if snap_date else {}
        risk_state = "NORMAL" if str(quality.get("state")) == "ok" else "RISK"

        # ---- 4) 指标 ----
        ind_map: Dict[str, Dict[str, Any]] = {}
        for u in universe:
            c = u["code"]
            q = qmap.get(c) or {}
            ind_map[c] = _build_ind(c, u.get("name") or q.get("name"),
                                    q, score_map.get(c), risk_state)

        # ---- 5) 先卖（退出规则）—— 定时模式下非盘中不交易 ----
        can_trade = (not _STATE.scheduled) or _in_session()
        sells: List[Dict[str, Any]] = []
        for pos in positions:
            if not can_trade:
                break
            c = str(pos["code"])
            ind = ind_map.get(c) or {}
            res = rmod.eval_exit(ind, pos, rules)
            price = ind.get("price")
            if res["side"] == "SELL" and price is not None:
                rule_name = res["triggers"][0]["rule"] if res["triggers"] else "退出"
                detail = res["triggers"][0]["detail"] if res["triggers"] else ""
                tr = _do_sell(pos, float(price), rule_name, detail)
                if tr:
                    sells.append(tr)

        # ---- 6) 再买（filter 漏斗）----
        acct = _init_account()
        # 候选（排除已持仓 + 已在本轮卖出的）
        held_now = {str(p["code"]) for p in db.live_positions_list()}
        buy_cands = [ind_map[u["code"]] for u in universe
                     if u["code"] not in held_now and u["code"] in ind_map]
        funnel = rmod.eval_filter_funnel(buy_cands, rules)
        _STATE.funnel = funnel["stages"]

        buys: List[Dict[str, Any]] = []
        cash = float(acct.get("cash") or 0.0)
        equity = cash + sum(
            int(p["qty"]) * float((ind_map.get(str(p["code"])) or {})
                                  .get("price") or p.get("avg_cost") or 0.0)
            for p in db.live_positions_list())
        for ind in funnel["passed"]:
            if not can_trade or cash <= 0:
                break
            price = ind.get("price")
            tr = _do_buy(ind, float(price) if price else None, equity, cash,
                         "规则漏斗通过")
            if tr:
                buys.append(tr)
                cash -= tr["amount"] + tr["fee"]

        # ---- 7) 重点观察（接近止损 / 接近突破 / 高分未买）----
        watch = _build_watch(ind_map, rules)

        # ---- 8) 更新状态 ----
        _STATE.buys = buys
        _STATE.sells = sells
        _STATE.watch = watch
        _STATE.warns = warns
        _STATE.last_tick = _now_iso()
        _STATE.tick_count += 1
        _STATE.last_error = None
        return _STATE.snapshot()


def _build_watch(ind_map: Dict[str, Dict[str, Any]],
                 rules: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """重点观察：接近止损（距止损线 <2%）、接近突破（距 N 日高 <2%）、高分。"""
    watch: List[Dict[str, Any]] = []
    positions = db.live_positions_list()
    pos_map = {str(p["code"]): p for p in positions}
    for c, ind in ind_map.items():
        price = ind.get("price")
        if price is None:
            continue
        notes: List[str] = []
        pos = pos_map.get(c)
        if pos:
            cost = float(pos.get("avg_cost") or 0.0)
            if cost > 0:
                stop_line = cost * 0.92  # 与默认止损 8% 一致的观察线
                if float(price) <= stop_line * 1.02:
                    notes.append("距止损线 %.2f 仅 %.1f%%" % (stop_line, 100 * abs(float(price) - stop_line) / stop_line))
        hi = (ind.get("high_n") or {}).get("20")
        if hi and float(price) >= float(hi) * 0.98:
            notes.append("接近突破 20 日高 %.2f" % float(hi))
        score = ind.get("score")
        if score is not None and float(score) >= 80 and not pos:
            notes.append("高分 %.1f（六维榜）" % float(score))
        if notes:
            watch.append({"code": c, "name": ind.get("name"),
                          "price": float(price), "notes": notes})
    return watch


def _load_rules() -> List[Dict[str, Any]]:
    rows = db.trading_rules_list()
    if not rows:
        rows = rmod.normalize_rules(None)
        db.trading_rules_save(rows)
    return rows


# --------------------------------------------------------------- 启停控制
def _loop() -> None:
    while not _STOP.is_set():
        try:
            tick()
        except Exception as exc:  # noqa: BLE001
            logger.exception("实时循环 tick 失败")
            _STATE.last_error = str(exc)
        _STOP.wait(max(1.0, float(_STATE.interval)))


def start(interval: Optional[float] = None,
          scheduled: Optional[bool] = None) -> Dict[str, Any]:
    global _LOOP_THREAD
    with _LOCK:
        if interval:
            _STATE.interval = float(interval)
        if scheduled is not None:
            _STATE.scheduled = bool(scheduled)
        if _STATE.running:
            return _STATE.snapshot()
        _STATE.running = True
        _STATE.last_error = None
        _STOP.clear()
        _LOOP_THREAD = threading.Thread(target=_loop, daemon=True,
                                        name="live-trading-loop")
        _LOOP_THREAD.start()
    return _STATE.snapshot()


def stop() -> Dict[str, Any]:
    with _LOCK:
        _STATE.running = False
        _STOP.set()
    return _STATE.snapshot()


def state() -> Dict[str, Any]:
    """面板状态 + 账户 + 持仓（带最新价与浮动盈亏）。"""
    snap = _STATE.snapshot()
    acct = db.live_account_get()
    positions = db.live_positions_list()
    # 浮动盈亏：用最近一轮的实时价（ind_map 不在快照里，重新用缓存价估）
    pos_out = []
    pos_value = 0.0
    for p in positions:
        price = _last_price(str(p["code"]), p.get("avg_cost"))
        qty = int(p["qty"])
        mv = qty * price
        pos_value += mv
        pos_out.append({**p, "price": price, "market_value": mv,
                        "pnl": mv - qty * float(p.get("avg_cost") or 0.0)})
    cash = float((acct or {}).get("cash") or 0.0)
    equity = cash + pos_value
    snap.update({
        "account": {**(acct or {}),
                    "position_value": pos_value, "equity": equity,
                    "total_pnl": equity - float((acct or {}).get("initial_cash") or equity)},
        "positions": pos_out,
    })
    return snap


def _last_price(code: str, fallback: Optional[float]) -> float:
    """最近一轮实时价（从历史缓存最新价回落；拿不到用成本）。"""
    hist = _HIST.get(code)
    if hist and hist[-1].get("close") is not None:
        return float(hist[-1]["close"])
    return float(fallback or 0.0)


def reset() -> Dict[str, Any]:
    """重置面板内存状态（不动库里的账户/持仓/流水）。"""
    global _STATE
    _STATE = _State()
    return _STATE.snapshot()
