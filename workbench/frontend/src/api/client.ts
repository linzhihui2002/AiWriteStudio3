/**
 * API 客户端 —— 基于 fetch 的薄封装 + 全部业务端点。
 *
 * baseURL 固定为 `/api`（开发期由 Vite proxy 转发到 http://127.0.0.1:8790，
 * 生产期由 FastAPI 静态托管同源提供），因此不在代码中硬编码主机与端口。
 */

import type {
  AgentItem,
  BibleEntity,
  BibleOverview,
  CardList,
  CardHighlightEntry,
  CardParseTask,
  ChapterDetail,
  ChapterSummary,
  ChatReply,
  ChatSession,
  ChatSessionDetail,
  ChatContext, ChatDefaults,
  WritingPrefs,
  ChatRun,
  ChatRunEvent,
  ChatFileChange,
  ChatInteractionResponse, ChatInteraction,
  ChatMemoryEntry,
  ChatPermissionMode,
  ConflictCheck,
  ContextPreview,
  Contract,
  DeleteResult,
  DefaultTarget,
  Foreshadow,
  GateCheck,
  Health,
  ImageJob,
  ImageProvider,
  ImageRecord,
  OperationLogEntry,
  OutlineCandidate,
  OutlineState,
  PipelineResult,
  Project,
  ProjectTree,
  ProposalItem,
  Provider,
  QualityMatrix,
  ReviewResult,
  SoftDeslopResult,
  RoutingDecision,
  RoutingInfo,
  Settings,
  SkillItem,
  StyleFingerprint,
  StyleReferenceMetrics,
  IngestionResult,
  TaskRecord,
  Template,
  TemplateFile,
  TemplatePackage,
  TemplatePrefs,
  TemplateTree,
  TimelineEntry,
  TrashEntry,
  TreeNode,
  UsageSummary,
  VectorStats,
  WorkflowDefinition,
  WorkflowRun,
} from './types'

const API_BASE = '/api'

/** 归一化后的 API 错误：status=0 表示网络层失败（后端未启动等）。 */
export class ApiError extends Error {
  readonly status: number
  readonly code?: string

  constructor(message: string, status: number, code?: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  headers?: Record<string, string>
  signal?: AbortSignal
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

async function toApiError(response: Response): Promise<ApiError> {
  let message = ''
  let code: string | undefined
  try {
    const data: unknown = await response.json()
    if (data && typeof data === 'object') {
      const record = data as Record<string, unknown>
      if (typeof record.detail === 'string') message = record.detail
      else if (typeof record.message === 'string') message = record.message
      if (typeof record.code === 'string') code = record.code
    }
  } catch {
    // 响应体不是 JSON，使用状态码兜底文案
  }
  return new ApiError(message || `请求失败（HTTP ${response.status}）`, response.status, code)
}

/** 发起一次 API 请求：自动 JSON 编解码，失败统一抛 ApiError。 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', ...options.headers }
  let body: string | undefined
  if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method: options.method ?? 'GET',
      headers,
      body,
      signal: options.signal,
    })
  } catch (error) {
    if (isAbortError(error)) throw error
    throw new ApiError('无法连接本地服务（127.0.0.1:8790），请确认后端已启动。', 0, 'NETWORK_ERROR')
  }

  if (!response.ok) throw await toApiError(response)
  if (response.status === 204) return undefined as T

  const text = await response.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    return text as unknown as T
  }
}

function qs(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return
    search.set(key, String(value))
  })
  const text = search.toString()
  return text ? `?${text}` : ''
}

/** SSE 流读取：逐条解析 `data: {json}` 事件。 */
export async function readSSE(
  path: string,
  body: unknown,
  onEvent: (payload: Record<string, unknown>) => void,
  signal?: AbortSignal,
  method: 'POST' | 'GET' = 'POST',
  onFrame?: () => void,
): Promise<void> {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: method === 'GET' ? undefined : JSON.stringify(body ?? {}),
    signal,
  })
  if (!response.ok) throw await toApiError(response)
  if (!response.body) throw new ApiError('流式响应为空（浏览器不支持 ReadableStream）', 0)

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  const dispatch = (part: string) => {
    onFrame?.()
    const data = part.split(/\r?\n/).filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart()).join('\n')
    if (!data) return
    let payload: Record<string, unknown>
    try { payload = JSON.parse(data) as Record<string, unknown> } catch { return }
    onEvent(payload)
  }
  try {
    for (;;) {
      const { value, done } = await reader.read()
      buffer += done ? decoder.decode() : decoder.decode(value, { stream: true })
      const parts = buffer.split(/\r?\n\r?\n/)
      buffer = parts.pop() ?? ''
      parts.forEach(dispatch)
      if (done) {
        if (buffer.trim()) dispatch(buffer)
        break
      }
    }
  } finally {
    reader.releaseLock()
  }
}

// ─────────────────────────── 健康 / 设置 ───────────────────────────

export const getHealth = (signal?: AbortSignal) => request<Health>('/health', { signal })

export const getSettings = () => request<Settings>('/settings')

export const updateSettings = (patch: Record<string, unknown>) =>
  request<Settings>('/settings', { method: 'PUT', body: { patch } })

export const resetSettings = () => request<Settings>('/settings/reset', { method: 'POST' })

// ─────────────────────────── 项目 ───────────────────────────

export interface CreateProjectInput {
  name: string
  genre?: string
  platform?: string
  protagonist?: string
  one_liner?: string
  template?: string | null
}

export const listProjects = (includeArchived = false) =>
  request<Project[]>(`/projects${qs({ include_archived: includeArchived })}`)

export const createProject = (input: CreateProjectInput) =>
  request<Project>('/projects', { method: 'POST', body: input })

export const getProject = (id: number) => request<Project>(`/projects/${id}`)

export interface UpdateProjectInput {
  name?: string
  genre?: string
  platform?: string
  protagonist?: string
  one_liner?: string
}

export const updateProject = (id: number, patch: UpdateProjectInput) =>
  request<Project>(`/projects/${id}`, { method: 'PATCH', body: patch })

/** 上传封面：请求体为原始图片字节（不走 JSON request）。 */
export async function uploadCover(projectId: number, file: File): Promise<{ has_cover: boolean }> {
  const response = await fetch(`${API_BASE}/projects/${projectId}/cover`, {
    method: 'PUT',
    headers: { 'Content-Type': file.type || 'application/octet-stream' },
    body: file,
  })
  if (!response.ok) throw await toApiError(response)
  return (await response.json()) as { has_cover: boolean }
}

