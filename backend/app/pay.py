"""收银台（docs/06 §2）—— **占位支付，收不到钱**。

用户在飞书发「订阅Pro」拿到一条链接，打开后在页面上选月份与支付方式，
拿到订单号，然后由管理员在 `/subs` 页照订单点确认开通。

## 为什么不做真收款

真收款要三样现在都没有的东西：商户号、支付回调、能被手机访问到的公网地址
（后端目前只监听本机/内网）。在缺这三样的情况下接一个「看起来能付」的入口，
结果就是**用户真付了钱而系统不知道** —— 这是纠纷，不是功能缺失。所以本模块
从下单到确认全程不碰真实支付通道，`去支付` 只生成一张待确认订单。

## 金额口径

`amount_of()` 是**全站唯一算钱的地方** —— 页面显示的金额与落库的金额必须同源，
否则会出现「页面 297、订单 297.00001」这种对不上的账。

定价分三层，**页面改过的优先**（存 `kv` 表，改完即时生效、不用重启）：

    每月单价  unit_price()      没改过 = `settings.pro_price`
    档位      months_options()  没改过 = 解析 `settings.pay_months`（"1,3,6,12"）
    单档价    amounts           某一档单独填了就用它，没填 = 单价 × 月数

所以「金额 = 单价 × 月数、线性不打折」只是**没改时的默认**，不再是写死的规则
（2026-10-14 用户定：每档可单独定价 + 档位可在页面上增删）。改价只影响**新**订单
—— 金额在下单那一刻快照，历史订单与已开通的订阅不会跟着变。
"""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from app.config import settings
from app.store import db

logger = logging.getLogger(__name__)

#: 支付方式。**只做渠道记账** —— 页面上让人选微信还是支付宝，订单里记下来，
#: 真接支付网关时才知道该出哪个码。
CHANNELS = ("wechat", "alipay")
CHANNEL_LABELS = {"wechat": "微信支付", "alipay": "支付宝"}


class PayError(ValueError):
    """下单参数不对。调用方转成 HTTP 4xx，**不要**当成 500 告警刷屏。"""


# ------------------------------------------------------------ 定价（kv 表）
#
# 存储走 `kv` 而不是 `.env`：用户在页面上点的开关本来就该存库里（见 `db.kv_get`
# 上面那段），而且 `.env` 改了要重启才读得到，页面上改价却要重启是说不通的。

_PRICING_KEY = "pay_pricing"


def _pricing() -> Dict[str, Any]:
    """页面改过的定价原样读出来。没存过 / 解析不出来 → `{}` = 全部回退 `settings`。

    **解析失败必须回退而不是抛** —— 定价那一行写坏了就让整条「订阅Pro」链路挂掉，
    代价远大于「先按默认价卖着」。与 `months_options`「档位写错退回 1 个月」同一态度。
    """
    raw = db.kv_get(_PRICING_KEY, "") or ""
    if not raw:
        return {}
    try:
        saved = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("pay_pricing 解析失败，回退 settings 默认值：%s", raw[:80])
        return {}
    return saved if isinstance(saved, dict) else {}


def _clean_months(items: Iterable[Any]) -> List[int]:
    """档位清洗：正整数、去重、保持输入顺序。"""
    out: List[int] = []
    for it in items:
        try:
            n = int(str(it).strip())
        except (TypeError, ValueError):
            logger.warning("档位里有非数字项：%r", it)
            continue
        if n > 0 and n not in out:
            out.append(n)
    return out


