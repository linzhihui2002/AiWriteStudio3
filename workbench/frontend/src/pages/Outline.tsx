import { usePageChatContext } from '../state/usePageChatContext'
import WorkspacePage, { ResourceState, useResourceRequest } from '../components/WorkspacePage'
/** 大纲规划：灵感追问 → 候选 → 锁定 → 冻结 → 滚动规划（只细化近 2 卷）。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  expandOutline,
  freezeOutline,
  lockOutline,
  outlineCandidates,
  outlineHistory,
  readOutline,
  refineOutlineCandidate,
  rollingPlan,
  saveOutline,
  unfreezeOutline,
} from '../api/client'
import type { OutlineState } from '../api/types'
import MarkdownView from '../components/MarkdownView'
import Drawer from '../components/Drawer'
import { useConfirm } from '../components/ConfirmDialog'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'
import { useScopedAction } from '../state/useScopedAction'
import '../styles/outline.css'

export default function Outline() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const toast = useToast()

  const [state, setState] = useState<OutlineState | null>(null)
  const [content, setContent] = useState('')
  const resource = useResourceRequest(projectId)
  const historyResource = useResourceRequest(projectId)
  const operation = useScopedAction(projectId)
  const currentContent = useRef(content)
  currentContent.current = content
  const draftDirty = useRef(false)
  useEffect(() => { draftDirty.current = false; setState(null); setContent(''); setIdea(''); setRolling(null); setHistoryOpen(false); setHistory([]); setSelectedCandidateId(null); setRefineDrafts({}); setRefineError(''); setCandidateNotice(null) }, [projectId])
  const [idea, setIdea] = useState('')
  const currentIdea = useRef(idea)
  currentIdea.current = idea
  const busy = !!operation.pending
  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState<Array<Record<string, unknown>>>([])
  const [rolling, setRolling] = useState<{ refined: string[]; suggestion: string; note: string } | null>(null)
  const [confirm, confirmNode] = useConfirm()
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null)
  const [refineDrafts, setRefineDrafts] = useState<Record<string, string>>({})
  const [refineError, setRefineError] = useState('')
  const [candidateNotice, setCandidateNotice] = useState<{ text: string; error: boolean } | null>(null)
  const refinementPanel = useRef<HTMLElement>(null)
  const refinementInput = useRef<HTMLTextAreaElement>(null)
  const selectedCandidate = state?.candidates.find(candidate => candidate.id === selectedCandidateId)
  const refineDraft = selectedCandidateId ? refineDrafts[selectedCandidateId] || '' : ''
  const currentRefineDraft = useRef(refineDraft)
  currentRefineDraft.current = refineDraft
  const generatingCandidates = operation.pending === 'candidates'
  const refiningCandidate = operation.pending === 'refine'

  const load = useCallback(async () => {
    const token = resource.begin()
    try {
      const data = await readOutline(projectId)
      if (!resource.accept(token)) return
      setState(data)
      setSelectedCandidateId(previous => data.candidates.some(candidate => candidate.id === previous) ? previous : data.candidates[0]?.id ?? null)
      if (!draftDirty.current) setContent(data.content)
      resource.finish(token)
    } catch (err) {
      resource.fail(token, errorMessage(err))
    }
  }, [projectId, resource.begin, resource.accept, resource.finish, resource.fail])

  useEffect(() => {
    void load()
  }, [load])

  usePageChatContext({ projectId, activeFile: '大纲/大纲.md', hasUnsavedChanges: draftDirty.current, onFilesChanged: () => load() })

  const onSave = async () => {
    if (!state) return
    const token = operation.begin('save')
    if (!token) return
    try {
      const frozen = state.frozen
      if (frozen) {
        const confirmed = await confirm({
          title: '大纲已冻结',
          message: '大纲已冻结：修改需要差异确认。确认后将记录本次差异并重新冻结，是否继续？',
          confirmText: '继续保存',
        })
        if (!confirmed) return
      }
      if (!operation.accept(token)) return
      const submitted = currentContent.current
      await saveOutline(projectId, submitted, frozen)
      if (!operation.accept(token)) return
      draftDirty.current = currentContent.current !== submitted
      toast.push('大纲已保存', 'success')
      await load()
    } catch (err) {
      if (operation.accept(token)) toast.push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  const onExpand = async () => {
    const token = operation.begin('expand')
    if (!token) return
    const submitted = currentIdea.current
    try {
      const result = await expandOutline(projectId, submitted, true)
      if (!operation.accept(token)) return
      if (currentIdea.current === submitted) setIdea('')
      await load()
      toast.push(`已生成 ${result.questions.length} 个待拍板问题`, 'success')
    } catch (err) {
      if (operation.accept(token)) toast.push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  const onCandidates = async () => {
    const token = operation.begin('candidates')
    if (!token) return
    setCandidateNotice(null)
    try {
      const result = await outlineCandidates(projectId, currentIdea.current, 3, true)
      if (!operation.accept(token)) return
      setState(previous => previous ? { ...previous, candidates: result.candidates } : previous)
      const message = result.added_count > 0
        ? `已新增 ${result.added_count} 个候选，累计 ${result.count} 个${result.duplicate_count ? `；已过滤 ${result.duplicate_count} 个重复方案` : ''}。`
        : result.message || '本次没有新的差异化候选，已有候选已保留。可以调整灵感后继续生成。'
      setCandidateNotice({ text: message, error: false })
      await load()
      if (operation.accept(token)) toast.push(message, result.added_count > 0 ? 'success' : 'info')
    } catch (err) {
      if (operation.accept(token)) {
        const message = errorMessage(err)
        setCandidateNotice({ text: `生成失败，已有候选已保留。${message}`, error: true })
        toast.push(message, 'error')
      }
    } finally {
      operation.finish(token)
    }
  }

  const onRefine = async () => {
    if (!selectedCandidate || !currentRefineDraft.current.trim()) return
    const token = operation.begin('refine')
    if (!token) return
    const candidateId = selectedCandidate.id
    const submitted = currentRefineDraft.current
    setRefineError('')
    try {
      const result = await refineOutlineCandidate(projectId, candidateId, submitted.trim())
      if (!operation.accept(token)) return
      setState(previous => previous ? { ...previous, candidates: result.candidates } : previous)
      setSelectedCandidateId(result.candidate.id)
      setRefineDrafts(previous => {
        if (previous[candidateId] === submitted) return { ...previous, [candidateId]: '' }
        // Keep text the author added while this request was running available in the next round.
        return { ...previous, [result.candidate.id]: previous[candidateId] || '' }
      })
      await load()
      if (operation.accept(token)) toast.push('已生成优化版本，原候选已保留；可继续对话或锁定新版本。', 'success')
    } catch (err) {
      if (operation.accept(token)) {
        setRefineError(errorMessage(err))
        toast.push(errorMessage(err), 'error')
      }
    } finally {
      operation.finish(token)
    }
  }

  const selectCandidate = (candidateId: string) => {
    setSelectedCandidateId(candidateId)
    setRefineError('')
    refinementPanel.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    refinementInput.current?.focus({ preventScroll: true })
  }

  const onLock = async (candidateId: string) => {
    const token = operation.begin('lock')
    if (!token) return
    try {
      if (draftDirty.current && !(await confirm({ title: '替换正式大纲', message: '当前正式大纲有尚未保存的改动，锁定候选会替换它。确认使用这个候选？', confirmText: '锁定候选' }))) return
      if (!operation.accept(token)) return
      const submitted = currentContent.current
      await lockOutline(projectId, candidateId)
      if (!operation.accept(token)) return
      draftDirty.current = currentContent.current !== submitted
      toast.push('已锁定为正式大纲', 'success')
      await load()
    } catch (err) {
      if (operation.accept(token)) toast.push(errorMessage(err), 'error')
    } finally { operation.finish(token) }
  }

  const onFreeze = async () => {
    if (!state) return
    const shouldFreeze = !state.frozen
    if (shouldFreeze && draftDirty.current) {
      toast.push('请先保存大纲再冻结，当前修改尚未保存。', 'info')
      return
    }
    const token = operation.begin(shouldFreeze ? 'freeze' : 'unfreeze')
    if (!token) return
    try {
      if (shouldFreeze) await freezeOutline(projectId)
      else await unfreezeOutline(projectId)
      if (!operation.accept(token)) return
      setState(previous => previous ? { ...previous, frozen: shouldFreeze } : previous)
      toast.push(shouldFreeze ? '大纲已冻结（章节生产的 CHECK 门控已通过）' : '已取消冻结，可以继续修改大纲；完成后可重新冻结。', 'success')
      await load()
    } catch (err) {
      if (operation.accept(token)) toast.push(errorMessage(err), 'error')
    } finally { operation.finish(token) }
  }

  const onRolling = async () => {
    const token = operation.begin('rolling')
    if (!token) return
    try {
      const result = await rollingPlan(projectId, true)
      if (!operation.accept(token)) return
      setRolling(result)
      toast.push(`已细化近 ${result.refined.length} 卷，写入 大纲/章纲.md`, 'success')
    } catch (err) {
      if (operation.accept(token)) toast.push(errorMessage(err), 'error')
    } finally {
      operation.finish(token)
    }
  }

  return (
    <WorkspacePage>
      <header className="page-header">
        <div>
          <h1 className="page-header__title">大纲规划</h1>
          <p className="page-header__desc">
            {state?.project_name ?? ''} · 冻结后修改走差异确认
          </p>
        </div>
        <div className="btn-row">
          <Link className="btn btn--sm" to={`/project/${projectId}/chat`}>
            去写作
          </Link>
          <button className="btn btn--sm" type="button" onClick={() => void load()}>
            刷新
          </button>
          <button
            className="btn btn--sm"
            type="button"
            onClick={async () => {
              try {
                setHistoryOpen(true)
                const token = historyResource.begin()
                try { const data = await outlineHistory(projectId); if (historyResource.accept(token)) { setHistory(data); historyResource.finish(token) } } catch (err) { historyResource.fail(token, errorMessage(err)) }
              } catch (err) {
                toast.push(errorMessage(err), 'error')
              }
            }}
          >
            修改历史
          </button>
          <button className="btn btn--sm" type="button" disabled={busy} onClick={() => void onRolling()}>
            滚动规划
          </button>
          <button className="btn btn--sm" type="button" disabled={busy || !resource.loaded || !state} aria-busy={operation.pending === 'freeze' || operation.pending === 'unfreeze'} onClick={() => void onFreeze()}>
            {operation.pending === 'freeze' ? '正在冻结…' : operation.pending === 'unfreeze' ? '正在取消冻结…' : state?.frozen ? '取消冻结' : '冻结大纲'}
          </button>
          <button className="btn btn--primary btn--sm" type="button" disabled={busy || !resource.loaded} onClick={() => void onSave()}>
            保存
          </button>
        </div>
      </header>

      <ResourceState {...resource} hasData={resource.loaded && !!state} onRetry={() => void load()}>
      <ol className="workspace-stage" aria-label="大纲规划阶段">{['构思与追问','比较候选','锁定大纲','冻结验收','滚动规划'].map((label, index) => { const current = rolling ? 4 : state?.frozen ? 3 : state?.locked ? 2 : state?.candidates.length ? 1 : 0; return <li key={label} className={index === current ? 'is-current' : index < current ? 'is-done' : ''} aria-current={index === current ? 'step' : undefined}><span>{index + 1}.</span> {label}</li> })}</ol>
      <div className="row" style={{ marginBottom: 'var(--space-3)' }}>
        <span className={state?.frozen ? 'tag tag--ok' : 'tag tag--warn'}>
          {state?.frozen ? '已冻结' : '未冻结'}
        </span>
        {state?.locked ? <span className="tag">锁定候选：{state.locked.title}</span> : null}
        <span className="tag">历史 {state?.history_count ?? 0} 次</span>
        {!state?.frozen && draftDirty.current ? <span className="muted">有未保存的修改，请先保存大纲再冻结。</span> : null}
      </div>
      {operation.pending === 'freeze' || operation.pending === 'unfreeze' ? <div className="outline-progress" role="status"><span className="outline-spinner" aria-hidden="true" /><span>{operation.pending === 'freeze' ? '正在冻结大纲…' : '正在取消冻结大纲…'}</span></div> : null}

      <section className="panel">
        <header className="panel__header">
          <h2 className="panel__title">灵感扩展（定向追问）</h2>
        </header>
        <div className="panel__body stack">
          <textarea
            autoComplete={NO_AUTOFILL}
            className="textarea"
            placeholder="把你的灵感写在这里，例如：一个被逐出师门的少年，靠一本残卷重登仙途…"
            value={idea}
            onChange={(event) => { currentIdea.current = event.target.value; setIdea(event.target.value) }}
          />
          <div className="btn-row">
            <button className="btn btn--sm" type="button" disabled={busy} onClick={() => void onExpand()}>
              {operation.pending === 'expand' ? '正在追问…' : '追问关键问题'}
            </button>
            <button className="btn btn--primary btn--sm" type="button" disabled={busy} aria-busy={generatingCandidates} onClick={() => void onCandidates()}>
              {generatingCandidates ? <><span className="outline-spinner" aria-hidden="true" />正在生成候选…</> : state?.candidates.length ? '继续生成 2–3 个候选' : '生成 2–3 个候选'}
            </button>
          </div>
          {generatingCandidates ? <div className="outline-progress" role="status" aria-live="polite"><span className="outline-spinner" aria-hidden="true" /><span>正在生成候选，完成后会追加到下方。已有候选已保留，请稍候…</span></div> : candidateNotice ? <div className={`workspace-notice${candidateNotice.error ? ' workspace-notice--error' : ''}`} role={candidateNotice.error ? 'alert' : 'status'}><span>{candidateNotice.text}</span></div> : <p className="muted outline-hint">每次生成追加新方案，自动过滤重复候选。选中一个候选后，可通过对话继续优化。</p>}
          {state?.questions.length ? (
            <ol className="muted" style={{ margin: 0, paddingLeft: 'var(--space-5)' }}>
              {state.questions.map((question, index) => (
                <li key={index}>{question}</li>
              ))}
            </ol>
          ) : null}
        </div>
      </section>

      {state?.candidates.length ? (
        <section className="panel" style={{ marginTop: 'var(--space-4)' }}>
          <header className="panel__header">
            <h2 className="panel__title">大纲候选 <span className="tag">共 {state.candidates.length} 个</span></h2>
            <span className="muted">先对话优化，再确认锁定为正式大纲</span>
          </header>
          <div className="panel__body">
            <div className="workspace-candidate-grid">
              {state.candidates.map(candidate => (
                <article key={candidate.id} className={`asset-card outline-candidate${candidate.id === selectedCandidateId ? ' is-selected' : ''}`} aria-label={candidate.title}>
                  <div className="asset-card__head">
                    <span className="asset-card__name">{candidate.title}</span>
                    <span className="tag">{candidate.parent_id ? '对话优化' : candidate.source === 'model' ? '模型生成' : '占位'}</span>
                  </div>
                  {candidate.parent_id ? <p className="muted outline-hint">基于 {state.candidates.find(item => item.id === candidate.parent_id)?.title || '原候选'} 优化</p> : null}
                  <div style={{ maxHeight: '360px', overflow: 'auto' }}>
                    <MarkdownView text={candidate.content} />
                  </div>
                  <div className="btn-row">
                    <button className={`btn btn--sm${candidate.id === selectedCandidateId ? ' btn--primary' : ''}`} type="button" disabled={busy} aria-pressed={candidate.id === selectedCandidateId} onClick={() => selectCandidate(candidate.id)}>
                      {candidate.id === selectedCandidateId ? '已选中 · 对话优化' : '对话优化'}
                    </button>
                    <button className="btn btn--sm" type="button" disabled={busy} onClick={() => void onLock(candidate.id)}>
                      锁定这个候选
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>
      ) : null}

      {selectedCandidate ? (
        <section className="panel outline-refinement" ref={refinementPanel} aria-labelledby="outline-refinement-title" style={{ marginTop: 'var(--space-4)' }}>
          <header className="panel__header">
            <h2 id="outline-refinement-title" className="panel__title">候选对话优化</h2>
            <span className="tag tag--primary">当前选择：{selectedCandidate.title}</span>
          </header>
          <div className="panel__body stack">
            <p className="muted outline-hint">告诉助手你想调整的主线、人物动机、节奏或卷级结构。每轮优化保留原候选，并自动选中新版本；确认锁定后才写入正式大纲。</p>
            <details className="outline-selected-preview" key={selectedCandidate.id}>
              <summary>查看当前候选全文 · {selectedCandidate.title}</summary>
              <div><MarkdownView text={selectedCandidate.content} /></div>
            </details>
            {selectedCandidate.conversation?.length ? (
              <div className="outline-conversation" role="log" aria-label="候选优化对话记录" aria-live="polite">
                {selectedCandidate.conversation.map((message, index) => <div key={index} className={`outline-message outline-message--${message.role}`}>
                  <strong>{message.role === 'user' ? '你' : '助手 · 优化版本'}</strong>
                  {message.role === 'user' ? <p>{message.content}</p> : <details><summary>查看本轮优化结果</summary><MarkdownView text={message.content} /></details>}
                </div>)}
              </div>
            ) : <p className="muted outline-hint">还没有优化记录。从下面提出第一条修改意见。</p>}
            {refineError ? <div className="workspace-notice workspace-notice--error" role="alert"><span>优化未完成，原候选和修改意见已保留。{refineError}</span></div> : null}
            <form className="stack outline-refine-form" onSubmit={event => { event.preventDefault(); void onRefine() }}>
              <label className="field" htmlFor="outline-refine-message">
                <span className="field__label">对当前候选的修改意见</span>
                <textarea id="outline-refine-message" ref={refinementInput} autoComplete={NO_AUTOFILL} className="textarea" placeholder="例如：保留双世界设定，让主角的目标更明确，第一卷先聚焦求生，卷末再揭示搜玄录的代价。" value={refineDraft} onChange={event => {
                  const value = event.target.value
                  currentRefineDraft.current = value
                  setRefineDrafts(previous => ({ ...previous, [selectedCandidate.id]: value }))
                }} />
              </label>
              <div className="btn-row">
                <button className="btn btn--primary btn--sm" type="submit" disabled={busy || !refineDraft.trim()} aria-busy={refiningCandidate}>
                  {refiningCandidate ? <><span className="outline-spinner" aria-hidden="true" />正在优化候选…</> : '发送并优化'}
                </button>
                <span className="muted">基于 {selectedCandidate.title} 和已有对话继续调整</span>
              </div>
              {refiningCandidate ? <div className="outline-progress" role="status"><span className="outline-spinner" aria-hidden="true" /><span>正在根据你的修改意见优化当前候选，完成后会保留为新版本…</span></div> : null}
            </form>
          </div>
        </section>
      ) : null}

      <section className="panel" style={{ marginTop: 'var(--space-4)' }}>
        <header className="panel__header">
          <h2 className="panel__title">正式大纲</h2>
          <span className="muted">{draftDirty.current ? '有未保存的修改' : '保存后留下修改记录'}</span>
        </header>
        <div className="panel__body stack">
          <textarea
            autoComplete={NO_AUTOFILL}
            className="textarea"
            style={{ minHeight: '320px' }}
            value={content}
            onChange={(event) => { currentContent.current = event.target.value; draftDirty.current = true; setContent(event.target.value) }}
          />
          <div className="row row--between">
            <span className="muted">字数 {content.replace(/\s/g, '').length}</span>
            <span className="muted">冻结后修改需确认，差异会记入历史</span>
          </div>
        </div>
      </section>

      {rolling ? (
        <section className="panel" style={{ marginTop: 'var(--space-4)' }}>
          <header className="panel__header">
            <h2 className="panel__title">滚动规划结果（近 2 卷）</h2>
            <span className="muted">{rolling.note}</span>
          </header>
          <div className="panel__body">
            <div className="row">
              {rolling.refined.map((title) => (
                <span key={title} className="tag tag--primary">
                  {title}
                </span>
              ))}
            </div>
            <pre className="run-log" style={{ marginTop: 'var(--space-3)' }}>
              {rolling.suggestion}
            </pre>
          </div>
        </section>
      ) : null}

      </ResourceState>

      <Drawer title="大纲修改历史" open={historyOpen} onClose={() => setHistoryOpen(false)} width={860}>
        <ResourceState {...historyResource} hasData={historyResource.loaded} onRetry={() => { const token = historyResource.begin(); void outlineHistory(projectId).then(data => { if (historyResource.accept(token)) { setHistory(data); historyResource.finish(token) } }).catch(err => historyResource.fail(token, errorMessage(err))) }}>
        {history.length === 0 ? (
          <p className="muted">暂无历史记录。</p>
        ) : (
          <div className="stack stack--tight">
            {history.map((entry, index) => (
              <div key={index} className="panel">
                <div className="panel__body panel__body--tight">
                  <div className="row row--between">
                    <span className="mono">{String(entry.at ?? '')}</span>
                    <span className={entry.confirmed ? 'tag tag--ok' : 'tag'}>{entry.confirmed ? '差异确认' : '直接保存'}</span>
                  </div>
                  <pre className="run-log">{JSON.stringify(entry.diff ?? [], null, 2)}</pre>
                </div>
              </div>
            ))}
          </div>
        )}
        </ResourceState>
      </Drawer>

      {confirmNode}
    </WorkspacePage>
  )
}
