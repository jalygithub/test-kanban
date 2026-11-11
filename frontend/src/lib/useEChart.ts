import { useEffect, useRef, useState } from 'react'
import * as echarts from 'echarts/core'
import { BarChart, LineChart, ScatterChart, CandlestickChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkPointComponent,
  MarkLineComponent,
  // 板块资金流的图配了 title（图表内那行小标题），不注册会被 echarts 打回并报
  // "Component title is used but not imported" —— 标题静默不显示
  TitleComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsCoreOption, ECharts } from 'echarts/core'

// 按需注册，避免整包引入（echarts 6 同样支持 core 方式）
echarts.use([
  BarChart,
  LineChart,
  ScatterChart,
  CandlestickChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  MarkPointComponent,
  MarkLineComponent,
  TitleComponent,
  CanvasRenderer,
])

/**
 * 响应式 ECharts 实例：size 变化自动 resize，卸载时 dispose。
 *
 * **宿主元素用 state 跟踪，不能用 `useRef` + `[]`。**
 * 图表容器可能是条件渲染的（关注页要等 `detail` 回来才挂 div），
 * 首次挂载时 `hostRef.current` 还是 null，`[]` 的 effect 直接 return
 * 一次就再也不跑了 —— 结果是容器在、尺寸对，但 canvas 永远是 0 个。
 *
 * 返回的是 ref **回调**，调用方仍写 `ref={xxx}`，用法不变。
 *
 * 注意 init 和 setOption 必须在**同一个 commit** 里按声明顺序执行：
 * init 的 effect 在前、setOption 的在后，且 chart 实例放在 ref 里
 * 同步赋值，否则 setOption 会读到上一轮已被 dispose 的实例。
 *
 * `events` 是给图表加交互的口子（比如点柱子跳转）：每个 effect 周期
 * 挂载/卸载一次，调用方要用 `useMemo` 固定住引用，否则每次渲染都重绑。
 */
export type ChartEvents = Record<
  string,
  (params: { name?: string; dataIndex?: number; seriesName?: string; event?: Event }) => void
>

export function useEChart(option: EChartsCoreOption | null, events?: ChartEvents) {
  const [host, setHost] = useState<HTMLDivElement | null>(null)
  const chartRef = useRef<ECharts | null>(null)

  useEffect(() => {
    if (!host) return
    const inst = echarts.init(host)
    chartRef.current = inst

    const onResize = () => inst.resize()
    window.addEventListener('resize', onResize)
    const ro = new ResizeObserver(onResize)
    ro.observe(host)

    return () => {
      window.removeEventListener('resize', onResize)
      ro.disconnect()
      inst.dispose()
      chartRef.current = null
    }
  }, [host])

  useEffect(() => {
    if (option && chartRef.current) {
      chartRef.current.setOption(option, { notMerge: true })
    }
  }, [option, host])

  // 事件绑定放在 init 之后：此时 chartRef.current 才是本轮的实例
  useEffect(() => {
    const inst = chartRef.current
    if (!inst || !events) return
    const names = Object.keys(events)
    // echarts 的 handler 签名是 (...args: unknown[])，与调用方写死的参数类型
    // 逆变不兼容，这里过一道 cast（handler 实际只收一个 params 对象）
    const asHandler = (fn: ChartEvents[string]) =>
      fn as unknown as (...args: unknown[]) => boolean | void
    names.forEach((n) => inst.on(n, asHandler(events[n])))
    return () => {
      // off(name) 摘掉该事件的全部处理器 —— 这些都是本 hook 挂的，没有别人
      names.forEach((n) => inst.off(n))
    }
  }, [events, host])

  return setHost
}
