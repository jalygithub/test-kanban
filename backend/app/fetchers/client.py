"""统一的 AkShare 调用入口。

集中做三件事：安装网络修复、限速退避、结构化日志。
所有取数都必须经过 call()，禁止在别处直接 import akshare 发请求。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from app import netfix
from app.config import settings

logger = logging.getLogger(__name__)

# 必须在首次请求前安装（IPv4 优先 + 分域限速与随机抖动 + 总时长闸门）
netfix.install(
    min_interval=settings.request_interval,
    sensitive_interval=settings.request_sensitive_interval,
    jitter=settings.request_jitter,
    sensitive_jitter=settings.request_sensitive_jitter,
    ipv4_first=settings.ipv4_first,
    total_timeout=settings.request_total_timeout,
)

import akshare as ak  # noqa: E402  必须在 netfix.install 之后

__all__ = ["ak", "call", "norm_code", "market_of"]

# 两个市场的补零规则**不能共用**：A 股恒 6 位、港股恒 5 位。
# 港股 `00700` 补成 6 位会变成 `000700` —— 那是另一个代码，接口会返回别的东西
# 或者干脆空手而归，而且和 A 股按 6 位建的索引永远对不上。
CN_LEN = 6
HK_LEN = 5


def norm_code(value, market: str = "cn") -> str:
    """证券代码归一化。

    `market="cn"`（默认）统一为 **6 位**数字字符串。

    陷阱：同花顺、深交所接口的 `股票代码` 列常被 pandas 推断成 int64，
    `000001` -> `1`、`002638` -> `2638`，直接拼接口会拿不到数据，
    而且与其它源按 `000001` 建的索引对不上。所有源的代码必须先过这里。

    `market="hk"` 统一为 **5 位**数字字符串（`00700`、`09988`）。

    **代码里写死的前缀/后缀优先于 market 参数**：`hk00700`、`00700.HK`
    无论传什么 market 都按港股归一化 —— 否则调用方漏传 market 就会把
    港股代码悄悄补成 6 位，错得无声无息。
    """
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if text.upper().endswith(".HK"):  # 00700.HK
        market, text = "hk", text[:-3]
    for prefix in ("sz", "sh", "bj", "SZ", "SH", "BJ", "hk", "HK"):
        if text.startswith(prefix) and len(text) > 2 and text[2:].isdigit():
            text = text[2:]
            if prefix.lower() == "hk":
                market = "hk"
            break
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if market == "hk":
        # 6 位及以上一律按输入错误处理：宁可返回空让调用方报 400，
        # 也不要猜它是不是把 A 股代码贴错了地方。
        return text.zfill(HK_LEN) if text.isdigit() and len(text) <= HK_LEN else ""
    if text.isdigit() and len(text) < CN_LEN:
        text = text.zfill(CN_LEN)
    return text


def market_of(value) -> str:
    """判断代码属于哪个市场：`"hk"` 或 `"cn"`。

    判据是**归一化后的位数**（A 股 6 位、港股 5 位），所以对 `hk00700`、
    `00700.HK`、`00700` 都给得出正确结果。

    ⚠ 只用于**已经成型的字符串**（路由参数、用户输入、库里存的 code）。
    **不要拿 pandas 原始值来判**：`000700` 被读成 int `700` 后，
    3 位数字会被判成港股，而它其实是 A 股 —— 那种值先过 `norm_code()`
    （market 默认 cn）补回 6 位再判。
    """
    return "hk" if norm_code(value, "hk") else "cn"


def call(label: str, fn: Callable[..., Any], *args, **kwargs) -> Any:
    """带计时、限速、退避重试的取数调用。

    `kwargs` 中与 `with_retry` 同名的（`retry_on` / `base_delay`）归重试逻辑消费，
    其余原样传给 `fn` —— `retries` 由 `settings.fetch_retries` 统一给定。
    """
    started = time.perf_counter()
    try:
        result = netfix.with_retry(
            fn,
            *args,
            retries=settings.fetch_retries,
            label=label,
            **kwargs,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - started
        logger.error("取数失败 %-34s %.2fs %s: %s",
                     label, elapsed, type(exc).__name__, str(exc)[:120])
        raise
    elapsed = time.perf_counter() - started
    rows = len(result) if hasattr(result, "__len__") else "?"
    logger.info("取数成功 %-34s %.2fs rows=%s", label, elapsed, rows)
    return result
