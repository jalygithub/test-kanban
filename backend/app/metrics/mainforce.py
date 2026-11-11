"""主力建仓候选打分模型。

流程与 docs/01-需求与技术选型方案.md §8 一致：

    全市场 ~5200
      │ 粗筛（ST / 停牌 / 涨跌停池 / 次新 / 流通市值区间 / 无流动性）
      ▼
    ~3000
      │ 廉价预选（只用批量接口就能算出的信号）
      ▼
    preselect_top(300)  ← 逐只拉日线，这一步是唯一的 N 次请求
      │ 负面过滤（涨停池 / 连板>=2 / 20日涨幅>=40%）
      ▼
    六维度百分位打分 -> 加权求和 -> Top N

关于「百分位归一化」：各维度原始量纲不同（元、百分比、变异系数、无量纲比例），
直接加权没有意义。因此每个维度先在**候选池内**转成 0-100 百分位，再按权重加权，
最终总分 = Σ(维度百分位 × 权重) / Σ(可用权重)，范围 0-100。

维度缺数据时（例如东财资金流接口不可用导致「大单净额占比」无数据），
该维度整体弃用并**按剩余权重重归一化**，结果里通过 `degraded` 字段如实标注。

免责声明：本模型仅为研究与可视化用途，输出不构成任何投资建议。
"""

from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import pandas as pd
import yaml

from app.config import BACKEND_DIR, settings
from app.fetchers import calendar as trade_calendar
from app.fetchers import listing as listing_api
from app.fetchers import quotes, ztpool
from app.pipeline import ingest as pipeline_ingest
from app.store import db

logger = logging.getLogger(__name__)

SCORER_PATH = BACKEND_DIR / "config" / "scorer.yaml"

# 维度定义：key -> (配置里的权重键, 展示名, 方向)
# 方向 higher_is_better 表示原始值越大得分越高
DIMENSIONS: List[Tuple[str, str, str, bool]] = [
    ("inflow_consistency", "主力资金持续流入", "流入持续性", True),
    ("volume_up_price_flat", "放量不涨", "放量不涨", True),
    ("large_order_ratio", "大单净额占比", "大单占比", True),
    ("price_position_low", "股价位置低", "低位", False),
    ("turnover_moderate", "换手适中", "换手适中", True),
    ("chip_convergence", "筹码收敛", "筹码收敛", True),
]

DEFAULT_CONFIG: Dict[str, Any] = {
    "weights": {
        "inflow_consistency": 30,
        "volume_up_price_flat": 20,
        "large_order_ratio": 15,
        "price_position_low": 15,
        "turnover_moderate": 10,
        "chip_convergence": 10,
    },
    "coarse_filter": {
        "exclude_st": True,
        "exclude_suspended": True,
        "exclude_limit_pool": True,
        "min_listed_days": 60,
        "float_mv_min": 1_500_000_000,
        "float_mv_max": 200_000_000_000,
        "min_amount": 5_000_000,
    },
    "negative_filter": {
        "exclude_zt_pool": True,
        "max_boards": 1,
        "max_gain_20d": 40.0,
    },
    "metrics": {
        # ---- §8.3 维度1 主力资金持续流入 ----
        "inflow_window_days": 5,     # 「近 5 日净流入/流通市值」与「>=3日为正」共用窗口
        "inflow_min_days": 3,        # 「>=3 日为正」的最低观测天数
        "inflow_lookback_days": 10,  # 预选阶段的连续性窗口（打分阶段不用）
        # ---- §8.3 维度2 放量但不涨 ----
        "volume_short_days": 5,
        "volume_base_days": 20,
        "volume_up_ratio": [1.5, 4.0],   # 近5日均量/20日均量甜区
        "price_gain_range": [0.0, 12.0], # 近5日涨幅甜区
        # ---- §8.3 维度4 股价位置低 ----
        "price_position_days": 250,
        # ---- §8.3 维度5 换手适中 ----
        "turnover_range": [3.0, 12.0],
        # ---- §8.3 维度6 筹码收敛 ----
        "chip_recent_days": 10,
        "chip_base_days": 20,
        "chip_threshold": 0.8,       # 近10日均振幅 / 前20日均振幅 < 0.8 即达标
    },
    # history_days 要覆盖 price_position_days=250 个交易日：
    # 一年约 250 个交易日 / 365 自然日，取 400 留出节假日与停牌余量
    "fetch": {"history_days": 400, "preselect_top": 300},
    "output": {"top_n": 30},
    # 维度百分位达到该值才写进「理由」，避免满屏都是弱信号
    "reason_min_pct": 60,
    "reason_max_items": 3,
}


# ============================================================ 配置


def load_config() -> Dict[str, Any]:
    """读取 scorer.yaml，缺失项回落到默认值。"""
    cfg: Dict[str, Any] = {}
    path = Path(settings.scorer_config) if settings.scorer_config else SCORER_PATH
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
        except Exception as exc:  # noqa: BLE001
            logger.error("打分配置读取失败，使用默认值: %s", exc)

    merged = dict(DEFAULT_CONFIG)
    for section, defaults in DEFAULT_CONFIG.items():
        incoming = cfg.get(section)
        if isinstance(defaults, dict):
            merged[section] = {**defaults, **(incoming or {})}
        elif incoming is not None:
            merged[section] = incoming
    # 兜底：yaml 里没有的新字段也要存在
    for key, value in DEFAULT_CONFIG.items():
        merged.setdefault(key, value)
    return merged


# ============================================================ 工具


def _pct(values, higher_is_better: bool = True) -> pd.Series:
    """候选池内百分位归一化到 0-100。缺失值保持 NaN，不参与排名。"""
    series = values if isinstance(values, pd.Series) else pd.Series(values)
    series = pd.to_numeric(series, errors="coerce").astype("float64")
    if series.notna().sum() == 0:
        return pd.Series(index=series.index, dtype="float64")
    rank = series.rank(method="average", na_option="keep", pct=True) * 100.0
    return rank if higher_is_better else 100.0 - rank


def _fmt_yuan(value: Optional[float]) -> str:
    """金额 -> 人类可读（用于生成理由文本）。"""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "-"
    value = float(value)
    sign = "-" if value < 0 else ""
    value = abs(value)
    if value >= 1e8:
        return f"{sign}{value / 1e8:.2f}亿"
    if value >= 1e4:
        return f"{sign}{value / 1e4:.0f}万"
    return f"{sign}{value:.0f}"


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return value
    return max(low, min(high, value))


def _num(value: Any) -> Optional[float]:
    """Series.get() 在键缺失时返回 NaN（不是 None），统一成 Optional。"""
    if value is None or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


# ============================================================ 粗筛


