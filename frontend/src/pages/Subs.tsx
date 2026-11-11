/**
 * 「订阅管理」页 —— 订阅制的后台（docs/06 §7）。**owner 专属**。
 *
 * 2026-10-14 拆成四个页签（原来一屏堆五张卡，找东西得滚）：
 * **① 已订阅**（手工开通/续期 + 立即推送 + 订阅者表）、**② 邀请开通**（名单 +
 * 生成链接 + 飞书私聊推送）、**③ 收银台**（订单）、**④ 定价与链接**（定价 + 链接
 * 前缀）。免责声明留在页签外面，任何页签下都看得见。
 *
 * 一屏看全一个订阅者：什么时候订的、什么时候到期、付了多少、关注了哪几只
 * （还剩几个名额）、14:50 推没推成。行内能做的只有三件：续期、改单独关注
 * 上限、关闭订阅。
 *
 * ## 为什么金额不在行内改
 * 占位支付链接收不到钱，`amount` 只是个记账数字 —— 它由**开通时**那个表单
 * 定（续期时会一起覆盖）。低频、且要跟开通动作对得上，塞进行内编辑反而容易
 * 出现「改了金额但没开通」这种对不上的状态。
 *
 * ## 首屏加载必须写成 promise 链
 * `react(set-state-in-effect)` 会把 effect 调用链上**同步**发生的 setState
 * 判成违规，`async` 函数体会被整体视作同步。全站基线是恰好 6 条，新页面
 * 不能加第 7 条 —— 所以 `load` 的第一个动作是 `api.subsList()` 这个 promise
 * 本身，所有 setState 都落在 `.then` / `.catch` / `.finally` 里。
 * （事件处理器里同步 setState 不受影响，规则只盯 effect。）
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Popconfirm,
  Select,
  Space,
  Spin,
  Table,
  Tabs,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, SendOutlined } from '@ant-design/icons'
import {
  api,
  errMsg,
  type InviteRoster,
  type PayOrder,
  type PayPricing,
  type Sub,
  type SubFund,
  type SubListResponse,
  type SubPush,
} from '../lib/api'
import { short } from '../lib/format'

const { Text, Paragraph } = Typography

function statusTag(r: Sub) {
  if (r.active) return <Tag color="success">订阅中</Tag>
  if (r.plan === 'pro') return <Tag color="error">已过期</Tag>
  return <Tag color="default">未订阅</Tag>
}

/**
 * 展开行：这个人的关注明细 + 推送历史。两件只读的事一次拉完，
 * 照样是 `.then` 里 setState（同一个 lint 约束）。
 */
