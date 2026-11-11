#!/usr/bin/env bash
# 数据备份 —— 用 SQLite Online Backup API 出快照，抓取写到一半也不会拷到坏文件。
#
#   bash backup.sh                 # 立刻备份一份到 ./backup/
#   KEEP=30 bash backup.sh         # 只留最近 30 份
#
# 每天自动跑（crontab -e，root；日志直接落在部署包根目录，避开"目录还不存在"的坑）：
#   10 3 * * * cd /opt/kanban && KEEP=14 bash backup.sh >> /opt/kanban/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"

KEEP=${KEEP:-14}
STAMP=$(date +%F)
mkdir -p backup

echo "==> SQLite 快照 $(date '+%F %T')"
# 在容器内生成快照（Python 标准库自带 sqlite3，python:3.12-slim 不需要 sqlite3 CLI）
docker compose exec -T app python -c "
import sqlite3
src = sqlite3.connect('/app/backend/data/market.db')
dst = sqlite3.connect('/tmp/market-backup.db')
src.backup(dst)          # Online Backup API：拿到的是一致性快照
dst.close(); src.close()
n = sqlite3.connect('/tmp/market-backup.db').execute('select count(*) from snapshot').fetchone()[0]
w = sqlite3.connect('/tmp/market-backup.db').execute('select count(*) from watchlist').fetchone()[0]
print(f'   snapshot={n} 行  watchlist={w} 行')
"

echo "==> 取回宿主机"
docker compose cp app:/tmp/market-backup.db "backup/market-$STAMP.db"
docker compose cp app:/app/backend/data/trade_calendar.json "backup/trade_calendar-$STAMP.json" 2>/dev/null || true
docker compose exec -T app rm -f /tmp/market-backup.db

echo "==> 只留最近 $KEEP 份"
ls -1t backup/market-*.db         2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t backup/trade_calendar-*.json 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "==> 当前备份："
ls -lh backup | tail -n +2
