# -*- coding: utf-8 -*-
r"""订阅制（docs/06）的**离线**回归，2026-10-12。

每一条都对应一个「不测就会漏」的点：

  1) **权限层是默认拒的**：`_identify` 认不出 → key='' → 不在 `_ACCESS` 里
     → 走不到执行段。旧版那条长 if 链结构上是 default allow —— 少登记一条
     access，未付费用户就白嫖了，所以「认不出 = 拒」必须是断言而不是愿望。
  2) **入口指令是 `any`**：`订阅Pro` 要是需要订阅才发得出去，就死锁了。
  3) **owner 是超管**：即使本人没订，也放行一切（「试一下」靠它演遍所有指令）。
  4) **权限只看 sender_id**：带不带 `chat_id` 结果必须一致 —— 否则
     「私聊绕过群里校验」就是个现成的洞。
  5) **三条新前缀不吃掉股票指令**：`关注基金 161725` 归 fund_add、
     `关注 600825` 仍归 watch_add；反过来也一样。
  6) **过期即降级**：`expires_at` 落后于现在就当没订，不需要定时任务去改字段。
  7) **关注写的是 `subscriber_fund`，不碰全局 `fund_watchlist`**。
  8) **超限回「关注数量超出」**，且那一条不许落库。
  9) **14:50 推送**：名单只含「期内 + 有关注」的；正文**必须带免责声明**；
     按 `open_id` 私发而不是群 `chat_id`；成败都写 `push_log`。
 10) **落库编排搬到 db 之后 `routes._record_fund_logs` 这个名字还在**
     （`_review_check` / `_fund_check` 直接引它，搬家不能把回归弄挂）。

全离线：开头封死非回环连接与 DNS，`fund_estimate.estimate` 与
`feishu.send_text` 一律桩掉 —— 一条消息都不会真发出去。

跑法：
  D:\test-kanban\backend\.venv\Scripts\python.exe D:\test-kanban\scripts\tests\_sub_check.py
"""
import socket
import sys
import tempfile
from pathlib import Path

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

# 订阅表也得落在草稿库上 —— 直接跑就是往真 market.db 里插测试订阅者。
_SCRATCH = Path(tempfile.mkdtemp(prefix="kanban_sub_"))
settings.db_path = _SCRATCH / "market.db"

from app import feishu, scheduler as sch             # noqa: E402
from app import invite, pay                          # noqa: E402
from app import subscribe as sub                     # noqa: E402
from app.api import routes as R                      # noqa: E402
from app.store import db                             # noqa: E402
from fastapi import FastAPI                          # noqa: E402
from fastapi.testclient import TestClient            # noqa: E402

N = [0]


def ok(cond, msg):
    N[0] += 1
    if not cond:
        print("FAIL #%d  %s" % (N[0], msg))
        sys.stdout.flush()
        sys.exit(1)
    print("ok   #%d  %s" % (N[0], msg))


OWNER = "ou_owner_person"
STRANGER = "ou_stranger_person"
db.kv_set(feishu._OWNER_KEY, OWNER)

# ==================== 1) 权限层：默认拒、入口免费、owner 超管 ================
ok(feishu._ACCESS.get("help") == "any",
   "帮助是 access=any（任何人要能问出怎么用）")
ok(feishu._ACCESS.get("sub") == "any",
   "订阅Pro 是 access=any（需要订阅才发得出去就死锁了）")
for _k in ("estimate", "fund_add", "fund_del", "fund_list"):
    ok(feishu._ACCESS.get(_k) == "sub", "%s 需要订阅期内" % _k)
for _k in ("run", "light", "status", "query", "watch_add", "watch_del",
           "activate"):
    ok(feishu._ACCESS.get(_k) == "owner", "%s 是 owner 专用（订阅者不给）" % _k)

ok(feishu._identify("这是一句胡话") == ("", ()),
   "认不出的输入 -> key=''")
ok(feishu._ACCESS.get("") is None,
   "'' 不在注册表里 -> 两段都判成拒（这就是默认拒的机制）")
ok("nonexistent_cmd" not in feishu._ACCESS,
   "漏登记 access 的指令会被 dict.get 判成 None -> 拒")

ok("没听懂" in feishu._card_to_text(feishu.dispatch("这是一句胡话", OWNER)),
   "未知输入回「没听懂」+ 帮助，而不是执行任何东西")

# ---- 入口必须免费 ----
_h = feishu.dispatch("帮助", STRANGER)
ok(isinstance(_h, dict) and _h.get("schema") == "2.0",
   "「帮助」回的是卡片，不是纯文本")
ok("订阅Pro" in feishu._card_to_text(_h),
   "没订阅的陌生人也能看帮助")
_r = feishu.dispatch("订阅Pro", STRANGER)
ok("订阅" in _r and "Pro" in _r, "陌生人拿得到订阅入口")
ok(sub.paywall_text() not in _r,
   "订阅Pro 本身不能被 paywall 拦（拦了就永远开不了通）")

# ---- 订阅闸 ----
_r = feishu.dispatch("基金 161725", STRANGER, allow_side_effects=False)
ok(sub.paywall_text() in _r, "未订阅者发「基金」-> 被 paywall 拦")
_r = feishu.dispatch("关注基金 161725", STRANGER, allow_side_effects=False)
ok(sub.paywall_text() in _r, "未订阅者发「关注基金」-> 同样被拦")
_r = feishu.dispatch("删除基金 161725", STRANGER, allow_side_effects=False)
ok(sub.paywall_text() in _r, "未订阅者发「删除基金」-> 同样被拦")
_r = feishu.dispatch("基金关注", STRANGER, allow_side_effects=False)
ok(sub.paywall_text() in _r, "未订阅者发「基金关注」-> 同样被拦")

sub.cmd_activate(STRANGER, 99)
ok(sub.is_active(STRANGER), "开通后即在订阅期内")
_r = feishu.dispatch("基金 161725", STRANGER, allow_side_effects=False)
ok("试一下" in _r, "已订阅者过闸走到执行段（试一下模式不发网络）")
_r = feishu.dispatch("关注基金 161725", STRANGER, allow_side_effects=False)
ok("试一下" in _r, "已订阅者用关注指令也过闸")

