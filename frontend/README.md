# 前端

React 19 + TypeScript + Vite 8 + Ant Design 6 + ECharts 6

启动与构建说明见仓库根目录 [`README.md`](../README.md)。

```powershell
npm install
npm run dev     # http://localhost:5173，/api 代理到 127.0.0.1:8000
npm run build   # tsc -b && vite build
```

## 目录

| 路径 | 职责 |
|---|---|
| `src/lib/api.ts` | 后端 API 客户端，统一错误处理与类型定义 |
| `src/lib/useEChart.ts` | ECharts 按需注册 + 响应式容器 hook；可选第二个参数 `events` 挂图表事件（如点柱子下钻），每个 effect 周期绑一次、卸载时 `off` 掉，调用方要用 `useMemo` 固定引用 |
| `src/lib/format.ts` | 共用展示格式化：`YYYYMMDD` 日期、相对时间、任务状态标签（总览与定时任务两页共用同一口径）；**涨跌配色 `signType` / `signColor`，全站唯一口径** |
| `src/lib/ths.ts` | 同花顺深链接模板与拼装（`thsUrl(code)`），协议见下方「点击代码打开同花顺」 |
| `src/lib/live.ts` | 实时行情用的**交易时段判定**（A 股 09:30-11:30+13:00-15:00、港股 09:30-12:00+13:00-16:00 → 未开盘/交易中/午间休市/已收盘）。只看时钟不看日历 —— 休市日由报价自己停在最后一笔，不把「今天休市」渲染成「数据拿不到」 |
| `src/pages/Overview.tsx` | 总览页（`/`）：统计卡 + 连板梯队金字塔 + 板块 Top/Bottom + 候选 Top5 + 任务与新鲜度。**统计卡带 `to` 就整卡可点**（`.stat-link`），梯队柱子与 Top5 整行可点，都是下钻入口，见下方「下钻与详情页」 |
| `src/pages/Jobs.tsx` | 定时任务（`/jobs`）：调度开关（持久化）+ 手动执行 / 强制重跑 + **跑完通知（飞书 webhook：地址与签名校验密钥存后端 SQLite、开关、**推送内容勾选框**（后端 `section_defs` 下发，勾中即存 `notify_sections`）、测试发送，正文回显在 `.notify-preview` 预览框里）** + **飞书指令（入站长连接：App ID / Secret / 白名单 open_id、连接状态与上次收消息时间、「试一下」跑分发但不执行副作用）** + 运行历史 |
| `src/pages/SectorFlow.tsx` | 板块资金流：净流入双向条形图 + 排行表 |
| `src/pages/LimitPools.tsx` | 涨停 / 炸板 / 跌停 / 强势股四池，**同时是总览卡片与梯队柱子的「清单详情页」**：读 URL 参数 `?tab=` 预选池、`?boards=` 过滤连板数（带清除筛选），整行可点进个股详情。涨停池尾部是后端回看历史补上的**断板行**（`broken`）：连板数冻结、行情列一律「—」、挂灰 Tag「断 N 天」+ 最后上榜日，整行压灰；所有排序器外面套了一层 `tail()`，断板行**恒排末尾**且互相并列（没有当天行情可比，放任排序器回 `undefined - undefined` 会让顺序每次点都乱跳） |
| `src/pages/StockDetail.tsx` | 个股详情（`/stock/:code`）：近 90 日 K 线（MA5/MA20 + 成交量 + dataZoom）+ 近 10 日主力净流入柱与逐日明细 + 档案（命中池 / 打分 / 关注状态）+ 顶部关注按钮；各块独立兜错，缺数据只降级那一格 |
| `src/pages/MainForce.tsx` | 主力建仓候选：六维度分项 + 入选理由 + 近5日总流入（点数字看逐日弹框）+ 降级提示 + 行首星标关注 |
| `src/pages/Watchlist.tsx` | 关注股票：K线（建仓日标注）+ 区间表现 + 大盘对比 + 当时打分快照；页内两个 Tab「日线复盘 / 实时行情」。**内容故意放在 Tabs 外面用三元切换**（塞进 `items[].children` 的话，antd 会把切走的 pane 留在 DOM 里隐藏着，`LiveBoard` 的轮询就会空转），`extra`（区间表现/刷新列表）在实时视图下隐藏 |
| `src/pages/FundEstimate.tsx` | **基金估值**（`/funds`，左侧新顶级菜单）：手动添加 6 位基金代码（**纯本地写库、不联网**）→ 点「查询估值」才发一次 `/api/fund-estimate`，**没有轮询、没有自动刷新**。表格行**以关注列表为准**（删一只它的估值行跟着消失，新添加的先出一行空的），估值结果按代码并进来；两条链能给的字段不一样 —— 官方估值有估算净值、持仓穿透只有涨跌幅 + 覆盖率，缺的列就是「—」，**绝不填默认值**；降级原因原样摊成 warning 条，`stale` 打「非今日」标。**涨跌幅只有一列，按时段切换**（后端算好 `display_pct`/`display_kind`/`display_reason`，前端只标「估算 / 实际」Tag，悬停摊出两个数各自的时点）：交易中→估算、盘前→前一交易日净值、收盘后机构净值出了→实际、没出→估算。首屏 `loadList` 写成 promise 链 —— effect 里同步 setState 会亮 `react(set-state-in-effect)`，全站基线 6 条不能加第 7 条；查询用 `seq` 丢迟到的旧响应。页面下半部分是 **T+1 误差复盘**：`loadReview` 同样写成 promise 链，与列表一起在首屏拉（`/api/fund-review` **只读本地 SQLite、零网络**，不违反「不点查询就不发包」）；表格按 **(基金 × 数据来源)** 分组，列是样本数 / 平均误差 / 平均绝对误差 / 最坏高估 / 最坏低估 / 覆盖率 / 最近一次，误差单位写 **pp**（涨跌幅之差不是百分比），样本 < 5 打「样本少」标，口径说明与异常计数原样摊给用户 |
| `src/pages/News.tsx` | 财经新闻（`/news`）：分类横排（Segmented 带条目数）→ 下方条目列表（标题 + 来源/时间/分类 + 热度进度条，整行可点、键盘 Enter 也可开）→ 右侧抽屉看正文与**热度四项构成**；数据一次拉全量、切分类纯本地过滤，避免每点一次就打源站；六源失败态、演示数据态、部分源降级态各有提示。热度配色用灰-蓝-金-橙，**刻意避开涨跌的红绿** |
| `src/pages/Placeholder.tsx` | 通用占位组件，目前只用作 404 页 |
| `src/components/StockCode.tsx` | 股票代码组件：渲染成可点的 `<a href="hexin://…">`，点它用本机同花顺打开该股详情；深链接拼不出来时退化成纯文本（不留死链）。点击时**阻止冒泡**，免得整行的「进详情」跟着触发。被多处使用：涨停/炸板/跌停/强势四池、主力建仓列表、总览 Top5、关注列表（含详情标题）、个股详情页标题 |
| `src/components/LiveBoard.tsx` | **实时行情视图**（关注页第二个 Tab）：3/5/10 秒可调轮询 `/api/realtime` + 盘面涨跌家数与合计 + 12 列可排序表格（点行选中）+ 选中股的分时图与当日资金流。**null 一律显示「—」，绝不填默认值**（填 0 会把「校验没过」和「真的没成交」变成一回事）；后端的降级原因原样摊成 warning 条，不做静默吞错；迟到的旧响应靠 `seq` 丢弃 |
| `src/App.tsx` | 路由与整体布局；侧栏菜单做成 **sticky + 视口高度**（内容页很高时菜单不会被滚走），菜单**最下方**挂危险色「停止服务」按钮：Popconfirm 确认 → `POST /api/system/stop` → 切到「服务已关闭」结束屏（连接失败 status=0 也按已停止处理，因为此时服务确实没了）；端点返回 409（任务在跑）则只提示不切屏 |