export const deleteCover = (projectId: number) =>
  request<{ has_cover: boolean }>(`/projects/${projectId}/cover`, { method: 'DELETE' })

export const archiveProject = (id: number, archived = true) =>
  request<Project>(`/projects/${id}/archive`, { method: 'POST', body: { archived } })

/** 删除整本书：默认移入回收站（可恢复）；permanent=true 彻底删除。 */
export const deleteProject = (id: number, permanent = false) =>
  request<{ id: number; name: string; deleted: boolean; purged: boolean }>(
    `/projects/${id}${qs({ permanent: permanent || undefined })}`,
    { method: 'DELETE' },
  )

// ─────────────────────────── 文档树 / 章节 ───────────────────────────

export const getTree = (projectId: number) => request<ProjectTree>(`/projects/${projectId}/tree`)

export const createNode = (projectId: number, parent_rel: string, name: string, is_dir: boolean) =>
  request<TreeNode>(`/projects/${projectId}/tree/node`, {
    method: 'POST',
    body: { parent_rel, name, is_dir },
  })

export const patchNode = (
  projectId: number,
  payload: { rel_path: string; new_name?: string; dst_parent_rel?: string },
) => request<TreeNode>(`/projects/${projectId}/tree/node`, { method: 'PATCH', body: payload })

export const deleteNode = (projectId: number, relPath: string) =>
  request<DeleteResult>(`/projects/${projectId}/tree/node${qs({ rel_path: relPath })}`, {
    method: 'DELETE',
  })

export const deleteNodes = (projectId: number, relPaths: string[]) =>
  request<DeleteResult[]>(`/projects/${projectId}/tree/delete-batch`, {
    method: 'POST',
    body: { rel_paths: relPaths },
  })

export const listChapters = (projectId: number) =>
  request<ChapterSummary[]>(`/projects/${projectId}/chapters`)

export const createChapter = (projectId: number, title?: string) =>
  request<ChapterDetail>(`/projects/${projectId}/chapters`, {
    method: 'POST',
    body: { title: title ?? null },
  })

export const readChapter = (projectId: number, relPath: string) =>
  request<ChapterDetail>(`/projects/${projectId}/chapters/content${qs({ rel_path: relPath })}`)

export const saveChapter = (
  projectId: number,
  relPath: string,
  content: string,
  status?: string,
  expectedHash?: string,
) =>
  request<ChapterDetail>(`/projects/${projectId}/chapters/content`, {
    method: 'PUT',
    body: { rel_path: relPath, content, status: status ?? null, expected_hash: expectedHash },
  })

export const updateChapterMeta = (
  projectId: number,
  payload: { rel_path: string; title?: string; status?: string },
) =>
  request<ChapterDetail>(`/projects/${projectId}/chapters`, {
    method: 'PATCH',
    body: payload,
  })

export const deleteChapter = (projectId: number, relPath: string) =>
  request<DeleteResult>(`/projects/${projectId}/chapters${qs({ rel_path: relPath })}`, {
    method: 'DELETE',
  })

// ─────────────────────────── 回收站 / 快照 / 冲突 ───────────────────────────

/** 回收站总表：整本书（软删除）+ 各项目的文件/文件夹条目。 */
export const listTrash = () => request<TrashEntry[]>('/trash')

export const restoreTrash = (trashRel: string) =>
  request<{ rel_path: string; restored_from: string }>('/trash/restore', {
    method: 'POST',
    body: { trash_rel: trashRel },
  })

/** 从回收站恢复整本书。 */
export const restoreProject = (id: number) =>
  request<{ rel_path: string; restored_from: string }>(`/projects/${id}/restore`, {
    method: 'POST',
  })

export const listSnapshots = (projectId: number, relPath?: string) =>
  request<Array<{ id: number; rel_path: string; reason: string; created_at: string }>>(
    `/projects/${projectId}/snapshots${qs({ rel_path: relPath })}`,
  )

export const restoreSnapshot = (projectId: number, snapshotId: number) =>
  request<{ rel_path: string }>(`/projects/${projectId}/snapshots/restore`, {
    method: 'POST',
    body: { snapshot_id: snapshotId },
  })

export const checkConflict = (
  projectId: number,
  payload: { rel_path: string; base_mtime?: number; base_hash?: string; current_content?: string },
) => request<ConflictCheck>(`/projects/${projectId}/conflict/check`, { method: 'POST', body: payload })

export const resolveConflict = (
  projectId: number,
  payload: { rel_path: string; mode: 'keep-mine' | 'take-external'; content?: string },
) => request<Record<string, unknown>>(`/projects/${projectId}/conflict/resolve`, {
  method: 'POST',
  body: payload,
})

// ─────────────────────────── 索引 / 模板 / 导入导出 ───────────────────────────

export const rebuildIndex = (projectId: number) =>
  request<Record<string, unknown>>(`/projects/${projectId}/index/rebuild`, { method: 'POST' })

export const indexStats = (projectId: number) =>
  request<{ documents: number; words: number; by_kind: Record<string, { count: number; words: number }>; fts: boolean }>(
    `/projects/${projectId}/index/stats`,
  )

export const searchDocuments = (projectId: number, q: string, limit = 20) =>
  request<Array<{ rel_path: string; kind: string; snippet: string; source: string }>>(
    `/projects/${projectId}/search${qs({ q, limit })}`,
  )

export const listTemplates = () => request<Template[]>('/templates')

export const saveAsTemplate = (name: string, projectId: number) =>
  request<Template>('/templates', { method: 'POST', body: { name, project_id: projectId } })

