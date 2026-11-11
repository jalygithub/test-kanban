import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert, Button, Card, Space, Spin, Table, Tabs, Tag, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useNavigate, useSearchParams } from 'react-router-dom'
import StockCode from '../components/StockCode'
import { api, type LimitRow, type PoolType } from '../lib/api'
import { signType, yi } from '../lib/format'

const { Text } = Typography

const TAB_KEYS: PoolType[] = ['zt', 'zb', 'dt', 'strong']

/** 时间字段是 HHMMSS 或 HHMMSSmmm，格式化成 HH:MM:SS */
const fmtTime = (v?: string) => {
  if (!v) return '-'
  const s = String(v).padStart(6, '0')
  return `${s.slice(0, 2)}:${s.slice(2, 4)}:${s.slice(4, 6)}`
}

/** 数值单元格：拿不到就是 `-`（断板行的行情字段后端一律不带） */
const numCell = (v?: number, digits = 2) => (v == null ? '-' : v.toFixed(digits))

/** 涨跌幅单元格：拿不到就是 `-`，绝不拿断板前那天的旧值冒充当天涨幅 */
const pctCell = (v?: number) =>
  v == null ? '-' : <Text type={signType(v)}>{v.toFixed(2)}</Text>

/**
 * 排序器外壳：**断板行恒排在末尾**，不参与名次竞争。
 *
 * 「断板沉底」那一位要跟着 `sortOrder` 反号 —— antd 的 `getSortData` 对
 * `descend` 是 `sortOrder === 'ascend' ? r : -r`，连板列默认就是 `descend`，
 * 不反号会被翻成断板顶到表头（10 板的断板跑第一行）。同组之间不加符号，
 * 升降方向交给 antd 自己翻。
 *
 * 两行都是断板时一律返回并列 —— 它们没有当天的行情可比，放任排序器回
 * `undefined - undefined`（NaN）会让表格顺序每次点都乱跳。
 */
const tail =
  (cmp: (a: LimitRow, b: LimitRow) => number) =>
  (a: LimitRow, b: LimitRow, sortOrder?: 'ascend' | 'descend' | null): number => {
    const s = sortOrder === 'descend' ? -1 : 1
    const t = ((a.broken ? 1 : 0) - (b.broken ? 1 : 0)) * s
    if (t !== 0) return t
    if (a.broken) return 0
    const v = cmp(a, b)
    return Number.isNaN(v) ? 0 : v
  }

function usePools(type: PoolType) {
  const [data, setData] = useState<{ date: string; rows: LimitRow[] } | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(
    async (refresh = false) => {
      setLoading(true)
      setError(null)
      try {
        const res = await api.limitPool(type, undefined, refresh)
        setData({ date: res.date, rows: res.rows ?? [] })
      } catch (e) {
        setError((e as Error).message)
      } finally {
        setLoading(false)
      }
    },
    [type],
  )

  useEffect(() => {
    void load()
  }, [load])

  return { ...data, loading, error, reload: load }
}

