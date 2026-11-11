import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Input,
  Popconfirm,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  api,
  type FeishuConfig,
  type FeishuPush,
  type JobRun,
  type JobStatus,
  type NotifyConfig,
} from '../lib/api'
import {
  fmtDate,
  parseDetail,
  rel,
  secondsBetween,
  STATUS_COLOR,
  STATUS_LABEL,
} from '../lib/format'

const { Text } = Typography

const KIND_LABEL: Record<string, string> = {
  scheduled: '定时触发',
  manual: '手动执行',
  feishu: '飞书指令',
}

/** ISO 时间转本地显示。job_run 存的是 UTC，直接 slice 会差 8 小时。 */
const localTime = (iso?: string | null) => {
  if (!iso) return '-'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  return new Date(t).toLocaleString('zh-CN', { hour12: false })
}

/** 入站长连接的状态标签：没开 / 凭据错 / 连着 / 还没连，一眼分清。 */
const feishuTag = (c: FeishuConfig | null) => {
  if (!c?.enabled) return { color: 'default', text: '未开启' }
  if (c.state.last_error) return { color: 'red', text: '连接失败' }
  if (c.state.connected) return { color: 'green', text: '已连接' }
  if (c.state.running) return { color: 'orange', text: '连接中' }
  return { color: 'default', text: c.has_app ? '未连接' : '未配置' }
}

/**
 * 定时任务页：开关调度 + 手动触发 + 运行历史。
 *
 * 开关是**持久化**的 —— 关掉之后重启后端也不会自己开回来。
 */
