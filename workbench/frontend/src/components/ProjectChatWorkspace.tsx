import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Outlet, useLocation, useNavigate, useParams } from 'react-router-dom'
import { getTree, listChapters } from '../api/client'
import type { ChapterSummary, TreeNode } from '../api/types'
import { ProjectChatContext, type ChatSelectionAttachment, type ProjectChatPageContext } from '../state/projectChat'
import ChatPanel from './ChatPanel'
import { useOverlayFocus } from '../state/useOverlayFocus'
import Icon from './Icon'
import useChatController from '../state/useChatController'

/** 项目路由保持唯一助手实例。Portal 只改变呈现位置，不改变控制器身份。 */
export default function ProjectChatWorkspace() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  return <ProjectChatProject key={projectId} projectId={projectId} />
}

function ProjectChatProject({ projectId }: { projectId: number }) {
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const [pageContext, setPageContext] = useState<ProjectChatPageContext | null>(null)
  const contextToken = useRef<symbol | null>(null)
  const [host, setHost] = useState<HTMLElement | null>(null)
  const [fallbackHost, setFallbackHost] = useState<HTMLDivElement | null>(null)
  const fallbackHostRef = useRef<HTMLDivElement | null>(null)
  const [isOpen, setOpen] = useState(false)
  const [files, setFiles] = useState<string[]>([])
  const [chapters, setChapters] = useState<ChapterSummary[]>([])
  const [attachment, setAttachment] = useState<(ChatSelectionAttachment & { id: number }) | null>(null)
  const [activity, setActivity] = useState('')
  const [resourceError, setResourceError] = useState('')
  const refreshSequence = useRef(0)
  const isChatPage = pathname.endsWith('/chat')
  const refresh = useCallback(async () => {
    const sequence = ++refreshSequence.current
    const [treeResult, chaptersResult] = await Promise.allSettled([getTree(projectId), listChapters(projectId)])
    if (sequence !== refreshSequence.current) return
    const errors: string[] = []
    const paths = new Set<string>()
    const walk = (nodes: TreeNode[]) => nodes.forEach((node) => {
      if (node.type === 'file') paths.add(node.rel_path)
      if (node.children) walk(node.children)
    })
    if (treeResult.status === 'fulfilled') {
      walk(treeResult.value.root_nodes); treeResult.value.groups.forEach((group) => walk(group.nodes))
      setFiles([...paths].sort())
    } else errors.push('参考材料暂未刷新')
    if (chaptersResult.status === 'fulfilled') setChapters(chaptersResult.value)
    else errors.push('章节目标暂未刷新')
    setResourceError(errors.join('；'))
  }, [projectId])
  useEffect(() => { void refresh(); return () => { refreshSequence.current += 1 } }, [refresh])
  useEffect(() => { setOpen(false) }, [pathname])
  const registerPageContext = useCallback((context: ProjectChatPageContext) => {
    if (context.projectId !== projectId) return () => undefined
    const token = Symbol('page-chat-context')
    contextToken.current = token; setPageContext(context)
    return () => {
      if (contextToken.current === token) { contextToken.current = null; setPageContext(null) }
    }
  }, [projectId])
  const value = useMemo(() => ({
    registerPageContext, setChatHost: setHost,
    openChat: () => setOpen(true), closeChat: () => setOpen(false), isOpen,
    attachSelection: (selection: ChatSelectionAttachment) => {
      setAttachment({ ...selection, id: Date.now() }); setOpen(true)
    },
  }), [registerPageContext, isOpen])
  const pageRef = useRef(pageContext)
  pageRef.current = pageContext
  const prepareContext = useCallback(async () => {
    const page = pageRef.current
    return { project_id: projectId, page_type: page?.pageType || 'chat', ...(await page?.prepareContext() || {}) }
  }, [projectId])
  const openFile = useCallback((path: string) => {
    if (pageRef.current) return pageRef.current.onOpenFile(path)
    navigate(`/project/${projectId}/editor?path=${encodeURIComponent(path)}`)
  }, [navigate, projectId])
  const filesChanged = useCallback(async (changes: Parameters<ProjectChatPageContext['onFilesChanged']>[0]) => {
    await refresh()
    await pageRef.current?.onFilesChanged(changes)
    window.dispatchEvent(new CustomEvent('aiw:project-files-changed', { detail: { projectId, changes } }))
  }, [refresh, projectId])
  const overlay = isOpen && !isChatPage
  useOverlayFocus(fallbackHostRef, overlay, () => setOpen(false))
  const controller = useChatController({
    projectId, chapterRel: pageContext?.chapterRel,
    targetChapterRel: pageContext?.targetChapterRel, selection: pageContext?.selection,
    hasUnsavedChanges: pageContext?.hasUnsavedChanges, availableFiles: pageContext?.availableFiles ?? files,
    availableChapters: chapters, prepareContext, onFilesChanged: filesChanged, onOpenFile: openFile,
    onCollapse: () => setOpen(false), portalHost: overlay ? fallbackHost : host ?? fallbackHost,
    attachment, onActivityChanged: setActivity, presentation: isChatPage ? 'page' : overlay ? 'overlay' : 'panel',
  })
  return <ProjectChatContext.Provider value={value}>
    {resourceError ? <div className="project-chat-resource-error" role="status">{resourceError}<button className="btn btn--sm" onClick={() => void refresh()}>重新读取材料</button></div> : null}
    <Outlet />
    {!isChatPage && <button type="button" className="project-assistant-launch" onClick={() => setOpen(true)}
      aria-label="打开本书写作助手"><Icon name="chat" /> 本书助手{activity ? <span>{activity}</span> : null}</button>}
    <div className="project-assistant-overlay" data-workbench-overlay hidden={!overlay}>
      <button type="button" className="project-assistant-backdrop" aria-label="关闭助手遮罩" onClick={() => setOpen(false)} />
      <div ref={(element) => { fallbackHostRef.current = element; setFallbackHost(element) }} className="project-assistant-surface"
        role={overlay ? 'dialog' : undefined} aria-modal={overlay || undefined} aria-label="本书写作助手" tabIndex={-1} />
    </div>
    <ChatPanel controller={controller} />
  </ProjectChatContext.Provider>
}
