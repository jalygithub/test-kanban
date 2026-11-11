"""行情与资金流取数 —— 多源回退。

设计前提：东财 push2 家族会限流 / IPv6 节点异常，且**恢复时间不可控**。
因此每类数据都定义「主源 + 备源」，并用**熔断**避免对已挂的源反复重试打穿重试逻辑。

  数据              主源(东财)                        备源
  --------------    ------------------------------   --------------------------
  全市场快照        stock_zh_a_spot_em                stock_fund_flow_individual(同花顺)
  个股资金流        stock_individual_fund_flow_rank   同上（无大单分解）
  个股逐日资金流    stock_fflow_daykline(http)        —（无备源，失败就等快照自积累）
  日线历史          stock_zh_a_hist                   stock_zh_a_hist_tx(腾讯)

单位约定（各源不一致，必须归一化）：
  - 换手率：统一为**百分数**（东财给 3.84 表示 3.84%；腾讯/新浪给 0.00384 表示 0.384%）
  - 金额：统一为**元**
  - 涨跌幅：统一为**百分数**
"""
from __future__ import annotations

import logging
import re
import time
from typing import Dict, List, Optional

import pandas as pd

from app.fetchers.client import ak, call, market_of, norm_code

# 港股专线在 quotes 之前 import 也可以，但保持这里 —— 它同样依赖
# netfix.install() 已装好（见 hk.py 顶部注释）。
from app.fetchers import hk as hk_fetch  # noqa: E401

# 必须放在 client 之后：netfix.install() 已经装好限速/超时/IPv4 优先，
# 这里的 requests.get 会走同一套兜底（daykline 走不了 akshare 的 call 之外路径）。
import requests  # noqa: E402

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------- 熔断

_CIRCUIT: Dict[str, float] = {}  # 源名 -> 恢复时刻(monotonic)
_CIRCUIT_TTL = 600.0  # 挂掉后 10 分钟内不再尝试
_FAILURES: Dict[str, int] = {}

# 同花顺个股资金流是百页分页接口，单次 35-105s，一轮流水线内会被读两次
_FLOW_CACHE_TTL = 1800.0
_FLOW_CACHE: Optional[tuple] = None

# 同花顺页面被限流/拦截/劫持时，akshare 抛的是**解析类**异常而非网络异常：
# 缺 page_info -> AttributeError、某页列数不符 -> ValueError/KeyError/IndexError。
# 它们不在 with_retry 的默认 retry_on 里，于是「抓到一次坏页」直接打死整步打分。
# 页面结构本身没变（10-09、10-12 同一接口都正常返回 5200+ 行），坏页是偶发内容，
# 重抓一次就好 —— 所以这条链显式把解析类异常也纳入重试。
_FLOW_RETRY_ON = (ConnectionError, TimeoutError, OSError,
                  AttributeError, ValueError, KeyError, IndexError)


def _down(name: str) -> bool:
    return time.monotonic() < _CIRCUIT.get(name, 0.0)


def _mark_down(name: str) -> None:
    _CIRCUIT[name] = time.monotonic() + _CIRCUIT_TTL
    logger.warning("数据源熔断 %s（%d 分钟后重试）", name, int(_CIRCUIT_TTL / 60))


def _mark_up(name: str) -> None:
    _CIRCUIT.pop(name, None)
    _FAILURES.pop(name, None)


def reset_circuit() -> None:
    """每轮流水线开始时调用：复位熔断，并清掉上一轮的批量缓存。"""
    _CIRCUIT.clear()
    _FAILURES.clear()
    global _FLOW_CACHE
    _FLOW_CACHE = None
    hk_fetch.reset_circuit()   # 港股专线有自己的一套源名，一并复位


# ----------------------------------------------------------------- 解析


def _parse_cn_number(value, *, percent: bool = False) -> Optional[float]:
    """解析东财/同花顺的中文单位数值。

    支持：'2.99亿' -> 299000000、'2753.86万' -> 27538600、'20.03%' -> 20.03、
          12345.6（原样）、'-' / '' / None -> None
    """
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text or text in {"-", "--", "N/A", "nan", "None"}:
        return None

    multiplier = 1.0
    if text.endswith("亿"):
        multiplier = 1e8
        text = text[:-1]
    elif text.endswith("万"):
        multiplier = 1e4
        text = text[:-1]

    text = text.rstrip("%").rstrip("％")
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return float(match.group()) * multiplier


