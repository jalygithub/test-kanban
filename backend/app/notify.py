"""跑完任务后把摘要推到飞书（自定义机器人 webhook）。

用户拍板的三件事（2026-09-29）：

  1. 只接飞书 —— 个人微信没有官方 webhook（要发微信只能企业微信机器人或
     Server酱这类第三方转推），按用户选择不做。
  2. **只在定时任务跑完后发**：手动「立即执行」不发，盘后抓完自动推一条，
     跑挂了/部分失败也照发，正文里标出哪几步挂了。
  3. webhook 地址存 SQLite（``store.db.kv_*``），由「定时任务」页配置并带
     「测试发送」；不进代码仓库，改了不用重启。

正文取的全是**库内数据**（股池 / 打分 / 板块资金流 / 关注 / 逐日资金流），
唯一的网络请求是「关注股当日涨跌幅库里查不到」时补拉一次日线，且带预算与
try 兜底，拿不到就显示 "-"。

发送失败**绝不影响任务本身** —— after_job 把拼装与发送的所有异常都吞掉只记
日志（通知在任务收尾之后发，抛出去只会污染任务日志）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from app import cmdcfg
from app.config import settings
from app.fetchers import quotes
from app.metrics import mainforce
from app.store import db

logger = logging.getLogger(__name__)

_WEBHOOK_KEY = "notify_webhook"
_SECRET_KEY = "notify_secret"
_ENABLED_KEY = "notify_enabled"
_SECTIONS_KEY = "notify_sections"

_LOCK = threading.Lock()
_LAST: Dict[str, Any] = {"at": "", "ok": None, "detail": ""}

SEND_TIMEOUT = (5.0, 10.0)   # (建连, 读回)
_PCT_BUDGET = 20.0           # 关注股补拉日线的总预算（秒），超了就显示 "-"
_PCT_MAX = 8                 # 单次最多补拉几只
_WEEK = "一二三四五六日"
STATUS_LABEL = {"success": "成功", "partial": "部分失败", "failed": "失败",
                "test": "测试"}
KIND_LABEL = {"scheduled": "定时任务", "manual": "手动执行",
              "feishu": "飞书指令"}


# ------------------------------------------------------------------ 配置


def config() -> Dict[str, Any]:
    """当前通知配置 + 上次发送结果（给「定时任务」页）。"""
    webhook = db.kv_get(_WEBHOOK_KEY, "") or ""
    secret = db.kv_get(_SECRET_KEY, "") or ""
    with _LOCK:
        last = dict(_LAST)
    return {
        "enabled": db.kv_get(_ENABLED_KEY, "0") == "1",
        "webhook": webhook,
        "secret": secret,
        "has_webhook": bool(webhook),
        "signed": bool(secret),
        "sections": read_sections(),
        # 分段清单一并下发：前端按它渲染勾选框，后端加一段前端自动多一项，
        # 两边不用各维护一份文案
        "section_defs": [{"key": k, "label": lbl} for k, lbl in SECTION_DEFS],
        "last": last,
    }


def set_config(enabled: bool, webhook: str, secret: str,
               sections: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """保存配置。webhook 必须是 http(s) 地址，secret 留空表示不用签名校验。

    ``sections`` 是勾选的分段 key（见 ``SECTION_DEFS``）：
      - ``None`` 表示**不改动**现有勾选（老调用方/只想改地址时传 None）；
      - 传空列表 = 一个数据段都不推，正文只剩标题、任务状态与免责声明。

    未知 key 只丢弃并记日志，不报 422 —— 这是配置保存，因为一个陈旧的分段
    key 让用户的 webhook / 口令存不下去，代价比多推一段大得多。
    """
    webhook = (webhook or "").strip()
    secret = (secret or "").strip()
    if webhook and not webhook.startswith(("http://", "https://")):
        raise ValueError("webhook 必须是 http(s) 开头的地址")

    db.kv_set(_WEBHOOK_KEY, webhook)
    db.kv_set(_SECRET_KEY, secret)
    db.kv_set(_ENABLED_KEY, "1" if enabled else "0")
    if sections is not None:
        known = {k for k, _ in SECTION_DEFS}
        picked = [str(k) for k in sections]
        unknown = [k for k in picked if k not in known]
        if unknown:
            logger.warning("丢弃未知的推送分段：%s", unknown)
        # 按 SECTION_DEFS 顺序规整后落库，读回来的顺序就是正文顺序
        db.kv_set(_SECTIONS_KEY, json.dumps(
            [k for k, _ in SECTION_DEFS if k in set(picked)],
            ensure_ascii=False))
    logger.info("飞书推送配置已更新：enabled=%s webhook=%s signed=%s sections=%s",
                enabled, bool(webhook), bool(secret),
                "不变" if sections is None else read_sections())
    return config()


# ------------------------------------------------------------------ 分段
#
# 推送正文按段拆开，用户在页面上勾哪几段就发哪几段。**顺序即正文顺序**。
# 标题、任务状态行与免责声明不在清单里 —— 那是消息的框架，永远在。

SECTION_DEFS: List[Tuple[str, str]] = [
    ("pools", "涨跌停池（涨停/炸板/跌停/强势 + 最高连板）"),
    ("mainforce", "疑似主力建仓 Top10"),
    ("sector", "板块资金流 · 行业净流入/净流出 Top5"),
    ("watch", "关注股票（当日涨跌幅 + 近 5 日主力净流入）"),
    ("failed", "失败步骤（哪几步挂了）"),
]
_DEFAULT_SECTIONS: List[str] = [k for k, _ in SECTION_DEFS]


def read_sections() -> List[str]:
    """当前勾选的分段 key，按 ``SECTION_DEFS`` 顺序返回。

    没存过（升级上来的老库）= 全选，**行为与没有这个功能时完全一致**。
    存了 ``[]`` 就是真·全不选 —— 判空的是 JSON 串而不是解析结果，
    免得把用户「一个都不推」的选择又改回全选。
    """
    raw = db.kv_get(_SECTIONS_KEY, "") or ""
    if not raw:
        return list(_DEFAULT_SECTIONS)
    try:
        saved = json.loads(raw)
    except ValueError:
        logger.warning("notify_sections 存的不是合法 JSON，回退成全选：%r", raw)
        return list(_DEFAULT_SECTIONS)
    if not isinstance(saved, list):
        return list(_DEFAULT_SECTIONS)
    picked = {k for k in saved if isinstance(k, str)}
    # 已从清单里删掉的旧 key 静默丢弃，不让它影响其余勾选
    return [k for k, _ in SECTION_DEFS if k in picked]



# ------------------------------------------------------------------ 发送


def _record(ok: bool, detail: str) -> Dict[str, Any]:
    with _LOCK:
        _LAST.update({
            "at": datetime.now().isoformat(timespec="seconds"),
            "ok": ok,
            "detail": detail,
        })
        out = dict(_LAST)
    if ok:
        logger.info("飞书推送成功")
    else:
        logger.warning("飞书推送失败: %s", detail)
    return {"ok": ok, **out}


def send(text: str) -> Dict[str, Any]:
    """POST 到飞书自定义机器人。返回 {ok, at, detail}，**不抛异常**。

    飞书安全设置三种：自定义关键词 / IP 白名单 / 签名校验。关键词要求正文含
    指定词（我们的正文是股票名与板块名，命中与否取决于机器人怎么配），IP
    白名单要用户自己加服务器出口 IP，签名校验则带 secret 自动算 sign。
    任何一种没配对，飞书都会回非 0 code，这里如实回给页面。
    """
    webhook = (db.kv_get(_WEBHOOK_KEY, "") or "").strip()
    if not webhook:
        return _record(False, "未配置飞书 webhook（在本页填好地址并保存）")

    payload: Dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
    secret = (db.kv_get(_SECRET_KEY, "") or "").strip()
    if secret:
        # 签名校验：key = "时间戳\n密钥"，空消息做 HMAC-SHA256，再 base64
        ts = str(int(time.time()))
        sign = base64.b64encode(
            hmac.new(f"{ts}\n{secret}".encode("utf-8"),
                     digestmod=hashlib.sha256).digest(),
        ).decode("utf-8")
        payload["timestamp"] = ts
        payload["sign"] = sign

    try:
        resp = requests.post(webhook, json=payload, timeout=SEND_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001  网络/超时一律降级成结果
        return _record(False, f"{type(exc).__name__}: {exc}")

    try:
        data = resp.json()
    except ValueError:  # 对端回了 HTML/空 body
        data = {}
    if not isinstance(data, dict):
        data = {}
    code = data.get("code", data.get("StatusCode", -1))
    msg = str(data.get("msg") or data.get("StatusMessage") or "").strip()
    if code == 0:
        return _record(True, "已发送到飞书")
    return _record(False, f"飞书返回 code={code}" + (f" {msg}" if msg else ""))


# -------------------------------------------------------------- 格式化


def _money(v: Any) -> str:
    """元 → 「+1.23亿」/「-4567万」。非数字给「-」。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "-"
    x = float(v)
    sign = "+" if x >= 0 else "-"
    a = abs(x)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.2f}亿"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.0f}万"
    return f"{sign}{a:.0f}元"


