import { readSSE, request } from './client'
import type { ProposalItem } from './types'

export interface KnowledgeDocument {
  id: string
  rel_path: string
  title: string
  kind: string
  status?: string
  chapter_number?: number | null
  chunk_count: number
  index_status?: 'indexed' | 'pending' | 'stale' | 'excluded'
  index_label?: string
}
export interface KnowledgeModel {
  name?: string
  model?: string
  mode?: string
  provider?: string
  ready?: boolean
  dim?: number
  fingerprint?: string
  [key: string]: unknown
}
export interface KnowledgeOverview {
  project_id: number
  knowledge_key: string
  project_name: string
  db_path: string
  documents: KnowledgeDocument[]
  counts: { documents: number; chunks: number; nodes: number; edges: number;
    indexed_documents?: number; eligible_documents?: number; excluded_documents?: number }
  job?: { id?: string; status: string; progress?: number; error?: string } | null
  model: KnowledgeModel
  generation_model?: { name?: string; ready?: boolean; error?: string }
  vector_ready?: boolean
  degraded_reason?: string
  graph_version?: string
  graph_updated_at?: string
}
export interface KnowledgeNode {
  id: string
  anchor?: string
  kind: string
  label: string
  source: string
  candidate_id?: string
  candidate_ids?: string[]
  review_status?: string
  lifecycle?: KnowledgeLifecycle
  document: string
  chapter_start?: number | null
  chapter_end?: number | null
  evidence_ids: string[]
  chapter_number?: number | null
  description?: string
  preview?: string
  planned?: boolean
  documents?: string[]
  rel_paths?: string[]
  source_location?: { rel_path: string; line_start: number; line_end: number; start?: number; end?: number;
    document_hash?: string; evidence_id: string }
}
export interface KnowledgeEdge {
  id: string
  source: string
  target: string
  relation: string
  origin: string
  candidate_id?: string
  review_status?: string
  lifecycle?: KnowledgeLifecycle
  evidence_ids: string[]
  chapter_start?: number | null
  chapter_end?: number | null
  document?: string
}
export interface KnowledgeGraphData {
  project_id: number
  knowledge_key: string
  nodes: KnowledgeNode[]
  edges: KnowledgeEdge[]
  truncated: boolean
  count: number
  document_nodes?: Record<string, string[]>
  graph_version?: string
}
export interface KnowledgeEvidence {
  id: string
  evidence_id: string
  text: string
  rel_path: string
  line_start: number
  line_end: number
  chapter_number?: number | null
  source: string
  source_tier?: 'author' | 'reference' | 'unknown'
  provenance?: 'author' | 'model' | 'unknown'
  score?: number
  node_ids?: string[]
  document_hash?: string
  archived?: boolean
  current_document_hash?: string | null
  archive?: { change_id: number; proposal_id: number; created_at?: string; document_hash: string } | null
}
export type KnowledgeProfile = 'history' | 'planning' | 'review' | 'teardown'
export interface KnowledgeSearchInput {
  query: string
  mode: 'hybrid' | 'keyword'
  profile: KnowledgeProfile
  chapter_rel?: string
  document?: string
  limit?: number
}
export interface KnowledgeSearchResult {
  project_id: number
  knowledge_key: string
  query: string
  queries: string[]
  hits: KnowledgeEvidence[]
  degraded_reason?: string
}
export type KnowledgeStreamEvent =
  | ({ type: 'evidence' } & KnowledgeSearchResult)
  | { type: 'delta'; text: string; project_id?: number; knowledge_key?: string }
  | { type: 'done'; project_id?: number; knowledge_key?: string }
  | { type: 'error'; message: string; invalidate_answer?: boolean; project_id?: number; knowledge_key?: string }

const base = (projectId: number) => `/projects/${projectId}/knowledge`
export const getKnowledge = (projectId: number, signal?: AbortSignal) =>
  request<KnowledgeOverview>(base(projectId), { signal })
export const getKnowledgeGraph = (projectId: number, params: {
  document?: string; kinds?: string[]; chapter_before?: number; center?: string; limit?: number; include_content?: boolean
}, signal?: AbortSignal) => {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '' && (!Array.isArray(value) || value.length)) {
      query.set(key, Array.isArray(value) ? value.join(',') : String(value))
    }
  })
  return request<KnowledgeGraphData>(`${base(projectId)}/graph?${query}`, { signal })
}
export const syncKnowledge = (projectId: number, force = false, signal?: AbortSignal) =>
  request<KnowledgeOverview>(`${base(projectId)}/sync`, { method: 'POST', body: { force }, signal })
export const searchKnowledge = (projectId: number, input: KnowledgeSearchInput, signal?: AbortSignal) =>
  request<KnowledgeSearchResult>(`/projects/${projectId}/retrieval/search`, { method: 'POST', body: input, signal })
export const getKnowledgeEvidence = (projectId: number, evidenceId: string, signal?: AbortSignal) =>
  request<KnowledgeEvidence>(`${base(projectId)}/evidence/${encodeURIComponent(evidenceId)}`, { signal })
