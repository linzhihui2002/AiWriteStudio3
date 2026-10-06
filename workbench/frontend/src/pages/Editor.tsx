/**
 * 编辑器（三栏工作台）：左文档树 · 中正文（编辑/预览/阅读态）· 右陪练面板。
 *
 * 关键纪律
 * - 页面固定高度（page--editor），不整页滚动；合同/循环收在底部可折叠抽屉；
 * - 文档树与对话面板可折叠（localStorage 记忆），正文产物一律经收件箱落盘（8 步循环的 DRAFT 只写草稿区）；
 * - 保存前做外部改动检测（mtime + hash）→ 冲突弹窗（保留我的 / 采用外部）；
 * - 字数里程碑是**装饰层**，不写入正文、不参与统计（阈值来自全局设置）；
 * - 快捷键：Ctrl+S 保存、Alt+↑/↓ 切章、Ctrl+Shift+D 阅读态、Ctrl+Enter 局部操作条生成。
 */

import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import {
  ApiError,
  checkConflict,
  checkGates,
  createChapter,
  createNode,
  deleteNode,
  freezeContract,
  generateContract,
  getContract,
  getTree,
  getWritingPrefs,
  ghostText,
  importDocument,
  listChapters,
  listCardHighlights,
  localOperation,
  patchNode,
  previewContext,
  readChapter,
  resolveConflict,
  restoreSnapshot,
  runPipeline,
  saveChapter,
  listSnapshots,
  updateChapterMeta,
} from '../api/client'
import type {
  ChapterDetail,
  ChapterSummary,
  ChatContext,
  ChatFileChange,
  ConflictCheck,
  ContextBlock,
  ContextRetrievalSummary,
  Contract,
  ProjectTree,
  TreeNode,
} from '../api/types'
import { useProjectChat } from '../state/projectChat'
import DiffView from '../components/DiffView'
import DocumentTree from '../components/DocumentTree'
import MarkdownView from '../components/MarkdownView'
import ChapterEditor, { type ChapterEditorHandle, type ChapterEditorSnapshot } from '../components/ChapterEditor'
import Modal from '../components/Modal'
import Drawer from '../components/Drawer'
import PanelResizer from '../components/PanelResizer'
import { errorMessage, useToast } from '../state/useToast'
import { usePanelWidth } from '../state/usePanelWidth'
import { useSettings } from '../state/useSettings'
import { NO_AUTOFILL } from '../lib/autofill'
import { chapterLabel, chapterLabelWithCount } from '../lib/chapterName'
import { evidenceEditorUrl, evidenceSelection } from '../lib/knowledgeState'
import { readEditorDraft, storeEditorDraft, clearEditorDraft, acknowledgeEditorSave, rememberEditorFile, lastEditorFile } from '../lib/editorDraftState'
import { ResourceState, useResourceRequest } from '../components/WorkspacePage'
import { buildNovelDecorations, replaceSelectedText, type NovelEntity } from '../lib/novelDecorations'
import { useScopedAction } from '../state/useScopedAction'

// 章节正文文件为纯文本（第NNNN章.txt；未迁移的 .md 仍按章节处理）。
const isChapterPath = (relPath: string) => /^章节\/第\d{4,}章\.(?:txt|md)$/.test(relPath)

/**
 * 落盘内容：章节是纯正文（无 frontmatter），保存时只回传正文；
 * 其它文档（大纲 / 设定等 .md）仍保留原 frontmatter 前缀。
 */
const documentContent = (doc: ChapterDetail, body: string) =>
  isChapterPath(doc.rel_path) ? body : doc.content.slice(0, doc.content.length - doc.body.length) + body
const bookHistories = new Map<number, Map<string, ChapterEditorSnapshot>>()
function historyForBook(projectId: number) {
  if (!bookHistories.has(projectId)) bookHistories.set(projectId, new Map())
  return bookHistories.get(projectId)!
}

