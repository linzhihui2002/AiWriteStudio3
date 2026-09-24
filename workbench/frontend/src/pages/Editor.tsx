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
import { Link, useParams } from 'react-router-dom'
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
  ghostText,
  importDocument,
  listChapters,
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
  Contract,
  ProjectTree,
  TreeNode,
} from '../api/types'
import ChatPanel from '../components/ChatPanel'
import DiffView from '../components/DiffView'
import DocumentTree from '../components/DocumentTree'
import MarkdownView from '../components/MarkdownView'
import Modal from '../components/Modal'
import PanelResizer from '../components/PanelResizer'
import { errorMessage, useToast } from '../state/useToast'
import { usePanelWidth } from '../state/usePanelWidth'
import { useSettings } from '../state/useSettings'
import { NO_AUTOFILL } from '../lib/autofill'
import { chapterLabel, chapterLabelWithCount } from '../lib/chapterName'

const isChapterPath = (relPath: string) => /^章节\/第\d{4,}章\.md$/.test(relPath)

export default function Editor() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const { push: notify } = useToast()
  const { settings } = useSettings()

  const [tree, setTree] = useState<ProjectTree | null>(null)
  const [chapters, setChapters] = useState<ChapterSummary[]>([])
  const [activeRel, setActiveRel] = useState('')
  const [targetChapterRel, setTargetChapterRel] = useState('')
  const [detail, setDetail] = useState<ChapterDetail | null>(null)
  const [text, setText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [mode, setMode] = useState<'edit' | 'preview' | 'reading'>('edit')
  const [statusFilter, setStatusFilter] = useState<string>('')

  const [contract, setContract] = useState<Contract | null>(null)
  const [gates, setGates] = useState<{ prerequisites: { blocked: boolean; reasons: string[]; checks: Array<{ key: string; label: string; ok: boolean; hint: string; optional?: boolean }> }; contract: { ok: boolean; hint: string } } | null>(null)
  const [pipelineLog, setPipelineLog] = useState<string[]>([])
  const [running, setRunning] = useState(false)

  const [conflict, setConflict] = useState<ConflictCheck | null>(null)
  const [contextPreview, setContextPreview] = useState<Array<{ level: number; title: string; tokens: number; mandatory: boolean }>>([])

  const [selection, setSelection] = useState('')
  const [localBusy, setLocalBusy] = useState(false)
  const [draft, setDraft] = useState('')
  const [draftSource, setDraftSource] = useState('')
  const [ghost, setGhost] = useState('')

  const [snapshots, setSnapshots] = useState<Array<{ id: number; reason: string; created_at: string }>>([])
  const [snapshotOpen, setSnapshotOpen] = useState(false)

  // ── 面板折叠 + 宽度（纯 UI 偏好，localStorage 记忆；宽度可拖拽分隔条调整）──
  const [leftOpen, setLeftOpen] = useState(() => localStorage.getItem('aiw.editor.leftOpen') !== '0')
  const [rightOpen, setRightOpen] = useState(() => localStorage.getItem('aiw.editor.rightOpen') !== '0')
  const [drawerOpen, setDrawerOpen] = useState(() => localStorage.getItem('aiw.editor.drawerOpen') === '1')
  const [leftWidth, setLeftWidth] = usePanelWidth('aiw.editor.leftWidth', 260, 180, 420)
  const [rightWidth, setRightWidth] = usePanelWidth('aiw.editor.rightWidth', 320, 280, 560)
  const editorRef = useRef<HTMLDivElement>(null)

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
  const saveInFlightRef = useRef<Promise<boolean> | null>(null)
  const loadSequenceRef = useRef(0)
  const selectionRangeRef = useRef<{ start: number; end: number } | null>(null)
  const latestRef = useRef({ projectId, activeRel, detail, text, dirty, selection })
  latestRef.current = { projectId, activeRel, detail, text, dirty, selection }
  const [filePreview, setFilePreview] = useState<{ path: string; content: string } | null>(null)

  useEffect(() => {
    setActiveRel(''); setTargetChapterRel(''); setDetail(null); setText(''); setDirty(false); setSelection('')
    baseRef.current = null; selectionRangeRef.current = null; setConflict(null)
  }, [projectId])

  const milestone = settings?.milestone ?? { enabled: true, step: 500 }
  const ghostEnabled = settings?.editor.ghost_text ?? true

  // ── 载入 ──
  const loadTree = useCallback(async () => {
    try {
      const data = await getTree(projectId)
      setTree(data)
      const list = await listChapters(projectId)
      setChapters(list)
      setTargetChapterRel((current) => list.some((item) => item.rel_path === current)
        ? current : list[0]?.rel_path ?? '')
      if (!activeRel && list.length) setActiveRel(list[0].rel_path)
      return data
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
  }, [projectId, activeRel, notify])

  useEffect(() => {
    void loadTree()
  }, [loadTree])

  useEffect(() => {
    if (chapters.some((chapter) => chapter.rel_path === activeRel)) setTargetChapterRel(activeRel)
  }, [activeRel, chapters])

  const loadChapter = useCallback(
    async (relPath: string) => {
      if (!relPath) return
      const requestId = ++loadSequenceRef.current
      try {
        const data = await readChapter(projectId, relPath)
        if (requestId !== loadSequenceRef.current || latestRef.current.projectId !== projectId
          || latestRef.current.activeRel !== relPath) return
        // 后台刷新和文件切换期间，绝不让迟到的读取覆盖作者尚未保存的输入。
        if (latestRef.current.dirty) return
        setDetail(data)
        setText(data.body)
        setDirty(false)
        latestRef.current = { ...latestRef.current, detail: data, text: data.body, dirty: false, selection: '' }
        baseRef.current = data.hash ? { mtime: data.mtime || 0, hash: data.hash } : null
        setSelection('')
        selectionRangeRef.current = null
        setDraft('')
        setGhost('')
        const [contractData, gateData, snapshots] = await Promise.all([
          isChapterPath(relPath) ? getContract(projectId, relPath).catch(() => null) : Promise.resolve(null),
          isChapterPath(relPath) ? checkGates(projectId, relPath).catch(() => null) : Promise.resolve(null),
          listSnapshots(projectId, relPath).catch(() => []),
        ])
        if (requestId !== loadSequenceRef.current || latestRef.current.activeRel !== relPath) return
        setContract(contractData)
        setGates(gateData)
        setSnapshots(snapshots)
      } catch (err) {
        notify(errorMessage(err), 'error')
      }
    },
    [projectId, notify],
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
      const content = document.content.slice(0, document.content.length - document.body.length) + state.text
      const operation = (async () => {
        setSaving(true)
        try {
          if (!options.force && baseline) {
            const result = await checkConflict(state.projectId, { rel_path: document.rel_path,
              base_mtime: baseline.mtime, base_hash: baseline.hash, current_content: content })
            if (result.changed) { setConflict(result); return false }
          }
          const saved = await saveChapter(state.projectId, document.rel_path, content, document.status,
            options.force ? undefined : document.hash || baseline?.hash)
          if (latestRef.current.projectId === state.projectId && latestRef.current.activeRel === document.rel_path) {
            const stillDirty = latestRef.current.text !== state.text
            latestRef.current = { ...latestRef.current, detail: saved, dirty: stillDirty }
            setDetail(saved)
            setDirty(stillDirty)
            baseRef.current = saved.hash ? { mtime: saved.mtime || 0, hash: saved.hash } : baseline
          }
          if (!options.quiet) notify(`已保存 · ${saved.word_count} 字`, 'success')
          await loadTree()
          return true
        } catch (err) {
          // 保存前检查与实际落盘之间的并发变化仍由 expected_hash 原子保护。
          if (baseline) {
            const changed = await checkConflict(state.projectId, { rel_path: document.rel_path,
              base_hash: baseline.hash, current_content: content }).catch(() => null)
            if (changed?.changed) setConflict(changed)
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
    setDetail(null)
    setText('')
    setDirty(false)
    setSelection('')
    setGhost('')
    setActiveRel(relPath)
  }

  const prepareChatContext = async (): Promise<ChatContext> => {
    const initial = latestRef.current
    const target = chapters.some((chapter) => chapter.rel_path === targetChapterRel)
      ? targetChapterRel : undefined
    // Unsupported automatic attachments are reported by the run service. They
    // must not block the author's message or try to save a non-Markdown target.
    if (initial.activeRel && !initial.activeRel.toLowerCase().endsWith('.md')) return { active_file: initial.activeRel, target_chapter: target }
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
        current_content: state.detail ? state.detail.content.slice(0, state.detail.content.length - state.detail.body.length) + state.text : state.text })
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
    if (path.toLowerCase().endsWith('.md')) {
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

  const chatFiles = useMemo(() => {
    const paths = new Set<string>()
    const walk = (nodes: TreeNode[]) => nodes.forEach((node) => {
      if (node.type === 'file') paths.add(node.rel_path)
      if (node.children) walk(node.children)
    })
    if (tree) { walk(tree.root_nodes); tree.groups.forEach((group) => walk(group.nodes)) }
    return Array.from(paths).sort()
  }, [tree])

  // ── 自动保存 ──
  useEffect(() => {
    if (!dirty || conflict || !settings?.editor.autosave_seconds) return
    const timer = window.setTimeout(() => void save(), settings.editor.autosave_seconds * 1000)
    return () => window.clearTimeout(timer)
  }, [dirty, conflict, text, settings?.editor.autosave_seconds, save])

  // ── 阅读态：把沉浸单栏标记写到 <html>（CSS 据此收起侧栏与工具栏）──
  useEffect(() => {
    const root = document.documentElement
    root.setAttribute('data-reading', mode === 'reading' ? 'on' : 'off')
    return () => root.setAttribute('data-reading', 'off')
  }, [mode])

  // ── 快捷键 ──
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
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
  }, [save, chapters, activeRel])

  // ── Ghost Text（空闲 2.5s 拉候选；Tab 接受 / Esc 拒绝）──
  useEffect(() => {
    if (!ghostEnabled || !detail || !isChapterPath(detail.rel_path) || mode !== 'edit') return
    const timer = window.setTimeout(async () => {
      const tail = text.slice(-400)
      if (tail.trim().length < 20) return
      try {
        const result = await ghostText(projectId, {
          chapter_rel: detail.rel_path,
          prefix: tail,
          suffix: '',
        })
        if (result.ok && result.candidate) setGhost(result.candidate)
      } catch {
        setGhost('')
      }
    }, 2500)
    return () => window.clearTimeout(timer)
  }, [text, ghostEnabled, detail, mode, projectId])

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

  // ── 章节状态：只改 frontmatter，不提交正文（避免与未保存的编辑打架）──
  const onSetStatus = async (status: string) => {
    if (!detail || !isChapterPath(detail.rel_path) || status === detail.status) return
    try {
      const updated = await updateChapterMeta(projectId, {
        rel_path: detail.rel_path,
        status,
      })
      setDetail(updated)
      notify(`本章状态：${status}`, 'success')
      await loadTree()
    } catch (err) {
      notify(errorMessage(err), 'error')
    }
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
        query: '',
        use_retrieval: true,
      })
      setContextPreview(
        result.preview.items.map((item) => ({
          level: item.level,
          title: item.title,
          tokens: item.tokens,
          mandatory: item.mandatory,
        })),
      )
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
    try {
      const result = await localOperation(projectId, {
        chapter_rel: detail.rel_path,
        selection,
        operation,
      })
      const produced = String(result.text ?? '')
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
    const next = text.replace(selection, draft)
    setText(next)
    setDirty(true)
    setDraft('')
  }

  const wordCount = useMemo(() => text.replace(/\s/g, '').length, [text])
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
    <div className="page page--wide page--editor">
      <header className="page-header page-header--compact">
        <div className="page-header__compact-main">
          <h1 className="page-header__title">{tree?.project.name ?? '编辑器'}</h1>
          <span className="page-header__desc">
            Ctrl+S 保存 · Alt+↑/↓ 切章 · Ctrl+Shift+D 阅读态
          </span>
        </div>
        <div className="btn-row">
          <Link className="btn btn--sm" to={`/project/${projectId}/outline`}>
            大纲
          </Link>
          <Link className="btn btn--sm" to={`/project/${projectId}/bible`}>
            Story Bible
          </Link>
          <Link className="btn btn--sm" to={`/project/${projectId}/review`}>
            审稿
          </Link>
          <Link className="btn btn--sm" to={`/project/${projectId}/cards`}>
            设定卡片
          </Link>
          <Link className="btn btn--sm" to="/inbox">
            收件箱
          </Link>
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
              {!leftOpen ? (
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  title="展开文档树"
                  aria-label="展开文档树"
                  onClick={() => setLeftOpen(true)}
                >
                  » 目录
                </button>
              ) : null}
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
              正在编辑：<strong>{activeRel || '未选择文件'}</strong>
            </span>
            <div className="btn-row">
              {isChapter ? <label className="editor-toolbar__field editor-toolbar__field--status">
                <span className="editor-toolbar__label">本章状态</span>
                <select
                  className="select"
                  value={detail?.status ?? ''}
                  disabled={!detail}
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
              {!rightOpen ? (
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  title="展开对话面板"
                  aria-label="展开对话面板"
                  onClick={() => setRightOpen(true)}
                >
                  对话 «
                </button>
              ) : null}
            </div>
          </div>

          <div className="editor__body">
            {!detail ? (
              <div className="empty-state">
                <p className="empty-state__title">请选择或新建一章</p>
                <p className="empty-state__desc">左栏「新建章节」即可开始写。</p>
              </div>
            ) : mode === 'edit' ? (
              <>
                <textarea
                  autoComplete={NO_AUTOFILL}
                  ref={textareaRef}
                  className="prose-editor"
                  value={text}
                  spellCheck={false}
                  onChange={(event) => {
                    setText(event.target.value)
                    setDirty(true)
                    setGhost('')
                  }}
                  onSelect={(event) => {
                    const target = event.target as HTMLTextAreaElement
                    setSelection(target.value.slice(target.selectionStart, target.selectionEnd))
                    selectionRangeRef.current = { start: target.selectionStart, end: target.selectionEnd }
                  }}
                  style={{ minHeight: '52vh' }}
                />
                {ghost ? (
                  <div className="ghost-chip">
                    {ghost}
                    <div className="row" style={{ marginTop: 'var(--space-2)' }}>
                      <button
                        className="btn btn--sm btn--primary"
                        type="button"
                        onClick={() => {
                          setText(text + ghost)
                          setDirty(true)
                          setGhost('')
                        }}
                      >
                        Tab 接受
                      </button>
                      <button className="btn btn--sm" type="button" onClick={() => setGhost('')}>
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
                    <button className="btn btn--ghost btn--sm" type="button" onClick={() => setSelection('')}>
                      取消选择
                    </button>
                  </div>
                ) : null}
              </>
            ) : (
              <div className="prose-reading">
                <MarkdownView
                  text={text}
                  milestone={{
                    step: milestone.step,
                    enabled: isChapter && milestone.enabled && (settings?.editor.milestone_inline ?? true),
                    budget,
                    decorate: mode === 'reading' || mode === 'preview',
                  }}
                />
              </div>
            )}
          </div>

          <div className="milestone-rail">
            <span>{isChapter ? '正文' : '文件'} {wordCount} 字</span>
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
        {rightOpen ? (
          <aside className="editor__col editor__col--right">
            <ChatPanel
              projectId={projectId}
              chapterRel={activeRel || null}
              targetChapterRel={targetChapterRel || null}
              selection={selection}
              hasUnsavedChanges={dirty}
              availableFiles={chatFiles}
              prepareContext={prepareChatContext}
              onFilesChanged={handleChatChanges}
              onOpenFile={openChatFile}
              onCollapse={() => setRightOpen(false)}
            />
          </aside>
        ) : null}
      </div>

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
          <label className="btn btn--ghost btn--sm" style={{ cursor: 'pointer' }}>
            追加导入片段为草稿章
            <input
              type="file"
              accept=".md,.txt"
              style={{ display: 'none' }}
              onChange={async (event) => {
                const file = event.target.files?.[0]
                event.target.value = ''
                if (!file) return
                try {
                  const content = await file.text()
                  const result = await importDocument(projectId, { filename: file.name, content })
                  notify(`已导入 ${result.count} 章`, 'success')
                  await loadTree()
                } catch (err) {
                  notify(errorMessage(err), 'error')
                }
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
                </tr>
              </thead>
              <tbody>
                {contextPreview.map((item) => (
                  <tr key={`${item.level}-${item.title}`}>
                    <td>{item.level}</td>
                    <td>{item.title}</td>
                    <td className="mono">{item.tokens}</td>
                    <td>{item.mandatory ? '是' : '否'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : null}
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
                try {
                  await resolveConflict(projectId, {
                    rel_path: detail.rel_path,
                    mode: 'take-external',
                  })
                  setConflict(null)
                  await loadChapter(detail.rel_path)
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
                try {
                  await resolveConflict(projectId, {
                    rel_path: detail.rel_path,
                    mode: 'keep-mine',
                    content: detail.content.replace(detail.body, text),
                  })
                  setConflict(null)
                  notify('已保留我的版本（外部版本已存快照）', 'success')
                  await loadChapter(detail.rel_path)
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

      <Modal title={filePreview?.path || '文件内容'} open={filePreview !== null} onClose={() => setFilePreview(null)} width={920}>
        <MarkdownView text={filePreview?.content || ''} />
      </Modal>

      {/* 快照 */}
      <Modal title="历史快照" open={snapshotOpen} onClose={() => setSnapshotOpen(false)}>
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
                    <button
                      className="btn btn--sm"
                      type="button"
                      onClick={async () => {
                        try {
                          await restoreSnapshot(projectId, item.id)
                          notify('已回滚到该快照（回滚前已再存一份）', 'success')
                          setSnapshotOpen(false)
                          if (activeRel) await loadChapter(activeRel)
                        } catch (err) {
                          notify(errorMessage(err), 'error')
                        }
                      }}
                    >
                      回滚到此版本
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Modal>
    </div>
  )
}
