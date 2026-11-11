# 策略实验室 —— 使用说明

> 本模块**只做历史研究与模拟**，不接真实交易、不发任何下单指令、不承诺盈利。
> 结论边界见 [`limitations.md`](./limitations.md)；数据底子见 [`implementation-audit.md`](./implementation-audit.md)。

## 一句话定位

把**已经发生过的**主力打分快照拿回来，按当时可见的分值重建「前 N 名」这个规则，
再看这些股票之后 1/3/5/10/20 个交易日的表现。

**信号不重算**——直接用入库时那份快照。原因写在 `implementation-audit.md` §3：
重算要读日线（本库没有），且前复权 `qfq` 会用未来的除权事件改写历史价格，
算出来的每个分都是被未来信息污染过的。

---

## 一、先看结论：这次安装能做什么

| 想要的 | 现在 |
|---|---|
| 「当时按六维分排前 5，之后 5 天怎么样」 | ✅ 可以，逐条可追溯 |
| 换权重 / 删一个因子后排名与收益怎么变 | ✅ 可以（因子消融） |
| 当前股票池**回看**历史（模式 A） | ✅ 可以，**但带幸存者偏差** |
| 当时真实存在的完整股票池回看（模式 B） | ❌ **显式拒绝**，理由见下 |
| 完整六维含收益回测 | ❌ **BLOCKED**（5/6 因子依赖的日线不落库） |
| 理论收益（不扣费） | ✅ 每条都给 |
| 可执行收益（扣费） | ⚠️ **默认留空**，配了成本模型才有 |
| 最大回撤 | ❌ **留空**，没建组合净值曲线 |

点 `GET /api/research/audit` 看当前库的实时结论；`GET /api/research/capabilities`
看上面这张表的机器可读版本（前端表单直接读它，不在前端写死）。

**模式 B 为什么拒绝**：本库只有今天的股票列表，没有「当时真实存在的完整名单」，
拿它回看等于把已被系统自动过滤掉的股票和退市/ST/长停股票排除在外，
得到的收益必然**系统性偏高**，而且这种偏差无法用统计手段事后修正。
按模式 A 跑完再标成模式 B，等于把一个已知偏差伪装成结论。

---

## 二、一次完整流程

### 1. 建实验（配置到此冻结）

```http
POST /api/research/experiments
{
  "name": "六维前5 · 默认权重",
  "strategy_version": "sixdim-v1",
  "universe_mode": "current",
  "start_date": "20261006",
  "end_date": "20261016",
  "config": {
    "weights":     {"inflow_consistency": 30, "volume_up_price_flat": 20,
                    "large_order_ratio": 15, "price_position_low": 15,
                    "turnover_moderate": 10, "chip_convergence": 10},
    "horizons":    [1, 3, 5, 10, 20],
    "portfolios":  ["top5", "top10", "top30"],
    "n_top":       10,
    "min_score":   0
  }
}
```

配置会**原样冻结进库**并算出 `config_hash`。之后这个实验不管在多少个运行里
跑过多少次，用的都是这份配置——不会因为有人改了代码默认值就悄悄变口径。

`config` 里所有键都是可选的，缺省取：

| 键 | 默认 | 说明 |
|---|---|---|
| `weights` | **不填** = 用快照自带的权重 | 填了就以填的为准；**键必须写全**，少一个 = 那个因子被消融 |
| `horizons` | `[1,3,5,10,20]` | 只能取这五个值 |
| `portfolios` | `["top5","top10","top30"]` | 同上 |
| `n_top` | `10` | 研究层排名取前多少名 |
| `min_score` | `0` | 研究层最低分 |
| `cost` | **不配** | 见下「交易成本」 |
| `ablation` | 无 | 见「因子消融」 |

**不填 `weights` 是推荐做法**：那时引擎按快照入库时自己的 `used_weights`
重放，等于「复现当初那个口径」，不受后来改口径的影响。
六个维度键名固定，与 `backend/config/scorer.yaml` 一致：

| 维度键 | 名称 | 默认权重 |
|---|---|---|
| `inflow_consistency` | 主力资金持续性 | 30 |
| `volume_up_price_flat` | 量增价稳 | 20 |
| `large_order_ratio` | 大单净额占比 | 15 |
| `price_position_low` | 股价位置低 | 15 |
| `turnover_moderate` | 换手适中 | 10 |
| `chip_convergence` | 筹码收敛 | 10 |

`horizons` / `portfolios` **只接受那五个/三个值**。多给一个 = 同一次运行里
塞进更多组合规则 = 从每条样本上多切几刀，收益统计的多重比较问题更重。

### 2. 跑一轮

