/**
 * 轻量 Markdown 渲染 + 字数里程碑装饰层。
 *
 * 安全：**不注入 HTML**（纯文本 → React 元素），因此正文里的任何标记都不会被执行。
 * 里程碑（Task 29a）：只在**渲染层**插入「已满 N 字」徽标，绝不写入正文、不参与统计。
 */

import { Fragment, type ReactNode } from 'react'

export interface MilestoneInfo {
  step: number
  enabled: boolean
  budget: number
  /** 只在阅读/预览态注入装饰 */
  decorate: boolean
}

function inline(text: string, keyPrefix: string): ReactNode[] {
  const nodes: ReactNode[] = []
  const pattern = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`)/g
  let lastIndex = 0
  let match: RegExpExecArray | null
  let index = 0
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) nodes.push(text.slice(lastIndex, match.index))
    const token = match[0]
    index += 1
    const key = `${keyPrefix}-i${index}`
    if (token.startsWith('**')) {
      nodes.push(<strong key={key}>{token.slice(2, -2)}</strong>)
    } else if (token.startsWith('`')) {
      nodes.push(<code key={key}>{token.slice(1, -1)}</code>)
    } else {
      nodes.push(<em key={key}>{token.slice(1, -1)}</em>)
    }
    lastIndex = match.index + token.length
  }
  if (lastIndex < text.length) nodes.push(text.slice(lastIndex))
  return nodes
}

export interface MarkdownViewProps {
  text: string
  milestone?: MilestoneInfo
  /** 空正文时的提示 */
  emptyHint?: string
}

export default function MarkdownView({ text, milestone, emptyHint }: MarkdownViewProps) {
  const source = (text ?? '').replace(/\r\n/g, '\n')
  if (!source.trim()) {
    return <p className="muted">{emptyHint ?? '（正文为空）'}</p>
  }

  const lines = source.split('\n')
  const blocks: ReactNode[] = []
  let paragraph: string[] = []
  let quote: string[] = []
  let list: string[] = []
  let written = 0
  let nextMilestone = milestone?.step ?? 0
  let milestoneIndex = 0

  const flushParagraph = () => {
    if (!paragraph.length) return
    const textBlock = paragraph.join(' ')
    blocks.push(<p key={`p${blocks.length}`}>{inline(textBlock, `p${blocks.length}`)}</p>)
    paragraph = []
  }
  const flushQuote = () => {
    if (!quote.length) return
    blocks.push(
      <blockquote key={`q${blocks.length}`}>{inline(quote.join(' '), `q${blocks.length}`)}</blockquote>,
    )
    quote = []
  }
  const flushList = () => {
    if (!list.length) return
    blocks.push(
      <ul key={`u${blocks.length}`} className="prose-list">
        {list.map((item, index) => (
          <li key={index}>{inline(item, `l${blocks.length}-${index}`)}</li>
        ))}
      </ul>,
    )
    list = []
  }
  const flushAll = () => {
    flushParagraph()
    flushQuote()
    flushList()
  }

  const maybeMilestone = (count: number) => {
    if (!milestone?.decorate || !milestone.enabled || milestone.step <= 0) return
    while (nextMilestone > 0 && count >= nextMilestone) {
      milestoneIndex += 1
      const reached = nextMilestone
      const percent = milestone.budget
        ? ` · 预算 ${milestone.budget} · ${Math.round((reached / milestone.budget) * 100)}%`
        : ''
      blocks.push(
        <div className="milestone-badge" key={`m-${milestoneIndex}-${reached}`} aria-hidden="true">
          已满 {reached} 字{percent}
        </div>,
      )
      nextMilestone += milestone.step
    }
  }

  lines.forEach((raw, lineIndex) => {
    const line = raw.trimEnd()
    const trimmed = line.trim()

    if (!trimmed) {
      flushAll()
      return
    }
    if (/^#{1,4}\s+/.test(trimmed)) {
      flushAll()
      const level = trimmed.match(/^#+/)![0].length
      const content = trimmed.replace(/^#{1,4}\s+/, '')
      const Tag = (level <= 2 ? 'h3' : 'h4') as 'h3' | 'h4'
      blocks.push(
        <Tag key={`h${lineIndex}`} className={`prose-heading prose-heading--${level}`}>
          {inline(content, `h${lineIndex}`)}
        </Tag>,
      )
      written += content.replace(/\s/g, '').length
      maybeMilestone(written)
      return
    }
    if (/^(-{3,}|\*{3,})$/.test(trimmed)) {
      flushAll()
      blocks.push(<hr key={`hr${lineIndex}`} className="prose-rule" />)
      return
    }
    if (trimmed.startsWith('>')) {
      flushParagraph()
      flushList()
      quote.push(trimmed.replace(/^>\s?/, ''))
      written += trimmed.replace(/\s/g, '').length
      return
    }
    if (/^([-*+]|\d+\.)\s+/.test(trimmed)) {
      flushParagraph()
      flushQuote()
      list.push(trimmed.replace(/^([-*+]|\d+\.)\s+/, ''))
      written += trimmed.replace(/\s/g, '').length
      maybeMilestone(written)
      return
    }
    if (trimmed.startsWith('|')) {
      flushAll()
      const cells = trimmed.replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim())
      if (cells.every((cell) => /^:?-{2,}:?$/.test(cell) || cell === '')) return
      blocks.push(
        <div className="prose-table-row" key={`tr${lineIndex}`}>
          {cells.map((cell, index) => (
            <span key={index}>{inline(cell, `td${lineIndex}-${index}`)}</span>
          ))}
        </div>,
      )
      return
    }

    flushQuote()
    flushList()
    paragraph.push(trimmed)
    written += trimmed.replace(/\s/g, '').length
    maybeMilestone(written)
  })
  flushAll()

  return <div className="prose">{blocks.map((block, index) => <Fragment key={index}>{block}</Fragment>)}</div>
}