export default function LimitPools() {
  const zt = usePools('zt')
  const zb = usePools('zb')
  const dt = usePools('dt')
  const strong = usePools('strong')

  const nav = useNavigate()
  // 深链：总览的统计卡 / 连板梯队点柱子都是带着 ?tab= 与 ?boards= 跳过来的，
  // 两个入口共用本页当「清单详情」
  const [sp, setSp] = useSearchParams()
  const rawTab = sp.get('tab') ?? 'zt'
  const active: PoolType = TAB_KEYS.includes(rawTab as PoolType) ? (rawTab as PoolType) : 'zt'
  const boards = Number(sp.get('boards') ?? '') || null

  /** 连板数筛选只对涨停池有意义；不带 ?boards= 时就是原样全量 */
  const ztRows = useMemo(
    () => (boards ? (zt.rows ?? []).filter((r) => r.boards === boards) : (zt.rows ?? [])),
    [zt.rows, boards],
  )

  /** 断板回看带回的那几只 —— 它们不在当日池子里，标签与表尾位置都要单拎出来 */
  const ztBroken = useMemo(() => (zt.rows ?? []).filter((r) => r.broken).length, [zt.rows])
  const ztOnBoard = (zt.rows?.length ?? 0) - ztBroken

  const date = zt.date ?? ''

  const ztColumns: ColumnsType<LimitRow> = useMemo(
    () => [
      {
        title: '连板',
        dataIndex: 'boards',
        width: 130,
        align: 'center',
        sorter: tail((a, b) => (a.boards ?? 0) - (b.boards ?? 0)),
        defaultSortOrder: 'descend',
        render: (v: number, r) =>
          r.broken ? (
            // 旧数据：连板数冻结在断板前的值，另挂一枚灰 Tag 说明断了几天
            <div>
              <Space size={4}>
                <Tag>{v}板</Tag>
                <Tag>断{r.miss_days ?? '-'}天</Tag>
              </Space>
              <div>
                <Text type="secondary" style={{ fontSize: 11 }}>
                  {r.last_date}
                </Text>
              </div>
            </div>
          ) : v >= 3 ? (
            <Tag color="red">{v}板</Tag>
          ) : v === 2 ? (
            <Tag color="orange">{v}板</Tag>
          ) : (
            <Tag>{v}板</Tag>
          ),
      },
      {
        title: '代码',
        dataIndex: 'code',
        width: 90,
        render: (v: string, r) => <StockCode code={v} name={r.name} />,
      },
      { title: '名称', dataIndex: 'name', width: 120 },
      {
        title: '涨幅%',
        dataIndex: 'pct_chg',
        width: 90,
        align: 'right',
        sorter: tail((a, b) => (a.pct_chg ?? 0) - (b.pct_chg ?? 0)),
        render: (v: number) => pctCell(v),
      },
      {
        title: '现价',
        dataIndex: 'price',
        width: 80,
        align: 'right',
        render: (v: number) => numCell(v),
      },
      {
        title: '首封时间',
        dataIndex: 'first_seal_time',
        width: 100,
        align: 'center',
        sorter: tail((a, b) =>
          String(a.first_seal_time ?? '').localeCompare(String(b.first_seal_time ?? '')),
        ),
        render: fmtTime,
      },
      {
        title: '炸板次',
        dataIndex: 'break_count',
        width: 80,
        align: 'center',
        render: (v?: number) => numCell(v, 0),
      },
      {
        title: '封单(亿)',
        dataIndex: 'seal_amount',
        width: 95,
        align: 'right',
        sorter: tail((a, b) => (a.seal_amount ?? 0) - (b.seal_amount ?? 0)),
        render: yi,
      },
      {
        title: '换手%',
        dataIndex: 'turnover',
        width: 80,
        align: 'right',
        render: (v?: number) => numCell(v, 1),
      },
      { title: '流通市值(亿)', dataIndex: 'float_mv', width: 110, align: 'right', render: yi },
      { title: '涨停统计', dataIndex: 'zt_stat', width: 90, align: 'center' },
      { title: '行业', dataIndex: 'industry', width: 110, ellipsis: true },
    ],
    [],
  )

  const plainColumns: ColumnsType<LimitRow> = useMemo(
    () => [
      {
        title: '代码',
        dataIndex: 'code',
        width: 90,
        render: (v: string, r) => <StockCode code={v} name={r.name} />,
      },
      { title: '名称', dataIndex: 'name', width: 130 },
      {
        title: '涨幅%',
        dataIndex: 'pct_chg',
        width: 95,
        align: 'right',
        sorter: (a, b) => a.pct_chg - b.pct_chg,
        defaultSortOrder: 'descend',
        render: (v: number) => (
          <Text type={signType(v)}>{v.toFixed(2)}</Text>
        ),
      },
      { title: '现价', dataIndex: 'price', width: 85, align: 'right', render: (v: number) => v?.toFixed(2) },
      { title: '成交额(亿)', dataIndex: 'amount', width: 105, align: 'right', render: yi },
      { title: '换手%', dataIndex: 'turnover', width: 80, align: 'right', render: (v?: number) => v?.toFixed(1) },
      { title: '流通市值(亿)', dataIndex: 'float_mv', width: 110, align: 'right', render: yi },
      { title: '行业', dataIndex: 'industry', width: 120, ellipsis: true },
    ],
    [],
  )

  const dtColumns: ColumnsType<LimitRow> = useMemo(
    () => [
      {
        title: '代码',
        dataIndex: 'code',
        width: 90,
        render: (v: string, r) => <StockCode code={v} name={r.name} />,
      },
      { title: '名称', dataIndex: 'name', width: 130 },
      {
        title: '跌幅%',
        dataIndex: 'pct_chg',
        width: 95,
        align: 'right',
        sorter: (a, b) => a.pct_chg - b.pct_chg,
        defaultSortOrder: 'ascend',
        render: (v: number) => <Text type={signType(v)}>{v.toFixed(2)}</Text>,
      },
      { title: '现价', dataIndex: 'price', width: 85, align: 'right', render: (v: number) => v?.toFixed(2) },
      { title: '连续跌停', dataIndex: 'consecutive_dt', width: 95, align: 'center' },
      { title: '开板次', dataIndex: 'open_count', width: 80, align: 'center' },
      { title: '换手%', dataIndex: 'turnover', width: 80, align: 'right', render: (v?: number) => v?.toFixed(1) },
      { title: '流通市值(亿)', dataIndex: 'float_mv', width: 110, align: 'right', render: yi },
      { title: '行业', dataIndex: 'industry', width: 120, ellipsis: true },
    ],
    [],
  )

  const renderTable = (
    state: ReturnType<typeof usePools>,
    columns: ColumnsType<LimitRow>,
    rows?: LimitRow[],
  ) => (
    <>
      {state.error && (
        <Alert type="error" showIcon style={{ marginBottom: 12 }} title={state.error} />
      )}
      <Table<LimitRow>
        rowKey={(r) => r.code}
        size="small"
        loading={state.loading}
        columns={columns}
        dataSource={rows ?? state.rows ?? []}
        rowClassName={(r) => (r.broken ? 'row-broken' : '')}
        pagination={{
          pageSize: 50,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 只`,
        }}
        scroll={{ x: 1100 }}
        // 整行可点 → 个股详情（走势 / 主力净流入 / 档案）
        onRow={(r) => ({
          onClick: () => nav(`/stock/${r.code}`),
          style: { cursor: 'pointer' },
        })}
      />
    </>
  )

  return (
    <Card
      title={`涨跌停全景 ${date ? `· ${date}` : ''}`}
      extra={
        <Space wrap>
          <Text type="secondary">点击任意一行 → 个股详情</Text>
          <Text type="secondary">数据源：东方财富涨停板行情</Text>
          <Button size="small" onClick={() => void zt.reload(true)} loading={zt.loading}>
            重新抓取
          </Button>
        </Space>
      }
    >
      <Spin spinning={zt.loading && !zt.rows}>
        {active === 'zt' && boards != null && (
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            closable={{ onClose: () => setSp({ tab: 'zt' }) }}
            title={`连板数 = ${boards} 板 · 命中 ${ztRows.length} 只${
              ztRows.length === 0 && !zt.loading ? '（该板级当日没有个股）' : ''
            }`}
            action={
              <Button size="small" onClick={() => setSp({ tab: 'zt' })}>
                清除筛选
              </Button>
            }
          />
        )}
        <Tabs
          activeKey={active}
          onChange={(k) => setSp({ tab: k })}
          items={[
            {
              key: 'zt',
              label: ztBroken
                ? `涨停池 (${ztOnBoard} + 断板 ${ztBroken})`
                : `涨停池 (${ztOnBoard})`,
              children: (
                <>
                  {ztBroken > 0 && (
                    <Alert
                      type="info"
                      showIcon
                      style={{ marginBottom: 12 }}
                      title={`断板 ${ztBroken} 只已排在表尾`}
                      description="连板≥2 的股票断板后仍列出，连板数冻结在断板前的值；行情列是断板当天的旧数据，一律显示为 —。连续缺席 3 个交易日即不再显示。"
                    />
                  )}
                  {renderTable(zt, ztColumns, ztRows)}
                </>
              ),
            },
            {
              key: 'zb',
              label: `炸板池 (${zb.rows?.length ?? 0})`,
              children: renderTable(zb, plainColumns),
            },
            {
              key: 'dt',
              label: `跌停池 (${dt.rows?.length ?? 0})`,
              children: renderTable(dt, dtColumns),
            },
            {
              key: 'strong',
              label: `强势股 (${strong.rows?.length ?? 0})`,
              children: renderTable(strong, plainColumns),
            },
          ]}
        />
      </Spin>
    </Card>
  )
}
