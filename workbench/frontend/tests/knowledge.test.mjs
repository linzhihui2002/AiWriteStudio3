import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

async function importTypeScript(relativePath, replacement = '') {
  const source = await readFile(new URL(relativePath, import.meta.url), 'utf8')
  const modified = replacement ? source.replace("import { readSSE, request } from './client'", replacement) : source
  const { outputText } = ts.transpileModule(modified, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
  })
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
}
const { matchesKnowledgeBook, evidenceEditorUrl, evidenceSelection, sourceLabel, splitKnowledgeCitations } =
  await importTypeScript('../src/lib/knowledgeState.ts')

test('late responses and replaced project identities cannot be presented as the current book', () => {
  assert.equal(matchesKnowledgeBook({ project_id: 7, knowledge_key: 'book-a' }, 7, 'book-a'), true)
  assert.equal(matchesKnowledgeBook({ project_id: 8, knowledge_key: 'book-b' }, 7, 'book-a'), false)
  assert.equal(matchesKnowledgeBook({ project_id: 7, knowledge_key: 'restored-book' }, 7, 'book-a'), false)
  assert.equal(matchesKnowledgeBook({ project_id: 7, knowledge_key: '' }, 7), false)
})

test('evidence links encode book, source lines, version and names without losing Chinese or URL delimiters', () => {
  const path = '设定/角色 A & B.md'
  const link = new URL(evidenceEditorUrl(7, { rel_path: path, line_start: 12, line_end: 14,
    evidence_id: 'proof/a?b', document_hash: 'document-hash' }), 'http://localhost')
  assert.equal(link.pathname, '/project/7/editor')
  assert.equal(link.searchParams.get('path'), path)
  assert.equal(link.searchParams.get('line'), '12')
  assert.equal(link.searchParams.get('end_line'), '14')
  assert.equal(link.searchParams.get('hash'), 'document-hash')
  assert.equal(link.searchParams.get('evidence'), 'proof/a?b')
})

test('evidence selection maps full-file lines into an editor body with frontmatter', () => {
  const body = '第一行\n第二行\n第三行'
  const content = '---\n名字: 角色\n---\n' + body
  const [start, end] = evidenceSelection(body, content, 5, 6)
  assert.equal(body.slice(start, end), '第二行\n第三行')
  assert.deepEqual(evidenceSelection(body, body, 2, 2), [4, 7])
  assert.deepEqual(evidenceSelection(body, content, 1, 2), [0, 0])
  const clamped = evidenceSelection(body, body, 100, 101)
  assert.equal(body.slice(...clamped), '第三行')
})

test('graph source labels keep co-occurrence separate from relationships and uncertain AI evidence', () => {
  assert.equal(sourceLabel('cooccurrence'), '同章共现')
  assert.equal(sourceLabel('model'), '模型提取 · 待核实')
  assert.equal(sourceLabel('explicit'), '原文明确关系')
})

test('answer citation parsing keeps unsupported references and normal text intact', () => {
  assert.deepEqual(splitKnowledgeCitations('答应了她[1]，但伤势待核实[12]。'), [
    { text: '答应了她' }, { text: '[1]', citation: 1 }, { text: '，但伤势待核实' },
    { text: '[12]', citation: 12 }, { text: '。' },
  ])
})

const calls = []
globalThis.knowledgeTestRequest = async (path, options) => { calls.push({ path, options }); return {} }
globalThis.knowledgeTestSSE = async (...args) => { calls.push(args) }
const api = await importTypeScript('../src/api/knowledge.ts',
  'const request = globalThis.knowledgeTestRequest; const readSSE = globalThis.knowledgeTestSSE;')

