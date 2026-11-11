import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Col,
  DatePicker,
  Divider,
  Empty,
  Form,
  Input,
  InputNumber,
  List,
  Modal,
  Popconfirm,
  Progress,
  Row,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
// 只要类型：DatePicker 传出来的值就是 antd 自带的 dayjs 实例，
// 运行期一行都不 import dayjs（它是 antd 的传递依赖，装不上独立版本）。
import type { Dayjs } from 'dayjs'
import { QuestionCircleOutlined } from '@ant-design/icons'
import {
  api,
  errMsg,
  type ResearchAudit,
  type ResearchCapabilities,
  type ResearchCreateBody,
  type ResearchExperiment,
  type ResearchMetric,
  type ResearchOutcome,
  type ResearchRun,
  type ResearchSignal,
} from '../lib/api'
import { HelpDrawer } from '../components/HelpDrawer'
import { fmtPct, signType } from '../lib/format'

const { Text, Paragraph, Title } = Typography

/**
 * 策略实验室（docs/backtest/）。
 *
 * 四个 Tab 的先后是有意的：**先看数据审计**，再建实验。无历史股票池、
 * 快照不存因子原始值、基准序列有覆盖边界 —— 这几件事决定了这个模块能回答
 * 什么问题、不能回答什么。先让人看见结论边界，比先让人填一个日期框重要。
 *
 * 全页**一个外发请求都不发**：所有端点读本地 SQLite。
 */

/** 六个因子的键名与中文名，与 `backend/config/scorer.yaml` 一一对应。 */
const DIMS: { key: string; label: string; weight: number }[] = [
  { key: 'inflow_consistency', label: '主力资金持续性', weight: 30 },
  { key: 'volume_up_price_flat', label: '量增价稳', weight: 20 },
  { key: 'large_order_ratio', label: '大单净额占比', weight: 15 },
  { key: 'price_position_low', label: '股价位置低', weight: 15 },
  { key: 'turnover_moderate', label: '换手适中', weight: 10 },
  { key: 'chip_convergence', label: '筹码收敛', weight: 10 },
]

const DIM_LABEL: Record<string, string> = Object.fromEntries(
  DIMS.map((d) => [d.key, d.label]),
)

const DOMAIN_TAG: Record<string, string> = {
  READY: 'green',
  PARTIAL: 'orange',
  BLOCKED: 'red',
}

const RUN_TAG: Record<string, string> = {
  pending: 'default',
  running: 'blue',
  done: 'green',
  cancelled: 'default',
  failed: 'red',
}

const OUTCOME_TAG: Record<string, string> = {
  OK: 'green',
  PENDING: 'default',
  NO_DATA: 'orange',
}

const FILL_TAG: Record<string, string> = {
  FILLED: 'green',
  UNFILLABLE: 'red',
  SELLABLE: 'green',
  LIMIT_DOWN: 'red',
  UNCONFIRMED: 'orange',
  NOT_DUE: 'default',
}

/**
 * 收益的复权口径三档（`engine.price_adjust`）。三档**必须看起来不一样**：
 * `UNADJUSTED` 是「确认这段没除权」，绝不能与 `RAW`（真折算过）同色同字 ——
 * 没做折算动作就不该说自己折算过。`NULL`（没算出收益）根本不进这张表。
 */
const ADJUST_TAG: Record<string, { color: string; text: string; note: string }> = {
  RAW: {
    color: 'green',
    text: '已折算',
    note: '两端 adj_factor 都有且不同 —— 收益按 exit×exit_factor / entry×entry_factor 折算过',
  },
  UNADJUSTED: {
    color: 'blue',
    text: '无除权·直算',
    note: '两端因子相同 —— 确认这段没除权，不复权价直算本就对',
  },
  UNADJUSTED_UNCERTAIN: {
    color: 'orange',
    text: '判不了',
    note: '缺 adj_factor —— 若这段期间发生过除权，该收益不反映真实回报',
  },
}

/** `NULL` 显示成 `-`，**绝不显示成 0** —— 缺数据和值为 0 是两回事。 */
const num = (v?: number | null, d = 4) => (v == null ? '-' : v.toFixed(d))

// ------------------------------------------------------------------ hooks

function useCapabilities() {
  const [caps, setCaps] = useState<ResearchCapabilities | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    void (async () => {
      try {
        setCaps(await api.researchCapabilities())
      } catch (e) {
        setError(errMsg(e))
      }
    })()
  }, [])

  return { caps, error }
}

