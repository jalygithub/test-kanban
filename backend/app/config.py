"""应用配置。"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BACKEND_DIR / "data"


class Settings(BaseSettings):
    # HTTP
    request_interval: float = 0.35  # 普通源请求最小间隔（秒），按域名分桶
    # 东财 push2/push2his 家族实测最容易封（RemoteDisconnected），单独放慢
    request_sensitive_interval: float = 1.5
    # 随机抖动：每次请求在最小间隔之上再随机等 0~jitter 秒。
    # 固定等间距是最像机器的节奏，抖动既打散节奏又不破坏「最小间隔」这个下限。
    request_jitter: float = 0.5  # 普通源
    request_sensitive_jitter: float = 2.0  # 敏感源：实际间隔落在 1.5~3.5s
    ipv4_first: bool = True  # IPv4 优先，绕开 push2 的 IPv6 坏节点
    fetch_retries: int = 3
    # 单次 HTTP 请求总时长硬上限（秒）。read timeout 只管单次 recv，挡不住滴流响应，
    # 这个才是「整次请求最久等多久」的兜底，防止 16:30 的盘后任务被永久卡死。
    request_total_timeout: float = 120.0

    # 存储
    db_path: Path = DATA_DIR / "market.db"

    # 调度
    fetch_hour: int = 16
    fetch_minute: int = 30

    # ---- 订阅制（docs/06）----
    # 交易日盘中估值推送的触发时刻。**14:50 拿不到当日净值**（收盘后才更新），
    # 推的是实时估值。
    push_hour: int = 14
    push_minute: int = 50
    # 单个订阅用户的关注基金数上限。subscriber.fund_limit 为空时用这个全局值。
    fund_watch_limit: int = 10
    # 订阅时长（天），从 paid_at 起算，过期自动降级为未订阅。
    pro_days: int = 30
    # 占位支付链接的定价。**只是记账数字** —— 占位链接本身收不到钱，
    # 人工开通（`开通 <open_id> [金额]`）时可覆盖。
    pro_price: float = 99.0
    # ---- 收银台（docs/06 §2）----
    # 可选的订阅月数档位。金额 = pro_price × 月数（线性，不打折）。
    pay_months: str = "1,3,6,12"
    # 收银台链接的**对外地址前缀**。飞书消息里的链接是绝对地址 —— 手机上
    # 点开必须能连到这个地址，所以买服务器后要改成公网域名（并把 nginx 的
    # basic_auth 从 /pay/ 路径上摘掉，否则用户会被口令框挡在门外）。
    pay_base_url: str = "http://localhost:5173"
    # 收银台链接有效期（天）。过期后页面回「链接已失效」，重新发「订阅Pro」拿新的。
    pay_link_days: int = 30

    # 打分模型
    scorer_config: Path = BACKEND_DIR / "config" / "scorer.yaml"

    # 运行期产物目录（日志、服务注册表）。在仓库根，不在 backend/ 下。
    run_dir: Path = BACKEND_DIR.parent / "run"

    # 逐日资金流回补（外网）
    # 关掉后**任何路径都不再发外网资金流请求**：/main-force/inflow 的后台回补线程、
    # 飞书推送成功后的关注股回补、飞书「关注列表」前台现查（缺数先补再返回）。
    # 内网下东财全程 RST 时回补永远 fetched=0 却仍每 10 分钟重试一轮，这批请求会
    # 被安全设备拦截并记录；有网时保持默认 true。改法：backend/.env 写
    # STOCK_INFLOW_BACKFILL=false，重启后端。
    inflow_backfill: bool = True

    # 数据保留（天）。数据管理页展示并可覆盖，后端按 DATASET_FLOOR 硬下限兜底 ——
    # 低于代码实际需要的窗口会直接破坏矩阵回看、日期下拉与打分窗口，所以不放行。
    retention_main_force_days: int = 365    # 矩阵 days 最大 365
    retention_inflow_days: int = 60         # 打分只要 5 天（inflow_window_days），留余量
    retention_pool_days: int = 60           # 日期下拉 list_dates() 默认 limit=30

    # 运行期产物：日志与任务记录
    log_max_bytes: int = 20 * 1024 * 1024   # 启动时轮转阈值（start_all.py 读）
    job_run_keep: int = 200                 # job_run 最多留多少行

    # 演示数据兜底
    enable_demo_fallback: bool = True

    class Config:
        env_prefix = "STOCK_"
        env_file = BACKEND_DIR / ".env"


settings = Settings()

DATA_DIR.mkdir(parents=True, exist_ok=True)