test('all knowledge API requests carry the current book and preserve cancellation signals', async () => {
  calls.length = 0
  const abort = new AbortController()
  await api.getKnowledge(8, abort.signal)
  await api.getKnowledgeGraph(8, { document: '设定/人物.md', kinds: ['character', 'item'], center: 'id#a', limit: 300 }, abort.signal)
  await api.getKnowledgeEvidence(8, 'id/a?b', abort.signal)
  await api.searchKnowledge(8, { query: '他的承诺', mode: 'hybrid', profile: 'history', limit: 6 }, abort.signal)
  await api.saveBookVectorConfig(8, { mode: 'local' }, abort.signal)
  assert.equal(calls[0].path, '/projects/8/knowledge')
  const graphUrl = new URL(calls[1].path, 'http://localhost')
  assert.equal(graphUrl.searchParams.get('document'), '设定/人物.md')
  assert.equal(graphUrl.searchParams.get('kinds'), 'character,item')
  assert.equal(graphUrl.searchParams.get('center'), 'id#a')
  assert.equal(calls[2].path, '/projects/8/knowledge/evidence/id%2Fa%3Fb')
  assert.equal(calls[3].path, '/projects/8/retrieval/search')
  assert.equal(calls[4].path, '/projects/8/vector/model')
  calls.forEach((call) => assert.equal(call.options.signal, abort.signal))
})

test('knowledge SSE endpoint is scoped and delivers typed evidence before answer deltas', async () => {
  calls.length = 0
  const abort = new AbortController()
  const values = []
  await api.askKnowledge(9, { query: '找承诺', mode: 'hybrid', profile: 'review' }, (value) => values.push(value), abort.signal)
  const [path, body, onEvent, signal] = calls[0]
  assert.equal(path, '/projects/9/knowledge/ask')
  assert.equal(body.profile, 'review')
  assert.equal(signal, abort.signal)
  onEvent({ type: 'evidence', project_id: 9, knowledge_key: 'book-9', hits: [] })
  onEvent({ type: 'delta', text: '尚未找到依据' })
  assert.equal(values[0].project_id, 9)
  assert.equal(values[1].text, '尚未找到依据')
})

test('entity directory is independently paginated and optional content remains an explicit graph request', async () => {
  calls.length = 0
  const abort = new AbortController()
  await api.getKnowledgeEntities(8, { q: '林&雪', kind: 'character', offset: 300, limit: 100 }, abort.signal)
  await api.getKnowledgeGraph(8, { include_content: true, limit: 300 }, abort.signal)
  const entities = new URL(calls[0].path, 'http://localhost')
  assert.equal(entities.pathname, '/projects/8/knowledge/entities')
  assert.equal(entities.searchParams.get('q'), '林&雪')
  assert.equal(entities.searchParams.get('offset'), '300')
  assert.equal(entities.searchParams.get('kind'), 'character')
  assert.equal(new URL(calls[1].path, 'http://localhost').searchParams.get('include_content'), 'true')
  calls.forEach((call) => assert.equal(call.options.signal, abort.signal))
})

test('extraction, cancellation and candidate review preserve the selected book and structured edits', async () => {
  calls.length = 0
  const abort = new AbortController()
  await api.extractKnowledge(9, { paths: ['章节/第0003章.txt'], types: ['field', 'relation'], retry_job_id: 'job-a' }, abort.signal)
  await api.getKnowledgeExtractionJobs(9, abort.signal)
  await api.getKnowledgeExtractionJob(9, 'job/a', abort.signal)
  await api.cancelKnowledgeExtraction(9, 'job/a', abort.signal)
  await api.getKnowledgeCandidates(9, 'pending,conflict', abort.signal)
  const edit = { id: 'candidate-a', version: 3, decision: 'approve', value: '改为师徒', entity_anchor: 'person-a',
    target_entity_anchor: 'person-b', lifecycle: 'planned', chapter_start: 4, chapter_end: 6 }
  await api.reviewKnowledgeCandidates(9, [edit], abort.signal)
  assert.equal(calls[0].path, '/projects/9/knowledge/extract')
  assert.deepEqual(calls[0].options.body.paths, ['章节/第0003章.txt'])
  assert.equal(calls[1].path, '/projects/9/knowledge/jobs?limit=20')
  assert.equal(calls[2].path, '/projects/9/knowledge/jobs/job%2Fa')
  assert.equal(calls[3].path, '/projects/9/knowledge/jobs/job%2Fa/cancel')
  assert.equal(new URL(calls[4].path, 'http://localhost').searchParams.get('status'), 'pending,conflict')
  assert.equal(calls[5].path, '/projects/9/knowledge/candidates/review')
  assert.deepEqual(calls[5].options.body, { items: [edit] })
  calls.forEach((call) => assert.equal(call.options.signal, abort.signal))
})
