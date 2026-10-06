import { useEffect, useState } from 'react'
import { deleteChatMemory, getChatMemory, updateChatMemory } from '../api/client'
import type { ChatMemoryEntry } from '../api/types'
import { errorMessage } from '../state/useToast'
import Drawer from './Drawer'

export default function ChatMemoryPanel({ projectId, open, onClose, onSource }: {
  projectId: number
  open: boolean
  onClose: () => void
  onSource: (sessionId: number, messageId?: number | null) => void
}) {
  const [entries, setEntries] = useState<ChatMemoryEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [editing, setEditing] = useState<string | number | null>(null)
  const [content, setContent] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    if (!open) return
    let active = true
    setLoading(true); setError(''); setEditing(null)
    void getChatMemory(projectId).then((data) => { if (active) setEntries(data.entries) })
      .catch((err) => { if (active) setError(errorMessage(err)) })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [open, projectId])
  const save = async (entry: ChatMemoryEntry) => {
    if (!content.trim() || busy) return
    setBusy(true); setError('')
    try {
      await updateChatMemory(projectId, entry.id, content.trim())
      setEntries((await getChatMemory(projectId)).entries)
      setEditing(null)
    } catch (err) { setError(errorMessage(err)) }
    finally { setBusy(false) }
  }
  const remove = async (entry: ChatMemoryEntry) => {
    if (busy) return
    setBusy(true); setError('')
    try {
      await deleteChatMemory(projectId, entry.id)
      setEntries((items) => items.filter((item) => item.id !== entry.id))
      if (editing === entry.id) setEditing(null)
    } catch (err) { setError(errorMessage(err)) }
    finally { setBusy(false) }
  }
  return <Drawer title="本书对话记忆" open={open} onClose={onClose} width={680}>
    <p className="muted">这里保存已提取的创作偏好和决定，供本书后续对话参考。你可以更正、删除，或回到来源对话核对。</p>
    {error ? <p className="chat__error" role="alert">{error}</p> : null}
    {loading ? <p className="muted">正在读取记忆…</p> : entries.length === 0 ? <p className="muted">暂时没有记忆。对话中的明确偏好和决定会在处理后显示在这里。</p> : <div className="chat__memory-list">{entries.map((entry) => <article className="chat__memory-entry" key={entry.id}>
      <div className="chat__memory-meta"><span>{({ active: '当前使用', confirmed: '已确认', pending: '待确认', superseded: '已更新', conflict: '存在冲突', candidate: '待确认' } as Record<string, string>)[entry.status] || entry.status}</span>{entry.updated_at ? <time>{new Date(entry.updated_at).toLocaleString()}</time> : null}</div>
      {editing === entry.id ? <textarea className="textarea" aria-label="修改记忆" value={content} onChange={(event) => setContent(event.target.value)} disabled={busy} /> : <p>{entry.content}</p>}
      {entry.conflicts?.length ? <div className="chat__memory-conflicts" role="note"><strong>与书稿中的记录有差异，请核对</strong><ul>{entry.conflicts.map((conflict, index) => <li key={`${conflict.path}-${index}`}><span>{conflict.path}</span>：{conflict.content}</li>)}</ul><p>书稿尚未因这条记忆修改。可以在对话中明确要保留哪项设定，再决定是否同步书稿。</p></div> : null}
      {entry.sources?.length || entry.history?.length ? <details className="chat__memory-history"><summary>来源与更新记录</summary>{entry.sources?.map((source, index) => <div key={`source-${index}`}><strong>{source.kind === 'author_edit' ? '作者在面板更正' : '作者消息'}</strong>{source.recorded_at ? <time>{new Date(source.recorded_at).toLocaleString()}</time> : null}<p>{source.quote}</p>{source.session_id ? <button type="button" className="btn btn--ghost btn--sm" onClick={() => { onClose(); onSource(source.session_id!, source.message_id) }}>跳转来源消息</button> : null}</div>)}{entry.history?.length ? <><strong>此前记录</strong>{entry.history.map((version, index) => <p key={`version-${index}`}>{version.updated_at ? `${new Date(version.updated_at).toLocaleString()} · ` : ''}{version.content}</p>)}</> : null}</details> : null}
      <div className="chat__interaction-actions">{editing === entry.id ? <><button type="button" className="btn btn--primary btn--sm" disabled={busy || !content.trim()} onClick={() => void save(entry)}>保存更正</button><button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={() => setEditing(null)}>取消</button></> : <button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={() => { setEditing(entry.id); setContent(entry.content) }}>修改</button>}
        {entry.source_session_id ? <button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={() => { onClose(); onSource(entry.source_session_id!, entry.source_message_id) }}>查看来源</button> : null}<button type="button" className="btn btn--ghost btn--sm" disabled={busy} onClick={() => void remove(entry)}>删除</button></div>
    </article>)}</div>}
  </Drawer>
}
