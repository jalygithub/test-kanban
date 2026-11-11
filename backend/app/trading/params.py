# -*- coding: utf-8 -*-
"""规则策略 V1 的参数定义与版本。

参数与版本分离：**每次运行落实际使用的参数快照**（`strategy_run.params_json`），
版本 = 归一化 JSON 的 sha256 前 12 位（内容哈希 → 同参数必同版本，改一个
小数位就是一个新版本）。历史结果复现时，「当时按什么参数跑的」不靠记忆靠哈希。

默认值是**教科书口径的起点，不是被验证过的最优参数** —— 本阶段禁止自动搜参，
任何「这组参数更好」的结论都得由人拿回测证据另行下结论。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Optional

#: 规则版本。策略逻辑本身变更时 bump —— 与参数版本正交（逻辑变了参数没变
#: 也是另一个版本）。
RULE_VERSION = "rule-v1"

DEFAULT_PARAMS: Dict[str, Any] = {
    # ---- 买入候选 ----
    "candidate_top_n": 5,        # 取六维榜前 N 名作为买入候选
    "trend_ma": 20,             # 趋势过滤：收盘 >= MA(N)，窗口不足不交易
    "min_amount": 5e7,          # 流动性下限：近 5 日日均成交额（元）
    "min_score": 0.0,           # 六维综合分下限
    # ---- 仓位约束 ----
    "max_position_pct": 0.20,   # 单股仓位上限（占权益）
    "max_positions": 5,         # 最大持仓数量
    "max_total_exposure": 0.80,  # 总仓位上限（多头市值 / 权益）
    "initial_cash": 1_000_000.0,  # 模拟账户初始资金（仅首次开户生效）
    "fee_reserve": 0.005,       # 买入预算预留的费用+滑点缓冲（占成交额）
    "allow_pyramid": False,     # 是否允许加仓（默认禁止：一票一单）
    # ---- 卖出规则（逐条独立配置、独立记录触发原因）----
    "stop_loss_pct": 0.08,      # 固定止损：收盘跌破入场成本 -N%
    "trail_pct": 0.10,          # 移动止盈：自峰值回撤 N% 离场
    "exit_trend_ma": 10,        # 趋势破坏：收盘 < MA(N)
    "max_hold_days": 10,        # 最大持仓交易日（含信号日、不含入场日）
    "risk_force_exit": False,   # 数据风险状态下是否强平持仓（默认只禁新开仓）
    # ---- 成本（**研究假设费率**，非券商真值；与 engine.DEFAULT_COST 同源）----
    "slippage": 0.001,
    "commission_rate": 0.00025,
    "min_commission": 5.0,
    "stamp_rate": 0.0005,
}

# 校验表：key -> (lo, hi, 必须是整数)。范围给得宽，拦的是把 0.8 写成 80
# 这类会静默放大一个数量级的笔误。
_RANGES: Dict[str, tuple] = {
    "candidate_top_n": (1, 30, True),
    "trend_ma": (2, 250, True),
    "min_amount": (0.0, 1e11, False),
    "min_score": (0.0, 100.0, False),
    "max_position_pct": (0.01, 1.0, False),
    "max_positions": (1, 50, True),
    "max_total_exposure": (0.01, 1.0, False),
    "initial_cash": (10_000.0, 1e10, False),
    "fee_reserve": (0.0, 0.05, False),
    "stop_loss_pct": (0.005, 0.5, False),
    "trail_pct": (0.005, 0.5, False),
    "exit_trend_ma": (2, 250, True),
    "max_hold_days": (1, 250, True),
    "slippage": (0.0, 0.05, False),
    "commission_rate": (0.0, 0.01, False),
    "min_commission": (0.0, 100.0, False),
    "stamp_rate": (0.0, 0.01, False),
}
_BOOLS = ("allow_pyramid", "risk_force_exit")


def normalize(params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """合并默认值、拒绝未知键、校验取值范围。

    未知键直接抛错而不是忽略 —— 「拼错的参数被静默丢掉、跑出来还是默认值」
    正是任务书「拼错的参数不许静默忽略」要拦的（与 pipeline 400 口径一致）。
    """
    given = dict(params or {})
    unknown = set(given) - set(DEFAULT_PARAMS)
    if unknown:
        raise ValueError("未知参数: %s" % ", ".join(sorted(unknown)))
    out = dict(DEFAULT_PARAMS)
    out.update(given)
    for key in _BOOLS:
        v = out[key]
        if not isinstance(v, bool):
            raise ValueError("参数 %s 必须是布尔值，收到 %r" % (key, v))
    for key, (lo, hi, integer) in _RANGES.items():
        v = out[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("参数 %s 必须是数值，收到 %r" % (key, v))
        if integer:
            if int(v) != v:
                raise ValueError("参数 %s 必须是整数，收到 %r" % (key, v))
            out[key] = int(v)
        else:
            out[key] = float(v)
        if not (lo <= out[key] <= hi):
            raise ValueError(
                "参数 %s 越界 [%s, %s]，收到 %r" % (key, lo, hi, out[key]))
    if out["max_position_pct"] > out["max_total_exposure"]:
        raise ValueError("单股仓位上限不能大于总仓位上限")
    return out


def params_version(params: Optional[Dict[str, Any]] = None) -> str:
    """参数版本 = 归一化后 JSON 的 sha256 前 12 位。"""
    canon = json.dumps(normalize(params), sort_keys=True,
                       ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]


def params_json(params: Optional[Dict[str, Any]] = None) -> str:
    """归一化参数的稳定 JSON（落库用；与 params_version 同一形态）。"""
    return json.dumps(normalize(params), sort_keys=True, ensure_ascii=False)
