import { Suspense, useEffect, useRef, useState, type CSSProperties } from 'react'
import { NavLink, Outlet, useLocation, useNavigate, useParams } from 'react-router-dom'
import { listProjects, listProposals } from '../api/client'
import type { Project } from '../api/types'
import PanelResizer from './PanelResizer'
import ThemeSwitcher from './ThemeSwitcher'
import Icon, { type IconName } from './Icon'
import { usePanelWidth } from '../state/usePanelWidth'
import { useOverlayFocus } from '../state/useOverlayFocus'

const PROJECT_SECTIONS: { key: string; label: string; group: string; icon: IconName }[] = [
  { key: 'chat', label: '创作对话', group: '创作', icon: 'chat' },
  { key: 'editor', label: '正文编辑器', group: '创作', icon: 'file' },
  { key: 'outline', label: '大纲规划', group: '创作', icon: 'outline' },
  { key: 'bible', label: '设定总览', group: '资料', icon: 'book' },
  { key: 'cards', label: '设定卡片', group: '资料', icon: 'cards' },
  { key: 'knowledge', label: '知识库与图谱', group: '资料', icon: 'search' },
  { key: 'teardown', label: '拆书资产库', group: '资料', icon: 'outline' },
  { key: 'review', label: '审稿中心', group: '检查', icon: 'review' },
  { key: 'settings', label: '项目设置', group: '检查', icon: 'settings' },
]
const GLOBAL_SECTIONS: { key: string; label: string; to: string; icon: IconName }[] = [
  { key: 'bookshelf', label: '书架', to: '/', icon: 'book' },
  { key: 'inbox', label: '收件箱', to: '/inbox', icon: 'inbox' },
  { key: 'workflows', label: '工作流', to: '/workflows', icon: 'workflow' },
  { key: 'dashboard', label: '仪表盘', to: '/dashboard', icon: 'chart' },
  { key: 'images', label: '生图工坊', to: '/images', icon: 'image' },
  { key: 'settings', label: '设置', to: '/settings', icon: 'settings' },
]
const CURRENT_PROJECT_KEY = 'aiw.currentProjectId'
function storedValue(key: string) { try { return localStorage.getItem(key) } catch { return null } }
function resolveTitle(pathname: string) {
  const global = GLOBAL_SECTIONS.find(section => section.to === '/' ? pathname === '/' : pathname.startsWith(section.to))
  if (global) return global.label
  const section = /^\/project\/[^/]+\/([^/]+)/.exec(pathname)?.[1]
  return PROJECT_SECTIONS.find(item => item.key === section)?.label ?? '工作台'
}
/** Kept for existing consumers; automatic navigation now applies to every workspace. */
export function knowledgeNavigationOverlay(pathname: string, width: number): boolean {
  return width < 920 && /^\/project\/[^/]+\/knowledge\/?$/.test(pathname)
}
export function workspaceNavigationOverlay(width: number): boolean { return width < 920 }

