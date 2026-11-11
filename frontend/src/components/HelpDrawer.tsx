import { Drawer } from 'antd'
import type { ReactNode } from 'react'

/**
 * 页面的「操作说明」抽屉 —— 从**右侧**滑出（与站内新闻详情抽屉同一套手势）。
 *
 * 受控组件：开关状态由页面自己那两行持有（setState 落在 onClick 里，
 * 不碰 effect，也就不会给 oxlint 的 `set-state-in-effect` 基线添条数）。
 * 正文是页面里的静态 JSX —— 页面长了什么功能，说明就写在同一份文件里，
 * 改功能时一眼看得见要同步的文案（抽屉与实际控件一字不差是本页的验收口径）。
 *
 * 零请求：正文只是文本与标签，打开抽屉不发任何接口调用。
 */
export function HelpDrawer({
  title,
  open,
  onClose,
  size = 640,
  children,
}: {
  title: string
  open: boolean
  onClose: () => void
  /** 抽屉宽度（antd 6 的 Drawer 用 size 收宽度） */
  size?: number
  children: ReactNode
}) {
  return (
    <Drawer title={title} open={open} onClose={onClose} placement="right" size={size}>
      {children}
    </Drawer>
  )
}
