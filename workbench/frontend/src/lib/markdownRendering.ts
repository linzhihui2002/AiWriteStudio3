/** Small display transforms around the standard Markdown parser; no raw HTML execution. */
interface SyntaxNode {
  type: string
  value?: string
  tagName?: string
  properties?: Record<string, unknown>
  children?: SyntaxNode[]
}

/** The only HTML spelling treated as presentation is a literal attribute-free line break. */
export function remarkSafeBreaks() {
  return (tree: SyntaxNode) => {
    const walk = (node: SyntaxNode) => {
      node.children?.forEach((child) => {
        if (child.type === 'html' && /^<br\s*\/?\s*>$/i.test(child.value ?? '')) {
          child.type = 'break'
          delete child.value
        } else walk(child)
      })
    }
    walk(tree)
  }
}

const labels = {
  author: { label: '作者提供', explanation: 'author：来源于作者，不自动表示已确认。' },
  model: { label: '模型整理', explanation: 'model：模型提炼或建议，确认状态以原文为准。' },
  unknown: { label: '待确认', explanation: 'unknown：信息或来源尚未确认。' },
}
type SourceKey = keyof typeof labels
interface Replacement { from: number; to: number; tokenTo: number; source: SourceKey }
const excluded = new Set(['pre', 'code', 'a', 'blockquote'])
const blocks = new Set(['p', 'li', 'td', 'th', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
const isSourceLabel = (value: string) => /^(?:来源|出处|信息来源|原始来源|source)$/i.test(value.trim())

function readableText(node: SyntaxNode): string {
  if (excluded.has(node.tagName ?? '') && !sourceCode(node)) return '\0'.repeat(textLength(node))
  if (node.type === 'text') return node.value ?? ''
  return node.children?.map(readableText).join('') ?? ''
}
function sourceCode(node: SyntaxNode): boolean {
  return node.tagName === 'code' && /^(?:author|model|unknown)$/i.test(
    node.children?.map((child) => child.type === 'text' ? child.value ?? '' : '').join('') ?? '')
}

function sourceTag(source: SourceKey): SyntaxNode {
  const info = labels[source]
  return { type: 'element', tagName: 'span', properties: {
    className: ['markdown-source', `markdown-source--${source}`],
    title: info.explanation, dataSource: source,
  }, children: [{ type: 'text', value: info.label }] }
}
function textLength(node: SyntaxNode): number {
  return node.type === 'text' ? (node.value ?? '').length
    : node.children?.reduce((sum, child) => sum + textLength(child), 0) ?? 0
}

function provenanceMatches(text: string, sourceCell: boolean): Replacement[] {
  const matches: Replacement[] = []
  const tokens = /\b(author|model|unknown)\b/gi
  for (const match of text.matchAll(tokens)) {
    const from = match.index!
    const source = match[0].toLowerCase() as SourceKey
    const before = text.slice(0, from)
    const after = text.slice(from + match[0].length)
    const provenance = sourceCell || /(?:来源(?:标注)?|原始来源|信息来源|出处)\s*[:：=]?\s*$/.test(before)
      || /[（(]\s*$/.test(before) && /^\s*(?:[）)]|原始|本轮|作者|提供|来源|整理|建议)/.test(after)
      || source === 'model' && /^\s*(?:建议|提示|规划|提炼|整理|补丁)/.test(after)
      || source === 'unknown' && (/^\s*(?:待定|待确认|待核实|未定|[（(])/.test(after)
        || /^\s*$/.test(before) && /^\s*$/.test(after)
        || /[:：]\s*$/.test(before))
      || /来源标注\s*[:：].*$/.test(before) && /^\s*=/.test(after)
    if (!provenance) continue
    let to = from + match[0].length
    if (source === 'unknown') {
      const duplicate = after.match(/^\s*(?:待确认|待核实|待定|未定)(?=[\s，。；：、（）()!?！？，]|$)/)
      if (duplicate) to += duplicate[0].length
    }
    matches.push({ from, to, tokenTo: from + match[0].length, source })
  }
  return matches
}

function decorateBlock(block: SyntaxNode, sourceCell: boolean) {
  const text = readableText(block)
  const codeRanges: Array<{ from: number; to: number }> = []
  let codeOffset = 0
  const collectCodes = (node: SyntaxNode) => {
    if (sourceCode(node)) { codeRanges.push({ from: codeOffset, to: codeOffset + textLength(node) }); codeOffset += textLength(node); return }
    if (excluded.has(node.tagName ?? '')) { codeOffset += textLength(node); return }
    if (node.type === 'text') { codeOffset += (node.value ?? '').length; return }
    node.children?.forEach(collectCodes)
  }
  collectCodes(block)
  const matches = provenanceMatches(text, sourceCell).filter((match) => {
    if (!codeRanges.some((range) => range.from === match.from && range.to === match.tokenTo)) return true
    // Inline technical words remain code unless their containing field supplies provenance context.
    const before = text.slice(0, match.from)
    const after = text.slice(match.tokenTo)
    return sourceCell || /(?:来源(?:标注)?|原始来源|信息来源|出处)\s*[:：=]?\s*$/.test(before)
      || match.source === 'unknown' && /[:：]\s*$/.test(before)
      || /[（(]\s*$/.test(before) && /^\s*(?:[）)]|原始|本轮|作者|提供|来源|整理|建议)/.test(after)
  })
  if (!matches.length) return
  let offset = 0
  const walk = (node: SyntaxNode) => {
    if (sourceCode(node)) {
      const length = textLength(node)
      const match = matches.find((item) => item.from === offset && item.tokenTo === offset + length)
      offset += length
      if (match) Object.assign(node, sourceTag(match.source))
      return
    }
    if (excluded.has(node.tagName ?? '')) { offset += textLength(node); return }
    if (!node.children) return
    const children: SyntaxNode[] = []
    node.children.forEach((child) => {
      if (child.type !== 'text') { walk(child); children.push(child); return }
      const value = child.value ?? ''
      const start = offset
      offset += value.length
      const local = matches.filter((match) => match.from < offset && match.to > start)
      let cursor = 0
      local.forEach((match) => {
        const from = Math.max(0, match.from - start)
        // Tokens themselves are never assembled across markup boundaries.
        if (from < cursor || match.from >= start && match.tokenTo > offset) return
        if (from > cursor) children.push({ type: 'text', value: value.slice(cursor, from) })
        if (match.from >= start) children.push(sourceTag(match.source))
        cursor = Math.min(value.length, match.to - start)
      })
      if (cursor < value.length) children.push({ type: 'text', value: value.slice(cursor) })
    })
    node.children = children
  }
  walk(block)
}

/** Opt-in and context-aware: code, quotations, links, and ordinary English remain literal. */
export function rehypeSourceLabels() {
  return (tree: SyntaxNode) => {
    const visit = (node: SyntaxNode, sourceColumns: Set<number> = new Set(), sourceCell = false) => {
      if (excluded.has(node.tagName ?? '')) return
      if (node.tagName === 'table') {
        const header = node.children?.find((child) => child.tagName === 'thead')
        const row = header?.children?.find((child) => child.tagName === 'tr')
        const cells = row?.children?.filter((child) => child.tagName === 'th') ?? []
        const columns = new Set(cells.flatMap((cell, index) =>
          isSourceLabel(readableText(cell)) ? [index] : []))
        node.children?.forEach((child) => visit(child, columns))
        return
      }
      if (node.tagName === 'tr') {
        const cells = node.children?.filter((child) => child.tagName === 'td' || child.tagName === 'th') ?? []
        const sourceValueRow = cells[0]?.tagName === 'td' && isSourceLabel(readableText(cells[0]))
        let index = 0
        node.children?.forEach((child) => {
          if (child.tagName === 'td' || child.tagName === 'th') {
            const column = index++
            visit(child, sourceColumns, sourceColumns.has(column) || sourceValueRow && column === 1)
          } else visit(child, sourceColumns)
        })
        return
      }
      if (blocks.has(node.tagName ?? '')) {
        decorateBlock(node, sourceCell)
        // Lists can contain nested block elements; their already tagged text is left alone.
        if (node.tagName !== 'li') return
      }
      node.children?.forEach((child) => visit(child, sourceColumns))
    }
    visit(tree)
  }
}