def _coarse_filter(
    snapshot: List[Dict],
    listing: Dict[str, Dict],
    zt_codes: Set[str],
    dt_codes: Set[str],
    cfg: Dict[str, Any],
    trade_date: str,
) -> Tuple[List[Dict], Dict[str, int]]:
    """全市场 -> 约 3000。返回 (候选列表, 各筛除原因的计数)。"""
    rules = cfg["coarse_filter"]
    reasons: Dict[str, int] = {}

    def drop(bucket: str) -> None:
        reasons[bucket] = reasons.get(bucket, 0) + 1

    # 上市交易日数：交易日历不可用时退回自然日（30 自然日 ≈ 20 交易日）
    listed_cache: Dict[str, Optional[int]] = {}

    def listed_days(code: str) -> Optional[int]:
        if code not in listed_cache:
            info = listing.get(code) or {}
            listed_cache[code] = listing_api.listed_trading_days(
                info.get("list_date", ""), trade_date)
        return listed_cache[code]

    out: List[Dict] = []
    seen: Set[str] = set()
    limit_pool = zt_codes | dt_codes

    for row in snapshot:
        code = str(row.get("code") or "").strip()
        name = str(row.get("name") or "")
        if not code:
            drop("no_code")
            continue
        if code in seen:          # 快照偶发重复行，会污染百分位索引
            drop("duplicate")
            continue
        seen.add(code)

        if rules.get("exclude_st") and (row.get("is_st") or "ST" in name.upper()
                                        or "退" in name):
            drop("st")
            continue

        price = row.get("price")
        amount = row.get("amount")
        if rules.get("exclude_suspended"):
            if price is None or price <= 0 or amount is None or amount <= 0:
                drop("suspended")
                continue
            if amount < rules.get("min_amount", 0):
                drop("illiquid")
                continue

        if rules.get("exclude_limit_pool") and code in limit_pool:
            drop("limit_pool")
            continue

        info = listing.get(code) or {}
        ld = listed_days(code)
        if ld is not None and ld < rules.get("min_listed_days", 0):
            drop("new_listing")
            continue
        if ld is None and info.get("list_date"):
            natural = listing_api.listed_natural_days(
                info["list_date"], trade_date)
            # 上市不足 min_listed_days*1.5 个自然日，视为次新
            if natural is not None and natural < rules.get("min_listed_days", 0) * 1.5:
                drop("new_listing")
                continue

        float_mv = row.get("float_mv")
        if float_mv is not None:
            if float_mv < rules.get("float_mv_min", 0):
                drop("mv_too_small")
                continue
            if float_mv > rules.get("float_mv_max", float("inf")):
                drop("mv_too_large")
                continue

        row = dict(row)
        row["industry"] = info.get("industry", "") or ""
        row["list_date"] = info.get("list_date", "")
        row["float_shares"] = info.get("float_shares")
        out.append(row)

    return out, reasons


# ============================================================ 资金流


def _load_inflow_history(codes: Sequence[str], limit: int) -> Dict[str, List[Dict]]:
    """读取已落库的逐日资金流记录（时间正序，每项含 net_inflow / large_order_net / amount）。

    数据随盘后任务逐日自我积累 —— §8.3 的维度 1（近 5 日净流入 / 流通市值，
    且 >=3 日为正）和维度 3（近 5 日大单占比均值）都要跨交易日的序列，
    单日快照算不出来。冷启动不足 `inflow_min_days` 天时该子信号为 NaN，
    模型按剩余子证据归一化并在结果里标注 degraded。
    """
    dates = sorted(db.list_dates("stock_net_inflow", limit))
    series: Dict[str, List[Dict]] = {c: [] for c in codes}
    wanted = set(codes)
    for day in dates:
        rows = db.load("stock_net_inflow", day) or []
        for item in rows:
            code = item.get("code")
            if code in wanted:
                series[code].append({
                    "net_inflow": item.get("net_inflow"),
                    "large_order_net": item.get("large_order_net"),
                    "amount": item.get("amount"),
                })
    return series


def inflow_window(end_date: str, days: int) -> List[str]:
    """`end_date` 及之前、库内 `stock_net_inflow` 已有的最近 `days` 个交易日（升序）。

    只看库内已有的日期，不推交易日历 —— 窗口长度不足 `days` 本身就说明
    该回补了，由 `inflow_gaps` 报给调用方。
    """
    dates = [d for d in db.list_dates("stock_net_inflow", 240) if d <= end_date]
    return sorted(dates)[-days:]


def inflow_gaps(codes: Sequence[str], end_date: str,
                days: int = 5) -> Tuple[List[str], List[str]]:
    """返回 (窗口, 逐日资金流还不够 `days` 天的代码)。纯 DB 读，无网络。"""
    window = inflow_window(end_date, days)
    by_date = {
        d: {r.get("code"): r for r in (db.load("stock_net_inflow", d) or [])}
        for d in window
    }

    def cover(code: str) -> int:
        return sum(
            1 for d in window
            if isinstance((by_date[d].get(code) or {}).get("net_inflow"),
                          (int, float))
        )

    seen: Set[str] = set()
    missing: List[str] = []
    for code in codes:
        if not code or code in seen:
            continue
        seen.add(code)
        if cover(code) < days:
            missing.append(code)
    return window, missing


# 一次回补尝试后（无论成败）多久不再重复尝试同一只股。
# 成功的股靠 inflow_gaps 的覆盖数天然排除，这里防的是**失败**的股
# 每次开页面都对着挂掉的东财空转一轮 budget。
_BACKFILL_COOLDOWN = 600.0
_BACKFILL_TRIED: Dict[str, float] = {}


def backfill_inflow(codes: Sequence[str], end_date: str, days: int = 5,
                    *, budget_sec: Optional[float] = None,
                    max_codes: Optional[int] = None) -> Dict[str, Any]:
    """把「近 `days` 日逐日主力净流入」从东财 daykline 补进 `stock_net_inflow`。

    为什么需要：快照是**每天盘后落一行**攒出来的，冷启动只有 1 天，而
    §8.3 维度 1 要「近 5 日」、页面也要展示逐日明细 —— 不回补就得干等 5 个交易日。
    补进同一张表而不是另开一张，是因为打分和展示读的是同一份数据，
    而且 daykline 带大单/超大单分解，顺带把维度 3 的 `large_order_net`
    缺口也填上（M4 那轮 `large_order_ratio` 就是这么 degraded 的）。

    写入是**按日期合并**而不是整体覆盖：20260924 那天的快照有 4725 行，
    整体覆盖会把它们冲掉，所以逐个日期 load → 按 code upsert → save。

    时间/条数双上限，供 HTTP 路径在后台线程里调用；脚本路径可以都不传。
    """
    stats: Dict[str, Any] = {"need": 0, "fetched": 0, "filled": 0,
                             "window": inflow_window(end_date, days)}
    if not end_date:
        return stats
    _, need = inflow_gaps(codes, end_date, days)
    need = [c for c in need if time.monotonic() - _BACKFILL_TRIED.get(c, 0.0)
            > _BACKFILL_COOLDOWN]
    stats["need"] = len(need)
    if not need:
        return stats

    patches: Dict[str, Dict[str, Dict[str, Any]]] = {}
    started = time.monotonic()
    for code in need:
        if max_codes is not None and stats["fetched"] >= max_codes:
            break
        if budget_sec is not None and time.monotonic() - started >= budget_sec:
            break
        _BACKFILL_TRIED[code] = time.monotonic()
        rows = quotes.stock_fflow_daykline(code, lmt=max(days * 4, 30))
        if not rows:
            continue
        stats["fetched"] += 1
        # daykline 是全历史，取 end_date 及之前最近 days 天；新股天然不足 days
        for row in [r for r in rows if r["date"] <= end_date][-days:]:
            patches.setdefault(row["date"], {})[code] = {
                "code": code,
                "net_inflow": row["net_inflow"],
                "large_order_net": row.get("large_order_net"),
                "net_pct": row.get("net_pct"),
            }

    for date, patch in patches.items():
        old = db.load("stock_net_inflow", date) or []
        existed = bool(old)
        by_code: Dict[Any, Dict[str, Any]] = {r.get("code"): r for r in old}
        for code, row in patch.items():
            prev = by_code.get(code) or {}
            # amount 是打分时从行情快照带进来的，daykline 不提供，必须留着
            if isinstance(prev.get("amount"), (int, float)):
                row = {**row, "amount": prev["amount"]}
            by_code[code] = row
            stats["filled"] += 1
        # 混合来源如实标注：已有快照里其余股票仍是原源（多为同花顺降级值）
        db.save("stock_net_inflow", date, list(by_code.values()),
                source="mixed" if existed else "eastmoney")

    stats["window"] = inflow_window(end_date, days)
    return stats


