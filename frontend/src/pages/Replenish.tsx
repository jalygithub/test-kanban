import { useEffect, useState } from 'react'
import { Alert, Button, Card, Progress, Space, Tag, Typography } from 'antd'
import {
  api,
  errMsg,
  type ReplenishJob,
  type ReplenishJobState,
  type ReplenishStatus,
} from '../lib/api'

const { Text, Paragraph, Title } = Typography

/**
 * 历史补录（2026-11-11）：把「主力建仓候选」页缺的历史数据补回来。
 *
 * 四个按钮 = 四条流水线，**点了才会联网**（几百到上万次请求）—— 启动、
 * 页面刷新、任何定时器都不会自动开跑。全部断点续跑：中断后原样再点一次
 * 就接着补，已落库的不覆盖。状态轮询只打 `/api/replenish/status`（零联网）。
 */
const JOB_META: Record<ReplenishJob, {
  title: string
  tag: string
  desc: string
  cost: string
}> = {
  bars: {
    title: '① 日线全历史回填（含复权因子）',
    tag: '约 1 万次请求 · 5-8 小时',
    desc:
      '把全部 A 股 2026-01-01 起的不复权日线（开高低收 / 成交量 / 成交额）和复权因子补进本地库。'
      + '历史打分、上榜矩阵的「每天的股价」、个股详情页的 K 线都吃这份数据。'
      + '已覆盖的票自动跳过，只发真正缺的请求。',
    cost: '日线 + 因子逐票补；单批失败率过高会自动停（防限流）',
  },
  inflow: {
    title: '② 逐日主力净流入全历史回填',
    tag: '约 5400 次请求 · 3-5 小时',
    desc:
      '从东财逐日资金流接口把每只股票的主力净流入 / 大单净额按日补进本地库 —— '
      + '打分维度 1/3 和「近 5 日净流入」列的历史数据来源。'
      + '只补缺失格：某天已有盘后当场采的快照就不覆盖。',
    cost: '一只票一次取全历史；已补过的票自动跳过',
  },
  pools: {
    title: '③ 涨/炸/跌停/强势池补采（近 30 天）',
    tag: '少量请求 · 1 小时内',
    desc:
      '把最近 30 天缺的涨停 / 炸板 / 跌停 / 强势池按日取回 —— 历史打分的粗筛要靠它排除涨跌停。'
      + '⚠️ 东财只保留最近 30 个自然日，过期日期永久不可补（收盘当场采的，事后取不回），'
      + '页面会如实列出哪些日期补不了。',
    cost: '先点这个再点 ④；已有快照的日期不覆盖',
  },
  score: {
    title: '④ 历史打分补跑',
    tag: '约 12-15 分钟/天',
    desc:
      '逐日跑主力建仓打分，把「上榜矩阵」的历史列补出来。只覆盖池子拿得到的日期'
      + '（近 30 天 + 本地已有池子快照的日期）；更早的日期会被跳过并写明原因 —— '
      + '「历史模式打分」（用日线反推涨跌停）支持后同一个按钮就能全覆盖。'
      + '已打过的日期自动跳过。',
    cost: '建议顺序：① → ② → ③ → ④；④ 依赖 ① 的日线',
  },
}

const STATUS_TAG: Record<string, { text: string; color: string }> = {
  running: { text: '运行中', color: 'processing' },
  done: { text: '已完成', color: 'success' },
  failed: { text: '有失败', color: 'warning' },
  idle: { text: '未开始', color: 'default' },
}

function statusOf(st: ReplenishJobState | undefined): keyof typeof STATUS_TAG {
  if (!st) return 'idle'
  if (st.running) return 'running'
  if (st.error) return 'failed'
  if (st.finished_at) return st.failed > 0 ? 'failed' : 'done'
  return 'idle'
}