# ---- owner 是超管：本人没订也放行（否则「试一下」演不动订阅指令）----
_r = feishu.dispatch("基金 161725", OWNER, allow_side_effects=False)
ok("试一下" in _r, "owner 没订阅也放行一切（超管）")

# ---- owner 未绑定：owner 指令给绑定提示，any 指令照旧可用 ----
db.kv_set(feishu._OWNER_KEY, "")
ok(feishu.dispatch("状态", OWNER).startswith("还没绑定"),
   "owner 未绑定时 owner 指令 -> 绑定提示（行为与旧版一致）")
ok(isinstance(feishu.dispatch("帮助", OWNER), dict),
   "owner 未绑定时仍能看帮助（access=any 不吃这道闸）")
db.kv_set(feishu._OWNER_KEY, OWNER)

ok(feishu.dispatch("状态", STRANGER).startswith("无权限"),
   "订阅者发 owner 指令 -> 无权限（订阅不等于超管）")

# ---- 私聊不改变授权：权限只看 sender，不看会话类型 ----
_a = feishu.dispatch("基金 161725", STRANGER, chat_id="oc_some_group",
                     allow_side_effects=False)
_b = feishu.dispatch("基金 161725", STRANGER, chat_id=STRANGER,
                     allow_side_effects=False)
ok(_a == _b,
   "带群 chat_id 与带本人 open_id 结果一致（私聊绕不过群里那道闸）")

# ==================== 2) 三条新前缀不与股票指令撞车 =========================
_CASES = [
    ("关注基金 161725", "fund_add"),
    ("删除基金 161725", "fund_del"),
    ("取消基金 161725", "fund_del"),
    ("基金 161725", "estimate"),
    ("基金关注", "fund_list"),
    ("我的基金", "fund_list"),
    ("订阅Pro", "sub"),
    ("开通 ou_abc 99", "activate"),
    # ---- 股票那套原样不动 ----
    ("关注 600825", "watch_add"),
    ("关注 600825 20260928", "watch_add"),
    ("删除 600825", "watch_del"),
    ("取关 600825", "watch_del"),
    ("600825", "query"),
    ("查询 600825", "query"),
    ("关注列表", "watch_list"),
    ("我的关注", "watch_list"),
]
for _text, _want in _CASES:
    _got = feishu._identify(_text)[0]
    ok(_got == _want, "%r -> %s（got %s）" % (_text, _want, _got))

# ==================== 3) 订阅态：开通、续期累加、过期即降级 ==================
NEW = "ou_new_person"
ok(db.sub_get(NEW) is None, "没登记过的人库里没有这一行")
ok(not sub.is_active(NEW), "没登记 -> 不是订阅者")

_row = db.sub_activate(NEW, amount=None)
ok(_row["plan"] == "pro", "开通后 plan='pro'")
ok(_row["amount"] == settings.pro_price,
   "金额留空时记全局占位价 %s（got %r）" % (settings.pro_price, _row["amount"]))
ok(sub.is_active(NEW), "刚开通在订阅期内")
_exp1 = _row["expires_at"]
_row2 = db.sub_activate(NEW)
ok(_row2["expires_at"] > _exp1,
   "续期**从原到期日累加**，不从今天重算（got %s -> %s）"
   % (_exp1, _row2["expires_at"]))

_conn = db._connect()
_conn.execute("UPDATE subscriber SET expires_at=? WHERE open_id=?",
              ("2020-01-01T00:00:00", NEW))
_conn.commit()
ok(not sub.is_active(NEW),
   "到期即降级（判据就是 expires_at，不需要定时任务改字段）")

# ---- 限额 ----
ok(sub.limit_of(NEW) == settings.fund_watch_limit,
   "没单独设置 -> 用全局上限 %d" % settings.fund_watch_limit)
db.sub_set(NEW, fund_limit=5)
ok(sub.limit_of(NEW) == 5, "单独设置优先于全局")
db.sub_set(NEW, fund_limit=0)
ok(sub.limit_of(NEW) == settings.fund_watch_limit,
   "设 0 = 清掉单独设置、回到全局默认")

# ==================== 4) 关注增删：有上限、且不碰全局表 ======================
WATCHER = "ou_watcher_person"
db.sub_activate(WATCHER)
_before_global = len(db.fund_watch_list())

_r = sub.cmd_add(WATCHER, "161725")
ok("已关注" in _r, "加关注成功")
ok("/%d" % settings.fund_watch_limit in _r, "回复带上名额计数")
_r = sub.cmd_add(WATCHER, "161725")
ok("已经在" in _r, "重复加 -> 明确说已经在，不会翻倍")
ok(len(db.fund_watch_list()) == _before_global,
   "**没有**污染全局 fund_watchlist（那是网页端「基金估值」的表）")
ok(len(db.sub_funds(WATCHER)) == 1, "只写进 subscriber_fund 一条")

_r = sub.cmd_remove(WATCHER, "110011")
ok("不在" in _r, "删一个没关注的 -> 明确说不在")
_r = sub.cmd_remove(WATCHER, "161725")
ok("已取消关注" in _r, "删成功")
ok(len(db.sub_funds(WATCHER)) == 0, "删完 subscriber_fund 空了")
ok(len(db.fund_watch_list()) == _before_global, "删除同样没碰全局表")

# ---- 超限：提示 + 不落库 ----
db.sub_set(WATCHER, fund_limit=2)
sub.cmd_add(WATCHER, "110011")
sub.cmd_add(WATCHER, "161725")
_r = sub.cmd_add(WATCHER, "001180")
ok("关注数量超出" in _r, "超过上限 -> 提示「关注数量超出」")
ok(len(db.sub_funds(WATCHER)) == 2, "被拒的那条**没有**落库（got %d）"
   % len(db.sub_funds(WATCHER)))

_r = sub.cmd_my_funds(WATCHER, allow=False)
ok("试一下" in _r, "试一下模式下「基金关注」不发网络")

# ==================== 5) /api/subs 接口 =====================================
app = FastAPI()
app.include_router(R.router)
c = TestClient(app)

