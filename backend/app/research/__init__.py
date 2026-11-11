# -*- coding: utf-8 -*-
"""历史回测与策略研究模块（docs/backtest/）。

与实时盘后任务**解耦**：只读库、只写自己的 `research_*` 表，不碰
`scheduler` / `mainforce` 的流水线，不改 `scorer.yaml` 的任何权重。

铁律（见 docs/backtest/implementation-audit.md）：
  * 缺什么报什么，**绝不拿当前值冒充历史值**；
  * 日线不落库 => 含收益的完整六维回测 BLOCKED，框架先建、结果先空；
  * 零联网 —— 本包任何模块都不许发网络请求。
"""
