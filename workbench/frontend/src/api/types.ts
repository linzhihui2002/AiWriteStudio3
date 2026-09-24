/** 后端响应类型定义（与 FastAPI 的 Pydantic 模型一一对应）。
 *
 * 约定：字段名保持后端的中文键（如「情节点」「字数预算」）以便直接渲染，
 * 布尔与数值字段保持英文键。
 */

export interface Health {
  status: string
  version?: string
  port?: number
  db_ready?: boolean
  fts?: boolean
  vendor_dsh?: boolean
}

export interface Project {
  id: number
  name: string
  path: string
  created_at: string
  archived: boolean
  genre?: string | null
  platform?: string | null
  protagonist?: string | null
  one_liner?: string | null
  has_cover?: boolean
}

export interface TreeNode {
  name: string
  rel_path: string
  type: 'file' | 'dir'
  is_empty?: boolean
  size?: number | null
  mtime?: number | null
  file_count?: number | null
  children?: TreeNode[] | null
  is_chapter?: boolean | null
  number?: number | null
  title?: string | null
  status?: string | null
  word_count?: number | null
}

export interface TreeGroup {
  key: string
  name: string
  rel_path: string
  exists: boolean
  nodes: TreeNode[]
}

export interface ProjectTree {
  project: { id: number; name: string; path: string; archived: boolean }
  groups: TreeGroup[]
  root_nodes: TreeNode[]
}

export interface ChapterDetail {
  hash?: string
  mtime?: number
  rel_path: string
  file_name: string
  content: string
  meta: Record<string, unknown>
  body: string
  title: string
  status: string
  word_count: number
  contract_id: string
  is_empty: boolean
}

export interface ChapterSummary {
  rel_path: string
  file_name: string
  number: number | null
  title: string
  status: string
  word_count: number
  contract_id: string
  is_empty: boolean
  size: number
  mtime: number
}

export interface DeleteResult {
  rel_path: string
  type: string
  action: 'deleted' | 'trashed' | 'failed'
  trash_rel?: string | null
  reason?: string
}

export interface Template {
  id: number
  name: string
  path: string
  is_builtin: boolean
  exists?: boolean
  created_at: string
}

export interface ConflictCheck {
  changed: boolean
  reason: string
  rel_path: string
  disk: { mtime: number; hash: string; size: number }
  external_content?: string | null
  diff: DiffLine[]
}

export interface DiffLine {
  type: 'equal' | 'add' | 'remove'
  line: number
  text: string
}

export interface ProposalItem {
  id: number
  project_id: number | null
  task_id: number | null
  kind: string
  title: string
  target_path: string
  status: string
  created_at: string
  applied_at?: string | null
  meta: Record<string, unknown>
  content: string
  chars: number
  is_new_file: boolean
  diff?: DiffLine[]
}

export interface ContextBlock {
  level: number
  title: string
  source: string
  chars: number
  tokens: number
  mandatory: boolean
  truncated?: boolean
}

export interface ContextPreview {
  items: ContextBlock[]
  total_tokens: number
  budget_tokens: number
  usage_ratio: number
  degradation: Array<{ level: number; title: string; reason: string }>
  excluded_levels: number[]
}

export interface Contract {
  chapter_rel?: string
  plot_points: string[]
  word_budget: number
  hook_type: string
  entities: string[]
  must_connect?: string[]
  constraints?: string[]
  source?: string
  status: 'draft' | 'frozen' | string
  frozen_at?: string | null
  created_at?: string
  history?: Array<{ diff: Record<string, unknown>; confirmed: boolean }>
}

export interface GateCheck {
  key: string
  label: string
  ok: boolean
  optional?: boolean
  hint: string
}

export interface HardGate {
  key: string
  passed: boolean
  detail: string
  blocking: boolean
  hits?: Record<string, unknown>
  findings?: Array<{ line: number; column: number; type: string; text: string }>
  leaks?: Array<{ line: number; text: string; reason: string }>
}

export interface ReviewResult {
  review_id: number
  rel_path: string
  verdict: string
  hard_gates: { passed: boolean; words: number; gates: HardGate[]; locations: Location[]; blocking_gates: string[] }
  items: Array<{ 项: string; 判定: string; 证据: string }>
  consistency: Array<{ 类型: string; 说明: string; 证据: string }>
  revise_instructions: string[]
  counts: { 已完成: number; 未完成: number; 待核实: number }
  ai_used: boolean
}

export interface Location {
  gate: string
  line: number
  text: string
  reason: string
}

