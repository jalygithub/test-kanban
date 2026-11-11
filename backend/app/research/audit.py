# -*- coding: utf-8 -*-
"""阶段 1 —— 数据可回测性审计（只读、零联网）。

审计对象是**库里的既有事实**，不做任何推断式填充：

  * 查得到的 → 给区间、覆盖率、缺失率；
  * 查不到的 → 给 ``BLOCKED`` 与**具体原因**，绝不拿「当前值」冒充历史值；
  * 能区分「当日观测」与「事后回补」的，逐日标注（``fetched_at`` 对比
    ``trade_date``，UTC+8 后同日才算当日观测）。

状态口径（任务书 §四）::

    READY    可用于指定回测
    PARTIAL  部分可用，存在已知限制（**不得自动当作 READY**）
    BLOCKED  关键数据不足，不能执行对应实验

审计围绕**信号日**展开：``main_force`` 快照里有几天，就把那几天逐日判定
一遍「这一天上六维回测还缺什么」。这样结论是可执行的，而不是一句空泛的
「数据不足」。

与采集侧的分工（2026-10-16 用户拍板「做成共享」）::

    app/pipeline/coverage.py   事实 —— 采到哪天、缺什么、上次什么时候、错在哪
    本模块                     判定 —— 拿这些事实判「能不能回测」

本模块**不再自己算覆盖事实**，只在 ``report["pipeline"]`` 里原样带上
``coverage.coverage_report()`` 的结果。所以策略实验室页和数据管理页看到的
「缺 20260929」「6 天条数偏低」是同一份数字，不会出现一个说 3 天一个说 4 天。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.config import settings
from app.pipeline import coverage as pipeline_coverage

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------- 状态口径
READY = "READY"
PARTIAL = "PARTIAL"
BLOCKED = "BLOCKED"
_STATUS_RANK = {READY: 0, PARTIAL: 1, BLOCKED: 2}


def worst(*statuses: str) -> str:
    """取最差状态。PARTIAL **不会**被 READY 抹平。"""
    return max(statuses, key=lambda s: _STATUS_RANK.get(s, 2))


# ------------------------------------------------------------- 日线表的发现
# 只认「同时具备 open/high/low/close 四列」的表，避免把 watchlist.open_id
# 这种同名字段误判成行情表（审计时踩过这个坑）。
_BAR_COLS = ("open", "high", "low", "close")
_DATE_COLS = ("trade_date", "date", "day", "candle_date")
_CODE_COLS = ("code", "symbol", "sec_code", "ts_code")
_VOL_COLS = ("volume", "vol", "amount")
_ADJ_HINTS = ("adj", "hfq", "qfq", "factor", "复权")


def _q(ident: str) -> str:
    """标识符加双引号。名字只来自 sqlite_master（我们自己的 schema），
    转义引号后即可安全拼进 SQL。"""
    return '"' + ident.replace('"', '""') + '"'


def _cols(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute("PRAGMA table_info(%s)" % _q(table))]


def _tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def discover_bar_tables(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    """找出库里真正的日线表。没有则返回空列表 —— 调用方据此判 BLOCKED。"""
    out: List[Dict[str, Any]] = []
    for table in _tables(conn):
        cols = _cols(conn, table)
        low = [c.lower() for c in cols]
        if not all(c in low for c in _BAR_COLS):
            continue
        date_col = next((cols[i] for i, c in enumerate(low) if c in _DATE_COLS), None)
        code_col = next((cols[i] for i, c in enumerate(low) if c in _CODE_COLS), None)
        adj = sorted({c for c in low if any(h in c for h in _ADJ_HINTS)})
        out.append({
            "table": table,
            "date_col": date_col,
            "code_col": code_col,
            "columns": cols,
            "adjust_fields": adj,
        })
    return out


# ================================================================ 审计服务
class BacktestDataAuditService:
    """对本地库做一次可回测性体检，输出结构化报告。

    每次调用 ``audit()`` 都是**现查现算**，不缓存 —— 库在变，缓存会让页面
    显示上一次的结论。代价是几万行 payload 的解析，实测远低于百毫秒级。
    """

    # ---------------------------------------------------------- 入口
    def audit(self) -> Dict[str, Any]:
        path = Path(settings.db_path)
        exists = path.exists()
        report: Dict[str, Any] = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "db_path": str(path),
            "db_exists": exists,
            "offline": True,
            "domains": {},
            "signal_days": [],
            "overall": {},
            # 采集覆盖事实（共享层）。库文件不存在时留空 dict —— 形状稳定，
            # 前端不用同时处理 null / undefined / 缺键三种情况。
            "pipeline": {},
        }
        if not exists:
            empty = {
                "status": BLOCKED,
                "reasons": ["数据库文件不存在，无可审计的数据。"],
            }
            report["domains"] = {k: dict(empty) for k in
                                 ("daily_bars", "fund_flow", "universe",
                                  "benchmark", "scoring_snapshots")}
            report["overall"] = self._overall(report)
            return report

        try:
            # 复用共享层的只读打开方式：全项目**只有一处** mode=ro 连接逻辑，
            # 以后要加只读超时、URI 参数，改一个地方就行。
            conn = pipeline_coverage.open_ro(str(path))
        except (sqlite3.Error, OSError) as exc:  # pragma: no cover - 文件损坏才走得到
            logger.error("只读打开库失败: %s", exc)
            report["domains"] = {
                "daily_bars": {"status": BLOCKED, "reasons": [f"库无法只读打开：{exc}"]},
            }
            report["pipeline"] = self._pipeline_facts(str(path), exc)
            report["overall"] = self._overall(report)
            return report

        try:
            cfg = self._scorer_cfg()
            domains = {
                "daily_bars": self._audit_daily_bars(conn, cfg),
                "fund_flow": self._audit_fund_flow(conn, cfg),
                "universe": self._audit_universe(conn),
                "benchmark": self._audit_benchmark(conn),
                "scoring_snapshots": self._audit_snapshots(conn),
            }
            report["domains"] = domains
            report["signal_days"] = self._signal_days(conn, domains, cfg)
        finally:
            conn.close()

        # 覆盖事实来自共享层，不在本模块重算（见模块 docstring 的分工）。
        report["pipeline"] = self._pipeline_facts(str(path))
        report["overall"] = self._overall(report)
        return report

    @staticmethod
    def _pipeline_facts(path: str,
                        open_error: Optional[BaseException] = None
                        ) -> Dict[str, Any]:
        """采集覆盖事实（共享层）。库打不开也要返回结构，别让前端拿到 null。"""
        if open_error is not None:
            return {"error": f"库无法只读打开：{type(open_error).__name__}: {open_error}"}
        try:
            return pipeline_coverage.coverage_report(path)
        except Exception as exc:  # noqa: BLE001
            # 覆盖事实拿不到**不该拖垮整份可回测性审计** —— 两边各报各的错。
            logger.warning("采集覆盖事实获取失败: %s", exc)
            return {"error": f"覆盖事实获取失败：{type(exc).__name__}: {exc}"}

    # ---------------------------------------------------------- 配置
    @staticmethod
    def _scorer_cfg() -> Dict[str, Any]:
        """评分配置**唯一入口** —— 与打分流水线读同一份 scorer.yaml，
        窗口要求不会出现两套口径。"""
        from app.metrics import mainforce
        try:
            return mainforce.load_config()
        except Exception as exc:  # noqa: BLE001
            logger.error("读取 scorer.yaml 失败：%s", exc)
            return {}

    @staticmethod
    def _dimensions() -> List[Dict[str, Any]]:
        """六个维度的 key / 展示名 / 权重。权重取自 scorer.yaml，
        标签取自 mainforce.DIMENSIONS，两边都是既有事实。"""
        from app.metrics import mainforce
        cfg = BacktestDataAuditService._scorer_cfg()
        weights = cfg.get("weights") or {}
        out: List[Dict[str, Any]] = []
        for d in getattr(mainforce, "DIMENSIONS", ()):
            key = d[0]
            label = d[1] if len(d) > 1 else key
            out.append({"key": key, "label": label,
                        "weight": weights.get(key, 0)})
        return out

    # ---------------------------------------------------------- 1) 日线
    def _audit_daily_bars(self, conn: sqlite3.Connection,
                          cfg: Dict[str, Any]) -> Dict[str, Any]:
        bars = discover_bar_tables(conn)
        if not bars:
            return {
                "status": BLOCKED,
                "tables": [],
                "reasons": [
                    "库里没有任何带 open/high/low/close 四列的日线表（逐表核对）；"
                    "日线是打分时现取现算、算完即丢，不落库。",
                    "因此「近 250 日价格分位」「近 5 日/20 日均量」「近 10 日/前 20 日振幅」"
                    "三个维度在历史日期上**无法计算**，也无法计算任何 T+n 收益。",
                ],
                "known_facts": [
                    "运行期取数走 app/fetchers/quotes.py: daily_history()，"
                    "adjust='qfq'（前复权），列：date/open/close/high/low/volume/amount/turnover/pct_chg。",
                    "前复权价格会随后续除权除息**回溯改写** —— 将来若要落库，"
                    "必须存不复权原始 OHLC + 复权因子，不能存前复权序列。",
                ],
                "price_position_window": (cfg.get("metrics") or {}).get("price_position_days"),
                "required_history_days": (cfg.get("fetch") or {}).get("history_days"),
                "sufficient_for_price_position": False,
            }

        need_250 = int((cfg.get("metrics") or {}).get("price_position_days") or 250)
        tables = []
        for b in bars:
            st = self._bar_stats(conn, b)
            st["sufficient_for_price_position"] = (st.get("days") or 0) >= need_250
            tables.append(st)
        days = max((t.get("days") or 0) for t in tables)
        status = READY if days >= need_250 else PARTIAL
        reasons: List[str] = []
        if status != READY:
            reasons.append(
                f"最久日线仅 {days} 个交易日 < 维度4所需的 {need_250} 日，"
                "250 日价格分位在这些日期上**不可计算**（不得用短窗冒充 250 日）。")
        for t in tables:
            for r in t.get("anomaly_reasons") or []:
                reasons.append(f"{t['table']}: {r}")
        return {
            "status": status,
            "tables": tables,
            "reasons": reasons,
            "price_position_window": need_250,
            "sufficient_for_price_position": days >= need_250,
        }

    @staticmethod
    def _bar_stats(conn: sqlite3.Connection, b: Dict[str, Any]) -> Dict[str, Any]:
        """单张日线表的覆盖与质量统计。缺列就跳过对应口径，不填零。"""
        table, date_col, code_col = b["table"], b["date_col"], b["code_col"]
        out: Dict[str, Any] = {"table": table, "columns": b["columns"],
                               "adjust_fields": b["adjust_fields"]}
        if not date_col or not code_col:
            out["status"] = BLOCKED
            out["reasons"] = ["表缺日期列或证券代码列，无法按日/按券统计。"]
            return out

        T, D, C = _q(table), _q(date_col), _q(code_col)
        row = conn.execute(
            f"SELECT COUNT(*) AS rows, COUNT(DISTINCT {D}) AS days, "
            f"MIN({D}) AS first_date, MAX({D}) AS last_date, "
            f"COUNT(DISTINCT {C}) AS securities FROM {T}").fetchone()
        out.update(rows=row["rows"], days=row["days"],
                   first_date=row["first_date"], last_date=row["last_date"],
                   securities=row["securities"])

        # 每券交易日数（决定单券能否凑出 250 日窗口）
        r = conn.execute(
            f"SELECT MIN(c) AS lo, MAX(c) AS hi FROM "
            f"(SELECT {C}, COUNT(DISTINCT {D}) AS c FROM {T} GROUP BY {C})").fetchone()
        out["days_per_security"] = {"min": r["lo"], "max": r["hi"]}

        # OHLCV 缺失率
        nulls: Dict[str, Optional[float]] = {}
        for col in b["columns"]:
            lc = col.lower()
            if lc in _BAR_COLS or lc in _VOL_COLS:
                r = conn.execute(
                    f"SELECT SUM({_q(col)} IS NULL) AS n FROM {T}").fetchone()
                nulls[col] = (r["n"] / out["rows"]) if out["rows"] else None
        out["null_rates"] = nulls

        # 重复记录（同券同日多行）
        r = conn.execute(
            f"SELECT COUNT(*) AS n FROM (SELECT {C},{D},COUNT(*) AS c FROM {T} "
            f"GROUP BY {C},{D} HAVING c>1)").fetchone()
        out["duplicate_keys"] = r["n"]

        # 异常值。谓词写在字典里，避免同名字段的不同口径搅在一条三元里。
        preds: List[Tuple[str, Optional[str], Optional[str]]] = [
            ("close<=0", "close", "<= 0"),
            ("volume<0", "volume", "< 0"),
            ("amount<=0", "amount", "<= 0"),
        ]
        if "high" in b["columns"] and "low" in b["columns"]:
            preds.append(("high<low", None, None))

        anomaly: Dict[str, int] = {}
        for label, col, op in preds:
            if col is None:                       # high < low，跨列表达式
                expr = f"SUM({_q('high')} < {_q('low')})"
            elif col in b["columns"]:
                expr = f"SUM({_q(col)} {op})"
            else:
                continue
            n = conn.execute(f"SELECT {expr} AS n FROM {T}").fetchone()["n"]
            if n:
                anomaly[label] = int(n)
        out["anomalies"] = anomaly
        out["anomaly_reasons"] = [f"{k} {v} 条" for k, v in anomaly.items()]
        return out

    # ---------------------------------------------------------- 2) 资金流
    def _audit_fund_flow(self, conn: sqlite3.Connection,
                         cfg: Dict[str, Any]) -> Dict[str, Any]:
        metrics = cfg.get("metrics") or {}
        window = int(metrics.get("inflow_window_days") or 5)

        dates = [r["trade_date"] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM snapshot "
            "WHERE dataset='stock_net_inflow' ORDER BY trade_date")]
        meta = {r["trade_date"]: dict(r) for r in conn.execute(
            "SELECT trade_date, row_count, source, fetched_at FROM snapshot "
            "WHERE dataset='stock_net_inflow'")}

        if not dates:
            return {
                "status": BLOCKED,
                "reasons": ["库里没有 stock_net_inflow 快照，主力净额与大单净额历史全缺。"],
                "primary": {"status": BLOCKED, "days": 0},
                "large_order": {"status": BLOCKED, "coverage": 0.0},
            }

        # 逐日解析：覆盖券数、大单字段非空数、当日观测 vs 事后回补
        per_day: List[Dict[str, Any]] = []
        day_securities: Dict[str, set] = {}
        lo_rows = lo_nonnull = total_rows = 0
        sources: Dict[str, int] = {}
        backfilled: List[str] = []

        for d in dates:
            payload = conn.execute(
                "SELECT payload FROM snapshot WHERE dataset='stock_net_inflow' "
                "AND trade_date=?", (d,)).fetchone()
            try:
                rows = json.loads(payload["payload"])
            except (TypeError, ValueError):
                rows = []
            if not isinstance(rows, list):
                rows = []
            codes = set()
            for it in rows:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("code") or "").strip()
                if not code:
                    continue
                codes.add(code)
                total_rows += 1
                if it.get("large_order_net") is not None:
                    lo_nonnull += 1
            day_securities[d] = codes
            m = meta.get(d) or {}
            sources[str(m.get("source") or "")] = sources.get(str(m.get("source") or ""), 0) + 1
            fetched = m.get("fetched_at")
            same_day = self._is_same_day(d, fetched)
            if fetched and not same_day:
                backfilled.append(d)
            per_day.append({
                "trade_date": d,
                "securities": len(codes),
                "row_count": m.get("row_count"),
                "source": m.get("source"),
                "fetched_at": fetched,
                "observed": "same_day" if same_day else "backfilled",
            })

        # 每券覆盖天数 >= window 的证券数
        counts: Dict[str, int] = {}
        for d in dates:
            for c in day_securities[d]:
                counts[c] = counts.get(c, 0) + 1
        full_window = sum(1 for v in counts.values() if v >= window)

        # 冷启动判定用**相对峰值**而不是硬编码绝对只数：数据规模会随时间
        # 变化，写死一个 1000 之类的经验值既对小库失真，也会在市场扩容后悄悄
        # 改变结论。低于峰值一半即视为该日覆盖不完整。
        peak = max((p["securities"] for p in per_day), default=0)
        thin = [p["trade_date"] for p in per_day
                if peak and p["securities"] < peak * 0.5]

        # 三档：凑不出窗口 -> BLOCKED；能凑但有冷启动日或事后回补 -> PARTIAL；
        # 两者都没有才算 READY。**PARTIAL 不会因为「能算」而自动升格。**
        if len(dates) < window or full_window == 0:
            primary_status = BLOCKED
        elif thin or backfilled:
            primary_status = PARTIAL
        else:
            primary_status = READY

        primary_reasons: List[str] = []
        if len(dates) < window:
            primary_reasons.append(
                f"仅 {len(dates)} 个交易日 < 所需 {window} 日窗口，一个完整窗口都凑不出。")
        if full_window == 0:
            primary_reasons.append(f"没有任何证券凑满 {window} 日观测。")
        if thin:
            primary_reasons.append(
                f"覆盖不完整：{'、'.join(thin[:6])} 的证券数不足峰值({peak})的一半，"
                "这些日期的连续性子证据缺失。")
        if backfilled:
            primary_reasons.append(
                f"{len(backfilled)} 天是事后回补（fetched_at 晚于信号日），"
                "与当日观测值口径不同，需按 observed 分组统计。")

        lo_status = BLOCKED
        lo_reasons = ["大单净额只有东财 push2 提供，本网络不可用；"
                      "存量 stock_net_inflow 里 large_order_net 全为 NULL。"]
        coverage = (lo_nonnull / total_rows) if total_rows else 0.0
        if coverage > 0:
            lo_status = PARTIAL if coverage < 0.9 else READY
            lo_reasons = [f"大单净额覆盖率 {coverage:.1%}，未达 100%。"]

        return {
            "status": worst(primary_status, lo_status),
            "reasons": primary_reasons + lo_reasons,
            "primary": {
                "status": primary_status,
                "dataset": "stock_net_inflow",
                "days": len(dates),
                "first_date": dates[0],
                "last_date": dates[-1],
                "window": window,
                "securities_total": len(counts),
                "securities_full_window": full_window,
                "per_day": per_day,
                "sources": sources,
                "backfilled_dates": backfilled,
                "provenance_rule": "fetched_at 当日(UTC+8)对齐 trade_date 为观测，晚于即回补",
                "reasons": primary_reasons,
            },
            "large_order": {
                "status": lo_status,
                "dataset": "stock_net_inflow.large_order_net",
                "coverage": coverage,
                "non_null": lo_nonnull,
                "total": total_rows,
                "reasons": lo_reasons,
            },
        }

    @staticmethod
    def _is_same_day(trade_date: str, fetched_at: Optional[str]) -> bool:
        """fetched_at 是 UTC ISO，转本地(UTC+8)后与 trade_date 同日才算当日观测。"""
        if not fetched_at:
            return False
        try:
            dt = datetime.fromisoformat(str(fetched_at).replace("Z", "+00:00"))
        except ValueError:
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(timezone(timedelta(hours=8)))
        return local.strftime("%Y%m%d") == str(trade_date)

    # ---------------------------------------------------------- 3) 股票池
    def _audit_universe(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        has_table = any(
            t.startswith("universe_snapshot") for t in _tables(conn))
        dates = [r["trade_date"] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM snapshot WHERE dataset='main_force' "
            "ORDER BY trade_date")]
        codes: set = set()
        with_list = with_industry = 0
        rows_total = 0
        for d in dates:
            payload = conn.execute(
                "SELECT payload FROM snapshot WHERE dataset='main_force' "
                "AND trade_date=?", (d,)).fetchone()
            try:
                obj = json.loads(payload["payload"])[0]
            except (TypeError, ValueError, IndexError):
                continue
            for it in obj.get("all_rows") or []:
                if not isinstance(it, dict):
                    continue
                rows_total += 1
                code = str(it.get("code") or "").strip()
                if code:
                    codes.add(code)
                if it.get("list_date"):
                    with_list += 1
                if it.get("industry"):
                    with_industry += 1

        reasons = []
        if not dates:
            reasons.append("没有任何 main_force 快照，拿不到当日池成员。")
        reasons.append(
            "股票池成员只在**信号日当天**存在（来自当日快照），"
            "退市/停牌/ST 都是当日名称与当日行情推断，**没有历史状态表**。")
        if codes:
            reasons.append(
                "当前股票池回看 —— 已退市股票不会出现在任何一天的快照里，"
                "**存在幸存者偏差**，不得标记为严格历史回测。")

        status = PARTIAL if dates and codes else BLOCKED
        return {
            "status": status,
            "reasons": reasons,
            "has_daily_snapshot_table": has_table,
            "signal_days": len(dates),
            "first_date": dates[0] if dates else None,
            "last_date": dates[-1] if dates else None,
            "codes_seen": len(codes),
            "rows": rows_total,
            "with_list_date": with_list,
            "with_industry": with_industry,
            "delisting_date": BLOCKED,
            "suspension_history": BLOCKED,
            "historical_st_status": BLOCKED,
            "historical_membership": READY if has_table else BLOCKED,
            "note": "未建每日股票池快照表时，历史成员不可还原；"
                    "阶段 2 新增 universe_snapshot 供未来逐日积累。",
        }

    # ---------------------------------------------------------- 4) 基准
    #: `_load_bench` 认的表名线索与指数字面量 —— **审计与引擎同一套判据**：
    #: 审计说库里有，引擎就真取得到；两边各写一套，迟早一个说有一个说没有。
    IDX_NAME_HINTS = ("index", "idx", "benchmark")
    IDX_CODES = ("sh000300", "000300")

    @classmethod
    def _index_series(cls, conn: sqlite3.Connection,
                      bars: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """库里那条沪深300 收盘序列；没有可读的指数行就回 None（**不猜**）。"""
        for t in [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'")]:
            if not any(k in t.lower() for k in cls.IDX_NAME_HINTS):
                continue
            b = next((x for x in bars if x["table"] == t), None)
            if not b or not b["date_col"] or not b["code_col"]:
                continue
            cols = {c.lower(): c for c in b["columns"]}
            if "close" not in cols:
                continue
            D, C, K = _q(b["date_col"]), _q(b["code_col"]), _q(cols["close"])
            row = conn.execute(
                f"SELECT COUNT(*) AS rows, COUNT(DISTINCT {D}) AS days, "
                f"MIN({D}) AS first_date, MAX({D}) AS last_date FROM {_q(t)} "
                f"WHERE {C} IN (?, ?) AND {K} IS NOT NULL",
                cls.IDX_CODES).fetchone()
            if row["rows"]:
                return {"table": t, "rows": row["rows"], "days": row["days"],
                        "first_date": row["first_date"],
                        "last_date": row["last_date"]}
        return None

    @classmethod
    def _bar_last(cls, conn: sqlite3.Connection,
                  bars: List[Dict[str, Any]]) -> Optional[str]:
        """个股日线的最晚日期（指数表不算）—— 判指数序列落没落后要用它比。"""
        last: Optional[str] = None
        for b in bars:
            if any(k in b["table"].lower() for k in cls.IDX_NAME_HINTS):
                continue
            if not b["date_col"]:
                continue
            v = conn.execute(
                f"SELECT MAX({_q(b['date_col'])}) FROM {_q(b['table'])}"
            ).fetchone()[0]
            if v and (last is None or str(v) > str(last)):
                last = v
        return last

    def _audit_benchmark(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        """沪深300 是否**在库里**，判据与 `research/engine._load_bench` 一字不差。

        2026-10-30 修：早先这里是「有任意日线表就算落库（persisted=True）」——
        那只在指数恰好也落了库时才碰巧对；个股回填一到位，它就会把「没基准」
        误报成「有基准」，于是每个信号日都判 READY、超额收益的留空理由跟着错。
        现在直接查表里有没有 sh000300/000300 的行，有就是有、没有就是没有。
        """
        bars = discover_bar_tables(conn)
        idx = self._index_series(conn, bars)
        common: Dict[str, Any] = {
            "index": "沪深300 (sh000300)",
            "industry_index": BLOCKED,
            "industry_note": "sector_flow_industry 只有 latest 一天且是资金流，不是指数行情。",
        }
        if idx is not None:
            reasons = [
                f"指数表 {idx['table']} 落了 sh000300 共 {idx['days']} 个交易日"
                f"（{idx['first_date']} ~ {idx['last_date']}），"
                "engine._load_bench 可直接读 —— 覆盖到的信号日能算超额收益。"
            ]
            status = READY
            bar_last = self._bar_last(conn, bars)
            if bar_last and str(idx["last_date"]) < str(bar_last):
                status = PARTIAL
                reasons.append(
                    f"指数序列止于 {idx['last_date']}，落后个股日线最晚 {bar_last} —— "
                    "这区间内的信号日算不出超额收益（留空，不填 0）。")
            return {**common, "status": status, "reasons": reasons,
                    "persisted": True, "has_index_table": True, **idx}
        if not bars:
            return {
                "status": PARTIAL,
                "reasons": [
                    "沪深300 未落库 —— 运行期走 metrics/track.py 的"
                    "ak.stock_zh_index_daily('sh000300')（新浪源），"
                    "**进程内缓存 _INDEX，不持久化**。",
                    "后果：回测时现取的指数序列无法保证与信号日当时一致，"
                    "超额收益只能标 PARTIAL，基准缺失时**超额收益留空，不得填 0**。",
                ],
                "persisted": False,
                "has_index_table": False,
                **common,
            }
        return {
            "status": PARTIAL,
            "reasons": [
                f"库里有日线表 {'、'.join(b['table'] for b in bars)}，但没有任何含 "
                "sh000300/000300 的指数行（表名须含 index/idx/benchmark，与 "
                "engine._load_bench 同判据）—— **超额收益留空，不得填 0**；"
                "指数只能运行期现取（metrics/track.py 新浪源、进程内缓存），"
                "无法保证与信号日当时一致。",
            ],
            "persisted": False,
            "has_index_table": False,
            **common,
        }

    # ---------------------------------------------------------- 5) 打分快照
    def _audit_snapshots(self, conn: sqlite3.Connection) -> Dict[str, Any]:
        rows = list(conn.execute(
            "SELECT trade_date, row_count, source, fetched_at FROM snapshot "
            "WHERE dataset='main_force' ORDER BY trade_date"))
        if not rows:
            return {"status": BLOCKED, "days": 0,
                    "reasons": ["没有 main_force 快照，信号都不存在，无从回测。"],
                    "replay_ready": False}

        weight_sets: Dict[str, int] = {}
        degraded: Dict[str, int] = {}
        total_weights: Dict[str, int] = {}
        params_keys: List[str] = []
        for r in rows:
            payload = conn.execute(
                "SELECT payload FROM snapshot WHERE dataset='main_force' "
                "AND trade_date=?", (r["trade_date"],)).fetchone()
            try:
                obj = json.loads(payload["payload"])[0]
            except (TypeError, ValueError, IndexError):
                continue
            params = obj.get("params") or {}
            model = obj.get("model") or {}
            w = json.dumps(params.get("weights"), sort_keys=True, ensure_ascii=False)
            weight_sets[w] = weight_sets.get(w, 0) + 1
            deg = tuple(model.get("degraded") or ())
            degraded[str(deg)] = degraded.get(str(deg), 0) + 1
            tw = str(model.get("total_weight"))
            total_weights[tw] = total_weights.get(tw, 0) + 1
            if not params_keys:
                params_keys = sorted(params.keys())

        # 权重一致即「可重放」；降级导致的口径差异单独列进 reasons，但不
        # 否定重放能力本身（重放用的就是快照里记着的那套实际权重）。
        stable = len(weight_sets) <= 1
        status = READY if stable else PARTIAL
        reasons: List[str] = []
        if not stable:
            reasons.append(f"权重在 {len(weight_sets)} 套之间漂移，跨日不可直接比较。")
        if len(total_weights) > 1 or (len(total_weights) == 1 and
                                      next(iter(total_weights)) != "100"):
            reasons.append(
                f"实际 total_weight 取值 {dict(total_weights)} —— "
                "降级维度会让每日有效权重不同，统计须按 used_weights 分组。")
        if len(degraded) > 1:
            reasons.append(f"降级组合有 {len(degraded)} 种，跨日口径不一致。")

        return {
            "status": status,
            "days": len(rows),
            "first_date": rows[0]["trade_date"],
            "last_date": rows[-1]["trade_date"],
            "dates": [r["trade_date"] for r in rows],
            "replay_ready": stable,
            "weights_stable": stable,
            "weight_variants": len(weight_sets),
            "total_weights": total_weights,
            "degraded_variants": degraded,
            "params_keys": params_keys,
            "reasons": reasons or ["快照含全套 params / all_rows / model，"
                                   "**同一份快照内的排名可确定性重放**。"],
            "recompute_from_raw": BLOCKED,
            "recompute_note": "从原始行情重算某历史日的分数不可行 —— 5/6 因子依赖的日线不落库。",
        }

    # ---------------------------------------------------------- 信号日判定
    def _signal_days(self, conn: sqlite3.Connection,
                     domains: Dict[str, Any],
                     cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
        """逐个信号日回答：这天能不能上六维回测，缺什么。"""
        metrics = cfg.get("metrics") or {}
        need_inflow = int(metrics.get("inflow_window_days") or 5)
        need_bars = int(metrics.get("price_position_days") or 250)

        inflow_dates = {r[0] for r in conn.execute(
            "SELECT DISTINCT trade_date FROM snapshot "
            "WHERE dataset='stock_net_inflow'")}
        sig = domains.get("scoring_snapshots") or {}
        sig_dates = sig.get("dates") or []
        bars = domains.get("daily_bars") or {}
        bar_days = bars.get("sufficient_for_price_position")

        out: List[Dict[str, Any]] = []
        for d in sig_dates:
            prior_inflow = sum(1 for x in inflow_dates if x < d)
            missing: List[str] = []
            if prior_inflow < need_inflow:
                missing.append(f"信号日前仅 {prior_inflow} 日主力净额 < 所需 {need_inflow} 日")
            if not bar_days:
                missing.append(f"日线不落库，无法凑 {need_bars} 日价格窗口，也无法算 T+n 收益")
            if not (domains.get("benchmark") or {}).get("persisted"):
                missing.append("沪深300 未落库，超额收益无法保证与信号日对齐")
            out.append({
                "signal_date": d,
                "prior_inflow_days": prior_inflow,
                "status": READY if not missing else BLOCKED,
                "missing": missing,
            })
        return out

    # ---------------------------------------------------------- 总览
    @staticmethod
    def _overall(report: Dict[str, Any]) -> Dict[str, Any]:
        domains = report.get("domains") or {}
        if not domains:
            return {"status": BLOCKED, "strict_full_model_backtest": False,
                    "reasons": ["无可审计的数据。"]}
        statuses = {k: v.get("status", BLOCKED) for k, v in domains.items()}
        overall = worst(*statuses.values())
        signal_days = report.get("signal_days") or []
        ready_days = sum(1 for s in signal_days if s["status"] == READY)
        return {
            "status": overall,
            "domain_statuses": statuses,
            "strict_full_model_backtest": overall == READY and ready_days > 0,
            "replay_only_backtest": bool(
                (domains.get("scoring_snapshots") or {}).get("replay_ready")),
            "signal_days_total": len(signal_days),
            "signal_days_ready": ready_days,
            "reasons": [
                f"{k} = {v}" for k, v in statuses.items()
                if v != READY
            ] or ["全部数据域就绪。"],
            "next_step": "要解开 BLOCKED，唯一路径是**授权抓取并落库日线**"
                         "（不复权 OHLC + 复权因子）；内网调股票接口会被拦截并记录，"
                         "须由使用者明确授权。",
        }
