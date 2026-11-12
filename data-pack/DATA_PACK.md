# DATA PACK — 沙箱代跑回填数据（第 1 轮）

- 生成时间：2026-11-12 11:30
- 区间：20260101 .. 20261112
- 跑批记录：09:29–11:28（约 2 小时），详见 backfill_run.log / backfill_progress.json

## 本轮成果

| 数据集 | 进度 | 说明 |
|---|---|---|
| daily_bar（不复权日线 + 复权因子） | **1050 / 5225 只** · 215,466 行 | 每只完整覆盖 20260105..20261112（约 207 个交易日） |
| stock_net_inflow（逐日主力净流入） | 未开始 | bars 按顺序占满了本轮，下一轮跑 |

速度约 8.9 只/分钟（东财 https 443 被掐 → 自动落腾讯备源）。预计 ① 还需约 4 轮 2.5h、② 约 2 轮。

## 怎么导入你的本地库

```powershell
cd D:\test-kanban
git fetch origin data-pack
git checkout data-pack -- data-pack
gzip -d data-pack\market.db.gz        # 或用 7-Zip 解压
py -3 scripts\import_datapack.py data-pack\market.db          # 预览
py -3 scripts\import_datapack.py data-pack\market.db --apply  # 真写
```

导入规矩与回填工具一致：**已有行一律不覆盖**（daily_bar ON CONFLICT DO NOTHING；
stock_net_inflow 按 (date,code) 缺失格才填；kv 只并 inflow 完成清单）。

## 续跑

下次对助手说「继续跑数据」即可 —— 断点续跑判据与 replenish.py 同源
（bars = bar_span 区间覆盖，inflow = kv 完成清单），重复导入也安全。
