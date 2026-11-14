# DATA PACK — 沙箱代跑回填数据

数据只走 `data-pack` 分支（master 只放代码）。本目录 = 一份可直接导入的 data pack。

- **最新：第 2 轮** · 生成时间：2026-11-14 03:05
- 上一版：第 1 轮 · 2026-11-12 11:30

## 当前库总量（market.db.gz，第 2 轮末）

| 数据集 | 进度 | 说明 |
|---|---|---|
| daily_bar（不复权日线 + 复权因子） | **1070 / 5224 只** · **219,804 行** · 20260105..20261113（208 个交易日） | 较第 1 轮 +4,338 行；覆盖判据（bar_span）801 只已盖满目标区间 |
| stock_net_inflow（逐日主力净流入） | **未开始（0 行）** | 东财资金流源被安全设备 RST，本轮没跑成 |

约 12 只 001xxx/002xxx 次新股行数天然偏少（上市日起才有数据），属正常；
000831、002129 尾部到 1030，002388 到 1111（疑似停牌/缺尾），续跑时留意补。

## 第 2 轮跑批记录（backfill_run.log / backfill_progress.json）

- 时间：2026-11-13 16:49 – 18:05，4 次启动（沙箱重启，断点续跑，区间 20260105..20261112）
- 速度：正常段约 6 只/分（东财 RST → 腾讯备源 `stock_zh_a_hist_tx`）
- **结束原因**：18:03 起腾讯备源也开始 `ConnectionError: Remote end closed connection`（连续全失败），进程 18:05 停止——**不是时间预算耗尽，是数据源被掐**。预算还剩约 9.7 小时没用掉。
- 有效产出：约 25–50 只补缺，多数票是 bar_span 补记/查漏（已有行不覆盖）

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
stock_net_inflow 按 (date,code) 缺失格才填；kv 只并 inflow 完成清单）。重复导入安全。

## 续跑

下次对助手说「继续跑数据」即可 —— 断点续跑判据与 replenish.py 同源
（bars = bar_span 区间覆盖，inflow = kv 完成清单），重复导入也安全。

续跑要点：
- 先确认数据源通不通（东财 `*.em` 被 RST 是常态，腾讯备源也会间歇被掐）；源不通就别烧预算
- 剩余目标：bars 补齐 5224 只名单（约 4200 只未盖满）+ inflow 全量（刚开始就断）
- 跑批日志/进度文件不入 master（gitignore），只随 data pack 走

## 2026-11-14 12:49:48 轮：daily_bar 1070/5224 只 · 219856 行 · 20260105..20261113；inflow 未开始（东财 RST）
