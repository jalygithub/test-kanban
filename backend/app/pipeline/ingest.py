# -*- coding: utf-8 -*-
"""逐域采集状态机、重试与校验闸门（阶段 3）。

三件事，都在**现有 `scheduler.py` 之上**加，不另起一套调度：

1. **逐域状态**：每个 (数据域, 交易日) 一行，跑完就落 `SUCCESS` / `PARTIAL`
   / `FAILED`，中途挂了退成 `WAITING` 等重试。`job_run` 那张整轮流水线的表
   照旧不动 —— 它回答「这轮跑完了没」，这张回答「pool_zt 那一天到底怎么了」。
2. **有上限的重试 + 指数退避**：源挂着的时候无限重试会把请求打爆，也会让
   「最近一次错误」永远被下一次失败盖掉，页面上就再也看不到真正的原因了。
3. **校验闸门**：**没验证过的数据不许标成功。** 这是任务书的原话，也是这一
   整阶段最要紧的一条。

## 闸门为什么是「保守」而不是「准确」

判 `PARTIAL` 用的是「有没有把数据采全」，不是「源有没有抛异常」——
这两件事在过去被混成了一个。`_finish()` 原本的逻辑是：只要每一步都没抛
异常就判 `success`，于是 `{"pool_zt": 0}` 也照样是 success（真库 job_run #18
就是这么记的）。那一天到底真的一只涨停都没有，还是源只回了空壳，从记录上
**完全分不出来**。

本模块一律按「0 行不算采全」处理。这会在**跌停数天然为 0** 的强市日造成
误报 —— 这是**故意偏保守**的方向：宁可多标一次「没验证过」，也不让一个
读到空列表的人以为那天真的一个涨停都没有。哪个域确实允许为空，显式传
``allow_empty=True``，把这个判断写在调用处、留下痕迹，而不是在这里猜。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.store import db

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 状态
WAITING = "WAITING"      # 排队未跑 / 失败后等重试
RUNNING = "RUNNING"      # 正在跑
SUCCESS = "SUCCESS"      # 跑完且通过校验
PARTIAL = "PARTIAL"      # 跑完但数据不完整
FAILED = "FAILED"        # 抛异常，或重试用尽

#: 库内状态 → 任务书要求的对外状态词。**库内枚举一个都不改**：这五个值已经
#: 写进 12 份回归、覆盖审计和前端标签，改名等于让所有历史记录变成认不出的
#: 值。对外多给一层 `status_alias`，两头都对得上，旧接口一个字节都没变。
API_STATUS = {
    WAITING: "pending",           # 排队未跑 / 失败后等重试
    RUNNING: "running",
    SUCCESS: "success",
    PARTIAL: "partial_success",   # 跑成了但数据不全 —— 单列一个，不并进
                                  # success（会让人以为可以放心用），也不并
                                  # 进 failed（那要查的是源，不是网络）
    FAILED: "failed",
}


def api_status(status: Optional[str]) -> str:
    """库内状态 → 对外状态词。**认不出的原样回给**，不猜 —— 把一个将来新增
    的状态硬说成 `failed`，页面上会比实际情况更糟。"""
    return API_STATUS.get(status or "", status or "")

#: 退避基数与上限（秒）。attempts=1 失败后等 15s，2 失败后 30s，往后翻倍，
#: 最长 5 分钟 —— 超过这个数再等下去，「补跑」这件事本身已经没有意义了，
#: 人该去看一眼错误是什么。
_BACKOFF_BASE = 15
_BACKOFF_CAP = 300


def backoff_seconds(attempts: int) -> int:
    """第 n 次失败后等多久再试（指数退避 + 上限）。"""
    n = max(1, int(attempts))
    return min(_BACKOFF_CAP, _BACKOFF_BASE * (2 ** (n - 1)))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _retry_at(attempts: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc)
            + timedelta(seconds=backoff_seconds(attempts))).isoformat(
                timespec="seconds")


# -------------------------------------------------------------- 校验闸门


def validate_rows(rows: Sequence[Dict[str, Any]],
                  required: Sequence[str] = ()) -> Tuple[int, int, List[str]]:
    """行级校验，返回 ``(总数, 没通过的行数, 原因列表)``。

    **不修数据**。缺字段就记下来让上层判 PARTIAL，绝不补默认值 ——
    任务书「不得把缺失填成 0」在这里落地。``required`` 为空时只做最基本的一条：
    每行得是个 dict 且有 ``code``（板块资金流没有 code，用 ``name``）。

    三类判据，都只报不改（任务书阶段 3 的「四类校验」里能在这层落地的三类，
    第四类「交易日期」是任务级的，由调用方钉住 ``trade_date``）：

    1. **缺必填字段** —— 原有。
    2. **重复记录** —— 同一个 ``code`` 出现多次。同一天同一只票只该有一条；
       重了说明上游拼了两批，条数会虚高、覆盖率会虚低。
    3. **关键价格非正** —— ``price`` / ``close`` 是数字且 ``<= 0``。价格是数字
       却为 0 或负，是**坏数据**，不是「没采到」（没采到的是 None，那条由
       必填字段判据去管，且**只报不改**）。
    """
    total = bad = 0
    missing: Dict[str, int] = {}
    dup = bad_price = 0
    seen: set = set()
    for item in rows:
        total += 1
        if not isinstance(item, dict):
            bad += 1
            missing["<非对象行>"] = missing.get("<非对象行>", 0) + 1
            continue
        # 调用方没给必填字段时，按行的形状推：有 code 就认 code，只有 name
        # 的（板块资金流）就认 name，两者都没有就不强求 —— 不硬塞一个键名
        # 进去，否则整批会被判成缺字段，闸门直接失效。
        if required:
            keys: Tuple[str, ...] = tuple(required)
        elif "code" in item:
            keys = ("code",)
        elif "name" in item:
            keys = ("name",)
        else:
            keys = ()
        absent = [k for k in keys if item.get(k) in (None, "")]
        if absent:
            bad += 1
            for k in absent:
                missing[k] = missing.get(k, 0) + 1
            # 缺字段的行不再往下判重复/价格：一次只报一个原因，人排查时
            # 「这行缺 code」比「这行缺 code 且价格非正」有用得多。
            continue
        code = item.get("code")
        if code:
            if code in seen:
                dup += 1
            else:
                seen.add(code)
        for price_key in ("price", "close"):
            value = item.get(price_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and value <= 0:
                bad_price += 1
                break
    reasons = ["%d 行缺字段 %s" % (n, k) for k, n in sorted(missing.items())]
    if dup:
        reasons.append("%d 行代码重复（同一只票同一天出现多次）" % dup)
    if bad_price:
        reasons.append("%d 行关键价格非正（price/close <= 0）" % bad_price)
    return total, bad + dup + bad_price, reasons


def gate(domain: str, stored: int, failed_rows: int,
         allow_empty: bool = False,
         min_rows: Optional[int] = None) -> Tuple[str, List[str]]:
    """数据闸门：这一域这一天**配不配**标 SUCCESS。返回 ``(status, 原因)``。

    两条判据，任一不过即 PARTIAL：

    1. ``stored == 0`` —— 落库 0 行。没数据就没有「采全了」这回事，
       哪怕源一次异常都没抛（那正是 job_run #18 踩过的坑）。
    2. ``stored < min_rows`` —— 条数低于该域的下限。

    ``failed_rows > 0`` 由调用方叠加，这里不管。
    """
    reasons: List[str] = []
    if stored == 0 and not allow_empty:
        reasons.append("落库 0 行（源未抛异常，但没有任何数据）")
    if min_rows is not None and 0 < stored < min_rows:
        reasons.append("仅 %d 行，低于下限 %d" % (stored, min_rows))
    return (PARTIAL if reasons else SUCCESS), reasons


# --------------------------------------------------------------- 主流程


def record(domain: str, trade_date: str,
           rows: Sequence[Dict[str, Any]],
           source: str = "",
           required: Sequence[str] = (),
           allow_empty: bool = False,
           min_rows: Optional[int] = None,
           store: bool = True,
           coverage_extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """校验 → 落库 → 登记状态。一步走完，不会出现「存了但没记状态」。

    :param rows: 源返回的行。**原样落库**，不筛不补 —— 校验只报不改。
    :param store: False 时**只校验、只记状态，不碰 snapshot**。

        `main_force` 必须走这条：`mainforce.run()` 自己落的是**包型** payload
        （`[{rows, all_rows, params, model}]`），这里再 `db.save` 一次会用一个
        裸行列表把整个包覆盖掉 —— `all_rows`、`params`、`model` 当场没了，
        研究模块的可重放校验（`replay_score`）也就跟着废了。
        这类「取数方自己负责落库」的域，一律传 ``store=False``。

    :param coverage_extra: 播进 ``coverage_detail`` 的任务级补充（资金流任务的
        「名单数 vs 排名返回数」就走这条）。**同名键以本函数算出的为准** ——
        调用方不能拿它把 processed/stored/failed_rows 覆盖掉，那三个数只能由
        校验自己出，否则「记了 5000 行、实际存了 3000」这种账会对外一致地错。

    :return: ``{status, stored, failed_rows, reasons, coverage_rate}``

    注意这里**不会**调 ``ingest_start``：调用方（`scheduler._collect_domain`）
    在真正发请求之前就该把状态置成 RUNNING，否则页面上「正在抓取」那一栏
    整个抓取期间都会是空的。
    """
    total, bad, why = validate_rows(rows, required)
    status, reasons = gate(domain, total, failed_rows=bad,
                           allow_empty=allow_empty, min_rows=min_rows)
    reasons = list(reasons) + list(why)
    if bad:
        reasons.append("%d/%d 行未通过校验" % (bad, total))
        # 没通过校验的行也是「没采全」。`gate()` 只看行数（它没有 failed_rows
        # 的判据可写），降级必须在这里做 —— 漏了这一句，10 行里有 5 行缺
        # code 也照样标 SUCCESS，闸门等于没有。重复记录、关键价格非正同理。
        if status == SUCCESS:
            status = PARTIAL

    stored = total if not store else db.save(domain, trade_date, list(rows),
                                             source=source)
    # 覆盖范围/校验结果落进任务记录（任务书阶段 2 要求的字段之一）。
    #
    # coverage_rate 这里算的是**本次的字段校验通过率**，不是「日期窗口盖了
    # 多少天」—— 后者要扫整张 snapshot 表（见 pipeline.coverage），放不进
    # 每次采集的收尾。窗口那一半由阶段 3 的 `data_quality_check` 回填进
    # coverage_detail.window，两件事各归各的，不搅成一个分不清的数。
    #
    # total == 0 时给 None 而不是 0.0：「一行都没有」和「覆盖率是零」是两
    # 件事，把前者写成后者等于替数据下了一个判断。
    validated = total - bad
    coverage_rate = (validated / total) if total else None
    # coverage_extra 在**后面**合，再把校验自己数出来的几个键写回去 —— 顺序
    # 反了就等于让调用方替校验报数（那正是「记 5000、存 3000」的来源）。
    detail: Dict[str, Any] = {}
    if coverage_extra:
        detail.update(coverage_extra)
    detail.update({
        "processed": total, "stored": stored,
        "failed_rows": bad, "validated": validated,
        "required": list(required) or None,
        "reasons": reasons, "source": source or None,
    })
    db.ingest_finish(domain, trade_date, status,
                     processed=total, stored=stored,
                     failed_rows=bad, error="; ".join(reasons) or None,
                     coverage_rate=coverage_rate,
                     coverage_detail=detail,
                     # 域级也记一份来源（不只是塞进 detail 的 JSON）：页面要按
                     # 域筛「哪些是东财来的」，让人去 LIKE 一段 JSON 是不能用的。
                     source=source or None)
    if status != SUCCESS:
        logger.warning("采集闸门判 %s @ %s：%s", domain, trade_date,
                       "; ".join(reasons))
    return {"status": status, "stored": stored, "failed_rows": bad,
            "reasons": reasons, "coverage_rate": coverage_rate}


def record_failure(domain: str, trade_date: str, exc: BaseException,
                   ) -> Dict[str, Any]:
    """一步抛了异常：按剩余重试次数决定退成 WAITING 还是 FAILED。

    ``attempts`` 已经在 ``ingest_start`` 里 +1，这里只读不写 —— 两个地方各加
    一次会把实际重试次数记成两倍，退避就永远等不到点。
    """
    row = db.ingest_get(domain, trade_date) or {}
    attempts = int(row.get("attempts") or 0)
    cap = int(row.get("max_attempts") or 3)
    err = f"{type(exc).__name__}: {exc}"

    if attempts >= cap:
        db.ingest_finish(domain, trade_date, FAILED, error=err)
        db.ingest_set_retry(domain, trade_date, None)
        logger.error("采集失败（第 %d/%d 次，不再重试）%s @ %s：%s",
                     attempts, cap, domain, trade_date, err)
        return {"status": FAILED, "attempts": attempts, "max_attempts": cap,
                "error": err, "retry_at": None}

    when = _retry_at(attempts)
    db.ingest_finish(domain, trade_date, WAITING, error=err)
    db.ingest_set_retry(domain, trade_date, when)
    logger.warning("采集失败（第 %d/%d 次），%d 秒后重试 %s @ %s：%s",
                   attempts, cap, backoff_seconds(attempts),
                   domain, trade_date, err)
    return {"status": WAITING, "attempts": attempts, "max_attempts": cap,
            "error": err, "retry_at": when}


# ------------------------------------------------------------- 漏采检查


def recent_gaps(lookback: int = 30) -> Dict[str, Any]:
    """近期漏采 —— **只读、零联网**，直接复用覆盖审计的同一份事实。

    两处都用 `pipeline.coverage` 的 `gaps()`：调度侧说「漏了 3 天」和数据
    管理页说「漏了 3 天」必须是同一个数字。另起一套算法就会各说各话。
    """
    from app.pipeline import coverage
    gaps = coverage.gaps(lookback=lookback)
    due = db.ingest_due()
    return {
        "lookback": lookback,
        "gaps": gaps,
        # 到点该重试的：**不自动跑**，只摆出来让人决定（见 scheduler._startup_check）
        "retry_due": due,
        "offline": True,
    }


def startup_check() -> Dict[str, Any]:
    """启动时的漏采自检。**只提醒，不补跑。**

    口径与 `scheduler._warn_if_missed_today` 一致：未经告知不主动做事。
    自动补跑历史漏采日期是一次会打十几分钟网络的批量请求，必须由人发起。
    """
    out = recent_gaps(lookback=30)
    total = sum(len(v) for v in out["gaps"].values())
    if total:
        worst = max(out["gaps"].items(), key=lambda kv: len(kv[1]))
        logger.warning(
            "启动自检：近 %d 个交易日内共 %d 处漏采（最多的是 %s 的 %d 天）。"
            "不会自动补跑 —— 需要补请调 POST /api/pipeline/collect。",
            out["lookback"], total, worst[0], len(worst[1]))
    if out["retry_due"]:
        logger.warning("启动自检：有 %d 条采集任务到点待重试（不会自动跑）。",
                       len(out["retry_due"]))
    out["gaps_total"] = total
    return out


# ------------------------------------------------- 最近一次成功与数据质量
#
# 阶段 4 要的「最近一次成功交易日及数据质量状态」。与 `recent_gaps()` 分工：
# 那个答「缺了哪几天」（按日期看缺口），这个答「最近哪天是好的、每个域现在
# 什么状态」（按状态看现状）。两个都要 —— 只有缺口的人不知道现在能不能用，
# 只有现状的人不知道历史上漏了什么。

#: 聚合最多看这么多行。`ingest_list` 新的在前，「最近」永远在最前面；更早的
#: 日子再往前翻也答不出「最近」，扫全表只是白花时间（11 个域 × 约 90 个交易日）。
_HEALTH_ROWS = 1000


def health_report() -> Dict[str, Any]:
    """最近一次成功交易日 + 各域数据质量状态。**只读，零联网。**

    三条判据写在明处，因为换一个判据就是另一个答案：

    - **成功 = 那天有记录的域全是 `SUCCESS`**。`PARTIAL` 不算 —— 它的含义正是
      「跑成了但数据不全」，算进去会让「最近一次成功」在一个只回了半张榜的
      日子亮绿灯，比没有这个字段更坏；
    - **那天没记录的域不算失败**：阶段 3 那三个域在更早的日子一行都没有，
      要求「全域都在」会让这个字段永远是空。覆盖到几个核心域由
      ``last_success.covered / core`` 说，``complete`` 如实给 true/false；
    - **质量状态取 `coverage_detail.window`**，不在这现算 —— 现算要逐行解析整张
      snapshot，每天 5,400 行的 `daily_market` 会让一次页面刷新卡住。没跑过
      ``data_quality_check`` 就是 `None`：**「没查过」和「查过没问题」必须分得开**。
    """
    from app.pipeline import coverage           # 惰性：本模块是它的下游常量来源
    core = sorted(coverage._SNAP_KINDS)
    rows = db.ingest_list(limit=_HEALTH_ROWS)
    latest: Dict[str, Dict[str, Any]] = {}
    by_date: Dict[str, Dict[str, Optional[str]]] = {}
    for r in rows:
        dom = str(r.get("domain") or "")
        day = str(r.get("trade_date") or "")
        if not (dom and day):
            continue
        by_date.setdefault(day, {})[dom] = r.get("status")
        cur = latest.get(dom)
        if cur is None or day > str(cur.get("trade_date") or ""):
            latest[dom] = r

    last_success: Optional[Dict[str, Any]] = None
    for day in sorted(by_date, reverse=True):
        st = by_date[day]
        if st and all(v == SUCCESS for v in st.values()):
            covered = sorted(st)
            last_success = {
                "trade_date": day,
                "domains": covered,
                "covered": len(covered),
                "core": len(core),
                # 那天有记录的域是不是就是全部核心域 —— 不是就 false。免得
                # 「成功」只覆盖跑了两个域、页面上却当整轮全绿显示。
                "complete": set(covered) >= set(core),
            }
            break

    domains: List[Dict[str, Any]] = []
    for dom in core:
        r = latest.get(dom)
        if not r:
            # 从没采过：给 `collected=false`，**不发明第六个状态词**。
            # 状态词仍只有那五个（`API_STATUS`），新造一个「未采集」会让
            # 下一个人以为库里存过这个值。
            domains.append({"domain": dom, "collected": False,
                            "trade_date": None, "status": None,
                            "status_alias": None, "coverage_rate": None,
                            "source": None, "attempts": None, "error": None,
                            "window": None})
            continue
        detail: Dict[str, Any] = {}
        raw = r.get("coverage_detail")
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    detail = loaded
            except (TypeError, ValueError):
                detail = {}          # 某一行的 detail 坏了不该让整个端点 500
        win = detail.get("window")
        domains.append({
            "domain": dom, "collected": True,
            "trade_date": r.get("trade_date"),
            "status": r.get("status"),
            "status_alias": api_status(r.get("status")),
            "coverage_rate": r.get("coverage_rate"),
            "source": r.get("source"),
            "attempts": r.get("attempts"),
            "error": r.get("error"),
            "window": win if isinstance(win, dict) else None,
        })

    try:
        from app import scheduler        # 惰性：scheduler 模块级 import 本模块
        retry_check = scheduler.last_retry_check()
    except Exception:                    # noqa: BLE001  查不到不该让端点 500
        retry_check = None
    return {
        "last_success": last_success,
        "last_success_date": (last_success or {}).get("trade_date"),
        "domains": domains,
        "core": len(core),
        # 到点该补跑的条数：**只报不跑**（定时点也只检查，见 scheduler._retry_fire）
        "retry_due": len(db.ingest_due()),
        "retry_check": retry_check,
        "checked_at": _utc_now(),
        "offline": True,
        "note": ("last_success 只认当天全部 SUCCESS；PARTIAL 是「跑成了但数据不"
                 "全」，不算成功。window 由 data_quality_check 回填，None = 没跑"
                 "过质量检查（不是「查过没问题」）。"),
    }


#: 主力榜的输入域（任务书第 8 条，`plan.md` §7 已定「宽松方案」）。
#: 打分**直接消费**这几个域的当日数据 —— 缺哪个，榜上就少一类证据
#: （涨停基因 / 资金流 / 板块背景）。daily_market / fund_flow_rank /
#: daily_universe 是同步域，打分不消费它们，不给这张榜扣不相干的帽子；
#: main_force 本身是榜单域，不在这里自证。
RANK_INPUT_DOMAINS: Tuple[str, ...] = (
    "pool_zt", "pool_zb", "pool_dt", "pool_strong",
    "stock_net_inflow", "sector_flow_industry", "sector_flow_concept",
)


def rank_quality(trade_date: str,
                 domains: Sequence[str] = RANK_INPUT_DOMAINS) -> Dict[str, Any]:
    """当天输入域的采集体检。**只读 `ingest_job`，零联网。**

    任务书第 8 条「不完整数据不会生成误导性的正式选股排名」，宽松方案：
    不拦输出（拦了会让人以为系统坏了，而缺口本身不是打分程序的错），
    但**整榜必须让人一眼看到数据不全** —— 缺了涨停池还照常发一张 Top30，
    读者会把它当成「正式选股结果」。

    三条判据（换个判据就是另一个答案，所以写在明处）：

    - 任一域当天**有记录且非 `SUCCESS`**（PARTIAL / FAILED / RUNNING /
      WAITING 都算）→ `state="incomplete"`，`missing` 逐项给
      `{domain, trade_date, status, coverage_rate}`；
    - 全 `SUCCESS`（或压根没记录）→ `state="ok"`。**那天没记录的域不算
      失败**，与 `health_report` 同口径：没采过是「无人采」的问题，
      `/pipeline/health` 的 `collected=false` 已经管它；在这里再喊一遍
      只会让红条常驻，人对真警告脱敏；
    - `coverage_rate` 原样带出，**NULL 就是 NULL、绝不补 0** —— 0 是
      「一条都没有」，NULL 是「没算」，混了就分不出「空榜」和「没查」。
    """
    missing: List[Dict[str, Any]] = []
    for dom in domains:
        row = db.ingest_get(dom, trade_date)
        if not row:
            continue
        if str(row.get("status") or "").upper() == SUCCESS:
            continue
        missing.append({
            "domain": dom,
            "trade_date": trade_date,
            "status": row.get("status"),
            "coverage_rate": row.get("coverage_rate"),
        })
    return {
        "state": "incomplete" if missing else "ok",
        "missing": missing,
        "checked_at": _utc_now(),
    }
