"""基金盘中估值 —— 天天基金官方估值为主源，持仓穿透为备，两条都挂就留空。

## 为什么是两条链

基金一天只公布一次净值，盘中那个数是**估**出来的，不是行情。市面上两条路：

    官方估值  天天基金 `FundValuationLast` 直接给 `GSZ`（估算净值）与
              `GSZZL`（估算涨跌幅）。口径由源自己定，是个黑箱，但**覆盖是
              完整的**（含债券、现金、未进前十的持仓），且**支持批量**。
    持仓穿透  拿上一季披露的前十大重仓股 + 它们此刻的涨跌幅，加权算出来。
              可解释、能自证，但**只覆盖披露出来的那一小部分仓位**。

主源选官方估值（覆盖完整），持仓穿透只在前者拿不到时才用。两条都失败 →
字段置 None，前端显示「—」，**绝不拿假数顶上**。

## 官方估值换过一次源（2026-07-21）

旧源 `fundgz.1234567.com.cn/js/{code}.js`（JSONP）因监管要求**已全量下线**：
所有代码一律 301 到 `notfound.html`，返回一个 3924 字节的 HTML 错误页。
**关键是它 HTTP 仍然是 200** —— 日志里会记成「取数成功」，只有解析响应体
才能识破；光看状态码会被骗过去。

新源是天天基金 H5 在用的：

    GET https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast
        FCODES=161725,001632      备域同路径：fundcomapi.eastmoney.com
        FIELDS=FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE

字段与旧源一一对应，但有两条差别都会影响判断，**别照搬旧逻辑**：

1. **`NAV` 不是「昨收」**，而是 `PDATE` 那天**已公布的净值**。盘中两者等价
   （NAV 还是昨天的），但盘后与周末 NAV 已更新成当天值，而 `GSZ`/`GSZZL`
   还停在估值 —— 此时 `GSZZL` 相对的是上一交易日、`(GSZ/NAV−1)` 相对的是
   PDATE 当日，**两者之差恰恰是估值误差本身**，不是解析错误。
2. **部分主动管理型基金不再提供估值**：`GSZ`/`GSZZL`/`GZTIME` 三个字段是
   `null`，但 `SHORTNAME`/`NAV`/`PDATE` 照给。**这不是取数失败**，页面必须
   区分开，否则用户会以为接口又挂了。

## 与实时行情共用的部分

持仓股的实时涨跌幅**直接调 `fetchers.live.quotes()`** —— A 股/港股分批、腾讯
批量源、字段级不变量校验、`tx_quote` 熔断全部原样复用，这里一行都不重写。
一只基金十只重仓股，二十只基金共两百个代码，`quotes()` 一次批量拿完。

## 本模块的不变量（过不了就整条链判失败）

官方估值换源后**不能再用 `gszzl ≈ (gsz / nav − 1) × 100`**（理由见「换过一次源」
一节第 1 条：两者基准不同，差值就是估值误差本身，硬比会把所有基金都判成结构
错误）。换成一个宽松但仍有意义的判据：

    |gsz / nav − 1| × 100 ≤ 20%      抓数量级错误，不抓口径差

它抓得住小数点错位与单位错乱（那种会差几倍、几十倍），抓不住零点几个百分点
的差异 —— 后者是正常现象，不是解析错误。另外新源是**正经 JSON + 明确字段名**，
比旧的 JSONP 字符串拼接本来就更不容易错位，所以这道门的作用相应变小，但
**没有消失**：拿不到 nav 就等于完全没有交叉验证，那时整条链照样判失败。

持仓穿透有三道门，任何一道不过就判失败：

    结构   `cells[1]` 必须是 6 位数字（股票代码列）、每行有且仅有一个以 `%`
           结尾的格（占净值比例列）。**不写死列序号，靠这两个特征互相印证**，
           对不上就把该行计入坏行，坏行过半直接放弃整张表。
    规模   有效持仓 ≥ 3 只。前十大正常是 10 只，只解析出 1~2 只说明解析错了。
    覆盖   取到价的持仓合计占净值 ≥ 20%。前十大正常占 40~60%，若只算出个位数
           要么解析错了，要么这是一只 QDII / 债基（持仓是美股或债券）——
           硬算出来会是个接近 0 的**错数**，比不显示更害人。

## 三个刻意的取舍

**只认 6 位 A 股代码。** 港股/美股在天天基金持仓表里长什么样本环境没验证过，
宁可不计入，也不能把 `00700` 当 A 股丢给 `quotes()` 去查 —— 查出来的是别的东西。
代价是 QDII 的穿透估值基本不可用，由第三道门（覆盖 ≥ 20%）拦下来，显示「未取到」。

**穿透链只给涨跌幅，不给估算净值。** 算涨跌幅不需要昨收，算净值需要，而昨收
恰恰只有官方估值那个接口才有 —— 两条链挂一个才轮到另一条，所以拿不到。字段
留 None，前端显示「—」。

**官方估值的时点不是今天就打 `stale` 标。** 周一早上源还挂着周五的 `gztime`
是常态，把上周五的估值当成今天的现值是错数。这里**不把值抹掉**（抹掉反而让
人以为接口挂了），只连同时间一起显示，由用户判断。

## 降级承诺

拿不到、熔断中、结构对不上，一律返回空字段 + 一条 warning。
熔断与 `live.py` / `hk.py` / `quotes.py` **各自独立**：东财系在公司网被 RST
时一次都通不了，共用熔断会把本来能用的腾讯行情一起判死。
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from app.fetchers import fund_nav as nav_fetch
from app.fetchers import live as live_fetch
from app.fetchers.client import call

logger = logging.getLogger(__name__)

__all__ = ["estimate", "reset_circuit"]

# ----------------------------------------------------------------- 熔断

_CIRCUIT: Dict[str, float] = {}   # 源名 -> 恢复时刻(monotonic)
_CIRCUIT_TTL = 900.0              # 15 分钟：本页手动触发，不像实时页 5 秒一轮
_FAILURES: Dict[str, int] = {}


def _down(name: str) -> bool:
    return time.monotonic() < _CIRCUIT.get(name, 0.0)


def _mark_up(name: str) -> None:
    _CIRCUIT.pop(name, None)
    _FAILURES.pop(name, None)


def _mark_down(name: str) -> None:
    _CIRCUIT[name] = time.monotonic() + _CIRCUIT_TTL
    logger.warning("基金估值源熔断 %s（%d 分钟后重试）", name, int(_CIRCUIT_TTL / 60))


def _fail(name: str, exc: Exception, threshold: int = 3) -> None:
    """连续失败 3 次才熔断 —— 单次抖动不该把一条链判死。"""
    _FAILURES[name] = _FAILURES.get(name, 0) + 1
    if _FAILURES[name] >= threshold:
        _mark_down(name)
        logger.warning("基金估值源 %s 连续失败 %d 次，熔断: %s",
                       name, _FAILURES[name], exc)


def reset_circuit() -> None:
    _CIRCUIT.clear()
    _FAILURES.clear()


# ----------------------------------------------------------------- 基础件

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    else:
        m = _NUM_RE.search(str(value))
        if not m:
            return None
        v = float(m.group())
    return None if v != v else v   # NaN


def _decode(raw: bytes) -> str:
    """UTF-8 → GBK 顺序与 `live._decode` 一致。**顺序不能反**：大量 UTF-8 中文
    用 GBK 解不会报错地产出乱码，反过来则几乎必然抛错走 fallback。"""
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
    """单次取数。**不自带重试** —— 重试由 `client.call()` 统一做，
    这里再包一层会把 3 次指数退避乘成 9 次。

    `headers` 是给东财系准备的：`fundf10` 有 WAF，缺 `Referer` 直接 404，
    `Referer` 域名写错则 514（实测 2026-10-10）。不传就是裸请求。
    """
    def _req() -> str:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        return _decode(resp.content)

    return _req()


# ------------------------------------------------- 链 A：天天基金官方估值

# 新源（2026-07-21 起）。旧的 fundgz JSONP 已全量下线，见模块文档。
_VAL_URL = "https://fundcomapi.tiantianfunds.com/mm/newCore/FundValuationLast"
_VAL_URL_BACKUP = "https://fundcomapi.eastmoney.com/mm/newCore/FundValuationLast"
_VAL_FIELDS = "FCODE,SHORTNAME,GSZZL,GZTIME,GSZ,NAV,PDATE"

# 浏览器 UA。**两条链都要**：fundgz 下线前就认 UA，fundf10 的 WAF 更是
# 明确认；`requests` 默认的 `python-requests/x.y` 没实测过能不能过，别赌。
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 估值与已公布净值的最大允许偏离（%）。见模块文档「不变量」：这个阈值抓的是
# **数量级错误**（小数点错位、单位错乱会差几倍），不是口径差。实测正常值在
# 0.05% 量级，极端行情下场外基金单日涨跌也远到不了 20%。
_MAX_DRIFT = 20.0


def _stale(gztime: str) -> bool:
    """估值时点不是今天 → 标记非今日。认不出日期也当非今日（保守）。

    周末与周一早上源挂着上一交易日的 `GZTIME` 是常态，把上周五的估值当成
    今天的现值是错数。这里**不把值抹掉**（抹掉反而让人以为接口挂了），
    只连同时间一起显示，由用户判断。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", gztime or "")
    if not m:
        return True
    return m.group(1) != today


