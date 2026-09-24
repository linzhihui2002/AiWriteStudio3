import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

// Use the installed TypeScript compiler and Node's test runner; no browser or new dependencies.
async function importTypeScript(relativePath) {
  const source = await readFile(new URL(relativePath, import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } })
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
}
const { applyChatEvent, runSteps, isChatRunTerminal, sessionPermission, formatQuestionResponse, mergeChatSnapshot, questionResponse, prepareChatContext, approvalReason } = await importTypeScript('../src/lib/chatState.ts')
const { readSSE, respondChatInteraction, updateChatMemory, regenerateChatTitle, getChatDefaults, putChatDefaults, createChatRun } = await importTypeScript('../src/api/client.ts')
const initialRun = () => ({ id: 'run-a', session_id: 12, status: 'running', text: '', last_seq: 0, changes: [] })
const event = (seq, kind, payload = {}) => ({ seq, event: kind, run_id: 'run-a', session_id: 12, ...payload })

test('replayed deltas are applied once and events from another run or session are ignored', () => {
  const first = applyChatEvent(initialRun(), event(1, 'delta', { text: '修仙' }))
  assert.equal(applyChatEvent(first, event(1, 'delta', { text: '修仙' })), first)
  assert.equal(applyChatEvent(first, event(2, 'delta', { run_id: 'other', text: '串话' })), first)
  assert.equal(applyChatEvent(first, event(2, 'delta', { session_id: 99, text: '串话' })), first)
  assert.equal(applyChatEvent(first, event(2, 'delta', { text: '世界' })).text, '修仙世界')
})

test('step completion retains its arguments, skill/agent classification, parent and timestamps', () => {
  let run = applyChatEvent(initialRun(), event(1, 'step', { call_id: 'skill-1', kind: 'skill', tool: 'load_skill', label: '章节规划', args: { name: 'novel-planning' }, started_at: '2026-09-22T01:00:00Z', parent_call_id: 'agent-1', status: 'running' }))
  run = applyChatEvent(run, event(2, 'step', { call_id: 'skill-1', status: 'done', result: '已载入章节规划', elapsed_ms: 450, finished_at: '2026-09-22T01:00:00.450Z' }))
  assert.equal(run.steps.length, 1)
  assert.equal(run.steps[0].kind, 'skill')
  assert.equal(run.steps[0].label, '章节规划')
  assert.deepEqual(run.steps[0].args, { name: 'novel-planning' })
  assert.equal(run.steps[0].elapsed_ms, 450)
  assert.equal(run.steps[0].result, '已载入章节规划')
  assert.equal(run.steps[0].parent_call_id, 'agent-1')
})

test('snapshot steps override older replay events and retain incomplete replay details', () => {
  const steps = runSteps({ ...initialRun(), events: [event(1, 'step', { call_id: 'a', tool: 'read_file', args: { path: '设定.md' }, status: 'running' })], steps: [{ call_id: 'a', index: 1, tool: 'read_file', status: 'done', result: '读取完成' }] })
  assert.equal(steps.length, 1)
  assert.equal(steps[0].status, 'done')
  assert.deepEqual(steps[0].args, { path: '设定.md' })
})

test('question survives waiting status and is updated in place after response', () => {
  const interaction = { id: 'q1', run_id: 'run-a', kind: 'question', status: 'pending', payload: { questions: [{ id: 'tone', question: '文风？', options: [{ id: 'a', label: '克制' }] }] } }
  let run = applyChatEvent(initialRun(), event(1, 'interaction', { interaction }))
  run = applyChatEvent(run, event(2, 'status', { status: 'waiting_input', message: '等待选择文风' }))
  assert.equal(isChatRunTerminal(run), false)
  assert.equal(run.status_message, '等待选择文风')
  run = applyChatEvent(run, event(3, 'interaction', { interaction: { ...interaction, status: 'answered', response: { answers: [{ id: 'tone', selected: [], custom: '轻松幽默' }] } } }))
  assert.equal(run.interactions.length, 1)
  assert.equal(run.interactions[0].status, 'answered')
  assert.equal(formatQuestionResponse(run.interactions[0]), '文风？：轻松幽默')
})