export const askKnowledge = (projectId: number, input: KnowledgeSearchInput,
  onEvent: (event: KnowledgeStreamEvent) => void, signal?: AbortSignal) =>
  readSSE(`${base(projectId)}/ask`, input, (event) => onEvent(event as unknown as KnowledgeStreamEvent), signal)
export const prepareKnowledgeModel = (signal?: AbortSignal) =>
  request<Record<string, unknown>>('/vector/model/prepare', { method: 'POST', body: {}, signal })
export const getBookVectorConfig = (projectId: number, signal?: AbortSignal) =>
  request<{ config: Record<string, unknown>; effective?: Record<string, unknown>; inherited?: boolean; override?: Record<string, unknown> }>(
    `/projects/${projectId}/vector/model`, { signal })
export const saveBookVectorConfig = (projectId: number, config: Record<string, unknown>, signal?: AbortSignal) =>
  request<Record<string, unknown>>(`/projects/${projectId}/vector/model`, { method: 'PUT', body: config, signal })

export interface KnowledgeEntityIndex {
  project_id: number
  knowledge_key: string
  nodes: KnowledgeNode[]
  count: number
}
export interface KnowledgeExtractionJob {
  id: string
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  phase: string
  files_total: number
  files_completed: number
  chunks_total: number
  chunks_completed: number
  candidate_count: number
  failed_documents: Array<{ rel_path: string; error: string }>
  paths: string[]
  types: string[]
  created_at: string
  updated_at: string
  current_document?: string
  document_progress?: Array<{ rel_path: string; status: string; chunks_total: number; chunks_completed: number }>
}
export interface KnowledgeCandidateEntity { anchor: string; kind: string; label: string; document: string;
  line_start?: number; line_end?: number; definition?: string; fields?: Record<string, string>; description?: string }
export type KnowledgeLifecycle = 'draft' | 'planned' | 'setting_fact' | 'historical_event'
export interface KnowledgeCandidate {
  id: string
  version: number
  kind: 'entity' | 'field' | 'relation'
  entity: KnowledgeCandidateEntity
  entity_options: KnowledgeCandidateEntity[]
  requires_entity_choice: boolean
  field: string
  current_value: string
  suggested_value: string
  relation: string
  target_entity?: KnowledgeCandidateEntity | null
  target_entity_options?: KnowledgeCandidateEntity[]
  requires_target_entity_choice?: boolean
  provenance: 'model'
  review_status: string
  lifecycle: KnowledgeLifecycle
  source_lifecycle?: KnowledgeLifecycle
  chapter_start?: number | null
  chapter_end?: number | null
  evidence: { rel_path: string; quote: string; start: number; end: number; line_start: number; line_end: number; document_hash: string }
  target_path: string
  proposal_id?: number | null
  conflicts: Array<string | { id: string; value: string; evidence?: { rel_path?: string; quote?: string; line_start?: number; line_end?: number } }>
  stale_reason?: string
  created_at: string
}
export interface KnowledgeReviewItem {
  id: string
  version: number
  decision: 'approve' | 'ignore'
  value?: string
  entity_anchor?: string
  target_entity_anchor?: string
  lifecycle?: KnowledgeLifecycle
  chapter_start?: number
  chapter_end?: number
}
export const getKnowledgeEntities = (projectId: number, params: { q?: string; kind?: string; offset?: number; limit?: number }, signal?: AbortSignal) => {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== '') query.set(key, String(value)) })
  return request<KnowledgeEntityIndex>(`${base(projectId)}/entities?${query}`, { signal })
}
export const extractKnowledge = (projectId: number, input: { paths?: string[]; types: string[]; force?: boolean; retry_job_id?: string }, signal?: AbortSignal) =>
  request<KnowledgeExtractionJob>(`${base(projectId)}/extract`, { method: 'POST', body: input, signal })
export const getKnowledgeExtractionJob = (projectId: number, id: string, signal?: AbortSignal) =>
  request<KnowledgeExtractionJob>(`${base(projectId)}/jobs/${encodeURIComponent(id)}`, { signal })
export const getKnowledgeExtractionJobs = (projectId: number, signal?: AbortSignal) =>
  request<{ jobs: KnowledgeExtractionJob[] }>(`${base(projectId)}/jobs?limit=20`, { signal })
export const cancelKnowledgeExtraction = (projectId: number, id: string, signal?: AbortSignal) =>
  request<KnowledgeExtractionJob>(`${base(projectId)}/jobs/${encodeURIComponent(id)}/cancel`, { method: 'POST', body: {}, signal })
export const getKnowledgeCandidates = (projectId: number, status = 'pending', signal?: AbortSignal) =>
  request<{ candidates: KnowledgeCandidate[]; counts: Record<string, number> }>(`${base(projectId)}/candidates?status=${encodeURIComponent(status)}`, { signal })
export const reviewKnowledgeCandidates = (projectId: number, items: KnowledgeReviewItem[], signal?: AbortSignal) =>
  request<{ proposals: ProposalItem[]; candidates: KnowledgeCandidate[]; failures: Array<{ id: string; error: string }> }>(
    `${base(projectId)}/candidates/review`, { method: 'POST', body: { items }, signal })
