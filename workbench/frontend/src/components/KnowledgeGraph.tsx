import { useEffect, useRef, useState } from 'react'
import cytoscape, { type Core, type EventObject, type StylesheetStyle } from 'cytoscape'
import type { KnowledgeEdge, KnowledgeGraphData, KnowledgeNode } from '../api/knowledge'
import { fitKnowledgeGraph, highlightKnowledgeGraph, reconcileKnowledgeGraph } from '../lib/knowledgeGraphState'

export const KNOWLEDGE_KINDS = [
  { kind: 'chapter', label: '章节', token: '--highlight-3', shape: 'diamond' },
  { kind: 'document', label: '文档', token: '--highlight-1', shape: 'rectangle' },
  { kind: 'content', label: '内容', token: '--highlight-1', shape: 'rectangle' },
  { kind: 'character', label: '人物', token: '--highlight-2', shape: 'ellipse' },
  { kind: 'item', label: '物品', token: '--highlight-4', shape: 'ellipse' },
  { kind: 'event', label: '事件 / 情节', token: '--color-warning', shape: 'diamond' },
  { kind: 'location', label: '地点', token: '--highlight-5', shape: 'hexagon' },
  { kind: 'scene', label: '场景', token: '--highlight-5', shape: 'hexagon' },
  { kind: 'faction', label: '势力', token: '--color-success', shape: 'hexagon' },
  { kind: 'foreshadow', label: '伏笔', token: '--highlight-4', shape: 'diamond' },
  { kind: 'concept', label: '主题 / 概念', token: '--color-primary', shape: 'ellipse' },
  { kind: 'world', label: '世界', token: '--highlight-5', shape: 'hexagon' },
  { kind: 'skill', label: '能力', token: '--highlight-4', shape: 'ellipse' },
  { kind: 'state', label: '状态', token: '--highlight-3', shape: 'diamond' },
] as const
export const knowledgeKindLabel = (kind: string) => KNOWLEDGE_KINDS.find((item) => item.kind === kind)?.label || kind

/** Keep small focused views readable without inflating dense panoramas. */
export function knowledgeLabelSizing(nodeCount: number, zoom: number) {
  const safeZoom = Math.max(0.12, zoom)
  const small = nodeCount > 0 && nodeCount <= 20
  return { fontSize: Math.max(14, Math.min(small ? 40 : 30, (small ? 14 : 10) / safeZoom)),
    maxWidth: Math.max(150, Math.min(small ? 380 : 330, (small ? 150 : 112) / safeZoom)) }
}

function graphStyles(): StylesheetStyle[] {
  const styles = getComputedStyle(document.documentElement)
  const color = (token: string) => styles.getPropertyValue(token).trim()
  return [
    { selector: 'node', style: { 'background-color': color('--color-primary'), label: 'data(label)',
      color: color('--color-fg'), 'font-family': styles.getPropertyValue('--font-sans').trim(),
      'font-size': 14, 'text-valign': 'bottom', 'text-margin-y': 8, 'text-max-width': '150px',
      'text-wrap': 'ellipsis', 'min-zoomed-font-size': 0,
      'text-background-color': color('--color-bg-raised'), 'text-background-opacity': 0.88,
      'text-background-padding': '2px', width: 34, height: 34, 'border-width': 2,
      'border-color': color('--color-bg-raised') } },
    { selector: 'edge', style: { width: 1.8, 'line-color': color('--color-fg-subtle'),
      'target-arrow-color': color('--color-fg-subtle'), 'target-arrow-shape': 'triangle',
      'curve-style': 'bezier', 'arrow-scale': 0.85, opacity: 0.9 } },
    { selector: 'edge[edgeKind = "mention"]',
      style: { 'line-style': 'dashed', 'line-dash-pattern': [5, 4], 'target-arrow-shape': 'none' } },
    { selector: 'edge[edgeKind = "planned"]', style: { 'line-style': 'dashed',
      'line-color': color('--color-warning'), 'target-arrow-color': color('--color-warning') } },
    { selector: 'edge[edgeKind = "relationship"]', style: { width: 2.3,
      'line-color': color('--color-primary'), 'target-arrow-color': color('--color-primary') } },
    { selector: 'node[reviewStatus = "pending"], node[reviewStatus = "conflict"]', style: {
      'border-width': 3, 'border-style': 'dashed', 'border-color': color('--color-warning') } },
    { selector: 'edge[reviewStatus = "pending"], edge[reviewStatus = "conflict"]', style: {
      'line-style': 'dashed', 'line-color': color('--color-warning'), 'target-arrow-color': color('--color-warning') } },
    { selector: '.is-muted', style: { opacity: 0.42 } },
    { selector: '.is-hit', style: { 'border-color': color('--color-focus'), 'border-width': 5 } },
    { selector: '.is-context', style: { 'border-color': color('--color-focus'), 'border-width': 3 } },
    { selector: 'edge.is-evidence', style: { width: 3, opacity: 1,
      'line-color': color('--color-focus'), 'target-arrow-color': color('--color-focus') } },
    { selector: 'node:selected', style: { 'border-color': color('--color-focus'), 'border-width': 5,
      'font-weight': 'bold', opacity: 1, 'z-index': 20 } },
    { selector: 'edge:selected, edge.is-hovered', style: { label: 'data(relation)', width: 3,
      'font-size': 12, color: color('--color-fg'), 'text-rotation': 'autorotate',
      'text-background-color': color('--color-bg-raised'), 'text-background-opacity': 1,
      'text-background-padding': '4px', 'text-border-color': color('--color-border-strong'),
      'text-border-width': 1, 'text-border-opacity': 1, 'line-color': color('--color-focus'),
      'target-arrow-color': color('--color-focus'), opacity: 1, 'z-index': 20 } },
    ...KNOWLEDGE_KINDS.map((item) => ({ selector: `node[kind = "${item.kind}"]`,
      style: { 'background-color': color(item.token), shape: item.shape } })),
  ] as StylesheetStyle[]
}