def _collect_inflow(
    candidates: List[Dict],
    trade_date: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """汇总当日与多窗口资金流。东财可用时用主力净额，否则用同花顺净额。"""
    codes = [c["code"] for c in candidates]
    code_set = set(codes)
    metrics_cfg = cfg["metrics"]
    amount_of = {c["code"]: c.get("amount") for c in candidates}

    windows: Dict[str, Dict[str, float]] = {}
    today_rows: List[Dict] = []
    em_ok = True
    for indicator in ("今日", "3日", "5日", "10日"):
        rows = quotes.fund_flow_rank(indicator)
        if not rows:
            em_ok = False
            continue
        if indicator == "今日":
            today_rows = rows
        windows[indicator] = {
            r["code"]: r["main_net"]
            for r in rows
            if r.get("code") in code_set and isinstance(r.get("main_net"), (int, float))
        }

    # 当日净额：优先东财「今日」主力净额，退化到同花顺净额（无大单分解）
    today: Dict[str, float] = {}
    order_net: Dict[str, float] = {}
    if today_rows:
        today = dict(windows.get("今日", {}))
        for r in today_rows:
            code = r.get("code")
            if code not in code_set:
                continue
            parts = [v for v in (r.get("super_net"), r.get("large_net"))
                     if isinstance(v, (int, float))]
            if parts:  # §8.3 维度3 用「超大单+大单」
                order_net[code] = float(sum(parts))
    else:
        logger.info("东财资金流不可用，改用同花顺净额（无大单分解）")
        try:
            fallback_rows = quotes.fund_flow_individual()
        except Exception as exc:  # noqa: BLE001
            # 备源也挂时不能让整步打分陪葬：today 留空 = 当日该维度无数据，
            # 各维度都按「无数据 -> 弃用并重归一化」处理，且 `if today` 不落库，
            # 不会凭空写入假数（同花顺备源在热点网络下会抓到坏页，见 quotes.py）。
            logger.warning("同花顺净额不可用，当日净额留空: %s: %s",
                           type(exc).__name__, str(exc)[:80])
        else:
            for row in fallback_rows:
                code = row.get("code")
                if code in code_set and isinstance(row.get("net_inflow"), (int, float)):
                    today[code] = float(row["net_inflow"])

    # 落库：后续交易日靠这个序列算「近5日」与「>=3日为正」
    if today:
        # 字段级合并而非整体覆盖：大单分解只有东财给得出，如果上一轮 EM 还可用、
        # 这一轮它熔断了，直接 save 会把已抓到的 large_order_net 覆盖成 None，
        # 维度 3 的 5 日均值就凭空少一天（本网络 EM 常年不可用，这个坑很容易踩）
        prev_rows = db.load("stock_net_inflow", trade_date) or []
        prev = {r.get("code"): r for r in prev_rows}
        payload: List[Dict[str, Any]] = []
        for code, value in today.items():
            old = prev.get(code) or {}
            amount = amount_of.get(code)
            payload.append({
                "code": code,
                "net_inflow": value,
                "large_order_net": order_net.get(code, old.get("large_order_net")),
                "amount": amount if isinstance(amount, (int, float))
                else old.get("amount"),
            })
        db.save(
            "stock_net_inflow", trade_date, payload,
            source="eastmoney" if em_ok else "10jqka",
        )

    history = _load_inflow_history(
        codes,
        max(int(metrics_cfg.get("inflow_lookback_days", 10)),
            int(metrics_cfg.get("inflow_min_days", 3)) + 1),
    )

    return {
        "today": today,
        "windows": windows,
        "history": history,
        "order_net": order_net,
        "amount": amount_of,
        "em_ok": em_ok,
    }


# ============================================================ 维度


def _dim_inflow(cands: List[Dict], inflow: Dict[str, Any],
                cfg: Dict[str, Any]) -> Tuple[pd.Series, Dict[str, str]]:
    """主力资金持续流入（30）。对应 §8.3 维度 1：
    **近 5 日主力净流入总额 / 流通市值，且 >= 3 日为正**（同一个 5 日窗口）。

    两个子证据各自先做百分位，再按「且」的语义取**乘积**：

    - 近 5 日净流入 / 流通市值 —— 除以流通市值才可比，否则大盘股凭绝对
      金额天然占优
    - 近 5 日中净流入为正的天数占比 —— 「>=3 日为正」是硬阈值，改成占比
      后在百分位下单调一致，但不会在「第 3 天」处断崖

    用乘积而非加权平均：加权平均会让「单日巨额流入、其余全流出」的股票
    凭一个子证据就接近满分，而那恰恰不是「持续」流入。任一子证据缺失时
    退化为剩下的那个，全部缺失返回 NaN（该维度弃用）。

    乘积会把两个均匀百分位压成均值约 25 的偏态分布，与其他维度不同量纲，
    所以池内再做一次百分位 —— 单调变换，不改变排序。
    """
    m = cfg["metrics"]
    min_days = int(m.get("inflow_min_days", 3))
    window = int(m.get("inflow_window_days", 5))

    base = pd.Index([c["code"] for c in cands], name="code")
    float_mv = {c["code"]: c.get("float_mv") for c in cands}
    today: Dict[str, float] = inflow.get("today") or {}
    history: Dict[str, List[Dict]] = inflow.get("history") or {}

    def _nets(code: str, days: int) -> List[float]:
        recs = (history.get(code) or [])[-days:]
        return [float(r["net_inflow"]) for r in recs
                if isinstance(r.get("net_inflow"), (int, float))]

    # (a) 近 5 日净流入 / 流通市值
    # (b) 近 5 日中净流入为正的天数占比
    flow_ratio: Dict[str, float] = {}
    consistency: Dict[str, float] = {}
    for code in base:
        mv = float_mv.get(code)
        mv = float(mv) if isinstance(mv, (int, float)) and mv > 0 else None

        recent = _nets(code, window)
        if recent:
            ratio_source = sum(recent)
        elif isinstance(today.get(code), (int, float)):
            ratio_source = float(today[code])
        else:
            ratio_source = None
        if ratio_source is not None and mv:
            flow_ratio[code] = ratio_source / mv

        if len(recent) >= min_days:
            consistency[code] = sum(1 for v in recent if v > 0) / len(recent)

    flow_pct = _pct(pd.Series(flow_ratio, dtype="float64"),
                    higher_is_better=True)
    cons_pct = _pct(pd.Series(consistency, dtype="float64"),
                    higher_is_better=True)

    combined: Dict[str, float] = {}
    for code in base:
        a = _num(flow_pct.get(code))
        b = _num(cons_pct.get(code))
        if a is None and b is None:
            continue
        if a is None:
            combined[code] = float(b)
        elif b is None:
            combined[code] = float(a)
        else:
            combined[code] = a * b / 100.0

    if not combined:
        return pd.Series(index=base, dtype="float64"), {}

    score = _pct(pd.Series(combined, dtype="float64"),
                 higher_is_better=True).reindex(base)

    # 理由 —— 文案格式对齐 docs/01 §8.4 给的示例：
    #   「近5日主力净流入占流通市值 4.2%（5日中4日为正）」
    reasons: Dict[str, str] = {}
    min_pct = float(cfg.get("reason_min_pct", 60))
    for code in base:
        value = score.get(code)
        if value is None or pd.isna(value) or value < min_pct:
            continue
        txt: List[str] = []
        mv = float_mv.get(code)
        recent = _nets(code, window)
        if recent and isinstance(mv, (int, float)) and mv > 0:
            total = sum(recent)
            piece = (f"近{len(recent)}日主力净流入{_fmt_yuan(total)}，"
                     f"占流通市值{total / mv * 100:.1f}%")
            if len(recent) >= min_days:
                piece += f"（{len(recent)}日中{sum(1 for v in recent if v > 0)}日为正）"
            txt.append(piece)
        if not txt:
            net = today.get(code)
            if net is not None:
                txt.append(f"当日净流入{_fmt_yuan(float(net))}")
        if txt:
            reasons[code] = "、".join(txt)
    return score, reasons


def _band_sweet(value: Optional[float], lo: float, hi: float,
                tol_lo: Optional[float] = None,
                tol_hi: Optional[float] = None) -> float:
    """落在甜区 [lo, hi] 得 1，向外按容差线性衰减到 0。

    用「区间 + 容差」而不是硬阈值：硬阈值在百分位排序下会让区间边缘的两只
    股票拿到 1 和 0 的断崖差，而它们其实只差一点。容差缺省取区间宽度。
    """
    if value is None or not math.isfinite(value):
        return 0.0
    if lo <= value <= hi:
        return 1.0
    if value < lo:
        tol = tol_lo if tol_lo else (hi - lo)
        return _clip(1.0 - (lo - value) / max(tol, 1e-9))
    tol = tol_hi if tol_hi else (hi - lo)
    return _clip(1.0 - (value - hi) / max(tol, 1e-9))


def _range2(mapping: Dict[str, Any], key: str,
            default_lo: float, default_hi: float) -> Tuple[float, float]:
    """读配置里的区间。兼容旧的单值写法（会补成 [lo, lo*2]）。"""
    raw = mapping.get(key, None)
    if isinstance(raw, (list, tuple)) and len(raw) == 2:
        return float(raw[0]), float(raw[1])
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw), default_hi
    return default_lo, default_hi


