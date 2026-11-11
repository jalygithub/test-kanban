/**
 * 「指令管理」页 —— 内置指令的可改项、自定义指令、快捷指令卡片、推送文案模板。
 *
 * ## 三层真相只有一个出处
 * 内置指令的默认值住在后端 `cmdcfg.BUILTIN`；库里**只存用户改过的**，所以
 * 「恢复默认」= 删掉那一行。页面上永远只有一个真相，不会出现「改了又弹回来」
 * 这种两边打架的状态。
 *
 * ## 权限在这一页改不动
 * 内置指令的闸是后端 `_ACCESS` 代码常量，页面上的 access 对内置**只读**；
 * 自定义指令只给 sub / owner 两档（后端 400 拦着）。默认拒的口径不因这页而变。
 *
 * ## 带参数的指令不给勾「快捷」
 * 按钮点下去发出的是一整句**无参**触发词 —— 「查询 600825」没法点一下跑。
 * 后端不拦，所以在开关上就地 `disabled`，免得配完点下去才发现跑不了。
 *
 * ## 首屏加载必须是 promise 链
 * `react(set-state-in-effect)` 会把 effect 里**同步**发生的 setState 判成违规，
 * 全站基线恰好 6 条，新页面不能加第 7 条。所以 `load` 的第一个动作就是
 * `api.cmdList()` 这个 promise 本身，所有 setState 都落在 `.then/.catch/.finally`
 * （事件处理器里同步 setState 不受影响，规则只盯 effect）。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Space,
  Spin,
  Switch,
  Table,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined, ReloadOutlined, SendOutlined } from '@ant-design/icons'
import {
  api,
  errMsg,
  type CardElement,
  type CmdItem,
  type CmdTpl,
  type CmdListResponse,
  type QuickCard,
} from '../lib/api'

const { Text, Paragraph } = Typography

/** 权限档。内置的真闸在后端代码，这里只把**实际生效的那一档**显示出来。 */
function accessTag(a: string) {
  if (a === 'any') return <Tag color="cyan">任何人</Tag>
  if (a === 'sub') return <Tag color="blue">订阅者</Tag>
  return <Tag color="gold">仅本人</Tag>
}

/** 空 alias 与「只发主触发词」是同一件事，别在页面上留个空逗号。 */
const aliasText = (a: string) => a || '—'

const blank = (): CmdItem => ({
  key: '',
  kind: 'custom',
  label: '',
  text: '',
  alias: '',
  access: 'owner',
  reply: '',
  target: '',
  param: '',
  desc: '',
  enabled: 1,
  quick: 0,
  sort: 0,
  default_text: '',
  default_alias: '',
  edited: false,
})

/** 只取后端要的那几个字段 —— 整条 `CmdItem` 里还有 `default_*` / `edited`
 * 这类**派生值**，原样回传会把「页面算的」当成「用户填的」。 */
function payload(r: CmdItem, change: Partial<CmdItem>) {
  const merged = { ...r, ...change }
  return {
    key: merged.key,
    kind: merged.kind,
    label: merged.label,
    text: merged.text,
    alias: merged.alias,
    access: merged.access,
    reply: merged.reply,
    target: merged.target,
    enabled: !!merged.enabled,
    quick: !!merged.quick,
    sort: Number(merged.sort) || 0,
  }
}

/** 按钮埋在 `column_set` 里（一行 3 个），顶层只看得到分栏，得往下钻一层。 */
function walkBtns(els: CardElement[]): CardElement[] {
  const out: CardElement[] = []
  for (const e of els) {
    if (e.tag === 'button') out.push(e)
    else if (e.tag === 'column_set')
      for (const c of e.columns ?? []) out.push(...walkBtns(c.elements ?? []))
  }
  return out
}

