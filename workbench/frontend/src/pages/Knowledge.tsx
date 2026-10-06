import { usePageChatContext } from '../state/usePageChatContext'
import '../styles/workspacePages.css'
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { askKnowledge, getBookVectorConfig, getKnowledge, getKnowledgeEvidence, getKnowledgeGraph, getKnowledgeEntities,
  prepareKnowledgeModel, saveBookVectorConfig, searchKnowledge, syncKnowledge,
  type KnowledgeEdge, type KnowledgeEvidence, type KnowledgeGraphData, type KnowledgeNode,
  type KnowledgeOverview, type KnowledgeProfile, type KnowledgeSearchResult } from '../api/knowledge'
import KnowledgeGraph, { KNOWLEDGE_KINDS, knowledgeKindLabel } from '../components/KnowledgeGraph'
import KnowledgeReviewPanel from '../components/KnowledgeReviewPanel'
import { visibleKnowledgeGraph } from '../lib/knowledgeGraphState'
import PanelResizer from '../components/PanelResizer'
import Modal from '../components/Modal'
import { evidenceEditorUrl, knowledgeDocumentGroup, knowledgeDocumentStatus, knowledgeGraphPresentation, knowledgeGraphScope, knowledgeNodeDocuments, knowledgeNodePreview, knowledgeNodeStatus,
  knowledgeSelectionEvidenceIds, knowledgeDefinitionEvidence, matchesKnowledgeBook, relatedKnowledgeNodes, sourceLabel, splitKnowledgeCitations } from '../lib/knowledgeState'
import { usePanelWidth } from '../state/usePanelWidth'
import { errorMessage } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'
import '../styles/knowledge.css'

const busyStatus = (status?: string) => ['pending', 'queued', 'running', 'building', 'downloading', 'rebuilding'].includes(status || '')
const jobLabel = (status?: string) => ({ ready: '索引就绪', done: '同步完成', completed: '同步完成',
  queued: '等待同步', pending: '等待同步', running: '正在同步', building: '正在建库',
  failed: '同步失败', error: '同步失败', empty: '尚未建库', idle: '空闲', stale: '需要同步',
  downloading: '正在准备模型', rebuilding: '正在重建', cancelled: '同步已停止' } as Record<string, string>)[status || ''] || '等待同步'
type BrowserScope = { document: string; local: boolean; kinds: string[]; chapter: string; center: string; content: boolean }

