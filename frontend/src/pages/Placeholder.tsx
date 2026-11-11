import { Alert, Card, Result, Typography } from 'antd'

const { Paragraph } = Typography

/** 尚未实现的模块占位页，明确标注里程碑，避免看起来像「坏了」。 */
export default function Placeholder({ title, desc }: { title: string; desc: string }) {
  return (
    <Card>
      <Result status="info" title={title} subTitle={desc}>
        <Paragraph type="secondary">
          里程碑进度：M1 骨架 ✅ → M2 板块资金流 ✅ → M3 涨跌停 ✅ → M4 主力建仓 ✅ →
          M5 定时任务 ✅ → M6 个股详情 ✅
        </Paragraph>
        <Alert
          type="warning"
          showIcon
          title="数据免责声明"
          description="本项目仅用于数据整理与可视化研究，不提供投资建议，不构成任何买卖依据。"
        />
      </Result>
    </Card>
  )
}
