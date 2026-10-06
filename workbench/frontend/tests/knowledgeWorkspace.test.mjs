import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/knowledgeState.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
})
const { knowledgeGraphScope, knowledgeGraphPresentation, knowledgeNodeDocuments, relatedKnowledgeNodes,
  knowledgeSelectionEvidenceIds, knowledgeDocumentGroup, knowledgeDocumentStatus, knowledgeNodeStatus, sourceLabel, knowledgeReviewInput,
  knowledgeCandidateEntityOptions, knowledgeCandidateEntityLabel, knowledgeDefinitionEvidence } =
  await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

const node = (id, extra = {}) => ({ id, label: id, kind: 'character', document: '设定/人物.md',
  source: 'author', evidence_ids: [`evidence-${id}`], ...extra })
const a = node('甲', { documents: ['设定/人物.md', '章节/第0001章.txt'] })
const b = node('乙', { document: '设定/物品.md', rel_paths: ['章节/第0002章.txt'] })
const chapter = node('章节1', { kind: 'chapter', document: '章节/第0001章.txt' })
const graph = { project_id: 8, knowledge_key: 'book-a', nodes: [chapter, b, a], edges: [], count: 3,
  truncated: false, document_nodes: { '状态/时间线.md': ['甲'] } }

test('definition evidence locates the selected entity inside a larger verified chunk and refuses mismatched source versions', () => {
  const text = '# 设定\n\n## 甲\n身份：守门人\n\n## 乙\n身份：账房'
  const hit = { evidence_id: 'e1', rel_path: '设定/人物.md', text, line_start: 1, line_end: 7, document_hash: 'h1' }
  const entity = node('甲', { description: '## 甲\n身份：守门人', source_location: {
    evidence_id: 'e1', rel_path: hit.rel_path, line_start: 3, line_end: 4, document_hash: 'h1',
  } })
  assert.deepEqual(knowledgeDefinitionEvidence(hit, entity), { ...hit, text: entity.description, line_start: 3, line_end: 4 })
  assert.equal(knowledgeDefinitionEvidence({ ...hit, document_hash: 'h2' }, entity).text, text)
  assert.equal(knowledgeDefinitionEvidence({ ...hit, evidence_id: 'unrelated' }, entity).text, text)
  assert.equal(knowledgeDefinitionEvidence({ ...hit, line_end: 3 }, entity).text, text)
})

test('the browser can request a local material scope and retain an explicit panorama scope', () => {
  const full = knowledgeGraphScope('book-a', '', false, [], '', '')
  const selected = knowledgeGraphScope('book-a', '设定/人物.md', false, [], '', '')
  assert.deepEqual(selected, full)
  const local = knowledgeGraphScope('book-a', '设定/人物.md', true, [], '', '')
  assert.equal(local.document, '设定/人物.md')
  assert.notEqual(local.key, full.key)
  assert.equal(knowledgeGraphScope('book-a', '', true, [], '', '').document, undefined)
  assert.equal(knowledgeGraphScope('book-a', '', false, ['item', 'character'], '', '').key,
    knowledgeGraphScope('book-a', '', false, ['character', 'item'], '', '').key)
  assert.notEqual(knowledgeGraphScope('book-a', '', false, [], '12', '').key, full.key)
  assert.notEqual(knowledgeGraphScope('book-a', '', false, [], '', 'entity-b').key, full.key)
  assert.notEqual(knowledgeGraphScope('book-a', '', false, [], '', '', true).key, full.key)
})

test('related nodes follow all document memberships and server document maps, with entities before source nodes', () => {
  assert.deepEqual(relatedKnowledgeNodes(graph, '章节/第0001章.txt').map((value) => value.id), ['甲', '章节1'])
  assert.deepEqual(relatedKnowledgeNodes(graph, '章节/第0002章.txt').map((value) => value.id), ['乙'])
  assert.deepEqual(relatedKnowledgeNodes(graph, '状态/时间线.md').map((value) => value.id), ['甲'])
  assert.deepEqual(knowledgeNodeDocuments(a), ['设定/人物.md', '章节/第0001章.txt'])
  assert.deepEqual(relatedKnowledgeNodes(null, ''), [])
})

