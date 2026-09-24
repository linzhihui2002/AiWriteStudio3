/** 对话写作助手：服务端任务、可续接的事件流、文件改动与编辑器上下文。 */
import { useEffect, useRef, useState } from 'react'
import {
  cancelChatRun, chatQuickCards, createChatRun, createChatSession, deleteChatSession,
  getChatDefaults, getChatRun, getChatSession, listAgents, listChatSessions, listProviders,
  patchChatSession, putChatDefaults, revertChatRun, streamChatRun, regenerateChatTitle, respondChatInteraction,
} from '../api/client'
import type {
  AgentItem, ChatContext, ChatFileChange, ChatMessage, ChatRun, ChatRunEvent,
  ChatSession, ChatStep, Provider, ChatPermissionMode, ChatDefaults, ChatInteraction, ChatInteractionResponse, ContextPreview,
} from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import { useConfirm } from './ConfirmDialog'
import { usePrompt } from './PromptDialog'
import MarkdownView from './MarkdownView'
import DiffView from './DiffView'
import Modal from './Modal'
import { NO_AUTOFILL } from '../lib/autofill'
import { useSettings } from '../state/useSettings'
import { applyChatEvent, CHAT_PERMISSION_OPTIONS, isChatRunTerminal, mergeChatSnapshot, prepareChatContext, runSteps, savedInteractions, sessionPermission } from '../lib/chatState'
import ChatInteractionCard from './ChatInteractionCard'
import ChatMemoryPanel from './ChatMemoryPanel'

export interface ChatPanelProps {
  projectId: number
  chapterRel?: string | null
  targetChapterRel?: string | null
  selection?: string
  hasUnsavedChanges?: boolean
  availableFiles?: string[]
  prepareContext: () => Promise<ChatContext>
  onFilesChanged: (changes: ChatFileChange[]) => void | Promise<void>
  onOpenFile: (path: string) => void | Promise<void>
  onCollapse?: () => void
}

const statusText = (status: string) => ({
  queued: '排队中', starting: '准备中', running: '正在处理', cancelling: '正在停止',
  completed: '已完成', succeeded: '已完成', done: '已完成', failed: '未完成',
  error: '未完成', cancelled: '已停止', interrupted: '已中断', waiting_input: '等待你回应',
}[status] || status)
const isTerminal = isChatRunTerminal
const savedSteps = (value: unknown): ChatStep[] => Array.isArray(value) ? value as ChatStep[] : []
const savedChanges = (value: unknown): ChatFileChange[] => Array.isArray(value) ? value as ChatFileChange[] : []

function waitForReconnect(signal: AbortSignal, ms: number) {
  return new Promise<void>((resolve) => {
    const finish = () => { clearTimeout(timer); signal.removeEventListener('abort', finish); resolve() }
    const timer = window.setTimeout(finish, ms)
    signal.addEventListener('abort', finish, { once: true })
    if (signal.aborted) finish()
  })
}

