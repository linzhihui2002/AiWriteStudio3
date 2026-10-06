import { useEffect, useRef, useState } from 'react'
import {
  cancelChatRun, chatQuickCards, createChatRun, createChatSession, deleteChatSession,
  getChatDefaults, getChatRun, getChatSession, getWritingPrefs, listAgents, listChatSessions, listProviders,
  patchChatSession, putChatDefaults, putWritingPrefs, revertChatRun, streamChatRun, regenerateChatTitle, respondChatInteraction, readChapter,
} from '../api/client'
import type {
  AgentItem, ChatContext, ChatFileChange, ChatMessage, ChatRun, ChatRunEvent,
  ChatSession, ChatStep, Provider, ChatPermissionMode, ChatDefaults, ChatInteraction, ChatInteractionResponse,
  WritingPrefs, ChapterSummary,
} from '../api/types'
import { errorMessage, useToast } from './useToast'
import { useConfirm } from '../components/ConfirmDialog'
import { useSettings } from './useSettings'
import { applyChatEvent, CHAT_PERMISSION_OPTIONS, hasPersistedChatResult, isChatRunTerminal, mergeChatSnapshot, prepareChatContext, routingCard, runSteps, savedInteractions, sessionPermission } from '../lib/chatState'
import type { RoutingCard } from '../lib/chatState'
import { chatDraftStorageKey, isRecoverableChatConnectionError, parseChatDraft, shouldClearSentDraft, freezeChatRequest, submitChatInteraction, type ChatDraft } from '../lib/chatDraftState'
import type { ChatSelectionAttachment } from './projectChat'

export interface ChatControllerOptions {
  projectId: number
  chapterRel?: string | null
  targetChapterRel?: string | null
  selection?: string
  hasUnsavedChanges?: boolean
  availableFiles?: string[]
  availableChapters?: ChapterSummary[]
  portalHost?: HTMLElement | null
  presentation?: 'panel' | 'overlay' | 'page'
  attachment?: (ChatSelectionAttachment & { id: number }) | null
  onActivityChanged?: (status: string) => void
  prepareContext: () => Promise<ChatContext>
  onFilesChanged: (changes: ChatFileChange[]) => void | Promise<void>
  onOpenFile: (path: string) => void | Promise<void>
  onCollapse?: () => void
}

export const statusText = (status: string) => ({
  queued: '排队中', starting: '准备中', running: '正在处理', cancelling: '正在停止',
  completed: '已完成', succeeded: '已完成', done: '已完成', failed: '未完成',
  error: '未完成', cancelled: '已停止', interrupted: '已中断', waiting_input: '等待你回应',
  pending: '待处理', verified: '交付已验证', reported: '结果已报告', unverified: '待验证',
}[status] || status)
export const isTerminal = isChatRunTerminal
export const savedSteps = (value: unknown): ChatStep[] => Array.isArray(value) ? value as ChatStep[] : []
export const savedChanges = (value: unknown): ChatFileChange[] => Array.isArray(value) ? value as ChatFileChange[] : []

function waitForReconnect(signal: AbortSignal, ms: number) {
  return new Promise<void>((resolve) => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
    const timer = window.setTimeout(finish, ms)
    signal.addEventListener('abort', finish, { once: true })
    if (signal.aborted) finish()
  })
}