_r = c.get("/api/subs")
ok(_r.status_code == 200, "GET /api/subs -> 200（got %d）" % _r.status_code)
_body = _r.json()
ok("items" in _body and "settings" in _body, "返回 items + settings 两块")
ok(_body["settings"]["fund_watch_limit"] == settings.fund_watch_limit,
   "settings 透出全局上限（页面显示默认值用）")
ok(_body["settings"]["push_hour"] == settings.push_hour
   and _body["settings"]["push_minute"] == settings.push_minute,
   "settings 透出推送时刻 %02d:%02d"
   % (settings.push_hour, settings.push_minute))
ok(any(x["open_id"] == WATCHER and x["fund_count"] == 2 and x["limit"] == 2
       for x in _body["items"]),
   "列表里能看到关注数与生效上限")
ok(any(x["open_id"] == STRANGER and x["active"] is True
       for x in _body["items"]),
   "列表标出谁在订阅期内")
ok(any(x["open_id"] == NEW and x["active"] is False
       for x in _body["items"]),
   "过期的在列表里标成未订阅")

_r = c.post("/api/subs", json={"open_id": "ou_page_person", "amount": 88})
ok(_r.status_code == 200, "POST /api/subs 开通 -> 200")
_item = _r.json()["item"]
ok(_item["active"] is True, "开通即 active")
ok(_item["amount"] == 88, "金额按传入值记账（got %r）" % _item["amount"])

_r = c.post("/api/subs/ou_page_person", json={"fund_limit": 7})
_item = _r.json()["item"]
ok(_item["fund_limit"] == 7, "行内改单独上限")
ok(_item["limit"] == 7, "limit 字段跟着折算成 7")

_r = c.post("/api/subs/ou_page_person", json={"close": True})
_item = _r.json()["item"]
ok(_item["active"] is False, "关闭订阅 -> active=false")
ok(_item["plan"] == "", "关闭 = plan 清空")
ok(_item["expires_at"] in ("", None),
   "关闭 = expires_at 一并清空，否则重新开通会把剩余天数接上（got %r）"
   % _item["expires_at"])

ok(c.get("/api/subs/ou_page_person/funds").status_code == 200,
   "GET .../funds -> 200")
ok(c.get("/api/subs/ou_page_person/pushes").status_code == 200,
   "GET .../pushes -> 200")
ok(c.post("/api/subs/ou_nobody", json={"close": True}).status_code == 404,
   "编辑不存在的订阅者 -> 404")

# ==================== 6) 落库编排搬进 db 之后名字还在 ========================
ok(R._record_fund_logs is db.record_fund_logs,
   "routes._record_fund_logs 仍是那个可调对象（别名保住了回归）")
ok(R._estimate_date is db.estimate_date, "routes._estimate_date 同上")
ok(R._estimate_date({"as_of": "2026-10-08 14:30"}) == "2026-10-08",
   "日期段提取照旧")
R._record_fund_logs([{
    "code": "161725", "estimate_pct": 1.5, "estimate_nav": 1.0001,
    "source": "官方估值", "coverage": None, "as_of": "2026-10-12 14:30",
    "nav": 1.0, "nav_date": "2026-10-11",
}])
ok(any(r["code"] == "161725" for r in db.fund_estimates()),
   "落库仍写进 fund_estimate_log（复盘样本没断）")

# ==================== 7) 14:50 推送 =========================================
# 桩死发送与取数：一条消息都不能真出去，一个估值包都不能真发。
_SENT = []


def _fake_send(target, text, id_type="chat_id"):  # noqa: ANN001
    _SENT.append((target, text, id_type))
    return True


feishu.send_text = _fake_send

_EST_CALLS = []


def _fake_estimate(items):  # noqa: ANN001
    _EST_CALLS.append([i["code"] for i in items])
    return ([{
        "code": i["code"], "name": "测试基金", "source": "官方估值",
        "estimate_pct": 1.23, "estimate_nav": 1.0456,
        "nav": 1.0, "nav_date": "2026-10-11",
        "as_of": "2026-10-12 14:30", "stale": False,
        "coverage": None, "note": None,
    } for i in items], [])


sub.fund_fetch.estimate = _fake_estimate

# 另一个订阅者也得先有关注，否则测不出「多用户逐人分发」
sub.cmd_add(STRANGER, "005827")

_due = sub.due_open_ids()
ok(WATCHER in _due, "订阅期内且有关注 -> 在推送名单")
ok(STRANGER in _due, "两个订阅者都在名单（逐人分发的前提）")
ok(NEW not in _due, "已过期的不在推送名单")
ok("ou_page_person" not in _due, "手动关闭的不在推送名单")
ok("ou_nobody" not in _due, "没关注基金的不在推送名单（不骚扰）")

_stat = sub.run_push()
ok(_stat["targets"] == len(_due), "目标数与名单对得上（got %d vs %d）"
   % (_stat["targets"], len(_due)))
ok(_stat["sent"] == _stat["targets"], "名单里每个都发出去了")
ok(len(_SENT) == _stat["sent"], "确实逐人调了 send_text")
ok([t for (t, _, _) in _SENT] == _due, "按 due 顺序逐人发送")
ok(all(idt == "open_id" for (_, _, idt) in _SENT),
   "一律按 open_id 私发，不是群 chat_id")
ok(len(_EST_CALLS) >= 1,
   "取数被调用了（桩生效，不会真发包）")
ok(len(_EST_CALLS) == 1,
   "所有人的关注**并起来只查一次**，不是每人查一轮（got %d 次）"
   % len(_EST_CALLS))

_by_id = {t: txt for (t, txt, _) in _SENT}
_body = _by_id.get(WATCHER, "")
ok("不构成" in _body and "投资建议" in _body,
   "推送正文带免责声明（硬口径）")
ok("161725" in _body and "110011" in _body,
   "WATCHER 收到的是**他**关注的那两只")
ok("005827" in _by_id.get(STRANGER, ""),
   "STRANGER 收到的是他关注的那一只")
ok(_body != _by_id.get(STRANGER, ""),
   "两个人的正文不同（按人拆开发，不是一条群发）")
