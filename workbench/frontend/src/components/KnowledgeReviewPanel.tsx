import { useCallback, useEffect, useRef, useState } from 'react'
import { applyProposal, getProposal } from '../api/client'
import { cancelKnowledgeExtraction, extractKnowledge, getKnowledgeCandidates, getKnowledgeEntities,
  getKnowledgeExtractionJob, getKnowledgeExtractionJobs, reviewKnowledgeCandidates, type KnowledgeCandidate, type KnowledgeCandidateEntity,
  type KnowledgeDocument, type KnowledgeExtractionJob, type KnowledgeLifecycle } from '../api/knowledge'
import type { ProposalItem } from '../api/types'
import { errorMessage } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'
import { knowledgeCandidateEntityLabel, knowledgeCandidateEntityOptions, knowledgeReviewInput, type KnowledgeCandidateEdit } from '../lib/knowledgeState'

const lifecycleLabels: Record<KnowledgeLifecycle, string> = { draft: '草稿 · 待定', planned: '计划 · 尚未发生',
  setting_fact: '设定事实', historical_event: '已发生事件 · 作者确认' }
const statusLabels: Record<string, string> = { pending: '待审核', conflict: '有冲突', stale: '原文已变化', staged: '待写入',
  applied: '已写入', ignored: '已忽略', queued: '等待分析', running: '正在分析', completed: '分析完成', failed: '分析失败', cancelled: '已停止' }
type Edit = KnowledgeCandidateEdit
const initialEdit = (candidate: KnowledgeCandidate): Edit => ({ value: candidate.suggested_value || candidate.relation || '',
  entity: candidate.requires_entity_choice ? '' : candidate.entity.anchor, target: candidate.requires_target_entity_choice ? '' : candidate.target_entity?.anchor || '',
  lifecycle: candidate.lifecycle, start: String(candidate.chapter_start || ''), end: String(candidate.chapter_end || '') })