test('historical answer labels use option IDs and support free-text-only questions', () => {
  assert.equal(formatQuestionResponse({ payload: { questions: [{ id: 'q', header: '方向', options: [{ id: 'loot', label: '搜打撤' }] }] }, response: { answers: [{ id: 'q', selected: ['loot'], custom: '还要修仙' }] } }), '方向：搜打撤、还要修仙')
  assert.equal(formatQuestionResponse({ payload: { questions: [{ id: 'q', question: '主角叫什么？' }] }, response: { answers: [{ id: 'q', selected: [], custom: '陈墨' }] } }), '主角叫什么？：陈墨')
})

test('warnings de-duplicate, file changes update in place, and final text is authoritative', () => {
  let run = initialRun()
  run = applyChatEvent(run, event(1, 'context_warning', { message: '部分早期消息已压缩' }))
  run = applyChatEvent(run, event(2, 'context_warning', { message: '部分早期消息已压缩' }))
  run = applyChatEvent(run, event(3, 'file_change', { change: { id: 4, path: '章节/1.md', status: 'pending' } }))
  run = applyChatEvent(run, event(4, 'file_change', { change: { id: 4, path: '章节/1.md', status: 'applied' } }))
  assert.equal(run.context_warnings.length, 1)
  assert.equal(run.changes.length, 1)
  assert.equal(run.changes[0].status, 'applied')
  run = applyChatEvent(run, event(5, 'done', { ok: true, text: '修改已完成', changes: [] }))
  assert.equal(run.text, '修改已完成')
  assert.equal(isChatRunTerminal(run), true)
  assert.deepEqual(run.changes, [])
})

test('legacy session permissions migrate to an explicit choice without changing explicit settings', () => {
  assert.equal(sessionPermission({ mode: 'write', auto_apply: 1 }), 'auto')
  assert.equal(sessionPermission({ mode: 'read', auto_apply: 1 }), 'ask')
  assert.equal(sessionPermission({ mode: 'unknown', auto_apply: 1 }), 'ask')
  assert.equal(sessionPermission({ auto_apply: 1 }), 'ask')
  assert.equal(sessionPermission({ auto_apply: 0 }), 'ask')
  assert.equal(sessionPermission({ auto_apply: 0, permission_mode: 'full' }), 'full')
})

test('removing the open file as a reference retains an independent chapter target', () => {
  const context = prepareChatContext({ active_file: '设定/世界设定.md', base_hash: 'abc', selection: { text: '一段文字' }, target_chapter: '章节/旧目标.md' }, {
    includeCurrent: false, includeSelection: true, references: ['大纲/章纲.md'], targetChapterRel: '章节/第0001章.md',
  })
  assert.deepEqual(context, { target_chapter: '章节/第0001章.md', files: ['大纲/章纲.md'] })
  assert.deepEqual(prepareChatContext({ active_file: '设定/世界设定.md' }, {
    includeCurrent: true, includeSelection: false, references: ['设定/世界设定.md', '大纲/章纲.md'], targetChapterRel: null,
  }), { active_file: '设定/世界设定.md', files: ['大纲/章纲.md'] })
})

test('approval explanation reflects the running permission and dangerous operation', () => {
  const interaction = { payload: { operation: 'rewrite', path: '章节/第0001章.md' } }
  assert.match(approvalReason(interaction, 'auto'), /整篇覆盖/)
  assert.match(approvalReason(interaction, 'ask'), /逐次批准/)
  assert.equal(approvalReason({ payload: { ...interaction.payload, reason: '检测到版本冲突' } }, 'full'), '检测到版本冲突')
})

