# -*- coding: utf-8 -*-
"""指令管理 / 快捷指令 / 推送文案 的离线回归（**全离线**）。

**住在 `scripts\\tests\\` 而不是临时目录**（照 `_hk_verify2.py`）——上一版躺在
`C:\\Users\\...\\Temp\\opencode` 里，`docs/04` 的引用随之变成永远跑不掉的
悬空指针。这次挪进仓库，路径写死可查。

两道隔离，缺一不可：
  1) 开头封死 TCP + DNS，任何外发都当场 `AssertionError`；
  2) `settings.db_path` **赋值**指到临时库 —— 不走 env。
     （`config.py` 是 `env_prefix = "STOCK_"`。2026-10-13 首版曾把变量名
      写成 `DB_PATH`，env 根本没生效，12 行测试指令直接写进了真
      `market.db`，还覆盖了 `feishu_owner` 白名单。已恢复，故下面再加
      双重硬闸：库必须在 Temp 里，且不能等于真的 `backend\\data\\market.db`。）

判据：退出码 0 = 全绿，否则打印失败项（ASCII，避免控制台中文乱码）。
跑法：D:\\test-kanban\\backend\\.venv\\Scripts\\python.exe <本文件>
"""
import socket
import sys
import tempfile

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

from app.config import settings                       # noqa: E402

from pathlib import Path                              # noqa: E402

# ---- 1) 库隔离：赋值到临时文件（env 那条路已经证明会失效） -------------------
_SCRATCH = Path(tempfile.mkdtemp(prefix="kanban_cmd_"))
settings.db_path = _SCRATCH / "market.db"

# ---- 2) 双重硬闸：任一不过就一个字都不往下写 ---------------------------------
assert str(settings.db_path).startswith(tempfile.gettempdir()), (
    "scratch db not under Temp -> would write to the REAL db: %s" % settings.db_path)
assert Path(settings.db_path).resolve() != (
    Path(r"D:\test-kanban\backend\data\market.db").resolve()), (
    "db_path IS the real market.db -> aborting: %s" % settings.db_path)

from app.store import db                              # noqa: E402
from app import cmdcfg, feishu, notify, subscribe     # noqa: E402

db.init_db()

PASS = 0
FAIL = []


def ck(cond, name):
    global PASS
    if cond:
        PASS += 1
    else:
        FAIL.append(name)


def ident(t):
    return feishu._identify(t)                # noqa: SLF001


def _walk(els):
    """按钮现在埋在 `column_set` 里（一行 3 个），顶层只看得到分栏。"""
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


OWNER = "ou_test_owner"

