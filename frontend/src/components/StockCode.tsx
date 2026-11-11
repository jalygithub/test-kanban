import { Tooltip } from 'antd'
import { thsUrl } from '../lib/ths'

/**
 * 股票代码，点击交给本机同花顺客户端打开详情（协议见 `lib/ths.ts`）。
 *
 * 深链接生成不出来时（代码不合法 / 模板被改坏）**退化成纯文本**，
 * 而不是渲染一个点了没反应的死链。
 *
 * 港股一律纯文本：同花顺客户端的港股代码写法（`hk00700`？`00700.HK`？）
 * 没试出来，深链接功能本身也还在搁置状态 —— 按 A 股的模板硬拼一个，
 * 点下去只会跳到别的股票去，比不给链接更糟。
 */
export default function StockCode({
  code,
  name,
  market,
}: {
  code: string
  name?: string
  market?: 'cn' | 'hk'
}) {
  // 港股 code 恒为 1~5 位，A 股恒为 6 位；显式传了 market 就以它为准
  const isHk = market === 'hk' || (/^\d{1,5}$/.test(code) && market !== 'cn')
  if (isHk) {
    return <Tooltip title={`${code} 港股 · 暂不支持在同花顺中打开`}><span>{code}</span></Tooltip>
  }
  const href = thsUrl(code)
  if (!href) return <span>{code}</span>
  return (
    <Tooltip title={`在同花顺中打开${name ? ` ${name}` : ''}（${code}）`}>
      <a
        className="stock-code"
        href={href}
        // 列表行往往绑了「点行进详情」：点代码的语义是拉起同花顺，别把它冒泡出去
        onClick={(e) => e.stopPropagation()}
      >
        {code}
      </a>
    </Tooltip>
  )
}
