"""T+1 误差复盘 —— 把「盘中估出来的涨跌幅」和「事后公布的真实涨跌幅」对起来。

## 它回答什么问题

盘中估值是个估算，那到底准不准？这个模块用**已经发生过的数据**回答，不做任何
预测。产出是每个基金 × 每条链的四个数：样本数、平均误差、平均绝对误差、最坏
高估与最坏低估。

    误差 = 估算涨跌幅 − 实际涨跌幅        正 = 高估，负 = 低估

按**来源分组**是必须的：官方估值（覆盖完整、口径黑箱）与持仓穿透（只覆盖前十大
A 股、系统性偏低）的误差分布本来就不一样，混在一起算出来的平均值谁都不代表。

## 为什么叫 T+1

估值源每份响应的 `NAV` 是「最新一次公布的单位净值」，它只带**一个**净值日期
（`PDATE`）。算某天的实际涨跌幅需要**前后两个**净值，而第二天那个要等到当天
收盘后才公布。所以：

    D 日点查询   → 记下 D 日的估算（`fund_estimate_log`）
    D+1 日再点  → 接口带回 D 日净值，样本闭合

也就是说，**复盘的样本只来自你点过「查询估值」的那些日子**。这不是连续序列，
样本是自选的 —— 页面上必须写清楚，别把它当成一份完整的统计报告。

## 三条诚实声明

1. **「真实净值」也是天天基金公布的**，不是基金公司公告原文。这个复盘衡量的是
   「我们的估算 vs 天天基金的净值」，中间还隔着一层源。
2. **样本自选**：只在你手动点查询的日子留下估算，漏点的日子就是没有。
3. **误差超 ±20pp 的样本直接丢弃并计数**。这个量级几乎一定是取数或日期归属出了
   问题（比如把跨周末的两天当成相邻两天），留着会把均值拉到毫无意义的位置。
   丢弃的数量会进 `notes`，**不会静默消失**。

## 相邻交易日的判定

净值日期都是交易日，但「上一条净值」未必是**紧邻的**上一个交易日 —— 中间漏了
一条的话，算出来的就不是当天涨跌幅而是好几天的累计，误差会大到离谱。这里用
**自然日间隔 ≤ 5 天**当代理：跨周末最多 3 天，加上法定长假也够。超过就当作
「中间漏了净值」，这个样本不计。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

__all__ = ["review"]

# 样本少于这个数，平均值基本是噪声。页面据此标低置信度 —— 别让人拿 2 个样本
# 得出「这个源很准」的结论。
LOW_SAMPLE = 5

# 误差绝对值超过这个数的样本丢弃（理由见模块文档「三条诚实声明」第 3 条）
_MAX_ABS_ERR = 20.0

# 相邻净值的自然日间隔上限。跨周末 3 天，再加法定长假；超过说明中间漏了净值，
# 算出来的不是当天涨跌幅。
_MAX_NAV_GAP_DAYS = 5


def _prev(series: Dict[str, float], date: str) -> Optional[str]:
    """`date` 之前最近的一个净值日期。日期都是 `YYYY-MM-DD`，字典序即时间序。"""
    earlier = [d for d in series if d < date]
    return max(earlier) if earlier else None


def _gap_days(a: str, b: str) -> Optional[int]:
    """两个 `YYYY-MM-DD` 之间的自然日数；认不出来回 None（保守：当间隔过大处理）。"""
    try:
        da = datetime.strptime(a, "%Y-%m-%d")
        db = datetime.strptime(b, "%Y-%m-%d")
    except ValueError:
        return None
    return (da - db).days


def review(estimates: Iterable[Dict[str, Any]],
           navs: Iterable[Dict[str, Any]],
           names: Optional[Dict[str, str]] = None
           ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """把两条流水对齐成统计行。**纯函数，不碰数据库、不联网**。

    入参 `estimates` 是 `db.fund_estimates()` 的原样输出，`navs` 是
    `db.fund_navs()` 的原样输出，`names` 是 `{code: 名称}`（来自关注列表，
    已删除关注的基金回传空串，历史照算）。

    出参 `(rows, notes)`：
      - `rows` 每行 = 一只基金 × 一条链，字段见 `metrics` 模块头
      - `notes` 是要原样摊给用户的口径说明与异常计数
    """
    names = names or {}
    nav_by: Dict[str, Dict[str, float]] = {}
    for n in navs:
        code = str(n.get("code") or "").strip()
        d = str(n.get("nav_date") or "").strip()
        try:
            v = float(n.get("nav"))
        except (TypeError, ValueError):
            continue
        if code and d and v > 0:
            nav_by.setdefault(code, {})[d] = v

    notes: List[str] = []
    pending = 0          # 对不上真实净值的估算条数
    gap_skipped = 0      # 上一条净值离得太远，怕算的不是当天涨跌幅
    dropped = 0          # 误差过大，按取数/日期归属出错丢弃
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for e in estimates:
        code = str(e.get("code") or "").strip()
        date = str(e.get("date") or "").strip()
        est = e.get("est_pct")
        if not code or not date or est is None:
            continue

        series = nav_by.get(code)
        if not series:
            pending += 1
            continue
        cur = series.get(date)
        if cur is None:
            pending += 1          # 当天净值还没公布（或这天不是交易日）
            continue
        prev_date = _prev(series, date)
        if prev_date is None:
            pending += 1          # 还没有更早的净值可比
            continue
        gap = _gap_days(date, prev_date)
        if gap is None or gap > _MAX_NAV_GAP_DAYS:
            gap_skipped += 1      # 中间漏了净值，算出来的是多日累计，不是当天
            continue

        real = (cur / series[prev_date] - 1.0) * 100.0
        err = float(est) - real
        if abs(err) > _MAX_ABS_ERR:
            dropped += 1
            continue

        source = str(e.get("source") or "")
        key = (code, source)
        b = buckets.get(key)
        if b is None:
            b = {"errors": [], "covs": [], "last_date": "", "last_err": None}
            buckets[key] = b
        b["errors"].append(err)
        cov = e.get("coverage")
        if cov is not None:
            b["covs"].append(float(cov))
        if date >= b["last_date"]:
            b["last_date"] = date
            b["last_err"] = err

    rows: List[Dict[str, Any]] = []
    for (code, source), b in buckets.items():
        errs = sorted(b["errors"])
        n = len(errs)
        if not n:
            continue
        rows.append({
            "code": code,
            "name": names.get(code, ""),
            "source": source,
            "samples": n,
            "mean_err": round(sum(errs) / n, 3),
            "mean_abs_err": round(sum(abs(x) for x in errs) / n, 3),
            # 极值取自已保留的样本，所以「最坏」不会被那几条被丢掉的撑大
            "max_over": round(errs[-1], 3),
            "max_under": round(errs[0], 3),
            "last_date": b["last_date"],
            "last_err": round(b["last_err"], 3) if b["last_err"] is not None else None,
            "coverage_mean": (
                round(sum(b["covs"]) / len(b["covs"]), 2) if b["covs"] else None
            ),
            "low_sample": n < LOW_SAMPLE,
        })
    # 样本多的排前面：样本少的那几行本来就更可能是噪声
    rows.sort(key=lambda r: (-r["samples"], r["code"], r["source"]))

    if pending:
        notes.append(
            f"{pending} 条估算还对不上真实净值（当天的净值尚未公布，或这只基金还没攒够"
            f"可比数据）—— 第二天再点一次「查询估值」，前一天的样本就会闭合")
    if gap_skipped:
        notes.append(
            f"{gap_skipped} 条估算的上一条净值间隔超过 {_MAX_NAV_GAP_DAYS} 天，"
            f"中间多半漏了净值，算出来的是多日累计而不是当天涨跌幅，已不计入统计")
    if dropped:
        notes.append(
            f"{dropped} 条样本的误差超过 ±{_MAX_ABS_ERR:.0f}pp，这个量级几乎一定是"
            f"取数或日期归属出了问题，已丢弃不进统计")
    return rows, notes
