# -*- coding: utf-8 -*-
"""沙箱代跑（2026-11-12）—— 在助手侧的沙箱里跑历史回填，跑完打 data pack 推仓库。

背景：用户的本地机器跑 ① 日线 + ② 资金流全历史要 8-13 小时；沙箱网络没有
内网拦截问题，可以代跑。数据落**沙箱本地** `backend/data/market.db`，跑完由
`make_datapack` 打包 + `scripts/import_datapack.py` 合并进用户机器的库
（ON CONFLICT DO NOTHING / 已有行不覆盖，与回填工具同一条规矩）。

用法：
    python3 scripts/sandbox_backfill.py --budget-sec 9000

- **时间预算制**：到点在票边界停，已落库的都保留；下次再跑**自动续跑**
  （bars 判据 = `bar_span` 区间覆盖，inflow 判据 = kv 完成清单，
  与 `app/pipeline/replenish.py` 完全同源）。
- 进度每次落 `backend/data/backfill_progress.json` —— 进程被杀也只丢当前票。
- 名单：`listing.get_listing_info()`（交易所官网，约 5400 只）。库冷启动
  时 `load_scope("all")` 是空的，不能用它。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.fetchers import listing as listing_fetch          # noqa: E402
from app.fetchers import quotes                            # noqa: E402
from app.metrics import mainforce                          # noqa: E402
from app.pipeline import backfill as B                     # noqa: E402
from app.pipeline import factor as F                       # noqa: E402
from app.store import db                                   # noqa: E402

PROGRESS_PATH = BACKEND / "data" / "backfill_progress.json"
LOG_PATH = BACKEND / "data" / "backfill_run.log"

#: 与 replenish.py 同一个 kv 键（完成清单跟着 data pack 走，用户侧合并后
#: 他自己的补录任务也会跳过这些票）
INFLOW_DONE_KEY = "replenish_inflow_done"

CHUNK = 25          # bars 每批票数（进度粒度）
INFLOW_BATCH = 100  # inflow 每批合并一次落库


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_progress() -> dict:
    if PROGRESS_PATH.exists():
        try:
            return json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"runs": [], "bars": {"rows": 0, "codes_done": 0},
            "inflow": {"rows": 0, "codes_done": 0}}


def save_progress(p: dict) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(
        json.dumps(p, ensure_ascii=False, indent=1), encoding="utf-8")


def universe() -> list:
    info = listing_fetch.get_listing_info(refresh=True)
    return sorted(info.keys())


def run_bars(codes, start, end, deadline, prog) -> None:
    """日线 + 复权因子。判据：bar_span 已盖住 [start, end] 的票零请求跳过。"""
    todo = []
    for c in codes:
        span = db.bar_span("daily_bar", c)
        if span and span[0] <= start and span[1] >= end:
            continue
        todo.append(c)
    log("bars：名单 %d 只，待跑 %d 只（已覆盖 %d 跳过）"
        % (len(codes), len(todo), len(codes) - len(todo)))
    st = prog["bars"]
    for off in range(0, len(todo), CHUNK):
        if time.monotonic() > deadline:
            log("bars：时间预算到，停在 %d/%d" % (off, len(todo)))
            return
        chunk = todo[off:off + CHUNK]
        try:
            res = B.backfill("daily_bar", start, end, items=chunk,
                             budget=len(chunk) * 2)
            st["rows"] += int(res.get("inserted") or 0)
        except Exception as exc:  # noqa: BLE01  整批挂了记下继续下一批
            log("bars 批异常（%s）：%s: %s" % (chunk[:3], type(exc).__name__, exc))
        for c in chunk:
            if time.monotonic() > deadline:
                break
            try:
                F.backfill_factors(c)
            except Exception:  # noqa: BLE01  因子失败不连累
                pass
        st["codes_done"] += len(chunk)
        save_progress(prog)
        log("bars %d/%d 只 · 累计 %d 行 · %.0fmin"
            % (st["codes_done"], len(todo), st["rows"],
               (time.monotonic() - prog["_t0"]) / 60))
    log("bars 完成")


def run_inflow(codes, start, end, deadline, prog) -> None:
    """逐日主力净流入全历史。判据：kv 完成清单；只补 (code, date) 缺失格。"""
    done = set(json.loads(db.kv_get(INFLOW_DONE_KEY) or "[]"))
    todo = [c for c in codes if c not in done]
    log("inflow：名单 %d 只，待跑 %d 只（已完成 %d 跳过）"
        % (len(codes), len(todo), len(done)))
    st = prog["inflow"]
    for off in range(0, len(todo), INFLOW_BATCH):
        if time.monotonic() > deadline:
            log("inflow：时间预算到，停在 %d/%d" % (off, len(todo)))
            return
        batch = todo[off:off + INFLOW_BATCH]
        patches = {}
        for c in batch:
            if time.monotonic() > deadline:
                break
            try:
                rows = quotes.stock_fflow_daykline(c)
            except Exception as exc:  # noqa: BLE01
                log("inflow %s 失败：%s" % (c, exc))
                continue
            for r in rows:
                if start <= r["date"] <= end:
                    patches.setdefault(r["date"], {})[c] = {
                        "code": c, "net_inflow": r.get("net_inflow"),
                        "large_order_net": r.get("large_order_net"),
                        "net_pct": r.get("net_pct")}
        for date, patch in patches.items():
            old = db.load("stock_net_inflow", date) or []
            by_code = {r.get("code"): r for r in old}
            filled = 0
            for c, row in patch.items():
                if c in by_code:
                    continue
                by_code[c] = row
                filled += 1
            if filled:
                db.save("stock_net_inflow", date, list(by_code.values()),
                        source="mixed" if old else "eastmoney")
                st["rows"] += filled
        done.update(batch)
        db.kv_set(INFLOW_DONE_KEY, json.dumps(sorted(done), ensure_ascii=False))
        st["codes_done"] = len(done)
        save_progress(prog)
        log("inflow %d/%d 只 · 补格 %d · %.0fmin"
            % (st["codes_done"], len(codes), st["rows"],
               (time.monotonic() - prog["_t0"]) / 60))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-sec", type=int, default=9000)
    ap.add_argument("--start", default="20260101")
    ap.add_argument("--end", default=datetime.now().strftime("%Y%m%d"))
    args = ap.parse_args()

    prog = load_progress()
    prog["_t0"] = time.monotonic()
    prog["runs"].append({"started": datetime.now().isoformat(timespec="seconds"),
                         "budget_sec": args.budget_sec,
                         "start": args.start, "end": args.end})
    deadline = time.monotonic() + args.budget_sec
    log("开跑：预算 %.0f 分钟，区间 %s..%s" % (args.budget_sec / 60,
                                            args.start, args.end))
    try:
        codes = universe()
        log("名单 %d 只" % len(codes))
        if not codes:
            log("名单为空（交易所接口没通？）—— 收工")
            return 1
        save_progress(prog)
        run_bars(codes, args.start, args.end, deadline, prog)
        if time.monotonic() < deadline:
            run_inflow(codes, args.start, args.end, deadline, prog)
        else:
            log("预算已尽，inflow 留给下一轮")
    finally:
        prog["runs"][-1]["finished"] = datetime.now().isoformat(timespec="seconds")
        prog.pop("_t0", None)
        save_progress(prog)
        cov = db.bars_coverage("daily_bar")
        log("收尾：daily_bar %s 行 / %s 只 / %s..%s" % (
            cov.get("rows"), cov.get("codes"),
            cov.get("first_date"), cov.get("last_date")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