export default function Editor() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const statusAction = useScopedAction(projectId)
  const [searchParams] = useSearchParams()
  const evidencePath = searchParams.get('path') || ''
  const evidenceHash = searchParams.get('hash') || ''
  const evidenceStart = Number(searchParams.get('line')) || 1
  const evidenceEnd = Number(searchParams.get('end_line')) || evidenceStart
  const [evidenceNotice, setEvidenceNotice] = useState('')
  const evidenceApplied = useRef('')
  const { push: notify } = useToast()
  const { settings } = useSettings()
  const { registerPageContext, setChatHost, openChat, attachSelection } = useProjectChat()
  const [workspaceLayout, setWorkspaceLayout] = useState<'wide' | 'medium' | 'compact'>('wide')
  const [directoryOverlay, setDirectoryOverlay] = useState(false)

  const [tree, setTree] = useState<ProjectTree | null>(null)
  const [chapters, setChapters] = useState<ChapterSummary[]>([])
  const [activeRel, setActiveRel] = useState('')
  const [targetChapterRel, setTargetChapterRel] = useState('')
  const [detail, setDetail] = useState<ChapterDetail | null>(null)
  const [text, setText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [draftNotice, setDraftNotice] = useState('')
  const treeResource = useResourceRequest(projectId)
  const chapterResource = useResourceRequest(`${projectId}:${activeRel}`)
  const [saving, setSaving] = useState(false)
  const [mode, setMode] = useState<'edit' | 'preview' | 'reading'>('edit')
  const [statusFilter, setStatusFilter] = useState<string>('')

  const [contract, setContract] = useState<Contract | null>(null)
  const [gates, setGates] = useState<{ prerequisites: { blocked: boolean; reasons: string[]; checks: Array<{ key: string; label: string; ok: boolean; hint: string; optional?: boolean }> }; contract: { ok: boolean; hint: string } } | null>(null)
  const [pipelineLog, setPipelineLog] = useState<string[]>([])
  const [running, setRunning] = useState(false)

  const [conflict, setConflict] = useState<ConflictCheck | null>(null)
  const [contextPreview, setContextPreview] = useState<ContextBlock[]>([])
  const [contextRetrieval, setContextRetrieval] = useState<ContextRetrievalSummary | null>(null)

  const [selection, setSelection] = useState('')
  const [localBusy, setLocalBusy] = useState(false)
  const [draft, setDraft] = useState('')
  const [draftSource, setDraftSource] = useState('')
  const draftTarget = useRef<{ projectId: number; path: string; text: string; selection: string; start: number; end: number } | null>(null)
  const [ghost, setGhost] = useState<{ candidate: string; projectId: number; path: string; text: string } | null>(null)
  const [composing, setComposing] = useState(false)
  const [highlightEntities, setHighlightEntities] = useState<NovelEntity[]>([])
  const [draftDrag, setDraftDrag] = useState(false)

  const [snapshots, setSnapshots] = useState<Array<{ id: number; reason: string; created_at: string }>>([])
  const [snapshotOpen, setSnapshotOpen] = useState(false)
  // 快照回滚是破坏性操作：改为行内二次确认（按钮变「确认回滚/取消」）
  const [confirmingSnapshot, setConfirmingSnapshot] = useState<number | null>(null)
  // 本书字数区间（书级覆盖优先，缺省用全局 settings.writing）
  const [bookWordRange, setBookWordRange] = useState<{ min: number; max: number } | null>(null)

  // ── 面板折叠 + 宽度（纯 UI 偏好，localStorage 记忆；宽度可拖拽分隔条调整）──
  const [leftOpen, setLeftOpen] = useState(() => localStorage.getItem('aiw.editor.leftOpen') !== '0')
  const [rightOpen, setRightOpen] = useState(() => localStorage.getItem('aiw.editor.rightOpen') !== '0')
  const [drawerOpen, setDrawerOpen] = useState(() => localStorage.getItem('aiw.editor.drawerOpen') === '1')
  const [leftWidth, setLeftWidth] = usePanelWidth('aiw.editor.leftWidth', 260, 180, 420)
  const [rightWidth, setRightWidth] = usePanelWidth('aiw.editor.rightWidth', 320, 280, 560)
  const editorRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const element = editorRef.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => {
      const width = entry.contentRect.width
      setWorkspaceLayout(width >= 1200 ? 'wide' : width >= 900 ? 'medium' : 'compact')
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    localStorage.setItem('aiw.editor.leftOpen', leftOpen ? '1' : '0')
  }, [leftOpen])

  useEffect(() => {
    localStorage.setItem('aiw.editor.rightOpen', rightOpen ? '1' : '0')
  }, [rightOpen])

  useEffect(() => {
    localStorage.setItem('aiw.editor.drawerOpen', drawerOpen ? '1' : '0')
  }, [drawerOpen])

  const baseRef = useRef<{ mtime: number; hash: string } | null>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const chapterEditorRef = useRef<ChapterEditorHandle>(null)
  const editorHistory = useRef(historyForBook(projectId))
  const loadedDocument = useRef<{ projectId: number; path: string } | null>(null)
  const saveInFlightRef = useRef<Promise<boolean> | null>(null)
  const loadSequenceRef = useRef(0)
  const selectionRangeRef = useRef<{ start: number; end: number } | null>(null)
  const latestRef = useRef({ projectId, activeRel, detail, text, dirty, selection })
  latestRef.current = { projectId, activeRel, detail, text, dirty, selection }
  const [filePreview, setFilePreview] = useState<{ path: string; content: string } | null>(null)

  useEffect(() => {
    setActiveRel(''); setTargetChapterRel(''); setDetail(null); setText(''); setDirty(false); setSelection('')
    setTree(null); setChapters([]); setDraftNotice(''); setContract(null); setGates(null); setSnapshots([])
    setDraft(''); setGhost(null); setContextPreview([]); setContextRetrieval(null); setPipelineLog([])
    baseRef.current = null; selectionRangeRef.current = null; setConflict(null)
    loadedDocument.current = null
    editorHistory.current = historyForBook(projectId)
  }, [projectId])

  useEffect(() => {
    const snapshot = { projectId, activeRel, detail, text, dirty }
    const valid = loadedDocument.current?.projectId === projectId && loadedDocument.current.path === activeRel
    const persist = () => {
      const latest = latestRef.current
      const current = latest.projectId === projectId && latest.activeRel === activeRel ? latest : snapshot
      if (valid && current.dirty && current.detail?.rel_path === current.activeRel) {
        storeEditorDraft({ projectId: current.projectId, path: current.activeRel, text: current.text, base: current.detail })
      }
    }
    const timer = window.setTimeout(persist, 150)
    window.addEventListener('pagehide', persist)
    return () => { window.clearTimeout(timer); window.removeEventListener('pagehide', persist); persist() }
  }, [projectId, activeRel, text, dirty, detail])

  useEffect(() => {
    let alive = true
    setBookWordRange(null)
    void getWritingPrefs(projectId)
      .then((prefs) => { if (alive) setBookWordRange({ min: prefs.chapter_min_words, max: prefs.chapter_max_words }) })
      .catch(() => { /* 区间读取失败时回落全局设置，不打扰作者 */ })
    return () => { alive = false }
  }, [projectId])

  const milestone = settings?.milestone ?? { enabled: true, step: 500 }
  const ghostEnabled = settings?.editor.ghost_text ?? true
  const decorations = useMemo(() => buildNovelDecorations(text, {
    entities: highlightEntities,
    milestone: { enabled: milestone.enabled && (settings?.editor.milestone_inline ?? true), step: milestone.step },
  }), [text, highlightEntities, milestone.enabled, milestone.step, settings?.editor.milestone_inline])
  useEffect(() => {
    let alive = true
    let request = 0
    setHighlightEntities([])
    const refresh = async () => {
      const sequence = ++request
      try {
        const entries = await listCardHighlights(projectId)
        if (alive && sequence === request) setHighlightEntities(entries)
      } catch { /* Missing card data does not block plain-text writing. */ }
    }
    void refresh()
    window.addEventListener('focus', refresh)
    window.addEventListener('aiw:cards-changed', refresh)
    return () => { alive = false; window.removeEventListener('focus', refresh); window.removeEventListener('aiw:cards-changed', refresh) }
  }, [projectId])
  const wordRange = bookWordRange ?? (settings?.writing
    ? { min: settings.writing.chapter_min_words, max: settings.writing.chapter_max_words }
    : null)

  // ── 载入 ──
  const loadTree = useCallback(async () => {
    const token = treeResource.begin()
    try {
      const data = await getTree(projectId)
      const list = await listChapters(projectId)
      if (!treeResource.accept(token)) return
      setTree(data)
      setChapters(list)
      setTargetChapterRel((current) => list.some((item) => item.rel_path === current)
        ? current : list[0]?.rel_path ?? '')
      const remembered = lastEditorFile(projectId)
      const paths = new Set(list.map(item => item.rel_path))
      const collect = (nodes: TreeNode[]) => nodes.forEach(node => { if (node.type === 'file') paths.add(node.rel_path); if (node.children) collect(node.children) })
      collect(data.root_nodes); data.groups.forEach(group => collect(group.nodes))
      const first = evidencePath || (paths.has(remembered) ? remembered : list[0]?.rel_path)
      if (!latestRef.current.activeRel && first) setActiveRel(first)
      treeResource.finish(token)
      return data
    } catch (err) {
      treeResource.fail(token, errorMessage(err))
    }
  }, [projectId, activeRel, notify, evidencePath])

  useEffect(() => {
    void loadTree()
  }, [loadTree])

  useEffect(() => {
    if (chapters.some((chapter) => chapter.rel_path === activeRel)) setTargetChapterRel(activeRel)
  }, [activeRel, chapters])

  const loadChapter = useCallback(
    async (relPath: string, options: { replaceDirtyText?: string } = {}) => {
      if (!relPath) return
      const requestId = ++loadSequenceRef.current
      const token = chapterResource.begin()
      try {
        const data = await readChapter(projectId, relPath)
        if (requestId !== loadSequenceRef.current || latestRef.current.projectId !== projectId
          || latestRef.current.activeRel !== relPath) return
        // 后台刷新和文件切换期间，绝不让迟到的读取覆盖作者尚未保存的输入。
        if (latestRef.current.dirty && options.replaceDirtyText === undefined) { chapterResource.finish(token); return }
        if (options.replaceDirtyText !== undefined && latestRef.current.text !== options.replaceDirtyText) {
          notify('加载版本期间正文又有输入，已保留当前编辑内容。', 'info')
          chapterResource.finish(token)
          return
        }
        const cached = options.replaceDirtyText === undefined ? readEditorDraft(projectId, relPath) : null
        const restored = cached && cached.text !== data.body ? cached : null
        const base = restored && restored.base.hash !== data.hash ? restored.base : data
        const body = restored?.text ?? data.body
        loadedDocument.current = { projectId, path: relPath }
        setDetail(base)
        setText(body)
        setDirty(!!restored)
        latestRef.current = { ...latestRef.current, detail: base, text: body, dirty: !!restored, selection: '' }
        baseRef.current = base.hash ? { mtime: base.mtime || 0, hash: base.hash } : null
        if (!restored) clearEditorDraft(projectId, relPath)
        setDraftNotice(restored ? `已恢复未保存草稿${base.hash !== data.hash ? '；磁盘版本已变化，保存前需要核对差异。' : '。'}` : '')
        rememberEditorFile(projectId, relPath)
        chapterResource.finish(token)
        setSelection('')
        selectionRangeRef.current = null
        setDraft('')
        draftTarget.current = null
        setGhost(null)
        const [contractData, gateData, snapshots] = await Promise.all([
          isChapterPath(relPath) ? getContract(projectId, relPath).catch(() => null) : Promise.resolve(null),
          isChapterPath(relPath) ? checkGates(projectId, relPath).catch(() => null) : Promise.resolve(null),
          listSnapshots(projectId, relPath).catch(() => []),
        ])
        if (requestId !== loadSequenceRef.current || latestRef.current.projectId !== projectId || latestRef.current.activeRel !== relPath) return
        setContract(contractData)
        setGates(gateData)
        setSnapshots(snapshots)
      } catch (err) {
        chapterResource.fail(token, errorMessage(err))
      }
    },
    [projectId, notify, chapterResource.begin],
  )

  useEffect(() => {
    if (activeRel) void loadChapter(activeRel)
  }, [activeRel, loadChapter])

  // ── 保存（含外部改动检测）──
  const save = useCallback(
    (options: { force?: boolean; quiet?: boolean } = {}): Promise<boolean> => {
      if (saveInFlightRef.current) return saveInFlightRef.current
      const state = latestRef.current
      if (!state.detail) return Promise.resolve(true)
      if (state.detail.rel_path !== state.activeRel) {
        notify('文件正在切换，请稍后保存。', 'error')
        return Promise.resolve(false)
      }
      const document = state.detail
      const baseline = baseRef.current
      const content = documentContent(document, state.text)
      const operation = (async () => {
        setSaving(true)
        try {
          if (!options.force && baseline) {
            const result = await checkConflict(state.projectId, { rel_path: document.rel_path,
              base_mtime: baseline.mtime, base_hash: baseline.hash, current_content: content })
            if (result.changed) {
              if (latestRef.current.projectId === state.projectId && latestRef.current.activeRel === document.rel_path) setConflict(result)
              return false
            }
          }
          const saved = await saveChapter(state.projectId, document.rel_path, content, document.status,
            options.force ? undefined : document.hash || baseline?.hash)
          if (latestRef.current.projectId === state.projectId && latestRef.current.activeRel === document.rel_path) {
            const stillDirty = latestRef.current.text !== state.text
            latestRef.current = { ...latestRef.current, detail: saved, dirty: stillDirty }
            setDetail(saved)
            setDirty(stillDirty)
            baseRef.current = saved.hash ? { mtime: saved.mtime || 0, hash: saved.hash } : baseline
            if (stillDirty) storeEditorDraft({ projectId: state.projectId, path: document.rel_path, text: latestRef.current.text, base: saved })
            else setDraftNotice('')
          }
          acknowledgeEditorSave(state.projectId, document.rel_path, state.text, saved)
          if (!options.quiet) notify(`已保存 · ${saved.word_count} 字`, 'success')
          if (latestRef.current.projectId === state.projectId) await loadTree()
          return true
        } catch (err) {
          // 保存前检查与实际落盘之间的并发变化仍由 expected_hash 原子保护。
          if (baseline) {
            const changed = await checkConflict(state.projectId, { rel_path: document.rel_path,
              base_hash: baseline.hash, current_content: content }).catch(() => null)
            if (changed?.changed && latestRef.current.projectId === state.projectId && latestRef.current.activeRel === document.rel_path) setConflict(changed)
          }
          notify(errorMessage(err), 'error')
          return false
        } finally { setSaving(false); saveInFlightRef.current = null }
      })()
      saveInFlightRef.current = operation
      return operation
    },
    [notify, loadTree],
  )

  const selectFile = async (relPath: string) => {
    if (!relPath || relPath === latestRef.current.activeRel) return
    if (latestRef.current.dirty && !await save({ quiet: true })) return
    ++loadSequenceRef.current
    latestRef.current = { ...latestRef.current, activeRel: relPath, detail: null, text: '', dirty: false, selection: '' }
    baseRef.current = null
    loadedDocument.current = null
    setDetail(null)
    setText('')
    setDirty(false)
    setSelection('')
    setGhost(null)
    setDraftNotice('')
    setActiveRel(relPath)
  }

  // 深链接只能打开当前书的相对路径，文件读取仍由服务端做项目边界校验。
  useEffect(() => {
    evidenceApplied.current = ''
    setEvidenceNotice('')
    if (evidencePath) void selectFile(evidencePath)
    // 深链接变化触发一次切换；保存状态由 selectFile/latestRef 读取。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, evidencePath, evidenceHash, evidenceStart, evidenceEnd])

  useEffect(() => {
    if (!evidencePath || !detail || detail.rel_path !== evidencePath) return
    const key = `${projectId}:${evidencePath}:${evidenceHash}:${evidenceStart}:${evidenceEnd}:${detail.hash}`
    if (evidenceApplied.current === key) return
    evidenceApplied.current = key
    if (evidenceHash && detail.hash !== evidenceHash) {
      setEvidenceNotice('原文版本已变化，检索依据已失效。请返回知识图谱重新同步和检索。')
      chapterEditorRef.current?.selectAndReveal(0, 0)
      textareaRef.current?.setSelectionRange(0, 0)
      return
    }
    setEvidenceNotice(`已定位原文依据：${evidencePath} · L${evidenceStart}–${evidenceEnd}`)
    setMode('edit')
    const frame = window.requestAnimationFrame(() => {
      const textarea = textareaRef.current
      const [start, end] = evidenceSelection(detail.body, detail.content, evidenceStart, evidenceEnd)
      if (chapterEditorRef.current) { chapterEditorRef.current.selectAndReveal(start, end); return }
      if (!textarea) return
      textarea.focus()
      textarea.setSelectionRange(start, end)
      const linesBefore = detail.body.slice(0, start).split('\n').length - 1
      const lineHeight = parseFloat(getComputedStyle(textarea).lineHeight) || 28
      textarea.scrollTop = Math.max(0, linesBefore * lineHeight - textarea.clientHeight / 3)
    })
    return () => window.cancelAnimationFrame(frame)
  }, [projectId, evidencePath, evidenceHash, evidenceStart, evidenceEnd, detail])

  const prepareChatContext = async (): Promise<ChatContext> => {
    const initial = latestRef.current
    const target = chapters.some((chapter) => chapter.rel_path === targetChapterRel)
      ? targetChapterRel : undefined
    // Unsupported automatic attachments are reported by the run service. They
    // must not block the author's message or try to save a non-Markdown target.
    // 章节正文（.txt）是受支持的目标，必须照常"先保存再发送"。
    if (initial.activeRel && !isChapterPath(initial.activeRel)
      && !initial.activeRel.toLowerCase().endsWith('.md')) {
      return { active_file: initial.activeRel, target_chapter: target }
    }
    // 等待正在进行的保存；若保存中又输入了内容，继续保存最新一版。
    if (saveInFlightRef.current && !await saveInFlightRef.current) throw new Error('请先处理当前文件的版本冲突，再发送。')
    while (latestRef.current.dirty) {
      if (!await save({ quiet: true })) throw new Error('当前修改尚未保存，请处理版本冲突或保存错误后重试。')
    }
    const state = latestRef.current
    if (state.projectId !== projectId) throw new Error('项目已切换，请在当前项目重新发送。')
    if (!state.activeRel) return { target_chapter: target }
    if (!state.detail) throw new Error('当前文件正在载入，请稍后发送。')
    // 没有本地编辑时也要核对磁盘，避免 AI 基于已经失效的编辑器版本改写。
    let disk: ConflictCheck
    try {
      disk = await checkConflict(projectId, { rel_path: state.activeRel, base_hash: state.detail.hash || baseRef.current?.hash })
    } catch (error) {
      // A clean editor whose file was moved/deleted is an optional attachment;
      // the backend skips it with a durable context warning. Dirty/conflicting
      // edits still follow the save checks above and are never discarded.
      if (error instanceof ApiError && error.status === 404) return { active_file: state.activeRel, target_chapter: target }
      throw error
    }
    if (disk.changed) {
      await loadChapter(state.activeRel)
      throw new Error('文件已有新的改动，编辑器已刷新。请核对内容后再次发送。')
    }
    return { active_file: state.activeRel, target_chapter: target, base_hash: state.detail.hash || disk.disk.hash,
      selection: state.selection.trim() ? { text: state.selection, ...selectionRangeRef.current } : undefined }
  }

  const handleChatChanges = async (changes: ChatFileChange[]) => {
    const refreshed = await loadTree()
    const state = latestRef.current
    if (state.projectId !== projectId || !state.activeRel) return
    const matching = changes.filter((change) => change.path === state.activeRel || change.destination === state.activeRel)
    if (!matching.length) return
    const change = matching[matching.length - 1]
    const paths = new Set<string>()
    const collect = (nodes: TreeNode[]) => nodes.forEach((node) => {
      if (node.type === 'file') paths.add(node.rel_path)
      if (node.children) collect(node.children)
    })
    if (refreshed) { collect(refreshed.root_nodes); refreshed.groups.forEach((group) => collect(group.nodes)) }
    if (state.dirty) {
      const result = await checkConflict(projectId, { rel_path: state.activeRel,
        base_hash: state.detail?.hash || baseRef.current?.hash,
        current_content: state.detail ? documentContent(state.detail, state.text) : state.text })
      if (result.changed) setConflict(result)
      return
    }
    const reverted = change.reverted || change.status === 'reverted'
    const nextPath = reverted ? change.path : change.destination || change.path
    if (refreshed && !paths.has(state.activeRel) && !paths.has(nextPath)) {
      setActiveRel(''); setDetail(null); setText(''); setSelection(''); baseRef.current = null
      return
    }
    if (nextPath !== state.activeRel && refreshed && !paths.has(state.activeRel) && paths.has(nextPath)) await selectFile(nextPath)
    else await loadChapter(state.activeRel)
  }

  const openChatFile = async (path: string) => {
    if (isChapterPath(path) || path.toLowerCase().endsWith('.md')) {
      if (path === latestRef.current.activeRel) {
        if (latestRef.current.dirty && !await save({ quiet: true })) throw new Error('请先处理当前文件的未保存修改。')
        await loadChapter(path)
      }
      else await selectFile(path)
    } else {
      const file = await readChapter(projectId, path)
      setFilePreview({ path, content: file.content })
    }
  }

  // 追加导入草稿章：文件选择与拖拽共用同一处理
  const importDraftFile = async (file: File) => {
    try {
      const content = await file.text()
      const result = await importDocument(projectId, { filename: file.name, content })
      notify(`已导入 ${result.count} 章`, 'success')
      await loadTree()
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const chatFiles = useMemo(() => {
    const paths = new Set<string>()
    const walk = (nodes: TreeNode[]) => nodes.forEach((node) => {
      if (node.type === 'file') paths.add(node.rel_path)
      if (node.children) walk(node.children)
    })
    if (tree) { walk(tree.root_nodes); tree.groups.forEach((group) => walk(group.nodes)) }
    return Array.from(paths).sort()
  }, [tree])
  const chatCallbacks = useRef({ prepareChatContext, handleChatChanges, openChatFile })
  chatCallbacks.current = { prepareChatContext, handleChatChanges, openChatFile }
  useEffect(() => registerPageContext({
    projectId, pageType: 'editor',
    chapterRel: activeRel || null, targetChapterRel: targetChapterRel || null, selection,
    hasUnsavedChanges: dirty, availableFiles: chatFiles,
    prepareContext: () => chatCallbacks.current.prepareChatContext(),
    onFilesChanged: (changes) => chatCallbacks.current.handleChatChanges(changes),
    onOpenFile: (path) => chatCallbacks.current.openChatFile(path),
  }), [registerPageContext, projectId, activeRel, targetChapterRel, selection, dirty, chatFiles])

  // ── 自动保存 ──
  useEffect(() => {
    if (!dirty || conflict || composing || !settings?.editor.autosave_seconds) return
    const timer = window.setTimeout(() => void save(), settings.editor.autosave_seconds * 1000)
    return () => window.clearTimeout(timer)
  }, [dirty, conflict, composing, text, settings?.editor.autosave_seconds, save])

  // ── 阅读态：把沉浸单栏标记写到 <html>（CSS 据此收起侧栏与工具栏）──
  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-reading', mode === 'reading' ? 'on' : 'off')
    return () => root.setAttribute('data-reading', 'off')
  }, [mode])

  // ── 快捷键 ──
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing || composing) return
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
        event.preventDefault()
        void save()
        return
      }
      if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
        event.preventDefault()
        const index = chapters.findIndex((item) => item.rel_path === activeRel)
        if (index === -1) return
        const next = event.key === 'ArrowUp' ? index - 1 : index + 1
        if (next >= 0 && next < chapters.length) void selectFile(chapters[next].rel_path)
        return
      }
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'd') {
        event.preventDefault()
        setMode((current) => (current === 'reading' ? 'edit' : 'reading'))
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [save, chapters, activeRel, composing])

  // ── Ghost Text（空闲 2.5s 拉候选；Tab 接受 / Esc 拒绝）──
  useEffect(() => {
    if (composing || !ghostEnabled || !detail || !isChapterPath(detail.rel_path) || mode !== 'edit') {
      setGhost(null)
      return
    }
    let alive = true
    const timer = window.setTimeout(async () => {
      const tail = text.slice(-400)
      if (tail.trim().length < 20) return
      try {
        const result = await ghostText(projectId, {
          chapter_rel: detail.rel_path,
          prefix: tail,
          suffix: '',
        })
        const state = latestRef.current
        if (alive && state.projectId === projectId && state.activeRel === detail.rel_path && state.text === text && result.ok && result.candidate) {
          setGhost({ candidate: result.candidate, projectId, path: detail.rel_path, text })
        }
      } catch {
        if (alive) setGhost(null)
      }
    }, 2500)
    return () => { alive = false; window.clearTimeout(timer) }
  }, [text, ghostEnabled, detail, mode, projectId, composing])

  // ── 树 / 章节操作 ──
  const onCreateNode = async (parentRel: string, name: string, isDir: boolean) => {
    try {
      const node = await createNode(projectId, parentRel, name, isDir)
      notify(isDir ? '已创建目录' : `已创建 ${node.name}`, 'success')
      await loadTree()
      if (!isDir && node.is_chapter && node.rel_path) await selectFile(node.rel_path)
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const onRenameNode = async (node: TreeNode, newName: string) => {
    try {
      if (node.is_chapter) {
        // 章节改名 = 改标题；序号由系统固定，不重命名文件
        const updated = await updateChapterMeta(projectId, {
          rel_path: node.rel_path,
          title: newName,
        })
        if (latestRef.current.activeRel === node.rel_path) {
          setDetail(updated)
          latestRef.current = { ...latestRef.current, detail: updated }
        }
        notify(newName ? '章节名已更新' : '已清除章节名', 'success')
      } else {
        const openPath = latestRef.current.activeRel
        const containsOpenFile = openPath === node.rel_path || openPath.startsWith(`${node.rel_path}/`)
        if (containsOpenFile && latestRef.current.dirty && !await save({ quiet: true })) return
        const updated = await patchNode(projectId, { rel_path: node.rel_path, new_name: newName })
        notify('已重命名', 'success')
        if (containsOpenFile) await selectFile(updated.rel_path + openPath.slice(node.rel_path.length))
      }
      await loadTree()
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const onDeleteNode = async (node: TreeNode) => {
    try {
      const openPath = latestRef.current.activeRel
      const containsOpenFile = openPath === node.rel_path || openPath.startsWith(`${node.rel_path}/`)
      if (containsOpenFile && latestRef.current.dirty && !await save({ quiet: true })) return
      const result = await deleteNode(projectId, node.rel_path)
      notify(
        result.action === 'deleted'
          ? `「${node.name}」为空文件，已物理删除（不进回收站）`
          : `「${node.name}」已移入回收站（${result.reason ?? '含内容'}）`,
        'success',
      )
      if (containsOpenFile) {
        ++loadSequenceRef.current
        latestRef.current = { ...latestRef.current, activeRel: '', detail: null, text: '', dirty: false, selection: '' }
        setActiveRel(''); setDetail(null); setText(''); setSelection(''); baseRef.current = null
      }
      await loadTree()
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const onMoveNode = async (node: TreeNode, dstParentRel: string) => {
    try {
      const openPath = latestRef.current.activeRel
      const containsOpenFile = openPath === node.rel_path || openPath.startsWith(`${node.rel_path}/`)
      if (containsOpenFile && latestRef.current.dirty && !await save({ quiet: true })) return
      const updated = await patchNode(projectId, {
        rel_path: node.rel_path,
        dst_parent_rel: dstParentRel,
      })
      notify('已移动', 'success')
      if (containsOpenFile) await selectFile(updated.rel_path + openPath.slice(node.rel_path.length))
      await loadTree()
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const onNewChapter = async () => {
    try {
      const chapter = await createChapter(projectId)
      notify(`已新建 ${chapterLabel(chapter)}`, 'success')
      await loadTree()
      await selectFile(chapter.rel_path)
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  // ── 章节状态：只改元数据（chapters 表），不提交正文（避免与未保存的编辑打架）──
  const onSetStatus = async (status: string, relPath = detail?.rel_path) => {
    if (!relPath || !isChapterPath(relPath)) return
    const currentStatus = relPath === detail?.rel_path ? detail.status : chapters.find(chapter => chapter.rel_path === relPath)?.status
    if (status === currentStatus) return
    const token = statusAction.begin(relPath)
    if (!token) return
    try {
      const updated = await updateChapterMeta(projectId, {
        rel_path: relPath,
        status,
      })
      if (!statusAction.accept(token)) return
      const current = latestRef.current
      // Metadata changes must not adopt a new disk hash over unsaved author edits.
      if (current.projectId === projectId && current.activeRel === updated.rel_path && current.detail) {
        const next = { ...current.detail, status: updated.status }
        latestRef.current = { ...current, detail: next }
        setDetail(next)
      }
      notify(`章节状态：${status}`, 'success')
      await loadTree()
    } catch (err) {
      if (statusAction.accept(token)) notify(errorMessage(err), 'error')
    } finally { statusAction.finish(token) }
  }

  // ── 管线 / 合同 ──
  const onRunPipeline = async () => {
    if (!detail || !isChapterPath(detail.rel_path)) return
    setRunning(true)
    setPipelineLog([])
    try {
      const result = await runPipeline(projectId, {
        chapter_rel: detail.rel_path,
        resume: true,
        auto_freeze_contract: true,
        auto_apply: false,
        use_ai: true,
      })
      const lines = Object.entries(result.steps).map(
        ([step, data]) => `${step}：${data.status}`,
      )
      setPipelineLog([
        `管线状态：${result.status}`,
        ...lines,
        result.blocked_reasons?.length ? `阻断原因：${result.blocked_reasons.join('；')}` : '',
        result.review ? `审稿结论：${result.review.verdict}` : '',
        result.manual_reason ?? '',
      ].filter(Boolean))
      notify(
        result.status === 'done'
          ? '管线跑通，正文草稿已进收件箱（应用前请查看差异）'
          : `管线状态：${result.status}`,
        result.status === 'done' ? 'success' : 'info',
      )
      await loadChapter(detail.rel_path)
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setRunning(false)
    }
  }

  const onGenerateContract = async () => {
    if (!detail || !isChapterPath(detail.rel_path)) return
    try {
      const data = await generateContract(projectId, detail.rel_path, true)
      setContract(data)
      notify(`合同已生成（来源 ${data.source ?? 'model'}），确认后请冻结`, 'success')
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const onFreezeContract = async () => {
    if (!detail || !isChapterPath(detail.rel_path)) return
    try {
      const data = await freezeContract(projectId, detail.rel_path)
      setContract(data)
      notify('合同已冻结，可进入正文生产', 'success')
      const gateData = await checkGates(projectId, detail.rel_path)
      setGates(gateData)
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  const onPreviewContext = async () => {
    try {
      const result = await previewContext(projectId, {
        chapter_rel: targetChapterRel || null,
        query: [selection.trim(), ...(contract?.plot_points || []), ...(contract?.entities || [])].filter(Boolean).join(' ').slice(0, 1600)
          || `续写${detail?.title || targetChapterRel}，核对人物、物品和历史事件`,
        use_retrieval: true,
      })
      setContextPreview(result.preview.items)
      setContextRetrieval(result.preview.retrieval || null)
      notify(`上下文约 ${result.total_tokens} tokens / 预算 ${result.budget_tokens}`, 'info')
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }

  // ── 局部操作条 ──
  const onLocalOp = async (operation: string) => {
    if (!detail || !isChapterPath(detail.rel_path) || !selection.trim()) {
      notify('请先在正文里选中一段文字', 'error')
      return
    }
    setLocalBusy(true)
    const range = selectionRangeRef.current
    if (!range) { setLocalBusy(false); return }
    const target = { projectId, path: detail.rel_path, text, selection, ...range }
    try {
      const result = await localOperation(projectId, {
        chapter_rel: detail.rel_path,
        selection,
        operation,
      })
      const produced = String(result.text ?? '')
      if (latestRef.current.projectId !== target.projectId || latestRef.current.activeRel !== target.path || latestRef.current.text !== target.text) {
        notify('局部生成期间正文已变化，请重新选择需要处理的片段。', 'info')
        return
      }
      draftTarget.current = target
      setDraft(produced)
      setDraftSource(operation)
      notify(
        result.ok ? '已产出替换文本（在草稿区，可整段替换或丢弃）' : String(result.error_message ?? '生成失败'),
        result.ok ? 'success' : 'error',
      )
    } catch (err) {
      notify(errorMessage(err), 'error')
    } finally {
      setLocalBusy(false)
    }
  }

  const replaceSelection = () => {
    if (!draft.trim()) return
    const range = selectionRangeRef.current
    const target = draftTarget.current
    if (!target || target.projectId !== projectId || target.path !== activeRel || target.text !== text
      || !range || range.start !== target.start || range.end !== target.end || selection !== target.selection
      || replaceSelectedText(text, range, selection, draft) === null) {
      notify('选区内容已变化，请重新选择需要替换的正文。', 'error')
      return
    }
    if (chapterEditorRef.current) {
      if (!chapterEditorRef.current.replaceRange(range.start, range.end, draft, selection)) return
    } else {
      setText(replaceSelectedText(text, range, selection, draft)!)
      setDirty(true)
    }
    setDraft('')
    draftTarget.current = null
  }
  const acceptGhost = (source: 'keyboard' | 'button' = 'keyboard') => {
    if (!ghost || composing || !ghostEnabled || mode !== 'edit') return false
    const state = latestRef.current
    if (ghost.projectId !== state.projectId || ghost.path !== state.activeRel || ghost.text !== state.text) {
      setGhost(null)
      return false
    }
    const editor = chapterEditorRef.current
    if (!editor) return false
    const range = editor.getSelection()
    if (source === 'keyboard' && (range.start !== state.text.length || range.end !== state.text.length)) return false
    // A button accepts the chapter-tail continuation regardless of the current selection.
    // The adapter verifies the live document too, so a stale or repeated click cannot append it.
    if (!editor.appendText(ghost.candidate, ghost.text)) return false
    setGhost(null)
    return true
  }

  const wordCount = useMemo(() => text.replace(/\s/g, '').length, [text])
  // 字数门按汉字计数：区间比较同样只看汉字，与后端一致。
  const hanCount = useMemo(() => (text.match(/[\u4e00-\u9fff]/g) ?? []).length, [text])
  const rangeState = wordRange
    ? hanCount < wordRange.min ? 'under' as const : hanCount > wordRange.max ? 'over' as const : 'ok' as const
    : null
  const isChapter = chapters.some((item) => item.rel_path === activeRel)
  const budget = contract?.word_budget ?? 0
  const visibleChapters = useMemo(
    () => (statusFilter ? chapters.filter((item) => item.status === statusFilter) : chapters),
    [chapters, statusFilter],
  )

  // 下拉选项 = 筛选结果；当前章即使被筛掉也保留在列表首位，
  // 避免 value 悬空、也不会让正在编辑的章（可能含未保存正文）被切走。
  const dropdownChapters = useMemo(() => {
    const active = chapters.find((item) => item.rel_path === activeRel)
    if (!active || visibleChapters.some((item) => item.rel_path === activeRel)) {
      return visibleChapters
    }
    return [active, ...visibleChapters]
  }, [chapters, visibleChapters, activeRel])

  return (
    <div className="page page--wide page--editor editor-workspace" data-layout={workspaceLayout}>
      <header className="page-header page-header--compact">
        <div className="page-header__compact-main">
          <h1 className="page-header__title">{detail?.title || activeRel.split('/').pop() || '正文编辑器'}</h1>
          <span className="page-header__desc">
            Ctrl+S 保存 · Alt+↑/↓ 切章 · Ctrl+Shift+D 阅读态
          </span>
        </div>
        <div className="btn-row">
          <button className="btn btn--sm" type="button" aria-label="打开文档目录" onClick={() => workspaceLayout === 'wide' ? setLeftOpen((open) => !open) : setDirectoryOverlay(true)}>目录</button>
          <button className="btn btn--sm" type="button" onClick={() => void onNewChapter()}>新建章节</button>
          <button className="btn btn--sm" type="button" aria-label="打开本书助手" onClick={() => workspaceLayout !== 'compact' && mode !== 'reading' ? setRightOpen((open) => !open) : openChat()}>写作助手</button>
        </div>
      </header>

      <div
        ref={editorRef}
        className={`editor${mode === 'reading' ? ' editor--reading' : ''}`}
        style={
          {
            '--editor-left-w': `${leftWidth}px`,
            '--editor-right-w': `${rightWidth}px`,
          } as CSSProperties
        }
      >
        {/* 左栏：文档树（可收起 / 可拖宽） */}
        {leftOpen ? (
          <aside className="editor__col editor__col--left">
            <div className="editor__bar">
              <div className="row" style={{ flexWrap: 'nowrap' }}>
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  title="收起文档树"
                  aria-label="收起文档树"
                  onClick={() => setLeftOpen(false)}
                >
                  «
                </button>
                <span className="muted">文档树</span>
              </div>
              <button className="btn btn--ghost btn--sm" type="button" onClick={() => void onNewChapter()}>
                新建章节
              </button>
            </div>
            <div className="editor__body editor__body--flush">
              <DocumentTree
                tree={tree}
                activeRel={activeRel}
                onOpen={(path) => { void selectFile(path) }}
                onCreate={onCreateNode}
                onRename={onRenameNode}
                onDelete={onDeleteNode}
                onMove={onMoveNode}
                onSetStatus={(node, status) => { void onSetStatus(status, node.rel_path) }}
                pendingStatusRel={statusAction.pending}
              />
            </div>
          </aside>
        ) : null}

        {leftOpen ? (
          <PanelResizer
            className="panel-resizer--left"
            side="left"
            value={leftWidth}
            min={180}
            max={420}
            label="调整文档树宽度"
            onChange={setLeftWidth}
            onPreview={(next) => editorRef.current?.style.setProperty('--editor-left-w', `${next}px`)}
            onToggle={() => setLeftOpen(false)}
          />
        ) : null}

        {/* 中栏：正文 */}
        <section className="editor__col editor__col--center">
          <div className="editor__bar">
            <div className="editor-toolbar">
              <label className="editor-toolbar__field editor-toolbar__field--grow">
                <span className="editor-toolbar__label">章节</span>
                <select
                  className="select"
                  value={isChapter ? activeRel : ''}
                  onChange={(event) => { void selectFile(event.target.value) }}
                  disabled={dropdownChapters.length === 0}
                  aria-label="选择章节"
                >
                  {!isChapter ? <option value="">{activeRel ? '当前编辑非章节文件' : '未选择章节'}</option> : null}
                  {dropdownChapters.length === 0 ? (
                    isChapter ? <option value="">{chapters.length === 0 ? '暂无章节' : '当前筛选下无章节'}</option> : null
                  ) : (
                    dropdownChapters.map((item) => (
                      <option key={item.rel_path} value={item.rel_path}>
                        {chapterLabelWithCount(item)}
                      </option>
                    ))
                  )}
                </select>
              </label>
              <label className="editor-toolbar__field editor-toolbar__field--status">
                <span className="editor-toolbar__label">筛选</span>
                <select
                  className="select"
                  value={statusFilter}
                  onChange={(event) => setStatusFilter(event.target.value)}
                  aria-label="按状态筛选章节"
                >
                  <option value="">全部</option>
                  <option value="草稿">草稿</option>
                  <option value="完成">完成</option>
                  <option value="发表">发表</option>
                </select>
              </label>
              <span className="editor-toolbar__count">
                {statusFilter ? `${visibleChapters.length} / ${chapters.length} 章` : `共 ${chapters.length} 章`}
              </span>
            </div>
            <span className="editor-toolbar__active-file" title={activeRel || '未选择文件'}>
              <strong>{activeRel || '未选择文件'}</strong><span className={dirty ? 'editor-save-state is-dirty' : 'editor-save-state'}>{saving ? '保存中' : dirty ? '未保存' : detail ? '已保存' : ''}</span>
            </span>
            <div className="btn-row">
              {isChapter ? <label className="editor-toolbar__field editor-toolbar__field--status">
                <span className="editor-toolbar__label">本章状态</span>
                <select
                  className="select"
                  value={detail?.status ?? ''}
                  disabled={!detail || !!statusAction.pending}
                  aria-label="设置当前章节状态"
                  onChange={(event) => void onSetStatus(event.target.value)}
                >
                  <option value="">—</option>
                  <option value="草稿">草稿</option>
                  <option value="完成">完成</option>
                  <option value="发表">发表</option>
                </select>
              </label> : null}
              <button
                className="btn btn--sm"
                type="button"
                aria-pressed={mode === 'edit'}
                onClick={() => setMode('edit')}
              >
                编辑
              </button>
              <button
                className="btn btn--sm"
                type="button"
                aria-pressed={mode === 'preview'}
                onClick={() => setMode('preview')}
              >
                预览
              </button>
              <button
                className="btn btn--sm"
                type="button"
                aria-pressed={mode === 'reading'}
                onClick={() => setMode(mode === 'reading' ? 'edit' : 'reading')}
              >
                阅读态
              </button>
              <button className="btn btn--primary btn--sm" type="button" disabled={saving || !detail} onClick={() => void save()}>
                {saving ? '保存中…' : '保存'}
              </button>
            </div>
          </div>

          <div className="editor__body">
            {evidenceNotice && <p className="field__hint" role="status">{evidenceNotice}</p>}
            {draftNotice && <p className="field__hint" role="status">{draftNotice}</p>}
            <ResourceState {...treeResource} hasData={treeResource.loaded && !!tree} onRetry={() => void loadTree()}>
            <ResourceState {...(activeRel ? chapterResource : { loading: false, error: '', loaded: true })} hasData={!!detail} onRetry={() => void loadChapter(activeRel)}>
            {!detail ? (
              <div className="empty-state">
                <p className="empty-state__title">请选择或新建一章</p>
                <p className="empty-state__desc">左栏「新建章节」即可开始写。</p>
              </div>
            ) : mode === 'edit' ? (
              <>
                {isChapter ? <ChapterEditor
                  ref={chapterEditorRef}
                  value={text}
                  documentKey={`${projectId}:${activeRel}`}
                  decorations={decorations}
                  historyStore={editorHistory.current}
                  onChange={(value) => { setText(value); setDirty(true); setGhost(null) }}
                  onSelect={(range) => { setSelection(range.text); selectionRangeRef.current = { start: range.start, end: range.end } }}
                  onCompositionChange={setComposing}
                  onAcceptGhost={acceptGhost}
                  onRejectGhost={() => { if (!ghost) return false; setGhost(null); return true }}
                /> : <textarea
                  autoComplete={NO_AUTOFILL}
                  ref={textareaRef}
                  className="prose-editor"
                  value={text}
                  spellCheck={false}
                  onChange={(event) => {
                    setText(event.target.value)
                    setDirty(true)
                    setGhost(null)
                  }}
                  onSelect={(event) => {
                    const target = event.target as HTMLTextAreaElement
                    setSelection(target.value.slice(target.selectionStart, target.selectionEnd))
                    selectionRangeRef.current = { start: target.selectionStart, end: target.selectionEnd }
                  }}
                  style={{ minHeight: '52vh' }}
                />}
                {ghost ? (
                  <div className="ghost-chip">
                    <div className="muted">续写候选 · 接受后追加到章末</div>
                    {ghost.candidate}
                    <div className="row" style={{ marginTop: 'var(--space-2)' }}>
                      <button
                        className="btn btn--sm btn--primary"
                        type="button"
                        disabled={composing}
                        onClick={() => {
                          if (!acceptGhost('button')) notify('续写候选已失效或正文正在输入，请稍后重试。', 'info')
                        }}
                      >
                        Tab 接受
                      </button>
                      <button className="btn btn--sm" type="button" onClick={() => setGhost(null)}>
                        Esc 拒绝
                      </button>
                    </div>
                  </div>
                ) : null}
                {isChapter && selection.trim() ? (
                  <div className="local-op-bar">
                    <span className="muted">选中 {selection.replace(/\s/g, '').length} 字</span>
                    {[
                      ['rewrite', '改写'],
                      ['expand', '扩写'],
                      ['shrink', '缩写'],
                      ['polish', '润色'],
                    ].map(([key, label]) => (
                      <button
                        key={key}
                        className="btn btn--sm"
                        type="button"
                        disabled={localBusy}
                        onClick={() => void onLocalOp(key)}
                      >
                        {label}
                      </button>
                    ))}
                    <button className="btn btn--primary btn--sm" type="button" onClick={() => attachSelection({ path: activeRel, text: selection, start: selectionRangeRef.current?.start, end: selectionRangeRef.current?.end, baseHash: dirty ? undefined : detail?.hash })}>交给助手</button>
                    <button className="btn btn--ghost btn--sm" type="button" onClick={() => setSelection('')}>
                      取消选择
                    </button>
                  </div>
                ) : null}
              </>
            ) : (
              <div className={`prose-reading${isChapter ? '' : ' prose-reading--document'}`}>
                <MarkdownView
                  text={text}
                  plain={isChapter}
                  document={!isChapter}
                  sourceLabels={!isChapter && /^(设定|状态|大纲)\//.test(activeRel)}
                  decorations={isChapter ? decorations : undefined}
                  milestone={{
                    step: milestone.step,
                    enabled: isChapter && milestone.enabled && (settings?.editor.milestone_inline ?? true),
                    budget,
                    decorate: mode === 'reading' || mode === 'preview',
                  }}
                />
              </div>
            )}
            </ResourceState>
            </ResourceState>
          </div>

          <div className="milestone-rail">
            <span>{isChapter ? '正文' : '文件'} {wordCount} 字</span>
            {isChapter && wordRange ? (
              <span className={rangeState === 'under' ? 'tag tag--danger' : rangeState === 'over' ? 'tag tag--warn' : undefined}
                title={`每章字数区间 ${wordRange.min}–${wordRange.max} 汉字（下限不足会被拒收，超上限仅告警）`}>
                区间 {wordRange.min}–{wordRange.max} 汉字 · 当前 {hanCount}
                {rangeState === 'under' ? '（低于下限，落盘会被拒收）'
                  : rangeState === 'over' ? '（高于上限，仅告警）' : ''}
              </span>
            ) : null}
            {isChapter && milestone.enabled && milestone.step > 0 ? (
              <span>
                锚点：已满 {Math.floor(wordCount / milestone.step) * milestone.step} 字 / 每 {milestone.step} 字
              </span>
            ) : isChapter ? (
              <span className="muted">里程碑提示已关闭（可在设置开启）</span>
            ) : null}
            {isChapter && budget ? (
              <span>
                预算 {budget} · {Math.min(100, Math.round((wordCount / budget) * 100))}%
              </span>
            ) : isChapter ? (
              <span className="muted">本章无合同预算</span>
            ) : null}
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setSnapshotOpen(true)}>
              历史快照
            </button>
          </div>

            {draft ? (
            <div className="draft-area">
              <div className="row row--between">
                <span className="muted">草稿区（{draftSource}）</span>
                <div className="btn-row">
                  <button className="btn btn--primary btn--sm" type="button" onClick={replaceSelection}>
                    替换选中内容
                  </button>
                  <button className="btn btn--sm" type="button" onClick={() => setDraft('')}>
                    丢弃
                  </button>
                </div>
              </div>
              <pre className="run-log" style={{ marginTop: 'var(--space-2)' }}>
                {draft}
              </pre>
            </div>
          ) : null}
        </section>

        {rightOpen ? (
          <PanelResizer
            className="panel-resizer--right"
            side="right"
            value={rightWidth}
            min={280}
            max={560}
            label="调整对话面板宽度"
            onChange={setRightWidth}
            onPreview={(next) => editorRef.current?.style.setProperty('--editor-right-w', `${next}px`)}
            onToggle={() => setRightOpen(false)}
          />
        ) : null}

        {/* 右栏：陪练面板（可收起 / 可拖宽） */}
        {rightOpen && workspaceLayout !== 'compact' && mode !== 'reading' ? (
          <aside className="editor__col editor__col--right" ref={setChatHost}>
          </aside>
        ) : null}
      </div>
      <Drawer title="本书文档目录" open={directoryOverlay} onClose={() => setDirectoryOverlay(false)} width={420}>
        <button className="btn btn--primary btn--sm" onClick={() => void onNewChapter()}>新建章节</button>
        <DocumentTree tree={tree} activeRel={activeRel} onOpen={(path) => { void selectFile(path).then(() => setDirectoryOverlay(false)) }} onCreate={onCreateNode} onRename={onRenameNode} onDelete={onDeleteNode} onMove={onMoveNode} onSetStatus={(node, status) => { void onSetStatus(status, node.rel_path) }} pendingStatusRel={statusAction.pending} />
      </Drawer>

      {/* 底部抽屉：章节合同与 8 步循环（可收起，保住编辑区视口高度） */}
      {isChapter ? <section className={`workbench-drawer${drawerOpen ? ' is-open' : ''}`}>
        <div className="workbench-drawer__strip">
          <button
            className="btn btn--ghost btn--sm"
            type="button"
            aria-expanded={drawerOpen}
            onClick={() => setDrawerOpen((open) => !open)}
          >
            {drawerOpen ? '▾' : '▴'} 合同 · 8 步循环
          </button>
          {gates?.prerequisites.blocked ? (
            <span className="tag tag--danger" title={gates.prerequisites.reasons.join('；')}>
              门禁阻断
            </span>
          ) : gates ? (
            <span className={gates.contract.ok ? 'tag tag--ok' : 'tag tag--warn'}>
              合同{gates.contract.ok ? '已冻结' : '未就绪'}
            </span>
          ) : null}
          <div className="workbench-drawer__spacer" />
          <label
            className={`btn btn--ghost btn--sm${draftDrag ? ' is-dragover' : ''}`}
            style={{ cursor: 'pointer' }}
            onDragOver={(event) => { event.preventDefault(); setDraftDrag(true) }}
            onDragLeave={() => setDraftDrag(false)}
            onDrop={(event) => {
              event.preventDefault()
              setDraftDrag(false)
              const file = event.dataTransfer.files?.[0]
              if (file) void importDraftFile(file)
            }}
          >
            追加导入片段为草稿章（也可拖入 .md/.txt）
            <input
              type="file"
              accept=".md,.txt"
              style={{ display: 'none' }}
              onChange={async (event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (!file) return
                await importDraftFile(file)
              }}
            />
          </label>
        </div>
        {drawerOpen ? (
          <div className="workbench-drawer__body">
            <section className="panel">
              <header className="panel__header">
                <h2 className="panel__title">章节合同与 8 步循环</h2>
                <div className="btn-row">
                  <button className="btn btn--sm" type="button" disabled={!detail} onClick={() => void onGenerateContract()}>
                    生成合同
                  </button>
                  <button
                    className="btn btn--sm"
                    type="button"
                    disabled={!contract || contract.status === 'frozen'}
                    onClick={() => void onFreezeContract()}
                  >
                    冻结合同
                  </button>
                  <button className="btn btn--sm" type="button" disabled={!detail} onClick={() => void onPreviewContext()}>
                    上下文预览
                  </button>
                  <button className="btn btn--primary btn--sm" type="button" disabled={running || !detail} onClick={() => void onRunPipeline()}>
                    {running ? '管线执行中…' : '跑 8 步循环'}
                  </button>
                </div>
              </header>
              <div className="panel__body">
          {gates ? (
            <div className="row" style={{ marginBottom: 'var(--space-3)' }}>
              {gates.prerequisites.checks.map((check) => (
                <span
                  key={check.key}
                  className={check.ok ? 'tag tag--ok' : check.optional ? 'tag tag--warn' : 'tag tag--danger'}
                  title={check.hint}
                >
                  {check.label}
                </span>
              ))}
              <span className={gates.contract.ok ? 'tag tag--ok' : 'tag tag--danger'}>
                合同{gates.contract.ok ? '已冻结' : '未就绪'}
              </span>
              {gates.prerequisites.blocked ? (
                <span className="muted">阻断：{gates.prerequisites.reasons.join('；')}</span>
              ) : null}
            </div>
          ) : null}

          {contract ? (
            <div className="stack stack--tight">
              <div className="row">
                <span className={contract.status === 'frozen' ? 'tag tag--ok' : 'tag tag--warn'}>
                  {contract.status === 'frozen' ? '已冻结' : '草稿'}
                </span>
                <span className="tag">预算 {contract.word_budget} 字</span>
                <span className="tag">钩子 {contract.hook_type}</span>
                <span className="muted">来源 {contract.source ?? '—'}</span>
              </div>
              <ol className="muted" style={{ margin: 0, paddingLeft: 'var(--space-5)' }}>
                {contract.plot_points.map((point, index) => (
                  <li key={index}>{point}</li>
                ))}
              </ol>
              {contract.constraints?.length ? (
                <p className="muted">禁止事项：{contract.constraints.join('；')}</p>
              ) : null}
            </div>
          ) : (
            <p className="muted">本章还没有合同。生成后请确认内容再冻结（冻结后修改需差异确认）。</p>
          )}

          {pipelineLog.length ? (
            <pre className="run-log" style={{ marginTop: 'var(--space-3)' }}>
              {pipelineLog.join('\n')}
            </pre>
          ) : null}

          {contextPreview.length ? (
            <table className="table" style={{ marginTop: 'var(--space-3)' }}>
              <thead>
                <tr>
                  <th>级别</th>
                  <th>材料</th>
                  <th>Tokens</th>
                  <th>必读</th>
                  <th>依据</th>
                </tr>
              </thead>
              <tbody>
                {contextPreview.map((item) => (
                  <tr key={`${item.level}-${item.title}`}>
                    <td>{item.level}</td>
                    <td>{item.title}</td>
                    <td className="mono">{item.tokens}</td>
                    <td>{item.mandatory ? '是' : '否'}</td>
                    <td>{item.retrieval?.rel_path ? <Link to={evidenceEditorUrl(projectId, {
                      rel_path: item.retrieval.rel_path,
                      line_start: item.retrieval.line_start,
                      line_end: item.retrieval.line_end,
                      evidence_id: item.retrieval.evidence_id || item.retrieval.id,
                      document_hash: item.retrieval.document_hash || item.retrieval.hash,
                    })}>{item.retrieval.rel_path} · L{item.retrieval.line_start || 1}</Link> : item.source}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
          {contextRetrieval && <div className="field__hint" style={{ marginTop: 'var(--space-2)' }}>
            <strong>历史检索：</strong>{contextRetrieval.queries?.length ? contextRetrieval.queries.join('；') : '本次无检索词'}
            {contextRetrieval.hits?.length ? <span> · 命中 {contextRetrieval.hits.length} 条，注入 {contextRetrieval.hits.filter((hit) => hit.injected).length} 条</span> : <span> · 无命中</span>}
            {contextRetrieval.degradation?.map((reason, index) => <p key={`${index}-${reason}`}>检索降级：{reason}</p>)}
          </div>}
              </div>
            </section>
            <p className="muted" style={{ marginTop: 'var(--space-3)' }}>
              文件在别处改动后，重进页面即可刷新。
            </p>
          </div>
        ) : null}
      </section> : null}

      {/* 冲突弹窗 */}
      <Modal
        title="检测到外部改动"
        open={conflict !== null}
        onClose={() => setConflict(null)}
        footer={
          <div className="btn-row btn-row--end">
            <button
              className="btn"
              type="button"
              onClick={async () => {
                if (!detail) return
                const expectedText = latestRef.current.text
                try {
                  await resolveConflict(projectId, {
                    rel_path: detail.rel_path,
                    mode: 'take-external',
                  })
                  setConflict(null)
                  await loadChapter(detail.rel_path, { replaceDirtyText: expectedText })
                  notify('已采用外部版本', 'success')
                } catch (err) {
                  notify(errorMessage(err), 'error')
                }
              }}
            >
              采用外部
            </button>
            <button
              className="btn btn--primary"
              type="button"
              onClick={async () => {
                if (!detail) return
                const expectedText = latestRef.current.text
                try {
                  await resolveConflict(projectId, {
                    rel_path: detail.rel_path,
                    mode: 'keep-mine',
                    content: documentContent(detail, text),
                  })
                  setConflict(null)
                  notify('已保留我的版本（外部版本已存快照）', 'success')
                  await loadChapter(detail.rel_path, { replaceDirtyText: expectedText })
                } catch (err) {
                  notify(errorMessage(err), 'error')
                }
              }}
            >
              保留我的
            </button>
          </div>
        }
      >
        <p className="muted">
          该文件在编辑器外被修改（{conflict?.reason === 'content' ? '内容变化' : '时间戳变化'}）。
          下面是「我的版本 → 外部版本」的行级差异。
        </p>
        <DiffView lines={conflict?.diff ?? []} />
      </Modal>

      <Drawer title={filePreview?.path || '文件内容'} open={filePreview !== null} onClose={() => setFilePreview(null)} width={920}>
        <MarkdownView text={filePreview?.content || ''} document sourceLabels={/^(设定|状态|大纲)\//.test(filePreview?.path || '')} />
      </Drawer>

      {/* 快照 */}
      <Drawer title="历史快照" open={snapshotOpen} onClose={() => { setSnapshotOpen(false); setConfirmingSnapshot(null) }} width={860}>
        {snapshots.length === 0 ? (
          <p className="muted">暂无快照（保存与应用类操作会自动产生快照）。</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>时间</th>
                <th>原因</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {snapshots.map((item) => (
                <tr key={item.id}>
                  <td className="mono">{item.created_at}</td>
                  <td>{item.reason}</td>
                  <td>
                    {confirmingSnapshot === item.id ? (
                      <div className="btn-row">
                        <button
                          className="btn btn--danger btn--sm"
                          type="button"
                          onClick={async () => {
                            setConfirmingSnapshot(null)
                            const expectedText = latestRef.current.text
                            try {
                              await restoreSnapshot(projectId, item.id)
                              notify('已回滚到该快照（回滚前已再存一份）', 'success')
                              setSnapshotOpen(false)
                              if (activeRel) await loadChapter(activeRel, { replaceDirtyText: expectedText })
                            } catch (err) {
                              notify(errorMessage(err), 'error')
                            }
                          }}
                        >
                          确认回滚
                        </button>
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          onClick={() => setConfirmingSnapshot(null)}
                        >
                          取消
                        </button>
                      </div>
                    ) : (
                      <button
                        className="btn btn--sm"
                        type="button"
                        onClick={() => setConfirmingSnapshot(item.id)}
                      >
                        回滚到此版本
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Drawer>
    </div>
  )
}