/** 卡片预览：把 JSON 折成可读的几行，别甩一坨。 */
function cardLines(card: QuickCard | null) {
  if (!card) return ''
  const els = card.body?.elements ?? []
  const btns = walkBtns(els)
  const rows = els
    .filter((e) => e.tag === 'column_set')
    .map((e) => (e.columns ?? []).length)
  const head = [
    `schema: ${card.schema}`,
    `标题: ${card.header?.title?.content ?? '—'}`,
    `按钮 ${btns.length} 个，排成一行 3 个（各行为 ${rows.join('/') || '—'} 个）:`,
  ]
  const body = btns.map((b, i) => {
    const v = b.behaviors?.[0]?.value ?? {}
    return `  ${i + 1}. ${b.text?.content ?? ''}  →  cmd=${v.cmd ?? ''}`
  })
  if (!btns.length) body.push('  （没有按钮 —— 说明还没勾任何「进快捷卡」）')
  return [...head, ...body].join('\n')
}

// ------------------------------------------------------------ 快捷指令卡片

function QuickCardCard({ items, onErr }: { items: CmdItem[]; onErr: (s: string) => void }) {
  const [card, setCard] = useState<QuickCard | null>(null)
  const [sending, setSending] = useState(false)
  const [msg, setMsg] = useState('')

  // 预览是纯前端拼不出来的（按钮集合在后端），所以照旧走 promise 链。
  const preview = useCallback(() => {
    api
      .quickCardPreview()
      .then((r) => setCard(r.card))
      .catch((e: unknown) => onErr(errMsg(e)))
  }, [onErr])

  useEffect(() => {
    preview()
  }, [preview, items])

  const send = () => {
    setSending(true)
    setMsg('')
    api
      .quickCardSend()
      .then((r) => {
        setMsg(`已发到 ${r.to} 的单聊。去飞书点一个按钮试试。`)
        onErr('')
      })
      .catch((e: unknown) => onErr(errMsg(e)))
      .finally(() => setSending(false))
  }

  const quickCount = items.filter((r) => r.quick && r.enabled).length

  return (
    <Card
      title="快捷指令卡片"
      extra={
        <Space>
          <Button size="small" icon={<ReloadOutlined />} onClick={preview}>
            刷新预览
          </Button>
          <Button
            type="primary"
            size="small"
            icon={<SendOutlined />}
            loading={sending}
            disabled={quickCount === 0}
            onClick={send}
          >
            发我一张
          </Button>
        </Space>
      }
    >
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        卡片上只放勾了「进快捷卡」且没停用的 {quickCount} 条。
        <Text strong>点按钮 = 替你把那句触发词发出去</Text>
        ，回复照常回到这个会话。带参数的指令（查询 / 基金 / 关注）点一下跑不了，开关上不让勾。
      </Paragraph>
      <Alert
        type="warning"
        showIcon
        title="要点亮这条链，三样缺一不可"
        description={
          <span>
            ① 飞书开发者后台 → 事件与回调 → <Text code>回调配置</Text> → 订阅方式选
            「使用长连接接收回调」；② 同处添加<Text code>新版</Text>卡片回传交互回调{' '}
            <Text code>card.action.trigger</Text>（旧版只有 HTTP 回调，我们没有公网）；
            ③ 后端已内置的 lark-oapi CARD 帧补丁（不用你动）。任何一步没做，点按钮都是
            200340「出错了，请稍后重试」。
          </span>
        }
        style={{ marginBottom: 12 }}
      />
      {msg ? (
        <Alert
          type="success"
          showIcon
          title="发送结果"
          description={msg}
          style={{ marginBottom: 12 }}
        />
      ) : null}
      <pre className="notify-preview">{cardLines(card)}</pre>
    </Card>
  )
}

// ------------------------------------------------------------ 推送文案模板