def _dim_volume_flat(cands: List[Dict], histories: Dict[str, pd.DataFrame],
                     cfg: Dict[str, Any]) -> Tuple[pd.Series, Dict[str, str]]:
    """放量但不涨（20）。对应 §8.3 维度 2：

    **近 5 日均量 / 20 日均量 ∈ [1.5, 4] 且 近 5 日涨幅 ∈ [0%, 12%]**

    文档给的是两个硬区间，这里都换成甜区函数（区间内 1 分，区间外线性衰减），
    两个子分数相乘 —— 「放量」和「不涨」必须同时成立，与文档的 AND 语义一致。

    放量用**成交量**而非成交额：成交额把价格上涨也混进了「放量」，
    量价要分开判断，否则一只只涨不放量的股票会被误判成放量。
    """
    m = cfg["metrics"]
    short = int(m.get("volume_short_days", 5))
    base = int(m.get("volume_base_days", 20))
    need = short + base + 1
    ratio_lo, ratio_hi = _range2(m, "volume_up_ratio", 1.5, 4.0)
    chg_lo, chg_hi = _range2(m, "price_gain_range", 0.0, 12.0)

    raw: Dict[str, float] = {}
    reasons: Dict[str, str] = {}
    for row in cands:
        hist = histories.get(row["code"])
        if hist is None or len(hist) < need:
            continue
        volume = pd.to_numeric(hist["volume"], errors="coerce")
        close = pd.to_numeric(hist["close"], errors="coerce")
        vol_short = volume.tail(short).mean()
        vol_base = volume.tail(short + base).head(base).mean()
        if not vol_short or not vol_base or vol_base <= 0:
            continue
        ratio = float(vol_short / vol_base)

        recent_close = close.iloc[-short - 1]
        if not recent_close or recent_close <= 0:
            continue
        chg = (close.iloc[-1] / recent_close - 1.0) * 100.0
        if not math.isfinite(chg):
            continue

        # 放量在甜区内最佳：低于下限是缩量，高于上限多是放量出货
        vol_score = _band_sweet(ratio, ratio_lo, ratio_hi,
                                tol_lo=ratio_lo - 1.0,
                                tol_hi=ratio_hi * 0.5)
        # 涨幅甜区：跌太多说明还在出货，涨太多说明已经拉起来了
        chg_score = _band_sweet(chg, chg_lo, chg_hi, tol_lo=12.0, tol_hi=12.0)
        raw[row["code"]] = vol_score * chg_score

        reasons[row["code"]] = (
            f"近{short}日均量为前{base}日的{ratio:.1f}倍"
            f"（甜区 {ratio_lo:g}~{ratio_hi:g}），"
            f"近{short}日涨幅{chg:+.1f}%")

    return _pct(pd.Series(raw), higher_is_better=True), reasons


def _dim_large_order(cands: List[Dict], inflow: Dict[str, Any],
                     cfg: Dict[str, Any]) -> Tuple[pd.Series, Dict[str, str]]:
    """大单净额占比（15）。对应 §8.3 维度 3：

    **近 5 日 (超大单 + 大单净流入) / 成交额 均值**

    逐日的大单净额只有东财排行提供，所以当天的值随快照落库，
    之后每个交易日累加，用已积累的序列取近 5 日均值 —— 单日快照算不出均值。

    冷启动只有当天，均值退化为当日值；东财整个不可用则全 NaN，
    由上层弃用该维度并按剩余权重重归一化。
    """
    window = int(cfg["metrics"].get("inflow_window_days", 5))
    history: Dict[str, List[Dict]] = inflow.get("history") or {}
    order_net: Dict[str, float] = inflow.get("order_net") or {}
    amount: Dict[str, float] = inflow.get("amount") or {}

    raw: Dict[str, float] = {}
    reasons: Dict[str, str] = {}
    for cand in cands:
        code = cand["code"]
        ratios: List[float] = []
        last_net: Optional[float] = None

        for rec in (history.get(code) or [])[-window:]:
            net = rec.get("large_order_net")
            amt = rec.get("amount")
            if isinstance(net, (int, float)) and isinstance(amt, (int, float)) and amt > 0:
                ratios.append(net / amt)
                last_net = float(net)

        if not ratios:  # 当日值兜底（history 尚未含当天记录时）
            net = order_net.get(code)
            amt = amount.get(code)
            if isinstance(net, (int, float)) and isinstance(amt, (int, float)) and amt > 0:
                ratios.append(net / amt)
                last_net = float(net)

        if not ratios:
            continue
        mean_ratio = sum(ratios) / len(ratios)
        if not math.isfinite(mean_ratio):
            continue
        raw[code] = mean_ratio
        if last_net is not None:
            reasons[code] = (
                f"近{len(ratios)}日大单净额占比均值{mean_ratio * 100:.1f}%"
                f"（当日{_fmt_yuan(last_net)}）")

    return _pct(pd.Series(raw), higher_is_better=True), reasons




