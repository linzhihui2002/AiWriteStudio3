import type { ChatContext, ChatInteraction, ChatRun, ChatRunEvent, ChatSession, ChatStep, ChatPermissionMode, ChatQuestion, ChatInteractionResponse } from '../api/types'

export const CHAT_PERMISSION_OPTIONS: Array<{ value: ChatPermissionMode; label: string; description: string }> = [
  { value: 'ask', label: '请求批准', description: '读取和讨论可直接进行；修改文件前由你批准。' },
  { value: 'auto', label: '帮我批准', description: '新建、局部修改自动批准；删除、移动和整篇覆盖仍请你确认。' },
  { value: 'full', label: '完全访问', description: '允许本项目内的文件操作；仍受项目边界、冲突检查和写作门禁约束。' },
]

export const TERMINAL_CHAT_STATUSES = new Set(['completed', 'succeeded', 'done', 'failed', 'error', 'cancelled', 'interrupted'])
export const isChatRunTerminal = (run: ChatRun | null) => Boolean(run && TERMINAL_CHAT_STATUSES.has(run.status))

export function sessionPermission(session: ChatSession): ChatPermissionMode {
  return session.permission_mode || (session.mode === 'write' && session.auto_apply === 1 ? 'auto' : 'ask')
}

export function prepareChatContext(context: ChatContext, options: {
  includeCurrent: boolean
  includeSelection: boolean
  references: string[]
  targetChapterRel?: string | null
}): ChatContext {
  const prepared = { ...context }
  if (!options.includeCurrent) {
    delete prepared.active_file
    delete prepared.base_hash
    delete prepared.selection
  } else if (!options.includeSelection) delete prepared.selection
  if (options.targetChapterRel !== undefined) {
    if (options.targetChapterRel) prepared.target_chapter = options.targetChapterRel
    else delete prepared.target_chapter
  }
  prepared.files = options.references.filter((path) => path !== prepared.active_file)
  return prepared
}

export function approvalReason(interaction: ChatInteraction, runPermission?: ChatPermissionMode): string {
  const explicit = interaction.payload.reason || interaction.payload.approval_reason
  if (typeof explicit === 'string' && explicit.trim()) return explicit
  const permission = runPermission || (['ask', 'auto', 'full'].includes(String(interaction.payload.permission_mode))
    ? interaction.payload.permission_mode as ChatPermissionMode : undefined)
  if (permission === 'ask') return '本轮按「请求批准」运行，文件修改需要你逐次批准。'
  if (permission === 'auto') {
    const operation = interaction.payload.operation
    if (operation === 'rewrite') return '本轮按「帮我批准」运行，整篇覆盖仍需你确认。'
    if (operation === 'delete' || operation === 'move' || operation === 'rename') return '本轮按「帮我批准」运行，删除或移动文件仍需你确认。'
    return '本轮按「帮我批准」运行，此操作需要你确认。'
  }
  if (permission === 'full') return '本轮按「完全访问」运行，此操作仍需要你确认后才能继续。'
  return '请核对文件路径和具体改动，再决定是否批准。'
}

export function mergeChatStep(items: ChatStep[], event: Partial<ChatStep> & { seq?: number }): ChatStep[] {
  const id = event.call_id || String(event.index ?? event.seq ?? items.length)
  const previous = items.find((step) => (step.call_id || String(step.index)) === id)
  const defined = Object.fromEntries(Object.entries(event).filter(([, value]) => value !== undefined))
  const step = {
    index: Number(event.index ?? event.seq ?? previous?.index ?? items.length),
    tool: '', label: '', args: {}, status: 'running', ...previous, ...defined, call_id: id,
  } as ChatStep
  if (!step.label) step.label = step.tool || '处理项目'
  return previous ? items.map((item) => item === previous ? step : item) : [...items, step]
}

export function runSteps(run: ChatRun): ChatStep[] {
  let steps: ChatStep[] = []
  // Events are the replay fallback. Explicit snapshot steps carry the latest state.
  for (const event of run.events || []) if (event.event === 'step') steps = mergeChatStep(steps, event)
  for (const step of run.steps || []) steps = mergeChatStep(steps, step)
  return steps
}

export const savedInteractions = (value: unknown): ChatInteraction[] => Array.isArray(value) ? value as ChatInteraction[] : []

export function mergeChatInteractions(current: ChatInteraction[] = [], incoming: ChatInteraction[] = []): ChatInteraction[] {
  const result = [...current]
  for (const interaction of incoming) {
    const index = result.findIndex((item) => item.id === interaction.id)
    if (index < 0) result.push(interaction)
    else if ((result[index].revision || 0) <= (interaction.revision || 0)) result[index] = { ...result[index], ...interaction }
  }
  return result
}

