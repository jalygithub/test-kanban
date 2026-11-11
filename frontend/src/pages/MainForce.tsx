import { useCallback, useEffect, useMemo, useRef, useState, useImperativeHandle, type Ref } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  InputNumber,
  Modal,
  Progress,
  Select,
  Space,
  Spin,
  Statistic,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
  theme,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { StarFilled, StarOutlined, HistoryOutlined } from '@ant-design/icons'
import StockCode from '../components/StockCode'
import type { EChartsCoreOption } from 'echarts/core'
import { useEChart } from '../lib/useEChart'
import {
  api,
  ApiError,
  BACKFILL_BASE_START,
  type MainForceInflow,
  type MainForceMatrix,
  type MainForceMatrixRow,
  type MainForceProgress,
  type MainForceResponse,
  type MainForceRow,
  type MainForceState,
  type MainForceTop5Track,
  type MainForceTrackRow,
} from '../lib/api'
import { fmtDate, fmtPct, signColor, signType, yi } from '../lib/format'

const { Text, Paragraph } = Typography

/** 六维度固定顺序，与后端 mainforce.DIMENSIONS 一致 */
const DIMS = [
  { key: 'inflow_consistency', short: '流入持续', full: '主力资金持续流入' },
  { key: 'volume_up_price_flat', short: '放量不涨', full: '放量不涨' },
  { key: 'large_order_ratio', short: '大单占比', full: '大单净额占比' },
  { key: 'price_position_low', short: '低位', full: '股价位置低' },
  { key: 'turnover_moderate', short: '换手适中', full: '换手适中' },
  { key: 'chip_convergence', short: '筹码收敛', full: '筹码收敛' },
] as const

/** 列表「总流入」与点击弹框的窗口长度（对应后端 /main-force/inflow 的 days） */
const INFLOW_DAYS = 5

/**
 * 打分进度 → 0-100。**已完成的阶段 + 当前阶段内的比例**，不按「跑到第几阶段」
 * 直接除：那样进第 5 阶段就 4/7=57%、第 6 阶段 71%，然后在占八成墙钟时间的
 * 「预选与日线」上一动不动十几分钟 —— 比没有进度条更像卡死（后端 _PROG 那段
 * 注释讲的就是这个坑）。当前阶段不逐项计数（total=0）时按阶段边界算，即已完成
 * stage_no-1 个阶段，**不编中间值**。
 */
const progPercent = (p?: MainForceProgress | null): number => {
  if (!p || p.stages <= 0) return 0
  const inner = p.total > 0 ? p.done / p.total : 0
  const v = ((p.stage_no - 1 + inner) / p.stages) * 100
  return Math.min(100, Math.max(0, v))
}

/**
 * 打分进度的一行文字：`阶段 5/7 预选与日线 · 日线 120/300`。
 * 进度数据还没到（后端没给 progress）就说「准备中」，不冒充 0% 的具体阶段。
 */
const progLabel = (p?: MainForceProgress | null): string => {
  if (!p || p.stages <= 0) return '准备中'
  const head = `阶段 ${p.stage_no}/${p.stages} ${p.stage}`
  return p.total > 0 ? `${head} · ${p.done}/${p.total}` : head
}

