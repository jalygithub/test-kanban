/**
 * 交易时段判定 —— 实时行情视图顶部那枚「交易中 / 午间休市 / 已收盘」标签的依据。
 *
 * 这个文件原来叫 `liveMock.ts`，还带一整套按代码 hash 生成的**演示数据**
 * （`buildSnapshot` / `buildFlow` / `buildBase`）。实时行情接上真接口
 * （`api.realtime` → `/api/realtime`）之后，演示数据已整体删除 —— 盯盘页上
 * 不该再出现任何一个造出来的数字。留下的只有与数据源无关的时段判定。
 *
 * 时段照抄两个市场的实际规则，别改：
 *   A 股 09:30-11:30 + 13:00-15:00
 *   港股 09:30-12:00 + 13:00-16:00
 */
import type { Market } from './api'

export type { Market }

export type SessionState = 'pre' | 'open' | 'break' | 'closed'

/** [开盘, 收盘]，单位为自 00:00 起的分钟数 */
const SESSIONS: Record<Market, [number, number][]> = {
  cn: [
    [570, 690], // 09:30-11:30
    [780, 900], // 13:00-15:00
  ],
  hk: [
    [570, 720], // 09:30-12:00
    [780, 960], // 13:00-16:00
  ],
}

export const SESSION_LABEL: Record<SessionState, string> = {
  pre: '未开盘',
  open: '交易中',
  break: '午间休市',
  closed: '已收盘',
}

/**
 * 收盘后与开盘前的午休/隔夜都归到 `break`，四态足够打标签了。
 *
 * **这里只看时钟，不看日历** —— 周末与节假日它照样会说「交易中」。实时视图
 * 刻意不接交易日历：盯盘页最怕的是把「今天休市」渲染成「数据拿不到」，两件事
 * 分开看更清楚；真到休市日，报价会停在最后一笔、`updated_at` 也不会往前走。
 */
export function sessionState(market: Market, now: Date): SessionState {
  const cur = now.getHours() * 60 + now.getMinutes()
  const ss = SESSIONS[market]
  for (const [a, b] of ss) {
    if (cur >= a && cur < b) return 'open'
  }
  if (cur < ss[0][0]) return 'pre'
  if (cur >= ss[ss.length - 1][1]) return 'closed'
  return 'break'
}