ok("测试基金" in _body, "正文里带基金名（取数时顺带回填）")

_logs = db.push_log_tail(WATCHER, 5)
ok(len(_logs) > 0 and _logs[0]["kind"] == "fund_1450",
   "推送落 push_log（kind=fund_1450）")
ok(_logs[0]["ok"] == 1, "成功记 ok=1")

# ---- 发送失败也要留痕 ----
feishu.send_text = lambda *a, **k: False
_stat2 = sub.run_push()
ok(_stat2["failed"] == _stat2["targets"] and _stat2["targets"] > 0,
   "发送失败被统计（got %r）" % _stat2)
_logs = db.push_log_tail(WATCHER, 5)
ok(_logs[0]["ok"] == 0, "失败记 ok=0")
ok(bool(_logs[0]["detail"]), "失败带 detail 原因（订阅管理页要显示）")

# ---- 手动推送接口：不被 /subs/{open_id} 抢走 ----
feishu.send_text = _fake_send
_r = c.post("/api/subs/push")
ok(_r.status_code == 200,
   "POST /api/subs/push -> 200（没被 /subs/{open_id} 抢走，got %d）"
   % _r.status_code)
ok("push" in _r.json(), "返回推送统计")

# ---- schedule_info 透出第二个触发点 ----
_info = sch.schedule_info()
ok("push_hour" in _info and "push_minute" in _info,
   "schedule_info 带上推送时刻（前端要显示）")
ok(_info.get("push_hour") == settings.push_hour
   and _info.get("push_minute") == settings.push_minute,
   "推送时刻就是配置里的 %02d:%02d"
   % (settings.push_hour, settings.push_minute))

# ---- 两个开关各自独立（2026-10-12 用户拍板）----
# 桩掉交易日历：set_*_enabled 走 _reconcile -> start -> _warn_if_missed_*，
# 那里面要查交易日历，不桩会去触网（下面还有一条「全程零外发」的断言）。
sch.calendar.is_trade_day = lambda: False
# 草稿库里两个键都不存在，而 _ENABLED_KEY 缺省是开（既有行为不可改）——
# 显式关掉做基线，否则下面「没有被连带打开」的断言会被缺省值带偏。
db.kv_set(sch._ENABLED_KEY, "0")
db.kv_set(sch._PUSH_ENABLED_KEY, "0")
_info0 = sch.schedule_info()
ok(_info0.get("enabled") is False and _info0.get("push_enabled") is False,
   "初始两个开关各自独立关闭（got %r / %r）"
   % (_info0.get("enabled"), _info0.get("push_enabled")))

ok("push_enabled" in _info,
   "schedule_info 有独立的 push_enabled，不是一个合并的 enabled")
sch.set_push_enabled(True)
_info2 = sch.schedule_info()
ok(_info2.get("push_enabled") is True, "只开推送 -> push_enabled=true")
ok(_info2.get("enabled") is False,
   "**盘后任务没有被连带打开**（两个开关独立，got %r）" % _info2.get("enabled"))
sch.set_enabled(True)
_info3 = sch.schedule_info()
ok(_info3.get("enabled") is True, "再开盘后任务 -> enabled=true")
ok(_info3.get("push_enabled") is True, "推送没有被连带关掉")
sch.set_enabled(False)
sch.set_push_enabled(False)
_info4 = sch.schedule_info()
ok(_info4.get("enabled") is False and _info4.get("push_enabled") is False,
   "两个都能各自关回关闭状态（got %r / %r）"
   % (_info4.get("enabled"), _info4.get("push_enabled")))

# 路由：两个字段各自传，不传的那个不动
_r = c.post("/api/jobs/schedule", json={"push_enabled": True})
ok(_r.json()["schedule"]["push_enabled"] is True,
   "POST /jobs/schedule 只传 push_enabled 也能开推送")
ok(_r.json()["schedule"]["enabled"] is False,
   "不传 enabled 就不去动它（老调用方不受影响）")
_r = c.post("/api/jobs/schedule", json={"enabled": False, "push_enabled": False})
ok(_r.json()["schedule"]["push_enabled"] is False
   and _r.json()["schedule"]["enabled"] is False,
   "一次传两个也各改各的")
_r = c.post("/api/jobs/schedule", json={})
ok(_r.status_code == 200, "两个都不传 -> 200（只回状态，不改任何东西）")

# ==================== 8) 帮助卡片（2026-10-14 用户定：发指令卡片）==========
_card = feishu.help_card()
_ht = feishu._card_to_text(_card)
ok("订阅Pro" in _ht, "帮助里有订阅Pro")
ok("关注基金" in _ht, "帮助里有关注基金")
ok("删除基金" in _ht, "帮助里有删除基金")
ok("私聊" in _ht, "帮助说明可以私聊发指令")
ok("不构成" in _ht, "帮助带免责声明")

# 卡片本身：**update_multi 只能 true**（Card JSON 2.0 不支持独享卡片，
# 写 false 飞书当场拒收，2026-10-13 真机翻过车）
ok(_card.get("config", {}).get("update_multi") is True,
   "卡片 update_multi 必须是 true（1.0 的「独享卡片」直觉在这里是错的）")
ok(_card["header"]["title"]["content"] == "可用指令", "卡片标题")
_els = _card["body"]["elements"]
ok(not any(e.get("tag") == "button" or e.get("tag") == "column_set"
           for e in _els),
   "**不放按钮** —— 快捷指令卡才是「点一下就发」，误触「跑」就是一次真执行")
ok(all(e.get("tag") in ("markdown", "hr") for e in _els),
   "正文只用 markdown / hr 两种标签（2.0 没有 div）")
for _t in ("所有人都能用", "订阅后可用", "仅管理员"):
    ok(_t in _ht, f"帮助按权限分组列出「{_t}」")

# 数据源就是「指令管理」页：停用一条，卡片里跟着消失（`cmd_put` 走的是
# 「表里有行就覆盖默认值」那条路，空字段回落到 BUILTIN）
db.cmd_put({"key": "help", "kind": "builtin", "enabled": 0})
try:
    ok("帮助" not in feishu._card_to_text(feishu.help_card()),
       "停用的指令不出现在帮助卡片里")
