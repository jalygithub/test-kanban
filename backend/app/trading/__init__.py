# -*- coding: utf-8 -*-
"""自动买卖策略 V1（docs/ai_trading_lab/）。

分层：params（参数/版本）→ strategy（意图）→ risk（审核）→ paper（撮合
/记账）→ service（编排）。任何一层都不越过边界改账户 —— 现金与持仓只由
`paper` 的成交流程经 `db.sim_settle` 原子改写。
"""
