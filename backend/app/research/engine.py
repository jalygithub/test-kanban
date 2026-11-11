# -*- coding: utf-8 -*-
"""阶段 3 —— 基准回测引擎（`BacktestEngine`）。

与实时盘后任务**完全解耦**：只读 `snapshot` / `daily_bar` 类表，只写
`research_*` 五张表，不碰 `scheduler`、不改 `mainforce._score`、不读
`scorer.yaml` 的"最新值"（配置一律取实验创建时冻结的那份）。

## 三条不能违反的口径

1. **信号来自快照，不重算。** 日线不落库（见 `implementation-audit.md`），
   从原始行情重算历史日分数既不可行也会引入未来数据泄漏。所以信号直接读
   `main_force` 快照里那次真实打分的产出，并把 `config_hash` / 模型版本 /
   快照抓取时刻一并入档 —— 每条信号都能追溯回信号日、数据版本、模型版本。
2. **缺数据写 NULL，不写 0。** 0 是一个真实的收益率；把"没算出来"记成 0
   会直接污染胜率与均值。观察期没走完是 `PENDING`，缺行情是 `NO_DATA`，
   两者都不进收益统计。
3. **理论收益与可执行收益分开存。** 开盘涨停买不进、跌停卖不出时，理论
   价格仍可算，但可执行收益必须留空（`fill_status=UNFILLABLE`）。
   成本按**模拟成交金额**逐项算（佣金+印花税+滑点），不是在收益率上
   随便减一个固定百分比。

## 排序

`mainforce._score` 只按 `score` 降序，同分时依赖候选池顺序 —— 那是打分
流水线的内部顺序，对回测而言不算确定性规则。研究层在此之上加一条
**同分按证券代码升序**的裁决，并把规则版本 `SORT_VERSION` 写进信号，
这样换一次池序也不会让排名悄悄变化。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.research.audit import _q, discover_bar_tables
from app.store import db

logger = logging.getLogger(__name__)

# 排序规则版本。改规则必须改版本号，否则新旧实验的 rank 会被混着比较。
SORT_VERSION = "score-desc-code-asc-v1"

# 观察周期（**交易日**）。信号日 = D，入场参考时点 = D+1。
HORIZONS: Tuple[int, ...] = (1, 3, 5, 10, 20)

# 组合规则。首期一律**等权**，不引入仓位优化参数。
PORTFOLIOS: Tuple[str, ...] = ("top5", "top10", "top30")

# 交易成本默认值 —— **研究假设，不等于使用者实际券商费率**。
# 建实验时会把这份数值冻结进 config_snapshot，之后改这里不影响已建实验。
DEFAULT_COST: Dict[str, Any] = {
    "commission_rate": 0.00025,   # 佣金，买卖双向各收一次（万 2.5）
    "stamp_rate": 0.0005,         # 卖出印花税（千分之 0.5）
    "slippage": 0.001,            # 滑点：买入抬价、卖出压价各 0.1%
    "min_commission": 5.0,        # 单边最低佣金（元）
    "notional": 100000.0,         # 单只模拟投入（元），用于把成本换算成金额
    "assumption": True,           # 标记：以上为研究假设，非实际费率
}

# 交易所标准涨跌幅（**规则事实，不是估计**）：主板 10%，创业板/科创板 20%，
# 北交所 30%。
# 已知局限（必须随结果一起暴露，不能静默）：历史 **ST 状态不可得**（数据审计
# 中 `historical_st_status=BLOCKED`），ST 股 5% 的涨停可能漏判。因此
# `build_outcome` 会把该风险写进 quality_notes，而不是假装判定是完备的。
_BOARD_LIMIT = (
    (("30", "68"), 0.20),   # 创业板 30xxxx / 科创板 68xxxx
    (("8", "4"), 0.30),     # 北交所 8xxxxx / 4xxxxx（新代码段）
)


def limit_ratio(code: str) -> float:
    """该代码的涨跌停幅度。规则来源是交易所规定，非估计值。"""
    c = str(code or "")
    for prefixes, ratio in _BOARD_LIMIT:
        if any(c.startswith(p) for p in prefixes):
            return ratio
    return 0.10


# ======================================================== 可单测的纯函数
def replay_score(row: Dict[str, Any],
                 used_weights: Dict[str, float]) -> Tuple[Optional[float], float]:
    """从快照里存的**逐维百分位**重算综合分，返回 (score, coverage)。

    必须与 `mainforce._score` 的聚合方式逐位对齐（它就是照着那段写的）::

        total  = Σ used_weights
        score  = Σ_{dims 存在的维} dim_pct × weight / total      # 缺维按 0 计入
        cov    = Σ_{dims 存在的维} weight / total

    注意**不重归一化**：某只股票缺一个维度时，`_score` 是把它那份权重按 0
    算进去的（`series.fillna(0.0) * share`），覆盖率单独记账。这里若"贴心"
    地把剩余维度重新归一，分就会系统性偏高，回测也就和线上对不上了。

    误差来源：快照里的 `dim_pct` 已 `round(1)`、`score` 已 `round(2)`，
    所以重算值与入库值会有约 ±0.06 的偏差，属正常，测试按此容差断言。
    """
    total = float(sum(used_weights.values()))
    if total <= 0:
        return None, 0.0
    acc = 0.0
    cov = 0.0
    for key, dim in (row.get("dims") or {}).items():
        if not isinstance(dim, dict):
            continue
        weight = used_weights.get(key, dim.get("weight"))
        if weight is None:
            continue
        w = float(weight)
        acc += float(dim.get("score") or 0.0) * (w / total)
        cov += w / total
    return (acc, cov) if cov > 0 else (None, 0.0)


def rank_signals(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """确定性排序：综合分降序、证券代码升序。返回值带 `rank`（1 起）。

    并列裁决是**研究层**加的，打分流水线原样不动 —— 它只按 score 降序，
    同分时保留池序。回测必须与池序无关，否则换一次预选顺序，Top5 就变了。
    """
    ordered = sorted(
        rows,
        key=lambda r: (-float(r.get("score") or 0.0), str(r.get("code") or "")),
    )
    out: List[Dict[str, Any]] = []
    for i, r in enumerate(ordered, start=1):
        item = dict(r)
        item["rank"] = i
        out.append(item)
    return out


def drop_weights(base: Dict[str, float], drop: Sequence[str]
                 ) -> Dict[str, float]:
    """因子消融：把若干维的权重**归零**，其余保持绝对值不变。

    归零而不是"重归一剩下的"，是为了与 `_score` 的缺维口径走同一条路：
    它把缺的那份权重按 0 分计入（`mainforce.py:979`）。若这里改成重归一，
    消融实验就会长出第二套聚合规则，结论既不可比也不可复现。

    代价是总分整体下降 —— 那是口径的必然结果，消融看的是**排序变化**，
    不是绝对分高低。
    """
    z = {k for k in (drop or ())}
    return {k: (0.0 if k in z else float(v)) for k, v in base.items()}


def reweight_rank(rows: Sequence[Dict[str, Any]],
                  weights: Dict[str, float]) -> List[Dict[str, Any]]:
    """用**给定权重**对快照里的逐维百分位分重新聚合并排序。

    这是因子消融在当前数据条件下唯一诚实的做法：日线不落库，逐维百分位
    无法重算（重算即需要原始行情，且 qfq 有未来泄漏）。能改的只有聚合权重。

    因此消融的结论**只能**表述为"换权重后排名/收益怎么变"，**不能**表述为
    "该因子的边际贡献是多少" —— 逐维分本身是当日全市场的横截面产物，
    拿它重加权推不出因子的因果贡献。

    权重耗尽（某股所有维都不在 `weights` 里）的行会被剔除而不是按 0 分垫底：
    0 分和"这股没有可用维度"是两回事，混在一起会污染 TopN。
    """
    scored: List[Dict[str, Any]] = []
    for r in rows:
        dims = r.get("dims") or {}
        sc, cov = replay_score({"dims": dims}, weights)
        if sc is None:
            continue
        scored.append({"code": r.get("code"), "score": round(sc, 2),
                       "coverage": cov, "dims": dims,
                       "snapshot_score": r.get("snapshot_score")})
    return rank_signals(scored)


def theoretical_return(entry_price: Optional[float],
                       exit_price: Optional[float]) -> Optional[float]:
    """理论收益率 (exit-entry)/entry。任一端缺失或入场价非正 -> None（不是 0）。"""
    if entry_price is None or exit_price is None:
        return None
    try:
        e = float(entry_price)
        x = float(exit_price)
    except (TypeError, ValueError):
        return None
    if e <= 0:
        return None
    return (x - e) / e


def apply_costs(entry_price: float, exit_price: float,
                qty: float, cost: Dict[str, Any]) -> Dict[str, Any]:
    """按**模拟成交金额**逐项计费，返回成本与净收益。

    返回字段::

        buy_amount   买入成交额 = qty × 买入实付价（含滑点）
        sell_amount  卖出成交额 = qty × 卖出实收价（含滑点）
        commission   佣金，双向，单边不低于 min_commission
        stamp        卖出印花税
        total_cost   commission + stamp
        net_return   扣费后收益率
        cost_ratio   总成本 / 买入成交额（收益率口径的等效扣减）

    费率是**线性**的，因此净收益率与 notional 无关；notional 只用于把成本
    换算成金额展示。这正是"不能只在最终收益上减一个固定百分比"的落点 ——
    佣金按成交额、印花税只按卖出，三者不会被压缩成同一个数。
    """
    slip = float(cost.get("slippage", 0.0))
    buy_px = float(entry_price) * (1.0 + slip)
    sell_px = float(exit_price) * (1.0 - slip)
    buy_amount = qty * buy_px
    sell_amount = qty * sell_px
    rate = float(cost.get("commission_rate", 0.0))
    floor = float(cost.get("min_commission", 0.0))
    commission = max(buy_amount * rate, floor) + max(sell_amount * rate, floor)
    stamp = sell_amount * float(cost.get("stamp_rate", 0.0))
    total_cost = commission + stamp
    if buy_amount <= 0:
        return {"buy_amount": buy_amount, "sell_amount": sell_amount,
                "commission": commission, "stamp": stamp,
                "total_cost": total_cost, "net_return": None,
                "cost_ratio": None}
    return {
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "commission": commission,
        "stamp": stamp,
        "total_cost": total_cost,
        "net_return": (sell_amount - buy_amount - total_cost) / buy_amount,
        "cost_ratio": total_cost / buy_amount,
    }


# ---------------------------------------------------------------- 复权口径
#
# 阶段 5 的落点。**所有收益字段一律按折算后的可比价算**，并在记录上标注口径。
#
# 为什么必须有这一层：回填落的是**不复权** OHLC（`backfill.py` 的硬口径）。
# 不复权价在除权日会跳空下跌 —— 一只 10 元每 10 股派 2 元的票，除权那天收盘
# 从 10.00 变成 8.00，看着像 -20% 的暴跌，实际上持有人一分钱没少。
# 拿不复权价直接跨除权日算收益，长期回测会被这类「假亏损」系统性拉垮。
#
# 后复权因子（adj_factor）随除权单调递增：某日的后复权价 = 不复权价 × 因子。
# 两端各乘自己的因子再比，比值才是真实收益率。

ADJ_RAW = "RAW"                        # 两端因子都有且不同 => 折算过
ADJ_UNADJUSTED = "UNADJUSTED"          # 因子相同 => 期间无除权，不复权价直算即正确
ADJ_UNCERTAIN = "UNADJUSTED_UNCERTAIN"  # 缺因子 => **判不了**，可能含除权跳空

ADJ_METHODS = (ADJ_RAW, ADJ_UNADJUSTED, ADJ_UNCERTAIN)

_ADJ_UNCERTAIN_NOTE = (
    "缺复权因子（adj_factor），收益按不复权价直算 —— "
    "若入场与出场之间发生过除权，该收益**不反映真实回报**")


def _factor(bar: Optional[Dict[str, Any]]) -> Optional[float]:
    """从一根 K 线里取复权因子；取不到或非正一律回 None（**不回 1.0**）。

    回 1.0 等于悄悄声明「没有复权」—— 那是口径不是数据，而缺数据时我们并不
    知道到底有没有除权。这与 `backfill.py` 里 `adj_factor` 取不到写 NULL
    是同一条原则的两端。
    """
    if not bar:
        return None
    try:
        v = bar.get("adj_factor")
        if v is None:
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def price_adjust(entry_bar: Optional[Dict[str, Any]],
                 exit_bar: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """入场/出场两根 K 线的折算系数与口径标注。

    返回 ``{entry_factor, exit_factor, method, note}``。两个因子**恒非 None**
    （取不到时给 1.0 表示「不折算」），但只要有一端取不到，``method`` 就是
    ``UNADJUSTED_UNCERTAIN``，`note` 会写明这段收益可能含除权跳空。

    :param entry_bar: 入场日（D+1）那根 K 线，用它的 **open** 算入场
    :param exit_bar:  出场日（T+n）那根 K 线，用它的 **close** 算出场
    """
    ef, xf = _factor(entry_bar), _factor(exit_bar)
    if ef is None or xf is None:
        return {"entry_factor": 1.0, "exit_factor": 1.0,
                "method": ADJ_UNCERTAIN, "note": _ADJ_UNCERTAIN_NOTE}
    if abs(ef - xf) < 1e-9:
        # 因子相同 = 这段没发生过除权，不复权价直算就是对的。标 UNADJUSTED
        # 而不是 RAW —— 没有折算动作就不该说自己折算过。
        return {"entry_factor": ef, "exit_factor": xf,
                "method": ADJ_UNADJUSTED, "note": None}
    return {"entry_factor": ef, "exit_factor": xf,
            "method": ADJ_RAW,
            "note": "已按 adj_factor 折算成可比价"}


def percentile(values: Sequence[float], p: float) -> Optional[float]:
    """线性插值分位数（p ∈ 0~100）。空序列返回 None，**不返回 0**。

    口径与 numpy 的 `linear` 一致：先把样本升序，再按 (n-1)×q 定位插值。
    """
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    nums.sort()
    if len(nums) == 1:
        return nums[0]
    q = max(0.0, min(100.0, float(p))) / 100.0
    pos = (len(nums) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(nums) - 1)
    frac = pos - lo
    return nums[lo] + (nums[hi] - nums[lo]) * frac


def summarize(values: Sequence[Optional[float]],
              pending: int = 0, no_data: int = 0) -> Dict[str, Any]:
    """描述性统计。**只对已完成且非 None 的样本求统计**。

    按任务书：重叠持有期的样本彼此相关，第一版只给描述性统计，
    不做也不输出任何显著性检验结论。
    """
    nums = [float(v) for v in values if v is not None]
    n = len(nums)
    if not n:
        return {"count": 0, "pending": pending, "no_data": no_data,
                "avg": None, "median": None, "win_rate": None,
                "p10": None, "p25": None, "p75": None, "p90": None,
                "notes": ["无有效样本，统计不可算。"]}
    nums_sorted = sorted(nums)
    return {
        "count": n,
        "pending": pending,
        "no_data": no_data,
        "avg": sum(nums) / n,
        "median": percentile(nums, 50),
        "win_rate": sum(1 for v in nums if v > 0) / n,
        "p10": percentile(nums, 10),
        "p25": percentile(nums, 25),
        "p75": percentile(nums, 75),
        "p90": percentile(nums, 90),
        "notes": [],
    }


# ==================================================== 行情读取（只读、离线）
def _open_ro() -> sqlite3.Connection:
    """**只读**连接，专供回测读行情。

    与 `db` 模块那条可写连接分开设：SQLite 默认（非 WAL）下读写同文件会
    抢锁，所以引擎一律「先读完、关掉只读连接、再写 research_* 表」。
    """
    con = sqlite3.connect(Path(settings.db_path).resolve().as_uri() + "?mode=ro",
                          uri=True)
    con.row_factory = sqlite3.Row
    return con


def _bar_source(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    """第一张可用日线表；没有日线表返回 None（回测就此 BLOCKED）。"""
    for b in discover_bar_tables(conn):
        if b["date_col"] and b["code_col"]:
            return b
    return None


def _ref_dates(conn: sqlite3.Connection, b: Dict[str, Any]) -> List[str]:
    """交易日序列 = 日线表里出现过的日期（升序）。

    刻意**不走交易日历**：日历需要联网或依赖缓存新鲜度，而回测口径应当
    由「我们实际有行情的那些天」定义 —— 否则交易日历里有、行情却没有的
    那天会被算成一个不存在的观察点。
    """
    rows = conn.execute(
        "SELECT DISTINCT %s AS d FROM %s ORDER BY d"
        % (_q(b["date_col"]), _q(b["table"]))).fetchall()
    return [str(r["d"]) for r in rows]


def _load_bars(conn: sqlite3.Connection, b: Dict[str, Any],
               codes: Sequence[str], since: str) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """按代码分块取 OHLC。返回 {code: {date: {…}}}。"""
    low = {c.lower(): c for c in b["columns"]}
    need = [low[k] for k in ("open", "high", "low", "close") if k in low]
    # 复权因子：回填表（daily_bar）有这一列，老日线表没有。有就一起取出来，
    # 没有则收益一律按不复权价直算并标 UNADJUSTED_UNCERTAIN —— 不能因为表里
    # 没这列就当「没有除权」，那是在替数据做决定。
    if "adj_factor" in low:
        need.append(low["adj_factor"])
    T, C, D = _q(b["table"]), _q(b["code_col"]), _q(b["date_col"])
    sel = ", ".join([f"{C} AS b_code", f"{D} AS b_date"]
                    + [_q(c) for c in need])
    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    seq = list(dict.fromkeys(str(c) for c in codes))
    for i in range(0, len(seq), 400):
        part = seq[i:i + 400]
        ph = ",".join("?" for _ in part)
        for row in conn.execute(
                f"SELECT {sel} FROM {T} WHERE {C} IN ({ph}) AND {D} >= ?",
                (*part, since)):
            out.setdefault(str(row["b_code"]), {})[str(row["b_date"])] = dict(row)
    return out


def _load_bench(conn: sqlite3.Connection, since: str) -> Optional[Dict[str, float]]:
    """沪深300 收盘序列。

    **只有库里存在明确命名为指数的行情表时才返回数据**；否则返回 None。
    不去猜「某张表里有没有指数代码」—— 猜错会把个股行情当基准，
    超额收益就成了一笔糊涂账。库里没有可读的指数序列时这里就是 None，
    超额收益一律留空（不是 0）。审计 `audit._audit_benchmark` 用的是**同一套
    判据**（表名线索 + 代码字面量），两边结论不会一个说有一个说没有。
    """
    for t in (r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")):
        low = t.lower()
        if not any(k in low for k in ("index", "idx", "benchmark")):
            continue
        b = next((x for x in discover_bar_tables(conn) if x["table"] == t), None)
        if not b or not b["date_col"] or not b["code_col"]:
            continue
        low_cols = {c.lower(): c for c in b["columns"]}
        if "close" not in low_cols:
            continue
        rows = conn.execute(
            "SELECT %s AS d, %s AS c FROM %s WHERE %s IN ('sh000300','000300') "
            "AND %s >= ? ORDER BY %s" % (
                _q(b["date_col"]), _q(low_cols["close"]), _q(b["table"]),
                _q(b["code_col"]), _q(b["date_col"]), _q(b["date_col"])),
            (since,)).fetchall()
        if rows:
            return {str(r["d"]): float(r["c"]) for r in rows if r["c"] is not None}
    return None


def _at_limit(px: Optional[float], prev_close: Optional[float],
              code: str, up: bool) -> Optional[bool]:
    """是否触及涨/跌停价。返回 None = **判不了**（前收缺失），不臆断。

    涨停价按交易所规则四舍五入到分：`round(prev*(1±r), 2)`；容差半分
    （0.005）用来吸收定价取整带来的尾差。
    """
    if px is None or prev_close is None:
        return None
    try:
        p, prev = float(px), float(prev_close)
    except (TypeError, ValueError):
        return None
    if prev <= 0:
        return None
    r = limit_ratio(code)
    target = round(prev * (1.0 + r if up else 1.0 - r), 2)
    return (p >= target - 0.005) if up else (p <= target + 0.005)


def _outcome_base(signal_date: str, code: str, horizon: int,
                  status: str, reason: str) -> Dict[str, Any]:
    """统一的「不可算」骨架：所有收益字段一律 None，**不写 0**。

    ``adjust_method`` 也是 None —— 根本没算出收益，也就谈不上口径。给它填
    UNADJUSTED 会让人以为「算过了，只是没折算」。
    """
    return {
        "signal_date": signal_date, "code": code, "horizon": horizon,
        "status": status, "theory_return": None, "bench_return": None,
        "excess_return": None, "entry_date": None, "entry_price": None,
        "exit_date": None, "exit_price": None, "fill_status": None,
        "sell_status": None, "exec_return": None, "mfe": None, "mae": None,
        "adjust_method": None, "adjust_note": None,
        "entry_factor": None, "exit_factor": None,
        "data_quality": "BLOCKED", "missing_reason": reason,
    }


def build_outcome(signal_date: str, code: str, horizon: int,
                  bars: Dict[str, Dict[str, Any]], ref_dates: Sequence[str],
                  cost: Optional[Dict[str, Any]],
                  bench: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """算一个 (信号日, 代码, 观察期) 的结果。

    时点口径（写死在这里，前后端与文档都按这个读）::

        D      = 信号日（交易日）
        entry  = D 之后的第 1 个交易日，取**开盘价**
        exit   = D 之后的第 n 个交易日（T+n），取**收盘价**

    即 T+1 是「当天买当天卖」的最短观察，T+5 = 持有 5 个交易日。
    """
    if not ref_dates:
        return _outcome_base(signal_date, code, horizon, "NO_DATA",
                             "库里没有交易日序列（无日线表）")
    try:
        idx = ref_dates.index(signal_date)
    except ValueError:
        return _outcome_base(signal_date, code, horizon, "NO_DATA",
                             "信号日不在交易日序列内")
    entry_i = idx + 1
    exit_i = idx + horizon
    if entry_i >= len(ref_dates) or exit_i >= len(ref_dates):
        # 观察期没走完 —— 连"能不能买"都还没发生，明确记 NOT_DUE，
        # 与 NO_DATA（判不了）区分开：前者是"还没到时候"，后者是"数据不够"。
        out = _outcome_base(signal_date, code, horizon, "PENDING",
                            "观察期未走完（信号日之后的交易日不足）")
        out["fill_status"] = "NOT_DUE"
        out["sell_status"] = "NOT_DUE"
        return out

    entry_date = ref_dates[entry_i]
    exit_date = ref_dates[exit_i]

    entry_bar = bars.get(code, {}).get(entry_date)
    exit_bar = bars.get(code, {}).get(exit_date)
    if not entry_bar:
        return _outcome_base(signal_date, code, horizon, "NO_DATA",
                             f"{entry_date} 无行情（停牌或数据缺失）")
    entry_px = entry_bar.get("open")
    if entry_px is None:
        return _outcome_base(signal_date, code, horizon, "NO_DATA",
                             f"{entry_date} 缺开盘价")
    if not exit_bar:
        return _outcome_base(signal_date, code, horizon, "NO_DATA",
                             f"{exit_date} 无行情（停牌或数据缺失）")
    exit_px = exit_bar.get("close")
    if exit_px is None:
        return _outcome_base(signal_date, code, horizon, "NO_DATA",
                             f"{exit_date} 缺收盘价")

    # ---- 复权折算（阶段 5）----
    # 两端各乘自己的后复权因子再比，比值才是真实收益率。缺因子时两个系数都
    # 给 1.0（即不折算），数值与折算前**完全一致** —— 但口径被记下来了，
    # 读的人看得出这段收益可能含除权跳空。这跟「把缺失填成 0」是两回事：
    # 这里没有编造任何价格，只是把「我们不知道有没有除权」如实写在记录上。
    adj = price_adjust(entry_bar, exit_bar)
    adj_entry = float(entry_px) * adj["entry_factor"]
    adj_exit = float(exit_px) * adj["exit_factor"]

    theory = theoretical_return(adj_entry, adj_exit)
    if theory is None:
        return _outcome_base(signal_date, code, horizon, "NO_DATA",
                             "入场价非正，收益不可算")

    # ---- 入场成交判定（涨停买不进，不许默认买到了）----
    prev_close = bars.get(code, {}).get(signal_date, {}).get("close")
    limit_up = _at_limit(entry_px, prev_close, code, up=True)
    if limit_up is None:
        fill_status = "UNCONFIRMED"
    elif limit_up:
        fill_status = "UNFILLABLE"
    else:
        fill_status = "FILLED"

    # ---- 出场成交判定（跌停卖不出）----
    prev_for_exit = bars.get(code, {}).get(ref_dates[exit_i - 1], {}).get("close")
    if fill_status != "FILLED":
        sell_status = "NOT_DUE"
    else:
        limit_dn = _at_limit(exit_px, prev_for_exit, code, up=False)
        if limit_dn is None:
            sell_status = "UNCONFIRMED"
        elif limit_dn:
            sell_status = "LIMIT_DOWN"
        else:
            sell_status = "SELLABLE"

    # ---- 可执行收益：只有「买得进 + 卖得出 + 有成本模型」三条都成立才有 ----
    # 同样用折算后的价 —— 成本是线性的，net_return 与名义本金无关，但
    # min_commission 那道下限会随计价口径变，这一点已写进 adjust_note。
    exec_return: Optional[float] = None
    if cost and fill_status == "FILLED" and sell_status == "SELLABLE":
        qty = float(cost.get("notional", 100000.0)) / adj_entry
        exec_return = apply_costs(adj_entry, adj_exit, qty,
                                  cost).get("net_return")

    # ---- MFE / MAE：入场到出场这段的最高/最低相对入场价 ----
    # 窗口内**每一根** K 线用自己的因子折算；某日缺因子就退回入场日的系数，
    # 不整窗一刀切 —— 除权若发生在窗口中途，一刀切会把换算后的高低点算错。
    highs: List[float] = []
    lows: List[float] = []
    for d in ref_dates[entry_i:exit_i + 1]:
        r = bars.get(code, {}).get(d)
        if not r:
            continue
        f = _factor(r) or adj["entry_factor"]
        if r.get("high") is not None:
            highs.append(float(r["high"]) * f)
        if r.get("low") is not None:
            lows.append(float(r["low"]) * f)
    mfe = (max(highs) / adj_entry) - 1.0 if highs else None
    mae = (min(lows) / adj_entry) - 1.0 if lows else None

    # ---- 基准：日期必须与入场/出场**同日对齐**，缺就是缺 ----
    bench_return: Optional[float] = None
    if bench:
        be = bench.get(entry_date)
        bx = bench.get(exit_date)
        if be and bx:
            bench_return = (float(bx) - float(be)) / float(be)
    excess = (theory - bench_return) if bench_return is not None else None

    reasons: List[str] = []
    if fill_status == "UNFILLABLE":
        reasons.append(f"{entry_date} 开盘涨停，无法确认成交")
    elif fill_status == "UNCONFIRMED":
        reasons.append("前一交易日收盘价缺失，**无法确认**是否涨停买得进")
    if sell_status == "LIMIT_DOWN":
        reasons.append(f"{exit_date} 跌停，无法确认卖出")
    elif sell_status == "UNCONFIRMED":
        reasons.append("出场前一交易日收盘价缺失，无法确认跌停与否")
    if bench_return is None:
        reasons.append("基准行情缺失，超额收益留空（不填 0）")

    # `entry_price` / `exit_price` 记的是**真实成交价（不复权）**，不是折算价 ——
    # 价格字段回答「当时多少钱成交的」，收益字段回答「实际赚了多少」，两者
    # 口径不同不能混。折算系数单独存两列，谁想复核都能自己乘回去。
    return {
        "signal_date": signal_date, "code": code, "horizon": horizon,
        "status": "DONE", "theory_return": theory,
        "bench_return": bench_return, "excess_return": excess,
        "entry_date": entry_date, "entry_price": float(entry_px),
        "exit_date": exit_date, "exit_price": float(exit_px),
        "fill_status": fill_status, "sell_status": sell_status,
        "exec_return": exec_return, "mfe": mfe, "mae": mae,
        "adjust_method": adj["method"], "adjust_note": adj["note"],
        "entry_factor": adj["entry_factor"], "exit_factor": adj["exit_factor"],
        "data_quality": "PARTIAL" if reasons else "READY",
        "missing_reason": "；".join(reasons) or None,
    }


# ================================================================ 运行器
# 重算分与入库分的允许偏差。快照里 dim_pct 已 round(1)、score 已 round(2)，
# 逐维 0.05 的取整误差按权重摊到总分上，最坏约 0.06；超过就说明快照与
# 打分实现对不上，必须报警而不是照单全收。
REPLAY_TOL = 0.06

# 幸存者偏差：模式 A（当前股票池回看）的固有性质。写成常量是为了让它在
# 运行警告、指标质量说明和文档里保持**同一句话**，避免三处各说各的。
SURVIVORSHIP_NOTE = (
    "幸存者偏差（股票池模式 A 固有）：实验按**当前**股票池回看，"
    "已退市/长期停牌的证券不会出现在任何一天的 all_rows 里，"
    "长期收益被系统性高估。这是数据条件所限，不是可调参数；"
    "要消除它必须启用模式 B（历史股票池），而 B 当前 BLOCKED。")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class BacktestEngine:
    """一次实验的执行器。

    **不阻塞调用方** —— API 层拿线程调它，进度靠 `research_runs` 回读。
    运行状态与阶段写在库里，进程重启也不丢（任务书对后台任务的硬要求）。
    """

    def __init__(self, experiment_id: int, run_id: int) -> None:
        self.experiment_id = experiment_id
        self.run_id = run_id
        self.warnings: List[str] = []
        # 本轮结果按复权口径分成几档、各多少条。摆出来是为了让「有多少收益
        # 其实判不了有没有除权」这件事**看得到**，而不是埋在每行的字段里。
        self.adjust_counts: Dict[str, int] = {}

    # ---------------------------------------------------------- 小工具
    def _cancelled(self) -> bool:
        run = db.research_run_get(self.run_id)
        return bool(run and run.get("status") == "cancelled")

    def _stage(self, stage: str, progress: float) -> None:
        db.research_run_update(self.run_id, stage=stage, progress=progress,
                               warnings=self.warnings)

    def _warn(self, msg: str) -> None:
        self.warnings.append(msg)
        logger.warning("[研究] %s", msg)

    def _finish(self, status: str, error: Optional[str] = None) -> Dict[str, Any]:
        done = status in ("done", "cancelled")
        db.research_run_update(
            self.run_id, status=status,
            progress=100.0 if status == "done" else None,
            error_summary=error, finished=True, warnings=self.warnings)
        db.research_experiment_set_state(
            self.experiment_id, status=status,
            finished_at=None if status == "running" else _now(),
            error_summary=error)
        return {"ok": status == "done", "status": status,
                "warnings": list(self.warnings), "error": error,
                "adjust": dict(self.adjust_counts),
                "finished": done}

    # ---------------------------------------------------------- 主流程
    def run(self) -> Dict[str, Any]:
        try:
            return self._run()
        except Exception as exc:  # noqa: BLE001 异常必须落到库里，不能只打日志
            logger.exception("实验 %s 运行失败", self.experiment_id)
            msg = f"{type(exc).__name__}: {exc}"
            self._warn(msg)
            return self._finish("failed", error=msg)

    def _run(self) -> Dict[str, Any]:
        exp = db.research_experiment_get(self.experiment_id)
        if exp is None:
            raise ValueError(f"实验不存在：{self.experiment_id}")
        # 模式 B 在建实验时已被 db 拒绝；这里再拦一道 —— 万一有历史遗留行
        # 或库被手工改过，也不许按 A 跑完再标成 B。
        if exp.get("universe_mode") != db.UNIVERSE_MODE_CURRENT:
            raise ValueError(db.UNIVERSE_HISTORICAL_BLOCK_REASON)
        frozen = db.research_experiment_config(self.experiment_id)
        if frozen is None:
            raise ValueError("配置快照缺失，无法复现实验")
        config_hash, config = frozen
        if not isinstance(config, dict):
            raise ValueError("配置快照损坏，无法复现实验")
        if self._cancelled():
            return self._finish("cancelled")

        # 交易成本**默认不配**：没有业务方确认的费率，就不该凭空扣一笔。
        # 未配置时可执行收益一律留空（`exec_return` 为 NULL），理论收益照常给；
        # 想看扣费口径，得在建实验时显式传一个 `cost` 字典（见 DEFAULT_COST
        # 里那组标了 assumption=True 的研究假设值）。
        raw_cost = config.get("cost")
        if isinstance(raw_cost, dict) and raw_cost:
            cost: Optional[Dict[str, Any]] = {**DEFAULT_COST, **raw_cost}
            self._warn("本实验使用**研究假设**的交易成本（非实际券商费率）："
                       + json.dumps({k: cost[k] for k in DEFAULT_COST},
                                    ensure_ascii=False, sort_keys=True))
        else:
            cost = None
            self._warn("未配置交易成本：可执行收益（exec_return）一律留空，"
                       "不做扣费假设；理论收益不受影响。")
        horizons = tuple(int(h) for h in (config.get("horizons") or HORIZONS))
        portfolios = tuple(config.get("portfolios") or PORTFOLIOS)

        # 幸存者偏差是模式 A 的**固有性质**，不是可调参数 —— 每一轮都要挂在
        # 警告里，别让它只躺在文档里。已退市/长期停牌的证券在任何一天的快照
        # 里都不出现，长期收益因此被系统性高估。
        self._warn(SURVIVORSHIP_NOTE)

        # ---- 1) 采集：只读连接，读完就关，避免与写库抢锁 ----
        self._stage("采集信号与行情", 5)
        snaps, ref_dates, bars, bench = self._collect(
            exp["start_date"], exp["end_date"], horizons)
        if self._cancelled():
            return self._finish("cancelled")
        if not snaps:
            raise ValueError("区间内没有任何 main_force 快照，没有信号可回测")

        # ---- 2) 信号 ----
        self._stage("生成信号", 35)
        signals = self._build_signals(snaps, exp, config_hash, horizons)
        db.research_signals_replace(self.experiment_id, self.run_id, signals)
        self._warn_once_signals(len(signals), len(snaps))

        # 数据版本：main_force 以 (dataset, trade_date) 为唯一键，信号日就是
        # 它的数据版本；这里把区间与抓取时刻一并记进实验，方便事后核对
        # "当时用的是哪一批快照"。
        self._record_data_version(snaps)

        if self._cancelled():
            # 只写了信号、没写结果与指标 —— 这种半成品不该被当成完整结果，
            # 指标表留空就是最诚实的表达。
            return self._finish("cancelled")

        # ---- 3) 收益 ----
        self._stage("计算观察期收益", 70)
        if not ref_dates:
            self._warn("库里没有日线表：全部样本标记 NO_DATA，收益字段一律留空"
                       "（不填 0）。要解开它需要授权抓取并落库日线。")
        outcomes = self._build_outcomes(signals, horizons, bars, ref_dates,
                                        cost, bench)
        db.research_outcomes_replace(self.experiment_id, outcomes)
        # 收益刚算完，正是把复权口径的不确定性挂进警告的时机
        self._warn_adjust(outcomes)
        if self._cancelled():
            return self._finish("cancelled")

        # ---- 4) 指标 ----
        self._stage("汇总统计", 92)
        metrics = self._build_metrics(signals, outcomes, portfolios, horizons)
        db.research_metrics_replace(self.experiment_id, metrics)
        db.research_run_update(self.run_id, progress=100.0, stage="完成",
                               warnings=self.warnings)
        return self._finish("done")

    # ---------------------------------------------------------- 采集
    def _collect(self, start: str, end: str, horizons: Sequence[int]
                 ) -> Tuple[List[Dict[str, Any]], List[str],
                            Dict[str, Dict[str, Dict[str, Any]]],
                            Optional[Dict[str, float]]]:
        conn = _open_ro()
        try:
            rows = conn.execute(
                "SELECT trade_date, payload, fetched_at, source FROM snapshot"
                " WHERE dataset='main_force' AND trade_date BETWEEN ? AND ?"
                " ORDER BY trade_date", (start, end)).fetchall()
            snaps: List[Dict[str, Any]] = []
            codes: set = set()
            for r in rows:
                try:
                    obj = json.loads(r["payload"])[0]
                except (TypeError, ValueError, IndexError):
                    self._warn(f"{r['trade_date']} 的 main_force payload 无法解析，跳过该日")
                    continue
                snaps.append({"date": str(r["trade_date"]), "obj": obj,
                              "fetched_at": r["fetched_at"],
                              "source": r["source"]})
                for it in obj.get("all_rows") or []:
                    if isinstance(it, dict) and it.get("code"):
                        codes.add(str(it["code"]))

            bar_info = _bar_source(conn)
            ref_dates: List[str] = []
            bars: Dict[str, Dict[str, Dict[str, Any]]] = {}
            bench: Optional[Dict[str, float]] = None
            if bar_info is None:
                self._warn("未发现日线表（open/high/low/close 四列），"
                           "观察期收益全部不可算。")
            else:
                ref_dates = _ref_dates(conn, bar_info)
                # 最远需要看到「最后一个信号日 + 最大观察期」，但直接取全部
                # 比多算一次边界稳妥：since 用起始日，后面的裁剪在 build 里做。
                if snaps:
                    bars = _load_bars(conn, bar_info, codes, snaps[0]["date"])
                bench = _load_bench(conn, snaps[0]["date"] if snaps else start)
            return snaps, ref_dates, bars, bench
        finally:
            conn.close()

    def _warn_adjust(self, outcomes: Sequence[Dict[str, Any]]) -> None:
        """把「有多少收益其实判不了有没有除权」摆到运行警告里。

        **刻意不并进 `data_quality`**：那条维度回答的是「行情本身齐不齐」
        （缺入场/出场 K 线、缺基准），而复权口径回答的是「算出来的这个数
        可不可信」。两者混成一个，就会出现「行情齐全所以标 READY」、却没
        人看得出这批收益其实横跨了除权日 —— 那是最危险的一种看起来没问题。

        缺因子导致的不确定性在这里单独计数并挂进 warnings，同时随
        `_finish` 的返回值一起透出去（`adjust` 字段）。
        """
        cnt: Dict[str, int] = {}
        for o in outcomes:
            if o.get("status") != "DONE":
                continue          # 没算出收益的行不计入，它们本来就没有口径
            m = str(o.get("adjust_method") or "NONE")
            cnt[m] = cnt.get(m, 0) + 1
        self.adjust_counts = cnt
        if not cnt:
            return
        total = sum(cnt.values())
        if cnt.get(ADJ_UNCERTAIN):
            self._warn(
                f"复权口径：{cnt[ADJ_UNCERTAIN]}/{total} 条收益**判不了**"
                "期间是否发生过除权（日线表缺 adj_factor）。这些数字仍按"
                "不复权价直算 —— 若持仓区间内有除权，它不反映真实回报。"
                "要解开它需要回填 `daily_bar.adj_factor`（阶段 4 工具已就绪）。")
        elif cnt.get(ADJ_RAW):
            self._warn(f"复权口径：{cnt[ADJ_RAW]}/{total} 条已按 adj_factor "
                       f"折算成可比价。")
        # UNADJUSTED 不挂警告：因子相同 = 确认期间无除权，直算本就是对的。
        # 把它也写进警告会让真正的 UNCERTAIN 淹没在噪声里。

    def _warn_once_signals(self, n_signals: int, n_days: int) -> None:
        self._warn(f"本轮写入 {n_signals} 条信号，覆盖 {n_days} 个信号日；"
                   f"排序规则 {SORT_VERSION}（同分按证券代码升序）。")

    def _record_data_version(self, snaps: Sequence[Dict[str, Any]]) -> None:
        if not snaps:
            return
        first, last = snaps[0]["date"], snaps[-1]["date"]
        fetched = [s["fetched_at"] for s in snaps if s.get("fetched_at")]
        ver = f"main_force:{first}..{last}({len(snaps)}d)"
        if fetched:
            ver += f";fetched_max={max(fetched)}"
        db.research_experiment_set_data_snapshot(self.experiment_id, ver)

    @staticmethod
    def _weights_for(ablation: Any, base: Dict[str, float]) -> Dict[str, float]:
        """消融实验的聚合权重。

        两种写法，`weights`（直接给全量权重）优先于 `drop`（把某些维归零）：
          {"weights": {"inflow_consistency": 50, ...}}
          {"drop": ["chip_convergence"]}
        两者都没有 => 原样返回 `base`，即快照里那次真实打分用的权重。
        """
        if not isinstance(ablation, dict):
            return base
        ov = ablation.get("weights")
        if isinstance(ov, dict) and ov:
            return {k: float(v) for k, v in ov.items()}
        drop = ablation.get("drop")
        if drop:
            return drop_weights(base, drop)
        return base

    # ---------------------------------------------------------- 信号
    def _build_signals(self, snaps: Sequence[Dict[str, Any]],
                       exp: Dict[str, Any], config_hash: str,
                       horizons: Sequence[int]) -> List[Dict[str, Any]]:
        snap_cfg = exp.get("config_snapshot") or {}
        weights = snap_cfg.get("weights") or {}
        abl_cfg = snap_cfg.get("ablation") or {}
        model_version = exp.get("strategy_version") or "unversioned"
        out: List[Dict[str, Any]] = []

        for snap in snaps:
            if self._cancelled():
                break
            obj = snap["obj"]
            model = obj.get("model") or {}
            used = model.get("used_weights") or weights
            degraded = list(model.get("degraded") or [])
            abl_used = self._weights_for(abl_cfg, used)
            ablated = abl_used != used

            rows: List[Dict[str, Any]] = []
            for it in obj.get("all_rows") or []:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("code") or "").strip()
                if not code or it.get("score") is None:
                    continue
                rows.append({"code": code, "score": float(it["score"]),
                             "snapshot_score": float(it["score"]),
                             "dims": it.get("dims") or {},
                             "coverage": it.get("coverage")})

            ranked = reweight_rank(rows, abl_used) if ablated \
                else rank_signals(rows)
            for item in ranked:
                dims = item["dims"]
                missing: List[str] = []
                for key in used:
                    if key not in dims:
                        missing.append(f"{key}: 该股无此项数据")
                for key in degraded:
                    msg = f"{key}: 本轮整维降级（覆盖率不足）"
                    if msg not in missing:
                        missing.append(msg)

                # 与既有打分实现的一致性校验：重算值与入库分对不上就报警。
                # 消融实验跳过 —— 那时 item["score"] 是**按实验权重重算**的，
                # 与快照入库分本来就不同，拿它去对拍只会制造假警报；原始分
                # 照样入档在 provenance.snapshot_score 里，供事后逐条核对。
                if not ablated:
                    replay, cov = replay_score({"dims": dims}, used)
                    if (replay is not None
                            and abs(replay - item["score"]) > REPLAY_TOL):
                        self._warn(
                            f"{snap['date']} {item['code']} 综合分重算 "
                            f"{replay:.3f} vs 入库 {item['score']:.3f}，偏差超 "
                            f"{REPLAY_TOL}，请核对快照与打分实现是否已不同步")
                    stored_cov = item.get("coverage")
                    if (stored_cov is not None and cov
                            and abs(cov - float(stored_cov)) > 0.002):
                        self._warn(
                            f"{snap['date']} {item['code']} 覆盖率重算 {cov:.3f} "
                            f"vs 入库 {float(stored_cov):.3f}")

                # 逐维只存快照里**真实存在**的字段；raw 一律 null —— 快照
                # 不存因子原始指标值（查证过），不能凭空补一个数。
                factor_dims = {
                    k: {"label": d.get("label"), "short": d.get("short"),
                        "score": d.get("score"), "weight": d.get("weight"),
                        "raw": None}
                    for k, d in dims.items() if isinstance(d, dict)
                }
                rank = item["rank"]
                out.append({
                    "signal_date": snap["date"],
                    "code": item["code"],
                    "model_version": model_version,
                    "score": item["score"],
                    "rank": rank,
                    "top5": rank <= 5,
                    "top10": rank <= 10,
                    "top30": rank <= 30,
                    "data_quality": "PARTIAL" if missing else "READY",
                    "missing": missing or None,
                    "factors": {
                        "dims": factor_dims,
                        "raw_available": False,
                        "raw_note": "快照只存单维百分位与权重，不含因子原始指标值；"
                                    "原始值不可回溯，见 docs/backtest/limitations.md",
                        "provenance": {
                            "dataset": "main_force",
                            "trade_date": snap["date"],
                            "fetched_at": snap.get("fetched_at"),
                            "source": snap.get("source"),
                            "config_hash": config_hash,
                            "total_weight": model.get("total_weight"),
                            "degraded": degraded,
                            "sort_version": SORT_VERSION,
                            "horizons": list(horizons),
                            # 消融实验必须留两条对照：原始入库分 + 实验用的权重。
                            # 少了它们，事后没人能说清"这个排名是哪套权重重排出来的"。
                            "snapshot_score": item.get("snapshot_score"),
                            "ablation": (
                                {"weights": abl_used,
                                 "drop": list(abl_cfg.get("drop") or [])}
                                if ablated else None),
                        },
                    },
                })

        if not any(w.startswith("快照只存") or "原始指标" in w for w in self.warnings):
            self._warn("因子原始值不可回溯：main_force 快照只存单维百分位与权重，"
                       "research_signals 的 dims.raw 全为 null（如实标记，不编造）。")
        return out

    # ---------------------------------------------------------- 收益
    def _build_outcomes(self, signals: Sequence[Dict[str, Any]],
                        horizons: Sequence[int],
                        bars: Dict[str, Dict[str, Dict[str, Any]]],
                        ref_dates: Sequence[str],
                        cost: Optional[Dict[str, Any]],
                        bench: Optional[Dict[str, float]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for i, s in enumerate(signals):
            if i % 200 == 0 and self._cancelled():
                break
            for h in horizons:
                out.append(build_outcome(s["signal_date"], s["code"], int(h),
                                         bars, ref_dates, cost, bench))
        return out

    # ---------------------------------------------------------- 指标
    def _build_metrics(self, signals: Sequence[Dict[str, Any]],
                       outcomes: Sequence[Dict[str, Any]],
                       portfolios: Sequence[str],
                       horizons: Sequence[int]) -> List[Dict[str, Any]]:
        # 信号按 (date, code) 索引，供组合规则取成员
        idx: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for s in signals:
            idx.setdefault(s["signal_date"], {})[s["code"]] = s
        # 消融与否直接看信号里记的 provenance，比回读 config 更贴近事实：
        # 那是这次实际用的权重，不是"打算用"的权重。
        abl_active = any(
            (s.get("factors") or {}).get("provenance", {}).get("ablation")
            for s in signals)

        out_rows: Dict[Tuple[str, int], List[Dict[str, Any]]] = {}
        for o in outcomes:
            out_rows.setdefault((o["signal_date"], o["code"], int(o["horizon"])),
                                []).append(o)

        rows: List[Dict[str, Any]] = []
        for rule in portfolios:
            n = {"top5": 5, "top10": 10, "top30": 30}.get(rule)
            if n is None:
                continue
            for h in horizons:
                theory: List[Optional[float]] = []
                excess: List[Optional[float]] = []
                cost_ratio: List[float] = []
                pending = no_data = fillable = fills = done_count = 0

                for date, by_code in idx.items():
                    members = [c for c, s in by_code.items() if s["rank"] <= n]
                    for code in members:
                        # 键里已经含 horizon，取首条即可（唯一约束保证不会多）
                        lst = out_rows.get((date, code, h)) or []
                        o = lst[0] if lst else None
                        if o is None:
                            continue
                        st = o["status"]
                        if st == "PENDING":
                            pending += 1
                        elif st == "NO_DATA":
                            no_data += 1
                        else:
                            done_count += 1
                            theory.append(o["theory_return"])
                            excess.append(o["excess_return"])
                            # 「买得进」和「算得出可执行收益」是两件事：
                            # 前者只看成交判定，后者还要求有成本模型。
                            # 混在一起会让"没配成本"被记成"涨停买不进"。
                            if o.get("fill_status") == "FILLED":
                                fillable += 1
                            if o["exec_return"] is not None:
                                fills += 1
                                if (o["theory_return"] is not None
                                        and o["exec_return"] is not None):
                                    cost_ratio.append(
                                        o["theory_return"] - o["exec_return"])

                s_theory = summarize(theory, pending=pending, no_data=no_data)
                s_excess = summarize([x for x in excess if x is not None])

                notes: List[str] = [SURVIVORSHIP_NOTE]
                if abl_active:
                    notes.append(
                        "因子消融：本表排名由实验权重对**快照里的逐维百分位**"
                        "重排得到（逐维分无法重算，日线不落库）。因此只能回答"
                        "「换权重后排名/收益怎么变」，不能回答「该因子的边际"
                        "贡献是多少」。")
                if s_theory["count"] and s_theory["count"] < 30:
                    notes.append(
                        f"有效样本 {s_theory['count']} < 30，统计仅作描述性参考。")
                if pending:
                    notes.append(f"{pending} 个样本观察期未走完（PENDING），"
                                 "已从收益统计中剔除，不计为 0 收益。")
                if no_data:
                    notes.append(f"{no_data} 个样本缺行情（NO_DATA），不计为 0 收益。")
                if not s_excess["count"]:
                    notes.append("沪深300 覆盖不到这些信号日（或库里没有指数表），"
                                 "超额收益全为空（NULL），不是 0 —— "
                                 "无法给出相对基准的结论。")
                if fillable > fills:
                    notes.append(
                        f"{fillable - fills} 个样本两端确认可成交，但未配置"
                        "交易成本（或成本模型算不出），可执行收益留空 —— "
                        "这是「没配成本」，不是「买不进」。")
                if s_theory["count"]:
                    notes.append("重叠持有期的样本彼此相关，本表只给描述性统计，"
                                 "**不作显著性检验**，不得据此宣称策略有效。")
                notes.append("max_drawdown 为空：第一版未构建逐日组合净值曲线，"
                             "把各股 T+n 收益排开算回撤是错误口径，故不填。")

                rows.append({
                    "portfolio_rule": rule, "horizon": int(h),
                    "valid_signals": s_theory["count"],
                    "valid_fills": fills,
                    "avg_return": s_theory["avg"],
                    "median_return": s_theory["median"],
                    "win_rate": s_theory["win_rate"],
                    "avg_excess": s_excess["avg"],
                    "median_excess": s_excess["median"],
                    "p10": s_theory["p10"], "p25": s_theory["p25"],
                    "p75": s_theory["p75"], "p90": s_theory["p90"],
                    "max_drawdown": None,
                    "trading_cost": (sum(cost_ratio) / len(cost_ratio)
                                     if cost_ratio else None),
                    "unfilled_rate": ((done_count - fillable) / done_count
                                      if done_count else None),
                    "pending_count": pending, "no_data_count": no_data,
                    "quality_notes": notes,
                })
        return rows