finally:
    db.cmd_remove("help")
ok("帮助" in feishu._card_to_text(feishu.help_card()), "恢复默认后帮助又回来了")

# 自定义指令 target 指向「帮助」：卡片不能拿去拼附带文案，必须原样透传
# （`access="any"` 只给内置，自定义只能 sub/owner —— 见 db._CMD_ACCESS）
ok(db.cmd_put({"key": "t_help", "kind": "custom", "text": "说明", "alias": "",
               "target": "帮助", "reply": "附带一句", "access": "sub",
               "enabled": 1}) is not None, "建一条指向帮助的自定义指令")
try:
    _r = feishu.dispatch("说明", WATCHER)
    ok(isinstance(_r, dict), "target 指向卡片时原样透传，不拿 dict 去拼附带文案")
    ok("附带一句" not in feishu._card_to_text(_r), "卡片不接附带文案")
finally:
    db.cmd_remove("t_help")

# 「没听懂」与空输入也走卡片
ok(isinstance(feishu.dispatch("", OWNER), dict), "空输入回帮助卡片")
_r = feishu.dispatch("这玩意儿我完全不认识", OWNER)
ok(isinstance(_r, dict) and "没听懂" in feishu._card_to_text(_r),
   "认不出的输入回带「没听懂」前缀的帮助卡片")

# 「试一下」那个框是纯文本的，卡片得拍平了再回
_sim = feishu.simulate("帮助")
ok(isinstance(_sim["reply"], str) and "订阅Pro" in _sim["reply"],
   "simulate 把卡片拍平成文本（页面是纯文本框，装不下卡片）")

# ==================== 9) 收银台（docs/06 §2 · 占位演示） =====================
#
# 这一节盯四个「不测就会漏」的点：
#   a) 链接**不带 open_id**，带的是随机 token —— URL 会进浏览器历史、聊天
#      转发与 nginx 日志，裸 open_id 等于到处撒身份标识。
#   b) 金额是 `pro_price × 月数`，**后端算**：前端传金额就可能出现
#      「页面 297、库里 296.99」这种对不上的账。
#   c) 重复确认订单被挡（409）—— 否则连点两次订阅期间累加两遍。
#   d) token 过期即失效，不让链接永久有效。

import re as _re

# 换一个**没订阅过**的人：STRANGER 在前面已经被开通，回的是「你已订阅」，拿不到链接。
PAYER = "ou_payer_person"
_r = feishu.dispatch("订阅Pro", PAYER)
_m = _re.search(r"https?://\S*/pay/([A-Za-z0-9_\-]+)", _r)
_tok = _m.group(1) if _m else ""
ok(bool(_m), "订阅Pro 回的链接形如 http://…/pay/<token>")
ok(len(_tok) >= 16, "token 是长随机串（got %r）" % _tok[:24])
ok(PAYER not in _r,
   "链接里**不出现 open_id**（URL 会进历史/日志，裸 open_id 就是泄露）")

# ---- 档位与金额：后端算，1/3/6/12 个月线性 ----
_plans = {p["months"]: p for p in pay.plans()}
ok(sorted(_plans) == [1, 3, 6, 12], "档位是 1/3/6/12（got %s）" % sorted(_plans))
ok(_plans[1]["amount"] == settings.pro_price,
   "1 个月 = 单价 %s" % settings.pro_price)
ok(_plans[12]["amount"] == round(settings.pro_price * 12, 2),
   "12 个月 = 单价 × 12（线性不打折，got %s）" % _plans[12]["amount"])
ok(all(p["days"] == settings.pro_days * p["months"] for p in _plans.values()),
   "天数 = pro_days × 月数（不用日历月，避开 2026-02-31 这种日期）")

# ---- 页面首屏：不回 open_id ----
_r = c.get("/api/pay/%s" % _tok)
ok(_r.status_code == 200, "GET /api/pay/<token> -> 200（got %d）" % _r.status_code)
_info = _r.json()
ok("open_id" not in _info and "token" not in _info,
   "首屏**不回 open_id / token**（页面要的只是昵称与到期时间）")
ok(_info["nickname"] is not None and "plans" in _info,
   "带得回档位与昵称")

_r = c.get("/api/pay/not-a-real-token")
ok(_r.status_code == 404, "不存在的 token -> 404（got %d）" % _r.status_code)

# ---- 下单：金额后端算 ----
_r = c.post("/api/pay/%s/order" % _tok,
            json={"months": 3, "channel": "wechat"})
ok(_r.status_code == 200, "下单 3 个月微信 -> 200（got %d）" % _r.status_code)
_o = _r.json()["order"]
ok(_o["amount"] == round(settings.pro_price * 3, 2),
   "3 个月金额 = %s（后端算的，got %s）" % (round(settings.pro_price * 3, 2),
                                            _o["amount"]))
ok(_o["channel_label"] == "微信支付", "渠道带中文名（got %s）" % _o["channel_label"])
ok(_o["status"] == "pending", "新订单是 pending")

_r = c.post("/api/pay/%s/order" % _tok,
            json={"months": 3, "channel": "alipay"})
ok(_r.status_code == 200 and _r.json()["order"]["channel"] == "alipay",
   "支付宝也能下单")

_r = c.post("/api/pay/%s/order" % _tok,
            json={"months": 7, "channel": "wechat"})
ok(_r.status_code == 400, "不在档位里的月份 -> 400（got %d）" % _r.status_code)

_r = c.post("/api/pay/%s/order" % _tok,
            json={"months": 1, "channel": "bitcoin"})
ok(_r.status_code == 400, "不认的支付方式 -> 400（got %d）" % _r.status_code)

# ---- 收银台定价：每档可单独定价，留空按 单价 × 月数（2026-10-14 用户定）----
# 两条路由必须注册在 /subs/{open_id} 之前 —— 注册晚了会被 {open_id}="pricing"
# 抢走。与上面 `/api/subs/push` 那条是同一个坑，这里是它的定价版。
_r = c.get("/api/subs/pricing")
ok(_r.status_code == 200,
   "GET /api/subs/pricing -> 200（没被 /subs/{open_id} 抢走，got %d）"
   % _r.status_code)
