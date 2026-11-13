# -*- coding: utf-8 -*-
"""快捷指令卡片链路的离线回归（**全离线**）。

**住在 `scripts\\tests\\` 而不是临时目录**（照 `_hk_verify2.py`）——上一版躺在
`C:\\Users\\...\\Temp\\opencode` 里会丢，`docs/04` 的引用随之变成悬空指针。

证明三件事，**全程零网络**（`send_text` 被替换掉，长连接根本不建立，
开头还封死了 TCP + DNS）：

1. lark-oapi 的 `card` 帧**确实被静默丢弃**（不回响应帧 → 点按钮必弹 200340）；
2. `_patch_lark_ws_card_frames` 打上后，同一帧被改标成 `event` 走通，
   并且**真的写回了响应帧**；
3. `_on_card_action` 的四种回包，以及「点按钮 → 后台 dispatch → 发回复」
   这条完整链路。

库隔离：`settings.db_path` **赋值**指到临时库，不走 env —— 2026-10-13 首版
把变量名写成 `DB_PATH`（真实前缀是 `STOCK_`），env 没生效，12 行测试数据写进了
真 `market.db`。已恢复，故下面加双重硬闸。

判据：退出码 0 = 全绿。跑法：
  D:\\test-kanban\\backend\\.venv\\Scripts\\python.exe <本文件>
"""
import asyncio
import base64
import json
import socket
import sys
import tempfile
import threading
import time
import types

# ---- 0) 挡住所有非回环连接与 DNS 查询 ---------------------------------------
_orig = socket.socket.connect
_orig_gai = socket.getaddrinfo
_BLOCKED = []
_LOOP = ("127.0.0.1", "::1", "localhost")


def _guard(self, addr):  # noqa: ANN001
    host = addr[0] if isinstance(addr, tuple) and addr else addr
    if str(host) not in _LOOP:
        _BLOCKED.append(str(host))
        raise AssertionError("BLOCKED-NONLOOPBACK: %s" % (addr,))
    return _orig(self, addr)


def _gai_guard(host, port, *a, **k):  # noqa: ANN001
    if str(host) not in _LOOP:
        _BLOCKED.append("dns:%s" % (host,))
        raise AssertionError("BLOCKED-DNS: %s:%s" % (host, port))
    return _orig_gai(host, port, *a, **k)


socket.socket.connect = _guard
socket.getaddrinfo = _gai_guard

sys.path.insert(0, r"D:\test-kanban\backend")

from app.config import settings                      # noqa: E402

from pathlib import Path                             # noqa: E402

# ---- 1) 库隔离：赋值到临时文件（env 那条路已经证明会失效） -------------------
_SCRATCH = Path(tempfile.mkdtemp(prefix="kanban_card_"))
settings.db_path = _SCRATCH / "market.db"

# ---- 2) 双重硬闸：任一不过就一个字都不往下写 ---------------------------------
assert str(settings.db_path).startswith(tempfile.gettempdir()), (
    "scratch db not under Temp -> would write to the REAL db: %s" % settings.db_path)
assert Path(settings.db_path).resolve() != (
    Path(r"D:\test-kanban\backend\data\market.db").resolve()), (
    "db_path IS the real market.db -> aborting: %s" % settings.db_path)

from app.store import db                             # noqa: E402
from app import cmdcfg, feishu                       # noqa: E402

db.init_db()

PASS = 0
FAIL = []


def ck(cond, name):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(name)


def _walk(els):
    """按钮埋在 `column_set` 里（一行 3 个），顶层只看得到分栏。"""
    out = []
    for e in els:
        if e.get("tag") == "button":
            out.append(e)
        elif e.get("tag") == "column_set":
            for c in e.get("columns") or []:
                out.extend(_walk(c.get("elements") or []))
    return out


def buttons(card):
    return _walk(card["body"]["elements"])


# ------------------------------------------------------------------ 准备
from lark_oapi.ws.client import Client as WsClient    # noqa: E402
from lark_oapi.ws.pb.pbbp2_pb2 import Frame           # noqa: E402
from lark_oapi.ws.const import HEADER_TYPE            # noqa: E402
from lark_oapi.event.dispatcher_handler import (       # noqa: E402
    EventDispatcherHandler)

#: 打补丁**之前**的原方法 —— 用它跑同一帧，才能证明「不补就会被丢掉」。
UNPATCHED = WsClient._handle_data_frame

OWNER = "ou_ck_owner"
CHAT = "oc_ck_chat"
db.kv_set("feishu_owner", OWNER)
# 回复方式 2026-11-13 起默认「私发」：本回归盯的是**卡片帧补丁**，下面那条
# 「回复回原会话」断言按旧口径写，先把开关显式拨到 group（断言与计数不动）。
db.kv_set("feishu_reply_mode", "group")

# 一条**只回文案**的自定义指令：dispatch 全程不碰网络
cmdcfg.save({"key": "ckcard", "kind": "custom", "text": "测速",
             "reply": "OFFLINE_REPLY", "access": "owner", "label": "测速"})