export default function Jobs() {
  const [status, setStatus] = useState<JobStatus | null>(null)
  const [history, setHistory] = useState<JobRun[]>([])
  const [error, setError] = useState<string | null>(null)
  // 初次加载不置 loading：effect 里同步 setState 会被 React 的纯度检查拦下，
  // 改成「没有数据也没有错误」时显示 Spin
  const [loading, setLoading] = useState(false)
  /** 正在切哪个开关（'' = 都没在切），两个开关要分开转圈 */
  const [toggling, setToggling] = useState('')
  const [kicking, setKicking] = useState(false)
  const [now, setNow] = useState(0)
  // ---- 飞书推送（跑完通知）--------------------------------------
  const [notify, setNotify] = useState<NotifyConfig | null>(null)
  const [webhook, setWebhook] = useState('')
  const [secret, setSecret] = useState('')
  // 勾选的分段。null = 还没从后端拿到，别拿空数组去覆盖库里的勾选
  const [sections, setSections] = useState<string[] | null>(null)
  const [saving, setSaving] = useState(false)
  const [testing, setTesting] = useState(false)
  // 「推送摘要」的进行态。飞书指令那块也有个 pushing/push，是另一个功能
  // （主动给手机推看板状态，不经 webhook），别复用。
  const [digesting, setDigesting] = useState(false)
  const [preview, setPreview] = useState<string | null>(null)
  // ---- 飞书指令（入站长连接）----------------------------------------
  const [feishu, setFeishu] = useState<FeishuConfig | null>(null)
  const [fAppId, setFAppId] = useState('')
  const [fSecret, setFSecret] = useState('')
  const [fOwner, setFOwner] = useState('')
  const [fSaving, setFSaving] = useState(false)
  const [simText, setSimText] = useState('')
  const [simReply, setSimReply] = useState<string | null>(null)
  const [simming, setSimming] = useState(false)
  const [pushing, setPushing] = useState(false)
  const [pushInfo, setPushInfo] = useState<FeishuPush | null>(null)

  // 用 promise 链而不是 async/await：load 体内没有任何同步 setState，
  // 效果里 `void load()` 才不会被 set-state-in-effect 判为「在 effect 里同步改状态」
  const load = useCallback(
    () =>
      Promise.all([api.jobStatus(), api.jobHistory()])
        .then(([s, h]) => {
          setStatus(s)
          setHistory(h.items)
          setNow(Date.now())
          setError(null)
        })
        .catch((e: Error) => setError(e.message)),
    [],
  )

  useEffect(() => {
    void load()
  }, [load])

  // 推送与入站配置**只在挂载时拉一次**：任务进行中的轮询会反复调 load，
  // 混进去会把用户正在输入的地址/凭据冲掉
  const loadNotify = useCallback(
    () =>
      Promise.all([api.notifyConfig(), api.feishuConfig()])
        .then(([n, f]) => {
          setNotify(n.notify)
          setWebhook(n.notify.webhook)
          setSecret(n.notify.secret)
          setSections(n.notify.sections)
          setFeishu(f.feishu)
          setFAppId(f.feishu.app_id)
          setFSecret(f.feishu.app_secret)
          setFOwner(f.feishu.owner)
        })
        .catch((e: Error) => setError(e.message)),
    [],
  )

  useEffect(() => {
    void loadNotify()
  }, [loadNotify])

  const running = status?.current.running ?? false

  // 任务在跑就轮询；跑完最后一次拉到终态后自动停
  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => void load(), 3000)
    return () => window.clearInterval(timer)
  }, [running, load])

  const refresh = async () => {
    setLoading(true)
    try {
      await load()
    } finally {
      setLoading(false)
    }
  }

  /**
   * 开/关调度。**两个开关各自独立**：`enabled` = 盘后任务，
   * `push_enabled` = 14:50 估值推送 —— 传哪个后端就只改哪个。
   */
  const toggle = async (patch: { enabled?: boolean; push_enabled?: boolean }) => {
    setToggling(patch.push_enabled === undefined ? 'jobs' : 'push')
    try {
      const r = await api.setSchedule(patch)
      setStatus((prev) => (prev ? { ...prev, schedule: r.schedule } : prev))
      setNow(Date.now())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setToggling('')
    }
  }

  const kick = async (force: boolean) => {
    setKicking(true)
    try {
      await api.runJob(undefined, force)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setKicking(false)
    }
  }

  /** 保存推送配置。`next` 来自开关，省得开一下关又得再读一遍输入框。
   *
   * `secs` 同理：勾选框一变就 `save(undefined, next)` 直接落库，不留「勾了忘
   * 点保存」的坑。`sections` 状态此时可能还没更新（setState 是异步的），所以
   * 勾选值一律**从入参传进来**，不读 state。 */
  const save = async (next?: boolean, secs?: string[]) => {
    setSaving(true)
    try {
      const chosen = secs ?? sections
      const r = await api.saveNotify({
        enabled: next ?? !!notify?.enabled,
        webhook: webhook.trim(),
        secret: secret.trim(),
        ...(chosen === null || chosen === undefined ? {} : { sections: chosen }),
      })
      setNotify(r.notify)
      setWebhook(r.notify.webhook)
      setSecret(r.notify.secret)
      setSections(r.notify.sections)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  /** 按库内数据立刻发一条到手机；正文回填到预览，结果回填到「上次发送」。 */
  const test = async () => {
    setTesting(true)
    try {
      const r = await api.testNotify()
      setPreview(r.text)
      setNotify((prev) =>
        prev
          ? { ...prev, last: { at: r.at, ok: r.ok, detail: r.detail } }
          : prev,
      )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setTesting(false)
    }
  }

  /** 手动把最近一轮任务的摘要推出去 —— 手动「立即执行」跑完不会自动发
   * （见 scheduler），这是补发的唯一入口。后端可能回 {skipped}：那不是失败，
   * 是「为什么没发」的说明，照实显示，别当成发送成功。 */
  const pushDigest = async () => {
    setDigesting(true)
    try {
      const r = await api.pushDigest()
      if (r.skipped) {
        setError(r.skipped)
        return
      }
      setNotify((prev) =>
        prev
          ? { ...prev, last: { at: r.at ?? '', ok: !!r.ok, detail: r.detail ?? '' } }
          : prev,
      )
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setDigesting(false)
    }
  }

  /** 保存入站凭据与白名单。`next` 来自开关，省得开一下关又得重读输入框。 */
  const saveFeishu = async (next?: boolean) => {
    setFSaving(true)
    try {
      const r = await api.saveFeishu({
        app_id: fAppId.trim(),
        app_secret: fSecret.trim(),
        owner: fOwner.trim(),
        enabled: next ?? !!feishu?.enabled,
      })
      setFeishu(r.feishu)
      setFAppId(r.feishu.app_id)
      setFSecret(r.feishu.app_secret)
      setFOwner(r.feishu.owner)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setFSaving(false)
    }
  }

  /** 「试一下」：把指令走一遍分发逻辑并展示回复，跑任务/改关注都不执行。 */
  const simFeishu = async () => {
    const text = simText.trim()
    if (!text) return
    setSimming(true)
    try {
      const r = await api.simFeishu(text)
      setSimReply(r.reply)
      setFeishu((prev) => (prev ? { ...prev, state: r.state } : prev))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSimming(false)
    }
  }

  /** 主动给手机推一条状态（应用消息，不经 webhook），随时能验证出站链路。 */
  const push = async () => {
    setPushing(true)
    try {
      const r = await api.pushFeishu()
      setPushInfo(r.push)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setPushing(false)
    }
  }

  const schedule = status?.schedule
  const current = status?.current
  const last = status?.last
  const lastDetail = parseDetail(last?.detail)

  const cols: ColumnsType<JobRun> = [
    { title: '#', dataIndex: 'id', width: 64, align: 'center' },
    {
      title: '触发方式',
      dataIndex: 'kind',
      width: 100,
      render: (v: string) => KIND_LABEL[v] ?? v,
    },
    {
      title: '状态',
      dataIndex: 'status',
      width: 96,
      render: (v: string) => (
        <Tag color={STATUS_COLOR[v] ?? 'default'}>{STATUS_LABEL[v] ?? v}</Tag>
      ),
    },
    {
      title: '数据日期',
      dataIndex: 'detail',
      width: 110,
      render: (raw: string) => fmtDate(parseDetail(raw)?.date),
    },
    {
      title: '开始时间',
      dataIndex: 'started_at',
      width: 180,
      render: (v: string) => <Text type="secondary">{localTime(v)}</Text>,
    },
    {
      title: '耗时',
      key: 'dur',
      width: 88,
      align: 'right',
      render: (_, r) => {
        const s = secondsBetween(r.started_at, r.finished_at)
        if (s !== null) {
          return <Text>{s >= 60 ? `${Math.floor(s / 60)}m${s % 60}s` : `${s}s`}</Text>
        }
        // 没有 finished_at：真在跑才叫进行中，否则是迁移前的老记录（status='manual'）
        return (
          <Text type="secondary">{r.status === 'running' ? '进行中' : '-'}</Text>
        )
      },
    },
    {
      title: '失败步骤',
      key: 'failed',
      render: (_, r) => {
        const failed = parseDetail(r.detail)?.failed ?? []
        return failed.length ? (
          <Text type="danger" style={{ fontSize: 12 }}>
            {failed.join('、')}
          </Text>
        ) : (
          <Text type="secondary">-</Text>
        )
      },
    },
  ]

  return (
    <Card
      title="定时任务"
      extra={
        <Button size="small" onClick={() => void refresh()} loading={loading}>
          刷新
        </Button>
      }
    >
      {error && (
        <Alert type="error" showIcon title={error} style={{ marginBottom: 12 }} />
      )}

      {/* ---- 两个开关：盘后任务 / 估值推送，各自独立（2026-10-12）---- */}
      <Card
        size="small"
        title={
          <Space wrap>
            <span>定时开关</span>
            <Tag color={schedule?.enabled ? 'blue' : 'default'}>
              盘后任务 {schedule?.enabled ? '已开启' : '已暂停'}
            </Tag>
            <Tag color={schedule?.push_enabled ? 'blue' : 'default'}>
              估值推送 {schedule?.push_enabled ? '已开启' : '已暂停'}
            </Tag>
          </Space>
        }
        extra={
          <Space wrap>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {schedule?.enabled
                ? `盘后 ${String(schedule.hour).padStart(2, '0')}:${String(
                    schedule.minute,
                  ).padStart(2, '0')} · 下次 ${rel(schedule.next_run, now)}`
                : '盘后不会自动抓数打分'}
            </Text>
            <Switch
              checked={!!schedule?.enabled}
              loading={toggling === 'jobs'}
              checkedChildren="开"
              unCheckedChildren="关"
              onChange={(v) => void toggle({ enabled: v })}
            />
            <Text type="secondary" style={{ fontSize: 12 }}>
              {schedule?.push_enabled
                ? `推送 ${String(schedule.push_hour ?? 14).padStart(2, '0')}:${String(
                    schedule.push_minute ?? 50,
                  ).padStart(2, '0')} · 下次 ${rel(schedule.next_push, now)}`
                : '不会自动推送估值'}
            </Text>
            <Switch
              checked={!!schedule?.push_enabled}
              loading={toggling === 'push'}
              checkedChildren="开"
              unCheckedChildren="关"
              onChange={(v) => void toggle({ push_enabled: v })}
            />
          </Space>
        }
      >
        <Space orientation="vertical" style={{ width: '100%' }} size={10}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            盘后任务：每个交易日收盘后自动抓取涨跌停池、板块资金流并重跑主力建仓打分（约
            12-15 分钟），非交易日自动跳过。关闭后需手动触发。
          </Text>
          <Text type="secondary" style={{ fontSize: 13 }}>
            估值推送：交易日把每个订阅者关注基金的实时估值私发给他，每次发送都留痕
            （成没成与失败原因在「订阅管理」页可见）。盘中拿不到当日净值，推的是实时估值。
            没有订阅者、或他们都没关注基金时什么都不会发。两个开关互不影响 ——
            关掉盘后任务不妨碍推送。开关选择会保存，重启后端仍然生效。
          </Text>

          <Space wrap>
            <Popconfirm
              title="立即执行完整盘后任务？"
              description="抓涨跌停池、板块资金流并重跑主力建仓打分，首次约 12-15 分钟；当天已打过分会直接跳过打分，约 10 秒。期间可继续浏览其它页面。"
              okText="执行"
              cancelText="取消"
              onConfirm={() => void kick(false)}
            >
              <Button type="primary" loading={kicking || running}>
                {running ? '任务进行中…' : '立即执行'}
              </Button>
            </Popconfirm>
            <Popconfirm
              title="强制重跑并重新打分？"
              description="当天已打过分也会重算（force），打分必须重跑，约 12-15 分钟。非必要不用。"
              okText="强制重跑"
              cancelText="取消"
              onConfirm={() => void kick(true)}
            >
              <Button danger disabled={running || kicking}>
                强制重跑
              </Button>
            </Popconfirm>
          </Space>

          {running && (
            <Alert
              type="info"
              showIcon
              title={
                <Space>
                  <span>任务进行中</span>
                  <Text type="secondary">
                    {current?.kind} · {fmtDate(current?.date) || '计算中'}
                  </Text>
                </Space>
              }
              description={
                <Text type="secondary" style={{ fontSize: 12 }}>
                  已完成 {Object.keys(current?.steps ?? {}).length} 步 · 开始于{' '}
                  {localTime(current?.started_at)}
                </Text>
              }
            />
          )}

          {!running && last && (
            <Text type="secondary" style={{ fontSize: 12 }}>
              上一次：{KIND_LABEL[last.kind] ?? last.kind} ·{' '}
              {STATUS_LABEL[last.status] ?? last.status} ·{' '}
              {fmtDate(lastDetail?.date) || localTime(last.started_at)}
            </Text>
          )}
        </Space>
      </Card>

      {/* ---- 跑完通知（飞书） -------------------------------------- */}
      <Card
        size="small"
        style={{ marginTop: 12 }}
        title={
          <Space>
            <span>跑完通知（飞书）</span>
            <Tag color={notify?.enabled ? 'blue' : 'default'}>
              {notify?.enabled ? '已开启' : '未开启'}
            </Tag>
          </Space>
        }
        extra={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {notify?.has_webhook ? '配置已保存' : '未配置 webhook'}
            </Text>
            <Switch
              checked={!!notify?.enabled}
              loading={saving}
              checkedChildren="开"
              unCheckedChildren="关"
              disabled={!webhook.trim()}
              onChange={(v) => void save(v)}
            />
          </Space>
        }
      >
        <Space orientation="vertical" style={{ width: '100%' }} size={10}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            每个交易日定时任务跑完后自动推一条到手机。正文按段拆开，下面勾中的才推；
            标题、任务状态（触发方式/结果/用时）与免责声明是消息框架，始终保留。
            手动「立即执行」与盘中轻量抓取不发送 —— 这个开关管的是自动推送，
            手动跑完想收一条就点「推送摘要」，正文与自动推送一致（测试发送那条只证明通路是通的）。
            地址只存在本机数据库，不写进代码，改完不用重启。
          </Text>

          <div>
            <Text type="secondary" style={{ fontSize: 13, display: 'block', marginBottom: 6 }}>
              推送内容（勾中的才发，改动即时保存）：
            </Text>
            <Checkbox.Group
              value={sections ?? notify?.sections ?? []}
              onChange={(v) => void save(undefined, v as string[])}
              options={(notify?.section_defs ?? []).map((d) => ({
                label: d.label,
                value: d.key,
              }))}
            />
            {(sections ?? notify?.sections ?? []).length === 0 && (
              <Alert
                style={{ marginTop: 8 }}
                type="warning"
                showIcon
                message="一段都没勾：只发标题与任务状态，正文是空的"
              />
            )}
          </div>

          <Space wrap>
            <Input
              style={{ width: 440, maxWidth: '100%' }}
              placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
              value={webhook}
              onChange={(e) => setWebhook(e.target.value)}
              allowClear
            />
            <Input.Password
              style={{ width: 240 }}
              placeholder="签名校验密钥（未开安全设置可留空）"
              value={secret}
              onChange={(e) => setSecret(e.target.value)}
              allowClear
            />
            <Button type="primary" loading={saving} onClick={() => void save()}>
              保存
            </Button>
            <Button
              loading={testing}
              disabled={!webhook.trim()}
              onClick={() => void test()}
            >
              测试发送
            </Button>
            {/* 判据用「已保存的配置」而不是输入框：推送走的是库里存的地址，
                临时清空输入框不该把手动补发的入口也一起锁死。 */}
            <Button
              loading={digesting}
              disabled={!notify?.has_webhook}
              onClick={() => void pushDigest()}
            >
              推送摘要
            </Button>
          </Space>

          {notify?.last?.at && (
            <Text
              type={notify.last.ok ? 'success' : 'danger'}
              style={{ fontSize: 12 }}
            >
              上次发送：{localTime(notify.last.at)} ·{' '}
              {notify.last.ok ? '成功' : '失败'} · {notify.last.detail}
            </Text>
          )}

          {preview && (
            <div className="notify-preview">{preview}</div>
          )}
        </Space>
      </Card>

      {/* ---- 飞书指令（入站长连接，手机遥控） ------------------------ */}
      <Card
        size="small"
        style={{ marginTop: 12 }}
        title={
          <Space>
            <span>飞书指令（手机遥控）</span>
            <Tag color={feishuTag(feishu).color}>{feishuTag(feishu).text}</Tag>
          </Space>
        }
        extra={
          <Space>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {feishu?.has_app ? '凭据已保存' : '未配置应用'}
            </Text>
            <Switch
              checked={!!feishu?.enabled}
              loading={fSaving}
              checkedChildren="开"
              unCheckedChildren="关"
              disabled={!fAppId.trim() || !fSecret.trim()}
              onChange={(v) => void saveFeishu(v)}
            />
          </Space>
        }
      >
        <Space orientation="vertical" style={{ width: '100%' }} size={10}>
          <Text type="secondary" style={{ fontSize: 13 }}>
            在飞书里直接发文字遥控看板：跑 / 轻跑 / 关注 600825 / 取关 600825 /
            查询 600825 / 状态 / 摘要 / 候选 / 板块 / 关注列表。走企业自建应用的
            「事件订阅 → 长连接」，本机主动连出去，<b>不需要公网 IP、不用内网穿透</b>
            （以后把项目扔到服务器上同样能收）。只有白名单里的 open_id 发的才执行，
            群里别人喊「跑」一律拒绝；「跑」跑完会把摘要推回这个会话。
          </Text>

          <Space wrap>
            <Input
              style={{ width: 200 }}
              placeholder="App ID（cli_ 开头）"
              value={fAppId}
              onChange={(e) => setFAppId(e.target.value)}
              allowClear
            />
            <Input.Password
              style={{ width: 220 }}
              placeholder="App Secret"
              value={fSecret}
              onChange={(e) => setFSecret(e.target.value)}
              allowClear
            />
            <Input
              style={{ width: 340, maxWidth: '100%' }}
              placeholder="只响应这个 open_id（留空 = 收到也不执行）"
              value={fOwner}
              onChange={(e) => setFOwner(e.target.value)}
              allowClear
            />
            <Button type="primary" loading={fSaving} onClick={() => void saveFeishu()}>
              保存
            </Button>
          </Space>

          <Text type="secondary" style={{ fontSize: 12 }}>
            配置步骤：open.feishu.cn → 创建「企业自建应用」→ 添加机器人能力 →
            事件订阅选<b>使用长连接接收事件</b> → 订阅 im.message.receive_v1 →
            发布版本。填上 App ID / Secret 保存即可；open_id
            还没绑定时，在飞书里随便发句话，回复里会带上你的 open_id，抄进来保存。
          </Text>

          <Text style={{ fontSize: 12 }}>
            上次收到消息：{feishu?.state.last_msg_at || '—'} · 已处理{' '}
            {feishu?.state.handled ?? 0} 条
            {feishu?.state.started_at
              ? ` · 连接起于 ${feishu.state.started_at.replace('T', ' ')}`
              : ''}
          </Text>
          {feishu?.state.last_error && (
            <Text type="danger" style={{ fontSize: 12 }}>
              连接错误：{feishu.state.last_error}
            </Text>
          )}

          <Space wrap>
            <Input
              style={{ width: 320, maxWidth: '100%' }}
              placeholder="试一下：发条指令看会怎么回（不会真跑任务、不改关注）"
              value={simText}
              onChange={(e) => setSimText(e.target.value)}
              onPressEnter={() => void simFeishu()}
              allowClear
            />
            <Button
              loading={simming}
              disabled={!simText.trim()}
              onClick={() => void simFeishu()}
            >
              试一下
            </Button>
            <Button
              loading={pushing}
              disabled={!feishu?.has_app}
              onClick={() => void push()}
            >
              发我一条
            </Button>
          </Space>

          {pushInfo && (
            <Text
              type={pushInfo.ok ? 'success' : 'danger'}
              style={{ fontSize: 12 }}
            >
              主动推送：{pushInfo.detail}
              {pushInfo.target ? ` · 目标 ${pushInfo.target}` : ''}
            </Text>
          )}

          {simReply && <div className="notify-preview">{simReply}</div>}
        </Space>
      </Card>

      {/* ---- 历史 ---------------------------------------------------- */}
      <Card size="small" style={{ marginTop: 12 }} title="运行历史">
        <Table<JobRun>
          rowKey="id"
          size="small"
          pagination={{ pageSize: 10, showSizeChanger: false }}
          columns={cols}
          dataSource={history}
          locale={{ emptyText: '尚无任务记录' }}
        />
      </Card>

      <Alert
        type="warning"
        showIcon
        style={{ marginTop: 12 }}
        title="数据免责声明"
        description="任务抓取与统计的涨跌停池、板块资金流、主力建仓打分均来自公开接口，仅用于数据可视化研究，不提供投资建议，不构成任何买卖依据。"
      />
    </Card>
  )
}