_p = _r.json()["pricing"]
ok(_p["months"] == [1, 3, 6, 12],
   "没改过时档位 = 配置里的 1/3/6/12（got %s）" % _p["months"])
ok(_p["price"] == settings.pro_price, "没改过时单价 = settings.pro_price")
ok(_p["amounts"] == {}, "没改过时没有单档定价")

_UP = float(settings.pro_price)
_r = c.post("/api/subs/pricing",
            json={"price": _UP, "months": [1, 3, 6], "amounts": {"3": 268}})
ok(_r.status_code == 200, "保存定价 -> 200（got %d）" % _r.status_code)
_p = _r.json()["pricing"]
ok(_p["amounts"] == {"3": 268}, "回读带得回单档定价（got %r）" % _p["amounts"])
ok(pay.amount_of(3) == 268, "3 个月用单独价 268（got %s）" % pay.amount_of(3))
ok(pay.amount_of(1) == _UP, "1 个月没单独价 -> 单价 × 1（got %s）"
   % pay.amount_of(1))
ok(pay.amount_of(6) == round(_UP * 6, 2), "6 个月没单独价 -> 单价 × 6（got %s）"
   % pay.amount_of(6))
ok(pay.months_options() == [1, 3, 6], "档位跟着改，12 被移除（got %s）"
   % pay.months_options())
_pl = {p["months"]: p for p in pay.plans()}
ok(_pl[3]["amount"] == 268, "飞书/收银台的档位表也是 268（got %s）" % _pl[3]["amount"])

# 页面显示与订单落库必须同源 —— 否则就是「页面 268、订单 297」
_r = c.post("/api/pay/%s/order" % _tok,
            json={"months": 3, "channel": "wechat"})
ok(_r.status_code == 200, "按新价下单 -> 200（got %d）" % _r.status_code)
ok(_r.json()["order"]["amount"] == 268,
   "订单金额 = 页面显示的 268（got %s）" % _r.json()["order"]["amount"])

# 语义校验走 400（`pay.set_pricing` 抛 PayError），不是静默丢掉
_r = c.post("/api/subs/pricing", json={"price": 0, "months": [1], "amounts": {}})
ok(_r.status_code == 400, "单价非正 -> 400（got %d）" % _r.status_code)
_r = c.post("/api/subs/pricing", json={"price": _UP, "months": [], "amounts": {}})
ok(_r.status_code == 400, "档位为空 -> 400（got %d）" % _r.status_code)
_r = c.post("/api/subs/pricing",
            json={"price": _UP, "months": [1, 3], "amounts": {"12": 100}})
ok(_r.status_code == 400,
   "给不存在的档位定价 -> 400（否则改了档位还留着孤儿价）")
# 不传某档 = 那档回到线性，**不需要**额外的删除动作
_r = c.post("/api/subs/pricing",
            json={"price": _UP, "months": [1, 3, 6, 12], "amounts": {}})
ok(_r.status_code == 200, "清空单档定价 -> 200（got %d）" % _r.status_code)
ok(pay.amount_of(3) == round(_UP * 3, 2),
   "清空后回到 单价 × 3 = %s（got %s）" % (round(_UP * 3, 2), pay.amount_of(3)))

# kv 写坏必须回退 settings，不能让整条「订阅Pro」链路挂掉
db.kv_set(pay._PRICING_KEY, "{oops")
ok(pay.amount_of(3) == round(settings.pro_price * 3, 2),
   "kv 写坏 -> 金额回退 settings，不抛（got %s）" % pay.amount_of(3))
ok(pay.months_options() == [1, 3, 6, 12],
   "kv 写坏 -> 档位也回退配置（got %s）" % pay.months_options())
db.kv_set(pay._PRICING_KEY, "")
ok(pay.amount_of(3) == round(settings.pro_price * 3, 2),
   "清掉 kv -> 回到 settings 默认（got %s）" % pay.amount_of(3))

# ---- 邀请链接：一人一条，身份靠 token 反查（docs/06 §11）----
# `/invite/list` 与 `/invite/make` 必须注册在 `/invite/{token}` 之前 —— 与上面
# `/api/subs/push`、`/pay/orders` 是同一个「被路径参数抢走」的坑。
_r = c.get("/api/invite/list")
ok(_r.status_code == 200,
   "GET /api/invite/list -> 200（没被 /invite/{token} 抢走，got %d）"
   % _r.status_code)
ok("items" in _r.json() and "pro_days" in _r.json(),
   "名单接口带得回 pro_days")

from datetime import datetime as _dt2, timedelta as _td2  # noqa: E402

_INV = "ou_invitee_001"
db.sub_upsert(_INV, "小王")          # 名单 = 发过消息的人（feishu 落进来）
_r = c.get("/api/invite/list")
_row = [x for x in _r.json()["items"] if x["open_id"] == _INV]
ok(bool(_row), "发过消息就进名单")
ok(_row[0]["plan"] == "" and _row[0]["active"] is False,
   "在名单里但还没订阅（plan='' = 名单里没订，不是「没这行」）")

# 手工加名单：**一行飞书接口都不调** —— 长连接在公司内网连不上时这是唯一入口
_INV2 = "ou_invitee_002"
_r = c.post("/api/invite/join", json={"open_id": _INV2, "nickname": "小李"})
ok(_r.status_code == 200,
   "POST /api/invite/join -> 200（没被 /invite/{token} 抢走，got %d）"
   % _r.status_code)
ok(_r.json()["item"]["open_id"] == _INV2, "join 回得回来加进去的那个人")
_r = c.post("/api/invite/join", json={"open_id": _INV2, "nickname": "老李"})
ok(_r.status_code == 200, "重复 join 同一个人 -> 200（upsert，不是报错）")
_ids = [x["open_id"] for x in c.get("/api/invite/list").json()["items"]]
ok(_ids.count(_INV2) == 1, "名单里他只有一行，没有重出一行")
_row = [x for x in c.get("/api/invite/list").json()["items"]
        if x["open_id"] == _INV2][0]