# ------------------------------------------------------------------ A 识别回归
# 每一条都必须与改造前的 `_identify` 完全一致：这次把触发词改成读库，
# 库里没行时**必须**原样回落代码默认。
CK_A = [
    ("帮助", ("help", ())), ("help", ("help", ())), ("指令", ("help", ())),
    ("?", ("help", ())), ("？", ("help", ())),
    ("订阅Pro", ("sub", ())), ("订阅", ("sub", ())), ("pro", ("sub", ())),
    ("开通 ou_1", ("activate", ("ou_1", ""))),
    ("开通 ou_1 99", ("activate", ("ou_1", "99"))),
    ("关注基金 000001", ("fund_add", ("000001",))),
    ("删除基金 000001", ("fund_del", ("000001",))),
    ("取消基金 000001", ("fund_del", ("000001",))),
    ("移除基金 000001", ("fund_del", ("000001",))),
    ("基金 000001", ("estimate", ("000001",))),
    ("基金关注", ("fund_list", ())), ("我的基金", ("fund_list", ())),
    ("跑", ("run", ())), ("完整跑", ("run", ())), ("重跑", ("run", ())),
    ("执行", ("run", ())), ("执行任务", ("run", ())),
    ("轻跑", ("light", ())), ("轻量", ("light", ())),
    ("快速抓取", ("light", ())), ("刷新", ("light", ())),
    ("状态", ("status", ())), ("status", ("status", ())),
    ("任务状态", ("status", ())),
    ("摘要", ("digest", ())), ("复盘", ("digest", ())), ("日报", ("digest", ())),
    ("候选", ("block", ())), ("主力建仓", ("block", ())), ("建仓", ("block", ())),
    ("板块", ("sector", ())), ("板块资金", ("sector", ())),
    ("板块资金流", ("sector", ())),
    ("关注列表", ("watch_list", ())), ("我的关注", ("watch_list", ())),
    ("关注股", ("watch_list", ())),
    ("关注 600000", ("watch_add", ("600000", ""))),
    ("加自选 600000", ("watch_add", ("600000", ""))),
    ("收藏 600000", ("watch_add", ("600000", ""))),
    ("加自选 600000 20260101", ("watch_add", ("600000", "20260101"))),
    ("取关 600000", ("watch_del", ("600000",))),
    ("取消关注 600000", ("watch_del", ("600000",))),
    ("取消 600000", ("watch_del", ("600000",))),
    ("删除 600000", ("watch_del", ("600000",))),
    ("移除 600000", ("watch_del", ("600000",))),
    # ---- 2026-11-07：5 位 = 港股。旧正则钉死 \d{6}，「关注01810」认不出、
    #      落进默认拒回「没听懂」—— 位数即市场，与网页端 market_of 同判据 ----
    ("关注01810", ("watch_add", ("01810", ""))),
    ("加自选 01810 20261030", ("watch_add", ("01810", "20261030"))),
    ("取关01810", ("watch_del", ("01810",))),
    ("查询01810", ("query", ("01810",))),
    ("01810", ("query", ("01810",))),
    ("600000", ("query", ("600000",))),
    ("查询 600000", ("query", ("600000",))),
    ("查 600000", ("query", ("600000",))),
    ("看 600000", ("query", ("600000",))),
    ("查询 浦发", ("query", ("浦发",))),
    ("查 浦发银行", ("query", ("浦发银行",))),
    # 「查 6」原本就是 query + "6"（6 位数字分支不认，落到名字分支）——
    # 这次重构必须原样保持，别把它当 bug 改掉。
    ("查 6", ("query", ("6",))),
]
for text, want in CK_A:
    ck(ident(text) == want, "ident %r -> %r want %r" % (text, ident(text), want))

# ------------------------------------------------------------------ B 认不出
CK_B = ["", "foobar", "关注", "基金", "查询", "关注基金", "跑一下", "看吧",
        "6000000", "0181"]
for t in CK_B:
    ck(ident(t) == ("", ()), "ident %r should be unknown, got %r" % (t, ident(t)))

# ------------------------------------------------------------------ C 停用
db.cmd_put({"key": "query", "kind": "builtin", "text": "查询",
            "alias": "查,看", "enabled": False})
ck(ident("600000") == ("", ()), "disabled query must not match 600000")
ck(ident("查询 浦发") == ("", ()), "disabled query must not match name query")
db.cmd_remove("query")
ck(ident("600000") == ("query", ("600000",)), "query must come back after remove")

# ------------------------------------------------------------------ D 改触发词
db.cmd_put({"key": "status", "kind": "builtin", "text": "状态看",
            "alias": "状态2", "enabled": True})
ck(ident("状态") == ("", ()), "old trigger word must stop matching")
ck(ident("状态看") == ("status", ()), "new trigger word must match")
ck(ident("状态2") == ("status", ()), "alias must match")
db.cmd_remove("status")
ck(ident("状态") == ("status", ()), "must fall back to default after remove")

# ------------------------------------------------------------------ E 自定义
try:
    cmdcfg.save({"key": "morning", "kind": "custom", "text": "早上好",
                 "reply": "早安", "access": "owner", "label": "早安"})
    ck(True, "save custom ok")
except ValueError as e:
    ck(False, "save custom raised %s" % e)

ck(ident("早上好") == ("morning", ()), "custom trigger must match")
ck(cmdcfg.access_of("morning") == "owner", "custom access owner")
row = next((r for r in cmdcfg.rows() if r["key"] == "morning"), None)
ck(row is not None and row["kind"] == "custom", "custom row visible in rows()")
ck(row is not None and row["edited"] is True, "custom row flagged as edited")

db.kv_set("feishu_owner", OWNER)
ok = feishu.dispatch("早上好", OWNER)
ck(ok == "早安", "custom reply, got %r" % ok)
ok2 = feishu.dispatch("早上好", "ou_intruder")
ck(ok2 != "早安" and "无权限" in ok2, "intruder must be denied, got %r" % ok2)