export default function ChatPanel({
  projectId, chapterRel, targetChapterRel, selection, hasUnsavedChanges, availableFiles = [],
  prepareContext, onFilesChanged, onOpenFile, onCollapse,
}: ChatPanelProps) {
  const { push: notify } = useToast()
  const { settings, loading: settingsLoading } = useSettings()
  const [confirm, confirmNode] = useConfirm()
  const [prompt, promptNode] = usePrompt()
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeId, setActiveId] = useState<number | null>(null)
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [run, setRun] = useState<ChatRun | null>(null)
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
  const [expanded, setExpanded] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(() => localStorage.getItem('aiw.chat.settings.open') === 'true')
  const [chatDefaults, setChatDefaults] = useState<ChatDefaults | null>(null)
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
  const sendLockRef = useRef(false)
  const viewRef = useRef({ projectId, activeId })
  viewRef.current = { projectId, activeId }
  const callbacksRef = useRef({ onFilesChanged, prepareContext, onOpenFile })
  callbacksRef.current = { onFilesChanged, prepareContext, onOpenFile }
  const requestRef = useRef<{ fingerprint: string; id: string } | null>(null)
  const draftSessionRef = useRef<{ projectId: number; sessionId: number } | null>(null)
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

  useEffect(() => { setIncludeCurrent(true); setIncludeSelection(true) }, [projectId])
  useEffect(() => { setIncludeSelection(true) }, [selection])

  useEffect(() => {
    let alive = true
    setChatDefaults(null)
    setDefaultsLoading(true)
    void getChatDefaults(projectId).then((defaults) => { if (alive) setChatDefaults(defaults) })
      .catch((err) => { if (alive) setInputError(errorMessage(err)) })
      .finally(() => { if (alive) setDefaultsLoading(false) })
    return () => { alive = false }
  }, [projectId])

  useEffect(() => {
    let alive = true
    setSessionsLoading(true)
    setLoading(true)
    setSessions([])
    setActiveId(null)
    setMessages([])
    setRun(null)
    setInput('')
    setReferences([])
    setDraftPermission(null)
    setDraftDiscussion(null)
    draftSessionRef.current = null
    void listChatSessions(projectId).then((items) => {
      if (!alive) return
      setSessions(items)
      const remembered = localStorage.getItem(`aiw.chat.session.${projectId}`)
      setActiveId(remembered === 'new' ? null : items.find((item) => String(item.id) === remembered)?.id ?? items[0]?.id ?? null)
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
    setRun(null)
    setMessages([])
    setInputError('')
    setReconnecting(false)
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
        setRun(snapshot)
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
      if (event.key === 'Escape') setExpanded(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  useEffect(() => {
    const container = scrollRef.current
    if (container && followBottomRef.current) container.scrollTop = container.scrollHeight
  }, [messages, run?.text, run?.changes, steps])

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
    const valid = () => !controller.signal.aborted && viewRef.current.projectId === projectId && viewRef.current.activeId === sessionId
    const syncFiles = (changes: ChatFileChange[]) => {
      if (changes.length && valid()) void Promise.resolve(callbacksRef.current.onFilesChanged(changes)).catch((err) => notify(errorMessage(err), 'error'))
    }
    const applySnapshot = (snapshot: ChatRun) => {
      if (!valid()) return
      cursor = Math.max(cursor, Number(snapshot.last_seq || 0))
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
        try {
          await streamChatRun(runId, cursor, onEvent, controller.signal)
          if (!valid()) return
          const snapshot = await getChatRun(runId, controller.signal)
          applySnapshot(snapshot)
          failures = 0
        } catch (err) {
          if (!valid()) return
          failures += 1
          setReconnecting(true)
          // 事件连接断开后先核对持久化状态，再继续订阅；不重新发送任务。
          try { applySnapshot(await getChatRun(runId, controller.signal)) } catch { /* 下次重连继续 */ }
        }
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
        if (data.messages.some((message) => String(message.meta?.run_id) === runId)) setRun(null)
      } catch { /* 保留当前结果，下次进入会话仍可读取 */ }
    })()
    return () => controller.abort()
    // 仅任务身份变化时重建订阅，delta 不触发断线。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [run?.id, activeId, projectId, notify])

  const selectSession = (id: number | null) => {
    if (sending) return
    localStorage.setItem(`aiw.chat.session.${projectId}`, id ? String(id) : 'new')
    setActiveId(id)
    setInput('')
    setReferences([])
    setSourceTarget(null)
    if (id === null) { setDraftPermission(null); setDraftDiscussion(null) }
    requestRef.current = null
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

  const send = async (continueFromRunId?: string) => {
    const text = continueFromRunId ? '' : input.trim()
    if ((!text && !continueFromRunId) || busy || loading || sessionsLoading || settingsLoading || defaultsLoading || configuring || sendLockRef.current) return
    sendLockRef.current = true
    setSending(true)
    setInputError('')
    const origin = { projectId, activeId }
    const sameView = () => viewRef.current.projectId === origin.projectId && viewRef.current.activeId === origin.activeId
    try {
      const prepared = await callbacksRef.current.prepareContext()
      if (!sameView()) return
      const context = prepareChatContext(prepared, { includeCurrent, includeSelection, references, targetChapterRel })
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
      const fingerprint = JSON.stringify({ sessionId, text, continueFromRunId, provider, model, agent: selectedAgent })
      if (requestRef.current?.fingerprint !== fingerprint) requestRef.current = { fingerprint, id: crypto.randomUUID() }
      const started = await createChatRun(sessionId, { client_request_id: requestRef.current.id, text, context,
        ...(continueFromRunId ? { continue_from_run_id: continueFromRunId } : {}), provider, model, agent: selectedAgent })
      if (!sameView()) return
      requestRef.current = null
      draftSessionRef.current = null
      if (!continueFromRunId) setInput('')
      setSourceTarget(null)
      followBottomRef.current = true
      if (activeId === sessionId) {
        setRun(started)
        const data = await getChatSession(sessionId)
        if (!sameView()) return
        setMessages(data.messages)
      } else {
        localStorage.setItem(`aiw.chat.session.${projectId}`, String(sessionId))
        setActiveId(sessionId)
      }
    } catch (err) { if (sameView()) setInputError(errorMessage(err)) }
    finally { sendLockRef.current = false; setSending(false) }
  }

  const stop = async () => {
    if (!run || isTerminal(run)) return
    const id = run.id
    setRun((item) => item?.id === id ? { ...item, status: 'cancelling' } : item)
    try { await cancelChatRun(id) }
    catch (err) { setInputError(`停止请求未确认：${errorMessage(err)}。可再次点击停止。`) }
  }

  const rename = async () => {
    if (!current) return
    const title = await prompt({ title: '重命名会话', label: '会话标题', defaultValue: current.title })
    if (title?.trim()) await updateSettings({ title: title.trim() })
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
    await respondChatInteraction(interaction.run_id, interaction.id, response)
    const updated = await getChatRun(interaction.run_id)
    if (viewRef.current.activeId !== sessionId || viewRef.current.projectId !== projectId) return
    setRun((item) => item?.id === updated.id ? mergeChatSnapshot(item, updated) : item)
    setMessages((items) => items.map((message) => String(message.meta?.run_id) === updated.id
      ? { ...message, meta: { ...message.meta, interactions: updated.interactions } } : message))
  }
  const remove = async () => {
    if (!current || busy) return
    if (!await confirm({ title: '删除会话', message: `删除「${current.title}」及消息记录？已经写入的小说文件会保留。`, confirmText: '删除', danger: true })) return
    try {
      await deleteChatSession(current.id)
      setSessions((items) => items.filter((item) => item.id !== current.id))
      selectSession(null)
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

  const renderSteps = (items: ChatStep[], live = false) => items.length > 0 ? (
    <details className="chat__activity" open={live && busy || undefined}>
      <summary>处理过程 · {items.length} 步</summary>
      <div className="chat__steps">{items.map((step, index) => (
        <details key={step.call_id || `${step.index}-${index}`} className={`chat__step chat__step--${step.status}${step.parent_call_id ? ' chat__step--child' : ''}`}>
          <summary><span className="chat__step-icon">{step.status === 'running' ? '⟳' : step.status === 'done' ? '✓' : step.status === 'cancelled' ? '−' : '!'}</span> <span className="chat__step-kind">{({ tool: '工具', skill: '技能', agent: 'Agent', phase: '阶段' } as Record<string, string>)[step.kind || 'tool'] || '步骤'}</span> {step.label || step.tool}
            {step.elapsed_ms !== undefined ? <span className="chat__step-duration">{(step.elapsed_ms / 1000).toFixed(1)} 秒</span> : null}
            <span className="chat__step-summary">{step.summary || (step.status === 'running' ? '进行中' : '')}</span></summary>
          <div className="chat__step-details">{step.tool ? <div className="muted">调用：{step.tool}</div> : null}{step.started_at ? <div className="muted">开始：{new Date(step.started_at).toLocaleTimeString()}{step.finished_at ? ` · 结束：${new Date(step.finished_at).toLocaleTimeString()}` : ''}</div> : null}
            {Object.keys(step.args || {}).length > 0 ? <><strong>输入</strong><pre className="chat__tool-detail">{JSON.stringify(step.args, null, 2)}</pre></> : null}
            {step.result !== undefined && step.result !== null ? <><strong>结果</strong><pre className="chat__tool-detail">{typeof step.result === 'string' ? step.result : JSON.stringify(step.result, null, 2)}</pre></> : null}</div>
        </details>
      ))}</div>
    </details>
  ) : null

  const renderChanges = (changes: ChatFileChange[]) => changes.length > 0 ? (
    <div className="chat__changes" aria-label="本轮文件改动">
      <div className="chat__changes-title">文件改动 · {changes.length}</div>
      {changes.map((change) => {
        const reverted = change.reverted || change.status === 'reverted'
        return <div className="chat__change" key={change.id}>
          <div className="chat__change-name"><span>{({ create: '新建', write: '修改', edit: '修改', rewrite: '整篇覆盖', replace_exact: '局部修改', move: '移动', rename: '重命名', delete: '删除' } as Record<string, string>)[change.operation] || change.operation}</span>
            <strong>{change.path}{change.destination ? ` → ${change.destination}` : ''}</strong></div>
          <div className="chat__change-actions"><span className="muted">{reverted ? '已撤回' : ({ pending: '待确认', proposed: '待确认', applied: '已保存', failed: '未应用', rejected: '门禁未通过', applying: '保存中' } as Record<string, string>)[change.status] || change.status}</span>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setDiff(change)}>查看差异</button>
            {change.operation !== 'delete' || reverted ? <button className="btn btn--ghost btn--sm" type="button" onClick={() => void Promise.resolve(callbacksRef.current.onOpenFile(reverted ? change.path : change.destination || change.path)).catch((err) => setInputError(errorMessage(err)))}>打开</button> : null}
            <button className="btn btn--ghost btn--sm" type="button" disabled={busy || reverted || reverting !== null || change.status !== 'applied'} onClick={() => void undo(change)}>{reverting === change.id ? '撤回中…' : '撤回'}</button>
          </div>
        </div>
      })}
    </div>
  ) : null

  const renderContext = (preview?: ContextPreview) => preview?.items?.length ? <details className="chat__activity">
    <summary>本轮引用 · {preview.items.length} 项 · 约 {preview.total_tokens} tokens</summary>
    <ul>{preview.items.map((item, index) => <li key={`${item.source}-${index}`}><strong>{item.title}</strong><div className="muted">{item.source}</div><small>{item.chars} 字符{item.truncated ? ' · 已按预算截断' : ''}</small></li>)}</ul>
    {preview.degradation?.length ? <p className="muted">部分材料已按上下文预算裁剪。</p> : null}
  </details> : null

  const renderResult = (text: string, meta: Record<string, unknown>, live = false) => <>
    {renderSteps(live ? steps : savedSteps(meta.steps), live)}
    {renderContext(live ? run?.context_preview : meta.context_preview as ContextPreview | undefined)}
    {text ? <MarkdownView text={text} /> : live ? <p className="muted">{reconnecting ? '连接恢复后会接着显示，任务仍在后台运行…' : run?.status === 'waiting_input' ? '选择下方选项或回应批准请求后，我会继续处理。' : run?.status_message || '正在阅读项目并处理你的要求…'}</p> : null}
    {(live ? run?.context_warnings || [] : Array.isArray(meta.context_warnings) ? meta.context_warnings as string[] : []).map((warning, index) => <div className="chat__context-warning" role="status" key={index}>{warning}</div>)}
    {(live ? run?.interactions || [] : savedInteractions(meta.interactions)).map((interaction) => <ChatInteractionCard key={interaction.id} interaction={interaction} runPermission={live ? run?.permission_mode : meta.permission_mode as ChatPermissionMode | undefined} onRespond={respond} />)}
    {renderChanges(live ? run?.changes || [] : savedChanges(meta.changes))}
    {typeof meta.error_message === 'string' && meta.error_message ? <div className="chat__error" role="alert">{meta.error_message}</div> : null}
    {typeof meta.status === 'string' ? <div className="chat__result-status" role={live ? 'status' : undefined}>{statusText(meta.status)}{live && run?.status_message ? ` · ${run.status_message}` : ''}</div> : null}
    {['failed', 'error', 'interrupted', 'cancelled'].includes(String(meta.status)) && (live ? run?.id : typeof meta.run_id === 'string' ? meta.run_id : null) ? <button type="button" className="btn btn--sm" disabled={busy || sending} onClick={() => void send((live ? run?.id : meta.run_id as string) || undefined)}>继续处理</button> : null}
    {Array.isArray(meta.proposal_ids) && meta.proposal_ids.length > 0 && !meta.auto_applied ? <a className="chat__inbox-link" href="/inbox">查看收件箱中的修改建议</a> : null}
  </>

  return <>
    {expanded ? <div className="chat__backdrop" onClick={() => setExpanded(false)} /> : null}
    <div className={`chat${expanded ? ' chat--expanded' : ''}`}>
      <div className="chat__header">
        <div className="chat__heading"><span className="chat__heading-icon">✦</span><strong>写作助手</strong><span className="chat__badge">{busy ? statusText(run?.status || 'starting') : '与小说文件一起工作'}</span></div>
        <div className="chat__header-actions"><button className="btn btn--ghost btn--sm" type="button" aria-label={expanded ? '缩小对话面板' : '展开对话面板'} onClick={() => setExpanded(!expanded)}>{expanded ? '缩小' : '展开'}</button>
          {onCollapse ? <button className="btn btn--ghost btn--sm" type="button" aria-label="收起对话面板" onClick={onCollapse}>收起</button> : null}</div>
      </div>
      <div className="chat__configuration">
        <button className="chat__configuration-toggle" type="button" aria-expanded={settingsOpen} aria-controls="chat-configuration-content" onClick={() => { const next = !settingsOpen; setSettingsOpen(next); localStorage.setItem('aiw.chat.settings.open', String(next)) }}>
          <span className="chat__configuration-action">{settingsOpen ? '▾ 收起设置' : '▸ 展开设置'}</span>
          <strong title={current?.title || '新对话'}>{current?.title || '新对话'}</strong>
          <span title={modelLabel}>{modelLabel}</span>
          <span className="chat__configuration-permission">{run && !isTerminal(run) ? `本轮：${permissionLabel(runPermission)}${runDiscussionOnly ? ' · 仅讨论' : ''}；下轮：${permissionLabel(selectedPermission)}${discussionOnly ? ' · 仅讨论' : ''}` : `下一轮：${permissionLabel(selectedPermission)}${discussionOnly ? ' · 仅讨论' : ''}`}</span>
        </button>
        {settingsOpen ? <div id="chat-configuration-content" className="chat__configuration-content">
      <div className="chat__session-bar">
        <select className="select" value={activeId ?? ''} onChange={(event) => selectSession(Number(event.target.value) || null)} aria-label="会话选择" disabled={sending}>
          <option value="">新对话</option>{sessions.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
        </select>
        <button className="btn btn--sm" type="button" disabled={sending} onClick={() => selectSession(null)}>新对话</button>
        <details className="chat__session-menu"><summary aria-label="会话操作">•••</summary><div><button className="btn btn--ghost btn--sm" type="button" disabled={!current || configuring} onClick={() => void rename()}>重命名</button><button className="btn btn--ghost btn--sm" type="button" disabled={!current || naming || current.title_status === 'pending' || current.title_status === 'running'} onClick={() => void autoName()}>{naming ? '提交中…' : 'AI 重新命名'}</button><button className="btn btn--ghost btn--sm" type="button" disabled={!current || busy} onClick={() => void remove()}>删除会话</button></div></details>
      </div>
      {current?.title_status === 'pending' || current?.title_status === 'running' ? <div className="chat__title-status" role="status">AI 正在为对话命名…</div> : current?.title_status === 'failed' ? <div className="chat__title-status">AI 命名暂未完成<button type="button" className="btn btn--ghost btn--sm" disabled={naming} onClick={() => void autoName()}>重试</button></div> : null}
      <div className="chat__settings">
        <select className="select" aria-label="权限模式" value={selectedPermission} disabled={configuring} onChange={(event) => void updateSettings({ permission_mode: event.target.value as ChatPermissionMode })}>
          {CHAT_PERMISSION_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <select className="select" aria-label="模型选择" value={selectedModel} disabled={busy || configuring} onChange={(event) => { const [provider_id = '', model_id = ''] = event.target.value.split('::'); void updateSettings({ provider_id, model_id }) }}>
          <option value="">默认模型</option>{providers.filter((provider) => provider.enabled).map((provider) => { const primary = provider.models[0]?.id ?? ''; return <option key={provider.provider_id} value={`${provider.provider_id}::${primary}`}>{provider.display_name} / {primary || '（未配置模型）'}</option> })}
        </select>
      </div>
      <div className="chat__permission-note">{CHAT_PERMISSION_OPTIONS.find((option) => option.value === selectedPermission)?.description}{busy ? <strong>本轮实际权限：{permissionLabel(runPermission)}{runDiscussionOnly ? ' · 仅讨论' : ''}；下一轮设置：{permissionLabel(selectedPermission)}{discussionOnly ? ' · 仅讨论' : ''}。设置变更对下一轮生效。</strong> : null}</div>
      <div className="chat__discussion-row"><label><input type="checkbox" checked={discussionOnly} disabled={configuring} onChange={(event) => void updateSettings({ discussion_only: event.target.checked })} />仅讨论，不修改文件</label><button className="btn btn--ghost btn--sm" type="button" onClick={() => setMemoryOpen(true)}>本书记忆</button></div>
      <details className="chat__advanced"><summary>高级设置</summary><label>写作分工<select className="select" aria-label="Agent 选择" value={selectedAgent} disabled={busy || configuring} onChange={(event) => void updateSettings({ agent: event.target.value, agent_pinned: event.target.value ? 1 : 0 })}><option value="">自动选择</option>{agents.map((agent) => <option key={agent.name} value={agent.name}>{agent.title || agent.name}</option>)}</select></label></details>
      <div className="chat__book-defaults"><div><strong>本书新对话默认</strong><span>{chatDefaults?.source === 'book' ? '本书设置' : '沿用全局设置'}</span></div><div><select className="select" aria-label="本书新对话默认权限" value={chatDefaults?.permission_mode ?? settings?.chat?.permission_mode ?? 'auto'} disabled={defaultsLoading || savingDefaults} onChange={(event) => void updateDefaults({ permission_mode: event.target.value as ChatPermissionMode })}>{CHAT_PERMISSION_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</select><label><input type="checkbox" checked={chatDefaults?.discussion_only ?? settings?.chat?.discussion_only ?? false} disabled={defaultsLoading || savingDefaults} onChange={(event) => void updateDefaults({ discussion_only: event.target.checked })} />仅讨论</label></div><small>只影响本书之后创建的新对话；已有对话保持原权限。</small></div>
        </div> : null}
      </div>
      <div className="chat__messages" ref={scrollRef} onScroll={() => { const area = scrollRef.current; if (area) followBottomRef.current = area.scrollHeight - area.scrollTop - area.clientHeight < 70 }} aria-busy={busy}>
        {loading ? <p className="muted">正在载入会话…</p> : messages.length === 0 && !run ? <div className="chat__welcome"><span className="chat__welcome-mark">✦</span><h3>把想法写进你的小说</h3><p>让我先阅读相关章节和设定，再续写、修改或整理文件。每次改动都可以查看差异和撤回。</p><div className="chat__welcome-examples"><button type="button" onClick={() => { setInput('阅读当前章节和相关设定，帮我把这一章的开头改得更紧凑。'); inputRef.current?.focus() }}>修改当前章节</button><button type="button" onClick={() => { setInput('阅读大纲和已有章节，继续写下一章并保存到章节文件。'); inputRef.current?.focus() }}>续写并保存下一章</button><button type="button" onClick={() => { setInput('检查当前章节的人物表现与设定是否一致，只报告问题。'); inputRef.current?.focus() }}>检查人物与设定</button></div></div> : null}
        {messages.filter((message) => !(run && message.role !== 'user' && String(message.meta?.run_id) === run.id)).map((message) => <div key={message.id} data-message-id={message.id} className={`chat__msg chat__msg--${message.role === 'user' ? 'user' : 'assistant'}${sourceTarget?.sessionId === activeId && sourceTarget?.messageId === message.id ? ' chat__msg--source' : ''}`}><div className="chat__speaker">{message.role === 'user' ? '你' : '写作助手'}</div>{message.role === 'user' ? message.content : renderResult(message.content, message.meta)}</div>)}
        {run ? <div className="chat__msg chat__msg--assistant"><div className="chat__speaker">写作助手</div>{renderResult(run.text || '', { status: run.status, error_message: run.error_message }, true)}</div> : null}
      </div>
      <div className="chat__composer">
        <div className="chat__context" aria-label="本轮上下文">
          {chapterRel ? <button type="button" title={includeCurrent ? `移除参考文件：${chapterRel}` : `附加参考文件：${chapterRel}`} onClick={() => setIncludeCurrent(!includeCurrent)}>{includeCurrent ? `参考：${chapterRel} ×` : `+ 参考：${chapterRel}`}</button> : <span>未附加参考文件</span>}
          {targetChapterRel ? <span title={`正文目标章节：${targetChapterRel}`}>正文目标：{targetChapterRel}</span> : <span>正文目标：未指定</span>}
          {selection?.trim() && includeCurrent ? <button type="button" title={includeSelection ? '移除选区上下文' : '附加选区上下文'} onClick={() => setIncludeSelection(!includeSelection)}>{includeSelection ? `已选 ${selection.length} 字 ×` : '+ 选区'}</button> : null}
          {hasUnsavedChanges ? <span className="chat__context-dirty">发送前保存修改</span> : null}{references.map((path) => <button key={path} type="button" title={`移除参考：${path}`} onClick={() => setReferences((items) => items.filter((item) => item !== path))}>{path.split('/').pop()} ×</button>)}</div>
        <details className="chat__references"><summary>添加参考文件{references.length ? `（${references.length}）` : ''}</summary><input className="input" aria-label="搜索参考文件" placeholder="按文件名查找" value={fileSearch} onChange={(event) => setFileSearch(event.target.value)} /><div>{availableFiles.filter((path) => path !== chapterRel && path.includes(fileSearch)).slice(0, 100).map((path) => <label key={path}><input type="checkbox" checked={references.includes(path)} onChange={(event) => setReferences((items) => event.target.checked ? [...items, path] : items.filter((item) => item !== path))} />{path}</label>)}</div></details>
        {quickCards.length ? <div className="quick-cards">{quickCards.map((card) => <button key={card.key} className="quick-card" type="button" onClick={() => { setInput(card.prompt); inputRef.current?.focus() }}>{card.label}</button>)}</div> : null}
        <textarea ref={inputRef} autoComplete={NO_AUTOFILL} className="textarea chat__input" aria-label="给写作助手的要求" placeholder="想写什么，或想修改哪个文件？" value={input} onChange={(event) => setInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void send() } }} />
        {inputError ? <div className="chat__error" role="alert">{inputError}<button type="button" className="btn btn--ghost btn--sm" onClick={() => setInputError('')}>关闭</button></div> : null}
        <div className="chat__composer-foot"><span className="muted">{reconnecting ? '连接恢复中 · 任务仍在运行' : run?.status === 'waiting_input' ? '请先回应上方的问题或批准请求' : busy ? '可切换会话，任务会继续运行' : 'Ctrl + Enter 发送'}</span>{run && !isTerminal(run) ? <button className="btn btn--danger btn--sm" type="button" onClick={() => void stop()}>停止</button> : <button className="btn btn--primary btn--sm" type="button" disabled={!input.trim() || sending || loading || sessionsLoading || settingsLoading || configuring} onClick={() => void send()}>{sending ? '准备中…' : '发送 ↑'}</button>}</div>
      </div>
    </div>
    <Modal title={`文件差异 · ${diff?.path || ''}`} open={diff !== null} onClose={() => setDiff(null)} width={1000}>
      {diff ? Array.isArray(diff.diff) ? <DiffView lines={diff.diff} /> : typeof diff.diff === 'string' ? <pre className="chat__diff-text">{diff.diff}</pre> : <div className="chat__diff-fallback"><section><h3>修改前</h3><pre>{diff.before_content ?? '文件不存在'}</pre></section><section><h3>修改后</h3><pre>{diff.after_content ?? '文件已移除'}</pre></section></div> : null}
    </Modal>
    <ChatMemoryPanel projectId={projectId} open={memoryOpen} onClose={() => setMemoryOpen(false)} onSource={(sessionId, messageId) => { selectSession(sessionId); setSourceTarget({ sessionId, messageId }) }} />
    {confirmNode}{promptNode}
  </>
}
