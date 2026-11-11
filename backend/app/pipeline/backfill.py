# -*- coding: utf-8 -*-
"""历史数据回填（阶段 4）—— 只补**有历史接口**的数据，没有的一律显式拒绝。

三条不可让步的口径：

1. **日线绝不落前复权。** `fetchers/quotes.daily_history()` 里写死
   ``adjust="qfq"``，本模块**不走它**，直接调 akshare 并显式传
   ``adjust=""``（不复权）+ 独立的 ``adj_factor`` 列。前复权是「从今天往回
   算」的价格 —— 日后配息除权，整段历史都会跟着变，回测等于在追一个不断
   漂移的目标（docs/backtest/§3 记过这个坑）。

2. **已有数据不覆盖。** 落库走 `bars_upsert` 的 ``ON CONFLICT DO NOTHING``。
   回填最怕的不是漏，而是把已经核对过的行情用一次重新抓取静默盖掉、而那次
   抓取恰好源只回了半截 —— 那样连追溯的可能都没了。要重写必须显式调
   `bars_overwrite`，把意图留在调用处。

3. **当日数据不假装能补。** `pool_*` 与 `stock_net_inflow` 是收盘那一刻
   当场采的，事后公开接口**取不回**。回填工具对它们一律拒绝并说明原因 ——
   返回「不支持」，绝不返回空结果让调用方以为「补过了，本来就没有」。

## 关于联网

``plan()`` **零联网**，只读本地库：回填了什么、还缺什么、要发多少次请求。
``backfill()`` **会联网**，只在人明确点了「开始回填」时才调 —— 启动、页面
刷新、任何定时器都不碰它。当前处于内网环境，抓股票接口会被公司安全设备
拦截并留记录，所以本轮**只交付工具、不执行真实回填**（docs/data_pipeline/
audit.md §12 决策 1）。

## 断点续跑为什么不需要额外的状态表

``backfill()`` 每只票先读 `bar_span`（已有首末日），区间已覆盖就跳过，
未覆盖才发请求。所以「暂停后原样再跑一次」就是恢复 —— 中断到哪、下次就从
哪继续，没有一张需要人工对齐的进度表。

## 阶段 5 补的三件事

1. **腾讯备源**：东财失败/回空 → `stock_zh_a_hist_tx(adjust="")`，整只票同源，
   ``source`` 如实落列（上面 `fetch_daily` 有细节）。
2. **失败区间单独重试**：`bar_span` 看不出「首末日盖住了但中间断了」，所以
   有了 `db.bar_gaps()`；``only_gaps=True`` 时 `plan()`/`backfill()` 只按
   缺口子区间发请求，一天不缺就一次请求都不发。
3. **收尾自动重跑覆盖审计**：`backfill()` 结束时把 `bars_coverage`（日线域
   另加因子覆盖）放进返回值 —— 跑批日志与 `job_run.detail` 都看得到，不用
   再手动点一次刷新。`should_stop` 则给「优雅暂停」用：票边界上停，已落库的
   一行都不少。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set

from app.config import settings
from app.store import db

logger = logging.getLogger(__name__)

#: 沪深300。研究引擎 `_load_bench` 只认 `'sh000300'` / `'000300'` 这两个字面量。
INDEX_CODE = "sh000300"

#: 复权口径：**空串 = 不复权**。改这个值等于改历史价格的定义，不许改。
ADJUST = ""

#: 源名。行上的 ``source`` 与 `daily_bar.source` 列如实记这几个值 —— 事后查
#: 「这行是谁给的」，答案必须在库里，不在某个人的记忆里。
SRC_EM = "eastmoney"
SRC_TX = "tencent"
SRC_SINA = "sina"


class FetchError(RuntimeError):
    """取数失败，且**说得出是哪个源在拒**。

    熔断阈值不变（那还是脚本的事），但失败必须带源：整批失败时若分不清是
    东财在拦还是腾讯在挂，就只能瞎猜该等多久、该换谁。
    """

    def __init__(self, msg: str, source: str = "") -> None:
        super().__init__(msg)
        #: 最后试过的那个源（东财挂了才轮到它）
        self.source = source

#: 回填默认起始日（2026-10-16 用户拍板，docs/data_pipeline/audit.md §12）
DEFAULT_START = "20260801"

#: 可回填的数据域 → 说明与落表
BACKFILLABLE: Dict[str, Dict[str, Any]] = {
    "daily_bar": {
        "label": "全市场个股日线",
        "table": "daily_bar",
        "needs": "codes",
        "note": "不复权 OHLC + 独立复权因子。解开回测 BLOCKED 的关键。",
    },
    "index": {
        "label": "沪深300 指数日线",
        "table": "index_daily",
        "needs": None,          # 固定 sh000300，不需要外部列表
        "note": "解开「无基准 → 无超额收益」的关键。",
    },
    "sector_hist": {
        "label": "板块历史资金流",
        "table": None,          # 仍落 snapshot，按日期归档
        "needs": "names",
        "note": "按板块名逐个取历史，需先有板块名单。",
    },
}

#: 显式拒绝清单。**key 必须列全** —— 漏一个，调用方就会拿到空结果并误以为
#: 「补过了，本来就没有」。
REFUSED: Dict[str, str] = {
    "pool_zt": "涨停池是收盘那一刻当场采的，事后公开接口取不回 —— 永久不可补。",
    "pool_zb": "炸板池同上，当日数据事后不可得。",
    "pool_dt": "跌停池同上，当日数据事后不可得。",
    "pool_strong": "强势池同上，当日数据事后不可得。",
    "stock_net_inflow": "个股当日资金流是收盘当场采的，事后不可得 —— 永久不可补。",
    "listing": "股票基础信息只给**当前**名单，不带日期维度，回填历史无意义。",
}

_REFUSED_HINT = "已在 REFUSED 显式拒绝的域不许回填 —— 用计划或补跑代替。"


def refuse(domain: str) -> Optional[str]:
    """该域能不能回填。不能则返回**给用户看的理由**，能则 None。"""
    return REFUSED.get(domain)


def _require_known(domain: str) -> Dict[str, Any]:
    """域必须在 BACKFILLABLE 里，且**没被拒绝**。"""
    why = refuse(domain)
    if why:
        raise ValueError(why)
    spec = BACKFILLABLE.get(domain)
    if spec is None:
        raise ValueError(
            f"不认识的数据域：{domain!r}。"
            f"可回填的有 {sorted(BACKFILLABLE)}；"
            + (f"已明确拒绝的有 {sorted(REFUSED)}（{_REFUSED_HINT}）"
               if REFUSED else ""))
    return spec


def _norm_date(d: str) -> str:
    """`2026-08-01` → `20260801`。非法就抛，不猜。

    只查形状不够：`2026-08-32` 归一后是 8 位数字，形状完全合法，但不是个
    真日期 —— 放过去的结果是「按这个区间去抓，一无所获」，而日志上看着就像
    「那个区间本来就没有数据」。宁可在这里就报错。
    """
    s = str(d).strip().replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"日期格式必须是 YYYYMMDD 或 YYYY-MM-DD，收到 {d!r}")
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"不是合法日期：{d!r}") from exc
    return s


# ------------------------------------------------------------------ 取数
#
# 三个取数器都是**模块级函数**，测试把它们换成替身即可全离线跑通端到端。
# 真实调用一个都不会发生 —— 这也是测试里唯一的联网点。


# --------------------------------------------------------------- 股票名单
#
# 回填跑批和页面看进度必须用**同一套**名单判据。两边各写一份的话，「到底有
# 多少只该跑」就会出现两个数，而对不上时没人说得清哪个是对的 —— 这种分歧
# 恰恰出现在最容易骗人的地方（进度条走到 100% 但其实少了一百只）。
#
# 名单只从本地库的 snapshot payload 里解出来，**一次外发都没有**。本模块
# 不为此去联网取名单：`plan()` 要求调用方显式传 items 是同一个道理。

#: 名单范围。`all` 是并集，不是「最新一天」—— 逐日资金流只有当天出现过的
#: 票才有行，并集才等于「这段时间市场上露过面的全部代码」。
SCOPES = ("all", "mf", "pool", "wl")

#: `pool` 覆盖的股票池数据域。加新的池子要同时改这里，否则新池子的票
#: 悄悄不进名单，而名单看起来仍然是「全的」。
POOL_DATASETS = ("pool_zt", "pool_zb", "pool_dt", "pool_strong")


def _open_ro() -> sqlite3.Connection:
    """只读打开当前库。URI 模式开，杜绝意外写入。"""
    con = sqlite3.connect(
        Path(settings.db_path).resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def _walk_codes(obj: Any, into: Set[str]) -> None:
    """从 snapshot 的 payload 里把 6 位代码全挖出来。

    payload 是包型（`[{rows, all_rows, ...}]`）或裸列表，结构随域不同，所以
    按**结构**遍历而不是按固定键取 —— 键名改了这里不会静默漏掉一批票。漏一
    批票的后果是回填范围比预期小，而进度条照样显示「跑完了」。
    """
    if isinstance(obj, dict):
        c = obj.get("code")
        if isinstance(c, str) and len(c) == 6 and c.isdigit():
            into.add(c)
        for v in obj.values():
            _walk_codes(v, into)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_codes(v, into)
    elif isinstance(obj, str) and len(obj) == 6 and obj.isdigit():
        into.add(obj)


def _payloads(dataset: str) -> List[Any]:
    """该数据域全部归档日的 payload（按日期升序），解析不动的跳过。"""
    con = _open_ro()
    try:
        rows = con.execute(
            "SELECT payload FROM snapshot WHERE dataset=? ORDER BY trade_date",
            (dataset,)).fetchall()
    finally:
        con.close()
    out: List[Any] = []
    for r in rows:
        try:
            out.append(json.loads(r["payload"]))
        except (TypeError, ValueError):
            continue          # 解析不动的那天不参与并集，也不算「没有名单」
    return out


def load_scope(scope: str) -> List[str]:
    """按范围取股票名单（升序）。**只读本地库，零联网。**

    - ``all``  : `stock_net_inflow` 全部归档日的代码**并集**（≈全市场）
    - ``mf``   : `main_force` 全 payload 并集（研究引擎的 universe 就是它）
    - ``pool`` : `POOL_DATASETS` 四个股票池的并集
    - ``wl``   : `watchlist`（网页端自选，含港股 5 位码 —— 这里不过滤市场，
                 等调用方按 `code_to_symbol` 自己判，判不出的自然会被跳过）

    名单为空时回**空列表**，不抛异常也不猜：调用方要把「名单是空的」当成
    一个明确的结论显示出来，而不是拿到一个看起来正常的空进度条。
    """
    if scope not in SCOPES:
        raise ValueError(f"未知名单范围 {scope}（可选：{'/'.join(SCOPES)}）")

    if scope == "wl":
        con = _open_ro()
        try:
            rows = con.execute("SELECT code FROM watchlist").fetchall()
        finally:
            con.close()
        return sorted({str(r[0]) for r in rows if r[0]})

    datasets = {
        "all": ("stock_net_inflow",),
        "mf": ("main_force",),
        "pool": POOL_DATASETS,
    }[scope]
    got: Set[str] = set()
    for ds in datasets:
        for pl in _payloads(ds):
            _walk_codes(pl, got)
    return sorted(got)


def fetch_index(_target: str, start_date: str, end_date: str
                ) -> List[Dict[str, Any]]:
    """沪深300 日线（新浪源）。返回不复权 OHLC 的行列表。

    首参 ``_target`` **故意不使用**：三个取数器统一收 `(target, start, end)`，
    这样 `backfill()` 不必按域分支去凑参数个数 —— 分支一多就容易给某一个传错
    个数（第一版就是这么把 `fetch_index` 只传了 2 个参数）。
    """
    import akshare as ak  # noqa: PLC0415  按需导入，避免拖慢启动
    import pandas as pd   # noqa: PLC0415
    from app.fetchers.client import call  # noqa: PLC0415

    raw = call("回填 沪深300",
               ak.stock_zh_index_daily, symbol=INDEX_CODE)
    if raw is None or len(raw) == 0:
        return []
    df = pd.DataFrame(raw)
    out = pd.DataFrame({
        "code": INDEX_CODE,
        "trade_date": pd.to_datetime(df["date"]).dt.strftime("%Y%m%d"),
        "open": pd.to_numeric(df.get("open"), errors="coerce"),
        "high": pd.to_numeric(df.get("high"), errors="coerce"),
        "low": pd.to_numeric(df.get("low"), errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df.get("volume"), errors="coerce"),
        "pct_chg": None,
        # 这条链走新浪，如实记 —— 事后查「这行谁给的」答案在库里不在记忆里
        "source": SRC_SINA,
    })
    out = out[(out["trade_date"] >= start_date)
              & (out["trade_date"] <= end_date)]
    return out.where(out.notna(), None).to_dict("records")


def fetch_daily(code: str, start_date: str, end_date: str
                ) -> List[Dict[str, Any]]:
    """个股日线，**不复权**。东财为主、腾讯备源（2026-10-17 拍板走腾讯）。

    不走 `quotes.daily_history()` —— 它内部写死 ``adjust="qfq"``（前复权），
    落进历史表就再也分不清哪根线是当时真实的价格。这里两条路径都显式传
    ``adjust=ADJUST``（空串 = 不复权），AST 断言钉死。

    **整只票一次性同源**：东财失败或回空才回退腾讯，回退发生在**票级** ——
    一只票的全部行来自同一个源，绝不在票内逐日切换（半东财半腾讯的日线，
    事后解释不清哪天是哪个口径）。行上带 ``source``，如实落进 `daily_bar.source`。

    字段映射**复用** `quotes._hist_from_tx`（腾讯/新浪的 turnover 是小数须 ×100、
    无涨跌幅列按收盘推算），不另写一套：两处各写一套，迟早漂移成两种口径。

    两个源都回空 → 回 ``[]``（调用方按「源返回 0 行」计 failed）；两个源都
    挂 → 抛 `FetchError`，``source`` 记**最后试的那个源**，消息里两个源的错都
    在 —— 熔断阈值不变，但日志上要看得出是谁在拒。
    """
    import akshare as ak  # noqa: PLC0415
    from app.fetchers.client import call  # noqa: PLC0415

    errs: List[str] = []

    # ---- 主源：东财 ----
    df = None
    try:
        df = call(f"回填日线 {code}", ak.stock_zh_a_hist,
                  symbol=code, period="daily",
                  start_date=start_date, end_date=end_date, adjust=ADJUST)
    except Exception as exc:  # noqa: BLE001  主源挂了要落到备源，不是整票失败
        errs.append(f"{SRC_EM}: {type(exc).__name__}: {exc}")
    rows = _em_rows(code, df)
    if rows:
        return rows
    if df is not None and len(df) == 0:
        errs.append(f"{SRC_EM}: 0 行")

    # ---- 备源：腾讯 ----
    try:
        raw = call(f"回填日线 {code}(腾讯)", ak.stock_zh_a_hist_tx,
                   symbol=code, start_date=start_date, end_date=end_date,
                   adjust=ADJUST)
    except Exception as exc:  # noqa: BLE001
        errs.append(f"{SRC_TX}: {type(exc).__name__}: {exc}")
        raise FetchError("；".join(errs), source=SRC_TX) from exc
    return _tx_rows(code, raw)


def _em_rows(code: str, df: Any) -> List[Dict[str, Any]]:
    """东财 `stock_zh_a_hist` 的行 → 回填行（字段名是中文那套）。"""
    if df is None or len(df) == 0:
        return []
    out = [{
        "code": code,
        "trade_date": str(r.get("日期") or "").replace("-", ""),
        "open": _num(r.get("开盘")),
        "high": _num(r.get("最高")),
        "low": _num(r.get("最低")),
        "close": _num(r.get("收盘")),
        "volume": _num(r.get("成交量")),
        "amount": _num(r.get("成交额")),
        "turnover": _num(r.get("换手率")),
        "pct_chg": _num(r.get("涨跌幅")),
        # 复权因子接口本域拿不到 —— 留 NULL，**不填 1.0 充数**（填了就等于
        # 悄悄声明「没有复权」，那是口径，不是数据）。
        "adj_factor": None,
        "source": SRC_EM,
    } for r in df.to_dict("records")]
    return [r for r in out if r["trade_date"]]


def _tx_rows(code: str, df: Any) -> List[Dict[str, Any]]:
    """腾讯 `stock_zh_a_hist_tx` 的行 → 回填行。

    映射**复用** `quotes._hist_from_tx`：turnover 小数 ×100、pct_chg 按收盘
    推算都在那边，这里只做列名对齐（`date` → `trade_date`）+ 补 code/source。
    """
    if df is None or len(df) == 0:
        return []
    # 惰性导入：`quotes` 模块级就 import akshare，拖进 backfill 顶层会把
    # 「按需导入、不拖慢启动」这条破掉。
    from app.fetchers.quotes import _hist_from_tx  # noqa: PLC0415
    got = _hist_from_tx(df)
    if got is None or len(got) == 0:
        return []
    out = [{
        "code": code,
        "trade_date": str(r.get("date") or "").replace("-", ""),
        "open": _num(r.get("open")),
        "high": _num(r.get("high")),
        "low": _num(r.get("low")),
        "close": _num(r.get("close")),
        "volume": _num(r.get("volume")),
        "amount": _num(r.get("amount")),
        "turnover": _num(r.get("turnover")),
        "pct_chg": _num(r.get("pct_chg")),
        "adj_factor": None,
        "source": SRC_TX,
    } for r in got.to_dict("records")]
    return [r for r in out if r["trade_date"]]


def _num(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return None if x != x else x          # NaN -> None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ 计划


def plan(domain: str, start_date: str, end_date: str,
         items: Optional[Sequence[str]] = None,
         only_gaps: bool = False) -> Dict[str, Any]:
    """回填计划。**只读本地库、零联网** —— 点开看看要发多少请求就走这个。

    拒绝的域在这里就已经回错误，不会进到 `backfill()`。

    :param only_gaps: 按**缺口**算待办，而不是按「首末日盖没盖住」。默认 False
        与 `backfill()` 的默认行为一一对应 —— 计划说跑几只，执行就跑几只，两
        个口径不许分叉。开关是什么都无所谓：**中间断档的票一律报进 ``holes``**，
        那正是「首末日看着盖住了、里面其实是空的」那批（东财被拦的那几天）。
    """
    spec = _require_known(domain)
    start, end = _norm_date(start_date), _norm_date(end_date)
    if end < start:
        raise ValueError(f"结束日 {end} 早于起始日 {start}")

    if spec["table"]:
        cov = db.bars_coverage(spec["table"])
    else:
        cov = {"rows": None, "days": None, "codes": None,
               "first_date": None, "last_date": None}

    holes: List[Dict[str, Any]] = []

    def _holes_of(code: str) -> List[Dict[str, Any]]:
        """该票中间断了哪几段。ranges 截断，segments 是真数。"""
        g = db.bar_gaps(spec["table"], code, start, end) if spec["table"] else []
        return ([{"code": code, "segments": len(g), "ranges": g[:20],
                  "ranges_truncated": len(g) > 20}] if g else [])

    todo: List[str] = []
    done: List[Dict[str, Any]] = []
    if spec.get("needs") == "codes":
        if not items:
            # 抛错而不是回 {"error": ...}：缺 codes 跟「日期不合法」是同一类
            # 输入问题，端点那边统一按 400 处理。回错误字典的话，HTTP 会 200，
            # 前端只看 status 就会以为「计划算出来了，待办 0 个」。
            raise ValueError("全市场日线需要显式传 codes（股票列表），"
                             "不会自己去联网取名单。")
        for c in items:
            c = str(c)
            span = db.bar_span(spec["table"], c)
            # 区间已盖住 [start, end] 才算完成 —— 断点续跑的判据就是它
            if span and span[0] <= start and span[1] >= end:
                # 盖住了**不代表里面是满的**：首末日看不出中间断没断
                sub = _holes_of(c)
                if sub:
                    holes.extend(sub)
                    if only_gaps:
                        todo.append(c)      # 只补缺口模式下，它就是要跑的
                        continue
                done.append({"code": c, "span": list(span)})
            else:
                todo.append(c)
    elif domain == "sector_hist":
        todo = [str(x) for x in (items or [])]
    # index：固定一只，看库里区间够不够
    if domain == "index":
        span = db.bar_span(spec["table"], INDEX_CODE)
        if span and span[0] <= start and span[1] >= end:
            sub = _holes_of(INDEX_CODE)
            if sub:
                holes.extend(sub)
                if only_gaps:
                    todo = [INDEX_CODE]
                else:
                    done.append({"code": INDEX_CODE, "span": list(span)})
            else:
                done.append({"code": INDEX_CODE, "span": list(span)})
        else:
            todo = [INDEX_CODE]

    return {
        "domain": domain,
        "label": spec["label"],
        "start": start,
        "end": end,
        "offline": True,
        "coverage": cov,
        # 已完成 / 待回填**分开记** —— 只回 todo 会看不出「其实已经补过了」
        "already_done": len(done),
        "to_fetch": len(todo),
        "todo": todo[:200],                 # 截断：5200 只全列出来没意义
        "todo_truncated": len(todo) > 200,
        # 中间断档的票（首末日盖住了、里面有洞）—— 单独报，不混进 todo：
        # 普通回填对它们是**跳过**的，混进去就会出现「计划说跑 3、实跑 0」
        "hole_codes": len(holes),
        "holes": holes[:50],
        "holes_truncated": len(holes) > 50,
        "only_gaps": bool(only_gaps),
        "adjust": ADJUST if domain in ("daily_bar", "index") else None,
        "note": spec["note"],
    }


# ------------------------------------------------------------------ 执行


def backfill(domain: str, start_date: str, end_date: str,
             items: Optional[Sequence[str]] = None,
             budget: Optional[int] = None,
             on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
             fetcher: Optional[Callable[..., List[Dict[str, Any]]]] = None,
             only_gaps: bool = False,
             should_stop: Optional[Callable[[], bool]] = None
             ) -> Dict[str, Any]:
    """执行回填。**会联网** —— 只在人明确点了「开始回填」时才调。

    :param budget:      本次最多发多少次取数请求。用完即停，返回
                        ``finished=False`` 与剩余清单 —— **再跑一次同样的
                        参数就接着干**，这就是断点续跑。
    :param on_progress: 每批回调一次，给页面画进度用。回调里的异常不影响回填。
    :param fetcher:     注入替身取数器（测试用）。缺省走真实的网络取数。
    :param only_gaps:   只补缺口（阶段 5）：逐票比对**已有交易日**，中间断档的
                        段单独重试，一天不缺就一次请求都不发。判据与
                        `plan(only_gaps=True)` 同源 —— 计划与执行不许分叉。
    :param should_stop: 优雅暂停（阶段 5）：每只票**落库之后**问一次，回 True
                        就收尾退出（``paused=True``、``finished=False``）。票
                        边界停，落过的都在，原样再跑一次就是续跑。

    :return: ``{finished, requested, ran, skipped, fetched, inserted, failed,
              paused, sources, coverage, ...}``

    **不把「补过了」写成 success**：发了请求但一行没落库的，计进 ``failed``
    并带原因，不会混进 ``inserted``。
    """
    spec = _require_known(domain)
    start, end = _norm_date(start_date), _norm_date(end_date)
    if end < start:
        raise ValueError(f"结束日 {end} 早于起始日 {start}")

    get = fetcher or (fetch_index if domain == "index"
                      else fetch_daily if domain == "daily_bar" else None)
    if get is None:
        raise ValueError(f"数据域 {domain} 没有可用的取数器")

    # ---- 先算要跑哪些（与 plan 同一套判据，不另写一遍逻辑）----
    # holes: 票 → 缺口子区间 [[from, to], ...]；有洞的票逐段取，不整段重跑。
    todo: List[str] = []
    holes: Dict[str, List[List[str]]] = {}
    skipped = 0
    if spec.get("needs") == "codes":
        if not items:
            raise ValueError("全市场日线需要显式传 codes（股票列表）")
        for c in items:
            c = str(c)
            span = db.bar_span(spec["table"], c) if spec["table"] else None
            if span and span[0] <= start and span[1] >= end:
                # 首末日盖住了 —— 但里面断没断要逐日看（阶段 5 的缺口重试）
                if only_gaps and spec["table"]:
                    # 普通模式不查缺口：它对洞无感，查了也是白查一次
                    g = db.bar_gaps(spec["table"], c, start, end)
                    if g:
                        holes[c] = g
                        todo.append(c)
                    else:
                        skipped += 1       # 一天不缺 => 一次请求都不发
                else:
                    skipped += 1
            else:
                todo.append(c)
    elif domain == "index":
        span = db.bar_span(spec["table"], INDEX_CODE)
        covered = bool(span and span[0] <= start and span[1] >= end)
        if only_gaps and covered:
            g = db.bar_gaps(spec["table"], INDEX_CODE, start, end)
            if g:
                holes[INDEX_CODE] = g
                todo = [INDEX_CODE]
            else:
                skipped = 1
        else:
            # 普通模式照旧整段取（区间没盖住、或压根没跑过）
            todo = [INDEX_CODE]
    else:
        todo = [str(x) for x in (items or [])]

    requested = len(todo)
    if budget is not None:
        todo = todo[:max(0, int(budget))]

    inserted = failed = fetched = ran_n = 0
    problems: List[Dict[str, Any]] = []
    #: 源 → {items, rows, inserted, failed}。整批失败时看得出是谁在拒，
    #: 而不是只能猜「大概是被封了吧」。
    sources: Dict[str, Dict[str, int]] = {}
    paused = False

    def _src(name: str) -> Dict[str, int]:
        return sources.setdefault(
            name, {"items": 0, "rows": 0, "inserted": 0, "failed": 0})

    for i, target in enumerate(todo):
        if should_stop is not None and should_stop():
            # 优雅暂停：在**票边界**停 —— 一只票要么整只进来要么整只不进，
            # 不留半截。落过的都在，原样再跑一次就是续跑。
            paused = True
            logger.info("收到暂停请求，回填在票边界停下 %s [%s..%s]",
                        domain, start, end)
            break
        ran_n += 1               # 实际跑了几只 —— 暂停时它 < len(todo)，不谎报
        ranges = holes.get(target) or [(start, end)]
        rows: List[Dict[str, Any]] = []
        try:
            for (a, b) in ranges:      # 只补缺口时一次一段，不是整段重跑
                rows.extend(get(target, a, b) or [])
        except Exception as exc:  # noqa: BLE001  单只票挂了不能连累整批
            failed += 1
            # `FetchError` 带 source（哪个源在拒）；普通异常没有就留空
            src = str(getattr(exc, "source", "") or "")
            if src:
                _src(src)["failed"] += 1
            prob: Dict[str, Any] = {"item": target,
                                    "error": f"{type(exc).__name__}: {exc}"}
            if src:
                prob["source"] = src
            if holes:
                prob["range"] = [ranges[0][0], ranges[-1][1]]
            problems.append(prob)
            logger.warning("回填失败 %s %s：%s", domain, target, exc)
        else:
            fetched += len(rows)
            name = str(rows[0].get("source") or "unattributed") if rows else ""
            if rows:
                rec = _src(name)
                rec["items"] += 1
                rec["rows"] += len(rows)
            if spec["table"] and rows:
                n = db.bars_upsert(spec["table"], rows)
                inserted += n
                _src(name)["inserted"] += n
            elif spec["table"] and not rows:
                # 源回了空 —— 这不是成功，记下来让人看得见
                failed += 1
                problems.append({"item": target, "source": None,
                                 "error": "源返回 0 行，未落库"})
        if on_progress and (i + 1) % 10 == 0:
            try:
                on_progress({"domain": domain, "done": i + 1,
                             "total": len(todo), "inserted": inserted})
            except Exception:  # noqa: BLE001  进度回调绝不影响回填本身
                logger.debug("进度回调抛异常，忽略", exc_info=True)

    out = {
        "domain": domain,
        "start": start,
        "end": end,
        "requested": requested,     # 这一轮本来该跑多少
        "ran": ran_n,               # 实际跑了几只（budget/暂停都会让它小于 requested）
        "skipped": skipped,         # 区间已覆盖（或不缺），压根没发请求
        "fetched": fetched,         # 源一共给了多少行
        "inserted": inserted,       # **真正新插入**多少行（已有的不算）
        "failed": failed,
        "problems": problems[:50],
        # finished=False 就是「还有活没干完」，前端据此显示「继续回填」按钮
        "finished": (not paused) and len(todo) == requested,
        "paused": paused,
        "only_gaps": bool(only_gaps),
        "sources": sources,
        "offline": False,
        "adjust": ADJUST if domain in ("daily_bar", "index") else None,
    }
    # 收尾自动重跑覆盖审计（阶段 5）：跑完就该知道现在覆盖到哪了，不必再手动
    # 点一次刷新。零联网 —— 全是本地 COUNT / MIN / MAX。
    if spec["table"]:
        out["coverage"] = db.bars_coverage(spec["table"])
        if domain == "daily_bar":
            out["factor_coverage"] = db.bars_factor_coverage(spec["table"])
    logger.info("回填结束 %s [%s..%s] 请求 %d 跳过 %d 取得 %d 行 落库 %d 行 "
                "失败 %d%s", domain, start, end, requested, skipped, fetched,
                inserted, failed, "（已暂停）" if paused else "")
    return out
