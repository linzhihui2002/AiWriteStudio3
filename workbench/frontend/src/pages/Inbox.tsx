/** 收件箱 —— AI 产出不静默落盘，必须人工核对差异后应用。 */

import { useCallback, useEffect, useState } from 'react'
import Modal from '../components/Modal'
import DiffView from '../components/DiffView'
import {
  applyProposal,
  applyProposals,
  discardProposal,
  discardProposals,
  getProposal,
  listProposals,
  updateProposal,
} from '../api/client'
import type { ProposalItem } from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

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

export default function Inbox() {
  const { push } = useToast()

  const [statusKey, setStatusKey] = useState('pending')
  const [projectFilter, setProjectFilter] = useState('')
  const [items, setItems] = useState<ProposalItem[]>([])
  const [selected, setSelected] = useState<number[]>([])
  const [detail, setDetail] = useState<ProposalItem | null>(null)
  const [editContent, setEditContent] = useState('')
  const [batch, setBatch] = useState<BatchEntry[] | null>(null)
  const [busy, setBusy] = useState(false)

  const statusValue = STATUS_TABS.find((tab) => tab.key === statusKey)?.value ?? null
  const projectId = projectFilter.trim() === '' ? undefined : Number(projectFilter)

  const load = useCallback(async () => {
    try {
      const data = await listProposals(projectId, statusValue)
      setItems(data)
      setSelected((current) => current.filter((id) => data.some((item) => item.id === id)))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, statusValue, push])

  useEffect(() => {
    void load()
  }, [load])

  const openDetail = async (id: number) => {
    setBusy(true)
    try {
      const data = await getProposal(id)
      setDetail(data)
      setEditContent(data.content)
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const closeDetail = () => {
    setDetail(null)
    setEditContent('')
  }

  const handleApply = async (id: number, content?: string) => {
    setBusy(true)
    try {
      await applyProposal(id, content)
      push('已应用提案（已生成快照并刷新索引）', 'success')
      closeDetail()
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handleEditApply = async () => {
    if (!detail) return
    setBusy(true)
    try {
      await updateProposal(detail.id, editContent)
      await applyProposal(detail.id, editContent)
      push('已按编辑内容应用提案', 'success')
      closeDetail()
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handleDiscard = async (id: number) => {
    setBusy(true)
    try {
      await discardProposal(id)
      push('已丢弃提案', 'success')
      closeDetail()
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const toggleSelect = (id: number) => {
    setSelected((current) =>
      current.includes(id) ? current.filter((item) => item !== id) : [...current, id],
    )
  }

  const allSelected = items.length > 0 && selected.length === items.length

  const toggleAll = () => {
    setSelected(allSelected ? [] : items.map((item) => item.id))
  }

  const handleBatchApply = async () => {
    if (selected.length === 0) return
    setBusy(true)
    try {
      const result = await applyProposals(selected)
      setBatch(result.results as unknown as BatchEntry[])
      push(`批量应用：成功 ${result.applied} 条，失败 ${result.failed} 条`, 'success')
      setSelected([])
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handleBatchDiscard = async () => {
    if (selected.length === 0) return
    setBusy(true)
    try {
      const result = (await discardProposals(selected)) as unknown as {
        discarded: number
        results?: BatchEntry[]
      }
      setBatch(result.results ?? [])
      push(`批量丢弃：成功 ${result.discarded} 条`, 'success')
      setSelected([])
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
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

          <div className="field">
            <label className="field__label" htmlFor="inbox-project">
              项目 ID
            </label>
            <input
              id="inbox-project"
              className="input"
              type="number"
              min={1}
              placeholder="留空表示全部项目"
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
            />
          </div>

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

      {items.length === 0 ? (
        <div className="empty-state">
          <p className="empty-state__title">暂无提案</p>
          <p className="empty-state__desc">当前筛选条件下没有待处理的 AI 产出。</p>
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
                      disabled={busy}
                      onClick={() => void handleApply(proposal.id)}
                    >
                      应用
                    </button>
                    <button
                      className="btn btn--danger btn--sm"
                      type="button"
                      disabled={busy}
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
                    <span className="muted">项目 #{proposal.project_id}</span>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      <Modal
        title={detail ? `提案 #${detail.id} · ${detail.title}` : '提案详情'}
        open={detail !== null}
        onClose={closeDetail}
        width={860}
        footer={
          detail ? (
            <div className="btn-row btn-row--end">
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
                disabled={busy}
                onClick={() => void handleDiscard(detail.id)}
              >
                丢弃
              </button>
              <button
                className="btn"
                type="button"
                disabled={busy}
                onClick={() => void handleEditApply()}
              >
                编辑后应用
              </button>
              <button
                className="btn btn--primary"
                type="button"
                disabled={busy}
                onClick={() => void handleApply(detail.id)}
              >
                应用
              </button>
            </div>
          ) : null
        }
      >
        {detail ? (
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

            <div>
              <p className="muted">差异对比</p>
              <DiffView lines={detail.diff ?? []} emptyHint="该提案没有可展示的差异" />
            </div>

            <div className="field">
              <label className="field__label" htmlFor="inbox-content">
                提案内容（可编辑后应用）
              </label>
              <textarea
                autoComplete={NO_AUTOFILL}
                id="inbox-content"
                className="textarea textarea--mono"
                value={editContent}
                onChange={(event) => setEditContent(event.target.value)}
              />
              <span className="field__hint">按修改后的内容应用。</span>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  )
}