## 涨跌配色口径

**绿跌红涨**（A股惯例，与 K 线一致），全站统一走 `src/lib/format.ts` 的两个函数，新页面别再手写三元：

- `signType(v)` → 给 `<Text type>` 用：`>= 0` 红（`danger`）、`< 0` 绿（`success`）
- `signColor(v, token)` → 给内联 `style` / ECharts `itemStyle.color` 用：`>= 0` 红、`< 0` 绿

坑：antd 的语义色是反的 —— `success` 是绿、`error` 是红，所以直接写 `type={v >= 0 ? 'success' : 'danger'}` 会得到「绿涨红跌」。历史上各页各写一套，导致同一天的净流入表格里是绿的、柱状图里是红的，现已全部收敛（含涨停池/跌停池原本硬编码的 `type="success"` / `type="danger"`、板块资金流图表的硬编码 hex）。

与涨跌无关的颜色不走这套：任务状态（成功=绿）、六维度分数色阶、连板 Tag 分层色、关注页最大回撤。

## 点击代码打开同花顺

列表里的股票代码都渲染成 `src/components/StockCode.tsx`，点击后在**本机同花顺客户端**里打开该股详情。

**原理**：同花顺（9.50.70，`D:\Program Files (x86)\同花顺\hexin.exe`）注册了 URL 协议 `hexin://`，注册表命令就是 `"hexin.exe" %1`。所以做法是最朴素的 `<a href="hexin://…">` —— 浏览器交给系统、系统交给同花顺：