test('zero search hits never fall back to the entire book graph or all nodes in the selected document', () => {
  assert.deepEqual(relatedKnowledgeNodes(graph, '', []), [])
  assert.deepEqual(relatedKnowledgeNodes(graph, '章节/第0001章.txt', []), [])
  assert.deepEqual(relatedKnowledgeNodes(graph, '', ['乙']).map((value) => value.id), ['乙'])
  assert.deepEqual(relatedKnowledgeNodes(graph, '章节/第0001章.txt', ['乙']), [])
  assert.equal(relatedKnowledgeNodes(graph, '').length, 3)
})

test('a document outside the capped panorama has no invented related nodes and remains available as an explicit local scope', () => {
  const selected = '设定/未入全景的人物.md'
  const capped = { ...graph, truncated: true, count: 411 }
  assert.deepEqual(relatedKnowledgeNodes(capped, selected), [])
  assert.equal(knowledgeGraphScope('book-a', selected, false, [], '', '').document, undefined)
  assert.equal(knowledgeGraphScope('book-a', selected, true, [], '', '').document, selected)
})

test('opening node detail requests its precise source evidence first, without duplicate requests', () => {
  assert.deepEqual(knowledgeSelectionEvidenceIds(node('甲', { evidence_ids: ['late', 'source', 'late'],
    source_location: { evidence_id: 'source', rel_path: '设定/人物.md', line_start: 18, line_end: 21 } })),
  ['source', 'late'])
  assert.deepEqual(knowledgeSelectionEvidenceIds({ id: 'edge', evidence_ids: ['p1', 'p1', 'p2'] }), ['p1', 'p2'])
})

test('scope changes preserve the same book canvas without publishing stale data as new-scope statistics', () => {
  const previous = knowledgeGraphPresentation(graph, 8, 'book-a', 'full', 'local')
  assert.equal(previous.rendered, graph)
  assert.equal(previous.current, null)
  assert.equal(previous.changingScope, true)
  const committed = knowledgeGraphPresentation(graph, 8, 'book-a', 'local', 'local')
  assert.equal(committed.current, graph)
  assert.equal(committed.changingScope, false)
})

test('late responses and restored book identities cannot populate even the retained canvas', () => {
  for (const [projectId, key] of [[9, 'book-a'], [8, 'book-b'], [8, undefined]]) {
    assert.deepEqual(knowledgeGraphPresentation(graph, projectId, key, 'full', 'full'),
      { rendered: null, current: null, changingScope: false })
  }
  assert.equal(knowledgeGraphPresentation(null, 8, 'book-a', '', 'full').rendered, null)
})

test('author sources, draft exclusions and planned foreshadowing are readable and do not imply past fact', () => {
  assert.equal(knowledgeDocumentGroup({ rel_path: '状态/伏笔追踪.md', kind: 'state' }), '状态与追踪')
  assert.equal(knowledgeDocumentGroup({ rel_path: '材料/已核实事实.md', kind: 'fact_card' }), '拆书事实卡')
  assert.equal(knowledgeDocumentStatus({ rel_path: '设定/人物.md', chunk_count: 3, index_status: 'indexed', status: 'author' }),
    '已索引 · 3 片段 · 作者材料')
  assert.equal(knowledgeDocumentStatus({ rel_path: '章节/第0003章.txt', chunk_count: 0, index_status: 'excluded', status: 'draft' }),
    '未纳入历史索引 · 草稿')
  assert.equal(knowledgeNodeStatus(node('伏笔', { planned: true, kind: 'foreshadow' })), '计划 · 未埋设')
  assert.equal(knowledgeNodeStatus(node('大纲事件', { planned: true, kind: 'event' })), '计划 · 尚未发生')
  assert.equal(knowledgeNodeStatus(node('已埋伏笔', { kind: 'foreshadow' })), '')
  assert.equal(sourceLabel('planned'), '计划关系')
  assert.equal(sourceLabel('model', 'approved'), '模型提取 · 已审核')
  assert.equal(knowledgeNodeStatus(node('草稿', { lifecycle: 'draft' })), '草稿 · 待定')
})

