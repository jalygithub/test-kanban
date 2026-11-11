import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Popconfirm,
  Row,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { EChartsCoreOption } from 'echarts/core'
import { Link, useNavigate } from 'react-router-dom'
import { JobProgress } from '../components/JobProgress'
import StockCode from '../components/StockCode'
import {
  api,
  type JobStatus,
  type MainForceRow,
  type Overview as OverviewData,
  type SectorRow,
} from '../lib/api'
import { useEChart } from '../lib/useEChart'
import {
  fmtDate,
  parseDetail,
  rel,
  signType,
  STATUS_COLOR,
  STATUS_LABEL,
} from '../lib/format'

const { Text } = Typography

const DATASET_LABEL: Record<string, string> = {
  pool_zt: '涨停池',
  pool_zb: '炸板池',
  pool_dt: '跌停池',
  pool_strong: '强势股',
  sector_flow_industry: '行业资金流',
  sector_flow_concept: '概念资金流',
  main_force: '主力建仓打分',
}

/** 统计数字卡。刻意不用 antd Statistic：v6 已把 valueStyle 标记为弃用。
 *  传了 `to` 就整卡可点 —— 数字只是入口，点进去看这份数据的明细
 *  （与连板梯队点柱子共用同一个清单页）。 */
function Stat({
  label,
  value,
  hint,
  to,
}: {
  label: string
  value: string
  hint?: string
  to?: string
}) {
  const card = (
    <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {label}
      </Text>
      <div
        style={{
          fontSize: 30,
          lineHeight: 1.2,
          fontWeight: 600,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {value}
      </div>
      {hint ? (
        <Text type="secondary" style={{ fontSize: 12 }} ellipsis>
          {hint}
        </Text>
      ) : null}
    </Card>
  )
  return to ? (
    <Link to={to} className="stat-link" title={`查看${label}明细`}>
      {card}
    </Link>
  ) : (
    card
  )
}

export default function Overview() {
  const nav = useNavigate()
  const [data, setData] = useState<OverviewData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [kicking, setKicking] = useState(false)
  /** 上次拿到数据的时刻，渲染期只读它、不再调 Date.now() */
  const [now, setNow] = useState(0)

  // silent=true 给轮询用：不能每次都翻转整页 loading，否则任务跑起来后
  // 页面会每 3 秒闪一次
  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true)
    setError(null)
    try {
      setData(await api.overview())
      // 与 setData 同批提交：data 非空时 now 必然已经有效
      setNow(Date.now())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const job: JobStatus | undefined = data?.job
  const running = job?.current.running ?? false

  // 后台任务在跑就轮询；跑完最后拉一次总览拿到终态
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => void load(true), 3000)
    return () => window.clearInterval(timer)
  }, [running, load])

  const kick = async () => {
    setKicking(true)
    try {
      await api.runJob()
      await load(true)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setKicking(false)
    }
  }

  // ---- 连板梯队金字塔 -------------------------------------------------
  // 纵向类别轴：1板在底、最高板在顶，家数自下而上收窄，就是复盘里那座金字塔。
  const ladderOption = useMemo<EChartsCoreOption | null>(() => {
    if (!data) return null
    const entries = Object.entries(data.ladder)
      .map(([b, n]) => ({ boards: Number(b), n }))
      .filter((x) => Number.isFinite(x.boards) && x.n > 0)
      .sort((a, b) => a.boards - b.boards)
    if (!entries.length) return null
    return {
      grid: { left: 8, right: 44, top: 8, bottom: 4, containLabel: true },
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      xAxis: {
        type: 'value',
        minInterval: 1,
        splitLine: { lineStyle: { type: 'dashed', color: '#f0f0f0' } },
        axisLabel: { color: '#999', fontSize: 11 },
      },
      yAxis: {
        type: 'category',
        data: entries.map((e) => `${e.boards}板`),
        axisTick: { show: false },
        axisLine: { lineStyle: { color: '#d9d9d9' } },
        axisLabel: { fontSize: 12 },
      },
      series: [
        {
          type: 'bar',
          barWidth: 16,
          // 配色对齐涨跌停页的 Tag：>=3 红、2 橙、其余浅蓝
          data: entries.map((e) => ({
            value: e.n,
            itemStyle: {
              color:
                e.boards >= 3 ? '#f5222d' : e.boards === 2 ? '#fa8c16' : '#91caff',
              borderRadius: [0, 4, 4, 0],
            },
          })),
          label: { show: true, position: 'right', formatter: '{c} 家', fontSize: 11 },
        },
      ],
    }
  }, [data])
  // 点柱子 → 该板级的个股清单。和统计卡点进去的是**同一个页面**（/limit），
  // 只是带着不同的筛选参数，两边的「详情」因此不会长成两套样子。
  const ladderEvents = useMemo(
    () => ({
      click: (p: { name?: string; dataIndex?: number }) => {
        const boards = Number.parseInt(String(p.name ?? ''), 10)
        if (Number.isFinite(boards) && boards > 0) nav(`/limit?tab=zt&boards=${boards}`)
        else if (p.dataIndex != null) nav('/limit?tab=zt')
      },
    }),
    [nav],
  )
  const ladderChart = useEChart(ladderOption, ladderEvents)

  // ---- 板块资金流 ------------------------------------------------------
  const sectorCols: ColumnsType<SectorRow> = useMemo(
    () => [
      { title: '板块', dataIndex: 'name', ellipsis: true },
      {
        title: '净流入(亿)',
        dataIndex: 'net_inflow',
        width: 96,
        align: 'right',
        render: (v: number) => (
          <Text type={signType(v)}>{v.toFixed(2)}</Text>
        ),
      },
      { title: '家数', dataIndex: 'stock_count', width: 56, align: 'center' },
      {
        title: '领涨%',
        dataIndex: 'leading_pct_chg',
        width: 74,
        align: 'right',
        render: (v: number) => (
          <Text type={signType(v)}>{Number(v).toFixed(2)}</Text>
        ),
      },
    ],
    [],
  )

  // ---- 主力建仓 Top5 ---------------------------------------------------
  const mfCols: ColumnsType<MainForceRow> = useMemo(
    () => [
      { title: '#', dataIndex: 'rank', width: 44, align: 'center' },
      {
        title: '代码',
        dataIndex: 'code',
        width: 82,
        render: (v: string, row) => <StockCode code={v} name={row.name} />,
      },
      { title: '名称', dataIndex: 'name', ellipsis: true },
      {
        title: '综合分',
        dataIndex: 'score',
        width: 78,
        align: 'right',
        sorter: (a, b) => a.score - b.score,
        defaultSortOrder: 'descend',
        render: (v: number) => <Text strong>{Number(v).toFixed(1)}</Text>,
      },
      { title: '行业', dataIndex: 'industry', width: 96, ellipsis: true },
    ],
    [],
  )

  const leader = data?.leader
  const lastDetail = parseDetail(job?.last?.detail)
  const schedule = job?.schedule

  const statCards = data ? (
    <Row gutter={[12, 12]}>
      <Col xs={12} sm={8} md={4}>
        <Stat label="涨停" value={String(data.pools.zt ?? 0)} to="/limit?tab=zt" />
      </Col>
      <Col xs={12} sm={8} md={4}>
        <Stat label="炸板" value={String(data.pools.zb ?? 0)} to="/limit?tab=zb" />
      </Col>
      <Col xs={12} sm={8} md={4}>
        <Stat label="跌停" value={String(data.pools.dt ?? 0)} to="/limit?tab=dt" />
      </Col>
      <Col xs={12} sm={8} md={4}>
        <Stat label="强势股" value={String(data.pools.strong ?? 0)} to="/limit?tab=strong" />
      </Col>
      <Col xs={12} sm={8} md={4}>
        <Stat
          label="最高连板"
          value={leader ? `${leader.boards}板` : '-'}
          hint={leader ? `${leader.name} ${leader.zt_stat ?? ''}` : '涨停池为空'}
          to={leader ? `/stock/${leader.code}` : '/limit?tab=zt'}
        />
      </Col>
      <Col xs={12} sm={8} md={4}>
        <Stat
          label="已关注"
          value={String(data.watchlist_count)}
          hint={data.main_force.date ? `打分日 ${fmtDate(data.main_force.date)}` : '尚未打分'}
          to="/watchlist"
        />
      </Col>
    </Row>
  ) : null

  return (
    <Spin spinning={loading && !data}>
      <Card
        title={
          <Space>
            <span>总览</span>
            <Tag color="blue">{fmtDate(data?.trade_date)}</Tag>
          </Space>
        }
        extra={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {data?.freshness?.[0]?.fetched_at
                ? `数据更新于 ${rel(data.freshness[0].fetched_at, now)}`
                : '暂无数据'}
            </Text>
            <Button size="small" onClick={() => void load()} loading={loading && !!data}>
              刷新
            </Button>
            <Popconfirm
              title="立即执行完整盘后任务？"
              description="抓涨跌停池、板块资金流并重跑主力建仓打分，首次约 12-15 分钟；当天已打过分会直接跳过打分，约 10 秒。期间可继续浏览其它页面。"
              okText="执行"
              cancelText="取消"
              onConfirm={kick}
            >
              <Button size="small" type="primary" loading={kicking || running}>
                {running ? '任务进行中…' : '立即执行'}
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        {error && (
          <Alert type="error" showIcon style={{ marginBottom: 12 }} title={error} />
        )}

        {statCards}

        <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
          <Col xs={24} lg={11}>
            <Card
              size="small"
              title="连板梯队"
              extra={
                <Text type="secondary" style={{ fontSize: 12 }}>
                  点柱子看该板级个股
                </Text>
              }
            >
              {ladderOption ? (
                <div ref={ladderChart} style={{ height: 200, width: '100%' }} />
              ) : (
                <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无涨停数据" />
              )}
            </Card>
          </Col>

          <Col xs={24} lg={13}>
            <Card size="small" title="板块资金流 · 行业">
              <Row gutter={12}>
                <Col span={12}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    净流入前 5
                  </Text>
                  <Table<SectorRow>
                    rowKey={(r) => r.name}
                    size="small"
                    pagination={false}
                    columns={sectorCols}
                    dataSource={data?.sector_flow?.top ?? []}
                  />
                </Col>
                <Col span={12}>
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    净流出前 5
                  </Text>
                  <Table<SectorRow>
                    rowKey={(r) => r.name}
                    size="small"
                    pagination={false}
                    columns={sectorCols}
                    dataSource={data?.sector_flow?.bottom ?? []}
                  />
                </Col>
              </Row>
            </Card>
          </Col>
        </Row>

        <Row gutter={[12, 12]} style={{ marginTop: 12 }}>
          <Col xs={24} lg={13}>
            <Card
              size="small"
              title="主力建仓候选 Top 5"
              extra={<Link to="/main-force">查看全部 →</Link>}
            >
              <Table<MainForceRow>
                rowKey={(r) => r.code}
                size="small"
                pagination={false}
                columns={mfCols}
                dataSource={data?.main_force?.top ?? []}
                // 整行可点 → 个股详情（走势 / 资金流 / 档案），与柱状图点进来的同款
                onRow={(r) => ({
                  onClick: () => nav(`/stock/${r.code}`),
                  style: { cursor: 'pointer' },
                })}
                locale={{
                  emptyText: (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description="尚未打分，可在「主力建仓候选」页触发"
                    />
                  ),
                }}
              />
              {data?.main_force?.degraded?.length ? (
                <Alert
                  type="warning"
                  showIcon
                  style={{ marginTop: 8 }}
                  title={`打分时降级的维度：${data.main_force.degraded.join('、')}`}
                  description="该维度数据源不可用，权重已按剩余维度重新归一化；随着逐日资金流累积会自动恢复。"
                />
              ) : null}
            </Card>
          </Col>

          <Col xs={24} lg={11}>
            <Card size="small" title="任务与数据" extra={<Link to="/jobs">管理 →</Link>}>
              <Space orientation="vertical" style={{ width: '100%' }} size={10}>
                <div>
                  <Text strong>盘后定时任务</Text>
                  <div style={{ marginTop: 4 }}>
                    {schedule?.enabled ? (
                      <Space wrap size={6}>
                        <Tag color="blue">
                          每交易日{' '}
                          {String(schedule.hour).padStart(2, '0')}:
                          {String(schedule.minute).padStart(2, '0')}
                        </Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          下次 {rel(schedule.next_run, now)}
                        </Text>
                      </Space>
                    ) : (
                      <Tag>调度未启动</Tag>
                    )}
                  </div>
                </div>

                {running && job?.current ? (
                  <Alert
                    type="info"
                    showIcon
                    title={
                      <Space>
                        <span>任务进行中</span>
                        <Text type="secondary">
                          {job.current.kind} ·{' '}
                          {fmtDate(job.current.date) || '计算中'}
                        </Text>
                      </Space>
                    }
                    description={<JobProgress current={job.current} />}
                  />
                ) : null}

                <div>
                  <Text strong>上一次任务</Text>
                  {job?.last ? (
                    <div style={{ marginTop: 4 }}>
                      <Space wrap size={6}>
                        <Tag color={STATUS_COLOR[job.last.status] ?? 'default'}>
                          {STATUS_LABEL[job.last.status] ?? job.last.status}
                        </Tag>
                        <Tag>{job.last.kind}</Tag>
                        <Text type="secondary" style={{ fontSize: 12 }}>
                          {fmtDate(lastDetail?.date) || job.last.started_at.slice(0, 10)}
                          {job.last.finished_at
                            ? ` · 耗时 ${Math.max(
                                0,
                                Math.round(
                                  (Date.parse(job.last.finished_at) -
                                    Date.parse(job.last.started_at)) / 1000,
                                ),
                              )}s`
                            : ''}
                        </Text>
                      </Space>
                      {lastDetail?.failed?.length ? (
                        <div style={{ marginTop: 4 }}>
                          <Text type="danger" style={{ fontSize: 12 }}>
                            失败步骤：{lastDetail.failed.join('、')}
                          </Text>
                        </div>
                      ) : null}
                    </div>
                  ) : (
                    <div style={{ marginTop: 4 }}>
                      <Text type="secondary">尚无记录</Text>
                    </div>
                  )}
                </div>

                <div>
                  <Text strong>数据新鲜度</Text>
                  <div style={{ marginTop: 4 }}>
                    {(data?.freshness ?? []).map((f) => (
                      <div
                        key={f.dataset}
                        style={{
                          display: 'flex',
                          justifyContent: 'space-between',
                          gap: 8,
                          fontSize: 12,
                          lineHeight: '22px',
                        }}
                      >
                        <Text type="secondary">{DATASET_LABEL[f.dataset] ?? f.dataset}</Text>
                        <Space size={8}>
                          <Text type="secondary">{f.row_count ?? 0} 条</Text>
                          <Text
                            type={
                              f.fetched_at &&
                              now - Date.parse(f.fetched_at) > 2 * 86400 * 1000
                                ? 'warning'
                                : 'secondary'
                            }
                          >
                            {rel(f.fetched_at, now)}
                          </Text>
                        </Space>
                      </div>
                    ))}
                  </div>
                </div>
              </Space>
            </Card>
          </Col>
        </Row>

        <Alert
          type="warning"
          showIcon
          style={{ marginTop: 12 }}
          title="数据免责声明"
          description="本页汇总的涨跌停池、板块资金流、连板梯队与主力建仓打分均来自公开接口的整理与统计，仅用于数据可视化研究，不提供投资建议，不构成任何买卖依据。"
        />
      </Card>
    </Spin>
  )
}
