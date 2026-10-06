import { useCallback, useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { getChatDefaults, getWritingPrefs, putChatDefaults, putWritingPrefs } from '../api/client'
import type { ChatDefaults, ChatPermissionMode, WritingPrefs } from '../api/types'
import WorkspacePage, { ResourceState, useResourceRequest } from '../components/WorkspacePage'
import { CHAT_PERMISSION_OPTIONS } from '../lib/chatState'
import { errorMessage, useToast } from '../state/useToast'

/** Book defaults are separate from the settings of the current conversation. */
export default function BookSettings() {
  const { id } = useParams()
  return <BookSettingsForm key={id} projectId={Number(id)} />
}

function BookSettingsForm({ projectId }: { projectId: number }) {
  const { push } = useToast()
  const defaultsResource = useResourceRequest(`${projectId}:chat-defaults`)
  const writingResource = useResourceRequest(`${projectId}:writing-prefs`)
  const [defaults, setDefaults] = useState<ChatDefaults | null>(null)
  const [writing, setWriting] = useState<WritingPrefs | null>(null)
  const [range, setRange] = useState({ min: 2000, max: 4000 })
  const [saving, setSaving] = useState<'defaults' | 'writing' | null>(null)
  const [saveError, setSaveError] = useState('')
  const loadDefaults = useCallback(async () => {
    const token = defaultsResource.begin()
    try {
      const value = await getChatDefaults(projectId)
      if (!defaultsResource.accept(token)) return
      setDefaults(value); defaultsResource.finish(token)
    } catch (error) { defaultsResource.fail(token, errorMessage(error)) }
  }, [projectId, defaultsResource.begin, defaultsResource.accept, defaultsResource.finish, defaultsResource.fail])
  const loadWriting = useCallback(async () => {
    const token = writingResource.begin()
    try {
      const value = await getWritingPrefs(projectId)
      if (!writingResource.accept(token)) return
      setWriting(value); setRange({ min: value.chapter_min_words, max: value.chapter_max_words }); writingResource.finish(token)
    } catch (error) { writingResource.fail(token, errorMessage(error)) }
  }, [projectId, writingResource.begin, writingResource.accept, writingResource.finish, writingResource.fail])
  useEffect(() => { void loadDefaults(); void loadWriting() }, [loadDefaults, loadWriting])
  const announce = () => window.dispatchEvent(new CustomEvent('aiw:book-defaults-changed', { detail: { projectId } }))
  const saveDefaults = async () => {
    if (!defaults || saving) return
    setSaving('defaults'); setSaveError('')
    try {
      setDefaults(await putChatDefaults(projectId, { permission_mode: defaults.permission_mode, discussion_only: defaults.discussion_only }))
      announce(); push('已保存本书新对话默认设置', 'success')
    } catch (error) { setSaveError(errorMessage(error)) }
    finally { setSaving(null) }
  }
  const saveWriting = async () => {
    if (saving) return
    if (!Number.isInteger(range.min) || !Number.isInteger(range.max) || range.min <= 0 || range.max <= range.min || range.max > 50000) {
      setSaveError('字数下限和上限须为正整数，下限小于上限，上限不超过 50000。'); return
    }
    setSaving('writing'); setSaveError('')
    try {
      setWriting(await putWritingPrefs(projectId, { chapter_min_words: range.min, chapter_max_words: range.max }))
      announce(); push('已保存本书每章字数区间', 'success')
    } catch (error) { setSaveError(errorMessage(error)) }
    finally { setSaving(null) }
  }
  return <WorkspacePage>
    <header className="page-header"><div><h1 className="page-header__title">项目设置</h1><p className="page-header__desc">这本书的创作默认值。当前对话的模型、权限和角色仍在对话设置中调整。</p></div></header>
    {saveError && <div className="workspace-notice workspace-notice--error" role="alert">{saveError} 输入已保留，可修改后重新保存。</div>}
    <section className="panel"><div className="panel__header"><h2 className="panel__title">新对话默认设置</h2></div><div className="panel__body stack">
      <ResourceState {...defaultsResource} hasData={!!defaults} onRetry={() => void loadDefaults()}>
        {defaults && <><p className="muted">{defaults.source === 'book' ? '使用本书设置' : '沿用全局设置'}。只影响本书以后创建的新对话。</p>
          <label className="field"><span className="field__label">默认权限</span><select className="select" aria-label="本书新对话默认权限" value={defaults.permission_mode} disabled={!!saving} onChange={event => setDefaults({ ...defaults, permission_mode: event.target.value as ChatPermissionMode })}>{CHAT_PERMISSION_OPTIONS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}</select><span className="field__hint">{CHAT_PERMISSION_OPTIONS.find(option => option.value === defaults.permission_mode)?.description}</span></label>
          <label className="checkbox"><input type="checkbox" checked={defaults.discussion_only} disabled={!!saving} onChange={event => setDefaults({ ...defaults, discussion_only: event.target.checked })} />新对话默认仅讨论，不修改文件</label>
          <div><button className="btn btn--primary" disabled={!!saving} onClick={() => void saveDefaults()}>{saving === 'defaults' ? '保存中…' : '保存对话默认值'}</button></div></>}
      </ResourceState>
    </div></section>
    <section className="panel"><div className="panel__header"><h2 className="panel__title">每章字数区间</h2></div><div className="panel__body stack">
      <ResourceState {...writingResource} hasData={!!writing} onRetry={() => void loadWriting()}>
        {writing && <><p className="muted">{writing.source === 'book' ? '使用本书设置' : '沿用全局设置'}。字数下限用于生成目标与章节字数门，上限用于提醒。</p>
          <label className="field"><span className="field__label">下限</span><input className="input" type="number" min={1} max={50000} aria-label="本书每章字数下限" value={range.min} disabled={!!saving} onChange={event => setRange({ ...range, min: Number(event.target.value) })} /></label>
          <label className="field"><span className="field__label">上限</span><input className="input" type="number" min={1} max={50000} aria-label="本书每章字数上限" value={range.max} disabled={!!saving} onChange={event => setRange({ ...range, max: Number(event.target.value) })} /></label>
          <div><button className="btn btn--primary" disabled={!!saving} onClick={() => void saveWriting()}>{saving === 'writing' ? '保存中…' : '保存字数区间'}</button></div></>}
      </ResourceState>
    </div></section>
  </WorkspacePage>
}
