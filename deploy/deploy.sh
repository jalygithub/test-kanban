#!/usr/bin/env bash
# A股看板 · 服务器端一键部署
#   把整个 kanban-deploy-YYYYMMDD/ 目录传到服务器任意位置（如 /opt/kanban）后：
#       bash deploy.sh
# 幂等：重复执行不会重复 load 镜像、不会重建数据。
set -euo pipefail
cd "$(dirname "$0")"

echo "== 1/4 加载镜像 =="
IMAGES="kanban-backend:1.0 nginx:1.27-alpine"
missing=0
for img in $IMAGES; do
  docker image inspect "$img" >/dev/null 2>&1 || missing=1
done
if [ "$missing" = "1" ]; then
  # 这个 tar 里同时装着后端镜像和 nginx:1.27-alpine —— 服务器**不需要联网 pull**
  # （国内/香港服务器配 Docker Hub 加速源是笔糊涂账，离线带上最省事）
  docker load -i kanban-images-1.0.tar
fi
for img in $IMAGES; do
  if ! docker image inspect "$img" >/dev/null 2>&1; then
    echo "   ❌ 缺镜像 $img —— kanban-images-1.0.tar 没传上来或没 load 成功" >&2
    exit 1
  fi
done
echo "   镜像齐了：$(echo $IMAGES)"

echo "== 2/4 起服务 =="
docker compose up -d

echo "== 3/4 时区自检（必须是 CST +0800）=="
docker compose exec app date

echo "== 4/4 鉴权自检 =="
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1/api/health || true)
echo "   不带口令 GET /api/health -> $code"
if [ "$code" = "401" ]; then
  echo "   ✅ 鉴权生效"
else
  echo "   ⚠️  期望 401，实际 $code —— 检查 nginx/default.conf 的 auth_basic 与 .htpasswd 是否存在"
fi

echo
echo "下一步："
echo "  1. 浏览器打开 http://<服务器IP>/   （会弹口令框，用 pack 时生成的账号口令）"
echo "  2. 看启动日志：docker compose logs -f app"
echo "     应看到「盘后调度已按上次的设置保持关闭」—— 定时任务按你的设置保持关掉"
echo "  3. 飞书发「状态」验证入站：定时任务页的「已处理 N 条」应该 +1"