export const deleteTemplate = (name: string) =>
  request<{ name: string }>(`/templates/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const duplicateTemplate = (name: string, newName: string) =>
  request<Template>(`/templates/${encodeURIComponent(name)}/duplicate`, {
    method: 'POST',
    body: { new_name: newName },
  })

export const createBlankTemplate = (name: string) =>
  request<Template>('/templates/blank', { method: 'POST', body: { name } })

export const renameTemplate = (name: string, newName: string) =>
  request<Template>(`/templates/${encodeURIComponent(name)}`, {
    method: 'PATCH',
    body: { new_name: newName },
  })

export const getTemplatePrefs = () => request<TemplatePrefs>('/templates/prefs')

export const updateTemplatePrefs = (patch: Partial<TemplatePrefs>) =>
  request<TemplatePrefs>('/templates/prefs', { method: 'PUT', body: patch })

export const importTemplate = (payload: TemplatePackage, newName?: string) =>
  request<Template>('/templates/import', {
    method: 'POST',
    body: { payload, new_name: newName },
  })

export const exportTemplate = (name: string) =>
  request<TemplatePackage>(`/templates/${encodeURIComponent(name)}/export`)

export const getTemplateTree = (name: string) =>
  request<TemplateTree>(`/templates/${encodeURIComponent(name)}/tree`)

export const readTemplateFile = (name: string, relPath: string) =>
  request<TemplateFile>(
    `/templates/${encodeURIComponent(name)}/file${qs({ rel_path: relPath })}`,
  )

export const writeTemplateFile = (name: string, relPath: string, content: string) =>
  request<TemplateFile>(`/templates/${encodeURIComponent(name)}/file`, {
    method: 'PUT',
    body: { rel_path: relPath, content },
  })

export const createTemplateNode = (
  name: string,
  parentRel: string,
  nodeName: string,
  isDir: boolean,
) =>
  request(`/templates/${encodeURIComponent(name)}/node`, {
    method: 'POST',
    body: { parent_rel: parentRel, name: nodeName, is_dir: isDir },
  })

export const patchTemplateNode = (
  name: string,
  relPath: string,
  patch: { new_name?: string; dst_parent_rel?: string },
) =>
  request(`/templates/${encodeURIComponent(name)}/node`, {
    method: 'PATCH',
    body: { rel_path: relPath, ...patch },
  })

export const deleteTemplateNode = (name: string, relPath: string) =>
  request(`/templates/${encodeURIComponent(name)}/node${qs({ rel_path: relPath })}`, {
    method: 'DELETE',
  })

export const exportChapters = (
  projectId: number,
  scope: 'body' | 'with_title' = 'with_title',
  format: 'md' | 'txt' = 'md',
) =>
  request<{ filename: string; content: string; chapter_count: number; word_count: number }>(
    `/projects/${projectId}/export${qs({ scope, format })}`,
  )

export const importDocument = (
  projectId: number,
  payload: { filename: string; content: string; title?: string; as_single_chapter?: boolean },
) => request<{ count: number; word_count: number; created: Array<{ rel_path: string; title: string }> }>(
  `/projects/${projectId}/import`,
  { method: 'POST', body: payload },
)

export const exportPackage = (projectId: number) =>
  request<Record<string, unknown>>(`/projects/${projectId}/export/package`)

export const restorePackage = (payload: Record<string, unknown>, newName?: string) =>
  request<Record<string, unknown>>(`/projects/import/package${qs({ new_name: newName })}`, {
    method: 'POST',
    body: payload,
  })

export const projectLog = (projectId: number, limit = 50) =>
  request<OperationLogEntry[]>(`/projects/${projectId}/log${qs({ limit })}`)

// ─────────────────────────── 模型供应商 / 引擎 ───────────────────────────

export const listProviders = () =>
  request<{
    providers: Provider[]
    capability_tags: string[]
    cipher: string
    defaults: DefaultTarget
  }>('/providers')

export const upsertProvider = (payload: Record<string, unknown>) =>
  request<Provider>('/providers', { method: 'POST', body: payload })

export const setDefaultProvider = (payload: DefaultTarget) =>
  request<DefaultTarget>('/providers/default', { method: 'PUT', body: payload })

export const deleteProvider = (providerId: string) =>
  request<{ provider_id: string }>(`/providers/${encodeURIComponent(providerId)}`, {
    method: 'DELETE',
  })

export const enableProvider = (providerId: string, enabled: boolean) =>
  request<Provider>(`/providers/${encodeURIComponent(providerId)}/enable`, {
    method: 'POST',
    body: { enabled },
  })

export const healthCheck = (providerId: string) =>
  request<{ ok: boolean; reason?: string; models?: string[]; model_count?: number }>(
    '/providers/health',
    { method: 'POST', body: { provider_id: providerId } },
  )

export const listModels = (payload: { provider_id?: string; base_url?: string; api_key?: string }) =>
  request<{ models: Array<{ id: string }>; count: number }>('/providers/models', {
    method: 'POST',
    body: payload,
  })

export const projectProviders = () =>
  request<Record<string, unknown>>('/providers/project', { method: 'POST' })

export const importProvidersYaml = (yamlText: string) =>
  request<{ imported: Provider[]; note: string }>('/providers/import', {
    method: 'POST',
    body: { yaml_text: yamlText },
  })

export const importModelsMd = () =>
  request<{ migrated: Array<{ secret_ref: string; masked: string }>; found: number; note: string }>(
    '/providers/import-models-md',
    { method: 'POST' },
  )

export const getEngines = () => request<RoutingInfo>('/engines')

export const updateEngines = (patch: Record<string, unknown>) =>
  request<RoutingInfo>('/engines', { method: 'PUT', body: { patch } })

export const getProjectEngines = (projectId: number) =>
  request<{ config: Record<string, unknown>; effective: Record<string, unknown> }>(
    `/projects/${projectId}/engines`,
  )

export const updateProjectEngines = (projectId: number, patch: Record<string, unknown>) =>
  request<Record<string, unknown>>(`/projects/${projectId}/engines`, {
    method: 'PUT',
    body: patch,
  })

export const listTasks = (projectId?: number, limit = 50) =>
  request<TaskRecord[]>(`/tasks${qs({ project_id: projectId, limit })}`)

export const cancelTask = (taskId: number) =>
  request<Record<string, unknown>>(`/tasks/${taskId}/cancel`, { method: 'POST' })

export const getUsage = (projectId?: number, days = 30) =>
  request<UsageSummary>(`/usage${qs({ project_id: projectId, days })}`)

// ─────────────────────────── 上下文 / 合同 / 管线 ───────────────────────────

export const previewContext = (projectId: number, payload: Record<string, unknown>) =>
  request<{ preview: ContextPreview; total_tokens: number; budget_tokens: number; duration_ms: number }>(
    `/projects/${projectId}/context/preview`,
    { method: 'POST', body: payload },
  )

export const listContracts = (projectId: number) =>
  request<Array<Contract & { chapter_rel: string }>>(`/projects/${projectId}/contracts`)

export const generateContract = (projectId: number, chapterRel: string, useAi = true) =>
  request<Contract>(`/projects/${projectId}/contracts/generate`, {
    method: 'POST',
    body: { chapter_rel: chapterRel, use_ai: useAi },
  })

export const getContract = (projectId: number, chapterRel: string) =>
  request<Contract>(`/projects/${projectId}/contracts/detail${qs({ chapter_rel: chapterRel })}`)

export const updateContract = (
  projectId: number,
  chapterRel: string,
  patch: Record<string, unknown>,
  confirm = false,
) =>
  request<Contract>(`/projects/${projectId}/contracts`, {
    method: 'PATCH',
    body: { chapter_rel: chapterRel, patch, confirm },
  })

export const freezeContract = (projectId: number, chapterRel: string) =>
  request<Contract>(`/projects/${projectId}/contracts/freeze`, {
    method: 'POST',
    body: { chapter_rel: chapterRel, confirm: true },
  })

export const checkGates = (projectId: number, chapterRel: string) =>
  request<{ prerequisites: { checks: GateCheck[]; blocked: boolean; reasons: string[] }; contract: { ok: boolean; hint: string } }>(
    `/projects/${projectId}/gates${qs({ chapter_rel: chapterRel })}`,
  )

export const freezeOutline = (projectId: number) =>
  request<Record<string, unknown>>(`/projects/${projectId}/outline/freeze`, { method: 'POST' })

export const unfreezeOutline = (projectId: number) =>
  request<Record<string, unknown>>(`/projects/${projectId}/outline/unfreeze`, { method: 'POST' })

export const runPipeline = (projectId: number, payload: Record<string, unknown>) =>
  request<PipelineResult>(`/projects/${projectId}/pipeline/run`, { method: 'POST', body: payload })

export const listPipelineCheckpoints = (projectId: number) =>
  request<Array<Record<string, unknown>>>(`/projects/${projectId}/pipeline/checkpoints`)

// ─────────────────────────── 收件箱 ───────────────────────────

export const listProposals = (projectId?: number, status: string | null = 'pending') =>
  request<ProposalItem[]>(`/proposals${qs({ project_id: projectId, status })}`)

export const proposalCount = (projectId?: number) =>
  request<{ pending: number }>(`/proposals/count${qs({ project_id: projectId })}`)

export const getProposal = (id: number) => request<ProposalItem>(`/proposals/${id}`)

export const updateProposal = (id: number, content: string) =>
  request<ProposalItem>(`/proposals/${id}`, { method: 'PUT', body: { content } })

export const rebaseStateProposal = (id: number) =>
  request<{ proposal_id: number | null; already_current: boolean; proposal: ProposalItem | null }>(
    `/proposals/${id}/rebase-state`, { method: 'POST' })

export const reviewProposal = (id: number, useAi = true) =>
  request<ReviewResult>(`/proposals/${id}/review`, { method: 'POST', body: { use_ai: useAi } })

export const applyProposal = (id: number, content?: string, status?: string) =>
  request<Record<string, unknown>>(`/proposals/${id}/apply`, {
    method: 'POST',
    body: { content: content ?? null, status: status ?? null },
  })

export const discardProposal = (id: number) =>
  request<ProposalItem>(`/proposals/${id}/discard`, { method: 'POST' })

export const applyProposals = (ids: number[]) =>
  request<{ applied: number; failed: number; results: Array<Record<string, unknown>> }>(
    '/proposals/apply-batch',
    { method: 'POST', body: { ids } },
  )

export const discardProposals = (ids: number[]) =>
  request<{ discarded: number }>('/proposals/discard-batch', { method: 'POST', body: { ids } })

// ─────────────────────────── 审稿 / 质检 / 质量债 ───────────────────────────

export const reviewChapter = (projectId: number, chapterRel: string, useAi = true) =>
  request<ReviewResult>(`/projects/${projectId}/review`, {
    method: 'POST',
    body: { chapter_rel: chapterRel, use_ai: useAi },
  })

export const softDeslop = (projectId: number, chapterRel: string, useAi = true) =>
  request<SoftDeslopResult>(
    `/projects/${projectId}/review/deslop`,
    { method: 'POST', body: { chapter_rel: chapterRel, use_ai: useAi } },
  )

export const qualityMatrix = (projectId: number) =>
  request<QualityMatrix>(`/projects/${projectId}/quality-matrix`)

export const listDebts = (projectId: number, status: string | null = 'open') =>
  request<Array<{ id: number; rel_path: string; level: string; status: string; note: string; created_at: string }>>(
    `/projects/${projectId}/debts${qs({ status })}`,
  )

export const resolveDebt = (debtId: number, status = 'resolved') =>
  request<Record<string, unknown>>(`/debts/${debtId}/resolve${qs({ status })}`, { method: 'POST' })

// ─────────────────────────── 摄取（四件套） ───────────────────────────

export const ingestChapter = (projectId: number, chapterRel: string, useAi = true) =>
  request<IngestionResult>(`/projects/${projectId}/ingest`, {
    method: 'POST',
    body: { chapter_rel: chapterRel, use_ai: useAi },
  })

export const listTimeline = (projectId: number) =>
  request<TimelineEntry[]>(`/projects/${projectId}/timeline`)

export const listForeshadows = (projectId: number) =>
  request<Foreshadow[]>(`/projects/${projectId}/foreshadows`)

export const confirmForeshadow = (projectId: number, line: number, plannedChapter = '', expectedHash?: string) =>
  request<Record<string, unknown>>(
    `/projects/${projectId}/foreshadows/confirm${qs({ line, planned_chapter: plannedChapter, expected_hash: expectedHash })}`,
    { method: 'POST' },
  )

export const listLedger = (projectId: number) =>
  request<Array<{ item: string; chapter: string; delta: string; balance: string; unit: string }>>(
    `/projects/${projectId}/ledger`,
  )

// ─────────────────────────── 写作辅助 ───────────────────────────

export const generate = (projectId: number, payload: Record<string, unknown>) =>
  request<Record<string, unknown>>(`/projects/${projectId}/generate`, { method: 'POST', body: payload })

export const localOperation = (
  projectId: number,
  payload: { chapter_rel: string; selection: string; operation: string; instruction?: string },
) => request<Record<string, unknown>>(`/projects/${projectId}/local-op`, { method: 'POST', body: payload })

export const ghostText = (
  projectId: number,
  payload: { chapter_rel: string; prefix: string; suffix?: string },
) => request<{ candidate: string; ok: boolean }>(`/projects/${projectId}/ghost-text`, {
  method: 'POST',
  body: payload,
})

export const sampleStyle = (projectId: number, relPath: string, approved = true, note?: string) =>
  request<StyleFingerprint>(`/projects/${projectId}/style/sample`, {
    method: 'POST',
    body: { rel_path: relPath, approved, note },
  })

export const listStyleFingerprints = (projectId: number, approvedOnly = false) =>
  request<{ samples: StyleFingerprint[]; reference: StyleReferenceMetrics | null }>(
    `/projects/${projectId}/style/fingerprints${qs({ approved_only: approvedOnly })}`,
  )

export const cancelStyleApproval = (projectId: number, fingerprintId: number) =>
  request<StyleFingerprint>(`/projects/${projectId}/style/fingerprints/${fingerprintId}`, {
    method: 'PATCH', body: { approved: false },
  })

export const listPrompts = () =>
  request<Array<{ prompt_id: string; version: string; task_type: string; current: boolean }>>('/prompts')

// ─────────────────────────── Story Bible / 大纲 / 卡片 ───────────────────────────

export const bibleOverview = (projectId: number) =>
  request<BibleOverview>(`/projects/${projectId}/bible`)

export const bibleEntities = (projectId: number, kind: string) =>
  request<{ kind: string; label: string; entities: BibleEntity[]; count: number }>(
    `/projects/${projectId}/bible/${kind}`,
  )

export const bibleUnknown = (projectId: number) =>
  request<Array<{ kind: string; name: string; ref: string; 说明: string; reason: 'source_unknown' | 'missing_field'; rel_path: string; field?: string }>>(
    `/projects/${projectId}/bible/unknown`,
  )

export const markBibleSource = (projectId: number, ref: string, source: string, note = '') =>
  request<Record<string, unknown>>(`/projects/${projectId}/bible/source`, {
    method: 'POST',
    body: { ref, source, note },
  })

export const readOutline = (projectId: number) =>
  request<OutlineState>(`/projects/${projectId}/outline`)

export const saveOutline = (projectId: number, content: string, confirm = false) =>
  request<{ content: string; frozen: boolean }>(`/projects/${projectId}/outline`, {
    method: 'PUT',
    body: { content, confirm },
  })

export const expandOutline = (projectId: number, idea: string, useAi = true) =>
  request<{ questions: string[] }>(`/projects/${projectId}/outline/expand`, {
    method: 'POST',
    body: { idea, use_ai: useAi },
  })

export const outlineCandidates = (projectId: number, idea: string, count = 3, useAi = true) =>
  request<{ candidates: OutlineCandidate[]; count: number; added_count: number; duplicate_count: number; message?: string }>(
    `/projects/${projectId}/outline/candidates`,
    { method: 'POST', body: { idea, count, use_ai: useAi } },
  )

export const refineOutlineCandidate = (projectId: number, candidateId: string, message: string) =>
  request<{ candidate: OutlineCandidate; candidates: OutlineCandidate[]; count: number }>(
    `/projects/${projectId}/outline/candidates/${encodeURIComponent(candidateId)}/refine`,
    { method: 'POST', body: { message } },
  )

export const lockOutline = (projectId: number, candidateId: string | number) =>
  request<Record<string, unknown>>(`/projects/${projectId}/outline/lock`, {
    method: 'POST',
    body: { ...(typeof candidateId === 'number' ? { index: candidateId } : { candidate_id: candidateId }), confirm: true },
  })

export const rollingPlan = (projectId: number, useAi = true) =>
  request<{ volumes_total: number; refined: string[]; suggestion: string; note: string }>(
    `/projects/${projectId}/outline/rolling${qs({ use_ai: useAi })}`,
    { method: 'POST' },
  )

export const outlineHistory = (projectId: number) =>
  request<Array<Record<string, unknown>>>(`/projects/${projectId}/outline/history`)

export const listCards = (
  projectId: number,
  params: { category?: string; query?: string; highlight_only?: boolean; use_ai?: boolean } = {},
) => request<CardList>(`/projects/${projectId}/cards${qs(params)}`)

export const refreshCards = (projectId: number, category?: string, force = false) =>
  request<CardParseTask>(`/projects/${projectId}/cards/refresh`, {
    method: 'POST',
    body: { category: category ?? null, force },
  })

export const getCardParseTask = (projectId: number, taskId: string) =>
  request<CardParseTask>(`/projects/${projectId}/cards/tasks/${taskId}`)

export const cancelCardParseTask = (projectId: number, taskId: string) =>
  request<CardParseTask>(`/projects/${projectId}/cards/tasks/${taskId}/cancel`, { method: 'POST' })

export const listCardHighlights = (projectId: number) =>
  request<CardHighlightEntry[]>(`/projects/${projectId}/cards/highlights`)

export const setCardHighlight = (projectId: number, ref: string, color: string, mode?: 'auto' | 'manual' | 'off') =>
  request<{ ref: string; highlight: string }>(`/projects/${projectId}/cards/highlight`, {
    method: 'POST',
    body: { ref, color, mode },
  })

export const cardChapterLine = (projectId: number, ref: string) =>
  request<{ name: string; total: number; occurrences: Array<{ rel_path: string; number: number; count: number }> }>(
    `/projects/${projectId}/cards/chapter-line${qs({ ref })}`,
  )

export const updateCard = (projectId: number, ref: string, fields: Record<string, string>, name?: string,
  options?: { field_edits?: Array<{ original_name: string; new_name?: string; value?: string; delete?: boolean }>; expected_hash?: string }) =>
  request<Record<string, unknown>>(`/projects/${projectId}/cards`, {
    method: 'PATCH',
    body: { ref, fields, name, ...options },
  })

export const addCard = (
  projectId: number,
  payload: { category: string; name: string; fields?: Record<string, string>; via_proposal?: boolean },
) => request<Record<string, unknown>>(`/projects/${projectId}/cards`, { method: 'POST', body: payload })

export const deleteCard = (projectId: number, ref: string) =>
  request<Record<string, unknown>>(`/projects/${projectId}/cards${qs({ ref, via_proposal: true })}`, {
    method: 'DELETE',
  })

export const generateCard = (projectId: number, category: string, name: string, hint = '') =>
  request<{ proposal_id: number; card: string; fields: Record<string, string> }>(
    `/projects/${projectId}/cards/generate`,
    { method: 'POST', body: { category, name, hint } },
  )

export const exportCards = (projectId: number, category?: string, format: 'md' | 'json' = 'md') =>
  request<{ count: number; content: string }>(`/projects/${projectId}/cards/export`, {
    method: 'POST',
    body: { category: category ?? null, format },
  })

// ─────────────────────────── 角色 ───────────────────────────

export const listCharacters = (projectId: number) =>
  request<Array<{ name: string; fields: Record<string, string>; missing: string[]; source: string; state: Record<string, string> }>>(
    `/projects/${projectId}/characters`,
  )

export const listCharacterStates = (projectId: number, character?: string) =>
  request<{ states: Record<string, Record<string, string>>; index: Array<Record<string, unknown>> }>(
    `/projects/${projectId}/characters/states${qs({ character })}`,
  )

export const applyCharacterState = (
  projectId: number,
  payload: { character: string; field: string; value: string; source?: string; chapter?: string; evidence?: string },
) => request<Record<string, unknown>>(`/projects/${projectId}/characters/states`, { method: 'POST', body: payload })

export const updateCharacterField = (
  projectId: number,
  payload: { character: string; field: string; value: string },
) => request<Record<string, unknown>>(`/projects/${projectId}/characters/field`, { method: 'PUT', body: payload })

export const listMemory = (projectId: number, character?: string) =>
  request<Array<{ id: number; character: string; know_what: string; when_known: string; source_event: string }>>(
    `/projects/${projectId}/characters/memory${qs({ character })}`,
  )

export const addMemory = (
  projectId: number,
  payload: { character: string; know_what: string; when_known?: string; source_event?: string },
) => request<Record<string, unknown>>(`/projects/${projectId}/characters/memory`, { method: 'POST', body: payload })

export const growthCurve = (projectId: number, name: string) =>
  request<{ character: string; series: Record<string, Array<Record<string, unknown>>>; events: TimelineEntry[] }>(
    `/projects/${projectId}/characters/${encodeURIComponent(name)}/growth`,
  )

// ─────────────────────────── 向量检索 ───────────────────────────

export const vectorModelInfo = () => request<Record<string, unknown>>('/vector/model')

export const setVectorModel = (payload: { model_dir?: string; provider?: string; model?: string }) =>
  request<Record<string, unknown>>('/vector/model', { method: 'PUT', body: payload })

export const vectorStats = (projectId: number) =>
  request<VectorStats>(`/projects/${projectId}/vector/stats`)

export const vectorRebuild = (projectId: number) =>
  request<Record<string, unknown>>(`/projects/${projectId}/vector/rebuild`, { method: 'POST' })

export const vectorSearch = (projectId: number, q: string, limit = 5) =>
  request<{ hits: Array<Record<string, unknown>>; count: number; ready: boolean }>(
    `/projects/${projectId}/vector/search${qs({ q, limit })}`,
  )

// ─────────────────────────── 技能 / Agent / 规则 / 路由 ───────────────────────────

export const listSkills = () =>
  request<{ skills: SkillItem[]; estimated_tokens: number; target_root: string }>('/skills')

export const getSkill = (name: string) =>
  request<SkillItem & { content: string }>(`/skills/${encodeURIComponent(name)}`)

export const createSkill = (payload: { name: string; description: string; body: string }) =>
  request<SkillItem>('/skills', { method: 'POST', body: payload })

export const updateSkill = (name: string, content: string) =>
  request<SkillItem>(`/skills/${encodeURIComponent(name)}`, { method: 'PUT', body: { content } })

export const importSkill = (content: string, name?: string) =>
  request<SkillItem>('/skills/import', { method: 'POST', body: { content, name: name ?? null } })

export const setSkillEnabled = (name: string, enabled: boolean) =>
  request<SkillItem>(`/skills/${encodeURIComponent(name)}/enable${qs({ enabled })}`, { method: 'POST' })

export const deleteSkill = (name: string) =>
  request<{ name: string }>(`/skills/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const syncSkills = () => request<Record<string, unknown>>('/skills/sync', { method: 'POST' })

export const generateSkill = (description: string, name = '', save = false) =>
  request<{ ok: boolean; name: string; content: string; error?: string }>('/skills/generate', {
    method: 'POST',
    body: { description, name, save },
  })

export const listAgents = (includeBuiltin = false) =>
  request<{ agents: AgentItem[]; builtin_count: number; tool_whitelist: string[] }>(
    `/agents${qs({ include_builtin: includeBuiltin })}`,
  )

export const createAgent = (payload: Record<string, unknown>) =>
  request<AgentItem>('/agents', { method: 'POST', body: payload })

export const updateAgent = (name: string, patch: Record<string, unknown>) =>
  request<AgentItem>(`/agents/${encodeURIComponent(name)}`, { method: 'PATCH', body: { patch } })

export const duplicateAgent = (name: string, newName: string) =>
  request<AgentItem>(`/agents/${encodeURIComponent(name)}/duplicate`, {
    method: 'POST',
    body: { new_name: newName },
  })

export const setAgentModel = (name: string, providerId: string, modelId: string) =>
  request<AgentItem>(`/agents/${encodeURIComponent(name)}/model`, {
    method: 'POST',
    body: { provider_id: providerId, model_id: modelId },
  })

export const deleteAgent = (name: string) =>
  request<{ name: string }>(`/agents/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const draftAgent = (description: string, name = '') =>
  request<{ ok: boolean; draft: Record<string, unknown> | null; error?: string }>('/agents/draft', {
    method: 'POST',
    body: { description, name },
  })

export const listRules = (projectId?: number) =>
  request<{ global: RuleLike[]; project: RuleLike[]; skill_builtin: RuleLike[] }>(
    `/rules${qs({ project_id: projectId })}`,
  )

export const resolvedRules = (projectId?: number) =>
  request<{ effective: RuleLike[]; conflicts: Array<Record<string, unknown>>; order: string[] }>(
    `/rules/resolved${qs({ project_id: projectId })}`,
  )

export const upsertRule = (payload: Record<string, unknown>, projectId?: number) =>
  request<RuleLike>(`/rules${qs({ project_id: projectId })}`, { method: 'POST', body: payload })

export const deleteRule = (name: string, scope: string, projectId?: number) =>
  request<Record<string, unknown>>(
    `/rules/${encodeURIComponent(name)}${qs({ scope, project_id: projectId })}`,
    { method: 'DELETE' },
  )

export const compileRules = (projectId?: number) =>
  request<Record<string, unknown>>(`/rules/compile${qs({ project_id: projectId })}`, { method: 'POST' })

export interface RuleLike {
  id: string
  scope: string
  name: string
  body: string
  keys: string[]
  confirmed: boolean
  enabled: boolean
}

export const routeIntent = (text: string, projectId?: number, agent = '') =>
  request<RoutingDecision>('/routing/route', {
    method: 'POST',
    body: { text, project_id: projectId ?? null, agent },
  })

export const routingIntents = () =>
  request<{ intents: Array<{ intent: string; agent: string; triggers: string[] }> }>('/routing/intents')

// ─────────────────────────── 工作流 ───────────────────────────

export const listWorkflows = () =>
  request<{ workflows: WorkflowDefinition[]; node_types: string[] }>('/workflows')

export const getWorkflow = (name: string) =>
  request<WorkflowDefinition>(`/workflows/${encodeURIComponent(name)}`)

export const saveWorkflow = (definition: Record<string, unknown>) =>
  request<WorkflowDefinition>('/workflows', { method: 'POST', body: { definition } })

export const duplicateWorkflow = (name: string, newName: string) =>
  request<WorkflowDefinition>(
    `/workflows/${encodeURIComponent(name)}/duplicate${qs({ new_name: newName })}`,
    { method: 'POST' },
  )

export const deleteWorkflow = (name: string) =>
  request<{ name: string }>(`/workflows/${encodeURIComponent(name)}`, { method: 'DELETE' })

export const startWorkflowRun = (payload: Record<string, unknown>) =>
  request<WorkflowRun>('/workflows/run', { method: 'POST', body: payload })

export const listWorkflowRuns = (projectId?: number) =>
  request<WorkflowRun[]>(`/workflows/runs/list${qs({ project_id: projectId })}`)

export const getWorkflowRun = (runId: number) =>
  request<WorkflowRun>(`/workflows/runs/${runId}`)

export const pauseWorkflowRun = (runId: number) =>
  request<WorkflowRun>(`/workflows/runs/${runId}/pause`, { method: 'POST' })

export const resumeWorkflowRun = (runId: number) =>
  request<WorkflowRun>(`/workflows/runs/${runId}/resume`, { method: 'POST' })

// ─────────────────────────── 对话 ───────────────────────────

export const listChatSessions = (projectId?: number) =>
  request<ChatSession[]>(`/chat/sessions${qs({ project_id: projectId })}`)

export const getChatDefaults = (projectId: number) =>
  request<ChatDefaults>(`/projects/${projectId}/chat-defaults`)

export const putChatDefaults = (projectId: number, defaults: Pick<ChatDefaults, 'permission_mode' | 'discussion_only'>) =>
  request<ChatDefaults>(`/projects/${projectId}/chat-defaults`, { method: 'PUT', body: defaults })

export const getWritingPrefs = (projectId: number) =>
  request<WritingPrefs>(`/projects/${projectId}/writing-prefs`)

export const putWritingPrefs = (projectId: number, prefs: Pick<WritingPrefs, 'chapter_min_words' | 'chapter_max_words'>) =>
  request<WritingPrefs>(`/projects/${projectId}/writing-prefs`, { method: 'PUT', body: prefs })

export const createChatSession = (payload: {
  project_id?: number; title?: string; agent?: string; agent_pinned?: number
  provider_id?: string; model_id?: string; mode?: 'write' | 'read'; auto_apply?: number
  permission_mode?: ChatPermissionMode; discussion_only?: boolean
}) =>
  request<ChatSession>('/chat/sessions', { method: 'POST', body: payload })

export const getChatSession = (sessionId: number) =>
  request<ChatSessionDetail>(`/chat/sessions/${sessionId}`)

export const patchChatSession = (sessionId: number, patch: Record<string, unknown>) =>
  request<ChatSession>(`/chat/sessions/${sessionId}`, { method: 'PATCH', body: { patch } })

export const deleteChatSession = (sessionId: number) =>
  request<{ id: number }>(`/chat/sessions/${sessionId}`, { method: 'DELETE' })

export const chatQuickCards = () =>
  request<Array<{ key: string; label: string; prompt: string; task_type: string }>>('/chat/quick-cards')

export const sendChatMessage = (sessionId: number, payload: Record<string, unknown>) =>
  request<ChatReply>(`/chat/sessions/${sessionId}/send`, { method: 'POST', body: payload })

export const createChatRun = (sessionId: number, payload: {
  client_request_id: string; text: string; context: ChatContext
  continue_from_run_id?: string
  provider?: string; model?: string; agent?: string
}) => request<ChatRun>(`/chat/sessions/${sessionId}/runs`, { method: 'POST', body: payload })

export const getChatRun = (runId: string, signal?: AbortSignal) =>
  request<ChatRun>(`/chat/runs/${runId}`, { signal })

export const streamChatRun = (runId: string, after: number,
  onEvent: (event: ChatRunEvent) => void, signal?: AbortSignal, onFrame?: () => void) =>
  readSSE(`/chat/runs/${runId}/events${qs({ after })}`, undefined,
    (event) => onEvent(event as ChatRunEvent), signal, 'GET', onFrame)

export const cancelChatRun = (runId: string) =>
  request<ChatRun>(`/chat/runs/${runId}/cancel`, { method: 'POST' })

export const respondChatInteraction = (runId: string, interactionId: string, response: ChatInteractionResponse) =>
  request<ChatInteraction>(`/chat/runs/${runId}/interactions/${encodeURIComponent(interactionId)}/respond`, {
    method: 'POST', body: { response },
  })

export const regenerateChatTitle = (sessionId: number) =>
  request<ChatSession>(`/chat/sessions/${sessionId}/title/regenerate`, { method: 'POST' })

export const getChatMemory = (projectId: number) =>
  request<{ entries: ChatMemoryEntry[] }>(`/projects/${projectId}/chat-memory`)

export const updateChatMemory = (projectId: number, memoryId: number | string, content: string) =>
  request<ChatMemoryEntry>(`/projects/${projectId}/chat-memory/${encodeURIComponent(String(memoryId))}`, {
    method: 'PATCH', body: { content },
  })

export const deleteChatMemory = (projectId: number, memoryId: number | string) =>
  request<unknown>(`/projects/${projectId}/chat-memory/${encodeURIComponent(String(memoryId))}`, { method: 'DELETE' })

export const revertChatRun = (runId: string, changeIds?: number[]) =>
  request<{ changes?: ChatFileChange[]; run?: ChatRun }>(`/chat/runs/${runId}/revert`, {
    method: 'POST', body: { change_ids: changeIds },
  })

// ─────────────────────────── 拆书资产库（M4 / Task 44） ───────────────────────────

export interface TeardownTarget {
  target: string
  genre: string
  chars: number
  imported_at: string
  has_analysis: boolean
  has_facts: boolean
  fact_count: number
}

export interface TeardownFact {
  target: string
  genre: string
  dimension: string
  conclusion: string
  evidence: string
  quote: string
}

export const listTeardownTargets = (projectId: number) =>
  request<{ targets: TeardownTarget[]; dimensions: string[] }>(`/projects/${projectId}/teardown`)

export const importTeardownTarget = (
  projectId: number,
  payload: { target_name: string; content: string; genre?: string; source_note?: string },
) => request<{ target: string; genre: string; chars: number; path: string }>(
  `/projects/${projectId}/teardown/import`,
  { method: 'POST', body: payload },
)

export const analyzeTeardown = (
  projectId: number,
  target: string,
  useAi = true,
  extraNote = '',
) =>
  request<{
    target: string
    genre: string
    dimensions: string[]
    facts: TeardownFact[]
    fact_count: number
    missing_evidence: number
    source: string
    files: string[]
  }>(`/projects/${projectId}/teardown/analyze`, {
    method: 'POST',
    body: { target, use_ai: useAi, extra_note: extraNote },
  })

export const listTeardownFacts = (projectId: number, target?: string) =>
  request<{ facts: TeardownFact[]; count: number; missing_evidence: number }>(
    `/projects/${projectId}/teardown/facts${qs({ target })}`,
  )

export const recallTeardown = (projectId: number, genre = '', limit = 6) =>
  request<{ genre: string; matched: number; facts: TeardownFact[] }>(
    `/projects/${projectId}/teardown/recall${qs({ genre, limit })}`,
  )

export const readTeardownArtifact = (
  projectId: number,
  target: string,
  kind: 'source' | 'analysis' | 'timeline' | 'facts' = 'facts',
) =>
  request<{ target: string; kind: string; meta: Record<string, unknown>; content: string }>(
    `/projects/${projectId}/teardown/artifact${qs({ target, kind })}`,
  )

export const deleteTeardownTarget = (projectId: number, target: string) =>
  request<{ target: string; deleted: boolean }>(
    `/projects/${projectId}/teardown${qs({ target })}`,
    { method: 'DELETE' },
  )

// ─────────────────────────── 发布导出（M5 / Task 66） ───────────────────────────

export interface PublishPlatform {
  key: string
  label: string
  word_range: [number, number]
  suffix: string
  notes: string[]
}

export const listPublishPlatforms = () => request<PublishPlatform[]>('/publish/platforms')

export const buildPublishExport = (
  projectId: number,
  platform: string,
  onlyCompleted = false,
) =>
  request<{
    platform: string
    platform_label: string
    filename: string
    content: string
    stats: { chapters: number; words: number; avg_words: number; word_range: [number, number] }
    warnings: Array<{ rel_path: string; level: string; note: string }>
    checklist: Array<{ item: string; ok: boolean; basis: string }>
    notes: string[]
    project_name: string
  }>(`/projects/${projectId}/publish/export`, {
    method: 'POST',
    body: { platform, only_completed: onlyCompleted },
  })

export const exportUsageCsv = (summary: UsageSummary): string => {
  const lines = ['day,tokens,prompt,completion,calls']
  summary.by_day.forEach((row) => {
    lines.push(`${row.day},${row.tokens},${row.prompt},${row.completion},${row.calls}`)
  })
  return lines.join('\n')
}

// ─────────────────────────── 生图工坊 ───────────────────────────

export const listImageProviders = () =>
  request<{ providers: ImageProvider[] }>('/images/providers')

export const upsertImageProvider = (payload: {
  provider_id: string
  display_name?: string
  model_id: string
  base_url: string
  api_key?: string | null
  timeout_seconds?: number
  enabled?: boolean | null
}) => request<ImageProvider>('/images/providers', { method: 'POST', body: payload })

export const deleteImageProvider = (providerId: string) =>
  request<{ provider_id: string; deleted: boolean }>(
    `/images/providers/${encodeURIComponent(providerId)}`,
    { method: 'DELETE' },
  )

export const enableImageProvider = (providerId: string, enabled: boolean) =>
  request<ImageProvider>(`/images/providers/${encodeURIComponent(providerId)}/enable`, {
    method: 'POST',
    body: { enabled },
  })

export const healthCheckImageProvider = (providerId: string) =>
  request<{
    ok: boolean
    kind?: string
    reason?: string
    model_count?: number
    model_matched?: boolean
    note?: string
    models?: string[]
  }>('/images/providers/health', { method: 'POST', body: { provider_id: providerId } })

/** 参考图上传：请求体为原始图片字节（不走 JSON request，仿 uploadCover）。 */
export async function uploadImageRef(
  file: File,
): Promise<{ upload_id: string; filename: string; size: number; media_type: string }> {
  const name = encodeURIComponent(file.name || 'reference')
  const response = await fetch(
    `${API_BASE}/images/uploads?filename=${name}`,
    {
      method: 'PUT',
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file,
    },
  )
  if (!response.ok) throw await toApiError(response)
  return (await response.json()) as {
    upload_id: string
    filename: string
    size: number
    media_type: string
  }
}

export interface ImageGenerateInput {
  provider_id: string
  prompt: string
  size: string
  quality: string
  output_format: string
  background: boolean
  moderation: string
  n: number
  input_upload_ids?: string[]
}

export const generateImages = (payload: ImageGenerateInput) =>
  request<{ job: ImageJob }>('/images/generate', { method: 'POST', body: payload })

export const listImageJobs = (active = true, limit = 50) =>
  request<{ jobs: ImageJob[] }>(`/images/jobs${qs({ active, limit })}`)

export const getImageJob = (jobId: number) =>
  request<{ job: ImageJob }>(`/images/jobs/${jobId}`)

export const cancelImageJob = (jobId: number) =>
  request<{ job: ImageJob }>(`/images/jobs/${jobId}/cancel`, { method: 'POST' })

export const listImageRecords = (
  params: { favorite?: boolean; search?: string; limit?: number } = {},
) =>
  request<{ records: ImageRecord[] }>(
    `/images/records${qs({
      favorite: params.favorite === undefined ? undefined : String(params.favorite),
      search: params.search,
      limit: params.limit,
    })}`,
  )

export const patchImageRecord = (
  recordId: number,
  patch: { favorite?: boolean; prompt?: string },
) =>
  request<{ record: ImageRecord }>(`/images/records/${recordId}`, {
    method: 'PATCH',
    body: patch,
  })

export const deleteImageRecord = (recordId: number) =>
  request<{ id: number; deleted: boolean }>(`/images/records/${recordId}`, {
    method: 'DELETE',
  })

export const reindexImages = () =>
  request<{ rebuilt: number; skipped: number }>('/images/reindex', { method: 'POST' })
