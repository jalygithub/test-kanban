/**
 * 自动交易（实时纸面交易）面板。
 *
 * 你说的「实时获取行情 → 按规则选股 → 交易」就是这个页面：
 *  - 顶部控制条：运行开关 + 轮询间隔 + 定时触发开关 + 手动跑一轮 + 状态
 *  - 账户概览：总资产 / 现金 / 持仓市值 / 总盈亏 / 今日盈亏
 *  - 选股漏斗：xxx 规则过滤出多少只（每级拦下/通过）
 *  - 持仓盈亏 + 重点观察（接近止损 / 接近突破 / 高分）
 *  - 今日交易：买入 / 卖出（金额、数量、触发规则）
 *  - 「管理规则」→ 抽屉里增删改规则（RuleDrawer）
 *
 * 全部**纸面模拟**，不真实下单。运行时按间隔轮询 `/trading/live/state`。
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Col,
  Empty,
  Row,
  Select,
  Space,
  Statistic,
  Switch,
  Table,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import {
  CaretRightOutlined,
  PauseOutlined,
  ReloadOutlined,
  SettingOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import {
  api,
  errMsg,
  type TradingLiveState,
  type TradingLiveTrade,
  type TradingRulesPayload,
} from '../lib/api'
import RuleDrawer from '../components/RuleDrawer'

const { Text, Title } = Typography

const fmt = (n: number | null | undefined, d = 2) =>
  n == null ? '-' : Number(n).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d })

export default function Trading() {
  const [state, setState] = useState<TradingLiveState | null>(null)
  const [loading, setLoading] = useState(false)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [ruleVersion, setRuleVersion] = useState<string>('')
  const [interval, setIntervalSec] = useState(5)
  const [scheduled, setScheduled] = useState(false)
  const timer = useRef<ReturnType<typeof setInterval> | null>(null)

  const refresh = useCallback(async () => {
    try {
      const s = await api.tradingLiveState()
      setState(s)
    } catch (e) {
      // 静默：轮询里弹错会刷屏，只在手动操作时提示
    }
  }, [])

  // 轮询：running 时按间隔刷新
  useEffect(() => {
    refresh()
    if (timer.current) clearInterval(timer.current)
    timer.current = setInterval(() => {
      if (state?.running) refresh()
    }, Math.max(2, interval) * 1000)
    return () => {
      if (timer.current) clearInterval(timer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interval, state?.running])

  const start = async () => {
    setLoading(true)
    try {
      const s = await api.tradingLiveStart({ interval, scheduled })
      setState(s)
      message.success(scheduled ? '已启动（定时触发：仅盘中下单）' : '已启动实时循环')
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  const stop = async () => {
    setLoading(true)
    try {
      setState(await api.tradingLiveStop())
      message.info('已停止')
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  const runOnce = async () => {
    setLoading(true)
    try {
      setState(await api.tradingLiveTick())
      message.success('已跑一轮')
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  const onRulesSaved = (p: TradingRulesPayload) => {
    setRuleVersion(p.version)
    refresh()
  }

  const acct = state?.account
  const positions = state?.positions ?? []
  const buys = state?.buys ?? []
  const sells = state?.sells ?? []

  const tradeColumns = [
    { title: '时间', dataIndex: 'ts', key: 'ts', render: (v: string) => v?.slice(11, 19) || '-' },
    { title: '代码', dataIndex: 'code', key: 'code' },
    { title: '名称', dataIndex: 'name', key: 'name', render: (v: string | null) => v || '-' },
    { title: '价格', dataIndex: 'price', key: 'price', render: (v: number) => fmt(v) },
    { title: '数量', dataIndex: 'qty', key: 'qty', render: (v: number) => `${v} 股` },
    { title: '金额', dataIndex: 'amount', key: 'amount', render: (v: number) => fmt(v, 0) },
    {
      title: '盈亏',
      dataIndex: 'pnl',
      key: 'pnl',
      render: (v: number | null) =>
        v == null ? '-' : <Text type={v >= 0 ? 'success' : 'danger'}>{fmt(v, 0)}</Text>,
    },
    { title: '触发规则', dataIndex: 'rule', key: 'rule', render: (v: string | null) => v ? <Tag>{v}</Tag> : '-' },
  ]

  const posColumns = [
    { title: '代码', dataIndex: 'code', key: 'code' },
    { title: '名称', dataIndex: 'name', key: 'name', render: (v: string | null) => v || '-' },
    { title: '现价', dataIndex: 'price', key: 'price', render: (v: number) => fmt(v) },
    { title: '成本', dataIndex: 'avg_cost', key: 'avg_cost', render: (v: number) => fmt(v) },
    { title: '数量', dataIndex: 'qty', key: 'qty', render: (v: number) => `${v} 股` },
    {
      title: '浮动盈亏',
      dataIndex: 'pnl',
      key: 'pnl',
      render: (v: number) => <Text type={v >= 0 ? 'success' : 'danger'}>{fmt(v, 0)}</Text>,
    },
  ]

  return (
    <div>
      <Space style={{ width: '100%', justifyContent: 'space-between', marginBottom: 12 }} wrap>
        <Space>
          <Title level={4} style={{ margin: 0 }}>自动交易 · 纸面模拟</Title>
          <Button
            type="primary"
            icon={state?.running ? <PauseOutlined /> : <CaretRightOutlined />}
            loading={loading}
            onClick={state?.running ? stop : start}
          >
            {state?.running ? '停止' : '启动'}
          </Button>
          <Button icon={<ThunderboltOutlined />} loading={loading} onClick={runOnce}>手动跑一轮</Button>
          <Button icon={<SettingOutlined />} onClick={() => setDrawerOpen(true)}>管理规则</Button>
        </Space>
        <Space>
          <Text type="secondary">间隔</Text>
          <Select
            size="small"
            value={interval}
            onChange={setIntervalSec}
            options={[5, 10, 30, 60].map((v) => ({ value: v, label: `${v}s` }))}
            style={{ width: 72 }}
          />
          <Text type="secondary">定时触发</Text>
          <Switch size="small" checked={scheduled} onChange={setScheduled} />
          <Button size="small" icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
        </Space>
      </Space>

      <Space style={{ marginBottom: 12 }} wrap>
        <Tag color={state?.running ? 'green' : 'default'}>{state?.running ? '运行中' : '已停止'}</Tag>
        <Tag color={state?.in_session ? 'blue' : 'default'}>{state?.in_session ? '盘中' : '非盘中'}</Tag>
        {scheduled && <Tag color="purple">定时触发（仅盘中下单）</Tag>}
        {ruleVersion && <Tag>规则版本 {ruleVersion}</Tag>}
        {state?.last_error && <Tag color="red">异常 {state.last_error}</Tag>}
        {state?.last_tick && <Text type="secondary">最后更新 {state.last_tick.slice(11, 19)}</Text>}
      </Space>

      {/* 账户概览 */}
      <Card size="small" style={{ marginBottom: 12 }}>
        <Row gutter={16}>
          <Col span={4}><Statistic title="总资产" value={fmt(acct?.equity, 0)} /></Col>
          <Col span={4}><Statistic title="现金" value={fmt(acct?.cash, 0)} /></Col>
          <Col span={4}><Statistic title="持仓市值" value={fmt(acct?.position_value, 0)} /></Col>
          <Col span={4}>
            <Statistic
              title="总盈亏"
              value={fmt(acct?.total_pnl, 0)}
              valueStyle={{ color: (acct?.total_pnl ?? 0) >= 0 ? '#3f8600' : '#cf1322' }}
            />
          </Col>
          <Col span={4}>
            <Statistic
              title="已实现盈亏"
              value={fmt(acct?.realized_pnl, 0)}
              valueStyle={{ color: (acct?.realized_pnl ?? 0) >= 0 ? '#3f8600' : '#cf1322' }}
            />
          </Col>
          <Col span={4}><Statistic title="持仓数" value={positions.length} /></Col>
        </Row>
      </Card>

      <Row gutter={12}>
        {/* 选股漏斗 */}
        <Col span={10}>
          <Card size="small" title="实时选股漏斗">
            {state?.funnel?.length ? (
              state.funnel.map((s, i) => (
                <div key={s.rule_id} style={{ display: 'flex', justifyContent: 'space-between', padding: '6px 0', borderBottom: '1px dashed #f0f0f0' }}>
                  <Space>
                    <Tag>{i + 1}</Tag>
                    <Text>{s.name}</Text>
                  </Space>
                  <Space>
                    <Text type="success">过 {s.passed_n}</Text>
                    <Text type="danger">拦 {s.blocked_n}</Text>
                  </Space>
                </div>
              ))
            ) : (
              <Empty description="启动后显示逐级过滤" />
            )}
            {buys.length > 0 && (
              <Alert type="success" showIcon style={{ marginTop: 10 }} message={`本轮买入 ${buys.length} 只`} />
            )}
          </Card>
        </Col>

        {/* 持仓盈亏 + 重点观察 */}
        <Col span={14}>
          <Card size="small" title="持仓 & 盈亏" style={{ marginBottom: 12 }}>
            <Table
              size="small"
              rowKey="code"
              columns={posColumns}
              dataSource={positions}
              pagination={false}
              locale={{ emptyText: <Empty description="暂无持仓" /> }}
            />
          </Card>
          <Card size="small" title="重点观察">
            {state?.watch?.length ? (
              state.watch.map((w) => (
                <div key={w.code} style={{ padding: '4px 0', borderBottom: '1px dashed #f0f0f0' }}>
                  <Space>
                    <Text strong>{w.code}</Text>
                    <Text>{w.name}</Text>
                    <Text>{fmt(w.price)}</Text>
                  </Space>
                  <div>
                    {w.notes.map((n, i) => (
                      <Tag key={i} color="orange">{n}</Tag>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <Empty description="暂无重点观察" />
            )}
          </Card>
        </Col>
      </Row>

      {/* 今日交易 */}
      <Card size="small" title="今日交易" style={{ marginTop: 12 }}>
        <Tabs
          items={[
            {
              key: 'buy',
              label: `买入 ${buys.length}`,
              children: (
                <Table size="small" rowKey="ts" columns={tradeColumns} dataSource={buys as TradingLiveTrade[]} pagination={false} locale={{ emptyText: <Empty description="今日无买入" /> }} />
              ),
            },
            {
              key: 'sell',
              label: `卖出 ${sells.length}`,
              children: (
                <Table size="small" rowKey="ts" columns={tradeColumns} dataSource={sells as TradingLiveTrade[]} pagination={false} locale={{ emptyText: <Empty description="今日无卖出" /> }} />
              ),
            },
          ]}
        />
      </Card>

      <RuleDrawer open={drawerOpen} onClose={() => setDrawerOpen(false)} onSaved={onRulesSaved} />
    </div>
  )
}
