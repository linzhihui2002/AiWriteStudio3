import type { ProseHistorySource, ProseLocation } from '../api/types'

export interface ProseAdvice {
  kind: string
  evidence: string
  location: ProseLocation | null
  related: ProseLocation[]
  relatedSources: ProseHistorySource[]
  reason: string
  suggestion: string
  source: 'diagnostic' | 'model' | 'history'
  textTruncated: boolean
}

export interface ProseAdviceReport {
  available: boolean
  findings: ProseAdvice[]
  truncated: boolean
  history: {
    available: boolean
    status: string
    checkedSources: number
    excluded: Array<{ rel_path: string; reason: string }>
    degradation: string[]
  }
}

function object(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : null
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function location(value: unknown): ProseLocation | null {
  const item = object(value)
  if (!item || typeof item.line !== 'number' || !Number.isInteger(item.line) || item.line < 1) return null
  const column = typeof item.column === 'number' && Number.isInteger(item.column) && item.column > 0 ? item.column : 1
  const endLine = typeof item.end_line === 'number' && Number.isInteger(item.end_line) && item.end_line >= item.line
    ? item.end_line : item.line
  return { line: item.line, column, end_line: endLine,
    ...(typeof item.end_column === 'number' && Number.isInteger(item.end_column) && item.end_column > 0
      ? { end_column: item.end_column } : {}) }
}

/** Only presentation data: never turn expression suggestions into acceptance failures. */
export function proseAdviceReport(quality: unknown, modelSuggestions: unknown, historyQuality?: unknown): ProseAdviceReport {
  const report = object(quality)
  const available = report?.version === 1 && report.blocking === false && Array.isArray(report.findings)
  const findings: ProseAdvice[] = []
  if (available) {
    for (const raw of report.findings as unknown[]) {
      const item = object(raw)
      if (!item || item.severity !== 'warning' || !text(item.text) || !text(item.reason)) continue
      findings.push({ kind: text(item.kind), evidence: text(item.text), location: location(item),
        related: Array.isArray(item.related_locations)
          ? item.related_locations.map(location).filter((value): value is ProseLocation => value !== null) : [],
        relatedSources: [], reason: text(item.reason), suggestion: text(item.suggestion), source: 'diagnostic',
        textTruncated: item.text_truncated === true })
    }
  }
  if (Array.isArray(modelSuggestions)) {
    for (const raw of modelSuggestions) {
      const item = object(raw)
      if (!item || !text(item.evidence) || !text(item.reason)) continue
      findings.push({ kind: text(item.kind), evidence: text(item.evidence), location: location(item),
        related: [], relatedSources: [], reason: text(item.reason), suggestion: text(item.suggestion), source: 'model',
        textTruncated: false })
    }
  }
  const history = object(historyQuality)
  const historyAvailable = history?.version === 1 && history.blocking === false && Array.isArray(history.findings)
  const historyStatus = historyAvailable ? text(history.status) || 'available' : 'missing'
  if (historyAvailable && historyStatus !== 'not_checked') {
    for (const raw of history.findings as unknown[]) {
      const item = object(raw)
      if (!item || item.severity !== 'warning' || !text(item.text) || !text(item.reason)
        || !['cross_chapter_paragraph', 'cross_chapter_sentence'].includes(text(item.kind))) continue
      const relatedSources = Array.isArray(item.related_sources) ? item.related_sources.flatMap(rawSource => {
        const source = object(rawSource)
        const sourceLocation = location(source)
        if (!source || !sourceLocation || !text(source.rel_path) || !text(source.content_hash)) return []
        return [{ ...sourceLocation,
          rel_path: text(source.rel_path), content_hash: text(source.content_hash),
          text: text(source.text), text_truncated: source.text_truncated === true }]
      }) : []
      if (relatedSources.length === 0) continue
      findings.push({ kind: text(item.kind), evidence: text(item.text), location: location(item),
        related: [], relatedSources, reason: text(item.reason), suggestion: text(item.suggestion),
        source: 'history', textTruncated: item.text_truncated === true })
    }
  }
  const checkedSources = historyAvailable && Array.isArray(history.sources) ? history.sources.filter(raw => {
    const source = object(raw)
    return source && text(source.rel_path) && text(source.content_hash)
  }).length : new Set(findings.filter(finding => finding.source === 'history')
    .flatMap(finding => finding.relatedSources.map(source => source.rel_path))).size
  const excluded = historyAvailable && Array.isArray(history.excluded) ? history.excluded.flatMap(raw => {
    const item = object(raw)
    return item && text(item.rel_path) && text(item.reason)
      ? [{ rel_path: text(item.rel_path), reason: text(item.reason) }] : []
  }) : []
  return { available: available || findings.length > 0 || historyAvailable, findings,
    truncated: (available && report.truncated === true) || (historyAvailable && history.truncated === true),
    history: { available: historyAvailable, status: historyStatus, checkedSources, excluded,
      degradation: historyAvailable && Array.isArray(history.degradation) ? history.degradation.map(text).filter(Boolean) : [] } }
}

export function proseLocationLabel(value: ProseLocation | null): string {
  if (!value) return '位置未提供'
  if (value.end_column) return value.end_line > value.line
    ? `第 ${value.line}—${value.end_line} 行（起始第 ${value.column} 列，结束第 ${value.end_column} 列）`
    : `第 ${value.line} 行，第 ${value.column}—${value.end_column} 列`
  return value.end_line > value.line
    ? `第 ${value.line}—${value.end_line} 行（起始第 ${value.column} 列）`
    : `第 ${value.line} 行，第 ${value.column} 列`
}

export function proseKindLabel(kind: string): string {
  const labels: Record<string, string> = {
    repeated_paragraph: '段落重复', repeated_sentence: '句子重复',
    cross_chapter_paragraph: '跨章段落重复', cross_chapter_sentence: '跨章句子重复',
    exact_paragraph_repeat: '段落重复', exact_sentence_repeat: '句子重复',
    repetition: '表达重复', explanation: '解释过多', over_explanation: '解释过多',
    summary: '段尾总结', dialogue: '对白表达', same_voice: '人物说话相似',
  }
  return labels[kind] || (/[\u4e00-\u9fff]/.test(kind) ? kind : '表达观察')
}

export function proseHistoryStatusLabel(history: ProseAdviceReport['history']): string {
  if (!history.available) return '本次结果未包含跨章重复检查报告。'
  if (history.status === 'not_checked') return '本次未检查跨章重复，不能据此判断历史正文是否重复。'
  if (history.status === 'degraded') return history.checkedSources > 0
    ? '跨章检查范围不完整，以下结果仅来自可用的此前章节正文。'
    : '历史正文暂不可用，未完成跨章核对。'
  if (history.status === 'empty' || history.checkedSources === 0) return '本次没有可核对的此前章节正文。'
  return `本次核对了 ${history.checkedSources} 份此前章节正文；检查范围不代表全书。`
}
