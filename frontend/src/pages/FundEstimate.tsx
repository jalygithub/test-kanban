/**
 * 「基金估值」页 —— 左侧新菜单的第一项。
 *
 * ## 它和「关注股票 · 实时行情」的区别
 *
 * 那边盯的是**个股现价**，行情本身就摆在那儿，5 秒轮询一次是合理的；
 * 这边是**盘中估算的净值**，一天只公布一次，盘中那个数是估出来的，不是行情。
 * 所以这里**手动触发** —— 点一次「查询估值」发一次请求，没有轮询、没有
 * 自动刷新。**不点「查询估值」，一个估值包都不发。**
 *
 * 添加框里那个候选搜索是另一码事（`components/SearchPick`）：它是添加的一部分，
 * 只在**打字停顿 400ms** 时触发，不打字就一个包都没有。落库本身仍然零网络，
 * 搜不到也照旧能手贴 6 位代码。
 *
 * ## 铁律：null 就显示「—」，绝不填默认值
 *
 * 后端两条链（天天基金官方估值 / 持仓穿透）各自带不变量校验，过不了就置 null。
 * 补一个 0 出去，会把「没取到」和「今天真的没涨跌」变成同一件事 —— 比空着更害人。
 *
 * 两条链能给的东西**不一样**，这是刻意的，不是 bug：
 *   官方估值  有估算净值和最新净值，没有覆盖率
 *   持仓穿透  只有涨跌幅 + 覆盖率（算涨跌幅不需要昨收，算净值需要，而昨收只有
 *             官方那个接口才有 —— 两条链挂一个才轮到另一条）
 * 所以同一行里有的列是数、有的列是「—」，完全正常。
 *
 * ## 涨跌幅这一列为什么只有一个
 *
 * 用户拍板的口径（2026-10-13）：交易中显示估算；盘前显示前一交易日的净值；
 * 收盘后当日机构净值出了显示实际，没出仍显示估算。所以这里**只放一个会切换的
 * 涨跌幅**，来源挂一枚「估算 / 实际」Tag，两个数各自的时点连同切换理由塞进
 * Tooltip。第三条净值历史链只服务这件事：拿不到就让它退回估算，估值本身照常。
 *
 * ## 首屏加载为什么写成 promise 链
 *
 * `react(set-state-in-effect)` 会把 effect 里**同步**发生的 setState 判成违规
 * —— `async` 函数体会被整体视作同步，`await` 之后的 setState 也躲不掉。
 * 全站基线是恰好 6 条（Watchlist 的首屏加载占了两条），新页面不能加第 7 条。
 * 所以 `loadList` 里第一个动作是 `api.fundWatchList()` 这个 promise 本身，
 * 任何 setState 都落在 `.then` / `.finally` 里。`listReady` 也是同理 ——
 * 在 `.finally` 里翻，不在函数体开头同步翻。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
  theme,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, PlusOutlined, SearchOutlined } from '@ant-design/icons'
import SearchPick from '../components/SearchPick'
import {
  ApiError,
  api,
  type FundEstimate,
  type FundEstimateResponse,
  type FundReviewResponse,
  type FundReviewRow,
  type FundWatchItem,
  type SearchItem,
} from '../lib/api'
import { SESSION_LABEL, sessionState, type SessionState } from '../lib/live'
import { cmp, pctText, signType } from '../lib/format'

const { Text, Paragraph } = Typography

/** null 安全的四位小数。**不能写 `(v ?? 0).toFixed(4)`** —— 那会把 null 变成 0 */
const fmt4 = (v: number | null | undefined) => (v == null ? '—' : v.toFixed(4))

/**
 * 误差单位是**百分点（pp）不是百分比**，所以后缀写 pp —— 写成 `%` 会让人以为
 * 是「相对误差」。正 = 高估，负 = 低估。
 */