function TemplatesCard({
  templates,
  onSaved,
  onErr,
}: {
  templates: Record<string, CmdTpl>
  onSaved: () => void
  onErr: (s: string) => void
}) {
  // 不在渲染里补初值（那也是 setState）：值没改过时直接读 `tpl.text`，
  // 第一次敲键盘才把整份草稿建起来 —— `draft` 只记「改过的那部分」。
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState('')

  const save = (kind: string) => {
    setBusy(kind)
    api
      .cmdTemplate(kind, draft[kind] ?? '')
      .then(() => {
        // 保存后整页刷新，草稿清空 —— 现在服务端那份就是真值，别再拿旧草稿盖住它
        setDraft({})
        onSaved()
        onErr('')
      })
      .catch((e: unknown) => onErr(errMsg(e)))
      .finally(() => setBusy(''))
  }

  return (
    <Card title="推送文案">
      <Paragraph type="secondary" style={{ marginTop: 0 }}>
        两段抬头文案可以自己写。<Text strong>清空输入框 = 恢复默认</Text>
        ；正文的数据行由代码现拼，<Text strong>免责声明不在这里、改不掉</Text>
        。摘要抬头里写 <Text code>{'{date}'}</Text> 会代入当天日期。
      </Paragraph>
      <Space orientation="vertical" size={16} style={{ width: '100%' }}>
        {Object.entries(templates).map(([kind, tpl]) => {
          // draft 里没有这个 key = 没动过，显示的就是服务端那份数值
          const value = draft[kind] ?? tpl.text
          const dirty = value !== tpl.text
          return (
            <div key={kind}>
              <Text strong>{tpl.label}</Text>
              <Text type="secondary">（默认：{tpl.default || '（带日期，现拼）'}）</Text>
              <Input.TextArea
                value={value}
                rows={2}
                maxLength={200}
                showCount
                placeholder="留空 = 用默认文案"
                onChange={(e) => setDraft((prev) => ({ ...prev, [kind]: e.target.value }))}
                style={{ marginTop: 4 }}
              />
              <Button
                size="small"
                type="primary"
                disabled={!dirty}
                loading={busy === kind}
                onClick={() => save(kind)}
                style={{ marginTop: 6 }}
              >
                保存这段
              </Button>
            </div>
          )
        })}
      </Space>
    </Card>
  )
}

// ------------------------------------------------------------------ 编辑框

