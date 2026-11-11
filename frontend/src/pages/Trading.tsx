import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Divider,
  Input,
  Popconfirm,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { QuestionCircleOutlined } from '@ant-design/icons'
import {
  api,
  errMsg,
  type TradingExecResult,
  type TradingOrder,
  type TradingParams,
  type TradingPosition,
  type TradingRun,
  type TradingRunResult,
  type TradingSignal,
  type TradingSignalReason,
  type TradingStatus,
} from '../lib/api'
import { compact, fmtDate, fmtNum } from '../lib/format'
import { HelpDrawer } from '../components/HelpDrawer'

const { Text, Paragraph, Title } = Typography

/**
 * 自动买卖策略 V1 的操作台（模拟盘）。
 *
 * 硬口径：**零联网、不实盘、不自动运行** —— 首屏四个 GET 全读本地库；
 * 会改库的两个 POST（生成信号、执行订单）只出现在人的点击里，且后端没有任何
 * 定时器碰 `/api/trading/*`。所有展示遵循「缺了就显示缺，绝不编数字」：
 * 无行情=红标，未开户=明说，拒单/过期都带理由。
 */

/** run 状态：OK=真跑出了信号；REFUSED=明确不交易（理由在 refusal_reason）。 */
const RUN_TAG: Record<string, { color: string; text: string }> = {
  OK: { color: 'green', text: '已生成' },
  REFUSED: { color: 'red', text: '拒绝' },
}

const ORDER_TAG: Record<string, { color: string; text: string }> = {
  PENDING: { color: 'blue', text: '待执行' },
  FILLED: { color: 'green', text: '已成交' },
  REJECTED: { color: 'red', text: '已拒单' },
  EXPIRED: { color: 'orange', text: '已过期' },
}

/** 红买绿卖，与全站红涨绿跌同色系；HOLD 是中性留痕，不染色。 */
const sideTag = (side: string | null | undefined) =>
  side === 'BUY' ? (
    <Tag color="red">买入</Tag>
  ) : side === 'SELL' ? (
    <Tag color="green">卖出</Tag>
  ) : (
    <Tag>不动</Tag>
  )

