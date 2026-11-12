# -*- coding: utf-8 -*-
"""data pack 导入（2026-11-12）—— 把沙箱代跑的回填数据合并进本地 market.db。

与回填工具**同一条规矩**：已有行一律不覆盖 ——
- `daily_bar`：走 `db.bars_upsert`（ON CONFLICT DO NOTHING）
- `stock_net_inflow`：按 (date, code) 缺失格才填，盘后当场采的快照优先
- `kv`：只合并 inflow 完成清单（并集），其余键不动

用法：
    python3 scripts/import_datapack.py data-pack/market.db             # 预览（默认）
    python3 scripts/import_datapack.py data-pack/market.db --apply    # 真写

预览与实跑算的是同一批行 —— 看完预览再 --apply，差值就是这段时间新落的。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.store import db                                   # noqa: E402

INFLOW_DONE_KEY = "replenish_inflow_done"


def merge_bars(pack: sqlite3.Connection, apply: bool) -> dict:
    rows = pack.execute(
        "SELECT * FROM daily_bar").fetchall()
    cols = [d[0] for d in pack.execute("SELECT * FROM daily_bar LIMIT 0").description]
    out = {"pack_rows": len(rows), "inserted": 0}
    if apply:
        dicts = [dict(zip(cols, r)) for r in rows]
        for i in range(0, len(dicts), 2000):
            out["inserted"] += db.bars_upsert("daily_bar", dicts[i:i + 2000])
    return out


def merge_inflow(pack: sqlite3.Connection, apply: bool) -> dict:
    out = {"pack_rows": 0, "inserted": 0, "dates": 0}
    cur = pack.execute(
        "SELECT trade_date, payload FROM snapshot WHERE dataset=?",
        ("stock_net_inflow",))
    for trade_date, payload in cur.fetchall():
        rows = json.loads(payload)
        if not isinstance(rows, list):
            continue
        out["pack_rows"] += len(rows)
        out["dates"] += 1
        old = db.load("stock_net_inflow", trade_date) or []
        by_code = {r.get("code"): r for r in old}
        filled = 0
        for r in rows:
            c = r.get("code")
            if c in by_code:
                continue          # 本地已有（多为盘后当场采的快照），不覆盖
            by_code[c] = r
            filled += 1
        out["inserted"] += filled
        if apply and filled:
            db.save("stock_net_inflow", trade_date, list(by_code.values()),
                    source="mixed" if old else "eastmoney")
    return out


def merge_kv(pack: sqlite3.Connection, apply: bool) -> dict:
    out = {"merged": 0}
    row = pack.execute("SELECT value FROM kv WHERE key=?",
                       (INFLOW_DONE_KEY,)).fetchone()
    if not row:
        return out
    pack_done = set(json.loads(row[0] or "[]"))
    local = set(json.loads(db.kv_get(INFLOW_DONE_KEY) or "[]"))
    union = sorted(pack_done | local)
    out["merged"] = len(pack_done - local)
    if apply:
        db.kv_set(INFLOW_DONE_KEY, json.dumps(union, ensure_ascii=False))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pack", help="data pack 里的 market.db 路径")
    ap.add_argument("--apply", action="store_true", help="真写（默认只预览）")
    args = ap.parse_args()

    pack_path = Path(args.pack)
    if not pack_path.exists():
        print(f"找不到 {pack_path}")
        return 1
    pack = sqlite3.connect(str(pack_path))
    mode = "写入" if args.apply else "预览"
    print(f"=== data pack 导入（{mode}）：{pack_path} ===")
    bars = merge_bars(pack, args.apply)
    print("daily_bar：包内 %d 行 → %s %d 行"
          % (bars["pack_rows"], "新插入" if args.apply else "预计新增", bars["inserted"]))
    inflow = merge_inflow(pack, args.apply)
    print("stock_net_inflow：%d 天 / 包内 %d 行 → %s %d 行"
          % (inflow["dates"], inflow["pack_rows"],
             "新插入" if args.apply else "预计新增", inflow["inserted"]))
    kv = merge_kv(pack, args.apply)
    print("inflow 完成清单：并入 %d 只" % kv["merged"])
    if not args.apply:
        print("（以上是预览 —— 加 --apply 才真写）")
    else:
        cov = db.bars_coverage("daily_bar")
        print("完成。本地 daily_bar：%s 行 / %s 只 / %s..%s"
              % (cov.get("rows"), cov.get("codes"),
                 cov.get("first_date"), cov.get("last_date")))
    pack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