function EditModal({
  item,
  creating,
  onCancel,
  onSaved,
  onErr,
}: {
  item: CmdItem
  creating: boolean
  onCancel: () => void
  onSaved: (msg: string) => void
  onErr: (s: string) => void
}) {
  // **不写 `useEffect(() => setDraft(item))`** —— 那是同步 setState，会亮
  // `set-state-in-effect`（基线 6 条，多一条就红）。改成「只在打开时挂载」：
  // 每次打开都是新实例，初值直接从 prop 来，整段 effect 都不需要。
  const [draft, setDraft] = useState<CmdItem>(item)
  const [busy, setBusy] = useState(false)

  const set = <K extends keyof CmdItem>(k: K, v: CmdItem[K]) =>
    setDraft((prev) => ({ ...prev, [k]: v }))

  const ok = () => {
    if (!draft.text.trim()) {
      onErr('触发文本不能为空 —— 点按钮发出去的就是它。')
      return
    }
    if (creating && !/^[A-Za-z0-9_-]{1,40}$/.test(draft.key)) {
      onErr('key 只能是字母 / 数字 / 下划线 / 中划线，最长 40。')
      return
    }
    setBusy(true)
    api
      .cmdSave(payload(draft, {}))
      .then(() => {
        onSaved(creating ? `已新增指令「${draft.label || draft.key}」。` : '已保存。')
        onCancel()
      })
      .catch((e: unknown) => onErr(errMsg(e)))
      .finally(() => setBusy(false))
  }

  const isBuiltin = draft.kind === 'builtin'

  return (
    <Modal
      open
      title={creating ? '新增自定义指令' : `编辑：${draft.label || draft.key}`}
      okText="保存"
      cancelText="取消"
      confirmLoading={busy}
      onOk={ok}
      onCancel={onCancel}
      width={620}
    >
      <Space orientation="vertical" size={12} style={{ width: '100%' }}>
        {creating ? (
          <div>
            <Text type="secondary">key（唯一标识，建好后不可改）</Text>
            <Input
              value={draft.key}
              placeholder="例如 xingqi"
              onChange={(e) => set('key', e.target.value)}
            />
          </div>
        ) : (
          <div>
            <Text type="secondary">key</Text>
            <div>
              <Text code>{draft.key}</Text>{' '}
              {isBuiltin ? <Tag>内置</Tag> : <Tag color="purple">自定义</Tag>}
            </div>
          </div>
        )}

        <div>
          <Text type="secondary">展示名（按钮上写这个）</Text>
          <Input value={draft.label} maxLength={40} onChange={(e) => set('label', e.target.value)} />
        </div>

        <div>
          <Text type="secondary">触发文本（发这句话 = 触发这条指令）</Text>
          <Input value={draft.text} maxLength={40} onChange={(e) => set('text', e.target.value)} />
          {isBuiltin && draft.default_text && draft.text !== draft.default_text ? (
            <Text type="warning" style={{ fontSize: 12 }}>
              改过：默认是「{draft.default_text}」，旧词不再识别（把旧词填回别名即可保留）
            </Text>
          ) : null}
        </div>

        <div>
          <Text type="secondary">其余别名（逗号分隔）</Text>
          <Input
            value={draft.alias}
            maxLength={200}
            placeholder={draft.default_alias || '没有别名'}
            onChange={(e) => set('alias', e.target.value)}
          />
        </div>

        {!isBuiltin ? (
          <div>
            <Text type="secondary">权限（这条指令谁能触发）</Text>
            <div>
              <Tag color="gold" style={{ cursor: 'pointer' }}
                onClick={() => set('access', 'owner')}>
                仅本人
              </Tag>
              <Tag color="blue" style={{ cursor: 'pointer' }}
                onClick={() => set('access', 'sub')}>
                订阅者
              </Tag>
              <Text type="secondary" style={{ fontSize: 12 }}>
                当前：{draft.access === 'sub' ? '订阅者' : '仅本人'}。查不到 = 默认拒。
              </Text>
            </div>
          </div>
        ) : null}

        <div>
          <Text type="secondary">实际执行的指令原文</Text>
          <Input
            value={draft.target}
            maxLength={80}
            placeholder={isBuiltin ? '内置指令不用填' : '留空 = 只回下面的固定文案；填「状态」就等于转发给状态'}
            onChange={(e) => set('target', e.target.value)}
          />
        </div>

        <div>
          <Text type="secondary">附带文案（执行完在回复后面再推一段，可空）</Text>
          <Input.TextArea
            value={draft.reply}
            rows={3}
            maxLength={500}
            placeholder="留空 = 不追加"
            onChange={(e) => set('reply', e.target.value)}
          />
        </div>

        <div>
          <Text type="secondary">卡片排序（小的在前）</Text>
          <Input
            value={String(draft.sort)}
            style={{ width: 120 }}
            onChange={(e) => set('sort', Number(e.target.value) || 0)}
          />
        </div>

        {isBuiltin && draft.edited ? (
          <Alert
            type="info"
            showIcon
            title="这条已改过"
            description="点下面「保存」生效；想回到代码默认，在列表里点「恢复默认」（等于删掉库里这一行）。"
          />
        ) : null}
      </Space>
    </Modal>
  )
}

// -------------------------------------------------------------------- 页面

