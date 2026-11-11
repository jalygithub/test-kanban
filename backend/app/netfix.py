"""网络修复层 —— 必须在 import akshare 之前调用 install()。

解决三个已实测确认的问题（详见 docs/02-数据源与网络排查.md）：

1. IPv6 坏节点
   push2.eastmoney.com 的 IPv6 节点（240e:e1:...）TCP 能建连但 HTTP 层被 RST。
   Python 的 create_connection 只在「TCP 建连失败」时才换下一个地址，HTTP 层
   被重置不会触发切换，因此请求直接失败、无法自动降级到 IPv4。
   -> 修法：getaddrinfo 排序为 IPv4 优先。不删除 IPv6，因为 push2ex 等域名的
      IPv6 是正常的，一刀切禁用会引入新问题。

2. 限流
   高频请求会导致 push2 家族返回 RemoteDisconnected（同花顺、新浪不受影响）。
   -> 修法：全局限速，保证任意两次 HTTP 请求间隔 >= min_interval。

3. 读超时只管「单次 recv」，挡不住滴流响应
   requests 的 timeout=(connect, read) 里，read 是**每次 recv** 的上限而非
   整次请求的上限；urllib3 2.x 也只在建连时 settimeout 一次，之后整条连接沿用。
   服务端只要每次 recv 都在 read 秒内回哪怕 1 字节，超时就永不触发 ——
   Content-Length 没读满就一直等下去，请求永久挂起，盘后定时任务再也醒不过来。
   -> 修法：给每次请求挂一个绝对截止时刻，recv/recv_into 之前把套接字超时收紧到
      剩余预算，预算耗尽直接抛 socket.timeout。收紧动作落在 settimeout 上而不是
      改调用方式，SSL 的 C 层 SSL_read（不经 Python 的 recv）才能靠 SO_RCVTIMEO 生效。
"""

from __future__ import annotations

import logging
import random
import socket
import ssl
import threading
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

_installed = False
_orig_getaddrinfo = socket.getaddrinfo
_lock = threading.Lock()
# 按 (scheme, host) 记录下次允许发起请求的时刻
_next_allowed_at: dict = {}

# 东财 push2 家族对高频最敏感（实测被限流后返回 RemoteDisconnected），
# 其余源（同花顺 / 新浪 / 腾讯）可以跑得更快。
# 注意：同花顺资金流、新浪快照都是百页级分页接口，间隔调大会直接拖慢整个盘后任务，
# 所以「放慢」只重压在会封号的东财上，普通源靠随机抖动打散节奏即可。
DEFAULT_INTERVAL = 0.35
SENSITIVE_INTERVAL = 1.5  # 敏感源的最小间隔（取 max(默认, 该值)）
DEFAULT_JITTER = 0.5  # 普通源每次再随机等 0~0.5s
SENSITIVE_JITTER = 2.0  # 敏感源每次再随机等 0~2s
SENSITIVE_SUFFIXES = (".push2.eastmoney.com", "push2his.eastmoney.com")

# (connect, read) 秒。akshare 不传 timeout，缺省兜底在这里补。
# read=30 是给同花顺/新浪百页分页的慢响应留余量，单页拿到即返回，不会拖慢正常路径。
# 注意它只约束「单次 recv」，整次请求的上限靠下面的 TOTAL_TIMEOUT。
DEFAULT_TIMEOUT = (5.05, 30.0)

# 单次请求的**总时长**硬上限（秒）。与上面的 read timeout 是两回事。
# 取值依据：_m4_full.log 里最慢的单次成功请求约 35s（腾讯日线偶发 23~35s，
# 常态 1.7s），120s 留了 3 倍余量。误杀的代价只是这一路失败、由 fetchers 的
# 多源回退接管；而挂死是不可恢复的，会把 16:30 的盘后任务永久卡住。
TOTAL_TIMEOUT = 120.0


def _pace_for(key: str) -> tuple:
    """按 host 后缀匹配，返回该域名的 (最小间隔秒, 抖动上限秒)。

    实际每次请求要等的时长 = 最小间隔 + random(0, 抖动上限)，
    也就是普通源 0.35~0.85s、东财 push2 家族 1.5~3.5s。
    """
    _, _, host = key.partition("|")
    for suffix in SENSITIVE_SUFFIXES:
        if host == suffix.lstrip(".") or host.endswith(suffix):
            return max(_default_interval, _sensitive_interval), _sensitive_jitter
    return _default_interval, _default_jitter