# 关键：把出站发消息换成记录器，这条回归就真的零网络了
sent = []
_orig_send_text = feishu.send_text


def _fake_send_text(chat_id, text, id_type="chat_id"):
    sent.append((chat_id, text))
    return True


feishu.send_text = _fake_send_text


def make_client(handler):
    """绕开构造函数（那里面才有网络与重连），只挂测试需要的三个属性。"""
    c = WsClient.__new__(WsClient)
    c._event_handler = handler
    c._written = []

    async def _wm(data):
        c._written.append(data)

    c._write_message = _wm
    c._fmt_log = lambda fmt, *a: ""
    return c


def make_frame(cmd, chat=CHAT, sender=OWNER):
    f = Frame()
    for k, v in ((HEADER_TYPE, "card"), ("message_id", "m1"),
                 ("sum", "1"), ("seq", "1"), ("trace_id", "t1")):
        h = f.headers.add()
        h.key = k
        h.value = v
    ev = {
        "schema": "2.0",
        "header": {"event_type": "card.action.trigger", "token": "tk"},
        "event": {
            "operator": {"open_id": sender},
            "action": {"value": {"cmd": cmd}, "tag": "button"},
            "context": {"open_chat_id": chat},
        },
    }
    f.payload = json.dumps(ev, ensure_ascii=False).encode("utf-8")
    # 这四个是 pbbp2.Frame 的**必填**字段，缺了 SerializeToString 会抛
    # EncodeError（ping 帧里就是这么填的）
    f.service = 1
    f.method = 1          # FrameType.DATA
    f.SeqID = 0
    f.LogID = 0
    return f


def header_value(frame, key):
    for h in frame.headers:
        if h.key == key:
            return h.value
    return None


# ------------------------------------------------- A 不补丁 = 帧被丢掉
real_handler = (EventDispatcherHandler.builder("", "")
                .register_p2_card_action_trigger(feishu._on_card_action)
                .build())
c_un = make_client(real_handler)
f_un = make_frame("ckcard")
asyncio.run(UNPATCHED(c_un, f_un))
ck(c_un._written == [],
   "WITHOUT the patch a card frame must be silently dropped (the bug)")
ck(not sent, "without the patch nothing should have been dispatched")

# ------------------------------------------------- B 补丁：改标 + 走通
ck(feishu._patch_lark_ws_card_frames() is True, "patch reports applied")
ck(feishu._patch_lark_ws_card_frames() is False, "patch is idempotent")

c_ok = make_client(real_handler)
f_ok = make_frame("ckcard")
asyncio.run(WsClient._handle_data_frame(c_ok, f_ok))
ck(len(c_ok._written) == 1, "WITH the patch a response frame must be written")
ck(header_value(f_ok, HEADER_TYPE) == "event",
   "card frame header rewritten to 'event'")

# 响应帧是 **protobuf 二进制**：payload 字段里才是那层 JSON。解错层次会
# 把一个本来正确的回包当成失败。
echo = Frame()
echo.ParseFromString(c_ok._written[0])
outer = json.loads(echo.payload.decode("utf-8"))
ck(outer.get("code") == 200, "response frame status 200, got %r" % outer.get("code"))
blob = outer.get("data") or ""
inner = json.loads(base64.b64decode(blob).decode("utf-8")) if blob else {}
toast = inner.get("toast") or {}
ck(toast.get("type") == "success",
   "toast type must be success, got %r" % toast)
ck("测速" in (toast.get("content") or ""),
   "toast names the pressed button, got %r" % toast)

# ------------------------------------------------- C 后台 dispatch 真的跑了
deadline = time.time() + 5
while time.time() < deadline and not sent:
    time.sleep(0.05)
ck(len(sent) == 1, "background dispatch ran and produced exactly one send, got %r"
   % (sent,))
if sent:
    ck(sent[0][0] == CHAT, "reply goes back to the original chat")
    ck(sent[0][1] == "OFFLINE_REPLY", "reply is the custom copy, got %r" % sent[0][1])

# ------------------------------------------------- D _on_card_action 的回包
def toast_of(resp):
    t = getattr(resp, "toast", None)
    return (getattr(t, "type", "") or "", getattr(t, "content", "") or "")


def ns(**kw):
    return types.SimpleNamespace(**kw)


r = feishu._on_card_action(ns(event=ns(
    action=ns(value={}), context=ns(open_chat_id=CHAT),
    operator=ns(open_id=OWNER))))
k, m = toast_of(r)
ck(k == "error" and "cmd" in m, "no cmd -> error toast, got %r/%r" % (k, m))

r = feishu._on_card_action(ns(event=ns(
    action=ns(value={"cmd": "does_not_exist"}),
    context=ns(open_chat_id=CHAT), operator=ns(open_id=OWNER))))