def _dim_price_position(cands: List[Dict], histories: Dict[str, pd.DataFrame],
                        cfg: Dict[str, Any]) -> Tuple[pd.Series, Dict[str, str]]:
    """股价位置低（15）。对应 §8.3 维度 4：

    **收盘价在近 250 日最高-最低区间的分位数**，越低越像还在底部吸筹。

    用区间上下沿而非均值：区间会自动把除权、长期单边下跌都算进去。
    上市不足 250 个交易日的用实际行数（粗筛已剔除次新股，影响有限）。

    价格取**日线最后一根收盘价**而非快照的最新价：high/low 来自 qfq 日线，
    而快照是不复权的，两者只有在「最新一根未复权」时才恰好相等。用同一根
    K 线的收盘价即可彻底避开复权口径不一致 —— 一旦不一致，现价会直接落到
    区间之外，分位数要么远超 100% 要么是负数。
    """
    days = int(cfg["metrics"].get("price_position_days", 250))
    raw: Dict[str, float] = {}
    reasons: Dict[str, str] = {}
    for row in cands:
        hist = histories.get(row["code"])
        if hist is None or len(hist) < 5:
            continue
        tail = hist.tail(days)
        low = pd.to_numeric(tail["low"], errors="coerce").min()
        high = pd.to_numeric(tail["high"], errors="coerce").max()
        close = pd.to_numeric(hist["close"], errors="coerce")
        price = _num(float(close.iloc[-1])) if len(close) else None
        if price is None:
            price = _num(row.get("price"))
        if not price or not low or not high or high <= low:
            continue
        pos = (price - low) / (high - low)
        if not math.isfinite(pos):
            continue
        raw[row["code"]] = pos
        reasons[row["code"]] = (
            f"现价处于近{len(tail)}日价格区间的{pos * 100:.0f}%分位"
            f"（区间 {low:.2f}~{high:.2f}）")

    # 位置越低得分越高 -> 反向
    return _pct(pd.Series(raw), higher_is_better=False), reasons


def _turnover_closeness(value: float, low: float, high: float) -> float:
    """换手落在理想区间时得分高，区间中心最高，区间外线性衰减到 0。"""
    if value is None or value <= 0 or not math.isfinite(value):
        return 0.0
    if value < low:
        return _clip(1.0 - (low - value) / max(low, 1e-9))
    if value > high:
        return _clip(1.0 - (value - high) / max(high, 1e-9))
    center = math.sqrt(low * high)          # 几何中心，区间是乘性尺度
    span = max(abs(math.log(low / center)), abs(math.log(high / center)), 1e-9)
    return _clip(1.0 - abs(math.log(value / center)) / span)


def _asof(hist: Optional[pd.DataFrame], trade_date: str, field: str) -> Optional[float]:
    """取日线里**恰好等于 trade_date** 那一行的字段值，没有则返回 None。

    必须严格按日期匹配，不能图省事取 `iloc[-1]`：历史表被 `_fetch_histories`
    用 `end=trade_date` 截断，若那天该股停牌或缺行，末行会落在更早的日期 ——
    直接取末行等于拿前几天的换手冒充当天，比退回快照更糟。

    两者基准不同：日线是 as-of 的，而快照是 `market_snapshot()` 抓的**当前**行情。
    正常跑当天两者一致；补跑历史日时快照却是今天的（docs/04 §4 挂起项二）。
    """
    if hist is None or hist.empty:
        return None
    if "date" not in hist.columns or field not in hist.columns:
        return None
    hit = hist.loc[hist["date"] == trade_date, field]
    if hit.empty:
        return None
    v = pd.to_numeric(hit.iloc[0], errors="coerce")
    if pd.isna(v):
        return None
    return float(v)


def _dim_turnover(cands: List[Dict], histories: Dict[str, pd.DataFrame],
                  cfg: Dict[str, Any], trade_date: str) -> Tuple[pd.Series, Dict[str, str]]:
    """换手适中（10）。对应 §8.3 维度 5：**当日换手率 ∈ [3%, 12%]**。

    有换手才有筹码交换，但换手过高说明是在炒作而非吸筹。

    打分阶段**优先取日线里 trade_date 那一行的换手**，取不到才退回快照字段 ——
    快照是当前行情，补跑历史日会把今天的换手算进那一天。注意预选 `_preselect`
    仍在逐只日线取回**之前**跑，那里只能用快照，所以补跑时「选出哪 300 只」
    仍受当日换手影响（已记在 docs/04 §4 挂起项二）。
    打分仍保留中心峰值而非「区间内一律满分」：百分位归一化下，
    区间内全为 1 会让这个维度对所有入池股票变成常数、完全失去区分度。
    """
    m = cfg["metrics"]
    low, high = _range2(m, "turnover_range", 3.0, 12.0)

    raw: Dict[str, float] = {}
    reasons: Dict[str, str] = {}
    for row in cands:
        value = _asof(histories.get(row["code"]), trade_date, "turnover")
        if value is None:
            value = row.get("turnover")
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            continue
        value = float(value)
        raw[row["code"]] = _turnover_closeness(value, low, high)
        if low <= value <= high:
            reasons[row["code"]] = f"当日换手{value:.1f}%，处于理想区间 {low:g}~{high:g}%"
        elif value < low:
            reasons[row["code"]] = f"当日换手{value:.1f}%，关注度偏低，筹码交换不足"
        else:
            reasons[row["code"]] = f"当日换手{value:.1f}%，抛压偏重，疑似在炒"

    return _pct(pd.Series(raw), higher_is_better=True), reasons


