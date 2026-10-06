import { useEffect, useRef, useState } from 'react'
import type { ChatInteraction, ChatInteractionResponse, ChatPermissionMode } from '../api/types'
import { approvalReason, formatQuestionResponse, questionDraft, questionResponse, type ChatQuestionDraft } from '../lib/chatState'
import DiffView from './DiffView'

export default function ChatInteractionCard({ interaction, runPermission, onRespond }: {
  interaction: ChatInteraction
  runPermission?: ChatPermissionMode
  onRespond: (interaction: ChatInteraction, response: ChatInteractionResponse) => Promise<void>
}) {
  const storageKey = `aiw.chat.question.${interaction.run_id}.${interaction.id}`
  const [answers, setAnswers] = useState<Record<string, ChatQuestionDraft>>(() => {
    try {
      const stored = JSON.parse(localStorage.getItem(storageKey) || '{}')
      return Object.fromEntries(Object.entries(stored).filter(([, value]) => value && typeof value === 'object').map(([id, value]) => {
        const answer = value as ChatQuestionDraft
        return [id, { selected: Array.isArray(answer.selected) ? answer.selected.filter((item) => typeof item === 'string') : [], other: answer.other === true, custom: typeof answer.custom === 'string' ? answer.custom : '' }]
      }))
    } catch { return {} }
  })
  useEffect(() => { try { localStorage.setItem(storageKey, JSON.stringify(answers)) } catch { /* retain in memory */ } }, [storageKey, answers])
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const [unconfirmed, setUnconfirmed] = useState<ChatInteractionResponse | null>(null)
  const lock = useRef(false)
  const pending = interaction.status === 'pending'
  const questions = interaction.payload.questions || []
  const stateFor = (id: string) => questionDraft(questions.find((question) => question.id === id)!, answers)
  const submit = async (response: ChatInteractionResponse) => {
    if (lock.current || !pending) return
    lock.current = true
    setSubmitting(true)
    setError('')
    try { await onRespond(interaction, response); setUnconfirmed(null) }
    catch (err) {
      setError(err instanceof Error ? err.message : '提交失败，请重试')
      if (err && typeof err === 'object' && 'code' in err && err.code === 'INTERACTION_UNCONFIRMED') setUnconfirmed(response)
      else setUnconfirmed(null)
    }
    finally { lock.current = false; setSubmitting(false) }
  }
  const response = questionResponse(questions, answers)
  const blocked = submitting || Boolean(unconfirmed)
  const status = { pending: '等待你回答', answered: '已回答', cancelled: '已取消', interrupted: '已中断', expired: '已失效' }[interaction.status]
  const operation = ({ create: '新建', rewrite: '整篇覆盖', replace_exact: '局部修改', write: '修改', edit: '修改', delete: '删除', move: '移动', rename: '重命名' } as Record<string, string>)[interaction.payload.operation || ''] || interaction.payload.operation || '修改'
  const responseText = formatQuestionResponse(interaction)
  // 写域越界：批准卡上标注本轮范围与目标材料，并让作者选择授权粒度。
  const scope = interaction.payload.scope
  const outOfScope = Boolean(scope?.out_of_scope)
  const scopeOptions = scope?.options?.length ? scope.options : [{ id: 'once', label: '仅此次批准' }, { id: 'material', label: '本任务内允许改这类材料' }]
  const responded = interaction.response && 'decision' in interaction.response ? interaction.response : null
  const decisionText = responded ? (responded.decision === 'approve'
    ? (responded.scope === 'task' ? '你已批准这次操作，本任务内不再询问'
      : responded.scope === 'material' ? '你已批准，本任务内允许改这类材料'
      : responded.scope === 'once' ? '你已批准这次操作（仅此次）' : '你已批准这次操作')
    : '你已拒绝这次操作') : ''
  if (!pending) return <details className={`chat__interaction chat__interaction--${interaction.kind}`}><summary>{interaction.kind === 'approval' ? `${operation} · ${interaction.payload.path || '文件操作'}` : '作者回应'} · {status}</summary><p className="chat__answered">{decisionText || responseText || '本次交互已结束。'}</p></details>
  return <section className={`chat__interaction chat__interaction--${interaction.kind}`} aria-label={interaction.kind === 'approval' ? '文件操作批准' : '待回答问题'}>
    <div className="chat__interaction-heading"><strong>{interaction.kind === 'approval' ? '文件操作需要批准' : '请你选择'}</strong><span>{pending && interaction.kind === 'approval' ? '等待你批准' : status}</span></div>
    {interaction.kind === 'approval' ? <>
      <p className="chat__approval-path">{operation} · {interaction.payload.path}{interaction.payload.destination ? ` → ${interaction.payload.destination}` : ''}</p>
      <p className="chat__approval-reason">{approvalReason(interaction, runPermission)}</p>
      {outOfScope ? <p className="chat__approval-reason"><strong>越界</strong>{scope?.label ? `　${scope.label}` : ''}{scope?.material ? `　目标材料：${scope.material}/` : ''}</p> : null}
      <details className="chat__approval-diff"><summary>查看具体改动</summary>
        {Array.isArray(interaction.payload.diff) ? <DiffView lines={interaction.payload.diff} /> : typeof interaction.payload.diff === 'string' ? <pre>{interaction.payload.diff}</pre> : <div className="chat__diff-fallback"><div><strong>修改前</strong><pre>{interaction.payload.before_content ?? '文件不存在'}</pre></div><div><strong>修改后</strong><pre>{interaction.payload.after_content ?? '文件已移除'}</pre></div></div>}
      </details>
      {pending ? <div className="chat__interaction-actions">{outOfScope ? <>
        {scopeOptions.map((option) => <button type="button" className="btn btn--primary btn--sm" key={option.id} disabled={blocked} onClick={() => void submit({ decision: 'approve', scope: option.id as 'once' | 'material' })}>{submitting ? '提交中…' : option.label}</button>)}
        <button type="button" className="btn btn--ghost btn--sm" disabled={blocked} onClick={() => void submit({ decision: 'reject' })}>拒绝</button>
      </> : <>
        <button type="button" className="btn btn--primary btn--sm" disabled={blocked} onClick={() => void submit({ decision: 'approve' })}>{submitting ? '提交中…' : '批准这次操作'}</button>
        <button type="button" className="btn btn--sm" disabled={blocked} onClick={() => void submit({ decision: 'approve', scope: 'task' })} title="批准本次操作，本次任务内后续修改不再询问">本任务内不再询问</button>
        <button type="button" className="btn btn--ghost btn--sm" disabled={blocked} onClick={() => void submit({ decision: 'reject' })}>拒绝</button>
      </>}</div> : decisionText ? <p className="muted">{decisionText}</p> : null}
    </> : pending ? <form noValidate onSubmit={(event) => {
      event.preventDefault()
      if (response) void submit(response)
    }}>
      {questions.map((question) => {
        const answer = stateFor(question.id)
        const update = (patch: Partial<typeof answer>) => setAnswers((items) => ({ ...items, [question.id]: { ...stateFor(question.id), ...patch } }))
        return <fieldset key={question.id} disabled={blocked} className="chat__question"><legend>{question.header ? <span className="chat__question-header">{question.header}</span> : null}{question.question}{question.multi_select ? <small>（可多选）</small> : null}</legend>
          {(question.options || []).map((option, index) => {
            const id = option.id || option.label
            return <label className={`chat__option${answer.selected.includes(id) ? ' is-selected' : ''}`} key={`${id}-${index}`}><input type={question.multi_select ? 'checkbox' : 'radio'} name={`question-${interaction.id}-${question.id}`} checked={answer.selected.includes(id)} onChange={(event) => update({ selected: question.multi_select ? event.target.checked ? [...answer.selected, id] : answer.selected.filter((value) => value !== id) : [id], ...(question.multi_select ? {} : { other: false }) })} /><span><strong>{option.label}</strong>{option.description ? <small>{option.description}</small> : null}</span></label>
          })}
          <label className={`chat__option${answer.other ? ' is-selected' : ''}`}><input type={question.multi_select ? 'checkbox' : 'radio'} name={`question-${interaction.id}-${question.id}`} checked={answer.other} onChange={(event) => update({ other: event.target.checked, ...(question.multi_select ? {} : { selected: [] }) })} /><span><strong>其他</strong><small>用自己的话回答</small></span></label>
          {answer.other ? <textarea className="textarea chat__custom-answer" aria-label={`${question.header || question.question}：其他回答`} placeholder="填写你的答案" value={answer.custom} onChange={(event) => update({ custom: event.target.value })} aria-required="true" /> : null}
        </fieldset>
      })}
      <button type="submit" className="btn btn--primary btn--sm" disabled={!response || blocked}>{submitting ? '提交中…' : '提交回答并继续'}</button>
    </form> : <p className="chat__answered">{responseText || '本次问题已结束。'}</p>}
    {error ? <p className="chat__error" role="alert">{error}{unconfirmed ? <button type="button" className="btn btn--sm" disabled={submitting} onClick={() => void submit(unconfirmed)}>核对提交结果</button> : null}</p> : null}
  </section>
}
