import { useState } from 'react'
import {
  Alert,
  Button,
  Layout,
  Menu,
  Popconfirm,
  Result,
  Tooltip,
  Typography,
  theme,
  type MenuProps,
} from 'antd'
import {
  CrownOutlined,
  DashboardOutlined,
  DatabaseOutlined,
  ControlOutlined,
  ExperimentOutlined,
  FundOutlined,
  HistoryOutlined,
  LineChartOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PoweroffOutlined,
  RiseOutlined,
  AimOutlined,
  ReadOutlined,
  ScheduleOutlined,
  StarOutlined,
  ThunderboltOutlined,
} from '@ant-design/icons'
import { NavLink, Route, Routes, useLocation } from 'react-router-dom'
import DataManage from './pages/DataManage'
import Cmds from './pages/Cmds'
import FundEstimate from './pages/FundEstimate'
import Jobs from './pages/Jobs'
import Lab from './pages/Lab'
import LimitPools from './pages/LimitPools'
import MainForce from './pages/MainForce'
import News from './pages/News'
import Overview from './pages/Overview'
import Replenish from './pages/Replenish'
import SectorFlow from './pages/SectorFlow'
import StockDetail from './pages/StockDetail'
import Subs from './pages/Subs'
import Trading from './pages/Trading'
import Pay from './pages/Pay'
import Invite from './pages/Invite'
import Watchlist from './pages/Watchlist'
import Placeholder from './pages/Placeholder'
import { api, ApiError } from './lib/api'

const { Sider, Content, Header } = Layout

/**
 * 导航结构。
 *
 * 「关注股票」拆成子菜单而不是加一项顶级菜单：它和 A 股/港股共用同一套
 * 关注数据与同一个页面组件，只是过滤口径不同 —— 拆成三条子路由后，
 * 高亮跟着 pathname 走（`selectedKeys=[pathname]`），不需要额外的状态。
 *
 * 「基金估值」则是**另开的顶级菜单**：基金与个股是两套代码、两张关注表
 * （`watchlist` vs `fund_watchlist`）、两个页面，共用不了任何东西 ——
 * 硬塞进「关注股票」的子菜单反而会让 `/watchlist/cn` 那套按位数判市场的
 * 逻辑去接 6 位基金代码。
 */
const ITEMS: MenuProps['items'] = [
  { key: '/', icon: <DashboardOutlined />, label: <NavLink to="/">总览</NavLink> },
  { key: '/sector', icon: <FundOutlined />, label: <NavLink to="/sector">板块资金流</NavLink> },
  { key: '/limit', icon: <RiseOutlined />, label: <NavLink to="/limit">涨停 / 跌停</NavLink> },
  { key: '/main-force', icon: <AimOutlined />, label: <NavLink to="/main-force">主力建仓候选</NavLink> },
  {
    // 策略实验室紧跟主力建仓候选：两者是同一条链的两端 —— 那边产信号，
    // 这边把**过去的**信号拿回来重放、消融、并审计数据底子到底能支撑什么。
    key: '/lab',
    icon: <ExperimentOutlined />,
    label: <NavLink to="/lab">策略实验室</NavLink>,
  },
  {
    // 自动交易紧跟策略实验室：同一链条的下半场 —— 那边把历史信号拿回来
    // 研究，这边按同一套规则推进当下的信号/下单/成交。全程模拟盘（本地库
    // 七张表），不接实盘、不发真实下单指令，也没有任何定时器碰它。
    key: '/trading',
    icon: <ThunderboltOutlined />,
    label: <NavLink to="/trading">自动交易</NavLink>,
  },
  {
    // 父项 key 刻意与子项不同名：「全部」项的 key 就是路由 '/watchlist'，
    // 父项若也用它，antd 会把父项一并标成选中，而切到「港股」时又不标。
    key: 'section-watchlist',
    icon: <StarOutlined />,
    label: '关注股票',
    children: [
      { key: '/watchlist', label: <NavLink to="/watchlist">全部</NavLink> },
      { key: '/watchlist/cn', label: <NavLink to="/watchlist/cn">A 股</NavLink> },
      { key: '/watchlist/hk', label: <NavLink to="/watchlist/hk">港股</NavLink> },
    ],
  },
  { key: '/funds', icon: <LineChartOutlined />, label: <NavLink to="/funds">基金估值</NavLink> },
  { key: '/news', icon: <ReadOutlined />, label: <NavLink to="/news">财经新闻</NavLink> },
  { key: '/jobs', icon: <ScheduleOutlined />, label: <NavLink to="/jobs">定时任务</NavLink> },
  {
    // 指令管理紧跟在定时任务后面：两者管的是同一条飞书链路的两端 ——
    // 这页配「说什么、怎么触发」，定时任务页配「什么时候自动说」。
    key: '/cmds',
    icon: <ControlOutlined />,
    label: <NavLink to="/cmds">指令管理</NavLink>,
  },
  {
    key: '/subs',
    icon: <CrownOutlined />,
    label: <NavLink to="/subs">订阅管理</NavLink>,
  },
  { key: '/replenish', icon: <HistoryOutlined />, label: <NavLink to="/replenish">历史补录</NavLink> },
  { key: '/data', icon: <DatabaseOutlined />, label: <NavLink to="/data">数据管理</NavLink> },
]