function SubDetail({ openId }: { openId: string }) {
  const [funds, setFunds] = useState<SubFund[] | null>(null)
  const [pushes, setPushes] = useState<SubPush[] | null>(null)

  useEffect(() => {
    Promise.all([api.subFunds(openId), api.subPushes(openId)])
      .then(([f, p]) => {
        setFunds(f.items)
        setPushes(p.items)
      })
      .catch(() => {
        setFunds([])
        setPushes([])
      })
  }, [openId])

  if (funds === null || pushes === null) return <Spin size="small" />

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <div>
        <Text strong>
          关注基金（{funds.length} 只）
        </Text>
        {funds.length === 0 ? (
          <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
            还没关注。群里/私聊发「关注基金 161725」即可。
          </Paragraph>
        ) : (
          <ul style={{ marginTop: 4, marginBottom: 0, paddingLeft: 18 }}>
            {funds.map((f) => (
              <li key={f.code}>
                <Text code>{f.code}</Text> {f.name || '（名称未回填）'}
                <Text type="secondary"> · {short(f.added_at)} 加入</Text>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <Text strong>消息推送情况（最近 {pushes.length} 条）</Text>
        {pushes.length === 0 ? (
          <Paragraph type="secondary" style={{ marginTop: 4, marginBottom: 0 }}>
            还没有推送记录。交易日到点自动推，或用右上角「立即推送」试一次。
          </Paragraph>
        ) : (
          <ul style={{ marginTop: 4, marginBottom: 0, paddingLeft: 18 }}>
            {pushes.map((p) => (
              <li key={p.id}>
                {p.ok ? (
                  <Tag color="success">成功</Tag>
                ) : (
                  <Tag color="error">失败</Tag>
                )}
                <Text>{short(p.sent_at)}</Text>
                {p.detail ? <Text type="secondary"> · {p.detail}</Text> : null}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Space>
  )
}

/**
 * 收银台订单（docs/06 §2）。订阅者在飞书里点开收银台、选完月份与支付方式后，
 * 这里就是他那张待确认订单的落点 —— 管理员照它点「确认开通」。
 *
 * **只有 pending 能点**：`done` 再点一次，后端会把订阅期间累加两遍（真实存在
 * 的操作事故），所以前端不给按钮，后端也用 409 挡着，两头都拦。
 */
function PayOrdersCard() {
  const [orders, setOrders] = useState<PayOrder[] | null>(null)
  const [busy, setBusy] = useState('')
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')

  const load = useCallback(() => {
    api
      .payOrders()
      .then((r) => setOrders(r.items))
      .catch((e: unknown) => setErr(errMsg(e)))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const confirm = (orderNo: string) => {
    setBusy(orderNo)
    api
      .payConfirm(orderNo)
      .then((r) => {
        setMsg(
          `订单 ${r.order.order_no} 已开通：${r.order.months} 个月 · ¥${r.order.amount}。`,
        )
        setErr('')
        load()
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setBusy(''))
  }

  const columns: ColumnsType<PayOrder> = [
    {
      title: '订单号',
      dataIndex: 'order_no',
      width: 200,
      render: (v: string) => <Text code copyable={{ text: v }}>{v}</Text>,
    },
    {
      title: '订阅者',
      key: 'who',
      render: (_, r) => (
        <Space size={4} direction="vertical">
          <Text code>{r.open_id}</Text>
          {r.nickname ? <Text type="secondary">{r.nickname}</Text> : null}
        </Space>
      ),
    },
    { title: '月份', dataIndex: 'months', width: 80, render: (v: number) => `${v} 个月` },
    {
      title: '金额',
      dataIndex: 'amount',
      width: 100,
      render: (v: number, r) => (
        <Text type={r.status === 'done' ? 'secondary' : undefined}>
          ¥{v}
          <Text type="secondary">（单价 ¥{r.price}）</Text>
        </Text>
      ),
    },
    { title: '支付方式', dataIndex: 'channel_label', width: 100 },
    {
      title: '状态',
      key: 'status',
      width: 96,
      render: (_, r) =>
        r.status === 'done' ? (
          <Tag color="success">已开通</Tag>
        ) : r.status === 'closed' ? (
          <Tag color="default">已作废</Tag>
        ) : (
          <Tag color="warning">待确认</Tag>
        ),
    },
    { title: '时间', dataIndex: 'created_at', width: 150, render: (v: string) => short(v) },
    {
      title: '操作',
      key: 'ops',
      width: 120,
      render: (_, r) =>
        r.status === 'pending' ? (
          <Button
            size="small"
            type="primary"
            loading={busy === r.order_no}
            onClick={() => confirm(r.order_no)}
          >
            确认开通
          </Button>
        ) : (
          <Text type="secondary">{r.confirmed_at ? short(r.confirmed_at) : '—'}</Text>
        ),
    },
  ]

  return (
    <Card
      title="收银台订单"
      extra={
        <Button size="small" icon={<ReloadOutlined />} onClick={load}>
          刷新
        </Button>
      }
    >
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        订阅者发「订阅Pro」拿到收银台链接，在页面上选月份与支付方式后生成这里的一张订单。
        <Text strong>占位演示收不到钱</Text>
        —— 金额只是记账，核对收款后点「确认开通」，按订单月份顺延到期。
      </Paragraph>
      {err ? (
        <Alert type="error" showIcon title="操作失败" description={err} style={{ marginBottom: 12 }} />
      ) : null}
      {msg ? (
        <Alert type="success" showIcon title="已开通" description={msg} style={{ marginBottom: 12 }} />
      ) : null}
      <Table<PayOrder>
        rowKey="order_no"
        size="small"
        loading={orders === null}
        columns={columns}
        dataSource={orders ?? []}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        locale={{
          emptyText: <Empty description="还没有收银台订单。" />,
        }}
      />
    </Card>
  )
}


/** 定价表单。**全用字符串** —— 输入框要能为空（空 = 那档按 单价 × 月数），
 * 存成 number 就没法表达「刚清空还没输入」这个状态。 */
type PricingForm = {
  price: string
  months: string[]
  amounts: Record<string, string>
}

/** 服务端定价 → 表单。**放模块顶层是为了让 effect 的依赖数组干净** ——
 * 写在组件里就是 effect 的自由变量，`exhaustive-deps` 会多亮一条。 */
const toForm = (p: PayPricing): PricingForm => ({
  price: String(p.price),
  months: p.months.map(String),
  amounts: Object.fromEntries(
    Object.entries(p.amounts).map(([k, v]) => [k, String(v)]),
  ),
})

/**
 * 收银台定价卡片（docs/06 §2）。**自己拉、自己存** —— 不从父组件接 `pricing`
 * 再同步，那样得在 effect 里 setState（`react(set-state-in-effect)` 全站基线
 * 恰好 6 条，加不了第 7 条）；用 `key` 强制重挂又会在父组件刷新时丢输入焦点。
 *
 * 三条口径：
 *  1. 改完存后端 `kv` 表，**即时生效、不用重启**；
 *  2. 已下的订单金额是下单那一刻快照的，改价不影响历史；
 *  3. 占位演示**不接支付网关、收不到钱** —— 这句必须留在页面上。
 */
function PricingCard() {
  const [form, setForm] = useState<PricingForm | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    api
      .subPricing()
      .then((r) => {
        setForm(toForm(r.pricing))
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setLoaded(true))
  }, [])

  if (!loaded) {
    return <Card title="收银台定价" loading />
  }
  if (!form) {
    return (
      <Card title="收银台定价">
        <Alert
          type="error"
          showIcon
          title="读不到当前定价"
          description={err ?? '后端没响应。'}
        />
      </Card>
    )
  }

  const setField = (v: string) => setForm((p) => (p ? { ...p, price: v } : p))
  const setMonths = (v: string[]) => setForm((p) => (p ? { ...p, months: v } : p))
  const setAmount = (m: string, v: string) =>
    setForm((p) => (p ? { ...p, amounts: { ...p.amounts, [m]: v } } : p))

  const save = () => {
    const p = Number(form.price.trim())
    if (!Number.isFinite(p) || p <= 0) {
      setMsg('')
      setErr(`每月单价得是正数，收到「${form.price}」`)
      return
    }
    // 去重后的档位，既当请求参数也当渲染 key —— 标签模式下重复输入要挡住
    const ms: number[] = []
    for (const raw of form.months) {
      const n = Number(String(raw).trim())
      if (!Number.isInteger(n) || n <= 0) {
        setMsg('')
        setErr(`档位得是正整数月数，收到「${raw}」`)
        return
      }
      if (!ms.includes(n)) ms.push(n)
    }
    if (ms.length === 0) {
      setMsg('')
      setErr('至少要留一个档位。')
      return
    }
    const out: Record<string, number> = {}
    for (const m of ms) {
      const raw = (form.amounts[String(m)] ?? '').trim()
      if (!raw) continue // 留空 = 单价 × 月数，键不传就是这个意思
      const n = Number(raw)
      if (!Number.isFinite(n) || n <= 0) {
        setMsg('')
        setErr(`${m} 个月的金额得是正数，收到「${raw}」`)
        return
      }
      out[String(m)] = n
    }
    setSaving(true)
    api
      .subPricingSave({ price: p, months: ms, amounts: out })
      .then((r) => {
        setForm(toForm(r.pricing))
        setMsg('已保存。收银台页面、飞书档位表与新订单即时生效；已下的订单不受影响。')
        setErr(null)
      })
      .catch((e: unknown) => {
        setMsg('')
        setErr(errMsg(e))
      })
      .finally(() => setSaving(false))
  }

  // 渲染去重：存的是编辑中的原始数组，重复 tag 得先剔掉，否则 React key 撞车
  const shown = form.months.filter((m, i) => form.months.indexOf(m) === i)
  const unit = Number(form.price)
  const linear = (m: string) => {
    const n = Number(m)
    if (!Number.isFinite(unit) || unit <= 0 || !Number.isInteger(n) || n <= 0) {
      return '留空 = 单价 × 月数'
    }
    return `留空 = ${Math.round(unit * n * 100) / 100}`
  }

  return (
    <Card
      title="收银台定价"
      extra={
        <Text type="secondary">
          后端 <Text code>pay.amount_of</Text> 是全站唯一算钱处
        </Text>
      }
    >
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        收银台页面、飞书「订阅Pro」档位表与新订单金额三处同源。
        <Text strong>改完即时生效、不用重启</Text>
        ；已下的订单按下单那一刻快照，不受影响。
        这是占位演示，<Text strong>不接支付网关、收不到钱</Text>。
      </Paragraph>
      <Space wrap size={16} align="center">
        <Space size={8}>
          <Text>每月单价</Text>
          <Input
            value={form.price}
            onChange={(e) => setField(e.target.value)}
            style={{ width: 140 }}
            addonAfter="元"
          />
        </Space>
        <Space size={8}>
          <Text>档位（月数）</Text>
          <Select
            mode="tags"
            value={form.months}
            onChange={(v: string[]) => setMonths(v)}
            style={{ minWidth: 300 }}
            tokenSeparators={[',', ' ', '，']}
            placeholder="回车新增，点标签 × 删除"
          />
        </Space>
      </Space>

      <div style={{ marginTop: 16 }}>
        <Text strong>各档金额</Text>
        <Paragraph type="secondary" style={{ margin: '4px 0 8px' }}>
          留空 = 每月单价 × 月数（线性）；填了就用填的，可以做「3 个月特惠价」。
          删掉某个档位时，它的单独定价会一并丢掉。
        </Paragraph>
        <Space wrap size={12}>
          {shown.map((m) => (
            <Space key={m} size={6}>
              <Text>{m} 个月</Text>
              <Input
                value={form.amounts[m] ?? ''}
                onChange={(e) => setAmount(m, e.target.value)}
                style={{ width: 160 }}
                addonAfter="元"
                placeholder={linear(m)}
              />
            </Space>
          ))}
        </Space>
      </div>

      <Space style={{ marginTop: 16 }}>
        <Button type="primary" loading={saving} onClick={save}>
          保存定价
        </Button>
        <Text type="secondary">
          保存即生效，收银台与飞书档位表下一次读取就是新价。
        </Text>
      </Space>
      {err ? (
        <Alert
          type="error"
          showIcon
          title="保存失败"
          description={err}
          style={{ marginTop: 12 }}
        />
      ) : null}
      {msg ? (
        <Alert
          type="success"
          showIcon
          title="已完成"
          description={msg}
          style={{ marginTop: 12 }}
        />
      ) : null}
    </Card>
  )
}


/**
 * 链接前缀（收银台 + 邀请链接共用，`pay.base_url()`）。**存 kv，改完即时生效**
 * —— 不用改 `.env` 也不用重启后端（与定价同口径）。
 *
 * 默认 `http://localhost:5173` 那是**电脑自己**，手机打不开（手机的 localhost 是
 * 手机自己），所以电脑连手机热点时要在这里换成电脑在热点网段的 IP。热点的 IP 每次
 * 开都可能变（DHCP），做成页面可改正是为了这个。
 */
function BaseUrlCard() {
  const [url, setUrl] = useState('')
  const [def, setDef] = useState('')
  const [loaded, setLoaded] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api
      .payBaseGet()
      .then((r) => {
        setUrl(r.url)
        setDef(r.default)
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setLoaded(true))
  }, [])

  const save = () => {
    setSaving(true)
    setSaved(false)
    api
      .payBaseSet(url.trim())
      .then((r) => {
        setUrl(r.url)
        setSaved(true)
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setSaving(false))
  }

  return (
    <Card title="链接前缀" loading={!loaded}>
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        收银台与邀请链接都以它开头。默认 <Text code>http://localhost:5173</Text>
        —— 那是电脑自己，<Text strong>手机打不开</Text>
        （手机上的 localhost 指手机自己）。电脑连手机热点时，把它换成电脑在热点网段
        的 IP：<Text code>ipconfig</Text> 看「IPv4 地址」，一般形如{' '}
        <Text code>http://192.168.137.x:5173</Text>。
      </Paragraph>
      {err ? (
        <Alert type="error" showIcon title="保存失败" description={err} style={{ marginBottom: 12 }} />
      ) : null}
      <Space style={{ marginBottom: 8 }}>
        <Input
          value={url}
          onChange={(e) => {
            setUrl(e.target.value)
            setSaved(false)
          }}
          style={{ width: 440 }}
          placeholder="http://192.168.137.x:5173"
        />
        <Button type="primary" loading={saving} onClick={save}>
          保存
        </Button>
        {url.trim() !== def ? (
          <Button size="small" onClick={() => setUrl(def)}>
            恢复默认
          </Button>
        ) : null}
      </Space>
      <Paragraph type="secondary" style={{ marginBottom: 0 }}>
        {saved ? <Tag color="success">已保存 —— 之后新生成的链接会带上它</Tag> : null}只影响之后新生成的链接，已发出去的是快照、不动。
      </Paragraph>
    </Card>
  )
}


/**
 * 邀请开通卡片（docs/06 §11）。**名单就是 `subscriber` 表** —— 发过消息的人由
 * 长连接落进来（`feishu._handle_event` → `sub_upsert`，`plan=''` 是还没订阅的），
 * 不另开名单表，也就不存在「名单里有但他其实已经订了」这种要对账的状态。
 *
 * **一人一条链接**：点一个裸的 `<a href>` 只发 URL，浏览器不会附带任何飞书身份
 * —— 所以身份在生成链接那一刻就烧进 token 了。群发一条通用的做不到，那要走
 * 飞书网页应用 OAuth（重定向 URL 要求 HTTPS + 域名，`docs/06 §9` 的 P1 待办）。
 */
function InviteCard() {
  const [items, setItems] = useState<InviteRoster[]>([])
  const [proDays, setProDays] = useState(30)
  const [loaded, setLoaded] = useState(false)
  const [making, setMaking] = useState('')
  /** 手工加名单的输入框。**不调任何飞书接口** —— 长连接在公司内网连不上时，
   * 「收到消息自动落一行」那条入口收不到东西，这里是唯一能把人加进名单的路 */
  const [newOpenId, setNewOpenId] = useState('')
  const [newNick, setNewNick] = useState('')
  const [joining, setJoining] = useState(false)
  /** 本次会话里生成过的链接 `{open_id: url}`。**不落库回显** —— 下次刷新就要
   * 重新生成，免得页面上堆一堆已经没人记得发给谁的旧链接。 */
  const [links, setLinks] = useState<Record<string, string>>({})
  /** 正在推送的 open_id（防连点）；'' = 没有在推 */
  const [pushing, setPushing] = useState('')
  const [pushMsg, setPushMsg] = useState('')
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    api
      .inviteList()
      .then((r) => {
        setItems(r.items)
        setProDays(r.pro_days)
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setLoaded(true))
  }, [])

  const make = (openId: string) => {
    setMaking(openId)
    api
      .inviteMake(openId)
      .then((r) => {
        setLinks((prev) => ({ ...prev, [openId]: r.link.url }))
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setMaking(''))
  }

  const join = () => {
    const oid = newOpenId.trim()
    if (!oid) {
      setErr('先填一个 open_id')
      return
    }
    setJoining(true)
    api
      .inviteJoin(oid, newNick.trim())
      .then((r) => {
        // 已经在名单里就替换掉（`sub_upsert` 是 ON CONFLICT 更新），别重出一行
        setItems((prev) => [r.item, ...prev.filter((x) => x.open_id !== r.item.open_id)])
        setNewOpenId('')
        setNewNick('')
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setJoining(false))
  }

  /**
   * 生成链接 + 飞书私聊发给他，一次做完（`POST /api/invite/push`）。
   *
   * **发的是私聊不是群 webhook**：链接一人一条、token 里烧的是这个人的 open_id，
   * 进群就等于群里谁点开都算到他自己头上。发送失败（`sent=false`）不作废 ——
   * 链接照样存进 `links`，页面上还留着，复制手发即可。
   */
  const push = (openId: string, nick: string) => {
    setPushing(openId)
    setPushMsg('')
    api
      .invitePush(openId, nick)
      .then((r) => {
        setLinks((prev) => ({ ...prev, [openId]: r.link.url }))
        setPushMsg(
          r.sent
            ? `已通过飞书私聊发给 ${nick || openId}。`
            : `飞书没发出去（凭据或对方不可达），但链接已生成 —— 复制下面的链接手动发。`,
        )
        setErr(null)
      })
      .catch((e: unknown) => {
        setPushMsg('')
        setErr(errMsg(e))
      })
      .finally(() => setPushing(''))
  }

  const columns: ColumnsType<InviteRoster> = [
    {
      title: '昵称',
      dataIndex: 'nickname',
      render: (v: string) => v || <Text type="secondary">未填写</Text>,
    },
    {
      title: 'open_id',
      dataIndex: 'open_id',
      render: (v: string) => (
        <Text code style={{ fontSize: 12 }}>
          {v}
        </Text>
      ),
    },
    {
      title: '状态',
      key: 'status',
      width: 200,
      render: (_, r) =>
        r.active ? (
          <Space size={6}>
            <Tag color="success">已订阅</Tag>
            <Text type="secondary">至 {short(r.expires_at)}</Text>
          </Space>
        ) : (
          <Tag>未订阅</Tag>
        ),
    },
    {
      title: '邀请链接',
      key: 'link',
      render: (_, r) => {
        const url = links[r.open_id]
        if (!url) {
          return (
            <Button size="small" loading={making === r.open_id} onClick={() => make(r.open_id)}>
              生成链接
            </Button>
          )
        }
        return (
          <div style={{ maxWidth: 320 }}>
            <Text copyable={{ text: url }} style={{ fontSize: 12, wordBreak: 'break-all' }}>
              {url}
            </Text>
          </div>
        )
      },
    },
    {
      // 一键「生成 + 飞书私聊发给他」。与左边「生成链接」分工：那个只造链接供复制，
      // 这个连发送一起做（发不出去时链接照样会出现在左边那格）
      title: '推送',
      key: 'push',
      width: 130,
      render: (_, r) => (
        <Button
          size="small"
          icon={<SendOutlined />}
          loading={pushing === r.open_id}
          onClick={() => push(r.open_id, r.nickname)}
        >
          发给他
        </Button>
      ),
    },
  ]

  return (
    <Card title="邀请开通" loading={!loaded}>
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        点邀请链接 → 欢迎页 → 点「订阅」，直接开通 <Text strong>{proDays} 天</Text>
        ，<Tag color="success">免费</Tag>（金额落 0，在「已订阅」页签一眼能认出来）。
      </Paragraph>
      <Paragraph type="secondary">
        链接 <Text strong>一人一条</Text>
        ，只能发给名单里的人 —— 点开时后端从 token 反查身份，群发一条通用的查不出
        是谁（那要走飞书 OAuth + HTTPS 域名）。名单 =
        <Text strong>给机器人发过消息的人</Text>
        （<Text code>feishu._handle_event</Text> 落一行），但
        <Text strong>飞书长连接在内网连不上时它会一直是空的</Text>
        ，这时在下面手工填 open_id 补进来。<Text strong>加人不调飞书接口</Text>
        ；只有点「发给他」时才真的发一条私聊（走
        <Text code>send_text</Text> 的 <Text code>open_id</Text> 模式，与长连接无关）。
      </Paragraph>
      <Space style={{ marginBottom: 12 }} wrap>
        <Input
          placeholder="open_id（ou_ 开头）"
          value={newOpenId}
          onChange={(e) => setNewOpenId(e.target.value)}
          style={{ width: 320 }}
        />
        <Input
          placeholder="昵称（可空）"
          value={newNick}
          onChange={(e) => setNewNick(e.target.value)}
          style={{ width: 160 }}
        />
        <Button loading={joining} onClick={join}>
          加入名单
        </Button>
      </Space>
      {err ? (
        <Alert type="error" showIcon title="操作失败" description={err} style={{ marginBottom: 12 }} />
      ) : null}
      {pushMsg ? (
        <Alert
          type="success"
          showIcon
          title="推送结果"
          description={pushMsg}
          style={{ marginBottom: 12 }}
        />
      ) : null}
      <Table<InviteRoster>
        rowKey="open_id"
        size="small"
        columns={columns}
        dataSource={items}
        pagination={{ pageSize: 10, hideOnSinglePage: true }}
        locale={{
          emptyText: (
            <Empty description="名单是空的 —— 让对方先给机器人发条消息，或在上面手工填 open_id。" />
          ),
        }}
      />
    </Card>
  )
}


export default function Subs() {
  const [items, setItems] = useState<Sub[]>([])
  const [cfg, setCfg] = useState<SubListResponse['settings'] | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  /** 正在操作的 open_id（行内按钮防连点）；'' = 没有在操作 */
  const [busy, setBusy] = useState('')
  const [pushing, setPushing] = useState(false)
  const [pushMsg, setPushMsg] = useState('')
  /**
   * **表格行内操作的回执，渲染在「订阅者」卡里**（不是上面那张卡）。
   *
   * 2026-10-14 用户点「关闭」后反馈「没反应、不显示成功或失败、没改状态」——
   * 日志里那次 `POST /api/subs/{id}` 其实返回了 200，真正缺的是**回执**：
   * 成功时一句没有，失败时 `err` 挂在上面那张卡（人在下面的表格根本看不见）。
   * 行内操作必须就地给结果。
   */
  const [rowMsg, setRowMsg] = useState<{ kind: 'success' | 'error'; text: string } | null>(
    null,
  )
  /**
   * 当前页签。**四支的子内容都在第一次渲染就构造好**（antd Tabs 不销毁未选中的
   * 面板），所以各支的 effect 与拆分前一样只跑一次；改成懒挂载会变成切页签反复
   * 拉接口，也更容易碰到 `react(set-state-in-effect)` 那 6 条基线。
   */
  const [tab, setTab] = useState('subs')

  const [newId, setNewId] = useState('')
  const [newAmount, setNewAmount] = useState('')
  const [newNick, setNewNick] = useState('')

  const load = useCallback(() => {
    api
      .subsList()
      .then((r) => {
        setItems(r.items)
        setCfg(r.settings)
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  /** 刷新：`setLoading(true)` 在事件处理器里同步做是允许的（规则只盯 effect）。 */
  const reload = () => {
    setLoading(true)
    load()
  }

  const replace = (sub: Sub) =>
    setItems((prev) => prev.map((x) => (x.open_id === sub.open_id ? sub : x)))

  /** 这次行内操作到底干了什么，给人看的一句话。 */
  const rowLabel = (p: Parameters<typeof api.subUpdate>[1]): string => {
    if (p.close) return '已关闭订阅（关注不清空，重新开通后还在）。'
    if (p.fund_limit !== undefined)
      return p.fund_limit
        ? `单独关注上限已改为 ${p.fund_limit}。`
        : '单独关注上限已恢复全局默认。'
    if (p.amount !== undefined)
      return p.amount ? `金额已改为 ${p.amount}。` : '金额已恢复默认价。'
    if (p.nickname !== undefined) return `备注名已改为「${p.nickname || '（空）'}」。`
    return '已保存。'
  }

  const patch = (openId: string, p: Parameters<typeof api.subUpdate>[1]) => {
    setBusy(openId)
    api
      .subUpdate(openId, p)
      .then((r) => {
        replace(r.item)
        setRowMsg({ kind: 'success', text: rowLabel(p) })
        // 兜底重拉：**显示状态以服务端为准**。本地 `replace` 万一没命中
        // （行不在列表里、或返回体与列表口径不一致），表现就是「点了没改状态」。
        load()
      })
      .catch((e: unknown) => setRowMsg({ kind: 'error', text: errMsg(e) }))
      .finally(() => setBusy(''))
  }

  const activate = (openId: string, amount: number | null, nick: string) => {
    setBusy(openId)
    api
      .subActivate(openId, amount, nick)
      .then((r) => {
        setItems((prev) => [r.item, ...prev.filter((x) => x.open_id !== r.item.open_id)])
        setRowMsg({
          kind: 'success',
          text: `已开通 / 续期，到期时间 ${short(r.item.expires_at)}。`,
        })
        load()
      })
      .catch((e: unknown) => setRowMsg({ kind: 'error', text: errMsg(e) }))
      .finally(() => setBusy(''))
  }

  const submitNew = () => {
    const id = newId.trim()
    if (!id) return
    const raw = newAmount.trim()
    let amount: number | null = null
    if (raw) {
      const n = Number(raw)
      if (!Number.isFinite(n) || n <= 0) {
        setErr(`金额得是正数，收到「${raw}」`)
        return
      }
      amount = n
    }
    activate(id, amount, newNick.trim())
    setNewId('')
    setNewAmount('')
    setNewNick('')
    setErr(null)
  }

  const pushNow = () => {
    setPushing(true)
    api
      .subsPushNow()
      .then((r) => {
        setPushMsg(
          `已推 ${r.push.sent}/${r.push.targets} 人，失败 ${r.push.failed}。`,
        )
        setErr(null)
        reload()
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setPushing(false))
  }

  const columns: ColumnsType<Sub> = [
    {
      title: 'open_id',
      dataIndex: 'open_id',
      width: 220,
      render: (v: string, r) => (
        <Space size={4} direction="vertical">
          <Text code copyable={{ text: v }}>
            {v}
          </Text>
          {r.nickname ? <Text type="secondary">{r.nickname}</Text> : null}
        </Space>
      ),
    },
    {
      title: '状态',
      key: 'state',
      width: 96,
      render: (_, r) => statusTag(r),
    },
    {
      title: '订阅时间',
      dataIndex: 'paid_at',
      width: 150,
      render: (v: string | null) => short(v),
    },
    {
      title: '到期',
      dataIndex: 'expires_at',
      width: 150,
      render: (v: string | null) => short(v),
    },
    {
      title: '金额',
      dataIndex: 'amount',
      width: 90,
      // 0 = 邀请免费开通。显示成「免费」而不是「¥0」，一眼能分出哪些是白送的
      render: (v: number | null) =>
        v == null ? '—' : v === 0 ? <Tag color="success">免费</Tag> : `¥${v}`,
    },
    {
      title: '关注基金',
      key: 'funds',
      width: 130,
      render: (_, r) => (
        <Text type={r.fund_count >= r.limit ? 'warning' : undefined}>
          {r.fund_count} / {r.limit}
        </Text>
      ),
    },
    {
      title: '单独上限',
      dataIndex: 'fund_limit',
      width: 120,
      // 即改即存。0 = 恢复用全局默认（后端把它写成 NULL）
      render: (v: number | null, r) => (
        <Select
          size="small"
          value={v ?? 0}
          style={{ width: 104 }}
          disabled={busy === r.open_id}
          onChange={(next) => patch(r.open_id, { fund_limit: next })}
          options={[
            { value: 0, label: `默认 ${cfg?.fund_watch_limit ?? 10}` },
            ...[5, 10, 20, 30].map((n) => ({ value: n, label: String(n) })),
          ]}
        />
      ),
    },
    {
      title: '推送',
      key: 'push',
      width: 170,
      render: (_, r) =>
        r.push.total === 0 ? (
          <Text type="secondary">还没推过</Text>
        ) : (
          <Space size={4} direction="vertical">
            <Text type={r.push.fail > 0 ? 'danger' : undefined}>
              成功 {r.push.ok} / 失败 {r.push.fail}
            </Text>
            <Text type="secondary">最近 {short(r.push.last_at)}</Text>
          </Space>
        ),
    },
    {
      title: '操作',
      key: 'ops',
      width: 170,
      // **不在订阅期就不给「关闭」按钮** —— 那是个点了什么都不改的红色按钮
      // （2026-10-14 用户直接问到「为什么关闭的订阅还显示关闭按钮」）。
      // 关着的人只剩「开通」一件事可做，文案也跟着说人话。
      render: (_, r) => (
        <Space size={6}>
          <Button
            size="small"
            type={r.active ? undefined : 'primary'}
            loading={busy === r.open_id}
            onClick={() => activate(r.open_id, null, '')}
          >
            {r.active ? '续期' : '开通'} {cfg?.pro_days ?? 30} 天
          </Button>
          {r.active ? (
            <Popconfirm
              title="关闭这个订阅？"
              description="关注不清空，重新开通后还在。"
              okText="关闭"
              cancelText="取消"
              onConfirm={() => patch(r.open_id, { close: true })}
            >
              <Button size="small" danger loading={busy === r.open_id}>
                关闭
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      {/* 四支**都在第一次渲染就构造好**：antd Tabs 不销毁未选中的面板，各支的
          effect 与拆分前一样只跑一次；改成懒挂载会变成切页签反复拉接口，也更
          容易碰到 `react(set-state-in-effect)` 那 6 条基线。 */}
      <Tabs
        activeKey={tab}
        onChange={setTab}
        items={[
          {
            key: 'subs',
            label: '已订阅',
            children: (
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <Card
                  title="订阅管理"
                  extra={
                    <Space>
                      <Button icon={<ReloadOutlined />} onClick={reload} loading={loading}>
                        刷新
                      </Button>
                      <Button
                        type="primary"
                        icon={<SendOutlined />}
                        loading={pushing}
                        onClick={pushNow}
                      >
                        立即推送
                      </Button>
                    </Space>
                  }
                >
                  <Paragraph type="secondary" style={{ marginTop: 0 }}>
                    订阅者通过飞书发「订阅Pro」拿到
                    <Text strong>收银台链接</Text>
                    （选月份与支付方式，演示环境收不到钱），到「收银台」页签核对后点确认开通，
                    或直接在这里手工开通 / 续期。到期自动降级；交易日{' '}
                    {String(cfg?.push_hour ?? 14).padStart(2, '0')}:
                    {String(cfg?.push_minute ?? 50).padStart(2, '0')} 推送关注基金的实时估值。
                  </Paragraph>

                  <Space wrap>
                    <Input
                      placeholder="飞书 open_id（ou_ 开头）"
                      value={newId}
                      onChange={(e) => setNewId(e.target.value)}
                      style={{ width: 260 }}
                      allowClear
                    />
                    <Input
                      placeholder={`金额（留空 = ${cfg?.pro_price ?? 99}）`}
                      value={newAmount}
                      onChange={(e) => setNewAmount(e.target.value)}
                      style={{ width: 170 }}
                      allowClear
                    />
                    <Input
                      placeholder="备注名（群昵称等）"
                      value={newNick}
                      onChange={(e) => setNewNick(e.target.value)}
                      style={{ width: 170 }}
                      allowClear
                    />
                    <Button type="primary" onClick={submitNew} loading={busy !== ''}>
                      开通 / 续期
                    </Button>
                  </Space>

                  {err ? (
                    <Alert
                      type="error"
                      showIcon
                      title="操作失败"
                      description={err}
                      style={{ marginTop: 12 }}
                    />
                  ) : null}
                  {pushMsg ? (
                    <Alert
                      type="success"
                      showIcon
                      title="推送结果"
                      description={pushMsg}
                      style={{ marginTop: 12 }}
                    />
                  ) : null}
                </Card>

                <Card title="订阅者">
                  {rowMsg ? (
                    <Alert
                      type={rowMsg.kind}
                      showIcon
                      title={rowMsg.kind === 'success' ? '操作成功' : '操作失败'}
                      description={rowMsg.text}
                      style={{ marginBottom: 12 }}
                      closable
                      onClose={() => setRowMsg(null)}
                    />
                  ) : null}
                  <Table<Sub>
                    rowKey="open_id"
                    size="small"
                    loading={loading}
                    columns={columns}
                    dataSource={items}
                    pagination={{ pageSize: 20, hideOnSinglePage: true }}
                    expandable={{ expandedRowRender: (r) => <SubDetail openId={r.open_id} /> }}
                    locale={{
                      emptyText: (
                        <Empty
                          description={
                            <span>
                              还没有订阅者。发「订阅Pro」拿到链接后，在这里开通。
                            </span>
                          }
                        />
                      ),
                    }}
                  />
                </Card>
              </Space>
            ),
          },
          { key: 'invite', label: '邀请开通', children: <InviteCard /> },
          { key: 'cashier', label: '收银台', children: <PayOrdersCard /> },
          {
            key: 'pricing',
            label: '定价与链接',
            children: (
              <Space direction="vertical" size={16} style={{ width: '100%' }}>
                <PricingCard />
                <BaseUrlCard />
              </Space>
            ),
          },
        ]}
      />

      <Alert
        type="info"
        showIcon
        title="数据免责声明"
        description="本页订阅信息与推送记录仅用于管理自有订阅关系。所推送的基金估值来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
      />
    </Space>
  )
}
