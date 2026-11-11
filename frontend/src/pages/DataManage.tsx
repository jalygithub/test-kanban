import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Input,
  Popconfirm,
  Progress,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { JobProgress } from '../components/JobProgress'
import {
  api,
  type BackfillPlan,
  type BarsReport,
  type BarsScope,
  type DataDataset,
  type DataFile,
  type DataPurge,
  type DataUsage,
  type IngestItem,
  type IngestList,
  type JobStatus,
  type PipelineCoverage,
  type PipelineDataset,
  type PipelineGaps,
  type PipelineHealth,
  type PurgeScope,
} from '../lib/api'
import {
  fmtDate,
  fmtRanges,
  parseDetail,
  secondsBetween,
  short,
  STATUS_COLOR,
  STATUS_LABEL,
} from '../lib/format'

const { Text } = Typography

/** dataset 的中文名。总览页有一份类似的，但缺 stock_net_inflow，这里补齐自用。 */
const DATASET_LABEL: Record<string, string> = {
  stock_net_inflow: '逐日主力净流入',
  main_force: '主力建仓打分',
  pool_zt: '涨停池',
  pool_zb: '炸板池',
  pool_dt: '跌停池',
  pool_strong: '强势股',
  sector_flow_industry: '行业资金流',
  sector_flow_concept: '概念资金流',
  daily_bar: '日线采集',
}

const label = (k: string) => DATASET_LABEL[k] ?? k

const fmtBytes = (n?: number | null) => {
  if (n === null || n === undefined) return '-'
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(2)} MB`
  if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${n} B`
}

/** 日志的 mtime 是 Unix 秒（stat().st_mtime）；异常值回退 '-' */
const fmtMtime = (s?: number) => {
  if (!s) return '-'
  const t = new Date(s * 1000)
  return Number.isNaN(t.getTime()) ? '-' : t.toLocaleString('zh-CN', { hour12: false })
}

/**
 * 采集五态的中文名。它们在页面上长得像、后果完全不同：「没跑任务」是没人管，
 * 「源挂了」要修网络，「不完整」要怀疑数据本身。混成一个「失败」，
 * 人就不知道该干什么（口径见 `app/pipeline/coverage.verdict()`）。
 */
const STATE_TAG: Record<string, { color: string; text: string }> = {
  OK: { color: 'green', text: '完整' },
  PARTIAL: { color: 'orange', text: '不完整' },
  NO_DATA: { color: 'red', text: '采了但空' },
  SOURCE_DOWN: { color: 'red', text: '源故障' },
  NO_JOB: { color: 'default', text: '无人采' },
}

/** `ingest_job.status` 的中文名。`PARTIAL` 是「跑成了但数据不能信」，不是成功。 */
const INGEST_TAG: Record<string, { color: string; text: string }> = {
  SUCCESS: { color: 'green', text: '成功' },
  PARTIAL: { color: 'orange', text: '数据不可信' },
  FAILED: { color: 'red', text: '失败' },
  WAITING: { color: 'blue', text: '排队' },
  RUNNING: { color: 'processing', text: '跑着' },
}

/**
 * 每点一次「开始回填」最多发多少次请求。长跑（几千只 × 一年）交给
 * `scripts/backfill_run.py` —— 那边有分批熔断与断点续跑，HTTP 请求里跑
 * 几小时只会被超时掐断，还占着写锁。
 */
const BUDGET = 20

/** `Promise.allSettled` 的拒绝原因 -> 人能看的一句话 */
const errText = (reason: unknown) => {
  if (reason instanceof Error) return reason.message
  if (typeof reason === 'object' && reason && 'message' in reason) {
    return String((reason as { message: unknown }).message)
  }
  return String(reason)
}

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

/** 预览/执行的统一结果条：按 scope 把后端返回的关键数字讲成人话。 */
function PurgeResult({ r }: { r: DataPurge }) {
  if (r.error) return <Alert type="error" showIcon title={r.error} />

  const lines: string[] = []
  if (r.scope === 'snapshot') {
    lines.push(
      `${r.dataset ? label(r.dataset) : ''}：匹配 ${r.matched ?? 0} 天，` +
        `可删 ${r.deletable ?? 0} 天（${fmtBytes(r.deletable_bytes)}）`,
    )
    if (r.blocked?.length) {
      lines.push(
        `触到硬下限、被保住 ${r.blocked.length} 天：${r.blocked.slice(0, 6).join('、')}` +
          `${r.blocked.length > 6 ? '…' : ''}（至少保留最近 ${r.floor_days} 天）`,
      )
    }
    if (r.applied && r.deleted !== undefined) lines.push(`已删除 ${r.deleted} 行`)
  } else if (r.scope === 'retention') {
    lines.push(
      `合计可删 ${r.total_deletable ?? 0} 天（${fmtBytes(r.total_bytes)}）`,
    )
    for (const p of r.plan ?? []) {
      if (!p.dates_total) continue
      lines.push(
        `${label(p.dataset)}：库内 ${p.dates_total} 天 → 保留最近 ${p.keep_days} 天` +
          `${p.floored ? `（硬下限 ${p.floor_days} 天抬高了配置的 ${p.retention_days} 天）` : ''}，` +
          (p.deletable
            ? `可删 ${p.deletable} 天（${fmtBytes(p.bytes)}，${p.del_oldest} ~ ${p.del_newest}）`
            : '无可删'),
      )
    }
    if (r.applied && r.deleted) lines.push(`已删除 ${r.deleted} 行`)
  } else if (r.scope === 'job_run') {
    lines.push(
      `共 ${r.total_rows ?? 0} 行 → 未收尾 ${r.unfinished ?? 0} 行、` +
        `超出保留 ${r.over_limit ?? 0} 行，本次${r.applied ? '删除' : '可删'} ` +
        `${r.deleted ?? r.deletable ?? 0} 行`,
    )
  } else if (r.scope === 'vacuum') {
    lines.push(
      r.applied
        ? `已回收 ${fmtBytes(r.freed)}（${fmtBytes(r.before)} → ${fmtBytes(r.after)}）`
        : 'VACUUM 没有可预览的内容，确认后直接执行',
    )
  } else if (r.scope === 'log') {
    lines.push(
      r.applied
        ? `已清空 ${r.name}（原 ${fmtBytes(r.before)}）`
        : `将清空 ${r.target?.name ?? '-'}（当前 ${fmtBytes(r.target?.bytes)}）`,
    )
  }
  if (r.note) lines.push(r.note)
  if (r.hint) lines.push(r.hint)
  if (!lines.length) lines.push('没有可执行的内容')

  const title = r.dry_run
    ? '预览（未写库）'
    : r.applied
      ? '已执行'
      : '未写入（没有可删的内容）'

  return (
    <Alert
      type={r.dry_run ? 'info' : r.applied ? 'success' : 'warning'}
      showIcon
      title={title}
      description={
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {lines.map((s) => (
            <li key={s}>{s}</li>
          ))}
        </ul>
      }
    />
  )
}

