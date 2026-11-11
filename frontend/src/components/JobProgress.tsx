import { Progress, Space, Typography } from 'antd'
import type { JobCurrent } from '../lib/api'

const { Text } = Typography

/** 流水线**步骤名** -> 中文，给进度条用。
 *  刻意不复用 DATASET_LABEL：池和打分的步骤名恰好等于数据集名，但两个板块
 *  不是 —— 步骤叫 `sector_industry`（scheduler._collect 拼的），数据集才叫
 *  `sector_flow_industry`。拿 DATASET_LABEL 去查会掉出原始键名。 */
const STEP_LABEL: Record<string, string> = {
  pool_zt: '涨停池',
  pool_zb: '炸板池',
  pool_dt: '跌停池',
  pool_strong: '强势股',
  sector_industry: '行业资金流',
  sector_concept: '概念资金流',
  daily_bar: '日线采集',
  main_force: '主力建仓打分',
  top5_track: '前5跟踪取价',
}

/** 后台任务进度：进度条 + 正在做什么 + 已完成哪些步骤。
 *
 *  2026-10-17 阶段 6 从「总览」页抽到 components/：「数据管理」页也要展示
 *  同一份 `job_status.current` 进度，两处各写一遍进度口径迟早会分叉。
 *
 *  分母是后端给的 `plan`（而不是写死 7）—— 步骤增减只在 scheduler 一处改，
 *  这边跟着走就不错位。
 *
 *  `main_force` 一步占整条流水线八成以上的墙钟时间，所以把打分内部的
 *  「日线 x/N」折算成小数加进百分比；不加的话进度条会冲到 6/7=86% 之后
 *  一动不动地卡二十分钟，比不显示更像卡死。 */
export function JobProgress({ current }: { current: JobCurrent }) {
  const plan = current.plan ?? []
  const doneKeys = Object.keys(current.steps ?? {})
  const total = plan.length
  const sub = current.sub
  const inScoring = current.current_step === 'main_force'
  const subFrac = inScoring && sub && sub.total > 0
    ? Math.min(1, sub.done / sub.total)
    : 0
  const pct = total
    ? Math.round(
        ((Math.min(doneKeys.length, total) + subFrac) / total) * 100,
      )
    : 0
  const cur = current.current_step
    ? (STEP_LABEL[current.current_step] ?? current.current_step)
    : '收尾'
  const doneList = doneKeys.map((k) => STEP_LABEL[k] ?? k)

  return (
    <Space orientation="vertical" style={{ width: '100%' }} size={6}>
      <Progress
        percent={pct}
        format={() => (total ? `${doneKeys.length}/${total} 步` : `${doneKeys.length} 步`)}
      />
      <div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          正在：{cur}
          {inScoring && sub?.stage
            ? ` · ${sub.stage}${sub.total > 0 ? ` ${sub.done}/${sub.total}` : ''}`
            : ''}
          {inScoring ? '（这步最长，约 12-15 分钟）' : ''}
        </Text>
      </div>
      <div>
        <Text type="secondary" style={{ fontSize: 12 }}>
          已完成：{doneList.length ? doneList.join('、') : '—'}
          {total ? `（${doneList.length}/${total}）` : ''}
        </Text>
      </div>
      {current.started_at ? (
        <div>
          <Text type="secondary" style={{ fontSize: 12 }}>
            开始于 {current.started_at}
          </Text>
        </div>
      ) : null}
    </Space>
  )
}