export default function KnowledgeGraph({ data, highlighted = [], highlightedEvidence = [], highlightedDocument = '',
  scopeKey = '', fitRequest = 0, emptyMessage, onSelect }: {
  data: KnowledgeGraphData | null
  highlighted?: string[]
  highlightedEvidence?: string[]
  highlightedDocument?: string
  scopeKey?: string
  fitRequest?: number
  emptyMessage?: string
  onSelect: (item: KnowledgeNode | KnowledgeEdge, kind: 'node' | 'edge') => void
}) {
  const container = useRef<HTMLDivElement>(null)
  const graph = useRef<Core | null>(null)
  const select = useRef(onSelect)
  select.current = onSelect
  const [layoutBusy, setLayoutBusy] = useState(false)
  const [legendOpen, setLegendOpen] = useState(false)
  const [tooltip, setTooltip] = useState('')
  const viewScope = useRef('')

  useEffect(() => {
    if (!container.current) return
    const instance = cytoscape({ container: container.current, style: graphStyles(),
      minZoom: 0.12, maxZoom: 3, wheelSensitivity: 0.18, boxSelectionEnabled: false,
      selectionType: 'single' })
    graph.current = instance
    instance.on('tap', 'node, edge', (event: EventObject) => {
      const value = event.target.data('record') as KnowledgeNode | KnowledgeEdge
      if (value) select.current(value, event.target.isNode() ? 'node' : 'edge')
    })
    instance.on('mouseover', 'node, edge', (event: EventObject) => {
      const element = event.target
      const record = element.data('record') as KnowledgeNode | KnowledgeEdge
      element.addClass('is-hovered')
      setTooltip(element.isNode() ? (record as KnowledgeNode).label
        : `${element.source().data('record')?.label || ''} → ${element.target().data('record')?.label || ''} · ${(record as KnowledgeEdge).relation}`)
    })
    instance.on('mouseout', 'node, edge', (event: EventObject) => {
      event.target.removeClass('is-hovered'); setTooltip('')
    })
    instance.on('grab pan zoom', () => setTooltip(''))
    // Keep overview labels legible without allowing small subgraphs to inflate.
    // The full name stays available in the tooltip and the related-node list.
    const readableLabels = () => {
      const sizing = knowledgeLabelSizing(instance.nodes().length, instance.zoom())
      instance.nodes().style({ 'font-size': sizing.fontSize, 'text-max-width': `${sizing.maxWidth}px` })
    }
    instance.on('zoom add', readableLabels)
    let wasNarrow = false
    let previousWidth = instance.width()
    const resize = new ResizeObserver(() => {
      instance.resize()
      if (previousWidth <= 1 && instance.width() > 1) fitKnowledgeGraph(instance)
      previousWidth = instance.width()
      const narrow = instance.width() < 720
      if (narrow && !wasNarrow) setLegendOpen(false)
      wasNarrow = narrow
    })
    resize.observe(container.current)
    const theme = new MutationObserver(() => { instance.style(graphStyles()); readableLabels() })
    theme.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'style'] })
    return () => { resize.disconnect(); theme.disconnect(); instance.destroy(); graph.current = null }
  }, [])

  useEffect(() => {
    const instance = graph.current
    if (!instance) return
    const change = reconcileKnowledgeGraph(instance, data, data ? `${data.project_id}:${data.knowledge_key}` : '', scopeKey)
    const scopeChanged = viewScope.current !== scopeKey
    viewScope.current = scopeKey
    if (change.reset) setTooltip('')
    if (change.initialLayout) {
      setLayoutBusy(true)
      const layout = instance.layout({ name: 'cose', animate: false, fit: false,
        nodeDimensionsIncludeLabels: true, randomize: false, nodeRepulsion: 11000,
        idealEdgeLength: 125, componentSpacing: 95, numIter: 600 })
      layout.one('layoutstop', () => {
        // CoSE can return a tall cluster in a wide workspace. Rotate only the
        // initial layout to use the available canvas; later refreshes retain it.
        const bounds = instance.nodes().boundingBox({ includeLabels: false })
        if (instance.width() > instance.height() * 1.3 && bounds.h > bounds.w * 1.2) {
          instance.nodes().positions((node) => ({ x: -node.position('y'), y: node.position('x') }))
        }
        setLayoutBusy(false); fitKnowledgeGraph(instance)
      })
      layout.run()
      return () => { layout.stop() }
    }
    if ((scopeChanged || change.reset) && !change.restored) fitKnowledgeGraph(instance)
  }, [data, scopeKey])

  useEffect(() => {
    const instance = graph.current
    if (!instance) return
    highlightKnowledgeGraph(instance, highlighted, highlightedEvidence, highlightedDocument)
  }, [highlighted, highlightedEvidence, highlightedDocument, data])

  useEffect(() => {
    if (graph.current) fitKnowledgeGraph(graph.current)
  }, [fitRequest])

  const zoomBy = (factor: number) => {
    const instance = graph.current
    if (!instance) return
    instance.zoom({ level: Math.min(3, Math.max(0.12, instance.zoom() * factor)),
      renderedPosition: { x: instance.width() / 2, y: instance.height() / 2 } })
  }

  return (
    <div className="knowledge-graph">
      <div className="knowledge-graph__toolbar">
        <span className="muted">{layoutBusy ? '正在排列…' : '拖动节点 · 滚轮缩放 · 点击查看依据'}</span>
        <div className="btn-row">
          <button className="btn btn--sm" onClick={() => zoomBy(1.2)} aria-label="放大图谱">＋</button>
          <button className="btn btn--sm" onClick={() => zoomBy(1 / 1.2)} aria-label="缩小图谱">−</button>
          <button className="btn btn--sm" onClick={() => { if (graph.current) fitKnowledgeGraph(graph.current) }}>适应视图</button>
          <button className="btn btn--sm" aria-expanded={legendOpen} onClick={() => setLegendOpen((open) => !open)}>图例</button>
        </div>
      </div>
      <div ref={container} className="knowledge-graph__canvas" role="img" aria-label={`本书知识图谱，${data?.nodes.length || 0} 个节点，${data?.edges.length || 0} 条关系。可从节点列表选择查看。`} />
      {tooltip && <div className="knowledge-graph__tooltip" role="tooltip">{tooltip}</div>}
      {!data?.nodes.length && <div className="knowledge-graph__empty"><strong>还没有可展示的节点</strong><p>{emptyMessage || '同步本书资料，或清除筛选条件。'}</p></div>}
      {legendOpen && <div className="knowledge-graph__legend" aria-label="节点图例">
        {KNOWLEDGE_KINDS.filter((item) => data?.nodes.some((node) => node.kind === item.kind)).map((item) => <span key={item.kind}><i data-shape={item.shape} style={{ background: `var(${item.token})` }} />{item.label}</span>)}
        <span className="knowledge-graph__edge-legend"><i data-edge-kind="structure" />包含 / 定义</span>
        <span className="knowledge-graph__edge-legend"><i data-edge-kind="mention" />原文提及</span>
        <span className="knowledge-graph__edge-legend"><i data-edge-kind="relationship" />明确关系</span>
        <small>提及不代表人物关系；关系详情可查看原文和来源。</small>
      </div>}
      <div className="knowledge-graph__accessible">
        <select className="select" aria-label="选择图谱节点" value="" onChange={(event) => {
          const node = data?.nodes.find((item) => item.id === event.target.value)
          if (node) { select.current(node, 'node'); graph.current?.elements().unselect(); graph.current?.getElementById(node.id).select() }
        }}><option value="">节点列表（{data?.nodes.length || 0}）</option>
          {data?.nodes.map((node) => <option key={node.id} value={node.id}>{knowledgeKindLabel(node.kind)} · {node.label}</option>)}
        </select>
        <select className="select" aria-label="选择图谱关系" value="" onChange={(event) => {
          const edge = data?.edges.find((item) => item.id === event.target.value)
          if (edge) { select.current(edge, 'edge'); graph.current?.elements().unselect(); graph.current?.getElementById(edge.id).select() }
        }}><option value="">关系列表（{data?.edges.length || 0}）</option>
          {data?.edges.map((edge) => <option key={edge.id} value={edge.id}>{data.nodes.find((node) => node.id === edge.source)?.label} → {data.nodes.find((node) => node.id === edge.target)?.label} · {edge.relation}</option>)}
        </select>
      </div>
    </div>
  )
}