def _val_payload(text: str) -> Optional[List[Dict[str, Any]]]:
    """`{"data":[…],"errorCode":0,"success":true}` → 节点列表。

    **结构对不上就 None**，不猜。`errorCode` 缺席时当 0 处理（源哪天去掉
    这个字段不至于整条链瞎掉），但给成非 0 就是明确失败。
    """
    try:
        node = json.loads((text or "").strip())
    except ValueError:
        return None
    if not isinstance(node, dict):
        return None
    if node.get("errorCode") not in (0, "0", None):
        return None
    data = node.get("data")
    return data if isinstance(data, list) else None


def _val_row(code: str, node: Dict[str, Any], warns: List[str]
             ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """一个基金节点 → `(行结构 or None, 名称与净值提示 or None)`。

    **`GSZ`/`GSZZL` 为 null 与「取数失败」是两回事**：源照给了名称、正式
    净值与净值日期，只是这只基金它不再提供盘中估值（部分主动管理型基金
    2026 年起如此）。这必须是一条**独立的、能读懂的** warning，混进
    「取数失败」里会让用户以为接口又挂了。

    两条失败路径都会把 `hint` 交出去：名称与正式净值是白送的真数据，**不能
    跟着一起扔** —— 那样用户只看到一个光秃秃的代码，还会以为基金不存在。
    """
    nav = _num(node.get("NAV"))
    est = _num(node.get("GSZ"))
    pct = _num(node.get("GSZZL"))
    name = str(node.get("SHORTNAME") or "").strip()
    nav_date = str(node.get("PDATE") or "").strip() or None
    gztime = str(node.get("GZTIME") or "").strip()

    hint = {"name": name, "nav": nav, "nav_date": nav_date}

    if est is None or pct is None:
        warns.append(f"{code} 天天基金不提供该基金的盘中估值"
                     f"（{name or '名称未知'}），改走持仓穿透")
        return None, hint

    if not nav or nav <= 0:
        # 没有 nav 就没有任何交叉验证，整条链判失败 —— 宁可换下一条链。
        warns.append(f"{code} 估值源缺少有效正式净值，改走持仓穿透")
        return None, hint

    # 换源后的自洽校验（方案 A）。**不是** `gszzl ≈ (gsz/nav−1)×100` ——
    # 那条在新口径下必然失败，因为 GSZZL 相对上一交易日、而 nav 是 PDATE
    # 当日已公布净值，两者之差就是估值误差本身。这里只抓数量级错误。
    drift = abs(est / nav - 1.0) * 100.0
    if drift > _MAX_DRIFT:
        warns.append(f"{code} 估值与已公布净值相差 {drift:.1f}%，"
                     f"超出 {_MAX_DRIFT:.0f}%，两个字段都不用")
        return None, hint

    return {
        "source": "官方估值",
        "name": name,
        "nav": nav,
        "nav_date": nav_date,
        "estimate_nav": est,
        "estimate_pct": pct,
        "coverage": None,
        "as_of": gztime or None,
        "stale": _stale(gztime),
        "note": None,
    }, hint


def _valuations(codes: List[str], warns: List[str]
                ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """链 A：**一次批量**拿全部基金的官方估值。返回 `(hits, hints)`。

      `hits`  拿到估值的基金，`{code: 行结构}` —— 调用方据此认为链 A 成功
      `hints` 源**回答了但不提供估值**的基金，`{code: {name, nav, nav_date}}`

    `hints` 里的基金仍要送去链 B 估涨跌幅，但名称与正式净值用这里的（链 B
    只能算涨跌幅，给不出这两样）。一个代码都进不来就直接回 `({}, {})` ——
    让 `_down` 的告警说清楚，别发空请求。
    """
    if not codes:
        return {}, {}
    if _down("em_fundval"):
        warns.append(f"天天基金估值源熔断中，{len(codes)} 只基金改走持仓穿透")
        return {}, {}

    text: Optional[str] = None
    last: Optional[Exception] = None
    for url in (_VAL_URL, _VAL_URL_BACKUP):
        try:
            text = call(f"天天基金估值 {len(codes)} 只", _get, url,
                        params={"FCODES": ",".join(codes), "FIELDS": _VAL_FIELDS},
                        headers={"User-Agent": _UA})
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            logger.info("天天基金估值 %s 失败（%s），换备域", url, type(exc).__name__)

    if text is None:
        _fail("em_fundval", last or Exception("估值接口无响应"))
        warns.append(f"天天基金估值取数失败：{type(last).__name__ if last else 'unknown'}")
        return {}, {}
    _mark_up("em_fundval")

    payload = _val_payload(text)
    if payload is None:
        warns.append("天天基金估值返回结构与预期不符，全部改走持仓穿透")
        return {}, {}

    hits: Dict[str, Dict[str, Any]] = {}
    hints: Dict[str, Dict[str, Any]] = {}
    for node in payload:
        if not isinstance(node, dict):
            continue
        code = str(node.get("FCODE") or "").strip()
        if not code:
            continue
        row, hint = _val_row(code, node, warns)
        if row is not None:
            hits[code] = row
        elif hint is not None:
            hints[code] = hint
    return hits, hints


# ------------------------------------------------- 链 B：持仓穿透

_F10_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"

# **东财 WAF 认这两样**，缺一个就拿不到数据（实测 2026-10-10）：
#   无 Referer/UA            → HTTP 404
#   Referer 写成 fund.eastmoney.com → HTTP 514（WAF 拦截）
#   Referer = fundf10.eastmoney.com + UA → HTTP 200，11686 字节
# Referer 必须是**发起页**所在域，写成主站域名同样过不了。
_F10_HEADERS = {
    "Referer": "https://fundf10.eastmoney.com/",
    "User-Agent": _UA,
}

_TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_H4T_RE = re.compile(r'<h4[^>]*class="[^"]*\bt\b[^"]*"[^>]*>(.*?)</h4>', re.S | re.I)
_H4_RE = re.compile(r"<h4[^>]*>(.*?)</h4>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_PCT_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*%$")
_CN_CODE_RE = re.compile(r"^\d{6}$")
# 只解这几个实体：天天基金的持仓表用 `&nbsp;` 拼「期次 + 股票投资明细」，
# 不解开的话 note 里会原样显示成 `2026年3季度&nbsp;&nbsp;股票投资明细`。
# `&amp;` 必须排最后 —— 放前面会把 `&amp;nbsp;` 这种「字面量」二次解码。
_ENTITIES = (
    ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
    ("&#39;", "'"), ("&#x27;", "'"), ("&amp;", "&"),
)

# 穿透链的三道门（详见模块文档）
_MIN_POSITIONS = 3
_MIN_COVERAGE = 20.0        # 前十大正常占 40~60%，低于这个数不是算错了就是非 A 股
_BAD_ROW_RATIO = 0.5        # 过半行结构对不上 = 整张表结构变了，别猜
_HOLD_BUDGET = 20.0         # 持仓拉取总预算（秒），同 `_attach_perf` 的思路
_MAX_HOLD_CODES = 400       # 一次请求里最多查多少只重仓股，防止打爆批量接口


def _js_string(text: str, marker: str) -> Optional[str]:
    """从 `var apidata={ content:"...", ... };` 里取出字符串值。

    不引第三方 JS 解析器：`demjson` 不在依赖里，而这个返回体只用到
    `"` `\\` `\'` `\"` `\n` `\r` `\t` 几种转义，手写够用；未知转义按
    「丢掉反斜杠、保留字符」处理，不会因为一个没见过的转义码整段作废。
    """
    i = (text or "").find(marker)
    if i < 0:
        return None
    i += len(marker)
    while i < len(text) and text[i] in " \t\r\n":
        i += 1
    if i >= len(text) or text[i] != '"':
        return None
    i += 1
    out: List[str] = []
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text):
                return None
            nxt = text[i + 1]
            # \uXXXX 要真解出来：只丢反斜杠会把 `中文` 变成 `u4e2d`，
            # 而天天基金的持仓名完全可能走这种转义。四个 hex 位对不上就
            # 退回下面的「保留字符」分支，不猜。
            hex4 = text[i + 2:i + 6]
            if nxt == "u" and len(hex4) == 4 and re.fullmatch(r"[0-9a-fA-F]{4}", hex4):
                out.append(chr(int(hex4, 16)))
                i += 6
                continue
            out.append({"n": "\n", "r": "\r", "t": "\t",
                        '"': '"', "\\": "\\", "/" : "/"}.get(nxt, nxt))
            i += 2
            continue
        if ch == '"':
            return "".join(out)
        out.append(ch)
        i += 1
    return None


def _strip_tags(html: str) -> str:
    out = _TAG_RE.sub("", html or "")
    for a, b in _ENTITIES:
        out = out.replace(a, b)
    return re.sub(r"\s+", " ", out).strip()


def _parse_holdings(table_html: str) -> Tuple[List[Tuple[str, float]], int, int]:
    """表格 HTML → `([(A股代码, 占净值%)], 有效行数, 坏行数)`。

    **不写死列序号**：靠「第 2 格是 6 位数字」与「有且仅有一个格以 % 结尾」
    两个特征互相印证，任一不成立就把该行计入坏行。列序变了 → 坏行过半 →
    调用方放弃整张表，而不是按错位的列读出一堆张冠李戴的数。
    """
    out: List[Tuple[str, float]] = []
    seen = set()
    good = bad = 0
    for row in _ROW_RE.findall(table_html):
        cells = [_strip_tags(c) for c in _TD_RE.findall(row)]
        if len(cells) < 4:
            continue
        code = cells[1].strip()
        shares = [m for m in (_PCT_RE.match(c) for c in cells) if m]
        if not _CN_CODE_RE.match(code) or len(shares) != 1:
            bad += 1
            continue
        share = _num(shares[0].group(1))
        # 占净值比例是个百分数：单只不可能 0，也不可能 ≥ 100（那是全仓）
        if share is None or share <= 0 or share >= 100:
            bad += 1
            continue
        good += 1
        if code not in seen:
            seen.add(code)
            out.append((code, share))
    return out, good, bad


def _holdings(code: str, warns: List[str]) -> Tuple[Optional[str], List[Tuple[str, float]]]:
    """最新一期前十大重仓股 → `(期次标签, [(A股代码, 占净值%)])`。

    **只认 6 位 A 股代码**，理由见模块文档。拿不到返回 `(None, [])`。
    """
    if _down("em_fundf10"):
        warns.append("天天基金持仓源熔断中，本轮不走持仓穿透")
        return None, []
    try:
        # `topline=10` 是**实测能拿到持仓表**的那一档（11686 字节）。之前写的
        # `topline=10000` 从未验证过；而 `topline=` 空值实测只回 1512 字节、
        # 根本没有持仓表 —— 参数不是摆设。`year`/`month` 留空 = 取最新一期。
        text = call(f"天天基金持仓 {code}", _get, _F10_URL, params={
            "type": "jjcc", "code": code, "topline": "10",
            "year": "", "month": "", "rt": str(int(time.time() * 1000)),
        }, headers=_F10_HEADERS)
        _mark_up("em_fundf10")
    except Exception as exc:  # noqa: BLE001
        _fail("em_fundf10", exc)
        warns.append(f"{code} 持仓取数失败：{type(exc).__name__}")
        return None, []

    content = _js_string(text, "content:")
    if not content:
        warns.append(f"{code} 持仓接口返回结构与预期不符，本轮不走持仓穿透")
        return None, []

    # 最新一期在最前面：优先认 `<h4 class="t">`（与参考实现一致），认不到再放宽
    # 到任意 h4。没有标题就取第一张表，但期次标 None —— 不编。
    h4 = _H4T_RE.search(content) or _H4_RE.search(content)
    label = _strip_tags(h4.group(1)) if h4 else None
    if not label:
        label = None
    table = _TABLE_RE.search(content, h4.end() if h4 else 0)
    if not table:
        warns.append(f"{code} 持仓表缺失，本轮不走持仓穿透")
        return None, []

    rows, good, bad = _parse_holdings(table.group(1))
    total = good + bad
    if not rows:
        warns.append(f"{code} 持仓表解析不出任何 A 股持仓，本轮不走持仓穿透")
        return None, []
    if total and bad / total > _BAD_ROW_RATIO:
        warns.append(f"{code} 持仓表结构与预期不符（{bad} 行读不懂 / 共 {total} 行），"
                     f"本轮不走持仓穿透")
        return None, []
    return label, rows


def _compose(code: str, period: Optional[str],
             rows: List[Tuple[str, float]],
             pct_by: Dict[str, Optional[float]],
             stamps: Dict[str, str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """把持仓与行情合成一个估值行。返回 `(行, 失败原因)`，原因非 None 即失败。

    公式取**绝对贡献**而不是归一化加权平均：

        est% = Σ(wᵢ × rᵢ) / 100          wᵢ=占净值%  rᵢ=当日涨跌幅%
        coverage = Σ wᵢ（仅算取到价的）

    归一化（Σ(w·r)/Σw）估的是「股票部分的涨跌幅」，要还原成基金涨跌幅还得再乘
    一个股票仓位占比 —— 而那个占比要多打一次资产配置接口、又是另一张没验证过的
    HTML 表。绝对贡献不需要它：算出来的就是**前十大重仓股对基金净值的实际贡献
    （百分点）**，每个输入都能自证，缺的部分明确体现在 `coverage` 里，不靠一个
    假设去补。
    """
    priced = [(c, w) for c, w in rows if pct_by.get(c) is not None]
    coverage = sum(w for _, w in priced)
    if len(priced) < _MIN_POSITIONS:
        return None, (f"{code} 只取到 {len(priced)} 只重仓股的行情"
                      f"（至少需要 {_MIN_POSITIONS} 只），本轮不估算")
    if coverage < _MIN_COVERAGE:
        return None, (f"{code} 取到价的持仓仅覆盖净值 {coverage:.1f}%"
                      f"（门槛 {_MIN_COVERAGE:.0f}%），多半是非 A 股持仓或解析出错，"
                      f"本轮不估算")

    contrib = sum(w * pct_by[c] for c, w in priced) / 100.0   # 百分点
    # 行情时间戳是 `HH:MM:SS`，字符串比较即时间比较；没有就回 None，
    # 由路由层用自己收到的时刻兜底（同 `/api/realtime` 的 as_of）。
    used = [stamps[c] for c, _ in priced if stamps.get(c)]

    note = (f"仅覆盖前十大重仓股中的 A 股部分（合计净值 {coverage:.1f}%），"
            f"其余仓位未计入，本值通常低于真实涨跌幅")
    if period:
        note = f"持仓期次 {period}；{note}"
    return {
        "source": "持仓穿透",
        "name": "",
        "nav": None,
        "nav_date": None,
        "estimate_nav": None,
        "estimate_pct": round(contrib, 4),
        "coverage": round(coverage, 2),
        "as_of": max(used) if used else None,
        "stale": False,
        "note": note,
    }, None


# --------------------------------------------------------------- 主入口


def estimate(items: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """批量盘中估值。入参 `[{code}]`，出参 `(rows, warnings)`。

    **每个入参都会得到一行** —— 拿不到就整行空字段，前端照常渲染出「—」。
    少一行会让用户以为这只基金没添加成功。

    两段式：先对全部走链 A（**一次批量请求**，新接口支持 `FCODES` 多只），
    没拿到估值的再统一走链 B。链 B 的持仓逐只拉（每只一次），但**行情只批量
    拉一次** —— 两百个代码塞进同一次 `quotes()`，复用它的 A 股分批与熔断。

    第三段是净值历史（`fetchers.fund_nav`），只为给出行里的
    `actual_pct` / `actual_date` —— 页面据此在**盘前显示前一交易日的实际、
    盘后看当日机构净值出没出**。它天生可以缺席：拿不到就是 None，估值照常。
    顺带把序列塞进 `nav_series` 交路由层回填本地库，于是首屏回读
    （`/fund-estimate/last`，**零网络**）也能算出实际涨跌幅。
    """
    warns: List[str] = []
    order: List[str] = []
    results: Dict[str, Dict[str, Any]] = {}
    seen = set()

    for it in items:
        code = str(it.get("code") or "").strip()
        if not code or code in seen:
            continue
        seen.add(code)
        order.append(code)

    hits, hints = _valuations(order, warns)
    for code, hit in hits.items():
        hit["code"] = code
        results[code] = hit
    # 链 A 没给估值的（含「这只基金不提供估值」与「取数失败」两种）都送链 B，
    # 但把链 A 白送的名称与正式净值一并带过去 —— 链 B 只算得出涨跌幅。
    pending = [c for c in order if c not in results]

    if pending:
        _penetrate(pending, results, warns, hints)

    # 净值历史：**在最后统一挂**，保证每个 code 都有这几个键（前端不必判
    # key 存不存在），且失败只让它们留空，不回滚已经算好的估算。
    series_by = nav_fetch.histories(order, warns)
    rows: List[Dict[str, Any]] = []
    for code in order:
        row = results.get(code) or _empty(code)
        series = series_by.get(code) or []
        last = series[-1] if series else None
        row["actual_pct"] = last.get("pct") if last else None
        row["actual_date"] = last.get("date") if last else None
        row["nav_series"] = series or None
        rows.append(row)
    return rows, warns


def _empty(code: str) -> Dict[str, Any]:
    """两条链都失败时的占位行：全字段 None，靠 warnings 解释为什么。"""
    return {
        "code": code,
        "source": "",
        "name": "",
        "nav": None,
        "nav_date": None,
        "estimate_nav": None,
        "estimate_pct": None,
        "actual_pct": None,
        "actual_date": None,
        "nav_series": None,
        "coverage": None,
        "as_of": None,
        "stale": False,
        "note": None,
    }


def _penetrate(codes: List[str], results: Dict[str, Dict[str, Any]],
               warns: List[str],
               hints: Optional[Dict[str, Dict[str, Any]]] = None) -> None:
    """链 B：逐只拉持仓 → 批量拉行情 → 逐只合成。全程带时间预算。

    `hints` 是链 A 交过来的「名称与正式净值」（那些源回答了但不提供估值的
    基金）。链 B 只能算涨跌幅，这两样得从这里补，否则用户只看得到一串代码。
    """
    hints = hints or {}
    deadline = time.monotonic() + _HOLD_BUDGET

    holds: Dict[str, Tuple[Optional[str], List[Tuple[str, float]]]] = {}
    for code in codes:
        if time.monotonic() > deadline:
            warns.append(f"持仓拉取超时（{_HOLD_BUDGET:.0f}s 预算用尽），"
                         f"剩余 {len(codes) - len(holds)} 只本轮不估算")
            break
        holds[code] = _holdings(code, warns)

    need = sorted({c for _, rows in holds.values() for c, _ in rows})
    if len(need) > _MAX_HOLD_CODES:
        warns.append(f"重仓股共 {len(need)} 只，超出 {_MAX_HOLD_CODES} 只上限，本轮不走持仓穿透")
        return

    pct_by: Dict[str, Optional[float]] = {}
    stamps: Dict[str, str] = {}
    if need:
        # 复用实时行情那条链：A 股分批、字段级校验、tx_quote 熔断全都现成
        qrows, qwarns = live_fetch.quotes([{"code": c, "market": "cn"} for c in need])
        warns.extend(qwarns)
        for r in qrows:
            pct_by[r["code"]] = r.get("pct")
            if r.get("updated_at"):
                stamps[r["code"]] = r["updated_at"]

    for code in codes:
        if code not in holds:
            continue
        period, rows = holds[code]
        if not rows:
            continue
        row, err = _compose(code, period, rows, pct_by, stamps)
        if row is None:
            if err:
                warns.append(err)
            continue
        row["code"] = code
        # 链 A 白送的名称与正式净值 —— 链 B 只算得出涨跌幅，这两样要补上，
        # 否则「这只基金不提供估值」的那些会显示成一串光秃秃的代码。
        hint = hints.get(code) or {}
        if not row.get("name"):
            row["name"] = hint.get("name") or ""
        if row.get("nav") is None:
            row["nav"] = hint.get("nav")
        if row.get("nav_date") is None:
            row["nav_date"] = hint.get("nav_date")
        results[code] = row