export default function Cmds() {
  const [items, setItems] = useState<CmdItem[]>([])
  const [templates, setTemplates] = useState<Record<string, CmdTpl>>({})
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState('')
  const [editing, setEditing] = useState<CmdItem | null>(null)
  const [creating, setCreating] = useState(false)

  const load = useCallback(() => {
    api
      .cmdList()
      .then((r: CmdListResponse) => {
        setItems(r.items)
        setTemplates(r.templates)
        setErr('')
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  /** 手动「刷新」按钮：显式动作，整表转个圈也无妨。 */
  const reload = () => {
    setLoading(true)
    load()
  }

  /**
   * 保存 / 开关之后的**静默刷新** —— 关键是别碰 `loading`。
   *
   * 表格是 `{loading ? <Spin /> : <Table />}` 二选一渲染的，`loading=true` 会让
   * 整个 Table **卸载换成 spinner**，滚动位置、展开、正在弹的气泡全丢，用户看到
   * 的就是「点一个开关，整页刷一遍」。静默刷新时表一直挂着，只有数据变。
   */
  const refresh = () => {
    load()
  }

  /** 即存：整条回传，成功后静默刷新（`edited` / 默认值都由后端算，前端不猜）。 */
  const patch = (row: CmdItem, change: Partial<CmdItem>, note?: string) => {
    setBusy(row.key)
    api
      .cmdSave(payload(row, change))
      .then(() => {
        setMsg(note ?? '已保存。')
        setErr('')
        refresh()
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setBusy(''))
  }

  const remove = (row: CmdItem) => {
    setBusy(row.key)
    api
      .cmdRemove(row.key)
      .then((r) => {
        setMsg(
          r.deleted
            ? row.kind === 'builtin'
              ? `「${row.label || row.key}」已恢复代码默认。`
              : `「${row.label || row.key}」已删除。`
            : '这条本来就没存过，已经是默认值。',
        )
        setErr('')
        refresh()
      })
      .catch((e: unknown) => setErr(errMsg(e)))
      .finally(() => setBusy(''))
  }

  const startCreate = () => {
    setCreating(true)
    setEditing(blank())
  }

  const columns: ColumnsType<CmdItem> = [
    {
      title: '指令',
      key: 'name',
      width: 190,
      render: (_, r) => (
        <Space size={4} orientation="vertical">
          <Space size={6}>
            <Text strong>{r.label || r.key}</Text>
            {r.kind === 'builtin' ? <Tag>内置</Tag> : <Tag color="purple">自定义</Tag>}
            {r.edited && r.kind === 'builtin' ? <Tag color="orange">已改</Tag> : null}
          </Space>
          <Text code>{r.key}</Text>
        </Space>
      ),
    },
    {
      title: '触发文本',
      key: 'text',
      width: 210,
      render: (_, r) => (
        <Space size={4} orientation="vertical">
          <Text code>{r.text || '—'}</Text>
          <Text type="secondary" style={{ fontSize: 12 }}>
            别名：{aliasText(r.alias)}
          </Text>
        </Space>
      ),
    },
    {
      title: '说明 / 参数',
      key: 'desc',
      render: (_, r) => (
        <Space size={4} orientation="vertical">
          <Text type="secondary">{r.desc || '—'}</Text>
          {r.param ? <Text type="warning" style={{ fontSize: 12 }}>吃参数：{r.param}</Text> : null}
        </Space>
      ),
    },
    {
      title: '权限',
      dataIndex: 'access',
      width: 96,
      render: (v: string, r) =>
        r.kind === 'builtin' ? (
          <Tooltip title="内置指令的闸写在后端代码里，这一页改不动它">
            {accessTag(v)}
          </Tooltip>
        ) : (
          accessTag(v)
        ),
    },
    {
      title: '进快捷卡',
      key: 'quick',
      width: 96,
      render: (_, r) => (
        <Tooltip title={r.param ? '带参数的指令点一下跑不了，不能进快捷卡' : undefined}>
          <Switch
            size="small"
            checked={!!r.quick}
            disabled={busy === r.key || !!r.param}
            onChange={(v) =>
              patch(r, { quick: v ? 1 : 0 }, v ? '已加进快捷指令卡片。' : '已从快捷卡片移除。')
            }
          />
        </Tooltip>
      ),
    },
    {
      title: '启用',
      key: 'enabled',
      width: 80,
      render: (_, r) => (
        <Switch
          size="small"
          checked={!!r.enabled}
          disabled={busy === r.key}
          onChange={(v) =>
            patch(r, { enabled: v ? 1 : 0 }, v ? '已启用。' : '已停用 —— 这句话不再识别。')
          }
        />
      ),
    },
    {
      title: '操作',
      key: 'ops',
      width: 170,
      render: (_, r) => (
        <Space size={6}>
          <Button
            size="small"
            loading={busy === r.key}
            onClick={() => {
              setCreating(false)
              setEditing(r)
            }}
          >
            编辑
          </Button>
          <Popconfirm
            title={r.kind === 'builtin' ? '恢复这条的代码默认值？' : '删除这条自定义指令？'}
            description={
              r.kind === 'builtin'
                ? '改动会被丢掉，回到出厂触发词与文案。'
                : '删掉后它就不再识别了。'
            }
            okText={r.kind === 'builtin' ? '恢复默认' : '删除'}
            cancelText="取消"
            onConfirm={() => remove(r)}
          >
            <Button size="small" danger disabled={!r.edited && r.kind === 'builtin'}>
              {r.kind === 'builtin' ? '恢复默认' : '删除'}
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      <Card
        title="指令管理"
        extra={
          <Space>
            <Button icon={<ReloadOutlined />} onClick={reload} loading={loading}>
              刷新
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={startCreate}>
              新增自定义指令
            </Button>
          </Space>
        }
      >
        <Paragraph type="secondary" style={{ marginTop: 0 }}>
          这里管两件事：① <Text strong>内置 17 条</Text>指令的触发词、别名、附带文案与快捷标记；
          ② <Text strong>自定义指令</Text> —— 触发词可以只是转发一条真实指令（比如「查新华」=
          查询 600825），也可以纯回一句固定文案。内置的权限写在后端代码里，
          <Text strong>这一页改不动</Text>；自定义只有「仅本人 / 订阅者」两档，查不到一律拒。
        </Paragraph>

        {err ? (
          <Alert type="error" showIcon title="操作失败" description={err} style={{ marginBottom: 12 }} />
        ) : null}
        {msg ? (
          <Alert type="success" showIcon title="已完成" description={msg} style={{ marginBottom: 12 }} />
        ) : null}

        {loading ? (
          <Spin />
        ) : (
          <Table<CmdItem>
            rowKey="key"
            size="small"
            columns={columns}
            dataSource={items}
            pagination={{ pageSize: 20, hideOnSinglePage: true }}
            locale={{
              emptyText: (
                <Empty
                  description={
                    <span>
                      还没有指令。后端应有 17 条内置指令 —— 出现空表说明后端没连上。
                    </span>
                  }
                />
              ),
            }}
          />
        )}
      </Card>

      <QuickCardCard items={items} onErr={setErr} />

      <TemplatesCard templates={templates} onSaved={refresh} onErr={setErr} />

      {/* 只在打开时挂载：EditModal 靠**挂载**取初值，这样就不用在 effect 里
          同步 setState（那会亮 set-state-in-effect）。 */}
      {editing ? (
        <EditModal
          item={editing}
          creating={creating}
          onCancel={() => {
            setEditing(null)
            setCreating(false)
          }}
          onSaved={(m) => {
            setMsg(m)
            setErr('')
            refresh()
          }}
          onErr={setErr}
        />
      ) : null}

      <Alert
        type="info"
        showIcon
        title="数据免责声明"
        description="本页只配置指令的触发方式与展示文案。所呈现的行情与基金数据来自公开接口的整理与统计，仅用于数据可视化研究，不提供任何买卖信号，不构成投资建议。"
      />
    </Space>
  )
}
