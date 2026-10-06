import { createContext, useContext } from 'react'
import type { ChatContext, ChatFileChange } from '../api/types'

/** 页面只提供材料与编辑器回调；角色、写域与批准仍由服务端决定。 */
export interface ProjectChatPageContext {
  projectId: number
  pageType: string
  chapterRel?: string | null
  targetChapterRel?: string | null
  selection?: string
  hasUnsavedChanges?: boolean
  availableFiles?: string[]
  prepareContext: () => Promise<ChatContext>
  onFilesChanged: (changes: ChatFileChange[]) => void | Promise<void>
  onOpenFile: (path: string) => void | Promise<void>
}

export interface ChatSelectionAttachment {
  path: string
  text: string
  start?: number
  end?: number
  baseHash?: string
}

export interface ProjectChatWorkspaceValue {
  registerPageContext: (context: ProjectChatPageContext) => () => void
  setChatHost: (element: HTMLElement | null) => void
  openChat: () => void
  closeChat: () => void
  attachSelection: (selection: ChatSelectionAttachment) => void
  isOpen: boolean
}

export const ProjectChatContext = createContext<ProjectChatWorkspaceValue | null>(null)
export function useProjectChat() {
  const workspace = useContext(ProjectChatContext)
  if (!workspace) throw new Error('本书助手需要 ProjectChatWorkspace 项目父路由')
  return workspace
}
