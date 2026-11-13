"""飞书**入站**指令：长连接收消息 → 解析 → 调现有能力 → 回一条消息。

和 notify.py 同样叫「飞书」，但方向、凭据、能力完全不同 —— 一个进一个出：

=========== 出站 notify.py ==================  ============ 入站 feishu.py ============
自定义机器人 webhook（POST 一个 URL）           企业自建应用 app_id + app_secret
只能往那个群发一条，收不到任何东西               事件订阅「长连接」收消息
无需凭据、无需权限                             要在开放平台开机器人能力与事件

为什么用**长连接**：飞书事件有两种投递方式 —— 回调 URL（要公网可达，本地得做
内网穿透）和长连接（本机主动连出去建 WebSocket）。选后者，家里电脑、云服务器、
NAT 后面都一样能收，**不需要任何入站端口**，这也是「把项目扔到服务器上、电脑
不用一直开着」这条路线的前提。

用户拍板（2026-09-29）：
  1. 只认白名单那一个 open_id —— 群里别人喊「跑」不算数。还没绑定时回一句
     「你的 open_id 是 …」让人照着填，且**不执行任何指令**。
  2. 指令先做「触发任务 / 关注取关」，顺手给状态、查询、摘要这类只读指令。
  3. 「跑」用 kind=\"feishu\"，跑完**照样推摘要**（人在手机前，跑完得有回音；
     页面上手动「立即执行」仍按原选择不推）。

凭据与开关存 SQLite kv（feishu_app_id / feishu_app_secret / feishu_owner /
feishu_inbound），「定时任务」页配，改了立即生效、不用重启，也不进代码仓库。

长连接跑在守护线程里，进程退出即断；消息处理再丢一层线程，绝不能把 SDK 的
事件循环卡住（卡住 = 收不到后面的消息，还会掉心跳）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import requests

from app import cmdcfg, notify, scheduler, subscribe
from app.fetchers import hk as hk_fetch
from app.fetchers.client import market_of, norm_code
from app.store import db

logger = logging.getLogger(__name__)

_APP_ID_KEY = "feishu_app_id"
_APP_SECRET_KEY = "feishu_app_secret"
_OWNER_KEY = "feishu_owner"
_ENABLED_KEY = "feishu_inbound"
#: 群里指令的回复方式（2026-11-13 用户定）：`private` = 私发给发送人本人的
#: 单聊，不在群里刷屏（**默认**）；`group` = 照旧回原会话。存 kv，页面可切。
_REPLY_MODE_KEY = "feishu_reply_mode"

_API_BASE = "https://open.feishu.cn/open-apis"
_TIMEOUT = (5.0, 10.0)      # (建连, 读回)

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {
    # running: 守护线程在跑；connected: 已通过凭据预检并进入等待收消息
    "running": False, "connected": False, "started_at": "",
    "last_error": "", "last_msg_at": "", "handled": 0,
}
_GEN = 0                     # 代数：每次启停 +1，在途的旧代事件直接丢
_THREAD: Optional[threading.Thread] = None
_STARTED_WITH: tuple = ("", "")
_CLIENT = None               # lark.Client，发消息用（SDK 自己管 tenant_token）
_CLIENT_FOR: tuple = ("", "")
_WS_PATCHED = False          # CARD 帧补丁是否已打（见 _patch_lark_ws_card_frames）

_WATCHDOG: Optional[threading.Thread] = None
_WATCHDOG_GEN = 0            # 看门狗代数：stop() 换代即可让它退出
_RETRY_SEC = 60              # 开着但没连上时，每隔多久重试一次

_ATE = re.compile(r"@_user_\d+|@_all")   # 群里 @机器人 留下的占位符

# 指令说明（`帮助`）由 `help_card()` **现算**，不再有一份写死的清单 ——
# 写死的那份和「指令管理」页是两份真相，页面上改完就对不上账了。


# ------------------------------------------------------------------ 配置


def reply_mode() -> str:
    """群里指令的回复方式：`private`（默认，私发给发送人）/ `group`（回原会话）。

    只认这两个值，库里的脏值一律回落 `private` —— 回复方式宁可保守（发私聊），
    也不能因为一个拼错的开关把内容晒回群里。
    """
    return "group" if db.kv_get(_REPLY_MODE_KEY) == "group" else "private"


def set_reply_mode(mode: str) -> str:
    """改回复方式。非法值直接抛（`/feishu/config` 由 pydantic 先挡一道 422）。"""
    if mode not in ("private", "group"):
        raise ValueError(f"reply_mode 只能是 private / group，收到 {mode!r}")
    db.kv_set(_REPLY_MODE_KEY, mode)
    return mode


def config() -> Dict[str, Any]:
    """给「定时任务」页：凭据、白名单、开关、连接状态。"""
    with _LOCK:
        state = dict(_STATE)
    app_id = (db.kv_get(_APP_ID_KEY) or "").strip()
    app_secret = (db.kv_get(_APP_SECRET_KEY) or "").strip()
    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "owner": (db.kv_get(_OWNER_KEY) or "").strip(),
        "enabled": db.kv_get(_ENABLED_KEY) == "1",
        "reply_mode": reply_mode(),
        "has_app": bool(app_id and app_secret),
        "state": state,
    }


def set_config(app_id: str, app_secret: str, owner: str,
               enabled: bool, reply_mode: Optional[str] = None
               ) -> Dict[str, Any]:
    """保存并立即生效（预检凭据 → 需要时重启长连接），不用重启后端。

    :param reply_mode: ``None`` = **不动**这个开关。旧页面/旧调用方不带该
                       字段，不能让一次凭据保存顺手把回复方式抹回默认。
    """
    db.kv_set(_APP_ID_KEY, app_id.strip())
    db.kv_set(_APP_SECRET_KEY, app_secret.strip())
    db.kv_set(_OWNER_KEY, owner.strip())
    db.kv_set(_ENABLED_KEY, "1" if enabled else "0")
    if reply_mode is not None:
        set_reply_mode(reply_mode)
    with _LOCK:
        _STATE["last_error"] = ""     # 旧错误先清掉，由 start 重新判定
    start()
    return config()


# ---------------------------------------------------------- 长连接生命周期


def start() -> None:
    """开长连接（幂等：同凭据已经在跑就什么都不做）。

    先做一次凭据预检再连：app_id 打错时 SDK 会掉进无限重连，页面上永远显示
    「运行中」却查不出原因；预检把飞书返回的 code/msg 原样透出来才好排查。
    """
    conf = config()
    if not conf["enabled"]:
        stop()
        return
    if not conf["has_app"]:
        with _LOCK:
            _STATE.update({"running": False, "connected": False})
        return
    _ensure_watchdog()      # 网络不通/开机时没连上，60s 后自己再试

    err = _preflight(conf["app_id"], conf["app_secret"])
    if err:
        with _LOCK:
            _STATE.update({"running": False, "connected": False,
                           "last_error": err})
        logger.warning("飞书长连接未启动：%s", err)
        return

    global _THREAD, _GEN, _STARTED_WITH

    app_id, app_secret = conf["app_id"], conf["app_secret"]
    with _LOCK:
        alive = _THREAD is not None and _THREAD.is_alive()
        if alive and _STARTED_WITH == (app_id, app_secret):
            _STATE["running"] = True      # 同凭据已在跑
            return
    if alive:
        _stop_thread()                    # 凭据变了：先停旧的

    with _LOCK:
        _GEN += 1
        gen = _GEN
        _STATE.update({
            "running": True, "connected": False,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "last_error": "",
        })
    _STARTED_WITH = (app_id, app_secret)
    thread = threading.Thread(target=_run, name="feishu-ws", daemon=True,
                              args=(app_id, app_secret, gen))
    _THREAD = thread
    thread.start()


def stop() -> None:
    """关长连接。先换代再停：即便线程没停干净，旧代消息也不会被执行。"""
    global _GEN, _WATCHDOG_GEN
    with _LOCK:
        _GEN += 1
        _WATCHDOG_GEN += 1          # 看门狗一起停，别在关掉后还自己重连
        _STATE.update({"running": False, "connected": False})
    _stop_thread()


def _ensure_watchdog() -> None:
    """起（或复用）重连看门狗：开着却没连上时每 `_RETRY_SEC` 秒试一次。

    没有它的话，后端启动时网络恰好不通（这里网络抖动是常态），长连接会一直
    停在 `last_error` 上，得手工再保存一次配置才肯连。
    """
    global _WATCHDOG, _WATCHDOG_GEN
    with _LOCK:
        if _WATCHDOG is not None and _WATCHDOG.is_alive():
            return                   # 已经有一个在跑（包含当前就是它调进来的情况）
        _WATCHDOG_GEN += 1
        gen = _WATCHDOG_GEN
    thread = threading.Thread(target=_watchdog_loop, args=(gen,),
                              name="feishu-retry", daemon=True)
    _WATCHDOG = thread
    thread.start()


def _watchdog_loop(gen: int) -> None:
    while True:
        time.sleep(_RETRY_SEC)
        try:
            with _LOCK:
                if _WATCHDOG_GEN != gen:
                    return           # 已停 / 已换代
                if _THREAD is not None and _THREAD.is_alive():
                    continue         # 连着呢，下一轮再看
            conf = config()
            if not conf["enabled"] or not conf["has_app"]:
                return               # 用户关掉了，看门狗自己退场
            logger.info("飞书长连接未连上，重试…")
            start()
        except Exception:  # noqa: BLE001
            logger.exception("飞书重连看门狗异常")


def _stop_thread() -> None:
    global _THREAD, _STARTED_WITH
    thread, _THREAD = _THREAD, None
    _STARTED_WITH = ("", "")
    if thread is None or not thread.is_alive():
        return
    try:
        # SDK 的 start() 卡在一个 module 级事件循环里 sleep，停掉那个循环它就返回
        import lark_oapi.ws.client as ws_client
        ws_client.loop.call_soon_threadsafe(ws_client.loop.stop)
    except Exception:  # noqa: BLE001
        logger.debug("停飞书长连接失败", exc_info=True)
    thread.join(timeout=5)
    if thread.is_alive():
        logger.warning("飞书长连接线程未在 5s 内退出（不再启动新连接）")
        with _LOCK:
            _STATE["last_error"] = "旧连接未能停止，请重启后端"


def _fetch_token(app_id: str, app_secret: str) -> tuple:
    """换 `tenant_access_token`。成功返回 ``(token, "")``，失败 ``("", 可读原因)``。"""
    try:
        resp = requests.post(
            f"{_API_BASE}/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=_TIMEOUT,
        )
        data = resp.json() or {}
    except Exception as exc:  # noqa: BLE001
        return "", f"连不上 open.feishu.cn：{type(exc).__name__}: {exc}"
    if data.get("code") != 0:
        return "", f"凭据校验失败 code={data.get('code')}：{data.get('msg')}"
    return str(data.get("tenant_access_token") or ""), ""


def _preflight(app_id: str, app_secret: str) -> str:
    """验一次凭据：成功返回 ""，失败返回可读原因（显示在卡片的错误行上）。"""
    return _fetch_token(app_id, app_secret)[1]


def _list_chats() -> List[Dict[str, Any]]:
    """机器人所在的群列表（要 `im:chat:readonly`）。owner 为空时用它找发送目标。"""
    conf = config()
    token, err = _fetch_token(conf["app_id"], conf["app_secret"])
    if not token:
        raise RuntimeError(err)
    resp = requests.get(f"{_API_BASE}/im/v1/chats", params={"page_size": 50},
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=_TIMEOUT)
    data = resp.json() or {}
    if data.get("code") != 0:
        raise RuntimeError(f"列群失败 code={data.get('code')}：{data.get('msg')}")
    return (data.get("data") or {}).get("items") or []


def _run(app_id: str, app_secret: str, gen: int) -> None:
    """长连接线程本体：lark 的 start() 会阻塞在这里直到 stop 或出错。"""
    global _CLIENT, _CLIENT_FOR
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

        _CLIENT = (lark.Client.builder().app_id(app_id).app_secret(app_secret)
                   .log_level(lark.LogLevel.WARNING).build())
        _CLIENT_FOR = (app_id, app_secret)

        def on_message(data: P2ImMessageReceiveV1) -> None:
            # SDK 在事件循环里回调，处理与回消息都丢线程 —— 卡住循环就掉心跳
            threading.Thread(target=_handle_event, args=(data, gen),
                             name="feishu-msg", daemon=True).start()

        # 卡片按钮的回调走**同一条长连接**，不需要公网。前提是先把 SDK 那个
        # CARD 帧丢弃的 bug 补掉，否则注册了也收不到（幂等，重复调用无害）。
        _patch_lark_ws_card_frames()
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(on_message)
                   .register_p2_card_action_trigger(_on_card_action)
                   .build())
        client = lark.ws.Client(app_id, app_secret, event_handler=handler,
                                log_level=lark.LogLevel.WARNING,
                                auto_reconnect=True)
        with _LOCK:
            if _GEN == gen:
                # 进入阻塞等待；下面若连接失败会抛出去并被改成 last_error
                _STATE["connected"] = True
        client.start()
        # start 正常返回说明循环被停了：把连接也断干净（SDK 没给公开的 stop）
        try:
            import lark_oapi.ws.client as ws_client
            ws_client.loop.run_until_complete(client._disconnect())  # noqa: SLF001
        except Exception:  # noqa: BLE001
            logger.debug("断开飞书连接失败", exc_info=True)
    except Exception as exc:  # noqa: BLE001
        with _LOCK:
            expected = _GEN != gen       # stop() 换代后的退出是预期内的
        if expected:
            logger.info("飞书长连接已停止")
        else:
            logger.warning("飞书长连接异常退出: %s", exc)
            with _LOCK:
                _STATE.update({"running": False, "connected": False,
                               "last_error": f"{type(exc).__name__}: {exc}"})
    finally:
        with _LOCK:
            if _GEN == gen:
                _STATE.update({"running": False, "connected": False})


# ---------------------------------------------------------------- 发消息


def _api_client() -> Any:
    """lark.Client（发应用消息）。凭据换了就重建。"""
    global _CLIENT, _CLIENT_FOR
    conf = config()
    if _CLIENT is None or _CLIENT_FOR != (conf["app_id"], conf["app_secret"]):
        import lark_oapi as lark
        _CLIENT = (lark.Client.builder().app_id(conf["app_id"])
                   .app_secret(conf["app_secret"])
                   .log_level(lark.LogLevel.WARNING).build())
        _CLIENT_FOR = (conf["app_id"], conf["app_secret"])
    return _CLIENT


#: 回复文本里的 Markdown 链接 `[中文](url)`。只有写成这样的链接会被
#: `send_text` 升级成飞书**富文本（post）里的中文锚点**；裸 URL 仍按纯文本发
#: —— 文本消息里飞书只能把链接显示成 URL 本身，换不了字（2026-11-13 用户反馈）。
_MD_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")


def _post_body(text: str) -> Dict[str, Any]:
    """把带 `[中文](url)` 的文本拼成飞书 post 消息体：**逐行成段**，行内按
    链接切成 text / a 片段。空行给一个空格占位 —— 飞书不收空段。"""
    lines: List[List[Dict[str, str]]] = []
    for line in text.split("\n"):
        segs: List[Dict[str, str]] = []
        pos = 0
        for m in _MD_LINK.finditer(line):
            if m.start() > pos:
                segs.append({"tag": "text", "text": line[pos:m.start()]})
            segs.append({"tag": "a", "text": m.group(1), "href": m.group(2)})
            pos = m.end()
        if pos < len(line):
            segs.append({"tag": "text", "text": line[pos:]})
        lines.append(segs or [{"tag": "text", "text": " "}])
    return {"zh_cn": {"title": "", "content": lines}}


def send_text(chat_id: str, text: str, id_type: str = "chat_id") -> bool:
    """以**应用**身份发一条消息。SDK 自己取并缓存 tenant_access_token。

    正文里写了 `[中文](url)` 就升级成**富文本（post）**，链接渲染成中文锚点；
    没有这种链接就照旧发纯文本（`msg_type=text`）。

    :param id_type: ``chat_id``（群/会话）或 ``open_id``（某个人的单聊）

    没走自定义机器人 webhook：那个只能发到它所在的群，而收到的指令可能来自
    单聊或别的群 —— 回错了地方比不回更糟。
    """
    if not chat_id or not text:
        return False
    rich = _MD_LINK.search(text) is not None
    try:
        from lark_oapi.api.im.v1 import (CreateMessageRequest,
                                          CreateMessageRequestBody)
        body = (CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type("post" if rich else "text")
                .content(json.dumps(_post_body(text) if rich else {"text": text},
                                    ensure_ascii=False))
                .build())
        req = (CreateMessageRequest.builder().receive_id_type(id_type)
               .request_body(body).build())
        resp = _api_client().im.v1.message.create(req)
        if not resp.success():
            logger.warning("飞书发消息失败 code=%s msg=%s", resp.code, resp.msg)
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("飞书发消息异常 %s=%s", id_type, chat_id)
        return False


# ------------------------------------------------------------ 快捷指令卡片
#
# 「点一下 = 替你把指令发出去」。点击**不会**发出任何消息，飞书只是回调通知
# 本机「谁点了哪个按钮」，所以这条链要三样东西凑齐，缺一条就报 200340：
#   1. 后台「事件与回调 → 回调配置」选**使用长连接接收回调**（用户去点）；
#   2. 后台添加**新版**卡片回传交互回调 `card.action.trigger`（旧版没有长连接）；
#   3. 本机 SDK 的 CARD 帧补丁（见 `_patch_lark_ws_card_frames`）。


def send_card(chat_id: str, card: Dict[str, Any],
              id_type: str = "chat_id") -> bool:
    """以**应用**身份发一张交互卡片（`msg_type=interactive`）。

    与 `send_text` 同一接口、同一份凭据，只是 content 从 `{"text": …}`
    换成卡片 JSON。
    """
    if not chat_id or not card:
        return False
    try:
        from lark_oapi.api.im.v1 import (CreateMessageRequest,
                                          CreateMessageRequestBody)
        body = (CreateMessageRequestBody.builder()
                .receive_id(chat_id).msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False))
                .build())
        req = (CreateMessageRequest.builder().receive_id_type(id_type)
               .request_body(body).build())
        resp = _api_client().im.v1.message.create(req)
        if not resp.success():
            logger.warning("飞书发卡片失败 code=%s msg=%s", resp.code, resp.msg)
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("飞书发卡片异常 %s=%s", id_type, chat_id)
        return False


def quick_card() -> Dict[str, Any]:
    """快捷指令卡片（**schema 2.0**）。

    必须是新版卡片：旧版按钮回传的是 `card.action.trigger_v1`，那条链飞书
    只走 HTTP 回调地址，**没有长连接模式**，我们没有公网就永远收不到。

    按钮只取「指令管理」页勾了「进快捷卡」且没停用的那几条，按 sort 排。

    **一律 1/3 宽**：按钮直接挂 `body.elements` 下会各占一行，十来个就把卡片
    撑成一条长龙。按 3 个一组塞进 `column_set`，**末行不足 3 个也补成 3 列**（缺的
    用空 markdown 占位），否则末行只剩 1~2 列，权重一均分按钮就被撑宽，跟上面几
    行对不齐 —— 2026-10-13 用户就是这么报的。

    **唯一例外是总数 < 3**：卡片一共就 1 个按钮时让它占满一行，2 个时平分一行。
    这时候还摆一个 1/3 的格子，右边三分之二全是空白，比不齐更难看。
    """
    rows = [r for r in cmdcfg.rows()
            if int(r.get("quick") or 0) and int(r.get("enabled", 1) or 0)]
    rows.sort(key=lambda r: (int(r.get("sort") or 0), str(r.get("key"))))

    btns: List[Dict[str, Any]] = [{
        "tag": "button",
        "text": {"tag": "plain_text",
                 "content": str(r.get("label") or r.get("key"))},
        "type": "primary",
        "behaviors": [{"type": "callback",
                       "value": {"cmd": str(r.get("key"))}}],
    } for r in rows]

    # 空列占位：只占位置，不接任何交互。**不能省** —— 省了末行就只剩 1~2 列，
    # 权重一均分按钮就变宽，跟上面的 1/3 宽对不齐。
    spacer = {"tag": "markdown", "content": ""}

    elements: List[Dict[str, Any]] = []
    if not btns:
        # 2.0 没有 `div` 这个标签（那是 1.0 的），写下去飞书会整张卡片拒收。
        elements.append({
            "tag": "markdown",
            "content": "还没勾选任何快捷指令 —— 到看板「指令管理」页勾一下。",
        })
    full_grid = len(btns) >= 3
    for i in range(0, len(btns), 3):
        row = btns[i:i + 3]
        if len(row) == 1 and not full_grid:
            elements.append(row[0])          # 就这一个按钮：占满整行
            continue
        cols: List[Dict[str, Any]] = [
            {"tag": "column", "width": "weighted", "weight": 1,
             "vertical_align": "top", "elements": [b]}
            for b in row]
        if full_grid:
            while len(cols) < 3:             # 末行补空列，保住 1/3 宽
                cols.append({"tag": "column", "width": "weighted", "weight": 1,
                             "vertical_align": "top", "elements": [spacer]})
        elements.append({
            "tag": "column_set",
            "flex_mode": "trisect" if full_grid else "bisect",
            "horizontal_spacing": "medium",
            "background_style": "default",
            "margin": "0px 0px 8px 0px",
            "columns": cols,
        })
    return {
        "schema": "2.0",
        # **必须是 true**：Card JSON 2.0 只支持共享卡片，`update_multi` 设
        # `false` 会被飞书当场拒收（ErrCode 300302，外层裹成 230099
        # "Failed to create card content"）。1.0 时代「独享卡片」的直觉在这里是错的。
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "快捷指令（点一下就发）"},
                   "template": "blue"},
        "body": {"elements": elements},
    }


#: 分组标题。**顺序即显示顺序** —— 从「人人能用」到「仅管理员」，让没订阅的人
#: 先看见自己能干什么，而不是先看见一堆用不了的。
_ACCESS_LABEL = (("any", "所有人都能用"),
                 ("sub", "订阅后可用"),
                 ("owner", "仅管理员"))


def help_card(lead: str = "") -> Dict[str, Any]:
    r"""`帮助` 的指令说明卡片（**只列说明，不放按钮** —— 2026-10-14 用户定）。

    数据源是 `cmdcfg.rows()`：**与「指令管理」页同一份**。在那儿改了触发词 /
    说明、或者停用一条，这张卡立刻跟着变，停用的直接不出现。

    **不放按钮是刻意的**：快捷指令卡才是「点一下就发」，这张卡只负责「有什么、
    怎么用」。混进「跑」「开通」这类按钮，误触一次就是一次真实执行（完整盘后
    任务要跑 12-15 分钟）。
    """
    groups: Dict[str, List[str]] = {k: [] for k, _ in _ACCESS_LABEL}
    for r in cmdcfg.rows():
        if not int(r.get("enabled", 1) or 0):
            continue
        acc = str(r.get("access") or "owner")
        if acc not in groups:
            acc = "owner"      # 说明写错了也不能变成「人人可用」
        words: List[str] = []
        for w in ([str(r.get("text") or "")]
                  + re.split(r"[,，、]", str(r.get("alias") or ""))):
            if w.strip() and w.strip() not in words:
                words.append(w.strip())
        head = f"**{str(r.get('label') or r.get('key') or '')}**"
        if words:
            head += f"（{' / '.join(words)}）"
        param = str(r.get("param") or "").strip()
        if param:
            head += f" `{param}`"
        desc = str(r.get("desc") or "").strip()
        groups[acc].append(f"{head}\n　{desc}" if desc else head)

    elements: List[Dict[str, Any]] = []
    if lead:
        elements.append({"tag": "markdown", "content": f"**{lead}**"})
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown",
                     "content": "群里发请先 @我，也可以直接私聊我。"})
    for acc, title in _ACCESS_LABEL:
        if not groups[acc]:
            continue
        elements.append({"tag": "hr"})
        elements.append({"tag": "markdown", "content": f"**{title}**"})
        elements.append({"tag": "markdown", "content": "\n".join(groups[acc])})
    elements.append({"tag": "hr"})
    elements.append({
        "tag": "markdown",
        "content": ("触发词、附带文案与「快捷指令」按钮都在看板「指令管理」页配置；"
                    "自定义指令也可以只转发一句现成指令（如「查新华」= 查询 600825）。\n"
                    "行情仅供研究参考，不构成任何投资建议。"),
    })
    return {
        "schema": "2.0",
        # 口径同 `quick_card`：Card JSON 2.0 不支持独享卡片，设 false 会被拒收。
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": "可用指令"},
                   "template": "blue"},
        "body": {"elements": elements},
    }


def _card_to_text(card: Dict[str, Any]) -> str:
    """把卡片拍平成纯文本。只给「试一下」那个预览框用 —— 页面上是纯文本框、
    装不下卡片，但内容还得让人看得见。"""
    out: List[str] = []
    head = ((card.get("header") or {}).get("title") or {}).get("content")
    if head:
        out.append(str(head))
    for el in (card.get("body") or {}).get("elements") or []:
        if el.get("tag") == "markdown" and el.get("content"):
            out.append(str(el["content"]))
    return "\n".join(out)


def push_quick_card(target: str = "", id_type: str = "open_id") -> Dict[str, Any]:
    """把快捷指令卡片发出去（「指令管理」页的「发我一张」按钮）。

    不传 `target` 就发给白名单那个人的单聊 —— 这就是「发送给用户」的默认语义。
    **连不上就说连不上**，不返回假成功：卡片没发出去时页面上显示「已发送」，
    用户会一直以为功能是好的。
    """
    conf = config()
    if not conf["has_app"]:
        return {"ok": False, "reason": "还没配置飞书应用凭据（去「定时任务」页填）"}
    if not conf["state"].get("connected"):
        return {"ok": False, "reason": "长连接没连上，飞书收发都发不出去"}
    to = (target or "").strip() or conf["owner"]
    if not to:
        return {"ok": False, "reason": "没指定收件人，也没绑定 open_id"}
    if not send_card(to, quick_card(), id_type=id_type):
        return {"ok": False, "reason": "飞书拒绝了这张卡片，看后端日志"}
    return {"ok": True, "to": to}


def _on_card_action(data: Any) -> Any:
    """卡片按钮回调。**必须秒回** —— 飞书等 3 秒收不到回包就弹 200340。

    所以这里只回一句 toast，真正的分发丢后台线程：指令里「查询」「估值」
    都要联网，串行等它们必然超时。
    """
    from lark_oapi.event.callback.model.p2_card_action_trigger import (
        P2CardActionTriggerResponse)

    def _toast(kind: str, msg: str) -> Any:
        return P2CardActionTriggerResponse(
            {"toast": {"type": kind, "content": msg}})

    ev = getattr(data, "event", None)
    action = getattr(ev, "action", None) if ev else None
    ctx = getattr(ev, "context", None) if ev else None
    operator = getattr(ev, "operator", None) if ev else None
    key = str((getattr(action, "value", None) or {}).get("cmd") or "")
    sender = str(getattr(operator, "open_id", "") or "")
    chat = str(getattr(ctx, "open_chat_id", "") or "")
    if not key:
        return _toast("error", "这个按钮没带 cmd，去「指令管理」页重新发一张")

    row = next((r for r in cmdcfg.rows() if r.get("key") == key), None)
    if row is None or not int(row.get("enabled", 1) or 0):
        return _toast("error", "指令不存在或已停用")
    if not chat:
        return _toast("error", "回包里没有会话 id，无法回复")

    # 真正发出去的那句：自定义优先用 target，内置直接用它的触发词。
    # 带参数的指令（查询 / 基金 / 关注）点一下跑不了，页面上不给勾进快捷卡。
    text = cmdcfg.custom_target(key) or str(row.get("text") or "")

    threading.Thread(target=_card_run, args=(text, sender, chat, key),
                     name="feishu-card", daemon=True).start()
    # **不在这儿判权限**：`dispatch` 里那道 `_allow` 闸会对未绑定的人回一句
    # 解释，那句也得发给他才看得见。这里只负责让按钮先有回音。
    return _toast("success", f"已发送：{row.get('label') or key}")


def _reply_target(sender_id: str, chat_id: Optional[str]) -> Tuple[str, str]:
    """按当前回复方式算回复目标，返回 ``(目标 id, id_type)``。

    `private`（默认）→ 发给**发送人本人**的单聊（``open_id``）：群里发指令、
    @机器人、点卡片按钮都不在群里刷屏；发送人拿不到时才退回原会话。
    `group` → 原样回原会话（``chat_id``，旧行为）。
    单聊里两种模式效果相同（那个人的 open_id 单聊就是这个会话）。
    """
    if reply_mode() == "private" and sender_id:
        return sender_id, "open_id"
    return chat_id or "", "chat_id"


def _send_reply(target: str, reply: Union[str, Dict[str, Any]],
                id_type: str = "chat_id") -> bool:
    """把 `dispatch` 的回复发出去：**字符串走文本、字典走卡片**。

    分流只放这一处 —— 长连接与卡片回调两条入口都走它，不会出现「一条能发
    卡片、另一条把卡片当文本发出去」的漏改。
    """
    if isinstance(reply, dict):
        return send_card(target, reply, id_type=id_type)
    return send_text(target, reply, id_type)


def _card_run(text: str, sender: str, chat: str, key: str) -> None:
    """后台执行一条被点中的指令，把回复发到**按回复方式算出的目标**。"""
    try:
        reply = dispatch(text, sender, chat_id=chat)
        target, id_type = _reply_target(sender, chat)
        if reply and not _send_reply(target, reply, id_type):
            # 私发失败**不回退群里**：回退会把只该给发送人的内容晒到群里
            logger.warning("卡片指令 %s 的回复没发出去（%s=%s）",
                           key, id_type, target)
    except Exception:  # noqa: BLE001
        logger.exception("卡片指令 %s 执行失败", key)


def _patch_lark_ws_card_frames() -> bool:
    """lark-oapi 1.6.4 → 1.7.4（含上游 `v2_main`，至今未修）的 ws 客户端把
    `card` 类型帧直接 `return` 丢掉，于是 **注册了 `register_p2_card_action_trigger`
    也永远不会被调用，更不会回写响应帧** —— 用户点卡片按钮 3 秒后必弹 200340
    「出错了，请稍后重试」。飞书推卡片回调用的正是 `card` 帧。

    这里只把帧头的 `type` 从 `card` 改标成 `event`，再交给 SDK **原方法**去
    派发与回写 —— 分发逻辑一行都不重写。幂等；上游哪天修了也不冲突（改标之后
    走的是 EVENT 分支，效果一致）。
    """
    global _WS_PATCHED
    if _WS_PATCHED:
        return False
    try:
        from lark_oapi.ws.client import Client as _ws
        from lark_oapi.ws.const import HEADER_TYPE
    except Exception:  # noqa: BLE001  # SDK 没装 → 飞书整条链都不通，不必报
        return False
    if getattr(_ws, "_kanban_card_patch", False):
        _WS_PATCHED = True
        return False
    orig = _ws._handle_data_frame

    async def _patched(self, frame):  # type: ignore[no-untyped-def]
        for h in frame.headers:
            if h.key == HEADER_TYPE and h.value == "card":
                h.value = "event"
                break
        return await orig(self, frame)

    _ws._handle_data_frame = _patched
    _ws._kanban_card_patch = True
    _WS_PATCHED = True
    logger.info("已修补 lark-oapi 的 CARD 帧丢弃 bug（卡片回调可用）")
    return True


def push_status() -> Dict[str, Any]:
    """**主动**往飞书推一条看板状态（卡片上的「发我一条」）。

    发给白名单里那个人的单聊；owner 没填就退而求其次发到机器人所在的第一个群，
    都拿不到就如实报错。正文全取库内数据，纯只读，随时可点。
    """
    conf = config()
    if not conf["has_app"]:
        return {"ok": False, "detail": "未配置应用凭据（App ID / Secret）",
                "text": ""}
    target = conf["owner"]
    id_type = "open_id"
    if not target:
        try:
            chats = _list_chats()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc), "text": ""}
        if not chats:
            return {"ok": False,
                    "detail": "白名单为空，且机器人不在任何群里 —— "
                              "填一个 open_id 或把机器人拉进一个群",
                    "text": ""}
        target = chats[0].get("chat_id") or ""
        id_type = "chat_id"
    text = "\n".join([
        "A股看板 · 主动推送", "",
        _cmd_status(), "",
        "数据来自公开接口，仅供研究参考，不构成投资建议。",
    ])
    ok = send_text(target, text, id_type)
    return {"ok": ok, "text": text, "target": target,
            "detail": "已发送" if ok else "飞书返回失败，详见后端日志"}


# ------------------------------------------------------------ 收到消息之后


def _handle_event(data: Any, gen: int) -> None:
    """一条 im.message.receive_v1：取内容 → 分发 → 回复。独立线程里跑。"""
    try:
        with _LOCK:
            if _GEN != gen:
                return                      # 已停/已换代，丢弃在途事件
        event = getattr(data, "event", None)
        msg = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        if msg is None:
            return
        with _LOCK:
            _STATE["last_msg_at"] = datetime.now().strftime("%H:%M:%S")
            _STATE["handled"] += 1

        sender_type = getattr(sender, "sender_type", "") or ""
        if sender_type and sender_type != "user":
            return                          # 机器人/应用自己发的，防回环
        sender_id = _sender_id(sender)
        # 回复目标只在这里算一次：私发开关对「非文字提示」与「正式回复」两条
        # 出口一致生效，不会出现一条私发、另一条回群。
        target, id_type = _reply_target(sender_id, msg.chat_id)
        if (msg.message_type or "text") != "text":
            send_text(target, "我只认文字指令，发「帮助」看用法。", id_type)
            return

        text = _clean(_text_of(msg.content))
        # 谁发过消息谁进名单（`plan=''` = 在名单里、还没订阅）。这是邀请链接
        # **唯一**的 bootstrap：没有这一步名单永远是空的，一条邀请链接也生成
        # 不出来 —— `invite.make_link` 得先知道给谁。
        # **这不是多调一次飞书接口**：open_id 本来就在长连接推过来的事件里，
        # 落库是纯本地动作。昵称事件里没有，留空即可（`sub_upsert` 不会拿空串
        # 抹掉已有名字，管理员也能在订阅管理页填）。
        if sender_id:
            db.sub_upsert(sender_id)
        reply = dispatch(text, sender_id, chat_id=msg.chat_id)
        if reply and not _send_reply(target, reply, id_type):
            # 私发失败**不回退群里**（隐私优先），只记日志 —— 回退会把只该
            # 给发送人的内容晒到整个群。
            logger.warning("回复没发出去（%s=%s），不回退到原会话",
                           id_type, target)
    except Exception:  # noqa: BLE001
        logger.exception("处理飞书消息失败")


def _text_of(content: Any) -> str:
    """事件里的 content 是 JSON 字符串：{"text":"@_user_1 跑"}。"""
    try:
        obj = json.loads(content or "{}")
        return str(obj.get("text", "") if isinstance(obj, dict) else obj)
    except (TypeError, ValueError):
        return str(content or "")


def _sender_id(sender: Any) -> str:
    ids = getattr(sender, "sender_id", None)
    return (getattr(ids, "open_id", "") or getattr(ids, "user_id", "")
            or getattr(ids, "union_id", "") or "")


def _clean(text: str) -> str:
    """去掉群里的 @ 占位符、全角空格与首尾空白。"""
    return _ATE.sub(" ", text or "").replace("\u3000", " ").strip()


# ---------------------------------------------------------------- 指令分发


# --------------------------------------------------------------- 指令注册表
#
# 三段式：识别 `_identify` → 授权 `_allow` → 执行 `_execute`。
#
# **为什么必须是这个结构**（docs/06 §4）：旧版是一条 `if t in (...)` 长链，
# 上面挂一个「是不是白名单那个人」的前置 if —— 结构上是 **default allow**：
# 订阅制一开，任何一条忘了过闸的新指令，未付费用户直接就能用。改成分段后
# **识别不出的、以及识别出了却没在 `_ACCESS` 里登记 access 的，一律走不到
# 执行段**（默认拒），新增指令变成「登记一行」而不是重审整条 if 链。
#
# 订阅者的权限不看 `owner`，而是查 `subscriber` 里的有效期 —— 一个人是
# 超管、其余是订阅者，两条身份线互不干扰。


_ACCESS: Dict[str, str] = {
    # ---- 入口指令必须是 "any" ----
    # 否则没订阅的人拿不到「订阅Pro」，永远开不了通，直接死锁。
    "help": "any",
    "sub": "any",
    # ---- 订阅功能：订阅期内可用 ----
    "estimate": "sub",         # 基金 161725
    "fund_add": "sub",         # 关注基金 161725
    "fund_del": "sub",         # 删除基金 161725
    "fund_list": "sub",        # 基金关注
    # ---- 超管：订阅者不给 ----
    "activate": "owner",       # 开通 <open_id> [金额]
    "run": "owner",
    "light": "owner",
    "status": "owner",
    "digest": "owner",
    "block": "owner",
    "sector": "owner",
    "watch_list": "owner",
    "watch_add": "owner",
    "watch_del": "owner",
    "query": "owner",
}


def _words(key: str, *default: str) -> Optional[List[str]]:
    r"""这条指令现在认哪些触发词：库里改过就用库里的，没改用代码默认。

    **停用返回 `None`** —— 带参数的分支据此整段跳过。返回空列表是不行的：
    空候选会拼出 `^(?:)\s*(\d{6})$`，那等于「裸 6 位数字」，会把所有股票
    查询统统吞掉。
    """
    return cmdcfg.words(key, list(default))


def _re(words: List[str], tail: str) -> "re.Pattern[str]":
    """把触发词拼成 `^(?:词1|词2)<tail>`。**必须 escape** —— 「?」「？」
    这类词不转义进正则就是另一个意思了。
    """
    return re.compile(rf"^(?:{'|'.join(re.escape(w) for w in words)}){tail}")


def _identify(t: str) -> Tuple[str, tuple]:
    """文本 → `(指令 key, 参数)`；认不出返回 `("", ())`。

    顺序上把订阅指令排在最前，避免被下面的股票正则吃掉 —— 实测三条新前缀
    都不会被旧正则匹配上（`关注` 后面必须紧跟空白或数字，`关注基金` 卡在
    `基` 上），但排前面是**让这件事不依赖巧合**，将来谁放宽正则也不会踩。
    """
    # ---- 自定义指令（最前：别被下面的股票正则吃掉）----
    hit = cmdcfg.match_custom(t)
    if hit:
        return hit, ()

    # ---- 订阅制（docs/06）----
    w = _words("sub", "订阅Pro", "订阅", "pro")
    if w and t in w:
        return "sub", ()
    w = _words("activate", "开通")
    if w:
        m = _re(w, r"\s+(\S+)(?:\s+([\d.]+))?$").match(t)
        if m:
            return "activate", (m.group(1), m.group(2) or "")
    w = _words("fund_add", "关注基金")
    if w:
        m = _re(w, r"\s*(\d{6})$").match(t)
        if m:
            return "fund_add", (m.group(1),)
    w = _words("fund_del", "删除基金", "取消基金", "移除基金")
    if w:
        m = _re(w, r"\s*(\d{6})$").match(t)
        if m:
            return "fund_del", (m.group(1),)
    w = _words("estimate", "基金")
    if w:
        m = _re(w, r"\s*(\d{6})$").match(t)
        if m:
            return "estimate", (m.group(1),)
    w = _words("fund_list", "基金关注", "我的基金")
    if w and t in w:
        return "fund_list", ()

    # ---- 有副作用的两条 ----
    w = _words("run", "跑", "完整跑", "重跑", "执行", "执行任务")
    if w and t in w:
        return "run", ()
    w = _words("light", "轻跑", "轻量", "快速抓取", "刷新")
    if w and t in w:
        return "light", ()

    # ---- 只读 ----
    w = _words("help", "帮助", "help", "指令", "?", "？")
    if w and t in w:
        return "help", ()
    w = _words("status", "状态", "status", "任务状态")
    if w and t in w:
        return "status", ()
    w = _words("digest", "摘要", "复盘", "日报")
    if w and t in w:
        return "digest", ()
    w = _words("block", "候选", "主力建仓", "建仓")
    if w and t in w:
        return "block", ()
    w = _words("sector", "板块", "板块资金", "板块资金流")
    if w and t in w:
        return "sector", ()
    w = _words("watch_list", "关注列表", "我的关注", "关注股")
    if w and t in w:
        return "watch_list", ()

    # ---- 股票关注 / 取关 / 查询（口径见 docs/06 §2；2026-11-07 加 5 位港股）----
    # **位数即市场**：5 位 = 港股（01810 小米）、6 位 = A 股 —— 与网页端
    # `client.market_of` 同判据。此前正则钉死 `\d{6}`，「关注01810」永远
    # 识别不出，落进默认拒回「没听懂」。只放宽位数，触发词与日期参数不动。
    w = _words("watch_add", "关注", "加自选", "收藏")
    if w:
        m = _re(w, r"\s*(\d{5,6})(?:\s+(\d{8}))?$").match(t)
        if m:
            return "watch_add", (m.group(1), m.group(2) or "")
    w = _words("watch_del", "取关", "取消关注", "取消", "删除", "移除")
    if w:
        m = _re(w, r"\s*(\d{5,6})$").match(t)
        if m:
            return "watch_del", (m.group(1),)
    w = _words("query", "查询", "查", "看")
    if w:
        m = _re(w, r"?\s*(\d{5,6})$").match(t)
        if m:
            return "query", (m.group(1),)
        m = _re(w, r"\s+(.+)$").match(t)
        if m:
            return "query", (m.group(1).strip(),)
    return "", ()


def _allow(access: str, sender_id: str, owner: str) -> Tuple[bool, str]:
    """L1 认证 + L2 授权：`(能不能执行, 拒绝时的回复)`。

    **只看 `sender_id`，绝不看会话类型** —— 私聊 / 群聊只决定回复发到哪，
    不决定能不能执行；否则「私聊绕过群里校验」就是个现成的洞。

    对**绑定的 owner 本人**放行一切（包括 `sub`）：超管要能拿「试一下」
    演遍所有指令，否则 simulate 到订阅指令上只会得到一句「请先订阅」。
    """
    if sender_id and owner and sender_id == owner:
        return True, ""
    if access == "any":
        return True, ""
    if access == "sub":
        if subscribe.is_active(sender_id):
            return True, ""
        return False, subscribe.paywall_text()
    # ---- access == "owner" ----
    if not owner:
        return False, (
            "还没绑定发送人，这条指令没有执行。\n"
            f"你的 open_id：{sender_id or '（在飞书里发条消息就有了）'}\n"
            "填到「定时任务 → 飞书指令 → 只响应这个人」保存后即可使用。"
        )
    if not sender_id or sender_id == owner:
        return True, ""
    return False, "无权限：这个看板只响应绑定的那个人。"


def _execute(cmd: str, args: tuple, sender_id: str,
             chat_id: Optional[str], allow: bool) -> Union[str, Dict[str, Any]]:
    """过闸之后才走到这里。**新增指令必须同时给 `_ACCESS` 加一行**，
    否则上一步就被拒了（默认拒）。

    返回**字符串 = 文本回复**，返回**字典 = 飞书卡片**（发之前由
    `_handle_event` / `_card_run` 分流到 `send_card`）。"""
    if cmd == "help":
        return help_card()
    # ---- 订阅制 ----
    if cmd == "sub":
        return subscribe.cmd_subscribe(sender_id)
    if cmd == "activate":
        oid, amt = args
        if not allow:
            return f"（试一下）会开通 {oid} 的订阅 Pro。"
        try:
            amount: Optional[float] = float(amt) if amt else None
        except ValueError:
            return f"金额得是数字，收到 {amt!r}。用法：开通 {oid} 99"
        return subscribe.cmd_activate(oid, amount)
    if cmd == "estimate":
        return subscribe.cmd_estimate(args[0], allow)
    if cmd == "fund_add":
        return subscribe.cmd_add(sender_id, args[0], allow)
    if cmd == "fund_del":
        return subscribe.cmd_remove(sender_id, args[0], allow)
    if cmd == "fund_list":
        return subscribe.cmd_my_funds(sender_id, allow)

    # ---- 以下与旧版一致（owner 专用）----
    if cmd == "run":
        return _cmd_run(chat_id, allow, sender_id)
    if cmd == "light":
        return _cmd_light(chat_id, allow, sender_id)
    if cmd == "status":
        return _cmd_status()
    if cmd == "digest":
        return _cmd_digest()
    if cmd == "block":
        return _cmd_block(lambda: notify.mainforce_block(_trade_date() or ""))
    if cmd == "sector":
        return _cmd_block(lambda: notify.sector_block())
    if cmd == "watch_list":
        return _cmd_block(lambda: notify.watch_block(_trade_date() or ""))
    if cmd == "watch_add":
        return _cmd_watch_add(args[0], args[1], allow)
    if cmd == "watch_del":
        return _cmd_watch_del(args[0], allow)
    if cmd == "query":
        return _cmd_query(args[0])
    return help_card(f"没听懂「{cmd}」")


#: 自定义指令的 `target` 可以再指向另一条自定义指令 —— 环必须封顶，否则
#: 「A 指向 B、B 指向 A」能一直转下去（而且每层都挂一次附带文案）。
_MAX_DEPTH = 3


def dispatch(text: str, sender_id: str = "", *, chat_id: Optional[str] = None,
             allow_side_effects: bool = True, _depth: int = 0
             ) -> Union[str, Dict[str, Any]]:
    """解析一条文本指令，返回回复（不负责发送）。

    返回**字符串 = 文本**，返回**字典 = 飞书卡片** —— 调用方据此分流。

    :param sender_id:        事件里的 open_id，用来过认证与授权
    :param chat_id:          回复目标；带副作用的指令要在后台线程里回就用得上
    :param allow_side_effects: False = 只演一遍（「试一下」用），不跑任务不改库
                               **新权限层必须尊重它**，否则模拟反而真跑了
    """
    t = _clean(text)
    if not t:
        return help_card()

    owner = (db.kv_get(_OWNER_KEY) or "").strip()

    cmd, args = _identify(t)
    # 内置看 `_ACCESS`（代码常量，改库放宽不了）；自定义看库里那条 access，
    # **两边都查不到就落进下面的默认拒分支**。
    access = _ACCESS.get(cmd) or cmdcfg.access_of(cmd)
    if access is None:
        # 认不出，或者认出了却没登记 access —— 两种都按**默认拒**处理。
        if cmd:
            logger.warning("指令 %r 未登记 access，默认拒", cmd)
        return help_card(f"没听懂「{t[:24]}」")
    ok, deny = _allow(access, sender_id, owner)
    if not ok:
        return deny
    if cmdcfg.is_custom(cmd):
        target = cmdcfg.custom_target(cmd)
        if not target:
            # `run_custom` 返回的就是 reply 本身，**不能再 decorate 一次** ——
            # 否则「早安」会变成「早安\\n\\n早安」。
            return cmdcfg.run_custom(cmd)
        if _depth >= _MAX_DEPTH:
            # 成环了（A→B→A）。必须明说「配置有问题」，不能静默给句正常回复。
            logger.warning("自定义指令 %s 的 target 成环，已截断", cmd)
            return ("指令配置成环了（自定义指令的 target 指回了自己），已停止。\n"
                    "去「指令管理」页检查 target。")
        # target 那一跳**再过一次上面的闸**：把「状态」指给一条 sub 指令，
        # 不该让 sub 用户跑出 owner 的东西。
        inner = dispatch(target, sender_id, chat_id=chat_id,
                         allow_side_effects=allow_side_effects,
                         _depth=_depth + 1)
        if isinstance(inner, dict):
            # 卡片没法再接一段附带文案（它是结构化元素，不是一行字），原样透传
            return inner
        return cmdcfg.decorate(cmd, inner)
    # 内置指令的附带文案接在正文后面；没配就原样返回。
    inner = _execute(cmd, args, sender_id, chat_id, allow_side_effects)
    if isinstance(inner, dict):
        return inner          # 同上：卡片直接发，不再拼附带文案
    return cmdcfg.decorate(cmd, inner)


def simulate(text: str) -> Dict[str, Any]:
    """不碰真实连接跑一遍分发（「试一下」按钮）。副作用一律不执行。"""
    conf = config()
    owner = conf["owner"]
    reply = dispatch(text, owner, allow_side_effects=False)
    if isinstance(reply, dict):
        # 「试一下」那个框是纯文本的，装不下卡片 —— 拍平了照样看得见内容。
        reply = _card_to_text(reply)
    return {"input": text, "reply": reply, "owner": owner,
            "bound": bool(owner), "state": conf["state"],
            "has_app": conf["has_app"]}


def _cmd_block(factory: Callable[[], List[str]]) -> str:
    """分段正文类指令（候选/板块/关注列表），没数据给一句人话。"""
    try:
        lines = factory()
    except Exception as exc:  # noqa: BLE001
        logger.exception("拼指令回复失败")
        return f"拼装失败：{type(exc).__name__}: {exc}"
    return "\n".join(lines) if lines else "暂无数据（先跑一轮任务）。"


# ------------------------------------------------------------ 有副作用的


def _cmd_run(chat_id: Optional[str], allow: bool,
             sender_id: str = "") -> str:
    if scheduler.state()["running"]:
        st = scheduler.state()
        return (f"已有一轮盘后任务在进行中（{st.get('kind', '')} · "
                f"{st.get('started_at', '')}），跑完我会推摘要。")
    if not allow:
        return ("（试一下）会启动完整盘后任务：抓 4 个涨跌停池 + 板块资金流 + "
                "打分，约 12-15 分钟，跑完自动推摘要到这个会话。")

    def _worker() -> None:
        try:
            scheduler.run_post_close(kind="feishu")
        except Exception as exc:  # noqa: BLE001
            logger.exception("飞书触发的盘后任务失败")
            # 异步回执也守回复方式：群里触发的失败**私发给发起人**，不回群里。
            # 发送失败不回退群里（隐私优先），send_text 内部已记日志。
            target, id_type = _reply_target(sender_id, chat_id)
            if target:
                send_text(target, f"盘后任务失败：{type(exc).__name__}: {exc}",
                          id_type)

    threading.Thread(target=_worker, name="post-close-feishu",
                     daemon=True).start()
    return ("已开始完整盘后任务（抓池 + 板块资金流 + 打分，约 12-15 分钟）。\n"
            "跑完会自动把摘要推到这个会话。")


def _cmd_light(chat_id: Optional[str], allow: bool,
               sender_id: str = "") -> str:
    if not allow:
        return ("（试一下）会启动轻量抓取：涨跌停池 + 板块资金流，几十秒，"
                "完成后我再单独报一次结果。")

    def _worker() -> None:
        # 同 `_cmd_run`：异步回执按回复方式走，群里触发也私发给发起人。
        target, id_type = _reply_target(sender_id, chat_id)
        try:
            out = scheduler.run_light(kind="feishu")
        except Exception as exc:  # noqa: BLE001
            logger.exception("飞书触发的轻量抓取失败")
            if target:
                send_text(target, f"轻量抓取失败：{type(exc).__name__}: {exc}",
                          id_type)
            return
        steps = " · ".join(f"{k} {v}" for k, v in
                           (out.get("results") or {}).items())
        txt = f"轻量抓取完成（{notify.STATUS_LABEL.get(out.get('status'), out.get('status'))} · {out.get('date')}）\n{steps}"
        if out.get("failed"):
            txt += "\n失败：" + "、".join(out["failed"])
        if target:
            send_text(target, txt, id_type)

    threading.Thread(target=_worker, name="light-feishu",
                     daemon=True).start()
    return "轻量抓取中（涨跌停池 + 板块资金流，几十秒），完成后我再报一次结果。"


def _cmd_watch_add(code: str, source_date: str, allow: bool) -> str:
    # 与网页端同口径归一化（routes.watchlist_add 三步里的前两步）：
    # **位数即市场**（6 位 A 股、5 位港股），代码先归一再落库 ——
    # 直接存原样，后面查日线会拿不到数据。
    market = market_of(code)
    code = norm_code(code, market) or code
    stock = _find_stock(code) if market == "cn" else None
    name = (stock or {}).get("name") or code
    price = (stock or {}).get("price")
    if market == "hk" and allow:
        # 港股不在 A 股候选/股池里，名字与现价只能问腾讯（约 50ms）。
        # 拿不到回 {} 就用代码兜底 —— **绝不因为查不到名字而关注失败**。
        # 「试一下」（allow=False）不联网：模拟就该零外发。
        quote = hk_fetch.quote(code)
        name = quote.get("name") or code
        price = quote.get("price") or price
    src = source_date or (stock or {}).get("mf_date") or _trade_date() or ""
    if not src:
        return (f"拿不到 {code} 的建仓日，请显式给日期：\n"
                f"关注 {code} 20260928")
    item = {
        "code": code,
        "market": market,
        "name": name,
        "industry": (stock or {}).get("industry") or "",
        "source_date": src,
        "rank": (stock or {}).get("rank"),
        "score": (stock or {}).get("score"),
        "price": price,
        "snapshot": {
            "pct_chg": (stock or {}).get("pct_chg"),
            "note": "飞书指令关注",
        },
    }
    if not allow:
        return (f"（试一下）会关注 {name}({code})，建仓日 {src}"
                + (f"，打分 {item['score']}" if item["score"] is not None else "")
                + "。")
    db.watch_add(item)
    return (f"已关注 {name}({code}) · 建仓日 {src}\n"
            "发「关注列表」看它今天的涨跌与资金流。")


def _cmd_watch_del(code: str, allow: bool) -> str:
    if not allow:
        return f"（试一下）会取消关注 {code}。"
    if not db.watch_remove(code):
        return f"{code} 不在关注列表。"
    return f"已取消关注 {code}。"


# ---------------------------------------------------------------- 只读的


def _trade_date() -> Optional[str]:
    """优先用库内已有日期（回看历史），否则用交易日历算最近交易日。"""
    for dataset in ("pool_zt", "pool_zb", "pool_dt"):
        d = db.latest_date(dataset)
        if d:
            return d
    try:
        from app.fetchers import calendar
        return calendar.last_trade_date()
    except Exception as exc:  # noqa: BLE001
        logger.warning("交易日历不可用: %s", exc)
        return None


def _cmd_status() -> str:
    st = scheduler.state()
    info = scheduler.schedule_info()
    lines: List[str] = []
    if st.get("running"):
        done = len(st.get("steps") or {})
        lines.append(f"任务：进行中（{st.get('kind', '')} · "
                     f"{st.get('started_at', '')} · 已完成 {done} 步）")
    else:
        lines.append("任务：空闲")
    job = db.last_job()
    if job:
        label = notify.STATUS_LABEL.get(job.get("status"), job.get("status"))
        kind = notify.KIND_LABEL.get(job.get("kind"), job.get("kind"))
        when = (job.get("started_at") or "").replace("T", " ")[:16]
        lines.append(f"上次：{kind} · {label} · {when}")
    hour, minute = info.get("hour", 0), info.get("minute", 0)
    next_run = (info.get("next_run") or "-").replace("T", " ")[:16]
    lines.append(f"定时：{'开' if info.get('enabled') else '关'} · "
                 f"每交易日 {hour:02d}:{minute:02d} · 下次 {next_run}")
    for dataset, label in (("pool_zt", "涨跌停池"),
                           ("main_force", "候选打分"),
                           ("sector_flow_industry", "板块资金流")):
        meta = db.snapshot_meta(dataset)
        if not meta:
            lines.append(f"{label}：暂无数据")
            continue
        when = (meta.get("fetched_at") or "").replace("T", " ")[:16]
        if dataset == "main_force":          # 打分是整包 JSON，行数恒为 1
            lines.append(f"{label}：{meta.get('trade_date')} · {when}")
        else:
            lines.append(f"{label}：{meta.get('trade_date')} · "
                         f"{meta.get('row_count')} 行 · {when}")
    return "\n".join(lines)


def _cmd_digest() -> str:
    date = _trade_date()
    if not date:
        return "库里还没有数据，先跑一轮（发「跑」）。"
    try:
        return notify.build_digest(date, kind="飞书指令")
    except Exception as exc:  # noqa: BLE001
        logger.exception("拼摘要失败")
        return f"拼摘要失败：{type(exc).__name__}: {exc}"


def _find_stock(code: str) -> Optional[Dict[str, Any]]:
    """在候选与股池里找一只股，拼出「建仓日 / 打分 / 涨跌 / 行业」等字段。"""
    out: Dict[str, Any] = {}
    mf_date = db.latest_date("main_force")
    if mf_date:
        mf = db.load_one("main_force", mf_date) or {}
        top_rows = mf.get("rows") or []
        for i, r in enumerate(top_rows, 1):
            if r.get("code") == code:
                out.update({
                    "name": r.get("name"), "industry": r.get("industry"),
                    "price": r.get("price"), "pct_chg": r.get("pct_chg"),
                    "score": r.get("score"), "rank": i, "mf_date": mf_date,
                })
                break
        if not out:
            for r in mf.get("all_rows") or []:
                if r.get("code") == code:
                    out.update({
                        "name": r.get("name"), "industry": r.get("industry"),
                        "price": r.get("price"), "pct_chg": r.get("pct_chg"),
                        "score": r.get("score"), "rank": None,
                        "mf_date": mf_date,
                    })
                    break
    date = _trade_date()
    if date:
        for dataset in ("pool_zt", "pool_zb", "pool_dt", "pool_strong"):
            for r in db.load(dataset, date) or []:
                if r.get("code") == code:
                    out.setdefault("name", r.get("name"))
                    out.setdefault("industry", r.get("industry"))
                    out.setdefault("price", r.get("price"))
                    out.setdefault("pct_chg", r.get("pct_chg"))
                    out.setdefault("pool", dataset)
                    break
    return out or None


def _find_by_name(key: str) -> Optional[str]:
    """按名字（含模糊）在候选和股池里找代码。"""
    date = _trade_date()
    rows: List[Dict[str, Any]] = []
    mf = db.load_one("main_force", db.latest_date("main_force") or "")
    rows += (mf or {}).get("all_rows") or []
    if date:
        for dataset in ("pool_zt", "pool_zb", "pool_dt", "pool_strong"):
            rows += db.load(dataset, date) or []
    key = key.strip()
    for r in rows:
        if r.get("name") == key:
            return r.get("code")
    for r in rows:
        name = r.get("name") or ""
        if key in name or name in key:
            return r.get("code")
    return None


def _cmd_query(key: str) -> str:
    key = key.strip()
    # 位数即市场：5 位 = 港股、6 位 = A 股（与 _identify 同判据）
    code = key if re.fullmatch(r"\d{5,6}", key) else _find_by_name(key)
    if not code:
        return f"没找到「{key}」（按代码或名称在股池/候选里匹配）。"
    if market_of(code) == "hk":
        # 港股：候选打分 / 涨跌停池 / 主力资金流都是 A 股概念，只回行情与
        # 关注状态。quote 拿不到名字/价就用代码与 `-`，不编数。
        quote = hk_fetch.quote(code)
        name = quote.get("name") or code
        lines = [f"{name}({code}) · 港股",
                 f"现价 {quote.get('price') or '-'} · "
                 f"{notify._pct(quote.get('pct_chg'))}"]
        if code in {w.get("code") for w in db.watch_list()}:
            lines.append("关注：已关注（发「取关」移除）")
        else:
            lines.append(f"关注：未关注（发「关注 {code}」加入）")
        return "\n".join(lines)
    stock = _find_stock(code) or {}
    name = stock.get("name") or code
    date = _trade_date() or ""
    lines = [f"{name}({code})"]

    pool_label = {"pool_zt": "涨停", "pool_zb": "炸板", "pool_dt": "跌停",
                  "pool_strong": "强势"}
    hit = None
    if date:
        for dataset in ("pool_zt", "pool_zb", "pool_dt", "pool_strong"):
            for r in db.load(dataset, date) or []:
                if r.get("code") == code:
                    hit = (dataset, r)
                    break
            if hit:
                break
    if hit:
        dataset, row = hit
        boards = int(row.get("boards") or 1) or 1
        tail = f" · {boards}板" if dataset == "pool_zt" and boards >= 2 else ""
        lines.append(f"{pool_label[dataset]}池{tail} · "
                     f"现价 {stock.get('price') or row.get('price')} · "
                     f"{notify._pct(stock.get('pct_chg') or row.get('pct_chg'))}")
    elif stock.get("price") is not None:
        lines.append(f"现价 {stock.get('price')} · "
                     f"{notify._pct(stock.get('pct_chg'))}")
    else:
        lines.append("不在当日任何股池")

    if stock.get("score") is not None:
        rank = f" · 第 {stock['rank']} 名" if stock.get("rank") else ""
        lines.append(f"候选打分 {float(stock['score']):.1f} 分{rank}"
                     f" · {stock.get('mf_date', '')}")
    else:
        lines.append("未进候选打分")

    got = (notify.inflow_summary(date or _trade_date() or "",
                                 [code]).get(code) or {})
    if got.get("days"):
        lines.append(f"近{got['days']}日主力净流入 {notify._money(got['total'])}")
    if code in {w.get("code") for w in db.watch_list()}:
        lines.append(f"关注：已关注（发「取关 {code}」移除）")
    else:
        lines.append(f"关注：未关注（发「关注 {code}」加入）")
    return "\n".join(lines)
