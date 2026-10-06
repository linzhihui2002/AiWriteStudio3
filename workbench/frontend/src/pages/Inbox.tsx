import WorkspacePage, { ResourceState, useResourceRequest, ProjectPicker } from '../components/WorkspacePage'
/** 收件箱 —— AI 产出不静默落盘，必须人工核对差异后应用。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import Drawer from '../components/Drawer'
import DiffView from '../components/DiffView'
import ProseQualityReport from '../components/ProseQualityReport'
import {
  applyProposal,
  applyProposals,
  discardProposal,
  discardProposals,
  getProposal,
  listProposals,
  listProjects,
  reviewProposal,
  rebaseStateProposal,
  updateProposal,
} from '../api/client'
import type { Project, ProposalItem, ReviewResult } from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'
import { useScopedAction } from '../state/useScopedAction'

interface BatchEntry {
  id: number
  ok: boolean
  error?: string
}

const STATUS_TABS: ReadonlyArray<{ key: string; label: string; value: string | null }> = [
  { key: 'all', label: '全部', value: null },
  { key: 'pending', label: '待处理', value: 'pending' },
  { key: 'applied', label: '已应用', value: 'applied' },
  { key: 'discarded', label: '已丢弃', value: 'discarded' },
]

const STATUS_LABELS: Record<string, string> = {
  pending: '待处理',
  applied: '已应用',
  discarded: '已丢弃',
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

function statusTagClass(status: string): string {
  if (status === 'applied') return 'tag tag--ok'
  if (status === 'discarded') return 'tag'
  return 'tag tag--warn'
}

function metadataObject(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function isPipelineCandidate(proposal: ProposalItem): boolean {
  return metadataObject(proposal.meta.pipeline_candidate) !== null
}

export default function Inbox() {
  const { push } = useToast()

  const [statusKey, setStatusKey] = useState('pending')
  const [projectFilter, setProjectFilter] = useState('')
  const [items, setItems] = useState<ProposalItem[]>([])
  const [selected, setSelected] = useState<number[]>([])
  const [detail, setDetail] = useState<ProposalItem | null>(null)
  const [detailId, setDetailId] = useState<number | null>(null)
  const [editContent, setEditContent] = useState('')
  const [batch, setBatch] = useState<BatchEntry[] | null>(null)
  const [detailEpoch, setDetailEpoch] = useState(0)
  const operation = useScopedAction(`${projectFilter}:${statusKey}:${detailId ?? 'list'}:${detailEpoch}`)
  const busy = !!operation.pending
  const editContentRef = useRef(editContent)
  editContentRef.current = editContent
  const [reviewProgress, setReviewProgress] = useState('')
  const [reviewReport, setReviewReport] = useState<{ proposalId: number; content: string; result: ReviewResult } | null>(null)
  const detailIdRef = useRef(detailId)
  detailIdRef.current = detailId
  const [projects, setProjects] = useState<Project[]>([])
  const resource = useResourceRequest(`${projectFilter}:${statusKey}`)
  const detailResource = useResourceRequest(`${projectFilter}:${statusKey}:${detailId}`)
  const projectsResource = useResourceRequest('projects')
  const loadProjects = useCallback(async () => { const token = projectsResource.begin(); try { const data = await listProjects(true); if (!projectsResource.accept(token)) return; setProjects(data); projectsResource.finish(token) } catch (err) { projectsResource.fail(token, errorMessage(err)) } }, [])
  useEffect(() => { void loadProjects() }, [loadProjects])

  const statusValue = STATUS_TABS.find((tab) => tab.key === statusKey)?.value ?? null
  const projectId = projectFilter.trim() === '' ? undefined : Number(projectFilter)

  const load = useCallback(async () => {
    const token = resource.begin()
    try {
      const data = await listProposals(projectId, statusValue)
      if (!resource.accept(token)) return
      setItems(data)
      resource.finish(token)
      setSelected((current) => current.filter((id) => data.some((item) => item.id === id)))
    } catch (err) {
      resource.fail(token, errorMessage(err))
    }
  }, [projectId, statusValue, push])

  useEffect(() => {
    void load()
  }, [load])

  const loadDetail = useCallback(async () => {
    if (detailId === null) return
    const token = detailResource.begin()
    try { const data = await getProposal(detailId); if (!detailResource.accept(token)) return; setDetail(data); setEditContent(data.content); detailResource.finish(token) } catch (err) { detailResource.fail(token, errorMessage(err)) }
  }, [detailId, projectFilter, statusKey])
  useEffect(() => { if (detailId !== null) void loadDetail() }, [detailId, loadDetail])
  useEffect(() => { setDetailId(null); setDetail(null); setEditContent(''); setReviewReport(null); setBatch(null); setSelected([]); setReviewProgress('') }, [projectFilter, statusKey])
  const openDetail = (id: number) => { setDetailEpoch(value => value + 1); setReviewProgress(''); setReviewReport(null); setDetailId(id) }

  const closeDetail = () => {
    setDetailEpoch(value => value + 1)
    setReviewProgress('')
    setDetailId(null)
    setDetail(null)
    setEditContent('')
    setReviewReport(null)
  }

  const handleApply = async (id: number, content?: string) => {
    const token = operation.begin('apply')
    if (!token) return
    try {
      await applyProposal(id, content)
      if (!operation.accept(token)) return
      push('已应用提案（已生成快照并刷新索引）', 'success')
      closeDetail()
      await load()
    } catch (err) {
      if (operation.accept(token)) push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  const handleReview = async () => {
    if (!detail || detail.status !== 'pending' || !isPipelineCandidate(detail)) return
    const proposalId = detail.id
    const content = editContent
    const token = operation.begin('review')
    if (!token) return
    setReviewReport(null)
    try {
      if (content !== detail.content) {
        setReviewProgress('正在保存编辑内容…')
        const saved = await updateProposal(proposalId, content)
        if (!operation.accept(token)) return
        if (detailIdRef.current === proposalId) setDetail(saved)
      }
      setReviewProgress('正在审查当前候选，核对章节合同与正文证据…')
      const result = await reviewProposal(proposalId)
      if (!operation.accept(token)) return
      if (detailIdRef.current === proposalId) setReviewReport({ proposalId, content, result })
      const refreshed = await getProposal(proposalId)
      if (!operation.accept(token)) return
      if (detailIdRef.current === proposalId) {
        setDetail(refreshed)
        if (editContentRef.current === content) setEditContent(refreshed.content)
      }
      push(result.verdict === '通过'
        ? '当前候选审查通过，请核对后点击“应用”写入正文'
        : '当前候选审查未通过，请按审查问题修改后再审查', result.verdict === '通过' ? 'success' : 'info')
      await load()
    } catch (err) {
      if (operation.accept(token)) push(errorMessage(err), 'error')
    } finally {
      if (operation.accept(token)) setReviewProgress('')
      operation.finish(token)
    }
  }

  const handleEditApply = async () => {
    if (!detail) return
    if (isPipelineCandidate(detail)) {
      await handleReview()
      return
    }
    const token = operation.begin('edit-apply')
    if (!token) return
    const proposalId = detail.id
    const submitted = editContent
    try {
      await updateProposal(proposalId, submitted)
      if (!operation.accept(token)) return
      if (editContentRef.current !== submitted) { push('编辑内容已变化，请核对后重新应用', 'info'); return }
      await applyProposal(proposalId, submitted)
      if (!operation.accept(token)) return
      push('已按编辑内容应用提案', 'success')
      closeDetail()
      await load()
    } catch (err) {
      if (operation.accept(token)) push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  const handleDiscard = async (id: number) => {
    const token = operation.begin('discard')
    if (!token) return
    try {
      await discardProposal(id)
      if (!operation.accept(token)) return
      push('已丢弃提案', 'success')
      closeDetail()
      await load()
    } catch (err) {
      if (operation.accept(token)) push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  const handleRebaseState = async () => {
    if (!detail) return
    if (editContentRef.current !== detail.content) {
      push('候选中还有未保存的人工修改，请先保留修改再核对当前状态材料', 'error')
      return
    }
    const token = operation.begin('rebase-state')
    if (!token) return
    try {
      const result = await rebaseStateProposal(detail.id)
      if (!operation.accept(token)) return
      push(result.already_current ? '当前角色状态已经包含这些变化' : '已按当前状态重提，请核对新的完整差异', 'info')
      if (result.proposal) openDetail(result.proposal.id)
      else closeDetail()
      await load()
    } catch (err) {
      if (operation.accept(token)) push(errorMessage(err), 'error')
    } finally { operation.finish(token) }
  }

  const toggleSelect = (id: number) => {
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    )
  }

  const eligible = items.filter(item => item.status === 'pending')
  const allSelected = eligible.length > 0 && selected.length === eligible.length

  const toggleAll = () => {
    setSelected(allSelected ? [] : eligible.map((item) => item.id))
  }

  const handleBatchApply = async () => {
    if (selected.length === 0) return
    const token = operation.begin('batch-apply')
    if (!token) return
    try {
      const result = await applyProposals(selected)
      if (!operation.accept(token)) return
      setBatch(result.results as unknown as BatchEntry[])
      push(`批量应用：成功 ${result.applied} 条，失败 ${result.failed} 条`, 'success')
      setSelected([])
      await load()
    } catch (err) {
      if (operation.accept(token)) push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  const handleBatchDiscard = async () => {
    if (selected.length === 0) return
    const token = operation.begin('batch-discard')
    if (!token) return
    try {
      const result = (await discardProposals(selected)) as unknown as {
        discarded: number
        results?: BatchEntry[]
      }
      if (!operation.accept(token)) return
      setBatch(result.results ?? [])
      push(`批量丢弃：成功 ${result.discarded} 条`, 'success')
      setSelected([])
      await load()
    } catch (err) {
      if (operation.accept(token)) push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  const pipelineDetail = detail !== null && isPipelineCandidate(detail)
  const editedCandidate = pipelineDetail && editContent !== detail?.content
  const currentReview = reviewReport?.proposalId === detail?.id ? reviewReport : null
  const reviewIsCurrent = currentReview !== null && currentReview.content === editContent
  const reviewBlocksApply = currentReview !== null && (!reviewIsCurrent || currentReview.result.verdict !== '通过' || !currentReview.result.ai_used)
  const storedReview = metadataObject(detail?.meta.pipeline_review)

  return (
    <WorkspacePage>
      <header className="page-header">
        <div>
          <h1 className="page-header__title">收件箱</h1>
          <p className="page-header__desc">AI 产出以提案形式集中在此，人工确认后再落盘。</p>
        </div>
      </header>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">筛选</h2>
          <span className="muted">共 {items.length} 条</span>
        </div>
        <div className="panel__body stack">
          <div className="tabs">
            {STATUS_TABS.map((tab) => (
              <button
                key={tab.key}
                className={tab.key === statusKey ? 'tab is-active' : 'tab'}
                type="button"
                aria-pressed={tab.key === statusKey}
                onClick={() => setStatusKey(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </div>

          <ResourceState {...projectsResource} hasData={projectsResource.loaded} onRetry={() => void loadProjects()}><ProjectPicker projects={projects} value={projectFilter} onChange={value => { setProjectFilter(value); setSelected([]) }} allowAll label="提案书籍" /></ResourceState>
          <div className="btn-row">
            <button
              className="btn btn--primary"
              type="button"
              disabled={busy || selected.length === 0}
              onClick={() => void handleBatchApply()}
            >
              批量应用（{selected.length}）
            </button>
            <button
              className="btn btn--danger"
              type="button"
              disabled={busy || selected.length === 0}
              onClick={() => void handleBatchDiscard()}
            >
              批量丢弃（{selected.length}）
            </button>
            <button className="btn btn--ghost" type="button" onClick={() => void load()}>
              刷新
            </button>
          </div>

          {batch ? (
            <div className="stack stack--tight">
              <p className="muted">批量结果（逐条）</p>
              <ul className="notice__list">
                {batch.map((entry) => (
                  <li key={entry.id}>
                    提案 <span className="mono">{entry.id}</span>：
                    <span className={entry.ok ? 'cell-pass' : 'cell-fail'}>
                      {entry.ok ? '成功' : '失败'}
                    </span>
                    {entry.error ? <span className="muted">（{entry.error}）</span> : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </section>

      <ResourceState {...resource} hasData={resource.loaded && items.length > 0} onRetry={() => void load()}>
      {items.length === 0 ? (
        <div className="empty-state">
          <p className="empty-state__title">暂无提案</p>
          <p className="empty-state__desc">当前书籍与状态下没有提案，可切换筛选查看其他记录。</p>
        </div>
      ) : (
        <div className="stack">
          <label className="checkbox">
            <input type="checkbox" checked={allSelected} onChange={toggleAll} />
            全选当前列表
          </label>
          {items.map((proposal) => (
            <div className="panel" key={proposal.id}>
              <div className="panel__body panel__body--tight">
                <div className="row row--between">
                  <div className="row">
                    <input
                      type="checkbox"
                      disabled={proposal.status !== 'pending' || busy}
                      checked={selected.includes(proposal.id)}
                      onChange={() => toggleSelect(proposal.id)}
                      aria-label={`选择提案 ${proposal.id}`}
                    />
                    <span className="tag tag--primary">{proposal.kind}</span>
                    <span>{proposal.title}</span>
                    {proposal.is_new_file ? <span className="tag tag--warn">新文件</span> : null}
                  </div>
                  <div className="btn-row">
                    <span className={statusTagClass(proposal.status)}>
                      {statusLabel(proposal.status)}
                    </span>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      disabled={busy}
                      onClick={() => void openDetail(proposal.id)}
                    >
                      查看
                    </button>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      disabled={busy || proposal.status !== 'pending'}
                      onClick={() => void handleApply(proposal.id)}
                    >
                      应用
                    </button>
                    <button
                      className="btn btn--danger btn--sm"
                      type="button"
                      disabled={busy || proposal.status !== 'pending'}
                      onClick={() => void handleDiscard(proposal.id)}
                    >
                      丢弃
                    </button>
                  </div>
                </div>
                <div className="row">
                  <span className="muted">
                    目标：<span className="mono">{proposal.target_path}</span>
                  </span>
                  <span className="muted">字数 {proposal.chars}</span>
                  <span className="muted mono">{proposal.created_at}</span>
                  {proposal.project_id === null ? null : (
                    <span className="muted">{projects.find(project => project.id === proposal.project_id)?.name || `书籍 #${proposal.project_id}`}</span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      </ResourceState>

      <Drawer
        title={detail ? `提案 #${detail.id} · ${detail.title}` : '提案详情'}
        open={detailId !== null}
        onClose={closeDetail}
        width={860}
        footer={
          detail?.id === detailId && detailResource.loaded ? (
            <div className="btn-row btn-row--end">
              {detail.kind === 'state' && detail.meta.ingestion && Array.isArray(detail.meta.state_changes) ? <button
                className="btn" type="button" disabled={busy || detail.status !== 'pending'}
                onClick={() => void handleRebaseState()}>按当前状态重提</button> : null}
              <button
                className="btn btn--ghost"
                type="button"
                disabled={busy}
                onClick={closeDetail}
              >
                取消
              </button>
              <button
                className="btn btn--danger"
                type="button"
                disabled={busy || detail.status !== 'pending'}
                onClick={() => void handleDiscard(detail.id)}
              >
                丢弃
              </button>
              <button
                className="btn"
                type="button"
                disabled={busy || detail.status !== 'pending'}
                onClick={() => void handleEditApply()}
              >
                {pipelineDetail ? '审查当前候选' : '编辑后应用'}
              </button>
              <button
                className="btn btn--primary"
                type="button"
                disabled={busy || detail.status !== 'pending' || editedCandidate || reviewBlocksApply}
                onClick={() => void handleApply(detail.id)}
              >
                应用
              </button>
            </div>
          ) : null
        }
      >
        <ResourceState {...detailResource} hasData={detailResource.loaded && detail?.id === detailId} onRetry={() => void loadDetail()}>
        {detail?.id === detailId ? (
          <div className="stack">
            <div className="row row--between">
              <div className="row">
                <span className="tag tag--primary">{detail.kind}</span>
                <span className={statusTagClass(detail.status)}>{statusLabel(detail.status)}</span>
                {detail.is_new_file ? <span className="tag tag--warn">新文件</span> : null}
              </div>
              <span className="muted mono">{detail.created_at}</span>
            </div>

            <div className="row">
              <span className="muted">
                目标：<span className="mono">{detail.target_path}</span>
              </span>
              <span className="muted">字数 {detail.chars}</span>
            </div>

            {pipelineDetail ? (
              <section className="panel" aria-label="候选审查">
                <div className="panel__body stack stack--tight">
                  <p>这是管线生成的章节候选。编辑后先保存并审查，逐项通过后由你点击“应用”写入正文。</p>
                  {reviewProgress ? <p className="muted" role="status" aria-live="polite">{reviewProgress}</p> : null}
                  {editedCandidate || (currentReview && !reviewIsCurrent) ? (
                    <p className="notice notice--warn">正文已修改，需点击“审查当前候选”重新审查修改后的版本。</p>
                  ) : null}
                  {!currentReview && typeof storedReview?.verdict === 'string' ? (
                    <p className="muted">上次审查：{storedReview.verdict}。正文或合同发生变化后需重新审查。</p>
                  ) : null}
                  {currentReview ? (
                    <>
                      <div className="row">
                        <span className={currentReview.result.verdict === '通过' && reviewIsCurrent ? 'tag tag--ok' : 'tag tag--warn'}>
                          {reviewIsCurrent ? '当前候选' : '修改前候选'}：{currentReview.result.verdict}
                        </span>
                        <span className="muted">已完成 {currentReview.result.counts.已完成} 项，未完成 {currentReview.result.counts.未完成} 项，待核实 {currentReview.result.counts.待核实} 项</span>
                      </div>
                      {!currentReview.result.ai_used ? <p className="notice notice--warn">{currentReview.result.ai_error || '尚未获得可核验的 AI 逐项审稿，不能应用。'}</p> : null}
                      {currentReview.result.items.some(item => item.判定 !== '已完成') ? (
                        <ul className="notice__list">
                          {currentReview.result.items.filter(item => item.判定 !== '已完成').map((item, index) => (
                            <li key={`${item.项}:${index}`}>
                              {item.项}：{item.判定}。{item.核验说明 || item.证据 || '缺少可定位的正文证据，请修改或补充后再审查。'}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {currentReview.result.hard_gates.gates.some(gate => gate.blocking && !gate.passed) ? (
                        <ul className="notice__list">
                          {currentReview.result.hard_gates.gates.filter(gate => gate.blocking && !gate.passed).map(gate => (
                            <li key={gate.key}>正文门禁：{gate.detail}</li>
                          ))}
                        </ul>
                      ) : null}
                      {currentReview.result.hard_gates.locations.length > 0 ? (
                        <ul className="notice__list">
                          {currentReview.result.hard_gates.locations.map((location, index) => (
                            <li key={`${location.gate}:${index}`}>{location.line > 0 ? `第 ${location.line} 行：` : ''}{location.reason}{location.text ? `（${location.text}）` : ''}</li>
                          ))}
                        </ul>
                      ) : null}
                      {currentReview.result.consistency.length > 0 ? (
                        <ul className="notice__list">
                          {currentReview.result.consistency.map((issue, index) => (
                            <li key={index}>{issue.类型}：{issue.说明}{issue.证据 ? `（${issue.证据}）` : ''}</li>
                          ))}
                        </ul>
                      ) : null}
                      {currentReview.result.revise_instructions.length > 0 ? (
                        <div>
                          <p className="muted">修改建议</p>
                          <ul className="notice__list">{currentReview.result.revise_instructions.map((instruction, index) => <li key={index}>{instruction}</li>)}</ul>
                        </div>
                      ) : null}
                    </>
                  ) : null}
                </div>
              </section>
            ) : null}

            {currentReview && <ProseQualityReport quality={currentReview.result.prose_quality}
              modelSuggestions={currentReview.result.prose_review} historyQuality={currentReview.result.prose_history} stale={!reviewIsCurrent} />}

            <div>
              <p className="muted">差异对比</p>
              <DiffView lines={detail.diff ?? []} emptyHint="该提案没有可展示的差异" />
            </div>

            <div className="field">
              <label className="field__label" htmlFor="inbox-content">
                {pipelineDetail ? '候选正文（修改后需重新审查）' : '提案内容（可编辑后应用）'}
              </label>
              <textarea
                autoComplete={NO_AUTOFILL}
                id="inbox-content"
                className="textarea textarea--mono"
                readOnly={detail.status !== 'pending'}
                disabled={busy}
                value={editContent}
                onChange={(event) => { editContentRef.current = event.target.value; setEditContent(event.target.value) }}
              />
              <span className="field__hint">{pipelineDetail ? '点击“审查当前候选”会先保存当前编辑内容。审查不会自动应用正文。' : '按修改后的内容应用。'}</span>
            </div>
          </div>
        ) : null}</ResourceState>
      </Drawer>
    </WorkspacePage>
  )
}