export default function App() {
  const { token } = theme.useToken()
  // 菜单高亮跟随路由，否则新增/跳转页面时左侧始终停在默认项
  const { pathname } = useLocation()

  const [stopped, setStopped] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState<string | null>(null)

  /** 关掉前后端两个进程。成功之后这个页面就再也取不到数了，直接切到结束屏。 */
  const stopServices = async () => {
    setStopping(true)
    setStopError(null)
    try {
      await api.stopServices()
      setStopped(true)
    } catch (err) {
      // status=0 是连不上 —— 说明后端已经没了，那就当停止成功
      if (err instanceof ApiError && err.status === 0) {
        setStopped(true)
      } else {
        setStopError((err as Error).message)
      }
    } finally {
      setStopping(false)
    }
  }

  // 收银台（docs/06 §2）与邀请欢迎页（§11）：**整页脱离后台布局**。这两个页面
  // 是订阅者 / 被邀请人本人用手机打开的，把侧边栏（尤其那个调 /api/system/stop
  // 的「停止服务」按钮 —— 后端目前无鉴权）暴露给他们是现成的洞。所以不套
  // <Layout>/<Sider>。
  // 放在 `stopped` 判断**之前**：「服务已关闭」是 owner 在自己页面上点出来的
  // 状态，不该反过来把订阅者挡在收银台 / 邀请页外面。
  const m = /^\/pay\/(.+)$/.exec(pathname)
  if (m) {
    return <Pay token={decodeURIComponent(m[1])} />
  }
  const mi = /^\/invite\/(.+)$/.exec(pathname)
  if (mi) {
    return <Invite token={decodeURIComponent(mi[1])} />
  }

  if (stopped) {
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'grid',
          placeItems: 'center',
          background: token.colorBgLayout,
        }}
      >
        <Result
          status="success"
          title="服务已关闭"
          subTitle={
            <>
              前端 dev server 与后端 uvicorn 都已退出，这段时间的 16:30
              盘后任务也不会执行。需要继续使用时，双击桌面上的
              「A股看板一键启动」重新开启即可。
            </>
          }
          extra={[
            <Button key="reload" type="primary" onClick={() => window.location.reload()}>
              我已重新启动，刷新页面
            </Button>,
          ]}
        />
      </div>
    )
  }

  // hasSider 显式传：Sider 已下沉进 <AppSider>，antd 首轮的
  // `node.type === Sider` 直接子节点探测认不出来 —— 不传会先按上下
  // 堆叠渲染、等 Sider 的 effect 注册完再翻回横排，白闪一帧。
  return (
    <Layout style={{ minHeight: '100vh' }} hasSider>
      {/* 侧栏整块（含收起/展开 state）住在 AppSider 里 —— 见文件末尾的说明 */}
      <AppSider
        pathname={pathname}
        stopping={stopping}
        stopError={stopError}
        stopServices={stopServices}
      />

      <Layout>
        <Header
          style={{
            background: token.colorBgContainer,
            padding: '0 20px',
            display: 'flex',
            alignItems: 'center',
          }}
        >
          <Typography.Text type="secondary">
            盘后复盘 · 数据仅供参考，不构成投资建议
          </Typography.Text>
        </Header>

        <Content style={{ padding: 16, background: token.colorBgLayout }}>
          <Routes>
            <Route path="/" element={<Overview />} />
            <Route path="/sector" element={<SectorFlow />} />
            <Route path="/limit" element={<LimitPools />} />
            <Route path="/main-force" element={<MainForce />} />
            {/* 策略实验室：历史信号重放 + 因子消融 + 数据审计。
                只做研究与模拟，不接真实交易、不发下单指令。 */}
            <Route path="/lab" element={<Lab />} />
            {/* 自动交易（模拟盘）：策略 V1 的操作台 —— 信号/订单/成交全部落
                本地库的模拟账户，按 D+1 开盘价虚拟撮合；只有人点击才推进。 */}
            <Route path="/trading" element={<Trading />} />
            {/* 关注股票：三条子路由共用同一页面。注意是**字面路径**、没有
                `:market` 参数段，所以 Watchlist 是读 pathname 判过滤的 ——
                别改成 useParams，那样三个子菜单会渲染出完全一样的内容。 */}
            <Route path="/watchlist" element={<Watchlist />} />
            <Route path="/watchlist/cn" element={<Watchlist />} />
            <Route path="/watchlist/hk" element={<Watchlist />} />
            {/* 基金估值：手动添加 6 位基金代码 → 手动点「查询估值」才发请求，
                没有轮询。与个股是两张关注表，互不影响。 */}
            <Route path="/funds" element={<FundEstimate />} />
            <Route path="/news" element={<News />} />
            <Route path="/jobs" element={<Jobs />} />
            {/* 指令管理：内置指令的触发词/文案/快捷标记 + 自定义指令 +
                快捷指令卡片 + 两段推送抬头。只做行情呈现，不给买卖信号。 */}
            <Route path="/cmds" element={<Cmds />} />
            {/* 订阅管理：订阅者、他的关注基金、推送情况与限额。
                owner 专属页面，只做行情呈现、不给买卖信号。 */}
            <Route path="/subs" element={<Subs />} />
            {/* 数据管理：看占用 + 按数据集删除 + 清残留 + VACUUM + 清日志。
                删除默认 dry_run，真删要后端两道门（dry_run=false + confirm）。 */}
            <Route path="/replenish" element={<Replenish />} />
            <Route path="/data" element={<DataManage />} />
            {/* 个股详情：总览卡片/连板梯队点进来的落点 */}
            <Route path="/stock/:code" element={<StockDetail />} />
            <Route
              path="*"
              element={<Placeholder title="404" desc="页面不存在" />}
            />
          </Routes>
        </Content>
      </Layout>
    </Layout>
  )
}