def _pct(v: Any) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "-"
    return f"{float(v):+.2f}%"


def _yi(v: Any) -> str:
    """板块资金流的口径是**亿元**（同花顺/东财板块接口都直接给亿），
    与个股/资金流的「元」不同 —— 混用会把 8.6亿 显示成 8.6元。"""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return "-"
    return f"{float(v):+.2f}亿"


def _date_label(d: str) -> str:
    try:
        day = datetime.strptime(d, "%Y%m%d")
    except (TypeError, ValueError):
        return d or "-"
    return f"{day:%Y-%m-%d}（{_WEEK[day.weekday()]}）"


def _elapsed(sec: Optional[float]) -> str:
    if not isinstance(sec, (int, float)) or sec <= 0:
        return ""
    sec = int(sec)
    return f"{sec // 60}m{sec % 60}s" if sec >= 60 else f"{sec}s"


# -------------------------------------------------------------- 摘要正文


def _watch_pct(trade_date: str, codes: Sequence[str]) -> Dict[str, float]:
    """关注股**当日**涨跌幅（百分数）。先查库，缺的补拉日线（有预算）。"""
    out: Dict[str, float] = {}

    def _take(rows: Sequence[Dict[str, Any]]) -> None:
        for r in rows or []:
            code, pct = r.get("code"), r.get("pct_chg")
            if code and isinstance(pct, (int, float)) and not isinstance(pct, bool):
                out.setdefault(code, float(pct))

    # 1) 当日股池（涨停/炸板/跌停/强势都会带 pct_chg）
    for kind in ("zt", "zb", "dt", "strong"):
        _take(db.load(f"pool_{kind}", trade_date) or [])

    # 2) 当日打分候选：all_rows 覆盖全部打分对象，比 rows（Top N）全。
    #    打分日与目标日不一致时**不采用** —— 免得把旧涨幅当今日涨幅。
    if db.latest_date("main_force") == trade_date:
        mf = db.load_one("main_force", trade_date) or {}
        _take(mf.get("all_rows") or mf.get("rows") or [])

    missing = [c for c in codes if c and c not in out]
    if missing:
        out.update(_fetch_pct(trade_date, missing[:_PCT_MAX]))
    return out