def _dim_chip_convergence(cands: List[Dict], histories: Dict[str, pd.DataFrame],
                          cfg: Dict[str, Any]) -> Tuple[pd.Series, Dict[str, str]]:
    """筹码收敛（10）。对应 §8.3 维度 6：

    **近 10 日振幅均值 < 前 20 日振幅均值 × 0.8**（波动收窄 = 吸筹压价）

    原始值取比值 `近10日均振幅 / 前20日均振幅`，越小越收敛，
    因此百分位反向计算 —— 0.8 就是文档给的及格线，理由文案直接标出是否达标。
    """
    m = cfg["metrics"]
    recent_days = int(m.get("chip_recent_days", 10))
    base_days = int(m.get("chip_base_days", 20))
    need = recent_days + base_days
    threshold = float(m.get("chip_threshold", 0.8))

    raw: Dict[str, float] = {}
    reasons: Dict[str, str] = {}
    for row in cands:
        hist = histories.get(row["code"])
        if hist is None or len(hist) < need:
            continue
        high = pd.to_numeric(hist["high"], errors="coerce")
        low = pd.to_numeric(hist["low"], errors="coerce")
        close = pd.to_numeric(hist["close"], errors="coerce")
        # 振幅 = (最高-最低) / 昨收，用昨收而非今收才能对齐 K 线口径
        amplitude = (high - low) / close.shift(1) * 100.0

        recent = amplitude.tail(recent_days).dropna()
        base = amplitude.head(len(amplitude) - recent_days).tail(base_days).dropna()
        if recent.empty or base.empty:
            continue
        mean_recent = float(recent.mean())
        mean_base = float(base.mean())
        if not mean_base or mean_base <= 0 or not math.isfinite(mean_recent):
            continue

        ratio = mean_recent / mean_base
        raw[row["code"]] = ratio
        verdict = "波动收窄，符合吸筹形态" if ratio < threshold else "波动未收窄"
        reasons[row["code"]] = (
            f"近{recent_days}日均振幅{mean_recent:.2f}% / "
            f"前{base_days}日{mean_base:.2f}% = {ratio:.2f}"
            f"（及格线 {threshold:g}），{verdict}")

    # 比值越小越收敛 -> 反向
    return _pct(pd.Series(raw), higher_is_better=False), reasons


# ============================================================ 打分