/** 本地今天（YYYYMMDD）。用来判断漏跑的列是「今天」还是「已经过去」 */
const todayStr = () => {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}`
}

/** 各筛除原因的中文名 */
const DROP_LABEL: Record<string, string> = {
  st: 'ST',
  suspended: '停牌',
  illiquid: '成交额过低',
  limit_pool: '涨跌停池',
  new_listing: '次新股',
  mv_too_small: '流通市值<15亿',
  mv_too_large: '流通市值>2000亿',
  no_code: '无代码',
  duplicate: '重复行',
  zt_pool: '涨停池',
  multi_board: '连板≥2',
  gained_too_much: '20日涨幅≥40%',
}

const fmtDrops = (drops: Record<string, number>) =>
  Object.entries(drops)
    .map(([k, v]) => `${DROP_LABEL[k] ?? k} ${v}`)
    .join('、')

/**
 * 矩阵查询输入框：**按键只重渲染它自己**。
 *
 * 查询 state 若住在 MainForce 里，每敲一个键都会把整页连同那张几十列、
 * 上千格的矩阵表一起重渲染 —— 输入卡顿的根因。这里把即时文本关在组件
 * 内部，停手 800ms 才把词交给父级去过滤（父级整页因此只重画一次）；
 * 点 × 清空立即生效，不让人对着旧结果干等。
 */
function MatrixSearch({ onApply }: { onApply: (v: string) => void }) {
  const [text, setText] = useState('')
  const timer = useRef(0)

  const push = (v: string) => {
    setText(v)
    window.clearTimeout(timer.current)
    if (!v) {
      onApply(v)
      return
    }
    timer.current = window.setTimeout(() => onApply(v), 800)
  }

  // 只清定时器、不碰 setState —— 卸载时掐掉没落地的那次回调即可
  useEffect(() => () => window.clearTimeout(timer.current), [])

  return (
    <Input
      size="small"
      allowClear
      placeholder="搜索代码 / 名称"
      aria-label="按代码或名称过滤上榜矩阵"
      style={{ width: 190 }}
      value={text}
      onChange={(e) => push(e.target.value)}
    />
  )
}

/** 矩阵筛选条件：`null` = 不限。与查询词一样**纯前端** —— 整份矩阵已在手上 */
interface MatrixFilter {
  /** 上榜次数下限（`hits ≥`，窗口内上榜天数） */
  hitsMin: number | null
  /** 最好名次上限（`best_rank ≤`，窗口内曾取得的最好名次） */
  rankMax: number | null
  /** 当前连榜天数下限 */
  streakMin: number | null
  /** 最新收盘价下限（元，不复权） */
  priceMin: number | null
  /** 最新收盘价上限（元，不复权） */
  priceMax: number | null
}

const EMPTY_FILTER: MatrixFilter = {
  hitsMin: null,
  rankMax: null,
  streakMin: null,
  priceMin: null,
  priceMax: null,
}

const hasFilter = (f: MatrixFilter) =>
  f.hitsMin != null ||
  f.rankMax != null ||
  f.streakMin != null ||
  f.priceMin != null ||
  f.priceMax != null

/**
 * 矩阵行 + 两个派生值（筛选用，也直接进列渲染）。
 * 派生值口径（2026-11-07 拍板）：
 *   · `streak` 当前连榜 —— 从最新**已跑**交易日往回数的连续上榜天数，
 *     最新一天没上榜 = 0；漏跑列没跑任务，跳过不判（断点只在「跑了但没上榜」）。
 *   · `lastPrice` 最新收盘价 —— 从最新日期往回第一个 `prices` 值（不复权，
 *     后端 `daily_bar` 单一出处）；缺本地日线为 null，显 `-`，设了价格条件
 *     就排除这只票 —— **不编数**。
 */
type MatrixRowX = MainForceMatrixRow & {
  streak: number
  lastPrice: number | null
}

/**
 * 矩阵筛选条（上榜次数 / 最好名次 / 当前连榜 / 最新收盘价区间）。
 *
 * 与 `MatrixSearch` 同一个理由：即时值关在组件内部，停手 800ms 才回填父级 ——
 * 否则每敲一个键都会把整页连同几十列、上千格的矩阵重画一次，输入是卡的。
 * 点「清空」立即生效，不让人对着旧结果干等。
 */
function MatrixFilters({ onApply }: { onApply: (f: MatrixFilter) => void }) {
  const [f, setF] = useState<MatrixFilter>(EMPTY_FILTER)
  const timer = useRef(0)

  const push = (patch: Partial<MatrixFilter>) => {
    const next = { ...f, ...patch }
    setF(next)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => onApply(next), 800)
  }

  const clear = () => {
    setF(EMPTY_FILTER)
    window.clearTimeout(timer.current)
    onApply(EMPTY_FILTER)
  }

  // 只清定时器、不碰 setState —— 卸载时掐掉没落地的那次回调即可
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const lbl = { fontSize: 12 }

  return (
    <Space wrap size={8} style={{ marginBottom: 10 }}>
      <Text type="secondary" style={lbl}>
        筛选：
      </Text>
      <Tooltip title="窗口内上榜的天数，达到该天数才留下（漏跑日没跑任务，不计为上榜也不计为断点）">
        <Text type="secondary" style={lbl}>
          上榜次数 ≥
        </Text>
      </Tooltip>
      <InputNumber
        size="small"
        min={1}
        style={{ width: 64 }}
        value={f.hitsMin}
        placeholder="不限"
        aria-label="上榜次数下限"
        onChange={(v) => push({ hitsMin: v })}
      />
      <Tooltip title="窗口内曾取得的最好名次，进过前 N 名才留下">
        <Text type="secondary" style={lbl}>
          最好名次 ≤
        </Text>
      </Tooltip>
      <InputNumber
        size="small"
        min={1}
        style={{ width: 64 }}
        value={f.rankMax}
        placeholder="不限"
        aria-label="最好名次上限"
        onChange={(v) => push({ rankMax: v })}
      />
      <Tooltip title="从最近一个已跑交易日往回数、连续上榜的天数；最新一天没上榜计 0。漏跑列没跑任务，跳过不判">
        <Text type="secondary" style={lbl}>
          当前连榜 ≥
        </Text>
      </Tooltip>
      <InputNumber
        size="small"
        min={1}
        style={{ width: 64 }}
        value={f.streakMin}
        placeholder="不限"
        aria-label="当前连榜天数下限"
        onChange={(v) => push({ streakMin: v })}
      />
      <Text type="secondary" style={lbl}>
        天
      </Text>
      <Tooltip title="最近一个已跑交易日的收盘价（不复权）。缺本地日线的票只要设了价格条件就被排除，不编数">
        <Text type="secondary" style={lbl}>
          最新收盘价
        </Text>
      </Tooltip>
      <InputNumber
        size="small"
        min={0}
        style={{ width: 76 }}
        value={f.priceMin}
        placeholder="最低"
        aria-label="最新收盘价下限"
        onChange={(v) => push({ priceMin: v })}
      />
      <Text type="secondary" style={lbl}>
        ~
      </Text>
      <InputNumber
        size="small"
        min={0}
        style={{ width: 76 }}
        value={f.priceMax}
        placeholder="最高"
        aria-label="最新收盘价上限"
        onChange={(v) => push({ priceMax: v })}
      />
      <Text type="secondary" style={lbl}>
        元
      </Text>
      {hasFilter(f) && (
        <Button size="small" onClick={clear}>
          清空筛选
        </Button>
      )}
    </Space>
  )
}

/** 弹框的数据来源：矩阵 tab 还是前5跟踪 tab —— 两份 payload 同形状，画法共用 */
type TrendSource = 'matrix' | 'track'

/** 弹框只需要这两样：日期轴 + 漏跑日（矩阵与前5跟踪 payload 的公共交集） */
type TrendEnvelope = Pick<MainForceMatrix, 'dates' | 'missed_dates'>

/** 父级只拿这个句柄点代码开弹框 —— 开合 state 不上浮，整页不跟着重画 */
interface MatrixTrendHandle {
  /** `source` 标明从哪个 tab 点的，缺省矩阵 —— 决定去哪份 payload 回查行 */
  open: (code: string, source?: TrendSource) => void
}

/**
 * 股价走势 + 上榜名次弹框：矩阵 / 前5跟踪两个 tab 点代码的落点。
 *
 * **开合 state 关在组件内部**，与 `MatrixSearch` 同一个理由：MainForce 那棵树
 * 里有两张几十列、上千格的表，state 若住在父级，每开/关一次弹框就把整页连同
 * 两张表一起重渲染一遍（实测卡约 1 秒），看着像「点了没反应」。父级只经 ref
 * 调 `open()`，开合只重画本组件这一小块。
 *
 * 数据全取自已在手的两份 payload（cells + prices），打开零请求；`destroyOnHidden`
 * 让关闭即销毁画布，重开按新行重新量尺寸，不留上一只票的残影。
 * 行按 code + source 回查**当前**那份行：刷新后行没了就显空态，不显示过期快照。
 */
function MatrixTrendModal({
  matrix,
  rows,
  track,
  ref,
}: {
  matrix: TrendEnvelope | null
  rows: MatrixRowX[]
  track: MainForceTop5Track | null
  ref?: Ref<MatrixTrendHandle>
}) {
  const { token } = theme.useToken()
  /** 开的是哪只票、从哪个 tab 开的 —— 两个值一起存，缺一回查会串行 */
  const [pick, setPick] = useState<{ code: string; source: TrendSource } | null>(null)
  const open = useCallback(
    (code: string, source: TrendSource = 'matrix') => setPick({ code, source }),
    [],
  )
  useImperativeHandle(ref, () => ({ open }), [open])

  const onTrack = pick?.source === 'track'
  /** 两份 payload 同形状：轴/漏跑取 source 那份，行也回查 source 那份 */
  const env: TrendEnvelope | null = onTrack ? track : matrix
  const list: MainForceMatrixRow[] = onTrack ? (track?.rows ?? []) : rows
  /** 弹框里的那只票：按 code 回查当前行（行没了 = 空态） */
  const trendRow = pick ? (list.find((r) => r.code === pick.code) ?? null) : null

  /**
   * 走势图 option：图例两条、两条 yAxis ——
   *   · 股价（收盘，不复权）走左轴：漏跑日与缺本地日线的日子给 null 断线，
   *     **不插值不编数**，与格子里「缺价显 -」同口径；
   *   · 上榜名次走右轴且**反转**（第 1 名在最上）：只有上榜那天有点，
   *     空档 = 当天没上榜（跑了但没上），与矩阵格子同口径。
   * 颜色避开红绿涨跌语义：蓝 = 股价、橙 = 名次。
   */
  const trendOption = useMemo<EChartsCoreOption | null>(() => {
    if (!trendRow) return null
    const dates = env?.dates ?? []
    if (dates.length === 0) return null
    const missed = new Set(env?.missed_dates ?? [])
    return {
      animation: false,
      color: [token.colorPrimary, token.colorWarning],
      legend: { top: 0, data: ['股价（收盘）', '上榜名次'] },
      grid: { left: 58, right: 58, top: 36, bottom: 48 },
      tooltip: { trigger: 'axis' },
      xAxis: {
        type: 'category',
        // 矩阵日期是 YYYYMMDD 升序；轴上只留 MM/DD，重叠的自动藏
        data: dates.map((d) => `${d.slice(4, 6)}/${d.slice(6, 8)}`),
        axisLabel: { hideOverlap: true },
      },
      yAxis: [
        { type: 'value', scale: true, name: '收盘价' },
        {
          type: 'value',
          inverse: true,
          min: 1,
          name: '名次',
          axisLabel: { formatter: '#{value}' },
        },
      ],
      // 窗口默认 90 天、一屏挤不下：滚轮/双指缩放平移（组件已注册）
      dataZoom: [{ type: 'inside' }],
      series: [
        {
          name: '股价（收盘）',
          type: 'line',
          yAxisIndex: 0,
          showSymbol: false,
          connectNulls: false,
          data: dates.map((d) => (missed.has(d) ? null : (trendRow.prices?.[d] ?? null))),
          tooltip: { valueFormatter: (v: number | null) => (v == null ? '-' : `${v} 元`) },
        },
        {
          name: '上榜名次',
          type: 'line',
          yAxisIndex: 1,
          showSymbol: true,
          connectNulls: false,
          data: dates.map((d) => trendRow.cells[d]?.rank ?? null),
          tooltip: { valueFormatter: (v: number | null) => (v == null ? '未上榜' : `第 ${v} 名`) },
        },
      ],
    }
  }, [trendRow, env?.dates, env?.missed_dates, token])
  const trendChart = useEChart(trendOption)

  return (
    <Modal
      open={pick !== null}
      title={
        trendRow ? `${trendRow.name} ${trendRow.code} · 股价走势与上榜名次` : '股价走势与上榜名次'
      }
      footer={null}
      width={760}
      destroyOnHidden
      onCancel={() => setPick(null)}
    >
      {!trendRow ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description={
            onTrack
              ? '该股不在前5跟踪名单中（可能已刷新名单或换了数据）'
              : '该股不在当前矩阵中（可能已被筛选掉或换了查询范围）'
          }
        />
      ) : (
        <>
          <div ref={trendChart} style={{ height: 340, width: '100%' }} />
          <Text type="secondary" style={{ fontSize: 12 }}>
            收盘价不复权（daily_bar 单一出处）：漏跑日与缺本地日线的日子断线；
            名次只在上榜日有点，空档 = 当天没上榜。滚轮/双指可缩放平移。
            本结果仅用于研究与可视化，不构成任何投资建议。
          </Text>
        </>
      )}
    </Modal>
  )
}

export default function MainForce() {
  const { token } = theme.useToken()

  const [data, setData] = useState<MainForceResponse | null>(null)
  const [dates, setDates] = useState<string[]>([])
  const [activeDate, setActiveDate] = useState<string | undefined>()
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notReady, setNotReady] = useState(false)
  const [runState, setRunState] = useState<MainForceState | null>(null)
  /** 已关注的代码集合，决定每行星标是实心还是空心 */
  const [watched, setWatched] = useState<Set<string>>(new Set())
  const [tip, setTip] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  /** 近 N 日逐日主力净流入（列表「总流入」列与点击弹框共用） */
  const [inflow, setInflow] = useState<MainForceInflow | null>(null)
  /** 弹框里正在看逐日明细的行 */
  const [inflowRow, setInflowRow] = useState<MainForceRow | null>(null)

  useEffect(() => {
    void api
      .watchlist()
      .then((r) => setWatched(new Set(r.items.map((i) => i.code))))
      .catch(() => undefined)
  }, [])

  const load = useCallback(async (date?: string) => {
    setLoading(true)
    setError(null)
    try {
      const res = await api.mainForce(date)
      setData(res)
      setNotReady(false)
      setActiveDate(res.date)
    } catch (e) {
      const err = e as ApiError
      if (err.status === 404) {
        setNotReady(true)
        setData(null)
      } else {
        setError(err.message)
      }
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** 三个视图：单日榜单 / 跨日上榜矩阵 / 前5跟踪 */
  const [tab, setTab] = useState<'list' | 'matrix' | 'track'>('list')
  const [matrix, setMatrix] = useState<MainForceMatrix | null>(null)
  /** 初始即加载态：首开矩阵页时 matrix 还是 null，靠它撑起 Spin */
  const [matrixLoading, setMatrixLoading] = useState(true)
  const [matrixError, setMatrixError] = useState<string | null>(null)
  const [matrixDays, setMatrixDays] = useState(90)
  /**
   * 矩阵内按代码/名称过滤的**已落地**词（纯前端，数据已在手上，不重打接口）。
   * 即时输入的文本不在这儿 —— 那份关在 `MatrixSearch` 组件里：state 若住在这
   * 一层，每敲一个键都会把整页连同几十列的矩阵表一起重渲染，输入就是卡的。
   * 这里只在停手 800ms 后被回填一次，整页也只跟着重画这一次。
   */
  const [matrixQuery, setMatrixQuery] = useState('')
  /** 过滤后行数会变，页码得跟着回 1，否则停在旧页上看着像「没结果」 */
  const [matrixPage, setMatrixPage] = useState(1)
  /**
   * 矩阵筛选条件（上榜次数 / 最好名次 / 当前连榜 / 最新收盘价）：
   * 同样纯前端、同样由 `MatrixFilters` 停手 800ms 后回填一次，
   * 改条件也要把页码打回 1。
   */
  const [matrixFilter, setMatrixFilter] = useState<MatrixFilter>(EMPTY_FILTER)
  /**
   * 矩阵里点代码打开的走势弹框（2026-11-10 换掉「拉起同花顺」）。
   * **开合 state 关在 `MatrixTrendModal` 内部**，与 `MatrixSearch` 同一个理由：
   * 住在这一层，每开/关一次弹框就把整页连同两张几十列的表一起重渲染一遍，
   * 实测卡约 1 秒，看着像「点了没反应」。这里只留 ref，点代码直调子组件的
   * open()，整页不重画；行按 code 回查、刷新后行没了显空态的语义在子组件里。
   */
  const trendRef = useRef<MatrixTrendHandle>(null)

  const loadMatrix = useCallback(async (days: number) => {
    try {
      const res = await api.mainForceMatrix(days)
      setMatrix(res)
      setMatrixError(null)
    } catch (e) {
      setMatrixError((e as Error).message)
    }
    setMatrixLoading(false)
  }, [])

  /**
   * 取一次矩阵并点亮 Spin。刻意**不放在 effect 里** —— effect 中同步 setState
   * 会新增一条 oxlint 的 react(set-state-in-effect)（本仓库只保留既有 6 条）。
   * 改由事件回调触发：切到该 Tab、改范围、重试，以及打分跑完的轮询回调。
   */
  const ensureMatrix = useCallback(
    (days: number) => {
      setMatrixLoading(true)
      void loadMatrix(days)
    },
    [loadMatrix],
  )

  /**
   * 前5跟踪名单（新 tab，2026-11-11）：后端扫 main_force 快照推导，
   * 零联网读本地。与矩阵同套 ensure 语义 —— 切到该 Tab、打分跑完、手动
   * 刷新时取一次，取之前用 loading 撑 Spin；**不放 effect 里**（同步
   * setState 会新增一条 oxlint 的 set-state-in-effect）。
   */
  const [track, setTrack] = useState<MainForceTop5Track | null>(null)
  const [trackLoading, setTrackLoading] = useState(false)
  const [trackError, setTrackError] = useState<string | null>(null)

  const loadTrack = useCallback(async () => {
    try {
      const res = await api.mainForceTop5Track()
      setTrack(res)
      setTrackError(null)
    } catch (e) {
      setTrackError((e as Error).message)
    }
    setTrackLoading(false)
  }, [])

  const ensureTrack = useCallback(() => {
    setTrackLoading(true)
    void loadTrack()
  }, [loadTrack])

  /**
   * 矩阵行：先算两个派生值（当前连榜、最新收盘价），再按「代码/名称关键字 +
   * 筛选条件」过滤。纯前端做 —— 整份矩阵一次已取回，为一个词再打一次接口
   * 没有意义；漏跑/补跑那些列的语义不受过滤影响，只筛「哪些票进表」。
   * 关键词与筛选值都是**已落地**的：`MatrixSearch` / `MatrixFilters` 停手
   * 800ms 才回填，输入过程中不重算。
   */
  const matrixRows = useMemo<MatrixRowX[]>(() => {
    const rows = matrix?.rows ?? []
    const ran = matrix?.ran_dates ?? []
    const dates = matrix?.dates ?? []
    const latestRan = ran[ran.length - 1] ?? ''
    const q = matrixQuery.trim()
    const lower = q.toLowerCase()
    const f = matrixFilter

    return rows
      .map((r) => {
        // 当前连榜：从最新已跑交易日往回数，「跑了但没上榜」即断；
        // 漏跑日不在 ran_dates 里，天然跳过 —— 没跑不判，不当断点
        let streak = 0
        if (latestRan) {
          for (let i = ran.length - 1; i >= 0; i--) {
            if (r.cells[ran[i]]) streak++
            else break
          }
        }
        // 最新收盘价：从最新日期往回找第一个有价的；都没价 = null（显 -）
        let lastPrice: number | null = null
        for (let i = dates.length - 1; i >= 0; i--) {
          const p = r.prices[dates[i]]
          if (p != null) {
            lastPrice = p
            break
          }
        }
        return { ...r, streak, lastPrice }
      })
      .filter((r) => {
        if (q && !(r.code.toLowerCase().includes(lower) || r.name.includes(q)))
          return false
        if (f.hitsMin != null && r.hits < f.hitsMin) return false
        if (f.rankMax != null && r.best_rank > f.rankMax) return false
        if (f.streakMin != null && r.streak < f.streakMin) return false
        // 价格条件：缺价的票无从判断，直接排除（不编数）
        if (f.priceMin != null || f.priceMax != null) {
          if (r.lastPrice == null) return false
          if (f.priceMin != null && r.lastPrice < f.priceMin) return false
          if (f.priceMax != null && r.lastPrice > f.priceMax) return false
        }
        return true
      })
  }, [matrix?.rows, matrix?.ran_dates, matrix?.dates, matrixQuery, matrixFilter])

  useEffect(() => {
    void api
      .mainForceDates()
      .then((r) => setDates(r.dates))
      .catch(() => undefined)
  }, [data?.date])

  useEffect(() => {
    void api.mainForceState().then(setRunState).catch(() => undefined)
  }, [])

  const running = runState?.running ?? false
  /** 正在补跑哪一天（空串 = 未指定日期，即跑最近交易日） */
  const runningDate = runState?.date ?? ''

  // 打分在后台线程跑，3s 轮询一次，跑完自动刷新结果
  useEffect(() => {
    if (!running) return
    const id = window.setInterval(() => {
      void api
        .mainForceState()
        .then((s) => {
          setRunState(s)
          if (!s.running && !s.error) {
            void load()
            // 矩阵要单独取一次：同一天重算时 data.date 不变，
            // 靠日期变化触发不到，否则重算完矩阵还是旧的。
            ensureMatrix(matrixDays)
            // 跟踪名单同理：新打进前5的票要进名单，已进的截止日要顺延
            ensureTrack()
          }
        })
        .catch(() => undefined)
    }, 3000)
    return () => window.clearInterval(id)
  }, [running, load, ensureMatrix, matrixDays, ensureTrack])

  /**
   * 起一轮打分。`date` 指定补跑某一天（矩阵里漏跑的列），不传就是跑最近交易日。
   * `setRunState` 必须把 date 一并记上 —— 否则补跑期间分不清在跑哪一天，
   * 漏跑列表头那个按钮的 loading 就点不亮，其他日期也不会正确进入禁用态。
   */
  const recompute = useCallback(async (date?: string) => {
    try {
      await api.mainForceRecompute(date)
      setRunState({
        running: true,
        error: '',
        started_at: '',
        finished_at: '',
        date: date ?? '',
        output: 0,
        elapsed_sec: null,
        degraded: [],
        // 后端还没轮询回来，进度先给 null —— 显示「准备中」，别编一个 0% 阶段名
        progress: null,
      })
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  /** 逐日主力净流入：先取一次；backfilling=true 表示后端回补线程在跑，轮询到补齐 */
  const inflowCodes = useMemo(() => (data?.rows ?? []).map((r) => r.code), [data?.rows])

  useEffect(() => {
    const date = data?.date
    if (!date || inflowCodes.length === 0) return

    let cancelled = false
    let timer = 0
    let tries = 0

    const tick = async () => {
      try {
        const res = await api.mainForceInflow(date, inflowCodes, INFLOW_DAYS)
        if (cancelled) return
        setInflow(res)
        // missing>0 且 backfilling=true = 后端还在逐只补东财 daykline，1.5s 后再来；
        // 40 次（约 1 分钟）封顶，免得源挂掉后前端无限轮询。
        // backfilling=false（空闲，或 STOCK_INFLOW_BACKFILL=false 关掉了）就停 ——
        // 只看 missing 会在内网下一直空转，每轮还真的开出一次外网回补线程。
        if (res.missing > 0 && res.backfilling && tries < 40) {
          tries += 1
          timer = window.setTimeout(() => void tick(), 1500)
        }
      } catch {
        // 逐日明细是加分项，拿不到就让列表列显示「-」，不影响主表
      }
    }

    void tick()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [data?.date, inflowCodes])

  /** 切到别的打分日时，上一份明细属于旧日期，直接当没有 */
  const inflowNow =
    inflow && inflow.date === data?.date && inflow.days === INFLOW_DAYS ? inflow : null

  const weights = useMemo(() => data?.params.weights ?? {}, [data?.params.weights])
  const model = data?.model
  const stats = data?.stats
  const degraded = model?.degraded ?? []
  /** 阶段 7 第 8 条：当天输入域的采集体检。老快照没有这个字段 = 没查过，不告警 */
  const dq = model?.data_quality
  const dqMissing = dq?.missing ?? []

  const toggleWatch = useCallback(
    async (row: MainForceRow) => {
      if (!data?.date) return
      const on = watched.has(row.code)
      try {
        const res = on
          ? await api.watchRemove(row.code)
          : await api.watchAdd({
              code: row.code,
              name: row.name,
              source_date: data.date,
              industry: row.industry ?? '',
              rank: row.rank,
              score: row.score,
              price: row.price ?? undefined,
              snapshot: { reasons: row.reasons, dims: row.dims },
            })
        setWatched(new Set(res.items.map((i) => i.code)))
        setTip({
          type: 'success',
          text: on
            ? `已取消关注 ${row.name}`
            : `已关注 ${row.name}，走势从 ${fmtDate(data.date)} 起算，见左侧「关注股票」`,
        })
      } catch (e) {
        setTip({ type: 'error', text: `${on ? '取消关注' : '关注'}失败：${(e as Error).message}` })
      }
    },
    [watched, data],
  )

  // ------------------------------------------------------- 手动补历史日线
  //
  // 全市场跑批一轮要几个小时，页面上想看某一只的历史不该等它 —— 这里给每行
  // 一个单票入口，走后端同一个 `/api/pipeline/backfill`（`items=[code]`）。
  // `only_gaps=true`：库里已有首末日覆盖时本地挖洞、只补缺的那几天，一天不
  // 缺就一次请求都不发；`ON CONFLICT DO NOTHING` 保证已有行情一行不被改写。
  const [pulling, setPulling] = useState<string | null>(null)

  const pullHistory = useCallback(async (row: MainForceRow) => {
    setPulling(row.code)
    try {
      const r = await api.backfillOne(row.code)
      if (r.failed > 0) {
        const p = r.problems[0]
        setTip({
          type: 'error',
          text:
            `补 ${row.name} 历史日线失败：${p?.error ?? '未知错误'}` +
            (p?.source ? `（${p.source}）` : '') +
            '。东财常态拒连时可稍后重试，会自动走腾讯备源。',
        })
      } else if (r.inserted > 0) {
        setTip({
          type: 'success',
          text: `已补 ${row.name} ${row.code}：新增 ${r.inserted} 行日线（重复日期已跳过，已有数据未覆盖）`,
        })
      } else if (r.skipped > 0) {
        setTip({
          type: 'success',
          text: `${row.name} ${row.code} 的 ${BACKFILL_BASE_START} 至今日线已齐，未发请求`,
        })
      } else {
        // 源回了 0 行且本地也没判「已齐」——多为源端滞后，如实说没有，不编行数
        setTip({
          type: 'error',
          text: `源端未返回 ${row.code} 的日线（可能数据滞后或代码已退市），本地未新增`,
        })
      }
    } catch (e) {
      setTip({ type: 'error', text: `补历史日线失败：${(e as Error).message}` })
    } finally {
      setPulling(null)
    }
  }, [])

  const columns: ColumnsType<MainForceRow> = useMemo(() => {
    /** 缺数据的行排到最后；用有限值而不是 -Infinity，否则两行都缺会算出 NaN */
    const totalOf = (code: string) => inflowNow?.items[code]?.total ?? -1e18

    /** 正在补历史日线的那只（单票动作，同一时刻只允许一只在跑） */
    const pullCol: ColumnsType<MainForceRow> = [
      {
        title: '',
        key: 'pull',
        width: 52,
        align: 'center',
        render: (_: unknown, row: MainForceRow) => (
          <Tooltip
            title={
              `联网补 ${row.name} 的历史日线（${BACKFILL_BASE_START} 起）进本地库：` +
              '只补缺的日期，已有行情一行都不覆盖。与全市场跑批同一张表同一口径。'
            }
          >
            <Button
              type="text"
              size="small"
              aria-label="补历史日线"
              icon={<HistoryOutlined />}
              loading={pulling === row.code}
              disabled={pulling !== null && pulling !== row.code}
              onClick={() => void pullHistory(row)}
            />
          </Tooltip>
        ),
      },
    ]

    const dimCols: ColumnsType<MainForceRow> = DIMS.map((d) => ({
      title: (
        <Tooltip title={`${d.full} · 权重 ${weights[d.key] ?? '-'}`}>
          <span>{d.short}</span>
        </Tooltip>
      ),
      key: d.key,
      width: 74,
      align: 'center',
      sorter: (a, b) => (a.dims[d.key]?.score ?? -1) - (b.dims[d.key]?.score ?? -1),
      render: (_: unknown, row: MainForceRow) => {
        const dim = row.dims[d.key]
        if (!dim) {
          return (
            <Tooltip title="该维度数据缺失，权重已按剩余维度重归一化">
              <Text type="secondary">-</Text>
            </Tooltip>
          )
        }
        const color =
          dim.score >= 75
            ? token.colorSuccess
            : dim.score >= 50
              ? token.colorInfo
              : token.colorTextTertiary
        return <Text style={{ color, fontVariantNumeric: 'tabular-nums' }}>{dim.score.toFixed(0)}</Text>
      },
    }))

    return [
      {
        title: '',
        key: 'watch',
        width: 52,
        align: 'center',
        render: (_: unknown, row: MainForceRow) => {
          const on = watched.has(row.code)
          return (
            <Tooltip
              title={
                on
                  ? '已关注 · 点击取消'
                  : `关注后在「关注股票」里跟踪它从 ${fmtDate(data?.date)} 至今的走势`
              }
            >
              <Button
                type="text"
                size="small"
                aria-label={on ? '取消关注' : '关注'}
                icon={
                  on ? <StarFilled style={{ color: token.colorWarning }} /> : <StarOutlined />
                }
                onClick={() => void toggleWatch(row)}
              />
            </Tooltip>
          )
        },
      },
      // 单票补历史日线的入口，长在星标旁边 —— 想补哪只，手就在哪只上
      ...pullCol,
      { title: '#', dataIndex: 'rank', width: 52, align: 'center' },
      {
        title: '代码',
        dataIndex: 'code',
        width: 88,
        render: (v: string, row) => <StockCode code={v} name={row.name} />,
      },
      {
        title: '名称',
        dataIndex: 'name',
        width: 110,
        ellipsis: true,
        render: (v: string, row) => (
          <Tooltip title={`${v} · ${row.industry || '行业未知'}`}>
            <span>{v}</span>
          </Tooltip>
        ),
      },
      {
        title: '综合分',
        dataIndex: 'score',
        width: 136,
        sorter: (a, b) => a.score - b.score,
        defaultSortOrder: 'descend',
        render: (v: number) => (
          <Space size={6}>
            <Progress
              percent={v}
              size="small"
              showInfo={false}
              style={{ width: 64 }}
              strokeColor={v >= 70 ? token.colorSuccess : token.colorPrimary}
            />
            <Text strong>{v.toFixed(1)}</Text>
          </Space>
        ),
      },
      ...dimCols,
      {
        title: '现价',
        dataIndex: 'price',
        width: 82,
        align: 'right',
        sorter: (a, b) => (a.price ?? 0) - (b.price ?? 0),
        render: (v?: number | null) => (v == null ? '-' : v.toFixed(2)),
      },
      {
        title: '涨幅%',
        dataIndex: 'pct_chg',
        width: 88,
        align: 'right',
        sorter: (a, b) => (a.pct_chg ?? 0) - (b.pct_chg ?? 0),
        render: (v?: number | null) =>
          v == null ? (
            '-'
          ) : (
            <Text type={signType(v)}>{v.toFixed(2)}</Text>
          ),
      },
      {
        title: '换手%',
        dataIndex: 'turnover',
        width: 78,
        align: 'right',
        render: (v?: number | null) => (v == null ? '-' : v.toFixed(1)),
      },
      {
        title: '流通市值(亿)',
        dataIndex: 'float_mv',
        width: 108,
        align: 'right',
        sorter: (a, b) => (a.float_mv ?? 0) - (b.float_mv ?? 0),
        render: yi,
      },
      {
        title: (
          <Tooltip title={`近${INFLOW_DAYS}个交易日主力净流入合计，点击查看逐日明细`}>
            <span>近{INFLOW_DAYS}日总流入(亿)</span>
          </Tooltip>
        ),
        key: 'inflow_total',
        width: 142,
        align: 'right',
        sorter: (a, b) => totalOf(a.code) - totalOf(b.code),
        render: (_: unknown, row: MainForceRow) => {
          const item = inflowNow?.items[row.code]
          if (!item || item.days.length === 0) {
            return (
              <Tooltip
                title={
                  inflowNow?.backfilling
                    ? '逐日资金流回补中，补齐后会自动刷新'
                    : (inflowNow?.missing ?? 0) > 0
                      ? '缺逐日资金流，且回补未启用或冷却中（内网下东财取不到，' +
                        '关掉见 backend/.env 的 STOCK_INFLOW_BACKFILL）'
                      : '暂无逐日资金流数据'
                }
              >
                <Text type="secondary">-</Text>
              </Tooltip>
            )
          }
          const value = item.total / 1e8
          return (
            <Tooltip title={`点击展开 ${item.days.length} 个交易日的逐日明细`}>
              <Button
                type="link"
                size="small"
                style={{ padding: 0, height: 'auto', fontVariantNumeric: 'tabular-nums' }}
                onClick={() => setInflowRow(row)}
              >
                <span style={{ color: signColor(value, token) }}>
                  {value.toFixed(2)}
                </span>
              </Button>
            </Tooltip>
          )
        },
      },
      {
        title: '入选理由',
        key: 'reasons',
        width: 300,
        ellipsis: true,
        render: (_: unknown, row) =>
          row.reasons.length ? (
            <Tooltip
              title={row.reasons.map((r) => (
                <div key={r}>· {r}</div>
              ))}
            >
              <Text type="secondary">{row.reasons.join('；')}</Text>
            </Tooltip>
          ) : (
            <Text type="secondary">-</Text>
          ),
      },
    ]
  }, [weights, token, watched, toggleWatch, data?.date, inflowNow, pulling, pullHistory])

  const expandedRow = (row: MainForceRow) => (
    <div style={{ padding: '4px 8px' }}>
      <Paragraph style={{ marginBottom: 6 }}>
        <Text strong>入选理由（按贡献度排序）</Text>
      </Paragraph>
      {row.reasons.length ? (
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {row.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      ) : (
        <Text type="secondary">各维度均未达到入选阈值，仅因加权总分进入名单</Text>
      )}
      <div style={{ marginTop: 10, display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {DIMS.map((d) => {
          const dim = row.dims[d.key]
          if (!dim) return null
          const contribution = (dim.score * (dim.weight ?? 0)) / (model?.total_weight || 100)
          return (
            <Tag key={d.key} style={{ marginInlineEnd: 0 }}>
              {d.full}：{dim.score.toFixed(0)} 分 × 权重 {dim.weight ?? '-'} → 贡献 {contribution.toFixed(1)}
            </Tag>
          )
        })}
      </div>
      <div style={{ marginTop: 8 }}>
        <Text type="secondary">
          数据覆盖度 {(row.coverage * 100).toFixed(0)}% · 上市日期 {fmtDate(row.list_date) || '未知'} ·
          成交额 {yi(row.amount)} 亿
        </Text>
      </div>
    </div>
  )

  // HMR 探针：确认 Fast Refresh 重挂载时榜单是否瞬时清空（勿删，验证后还原）
  const header = (
    <Space orientation="vertical" size={8} style={{ width: '100%' }}>
      {error && <Alert type="error" showIcon title={error} />}
      {tip && (
        <Alert type={tip.type} showIcon title={tip.text} closable={{ onClose: () => setTip(null) }} />
      )}
      {runState?.error && (
        <Alert type="error" showIcon title={`重算失败：${runState.error}`} />
      )}
      {running && (
        <Alert
          type="info"
          showIcon
          title={
            runState?.date
              ? `正在打分 ${fmtDate(runState.date)} 的数据`
              : '打分进行中'
          }
          description={
            <>
              {runState?.progress && (
                <Progress
                  percent={progPercent(runState.progress)}
                  size="small"
                  style={{ maxWidth: 320 }}
                />
              )}
              <div>
                <Text type="secondary">{progLabel(runState?.progress)}</Text>
                {` · 单轮需逐只拉取约 ${data?.params.preselect_top ?? 300} 只日线，约 10-15 分钟，完成后自动刷新。此结果为研究性排序，不构成投资建议。`}
              </div>
            </>
          }
        />
      )}
      {degraded.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title="部分维度已降级"
          description={
            <>
              以下维度数据覆盖不足，已按剩余维度重归一化权重（多为东财资金流接口不可用）：
              {degraded.map((k) => DIMS.find((d) => d.key === k)?.full ?? k).join('、')}
              。综合分在同批结果内仍可比，但绝对值低于满维度状态。
            </>
          }
        />
      )}
      {dq?.state === 'incomplete' && (
        <Alert
          type="warning"
          showIcon
          title={`本榜基于不完整数据（${fmtDate(dqMissing[0]?.trade_date)}）`}
          description={
            <>
              以下采集域当天未成或数据不全：
              {dqMissing
                .map(
                  (m) =>
                    `${m.domain} ${m.status ?? '—'}` +
                    (m.coverage_rate == null
                      ? ''
                      : `（覆盖 ${(m.coverage_rate * 100).toFixed(0)}%）`),
                )
                .join('、')}
              。排名仍按当日实际可得数据计算，缺口域的证据缺失，请连同此标记一并解读
              —— 它不是选股信号，也不构成任何投资建议。
            </>
          }
        />
      )}
      {stats && (
        <Space wrap size={16}>
          <Statistic title="全市场" value={stats.universe} />
          <Statistic title="粗筛后" value={stats.coarse_kept} />
          <Tooltip title={`粗筛筛除：${fmtDrops(stats.coarse_dropped)}`}>
            <Statistic title="拉取日线" value={stats.preselected} />
          </Tooltip>
          {stats.preselected - stats.histories > 0 && (
            <Tooltip
              title={`日线拉取失败 ${stats.preselected - stats.histories} 只，` +
                `成功率 ${(stats.histories / Math.max(stats.preselected, 1) * 100).toFixed(1)}%。` +
                '重试后仍失败会直接剔除，否则该股算不出「放量不涨 / 位置 / 筹码」，只能给半残分数'}
            >
              <Statistic
                title="日线失败"
                value={stats.preselected - stats.histories}
                styles={{ content: { color: token.colorWarning } }}
              />
            </Tooltip>
          )}
          <Tooltip title={`负面筛除：${fmtDrops(stats.negative_dropped)}`}>
            <Statistic title="完成打分" value={stats.scored} />
          </Tooltip>
          <Statistic title="输出 Top" value={stats.output} styles={{ content: { color: token.colorSuccess } }} />
          <Statistic
            title="已关注"
            value={watched.size}
            styles={{ content: watched.size ? { color: token.colorWarning } : undefined }}
          />
          <Statistic title="耗时(秒)" value={data?.elapsed_sec ?? 0} />
        </Space>
      )}
      {inflowNow && inflowNow.missing > 0 && inflowNow.backfilling && (
        <Space size={8}>
          <Tag color="processing">逐日资金流回补中</Tag>
          <Text type="secondary" style={{ fontSize: 12 }}>
            还差 {inflowNow.missing} 只补齐近{INFLOW_DAYS}日明细，后端正在逐只拉取东财
            daykline（约每秒 1 只），补齐后本列自动刷新。
          </Text>
        </Space>
      )}
      <Space wrap size={8}>
        <Text type="secondary">维度权重：</Text>
        {DIMS.map((d) => (
          <Tag key={d.key} style={{ marginInlineEnd: 0 }}>
            {d.short} {weights[d.key] ?? '-'}
          </Tag>
        ))}
        <Text type="secondary" style={{ marginLeft: 8 }}>
          粗筛：剔除 ST / 停牌 / 次新({stats ? data?.params.min_listed_days : 60}个交易日) / 流通市值
          {yi(data?.params.float_mv_min)}-{yi(data?.params.float_mv_max)}亿 ·
          负面过滤：涨停池、连板≥{data?.params.max_boards ?? 2}、20日涨幅≥
          {data?.params.max_gain_20d ?? 40}%
        </Text>
      </Space>
    </Space>
  )

  /* ------------------------------------------------------------------ *
   * 上榜矩阵：行=股票、列=交易日，格子=当天名次
   *
   * 三种格子必须能一眼分开 —— 混了就会把「这天压根没跑任务」误读成
   * 「跑了但这只没上榜」，而这俩在数据里都是"没有值"：
   *   · 漏跑日期 → 整列上底色，表头标「未跑」，不给价
   *   · 跑过没上榜 → 只写当日收盘价（缺本地日线显 `-`）
   *   · 上榜 → 写名次，名次下方同写收盘价
   * 收盘价取 `daily_bar`（不复权），由后端 `prices` 一次给全，
   * 前端不自己算日期、也不自己去补数。
   * ------------------------------------------------------------------ */
  const matrixColumns = useMemo<ColumnsType<MatrixRowX>>(() => {
    const missed = new Set(matrix?.missed_dates ?? [])
    // 涨/跌停池过期的漏跑日：后端已判 400，按钮撤掉换成「不可补」标注
    const unfillable = new Set(matrix?.unfillable_dates ?? [])

    const base: ColumnsType<MatrixRowX> = [
      {
        title: '代码',
        key: 'code',
        width: 104,
        fixed: 'left',
        // 点代码不再拉起同花顺（2026-11-10 换掉）：改开本页走势弹框，
        // 数据全在矩阵 payload（cells + prices）里，零额外请求。
        // stopPropagation：别把点击冒泡给表格行。
        render: (_: unknown, row) => (
          <Tooltip title={`打开 ${row.name || row.code} 的股价走势与上榜名次`}>
            <a
              className="stock-code"
              onClick={(e) => {
                e.stopPropagation()
                trendRef.current?.open(row.code)
              }}
            >
              {row.code}
            </a>
          </Tooltip>
        ),
      },
      {
        title: '名称',
        key: 'name',
        width: 110,
        fixed: 'left',
        ellipsis: true,
        render: (_: unknown, row) => row.name || '-',
      },
      {
        title: '上榜次数',
        dataIndex: 'hits',
        width: 94,
        align: 'center',
        sorter: (a, b) => a.hits - b.hits,
        defaultSortOrder: 'descend',
        render: (v: number) => <Text strong>{v}</Text>,
      },
      {
        title: (
          <Tooltip title="从最近一个已跑交易日往回数、连续上榜的天数；最新一天没上榜显 —。漏跑列没跑任务，跳过不判">
            <span>当前连榜</span>
          </Tooltip>
        ),
        key: 'streak',
        width: 92,
        align: 'center',
        sorter: (a, b) => a.streak - b.streak,
        render: (_: unknown, row) =>
          row.streak > 0 ? (
            <Text strong>{row.streak} 天</Text>
          ) : (
            <Text type="secondary">—</Text>
          ),
      },
      {
        title: '最好名次',
        dataIndex: 'best_rank',
        width: 94,
        align: 'center',
        sorter: (a, b) => a.best_rank - b.best_rank,
        render: (v: number) => `#${v}`,
      },
      {
        title: '最近上榜',
        dataIndex: 'last_date',
        width: 106,
        align: 'center',
        render: (v: string) => fmtDate(v),
      },
      {
        title: (
          <Tooltip title="最近一个已跑交易日的收盘价（不复权，daily_bar 单一出处）；缺本地日线显 -">
            <span>最新价</span>
          </Tooltip>
        ),
        key: 'last_price',
        width: 88,
        align: 'center',
        // 没价的按 -1 排在最前/最后都行 —— 与「缺数据显示 -」一致，不编值
        sorter: (a, b) => (a.lastPrice ?? -1) - (b.lastPrice ?? -1),
        render: (_: unknown, row) =>
          row.lastPrice == null ? (
            <Text type="secondary">-</Text>
          ) : (
            <Text style={{ fontVariantNumeric: 'tabular-nums' }}>
              {row.lastPrice.toFixed(2)}
            </Text>
          ),
      },
    ]

    const today = todayStr()

    // 列按日期**降序**（最新在最左）：矩阵是往右翻旧账的长表，打开就该先看见
    // 最近的。后端 dates 保持升序不动 —— 聚合侧靠升序遍历让 name/industry 保留
    // 最新叫法，API 契约也由基线钉死，展示顺序在这里反过来。先展开再 reverse：
    // 直接 reverse 会原地改 React 状态里的数组。
    const orderedDates = [...(matrix?.dates ?? [])].reverse()

    const dateCols: ColumnsType<MatrixRowX> = orderedDates.map((d) => {
      const isMissed = missed.has(d)
      // 漏跑的那天若已经过去，补跑会混入当前行情快照，tooltip 得讲清楚
      const isPast = d < today
      const isUnfillable = isMissed && unfillable.has(d)

      return {
        key: d,
        // 补跑按钮就长在缺数据那一列的表头上 —— 缺哪天，按钮出现在哪天旁边
        title: isMissed ? (
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              gap: 4,
              lineHeight: 1.25,
            }}
          >
            <span>
              {d.slice(4, 6)}/{d.slice(6, 8)}
            </span>
            {isUnfillable ? (
              // 涨/跌停池过了东财 30 天窗口、本地也没快照 —— 永久补不上，
              // 撤按钮换成标注，别让人点了等 4 分钟再吃一个 400
              <Tooltip
                title={`${fmtDate(d)} 的涨/跌停池已过东财 30 天保留期，` +
                  '本地也没有当日快照 —— 池子是收盘当场采的，事后取不回，该日永久不可补。'}
              >
                <Text type="secondary" style={{ fontSize: 11 }}>
                  不可补
                </Text>
              </Tooltip>
            ) : (
              <Tooltip
                title={
                  isPast
                    ? `补跑 ${fmtDate(d)}，约 12-15 分钟。⚠ 该日已过去：` +
                      '打分维度与输出行情已按该日日线还原，但粗筛的停牌/ST/成交额、' +
                      '候选池预选（选出哪 300 只）、流通市值仍取自当前快照，' +
                      '与当天实跑不完全可比。内网下外网接口会被拦截记录。'
                    : `跑 ${fmtDate(d)} 的打分，约 12-15 分钟。` +
                      '内网环境下外网行情接口会被安全设备拦截并记录。'
                }
              >
                <Button
                  size="small"
                  type="primary"
                  loading={running && runningDate === d}
                  disabled={running}
                  onClick={() => void recompute(d)}
                  style={{ height: 20, padding: '0 6px', fontSize: 11 }}
                >
                  补跑
                </Button>
              </Tooltip>
            )}
          </div>
        ) : (
          <div style={{ lineHeight: 1.25 }}>
            {d.slice(4, 6)}/{d.slice(6, 8)}
          </div>
        ),
        // 漏跑列要放得下「日期 + 按钮」
        width: isMissed ? 128 : 78,
        align: 'center',
        onCell: isMissed
          ? () => ({ style: { backgroundColor: token.colorFillQuaternary } })
          : undefined,
        render: (_: unknown, row: MatrixRowX) => {
          // 漏跑列没有打分、也不给价（整块走「未跑」底色）；跑过的列一律
          // 给当日收盘价，本地日线缺就显 `-` —— 「没上榜」和「没价」是
          // 两回事，不能都留白混过去
          if (isMissed) return null
          const price = row.prices?.[d]
          const priceText = price == null ? '-' : price.toFixed(2)
          const priceLine = (
            <Text
              type="secondary"
              style={{ fontSize: 11, lineHeight: 1, fontVariantNumeric: 'tabular-nums' }}
            >
              {priceText}
            </Text>
          )
          const cell = row.cells[d]
          if (!cell) return priceLine
          return (
            <Tooltip
              title={`${fmtDate(d)} 当天第 ${cell.rank} 名${
                cell.score != null ? ` · 综合分 ${cell.score}` : ''
              } · 收盘 ${priceText}`}
            >
              <div
                style={{
                  display: 'inline-flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 2,
                }}
              >
                <Tag
                  color="processing"
                  style={{ marginInlineEnd: 0, fontVariantNumeric: 'tabular-nums' }}
                >
                  #{cell.rank}
                </Tag>
                {priceLine}
              </div>
            </Tooltip>
          )
        },
      }
    })

    return [...base, ...dateCols]
  }, [matrix?.dates, matrix?.missed_dates, matrix?.unfillable_dates, token, running, runningDate, recompute])

  /** 筛选条件是否有任意一项生效 —— 空态文案与命中 Tag 都要看它 */
  const filterActive = hasFilter(matrixFilter)

  const matrixPane = (
    <Spin spinning={matrixLoading && !matrix}>
      {matrixError && (
        <Alert
          type="error"
          showIcon
          title={matrixError}
          action={
            <Button size="small" onClick={() => ensureMatrix(matrixDays)}>
              重试
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}
      {runState?.error && (
        <Alert
          type="error"
          showIcon
          title={`重算失败：${runState.error}`}
          style={{ marginBottom: 12 }}
        />
      )}
      {running && (
        <Alert
          type="info"
          showIcon
          title={
            runState?.date
              ? `正在补跑 ${fmtDate(runState?.date)} 的上榜数据`
              : '补跑进行中'
          }
          description={
            <>
              {/* 没有进度数据就不画条 —— 画一条恒 0% 的条比不画更像卡死 */}
              {runState?.progress && (
                <Progress
                  percent={progPercent(runState.progress)}
                  size="small"
                  style={{ maxWidth: 320 }}
                />
              )}
              <div>
                <Text type="secondary">{progLabel(runState?.progress)}</Text>
                {' · 完成后矩阵自动刷新'}
              </div>
            </>
          }
          style={{ marginBottom: 12 }}
        />
      )}
      {matrix ? (
        <>
          <Space wrap size={8} style={{ marginBottom: 10 }}>
            <Select
              size="small"
              style={{ width: 156 }}
              value={matrixDays}
              onChange={(v) => {
                setMatrixDays(v)
                ensureMatrix(v)
              }}
              options={[
                { value: 30, label: '最近 30 个交易日' },
                { value: 90, label: '最近 90 个交易日' },
                { value: 365, label: '最近 365 个交易日' },
              ]}
            />
            <MatrixSearch
              onApply={(v) => {
                setMatrixQuery(v)
                setMatrixPage(1)
              }}
            />
            <Tag color="blue">{matrix.total} 只上榜过</Tag>
            {(matrixQuery.trim() || filterActive) && (
              <Tag>匹配 {matrixRows.length} 只</Tag>
            )}
            <Tag>{matrix.ran_dates.length} 天有数据</Tag>
            {matrix.missed_dates.length > 0 && (
              <Tooltip
                title={
                  '补跑按钮长在下表对应日期的表头上，缺哪天就点哪天；' +
                  '涨/跌停池已过东财 30 天保留期的列标「不可补」，点不了'
                }
              >
                <Tag color="orange">
                  缺 {matrix.missed_dates.length} 天未跑 ·{' '}
                  {matrix.missed_dates.map((d) => fmtDate(d)).join('、')}
                </Tag>
              </Tooltip>
            )}
          </Space>

          {/* 筛选条单独一行：上面那行已有范围选择/搜索/统计，再塞就挤不下 */}
          <MatrixFilters
            onApply={(f) => {
              setMatrixFilter(f)
              setMatrixPage(1)
            }}
          />

          {/* 查不到时**只换表格那一块**：范围选择 / 搜索框 / 筛选条必须留在原地。
              以前这里是整块三元分支，空结果把工具条一起顶掉 —— 搜索框一卸载，
              那个查不到的词还留在父级 state 里却再也清不掉，表单和列表整个被
              提示盖住（2026-11-11 用户反馈）。 */}
          {matrix.total === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description="该区间内还没有任何上榜记录"
            />
          ) : matrixRows.length === 0 ? (
            <Empty
              image={Empty.PRESENTED_IMAGE_SIMPLE}
              description={
                matrixQuery.trim() && filterActive
                  ? `没有同时满足查询「${matrixQuery.trim()}」和筛选条件的上榜股票`
                  : matrixQuery.trim()
                    ? `没有代码或名称包含「${matrixQuery.trim()}」的上榜股票`
                    : '没有满足筛选条件的上榜股票'
              }
            />
          ) : (
            <>
              <Space size={14} style={{ marginBottom: 10 }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  图例：
                </Text>
                <Tag color="processing" style={{ marginInlineEnd: 0 }}>
                  #8
                </Tag>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  当天名次
                </Text>
                <Text type="secondary" style={{ fontSize: 12, fontVariantNumeric: 'tabular-nums' }}>
                  12.34
                </Text>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  当日收盘价（不复权，缺本地日线显 -）
                </Text>
                <div
                  style={{
                    width: 34,
                    height: 20,
                    borderRadius: token.borderRadiusSM,
                    backgroundColor: token.colorFillQuaternary,
                  }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  当天没跑任务
                </Text>
                <div
                  style={{
                    width: 34,
                    height: 20,
                    borderRadius: token.borderRadiusSM,
                    border: `1px solid ${token.colorBorderSecondary}`,
                  }}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  跑过但没上榜
                </Text>
              </Space>

              <Table<MatrixRowX>
                rowKey="code"
                size="small"
                columns={matrixColumns}
                dataSource={matrixRows}
                pagination={{
                  current: matrixPage,
                  onChange: (p) => setMatrixPage(p),
                  pageSize: 50,
                  showSizeChanger: true,
                  pageSizeOptions: [20, 50, 100, 200],
                  size: 'small',
                  showTotal: (t) => `共 ${t} 只`,
                }}
                scroll={{ x: 'max-content', y: 460 }}
              />
            </>
          )}
        </>
      ) : null}
    </Spin>
  )

  /* ------------------------------------------------------------------ *
   * 前5跟踪（2026-11-11 拍板）：名单由后端扫 main_force 快照推导 ——
   * 当日 rank 1-5 进名单，30 自然日、截止 = 最后一次进前5 + 30 天，
   * 重复上榜顺延；到期票留在表里标「已到期」、不再拉股价。
   * 点代码开**同一套**走势弹框（source='track'），数据全在 payload 里。
   * ------------------------------------------------------------------ */
  const trackColumns = useMemo<ColumnsType<MainForceTrackRow>>(() => {
    /** 起始日（含）之后第一个有价的收盘价；整段缺价回 null，显 `-` 不编数 */
    const firstPrice = (row: MainForceTrackRow): number | null => {
      for (const d of track?.dates ?? []) {
        if (d < row.first_date) continue
        const p = row.prices[d]
        if (p != null) return p
      }
      return null
    }
    /** 最新可得收盘价（从轴尾往回找第一个有价的）；没有回 null */
    const lastPrice = (row: MainForceTrackRow): number | null => {
      const dates = track?.dates ?? []
      for (let i = dates.length - 1; i >= 0; i--) {
        const p = row.prices[dates[i]]
        if (p != null) return p
      }
      return null
    }

    return [
      {
        title: '代码',
        key: 'code',
        width: 104,
        fixed: 'left',
        // 与矩阵点代码同一落点（MatrixTrendModal），source 标明行回查哪份
        render: (_: unknown, row) => (
          <Tooltip title={`打开 ${row.name || row.code} 的股价走势与上榜名次`}>
            <a
              className="stock-code"
              onClick={(e) => {
                e.stopPropagation()
                trendRef.current?.open(row.code, 'track')
              }}
            >
              {row.code}
            </a>
          </Tooltip>
        ),
      },
      {
        title: '名称',
        dataIndex: 'name',
        width: 110,
        fixed: 'left',
        ellipsis: true,
        render: (v: string) => v || '-',
      },
      {
        title: (
          <Tooltip title="第一次进当日榜前5的日子 —— 跟踪从这天算起">
            <span>首次进前5</span>
          </Tooltip>
        ),
        dataIndex: 'first_date',
        width: 106,
        align: 'center',
        render: (v: string) => fmtDate(v),
      },
      {
        title: (
          <Tooltip title="最后一次进前5的日子；重复上榜时截止日跟着它顺延">
            <span>最后上榜</span>
          </Tooltip>
        ),
        dataIndex: 'last_date',
        width: 106,
        align: 'center',
        render: (v: string) => fmtDate(v),
      },
      {
        title: (
          <Tooltip title="最后一次进前5 + 30 自然日；截止日当天仍在跟踪期">
            <span>跟踪截止</span>
          </Tooltip>
        ),
        dataIndex: 'deadline',
        width: 106,
        align: 'center',
        render: (v: string) => fmtDate(v),
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 88,
        align: 'center',
        render: (v: string) =>
          v === '跟踪中' ? (
            <Tag color="processing" style={{ marginInlineEnd: 0 }}>
              跟踪中
            </Tag>
          ) : (
            <Tag style={{ marginInlineEnd: 0 }}>已到期</Tag>
          ),
      },
      {
        title: (
          <Tooltip title="进当日榜前5的次数（矩阵的上榜次数是全部名次，这里是跟踪口径）">
            <span>进前5次数</span>
          </Tooltip>
        ),
        dataIndex: 'hits',
        width: 96,
        align: 'center',
        sorter: (a, b) => a.hits - b.hits,
        defaultSortOrder: 'descend',
        render: (v: number) => <Text strong>{v}</Text>,
      },
      {
        title: '最好名次',
        dataIndex: 'best_rank',
        width: 92,
        align: 'center',
        sorter: (a, b) => a.best_rank - b.best_rank,
        render: (v: number) => <Text strong>#{v}</Text>,
      },
      {
        title: (
          <Tooltip title="最新可得收盘价（不复权，daily_bar 单一出处）；缺本地日线显 -">
            <span>最新收盘价</span>
          </Tooltip>
        ),
        key: 'last_price',
        width: 108,
        align: 'right',
        render: (_: unknown, row) => {
          const p = lastPrice(row)
          return p == null ? (
            <Text type="secondary">-</Text>
          ) : (
            <Text style={{ fontVariantNumeric: 'tabular-nums' }}>{p.toFixed(2)}</Text>
          )
        },
      },
      {
        title: (
          <Tooltip title="首次进前5日 → 最新可得收盘价（不复权）的涨跌幅；任一端缺价显 -">
            <span>跟踪区间涨跌</span>
          </Tooltip>
        ),
        key: 'range_pct',
        width: 118,
        align: 'right',
        render: (_: unknown, row) => {
          const from = firstPrice(row)
          const to = lastPrice(row)
          if (from == null || to == null || from === 0) {
            return <Text type="secondary">-</Text>
          }
          const pct = (to / from - 1) * 100
          return (
            <Text style={{ color: signColor(pct, token), fontVariantNumeric: 'tabular-nums' }}>
              {fmtPct(pct)}
            </Text>
          )
        },
      },
    ]
  }, [track?.dates, token])

  const trackPane = (
    <Spin spinning={trackLoading && !track}>
      {trackError && (
        <Alert
          type="error"
          showIcon
          title={trackError}
          action={
            <Button size="small" onClick={ensureTrack}>
              重试
            </Button>
          }
          style={{ marginBottom: 12 }}
        />
      )}
      {track && track.rows.length === 0 && !trackError ? (
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="还没有股票进过前5 —— 盘后打分完成后自动进名单"
        />
      ) : track ? (
        <>
          <Space wrap size={8} style={{ marginBottom: 10 }}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              口径：当日打分榜第 1~5 名进名单，跟踪 30 自然日、截止 = 最后一次进前5 + 30
              天，重复上榜自动顺延；到期保留可回看、不再拉股价。点代码看走势。
            </Text>
            <Tag color="processing">跟踪中 {track.tracking}</Tag>
            <Tag>已到期 {track.expired}</Tag>
            <Button size="small" onClick={ensureTrack} loading={trackLoading}>
              刷新
            </Button>
          </Space>
          <Table<MainForceTrackRow>
            rowKey="code"
            size="small"
            columns={trackColumns}
            dataSource={track.rows}
            pagination={
              track.rows.length > 15
                ? {
                    pageSize: 15,
                    showSizeChanger: false,
                    size: 'small',
                    showTotal: (t) => `共 ${t} 只`,
                  }
                : false
            }
            scroll={{ x: 'max-content' }}
          />
        </>
      ) : null}
    </Spin>
  )

  const detail = inflowRow ? (inflowNow?.items[inflowRow.code] ?? null) : null
  const detailDays = detail?.days ?? []
  const detailTotal = detail?.total ?? 0
  /** 逐日条形按窗口内最大绝对值归一，正半轴向右、负半轴向左 */
  const detailPeak = Math.max(...detailDays.map((d) => Math.abs(d.net_inflow)), 1)

  return (
    <Card
      title={`主力建仓候选 ${data ? `· ${fmtDate(data.date)}` : ''}`}
      extra={
        // 这一排（回看历史的日期下拉、刷新、重新打分）全是单日榜单的操作，
        // 切到矩阵页时不显示 —— 矩阵自带范围选择，补跑按钮长在漏跑列的表头上。
        tab === 'list' ? (
          <Space>
            {dates.length > 1 && (
              <Select
                size="small"
                style={{ width: 132 }}
                value={activeDate}
                options={dates.map((d) => ({ value: d, label: fmtDate(d) }))}
                onChange={(v) => void load(v)}
                placeholder="回看历史"
              />
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              盘后定时抓取 · 约 {data?.params.preselect_top ?? 300} 只逐只拉取日线
            </Text>
            <Button size="small" onClick={() => void load(activeDate)} loading={loading}>
              刷新
            </Button>
            <Button size="small" type="primary" onClick={() => void recompute()} loading={running}>
              {running ? '打分中…' : '重新打分'}
            </Button>
          </Space>
        ) : undefined
      }
    >
      <Tabs
        activeKey={tab}
        onChange={(k) => {
          const next = k as 'list' | 'matrix' | 'track'
          setTab(next)
          if (next === 'matrix') ensureMatrix(matrixDays)
          if (next === 'track') ensureTrack()
        }}
        items={[
          {
            key: 'list',
            label: '最新榜单',
            children: (
              <Spin spinning={loading && !data}>
                {notReady && !running ? (
                  <Empty description="尚未打分。单轮需逐只拉取约 300 只日线，耗时数分钟。">
                    <Button type="primary" onClick={() => void recompute()}>
                      开始打分
                    </Button>
                  </Empty>
                ) : (
                  <>
                    <div style={{ marginBottom: 14 }}>{header}</div>
                    {running && !data ? (
                      <Empty
                        image={Empty.PRESENTED_IMAGE_SIMPLE}
                        description="打分进行中，完成后自动刷新"
                      />
                    ) : (
                      <Table<MainForceRow>
                        rowKey="code"
                        size="small"
                        columns={columns}
                        dataSource={data?.rows ?? []}
                        pagination={false}
                        scroll={{ x: 1702 }}
                        expandable={{ expandedRowRender: expandedRow }}
                      />
                    )}
                  </>
                )}
              </Spin>
            ),
          },
          {
            key: 'matrix',
            label: '上榜矩阵',
            children: matrixPane,
          },
          {
            key: 'track',
            label: '前5跟踪',
            children: trackPane,
          },
        ]}
      />

      {/* 免责声明留在 Tab 外：两个视图共用，切页也不会丢 */}
      <Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {data?.disclaimer ?? '本结果仅用于研究与可视化，不构成任何投资建议。'}
          模型按候选池内百分位加权，输出为研究性排序，不预测后市表现。
        </Text>
      </Paragraph>

      {/* 逐日明细弹框。antd Modal 走 portal 渲染，挂在 Card 下只是省一层包裹 */}
      <Modal
        open={inflowRow !== null}
        title={
          inflowRow
            ? `${inflowRow.name} ${inflowRow.code} · 近${
                detailDays.length || INFLOW_DAYS
              }日主力净流入明细`
            : ''
        }
        footer={null}
        width={560}
        onCancel={() => setInflowRow(null)}
      >
        {!detail || detailDays.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该股暂无逐日资金流数据" />
        ) : (
          <>
            <Space size={32} style={{ marginBottom: 18 }}>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {detailDays.length} 日合计
                </Text>
                <div
                  style={{
                    fontSize: 26,
                    lineHeight: '32px',
                    fontVariantNumeric: 'tabular-nums',
                    color: signColor(detailTotal, token),
                  }}
                >
                  {(detailTotal / 1e8).toFixed(3)} 亿
                </div>
              </div>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  占流通市值
                </Text>
                <div
                  style={{
                    fontSize: 26,
                    lineHeight: '32px',
                    fontVariantNumeric: 'tabular-nums',
                  }}
                >
                  {inflowRow?.float_mv
                    ? `${((detailTotal / inflowRow.float_mv) * 100).toFixed(2)}%`
                    : '-'}
                </div>
              </div>
            </Space>

            {detailDays.map((d) => {
              const value = d.net_inflow / 1e8
              const positive = value >= 0
              const half = (Math.abs(d.net_inflow) / detailPeak) * 50
              return (
                <div
                  key={d.date}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 12,
                    padding: '7px 0',
                    borderBottom: `1px solid ${token.colorSplit}`,
                  }}
                >
                  <Text style={{ width: 92, fontVariantNumeric: 'tabular-nums' }}>
                    {fmtDate(d.date)}
                  </Text>
                  <div
                    style={{
                      flex: 1,
                      position: 'relative',
                      height: 16,
                      background: token.colorFillQuaternary,
                      borderRadius: 8,
                    }}
                  >
                    {/* 0 轴 */}
                    <span
                      style={{
                        position: 'absolute',
                        left: '50%',
                        top: 0,
                        bottom: 0,
                        width: 1,
                        background: token.colorSplit,
                      }}
                    />
                    <span
                      style={{
                        position: 'absolute',
                        top: 2,
                        bottom: 2,
                        borderRadius: 8,
                        opacity: 0.85,
                        background: signColor(value, token),
                        ...(positive
                          ? { left: '50%', width: `${half}%` }
                          : { right: '50%', width: `${half}%` }),
                      }}
                    />
                  </div>
                  <Text
                    type={signType(value)}
                    style={{ width: 96, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }}
                  >
                    {value.toFixed(3)}
                  </Text>
                </div>
              )
            })}

            <div style={{ marginTop: 14 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {detailDays.length < INFLOW_DAYS
                  ? `窗口内仅 ${detailDays.length} 个交易日有数据：逐日快照随盘后任务每日积累，缺口会在本页自动回补，补齐前先显示已有天数。`
                  : `窗口 ${fmtDate(detailDays[0].date)} ~ ${fmtDate(
                      detailDays[detailDays.length - 1].date,
                    )}，金额单位亿元。`}
                本结果仅用于研究与可视化，不构成任何投资建议。
              </Text>
            </div>
          </>
        )}
      </Modal>

      {/* 股价走势 + 上榜名次弹框：矩阵/前5跟踪两个 tab 点代码的落点，见 MatrixTrendModal */}
      <MatrixTrendModal ref={trendRef} matrix={matrix} rows={matrixRows} track={track} />
    </Card>
  )
}
