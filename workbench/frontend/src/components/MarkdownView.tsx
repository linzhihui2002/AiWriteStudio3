/** Safe GFM documents and plain-text novel decorations. Display never changes the file. */
import { Fragment, type CSSProperties, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { buildNovelDecorations, type NovelDecorations, type NovelMark } from '../lib/novelDecorations'
import { remarkSafeBreaks, rehypeSourceLabels } from '../lib/markdownRendering'
import '../styles/markdown.css'

export interface MilestoneInfo {
  step: number
  enabled: boolean
  budget: number
  /** Decorations are only shown when this is enabled. */
  decorate: boolean
}

export interface MarkdownViewProps {
  text: string
  milestone?: MilestoneInfo
  emptyHint?: string
  /** Chapters remain literal text, including Markdown-looking characters. */
  plain?: boolean
  /** Full document typography, independent of novel reading typography. */
  document?: boolean
  /** Enable Chinese provenance labels only in explicitly identified creative documents. */
  sourceLabels?: boolean
  decorations?: NovelDecorations
}

function markText(value: string, marks: NovelMark[], key: string): ReactNode {
  let content: ReactNode = value
  const name = marks.find((mark) => mark.kind === 'name')
  if (name) {
    const style = name.colors ? {
      '--novel-name-light': name.colors.light,
      '--novel-name-dark': name.colors.dark,
      '--novel-name-paper': name.colors.paper,
    } as CSSProperties : undefined
    content = <span className="novel-reading__name" style={style} data-entity-id={name.entityId}>{content}</span>
  }
  if (marks.some((mark) => mark.kind === 'dialogue')) {
    content = <em className="novel-reading__dialogue">{content}</em>
  }
  return <Fragment key={key}>{content}</Fragment>
}

function plainLine(text: string, from: number, to: number, decorations: NovelDecorations): ReactNode[] {
  const marks = decorations.marks.filter((mark) => mark.from < to && mark.to > from)
  const milestones = decorations.milestones.filter((node) => node.at >= from && node.at <= to)
  const boundaries = new Set([from, to])
  marks.forEach((mark) => { boundaries.add(Math.max(from, mark.from)); boundaries.add(Math.min(to, mark.to)) })
  milestones.forEach((node) => boundaries.add(node.at))
  const positions = [...boundaries].sort((left, right) => left - right)
  const nodes: ReactNode[] = []
  positions.forEach((position, index) => {
    milestones.filter((node) => node.at === position).forEach((node) => {
      nodes.push(<span key={`m-${position}-${node.count}`} className="novel-reading__milestone"
        aria-hidden="true" data-count={node.count} data-label={`已满 ${node.count} 字`} />)
    })
    const end = positions[index + 1]
    if (end !== undefined && end > position) {
      nodes.push(markText(text.slice(position, end),
        marks.filter((mark) => mark.from <= position && mark.to >= end), `${position}-${end}`))
    }
  })
  return nodes
}

export default function MarkdownView({ text, milestone, emptyHint, plain = false, document = false,
  sourceLabels = false, decorations }: MarkdownViewProps) {
  const source = text ?? ''
  if (!source.trim()) return <p className="muted">{emptyHint ?? '（正文为空）'}</p>

  if (plain) {
    const display = decorations ?? buildNovelDecorations(source, {
      milestone: { enabled: !!milestone?.decorate && milestone.enabled, step: milestone?.step ?? 0 },
    })
    const paragraphs: ReactNode[] = []
    let offset = 0
    // Preserve original offsets, including CRLF, for evidence and shared editing decorations.
    const pieces = source.split(/(\r\n|\n|\r)/)
    pieces.forEach((piece, index) => {
      if (index % 2 === 0 && piece.trim()) {
        paragraphs.push(<p key={offset}>{plainLine(source, offset, offset + piece.length, display)}</p>)
      }
      offset += piece.length
    })
    return <div className="prose novel-reading">{paragraphs}</div>
  }

  return <div className={`markdown-view${document ? ' markdown-view--document' : ''}`}>
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkSafeBreaks]} skipHtml
      rehypePlugins={document && sourceLabels ? [rehypeSourceLabels] : []}
      components={{
        table: ({ node: _node, ...props }) => <div className="markdown-table-scroll" tabIndex={0}
          role="region" aria-label="表格，可横向滚动"><table {...props} /></div>,
        a: ({ node: _node, href, ...props }) => <a {...props} href={href}
          {...(/^https?:\/\//i.test(href ?? '') ? { target: '_blank', rel: 'noopener noreferrer' } : {})} />,
      }}>
      {source}
    </ReactMarkdown>
  </div>
}