const ppText = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(2)}pp`

/** 还没查过（或该只不在结果里）时的空行。字段与后端 `_empty()` 对齐。 */
const blankRow = (item: FundWatchItem): FundEstimate => ({
  code: item.code,
  source: '',
  name: item.name,
  nav: null,
  nav_date: null,
  estimate_nav: null,
  estimate_pct: null,
  actual_pct: null,
  actual_date: null,
  display_pct: null,
  display_kind: 'none',
  display_reason: '',
  coverage: null,
  as_of: null,
  stale: false,
  note: null,
})

export default function FundEstimate() {
  const { token } = theme.useToken()

  // ---- 关注列表（本地 SQLite，不联网）
  const [items, setItems] = useState<FundWatchItem[]>([])
  const [listReady, setListReady] = useState(false)
  const [addCode, setAddCode] = useState('')
  const [addName, setAddName] = useState('')
  const [adding, setAdding] = useState(false)

  // ---- 估值查询（手动触发）
  const [data, setData] = useState<FundEstimateResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  /** 查询那一刻的交易时段。存下来而不是渲染期现算 —— `new Date()` 在渲染里
   *  属于不纯调用，React 编译器会因此跳过整个组件的记忆化。 */
  const [session, setSession] = useState<SessionState | null>(null)
  /** 丢掉迟到的旧响应：慢的那一轮晚回来时，不能把新的覆盖掉 */
  const seq = useRef(0)

  // ---- T+1 误差复盘（**只读本地 SQLite，零网络**）
  const [review, setReview] = useState<FundReviewResponse | null>(null)
  const [reviewReady, setReviewReady] = useState(false)
  const [reviewErr, setReviewErr] = useState<string | null>(null)

  // ---- 首屏回读（本地库）失败时的原因。**与 `error` 分开**：后者是「查询估值」
  //      联网取数失败，混在一起会让「读本地缓存没读到」看起来像数据源挂了。
  const [cachedErr, setCachedErr] = useState<string | null>(null)

  const loadList = useCallback(() => {
    return api
      .fundWatchList()
      .then((res) => {
        setItems(res.items)
        setError(null)
      })
      .catch((e) => setError((e as ApiError).message))
      .finally(() => setListReady(true))
  }, [])

  /**
   * 复盘跟列表**一起在首屏拉**：它只读本地 SQLite，一个外网包都不发，
   * 所以不违反「不点查询就不发包」这条承诺。
   */
  const loadReview = useCallback(() => {
    return api
      .fundReview()
      .then((res) => setReview(res))
      .catch((e) => setReviewErr((e as ApiError).message))
      .finally(() => setReviewReady(true))
  }, [])

  /**
   * 首屏回读上次查询的结果，同样只读本地库、零网络 —— 流水本来每次查询都
   * 在写，缺的只是回读这一步，于是页面每次打开都得重新查一遍。
   *
   * 三点约束：
   * * **走同一个 `seq`**：用户在回读返回前就点了「查询估值」时，迟到的旧
   *   结果不能把刚查出来的真实值盖掉。
   * * **`items` 为空不落库**：一只都没查过时保持 `data === null`，下面那句
   *   「点右上角查询估值」才还在 —— 回读一个空结果会把引导语吃掉。
   * * **失败走独立的 `cachedErr`**：那是 warning 通道，`error` 的红色 Alert
   *   是**联网**查询失败专用 —— 混进去会让「读本地缓存没读到」看起来像数据源
   *   挂了。`data` 仍是 `null`，表现退回「首屏空着」的既有样子；真故障会由
   *   「查询估值」那条路径如实报出来，这里不重试。
   */
  const loadCached = useCallback(() => {
    const mine = ++seq.current
    return api
      .fundEstimateLast()
      .then((res) => {
        if (mine !== seq.current) return
        setCachedErr(null)
        if (res.items.length) setData(res)
      })
      .catch((e) => {
        if (mine !== seq.current) return
        setCachedErr(e instanceof ApiError ? e.message : String(e))
      })
  }, [])

  useEffect(() => {
    void loadList()
    void loadReview()
    void loadCached()
  }, [loadList, loadReview, loadCached])

  const codes = useMemo(() => items.map((i) => i.code), [items])

  /** 手动查询。**没有自动触发** —— 只从按钮的 onClick 进来，所以这里同步
   *  setState 不受 `set-state-in-effect` 约束（那条规则只管 effect）。 */
  const runQuery = useCallback(() => {
    if (!codes.length) return
    const mine = ++seq.current
    setLoading(true)
    setError(null)
    setCachedErr(null)
    setSession(sessionState('cn', new Date()))
    api
      .fundEstimate(codes)
      .then((res) => {
        if (mine !== seq.current) return
        setData(res)
      })
      .catch((e) => {
        if (mine !== seq.current) return
        setError(e instanceof ApiError ? e.message : String(e))
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false)
      })
  }, [codes])

  /**
   * 真正落库的那一步。参数**不是 state** —— 搜索选中时 code/name 来自候选，
   * 而 setState 是异步的，「先 set 再读」必然读到上一轮的旧值，加错对象。
   */
  const addOne = useCallback(
    async (raw: string, name: string) => {
      const code = raw.trim()
      if (!/^\d{6}$/.test(code)) {
        setError('基金代码应为 6 位数字，如 161725')
        return false
      }
      if (items.some((i) => i.code === code)) {
        setError(`${code} 已在关注列表里`)
        return false
      }
      setAdding(true)
      try {
        // 后端纯本地写库、不联网 —— 名称留空也照样成功
        await api.fundWatchAdd(code, name.trim())
        setAddCode('')
        setAddName('')
        setError(null)
        await loadList()
        return true
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e))
        return false
      } finally {
        setAdding(false)
      }
    },
    [items, loadList],
  )

  /** 手输路径：读两个输入框。 */
  const addFund = useCallback(() => addOne(addCode, addName), [addOne, addCode, addName])

  /** 搜索选中路径：候选自带代码与名称，框里的名称不掺和。 */
  const pickFund = useCallback((it: SearchItem) => void addOne(it.code, it.name), [addOne])

  /** 回车且无候选可选中 —— 名称框里填了什么就用什么，留空交后端回填。 */
  const enterFund = useCallback((text: string) => void addOne(text, addName), [addOne, addName])

  const removeFund = useCallback(
    async (code: string) => {
      try {
        await api.fundWatchRemove(code)
        setData((prev) =>
          prev ? { ...prev, items: prev.items.filter((i) => i.code !== code) } : prev,
        )
        await loadList()
      } catch (e) {
        setError(e instanceof ApiError ? e.message : String(e))
      }
    },
    [loadList],
  )

  /**
   * 表格行 = **关注列表**，估值结果按代码并进来。
   *
   * 方向是「以列表为准」：删掉一只它的估值行跟着消失，新添加的那只会先以
   * 空行出现（等下次查询再填）。反过来以查询结果为准的话，删完再刷新页面
   * 会看到一只已经不在列表里的基金 —— 那是更坏的错觉。
   */
  const rows = useMemo(
    () =>
      items.map((item) => {
        const hit = data?.items.find((x) => x.code === item.code)
        return hit ? { ...hit, name: hit.name || item.name } : blankRow(item)
      }),
    [items, data],
  )

  /** 来源标签。两条链都失败时标红，并指到上方的降级说明 —— 「—」得有解释。 */
  const sourceCell = (r: FundEstimate) => {
    if (!r.source) {
      return (
        <Tooltip title="官方估值与持仓穿透两条链都没拿到数，原因见上方提示">
          <Tag color="red">未取到</Tag>
        </Tooltip>
      )
    }
    const tag = (
      <Tag color={r.source === '官方估值' ? 'blue' : 'orange'}>{r.source}</Tag>
    )
    return r.note ? <Tooltip title={r.note}>{tag}</Tooltip> : tag
  }

  /**
   * 涨跌幅主列。**显示谁由后端按时段定**（规则见 `routes._attach_display`）：
   * 交易中→估算、盘前→前一交易日净值、收盘后机构净值出了→实际、没出→估算。
   *
   * 前端只做两件事：把来源标出来；把「为什么是它」连同**估算与实际各自的时点**
   * 一起摊进 Tooltip。两者差 1 个百分点是常态，不带时点地并排只会让人拿今天的
   * 估算去比昨天的净值 —— 那正是「差 1 个点」的来源之一。
   */
  const displayCell = (r: FundEstimate) => {
    const kindTag =
      r.display_kind === 'actual' ? (
        <Tag color="green" style={{ marginInlineEnd: 0 }}>
          实际
        </Tag>
      ) : r.display_kind === 'estimate' ? (
        <Tag color="blue" style={{ marginInlineEnd: 0 }}>
          估算
        </Tag>
      ) : null

    const lines: string[] = []
    if (r.display_reason) lines.push(r.display_reason)
    if (r.estimate_pct != null)
      lines.push(`估算 ${pctText(r.estimate_pct)}${r.as_of ? `（时点 ${r.as_of}）` : ''}`)
    if (r.actual_pct != null)
      lines.push(`实际 ${pctText(r.actual_pct)}（净值日期 ${r.actual_date ?? '—'}）`)

    const body = (
      <Space size={4}>
        {r.display_pct == null ? (
          <span>—</span>
        ) : (
          <Text type={signType(r.display_pct)}>{pctText(r.display_pct)}</Text>
        )}
        {kindTag}
      </Space>
    )
    if (!lines.length) return body
    return (
      <Tooltip
        title={
          <div>
            {lines.map((l) => (
              <div key={l}>{l}</div>
            ))}
          </div>
        }
      >
        <span>{body}</span>
      </Tooltip>
    )
  }

  const columns: ColumnsType<FundEstimate> = [
    {
      title: '基金代码',
      dataIndex: 'code',
      width: 100,
      render: (v: string) => <Text code>{v}</Text>,
    },
    {
      title: '名称',
      dataIndex: 'name',
      ellipsis: true,
      render: (v: string, r) =>
        v ? <Text>{v}</Text> : <Text type="secondary">{r.code}</Text>,
    },
    {
      title: (
        <Tooltip title="按时段自动切换：交易中显示估算；盘前显示前一交易日的净值；收盘后当日机构净值出了就显示实际，没出仍显示估算。悬停数值可看两个数各自的时点">
          涨跌幅
        </Tooltip>
      ),
      dataIndex: 'display_pct',
      width: 150,
      align: 'right',
      sorter: (a, b) => cmp(a.display_pct, b.display_pct),
      render: (_: number | null, r) => displayCell(r),
    },
    {
      title: '估算净值',
      dataIndex: 'estimate_nav',
      width: 110,
      align: 'right',
      sorter: (a, b) => cmp(a.estimate_nav, b.estimate_nav),
      render: (v: number | null) => fmt4(v),
    },
    {
      title: '最新净值',
      dataIndex: 'nav',
      width: 130,
      align: 'right',
      render: (v: number | null, r) =>
        v == null ? (
          '—'
        ) : (
          <Tooltip title={r.nav_date ? `净值日期 ${r.nav_date}` : '净值日期未取到'}>
            <span>{fmt4(v)}</span>
          </Tooltip>
        ),
    },
    {
      title: '数据来源',
      dataIndex: 'source',
      width: 130,
      render: (_: string, r) => sourceCell(r),
    },
    {
      title: '估值时点',
      dataIndex: 'as_of',
      width: 170,
      render: (v: string | null, r) => {
        const stamp = v ?? data?.as_of ?? null
        return (
          <Space size={4}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {stamp ?? '—'}
            </Text>
            {r.stale && (
              <Tooltip title="估值时点不是今天 —— 周一早上源还挂着上周五的估值是常态，别当今天的现值看">
                <Tag color="orange" style={{ marginInlineEnd: 0 }}>
                  非今日
                </Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: '',
      key: 'op',
      width: 64,
      align: 'right',
      render: (_: unknown, r) => (
        <Popconfirm
          title={`取消关注 ${r.code}？`}
          okText="取消关注"
          cancelText="再想想"
          onConfirm={() => void removeFund(r.code)}
        >
          <Button type="text" size="small" danger icon={<DeleteOutlined />} />
        </Popconfirm>
      ),
    },
  ]

  /**
   * 复盘表的列。
   *
   * 误差列沿用全站「正数偏暖」的配色（`signType`），但**正负的含义与涨跌无关**
   * —— 正 = 高估、负 = 低估。所以每个误差表头都挂了 Tooltip 说明，不靠颜色
   * 单独传递语义。
   */
  const reviewColumns: ColumnsType<FundReviewRow> = [
    {
      title: '基金代码',
      dataIndex: 'code',
      width: 100,
      render: (v: string) => <Text code>{v}</Text>,
    },
    {
      title: '名称',
      dataIndex: 'name',
      ellipsis: true,
      render: (v: string, r) =>
        v ? <Text>{v}</Text> : <Text type="secondary">{r.code}</Text>,
    },
    {
      title: '数据来源',
      dataIndex: 'source',
      width: 110,
      render: (v: FundReviewRow['source']) => (
        <Tag color={v === '官方估值' ? 'blue' : 'orange'}>{v}</Tag>
      ),
    },
    {
      title: '样本',
      dataIndex: 'samples',
      width: 100,
      align: 'right',
      sorter: (a, b) => a.samples - b.samples,
      render: (v: number, r) => (
        <Space size={4}>
          <span>{v}</span>
          {r.low_sample && (
            <Tooltip
              title={`样本少于 ${review?.low_sample ?? 5} 条，平均值基本是噪声，别据此下结论`}
            >
              <Tag color="orange" style={{ marginInlineEnd: 0 }}>
                样本少
              </Tag>
            </Tooltip>
          )}
        </Space>
      ),
    },
    {
      title: (
        <Tooltip title="平均误差 = 估算涨跌幅 − 实际涨跌幅 的均值。正 = 系统性高估，负 = 系统性低估">
          平均误差
        </Tooltip>
      ),
      dataIndex: 'mean_err',
      width: 112,
      align: 'right',
      sorter: (a, b) => cmp(a.mean_err, b.mean_err),
      render: (v: number) => <Text type={signType(v)}>{ppText(v)}</Text>,
    },
    {
      title: (
        <Tooltip title="平均绝对误差 —— 与有没有系统性偏差无关，纯粹是噪声水平。越小越贴">
          平均绝对误差
        </Tooltip>
      ),
      dataIndex: 'mean_abs_err',
      width: 130,
      align: 'right',
      sorter: (a, b) => cmp(a.mean_abs_err, b.mean_abs_err),
      render: (v: number) => `${v.toFixed(2)}pp`,
    },
    {
      title: <Tooltip title="样本里高估得最狠的一次">最坏高估</Tooltip>,
      dataIndex: 'max_over',
      width: 112,
      align: 'right',
      render: (v: number) => <Text type={signType(v)}>{ppText(v)}</Text>,
    },
    {
      title: <Tooltip title="样本里低估得最狠的一次">最坏低估</Tooltip>,
      dataIndex: 'max_under',
      width: 112,
      align: 'right',
      render: (v: number) => <Text type={signType(v)}>{ppText(v)}</Text>,
    },
    {
      title: (
        <Tooltip title="只有持仓穿透链有：该链样本的平均覆盖率（取到价的前十大重仓股合计占净值比例）">
          覆盖率
        </Tooltip>
      ),
      dataIndex: 'coverage_mean',
      width: 100,
      align: 'right',
      render: (v: number | null) => (v == null ? '—' : `${v.toFixed(1)}%`),
    },
    {
      title: '最近一次',
      dataIndex: 'last_date',
      width: 170,
      render: (v: string, r) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {v || '—'}
          {r.last_err != null ? `（误差 ${ppText(r.last_err)}）` : ''}
        </Text>
      ),
    },
  ]

  return (
    <>
    <Card
      title={
        <Space size={8} wrap>
          <span>基金估值</span>
          {session && (
            <Tag color={session === 'open' ? 'green' : 'default'}>
              A 股{SESSION_LABEL[session]}
            </Tag>
          )}
          <Text type="secondary" style={{ fontSize: 12, fontWeight: 'normal' }}>
            手动查询，不自动刷新
          </Text>
        </Space>
      }
      extra={
        <Space>
          <Tooltip title="手动触发一次估算；不点就不会发出任何外网请求">
            <Button
              type="primary"
              icon={<SearchOutlined />}
              loading={loading}
              disabled={!codes.length}
              onClick={runQuery}
            >
              查询估值
            </Button>
          </Tooltip>
          <Button
            size="small"
            loading={!listReady}
            onClick={() => {
              setListReady(false)
              void loadList()
            }}
          >
            刷新列表
          </Button>
        </Space>
      }
    >
      {error && (
        <Alert
          type="error"
          showIcon
          title={error}
          closable={{ onClose: () => setError(null) }}
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 这一屏是从本地库回读的上次结果，不是刚查的 —— 不标出来就是拿旧数据
          冒充实时。`as_of` 是落库时刻，完整到日期，因为数据可能来自几天前。 */}
      {data?.cached && (
        <Alert
          type="info"
          showIcon
          title="下面是上次查询保存下来的结果，不是刚查的"
          description={
            <span style={{ fontSize: 12 }}>
              落库于 {data.as_of || '未知时间'}。点右上角「查询估值」才会重新
              联网取数；首屏这一屏只读本地数据库，一个网络请求都不发。
            </span>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 回读没读出来时不弹红色错误（那是联网查询失败的通道），退回「首屏空着」
          的表现，但得说清楚为什么空。 */}
      {!data && cachedErr && (
        <Alert
          type="warning"
          showIcon
          title="上次查询的结果没读出来，这一屏暂时是空的"
          description={<span style={{ fontSize: 12 }}>{cachedErr}</span>}
          closable={{ onClose: () => setCachedErr(null) }}
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 后端的降级原因原样摊出来 —— 静默吞掉的话，「—」就没法解释了 */}
      {(data?.warnings.length ?? 0) > 0 && (
        <Alert
          type="warning"
          showIcon
          title={`数据源降级 ${data?.warnings.length} 项，对应字段显示为「—」`}
          description={
            <div style={{ maxHeight: 104, overflowY: 'auto' }}>
              {data!.warnings.slice(0, 8).map((w) => (
                <div key={w} style={{ fontSize: 12 }}>
                  · {w}
                </div>
              ))}
              {data!.warnings.length > 8 && (
                <div style={{ fontSize: 12, color: token.colorTextTertiary }}>
                  … 其余 {data!.warnings.length - 8} 条同上
                </div>
              )}
            </div>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 手动添加：左边既能搜（防抖 400ms、选中即加）也能直接敲 6 位代码回车，
          右边的名称框只服务「敲代码」这条路 —— 走搜索时名称由候选自带。 */}
      <div
        style={{
          display: 'flex',
          gap: 8,
          marginBottom: 12,
          flexWrap: 'wrap',
          alignItems: 'center',
        }}
      >
        <SearchPick
          kind="fund"
          width={260}
          value={addCode}
          onChange={setAddCode}
          placeholder="输代码或名称搜索，选中即添加"
          taken={codes}
          disabled={adding}
          onPick={pickFund}
          onEnter={enterFund}
        />
        <Input
          size="small"
          style={{ width: 180 }}
          placeholder="名称（可留空，查询时自动补）"
          value={addName}
          allowClear
          onChange={(e) => setAddName(e.target.value)}
          onPressEnter={() => void addFund()}
        />
        <Button
          size="small"
          type="primary"
          ghost
          icon={<PlusOutlined />}
          loading={adding}
          onClick={() => void addFund()}
        >
          添加
        </Button>
        <Text type="secondary" style={{ fontSize: 12 }}>
          搜索会联网找候选；添加本身只写本地库，搜不到就直接输代码
        </Text>
      </div>

      {!listReady ? (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <Spin />
        </div>
      ) : !items.length ? (
        <Empty description="还没有关注的基金，在上方输入 6 位代码添加" />
      ) : (
        <Table<FundEstimate>
          rowKey="code"
          size="small"
          columns={columns}
          dataSource={rows}
          pagination={false}
          loading={loading}
          scroll={{ x: 940 }}
          locale={{
            emptyText: (
              <Empty description="还没有关注的基金，在上方输入 6 位代码添加" />
            ),
          }}
        />
      )}

      {listReady && !!items.length && !data && (
        <Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            列表已就绪，点右上角「查询估值」开始估算 —— 不点不会发出任何请求。
          </Text>
        </Paragraph>
      )}

      <Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {data?.disclaimer ??
            '盘中估值由公开数据估算得出，口径与延迟以数据源为准；未通过校验的字段'
              + '一律留空，不做补齐。持仓数据滞后于基金经理实际调仓，估值不代表基金'
              + '真实净值，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。'}
        </Text>
      </Paragraph>
    </Card>

    <Card
      title={
        <Space size={8} wrap>
          <span>T+1 误差复盘</span>
          <Tooltip title="把盘中估出来的涨跌幅，和事后公布的真实涨跌幅对起来，看估算到底准不准。本表只读本地数据库，一个网络请求都不发。">
            <Tag color="default">估算 vs 真实净值</Tag>
          </Tooltip>
        </Space>
      }
      style={{ marginTop: 16 }}
    >
      {reviewErr && (
        <Alert
          type="error"
          showIcon
          title="复盘数据读取失败"
          description={reviewErr}
          closable
          onClose={() => setReviewErr(null)}
          style={{ marginBottom: 12 }}
        />
      )}

      <Paragraph style={{ marginTop: 0, marginBottom: 12 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {review?.notes[0] ??
            '误差 = 估算涨跌幅 − 实际涨跌幅，正 = 高估、负 = 低估。'}
        </Text>
      </Paragraph>

      {!reviewReady ? (
        <div style={{ textAlign: 'center', padding: '48px 0' }}>
          <Spin />
        </div>
      ) : !(review?.items.length ?? 0) ? (
        <Empty
          description={
            <span>
              还没有可对比的样本
              <br />
              <Text type="secondary" style={{ fontSize: 12 }}>
                点一次「查询估值」会记下当天的估算；第二天再点一次，
                前一天的样本就会闭合 —— 样本只在你手动查询的日子产生。
              </Text>
            </span>
          }
        />
      ) : (
        <>
          <Table<FundReviewRow>
            rowKey={(r) => `${r.code}|${r.source}`}
            size="small"
            columns={reviewColumns}
            dataSource={review?.items ?? []}
            pagination={
              (review?.items.length ?? 0) > 15
                ? { pageSize: 15, showSizeChanger: false }
                : false
            }
            scroll={{ x: 1180 }}
          />

          {(review?.notes.length ?? 0) > 1 && (
            <Paragraph style={{ marginTop: 12, marginBottom: 0 }}>
              {(review?.notes ?? []).slice(1).map((n) => (
                <div key={n}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    · {n}
                  </Text>
                </div>
              ))}
            </Paragraph>
          )}
        </>
      )}

      <Paragraph style={{ marginTop: 16, marginBottom: 0 }}>
        <Text type="secondary" style={{ fontSize: 12 }}>
          {review?.disclaimer ??
            '本表仅呈现历史估算与已公布净值的差异，用于数据可视化研究，'
              + '不提供任何买卖信号，不构成投资建议。'}
        </Text>
      </Paragraph>
    </Card>
    </>
  )
}
