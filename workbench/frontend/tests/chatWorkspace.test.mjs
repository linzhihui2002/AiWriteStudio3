import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'

const require = createRequire(import.meta.url)
async function importTS(path, jsx = false) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  let { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2020, ...(jsx ? { jsx: ts.JsxEmit.ReactJSX } : {}) } })
  if (jsx) for (const name of ['react', 'react/jsx-runtime']) {
    outputText = outputText.replaceAll(`from "${name}"`, `from "${pathToFileURL(require.resolve(name)).href}"`)
      .replaceAll(`from '${name}'`, `from '${pathToFileURL(require.resolve(name)).href}'`)
  }
  return import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
}
const { chatDraftStorageKey, parseChatDraft, shouldClearSentDraft, isRecoverableChatConnectionError,
  freezeChatRequest, isChatSendShortcut, submitChatInteraction, ChatSubmissionUnconfirmed } = await importTS('../src/lib/chatDraftState.ts')
const { createChatRun, readSSE } = await importTS('../src/api/client.ts')
const { applyChatEvent, hasPersistedChatResult } = await importTS('../src/lib/chatState.ts')
const history = await importTS('../src/components/ChatHistoryMessage.tsx', true)

test('a terminal snapshot remains visible until the completed assistant result is persisted', () => {
  const user = { role: 'user', meta: { run_id: 'run-a', status: 'completed' } }
  const pending = { role: 'assistant', meta: { run_id: 'run-a', status: 'running' } }
  assert.equal(hasPersistedChatResult([user, pending], 'run-a'), false)
  assert.equal(hasPersistedChatResult([{ role: 'assistant', meta: { run_id: 'run-b', status: 'completed' } }], 'run-a'), false)
  assert.equal(hasPersistedChatResult([user, { ...pending, meta: { run_id: 'run-a', status: 'cancelled' } }], 'run-a'), true)
})

test('drafts preserve text, explicit cleared target, references and exact selection separately by book/session', () => {
  const draft = { text: '按第二个方案继续', references: ['设定/角色.md'], target: null, revision: 7,
    includeCurrent: true, includeSelection: false,
    selectionContext: { active_file: '章节/第0001章.txt', base_hash: 'v2', selection: { text: '只处理这段', start: 3, end: 8 } } }
  assert.deepEqual(parseChatDraft(JSON.stringify(draft)), draft)
  assert.notEqual(chatDraftStorageKey(1, 12), chatDraftStorageKey(2, 12))
  assert.notEqual(chatDraftStorageKey(1, null), chatDraftStorageKey(1, 12))
  assert.equal(parseChatDraft('broken').text, '')
  assert.deepEqual(parseChatDraft('{"references":[4,"设定/角色.md"],"revision":-1}').references, ['设定/角色.md'])
})

test('sending acknowledgement clears only its draft revision, leaving newly typed work intact', () => {
  assert.equal(shouldClearSentDraft(4, 4), true)
  assert.equal(shouldClearSentDraft(4, 5), false)
})

test('retry sends exactly its immutable accepted snapshot despite later text/target/reference changes', async (context) => {
  const original = { client_request_id: 'same-request', text: '改开头', context: { active_file: '章节/第0001章.txt',
    target_chapter: '章节/第0001章.txt', base_hash: 'v1', files: ['大纲/章纲.md'], selection: { text: '第一句', start: 0, end: 3 } } }
  const snapshot = freezeChatRequest(original)
  original.text = '新的要求'; original.context.target_chapter = '章节/第0002章.txt'
  original.context.selection.text = '另一句'; original.context.files.push('设定/角色.md')
  const calls = []
  context.mock.method(globalThis, 'fetch', async (_url, options) => {
    calls.push(JSON.parse(options.body))
    return new Response(JSON.stringify({ id: 'run-a', session_id: 12, status: 'queued' }))
  })
  await createChatRun(12, snapshot); await createChatRun(12, snapshot)
  assert.deepEqual(calls[0], calls[1])
  assert.equal(calls[1].context.target_chapter, '章节/第0001章.txt')
  assert.equal(calls[1].context.selection.text, '第一句')
  assert.deepEqual(calls[1].context.files, ['大纲/章纲.md'])
  assert.throws(() => { snapshot.context.files.push('越界新材料') }, TypeError)
})

test('Ctrl/Command Enter waits until Chinese composition completes', () => {
  const enter = { key: 'Enter', ctrlKey: true, metaKey: false }
  assert.equal(isChatSendShortcut({ ...enter, isComposing: true }), false)
  assert.equal(isChatSendShortcut({ ...enter, keyCode: 229 }), false)
  assert.equal(isChatSendShortcut(enter), true)
  assert.equal(isChatSendShortcut({ ...enter, ctrlKey: false, metaKey: true }), true)
  assert.equal(isChatSendShortcut({ ...enter, ctrlKey: false }), false)
})