def _pick_column(df: pd.DataFrame, *keywords: str,
                 exclude: Optional[List[str]] = None) -> Optional[str]:
    """按关键词序列定位列名（要求全部命中），可排除含指定子串的列。

    用于兼容东财不同 indicator（今日/3日/5日/10日）下列名前缀变化。
    """
    exclude = exclude or []
    for col in df.columns:
        name = str(col)
        if all(k in name for k in keywords) and not any(x in name for x in exclude):
            return col
    return None


def _explain_ths(exc: BaseException) -> str:
    """把 akshare 在同花顺页面上炸出的解析异常翻成人话。

    这类异常不是「代码坏了」而是「抓回来的不是那张表」：页面被限流/拦截时
    `page_info` span 缺失 -> AttributeError，某一页表格列数变了 -> ValueError
    （concat 的列并集对不上写死的列名）/ KeyError / IndexError。
    """
    text = f"{type(exc).__name__}: {str(exc)[:80]}"
    if isinstance(exc, (AttributeError, ValueError, KeyError, IndexError)):
        return text + "（抓回的页面不含预期内容：限流/拦截，或上游改版）"
    return text


# ----------------------------------------------------------------- 快照


def market_snapshot() -> List[Dict]:
    """全市场快照（粗筛输入 + 盘后日线采集）。

    主源东财字段最全（含流通市值与今开/最高/最低/成交量 —— 后四个是
    `bars_sync` 转 `daily_bar` 的原料，备源同花顺没有，降级那天日线判失败）；
    备源同花顺单次调用拿到价格/涨幅/换手率/成交额，流通市值用
    `成交额 / 换手率` 反推（换手率=成交量/流通股本、成交额≈成交量×价）。
    """
    em_err: Optional[str] = None
    if _down("em_snapshot"):
        em_err = "熔断中"
    else:
        try:
            df = call("全市场快照(东财)", ak.stock_zh_a_spot_em)
            _mark_up("em_snapshot")
            return _snapshot_from_em(df)
        except Exception as exc:  # noqa: BLE001
            _FAILURES["em_snapshot"] = _FAILURES.get("em_snapshot", 0) + 1
            em_err = f"{type(exc).__name__}: {str(exc)[:80]}"
            logger.warning("东财快照失败(%d): %s", _FAILURES["em_snapshot"], exc)
            if _FAILURES["em_snapshot"] >= 2:
                _mark_down("em_snapshot")

    logger.info("改用同花顺快照（含资金流字段）")
    try:
        rows = fund_flow_individual()
    except Exception as exc:  # noqa: BLE001
        # 备源也挂 = 两源全灭。不能把 akshare 的原始异常直接抛上去：
        # 「'NoneType' object has no attribute 'text'」看不出所以然，这里连主源的
        # 原因一起带回，job_run.detail 里就能自解释（快照没了就真的没法打分）。
        raise RuntimeError(
            f"全市场快照两源皆挂：东财 {em_err or '-'}；同花顺 {_explain_ths(exc)}"
        ) from exc
    for row in rows:
        turnover = row.get("turnover")
        amount = row.get("amount")
        # 流通市值 ≈ 成交额 / 换手率（换手率需转为小数）
        # 推导：换手率 = 成交量/流通股本，成交额 ≈ 成交量×价
        #       => 流通股本 ≈ 成交额/(价×换手率) => 流通市值 ≈ 成交额/换手率
        if turnover and amount and turnover > 0:
            row["float_mv"] = round(amount / (turnover / 100.0), 0)
        else:
            row["float_mv"] = None
        name = str(row.get("name") or "")
        row["is_st"] = "ST" in name.upper() or "退" in name
        row["source"] = "10jqka"
    return rows


