/**
 * 「实时行情」视图 —— 关注股票页的第二个 Tab。
 *
 * 与日线复盘视图的分工：
 *   日线复盘  盘后回看，看的是**已发生**的区间表现与建仓理由
 *   实时行情  盘中盯盘，看的是**正在发生**的现价、分时与资金流
 * 两者共用同一份关注列表（添加表单与免责声明都在 Tab 外），
 * 所以这里只接收 `items`，不自己拉列表。
 *
 * 取数 `api.realtime(codes, detail)` → `GET /api/realtime`：**只查列表里这些
 * 代码**，一次也不扫全市场。上游是腾讯的批量行情（A 股一批、港股一批、资金流
 * 一批，外加当前选中那只的分时），字段在后端逐个过不变量校验。
 *
 * ## 铁律：null 就显示「—」，绝不填默认值
 *
 * 后端对任何过不了校验的字段都返回 null（涨跌额对不上「现价−昨收」、最高价低于
 * 现价、成交额与成交量×价格差一个数量级…）。补一个 0 出去，会把「校验没过」和
 * 「今天真的没成交」变成同一件事 —— 比空着更害人。
 *
 * ## 轮询
 *
 * `load` 写成 promise 链而不是 async/await：effect 体里的 `void load()` 因此
 * 不含任何同步 setState，`react(set-state-in-effect)` 才不会亮（`News` 与
 * `DataManage` 的首屏加载都踩过这个坑，注释里记着同样的话 —— `async` 函数
 * 体会被整体视作同步，`await` 之后的 setState 也躲不掉）。
 * 另外 `seq` 用来丢掉迟到的旧响应 —— 慢的那一轮晚回来时，不能把新的覆盖掉。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Segmented,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
  theme,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined } from '@ant-design/icons'
import StockCode from './StockCode'
import {
  api,
  type LiveFlow,
  type LiveQuote,
  type Market,
  type RealtimeResponse,
  type WatchItem,
} from '../lib/api'
import { SESSION_LABEL, sessionState } from '../lib/live'
import { cmp, compact, pctText, signColor } from '../lib/format'
import { useEChart } from '../lib/useEChart'
import type { EChartsCoreOption } from 'echarts/core'

const { Text } = Typography

/** null 安全的两位小数。注意不能写成 `(v ?? 0).toFixed(2)` —— 那会把 null 变成 0 */
const fmt2 = (v: number | null | undefined) => (v == null ? '—' : v.toFixed(2))

/** 振幅不是接口给的，是 (最高−最低)/昨收 算出来的 —— 任一端缺失就没有振幅 */
const ampOf = (r: LiveQuote) =>
  r.high == null || r.low == null || !r.prev_close
    ? null
    : ((r.high - r.low) / r.prev_close) * 100