export interface QualityMatrixRow {
  rel_path: string
  number: number | null
  title: string
  status: string
  word_count: number
  cells: Record<string, string>
  debt: Array<{ level: string; status: string; note: string }>
}

export interface QualityMatrix {
  project_id: number
  project_name: string
  columns: string[]
  chapters: QualityMatrixRow[]
  summary: Record<string, number>
}

export interface PipelineResult {
  status: 'done' | 'blocked' | 'failed' | 'manual' | 'paused'
  chapter_rel: string
  steps: Record<string, { status: string; detail?: Record<string, unknown> }>
  events: Array<Record<string, unknown>>
  blocked_reasons?: string[]
  manual_reason?: string
  proposal_id?: number | null
  revise_rounds?: number
  review?: {
    verdict: string
    hard_gates: ReviewResult['hard_gates']
    ai_review: ReviewResult | null
  }
}

export interface CardItem {
  id: string
  name: string
  fields: Record<string, string>
  summary: string
  source: string
  category: string
  file: string
  ref: string
  highlight: string
  field_list: string[]
}

export interface CardList {
  project_id: number
  project_name: string
  cards: CardItem[]
  count: number
  counts_by_category: Record<string, number>
  palette: string[]
  categories: Array<{ key: string; label: string; path: string; builtin: boolean }>
}

export interface BibleOverview {
  project_id: number
  project_name: string
  labels: Record<string, string>
  counts: Record<string, number>
  total: number
  timeline_count: number
  foreshadow_open: number
  foreshadow_total: number
  unknown_refs: string[]
  unknown_count: number
}

export interface BibleEntity {
  name: string
  content: string
  summary: string
  ref: string
  source: string
  fields?: Record<string, string>
  missing?: string[]
  status?: string
  planted_in?: string
  evidence?: string
  line?: number
}

export interface Character {
  name: string
  raw: string
  fields: Record<string, string>
  missing: string[]
  source: string
  state: Record<string, string>
}

export interface TimelineEntry {
  chapter: string
  story_time: string
  event: string
  evidence: string
}

export interface Foreshadow {
  line: number
  content: string
  status: string
  planted_in: string
  evidence: string
}

export interface ProviderModel {
  id: string
  name: string
  context_window: number
  max_tokens: number
}

export interface Provider {
  id: number
  provider_id: string
  display_name: string
  source: string
  base_url: string
  models: ProviderModel[]
  model_count?: number
  capabilities: string[]
  timeout_seconds: number
  enabled: boolean
  health: string
  has_secret: boolean
  masked_secret: string
  created_at: string
}

export interface DefaultTarget {
  provider_id: string
  model_id: string
}

export interface EngineInfo {
  available?: boolean
  engine?: string
  provider?: string
  model?: string
  profile?: string
  dsh_home?: string
  default_provider?: string
  default_model?: string
  error?: string
  providers?: Array<{
    provider_id: string
    models: ProviderModel[]
    enabled: boolean
    has_secret: boolean
  }>
}

export interface RoutingInfo {
  decision_table: Record<string, string>
  global_default: string
  global_overrides: Record<string, string>
  fallback_engine: string
  engines: Record<string, EngineInfo>
}

export interface TaskRecord {
  id: number
  project_id: number | null
  task_type: string
  engine: string
  model: string
  provider: string
  agent: string
  status: string
  prompt_id: string
  prompt_version: string
  context_snapshot: Record<string, unknown> | string | null
  result: Record<string, unknown> | string | null
  error: string | null
  created_at: string
  finished_at: string | null
}

export interface UsageSummary {
  days: number
  totals: { tokens: number; cost: number; calls: number; today_tokens: number }
  by_day: Array<{ day: string; tokens: number; prompt: number; completion: number; cost: number; calls: number }>
  by_project: Array<{ project_id: number | null; tokens: number; cost: number; calls: number }>
  by_task_type: Array<{ task_type: string; tokens: number; cost: number; calls: number }>
  budget: {
    daily_token_limit: number
    alert_ratio: number
    currency_per_1k_tokens: number
    alert: boolean
  }
}

export interface SkillItem {
  name: string
  description: string
  source: string
  path: string
  enabled: boolean
  size_bytes: number
  synced: boolean
  synced_path?: string
  error?: string
}

export interface AgentItem {
  name: string
  title: string
  description: string
  system_prompt: string
  skills: string[]
  provider_id: string
  model_id: string
  tools: string[]
  params: Record<string, unknown>
  is_builtin: boolean
  enabled: boolean
  version: number
}