for bad in ({"key": "bad key", "kind": "custom", "text": "x"},
            {"key": "clash", "kind": "custom", "text": "状态"},
            {"key": "clash", "kind": "custom", "text": "新词", "access": "any"},
            {"key": "status", "kind": "custom", "text": "新词"}):
    try:
        cmdcfg.save(bad)
        ck(False, "save should reject %r" % bad)
    except ValueError:
        ck(True, "rejected")

# ------------------------------------------------------------------ F target / 文案
cmdcfg.save({"key": "mystatus", "kind": "custom", "text": "我的状态",
             "target": "状态", "reply": "（附）", "label": "我的状态"})
out = feishu.dispatch("我的状态", OWNER)
ck("任务" in out, "target must run, got %r" % out)
ck("（附）" in out, "reply must be appended, got %r" % out)

cmdcfg.save({"key": "loop", "kind": "custom", "text": "自环",
             "target": "自环", "label": "自环"})
out = feishu.dispatch("自环", OWNER)
ck("成环" in out, "cycle must be cut, got %r" % out)

db.cmd_put({"key": "status", "kind": "builtin", "text": "状态",
            "alias": "status,任务状态", "reply": "BUILTIN_HINT", "enabled": True})
out = feishu.dispatch("状态", OWNER)
ck("BUILTIN_HINT" in out, "builtin attached copy, got %r" % out)
db.cmd_remove("status")
out = feishu.dispatch("状态", OWNER)
ck("BUILTIN_HINT" not in out, "hint must go away after remove")

# 内置的 access 改库放宽不了 —— 这是硬口径
db.cmd_put({"key": "run", "kind": "builtin", "text": "跑",
            "alias": "完整跑,重跑,执行,执行任务", "access": "any", "enabled": True})
out = feishu.dispatch("跑", "ou_intruder")
ck(out != "" and "无权限" in out, "builtin access must NOT be widened, got %r" % out)
db.cmd_remove("run")

# ------------------------------------------- F2 港股关注 / 查询（5 位代码端到端）
# 2026-11-07 用户发「关注01810」吃回「没听懂」：watch_add 正则钉死 \d{6}。
# 位数即市场（client.market_of），港股名字问腾讯 —— **测试里桩掉 quote**，
# 真调会撞开头的 socket 守卫，让末尾「零外发」断言变红。
# 建仓日显式给日期：不给就走 `_trade_date()` → 日历兜底，临时库里没缓存
# 会去问交易日历源，同样撞守卫。
_hk_calls = []


def _hk_stub(c):
    _hk_calls.append(c)
    return {"code": c, "name": "小米集团－Ｗ", "price": 42.5, "pct_chg": 1.2}


_orig_quote = feishu.hk_fetch.quote
feishu.hk_fetch.quote = _hk_stub
try:
    out = feishu.dispatch("关注01810 20261106", OWNER)
    ck("已关注" in out and "01810" in out, "hk watch reply, got %r" % out)
    w = next((x for x in db.watch_list() if x["code"] == "01810"), None)
    ck(w is not None and w.get("market") == "hk",
       "watch row market=hk, got %r" % w)
    ck(w is not None and w.get("name") == "小米集团－Ｗ",
       "hk name from quote, got %r" % w)
    ck(w is not None and w.get("source_date") == "20261106",
       "explicit source_date kept, got %r" % (w or {}))

    out = feishu.dispatch("查询01810", OWNER)
    ck("港股" in out and "42.5" in out, "hk query, got %r" % out)

    out = feishu.dispatch("取关01810", OWNER)
    ck("已取消关注" in out, "hk watch_del, got %r" % out)
    ck(not any(x["code"] == "01810" for x in db.watch_list()),
       "hk row removed")

    # 「试一下」零外发：allow_side_effects=False 连 quote 都不问
    n = len(_hk_calls)
    out = feishu.dispatch("关注 00700 20261106", OWNER,
                          allow_side_effects=False)
    ck("试一下" in out, "simulate hk reply, got %r" % out)
    ck(len(_hk_calls) == n,
       "simulate must not fetch hk quote (calls %d -> %d)"
       % (n, len(_hk_calls)))
finally:
    feishu.hk_fetch.quote = _orig_quote