export default function LiveBoard({ items }: { items: WatchItem[] }) {
  const { token } = theme.useToken()

  const [auto, setAuto] = useState(true)
  const [sec, setSec] = useState(5)
  const [tick, setTick] = useState(0)
  const [selected, setSelected] = useState<string | null>(null)
  const [data, setData] = useState<RealtimeResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  // 时钟跟数据一起走：把自动刷新关掉时，面板整个冻结，状态标签也不该自己往前跳
  const [now, setNow] = useState(() => new Date())
  const seq = useRef(0)

  const codes = useMemo(() => items.map((i) => i.code), [items])
  const codeKey = codes.join(',')

  // 选中项在渲染期推导，被删掉/不在当前列表就落到第一个 —— 与日线视图同一套口径，
  // 不用 effect 同步（那样 items 每变一次就多渲染一轮）。
  const active =
    selected && items.some((i) => i.code === selected) ? selected : (items[0]?.code ?? null)

  /**
   * 用 promise 链而不是 async/await —— 与 `News` / `DataManage` 的首屏加载同款：
   * load 体内没有任何**同步** setState，effect 里 `void load()` 才不会被
   * `set-state-in-effect` 判成「在 effect 里同步改状态」（`async` 函数体会被
   * 整体视作同步，`await` 之后的 setState 也躲不掉）。
   *
   * `seq` 用来丢掉迟到的旧响应：慢的那一轮晚回来时不能把新的覆盖掉。
   */
  const load = useCallback(() => {
    if (!codeKey) return Promise.resolve()
    const mine = ++seq.current
    return api
      .realtime(codeKey.split(','), active ?? '')
      .then((res) => {
        if (mine !== seq.current) return
        setData(res)
        setError(null)
      })
      .catch((e: Error) => {
        if (mine !== seq.current) return
        setError(e.message)
      })
      .finally(() => {
        // 失败也推进时钟：盘中/收盘标签该反映真实时间，而不是冻在上一次成功那一刻
        if (mine === seq.current) setNow(new Date())
      })
  }, [codeKey, active])

  useEffect(() => {
    void load()
    if (!auto) return undefined
    const id = window.setInterval(() => void load(), sec * 1000)
    return () => window.clearInterval(id)
  }, [load, auto, sec, tick])

  const quotes = useMemo(
    () => (data?.quotes ?? []).filter((q) => items.some((i) => i.code === q.code)),
    [data, items],
  )
  const flows = useMemo(() => data?.flows ?? {}, [data])
  const warns = useMemo(() => Array.from(new Set(data?.warnings ?? [])), [data])
  /** 分时必须属于当前选中的那只 —— 点行之后上一帧的响应可能还没回来 */
  const intraday = data?.intraday && data.intraday.code === active ? data.intraday : null

  const cur = useMemo(() => quotes.find((q) => q.code === active) ?? null, [quotes, active])
  const flow = cur ? (flows[cur.code] ?? null) : null

  const flowOf = useMemo(() => {
    const m = new Map<string, LiveFlow | null>()
    for (const q of quotes) m.set(q.code, flows[q.code] ?? null)
    return m
  }, [quotes, flows])

  const summary = useMemo(() => {
    let up = 0
    let down = 0
    let unknown = 0
    let amount = 0
    let net = 0
    let flowCount = 0
    for (const q of quotes) {
      if (q.pct == null) unknown++
      else if (q.pct > 0) up++
      else if (q.pct < 0) down++
      if (q.amount != null) amount += q.amount
      const f = flows[q.code]
      if (f) {
        net += f.net
        flowCount++
      }
    }
    return {
      up,
      down,
      unknown,
      flat: quotes.length - up - down - unknown,
      amount,
      net,
      flowCount,
    }
  }, [quotes, flows])

  const market: Market = cur?.market ?? (items[0]?.market === 'hk' ? 'hk' : 'cn')
  const st = sessionState(market, now)

  // ------------------------------------------------------------ 分时走势
  const intradayOption = useMemo<EChartsCoreOption | null>(() => {
    const d = intraday
    const pc = d?.prev_close ?? cur?.prev_close ?? null
    if (!d || !d.times.length || !pc) return null
    const { times, prices, avg } = d
    // 后端只在「等长且每根都解析得出正数」时才给 avg，这里再挡一道，免得半截线画上去
    const hasAvg = !!avg && avg.length === prices.length
    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params: unknown) => {
          const list = (params ?? []) as { axisValue: string }[]
          const i = times.indexOf(list[0]?.axisValue ?? '')
          const p = prices[i]
          const lines = [list[0]?.axisValue ?? '']
          if (p != null) {
            const chg = ((p / pc - 1) * 100).toFixed(2)
            lines.push(`价格 ${p.toFixed(2)}　${Number(chg) >= 0 ? '+' : ''}${chg}%`)
            if (hasAvg && avg && avg[i] != null) lines.push(`均价 ${avg[i].toFixed(2)}`)
          }
          return lines.join('<br/>')
        },
      },
      legend: {
        data: hasAvg ? ['价格', '均价'] : ['价格'],
        top: 0,
        right: 0,
        textStyle: { fontSize: 11 },
      },
      grid: { left: 60, right: 52, top: 26, bottom: 34 },
      xAxis: {
        type: 'category',
        data: times,
        boundaryGap: false,
        axisLine: { lineStyle: { color: token.colorBorder } },
        axisLabel: { fontSize: 10, color: token.colorTextTertiary },
      },
      yAxis: {
        type: 'value',
        scale: true,
        splitLine: { lineStyle: { type: 'dashed', color: token.colorBorder } },
        axisLabel: {
          fontSize: 10,
          color: token.colorTextTertiary,
          formatter: (v: number) => v.toFixed(2),
        },
      },
      series: [
        {
          name: '价格',
          type: 'line',
          data: prices,
          showSymbol: false,
          z: 3,
          lineStyle: { width: 1.4, color: token.colorPrimary },
          itemStyle: { color: token.colorPrimary },
          // 昨收参考线：盘中一眼看出现在相对昨收的高低
          markLine: {
            silent: true,
            symbol: 'none',
            lineStyle: { type: 'dashed', color: token.colorTextTertiary },
            label: {
              formatter: `昨收 ${pc.toFixed(2)}`,
              fontSize: 10,
              color: token.colorTextTertiary,
              position: 'insideEndTop',
            },
            data: [{ yAxis: pc }],
          },
        },
        ...(hasAvg && avg
          ? [
              {
                name: '均价',
                type: 'line',
                data: avg,
                showSymbol: false,
                lineStyle: { width: 1, type: 'dashed', color: token.colorWarning },
              },
            ]
          : []),
      ],
    }
  }, [intraday, cur, token])

  const chartRef = useEChart(intradayOption)

  // ------------------------------------------------------------ 表格列
  const columns: ColumnsType<LiveQuote> = useMemo(
    () => [
      {
        title: '代码 / 名称',
        dataIndex: 'code',
        width: 150,
        render: (_, r) => <StockCode code={r.code} name={r.name} market={r.market} />,
      },
      {
        title: '现价',
        dataIndex: 'price',
        width: 92,
        align: 'right',
        sorter: (a, b) => a.price - b.price,
        render: (_, r) => (
          <Text strong style={{ color: signColor(r.pct, token) }}>
            {r.price.toFixed(2)}
          </Text>
        ),
      },
      {
        title: '涨跌幅',
        dataIndex: 'pct',
        width: 96,
        align: 'right',
        defaultSortOrder: 'descend',
        sorter: (a, b) => cmp(a.pct, b.pct),
        render: (_, r) =>
          r.pct == null ? (
            <Text type="secondary">—</Text>
          ) : (
            <Text strong style={{ color: signColor(r.pct, token) }}>
              {pctText(r.pct)}
            </Text>
          ),
      },
      {
        title: '涨跌额',
        dataIndex: 'change',
        width: 84,
        align: 'right',
        sorter: (a, b) => cmp(a.change, b.change),
        render: (_, r) =>
          r.change == null ? (
            <Text type="secondary">—</Text>
          ) : (
            <Text style={{ color: signColor(r.change, token) }}>{r.change.toFixed(2)}</Text>
          ),
      },
      {
        title: '最高',
        dataIndex: 'high',
        width: 76,
        align: 'right',
        sorter: (a, b) => cmp(a.high, b.high),
        render: (_, r) =>
          r.high == null ? (
            <Text type="secondary">—</Text>
          ) : (
            <Text style={{ color: token.colorError }}>{r.high.toFixed(2)}</Text>
          ),
      },
      {
        title: '最低',
        dataIndex: 'low',
        width: 76,
        align: 'right',
        sorter: (a, b) => cmp(a.low, b.low),
        render: (_, r) =>
          r.low == null ? (
            <Text type="secondary">—</Text>
          ) : (
            <Text style={{ color: token.colorSuccess }}>{r.low.toFixed(2)}</Text>
          ),
      },
      {
        title: '成交量',
        dataIndex: 'volume',
        width: 88,
        align: 'right',
        // 单位是「手」且仅 A 股有值 —— 后端拿「成交额 ≈ 成交量×价格」验过量级才敢给，
        // 港股每手股数不固定、证不了单位，所以那一列在港股恒为「—」
        sorter: (a, b) => cmp(a.volume, b.volume),
        render: (_, r) =>
          r.volume == null ? <Text type="secondary">—</Text> : `${compact(r.volume)}手`,
      },
      {
        title: '成交额',
        dataIndex: 'amount',
        width: 96,
        align: 'right',
        sorter: (a, b) => cmp(a.amount, b.amount),
        render: (_, r) =>
          r.amount == null ? <Text type="secondary">—</Text> : compact(r.amount),
      },
      {
        title: '换手',
        dataIndex: 'turnover',
        width: 78,
        align: 'right',
        sorter: (a, b) => cmp(a.turnover, b.turnover),
        render: (_, r) =>
          r.turnover == null ? <Text type="secondary">—</Text> : `${r.turnover.toFixed(2)}%`,
      },
      {
        title: '振幅',
        dataIndex: 'amp',
        width: 74,
        align: 'right',
        sorter: (a, b) => cmp(ampOf(a), ampOf(b)),
        render: (_, r) => {
          const v = ampOf(r)
          return v == null ? <Text type="secondary">—</Text> : `${v.toFixed(2)}%`
        },
      },
      {
        title: '资金净流入',
        key: 'flow',
        width: 108,
        align: 'right',
        sorter: (a, b) => cmp(flowOf.get(a.code)?.net, flowOf.get(b.code)?.net),
        render: (_, r) => {
          const f = flowOf.get(r.code)
          if (!f) return <Text type="secondary">—</Text>
          return (
            <Text style={{ color: signColor(f.net, token) }}>
              {f.net >= 0 ? '+' : ''}
              {compact(f.net)}
            </Text>
          )
        },
      },
      {
        title: '更新',
        dataIndex: 'updated_at',
        width: 86,
        align: 'right',
        render: (_, r) => <Text type="secondary">{r.updated_at ?? '—'}</Text>,
      },
    ],
    [flowOf, token],
  )

  if (!items.length) {
    return (
      <Empty description="还没有关注的股票">
        <Text type="secondary">
          在上面贴 6 位 A 股代码或 1~5 位港股代码添加。加入后这里会实时刷新它们的现价、
          分时走势与资金流向。
        </Text>
      </Empty>
    )
  }

  const stColor =
    st === 'open' ? 'green' : st === 'closed' ? 'default' : st === 'break' ? 'orange' : 'blue'
  const flowTotal = flow ? flow.inflow + flow.outflow : 0
  const inPct = flow && flowTotal > 0 ? (flow.inflow / flowTotal) * 100 : 50

  return (
    <div>
      {/* 工具条：状态 + 新鲜度在左，轮询控制在右 */}
      <div
        style={{
          display: 'flex',
          gap: 10,
          flexWrap: 'wrap',
          alignItems: 'center',
          marginBottom: 12,
        }}
      >
        <Tag color={stColor} style={{ margin: 0 }}>
          {SESSION_LABEL[st]}
        </Tag>
        <Text type="secondary" style={{ fontSize: 12 }}>
          最后更新 {data?.as_of ?? '--:--:--'} · 关注 {items.length} 只
        </Text>
        <Tooltip title="报价与资金流来自腾讯公开行情接口，分时来自腾讯分时接口。所有字段在后端逐个过不变量校验，过不了的显示为「—」，不做补齐。">
          <Tag style={{ margin: 0, cursor: 'help' }}>公开行情接口</Tag>
        </Tooltip>
        <div style={{ flex: 1, minWidth: 8 }} />
        <Text type="secondary" style={{ fontSize: 12 }}>
          自动刷新
        </Text>
        <Switch size="small" checked={auto} onChange={setAuto} />
        <Segmented
          size="small"
          value={sec}
          disabled={!auto}
          onChange={(v) => setSec(v as number)}
          options={[
            { label: '3秒', value: 3 },
            { label: '5秒', value: 5 },
            { label: '10秒', value: 10 },
          ]}
        />
        <Button size="small" icon={<ReloadOutlined />} onClick={() => setTick((t) => t + 1)}>
          立即刷新
        </Button>
      </div>

      {error && (
        <Alert
          type="error"
          showIcon
          title={`实时行情拉取失败：${error}`}
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 后端的降级原因原样摊出来 —— 静默吞掉的话，「—」就没法解释了 */}
      {warns.length > 0 && (
        <Alert
          type="warning"
          showIcon
          title={`数据源降级 ${warns.length} 项，对应字段显示为「—」`}
          description={
            <div style={{ maxHeight: 104, overflowY: 'auto' }}>
              {warns.slice(0, 8).map((w) => (
                <div key={w} style={{ fontSize: 12 }}>
                  · {w}
                </div>
              ))}
              {warns.length > 8 && (
                <div style={{ fontSize: 12, color: token.colorTextTertiary }}>
                  …等共 {warns.length} 条
                </div>
              )}
            </div>
          }
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 涨跌家数与总量：盘面一览 */}
      <div
        style={{
          display: 'flex',
          gap: 24,
          flexWrap: 'wrap',
          marginBottom: 12,
          padding: '8px 12px',
          background: token.colorFillQuaternary,
          borderRadius: 6,
          fontSize: 12,
        }}
      >
        <Text>
          <Text type="secondary">上涨</Text>{' '}
          <Text strong style={{ color: token.colorError }}>
            {summary.up}
          </Text>
        </Text>
        <Text>
          <Text type="secondary">下跌</Text>{' '}
          <Text strong style={{ color: token.colorSuccess }}>
            {summary.down}
          </Text>
        </Text>
        <Text>
          <Text type="secondary">平盘</Text> <Text strong>{summary.flat}</Text>
        </Text>
        {summary.unknown > 0 && (
          <Text>
            <Text type="secondary">涨跌未取到</Text>{' '}
            <Text strong>{summary.unknown}</Text>
          </Text>
        )}
        <Text>
          <Text type="secondary">合计成交额</Text> <Text strong>{compact(summary.amount)}</Text>
        </Text>
        {summary.flowCount > 0 && (
          <Text>
            <Text type="secondary">合计净流入</Text>{' '}
            <Text strong style={{ color: signColor(summary.net, token) }}>
              {summary.net >= 0 ? '+' : ''}
              {compact(summary.net)}
            </Text>
          </Text>
        )}
      </div>

      <Table<LiveQuote>
        rowKey="code"
        size="small"
        columns={columns}
        dataSource={quotes}
        loading={!data && !error}
        pagination={false}
        scroll={{ x: 1104 }}
        onRow={(r) => ({ onClick: () => setSelected(r.code) })}
        rowClassName={(r) => (r.code === active ? 'ant-table-row-selected' : '')}
        style={{ marginBottom: 16 }}
      />

      {!cur ? (
        <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选中一只股票查看分时与资金流" />
      ) : (
        <>
          <div style={{ display: 'flex', gap: 8, alignItems: 'baseline', marginBottom: 10 }}>
            <Text strong style={{ fontSize: 15 }}>
              {cur.name}
            </Text>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {cur.code}
            </Text>
            {cur.market === 'hk' && <Tag color="blue">港股</Tag>}
            <Text strong style={{ color: signColor(cur.pct, token), fontSize: 18 }}>
              {cur.price.toFixed(2)}
            </Text>
            {cur.change != null && cur.pct != null && (
              <Text style={{ color: signColor(cur.pct, token), fontSize: 14 }}>
                {cur.change >= 0 ? '+' : ''}
                {cur.change.toFixed(2)}　{pctText(cur.pct)}
              </Text>
            )}
            <Text type="secondary" style={{ fontSize: 12 }}>
              更新于 {cur.updated_at ?? data?.as_of ?? '—'}
            </Text>
          </div>

          <Row gutter={[12, 12]} style={{ marginBottom: 8 }}>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                今开
              </Text>
              <div style={{ fontSize: 16 }}>{fmt2(cur.open)}</div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                昨收
              </Text>
              <div style={{ fontSize: 16 }}>{fmt2(cur.prev_close)}</div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                最高
              </Text>
              <div style={{ fontSize: 16, color: cur.high == null ? undefined : token.colorError }}>
                {fmt2(cur.high)}
              </div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                最低
              </Text>
              <div style={{ fontSize: 16, color: cur.low == null ? undefined : token.colorSuccess }}>
                {fmt2(cur.low)}
              </div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                成交量
              </Text>
              <div style={{ fontSize: 16 }}>
                {cur.volume == null ? '—' : `${compact(cur.volume)}手`}
              </div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                成交额
              </Text>
              <div style={{ fontSize: 16 }}>{cur.amount == null ? '—' : compact(cur.amount)}</div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                换手率
              </Text>
              <div style={{ fontSize: 16 }}>
                {cur.turnover == null ? '—' : `${cur.turnover.toFixed(2)}%`}
              </div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                振幅
              </Text>
              <div style={{ fontSize: 16 }}>
                {(() => {
                  const v = ampOf(cur)
                  return v == null ? '—' : `${v.toFixed(2)}%`
                })()}
              </div>
            </Col>
            <Col xs={12} sm={8} md={4}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                市盈率
              </Text>
              <div style={{ fontSize: 16 }}>{cur.pe == null ? '—' : cur.pe.toFixed(1)}</div>
            </Col>
          </Row>

          <Row gutter={[12, 12]}>
            <Col xs={24} lg={15}>
              <Card size="small" title={`分时走势 · ${cur.name}`}>
                {intradayOption ? (
                  <>
                    <div ref={chartRef} style={{ width: '100%', height: 380 }} />
                    <Text type="secondary" style={{ fontSize: 11, display: 'block', marginTop: 6 }}>
                      只画价格与均价 —— 分时接口给的成交量，其单位与是否累计都未经验证，
                      画出来可能是错的，验过再加。
                    </Text>
                  </>
                ) : (
                  <Empty
                    image={Empty.PRESENTED_IMAGE_SIMPLE}
                    description="未取到当日分时（源未返回，或结构未通过校验）"
                  />
                )}
              </Card>
            </Col>
            <Col xs={24} lg={9}>
              <Card size="small" title="资金流向（当日）">
                {!flow ? (
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {cur.market === 'hk'
                      ? '港股不提供个股资金流（该口径只覆盖 A 股），这里只给行情。'
                      : '本轮未取到该股资金流 —— 源接口未返回，或字段未通过「净额 = 流入 − 流出」校验。'}
                  </Text>
                ) : (
                  <>
                    <div style={{ display: 'flex', alignItems: 'baseline', gap: 8 }}>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        净流入
                      </Text>
                      <Text strong style={{ color: signColor(flow.net, token), fontSize: 20 }}>
                        {flow.net >= 0 ? '+' : ''}
                        {compact(flow.net)}
                      </Text>
                      <Text style={{ fontSize: 12, color: signColor(flow.ratio, token) }}>
                        {pctText(flow.ratio)}
                      </Text>
                    </div>
                    <div
                      style={{
                        display: 'flex',
                        height: 8,
                        borderRadius: 4,
                        overflow: 'hidden',
                        marginTop: 8,
                        background: token.colorFillTertiary,
                      }}
                      title="左=流入，右=流出"
                    >
                      <div style={{ width: `${inPct}%`, background: token.colorError }} />
                      <div style={{ flex: 1, background: token.colorSuccess }} />
                    </div>
                    <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        流入 {compact(flow.inflow)}
                      </Text>
                      <Text type="secondary" style={{ fontSize: 11 }}>
                        流出 {compact(flow.outflow)}
                      </Text>
                    </div>
                    <Text type="secondary" style={{ fontSize: 12, display: 'block', marginTop: 10 }}>
                      口径：源接口的段序未经实测，本页只保证「净流入 = 流入 − 流出」这一条
                      不变量，因此不标主力 / 散户。港股无此口径。
                    </Text>
                  </>
                )}
              </Card>
            </Col>
          </Row>
        </>
      )}
    </div>
  )
}