test('chapter material labels show the persisted Chinese status together with current index progress', () => {
  const document = { rel_path: '章节/第0003章.txt', kind: 'chapter', chunk_count: 2 }
  assert.equal(knowledgeDocumentStatus({ ...document, status: '草稿', index_status: 'excluded' }),
    '未纳入历史索引 · 草稿')
  assert.equal(knowledgeDocumentStatus({ ...document, status: '完成', index_status: 'pending' }),
    '等待索引 · 已完成')
  assert.equal(knowledgeDocumentStatus({ ...document, status: '发表', index_status: 'indexed' }),
    '已索引 · 2 片段 · 已发表')
  assert.equal(knowledgeDocumentStatus({ ...document, status: '发表', index_status: 'stale', index_label: '原文已变化，待同步' }),
    '原文已变化，待同步 · 已发表')
  assert.equal(knowledgeDocumentStatus({ ...document, status: 'published', index_status: 'indexed' }),
    '已索引 · 2 片段 · 已发表')
})

test('candidate review requires a deliberate entity choice, a relation target and valid chapter boundaries', () => {
  const candidate = { id: 'candidate-a', version: 5, kind: 'relation', lifecycle: 'setting_fact' }
  const edit = { value: '师徒', entity: 'person-a', target: 'person-b', lifecycle: 'setting_fact', start: '3', end: '8' }
  assert.deepEqual(knowledgeReviewInput(candidate, edit, 'approve'), { id: 'candidate-a', version: 5, decision: 'approve',
    value: '师徒', entity_anchor: 'person-a', target_entity_anchor: 'person-b', lifecycle: 'setting_fact', chapter_start: 3, chapter_end: 8 })
  assert.throws(() => knowledgeReviewInput(candidate, { ...edit, entity: '' }, 'approve'), /确认关联实体/)
  assert.throws(() => knowledgeReviewInput(candidate, { ...edit, target: '' }, 'approve'), /关系对象/)
  assert.throws(() => knowledgeReviewInput(candidate, { ...edit, start: '0' }, 'approve'), /正整数/)
  assert.throws(() => knowledgeReviewInput(candidate, { ...edit, start: '3.5' }, 'approve'), /正整数/)
  assert.throws(() => knowledgeReviewInput(candidate, { ...edit, end: '2' }, 'approve'), /早于/)
  assert.deepEqual(knowledgeReviewInput(candidate, { ...edit, target: '', start: 'bad' }, 'ignore'), {
    id: 'candidate-a', version: 5, decision: 'ignore' })
})

test('an explicit author review can confirm a draft or planned event as historical', () => {
  const edit = { value: '尚未发生的结盟', entity: 'person-a', target: 'person-b', lifecycle: 'historical_event', start: '', end: '' }
  for (const lifecycle of ['draft', 'planned']) assert.equal(knowledgeReviewInput({
    id: 'candidate-a', version: 1, kind: 'relation', lifecycle: 'setting_fact', source_lifecycle: lifecycle,
  }, edit, 'approve').lifecycle, 'historical_event')
})

test('ambiguous entity choices exclude the proposed-new placeholder and deduplicate only identical definition anchors', () => {
  const first = { anchor: 'definition-1', label: '沈砚', document: '设定/人物.md', kind: 'character', line_start: 5, fields: { 身份: '船医' } }
  const second = { ...first, anchor: 'definition-2', line_start: 18, fields: { 身份: '暗探' } }
  const placeholder = { ...first, anchor: 'new-person', line_start: undefined }
  const candidate = { entity: placeholder, entity_options: [first, second], requires_entity_choice: true,
    target_entity: placeholder, target_entity_options: [first, second], requires_target_entity_choice: true }
  assert.deepEqual(knowledgeCandidateEntityOptions(candidate).map((value) => value.anchor), ['definition-1', 'definition-2'])
  assert.deepEqual(knowledgeCandidateEntityOptions(candidate, [first], true).map((value) => value.anchor), ['definition-1', 'definition-2'])
  assert.match(knowledgeCandidateEntityLabel(first), /L5.*船医/)
  assert.match(knowledgeCandidateEntityLabel(second), /L18.*暗探/)
  assert.equal(knowledgeCandidateEntityOptions({ entity: placeholder, entity_options: [] })[0], placeholder)
})