export interface RuleItem {
  id: string
  scope: string
  name: string
  body: string
  keys: string[]
  priority: number
  confirmed: boolean
  enabled: boolean
  source: string
}

export interface WorkflowNode {
  id: string
  type: string
  label: string
  engine: string
  model: string
  skill: string
  next: string
}

export interface WorkflowDefinition {
  name: string
  description: string
  nodes: WorkflowNode[]
  batch: { chapters?: string[]; stop_on_failure?: boolean }
  is_builtin: boolean
  version: number
  path?: string
}

export interface WorkflowRun {
  id: number
  project_id: number
  status: string
  cursor: number
  payload: Record<string, unknown>
  log: Array<Record<string, unknown>>
  created_at: string
  updated_at: string | null
  progress: {
    chapter_index: number
    chapters_total: number
    node_index: number
    completed: string[]
  }
}

export type ChatPermissionMode = 'ask' | 'auto' | 'full'

export interface ChatDefaults {
  permission_mode: ChatPermissionMode
  discussion_only: boolean
  source: string
}

export interface ChatSession {
  id: number
  project_id: number | null
  title: string
  agent: string
  provider_id: string
  model_id: string
  /** 直写开关：1 = 写工具直接落盘（写前自动存快照），0 = 只进收件箱 */
  auto_apply: number
  /** 1 = 作者手选 Agent；0 = 按意图自动路由 */
  agent_pinned: number
  mode?: 'write' | 'read'
  permission_mode?: ChatPermissionMode
  discussion_only?: boolean
  title_source?: 'default' | 'fallback' | 'ai' | 'user'
  title_status?: 'idle' | 'pending' | 'running' | 'complete' | 'failed'
  created_at: string
  updated_at: string | null
}

/** 对话中的一次工具调用（步骤时间线） */
export interface ChatStep {
  index: number
  call_id?: string
  tool: string
  label: string
  args: Record<string, unknown>
  status: 'running' | 'done' | 'error' | 'cancelled'
  summary?: string
  elapsed_ms?: number
  kind?: 'tool' | 'skill' | 'agent' | 'phase'
  started_at?: string
  finished_at?: string
  result?: unknown
  parent_call_id?: string
}

export interface ChatQuestion {
  id: string
  question: string
  header?: string
  options?: Array<{ id?: string; label: string; description?: string }>
  multi_select?: boolean
}

export interface ChatInteraction {
  id: string
  run_id: string
  tool_call_id?: string
  kind: 'question' | 'approval'
  payload: {
    questions?: ChatQuestion[]
    operation?: string
    path?: string
    destination?: string
    before_content?: string | null
    after_content?: string | null
    diff?: DiffLine[] | string
    expected_hash?: string
    [key: string]: unknown
  }
  status: 'pending' | 'answered' | 'cancelled' | 'interrupted' | 'expired'
  response?: ChatInteractionResponse | null
  revision?: number
}

export type ChatInteractionResponse = { decision: 'approve' | 'reject' } | {
  answers: Array<{ id: string; selected: string[]; custom?: string }>
}

export interface ChatMemoryEntry {
  id: number | string
  content: string
  status: string
  source_session_id?: number | null
  source_message_id?: number | null
  updated_at?: string | null
  sources?: Array<{ session_id?: number; message_id?: number; quote: string; kind: string; recorded_at?: string }>
  history?: Array<{ content: string; status: string; updated_at?: string }>
  conflicts?: Array<{ path: string; content: string }>
}

export interface ChatMessage {
  id: number
  role: 'user' | 'assistant' | string
  content: string
  meta: Record<string, unknown>
  created_at: string
}

export interface ChatContext {
  active_file?: string
  target_chapter?: string
  selection?: { text: string; start?: number; end?: number }
  base_hash?: string
  files?: string[]
}

export interface ChatFileChange {
  id: number
  run_id: string
  operation: string
  path: string
  destination?: string | null
  status: string
  before_content?: string | null
  after_content?: string | null
  diff?: DiffLine[] | string
  reverted?: boolean
}

export interface ChatRun {
  id: string
  session_id: number
  status: string
  text: string
  error_code?: string | null
  error_message?: string | null
  changes: ChatFileChange[]
  events?: ChatRunEvent[]
  steps?: ChatStep[]
  last_seq: number
  interactions?: ChatInteraction[]
  context_warnings?: string[]
  status_message?: string
  context_preview?: ContextPreview
  permission_mode?: ChatPermissionMode
  discussion_only?: boolean
  read_only?: boolean
}

