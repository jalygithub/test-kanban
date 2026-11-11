/**
 * 页面共用的展示格式化：日期、相对时间、任务状态标签。
 *
 * 「总览」和「定时任务」两页都要展示同一份 job_run 数据，口径必须一致 ——
 * 分散在各页面里各写一遍，早晚会出现一边显示「成功」一边显示「success」。
 */

/** YYYYMMDD -> YYYY-MM-DD；不是 8 位数字就原样返回。 */
export const fmtDate = (d?: string | null) =>
  d && /^\d{8}$/.test(d)
    ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}`
    : (d ?? '-')

/** ISO 时间 -> `2026-10-12 14:50`；没有就显示「—」，**不编一个日期出来**。 */
export const short = (s: string | null | undefined) =>
  s ? s.replace('T', ' ').slice(0, 16) : '—'

/**
 * null 安全的定长小数。**不能写 `(v ?? 0).toFixed(d)`** —— 那会把 null 变成 0，
 * 表格里就分不出「没数据」和「就是 0」。占位符是 `-`（与 `fmtPct` 同派）。
 */
export const fmtNum = (v?: number | null, d = 2) => (v == null ? '-' : v.toFixed(d))

/** 带符号涨跌幅 `+1.23%`。null 显示 `-`。 */
export const fmtPct = (v?: number | null) =>
  v == null ? '-' : `${v > 0 ? '+' : ''}${v.toFixed(2)}%`

/** 同 `fmtPct`，但调用方保证非空（分时、估值卡那两处的入参是 `number`）。 */
export const pctText = (v: number) => `${v > 0 ? '+' : ''}${v.toFixed(2)}%`

/** 元 -> 亿，一位小数；缺值显示 `-`（总览池子与主力榜单同一口径）。 */
export const yi = (v?: number | null) => (v == null ? '-' : (v / 1e8).toFixed(1))

/**
 * 排序比较：null 一律沉底。
 * **不能写 `nz(a) - nz(b)`** —— 两个 null 会得到 `-Infinity - -Infinity = NaN`，
 * antd 拿到 NaN 会给出不可复现的排序结果。
 */
export const cmp = (a: number | null | undefined, b: number | null | undefined) => {
  if (a == null && b == null) return 0
  if (a == null) return -1
  if (b == null) return 1
  return a - b
}

/**
 * 简单移动平均，前 n-1 根留空。
 * 入参只要 `close`，所以日线类型不必为它互相 import。
 */
export const ma = (bars: { close: number | null }[], n: number) =>
  bars.map((_, i) => {
    if (i < n - 1) return null
    let sum = 0
    for (let k = i - n + 1; k <= i; k++) sum += bars[k].close ?? 0
    return +(sum / n).toFixed(3)
  })

/** K 线涨跌色（红涨绿跌）。**与 `signType`/`signColor` 同口径**，只是 ECharts
 *  要的是颜色串不是 antd 语义色，所以常量单独放一份，别再在页面里手写 hex。 */
export const UP_COLOR = '#e53935'
export const DOWN_COLOR = '#1e9e57'

/**
 * 相对时间。`now` 由调用方在**加载完成时**捕获后传入 —— 渲染期直接调 `Date.now()`
 * 属于不纯调用，React 编译器会因此跳过整个组件的记忆化。
 * 未来时间（「下次触发」这类）返回「X 后」，否则会恒定显示「刚刚」。
 */
export const rel = (iso: string | null | undefined, now: number) => {
  if (!iso) return '待定'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const diff = (t - now) / 1000
  const n = Math.abs(diff)
  const unit =
    n < 60
      ? `${Math.round(n)} 秒`
      : n < 3600
        ? `${Math.round(n / 60)} 分钟`
        : n < 86400
          ? `${Math.round(n / 3600)} 小时`
          : `${Math.round(n / 86400)} 天`
  return `${unit}${diff < 0 ? '前' : '后'}`
}

/** 任务状态配色。历史行的 status 可能是旧值 'manual'。 */
export const STATUS_COLOR: Record<string, string> = {
  success: 'green',
  partial: 'orange',
  failed: 'red',
  running: 'blue',
  manual: 'default',
}
export const STATUS_LABEL: Record<string, string> = {
  success: '成功',
  partial: '部分失败',
  failed: '失败',
  running: '进行中',
  manual: '历史记录',
}

/** job_run.detail 是 JSON 字符串，坏了就当没有。 */
export function parseDetail(raw?: string | null) {
  if (!raw) return null
  try {
    return JSON.parse(raw) as {
      date?: string
      failed?: string[]
      results?: Record<string, unknown>
    }
  } catch {
    return null
  }
}

/** 两次 ISO 时间的间隔秒数，用于「耗时 Ns」。 */
export const secondsBetween = (from?: string | null, to?: string | null) => {
  if (!from || !to) return null
  const a = Date.parse(from)
  const b = Date.parse(to)
  if (Number.isNaN(a) || Number.isNaN(b)) return null
  return Math.max(0, Math.round((b - a) / 1000))
}

// ------------------------------------------------------------ 涨跌配色
//
// **全站唯一口径：红涨绿跌**（A股惯例，与 K 线一致）。
// 坑在于 antd 的语义色正好反着来 —— `success` 是绿、`error` 是红，所以
// `type={v >= 0 ? 'success' : 'danger'}` 会得到「绿涨红跌」。历史上好几个页面
// 各写各的，于是同一天的净流入在表格里是绿的、在柱状图里是红的。
// 统一收敛成这两个函数，新页面直接用，别再手写三元。

/** 给 `<Text type>` / `valueStyle` 用：>=0 红（danger），<0 绿（success）。 */
export const signType = (v: number | null | undefined) =>
  v == null ? undefined : v >= 0 ? ('danger' as const) : ('success' as const)

/** 给 ECharts / 内联 style 用：>=0 红，<0 绿。 */
export const signColor = (
  v: number | null | undefined,
  token: { colorError: string; colorSuccess: string },
) => (v == null ? undefined : v >= 0 ? token.colorError : token.colorSuccess)

/**
 * 金额/成交量压缩显示。**单位是元和股**，与各页现有口径一致 ——
 * 关注页与个股详情页原本各写了一份，实时行情页是第三处，收敛到这里。
 */
export const compact = (v: number) =>
  Math.abs(v) >= 1e8
    ? `${(v / 1e8).toFixed(1)}亿`
    : Math.abs(v) >= 1e4
      ? `${(v / 1e4).toFixed(0)}万`
      : String(Math.round(v))

// ---- 缺失区间合并（阶段 6）--------------------------------------------------

/**
 * `YYYYMMDD` 的下一天。**用整数日期算术、不用 `new Date()`** ——
 * 这个函数在渲染期被调用（表格单元格里算缺失区间），`Date` 是不纯调用，
 * 会让 React 编译器跳过整个组件的记忆化（见项目 AGENTS）。
 */
const nextDay = (s: string) => {
  const y = +s.slice(0, 4)
  const m = +s.slice(4, 6)
  const d = +s.slice(6, 8)
  const leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0
  const dim = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][
    m - 1
  ]
  if (!dim) return s // 月份越界：原样返回，不猜
  if (d < dim) return s.slice(0, 6) + String(d + 1).padStart(2, '0')
  if (m < 12) return s.slice(0, 4) + String(m + 1).padStart(2, '0') + '01'
  return `${y + 1}0101`
}

/**
 * 日期列表 -> 合并后的区间串：`[20261012, 20261013, 20261015]`
 * -> `'20261012 ~ 20261013、20261015'`。缺 243 天也只占一行，
 * 不会摊成一屏的逗号（覆盖审计域级与回填进度个股级两处共用）。
 *
 * 纯函数，**按日历天连续合并** —— 不拿交易日历去猜「这两个交易日之间
 * 隔了几天」，输入相邻才合并；先排序（定长数字，字典序=时间序），调用方
 * 不用管输入顺序。非 `YYYYMMDD` 的条目**原样保留在末尾**，不合并也不丢。
 */
export function fmtRanges(
  dates: readonly (string | null | undefined)[],
): string {
  const raw = (dates ?? []).filter((d): d is string => !!d)
  const odd = raw.filter((d) => !/^\d{8}$/.test(d))
  const days = raw.filter((d) => /^\d{8}$/.test(d)).sort()
  const parts: string[] = []
  let start = ''
  let prev = ''
  for (const d of days) {
    if (!prev) {
      start = prev = d
    } else if (d === nextDay(prev)) {
      prev = d
    } else {
      parts.push(start === prev ? start : `${start} ~ ${prev}`)
      start = prev = d
    }
  }
  if (prev) parts.push(start === prev ? start : `${start} ~ ${prev}`)
  parts.push(...odd)
  return parts.join('、')
}
