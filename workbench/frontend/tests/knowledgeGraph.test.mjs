import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import cytoscape from 'cytoscape'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/knowledgeGraphState.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
})
const { reconcileKnowledgeGraph, boundedGraphViewport, highlightKnowledgeGraph, knowledgeEdgeKind, visibleKnowledgeGraph } =
  await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

const node = (id, extra = {}) => ({ id, kind: 'character', label: id, source: 'author', document: '设定/人物.md',
  evidence_ids: [`proof-${id}`], ...extra })
const edge = (id, from, to, extra = {}) => ({ id, source: from, target: to, relation: '提及', origin: 'document',
  document: '章节/第0001章.txt', evidence_ids: [`proof-${id}`], ...extra })
const data = (nodes, edges = [], key = 'book-a') => ({ project_id: 1, knowledge_key: key, nodes, edges,
  count: nodes.length, truncated: false })
const graph = () => cytoscape({ headless: true, minZoom: 0.12, maxZoom: 3 })

test('incremental refresh preserves dragged positions, selection and author viewport', () => {
  const cy = graph()
  try {
    const initial = data([node('a'), node('b')], [edge('ab', 'a', 'b')])
    assert.equal(reconcileKnowledgeGraph(cy, initial, 'book-a').initialLayout, true)
    cy.$id('a').position({ x: 481, y: -98 }).select()
    cy.$id('b').position({ x: 101, y: 206 })
    cy.viewport({ zoom: 0.75, pan: { x: 123, y: -47 } })
    const change = reconcileKnowledgeGraph(cy, { ...initial, nodes: [node('a', { label: '改过的名字' }), node('b')] }, 'book-a')
    assert.equal(change.initialLayout, false)
    assert.deepEqual(cy.$id('a').position(), { x: 481, y: -98 })
    assert.equal(cy.$id('a').selected(), true)
    assert.equal(cy.$id('a').data('label'), '改过的名字')
    assert.deepEqual(cy.pan(), { x: 123, y: -47 })
    assert.equal(cy.zoom(), 0.75)
  } finally { cy.destroy() }
})

test('local expansion adds neighbours without re-laying out retained nodes', () => {
  const cy = graph()
  try {
    reconcileKnowledgeGraph(cy, data([node('a')]), 'book-a')
    cy.$id('a').position({ x: 600, y: 300 })
    const change = reconcileKnowledgeGraph(cy, data([node('a'), node('b')], [edge('ab', 'a', 'b')]), 'book-a')
    assert.equal(change.initialLayout, false)
    assert.deepEqual(cy.$id('a').position(), { x: 600, y: 300 })
    assert.ok(Math.hypot(cy.$id('b').position('x') - 600, cy.$id('b').position('y') - 300) < 150)
    assert.equal(cy.edges().length, 1)
  } finally { cy.destroy() }
})

test('filtering and restoring the same book remembers positions while removing absent evidence', () => {
  const cy = graph()
  try {
    const full = data([node('a'), node('b')], [edge('ab', 'a', 'b')])
    reconcileKnowledgeGraph(cy, full, 'book-a')
    cy.$id('a').position({ x: 40, y: 88 })
    cy.$id('b').position({ x: -150, y: -90 })
    reconcileKnowledgeGraph(cy, data([node('b')]), 'book-a')
    assert.equal(cy.$id('a').length, 0)
    assert.equal(cy.edges().length, 0)
    reconcileKnowledgeGraph(cy, full, 'book-a')
    assert.deepEqual(cy.$id('a').position(), { x: 40, y: 88 })
    assert.deepEqual(cy.$id('b').position(), { x: -150, y: -90 })
  } finally { cy.destroy() }
})

test('book switches drop positions, selection, classes and old edges even when names or IDs repeat', () => {
  const cy = graph()
  try {
    reconcileKnowledgeGraph(cy, data([node('a'), node('b')], [edge('ab', 'a', 'b')]), '1:book-a')
    cy.$id('a').position({ x: 9400, y: -8800 }).select().addClass('is-hit')
    cy.viewport({ zoom: 2.7, pan: { x: 900, y: -600 } })
    const change = reconcileKnowledgeGraph(cy, data([node('a')], [], 'book-b'), '2:book-b')
    assert.equal(change.reset, true)
    assert.equal(change.initialLayout, true)
    assert.equal(cy.elements().length, 1)
    assert.equal(cy.$id('a').selected(), false)
    assert.equal(cy.$id('a').hasClass('is-hit'), false)
    assert.notDeepEqual(cy.$id('a').position(), { x: 9400, y: -8800 })
    assert.equal(cy.zoom(), 1)
    assert.deepEqual(cy.pan(), { x: 0, y: 0 })
  } finally { cy.destroy() }
})

test('a small graph fits centred without magnifying nodes and labels three times', () => {
  const view = boundedGraphViewport({ x1: -50, y1: -20, w: 180, h: 110 }, 1000, 650)
  assert.equal(view.zoom, 1.15)
  assert.equal((40 * view.zoom) + view.pan.x, 500)
  assert.equal((35 * view.zoom) + view.pan.y, 325)
  const large = boundedGraphViewport({ x1: -1000, y1: -700, w: 2000, h: 1400 }, 1000, 650)
  assert.ok(large.zoom < 0.4)
})

test('document-origin mention edges are dashed mentions, not inferred character relationships', () => {
  assert.equal(knowledgeEdgeKind(edge('ab', 'a', 'b')), 'mention')
  assert.equal(knowledgeEdgeKind(edge('ab', 'a', 'b', { relation: '包含' })), 'structure')
  assert.equal(knowledgeEdgeKind(edge('ab', 'a', 'b', { relation: '师徒', origin: 'explicit' })), 'relationship')
  assert.equal(knowledgeEdgeKind(edge('ab', 'a', 'b', { relation: '计划结盟' })), 'planned')
})