```http
POST /api/research/experiments/1/run      →  {"run": {"id": 7, "status": "running", ...}}
GET  /api/research/runs/7                 →  轮询进度（stage / progress / warnings）
POST /api/research/runs/7/cancel          →  协作式取消
```

- 运行在**后台线程**，不阻塞请求；进度写在库里，**进程重启也不丢**。
- 重复点启动会返回 **409**（同一把锁里拒绝已有 pending/running 的实验），
  不会静默开出第二条并行任务。
- 取消是**协作式**的：引擎在每个阶段边界和长循环里查状态。已写入的信号保留，
  **指标不写**——半成品不冒充完整结果。
- 跑完看 `warnings`。幸存者偏差、因子原始值缺失、无基准、缺成本模型……
  每一条运行该说的话都在里面。

### 3. 读结果

```http
GET /api/research/experiments/1/signals?limit=200&offset=0     # 信号（分页）
GET /api/research/experiments/1/outcomes?horizon=5&limit=1000  # 逐条结果（按观察期过滤）
GET /api/research/experiments/1/metrics                        # 汇总指标（组合 × 观察期）
GET /api/research/experiments/1/runs                           # 运行历史
GET /api/research/experiments/1                                # 详情 + 冻结配置
```

结果行数 = 信号数 × 观察期数，**必须**按 `horizon` 过滤或用 `limit` 截断。
所有列表端点都会如实回报 `has_more` / `truncated`，不会静默丢数据。

### 4. 删除

```http
DELETE /api/research/experiments/1   →  {"deleted": {"experiment":1,"runs":2,"signals":37,...}}
```

- **级联**：一次事务连同该实验的运行、信号、逐条结果、汇总指标一起删，
  逐表回报删了几行；别的实验的数据一行不动。
- 实验不存在回 **404**；还有未完成的运行、或取消后引擎线程还没退出回
  **409**（先取消、稍候再删）—— 不会删一半被写回孤儿数据。
- 页面上是实验列表「删除」按钮 + 二次确认框，确认后才发请求。

---

## 三、口径说明

### 观察周期

| 记号 | 含义 |
|---|---|
| `D` | 信号日（快照所属交易日） |
| 入场 | `D+1` **开盘价** |
| `T+n` | `D` 之后第 `n` 个交易日**收盘价**（`n ∈ {1,3,5,10,20}`） |

交易日序列从日线表 `DISTINCT trade_date` 推导，**不依赖联网日历**。

### 逐条结果的三种状态

| `status` | 含义 | 进收益统计？ |
|---|---|---|
| `OK` | 观察期走完，有可比数据 | ✅ |
| `PENDING` | 观察期还没走完（比如今天就是第 2 天，要算 5 日） | ❌ |
| `NO_DATA` | 缺行情，判不了 | ❌ |

`PENDING` 和 `NO_DATA` **分开记账**：「还没到时候」和「判不了」是两回事，
混在一起会让样本量看着比实际多。

### 成交判定（模拟，不是真实成交）

| 场景 | `fill_status` | `exec_return` |
|---|---|---|
| 次日正常 | `FILLED` | 配了成本才有 |
| 次日一字涨停，买不进 | `UNFILLABLE` | 无 |
| 前收缺失，判不了是否涨停 | `UNCONFIRMED` | 无 |
| 观察期未到 | `NOT_DUE` | 无 |

出场同理：`SELLABLE` / `LIMIT_DOWN`（跌停卖不出）/ `UNCONFIRMED` / `NOT_DUE`。
**两端都确认才给可执行收益**。

涨跌幅按交易所规则（主板 10% / 创业板·科创板 20% / 北交所 30%）——这是
规则事实。历史 ST 状态本库拿不到，所以 `*ST` 股可能被误判可成交，
这条风险写在每条结果的 `quality_notes` 里。

### 收益两种口径

```
theory_return = (exit − entry) / entry                ← 纯价差，不扣费
excess_return = theory_return − bench_return          ← 有基准时才给
exec_return   = theory_return − 佣金 − 卖出印花税 − 滑点   ← 需要成本模型
```

三者**并排显示**，不合并成一个数字。`max_drawdown` 留空——没有构建逐日组合
净值曲线，拿个股收益排开算回撤是假的。

### 复权口径（每条结果都标着）

**所有收益字段一律按折算后的可比价算**，`entry_price` / `exit_price` 记的仍是
**真实成交价（不复权）**——价格回答「当时多少钱成交的」，收益回答「实际赚了多少」，
两者口径不同不能混。折算系数单独存 `entry_factor` / `exit_factor`，想复核乘回去即可。

```
theory_return = (exit × exit_factor) / (entry × entry_factor) − 1
```

