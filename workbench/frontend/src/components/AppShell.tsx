import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { NavLink, Outlet, useLocation, useParams } from 'react-router-dom'
import PanelResizer from './PanelResizer'
import ThemeSwitcher from './ThemeSwitcher'
import { proposalCount } from '../api/client'
import { usePanelWidth } from '../state/usePanelWidth'

/** 项目内的六个工作区（均需项目上下文）。 */
const PROJECT_SECTIONS = [
  { key: 'editor', label: '正文编辑器' },
  { key: 'outline', label: '大纲规划' },
  { key: 'bible', label: 'Story Bible' },
  { key: 'cards', label: '设定卡片' },
  { key: 'teardown', label: '拆书资产库' },
  { key: 'review', label: '审稿中心' },
] as const

/** 全局区（无需项目上下文）。 */
const GLOBAL_SECTIONS = [
  { key: 'bookshelf', label: '书架', to: '/', end: true },
  { key: 'workflows', label: '工作流', to: '/workflows', end: false },
  { key: 'inbox', label: '收件箱', to: '/inbox', end: false },
  { key: 'dashboard', label: '仪表盘', to: '/dashboard', end: false },
  { key: 'images', label: '生图工坊', to: '/images', end: false },
  { key: 'settings', label: '设置', to: '/settings', end: false },
] as const

function resolveTitle(pathname: string): string {
  if (pathname === '/') return '书架'
  if (pathname.startsWith('/settings')) return '设置'
  if (pathname.startsWith('/workflows')) return '工作流'
  if (pathname.startsWith('/inbox')) return '收件箱'
  if (pathname.startsWith('/dashboard')) return '仪表盘'
  if (pathname.startsWith('/images')) return '生图工坊'
  const matched = /^\/project\/[^/]+\/([^/]+)/.exec(pathname)
  if (matched) {
    const section = PROJECT_SECTIONS.find((item) => item.key === matched[1])
    if (section) return section.label
  }
  return '未找到页面'
}

/**
 * 布局壳：左侧可收起/可拖宽的导航 + 右侧主内容区（顶栏 + 路由出口）。
 * 顶栏含：收件箱待处理数、主题切换、侧栏收起/展开（纯 UI 偏好，localStorage 记忆）。
 */
export default function AppShell() {
  const { pathname } = useLocation()
  const { id: projectId } = useParams<{ id: string }>()
  const [pending, setPending] = useState(0)
  const shellRef = useRef<HTMLDivElement>(null)

  // 侧栏折叠 + 宽度（纯 UI 偏好，localStorage 记忆；原「专注模式」已移除）
  const [sidebarCollapsed, setSidebarCollapsed] = useState(
    () => localStorage.getItem('aiw.shell.sidebarCollapsed') === '1',
  )
  const [sidebarWidth, setSidebarWidth] = usePanelWidth('aiw.shell.sidebarWidth', 208, 180, 360)

  useEffect(() => {
    localStorage.setItem('aiw.shell.sidebarCollapsed', sidebarCollapsed ? '1' : '0')
  }, [sidebarCollapsed])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const data = await proposalCount(projectId ? Number(projectId) : undefined)
        if (alive) setPending(data.pending)
      } catch {
        if (alive) setPending(0)
      }
    }
    void load()
    const timer = window.setInterval(load, 15000)
    return () => {
      alive = false
      window.clearInterval(timer)
    }
  }, [projectId, pathname])

  // 键盘可达：Ctrl+Shift+F 切换侧栏（沿用原专注模式快捷键）
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.shiftKey && event.key.toLowerCase() === 'f') {
        event.preventDefault()
        setSidebarCollapsed((collapsed) => !collapsed)
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [])

  return (
    <div
      ref={shellRef}
      className={`app-shell${sidebarCollapsed ? ' app-shell--no-sidebar' : ''}`}
      style={{ '--app-sidebar-w': `${sidebarWidth}px` } as CSSProperties}
    >
      <aside className="app-sidebar">
        <div className="app-brand">
          <span className="app-brand__name">AI 小说创作工作台</span>
          <span className="app-brand__meta">v0.1 · 本地优先</span>
        </div>

        <nav className="app-nav">
          <div className="app-nav__group">
            <span className="app-nav__label">全局</span>
            {GLOBAL_SECTIONS.map((section) => (
              <NavLink
                key={section.key}
                to={section.to}
                end={section.end}
                className={({ isActive }) => (isActive ? 'nav-item is-active' : 'nav-item')}
              >
                {section.label}
                {section.key === 'inbox' && pending > 0 ? (
                  <span className="tag tag--warn" style={{ marginLeft: 'auto' }}>
                    {pending}
                  </span>
                ) : null}
              </NavLink>
            ))}
          </div>

          <div className="app-nav__group">
            <span className="app-nav__label">项目</span>
            {projectId ? (
              <>
                <span className="app-nav__project" title={projectId}>
                  #{projectId}
                </span>
                {PROJECT_SECTIONS.map((section) => (
                  <NavLink
                    key={section.key}
                    to={`/project/${projectId}/${section.key}`}
                    className={({ isActive }) => (isActive ? 'nav-item is-active' : 'nav-item')}
                  >
                    {section.label}
                  </NavLink>
                ))}
              </>
            ) : (
              <>
                {PROJECT_SECTIONS.map((section) => (
                  <span
                    key={section.key}
                    className="nav-item is-disabled"
                    aria-disabled="true"
                    title="请先从书架进入一个项目"
                  >
                    {section.label}
                  </span>
                ))}
                <span className="app-nav__hint">请先从书架进入一个项目。</span>
              </>
            )}
          </div>
        </nav>
      </aside>

      {sidebarCollapsed ? null : (
        <PanelResizer
          className="panel-resizer--sidebar"
          side="left"
          value={sidebarWidth}
          min={180}
          max={360}
          label="调整主导航宽度"
          onChange={setSidebarWidth}
          onPreview={(next) => shellRef.current?.style.setProperty('--app-sidebar-w', `${next}px`)}
          onToggle={() => setSidebarCollapsed(true)}
        />
      )}

      <div className="app-main">
        <header className="app-topbar">
          <div className="app-topbar__title">
            {resolveTitle(pathname)}
            {projectId ? <span className="app-topbar__crumb"> / #{projectId}</span> : null}
          </div>
          <div className="app-topbar__actions">
            <button
              className="btn btn--ghost btn--sm"
              type="button"
              aria-pressed={sidebarCollapsed}
              onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
              title={sidebarCollapsed ? '展开侧栏（Ctrl+Shift+F）' : '收起侧栏（Ctrl+Shift+F）'}
            >
              {sidebarCollapsed ? '展开侧栏' : '收起侧栏'}
            </button>
            <ThemeSwitcher />
          </div>
        </header>

        <main className="app-content">
          <Outlet />
        </main>
      </div>
    </div>
  )
}