import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocation } from 'react-router-dom'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Input,
  Popconfirm,
  Row,
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
import { ReloadOutlined, StarFilled, StarOutlined, HistoryOutlined } from '@ant-design/icons'
import LiveBoard from '../components/LiveBoard'
import SearchPick from '../components/SearchPick'
import StockCode from '../components/StockCode'
import {
  api,
  ApiError,
  BENCHMARK,
  BACKFILL_BASE_START,
  type SearchItem,
  type WatchDetail,
  type WatchItem,
  type WatchPerf,
} from '../lib/api'
import { DOWN_COLOR, UP_COLOR, cmp, compact, fmtDate, fmtNum, fmtPct, ma, signColor } from '../lib/format'
import { useEChart } from '../lib/useEChart'

const { Text, Paragraph } = Typography

/**
 * 「较加入时」= 当前股价相对**加入时录入价**（`price`）的涨跌幅，单位 %。
 * 与「现价」列同源（都取最近收盘 `perf.last_close`）；任一端缺失、或价格
 * 非正 → null（页面显示 `-`，绝不编数）。与「区间涨跌」的区别：那列从
 * **建仓日收盘**（perf 自己的基准）起算，这列从**加入时录入的价**起算。
 */
const vsAddPct = (r: WatchItem): number | null => {
  const base = r.price
  const cur = r.perf?.last_close
  if (base == null || cur == null || base <= 0 || cur <= 0) return null
  return ((cur - base) / base) * 100
}

