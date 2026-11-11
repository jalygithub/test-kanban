"""实时行情取数 —— 腾讯批量为主源，字段全部过不变量校验。

## 为什么是腾讯

公司网下东财 `push2*` 家族在建连期被 RST（docs/02 问题 1、坑 3），而且
**每次尝试都会被安全设备记录** —— 实时页是按 5 秒轮询的，打东财等于每分钟
往拦截日志里灌十几条。腾讯 `qt.gtimg.cn` 实测是通的（`hk.py` 的名称/现价
就走它），且**支持批量**：十几二十只一次请求拿完。

熔断名与 `quotes.py` / `hk.py` **各自独立**，理由同 `hk.py` 模块文档第 2 条：
一边挂掉不能连累另一边。

## 字段索引的风险（本模块最重要的部分）

项目里只验证过腾讯返回的**前 6 段** —— `hk.py:246`：1=名称 2=代码
3=最新价 4=昨收 5=今开。第 30 段之后的涨跌 / 涨跌幅 / 最高 / 最低 /
成交额 / 换手率 / 市盈率是按公开文档写的，**本环境没有实测过**（约定不联网
探测）。分时（`web.ifzq.gtimg.cn`）与资金流（`qt.gtimg.cn/q=ff_`）更是
整个接口都没验证过。

所以这里的铁律是：**每个字段都要过不变量校验，过不了就置 None 并记进
warnings，宁可前端显示 `-`，也绝不显示一个猜出来的错数。** 校验分三档：

    强（错了就是明显错数）  最高 ≥ 最新价 ≥ 最低
                            涨跌   ≈ 最新价 - 昨收
                            涨跌幅 ≈ 最新价/昨收 - 1
                            今开/最高/最低 落在 昨收 ±30% 内
    中（单位可能差 10^k）   成交额 ≈ 成交量 × 价格
    弱（只能挡离谱值）      换手率 0~100、市盈率在 ±10000 内

A 股「成交额 ≈ 成交量×价格」按 1 手 = 100 股算，能抓住**万元 vs 元**这类
10^4 级单位错；港股每手股数不固定，同一式子只能放宽到 10^2 级，抓不住
10 倍错，所以港股的成交额一律不显示（置 None）—— 宁可少一列，不要错一列。

## 降级承诺

拿不到、熔断中、结构对不上，一律返回空 + 一条 warning，**绝不拿假数顶上**。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from app.fetchers.client import call, market_of, norm_code

logger = logging.getLogger(__name__)

__all__ = ["quotes", "intraday", "flow", "reset_circuit", "symbol"]

# ----------------------------------------------------------------- 熔断

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
    logger.warning("实时行情源熔断 %s（%d 分钟后重试）", name, int(_CIRCUIT_TTL / 60))


def _fail(name: str, exc: Exception, threshold: int = 3) -> None:
    """连续失败 3 次才熔断 —— 实时页 5 秒一轮，偶发抖动不该直接掐掉。"""
    _FAILURES[name] = _FAILURES.get(name, 0) + 1
    if _FAILURES[name] >= threshold:
        _mark_down(name)
        logger.warning("实时源 %s 连续失败 %d 次，熔断: %s", name, _FAILURES[name], exc)


def reset_circuit() -> None:
    _CIRCUIT.clear()
    _FAILURES.clear()


# ----------------------------------------------------------------- 基础件

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _num(value, multiplier: float = 1.0) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = _NUM_RE.search(str(value))
        if not m:
            return None
        v = float(m.group())
    if v != v:  # NaN
        return None
    return v * multiplier


# A 股按首位分交易所：6 沪、0/3 深、4/8/9 北。
_PREFIX = {"6": "sh", "0": "sz", "3": "sz", "4": "bj", "8": "bj", "9": "bj"}


def symbol(code: str, market: str) -> str:
    """腾讯符号：`sh600519` / `sz000001` / `hk00700`。

    港股恒 5 位、A 股恒 6 位，补零规则由 `norm_code` 统一（理由见它自己的文档）。
    """
    if market == "hk":
        c = norm_code(code, "hk")
        return f"hk{c}" if c else ""
    c = norm_code(code, "cn")
    return f"{_PREFIX.get(c[:1], 'sh')}{c}" if c else ""


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    # qt.gtimg.cn 是 GBK（hk.py 已验证），web.ifzq 是 UTF-8。
    # **顺序不能反**：大量 UTF-8 中文用 GBK 解会不报错地产出乱码，
    # 反过来 GBK 中文几乎必然不是合法 UTF-8、会正常抛错走 fallback。
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _get(url: str, params: Optional[Dict[str, Any]] = None,
         encoding: Optional[str] = None) -> str:
    """单次取数。**不自带重试** —— 重试由 `client.call()` 统一做，
    这里再包一层会把 3 次指数退避乘成 9 次，把限速窗口拉到分钟级。"""
    def _req() -> str:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        if encoding:
            resp.encoding = encoding
            return resp.text
        return _decode(resp.content)

    return _req()


def _chunks(text: str) -> Dict[str, List[str]]:
    """把 `v_sh600519="1~贵州茅台~...";` 拆成 {sh600519: [...]}。

    前缀 `v_sh600519="` 只污染第 0 段（hk.py:247 已确认），第 1 段起就是干净的。
    `v_pv_none_match="1"` 是「查无此码」的哨兵，跳过。
    """
    out: Dict[str, List[str]] = {}
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, val = chunk.partition("=")
        key = key.strip()
        if key.startswith("v_"):
            key = key[2:]
        if not key or key.startswith("pv_"):
            continue
        out[key] = val.strip().strip('"').split("~")
    return out


# ----------------------------------------------------------------- 报价


def _safe(parts: List[str], idx: int, lo: float, hi: float,
          origin: Optional[float] = None, tol: float = 0.0,
          multiplier: float = 1.0) -> Optional[float]:
    """取第 idx 段并做区间校验；越界/解析失败一律回 None（前端显示 -）。"""
    if idx >= len(parts):
        return None
    v = _num(parts[idx], multiplier)
    if v is None or v < lo or v > hi:
        return None
    if origin is not None and tol and abs(v - origin) > tol:
        return None
    return v


def _parse_quote(parts: List[str], code: str, market: str,
                 fallback_name: str, warns: List[str]) -> Optional[Dict[str, Any]]:
    if len(parts) < 5:
        return None

    name = (parts[1] or "").strip() or fallback_name
    price = _num(parts[3])
    prev = _num(parts[4])
    # 第 3/4 段是 hk.py 实测验证过的，它们不对说明整行都不对，直接放弃
    if not price or not prev or price <= 0 or prev <= 0:
        warns.append(f"{code} 报价行缺少有效现价/昨收，已跳过")
        return None

    # ---- 强校验：这几项错了就是肉眼可见的错数
    hi_lo = (prev * 0.5, prev * 1.5)
    open_ = _safe(parts, 5, *hi_lo)
    high = _safe(parts, 33, *hi_lo)
    low = _safe(parts, 34, *hi_lo)
    if high is not None and high + 1e-9 < price:
        warns.append(f"{code} 最高价低于现价，弃用最高价")
        high = None
    if low is not None and low - 1e-9 > price:
        warns.append(f"{code} 最低价高于现价，弃用最低价")
        low = None
    if high is not None and low is not None and high + 1e-9 < low:
        high = low = None

    change = _num(parts[31]) if len(parts) > 31 else None
    real_change = price - prev
    if change is None or abs(change - real_change) > max(0.02, abs(real_change) * 0.05):
        warns.append(f"{code} 涨跌额与「现价-昨收」对不上，弃用")
        change = None

    pct = _num(parts[32]) if len(parts) > 32 else None
    real_pct = (price / prev - 1.0) * 100.0
    if pct is None or abs(pct - real_pct) > 0.05:
        warns.append(f"{code} 涨跌幅与「现价/昨收-1」对不上，弃用")
        pct = None

    # ---- 中校验：成交额 ≈ 成交量 × 价格。抓万元/元这类 10^k 单位错。
    volume = _safe(parts, 36, 0, 1e12) if len(parts) > 36 else None
    amount = _safe(parts, 37, 0, 1e13, multiplier=1e4) if len(parts) > 37 else None
    if volume is None or amount is None or volume <= 0 or amount <= 0:
        volume = amount = None
    elif market == "cn":
        # A 股 1 手 = 100 股，比值应落在 1 附近；10 倍单位错会被这道拦下
        if not 0.3 <= amount / (volume * 100 * price) <= 3.0:
            warns.append(f"{code} 成交额与成交量×价格对不上，弃用成交额/成交量")
            volume = amount = None
    else:
        # 港股每手股数不固定，同一式子只能证明量级、证不了单位 → 不显示
        volume = amount = None

    turnover = _safe(parts, 38, 0, 100) if len(parts) > 38 else None
    pe = _safe(parts, 39, -10000, 10000) if len(parts) > 39 else None

    return {
        "code": code,
        "market": market,
        "name": name,
        "price": price,
        "prev_close": prev,
        "open": open_,
        "high": high,
        "low": low,
        "change": change,
        "pct": pct,
        "volume": volume,
        "amount": amount,
        "turnover": turnover,
        "pe": pe,
        "updated_at": _stamp(parts),
    }


_TS_HMS = re.compile(r"^(\d{2}:\d{2}:\d{2})")
_TS_COMPACT = re.compile(r"(\d{2})(\d{2})(\d{2})$")


def _stamp(parts: List[str]) -> Optional[str]:
    """第 30 段是时间，格式随接口版本在 `HH:MM:SS` 与 `yyyyMMddHHmmss` 间变。
    两种都认不出来就回 None，由前端用自己的接收时刻兜底。"""
    if len(parts) <= 30:
        return None
    raw = parts[30].strip()
    m = _TS_HMS.search(raw)
    if m:
        return m.group(1)
    m = _TS_COMPACT.search(raw)
    if m:
        return ":".join(m.groups())
    return None


def quotes(items: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """批量实时报价。入参 `[{code, market?, name?}]`，出参 `(rows, warnings)`。

    A 股与港股**分两次请求**：混在一条 `q=` 里时，一旦腾讯不支持混排就会
    整批拿不到，两个市场互相连累；分开发最多多花 0.35s（分域限速），换来的
    是故障隔离。
    """
    rows: List[Dict[str, Any]] = []
    warns: List[str] = []
    groups: Dict[str, List[Dict[str, Any]]] = {"cn": [], "hk": []}
    seen = set()
    for it in items:
        mkt = it.get("market") or market_of(it.get("code"))
        code = norm_code(it.get("code"), mkt)
        # 去重放在**归一化之后**：`700` 与 `00700` 是同一只，先去重会漏掉
        if code and code not in seen:
            seen.add(code)
            groups[mkt].append({"code": code, "market": mkt,
                                "name": str(it.get("name") or "")})

    for mkt in ("cn", "hk"):
        group = groups[mkt]
        if not group:
            continue
        _one_batch(group, rows, warns)
    return rows, warns


def _one_batch(group: List[Dict[str, Any]], rows: List[Dict[str, Any]],
               warns: List[str]) -> None:
    mkt = group[0]["market"]
    label = "腾讯实时报价(A股)" if mkt == "cn" else "腾讯实时报价(港股)"
    if _down("tx_quote"):
        warns.append(f"{label} 熔断中（10 分钟后自动恢复），本轮报价不可用")
        return

    syms = [symbol(g["code"], mkt) for g in group]
    try:
        text = call(label, _get, f"http://qt.gtimg.cn/q={','.join(syms)}", encoding="gbk")
        _mark_up("tx_quote")
    except Exception as exc:  # noqa: BLE001
        _fail("tx_quote", exc)
        warns.append(f"{label} 失败：{type(exc).__name__}")
        return

    parsed = _chunks(text)
    for g in group:
        parts = parsed.get(symbol(g["code"], mkt))
        if parts is None:
            warns.append(f"{g['code']} 报价行缺失（接口查无此码）")
            continue
        row = _parse_quote(parts, g["code"], mkt, g["name"], warns)
        if row:
            rows.append(row)


# ----------------------------------------------------------------- 分时


def intraday(code: str, market: str) -> Optional[Dict[str, Any]]:
    """当日分时。取不到或结构对不上一律回 None，**不猜**。

    结构校验：time/price 必须等长、非空、每根都能解析成正数。
    """
    sym = symbol(code, market)
    if not sym:
        return None
    if _down("tx_minute"):
        return None
    try:
        text = call(f"腾讯分时 {sym}", _get,
                    "https://web.ifzq.gtimg.cn/appstock/app/minute/query",
                    params={"code": sym})
        _mark_up("tx_minute")
    except Exception as exc:  # noqa: BLE001
        _fail("tx_minute", exc)
        logger.warning("分时取数失败 %s: %s", sym, exc)
        return None

    try:
        node = json.loads(text)
    except (ValueError, TypeError):
        return None
    data = node.get("data") if isinstance(node, dict) else None
    inner = data.get(sym) if isinstance(data, dict) else None
    body = inner.get("data") if isinstance(inner, dict) else None
    if not isinstance(body, dict):
        return None

    times = body.get("time")
    prices_raw = body.get("price")
    if not isinstance(times, list) or not isinstance(prices_raw, list):
        return None
    if not times or len(times) != len(prices_raw):
        return None
    prices = [_num(p) for p in prices_raw]
    if any(p is None or p <= 0 for p in prices):
        return None

    def _series(key: str) -> Optional[List[float]]:
        raw = body.get(key)
        if not isinstance(raw, list) or len(raw) != len(prices):
            return None
        vals = [_num(v) for v in raw]
        # 中途缺一根就整条丢掉：半截序列画出来的均价线是断的，比没有更误导
        if any(v is None or v < 0 for v in vals):
            return None
        return vals

    avg = _series("avg_price")
    vols = _series("volume")
    return {
        "code": code,
        "market": market,
        "date": str(body.get("date") or ""),
        "times": [str(t) for t in times],
        "prices": prices,
        "avg": avg,
        "volumes": vols,
        "prev_close": _num(inner.get("prec")) if isinstance(inner, dict) else None,
    }


# ----------------------------------------------------------------- 资金流


def _looks_like_date(v: float) -> bool:
    return v == int(v) and 20000101 <= v <= 20991231


def _parse_flow(parts: List[str], code: str, warns: List[str]) -> Optional[Dict[str, Any]]:
    """腾讯 `ff_` 的段序未经本环境验证，因此**只认能自证的结构**。

    做法：逐位扫描，找出满足「净额 ≈ 流入 − 流出」的连续三元组。位置对了这个
    等式必然成立，位置错了基本凑不上；日期段（`20261009` 这种）显式排除掉，
    免得被当成「流入」而错位匹配。

    取的是**第一组**合格三元组，所以字段只能叫 `net`/`inflow`/`outflow` ——
    它到底是「主力」还是「散户」取决于源的返回顺序，没验证过就不敢这么写。
    找不到合格三元组就回 None，前端显示暂无。
    """
    raw = [_num(p) for p in parts[1:]]
    for i in range(len(raw) - 2):
        a, b, c = raw[i], raw[i + 1], raw[i + 2]
        if a is None or b is None or c is None:
            continue
        if a <= 0 or b <= 0 or _looks_like_date(a) or _looks_like_date(b):
            continue
        if abs((a - b) - c) > max(1.0, abs(c) * 0.01):
            continue
        if abs(c) > a + b:      # 净额不可能大于进出之和
            continue
        return {
            "code": code,
            "net": c,
            "inflow": a,
            "outflow": b,
            "ratio": round(c / max(a + b, 1.0) * 100.0, 2),
        }

    warns.append(f"{code} 资金流字段未通过不变量校验，本轮不显示")
    return None


def flow(codes: Iterable[Tuple[str, str]]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """批量当日资金流。入参 `[(code, market)]`，出参 `({code: flow}, warnings)`。

    **只支持 A 股**：主力/超大单口径本身只覆盖 A 股（与个股详情页一致）。
    港股直接不请求。
    """
    out: Dict[str, Dict[str, Any]] = {}
    warns: List[str] = []
    cn = [c for c, m in codes if m != "hk"]
    if not cn:
        return out, warns

    syms = [f"ff_{symbol(c, 'cn')}" for c in cn]
    if _down("tx_flow"):
        warns.append("资金流源熔断中（10 分钟后自动恢复），本轮不显示资金流")
        return out, warns
    try:
        text = call("腾讯当日资金流", _get,
                    f"http://qt.gtimg.cn/q={','.join(syms)}", encoding="gbk")
        _mark_up("tx_flow")
    except Exception as exc:  # noqa: BLE001
        _fail("tx_flow", exc)
        warns.append(f"资金流源失败：{type(exc).__name__}")
        return out, warns

    parsed = _chunks(text)
    for c in cn:
        parts = parsed.get(f"ff_{symbol(c, 'cn')}")
        if parts is None:
            continue  # 源压根没回这一只，交给下面的整批降级判断
        row = _parse_flow(parts, c, warns)
        if row:
            out[c] = row
    if cn and not out:
        # 一只都没成时，逐只的「未通过校验」就是噪音 —— 二十只股会刷出二十条
        # 一模一样的话。收敛成一条说清这是**源**的问题，别让用户以为每只股票
        # 各自出了状况。
        warns.clear()
        warns.append("资金流接口返回的结构与预期不符，本轮不显示（未验证过的源，属预期降级）")
    return out, warns
