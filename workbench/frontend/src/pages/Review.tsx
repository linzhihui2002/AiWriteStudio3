/** 审稿中心 —— 逐项审稿 / 硬门禁定位 / 质检矩阵 / 质量债 / 文风指纹。 */

import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  ingestChapter,
  listChapters,
  listDebts,
  listStyleFingerprints,
  qualityMatrix,
  resolveDebt,
  reviewChapter,
  sampleStyle,
  softDeslop,
} from '../api/client'
import type { ChapterSummary, QualityMatrix, ReviewResult, StyleFingerprint } from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import { chapterLabelWithCount } from '../lib/chapterName'

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
  reference: Record<string, unknown> | null
}

interface DeslopSummary {
  suggestions: number
  proposalIds: number[]
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
  if (typeof value === 'number') return value.toFixed(digits)
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
  const [result, setResult] = useState<ReviewResult | null>(null)
  const [matrix, setMatrix] = useState<QualityMatrix | null>(null)
  const [debts, setDebts] = useState<DebtItem[]>([])
  const [style, setStyle] = useState<StylePayload>({ samples: [], reference: null })
  const [deslop, setDeslop] = useState<DeslopSummary | null>(null)
  const [ingest, setIngest] = useState<Record<string, unknown> | null>(null)
  const [busy, setBusy] = useState('')

