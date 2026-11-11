"""财经新闻聚合：多源快讯 + 主题分类 + 热度打分（研究用，不构成投资建议）。

**多源**（2026-09 实测全部实时可达，均走 fetchers.client.call 的限速/重试）：
    cls    财联社电报    stock_info_global_cls     标题+正文，无链接
    em     东方财富快讯  stock_info_global_em      标题+摘要+链接
    futu   富途电报      stock_info_global_futu    正文+链接（标题常为空）
    sina   新浪财经快讯  stock_info_global_sina    纯文本（无标题）
    shmet  上期所快讯    futures_news_shmet        纯文本，商品期货为主
    cjzc   东财财经早餐  stock_info_cjzc_em        每日一条长文+链接

同花顺 stock_info_global_ths 与部分东财名单接口在本机已被封（RemoteDisconnected），
解封后再加回来 —— 多源聚合本身就是回退：任一源挂掉由其余源补位，
**全部**挂掉才落回内置演示数据（demo=true）。

热度是本项目自己算的参考值（0~100，与涨跌配色无关）：
    热度 = 时效(0..40) + 关键词(0..35) + 多源报道(0..15) + 源权重(0..10)
同一条新闻被越多不同源同时报道，多源分越高；公式在 README 里同步写明。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd

from app.fetchers.client import ak, call

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 采集

# 单轮抓取的总时长上限（秒）。并发跑，所以就是这一轮最久等多久。
FEED_TIMEOUT = 15.0
# 进程内缓存时长（秒）：快讯 5 分钟刷新一次足够，避免每次进页面都打源站
CACHE_TTL = 300.0


def _txt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    return str(value).strip()


def _head(text: str, limit: int = 40) -> str:
    """从纯文本里抠标题：优先取【】里的题，其次取首句。

    新浪、上期所两路是纯文本没有标题列，正文常以「【题】正文…」开头，
    不抠出来的话列表里会看到一串带半截「】」的长句。
    """
    line = next((s.strip() for s in (text or "").splitlines() if s.strip()), "")
    m = re.match(r"^【([^】]{2,80})】", line)
    if m:
        return m.group(1).strip()
    line = re.sub(r"^【+", "", line)
    if len(line) <= limit:
        return line
    cut = max(line.rfind("。", 0, limit), line.rfind("！", 0, limit),
              line.rfind("；", 0, limit))
    if cut >= 12:
        return line[: cut + 1]
    return line[: limit - 1] + "…"


def _mk(source: str, title: str, content: str, published: str, url: str) -> Dict[str, str]:
    title = (title or "").strip()
    if not title:
        title = _head(content)
    content = (content or "").strip()
    key = f"{source}|{title}|{published}"
    return {
        "id": hashlib.md5(key.encode("utf-8")).hexdigest()[:12],
        "source": source,
        "title": title or "(无标题)",
        "content": content[:500],
        "truncated": len(content) > 500,
        "published_at": _norm_time(published),
        "url": (url or "").strip(),
    }


def _map_cls(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [
        _mk("cls", _txt(r.get("标题")), _txt(r.get("内容")),
            f'{_txt(r.get("发布日期"))} {_txt(r.get("发布时间"))}', "")
        for _, r in df.iterrows()
    ]


def _map_em(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [
        _mk("em", _txt(r.get("标题")), _txt(r.get("摘要")),
            _txt(r.get("发布时间")), _txt(r.get("链接")))
        for _, r in df.iterrows()
    ]


def _map_futu(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [
        _mk("futu", _txt(r.get("标题")), _txt(r.get("内容")),
            _txt(r.get("发布时间")), _txt(r.get("链接")))
        for _, r in df.iterrows()
    ]


def _map_sina(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [
        _mk("sina", "", _txt(r.get("内容")), _txt(r.get("时间")), "")
        for _, r in df.iterrows()
    ]


def _map_shmet(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [
        _mk("shmet", "", _txt(r.get("内容")), _txt(r.get("发布时间")), "")
        for _, r in df.iterrows()
    ]


def _map_cjzc(df: pd.DataFrame) -> List[Dict[str, str]]:
    return [
        _mk("cjzc", _txt(r.get("标题")), _txt(r.get("摘要")),
            _txt(r.get("发布时间")), _txt(r.get("链接")))
        for _, r in df.iterrows()
    ]


FEEDS: List[Dict[str, Any]] = [
    {"key": "cls", "label": "财联社电报", "limit": 20,
     "fn": ak.stock_info_global_cls, "map": _map_cls},
    {"key": "em", "label": "东方财富快讯", "limit": 120,
     "fn": ak.stock_info_global_em, "map": _map_em},
    {"key": "futu", "label": "富途电报", "limit": 50,
     "fn": ak.stock_info_global_futu, "map": _map_futu},
    {"key": "sina", "label": "新浪财经快讯", "limit": 20,
     "fn": ak.stock_info_global_sina, "map": _map_sina},
    {"key": "shmet", "label": "上期所快讯", "limit": 12,
     "fn": ak.futures_news_shmet, "map": _map_shmet},
    {"key": "cjzc", "label": "东财财经早餐", "limit": 4,
     "fn": ak.stock_info_cjzc_em, "map": _map_cjzc},
]
SOURCE_LABEL = {f["key"]: f["label"] for f in FEEDS}

# 内置演示数据（源站全挂时兜底，接口里 demo=true 会在页面上明示）
_DEMO_ROWS = [
    ("宏观政策", "央行开展逆回购操作 维护流动性合理充裕", "【演示数据】人民银行公告，为维护银行体系流动性合理充裕，今日开展逆回购操作。"),
    ("宏观政策", "统计局：8月规模以上工业增加值同比增长", "【演示数据】国家统计局发布数据，工业生产延续恢复态势。"),
    ("股市", "两市成交额重回万亿 券商板块领涨", "【演示数据】沪深两市早盘震荡上行，成交额较前一交易日放大。"),
    ("股市", "多只高位股盘中炸板 情绪面有所降温", "【演示数据】涨停池炸板率上升，短线资金观望情绪浓厚。"),
    ("行业公司", "某公司披露中标大额订单 金额占去年营收三成", "【演示数据】公司称该项目顺利实施将对未来业绩产生积极影响。"),
    ("行业公司", "多家上市公司发布回购与增持公告", "【演示数据】回购股份拟用于股权激励或注销，彰显管理层信心。"),
    ("海外市场", "美联储议息会议在即 市场聚焦利率指引", "【演示数据】交易员关注会后声明中关于通胀与就业的表述。"),
    ("海外市场", "美股三大指数收盘涨跌不一 纳指领涨", "【演示数据】大型科技股多数上涨，市场等待宏观数据落地。"),
    ("商品期货", "国际油价震荡走高 OPEC+产量政策引关注", "【演示数据】供应端不确定性上升，内外盘原油价差收窄。"),
    ("商品期货", "黑色系期货夜盘多数下跌 铁矿石领跌", "【演示数据】现货成交转弱，产业链观望情绪升温。"),
    ("要闻", "国常会部署下一步重点工作", "【演示数据】会议研究了稳增长、促消费等政策举措。"),
    ("要闻", "北向资金今日净流入 规模创阶段新高", "【演示数据】互联互通机制下外资对A股配置热度回升。"),
]
def _demo_items() -> List[Dict[str, Any]]:
    """演示数据：时间按「今天陆续发生」生成，页面上的时效分才合理。"""
    now = datetime.now().replace(second=0, microsecond=0)
    items = []
    for i, (_label, title, content) in enumerate(_DEMO_ROWS):
        stamp = now - timedelta(minutes=i * 25)
        items.append(_mk("demo", title, content, stamp.strftime("%Y-%m-%d %H:%M:%S"), ""))
    return items


# ---------------------------------------------------------------- 主题分类

# 顺序即优先级：先命中先得（美联储→海外市场，央行→宏观政策）
CATEGORIES: List[Tuple[str, str]] = [
    ("hot", "要闻"),
    ("macro", "宏观政策"),
    ("market", "股市"),
    ("industry", "行业公司"),
    ("global", "海外市场"),
    ("commodity", "商品期货"),
]

_CLASS_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("global", ("美联储", "美股", "纳斯达克", "道琼斯", "标普", "欧央行", "欧洲央行",
                "日本央行", "日央行", "特朗普", "白宫", "美国国会", "关税", "制裁",
                "俄罗斯", "乌克兰", "伊朗", "以色列", "朝鲜", "联合国", "G7", "G20",
                "美元指数", "美债", "日元", "英镑", "欧元区", "东证", "首尔",
                "英国", "法国", "德国", "意大利", "西班牙", "韩国", "印度", "沙特",
                "加拿大", "澳大利亚", "越南", "巴基斯坦", "尼泊尔")),
    ("commodity", ("期货", "螺纹", "原油", "油价", "黄金", "金价", "铜价", "铁矿",
                   "焦煤", "焦炭", "玻璃", "纯碱", "PTA", "甲醇", "棉花", "白糖",
                   "豆粕", "生猪", "玉米", "沥青", "沪铜", "沪金", "白银", "OPEC",
                   "伦镍", "伦铝", "多晶硅")),
    ("macro", ("央行", "国务院", "政治局", "发改委", "财政部", "统计局", "证监会",
               "货币政策", "财政政策", "降准", "降息", "加息", "LPR", "MLF", "逆回购",
               "国债", "专项债", "CPI", "PPI", "PMI", "GDP", "社融", "进出口",
               "房地产", "楼市", "住建部", "商务部", "工信部", "外汇局", "金监总局")),
    ("market", ("A股", "沪指", "深成", "创业板", "科创板", "北证", "北交所", "上证",
                "涨停", "跌停", "炸板", "龙虎榜", "券商", "基金", "北向", "融资融券",
                "IPO", "退市", "复牌", "停牌", "股价", "个股", "板块",
                "领涨", "领跌", "成交额", "两市", "大盘", "股指", "A50")),
    ("industry", ("公司", "量产", "中标", "订单", "董事长", "总经理", "董事会", "股东大会",
                  "签署", "战略合作", "新品", "发布会", "扩产", "产能", "中报", "年报",
                  "季度报告", "业绩", "净利润", "营收", "亏损", "扭亏", "并购", "重组",
                  "增持", "回购", "分红", "送转", "定增", "可转债", "专利", "获批",
                  "集采", "招标", "供应链", "出货", "量产")),
]


def classify(text: str) -> str:
    """按关键词给新闻打一个主题标签；全都不中就归「要闻」。"""
    hay = (text or "").upper()
    for key, words in _CLASS_RULES:
        for word in words:
            if word in hay:
                return key
    return "hot"


# ---------------------------------------------------------------- 热度

_FRESH_BANDS: Tuple[Tuple[int, int], ...] = (
    (15 * 60, 40), (60 * 60, 34), (3 * 3600, 28),
    (6 * 3600, 22), (12 * 3600, 16), (24 * 3600, 10),
)
_FRESH_UNKNOWN = 5

# (关键词, 分值)：命中即累加，heat_kw 封顶 35
_HOT_WORDS: Tuple[Tuple[str, int], ...] = (
    ("降准", 7), ("降息", 7), ("政治局", 6), ("美联储", 6), ("重组", 6),
    ("业绩预增", 6), ("央行", 5), ("加息", 5), ("LPR", 5), ("证监会", 5),
    ("国务院", 5), ("涨停", 5), ("并购", 5), ("扭亏", 5), ("关税", 5),
    ("跌停", 4), ("炸板", 4), ("增持", 4), ("回购", 4), ("退市", 4),
    ("制裁", 4), ("万亿", 4), ("北向", 3), ("IPO", 3),
)
_HOT_CAP = 35
_SOURCE_WEIGHT = {"cls": 10, "em": 10, "futu": 8, "sina": 8, "cjzc": 8, "shmet": 6, "demo": 0}
_MULTI_BONUS = {2: 8, 3: 15}  # 被 2 个 / ≥3 个不同源同时报道


def _parse_dt(text: str) -> Optional[datetime]:
    """各源时间格式不一（带不带秒、带不带时区、只有日期），统一成裸本地时间。"""
    t = (text or "").strip().replace("T", " ")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t).replace(tzinfo=None)
    except ValueError:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", t)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d")
        except ValueError:
            return None


def _norm_time(text: str) -> str:
    dt = _parse_dt(text)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _fresh_score(published: Optional[datetime], now: datetime) -> int:
    if published is None:
        return _FRESH_UNKNOWN
    age = (now - published).total_seconds()
    if age < 0:
        age = 0.0
    for secs, score in _FRESH_BANDS:
        if age <= secs:
            return score
    return 5


def _hot_score(text: str) -> int:
    hay = (text or "").upper()
    total = sum(w for k, w in _HOT_WORDS if k in hay)
    return min(_HOT_CAP, total)


def _sig(text: str) -> str:
    """去标点、转小写，用于去重与多源归组。"""
    return re.sub(r"\W", "", (text or "")).lower()


def _grams(text: str) -> Set[str]:
    t = _sig(text)
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else ({t} if t else set())


def _similar(a: Set[str], b: Set[str]) -> float:
    """重合度（交集 / 较短集合）：长标题包含短标题也算同一条。"""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _group_stories(items: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """把「同一条新闻的多源报道」归成一组：组内合并成一条展示，组数即新闻条数。"""
    groups: List[Dict[str, Any]] = []
    for it in items:  # 已按时间倒序，组代表取最新那条
        grams = _grams(it["title"] or it["content"])
        grp = next((g for g in groups if _similar(grams, g["grams"]) >= 0.6), None)
        if grp is None:
            grp = {"grams": grams, "items": []}
            groups.append(grp)
        else:
            grp["grams"] |= grams
        grp["items"].append(it)
    return [g["items"] for g in groups]


def _primary_rank(it: Dict[str, Any]) -> Tuple[int, bool, int, str]:
    """同一条新闻多个源都发时挑展示主体：源权重 > 有原文链接 > 正文长 > 时间新。"""
    return (_SOURCE_WEIGHT.get(it["source"], 0), bool(it["url"]),
            len(it["content"]), it["published_at"])


def _merge(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    """跨源合并成一条：保留主体，附上「还有哪些源也报了 + 各源原文链接」。"""
    primary = max(group, key=_primary_rank)
    seen: List[str] = []
    links: List[Dict[str, str]] = []
    for it in sorted(group, key=lambda x: -_SOURCE_WEIGHT.get(x["source"], 0)):
        if it["source"] in seen:
            continue
        seen.append(it["source"])
        if it["url"]:
            links.append({"source": it["source"],
                          "label": SOURCE_LABEL.get(it["source"], it["source"]),
                          "url": it["url"]})
    primary["sources"] = seen
    primary["source_count"] = len(seen)
    primary["source_links"] = links
    primary["heat_multi"] = _MULTI_BONUS.get(len(seen), 0) if len(seen) >= 2 else 0
    return primary


def _score(items: List[Dict[str, Any]], now: datetime) -> List[Dict[str, Any]]:
    """跨源合并 -> 补 category / 各项热度分 / 总热度 -> 按热度+时间排序。"""
    merged = [_merge(group) for group in _group_stories(items)]
    for it in merged:
        published = _parse_dt(it["published_at"])
        it["category"] = classify(f'{it["title"]} {it["content"]}')
        it["heat_fresh"] = _fresh_score(published, now)
        it["heat_keyword"] = _hot_score(f'{it["title"]} {it["content"]}')
        it["heat_source"] = _SOURCE_WEIGHT.get(it["source"], 0)
        it["heat"] = min(100, it["heat_fresh"] + it["heat_keyword"]
                         + it["heat_multi"] + it["heat_source"])
    merged.sort(key=lambda x: (x["heat"], x["published_at"]), reverse=True)
    return merged


# ---------------------------------------------------------------- 抓取与缓存

_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"at": 0.0, "payload": None}


def _run_feed(feed: Dict[str, Any]) -> Tuple[List[Dict[str, str]], str]:
    try:
        df = call(feed["label"], feed["fn"])
        rows = feed["map"](df)[: feed["limit"]]
        if not rows:
            return [], "返回空数据"
        return rows, ""
    except Exception as exc:  # noqa: BLE001  单源失败不应拖垮整个页面
        return [], f"{type(exc).__name__}: {exc}"[:160]


def _fetch_all() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """并发抓全部源，单轮总时长封顶 FEED_TIMEOUT；返回 (items, 源状态)。

    线程池不 wait：某源卡死时这一轮直接判它超时，不能让全页面陪它等
    （netfix 的总时长闸门会在后台把它掐掉）。
    """
    started = time.monotonic()
    status: List[Dict[str, Any]] = []
    items: List[Dict[str, str]] = []
    pool = ThreadPoolExecutor(max_workers=len(FEEDS))
    try:
        futures = [(feed, pool.submit(_run_feed, feed)) for feed in FEEDS]
        for feed, fut in futures:
            remain = max(0.5, FEED_TIMEOUT - (time.monotonic() - started))
            try:
                rows, err = fut.result(timeout=remain)
            except FutureTimeout:
                rows, err = [], f"超时(>{FEED_TIMEOUT:.0f}s)"
            status.append({"key": feed["key"], "label": feed["label"],
                           "ok": not err, "rows": len(rows), "error": err})
            items.extend(rows)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # 源内去重（同源同标题只留一条）
    seen: Set[str] = set()
    uniq: List[Dict[str, str]] = []
    for it in items:
        sig = f'{it["source"]}|{_sig(it["title"])[:60]}'
        if sig in seen:
            continue
        seen.add(sig)
        uniq.append(it)

    now = datetime.now()
    uniq.sort(key=lambda x: x["published_at"], reverse=True)
    return _score(uniq, now), status


def _build() -> Dict[str, Any]:
    items, sources = _fetch_all()
    demo = not any(s["ok"] for s in sources)
    if demo:
        logger.warning("新闻多源全部失败，回退内置演示数据: %s",
                       [s["error"] for s in sources if s["error"]])
        items = _score(_demo_items(), datetime.now())

    counts: Dict[str, int] = {key: 0 for key, _ in CATEGORIES}
    for it in items:
        counts[it["category"]] = counts.get(it["category"], 0) + 1

    failed = [s["label"] for s in sources if not s["ok"]]
    return {
        "items": items,
        "categories": [{"key": "all", "label": "全部", "count": len(items)}] + [
            {"key": key, "label": label, "count": counts.get(key, 0)}
            for key, label in CATEGORIES
        ],
        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "sources": sources,
        "failed_sources": failed,
        "demo": demo,
        "ttl_sec": int(CACHE_TTL),
        "score_note": "热度 = 时效(0~40) + 关键词(0~35) + 多源报道(0~15) + 源权重(0~10)",
    }


def get_news(refresh: bool = False) -> Dict[str, Any]:
    """取新闻（进程内 5 分钟缓存）。持锁抓取，避免并发请求重复打源站。"""
    with _LOCK:
        cached = _CACHE["payload"]
        if (not refresh and cached is not None
                and time.monotonic() - _CACHE["at"] < CACHE_TTL):
            payload = dict(cached)
            payload["age_sec"] = int(time.monotonic() - _CACHE["at"])
            return payload
        payload = _build()
        _CACHE.update(at=time.monotonic(), payload=payload)
        result = dict(payload)
        result["age_sec"] = 0
        return result