k, m = toast_of(r)
ck(k == "error" and "不存在" in m, "unknown cmd -> error toast, got %r/%r" % (k, m))

r = feishu._on_card_action(ns(event=ns(
    action=ns(value={"cmd": "ckcard"}), context=ns(open_chat_id=""),
    operator=ns(open_id=OWNER))))
k, m = toast_of(r)
ck(k == "error" and "会话" in m, "missing chat id -> error toast, got %r/%r" % (k, m))

# 停用的指令不能点
db.cmd_put({"key": "ckcard", "kind": "custom", "text": "测速",
            "reply": "OFFLINE_REPLY", "access": "owner", "enabled": False})
r = feishu._on_card_action(ns(event=ns(
    action=ns(value={"cmd": "ckcard"}), context=ns(open_chat_id=CHAT),
    operator=ns(open_id=OWNER))))
k, m = toast_of(r)
ck(k == "error" and "停用" in m, "disabled cmd -> error toast, got %r/%r" % (k, m))
db.cmd_remove("ckcard")

# 成功那条：先确认它是**秒回**的 toast（不是等 dispatch 完）
cmdcfg.save({"key": "ckcard", "kind": "custom", "text": "测速",
             "reply": "OFFLINE_REPLY", "access": "owner", "label": "测速"})
sent.clear()
t0 = time.time()
r = feishu._on_card_action(ns(event=ns(
    action=ns(value={"cmd": "ckcard"}), context=ns(open_chat_id=CHAT),
    operator=ns(open_id=OWNER))))
elapsed = time.time() - t0
k, m = toast_of(r)
ck(k == "success" and "测速" in m, "valid cmd -> success toast, got %r/%r" % (k, m))
ck(elapsed < 1.0, "toast must return immediately, took %.2fs" % elapsed)

# 未绑定的人点了：**不拦**，由 dispatch 里的授权闸回话（所以这里只该有一条
# 「拒绝」的发送，而不是「已发送」的空头支票）
sent.clear()
db.kv_set("feishu_owner", "")          # 没有白名单 = 谁都不算 owner
r = feishu._on_card_action(ns(event=ns(
    action=ns(value={"cmd": "ckcard"}), context=ns(open_chat_id=CHAT),
    operator=ns(open_id="ou_nobody"))))
k, m = toast_of(r)
ck(k == "success", "permission denial happens in dispatch, not in the toast")
db.kv_set("feishu_owner", OWNER)

# ------------------------------------------------- E 卡片本身
card = feishu.quick_card()
ck(card.get("schema") == "2.0", "card must be schema 2.0")
# 2026-10-13 实翻车：这里曾写成 `update_multi: False`（1.0 时代「独享卡片」的
# 旧习惯），Card JSON 2.0 明令只支持 true → 飞书拒绝创建，ErrCode 300302
# 被外层裹成 230099 "Failed to create card content"，接口回 502。
ck(card.get("config", {}).get("update_multi") is True,
   "schema 2.0 forbids update_multi=false (Feishu ErrCode 300302)")
btns = buttons(card)
ck(all(b.get("behaviors") and b["behaviors"][0].get("type") == "callback"
       for b in btns), "every button carries a callback behavior")
ck(all(b["behaviors"][0]["value"].get("cmd") for b in btns),
   "every button carries its cmd key")
# 一行 3 个：按钮直接挂 body 会各占一行，十来个按钮就把卡片撑成一条长龙
_rows = [e for e in card["body"]["elements"] if e.get("tag") == "column_set"]
ck(all(len(c.get("columns") or []) <= 3 for c in _rows),
   "at most 3 buttons per row, got %r"
   % [len(c.get("columns") or []) for c in _rows])

# 一个内置指令的附带文案不能塞进卡片按钮（按钮只发触发词）
db.cmd_put({"key": "help", "kind": "builtin", "text": "帮助",
            "alias": "help、指令、?、？", "reply": "X" * 600, "quick": 1,
            "enabled": True})
card2 = feishu.quick_card()
btn2 = buttons(card2)
ck(all(len((b["text"] or {}).get("content") or "") <= 60 for b in btn2),
   "button labels stay short even when a long reply is configured")

feishu.send_text = _orig_send_text

ck(not _BLOCKED, "全程零外发（被挡 %r）" % (_BLOCKED[:3],))

print("PASS=%d FAIL=%d" % (PASS, len(FAIL)))
for f in FAIL:
    print("  FAIL: %s" % f)
# 控制台中文是乱码 —— 报告另存 UTF-8，用 read 工具看（路径打印成 ASCII）
_rpt = _SCRATCH / "card_report.txt"
with open(_rpt, "w", encoding="utf-8") as fh:
    fh.write("PASS=%d FAIL=%d\n" % (PASS, len(FAIL)))
    for f in FAIL:
        fh.write("  FAIL: %s\n" % f)
print("REPORT=%s" % _rpt)
sys.exit(1 if FAIL else 0)
