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

/** 开书模板偏好：默认模板 + 题材/平台映射。 */
export interface TemplatePrefs {
  default: string
  by_genre: Record<string, string>
  by_platform: Record<string, string>
}

/** 模板内文件树节点（与项目文档树不同：无章节语义）。 */
export interface TemplateTreeNode {
  name: string
  rel_path: string
  type: 'file' | 'dir'
  is_empty?: boolean
  size?: number
  mtime?: number
  children?: TemplateTreeNode[]
}

export interface TemplateTree {
  name: string
  exists: boolean
  nodes: TemplateTreeNode[]
}

export interface TemplateFile {
  rel_path: string
  content: string
  is_empty: boolean
  size: number
  mtime: number
}

/** 模板包（导出/导入）。 */
export interface TemplatePackage {
  name: string
  exported_at?: string
  dirs: string[]
  files: Record<string, string>
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
  retrieval?: ContextRetrievalHit
  type?: 'style'
  style_reference?: { reference_hash: string; samples: StyleReferenceSample[] }
}

export interface StyleReferenceSample {
  id: number
  rel_path: string
  content_hash: string
  excerpt_hash?: string
  injected_hash?: string
  fragment_index?: number
  text?: string
  tokens?: number
  truncated?: boolean
  note?: string
}

export interface StyleReferenceSummary {
  enabled?: boolean
  injected?: boolean
  status?: string
  samples: StyleReferenceSample[]
  reference_hash?: string
  tokens?: number
  selected_samples?: StyleReferenceSample[]
  selected_reference_hash?: string
  selected_tokens?: number
  excluded?: Array<{ id: number; rel_path: string; code: string; reason: string }>
  degradation?: string[]
}

export interface ContextRetrievalHit {
  id?: string
  evidence_id?: string
  rel_path: string
  line_start?: number
  line_end?: number
  hash?: string
  document_hash?: string
  source?: string
  injected?: boolean
  score?: number
}

export interface ContextRetrievalSummary {
  profile?: string
  queries?: string[]
  hits?: ContextRetrievalHit[]
  degradation?: string[]
}

export interface ContextPreview {
  items: ContextBlock[]
  total_tokens: number
  budget_tokens: number
  usage_ratio: number
  degradation: Array<{ level: number; title: string; reason: string }>
  excluded_levels: number[]
  retrieval?: ContextRetrievalSummary
  style_reference?: StyleReferenceSummary
  prose_history_reference?: ProseHistoryReferenceSummary
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
  items: Array<{ 项: string; 判定: string; 证据: string; 核验说明?: string; 证据行?: number; 证据列?: number }>
  consistency: Array<{ 类型: string; 说明: string; 证据: string }>
  revise_instructions: string[]
  counts: { 已完成: number; 未完成: number; 待核实: number }
  ai_used: boolean
  ai_error?: string
  ai_error_code?: string
  task_id?: number
  source_hash?: string
  candidate_hash?: string
  contract_hash?: string
  contract_changed?: boolean
  contract_present?: boolean
  prose_quality?: ProseQuality
  prose_review?: ProseReviewSuggestion[]
  prose_history?: ProseHistoryQuality
}

export interface ProseLocation {
  line: number
  column: number
  end_line: number
  end_column?: number
}

export interface ProseQualityFinding extends ProseLocation {
  kind: string
  severity: 'warning'
  text: string
  related_locations: ProseLocation[]
  reason: string
  suggestion: string
  text_truncated?: boolean
}

export interface ProseQuality {
  version: 1
  blocking: false
  findings: ProseQualityFinding[]
  counters: Record<string, number>
  truncated: boolean
}

export interface ProseHistorySource extends ProseLocation {
  rel_path: string
  content_hash: string
  text?: string
  text_truncated?: boolean
}

export interface ProseHistoryQualityFinding extends Omit<ProseQualityFinding, 'kind' | 'related_locations'> {
  kind: 'cross_chapter_paragraph' | 'cross_chapter_sentence'
  related_locations?: ProseLocation[]
  related_sources: ProseHistorySource[]
}

export interface ProseHistoryQuality {
  version: 1
  blocking: false
  findings: ProseHistoryQualityFinding[]
  counters: Record<string, number>
  truncated: boolean
  status?: 'available' | 'empty' | 'degraded' | 'not_checked'
  sources?: Array<{ rel_path: string; content_hash: string }>
  reference_hash?: string
  excluded?: Array<{ rel_path: string; reason: string }>
  degradation?: string[]
}

export interface ProseHistoryReferenceSummary {
  enabled?: boolean
  injected?: boolean
  status?: string
  samples: ProseHistorySource[]
  reference_hash?: string
  tokens?: number
  truncated?: boolean
  selected_samples?: ProseHistorySource[]
  selected_reference_hash?: string
  selected_tokens?: number
  excluded?: Array<{ rel_path: string; reason: string }>
  degradation?: string[]
}

export interface ProseReviewSuggestion {
  kind: string
  evidence: string
  line: number
  column: number
  reason: string
  suggestion: string
}

export interface SoftDeslopResult {
  suggestions: Array<{ line: number; issue: string; original: string; replacement: string }>
  proposal_ids: number[]
  style_comparison: Record<string, unknown> | null
  ai_used: boolean
  ai_error: string
  ai_error_code?: string
  task_id?: number
  source_changed: boolean
  candidate_hash: string
  rejected_suggestions: Array<{ index: number; reason: string; count?: number }>
}