export interface ChatRunEvent {
  event: string
  run_id: string
  session_id: number
  seq: number
  [key: string]: unknown
}

export interface ChatSessionDetail extends ChatSession {
  messages: ChatMessage[]
  active_run?: ChatRun | string | null
}

export interface RoutingDecision {
  matched: boolean
  intent: string
  agent: string
  skill?: string
  task_type?: string
  confidence?: string
  basis: string
  candidates: Array<{ intent: string; agent: string; trigger?: string }>
  agent_title?: string
  available_agents?: string[]
  silent_write?: boolean
}

export interface ChatReply {
  session_id: number
  reply: string
  ok: boolean
  error_code?: string | null
  error_message?: string | null
  routing: RoutingDecision
  task_id?: number
  engine?: string
  model?: string
  proposal_ids: number[]
  context_preview?: ContextPreview
}

export interface Settings {
  chat?: { permission_mode: ChatPermissionMode; discussion_only: boolean }
  milestone: { enabled: boolean; step: number }
  appearance: {
    theme: string
    followSystem: boolean
    reduceMotion: boolean
    fontSize: number
    lineHeight: number
    letterSpacing: number
    pageWidth: number
    colorTemperature: number
    contrast: number
  }
  engines: {
    default: string
    overrides: Record<string, string>
    dsh_timeout_seconds: number
    default_provider?: string
    default_model?: string
  }
  budget: { daily_token_limit: number; alert_ratio: number; currency_per_1k_tokens: number }
  editor: { autosave_seconds: number; ghost_text: boolean; milestone_inline: boolean }
  vector?: { model_dir?: string; provider?: string; model?: string }
}

export interface StyleFingerprint {
  id: number
  rel_path: string
  approved: boolean
  han: number
  sentence_len_mean: number
  sentence_len_cv: number
  dialog_ratio: number
  dash_per_1000: number
  created_at: string
}

export interface VectorStats {
  project_id: number
  chunks: number
  documents: number
  by_kind: Record<string, number>
  embedder: { name: string; dim: number; offline: boolean; note: string }
  db_path: string
  ready: boolean
}

export interface OutlineState {
  project_id: number
  project_name: string
  content: string
  candidates: Array<{ title: string; content: string; source: string }>
  locked: { title: string; at: string } | null
  questions: string[]
  frozen: boolean
  history_count: number
  rolling_window: number
}

export interface OperationLogEntry {
  id: number
  project_id: number | null
  action: string
  rel_path: string | null
  detail: string | null
  created_at: string
}

export interface TrashEntry {
  trash_rel: string
  project_name: string
  rel_path: string | null
  kind: string | null
  deleted_at: string | null
}

// ─────────────────────────── 生图工坊 ───────────────────────────

export interface ImageCapabilities {
  adapter_key: string
  size_mode: string
  base_resolutions: string[]
  ratios: string[]
  qualities: string[]
  formats: string[]
  background: boolean
  moderations: string[]
  max_n: number
  supports_edit: boolean
  min_side: number
  max_side: number
  background_map?: Record<string, string>
}

export interface ImageProvider {
  id: number
  provider_id: string
  display_name: string
  model_id: string
  base_url: string
  timeout_seconds: number
  enabled: boolean
  health: string
  has_secret: boolean
  masked_secret: string
  created_at: string
  adapter_key: string
  capabilities: ImageCapabilities
}

export interface ImageJob {
  id: number
  provider_id: string
  model_id: string
  mode: 'generate' | 'edit'
  prompt: string
  params: {
    size?: string
    quality?: string
    output_format?: string
    background?: boolean
    moderation?: string
    n?: number
    input_uploads?: Array<{ upload_id: string; filename: string }>
  }
  status: 'running' | 'succeeded' | 'failed' | 'cancelled'
  error: string
  duration_ms: number | null
  created_at: string
  finished_at: string | null
}

export interface ImageRecord {
  id: number
  job_id: number | null
  provider_id: string
  model_id: string
  prompt: string
  params: {
    size?: string
    quality?: string
    output_format?: string
    background?: boolean
    moderation?: string
    n?: number
    input_uploads?: Array<{ upload_id: string; filename: string }>
  }
  file_rel: string
  size: string
  favorite: boolean
  created_at: string
  url: string
}

export interface ImageUploadRef {
  upload_id: string
  filename: string
  size: number
  media_type: string
}