def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):
    """IPv4 优先排序；保留 IPv6 作为回退项。"""
    result = _orig_getaddrinfo(host, port, family, type, proto, flags)
    if not result:
        return result
    result = list(result)
    result.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
    return result


# ------------------------------------------------------------ 总时长闸门

# 每线程记录「本次请求的绝对截止时刻」（time.monotonic）。
# 用线程局部是因为 requests 是同步阻塞调用，一次请求从建连到收完 body
# 始终在同一个线程里；别的线程（含 uvicorn 的事件循环）读不到值，天然不受影响。
_DEADLINE = threading.local()
_guard_installed = False


def _guarded_read(orig, name: str):
    """给套接字读操作套上总时长闸门。

    只在有活动截止时刻、且套接字是阻塞模式时才介入；两者都不满足就原样放行，
    因此对同进程里 uvicorn 的非阻塞套接字零影响。
    """

    def wrapper(self, *args, **kwargs):
        deadline = getattr(_DEADLINE, "value", None)
        if deadline is None:
            return orig(self, *args, **kwargs)

        old = self.gettimeout()
        if old == 0:
            # 非阻塞套接字（asyncio 事件循环）完全不碰
            return orig(self, *args, **kwargs)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout(f"netfix: 请求总时长预算已耗尽（{TOTAL_TIMEOUT:g}s）")

        if old is None or old > remaining:
            # 收紧到剩余预算。SSL 的 C 层 SSL_read 不经过 Python 的 recv，
            # 只能靠套接字上的 SO_RCVTIMEO 生效，所以必须改 settimeout 而不是改调用。
            self.settimeout(max(remaining, 0.05))
            try:
                return orig(self, *args, **kwargs)
            finally:
                self.settimeout(old)
        return orig(self, *args, **kwargs)

    wrapper.__name__ = f"_deadline_guarded_{name}"
    return wrapper


def _install_deadline_guard() -> None:
    """给 recv / recv_into 补上总时长闸门（可重复调用）。

    socket.socket 的 recv 来自 _socket C 基类，得直接在类上 shadow；
    ssl.SSLSocket 自己重写了 recv_into，要单独补一份，否则 HTTPS 走的是它那份。
    """
    global _guard_installed
    if _guard_installed:
        return
    for cls in (socket.socket, ssl.SSLSocket):
        for meth in ("recv", "recv_into"):
            if meth in cls.__dict__:
                orig = cls.__dict__[meth]
            elif cls is socket.socket:
                orig = getattr(socket.socket, meth)
            else:
                continue  # 继承自 socket.socket，那边已经补过了
            setattr(cls, meth, _guarded_read(orig, meth))
    _guard_installed = True
    logger.debug("已安装总时长闸门 recv/recv_into 上限=%.1fs", TOTAL_TIMEOUT)


