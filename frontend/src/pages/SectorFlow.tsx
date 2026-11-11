import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Radio, Space, Spin, Table, Typography, theme } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { EChartsCoreOption } from 'echarts/core'
import { api, type SectorRow } from '../lib/api'
import { signColor, signType } from '../lib/format'
import { useEChart } from '../lib/useEChart'

const { Text } = Typography

function useFlow(kind: 'industry' | 'concept') {
  const [rows, setRows] = useState<SectorRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(
    async (refresh = false) => {
      setLoading(true)
      setError(null)
      try {
        const res = await api.sectorFlow(kind, refresh)
        setRows(res.rows ?? [])
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setLoading(false)
      }
    },
    [kind],
  )

  useEffect(() => {
    void load()
  }, [load])

  return { rows, loading, error, reload: load }
}

export default function SectorFlow() {
  const [kind, setKind] = useState<'industry' | 'concept'>('industry')
  const flow = useFlow(kind)
  const { rows, loading, error, reload } = flow
  // 图上的涨跌颜色跟着表格走同一套 token，别再各写一套 hex
  const { token } = theme.useToken()

  /** 流入前 10 与流出前 10，正负双向条形图 */
  const chartRef = useEChart(useMemo<EChartsCoreOption | null>(() => {
    if (!rows.length) return null
    const sorted = [...rows].sort((a, b) => b.net_inflow - a.net_inflow)
    const top = [...sorted.slice(0, 10).reverse(), ...sorted.slice(-10).reverse()]
    const names = top.map((r) => r.name)
    const values = top.map((r) => Number(r.net_inflow.toFixed(2)))

    return {
      title: {
        text: `板块主力净流入 TOP/BOTTOM 10（亿元）`,
        left: 'center',
        textStyle: { fontSize: 14 },
      },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      grid: { left: 110, right: 40, top: 44, bottom: 30 },
      xAxis: { type: 'value', name: '亿元' },
      yAxis: {
        type: 'category',
        data: names,
        axisLabel: { width: 100, overflow: 'truncate' },
      },
      series: [
        {
          type: 'bar',
          data: values.map((v) => ({
            value: v,
            itemStyle: { color: signColor(v, token) },
          })),
          label: { show: true, position: 'right', formatter: '{c}' },
        },
      ],
    }
  }, [rows, token]))

  const columns: ColumnsType<SectorRow> = useMemo(
    () => [
      { title: '#', dataIndex: 'rank', width: 55, align: 'center' },
      { title: '板块', dataIndex: 'name', width: 140, ellipsis: true },
      {
        title: '涨跌幅%',
        dataIndex: 'pct_chg',
        width: 95,
        align: 'right',
        sorter: (a, b) => a.pct_chg - b.pct_chg,
        defaultSortOrder: 'descend',
        render: (v: number) => (
          <Text type={signType(v)}>{v.toFixed(2)}</Text>
        ),
      },
      {
        title: '净流入(亿)',
        dataIndex: 'net_inflow',
        width: 110,
        align: 'right',
        sorter: (a, b) => a.net_inflow - b.net_inflow,
        render: (v: number) => (
          <Text type={signType(v)}>{v.toFixed(2)}</Text>
        ),
      },
      { title: '流入(亿)', dataIndex: 'inflow', width: 95, align: 'right', render: (v: number) => v.toFixed(2) },
      { title: '流出(亿)', dataIndex: 'outflow', width: 95, align: 'right', render: (v: number) => v.toFixed(2) },
      { title: '家数', dataIndex: 'stock_count', width: 70, align: 'center' },
      { title: '领涨股', dataIndex: 'leading_stock', width: 110, ellipsis: true },
      {
        title: '领涨%',
        dataIndex: 'leading_pct_chg',
        width: 85,
        align: 'right',
        render: (v: number) => (
          <Text type={signType(v)}>{v.toFixed(2)}</Text>
        ),
      },
    ],
    [],
  )

  return (
    <Card
      title="板块资金流"
      extra={
        <Space>
          <Radio.Group
            value={kind}
            onChange={(e) => setKind(e.target.value)}
            options={[
              { label: '行业板块', value: 'industry' },
              { label: '概念板块', value: 'concept' },
            ]}
            optionType="button"
            size="small"
          />
          <Button size="small" onClick={() => void reload(true)} loading={loading}>
            重新抓取
          </Button>
        </Space>
      }
    >
      {error && (
        <Alert type="error" showIcon title={error} style={{ marginBottom: 12 }} />
      )}

      <div ref={chartRef} style={{ width: '100%', height: 460 }} />

      <Spin spinning={loading} style={{ marginTop: 12 }}>
        <Table<SectorRow>
          rowKey={(r) => r.name}
          size="small"
          loading={loading}
          columns={columns}
          dataSource={rows}
          pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 个板块` }}
          scroll={{ x: 950 }}
        />
      </Spin>
    </Card>
  )
}
