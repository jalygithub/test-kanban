import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert,
  Button,
  Card,
  Drawer,
  Empty,
  Pagination,
  Progress,
  Segmented,
  Space,
  Spin,
  Tag,
  Typography,
} from 'antd'
import { LinkOutlined, ReloadOutlined } from '@ant-design/icons'
import { api, type NewsItem, type NewsResponse } from '../lib/api'

const { Text, Paragraph } = Typography

/** 热度配色：刻意避开全站的涨(红)/跌(绿)，用 灰-蓝-金-橙 四档。 */
const heatColor = (v: number) =>
  v >= 80 ? '#fa541c' : v >= 60 ? '#faad14' : v >= 40 ? '#1677ff' : '#8c8c8c'
const heatLabel = (v: number) => (v >= 80 ? '高' : v >= 60 ? '较高' : v >= 40 ? '中等' : '一般')

/** published_at 是后端本地时间 "YYYY-MM-DD HH:MM:SS"，列表里只显示到分。 */
const fmtTime = (s?: string) => (s ? s.slice(5, 16) : '-')

const SRC_TAG: Record<string, string> = {
  cls: 'geekblue',
  em: 'volcano',
  futu: 'purple',
  sina: 'orange',
  shmet: 'cyan',
  cjzc: 'magenta',
  demo: 'default',
}

const PAGE_SIZE = 20

/**
 * 财经新闻页：分类横排，点分类在下方看条目，条目右侧是热度，点条目看正文。
 *
 * 数据一次拉全量（后端 5 分钟缓存），分类切换纯本地过滤 —— 切分类不再打源站。
 */
