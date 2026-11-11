/**
 * 收银台 —— 订阅者在飞书里点开的那条链接的落点（docs/06 §2）。
 *
 * ## 它在布局之外
 * `App.tsx` 里所有后台页都套在 `<Layout><Sider>` 里，而这个页面是**订阅者本人**
 * 用手机打开的：把侧边栏（尤其那个调 `/api/system/stop` 的「停止服务」按钮，
 * 后端目前无鉴权）暴露给每个订阅者是现成的洞。所以它从主布局里整个摘出去。
 *
 * ## 「去支付」不做真支付
 * 没有支付网关、没有回调、后端只监听本机/内网 —— 点下去生成一张**待确认订单**，
 * 页面提示「联系管理员开通」，管理员在订阅管理页照订单点确认。真收款要等
 * 买服务器 + 接网关，在那之前页面必须把这句话说清楚，否则用户以为付了钱就自动生效。
 *
 * ## 首屏加载走 promise 链
 * 与 `Subs.tsx` 同一个理由：`react(set-state-in-effect)` 把 effect 调用链上
 * **同步**发生的 setState 判成违规，全站基线恰好 6 条，不能加第 7 条。
 */
import { useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Divider,
  Radio,
  Skeleton,
  Space,
  Table,
  Tag,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { api, errMsg, type PayInfo, type PayOrder, type PayPlansResponse } from '../lib/api'
import { short } from '../lib/format'

const { Title, Paragraph, Text } = Typography

const yuan = (n: number) => `¥${n.toLocaleString('zh-CN', { maximumFractionDigits: 2 })}`

/** 订单状态。pending 才能被管理员确认，done 之后重复点会被后端 409 挡下 */
function statusTag(status: string) {
  if (status === 'done') return <Tag color="success">已开通</Tag>
  if (status === 'closed') return <Tag color="default">已作废</Tag>
  return <Tag color="warning">待确认</Tag>
}

const ORDER_COLUMNS: ColumnsType<PayOrder> = [
  {
    title: '订单号',
    dataIndex: 'order_no',
    render: (v: string) => <Text code>{v}</Text>,
  },
  { title: '月份', dataIndex: 'months', width: 80, render: (v: number) => `${v} 个月` },
  { title: '金额', dataIndex: 'amount', width: 100, render: (v: number) => yuan(v) },
  { title: '支付方式', dataIndex: 'channel_label', width: 100 },
  { title: '状态', key: 'status', width: 96, render: (_, r) => statusTag(r.status) },
  { title: '时间', dataIndex: 'created_at', width: 150, render: (v: string) => short(v) },
]

export default function Pay({ token }: { token: string }) {
  const [info, setInfo] = useState<PayInfo | null>(null)
  const [plansOnly, setPlansOnly] = useState<PayPlansResponse | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [months, setMonths] = useState<number | null>(null)
  const [channel, setChannel] = useState<string>('wechat')
  const [ordering, setOrdering] = useState(false)
  const [placed, setPlaced] = useState<PayOrder | null>(null)

  useEffect(() => {
    // token 对不对决定了这一页是收银台还是「链接已失效」，所以先问带 token 的
    // 那个接口；它 404 时**再**拉公开档位表，好歹把失效页渲染得体面些。
    api
      .payInfo(token)
      .then((r) => {
        setInfo(r)
        setMonths(r.plans[0]?.months ?? null)
        if (r.orders.length) setPlaced(r.orders[0])
        setErr(null)
      })
      .catch(() =>
        api
          .payPlans()
          .then((p) => {
            setPlansOnly(p)
            setMonths(p.plans[0]?.months ?? null)
          })
          .catch((e: unknown) => setErr(errMsg(e))),
      )
  }, [token])

  // 「去支付」是事件处理器，里面的同步 setState 不受 effect 那条规则约束
  const submit = () => {
    if (!months) return
    setOrdering(true)
    api
      .payOrder(token, months, channel)
      .then((r) => {
        setPlaced(r.order)
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setOrdering(false))
  }

  const plans = info?.plans ?? plansOnly?.plans ?? []
  const channels = info?.channels ?? plansOnly?.channels ?? []
  const chosen = plans.find((p) => p.months === months)

  // ---- 链接失效 / 拉不到 ------------------------------------------------
  if (err && !info && !plansOnly) {
    return (
      <Card style={{ maxWidth: 560, margin: '32px auto' }}>
        <Title level={4}>链接无效</Title>
        <Paragraph>{err}</Paragraph>
        <Paragraph type="secondary">
          请在飞书里重新发送「订阅Pro」，用最新收到的链接打开本页。
        </Paragraph>
        <Alert
          type="info"
          showIcon
          title="数据免责声明"
          description="本页仅用于订阅关系的管理，所展示的基金估值来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
        />
      </Card>
    )
  }

  // ---- 首屏还在拉 --------------------------------------------------------
  if (!info && !plansOnly) {
    return (
      <Card style={{ maxWidth: 560, margin: '32px auto' }}>
        <Skeleton active paragraph={{ rows: 4 }} />
      </Card>
    )
  }

  const name = info?.nickname || '订阅者'

  return (
    <Space direction="vertical" size={16} style={{ width: '100%', maxWidth: 560, margin: '0 auto' }}>
      <Card>
        <Title level={4} style={{ marginBottom: 8 }}>
          订阅 Pro
        </Title>
        <Paragraph type="secondary" style={{ marginBottom: 16 }}>
          你好，{name}。选择订阅时长与支付方式，开通后可在交易日 14:50
          收到关注基金的实时估值推送。
        </Paragraph>

        {info?.active ? (
          <Alert
            type="success"
            showIcon
            title="你已在订阅期内"
            description={`到期时间 ${short(info.expires_at)}。再下单会从到期日往后顺延，不会覆盖掉剩余时长。`}
            style={{ marginBottom: 16 }}
          />
        ) : null}

        <Descriptions column={1} size="small" style={{ marginBottom: 16 }}>
          <Descriptions.Item label="当前状态">
            {info?.active ? (
              <Tag color="success">订阅中 · {short(info.expires_at)} 到期</Tag>
            ) : (
              <Tag color="default">未订阅</Tag>
            )}
          </Descriptions.Item>
        </Descriptions>

        <Text strong>订阅时长</Text>
        <Radio.Group
          style={{ display: 'block', marginTop: 8 }}
          value={months}
          onChange={(e) => setMonths(e.target.value)}
          options={plans.map((p) => ({
            value: p.months,
            label: `${p.label} · ${yuan(p.amount)}`,
          }))}
        />

        <Divider style={{ margin: '16px 0' }} />

        <Text strong>支付方式</Text>
        <Radio.Group
          style={{ display: 'block', marginTop: 8 }}
          value={channel}
          onChange={(e) => setChannel(e.target.value)}
          options={channels.map((c) => ({ value: c.value, label: c.label }))}
        />

        {chosen ? (
          <Paragraph style={{ marginTop: 16, marginBottom: 8 }}>
            应付金额：<Text strong style={{ fontSize: 20 }}>{yuan(chosen.amount)}</Text>
            <Text type="secondary">（{chosen.label}，共 {chosen.days} 天）</Text>
          </Paragraph>
        ) : null}

        {err ? (
          <Alert
            type="error"
            showIcon
            title="下单失败"
            description={err}
            style={{ marginTop: 12 }}
          />
        ) : null}

        <Button
          type="primary"
          block
          size="large"
          style={{ marginTop: 16 }}
          loading={ordering}
          disabled={!months || !chosen}
          onClick={submit}
        >
          去支付
        </Button>

        <Alert
          type="warning"
          showIcon
          title="演示环境：不会真正扣款"
          description={
            <>
              本收银台为占位演示，尚未接入真实支付通道。点「去支付」只会生成一张订单号，
              请联系管理员核实后为你开通。已经付费？把订单号发给管理员即可，不会重复扣款。
            </>
          }
          style={{ marginTop: 16 }}
        />
      </Card>

      {placed ? (
        <Card title="我的订单">
          <Descriptions column={1} size="small">
            <Descriptions.Item label="订单号">
              <Text code copyable={{ text: placed.order_no }}>
                {placed.order_no}
              </Text>
            </Descriptions.Item>
            <Descriptions.Item label="内容">
              {placed.months} 个月 · {placed.channel_label} · {yuan(placed.amount)}
            </Descriptions.Item>
            <Descriptions.Item label="状态">{statusTag(placed.status)}</Descriptions.Item>
            <Descriptions.Item label="时间">{short(placed.created_at)}</Descriptions.Item>
          </Descriptions>
          {placed.status === 'pending' ? (
            <Alert
              type="info"
              showIcon
              title="待管理员确认"
              description="把上面的订单号发给管理员，核对后即可开通。开通结果会以飞书消息通知你。"
              style={{ marginTop: 12 }}
            />
          ) : null}
        </Card>
      ) : null}

      {info?.orders?.length ? (
        <Card title={`全部订单（${info.orders.length}）`}>
          <Table<PayOrder>
            rowKey="order_no"
            size="small"
            columns={ORDER_COLUMNS}
            dataSource={info.orders}
            pagination={false}
          />
        </Card>
      ) : null}

      <Alert
        type="info"
        showIcon
        title="数据免责声明"
        description="本页仅用于订阅关系的管理。所推送的基金估值来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
      />
    </Space>
  )
}