test('a late response snapshot cannot remove newer streamed text or restore an answered question', () => {
  const current = { ...initialRun(), text: '已经继续写作', last_seq: 9, interactions: [{ id: 'q1', status: 'answered' }] }
  const stale = { ...initialRun(), text: '请先选择', last_seq: 6, interactions: [{ id: 'q1', status: 'pending' }] }
  assert.equal(mergeChatSnapshot(current, stale), current)
  const fresh = { ...current, last_seq: 10, status: 'completed' }
  assert.deepEqual(mergeChatSnapshot(current, fresh), fresh)
  assert.equal(mergeChatSnapshot(null, stale), stale)
})

test('durable interaction revisions prevent stale cards from restoring pending state', () => {
  const answered = { id: 'q1', run_id: 'run-a', kind: 'question', status: 'answered', revision: 3, payload: {} }
  const pending = { ...answered, status: 'pending', revision: 1 }
  const current = { ...initialRun(), last_seq: 7, interactions: [answered] }
  const fromEvent = applyChatEvent(current, event(8, 'interaction', { interaction: pending }))
  assert.equal(fromEvent.last_seq, 8)
  assert.equal(fromEvent.interactions[0].status, 'answered')
  const snapshot = mergeChatSnapshot(current, { ...current, last_seq: 8, interactions: [pending] })
  assert.equal(snapshot.interactions[0].revision, 3)
  const finished = { ...current, status: 'completed' }
  assert.equal(mergeChatSnapshot(finished, { ...current, status: 'running' }).status, 'completed')
})

test('Other requires nonblank custom text and single-choice cannot include another choice', () => {
  const questions = [{ id: 'q', question: '方向', options: [{ id: 'a', label: '仙侠' }, { id: 'b', label: '悬疑' }] }]
  assert.equal(questionResponse(questions, {}), null)
  assert.equal(questionResponse(questions, { q: { selected: [], other: true, custom: '  ' } }), null)
  assert.equal(questionResponse(questions, { q: { selected: ['a'], other: true, custom: '修仙搜打撤' } }), null)
  assert.deepEqual(questionResponse(questions, { q: { selected: [], other: true, custom: ' 修仙搜打撤 ' } }), { answers: [{ id: 'q', selected: [], custom: '修仙搜打撤' }] })
  assert.deepEqual(questionResponse(questions, { q: { selected: ['a'], other: false, custom: '之前的其他草稿' } }), { answers: [{ id: 'q', selected: ['a'] }] })
})

test('multi-select may combine several choices and Other; unknown option IDs are rejected', () => {
  const questions = [{ id: 'q', question: '方向', multi_select: true, options: [{ id: 'a', label: '仙侠' }, { label: '悬疑' }] }]
  assert.deepEqual(questionResponse(questions, { q: { selected: ['a', '悬疑', 'a'], other: true, custom: '经营元素' } }), { answers: [{ id: 'q', selected: ['a', '悬疑'], custom: '经营元素' }] })
  assert.equal(questionResponse(questions, { q: { selected: ['不存在'], other: false, custom: '' } }), null)
})

test('free-text questions default to custom input and every question must be answered', () => {
  const questions = [{ id: 'name', question: '叫什么？' }, { id: 'style', question: '风格？', options: [{ label: '克制' }] }]
  assert.equal(questionResponse(questions, { name: { selected: [], other: true, custom: '陈墨' } }), null)
  assert.deepEqual(questionResponse(questions, { name: { selected: [], other: true, custom: '陈墨' }, style: { selected: ['克制'], other: false, custom: '' } }), { answers: [{ id: 'name', selected: [], custom: '陈墨' }, { id: 'style', selected: ['克制'] }] })
})

test('SSE decodes Chinese split at every byte, handles comments and a final unterminated event', async (context) => {
  const bytes = new TextEncoder().encode(': heartbeat\r\n\r\ndata: {"event":"delta","text":"修仙"}\r\n\r\ndata: invalid\n\ndata: {"event":"done","text":"完成"}')
  context.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({ start(controller) { for (const byte of bytes) controller.enqueue(new Uint8Array([byte])); controller.close() } })))
  const events = []
  await readSSE('/chat/runs/run-a/events', undefined, (value) => events.push(value), undefined, 'GET')
  assert.deepEqual(events, [{ event: 'delta', text: '修仙' }, { event: 'done', text: '完成' }])
})

