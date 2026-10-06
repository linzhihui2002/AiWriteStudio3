import type { ChatContext, ChatInteraction, ChatInteractionResponse, ChatRun } from '../api/types'

export interface ChatDraft {
  text: string
  references: string[]
  target?: string | null
  includeCurrent: boolean
  includeSelection: boolean
  selectionContext?: ChatContext
  revision: number
}
export const emptyChatDraft = (): ChatDraft => ({ text: '', references: [], includeCurrent: true, includeSelection: true, revision: 0 })
export const chatDraftStorageKey = (projectId: number, sessionId: number | null) => `aiw.chat.draft.${projectId}.${sessionId ?? 'new'}`

/** Only accept the draft fields we own; stored browser data is not trusted. */
export function parseChatDraft(raw: string | null): ChatDraft {
  if (!raw) return emptyChatDraft()
  try {
    const value = JSON.parse(raw)
    if (!value || typeof value !== 'object') return emptyChatDraft()
    return {
      text: typeof value.text === 'string' ? value.text : '',
      references: Array.isArray(value.references) ? value.references.filter((item: unknown): item is string => typeof item === 'string') : [],
      ...(typeof value.target === 'string' || value.target === null ? { target: value.target } : {}),
      includeCurrent: value.includeCurrent !== false,
      includeSelection: value.includeSelection !== false,
      revision: Number.isSafeInteger(value.revision) && value.revision >= 0 ? value.revision : 0,
      ...(value.selectionContext && typeof value.selectionContext.active_file === 'string'
        && typeof value.selectionContext.selection?.text === 'string'
        ? { selectionContext: { active_file: value.selectionContext.active_file,
          base_hash: typeof value.selectionContext.base_hash === 'string' ? value.selectionContext.base_hash : undefined,
          selection: { text: value.selectionContext.selection.text,
            start: Number.isInteger(value.selectionContext.selection.start) ? value.selectionContext.selection.start : undefined,
            end: Number.isInteger(value.selectionContext.selection.end) ? value.selectionContext.selection.end : undefined } } } : {}),
    }
  } catch { return emptyChatDraft() }
}

export function shouldClearSentDraft(sentRevision: number, currentRevision: number): boolean {
  return sentRevision === currentRevision
}

/** Network/5xx errors can recover; missing resources and permission errors need action. */
export function isRecoverableChatConnectionError(error: unknown): boolean {
  const status = error && typeof error === 'object' && 'status' in error ? Number(error.status) : 0
  return !status || status >= 500 || status === 408 || status === 429
}

export function chatSessionMatches(title: string, query: string): boolean {
  return title.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
}

export function freezeChatRequest<T>(payload: T): T {
  const snapshot = JSON.parse(JSON.stringify(payload)) as T
  const freeze = (value: unknown) => {
    if (!value || typeof value !== 'object') return
    Object.values(value).forEach(freeze)
    Object.freeze(value)
  }
  freeze(snapshot)
  return snapshot
}

export function isChatSendShortcut(event: { key: string; ctrlKey: boolean; metaKey: boolean; isComposing?: boolean; keyCode?: number }): boolean {
  return !event.isComposing && event.keyCode !== 229 && event.key === 'Enter' && (event.ctrlKey || event.metaKey)
}

export class ChatSubmissionUnconfirmed extends Error {
  readonly code = 'INTERACTION_UNCONFIRMED'
  constructor() { super('提交结果尚未确认，请先核对任务状态。') }
}

/** Acknowledgement is authoritative; a later optional refresh cannot undo it. */
export async function submitChatInteraction(
  interaction: ChatInteraction, response: ChatInteractionResponse,
  options: { verifyFirst: boolean; submit: () => Promise<ChatInteraction>; snapshot: () => Promise<ChatRun>;
    confirmed: (interaction: ChatInteraction) => void; refreshed: (run: ChatRun) => void; syncPending: () => void },
): Promise<void> {
  if (options.verifyFirst) {
    const run = await options.snapshot().catch(() => { throw new ChatSubmissionUnconfirmed() })
    const saved = run.interactions?.find((item) => item.id === interaction.id)
    if (!saved) throw new ChatSubmissionUnconfirmed()
    if (saved.status !== 'pending') { options.confirmed(saved); options.refreshed(run); return }
  }
  let acknowledged: ChatInteraction
  try { acknowledged = await options.submit() }
  catch (error) {
    if (!isRecoverableChatConnectionError(error)) throw error
    const run = await options.snapshot().catch(() => { throw new ChatSubmissionUnconfirmed() })
    const saved = run.interactions?.find((item) => item.id === interaction.id)
    if (!saved) throw new ChatSubmissionUnconfirmed()
    if (saved.status === 'pending') throw error
    acknowledged = saved
  }
  options.confirmed(acknowledged?.id ? acknowledged : { ...interaction, status: 'answered', response,
    revision: (interaction.revision || 0) + 1 })
  try { options.refreshed(await options.snapshot()) } catch { options.syncPending() }
}