def _fetch_pct(trade_date: str, codes: Sequence[str]) -> Dict[str, float]:
    """库里没有的关注股补拉日线。总预算 `_PCT_BUDGET` 秒，失败/停牌给 「-」。"""
    out: Dict[str, float] = {}
    try:
        start = (datetime.strptime(trade_date, "%Y%m%d")
                 - timedelta(days=30)).strftime("%Y%m%d")
    except (TypeError, ValueError):
        return out
    began = time.monotonic()
    for code in codes:
        if time.monotonic() - began > _PCT_BUDGET:
            logger.info("补拉涨跌幅超预算，剩余 %s 显示 '-'", list(codes)[len(out):])
            break
        try:
            df = quotes.daily_history(code, start, trade_date)
        except Exception as exc:  # noqa: BLE001
            logger.debug("补拉日线 %s 失败: %s", code, exc)
            continue
        if df is None or df.empty:
            continue
        last = df.iloc[-1]
        if str(last.get("date")) != trade_date:
            continue  # 最后一根不是目标日（停牌/源数据旧），不能当当日涨跌幅
        try:
            value = float(last.get("pct_chg"))
        except (TypeError, ValueError):
            continue
        if not math.isnan(value):
            out[code] = value
    return out


def _inflow_by_code(codes: Sequence[str], end_date: str, days: int = 5
                    ) -> Dict[str, Dict[str, Any]]:
    """每个 code 在**固定窗口**内的主力净流入合计（元）+ 实际覆盖天数。

    窗口 = ``end_date`` 及之前、库内已有的最近 `days` 个交易日，见
    ``mainforce.inflow_window``。**主力建仓段、关注段、飞书「查询」、网页端弹框
    四处共用这一个函数** —— 口径不一致时，同一只股票在推送和页面上会给出两个数。

    别改成「按该股自己的记录取最近 N 条」来凑满 N 天：那样缺一天就往后够，
    够多远没有上限。实测 20260929 抓取失败（全库仅 30 行），荣盛发展缺当天记录，
    就够到 6 个交易日之前的 20260924，把那天 -1.01 亿 的流出算进「近5日」，
    显示 +2670万 而页面显示 +1.28亿。固定窗口只会在有缺口时**少算**，
    `days` 如实反映覆盖了几眼，缺口由 ``inflow_gaps`` 报给网页端去回补。

    `end_date` 为空（飞书指令拿不到交易日）时退回库内最新一天，不返回全 0。
    """
    picked: Dict[str, Dict[str, Any]] = {
        c: {"total": 0.0, "days": 0} for c in codes if c}
    if not picked:
        return {}
    if not end_date:
        end_date = db.latest_date("stock_net_inflow") or ""
    wanted = set(picked)
    for day in mainforce.inflow_window(end_date, days):
        for r in db.load("stock_net_inflow", day) or []:
            code = r.get("code")
            if code in wanted and isinstance(r.get("net_inflow"), (int, float)):
                picked[code]["total"] += float(r["net_inflow"])
                picked[code]["days"] += 1
    return picked