  const loadQuality = useCallback(async () => {
    try {
      setMatrix(await qualityMatrix(projectId))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
    try {
      setDebts(await listDebts(projectId))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
    try {
      setStyle(await listStyleFingerprints(projectId))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, push])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const list = await listChapters(projectId)
        if (!alive) return
        setChapters(list)
        setChapterRel((current) => current || (list[0]?.rel_path ?? ''))
      } catch (err) {
        if (alive) push(errorMessage(err), 'error')
      }
    }
    void load()
    void loadQuality()
    return () => {
      alive = false
    }
  }, [projectId, push, loadQuality])

  const runChapterReview = async () => {
    if (!chapterRel) {
      push('请先选择章节', 'error')
      return
    }
    setBusy('review')
    try {
      const data = await reviewChapter(projectId, chapterRel)
      setResult(data)
      push(`逐项审稿完成：${data.verdict}`, 'success')
      await loadQuality()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const runDeslop = async () => {
    if (!chapterRel) {
      push('请先选择章节', 'error')
      return
    }
    setBusy('deslop')
    try {
      const data = await softDeslop(projectId, chapterRel)
      setDeslop({ suggestions: data.suggestions.length, proposalIds: data.proposal_ids })
      push(`去AI味软审完成：生成 ${data.proposal_ids.length} 条收件箱提案`, 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const runIngest = async () => {
    if (!chapterRel) {
      push('请先选择章节', 'error')
      return
    }
    setBusy('ingest')
    try {
      const data = await ingestChapter(projectId, chapterRel)
      setIngest(data)
      push('已摄取四件套', 'success')
      await loadQuality()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const markSample = async () => {
    if (!chapterRel) {
      push('请先选择章节', 'error')
      return
    }
    setBusy('sample')
    try {
      await sampleStyle(projectId, chapterRel)
      try {
        setStyle(await listStyleFingerprints(projectId))
      } catch (err) {
        push(errorMessage(err), 'error')
      }
      push('已标记为文风样本', 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const handleResolveDebt = async (debtId: number, status: 'waived' | 'resolved') => {
    try {
      await resolveDebt(debtId, status)
      push(status === 'waived' ? '已登记放行' : '已标记解决', 'success')
      await loadQuality()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const gates = result?.hard_gates.gates ?? []
  const locations = result?.hard_gates.locations ?? []

  return (
    <div className="page page--wide">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">审稿中心</h1>
          <p className="page-header__desc">
            对照章节合同逐项验收；硬门禁不通过即阻断，待核实一律不算通过。
          </p>
        </div>
        <button
          className="btn"
          type="button"
          onClick={() => navigate(`/project/${projectId}/editor`)}
        >
          前往编辑器
        </button>
      </header>

      <section className="panel">
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

          <div className="btn-row">
            <button
              className="btn btn--primary"
              type="button"
              disabled={busy !== '' || !chapterRel}
              onClick={() => void runChapterReview()}
            >
              {busy === 'review' ? '审稿中…' : '逐项审稿'}
            </button>
            <button
              className="btn"
              type="button"
              disabled={busy !== '' || !chapterRel}
              onClick={() => void runDeslop()}
            >
              {busy === 'deslop' ? '软审中…' : '去AI味软审'}
            </button>
            <button
              className="btn"
              type="button"
              disabled={busy !== '' || !chapterRel}
              onClick={() => void runIngest()}
            >
              {busy === 'ingest' ? '摄取中…' : '摄取四件套'}
            </button>
            <button
              className="btn"
              type="button"
              disabled={busy !== '' || !chapterRel}
              onClick={() => void markSample()}
            >
              {busy === 'sample' ? '登记中…' : '标记为文风样本'}
            </button>
          </div>

          {deslop ? (
            <p className="muted">
              去AI味软审：<span className="mono">{deslop.suggestions}</span> 条建议已进入收件箱，提案数{' '}
              <span className="mono">{deslop.proposalIds.length}</span>。
            </p>
          ) : null}

          {ingest ? (
            <div className="stack stack--tight">
              <p className="muted">摄取四件套返回计数：</p>
              <ul className="notice__list">
                {Object.entries(ingest).map(([key, value]) => (
                  <li key={key}>
                    <span className="mono">{key}</span>：
                    {value !== null && typeof value === 'object'
                      ? JSON.stringify(value)
                      : String(value)}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </section>

      {result ? (
        <>
          <section className="panel">
            <div className="panel__header">
              <h2 className="panel__title">审稿结论</h2>
              <span className={verdictTagClass(result.verdict)}>{result.verdict}</span>
            </div>
            <div className="panel__body stack">
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
                  <p className="muted">待核实不算通过，需人工复核。</p>
                ) : null}
              </div>

              <div>
                <p className="muted">一致性检查</p>
                {result.consistency.length === 0 ? (
                  <p className="muted">未发现一致性问题。</p>
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
                  <p className="muted">无需修改。</p>
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
        </>
      ) : (
        <div className="empty-state">
          <p className="empty-state__title">尚未审稿</p>
          <p className="empty-state__desc">选择章节后点击「逐项审稿」，结果会在此逐项展示。</p>
        </div>
      )}

      <section className="panel">
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
                                onClick={() => navigate(`/project/${projectId}/editor`)}
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

      <section className="panel">
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

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">文风指纹</h2>
        </div>
        <div className="panel__body stack">
          {style.samples.length === 0 ? (
            <p className="muted">尚无文风样本，可先在章节上「标记为文风样本」。</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>章节</th>
                  <th>是否认可</th>
                  <th>平均句长</th>
                  <th>句长变异</th>
                  <th>对话占比</th>
                  <th>破折号密度</th>
                </tr>
              </thead>
              <tbody>
                {style.samples.map((sample) => (
                  <tr key={sample.id}>
                    <td className="mono">{sample.rel_path}</td>
                    <td>
                      <span className={sample.approved ? 'tag tag--ok' : 'tag tag--warn'}>
                        {sample.approved ? '认可' : '未认可'}
                      </span>
                    </td>
                    <td className="mono">{formatMetric(sample.sentence_len_mean)}</td>
                    <td className="mono">{formatMetric(sample.sentence_len_cv)}</td>
                    <td className="mono">{formatMetric(sample.dialog_ratio, 3)}</td>
                    <td className="mono">{formatMetric(sample.dash_per_1000)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {style.reference ? (
            <div className="stack stack--tight">
              <p className="muted">聚合参考指标</p>
              <ul className="notice__list">
                {Object.entries(style.reference).map(([key, value]) => (
                  <li key={key}>
                    <span className="mono">{key}</span>：{formatMetric(value)}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <p className="muted">暂无聚合参考指标。</p>
          )}
        </div>
      </section>

      <p className="page-footnote">
        审稿只报告、不自动改稿；硬门禁不通过时不会写入正文。
      </p>
    </div>
  )
}