def _score(
    cands: List[Dict],
    histories: Dict[str, pd.DataFrame],
    inflow: Dict[str, Any],
    cfg: Dict[str, Any],
    trade_date: str,
) -> Tuple[List[Dict], Dict[str, Any]]:
    """六维度打分。返回 (行列表, 元信息)。"""
    if not cands:
        return [], {"degraded": [], "used_weights": {}}

    codes = [c["code"] for c in cands]
    index = pd.Index(codes, name="code")

    builders = {
        "inflow_consistency": lambda: _dim_inflow(cands, inflow, cfg),
        "volume_up_price_flat": lambda: _dim_volume_flat(cands, histories, cfg),
        "large_order_ratio": lambda: _dim_large_order(cands, inflow, cfg),
        "price_position_low": lambda: _dim_price_position(cands, histories, cfg),
        "turnover_moderate": lambda: _dim_turnover(cands, histories, cfg, trade_date),
        "chip_convergence": lambda: _dim_chip_convergence(cands, histories, cfg),
    }

    scores: Dict[str, pd.Series] = {}
    reasons: Dict[str, Dict[str, str]] = {}
    degraded: List[str] = []
    used_weights: Dict[str, float] = {}

    for key, label, _, _ in DIMENSIONS:
        try:
            series, reason = builders[key]()
        except Exception as exc:  # noqa: BLE001  单维度失败不能拖垮整轮
            logger.exception("维度 %s 计算失败", key)
            series, reason = pd.Series(dtype="float64"), {}
            degraded.append(key)

        series = series.reindex(index) if series is not None else \
            pd.Series(index=index, dtype="float64")

        coverage = float(series.notna().mean()) if len(series) else 0.0
        if coverage < 0.10:
            # 覆盖率过低说明该维度数据缺失（多为东财接口不可用）
            degraded.append(key)
            logger.warning("维度 %s 覆盖率 %.0f%%，弃用并重归一化权重",
                           label, coverage * 100)
            continue

        scores[key] = series
        reasons[key] = reason
        used_weights[key] = float(cfg["weights"].get(key, 0))

    total_weight = sum(used_weights.values())
    if total_weight <= 0:
        return [], {"degraded": degraded, "used_weights": {}}

    weighted = pd.Series(0.0, index=index)
    coverage = pd.Series(0.0, index=index)
    for key, weight in used_weights.items():
        series = scores[key].astype("float64")
        valid = series.notna()
        weighted = weighted + series.fillna(0.0) * (weight / total_weight)
        coverage = coverage + valid.astype(float) * (weight / total_weight)

    final = weighted.where(coverage > 0)

    min_pct = float(cfg.get("reason_min_pct", 60))
    max_items = int(cfg.get("reason_max_items", 3))

    rows: List[Dict] = []
    for i, cand in enumerate(cands):
        code = cand["code"]
        value = final.get(code)
        if value is None or pd.isna(value):
            continue

        dim_detail: Dict[str, Dict[str, Any]] = {}
        for key, label, short, _ in DIMENSIONS:
            if key not in scores:
                continue
            dim_value = scores[key].get(code)
            if dim_value is None or pd.isna(dim_value):
                continue
            dim_detail[key] = {
                "label": label,
                "short": short,
                "score": round(float(dim_value), 1),
                "weight": used_weights.get(key),
            }

        # 理由：按「维度百分位 × 权重」排序，取前 N 条达到阈值的
        ranked = sorted(
            (
                (dim["score"] * float(dim["weight"] or 0), key)
                for key, dim in dim_detail.items()
                if dim["score"] >= min_pct and (reasons.get(key) or {}).get(code)
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        items = [reasons[key][code] for _, key in ranked[:max_items]]

        # 行情字段优先取该日日线：补跑历史日时快照是今天的，
        # 直接用 cand 会在 0929 那一行显示今天的价 / 涨跌幅 / 换手 / 成交额。
        hist = histories.get(code)
        asof_price = _asof(hist, trade_date, "close")
        asof_pct = _asof(hist, trade_date, "pct_chg")
        asof_turn = _asof(hist, trade_date, "turnover")
        asof_amt = _asof(hist, trade_date, "amount")

        rows.append({
            "code": code,
            "name": cand.get("name", ""),
            "industry": cand.get("industry", ""),
            "price": asof_price if asof_price is not None else cand.get("price"),
            "pct_chg": asof_pct if asof_pct is not None else cand.get("pct_chg"),
            "turnover": asof_turn if asof_turn is not None else cand.get("turnover"),
            # 流通市值日线里没有，仍取自当前快照 —— 已记在 docs/04 §4 挂起项二
            "float_mv": cand.get("float_mv"),
            "amount": asof_amt if asof_amt is not None else cand.get("amount"),
            "list_date": cand.get("list_date", ""),
            "score": round(float(value), 2),
            "coverage": round(float(coverage.get(code) or 0), 3),
            "dims": dim_detail,
            "reasons": items,
        })

    rows.sort(key=lambda r: r["score"], reverse=True)
    meta = {
        "degraded": sorted(set(degraded)),
        "used_weights": used_weights,
        "total_weight": total_weight,
        "coverage_mean": round(float(coverage.mean()), 3),
    }
    return rows, meta


# ============================================================ 预选


def _preselect(cands: List[Dict], inflow: Dict[str, Any],
               cfg: Dict[str, Any]) -> List[Dict]:
    """用**不需要逐只请求**的信号排序，挑出拉日线的候选。

    拉日线是 N 次请求（300 只约 4 分钟），必须先在 ~3000 只里收敛。
    只用当日净额 / 连续性 / 换手贴合度 / 成交额，这四个信号在粗筛阶段就已具备。
    """
    limit = int(cfg["fetch"].get("preselect_top", 300))
    if len(cands) <= limit:
        return list(cands)

    m = cfg["metrics"]
    low, high = _range2(m, "turnover_range", 3.0, 12.0)
    min_days = int(m.get("inflow_min_days", 3))
    lookback = int(m.get("inflow_lookback_days", 10))

    index = pd.Index([c["code"] for c in cands], name="code")
    today = pd.Series({k: v for k, v in (inflow.get("today") or {}).items()},
                      dtype="float64")
    amount = pd.Series({c["code"]: c.get("amount") for c in cands},
                       dtype="float64")
    turnover = pd.Series(
        {c["code"]: _turnover_closeness(c.get("turnover") or 0, low, high)
         for c in cands}, dtype="float64")

    # 连续性：history 每项是 {net_inflow, ...} 记录
    consistency: Dict[str, float] = {}
    for code in index:
        records = (inflow.get("history") or {}).get(code) or []
        nets = [r.get("net_inflow") for r in records[-lookback:]
                if isinstance(r.get("net_inflow"), (int, float))]
        if len(nets) >= min_days:
            consistency[code] = sum(1 for v in nets if v > 0) / len(nets)

    parts = [
        (_pct(today.reindex(index), True), 0.45),
        (_pct(pd.Series(consistency, dtype="float64").reindex(index), True), 0.25),
        (_pct(turnover.reindex(index), True), 0.15),
        (_pct(amount.reindex(index), True), 0.15),
    ]

    acc = pd.Series(0.0, index=index)
    wsum = pd.Series(0.0, index=index)
    for series, weight in parts:
        valid = series.notna()
        acc = acc + series.fillna(0.0) * weight
        wsum = wsum + valid.astype(float) * weight
    rank_score = acc / wsum.replace(0, pd.NA)
    rank_score = rank_score.fillna(-1.0)

    chosen = rank_score.sort_values(ascending=False).head(limit).index
    chosen_set = set(chosen)
    return [c for c in cands if c["code"] in chosen_set]


# ====================================================== 打分进度

# 打分是整条盘后流水线里唯一动辄十几分钟的一步（模块开头写的「唯一的 N 次请求」
# 就是下面的逐只拉日线）。页面上的进度条若只按「步数」算，会一口气冲到 6/7 = 86%
# 然后卡住十几分钟不动 —— 那比没有进度条更让人以为是卡死了。
# 这里记两件事：跑到第几个阶段、逐只日线拉到第几只。
_prog_lock = threading.Lock()
_STAGE_TOTAL = 7  # run() 里 _stage(...) 的调用个数，加阶段要同步改
_PROG: Dict[str, Any] = {"stage": "", "stage_no": 0, "stages": 0,
                         "done": 0, "total": 0}


def progress() -> Dict[str, Any]:
    """打分内部进度的**拷贝**。没在打分时读到的是上一次的残留值 ——
    调用方（scheduler.state）只在「当前步骤确实是 main_force」时才取它，
    任务一结束就不再看，所以残留值不会漏到页面上。"""
    with _prog_lock:
        return dict(_PROG)


def _stage(no: int, name: str) -> None:
    """进入第 `no` 个阶段（阶段内计数归零）。纯记账，不碰打分数据。"""
    with _prog_lock:
        _PROG.update(stage=name, stage_no=no, stages=_STAGE_TOTAL,
                     done=0, total=0)


def _count(done: int, total: int) -> None:
    """阶段内进度（逐只日线：拉到第几只 / 共几只）。"""
    with _prog_lock:
        _PROG.update(done=done, total=total)


# ============================================================ 日线


def _fetch_histories(cands: List[Dict], trade_date: str,
                     cfg: Dict[str, Any]) -> Tuple[Dict[str, pd.DataFrame], int]:
    """逐只拉取日线。东财熔断后自动改走腾讯。返回 (表字典, 成功数)。"""
    days = int(cfg["fetch"].get("history_days", 400))
    end = datetime.strptime(trade_date, "%Y%m%d")
    start = end - timedelta(days=days)

    result: Dict[str, pd.DataFrame] = {}
    failed: List[str] = []
    miss_streak = 0
    for i, cand in enumerate(cands, 1):
        # 每只都刷一次：300 次拿锁可忽略，换来的是这一步（唯一 N 次请求的一步）
        # 在进度条上真的在动，而不是停在那儿二十分钟
        _count(i, len(cands))
        hist = quotes.daily_history(
            cand["code"], start.strftime("%Y%m%d"), end.strftime("%Y%m%d"))
        if hist is None or hist.empty:
            failed.append(cand["code"])
            # 连续落空说明数据源整体不可用，继续逐只试只会把任务拖到几小时
            miss_streak += 1
            if miss_streak >= 20:
                logger.error("连续 %d 只日线落空，提前终止本轮取数", miss_streak)
                break
            continue
        result[cand["code"]] = hist
        miss_streak = 0
        if i % 50 == 0:
            logger.info("日线进度 %d/%d", i, len(cands))
    if failed:
        logger.warning("日线获取失败 %d 只: %s", len(failed), failed[:10])
    return result, len(result)


# ============================================================ 负面过滤


def _negative_filter(cands: List[Dict], histories: Dict[str, pd.DataFrame],
                     zt_codes: Set[str], boards: Dict[str, int],
                     cfg: Dict[str, Any]) -> Tuple[List[Dict], Dict[str, int]]:
    rules = cfg["negative_filter"]
    reasons: Dict[str, int] = {}

    def drop(bucket: str) -> None:
        reasons[bucket] = reasons.get(bucket, 0) + 1

    max_gain = float(rules.get("max_gain_20d", 40.0))
    max_boards = int(rules.get("max_boards", 1))

    out: List[Dict] = []
    for row in cands:
        code = row["code"]
        if rules.get("exclude_zt_pool") and code in zt_codes:
            drop("zt_pool")
            continue
        if boards.get(code, 0) > max_boards:
            drop("multi_board")
            continue

        hist = histories.get(code)
        gain = None
        if hist is not None and len(hist) >= 21:
            close = pd.to_numeric(hist["close"], errors="coerce")
            base = close.iloc[-21]
            last = close.iloc[-1]
            if base and base > 0:
                gain = (last / base - 1.0) * 100.0
        if gain is not None and math.isfinite(gain) and gain >= max_gain:
            drop("gained_too_much")
            continue

        row = dict(row)
        row["gain_20d"] = round(gain, 2) if gain is not None else None
        out.append(row)
    return out, reasons


# ============================================================ 编排


def pool_unavailable_reason(trade_date: str) -> Optional[str]:
    """这一天的涨/跌停池拿不拿得到：可得回 `None`，否则回一句给人看的原因。

    两条路都算可得：① 还在东财 30 天窗口内（起跑时联网现取，见
    `ztpool.window_start`）；② 已过窗口、但盘后任务当天把池子采进了本地库
    （`pool_zt` / `pool_dt` 快照）。两者都没有 = 永久不可补 —— 池子是收盘
    那一刻当场采的，事后公开接口取不回（`backfill.py` 的 REFUSED 写的就是
    这条）。补跑按钮点下去 0 秒就该知道结果，不是烧完 4 分钟取数再死。
    """
    if trade_date >= ztpool.window_start():
        return None
    zt = db.load("pool_zt", trade_date) or []
    dt = db.load("pool_dt", trade_date) or []
    if zt and dt:
        return None
    lack = "/".join(name for name, rows in (("pool_zt", zt), ("pool_dt", dt))
                    if not rows)
    return ("东财涨/跌停池只保留最近 30 个自然日（今天最早可补 %s）：%s 已过期"
            "且本地没有当日 %s 快照 —— 池子事后取不回，该日永久不可补"
            % (ztpool.window_start(), trade_date, lack))


def _load_pools(trade_date: str) -> Tuple[List[Dict], List[Dict]]:
    """取某日涨停 / 跌停池：窗口内联网现取，过窗口只认本地快照。

    过窗口的日期 `pool_unavailable_reason` 已保证两份快照都在 —— 这里不再
    判缺，缺了就是它漏判，让它当场炸。
    """
    if trade_date >= ztpool.window_start():
        return ztpool.get_zt_pool(trade_date), ztpool.get_dt_pool(trade_date)
    zt = db.load("pool_zt", trade_date) or []
    dt = db.load("pool_dt", trade_date) or []
    logger.info("过窗口的 %s 池子改读本地快照 zt=%d dt=%d",
                trade_date, len(zt), len(dt))
    return zt, dt


def run(date: Optional[str] = None, refresh: bool = False) -> Dict[str, Any]:
    """跑一轮打分。默认命中缓存直接返回，`refresh=True` 强制重算。"""
    cfg = load_config()
    trade_date = date or trade_calendar.last_trade_date()

    if not refresh:
        cached = db.load_one("main_force", trade_date)
        if cached:
            logger.info("命中缓存 main_force @ %s", trade_date)
            return cached

    quotes.reset_circuit()
    started = datetime.now()

    # 1) 涨停 / 跌停池 —— 排在全市场快照**之前**：池子拿不到（过窗口、源
    #    回空）必须几秒内失败，不该让 150s 的快照先白烧（2026-11-02 补跑
    #    20260929 就是烧完 4 分钟才死在跌停池的 30 天闸门上）。
    _stage(1, "涨停/跌停池")
    reason = pool_unavailable_reason(trade_date)
    if reason:
        raise RuntimeError(reason)
    zt_rows, dt_rows = _load_pools(trade_date)
    if not zt_rows:
        # A 股当天必有涨停：窗口内回空是源异常，判失败交给重试，
        # 不许静默按空池出榜（「0 行不许判 SUCCESS」同款闸门）。
        raise RuntimeError("涨停池 %s 源回空：当日必有涨停，判源异常不判空榜"
                           % trade_date)
    zt_codes = {r.get("code") for r in zt_rows if r.get("code")}
    dt_codes = {r.get("code") for r in dt_rows if r.get("code")}
    boards = {r.get("code"): int(r.get("boards") or 1)
              for r in zt_rows if r.get("code")}

    # 2) 全市场快照
    _stage(2, "全市场快照")
    snapshot = quotes.market_snapshot()
    if not snapshot:
        raise RuntimeError("全市场快照为空，无法打分")
    listing = listing_api.get_listing_info()

    # 3) 粗筛
    _stage(3, "粗筛")
    coarse, coarse_stats = _coarse_filter(
        snapshot, listing, zt_codes, dt_codes, cfg, trade_date)
    logger.info("粗筛 %d -> %d，筛除明细 %s",
                len(snapshot), len(coarse), coarse_stats)
    if not coarse:
        raise RuntimeError("粗筛后无候选")

    # 4) 资金流（含落库累积）
    _stage(4, "资金流")
    inflow = _collect_inflow(coarse, trade_date, cfg)

    # 5) 预选 -> 逐只日线（唯一的 N 次请求，十几分钟基本耗在这里）
    _stage(5, "预选与日线")
    preselected = _preselect(coarse, inflow, cfg)
    logger.info("预选 %d 只拉取日线", len(preselected))
    histories, hist_ok = _fetch_histories(preselected, trade_date, cfg)

    # 6) 负面过滤
    _stage(6, "负面过滤")
    finalists, negative_stats = _negative_filter(
        preselected, histories, zt_codes, boards, cfg)
    # 日线没拉到的无法算「放量不涨/位置/筹码」，直接剔除避免半残分数
    finalists = [r for r in finalists if r["code"] in histories]
    logger.info("负面过滤后 %d 只（日线成功 %d）",
                len(finalists), hist_ok)

    # 7) 打分
    _stage(7, "六维度打分")
    rows, meta = _score(finalists, histories, inflow, cfg, trade_date)

    # 数据体检（任务书第 8 条，宽松方案）：当天输入域有没有缺 —— 缺了就
    # **整榜标记**，不拦输出。字段**加进 `model`**，既有键一个不动
    # （`replay_score` 与历史快照重放不受影响）；老快照没有这个字段，
    # 前端按「没查过」处理。
    meta = dict(meta or {})
    meta["data_quality"] = pipeline_ingest.rank_quality(trade_date)

    top_n = int(cfg["output"].get("top_n", 30))
    top_rows = rows[:top_n]
    for rank, row in enumerate(top_rows, 1):
        row["rank"] = rank

    result: Dict[str, Any] = {
        "date": trade_date,
        "generated_at": started.isoformat(timespec="seconds"),
        "elapsed_sec": round((datetime.now() - started).total_seconds(), 1),
        "params": {
            "weights": cfg["weights"],
            "top_n": top_n,
            "preselect_top": int(cfg["fetch"].get("preselect_top", 300)),
            "min_listed_days": int(cfg["coarse_filter"]["min_listed_days"]),
            "float_mv_min": cfg["coarse_filter"]["float_mv_min"],
            "float_mv_max": cfg["coarse_filter"]["float_mv_max"],
            "max_boards": int(cfg["negative_filter"]["max_boards"]),
            "max_gain_20d": float(cfg["negative_filter"]["max_gain_20d"]),
            "turnover_range": list(_range2(cfg["metrics"], "turnover_range",
                                           3.0, 12.0)),
            "inflow_window_days": int(cfg["metrics"].get("inflow_window_days", 5)),
            "price_position_days": int(cfg["metrics"].get("price_position_days", 250)),
            "chip_threshold": float(cfg["metrics"].get("chip_threshold", 0.8)),
        },
        "stats": {
            "universe": len(snapshot),
            "coarse_kept": len(coarse),
            "coarse_dropped": coarse_stats,
            "preselected": len(preselected),
            "histories": hist_ok,
            "negative_dropped": negative_stats,
            "scored": len(rows),
            "output": len(top_rows),
        },
        "model": meta,
        "rows": top_rows,
        "all_rows": rows,
        "disclaimer": "本结果仅用于研究与可视化，不构成任何投资建议。",
    }

    db.save_one("main_force", trade_date, result, source="scorer")
    logger.info("打分完成 %s -> Top%d，耗时 %.1fs，降级维度 %s",
                trade_date, len(top_rows), result["elapsed_sec"],
                meta.get("degraded") or "无")
    return result


def available_dates() -> List[str]:
    """已打过分的历史交易日（供前端回看）。"""
    return db.list_dates("main_force", limit=60)
