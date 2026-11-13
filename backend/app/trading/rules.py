# -*- coding: utf-8 -*-
"""选股/退出规则引擎 —— 把「规则」从策略代码里抽出来，变成可配置的数据。

## 这个模块解决什么

改造前规则硬编码在 `strategy.py`（趋势 MA / 流动性 / 分数 / 止损 … 写死在
函数里），用户**无法选规则、改规则、加规则**。本模块把每条规则抽象成
「类型 + 参数」的**数据**，于是：

- **选规则**：一堆预置规则，每条一个开关（enabled）。
- **改规则**：改某条规则的参数（如止损 8% → 10%）。
- **加规则**：从**规则类型清单**里挑一种、填参数，新增一条实例。

## 一个诚实的边界（别指望纯配置包办一切）

「加规则」= 加**已有规则类型**的一个实例（参数可配）。若要一种**全新逻辑**
（例如「MACD 顶背离」），必须在 `_BUY_TYPES` / `_SELL_TYPES` 里新增一个
求值器 —— 那是写代码，不是填配置。本模块不假装纯配置能表达任意算法。

## 规则的两族

- **filter（选股/过滤）**：对候选池逐票判定，决定**买不买**。按顺序求值，
  形成「漏斗」：每条规则统计拦下几只、通过几只 —— 正是页面上「xxx 规则
  过滤出多少只」那一列。
- **exit（退出）**：对**持仓**逐票判定，决定**卖不卖**。命中即卖，独立记因。

## 求值契约：纯函数 + 预计算指标

求值器不自己联网、不读库。它只读一个 `ind`（indicators，指标快照）——
由调用方（实时循环 / 回测）预先算好。这样规则引擎**纯计算、可离线单测**，
且实时与回测共用同一套规则口径，不会两边算出不一样的结果。

`ind` 形状（字段按需，缺了求值器自行判 None 并留痕，绝不臆断）::

    {
      "code": "600519", "name": "贵州茅台",
      "price": 1680.0,        # 最新价（实时）或收盘价（回测）
      "open": .., "high": .., "low": ..,
      "change_pct": 1.2,      # 当日涨跌幅 %
      "volume": .., "amount": ..,     # 当日成交量 / 成交额
      "score": 82.0,          # 六维综合分（选股池来源）
      "ma":    {"20": 1650.0, "10": 1665.0},   # 含当日的 N 日均线
      "avg_amount": {"5": 6.2e8},              # 近 N 日日均成交额
      "high_n":  {"20": 1720.0},               # 近 N 日最高价（不含当日，突破基准）
    }
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Dict, List, Optional, Tuple

#: 规则引擎版本。**求值逻辑**变更时 bump —— 与参数/规则实例无关。
RULE_ENGINE_VERSION = "rule-engine-v1"

#: 求值结果 = (是否通过/触发, 人读说明)。
EvalResult = Tuple[bool, str]


# --------------------------------------------------------------- 指标读取件
def _f(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # NaN → None


def _ma(ind: Dict[str, Any], n: int) -> Optional[float]:
    return _f((ind.get("ma") or {}).get(str(n)))


def _avg_amount(ind: Dict[str, Any], n: int) -> Optional[float]:
    return _f((ind.get("avg_amount") or {}).get(str(n)))


def _high_n(ind: Dict[str, Any], n: int) -> Optional[float]:
    return _f((ind.get("high_n") or {}).get(str(n)))


# --------------------------------------------------------------- 买入/过滤求值
def _eval_trend_ma(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    n = int(p["ma"])
    price = _f(ind.get("price"))
    ma = _ma(ind, n)
    if price is None:
        return False, "最新价缺失，趋势不可判"
    if ma is None:
        return False, "MA%d 窗口不足/缺价，趋势不可判（不拿残窗冒充）" % n
    ok = price >= ma
    return ok, "最新价 %.2f %s MA%d %.2f" % (
        price, ">=" if ok else "<", n, ma)


def _eval_score_min(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    score = _f(ind.get("score"))
    if score is None:
        return False, "六维综合分缺失"
    ok = score >= float(p["min_score"])
    return ok, "综合分 %.2f %s 下限 %.2f" % (
        score, ">=" if ok else "<", float(p["min_score"]))


def _eval_amount_min(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    n = int(p["days"])
    avg = _avg_amount(ind, n)
    if avg is None:
        return False, "近 %d 日成交额窗口不完整，流动性不可判" % n
    ok = avg >= float(p["min_amount"])
    return ok, "近%d日均成交额 %.0f %s 下限 %.0f" % (
        n, avg, ">=" if ok else "<", float(p["min_amount"]))


def _eval_st_filter(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    name = str(ind.get("name") or "")
    is_st = "ST" in name.upper()
    return (not is_st), ("名称 %s 含 ST，排除" % name if is_st else "非 ST")


def _eval_price_range(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    price = _f(ind.get("price"))
    if price is None:
        return False, "最新价缺失"
    lo, hi = float(p["lo"]), float(p["hi"])
    ok = lo <= price <= hi
    return ok, "最新价 %.2f %s 区间 [%.2f, %.2f]" % (
        price, "在" if ok else "不在", lo, hi)


def _eval_pct_change(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    chg = _f(ind.get("change_pct"))
    if chg is None:
        return False, "涨跌幅缺失"
    lo, hi = float(p["lo"]), float(p["hi"])
    ok = lo <= chg <= hi
    return ok, "涨跌幅 %.2f%% %s 区间 [%.2f, %.2f]" % (
        chg, "在" if ok else "不在", lo, hi)


def _eval_ma_cross(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    """金叉 = 快线在慢线之上（实时口径看**当前**快/慢线位置）。

    注：真正的「今交叉」需要前一日快/慢线；实时循环只保证当下快线 > 慢线，
    判定为「多头排列」而非严格金叉。说明里如实标注，不冒充金叉。
    """
    fast_n, slow_n = int(p["fast"]), int(p["slow"])
    fast, slow = _ma(ind, fast_n), _ma(ind, slow_n)
    if fast is None or slow is None:
        return False, "MA%d/MA%d 窗口不足，不可判" % (fast_n, slow_n)
    ok = fast > slow
    return ok, "MA%d %.2f %s MA%d %.2f（多头排列）" % (
        fast_n, fast, ">" if ok else "<=", slow_n, slow)


def _eval_volume_surge(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    n = int(p["days"])
    mult = float(p["mult"])
    amount = _f(ind.get("amount"))
    avg_amt = _avg_amount(ind, n)
    if amount is None or avg_amt is None or avg_amt <= 0:
        return False, "近 %d 日均量/额窗口不完整，放量不可判" % n
    ok = amount >= avg_amt * mult
    return ok, "当日额 %.0f %s 近%d日均额 %.0f × %.1f" % (
        amount, ">=" if ok else "<", n, avg_amt, mult)


def _eval_breakout(ind: Dict[str, Any], p: Dict[str, Any]) -> EvalResult:
    n = int(p["days"])
    price = _f(ind.get("price"))
    base = _high_n(ind, n)
    if price is None or base is None:
        return False, "近 %d 日高点/最新价缺失，突破不可判" % n
    ok = price > base
    return ok, "最新价 %.2f %s 近%d日高点 %.2f" % (
        price, "突破" if ok else "未破", n, base)


# --------------------------------------------------------------- 退出求值
def _eval_stop_loss(ind: Dict[str, Any], p: Dict[str, Any],
                    pos: Dict[str, Any]) -> EvalResult:
    cost = _f(pos.get("avg_cost"))
    price = _f(ind.get("price"))
    if cost is None or price is None:
        return False, "成本/现价缺失，止损不可判"
    line = cost * (1.0 - float(p["pct"]))
    ok = price <= line
    return ok, "现价 %.2f %s 止损线 %.2f（成本 %.2f -%s%%）" % (
        price, "<=" if ok else ">", line, cost, float(p["pct"]) * 100)


def _eval_take_profit(ind: Dict[str, Any], p: Dict[str, Any],
                      pos: Dict[str, Any]) -> EvalResult:
    cost = _f(pos.get("avg_cost"))
    price = _f(ind.get("price"))
    if cost is None or price is None:
        return False, "成本/现价缺失，止盈不可判"
    line = cost * (1.0 + float(p["pct"]))
    ok = price >= line
    return ok, "现价 %.2f %s 止盈线 %.2f（成本 %.2f +%s%%）" % (
        price, ">=" if ok else "<", line, cost, float(p["pct"]) * 100)


def _eval_trailing_stop(ind: Dict[str, Any], p: Dict[str, Any],
                        pos: Dict[str, Any]) -> EvalResult:
    peak = _f(pos.get("peak_price")) or _f(ind.get("high"))
    price = _f(ind.get("price"))
    if peak is None or price is None:
        return False, "峰值/现价缺失，移动止盈不可判"
    line = peak * (1.0 - float(p["pct"]))
    ok = price <= line
    return ok, "现价 %.2f %s 峰值回撤线 %.2f（峰值 %.2f -%s%%）" % (
        price, "<=" if ok else ">", line, peak, float(p["pct"]) * 100)


def _eval_trend_break(ind: Dict[str, Any], p: Dict[str, Any],
                      pos: Dict[str, Any]) -> EvalResult:
    n = int(p["ma"])
    price = _f(ind.get("price"))
    ma = _ma(ind, n)
    if price is None:
        return False, "现价缺失，趋势不可判"
    if ma is None:
        return False, "MA%d 窗口不足，趋势不可判" % n
    ok = price < ma
    return ok, "现价 %.2f %s MA%d %.2f" % (price, "<" if ok else ">=", n, ma)


def _eval_max_hold(ind: Dict[str, Any], p: Dict[str, Any],
                   pos: Dict[str, Any]) -> EvalResult:
    held = pos.get("held_days")
    if held is None:
        return False, "持仓天数缺失"
    ok = int(held) >= int(p["days"])
    return ok, "已持仓 %s 个交易日 %s 上限 %s" % (
        held, ">=" if ok else "<", int(p["days"]))


def _eval_risk_exit(ind: Dict[str, Any], p: Dict[str, Any],
                    pos: Dict[str, Any]) -> EvalResult:
    risk = str(ind.get("risk_state") or "NORMAL")
    ok = risk == "RISK"
    return ok, "风险状态 %s（risk_exit 开关打开即强平）" % risk


# --------------------------------------------------------------- 类型注册表
# category: "filter" = 选股/过滤（决定买），"exit" = 退出（决定卖）。
# param_schema: 字段 -> {"type": "int|float|bool", "default": .., "min": .., "max": .., "label": ..}
# evaluate: filter → (ind, params)；exit → (ind, params, pos)。
_BUY_TYPES: Dict[str, Dict[str, Any]] = {
    "trend_ma": {
        "label": "趋势过滤（收盘/现价 ≥ MA）",
        "desc": "现价站上 N 日均线，顺势。窗口不足直接不通过（不拿残窗冒充）。",
        "params": {"ma": {"type": "int", "default": 20, "min": 2, "max": 250,
                          "label": "均线周期 N"}},
        "evaluate": _eval_trend_ma,
    },
    "score_min": {
        "label": "六维综合分下限",
        "desc": "只买综合分不低于阈值的候选。",
        "params": {"min_score": {"type": "float", "default": 0.0, "min": 0.0,
                                 "max": 100.0, "label": "最低综合分"}},
        "evaluate": _eval_score_min,
    },
    "amount_min": {
        "label": "流动性下限（近 N 日均成交额）",
        "desc": "流动性不足的票不碰，避免进得去出不来。",
        "params": {"days": {"type": "int", "default": 5, "min": 1, "max": 60,
                            "label": "窗口 N 日"},
                   "min_amount": {"type": "float", "default": 5e7, "min": 0.0,
                                  "max": 1e11, "label": "最低日均成交额（元）"}},
        "evaluate": _eval_amount_min,
    },
    "st_filter": {
        "label": "排除 ST",
        "desc": "名称含 ST 的一律不买。",
        "params": {},
        "evaluate": _eval_st_filter,
    },
    "price_range": {
        "label": "价格区间",
        "desc": "只买现价落在 [lo, hi] 内的票。",
        "params": {"lo": {"type": "float", "default": 0.0, "min": 0.0,
                          "max": 100000.0, "label": "价格下限"},
                   "hi": {"type": "float", "default": 100000.0, "min": 0.0,
                          "max": 100000.0, "label": "价格上限"}},
        "evaluate": _eval_price_range,
    },
    "pct_change": {
        "label": "当日涨跌幅区间",
        "desc": "只买当日涨幅落在 [lo, hi]%% 内的票（例如只要 0~5% 的温和上涨）。",
        "params": {"lo": {"type": "float", "default": -11.0, "min": -30.0,
                          "max": 30.0, "label": "涨幅下限 %"},
                   "hi": {"type": "float", "default": 11.0, "min": -30.0,
                          "max": 30.0, "label": "涨幅上限 %"}},
        "evaluate": _eval_pct_change,
    },
    "ma_cross": {
        "label": "均线多头（快线 > 慢线）",
        "desc": "短期均线上穿长期均线之上。注：实时口径判「多头排列」，非严格金叉。",
        "params": {"fast": {"type": "int", "default": 5, "min": 2, "max": 250,
                            "label": "快线 N"},
                   "slow": {"type": "int", "default": 20, "min": 3, "max": 250,
                            "label": "慢线 M"}},
        "evaluate": _eval_ma_cross,
    },
    "volume_surge": {
        "label": "放量（当日额 ≥ 均额 × 倍数）",
        "desc": "当日成交额较近 N 日均额放大到指定倍数，量价配合。",
        "params": {"days": {"type": "int", "default": 5, "min": 1, "max": 60,
                            "label": "窗口 N 日"},
                   "mult": {"type": "float", "default": 1.5, "min": 1.0,
                            "max": 10.0, "label": "放大倍数"}},
        "evaluate": _eval_volume_surge,
    },
    "breakout": {
        "label": "突破 N 日高点",
        "desc": "现价突破近 N 日最高价（不含当日）。",
        "params": {"days": {"type": "int", "default": 20, "min": 2, "max": 250,
                            "label": "突破周期 N"}},
        "evaluate": _eval_breakout,
    },
}

_SELL_TYPES: Dict[str, Dict[str, Any]] = {
    "stop_loss": {
        "label": "固定止损",
        "desc": "现价跌破成本 −N%%，离场。",
        "params": {"pct": {"type": "float", "default": 0.08, "min": 0.005,
                           "max": 0.5, "label": "止损幅度 N（0.08=8%）"}},
        "evaluate": _eval_stop_loss,
    },
    "take_profit": {
        "label": "固定止盈",
        "desc": "现价涨到成本 +N%%，离场。",
        "params": {"pct": {"type": "float", "default": 0.20, "min": 0.005,
                           "max": 2.0, "label": "止盈幅度 N（0.20=20%）"}},
        "evaluate": _eval_take_profit,
    },
    "trailing_stop": {
        "label": "移动止盈（自峰值回撤）",
        "desc": "现价自持仓期最高价回撤 N%%，锁定利润。",
        "params": {"pct": {"type": "float", "default": 0.10, "min": 0.005,
                           "max": 0.5, "label": "回撤幅度 N（0.10=10%）"}},
        "evaluate": _eval_trailing_stop,
    },
    "trend_break": {
        "label": "趋势破坏（现价 < MA）",
        "desc": "现价跌破 N 日均线，趋势走坏离场。",
        "params": {"ma": {"type": "int", "default": 10, "min": 2, "max": 250,
                          "label": "均线周期 N"}},
        "evaluate": _eval_trend_break,
    },
    "max_hold": {
        "label": "最大持仓天数",
        "desc": "持仓满 N 个交易日强制离场。",
        "params": {"days": {"type": "int", "default": 10, "min": 1, "max": 250,
                            "label": "最长持仓 N 日"}},
        "evaluate": _eval_max_hold,
    },
    "risk_exit": {
        "label": "风险状态强平",
        "desc": "数据风险状态下强平持仓（通常保持关闭，只禁新开仓）。",
        "params": {},
        "evaluate": _eval_risk_exit,
    },
}


def type_registry(category: str) -> Dict[str, Dict[str, Any]]:
    """给前端「新增规则」下拉用：某分类下可选的规则类型 + 参数模式。"""
    src = _BUY_TYPES if category == "filter" else _SELL_TYPES
    out: Dict[str, Dict[str, Any]] = {}
    for t, spec in src.items():
        out[t] = {"label": spec["label"], "desc": spec["desc"],
                  "params": spec["params"]}
    return out


# --------------------------------------------------------------- 默认规则
def _mk_default(rule_id: str, category: str, rtype: str, enabled: bool,
                params: Optional[Dict[str, Any]] = None, order: int = 0
                ) -> Dict[str, Any]:
    spec = (_BUY_TYPES if category == "filter" else _SELL_TYPES)[rtype]
    base = {k: v["default"] for k, v in spec["params"].items()}
    base.update(params or {})
    return {"id": rule_id, "category": category, "type": rtype,
            "name": spec["label"], "enabled": enabled, "order": order,
            "params": base}


#: 默认规则集。顺序即漏斗顺序（filter 类）。改这里 = 改出厂默认；已存库的
#: 规则以库为准，只有库为空时才落这批。**默认值是教科书起点，非验证过的最优。**
DEFAULT_RULES: List[Dict[str, Any]] = [
    _mk_default("buy_st", "filter", "st_filter", True, order=10),
    _mk_default("buy_score", "filter", "score_min", True,
                {"min_score": 0.0}, order=20),
    _mk_default("buy_trend", "filter", "trend_ma", True, {"ma": 20}, order=30),
    _mk_default("buy_amount", "filter", "amount_min", True,
                {"days": 5, "min_amount": 5e7}, order=40),
    _mk_default("buy_price", "filter", "price_range", False,
                {"lo": 0.0, "hi": 100000.0}, order=50),
    _mk_default("buy_pct", "filter", "pct_change", False,
                {"lo": -11.0, "hi": 11.0}, order=60),
    _mk_default("buy_cross", "filter", "ma_cross", False,
                {"fast": 5, "slow": 20}, order=70),
    _mk_default("buy_volume", "filter", "volume_surge", False,
                {"days": 5, "mult": 1.5}, order=80),
    _mk_default("buy_breakout", "filter", "breakout", False,
                {"days": 20}, order=90),
    # ---- 退出 ----
    _mk_default("sell_stop", "exit", "stop_loss", True, {"pct": 0.08},
                order=10),
    _mk_default("sell_profit", "exit", "take_profit", False, {"pct": 0.20},
                order=20),
    _mk_default("sell_trail", "exit", "trailing_stop", True, {"pct": 0.10},
                order=30),
    _mk_default("sell_trend", "exit", "trend_break", True, {"ma": 10},
                order=40),
    _mk_default("sell_maxhold", "exit", "max_hold", True, {"days": 10},
                order=50),
    _mk_default("sell_risk", "exit", "risk_exit", False, order=60),
]


# --------------------------------------------------------------- 校验 / 归一
def normalize_rule(raw: Dict[str, Any]) -> Dict[str, Any]:
    """校验并归一一条规则；未知类型/参数/越界一律抛 ValueError（不静默吞）。

    拼错的参数被静默丢掉、跑出来还是默认值 —— 正是本项目处处要拦的坑，
    这里同样拒绝而非忽略。
    """
    if not isinstance(raw, dict):
        raise ValueError("规则必须是对象")
    category = str(raw.get("category") or "")
    if category not in ("filter", "exit"):
        raise ValueError("规则 category 必须是 filter/exit，收到 %r" % category)
    rtype = str(raw.get("type") or "")
    spec = (_BUY_TYPES if category == "filter" else _SELL_TYPES).get(rtype)
    if spec is None:
        raise ValueError("未知规则类型 %r（分类 %s）" % (rtype, category))

    rule_id = str(raw.get("id") or "").strip()
    if not rule_id:
        raise ValueError("规则 id 不能为空")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("规则 %s 的 enabled 必须是布尔" % rule_id)
    try:
        order = int(raw.get("order", 0))
    except (TypeError, ValueError):
        raise ValueError("规则 %s 的 order 必须是整数" % rule_id)

    given = dict(raw.get("params") or {})
    unknown = set(given) - set(spec["params"])
    if unknown:
        raise ValueError("规则 %s 未知参数: %s" % (rule_id, ", ".join(sorted(unknown))))
    params: Dict[str, Any] = {}
    for key, ps in spec["params"].items():
        v = given[key] if key in given else ps["default"]
        typ = ps["type"]
        if typ == "bool":
            if not isinstance(v, bool):
                raise ValueError("规则 %s 参数 %s 必须是布尔" % (rule_id, key))
        elif typ in ("int", "float"):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError("规则 %s 参数 %s 必须是数值，收到 %r"
                                 % (rule_id, key, v))
            if typ == "int":
                if int(v) != v:
                    raise ValueError("规则 %s 参数 %s 必须是整数" % (rule_id, key))
                v = int(v)
            else:
                v = float(v)
            if "min" in ps and v < ps["min"]:
                raise ValueError("规则 %s 参数 %s 越界下限 %s，收到 %r"
                                 % (rule_id, key, ps["min"], v))
            if "max" in ps and v > ps["max"]:
                raise ValueError("规则 %s 参数 %s 越界上限 %s，收到 %r"
                                 % (rule_id, key, ps["max"], v))
        params[key] = v

    return {"id": rule_id, "category": category, "type": rtype,
            "name": str(raw.get("name") or spec["label"]),
            "enabled": enabled, "order": order, "params": params}


def normalize_rules(rules: Optional[List[Dict[str, Any]]] = None
                    ) -> List[Dict[str, Any]]:
    """归一整套规则，校验 id 唯一，按 (category, order, id) 稳定排序。"""
    if rules is None:
        rules = DEFAULT_RULES
    out = [normalize_rule(r) for r in rules]
    ids = [r["id"] for r in out]
    if len(set(ids)) != len(ids):
        raise ValueError("规则 id 重复：%s" % ", ".join(sorted(ids)))
    out.sort(key=lambda r: (0 if r["category"] == "filter" else 1,
                            r["order"], r["id"]))
    return out


def rules_version(rules: Optional[List[Dict[str, Any]]] = None) -> str:
    """规则集版本 = 归一化 JSON 的 sha256 前 12 位（内容哈希，改一字就变）。"""
    canon = json.dumps(normalize_rules(rules), sort_keys=True,
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------- 求值入口
def enabled_rules(rules: List[Dict[str, Any]], category: str) -> List[Dict[str, Any]]:
    return [r for r in normalize_rules(rules)
            if r["category"] == category and r["enabled"]]


def eval_filter_funnel(candidates: List[Dict[str, Any]],
                       rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对候选池顺序跑 filter 规则，产出「漏斗」：每条规则拦下/通过几只。

    返回::

        {"stages": [{"rule_id","name","type","passed_n","blocked_n","detail0"}],
         "passed": [候选 ind, ...],      # 全部 filter 规则都通过的票
         "per_code": {code: {"passed": bool,
                             "stops": [{"rule","detail"}]}}}   # 每票在哪条被拦
    """
    frules = enabled_rules(rules, "filter")
    stages: List[Dict[str, Any]] = []
    per_code: Dict[str, Dict[str, Any]] = {
        str(c.get("code")): {"passed": True, "stops": []} for c in candidates
    }
    alive = list(candidates)

    for rule in frules:
        spec = _BUY_TYPES[rule["type"]]
        evaluate = spec["evaluate"]
        blocked_n = 0
        first_detail = ""
        still: List[Dict[str, Any]] = []
        for c in alive:
            code = str(c.get("code"))
            ok, detail = evaluate(c, rule["params"])
            if not first_detail:
                first_detail = detail
            if ok:
                still.append(c)
            else:
                blocked_n += 1
                per_code[code]["passed"] = False
                per_code[code]["stops"].append({"rule": rule["name"],
                                                "detail": detail})
        stages.append({"rule_id": rule["id"], "name": rule["name"],
                       "type": rule["type"], "passed_n": len(still),
                       "blocked_n": blocked_n, "detail0": first_detail})
        alive = still

    return {"stages": stages, "passed": alive, "per_code": per_code}


def eval_exit(ind: Dict[str, Any], pos: Dict[str, Any],
              rules: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对单个持仓跑 exit 规则；命中任一即 SELL（整仓），否则 HOLD。

    返回 {"side": "SELL"|"HOLD", "triggers": [{"rule","detail"}],
          "checked": [{"rule","detail"}]} —— 触发与「检查过什么」都留痕。
    """
    erules = enabled_rules(rules, "exit")
    triggers: List[Dict[str, Any]] = []
    checked: List[Dict[str, Any]] = []
    for rule in erules:
        spec = _SELL_TYPES[rule["type"]]
        ok, detail = spec["evaluate"](ind, rule["params"], pos)
        checked.append({"rule": rule["name"], "detail": detail})
        if ok:
            triggers.append({"rule": rule["name"], "detail": detail})
    return {"side": "SELL" if triggers else "HOLD",
            "triggers": triggers, "checked": checked}