def _amounts(pricing: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """单独定过价的档位 `{月数: 金额}`，**只回有效的**。

    不是正数的一律丢掉：页面手抖存进 0 或负数会让收银台显示「3 个月 ¥0」，
    比没有这个定价更糟。
    """
    src = (pricing if pricing is not None else _pricing()).get("amounts")
    out: Dict[str, float] = {}
    if isinstance(src, dict):
        for k, v in src.items():
            try:
                m = int(str(k))
                n = float(v)
            except (TypeError, ValueError):
                continue
            if m > 0 and n > 0:
                out[str(m)] = n
    return out


def unit_price(pricing: Optional[Dict[str, Any]] = None) -> float:
    """每月单价。页面改过的优先，没改过用全局 `pro_price`。"""
    p = pricing if pricing is not None else _pricing()
    try:
        v = float(p.get("price"))
    except (TypeError, ValueError):
        return float(settings.pro_price)
    return v if v > 0 else float(settings.pro_price)


def months_options() -> List[int]:
    """可选的订阅月数档位。页面改过的优先（JSON 数组），其次配置里写死的
    `"1,3,6,12"`。两处都空 → 退回 1 个月 ——
    档位写错不该让整条「订阅Pro」链路挂掉。"""
    raw = _pricing().get("months")
    if isinstance(raw, list):
        edited = _clean_months(raw)
        if edited:
            return edited
    text = str(settings.pay_months or "").replace("，", ",")
    return _clean_months(text.split(",")) or [1]


def amount_of(months: int) -> float:
    """几个月多少钱。全站**只有这一处**算钱 —— 页面显示的金额与
    落库的金额必须同源，否则会出现「页面 297、订单 297.00001」这种对不上的账。

    该档单独定过价就用它，没有就按 `月单价 × 月数`。
    """
    p = _pricing()
    override = _amounts(p).get(str(int(months)))
    if override is not None:
        return round(override, 2)
    return round(unit_price(p) * int(months), 2)


def pricing() -> Dict[str, Any]:
    """给订阅管理页定价表单的当前值。

    `amounts` **只回单独定过价的那几档** —— 没定的档位前端显示空框，含义就是
    「按 单价 × 月数」，不必把线性结果也存一份进去。
    """
    return {"price": unit_price(), "months": months_options(),
            "amounts": _amounts()}


def set_pricing(price: float, months: List[int],
                amounts: Optional[Dict[str, float]] = None) -> Dict[str, Any]:
    """整体存定价并回读。**校验都在这里**，抛 `PayError` 由路由转 400。

    `amounts` 里没出现的档位 = 自动回到线性，所以「取消某档的单独定价」就是
    把那个键去掉再存一次，不需要额外的删除动作。
    """
    clean_m = _clean_months(months or [])
    if not clean_m:
        raise PayError("档位至少要留一个正整数月数")
    try:
        p = float(price)
    except (TypeError, ValueError) as exc:
        raise PayError(f"每月单价不是数字：{price!r}") from exc
    if p <= 0:
        raise PayError(f"每月单价得是正数，收到 {p:g}")

    clean_a: Dict[str, float] = {}
    for k, v in (amounts or {}).items():
        try:
            m = int(str(k))
            n = float(v)
        except (TypeError, ValueError) as exc:
            raise PayError(f"档位金额不是数字：{k}={v!r}") from exc
        if m not in clean_m:
            raise PayError(f"档位里没有 {m} 个月，不能给它定价（当前档位：{clean_m}）")
        if n <= 0:
            raise PayError(f"{m} 个月的金额得是正数，收到 {n:g}")
        clean_a[str(m)] = round(n, 2)

    db.kv_set(_PRICING_KEY, json.dumps(
        {"price": round(p, 2), "months": clean_m, "amounts": clean_a},
        ensure_ascii=False))
    logger.info("收银台定价已保存：单价 %s，档位 %s，单独定价 %s",
                round(p, 2), clean_m, clean_a or "（无，全部按线性）")
    return pricing()


def months_days(months: int) -> int:
    """几个月折算成多少天。沿用全局 `pro_days`（一个月的天数，现为 30），
    不用日历月 —— 「2026-02-31」这类日期会直接把开通逻辑弄崩。"""
    return int(settings.pro_days) * int(months)


def plans() -> List[Dict[str, Any]]:
    """给收银台页面的档位表：选几月、多少钱、折算多少天。"""
    return [{
        "months": m,
        "amount": amount_of(m),
        "days": months_days(m),
        "label": f"{m} 个月",
    } for m in months_options()]


# ------------------------------------------------------------------ 链接


#: 链接前缀存 `kv`。**`.env` 的 `pay_base_url` 只是默认值** —— 换网络（手机热点、
#: 内网、以后的公网域名）就变一次前缀，owner 在页面上改一次即可，不用动 `.env`
#: 也不用重启（与定价同口径：页面上点的东西本来就该存在库里）。
_BASE_KEY = "pay_base_url"


def base_url() -> str:
    """收银台与邀请链接共用的前缀，末尾不带 `/`。"""
    return str(db.kv_get(_BASE_KEY) or settings.pay_base_url or "").rstrip("/")


def set_base_url(url: str) -> str:
    """改链接前缀。**只影响之后新生成的链接** —— 已经发出去的是生成那一刻的
    快照，改前缀不该把别人手上的链接改没。

    抛 `PayError`：空串、或不是 http(s) 开头（少了这一刀，填个 `192.168.1.1`
    生成出来的链接是 `192.168.1.1/pay/xxx`，浏览器会当成相对路径拼在当前页上）。
    """
    val = (url or "").strip().rstrip("/")
    if not val:
        raise PayError("前缀不能为空。")
    if not val.startswith(("http://", "https://")):
        raise PayError("前缀要以 http:// 或 https:// 开头，例如 http://192.168.137.10:5173")
    db.kv_set(_BASE_KEY, val)
    return base_url()


def make_link(open_id: str, nickname: str = "") -> Dict[str, Any]:
    """给这个人发一个收银台链接。

    返回 `{url, token, expires_at}`。**链接里不出现 open_id** —— URL 会进
    浏览器历史、聊天记录与 nginx 日志，裸 open_id 等于到处撒身份标识；随机
    token 不可逆推，过期即作废。
    """
    row = db.pay_token_new(open_id, nickname,
                           days=int(settings.pay_link_days))
    return {
        "url": f"{base_url()}/pay/{row['token']}",
        "token": row["token"],
        "expires_at": row["expires_at"],
    }


def resolve(token: str) -> Optional[Dict[str, Any]]:
    """token → 这个人是谁。过期/不存在都返回 None（页面回「链接已失效」）。"""
    return db.pay_token_get(token)


# ------------------------------------------------------------------ 下单


def create_order(token: str, months: int, channel: str,
                 note: str = "") -> Dict[str, Any]:
    """按 token 下一张待确认订单。

    **金额在下单这一刻快照**（`db.pay_order_add` 里存下来），之后管理员改定价
    不影响已下的单。

    抛 `PayError`：token 失效、月份不在档位里、支付方式不认。
    """
    who = db.pay_token_get(token)
    if not who:
        raise PayError("收银台链接已失效，请在飞书重新发「订阅Pro」获取新链接。")
    if int(months) not in months_options():
        raise PayError(f"订阅月份得是 {'/'.join(map(str, months_options()))}，"
                       f"收到 {months!r}。")
    ch = (channel or "").strip().lower()
    if ch not in CHANNELS:
        raise PayError(f"支付方式只支持 {'、'.join(CHANNEL_LABELS[c] for c in CHANNELS)}，"
                       f"收到 {channel!r}。")
    order_no = _new_order_no()
    # 金额与页面显示走**同一个口子** `amount_of` —— 这里若改回直读单价，
    # 每档单独定价就会变成「页面 268、订单 297」。
    amount = amount_of(months)
    # `price` 存**折合每月**，让 `amount ≈ price × months` 那个关系继续成立
    # （列注释写的就是它）。4 位小数：268/3 = 89.3333，×3 是 267.9999 ——
    # 靠乘回来会差一分，所以落库的 `amount` 由参数直接给，不重算。
    price = round(amount / int(months), 4)
    row = db.pay_order_add(order_no, who["open_id"], months, price, ch,
                           token=token, nickname=who.get("nickname") or "",
                           note=note, amount=amount)
    logger.info("收银台下单：%s open_id=%s %d 个月 ¥%s via %s",
                order_no, who["open_id"], months, row["amount"], ch)
    return row


def _new_order_no() -> str:
    """订单号：`P + 年月日时分秒 + 4 位随机`。人看得懂（前缀 + 时间），
    同一秒并发也不撞（随机尾巴）。"""
    return "P" + datetime.now().strftime("%y%m%d%H%M%S") + \
        format(int.from_bytes(secrets.token_bytes(2), "big"), "04x")


# ------------------------------------------------------------------ 管理端


def order_list(limit: int = 100) -> List[Dict[str, Any]]:
    return db.pay_order_list(limit)


def confirm_order(order_no: str, note: str = "") -> Optional[Dict[str, Any]]:
    """管理员照订单开通。**先确认订单、再开通** —— 顺序反了的话，开通失败
    会让订单仍停在 pending，管理员再点一次就把订阅期间累加两遍。

    返回确认后的订单；`None` = 这张订单已经不是 pending（重复点确认被挡下）。
    """
    order = db.pay_order_confirm(order_no, note)
    if not order:
        return None
    db.sub_activate(order["open_id"], amount=order["amount"],
                    days=months_days(order["months"]),
                    nickname=order.get("nickname") or "")
    logger.info("收银台订单已开通：%s -> %s（%d 个月）",
                order_no, order["open_id"], order["months"])
    return order


def order_of_open_id(open_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    """这个人的订单（订阅管理页展开行里看）。"""
    return [o for o in db.pay_order_list(200)
            if str(o.get("open_id") or "") == (open_id or "").strip()][:limit]


def describe_channel(channel: str) -> str:
    return CHANNEL_LABELS.get((channel or "").strip().lower(), channel or "—")