test('SSE exposes partial output while the response stream is still open', async (context) => {
  let streamController
  const received = []
  context.mock.method(globalThis, 'fetch', async () => new Response(new ReadableStream({ start(controller) { streamController = controller } })))
  let onFirst
  const first = new Promise((resolve) => { onFirst = resolve })
  let finished = false
  const reading = readSSE('/chat/runs/run-a/events', undefined, (value) => { received.push(value); onFirst() }, undefined, 'GET').then(() => { finished = true })
  await new Promise((resolve) => setImmediate(resolve))
  streamController.enqueue(new TextEncoder().encode('data: {"event":"delta","text":"第一段"}\n\n'))
  await first
  assert.equal(finished, false)
  assert.equal(received[0].text, '第一段')
  streamController.enqueue(new TextEncoder().encode('data: {"event":"delta","text":"第二段"}\n\n'))
  streamController.close()
  await reading
  assert.equal(received.length, 2)
})

test('interaction, memory and title API calls use the agreed structured bodies', async (context) => {
  const calls = []
  context.mock.method(globalThis, 'fetch', async (url, options) => { calls.push({ url, options }); return new Response('{}', { headers: { 'Content-Type': 'application/json' } }) })
  await respondChatInteraction('run-a', 'approval/1', { decision: 'approve' })
  await respondChatInteraction('run-a', 'q1', { answers: [{ id: 'genre', selected: [], custom: '修仙搜打撤' }] })
  await updateChatMemory(5, 6, '主角务实')
  await regenerateChatTitle(12)
  assert.equal(calls[0].url, '/api/chat/runs/run-a/interactions/approval%2F1/respond')
  assert.deepEqual(JSON.parse(calls[0].options.body), { response: { decision: 'approve' } })
  assert.deepEqual(JSON.parse(calls[1].options.body), { response: { answers: [{ id: 'genre', selected: [], custom: '修仙搜打撤' }] } })
  assert.equal(calls[2].options.method, 'PATCH')
  assert.deepEqual(JSON.parse(calls[2].options.body), { content: '主角务实' })
  assert.equal(calls[3].url, '/api/chat/sessions/12/title/regenerate')
})

test('project chat defaults and explicit continuation use the agreed API bodies', async (context) => {
  const calls = []
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options })
    return new Response(JSON.stringify({ permission_mode: 'full', discussion_only: false, source: 'project' }), { headers: { 'Content-Type': 'application/json' } })
  })
  await getChatDefaults(8)
  await putChatDefaults(8, { permission_mode: 'full', discussion_only: false })
  await createChatRun(12, { client_request_id: 'req-1', text: '', context: { target_chapter: '章节/第0001章.md' }, continue_from_run_id: 'old-run' })
  assert.equal(calls[0].url, '/api/projects/8/chat-defaults')
  assert.equal(calls[1].options.method, 'PUT')
  assert.deepEqual(JSON.parse(calls[1].options.body), { permission_mode: 'full', discussion_only: false })
  assert.deepEqual(JSON.parse(calls[2].options.body), { client_request_id: 'req-1', text: '', context: { target_chapter: '章节/第0001章.md' }, continue_from_run_id: 'old-run' })
})

test('context provenance survives routing, completion and a reload snapshot', () => {
  const preview = {items:[{title:'本书已确认记忆',source:'.meta/chat-memory.json · 会话 5 消息 19'}],total_tokens:50,budget_tokens:1000}
  let run = applyChatEvent(initialRun(), event(1,'routing',{context_preview:preview,permission_mode:'ask',read_only:false}))
  run = applyChatEvent(run,event(2,'done',{status:'completed',text:'已完成',context_preview:preview}))
  assert.deepEqual(run.context_preview,preview)
  assert.equal(run.permission_mode,'ask')
  assert.equal(run.status_message,'')
  assert.deepEqual(mergeChatSnapshot(null,run).context_preview,preview)
})