export interface IngestionResult {
  chapter_rel: string
  summary: string
  timeline_added: number
  ledger_added: number
  foreshadow_added: number
  proposals_created: number[]
  memory_added: number
  characters_present: string[]
  ai_used: boolean
  state_changes_detected: number
  source_hash?: string
  source_changed?: boolean
  status?: string
  skipped?: boolean
  reused?: boolean
  replayed?: boolean
  run_id?: string
  proposal_ids?: number[]
  rejected?: Array<{ kind: string; reason: string }>
  counts?: Record<'timeline' | 'ledger' | 'foreshadow' | 'state', { added: number; updated: number; skipped: number; rejected: number }>
  warnings?: string[]
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

export interface CardEvidence {
  quote: string
  start: number
  end: number
  rel_path: string
  line_start: number
  line_end: number
  document_hash: string
}

export interface CardHighlightEntry {
  entity_id: string
  ref: string
  name: string
  aliases: string[]
  highlight: string
  highlight_mode: 'auto' | 'manual' | 'off'
  highlight_colors: { light: string; dark: string; paper: string }
}

export interface CardParseTask {
  id: string
  project_id: number
  status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  phase: string
  files_total: number
  files_completed: number
  parsed_files: number
  cached_files: number
  current_file: string | null
  failures: Array<{ file: string; error: string }>
  error: string
}

export interface CardItem extends CardHighlightEntry {
  id: string
  fields: Record<string, string>
  field_meta: Record<string, { source: string; evidence: CardEvidence; source_evidence: CardEvidence | null; capabilities?: { rename: boolean; delete: boolean; reason?: string } }>
  summary: string
  source: string
  category: string
  file: string
  field_list: string[]
  evidence: CardEvidence
  extent: CardEvidence | null
  source_hash: string
  stale: boolean
}

export interface CardList {
  project_id: number
  project_name: string
  cards: CardItem[]
  count: number
  counts_by_category: Record<string, number>
  palette: string[]
  categories: Array<{ key: string; label: string; path: string; builtin: boolean }>
  parse_status: 'not_parsed' | 'pending' | 'running' | 'completed' | 'failed'
  task_id: string | null
  task: CardParseTask | null
  stale_files: string[]
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
  foreshadow_planned?: number
  unknown_refs: string[]
  unknown_count: number
}

export interface BibleEntity {
  name: string
  content: string
  summary: string
  ref: string
  rel_path: string
  source: string
  fields?: Record<string, string>
  field_sources?: Record<string, string>
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
  document_hash: string
  is_planned: boolean
  planned_chapter?: string
  record_id?: string
  source?: string
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
  period?: { start: string; end: string }
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
  estimated_tokens: number
  references_estimated_tokens: number
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

/** 每章字数区间（书级覆盖优先，缺省回落全局 settings.writing）。 */
export interface WritingPrefs {
  chapter_min_words: number
  chapter_max_words: number
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
  estimated_tokens?: number
  call_id?: string
  tool: string
  label: string
  args: Record<string, unknown>
  status: 'running' | 'done' | 'error' | 'cancelled'
  summary?: string
  elapsed_ms?: number
  kind?: 'tool' | 'skill' | 'agent' | 'phase' | 'plan_step'
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
    reason?: string
    /** 写域越界信息（本轮范围 / 目标材料 / 授权粒度选项），仅越界批准卡带 */
    scope?: {
      out_of_scope?: boolean
      material?: string
      reason?: string
      label?: string
      options?: Array<{ id: string; label: string }>
      resolved?: string
    }
    [key: string]: unknown
  }
  status: 'pending' | 'answered' | 'cancelled' | 'interrupted' | 'expired'
  response?: ChatInteractionResponse | null
  revision?: number
}

/** scope=task：批准并在本次任务内不再询问；scope=material：本任务内允许改这类材料；scope=once：仅此次批准。 */
export type ChatInteractionResponse = { decision: 'approve' | 'reject'; scope?: 'task' | 'once' | 'material' } | {
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
  /** Descriptive page context; server-side session identity grants authority. */
  project_id?: number
  page_type?: string
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
  plan_state?: { fingerprint?: string; active_step?: number; steps: Array<{ step_id?: string; step?: number; label?: string; agent?: string; status: string; write_targets?: string[]; receipts?: Array<Record<string, unknown>>; failure_reason?: string; error?: string; reused?: boolean }> }
  completion?: { status?: string; message?: string; receipts?: Array<Record<string, unknown>>; error_code?: string }
  metrics?: Record<string, number | string | null>
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
  skills?: string[]
  write_targets?: string[]
  read_only_refs?: string[]
  plan?: Array<{ step: number; agent: string; write_targets: string[]; read_only_refs: string[]; note?: string }>
  source?: string
  scope_unresolved?: boolean
  reason?: string
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
  writing?: {
    always_inject_skills: string[]
    chapter_min_words: number
    chapter_max_words: number
    auto_deslop: { enabled: boolean; max_rounds: number }
  }
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
  note?: string
  content_hash?: string
  sample_version?: number
  usable?: boolean
  reference_status?: string
  reference_reason?: string
}

export interface StyleReferenceMetrics {
  samples: number
  sentence_len_mean: number
  sentence_len_cv: number
  dialog_ratio: number
  dash_per_1000: number
  ellipsis_per_1000: number
  top_bigrams: Array<{ word: string; count: number }>
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

export interface OutlineCandidate {
  id: string
  title: string
  content: string
  source: string
  parent_id?: string
  conversation?: Array<{ role: 'user' | 'assistant'; content: string }>
}

export interface OutlineState {
  project_id: number
  project_name: string
  content: string
  candidates: OutlineCandidate[]
  locked: { title: string; at: string; candidate_id?: string } | null
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
  project_id?: number | null
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