ok(_row["nickname"] == "老李", "再次 join 覆盖昵称（sub_upsert 的 CASE WHEN）")
ok(c.post("/api/invite/join", json={"open_id": ""}).status_code == 422,
   "空 open_id -> 422（是类型校验不是 400，口径同定价）")

_r = c.post("/api/invite/make", json={"open_id": _INV})
ok(_r.status_code == 200, "生成邀请链接 -> 200（got %d）" % _r.status_code)
_link = _r.json()["link"]
ok(_INV not in _link["url"],
   "URL 里**不出现 open_id**（会进浏览器历史/聊天转发/nginx 日志）")
ok(len(_link["token"]) >= 16, "token 是长随机串（got %r）" % _link["token"][:24])

_itok = _link["token"]
_r = c.get("/api/invite/%s" % _itok)
ok(_r.status_code == 200, "GET /api/invite/<token> -> 200（got %d）" % _r.status_code)
_info = _r.json()
ok(_info["nickname"] == "小王", "欢迎页带得回昵称（got %r）" % _info["nickname"])
ok("open_id" not in _info and "token" not in _info,
   "欢迎页**不回 open_id / token**（页面要的只是显示个名字）")
ok(_info["active"] is False, "还没开通时 active=False")

_r = c.post("/api/invite/%s/activate" % _itok)
ok(_r.status_code == 200, "点订阅 -> 200（got %d）" % _r.status_code)
_res = _r.json()["result"]
ok(_res["already"] is False, "第一次点是真开通（already=False）")
ok(_res["amount"] == 0, "金额落 0 = 免费标记（got %r）" % _res["amount"])
_row = db.sub_get(_INV)
ok(_row["plan"] == "pro", "subscriber 落了 plan='pro'")
ok(_row["amount"] == 0, "库里 amount=0，订阅管理页据此显示「免费」")
_exp1 = str(_row["expires_at"])
_delta = _dt2.fromisoformat(_exp1) - _dt2.now()
ok(_td2(days=int(settings.pro_days) - 1) < _delta
   <= _td2(days=int(settings.pro_days)),
   "免费开通给 pro_days=%s 天" % settings.pro_days)

# 连点 / 把链接转给第二个人点：**不重复累加天数**，否则 30 天翻倍
_r = c.post("/api/invite/%s/activate" % _itok)
ok(_r.status_code == 200 and _r.json()["result"]["already"] is True,
   "再点一次 -> already=True（不重复累加）")
ok(str(db.sub_get(_INV)["expires_at"]) == _exp1, "到期时间没变")

_row = [x for x in c.get("/api/invite/list").json()["items"]
        if x["open_id"] == _INV][0]
ok(_row["active"] is True and _row["amount"] == 0,
   "名单里他已经显示「已订阅 + 免费」")

# 「关闭 → 重新开通」**不许把关闭前没用完的天数接上**。`sub_activate` 那条
# 「续期从原到期日往后累加」只该管真·续期；关闭即归零。2026-10-14 用户实测
# 这条链把 pro_days=30 刷成了 +60，正好绕开上面「连点两下不翻倍」的保护。
ok(c.post("/api/subs/%s" % _INV, json={"close": True}).status_code == 200
   and db.sub_get(_INV)["expires_at"] in ("", None),
   "关闭即归零：plan 与 expires_at 一并清空")
_r = c.post("/api/invite/%s/activate" % _itok)
ok(_r.status_code == 200 and _r.json()["result"]["already"] is False,
   "关闭后再点 -> 真开通（already=False）")
_delta = _dt2.fromisoformat(str(db.sub_get(_INV)["expires_at"])) - _dt2.now()
ok(_td2(days=int(settings.pro_days) - 1) < _delta
   <= _td2(days=int(settings.pro_days)),
   "重新开通从**今天**起算 pro_days=%s 天，不接原到期日（got exp=%s）"
   % (settings.pro_days, db.sub_get(_INV)["expires_at"]))

# 失效 token：不存在 / 已过期
_r = c.get("/api/invite/not-a-real-token")
ok(_r.status_code == 404, "不存在的邀请 token -> 404（got %d）" % _r.status_code)
_r = c.post("/api/invite/not-a-real-token/activate")
ok(_r.status_code == 400, "拿失效 token 点订阅 -> 400（got %d）" % _r.status_code)

_bad = db.invite_token_new(_INV, "小王", days=1)
with db._lock:
    _conn2 = db._connect()
    _conn2.execute("UPDATE invite_token SET expires_at=? WHERE token=?",
                   ((_dt2.now() - _td2(days=1)).isoformat(timespec="seconds"),
                    _bad["token"]))
    _conn2.commit()
ok(db.invite_token_get(_bad["token"]) is None, "过期的邀请 token 一律当不存在")
ok(c.get("/api/invite/%s" % _bad["token"]).status_code == 404,
   "过期链接打开 -> 404「已失效」")

# ---- 链接前缀：换网络时在页面上改，不用动 .env 也不用重启（存 kv）----
_r = c.get("/api/system/pay-base-url")
ok(_r.status_code == 200, "GET /api/system/pay-base-url -> 200")
ok(_r.json()["url"] == str(settings.pay_base_url).rstrip("/"),
   "没改过 -> 回落 .env 默认（got %r）" % _r.json()["url"])

