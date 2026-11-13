/** 后端 API 客户端。统一处理错误与超时，页面不直接碰 fetch。 */

const BASE = ''

export class ApiError extends Error {
  status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

/**
 * 统一的错误文案提取。**与 `ApiError` 放一起** —— 各页面原来各写一份
 * （Pay/Subs/Invite/Cmds 四处逐字相同），而其余页面又各自 `(e as Error).message`，
 * 同一个 404 在两个页面显示得不一样。
 */
export const errMsg = (e: unknown) => (e instanceof ApiError ? e.message : String(e))

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: { 'Content-Type': 'application/json', ...(init?.headers ?? {}) },
    })
  } catch (err) {
    throw new ApiError(0, `无法连接后端（请确认 uvicorn 已启动）：${(err as Error).message}`)
  }

  const text = await res.text()
  let data: unknown = null
  try {
    data = text ? JSON.parse(text) : null
  } catch {
    data = text
  }

  if (!res.ok) {
    const detail =
      typeof data === 'object' && data !== null && 'detail' in data
        ? String((data as { detail: unknown }).detail)
        : text.slice(0, 200)
    throw new ApiError(res.status, detail || res.statusText)
  }
  return data as T
}

// ---------------------------------------------------------------- 类型

export interface LimitRow {
  rank?: number
  code: string
  name: string
  pct_chg: number
  price: number
  amount?: number
  float_mv?: number
  total_mv?: number
  turnover?: number
  seal_amount?: number
  first_seal_time?: string
  last_seal_time?: string
  break_count?: number
  zt_stat?: string
  boards?: number
  industry?: string
  continuous_dt?: number
  open_count?: number
  pe_ttm?: number
  is_high?: string
  vol_ratio?: number
  reason?: string
  /**
   * 断板回看（只出现在涨停池尾部，见 `metrics/board_streak`）：
   * 连板断了就不在当日池子里，靠历史快照补回来。`boards` 冻结在断板前的值，
   * 行情字段（现价/涨幅/封单…）后端**一律不带** —— 那是断板当天的旧数据，
   * 拿出来只会被当成当天行情看。
   */
  broken?: boolean
  miss_days?: number
  last_date?: string
}

export interface SectorRow {
  rank: number
  name: string
  index: number
  pct_chg: number
  inflow: number
  outflow: number
  net_inflow: number
  stock_count: number
  leading_stock: string
  leading_pct_chg: number
  leading_price?: number
}

export interface LimitResponse {
  date: string
  type: string
  rows: LimitRow[]
}

export interface SectorResponse {
  kind: string
  rows: SectorRow[]
}

export type PoolType = 'zt' | 'zb' | 'dt' | 'strong'

/** /api/stock/{code}/daily 的一根日线（与关注页 bars 同口径，单位同后端原样） */
export interface StockDailyBar {
  date: string
  open: number | null
  close: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  turnover: number | null
  pct_chg: number | null
}

/** 个股日线（详情页「走势」）：后端缓存 30 分钟，源挂了返回 rows=[] + error */
export interface StockDailyResponse {
  code: string
  /** cn=沪深北（6 位）/ hk=港股（5 位）。港股的池内状态、打分、资金流都没有数据 */
  market?: 'cn' | 'hk'
  /** 港股时由后端补齐（关注记录 → 腾讯行情），A 股由池子/打分给 */
  name?: string
  days: number
  rows: StockDailyBar[]
  cached: boolean
  age_sec: number
  error?: string
}

/** 打分模型的单个维度（score 为候选池内百分位 0-100） */
export interface MainForceDim {
  label: string
  short: string
  score: number
  weight?: number
}

export interface MainForceRow {
  rank: number
  code: string
  name: string
  industry?: string
  price: number | null
  pct_chg: number | null
  turnover?: number | null
  float_mv?: number | null
  amount?: number | null
  list_date?: string
  /** 综合分 0-100 */
  score: number
  /** 该行有多少权重维度拿到了数据（1.0 = 六维齐全） */
  coverage: number
  dims: Record<string, MainForceDim>
  reasons: string[]
}

/** 矩阵格子：某只股票在某天上榜的名次与综合分 */
export interface MainForceMatrixCell {
  rank: number
  score: number | null
}

/** 矩阵的一行 = 一只股票跨多天的上榜轨迹 */
export interface MainForceMatrixRow {
  code: string
  name: string
  industry: string
  /** 上榜天数 */
  hits: number
  /** 历史最好名次 */
  best_rank: number
  /** 最近一次上榜日 */
  last_date: string
  best_score: number | null
  /** 日期 -> 当天格子，只含上榜那天；没上榜的日子不进这个对象 */
  cells: Record<string, MainForceMatrixCell>
  /**
   * 跑过的日期 -> 当日收盘价（`daily_bar` 不复权，后端单一出处）。
   * **没上榜的日子也给价**；本地日线缺的日期不进这个对象，格子显 `-`。
   * 漏跑列没有打分也不给价，走「未跑」底色。
   */
  prices: Record<string, number>
}

/**
 * GET /api/main-force/matrix：行=股票、列=交易日的上榜矩阵。
 * `dates` 是区间内**全部**交易日，漏跑的走 `missed_dates` 单独标出来 ——
 * 「这天没跑任务」和「跑了但没上榜」在格子上长得一样，前端必须分开画。
 */
export interface MainForceMatrix {
  dates: string[]
  ran_dates: string[]
  missed_dates: string[]
  /**
   * 漏跑日里补不上的那些：涨/跌停池已过东财 30 天窗口且本地无当日快照
   * —— 池子事后取不回，点了也是 400。由后端算（前端看不出「本地有快照」
   * 这个例外），按钮对这些列不再显示。
   */
  unfillable_dates: string[]
  rows: MainForceMatrixRow[]
  total: number
  /** 库内最新打分日 */
  as_of: string
  days: number
  disclaimer: string
}

/**
 * 前5跟踪的一行（2026-11-11 拍板）：矩阵行 + 跟踪三日期与状态。
 * 与矩阵行同名字段的口径差异：`hits` = **进前5次数**（矩阵是全部上榜天数）、
 * `last_date` = **最后一次进前5日**（矩阵是任意名次的最近上榜日）。
 */
export interface MainForceTrackRow extends MainForceMatrixRow {
  /** 首次进前5日 —— 跟踪从这天算起 */
  first_date: string
  /** 跟踪截止日 = last_date + 30 自然日，重复上榜自动顺延 */
  deadline: string
  /** '跟踪中' | '已到期'（到期留在名单、不再拉股价） */
  status: string
}

/**
 * GET /api/main-force/top5-track：前5跟踪名单。
 * `dates/ran_dates/missed_dates + rows[].cells/prices` 与矩阵同形状 ——
 * 走势弹框直接复用矩阵那套画法（蓝 = 不复权收盘价断线、橙 = 名次右轴反转）。
 */
export interface MainForceTop5Track {
  dates: string[]
  ran_dates: string[]
  missed_dates: string[]
  rows: MainForceTrackRow[]
  total: number
  as_of: string
  tracking: number
  expired: number
  disclaimer: string
}

export interface MainForceStats {
  universe: number
  coarse_kept: number
  coarse_dropped: Record<string, number>
  preselected: number
  histories: number
  negative_dropped: Record<string, number>
  scored: number
  output: number
}

export interface MainForceParams {
  weights: Record<string, number>
  top_n: number
  preselect_top: number
  min_listed_days: number
  float_mv_min: number
  float_mv_max: number
  max_boards: number
  max_gain_20d: number
  turnover_range: [number, number]
  inflow_window_days: number
  price_position_days: number
  chip_threshold: number
}

export interface MainForceModel {
  degraded: string[]
  used_weights: Record<string, number>
  total_weight: number
  coverage_mean: number
  /**
   * 当天输入域的采集体检（阶段 7 第 8 条，宽松方案）：`state === 'incomplete'`
   * 时整榜标「本榜基于不完整数据」，不拦输出。**老快照没有这个字段** ——
   * 当「没查过」处理：不告警，也不假装查过没问题。
   */
  data_quality?: {
    state?: string
    missing?: {
      domain: string
      trade_date: string
      status: string | null
      coverage_rate: number | null
    }[]
    checked_at?: string
  }
}

export interface MainForceResponse {
  date: string
  generated_at: string
  elapsed_sec: number
  params: MainForceParams
  stats: MainForceStats
  model: MainForceModel
  rows: MainForceRow[]
  all?: MainForceRow[]
  disclaimer: string
}

/** 打分内部进度：7 个阶段走到第几个 + 当前阶段的逐项计数（逐只日线那步）。 */
export interface MainForceProgress {
  /** 当前阶段名，如「预选与日线」 */
  stage: string
  /** 第几个阶段（1 起） */
  stage_no: number
  /** 阶段总数（run() 里 _stage 的调用个数） */
  stages: number
  /** 当前阶段内已完成数 */
  done: number
  /** 当前阶段内总数；该阶段不逐项计数时为 0 */
  total: number
}

export interface MainForceState {
  running: boolean
  error: string
  started_at: string
  finished_at: string
  date: string
  output: number
  elapsed_sec: number | null
  degraded: string[]
  /** 只在 running 时有值 —— 后端不给闲时的残留进度（见 main_force_status） */
  progress?: MainForceProgress | null
}

/** 单个交易日的主力净流入明细（金额单位：元） */
export interface InflowDay {
  date: string
  net_inflow: number
  large_order_net?: number | null
}

export interface InflowItem {
  /** 窗口内有数据的交易日，升序；冷启动可能只有 1 天 */
  days: InflowDay[]
  /** 上述天数的合计（元） */
  total: number
}

/** /api/main-force/inflow：近 N 日逐日主力净流入（列表列与点击弹框共用） */
export interface MainForceInflow {
  date: string
  days: number
  /** 本次覆盖到的交易日（升序） */
  window: string[]
  items: Record<string, InflowItem>
  /** 还有几只没补齐；>0 时若 backfilling=true 则后端在回补，前端继续轮询 */
  missing: number
  /** 后端回补线程此刻是否真在跑；`STOCK_INFLOW_BACKFILL=false` 或空闲时恒为 false，
   *  missing>0 也别再轮询（内网下补不上，只会空转触发外网请求） */
  backfilling: boolean
}

/** 关注时存下的候选快照，用于回看「当时为什么关注它」 */
export interface WatchSnapshot {
  reasons?: string[]
  dims?: Record<string, MainForceDim>
}

export interface WatchItem {
  code: string
  /** cn=沪深北（代码 6 位）/ hk=港股（代码 5 位） */
  market?: string
  name: string
  industry?: string
  /** 疑似主力建仓日（即当时的打分日），走势从这天起算 */
  source_date: string
  rank?: number | null
  score?: number | null
  price?: number | null
  added_at: string
  snapshot?: WatchSnapshot
  /** 仅 perf=true 时返回；单只取数失败或超预算时为 null */
  perf?: WatchPerf | null
}

export interface WatchPerf {
  source_date: string
  start_date: string
  start_close: number
  last_date: string
  last_close: number
  pct: number
  high: number
  high_date: string
  low: number
  low_date: string
  max_drawdown: number
  trading_days: number
  calendar_days: number
  avg_turnover: number | null
  avg_amount: number | null
  index_pct: number | null
  alpha: number | null
}

export interface WatchBar {
  date: string
  open: number | null
  close: number | null
  high: number | null
  low: number | null
  volume: number | null
  amount: number | null
  turnover: number | null
  pct_chg: number
  /** true = 这根在建仓日之前，只是走势上下文，不计入区间统计 */
  pre?: boolean
}