export default function DataManage() {
  const [usage, setUsage] = useState<DataUsage | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** 真写库的那次调用；预览单独一个，两个按钮的 loading 才不会互相串 */
  const [applying, setApplying] = useState(false)
  const [previewing, setPreviewing] = useState(false)
  const [purgeErr, setPurgeErr] = useState<string | null>(null)
  const [result, setResult] = useState<DataPurge | null>(null)

  // ---- 清理表单 --------------------------------------------------------
  const [ds, setDs] = useState('pool_dt')
  const [mode, setMode] = useState<'date' | 'before'>('date')
  const [day, setDay] = useState('')

  /**
   * 首次加载。用 promise 链而不是 async/await：load 体内没有同步 setState，
   * effect 里 `void load()` 才不会被 set-state-in-effect 判为在 effect 里改状态。
   */
  const load = useCallback(
    () =>
      api
        .dataUsage()
        .then((u) => {
          setUsage(u)
          setError(null)
        })
        .catch((e: Error) => setError(e.message)),
    [],
  )

  useEffect(() => {
    void load()
  }, [load])

  const datasets = usage?.datasets ?? []
  const logs = usage?.logs ?? []
  const dayOk = /^\d{8}$/.test(day.trim())
  // 不用 useMemo：datasets 是 usage?.datasets ?? []，依赖每轮渲染都换引用，
  // 算 8 个元素的 find 没必要 memo，挂上去反而触发 exhaustive-deps 告警
  const current = datasets.find((d) => d.dataset === ds)
  const logTotal = logs.reduce((s, f) => s + (f.bytes ?? 0), 0)
  const reclaim = usage?.db.reclaimable_bytes ?? 0

  /**
   * 跑一次清理。`dry=true` 只算不删；真删额外带 confirm='DELETE' ——
   * 后端两道门（dry_run=false + confirm），少一道就 400。
   */
  const purge = (scope: PurgeScope, dry: boolean, extra: Record<string, unknown> = {}) => {
    setPurgeErr(null)
    if (dry) {
      setResult(null)
      setPreviewing(true)
    } else {
      setApplying(true)
    }
    api
      .dataPurge({ scope, dry_run: dry, ...(dry ? {} : { confirm: 'DELETE' }), ...extra })
      .then((r) => {
        setResult(r)
        // 真删之后重量级画像，让表格与统计立刻反映结果
        return dry ? undefined : load()
      })
      .catch((e: Error) => setPurgeErr(e.message))
      .finally(() => {
        setPreviewing(false)
        setApplying(false)
      })
  }

  const purgeSnapshot = (dry: boolean) =>
    purge('snapshot', dry, {
      dataset: ds,
      ...(mode === 'date' ? { date: day.trim() } : { before: day.trim() }),
    })

  /** 清掉表单状态：换了对象或日期就得重新预览，否则确认按钮会拿着旧结果删。 */
  const reset = () => {
    setResult(null)
    setPurgeErr(null)
  }

  // ---- 数据管线（阶段 6）：覆盖 / 采集状态 / 回填进度 / 健康 ---------------
  const [cov, setCov] = useState<PipelineCoverage | null>(null)
  const [gaps, setGaps] = useState<PipelineGaps | null>(null)
  const [ingest, setIngest] = useState<IngestList | null>(null)
  const [bars, setBars] = useState<BarsReport | null>(null)
  const [health, setHealth] = useState<PipelineHealth | null>(null)
  const [jstat, setJstat] = useState<JobStatus | null>(null)
  /** 六个只读端点各自失败的原因。后端没重启时新端点会 404 —— 得看得出是哪个 */
  const [pipeErr, setPipeErr] = useState<string | null>(null)

  const [retrying, setRetrying] = useState(false)
  const [retryMsg, setRetryMsg] = useState<string | null>(null)
  /** 补跑范围：空 = 全部到点的。任务书阶段 6 要「指定数据域」的入口就是它 */
  const [retryDomain, setRetryDomain] = useState('')
  const [jobBusy, setJobBusy] = useState(false)
  const [jobMsg, setJobMsg] = useState<string | null>(null)
  const [jobDate, setJobDate] = useState('')

  const [planDomain, setPlanDomain] = useState('daily_bar')
  const [planScope, setPlanScope] = useState<BarsScope>('all')
  const [planStart, setPlanStart] = useState('')
  const [planEnd, setPlanEnd] = useState('')
  const [plan, setPlan] = useState<BackfillPlan | null>(null)
  const [planBusy, setPlanBusy] = useState(false)
  const [planErr, setPlanErr] = useState<string | null>(null)
  const [runBusy, setRunBusy] = useState(false)
  const [runMsg, setRunMsg] = useState<string | null>(null)

  /**
   * 六个只读接口并列取，**各自成败互不牵连**：后端还没重启时
   * `/api/pipeline/health` 会 404，但覆盖审计与采集状态照样能看。
   * 用 allSettled 而不是 all —— 一个 404 把五份好数据一起扔掉，
   * 是典型的「因为一个失败所以什么都看不到」。
   *
   * ⚠️ 这六条**全是 GET、零联网**（任务书阶段 6 的硬约束）：会外发请求的
   * 三个动作（补跑重试 / 开始回填 / 跑这一轮）只在各自的 onClick 里，
   * 由 `scripts\\tests\\_ui_offline_check.py` 钉死，不许挪进这条加载路径。
   */
  const loadPipe = useCallback(
    () =>
      Promise.allSettled([
        api.pipelineCoverage(),
        api.pipelineGaps(30),
        api.pipelineIngest(50),
        api.pipelineBars('all', 5),
        api.pipelineHealth(),
        api.jobStatus(),
      ]).then((rs) => {
        const errs: string[] = []
        if (rs[0].status === 'fulfilled') setCov(rs[0].value)
        else errs.push(`覆盖审计：${errText(rs[0].reason)}`)
        if (rs[1].status === 'fulfilled') setGaps(rs[1].value)
        else errs.push(`漏采速查：${errText(rs[1].reason)}`)
        if (rs[2].status === 'fulfilled') setIngest(rs[2].value)
        else errs.push(`采集状态：${errText(rs[2].reason)}`)
        if (rs[3].status === 'fulfilled') setBars(rs[3].value)
        else errs.push(`行情进度：${errText(rs[3].reason)}`)
        if (rs[4].status === 'fulfilled') setHealth(rs[4].value)
        else errs.push(`健康体检：${errText(rs[4].reason)}`)
        if (rs[5].status === 'fulfilled') setJstat(rs[5].value)
        else errs.push(`任务进度：${errText(rs[5].reason)}`)
        setPipeErr(errs.length ? errs.join('；') : null)
      }),
    [],
  )

  useEffect(() => {
    void loadPipe()
  }, [loadPipe])

  /** 两个加载器一起刷 —— 顶上那个「刷新」按钮不该只刷一半 */
  const refresh = () => {
    void load()
    void loadPipe()
  }

  /**
   * 补跑：只跑**到点**的重试。没到点的捞出来等于绕过退避，源挂着会被反复打。
   * `retryDomain` 非空就只补那一个域 —— 挑域同样不改变「只跑到点的」这条。
   */
  const doRetry = () => {
    setRetrying(true)
    setRetryMsg(null)
    api
      .collectRetry(retryDomain || undefined)
      .then((r) => {
        const bad = r.results.filter((x) => x.status === 'error').length
        const where = retryDomain ? `「${label(retryDomain)}」` : '全部域'
        setRetryMsg(
          r.checked === 0
            ? `${where}当前没有到点的重试（还在退避窗口里的不会被捞出来）`
            : `${where}：应跑 ${r.checked} 条、实跑 ${r.ran} 条，其中失败 ${bad} 条`,
        )
        return loadPipe()
      })
      .catch((e: Error) => setRetryMsg(e.message))
      .finally(() => setRetrying(false))
  }

  /** 指定日期补跑一轮完整盘后任务（含打分，12-15 分钟），后台跑、立即返回。 */
  const runDatedJob = () => {
    setJobBusy(true)
    setJobMsg(null)
    api
      .runJob(jobDate.trim() || undefined)
      .then(() =>
        setJobMsg('已启动后台任务（含打分，约 12-15 分钟）。跑完再点刷新即可看到结果。'),
      )
      .catch((e: Error) => setJobMsg(e.message))
      .finally(() => setJobBusy(false))
  }

  /**
   * 回填计划。`end` 留空 = 后端补最新交易日（不是写死空串一路透传 ——
   * 那样区间就永远是「起始日到起始日」）。
   */
  const loadPlan = () =>
    api.backfillPlan(planDomain, planStart.trim(), planEnd.trim(), planScope)

  /** 先看计划（零联网）——「要发多少请求」必须在动手之前就摆在眼前。 */
  const showPlan = () => {
    setPlanBusy(true)
    setPlanErr(null)
    setRunMsg(null)
    loadPlan()
      .then((p) => setPlan(p))
      .catch((e: Error) => {
        setPlan(null)
        setPlanErr(e.message)
      })
      .finally(() => setPlanBusy(false))
  }

  /**
   * 执行回填。**会联网且是写库动作**，所以：必须先看过计划才可点、
   * 每次只给 `BUDGET` 次请求（跑完回读计划看还剩多少），绝不一次跑几小时 ——
   * 那种长跑交给 `scripts/backfill_run.py`（带熔断与断点续跑）。
   */
  const doBackfill = () => {
    setRunBusy(true)
    setRunMsg(null)
    api
      .backfillRun({
        domain: planDomain,
        start: plan?.start ?? '',
        end: plan?.end ?? '',
        scope: planScope,
        budget: BUDGET,
      })
      .then((r) => {
        setRunMsg(
          r.finished
            ? `这一轮跑完：新增 ${r.inserted} 行、失败 ${r.failed} 条。再看一次计划确认还剩什么。`
            : `这一批 ${r.ran} 只跑完：新增 ${r.inserted} 行、失败 ${r.failed} 条 —— 还没跑完，可继续。`,
        )
        return loadPipe().then(() => loadPlan())
      })
      .then((p) => setPlan(p))
      .catch((e: Error) => setRunMsg(e.message))
      .finally(() => setRunBusy(false))
  }

  const coverageColumns: ColumnsType<PipelineDataset> = [
    {
      title: '数据域',
      dataIndex: 'dataset',
      width: 172,
      render: (v: string) => (
        <Space size={6}>
          <Text strong>{label(v)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {v}
          </Text>
        </Space>
      ),
    },
    {
      title: '状态',
      dataIndex: 'state',
      width: 96,
      render: (s: string) => {
        const t = STATE_TAG[s]
        return <Tag color={t?.color}>{t?.text ?? s}</Tag>
      },
    },
    {
      title: '区间',
      key: 'span',
      width: 178,
      render: (_, d) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {d.first_date ?? '—'} ~ {d.last_date ?? '—'}
        </Text>
      ),
    },
    {
      title: '最新采集',
      key: 'latest',
      width: 118,
      render: (_, d) => {
        // 与「区间」里的 last_date 是两回事：那个是**数据**到哪天，
        // 这个是**最近一次采集记录**在哪天 —— 域采失败时两者会分开。
        const h = health?.domains.find((x) => x.domain === d.dataset)
        if (!h) return <Text type="secondary">—</Text>
        if (!h.collected) return <Text type="secondary">未采过</Text>
        const alias = h.status_alias
          ? (h.status ? INGEST_TAG[h.status] : undefined) ?? {
              color: 'default',
              text: h.status_alias,
            }
          : null
        return (
          <Space size={4}>
            <Text style={{ fontSize: 12 }}>{fmtDate(h.trade_date)}</Text>
            {alias && <Tag color={alias.color}>{alias.text}</Tag>}
          </Space>
        )
      },
    },
    {
      title: '缺失区间',
      key: 'missing_ranges',
      width: 168,
      render: (_, d) =>
        d.missing_dates.length ? (
          <Tooltip title={fmtRanges(d.missing_dates)}>
            <Text type="warning" style={{ fontSize: 12 }}>
              {fmtRanges(d.missing_dates)}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary" style={{ fontSize: 12 }}>
            —
          </Text>
        ),
    },
    {
      title: '覆盖率',
      dataIndex: 'coverage_rate',
      width: 76,
      align: 'right',
      render: (v: number | null) => (v === null ? '—' : `${Math.round(v * 100)}%`),
    },
    {
      title: '历史缺',
      key: 'missing',
      width: 74,
      align: 'right',
      render: (_, d) =>
        d.missing_dates.length ? (
          <Text type="warning">{d.missing_dates.length}</Text>
        ) : (
          '0'
        ),
    },
    {
      title: '近 30 日缺',
      key: 'recent',
      width: 92,
      align: 'right',
      render: (_, d) => {
        const n = (gaps?.gaps[d.dataset] ?? []).length
        return n ? <Text type="danger">{n}</Text> : '0'
      },
    },
    {
      title: '条数',
      dataIndex: 'rows_total',
      width: 84,
      align: 'right',
    },
    {
      title: '读不出',
      key: 'bad',
      width: 74,
      align: 'right',
      render: (_, d) =>
        d.unreadable_days.length ? (
          <Tooltip
            title={`落库后解析不出内容的日子：${d.unreadable_days.join(', ')}`}
          >
            <Tag color="red">{d.unreadable_days.length}</Tag>
          </Tooltip>
        ) : (
          '0'
        ),
    },
    {
      title: '最近一次错误',
      key: 'err',
      render: (_, d) =>
        d.job.last_error ? (
          <Tooltip title={d.job.last_error}>
            <Text type="danger" style={{ fontSize: 12 }}>
              {d.job.last_error.slice(0, 44)}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ]

  const ingestColumns: ColumnsType<IngestItem> = [
    { title: '数据域', dataIndex: 'domain', width: 150, render: (v: string) => label(v) },
    { title: '交易日', dataIndex: 'trade_date', width: 96 },
    {
      title: '状态',
      dataIndex: 'status',
      width: 104,
      render: (s: string) => {
        const t = INGEST_TAG[s]
        return <Tag color={t?.color}>{t?.text ?? s}</Tag>
      },
    },
    {
      title: '尝试',
      key: 'try',
      width: 66,
      align: 'right',
      render: (_, r) => `${r.attempts}/${r.max_attempts}`,
    },
    {
      title: '源返回/落库/没过',
      key: 'cnt',
      width: 130,
      render: (_, r) => (
        <Text type="secondary" style={{ fontSize: 12 }}>
          {r.processed ?? '—'} / {r.stored ?? '—'} / {r.failed_rows ?? '—'}
        </Text>
      ),
    },
    {
      title: '错误',
      key: 'err',
      render: (_, r) =>
        r.error ? (
          <Tooltip title={r.error}>
            <Text type="danger" style={{ fontSize: 12 }}>
              {r.error.slice(0, 44)}
            </Text>
          </Tooltip>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
  ]

  // 卡片上的几个摘要数。渲染期算，不碰 new Date() —— React Compiler 会因为
  // 不纯调用跳过整个组件的记忆化。
  const dfac = bars?.tables.daily_bar?.factor
  const factorText = !dfac
    ? '—'
    : dfac.supported
      ? `${dfac.with_factor ?? 0}/${dfac.rows}`
      : '—'
  const factorHint = !dfac
    ? '未取到'
    : !dfac.supported
      ? (dfac.note ?? '该表按 schema 就没有 adj_factor 列')
      : dfac.rate === null
        ? '判不了'
        : `${(dfac.rate * 100).toFixed(1)}% 已填 · ${(dfac.without_factor ?? 0)} 行判不了除权`

  const prog = bars?.progress
  const progressText = !prog ? '—' : prog.rate === null ? '判不了' : `${prog.covered}/${prog.codes}`
  const progressHint = !prog
    ? '未取到'
    : prog.rate === null
      ? prog.note
      : `完成 ${prog.covered} · 缺日子 ${prog.partial} · 无数据 ${prog.absent}`

  // ---- 「数据质量总览」卡的派生值。渲染期算，全是纯计算（不碰 Date）------
  const covList = cov ? Object.values(cov.datasets) : []
  const okCount = covList.filter((d) => d.state === 'OK').length
  /** 五态里非 OK 的分布；一项为 0 就不占字（'完整 12/12' 比三行 0 有用） */
  const covHint = covList.length
    ? (['PARTIAL', 'NO_DATA', 'SOURCE_DOWN', 'NO_JOB'] as const)
        .map((k) => `${STATE_TAG[k].text} ${covList.filter((d) => d.state === k).length}`)
        .filter((s) => !/\s0$/.test(s))
        .join(' · ') || '全部完整'
    : '未取到'
  const lastDetail = parseDetail(jstat?.last?.detail)
  const lastFailed = lastDetail?.failed ?? []
  const lastSecs = secondsBetween(
    jstat?.last?.started_at,
    jstat?.last?.finished_at,
  )

  const datasetColumns: ColumnsType<DataDataset> = [
    {
      title: '数据集',
      dataIndex: 'dataset',
      width: 190,
      render: (v: string) => (
        <Space size={6}>
          <Text strong>{label(v)}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {v}
          </Text>
        </Space>
      ),
    },
    {
      title: '交易日',
      dataIndex: 'dates',
      width: 76,
      align: 'right',
    },
    {
      title: '条目',
      dataIndex: 'entries',
      width: 88,
      align: 'right',
      render: (v: number) => v.toLocaleString('zh-CN'),
    },
    {
      title: '占用',
      dataIndex: 'bytes',
      width: 96,
      align: 'right',
      sorter: (a, b) => a.bytes - b.bytes,
      defaultSortOrder: 'descend',
      render: (v: number) => fmtBytes(v),
    },
    {
      title: '日期范围',
      key: 'span',
      width: 190,
      render: (_, r) => (
        <Text type="secondary">
          {r.undelatable ? '只存 latest（无历史）' : `${fmtDate(r.date_min)} ~ ${fmtDate(r.date_max)}`}
        </Text>
      ),
    },
    {
      title: '最后写入',
      dataIndex: 'fetched_at',
      width: 165,
      render: (v?: string | null) => (
        <Text type="secondary">{v ? v.replace('T', ' ').slice(0, 19) : '-'}</Text>
      ),
    },
    {
      title: '硬下限',
      dataIndex: 'floor_days',
      width: 100,
      render: (v: number, r) =>
        r.undelatable ? (
          <Tag color="red">不可删</Tag>
        ) : (
          <Tooltip
            title={`删完至少保留 ${v} 天。少于它会破坏矩阵回看 / 日期下拉 / 打分窗口，后端直接拒绝。`}
          >
            <Tag>≥ {v} 天</Tag>
          </Tooltip>
        ),
    },
    {
      title: '操作',
      key: 'act',
      width: 84,
      render: (_, r) => (
        <Button
          size="small"
          disabled={r.undelatable}
          onClick={() => {
            setDs(r.dataset)
            setMode('date')
            setDay('')
            reset()
          }}
        >
          清理
        </Button>
      ),
    },
  ]

  const logColumns: ColumnsType<DataFile> = [
    {
      title: '文件',
      dataIndex: 'name',
      width: 190,
      render: (v: string, r) => (
        <Space size={6}>
          <Text strong>{r.label ?? v}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            {v}
          </Text>
        </Space>
      ),
    },
    {
      title: '大小',
      dataIndex: 'bytes',
      width: 104,
      align: 'right',
      sorter: (a, b) => a.bytes - b.bytes,
      render: (v: number) => fmtBytes(v),
    },
    {
      title: '最后写入',
      dataIndex: 'mtime',
      width: 175,
      render: (v?: number) => <Text type="secondary">{fmtMtime(v)}</Text>,
    },
    {
      title: '操作',
      key: 'act',
      width: 96,
      render: (_, r) => (
        <Popconfirm
          title={`清空 ${r.name}？`}
          description="原地截断到 0 字节，不可恢复。正在写入的服务不受影响，会继续往后写。"
          okText="清空"
          okButtonProps={{ danger: true }}
          onConfirm={() => purge('log', false, { log_name: r.name })}
        >
          <Button size="small" danger disabled={!r.bytes || applying}>
            清空
          </Button>
        </Popconfirm>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title="数据管理"
        extra={
          <Button size="small" onClick={refresh} loading={previewing || applying}>
            刷新
          </Button>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            title="只看、只动本地文件，全程零外网请求"
            description={
              <>
                统计与清理只碰数据库文件
                {usage?.db.path ? `（${usage.db.path}）` : ''} 与 run/ 下的日志，
                后端不为此发起任何网络请求 —— 内网下的外网请求会被安全设备拦截并记录。
                删除一律先预览、不写库；真删需要二次确认，硬下限内的数据后端直接拒绝。
              </>
            }
          />
          {error && <Alert type="error" showIcon title={error} />}
          {purgeErr && <Alert type="error" showIcon title={purgeErr} />}

          <Spin spinning={!usage && !error}>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))',
                gap: 12,
              }}
            >
              <Stat
                label="数据库占用"
                value={fmtBytes(usage?.db.bytes)}
                hint={`${usage?.db.page_count ?? 0} 页 × ${usage?.db.page_size ?? 0} B`}
              />
              <Stat
                label="数据集"
                value={`${datasets.length}`}
                hint={`合计 ${fmtBytes(datasets.reduce((s, d) => s + d.bytes, 0))}`}
              />
              <Stat
                label="可回收空间"
                value={fmtBytes(reclaim)}
                hint={reclaim > 0 ? '删除后需 VACUUM 才真缩' : '无已删未回收的页'}
              />
              <Stat
                label="任务日志残留"
                value={`${usage?.job_run.unfinished ?? 0}`}
                hint={`${usage?.job_run.rows ?? 0} 行 · ${fmtBytes(usage?.job_run.bytes)}`}
              />
              <Stat label="运行期日志" value={fmtBytes(logTotal)} hint="超阈值时启动轮转" />
              <Stat
                label="关注股"
                value={`${usage?.watchlist_rows ?? 0}`}
                hint="用户数据，不参与任何清理"
              />
            </div>
          </Spin>
        </Space>
      </Card>

      <Card
        title="占用明细"
        extra={<Text type="secondary">字节数为 payload 的 UTF-8 大小</Text>}
      >
        <Table<DataDataset>
          rowKey="dataset"
          size="small"
          columns={datasetColumns}
          dataSource={datasets}
          pagination={false}
          scroll={{ x: 990 }}
        />
      </Card>

      <Card
        title="按数据集删除"
        extra={
          <Tag color={current?.undelatable ? 'red' : 'blue'}>
            {current ? label(current.dataset) : '-'}
          </Tag>
        }
      >
        <Space wrap size={12}>
          <Select
            style={{ width: 230 }}
            value={ds}
            onChange={(v) => {
              setDs(v)
              reset()
            }}
            options={datasets.map((d) => ({
              value: d.dataset,
              label: `${label(d.dataset)}（${d.dates} 天 · ${fmtBytes(d.bytes)}）`,
              disabled: d.undelatable,
            }))}
          />
          <Select
            style={{ width: 160 }}
            value={mode}
            onChange={(v) => {
              setMode(v as 'date' | 'before')
              reset()
            }}
            options={[
              { value: 'date', label: '只删这一天' },
              { value: 'before', label: '删这一天之前' },
            ]}
          />
          <Input
            style={{ width: 170 }}
            placeholder="YYYYMMDD"
            value={day}
            maxLength={8}
            onChange={(e) => {
              setDay(e.target.value.replace(/\D/g, ''))
              reset()
            }}
          />
          <Button
            disabled={!dayOk || current?.undelatable || previewing || applying}
            loading={previewing}
            onClick={() => purgeSnapshot(true)}
          >
            预览
          </Button>
          <Popconfirm
            title="确认删除？"
            description={
              result?.dry_run
                ? `将永久删除 ${result.deletable ?? 0} 天（${fmtBytes(result.deletable_bytes)}），不可恢复。`
                : '先点「预览」看清楚要删什么。'
            }
            okText="删除"
            okButtonProps={{ danger: true }}
            onConfirm={() => purgeSnapshot(false)}
          >
            <Button danger disabled={!dayOk || !result?.dry_run || applying} loading={applying}>
              确认删除
            </Button>
          </Popconfirm>
        </Space>
        <div style={{ marginTop: 10 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            两条底线由后端强制：sector_flow_* 只存 latest 一行，删掉板块页会直接空，已锁定不可删；
            其余数据集至少保留最近 1 天（逐日资金流 5 天），那是打分窗口的下限。
          </Text>
        </div>
        {result?.scope === 'snapshot' && (
          <div style={{ marginTop: 12 }}>
            <PurgeResult r={result} />
          </div>
        )}
      </Card>

      <Card
        title="按保留策略清理"
        extra={
          <Text type="secondary">
            打分 {usage?.retention.main_force_days} 天 · 资金流{' '}
            {usage?.retention.inflow_days} 天 · 涨跌停池 {usage?.retention.pool_days} 天
          </Text>
        }
      >
        <Space wrap size={12}>
          <Text type="secondary">
            一次把每个数据集最老的那些天清掉，不用逐个挑日期。保留天数配在{' '}
            <Text code>backend/app/config.py</Text> 的 <Text code>retention_*</Text>，
            实际保留取「配置值」与硬下限里较大的那个；<Text code>sector_flow_*</Text>{' '}
            只存 latest 一行，不进计划。
          </Text>
          <Button
            disabled={applying || previewing}
            loading={previewing}
            onClick={() => purge('retention', true)}
          >
            预览
          </Button>
          <Popconfirm
            title="按保留策略清理？"
            description={
              result?.scope === 'retention'
                ? `将删除 ${result.total_deletable ?? 0} 天（${fmtBytes(result.total_bytes)}），不可恢复。`
                : '先点「预览」看清楚要删什么。'
            }
            okText="清理"
            okButtonProps={{ danger: true }}
            onConfirm={() => purge('retention', false)}
          >
            <Button
              danger
              disabled={result?.scope !== 'retention' || applying}
              loading={applying && result?.scope === 'retention'}
            >
              确认清理
            </Button>
          </Popconfirm>
        </Space>
        {result?.scope === 'retention' && (
          <div style={{ marginTop: 12 }}>
            <PurgeResult r={result} />
          </div>
        )}
      </Card>

      <Card title="任务日志（job_run）">
        <Space wrap size={12}>
          <Text type="secondary">
            共 {usage?.job_run.rows ?? 0} 行，未收尾{' '}
            <Text strong>{usage?.job_run.unfinished ?? 0} 行</Text>
            （进程被杀时留下、finished_at 为空），保留上限 {usage?.retention.job_run_keep ?? 200} 行。
          </Text>
          <Button
            disabled={applying || previewing}
            loading={previewing}
            onClick={() => purge('job_run', true, { unfinished_only: true })}
          >
            预览清理
          </Button>
          <Popconfirm
            title="清理任务日志？"
            description="只删未收尾的残留记录，不影响任何看板数据。"
            okText="清理"
            okButtonProps={{ danger: true }}
            onConfirm={() => purge('job_run', false, { unfinished_only: true })}
          >
            <Button
              danger
              disabled={!usage?.job_run.unfinished || result?.scope !== 'job_run' || applying}
              loading={applying}
            >
              确认清理
            </Button>
          </Popconfirm>
        </Space>
        {result?.scope === 'job_run' && (
          <div style={{ marginTop: 12 }}>
            <PurgeResult r={result} />
          </div>
        )}
      </Card>

      <Card title="回收空间">
        <Space wrap size={12}>
          <Text type="secondary">
            该库 auto_vacuum=0 —— 删除只是打标记，文件不会变小，得显式 VACUUM 才真回收。
            当前可回收 {fmtBytes(reclaim)}。
          </Text>
          <Popconfirm
            title="执行 VACUUM？"
            description="重写整个数据库文件（毫秒级），期间写入会短暂等待。没有可预览的内容。"
            okText="执行"
            onConfirm={() => purge('vacuum', false)}
          >
            <Button disabled={applying} loading={applying && result?.scope === 'vacuum'}>
              执行 VACUUM
            </Button>
          </Popconfirm>
        </Space>
        {result?.scope === 'vacuum' && (
          <div style={{ marginTop: 12 }}>
            <PurgeResult r={result} />
          </div>
        )}
      </Card>

      <Card title="运行期日志" extra={<Text type="secondary">合计 {fmtBytes(logTotal)}</Text>}>
        <Table<DataFile>
          rowKey="name"
          size="small"
          columns={logColumns}
          dataSource={logs}
          pagination={false}
        />
        <div style={{ marginTop: 8 }}>
          <Text type="secondary" style={{ fontSize: 12 }}>
            超过 20 MB 的日志会在下次启动服务时轮转成 .1。运行中的文件在 Windows 上被写入方
            占用、改不了名，所以轮转只在启动前做；这里能做的只有原地清空。
          </Text>
        </div>
        {result?.scope === 'log' && (
          <div style={{ marginTop: 12 }}>
            <PurgeResult r={result} />
          </div>
        )}
      </Card>

      <Card title="表与配置">
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Space wrap size={8}>
            {usage?.tables.map((t) => (
              <Tag key={t.name}>
                {t.name} {t.rows} 行
              </Tag>
            ))}
          </Space>
          <Space wrap size={8}>
            {usage?.kv_keys.map((k) => (
              <Tag key={k}>{k}</Tag>
            ))}
          </Space>
          <Text type="secondary" style={{ fontSize: 12 }}>
            配置只显示键名 —— 值是飞书 App Secret 与 webhook 签名密钥，接口不回传。
            任何清理都不碰：{(usage?.protected_tables ?? ['kv', 'watchlist']).join('、')}。
            逐日资金流外网回补
            <Tag style={{ marginInlineStart: 6 }}>
              {usage?.inflow_backfill ? '开启' : '关闭（STOCK_INFLOW_BACKFILL=false）'}
            </Tag>
            关闭时资金流只靠盘后任务积累，缺的日期不会自动补。
          </Text>
        </Space>
      </Card>

      {/* 阶段 6 汇总卡：所有数字复用本页已取的六份只读数据，**零新请求** */}
      <Card
        title="数据质量总览"
        extra={
          <Text type="secondary">
            {health
              ? `体检于 ${health.checked_at.slice(0, 19).replace('T', ' ')} UTC · `
              : ''}
            零联网
          </Text>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Spin spinning={!health && !jstat && !pipeErr}>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
                gap: 12,
              }}
            >
              <Stat
                label="最近一次成功交易日"
                value={
                  health?.last_success_date
                    ? fmtDate(health.last_success_date)
                    : '—'
                }
                hint={
                  health?.last_success
                    ? health.last_success.complete
                      ? '当天有记录的域全 SUCCESS'
                      : `覆盖 ${health.last_success.covered}/${health.last_success.core} 个核心域`
                    : '还没出现过全域 SUCCESS 的日子（PARTIAL 不算成功）'
                }
              />
              <Stat
                label="自动调度（盘后任务）"
                value={jstat ? (jstat.schedule.enabled ? '开' : '关') : '—'}
                hint={
                  jstat
                    ? `每天 ${jstat.schedule.hour}:${String(jstat.schedule.minute).padStart(2, '0')} · 下次 ${short(jstat.schedule.next_run)}`
                    : '未取到'
                }
              />
              <Stat
                label="估值推送"
                value={jstat ? (jstat.schedule.push_enabled ? '开' : '关') : '—'}
                hint={jstat ? `下次 ${short(jstat.schedule.next_push)}` : '未取到'}
              />
              <Stat
                label="到点待补跑"
                value={health ? String(health.retry_due) : '—'}
                hint={
                  health?.retry_due
                    ? `只报不跑 · 上次检查 ${short(health.retry_check?.checked_at)}`
                    : '只报不跑；人点「补跑重试」才会发请求'
                }
              />
              <Stat
                label="覆盖五态"
                value={cov ? `${okCount}/${covList.length} 完整` : '—'}
                hint={covHint}
              />
              <Stat label="日线回填" value={progressText} hint={progressHint} />
              <Stat label="复权因子" value={factorText} hint={factorHint} />
            </div>
          </Spin>
          <div>
            <Text type="secondary" style={{ fontSize: 12 }}>
              盘后任务进度
            </Text>
            {jstat?.current.running ? (
              <>
                <JobProgress current={jstat.current} />
                {jstat.current.error && (
                  <Alert type="error" showIcon title={jstat.current.error} />
                )}
              </>
            ) : (
              <Space direction="vertical" size={8} style={{ width: '100%' }}>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {jstat?.last
                    ? `没在运行。上次（${jstat.last.kind}）跑的是 ${fmtDate(lastDetail?.date)} 的数据：`
                    : jstat
                      ? '没在运行，还没有运行记录。'
                      : '任务状态未取到。'}
                  {jstat?.last && (
                    <Tag color={STATUS_COLOR[jstat.last.status]}>
                      {STATUS_LABEL[jstat.last.status] ?? jstat.last.status}
                    </Tag>
                  )}
                  {lastSecs != null && `耗时 ${lastSecs}s`}
                </Text>
                {lastFailed.length > 0 && (
                  <Alert
                    type="warning"
                    showIcon
                    title="上次运行失败的步骤"
                    description={`${lastFailed.join('、')}（错误详情见「任务日志（job_run）」卡）`}
                  />
                )}
              </Space>
            )}
          </div>
        </Space>
      </Card>

      <Card
        title="采集覆盖与缺口"
        extra={
          <Text type="secondary">
            {cov ? `审计于 ${cov.generated_at.slice(0, 19).replace('T', ' ')} UTC · ` : ''}
            全程零联网
          </Text>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          {pipeErr && (
            <Alert
              type="warning"
              showIcon
              title="部分只读接口没取到"
              description={pipeErr}
            />
          )}
          {cov?.calendar.stale && (
            <Alert
              type="warning"
              showIcon
              title="交易日历已过期"
              description={cov.calendar.note}
            />
          )}
          <Spin spinning={!cov && !pipeErr}>
            <Table<PipelineDataset>
              rowKey="dataset"
              size="small"
              pagination={false}
              dataSource={Object.values(cov?.datasets ?? {})}
              columns={coverageColumns}
              scroll={{ x: 1316 }}
            />
          </Spin>
          <Text type="secondary" style={{ fontSize: 12 }}>
            五态口径：完整 = 日期齐且字段齐；不完整 = 缺日期 / 缺字段 / 条数骤降 /
            读不出内容；采了但空 = 任务跑过却一条没拿到；源故障 = 有错误记录；
            无人采 = 这一轮压根没有这一步。
          </Text>
        </Space>
      </Card>

      <Card
        title="历史行情与复权因子"
        extra={
          <Text type="secondary">
            {prog ? `${prog.window.start ?? '—'} ~ ${prog.window.end ?? '—'} · ` : ''}
            零联网
          </Text>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Spin spinning={!bars && !pipeErr}>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
                gap: 12,
              }}
            >
              {(['daily_bar', 'index_daily'] as const).map((t) => {
                const c = bars?.tables[t]?.coverage
                return (
                  <Stat
                    key={t}
                    label={t === 'daily_bar' ? '个股日线' : '沪深300 指数'}
                    value={c ? `${c.rows} 行` : '—'}
                    hint={c ? `${c.codes} 只 · ${c.days} 个交易日` : '未取到'}
                  />
                )
              })}
              <Stat label="复权因子" value={factorText} hint={factorHint} />
              <Stat label="回填进度" value={progressText} hint={progressHint} />
            </div>
          </Spin>

          {prog &&
            (prog.rate === null ? (
              <Alert type="info" showIcon title="进度无法判定" description={prog.note} />
            ) : (
              <div>
                <div
                  style={{
                    display: 'flex',
                    justifyContent: 'space-between',
                    fontSize: 12,
                    color: '#888',
                  }}
                >
                  <span>
                    名单 {prog.codes} 只 · 表内 {prog.days} 个交易日
                  </span>
                  <span>
                    完成 {prog.covered} · 缺日子 {prog.partial} · 无数据 {prog.absent}
                  </span>
                </div>
                <Progress
                  percent={Math.round(prog.rate * 100)}
                  status={prog.rate >= 1 ? 'success' : 'active'}
                />
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {prog.note}
                </Text>
              </div>
            ))}

          {prog && prog.incomplete.length > 0 && (
            <Table
              rowKey="code"
              size="small"
              pagination={false}
              dataSource={prog.incomplete}
              columns={[
                { title: '代码', dataIndex: 'code', width: 100 },
                { title: '已有天数', dataIndex: 'have', width: 92, align: 'right' },
                {
                  title: '还缺',
                  dataIndex: 'missing',
                  width: 76,
                  align: 'right',
                  render: (v: number) => <Text type="warning">{v}</Text>,
                },
                {
                  title: '缺的日期',
                  dataIndex: 'missing_dates',
                  render: (v: string[]) => (
                    <Tooltip title={fmtRanges(v)}>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {fmtRanges(v)}
                      </Text>
                    </Tooltip>
                  ),
                },
              ]}
            />
          )}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {bars?.note}
          </Text>
        </Space>
      </Card>

      <Card
        title="采集状态与补跑"
        extra={
          <Space size={8}>
            <Select
              size="small"
              style={{ width: 200 }}
              value={retryDomain}
              onChange={setRetryDomain}
              options={[
                { value: '', label: '补跑范围：全部到点' },
                ...Array.from(new Set((ingest?.items ?? []).map((x) => x.domain)))
                  .sort()
                  .map((d) => ({ value: d, label: `${label(d)}（${d}）` })),
              ]}
            />
            <Button size="small" onClick={doRetry} loading={retrying}>
              补跑重试
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="warning"
            showIcon
            title="补跑会联网"
            description="只跑已经到点的重试；没到点的一条都不碰（绕过退避会让源挂着的时候被反复打）。左边的范围下拉可只补一个数据域 —— 挑域同样不绕过退避。服务启动、页面刷新、任何定时器都不会自动调用它。"
          />
          {retryMsg && (
            <Alert type="info" showIcon title="补跑结果" description={retryMsg} />
          )}
          {jobMsg && (
            <Alert
              type={jobMsg.startsWith('已启动') ? 'info' : 'error'}
              showIcon
              title="盘后任务"
              description={jobMsg}
            />
          )}
          <Spin spinning={!ingest && !pipeErr}>
            <Table<IngestItem>
              rowKey="id"
              size="small"
              pagination={{ pageSize: 10, showSizeChanger: false }}
              dataSource={ingest?.items ?? []}
              columns={ingestColumns}
              scroll={{ x: 760 }}
            />
          </Spin>
          <Space wrap size={12}>
            <Text type="secondary">指定日期补跑一轮完整盘后任务（含打分，约 12-15 分钟）：</Text>
            <Input
              style={{ width: 210 }}
              placeholder="YYYYMMDD，留空 = 目标交易日"
              value={jobDate}
              maxLength={8}
              onChange={(e) => setJobDate(e.target.value.replace(/\D/g, ''))}
            />
            <Button loading={jobBusy} onClick={runDatedJob}>
              跑这一轮
            </Button>
          </Space>
        </Space>
      </Card>

      <Card title="历史回填：先看计划，再动手">
        <Space direction="vertical" size={12} style={{ width: '100%' }}>
          <Alert
            type="info"
            showIcon
            title="计划零联网，执行才联网"
            description="计划只读本地库：已经补过什么、还差多少只、要发多少次请求。执行才会真的往外发请求，且每次最多 20 次请求（跑完自动回读计划看剩余）；几千次的长跑请用 scripts/backfill_run.py —— 那边有分批熔断与断点续跑。同一时间只应有一个进程在写库。"
          />
          <Space wrap size={12}>
            <Select
              style={{ width: 178 }}
              value={planDomain}
              onChange={setPlanDomain}
              options={[
                { value: 'daily_bar', label: '全市场个股日线' },
                { value: 'index', label: '沪深300 指数日线' },
              ]}
            />
            <Select
              style={{ width: 156 }}
              value={planScope}
              onChange={setPlanScope}
              options={[
                { value: 'all', label: '名单：全市场' },
                { value: 'mf', label: '名单：主力建仓' },
                { value: 'pool', label: '名单：股票池' },
                { value: 'wl', label: '名单：自选' },
              ]}
            />
            <Input
              style={{ width: 200 }}
              placeholder="起始日 YYYYMMDD，留空 = 默认"
              value={planStart}
              maxLength={8}
              onChange={(e) => setPlanStart(e.target.value.replace(/\D/g, ''))}
            />
            <Input
              style={{ width: 200 }}
              placeholder="结束日 YYYYMMDD，留空 = 最新交易日"
              value={planEnd}
              maxLength={8}
              onChange={(e) => setPlanEnd(e.target.value.replace(/\D/g, ''))}
            />
            <Button loading={planBusy} onClick={showPlan}>
              查看计划
            </Button>
            <Popconfirm
              title="确认开始回填？"
              description={
                plan
                  ? `将至多发出 ${BUDGET} 次外网请求（计划显示还差 ${plan.to_fetch} 只）。内网环境下的外发会被安全设备拦截并记录。`
                  : '先点「查看计划」，看清楚要发多少请求。'
              }
              okText="开始"
              onConfirm={doBackfill}
            >
              <Button
                type="primary"
                danger
                disabled={!plan || plan.to_fetch === 0 || planBusy || runBusy}
                loading={runBusy}
              >
                开始回填
              </Button>
            </Popconfirm>
          </Space>
          {planErr && (
            <Alert type="error" showIcon title="计划算不出来" description={planErr} />
          )}
          {runMsg && <Alert type="info" showIcon title="回填结果" description={runMsg} />}
          {plan && (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
                gap: 12,
              }}
            >
              <Stat label="区间" value={`${plan.start} ~ ${plan.end}`} hint={plan.label} />
              <Stat
                label="已补过"
                value={`${plan.already_done}`}
                hint="区间已盖住，压根不发请求"
              />
              <Stat
                label="待回填"
                value={`${plan.to_fetch}`}
                hint={
                  plan.todo_truncated
                    ? `还有更多，只列出前 ${plan.todo.length} 只`
                    : `要发约 ${plan.to_fetch} 次取数请求`
                }
              />
              <Stat
                label="复权口径"
                value={plan.adjust === '' ? '不复权 + 独立因子' : '本域不适用'}
                hint={plan.note}
              />
            </div>
          )}
        </Space>
      </Card>

      <Alert
        type="warning"
        showIcon
        title="数据免责声明"
        description="本页仅用于查看与维护本地数据文件。所有数据来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
      />
    </Space>
  )
}
