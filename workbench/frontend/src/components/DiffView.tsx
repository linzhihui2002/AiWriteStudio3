/** 行级差异视图：应用提案 / 外部改动冲突时展示（+ 新增、- 删除、无标记为未变）。 */

import type { DiffLine } from '../api/types'

export interface DiffViewProps {
  lines: DiffLine[]
  /** 只显示变更附近的上下文行（默认 6 行） */
  context?: number
  emptyHint?: string
}

export default function DiffView({ lines, context = 6, emptyHint = '无差异' }: DiffViewProps) {
  if (!lines || lines.length === 0) {
    return <p className="muted">{emptyHint}</p>
  }

  const keep = new Set<number>()
  lines.forEach((line, index) => {
    if (line.type === 'equal') return
    for (let offset = -context; offset <= context; offset += 1) {
      const target = index + offset
      if (target >= 0 && target < lines.length) keep.add(target)
    }
  })

  const visible = lines.filter((_, index) => keep.has(index))

  return (
    <div className="diff" role="table" aria-label="差异对比">
      {visible.map((line, index) => (
        <div key={`${line.line}-${index}`} className={`diff__line diff__line--${line.type}`}>
          <span className="diff__no">{line.type === 'add' ? '' : line.line}</span>
          <span className="diff__sign">
            {line.type === 'add' ? '+' : line.type === 'remove' ? '−' : ' '}
          </span>
          <span className="diff__text">{line.text || '\u00a0'}</span>
        </div>
      ))}
    </div>
  )
}