export interface WatchRelist {
  date: string
  rank?: number
  score?: number
}

export interface WatchDetail extends WatchItem {
  bars: WatchBar[]
  stats: WatchPerf
  index: { date: string; close: number }[]
  relist: WatchRelist[]
}

export type Market = 'cn' | 'hk'

/**
 * 「实时行情」的一只报价。
 *
 * **除 code/market/name/price/prev_close 外全部可空** —— 后端逐字段做不变量校验
 * （涨跌额 ≈ 现价−昨收、最高 ≥ 现价 ≥ 最低、成交额 ≈ 成交量×价格…），
 * 对不上就置 null。前端必须原样显示「—」，**不许填默认值**：填出来的 0 会
 * 让「校验没过」和「真的没成交」变成同一件事。
 */
export interface LiveQuote {
  code: string
  market: Market
  name: string
  /** 现价（元 / 港元）。整行没有有效现价时后端会丢掉整条，故恒有值 */
  price: number
  prev_close: number
  open: number | null
  high: number | null
  low: number | null
  change: number | null
  /** 涨跌幅（%） */
  pct: number | null
  /** 成交量（A 股单位=手）。**港股恒为 null**：每手股数不固定，证不了单位就不显示 */
  volume: number | null
  /** 成交额（元）。港股同上恒为 null */
  amount: number | null
  /** 换手率（%） */
  turnover: number | null
  pe: number | null
  /** 源给的行情时间 HH:MM:SS；源没给则 null，前端用接收时刻兜底 */
  updated_at: string | null
}

/**
 * 当日资金流。
 *
 * 后端**只保证 `net ≈ inflow − outflow` 这一条不变量**：段序来自一个本环境
 * 没实测过的接口，能凑上这个等式的三元组才算数。所以字段叫「净流入」而不叫
 * 「主力净流入」—— 到底是哪个口径取决于源的返回顺序，没验证过不敢这么写。
 */
export interface LiveFlow {
  code: string
  net: number
  inflow: number
  outflow: number
  /** 净额占进出之和的百分比 */
  ratio: number
}

/** 当日分时。`avg` / `volumes` 为 null 时**整条不画**（半截线比没有更误导）。 */
export interface LiveIntraday {
  code: string
  market: Market
  date: string
  times: string[]
  prices: number[]
  avg: number[] | null
  volumes: number[] | null
  /** 昨收，取自分时接口；没有就用报价里的 prev_close */
  prev_close: number | null
}

export interface RealtimeResponse {
  quotes: LiveQuote[]
  /** key 是归一化后的代码（A 股 6 位 / 港股 5 位） */
  flows: Record<string, LiveFlow>
  /** 只给本次请求的 `detail` 那一只；不在列表里或取不到就是 null */
  intraday: LiveIntraday | null
  /** 数据源降级原因。**原样摊给用户**，不做静默吞错 */
  warnings: string[]
  as_of: string
  disclaimer: string
}

/**
 * 关注基金。与 `WatchItem` 分开存（后端也是两张表）：基金代码恒 6 位、
 * 与 A 股代码撞格式但语义完全不同，而且**没有市场与建仓日的概念**。
 */
export interface FundWatchItem {
  code: string
  name: string
  added_at: string
}

/**
 * 添加前模糊搜索的候选（`GET /api/search`）。
 *
 * **这一步是联网的**，与「添加零网络」是两件事：候选来自全市场，否则页面
 * 压根不知道「白酒指数基金」是 `161725`。取不到结果只影响多一条省事的路，
 * 手贴 6 位代码照旧能加。
 *
 * 两类候选字段不一样，这是上游决定的，前端**按 `kind` 分别读**：
 *   股票  有 `market`（`cn` / `hk`），没有下面三个
 *   基金  有 `type` / `company` / `nav` / `nav_date`，用来区分 A/C 份额
 * 后端已滤掉指数、窝轮、美股与基金行 —— 能拿到的都是加得进去的。
 */
export interface SearchItem {
  code: string
  name: string
  /** 仅股票候选：cn=沪深北 6 位，hk=港股 1~5 位 */
  market?: 'cn' | 'hk'
  /** 仅基金候选：基金类型，如「指数型-股票」 */
  type?: string | null
  /** 仅基金候选：基金公司 */
  company?: string | null
  /** 仅基金候选：最新单位净值及其日期，下拉里用来区分 A/C 份额 */
  nav?: number | null
  nav_date?: string | null
}

export interface SearchResponse {
  items: SearchItem[]
  q: string
  kind: string
  /** 仅股票搜索：回显后端实际用的市场过滤（cn / hk / 未限定） */
  market?: string | null
  /** 说明「这一步会联网」的文案，由后端给，避免前后端两处措辞漂移 */
  note: string
}

/**
 * 一只基金的盘中估值。
 *
 * **除 code / source / stale 外全部可空** —— 后端两条链（天天基金官方估值、
 * 持仓穿透）各自带不变量校验，过不了就置 null。前端必须原样显示「—」，
 * **不许填默认值**：填出来的 0 会把「没取到」和「今天真的没涨跌」变成一回事。
 *
 * 两条链能给的东西不一样，这是**刻意的**：
 *   官方估值  有 `nav` / `estimate_nav` / `estimate_pct`，无 `coverage`
 *   持仓穿透  只有 `estimate_pct`（算涨跌幅不需要昨收，算净值需要，而昨收只有
 *             官方那个接口才有 —— 两条链挂一个才轮到另一条）+ `coverage`
 * 所以任意一列出现「—」都是正常的，不是 bug。
 */
export interface FundEstimate {
  code: string
  /** "" = 两条链都失败，原因看 warnings */
  source: '' | '官方估值' | '持仓穿透'
  name: string
  /** 最新单位净值（昨收）。持仓穿透链恒为 null */
  nav: number | null
  nav_date: string | null
  estimate_nav: number | null
  /** 估算涨跌幅（%） */
  estimate_pct: number | null
  /**
   * **最近一次已公布**的机构净值涨跌幅（%）与其净值日期 —— 来自净值历史链，
   * 拿不到就是 null（`actual_date` 有值而 `actual_pct` 为 null 表示本地流水
   * 只攒到一天净值，算不出涨跌幅，同样显示「—」）。
   */
  actual_pct: number | null
  actual_date: string | null
  /**
   * 涨跌幅那一列**此刻该显示谁**，由后端按时段算好（规则见 `routes._attach_display`）：
   * 交易中→估算、盘前→前一交易日净值、盘后→当日机构净值出了就实际、没出才估算。
   * `display_kind` 为 `'none'` 时两条都没取到，前端显示「—」。
   */
  display_pct: number | null
  display_kind: 'estimate' | 'actual' | 'none'
  /** 为什么此刻显示这个值 —— 原样摊给用户（Tooltip），不做静默切换 */
  display_reason: string
  /** 持仓穿透链：取到价的重仓股合计占净值比例（%）。官方估值链恒为 null */
  coverage: number | null
  /** 源给的估值时点；源没给则 null，前端用响应的 as_of 兜底 */
  as_of: string | null
  /** 估值时点不是今天 —— 周一早上源还挂着周五的估值是常态，别当今天的看 */
  stale: boolean
  /** 口径说明。只有持仓穿透链有：覆盖了多少仓位、持仓是哪一期 */
  note: string | null
}

export interface FundEstimateResponse {
  items: FundEstimate[]
  /** 数据源降级原因。**原样摊给用户**，不做静默吞错 */
  warnings: string[]
  as_of: string
  disclaimer: string
  /**
   * 这一屏来自本地库回读（`GET /fund-estimate/last`），**不是刚查出来的**。
   * 页面必须据此挂出提示 —— 不标就是拿旧数据冒充实时。
   */
  cached?: boolean
}

/**
 * 一只基金 × 一条链的 T+1 误差统计。
 *
 * **按来源分组是必须的**：官方估值覆盖完整但口径黑箱；持仓穿透只覆盖前十大
 * 重仓股里的 A 股部分、系统性偏低。混在一起算出来的平均值谁都不代表。
 *
 * 单位一律是**百分点（pp）**，不是百分比 —— 这是「涨跌幅之差」而不是「相对误差」。
 */
export interface FundReviewRow {
  code: string
  /** 已取消关注的基金回传空串 —— 历史照算，删关注不抹掉复盘 */
  name: string
  source: '官方估值' | '持仓穿透'
  samples: number
  /** 平均误差（pp）。正 = 系统性高估，负 = 系统性低估 */
  mean_err: number
  /** 平均绝对误差（pp）—— 与有没有系统性偏差无关，纯粹是噪声水平 */
  mean_abs_err: number
  /** 样本内高估得最狠的一次（pp） */
  max_over: number
  /** 样本内低估得最狠的一次（pp） */
  max_under: number
  last_date: string
  last_err: number | null
  /** 只有持仓穿透链有：该链样本的平均覆盖率（%） */
  coverage_mean: number | null
  /** 样本 < low_sample，平均值基本是噪声 —— 页面必须标出来，别让人据此下结论 */
  low_sample: boolean
}

export interface FundReviewResponse {
  items: FundReviewRow[]
  /** 口径说明与异常计数（丢样本、对不上的条数）。**原样摊给用户** */
  notes: string[]
  /** 低于这个样本数就标低置信度 */
  low_sample: number
  disclaimer: string
}

/** 各市场的区间对比基准（后端按 market 选的指数） */
export const BENCHMARK: Record<string, string> = {
  cn: '沪深300',
  hk: '恒生指数',
}

/** 任务运行记录（job_run 表）。历史行的 status 可能是旧值 'manual'。 */
export interface JobRun {
  id: number
  kind: string
  started_at: string
  finished_at: string | null
  status: string
  detail: string
}

/** 打分那一步的内部进度。它占整条流水线八成以上的时间，缺了它进度条会卡死。 */
export interface JobSubProgress {
  stage: string
  stage_no: number
  stages: number
  done: number
  total: number
}

/** 进程内存里的实时进度，重启即清零 */
export interface JobCurrent {
  running: boolean
  kind: string
  date: string
  started_at: string
  finished_at: string
  error: string
  steps: Record<string, unknown>
  /** 本轮计划跑哪几步 —— 进度条的分母。旧后端不返回，兜底空数组 */
  plan?: string[]
  /** 正在跑哪一步；空串 = 步与步之间 / 已结束 */
  current_step?: string
  /** 仅当 `current_step === 'main_force'` 时后端才会给 */
  sub?: JobSubProgress
}

export interface ScheduleInfo {
  /** 盘后任务（16:30 抓取 + 打分） */
  enabled: boolean
  /** 14:50 估值推送 —— 与 `enabled` **各自独立**，不是同一个开关 */
  push_enabled: boolean
  hour: number
  minute: number
  next_run: string | null
  push_hour?: number
  push_minute?: number
  next_push?: string | null
}

/** 订阅者的推送汇总（按人从 `push_log` 聚合） */
export interface SubPushStat {
  total: number
  ok: number
  fail: number
  last_at: string
}

/** 一行订阅者（`GET /api/subs` 的 items 元素，后端已把计数拼好） */
export interface Sub {
  open_id: string
  nickname: string
  /** '' = 未订阅，'pro' = 订过（过期与否看 `active`） */
  plan: string
  paid_at: string | null
  expires_at: string | null
  amount: number | null
  /** 单独关注上限；null = 用全局 `fund_watch_limit` */
  fund_limit: number | null
  created_at: string
  active: boolean
  fund_count: number
  /** 生效上限（单独设置已折算进去） */
  limit: number
  push: SubPushStat
}

