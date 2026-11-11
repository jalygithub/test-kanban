import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
  theme,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { EChartsCoreOption } from 'echarts/core'
import { ArrowLeftOutlined, ReloadOutlined, StarFilled, StarOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import StockCode from '../components/StockCode'
import {
  api,
  type InflowDay,
  type LimitRow,
  type MainForceInflow,
  type MainForceRow,
  type PoolType,
  type StockDailyResponse,
  type WatchItem,
} from '../lib/api'
import { useEChart } from '../lib/useEChart'
import { DOWN_COLOR, UP_COLOR, compact, fmtDate, fmtNum, fmtPct, ma, signColor, signType } from '../lib/format'

const { Text } = Typography

const DAILY_DAYS = 90
const INFLOW_DAYS = 10

/** 图表轴上的短日期 MM-DD */
const md = (v: string) => (v.length === 8 ? `${v.slice(4, 6)}-${v.slice(6, 8)}` : v)

/** 小统计卡：只展示一个数 + 一行提示，不用 antd Statistic（v6 弃用了 valueStyle） */
function MiniStat({
  label,
  value,
  color,
  hint,
}: {
  label: string
  value: string
  color?: string
  hint?: string
}) {
  return (
    <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Text>
      <div
        style={{
          fontSize: 26,
          lineHeight: 1.3,
          fontWeight: 600,
          fontVariantNumeric: 'tabular-nums',
          color,
        }}
      >
        {value}
      </div>
      {hint ? (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {hint}
        </Text>
      ) : null}
    </Card>
  )
}

const COL = { xs: 12, sm: 8, md: 4 } as const

export default function StockDetail() {
  const { code = '' } = useParams()
  // 换股时用 key 重建子组件：状态跟着 code 走，
  // 不必在 effect 里同步 setState 清空（那会被 set-state-in-effect 抓）
  return <Detail key={code} code={code} />
}

function Detail({ code }: { code: string }) {
  const { token } = theme.useToken()
  const nav = useNavigate()

  const [daily, setDaily] = useState<StockDailyResponse | null>(null)
  const [pools, setPools] = useState<Partial<Record<PoolType, LimitRow>>>({})
  const [poolDate, setPoolDate] = useState('')
  const [mf, setMf] = useState<{ date: string; row?: MainForceRow } | null>(null)
  const [inflow, setInflow] = useState<MainForceInflow | null>(null)
  const [watchItem, setWatchItem] = useState<WatchItem | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tip, setTip] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    // promise 链 / async 体内第一个动作就是 await：effect 里没有同步 setState，
    // set-state-in-effect 才不会判成「在 effect 里改状态」。
    // 只有走势是硬依赖；池子、打分、资金流各自兜错，缺哪块降级哪块。
    const soft = <T,>(p: Promise<T>) => p.catch(() => null)
    void (async () => {
      try {
        const [d, mfRes, wlRes, zt, zb, dt, strong, inr] = await Promise.all([
          api.stockDaily(code, DAILY_DAYS),
          soft(api.mainForce()),
          soft(api.watchlist()),
          soft(api.limitPool('zt')),
          soft(api.limitPool('zb')),
          soft(api.limitPool('dt')),
          soft(api.limitPool('strong')),
          soft(api.mainForceInflow(undefined, [code], INFLOW_DAYS)),
        ])
        setDaily(d)
        setPools({
          zt: zt?.rows.find((r) => r.code === code),
          zb: zb?.rows.find((r) => r.code === code),
          dt: dt?.rows.find((r) => r.code === code),
          strong: strong?.rows.find((r) => r.code === code),
        })
        setPoolDate(zt?.date ?? '')
        setMf(
          mfRes
            ? { date: mfRes.date, row: (mfRes.all ?? mfRes.rows).find((r) => r.code === code) }
            : null,
        )
        setWatchItem(wlRes?.items.find((i) => i.code === code) ?? null)
        setInflow(inr)
        setError(null)
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setLoading(false)
      }
    })()
  }, [code])

  useEffect(() => {
    void load()
  }, [load])

  // ------------------------------------------------------------ 派生信息
  const rows = useMemo(() => daily?.rows ?? [], [daily])
  const last = rows.length ? rows[rows.length - 1] : undefined
  const first = rows.length ? rows[0] : undefined
  const poolRow = pools.zt ?? pools.zb ?? pools.dt ?? pools.strong
  // 港股在涨跌停池与打分里必然缺席（那两套是 A 股交易制度的产物），
  // 所以名称要从「关注记录」或日线接口回的名字里拿，否则标题只剩一串代码。
  const market: 'cn' | 'hk' =
    daily?.market === 'hk' || /^\d{1,5}$/.test(code) ? 'hk' : 'cn'
  const name =
    mf?.row?.name ?? poolRow?.name ?? watchItem?.name ?? daily?.name ?? ''
  const industry = mf?.row?.industry ?? poolRow?.industry ?? ''

  const periodRet =
    first?.close && last?.close ? ((last.close / first.close) - 1) * 100 : null
  const hi = rows.length ? Math.max(...rows.map((r) => r.high ?? -Infinity)) : null
  const lo = rows.length ? Math.min(...rows.map((r) => r.low ?? Infinity)) : null
  const inflowItem = inflow?.items?.[code]

  const poolTag = (() => {
    const tags = []
    if (pools.zt)
      tags.push(
        <Tag key="zt" color="red">
          涨停{pools.zt.boards ? ` · ${pools.zt.boards}板` : ''}
        </Tag>,
      )
    if (pools.zb)
      tags.push(
        <Tag key="zb" color="orange">
          炸板{pools.zb.break_count ? ` · 开板${pools.zb.break_count}次` : ''}
        </Tag>,
      )
    if (pools.dt)
      tags.push(
        <Tag key="dt" color="green">
          跌停
        </Tag>,
      )
    if (pools.strong)
      tags.push(
        <Tag key="strong" color="blue">
          强势股
        </Tag>,
      )
    return tags
  })()

  const toggleWatch = useCallback(async () => {
    const row = mf?.row
    const sourceDate = mf?.date || poolDate || last?.date || ''
    if (!watchItem && !sourceDate) {
      setTip({ type: 'error', text: '无法确定建仓日：库里没有该股的池子或打分日期' })
      return
    }
    setBusy(true)
    setTip(null)
    try {
      if (watchItem) {
        await api.watchRemove(code)
        setWatchItem(null)
        setTip({ type: 'success', text: `已取消关注 ${name || code}` })
      } else {
        const res = await api.watchAdd({
          code,
          name: name || code,
          source_date: sourceDate,
          market,
          industry: industry ?? '',
          rank: row?.rank,
          score: row?.score,
          price: row?.price ?? last?.close ?? undefined,
          snapshot: row ? { reasons: row.reasons, dims: row.dims } : undefined,
        })
        setWatchItem(res.items.find((i) => i.code === code) ?? null)
        setTip({
          type: 'success',
          text: `已关注 ${name || code}，走势从 ${fmtDate(sourceDate)} 起算，见左侧「关注股票」`,
        })
      }
    } catch (e) {
      setTip({ type: 'error', text: `${watchItem ? '取消关注' : '关注'}失败：${(e as Error).message}` })
    } finally {
      setBusy(false)
    }
  }, [code, industry, last, market, mf, name, poolDate, watchItem])

  // ------------------------------------------------------------ K线 + 成交量
  const kOption = useMemo<EChartsCoreOption | null>(() => {
    if (rows.length < 2) return null
    const dates = rows.map((r) => r.date)
    return {
      animation: false,
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'cross' },
        formatter: (params: unknown) => {
          const list = (params ?? []) as { axisValue: string }[]
          const r = rows.find((x) => x.date === list[0]?.axisValue)
          const lines = [fmtDate(list[0]?.axisValue)]
          if (r) {
            lines.push(
              `开 ${fmtNum(r.open)}　收 ${fmtNum(r.close)}`,
              `高 ${fmtNum(r.high)}　低 ${fmtNum(r.low)}`,
              `涨跌 ${fmtPct(r.pct_chg)}　换手 ${fmtNum(r.turnover, 1)}%`,
            )
          }
          return lines.join('<br/>')
        },
      },
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      legend: { data: ['K线', 'MA5', 'MA20'], top: 0, textStyle: { fontSize: 11 } },
      grid: [
        { left: 64, right: 56, top: 32, height: '50%' },
        { left: 64, right: 56, top: '66%', height: '16%' },
      ],
      xAxis: [
        {
          type: 'category',
          data: dates,
          gridIndex: 0,
          axisLine: { lineStyle: { color: token.colorBorder } },
          axisLabel: { fontSize: 10, color: token.colorTextTertiary },
        },
        {
          type: 'category',
          data: dates,
          gridIndex: 1,
          axisLabel: { show: false },
          axisTick: { show: false },
        },
      ],
      yAxis: [
        {
          scale: true,
          gridIndex: 0,
          splitLine: { lineStyle: { type: 'dashed', color: token.colorBorder } },
          axisLabel: { fontSize: 10, color: token.colorTextTertiary },
        },
        {
          gridIndex: 1,
          axisLabel: {
            fontSize: 10,
            color: token.colorTextTertiary,
            formatter: (v: number) => compact(v),
          },
          splitLine: { show: false },
        },
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1], start: 0, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 16 },
      ],
      series: [
        {
          name: 'K线',
          type: 'candlestick',
          data: rows.map((r) => [r.open, r.close, r.low, r.high]),
          itemStyle: { color: UP_COLOR, color0: DOWN_COLOR, borderColor: UP_COLOR, borderColor0: DOWN_COLOR },
        },
        { name: 'MA5', type: 'line', data: ma(rows, 5), showSymbol: false, lineStyle: { width: 1 } },
        { name: 'MA20', type: 'line', data: ma(rows, 20), showSymbol: false, lineStyle: { width: 1 } },
        {
          name: '成交量',
          type: 'bar',
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: rows.map((r) => ({
            value: r.volume,
            itemStyle: { color: (r.pct_chg ?? 0) >= 0 ? UP_COLOR : DOWN_COLOR },
          })),
        },
      ],
    }
  }, [rows, token])

  // ------------------------------------------------------------ 主力净流入
  const inflowOption = useMemo<EChartsCoreOption | null>(() => {
    const days = inflowItem?.days ?? []
    if (!days.length) return null
    return {
      animation: false,
      grid: { left: 76, right: 24, top: 24, bottom: 28 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const list = (params ?? []) as { axisValue: string; value?: number }[]
          const day = days.find((x) => md(x.date) === list[0]?.axisValue)
          const title = day ? fmtDate(day.date) : (list[0]?.axisValue ?? '')
          return `${title}<br/>主力净流入 ${compact(Number(list[0]?.value ?? 0))}`
        },
      },
      xAxis: {
        type: 'category',
        data: days.map((d) => md(d.date)),
        axisLine: { lineStyle: { color: token.colorBorder } },
        axisLabel: { fontSize: 10, color: token.colorTextTertiary },
      },
      yAxis: {
        type: 'value',
        axisLabel: { fontSize: 10, color: token.colorTextTertiary, formatter: (v: number) => compact(v) },
        splitLine: { lineStyle: { type: 'dashed', color: token.colorBorder } },
      },
      series: [
        {
          name: '主力净流入',
          type: 'bar',
          barWidth: 16,
          data: days.map((d) => ({
            value: +Number(d.net_inflow).toFixed(2),
            itemStyle: { color: signColor(d.net_inflow, token), borderRadius: [3, 3, 0, 0] },
          })),
          label: {
            show: true,
            position: 'top' as const,
            fontSize: 10,
            formatter: (p: { value?: number }) => compact(Number(p.value ?? 0)),
          },
        },
      ],
    }
  }, [inflowItem, token])

  const kRef = useEChart(kOption)
  const inflowRef = useEChart(inflowOption)

  const inflowCols: ColumnsType<InflowDay> = useMemo(
    () => [
      { title: '日期', dataIndex: 'date', render: (v: string) => fmtDate(v) },
      {
        title: '主力净流入',
        dataIndex: 'net_inflow',
        align: 'right',
        render: (v: number) => <Text type={signType(v)}>{compact(v)}</Text>,
      },
      {
        title: '大单净流',
        dataIndex: 'large_order_net',
        align: 'right',
        render: (v?: number | null) =>
          v == null || typeof v !== 'number' ? '-' : <Text type={signType(v)}>{compact(v)}</Text>,
      },
    ],
    [],
  )

  const stack: React.CSSProperties = { display: 'flex', flexDirection: 'column', gap: 8 }
  const poolCount = poolTag.length

  return (
    <Spin spinning={loading}>
      <Card
        title={
          <Space wrap>
            <Button size="small" icon={<ArrowLeftOutlined />} onClick={() => nav(-1)}>
              返回
            </Button>
            <Text strong style={{ fontSize: 18 }}>
              {name || (daily?.error ? code : '—')}
            </Text>
            <StockCode code={code} name={name} market={market} />
            {market === 'hk' && <Tag color="blue">港股</Tag>}
            {industry ? <Tag>{industry}</Tag> : null}
            {poolTag}
          </Space>
        }
        extra={
          <Space wrap>
            <Button
              size="small"
              type={watchItem ? 'primary' : 'default'}
              icon={watchItem ? <StarFilled /> : <StarOutlined />}
              // 档案没加载完就点会拿不到建仓日（池子 / 打分 / 日线都是刚发起的请求），
              // 与其弹「无法确定建仓日」，不如先转圈等数据
              loading={busy || loading}
              onClick={() => void toggleWatch()}
            >
              {watchItem ? '已关注' : '关注'}
            </Button>
            <Button size="small" icon={<ReloadOutlined />} loading={loading} onClick={() => void load()}>
              刷新
            </Button>
          </Space>
        }
      >
        {error && (
          <Alert type="error" showIcon style={{ marginBottom: 12 }} title={error} />
        )}
        {daily?.error && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            title={
              market === 'hk'
                ? `走势暂不可用：${daily.error}`
                : `走势暂不可用：${daily.error}（池内状态、打分与资金流仍可正常查看）`
            }
          />
        )}
        {tip && (
          <Alert
            type={tip.type}
            showIcon
            closable={{ onClose: () => setTip(null) }}
            style={{ marginBottom: 12 }}
            title={tip.text}
          />
        )}
        {market === 'hk' && !loading && !error && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            title="港股个股：不参与 A 股的涨跌停池、连板梯队与主力建仓打分，这里只提供行情与日线，区间对比基准为恒生指数。"
          />
        )}
        {market !== 'hk' && !name && !loading && !error && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            title="该股不在当前的涨跌停池与候选名单中，下面只显示行情与资金流。"
          />
        )}

        <Row gutter={[12, 12]} style={{ marginBottom: 12 }}>
          <Col {...COL}>
            <MiniStat
              label="最新收盘"
              value={fmtNum(last?.close)}
              hint={last ? fmtDate(last.date) : undefined}
            />
          </Col>
          <Col {...COL}>
            <MiniStat
              label="当日涨跌幅"
              value={fmtPct(last?.pct_chg)}
              color={signColor(last?.pct_chg, token)}
              hint="红涨绿跌"
            />
          </Col>
          <Col {...COL}>
            <MiniStat
              label={`近 ${DAILY_DAYS} 日累计`}
              value={fmtPct(periodRet)}
              color={signColor(periodRet, token)}
              hint={
                first && last
                  ? `${fmtDate(first.date)} → ${fmtDate(last.date)}`
                  : undefined
              }
            />
          </Col>
          <Col {...COL}>
            <MiniStat
              label="区间最高 / 最低"
              value={hi != null && lo != null ? `${fmtNum(hi)} / ${fmtNum(lo)}` : '-'}
              hint="日高 · 日低"
            />
          </Col>
          <Col {...COL}>
            <MiniStat
              label="最新成交额 / 换手"
              value={
                last?.amount != null
                  ? `${(last.amount / 1e8).toFixed(2)}亿`
                  : '-'
              }
              hint={last?.turnover != null ? `换手 ${fmtNum(last.turnover, 1)}%` : undefined}
            />
          </Col>
        </Row>

        <Row gutter={[12, 12]}>
          <Col xs={24} lg={16}>
            <Card size="small" title={`走势 · 近 ${DAILY_DAYS} 个交易日`} style={{ marginBottom: 12 }}>
              {kOption ? (
                <div ref={kRef} style={{ height: 340, width: '100%' }} />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无日线数据" />
              )}
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card
              size="small"
              title="档案"
              style={{ marginBottom: 12 }}
              extra={
                poolCount ? <Text type="secondary" style={{ fontSize: 12 }}>池内命中 {poolCount}</Text> : null
              }
            >
              <div style={stack}>
                <div style={stack}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {market === 'hk'
                      ? '涨跌停 / 强势池（不适用）'
                      : `涨跌停 / 强势池（${fmtDate(poolDate)}）`}
                  </Text>
                  <Space wrap>
                    {market === 'hk' ? (
                      <Text type="secondary">港股无涨跌停制度，本项不适用</Text>
                    ) : poolCount ? (
                      poolTag
                    ) : (
                      <Text type="secondary">不在任何池内</Text>
                    )}
                  </Space>
                </div>

                <div style={stack}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {market === 'hk'
                      ? '主力建仓打分（不适用）'
                      : `主力建仓打分（${mf ? fmtDate(mf.date) : '未打分'}）`}
                  </Text>
                  {market === 'hk' ? (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      打分只跑 A 股全市场，港股不在范围内
                    </Text>
                  ) : mf?.row ? (
                    <>
                      <Space wrap>
                        <Tag color="gold">综合分 {fmtNum(mf.row.score, 1)}</Tag>
                        <Tag>第 {mf.row.rank} 名</Tag>
                        {mf.row.coverage != null && (
                          <Tag>覆盖 {(mf.row.coverage * 100).toFixed(0)}%</Tag>
                        )}
                      </Space>
                      {mf.row.reasons?.length ? (
                        <div style={stack}>
                          {mf.row.reasons.slice(0, 4).map((r) => (
                            <Text key={r} style={{ fontSize: 12 }}>
                              · {r}
                            </Text>
                          ))}
                        </div>
                      ) : null}
                    </>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      未进入候选（或尚未跑打分）
                    </Text>
                  )}
                </div>

                <div style={stack}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    关注状态
                  </Text>
                  {watchItem ? (
                    <Space wrap>
                      <Tag color="blue">
                        {watchItem.rank != null || watchItem.score != null
                          ? '建仓日'
                          : '起算日'}{' '}
                        {fmtDate(watchItem.source_date)}
                      </Tag>
                      {watchItem.rank != null && <Tag>当时第 {watchItem.rank} 名</Tag>}
                      {watchItem.score != null && (
                        <Tag>当时 {fmtNum(watchItem.score, 1)} 分</Tag>
                      )}
                    </Space>
                  ) : (
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      未关注 · 点右上角「关注」纳入关注股票页
                    </Text>
                  )}
                </div>
              </div>
            </Card>
          </Col>
        </Row>

        <Row gutter={[12, 12]}>
          <Col xs={24} lg={16}>
            <Card
              size="small"
              title={`主力净流入 · 近 ${INFLOW_DAYS} 个交易日`}
              extra={
                inflowItem ? (
                  <Text type={signType(inflowItem.total)} style={{ fontWeight: 600 }}>
                    合计 {compact(inflowItem.total)}
                  </Text>
                ) : null
              }
            >
              {inflowOption ? (
                <div ref={inflowRef} style={{ height: 260, width: '100%' }} />
              ) : (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description={
                    inflow?.backfilling
                      ? `回补中，还差 ${inflow.missing} 只，稍后自动出现`
                      : market === 'hk'
                        ? '港股不提供个股资金流（主力/超大单口径只覆盖 A 股）'
                        : '暂无资金流数据（盘后流水线跑完后才有）'
                  }
                />
              )}
            </Card>
          </Col>
          <Col xs={24} lg={8}>
            <Card size="small" title="逐日明细">
              <Table<InflowDay>
                rowKey={(r) => r.date}
                size="small"
                columns={inflowCols}
                dataSource={inflowItem?.days ?? []}
                pagination={false}
                scroll={{ y: 200 }}
              />
            </Card>
          </Col>
        </Row>

        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          title="数据免责声明：本页走势、资金流与打分均来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议；口径与延迟以数据源为准。"
        />
      </Card>
    </Spin>
  )
}
