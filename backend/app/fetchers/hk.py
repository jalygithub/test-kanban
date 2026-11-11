"""港股取数 —— P0 只做「日线 + 恒生指数基准 + 名称/现价」。

**为什么单开一个模块，而不是塞进 `quotes.py`**：

1. 港股没有涨跌停/连板概念，板块资金流、逐日大单也完全是另一套接口
   （见 `docs/04-backlog.md` 第 1 条的 P1/P2），塞进去只会把 A 股那条
   「东财 → 腾讯」回退链越搅越浑。
2. **熔断名必须隔离**。`quotes.py` 里 `em_hist` 熔断 10 分钟是给 A 股日线
   用的；港股如果复用同一个名字，一次港股失败就能把 A 股日线也一起掐掉。
   所以这里全部用 `*_hk_*` 的独立源名。

2026-09-30 在公司网实测的源可用性（重要，决定了回退顺序）：

    数据      主源                                   备源
    ------    ------------------------------------   ----------------------------
    日线      东财 stock_hk_hist   ❌ 被 RST         新浪 stock_hk_daily   ✅ 通
    指数      —                                        新浪 index_daily_sina ✅ 通
    名称/价   —                                        腾讯 qt.gtimg.cn      ✅ 通

东财那条与 A 股同因被断（公司网按域名在 TLS 握手期 RST），**热点/服务器上是
通的**，所以仍然放主源；新浪这条是当前公司网下唯一能跑通的日线源，
**验收就是靠它**。

⚠ 不用 `ak.stock_hk_spot()` 查名称：它要翻 99 页、约 100 秒，
  而腾讯 `qt.gtimg.cn/q=hkXXXX` 单次约 50ms 就能拿到名称+现价。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import pandas as pd

# 必须先 import client：netfix.install() 要在任何网络请求之前装好，
# 而且下面的 requests 要走同一套分域限速与 IPv4 优先兜底。
from app.fetchers.client import ak, call, norm_code  # noqa: F401

import requests  # noqa: E402

logger = logging.getLogger(__name__)

__all__ = ["daily_history", "index_daily", "quote", "reset_circuit"]


# ----------------------------------------------------------------- 熔断
#
# 形状与 quotes.py 一致，但**独立一份**（理由见模块文档第 2 条）。
# TTL 沿用 10 分钟：封禁是按域名的，盲目重试只是白耗连接。

_CIRCUIT: Dict[str, float] = {}   # 源名 -> 恢复时刻(monotonic)
_CIRCUIT_TTL = 600.0
_FAILURES: Dict[str, int] = {}


def _down(name: str) -> bool:
    return time.monotonic() < _CIRCUIT.get(name, 0.0)


def _mark_up(name: str) -> None:
    _CIRCUIT.pop(name, None)
    _FAILURES.pop(name, None)


def _mark_down(name: str) -> None:
    _CIRCUIT[name] = time.monotonic() + _CIRCUIT_TTL
    logger.warning("港股数据源熔断 %s（%d 分钟后重试）",
                   name, int(_CIRCUIT_TTL / 60))


def reset_circuit() -> None:
    """每轮流水线开始时由 `quotes.reset_circuit()` 代为复位。"""
    _CIRCUIT.clear()
    _FAILURES.clear()


def _fail(name: str, exc: Exception, threshold: int = 2) -> None:
    """连续失败计数，到阈值才熔断（偶发超时不误伤）。"""
    _FAILURES[name] = _FAILURES.get(name, 0) + 1
    if _FAILURES[name] >= threshold:
        _mark_down(name)
        logger.warning("港股源 %s 连续失败 %d 次，熔断: %s",
                       name, _FAILURES[name], exc)


# ----------------------------------------------------------------- 解析


def _num(value) -> Optional[float]:
    try:
        out = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """取列；缺失时给一列 NaN，不让整条日线因为少一列就整体失败。"""
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(float("nan"), index=df.index)


# ----------------------------------------------------------------- 日线


def daily_history(code: str, start_date: str,
                  end_date: str) -> Optional[pd.DataFrame]:
    """港股日线（前复权），返回列与 A 股完全一致：

    date, open, close, high, low, volume, amount, turnover(百分数), pct_chg(百分数)

    两条源各自熔断、**连续失败**才计数（成功即清零）。
    """
    code = norm_code(code, "hk")
    if not code:
        return None

    if not _down("em_hk_hist"):
        try:
            df = call(f"港股日线 {code}", ak.stock_hk_hist, symbol=code,
                      period="daily", start_date=start_date,
                      end_date=end_date, adjust="qfq")
            _mark_up("em_hk_hist")
            out = _hist_from_em(df)
            if out is not None and not out.empty:
                return out
            logger.warning("东财港股日线 %s 无有效行，改用新浪", code)
        except Exception as exc:  # noqa: BLE001
            _fail("em_hk_hist", exc)

    if _down("sina_hk_hist"):
        return None
    try:
        # 新浪不支持起止日期，一次拿全量再本地截断
        df = call(f"港股日线 {code}(新浪)", ak.stock_hk_daily,
                  symbol=code, adjust="qfq")
        _mark_up("sina_hk_hist")
        return _hist_from_sina(df, start_date, end_date)
    except Exception as exc:  # noqa: BLE001
        _fail("sina_hk_hist", exc, threshold=3)
        return None


def _hist_from_em(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """东财港股日线。列名与 A 股同源（日期/开盘/收盘/…/换手率/涨跌幅），
    但**逐列防御式取值**：哪一列对不上只降级该列，不整条挂掉。
    """
    if df is None or df.empty:
        return None
    if "日期" not in df.columns or "收盘" not in df.columns:
        logger.warning("东财港股日线列名不符: %s", list(df.columns))
        return None

    close = pd.to_numeric(df["收盘"], errors="coerce")
    pct = _col(df, "涨跌幅")
    if pct.isna().all():
        # 少了涨跌幅列就自己算（东财给的是百分数）
        pct = close.pct_change() * 100.0

    return pd.DataFrame({
        "date": pd.to_datetime(df["日期"]).dt.strftime("%Y%m%d"),
        "open": _col(df, "开盘"),
        "close": close,
        "high": _col(df, "最高"),
        "low": _col(df, "最低"),
        "volume": _col(df, "成交量"),
        "amount": _col(df, "成交额"),
        "turnover": _col(df, "换手率"),
        "pct_chg": pct,
    })


def _hist_from_sina(df: pd.DataFrame, start_date: str,
                    end_date: str) -> Optional[pd.DataFrame]:
    """新浪港股日线：列 date/open/high/low/close/volume/amount，**没有换手率**
    （该列给 NaN，前端显示 `-`），也没有涨跌幅（按收盘推算）。

    两个必须注意的点：
    1. `date` 是 `2026-09-28` 这种带连字符的字符串，要先转成 `YYYYMMDD`
       才能与调用方传进来的 8 位日期比较；
    2. **先在全序列上算涨跌幅再截断** —— 反过来的话截断后的第一根 K 线
       没有前收，涨跌幅会是 NaN。
    """
    if df is None or df.empty:
        return None
    if "date" not in df.columns or "close" not in df.columns:
        logger.warning("新浪港股日线列名不符: %s", list(df.columns))
        return None

    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"]).dt.strftime("%Y%m%d"),
        "open": pd.to_numeric(df.get("open"), errors="coerce"),
        "high": pd.to_numeric(df.get("high"), errors="coerce"),
        "low": pd.to_numeric(df.get("low"), errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df.get("volume"), errors="coerce"),
        "amount": pd.to_numeric(df.get("amount"), errors="coerce"),
        # 新浪没有换手率，留 NaN 让前端显示 '-'（与 A 股腾讯备源同款降级）
        "turnover": float("nan"),
    })
    out["pct_chg"] = out["close"].pct_change() * 100.0

    win = out[(out["date"] >= start_date) & (out["date"] <= end_date)]
    return win.reset_index(drop=True) if not win.empty else None


# ----------------------------------------------------------------- 恒生指数


def index_daily() -> pd.DataFrame:
    """恒生指数日收盘，列 `date(YYYYMMDD)` / `close`。

    新浪源（东财的 `stock_hk_index_daily_em` 在公司网同样被 RST）。
    失败直接抛给调用方 —— 基准拿不到必须降级，绝不影响个股数据。
    """
    raw = call("恒生指数(新浪)", ak.stock_hk_index_daily_sina, symbol="HSI")
    if raw is None or raw.empty or "close" not in raw.columns:
        raise RuntimeError("恒生指数返回空")
    out = pd.DataFrame({
        "date": pd.to_datetime(raw["date"]).dt.strftime("%Y%m%d"),
        "close": pd.to_numeric(raw["close"], errors="coerce"),
    })
    out = out.dropna().reset_index(drop=True)
    if out.empty:
        raise RuntimeError("恒生指数无有效行")
    return out


# ------------------------------------------------------------- 名称 / 现价

_QT_URL = "http://qt.gtimg.cn/q=hk{code}"


def _qt_text(code: str) -> str:
    resp = requests.get(_QT_URL.format(code=code), timeout=15)
    resp.raise_for_status()
    resp.encoding = "gbk"   # 腾讯这个接口是 GBK
    return resp.text


def quote(code: str) -> Dict[str, Any]:
    """腾讯单股行情：名称 + 现价 + 涨跌幅（约 50ms，一次请求）。

    字段形如 `v_hk00700="100~腾讯控股~00700~432.000~439.800~439.400~…"`：
    1=名称 2=代码 3=最新价 4=昨收 5=今开。前缀 `v_hk00700="` 只污染第 0 段。

    用途：用户手动关注港股时没打名字，用它补齐。
    拿不到就返回 `{}`，调用方用代码兜底 —— **绝不因为查不到名字而关注失败**。
    """
    code = norm_code(code, "hk")
    if not code:
        return {}
    if _down("tx_hk_quote"):
        return {}
    try:
        text = call(f"港股快照 {code}", _qt_text, code)
        _mark_up("tx_hk_quote")
    except Exception as exc:  # noqa: BLE001
        _fail("tx_hk_quote", exc, threshold=3)
        return {}

    lines = (text or "").strip().splitlines()
    if not lines:
        return {}
    parts = lines[0].split("~")
    if len(parts) < 5:
        return {}
    price, prev = _num(parts[3]), _num(parts[4])
    pct = round((price / prev - 1.0) * 100.0, 2) if price and prev else None
    return {
        "code": code,
        "name": parts[1].strip(),
        "price": price,
        "pct_chg": pct,
    }
