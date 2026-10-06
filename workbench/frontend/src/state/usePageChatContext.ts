import { useEffect, useRef } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import type { ChatFileChange } from '../api/types'
import { useProjectChat } from './projectChat'

/** Material browsers offer reference files. Selecting a page never sets a write target. */
export function usePageChatContext({ projectId, activeFile, files = [], hasUnsavedChanges = false, onFilesChanged }: {
  projectId: number
  activeFile?: string | null
  files?: string[]
  hasUnsavedChanges?: boolean
  onFilesChanged?: (changes: ChatFileChange[]) => void | Promise<void>
}) {
  const { registerPageContext } = useProjectChat()
  const navigate = useNavigate()
  const { pathname } = useLocation()
  const pageType = pathname.split('/').pop() || 'materials'
  const refresh = useRef(onFilesChanged)
  refresh.current = onFilesChanged
  const path = activeFile?.split('#')[0] || null
  const fileKey = JSON.stringify([...new Set(files.map(file => file.split('#')[0]).filter(Boolean))])
  useEffect(() => {
    const availableFiles = JSON.parse(fileKey) as string[]
    return registerPageContext({
      projectId, pageType,
      chapterRel: path,
      targetChapterRel: null,
      availableFiles: availableFiles.length ? availableFiles : undefined,
      hasUnsavedChanges,
      prepareContext: async () => {
        if (hasUnsavedChanges) throw new Error('当前页面材料尚未保存，请先保存后发送。')
        return path ? { active_file: path } : {}
      },
      onFilesChanged: changes => refresh.current?.(changes),
      onOpenFile: file => navigate(`/project/${projectId}/editor?${new URLSearchParams({ path: file })}`),
    })
  }, [registerPageContext, navigate, projectId, pageType, path, fileKey, hasUnsavedChanges])
}