def _snapshot_from_em(df: pd.DataFrame) -> List[Dict]:
    out = []
    price_col = "最新价" if "最新价" in df.columns else None
    for _, r in df.iterrows():
        name = str(r.get("名称", ""))
        price = _parse_cn_number(r.get(price_col)) if price_col else None
        out.append({
            "code": norm_code(r.get("代码")),
            "name": name,
            "price": price,
            "pct_chg": _parse_cn_number(r.get("涨跌幅")),
            "turnover": _parse_cn_number(r.get("换手率")),
            "float_mv": _parse_cn_number(r.get("流通市值")),
            "amount": _parse_cn_number(r.get("成交额")),
            # 日线采集（2026-11-11）：快照顺带带出 OHLCV，盘后那一步直接转
            # `daily_bar` —— 逐只拉日线要 4800 次/天，这条路 0~1 次。
            "open": _parse_cn_number(r.get("今开")),
            "high": _parse_cn_number(r.get("最高")),
            "low": _parse_cn_number(r.get("最低")),
            "volume": _parse_cn_number(r.get("成交量")),
            "is_st": "ST" in name.upper() or "退" in name,
            "source": "eastmoney",
        })
    return out


# ------------------------------------------------------- 个股资金流（批量）


def fund_flow_individual() -> List[Dict]:
    """同花顺全市场个股资金流：一次调用拿 5000+ 只（分页，约 35-105s）。

    该接口在流水线里会被「快照降级」和「资金流降级」两处读取，因此做进程内
    TTL 缓存，避免同一轮跑两遍百页分页。
    """
    global _FLOW_CACHE
    now = time.monotonic()
    if _FLOW_CACHE and now - _FLOW_CACHE[0] < _FLOW_CACHE_TTL:
        return [dict(r) for r in _FLOW_CACHE[1]]

    # base_delay 抬到 3s：坏页多半是短时限流，1s 就回头大概率还撞上同一个状态
    df = call("个股资金流(同花顺)", ak.stock_fund_flow_individual,
              retry_on=_FLOW_RETRY_ON, base_delay=3.0)
    out = []
    for _, r in df.iterrows():
        out.append({
            "code": norm_code(r.get("股票代码")),
            "name": str(r.get("股票简称", "")).strip(),
            "price": _parse_cn_number(r.get("最新价")),
            "pct_chg": _parse_cn_number(r.get("涨跌幅")),
            "turnover": _parse_cn_number(r.get("换手率")),
            "amount": _parse_cn_number(r.get("成交额")),
            "net_inflow": _parse_cn_number(r.get("净额")),
            "inflow": _parse_cn_number(r.get("流入资金")),
            "outflow": _parse_cn_number(r.get("流出资金")),
            "source": "10jqka",
        })
    _FLOW_CACHE = (now, out)
    return [dict(r) for r in out]


def fund_flow_rank(indicator: str = "今日") -> List[Dict]:
    """东财个股资金流排行，含主力/超大单/大单分解。

    indicator: 今日 | 3日 | 5日 | 10日
    该接口是「主力资金持续流入」与「大单净额占比」两个维度的数据来源。
    东财不可用时返回空列表，由调用方降级处理。
    """
    if _down("em_fund_flow"):
        return []
    try:
        df = call(f"个股资金流排行({indicator})",
                  ak.stock_individual_fund_flow_rank, indicator=indicator)
        _mark_up("em_fund_flow")
    except Exception as exc:  # noqa: BLE001
        _FAILURES["em_fund_flow"] = _FAILURES.get("em_fund_flow", 0) + 1
        logger.warning("东财资金流排行失败(%d): %s", _FAILURES["em_fund_flow"], exc)
        if _FAILURES["em_fund_flow"] >= 2:
            _mark_down("em_fund_flow")
        return []

    tag = indicator  # 东财列名形如「今日主力净流入-净额」
    main_net = _pick_column(df, tag, "主力净流入", "净额",
                            exclude=["净占比", "万"]) or \
        _pick_column(df, "主力净流入", "净额", exclude=["净占比"])
    super_net = _pick_column(df, tag, "超大单净流入", "净额") or \
        _pick_column(df, "超大单净流入", "净额")
    large_net = _pick_column(df, tag, "大单净流入", "净额",
                             exclude=["超大单"]) or \
        _pick_column(df, "大单净流入", "净额", exclude=["超大单"])
    main_pct = _pick_column(df, tag, "主力净流入", "净占比")

    out = []
    for _, r in df.iterrows():
        out.append({
            "code": norm_code(r.get("代码")),
            "name": str(r.get("名称", "")).strip(),
            "main_net": _parse_cn_number(r.get(main_net)) if main_net else None,
            "super_net": _parse_cn_number(r.get(super_net)) if super_net else None,
            "large_net": _parse_cn_number(r.get(large_net)) if large_net else None,
            "main_pct": _parse_cn_number(r.get(main_pct)) if main_pct else None,
            "pct_chg": _parse_cn_number(
                r.get(_pick_column(df, "涨跌幅") or "涨跌幅")),
            "source": "eastmoney",
        })
    return out