export default function AppShell() {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const { id: routeProjectId } = useParams<{ id: string }>()
  const [storedProjectId, setStoredProjectId] = useState(() => storedValue(CURRENT_PROJECT_KEY))
  const [projects, setProjects] = useState<Project[]>([])
  const [projectsError, setProjectsError] = useState('')
  const [reload, setReload] = useState(0)
  const [pendingCount, setPendingCount] = useState<number | null>(null)
  const [windowWidth, setWindowWidth] = useState(() => window.innerWidth)
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => storedValue('aiw.shell.sidebarCollapsed') === '1')
  const [sidebarWidth, setSidebarWidth] = usePanelWidth('aiw.shell.sidebarWidth', 232, 200, 360)
  const shellRef = useRef<HTMLDivElement>(null)
  const navigationRef = useRef<HTMLElement>(null)
  const projectId = routeProjectId ?? storedProjectId ?? undefined
  const currentProject = projects.find(project => String(project.id) === projectId)
  const temporaryNavigation = workspaceNavigationOverlay(windowWidth)
  useOverlayFocus(navigationRef, temporaryNavigation && navigationOpen, () => setNavigationOpen(false))

  useEffect(() => {
    let current = true
    listProjects().then(items => { if (current) { setProjects(items); setProjectsError('') } }).catch(error => {
      if (current) setProjectsError(error instanceof Error ? error.message : '读取书籍失败')
    })
    listProposals(undefined, 'pending').then(items => { if (current) setPendingCount(items.length) }).catch(() => { if (current) setPendingCount(null) })
    return () => { current = false }
  }, [reload, pathname === '/'])
  useEffect(() => {
    const resize = () => setWindowWidth(window.innerWidth)
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])
  useEffect(() => { setNavigationOpen(false) }, [pathname, temporaryNavigation])
  useEffect(() => {
    if (!routeProjectId) return
    setStoredProjectId(routeProjectId)
    try { localStorage.setItem(CURRENT_PROJECT_KEY, routeProjectId) } catch { /* Optional UI preference. */ }
  }, [routeProjectId])
  useEffect(() => {
    try { localStorage.setItem('aiw.shell.sidebarCollapsed', sidebarCollapsed ? '1' : '0') } catch { /* Optional UI preference. */ }
  }, [sidebarCollapsed])
  useEffect(() => {
    if (navigationRef.current) navigationRef.current.inert = temporaryNavigation && !navigationOpen
  }, [temporaryNavigation, navigationOpen])
  useEffect(() => {
    const keydown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'f') {
        event.preventDefault()
        if (temporaryNavigation) setNavigationOpen(open => !open)
        else setSidebarCollapsed(collapsed => !collapsed)
      }
    }
    window.addEventListener('keydown', keydown)
    return () => window.removeEventListener('keydown', keydown)
  }, [temporaryNavigation])

  const navItem = (section: typeof PROJECT_SECTIONS[number]) => projectId ? (
    <NavLink key={section.key} to={`/project/${projectId}/${section.key}`} className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}>
      <Icon name={section.icon} /><span>{section.label}</span>
    </NavLink>
  ) : <span key={section.key} className="nav-item is-disabled" aria-disabled="true"><Icon name={section.icon} /><span>{section.label}</span></span>

  return <div ref={shellRef} className={`app-shell${sidebarCollapsed || temporaryNavigation ? ' app-shell--no-sidebar' : ''}${temporaryNavigation ? ' app-shell--navigation-overlay' : ''}${navigationOpen ? ' is-navigation-open' : ''}`} style={{ '--app-sidebar-w': `${sidebarWidth}px` } as CSSProperties}>
    {temporaryNavigation && navigationOpen && <button className="app-navigation-backdrop" type="button" aria-label="关闭导航" onClick={() => setNavigationOpen(false)} />}
    <aside ref={navigationRef} tabIndex={-1} className="app-sidebar" role={temporaryNavigation ? 'dialog' : undefined} aria-modal={temporaryNavigation && navigationOpen ? true : undefined} aria-label="工作台导航">
      <div className="app-brand"><Icon name="book" size={24} /><div><span className="app-brand__name">小说创作工作台</span><span className="app-brand__meta">本地优先 · 专注创作</span></div>
        {temporaryNavigation && <button className="btn btn--ghost btn--sm app-navigation-close" type="button" data-autofocus aria-label="关闭导航" onClick={() => setNavigationOpen(false)}><Icon name="close" /></button>}
      </div>
      <div className="app-book-switcher">
        <label htmlFor="workspace-book">当前创作</label>
        <select id="workspace-book" className="input" value={currentProject ? String(currentProject.id) : ''} onChange={event => { if (event.target.value) navigate(`/project/${event.target.value}/chat`) }}>
          <option value="" disabled>{projectId ? '正在读取书名…' : '从书架选择一本书'}</option>
          {projects.map(project => <option key={project.id} value={project.id}>{project.name}</option>)}
        </select>
        {projectsError && <div className="app-nav__error" role="status">书籍列表读取失败<button type="button" className="btn btn--ghost btn--sm" onClick={() => setReload(value => value + 1)}>重试</button></div>}
      </div>
      <nav className="app-nav" aria-label="工作区">
        {['创作', '资料', '检查'].map(group => <div key={group} className="app-nav__group"><span className="app-nav__label">{group}</span>{PROJECT_SECTIONS.filter(item => item.group === group).map(navItem)}</div>)}
        <div className="app-nav__group app-nav__group--global"><span className="app-nav__label">全局工具</span>
          {GLOBAL_SECTIONS.map(section => <NavLink key={section.key} to={section.to} end={section.to === '/'} className={({ isActive }) => `nav-item${isActive ? ' is-active' : ''}`}><Icon name={section.icon} /><span>{section.label}</span>{section.key === 'inbox' && pendingCount !== null && pendingCount > 0 && <span className="app-nav__count" aria-label={`${pendingCount} 项待处理`}>{pendingCount}</span>}</NavLink>)}
        </div>
      </nav>
    </aside>
    {!sidebarCollapsed && !temporaryNavigation && <PanelResizer className="panel-resizer--sidebar" side="left" value={sidebarWidth} min={200} max={360} label="调整主导航宽度" onChange={setSidebarWidth} onPreview={next => shellRef.current?.style.setProperty('--app-sidebar-w', `${next}px`)} onToggle={() => setSidebarCollapsed(true)} />}
    <div className="app-main">
      <header className="app-topbar">
        <button className="btn btn--ghost btn--sm app-navigation-toggle" type="button" aria-label={temporaryNavigation ? '打开工作台导航' : sidebarCollapsed ? '展开工作台导航' : '收起工作台导航'} aria-expanded={temporaryNavigation ? navigationOpen : !sidebarCollapsed} onClick={() => temporaryNavigation ? setNavigationOpen(open => !open) : setSidebarCollapsed(value => !value)} title="工作台导航（Ctrl+Shift+F）"><Icon name="menu" /></button>
        <div className="app-topbar__title">{currentProject && routeProjectId && <span className="app-topbar__book" title={currentProject.name}>{currentProject.name}<Icon name="chevron" size={14} /></span>}<span>{resolveTitle(pathname)}</span></div>
        <div className="app-topbar__actions"><ThemeSwitcher /></div>
      </header>
      <main className="app-content"><Suspense fallback={<div className="workspace-route-loading" role="status">正在打开工作区…</div>}><Outlet /></Suspense></main>
    </div>
  </div>
}
