import { AutoComplete, Tag, Typography } from 'antd'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
} from 'react'
import { api, type SearchItem } from '../lib/api'

/**
 * 添加关注前的模糊搜索框（股票页与基金页共用）。
 *
 * ## 联网时机：防抖 400ms
 *
 * 与全站「点一次发一次」的节奏不同 —— 这是**用户主动选择的例外**：输入停顿
 * 即出候选，比每敲一个字按一次回车顺手。代价是打字过程中会连发外部请求，
 * 所以三道防线一个都不能少：
 *
 *   1. **防抖** 400ms —— 连续敲字只在停顿后真正发一次；
 *   2. **后端缓存** 10 分钟 —— 加完下一只又回头敲同一个词，不再打上游；
 *   3. **序号作废** —— 慢的那个响应后到时，`seq` 已经变了，直接丢弃，
 *      否则会把上一个词的候选盖到当前输入上（这是 typeahead 最经典的 bug）。
 *
 * ## 文本是受控的，搜索结果是内部的
 *
 * **文本归父组件**（`value` / `onChange`）：表单里那个「添加」按钮要能读到框里
 * 敲了什么，否则手输代码 + 点按钮这条路会读到一个空串。**候选归本组件** ——
 * 它纯粹是搜索词的派生物，抬上去只会逼父组件再管一份没人关心的状态。
 *
 * 成功添加后清空文本的责任在**父组件**（`addOne` 在落库成功后 `setAddCode('')`），
 * 这里不越权代劳：失败时该留着用户敲的字，否则报错一弹、框里却空了，
 * 人根本不知道是哪个代码没加上。
 *
 * ## 为什么所有 setState 都在 setTimeout / Promise 里
 *
 * `react(set-state-in-effect)`（本仓库只保留既有 6 条，新代码不得新增）盯的
 * 是 **effect 主体里同步调用 setState**。防抖的清空与置忙必然发生在这条
 * 路上，所以它们一律放进 timer 回调或 `.then` —— 那是异步的，规则不管。
 * effect 主体里只做两件事：记序号、排 timer。
 *
 * ## 选中即添加，以及回车
 *
 * `onSelect` 把整条候选交回父组件：搜索框的唯一用途就是「找到并加进去」。
 *
 * 同一个框**也是手输代码的入口**（`onEnter`）—— 这样表单里不必再并排一个
 * 只认 6 位数字的框。两者的分工是：有高亮候选时 Enter 由 antd 走 `onSelect`，
 * 没有候选时轮到外层的 `handleKeyDown`。
 *
 * **Enter 不能靠 `onPressEnter`** —— antd 的 `AutoComplete` 基于 Select，那个
 * prop 根本不在它的类型里（tsc 当场拒绝）。改成在外层 `div` 接 `onKeyDown`，
 * 事件从 input 一路冒泡上来，antd 的内部处理先跑。**但不能赌这个次序**
 * （不同版本实现不同，赌错就是同一个代码添加两遍），所以 `commit` 打一个
 * 500ms 时间戳：**只放在回车那一侧**去检查 —— antd 先处理、我们后到，
 * 就被挡下；反过来（antd 没消费 Enter）则正常走 `onEnter`。
 *
 * 反过来那一侧（`handleSelect`）**不能加同样的守卫**：它挡不住任何真实风险
 * （一次点击 / 一次 Enter 只会触发一次 `onSelect`），却会把 500ms 内的第二次
 * 正常点选**静默吞掉** —— 用户看得见的「点了没反应」。
 */
const DEBOUNCE_MS = 400
const COMMIT_GUARD_MS = 500