`adjust_method` 三档，含义写死在 `engine.py`，前后端与文档同一份：

| 取值 | 判定 | 说明 |
|---|---|---|
| `RAW` | 两端 `adj_factor` 都有且不同 | 已折算 |
| `UNADJUSTED` | 两端因子相同 | **确认这段没除权**，不复权价直算本就对 |
| `UNADJUSTED_UNCERTAIN` | 任一端缺 `adj_factor` | ⚠️ 判不了，收益可能含除权跳空 |

- `adjust_note` 在 `UNCERTAIN` 档写明「若期间发生过除权，该收益不反映真实回报」。
- 运行 `warnings` 里会报「N/M 条判不了期间是否发生过除权」，`_finish` 的
  `adjust` 字段给出三档计数。
- **不并进 `data_quality`**：那条维度回答「行情齐不齐」，这条回答「这个数可不可信」。
- 窗口内的 MFE/MAE **逐根用自己的因子**，不整窗一刀切——除权落在持仓中途时，
  一刀切会把换算后的高低点算错。

> 现在库里的日线还没有 `adj_factor`（回填未授权执行），所以新跑的实验
> **基本都是 `UNADJUSTED_UNCERTAIN`**。数值仍算得出来，但**别把它当真实回报**。

### 交易成本

**默认不配。** 没有业务方确认的费率，就不该凭空扣一笔。不配时
`exec_return` 一律为 NULL，理论收益照常给。

想看扣费口径，在建实验时显式传一个 `cost`：

```json
"cost": {"commission_rate": 0.00025, "min_commission": 5.0,
         "stamp_rate": 0.0005, "slippage": 0.001,
         "notional": 100000.0, "assumption": true}
```

佣金双向且带 5 元下限、卖出收印花税、按模拟成交额逐项算。
**这组值标了 `assumption: true`，是研究假设，不等于任何人的实际券商费率。**
配了之后运行 `warnings` 里一定会出现「研究假设」字样。

> ⚠️ `unfilled_rate` 只看**成交判定**，不看有没有成本模型。
> 没配成本时它仍是真实的「买不进比例」，不会被抬成 1。

### 因子消融

建实验时传 `ablation`：

```json
"ablation": {"drop": ["large_order_ratio"]}
```

被删因子的权重**归零**（不是删键——删掉的话它会从 `used_weights` 的回退路径
里重新计入）。也可以整体换权重：

```json
"ablation": {"weights": {"inflow_consistency": 45, "volume_up_price_flat": 20,
                          "large_order_ratio": 0,  "price_position_low": 15,
                          "turnover_moderate": 10, "chip_convergence": 10}}
```

消融实验的原始入库分**照常入档**，实验权重和原始权重**双双进 provenance**。

**结论边界**：消融只回答「换这套权重，排名和收益怎么变」。
它**不宣称因子的边际贡献**——真要拆到单因子边际贡献，需要逐因子独立实验设计，
本库数据不够（因子原始值不可得，见 limitations §3）。

---

## 四、可复现性

每条信号都带：

| 字段 | 作用 |
|---|---|
| `config_hash` | 冻结配置的哈希，配置一改就是另一个实验 |
| `weights_snapshot` | 入库时用的权重原样 |
| `dims` / `replay_score` | 逐维分值 + 引擎重放的总分 |
| `snapshot_rank` / `replay_rank` | 快照自带排名 vs 引擎重排 |
| `sort_version` | 排序规则版本（`score-desc-code-asc-v1`） |
| `strategy_version` | 模型版本，由建实验时填 |
| `snapshot_model` / `snapshot_params` | 快照入库时的模型与参数 |

**并列裁决**：引擎在 score 降序之后按 **code 升序**固定裁决。快照本身没有并列
裁决（纯 `score` 降序，Python sort 稳定但依赖上游顺序）——
所以同一天可能有第 N 名并列时 `replay_rank` 与 `snapshot_rank` 不同，
这是**已知且有意**的差异，不是 bug。分值对拍容差 0.06。

---

## 五、前端

「策略实验室」页四个 Tab：

1. **数据审计** —— 五域可回测性结论，先看这个再建实验
2. **实验与运行** —— 建实验 / 跑 / 取消 / 运行历史 / 进度轮询
3. **信号与结果** —— 分页看信号与逐条结果
4. **因子消融** —— 配消融、对比不同权重下的结果

逐步操作指南（表单每个字段怎么填、每列怎么读、常见情况速查）见
[`lab-guide.md`](./lab-guide.md)。

## 六、免责声明

本模块只做行情呈现与历史研究，**不给买卖信号**，不构成任何投资建议。
回测是对**过去**的描述，不是对未来的承诺。历史收益不预示未来表现。