export default function KnowledgeReviewPanel({ projectId, knowledgeKey, documents, focusCandidateId, revision, onApplied, onCount, onOpenEvidence }: {
  projectId: number; knowledgeKey: string; documents: KnowledgeDocument[]; focusCandidateId?: string; revision?: string;
  onApplied: () => void; onCount: (count: number) => void; onOpenEvidence: (candidate: KnowledgeCandidate) => void
}) {
  const identity = useRef(`${projectId}:${knowledgeKey}`)
  identity.current = `${projectId}:${knowledgeKey}`
  const [filter, setFilter] = useState('pending,conflict')
  const currentFilter = useRef(filter)
  currentFilter.current = filter
  const [candidates, setCandidates] = useState<KnowledgeCandidate[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [edits, setEdits] = useState<Record<string, Edit>>({})
  const [checked, setChecked] = useState<string[]>([])
  const [proposals, setProposals] = useState<ProposalItem[]>([])
  const [job, setJob] = useState<KnowledgeExtractionJob | null>(null)
  const [paths, setPaths] = useState<string[]>([])
  const [types, setTypes] = useState(['entity', 'field', 'relation'])
  const [showExtract, setShowExtract] = useState(false)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [targetQuery, setTargetQuery] = useState('')
  const [targets, setTargets] = useState<KnowledgeCandidateEntity[]>([])
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const expected = `${projectId}:${knowledgeKey}`
    const response = await getKnowledgeCandidates(projectId, filter, signal)
    if (signal?.aborted || identity.current !== expected || currentFilter.current !== filter) return
    setCandidates(response.candidates); setCounts(response.counts)
    onCount((response.counts.pending || 0) + (response.counts.conflict || 0))
    setEdits((previous) => Object.fromEntries(response.candidates.map((candidate) => [candidate.id,
      previous[candidate.id] || initialEdit(candidate)])))
    const proposalIds = [...new Set(response.candidates.flatMap((candidate) => candidate.proposal_id ? [candidate.proposal_id] : []))]
    const values = await Promise.allSettled(proposalIds.map((id) => getProposal(id)))
    if (!signal?.aborted && identity.current === expected && currentFilter.current === filter) setProposals(values.flatMap((value) =>
      value.status === 'fulfilled' && value.value.project_id === projectId && value.value.status === 'pending' ? [value.value] : []))
  }, [projectId, knowledgeKey, filter, focusCandidateId, revision, onCount])

  useEffect(() => {
    setCandidates([]); setProposals([]); setEdits({}); setChecked([]); setJob(null); setError(''); setNotice('')
    setPaths([]); setShowExtract(false); setTargets([]); setTargetQuery(''); setBusy(false)
  }, [projectId, knowledgeKey])
  useEffect(() => {
    const request = new AbortController(); const expected = identity.current
    void getKnowledgeExtractionJobs(projectId, request.signal).then((response) => {
      if (!request.signal.aborted && identity.current === expected) setJob((previous) =>
        previous && ['queued', 'running'].includes(previous.status) ? previous : response.jobs[0] || null)
    }).catch((err) => { if (!request.signal.aborted) setError(`分析任务读取失败：${errorMessage(err)}`) })
    return () => request.abort()
  }, [projectId, knowledgeKey, revision])
  useEffect(() => { if (focusCandidateId) setFilter('pending,conflict') }, [focusCandidateId])
  useEffect(() => {
    if (focusCandidateId) document.getElementById(`knowledge-candidate-${focusCandidateId}`)?.scrollIntoView({ block: 'nearest' })
  }, [focusCandidateId, candidates])
  useEffect(() => {
    const request = new AbortController(); setLoading(true); setError(''); setChecked([])
    void refresh(request.signal).catch((err) => { if (!request.signal.aborted) setError(errorMessage(err)) })
      .finally(() => { if (!request.signal.aborted) setLoading(false) })
    return () => request.abort()
  }, [refresh])
  useEffect(() => {
    const request = new AbortController()
    const timer = window.setTimeout(() => {
      void getKnowledgeEntities(projectId, { q: targetQuery, limit: 100 }, request.signal).then((response) => {
        if (!request.signal.aborted && response.project_id === projectId && response.knowledge_key === knowledgeKey) {
          setTargets(response.nodes.filter((node) => node.anchor && (!node.candidate_id || node.review_status === 'approved')).map((node) => ({ anchor: node.anchor!, kind: node.kind, label: node.label, document: node.document,
            line_start: node.source_location?.line_start, line_end: node.source_location?.line_end, description: node.description })))
        }
      }).catch(() => { if (!request.signal.aborted) setTargets([]) })
    }, 200)
    return () => { request.abort(); window.clearTimeout(timer) }
  }, [projectId, knowledgeKey, targetQuery])
  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.status)) return
    const request = new AbortController(); const expected = identity.current
    const timer = window.setInterval(() => {
      void getKnowledgeExtractionJob(projectId, job.id, request.signal).then((next) => {
        if (request.signal.aborted || identity.current !== expected) return
        setJob(next)
        if (!['queued', 'running'].includes(next.status)) void refresh(request.signal).catch((err) => setError(errorMessage(err)))
      }).catch((err) => { if (!request.signal.aborted) setError(errorMessage(err)) })
    }, 1500)
    return () => { request.abort(); window.clearInterval(timer) }
  }, [job?.id, job?.status, projectId, refresh])

  const update = (candidate: KnowledgeCandidate, change: Partial<Edit>) => setEdits((previous) => ({
    ...previous, [candidate.id]: { ...(previous[candidate.id] || initialEdit(candidate)), ...change },
  }))
  const run = async (action: () => Promise<void>) => {
    const expected = identity.current
    setBusy(true); setError(''); setNotice('')
    try { await action() } catch (err) { if (identity.current === expected) setError(errorMessage(err)) }
    finally { if (identity.current === expected) setBusy(false) }
  }
  const review = (items: KnowledgeCandidate[], decision: 'approve' | 'ignore') => run(async () => {
    const expected = identity.current
    const payload = items.map((candidate) => knowledgeReviewInput(candidate, edits[candidate.id] || initialEdit(candidate), decision))
    const response = await reviewKnowledgeCandidates(projectId, payload)
    if (identity.current !== expected) return
    setProposals(response.proposals.filter((proposal) => proposal.project_id === projectId)); setChecked([])
    setNotice(decision === 'approve' ? '已生成差异预览，请核对后写入原资料。' : '已忽略所选候选。')
    if (response.failures.length) setError(response.failures.map((failure) => failure.error).join('；'))
    if (decision === 'approve') setFilter('staged')
    else await refresh()
  })
  const extract = (retry = false) => run(async () => {
    const expected = identity.current
    const next = await extractKnowledge(projectId, { paths: paths.length ? paths : undefined, types,
      ...(retry && job ? { retry_job_id: job.id } : {}) })
    if (identity.current === expected) { setJob(next); setShowExtract(false); setNotice('分析已开始，候选将进入待审列表。') }
  })
  const selectable = candidates.filter((candidate) => ['pending', 'conflict'].includes(candidate.review_status))
  const selected = selectable.filter((candidate) => checked.includes(candidate.id))
  return <div className="knowledge-review">
    <div className="knowledge-review__heading"><h2>候选审核</h2><button className="btn btn--sm" onClick={() => setShowExtract((value) => !value)}>分析资料</button></div>
    <p className="muted knowledge-review__intro">自动分析只生成候选。核对原文、修改内容并审阅差异后，才会写入本书资料。</p>
    {showExtract && <section className="knowledge-review__extract"><h3>选择本次分析范围</h3><p className="muted">未勾选文件时分析全部本书资料，包含草稿和大纲；草稿内容不会成为历史事实。</p>
      <div className="knowledge-review__types">{[['entity', '实体'], ['field', '字段'], ['relation', '关系']].map(([type, label]) => <label key={type}><input type="checkbox" checked={types.includes(type)} onChange={(event) => setTypes((values) => event.target.checked ? [...values, type] : values.filter((value) => value !== type))} />{label}</label>)}</div>
      <div className="knowledge-review__files">{documents.map((document) => <label key={document.rel_path}><input type="checkbox" checked={paths.includes(document.rel_path)} onChange={(event) => setPaths((values) => event.target.checked ? [...values, document.rel_path] : values.filter((value) => value !== document.rel_path))} />{document.title || document.rel_path}{document.index_status === 'excluded' && <small>草稿</small>}</label>)}</div>
      <button className="btn btn--primary btn--sm" disabled={busy || !types.length || Boolean(job && ['queued', 'running'].includes(job.status))} onClick={() => void extract()}>开始分析{paths.length ? `（${paths.length} 份）` : '全部资料'}</button>
    </section>}
    {job && <section className="knowledge-review__job" aria-live="polite"><strong>{statusLabels[job.status]} · {job.files_completed}/{job.files_total} 份资料</strong>
      <progress max={Math.max(1, job.chunks_total)} value={job.chunks_completed} aria-label="知识候选分析进度" /><span>{job.chunks_completed}/{job.chunks_total} 片段 · 新增 {job.candidate_count} 个候选</span>
      {job.status === 'running' && <small>{job.phase === 'preparing' ? '正在准备分析模型' : job.current_document ? `正在分析：${job.current_document}` : '正在分析变化的材料'}</small>}
      {Boolean(job.document_progress?.length) && <details><summary>文件与片段进度（{job.document_progress!.length} 份）</summary><ul className="knowledge-review__progress">{job.document_progress!.map((document) => <li key={document.rel_path}><span title={document.rel_path}>{document.rel_path}</span><small>{statusLabels[document.status] || document.status} · {document.chunks_completed}/{document.chunks_total} 片段</small></li>)}</ul></details>}
      {['queued', 'running'].includes(job.status) && <button className="btn btn--sm" disabled={busy} onClick={() => void run(async () => { const expected = identity.current; const next = await cancelKnowledgeExtraction(projectId, job.id); if (identity.current === expected) setJob(next) })}>停止分析</button>}
      {job.failed_documents.map((document) => <p key={document.rel_path} className="knowledge-review__failure">{document.rel_path}：{document.error}</p>)}
      {job.failed_documents.length > 0 && !['queued', 'running'].includes(job.status) && <button className="btn btn--sm" disabled={busy} onClick={() => void extract(true)}>重试失败资料</button>}
    </section>}
    <label className="field"><span className="field__label">候选状态</span><select className="select" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="pending,conflict">全部待审（{(counts.pending || 0) + (counts.conflict || 0)}）</option>{['pending', 'conflict', 'staged', 'stale', 'applied', 'ignored'].map((status) => <option key={status} value={status}>{statusLabels[status]}（{counts[status] || 0}）</option>)}</select></label>
    {error && <p className="knowledge-notice knowledge-notice--error" role="alert">{error}</p>}{notice && <p className="knowledge-notice" role="status">{notice}</p>}
    {selectable.length > 0 && <div className="knowledge-review__bulk"><label><input type="checkbox" checked={checked.length === selectable.length} onChange={(event) => setChecked(event.target.checked ? selectable.map((candidate) => candidate.id) : [])} />选择待审项</label><button className="btn btn--sm" disabled={busy || !selected.length} onClick={() => void review(selected, 'approve')}>预览所选（{selected.length}）</button><button className="btn btn--ghost btn--sm" disabled={busy || !selected.length} onClick={() => void review(selected, 'ignore')}>忽略</button></div>}
    {loading && <p role="status">正在读取本书候选…</p>}
    {!loading && !candidates.length && <div className="knowledge-search__empty">{filter.includes('pending') ? '暂无待审候选。同步或分析资料后可在这里核对新发现。' : '此状态暂无候选。'}</div>}
    {candidates.map((candidate) => {
      const edit = edits[candidate.id] || initialEdit(candidate); const editable = ['pending', 'conflict'].includes(candidate.review_status)
      const options = knowledgeCandidateEntityOptions(candidate)
      const targetOptions = knowledgeCandidateEntityOptions(candidate, targets, true)
      return <article className={`knowledge-review__candidate ${focusCandidateId === candidate.id ? 'is-focused' : ''}`} id={`knowledge-candidate-${candidate.id}`} key={candidate.id}>
        <div className="knowledge-review__heading">{editable && <input type="checkbox" aria-label={`选择候选 ${candidate.entity.label}`} checked={checked.includes(candidate.id)} onChange={(event) => setChecked((values) => event.target.checked ? [...values, candidate.id] : values.filter((id) => id !== candidate.id))} />}<strong>{candidate.entity.label} · {candidate.kind === 'relation' ? '关系' : candidate.field || '实体'}</strong><span>{statusLabels[candidate.review_status] || candidate.review_status}</span></div>
        <blockquote><button className="knowledge-evidence__path" onClick={() => onOpenEvidence(candidate)}>{candidate.evidence.rel_path} · L{candidate.evidence.line_start}–{candidate.evidence.line_end} ↗</button><p>{candidate.evidence.quote}</p></blockquote>
        {candidate.conflicts.length > 0 && <div className="knowledge-review__conflicts"><strong>{editable ? '需要核对的冲突' : '审核时的冲突'}</strong>{candidate.conflicts.map((conflict, index) => typeof conflict === 'string' ? <p key={index}>{conflict}</p> : <p key={index}>{conflict.id === 'existing' ? '原资料' : '其他候选'}：{conflict.value}{conflict.evidence?.rel_path && <small>{conflict.evidence.rel_path}{conflict.evidence.line_start ? ` · L${conflict.evidence.line_start}–${conflict.evidence.line_end}` : ''}</small>}{conflict.evidence?.quote && <q>{conflict.evidence.quote}</q>}</p>)}</div>}
        {candidate.review_status === 'stale' && candidate.stale_reason && <p className="knowledge-review__conflicts">原文已变化：{candidate.stale_reason}</p>}
        {(candidate.current_value || candidate.kind === 'field') && <p className="knowledge-review__current"><strong>{candidate.review_status === 'applied' ? '审核前内容：' : '当前内容：'}</strong>{candidate.current_value || '此字段尚未记录'}</p>}
        <p className="muted">写入位置：{candidate.target_path}<br />来源：模型提取 · {lifecycleLabels[candidate.lifecycle]}</p>
        <label className="field"><span className="field__label">候选内容</span><textarea className="textarea" rows={3} value={edit.value} disabled={!editable || busy} onChange={(event) => update(candidate, { value: event.target.value })} /></label>
        {editable && <><label className="field"><span className="field__label">确认关联实体{candidate.requires_entity_choice ? '（存在同名实体）' : ''}</span><select className="select" value={edit.entity} onChange={(event) => update(candidate, { entity: event.target.value })}><option value="">请选择实体</option>{options.map((entity) => <option key={entity.anchor} value={entity.anchor}>{knowledgeCandidateEntityLabel(entity)}</option>)}</select></label>
          {candidate.kind === 'relation' && <label className="field"><span className="field__label">关系对象{candidate.requires_target_entity_choice ? '（存在同名实体）' : ''}</span><input className="input" autoComplete={NO_AUTOFILL} placeholder="按名称筛选本书实体" value={targetQuery} onChange={(event) => setTargetQuery(event.target.value)} /><select className="select" value={edit.target} onChange={(event) => update(candidate, { target: event.target.value })}><option value="">选择关系对象</option>{targetOptions.map((entity) => <option key={entity.anchor} value={entity.anchor}>{knowledgeCandidateEntityLabel(entity)}</option>)}</select></label>}
          <label className="field"><span className="field__label">事实状态</span><select className="select" value={edit.lifecycle} onChange={(event) => update(candidate, { lifecycle: event.target.value as KnowledgeLifecycle })}>{Object.entries(lifecycleLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
          {edit.lifecycle === 'historical_event' && ['draft', 'planned'].includes(candidate.source_lifecycle || candidate.lifecycle) && <p className="knowledge-draft-note">你将明确确认这项草稿或计划中的事件已发生；核对差异并写入后，它会进入历史事实上下文，原始来源仍保留。</p>}
          <div className="knowledge-review__chapters"><label>起始章<input className="input" type="number" min={1} value={edit.start} onChange={(event) => update(candidate, { start: event.target.value })} /></label><label>结束章<input className="input" type="number" min={1} value={edit.end} onChange={(event) => update(candidate, { end: event.target.value })} /></label></div>
          <div className="btn-row"><button className="btn btn--primary btn--sm" disabled={busy} onClick={() => void review([candidate], 'approve')}>审阅写入差异</button><button className="btn btn--ghost btn--sm" disabled={busy} onClick={() => void review([candidate], 'ignore')}>忽略候选</button></div></>}
      </article>
    })}
    {proposals.map((proposal) => <article className="knowledge-review__proposal" key={proposal.id}><h3>{proposal.title}</h3><p className="muted">将写入：{proposal.target_path}</p><pre aria-label="写入差异">{proposal.diff?.length ? proposal.diff.map((line, index) => <span key={index} data-change={line.type}>{line.type === 'add' ? '+' : line.type === 'remove' ? '−' : ' '} {line.text.startsWith('<!-- wb-knowledge ') ? '[知识记录：来源、事实状态及原文依据]' : line.text === '<!-- /wb-knowledge -->' ? '[知识记录结束]' : line.text}{'\n'}</span>) : proposal.content}</pre>
      {proposal.diff?.some((line) => line.text.startsWith('<!-- wb-knowledge ')) && <details><summary>展开完整差异（含知识记录信息）</summary><pre aria-label="完整写入差异">{proposal.diff.map((line, index) => <span key={index} data-change={line.type}>{line.type === 'add' ? '+' : line.type === 'remove' ? '−' : ' '} {line.text}{'\n'}</span>)}</pre></details>}
      <button className="btn btn--primary btn--sm" disabled={busy} onClick={() => void run(async () => {
      const expected = identity.current
      const result = await applyProposal(proposal.id)
      if (identity.current === expected) {
        const knowledge = result.knowledge as { status?: string } | undefined
        setNotice(knowledge?.status === 'reconciliation_pending' ? '文件已写入，审核记录正在等待补账；刷新后会继续核对提交凭据。' : '已写入本书原资料，正在更新知识库。')
        await refresh(); onApplied()
      }
    })}>确认写入原资料</button></article>)}
  </div>
}