# ------------------------------------------------- 个股逐日资金流（东财 daykline）

# 只有这个接口能给出**每个交易日一行**的主力净流入，fund_flow_rank 给的是
# 「今日/3日/5日/10日」累计窗口，拆不出逐日明细。
_EFFLOW_URL = "http://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
_EFFLOW_PARAMS = {
    "lmt": 0,
    "klt": 101,
    "fields1": "f1,f2,f3,f7",
    "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
    "ut": "b2884a393a59ad64002292a3e90d46a5",
}
_EFFLOW_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://data.eastmoney.com/zjlx/detail.html",
}


def _secid(code: str, market: str = "cn") -> str:
    """A 股：6/9 开头是上交所(1)，其余按东财口径归到 0（深、北通用）；
    港股：东财市场码 **116**（`116.01810`）。"""
    if market == "hk":
        return f"116.{code}"
    return f"1.{code}" if code[:1] in ("6", "9") else f"0.{code}"


def _em_fflow_json(params: Dict) -> Dict:
    resp = requests.get(_EFFLOW_URL, params=params, headers=_EFFLOW_HEADERS,
                        timeout=20)
    resp.raise_for_status()
    return resp.json()


def stock_fflow_daykline(code: str, lmt: int = 30) -> List[Dict]:
    """东财个股**逐日**主力资金流，按日期升序返回。

    每项：`{date(YYYYMMDD), net_inflow, large_order_net, net_pct}`，金额单位**元**。

    四条本机踩出来的坑，改之前先读：

    1. **必须走 `http://`**。同一台机器、同一套参数，https(443) 恒定
       `RemoteDisconnected`，http(80) 稳定 200。akshare 内置的是 https，
       所以 `ak.stock_individual_fund_flow` 在本机永远失败 —— 这也是
       「东财 push2his 被断」这个结论的真正来源（是协议端口，不是域名被封）。
    2. 字段顺序不是 akshare 文档里那套，实测为
       `[日期, 主力净额, 小单净额, 中单净额, 大单净额, 超大单净额, 主力净占比,
        小单净占比, 中单净占比]`。两个恒等式可以自检：
       `主力 = 大单 + 超大单`、`主力 + 小单 + 中单 = 0`。
    3. 限速敏感：连续打会被踢回 RemoteDisconnected，所以归到独立熔断源
       `em_fflow_hist`，由 netfix 的 1s 最小间隔兜底。
    4. **港股同接口同字段**（2026-11-07 实测）：`secid=116.01810` 一样回
       逐日 kline，字段顺序与 A 股一致，恒等式①（主力=大单+超大单）成立；
       恒等式②（主力+小单+中单=0）港股**不成立** —— 我们只取 1/4/5/6 列，
       不受影响。熔断源单独叫 `em_fflow_hist_hk`，港股挂了不牵连 A 股。
    """
    text = str(code).strip()
    if text.isdigit() and len(text) < 5:
        # 3-4 位多半是 pandas 把 000700 读成 700 的产物（client.market_of
        # 文档点名的陷阱）：先按 A 股补回 6 位再判市场，别判成港股。
        text = norm_code(text)
    market = market_of(text)
    code = norm_code(text, market)
    if not code:
        return []
    src = "em_fflow_hist_hk" if market == "hk" else "em_fflow_hist"
    if _down(src):
        return []
    try:
        js = call(f"逐日资金流({code})", _em_fflow_json,
                  {**_EFFLOW_PARAMS, "secid": _secid(code, market)})
        _mark_up(src)
    except Exception as exc:  # noqa: BLE001
        _FAILURES[src] = _FAILURES.get(src, 0) + 1
        logger.warning("东财逐日资金流失败(%s, %d): %s",
                       code, _FAILURES[src], exc)
        if _FAILURES[src] >= 4:
            _mark_down(src)
        return []

    klines = ((js.get("data") or {}).get("klines")) or []
    out: List[Dict] = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        try:
            main = float(parts[1])
            large = float(parts[4])
            super_ = float(parts[5])
            pct = float(parts[6])
        except ValueError:
            continue
        out.append({
            "date": parts[0].replace("-", ""),
            "net_inflow": main,
            # §8.3 维度 3 用「超大单+大单」。东财口径下它恒等于主力净额，
            # 存下来是为了让 _collect_inflow 里 order_net 这条退化路径能被补上。
            "large_order_net": large + super_,
            "net_pct": pct,
        })
    return out


