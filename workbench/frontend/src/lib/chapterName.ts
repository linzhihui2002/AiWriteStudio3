/**
 * 章节显示名的事实源：序号固定，标题可选。
 * 文档树 / 编辑器章节下拉 / 审稿页共用同一口径，避免同一章在不同位置显示成不同名字。
 */

export interface ChapterNameSource {
  number?: number | null
  title?: string | null
  file_name?: string
  name?: string
  word_count?: number | null
}

/** 「第0001章 · 起风」；标题为空时只返回「第0001章」。 */
export function chapterLabel(source: ChapterNameSource): string {
  const number = source.number
  const base =
    typeof number === 'number' && number > 0
      ? `第${String(number).padStart(4, '0')}章`
      : (source.file_name ?? source.name ?? '').replace(/\.(?:md|txt)$/i, '')
  const title = (source.title ?? '').trim()
  return title ? `${base} · ${title}` : base
}

/** 「第0001章 · 起风 · 0 字」。 */
export function chapterLabelWithCount(source: ChapterNameSource): string {
  return `${chapterLabel(source)} · ${source.word_count ?? 0} 字`
}