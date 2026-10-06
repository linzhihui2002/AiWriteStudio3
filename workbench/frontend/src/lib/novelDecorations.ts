/** Pure display ranges. Offsets are UTF-16 positions in the unchanged document. */
export interface NovelColors { light: string; dark: string; paper: string }
export interface NovelEntity {
  entity_id: string
  name: string
  aliases: string[]
  highlight_mode: 'auto' | 'manual' | 'off'
  highlight_colors: NovelColors
}
export interface NovelMark {
  from: number
  to: number
  kind: 'name' | 'dialogue'
  entityId?: string
  name?: string
  colors?: NovelColors
}
export interface NovelMilestone { at: number; count: number }
export interface NovelDecorations { marks: NovelMark[]; milestones: NovelMilestone[] }
export interface NovelDecorationOptions {
  entities?: NovelEntity[]
  milestone?: { enabled: boolean; step: number }
}

const escapeRegex = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

export function buildNovelDecorations(text: string, options: NovelDecorationOptions = {}): NovelDecorations {
  const marks: NovelMark[] = []
  const milestones: NovelMilestone[] = []
  // An unclosed quote cannot bleed into the next physical paragraph.
  const quotes = /“[^“”\r\n]*”|「[^「」\r\n]*」|『[^『』\r\n]*』/g
  for (const match of text.matchAll(quotes)) {
    marks.push({ from: match.index!, to: match.index! + match[0].length, kind: 'dialogue' })
  }
  const dictionary = new Map<string, Map<string, NovelEntity>>()
  for (const entity of options.entities || []) {
    if (entity.highlight_mode === 'off' || !entity.highlight_colors?.light) continue
    for (const raw of [entity.name, ...(entity.aliases || [])]) {
      const name = raw.trim()
      if (!name || /[\r\n]/.test(name)) continue
      const objects = dictionary.get(name) || new Map<string, NovelEntity>()
      objects.set(entity.entity_id, entity)
      dictionary.set(name, objects)
    }
  }
  const terms = [...dictionary.keys()].sort((a, b) => b.length - a.length || a.localeCompare(b))
  if (terms.length) {
    const names = new RegExp(terms.map(escapeRegex).join('|'), 'g')
    for (const match of text.matchAll(names)) {
      const word = match[0]
      const from = match.index!
      const to = from + word.length
      if (/[A-Za-z0-9_]/.test(word[0]) && /[A-Za-z0-9_]/.test(text[from - 1] || '')) continue
      if (/[A-Za-z0-9_]$/.test(word) && /[A-Za-z0-9_]/.test(text[to] || '')) continue
      const objects = dictionary.get(word)!
      if (objects.size !== 1) continue // Consume an ambiguous term without guessing.
      const entity = objects.values().next().value!
      marks.push({ from, to, kind: 'name', entityId: entity.entity_id, name: entity.name, colors: entity.highlight_colors })
    }
  }
  const { enabled = false, step = 0 } = options.milestone || {}
  if (enabled && Number.isFinite(step) && step > 0) {
    const interval = Math.max(1, Math.floor(step))
    let written = 0
    let shown = 0
    let offset = 0
    // Stage markers belong to the next display row after a physical paragraph.
    // A long paragraph can cross several stages; show only its latest stage.
    for (const match of text.matchAll(/([^\r\n]*)(\r\n|\r|\n|$)/g)) {
      const paragraph = match[1]
      written += paragraph.replace(/\s/g, '').length
      const stage = Math.floor(written / interval) * interval
      if (paragraph.trim() && stage > shown) {
        milestones.push({ at: offset + paragraph.length, count: stage })
        shown = stage
      }
      offset += match[0].length
    }
  }
  marks.sort((a, b) => a.from - b.from || b.to - a.to || a.kind.localeCompare(b.kind))
  return { marks, milestones }
}

export function replaceSelectedText(text: string, selection: { start: number; end: number }, expected: string, replacement: string): string | null {
  const { start, end } = selection
  if (start < 0 || end < start || end > text.length || text.slice(start, end) !== expected) return null
  return text.slice(0, start) + replacement + text.slice(end)
}
