/** 大纲规划：灵感追问 → 候选 → 锁定 → 冻结 → 滚动规划（只细化近 2 卷）。 */

import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  expandOutline,
  freezeOutline,
  lockOutline,
  outlineCandidates,
  outlineHistory,
  readOutline,
  rollingPlan,
  saveOutline,
} from '../api/client'
import type { OutlineState } from '../api/types'
import MarkdownView from '../components/MarkdownView'
import Modal from '../components/Modal'
import { useConfirm } from '../components/ConfirmDialog'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

export default function Outline() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const toast = useToast()

  const [state, setState] = useState<OutlineState | null>(null)
  const [content, setContent] = useState('')
  const [idea, setIdea] = useState('')
  const [busy, setBusy] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [history, setHistory] = useState<Array<Record<string, unknown>>>([])
  const [rolling, setRolling] = useState<{ refined: string[]; suggestion: string; note: string } | null>(null)
  const [confirm, confirmNode] = useConfirm()

  const load = useCallback(async () => {
    try {
      const data = await readOutline(projectId)
      setState(data)
      setContent(data.content)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }, [projectId, toast])

  useEffect(() => {
    void load()
  }, [load])

  const onSave = async () => {
    if (!state) return
    setBusy(true)
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
      await saveOutline(projectId, content, frozen)
      toast.push('大纲已保存', 'success')
      await load()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onExpand = async () => {
    setBusy(true)
    try {
      const result = await expandOutline(projectId, idea, true)
      setIdea('')
      await load()
      toast.push(`已生成 ${result.questions.length} 个待拍板问题`, 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onCandidates = async () => {
    setBusy(true)
    try {
      const result = await outlineCandidates(projectId, idea, 3, true)
      await load()
      toast.push(`已生成 ${result.candidates.length} 个候选，请锁定其一`, 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onLock = async (index: number) => {
    try {
      await lockOutline(projectId, index)
      toast.push('已锁定为正式大纲', 'success')
      await load()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onFreeze = async () => {
    try {
      await freezeOutline(projectId)
      toast.push('大纲已冻结（章节生产的 CHECK 门控已通过）', 'success')
      await load()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onRolling = async () => {
    setBusy(true)
    try {
      const result = await rollingPlan(projectId, true)
      setRolling(result)
      toast.push(`已细化近 ${result.refined.length} 卷，写入 大纲/章纲.md`, 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page page--wide">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">大纲规划</h1>
          <p className="page-header__desc">
            {state?.project_name ?? ''} · 冻结后修改走差异确认
          </p>
        </div>
        <div className="btn-row">
          <Link className="btn btn--sm" to={`/project/${projectId}/editor`}>
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
                setHistory(await outlineHistory(projectId))
                setHistoryOpen(true)
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
          <button className="btn btn--sm" type="button" disabled={!state || state.frozen} onClick={() => void onFreeze()}>
            冻结大纲
          </button>
          <button className="btn btn--primary btn--sm" type="button" disabled={busy} onClick={() => void onSave()}>
            保存
          </button>
        </div>
      </header>

      <div className="row" style={{ marginBottom: 'var(--space-3)' }}>
        <span className={state?.frozen ? 'tag tag--ok' : 'tag tag--warn'}>
          {state?.frozen ? '已冻结' : '未冻结'}
        </span>
        {state?.locked ? <span className="tag">锁定候选：{state.locked.title}</span> : null}
        <span className="tag">历史 {state?.history_count ?? 0} 次</span>
      </div>

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
            onChange={(event) => setIdea(event.target.value)}
          />
          <div className="btn-row">
            <button className="btn btn--sm" type="button" disabled={busy} onClick={() => void onExpand()}>
              追问关键问题
            </button>
            <button className="btn btn--sm" type="button" disabled={busy} onClick={() => void onCandidates()}>
              生成 2-3 个候选
            </button>
          </div>
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
            <h2 className="panel__title">大纲候选</h2>
            <span className="muted">确认锁定后才写入 大纲/大纲.md</span>
          </header>
          <div className="panel__body">
            <div className="card-grid">
              {state.candidates.map((candidate, index) => (
                <article key={index} className="asset-card">
                  <div className="asset-card__head">
                    <span className="asset-card__name">{candidate.title}</span>
                    <span className="tag">{candidate.source === 'model' ? '模型生成' : '占位'}</span>
                  </div>
                  <div style={{ maxHeight: '240px', overflow: 'auto' }}>
                    <MarkdownView text={candidate.content} />
                  </div>
                  <div className="btn-row">
                    <button className="btn btn--primary btn--sm" type="button" onClick={() => void onLock(index)}>
                      锁定这个候选
                    </button>
                  </div>
                </article>
              ))}
            </div>
          </div>
        </section>
      ) : null}

      <section className="panel" style={{ marginTop: 'var(--space-4)' }}>
        <header className="panel__header">
          <h2 className="panel__title">正式大纲</h2>
          <span className="muted">自由书写，保存自动留档</span>
        </header>
        <div className="panel__body stack">
          <textarea
            autoComplete={NO_AUTOFILL}
            className="textarea"
            style={{ minHeight: '320px' }}
            value={content}
            onChange={(event) => setContent(event.target.value)}
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

      <Modal title="大纲修改历史" open={historyOpen} onClose={() => setHistoryOpen(false)} width={860}>
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
      </Modal>

      {confirmNode}
    </div>
  )
}