try:
    _r = c.post("/api/system/pay-base-url",
                json={"url": "http://192.168.137.10:5173/"})
    ok(_r.status_code == 200, "保存前缀 -> 200（got %d）" % _r.status_code)
    _NEW = "http://192.168.137.10:5173"
    ok(pay.base_url() == _NEW, "末尾的 / 去掉了（got %r）" % pay.base_url())
    ok(pay.make_link(OWNER)["url"].startswith(_NEW + "/pay/"),
       "新收银台链接带上新前缀")
    ok(invite.make_link(OWNER, "小王")["url"].startswith(_NEW + "/invite/"),
       "新邀请链接也带上新前缀（两条链接共用一处，不会只改一半）")
    ok(c.get("/api/system/pay-base-url").json()["url"] == _NEW,
       "读回来还是改过的值（存 kv，不是进程内存）")

    # 少了这一刀：填个裸 IP 生成出来的是 `192.168.1.1/pay/xxx`，浏览器会当成
    # 相对路径拼在当前页上，点开是 404 而不是「连不上」
    _r = c.post("/api/system/pay-base-url", json={"url": "192.168.137.10:5173"})
    ok(_r.status_code == 400,
       "非法前缀（够长但非 http）-> 400 语义校验（got %d）" % _r.status_code)
    # 太短的先被 Pydantic min_length 挡成 422 —— 422 是类型/长度校验，
    # 400 是语义校验，两者口径不同（见 docs/06 定价卡片那段）
    _r = c.post("/api/system/pay-base-url", json={"url": "ftp://x"})
    ok(_r.status_code == 422,
       "非法前缀（太短）-> 422 类型校验，不是 400（got %d）" % _r.status_code)
    ok(pay.base_url() == _NEW, "被拒的保存不会把前缀改没")
finally:
    db.kv_set("pay_base_url", "")

ok(pay.base_url() == str(settings.pay_base_url).rstrip("/"),
   "清掉 kv -> 回到默认")

# ---- 推送链接：生成 + 飞书私聊发给他（2026-10-14 用户定）----
# **把 send_text 换成桩**：真的发会打到 open.feishu.cn。这里只验「发给谁、
# 用哪种 id_type、正文里有没有那条链接」，一句都不出网。
_calls = []
_orig_send = feishu.send_text


def _fake_send(chat_id, text, id_type="chat_id"):  # noqa: ANN001
    _calls.append((chat_id, text, id_type))
    return True


feishu.send_text = _fake_send
try:
    _r = c.post("/api/invite/push",
                json={"open_id": OWNER, "nickname": "老王"})
    ok(_r.status_code == 200, "推送 -> 200（got %d）" % _r.status_code)
    _b = _r.json()
    ok(_b["sent"] is True, "回执 sent=True")
    ok("/invite/" in _b["link"]["url"], "链接照常生成（got %r）" % _b["link"]["url"])
    ok(len(_calls) == 1, "真的调了一次 send_text")
    _cid, _txt, _idt = _calls[0]
    ok(_cid == OWNER, "发给名单里那个人本人（got %r）" % _cid)
    ok(_idt == "open_id", "用 id_type=open_id 走单聊，不发群 webhook（got %r）" % _idt)
    ok(_b["link"]["url"] in _txt, "正文里带着刚生成的那条链接")
    ok(str(settings.pro_days) in _txt, "正文里写了开通多少天")

    # 发不出去（凭据 / 应用权限 / 对方不可达）不能把「拿到链接」也一起废掉
    feishu.send_text = lambda *a, **k: False
    _b = c.post("/api/invite/push", json={"open_id": OWNER}).json()
    ok(_b["sent"] is False, "飞书拒了 -> sent=False")
    ok("/invite/" in _b["link"]["url"], "sent=False 时链接照样给，复制手发即可")

    # 空 open_id 在 Pydantic 那层就被挡（min_length=1）—— 422 是类型校验，
    # 与 400 那种语义校验口径不同（定价、前缀两处同款）
    _r = c.post("/api/invite/push", json={"open_id": ""})
    ok(_r.status_code == 422, "空 open_id -> 422（got %d）" % _r.status_code)
finally:
    feishu.send_text = _orig_send

ok(feishu.send_text is _orig_send, "send_text 已还原，后续用例不会被桩挡住")

# ---- 管理端：列表在参数路由之前注册，否则被 {token} 抢走 ----
_r = c.get("/api/pay/orders")
ok(_r.status_code == 200, "GET /api/pay/orders -> 200（没被 /pay/{token} 抢走）")
ok(len(_r.json()["items"]) >= 2, "订单列表看得到刚下的两张")

_no = _o["order_no"]
_r = c.post("/api/pay/orders/%s/confirm" % _no)
ok(_r.status_code == 200, "确认开通 -> 200")
ok(_r.json()["order"]["status"] == "done", "确认后 status=done")

_r = c.post("/api/pay/orders/%s/confirm" % _no)
ok(_r.status_code == 409, "重复确认 -> 409（否则订阅期间累加两遍）")

_row = db.sub_get(_o["open_id"])
ok(_row and _row["plan"] == "pro", "确认订单真的开通了订阅")
ok(_row["amount"] == _o["amount"], "金额按订单记账（got %s）" % _row["amount"])

_r = c.get("/api/pay/orders/P0000000000000000")
ok(_r.status_code != 200, "确认不存在的订单 -> 4xx（got %d）" % _r.status_code)

# ---- d) token 过期即失效 ----
# 手工把这张 token 的到期时间改到昨天，再问一次：链接必须立刻作废。
# 少这条断言的话，「过期」只是写在 docstring 里的愿望 —— 哪天有人把
# `pay_token_get` 里的日期判断删掉，回归照样绿。
_expired = db.pay_token_get(_tok)
ok(_expired is not None, "刚发的 token 现在有效")
from datetime import datetime as _dt, timedelta as _td     # noqa: E402
with db._lock:
    _conn = db._connect()
    _conn.execute("UPDATE pay_token SET expires_at=? WHERE token=?",
                  ((_dt.now() - _td(days=1)).isoformat(timespec="seconds"), _tok))
    _conn.commit()
ok(db.pay_token_get(_tok) is None, "过期的 token 一律当不存在")
_r = c.get("/api/pay/%s" % _tok)
ok(_r.status_code == 404, "过期链接打开 -> 404「已失效」（got %d）" % _r.status_code)
_r = c.post("/api/pay/%s/order" % _tok, json={"months": 1, "channel": "wechat"})
ok(_r.status_code == 400, "拿过期链接下单 -> 400（got %d）" % _r.status_code)

# ==================== 10) 全程零外发 =========================================
ok(len(_BLOCKED) == 0,
   "全程没有试图外发（被挡 %d 次）: %s" % (len(_BLOCKED), _BLOCKED[:3]))

print("")
print("ALL %d ASSERTIONS PASSED (outbound attempts blocked: %d)"
      % (N[0], len(_BLOCKED)))