function useAudit(enabled: boolean) {
  const [audit, setAudit] = useState<ResearchAudit | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!enabled) return
    void (async () => {
      setLoading(true)
      try {
        setAudit(await api.researchAudit())
      } catch (e) {
        setError(errMsg(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [enabled])

  return { audit, loading, error }
}

function useExperiments(enabled: boolean) {
  const [items, setItems] = useState<ResearchExperiment[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const reload = useCallback(async () => {
    setLoading(true)
    try {
      setItems((await api.researchExperiments()).items ?? [])
    } catch (e) {
      setError(errMsg(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    if (!enabled) return
    // `reload` 的第一步是同步 setState，effect 里直接调它会亮
    // `set-state-in-effect`；推到微任务里，setState 就不在 effect 的同步
    // 调用栈上了，行为完全不变。
    void Promise.resolve().then(() => reload())
  }, [enabled, reload])

  return { items, loading, error, reload }
}

/**
 * 轮询一轮运行。**只在运行中轮询** —— 结束就停，不留在后台空转。
 *
 * 全部 setState 落在 async 回调里。`runId` 变空时返回 null 是**推导**出来的
 * （`box.id === runId`），而不是在 effect 里再补一次 `setRun(null)`。
 *
 * 2026-10-18 真机修：落定后必须**清掉 interval 且只通知一次**。
 * 反过来（interval 不清、每 tick 都 `onSettled`）页面会每 2 秒重拉一次
 * 实验列表，永不落地 —— 日志里 `runs/N` + `experiments` 无限交替就是这个状态。
 * `onSettled` 也要求身份稳定（调用方用 useCallback），否则 effect 每渲染重启。
 */
function useRunPoll(runId: number | null, onSettled?: () => void) {
  const [box, setBox] = useState<{ id: number; run: ResearchRun | null }>({
    id: -1,
    run: null,
  })

  useEffect(() => {
    if (!runId) return
    let stopped = false
    let timer = 0
    const tick = async () => {
      try {
        const r = (await api.researchRunDetail(runId)).run
        if (stopped) return
        setBox({ id: runId, run: r })
        if (r.status !== 'running' && r.status !== 'pending') {
          stopped = true
          window.clearInterval(timer)
          onSettled?.()
        }
      } catch {
        /* 轮询失败不停止 —— 下一轮再试；真正的错误由运行记录本身承载 */
      }
    }
    void tick()
    timer = window.setInterval(() => void tick(), 2000)
    return () => {
      stopped = true
      window.clearInterval(timer)
    }
  }, [runId, onSettled])

  return box.id === runId ? box.run : null
}

// ------------------------------------------------------------------ 审计

function AuditTab({ caps }: { caps: ResearchCapabilities | null }) {
  const { audit, loading, error } = useAudit(true)

  const domains = useMemo(() => {
    if (!audit?.domain_status) return []
    return Object.entries(audit.domain_status).map(([name, d]) => ({
      name,
      ...d,
    }))
  }, [audit])

  return (
    <Spin spinning={loading}>
      {error && (
        <Alert
          type="error"
          showIcon
          title={error}
          style={{ marginBottom: 12 }}
        />
      )}

      {caps && (
        <Alert
          type="warning"
          showIcon
          title="这个模块能回答什么、不能回答什么"
          description={
            <List
              size="small"
              dataSource={Object.entries(caps.blocked)}
              renderItem={([k, v]) => (
                <List.Item style={{ padding: '4px 0', border: 'none' }}>
                  <Text type="secondary">
                    <Tag color="red">做不了</Tag>
                    <Text strong>{k}</Text>
                    <br />
                    {v}
                  </Text>
                </List.Item>
              )}
            />
          }
          style={{ marginBottom: 12 }}
        />
      )}

      <Card size="small" title="五域数据审计" style={{ marginBottom: 12 }}>
        <Table
          rowKey="name"
          size="small"
          loading={loading}
          pagination={false}
          dataSource={domains}
          scroll={{ x: 720 }}
          columns={[
            {
              title: '域',
              dataIndex: 'name',
              width: 180,
              render: (v: string) => <Text strong>{v}</Text>,
            },
            {
              title: '状态',
              dataIndex: 'status',
              width: 110,
              align: 'center',
              render: (v: string) => (
                <Tag color={DOMAIN_TAG[v] ?? 'default'}>{v}</Tag>
              ),
            },
            {
              title: '说明',
              dataIndex: 'detail',
              render: (v: string) => <Text type="secondary">{v}</Text>,
            },
          ]}
        />
      </Card>

      {audit?.signal_days?.length ? (
        <Card size="small" title="逐信号日可回测性">
          <Table
            rowKey="date"
            size="small"
            loading={loading}
            pagination={{ pageSize: 20 }}
            dataSource={audit.signal_days}
            columns={[
              { title: '信号日', dataIndex: 'date', width: 120 },
              {
                title: '状态',
                dataIndex: 'status',
                width: 110,
                align: 'center',
                render: (v: string) => (
                  <Tag color={DOMAIN_TAG[v] ?? 'default'}>{v}</Tag>
                ),
              },
              {
                title: '原因',
                dataIndex: 'reason',
                render: (v?: string) =>
                  v ? <Text type="secondary">{v}</Text> : '-',
              },
            ]}
          />
        </Card>
      ) : (
        <Empty description="库里还没有信号日快照" />
      )}

      {audit?.summary && (
        <Alert
          type="info"
          showIcon
          title="审计结论"
          description={audit.summary}
          style={{ marginTop: 12 }}
        />
      )}
    </Spin>
  )
}

// ------------------------------------------------------------- 实验表单

interface FormValues {
  name: string
  /** 日历选出来的值；提交时在 toBody 里折成后端要的 YYYYMMDD */
  start_date: Dayjs | null
  end_date: Dayjs | null
  n_top?: number
  min_score?: number
  horizons?: number[]
  portfolios?: string[]
  with_cost?: boolean
  use_snapshot_weights?: boolean
  weights?: Record<string, number>
}

function toBody(v: FormValues, drop?: string[]): ResearchCreateBody {
  const cfg: NonNullable<ResearchCreateBody['config']> = {
    horizons: v.horizons?.length ? v.horizons : undefined,
    portfolios: v.portfolios?.length ? v.portfolios : undefined,
    n_top: v.n_top,
    min_score: v.min_score,
  }
  // 不勾「用快照自带权重」才提交自定义权重；**缺一个键 = 那个因子被消融**，
  // 所以六个键必须一起给，否则这里按 0 补齐并显式落库 —— 不能让前端漏传
  // 静默变成一个消融实验。
  if (!v.use_snapshot_weights) {
    cfg.weights = Object.fromEntries(
      DIMS.map((d) => [d.key, v.weights?.[d.key] ?? 0]),
    )
  }
  // 成本默认不配。勾了才提交预设值，且带 `assumption: true`。
  if (v.with_cost) {
    cfg.cost = {
      commission_rate: 0.00025,
      min_commission: 5,
      stamp_rate: 0.0005,
      slippage: 0.001,
      notional: 100000,
      assumption: true,
    }
  }
  if (drop?.length) cfg.ablation = { drop }
  return {
    name: v.name,
    strategy_version: 'sixdim-v1',
    universe_mode: 'current',
    start_date: v.start_date?.format('YYYYMMDD') ?? '',
    end_date: v.end_date?.format('YYYYMMDD') ?? '',
    config: cfg,
  }
}

function ExperimentForm({
  open,
  caps,
  initial,
  drop,
  onCancel,
  onCreated,
}: {
  open: boolean
  caps: ResearchCapabilities | null
  initial?: Partial<FormValues>
  /** 因子消融 Tab 传进来：要剔除的因子键 */
  drop?: string[]
  onCancel: () => void
  onCreated: (exp: ResearchExperiment) => void
}) {
  const [form] = Form.useForm<FormValues>()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    // `err` 不在这里清：调用方用 `key` 在打开时重挂整个表单，`err`/`busy`
    // 回到初值，比在 effect 里同步 setState 干净（也躲开 set-state-in-effect）。
    form.setFieldsValue({
      name: '',
      start_date: null,
      end_date: null,
      n_top: 10,
      min_score: 0,
      horizons: caps?.horizons ?? [1, 3, 5],
      portfolios: caps?.portfolios ?? ['top5'],
      with_cost: false,
      use_snapshot_weights: true,
      weights: Object.fromEntries(DIMS.map((d) => [d.key, d.weight])),
      ...initial,
    })
  }, [open, form, caps, initial])

  const submit = async (v: FormValues) => {
    setBusy(true)
    setErr(null)
    try {
      const res = await api.researchExperimentCreate(toBody(v, drop))
      onCreated(res.experiment)
    } catch (e) {
      // 400 的理由是后端原样回的（模式 B 的拒绝文案就走这条路），直接展示
      setErr(errMsg(e))
    } finally {
      setBusy(false)
    }
  }

  const useSnapshot = Form.useWatch('use_snapshot_weights', form)
  const withCost = Form.useWatch('with_cost', form)

  return (
    <Modal
      title={drop?.length ? `新建消融实验（剔除 ${drop.length} 个因子）` : '新建实验'}
      open={open}
      onCancel={onCancel}
      onOk={() => form.submit()}
      confirmLoading={busy}
      okText="创建（配置到此冻结）"
      cancelText="取消"
      width={680}
      destroyOnHidden
    >
      {err && (
        <Alert type="error" showIcon title={err} style={{ marginBottom: 12 }} />
      )}

      {drop?.length ? (
        <Alert
          type="warning"
          showIcon
          title="这是一次消融实验"
          description={
            <>
              将剔除：
              {drop.map((k) => (
                <Tag key={k}>{DIM_LABEL[k] ?? k}</Tag>
              ))}
              。被剔除因子的权重**归零**，其余不变。
              <br />
              结论边界：它只回答「换这套权重后排名与收益怎么变」，
              **不宣称该因子的边际贡献**。
            </>
          }
          style={{ marginBottom: 12 }}
        />
      ) : null}

      <Form
        form={form}
        labelCol={{ span: 7 }}
        wrapperCol={{ span: 16 }}
        onFinish={(v) => void submit(v)}
      >
        <Form.Item
          name="name"
          label="实验名"
          rules={[{ required: true, message: '给实验起个名字' }]}
        >
          <Input placeholder="例：六维前5 · 默认权重" maxLength={80} />
        </Form.Item>

        <Row gutter={12}>
          <Col span={12}>
            <Form.Item
              name="start_date"
              label="起始日"
              rules={[{ required: true, message: '必填' }]}
            >
              <DatePicker style={{ width: '100%' }} placeholder="点选回测首日" />
            </Form.Item>
          </Col>
          <Col span={12}>
            <Form.Item
              name="end_date"
              label="结束日"
              rules={[{ required: true, message: '必填' }]}
            >
              <DatePicker style={{ width: '100%' }} placeholder="点选回测末日" />
            </Form.Item>
          </Col>
        </Row>

        <Form.Item
          name="horizons"
          label="观察期"
          extra="只能取这五个值。多选 = 同一次运行里多切几刀，多重比较问题更重"
        >
          <Select
            mode="multiple"
            placeholder="选观察期"
            options={(caps?.horizons ?? []).map((h) => ({
              value: h,
              label: `T+${h}`,
            }))}
          />
        </Form.Item>

        <Form.Item name="portfolios" label="组合规则">
          <Select
            mode="multiple"
            placeholder="选组合规则"
            options={(caps?.portfolios ?? []).map((p) => ({ value: p, label: p }))}
          />
        </Form.Item>

        <Row gutter={12}>
          <Col span={8}>
            <Form.Item name="n_top" label="前 N 名" extra="研究层取前多少名">
              <InputNumber min={1} max={100} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
          <Col span={8}>
            <Form.Item name="min_score" label="最低分" extra="低于此分不入选">
              <InputNumber min={0} max={100} style={{ width: '100%' }} />
            </Form.Item>
          </Col>
        </Row>

        <Divider style={{ margin: '8px 0' }} />

        <Form.Item
          name="use_snapshot_weights"
          valuePropName="checked"
          label="权重"
          extra="推荐勾选：按快照入库时自己的权重重放，等于复现当初那个口径"
        >
          <Checkbox>用快照自带权重</Checkbox>
        </Form.Item>

        {!useSnapshot && (
          <Form.Item label="自定义权重" required>
            <Row gutter={[8, 8]}>
              {DIMS.map((d) => (
                <Col span={8} key={d.key}>
                  <Form.Item
                    name={['weights', d.key]}
                    noStyle
                    rules={[{ required: true, message: '六个键都要填' }]}
                  >
                    <InputNumber
                      min={0}
                      max={100}
                      style={{ width: '100%' }}
                      addonBefore={d.label}
                    />
                  </Form.Item>
                </Col>
              ))}
            </Row>
            <Text type="secondary" style={{ fontSize: 12 }}>
              六个键必须一起给 —— 少一个就等于把那个因子消融了，而你可能并没这个意思。
            </Text>
          </Form.Item>
        )}

        <Form.Item
          name="with_cost"
          valuePropName="checked"
          label="交易成本"
          extra="默认不勾：没有业务方确认的费率就不凭空扣一笔，可执行收益留空"
        >
          <Checkbox>
            按研究假设扣费
            <Tooltip title="佣金双向 万2.5 + 5 元下限 / 卖出印花税 千0.5 / 滑点 0.1%。这是研究假设，不等于任何人的实际券商费率。">
              <Text type="secondary" style={{ marginLeft: 6 }}>
                （假设值）
              </Text>
            </Tooltip>
          </Checkbox>
        </Form.Item>

        {withCost && (
          <Alert
            type="warning"
            showIcon
            title="这组费率是研究假设"
            description="佣金 万2.5（最低 5 元）、卖出印花税 千0.5、滑点 0.1%、名义本金 10 万。运行时会在 warnings 里再提醒一次。"
            style={{ marginBottom: 8 }}
          />
        )}
      </Form>
    </Modal>
  )
}

// ------------------------------------------------------------ 实验与运行

function RunCard({ run }: { run: ResearchRun }) {
  return (
    <Card
      size="small"
      title={
        <Space>
          <Text strong>运行 #{run.id}</Text>
          <Tag color={RUN_TAG[run.status] ?? 'default'}>{run.status}</Tag>
          <Text type="secondary">{run.stage}</Text>
        </Space>
      }
      style={{ marginBottom: 12 }}
    >
      <Progress percent={Math.round((run.progress ?? 0) * 100)} size="small" />
      {run.message && (
        <Paragraph type="secondary" style={{ marginBottom: 8 }}>
          {run.message}
        </Paragraph>
      )}
      {run.error && (
        <Alert
          type="error"
          showIcon
          title={run.error}
          style={{ marginBottom: 8 }}
        />
      )}
      {!!run.warnings?.length && (
        <Alert
          type="warning"
          showIcon
          title={`运行警告 ${run.warnings.length} 条`}
          description={
            <List
              size="small"
              dataSource={run.warnings}
              renderItem={(w) => (
                <List.Item style={{ padding: '2px 0', border: 'none' }}>
                  <Text type="warning" style={{ fontSize: 12 }}>
                    {w}
                  </Text>
                </List.Item>
              )}
            />
          }
        />
      )}
    </Card>
  )
}

function ExperimentsTab({
  caps,
  onOpen,
}: {
  caps: ResearchCapabilities | null
  onOpen: (id: number) => void
}) {
  const { items, loading, error, reload } = useExperiments(true)
  const [creating, setCreating] = useState(false)
  const [runId, setRunId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  // 身份必须稳定：useRunPoll 的 effect 依赖它，内联箭头会让 effect 每渲染重启
  const onRunSettled = useCallback(() => void reload(), [reload])
  const run = useRunPoll(runId, onRunSettled)

  const startRun = useCallback(async (eid: number) => {
    setBusy(true)
    try {
      const r = (await api.researchExperimentRun(eid)).run
      setRunId(r.id)
    } catch (e) {
      // 409 = 已有未完成运行。后端在同一把锁里拒绝，不会静默多开一条。
      Modal.warning({ title: '无法启动', content: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }, [])

  const cancelRun = async (rid: number) => {
    setBusy(true)
    try {
      await api.researchRunCancel(rid)
      await reload()
    } catch (e) {
      Modal.error({ title: '取消失败', content: errMsg(e) })
    } finally {
      setBusy(false)
    }
  }

  const removeExp = useCallback(
    async (eid: number) => {
      setBusy(true)
      try {
        await api.researchExperimentDelete(eid)
        await reload()
      } catch (e) {
        // 409 = 还有未完成的运行（或线程在收尾）；理由后端给，前端原样呈现。
        Modal.error({ title: '无法删除', content: errMsg(e) })
      } finally {
        setBusy(false)
      }
    },
    [reload],
  )

  const columns: ColumnsType<ResearchExperiment> = useMemo(
    () => [
      { title: 'ID', dataIndex: 'id', width: 60, align: 'center' },
      {
        title: '实验名',
        dataIndex: 'name',
        ellipsis: true,
        render: (v: string, r) => (
          <a onClick={() => onOpen(r.id)}>{v}</a>
        ),
      },
      {
        title: '股票池',
        dataIndex: 'universe_mode',
        width: 90,
        align: 'center',
        render: (v: string) =>
          v === 'historical' ? (
            <Tag color="red">B·禁用</Tag>
          ) : (
            <Tag color="blue">A·当前</Tag>
          ),
      },
      {
        title: '区间',
        width: 180,
        render: (_, r) => (
          <Text type="secondary">
            {r.start_date} ~ {r.end_date}
          </Text>
        ),
      },
      {
        title: '配置哈希',
        dataIndex: 'config_hash',
        width: 110,
        ellipsis: true,
        render: (v: string) => (
          <Tooltip title={v}>
            <Text type="secondary" code>
              {String(v).slice(0, 8)}
            </Text>
          </Tooltip>
        ),
      },
      {
        title: '操作',
        width: 210,
        render: (_, r) => (
          <Space size={4}>
            <Button size="small" onClick={() => onOpen(r.id)}>
              查看
            </Button>
            <Button
              size="small"
              type="primary"
              loading={busy}
              onClick={() => void startRun(r.id)}
            >
              运行
            </Button>
            <Popconfirm
              title="删除这个实验？"
              description="连同它的全部运行、信号、回测结果与指标一并删除，不可恢复。"
              okText="删除"
              cancelText="取消"
              okButtonProps={{ danger: true }}
              onConfirm={() => void removeExp(r.id)}
            >
              <Button size="small" danger loading={busy}>
                删除
              </Button>
            </Popconfirm>
          </Space>
        ),
      },
    ],
    [busy, onOpen, startRun, removeExp],
  )

  return (
    <>
      {error && (
        <Alert type="error" showIcon title={error} style={{ marginBottom: 12 }} />
      )}

      <Card
        size="small"
        title="实验列表"
        extra={
          <Button type="primary" onClick={() => setCreating(true)}>
            新建实验
          </Button>
        }
        style={{ marginBottom: 12 }}
      >
        <Table<ResearchExperiment>
          rowKey="id"
          size="small"
          loading={loading}
          dataSource={items}
          columns={columns}
          pagination={{ pageSize: 10 }}
          scroll={{ x: 870 }}
        />
      </Card>

      {run && (
        <>
          <RunCard run={run} />
          {(run.status === 'running' || run.status === 'pending') && (
            <Popconfirm
              title="取消这轮运行？"
              description="协作式取消：已写入的信号保留，指标不写 —— 半成品不冒充完整结果。"
              okText="取消运行"
              cancelText="继续跑"
              okButtonProps={{ danger: true }}
              onConfirm={() => void cancelRun(run.id)}
            >
              <Button danger loading={busy}>
                取消这轮运行
              </Button>
            </Popconfirm>
          )}
        </>
      )}

      <ExperimentForm
        key={creating ? 'open' : 'closed'}
        open={creating}
        caps={caps}
        onCancel={() => setCreating(false)}
        onCreated={(e) => {
          setCreating(false)
          void reload()
          void startRun(e.id)
        }}
      />
    </>
  )
}

// ------------------------------------------------------------ 信号与结果

function ResultsTab({ selected }: { selected: number | null }) {
  const [experiments, setExperiments] = useState<ResearchExperiment[]>([])
  const [picked, setPicked] = useState<number | null>(null)
  const [horizon, setHorizon] = useState<number | undefined>(undefined)
  const [metrics, setMetrics] = useState<ResearchMetric[]>([])
  const [signals, setSignals] = useState<ResearchSignal[]>([])
  const [outcomes, setOutcomes] = useState<ResearchOutcome[]>([])
  const [truncated, setTruncated] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  /**
   * 「从实验与运行点过来的查看」优先，其次是本页自己选的，再次是列表首条。
   *
   * **推导而不是 effect 同步**：把 `selected` 拷进 state 需要一个 effect，
   * 而那正好是 `set-state-in-effect` 要拦的写法 —— 推导一次到位，没有中间渲染。
   */
  const eid = selected ?? picked ?? experiments[0]?.id ?? null

  useEffect(() => {
    void (async () => {
      try {
        setExperiments((await api.researchExperiments()).items ?? [])
      } catch (e) {
        setError(errMsg(e))
      }
    })()
  }, [])

  useEffect(() => {
    if (!eid) return
    void (async () => {
      setLoading(true)
      setError(null)
      try {
        const [m, s, o] = await Promise.all([
          api.researchMetrics(eid),
          api.researchSignals(eid, 200, 0),
          api.researchOutcomes(eid, horizon, 1000),
        ])
        setMetrics(m.items ?? [])
        setSignals(s.items ?? [])
        setOutcomes(o.items ?? [])
        setTruncated(o.truncated)
      } catch (e) {
        setError(errMsg(e))
      } finally {
        setLoading(false)
      }
    })()
  }, [eid, horizon])

  const signalCols: ColumnsType<ResearchSignal> = useMemo(
    () => [
      { title: '信号日', dataIndex: 'signal_date', width: 100 },
      {
        title: '代码',
        dataIndex: 'code',
        width: 90,
        render: (v: string, r) => `${v} ${r.name}`,
      },
      {
        title: '快照排名',
        dataIndex: 'snapshot_rank',
        width: 85,
        align: 'center',
      },
      {
        title: '重放排名',
        dataIndex: 'replay_rank',
        width: 85,
        align: 'center',
        render: (v: number, r) =>
          v === r.snapshot_rank ? (
            v
          ) : (
            <Text type="warning">
              {v}
              <Tooltip title="与快照排名不同：快照本身没有并列裁决，引擎按 code 升序固定裁决。这是已知且有意的差异。">
                <Text type="warning" style={{ marginLeft: 4 }}>
                  ≠
                </Text>
              </Tooltip>
            </Text>
          ),
      },
      {
        title: '快照分',
        dataIndex: 'snapshot_score',
        width: 80,
        align: 'right',
        render: (v?: number | null) => (v == null ? '-' : v.toFixed(2)),
      },
      {
        title: '重放分',
        dataIndex: 'replay_score',
        width: 80,
        align: 'right',
        render: (v?: number | null) =>
          v == null ? '-' : <Text code>{v.toFixed(2)}</Text>,
      },
      {
        title: '覆盖率',
        dataIndex: 'coverage',
        width: 80,
        align: 'right',
        render: (v?: number | null) => (v == null ? '-' : v.toFixed(2)),
      },
    ],
    [],
  )

  const outcomeCols: ColumnsType<ResearchOutcome> = useMemo(
    () => [
      { title: '信号日', dataIndex: 'signal_date', width: 100 },
      {
        title: '代码',
        dataIndex: 'code',
        width: 130,
        render: (v: string, r) => `${v} ${r.name}`,
      },
      {
        title: '观察期',
        dataIndex: 'horizon',
        width: 80,
        align: 'center',
        render: (v: number) => `T+${v}`,
      },
      {
        title: '状态',
        dataIndex: 'status',
        width: 100,
        align: 'center',
        render: (v: string) => (
          <Tag color={OUTCOME_TAG[v] ?? 'default'}>{v}</Tag>
        ),
      },
      {
        title: '复权口径',
        dataIndex: 'adjust_method',
        width: 120,
        align: 'center',
        render: (v: string | null, r) => {
          // NULL = 没算出收益，谈不上口径。显示 `-` 而不是某一档，
          // 更不是 0 —— 缺口径与「判不了」是两回事，后者至少算过了。
          if (!v) {
            return (
              <Tooltip title="这行没算出收益，也就谈不上口径 —— 不填某个值冒充">
                <Text type="secondary">-</Text>
              </Tooltip>
            )
          }
          const a = ADJUST_TAG[v]
          return (
            <Tooltip title={r.adjust_note || a?.note || v}>
              <Tag color={a?.color ?? 'default'}>{a?.text ?? v}</Tag>
            </Tooltip>
          )
        },
      },
      {
        title: '入场价',
        dataIndex: 'entry_price',
        width: 90,
        align: 'right',
        render: (v?: number | null) => num(v, 2),
      },
      {
        title: '出场价',
        dataIndex: 'exit_price',
        width: 90,
        align: 'right',
        render: (v?: number | null) => num(v, 2),
      },
      {
        title: '理论收益',
        dataIndex: 'theory_return',
        width: 100,
        align: 'right',
        render: (v?: number | null) =>
          v == null ? (
            '-'
          ) : (
            <Text type={signType(v * 100)}>{fmtPct(v * 100)}</Text>
          ),
      },
      {
        title: '超额收益',
        dataIndex: 'excess_return',
        width: 100,
        align: 'right',
        render: (v?: number | null) =>
          v == null ? (
            <Tooltip title="基准取自库里的沪深300（与审计同判据）；覆盖不到的信号日或两端没对上就留空 —— 不拿当前点位冒充历史值">
              <Text type="secondary">-</Text>
            </Tooltip>
          ) : (
            <Text type={signType(v * 100)}>{fmtPct(v * 100)}</Text>
          ),
      },
      {
        title: '可执行收益',
        dataIndex: 'exec_return',
        width: 110,
        align: 'right',
        render: (v?: number | null) =>
          v == null ? (
            <Tooltip title="两端都确认可成交且配置了成本模型时才有；默认不配成本则一律留空">
              <Text type="secondary">-</Text>
            </Tooltip>
          ) : (
            <Text type={signType(v * 100)}>{fmtPct(v * 100)}</Text>
          ),
      },
      {
        title: '买入',
        dataIndex: 'fill_status',
        width: 100,
        align: 'center',
        render: (v?: string | null) =>
          v ? <Tag color={FILL_TAG[v] ?? 'default'}>{v}</Tag> : '-',
      },
      {
        title: '卖出',
        dataIndex: 'sell_status',
        width: 100,
        align: 'center',
        render: (v?: string | null) =>
          v ? <Tag color={FILL_TAG[v] ?? 'default'}>{v}</Tag> : '-',
      },
      {
        title: '缺失/质量说明',
        dataIndex: 'quality_notes',
        width: 260,
        ellipsis: true,
        render: (v: string[] | undefined, r) => {
          const all = [...(v ?? []), r.missing_reason].filter(Boolean)
          if (!all.length) return '-'
          return (
            <Tooltip title={all.join('；')}>
              <Text type="secondary" ellipsis>
                {all.join('；')}
              </Text>
            </Tooltip>
          )
        },
      },
    ],
    [],
  )

  const metricCols: ColumnsType<ResearchMetric> = useMemo(
    () => [
      {
        title: '组合',
        dataIndex: 'portfolio_rule',
        width: 90,
        align: 'center',
      },
      {
        title: '观察期',
        dataIndex: 'horizon',
        width: 80,
        align: 'center',
        render: (v: number) => `T+${v}`,
      },
      {
        title: '样本',
        dataIndex: 'sample_size',
        width: 70,
        align: 'right',
        render: (v: number) =>
          v < 30 ? (
            <Tooltip title="样本量不足 30：均值不稳，不要只看均值">
              <Text type="warning">{v}</Text>
            </Tooltip>
          ) : (
            v
          ),
      },
      {
        title: '未到期/缺数据',
        width: 120,
        align: 'center',
        render: (_, r) => (
          <Text type="secondary">
            {r.pending_count} / {r.no_data_count}
          </Text>
        ),
      },
      {
        title: '理论均值',
        dataIndex: 'mean_theory_return',
        width: 100,
        align: 'right',
        render: (v?: number | null) =>
          v == null ? '-' : <Text type={signType(v * 100)}>{fmtPct(v * 100)}</Text>,
      },
      {
        title: '中位数',
        dataIndex: 'median_theory_return',
        width: 90,
        align: 'right',
        render: (v?: number | null) =>
          v == null ? '-' : <Text type={signType(v * 100)}>{fmtPct(v * 100)}</Text>,
      },
      {
        title: '有效成交',
        dataIndex: 'valid_fills',
        width: 90,
        align: 'right',
      },
      {
        title: '买不进率',
        dataIndex: 'unfilled_rate',
        width: 95,
        align: 'right',
        render: (v?: number | null) => num(v, 3),
      },
      {
        title: '交易成本',
        dataIndex: 'trading_cost',
        width: 90,
        align: 'right',
        render: (v?: number | null) =>
          v == null ? (
            <Tooltip title="默认不配成本 —— 没有业务方确认的费率就不凭空扣一笔">
              <Text type="secondary">-</Text>
            </Tooltip>
          ) : (
            num(v, 4)
          ),
      },
      {
        title: '最大回撤',
        dataIndex: 'max_drawdown',
        width: 95,
        align: 'right',
        render: (v?: number | null) => (
          <Tooltip title="留空：未构建逐日组合净值曲线，拿个股收益排开算回撤是假的">
            <Text type="secondary">{v == null ? '-' : fmtPct(v)}</Text>
          </Tooltip>
        ),
      },
      {
        title: '质量说明',
        dataIndex: 'quality_notes',
        width: 300,
        ellipsis: true,
        render: (v?: string[]) => {
          if (!v?.length) return '-'
          return (
            <Tooltip title={v.join('；')}>
              <Text type="secondary" ellipsis>
                {v.join('；')}
              </Text>
            </Tooltip>
          )
        },
      },
    ],
    [],
  )

  return (
    <Spin spinning={loading}>
      <Card
        size="small"
        style={{ marginBottom: 12 }}
        title="选一个实验看结果"
        extra={
          <Space>
            <Select
              style={{ width: 300 }}
              placeholder="选实验"
              value={eid}
              onChange={setPicked}
              options={experiments.map((e) => ({
                value: e.id,
                label: `#${e.id} ${e.name}`,
              }))}
            />
            <Select
              style={{ width: 140 }}
              placeholder="全部观察期"
              allowClear
              value={horizon}
              onChange={setHorizon}
              options={[1, 3, 5, 10, 20].map((h) => ({
                value: h,
                label: `T+${h}`,
              }))}
            />
          </Space>
        }
      >
        {error && <Alert type="error" showIcon title={error} />}
        {!eid && <Empty description="还没有实验，先去「实验与运行」建一个" />}
      </Card>

      {eid && (
        <>
          <Card
            size="small"
            title="汇总指标（组合 × 观察期）"
            style={{ marginBottom: 12 }}
          >
            <Paragraph type="secondary" style={{ fontSize: 12 }}>
              幸存者偏差、缺基准、样本量不足、为何没有最大回撤 ——
              这些说明和数字一起显示，不能只放在文档里。
            </Paragraph>
            <Table<ResearchMetric>
              rowKey="id"
              size="small"
              dataSource={metrics}
              columns={metricCols}
              pagination={false}
              scroll={{ x: 1300 }}
            />
          </Card>

          <Card size="small" title={`信号（${signals.length} 条）`} style={{ marginBottom: 12 }}>
            <Paragraph type="secondary" style={{ fontSize: 12 }}>
              分值对拍容差 0.06。「重放排名 ≠ 快照排名」是已知且有意的差异：
              快照本身没有并列裁决，引擎按 code 升序固定裁决。
            </Paragraph>
            <Table<ResearchSignal>
              rowKey="id"
              size="small"
              dataSource={signals}
              columns={signalCols}
              pagination={{ pageSize: 20 }}
              scroll={{ x: 760 }}
            />
          </Card>

          <Card
            size="small"
            title={`逐条结果（${outcomes.length} 条）${truncated ? ' · 已截断' : ''}`}
          >
            {truncated && (
              <Alert
                type="warning"
                showIcon
                title="结果已截断"
                description="行数 = 信号数 × 观察期数。请用上方的观察期筛选缩小范围。"
                style={{ marginBottom: 8 }}
              />
            )}
            <Table<ResearchOutcome>
              rowKey="id"
              size="small"
              dataSource={outcomes}
              columns={outcomeCols}
              pagination={{ pageSize: 20, showTotal: (t) => `共 ${t} 条` }}
              scroll={{ x: 1500 }}
            />
          </Card>
        </>
      )}
    </Spin>
  )
}

// -------------------------------------------------------------- 因子消融

function AblationTab({ caps }: { caps: ResearchCapabilities | null }) {
  const { items, reload } = useExperiments(true)
  const [drop, setDrop] = useState<string[]>([])
  const [creating, setCreating] = useState(false)
  const [baseline, setBaseline] = useState<number | null>(null)
  const [ablated, setAblated] = useState<number | null>(null)
  const [baseMetrics, setBaseMetrics] = useState<ResearchMetric[]>([])
  const [ablMetrics, setAblMetrics] = useState<ResearchMetric[]>([])

  const loadMetrics = useCallback(async (id: number) =>
    (await api.researchMetrics(id)).items ?? [], [])

  useEffect(() => {
    if (!baseline) return
    void (async () => {
      setBaseMetrics(await loadMetrics(baseline))
    })()
  }, [baseline, loadMetrics])

  useEffect(() => {
    if (!ablated) return
    void (async () => {
      setAblMetrics(await loadMetrics(ablated))
    })()
  }, [ablated, loadMetrics])

  /** 同组合、同观察期才可比。对不上号的行直接不进对比表。 */
  const diff = useMemo(() => {
    const key = (r: ResearchMetric) => `${r.portfolio_rule}|${r.horizon}`
    const am = new Map(ablMetrics.map((r) => [key(r), r]))
    const out: { base: ResearchMetric; abl: ResearchMetric }[] = []
    for (const b of baseMetrics) {
      const a = am.get(key(b))
      // 组合或观察期对不上的直接丢掉 —— 拿 T+3 去对 T+5 说「因子有用」是错的
      if (a) out.push({ base: b, abl: a })
    }
    return out
  }, [baseMetrics, ablMetrics])

  return (
    <>
      <Alert
        type="info"
        showIcon
        title="消融能回答什么"
        description={
          <>
            它只回答「换这套权重后，排名与收益怎么变」。
            <strong>不宣称因子的边际贡献</strong> ——
            本库的 <Text code>main_force</Text> 快照只存每个维度的百分位和权重，
            **不存因子原始值**，所以没法拆到单因子因果贡献。
            <br />
            被剔除因子的权重是<strong>归零</strong>，不是删键（删掉会从回退路径重新计入）。
          </>
        }
        style={{ marginBottom: 12 }}
      />

      <Card
        size="small"
        title="建一个消融实验"
        style={{ marginBottom: 12 }}
        extra={
          <Space>
            <Button
              type="primary"
              disabled={!drop.length}
              onClick={() => setCreating(true)}
            >
              新建消融实验
            </Button>
          </Space>
        }
      >
        <Form layout="inline">
          <Form.Item label="剔除因子">
            <Select
              mode="multiple"
              style={{ minWidth: 420 }}
              placeholder="选要剔除的因子（权重将归零）"
              value={drop}
              onChange={setDrop}
              options={DIMS.map((d) => ({
                value: d.key,
                label: `${d.label}（权重 ${d.weight}）`,
              }))}
            />
          </Form.Item>
        </Form>
        {!!drop.length && (
          <Paragraph type="warning" style={{ marginTop: 8, marginBottom: 0 }}>
            将剔除：
            {drop.map((k) => (
              <Tag key={k}>{DIM_LABEL[k] ?? k}</Tag>
            ))}
            ，其权重归零、其余不变。
          </Paragraph>
        )}
      </Card>

      <Card
        size="small"
        title="对比：基线 vs 消融"
        extra={
          <Space>
            <Select
              style={{ width: 260 }}
              placeholder="基线实验"
              allowClear
              value={baseline}
              onChange={setBaseline}
              options={items.map((e) => ({ value: e.id, label: `#${e.id} ${e.name}` }))}
            />
            <Select
              style={{ width: 260 }}
              placeholder="消融实验"
              allowClear
              value={ablated}
              onChange={setAblated}
              options={items.map((e) => ({ value: e.id, label: `#${e.id} ${e.name}` }))}
            />
          </Space>
        }
      >
        {baseline && ablated && baseline === ablated && (
          <Alert
            type="warning"
            showIcon
            title="基线和消融选了同一个实验 —— 对比没有意义"
            style={{ marginBottom: 8 }}
          />
        )}
        {baseline && ablated && baseline !== ablated && (
          <Table
            rowKey={(r) => `${r.base.id}-${r.base.portfolio_rule}-${r.base.horizon}`}
            size="small"
            pagination={false}
            dataSource={diff}
            scroll={{ x: 900 }}
            columns={[
              {
                title: '组合 / 观察期',
                width: 160,
                render: (_, r) => (
                  <Text strong>
                    {r.base.portfolio_rule} · T+{r.base.horizon}
                  </Text>
                ),
              },
              {
                title: '基线理论均值',
                width: 130,
                align: 'right',
                render: (_, r) =>
                  r.base.mean_theory_return == null ? (
                    '-'
                  ) : (
                    <Text type={signType(r.base.mean_theory_return * 100)}>
                      {fmtPct(r.base.mean_theory_return * 100)}
                    </Text>
                  ),
              },
              {
                title: '消融理论均值',
                width: 130,
                align: 'right',
                render: (_, r) => {
                  const a = r.abl.mean_theory_return
                  if (a == null) return '-'
                  return <Text type={signType(a * 100)}>{fmtPct(a * 100)}</Text>
                },
              },
              {
                title: '差异',
                width: 110,
                align: 'right',
                render: (_, r) => {
                  const a = r.abl?.mean_theory_return
                  const b = r.base.mean_theory_return
                  if (a == null || b == null) return '-'
                  const d = (a - b) * 100
                  return <Text type={signType(d)}>{fmtPct(d)}</Text>
                },
              },
              {
                title: '样本（基线/消融）',
                width: 130,
                align: 'center',
                render: (_, r) => (
                  <Text type="secondary">
                    {r.base.sample_size} / {r.abl.sample_size}
                  </Text>
                ),
              },
              {
                title: '说明',
                ellipsis: true,
                render: (_, r) => {
                  const notes = [
                    ...(r.base.quality_notes ?? []),
                    ...(r.abl?.quality_notes ?? []),
                  ]
                  if (!notes.length) return '-'
                  return (
                    <Tooltip title={notes.join('；')}>
                      <Text type="secondary" ellipsis>
                        {notes.join('；')}
                      </Text>
                    </Tooltip>
                  )
                },
              },
            ]}
          />
        )}
        {!(baseline && ablated) && (
          <Empty description="选两个实验对比（基线 vs 消融）" />
        )}
      </Card>

      <ExperimentForm
        key={creating ? 'open' : 'closed'}
        open={creating}
        caps={caps}
        drop={drop}
        onCancel={() => setCreating(false)}
        onCreated={(e) => {
          setCreating(false)
          void reload()
          // 停在本页、对比下拉自动选中消融侧。不跳去结果 Tab ——
          // 消融实验建完还没跑，那边只有空表（运行仍要回「②」手动点）
          setAblated(e.id)
        }}
      />
    </>
  )
}

// ------------------------------------------------------------------ 页面

/**
 * 「操作说明」抽屉正文。**与页面实际控件一字不差地对齐**，按「控件字典」组织：
 * 每个按钮、每个表单字段、每张表各占一条 —— 写清楚「填什么 / 点了会发生什么 /
 * 出错长什么样」，不写成看不出所以然的宽泛叙述。免责口径与页脚同源。
 * 改页面控件时同步改这里，否则说明书会先于功能过期。
 */
function LabHelpBody() {
  return (
    <Space direction="vertical" size={12} style={{ display: 'block' }}>
      <Alert
        type="info"
        showIcon
        title="这页做什么"
        description="把已经发生过的主力打分快照拿回来，按当时可见的分值重建「前 N 名」规则，再看这些股票之后 1/3/5/10/20 个交易日的表现。只做历史研究与模拟：不接真实交易、不发下单指令；全页零联网，所有端点读本地 SQLite。"
      />

      <Title level={5} style={{ marginTop: 4 }}>
        四条结论边界（先看这个，再填日期）
      </Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>股票池 B·历史：禁用。</Text>
          本库没有历史股票池，实验一律按当前名单跑（列表里 <Tag color="blue">A·当前</Tag>{' '}
          才是可用的那个）；拿今天的名单回看过去有幸存者偏差，所以 <Tag color="red">B·禁用</Tag>
          不是没做完，是这个口径做出来会骗人。
        </li>
        <li>
          <Text strong>信号不重算（有意不做）。</Text>
          日线已落库，但重算要跨窗口比价就得用复权价 —— 前复权会用未来的除权事件改写历史价格，
          重算的每个分都被未来信息污染。所以只重放快照里已有的分值与排名。
        </li>
        <li>
          <Text strong>超额收益：基准取自库里的沪深300。</Text>
          基准覆盖不到的信号日、或两端没对上时整列留空 —— 不拿当前点位冒充历史值，也绝不填 0。
        </li>
        <li>
          <Text strong>最大回撤：恒留空。</Text>
          尚未构建逐日组合净值曲线；拿个股收益排开算一个回撤是假的，宁可不给。
        </li>
        <li>
          动态的那份见 ① 顶部「这个模块能回答什么、不能回答什么」黄条，随能力接口返回，以它为准。
        </li>
      </ul>

      <Divider style={{ margin: '4px 0' }} />

      <Title level={5}>① 数据审计 Tab —— 纯读数，本 Tab 没有任何按钮</Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>「这个模块能回答什么、不能回答什么」黄条</Text>：逐条列出做不了的事，
          每条自带红色 <Tag color="red">做不了</Tag> 标签和具体理由 —— 不是「暂未开放」这种空话。
        </li>
        <li>
          <Text strong>「五域数据审计」表</Text>：一行一个数据域（日线 / 资金流 / 股票池 / 基准 / 打分快照）。
          状态三档：<Tag color="green">READY</Tag> 数据齐 ·{' '}
          <Tag color="orange">PARTIAL</Tag> 能用但有缺口 · <Tag color="red">BLOCKED</Tag> 不可用；
          「说明」列写清缺什么、缺多少。
        </li>
        <li>
          <Text strong>「逐信号日可回测性」表</Text>：一行 = 一个主力打分快照日。
          <Tag color="green">READY</Tag> = 那天可回测；<Tag color="red">BLOCKED</Tag> 的「原因」列
          直接写缺什么（例：信号日前仅 4 日主力净额 &lt; 所需 5 日）。库里还没有任何快照时显示空态
          「库里还没有信号日快照」。
        </li>
        <li>
          <Text strong>「审计结论」蓝条</Text>：整体状态、信号日总数与其中可回测数、未就绪的域，
          以及当前能做哪种回测（仅重放 / 严格全模型）。
        </li>
      </ul>

      <Divider style={{ margin: '4px 0' }} />

      <Title level={5}>② 实验与运行 Tab —— 每个按钮做什么</Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>「新建实验」（实验列表右上角蓝钮）</Text>：打开新建弹窗（字段逐个见下一节）。
          创建成功后列表刷新，并<strong>自动为它开第一轮运行</strong> —— 不用再手点一次运行。
        </li>
        <li>
          <Text strong>行内「查看」</Text>：跳到「③ 信号与结果」并选中该实验；点实验名链接效果相同。
        </li>
        <li>
          <Text strong>行内「运行」（蓝钮）</Text>：给该实验起一轮后台运行，列表下方出现运行卡。
          该实验已有未完成运行时弹「无法启动」—— 后端 409 拒绝，不会静默多开一轮。
        </li>
        <li>
          <Text strong>行内「删除」（红钮）</Text>：先二次确认，再<strong>级联删除</strong> ——
          连同它的全部运行、信号、回测结果与指标一并删除、不可恢复；有未完成运行时删除同样被 409 拒。
        </li>
        <li>
          <Text strong>运行卡「取消这轮运行」（红钮，仅运行中出现）</Text>：协作式取消 ——
          已写入的信号保留、指标不写，半成品不冒充完整结果。
        </li>
        <li>
          <Text strong>运行卡上的读数</Text>：状态标签五档 pending（灰）/{' '}
          <Tag color="blue">running</Tag>（蓝）/ <Tag color="green">done</Tag>（绿）/
          cancelled（灰）/ <Tag color="red">failed</Tag>（红）；下面依次是进度条、阶段文字、message；
          失败时 error 红框；勾过成本假设，warnings 黄框里会再提醒一次。
        </li>
        <li>
          <Text strong>列表没有编辑入口</Text>：配置创建即冻结，换配置就新建一个实验。
          「配置哈希」列显示归一化配置哈希前 8 位（悬停看全值），两次实验配置是否一致对它即可。
        </li>
      </ul>

      <Divider style={{ margin: '4px 0' }} />

      <Title level={5}>新建实验弹窗 —— 每个字段填什么</Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>实验名</Text>：必填，1~80 字，自己认得出就行（placeholder 例：「六维前5 · 默认权重」）。
        </li>
        <li>
          <Text strong>起始日 / 结束日</Text>：都必填，<strong>点开日历选</strong>（不用手输，格式错不了）——
          回测区间的首日与末日；提交给后端时按 YYYYMMDD 落库。
        </li>
        <li>
          <Text strong>观察期</Text>：多选，只有 T+1 / T+3 / T+5 / T+10 / T+20 五个值 ——
          信号日之后持有几个交易日再统计表现；选多个 = 同一次运行里多切几刀，多重比较问题更重。
        </li>
        <li>
          <Text strong>组合规则</Text>：多选 top5 / top10 / top30 —— 每个信号日按重放排名买前几名。
        </li>
        <li>
          <Text strong>前 N 名</Text>：1~100 整数（默认 10）—— 研究层取前多少名进入候选。
        </li>
        <li>
          <Text strong>最低分</Text>：0~100（默认 0 = 不筛）—— 打分低于此值的票不入选。
        </li>
        <li>
          <Text strong>权重 · 勾「用快照自带权重」（默认勾，推荐）</Text>：按快照入库时自己的权重重放，
          等于复现当初那个口径。
        </li>
        <li>
          <Text strong>权重 · 取消勾选才出现的六个自定义框</Text>：主力资金持续性 30 / 量增价稳 20 /
          大单净额占比 15 / 股价位置低 15 / 换手适中 10 / 筹码收敛 10（默认值与
          <Text code>backend/config/scorer.yaml</Text> 一致，各填 0~100）。<strong>六格必须全填</strong> ——
          少一个就等于把那个因子消融了，而你可能并没这个意思（前端按 0 补齐并显式落库，
          不让漏传静默变成一次消融）。
        </li>
        <li>
          <Text strong>交易成本 · 默认不勾</Text>：没有业务方确认的费率就不凭空扣一笔，
          可执行收益整列留空。
        </li>
        <li>
          <Text strong>交易成本 · 勾「按研究假设扣费」</Text>：按 佣金双向 万2.5（最低 5 元）/
          卖出印花税 千0.5 / 滑点 0.1% / 名义本金 10 万 扣，弹窗里会再黄条提醒一次 ——
          这是研究假设，不等于任何人的实际券商费率。
        </li>
        <li>
          <Text strong>「创建（配置到此冻结）」</Text>：提交按钮的字面原文。创建后配置不可改，
          并自动开始第一轮运行；点「取消」不落任何数据。
        </li>
      </ul>

      <Divider style={{ margin: '4px 0' }} />

      <Title level={5}>③ 信号与结果 Tab —— 两个下拉、三张表</Title>
      <ul style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>「选实验」下拉</Text>：切要看的实验；在 ② 点实验名或「查看」也会直接落到本 Tab
          并选中。旁边 <Text strong>「全部观察期」下拉</Text>：按 T+N 筛选，点 × 清除 = 不筛。
        </li>
        <li>
          <Text strong>「汇总指标」表（组合 × 观察期一行）</Text>：理论均值 / 中位数、有效成交、买不进率。
          样本量 &lt; 30 标黄 —— 均值不稳，不要只看均值；「未到期/缺数据」列是没参与统计的行数。
          交易成本列默认 -（没配成本不扣费），最大回撤列恒 -（理由见开头）。
        </li>
        <li>
          <Text strong>「信号」表</Text>：快照排名 vs 重放排名，分值对拍容差 0.06。带{' '}
          <Text type="warning">≠</Text> 的行是<strong>已知且有意的差异</strong>：快照本身没有并列裁决，
          引擎按 code 升序固定裁决。
        </li>
        <li>
          <Text strong>「逐条结果」表</Text>：复权口径三档 —— <Tag color="green">已折算</Tag>（两端
          adj_factor 都有且不同）、<Tag color="blue">无除权·直算</Tag>（两端因子相同，确认这段没除权）、
          <Tag color="orange">判不了</Tag>（缺 adj_factor，若这段发生过除权，该收益不反映真实回报），
          没算出收益的行显示 -，不填某个值冒充。理论 / 超额 / 可执行三列收益缺了一律 -
          （可执行收益还要求两端确认可成交且配了成本模型）。「买入 / 卖出」两列是成交判定，
          拒单自带原因。行数 = 信号数 × 观察期数，超 1000 截断并提示用观察期筛选缩小范围。
        </li>
      </ul>

      <Divider style={{ margin: '4px 0' }} />

      <Title level={5}>④ 因子消融 Tab —— 三步</Title>
      <ol style={{ margin: 0, paddingLeft: 20 }}>
        <li>
          <Text strong>「剔除因子」多选</Text>：选中的因子权重<strong>归零</strong>、其余不变；
          归零不是删键 —— 删掉会从回退路径重新计入。
        </li>
        <li>
          <Text strong>「新建消融实验」按钮</Text>：一个因子都没选时禁用；点开的是同一张新建弹窗，
          顶部黄条列出将剔除的因子。建完<strong>停在本页</strong>、消融侧下拉自动选中 ——
          运行仍要回 ② 点「运行」。
        </li>
        <li>
          <Text strong>「对比」卡的两个下拉（基线实验 / 消融实验）</Text>：选两个才出对比表；
          只有同组合、同观察期的行才可比，对不上的不进表；两侧选同一个实验会提示「对比没有意义」。
        </li>
      </ol>
      <Paragraph style={{ margin: 0 }}>
        结论边界：消融只回答「换这套权重后排名与收益怎么变」，<strong>不宣称该因子的边际贡献</strong> ——
        快照只存每个维度的百分位和权重，不存因子原始值。
      </Paragraph>

      <Alert
        type="warning"
        showIcon
        title="免责声明"
        description="本模块只做行情呈现与历史研究，不给买卖信号，不构成任何投资建议。回测是对过去的描述，不是对未来的承诺；历史收益不预示未来表现。结论边界见 docs/backtest/limitations.md。"
      />
    </Space>
  )
}

export default function Lab() {
  const { caps, error: capsErr } = useCapabilities()
  const [selected, setSelected] = useState<number | null>(null)
  const [tab, setTab] = useState('audit')
  const [helpOpen, setHelpOpen] = useState(false)

  /**
   * 「查看」= 预选该实验并**直接落到结果 Tab**。
   * 只改 selected 不切页的话，点完按钮毫无视觉反馈，用户会以为按钮坏了
   * ——2026-10-18 真机反馈就是「点了没反应」。
   */
  const openResult = (id: number) => {
    setSelected(id)
    setTab('result')
  }

  return (
    <Card
      title="策略实验室"
      extra={
        <Space size="small">
          <Tag color="blue">只做历史研究与模拟</Tag>
          <Tag color="red">不接真实交易 · 不发下单指令</Tag>
          <Button icon={<QuestionCircleOutlined />} onClick={() => setHelpOpen(true)}>
            操作说明
          </Button>
        </Space>
      }
    >
      {capsErr && (
        <Alert
          type="error"
          showIcon
          title={capsErr}
          description="后端未启动或尚未加载研究模块。改过 backend/ 后需要重启服务才生效。"
          style={{ marginBottom: 12 }}
        />
      )}

      <Paragraph type="secondary">
        把<strong>已经发生过的</strong>主力打分快照拿回来，按当时可见的分值重建
        「前 N 名」这个规则，再看这些股票之后 1/3/5/10/20 个交易日的表现。
        <strong>信号不重算</strong> —— 重算要跨窗口比价就得用复权价，而前复权会用未来的
        除权事件改写历史价格，算出来的每个分都被未来信息污染。
      </Paragraph>

      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'audit',
            label: '① 数据审计',
            children: <AuditTab caps={caps} />,
          },
          {
            key: 'exp',
            label: '② 实验与运行',
            children: <ExperimentsTab caps={caps} onOpen={openResult} />,
          },
          {
            key: 'result',
            label: '③ 信号与结果',
            children: <ResultsTab selected={selected} />,
          },
          {
            key: 'abl',
            label: '④ 因子消融',
            children: <AblationTab caps={caps} />,
          },
        ]}
      />

      <Divider style={{ margin: '16px 0 8px' }} />
      <Text type="secondary" style={{ fontSize: 12 }}>
        本模块只做行情呈现与历史研究，<strong>不给买卖信号</strong>，不构成任何投资建议。
        回测是对过去的描述，不是对未来的承诺；历史收益不预示未来表现。
        结论边界见 <Text code>docs/backtest/limitations.md</Text>。
      </Text>

      <HelpDrawer
        title="策略实验室 · 操作说明"
        open={helpOpen}
        onClose={() => setHelpOpen(false)}
      >
        <LabHelpBody />
      </HelpDrawer>
    </Card>
  )
}