test('search highlights only evidence-backed edges and preserves unrelated context as muted', () => {
  const cy = graph()
  try {
    reconcileKnowledgeGraph(cy, data([node('a'), node('b'), node('c')], [
      edge('ab', 'a', 'b'), edge('ac', 'a', 'c'), edge('bc', 'b', 'c', { evidence_ids: [] }),
    ]), 'book-a')
    highlightKnowledgeGraph(cy, ['a'], ['proof-ab', 'proof-bc'])
    assert.equal(cy.$id('a').hasClass('is-hit'), true)
    assert.equal(cy.$id('ab').hasClass('is-evidence'), true)
    assert.equal(cy.$id('b').hasClass('is-context'), true)
    assert.equal(cy.$id('ac').hasClass('is-evidence'), false)
    assert.equal(cy.$id('bc').hasClass('is-evidence'), false)
    assert.equal(cy.$id('c').hasClass('is-muted'), true)
    highlightKnowledgeGraph(cy, [], [])
    assert.equal(cy.elements('.is-muted, .is-hit, .is-context, .is-evidence').length, 0)
  } finally { cy.destroy() }
})

test('selecting a document highlights canonical nodes with multiple source documents', () => {
  const cy = graph()
  try {
    const path = '章节/第0001章.txt'
    reconcileKnowledgeGraph(cy, data([node('a', { documents: ['设定/人物.md', path] }), node('b')],
      [edge('ab', 'a', 'b')]), 'book-a')
    highlightKnowledgeGraph(cy, [], [], path)
    assert.equal(cy.$id('a').hasClass('is-hit'), true)
    assert.equal(cy.$id('ab').hasClass('is-evidence'), true)
    assert.equal(cy.$id('b').hasClass('is-hit'), false)
    assert.equal(cy.$id('b').hasClass('is-context'), true)
  } finally { cy.destroy() }
})

test('graph cap keeps only renderable edges and stale graph clearing removes all records', () => {
  const cy = graph()
  try {
    reconcileKnowledgeGraph(cy, data(Array.from({ length: 305 }, (_, i) => node(`n${i}`)),
      [edge('in', 'n0', 'n299'), edge('out', 'n0', 'n304')]), 'book-a')
    assert.equal(cy.nodes().length, 300)
    assert.equal(cy.edges().length, 1)
    reconcileKnowledgeGraph(cy, null, '')
    assert.equal(cy.elements().length, 0)
  } finally { cy.destroy() }
})

test('each book and scope restores its own dragged positions and viewport without leaking them to another view', () => {
  const cy = graph()
  try {
    const values = data([node('a'), node('b')])
    reconcileKnowledgeGraph(cy, values, '1:book-a', 'panorama')
    cy.$id('a').position({ x: 320, y: -50 })
    cy.viewport({ zoom: 0.65, pan: { x: 55, y: -99 } })
    assert.equal(reconcileKnowledgeGraph(cy, data([node('a')]), '1:book-a', 'document-a').initialLayout, true)
    cy.$id('a').position({ x: -140, y: 205 })
    cy.viewport({ zoom: 1.1, pan: { x: -45, y: 210 } })
    const restored = reconcileKnowledgeGraph(cy, values, '1:book-a', 'panorama')
    assert.equal(restored.restored, true)
    assert.equal(restored.initialLayout, false)
    assert.deepEqual(cy.$id('a').position(), { x: 320, y: -50 })
    assert.equal(cy.zoom(), 0.65)
    assert.deepEqual(cy.pan(), { x: 55, y: -99 })
    reconcileKnowledgeGraph(cy, data([node('a')], [], 'book-b'), '2:book-b', 'panorama')
    assert.notDeepEqual(cy.$id('a').position(), { x: 320, y: -50 })
    reconcileKnowledgeGraph(cy, data([node('a')]), '1:book-a', 'document-a')
    assert.deepEqual(cy.$id('a').position(), { x: -140, y: 205 })
    assert.equal(cy.zoom(), 1.1)
    assert.deepEqual(cy.pan(), { x: -45, y: 210 })
  } finally { cy.destroy() }
})

test('relation switches and content toggles hide presentation records without inventing dangling edges or changing source data', () => {
  const original = data([node('a'), node('b'), node('chunk', { kind: 'content' })], [
    edge('ab', 'a', 'b', { relation: '师徒', origin: 'explicit' }),
    edge('ac', 'a', 'chunk'), edge('bc', 'b', 'chunk', { relation: '包含' }),
    edge('planned', 'a', 'b', { relation: '计划结盟' }),
  ])
  const focused = visibleKnowledgeGraph(original, ['relationship'], false)
  assert.deepEqual(focused.nodes.map((value) => value.id), ['a', 'b'])
  assert.deepEqual(focused.edges.map((value) => value.id), ['ab'])
  assert.equal(original.nodes.length, 3)
  assert.equal(original.edges.length, 4)
  const content = visibleKnowledgeGraph(original, ['mention', 'structure', 'planned'], true)
  assert.deepEqual(content.edges.map((value) => value.id), ['ac', 'bc', 'planned'])
  assert.equal(visibleKnowledgeGraph(null, [], false), null)
})

test('reviewed model provenance does not turn draft or planned edges into current facts', () => {
  for (const lifecycle of ['draft', 'planned']) assert.equal(knowledgeEdgeKind(
    edge('suggested', 'a', 'b', { relation: '交付鱼符', origin: 'model', lifecycle, review_status: 'approved' })), 'planned')
  assert.equal(knowledgeEdgeKind(edge('occurred', 'a', 'b', { relation: '交付鱼符', origin: 'model',
    lifecycle: 'historical_event', review_status: 'approved' })), 'relationship')
})
