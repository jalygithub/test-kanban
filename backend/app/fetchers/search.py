# -*- coding: utf-8 -*-
r"""添加时的模糊搜索（**联网**）—— 本仓库少数几个不靠用户点「查询」就发包的入口。

## 为什么必须联网

关注列表里每一项都是用户自己敲进来的代码 —— 页面压根不知道「白酒指数基金」是
`161725`，也不知道「茅台」是 `600519`。要让添加从「先去别处查代码、再回来手敲」
变成「输入名称、点一下」，候选只能来自**全市场**，那就只能联网。

**这是对既有承诺的一次明确修改**（README 原话是「添加时一次网络请求都不发」），
拆成两件事各自诚实：

    添加  POST /api/watchlist、/api/fund-watchlist   纯本地写库，**仍然零网络**
    搜索  GET /api/search                            联网，防抖触发

搜索取不到**不影响添加**：手动贴 6 位代码照旧一路畅通，搜索只是多一条省事的路。

## 两个源（都已实测，2026-10-10）

    基金  fundsuggest.eastmoney.com   JSON：{"ErrCode":0,"Datas":[{CODE,NAME,CATEGORY,…}]}
    股票  smartbox.gtimg.cn           **不是 JSON**：`v_hint="sh~600519~名~py~GP-A^…"`

两边都**必须带请求头**，否则拿不到数据（与 `fundf10` 同一类 WAF，实测无 Referer
仍 200，但没有理由去赌，UA 是浏览器的、Referer 是发起页的）。

## 过滤：这是最容易出错的地方

两个源都**按模糊匹配返回，不保证「只回你该加的东西」**：

- 基金源会回**指数**（`CATEGORY=600`，如「中证白酒」`399997`）。指数加进基金
  关注列表既查不到估值、也永远不会有净值，是条死数据 —— 只留 `CATEGORY=700`。
- 股票源会回**指数**（`ZS`）、**窝轮/权证**（`QZ`）、**美股**（`market=us`）、
  甚至**基金**（`market=jj`）。本页只加 A 股与港股，以上一律丢弃。
- 更阴的一条：**查不到时基金源照样回 10 条**（`ErrCode=0`）。所以
  `ErrCode` 只能证明「请求成功」，**不能证明「搜到了你要的」** ——
  过滤必须落在字段上，不能落在状态码上。

`_decode`、熔断、`client.call` 的用法与同目录其它 fetcher 保持一致；
熔断与缓存都只是**本模块自己的状态**，不与行情源共用（搜索挂了不该连累估值）。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.fetchers.client import call

logger = logging.getLogger(__name__)

__all__ = ["normalize_query", "search_fund", "search_stock", "reset_circuit"]


# --------------------------------------------------------------- 熔断
# 与 hk/live/quotes/fund_estimate 同款：每个源名一把锁，互不牵连。
_CIRCUIT: Dict[str, float] = {}   # 源名 -> 恢复时刻(monotonic)
_CIRCUIT_TTL = 600.0              # 10 分钟：手动触发，不需要像实时页那样激进
_FAILURES: Dict[str, int] = {}
_BREAK_THRESHOLD = 3


def _down(name: str) -> bool:
    return time.monotonic() < _CIRCUIT.get(name, 0.0)


def _mark_up(name: str) -> None:
    _CIRCUIT.pop(name, None)
    _FAILURES.pop(name, None)


def _mark_down(name: str) -> None:
    _CIRCUIT[name] = time.monotonic() + _CIRCUIT_TTL
    logger.warning("搜索源熔断 %s（%d 分钟后重试）", name, int(_CIRCUIT_TTL / 60))


def _fail(name: str, exc: Exception, threshold: int = _BREAK_THRESHOLD) -> None:
    n = _FAILURES.get(name, 0) + 1
    _FAILURES[name] = n
    if n >= threshold:
        _mark_down(name)
        _FAILURES.pop(name, None)


def reset_circuit() -> None:
    """给校验脚本用。搜索不在任何流水线里，没有「每轮开始复位」这回事。"""
    _CIRCUIT.clear()
    _FAILURES.clear()


# --------------------------------------------------------------- 缓存
# 防抖 400ms 意味着打字时会一连串请求，且用户常在「加完又想加下一只」之间
# 反复敲同一个词。相同 (kind, 词) 直接回缓存，既省上游也省熔断计数。
_CACHE: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}
_CACHE_TTL = 600.0        # 10 分钟：候选名单不会因行情变动而变
_CACHE_MAX = 200


def _cache_get(key: Tuple[str, str]) -> Optional[List[Dict[str, Any]]]:
    hit = _CACHE.get(key)
    if not hit:
        return None
    if time.monotonic() - hit[0] >= _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    # 取到也要挪到队尾，淘汰时才是真 LRU（dict 保持插入序）
    _CACHE[key] = hit
    return [dict(r) for r in hit[1]]


def _cache_put(key: Tuple[str, str], rows: List[Dict[str, Any]]) -> None:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.pop(next(iter(_CACHE)), None)
    _CACHE[key] = (time.monotonic(), [dict(r) for r in rows])


# --------------------------------------------------------------- 基础件


def _decode(raw: bytes) -> str:
    """UTF-8 → GBK，顺序与 `fund_estimate._decode` / `live._decode` 一致。

    **顺序不能反**：大量 UTF-8 中文用 GBK 解不会报错地产出乱码，反过来则
    几乎必然抛错走 fallback。两个源实测都是 UTF-8。
    """
    if not raw:
        return ""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _get(url: str, params: Optional[Dict[str, Any]] = None,
         headers: Optional[Dict[str, str]] = None) -> str:
    """单次取数。**不自带重试** —— 重试由 `client.call()` 统一做。"""
    def _req() -> str:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        return _decode(resp.content)

    return _req()


# 搜索词既可能贴代码也可能贴中文，两端都别放任超长：上游拿去也是白跑一趟。
_Q_MIN, _Q_MAX = 1, 24
_Q_RE = re.compile(r"^[0-9A-Za-z\u4e00-\u9fa5.\-（）()]+$")

# 浏览器 UA：与 fund_estimate._UA 同款（两个源都属「按 UA/Referer 认人」的那类）
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def normalize_query(q: str) -> str:
    """清洗搜索词。不合法直接 ValueError —— 由路由转成 400，**先于任何请求**。

    校验放在这里而不是路由里：长度与字符集正是上游白跑一趟的两种情况，
    在发包之前拦掉比收到 404 再解释要诚实。

    **中间的空白一并吃掉**（`招商 中证` → `招商中证`）：名称从别处复制过来
    带空格是常态，为一个空格报 400 只会让用户以为搜索坏了。
    """
    text = re.sub(r"\s+", "", q or "")
    if len(text) < _Q_MIN or len(text) > _Q_MAX:
        raise ValueError(f"搜索词长度需在 {_Q_MIN}~{_Q_MAX} 字之间")
    if not _Q_RE.match(text):
        raise ValueError("搜索词只能是中英文、数字与少量标点")
    return text


# --------------------------------------------------------------- 基金


_FUND_URL = "https://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
_FUND_HEADERS = {
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": _UA,
}
# 700=基金。600=指数会被源按模糊匹配一并返回，**加进来是死数据**（没有估值、
# 也不会有净值），必须按字段挡掉，不能看 ErrCode。
_FUND_CATEGORY = "700"
_FUND_MAX = 15


def search_fund(q: str) -> List[Dict[str, Any]]:
    """基金模糊搜索 → `[{"code","name","type","company","nav","nav_date"}]`。

    `code` 恒为 6 位数字；拿不到就回 `[]`（**不抛**，搜索失败不该打断添加）。
    """
    key = ("fund", q.lower())
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if _down("em_fundsearch"):
        logger.info("天天基金搜索源熔断中，跳过 %r", q)
        return []

    try:
        text = call(f"天天基金搜索 {q[:8]}", _get, _FUND_URL,
                    params={"m": "1", "key": q}, headers=_FUND_HEADERS)
        _mark_up("em_fundsearch")
    except Exception as exc:  # noqa: BLE001
        _fail("em_fundsearch", exc)
        logger.info("天天基金搜索失败（%s）：%s", type(exc).__name__, q)
        return []

    try:
        node = json.loads((text or "").strip())
    except ValueError:
        logger.warning("天天基金搜索返回结构与预期不符（%r）", (text or "")[:80])
        return []
    if not isinstance(node, dict) or node.get("ErrCode") not in (0, "0", None):
        # ErrCode 只证明请求成功 —— 查不到时它也是 0，所以这里不据此判定有结果。
        logger.info("天天基金搜索 ErrCode=%r", node.get("ErrCode") if isinstance(node, dict) else "?")
        return []

    out: List[Dict[str, Any]] = []
    for item in (node.get("Datas") or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("CATEGORY")) != _FUND_CATEGORY:
            continue
        code = str(item.get("CODE") or "").strip()
        name = str(item.get("NAME") or "").strip()
        # 指数被 CATEGORY 挡在外面了，这里再挡一次格式 —— 源哪天改了
        # 分类编码，宁可少给几条候选，也不能把一条死数据塞进关注列表。
        if not re.fullmatch(r"\d{6}", code) or not name:
            continue
        base = item.get("FundBaseInfo") or {}
        if not isinstance(base, dict):
            base = {}
        out.append({
            "code": code,
            "name": name,
            "type": str(base.get("FTYPE") or "").strip() or None,
            "company": str(base.get("JJGS") or "").strip() or None,
            "nav": base.get("DWJZ"),
            "nav_date": str(base.get("FSRQ") or "").strip() or None,
        })
        if len(out) >= _FUND_MAX:
            break

    _cache_put(key, out)
    return [dict(r) for r in out]


# --------------------------------------------------------------- 股票


_STOCK_URL = "https://smartbox.gtimg.cn/s3/"
_STOCK_HEADERS = {
    "Referer": "https://gu.qq.com/",
    "User-Agent": _UA,
}
# 只收本页加得进去的两类市场。us（美股）本页不支持，jj（基金）归基金页管，
# ZS（指数）/QZ（窝轮权证）既没有日线也加不进关注列表。
_STOCK_MARKETS = {"sh", "sz", "bj", "hk"}
_STOCK_TYPES = {"GP-A", "GP"}   # A 股 / 普通股；ZS=指数 QZ=权证一律丢
_STOCK_MAX = 15

_V_HINT_RE = re.compile(r'v_hint\s*=\s*"([^"]*)"')
_UNICODE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")


def _unescape(text: str) -> str:
    r"""`\u8d35\u5dde\u8305\u53f0` → `贵州茅台`。

    只解 `\uXXXX`：这是该源唯一的转义形式（实测）。别的反斜杠序列原样保留，
    宁可显示成 `\n` 这种字面量，也不能因为一个没见过的转义码整条候选作废。
    """
    return _UNICODE_RE.sub(lambda m: chr(int(m.group(1), 16)), text or "")


def search_stock(q: str, market: Optional[str] = None) -> List[Dict[str, Any]]:
    """股票模糊搜索 → `[{"code","name","market"}]`，`market` ∈ `cn` / `hk`。

    `market` 传 `cn` / `hk` 时只回该市场的候选。**子菜单那两个入口必须带上它**：
    在只看港股的页面上列出 `600519`，用户点下去只会拿到一条 400，而且看不出
    为什么 —— 与其给人一个注定失败的选项，不如一开始就别给。

    返回体**不是 JSON**，是 `v_hint="市场~代码~名称~拼音~类型^…"`；无结果时
    整个就是 `v_hint="N";`。拿不到就回 `[]`（**不抛**）。
    """
    if market not in (None, "cn", "hk"):
        raise ValueError("market 只能是 cn / hk")
    key = ("stock:" + (market or "any"), q.lower())
    cached = _cache_get(key)
    if cached is not None:
        return cached
    if _down("tx_smartbox"):
        logger.info("腾讯搜索源熔断中，跳过 %r", q)
        return []

    try:
        text = call(f"腾讯股票搜索 {q[:8]}", _get, _STOCK_URL,
                    params={"v": "2", "q": q, "t": "all"}, headers=_STOCK_HEADERS)
        _mark_up("tx_smartbox")
    except Exception as exc:  # noqa: BLE001
        _fail("tx_smartbox", exc)
        logger.info("腾讯股票搜索失败（%s）：%s", type(exc).__name__, q)
        return []

    m = _V_HINT_RE.search(text or "")
    if not m:
        # 连 v_hint 都没有 = 结构变了或被拦了，与「没搜到」分开记。
        logger.warning("腾讯股票搜索返回结构与预期不符（%r）", (text or "")[:80])
        return []
    payload = m.group(1)
    if payload.strip().upper() == "N":     # 源的「无结果」哨兵
        return []

    out: List[Dict[str, Any]] = []
    seen = set()
    for record in payload.split("^"):
        parts = record.split("~")
        if len(parts) < 5:
            continue
        # `ex` 是源给的交易所（sh/sz/bj/hk/us/jj）。**不能叫 market** ——
        # 那正是本函数的参数名，同名会把「只回某个市场」整个静默失效。
        ex, code, raw_name, _py, kind = parts[:5]
        ex = ex.strip().lower()
        code = code.strip()
        if ex not in _STOCK_MARKETS or kind not in _STOCK_TYPES:
            continue
        name = _unescape(raw_name).strip()
        # 位数是两个市场唯一的硬边界，与 `routes.watchlist_add` 同一套口径：
        # A 股 6 位、港股 1~5 位。对不上说明这条记录的格式变了，别猜。
        ok = (re.fullmatch(r"\d{6}", code) if ex != "hk"
              else re.fullmatch(r"\d{1,5}", code))
        if not ok or not name or code in seen:
            continue
        row_market = "hk" if ex == "hk" else "cn"
        # 子菜单限定的市场：不符的直接丢，别让人点一个注定 400 的候选。
        if market is not None and row_market != market:
            continue
        seen.add(code)
        out.append({"code": code, "name": name, "market": row_market})
        if len(out) >= _STOCK_MAX:
            break

    _cache_put(key, out)
    return [dict(r) for r in out]
