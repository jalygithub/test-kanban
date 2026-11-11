# -*- coding: utf-8 -*-
"""历史日线回填跑批（数据管线阶段 4/5 的执行入口）。

## 为什么是脚本而不是 HTTP 端点

`POST /api/pipeline/backfill` 同样能跑，但跑批时需要：分批、失败熔断、进度
落日志、随时 Ctrl+C 再续。这些放进 FastAPI 请求里要么超时、要么随进程退出
丢状态。脚本跑批 + `bar_span` 断点续跑才是这个量级该有的形态 —— 端点留给
「人点一下、跑一小段」。

**后端不必在跑**，本脚本直接写库（用可写连接）。反过来这也是为什么跑批时
**别同时开着后端做采集**：SQLite 没开 WAL（`docs/04 §11.3`），跨进程同时写
同一个文件会抢锁。

## 熔断：封 IP 时最该有的东西

单票失败只是数据问题；**整批失败是被封的症状**。后者还继续打，只会越封越狠，
白跑几小时还污染日志。所以两条停机判据：

1. **单批失败率**超过 `--max-batch-fail-rate`（默认 0.5）→ 立刻停。
2. **累计失败率**超过 `--max-fail-rate`（默认 0.25）→ 停。

停机时会打印「已经跑到哪、怎么续」，而不是只甩一句失败。两源（东财主源、
腾讯备源）的失败计数**分开摊出来**（`_log_sources`）—— 阈值不变，但「谁在拒」
不许只能靠猜。

## 优雅暂停与断点续跑

Ctrl+C **第一次** = 优雅暂停：当前票收完就退（批末再确认一次），落库的一行
都不少；**第二次** = 立即走人。价格靠 `bar_span`（区间盖住 `[start, end]` 才
算完成）；因子只补 `adj_factor IS NULL` 的行。两者都幂等 ——
**中断后原样再跑一次就是恢复**。

`--gaps` 只补缺口：逐票比对已有交易日，中间断档的段单独重试（东财被拦的
那几天），一天不缺就一次请求都不发。

跑法（默认全市场 × 拍板区间 20260801 起）::

    backend\\.venv\\Scripts\\python.exe scripts\\backfill_run.py
    backend\\.venv\\Scripts\\python.exe scripts\\backfill_run.py --scope=mf
    backend\\.venv\\Scripts\\python.exe scripts\\backfill_run.py --start 20251016 --batch 50
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.pipeline import backfill as B                            # noqa: E402
from app.pipeline import factor as F                              # noqa: E402
from app.store import db                                          # noqa: E402

LOG_DIR = Path(__file__).resolve().parents[1] / "run"
LOG_PATH = LOG_DIR / "backfill.log"

_LOG_FP = None

# 控制台是 GBK 时，🛑/✋ 这类 emoji 会 UnicodeEncodeError。文件才是权威副本，
# 控制台乱就乱，但**不许因为一个字符把跑批掀翻**（阶段 5 修的就是这个）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")     # type: ignore[attr-defined]
except Exception:  # noqa: BLE001  老版本/只读 stdout：不折腾，下面还有兜底
    pass


def _safe_print(line: str) -> None:
    """打印一行；控制台编码装不下就**换编码硬打**，绝不抛。"""
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        print(line.encode(enc, "replace").decode(enc), flush=True)


def log(msg: str) -> None:
    """一行一条，带时间戳，同时落日志文件和控制台。

    落文件不是仪式：跑批要 1~4 小时，中途出了问题回头看的唯一线索就是它。
    中文控制台回显可能乱码，所以文件才是权威副本 —— **先落文件再打印**：
    以前 print 排在写文件前面，GBK 控制台撞上 🛑 就 UnicodeEncodeError，于
    是「熔断停机」那行既进不了日志、还把整个跑批掀成退出码 1（阶段 5 修）。
    """
    line = f"{time.strftime('%H:%M:%S')} {msg}"
    global _LOG_FP
    if _LOG_FP is None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_FP = LOG_PATH.open("a", encoding="utf-8")
    _LOG_FP.write(line + "\n")
    _LOG_FP.flush()
    _safe_print(line)


#: 暂停请求。第一次 Ctrl+C = 优雅暂停（当前票收完就退），第二次 = 立即走人。
_PAUSE = {"asked": 0}


def _on_sigint(signum, frame):  # noqa: ANN001, ARG001
    _PAUSE["asked"] += 1
    if _PAUSE["asked"] >= 2:
        # 第二次还按 = 「等不下去了」，恢复默认行为，让它该炸就炸
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        raise KeyboardInterrupt
    log("✋ 暂停请求收到 —— 当前票收完就退；再按一次 Ctrl+C 立即退出")


# ------------------------------------------------------------------ 名单
#
# 判据在 `backfill.load_scope` —— 页面看进度用的是同一份。脚本这里不再自持
# 一份：两边各写一遍，「到底该跑多少只」就会有两个数，而进度条对不上时没
# 人说得清哪个才是对的。


def load_scope(scope: str) -> List[str]:
    """按范围取股票名单（零联网）。见 `app.pipeline.backfill.load_scope`。"""
    return B.load_scope(scope)


def default_start() -> str:
    """缺省起始日 = 用户拍板的 `DEFAULT_START`（20260801）。

    不再用「一年前」：那是拍板之前随手写的，会比定好的区间早半年 —— 多发
    几千次请求去补一段没人要的历史，还会把熔断概率一起抬上去。
    """
    return B.DEFAULT_START


# ------------------------------------------------------------------ 主流程


def run(scope: str, start: str, end: str, batch: int,
        max_batch_fail_rate: float, max_fail_rate: float,
        with_factor: bool, dry_run: bool, gaps: bool = False) -> Dict[str, Any]:
    codes = load_scope(scope)
    if not codes:
        log(f"范围 {scope} 名单为空 —— 库里没有可回填的股票名单")
        return {"ok": False, "reason": "名单为空"}

    # plan 与 backfill 同一套判据（only_gaps 原样透传）—— 计划说跑几只，
    # 执行就跑几只，两个口径不许分叉。
    pl = B.plan("daily_bar", start, end, codes, only_gaps=gaps)
    log(f"范围={scope} {start}..{end}  名单 {len(codes)} 只 | "
        f"待跑 {pl['to_fetch']} 只 | 已覆盖跳过 {pl['already_done']} 只"
        + (f" | 中间断档 {pl['hole_codes']} 只（--gaps 才会补）"
           if pl.get("hole_codes") else ""))
    if not with_factor:
        log("⚠ 未补复权因子：本轮收益将全部判为 UNADJUSTED_UNCERTAIN")
    if pl["to_fetch"] == 0 and not with_factor and not pl.get("hole_codes"):
        return {"ok": True, "reason": "区间已覆盖，无需重跑"}

    todo = codes if with_factor else [c for c in codes]
    total = len(todo)
    done = inserted = failed = factor_updated = 0
    fail_items: List[Dict[str, Any]] = []
    #: 源 → {items, rows, inserted, failed}。熔断时要看得出是哪个源在拒 ——
    #: 熔断阈值本身不变（50%/25%），但「谁在拒」不许只能靠猜。
    src_counts: Dict[str, Dict[str, int]] = {}
    t0 = time.time()

    if dry_run:
        log(f"[dry-run] 只算不跑：将分 {(total + batch - 1) // batch} 批 × {batch} 只")
        return {"ok": True, "dry_run": True, "total": total,
                "batches": (total + batch - 1) // batch}

    try:
        for off in range(0, total, batch):
            chunk = todo[off:off + batch]
            # ---- 1) 价格 ----
            try:
                res = B.backfill("daily_bar", start, end, items=chunk,
                                 budget=len(chunk), only_gaps=gaps,
                                 # 优雅暂停：`backfill` 在**票边界**问一次，
                                 # 已落库的一行都不少（见它的 should_stop）
                                 should_stop=lambda: _PAUSE["asked"] > 0)
            except Exception as exc:  # noqa: BLE001  整批挂了要停，不往下冲
                log(f"批 {off // batch + 1} 价格阶段整体异常，停止："
                    f"{type(exc).__name__}: {exc}")
                break
            inserted += int(res.get("inserted") or 0)
            f_failed = int(res.get("failed") or 0)
            failed += f_failed
            fail_items.extend(res.get("problems") or [])
            for name, rec in (res.get("sources") or {}).items():
                agg = src_counts.setdefault(
                    name, {"items": 0, "rows": 0, "inserted": 0, "failed": 0})
                for k in agg:
                    agg[k] += int(rec.get(k) or 0)

            # ---- 2) 因子 ----
            n_factor = 0
            if with_factor:
                for c in chunk:
                    try:
                        n_factor += int(F.backfill_factors(c).get("updated") or 0)
                    except Exception as exc:  # noqa: BLE001 单票因子挂了不连累整批
                        log(f"  因子 {c} 异常：{type(exc).__name__}: {exc}")
            factor_updated += n_factor

            done += len(chunk)
            rate = f_failed / len(chunk) if chunk else 0.0
            cum = failed / done if done else 0.0
            el = time.time() - t0
            eta = (el / done * (total - done)) if done else 0.0
            log(f"批 {off // batch + 1}/{(total + batch - 1) // batch} "
                f"价格+{inserted}行 因子+{n_factor}行 | 累计 {done}/{total} "
                f"失败 {failed}({rate:.0%}) | {el / 60:.1f}min 剩约 {eta / 60:.0f}min")

            # ---- 3) 熔断 ----
            if chunk and rate > max_batch_fail_rate:
                log(f"🛑 单批失败率 {rate:.0%} > {max_batch_fail_rate:.0%} —— "
                    f"这通常是**被限流/被封**的症状，不是个别票的问题。已停。")
                _log_sources(src_counts)
                break
            if cum > max_fail_rate:
                log(f"🛑 累计失败率 {cum:.0%} > {max_fail_rate:.0%}，已停。")
                _log_sources(src_counts)
                break

            # ---- 4) 优雅暂停：批末收尾再退 ----
            if _PAUSE["asked"] > 0:
                log("✋ 已暂停。落库的部分原样保留 —— **原样再跑一次就是续跑**。")
                break
    except KeyboardInterrupt:
        log("✋ 手动中断。已落库的部分原样保留 —— **原样再跑一次就是续跑**。")
    finally:
        cov = db.bars_coverage("daily_bar")
        fac = db.bars_factor_coverage("daily_bar")
        log(f"收尾：daily_bar {cov['rows']} 行 / {cov['codes']} 只 / "
            f"{cov['first_date']}..{cov['last_date']}")
        log(f"      因子已填 {fac['with_factor']}/{fac['rows']} 行"
            f"（判不了除权 {fac['without_factor']} 行）")
        _log_sources(src_counts)
        if fail_items:
            log("失败样本（前 10 条）：")
            for p in fail_items[:10]:
                log(f"    {p.get('item')}: {p.get('error')}")

    return {"ok": True, "total": total, "done": done,
            "inserted": inserted, "failed": failed,
            "factor_updated": factor_updated,
            "sources": src_counts,
            "elapsed_min": round((time.time() - t0) / 60, 1)}


def _log_sources(src_counts: Dict[str, Dict[str, int]]) -> None:
    """把两源的失败计数摊开打出来。熔断阈值不变，但**谁在拒**要看得见。"""
    if not src_counts:
        return
    parts = [f"{k} 跑 {v['items']} 只/行 {v['rows']}/插入 {v['inserted']}"
             f"/失败 {v['failed']}" for k, v in sorted(src_counts.items())]
    log("源计数（东财主源、腾讯备源）：" + "；".join(parts))


def main() -> int:
    ap = argparse.ArgumentParser(description="历史日线回填跑批")
    ap.add_argument("--scope", default="all",
                    choices=["all", "mf", "pool", "wl"],
                    help="名单范围：all=全市场 mf=main_force并集 pool=股票池 wl=自选")
    ap.add_argument("--start", default=None,
                    help=f"起始日 YYYYMMDD，默认 {B.DEFAULT_START}（用户拍板）")
    ap.add_argument("--end", default=date.today().strftime("%Y%m%d"))
    ap.add_argument("--batch", type=int, default=100, help="每批多少只票")
    ap.add_argument("--max-batch-fail-rate", type=float, default=0.5)
    ap.add_argument("--max-fail-rate", type=float, default=0.25)
    ap.add_argument("--no-factor", action="store_true", help="不补复权因子")
    ap.add_argument("--dry-run", action="store_true", help="只算量级，不发请求")
    ap.add_argument("--gaps", action="store_true",
                    help="只补缺口：逐票比对已有交易日，中间断档的段单独重试"
                         "（东财被拦的那几天）—— 一天不缺就一次请求都不发")
    a = ap.parse_args()

    # 每次跑批重新计数：Ctrl+C 的语义按「这一次跑」算，不跨进程继承
    _PAUSE["asked"] = 0
    try:
        signal.signal(signal.SIGINT, _on_sigint)
    except (ValueError, OSError):
        pass          # 非主线程/平台不支持：退回默认 Ctrl+C（等同硬中断）

    r = run(a.scope, a.start or default_start(), a.end, a.batch,
            a.max_batch_fail_rate, a.max_fail_rate,
            with_factor=not a.no_factor, dry_run=a.dry_run, gaps=a.gaps)
    return 0 if r.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