export default function SearchPick({
  kind,
  market,
  value,
  onChange,
  placeholder,
  width = 240,
  taken = [],
  disabled = false,
  onPick,
  onEnter,
}: {
  /** fund=天天基金，stock=腾讯（A 股 + 港股） */
  kind: 'fund' | 'stock'
  /** 仅股票：子菜单限定市场时必须传，否则会列出本页加不进去的候选 */
  market?: 'cn' | 'hk'
  /** 受控文本：由父组件持有，「添加」按钮读的就是它 */
  value: string
  onChange: (v: string) => void
  placeholder: string
  width?: number
  /** 已在关注列表里的 code —— 下拉里标出来，别让人白点一次 */
  taken?: string[]
  disabled?: boolean
  /** 选中候选。添加由父组件做（它才知道重复判定与错误展示） */
  onPick: (item: SearchItem) => void
  /** 回车且**没有**候选可选中时触发 —— 交回原始文本，由父组件校验是不是代码 */
  onEnter?: (text: string) => void
}) {
  const [items, setItems] = useState<SearchItem[]>([])
  const [busy, setBusy] = useState(false)
  /**
   * 最后一次**跑完**的搜索词。少了它就分不清「还没开始搜」和「搜了但没有」——
   * 输入的第一个字符会在防抖那 400ms 里白闪一句「没有匹配」，看着像功能坏了。
   */
  const [settled, setSettled] = useState('')
  /** 单调序号：每轮输入作废上一轮在途请求，晚到的响应直接丢 */
  const seq = useRef(0)
  /** 最近一次「已当作选中处理」的时刻，见文件头的双入口防重说明 */
  const lastPickAt = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    const term = value.trim()
    const timer = setTimeout(() => {
      if (!term) {
        setItems([])
        setSettled('')
        setBusy(false)
        return
      }
      setBusy(true)
      api
        .search(term, kind, market)
        .then((res) => {
          if (mine !== seq.current) return
          setItems(res.items)
          setSettled(term)
        })
        .catch(() => {
          // 搜索取不到就当没有候选 —— **绝不因此拦住添加**：手贴 6 位代码
          // 照样能加，把上游抖动渲染成一个红错误只会让人以为功能坏了。
          // 提示语也刻意不区分「确实没有」和「源挂了」：对用户来说下一步
          // 都是同一件事（换个词，或直接输代码），分叉只会制造困惑。
          if (mine !== seq.current) return
          setItems([])
          setSettled(term)
        })
        .finally(() => {
          if (mine === seq.current) setBusy(false)
        })
    }, DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [value, kind, market])

  const takenSet = useMemo(() => new Set(taken), [taken])

  const options = useMemo(() => {
    const term = value.trim()
    const rows = [...items]
    // 输入本身就是一个完整 code 时把它顶到最前 —— 这样「回车」和「用户看到的
    // 高亮行」永远指同一个对象，antd 那边无论高不高亮第一项都加不错东西。
    // 稳定排序，code 相同时保持上游给的顺序（模糊匹配的相关度）。
    rows.sort((a, b) => Number(b.code === term) - Number(a.code === term))
    return rows.map((it) => {
      const dup = takenSet.has(it.code)
      return {
        value: it.code,
        disabled: dup,
        label: (
          <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <Typography.Text code>{it.code}</Typography.Text>
            <Typography.Text ellipsis style={{ maxWidth: 180 }}>
              {it.name}
            </Typography.Text>
            {/* 基金的次要信息专门用来区分 A/C 份额 —— 选错要多付销售服务费 */}
            {kind === 'fund' && it.type ? (
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                {it.type}
                {it.company ? ` · ${it.company}` : ''}
              </Typography.Text>
            ) : null}
            {kind === 'stock' && it.market === 'hk' ? (
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>
                港股
              </Tag>
            ) : null}
            {dup ? (
              <Tag color="default" style={{ marginInlineEnd: 0 }}>
                已添加
              </Tag>
            ) : null}
          </span>
        ),
      }
    })
  }, [items, kind, takenSet, value])

  /** 两个入口（下拉点选 / 回车）唯一的收口。打时间戳供对方检查。 */
  const commit = useCallback(
    (hit: SearchItem) => {
      lastPickAt.current = Date.now()
      onPick(hit)
    },
    [onPick],
  )

  /**
   * **这里不设时间戳守卫** —— 那道守卫只该挡「同一次 Enter 被处理两遍」，
   * 而一次点击 / 一次 Enter 本来就只会触发一次 `onSelect`，加了挡不住任何
   * 真实风险，却会把 500ms 内的第二次正常点选**静默吞掉**：用户看得见的
   * 「点了没反应」，比多一次请求糟得多。
   */
  const handleSelect = useCallback(
    (_value: string) => {
      const hit = items.find((i) => i.code === _value)
      if (!hit || takenSet.has(hit.code)) return
      commit(hit)
    },
    [items, takenSet, commit],
  )

  const shown = value.trim()
  /** 只有「这一轮真的跑完了、而且一个都没给」才提示 —— 见 `settled` 的说明 */
  const noMatch = shown !== '' && shown === settled && !busy && items.length === 0

  /**
   * Enter 不能靠 `onPressEnter` —— antd 的 AutoComplete 基于 Select，那个 prop
   * 根本不在它的类型里（tsc 会当场拒绝）。改成在**外层**接键盘事件：它从 input
   * 一路冒泡上来，antd 的内部处理在我们之前跑，所以两种情况都对得上 ——
   *
   *   antd 消费了 Enter（有高亮候选）→ 先走 `onSelect` → `commit` 打了时间戳 →
   *       冒泡到这儿被守卫挡下，不会加第二遍；
   *   antd 没消费（没有候选）        → 落到 `onEnter`，手输代码这条路走通。
   */
  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (e.key !== 'Enter' || e.repeat) return
      if (Date.now() - lastPickAt.current < COMMIT_GUARD_MS) return
      const term = value.trim()
      if (!term) return
      // 输入本身就是一个可加的候选 code —— 无论 antd 这一轮有没有把高亮项
      // 消费掉，都按「选中它」处理。这样手敲 161725 回车与点下拉里的 161725
      // 走同一条路，也不会出现「框里是 161725、实际加进去的是别的」这种错位。
      const exact = items.find((i) => i.code === term && !takenSet.has(i.code))
      if (exact) {
        commit(exact)
        return
      }
      onEnter?.(term)
    },
    [value, items, takenSet, commit, onEnter],
  )

  return (
    <div onKeyDown={handleKeyDown}>
      <AutoComplete
        style={{ width }}
        size="small"
        value={value}
        disabled={disabled}
        placeholder={placeholder}
        options={options}
        onChange={onChange}
        onSelect={handleSelect}
        notFoundContent={
          noMatch ? (
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              没搜到匹配的{kind === 'fund' ? '基金' : '股票'}，可直接输代码回车
            </Typography.Text>
          ) : null
        }
        allowClear
      />
    </div>
  )
}