export default function Replenish() {
  const [st, setSt] = useState<ReplenishStatus | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState<ReplenishJob | null>(null)

  // 状态轮询：4s 一次，只打零联网端点。setState 落在 .then 回调内（编译器规则）
  useEffect(() => {
    let alive = true
    const tick = () => {
      api
        .replenishStatus()
        .then((d) => {
          if (alive) setSt(d)
        })
        .catch(() => {})
    }
    tick()
    const id = setInterval(tick, 4000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [])

  const start = (job: ReplenishJob) => {
    setBusy(job)
    setErr('')
    api
      .replenishStart(job)
      .then(() => api.replenishStatus())
      .then((d) => setSt(d))
      .catch((e) => setErr(errMsg(e)))
      .finally(() => setBusy(null))
  }

  const stop = (job: ReplenishJob) => {
    setBusy(job)
    api
      .replenishStop(job)
      .then(() => api.replenishStatus())
      .then((d) => setSt(d))
      .catch((e) => setErr(errMsg(e)))
      .finally(() => setBusy(null))
  }

  return (
    <div>
      <Title level={4}>历史补录</Title>
      <Alert
        type="info"
        showIcon
        title="怎么用"
        description={
          <Space direction="vertical" size={4}>
            <Text>
              四个按钮对应四条补数据流水线，<Text strong>点了才会联网</Text>
              （几百到上万次请求），全部<Text strong>断点续跑</Text>
              —— 中断后原样再点一次就接着补，已落库的不覆盖。
            </Text>
            <Text>
              建议顺序 <Text strong>① → ② → ③ → ④</Text>：
              ①② 是打分和事件研究的数据底座（耗时最长，可以先点上挂着）；
              ③ 是 ④ 的前置（粗筛要排除涨跌停）；④ 一天约 12-15 分钟。
            </Text>
            <Text type="danger">
              请在<Text strong>家里或服务器网络</Text>跑 ——
              公司内网下这些请求会被安全设备拦截并记录。跑的时候不用管，
              关掉浏览器也行（任务在后端跑），回来再看进度。
            </Text>
          </Space>
        }
        style={{ marginBottom: 16 }}
      />
      {err ? (
        <Alert
          type="error"
          showIcon
          title="操作失败"
          description={err}
          closable
          onClose={() => setErr('')}
          style={{ marginBottom: 16 }}
        />
      ) : null}
      <Space direction="vertical" size={16} style={{ width: '100%' }}>
        {(['bars', 'inflow', 'pools', 'score'] as ReplenishJob[]).map((job) => {
          const meta = JOB_META[job]
          const s = st?.jobs[job]
          const key = statusOf(s)
          const tag = STATUS_TAG[key]
          const pct = s && s.total > 0 ? Math.round((s.done / s.total) * 100) : 0
          return (
            <Card
              key={job}
              title={
                <Space>
                  <span>{meta.title}</span>
                  <Tag color={tag.color}>{tag.text}</Tag>
                </Space>
              }
              extra={<Tag>{meta.tag}</Tag>}
            >
              <Paragraph style={{ marginBottom: 8 }}>{meta.desc}</Paragraph>
              <Text type="secondary" style={{ fontSize: 12, display: 'block', marginBottom: 12 }}>
                {meta.cost}
              </Text>
              {s && (s.running || s.finished_at) ? (
                <div style={{ marginBottom: 12 }}>
                  <Progress percent={pct} status={s.running ? 'active' : s.error ? 'exception' : 'success'} />
                  <Text type="secondary" style={{ fontSize: 12 }}>
                    {s.msg || '…'}　新落库 {s.rows} · 失败 {s.failed}
                  </Text>
                  {s.error ? (
                    <Alert
                      type="error"
                      showIcon
                      title="出错了"
                      description={`${s.error}（原样再点一次就是续跑）`}
                      style={{ marginTop: 8 }}
                    />
                  ) : null}
                  {s.skipped.length > 0 ? (
                    <details style={{ marginTop: 8 }}>
                      <summary style={{ cursor: 'pointer', fontSize: 12, color: '#999' }}>
                        跳过 / 失败明细（{s.skipped.length} 条）
                      </summary>
                      <ul style={{ margin: '8px 0 0', paddingLeft: 20 }}>
                        {s.skipped.slice(0, 10).map((x) => (
                          <li key={x.key}>
                            <Text style={{ fontSize: 12 }}>
                              {x.key}：{x.reason}
                            </Text>
                          </li>
                        ))}
                        {s.skipped.length > 10 ? (
                          <li>
                            <Text type="secondary" style={{ fontSize: 12 }}>
                              …… 共 {s.skipped.length} 条
                            </Text>
                          </li>
                        ) : null}
                      </ul>
                    </details>
                  ) : null}
                </div>
              ) : null}
              <Space>
                <Button
                  type="primary"
                  loading={busy === job}
                  disabled={s?.running}
                  onClick={() => start(job)}
                >
                  {s?.running ? '运行中…' : s?.finished_at ? '继续补录（续跑）' : '开始补录'}
                </Button>
                {s?.running ? (
                  <Button danger loading={busy === job} onClick={() => stop(job)}>
                    停止
                  </Button>
                ) : null}
              </Space>
            </Card>
          )
        })}
      </Space>
      <Alert
        type="warning"
        showIcon
        title="数据免责声明"
        description="本页只做公开数据的补录与整理，补录结果仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
        style={{ marginTop: 16 }}
      />
    </div>
  )
}