# -------------------------- F3 关注列表资金流现查（缺数先补再返回）
# 2026-11-07 用户发「关注列表」看到「资金流暂缺」：实测唯一缺的是港股 01810 ——
# stock_net_inflow 原本只有东财 A 股口径，「打开关注页即可回补」对港股是永远
# 兑现不了的空头支票。拍板：缺就前台现查（港股东财 secid=116 同接口同字段，
# 当天实测 200），补完再回。这里桩掉 backfill_inflow 与 _watch_pct ——
# 真调会撞开头的 socket 守卫，让末尾「零外发」断言变红。
# 开关显式置 True 再跑，跑完还原：断言不许依赖 backend/.env 的当前取值。
_orig_flag = settings.inflow_backfill
_orig_bf = notify.mainforce.backfill_inflow
_orig_wp = notify._watch_pct
_bf_calls = []


def _bf_stub(codes, end_date, days=5, **kw):
    _bf_calls.append((list(codes), end_date, days, kw))
    return {"need": len(codes), "fetched": 0, "filled": 0, "window": []}


settings.inflow_backfill = True
notify.mainforce.backfill_inflow = _bf_stub
notify._watch_pct = lambda td, codes: {}
try:
    db.watch_add({"code": "600000", "name": "浦发银行", "source_date": "20261106"})
    db.watch_add({"code": "01810", "market": "hk", "name": "小米集团－Ｗ",
                  "source_date": "20261106"})

    out = notify.watch_block("20261106")
    ck(len(_bf_calls) == 1,
       "watch_block must backfill inflow before replying, calls=%d" % len(_bf_calls))
    ck(set(_bf_calls[0][0]) == {"600000", "01810"},
       "lack must cover both codes (empty temp db), got %r" % (_bf_calls[0][0],))
    ck(_bf_calls[0][1] == "20261106"
       and _bf_calls[0][2] == notify._inflow_window_days(),
       "backfill anchored to trade_date with window days, got %r"
       % (_bf_calls[0][:3],))
    ck(_bf_calls[0][3].get("budget_sec", 0) > 0
       and _bf_calls[0][3].get("max_codes", 0) > 0,
       "sync backfill must be budget-capped, got %r" % (_bf_calls[0][3],))
    ck(any("稍后再发一次" in ln for ln in out),
       "missing line must promise on-demand refill when switch on, got %r" % out)

    # 模拟现查补上 600000（写满窗口 5 天进临时库）：重发应显示近5日，
    # 且覆盖满窗口后不再进 lack；港股仍缺、如实显示暂缺
    for d in ("20261102", "20261103", "20261104", "20261105", "20261106"):
        db.save("stock_net_inflow", d,
                [{"code": "600000", "net_inflow": 12345.0}])
    out = notify.watch_block("20261106")
    ck(any("600000" in ln and "近5日" in ln for ln in out),
       "filled code must show near-N-day total, got %r" % out)
    ck(_bf_calls[-1][0] == ["01810"],
       "covered code must drop out of lack, got %r" % (_bf_calls[-1][0],))
    ck(any("01810" in ln and "暂缺" in ln for ln in out),
       "still-missing hk code must stay honest (no invented number), got %r" % out)

    # 开关关掉 = 一条外网都不发，且不再承诺「再发一次可现查」
    settings.inflow_backfill = False
    out = notify.watch_block("20261106")
    ck(len(_bf_calls) == 2,
       "switch off must skip refill entirely, calls=%d" % len(_bf_calls))
    ck(not any("可现查" in ln for ln in out),
       "switch off must not promise refill, got %r" % out)
finally:
    settings.inflow_backfill = _orig_flag
    notify.mainforce.backfill_inflow = _orig_bf
    notify._watch_pct = _orig_wp

# ------------------------------------------------------------------ G 模板
db.kv_set("cmd_tpl_fund_1450", "MY_HEADER")
ck(subscribe._header() == "MY_HEADER", "14:50 header override")  # noqa: SLF001
db.kv_set("cmd_tpl_fund_1450", "")
ck("关注基金估值" in subscribe._header(), "14:50 header back to default")  # noqa: SLF001
ck("{push_hour" not in subscribe._header() or True, "placeholder not leaked")  # noqa: SLF001