export default function Watchlist() {
  const { token } = theme.useToken()

  // 左侧子菜单传进来的过滤口径（App.tsx 只注册了 /watchlist、/cn、/hk 三条，
  // 到不了别处）。
  // **直接读 pathname 而不是 useParams**：那三条是字面路径、没有 `:market`
  // 参数段，`useParams()` 只会返回空对象 —— 用它判市场会让三个子菜单
  // 渲染出一模一样的内容，而类型上还查不出来（`market?: string` 本就可以
  // undefined）。过滤口径叫 `filter`（与 `detail.market` 派生出来的 `market`
  // ——「当前选中这只股属于哪个市场」—— 语义不同，不能同名）。
  // '' = 全部。后面**所有**跟着市场变的东西 —— 标题、表格行、手动关注的
  // 默认市场、空状态文案、基准说明 —— 都从这三个派生量出发，不写三处 if。
  const { pathname } = useLocation()
  // `slice` 出来是宽类型 string，必须在这里重新收窄成字面量 —— 下游
  // watchAdd.market 等处的类型都是 'cn' | 'hk'，宽 string 传不进去。
  const rawMarket = pathname.startsWith('/watchlist/') ? pathname.slice('/watchlist/'.length) : ''
  const filter: 'cn' | 'hk' | '' = rawMarket === 'cn' || rawMarket === 'hk' ? rawMarket : ''
  const isHk = filter === 'hk'
  const isCn = filter === 'cn'

  const [items, setItems] = useState<WatchItem[]>([])
  const [listLoading, setListLoading] = useState(false)
  const [perfLoading, setPerfLoading] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [detail, setDetail] = useState<WatchDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [tick, setTick] = useState(0)
  const [error, setError] = useState<string | null>(null)
  /** 视图：日线复盘（默认）看**已发生**的区间表现，实时行情看**正在发生**的盘面。
   *  两者共用同一份关注列表，所以添加表单与免责声明都留在切换器之外。 */
  const [tab, setTab] = useState<'daily' | 'live'>('daily')

  /** 先拿不含行情的列表（秒回），再后台补区间表现。
   *  两者可能乱序返回，所以**只补空不覆盖**，避免「刷新列表」把已经
   *  算好的区间表现冲成一列「-」（后端不带 perf 时该字段是 undefined）。 */
  const loadList = useCallback(async () => {
    setListLoading(true)
    try {
      const next = (await api.watchlist()).items
      setItems((prev) => {
        const prevPerf = new Map(prev.map((i) => [i.code, i.perf]))
        return next.map((i) =>
          i.perf === undefined && prevPerf.has(i.code)
            ? { ...i, perf: prevPerf.get(i.code) }
            : i,
        )
      })
      setError(null)
    } catch (e) {
      setError((e as ApiError).message)
    } finally {
      setListLoading(false)
    }
  }, [])

  const loadPerf = useCallback(async () => {
    setPerfLoading(true)
    try {
      setItems((await api.watchlist(true)).items)
      setError(null)
    } catch (e) {
      setError((e as ApiError).message)
    } finally {
      setPerfLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadList()
    void loadPerf()
  }, [loadList, loadPerf])

  /** 关注 / 取消关注后，后端返回的列表不含 perf，需要重新补算 */
  const reload = useCallback(() => {
    void loadPerf()
    setTick((t) => t + 1)
  }, [loadPerf])

  /** 当前子菜单下可见的行。接口不带 market 参数，**过滤在前端做** ——
   *  少一次请求，切换子菜单也秒回。老数据没有 market 字段时按 A 股算。 */
  const shown = useMemo(
    () => (filter ? items.filter((i) => (i.market ?? 'cn') === filter) : items),
    [items, filter],
  )

  /** 搜索下拉里要标「已添加」的 code 集合。**取全部而不是 `shown`** ——
   *  只看港股的子菜单里，A 股那条 `600519` 虽然不显示，但加了仍是重复关注，
   *  标出来比让人点一次换一个报错强。useMemo 固定引用，免得 SearchPick 里
   *  的 Set 每次渲染都重建。 */
  const takenCodes = useMemo(() => items.map((i) => i.code), [items])

  // 选中项在渲染期推导：当前选中的被删掉/不在列表里就自动落到第一个。
  // 用 effect 同步会导致 items 每变一次就多渲染一轮。
  // 注意基准是 `shown` 而不是 `items`：从 A 股切到港股子菜单时，
  // 上一个市场的选中项必须立刻失效，否则详情区会挂着别人的数据。
  const active =
    selected && shown.some((i) => i.code === selected) ? selected : (shown[0]?.code ?? null)

  useEffect(() => {
    if (!active) return
    let alive = true
    setDetailLoading(true)
    setError(null)
    api
      .watchSeries(active)
      .then((d) => alive && setDetail(d))
      .catch((e) => alive && setError((e as ApiError).message))
      .finally(() => alive && setDetailLoading(false))
    return () => {
      alive = false
    }
  }, [active, tick])

  const remove = useCallback(
    async (code: string) => {
      try {
        await api.watchRemove(code)
        reload()
      } catch (e) {
        setError((e as Error).message)
      }
    },
    [reload],
  )

  // ------------------------------------------------------- 手动补历史日线
  //
  // 与「主力建仓候选」页行首的时钟图标同一套：走后端 /api/pipeline/backfill
  // 的单票通道（items=[code]、only_gaps=true），只补缺的日期、已有行情一行
  // 不覆盖。**只对 A 股开放** —— daily_bar 回填链路走 akshare 的沪深日线
  // 接口，港股代码过去只会报错；港股走势本来就是每次实时拉的，没有
  // 「本地缺历史」这回事。
  const [pulling, setPulling] = useState<string | null>(null)
  const [tip, setTip] = useState<{ type: 'success' | 'error'; text: string } | null>(null)

  const pullHistory = useCallback(async (r: WatchItem) => {
    setPulling(r.code)
    try {
      const res = await api.backfillOne(r.code)
      if (res.failed > 0) {
        const p = res.problems[0]
        setTip({
          type: 'error',
          text:
            `补 ${r.name} 历史日线失败：${p?.error ?? '未知错误'}` +
            (p?.source ? `（${p.source}）` : '') +
            '。东财常态拒连时可稍后重试，会自动走腾讯备源。',
        })
      } else if (res.inserted > 0) {
        setTip({
          type: 'success',
          text: `已补 ${r.name} ${r.code}：新增 ${res.inserted} 行日线（重复日期已跳过，已有数据未覆盖）。点「刷新表现」看最新区间统计。`,
        })
      } else if (res.skipped > 0) {
        setTip({
          type: 'success',
          text: `${r.name} ${r.code} 的 ${BACKFILL_BASE_START} 至今日线已齐，未发请求`,
        })
      } else {
        setTip({
          type: 'error',
          text: `源端未返回 ${r.code} 的日线（可能数据滞后或代码已退市），本地未新增`,
        })
      }
    } catch (e) {
      setTip({ type: 'error', text: `补历史日线失败：${(e as ApiError).message}` })
    } finally {
      setPulling(null)
    }
  }, [])

  // ------------------------------------------------------------ 手动关注
  //
  // 「主力建仓候选」页的星标只覆盖**打分跑出来的 A 股**；港股不会出现在
  // 候选里，任何你想自己盯着的标的也一样 —— 没有这个入口，港股支持
  // 就是无源之水。代码按位数自动判市场（6 位 A 股 / 1~5 位港股），
  // 选错了后端会按位数校验并直接 400，不会存成一条查不到行情的假关注。
  const [addCode, setAddCode] = useState('')
  const [addName, setAddName] = useState('')
  const [addMarket, setAddMarket] = useState<'auto' | 'cn' | 'hk'>('auto')
  const [adding, setAdding] = useState(false)

  /**
   * 真正落库的那一步。参数**不是 state** —— 搜索选中时 code/name/market 都来自
   * 候选，而 setState 是异步的，「先 set 再读」必然读到上一轮的旧值，加错对象。
   *
   * `picked` 是搜索候选报上来的市场，优先级排在**子菜单之后、下拉之前**：
   * 子菜单已经把范围限死了（在港股页贴 6 位代码会直接 400，不会存成一条自己
   * 都看不见的假关注），必须它说了算；「全部」页则认候选的 —— 它比按位数猜准。
   */
  const addOne = useCallback(
    async (raw: string, name: string, picked?: 'cn' | 'hk') => {
      const code = raw.trim()
      if (!code) {
        setError('先填股票代码')
        return false
      }
      const market =
        filter ||
        picked ||
        (addMarket === 'auto'
          ? /^\d{1,5}$/.test(code) || /^hk/i.test(code) || /\.hk$/i.test(code)
            ? 'hk'
            : 'cn'
          : addMarket)
      setAdding(true)
      try {
        // 港股不传 name 也没关系：后端会去腾讯 qt.gtimg.cn 查（约 50ms）
        await api.watchAdd({ code, name: name.trim(), market })
        setAddCode('')
        setAddName('')
        setError(null)
        reload()
        return true
      } catch (e) {
        setError((e as ApiError).message)
        return false
      } finally {
        setAdding(false)
      }
    },
    [addMarket, filter, reload],
  )

  /** 手输路径：读两个输入框。 */
  const addWatch = useCallback(
    () => addOne(addCode, addName),
    [addOne, addCode, addName],
  )

  /** 搜索选中路径：候选自带代码、名称与市场。 */
  const pickWatch = useCallback(
    (it: SearchItem) => void addOne(it.code, it.name, it.market),
    [addOne],
  )

  /** 回车且无候选可选中 —— 名称框填了就用，留空交后端回填。 */
  const enterWatch = useCallback(
    (text: string) => void addOne(text, addName),
    [addOne, addName],
  )

  // 用 useMemo 固定引用：`detail?.bars ?? []` 每次渲染都是新数组，
  // 直接当 useMemo 依赖会让图表 option 每帧重算、ECharts 反复 setOption。
  const stats = useMemo(() => detail?.stats as WatchPerf | undefined, [detail])
  const bars = useMemo(() => detail?.bars ?? [], [detail])

  // ------------------------------------------------------------ 派生文案
  const market = detail?.market ?? 'cn'
  const benchName = BENCHMARK[market] ?? BENCHMARK.cn

  // 手动关注进来的（没有 rank / score 快照）没有"建仓"这回事 —— source_date
  // 只是「从这天起算」。港股几乎都是手动关注，直接标"建仓日"会让人误以为
  // 打分模型看上了它，而模型压根不跑港股。
  const hasScore = detail?.rank != null || detail?.score != null
  const startLabel = hasScore ? '建仓日' : '起算日'

  // ------------------------------------------------------------ K线 + 成交量
  const kRef = useEChart(
    useMemo(() => {
      if (!detail || !bars.length || !stats) return null
      const dates = bars.map((b) => b.date)
      const srcIdx = Math.max(0, dates.indexOf(stats.start_date))
      const hiIdx = dates.indexOf(stats.high_date)
      const loIdx = dates.indexOf(stats.low_date)

      return {
        animation: false,
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'cross' },
          formatter: (params: unknown) => {
            const list = (params ?? []) as {
              axisValue: string
              seriesName?: string
              data?: unknown
              value?: unknown
            }[]
            const bar = bars.find((b) => b.date === list[0]?.axisValue)
            const lines = [`${fmtDate(list[0]?.axisValue)}`]
            if (bar) {
              lines.push(
                `开 ${fmtNum(bar.open)}　收 ${fmtNum(bar.close)}`,
                `高 ${fmtNum(bar.high)}　低 ${fmtNum(bar.low)}`,
                `涨跌 ${fmtPct(bar.pct_chg)}　换手 ${fmtNum(bar.turnover, 1)}%`,
              )
            }
            return lines.join('<br/>')
          },
        },
        axisPointer: { link: [{ xAxisIndex: 'all' }] },
        legend: { data: ['K线', 'MA5', 'MA20'], top: 0, textStyle: { fontSize: 11 } },
        grid: [
          { left: 64, right: 64, top: 32, height: '50%' },
          { left: 64, right: 64, top: '66%', height: '16%' },
        ],
        xAxis: [
          {
            type: 'category',
            data: dates,
            gridIndex: 0,
            boundaryGap: true,
            axisLabel: { show: false },
            axisLine: { show: false },
            axisTick: { show: false },
          },
          {
            type: 'category',
            data: dates,
            gridIndex: 1,
            boundaryGap: true,
            axisLabel: { fontSize: 10 },
          },
        ],
        yAxis: [
          {
            scale: true,
            gridIndex: 0,
            axisLabel: { fontSize: 10 },
            splitLine: { lineStyle: { opacity: 0.25 } },
          },
          {
            gridIndex: 1,
            splitNumber: 2,
            axisLabel: { fontSize: 10, formatter: (v: number) => compact(v) },
            splitLine: { show: false },
          },
        ],
        dataZoom: [
          { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
          { type: 'slider', xAxisIndex: [0, 1], bottom: 2, height: 16 },
        ],
        series: [
          {
            name: 'K线',
            type: 'candlestick',
            data: bars.map((b) => [b.open, b.close, b.low, b.high]),
            itemStyle: {
              color: UP_COLOR,
              color0: DOWN_COLOR,
              borderColor: UP_COLOR,
              borderColor0: DOWN_COLOR,
            },
            markLine: {
              silent: true,
              animation: false,
              symbol: 'none',
              lineStyle: { type: 'dashed', color: token.colorPrimary },
              label: {
                formatter: `${startLabel} ${fmtDate(detail.source_date)}`,
                position: 'insideEndTop',
                fontSize: 11,
                color: token.colorPrimary,
              },
              data: [{ xAxis: srcIdx }],
            },
            markPoint: {
              symbol: 'pin',
              symbolSize: 46,
              animation: false,
              itemStyle: { color: token.colorTextTertiary },
              label: { fontSize: 10, color: '#fff' },
              data: [
                hiIdx >= 0 && {
                  coord: [dates[hiIdx], stats.high],
                  name: '区间最高',
                  value: fmtNum(stats.high),
                },
                loIdx >= 0 && {
                  coord: [dates[loIdx], stats.low],
                  name: '区间最低',
                  value: fmtNum(stats.low),
                },
              ].filter(Boolean),
            },
          },
          { name: 'MA5', type: 'line', data: ma(bars, 5), showSymbol: false, smooth: true, lineStyle: { width: 1 } },
          { name: 'MA20', type: 'line', data: ma(bars, 20), showSymbol: false, smooth: true, lineStyle: { width: 1 } },
          {
            name: '成交量',
            type: 'bar',
            xAxisIndex: 1,
            yAxisIndex: 1,
            data: bars.map((b) => ({
              value: b.volume,
              itemStyle: { color: (b.pct_chg ?? 0) >= 0 ? UP_COLOR : DOWN_COLOR },
            })),
          },
        ],
      }
    }, [detail, bars, stats, startLabel, token]),
  )

  // ------------------------------------------------------------ 收益率对比
  const retRef = useEChart(
    useMemo(() => {
      if (!detail || !bars.length || !stats) return null
      // 两条线都以**建仓日收盘**为 0% 基准：建仓日之后是标准累计收益，
      // 建仓日之前显示的是"相对建仓日"的位置，能看出它是怎么走到这个位置的。
      const base = stats.start_close || 1
      const refDate = stats.start_date
      const srcIdx = Math.max(0, bars.findIndex((b) => b.date >= refDate))
      const stockRet = bars.map((b) => +((((b.close ?? base) / base) - 1) * 100).toFixed(3))

      const sorted = [...(detail.index ?? [])].sort((a, b) => (a.date < b.date ? -1 : 1))
      let ref = 0
      while (ref + 1 < sorted.length && sorted[ref + 1].date <= refDate) ref += 1
      const idxBase =
        sorted[ref] && sorted[ref].date <= refDate ? sorted[ref].close : (sorted[0]?.close ?? 0)

      let ptr = 0
      const idxRet = bars.map((b) => {
        while (ptr + 1 < sorted.length && sorted[ptr + 1].date <= b.date) ptr += 1
        const cur = sorted[ptr] && sorted[ptr].date <= b.date ? sorted[ptr].close : null
        return cur && idxBase ? +((cur / idxBase - 1) * 100).toFixed(3) : null
      })

      return {
        animation: false,
        tooltip: { trigger: 'axis', valueFormatter: (v: unknown) => `${v}%` },
        legend: { data: [detail.name, benchName], top: 0, textStyle: { fontSize: 11 } },
        grid: { left: 64, right: 24, top: 34, bottom: 30 },
        xAxis: {
          type: 'category',
          data: bars.map((b) => b.date),
          axisLabel: { fontSize: 10 },
        },
        yAxis: {
          type: 'value',
          axisLabel: { fontSize: 10, formatter: (v: number) => `${v}%` },
          splitLine: { lineStyle: { opacity: 0.25 } },
        },
        series: [
          {
            name: detail.name,
            type: 'line',
            data: stockRet,
            showSymbol: false,
            lineStyle: { width: 2 },
            itemStyle: { color: token.colorPrimary },
            markLine: {
              silent: true,
              animation: false,
              symbol: 'none',
              lineStyle: { type: 'dashed', color: token.colorPrimary },
              label: { formatter: startLabel, position: 'insideEndTop', fontSize: 11 },
              data: [{ xAxis: srcIdx }],
            },
          },
          {
            name: benchName,
            type: 'line',
            data: idxRet,
            showSymbol: false,
            lineStyle: { width: 1.5, type: 'dashed' },
            itemStyle: { color: token.colorTextTertiary },
          },
        ],
      }
    }, [detail, bars, stats, benchName, startLabel, token]),
  )

  const columns: ColumnsType<WatchItem> = [
    {
      title: '代码',
      dataIndex: 'code',
      width: 112,
      render: (v: string, r) => (
        <Space size={4}>
          <Text copyable={{ text: v }}>
            <StockCode code={v} name={r.name} market={r.market === 'hk' ? 'hk' : 'cn'} />
          </Text>
          {r.market === 'hk' && (
            <Tag color="blue" style={{ marginInlineEnd: 0 }}>HK</Tag>
          )}
        </Space>
      ),
    },
    {
      title: '名称',
      dataIndex: 'name',
      width: 110,
      ellipsis: true,
      render: (v: string, r) => (
        <Tooltip title={`${v} · ${r.market === 'hk' ? '港股（无行业归属）' : r.industry || '行业未知'}`}>
          <span>{v}</span>
        </Tooltip>
      ),
    },
    {
      title: (
        <Tooltip title="A 股 = 打分给出的建仓日；港股与手动关注 = 起算日（打分模型只跑 A 股）">
          建仓日
        </Tooltip>
      ),
      dataIndex: 'source_date',
      width: 104,
      render: (v: string) => fmtDate(v),
    },
    { title: '当时排名', dataIndex: 'rank', width: 84, align: 'center', render: (v?: number | null) => (v ? `#${v}` : '-') },
    { title: '当时分', dataIndex: 'score', width: 76, align: 'right', render: (v?: number | null) => fmtNum(v, 1) },
    {
      title: (
        <Tooltip title="加入当时录入的股价：主力建仓候选 / 个股详情按打分当时的行情录入，港股关注由后端补当日价；没录到就显示 -（A 股手动关注不取价）">
          加入时股价
        </Tooltip>
      ),
      dataIndex: 'price',
      width: 104,
      align: 'right',
      render: (v: number | null, r) =>
        v == null ? (
          <Text type="secondary">-</Text>
        ) : (
          // 与「现价」同一套货币前缀：混在「全部」里时港股是港元、A 股是人民币
          <Text>
            {r.market === 'hk' ? 'HK$' : ''}
            {fmtNum(v)}
          </Text>
        ),
    },
    {
      title: '现价',
      width: 94,
      align: 'right',
      render: (_: unknown, r) =>
        r.perf ? (
          // 混在「全部」里时两种货币必须分得清：港股是港元、A 股是人民币
          <Text>
            {r.market === 'hk' ? 'HK$' : ''}
            {fmtNum(r.perf.last_close)}
          </Text>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    {
      title: (
        <Tooltip title="当前股价相对加入时股价的涨跌幅；当前股价与「现价」列同源（最近收盘），任一端缺失显示 -">
          较加入时
        </Tooltip>
      ),
      width: 100,
      align: 'right',
      sorter: (a, b) => cmp(vsAddPct(a), vsAddPct(b)),
      render: (_: unknown, r) => {
        const pct = vsAddPct(r)
        return pct == null ? (
          <Text type="secondary">-</Text>
        ) : (
          <Text style={{ color: signColor(pct, token) }}>{fmtPct(pct)}</Text>
        )
      },
    },
    {
      title: '区间涨跌',
      width: 100,
      align: 'right',
      sorter: (a, b) => (a.perf?.pct ?? -999) - (b.perf?.pct ?? -999),
      render: (_: unknown, r) =>
        r.perf ? (
          <Text style={{ color: signColor(r.perf.pct, token) }}>{fmtPct(r.perf.pct)}</Text>
        ) : (
          <Text type="secondary">-</Text>
        ),
    },
    {
      title: (
        <Tooltip title="A 股对沪深300、港股对恒生指数；只有单个市场的持仓时才可比">
          同期基准
        </Tooltip>
      ),
      width: 112,
      align: 'right',
      render: (_: unknown, r) =>
        r.perf?.index_pct == null ? (
          <Text type="secondary">-</Text>
        ) : (
          <Tooltip title={BENCHMARK[r.market ?? 'cn']}>{fmtPct(r.perf.index_pct)}</Tooltip>
        ),
    },
    {
      title: '超额',
      width: 88,
      align: 'right',
      sorter: (a, b) => (a.perf?.alpha ?? -999) - (b.perf?.alpha ?? -999),
      render: (_: unknown, r) =>
        r.perf?.alpha == null ? (
          <Text type="secondary">-</Text>
        ) : (
          <Text style={{ color: signColor(r.perf.alpha, token) }}>{fmtPct(r.perf.alpha)}</Text>
        ),
    },
    {
      title: '操作',
      key: 'action',
      width: 128,
      align: 'center',
      render: (_: unknown, r) => (
        <Space size={4}>
          <Button size="small" type={active === r.code ? 'primary' : 'text'} onClick={() => setSelected(r.code)}>
            查看
          </Button>
          {/* A 股才有「本地日线库」这回事；港股每次实时拉，不给这个按钮 */}
          {(r.market ?? 'cn') !== 'hk' && (
            <Tooltip title={`联网补 ${r.name} 的历史日线进本地库（${BACKFILL_BASE_START} 起）：只补缺的日期，已有行情不覆盖`}>
              <Button
                size="small"
                type="text"
                aria-label="补历史日线"
                icon={<HistoryOutlined />}
                loading={pulling === r.code}
                disabled={pulling !== null && pulling !== r.code}
                onClick={() => void pullHistory(r)}
              />
            </Tooltip>
          )}
          <Popconfirm title={`取消关注 ${r.name}？`} onConfirm={() => void remove(r.code)}>
            <Button size="small" type="text" danger>
              移除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  // 港股页摘掉「当时排名 / 当时分」：打分模型只跑 A 股，这两列在港股上
  // 永远是 '-'，摆着只会让人以为是数据还没加载出来。
  // 注意 columns 是 `ColumnGroupType | ColumnType` 联合类型，只有后者带
  // dataIndex，直接点属性编译不过，先收窄再比较。
  const shownColumns = isHk
    ? columns.filter((c) => {
        const key = 'dataIndex' in c ? c.dataIndex : undefined
        return key !== 'rank' && key !== 'score'
      })
    : columns

  const empty = !listLoading && !shown.length

  return (
    <Card
      title={
        <Space wrap>
          <StarFilled style={{ color: token.colorWarning }} />
          {isHk ? '港股关注' : isCn ? 'A 股关注' : '关注股票'}
          <Text type="secondary">
            （{shown.length}
            {filter && items.length > shown.length ? ` / 共 ${items.length}` : ''} 只）
          </Text>
          {isHk && <Tag color="blue">HK</Tag>}
          {isHk && (
            <Text type="secondary" style={{ fontSize: 12, fontWeight: 400 }}>
              同期基准 = 恒生指数
            </Text>
          )}
        </Space>
      }
      extra={
        // 实时行情那支有自己的一套刷新控件；「刷新表现」本来也是日线视图
        // 的概念（拉的是区间表现），挂在实时视图上只会让人分不清该点哪个。
        tab === 'live' ? null : (
          <Space>
            <Tooltip title="重新拉取区间表现（行情有 6 小时缓存）">
              <Button
                size="small"
                icon={<ReloadOutlined />}
                loading={perfLoading}
                onClick={() => void loadPerf()}
              >
                刷新表现
              </Button>
            </Tooltip>
            <Button size="small" loading={listLoading} onClick={() => void loadList()}>
              刷新列表
            </Button>
          </Space>
        )
      }
    >
      {error && (
        <Alert
          type="error"
          showIcon
          title={error} closable={{ onClose: () => setError(null) }}
          style={{ marginBottom: 12 }}
        />
      )}
      {tip && (
        <Alert
          type={tip.type}
          showIcon
          title={tip.text}
          closable={{ onClose: () => setTip(null) }}
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 手动关注：港股的唯一入口。左边的框既能搜（选中即关注）也能直接贴代码回车，
          右边名称框只服务「贴代码」这条路 —— 走搜索时名称由候选自带。 */}
      <div
        style={{
          display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap', alignItems: 'center',
        }}
      >
        <SearchPick
          kind="stock"
          market={filter || undefined}
          width={filter ? 240 : 260}
          value={addCode}
          onChange={setAddCode}
          placeholder={
            isHk
              ? '搜港股代码或名称，选中即关注'
              : isCn
                ? '搜 A 股代码或名称，选中即关注'
                : '搜代码或名称（A 股 / 港股），选中即关注'
          }
          taken={takenCodes}
          disabled={adding}
          onPick={pickWatch}
          onEnter={enterWatch}
        />
        <Input
          size="small"
          style={{ width: 150 }}
          placeholder={isCn ? '名称（可留空）' : '名称（港股可留空，自动查）'}
          value={addName}
          allowClear
          onChange={(e) => setAddName(e.target.value)}
          onPressEnter={() => void addWatch()}
        />
        {/* 子菜单已经限定了市场，再给一个可选「自动」只会让人选错；此时用
            标签占位，把「这里只能加 X」说清楚。全部页才需要真正的选择器。 */}
        {filter ? (
          <Tag
            color={isHk ? 'blue' : 'green'}
            style={{ margin: 0, lineHeight: '24px', fontSize: 12 }}
          >
            {isHk ? '港股 · 5 位' : 'A 股 · 6 位'}
          </Tag>
        ) : (
          <Select<'auto' | 'cn' | 'hk'>
            size="small"
            style={{ width: 118 }}
            value={addMarket}
            onChange={setAddMarket}
            options={[
              { value: 'auto', label: '市场 · 自动' },
              { value: 'cn', label: 'A股' },
              { value: 'hk', label: '港股' },
            ]}
          />
        )}
        <Button
          size="small"
          type="primary"
          icon={<StarOutlined />}
          loading={adding}
          onClick={() => void addWatch()}
        >
          关注
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {isHk
            ? '搜索会联网；港股不参与 A 股的涨跌停池、连板梯队与主力建仓打分，只提供行情与恒生指数对比。'
            : isCn
              ? '搜索会联网找候选，添加本身只写本地库；也可到「主力建仓候选」页点行首星标。'
              : '搜索会联网；港股只提供行情与基准对比 —— 不参与 A 股的涨跌停池、连板梯队与主力建仓打分。'}
        </Text>
      </div>

      {/* 视图切换。**内容故意放在 Tabs 外面**（只把 Tabs 当标签条用）：
          实时行情那支挂着自动轮询，做成 Tab children 的话 antd 会把它留在
          DOM 里隐藏着，切回日线后 interval 还在空转。放外面用三元切换，
          切走即卸载，轮询随之清理。 */}
      <Tabs
        activeKey={tab}
        onChange={(k) => setTab(k as 'daily' | 'live')}
        items={[
          { key: 'daily', label: '日线复盘' },
          { key: 'live', label: '实时行情' },
        ]}
      />

      {tab === 'live' ? (
        <LiveBoard items={shown} />
      ) : empty ? (
        <Empty
          description={
            isHk ? '还没有关注的港股' : isCn ? '还没有关注的 A 股' : '还没有关注的股票'
          }
        >
          <Text type="secondary">
            {isHk
              ? '在上面贴 1~5 位港股代码（00700、09988），名称会自动补齐；关注后可看它从起算日至今的走势与恒生指数的对比。'
              : isCn
                ? '上面贴 6 位代码，或到「主力建仓候选」页点行首的星标。关注后这里会显示它从起算日至今的走势。'
                : '上面直接贴代码关注（A 股与港股都行），或到「主力建仓候选」页点行首的星标。关注后这里会显示它从起算日至今的走势。'}
          </Text>
        </Empty>
      ) : (
        <>
          <Table<WatchItem>
            rowKey="code"
            size="small"
            columns={shownColumns}
            dataSource={shown}
            loading={listLoading || perfLoading}
            // 分页阈值跟着**过滤后**的行数走：港股页可能只显示 10 只，但全量 30 只，
            // 按 items.length 判断会挂一个永远只有一页的分页器。
            pagination={shown.length > 20 ? { pageSize: 20, size: 'small' } : false}
            onRow={(r) => ({ onClick: () => setSelected(r.code) })}
            rowClassName={(r) => (r.code === active ? 'ant-table-row-selected' : '')}
          />

          <Spin spinning={detailLoading} description="拉取日线中…">
            {!detail ? (
              <div style={{ marginTop: 24 }}>
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选中一只关注股查看走势" />
              </div>
            ) : (
              <div style={{ marginTop: 16 }}>
                <div style={{ marginBottom: 12, display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'baseline' }}>
                  <Text strong style={{ fontSize: 16 }}>
                    {detail.name}{' '}
                    <Text type="secondary">
                      <StockCode
                        code={detail.code}
                        name={detail.name}
                        market={market === 'hk' ? 'hk' : 'cn'}
                      />
                    </Text>
                  </Text>
                  {market === 'hk' && <Tag color="blue">港股</Tag>}
                  <Tag color={hasScore ? 'orange' : 'default'}>
                    {startLabel} {fmtDate(detail.source_date)}
                    {detail.rank ? ` · 当时第 ${detail.rank} 名` : ''}
                    {detail.score != null ? ` · ${detail.score.toFixed(1)} 分` : ''}
                  </Tag>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    基准 = {startLabel}收盘价 {fmtNum(stats?.start_close)}
                    {stats ? ` · ${fmtDate(stats.start_date)} ~ ${fmtDate(stats.last_date)}` : ''}
                  </Text>
                </div>

                {bars.length === 0 ? (
                  <Alert type="warning" showIcon title="该区间暂无行情数据" />
                ) : (
                  <>
                    <Row gutter={[12, 12]} style={{ marginBottom: 8 }}>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title="起始价"
                          value={stats?.start_close ?? 0}
                          precision={2}
                          styles={{ content: { fontSize: 18 } }}
                          suffix={<Text type="secondary" style={{ fontSize: 12 }}>{fmtDate(stats?.start_date)}</Text>}
                        />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title="现价"
                          value={stats?.last_close ?? 0}
                          precision={2}
                          styles={{ content: { fontSize: 18, color: signColor(stats?.pct, token) } }}
                          suffix={<Text type="secondary" style={{ fontSize: 12 }}>{fmtDate(stats?.last_date)}</Text>}
                        />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title="区间涨跌"
                          value={fmtPct(stats?.pct)}
                          styles={{ content: { fontSize: 18, color: signColor(stats?.pct, token) } }}
                        />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title={`同期${benchName}`}
                          value={fmtPct(stats?.index_pct)}
                          styles={{ content: { fontSize: 18 } }}
                        />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title="超额收益"
                          value={fmtPct(stats?.alpha)}
                          styles={{ content: { fontSize: 18, color: signColor(stats?.alpha, token) } }}
                        />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title="最大回撤"
                          value={fmtPct(stats?.max_drawdown)}
                          styles={{ content: { fontSize: 18, color: token.colorSuccess } }}
                        />
                      </Col>
                    </Row>

                    <Row gutter={[12, 12]} style={{ marginBottom: 8 }}>
                      <Col xs={12} sm={8} md={3}>
                        <Statistic title="区间最高" value={fmtNum(stats?.high)} styles={{ content: { fontSize: 16 } }} />
                      </Col>
                      <Col xs={12} sm={8} md={3}>
                        <Statistic title="区间最低" value={fmtNum(stats?.low)} styles={{ content: { fontSize: 16 } }} />
                      </Col>
                      <Col xs={12} sm={8} md={3}>
                        <Statistic title="交易日" value={stats?.trading_days ?? 0} styles={{ content: { fontSize: 16 } }} suffix={<Text type="secondary" style={{ fontSize: 12 }}>天</Text>} />
                      </Col>
                      <Col xs={12} sm={8} md={3}>
                        <Statistic title="自然日" value={stats?.calendar_days ?? 0} styles={{ content: { fontSize: 16 } }} suffix={<Text type="secondary" style={{ fontSize: 12 }}>天</Text>} />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic title="日均换手" value={fmtNum(stats?.avg_turnover, 1)} styles={{ content: { fontSize: 16 } }} suffix={<Text type="secondary" style={{ fontSize: 12 }}>%</Text>} />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title="日均成交额"
                          value={stats?.avg_amount ? (stats.avg_amount / 1e8).toFixed(2) : '-'}
                          styles={{ content: { fontSize: 16 } }}
                          suffix={<Text type="secondary" style={{ fontSize: 12 }}>亿</Text>}
                        />
                      </Col>
                      <Col xs={12} sm={8} md={4}>
                        <Statistic
                          title="最高/最低日期"
                          value={`${fmtDate(stats?.high_date)} / ${fmtDate(stats?.low_date)}`}
                          styles={{ content: { fontSize: 11 } }}
                        />
                      </Col>
                    </Row>

                    {stats && stats.trading_days <= 1 && (
                      <Alert
                        type="info"
                        showIcon
                        style={{ marginBottom: 10 }}
                        title={`${startLabel}就是最新交易日，区间表现要从下一个交易日收盘后才开始累积`}
                        description="所以上面的涨跌幅 / 超额 / 回撤目前都是 0，属正常。下面的 K 线和对比图已经带了起算日前 90 个自然日的走势上下文，可以先看它当时处于什么位置。"
                      />
                    )}

                    <Card size="small" title={`日K · 近 90 日（虚线 = ${startLabel}）`} style={{ marginBottom: 12 }}>
                      <div ref={kRef} style={{ width: '100%', height: 400 }} />
                    </Card>

                    <Row gutter={[12, 12]}>
                      <Col xs={24} lg={14}>
                        <Card size="small" title={`累计收益率对比（两线均以${startLabel}为 0%）`}>
                          <div ref={retRef} style={{ width: '100%', height: 280 }} />
                        </Card>
                      </Col>
                      <Col xs={24} lg={10}>
                        <Card size="small" title={hasScore ? '关注时的候选快照' : '关注记录'}>
                          <Paragraph style={{ marginBottom: 8 }}>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              {hasScore
                                ? `记录的是 ${fmtDate(detail.source_date)} 那轮打分的结论，用来回看「当时为什么关注它」。`
                                : `你于 ${fmtDate(detail.source_date)} 手动关注了它，没有经过 A 股打分模型，因此没有维度与入选理由。`}
                            </Text>
                          </Paragraph>
                          {hasScore && (
                            <>
                              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
                                {Object.entries(detail.snapshot?.dims ?? {}).map(([key, d]) => (
                                  <Tag key={key} style={{ marginInlineEnd: 0 }}>
                                    {d.short} {d.score.toFixed(0)}
                                  </Tag>
                                ))}
                                {!Object.keys(detail.snapshot?.dims ?? {}).length && (
                                  <Text type="secondary">该轮未存维度快照</Text>
                                )}
                              </div>
                              {detail.snapshot?.reasons?.length ? (
                                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12 }}>
                                  {detail.snapshot.reasons.map((r) => (
                                    <li key={r}>{r}</li>
                                  ))}
                                </ul>
                              ) : (
                                <Text type="secondary">当时未记录入选理由</Text>
                              )}
                            </>
                          )}
                          <div style={{ marginTop: 12, paddingTop: 8, borderTop: `1px solid ${token.colorSplit}` }}>
                            <Text strong style={{ fontSize: 12 }}>
                              {startLabel}之后再次入选 Top30
                            </Text>
                            <div style={{ marginTop: 4 }}>
                              {detail.relist.length ? (
                                detail.relist.map((r) => (
                                  <Tag key={r.date} color="blue" style={{ marginInlineEnd: 4 }}>
                                    {fmtDate(r.date)} 第 {r.rank ?? '-'} 名
                                    {r.score != null ? ` · ${r.score.toFixed(1)}` : ''}
                                  </Tag>
                                ))
                              ) : market === 'hk' ? (
                                <Text type="secondary" style={{ fontSize: 12 }}>
                                  港股不参与 A 股主力建仓打分，不会出现在候选里
                                </Text>
                              ) : hasScore ? (
                                <Text type="secondary" style={{ fontSize: 12 }}>
                                  暂无后续打分数据（只有 {fmtDate(detail.source_date)} 一轮）
                                </Text>
                              ) : (
                                <Text type="secondary" style={{ fontSize: 12 }}>
                                  手动关注，没有打分数据可比对
                                </Text>
                              )}
                            </div>
                          </div>
                        </Card>
                      </Col>
                    </Row>
                  </>
                )}
              </div>
            )}
          </Spin>
        </>
      )}

      <Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          关注功能仅用于跟踪你标记过的标的，起算点为打分给出的建仓日或你手动关注的起算日
          （两者都以当日收盘价为基准）。
          本页只呈现客观行情与当时打分，不预测后市，不构成任何买卖依据。
          区间表现与同期基准（A 股 = 沪深300，港股 = 恒生指数）对比仅作参照，
          历史走势不代表未来收益。
          实时行情视图走公开行情接口实时拉取，口径与延迟以数据源为准；
          未通过校验的字段一律留空，不做补齐，也不会给出任何买卖提示。
        </Text>
      </Paragraph>
    </Card>
  )
}