export default function Knowledge() {
  const [archivedEvidence, setArchivedEvidence] = useState<KnowledgeEvidence | null>(null)
  const projectId = Number(useParams<{ id: string }>().id)
  const navigate = useNavigate()
  const activeProject = useRef(projectId)
  activeProject.current = projectId
  const layout = useRef<HTMLDivElement>(null)
  const requests = useRef(new Set<AbortController>())
  const searchRequest = useRef<AbortController | null>(null)
  const detailRequest = useRef<AbortController | null>(null)
  const [leftWidth, setLeftWidth] = usePanelWidth('aiw.knowledge.leftWidth', 220, 190, 410)
  const [rightWidth, setRightWidth] = usePanelWidth('aiw.knowledge.rightWidth', 340, 300, 620)
  const [overview, setOverview] = useState<KnowledgeOverview | null>(null)
  const [graph, setGraph] = useState<KnowledgeGraphData | null>(null)
  const [committedGraphScope, setCommittedGraphScope] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [documentFilter, setDocumentFilter] = useState('')
  const [onlyDocument, setOnlyDocument] = useState(false)
  const [graphLoading, setGraphLoading] = useState(false)
  const [graphError, setGraphError] = useState('')
  const [graphRetry, setGraphRetry] = useState(0)
  const [modelPreparing, setModelPreparing] = useState(false)
  const [documentQuery, setDocumentQuery] = useState('')
  const [leftTab, setLeftTab] = useState<'documents' | 'entities'>('documents')
  const [rightTab, setRightTab] = useState<'material' | 'search' | 'review'>('material')
  const [mobileTab, setMobileTab] = useState<'documents' | 'graph' | 'details'>('graph')
  const [drawer, setDrawer] = useState<'documents' | 'details' | null>(null)
  const [leftCollapsed, setLeftCollapsed] = useState(false)
  const [rightCollapsed, setRightCollapsed] = useState(false)
  const [compact, setCompact] = useState(false)
  const [entityQuery, setEntityQuery] = useState('')
  const [entityKind, setEntityKind] = useState('')
  const [entityNodes, setEntityNodes] = useState<KnowledgeNode[]>([])
  const [entityCount, setEntityCount] = useState(0)
  const [entityOffset, setEntityOffset] = useState(0)
  const [entityLoading, setEntityLoading] = useState(false)
  const [entityError, setEntityError] = useState('')
  const [pendingCount, setPendingCount] = useState(0)
  const [focusCandidateId, setFocusCandidateId] = useState('')
  const [showContent, setShowContent] = useState(false)
  const [edgeKinds, setEdgeKinds] = useState(['structure', 'mention', 'planned', 'relationship'])
  const lastSuccessfulScope = useRef<BrowserScope | null>(null)
  const focusReturnScope = useRef<BrowserScope | null>(null)
  const [kinds, setKinds] = useState<string[]>([])
  const [chapterBefore, setChapterBefore] = useState('')
  const [center, setCenter] = useState('')
  const [graphQuery, setGraphQuery] = useState('')
  const [query, setQuery] = useState('')
  const [mode, setMode] = useState<'ai' | 'keyword'>('ai')
  const [profile, setProfile] = useState<KnowledgeProfile>('history')
  const [searching, setSearching] = useState(false)
  const [result, setResult] = useState<KnowledgeSearchResult | null>(null)
  const [answer, setAnswer] = useState('')
  const [searchError, setSearchError] = useState('')
  const [selectionState, setSelection] = useState<{ item: KnowledgeNode | KnowledgeEdge; kind: 'node' | 'edge';
    projectId: number; knowledgeKey: string } | null>(null)
  const [selectedEvidence, setSelectedEvidence] = useState<KnowledgeEvidence[]>([])
  const [evidenceCursor, setEvidenceCursor] = useState(0)
  const [evidenceLoading, setEvidenceLoading] = useState(false)
  const [evidenceError, setEvidenceError] = useState('')
  const [highlighted, setHighlighted] = useState<string[]>([])
  const [configOpen, setConfigOpen] = useState(false)
  const [bookMode, setBookMode] = useState('inherit')
  const [bookProvider, setBookProvider] = useState('')
  const [bookModel, setBookModel] = useState('')
  const [configBusy, setConfigBusy] = useState(false)
  const [configError, setConfigError] = useState('')
  const [notice, setNotice] = useState('')
  const current = overview?.project_id === projectId ? overview : null
  const graphScope = knowledgeGraphScope(current?.knowledge_key || '', documentFilter, onlyDocument, kinds, chapterBefore, center, showContent)
  const graphView = knowledgeGraphPresentation(graph, projectId, current?.knowledge_key, committedGraphScope, graphScope.key)
  const currentGraph = graphView.current
  const visibleGraph = useMemo(() => visibleKnowledgeGraph(graphView.rendered, edgeKinds, showContent), [graphView.rendered, edgeKinds, showContent])
  const graphBlocked = graphLoading || graphView.changingScope
  const displayedScope = graphBlocked && graphView.rendered && lastSuccessfulScope.current
    ? lastSuccessfulScope.current : { document: documentFilter, local: onlyDocument, center }
  const displayedDocument = current?.documents.find((doc) => doc.rel_path === displayedScope.document)
  const currentResult = result && current && matchesKnowledgeBook(result, projectId, current.knowledge_key) ? result : null
  const selection = selectionState?.projectId === projectId && selectionState.knowledgeKey === current?.knowledge_key ? selectionState : null
  const identity = useRef(current)
  identity.current = current
  useEffect(() => {
    if (!layout.current) return
    const observe = new ResizeObserver(([entry]) => {
      const reserved = (leftCollapsed ? 0 : leftWidth + 6) + (rightCollapsed ? 0 : rightWidth + 6)
      setCompact(window.innerWidth < 920 || entry.contentRect.width - reserved < 600)
    })
    observe.observe(layout.current)
    return () => observe.disconnect()
  }, [leftWidth, rightWidth, leftCollapsed, rightCollapsed])
  useEffect(() => {
    if (!drawer) return
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape') setDrawer(null) }
    window.addEventListener('keydown', close)
    return () => window.removeEventListener('keydown', close)
  }, [drawer])

  const controller = () => { const value = new AbortController(); requests.current.add(value); return value }
  const report = (err: unknown, signal: AbortSignal, setter = setError) => {
    if (!signal.aborted) setter(errorMessage(err))
  }
  const loadOverview = useCallback(async (signal: AbortSignal) => {
    const response = await getKnowledge(projectId, signal)
    if (!signal.aborted && activeProject.current === projectId && matchesKnowledgeBook(response, projectId)) {
      setOverview(response)
    } else if (!signal.aborted && activeProject.current === projectId) throw new Error('知识库响应不属于当前书籍，请重新加载。')
  }, [projectId])

  useEffect(() => {
    requests.current.forEach((request) => request.abort())
    requests.current.clear()
    setOverview(null); setGraph(null); setCommittedGraphScope(''); setResult(null); setAnswer(''); setSelection(null); setArchivedEvidence(null)
    setSelectedEvidence([]); setHighlighted([]); setError(''); setSearchError(''); setEvidenceError('')
    setDocumentFilter(''); setOnlyDocument(false); setDocumentQuery(''); setCenter(''); setKinds([]); setChapterBefore(''); setGraphQuery('')
    setQuery(''); setSearching(false); setConfigOpen(false); setNotice(''); setGraphError(''); setModelPreparing(false)
    setLeftTab('documents'); setRightTab('material'); setMobileTab('graph'); setDrawer(null); setEntityNodes([])
    setEntityCount(0); setEntityOffset(0); setEntityQuery(''); setEntityKind(''); setPendingCount(0); setFocusCandidateId(''); setShowContent(false)
    setEdgeKinds(['structure', 'mention', 'planned', 'relationship']); lastSuccessfulScope.current = null; focusReturnScope.current = null
    const request = controller()
    setLoading(true)
    void loadOverview(request.signal).catch((err) => report(err, request.signal))
      .finally(() => { if (!request.signal.aborted) setLoading(false) })
    return () => { requests.current.forEach((pending) => pending.abort()); requests.current.clear() }
  }, [projectId, loadOverview])

  const key = current?.knowledge_key
  useEffect(() => {
    searchRequest.current?.abort(); detailRequest.current?.abort()
    setResult(null); setAnswer(''); setSelection(null); setSelectedEvidence([]); setHighlighted([]); setArchivedEvidence(null)
    setSearching(false); setEvidenceLoading(false); setSearchError(''); setEvidenceError('')
  }, [key])
  const graphDocument = graphScope.document
  const selectedDocument = current?.documents.find((doc) => doc.rel_path === documentFilter)
  const graphMatches = graphQuery.trim() && currentGraph
    ? currentGraph.nodes.filter((node) => node.label.toLocaleLowerCase().includes(graphQuery.trim().toLocaleLowerCase()))
    : []
  const relatedNodes = useMemo(() => relatedKnowledgeNodes(currentGraph, documentFilter,
    currentResult ? currentResult.hits.flatMap((hit) => hit.node_ids || []) : undefined), [currentGraph, documentFilter, currentResult])
  const documentNodes = useMemo(() => documentFilter ? relatedKnowledgeNodes(currentGraph, documentFilter) : [], [currentGraph, documentFilter])
  const selectionNode = selection?.kind === 'node' ? selection.item as KnowledgeNode : null
  const selectionEdges = selectionNode ? currentGraph?.edges.filter((edge) => edge.source === selectionNode.id || edge.target === selectionNode.id) || [] : []
  const selectionEvidenceIds = selection ? knowledgeSelectionEvidenceIds(selection.item) : []
  const relatedEntityNodes = relatedNodes.filter((node) => !['document', 'chapter', 'content'].includes(node.kind))
  const visibleRelatedNodes = relatedEntityNodes.length ? relatedEntityNodes : relatedNodes
  const highlightedEvidence = selection?.item.evidence_ids || currentResult?.hits.map((hit) => hit.evidence_id) || []

  usePageChatContext({ projectId, activeFile: selectedDocument?.rel_path || selectedEvidence[0]?.rel_path, files: current?.documents.map(doc => doc.rel_path) || [], onFilesChanged: async () => { const request = controller(); try { await loadOverview(request.signal); setGraphRetry(value => value + 1) } finally { requests.current.delete(request) } } })

  const chooseDocument = (path: string) => { focusReturnScope.current = null; setDocumentFilter(path); setCenter(''); setOnlyDocument(Boolean(path)); setRightTab('material'); setMobileTab('graph'); setDrawer(null) }
  const applyScope = (previous: BrowserScope | null) => {
    if (!previous) return
    setDocumentFilter(previous.document); setOnlyDocument(previous.local); setKinds(previous.kinds)
    setChapterBefore(previous.chapter); setCenter(previous.center); setShowContent(previous.content); setGraphError('')
  }
  const restoreScope = () => applyScope(lastSuccessfulScope.current)
  const focusNode = (nodeId: string) => {
    if (!center) focusReturnScope.current = { document: documentFilter, local: onlyDocument, kinds, chapter: chapterBefore, center, content: showContent }
    setDocumentFilter(''); setOnlyDocument(false); setCenter(nodeId); setRightTab('material'); setMobileTab('graph'); setDrawer(null)
  }
  useEffect(() => { setEntityOffset(0); setEntityNodes([]) }, [entityQuery, entityKind, key])
  useEffect(() => {
    if (!key) return
    const request = controller(); setEntityLoading(true); setEntityError('')
    const timer = window.setTimeout(() => {
      void getKnowledgeEntities(projectId, { q: entityQuery, kind: entityKind, offset: entityOffset, limit: 100 }, request.signal)
        .then((response) => {
          if (!request.signal.aborted && activeProject.current === projectId && matchesKnowledgeBook(response, projectId, key)) {
            setEntityNodes((previous) => entityOffset ? [...new Map([...previous, ...response.nodes].map((node) => [node.id, node])).values()] : response.nodes)
            setEntityCount(response.count)
          }
        }).catch((err) => report(err, request.signal, setEntityError))
        .finally(() => { if (!request.signal.aborted) setEntityLoading(false) })
    }, 180)
    return () => { window.clearTimeout(timer); request.abort(); requests.current.delete(request) }
  }, [projectId, key, entityQuery, entityKind, entityOffset, current?.graph_updated_at])

  useEffect(() => {
    searchRequest.current?.abort()
    detailRequest.current?.abort()
    setSearching(false); setResult(null); setAnswer(''); setSelection(null)
    setSelectedEvidence([]); setHighlighted([]); setSearchError(''); setEvidenceError('')
  }, [projectId, documentFilter, chapterBefore, profile, mode])
  useEffect(() => {
    if (!key) return
    const request = controller()
    setGraphLoading(true); setGraphError('')
    void getKnowledgeGraph(projectId, { document: graphDocument || undefined, kinds,
      chapter_before: Number(chapterBefore) || undefined, center: center || undefined, include_content: showContent, limit: 300 }, request.signal)
      .then((response) => {
        if (!request.signal.aborted && activeProject.current === projectId && matchesKnowledgeBook(response, projectId, key)) {
          setGraph((previous) => previous && JSON.stringify(previous) === JSON.stringify(response) ? previous : response)
          setCommittedGraphScope(graphScope.key)
          lastSuccessfulScope.current = { document: documentFilter, local: onlyDocument, kinds, chapter: chapterBefore, center, content: showContent }
        } else if (!request.signal.aborted && activeProject.current === projectId) setGraphError('图谱响应不属于当前书籍，请重新加载。')
      }).catch((err) => report(err, request.signal, setGraphError))
      .finally(() => { if (!request.signal.aborted) setGraphLoading(false) })
    return () => { request.abort(); requests.current.delete(request) }
  }, [projectId, key, graphDocument, graphScope.key, kinds, chapterBefore, center, showContent, current?.graph_updated_at, current?.graph_version, graphRetry])

  useEffect(() => {
    if (!key) return
    const request = controller()
    let reading = false
    const timer = window.setInterval(() => {
      if (reading || document.visibilityState !== 'visible') return
      reading = true
      void loadOverview(request.signal).catch((err) => report(err, request.signal)).finally(() => { reading = false })
    }, busyStatus(current?.job?.status) || modelPreparing ? 1800 : 5000)
    return () => { window.clearInterval(timer); request.abort(); requests.current.delete(request) }
  }, [key, current?.job?.status, modelPreparing, loadOverview])

  useEffect(() => { if (modelPreparing && current?.model.ready) setModelPreparing(false) }, [modelPreparing, current?.model.ready])

  const groups = useMemo(() => {
    const map = new Map<string, KnowledgeOverview['documents']>()
    for (const doc of current?.documents || []) {
      if (documentQuery && !`${doc.title} ${doc.rel_path}`.toLocaleLowerCase().includes(documentQuery.trim().toLocaleLowerCase())) continue
      const name = knowledgeDocumentGroup(doc)
      map.set(name, [...(map.get(name) || []), doc])
    }
    const order = ['章节正文', '设定资料', '状态与追踪', '大纲与计划', '拆书事实卡']
    return [...map.entries()].sort(([a], [b]) => (order.includes(a) ? order.indexOf(a) : order.length)
      - (order.includes(b) ? order.indexOf(b) : order.length))
  }, [current?.documents, documentQuery])

  const sync = async (force = false) => {
    const request = controller()
    setError(''); setNotice('')
    try {
      const response = await syncKnowledge(projectId, force, request.signal)
      if (!request.signal.aborted && activeProject.current === projectId && matchesKnowledgeBook(response, projectId, key)) setOverview(response)
    } catch (err) { report(err, request.signal) }
    finally { requests.current.delete(request) }
  }

  const showSelection = async (item: KnowledgeNode | KnowledgeEdge, kind: 'node' | 'edge', offset = 0) => {
    detailRequest.current?.abort()
    if (item.candidate_id && item.review_status !== 'approved') {
      setHighlighted(kind === 'node' ? [item.id] : [(item as KnowledgeEdge).source, (item as KnowledgeEdge).target])
      setFocusCandidateId(item.candidate_id); setRightTab('review'); setMobileTab('details'); setDrawer('details'); return
    }
    const request = controller(); detailRequest.current = request
    setSelection({ item, kind, projectId, knowledgeKey: key || '' }); setRightTab('material'); setMobileTab('details'); setDrawer('details')
    if (!offset) { setSelectedEvidence([]); setEvidenceCursor(0) }
    setEvidenceError(''); setEvidenceLoading(true)
    setHighlighted(kind === 'node' ? [item.id] : [(item as KnowledgeEdge).source, (item as KnowledgeEdge).target])
    try {
      const ids = knowledgeSelectionEvidenceIds(item).slice(offset, offset + 6)
      const values = await Promise.allSettled(ids.map((evidenceId) => getKnowledgeEvidence(projectId, evidenceId, request.signal)))
      if (!request.signal.aborted && activeProject.current === projectId && identity.current?.knowledge_key === key) {
        const valid = values.flatMap((value) => value.status === 'fulfilled'
          ? [kind === 'node' ? knowledgeDefinitionEvidence(value.value, item as KnowledgeNode) : value.value] : [])
        setSelectedEvidence((previous) => offset ? [...previous, ...valid] : valid)
        setEvidenceCursor(offset + ids.length)
        const failure = values.find((value) => value.status === 'rejected')
        if (failure?.status === 'rejected') setEvidenceError(`${values.length - valid.length} 条依据暂时无法读取：${errorMessage(failure.reason)}`)
      }
    } catch (err) { report(err, request.signal, setEvidenceError) }
    finally { if (!request.signal.aborted) setEvidenceLoading(false); requests.current.delete(request) }
  }

  const openEvidence = async (evidence: Pick<KnowledgeEvidence, 'evidence_id'> & Partial<KnowledgeEvidence>) => {
    const request = controller()
    try {
      const full = await getKnowledgeEvidence(projectId, evidence.evidence_id, request.signal)
      if (!request.signal.aborted && activeProject.current === projectId && identity.current?.knowledge_key === key) {
        if (evidence.document_hash && evidence.document_hash !== full.document_hash) throw new Error('原文版本已更新，请刷新图谱后重新定位。')
        if (full.archived) { setArchivedEvidence(full); return }
        const location = evidence.document_hash && evidence.rel_path === full.rel_path ? evidence : full
        navigate(evidenceEditorUrl(projectId, { ...full, line_start: location.line_start, line_end: location.line_end }))
      }
    } catch (err) { report(err, request.signal, setEvidenceError) }
    finally { requests.current.delete(request) }
  }

  const closeSelection = () => {
    detailRequest.current?.abort(); setSelection(null); setSelectedEvidence([]); setEvidenceError('')
    setHighlighted(currentResult?.hits.flatMap((hit) => hit.node_ids || []) || [])
  }

  useEffect(() => {
    if (!selection || !currentGraph) return
    const updated = selection.kind === 'node' ? currentGraph.nodes.find((node) => node.id === selection.item.id)
      : currentGraph.edges.find((edge) => edge.id === selection.item.id)
    if (!updated) { closeSelection(); return }
    if (JSON.stringify(updated) !== JSON.stringify(selection.item)) void showSelection(updated, selection.kind)
  }, [currentGraph])
  useEffect(() => {
    if (!center || !currentGraph || selection?.item.id === center) return
    const node = currentGraph.nodes.find((item) => item.id === center)
    if (node) void showSelection(node, 'node')
  }, [currentGraph, center])

  const search = async (event: FormEvent) => {
    event.preventDefault()
    if (!query.trim() || !key) return
    searchRequest.current?.abort()
    const request = controller(); searchRequest.current = request
    setSearching(true); setSearchError(''); setResult(null); setAnswer(''); setSelection(null); setHighlighted([])
    const input = { query: query.trim(), mode: mode === 'keyword' ? 'keyword' as const : 'hybrid' as const,
      profile, document: documentFilter || undefined, limit: 6,
      chapter_rel: Number(chapterBefore) > 0
        ? current?.documents.find((doc) => doc.chapter_number === Number(chapterBefore))?.rel_path
          || `章节/第${String(Number(chapterBefore)).padStart(4, '0')}章.txt`
        : undefined }
    let verified = false
    try {
      if (mode === 'keyword') {
        const response = await searchKnowledge(projectId, input, request.signal)
        if (!request.signal.aborted && activeProject.current === projectId && matchesKnowledgeBook(response, projectId, key)) {
          setResult(response); setHighlighted(response.hits.flatMap((hit) => hit.node_ids || []))
        }
      } else {
        await askKnowledge(projectId, input, (payload) => {
          if (request.signal.aborted || activeProject.current !== projectId || identity.current?.knowledge_key !== key) return
          if (payload.type === 'evidence') {
            verified = matchesKnowledgeBook(payload, projectId, key)
            if (verified) { setResult(payload); setHighlighted(payload.hits.flatMap((hit) => hit.node_ids || [])) }
          } else if (payload.type === 'error') {
            setSearchError(payload.message)
            if (payload.invalidate_answer) setAnswer('')
          }
          else if (verified && payload.type === 'delta') setAnswer((text) => text + payload.text)
        }, request.signal)
      }
    } catch (err) { report(err, request.signal, setSearchError) }
    finally { if (!request.signal.aborted) setSearching(false); requests.current.delete(request) }
  }

  const openConfig = async () => {
    setConfigOpen(true); setConfigError(''); setConfigBusy(true)
    const request = controller()
    try {
      const response = await getBookVectorConfig(projectId, request.signal)
      if (!request.signal.aborted && activeProject.current === projectId) {
        setBookMode(response.inherited ? 'inherit' : String(response.override?.mode || response.config?.mode || 'inherit'))
        setBookProvider(String(response.override?.provider || response.config?.provider || ''))
        setBookModel(String(response.override?.model || response.config?.model || ''))
      }
    } catch (err) { report(err, request.signal, setConfigError) }
    finally { if (!request.signal.aborted) setConfigBusy(false); requests.current.delete(request) }
  }
  const saveConfig = async () => {
    const request = controller(); setConfigBusy(true); setConfigError('')
    try {
      await saveBookVectorConfig(projectId, { mode: bookMode,
        provider: bookMode === 'cloud' ? bookProvider.trim() : '',
        model: bookMode === 'cloud' ? bookModel.trim() : '' }, request.signal)
      await loadOverview(request.signal)
      if (!request.signal.aborted) { setConfigOpen(false); setNotice('已更新本书检索设置，索引将在后台更新。') }
    } catch (err) { report(err, request.signal, setConfigError) }
    finally { if (!request.signal.aborted) setConfigBusy(false); requests.current.delete(request) }
  }

  const evidenceCard = (hit: KnowledgeEvidence, index?: number) => (
    <article className="knowledge-evidence" key={hit.evidence_id} id={index === undefined ? undefined : `knowledge-citation-${index + 1}`}>
      <div className="knowledge-evidence__meta"><span>{index === undefined ? '依据' : `[${index + 1}]`} · {sourceLabel(hit.source)}{hit.source_tier ? ` · ${hit.source_tier === 'author' ? '本书原文' : sourceLabel(hit.source_tier)}` : ''}</span>
        {hit.score !== undefined && <span title="相关性只表示检索排序，不代表事实置信度">相关性 {hit.score.toFixed(3)}</span>}</div>
      <button className="knowledge-evidence__path" onClick={() => void openEvidence(hit)}>{hit.rel_path} · L{hit.line_start}–{hit.line_end} ↗</button>
      <p>{hit.text}</p>
      <div className="btn-row"><button className="btn btn--sm" onClick={() => void openEvidence(hit)}>打开原文</button>
        {Boolean(hit.node_ids?.length) && <button className="btn btn--ghost btn--sm" onClick={() => setHighlighted(hit.node_ids || [])}>定位图谱</button>}</div>
    </article>
  )

  return (
    <section className={`knowledge-page workspace-knowledge ${compact ? 'is-compact' : ''}`}>
      <Modal open={Boolean(archivedEvidence)} onClose={() => setArchivedEvidence(null)} title="审核时的原文依据" footer={<><button className="btn btn--sm" onClick={() => setArchivedEvidence(null)}>关闭</button><button className="btn btn--primary btn--sm" onClick={() => { if (archivedEvidence) navigate(`/project/${projectId}/editor?${new URLSearchParams({ path: archivedEvidence.rel_path })}`); setArchivedEvidence(null) }}>打开当前资料</button></>}>
        {archivedEvidence && <><p>这是本次审核写入前保留的原文快照，提交凭据已核验。当前资料已更新，下面的引句和行号对应审核时的版本。</p><p className="muted">{archivedEvidence.rel_path} · L{archivedEvidence.line_start}–{archivedEvidence.line_end}</p><blockquote className="knowledge-archive-quote">{archivedEvidence.text}</blockquote></>}
      </Modal>
      <header className="knowledge-page__header">
        <div><h1>知识库与图谱 <span className="muted">{current?.project_name || `本书 #${projectId}`}</span></h1>
          <div className="knowledge-counts"><span>▤ {current?.counts.indexed_documents ?? current?.counts.documents ?? 0} / {current?.counts.eligible_documents ?? current?.counts.documents ?? 0} 已索引 / 可索引文档</span><span>● {current?.counts.nodes || 0} 节点</span><span>⌁ {current?.counts.edges || 0} 关系</span><span>{current?.counts.chunks || 0} 检索片段</span></div></div>
        <div className="knowledge-page__actions"><button className="btn btn--sm knowledge-model" onClick={() => void openConfig()}>
          索引状态 · {jobLabel(current?.job?.status)}</button>
          <button className="btn btn--sm knowledge-desktop-toggle" aria-pressed={!leftCollapsed} onClick={() => setLeftCollapsed((value) => !value)}>{leftCollapsed ? '展开目录' : '收起目录'}</button>
          <button className="btn btn--sm knowledge-desktop-toggle" aria-pressed={!rightCollapsed} onClick={() => setRightCollapsed((value) => !value)}>{rightCollapsed ? '展开详情' : '收起详情'}</button>
          <button className="btn btn--sm knowledge-drawer-toggle" onClick={() => setDrawer('documents')}>资料与实体</button>
          <button className="btn btn--sm knowledge-drawer-toggle" onClick={() => setDrawer('details')}>详情与待审</button>
          <button className="btn btn--sm btn--primary" disabled={!current || busyStatus(current.job?.status)} onClick={() => void sync()}>同步本书资料</button></div>
      </header>
      {loading && <div className="knowledge-notice" role="status">正在读取本书知识库…</div>}
      {error && <div className="knowledge-notice knowledge-notice--error" role="alert">{error}<button className="btn btn--sm" onClick={() => void sync()}>重试同步</button></div>}
      {notice && <div className="knowledge-notice" role="status">{notice}<button className="btn btn--ghost btn--sm" onClick={() => setNotice('')}>关闭</button></div>}
      {current?.job?.error && current.job.error !== current.degraded_reason && <div className="knowledge-notice knowledge-notice--error">{current.job.error}</div>}
      {busyStatus(current?.job?.status) && <progress className="knowledge-progress" max={1} value={current?.job?.progress || 0} aria-label="本书索引同步进度" />}
      <nav className="knowledge-mobile-tabs" aria-label="知识库浏览区域">{[['documents', '资料与实体'], ['graph', '图谱'], ['details', '详情与待审']].map(([tab, label]) => <button key={tab} aria-pressed={mobileTab === tab} onClick={() => { setMobileTab(tab as typeof mobileTab); setDrawer(null) }}>{label}</button>)}</nav>
      <div ref={layout} className={`knowledge-workspace ${compact ? 'is-compact' : ''} ${leftCollapsed ? 'is-left-collapsed' : ''} ${rightCollapsed ? 'is-right-collapsed' : ''} ${drawer ? `is-${drawer}-open` : ''}`} data-mobile-tab={mobileTab} style={{ '--knowledge-left': `${leftWidth}px`, '--knowledge-right': `${rightWidth}px` } as CSSProperties}>
        {drawer && <button className="knowledge-drawer-backdrop" aria-label="关闭侧栏" onClick={() => setDrawer(null)} />}
        <aside className="knowledge-documents"><div className="knowledge-pane-heading"><h2>本书目录</h2><button className="btn btn--ghost btn--sm knowledge-drawer-close" onClick={() => setDrawer(null)}>关闭</button></div>
          <div className="knowledge-pane-tabs" role="tablist" aria-label="目录类型"><button role="tab" aria-selected={leftTab === 'documents'} onClick={() => setLeftTab('documents')}>资料</button><button role="tab" aria-selected={leftTab === 'entities'} onClick={() => setLeftTab('entities')}>实体</button></div>
          {leftTab === 'documents' ? <><input className="input" autoComplete={NO_AUTOFILL} placeholder="筛选本书文件" aria-label="筛选本书文件" value={documentQuery} onChange={(event) => setDocumentQuery(event.target.value)} />
          <button className={`knowledge-document ${!documentFilter ? 'is-active' : ''}`} onClick={() => chooseDocument('')}>全部本书资料 <span>{current?.counts.eligible_documents ?? current?.counts.documents ?? 0} 可索引</span></button>
          <div className="knowledge-documents__list">{groups.map(([group, docs]) => <details key={group} open><summary>{group} <span>({docs.length})</span></summary>
            {docs.map((doc) => <button key={doc.rel_path} className={`knowledge-document ${documentFilter === doc.rel_path ? 'is-active' : ''}`} title={`${doc.rel_path} · ${knowledgeDocumentStatus(doc)}`} onClick={() => chooseDocument(doc.rel_path)} aria-pressed={documentFilter === doc.rel_path}>
              <strong>{doc.title || doc.rel_path.split('/').slice(-1)[0]}</strong><span className="knowledge-document__badge">{doc.index_status === 'excluded' ? '草稿' : doc.index_status === 'indexed' ? '已索引' : doc.index_status === 'stale' ? '待更新' : '待同步'}</span>
              </button>)}
          </details>)}{!groups.length && !loading && <p className="knowledge-documents__empty muted">{documentQuery ? '未找到匹配文件，试试文件名或路径。' : '暂无本书资料。保存章节或设定后，再同步知识库。'}</p>}</div>
          </> : <><input className="input" autoComplete={NO_AUTOFILL} aria-label="筛选本书实体" placeholder="搜索人物、物品或其他实体" value={entityQuery} onChange={(event) => setEntityQuery(event.target.value)} /><select className="select" aria-label="实体类型" value={entityKind} onChange={(event) => setEntityKind(event.target.value)}><option value="">全部实体类型</option>{KNOWLEDGE_KINDS.filter((kind) => !['document', 'chapter', 'content'].includes(kind.kind)).map((kind) => <option key={kind.kind} value={kind.kind}>{kind.label}</option>)}</select>
            <div className="knowledge-documents__list">{entityNodes.map((node) => <button key={node.id} className={`knowledge-document knowledge-entity ${center === node.id ? 'is-active' : ''}`} onClick={() => focusNode(node.id)}><strong>{node.label}</strong><span>{knowledgeKindLabel(node.kind)} · {node.planned || node.lifecycle === 'draft' ? knowledgeNodeStatus(node) : sourceLabel(node.source, node.review_status)}</span><small title={node.document}>{node.document}</small></button>)}
              {entityLoading && <p className="muted" role="status">正在读取本书实体…</p>}{entityError && <p className="knowledge-notice knowledge-notice--error" role="alert">{entityError}</p>}
              {!entityLoading && !entityNodes.length && <p className="knowledge-documents__empty muted">{entityQuery || entityKind ? '没有匹配实体，试试其他名称或类型。' : '暂无实体。同步资料或审核分析候选后查看。'}</p>}
              {entityNodes.length < entityCount && <button className="btn btn--sm" disabled={entityLoading} onClick={() => setEntityOffset(entityNodes.length)}>加载更多实体（{entityNodes.length}/{entityCount}）</button>}</div><footer>本书实体 · 共 {entityCount} 项</footer></>}
        </aside>
        <PanelResizer value={leftWidth} min={190} max={410} side="left" label="调整知识库文件栏宽度" onChange={setLeftWidth} onPreview={(next) => layout.current?.style.setProperty('--knowledge-left', `${next}px`)} />
        <div className="knowledge-center"><div className="knowledge-filters"><label><span>节点类型</span><select className="select" value={kinds[0] || ''} onChange={(event) => setKinds(event.target.value ? [event.target.value] : [])}><option value="">全部类型</option>{KNOWLEDGE_KINDS.map((kind) => <option key={kind.kind} value={kind.kind}>{kind.label}</option>)}</select></label>
          <label><span>早于章节</span><input className="input" type="number" min={1} value={chapterBefore} placeholder="不限制" onChange={(event) => setChapterBefore(event.target.value)} /></label>
          <input className="input knowledge-filters__search" aria-label="搜索图谱节点" placeholder="搜索图谱节点" value={graphQuery} onChange={(event) => setGraphQuery(event.target.value)} />
          {graphQuery && <span className="knowledge-filters__matches">{graphMatches.length} 个匹配</span>}
          {(documentFilter || kinds.length || chapterBefore || center || graphQuery) && <button className="btn btn--ghost btn--sm" onClick={() => {
            chooseDocument(''); setKinds([]); setChapterBefore(''); setGraphQuery(''); setHighlighted([])
          }}>清除筛选</button>}
          </div>
          <div className="knowledge-relation-filters" aria-label="图谱展示内容">{[['structure', '包含 / 定义'], ['mention', '原文提及'], ['relationship', '明确关系'], ['planned', '计划关系']].map(([kind, label]) => <label key={kind}><input type="checkbox" checked={edgeKinds.includes(kind)} onChange={(event) => setEdgeKinds((values) => event.target.checked ? [...values, kind] : values.filter((value) => value !== kind))} />{label}</label>)}<label><input type="checkbox" checked={showContent} onChange={(event) => setShowContent(event.target.checked)} />内容片段</label></div>
          <div className="knowledge-scope" aria-live="polite"><span>{displayedScope.center ? '关联节点局部视图' : displayedScope.local ? '当前文件局部视图' : '本书全景'}{displayedDocument && <strong title={displayedDocument.rel_path}> · {displayedDocument.title || displayedDocument.rel_path}</strong>}{graphBlocked && graphView.rendered && <small>（保留的上一成功范围）</small>}</span>
            {selectedDocument && !onlyDocument && !center && <span className="muted">{graphError ? '范围读取失败，可重试或返回' : graphBlocked ? '正在更新图谱范围…' : documentNodes.length ? '已高亮文件相关节点'
              : currentGraph?.truncated ? '本次全景未包含此文件，可载入文件局部' : '当前图谱没有此文件的相关节点'}</span>}
            <div className="btn-row">{selectedDocument && !onlyDocument && !center && <button className="btn btn--sm" onClick={() => setOnlyDocument(true)}>只看此文件</button>}
              {center && focusReturnScope.current && <button className="btn btn--sm" onClick={() => { applyScope(focusReturnScope.current); focusReturnScope.current = null }}>返回聚焦前范围</button>}{(onlyDocument || center) && <button className="btn btn--sm" onClick={() => { focusReturnScope.current = null; setOnlyDocument(false); setCenter('') }}>返回本书全景</button>}</div></div>
          <div className="knowledge-center__graph" aria-busy={graphLoading}><div className="knowledge-center__canvas" aria-hidden={graphBlocked || undefined} ref={(node) => { if (node) node.inert = graphBlocked }}><KnowledgeGraph data={visibleGraph} scopeKey={JSON.stringify([committedGraphScope, [...edgeKinds].sort()])}
            highlighted={graphQuery ? graphMatches.map((node) => node.id) : highlighted}
            highlightedDocument={graphQuery ? undefined : documentFilter || undefined} highlightedEvidence={highlightedEvidence}
            emptyMessage={graphLoading || loading ? '正在加载本书图谱…' : graphError ? '图谱暂时无法加载，请重试。' : selectedDocument?.index_status === 'excluded' && onlyDocument
              ? '草稿未纳入历史图谱，可在右侧查询这份草稿原文。' : undefined}
            onSelect={(item, kind) => { if (!graphBlocked) void showSelection(item, kind) }} /></div>
            {graphBlocked && <div className="knowledge-graph-status" role={graphError ? 'alert' : 'status'}>{graphError ? <><span>当前范围加载失败：{graphError}</span><button className="btn btn--sm" onClick={() => setGraphRetry((count) => count + 1)}>重新加载图谱</button>{lastSuccessfulScope.current && <button className="btn btn--sm" onClick={restoreScope}>返回上一成功范围</button>}</> : <><span>正在更新当前范围…</span>{Boolean(graphView.rendered?.nodes.length) && <small>保留上一视图的位置，载入完成后继续浏览。</small>}</>}</div>}
            {graphError && !graphBlocked && <div className="knowledge-graph-status knowledge-notice--error" role="alert">{graphError}<button className="btn btn--sm" onClick={() => setGraphRetry((count) => count + 1)}>重新加载图谱</button></div>}</div>
          <div className="knowledge-center__status"><span>{graphBlocked ? graphError ? '当前范围加载失败' : '正在加载当前范围…' : currentGraph?.truncated ? '图谱最多显示 300 个节点；完整实体仍可从左侧目录浏览。' : `${visibleGraph?.nodes.length || 0} 个可见节点 · ${visibleGraph?.edges.length || 0} 条关系`}</span><span>点击节点查看原文依据</span></div>
        </div>
        <PanelResizer value={rightWidth} min={300} max={620} side="right" label="调整知识检索栏宽度" onChange={setRightWidth} onPreview={(next) => layout.current?.style.setProperty('--knowledge-right', `${next}px`)} />
        <aside className="knowledge-search"><div className="knowledge-pane-heading"><h2>本书知识</h2><button className="btn btn--ghost btn--sm knowledge-drawer-close" onClick={() => setDrawer(null)}>关闭</button></div><div className="knowledge-pane-tabs" role="tablist" aria-label="知识侧栏"><button role="tab" aria-selected={rightTab === 'material'} onClick={() => setRightTab('material')}>资料</button><button role="tab" aria-selected={rightTab === 'search'} onClick={() => setRightTab('search')}>检索</button><button role="tab" aria-selected={rightTab === 'review'} onClick={() => setRightTab('review')}>待审{pendingCount > 0 && <span className="knowledge-tab-count">{pendingCount}</span>}</button></div>
          {rightTab === 'search' && <><div className="knowledge-search__tabs" role="tablist" aria-label="检索方式"><button role="tab" aria-selected={mode === 'ai'} onClick={() => setMode('ai')}>AI 检索</button><button role="tab" aria-selected={mode === 'keyword'} onClick={() => setMode('keyword')}>关键词检索</button></div>
          <form className="knowledge-search__form" onSubmit={(event) => void search(event)}><label className="field"><span className="field__label">检索场景</span><select className="select" value={profile} onChange={(event) => setProfile(event.target.value as KnowledgeProfile)}><option value="history">回顾历史 / 连续性</option><option value="planning">剧情规划</option><option value="review">审稿查证</option><option value="teardown">拆书参考</option></select></label>
            <p className="knowledge-search__scope">检索范围：{selectedDocument ? selectedDocument.title || selectedDocument.rel_path : '本书可用资料'}{chapterBefore ? ` · 第 ${chapterBefore} 章之前` : ''}</p>
            {selectedDocument?.index_status === 'excluded' && <p className="knowledge-draft-note">已选定草稿：仅用于本次检索，不会纳入历史索引或自动写作召回。</p>}
            {selectedDocument?.index_status === 'stale' && <p className="knowledge-draft-note">此文件索引已过期；检索会回读当前原文，并使用关键词召回。</p>}
            <div className="knowledge-search__input"><input className="input" autoComplete={NO_AUTOFILL} aria-label="本书知识检索问题" placeholder={mode === 'ai' ? '提问，AI 将检索本书资料并回答…' : '输入人物、物品或原文关键词…'} value={query} onChange={(event) => setQuery(event.target.value)} /><button className="btn btn--primary" disabled={!query.trim() || !current || searching}>检索</button></div>
            {searching && <button type="button" className="btn btn--ghost btn--sm" onClick={() => { searchRequest.current?.abort(); setSearching(false) }}>停止回答</button>}</form></>}
          <div className="knowledge-search__results">
            {rightTab === 'search' && searchError && <p className="knowledge-notice knowledge-notice--error" role="alert">{searchError}</p>}
            {evidenceError && <p className="knowledge-notice knowledge-notice--error" role="alert">依据读取失败：{evidenceError}</p>}
            {rightTab === 'material' && selection && current && <section className="knowledge-selection"><button className="btn btn--ghost btn--sm knowledge-selection__back" onClick={closeSelection}>← 返回相关节点</button><div className="knowledge-selection__heading"><h2>{selection.kind === 'node' ? (selection.item as KnowledgeNode).label : (selection.item as KnowledgeEdge).relation}</h2></div>
              <p className="muted">{selection.kind === 'node' ? `${knowledgeKindLabel((selection.item as KnowledgeNode).kind)} · ${sourceLabel((selection.item as KnowledgeNode).source, selection.item.review_status)}` : sourceLabel((selection.item as KnowledgeEdge).origin, selection.item.review_status)}</p>
              {selection.item.lifecycle && ['draft', 'planned', 'setting_fact', 'historical_event'].includes(selection.item.lifecycle) && <p className="muted">事实状态：{({ draft: '草稿 · 待定', planned: '计划 · 尚未发生', setting_fact: '设定事实', historical_event: '已发生事件' } as Record<string, string>)[selection.item.lifecycle]}{(selection.item.chapter_start || selection.item.chapter_end) && ` · 适用第 ${selection.item.chapter_start || 1}${selection.item.chapter_end && selection.item.chapter_end !== selection.item.chapter_start ? `–${selection.item.chapter_end}` : selection.item.chapter_end ? '' : ' 章起'}${selection.item.chapter_end ? ' 章' : ''}`}</p>}
              {(selectionNode?.planned || ['draft', 'planned'].includes(selection.item.lifecycle || '')) && <p className="knowledge-planned-note">{selection.item.lifecycle === 'draft' ? '草稿 · 待定' : '计划 · 尚未发生'}；不能作为已发生的剧情事实。</p>}
              {selectionNode && (selectionNode.description || selectionNode.preview) && <p className="knowledge-selection__description">{selectionNode.description || selectionNode.preview}</p>}
              {selection.kind === 'edge' && <p>{currentGraph?.nodes.find((node) => node.id === (selection.item as KnowledgeEdge).source)?.label} → {currentGraph?.nodes.find((node) => node.id === (selection.item as KnowledgeEdge).target)?.label}</p>}
              {selectionNode && <><div className="btn-row"><button className="btn btn--sm" onClick={() => focusNode(selectionNode.id)}>聚焦一跳关系</button>
                {selectionNode.source_location && <button className="btn btn--sm" onClick={() => void openEvidence(selectionNode.source_location!)}>编辑原资料</button>}<button className="btn btn--ghost btn--sm" onClick={() => navigate(`/project/${projectId}/cards`)}>设定卡片</button>{Boolean(selectionNode.candidate_ids?.length) && <button className="btn btn--sm" onClick={() => { setFocusCandidateId(selectionNode.candidate_ids![0]); setRightTab('review') }}>关联待审（{selectionNode.candidate_ids!.length}）</button>}</div>
                <h3>关联材料（{knowledgeNodeDocuments(selectionNode).length}）</h3>{knowledgeNodeDocuments(selectionNode).map((path) => <button key={path} className="knowledge-related-edge" onClick={() => chooseDocument(path)}>{path}</button>)}
                <h3>关联关系（{selectionEdges.length}）</h3>{selectionEdges.length
                  ? selectionEdges.map((edge) => <button key={edge.id} className="knowledge-related-edge" onClick={() => void showSelection(edge, 'edge')}>
                    {currentGraph?.nodes.find((node) => node.id === edge.source)?.label || '未知'} → {currentGraph?.nodes.find((node) => node.id === edge.target)?.label || '未知'} · {edge.relation} · {sourceLabel(edge.origin, edge.review_status)}
                  </button>) : <p className="muted">当前视图中没有关联关系，可展开一跳或调整筛选。</p>}</>}
              <h3>来源依据（{selectedEvidence.length} / {selectionEvidenceIds.length}）</h3>{selectedEvidence.map((hit) => evidenceCard(hit))}
              {evidenceLoading && <p role="status">正在读取原文依据…</p>}
              {evidenceError && <button className="btn btn--sm" onClick={() => void showSelection(selection.item, selection.kind)}>重新读取依据</button>}
              {evidenceCursor < selectionEvidenceIds.length && <button className="btn btn--sm" disabled={evidenceLoading} onClick={() => void showSelection(selection.item, selection.kind, evidenceCursor)}>加载更多依据（剩余 {selectionEvidenceIds.length - evidenceCursor} 条）</button>}
              {!selectionEvidenceIds.length && <p className="muted">尚无原文依据，不能据此确认剧情事实。</p>}</section>}
            {rightTab === 'search' && currentResult?.degraded_reason && <p className="knowledge-notice">{currentResult.degraded_reason}</p>}
            {rightTab === 'search' && answer && currentResult && <section className="knowledge-answer"><h2>基于本书资料的回答</h2><div>{splitKnowledgeCitations(answer).map((part, index) => part.citation && currentResult.hits[part.citation - 1]
              ? <button key={index} className="knowledge-citation" onClick={() => document.getElementById(`knowledge-citation-${part.citation}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })}>{part.text}</button>
              : <span key={index}>{part.text}</span>)}</div></section>}
            {rightTab === 'search' && currentResult && <section><h2>相关依据（{currentResult.hits.length}）</h2>{currentResult.hits.map((hit, index) => evidenceCard(hit, index))}{!currentResult.hits.length && <p className="muted">本书资料中未找到足够依据。尝试补充人物名、地点或相关事件。</p>}</section>}
            {rightTab === 'material' && !selection && <section className="knowledge-related"><div className="knowledge-related__heading"><h2>相关节点 <span>({visibleRelatedNodes.length})</span></h2><span>{documentFilter ? '当前文件' : currentResult ? '检索命中' : '当前图谱'}</span></div>
              {selectedDocument && <div className="knowledge-material-summary"><strong>{selectedDocument.title || selectedDocument.rel_path}</strong><span>{knowledgeDocumentStatus(selectedDocument)}</span><small>{selectedDocument.rel_path}</small><button className="btn btn--sm" onClick={() => navigate(`/project/${projectId}/editor?${new URLSearchParams({ path: selectedDocument.rel_path })}`)}>编辑原资料</button></div>}
              {visibleRelatedNodes.map((node) => <article className="knowledge-node-card" key={node.id}>
                <div className="knowledge-node-card__meta"><span>{knowledgeKindLabel(node.kind)}</span><span>{sourceLabel(node.source, node.review_status)}</span></div>
                {(node.planned || node.lifecycle === 'draft') && <span className="knowledge-node-card__planned">{knowledgeNodeStatus(node)}</span>}
                <button className="knowledge-node-card__title" onClick={() => void showSelection(node, 'node')}>{node.label}</button>
                <p>{knowledgeNodePreview(node) || '查看节点详情与可回读的原文依据。'}</p>
                {node.source_location && <button className="knowledge-node-card__source" onClick={() => void openEvidence(node.source_location!)} title={node.source_location.rel_path}>
                  {node.source_location.rel_path} · L{node.source_location.line_start}–{node.source_location.line_end} ↗</button>}
                <button className="btn btn--ghost btn--sm" onClick={() => void showSelection(node, 'node')}>查看详情与依据</button>
              </article>)}
              {!visibleRelatedNodes.length && !searching && <div className="knowledge-search__empty"><strong>{graphError ? '当前范围读取失败，请重试或返回上一成功范围。' : graphBlocked || loading ? '正在读取相关节点…' : currentResult ? '当前图谱没有本次检索命中的节点' : selectedDocument?.index_status === 'excluded' ? '草稿未纳入历史图谱' : selectedDocument ? '当前视图没有此文件的相关节点' : '从本书资料中找到依据'}</strong>
                <p>{currentResult ? '请先查看上方原文依据，或调整检索词和图谱范围。' : selectedDocument?.index_status === 'excluded' ? '上方已明确选定此草稿，检索仅用于本次查询。' : selectedDocument ? currentGraph?.truncated ? '本次全景最多展示 300 个节点。载入文件局部视图，可继续查看此文件的实体与关系。' : '可以只看此文件，或同步资料后再查看；仍可直接检索原文。' : '例如：他上次答应了谁？这件物品最早出现在哪章？'}</p>
                {selectedDocument && !onlyDocument && !currentResult && !graphBlocked && <button className="btn btn--sm" onClick={() => { setCenter(''); setOnlyDocument(true) }}>载入此文件局部图谱</button>}</div>}
            </section>}
            {rightTab === 'search' && !currentResult && !searching && <div className="knowledge-search__empty"><strong>查找本书原文依据</strong><p>输入人物、物品或事件，查看可定位到原文的答案。</p></div>}
            {rightTab === 'search' && searching && <p className="muted" role="status">正在检索本书资料并组织回答…</p>}
            {current && <div hidden={rightTab !== 'review'}><KnowledgeReviewPanel projectId={projectId} knowledgeKey={current.knowledge_key} documents={current.documents} focusCandidateId={focusCandidateId} revision={`${current.graph_updated_at || ''}:${current.job?.id || ''}:${current.job?.status || ''}`} onCount={setPendingCount}
              onOpenEvidence={(candidate) => void openEvidence({ evidence_id: `candidate:${candidate.id}`, rel_path: candidate.evidence.rel_path, document_hash: candidate.evidence.document_hash, line_start: candidate.evidence.line_start, line_end: candidate.evidence.line_end })}
              onApplied={() => { const request = controller(); void loadOverview(request.signal).catch((err) => report(err, request.signal)).finally(() => requests.current.delete(request)); setGraphRetry((value) => value + 1) }} /></div>}
          </div>
        </aside>
      </div>
      <Modal open={configOpen} onClose={() => setConfigOpen(false)} title="本书向量检索设置" footer={<><button className="btn btn--sm" onClick={() => setConfigOpen(false)}>取消</button><button className="btn btn--sm btn--primary" disabled={configBusy} onClick={() => void saveConfig()}>保存并更新索引</button></>}>
        <p>本书使用独立数据库。模型文件可共享，检索资料始终限定在当前书内。</p>
        <p className="muted">当前检索：{String(current?.model.model || current?.model.name || '关键词')} · {current?.vector_ready ? '索引就绪' : '等待索引'}；问答：{current?.generation_model?.ready ? current.generation_model.name || '已配置' : '未配置'}</p>
        {current && (current.degraded_reason || current.model.ready === false || current.model.name === 'keyword') && <div className="knowledge-notice">{current.degraded_reason || '语义模型尚不可用，当前使用关键词检索。'}<button className="btn btn--sm" onClick={() => {
          const request = controller(); setModelPreparing(true); void prepareKnowledgeModel(request.signal)
            .then(() => { if (!request.signal.aborted) { setNotice('本地模型准备任务已启动；完成后自动建立本书索引。'); void loadOverview(request.signal) } })
            .catch((err) => { report(err, request.signal); if (!request.signal.aborted) setModelPreparing(false) }).finally(() => requests.current.delete(request))
        }} disabled={modelPreparing}>{modelPreparing ? '正在准备模型…' : '准备本地语义模型'}</button></div>}
        <label className="field"><span className="field__label">本书检索方式</span><select className="select" value={bookMode} onChange={(event) => setBookMode(event.target.value)}><option value="inherit">继承全局设置</option><option value="local">本地语义模型</option><option value="cloud">云嵌入模型</option><option value="lexical">仅关键词</option></select></label>
        {bookMode === 'cloud' && <><label className="field"><span className="field__label">已配置的服务商 ID</span><input className="input" value={bookProvider} onChange={(event) => setBookProvider(event.target.value)} autoComplete={NO_AUTOFILL} /></label><label className="field"><span className="field__label">嵌入模型 ID</span><input className="input" value={bookModel} onChange={(event) => setBookModel(event.target.value)} autoComplete={NO_AUTOFILL} /></label></>}
        {bookMode === 'local' && <p className="muted">使用工作台准备的中文本地语义模型；不可用时显示关键词降级状态。</p>}
        {configError && <p className="knowledge-notice knowledge-notice--error" role="alert">{configError}</p>}
        <button className="btn btn--sm" disabled={configBusy || !current || busyStatus(current.job?.status)} onClick={() => { setConfigOpen(false); void sync(true) }}>重建本书全部索引</button>
      </Modal>
    </section>
  )
}
