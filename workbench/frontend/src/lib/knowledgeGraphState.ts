import type { Core, Position } from 'cytoscape'
import type { KnowledgeEdge, KnowledgeGraphData, KnowledgeNode } from '../api/knowledge'

const STATE = '_knowledgeGraph'
type SavedView = { positions: Map<string, Position>; viewport?: { zoom: number; pan: Position } }
type GraphState = { book: string; scope: string; positions: Map<string, Position>; views: Map<string, SavedView> }

export function knowledgeEdgeKind(edge: KnowledgeEdge): string {
  if (['mention', 'cooccurrence', 'co_occurrence'].includes(edge.origin) || /提及|共现/.test(edge.relation)) return 'mention'
  if (edge.origin === 'planned' || ['draft', 'planned'].includes(edge.lifecycle || '') || /计划/.test(edge.relation)) return 'planned'
  if (/^(包含|定义|记录|当前记录|记录于章节|埋设于章节)$/.test(edge.relation)) return 'structure'
  return 'relationship'
}

/** Reconcile records without throwing away the author's positions or viewport on refresh. */
export function reconcileKnowledgeGraph(instance: Core, data: KnowledgeGraphData | null, book: string, scope = '') {
  let state = instance.scratch(STATE) as GraphState | undefined
  if (state) {
    instance.nodes().forEach((node) => { state!.positions.set(node.id(), { ...node.position() }) })
    if (state.book && instance.nodes().length) state.views.set(`${state.book}\u0000${state.scope}`, {
      positions: state.positions, viewport: { zoom: instance.zoom(), pan: { ...instance.pan() } },
    })
  }
  const reset = state?.book !== book || state?.scope !== scope
  const saved = state?.views.get(`${book}\u0000${scope}`)
  if (reset || !state) {
    instance.elements().remove()
    state = { book, scope, positions: saved?.positions || new Map(), views: state?.views || new Map() }
    instance.scratch(STATE, state)
    instance.viewport(saved?.viewport || { zoom: 1, pan: { x: 0, y: 0 } })
  }
  const previous = new Set(instance.nodes().map((node) => node.id()))
  instance.nodes().forEach((node) => { state.positions.set(node.id(), { ...node.position() }) })
  const nodes = data?.nodes.slice(0, 300) || []
  const allowed = new Set(nodes.map((node) => node.id))
  const edges = (data?.edges || []).filter((edge) => allowed.has(edge.source) && allowed.has(edge.target))
  const ids = new Set([...allowed, ...edges.map((edge) => edge.id)])
  const retained = nodes.filter((node) => previous.has(node.id)).length
  const remembered = nodes.filter((node) => state.positions.has(node.id)).length
  instance.batch(() => {
    instance.elements().filter((element) => !ids.has(element.id())).remove()
    nodes.forEach((node, index) => {
      const record = { id: node.id, label: node.label.length > 28 ? `${node.label.slice(0, 27)}…` : node.label,
        kind: node.kind, reviewStatus: node.review_status || '', record: node }
      const existing = instance.getElementById(node.id)
      if (existing.length) { existing.data(record); return }
      const saved = state.positions.get(node.id)
      const neighbour = edges.find((edge) => edge.source === node.id && previous.has(edge.target)
        || edge.target === node.id && previous.has(edge.source))
      const anchor = neighbour && instance.getElementById(neighbour.source === node.id ? neighbour.target : neighbour.source)
      const centre = anchor?.length ? anchor.position() : { x: 0, y: 0 }
      const angle = index * Math.PI * (3 - Math.sqrt(5))
      const radius = anchor?.length ? 95 + index % 3 * 24 : 130 + Math.sqrt(index) * 65
      instance.add({ group: 'nodes', data: record, position: saved || {
        x: centre.x + Math.cos(angle) * radius, y: centre.y + Math.sin(angle) * radius,
      } })
    })
    edges.forEach((edge) => {
      const record = { id: edge.id, source: edge.source, target: edge.target,
        relation: edge.relation, origin: edge.origin, reviewStatus: edge.review_status || '', edgeKind: knowledgeEdgeKind(edge), record: edge }
      const existing = instance.getElementById(edge.id)
      if (existing.length) {
        if (existing.data('source') !== edge.source || existing.data('target') !== edge.target) {
          existing.move({ source: edge.source, target: edge.target })
        }
        existing.data(record)
      } else instance.add({ group: 'edges', data: record })
    })
  })
  // Filtered-out positions are remembered for this book only, with a bounded cache.
  if (state.positions.size > 3000) state.positions = new Map([...state.positions].slice(-1500))
  if (state.views.size > 24) state.views = new Map([...state.views].slice(-24))
  return { reset, initialLayout: nodes.length > 0 && retained === 0 && remembered === 0,
    restored: Boolean(reset && saved?.viewport), added: nodes.length - retained, retained }
}

/** Presentation filters do not discard source records or alter what a relation means. */
export function visibleKnowledgeGraph(data: KnowledgeGraphData | null, edgeKinds: string[], showContent: boolean): KnowledgeGraphData | null {
  if (!data) return null
  const nodes = data.nodes.filter((node) => showContent || node.kind !== 'content')
  const ids = new Set(nodes.map((node) => node.id))
  return { ...data, nodes, edges: data.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target)
    && edgeKinds.includes(knowledgeEdgeKind(edge))) }
}

export function boundedGraphViewport(bounds: { x1: number; y1: number; w: number; h: number },
  width: number, height: number, padding = 68, maximumZoom = 1.15) {
  const zoom = Math.max(0.12, Math.min(maximumZoom,
    Math.max(1, width - padding * 2) / Math.max(1, bounds.w),
    Math.max(1, height - padding * 2) / Math.max(1, bounds.h)))
  return { zoom, pan: { x: width / 2 - (bounds.x1 + bounds.w / 2) * zoom,
    y: height / 2 - (bounds.y1 + bounds.h / 2) * zoom } }
}

export function fitKnowledgeGraph(instance: Core) {
  if (!instance.nodes().length) return
  instance.viewport(boundedGraphViewport(instance.nodes().boundingBox({ includeLabels: true }),
    instance.width(), instance.height()))
}

export function highlightKnowledgeGraph(instance: Core, highlighted: string[], evidence: string[], document = '') {
  const hitIds = new Set(highlighted)
  const evidenceIds = new Set(evidence)
  const matches = instance.nodes().filter((node) => {
    const record = node.data('record') as KnowledgeNode & { documents?: string[]; rel_paths?: string[] }
    return hitIds.has(node.id()) || Boolean(document && (record.document === document
      || record.documents?.includes(document) || record.rel_paths?.includes(document)))
  })
  const edges = instance.edges().filter((edge) => {
    const record = edge.data('record') as KnowledgeEdge & { document?: string }
    return (record.evidence_ids || []).length > 0 && (record.evidence_ids.some((id) => evidenceIds.has(id))
      || Boolean(document && record.document === document))
  })
  instance.batch(() => {
    instance.elements().removeClass('is-muted is-hit is-context is-evidence')
    if (!matches.length && !edges.length) return
    instance.elements().addClass('is-muted')
    matches.removeClass('is-muted').addClass('is-hit')
    edges.removeClass('is-muted').addClass('is-evidence')
    edges.connectedNodes().removeClass('is-muted').addClass('is-context')
  })
}
