"""订阅制（docs/06）：谁在订阅期内、能关注几只、14:50 给谁发什么。

职责边界：

  - 本模块只管**业务规则** —— 过期判定、关注限额、估值文案、推送编排
  - 指令解析与授权在 `feishu.py`（它顶层 import 本模块）
  - 落库编排在 `store/db.py`（`record_fund_logs`）

因此本模块**不得**顶层 import `feishu` 或 `api.routes`：前者会成环，后者会把
`routes → subscribe → routes` 拉成闭环。要用 `feishu` 就在函数里延迟导入。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from app import cmdcfg
from app.config import settings
from app.fetchers import fund_estimate as fund_fetch
from app.store import db

logger = logging.getLogger(__name__)

# 免责声明是全站硬口径（README / docs/01 / 各页页脚），推送消息同样要带：
# 只做行情呈现，不给买卖信号。
DISCLAIMER = "数据为盘中估算，仅供参考，不构成任何投资建议。"

#: 收银台的固定提示。**「占位、收不到钱」这句必须留着** —— 少了它，页面上
#: 一个像模像样的「去支付」会让用户以为真能付款，付了钱系统不知道就是纠纷。
PAY_NOTE = "（演示环境：收银台为占位演示，不会真正扣款；开通请联系管理员。）"

_PUSH_KIND = "fund_1450"          # push_log.kind：14:50 盘中估值推送
#: 给 `scheduler` 的漏推告警当判据用（同一个字符串才有意义，别各写各的）。
PUSH_KIND = _PUSH_KIND


# ------------------------------------------------------------------ 订阅态


def _alive(row: Optional[Dict[str, Any]]) -> bool:
    """这条订阅算不算数：`plan='pro'` 且 `expires_at` 还没到。

    **过期是判据本身，不是另一个状态** —— 到期后 `plan` 仍是 'pro'，只是
    `expires_at` 落后于现在，这里读一眼就自然降级成未订阅，不需要定时任务
    去批量改字段（也就不存在「忘了跑过期任务」这个失效模式）。
    """
    if not row:
        return False
    if (row.get("plan") or "") != "pro":
        return False
    exp = row.get("expires_at")
    if not exp:
        return False
    try:
        return datetime.fromisoformat(str(exp)) > datetime.now()
    except ValueError:
        # 时间戳认不出来一律按**未订阅**算。宽放会把脏数据变成永久免费。
        return False


def is_active(open_id: str) -> bool:
    """授权层 L2 用：这个人现在能不能用 `access="sub"` 的指令。"""
    return _alive(db.sub_get((open_id or "").strip()))


def expires_at(open_id: str) -> str:
    row = db.sub_get((open_id or "").strip()) or {}
    return str(row.get("expires_at") or "")[:19]


def limit_of(open_id: str) -> int:
    """生效的关注上限：`subscriber.fund_limit` 优先，空则用全局配置。"""
    row = db.sub_get((open_id or "").strip()) or {}
    own = row.get("fund_limit")
    try:
        if own is not None and int(own) > 0:
            return int(own)
    except (TypeError, ValueError):
        pass
    return max(1, int(settings.fund_watch_limit))


def paywall_text() -> str:
    """未订阅时统一的拦截话术（授权层用）。**入口指令本身不能被它拦住**，
    否则没订阅的人永远拿不到「订阅Pro」，直接死锁。"""
    return "这是订阅功能。请先发「订阅Pro」开通。"


# ------------------------------------------------------------------ 指令


def cmd_subscribe(open_id: str, nickname: str = "") -> str:
    """`订阅Pro` —— 入口指令，任何人可用。

    回的是**这个人自己的收银台链接**（带一次性 token，不是把 open_id 裸放进
    URL）。已在订阅期就只回状态，不重复发链接。
    """
    from app import pay   # 延迟：pay 只依赖 store/config，但别在模块顶层拉

    open_id = (open_id or "").strip()
    row = db.sub_get(open_id)
    if _alive(row):
        assert row is not None
        return "\n".join(filter(None, [
            "你已订阅 Pro。",
            f"到期时间：{expires_at(open_id)}",
            f"金额：{row.get('amount') if row.get('amount') is not None else '—'}",
            f"关注基金：{len(db.sub_funds(open_id))} / {limit_of(open_id)} 只",
            "",
            "到期前会自动降级，续期后再发一次「订阅Pro」即可。",
        ]))

    link = pay.make_link(open_id, nickname)
    plans = pay.plans()
    tail = ""
    if row and row.get("expires_at"):
        tail = f"上次订阅已于 {str(row['expires_at'])[:10]} 到期。"
    lines = [
        "订阅 Pro",
        "",
        # Markdown 链接：`send_text` 认出它就把消息升成富文本，飞书里显示成
        # 中文锚点（裸 URL 在文本消息里只能显示成 URL 本身）。
        f"[点此打开收银台]({link['url']})",
        "",
        "打开链接选月份与支付方式：",
    ]
    lines += [f"  {p['label']}  ¥{p['amount']:g}" for p in plans]
    lines += [
        "",
        f"开通后可用：基金 · 关注基金 · 删除基金 · 基金关注 · "
        f"交易日 {settings.push_hour:02d}:{settings.push_minute:02d} 估值推送",
        tail,
        PAY_NOTE,
        f"链接 {str(link['expires_at'])[:10]} 前有效。",
        DISCLAIMER,
    ]
    return "\n".join(x for x in lines if x != "")


def cmd_activate(open_id: str, amount: Optional[float] = None,
                 nickname: str = "") -> str:
    """`开通 <open_id> [金额]` —— owner 专用的人工开通。

    占位链接没有回调，这是 P0 **唯一**的开通路径：管理员照着收款记录手动开。
    续期从原到期日往后累加（见 `db.sub_activate`）。
    """
    open_id = (open_id or "").strip()
    if not open_id:
        return "用法：开通 <open_id> [金额]"
    row = db.sub_activate(open_id, amount=amount, nickname=nickname)
    return "\n".join(filter(None, [
        f"已开通 {open_id}",
        f"到期：{str(row.get('expires_at') or '')[:19]}",
        f"金额：{row.get('amount')}",
        f"关注上限：{limit_of(open_id)} 只",
    ]))


# ------------------------------------------------------------- 估值文案


def _fmt_pct(v: Any) -> str:
    """`None` 一律显示「—」，**绝不填默认值**（填 0 会把「没取到」和
    「今天真的没涨跌」变成一回事，全站同口径）。"""
    if v is None:
        return "—"
    try:
        return f"{float(v):+.2f}%"
    except (TypeError, ValueError):
        return "—"


def _fmt_nav(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.4f}"
    except (TypeError, ValueError):
        return "—"


def _line(r: Dict[str, Any]) -> str:
    """一只基金一行文本。文本消息没有颜色可用，涨跌全靠 `+/-` 号表达
    （网页端才是红涨绿跌）。"""
    code = str(r.get("code") or "")
    name = (str(r.get("name") or "").strip()) or code
    seg = [f"{name} {code}", _fmt_pct(r.get("estimate_pct"))]
    nav = _fmt_nav(r.get("estimate_nav"))
    if nav != "—":
        seg.append(f"估值 {nav}")
    as_of = str(r.get("as_of") or "")
    if as_of:
        seg.append(as_of[-8:] if len(as_of) >= 8 else as_of)
    line = "  " + "  ".join(seg)
    if r.get("stale"):
        line += "（非今日估值）"
    if not (r.get("source") or ""):
        line += "（没取到）"
    return line


def _header() -> str:
    """14:50 抬头文案。**可在「指令管理」页改，清空输入框 = 恢复默认**。

    改的只有这一行抬头 —— 数据行由 `_line()` 现拼，免责声明由推送正文固定
    追加（`DISCLAIMER` 不进模板，页面上没有删它的入口）。
    """
    return cmdcfg.tpl_effective("fund_1450")


def _fetch(codes: List[str]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """按 code 建索引，顺带落 T+1 复盘流水、回填基金名。"""
    if not codes:
        return {}, []
    rows, warns = fund_fetch.estimate([{"code": c} for c in codes])
    if rows:
        # 与「查询估值」共用同一条流水：白攒 T+1 复盘样本（docs/06 §2）
        db.record_fund_logs(rows)
        for r in rows:
            if r.get("name"):
                db.sub_fund_fill_name(str(r.get("code") or ""), str(r["name"]))
    return {str(r.get("code") or ""): r for r in rows}, warns


def cmd_estimate(code: str, allow: bool = True) -> str:
    """`基金 <6位>` —— 实时估值。要发网络，所以也要看 `allow`。"""
    code = (code or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return f"基金代码应为 6 位数字，收到 {code!r}。"
    if not allow:
        return f"（试一下）会查询基金 {code} 的实时估值（发一次网络请求）。"
    rows, warns = _fetch([code])
    r = rows.get(code)
    if r is None or not (r.get("source") or ""):
        return "\n".join(filter(None, [
            f"基金 {code} 估值没取到。",
            "；".join(warns)[:300] or "",
        ]))
    db.sub_fund_fill_name(code, str(r.get("name") or ""))
    extra = str(r.get("note") or "")
    return "\n".join(filter(None, [
        _line(r),
        f"来源：{r.get('source')}",
        extra[:200],
        DISCLAIMER,
    ]))


def cmd_add(open_id: str, code: str, allow: bool = True) -> str:
    """`关注基金 <6位>` —— 写 `subscriber_fund`，先过限额。"""
    open_id = (open_id or "").strip()
    code = (code or "").strip()
    if not allow:
        return (f"（试一下）会把基金 {code} 加进你的关注"
                f"（上限 {limit_of(open_id)} 只）。")
    limit = limit_of(open_id)
    funds = db.sub_funds(open_id)
    if any(f.get("code") == code for f in funds):
        return f"{code} 已经在你的关注里（{len(funds)}/{limit}）。"
    if len(funds) >= limit:
        # 需求 §1.6 的「关注数量超出」提示
        return (f"关注数量超出：你已关注 {len(funds)} 只，上限 {limit} 只。\n"
                f"先发「删除基金 <代码>」腾出位置。")
    db.sub_upsert(open_id)
    if not db.sub_fund_add(open_id, code):
        return f"{code} 已经在你的关注里。"
    return (f"已关注 {code}（{len(funds) + 1}/{limit}）。\n"
            f"发「基金 {code}」看实时估值，发「基金关注」看全部。")


def cmd_remove(open_id: str, code: str, allow: bool = True) -> str:
    """`删除基金 <6位>` —— 只删这个人自己的，不碰全局 `fund_watchlist`。"""
    open_id = (open_id or "").strip()
    code = (code or "").strip()
    if not allow:
        return f"（试一下）会取消关注基金 {code}。"
    if not db.sub_fund_remove(open_id, code):
        return f"{code} 不在你的关注里。"
    left = len(db.sub_funds(open_id))
    return f"已取消关注 {code}（还剩 {left}/{limit_of(open_id)} 只）。"


def cmd_my_funds(open_id: str, allow: bool = True) -> str:
    """`基金关注` —— 列出本人关注 + 一次拉全部估值。"""
    open_id = (open_id or "").strip()
    funds = db.sub_funds(open_id)
    if not funds:
        return "你还没关注基金。发「关注基金 161725」加上。"
    if not allow:
        return (f"（试一下）会查询这 {len(funds)} 只基金的实时估值"
                f"（上限 {limit_of(open_id)} 只）。")
    rows, _warns = _fetch([f["code"] for f in funds])
    out = [f"我的关注基金（{len(funds)}/{limit_of(open_id)} 只）"]
    missed = 0
    for f in funds:
        r = rows.get(f.get("code") or "")
        if r is None:
            missed += 1
            nm = (f.get("name") or "").strip() or f.get("code")
            out.append(f"  {nm} {f.get('code')}  —（没取到）")
        else:
            out.append(_line(r))
    out += ["", DISCLAIMER]
    if missed:
        out.insert(-2, f"（{missed} 只没取到，接口可能在维护）")
    return "\n".join(out)


# ------------------------------------------------------- 14:50 盘中推送


def due_open_ids() -> List[str]:
    """这次要推给谁：订阅期内 **且** 关注了基金的。没关注的不发，不骚扰。"""
    out: List[str] = []
    for row in db.sub_list():
        oid = str(row.get("open_id") or "")
        if oid and _alive(row) and db.sub_funds(oid):
            out.append(oid)
    return out


def _send(feishu_mod: Any, open_id: str, text: str) -> bool:
    """私发给这个人（`open_id` 单聊），失败重试 1 次。

    走 `feishu.send_text` 而不是 `notify.send` —— 后者是自定义机器人 webhook，
    只能发到它所在的**固定一个群**，按人分发用不了它。
    """
    for attempt in range(2):
        if feishu_mod.send_text(open_id, text, id_type="open_id"):
            return True
        logger.warning("14:50 推送给 %s 第 %d 次失败", open_id, attempt + 1)
    return False


def run_push() -> Dict[str, Any]:
    """14:50 推送编排，由 `scheduler` 在交易日触发。

    所有人的关注**并起来只查一次**（省请求、也省得同一个 code 被反复拉），
    再按人拆开私发；每次发送都写 `push_log`，成败与原因留痕给订阅管理页。
    """
    from app import feishu as feishu_mod   # 延迟：feishu 顶层 import 本模块

    targets = due_open_ids()
    stat: Dict[str, Any] = {"targets": len(targets), "sent": 0, "failed": 0}
    if not targets:
        logger.info("14:50 推送：没有需要推送的订阅者")
        return stat

    codes: List[str] = []
    seen = set()
    per_user: Dict[str, List[Dict[str, Any]]] = {}
    for oid in targets:
        funds = db.sub_funds(oid)
        per_user[oid] = funds
        for f in funds:
            c = str(f.get("code") or "")
            if c and c not in seen:
                seen.add(c)
                codes.append(c)

    rows_by_code, warns = _fetch(codes)
    if warns:
        logger.info("14:50 估值告警：%s", "；".join(warns)[:300])

    for oid in targets:
        out = [_header(), ""]
        missed = 0
        for f in per_user[oid]:
            r = rows_by_code.get(str(f.get("code") or ""))
            if r is None:
                missed += 1
                nm = (f.get("name") or "").strip() or f.get("code")
                out.append(f"  {nm} {f.get('code')}  —（没取到）")
            else:
                out.append(_line(r))
        out += ["", DISCLAIMER]
        if missed:
            out.insert(-2, f"（{missed} 只没取到）")
        ok = _send(feishu_mod, oid, "\n".join(out))
        db.push_log_add(oid, _PUSH_KIND, ok,
                        "" if ok else "发送失败（重试 1 次后仍失败）")
        if ok:
            stat["sent"] += 1
        else:
            stat["failed"] += 1
    logger.info("14:50 推送完成 目标=%d 成功=%d 失败=%d",
                stat["targets"], stat["sent"], stat["failed"])
    return stat