/** A response snapshot can arrive after newer SSE events; never roll the UI back. */
export function mergeChatSnapshot(current: ChatRun | null, snapshot: ChatRun): ChatRun {
  if (!current || current.id !== snapshot.id) return snapshot
  if (current.last_seq > snapshot.last_seq) return current
  const base = current.last_seq === snapshot.last_seq && isChatRunTerminal(current) ? current : snapshot
  return { ...base, interactions: mergeChatInteractions(current.interactions, snapshot.interactions) }
}

export interface ChatQuestionDraft { selected: string[]; other: boolean; custom: string }
export const questionDraft = (question: ChatQuestion, drafts: Record<string, ChatQuestionDraft>): ChatQuestionDraft =>
  drafts[question.id] || { selected: [], other: !question.options?.length, custom: '' }

export function questionResponse(questions: ChatQuestion[], drafts: Record<string, ChatQuestionDraft>): ChatInteractionResponse | null {
  if (!questions.length) return null
  const answers = []
  for (const question of questions) {
    const draft = questionDraft(question, drafts)
    const selected = [...new Set(draft.selected)]
    const allowed = new Set((question.options || []).map((option) => option.id || option.label))
    if (selected.some((id) => !allowed.has(id)) || (!selected.length && !draft.other)
      || (draft.other && !draft.custom.trim()) || (!question.multi_select && selected.length + Number(draft.other) !== 1)) return null
    answers.push({ id: question.id, selected, ...(draft.other ? { custom: draft.custom.trim() } : {}) })
  }
  return { answers }
}

/** Apply a durable event once, preserving rich step details from earlier events. */
export function applyChatEvent(run: ChatRun, event: ChatRunEvent): ChatRun {
  if (String(event.run_id) !== run.id || Number(event.session_id) !== run.session_id || event.seq <= (run.last_seq || 0)) return run
  const next = { ...run, last_seq: event.seq }
  if (event.event === 'delta') next.text = run.text + String(event.text || '')
  else if (event.event === 'step') next.steps = mergeChatStep(runSteps(run), event)
  else if (event.event === 'interaction' && event.interaction) {
    const interaction = event.interaction as ChatInteraction
    next.interactions = mergeChatInteractions(run.interactions, [interaction])
  } else if (event.event === 'routing') {
    if (event.context_preview) next.context_preview = event.context_preview as ChatRun['context_preview']
    if (typeof event.read_only === 'boolean') next.read_only = event.read_only
    if (['ask', 'auto', 'full'].includes(String(event.permission_mode))) next.permission_mode = event.permission_mode as ChatPermissionMode
  } else if (event.event === 'context_warning') {
    next.context_warnings = [...new Set([...(run.context_warnings || []), String(event.message || '')].filter(Boolean))]
  } else if (event.event === 'file_change' && event.change) {
    const change = event.change as ChatRun['changes'][number]
    next.changes = [...(run.changes || []).filter((item) => item.id !== change.id), change]
  } else if (event.event === 'status' || event.event === 'started') {
    next.status = String(event.status || 'running')
    next.status_message = typeof event.message === 'string' ? event.message : ''
  } else if (event.event === 'error') {
    next.error_code = String(event.code || '')
    next.error_message = String(event.message || '本轮未完成')
  } else if (event.event === 'done') {
    next.status_message = ''
    if (event.context_preview) next.context_preview = event.context_preview as ChatRun['context_preview']
    next.status = String(event.status || (event.ok ? 'completed' : 'failed'))
    if (typeof event.text === 'string') next.text = event.text
    if (Array.isArray(event.changes)) next.changes = event.changes as ChatRun['changes']
    if (Array.isArray(event.interactions)) next.interactions = mergeChatInteractions(run.interactions, event.interactions as ChatInteraction[])
  }
  return next
}

export function formatQuestionResponse(interaction: ChatInteraction): string {
  if (!interaction.response || !('answers' in interaction.response)) return ''
  return interaction.response.answers.map((answer) => {
    const question = interaction.payload.questions?.find((item) => item.id === answer.id)
    const selected = answer.selected.map((id) => question?.options?.find((option) => (option.id || option.label) === id)?.label || id)
    return `${question?.header || question?.question || answer.id}：${[...selected, answer.custom].filter(Boolean).join('、')}`
  }).join('\n')
}
