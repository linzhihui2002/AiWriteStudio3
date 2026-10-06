import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import type { Project } from '../api/types'
import { ResourceRequests, resourcePresentation, resourceSnapshotForScope } from '../lib/workspaceState'
import '../styles/workspacePages.css'

export default function WorkspacePage({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`page workspace-page ${className}`.trim()}>{children}</div>
}

/** A resource keeps its last successful data visible while a refresh fails. */
export function ResourceState({ loading, error, hasData, loaded = hasData, onRetry, emptyTitle, emptyDescription, children }: {
  loading: boolean; error?: string; hasData: boolean; loaded?: boolean; onRetry?: () => void;
  emptyTitle?: string; emptyDescription?: string; children?: ReactNode
}) {
  const presentation = resourcePresentation({ loading, error, hasData, loaded })
  if (presentation === 'loading') return <div className="workspace-resource workspace-resource--loading" role="status" aria-busy="true"><span className="workspace-loading-mark" />正在读取…</div>
  if (presentation === 'error') return <div className="workspace-resource workspace-resource--error" role="alert"><strong>暂时无法读取</strong><p>{error}</p>{onRetry && <button className="btn btn--sm" type="button" onClick={onRetry}>重新读取</button>}</div>
  return <>{(loading || error) && <div className={`workspace-notice${error ? ' workspace-notice--error' : ''}`} role={error ? 'alert' : 'status'}><span>{error ? `更新失败，保留上次读取的数据。${error}` : '正在更新，保留当前内容…'}</span>{error && onRetry && <button className="btn btn--sm" onClick={onRetry}>重试</button>}</div>}{!hasData && emptyTitle ? <div className="empty-state"><p className="empty-state__title">{emptyTitle}</p>{emptyDescription && <p className="empty-state__desc">{emptyDescription}</p>}</div> : children}</>
}

export function WorkspaceTabs<T extends string>({ label, items, value, onChange }: {
  label: string; items: ReadonlyArray<{ key: T; label: string }>; value: T; onChange: (key: T) => void
}) {
  return <nav className="workspace-tabs" aria-label={label}>{items.map(item => <button key={item.key} type="button" className={item.key === value ? 'is-active' : ''} aria-pressed={item.key === value} onClick={() => onChange(item.key)}>{item.label}</button>)}</nav>
}

export function ProjectPicker({ projects, value, onChange, allowAll = false, label = '书籍' }: {
  projects: Project[]; value: string; onChange: (value: string) => void; allowAll?: boolean; label?: string
}) {
  const legacy = value && !projects.some(project => String(project.id) === value)
  return <label className="field workspace-project-picker"><span className="field__label">{label}</span><select className="select" value={value} onChange={event => onChange(event.target.value)}><option value="">{allowAll ? '全部书籍' : '请选择书籍'}</option>{legacy && <option value={value}>书籍 #{value}（当前不在列表中）</option>}{projects.map(project => <option key={project.id} value={project.id}>{project.name}{project.archived ? ' · 已归档' : ''}</option>)}</select></label>
}

export function ActionMenu({ label = '更多', ariaLabel, children }: { label?: string; ariaLabel?: string; children: ReactNode }) {
  return <details className="workspace-more" onClick={event => event.stopPropagation()} onKeyDown={event => {
    event.stopPropagation()
    const details = event.currentTarget
    if (event.key === 'Escape' && details.open) { event.preventDefault(); details.open = false; details.querySelector('summary')?.focus() }
    if (event.key === 'ArrowDown' && (event.target as HTMLElement).tagName === 'SUMMARY') { event.preventDefault(); details.open = true; details.querySelector<HTMLElement>('.workspace-more__items button:not(:disabled), .workspace-more__items a[href]')?.focus() }
  }}><summary className="btn btn--sm" aria-label={ariaLabel || label}>{label}<span aria-hidden="true">⌄</span></summary><div className="workspace-more__items" onClick={event => { if ((event.target as HTMLElement).closest('button, a')) (event.currentTarget.parentElement as HTMLDetailsElement).open = false }}>{children}</div></details>
}

/** Requests are scoped to a book/filter and only the latest response may commit. */
export function useResourceRequest(scope: string | number) {
  const currentScope = useRef(scope)
  const tracker = useRef(new ResourceRequests())
  const dataScope = useRef<string | number | null>(null)
  currentScope.current = scope
  const [state, setState] = useState({ loading: true, error: '', loaded: false, scope })
  useEffect(() => () => tracker.current.invalidate(), [])
  const begin = useCallback(() => {
    const token = currentScope.current === scope ? tracker.current.begin(scope) : { sequence: -1, scope }
    if (currentScope.current !== scope) return token
    setState({ loading: true, error: '', loaded: dataScope.current === token.scope, scope: token.scope })
    return token
  }, [scope])
  const accept = useCallback((token: { sequence: number; scope: string | number }) => tracker.current.accepts(token, currentScope.current), [])
  const finish = useCallback((token: { sequence: number; scope: string | number }) => {
    if (!accept(token)) return
    dataScope.current = token.scope
    setState({ loading: false, error: '', loaded: true, scope: token.scope })
  }, [accept])
  const fail = useCallback((token: { sequence: number; scope: string | number }, error: string) => {
    if (accept(token)) setState({ loading: false, error, loaded: dataScope.current === token.scope, scope: token.scope })
  }, [accept])
  const isCurrentData = dataScope.current === scope
  const visible = resourceSnapshotForScope(state, scope)
  return { ...visible, loaded: isCurrentData && visible.loaded, begin, accept, finish, fail }
}