/** 数字统计卡。不用 antd Statistic：v6 已把 valueStyle 标记为弃用。 */
function Stat({ label: text, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card size="small" styles={{ body: { padding: '12px 16px' } }}>
      <Text type="secondary" style={{ fontSize: 12 }}>
        {text}
      </Text>
      <div
        style={{
          fontSize: 28,
          lineHeight: 1.25,
          fontWeight: 600,
          fontVariantNumeric: 'tabular-nums',
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

/**
 * 「操作说明」抽屉正文。与页面控件**一字不差**，按「控件字典」组织：输入框、
 * 按钮、统计卡、表格、状态标签逐个写 —— 「填什么 / 点了会发生什么 / 出错长什么样」。
 * 硬口径照抄页顶那条 Alert（不接实盘、不发任何真实下单指令、不会自动运行），
 * 免责声明照抄页脚；改控件同步改这里。
 *
 * 文中不含任何请求调用与计时器字样 —— `_ui_offline_check` 的 T 段盯着本页源码。
 */
function TradingHelpBody() {
  return (
    <Space direction="vertical" size={12} style={{ display: 'block' }}>
      <Alert
        type="warning"
        showIcon
        title="硬口径：模拟盘，不实盘"
        description="策略 V1 只在本地库生成信号与模拟订单、按下一交易日开盘价虚拟成交：不接实盘、不发任何真实下单指令，也不会自动运行 —— 只有人点按钮才推进一轮（同日重跑幂等，不重复下单）。首屏四个 GET 全读本地库，本页零联网、也不自动刷新。"
      />

      <Title level={5} style={{ marginTop: 4 }}>
        操作卡：1 个输入框 + 2 个按钮
      </Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>输入框「信号日期 YYYYMMDD，留空=最近快照日」</Text>：留空 = 自动取最近一个六维
          快照日；想指定那天就填 8 位数字（例 20261029）。格式不对（不是 8 位）输入框标红、
          「生成信号并下单」直接禁用，请求发不出去。
        </li>
        <li>
          <Text strong>「生成信号并下单」（蓝色主按钮）</Text>：跑一轮 —— 生成信号并把要下的模拟单
          落库。绿色回执给：信号日 → 执行日、买入 / 卖出 / 不动 计数、下单张数、风控拒单张数、
          组合权益与总仓位，信号同时载入下方「信号明细」表。同日重跑命中幂等，回执里注明
          「未重复下单」；那天数据不齐则本轮 <Tag color="red">拒绝</Tag>，理由红字挂在回执里，
          数据补齐后同日重跑可就地升级。
        </li>
        <li>
          <Text strong>「执行待成交订单」</Text>：先弹二次确认「执行待成交订单？」，点「执行」才跑 ——
          按每张订单所属执行日的开盘价虚拟撮合（含 T+1 解锁、涨停买不进、跌停卖不出、缺行情不成交），
          结果落库。回执给 成交 / 拒单 / 过期 三个计数；没有待执行订单时如实回「未执行」。
          执行完自动重拉全页，现金、持仓、订单立刻反映结果。
        </li>
        <li>
          <Text strong>五张统计卡</Text>：现金（可用现金；还没有账户时显示 —，首次点「生成信号并下单」
          自动开户）、初始资金、持仓数、T+1 冻结（当日买入、次日才可卖的持仓数）、
          待执行单（等着点上面第二个按钮撮合的单数）。
        </li>
      </ul>

      <Divider style={{ margin: '4px 0' }} />

      <Title level={5}>每张卡、每张表看什么</Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>「最近一轮信号」卡</Text>：run 状态标签、信号日 / 执行日、风险态、数据质量、
          候选数与信号数；<Tag color="red">拒绝</Tag> 的理由整条挂在这张卡里。
        </li>
        <li>
          <Text strong>「模拟持仓」表</Text>：代码 / 名称 / 持仓 / T+1 冻结（橙 Tag 带锁定日期）/
          成本价 / 成本因子 / 建仓日。还没有账户时明说「还没有模拟账户」，不编一张空表。
        </li>
        <li>
          <Text strong>「运行历史（最近 20 轮）」表</Text>：每行一轮 run；行内{' '}
          <Text strong>「看信号」</Text>按钮把该轮信号载入下方明细表。
        </li>
        <li>
          <Text strong>「信号明细」表（含 HOLD 留痕）</Text>：方向（买入红 / 卖出绿 / 不动灰）、
          目标数量、仓位占比、参考价；「行情日」缺数据时红色 <Tag color="red">缺行情</Tag>，
          不拿别的价格顶上；「判定理由」逐条给 rule 代码 + 说明，不动的票也有留痕。
        </li>
        <li>
          <Text strong>「订单（最近 100 张）」表</Text>：状态四档、委托价、成交价、费用；
          拒单与过期都带原因，看「拒单/过期原因」列。
        </li>
        <li>
          <Text strong>「参数默认值」卡</Text>：strategy 与 rule_version；参数版本 = 归一化 JSON 的
          sha256 前 12 位，每轮 run 落实际参数快照，历史订单按当时口径审定。
        </li>
      </ul>

      <Divider style={{ margin: '4px 0' }} />

      <Title level={5}>状态标签字典</Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          一轮运行：<Tag color="green">已生成</Tag>（真跑出了信号）／ <Tag color="red">拒绝</Tag>（REFUSED，
          本轮明确不交易）。拒绝理由由后端给出、原样显示在回执与「拒绝理由」列 —— 常见是那天没有六维快照、
          日期对不上、定不了下一交易日；数据补齐后同日重跑可就地升级。
        </li>
        <li>
          风险态：<Tag color="green">正常</Tag> ／ <Tag color="orange">受限</Tag>（输入域不完整）。
        </li>
        <li>
          订单：<Tag color="blue">待执行</Tag>／<Tag color="green">已成交</Tag>／
          <Tag color="red">已拒单</Tag>／<Tag color="orange">已过期</Tag>；
          拒单与过期都带原因，看「拒单/过期原因」列。
        </li>
        <li>T+1 冻结：当日买入、次日才可卖，橙色标出锁定日期。</li>
      </ul>

      <Alert
        type="info"
        showIcon
        title="展示纪律：缺了就显示缺"
        description="未开户明说、缺行情打红标、拿不到的数字一律显示 -，绝不编一个数填进去；拒单与过期都带理由。"
      />
      <Alert
        type="warning"
        showIcon
        title="数据免责声明"
        description="本页所有数据来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
      />
    </Space>
  )
}

export default function Trading() {
  const [status, setStatus] = useState<TradingStatus | null>(null)
  const [runs, setRuns] = useState<TradingRun[]>([])
  const [orders, setOrders] = useState<TradingOrder[]>([])
  const [params, setParams] = useState<TradingParams | null>(null)
  const [signals, setSignals] = useState<TradingSignal[] | null>(null)
  const [selRun, setSelRun] = useState<number | null>(null)
  const [loading, setLoading] = useState(true)
  const [loadErr, setLoadErr] = useState<string | null>(null)

  const [dateInput, setDateInput] = useState('')
  const [busyRun, setBusyRun] = useState(false)
  const [busyExec, setBusyExec] = useState(false)
  const [runResult, setRunResult] = useState<TradingRunResult | null>(null)
  const [execResult, setExecResult] = useState<TradingExecResult | null>(null)
  const [actionErr, setActionErr] = useState<string | null>(null)
  const [helpOpen, setHelpOpen] = useState(false)

  /**
   * 首屏加载：四个端点全部 GET、零联网。用 promise 链而不是 async/await：
   * load 体内没有同步 setState，effect 里 `void load()` 才不会被
   * set-state-in-effect 判为在 effect 里改状态。
   */
  const load = useCallback(
    () =>
      Promise.allSettled([
        api.tradingStatus(),
        api.tradingRuns(20),
        api.tradingOrders(100),
        api.tradingParams(),
      ])
        .then((res) => {
          const [st, rn, od, pr] = res
          if (st.status === 'fulfilled') {
            setStatus(st.value)
            setLoadErr(null)
          } else {
            setLoadErr(errMsg(st.reason))
          }
          if (rn.status === 'fulfilled') setRuns(rn.value.items)
          if (od.status === 'fulfilled') setOrders(od.value.items)
          if (pr.status === 'fulfilled') setParams(pr.value)
          setLoading(false)
        }),
    [],
  )

  useEffect(() => {
    void load()
  }, [load])

  /** 点某轮 run 的「看信号」。选中与取数都在事件里，effect 不掺和。 */
  const showSignals = (runId: number) => {
    setSelRun(runId)
    setSignals(null)
    api
      .tradingSignals(runId)
      .then((s) => setSignals(s.items))
      .catch((e: Error) => setActionErr(e.message))
  }

  /**
   * 生成一轮信号并下单。日期留空 = 后端取最近一个六维快照日；同日重跑
   * 幂等（不重复下单），REFUSED 的日子允许数据补齐后就地重试。
   */
  const runSignals = () => {
    const d = dateInput.trim()
    setActionErr(null)
    setRunResult(null)
    setBusyRun(true)
    api
      .tradingSignalRun(d ? { date: d } : {})
      .then((r) => {
        setRunResult(r)
        if (r.run_id == null) return undefined
        const runId = r.run_id
        return api.tradingSignals(runId).then((s) => {
          setSelRun(runId)
          setSignals(s.items)
        })
      })
      .catch((e: Error) => setActionErr(e.message))
      .finally(() => setBusyRun(false))
  }

  /**
   * 执行待成交订单（D+1 开盘价虚拟撮合，涨跌停与缺行情如实拒单/不成交）。
   * 执行完重量级只读状态，让现金/持仓/订单立刻反映结果。
   */
  const execOrders = () => {
    setActionErr(null)
    setExecResult(null)
    setBusyExec(true)
    api
      .tradingExecute({})
      .then((r) => {
        setExecResult(r)
        return load()
      })
      .catch((e: Error) => setActionErr(e.message))
      .finally(() => setBusyExec(false))
  }

  const positionColumns: ColumnsType<TradingPosition> = [
    { title: '代码', dataIndex: 'code', render: (c: string) => <Text code>{c}</Text> },
    { title: '名称', dataIndex: 'name', render: (n: string | null) => n ?? '-' },
    { title: '持仓', dataIndex: 'qty', align: 'right', render: (v: number) => fmtNum(v, 0) },
    {
      title: 'T+1 冻结',
      dataIndex: 'locked_qty',
      align: 'right',
      render: (v: number, r: TradingPosition) =>
        v > 0 ? <Tag color="orange">{fmtNum(v, 0)}（{fmtDate(r.locked_date)} 锁）</Tag> : '0',
    },
    { title: '成本价', dataIndex: 'avg_cost', align: 'right', render: (v: number) => fmtNum(v, 3) },
    {
      title: '成本因子',
      dataIndex: 'cost_factor',
      align: 'right',
      render: (v: number | null) => (v == null ? '-' : fmtNum(v, 4)),
    },
    { title: '建仓日', dataIndex: 'entry_date', render: (d: string) => fmtDate(d) },
  ]

  const runColumns: ColumnsType<TradingRun> = [
    { title: '#', dataIndex: 'id', width: 60 },
    { title: '信号日', dataIndex: 'signal_date', render: (d: string) => fmtDate(d) },
    { title: '执行日', dataIndex: 'exec_date', render: (d: string | null) => fmtDate(d) },
    {
      title: '状态',
      dataIndex: 'status',
      render: (s: string) => (
        <Tag color={RUN_TAG[s]?.color}>{RUN_TAG[s]?.text ?? s}</Tag>
      ),
    },
    {
      title: '风险态',
      dataIndex: 'risk_state',
      render: (v: string | null) =>
        v ? (
          <Tag color={v === 'RISK' ? 'orange' : 'green'}>
            {v === 'RISK' ? '受限' : '正常'}
          </Tag>
        ) : (
          '-'
        ),
    },
    {
      title: '候选/信号',
      dataIndex: 'candidates_n',
      render: (_: number, r: TradingRun) => `${r.candidates_n} / ${r.signals_n}`,
    },
    {
      title: '拒绝理由',
      dataIndex: 'refusal_reason',
      render: (v: string | null) => (v ? <Text type="danger">{v}</Text> : '-'),
    },
    {
      title: '操作',
      key: 'op',
      render: (_: unknown, r: TradingRun) => (
        <Button size="small" onClick={() => showSignals(r.id)}>
          看信号
        </Button>
      ),
    },
  ]

  const signalColumns: ColumnsType<TradingSignal> = [
    { title: '方向', dataIndex: 'side', width: 80, render: (s: string) => sideTag(s) },
    { title: '代码', dataIndex: 'code', render: (c: string) => <Text code>{c}</Text> },
    { title: '名称', dataIndex: 'name', render: (n: string | null) => n ?? '-' },
    {
      title: '目标数量',
      dataIndex: 'target_qty',
      align: 'right',
      render: (v: number | null) => (v == null ? '-' : fmtNum(v, 0)),
    },
    {
      title: '仓位占比',
      dataIndex: 'alloc_pct',
      align: 'right',
      render: (v: number | null) => (v == null ? '-' : `${(v * 100).toFixed(2)}%`),
    },
    { title: '参考价', dataIndex: 'est_price', align: 'right', render: (v: number | null) => fmtNum(v, 3) },
    {
      title: '行情日',
      dataIndex: 'bar_date',
      render: (d: string | null) => (d ? fmtDate(d) : <Tag color="red">缺行情</Tag>),
    },
    {
      title: '判定理由',
      dataIndex: 'reasons',
      render: (rs: TradingSignalReason[]) => (
        <Space direction="vertical" size={0}>
          {(rs ?? []).map((x, i) => (
            <span key={`${x.rule}-${i}`} style={{ fontSize: 12 }}>
              <Text code style={{ fontSize: 11 }}>
                {x.rule}
              </Text>{' '}
              <Text type="secondary" style={{ fontSize: 12 }}>
                {x.detail}
              </Text>
            </span>
          ))}
        </Space>
      ),
    },
  ]

  const orderColumns: ColumnsType<TradingOrder> = [
    { title: '单号', dataIndex: 'id', width: 70 },
    { title: '代码', dataIndex: 'code', render: (c: string) => <Text code>{c}</Text> },
    { title: '名称', dataIndex: 'name', render: (n: string | null) => n ?? '-' },
    { title: '方向', dataIndex: 'side', width: 80, render: (s: string) => sideTag(s) },
    { title: '数量', dataIndex: 'qty', align: 'right', render: (v: number) => fmtNum(v, 0) },
    {
      title: '状态',
      dataIndex: 'status',
      render: (s: string) => (
        <Tag color={ORDER_TAG[s]?.color}>{ORDER_TAG[s]?.text ?? s}</Tag>
      ),
    },
    { title: '执行日', dataIndex: 'exec_date', render: (d: string | null) => fmtDate(d) },
    { title: '委托价', dataIndex: 'est_price', align: 'right', render: (v: number | null) => fmtNum(v, 3) },
    { title: '成交价', dataIndex: 'fill_price', align: 'right', render: (v: number | null) => fmtNum(v, 3) },
    { title: '费用', dataIndex: 'fee', align: 'right', render: (v: number | null) => fmtNum(v, 2) },
    {
      title: '拒单/过期原因',
      dataIndex: 'reject_reason',
      render: (v: string | null) => (v ? <Text type="danger">{v}</Text> : '-'),
    },
  ]

  const acct = status?.account ?? null
  const positions = status?.positions ?? []
  const last = status?.last_run ?? runs[0] ?? null
  const dateOk = dateInput.trim() === '' || /^\d{8}$/.test(dateInput.trim())
  const paramRows = Object.entries(params?.defaults ?? {}).map(([k, v]) => ({
    k,
    v: typeof v === 'boolean' ? (v ? '是' : '否') : String(v),
  }))

  return (
    <Space direction="vertical" size={16} style={{ display: 'flex' }}>
      <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
        <Button icon={<QuestionCircleOutlined />} onClick={() => setHelpOpen(true)}>
          操作说明
        </Button>
      </div>

      <Alert
        type="info"
        showIcon
        title="模拟盘（paper trading）"
        description="策略 V1 只在本地库生成信号与模拟订单、按下一交易日开盘价虚拟成交：不接实盘、不发任何真实下单指令，也不会自动运行 —— 只有人点下面的按钮才推进一轮（同日重跑幂等，不重复下单）。"
      />

      {loadErr && <Alert type="error" showIcon title={loadErr} />}

      <Spin spinning={loading}>
        <Space direction="vertical" size={16} style={{ display: 'flex', width: '100%' }}>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
              gap: 12,
            }}
          >
            <Stat
              label="现金"
              value={acct ? `¥${compact(acct.cash)}` : '—'}
              hint={acct ? `¥ ${fmtNum(acct.cash, 2)}` : '首次生成信号时自动开户'}
            />
            <Stat label="初始资金" value={acct ? `¥${compact(acct.initial_cash)}` : '—'} />
            <Stat label="持仓数" value={String(positions.length)} />
            <Stat
              label="T+1 冻结"
              value={String(positions.filter((p) => (p.locked_qty ?? 0) > 0).length)}
              hint="当日买入、次日才可卖的持仓"
            />
            <Stat
              label="待执行单"
              value={String(status?.pending_orders ?? 0)}
              hint="点「执行待成交订单」按 D+1 开盘价撮合"
            />
          </div>

          {last && (
            <Card size="small" title={`最近一轮信号 #${last.id}`}>
              <Space wrap size={8}>
                <Tag color={RUN_TAG[last.status]?.color}>
                  {RUN_TAG[last.status]?.text ?? last.status}
                </Tag>
                <Text>信号日 {fmtDate(last.signal_date)}</Text>
                <Text type="secondary">执行日 {fmtDate(last.exec_date)}</Text>
                {last.risk_state && (
                  <Tag color={last.risk_state === 'RISK' ? 'orange' : 'green'}>
                    风险态 {last.risk_state === 'RISK' ? '受限（输入域不完整）' : '正常'}
                  </Tag>
                )}
                {last.quality_state && <Tag>数据质量 {last.quality_state}</Tag>}
                <Text type="secondary">
                  候选 {last.candidates_n} · 信号 {last.signals_n}
                </Text>
              </Space>
              {last.status === 'REFUSED' && last.refusal_reason && (
                <Alert
                  type="warning"
                  showIcon
                  title={last.refusal_reason}
                  style={{ marginTop: 8 }}
                />
              )}
            </Card>
          )}

          <Card size="small" title="操作">
            <Space wrap size={12}>
              <Input
                placeholder="信号日期 YYYYMMDD，留空=最近快照日"
                style={{ width: 250 }}
                maxLength={8}
                value={dateInput}
                status={dateOk ? undefined : 'error'}
                onChange={(e) => setDateInput(e.target.value)}
              />
              <Button type="primary" loading={busyRun} disabled={!dateOk} onClick={runSignals}>
                生成信号并下单
              </Button>
              <Popconfirm
                title="执行待成交订单？"
                description="按订单所属执行日的开盘价虚拟撮合（含 T+1 解锁、涨跌停拒单、缺行情不成交），结果落库。"
                okText="执行"
                cancelText="取消"
                onConfirm={execOrders}
              >
                <Button loading={busyExec}>执行待成交订单</Button>
              </Popconfirm>
            </Space>
            {actionErr && (
              <Alert type="error" showIcon title={actionErr} style={{ marginTop: 8 }} />
            )}
            {runResult && (
              <Alert
                type={runResult.status === 'OK' ? 'success' : 'warning'}
                showIcon
                style={{ marginTop: 8 }}
                title={
                  runResult.status === 'OK'
                    ? `信号已生成（run #${runResult.run_id}${runResult.idempotent ? '，同日重跑幂等命中、未重复下单' : ''}）`
                    : '本轮未生成信号（REFUSED）'
                }
                description={
                  <Space direction="vertical" size={2}>
                    {runResult.signal_date && (
                      <Text>
                        信号日 {fmtDate(runResult.signal_date)} → 执行日{' '}
                        {fmtDate(runResult.exec_date)}
                      </Text>
                    )}
                    {runResult.status === 'OK' && (
                      <Text>
                        信号：买入 {runResult.signals.BUY ?? 0} · 卖出{' '}
                        {runResult.signals.SELL ?? 0} · 不动 {runResult.signals.HOLD ?? 0}
                        ；下单 {runResult.orders.placed} 张，风控拒单{' '}
                        {runResult.orders.risk_rejected} 张
                        {runResult.portfolio?.equity != null && (
                          <>
                            ；组合权益 ¥{compact(runResult.portfolio.equity)}，总仓位{' '}
                            {(100 * (runResult.portfolio.exposure ?? 0)).toFixed(1)}%
                          </>
                        )}
                      </Text>
                    )}
                    {runResult.refusal_reason && (
                      <Text type="danger">{runResult.refusal_reason}</Text>
                    )}
                  </Space>
                }
              />
            )}
            {execResult && (
              <Alert
                type={execResult.ran ? 'success' : 'info'}
                showIcon
                style={{ marginTop: 8 }}
                title={
                  execResult.ran
                    ? `执行完成（${fmtDate(execResult.exec_date)}）：成交 ${execResult.filled} · 拒单 ${execResult.rejected} · 过期 ${execResult.expired}`
                    : `未执行：${execResult.reason ?? '没有待执行订单'}`
                }
              />
            )}
          </Card>

          <Card size="small" title="模拟持仓">
            {!status?.initialized ? (
              <Text type="secondary">
                还没有模拟账户 —— 生成第一轮信号时自动开户（初始资金取参数默认值）。
              </Text>
            ) : (
              <Table<TradingPosition>
                rowKey="code"
                size="small"
                pagination={false}
                dataSource={positions}
                columns={positionColumns}
              />
            )}
          </Card>

          <Card size="small" title="运行历史（最近 20 轮）">
            <Table<TradingRun>
              rowKey="id"
              size="small"
              dataSource={runs}
              columns={runColumns}
              pagination={{ pageSize: 10 }}
            />
          </Card>

          <Card
            size="small"
            title={
              selRun != null ? `信号明细（run #${selRun}，含 HOLD 留痕）` : '信号明细'
            }
          >
            {selRun == null ? (
              <Text type="secondary">
                在「运行历史」里点「看信号」，逐票查看买入/卖出/不动与每条判定理由。
              </Text>
            ) : signals == null ? (
              <Spin />
            ) : (
              <Table<TradingSignal>
                rowKey="id"
                size="small"
                dataSource={signals}
                columns={signalColumns}
                pagination={{ pageSize: 50 }}
              />
            )}
          </Card>

          <Card size="small" title="订单（最近 100 张）">
            <Table<TradingOrder>
              rowKey="id"
              size="small"
              dataSource={orders}
              columns={orderColumns}
              pagination={{ pageSize: 10 }}
            />
          </Card>

          <Card
            size="small"
            title={`参数默认值（${params?.strategy ?? '-'} · ${params?.rule_version ?? '-'}）`}
          >
            <Table
              rowKey="k"
              size="small"
              pagination={false}
              dataSource={paramRows}
              columns={[
                {
                  title: '参数',
                  dataIndex: 'k',
                  render: (k: string) => <Text code>{k}</Text>,
                },
                { title: '默认', dataIndex: 'v' },
              ]}
            />
            <Paragraph type="secondary" style={{ marginTop: 8, marginBottom: 0, fontSize: 12 }}>
              {Object.entries(params?.notes ?? {}).map(([k, v]) => (
                <div key={k}>{v}</div>
              ))}
              <div>
                参数版本 = 归一化 JSON 的 sha256 前 12
                位（当前默认 {params?.version_of_defaults ?? '-'}）；每轮 run
                落实际参数快照，历史订单按当时口径审定。
              </div>
            </Paragraph>
          </Card>
        </Space>
      </Spin>

      <Alert
        type="warning"
        showIcon
        title="数据免责声明"
        description="本页所有数据来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
      />

      <HelpDrawer
        title="自动交易 · 操作说明（模拟盘）"
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
      >
        <TradingHelpBody />
      </HelpDrawer>
    </Space>
  )
}
