#!/usr/bin/env bash
# 出站连通性预检 —— 香港节点**部署前/部署后各跑一次**。
# 只做 TLS + HTTP 只读探测，不写库、不触发抓取、不改任何东西。
#
#   bash probe.sh
#
# 为什么必须跑：东财对机房 IP 的友好度未知（家里 IP 是"节奏慢"，机房 IP 常被直接
# 拒连），而板块资金流**没有**腾讯回退 —— 东财不通 = 板块时间轴半瘫，这决定了
# 后面要不要做 Tushare 改造。
set -euo pipefail
cd "$(dirname "$0")"

echo "== 出站连通性预检（从容器内发出，等同于抓取时的真实路径）=="
docker compose exec -T app python - <<'PY'
import time
import urllib.request

TARGETS = [
    ("东财-行情",    "https://push2.eastmoney.com/api/qt/stock/get?secid=1.600519&fields=f43&fltt=2"),
    ("东财-历史K线", "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600519&klt=101&fqt=1&lmt=2&fields1=f1&fields2=f51,f53"),
    ("东财-数据中心", "https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD&pageSize=1&pageNumber=1&columns=ALL"),
    ("腾讯-行情",    "https://qt.gtimg.cn/q=sh600519"),
    ("飞书-入站",    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"),
]

ok = 0
for name, url in TARGETS:
    t = time.time()
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Connection": "close",
        })
        with urllib.request.urlopen(req, timeout=12) as r:
            body = r.read(80)
            mark = "✅" if r.status in (200, 400, 404) else "⚠️ "
            ok += r.status in (200, 400, 404)
            print(f"  {mark} {name:<14} HTTP {r.status}  {int((time.time()-t)*1000):>4}ms  {body[:40]!r}")
    except Exception as exc:  # noqa: BLE001 探测器只需如实报告
        print(f"  ❌ {name:<14} {type(exc).__name__}: {str(exc)[:90]}  {int((time.time()-t)*1000)}ms")

print(f"\n  通过 {ok}/{len(TARGETS)}")
print("  全通 = 可以直接跑「轻跑」验收；东财那三条挂了 = 先看是 IP 被限还是域名被拦，")
print("  再决定是否放慢限速（compose 里 STOCK_REQUEST_SENSITIVE_*）或推进数据源改造。")
PY
