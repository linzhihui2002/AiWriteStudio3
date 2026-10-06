import { usePageChatContext } from '../state/usePageChatContext'
import WorkspacePage, { ResourceState, useResourceRequest, WorkspaceTabs } from '../components/WorkspacePage'
/** 审稿中心 —— 逐项审稿 / 硬门禁定位 / 质检矩阵 / 质量债 / 文风指纹。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ingestChapter,
  cancelStyleApproval,
  listChapters,
  listDebts,
  listStyleFingerprints,
  qualityMatrix,
  readChapter,
  resolveDebt,
  reviewChapter,
  sampleStyle,
  softDeslop,
} from '../api/client'
import type { ChapterSummary, IngestionResult, QualityMatrix, ReviewResult, SoftDeslopResult, StyleFingerprint, StyleReferenceMetrics } from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import Drawer from '../components/Drawer'
import ProseQualityReport from '../components/ProseQualityReport'
import { chapterLabelWithCount } from '../lib/chapterName'
import { useScopedAction } from '../state/useScopedAction'
import { IngestionReport, SoftDeslopReport } from '../components/ReviewOperationReport'
import { reportVersion, REVIEW_MODES, softReviewMessage, type ReviewMode } from '../lib/reviewWorkspace'
import '../styles/reviewWorkspace.css'

interface DebtItem {
  id: number
  rel_path: string
  level: string
  status: string
  note: string
  created_at: string
}

interface StylePayload {
  samples: StyleFingerprint[]
  reference: StyleReferenceMetrics | null
}

/** 质量债分级说明（与后端 review_service 一致）。 */
const DEBT_LEVELS: ReadonlyArray<{ level: string; desc: string }> = [
  { level: '债-1', desc: '章级瑕疵（登记放行）' },
  { level: '债-2', desc: '一致性问题（放行但标记待校订）' },
  { level: '阻断', desc: '剧情路线冲突 / 合同未达成（停链回 CONTRACT）' },
]

function cellClass(value: string): string {
  if (value === '通过') return 'cell-pass'
  if (value === '未通过') return 'cell-fail'
  return 'cell-idle'
}

function verdictTagClass(verdict: string): string {
  return verdict === '通过' ? 'tag tag--ok' : 'tag tag--danger'
}

function itemTagClass(verdict: string): string {
  if (verdict === '已完成') return 'tag tag--ok'
  if (verdict === '未完成') return 'tag tag--danger'
  return 'tag tag--warn'
}

function debtTagClass(level: string): string {
  return level === '阻断' ? 'tag tag--danger' : 'tag tag--warn'
}

function debtLevelDesc(level: string): string {
  return DEBT_LEVELS.find((entry) => entry.level === level)?.desc ?? level
}

function formatMetric(value: unknown, digits = 2): string {
  if (typeof value === 'number' && Number.isFinite(value)) return value.toFixed(digits)
  if (typeof value === 'string') return value
  return '—'
}