export default function useChatController({
  projectId, chapterRel, targetChapterRel, selection, hasUnsavedChanges, availableFiles = [],
  prepareContext, onFilesChanged, onOpenFile, onCollapse, availableChapters = [],
  portalHost, presentation = 'panel', attachment, onActivityChanged,
}: ChatControllerOptions) {
  const { push: notify } = useToast()
  const { settings, loading: settingsLoading } = useSettings()
  const [confirm, confirmNode] = useConfirm()
  const [renamingTitle, setRenamingTitle] = useState(false)
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [run, setRun] = useState<ChatRun | null>(null)
  const acceptedRunRef = useRef<ChatRun | null>(null)
  const [routing, setRouting] = useState<RoutingCard | null>(null)
  const [agents, setAgents] = useState<AgentItem[]>([])
  const [providers, setProviders] = useState<Provider[]>([])
  const [quickCards, setQuickCards] = useState<Array<{ key: string; label: string; prompt: string }>>([])
  const [input, setInput] = useState('')
  const [inputError, setInputError] = useState('')
  const [loading, setLoading] = useState(true)
  const [sessionsLoading, setSessionsLoading] = useState(true)
  const [sending, setSending] = useState(false)
  const [configuring, setConfiguring] = useState(false)
  const [reconnecting, setReconnecting] = useState(false)
  const [connectionError, setConnectionError] = useState('')
  const [subscriptionVersion, setSubscriptionVersion] = useState(0)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historySearch, setHistorySearch] = useState('')
  const [referencesOpen, setReferencesOpen] = useState(false)
  const [materialPreview, setMaterialPreview] = useState<{ path: string; content: string; hash?: string; mtime?: number; loading?: boolean; error?: string } | null>(null)
  const materialPreviewSequence = useRef(0)
  const [newContent, setNewContent] = useState(false)
  const [sendPhase, setSendPhase] = useState('')
  const [targetChoice, setTargetChoice] = useState<string | null | undefined>(undefined)
  const [selectionContext, setSelectionContext] = useState<ChatContext | undefined>()
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [chatDefaults, setChatDefaults] = useState<ChatDefaults | null>(null)
  const [writingPrefs, setWritingPrefs] = useState<WritingPrefs | null>(null)
  const [wordRangeDraft, setWordRangeDraft] = useState<{ min: number; max: number } | null>(null)
  const [defaultsLoading, setDefaultsLoading] = useState(true)
  const [savingDefaults, setSavingDefaults] = useState(false)
  const [draftPermission, setDraftPermission] = useState<ChatPermissionMode | null>(null)
  const [draftDiscussion, setDraftDiscussion] = useState<boolean | null>(null)
  const [draftModel, setDraftModel] = useState('')
  const [draftAgent, setDraftAgent] = useState('')
  const [references, setReferences] = useState<string[]>([])
  const [includeCurrent, setIncludeCurrent] = useState(true)
  const [includeSelection, setIncludeSelection] = useState(true)
  const [fileSearch, setFileSearch] = useState('')
  const [diff, setDiff] = useState<ChatFileChange | null>(null)
  const [reverting, setReverting] = useState<number | null>(null)
  const [memoryOpen, setMemoryOpen] = useState(false)
  const [sourceTarget, setSourceTarget] = useState<{ sessionId: number; messageId?: number | null } | null>(null)
  const [naming, setNaming] = useState(false)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const followBottomRef = useRef(true)
  const scrollPositionRef = useRef(0)
  const sendLockRef = useRef(false)
  const viewRef = useRef({ projectId, activeId })
  viewRef.current = { projectId, activeId }
  const callbacksRef = useRef({ onFilesChanged, prepareContext, onOpenFile })
  callbacksRef.current = { onFilesChanged, prepareContext, onOpenFile }
  const requestRef = useRef<{ sessionId: number; payload: Parameters<typeof createChatRun>[1]; revision: number; originSessionId: number | null } | null>(null)
  const [hasRetry, setHasRetry] = useState(false)
  const draftSessionRef = useRef<{ projectId: number; sessionId: number } | null>(null)
  const revisionRef = useRef(0)
  const draftIdentityRef = useRef<number | null>(null)
  const draftsReadyRef = useRef(false)
  const appliedAttachmentRef = useRef(0)
  const uncertainInteractions = useRef(new Set<string>())
  const actionsRef = useRef<{ send: (id?: string, retry?: boolean) => Promise<void>; respond: (interaction: ChatInteraction, response: ChatInteractionResponse) => Promise<void>; undo: (change: ChatFileChange) => Promise<void> } | null>(null)
  const currentDraftRef = useRef<ChatDraft>({ text: '', references: [], includeCurrent: true, includeSelection: true, revision: 0 })
  currentDraftRef.current = { text: input, references, target: targetChoice, includeCurrent, includeSelection, selectionContext, revision: revisionRef.current }
  const draftCache = useRef(new Map<string, ChatDraft>())
  const changeInput = (text: string) => { revisionRef.current += 1; setInput(text) }
  const changeTarget = (value: string | null | undefined) => { revisionRef.current += 1; setTargetChoice(value) }
  const actualTarget = targetChoice === undefined ? targetChapterRel : targetChoice
  const storeDraft = () => {
    if (!draftsReadyRef.current) return
    const key = chatDraftStorageKey(projectId, draftIdentityRef.current)
    const draft = { ...currentDraftRef.current, revision: revisionRef.current }
    draftCache.current.set(key, draft)
    try { localStorage.setItem(key, JSON.stringify(draft)) } catch { /* Memory remains available if browser storage is full. */ }
  }
  const restoreDraft = (id: number | null) => {
    const key = chatDraftStorageKey(projectId, id)
    let draft = draftCache.current.get(key)
    if (!draft) { try { draft = parseChatDraft(localStorage.getItem(key)) } catch { draft = parseChatDraft(null) } }
    draftIdentityRef.current = id
    draftsReadyRef.current = true
    revisionRef.current = draft.revision
    setInput(draft.text); setReferences(draft.references); setTargetChoice(draft.target)
    setIncludeCurrent(draft.includeCurrent); setIncludeSelection(draft.includeSelection)
    setSelectionContext(draft.selectionContext)
  }
  const current = sessions.find((item) => item.id === activeId)
  const steps = run ? runSteps(run) : []
  const busy = sending || Boolean(run && !isTerminal(run))
  const selectedModel = current ? (current.provider_id ? `${current.provider_id}::${current.model_id}` : '') : draftModel
  const selectedAgent = current ? (current.agent_pinned ? current.agent : '') : draftAgent
  const selectedPermission = current ? sessionPermission(current) : draftPermission ?? chatDefaults?.permission_mode ?? settings?.chat?.permission_mode ?? 'auto'
  const discussionOnly = current ? current.discussion_only ?? current.mode !== 'write' : draftDiscussion ?? chatDefaults?.discussion_only ?? settings?.chat?.discussion_only ?? false
  const runPermission = run?.permission_mode || (sending ? selectedPermission : null)
  const runDiscussionOnly = run?.discussion_only ?? run?.read_only ?? discussionOnly
  const permissionLabel = (mode?: ChatPermissionMode | null) => CHAT_PERMISSION_OPTIONS.find((option) => option.value === mode)?.label || '读取中'
  const modelLabel = selectedModel ? (providers.find((provider) => provider.provider_id === selectedModel.split('::')[0])?.display_name || selectedModel.split('::')[0]) + ` / ${selectedModel.split('::')[1] || '默认'}` : '默认模型'
  const pendingTitleIds = sessions.filter((item) => item.title_status === 'pending' || item.title_status === 'running'
    || item.id === activeId && busy && item.title_source === 'default' && item.title_status === 'idle').map((item) => item.id).join(',')

  useEffect(() => { storeDraft() }, [input, references, targetChoice, includeCurrent, includeSelection, selectionContext])
  useEffect(() => { onActivityChanged?.(busy ? statusText(run?.status || 'starting') : '') }, [busy, run?.status, onActivityChanged])
  useEffect(() => {
    if (!attachment || attachment.id === appliedAttachmentRef.current) return
    appliedAttachmentRef.current = attachment.id
    revisionRef.current += 1
    setSelectionContext({ active_file: attachment.path, base_hash: attachment.baseHash,
      selection: { text: attachment.text, start: attachment.start, end: attachment.end } })
    setIncludeCurrent(true); setIncludeSelection(true)
    inputRef.current?.focus()
  }, [attachment])

  useEffect(() => { setIncludeCurrent(true); setIncludeSelection(true) }, [projectId])
  useEffect(() => { setIncludeSelection(true) }, [selection])
  const [defaultsVersion, setDefaultsVersion] = useState(0)
  useEffect(() => {
    const changed = (event: Event) => {
      if ((event as CustomEvent<{ projectId: number }>).detail?.projectId === projectId) setDefaultsVersion(value => value + 1)
    }
    window.addEventListener('aiw:book-defaults-changed', changed)
    return () => window.removeEventListener('aiw:book-defaults-changed', changed)
  }, [projectId])

  useEffect(() => {
    let alive = true
    setChatDefaults(null)
    setWritingPrefs(null)
    setWordRangeDraft(null)
    setDefaultsLoading(true)
    void getChatDefaults(projectId).then((defaults) => { if (alive) setChatDefaults(defaults) })
      .catch((err) => { if (alive) setInputError(errorMessage(err)) })
      .finally(() => { if (alive) setDefaultsLoading(false) })
    void getWritingPrefs(projectId).then((prefs) => {
      if (!alive) return
      setWritingPrefs(prefs)
      setWordRangeDraft({ min: prefs.chapter_min_words, max: prefs.chapter_max_words })
    }).catch((err) => { if (alive) setInputError(errorMessage(err)) })
    return () => { alive = false }
  }, [projectId, defaultsVersion])

  useEffect(() => {
    let alive = true
    setSessionsLoading(true)
    setLoading(true)
    setSessions([])
    setActiveId(null)
    setMessages([])
    setRun(null)
    setRouting(null)
    setInput('')
    setReferences([])
    setDraftPermission(null)
    setDraftDiscussion(null)
    draftSessionRef.current = null
    void listChatSessions(projectId).then((items) => {
      if (!alive) return
      setSessions(items)
      const remembered = localStorage.getItem(`aiw.chat.session.${projectId}`)
      const next = remembered === 'new' ? null : items.find((item) => String(item.id) === remembered)?.id ?? items[0]?.id ?? null
      restoreDraft(next); setActiveId(next)
    }).catch((err) => { if (alive) setInputError(errorMessage(err)) })
      .finally(() => { if (alive) setSessionsLoading(false) })
    return () => { alive = false }
  }, [projectId])

  useEffect(() => {
    void chatQuickCards().then(setQuickCards).catch(() => undefined)
    void listAgents(true).then((data) => setAgents(data.agents)).catch(() => undefined)
    void listProviders().then((data) => setProviders(data.providers)).catch(() => undefined)
  }, [])

  // 标题生成在正文完成后继续；短轮询只跟踪仍在命名的会话。
  useEffect(() => {
    if (!pendingTitleIds) return
    let alive = true
    let attempts = 0
    let timer: number | undefined
    const poll = async () => {
      try {
        const latest = await listChatSessions(projectId)
        if (!alive) return
        setSessions((items) => items.map((item) => {
          const updated = latest.find((entry) => entry.id === item.id)
          if (item.title_source === 'user' && updated?.title_source !== 'user') return item
          return updated ? { ...item, title: updated.title, title_source: updated.title_source, title_status: updated.title_status } : item
        }))
      } catch { /* 下次重试，标题失败不影响正文 */ }
      if (alive && ++attempts < 60) timer = window.setTimeout(() => void poll(), 2500)
    }
    timer = window.setTimeout(() => void poll(), 1500)
    return () => { alive = false; window.clearTimeout(timer) }
  }, [projectId, pendingTitleIds])

  useEffect(() => {
    let alive = true
    const accepted = acceptedRunRef.current?.session_id === activeId ? acceptedRunRef.current : null
    if (accepted) acceptedRunRef.current = null
    setRun(accepted)
    setRouting(null)
    setMessages([])
    setInputError('')
    setReconnecting(false)
    setConnectionError('')
    followBottomRef.current = true
    if (!activeId) { setLoading(false); return () => { alive = false } }
    setLoading(true)
    void getChatSession(activeId).then(async (data) => {
      if (!alive) return
      setMessages(data.messages)
      setSessions((items) => items.map((item) => item.id === data.id ? data : item))
      if (data.active_run) {
        const snapshot = typeof data.active_run === 'string' ? await getChatRun(data.active_run) : data.active_run
        if (!alive) return
        setRun((item) => mergeChatSnapshot(item, snapshot))
      } else {
        const latest = [...data.messages].reverse().find((message) => message.role === 'assistant')
        const changes = savedChanges(latest?.meta.changes)
        if (changes.length) void Promise.resolve(callbacksRef.current.onFilesChanged(changes)).catch((err) => notify(errorMessage(err), 'error'))
      }
    }).catch((err) => { if (alive) setInputError(errorMessage(err)) })
      .finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [projectId, activeId])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'c') {
        event.preventDefault(); inputRef.current?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    const container = scrollRef.current
    if (container && followBottomRef.current) container.scrollTop = container.scrollHeight
    else if (container) setNewContent(true)
  }, [messages, run?.text, run?.changes, run?.steps, run?.last_seq])
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const container = scrollRef.current
      if (container) container.scrollTop = followBottomRef.current ? container.scrollHeight : scrollPositionRef.current
    })
    return () => window.cancelAnimationFrame(frame)
  }, [portalHost, presentation])

  useEffect(() => {
    if (loading || !sourceTarget || sourceTarget.sessionId !== activeId) return
    const element = scrollRef.current?.querySelector<HTMLElement>(`[data-message-id="${sourceTarget.messageId}"]`)
    if (element) { followBottomRef.current = false; element.scrollIntoView({ block: 'center', behavior: 'smooth' }) }
  }, [messages, loading, activeId, sourceTarget])

  // 切换会话只断开订阅；任务在服务端继续，重新打开会从快照/序号续接。
  useEffect(() => {
    if (!run || run.session_id !== activeId) return
    const controller = new AbortController()
    const runId = run.id
    const sessionId = activeId
    let cursor = Number(run.last_seq || 0)
    let finished = isTerminal(run)
    let failures = 0
    setConnectionError('')
    const valid = () => !controller.signal.aborted && viewRef.current.projectId === projectId && viewRef.current.activeId === sessionId
    const syncFiles = (changes: ChatFileChange[]) => {
      if (changes.length && valid()) void Promise.resolve(callbacksRef.current.onFilesChanged(changes)).catch((err) => notify(errorMessage(err), 'error'))
    }
    const applySnapshot = (snapshot: ChatRun) => {
      if (!valid()) return
      cursor = Math.max(cursor, Number(snapshot.last_seq || 0))
      const card = routingCard((snapshot as ChatRun & { routing?: unknown }).routing)
      if (card) setRouting(card)
      setRun((item) => mergeChatSnapshot(item, snapshot))
      finished = isTerminal(snapshot)
      if (finished) syncFiles(snapshot.changes || [])
    }
    const onEvent = (event: ChatRunEvent) => {
      if (!valid() || String(event.run_id) !== runId || Number(event.session_id) !== sessionId) return
      const seq = Number(event.seq || 0)
      if (seq && seq <= cursor) return
      cursor = Math.max(cursor, seq)
      setReconnecting(false)
      setRun((item) => item?.id === runId ? applyChatEvent(item, event) : item)
      if (event.event === 'routing') setRouting(routingCard(event))
      if (event.event === 'file_change') {
        const change = event.change as ChatFileChange
        if (!change) return
        syncFiles([change])
      } else if (event.event === 'title') {
        setSessions((items) => items.map((item) => item.id === sessionId ? { ...item, title: String(event.title || item.title), title_source: event.title_source as ChatSession['title_source'] || item.title_source, title_status: event.title_status as ChatSession['title_status'] || 'complete' } : item))
      } else if (event.event === 'done') {
        finished = true
        syncFiles(savedChanges(event.changes))
      }
    }
    void (async () => {
      if (finished) syncFiles(run.changes || [])
      while (valid() && !finished) {
        const streamController = new AbortController()
        const abortStream = () => streamController.abort()
        controller.signal.addEventListener('abort', abortStream, { once: true })
        let lastFrame = Date.now()
        const watchdog = window.setInterval(() => {
          if (Date.now() - lastFrame >= 30000) streamController.abort()
        }, 1000)
        try {
          await streamChatRun(runId, cursor, onEvent, streamController.signal, () => { lastFrame = Date.now(); setReconnecting(false) })
          if (!valid()) return
          const snapshot = await getChatRun(runId, controller.signal)
          applySnapshot(snapshot)
          failures = 0
        } catch (err) {
          if (!valid()) return
          if (!isRecoverableChatConnectionError(err)) { setConnectionError(errorMessage(err)); setReconnecting(false); return }
          failures += 1
          setReconnecting(true)
          // 事件连接断开后先核对持久化状态，再继续订阅；不重新发送任务。
          try { applySnapshot(await getChatRun(runId, controller.signal)) } catch (snapshotError) {
            if (!isRecoverableChatConnectionError(snapshotError)) { setConnectionError(errorMessage(snapshotError)); setReconnecting(false); return }
          }
        } finally { window.clearInterval(watchdog); controller.signal.removeEventListener('abort', abortStream) }
        if (!finished && valid()) await waitForReconnect(controller.signal, Math.min(1000 * 2 ** Math.min(failures, 4), 10000))
      }
      if (!valid()) return
      setReconnecting(false)
      try {
        const data = await getChatSession(sessionId)
        if (!valid()) return
        setMessages(data.messages)
        setSessions((items) => items.map((item) => item.id === sessionId ? data : item))
        // 消息已经落库后移除临时卡片，刷新后仍能看到相同的改动和结果。
        if (hasPersistedChatResult(data.messages, runId)) setRun(null)
      } catch { /* 保留当前结果，下次进入会话仍可读取 */ }
    })()
    return () => controller.abort()
    // 仅任务身份变化时重建订阅，delta 不触发断线。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, activeId, projectId, notify, subscriptionVersion])

  const selectSession = (id: number | null) => {
    if (sending) return
    storeDraft(); restoreDraft(id)
    localStorage.setItem(`aiw.chat.session.${projectId}`, id ? String(id) : 'new')
    setActiveId(id)
    setSourceTarget(null)
    if (id === null) { setDraftPermission(null); setDraftDiscussion(null) }
    requestRef.current = null
    setHasRetry(false)
    draftSessionRef.current = null
  }

  const updateSettings = async (patch: Partial<ChatSession>) => {
    if (!current) {
      if (patch.permission_mode) setDraftPermission(patch.permission_mode)
      if (patch.discussion_only !== undefined) setDraftDiscussion(patch.discussion_only)
      if (patch.provider_id !== undefined) setDraftModel(patch.provider_id ? `${patch.provider_id}::${patch.model_id || ''}` : '')
      if (patch.agent_pinned !== undefined) setDraftAgent(patch.agent_pinned ? patch.agent || '' : '')
      return
    }
    setConfiguring(true)
    try {
      const updated = await patchChatSession(current.id, patch)
      setSessions((items) => items.map((item) => item.id === updated.id ? updated : item))
    } catch (err) { setInputError(errorMessage(err)) }
    finally { setConfiguring(false) }
  }

  const updateDefaults = async (patch: Partial<Pick<ChatDefaults, 'permission_mode' | 'discussion_only'>>) => {
    if (savingDefaults || defaultsLoading) return
    setSavingDefaults(true)
    setInputError('')
    try {
      const updated = await putChatDefaults(projectId, {
        permission_mode: patch.permission_mode ?? chatDefaults?.permission_mode ?? settings?.chat?.permission_mode ?? 'auto',
        discussion_only: patch.discussion_only ?? chatDefaults?.discussion_only ?? settings?.chat?.discussion_only ?? false,
      })
      setChatDefaults(updated)
    } catch (err) { setInputError(errorMessage(err)) }
    finally { setSavingDefaults(false) }
  }

  /** 保存本书字数区间（书级覆盖）；下限须为正整数且小于上限、上限不超过 50000。 */
  const saveWordRange = async () => {
    const draft = wordRangeDraft
    if (!draft || savingDefaults) return
    if (!Number.isInteger(draft.min) || !Number.isInteger(draft.max)
      || draft.min <= 0 || draft.max <= draft.min || draft.max > 50000) {
      setInputError('字数区间无效：下限与上限须为正整数且下限小于上限，上限不超过 50000')
      return
    }
    setSavingDefaults(true)
    setInputError('')
    try {
      const updated = await putWritingPrefs(projectId, {
        chapter_min_words: draft.min,
        chapter_max_words: draft.max,
      })
      setWritingPrefs(updated)
      setWordRangeDraft({ min: updated.chapter_min_words, max: updated.chapter_max_words })
    } catch (err) { setInputError(errorMessage(err)) }
    finally { setSavingDefaults(false) }
  }

  const send = async (continueFromRunId?: string, retry = false) => {
    const text = continueFromRunId ? '' : input.trim()
    if ((!text && !continueFromRunId && !retry) || busy || loading || sessionsLoading || settingsLoading || defaultsLoading || configuring || sendLockRef.current) return
    sendLockRef.current = true
    setSending(true)
    setInputError('')
    const origin = { projectId, activeId }
    const sameView = () => viewRef.current.projectId === origin.projectId && viewRef.current.activeId === origin.activeId
    const sentRevision = revisionRef.current
    const frozenOptions = { includeCurrent, includeSelection, references: [...references], targetChapterRel: actualTarget }
    const contextProvider = callbacksRef.current.prepareContext
    const attachedContext = selectionContext
    try {
      let request = retry ? requestRef.current : null
      if (!request) {
        setSendPhase('正在保存并核对材料…')
        const prepared = await contextProvider()
        if (!sameView()) return
        const context = prepareChatContext(attachedContext ?? prepared, frozenOptions)
        let sessionId = activeId || (draftSessionRef.current?.projectId === projectId ? draftSessionRef.current.sessionId : null)
        if (continueFromRunId && !activeId) return
        const [provider = '', model = ''] = selectedModel.split('::')
        if (!sessionId) {
          const created = await createChatSession({ project_id: projectId,
            ...(draftPermission !== null ? { permission_mode: draftPermission } : {}),
            ...(draftDiscussion !== null ? { discussion_only: draftDiscussion } : {}),
            provider_id: provider, model_id: model, agent: draftAgent, agent_pinned: draftAgent ? 1 : 0 })
          sessionId = created.id
          draftSessionRef.current = { projectId, sessionId }
          if (!sameView()) return
          setSessions((items) => [created, ...items.filter((item) => item.id !== created.id)])
        }
        request = { sessionId, revision: sentRevision, originSessionId: activeId,
          payload: freezeChatRequest({ client_request_id: crypto.randomUUID(), text, context,
            ...(continueFromRunId ? { continue_from_run_id: continueFromRunId } : {}), provider, model, agent: selectedAgent }) }
        requestRef.current = request
      }
      setSendPhase(retry ? '正在确认上次发送…' : '正在提交任务…')
      const { sessionId } = request
      const started = await createChatRun(sessionId, request.payload)
      if (!sameView()) return
      requestRef.current = null
      acceptedRunRef.current = activeId === sessionId ? null : started
      setHasRetry(false)
      draftSessionRef.current = null
      if (!request.payload.continue_from_run_id && shouldClearSentDraft(request.revision, revisionRef.current)) {
        setInput(''); setSelectionContext(undefined)
      }
      setSourceTarget(null)
      followBottomRef.current = true
      setRouting(null)
      if (activeId === sessionId) {
        setRun(started)
        try {
          const data = await getChatSession(sessionId)
          if (!sameView()) return
          setMessages(data.messages)
        } catch { notify('任务已受理，消息记录正在同步。', 'info') }
      } else {
        localStorage.setItem(`aiw.chat.session.${projectId}`, String(sessionId))
        draftIdentityRef.current = sessionId
        const clearSentDraft = !request.payload.continue_from_run_id && shouldClearSentDraft(request.revision, revisionRef.current)
        const transferred = { ...currentDraftRef.current, ...(clearSentDraft ? { text: '', selectionContext: undefined } : {}) }
        draftCache.current.set(chatDraftStorageKey(projectId, sessionId), transferred)
        try { localStorage.setItem(chatDraftStorageKey(projectId, sessionId), JSON.stringify(transferred)) } catch { /* optional storage */ }
        try { localStorage.removeItem(chatDraftStorageKey(projectId, null)) } catch { /* optional storage */ }
        draftCache.current.delete(chatDraftStorageKey(projectId, null))
        setActiveId(sessionId)
      }
    } catch (err) { if (sameView()) { setInputError(errorMessage(err)); setHasRetry(Boolean(requestRef.current)) } }
    finally { sendLockRef.current = false; setSending(false); setSendPhase('') }
  }

  const stop = async () => {
    if (!run || isTerminal(run)) return
    const id = run.id
    setRun((item) => item?.id === id ? { ...item, status: 'cancelling' } : item)
    try {
      const snapshot = await cancelChatRun(id)
      setRun((item) => item?.id === id ? mergeChatSnapshot(item, snapshot) : item)
      if (isTerminal(snapshot)) {
        setSubscriptionVersion((value) => value + 1)
        void Promise.resolve(callbacksRef.current.onFilesChanged(snapshot.changes || [])).catch(() => notify('任务已停止，文件状态正在同步。', 'info'))
      }
    }
    catch (err) { setInputError(`停止请求未确认：${errorMessage(err)}。可再次点击停止。`) }
  }

  // 会话标题就地改名（替代原「弹窗输入标题」）
  const commitRename = async (title: string) => {
    setRenamingTitle(false)
    if (title) await updateSettings({ title })
  }
  const autoName = async () => {
    if (!current || naming) return
    const sessionId = current.id
    setNaming(true)
    setInputError('')
    try {
      await regenerateChatTitle(sessionId)
      const updated = await getChatSession(sessionId)
      setSessions((items) => items.map((item) => item.id === sessionId ? updated : item))
    } catch (err) { setInputError(errorMessage(err)) }
    finally { setNaming(false) }
  }

  const respond = async (interaction: ChatInteraction, response: ChatInteractionResponse) => {
    const sessionId = activeId
    const valid = () => viewRef.current.activeId === sessionId && viewRef.current.projectId === projectId
    try {
      await submitChatInteraction(interaction, response, {
        verifyFirst: uncertainInteractions.current.has(interaction.id),
        submit: () => respondChatInteraction(interaction.run_id, interaction.id, response),
        snapshot: () => getChatRun(interaction.run_id),
        confirmed: (confirmed) => {
          uncertainInteractions.current.delete(interaction.id)
          if (!valid()) return
          setRun((item) => item?.id === interaction.run_id ? { ...item, interactions: (item.interactions || []).map((entry) => entry.id === interaction.id ? confirmed : entry) } : item)
          setMessages((items) => items.map((message) => String(message.meta?.run_id) === interaction.run_id
            ? { ...message, meta: { ...message.meta, interactions: savedInteractions(message.meta.interactions).map((entry) => entry.id === interaction.id ? confirmed : entry) } } : message))
        },
        refreshed: (updated) => {
          if (!valid()) return
          setRun((item) => item?.id === updated.id ? mergeChatSnapshot(item, updated) : item)
          setMessages((items) => items.map((message) => String(message.meta?.run_id) === updated.id
            ? { ...message, meta: { ...message.meta, interactions: updated.interactions } } : message))
        },
        syncPending: () => notify('已提交回应，任务状态正在同步。', 'info'),
      })
    } catch (error) {
      if (error && typeof error === 'object' && 'code' in error && error.code === 'INTERACTION_UNCONFIRMED') uncertainInteractions.current.add(interaction.id)
      throw error
    }
  }
  const remove = async () => {
    if (!current || busy) return
    if (!await confirm({ title: '删除会话', message: `删除「${current.title}」及消息记录？已经写入的小说文件会保留。`, confirmText: '删除', danger: true })) return
    try {
      await deleteChatSession(current.id)
      setSessions((items) => items.filter((item) => item.id !== current.id))
      selectSession(null)
      notify('已删除会话', 'success')
    } catch (err) { setInputError(errorMessage(err)) }
  }

  const undo = async (change: ChatFileChange) => {
    if (reverting !== null || busy) return
    setReverting(change.id)
    try {
      await revertChatRun(change.run_id, [change.id])
      const updated = await getChatRun(change.run_id)
      await callbacksRef.current.onFilesChanged(updated.changes || [])
      setRun((item) => item?.id === updated.id ? updated : item)
      setMessages((items) => items.map((message) => String(message.meta?.run_id) === updated.id
        ? { ...message, meta: { ...message.meta, changes: updated.changes } } : message))
      setDiff((item) => item?.id === change.id ? updated.changes.find((entry) => entry.id === change.id) || item : item)
      notify('已撤回这项改动', 'success')
    } catch (err) { setInputError(errorMessage(err)) }
    finally { setReverting(null) }
  }
  actionsRef.current = { send, respond, undo }

  const previewMaterial = async (path: string) => {
    const sequence = ++materialPreviewSequence.current
    setMaterialPreview(current => ({ ...(current?.path === path ? current : { path, content: '' }), loading: true, error: undefined }))
    try {
      const file = await readChapter(projectId, path)
      if (sequence === materialPreviewSequence.current) setMaterialPreview({ path, content: file.body, hash: file.hash, mtime: file.mtime })
    } catch (error) { if (sequence === materialPreviewSequence.current) setMaterialPreview(current => ({ ...(current?.path === path ? current : { path, content: '' }), loading: false, error: errorMessage(error) })) }
  }

  return {
    confirmNode, renamingTitle, setRenamingTitle, sessions, activeId, messages,
    run, routing, agents, providers, quickCards, input,
    inputError, setInputError, loading, sessionsLoading, sending, configuring,
    reconnecting, connectionError, setSubscriptionVersion, historyOpen, setHistoryOpen, historySearch,
    setHistorySearch, referencesOpen, setReferencesOpen, materialPreview, setMaterialPreview, materialPreviewSequence,
    newContent, setNewContent, sendPhase, targetChoice, selectionContext, settingsOpen,
    setSettingsOpen, chatDefaults, writingPrefs, wordRangeDraft, setWordRangeDraft, defaultsLoading,
    savingDefaults, references, setReferences, includeCurrent, setIncludeCurrent, includeSelection,
    setIncludeSelection, fileSearch, setFileSearch, diff, setDiff, reverting,
    memoryOpen, setMemoryOpen, sourceTarget, setSourceTarget, naming, inputRef,
    scrollRef, followBottomRef, scrollPositionRef, callbacksRef, hasRetry, revisionRef,
    actionsRef, changeInput, changeTarget, actualTarget, current, steps,
    busy, selectedModel, selectedAgent, selectedPermission, discussionOnly, runPermission,
    runDiscussionOnly, permissionLabel, modelLabel, selectSession, updateSettings, updateDefaults,
    saveWordRange, send, stop, commitRename, autoName, respond,
    remove, undo, previewMaterial, notify, settings, settingsLoading,
    projectId, chapterRel, targetChapterRel, selection, hasUnsavedChanges, availableFiles,
    onCollapse, availableChapters, portalHost, presentation
  }
}

export type ChatController = ReturnType<typeof useChatController>
