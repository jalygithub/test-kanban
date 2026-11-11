"""指令配置：内置指令的默认值、用户改过的那部分、推送文案模板。

三层职责，别混：

  - **内置指令的定义**（默认触发词 / 别名 / 说明 / 是否进快捷卡）是本文件的
    代码常量。表里没有对应行就用它。
  - **用户改过的部分**在 `cmd_item`（`store/db.py`）。删行 = 恢复默认，
    不存在「表里一份、代码里一份」两种真相。
  - **真正的授权闸**仍在 `feishu._ACCESS`。内置指令的 `access` 在库里只做
    展示，改它不会放宽任何权限；只有自定义指令的 `access` 生效，而且只允许
    sub / owner（`store/db.py` 里卡的）。默认拒的口径不因这个页面而变。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from app.store import db

logger = logging.getLogger(__name__)

#: 推送文案模板。**空串 = 用代码里的默认文案** —— 模板只负责「覆盖」，
#: 不负责「兜底」，所以清空输入框就等于恢复默认，不会有第三种状态。
TPL_KINDS = ("fund_1450", "digest")
TPL_LABEL = {
    "fund_1450": "14:50 关注基金估值推送",
    "digest": "盘后摘要（跑完推送 / 摘要指令）",
}
#: 免责声明**不进模板**：它是全站硬口径（README / docs/01 / 各页页脚 / 推送），
#: 由代码追加，页面上没有删除它的入口。
DISCLAIMER_1450 = "数据为盘中估算，仅供参考，不构成任何投资建议。"
DISCLAIMER_DIGEST = "数据来自公开接口，仅供研究参考，不构成投资建议。"


#: 内置指令。`text` 是主触发词，`alias` 是其余别名 —— 两者都可在页面上改，
#: 改完旧词就不再识别（想要两个都在，就把旧词填进 alias）。
#:
#: `quick` / `sort` 只是**默认值**：页面上把某条勾成「进快捷卡」即生效，
#: 不必为每条指令都动代码。
BUILTIN: List[Dict[str, Any]] = [
    {"key": "help", "label": "帮助", "text": "帮助",
     "alias": ["help", "指令", "?", "？"], "access": "any", "quick": 1, "sort": 10,
     "desc": "本说明"},
    {"key": "sub", "label": "订阅Pro", "text": "订阅Pro",
     "alias": ["订阅", "pro"], "access": "any", "quick": 0, "sort": 20,
     "desc": "开通 / 查看订阅"},
    {"key": "estimate", "label": "基金估值", "text": "基金",
     "alias": [], "access": "sub", "quick": 0, "sort": 30, "param": "6位基金代码",
     "desc": "一只基金的实时估值"},
    {"key": "fund_add", "label": "关注基金", "text": "关注基金",
     "alias": [], "access": "sub", "quick": 0, "sort": 40, "param": "6位基金代码",
     "desc": "加进我的关注（有数量上限）"},
    {"key": "fund_del", "label": "删除基金", "text": "删除基金",
     "alias": ["取消基金", "移除基金"], "access": "sub", "quick": 0, "sort": 50,
     "param": "6位基金代码", "desc": "取消关注"},
    {"key": "fund_list", "label": "我的基金", "text": "基金关注",
     "alias": ["我的基金"], "access": "sub", "quick": 1, "sort": 60,
     "desc": "我关注的基金与估值"},
    {"key": "activate", "label": "开通订阅", "text": "开通",
     "alias": [], "access": "owner", "quick": 0, "sort": 70,
     "param": "open_id [金额]", "desc": "管理员开通 / 续期"},
    {"key": "run", "label": "完整跑", "text": "跑",
     "alias": ["完整跑", "重跑", "执行", "执行任务"], "access": "owner", "quick": 1,
     "sort": 80, "desc": "完整盘后任务（约 12-15 分钟）"},
    {"key": "light", "label": "轻跑", "text": "轻跑",
     "alias": ["轻量", "快速抓取", "刷新"], "access": "owner", "quick": 1, "sort": 90,
     "desc": "只抓涨跌停池与板块资金流（几十秒）"},
    {"key": "status", "label": "状态", "text": "状态",
     "alias": ["status", "任务状态"], "access": "owner", "quick": 1, "sort": 100,
     "desc": "上次任务 / 下次定时 / 数据新鲜度"},
    {"key": "digest", "label": "摘要", "text": "摘要",
     "alias": ["复盘", "日报"], "access": "owner", "quick": 1, "sort": 110,
     "desc": "当天复盘"},
    {"key": "block", "label": "候选", "text": "候选",
     "alias": ["主力建仓", "建仓"], "access": "owner", "quick": 1, "sort": 120,
     "desc": "疑似主力建仓 Top10"},
    {"key": "sector", "label": "板块", "text": "板块",
     "alias": ["板块资金", "板块资金流"], "access": "owner", "quick": 1, "sort": 130,
     "desc": "板块净流入 / 净流出 Top5"},
    {"key": "watch_list", "label": "关注列表", "text": "关注列表",
     "alias": ["我的关注", "关注股"], "access": "owner", "quick": 1, "sort": 140,
     "desc": "我的关注涨跌与资金流"},
    {"key": "watch_add", "label": "关注股票", "text": "关注",
     "alias": ["加自选", "收藏"], "access": "owner", "quick": 0, "sort": 150,
     "param": "6位代码 [8位建仓日]", "desc": "加自选"},
    {"key": "watch_del", "label": "取关股票", "text": "取关",
     "alias": ["取消关注", "取消", "删除", "移除"], "access": "owner", "quick": 0,
     "sort": 160, "param": "6位代码", "desc": "取消自选"},
    {"key": "query", "label": "查询股票", "text": "查询",
     "alias": ["查", "看"], "access": "owner", "quick": 0, "sort": 170,
     "param": "6位代码 / 股票名", "desc": "是否在池、涨跌幅、打分、近5日主力净流入"},
]

_BY_KEY = {d["key"]: d for d in BUILTIN}


# ------------------------------------------------------------------ 读取


def _split_alias(v: Any) -> List[str]:
    return [x.strip() for x in re.split(r"[,，、]", str(v or "")) if x.strip()]


def words(key: str, default: List[str]) -> Optional[List[str]]:
    r"""这条指令现在认哪些触发词；**被停用返回 `None`**（调用方直接跳过该分支）。

    返回 `None` 而不是空列表是有意的：带参数的指令会把这些词拼进正则，
    空候选会拼出 `^(?:)\s*(\d{6})$` —— 那是「裸 6 位数字」，会把所有股票
    查询统统吞掉。
    """
    row = db.cmd_get(key)
    if row is not None and not row.get("enabled"):
        return None
    if row is None:
        return list(default)
    text = str(row.get("text") or "").strip()
    out: List[str] = []
    for w in [text or (default[0] if default else "")] + _split_alias(row.get("alias")):
        if w and w not in out:
            out.append(w)
    return out or list(default)


def access_of(key: str) -> Optional[str]:
    """自定义指令的 access；**内置指令与查不到的一律回 `None`**。

    回 `None` 会落到 `feishu.dispatch` 的「未登记 access，默认拒」分支 ——
    这正是默认拒要的效果。
    """
    row = db.cmd_get(key)
    if row is None or row.get("kind") != "custom":
        return None
    return str(row.get("access") or "owner")


def match_custom(text: str) -> str:
    """这条文本是不是某个**自定义**指令；是就回它的 key，否则空串。

    只做**精确匹配**（主触发词或别名），不支持带参数 —— 带参数的场景请把
    `target` 填成一条真实指令原文，触发词写短的（见 `run_custom`）。
    """
    t = (text or "").strip()
    if not t:
        return ""
    for row in db.cmd_list():
        if row.get("kind") != "custom" or not row.get("enabled"):
            continue
        if t == str(row.get("text") or "").strip() or t in _split_alias(row.get("alias")):
            return str(row.get("key") or "")
    return ""


def is_custom(key: str) -> bool:
    """这条是不是**自定义**指令（内置的恒 False）。"""
    row = db.cmd_get(key)
    return bool(row and row.get("kind") == "custom")


def custom_target(key: str) -> str:
    """自定义指令的 `target`：填了就说明「点了它等于发这条原文」。"""
    return str((db.cmd_get(key) or {}).get("target") or "").strip()


def run_custom(key: str) -> str:
    """自定义指令的固定文案。**没有 target 时的正文**（有 target 时
    `feishu.dispatch` 会拿 target 去再走一遍分发，不走这里）。"""
    row = db.cmd_get(key) or {}
    return str(row.get("reply") or "").strip() or f"已执行「{row.get('text') or key}」。"


def decorate(cmd: str, text: str) -> str:
    """在回复正文后面接上这条指令配的**附带文案**（没配就原样返回）。"""
    row = db.cmd_get(cmd) or {}
    extra = str(row.get("reply") or "").strip()
    if not extra or not text:
        return text
    return f"{text}\n\n{extra}"


# ------------------------------------------------------------------ 管理页


def _merged(default: Dict[str, Any], over: Optional[Dict[str, Any]]
            ) -> Dict[str, Any]:
    """代码默认 ∪ 库内改动。`edited` = 表里真有这一行（前端据此显示「已改」）。

    `alias` 一律用**逗号串**而不是列表：库里就是这么存的，页面上存回也按串
    传，往返不换形就不会出现「保存一次格式变一次」。
    """
    base = {
        "key": default.get("key") or (over or {}).get("key") or "",
        # `kind` 是**派生值**，不从库里读、也不从 BUILTIN 里读：传了默认定义
        # 就是 builtin，没传就是 custom。
        # 曾经写成 `default.get("kind") or ... or "custom"` —— BUILTIN 里根本
        # 没有 `kind` 这个键，于是内置行一律返回 custom；前端照着回传，
        # `save` 就把「改内置」判成「新建自定义」，key 撞内置 → 每次都 400，
        # 页面上表现为开关点不动（值被弹回原样）。
        "kind": "builtin" if default else "custom",
        "label": default.get("label") or "",
        "text": default.get("text") or "",
        "alias": ",".join(default.get("alias") or []),
        "access": default.get("access") or "owner",
        "reply": "",
        "target": "",
        "param": default.get("param") or "",
        "desc": default.get("desc") or "",
        "enabled": 1,
        "quick": int(default.get("quick") or 0),
        "sort": int(default.get("sort") or 0),
        "default_text": default.get("text") or "",
        "default_alias": ",".join(default.get("alias") or []),
    }
    if not over:
        return {**base, "edited": False}
    base.update({
        # `kind` 故意不在这儿：库里存的那一列不算数（见上）
        "label": str(over.get("label") or base["label"]),
        "text": str(over.get("text") or base["text"]),
        "alias": str(over.get("alias") or base["alias"]),
        "access": str(over.get("access") or base["access"]),
        "reply": str(over.get("reply") or ""),
        "target": str(over.get("target") or ""),
        "enabled": int(over.get("enabled") if over.get("enabled") is not None else 1),
        "quick": int(over.get("quick") or 0),
        "sort": int(over.get("sort") or base["sort"]),
    })
    return {**base, "edited": True}


def rows() -> List[Dict[str, Any]]:
    """给「指令管理」页：内置定义 ∪ 库内改动 ∪ 自定义。"""
    overrides = {str(r.get("key")): r for r in db.cmd_list()}
    out: List[Dict[str, Any]] = []
    seen = set()
    for d in BUILTIN:
        seen.add(d["key"])
        out.append(_merged(d, overrides.get(d["key"])))
    for key, over in overrides.items():
        if key in seen or over.get("kind") != "custom":
            continue
        out.append(_merged({}, over))
    out.sort(key=lambda r: (int(r.get("sort") or 0), str(r.get("key"))))
    return out


def save(item: Dict[str, Any]) -> Dict[str, Any]:
    """新增 / 修改一条，顺手把「自定义触发词撞了内置指令」这类坑当场拦掉。"""
    key = str(item.get("key") or "").strip()
    kind = str(item.get("kind") or "custom")
    row = db.cmd_get(key)
    if kind == "custom":
        # **key 也不许撞内置**：否则建一条 key=status 的自定义指令就能把
        # 内置「状态」整个顶掉（`is_custom` 一真，它的 access 就换人了）。
        if key in _BY_KEY:
            raise ValueError(f"{key} 是内置指令的 key，改它请走内置那条")
        if row is not None and row.get("kind") == "builtin":
            raise ValueError(f"{key} 已经是内置指令，改它请走内置那条")
        cand = [str(item.get("text") or "").strip()] + _split_alias(item.get("alias"))
        for w in filter(None, cand):
            if _clashes(w):
                raise ValueError(f"「{w}」已经被内置指令占用了，换个词")
    else:
        if key not in _BY_KEY:
            raise ValueError(f"{key} 不是内置指令")
    return db.cmd_put(item)


def _clashes(word: str) -> Optional[str]:
    """这个词是不是哪条内置指令正在用（页面上把内置触发词改掉之后就不算撞）。"""
    for d in BUILTIN:
        taken = words(d["key"], [d["text"]] + list(d.get("alias") or [])) or []
        if word in taken:
            return d["key"]
    return None


def remove(key: str) -> bool:
    """删自定义 = 彻底消失；删内置 = 恢复代码默认（表里不再有这一行）。"""
    return db.cmd_remove(key)


# ------------------------------------------------------------------ 推送文案


def _tpl_key(kind: str) -> str:
    return f"cmd_tpl_{kind}"


def tpl_default(kind: str) -> str:
    """代码默认文案。**按调用时的配置现算**，不是写死的字符串 ——
    14:50 抬头里的时刻跟着 `settings.push_hour/push_minute` 走。
    """
    from app.config import settings
    if kind == "fund_1450":
        return (f"A股看板 · 关注基金估值 "
                f"{settings.push_hour:02d}:{settings.push_minute:02d}")
    if kind == "digest":
        return ""           # 摘要的抬头带日期，由 notify 现拼
    return ""


def tpl_set(kind: str, text: str) -> None:
    if kind not in TPL_KINDS:
        raise ValueError(f"未知的推送文案类型：{kind}")
    val = (text or "").strip()
    if val:
        db.kv_set(_tpl_key(kind), val[:200])
    else:
        db.kv_set(_tpl_key(kind), "")


def tpl_text(kind: str) -> str:
    """页面上显示的值：改过就显示改过的，否则显示默认。"""
    if kind not in TPL_KINDS:
        return ""
    return db.kv_get(_tpl_key(kind)) or tpl_default(kind)


def tpl_effective(kind: str) -> str:
    """真正发出去的抬头文案。**免责声明不在这里**，由调用方固定追加。"""
    return tpl_text(kind) if kind in TPL_KINDS else ""