export default function Review() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const navigate = useNavigate()
  const { push } = useToast()

  const [chapters, setChapters] = useState<ChapterSummary[]>([])
  const [chapterRel, setChapterRel] = useState('')
  const scope = `${projectId}:${chapterRel}`
  const [dataScope, setDataScope] = useState(scope)
  const [storedResult, setResult] = useState<ReviewResult | null>(null)
  const result = dataScope === scope ? storedResult : null
  const [matrix, setMatrix] = useState<QualityMatrix | null>(null)
  const [debts, setDebts] = useState<DebtItem[]>([])
  const [style, setStyle] = useState<StylePayload>({ samples: [], reference: null })
  const [styleNote, setStyleNote] = useState('')
  const styleNoteEdited = useRef(false)
  const [storedDeslop, setDeslop] = useState<SoftDeslopResult | null>(null)
  const [storedIngest, setIngest] = useState<IngestionResult | null>(null)
  const deslop = dataScope === scope ? storedDeslop : null
  const ingest = dataScope === scope ? storedIngest : null
  const [mode, setMode] = useState<ReviewMode>('review')
  const [actionErrors, setActionErrors] = useState<Partial<Record<ReviewMode, string>>>({})
  const [currentHash, setCurrentHash] = useState('')
  const action = useScopedAction(scope)
  const busy = action.pending
  const [tab, setTab] = useState<'review' | 'quality' | 'style'>('review')
  const [reviewStarted, setReviewStarted] = useState(false)
  const [problem, setProblem] = useState<{title:string; evidence:string; kind:string; line?:number} | null>(null)
  const chaptersResource = useResourceRequest(projectId)
  const matrixResource = useResourceRequest(projectId)
  const debtsResource = useResourceRequest(projectId)
  const styleResource = useResourceRequest(projectId)
  const reviewResource = useResourceRequest(`${projectId}:${chapterRel}`)
  const versionResource = useResourceRequest(`${projectId}:${chapterRel}`)
  const loadVersion = useCallback(async () => {
    if (!chapterRel) return
    const token = versionResource.begin()
    try { const data = await readChapter(projectId, chapterRel); if (!versionResource.accept(token)) return; setCurrentHash(data.hash || ''); versionResource.finish(token) }
    catch (err) { if (versionResource.accept(token)) setCurrentHash(''); versionResource.fail(token, errorMessage(err)) }
  }, [projectId, chapterRel])

  const loadMatrix = useCallback(async () => { const token = matrixResource.begin(); try { const data = await qualityMatrix(projectId); if (!matrixResource.accept(token)) return; setMatrix(data); matrixResource.finish(token) } catch (err) { matrixResource.fail(token, errorMessage(err)) } }, [projectId])
  const loadDebts = useCallback(async () => { const token = debtsResource.begin(); try { const data = await listDebts(projectId); if (!debtsResource.accept(token)) return; setDebts(data); debtsResource.finish(token) } catch (err) { debtsResource.fail(token, errorMessage(err)) } }, [projectId])
  const loadStyle = useCallback(async () => { const token = styleResource.begin(); try { const data = await listStyleFingerprints(projectId); if (!styleResource.accept(token)) return; setStyle(data); styleResource.finish(token) } catch (err) { styleResource.fail(token, errorMessage(err)) } }, [projectId])
  const loadQuality = useCallback(async () => { await Promise.allSettled([loadMatrix(), loadDebts(), loadStyle()]) }, [loadMatrix, loadDebts, loadStyle])
  const loadChapters = useCallback(async () => { const token = chaptersResource.begin(); try { const data = await listChapters(projectId); if (!chaptersResource.accept(token)) return; setChapters(data); setChapterRel(value => data.some(item => item.rel_path === value) ? value : data[0]?.rel_path || ''); chaptersResource.finish(token) } catch (err) { chaptersResource.fail(token, errorMessage(err)) } }, [projectId])
  useEffect(() => { setChapters([]); setChapterRel(''); setMatrix(null); setDebts([]); setStyle({samples:[],reference:null}); setStyleNote(''); setResult(null); setDeslop(null); setIngest(null); setProblem(null); setActionErrors({}); setCurrentHash(''); void loadChapters(); void loadQuality() }, [projectId, loadChapters, loadQuality])
  useEffect(() => { setDataScope(scope); setResult(null); setReviewStarted(false); setDeslop(null); setIngest(null); setProblem(null); setActionErrors({}); setCurrentHash(''); setStyleNote(''); styleNoteEdited.current = false; void loadVersion() }, [chapterRel, loadVersion])
  useEffect(() => { if (!styleNoteEdited.current) setStyleNote(style.samples.find(sample => sample.rel_path === chapterRel)?.note || '') }, [chapterRel, style.samples])
  useEffect(() => { const refresh = () => { void loadVersion(); void loadQuality() }; window.addEventListener('focus', refresh); return () => window.removeEventListener('focus', refresh) }, [loadVersion, loadQuality])

  usePageChatContext({ projectId, activeFile: chapterRel, files: chapters.map(chapter => chapter.rel_path), onFilesChanged: async () => { await Promise.allSettled([loadChapters(), loadQuality(), loadVersion()]) } })

  const runChapterReview = async () => {
    if (!chapterRel) {
      push('请先选择章节', 'error')
      return
    }
    const actionToken = action.begin('review')
    if (!actionToken) return
    setReviewStarted(true)
    const token = reviewResource.begin()
    try {
      const data = await reviewChapter(projectId, chapterRel)
      if (!action.accept(actionToken) || !reviewResource.accept(token)) return
      setResult(data)
      reviewResource.finish(token)
      push(data.ai_used ? `逐项审稿完成：${data.verdict}` : data.ai_error || '本次只完成规则检查，AI 逐项验收尚未完成。', data.ai_used ? 'success' : 'error')
      await loadQuality()
      if (action.accept(actionToken)) await loadVersion()
    } catch (err) {
      if (action.accept(actionToken)) { reviewResource.fail(token, errorMessage(err)); await loadVersion() }
    } finally {
      action.finish(actionToken)
    }
  }

  const runDeslop = async () => {
    if (!chapterRel) {
      push('请先选择章节', 'error')
      return
    }
    const token = action.begin('deslop')
    if (!token) return
    setActionErrors(value => ({ ...value, deslop: '' }))
    try {
      const data = await softDeslop(projectId, chapterRel)
      if (!action.accept(token)) return
      setDeslop(data)
      const message = softReviewMessage(data)
      push(message, !data.ai_used ? 'error' : data.source_changed || data.proposal_ids.length === 0 ? 'info' : 'success')
      await loadVersion()
    } catch (err) {
      if (action.accept(token)) { setActionErrors(value => ({ ...value, deslop: errorMessage(err) })); push(errorMessage(err), 'error'); await loadVersion() }
    } finally {
      action.finish(token)
    }
  }

  const runIngest = async () => {
    if (!chapterRel) {
      push('请先选择章节', 'error')
      return
    }
    const token = action.begin('ingest')
    if (!token) return
    setActionErrors(value => ({ ...value, ingest: '' }))
    try {
      const data = await ingestChapter(projectId, chapterRel)
      if (!action.accept(token)) return
      setIngest(data)
      push(data.replayed ? '当前版本已经提取，没有重复追加记录' : '已提取四件套', 'success')
      await loadQuality()
      if (action.accept(token)) await loadVersion()
    } catch (err) {
      if (action.accept(token)) { setActionErrors(value => ({ ...value, ingest: errorMessage(err) })); push(errorMessage(err), 'error'); await loadVersion() }
    } finally {
      action.finish(token)
    }
  }

  const markSample = async (relPath = chapterRel, note?: string) => {
    if (!relPath) {
      push('请先选择章节', 'error')
      return
    }
    const token = action.begin('sample')
    if (!token) return
    try {
      await sampleStyle(projectId, relPath, true, note)
      if (!action.accept(token)) return
      if (note !== undefined && relPath === chapterRel) styleNoteEdited.current = false
      await loadStyle()
      if (action.accept(token)) push('已认可当前正文，写作时可参考它的文风', 'success')
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally {
      action.finish(token)
    }
  }

  const cancelSample = async (sampleId: number) => {
    const token = action.begin('cancel-sample')
    if (!token) return
    try { await cancelStyleApproval(projectId, sampleId); if (!action.accept(token)) return; await loadStyle(); if (action.accept(token)) push('已取消文风样稿认可', 'success') }
    catch (err) { if (action.accept(token)) push(errorMessage(err), 'error') }
    finally { action.finish(token) }
  }

  const handleResolveDebt = async (debtId: number, status: 'waived' | 'resolved') => {
    const token = action.begin('debt')
    if (!token) return
    try {
      await resolveDebt(debtId, status)
      if (!action.accept(token)) return
      push(status === 'waived' ? '已登记放行' : '已标记解决', 'success')
      await loadQuality()
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally {
      action.finish(token)
    }
  }

  const problems = result ? [...result.hard_gates.locations.map(item => ({title:item.reason, evidence:item.text, kind:item.gate, line:item.line})), ...result.items.filter(item => item.判定 !== '已完成').map(item => ({title:item.项, evidence:item.证据 || '尚无足够正文证据', kind:item.判定})), ...result.consistency.map(item => ({title:item.说明, evidence:item.证据, kind:item.类型}))] : []
  const gates = result?.hard_gates.gates ?? []
  const locations = result?.hard_gates.locations ?? []
  const resamplePath = (relPath: string) => chapters.find(chapter =>
    chapter.rel_path === relPath || chapter.rel_path.replace(/\.txt$/, '.md') === relPath)?.rel_path || ''
  const selectedMode = REVIEW_MODES.find(item => item.key === mode)!
  const currentSample = style.samples.find(sample => sample.rel_path === chapterRel)
  const reviewVersion = reportVersion(currentHash, result?.source_hash || result?.candidate_hash)
  const deslopVersion = reportVersion(currentHash, deslop?.candidate_hash, deslop?.source_changed)
  const ingestVersion = reportVersion(currentHash, ingest?.source_hash, ingest?.source_changed)
  const openFile = (path = chapterRel, line?: number) => navigate(`/project/${projectId}/editor?${new URLSearchParams({ path, ...(line ? {line: String(line)} : {}) })}`)

  return (
    <WorkspacePage className="review-workspace">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">审稿中心</h1>
          <p className="page-header__desc">
            对照章节合同逐项验收；硬门禁不通过即阻断，待核实一律不算通过。
          </p>
        </div>
        <button className="btn btn--sm" onClick={() => void Promise.allSettled([loadChapters(), loadQuality(), loadVersion()])}>刷新质量记录</button>
      </header>

      <WorkspaceTabs label="审稿工作区" items={[{key:'review',label:'章节审稿'}, {key:'quality',label:'质量跟踪'}, {key:'style',label:'文风档案'}]} value={tab} onChange={setTab} />
      {tab === 'review' && <><ResourceState {...chaptersResource} hasData={chaptersResource.loaded} onRetry={() => void loadChapters()}><section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">章节与操作</h2>
        </div>
        <div className="panel__body stack">
          <div className="field">
            <label className="field__label" htmlFor="review-chapter">
              选择章节
            </label>
            <select
              id="review-chapter"
              className="select"
              value={chapterRel}
              onChange={(event) => setChapterRel(event.target.value)}
            >
              {chapters.length === 0 ? <option value="">（暂无章节）</option> : null}
              {chapters.map((item) => (
                <option key={item.rel_path} value={item.rel_path}>
                  {chapterLabelWithCount(item)}
                </option>
              ))}
            </select>
            <span className="field__hint">
              当前章节：<span className="mono">{chapterRel || '未选择'}</span>
            </span>
          </div>

          <WorkspaceTabs label="本章检查模式" items={REVIEW_MODES} value={mode} onChange={setMode} />
          <div className="review-mode-intro">
            <p>{selectedMode.description}</p>
            {mode === 'ingest' && <p className="field__hint">请先将核对后的章节设为「完成」或「发表」，再提取本书追踪材料；角色状态以收件箱提案核对后写入。重复提取同一正文版本不会重复追加。</p>}
            <div className="btn-row">
              <button className="btn btn--primary" disabled={busy !== '' || !chapterRel}
                onClick={() => void (mode === 'review' ? runChapterReview() : mode === 'deslop' ? runDeslop() : runIngest())}>
                {busy === mode ? '处理中…' : selectedMode.action}
              </button>
              <button className="btn" disabled={busy !== '' || !chapterRel} onClick={() => void markSample()}>
                {busy === 'sample' ? '登记中…' : currentSample?.usable ? '更新文风样稿' : currentSample?.approved ? '重新认可当前正文' : '标记为文风样本'}
              </button>
              {currentSample?.approved && <button className="btn" disabled={busy !== ''} onClick={() => void cancelSample(currentSample.id)}>取消文风样稿认可</button>}
            </div>
          </div>
          {busy && busy !== mode && <p role="status" className="muted">另一项本章操作仍在进行，完成后可在对应模式查看结果。</p>}
        </div>
      </section>

      </ResourceState>
      {dataScope === scope && actionErrors[mode] && <p className="notice notice--warn" role="alert">{actionErrors[mode]}{(mode === 'deslop' && deslop || mode === 'ingest' && ingest) ? '；下面保留上次完成的结果。' : ''}</p>}
      {versionResource.error && <p className="notice notice--warn">正文版本暂时无法核验。{versionResource.error}</p>}
      {mode === 'deslop' && (deslop ? <SoftDeslopReport result={deslop} stale={deslopVersion === 'stale'} openFile={openFile} openInbox={() => navigate('/inbox')} />
        : <div className="empty-state"><p className="empty-state__title">检查本章表达</p><p className="empty-state__desc">点击“开始去AI味软审”，原文、建议写法和收件箱提案将在这里显示。</p></div>)}
      {mode === 'ingest' && (ingest ? <IngestionReport result={ingest} stale={ingestVersion === 'stale'} openFile={openFile} openInbox={() => navigate('/inbox')} />
        : <div className="empty-state"><p className="empty-state__title">整理本章四件套</p><p className="empty-state__desc">角色状态、时间线、资源账本和伏笔台账帮助下一章承接已发生的事实。</p></div>)}
      {mode === 'review' && (reviewStarted ? <ResourceState {...reviewResource} hasData={reviewResource.loaded && !!result} onRetry={() => void runChapterReview()}>
      {result ? (
        <>
          <section className="panel">
            <div className="panel__header">
              <h2 className="panel__title">审稿结论</h2>
              <span className={verdictTagClass(result.verdict)}>{result.verdict}</span>
            </div>
            <div className="panel__body stack">
              {reviewVersion === 'stale' && <p className="notice notice--warn">正文已变化，这份审稿结论对应旧版本。请重新审稿当前正文。</p>}
              {reviewVersion === 'unknown' && <p className="field__hint">本报告缺少可核验的正文版本，重新审稿可获得当前版本结论。</p>}
              {result.ai_error && <div className="review-diagnostic"><p className="notice notice--warn">{result.ai_error}</p><button className="btn btn--sm" disabled={busy !== ''} onClick={() => void runChapterReview()}>重试逐项审稿</button>{(result.ai_error_code || result.task_id) && <details><summary>查看诊断信息</summary><p>任务：{result.task_id || '未创建'} · 原因：{result.ai_error_code || '未分类'}</p></details>}</div>}
              <div className="row row--between">
                <span className="muted">
                  章节：<span className="mono">{result.rel_path}</span>
                </span>
                <span className="muted">
                  AI 参与：{result.ai_used ? '是' : '否'}
                </span>
              </div>

              <div className="btn-row">
                <span className="tag tag--ok">已完成 {result.counts.已完成}</span>
                <span className="tag tag--danger">未完成 {result.counts.未完成}</span>
                <span className="tag tag--warn">待核实 {result.counts.待核实}</span>
                <span className={result.hard_gates.passed ? 'tag tag--ok' : 'tag tag--danger'}>
                  硬门禁 {result.hard_gates.passed ? '通过' : '不通过'}
                </span>
                <span className="muted">
                  正文 <span className="mono">{result.hard_gates.words}</span> 字
                </span>
              </div>

              {problems.length > 0 && <section><h3>需要处理 · {problems.length} 项</h3><ol className="workspace-log-list">{problems.map((item, index) => <li key={index}><span className="tag tag--warn">{item.kind}</span><span>{item.title}</span><button className="btn btn--ghost btn--sm" onClick={() => setProblem(item)}>查看依据</button></li>)}</ol></section>}
              <div>
                <p className="muted">硬门禁明细</p>
                <table className="table">
                  <thead>
                    <tr>
                      <th>门禁</th>
                      <th>结论</th>
                      <th>详情</th>
                    </tr>
                  </thead>
                  <tbody>
                    {gates.map((gate) => (
                      <tr key={gate.key}>
                        <td>
                          {gate.key}
                          {gate.blocking ? '（阻断）' : ''}
                        </td>
                        <td className={gate.passed ? 'cell-pass' : 'cell-fail'}>
                          {gate.passed ? '通过' : '未通过'}
                        </td>
                        <td>{gate.detail}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {locations.length > 0 ? (
                <div>
                  <p className="muted">定位清单</p>
                  <table className="table">
                    <thead>
                      <tr>
                        <th>门禁</th>
                        <th>行号</th>
                        <th>文本</th>
                        <th>原因</th>
                      </tr>
                    </thead>
                    <tbody>
                      {locations.map((location, index) => (
                        <tr key={`${location.gate}-${location.line}-${index}`}>
                          <td>{location.gate}</td>
                          <td className="mono">{location.line}</td>
                          <td>{location.text}</td>
                          <td>{location.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : null}

              <div>
                <p className="muted">逐项验收</p>
                <table className="table">
                  <thead>
                    <tr>
                      <th>项</th>
                      <th>判定</th>
                      <th>证据</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.items.map((item, index) => (
                      <tr key={`${item.项}-${index}`}>
                        <td>{item.项}</td>
                        <td>
                          <span className={itemTagClass(item.判定)}>{item.判定}</span>
                        </td>
                        <td>{item.证据 || '（无正文证据）'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {result.counts.待核实 > 0 ? (
                  <p className="muted">待核实不算通过，需补足依据后复核。</p>
                ) : null}
              </div>

              <div>
                <p className="muted">一致性检查</p>
                {result.consistency.length === 0 ? (
                  <p className="muted">{result.ai_used ? '本次未列出一致性问题。' : 'AI 一致性检查尚未完成。'}</p>
                ) : (
                  <ul className="notice__list">
                    {result.consistency.map((item, index) => (
                      <li key={`${item.类型}-${index}`}>
                        <span className="tag tag--warn">{item.类型}</span> {item.说明}
                        {item.证据 ? <span className="muted">（依据：{item.证据}）</span> : null}
                      </li>
                    ))}
                  </ul>
                )}
              </div>

              <div>
                <p className="muted">修改指令</p>
                {result.revise_instructions.length === 0 ? (
                  <p className="muted">未提出合同或连续性修改要求。</p>
                ) : (
                  <ol className="notice__list">
                    {result.revise_instructions.map((instruction, index) => (
                      <li key={`${instruction}-${index}`}>{instruction}</li>
                    ))}
                  </ol>
                )}
              </div>
            </div>
          </section>
          <ProseQualityReport quality={result.prose_quality} modelSuggestions={result.prose_review} historyQuality={result.prose_history} stale={reviewVersion === 'stale'} />
        </>
      ) : (
        <div className="empty-state">
          <p className="empty-state__title">尚未审稿</p>
          <p className="empty-state__desc">选择章节后点击「逐项审稿」，结果会在此逐项展示。</p>
        </div>
      )}

      </ResourceState> : <div className="empty-state"><p className="empty-state__title">准备验收本章</p><p className="empty-state__desc">选择章节后开始逐项审稿，问题与原文依据会在这里显示。</p></div>)}</>}
      {tab === 'quality' && <><ResourceState {...matrixResource} hasData={matrixResource.loaded} onRetry={() => void loadMatrix()}><section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">质检进度表</h2>
          <span className="muted">未通过项可点击直达</span>
        </div>
        <div className="panel__body panel__body--tight">
          {matrix && matrix.chapters.length > 0 ? (
            <div className="matrix-scroll">
              <table className="table">
                <thead>
                  <tr>
                    <th>章节</th>
                    {matrix.columns.map((column) => (
                      <th key={column}>{column}</th>
                    ))}
                    <th>质量债</th>
                  </tr>
                </thead>
                <tbody>
                  {matrix.chapters.map((chapter) => (
                    <tr key={chapter.rel_path}>
                      <td>
                        {chapter.number === null ? '' : `${chapter.number}. `}
                        {chapter.title || chapter.rel_path}
                        <span className="muted">（{chapter.word_count} 字）</span>
                      </td>
                      {matrix.columns.map((column) => {
                        const value = chapter.cells[column] ?? '未跑'
                        return (
                          <td key={column} className={cellClass(value)}>
                            {value === '未通过' ? (
                              <button
                                className="btn btn--ghost btn--sm"
                                type="button"
                                onClick={() => navigate(`/project/${projectId}/editor?${new URLSearchParams({path: chapter.rel_path})}`)}
                                title="跳转到编辑器处理该章节"
                              >
                                {value}
                              </button>
                            ) : (
                              value
                            )}
                          </td>
                        )
                      })}
                      <td>
                        {chapter.debt.length === 0 ? (
                          <span className="muted">—</span>
                        ) : (
                          chapter.debt.map((item, index) => (
                            <span key={`${item.level}-${index}`} className={debtTagClass(item.level)}>
                              {item.level}
                            </span>
                          ))
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="empty-state">
              <p className="empty-state__title">暂无章节</p>
              <p className="empty-state__desc">该项目下还没有可质检的章节。</p>
            </div>
          )}
        </div>
      </section>

      </ResourceState><ResourceState {...debtsResource} hasData={debtsResource.loaded} onRetry={() => void loadDebts()}><section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">质量债</h2>
        </div>
        <div className="panel__body stack">
          <ul className="notice__list">
            {DEBT_LEVELS.map((entry) => (
              <li key={entry.level}>
                <span className={debtTagClass(entry.level)}>{entry.level}</span> {entry.desc}
              </li>
            ))}
          </ul>
          {debts.length === 0 ? (
            <p className="muted">当前没有未结清的质量债。</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>等级</th>
                  <th>章节</th>
                  <th>说明</th>
                  <th>时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {debts.map((debt) => (
                  <tr key={debt.id}>
                    <td>
                      <span className={debtTagClass(debt.level)} title={debtLevelDesc(debt.level)}>
                        {debt.level}
                      </span>
                    </td>
                    <td className="mono">{debt.rel_path}</td>
                    <td>{debt.note}</td>
                    <td className="mono">{debt.created_at}</td>
                    <td>
                      <div className="btn-row">
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          onClick={() => void handleResolveDebt(debt.id, 'waived')}
                        >
                          登记放行
                        </button>
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          onClick={() => void handleResolveDebt(debt.id, 'resolved')}
                        >
                          已解决
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      </ResourceState></>}
      {tab === 'style' && <ResourceState {...styleResource} hasData={styleResource.loaded} onRetry={() => void loadStyle()}><section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">作者认可的文风样稿</h2>
        </div>
        <div className="panel__body stack">
          <p className="muted">选择你自己写过或已经认可的章节。样稿只用于学习文风，其中的剧情不是新章事实。正文修改后，需要重新采样认可。</p>
          <div className="field">
            <label className="field__label" htmlFor="style-chapter">样稿章节</label>
            <select id="style-chapter" className="select" disabled={busy !== '' || chapters.length === 0}
              value={chapterRel} onChange={event => setChapterRel(event.target.value)}>
              {chapters.length === 0 && <option value="">暂无章节</option>}
              {chapters.map(chapter => <option key={chapter.rel_path} value={chapter.rel_path}>{chapterLabelWithCount(chapter)}</option>)}
            </select>
          </div>
          <div className="field">
            <label className="field__label" htmlFor="style-note">你希望保留的写法（可选）</label>
            <textarea id="style-note" className="textarea" rows={3} maxLength={1000} value={styleNote}
              disabled={busy !== ''} onChange={event => { styleNoteEdited.current = true; setStyleNote(event.target.value) }}
              placeholder="例如：对白留一点没说破的信息，情绪落在人物动作上。" />
            <span className="field__hint">备注用于说明文风偏好，不会改变人物设定或章节要求。</span>
          </div>
          <div className="btn-row"><button type="button" className="btn btn--primary" disabled={busy !== '' || !chapterRel}
            onClick={() => void markSample(chapterRel, styleNote)}>{busy === 'sample' ? '登记中…' : '采样并认可当前正文'}</button>
            {currentSample?.approved && <button className="btn" disabled={busy !== ''} onClick={() => void cancelSample(currentSample.id)}>取消文风样稿认可</button>}</div>
          {style.samples.length === 0 ? (
            <p className="muted">尚无文风样稿，认可章节后，写作助手会选取少量相关片段作参照。</p>
          ) : (
            <div className="review-table-scroll"><table className="table">
              <thead>
                <tr>
                  <th>章节</th>
                  <th>写作参照状态</th>
                  <th>作者备注</th>
                  <th>平均句长</th>
                  <th>句长变异</th>
                  <th>对话占比</th>
                  <th>破折号密度</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {style.samples.map((sample) => (
                  <tr key={sample.id}>
                    <td className="mono">{sample.rel_path}</td>
                    <td>
                      <span className={sample.usable ? 'tag tag--ok' : 'tag tag--warn'}>
                        {sample.usable ? '可用于写作' : sample.approved ? '需重新认可' : '未认可'}
                      </span>
                      {sample.reference_reason && <p className="field__hint">{sample.reference_reason}</p>}
                    </td>
                    <td>{sample.note || '—'}</td>
                    <td className="mono">{formatMetric(sample.sentence_len_mean)}</td>
                    <td className="mono">{formatMetric(sample.sentence_len_cv)}</td>
                    <td className="mono">{formatMetric(sample.dialog_ratio, 3)}</td>
                    <td className="mono">{formatMetric(sample.dash_per_1000)}</td>
                    <td><div className="btn-row">{!sample.usable && <button type="button" className="btn btn--sm"
                      disabled={busy !== '' || !resamplePath(sample.rel_path)}
                      onClick={() => void markSample(resamplePath(sample.rel_path))}>重新采样并认可</button>}
                      {sample.approved && <button className="btn btn--sm" disabled={busy !== ''} onClick={() => void cancelSample(sample.id)}>取消认可</button>}</div></td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}

          {style.reference ? (
            <div className="stack stack--tight">
              <p className="muted">有效样稿的统计参考（不作为写作硬指标）</p>
              <dl className="review-statistics">
                <div><dt>有效样稿</dt><dd>{style.reference.samples ?? style.samples.filter(sample => sample.usable).length} 章</dd></div>
                <div><dt>平均句长</dt><dd>{formatMetric(style.reference.sentence_len_mean)} 字</dd></div>
                <div><dt>句长变异</dt><dd>{formatMetric(style.reference.sentence_len_cv)}</dd></div>
                <div><dt>对话占比</dt><dd>{formatMetric(style.reference.dialog_ratio * 100, 1)}%</dd></div>
                <div><dt>破折号密度</dt><dd>{formatMetric(style.reference.dash_per_1000)} / 千字</dd></div>
                <div><dt>省略号密度</dt><dd>{formatMetric(style.reference.ellipsis_per_1000)} / 千字</dd></div>
              </dl>
              {style.reference.top_bigrams?.length > 0 && <p className="muted">常见双字词：{style.reference.top_bigrams.map(item => `${item.word}（${item.count} 次）`).join('、')}</p>}
            </div>
          ) : (
            <p className="muted">暂无聚合参考指标。</p>
          )}
        </div>
      </section>

      </ResourceState>}
      <Drawer title={problem?.title || '审稿依据'} open={dataScope === scope && !!problem} onClose={() => setProblem(null)}>{problem && <div className="stack"><span className="tag tag--warn">{problem.kind}</span><p>{problem.evidence}</p>{problem.line && <p className="muted">第 {problem.line} 行</p>}{reviewVersion === 'stale' && <p className="notice notice--warn">该依据对应旧正文，请先重新审稿。</p>}<button className="btn btn--primary" disabled={reviewVersion === 'stale'} onClick={() => { openFile(result?.rel_path || chapterRel, problem.line); setProblem(null) }}>定位章节原文</button></div>}</Drawer>
      <p className="page-footnote">
        审稿只报告、不自动改稿；硬门禁不通过时不会写入正文。
      </p>
    </WorkspacePage>
  )
}