# ----------------------------------------------------------------- 日线


def daily_history(code: str, start_date: str, end_date: str,
                  market: str = "cn") -> Optional[pd.DataFrame]:
    """个股日线（含换手率），返回列：
    date, open, close, high, low, volume, amount, turnover(百分数), pct_chg(百分数)

    `market="hk"` 走港股专线（东财 → 新浪），**熔断源名与 A 股完全隔离**，
    港股失败不会连累 A 股日线的 `em_hist`。

    A 股两个源都有熔断，且是**连续失败**计数（成功即清零）：
    只要对端还在回包，偶发超时不会误伤；一旦连续挂，立刻停手不再空耗时间。
    """
    if market == "hk":
        return hk_fetch.daily_history(code, start_date, end_date)

    if not _down("em_hist"):
        try:
            df = call(f"日线 {code}", ak.stock_zh_a_hist, symbol=code,
                      period="daily", start_date=start_date,
                      end_date=end_date, adjust="qfq")
            _mark_up("em_hist")
            return _hist_from_em(df)
        except Exception as exc:  # noqa: BLE001
            _FAILURES["em_hist"] = _FAILURES.get("em_hist", 0) + 1
            if _FAILURES["em_hist"] >= 2:
                _mark_down("em_hist")
                logger.warning("东财日线熔断，后续改用腾讯: %s", exc)

    if _down("tx_hist"):
        return None
    try:
        df = call(f"日线 {code}(腾讯)", ak.stock_zh_a_hist_tx, symbol=code,
                  start_date=start_date, end_date=end_date, adjust="qfq")
        _FAILURES["tx_hist"] = 0
        return _hist_from_tx(df)
    except Exception as exc:  # noqa: BLE001
        _FAILURES["tx_hist"] = _FAILURES.get("tx_hist", 0) + 1
        if _FAILURES["tx_hist"] >= 3:
            _mark_down("tx_hist")
            logger.warning("腾讯日线连续 %d 次失败，本轮停止日线取数: %s",
                           _FAILURES["tx_hist"], exc)
        else:
            logger.debug("腾讯日线 %s 失败(%d): %s",
                         code, _FAILURES["tx_hist"], exc)
        return None


def _hist_from_em(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    out = pd.DataFrame({
        "date": pd.to_datetime(df["日期"]).dt.strftime("%Y%m%d"),
        "open": pd.to_numeric(df["开盘"], errors="coerce"),
        "close": pd.to_numeric(df["收盘"], errors="coerce"),
        "high": pd.to_numeric(df["最高"], errors="coerce"),
        "low": pd.to_numeric(df["最低"], errors="coerce"),
        "volume": pd.to_numeric(df["成交量"], errors="coerce"),
        "amount": pd.to_numeric(df["成交额"], errors="coerce"),
        # 东财已是百分数，原样保留
        "turnover": pd.to_numeric(df["换手率"], errors="coerce"),
        "pct_chg": pd.to_numeric(df["涨跌幅"], errors="coerce"),
    })
    return out


def _hist_from_tx(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    if df is None or df.empty:
        return None
    close = pd.to_numeric(df["close"], errors="coerce")
    # 腾讯/新浪的 turnover 是**小数**（0.0049 = 0.49%），必须乘 100
    turnover = pd.to_numeric(df.get("turnover"), errors="coerce") * 100.0
    out = pd.DataFrame({
        "date": pd.to_datetime(df["date"]).dt.strftime("%Y%m%d"),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "close": close,
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce"),
        "amount": pd.to_numeric(df["amount"], errors="coerce"),
        "turnover": turnover,
    })
    # 腾讯无涨跌幅列，按收盘价推算
    out["pct_chg"] = close.pct_change() * 100.0
    return out