/** 该订阅者关注的基金 */
export interface SubFund {
  open_id: string
  code: string
  name: string
  added_at: string
}

/** 一条推送留痕（发没发、成没成、失败原因） */
export interface SubPush {
  id: number
  open_id: string
  kind: string
  sent_at: string
  ok: number
  detail: string
}

export interface SubListResponse {
  items: Sub[]
  /** 当前生效的全局配置，页面上显示默认值用 */
  settings: {
    pro_price: number
    pro_days: number
    fund_watch_limit: number
    push_hour: number
    push_minute: number
  }
}

// ---- 收银台（docs/06 §2）----------------------------------------------
// 占位演示：没有支付网关，「去支付」只生成一张待确认订单，
// 由管理员在订阅管理页照订单点确认开通。

/**
 * 收银台定价（后端 `pay.pricing()`）。存后端 SQLite 的 `kv` 表 ——
 * **改完即时生效、不用重启**；已下的订单金额是下单那一刻快照的，不受影响。
 */
export interface PayPricing {
  /** 每月单价（元） */
  price: number
  /** 可选档位（月数），页面上可增删 */
  months: number[]
  /**
   * 某档单独定价。**没有这个键 = 那档按 `price × months` 算** ——
   * 后端 `amount_of()` 是全站唯一算钱处，页面显示与订单落库因此同源。
   */
  amounts: Record<string, number>
}

/** 一个订阅档位。金额由后端 `pay.amount_of` 算：单独定过价就用它，否则单价 × 月数 */
export interface PayPlan {
  months: number
  amount: number
  days: number
  label: string
}

/** 支付方式。目前只有微信 / 支付宝，`value` 是后端认的英文标识 */
export interface PayChannel {
  value: string
  label: string
}

export interface PayOrder {
  order_no: string
  open_id: string
  nickname: string
  months: number
  /** 下单那一刻的单价快照 */
  price: number
  amount: number
  channel: string
  /** 后端算好的中文名，页面直接显示 */
  channel_label: string
  status: string
  created_at: string
  confirmed_at: string | null
  note: string
}

/** `GET /api/pay/plans` —— 档位与支付方式（不带 token，页面先渲染选月卡片） */
export interface PayPlansResponse {
  plans: PayPlan[]
  channels: PayChannel[]
  price: number
}

/** `GET /api/pay/{token}` —— 收银台首屏。**刻意不含 open_id**：页面不需要它 */
export interface PayInfo {
  nickname: string
  plans: PayPlan[]
  channels: PayChannel[]
  active: boolean
  expires_at: string
  orders: PayOrder[]
}

export interface JobStatus {
  last: JobRun | null
  current: JobCurrent
  schedule: ScheduleInfo
}

/**
 * 邀请名单的一行（`GET /api/invite/list`）。**不另开名单表** —— 名单就是
 * `subscriber` 表：`plan=''` 是发过消息但还没订阅的，`plan='pro'` 是已订阅。
 */
export interface InviteRoster {
  open_id: string
  nickname: string
  /** '' = 还没订阅，'pro' = 订过（过期与否看 `active`） */
  plan: string
  active: boolean
  expires_at: string
  /** 0 = 邀请免费开通；null = 从没开通过 */
  amount: number | null
}

export interface InviteListResponse {
  items: InviteRoster[]
  /** 免费开通给多少天（全局 `pro_days`） */
  pro_days: number
}

/** 一条邀请链接。**URL 里没有 open_id** —— 随机 token 不可逆推，过期即作废 */
export interface InviteLink {
  url: string
  token: string
  expires_at: string
}

/** 欢迎页首屏（`GET /api/invite/{token}`）。**故意不回 open_id** */
export interface InviteInfo {
  nickname: string
  active: boolean
  expires_at: string
  pro_days: number
}

/** 点「订阅」的结果。`already=true` = 本来就在订阅期，没重复累加天数 */
export interface InviteResult {
  already: boolean
  nickname: string
  expires_at: string
  amount: number | null
}

/** 推送邀请链接的结果。`sent=false` = 飞书没发出去，但链接已生成 */
export interface InvitePush {
  link: InviteLink
  sent: boolean
}

/** 链接前缀（收银台 + 邀请链接共用）。存 kv，改完即时生效 */
export interface PayBaseUrl {
  /** 形如 `http://192.168.137.10:5173`，末尾无 `/` */
  url: string
  /** `.env` 里的默认值 */
  default: string
}

/** 上一次推送的结果（飞书返回 code=0 才算成功，失败原因原样透出） */
export interface NotifyLast {
  at: string
  ok: boolean | null
  detail: string
}

/** 可勾选的推送分段（label 由后端下发，前端只负责渲染） */
export interface NotifySectionDef {
  key: string
  label: string
}

/** 飞书推送配置。webhook 与密钥存后端 SQLite，改了不用重启。 */
export interface NotifyConfig {
  enabled: boolean
  webhook: string
  secret: string
  has_webhook: boolean
  signed: boolean
  /** 已勾选的分段 key；标题/任务状态/免责声明不在其中（它们永远推送） */
  sections: string[]
  /** 分段全集与文案，按正文顺序排列 */
  section_defs: NotifySectionDef[]
  last: NotifyLast
}

/** 入站长连接的实时状态（进程内存，重启即清零） */
export interface FeishuState {
  running: boolean
  connected: boolean
  started_at: string
  last_error: string
  last_msg_at: string
  handled: number
}

/** 飞书**入站**指令配置：自建应用凭据 + 白名单 open_id。存后端 SQLite。 */
export interface FeishuConfig {
  app_id: string
  app_secret: string
  owner: string
  enabled: boolean
  /** 群里指令的回复方式：private=私发给发送人（默认）/ group=回原会话 */
  reply_mode: 'private' | 'group'
  has_app: boolean
  state: FeishuState
}

/** 「试一下」的返回：回复正文 + 最新连接状态（副作用一律没执行） */
export interface FeishuSim {
  input: string
  reply: string
  owner: string
  bound: boolean
  state: FeishuState
  has_app: boolean
}

/** 主动推送的结果：飞书收没收下 + 正文（用于页面回显） */
export interface FeishuPush {
  ok: boolean
  detail: string
  text: string
  target?: string
}

/** 数据新鲜度：该数据集最新一条快照的元信息（板块资金流的 trade_date 恒为 latest） */
export interface Freshness {
  dataset: string
  trade_date?: string
  row_count?: number
  source?: string
  fetched_at?: string
}

export interface Overview {
  trade_date: string | null
  pools: Record<PoolType, number>
  /** 连板梯队，键是连板数（字符串），值是家数 */
  ladder: Record<string, number>
  leader: {
    code: string
    name: string
    boards: number
    zt_stat?: string
    industry?: string
  } | null
  sector_flow: { top: SectorRow[]; bottom: SectorRow[] }
  main_force: {
    date: string | null
    count: number
    degraded: string[]
    top: MainForceRow[]
  }
  watchlist_count: number
  job: JobStatus
  freshness: Freshness[]
}

/** 财经新闻：后端六源聚合 + 主题分类 + 自算热度（0~100，与涨跌配色无关） */
export interface NewsLink {
  source: string
  label: string
  url: string
}

export interface NewsItem {
  id: string
  source: string
  /** 同一条新闻被哪些源报道过（去重后的源 key，主体在最前） */
  sources: string[]
  source_count: number
  source_links: NewsLink[]
  title: string
  content: string
  truncated: boolean
  published_at: string
  url: string
  category: string
  heat: number
  heat_fresh: number
  heat_keyword: number
  heat_multi: number
  heat_source: number
}

export interface NewsCategory {
  key: string
  label: string
  count: number
}

export interface NewsSourceStatus {
  key: string
  label: string
  ok: boolean
  rows: number
  error: string
}

export interface NewsResponse {
  items: NewsItem[]
  categories: NewsCategory[]
  category: string
  sources: NewsSourceStatus[]
  failed_sources: string[]
  /** true = 所有源都失败，当前展示内置演示数据 */
  demo: boolean
  fetched_at: string
  age_sec: number
  ttl_sec: number
  score_note: string
}

// ------------------------------------------------------------ 数据管理

/** 文件级占用。`kind` 区分数据文件与日志（日志另有 `label`，来自日志白名单） */
export interface DataFile {
  name: string
  path: string
  bytes: number
  kind?: string
  label?: string
  exists?: boolean
  /** 日志的 mtime 是 Unix 秒（`list_logs` 用 `stat().st_mtime`） */
  mtime?: number
}

/** 一个 dataset 的占用画像：按 (dataset, trade_date) 聚合 payload 字节数 */
export interface DataDataset {
  dataset: string
  /** 覆盖多少个交易日（`sector_flow_*` 恒为 1，trade_date 是 latest） */
  dates: number
  /** payload 内的条目合计（row_count 之和） */
  entries: number
  bytes: number
  date_min: string | null
  date_max: string | null
  fetched_at: string | null
  /** 硬下限：删完至少还要剩几天，`99999` 表示整类不可删 */
  floor_days: number
  undelatable: boolean
}

export interface DataUsage {
  db: {
    path: string
    bytes: number
    page_size: number
    page_count: number
    freelist_pages: number
    /** 删完要显式 VACUUM 才回收（该库 auto_vacuum=0） */
    reclaimable_bytes: number
    auto_vacuum: boolean
  }
  tables: { name: string; rows: number }[]
  datasets: DataDataset[]
  job_run: {
    rows: number
    /** finished_at 为空 = 进程被杀时留下的残留 */
    unfinished: number
    oldest: string | null
    newest: string | null
    bytes: number
  }
  files: DataFile[]
  logs: DataFile[]
  /** 只有键名 —— 值含飞书 App Secret 与 webhook 签名密钥，后端不回传 */
  kv_keys: string[]
  watchlist_rows: number
  retention: {
    main_force_days: number
    inflow_days: number
    pool_days: number
    job_run_keep: number
  }
  floor: Record<string, number>
  protected_tables: string[]
  inflow_backfill: boolean
}

export type PurgeScope = 'snapshot' | 'retention' | 'job_run' | 'vacuum' | 'log'

/** `scope='retention'` 的逐数据集计划：保留几天、可删几天、是否被底线抬高 */
export interface DataRetentionItem {
  dataset: string
  /** 配置的保留天数 */
  retention_days: number
  /** 实际保留天数 = max(retention, floor) */
  keep_days: number
  floor_days: number
  /** true = 配置小于硬下限，这次是底线把保留数抬上去了 */
  floored: boolean
  dates_total: number
  deletable: number
  bytes: number
  del_newest: string | null
  del_oldest: string | null
  kept_newest: string | null
  kept_oldest: string | null
}

/**
 * 清理返回。**默认 dry_run** —— 真删必须同时给 `dry_run=false` 与
 * `confirm='DELETE'`，两道门少一道后端就 400。
 *
 * `blocked` 是「想删但被硬下限拦下」的日期，前端据此解释为什么没删干净。
 */
export interface DataPurge {
  /** 后端回带，前端据此决定文案（vacuum 与 log 的字段会重叠） */
  scope?: PurgeScope
  dry_run: boolean
  applied: boolean
  error?: string
  hint?: string
  dataset?: string
  matched?: number
  deletable?: number
  deletable_bytes?: number
  dates?: string[]
  blocked?: string[]
  floor_days?: number
  reason?: string
  deleted?: number
  note?: string
  total_rows?: number
  keep?: number
  unfinished_only?: boolean
  unfinished?: number
  over_limit?: number
  ids?: number[]
  target?: DataFile | null
  /** scope='retention' 的逐数据集计划 */
  plan?: DataRetentionItem[]
  total_deletable?: number
  total_bytes?: number
  before?: number
  after?: number
  freed?: number
  name?: string
}