- **前端不调后端、不创建任何进程**，因此不需要「开进程」这种危险接口；
- 首次点击浏览器会弹「要打开 同花顺 吗？」，勾**始终允许**后不再提示；
- 代码列在四池（涨停/炸板/跌停/强势）、主力建仓候选、总览 Top5、关注列表（含详情标题）四处，配色是主题蓝 + 虚线下划线，刻意避开红绿涨跌色。

**深链接模板**（`src/lib/ths.ts` 的 `DEFAULT_TEMPLATE`，当前 `hexin://quote?code={code}`）：

跳哪只股票的 URL 写法没有公开文档，二进制里也只有 CEF 的 scheme 白名单、没有格式串，所以模板做成了可替换的，**试法**：

| 方式 | 操作 |
|---|---|
| 改源码 | 改 `src/lib/ths.ts` 的 `DEFAULT_TEMPLATE`，vite 热更，连刷新都不用 |
| 控制台 | `localStorage.setItem('ths.template', 'hexin://stock?code={code}')` → 刷新；`localStorage.removeItem('ths.template')` 恢复默认 |

候选写法：`hexin://quote?code={code}`（当前默认）· `hexin://stock?code={code}` · `hexin://{code}`（模板不写 `{code}` 时会自动把代码拼在后面）。

模板拼不出合法协议（或代码不是 6~12 位字母数字）时，`StockCode` 退化成纯文本，不会留下点了没反应的死链。

## 下钻与详情页

两层页面，**卡片和柱子共用第一层**：

| 入口 | 跳到 |
|---|---|
| 总览统计卡 涨停 / 炸板 / 跌停 / 强势股 | `/limit?tab=zt`（等价池，`zb` / `dt` / `strong` 同理） |
| 总览「最高连板」卡 | `/stock/{code}`（龙头个股详情） |
| 总览「已关注」卡 | `/watchlist` |
| 总览连板梯队柱子 | `/limit?tab=zt&boards={N}` —— 该板级的个股清单 |
| 四池清单 / 总览 Top5 整行 | `/stock/{code}` 个股详情 |

- **`/limit` 只认 URL 参数**：`useSearchParams` 双向绑定 `activeKey`，改筛选只改参数不改状态，
  所以来回跳不丢上下文；`?boards=` 只作用于涨停池，切换 tab 会顺手把它清掉。
- **整行可点**是 `Table` 的 `onRow`，代码列（`StockCode`）里 `stopPropagation`，
  于是「点代码开同花顺」和「点行进详情」互不打架。
- **`/stock/:code` 的状态跟 code 走**：外层取 `useParams`，内层组件用 `key={code}` 重建，
  换股就是换组件实例，不必在 effect 里同步 `setState` 清空（那会被 `set-state-in-effect` 抓）。
- 拉数用 promise 链 / `await` 后再 setState，effect 里没有同步 setState，
  所以 `oxlint` 仍是 6 条既有警告、0 新增。

## antd v6 弃用 prop 对照

控制台在 dev 下会把弃用告警打进 `error` 级别（`Warning: [antd: ...]`），**全站应保持 0 条**。
新代码照下表写；本仓已全部清理过一轮：

| 旧写法（会告警） | v6 写法 |
|---|---|
| `<Alert message={…} />` | `<Alert title={…} />`（**不能**改用 children —— `Alert` 没有 children prop） |
| `<Alert closable onClose={…} />` | `<Alert closable={{ onClose: … }} />` |
| `<Space direction="vertical">` | `<Space orientation="vertical">` |
| `<Statistic valueStyle={{ fontSize: 18 }} />` | `<Statistic styles={{ content: { fontSize: 18 } }} />` |
| `<Spin tip="…">` | `<Spin description="…">`（**`description` 是新名字**，别写反） |

顺带的两处同类坑：

- **ECharts 是按需注册的**（`src/lib/useEChart.ts` 的 `echarts.use([...])`）。option 里用到没注册的组件
  会被静默丢弃并报 `Component xxx is used but not imported` —— 板块资金流的 `title` 就吃过这个亏，
  标题一直没渲染。加新 option 组件时记得同步注册。
- 校验方式：浏览器控制台 `level=error` 读一遍（或代码里 `browser.console` 取 error 级消息）。