def _throttled_request(self, method, url, **kwargs):
    """包装 requests.Session.request，补上超时 + 按域名强制最小请求间隔 + 总时长闸门。

    三件事都必须在这里做，因为 akshare 绝大多数接口**不传 timeout**：
    一旦对端建连后不回包（实测腾讯行情接口会这样），不设超时就会永久挂起，
    盘后任务卡死且没有任何日志。限速则按域分桶 —— 东财 push2 敏感，
    同花顺/新浪的百页分页接口若被全局 1s 连坐，会把整个任务拖慢百倍。

    间隔 = 桶内最小间隔 + 随机抖动（普通 0~0.5s，东财 push2 0~2s）：
    均匀等间距是最容易被识别成机器请求的节奏，抖动既打散节奏，
    又不动「同域名两次请求至少间隔 N 秒」这条硬下限。

    总时长闸门必须包住整次调用：stream=False 时 resp.content 是在
    Session.request 内部读完的，所以建连 + 收头 + 收体三段都在这里覆盖得到。
    """
    if kwargs.get("timeout") is None:
        kwargs["timeout"] = DEFAULT_TIMEOUT

    if _default_interval > 0:
        try:
            scheme, _, host = url.partition("://")
            key = f"{scheme}|{host.split('/')[0]}"
        except Exception:  # noqa: BLE001
            key = "||"
        base, jitter_max = _pace_for(key)
        with _lock:
            # 抢占式排队：本请求占住 [slot, slot+间隔+抖动) 这一段，
            # 同域名的后续请求只能排到更后面 —— 下限由间隔保证，抖动只负责打散节奏
            now = time.monotonic()
            allowed_at = _next_allowed_at.get(key, 0.0)
            slot = allowed_at if allowed_at > now else now
            pace = base + (random.uniform(0.0, jitter_max) if jitter_max > 0 else 0.0)
            _next_allowed_at[key] = slot + pace
            wait = slot - now
        if wait > 0:
            # 睡在锁外：等东财那 1.5~3.5 秒时，不能把新浪/腾讯的线程也一起按住
            time.sleep(wait)

    prev = getattr(_DEADLINE, "value", None)
    deadline = time.monotonic() + TOTAL_TIMEOUT
    if prev is not None:
        deadline = min(deadline, prev)  # 嵌套请求取更紧的那个
    _DEADLINE.value = deadline
    try:
        return _orig_session_request(self, method, url, **kwargs)
    finally:
        _DEADLINE.value = prev  # 必须还原，线程池会复用线程


_orig_session_request = None
_default_interval = 0.0
_sensitive_interval = SENSITIVE_INTERVAL
_default_jitter = DEFAULT_JITTER
_sensitive_jitter = SENSITIVE_JITTER


def install(
    min_interval: float = DEFAULT_INTERVAL,
    sensitive_interval: float = SENSITIVE_INTERVAL,
    jitter: float = DEFAULT_JITTER,
    sensitive_jitter: float = SENSITIVE_JITTER,
    ipv4_first: bool = True,
    total_timeout: Optional[float] = None,
) -> None:
    """安装网络修复（IPv4 优先 + 默认超时 + 分级限速 + 总时长闸门）。重复调用无副作用。"""
    global _installed, _orig_session_request, _default_interval, TOTAL_TIMEOUT
    global _sensitive_interval, _default_jitter, _sensitive_jitter
    if _installed:
        return

    if total_timeout is not None:
        TOTAL_TIMEOUT = float(total_timeout)

    if ipv4_first:
        socket.getaddrinfo = _ipv4_first
        logger.debug("已安装 IPv4 优先解析")

    # 即使 min_interval=0 也要包装：超时兜底与总时长闸门不能关
    import requests

    _orig_session_request = requests.Session.request
    requests.Session.request = _throttled_request
    _default_interval = min_interval
    _sensitive_interval = max(min_interval, sensitive_interval)
    _default_jitter = max(0.0, jitter)
    _sensitive_jitter = max(0.0, sensitive_jitter)
    _install_deadline_guard()
    logger.debug(
        "已安装超时兜底 %s + 总时长闸门 %.1fs + 分级限速 普通=%.2fs(±%.1fs) 东财push2=%.2fs(±%.1fs)",
        DEFAULT_TIMEOUT, TOTAL_TIMEOUT,
        min_interval, _default_jitter, _sensitive_interval, _sensitive_jitter,
    )

    _installed = True


# ---------------------------------------------------------------- 重试


def with_retry(
    fn: Callable,
    *args,
    retries: int = 3,
    base_delay: float = 1.0,
    retry_on: Iterable[type] = (ConnectionError, TimeoutError, OSError),
    label: str = "",
    **kwargs,
):
    """对「网络类」异常做指数退避重试。

    注意：akshare 内部多数接口已有自己的重试，这里只兜底真正打穿的场景，
    所以默认只重试 2 次额外尝试，避免整体等待过久。
    """
    retry_on_t = tuple(retry_on)
    last_exc: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except retry_on_t as exc:  # 网络类错误才重试
            last_exc = exc
            if attempt == retries - 1:
                break
            delay = base_delay * (2**attempt) + random.uniform(0.2, 0.8)
            logger.warning(
                "%s 第 %d 次失败（%s），%.1fs 后重试",
                label or getattr(fn, "__name__", "call"),
                attempt + 1,
                type(exc).__name__,
                delay,
            )
            time.sleep(delay)
    raise last_exc