/**
 * 左侧菜单。**收起/展开的 state 住在这里，不放在 App** —— 这是「点收起卡一下」
 * 的根因：`collapsed` 若住在 App 里，一收一展会让 App 重渲染，右侧整页跟着
 * 被拖着重画一遍；主力建仓那张几十列、上千格的矩阵表每次都在里头。
 * 下沉之后收起只重画侧栏子树，右侧一个节点都不动（输入框卡顿是同一类问题，
 * 见 MainForce 的 `MatrixSearch` —— 即时文本关进子组件）。
 */
function AppSider({
  pathname,
  stopping,
  stopError,
  stopServices,
}: {
  pathname: string
  stopping: boolean
  stopError: string | null
  stopServices: () => Promise<void>
}) {
  // 关注股票子菜单的展开态。**默认就展开**，而且不跟路由抢控制权：
  // 「全部 / A 股 / 港股」是这次新加的入口，默认收起来等于把它藏了；
  // 之后开合完全听用户的（antd 非受控不行 —— selectedKeys 变化不会自动
  // 展开父项，deep link 进 /watchlist/hk 时会选中一个看不见的子项）。
  const [openKeys, setOpenKeys] = useState<string[]>(['section-watchlist'])

  // 左侧菜单可收起（一直展开要占掉 216px）。收起后子菜单只在悬停时弹出。
  const [collapsed, setCollapsed] = useState(false)

  /**
   * 收起 / 展开左侧菜单。**展开时必须自己补回 openKeys**：收起后 rc-menu 切到
   * vertical 模式（顺带把 openKeys 清空），展开时它只恢复自己的内部缓存、
   * **不回调 onOpenChange** —— 受控 openKeys 得在这里补，否则再展开后
   * 「关注股票」子菜单是收着的，deep link 进 /watchlist/hk 时高亮会落在
   * 一个看不见的子项上（见上面 openKeys 那段注释）。
   */
  const handleCollapse = (c: boolean) => {
    setCollapsed(c)
    setOpenKeys(c ? [] : ['section-watchlist'])
  }

  return (
    <Sider
      theme="dark"
      width={216}
      // 收起后只留 56px 图标轨。trigger 置 null：antd 默认触发条钉在 Sider
      // 底部，会和我们这个 sticky + height:100vh 的自定义页脚抢位置 ——
      // 收起按钮改放在系统名行的右上角（见上方 .sider-fold 角标）。
      collapsedWidth={56}
      breakpoint="lg"
      collapsible
      collapsed={collapsed}
      onCollapse={handleCollapse}
      trigger={null}
      // 内容页很高时 sider 会被拉成整篇文档那么高，菜单和「停止服务」
      // 就跟着滚出视野了；钉成视口高度 + sticky，菜单和按钮始终可见
      style={{ position: 'sticky', top: 0, height: '100vh', overflow: 'auto' }}
    >
      <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
        {/* 系统名：这里一处、邀请页文案一处，浏览器标签页名在 index.html */}
        {/* 收起角标钉在这一行的**右上角**（样式见 index.css 的 .sider-fold）：
            收起后 56px 轨里并排放不下「看板 + 角标」，标题让位隐藏 ——
            收起态角标是唯一的展开入口，所以挂 Tooltip 兜底 */}
        <div
          style={{
            height: 56,
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: '#fff',
            fontWeight: 600,
            letterSpacing: 1,
          }}
        >
          {collapsed ? null : '看板'}
          <Tooltip title={collapsed ? '展开菜单' : '收起菜单'} placement="right">
            <button
              type="button"
              className="sider-fold"
              aria-label={collapsed ? '展开菜单' : '收起菜单'}
              onClick={() => setCollapsed(!collapsed)}
              style={{ position: 'absolute', top: '50%', right: 6, transform: 'translateY(-50%)' }}
            >
              {collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            </button>
          </Tooltip>
        </div>
        <Menu
          theme="dark"
          mode="inline"
          selectedKeys={[pathname]}
          openKeys={openKeys}
          onOpenChange={setOpenKeys}
          items={ITEMS}
          style={{ flex: 1 }}
        />
        <div style={{ padding: collapsed ? 6 : 12 }}>
          <Popconfirm
            title="停止看板服务？"
            description="结束前端与后端两个进程，页面将无法再取数；之后双击桌面「A股看板一键启动」可重新开启。"
            okText="停止"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => void stopServices()}
          >
            {/* 收起态只留图标（文字在 56px 轨里装不下），悬停提示交给 Popconfirm */}
            <Button danger block loading={stopping} icon={<PoweroffOutlined />}>
              {collapsed ? null : '停止服务'}
            </Button>
          </Popconfirm>
          {stopError && (
            <Alert
              type="error"
              showIcon
              title={stopError}
              style={{ marginTop: 8, fontSize: 12 }}
            />
          )}
        </div>
      </div>
    </Sider>
  )
}