def _inflow_window_days() -> int:
    """打分用的「近 N 日」窗口（scorer.yaml → metrics.inflow_window_days）。

    展示口径必须跟着配置走：哪天把窗口改成 10，标题还写「近5日」就自相矛盾了。
    """
    try:
        metrics = mainforce.load_config().get("metrics") or {}
        return max(1, int(metrics.get("inflow_window_days", 5)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读 inflow_window_days 失败，回退 5：%s", exc)
        return 5


# 分段正文 —— build_digest（跑完推送）与 feishu.py（手机发指令）共用，
# 改文案只需改这里一处。每段返回完整块（含标题行），没数据返回 [] 或提示行。


def mainforce_block(trade_date: str, top: int = 10) -> List[str]:
    """「疑似主力建仓 TopN」分段：每行只有 **名字 + 代码 + 资金量**。

    分数、涨跌幅、理由文本都不推（2026-10-09 拍板）。资金量的口径写进标题行，
    免得裸的「+7523万」被当成成交额；实际覆盖不足窗口时不逐行标注，
    保持每行长度一致。
    """
    mf_date = db.latest_date("main_force")
    mf = db.load_one("main_force", mf_date) if mf_date else None
    rows = (mf or {}).get("rows") or []
    if not rows:
        return ["【疑似主力建仓】暂无打分数据（打分步骤可能失败）"]
    tag = "" if mf_date == trade_date else f"（打分日 {mf_date}，非目标日）"
    picked = rows[:top]
    window = _inflow_window_days()
    inflow = _inflow_by_code([r.get("code") or "" for r in picked],
                             mf_date or trade_date, window)
    out = [f"【疑似主力建仓 Top{top}】共 {len(rows)} 只"
           f" · 资金量=近{window}日主力净流入{tag}"]
    for i, r in enumerate(picked, 1):
        code = r.get("code") or ""
        got = inflow.get(code) or {}
        head = f"{i:>2}. {r.get('name') or code}({code})"
        total = got.get("total")
        if not isinstance(total, (int, float)) or not got.get("days"):
            out.append(f"{head} -")
            continue
        out.append(f"{head} {_money(total)}")
    return out


def sector_block(top: int = 5) -> List[str]:
    """「板块资金流 · 行业」分段：净流入/净流出各 TopN。纯 DB，口径亿元。"""
    sf = [r for r in (db.load("sector_flow_industry", "latest") or [])
          if isinstance(r.get("net_inflow"), (int, float))
          and not isinstance(r.get("net_inflow"), bool)]
    if not sf:
        return []
    sf.sort(key=lambda r: r["net_inflow"], reverse=True)
    out = [f"【板块资金流 · 行业】{len(sf)} 个板块", "净流入 TOP5"]
    for r in sf[:top]:
        out.append(f"  {_yi(r['net_inflow'])} {_pct(r.get('pct_chg'))} "
                   f"{r.get('name')}")
    out.append("净流出 TOP5")
    for r in sf[-top:][::-1]:
        out.append(f"  {_yi(r['net_inflow'])} {_pct(r.get('pct_chg'))} "
                   f"{r.get('name')}")
    return out


# 关注列表「缺数就现查」的预算（2026-11-07 拍板）：飞书指令是同步回复，
# 等太久对方以为死了 —— netfix 1s 最小间隔下 10 秒约补 10 只；补不完的
# 下一次接着补（已试过的股有 600s 冷却，不会对着挂掉的源空转）。
_WATCH_FLOW_BUDGET = 10.0
_WATCH_FLOW_MAX = 10


def watch_block(trade_date: str) -> List[str]:
    """「关注股票」分段：当日涨跌幅 + 近 5 日主力净流入。

    资金流缺口**前台现查**（2026-11-07 拍板）：没数据就先补再返回，
    而不是回一句「打开关注页即可回补」把用户支走 —— 那句对港股还是
    永远兑现不了的空头支票（`stock_net_inflow` 原本只有东财 A 股口径）。
    补走 `mainforce.backfill_inflow`，与网页端同一函数同一口径：
    港股（5 位码）经 `quotes.stock_fflow_daykline` 的 `secid=116.*`
    同接口取数（2026-11-07 实测），补完落同一张表、显示同一段文案。
    受 `STOCK_INFLOW_BACKFILL` 统一闸（关 = 一条外网请求都不发，见 docs/02）。
    """
    watch = db.watch_list()
    if not watch:
        return []
    codes = [w["code"] for w in watch]
    pct_of = _watch_pct(trade_date, codes)
    window = _inflow_window_days()
    flow_of = _inflow_by_code(codes, trade_date)
    # 现查锚点：优先指令给的交易日，拿不到退回库内最新资金流日
    end = trade_date or db.latest_date("stock_net_inflow") or ""
    if end and settings.inflow_backfill:
        _, lack = mainforce.inflow_gaps(codes, end, window)
        if lack:
            mainforce.backfill_inflow(
                lack, end, window,
                budget_sec=_WATCH_FLOW_BUDGET, max_codes=_WATCH_FLOW_MAX)
            flow_of = _inflow_by_code(codes, trade_date)
    out = [f"【关注股票】{len(watch)} 只 · 当日涨跌幅 / 主力净流入"]
    for w in watch:
        code = w.get("code") or ""
        name = w.get("name") or code
        got = flow_of.get(code) or {}
        if got.get("days"):
            flow = (f"近{got['days']}日 {_money(got['total'])}"
                    if got["days"] < window else f"近{window}日 {_money(got['total'])}")
        else:
            # 现查开着才有「再发一次」的指望；开关关着时不许开空头支票
            flow = ("资金流暂缺（稍后再发一次「关注列表」可现查）"
                    if settings.inflow_backfill else "资金流暂缺")
        out.append(f"· {name}({code}) {_pct(pct_of.get(code))} · {flow}")
    return out


def inflow_summary(trade_date: str, codes: Sequence[str],
                   days: int = 5) -> Dict[str, Dict[str, Any]]:
    """近 `days` 日主力净流入合计（摘要正文与飞书「查询」共用的公开入口）。"""
    return _inflow_by_code(codes, trade_date, days)


def pool_block(trade_date: str) -> List[str]:
    """「涨跌停池」分段：四个池子的计数 + 最高连板。无数据时给提示行。"""
    counts = {k: len(db.load(f"pool_{k}", trade_date) or [])
              for k in ("zt", "zb", "dt", "strong")}
    if not any(counts.values()):
        return [f"{trade_date} 无股池数据（抓取可能失败，见运行历史）"]
    out = [f"涨停 {counts['zt']} · 炸板 {counts['zb']} · "
           f"跌停 {counts['dt']} · 强势 {counts['strong']}"]
    zt_rows = [r for r in db.load("pool_zt", trade_date) or [] if r.get("code")]
    if zt_rows:
        top = max(zt_rows, key=lambda r: int(r.get("boards") or 1) or 1)
        boards = int(top.get("boards") or 1) or 1
        if boards >= 2:
            out.append(f"最高连板 {boards} 板：{top.get('name')}({top['code']})")
    return out


def _blocks_for(trade_date: str, failed: Sequence[str]
                ) -> Dict[str, Callable[[], List[str]]]:
    """分段 key → 惰性构造器。**用 lambda 是有意的**：没勾的段连数据都不去查 ——
    ``watch_block`` 里有补拉日线的网络调用（带 20s 预算），不勾就不该等它。"""
    return {
        "pools": lambda: pool_block(trade_date),
        "mainforce": lambda: mainforce_block(trade_date),
        "sector": lambda: sector_block(),
        "watch": lambda: watch_block(trade_date),
        "failed": lambda: (["失败步骤：" + "、".join(failed)] if failed else []),
    }


def _digest_title(trade_date: str) -> str:
    """摘要抬头。**可在「指令管理」页改**：正文里写 `{date}` 会代入当天日期，
    不写就没有日期；清空输入框 = 恢复默认。

    只换抬头这一行 —— 分段正文按 `SECTION_DEFS` 现拼，免责声明由
    `build_digest` 固定追加，模板里没有删它的入口。
    """
    tpl = cmdcfg.tpl_effective("digest")
    if not tpl:
        return f"A股看板 · {_date_label(trade_date)}盘后摘要"
    return tpl.replace("{date}", _date_label(trade_date))


def build_digest(trade_date: str, *, status: str = "",
                 kind: str = "定时任务", failed: Sequence[str] = (),
                 elapsed: Optional[float] = None) -> str:
    """拼一条摘要正文（纯文本，飞书 text 消息）。各段独立降级，缺数据就说明缺。

    只拼「定时任务 → 跑完通知」里勾选的分段（见 ``SECTION_DEFS``），没勾的一段
    都不出现；标题、任务状态行与免责声明是消息框架，不受勾选影响。
    飞书入站的「摘要」指令走的是同一条路，所以两边内容始终一致。
    """
    lines: List[str] = [_digest_title(trade_date)]

    if status:
        head = f"任务：{KIND_LABEL.get(kind, kind)} · {STATUS_LABEL.get(status, status)}"
        spent = _elapsed(elapsed)
        lines.append(f"{head} · 用时 {spent}" if spent else head)

    # ---- 勾选的分段（与飞书入站指令共用同一套正文） ----------------
    builders = _blocks_for(trade_date, failed)
    wanted = set(read_sections())
    for key, _label in SECTION_DEFS:
        if key not in wanted:
            continue
        block = builders[key]()
        if block:
            lines.append("")
            lines.extend(block)

    # ---- 免责声明（框架的一部分，永远在） --------------------------
    lines.append("")
    lines.append("数据来自公开接口，仅供研究参考，不构成投资建议。")
    return "\n".join(lines)


# -------------------------------------------------------------- 任务钩子


def after_job(trade_date: str, status: str, failed: Sequence[str] = (),
              elapsed: Optional[float] = None, kind: str = "scheduled",
              require_enabled: bool = True) -> Dict[str, Any]:
    """定时任务跑完后调用：没开通知直接返回，其余异常全部吞掉只记日志。

    :param require_enabled: 是否受「跑完通知」开关约束。**自动**触发（定时 /
        飞书指令）必须尊重它 —— 那是用户设的「别烦我」；**手动**点「推送摘要」
        传 False，因为人已经在页面上主动点了发送，开关管的是「自动」不是「手动」。
        webhook 地址两种情况都必须有，没地址谁也发不出去。
    """
    conf = config()
    if require_enabled and not conf["enabled"]:
        return {"skipped": "通知未开启"}
    if not conf["webhook"]:
        return {"skipped": "未配置 webhook"}

    try:
        text = build_digest(trade_date, status=status, kind=kind,
                            failed=failed, elapsed=elapsed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("拼装飞书摘要失败")
        return {"skipped": f"拼装失败: {type(exc).__name__}: {exc}"}

    result = send(text)
    if result.get("ok"):
        # 推送用得到关注股的逐日资金流，顺手把库里的缺口补上（后台线程，
        # 预算封顶，失败不影响任何东西）—— 下一条消息的数据就齐了
        _backfill_watch_inflow(trade_date)
    return result


def _backfill_watch_inflow(trade_date: str) -> None:
    if not settings.inflow_backfill:
        # STOCK_INFLOW_BACKFILL=false：内网下这条也会被安全设备拦截记录，一并关掉
        return
    codes = [w["code"] for w in db.watch_list() if w.get("code")]
    if not codes:
        return

    def _worker() -> None:
        try:
            stats = mainforce.backfill_inflow(
                codes, trade_date, 5, budget_sec=40.0, max_codes=30)
            if stats.get("need"):
                logger.info("关注股逐日资金流回补 %s", stats)
        except Exception:  # noqa: BLE001
            logger.exception("关注股逐日资金流回补异常")

    threading.Thread(target=_worker, name="notify-inflow", daemon=True).start()


def preview(trade_date: str, status: str = "test",
            kind: str = "测试发送") -> Dict[str, Any]:
    """按库内数据拼一条正文并立即发送（测试按钮用），把正文一并回给前端预览。"""
    text = build_digest(trade_date, status=status, kind=kind)
    result = send(text)
    return {**result, "text": text, "date": trade_date}
