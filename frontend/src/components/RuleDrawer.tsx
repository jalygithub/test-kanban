/**
 * 规则管理抽屉 —— 自动交易里「选规则 / 改规则 / 加规则」的入口。
 *
 * 三块：买入规则（filter，决定买）/ 卖出规则（exit，决定卖）/ 新增规则。
 * 每条规则：开关 + 名称 + 参数（行内改）+ 删除。改动都先落在**本地草稿**，
 * 点「保存」才一次性 PUT 给后端（整套替换，后端校验未知类型/越界 → 400）。
 *
 * 「新增规则」= 从**规则类型清单**挑一种 + 填参数（新增的是已有类型的实例，
 * 全新逻辑需改代码 —— 与 rules.py 的诚实边界一致，不在前端吹纯配置万能）。
 */
import { useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Drawer,
  InputNumber,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tabs,
  Tag,
  Typography,
  message,
} from 'antd'
import { DeleteOutlined, PlusOutlined, UndoOutlined } from '@ant-design/icons'
import {
  api,
  errMsg,
  type TradingRule,
  type TradingRuleType,
  type TradingRulesPayload,
} from '../lib/api'

const { Text, Paragraph } = Typography

interface Props {
  open: boolean
  onClose: () => void
  onSaved: (p: TradingRulesPayload) => void
}

/** 参数控件：按 spec 类型渲染 int/float/bool。 */
function ParamInput({
  spec,
  value,
  onChange,
}: {
  spec: { type: string; min?: number; max?: number; label: string }
  value: number | boolean
  onChange: (v: number | boolean) => void
}) {
  if (spec.type === 'bool') {
    return (
      <Switch
        size="small"
        checked={Boolean(value)}
        onChange={(v) => onChange(v)}
      />
    )
  }
  return (
    <InputNumber
      size="small"
      value={Number(value)}
      min={spec.min}
      max={spec.max}
      step={spec.type === 'int' ? 1 : 0.01}
      onChange={(v) => onChange(v ?? 0)}
      style={{ width: 120 }}
    />
  )
}

