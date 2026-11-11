"""邀请链接（docs/06 §11）—— 与收银台**并列**的一条链路。

    owner 在订阅管理页从名单里选人 → 生成邀请链接
    对方点开 → 欢迎页（后端从 token 反查出是谁）
    点「订阅」→ 免费开通 pro（`amount=0`）

## 为什么身份是「反查」出来的

点一个裸的 `<a href>`，浏览器发出去的就只有 URL 本身，**不会附带任何飞书
身份** —— 飞书在那条链路上不在场。所以身份必须在**链接生成那一刻**就烧进
token：生成链接的人（owner）本来就知道给谁生成，后端存 token → open_id 的
映射，点开时拿 token 换回身份。

这也意味着邀请链接**必须一人一条**。群发一条通用链接谁点都一样，后端面对的
就是匿名请求 —— 那条路要走飞书网页应用 OAuth，而 OAuth 的重定向 URL 要求
HTTPS + 域名，localhost 配不了（`docs/06 §9` 的 P1 待办）。

## 名单就是 `subscriber` 表

**不另开一张名单表**：`db.sub_upsert` 本来就会给「发过消息但还没订阅」的人建
一行，`plan=''` 就是「在名单里、还没订阅」，`plan='pro'` 是已订阅。一张表两个
状态，比两张表同步更省事，也不存在「名单里有但他其实已经订了」这种要对账的
状态。

## 金额 0 = 免费

`subscriber.amount` 是现成的一列（原本记占位价）。邀请开通落 `0.0`，订阅管理
页把 0 显示成「免费」—— 不加字段、不迁移，也就不存在新旧口径打架。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app import pay, subscribe
from app.config import settings
from app.store import db

logger = logging.getLogger(__name__)

#: 免责声明。**全站硬口径**（README / docs/01 / App.tsx / 各页页脚），
#: 欢迎页同样要带：只做行情呈现，不给买卖信号。
DISCLAIMER = subscribe.DISCLAIMER


class InviteError(ValueError):
    """参数不对。调用方转 HTTP 4xx，**不要**当成 500 告警刷屏。"""


def make_link(open_id: str, nickname: str = "",
              days: Optional[int] = None) -> Dict[str, Any]:
    """给这个人发一条邀请链接。

    返回 `{url, token, expires_at}`。**URL 里不出现 open_id** —— URL 会进
    浏览器历史、聊天转发与 nginx 日志，裸 open_id 就是到处撒身份标识；
    随机 token 不可逆推，过期即作废。

    有效期默认跟 `pro_days` 一样（30 天）：链接是给对方点开用的，比订阅本身
    短就没意义了。
    """
    open_id = (open_id or "").strip()
    if not open_id:
        raise InviteError("没指定 open_id，生成不了邀请链接。")
    # 昵称默认从名单里带出来：调用方（路由、以后的指令）不该各查一遍，
    # 而且飞书的消息事件里**根本没有昵称**（只有 sender_id），名字只能存在库里。
    row = db.sub_get(open_id) or {}
    nick = (nickname or "").strip() or str(row.get("nickname") or "")
    link = db.invite_token_new(open_id, nick, days=int(days or settings.pro_days))
    return {
        # 前缀与收银台同源（`pay.base_url`）：换网络时只改一处，两条链接一起跟上
        "url": f"{pay.base_url()}/invite/{link['token']}",
        "token": link["token"],
        "expires_at": link["expires_at"],
    }


def resolve(token: str) -> Optional[Dict[str, Any]]:
    """token → 这个人是谁。过期或不存在都回 `None`（页面显示「链接已失效」）。"""
    return db.invite_token_get(token)


def roster() -> List[Dict[str, Any]]:
    """邀请名单 = `subscriber` 表的全部行，`created_at` 倒序。

    `plan=''` 是还没订阅的，`plan='pro'` 是已订阅（`active` 交给调用方判 ——
    与 `db.sub_list` 同口径，这里不做业务判定）。
    """
    return db.sub_list()


def activate(token: str) -> Dict[str, Any]:
    """点「订阅」→ 免费开通 30 天。

    **已经在订阅期就只回状态、不重复累加天数** —— 否则把链接转给别人、或自己
    连点两下，30 天就翻倍了。这与 `pay.confirm_order` 用 409 挡重复确认是同一
    个理由：能连点的操作必须幂等。

    抛 `InviteError`：token 失效、没绑定身份。
    """
    who = db.invite_token_get(token)
    if not who:
        raise InviteError("邀请链接已失效，请向管理员重新索取。")
    oid = str(who.get("open_id") or "").strip()
    if not oid:
        raise InviteError("这条邀请链接没绑定身份，开通不了。")
    nick = str(who.get("nickname") or "")

    if subscribe.is_active(oid):
        row = db.sub_get(oid) or {}
        return {
            "already": True,
            "nickname": row.get("nickname") or nick,
            "expires_at": subscribe.expires_at(oid),
            "amount": row.get("amount"),
        }

    # amount=0.0 就是「免费」标记。天数不传 = 走 settings.pro_days。
    row = db.sub_activate(oid, amount=0.0, nickname=nick)
    logger.info("邀请开通（免费）%s 到期 %s", oid, row.get("expires_at"))
    return {
        "already": False,
        "nickname": row.get("nickname") or nick,
        "expires_at": str(row.get("expires_at") or ""),
        "amount": row.get("amount"),
    }