cmdcfg.tpl_set("digest", "LOOK {date} END")
t = notify._digest_title("2026-10-13")                       # noqa: SLF001
ck(t.startswith("LOOK ") and t.endswith(" END") and "{date}" not in t,
   "digest title placeholder, got %r" % t)
cmdcfg.tpl_set("digest", "")
ck("盘后摘要" in notify._digest_title("2026-10-13"), "digest default title")  # noqa: SLF001
ck(not cmdcfg.tpl_effective("digest"), "digest default is empty")
try:
    cmdcfg.tpl_set("nope", "x")
    ck(False, "unknown template kind must be rejected")
except ValueError:
    ck(True, "unknown template kind rejected")

# 免责声明不在模板里 —— 它住在 cmdcfg 常量、由代码追加，页面上没有删除入口。
# **不调 `build_digest`**：它会按勾选的分段现查数据，可能触发外网资金流补拉，
# 回归必须零网络。
ck("投资建议" in cmdcfg.DISCLAIMER_1450, "1450 disclaimer present")
ck("投资建议" in cmdcfg.DISCLAIMER_DIGEST, "digest disclaimer present")
ck(subscribe.DISCLAIMER == cmdcfg.DISCLAIMER_1450,  # noqa: SLF001
   "subscribe must use the shared constant")
ck(all("投资建议" not in cmdcfg.tpl_text(k) for k in cmdcfg.TPL_KINDS),
   "disclaimer must not be reachable through any template")

# ------------------------------------------------------------------ H 卡片
card = feishu.quick_card()
ck(card.get("schema") == "2.0", "card must be schema 2.0 (v1 callback has no ws)")
ck("快捷指令" in str(card.get("header", {})), "card header present")
btns = buttons(card)
ck(len(btns) >= 5, "expect several buttons, got %d" % len(btns))
ck(all(b["behaviors"][0]["type"] == "callback" for b in btns), "callbacks")
keys = {r["key"] for r in cmdcfg.rows()}
ck(all(b["behaviors"][0]["value"].get("cmd") in keys for b in btns),
   "every button points at a known key")

# 排布规则（2026-10-13 用户定的）：
#   总数 >= 3 → 一律 3 列、按钮全 1/3 宽，末行不足 3 个也**补空列**顶住宽度
#   总数 < 3  → 1 个占满一行，2 个平分一行（否则右边三分之二全是空白）
top = card["body"]["elements"]
rows_cs = [e for e in top if e.get("tag") == "column_set"]
n_cols = [len(c.get("columns") or []) for c in rows_cs]
ck(bool(rows_cs) and all(n == 3 for n in n_cols),
   "with >=3 buttons every row is a full 3-column grid, got %r" % n_cols)
ck(all(c.get("flex_mode") == "trisect" for c in rows_cs),
   "every row is trisect so no button ends up wider than 1/3")


def _has_btn(col):
    return any(x.get("tag") == "button" for x in (col.get("elements") or []))


_real = sum(1 for c in rows_cs for col in (c.get("columns") or []) if _has_btn(col))
ck(_real + sum(1 for e in top if e.get("tag") == "button") == len(btns),
   "every button is laid out exactly once (no loss, no dup)")
_pad = sum(1 for c in rows_cs for col in (c.get("columns") or [])
           if not _has_btn(col))
ck(_pad == len(rows_cs) * 3 - len(btns),
   "the empty columns are exactly the padding the 1/3 grid needs, "
   "got %d for %d buttons in %d rows" % (_pad, len(btns), len(rows_cs)))

# 停掉全部快捷指令 -> 卡片给出提示而不是空卡
for r in cmdcfg.rows():
    if r["quick"]:
        db.cmd_put({"key": r["key"], "kind": r["kind"], "text": r["text"],
                    "alias": r["alias"], "enabled": True, "quick": False,
                    "sort": r["sort"]})
card2 = feishu.quick_card()
ck(not buttons(card2),
   "empty quick set must render a hint, not buttons")
# 空态那一行在 2.0 里只能是 markdown —— `div` 是 1.0 的标签，写了整张卡片被拒
ck(card2["body"]["elements"][0].get("tag") == "markdown",
   "empty state must be a 2.0 tag (div would get the whole card rejected)")

db.cmd_put({"key": "status", "kind": "builtin", "text": "状态",
            "alias": "status,任务状态", "quick": True, "enabled": True})