export default function RuleDrawer({ open, onClose, onSaved }: Props) {
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [rules, setRules] = useState<TradingRule[]>([])
  const [types, setTypes] = useState<TradingRulesPayload['types'] | null>(null)
  const [newType, setNewType] = useState<{ category: 'filter' | 'exit'; type: string } | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const p = await api.tradingRulesGet()
      setRules(p.rules)
      setTypes(p.types)
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open) load()
  }, [open])

  const typeMap = (category: 'filter' | 'exit'): Record<string, TradingRuleType> =>
    (types?.[category] ?? {}) as Record<string, TradingRuleType>

  const update = (id: string, patch: Partial<TradingRule>) =>
    setRules((rs) => rs.map((r) => (r.id === id ? { ...r, ...patch } : r)))

  const remove = (id: string) => setRules((rs) => rs.filter((r) => r.id !== id))

  const addRule = () => {
    if (!newType) return
    const spec = typeMap(newType.category)[newType.type]
    if (!spec) return
    const params: Record<string, number | boolean> = {}
    for (const [k, s] of Object.entries(spec.params)) params[k] = s.default
    const id = `${newType.category}_${newType.type}_${Date.now()}`
    setRules((rs) => [
      ...rs,
      {
        id,
        category: newType.category,
        type: newType.type,
        name: spec.label,
        enabled: true,
        order: 100 + rs.length,
        params,
      },
    ])
    setNewType(null)
  }

  const save = async () => {
    setSaving(true)
    try {
      const p = await api.tradingRulesPut(rules)
      onSaved(p)
      message.success(`已保存 ${p.rules.length} 条规则（版本 ${p.version}）`)
      onClose()
    } catch (e) {
      message.error(errMsg(e))
    } finally {
      setSaving(false)
    }
  }

  const reset = async () => {
    try {
      const p = await api.tradingRulesReset()
      setRules(p.rules)
      setTypes(p.types)
      onSaved(p)
      message.success('已恢复出厂默认规则')
    } catch (e) {
      message.error(errMsg(e))
    }
  }

  /** 新增规则下拉：把类型清单摊平成选项。 */
  const typeOptions = useMemo(() => {
    const out: { value: string; label: string; category: 'filter' | 'exit' }[] = []
    for (const cat of ['filter', 'exit'] as const) {
      for (const [t, spec] of Object.entries(typeMap(cat))) {
        out.push({ value: `${cat}:${t}`, label: `${cat === 'filter' ? '买入' : '卖出'} · ${spec.label}`, category: cat })
      }
    }
    return out
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [types])

  const renderRules = (category: 'filter' | 'exit') => {
    const list = rules.filter((r) => r.category === category)
    const tm = typeMap(category)
    return (
      <div>
        {list.map((r) => {
          const spec = tm[r.type]
          return (
            <div
              key={r.id}
              style={{
                border: '1px solid #f0f0f0',
                borderRadius: 8,
                padding: '10px 12px',
                marginBottom: 10,
                background: r.enabled ? '#fff' : '#fafafa',
              }}
            >
              <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                <Space>
                  <Switch
                    checked={r.enabled}
                    onChange={(v) => update(r.id, { enabled: v })}
                  />
                  <Text strong>{r.name}</Text>
                  <Tag>{r.type}</Tag>
                </Space>
                <Popconfirm title="删除这条规则？" onConfirm={() => remove(r.id)}>
                  <Button danger size="small" type="text" icon={<DeleteOutlined />} />
                </Popconfirm>
              </Space>
              {spec?.desc && (
                <Paragraph type="secondary" style={{ margin: '6px 0 2px', fontSize: 12 }}>
                  {spec.desc}
                </Paragraph>
              )}
              <Space size="middle" wrap>
                {spec &&
                  Object.entries(spec.params).map(([k, ps]) => (
                    <Space key={k} size={4}>
                      <Text type="secondary" style={{ fontSize: 12 }}>{ps.label}</Text>
                      <ParamInput
                        spec={ps}
                        value={r.params[k] ?? ps.default}
                        onChange={(v) =>
                          update(r.id, { params: { ...r.params, [k]: v } })
                        }
                      />
                    </Space>
                  ))}
              </Space>
            </div>
          )
        })}
        {list.length === 0 && <Text type="secondary">（暂无{category === 'filter' ? '买入' : '卖出'}规则）</Text>}
      </div>
    )
  }

  return (
    <Drawer
      title="管理选股 / 退出规则"
      width={560}
      open={open}
      onClose={onClose}
      extra={
        <Space>
          <Button icon={<UndoOutlined />} onClick={reset}>恢复默认</Button>
          <Button type="primary" loading={saving} onClick={save}>保存</Button>
        </Space>
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 12 }}
        message="规则即数据"
        description="开关控制是否启用，参数行内可改，改动点「保存」才生效。新增规则 = 从规则类型里挑一种填参数（全新算法需改代码，非纯配置）。"
      />
      <div style={{ display: 'flex', gap: 8, marginBottom: 12 }}>
        <Select
          style={{ flex: 1 }}
          placeholder="选一种规则类型新增…"
          value={newType ? `${newType.category}:${newType.type}` : undefined}
          options={typeOptions}
          onChange={(v: string) => {
            const [category, type] = v.split(':')
            setNewType({ category: category as 'filter' | 'exit', type })
          }}
        />
        <Button type="primary" ghost icon={<PlusOutlined />} disabled={!newType} onClick={addRule}>
          新增
        </Button>
      </div>
      <Tabs
        loading={loading}
        items={[
          { key: 'filter', label: `买入规则（${rules.filter((r) => r.category === 'filter').length}）`, children: renderRules('filter') },
          { key: 'exit', label: `卖出规则（${rules.filter((r) => r.category === 'exit').length}）`, children: renderRules('exit') },
        ]}
      />
    </Drawer>
  )
}
