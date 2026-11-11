/**
 * 欢迎页 —— 邀请链接的落点（docs/06 §11）。
 *
 * ## 它在布局之外
 * 与 `Pay.tsx` 同一个理由：这个页面是被邀请人用手机打开的，套上 `<Sider>`
 * 就等于把「停止服务」按钮（后端 `/api/system/stop` 目前无鉴权）暴露给每个人。
 *
 * ## 身份是后端反查出来的
 * 点一个裸的 `<a href>` 只发 URL，浏览器**不会附带任何飞书身份** —— 所以身份在
 * 生成链接那一刻就烧进 token 了，页面这里只是拿 token 换回昵称。也因此这条链路
 * **必须一人一条**：群发一条通用的，后端面对的就是匿名请求，那要走飞书网页应用
 * OAuth（要 HTTPS 域名，见 `docs/06 §9` 的 P1 待办）。
 *
 * ## 首屏加载走 promise 链
 * 与 `Pay.tsx` / `Subs.tsx` 同一个理由：`react(set-state-in-effect)` 把 effect
 * 调用链上**同步**发生的 setState 判成违规，全站基线恰好 6 条，不能加第 7 条。
 */
import { useEffect, useState } from 'react'
import { Alert, Button, Card, Result, Skeleton, Space, Tag, Typography } from 'antd'
import { api, errMsg, type InviteInfo, type InviteResult } from '../lib/api'
import { short } from '../lib/format'

const { Title, Paragraph, Text } = Typography

/** 页脚免责声明。**全站硬口径**（README / docs/01 / App.tsx / 各页面页脚） */
const DISCLAIMER = '数据为盘中估算，仅供参考，不构成任何投资建议。'

export default function Invite({ token }: { token: string }) {
  const [info, setInfo] = useState<InviteInfo | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<InviteResult | null>(null)
  /**
   * **这次点过没有** —— 与 `done.already` 是两回事：
   * `already` = 「调用之前就已经在订阅期」（`invite.py` 幂等分支），**首次开通
   * 返回的是 `false`**。拿它当「显示成功」的开关，首次开通就永远不显示成功
   * （2026-10-14 用户实测踩中）。所以成功与否看这里。
   */
  const [activated, setActivated] = useState(false)

  useEffect(() => {
    api
      .inviteInfo(token)
      .then((r) => {
        setInfo(r)
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
  }, [token])

  const activate = () => {
    setBusy(true)
    api
      .inviteActivate(token)
      .then((r) => {
        setDone(r.result)
        setActivated(true)
        setErr(null)
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setBusy(false))
  }

  // 拿不到信息 = 链接过期或不存在，整页换成失效屏（不渲染按钮，免得点了再报一次）
  if (!info && err) {
    return (
      <div style={{ minHeight: '100vh', display: 'grid', placeItems: 'center', padding: 16 }}>
        <Result
          status="warning"
          title="邀请链接已失效"
          subTitle={err}
          extra={<Text type="secondary">向管理员重新索取一条即可，旧链接不会恢复。</Text>}
        />
      </div>
    )
  }

  const name = done?.nickname || info?.nickname || ''
  const title = name ? `欢迎，${name}` : '欢迎'
  // 已经开通就撤掉按钮 —— 后端本来也不会重复累加天数，但页面上重复给一个
  // 「订阅」按钮去点一个什么都不做的接口，是制造困惑。
  //
  // **判据是 `activated`，不是 `done.already`**：`already` 的含义是「调用之前
  // 就已经在订阅期」（`invite.py` 的幂等分支），首次开通返回的是 `false` ——
  // 拿它当开关，首次开通就永远停在按钮页。2026-10-14 用户实测踩中。
  const already = activated || (info?.active ?? false)
  const expires = done?.expires_at || info?.expires_at || ''

  return (
    <div
      style={{
        minHeight: '100vh',
        display: 'grid',
        placeItems: 'center',
        padding: 16,
        background: '#f5f5f5',
      }}
    >
      <Card style={{ maxWidth: 460, width: '100%' }}>
        {!info ? (
          <Skeleton active paragraph={{ rows: 4 }} />
        ) : (
          <>
            <Title level={3} style={{ marginTop: 0 }}>
              {title}
            </Title>
            <Paragraph type="secondary">
              你被邀请加入 <Text strong>看板</Text>
              。订阅后可用：基金实时估值、关注基金，以及交易日盘中的估值推送。
            </Paragraph>

            {already ? (
              <Alert
                type="success"
                showIcon
                title={
                  done?.already
                    ? '你已经在订阅期内'
                    : activated
                      ? '开通成功'
                      : '你已经是订阅用户'
                }
                description={
                  <Space direction="vertical" size={2}>
                    <Text>
                      到期时间：<Text strong>{short(expires)}</Text>
                    </Text>
                    <Text type="secondary">
                      邀请开通 <Tag color="success">免费</Tag>
                      {done?.already ? '，本次没有重复累加天数。' : '，无需付款。'}
                    </Text>
                  </Space>
                }
              />
            ) : (
              <>
                {err ? (
                  <Alert
                    type="error"
                    showIcon
                    title="开通失败"
                    description={err}
                    style={{ marginBottom: 12 }}
                  />
                ) : null}
                <Button
                  type="primary"
                  size="large"
                  block
                  loading={busy}
                  onClick={activate}
                >
                  立即订阅（免费）
                </Button>
                <Paragraph type="secondary" style={{ marginTop: 12, marginBottom: 0 }}>
                  点击即开通 <Text strong>{info.pro_days} 天</Text>
                  ，无需付款。
                </Paragraph>
              </>
            )}

            <Paragraph type="secondary" style={{ marginTop: 16, marginBottom: 0 }}>
              <Text type="secondary" style={{ fontSize: 12 }}>
                {DISCLAIMER}
              </Text>
            </Paragraph>
          </>
        )}
      </Card>
    </div>
  )
}
