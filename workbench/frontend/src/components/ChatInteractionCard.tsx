import { useRef, useState } from 'react'
import type { ChatInteraction, ChatInteractionResponse, ChatPermissionMode } from '../api/types'
import { approvalReason, formatQuestionResponse, questionDraft, questionResponse, type ChatQuestionDraft } from '../lib/chatState'
import DiffView from './DiffView'

export default function ChatInteractionCard({ interaction, runPermission, onRespond }: {
  interaction: ChatInteraction
  runPermission?: ChatPermissionMode
  onRespond: (interaction: ChatInteraction, response: ChatInteractionResponse) => Promise<void>
}) {
  const [answers, setAnswers] = useState<Record<string, ChatQuestionDraft>>({})
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const lock = useRef(false)
  const pending = interaction.status === 'pending'
  const questions = interaction.payload.questions || []
  const stateFor = (id: string) => questionDraft(questions.find((question) => question.id === id)!, answers)
  const submit = async (response: ChatInteractionResponse) => {
    if (lock.current || !pending) return
    lock.current = true
    setSubmitting(true)
    setError('')
    try { await onRespond(interaction, response) }
    catch (err) { setError(err instanceof Error ? err.message : '提交失败，请重试') }
    finally { lock.current = false; setSubmitting(false) }
  }
  const response = questionResponse(questions, answers)
  const status = { pending: '等待你回答', answered: '已回答', cancelled: '已取消', interrupted: '已中断', expired: '已失效' }[interaction.status]
  const operation = ({ create: '新建', rewrite: '整篇覆盖', replace_exact: '局部修改', write: '修改', edit: '修改', delete: '删除', move: '移动', rename: '重命名' } as Record<string, string>)[interaction.payload.operation || ''] || interaction.payload.operation || '修改'
  const responseText = formatQuestionResponse(interaction)
  return <section className={`chat__interaction chat__interaction--${interaction.kind}`} aria-label={interaction.kind === 'approval' ? '文件操作批准' : '待回答问题'}>
    <div className="chat__interaction-heading"><strong>{interaction.kind === 'approval' ? '文件操作需要批准' : '请你选择'}</strong><span>{pending && interaction.kind === 'approval' ? '等待你批准' : status}</span></div>
    {interaction.kind === 'approval' ? <>
      <p className="chat__approval-path">{operation} · {interaction.payload.path}{interaction.payload.destination ? ` → ${interaction.payload.destination}` : ''}</p>
      <p className="chat__approval-reason">{approvalReason(interaction, runPermission)}</p>
      <details className="chat__approval-diff"><summary>查看具体改动</summary>
        {Array.isArray(interaction.payload.diff) ? <DiffView lines={interaction.payload.diff} /> : typeof interaction.payload.diff === 'string' ? <pre>{interaction.payload.diff}</pre> : <div className="chat__diff-fallback"><div><strong>修改前</strong><pre>{interaction.payload.before_content ?? '文件不存在'}</pre></div><div><strong>修改后</strong><pre>{interaction.payload.after_content ?? '文件已移除'}</pre></div></div>}
      </details>
      {pending ? <div className="chat__interaction-actions"><button type="button" className="btn btn--primary btn--sm" disabled={submitting} onClick={() => void submit({ decision: 'approve' })}>{submitting ? '提交中…' : '批准这次操作'}</button><button type="button" className="btn btn--ghost btn--sm" disabled={submitting} onClick={() => void submit({ decision: 'reject' })}>拒绝</button></div> : interaction.response && 'decision' in interaction.response ? <p className="muted">{interaction.response.decision === 'approve' ? '你已批准这次操作' : '你已拒绝这次操作'}</p> : null}
    </> : pending ? <form onSubmit={(event) => {
      event.preventDefault()
      if (response) void submit(response)
    }}>
      {questions.map((question) => {
        const answer = stateFor(question.id)
        const update = (patch: Partial<typeof answer>) => setAnswers((items) => ({ ...items, [question.id]: { ...stateFor(question.id), ...patch } }))
        return <fieldset key={question.id} disabled={submitting} className="chat__question"><legend>{question.header ? <span className="chat__question-header">{question.header}</span> : null}{question.question}{question.multi_select ? <small>（可多选）</small> : null}</legend>
          {(question.options || []).map((option, index) => {
            const id = option.id || option.label
            return <label className={`chat__option${answer.selected.includes(id) ? ' is-selected' : ''}`} key={`${id}-${index}`}><input type={question.multi_select ? 'checkbox' : 'radio'} name={`question-${interaction.id}-${question.id}`} checked={answer.selected.includes(id)} onChange={(event) => update({ selected: question.multi_select ? event.target.checked ? [...answer.selected, id] : answer.selected.filter((value) => value !== id) : [id], ...(question.multi_select ? {} : { other: false }) })} /><span><strong>{option.label}</strong>{option.description ? <small>{option.description}</small> : null}</span></label>
          })}
          <label className={`chat__option${answer.other ? ' is-selected' : ''}`}><input type={question.multi_select ? 'checkbox' : 'radio'} name={`question-${interaction.id}-${question.id}`} checked={answer.other} onChange={(event) => update({ other: event.target.checked, ...(question.multi_select ? {} : { selected: [] }) })} /><span><strong>其他</strong><small>用自己的话回答</small></span></label>
          {answer.other ? <textarea className="textarea chat__custom-answer" aria-label={`${question.header || question.question}：其他回答`} placeholder="填写你的答案" value={answer.custom} onChange={(event) => update({ custom: event.target.value })} required /> : null}
        </fieldset>
      })}
      <button type="submit" className="btn btn--primary btn--sm" disabled={!response || submitting}>{submitting ? '提交中…' : '提交回答并继续'}</button>
    </form> : <p className="chat__answered">{responseText || '本次问题已结束。'}</p>}
    {error ? <p className="chat__error" role="alert">{error}</p> : null}
  </section>
}