// ---------------------------------------------------------- 指令管理（类型）

/** 一条指令：内置的代码默认 ∪ 库内改动 ∪ 自定义。 */
export interface CmdItem {
  key: string
  kind: 'builtin' | 'custom'
  label: string
  /** 主触发文本。自定义指令里它就是「点按钮替你发出去的那句」。 */
  text: string
  /** 其余别名，逗号分隔 —— **与后端往返同形**，别改成数组。 */
  alias: string
  /** 内置的只是展示（真闸在后端 `_ACCESS`）；自定义的 sub/owner 才生效。 */
  access: string
  /** 执行后附带的推送文案，可空 */
  reply: string
  /** 实际执行的指令原文；空 = 只回 reply */
  target: string
  /** 这条指令吃什么参数（内置才有，给页面当提示） */
  param: string
  desc: string
  enabled: number
  quick: number
  sort: number
  default_text: string
  default_alias: string
  /** 表里真有这一行 = 用户改过（页面据此显示「已改」并给恢复默认） */
  edited: boolean
}

/** 推送文案模板。`text === default` 即没改过；`text` 为空则等于默认。 */
export interface CmdTpl {
  label: string
  default: string
  text: string
}

export interface CmdListResponse {
  items: CmdItem[]
  templates: Record<string, CmdTpl>
}

/** 卡片元素（只声明页面用得到的几个字段） */
export interface CardElement {
  tag: string
  text?: { tag?: string; content?: string }
  behaviors?: { type: string; value?: Record<string, string> }[]
  /** 分栏容器 —— 按钮一行 3 个就是塞在这里面，预览要顺着往下钻一层 */
  columns?: { elements?: CardElement[] }[]
}

/** 快捷指令卡片。**必须是 schema 2.0** —— 1.0 的按钮回调没有长连接模式。 */
export interface QuickCard {
  schema: string
  header?: { title?: { content?: string }; template?: string }
  body: { elements: CardElement[] }
}

// ---- 策略实验室（docs/backtest/）------------------------------------------
//
// 这里的类型一律写得**宽松**（`number | null` 而不是 `number`）：后端在缺数据
// 时写 `NULL` 不写 `0`，而 `0` 在多数指标上是个看起来像结论的数字。
// 把 null 显式留在类型里，页面才没法"顺手"把缺失渲染成 0。

export type ResearchDomainStatus = 'READY' | 'PARTIAL' | 'BLOCKED'

export interface ResearchDomain {
  status: ResearchDomainStatus
  detail: string
  /** 缺什么、缺的比例 —— `detail` 的机器可读版，表格里直接渲染 */
  issues?: string[]
}

export interface ResearchAudit {
  generated_at: string
  domain_status: Record<string, ResearchDomain>
  /** 每个信号日一条：那天到底能不能跑 */
  signal_days?: {
    date: string
    status: ResearchDomainStatus
    reason?: string
  }[]
  summary?: string
}

/**
 * 后端 `BacktestDataAuditService.audit()` 的**原始**形状 —— 键名被
 * `scripts/tests/_research_check.py`（304 条）钉死：`domains` / `overall` /
 * `signal_days[].signal_date` 与 `.missing`。
 *
 * 页面读的是上面的 `ResearchAudit`。两边字段名不同而**各自类型都成立**，
 * 所以 `tsc` 一个都不会红（2026-10-30 实测：库里 14 个信号日、接口也回了
 * 14 条，页面却一列都读不出）—— 契约对齐只在 `researchAudit()` 这一处做。
 */
interface RawResearchAudit {
  generated_at: string
  domains?: Record<string, { status: ResearchDomainStatus; reasons?: string[] }>
  signal_days?: {
    signal_date: string
    status: ResearchDomainStatus
    missing?: string[]
    prior_inflow_days?: number
  }[]
  overall?: {
    status: ResearchDomainStatus
    reasons?: string[]
    signal_days_total?: number
    signal_days_ready?: number
    strict_full_model_backtest?: boolean
    replay_only_backtest?: boolean
  }
}

/** 审计结论一句话：就绪天数 + 没就绪的域 + 能做哪种回测。 */
function auditSummary(o: NonNullable<RawResearchAudit['overall']>): string {
  const parts = [
    `整体 ${o.status}：信号日 ${o.signal_days_total ?? 0} 个、其中可回测 ${o.signal_days_ready ?? 0} 个`,
  ]
  // 后端在全部就绪时把 reasons 填成「全部数据域就绪。」—— status 先挡一道，
  // 免得拼出「未就绪的域 —— 全部数据域就绪。」这种自相矛盾的句子。
  if (o.status !== 'READY' && o.reasons?.length) {
    parts.push(`未就绪的域 —— ${o.reasons.join('，')}`)
  }
  parts.push(
    o.replay_only_backtest
      ? '仅重放回测可用（快照内的分值可确定性重放）'
      : '重放回测也不可用',
  )
  parts.push(o.strict_full_model_backtest ? '严格全模型回测可用' : '严格全模型回测不可用')
  return `${parts.join('；')}。`
}

/** 后端形状 → 页面形状。**一处对齐**，页面继续只认 `ResearchAudit`。 */
const toResearchAudit = (r: RawResearchAudit): ResearchAudit => ({
  generated_at: r.generated_at,
  domain_status: Object.fromEntries(
    Object.entries(r.domains ?? {}).map(([name, d]) => [
      name,
      {
        status: d.status,
        // READY 时后端可能一条理由都不给（如无异常的日线域）—— 显示 `—`，
        // 不编一句「一切正常」冒充后端的话。
        detail: (d.reasons ?? []).join('；') || '—',
        issues: d.reasons,
      },
    ]),
  ),
  signal_days: (r.signal_days ?? []).map((s) => ({
    date: s.signal_date,
    status: s.status,
    reason: s.missing?.length ? s.missing.join('；') : undefined,
  })),
  summary: r.overall ? auditSummary(r.overall) : undefined,
})

/** 可选项、默认值、**禁用项的理由**一次给全 —— 前端不在这里写死任何清单 */
export interface ResearchCapabilities {
  horizons: number[]
  portfolios: string[]
  sort_version: string
  limit_ratio: Record<string, number>
  universe_modes: Record<string, { enabled: boolean; reason?: string; caveat?: string }>
  cost_preset: { values: Record<string, number | boolean>; note: string }
  blocked: Record<string, string>
  audit: string
}

export interface ResearchExperiment {
  id: number
  name: string
  strategy_version: string
  universe_mode: string
  start_date: string
  end_date: string
  config_hash: string
  config_snapshot: Record<string, unknown>
  created_at: string
}

export interface ResearchRun {
  id: number
  experiment_id: number
  status: 'pending' | 'running' | 'done' | 'cancelled' | 'failed'
  stage: string
  progress: number
  message: string
  warnings: string[]
  error?: string | null
  started_at?: string | null
  finished_at?: string | null
}

export interface ResearchSignal {
  id: number
  experiment_id: number
  signal_date: string
  code: string
  name: string
  snapshot_rank: number
  replay_rank: number
  snapshot_score: number | null
  replay_score: number | null
  coverage: number | null
  dims: Record<string, unknown>
}

export interface ResearchOutcome {
  id: number
  experiment_id: number
  signal_date: string
  code: string
  name: string
  horizon: number
  status: 'OK' | 'PENDING' | 'NO_DATA'
  entry_date: string | null
  exit_date: string | null
  entry_price: number | null
  exit_price: number | null
  bench_return: number | null
  theory_return: number | null
  excess_return: number | null
  exec_return: number | null
  fill_status: string | null
  sell_status: string | null
  missing_reason: string | null
  data_quality: string | null
  quality_notes: string[]
  /**
   * 收益的复权口径三档（`engine.price_adjust`）：`RAW` / `UNADJUSTED` /
   * `UNADJUSTED_UNCERTAIN`。`null` = 这行没算出收益（`status != DONE`），
   * 谈不上口径 —— 给它填某一档会让人以为「算过了，只是没折算」。
   */
  adjust_method: string | null
  /** 口径的补充说明；UNCERTAIN 时写明「若期间发生过除权，该收益不反映真实回报」 */
  adjust_note: string | null
}

export interface ResearchMetric {
  id: number
  experiment_id: number
  run_id: number
  portfolio_rule: string
  horizon: number
  sample_size: number
  pending_count: number
  no_data_count: number
  mean_theory_return: number | null
  median_theory_return: number | null
  mean_excess_return: number | null
  valid_fills: number
  mean_exec_return: number | null
  unfilled_rate: number | null
  trading_cost: number | null
  max_drawdown: number | null
  quality_notes: string[]
}

export interface ResearchCreated {
  experiment: ResearchExperiment
  config_hash: string
}

/** 建实验的入参。`config` 全部键可选，缺省取引擎里的研究默认值 */
export interface ResearchCreateBody {
  name: string
  strategy_version?: string
  universe_mode?: string
  start_date: string
  end_date: string
  data_snapshot_id?: string
  config?: {
    weights?: Record<string, number>
    horizons?: number[]
    portfolios?: string[]
    n_top?: number
    min_score?: number
    /** 不传 = **不配成本**，可执行收益留空（2026-10-16 用户拍板） */
    cost?: Record<string, number | boolean> | null
    ablation?: { drop?: string[]; weights?: Record<string, number> }
  }
}

// ------------------------------------------ 数据管线（阶段 6：覆盖 / 采集 / 进度）

/**
 * 采集五态。它们在页面上长得像、后果完全不同 —— 「没跑任务」是没人管，
 * 「源挂了」是要修网络，「部分股票」是要怀疑数据本身。混成一个「失败」，
 * 人就不知道该干什么（见 `app/pipeline/coverage.verdict()`）。
 */
export type PipelineState = 'NO_JOB' | 'NO_DATA' | 'SOURCE_DOWN' | 'PARTIAL' | 'OK'

/** 采集状态。`WAITING` 排队 / `RUNNING` 跑着 / `SUCCESS` 过了校验 /
 * `PARTIAL` 跑完但数据不能信 / `FAILED` 异常或重试用尽 */
export type IngestStatus = 'WAITING' | 'RUNNING' | 'SUCCESS' | 'PARTIAL' | 'FAILED'

export interface PipelineJob {
  step: string | null
  last_attempt_at: string | null
  last_error: string | null
  last_result: number | null
  /** `true` = 从 `job_run.detail` **推断**的，不是权威逐域记录（ingest_job 才是） */
  inferred: boolean
}

export interface PipelineDataset {
  dataset: string
  state: PipelineState
  first_date: string | null
  last_date: string | null
  distinct_dates: number
  rows_total: number
  missing_field_rows: number
  /** 有条数却解析出 0 行 —— 落库后坏了，比缺数据更该报警 */
  unreadable_days: string[]
  missing_dates: string[]
  coverage_rate: number | null
  expected_days: number | null
  last_success_at: string | null
  thin_days: string[]
  has_history: boolean
  placeholder_latest?: number
  domain_kind?: string
  step: string | null
  job: PipelineJob
}

export interface PipelineCoverage {
  generated_at: string
  offline: boolean
  calendar: { days: number; first: string | null; last: string | null; stale: boolean; note: string }
  datasets: Record<string, PipelineDataset>
  missing_jobs: string[]
  error?: string
}

