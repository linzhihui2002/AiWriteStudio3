import type { UsageSummary } from '../api/types'

export type DailyUsage = UsageSummary['by_day'][number]

/** Fill the backend's inclusive calendar window without local DST offsets. */
export function buildDailyUsage(rows: DailyUsage[], days: number, today?: Date): DailyUsage[] {
  const now = new Date()
  const end = today ?? new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()))
  const count = Math.max(1, Math.floor(days))
  const endTime = Date.UTC(end.getUTCFullYear(), end.getUTCMonth(), end.getUTCDate())
  const records = new Map(rows.map(row => [row.day, row]))
  return Array.from({ length: count }, (_, index) => {
    const day = new Date(endTime - (count - index - 1) * 86_400_000).toISOString().slice(0, 10)
    return records.get(day) ?? { day, tokens: 0, prompt: 0, completion: 0, cost: 0, calls: 0 }
  })
}

/** Use a readable 1 / 2 / 5 / 10 scale for integer Token counts. */
export function getChartCeiling(max: number): number {
  if (!Number.isFinite(max) || max <= 1) return 1
  const magnitude = 10 ** Math.floor(Math.log10(max))
  const scaled = max / magnitude
  return magnitude * (scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10)
}

const TASK_TYPE_LABELS: Record<string, string> = {
  embedding: '知识向量化',
  knowledge_answer: '知识库问答',
  'Agent 生成': '智能体生成',
  '去AI味重写': '去 AI 味重写',
  '去AI味软审': '去 AI 味审稿',
}

export function formatTaskType(type: string | null | undefined): string {
  const value = type?.trim() ?? ''
  return TASK_TYPE_LABELS[value] ?? (value || '未分类')
}

const TASK_STATUS_LABELS: Record<string, string> = {
  pending: '待执行',
  running: '进行中',
  done: '已完成',
  success: '已完成',
  completed: '已完成',
  failed: '失败',
  error: '失败',
  cancelled: '已取消',
  canceled: '已取消',
}

export function formatTaskStatus(status: string | null | undefined): string {
  const value = status?.trim() ?? ''
  return TASK_STATUS_LABELS[value] ?? (value || '未知状态')
}
