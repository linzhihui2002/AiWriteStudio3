import { memo, type ReactNode } from 'react'
import type { ChatMessage } from '../api/types'

interface HistoryProps {
  message: ChatMessage
  highlighted: boolean
  busy: boolean
  reverting: number | null
  writeDisabled?: boolean
  renderContent: (text: string, meta: Record<string, unknown>) => ReactNode
}

const renderCounts = new Map<number, number>()
/** Browser acceptance can verify historical Markdown is not reparsed per delta. */
export const chatHistoryRenderCounts = () => Object.fromEntries(renderCounts)
export const resetChatHistoryRenderCounts = () => renderCounts.clear()

function ChatHistoryMessage({ message, highlighted, renderContent }: HistoryProps) {
  if (renderCounts.size > 2000) renderCounts.delete(renderCounts.keys().next().value as number)
  renderCounts.set(message.id, (renderCounts.get(message.id) || 0) + 1)
  return <article data-message-id={message.id} className={`chat__msg chat__msg--${message.role === 'user' ? 'user' : 'assistant'}${highlighted ? ' chat__msg--source' : ''}`}>
    <div className="chat__speaker">{message.role === 'user' ? '你' : '写作助手'}<time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</time></div>
    {message.role === 'user' ? message.content : renderContent(message.content, message.meta)}
  </article>
}

export default memo(ChatHistoryMessage, (previous, next) => previous.message === next.message
  && previous.highlighted === next.highlighted && previous.busy === next.busy && previous.reverting === next.reverting
  && previous.writeDisabled === next.writeDisabled)