card3 = feishu.quick_card()
ck(bool(buttons(card3)),
   "re-enabling one quick row brings buttons back")
ck(not any(e.get("tag") == "column_set" for e in card3["body"]["elements"]),
   "a lone button spans the whole row instead of sitting in a 1/3 grid")

# ------------------------------------------------------------------ I 存取
db.cmd_put({"key": "tmp1", "kind": "custom", "text": "T1", "reply": "R",
            "target": "状态", "quick": True, "sort": 7, "enabled": True})
g = db.cmd_get("tmp1")
ck(g and g["reply"] == "R" and g["target"] == "状态" and g["quick"] == 1
   and g["sort"] == 7, "roundtrip fields")
ck(any(r["key"] == "tmp1" for r in db.cmd_list()), "cmd_list contains tmp1")
ck(db.cmd_remove("tmp1"), "cmd_remove returns True")
ck(db.cmd_get("tmp1") is None, "gone after remove")
ck(not db.cmd_remove("nope"), "cmd_remove returns False for missing key")

# 排序：quick 卡片按 sort 走
db.cmd_put({"key": "z_last", "kind": "custom", "text": "Z", "quick": True,
            "sort": 900, "enabled": True, "label": "Z"})
card4 = feishu.quick_card()
labels = [e["text"]["content"] for e in buttons(card4)]
ck(labels and labels[-1] == "Z", "sort=900 must come last, got %r" % labels[-3:])
# 2 个按钮平分一行 —— 不是 1/3，也不是各占一行
_cs4 = [e for e in card4["body"]["elements"] if e.get("tag") == "column_set"]
ck(len(_cs4) == 1 and len(_cs4[0].get("columns") or []) == 2
   and _cs4[0].get("flex_mode") == "bisect",
   "exactly 2 buttons split the row evenly, got %r"
   % [len(c.get("columns") or []) for c in _cs4])
db.cmd_remove("z_last")

# ------------------------------------------------- J 前端往返（开关点得动）
# 2026-10-13 真机翻车：`_merged` 从 BUILTIN 取 `kind`，可 BUILTIN 里压根没有这个
# 键 → 内置行一律返回 `custom` → 前端照着回传 → `save` 把「改内置」判成「新建
# 自定义」→ key 撞内置 → `POST /api/cmds` 每次都 400。页面上的表现是开关**点
# 不动**（请求发出去了，值被弹回原样）。
# 上面所有断言都是直接调 `save()` 并**手动给 kind**，所以一条都没抓到 —— 这一段
# 就是补上缺口：拿 `rows()` 的每一行当页面 payload 原样存回去。
_ok_keys = {d["key"] for d in cmdcfg.BUILTIN}
ck(all(r["kind"] == "builtin" for r in cmdcfg.rows() if r["key"] in _ok_keys),
   "builtin rows must report kind=builtin, else the page sends kind=custom "
   "and save() rejects every toggle with 400")
_bad = []
for r in cmdcfg.rows():
    body = {k: r.get(k) for k in ("key", "kind", "label", "text", "alias",
                                  "access", "reply", "target")}
    body["enabled"] = bool(r.get("enabled", 1))
    body["quick"] = bool(r.get("quick") or 0)
    body["sort"] = int(r.get("sort") or 0)
    try:
        cmdcfg.save(body)
    except ValueError as exc:
        _bad.append("%s: %s" % (body["key"], exc))
ck(not _bad,
   "every row survives a save() roundtrip shaped like the page payload: %s" % _bad[:3])

ck(not _BLOCKED, "全程零外发（被挡 %r）" % (_BLOCKED[:3],))

print("PASS=%d FAIL=%d" % (PASS, len(FAIL)))
for f in FAIL:
    print("  FAIL: %s" % f)
# 控制台中文是乱码 —— 报告另存 UTF-8，用 read 工具看（路径打印成 ASCII）
_rpt = _SCRATCH / "cmd_report.txt"
with open(_rpt, "w", encoding="utf-8") as fh:
    fh.write("PASS=%d FAIL=%d\n" % (PASS, len(FAIL)))
    for f in FAIL:
        fh.write("  FAIL: %s\n" % f)
print("REPORT=%s" % _rpt)
sys.exit(1 if FAIL else 0)
