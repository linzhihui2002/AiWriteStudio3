import type { SoftDeslopResult } from '../api/types'

export type ReviewMode = 'review' | 'deslop' | 'ingest'
export const REVIEW_MODES: ReadonlyArray<{ key: ReviewMode; label: string; description: string; action: string }> = [
  { key: 'review', label: '逐项审稿', action: '开始逐项审稿', description: '对照章节合同与硬门禁验收，展示正文依据和待核实项。' },
  { key: 'deslop', label: '去AI味软审', action: '开始去AI味软审', description: '检查套话、重复解释与表达节奏，建议进入收件箱，由你核对后应用。' },
  { key: 'ingest', label: '提取四件套', action: '开始提取四件套', description: '从当前已保存正文整理角色状态、时间线、资源账本和伏笔，用于下一章承接与连续性检查。' },
]

export function reportVersion(currentHash?: string, reportHash?: string, sourceChanged = false): 'current' | 'stale' | 'unknown' {
  if (sourceChanged) return 'stale'
  if (!currentHash || !reportHash) return 'unknown'
  return currentHash === reportHash ? 'current' : 'stale'
}

export function softReviewMessage(data: SoftDeslopResult): string {
  if (!data.ai_used) return data.ai_error || 'AI 软审未完成，尚未获得可核验的表达建议。'
  if (data.source_changed) return '正文已变化，建议对应旧版本，本次没有创建新提案。请重新软审当前正文。'
  if (data.proposal_ids.length) return `去AI味软审完成：${data.proposal_ids.length} 条提案已进入收件箱。`
  if (data.rejected_suggestions?.length) return '本次建议未通过原文核验，没有创建提案。'
  return '本次软审已完成，未提出表达修改建议。'
}

export function rejectedSuggestionCount(data: SoftDeslopResult): number {
  return (data.rejected_suggestions || []).reduce((total, item) => total +
    (typeof item.count === 'number' && Number.isFinite(item.count) && item.count > 0 ? Math.floor(item.count) : 1), 0)
}