export default function News() {
  const [data, setData] = useState<NewsResponse | null>(null)
  const [category, setCategory] = useState('all')
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [active, setActive] = useState<NewsItem | null>(null)
  const [page, setPage] = useState(1)

  // 用 promise 链而不是 async/await：load 体内没有任何同步 setState，
  // 效果里 `void load()` 才不会被 set-state-in-effect 判为「在 effect 里同步改状态」
  const load = useCallback(
    () =>
      api.news('all')
        .then((res) => {
          setData(res)
          setError(null)
        })
        .catch((e: Error) => setError(e.message)),
    [],
  )

  const reload = useCallback(async () => {
    setRefreshing(true)
    try {
      const res = await api.news('all', true)
      setData(res)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const items = useMemo(
    () => (data?.items ?? []).filter((it) => category === 'all' || it.category === category),
    [data, category],
  )

  // 纯渲染期推导：刷新后条数变少也不会停在越界页（避免在 effect 里补 setState）
  const lastPage = Math.max(1, Math.ceil(items.length / PAGE_SIZE))
  const current = Math.min(page, lastPage)
  const paged = useMemo(
    () => items.slice((current - 1) * PAGE_SIZE, current * PAGE_SIZE),
    [items, current],
  )

  const catLabel = (key: string) => data?.categories.find((c) => c.key === key)?.label ?? key
  const srcLabel = (key: string) => data?.sources.find((s) => s.key === key)?.label ?? key

  const options = useMemo(
    () =>
      (data?.categories ?? []).map((c) => ({ label: `${c.label} ${c.count}`, value: c.key })),
    [data],
  )

  return (
    <Space orientation="vertical" size={16} style={{ width: '100%' }}>
      {error && (
        <Alert
          type="error"
          showIcon
          title="新闻抓取失败"
          description={error}
          action={
            <Button size="small" onClick={() => void load()}>
              重试
            </Button>
          }
        />
      )}
      {data?.demo && (
        <Alert
          type="warning"
          showIcon
          title="当前为内置演示数据"
          description="六个新闻源都未能连通，页面展示的是随程序附带的示例内容，不代表真实盘面。"
        />
      )}
      {data && data.failed_sources.length > 0 && (
        <Alert
          type="info"
          showIcon
          title={`以下源本次抓取失败，内容已由其余源补位：${data.failed_sources.join('、')}`}
        />
      )}

      <Card
        size="small"
        title="财经新闻"
        extra={
          <Space wrap size={8}>
            <Text type="secondary" style={{ fontSize: 12 }}>
              {data ? `${data.fetched_at} 抓取 · 缓存 ${data.ttl_sec}s` : '正在连接…'}
            </Text>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              loading={refreshing}
              onClick={() => void reload()}
            >
              刷新
            </Button>
          </Space>
        }
      >
        {options.length > 0 ? (
          <Segmented
            block
            value={category}
            options={options}
            onChange={(v) => {
              setCategory(String(v))
              setPage(1)
            }}
          />
        ) : (
          <Spin />
        )}
      </Card>

      <Card size="small">
        {!data && !error ? (
          <div style={{ textAlign: 'center', padding: '48px 0' }}>
            <Spin description="正在抓取各源快讯…" />
          </div>
        ) : items.length === 0 ? (
          <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="该分类下暂无新闻" />
        ) : (
          <>
            {paged.map((it) => (
              <div
                key={it.id}
                className="news-row"
                role="button"
                tabIndex={0}
                onClick={() => setActive(it)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') setActive(it)
                }}
              >
                <div style={{ display: 'flex', width: '100%', gap: 16, alignItems: 'center' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <Space size={6} wrap>
                      <Text strong>{it.title}</Text>
                      {it.source_count > 1 && (
                        <Tag color="processing" style={{ marginInlineEnd: 0 }}>
                          {it.source_count} 源
                        </Tag>
                      )}
                    </Space>
                    <div style={{ marginTop: 4 }}>
                      <Text type="secondary" style={{ fontSize: 12 }}>
                        {srcLabel(it.source)} · {fmtTime(it.published_at)} · {catLabel(it.category)}
                      </Text>
                    </div>
                    <Paragraph
                      type="secondary"
                      ellipsis={{ rows: 1 }}
                      style={{ margin: '4px 0 0', fontSize: 12 }}
                    >
                      {it.content}
                    </Paragraph>
                  </div>
                  <div style={{ width: 148, flexShrink: 0, textAlign: 'right' }}>
                    <Text style={{ fontWeight: 600, color: heatColor(it.heat) }}>
                      热度 {it.heat}
                    </Text>
                    <Progress
                      percent={it.heat}
                      showInfo={false}
                      size="small"
                      strokeColor={heatColor(it.heat)}
                      style={{ margin: '2px 0 0' }}
                    />
                    <Text type="secondary" style={{ fontSize: 12 }}>
                      {heatLabel(it.heat)}
                    </Text>
                  </div>
                </div>
              </div>
            ))}
            <div style={{ display: 'flex', justifyContent: 'flex-end', marginTop: 12 }}>
              <Pagination
                current={current}
                pageSize={PAGE_SIZE}
                total={items.length}
                showSizeChanger={false}
                showTotal={(t) => `共 ${t} 条`}
                onChange={(p) => setPage(p)}
              />
            </div>
          </>
        )}
      </Card>

      <Text type="secondary" style={{ fontSize: 12 }}>
        {data?.score_note ? `${data.score_note}。` : ''}
        热度为本页自算的参考值，仅用于排序与浏览；新闻内容来自公开快讯源，不构成投资建议。
      </Text>

      <Drawer
        open={!!active}
        onClose={() => setActive(null)}
        size={600}
        title={active?.title}
        footer={
          active && active.source_links.length > 0 ? (
            <Space wrap>
              {active.source_links.map((l) => (
                <Button key={l.source} type="primary" icon={<LinkOutlined />}>
                  <a href={l.url} target="_blank" rel="noreferrer noopener">
                    {l.label} 原文
                  </a>
                </Button>
              ))}
            </Space>
          ) : null
        }
      >
        {active && (
          <Space orientation="vertical" size={16} style={{ width: '100%' }}>
            <Space wrap size={6}>
              <Tag color={SRC_TAG[active.source] ?? 'default'}>{srcLabel(active.source)}</Tag>
              <Tag>{catLabel(active.category)}</Tag>
              <Text type="secondary">{fmtTime(active.published_at)}</Text>
              {active.source_count > 1 && (
                <Tag color="processing">{active.source_count} 源同时报道</Tag>
              )}
            </Space>

            <Card size="small" title="热度">
              <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                <Text style={{ fontSize: 28, fontWeight: 700, color: heatColor(active.heat) }}>
                  {active.heat}
                </Text>
                <div style={{ flex: 1 }}>
                  <Progress
                    percent={active.heat}
                    showInfo={false}
                    strokeColor={heatColor(active.heat)}
                  />
                </div>
                <Text type="secondary">{heatLabel(active.heat)}</Text>
              </div>
              <Space size={4} wrap style={{ marginTop: 4 }}>
                <Tag>时效 {active.heat_fresh}</Tag>
                <Tag>关键词 {active.heat_keyword}</Tag>
                <Tag>多源 {active.heat_multi}</Tag>
                <Tag>来源 {active.heat_source}</Tag>
              </Space>
              <div>
                <Text type="secondary" style={{ fontSize: 12 }}>
                  {data?.score_note}
                </Text>
              </div>
            </Card>

            <Paragraph style={{ whiteSpace: 'pre-wrap' }}>
              {active.content}
              {active.truncated && <Text type="secondary">…（正文已截断，完整内容见原文链接）</Text>}
            </Paragraph>

            {active.source_links.length === 0 && (
              <Text type="secondary" style={{ fontSize: 12 }}>
                该条来自无原文链接的快讯源，正文即全部内容。
              </Text>
            )}
          </Space>
        )}
      </Drawer>
    </Space>
  )
}