export interface PipelineGaps {
  lookback: number
  /** `{数据域: [YYYYMMDD,...]}`，只含**已过期**交易日里的缺口 */
  gaps: Record<string, string[]>
  offline: boolean
}

/**
 * `coverage_detail.window`：`data_quality_check` 落的质量体检结果。
 * **没跑过检查就没有这个键** —— `window: null` 是「没查过」，
 * 不是「查过没问题」（`GET /pipeline/health` 的 note 原话）。
 */
export interface QualityWindow {
  coverage_rate?: number | null
  expected_days?: number | null
  missing_dates?: string[]
  thin_days?: string[]
  unreadable_days?: string[]
  state?: string | null
  first_date?: string | null
  last_date?: string | null
  checked_at?: string
}

/** 健康报告里的一个核心数据域。`collected=false` = 从来没采过 —— 状态词仍只有
 *  那五个（pending/running/success/partial_success/failed），不发明第六个。 */
export interface HealthDomain {
  domain: string
  collected: boolean
  trade_date: string | null
  status: IngestStatus | null
  status_alias: string | null
  coverage_rate: number | null
  source: string | null
  attempts: number | null
  error: string | null
  window: QualityWindow | null
}

/** `GET /api/pipeline/health`（阶段 4 后端、阶段 6 接页面）。零联网。 */
export interface PipelineHealth {
  /** 最近一次「当天有记录的域**全 SUCCESS**」的交易日；`null` = 还没出现过 */
  last_success: {
    trade_date: string
    domains: string[]
    covered: number
    core: number
    /** 那天是否覆盖全部核心域 —— false 时别当整轮全绿显示 */
    complete: boolean
  } | null
  last_success_date: string | null
  domains: HealthDomain[]
  core: number
  /** 到点该补跑的条数 —— **只报不跑**，人点了才发请求 */
  retry_due: number
  /** 上次定时补跑检查的 kv（含 `ran: false`）；没跑过为 null */
  retry_check: { checked_at?: string; due?: number; ran?: boolean } | null
  checked_at: string
  offline: boolean
}

export interface IngestItem {
  id: number
  domain: string
  trade_date: string
  status: IngestStatus
  attempts: number
  max_attempts: number
  /** `null` = 不再重试或从没失败过 */
  next_retry_at: string | null
  /** 源返回多少 / 实际落库多少 / 校验没过多少 —— 三个数缺一不可 */
  processed: number | null
  stored: number | null
  failed_rows: number | null
  error: string | null
  started_at: string | null
  finished_at: string | null
  updated_at: string
}

export interface IngestList {
  items: IngestItem[]
  count: number
  offline: boolean
  note: string
}

export interface BarsFactor {
  rows: number
  /** `null` = 这张表按 schema 就没有 `adj_factor` 列，不是「全部没填」 */
  with_factor: number | null
  without_factor: number | null
  codes: number
  rate: number | null
  supported: boolean
  note?: string
}

export interface BarsReport {
  offline: boolean
  progress: {
    scope: string
    /** 名单里有几只 —— 分母来自 `load_scope`，与跑批脚本同一套判据 */
    codes: number
    /** 表内交易日数 —— 判「齐不齐」的分母 */
    days: number
    window: { start: string | null; end: string | null }
    covered: number
    partial: number
    absent: number
    /** `null` = 判不了（表空或名单空）。**不是 0%，也不是 100%** */
    rate: number | null
    incomplete: { code: string; have: number; missing: number; missing_dates: string[] }[]
    note: string
  }
  tables: Record<
    string,
    {
      coverage: { rows: number; days: number; codes: number; first_date: string | null; last_date: string | null }
      factor: BarsFactor
    }
  >
  note: string
}

export type BarsScope = 'all' | 'mf' | 'pool' | 'wl'

export interface BackfillPlan {
  domain: string
  label: string
  start: string
  end: string
  offline: boolean
  coverage: { rows: number | null; days: number | null; codes: number | null; first_date: string | null; last_date: string | null }
  already_done: number
  to_fetch: number
  todo: string[]
  todo_truncated: boolean
  /**
   * 中间断档的票数（首末日盖住了、里面有洞）—— 阶段 5。**普通回填对它们是
   * 跳过的**，要补得走 `only_gaps`（只补缺口）。不混进 `todo`：混进去就会
   * 出现「计划说跑 3、实跑 0」。
   */
  hole_codes: number
  /** 断档明细（截断到 50 只）：`segments` 是真数，`ranges` 只列前 20 段 */
  holes: { code: string; segments: number; ranges: string[][]; ranges_truncated: boolean }[]
  holes_truncated: boolean
  /** 本次计划是不是按缺口算的 */
  only_gaps: boolean
  /** 价格域恒为 `''`（不复权）；其余域为 `null` */
  adjust: string | null
  note: string
}

export interface BackfillRunResult {
  domain: string
  start: string
  end: string
  /** 这一轮本来该跑多少 */
  requested: number
  /** 受 `budget` 限制实际跑了多少 */
  ran: number
  skipped: number
  fetched: number
  /** **真正新插入**多少行（已有的不算） */
  inserted: number
  failed: number
  problems: { item: string; error: string; source?: string | null; range?: string[] }[]
  /** `false` = 还有活没干完，原样再调一次即接着跑 */
  finished: boolean
  /** 阶段 5：优雅暂停在票边界停了，`finished` 因此是 `false` */
  paused: boolean
  only_gaps: boolean
  /**
   * 源计数：`{items, rows, inserted, failed}`。东财主源、腾讯备源各自记账 ——
   * 整批失败时看得出是谁在拒，而不是只能猜「大概是被封了吧」。
   */
  sources: Record<string, { items: number; rows: number; inserted: number; failed: number }>
  /** 回填收尾自动重跑的覆盖审计（表域才有；零联网） */
  coverage?: { rows: number | null; days: number | null; codes: number | null; first_date: string | null; last_date: string | null }
  factor_coverage?: { rows: number; with_factor: number; without_factor: number }
  offline: boolean
  adjust: string | null
}

// ---- 历史补录（/replenish 页，2026-11-11）------------------------------

export type ReplenishJob = 'bars' | 'inflow' | 'pools' | 'score'

export interface ReplenishJobState {
  running: boolean
  error: string
  started_at: string
  finished_at: string
  /** 本轮总单元数（只 / 天×4 池 / 交易日） */
  total: number
  done: number
  /** 真正新落库的行数 */
  rows: number
  failed: number
  /** 跳过/失败样本（前 100 条），每条带原因 */
  skipped: { key: string; reason: string }[]
  msg: string
}

export interface ReplenishStatus {
  active: ReplenishJob | null
  jobs: Record<ReplenishJob, ReplenishJobState>
}

// ---------------------------------------------------------------- 接口

// ---- 自动买卖策略 V1（模拟盘，docs/ai_trading_lab/）-------------------------
//
// 七个端点全部读写**本地 SQLite**：信号/订单/成交落模拟账户表，按 D+1
// 开盘价虚拟撮合。没有实盘通道，也不发任何真实下单指令 —— 前端仅有的两个
// POST（生成信号 / 执行订单）只在页面上的人的点击里出现（_ui_offline_check
// 的 T 段盯着这条边界）。

export interface TradingAccount {
  id: number
  cash: number
  initial_cash: number
  updated_at?: string
}

export interface TradingPosition {
  code: string
  name: string | null
  qty: number
  locked_qty: number
  locked_date: string | null
  avg_cost: number
  cost_factor: number | null
  entry_date: string
}

export interface TradingRun {
  id: number
  strategy: string
  signal_date: string
  exec_date: string | null
  rule_version: string
  param_version: string
  /** OK=真跑出了信号；REFUSED=明确不交易（refusal_reason 必有理由） */
  status: string
  refusal_reason: string | null
  risk_state: string | null
  quality_state: string | null
  candidates_n: number
  signals_n: number
  created_at?: string
}

export interface TradingSignalReason {
  rule: string
  detail: string
}

export interface TradingSignal {
  id: number
  run_id: number
  code: string
  name: string | null
  side: string // BUY | SELL | HOLD（HOLD 也逐票留痕）
  signal_date: string
  exec_date: string | null
  reasons: TradingSignalReason[]
  target_qty: number | null
  alloc_pct: number | null
  est_price: number | null
  rule_version: string
  param_version: string
  snapshot_date: string | null
  /** NULL = 信号日无行情（缺行情不编造） */
  bar_date: string | null
}

export interface TradingOrder {
  id: number
  signal_id: number
  code: string
  name: string | null
  side: string
  qty: number
  /** PENDING | FILLED | REJECTED | EXPIRED */
  status: string
  exec_date: string | null
  est_price: number | null
  reject_reason: string | null
  fill_price: number | null
  fee: number | null
}

export interface TradingStatus {
  account: TradingAccount | null
  positions: TradingPosition[]
  pending_orders: number
  last_run: TradingRun | null
  initialized: boolean
}

export interface TradingRunResult {
  run_id: number | null
  status: 'OK' | 'REFUSED'
  /** 同日重跑命中：回既有结果，不重复下单 */
  idempotent: boolean
  signal_date: string | null
  exec_date: string | null
  risk_state?: string | null
  quality_state?: string | null
  signals: Record<string, number>
  orders: { placed: number; risk_rejected: number }
  portfolio?: {
    cash?: number
    position_value?: number
    equity?: number
    exposure?: number
    available?: number
  }
  refusal_reason: string | null
}

export interface TradingExecResult {
  ran: boolean
  exec_date: string | null
  reason?: string | null
  filled: number
  rejected: number
  expired: number
}

export interface TradingParams {
  strategy: string
  rule_version: string
  defaults: Record<string, number | boolean>
  version_of_defaults: string
  notes: Record<string, string>
}

/**
 * 单票回填的缺省起点 = 项目历史日线基线（与 `scripts/backfill_run.py --start`
 * 同一天）。页面「补历史」按钮拉的就是这一段 —— 与全市场跑批同一口径，
 * 拉出来的票才和库里其余 2700 多只可比。
 */
export const BACKFILL_BASE_START = '20250801'

export interface TradingRuleParamSpec {
  type: 'int' | 'float' | 'bool'
  default: number | boolean
  min?: number
  max?: number
  label: string
}
export interface TradingRuleType {
  label: string
  desc: string
  params: Record<string, TradingRuleParamSpec>
}
export interface TradingRule {
  id: string
  category: 'filter' | 'exit'
  type: string
  name: string
  enabled: boolean
  order: number
  params: Record<string, number | boolean>
}
export interface TradingRulesPayload {
  rules: TradingRule[]
  types: { filter: Record<string, TradingRuleType>; exit: Record<string, TradingRuleType> }
  version: string
}
export interface TradingLiveTrade {
  ts: string
  code: string
  name?: string | null
  side: 'BUY' | 'SELL'
  qty: number
  price: number
  amount: number
  fee: number
  pnl?: number | null
  rule?: string | null
  detail?: string | null
  note?: string | null
}
export interface TradingFunnelStage {
  rule_id: string
  name: string
  type: string
  passed_n: number
  blocked_n: number
  detail0?: string
}
export interface TradingWatchItem {
  code: string
  name?: string | null
  price: number
  notes: string[]
}
export interface TradingLivePosition {
  code: string
  name?: string | null
  qty: number
  avg_cost: number
  peak_price?: number | null
  entry_ts: string
  price: number
  market_value: number
  pnl: number
}
export interface TradingLiveAccount {
  cash: number
  initial_cash: number
  realized_pnl: number
  position_value: number
  equity: number
  total_pnl: number
}
export interface TradingLiveState {
  running: boolean
  interval: number
  scheduled: boolean
  in_session: boolean
  last_tick?: string | null
  last_error?: string | null
  tick_count: number
  funnel: TradingFunnelStage[]
  buys: TradingLiveTrade[]
  sells: TradingLiveTrade[]
  watch: TradingWatchItem[]
  warns: string[]
  account?: TradingLiveAccount | null
  positions: TradingLivePosition[]
}