test('stream heartbeat frames reset liveness without appearing as chat events', async (context) => {
  context.mock.method(globalThis, 'fetch', async () => new Response(': heartbeat\n\ndata: {"event":"delta","text":"正文"}\n\n'))
  let frames = 0; const events = []
  await readSSE('/chat/runs/a/events', undefined, (event) => events.push(event), undefined, 'GET', () => frames++)
  assert.equal(frames, 2); assert.equal(events.length, 1)
  assert.equal(isRecoverableChatConnectionError({ status: 404 }), false)
  assert.equal(isRecoverableChatConnectionError({ status: 403 }), false)
  assert.equal(isRecoverableChatConnectionError({ status: 503 }), true)
  assert.equal(isRecoverableChatConnectionError({ status: 0 }), true)
})

const pending = { id: 'approve-1', run_id: 'run-a', kind: 'approval', status: 'pending', revision: 1, payload: { path: '章节/第0001章.txt', operation: 'write' } }
const response = { decision: 'approve' }
const ack = { ...pending, status: 'answered', response, revision: 2 }
test('approval POST acknowledgement remains confirmed when following snapshot refresh fails', async () => {
  const events = []
  await submitChatInteraction(pending, response, { verifyFirst: false,
    submit: async () => { events.push('post'); return ack },
    snapshot: async () => { events.push('snapshot'); throw new Error('offline') },
    confirmed: (item) => { events.push('confirmed'); assert.equal(item.status, 'answered') },
    refreshed: () => assert.fail('no snapshot'), syncPending: () => events.push('sync-pending') })
  assert.deepEqual(events, ['post', 'confirmed', 'snapshot', 'sync-pending'])
})

test('unknown POST result must be checked before retry and already answered cards never post again', async () => {
  const noop = () => {}
  await assert.rejects(submitChatInteraction(pending, response, { verifyFirst: false,
    submit: async () => { throw new Error('network loss') }, snapshot: async () => { throw new Error('still offline') },
    confirmed: noop, refreshed: noop, syncPending: noop }), ChatSubmissionUnconfirmed)
  let posts = 0; let confirmed = false
  await submitChatInteraction(pending, response, { verifyFirst: true,
    submit: async () => { posts++; return ack },
    snapshot: async () => ({ id: 'run-a', interactions: [ack] }),
    confirmed: () => { confirmed = true }, refreshed: noop, syncPending: noop })
  assert.equal(posts, 0); assert.equal(confirmed, true)
})

test('checkpoint/completion/metrics and live permission events keep their authoritative shape', () => {
  const initial = { id: 'run-a', session_id: 12, status: 'running', last_seq: 0, text: '', changes: [] }
  const event = (seq, kind, payload) => ({ seq, event: kind, run_id: 'run-a', session_id: 12, ...payload })
  let run = applyChatEvent(initial, event(1, 'plan_state', { plan_state: { active_step: 1, steps: [{ step: 1, status: 'verified', receipts: [] }] } }))
  run = applyChatEvent(run, event(2, 'completion', { completion: { status: 'verified', receipts: [{ change_id: 3 }] } }))
  run = applyChatEvent(run, event(3, 'metrics', { metrics: { tool_calls: 2, active_ms: 42 } }))
  run = applyChatEvent(run, event(4, 'status', { status: 'running', permission_mode: 'ask', discussion_only: true }))
  assert.equal(run.plan_state.steps[0].status, 'verified')
  assert.equal(run.completion.status, 'verified'); assert.equal(run.metrics.active_ms, 42)
  assert.equal(run.permission_mode, 'ask'); assert.equal(run.discussion_only, true)
})

test('real historical memo comparator leaves 300 long messages at one render across 60 active deltas', () => {
  history.resetChatHistoryRenderCounts()
  let parses = 0
  const properties = Array.from({ length: 300 }, (_, id) => ({ message: { id, role: 'assistant', content: '正文'.repeat(2500),
    meta: { steps: Array.from({ length: 40 }, () => ({ status: 'done' })) }, created_at: '2026-10-03T01:00:00Z' },
    highlighted: false, busy: true, reverting: null,
    renderContent: () => { parses++; return null } }))
  for (const props of properties) history.default.type(props)
  for (let delta = 0; delta < 60; delta++) for (const props of properties) {
    const next = { ...props, renderContent: () => { parses++; return null } }
    if (!history.default.compare(props, next)) history.default.type(next)
  }
  assert.equal(parses, 300)
  assert.equal(Object.values(history.chatHistoryRenderCounts()).every((count) => count === 1), true)
  const changed = { ...properties[0], message: { ...properties[0].message, content: '新正文' } }
  assert.equal(history.default.compare(properties[0], changed), false)
  history.default.type(changed)
  assert.equal(history.chatHistoryRenderCounts()[0], 2)
})