export const api = {
  limitPool: (type: PoolType, date?: string, refresh = false) => {
    const q = new URLSearchParams({ type })
    if (date) q.set('date', date)
    if (refresh) q.set('refresh', 'true')
    return request<LimitResponse>(`/api/limit?${q}`)
  },

  /** 个股日线。后端 30 分钟缓存；refresh=true 忽略缓存重拉。 */
  stockDaily: (code: string, days = 90, refresh = false) => {
    const q = new URLSearchParams({ days: String(days) })
    if (refresh) q.set('refresh', 'true')
    return request<StockDailyResponse>(`/api/stock/${code}/daily?${q}`)
  },

  sectorFlow: (kind: 'industry' | 'concept', refresh = false) => {
    const q = new URLSearchParams({ kind })
    if (refresh) q.set('refresh', 'true')
    return request<SectorResponse>(`/api/sector-fund-flow?${q}`)
  },

  /** 主力建仓候选。404 表示尚未打分。 */
  mainForce: (date?: string, all = false) => {
    const q = new URLSearchParams()
    if (date) q.set('date', date)
    if (all) q.set('all', 'true')
    const suffix = q.toString()
    return request<MainForceResponse>(`/api/main-force${suffix ? `?${suffix}` : ''}`)
  },

  mainForceState: () => request<MainForceState>('/api/main-force/status'),

  /** 后台线程开跑，立即返回；随后轮询 mainForceState。 */
  mainForceRecompute: (date?: string) =>
    request<{ status: string; date?: string }>(
      `/api/main-force/refresh${date ? `?date=${date}` : ''}`,
      { method: 'POST' },
    ),

  mainForceDates: () => request<{ dates: string[] }>('/api/main-force/dates'),

  /**
   * 上榜矩阵：一次拿多天，列里含漏跑的交易日。只读本地 DB 与日历缓存，
   * 后端不会为此发起任何外网请求（内网下会被拦截并记录）。
   */
  mainForceMatrix: (days = 90) =>
    request<MainForceMatrix>(`/api/main-force/matrix?days=${days}`),
  /** 前5跟踪名单（零联网读本地快照与日线） */
  mainForceTop5Track: () =>
    request<MainForceTop5Track>('/api/main-force/top5-track'),

  /**
   * 近 N 日逐日主力净流入。只读 DB、不阻塞请求，覆盖不足时后端**可能**起后台
   * 回补线程，用 `missing` 反馈还差几只 —— 但轮询条件必须是
   * `missing > 0 && backfilling`：`STOCK_INFLOW_BACKFILL=false` 或回补正处在
   * 10 分钟冷却里时 `backfilling` 为 false，再轮询只会空转；内网下每一轮都
   * 会真的打出一批被安全设备拦截并记录的外网请求。
   */
  mainForceInflow: (date: string | undefined, codes: string[], days = 5) => {
    const q = new URLSearchParams({ days: String(days) })
    if (date) q.set('date', date)
    q.set('codes', codes.join(','))
    return request<MainForceInflow>(`/api/main-force/inflow?${q}`)
  },

  /** 关注列表。perf=true 会顺带逐只算区间表现（首屏慢，之后走缓存）。 */
  watchlist: (perf = false) =>
    request<{ items: WatchItem[] }>(`/api/watchlist${perf ? '?perf=true' : ''}`),

  /** 关注。重复关注同一代码会刷新建仓日与打分快照。 */
  watchAdd: (item: {
    code: string
    name: string
    /** 不传 = 后端取最近交易日（手动关注时的默认起点） */
    source_date?: string
    /** 不传 = 按代码位数自动判：6 位 A 股 / 5 位港股 */
    market?: 'cn' | 'hk'
    industry?: string
    rank?: number
    score?: number
    price?: number
    snapshot?: WatchSnapshot
  }) =>
    request<{ items: WatchItem[] }>('/api/watchlist', {
      method: 'POST',
      body: JSON.stringify(item),
    }),

  watchRemove: (code: string) =>
    request<{ items: WatchItem[] }>(`/api/watchlist/${code}`, { method: 'DELETE' }),

  /** 关注基金列表。本地 SQLite，**不联网**。 */
  fundWatchList: () => request<{ items: FundWatchItem[] }>('/api/fund-watchlist'),

  /**
   * 添加基金 —— **纯本地写库**，名称留空也照样成功。名称等第一次查估值时
   * 由后端回填（只补空的，手填的优先于接口给的）。
   */
  fundWatchAdd: (code: string, name = '') =>
    request<{ items: FundWatchItem[] }>('/api/fund-watchlist', {
      method: 'POST',
      body: JSON.stringify({ code, name }),
    }),

  fundWatchRemove: (code: string) =>
    request<{ items: FundWatchItem[] }>(`/api/fund-watchlist/${code}`, {
      method: 'DELETE',
    }),

  /**
   * 添加前的模糊搜索 —— **本页唯一会联网的添加步骤**，防抖 400ms 触发。
   * 与 `fundWatchAdd` / `watchAdd` 的「纯本地写库」是两件事；搜索失败不
   * 影响添加（手贴 6 位代码照旧）。
   *
   * `market` 只对 `stock` 有意义：子菜单（只看 A 股 / 只看港股）必须传，
   * 否则候选里会混进一个点下去必 400 的代码。
   */
  search: (q: string, kind: 'fund' | 'stock', market?: 'cn' | 'hk') => {
    const p = new URLSearchParams({ q, kind })
    if (kind === 'stock' && market) p.set('market', market)
    return request<SearchResponse>(`/api/search?${p}`)
  },

  /**
   * 盘中估值。**手动触发，不轮询** —— 点一次发一次，是这个页面与实时行情页
   * 最大的不同（后者 5 秒一轮）。每个 code 都会回来一行，两条链都失败时该行
   * 全字段为 null。
   */
  fundEstimate: (codes: string[]) =>
    request<FundEstimateResponse>(
      `/api/fund-estimate?${new URLSearchParams({ codes: codes.join(',') })}`,
    ),

  /**
   * 上次查到的估值。**只读本地 SQLite，一个网络请求都不发** —— 跟 `fundReview`
   * 一样能在首屏拉，用来把页面从「每次打开都空着」变成「上次的结果直接在」。
   * 带 `cached: true`，前端必须标出「这是上次查询的结果」。
   */
  fundEstimateLast: () => request<FundEstimateResponse>('/api/fund-estimate/last'),

  /**
   * T+1 误差复盘。**只读本地 SQLite，一个网络请求都不发** —— 所以页面首屏就
   * 能拉它，不违反「不点查询估值就不发外网请求」那条承诺。
   */
  fundReview: () => request<FundReviewResponse>('/api/fund-review'),

  /** 关注股详情：建仓日至今的 K 线 + 区间表现 + 同期基准（A 股=沪深300 / 港股=恒生指数）+ 后续再入选 */
  watchSeries: (code: string, refresh = false) =>
    request<WatchDetail>(
      `/api/watchlist/${code}/series${refresh ? '?refresh=true' : ''}`,
    ),

  /**
   * 实时行情：**只查 codes 里这些**，不扫全市场。`detail` 是要带当日分时的那只
   * （须包含在 codes 里）。一次请求 = A 股一批 + 港股一批 + 资金流一批 + 分时一只，
   * 上游共 3~4 次请求，本地熔断互不影响。
   */
  realtime: (codes: string[], detail = '') => {
    const q = new URLSearchParams({ codes: codes.join(',') })
    if (detail) q.set('detail', detail)
    return request<RealtimeResponse>(`/api/realtime?${q}`)
  },

  /** 总览页一次性聚合（全读本地 SQLite，不发网络请求） */
  overview: () => request<Overview>('/api/overview'),

  /**
   * 财经新闻。固定拉全量（category=all），分类切换在本地过滤，
   * 避免每点一次分类就打一次源站；refresh=true 绕过 5 分钟缓存。
   */
  news: (category = 'all', refresh = false) => {
    const q = new URLSearchParams({ category })
    if (refresh) q.set('refresh', 'true')
    return request<NewsResponse>(`/api/news?${q}`)
  },

  // ---- 订阅管理（owner 专属，docs/06）---------------------------------
  subsList: () => request<SubListResponse>('/api/subs'),

  /** 收银台定价（订阅管理页的定价卡片）。存 kv 表，改完即时生效、不用重启 */
  subPricing: () => request<{ pricing: PayPricing }>('/api/subs/pricing'),

  /** 存定价。`amounts` 缺哪档 = 那档回到线性（单价 × 月数），不用单独的删除动作 */
  subPricingSave: (body: {
    price: number
    months: number[]
    amounts: Record<string, number>
  }) =>
    request<{ pricing: PayPricing }>('/api/subs/pricing', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---- 邀请链接（docs/06 §11）------------------------------------------
  /** 邀请名单。名单 = 发过消息的人，`plan=''` 是还没订阅的 */
  inviteList: () => request<InviteListResponse>('/api/invite/list'),

  /** 给某人生成一条邀请链接。**每次都是新的一条**，旧的到各自过期为止 */
  inviteMake: (open_id: string) =>
    request<{ link: InviteLink }>('/api/invite/make', {
      method: 'POST',
      body: JSON.stringify({ open_id }),
    }),

  /**
   * 生成邀请链接并**飞书私聊发给他**（`send_text` + `id_type=open_id`，现成能力，
   * 与长连接无关 —— 那是收消息的路）。`sent=false` = 飞书拒了，但 `link` 照样
   * 给了，复制手发即可。
   */
  invitePush: (open_id: string, nickname: string) =>
    request<InvitePush>('/api/invite/push', {
      method: 'POST',
      body: JSON.stringify({ open_id, nickname }),
    }),

  /**
   * 手工把人加进名单。**不调飞书接口** —— 正常入口是收到消息自动落一行，但
   * 长连接在公司内网连不上时名单会一直空着，owner 可以在这里手工补人。
   */
  inviteJoin: (open_id: string, nickname: string) =>
    request<{ item: InviteRoster }>('/api/invite/join', {
      method: 'POST',
      body: JSON.stringify({ open_id, nickname }),
    }),

  /** 欢迎页首屏。404 = 链接已失效 */
  inviteInfo: (token: string) =>
    request<InviteInfo>(`/api/invite/${encodeURIComponent(token)}`),

  /** 点「订阅」→ 免费开通。已在订阅期只回状态，不重复累加天数 */
  inviteActivate: (token: string) =>
    request<{ result: InviteResult }>(
      `/api/invite/${encodeURIComponent(token)}/activate`,
      { method: 'POST' },
    ),

  /**
   * 链接前缀（收银台 + 邀请链接共用）。换网络（手机热点 / 内网 / 公网域名）
   * 时在页面上改，存 kv 即时生效 —— 不用改 `.env` 也不用重启后端。
   */
  payBaseGet: () => request<PayBaseUrl>('/api/system/pay-base-url'),

  /** 只影响之后新生成的链接，已发出去的是快照、不动 */
  payBaseSet: (url: string) =>
    request<{ url: string }>('/api/system/pay-base-url', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),

  /** 开通 / 续期。金额留空由后端用全局 `pro_price`（占位价，可后改）。 */
  subActivate: (open_id: string, amount: number | null, nickname = '') =>
    request<{ item: Sub }>('/api/subs', {
      method: 'POST',
      body: JSON.stringify({ open_id, amount, nickname }),
    }),

  /** 行内编辑：`fund_limit: 0` = 恢复全局默认；`close: true` = 关闭订阅 */
  subUpdate: (
    open_id: string,
    patch: {
      amount?: number | null
      fund_limit?: number | null
      nickname?: string | null
      close?: boolean
    },
  ) =>
    request<{ item: Sub }>(`/api/subs/${encodeURIComponent(open_id)}`, {
      method: 'POST',
      body: JSON.stringify(patch),
    }),

  subFunds: (open_id: string) =>
    request<{ items: SubFund[]; limit: number }>(
      `/api/subs/${encodeURIComponent(open_id)}/funds`,
    ),

  subPushes: (open_id: string) =>
    request<{ items: SubPush[] }>(`/api/subs/${encodeURIComponent(open_id)}/pushes`),

  /** 立刻跑一次 14:50 推送（漏推告警的手动入口，不查交易日） */
  subsPushNow: () =>
    request<{ push: { targets: number; sent: number; failed: number } }>(
      '/api/subs/push',
      { method: 'POST' },
    ),

  // ---- 收银台（docs/06 §2）--------------------------------------------
  payPlans: () => request<PayPlansResponse>('/api/pay/plans'),

  payInfo: (token: string) => request<PayInfo>(`/api/pay/${encodeURIComponent(token)}`),

  /** 下单。金额不传 —— 后端按 `pro_price × months` 现算并快照。 */
  payOrder: (token: string, months: number, channel: string, note = '') =>
    request<{ order: PayOrder }>(`/api/pay/${encodeURIComponent(token)}/order`, {
      method: 'POST',
      body: JSON.stringify({ months, channel, note }),
    }),

  /** 待确认订单（订阅管理页）。重复确认会被后端 409 挡下。 */
  payOrders: () => request<{ items: PayOrder[] }>('/api/pay/orders'),

  payConfirm: (orderNo: string) =>
    request<{ order: PayOrder }>(
      `/api/pay/orders/${encodeURIComponent(orderNo)}/confirm`,
      { method: 'POST' },
    ),

  jobStatus: () => request<JobStatus>('/api/jobs/status'),

  /** 最近的任务运行历史（新的在前） */
  jobHistory: (limit = 20) => request<{ items: JobRun[] }>(`/api/jobs/history?limit=${limit}`),

  /**
   * 开/关定时调度。**两个开关各自独立**：`enabled` = 盘后任务（16:30 抓取
   * + 打分），`push_enabled` = 14:50 估值推送。传哪个改哪个，不传的保持原样。
   */
  setSchedule: (patch: { enabled?: boolean; push_enabled?: boolean }) =>
    request<{ schedule: ScheduleInfo }>('/api/jobs/schedule', {
      method: 'POST',
      body: JSON.stringify(patch),
    }),

  /** 后台跑一轮完整盘后流水线（含打分约 12-15 分钟），立即返回。 */
  runJob: (date?: string, force = false) => {
    const q = new URLSearchParams()
    if (date) q.set('date', date)
    if (force) q.set('force', 'true')
    const suffix = q.toString()
    return request<{ status: string; date: string | null; state: JobCurrent }>(
      `/api/jobs/run${suffix ? `?${suffix}` : ''}`,
      { method: 'POST' },
    )
  },

  /** 飞书推送配置 + 上次发送结果 */
  notifyConfig: () => request<{ notify: NotifyConfig }>('/api/jobs/notify'),

  /** 保存推送配置（地址/密钥/开关/勾选的分段）。开关管的是**自动**推送：
   * 只有定时任务跑完才发，手动「立即执行」不发 —— 手动跑完要收就点 pushDigest。
   * `sections` 省略则保持现有勾选。 */
  saveNotify: (body: {
    enabled: boolean
    webhook: string
    secret: string
    sections?: string[]
  }) =>
    request<{ notify: NotifyConfig }>('/api/jobs/notify', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 按库内最新数据拼一条摘要并**立即发送**，返回发送结果与正文预览 */
  testNotify: () =>
    request<{ ok: boolean; detail: string; at: string; text: string; date: string }>(
      '/api/jobs/notify/test',
      { method: 'POST' },
    ),

  /** 把最近一轮任务的**真实结果**（状态/触发方式/失败步骤）拼成摘要发出去，
   * 正文与自动推送一致。手动「立即执行」跑完不会自动推，想推就点这个。
   * 配置缺失时后端回 `{skipped}` 而不是报错 —— 那是「为什么没发」的说明。 */
  pushDigest: () =>
    request<{ ok?: boolean; detail?: string; at?: string; skipped?: string }>(
      '/api/jobs/notify/digest',
      { method: 'POST' },
    ),

  /** 入站指令配置 + 长连接状态 */
  feishuConfig: () => request<{ feishu: FeishuConfig }>('/api/feishu/config'),

  /** 保存入站配置：预检凭据 → 需要时重启长连接，不用重启后端 */
  saveFeishu: (body: {
    app_id: string
    app_secret: string
    owner: string
    enabled: boolean
    /** 不传 = 后端不动这个开关（防只发凭据的调用方把它抹回默认） */
    reply_mode?: 'private' | 'group'
  }) =>
    request<{ feishu: FeishuConfig }>('/api/feishu/config', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 把一条指令按分发逻辑走一遍并回回复，**不跑任务也不改关注** */
  simFeishu: (text: string) =>
    request<FeishuSim>('/api/feishu/simulate', {
      method: 'POST',
      body: JSON.stringify({ text }),
    }),

  /** **主动**往飞书推一条看板状态（发给白名单那个人的单聊） */
  pushFeishu: () =>
    request<{ push: FeishuPush }>('/api/feishu/push', { method: 'POST' }),

  /**
   * 数据占用画像。**只读**：后端只跑 SELECT，不写库也不发网络请求；
   * `kv` 只回键名（值是飞书 App Secret 与 webhook 签名密钥）。
   */
  dataUsage: () => request<DataUsage>('/api/data/usage'),

  /**
   * 数据清理。默认 `dry_run=true` —— 只算不删；真删要同时给
   * `dry_run:false` + `confirm:'DELETE'`，少一个后端 400。
   *
   * `scope='snapshot'` 需要 `dataset`，且 `date`（删这天）与 `before`
   * （删这天之前）二给一；硬下限内的日期会进 `blocked`，不会删。
   */
  dataPurge: (body: {
    scope: PurgeScope
    dataset?: string
    date?: string
    before?: string
    log_name?: string
    unfinished_only?: boolean
    dry_run?: boolean
    confirm?: string
  }) =>
    request<DataPurge>('/api/data/purge', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // ---- 数据管线：覆盖 / 采集状态 / 回填进度（阶段 6）----------------------

  /**
   * 采集覆盖审计：每个数据域「采到哪天、缺什么、上次错在哪」+ 五态判定。
   * **只读本地库、零联网** —— 交易日历读本地缓存，过期就照实说，
   * 不会为拿一份新日历去外发一次请求。
   */
  pipelineCoverage: () => request<PipelineCoverage>('/api/pipeline/coverage'),

  /** 近期漏采速查（只看最近 N 个**已过期**的交易日）。零联网。 */
  pipelineGaps: (lookback = 30) =>
    request<PipelineGaps>(`/api/pipeline/gaps?lookback=${lookback}`),

  /** 逐域采集状态（`ingest_job` 权威表）。零联网。 */
  pipelineIngest: (limit = 50) =>
    request<IngestList>(`/api/pipeline/ingest?limit=${limit}`),

  /**
   * 历史日线 + 复权因子的落库事实与回填进度。**零联网**。
   *
   * `progress.rate` 为 `null` 表示「判不了」（表空 / 名单空）—— **不是 0%**，
   * 也不是 100%；这两种数编出来都是骗人的。
   */
  pipelineBars: (scope: BarsScope = 'all', limit = 20) =>
    request<BarsReport>(`/api/pipeline/bars?scope=${scope}&limit=${limit}`),

  /**
   * 管线健康体检（阶段 6 接页面）：最近一次成功交易日 + 各域数据质量 +
   * 到点待补跑条数。**零联网**，只读 `ingest_job` + kv。
   *
   * `retry_due` 只是**报告**：取数请求仍然只有人点「补跑重试」那一条路
   * —— 定时触发点也只查不跑（`scheduler._retry_fire` 的 `ran` 恒 false）。
   */
  pipelineHealth: () => request<PipelineHealth>('/api/pipeline/health'),

  /**
   * 回填计划：已覆盖什么、还缺什么、**要发多少次请求**。零联网 ——
   * 动手前先看这个，不能让人手滑一下就开跑。
   *
   * `daily_bar` 必须给 `scope`（或显式 `items`）：名单取自本地库，后端不联网取名单。
   * `onlyGaps` = 按缺口算待办（阶段 5）；无论开关如何，中间断档的票都报在 `holes` 里。
   */
  backfillPlan: (
    domain: string,
    start: string,
    end: string,
    scope?: BarsScope,
    onlyGaps?: boolean,
  ) => {
    const q = new URLSearchParams({ domain, start, end })
    if (scope) q.set('scope', scope)
    if (onlyGaps) q.set('only_gaps', 'true')
    return request<BackfillPlan>(`/api/pipeline/backfill/plan?${q}`)
  },

  /**
   * ⚠️ **会联网**：把**到点**的采集重试跑掉。只在人点「补跑重试」时调 ——
   * 启动、页面刷新、任何定时器都不碰它。
   *
   * `domain` 只补那一个数据域（`pool_zt` 这种），不传 = 全部到点的。
   * 两种情况都**只跑已经到点的** —— 挑域不绕过退避，源挂着时不会被反复打。
   */
  collectRetry: (domain?: string) =>
    request<{
      checked: number
      ran: number
      results: {
        domain: string
        trade_date: string
        status: string
        stored?: number
        error?: string
        reason?: string
      }[]
    }>(
      `/api/pipeline/collect/retry${domain ? `?domain=${encodeURIComponent(domain)}` : ''}`,
      { method: 'POST' },
    ),

  /**
   * ⚠️ **会联网**，且是几千次请求量级。`budget` = 这一次最多发多少请求，
   * 返回 `finished=false` 表示还有活没干完 —— **原样再调一次接着跑**。
   * `onlyGaps` = 只补缺口：逐票比对已有交易日，中间断档的段单独重试。
   */
  backfillRun: (body: {
    domain: string
    start: string
    end: string
    scope?: BarsScope
    budget?: number
    onlyGaps?: boolean
  }) =>
    request<BackfillRunResult>('/api/pipeline/backfill', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /**
   * ⚠️ **会联网**：手动拉取**一只**票的历史日线（`daily_bar`），不必等全市场
   * 跑批。复用同一个 `/api/pipeline/backfill` 端点（`items=[code]`），落库
   * 同样是 `ON CONFLICT DO NOTHING` —— 已有的行一个都不会被改写。
   *
   * `only_gaps=true`：首末日已盖住 [start, end] 时本地挖洞、只补中间缺的那几
   * 天，一天不缺就一次请求都不发；区间没盖住（如缺最近几天）照旧整段取。
   * 键名是后端读的 snake_case（`backfillRun` 类型里的 `onlyGaps` 后端不认，
   * 那是数据管理页从没传过的死字段）。
   */
  backfillOne: (code: string, start = BACKFILL_BASE_START, end = '') =>
    request<BackfillRunResult>('/api/pipeline/backfill', {
      method: 'POST',
      body: JSON.stringify({ domain: 'daily_bar', items: [code], start, end, only_gaps: true }),
    }),

  /**
   * 历史补录四个 job 的状态。**零联网** —— 只读内存状态字典。
   */
  replenishStatus: () => request<ReplenishStatus>('/api/replenish/status'),

  /**
   * ⚠️ **会联网**，且是几百到上万次请求量级。只在「历史补录」页的人点按钮
   * 时调 —— 启动、页面刷新、任何定时器都不碰它。断点续跑：原样再点一次就是续跑。
   */
  replenishStart: (job: ReplenishJob, start?: string, end?: string) =>
    request<{ status: string; job: ReplenishJob; start: string; end: string }>(
      '/api/replenish/start',
      { method: 'POST', body: JSON.stringify({ job, start, end }) },
    ),

  /** 优雅停止（票/批边界退出，已落库的都保留）；停没在跑的 job 是无害操作。 */
  replenishStop: (job: ReplenishJob) =>
    request<{ stop_requested: boolean; job: ReplenishJob }>('/api/replenish/stop', {
      method: 'POST',
      body: JSON.stringify({ job }),
    }),

  /**
   * 停止整个看板：后端先结束前端 dev server，随后自己退出。
   * 返回后这个页面就不能再取数了，调用方应切到「服务已关闭」屏。
   */
  stopServices: () =>
    request<{ ok: boolean; detail: Record<string, string>; note: string }>(
      '/api/system/stop',
      { method: 'POST' },
    ),

  // ---- 指令管理 / 快捷指令 ------------------------------------------

  /** 指令列表（内置 ∪ 改动 ∪ 自定义）+ 两个推送文案模板的当前值 */
  cmdList: () => request<CmdListResponse>('/api/cmds'),

  /** 保存一条。400 = 校验没过（key 格式 / 触发词撞内置 / access 档位）。 */
  cmdSave: (body: {
    key: string
    kind: string
    label: string
    text: string
    alias: string
    access: string
    reply: string
    target: string
    enabled: boolean
    quick: boolean
    sort: number
  }) =>
    request<{ item: CmdItem }>('/api/cmds', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 删一条：内置 = 恢复代码默认，自定义 = 彻底消失。 */
  cmdRemove: (key: string) =>
    request<{ deleted: boolean }>(`/api/cmds/${encodeURIComponent(key)}`, {
      method: 'DELETE',
    }),

  /** 保存推送文案模板。`text` 清空即恢复默认；免责声明不在模板里。 */
  cmdTemplate: (kind: string, text: string) =>
    request<{ templates: Record<string, CmdTpl> }>('/api/cmds/template', {
      method: 'POST',
      body: JSON.stringify({ kind, text }),
    }),

  /** 只**拼**卡片 JSON、不发送 —— 页面预览，零网络。 */
  quickCardPreview: () => request<{ card: QuickCard }>('/api/cmds/quick-card'),

  /**
   * 把快捷指令卡片发到飞书。不填 `to` = 发给白名单那个人的单聊。
   * 502 = 连不上 / 被飞书拒绝，**页面按失败显示，别当成功**。
   */
  quickCardSend: (to = '', idType: 'open_id' | 'chat_id' = 'open_id') =>
    request<{ ok: boolean; to: string }>('/api/cmds/quick-card', {
      method: 'POST',
      body: JSON.stringify({ to, id_type: idType }),
    }),

  // ---- 策略实验室（docs/backtest/）---------------------------------------
  //
  // 这一组端点**一个外发请求都不发**：全部读本地 SQLite。日线不落库，所以
  // 缺什么报什么，不会去"顺手取一下"历史行情。
  //
  // 列表端点一律**分页**：实测一条 243 KB 的快照整条读只有 214 ops/s，
  // 按字段取有 870；结果行数 = 信号数 × 观察期数，不加分页会把整条链拖慢。

  /** 数据可回测性审计。故意不缓存 —— 库在变，缓存会让页面停在旧结论上 */
  researchAudit: () =>
    request<RawResearchAudit>('/api/research/audit').then(toResearchAudit),

  /**
   * 可选项 / 默认值 / **禁用项的理由**。建实验表单的下拉清单全从这里来，
   * 不在前端写死 —— 后端改了口径，页面自己就跟上。
   */
  researchCapabilities: () => request<ResearchCapabilities>('/api/research/capabilities'),

  /** 实验列表（新的在前）。不带 config_snapshot —— 列表页用不上 */
  researchExperiments: () => request<{ items: ResearchExperiment[] }>(
    '/api/research/experiments',
  ),

  /**
   * 建实验，配置到此冻结。
   * 校验不过返回 400，**理由原样带回**（模式 B 的拒绝文案就走这条路，
   * 前端不用自己复述一遍为什么不能选）。
   */
  researchExperimentCreate: (body: ResearchCreateBody) =>
    request<ResearchCreated>('/api/research/experiments', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 详情：实验本身 + 冻结配置 + 运行历史。「当时到底按什么参数跑的」一并给 */
  researchExperimentDetail: (id: number) =>
    request<{
      experiment: ResearchExperiment
      config_hash: string
      runs: ResearchRun[]
    }>(`/api/research/experiments/${id}`),

  /**
   * 开一轮运行（后台线程，立即返回）。
   * 重复点会 **409**（同一把锁里拒绝已有未完成运行），不会静默多开一条。
   */
  researchExperimentRun: (id: number) =>
    request<{ run: ResearchRun }>(`/api/research/experiments/${id}/run`, {
      method: 'POST',
    }),

  researchExperimentRuns: (id: number) =>
    request<{ items: ResearchRun[] }>(`/api/research/experiments/${id}/runs`),

  /**
   * 删实验，**级联**清掉它的全部运行、信号、结果与指标（一次事务，
   * 逐表回报删了几行）。有未完成的运行或取消后线程还没退出会 **409** ——
   * 先取消、稍候再删，不会删一半被引擎写回孤儿数据。
   */
  researchExperimentDelete: (id: number) =>
    request<{ deleted: Record<string, number> }>(
      `/api/research/experiments/${id}`,
      { method: 'DELETE' },
    ),

  /** 轮询用。进度写在库里，**进程重启也不丢** */
  researchRunDetail: (id: number) => request<{ run: ResearchRun }>(
    `/api/research/runs/${id}`,
  ),

  /**
   * 协作式取消：引擎在阶段边界和长循环里查状态，所以不会立刻停，但一定会停。
   * 已写入的信号保留、指标**不写** —— 半成品不冒充完整结果。
   */
  researchRunCancel: (id: number) =>
    request<{ cancelled: boolean; run: ResearchRun }>(
      `/api/research/runs/${id}/cancel`,
      { method: 'POST' },
    ),

  /** 信号分页。`has_more` 如实回报，不静默丢数据 */
  researchSignals: (id: number, limit = 200, offset = 0) =>
    request<{ items: ResearchSignal[]; has_more: boolean }>(
      `/api/research/experiments/${id}/signals?limit=${limit}&offset=${offset}`,
    ),

  /**
   * 逐条结果。**建议按 `horizon` 过滤**：行数 = 信号数 × 观察期数，
   * 全量拉会拖慢整条链。`truncated` 显式回报是否被截断。
   */
  researchOutcomes: (id: number, horizon?: number, limit = 1000) => {
    const q = new URLSearchParams({ limit: String(limit) })
    if (horizon != null) q.set('horizon', String(horizon))
    return request<{
      items: ResearchOutcome[]
      truncated: boolean
    }>(`/api/research/experiments/${id}/outcomes?${q}`)
  },

  /**
   * 汇总指标（组合 × 观察期）。每行都带 `quality_notes`：幸存者偏差、缺基准、
   * 样本量不足、为何没有最大回撤 —— 这些**必须**和数字一起到页面上。
   */
  researchMetrics: (id: number) =>
    request<{ items: ResearchMetric[] }>(
      `/api/research/experiments/${id}/metrics`,
    ),

  //
  // 自动买卖策略 V1（模拟盘）。所有端点零联网 —— 只读写本地库的七张
  // 策略/交易表。两个 POST 也只落模拟账户，且后端没有任何定时器碰它们：
  // 一轮信号只在人显式调用时生成（幂等：同策略同信号日重跑回既有结果）。
  //

  /** 生成一轮信号并下单。日期留空 = 后端取最近一个六维快照日 */
  tradingSignalRun: (body: { date?: string; params?: Record<string, unknown> } = {}) =>
    request<TradingRunResult>('/api/trading/signal/run', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 执行待成交订单（D+1 开盘价虚拟撮合）。缺省从最早待执行日开始 */
  tradingExecute: (body: { exec_date?: string } = {}) =>
    request<TradingExecResult>('/api/trading/execute', {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /** 账户 / 持仓 / 待执行单数 / 最近一轮 run。首屏读它，零联网 */
  tradingStatus: () => request<TradingStatus>('/api/trading/status'),

  tradingRuns: (limit = 20) =>
    request<{ items: TradingRun[] }>(`/api/trading/runs?limit=${limit}`),

  /** 某轮的全部信号，**含 HOLD**（不交易的每只票都有理由留痕） */
  tradingSignals: (runId: number) =>
    request<{ items: TradingSignal[] }>(`/api/trading/signals?run_id=${runId}`),

  tradingOrders: (limit = 100) =>
    request<{ items: TradingOrder[] }>(`/api/trading/orders?limit=${limit}`),

  /** 参数默认值与版本口径 —— 页面只读展示，与后端同一份事实 */
  tradingParams: () => request<TradingParams>('/api/trading/params'),

  // ==== 实时纸面自动交易（rules.py + live_loop.py）====
  /** 全部规则 + 可选类型 + 版本号 */
  tradingRulesGet: () => request<TradingRulesPayload>('/api/trading/rules'),
  /** 整套替换（增删改后一次性提交）；未知类型/参数/越界 → 400 */
  tradingRulesPut: (rules: TradingRule[]) =>
    request<TradingRulesPayload>('/api/trading/rules', {
      method: 'PUT',
      body: JSON.stringify({ rules }),
    }),
  /** 恢复出厂默认规则 */
  tradingRulesReset: () =>
    request<TradingRulesPayload>('/api/trading/rules/reset', { method: 'POST' }),

  /** 启动实时循环（后台线程） */
  tradingLiveStart: (body: { interval?: number; scheduled?: boolean } = {}) =>
    request<TradingLiveState>('/api/trading/live/start', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  tradingLiveStop: () =>
    request<TradingLiveState>('/api/trading/live/stop', { method: 'POST' }),
  /** 手动跑一轮（不启动循环） */
  tradingLiveTick: () =>
    request<TradingLiveState>('/api/trading/live/tick', { method: 'POST' }),
  /** 面板状态：账户/持仓（带浮盈）/漏斗/买卖/重点观察 */
  tradingLiveState: () => request<TradingLiveState>('/api/trading/live/state'),
  tradingLiveTrades: (date?: string) =>
    request<{ date: string; items: TradingLiveTrade[] }>(
      `/api/trading/live/trades${date ? `?date=${date}` : ''}`),
  tradingLiveReset: () =>
    request<TradingLiveState>('/api/trading/live/reset', { method: 'POST' }),
}
