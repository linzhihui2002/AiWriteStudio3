import WorkspacePage, { ResourceState, useResourceRequest } from '../components/WorkspacePage'
/**
 * 设置（全局页，不依赖项目上下文）。
 *
 * 分区：模型供应商 / 引擎路由 / 外观与护眼 / 编辑器偏好 / 预算与用量 /
 *       技能 / Agent / 规则 / 向量检索 / 关于与隔离。
 *
 * 纪律：复用全站主题 token，不写死颜色；控件全部为原生
 * button/input/select/label（键盘可达）；分区数据在切换分区时重新拉取。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Modal from '../components/Modal'
import CredentialInput from '../components/CredentialInput'
import '../styles/settingsProviders.css'
import TemplateManager from '../components/TemplateManager'
import { useConfirm } from '../components/ConfirmDialog'
import {
  compileRules,
  createAgent,
  createSkill,
  deleteAgent,
  deleteProvider,
  deleteRule,
  deleteSkill,
  draftAgent,
  duplicateAgent,
  enableProvider,
  generateSkill,
  getEngines,
  getHealth,
  getSkill,
  getUsage,
  healthCheck,
  importModelsMd,
  importProvidersYaml,
  importSkill,
  listAgents,
  listModels,
  listProviders,
  listRules,
  listSkills,
  projectProviders,
  resolvedRules,
  setAgentModel,
  setDefaultProvider,
  setSkillEnabled,
  setVectorModel,
  syncSkills,
  updateAgent,
  updateEngines,
  updateSkill,
  upsertProvider,
  upsertRule,
  vectorModelInfo,
} from '../api/client'
import type {
  AgentItem,
  DefaultTarget,
  Health,
  Provider,
  ProviderModel,
  RoutingInfo,
  RuleItem,
  Settings,
  SkillItem,
  UsageSummary,
} from '../api/types'
import { useSettings } from '../state/useSettings'
import { useToast, errorMessage } from '../state/useToast'
import { applyAppearance } from '../state/useAppearance'
import { THEMES, type Theme } from '../theme/useTheme'
import { CREDENTIAL_FIELD_EXTRA, NO_AUTOFILL } from '../lib/autofill'
import { CHAT_PERMISSION_OPTIONS } from '../lib/chatState'

// ─────────────────────────── 常量与本地类型 ───────────────────────────

const SECTIONS = [
  { key: 'providers', label: '模型供应商' },
  { key: 'engines', label: '引擎路由' },
  { key: 'appearance', label: '外观与护眼' },
  { key: 'editor', label: '编辑器偏好' },
  { key: 'chat', label: '对话偏好' },
  { key: 'templates', label: '模板管理' },
  { key: 'budget', label: '预算与用量' },
  { key: 'skills', label: '技能' },
  { key: 'agents', label: 'Agent' },
  { key: 'rules', label: '规则' },
  { key: 'vector', label: '向量检索' },
  { key: 'about', label: '关于与隔离' },
] as const

type SectionKey = (typeof SECTIONS)[number]['key']

/** 分区初始值：支持 `?section=xxx` 深链（如书架「管理模板」跳转）；非法值回落首个分区。 */
function initialSection(search: URLSearchParams): SectionKey {
  const requested = search.get('section')
  const keys: readonly string[] = SECTIONS.map((item) => item.key)
  if (requested && keys.includes(requested)) return requested as SectionKey
  return 'providers'
}

/** 与后端 settings_service.DEFAULT_SETTINGS.appearance 一致。 */
const DEFAULT_APPEARANCE: Settings['appearance'] = {
  theme: 'light',
  followSystem: false,
  reduceMotion: false,
  fontSize: 17,
  lineHeight: 1.9,
  letterSpacing: 0,
  pageWidth: 760,
  colorTemperature: 0,
  contrast: 1,
}

const ENGINE_OPTIONS = [
  { value: 'dsh-headless', label: 'dsh-headless（内置 dsh 无头）' },
  { value: 'direct-api', label: 'direct-api（直连 API）' },
]

/** 对话偏好：在共享 Settings.chat 类型上补齐后端 settings_service 的写域与意图判定三项。 */
type ChatPrefs = NonNullable<Settings['chat']> & {
  scope_strictness?: 'ask' | 'reject'
  scope_limit_full?: boolean
  intent_llm?: boolean
}

interface ProviderForm {
  provider_id: string
  display_name: string
  base_url: string
  models: ProviderModel[]
  capabilities: string[]
  timeout_seconds: number
  api_key: string
  enabled: boolean
}

interface AgentForm {
  name: string
  title: string
  description: string
  system_prompt: string
  skills: string[]
  tools: string[]
  materials: string[]
  boundaries: string
  capabilities: AgentCapability[]
  provider_id: string
  model_id: string
}

interface RuleConflict {
  key: string
  winner: { id: string; scope: string; name: string }
  losers: Array<{ id: string; scope: string; name: string }>
  basis: string
}

interface CompileResult {
  path: string
  chars: number
  compiled_from: string
  rules: number
}

interface SkillSyncResult {
  count?: number
  total_bytes?: number
  core_bytes?: number
  estimated_tokens?: number
  target_root?: string
  warnings?: string[]
  errors?: Array<{ path: string; error: string }>
}

/** 后端 /skills 在既有 SkillItem 上追加的绑定与引用字段（后端只加键，不删既有键）。 */
interface SkillRow extends SkillItem {
  appliesTo?: string[]
  reference_files?: string[]
  references_bytes?: number
  agents?: string[]
  suggested_agents?: string[]
  missing_references?: string[]
}

/** Agent 能力声明（后端 _to_public 已回传 capabilities，前端只读展示）。 */
interface AgentCapability {
  intent?: string
  triggers?: string[]
  skill?: string
  task_type?: string
}

/** 后端 Agent 定义上的声明式字段（materials / capabilities / boundaries）。 */
interface AgentRow extends AgentItem {
  materials?: string[]
  capabilities?: AgentCapability[]
  boundaries?: string
}

/** 主责材料取值兜底（与后端 agent_service.MATERIAL_DIRS 保持一致）。 */
const FALLBACK_MATERIAL_DIRS = ['章节', '设定', '大纲', '状态', '备忘录']

interface VectorModelFile {
  path: string
  size_bytes: number
  sha256: string
}

interface VectorModelInfo {
  active: { name: string; dim: number; model_dir: string; offline: boolean; note: string }
  default_dim: number
  models_dir: string
  local_files: VectorModelFile[]
  options: Array<{ name: string; dim?: number; download_required: boolean; note: string }>
  explicit_path_supported: boolean
}

interface VectorSetResult {
  note?: string
  active?: VectorModelInfo['active']
}

const EMPTY_PROVIDER_FORM: ProviderForm = {
  provider_id: '',
  display_name: '',
  base_url: '',
  models: [],
  capabilities: [],
  timeout_seconds: 60,
  api_key: '',
  enabled: true,
}

/** 上下文窗口 / 最大输出的快捷预设（也可直接手填数值）。 */
const CONTEXT_WINDOW_PRESETS = [
  { label: '128K', value: 131072 },
  { label: '256K', value: 262144 },
  { label: '512K', value: 524288 },
  { label: '1M', value: 1048576 },
]

const MAX_TOKENS_PRESETS = [
  { label: '4K', value: 4096 },
  { label: '16K', value: 16384 },
  { label: '32K', value: 32768 },
  { label: '128K', value: 131072 },
]

const EMPTY_MODEL_ROW: ProviderModel = {
  id: '',
  name: '',
  context_window: 0,
  max_tokens: 0,
}

const EMPTY_DEFAULT_TARGET: DefaultTarget = { provider_id: '', model_id: '' }

/** 模型行的可读标签：优先展示名称，其次 id。 */
function modelLabel(model: ProviderModel): string {
  return model.name && model.name !== model.id ? `${model.name}（${model.id}）` : model.id
}

/** 模型下拉里的「自定义…」哨兵值（只在前端使用，不写入后端）。 */
const CUSTOM_MODEL = '__custom__'

/** 取某供应商已配置的模型列表；未选供应商时返回空数组。 */
function providerModels(providers: Provider[], providerId: string): ProviderModel[] {
  return providers.find((item) => item.provider_id === providerId)?.models ?? []
}

interface ModelPickerProps {
  providers: Provider[]
  providerId: string
  value: string
  custom: boolean
  onCustom: (on: boolean) => void
  onPick: (modelId: string) => void
}

/**
 * 模型选择：按所选供应商的模型列表下拉选择，并保留「自定义…」手填入口。
 * 未选供应商时只读展示（模型由全局默认决定）；供应商未配模型时直接给手填框。
 */
function ModelPicker({ providers, providerId, value, custom, onCustom, onPick }: ModelPickerProps) {
  const models = providerModels(providers, providerId)
  const legacy = value !== '' && !models.some((model) => model.id === value)
  const manual = custom || (!models.length && !legacy)

  if (!providerId) {
    return (
      <label className="field" style={{ flex: '1 1 12rem' }}>
        <span className="field__label">模型</span>
        <select className="select" value={value} disabled>
          {value ? (
            <option value={value}>{value}（沿用全局供应商）</option>
          ) : (
            <option value="">（跟随全局默认模型）</option>
          )}
        </select>
      </label>
    )
  }

  return (
    <>
      <label className="field" style={{ flex: '1 1 12rem' }}>
        <span className="field__label">模型</span>
        {manual ? (
          <input
            autoComplete={NO_AUTOFILL}
            className="input"
            value={value}
            onChange={(event) => onPick(event.target.value)}
            placeholder="例如 deepseek-chat"
          />
        ) : (
          <select
            className="select"
            value={value}
            onChange={(event) => {
              const next = event.target.value
              if (next === CUSTOM_MODEL) {
                onCustom(true)
                return
              }
              onCustom(false)
              onPick(next)
            }}
          >
            {value === '' ? (
              <option value="" disabled>
                （未选择模型）
              </option>
            ) : null}
            {models.map((model) => (
              <option key={model.id} value={model.id}>
                {modelLabel(model)}
              </option>
            ))}
            {legacy ? <option value={value}>{value}（不在该供应商模型列表中）</option> : null}
            <option value={CUSTOM_MODEL}>自定义…</option>
          </select>
        )}
      </label>
      {!models.length ? (
        <span className="field__hint" style={{ flex: '1 1 12rem' }}>
          该供应商尚未配置模型，可直接手填 id；建议先到「模型供应商」拉取模型列表。
        </span>
      ) : null}
    </>
  )
}

const EMPTY_AGENT_FORM: AgentForm = {
  name: '',
  title: '',
  description: '',
  system_prompt: '',
  skills: [],
  tools: [],
  materials: [],
  boundaries: '',
  capabilities: [],
  provider_id: '',
  model_id: '',
}

/** 拆 SKILL.md 的 frontmatter（只取 name/description，够用即可）。 */
function parseSkillText(text: string): { name: string; description: string; body: string } {
  const match = /^---[ \t]*\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(text)
  if (!match) return { name: '', description: '', body: text }
  const meta: Record<string, string> = {}
  match[1].split('\n').forEach((line) => {
    const index = line.indexOf(':')
    if (index <= 0) return
    const key = line.slice(0, index).trim()
    const value = line.slice(index + 1).trim().replace(/^["']|["']$/g, '')
    if (key) meta[key] = value
  })
  return {
    name: meta.name ?? '',
    description: meta.description ?? '',
    body: text.slice(match[0].length),
  }
}

function splitList(text: string): string[] {
  return text
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

const SOURCE_LABEL: Record<string, string> = {
  builtin: '内置',
  workbench_custom: '自定义',
  imported: '导入',
  invalid: '无效',
}

// ─────────────────────────── 页面 ───────────────────────────

export default function SettingsPage() {
  const { settings, loading: settingsLoading, error: settingsError, patch, setTheme, refresh } = useSettings()
  const { push } = useToast()
  const [confirm, confirmNode] = useConfirm()
  const [searchParams, setSearchParams] = useSearchParams()

  const active = initialSection(searchParams)
  const setActive = (section: SectionKey) => { const next = new URLSearchParams(searchParams); next.set('section', section); setSearchParams(next) }
  const providersResource = useResourceRequest('providers')
  const enginesResource = useResourceRequest('engines')
  const budgetResource = useResourceRequest('budget')
  const skillsResource = useResourceRequest('skills')
  const agentsResource = useResourceRequest('agents')
  const vectorResource = useResourceRequest('vector')
  const aboutResource = useResourceRequest('about')
  const [reloadToken, setReloadToken] = useState(0)

  const notifyError = useCallback(
    (error: unknown) => {
      push(errorMessage(error), 'error')
    },
    [push],
  )

  // ── 供应商 ──
  const [providers, setProviders] = useState<Provider[]>([])
  const [capabilityTags, setCapabilityTags] = useState<string[]>([])
  const [cipher, setCipher] = useState('')
  const [providerForm, setProviderForm] = useState<ProviderForm>(EMPTY_PROVIDER_FORM)
  const [editingProviderId, setEditingProviderId] = useState('')
  const [providerModal, setProviderModal] = useState<
    { mode: 'create' | 'edit'; providerId?: string } | null
  >(null)
  const [modelOptions, setModelOptions] = useState<string[]>([])
  const [pulledSelection, setPulledSelection] = useState<string[]>([])
  const [defaultTarget, setDefaultTarget] = useState<DefaultTarget>(EMPTY_DEFAULT_TARGET)
  const [defaultDraft, setDefaultDraft] = useState<DefaultTarget>(EMPTY_DEFAULT_TARGET)
  const [healthResults, setHealthResults] = useState<
    Record<string, { ok: boolean; reason?: string; model_count?: number }>
  >({})
  const [yamlText, setYamlText] = useState('')
  const [providerNote, setProviderNote] = useState('')
  const [providerSaving, setProviderSaving] = useState(false)
  const providerSaveInFlight = useRef(false)
  const [providerPulling, setProviderPulling] = useState(false)
  const providerRequestVersion = useRef(0)

  // ── 引擎 ──
  const [routing, setRouting] = useState<RoutingInfo | null>(null)
  const [overrideRows, setOverrideRows] = useState<Record<string, string>>({})
  const [newOverrideKey, setNewOverrideKey] = useState('')
  const [newOverrideEngine, setNewOverrideEngine] = useState('dsh-headless')

  // ── 外观 ──
  const [appearanceDraft, setAppearanceDraft] = useState<Settings['appearance'] | null>(null)
  const appearanceTimer = useRef<number | null>(null)

  // ── 用量 ──
  const [usage, setUsage] = useState<UsageSummary | null>(null)

  // ── 技能 ──
  const [skills, setSkills] = useState<SkillRow[]>([])
  const [skillEstimatedTokens, setSkillEstimatedTokens] = useState(0)
  const [skillTargetRoot, setSkillTargetRoot] = useState('')
  const [syncResult, setSyncResult] = useState<SkillSyncResult | null>(null)
  const [skillModal, setSkillModal] = useState<
    { mode: 'edit' | 'create' | 'import' | 'generate'; name?: string } | null
  >(null)
  const [skillContent, setSkillContent] = useState('')
  const [skillForm, setSkillForm] = useState({
    name: '',
    description: '',
    body: '',
    importText: '',
    importName: '',
  })
  const [skillDraft, setSkillDraft] = useState<{ name: string; content: string } | null>(null)
  const [skillGenerateForm, setSkillGenerateForm] = useState({ description: '', name: '' })

  // ── Agent ──
  const [agents, setAgents] = useState<AgentRow[]>([])
  const [agentBuiltinCount, setAgentBuiltinCount] = useState(0)
  const [toolWhitelist, setToolWhitelist] = useState<string[]>([])
  const [materialDirs, setMaterialDirs] = useState<string[]>(FALLBACK_MATERIAL_DIRS)
  const [agentModal, setAgentModal] = useState<
    { mode: 'form' | 'draft'; name?: string } | null
  >(null)
  const [agentForm, setAgentForm] = useState<AgentForm>(EMPTY_AGENT_FORM)
  // Agent 卡片的行内动作（复制 / 配置模型）：不再用单字段弹窗
  const [agentInline, setAgentInline] = useState<{ name: string; mode: 'duplicate' | 'model' } | null>(null)
  const [duplicateName, setDuplicateName] = useState('')
  const [modelBinding, setModelBinding] = useState({ provider_id: '', model_id: '' })
  /** 模型字段是否处于「自定义…」手填模式（行内面板与弹窗互斥，共用一个标记）。 */
  const [modelCustom, setModelCustom] = useState(false)
  const [agentDraftForm, setAgentDraftForm] = useState({ description: '', name: '' })
  const [agentDraft, setAgentDraft] = useState<AgentForm | null>(null)
  const [agentDraftParams, setAgentDraftParams] = useState('{"temperature": 0.5}')

  // ── 规则 ──
  const [ruleProjectId, setRuleProjectId] = useState('')
  const rulesResource = useResourceRequest(ruleProjectId)
  const ruleProjectIdRef = useRef('')
  const [ruleGlobal, setRuleGlobal] = useState<RuleItem[]>([])
  const [ruleProject, setRuleProject] = useState<RuleItem[]>([])
  const [ruleSkill, setRuleSkill] = useState<RuleItem[]>([])
  const [resolved, setResolved] = useState<{
    effective: RuleItem[]
    conflicts: RuleConflict[]
    order: string[]
  } | null>(null)
  const [ruleForm, setRuleForm] = useState({
    name: '',
    body: '',
    keys: '',
    priority: 100,
    confirmed: false,
    enabled: true,
  })
  const [compileResult, setCompileResult] = useState<CompileResult | null>(null)

  // ── 向量 ──
  const [vectorInfo, setVectorInfo] = useState<VectorModelInfo | null>(null)
  const [vectorForm, setVectorForm] = useState({ model_dir: '', provider: '', model: '' })
  const [vectorNote, setVectorNote] = useState('')

  // ── 关于 ──
  const [health, setHealth] = useState<Health | null>(null)

  // ─────────────────────────── 加载器 ───────────────────────────

  const loadProviders = useCallback(async () => {
    const token = providersResource.begin()
    try {
      const data = await listProviders()
      if (!providersResource.accept(token)) return
      setProviders(data.providers)
      setCapabilityTags(data.capability_tags)
      setCipher(data.cipher)
      setDefaultTarget(data.defaults ?? EMPTY_DEFAULT_TARGET)
      providersResource.finish(token)
    } catch (error) {
      providersResource.fail(token, errorMessage(error))
    }
  }, [notifyError])

  const loadEngines = useCallback(async () => {
    const token = enginesResource.begin()
    try {
      const data = await getEngines()
      if (!enginesResource.accept(token)) return
      setRouting(data)
      setOverrideRows(data.global_overrides)
      enginesResource.finish(token)
    } catch (error) {
      enginesResource.fail(token, errorMessage(error))
    }
  }, [notifyError])

  const loadUsage = useCallback(async () => {
    const token = budgetResource.begin()
    try {
      const data = await getUsage()
      if (!budgetResource.accept(token)) return
      setUsage(data)
      budgetResource.finish(token)
    } catch (error) {
      budgetResource.fail(token, errorMessage(error))
    }
  }, [notifyError])

  const loadSkills = useCallback(async () => {
    const token = skillsResource.begin()
    try {
      const data = (await listSkills()) as unknown as {
        skills: SkillRow[]
        estimated_tokens: number
        target_root: string
      }
      if (!skillsResource.accept(token)) return
      setSkills(data.skills)
      setSkillEstimatedTokens(data.estimated_tokens)
      setSkillTargetRoot(data.target_root)
      skillsResource.finish(token)
    } catch (error) {
      skillsResource.fail(token, errorMessage(error))
    }
  }, [notifyError])

  const loadAgents = useCallback(async () => {
    const token = agentsResource.begin()
    try {
      const data = (await listAgents(true)) as unknown as {
        agents: AgentRow[]
        builtin_count: number
        tool_whitelist: string[]
        material_dirs?: string[]
      }
      if (!agentsResource.accept(token)) return
      setAgents(data.agents)
      setAgentBuiltinCount(data.builtin_count)
      setToolWhitelist(data.tool_whitelist)
      setMaterialDirs(data.material_dirs ?? FALLBACK_MATERIAL_DIRS)
      agentsResource.finish(token)
    } catch (error) {
      agentsResource.fail(token, errorMessage(error))
    }
  }, [notifyError])

  const loadRules = useCallback(async () => {
    const token = rulesResource.begin()
    const raw = ruleProjectIdRef.current.trim()
    const projectId = raw ? Number(raw) : undefined
    try {
      const [list, effective] = await Promise.all([
        listRules(projectId),
        resolvedRules(projectId),
      ])
      if (!rulesResource.accept(token)) return
      setRuleGlobal(list.global as unknown as RuleItem[])
      setRuleProject(list.project as unknown as RuleItem[])
      setRuleSkill(list.skill_builtin as unknown as RuleItem[])
      setResolved({
        effective: effective.effective as unknown as RuleItem[],
        conflicts: effective.conflicts as unknown as RuleConflict[],
        order: effective.order,
      })
      rulesResource.finish(token)
    } catch (error) {
      rulesResource.fail(token, errorMessage(error))
    }
  }, [notifyError, ruleProjectId])

  const loadVector = useCallback(async () => {
    const token = vectorResource.begin()
    try {
      const data = (await vectorModelInfo()) as unknown as VectorModelInfo
      if (!vectorResource.accept(token)) return
      setVectorInfo(data)
      setVectorForm({
        model_dir: data.active?.model_dir ?? '',
        provider: '',
        model: '',
      })
      vectorResource.finish(token)
    } catch (error) {
      vectorResource.fail(token, errorMessage(error))
    }
  }, [notifyError])

  const loadHealth = useCallback(async () => {
    const token = aboutResource.begin()
    try {
      const data = await getHealth()
      if (!aboutResource.accept(token)) return
      setHealth(data)
      aboutResource.finish(token)
    } catch (error) {
      aboutResource.fail(token, errorMessage(error))
    }
  }, [notifyError])

  useEffect(() => {
    void loadHealth()
  }, [loadHealth])

  // 全局默认模型：后端值变化时同步编辑草稿
  useEffect(() => {
    setDefaultDraft(defaultTarget)
  }, [defaultTarget])

  // 分区数据：切换到该分区（或点击「重新读取」）时拉取
  useEffect(() => {
    if (active === 'providers') void loadProviders()
    else if (active === 'engines') void loadEngines()
    else if (active === 'budget') void loadUsage()
    else if (active === 'skills') void loadSkills()
    else if (active === 'agents') {
      void loadAgents()
      void loadSkills()
      void loadProviders()
    } else if (active === 'rules') void loadRules()
    else if (active === 'vector') void loadVector()
    else if (active === 'about') void loadHealth()
  }, [
    active,
    reloadToken,
    loadProviders,
    loadEngines,
    loadUsage,
    loadSkills,
    loadAgents,
    loadRules,
    loadVector,
    loadHealth,
  ])

  // 外观草稿：跟随后端设置（拖动中的未落盘改动不覆盖）
  useEffect(() => {
    if (!settings) return
    if (appearanceTimer.current !== null) return
    setAppearanceDraft(settings.appearance)
  }, [settings])

  // 本地即时预览：与正文同源的 CSS 变量
  useEffect(() => {
    if (appearanceDraft) applyAppearance(appearanceDraft)
  }, [appearanceDraft])

  useEffect(
    () => () => {
      if (appearanceTimer.current !== null) window.clearTimeout(appearanceTimer.current)
    },
    [],
  )

  // ─────────────────────────── 通用动作 ───────────────────────────

  const saveSettings = useCallback(
    (payload: Record<string, unknown>) => {
      void patch(payload).then((result) => {
        if (!result) push('设置保存失败', 'error')
      })
    },
    [patch, push],
  )

  const flushAppearance = useCallback(
    (next: Settings['appearance']) => {
      void patch({ appearance: next }).then((result) => {
        if (!result) push('外观设置保存失败', 'error')
      })
    },
    [patch, push],
  )

  const saveAppearanceNow = useCallback(
    (next: Settings['appearance']) => {
      setAppearanceDraft(next)
      flushAppearance(next)
    },
    [flushAppearance],
  )

  const saveAppearanceDebounced = useCallback(
    (next: Settings['appearance']) => {
      setAppearanceDraft(next)
      if (appearanceTimer.current !== null) window.clearTimeout(appearanceTimer.current)
      appearanceTimer.current = window.setTimeout(() => {
        appearanceTimer.current = null
        flushAppearance(next)
      }, 300)
    },
    [flushAppearance],
  )

  const handleReload = () => {
    void refresh()
    setReloadToken((value) => value + 1)
  }

  // ─────────────────────────── 供应商动作 ───────────────────────────

  const updateProviderForm = (partial: Partial<ProviderForm>) =>
    setProviderForm((current) => ({ ...current, ...partial }))

  const resetProviderForm = () => {
    providerRequestVersion.current += 1
    setProviderPulling(false)
    setProviderForm(EMPTY_PROVIDER_FORM)
    setEditingProviderId('')
    setModelOptions([])
    setPulledSelection([])
  }

  const startEditProvider = (provider: Provider) => {
    providerRequestVersion.current += 1
    setProviderPulling(false)
    setProviderForm({
      provider_id: provider.provider_id,
      display_name: provider.display_name,
      base_url: provider.base_url,
      models: provider.models,
      capabilities: provider.capabilities,
      timeout_seconds: provider.timeout_seconds,
      api_key: '',
      enabled: provider.enabled,
    })
    setEditingProviderId(provider.provider_id)
    setModelOptions([])
    setPulledSelection([])
    setProviderModal({ mode: 'edit', providerId: provider.provider_id })
  }

  /** 打开「新建供应商」弹窗（清空上次编辑残留）。 */
  const startCreateProvider = () => {
    resetProviderForm()
    setProviderModal({ mode: 'create' })
  }

  /** 关闭弹窗并复位表单：取消 / Esc / 点遮罩 / 保存后都走这里。 */
  const closeProviderModal = () => {
    if (providerSaving) return
    resetProviderForm()
    setProviderModal(null)
  }

  // ── 模型行编辑 ──

  const updateModelRow = (index: number, partial: Partial<ProviderModel>) =>
    setProviderForm((current) => ({
      ...current,
      models: current.models.map((model, at) => (at === index ? { ...model, ...partial } : model)),
    }))

  const addModelRow = () =>
    setProviderForm((current) => ({ ...current, models: [...current.models, { ...EMPTY_MODEL_ROW }] }))

  const removeModelRow = (index: number) =>
    setProviderForm((current) => ({
      ...current,
      models: current.models.filter((_model, at) => at !== index),
    }))

  const appendPulledModels = () => {
    const existing = new Set(providerForm.models.map((model) => model.id))
    const selected = [...new Set(pulledSelection)].filter((id) => !existing.has(id))
    if (!selected.length) {
      push('请先勾选要添加的模型', 'error')
      return
    }
    setProviderForm((current) => {
      const existing = new Set(current.models.map((model) => model.id))
      const added = selected
        .filter((id) => !existing.has(id))
        .map((id) => ({ ...EMPTY_MODEL_ROW, id }))
      return { ...current, models: [...current.models, ...added] }
    })
    push(`已添加 ${selected.length} 个模型`, 'success')
    setPulledSelection([])
  }

  const submitProvider = async () => {
    if (providerSaveInFlight.current) return
    const providerId = providerForm.provider_id.trim()
    if (!providerId) {
      push('请填写 provider_id', 'error')
      return
    }
    providerSaveInFlight.current = true
    setProviderSaving(true)
    try {
      await upsertProvider({
        provider_id: providerId,
        display_name: providerForm.display_name,
        base_url: providerForm.base_url,
        models: providerForm.models.filter((model) => model.id.trim()),
        capabilities: providerForm.capabilities,
        timeout_seconds: Number(providerForm.timeout_seconds) || 60,
        api_key: providerForm.api_key.trim() ? providerForm.api_key.trim() : null,
        enabled: providerForm.enabled,
      })
      push('供应商已保存', 'success')
      resetProviderForm()
      setProviderModal(null)
      await loadProviders()
    } catch (error) {
      notifyError(error)
    } finally {
      providerSaveInFlight.current = false
      setProviderSaving(false)
    }
  }

  const pullModels = async () => {
    if (providerPulling) return
    const version = providerRequestVersion.current
    setProviderPulling(true)
    try {
      const payload = editingProviderId
        ? { provider_id: editingProviderId }
        : { base_url: providerForm.base_url, api_key: providerForm.api_key.trim() }
      const data = await listModels(payload)
      if (providerRequestVersion.current !== version) return
      setModelOptions(data.models.map((item) => item.id))
      setPulledSelection([])
      push(`已拉取 ${data.count} 个模型`, 'success')
    } catch (error) {
      if (providerRequestVersion.current === version) notifyError(error)
    } finally {
      if (providerRequestVersion.current === version) setProviderPulling(false)
    }
  }

  const saveDefaultTarget = async (payload: DefaultTarget) => {
    try {
      const saved = await setDefaultProvider(payload)
      setDefaultTarget(saved)
      await loadProviders()
      push(
        saved.provider_id ? `默认模型已设为 ${saved.provider_id} / ${saved.model_id}` : '已清除默认模型',
        'success',
      )
    } catch (error) {
      notifyError(error)
    }
  }

  const runHealth = async (providerId: string) => {
    try {
      const data = await healthCheck(providerId)
      setHealthResults((current) => ({ ...current, [providerId]: data }))
      push(
        data.ok ? '健康检查通过' : `健康检查未通过：${data.reason || '原因未知'}`,
        data.ok ? 'success' : 'error',
      )
      await loadProviders()
    } catch (error) {
      notifyError(error)
    }
  }

  // 启用前的「未做健康检查」只是提醒，不是危险操作：直接启用，改由卡片常驻 warn 标记提示
  const toggleProvider = async (provider: Provider) => {
    const next = !provider.enabled
    try {
      await enableProvider(provider.provider_id, next)
      push(next ? '已启用供应商' : '已停用供应商', 'success')
      await loadProviders()
    } catch (error) {
      notifyError(error)
    }
  }

  const removeProvider = async (providerId: string) => {
    const confirmed = await confirm({
      title: '删除供应商',
      message: `删除供应商「${providerId}」？其本机凭据引用与 dsh 投影会一并更新。`,
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    try {
      await deleteProvider(providerId)
      push('已删除供应商', 'success')
      if (editingProviderId === providerId) closeProviderModal()
      await loadProviders()
    } catch (error) {
      notifyError(error)
    }
  }

  const projectToDsh = async () => {
    try {
      const result = await projectProviders()
      setProviderNote(JSON.stringify(result, null, 2))
      push('已投影到独立 dsh-home', 'success')
    } catch (error) {
      notifyError(error)
    }
  }

  const importYaml = async () => {
    if (!yamlText.trim()) {
      push('请先粘贴 settings.yaml 文本', 'error')
      return
    }
    try {
      const result = await importProvidersYaml(yamlText)
      setProviderNote(
        `导入 ${result.imported.length} 个供应商：${result.imported
          .map((item) => item.provider_id)
          .join('、')}\n${result.note}`,
      )
      push('已导入非敏感配置', 'success')
      await loadProviders()
    } catch (error) {
      notifyError(error)
    }
  }

  const importMdKeys = async () => {
    try {
      const result = await importModelsMd()
      setProviderNote(
        `在 模型API.md 中找到 ${result.found} 条 Key，已迁入 ${result.migrated.length} 条：` +
          `${result.migrated.map((item) => `${item.secret_ref}(${item.masked})`).join('、') || '无'}\n${result.note}`,
      )
      push('凭据迁移完成', 'success')
      await loadProviders()
    } catch (error) {
      notifyError(error)
    }
  }

  // ─────────────────────────── 引擎动作 ───────────────────────────

  const saveEngines = async (payload: Record<string, unknown>) => {
    try {
      const data = await updateEngines(payload)
      setRouting(data)
      setOverrideRows(data.global_overrides)
      push('引擎设置已保存', 'success')
    } catch (error) {
      notifyError(error)
    }
  }

  const setOverride = (taskType: string, engine: string) => {
    const next = { ...overrideRows }
    if (engine) next[taskType] = engine
    else delete next[taskType]
    setOverrideRows(next)
    void saveEngines({ overrides: next })
  }

  const addOverride = () => {
    const taskType = newOverrideKey.trim()
    if (!taskType) {
      push('请填写任务类型', 'error')
      return
    }
    setOverride(taskType, newOverrideEngine)
    setNewOverrideKey('')
  }

  const extraOverrides = Object.keys(overrideRows).filter(
    (key) => routing !== null && !(key in routing.decision_table),
  )

  // ─────────────────────────── 技能动作 ───────────────────────────

  const openSkillEditor = async (name: string) => {
    try {
      const data = await getSkill(name)
      setSkillContent(data.content)
      setSkillModal({ mode: 'edit', name })
    } catch (error) {
      notifyError(error)
    }
  }

  const openSkillCreate = () => {
    setSkillForm({ name: '', description: '', body: '', importText: '', importName: '' })
    setSkillDraft(null)
    setSkillGenerateForm({ description: '', name: '' })
    setSkillModal({ mode: 'create' })
  }

  const openSkillMode = (mode: 'import' | 'generate') => {
    setSkillForm((current) => ({ ...current, importText: '', importName: '' }))
    setSkillDraft(null)
    setSkillGenerateForm({ description: '', name: '' })
    setSkillModal({ mode })
  }

  const saveSkillContent = async () => {
    if (!skillModal?.name) return
    try {
      await updateSkill(skillModal.name, skillContent)
      push('技能已更新', 'success')
      setSkillModal(null)
      await loadSkills()
    } catch (error) {
      notifyError(error)
    }
  }

  const createSkillNow = async () => {
    try {
      await createSkill({
        name: skillForm.name.trim(),
        description: skillForm.description.trim(),
        body: skillForm.body,
      })
      push('技能已创建', 'success')
      setSkillModal(null)
      await loadSkills()
    } catch (error) {
      notifyError(error)
    }
  }

  const importSkillNow = async () => {
    try {
      await importSkill(skillForm.importText, skillForm.importName.trim() || undefined)
      push('技能已导入', 'success')
      setSkillModal(null)
      await loadSkills()
    } catch (error) {
      notifyError(error)
    }
  }

  const generateSkillDraft = async () => {
    if (!skillGenerateForm.description.trim()) {
      push('请先描述技能用途', 'error')
      return
    }
    try {
      const result = await generateSkill(skillGenerateForm.description, skillGenerateForm.name, false)
      setSkillDraft({ name: result.name, content: result.content })
      if (!result.ok && result.error) push(`生成未成功：${result.error}`, 'error')
    } catch (error) {
      notifyError(error)
    }
  }

  const saveSkillDraft = async () => {
    if (!skillDraft) return
    const parsed = parseSkillText(skillDraft.content)
    const name = parsed.name || skillDraft.name || skillGenerateForm.name.trim()
    if (!name) {
      push('无法确定技能名，请补填名称后重试', 'error')
      return
    }
    try {
      await createSkill({
        name,
        description: parsed.description || skillGenerateForm.description.trim(),
        body: parsed.body.trim() ? parsed.body : skillDraft.content,
      })
      push('草案已保存为技能', 'success')
      setSkillModal(null)
      await loadSkills()
    } catch (error) {
      notifyError(error)
    }
  }

  const toggleSkill = async (skill: SkillItem) => {
    try {
      await setSkillEnabled(skill.name, !skill.enabled)
      push(skill.enabled ? '已停用技能' : '已启用技能', 'success')
      await loadSkills()
    } catch (error) {
      notifyError(error)
    }
  }

  /** 必注入技能清单（settings.writing.always_inject_skills）：只允许勾选当前存在的技能。 */
  const toggleAlwaysInject = (name: string, checked: boolean) => {
    const current = settings?.writing?.always_inject_skills ?? []
    if (checked && !skills.some((skill) => skill.name === name)) {
      push(`技能「${name}」不存在，请先新建或同步后再加入必注入清单`, 'error')
      return
    }
    const next = checked
      ? Array.from(new Set([...current, name]))
      : current.filter((item) => item !== name)
    saveSettings({ writing: { always_inject_skills: next } })
  }

  const removeSkill = async (name: string) => {
    const confirmed = await confirm({
      title: '删除技能',
      message: `删除技能「${name}」？内置技能会被后端拒绝并提示复制指引。`,
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    try {
      await deleteSkill(name)
      push('已删除技能', 'success')
      await loadSkills()
    } catch (error) {
      notifyError(error)
    }
  }

  const runSyncSkills = async () => {
    try {
      const result = (await syncSkills()) as unknown as SkillSyncResult
      setSyncResult(result)
      push(`已同步 ${result.count ?? 0} 个技能`, 'success')
      await loadSkills()
    } catch (error) {
      notifyError(error)
    }
  }


  // ─────────────────────────── Agent 动作 ───────────────────────────

  const openAgentCreate = () => {
    setAgentForm(EMPTY_AGENT_FORM)
    setModelCustom(false)
    setAgentModal({ mode: 'form' })
  }

  const openAgentEdit = (agent: AgentRow) => {
    setAgentForm({
      name: agent.name,
      title: agent.title,
      description: agent.description,
      system_prompt: agent.system_prompt,
      skills: agent.skills,
      tools: agent.tools,
      materials: agent.materials ?? [],
      boundaries: agent.boundaries ?? '',
      capabilities: agent.capabilities ?? [],
      provider_id: agent.provider_id,
      model_id: agent.model_id,
    })
    setModelCustom(false)
    setAgentModal({ mode: 'form', name: agent.name })
  }

  const saveAgentForm = async () => {
    const editing = agentModal?.mode === 'form' && agentModal.name ? agentModal.name : ''
    try {
      if (editing) {
        await updateAgent(editing, {
          title: agentForm.title,
          description: agentForm.description,
          system_prompt: agentForm.system_prompt,
          skills: agentForm.skills,
          tools: agentForm.tools,
          materials: agentForm.materials,
          boundaries: agentForm.boundaries,
          capabilities: agentForm.capabilities,
          provider_id: agentForm.provider_id,
          model_id: agentForm.model_id,
        })
        push('Agent 已更新', 'success')
      } else {
        await createAgent({
          name: agentForm.name.trim(),
          title: agentForm.title,
          description: agentForm.description,
          system_prompt: agentForm.system_prompt,
          skills: agentForm.skills,
          tools: agentForm.tools,
          materials: agentForm.materials,
          boundaries: agentForm.boundaries,
          capabilities: agentForm.capabilities,
          provider_id: agentForm.provider_id,
          model_id: agentForm.model_id,
          params: { temperature: 0.7 },
        })
        push('Agent 已创建', 'success')
      }
      setAgentModal(null)
      await loadAgents()
    } catch (error) {
      notifyError(error)
    }
  }

  const runDuplicateAgent = async (name: string) => {
    if (!duplicateName.trim()) {
      push('请填写副本名称', 'error')
      return
    }
    try {
      await duplicateAgent(name, duplicateName.trim())
      push('已复制 Agent', 'success')
      setAgentInline(null)
      await loadAgents()
    } catch (error) {
      notifyError(error)
    }
  }

  const runSetAgentModel = async (name: string) => {
    try {
      await setAgentModel(name, modelBinding.provider_id, modelBinding.model_id)
      push('模型绑定已保存', 'success')
      setAgentInline(null)
      await loadAgents()
    } catch (error) {
      notifyError(error)
    }
  }

  const runAgentDraft = async () => {
    if (!agentDraftForm.description.trim()) {
      push('请先描述 Agent 职责', 'error')
      return
    }
    try {
      const result = await draftAgent(agentDraftForm.description, agentDraftForm.name)
      if (!result.draft) {
        push(`草案生成失败：${result.error || '未知原因'}`, 'error')
        return
      }
      const draft = result.draft as unknown as Record<string, unknown>
      setAgentDraft({
        name: String(draft.name ?? ''),
        title: String(draft.title ?? ''),
        description: String(draft.description ?? ''),
        system_prompt: String(draft.system_prompt ?? ''),
        skills: Array.isArray(draft.skills) ? draft.skills.map((item) => String(item)) : [],
        tools: Array.isArray(draft.tools) ? draft.tools.map((item) => String(item)) : [],
        materials: Array.isArray(draft.materials) ? draft.materials.map((item) => String(item)) : [],
        boundaries: String(draft.boundaries ?? ''),
        capabilities: Array.isArray(draft.capabilities)
          ? (draft.capabilities as AgentCapability[]).filter(
              (item) => item && typeof item === 'object',
            )
          : [],
        provider_id: '',
        model_id: '',
      })
      setAgentDraftParams(JSON.stringify(draft.params ?? { temperature: 0.5 }, null, 2))
    } catch (error) {
      notifyError(error)
    }
  }

  const saveAgentDraft = async () => {
    if (!agentDraft) return
    let params: Record<string, unknown> = { temperature: 0.5 }
    try {
      const parsed = JSON.parse(agentDraftParams) as Record<string, unknown>
      if (parsed && typeof parsed === 'object') params = parsed
    } catch {
      push('params 不是合法 JSON，已按默认参数保存', 'info')
    }
    try {
      await createAgent({
        name: agentDraft.name.trim(),
        title: agentDraft.title,
        description: agentDraft.description,
        system_prompt: agentDraft.system_prompt,
        skills: agentDraft.skills,
        tools: agentDraft.tools,
        materials: agentDraft.materials,
        boundaries: agentDraft.boundaries,
        capabilities: agentDraft.capabilities,
        provider_id: agentDraft.provider_id,
        model_id: agentDraft.model_id,
        params,
      })
      push('草案已保存为 Agent', 'success')
      setAgentModal(null)
      setAgentDraft(null)
      await loadAgents()
    } catch (error) {
      notifyError(error)
    }
  }

  const toggleAgent = async (agent: AgentItem) => {
    try {
      await updateAgent(agent.name, { enabled: !agent.enabled })
      push(agent.enabled ? '已停用 Agent' : '已启用 Agent', 'success')
      await loadAgents()
    } catch (error) {
      notifyError(error)
    }
  }

  const removeAgent = async (name: string) => {
    const confirmed = await confirm({
      title: '删除 Agent',
      message: `删除 Agent「${name}」？`,
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    try {
      await deleteAgent(name)
      push('已删除 Agent', 'success')
      await loadAgents()
    } catch (error) {
      notifyError(error)
    }
  }

  // ─────────────────────────── 规则动作 ───────────────────────────

  const currentRuleProjectId = () => {
    const raw = ruleProjectIdRef.current.trim()
    if (!raw) return undefined
    const parsed = Number(raw)
    return Number.isFinite(parsed) ? parsed : undefined
  }

  const submitRule = async () => {
    if (!ruleForm.name.trim() || !ruleForm.body.trim()) {
      push('规则名与正文都不能为空', 'error')
      return
    }
    try {
      await upsertRule(
        {
          name: ruleForm.name.trim(),
          body: ruleForm.body,
          keys: splitList(ruleForm.keys),
          priority: Number(ruleForm.priority) || 100,
          confirmed: ruleForm.confirmed,
          enabled: ruleForm.enabled,
        },
        currentRuleProjectId(),
      )
      push('规则已保存', 'success')
      setRuleForm({ name: '', body: '', keys: '', priority: 100, confirmed: false, enabled: true })
      await loadRules()
    } catch (error) {
      notifyError(error)
    }
  }

  const removeRule = async (rule: RuleItem) => {
    const confirmed = await confirm({
      title: '删除规则',
      message: `删除规则「${rule.name}」？`,
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    try {
      await deleteRule(rule.name, rule.scope, currentRuleProjectId())
      push('规则已删除', 'success')
      await loadRules()
    } catch (error) {
      notifyError(error)
    }
  }

  const runCompileRules = async () => {
    try {
      const result = (await compileRules(currentRuleProjectId())) as unknown as CompileResult
      setCompileResult(result)
      push('规则已编译', 'success')
    } catch (error) {
      notifyError(error)
    }
  }

  // ─────────────────────────── 向量动作 ───────────────────────────

  const saveVectorModel = async () => {
    try {
      const result = (await setVectorModel({
        model_dir: vectorForm.model_dir,
        provider: vectorForm.provider,
        model: vectorForm.model,
      })) as unknown as VectorSetResult
      setVectorNote(result.note ?? '')
      push('嵌入器配置已保存', 'success')
      await loadVector()
    } catch (error) {
      notifyError(error)
    }
  }

  // ─────────────────────────── 供应商分区 ───────────────────────────

  /** 新建 / 编辑供应商弹窗：表单与列表分离，点「编辑」立即在视口内出现。 */
  function renderProviderModal() {
    if (!providerModal) return null
    const form = providerForm
    const availableModels = modelOptions.filter((id) => !form.models.some((model) => model.id === id))
    const renderTokenField = (
      index: number,
      key: 'context_window' | 'max_tokens',
      label: string,
      presets: Array<{ label: string; value: number }>,
    ) => {
      const inputId = `provider-model-${index}-${key}`
      return (
        <div className="field">
          <label className="field__label" htmlFor={inputId}>{label} <span className="provider-editor__unit">Token</span></label>
          <input
            id={inputId}
            className="input"
            type="number"
            min={0}
            value={form.models[index][key] || ''}
            onChange={(event) => updateModelRow(index, { [key]: Number(event.target.value) || 0 })}
            placeholder="使用默认值"
          />
          <div className="provider-editor__presets" role="group" aria-label={`模型 ${index + 1} ${label}预设`}>
            {presets.map((preset) => {
              const on = form.models[index][key] === preset.value
              return (
                <button key={preset.label} type="button" className="provider-editor__preset" aria-pressed={on}
                  onClick={() => updateModelRow(index, { [key]: on ? 0 : preset.value })}>
                  {preset.label}
                </button>
              )
            })}
          </div>
        </div>
      )
    }
    return (
      <Modal
        title={providerModal.mode === 'edit' ? `编辑供应商：${providerModal.providerId}` : '新建供应商'}
        open onClose={closeProviderModal} width={1040} className="provider-editor-modal"
        footer={
          <div className="provider-editor__footer">
            <span className="field__hint">{form.models.length} 个模型 · 凭据仅保存在本机</span>
            <div className="btn-row">
              {editingProviderId ? <button className="btn" type="button" disabled={providerSaving} onClick={() => void runHealth(editingProviderId)}>健康检查</button> : null}
              <button className="btn" type="button" disabled={providerSaving} onClick={closeProviderModal}>取消</button>
              <button className="btn btn--primary" type="button" disabled={providerSaving} onClick={() => void submitProvider()}>
                {providerSaving ? '保存中…' : '保存供应商'}
              </button>
            </div>
          </div>
        }
      >
        <fieldset className="provider-editor" disabled={providerSaving}>
          <section className="provider-editor__section" aria-labelledby="provider-connection-title">
            <div className="provider-editor__section-header">
              <div><h3 id="provider-connection-title">连接配置</h3><p>填写服务地址与 API Key，连接你的模型服务。</p></div>
              <span className="provider-editor__local-tag">本机安全存储</span>
            </div>
            <div className="provider-editor__connection-grid">
              <label className="field">
                <span className="field__label">供应商标识 <span className="provider-editor__unit">ID</span></span>
                <input className="input mono" name="provider-id" autoComplete={NO_AUTOFILL} value={form.provider_id}
                  disabled={Boolean(editingProviderId)} onChange={(event) => updateProviderForm({ provider_id: event.target.value })} placeholder="例如 deepseek" />
              </label>
              <label className="field">
                <span className="field__label">显示名称</span>
                <input className="input" name="provider-name" autoComplete={NO_AUTOFILL} value={form.display_name}
                  onChange={(event) => updateProviderForm({ display_name: event.target.value })} placeholder="例如 我的模型服务" />
              </label>
              <label className="field">
                <span className="field__label">请求超时 <span className="provider-editor__unit">秒</span></span>
                <input className="input" type="number" min={5} value={form.timeout_seconds}
                  onChange={(event) => updateProviderForm({ timeout_seconds: Number(event.target.value) || 60 })} />
              </label>
            </div>
            <div className="provider-editor__endpoint-grid">
              <label className="field">
                <span className="field__label">服务地址 <span className="provider-editor__unit">Base URL</span></span>
                <input className="input mono" name="provider-base-url" autoComplete={NO_AUTOFILL} {...CREDENTIAL_FIELD_EXTRA} value={form.base_url}
                  onChange={(event) => updateProviderForm({ base_url: event.target.value })} placeholder="https://api.example.com/v1" />
              </label>
              <div className="field">
                <label className="field__label" htmlFor="provider-credential">API Key</label>
                <CredentialInput id="provider-credential" name="provider-api-key" value={form.api_key}
                  onChange={(event) => updateProviderForm({ api_key: event.target.value })}
                  placeholder={editingProviderId ? '已存凭据留空保留，填入新 Key 可替换' : '粘贴 API Key'} />
              </div>
            </div>
          </section>
          <section className="provider-editor__section" aria-labelledby="provider-models-title">
            <div className="provider-editor__section-header">
              <div><h3 id="provider-models-title">模型配置 <span className="provider-editor__count">{form.models.length}</span></h3><p>可配置多个模型；额度留空时使用服务默认值。</p></div>
              <div className="btn-row">
                <button className="btn btn--sm" type="button" disabled={providerPulling} onClick={() => void pullModels()}>{providerPulling ? '拉取中…' : '拉取模型列表'}</button>
                <button className="btn btn--sm btn--primary" type="button" onClick={addModelRow}>＋ 添加模型</button>
              </div>
            </div>
            {form.models.length ? <div className="provider-editor__models">
              {form.models.map((model, index) => (
                <article className="provider-model-card" key={`model-${index}`} aria-label={`模型 ${index + 1}`}>
                  <div className="provider-model-card__header">
                    <span className="provider-model-card__number">{String(index + 1).padStart(2, '0')}</span>
                    <strong title={model.id || undefined}>{model.id || '新模型'}</strong>
                    <button className="btn btn--ghost btn--sm provider-model-card__remove" type="button" aria-label={`删除模型 ${index + 1}`} onClick={() => removeModelRow(index)}>删除</button>
                  </div>
                  <div className="provider-model-card__grid">
                    <label className="field">
                      <span className="field__label">模型 ID</span>
                      <input className="input mono" autoComplete={NO_AUTOFILL} value={model.id} onChange={(event) => updateModelRow(index, { id: event.target.value })} placeholder="例如 deepseek-chat" />
                    </label>
                    <label className="field">
                      <span className="field__label">显示名称 <span className="provider-editor__unit">可选</span></span>
                      <input className="input" autoComplete={NO_AUTOFILL} value={model.name} onChange={(event) => updateModelRow(index, { name: event.target.value })} placeholder="默认显示模型 ID" />
                    </label>
                    {renderTokenField(index, 'context_window', '上下文窗口', CONTEXT_WINDOW_PRESETS)}
                    {renderTokenField(index, 'max_tokens', '最大输出', MAX_TOKENS_PRESETS)}
                  </div>
                </article>
              ))}
            </div> : <div className="provider-editor__empty"><strong>添加你的第一个模型</strong><p>手动填写模型 ID，或从供应商拉取后批量添加。</p><button className="btn btn--sm" type="button" onClick={addModelRow}>添加模型</button></div>}
            {modelOptions.length ? <div className="provider-editor__pulled">
              <div className="provider-editor__section-header">
                <div><h3>发现 {modelOptions.length} 个模型</h3><p>勾选需要的模型，添加到上方配置。</p></div>
                <div className="btn-row">
                  <button className="btn btn--sm" type="button" disabled={!availableModels.length} onClick={() => setPulledSelection(pulledSelection.length === availableModels.length ? [] : availableModels)}>{availableModels.length && pulledSelection.length === availableModels.length ? '取消全选' : '全选'}</button>
                  <button className="btn btn--sm btn--primary" type="button" disabled={!pulledSelection.length} onClick={appendPulledModels}>添加所选模型</button>
                </div>
              </div>
              <div className="provider-editor__pulled-options">
                {modelOptions.map((id) => {
                  const added = form.models.some((model) => model.id === id)
                  const on = added || pulledSelection.includes(id)
                  return <label className="checkbox" key={id}><input type="checkbox" checked={on} disabled={added} onChange={() => setPulledSelection(pulledSelection.includes(id) ? pulledSelection.filter((item) => item !== id) : [...pulledSelection, id])} />{id}{added ? <span className="field__hint">已添加</span> : null}</label>
                })}
              </div>
            </div> : null}
          </section>
          <div className="provider-editor__options">
            <details>
              <summary>能力标签 <span className="provider-editor__unit">{form.capabilities.length ? `已选 ${form.capabilities.length} 项` : '可选'}</span></summary>
              <div className="chips">
                {capabilityTags.map((tag) => {
                  const on = form.capabilities.includes(tag)
                  return <button key={tag} type="button" className={on ? 'tag tag--primary' : 'tag'} aria-pressed={on}
                    onClick={() => updateProviderForm({ capabilities: on ? form.capabilities.filter((item) => item !== tag) : [...form.capabilities, tag] })}>{tag}</button>
                })}
              </div>
            </details>
            <label className="checkbox"><input type="checkbox" checked={form.enabled} onChange={(event) => updateProviderForm({ enabled: event.target.checked })} />保存后启用供应商</label>
          </div>
        </fieldset>
      </Modal>
    )
  }

  function renderProviders() {
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">全局默认模型</h3>
            {defaultTarget.provider_id ? (
              <span className="tag tag--ok">
                当前：{defaultTarget.provider_id} / {defaultTarget.model_id || '—'}
              </span>
            ) : (
              <span className="tag tag--warn">未设置</span>
            )}
          </div>
          <div className="panel__body stack">
            <p className="field__hint">
              未显式指定模型时，所有任务与对话都使用此模型；未设置则取第一个启用的供应商的首个模型。
            </p>
            <div className="row">
              <label className="field" style={{ flex: '1 1 14rem' }}>
                <span className="field__label">供应商</span>
                <select
                  className="select"
                  value={defaultDraft.provider_id}
                  onChange={(event) => {
                    const providerId = event.target.value
                    const found = providers.find((item) => item.provider_id === providerId)
                    setDefaultDraft({
                      provider_id: providerId,
                      model_id: found?.models[0]?.id ?? '',
                    })
                  }}
                >
                  <option value="">（未设置）</option>
                  {providers
                    .filter((provider) => provider.enabled)
                    .map((provider) => (
                      <option key={provider.provider_id} value={provider.provider_id}>
                        {provider.display_name || provider.provider_id}
                      </option>
                    ))}
                </select>
              </label>
              <label className="field" style={{ flex: '1 1 14rem' }}>
                <span className="field__label">模型</span>
                <select
                  className="select"
                  value={defaultDraft.model_id}
                  disabled={!defaultDraft.provider_id}
                  onChange={(event) =>
                    setDefaultDraft({ ...defaultDraft, model_id: event.target.value })
                  }
                >
                  <option value="">（首个模型）</option>
                  {(
                    providers.find((item) => item.provider_id === defaultDraft.provider_id)?.models ??
                    []
                  ).map((model) => (
                    <option key={model.id} value={model.id}>
                      {modelLabel(model)}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="btn btn--primary"
                type="button"
                onClick={() => void saveDefaultTarget(defaultDraft)}
              >
                保存默认模型
              </button>
              <button
                className="btn"
                type="button"
                onClick={() => void saveDefaultTarget({ provider_id: '', model_id: '' })}
              >
                清除
              </button>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">供应商列表</h3>
            <div className="btn-row">
              <span className={cipher === 'dpapi' ? 'tag tag--ok' : 'tag tag--warn'}>
                凭据存储：{cipher || '未知'}
              </span>
              <button className="btn btn--sm btn--primary" type="button" onClick={startCreateProvider}>
                新增供应商
              </button>
              <button className="btn btn--sm" type="button" onClick={() => void loadProviders()}>
                刷新
              </button>
            </div>
          </div>
          <div className="panel__body stack stack--tight">
            {providers.length === 0 ? (
              <p className="muted">尚未配置供应商。点右上角「新增供应商」开始配置。</p>
            ) : (
              providers.map((provider) => {
                const result = healthResults[provider.provider_id]
                const isDefault = defaultTarget.provider_id === provider.provider_id
                return (
                  <div className="panel provider-row stack stack--tight" key={provider.provider_id}>
                    <div className="provider-row__main">
                      <strong>{provider.display_name || provider.provider_id}</strong>
                      <span className="mono muted">{provider.provider_id}</span>
                      {isDefault ? <span className="tag tag--ok">默认</span> : null}
                      <span className={provider.enabled ? 'tag tag--ok' : 'tag'}>
                        {provider.enabled ? '已启用' : '已停用'}
                      </span>
                      <span
                        className={
                          provider.health === 'ok'
                            ? 'tag tag--ok'
                            : 'tag tag--warn'
                        }
                        title={provider.health === 'ok' ? undefined : '尚未通过健康检查，建议先做健康检查再启用'}
                      >
                        {provider.health || '未检查'}
                      </span>
                      {result ? (
                        <span className={result.ok ? 'tag tag--ok' : 'tag tag--danger'}>
                          {result.ok
                            ? `检查通过（${result.model_count ?? 0} 个模型）`
                            : result.reason || '未通过'}
                        </span>
                      ) : null}
                      {provider.has_secret ? (
                        <span className="tag tag--ok">{provider.masked_secret || '已配置 Key'}</span>
                      ) : (
                        <span className="tag tag--warn">未配置 Key</span>
                      )}
                    </div>
                    <div className="mono muted provider-row__url">
                      {provider.base_url || '（未填 base_url）'}
                    </div>
                    <div className="provider-row__meta">
                      <span className="chip">
                        {provider.models.length ? `模型 ${provider.models.length} 个` : '未配置模型'}
                      </span>
                      {isDefault && defaultTarget.model_id ? (
                        <span className="chip">默认模型：{defaultTarget.model_id}</span>
                      ) : null}
                      {provider.capabilities.length ? (
                        provider.capabilities.map((item) => (
                          <span className="chip" key={item}>
                            {item}
                          </span>
                        ))
                      ) : (
                        <span className="muted">无能力标签</span>
                      )}
                    </div>
                    <div className="btn-row">
                      <button
                        className="btn btn--sm btn--primary"
                        type="button"
                        onClick={() => startEditProvider(provider)}
                      >
                        编辑
                      </button>
                      <button
                        className="btn btn--sm"
                        type="button"
                        onClick={() => void runHealth(provider.provider_id)}
                      >
                        健康检查
                      </button>
                      <button
                        className="btn btn--sm"
                        type="button"
                        aria-pressed={provider.enabled}
                        onClick={() => void toggleProvider(provider)}
                      >
                        {provider.enabled ? '停用' : '启用'}
                      </button>
                      <button
                        className="btn btn--sm btn--danger"
                        type="button"
                        onClick={() => void removeProvider(provider.provider_id)}
                      >
                        删除
                      </button>
                    </div>
                  </div>
                )
              })
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">投影到内置 dsh</h3>
            <button className="btn btn--sm btn--primary" type="button" onClick={() => void projectToDsh()}>
              投影到内置 dsh
            </button>
          </div>
          <div className="panel__body stack">
            <p className="field__hint">把启用中的供应商同步到工作台内置引擎。</p>
            {providerNote ? (
              <div className="mono" style={{ whiteSpace: 'pre-wrap' }}>
                {providerNote}
              </div>
            ) : null}
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">从 settings.yaml 文本导入</h3>
          </div>
          <div className="panel__body stack">
            <p className="field__hint">只导入非敏感配置，Key 需在本页重新录入。</p>
            <textarea
              autoComplete={NO_AUTOFILL}
              className="textarea textarea--mono"
              value={yamlText}
              onChange={(event) => setYamlText(event.target.value)}
              placeholder={'llm-pi-ai:\n  providers:\n    - id: deepseek\n      base_url: https://api.deepseek.com/v1'}
            />
            <div className="btn-row">
              <button className="btn btn--primary" type="button" onClick={() => void importYaml()}>
                导入文本
              </button>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">迁移 模型API.md 中的 Key</h3>
            <button className="btn btn--sm" type="button" onClick={() => void importMdKeys()}>
              迁移 Key
            </button>
          </div>
          <div className="panel__body">
            <p className="field__hint">
              把 <span className="mono">模型API.md</span> 里的 Key 迁入本机安全存储。
            </p>
          </div>
        </div>
      </div>
    )
  }

  // ─────────────────────────── 引擎分区 ───────────────────────────

  function renderEngines() {
    if (!routing) return <p className="muted">引擎信息加载中…</p>
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">全局默认引擎</h3>
            <span className="muted">兜底引擎：{routing.fallback_engine || '—'}</span>
          </div>
          <div className="panel__body">
            <label className="field" style={{ maxWidth: '22rem' }}>
              <span className="field__label">未命中决策表与覆盖时使用</span>
              <select
                className="select"
                value={routing.global_default}
                onChange={(event) => void saveEngines({ default: event.target.value })}
              >
                {ENGINE_OPTIONS.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">决策表与任务级覆盖</h3>
            <button className="btn btn--sm" type="button" onClick={() => void loadEngines()}>
              刷新
            </button>
          </div>
          <div className="panel__body panel__body--tight">
            <table className="table">
              <thead>
                <tr>
                  <th>任务类型</th>
                  <th>决策表引擎</th>
                  <th>全局覆盖</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(routing.decision_table).map(([taskType, engine]) => (
                  <tr key={taskType}>
                    <td>{taskType}</td>
                    <td className="mono">{engine}</td>
                    <td>
                      <select
                        className="select"
                        value={overrideRows[taskType] ?? ''}
                        onChange={(event) => setOverride(taskType, event.target.value)}
                      >
                        <option value="">跟随决策表</option>
                        {ENGINE_OPTIONS.map((item) => (
                          <option key={item.value} value={item.value}>
                            {item.value}
                          </option>
                        ))}
                      </select>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {extraOverrides.length ? (
              <div className="stack stack--tight" style={{ marginTop: 'var(--space-3)' }}>
                <span className="field__label">其它覆盖（不在决策表中）</span>
                {extraOverrides.map((taskType) => (
                  <div className="row" key={taskType}>
                    <span style={{ minWidth: '10rem' }}>{taskType}</span>
                    <span className="mono">{overrideRows[taskType]}</span>
                    <button
                      className="btn btn--sm btn--danger"
                      type="button"
                      style={{ marginLeft: 'auto' }}
                      onClick={() => setOverride(taskType, '')}
                    >
                      移除
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
            <div className="row" style={{ marginTop: 'var(--space-3)' }}>
              <label className="field" style={{ flex: '1 1 12rem' }}>
                <span className="field__label">新增覆盖：任务类型</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={newOverrideKey}
                  onChange={(event) => setNewOverrideKey(event.target.value)}
                  placeholder="例如 正文生成"
                />
              </label>
              <label className="field" style={{ flex: '0 1 14rem' }}>
                <span className="field__label">引擎</span>
                <select
                  className="select"
                  value={newOverrideEngine}
                  onChange={(event) => setNewOverrideEngine(event.target.value)}
                >
                  {ENGINE_OPTIONS.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
              <button className="btn" type="button" onClick={addOverride}>
                添加覆盖
              </button>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">引擎可用性</h3>
          </div>
          <div className="panel__body panel__body--tight">
            <table className="table">
              <thead>
                <tr>
                  <th>引擎</th>
                  <th>可用</th>
                  <th>配置档 profile</th>
                  <th>dsh-home</th>
                  <th>供应商 / 模型</th>
                  <th>说明</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(routing.engines).map(([name, info]) => (
                  <tr key={name}>
                    <td className="mono">{info.engine || name}</td>
                    <td>
                      <span className={info.available ? 'tag tag--ok' : 'tag tag--danger'}>
                        {info.available ? '可用' : '不可用'}
                      </span>
                    </td>
                    <td className="mono">{info.profile || '—'}</td>
                    <td className="mono">{info.dsh_home || '—'}</td>
                    <td className="mono">
                      {info.provider || info.default_provider || '—'} / {info.model || info.default_model || '—'}
                    </td>
                    <td>
                      {info.error ? (
                        <span className="tag tag--warn">{info.error}</span>
                      ) : (
                        <span className="muted">正常</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    )
  }

  // ─────────────────────────── 外观分区 ───────────────────────────

  function renderAppearance() {
    const draft = appearanceDraft
    if (!draft) return <p className="muted">设置加载中…</p>
    const numbers: Array<{
      key: 'fontSize' | 'lineHeight' | 'letterSpacing' | 'pageWidth' | 'colorTemperature' | 'contrast'
      label: string
      min: number
      max: number
      step: number
      suffix: string
    }> = [
      { key: 'fontSize', label: '正文字号', min: 14, max: 24, step: 1, suffix: 'px' },
      { key: 'lineHeight', label: '行高', min: 1.4, max: 2.4, step: 0.05, suffix: '' },
      { key: 'letterSpacing', label: '字距', min: -1, max: 3, step: 0.25, suffix: 'px' },
      { key: 'pageWidth', label: '页面宽度', min: 520, max: 1000, step: 10, suffix: 'px' },
      { key: 'colorTemperature', label: '色温（负冷 / 正暖）', min: -50, max: 50, step: 1, suffix: '' },
      { key: 'contrast', label: '对比度', min: 0.8, max: 1.3, step: 0.05, suffix: '' },
    ]
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">外观与护眼</h3>
            <button
              className="btn btn--sm"
              type="button"
              onClick={() => saveAppearanceNow(DEFAULT_APPEARANCE)}
            >
              恢复默认
            </button>
          </div>
          <div className="panel__body stack">
            <div className="row">
              <span className="field__label">主题</span>
              <div className="btn-row">
                {THEMES.map((item: Theme) => (
                  <button
                    key={item}
                    type="button"
                    className={draft.theme === item ? 'btn btn--sm btn--primary' : 'btn btn--sm'}
                    aria-pressed={draft.theme === item}
                    onClick={() => void setTheme(item)}
                  >
                    {item === 'light' ? '亮色' : item === 'dark' ? '暗色' : '纸感'}
                  </button>
                ))}
              </div>
            </div>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={draft.followSystem}
                onChange={(event) =>
                  saveAppearanceNow({ ...draft, followSystem: event.target.checked })
                }
              />
              跟随系统亮暗（勾选后忽略上方主题选择）
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={draft.reduceMotion}
                onChange={(event) => saveAppearanceNow({ ...draft, reduceMotion: event.target.checked })}
              />
              减少动效
            </label>
            {numbers.map((item) => (
              <div className="row" key={item.key}>
                <label className="field" style={{ flex: '1 1 18rem' }}>
                  <span className="field__label">
                    {item.label}：{draft[item.key]}
                    {item.suffix}
                  </span>
                  <input
                    type="range"
                    min={item.min}
                    max={item.max}
                    step={item.step}
                    value={draft[item.key]}
                    onChange={(event) =>
                      saveAppearanceDebounced({ ...draft, [item.key]: Number(event.target.value) })
                    }
                  />
                </label>
                <input
                  className="input"
                  type="number"
                  min={item.min}
                  max={item.max}
                  step={item.step}
                  value={draft[item.key]}
                  style={{ width: '6.5rem' }}
                  onChange={(event) =>
                    saveAppearanceDebounced({ ...draft, [item.key]: Number(event.target.value) })
                  }
                />
              </div>
            ))}
            <p className="field__hint">改动会自动保存。</p>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">实时预览</h3>
          </div>
          <div className="panel__body">
            <div className="prose-reading">
              <p>
                雨落在青石板上，声音细碎。他把伞往廊下收了半分，等巷口那个人先走过去。灯从窗纸里透出来，
                一条一条铺在水面上，走一步，碎一步。
              </p>
              <p>
                「你迟了。」她说。他没答话，只把怀里那卷纸递过去。纸角被雨水洇软了，她接过来，
                指尖在封口上停了一瞬。
              </p>
            </div>
          </div>
        </div>
      </div>
    )
  }

  // ─────────────────────────── 编辑器分区 ───────────────────────────

  function renderChat() {
    if (!settings) return <p className="muted">设置加载中…</p>
    const chat = (settings.chat || { permission_mode: 'auto', discussion_only: false }) as ChatPrefs
    return <div className="panel"><div className="panel__header"><h3 className="panel__title">新对话默认设置</h3></div><div className="panel__body stack">
      <p className="muted">应用于以后创建的对话。已有对话保留自己的设置，也可以在对话面板中单独修改。</p>
      <fieldset className="chat__default-permissions"><legend>文件操作权限</legend>{CHAT_PERMISSION_OPTIONS.map((option) => <label className={`chat__option${chat.permission_mode === option.value ? ' is-selected' : ''}`} key={option.value}><input type="radio" name="default-chat-permission" checked={chat.permission_mode === option.value} onChange={() => saveSettings({ chat: { permission_mode: option.value } })} /><span><strong>{option.label}</strong><small>{option.description}</small></span></label>)}</fieldset>
      <label className="checkbox"><input type="checkbox" checked={chat.discussion_only} onChange={(event) => saveSettings({ chat: { discussion_only: event.target.checked } })} />默认仅讨论，不修改文件</label>
      <p className="muted">仅讨论开关优先于文件操作权限；关闭后继续使用所选权限。完全访问的范围始终限定在本书项目内。</p>
      <label className="field" style={{ maxWidth: '26rem' }}>
        <span className="field__label">意图判定（LLM）</span>
        <select className="select" value={chat.intent_llm === false ? 'off' : 'on'} onChange={(event) => saveSettings({ chat: { intent_llm: event.target.value === 'on' } })}>
          <option value="on">开启</option>
          <option value="off">关闭</option>
        </select>
        <span className="field__hint">关闭后只用关键词快路径，不再调用模型做意图判定（更省钱、更快，复杂请求的归属可能不准）。</span>
      </label>
      <label className="field" style={{ maxWidth: '26rem' }}>
        <span className="field__label">写域严格度</span>
        <select className="select" value={chat.scope_strictness === 'reject' ? 'reject' : 'ask'} onChange={(event) => saveSettings({ chat: { scope_strictness: event.target.value === 'reject' ? 'reject' : 'ask' } })}>
          <option value="ask">范围外需批准</option>
          <option value="reject">范围外一律拒绝</option>
        </select>
        <span className="field__hint">本轮任务有明确产出材料时，改其他材料属于越界：默认需你在对话里批准；选「一律拒绝」则直接拒绝。</span>
      </label>
      <label className="checkbox"><input type="checkbox" checked={chat.scope_limit_full === true} onChange={(event) => saveSettings({ chat: { scope_limit_full: event.target.checked } })} />完全访问也受写域限制</label>
      <p className="muted">默认关闭：「完全访问」档放行越界写入但会留痕；开启后完全访问档的越界写也要先批准。</p>
    </div></div>
  }

  function renderEditor() {
    if (!settings) return <p className="muted">设置加载中…</p>
    const milestone = settings.milestone
    const editor = settings.editor
    const writing = settings.writing
    const wordMin = writing?.chapter_min_words ?? 2000
    const wordMax = writing?.chapter_max_words ?? 4000
    const autoDeslop = writing?.auto_deslop ?? { enabled: true, max_rounds: 2 }
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">编辑器偏好</h3>
          </div>
          <div className="panel__body stack">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={milestone.enabled}
              onChange={(event) => saveSettings({ milestone: { enabled: event.target.checked } })}
            />
            启用字数里程碑
          </label>
          <label className="field" style={{ maxWidth: '20rem' }}>
            <span className="field__label">字数里程碑阈值（字）</span>
            <input
              className="input"
              type="number"
              min={100}
              step={100}
              value={milestone.step}
              onChange={(event) =>
                saveSettings({ milestone: { step: Number(event.target.value) || 500 } })
              }
            />
            <span className="field__hint">默认 500；每满该字数在正文中插入里程碑标记。</span>
          </label>
          <label className="field" style={{ maxWidth: '20rem' }}>
            <span className="field__label">自动保存间隔（秒）</span>
            <input
              className="input"
              type="number"
              min={0}
              value={editor.autosave_seconds}
              onChange={(event) =>
                saveSettings({ editor: { autosave_seconds: Number(event.target.value) || 0 } })
              }
            />
            <span className="field__hint">0 表示关闭自动保存，仅手动保存。</span>
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={editor.ghost_text}
              onChange={(event) => saveSettings({ editor: { ghost_text: event.target.checked } })}
            />
            启用续写提示（ghost text）
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={editor.milestone_inline}
              onChange={(event) =>
                saveSettings({ editor: { milestone_inline: event.target.checked } })
              }
            />
            在正文中内联显示里程碑标记
          </label>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">每章字数区间与去 AI 味</h3>
          </div>
          <div className="panel__body stack">
            <div className="row">
              <label className="field" style={{ flex: '0 1 10rem' }}>
                <span className="field__label">每章下限（汉字）</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={50000}
                  value={wordMin}
                  onChange={(event) =>
                    saveSettings({ writing: { chapter_min_words: Number(event.target.value) || 0 } })
                  }
                />
              </label>
              <label className="field" style={{ flex: '0 1 10rem' }}>
                <span className="field__label">每章上限（汉字）</span>
                <input
                  className="input"
                  type="number"
                  min={1}
                  max={50000}
                  value={wordMax}
                  onChange={(event) =>
                    saveSettings({ writing: { chapter_max_words: Number(event.target.value) || 0 } })
                  }
                />
              </label>
            </div>
            <span className="field__hint">
              全局默认区间（默认 2000-4000）。低于下限会被字数门拒收，高于上限只告警；单本书可在「项目设置」中覆盖。
            </span>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={autoDeslop.enabled}
                onChange={(event) =>
                  saveSettings({ writing: { auto_deslop: { enabled: event.target.checked } } })
                }
              />
              章节落盘前自动去味（未过去 AI 味门禁时按 human-linguistics 重写）
            </label>
            <label className="field" style={{ maxWidth: '20rem' }}>
              <span className="field__label">最多重写轮数（1-3）</span>
              <input
                className="input"
                type="number"
                min={1}
                max={3}
                value={autoDeslop.max_rounds}
                onChange={(event) => {
                  const rounds = Math.max(1, Math.min(3, Number(event.target.value) || 2))
                  saveSettings({ writing: { auto_deslop: { max_rounds: rounds } } })
                }}
              />
              <span className="field__hint">重写仍不过门禁则拒绝落盘并给出定位清单。</span>
            </label>
          </div>
        </div>
      </div>
    )
  }

  // ─────────────────────────── 预算与用量分区 ───────────────────────────

  function renderBudget() {
    if (!settings) return <p className="muted">设置加载中…</p>
    const budget = settings.budget
    const maxTokens = usage ? Math.max(1, ...usage.by_day.map((row) => row.tokens)) : 1
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">预算阈值</h3>
          </div>
          <div className="panel__body stack">
            <div className="row">
              <label className="field" style={{ flex: '1 1 12rem' }}>
                <span className="field__label">每日 Token 上限（0 = 不限制）</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={budget.daily_token_limit}
                  onChange={(event) =>
                    saveSettings({ budget: { daily_token_limit: Number(event.target.value) || 0 } })
                  }
                />
              </label>
              <label className="field" style={{ flex: '0 1 10rem' }}>
                <span className="field__label">告警比例（0~1）</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  max={1}
                  step={0.05}
                  value={budget.alert_ratio}
                  onChange={(event) =>
                    saveSettings({ budget: { alert_ratio: Number(event.target.value) || 0 } })
                  }
                />
              </label>
              <label className="field" style={{ flex: '0 1 12rem' }}>
                <span className="field__label">每千 Token 单价</span>
                <input
                  className="input"
                  type="number"
                  min={0}
                  step={0.001}
                  value={budget.currency_per_1k_tokens}
                  onChange={(event) =>
                    saveSettings({
                      budget: { currency_per_1k_tokens: Number(event.target.value) || 0 },
                    })
                  }
                />
              </label>
            </div>
            <p className="field__hint">
              达到日上限的告警比例（例如 0.8 即用掉 80% 时提醒）后，界面上会出现告警标记；单价用于折算成本。
            </p>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">用量（近 {usage?.days ?? 30} 天）</h3>
            <div className="btn-row">
              {usage?.budget.alert ? <span className="tag tag--warn">已触发告警</span> : <span className="tag tag--ok">未告警</span>}
              <button className="btn btn--sm" type="button" onClick={() => void loadUsage()}>
                刷新
              </button>
            </div>
          </div>
          <div className="panel__body stack">
            {!usage ? (
              <p className="muted">用量加载中…</p>
            ) : (
              <>
                <div className="row">
                  <label className="field" style={{ flex: '0 1 10rem' }}>
                    <span className="field__label">总 Token</span>
                    <span className="mono">{usage.totals.tokens}</span>
                  </label>
                  <label className="field" style={{ flex: '0 1 8rem' }}>
                    <span className="field__label">今日 Token</span>
                    <span className="mono">{usage.totals.today_tokens}</span>
                  </label>
                  <label className="field" style={{ flex: '0 1 8rem' }}>
                    <span className="field__label">调用次数</span>
                    <span className="mono">{usage.totals.calls}</span>
                  </label>
                  <label className="field" style={{ flex: '0 1 10rem' }}>
                    <span className="field__label">成本</span>
                    <span className="mono">{usage.totals.cost}</span>
                  </label>
                </div>
                {usage.by_day.length ? (
                  <div className="bar-chart">
                    {usage.by_day.map((row) => (
                      <div className="bar" key={row.day} title={`${row.day}：${row.tokens} tokens`}>
                        <div className="bar__track">
                          <div
                            className="bar__fill"
                            style={{ height: `${Math.round((row.tokens / maxTokens) * 100)}%` }}
                          />
                        </div>
                        <span className="bar__label">{row.day.slice(5)}</span>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="muted">暂无用量记录。</p>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    )
  }

  // ─────────────────────────── 技能分区 ───────────────────────────

  function renderSkills() {
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">技能同步与用量</h3>
            <div className="btn-row">
              <span className="tag">
                正文合计约 {skillEstimatedTokens.toLocaleString()} token
              </span>
              <button className="btn btn--sm" type="button" onClick={() => void loadSkills()}>
                刷新
              </button>
              <button className="btn btn--sm" type="button" onClick={() => void runSyncSkills()}>
                同步到 .dsh/skills
              </button>
            </div>
          </div>
          <div className="panel__body stack">
            <p className="field__hint">
              命中技能正文完整加载；引用文件按需读取。这里显示正文的 token 估算值，不包含尚未读取的引用文件。
            </p>
            {syncResult ? (
              <div className="mono" style={{ whiteSpace: 'pre-wrap' }}>
                {`已同步 ${syncResult.count ?? 0} 个技能：正文合计约 ${(syncResult.estimated_tokens ?? 0).toLocaleString()} token（估算值）`}
                {syncResult.target_root ? `\n目标目录：${syncResult.target_root}` : ''}
                {(syncResult.warnings ?? []).length
                  ? `\n告警：\n- ${(syncResult.warnings ?? []).join('\n- ')}`
                  : ''}
                {(syncResult.errors ?? []).length
                  ? `\n错误：\n- ${(syncResult.errors ?? []).map((item) => `${item.path}：${item.error}`).join('\n- ')}`
                  : ''}
              </div>
            ) : null}
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">技能列表</h3>
            <div className="btn-row">
              <button className="btn btn--sm btn--primary" type="button" onClick={openSkillCreate}>
                新建技能
              </button>
              <button className="btn btn--sm" type="button" onClick={() => openSkillMode('import')}>
                导入 SKILL.md
              </button>
              <button className="btn btn--sm" type="button" onClick={() => openSkillMode('generate')}>
                AI 生成草案
              </button>
            </div>
          </div>
          <div className="panel__body panel__body--tight">
            {skills.length === 0 ? (
              <p className="muted">尚未发现技能（skills/&lt;name&gt;/SKILL.md）。</p>
            ) : (
              <div className="skill-list">
                {skills.map((skill) => (
                  <div className="skill-card" key={skill.name}>
                    <div className="skill-card__head">
                      <div className="skill-card__title">
                        <span className="skill-card__name mono">{skill.name}</span>
                        <span className="tag">{SOURCE_LABEL[skill.source] ?? skill.source}</span>
                        <span className={skill.synced ? 'tag tag--ok' : 'tag tag--warn'}>
                          {skill.synced ? '已同步' : '未同步'}
                        </span>
                        {skill.error ? <span className="tag tag--danger">{skill.error}</span> : null}
                      </div>
                      <div className="btn-row">
                        <button
                          className="btn btn--sm"
                          type="button"
                          aria-pressed={skill.enabled}
                          onClick={() => void toggleSkill(skill)}
                        >
                          {skill.enabled ? '停用' : '启用'}
                        </button>
                        <button
                          className="btn btn--sm"
                          type="button"
                          onClick={() => void openSkillEditor(skill.name)}
                        >
                          查看 / 编辑
                        </button>
                        <button
                          className="btn btn--sm btn--danger"
                          type="button"
                          onClick={() => void removeSkill(skill.name)}
                        >
                          删除
                        </button>
                      </div>
                    </div>

                    {skill.error ? null : <p className="skill-card__desc">{skill.description}</p>}

                    <div className="skill-card__meta">
                      <span className="muted mono">正文约 {(skill.estimated_tokens ?? 0).toLocaleString()} token</span>
                      <span className="muted mono">
                        引用 {(skill.reference_files ?? []).length} 个 · 约 {(skill.references_estimated_tokens ?? 0).toLocaleString()} token（按需读取）
                      </span>
                      {(skill.agents ?? []).length ? (
                        <span className="tag tag--ok">已绑定：{(skill.agents ?? []).join('、')}</span>
                      ) : (skill.suggested_agents ?? []).length ? (
                        <span className="tag tag--warn">
                          未绑定，建议绑定到 {(skill.suggested_agents ?? []).join('、')}
                        </span>
                      ) : (
                        <span className="tag">未绑定</span>
                      )}
                    </div>

                    {(skill.missing_references ?? []).length ? (
                      <div className="tag tag--warn">
                        产物缺引用文件：{(skill.missing_references ?? []).join('、')}（请点「同步到 .dsh/skills」）
                      </div>
                    ) : null}
                    {(skill.reference_files ?? []).length ? (
                      <details>
                        <summary className="muted">引用文件（按需完整读取）</summary>
                        <ul>
                          {(skill.reference_files ?? []).map((path) => <li className="mono" key={path}>{path}</li>)}
                        </ul>
                      </details>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">必注入技能</h3>
          </div>
          <div className="panel__body stack">
            <p className="field__hint">
              勾选的技能会优先、完整地注入对话系统提示（写作轮次兜底注入）。技能须存在且已启用，不存在时注入环节自动跳过。
            </p>
            {skills.length === 0 ? (
              <p className="muted">尚未发现技能，请先在上方新建或导入并同步。</p>
            ) : (
              <div className="stack">
                {skills.map((skill) => (
                  <label className="checkbox" key={skill.name}>
                    <input
                      type="checkbox"
                      checked={(settings?.writing?.always_inject_skills ?? []).includes(skill.name)}
                      onChange={(event) => toggleAlwaysInject(skill.name, event.target.checked)}
                    />
                    <span className="mono">{skill.name}</span>
                    {skill.enabled ? '' : '（已停用，注入时跳过）'}
                  </label>
                ))}
              </div>
            )}
            {(() => {
              const known = new Set(skills.map((skill) => skill.name))
              const missing = (settings?.writing?.always_inject_skills ?? []).filter((name) => !known.has(name))
              return missing.length ? (
                <p className="field__hint">
                  以下必注入技能当前不存在，注入时会被跳过：{missing.join('、')}
                </p>
              ) : null
            })()}
          </div>
        </div>
      </div>
    )
  }

  function renderSkillModal() {
    if (!skillModal) return null
    const mode = skillModal.mode
    const title =
      mode === 'edit'
        ? `编辑技能：${skillModal.name ?? ''}`
        : mode === 'create'
          ? '新建技能'
          : mode === 'import'
            ? '导入 SKILL.md'
            : 'AI 生成技能草案'
    return (
      <Modal title={title} open onClose={() => setSkillModal(null)}>
        <div className="stack">
          {mode === 'edit' ? (
            <>
              <p className="field__hint">
                整篇编辑 SKILL.md（含 frontmatter）；frontmatter 的 name 必须与目录名一致，description 不能为空。
              </p>
              <p className="field__hint">
                已保存正文约 {(skills.find((skill) => skill.name === skillModal.name)?.estimated_tokens ?? 0).toLocaleString()} token（估算值）；保存后更新用量。
              </p>
              <textarea
                autoComplete={NO_AUTOFILL}
                className="textarea textarea--mono"
                value={skillContent}
                onChange={(event) => setSkillContent(event.target.value)}
              />
              <div className="btn-row">
                <button className="btn btn--primary" type="button" onClick={() => void saveSkillContent()}>
                  保存
                </button>
                <button className="btn" type="button" onClick={() => setSkillModal(null)}>
                  取消
                </button>
              </div>
            </>
          ) : null}

          {mode === 'create' ? (
            <>
              <label className="field">
                <span className="field__label">技能名（kebab-case）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={skillForm.name}
                  onChange={(event) => setSkillForm({ ...skillForm, name: event.target.value })}
                  placeholder="例如 chapter-polish"
                />
              </label>
              <label className="field">
                <span className="field__label">描述（何时使用）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={skillForm.description}
                  onChange={(event) => setSkillForm({ ...skillForm, description: event.target.value })}
                />
              </label>
              <label className="field">
                <span className="field__label">正文（Markdown）</span>
                <textarea
                  autoComplete={NO_AUTOFILL}
                  className="textarea textarea--mono"
                  value={skillForm.body}
                  onChange={(event) => setSkillForm({ ...skillForm, body: event.target.value })}
                  placeholder={'做什么 / 不做什么 / 步骤 / 验收清单'}
                />
              </label>
              <div className="btn-row">
                <button className="btn btn--primary" type="button" onClick={() => void createSkillNow()}>
                  创建
                </button>
                <button className="btn" type="button" onClick={() => setSkillModal(null)}>
                  取消
                </button>
              </div>
            </>
          ) : null}

          {mode === 'import' ? (
            <>
              <p className="field__hint">粘贴完整 SKILL.md（含 frontmatter 的 name 与 description）。</p>
              <label className="field">
                <span className="field__label">名称（可选，frontmatter 缺 name 时兜底）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={skillForm.importName}
                  onChange={(event) => setSkillForm({ ...skillForm, importName: event.target.value })}
                />
              </label>
              <textarea
                autoComplete={NO_AUTOFILL}
                className="textarea textarea--mono"
                value={skillForm.importText}
                onChange={(event) => setSkillForm({ ...skillForm, importText: event.target.value })}
              />
              <div className="btn-row">
                <button className="btn btn--primary" type="button" onClick={() => void importSkillNow()}>
                  导入
                </button>
                <button className="btn" type="button" onClick={() => setSkillModal(null)}>
                  取消
                </button>
              </div>
            </>
          ) : null}

          {mode === 'generate' ? (
            <>
              <p className="field__hint">先生成草案，确认无误后保存。</p>
              <label className="field">
                <span className="field__label">技能用途描述</span>
                <textarea
                  autoComplete={NO_AUTOFILL}
                  className="textarea"
                  value={skillGenerateForm.description}
                  onChange={(event) =>
                    setSkillGenerateForm({ ...skillGenerateForm, description: event.target.value })
                  }
                />
              </label>
              <label className="field">
                <span className="field__label">建议名称（可选，kebab-case）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={skillGenerateForm.name}
                  onChange={(event) => setSkillGenerateForm({ ...skillGenerateForm, name: event.target.value })}
                />
              </label>
              <div className="btn-row">
                <button className="btn" type="button" onClick={() => void generateSkillDraft()}>
                  生成草案
                </button>
              </div>
              {skillDraft ? (
                <>
                  <label className="field">
                    <span className="field__label">草案内容（可编辑）</span>
                    <textarea
                      autoComplete={NO_AUTOFILL}
                      className="textarea textarea--mono"
                      value={skillDraft.content}
                      onChange={(event) => setSkillDraft({ ...skillDraft, content: event.target.value })}
                    />
                  </label>
                  <div className="btn-row">
                    <button className="btn btn--primary" type="button" onClick={() => void saveSkillDraft()}>
                      保存草案为技能
                    </button>
                    <button className="btn" type="button" onClick={() => setSkillModal(null)}>
                      取消
                    </button>
                  </div>
                </>
              ) : null}
            </>
          ) : null}
        </div>
      </Modal>
    )
  }

  // ─────────────────────────── Agent 分区 ───────────────────────────

  function renderAgents() {
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">Agent 列表</h3>
            <div className="btn-row">
              <span className="tag">内置 {agentBuiltinCount} 个</span>
              <span className="tag">工具白名单：{toolWhitelist.join(' / ') || '—'}</span>
              <button className="btn btn--sm" type="button" onClick={() => void loadAgents()}>
                刷新
              </button>
              <button className="btn btn--sm btn--primary" type="button" onClick={openAgentCreate}>
                新建 Agent
              </button>
              <button
                className="btn btn--sm"
                type="button"
                onClick={() => {
                  setAgentDraftForm({ description: '', name: '' })
                  setAgentDraft(null)
                  setAgentModal({ mode: 'draft' })
                }}
              >
                AI 辅助创建
              </button>
            </div>
          </div>
          <div className="panel__body">
            {agents.length === 0 ? (
              <p className="muted">暂无 Agent。</p>
            ) : (
              <div className="card-grid">
                {agents.map((agent) => (
                  <div className="asset-card" key={agent.name}>
                    <div className="asset-card__head">
                      <span className="asset-card__name">{agent.name}</span>
                      {agent.is_builtin ? <span className="tag">内置</span> : null}
                      <span className={agent.enabled ? 'tag tag--ok' : 'tag tag--warn'}>
                        {agent.enabled ? '启用' : '停用'}
                      </span>
                    </div>
                    <div>{agent.title}</div>
                    <div className="asset-card__summary">{agent.description || '（无描述）'}</div>
                    {agent.skills.length ? (
                      <div className="chips">
                        {agent.skills.map((item) => (
                          <span className="chip" key={item}>
                            {item}
                          </span>
                        ))}
                      </div>
                    ) : null}
                    <div className="muted">
                      模型：{agent.provider_id || '未绑定'} / {agent.model_id || '跟随全局'}；工具：
                      {agent.tools.join('、') || '无'}
                    </div>
                    <div className="btn-row">
                      <button className="btn btn--sm" type="button" onClick={() => openAgentEdit(agent)}>
                        编辑
                      </button>
                      <button
                        className="btn btn--sm"
                        type="button"
                        onClick={() => {
                          setDuplicateName(`${agent.name}-copy`)
                          setAgentInline({ name: agent.name, mode: 'duplicate' })
                        }}
                      >
                        复制
                      </button>
                      <button
                        className="btn btn--sm"
                        type="button"
                        onClick={() => {
                          setModelBinding({ provider_id: agent.provider_id, model_id: agent.model_id })
                          setModelCustom(false)
                          setAgentInline({ name: agent.name, mode: 'model' })
                        }}
                      >
                        配置模型
                      </button>
                      <button
                        className="btn btn--sm"
                        type="button"
                        aria-pressed={agent.enabled}
                        onClick={() => void toggleAgent(agent)}
                      >
                        {agent.enabled ? '停用' : '启用'}
                      </button>
                      <button
                        className="btn btn--sm btn--danger"
                        type="button"
                        disabled={agent.is_builtin}
                        title="内置 Agent 不可删除，可复制后修改"
                        onClick={() => void removeAgent(agent.name)}
                      >
                        删除
                      </button>
                    </div>
                    {agentInline?.name === agent.name ? (
                      <div className="panel">
                        <div className="panel__body panel__body--tight stack stack--tight">
                          {agentInline.mode === 'duplicate' ? (
                            <>
                              <p className="field__hint">复制会带上原 Agent 的技能、工具与模型绑定；复制件是可修改的自定义 Agent。</p>
                              <label className="field">
                                <span className="field__label">副本名称（kebab-case）</span>
                                <input
                                  autoComplete={NO_AUTOFILL}
                                  className="input"
                                  value={duplicateName}
                                  onChange={(event) => setDuplicateName(event.target.value)}
                                />
                              </label>
                              <div className="btn-row">
                                <button className="btn btn--primary btn--sm" type="button" onClick={() => void runDuplicateAgent(agent.name)}>
                                  复制
                                </button>
                                <button className="btn btn--ghost btn--sm" type="button" onClick={() => setAgentInline(null)}>
                                  取消
                                </button>
                              </div>
                            </>
                          ) : (
                            <>
                              <p className="field__hint">留空则沿用全局默认模型。</p>
                              <label className="field">
                                <span className="field__label">供应商</span>
                                <select
                                  className="select"
                                  value={modelBinding.provider_id}
                                  onChange={(event) => {
                                    const providerId = event.target.value
                                    setModelBinding({
                                      provider_id: providerId,
                                      model_id: providerModels(providers, providerId)[0]?.id ?? '',
                                    })
                                    setModelCustom(false)
                                  }}
                                >
                                  <option value="">跟随全局默认</option>
                                  {providers.map((provider) => (
                                    <option key={provider.provider_id} value={provider.provider_id}>
                                      {provider.display_name || provider.provider_id}
                                    </option>
                                  ))}
                                </select>
                              </label>
                              <ModelPicker
                                providers={providers}
                                providerId={modelBinding.provider_id}
                                value={modelBinding.model_id}
                                custom={modelCustom}
                                onCustom={setModelCustom}
                                onPick={(modelId) => setModelBinding({ ...modelBinding, model_id: modelId })}
                              />
                              <div className="btn-row">
                                <button className="btn btn--primary btn--sm" type="button" onClick={() => void runSetAgentModel(agent.name)}>
                                  保存绑定
                                </button>
                                <button className="btn btn--ghost btn--sm" type="button" onClick={() => setAgentInline(null)}>
                                  取消
                                </button>
                              </div>
                            </>
                          )}
                        </div>
                      </div>
                    ) : null}
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    )
  }

  function renderAgentModal() {
    if (!agentModal) return null
    const mode = agentModal.mode
    const editing = mode === 'form' && Boolean(agentModal.name)
    const title =
      mode === 'form'
        ? editing
          ? `编辑 Agent：${agentModal.name ?? ''}`
          : '新建 Agent'
        : 'AI 辅助创建 Agent'
    return (
      <Modal title={title} open onClose={() => setAgentModal(null)}>
        <div className="stack">
          {mode === 'form' ? (
            <>
              <div className="row">
                <label className="field" style={{ flex: '1 1 12rem' }}>
                  <span className="field__label">名称（kebab-case）</span>
                  <input
                    autoComplete={NO_AUTOFILL}
                    className="input"
                    value={agentForm.name}
                    disabled={editing}
                    onChange={(event) => setAgentForm({ ...agentForm, name: event.target.value })}
                    placeholder="例如 foreshadow-checker"
                  />
                </label>
                <label className="field" style={{ flex: '1 1 12rem' }}>
                  <span className="field__label">中文短名</span>
                  <input
                    autoComplete={NO_AUTOFILL}
                    className="input"
                    value={agentForm.title}
                    onChange={(event) => setAgentForm({ ...agentForm, title: event.target.value })}
                  />
                </label>
              </div>
              <label className="field">
                <span className="field__label">一句话职责</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={agentForm.description}
                  onChange={(event) => setAgentForm({ ...agentForm, description: event.target.value })}
                />
              </label>
              <label className="field">
                <span className="field__label">系统提示词（必填）</span>
                <textarea
                  autoComplete={NO_AUTOFILL}
                  className="textarea textarea--mono"
                  value={agentForm.system_prompt}
                  onChange={(event) => setAgentForm({ ...agentForm, system_prompt: event.target.value })}
                  placeholder="写清做什么 / 不做什么 / 输出格式"
                />
              </label>
              <div className="stack stack--tight">
                <span className="field__label">绑定技能</span>
                <ResourceState {...skillsResource} hasData={skillsResource.loaded} onRetry={() => void loadSkills()}>
                <div className="chips">
                  {skills.length === 0 ? (
                    <span className="muted">暂无可绑定技能（请先在「技能」分区创建）。</span>
                  ) : (
                    skills.map((skill) => {
                      const on = agentForm.skills.includes(skill.name)
                      return (
                        <button
                          key={skill.name}
                          type="button"
                          className={on ? 'tag tag--primary' : 'tag'}
                          aria-pressed={on}
                          onClick={() =>
                            setAgentForm({
                              ...agentForm,
                              skills: on
                                ? agentForm.skills.filter((item) => item !== skill.name)
                                : [...agentForm.skills, skill.name],
                            })
                          }
                        >
                          {skill.name}
                        </button>
                      )
                    })
                  )}
                </div>
                </ResourceState>
              </div>
              <div className="stack stack--tight">
                <span className="field__label">工具（白名单内）</span>
                <div className="chips">
                  {toolWhitelist.map((tool) => {
                    const on = agentForm.tools.includes(tool)
                    return (
                      <button
                        key={tool}
                        type="button"
                        className={on ? 'tag tag--primary' : 'tag'}
                        aria-pressed={on}
                        onClick={() =>
                          setAgentForm({
                            ...agentForm,
                            tools: on
                              ? agentForm.tools.filter((item) => item !== tool)
                              : [...agentForm.tools, tool],
                          })
                        }
                      >
                        {tool}
                      </button>
                    )
                  })}
                </div>
              </div>
              <div className="stack stack--tight">
                <span className="field__label">主责材料（可多选，路由据此判定写域）</span>
                <div className="chips">
                  {materialDirs.map((material) => {
                    const on = agentForm.materials.includes(material)
                    return (
                      <button
                        key={material}
                        type="button"
                        className={on ? 'tag tag--primary' : 'tag'}
                        aria-pressed={on}
                        onClick={() =>
                          setAgentForm({
                            ...agentForm,
                            materials: on
                              ? agentForm.materials.filter((item) => item !== material)
                              : [...agentForm.materials, material],
                          })
                        }
                      >
                        {material}
                      </button>
                    )
                  })}
                </div>
                <span className="field__hint">
                  一个都不选表示「不写书稿或未声明」。
                </span>
              </div>
              <label className="field">
                <span className="field__label">边界声明（不做什么，一行）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={agentForm.boundaries}
                  onChange={(event) => setAgentForm({ ...agentForm, boundaries: event.target.value })}
                  placeholder="例如：只维护 设定/，不写大纲与正文。"
                />
              </label>
              {agentForm.capabilities.length ? (
                <div className="stack stack--tight">
                  <span className="field__label">能力声明（只读：会被什么词触发、绑定哪个技能）</span>
                  {agentForm.capabilities.map((capability, index) => (
                    <div className="muted mono" key={`${capability.intent ?? 'cap'}-${index}`}>
                      {capability.intent ?? '（未命名意图）'}｜触发词：
                      {(capability.triggers ?? []).join('、') || '—'}｜技能：
                      {capability.skill || '—'}
                    </div>
                  ))}
                </div>
              ) : null}
              <ResourceState {...providersResource} hasData={providersResource.loaded} onRetry={() => void loadProviders()}>
              <div className="row">
                <label className="field" style={{ flex: '1 1 12rem' }}>
                  <span className="field__label">供应商（可留空）</span>
                  <select
                    className="select"
                    aria-label="Agent 模型供应商"
                    value={agentForm.provider_id}
                    onChange={(event) => {
                      const providerId = event.target.value
                      setAgentForm({
                        ...agentForm,
                        provider_id: providerId,
                        model_id: providerModels(providers, providerId)[0]?.id ?? '',
                      })
                      setModelCustom(false)
                    }}
                  >
                    <option value="">跟随全局默认</option>
                    {providers.map((provider) => (
                      <option key={provider.provider_id} value={provider.provider_id}>
                        {provider.display_name || provider.provider_id}
                      </option>
                    ))}
                  </select>
                </label>
                <ModelPicker
                  providers={providers}
                  providerId={agentForm.provider_id}
                  value={agentForm.model_id}
                  custom={modelCustom}
                  onCustom={setModelCustom}
                  onPick={(modelId) => setAgentForm({ ...agentForm, model_id: modelId })}
                />
              </div>
              </ResourceState>
              <div className="btn-row">
                <button className="btn btn--primary" type="button" onClick={() => void saveAgentForm()}>
                  保存
                </button>
                <button className="btn" type="button" onClick={() => setAgentModal(null)}>
                  取消
                </button>
              </div>
            </>
          ) : null}

          {mode === 'draft' ? (
            <>
              <label className="field">
                <span className="field__label">职责描述</span>
                <textarea
                  autoComplete={NO_AUTOFILL}
                  className="textarea"
                  value={agentDraftForm.description}
                  onChange={(event) => setAgentDraftForm({ ...agentDraftForm, description: event.target.value })}
                />
              </label>
              <label className="field">
                <span className="field__label">建议名称（可选）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={agentDraftForm.name}
                  onChange={(event) => setAgentDraftForm({ ...agentDraftForm, name: event.target.value })}
                />
              </label>
              <div className="btn-row">
                <button className="btn" type="button" onClick={() => void runAgentDraft()}>
                  生成草案
                </button>
              </div>
              {agentDraft ? (
                <>
                  <div className="row">
                    <label className="field" style={{ flex: '1 1 12rem' }}>
                      <span className="field__label">名称</span>
                      <input
                        autoComplete={NO_AUTOFILL}
                        className="input"
                        value={agentDraft.name}
                        onChange={(event) => setAgentDraft({ ...agentDraft, name: event.target.value })}
                      />
                    </label>
                    <label className="field" style={{ flex: '1 1 12rem' }}>
                      <span className="field__label">中文短名</span>
                      <input
                        autoComplete={NO_AUTOFILL}
                        className="input"
                        value={agentDraft.title}
                        onChange={(event) => setAgentDraft({ ...agentDraft, title: event.target.value })}
                      />
                    </label>
                  </div>
                  <label className="field">
                    <span className="field__label">职责</span>
                    <input
                      autoComplete={NO_AUTOFILL}
                      className="input"
                      value={agentDraft.description}
                      onChange={(event) => setAgentDraft({ ...agentDraft, description: event.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">系统提示词</span>
                    <textarea
                      autoComplete={NO_AUTOFILL}
                      className="textarea textarea--mono"
                      value={agentDraft.system_prompt}
                      onChange={(event) => setAgentDraft({ ...agentDraft, system_prompt: event.target.value })}
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">技能（逗号分隔）</span>
                    <input
                      autoComplete={NO_AUTOFILL}
                      className="input"
                      value={agentDraft.skills.join(', ')}
                      onChange={(event) =>
                        setAgentDraft({ ...agentDraft, skills: splitList(event.target.value) })
                      }
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">工具（逗号分隔，须在白名单内）</span>
                    <input
                      autoComplete={NO_AUTOFILL}
                      className="input"
                      value={agentDraft.tools.join(', ')}
                      onChange={(event) =>
                        setAgentDraft({ ...agentDraft, tools: splitList(event.target.value) })
                      }
                    />
                  </label>
                  <label className="field">
                    <span className="field__label">params（JSON）</span>
                    <textarea
                      autoComplete={NO_AUTOFILL}
                      className="textarea textarea--mono"
                      value={agentDraftParams}
                      onChange={(event) => setAgentDraftParams(event.target.value)}
                    />
                  </label>
                  <div className="btn-row">
                    <button className="btn btn--primary" type="button" onClick={() => void saveAgentDraft()}>
                      保存草案为 Agent
                    </button>
                    <button className="btn" type="button" onClick={() => setAgentModal(null)}>
                      取消
                    </button>
                  </div>
                </>
              ) : null}
            </>
          ) : null}
        </div>
      </Modal>
    )
  }

  // ─────────────────────────── 规则分区 ───────────────────────────

  function renderRuleTable(title: string, items: RuleItem[], deletable: boolean) {
    return (
      <div className="panel">
        <div className="panel__header">
          <h3 className="panel__title">{title}</h3>
          <span className="tag">{items.length} 条</span>
        </div>
        <div className="panel__body panel__body--tight">
          {items.length === 0 ? (
            <p className="muted">暂无规则。</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>名称</th>
                  <th>正文</th>
                  <th>keys</th>
                  <th>标记</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {items.map((rule) => (
                  <tr key={`${rule.scope}-${rule.name}`}>
                    <td className="mono">{rule.name}</td>
                    <td style={{ whiteSpace: 'pre-wrap' }}>{rule.body}</td>
                    <td>
                      <div className="chips">
                        {(rule.keys ?? []).length ? (
                          (rule.keys ?? []).map((key) => (
                            <span className="chip" key={key}>
                              {key}
                            </span>
                          ))
                        ) : (
                          <span className="muted">无</span>
                        )}
                      </div>
                    </td>
                    <td>
                      <div className="chips">
                        {rule.confirmed ? <span className="tag tag--primary">作者确认</span> : null}
                        <span className={rule.enabled ? 'tag tag--ok' : 'tag tag--warn'}>
                          {rule.enabled ? '启用' : '停用'}
                        </span>
                      </div>
                    </td>
                    <td>
                      <button
                        className="btn btn--sm btn--danger"
                        type="button"
                        disabled={!deletable}
                        title={deletable ? '' : '技能内置规则不可删除'}
                        onClick={() => void removeRule(rule)}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    )
  }

  function renderRules() {
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">作用域</h3>
            <div className="btn-row">
              <button className="btn btn--sm" type="button" onClick={() => void loadRules()}>
                按此项目 ID 解析
              </button>
              <button className="btn btn--sm" type="button" onClick={() => void runCompileRules()}>
                编译规则
              </button>
            </div>
          </div>
          <div className="panel__body stack">
            <label className="field" style={{ maxWidth: '18rem' }}>
              <span className="field__label">项目 ID（留空 = 全局级规则）</span>
              <input
                className="input"
                type="number"
                min={1}
                value={ruleProjectId}
                onChange={(event) => {
                  setRuleProjectId(event.target.value)
                  ruleProjectIdRef.current = event.target.value
                }}
                placeholder="例如 1"
              />
            </label>
            <p className="field__hint">
              优先级链：{resolved?.order.join(' > ') || '作者确认 > 作品级 > 全局 > 技能内置'}。
            </p>
            {compileResult ? (
              <div className="mono" style={{ whiteSpace: 'pre-wrap' }}>
                {`编译产物：${compileResult.path}\n来源：${compileResult.compiled_from}；生效规则 ${compileResult.rules} 条；${compileResult.chars} 字符`}
              </div>
            ) : null}
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">新增 / 更新规则</h3>
          </div>
          <div className="panel__body stack">
            <div className="row">
              <label className="field" style={{ flex: '1 1 14rem' }}>
                <span className="field__label">规则名</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={ruleForm.name}
                  onChange={(event) => setRuleForm({ ...ruleForm, name: event.target.value })}
                />
              </label>
              <label className="field" style={{ flex: '0 1 8rem' }}>
                <span className="field__label">优先级（数字越小越优先）</span>
                <input
                  className="input"
                  type="number"
                  value={ruleForm.priority}
                  onChange={(event) => setRuleForm({ ...ruleForm, priority: Number(event.target.value) || 100 })}
                />
              </label>
            </div>
            <label className="field">
              <span className="field__label">正文</span>
              <textarea
                autoComplete={NO_AUTOFILL}
                className="textarea"
                value={ruleForm.body}
                onChange={(event) => setRuleForm({ ...ruleForm, body: event.target.value })}
                placeholder="例如：禁止出现现代网络用语；对话中不使用语气词堆叠。"
              />
            </label>
            <label className="field">
              <span className="field__label">keys（冲突判定用，逗号分隔）</span>
              <input
                autoComplete={NO_AUTOFILL}
                className="input"
                value={ruleForm.keys}
                onChange={(event) => setRuleForm({ ...ruleForm, keys: event.target.value })}
                placeholder="例如 文风, 人称"
              />
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={ruleForm.confirmed}
                onChange={(event) => setRuleForm({ ...ruleForm, confirmed: event.target.checked })}
              />
              作者确认（优先级最高）
            </label>
            <label className="checkbox">
              <input
                type="checkbox"
                checked={ruleForm.enabled}
                onChange={(event) => setRuleForm({ ...ruleForm, enabled: event.target.checked })}
              />
              启用
            </label>
            <div className="btn-row">
              <button className="btn btn--primary" type="button" onClick={() => void submitRule()}>
                保存规则
              </button>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">生效顺序与冲突</h3>
          </div>
          <div className="panel__body panel__body--tight">
            {!resolved || resolved.conflicts.length === 0 ? (
              <p className="muted">未发现冲突（冲突指同一 key 出现在不同作用域）。</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>key</th>
                    <th>胜出</th>
                    <th>落败</th>
                    <th>依据</th>
                  </tr>
                </thead>
                <tbody>
                  {resolved.conflicts.map((conflict) => (
                    <tr key={conflict.key}>
                      <td className="mono">{conflict.key}</td>
                      <td>
                        {conflict.winner.name}
                        <span className="tag">{conflict.winner.scope}</span>
                      </td>
                      <td>
                        {conflict.losers.map((item) => (
                          <div key={item.id}>
                            {item.name}
                            <span className="tag">{item.scope}</span>
                          </div>
                        ))}
                      </td>
                      <td>{conflict.basis}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {resolved ? (
              <div className="stack stack--tight" style={{ marginTop: 'var(--space-3)' }}>
                <span className="field__label">当前生效规则（{resolved.effective.length} 条）</span>
                <div className="chips">
                  {resolved.effective.map((rule) => (
                    <span className="chip" key={`${rule.scope}-${rule.name}`}>
                      [{rule.scope}] {rule.name}
                    </span>
                  ))}
                </div>
              </div>
            ) : null}
          </div>
        </div>

        {renderRuleTable('全局级规则', ruleGlobal, true)}
        {renderRuleTable('作品级规则', ruleProject, true)}
        {renderRuleTable('技能内置规则', ruleSkill, false)}
      </div>
    )
  }

  // ─────────────────────────── 向量分区 ───────────────────────────

  function renderVector() {
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">嵌入模型</h3>
            <div className="btn-row">
              {vectorInfo ? (
                <span className={vectorInfo.active.offline ? 'tag tag--ok' : 'tag tag--primary'}>
                  当前：{vectorInfo.active.name}（{vectorInfo.active.dim} 维）
                </span>
              ) : null}
              <button className="btn btn--sm" type="button" onClick={() => void loadVector()}>
                刷新
              </button>
            </div>
          </div>
          <div className="panel__body stack">
            <p className="field__hint">默认使用零下载的离线嵌入器；也可自备本地 ONNX 模型。</p>
            {vectorInfo ? (
              <p className="field__hint">
                模型目录：<span className="mono">{vectorInfo.models_dir}</span>；
                显式指定模型路径：{vectorInfo.explicit_path_supported ? '支持' : '不支持'}；
                向量库：<span className="mono">.workbench/vector/vectors.db</span>
              </p>
            ) : null}
            <div className="row">
              <label className="field" style={{ flex: '2 1 16rem' }}>
                <span className="field__label">模型目录 model_dir（留空用默认目录）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={vectorForm.model_dir}
                  onChange={(event) => setVectorForm({ ...vectorForm, model_dir: event.target.value })}
                />
              </label>
              <label className="field" style={{ flex: '1 1 10rem' }}>
                <span className="field__label">云嵌入 provider（可选）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={vectorForm.provider}
                  onChange={(event) => setVectorForm({ ...vectorForm, provider: event.target.value })}
                />
              </label>
              <label className="field" style={{ flex: '1 1 10rem' }}>
                <span className="field__label">云嵌入 model（可选）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={vectorForm.model}
                  onChange={(event) => setVectorForm({ ...vectorForm, model: event.target.value })}
                />
              </label>
            </div>
            <div className="btn-row">
              <button className="btn btn--primary" type="button" onClick={() => void saveVectorModel()}>
                保存嵌入器配置
              </button>
            </div>
            {vectorNote ? <p className="field__hint">{vectorNote}</p> : null}
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">可选路径说明</h3>
          </div>
          <div className="panel__body panel__body--tight">
            {vectorInfo ? (
              <table className="table">
                <thead>
                  <tr>
                    <th>嵌入器</th>
                    <th>维度</th>
                    <th>需要下载</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {vectorInfo.options.map((option) => (
                    <tr key={option.name}>
                      <td className="mono">{option.name}</td>
                      <td className="mono">{option.dim ?? '—'}</td>
                      <td>
                        <span className={option.download_required ? 'tag tag--warn' : 'tag tag--ok'}>
                          {option.download_required ? '需要' : '不需要'}
                        </span>
                      </td>
                      <td>{option.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted">加载中…</p>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">本机模型文件</h3>
          </div>
          <div className="panel__body panel__body--tight">
            {vectorInfo && vectorInfo.local_files.length ? (
              <table className="table">
                <thead>
                  <tr>
                    <th>文件</th>
                    <th>体积</th>
                    <th>sha256</th>
                  </tr>
                </thead>
                <tbody>
                  {vectorInfo.local_files.map((file) => (
                    <tr key={file.path}>
                      <td className="mono">{file.path}</td>
                      <td className="mono">{file.size_bytes}B</td>
                      <td className="mono">{file.sha256}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className="muted">未发现本地模型文件（当前使用零下载的 local-hashing，属正常状态）。</p>
            )}
          </div>
        </div>
      </div>
    )
  }

  // ─────────────────────────── 关于与隔离分区 ───────────────────────────

  function renderAbout() {
    return (
      <div className="stack">
        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">后端状态</h3>
            <div className="btn-row">
              <span className={health?.status === 'ok' ? 'tag tag--ok' : 'tag tag--warn'}>
                {health?.status ?? '未知'}
              </span>
              <button className="btn btn--sm" type="button" onClick={() => void loadHealth()}>
                重新检查
              </button>
            </div>
          </div>
          <div className="panel__body panel__body--tight">
            <table className="table">
              <tbody>
                <tr>
                  <th>版本</th>
                  <td className="mono">{health?.version ?? '—'}</td>
                </tr>
                <tr>
                  <th>端口</th>
                  <td className="mono">{health?.port ?? 8790}</td>
                </tr>
                <tr>
                  <th>数据库</th>
                  <td className="mono">{health?.db_ready ? '就绪' : '未就绪'}</td>
                </tr>
                <tr>
                  <th>全文检索 FTS</th>
                  <td className="mono">{health?.fts ? '可用' : '不可用（降级关键词匹配）'}</td>
                </tr>
                <tr>
                  <th>内置 dsh</th>
                  <td className="mono">{health?.vendor_dsh ? '已就绪' : '未就绪'}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div className="panel">
          <div className="panel__header">
            <h3 className="panel__title">隔离与环境（只读）</h3>
          </div>
          <div className="panel__body stack">
            <ul className="notice__list">
              <li>
                工作台只使用独立 DSH_HOME：<span className="mono">.workbench/dsh-home</span>，
                绝不读写用户全局 <span className="mono">~/.dsh</span>。
              </li>
              <li>
                只调用版本锁定的内置 dsh：<span className="mono">workbench/vendor/dsh/</span>，
                不依赖 PATH 上的全局 dsh、不做全局安装。
              </li>
              <li>
                后端固定监听 <span className="mono">127.0.0.1:8790</span>；不使用 3080（dsh web）、
                8765、3456。
              </li>
              <li>
                供应商凭据存放于 <span className="mono">.workbench/secrets.json</span>（当前 cipher=
                {cipher || '未知'}）；接口只返回掩码，不落日志、不入库。
              </li>
              <li>
                设置与向量库分别位于 <span className="mono">.workbench/settings.json</span> 与
                <span className="mono"> .workbench/vector/vectors.db</span>，可随目录整体备份或重建。
              </li>
              <li>
                技能源目录 <span className="mono">skills/</span>，同步产物
                <span className="mono"> {skillTargetRoot || '.dsh/skills'}</span>（禁止手工编辑产物）。
              </li>
            </ul>
          </div>
        </div>
      </div>
    )
  }

  // ─────────────────────────── 渲染 ───────────────────────────

  return (
    <WorkspacePage>
      <header className="page-header">
        <div>
          <h1 className="page-header__title">设置</h1>
          <p className="page-header__desc">
            模型、引擎、外观与写作偏好等全局设置。
          </p>
        </div>
        <div className="btn-row">
          <button className="btn btn--sm" type="button" onClick={handleReload}>
            重新读取
          </button>
        </div>
      </header>

      <div className="workspace-settings-layout">
      <nav className="workspace-settings-nav" aria-label="设置分区">
        {[{label:'模型与运行', keys:['providers','engines','budget']}, {label:'写作体验', keys:['appearance','editor','chat']}, {label:'创作资产', keys:['templates','skills','agents','rules']}, {label:'检索与系统', keys:['vector','about']}].map(group => <div key={group.label}><h2>{group.label}</h2>{SECTIONS.filter(item => group.keys.includes(item.key)).map(item => <button key={item.key} type="button" className={item.key === active ? 'is-active' : ''} aria-current={item.key === active ? 'page' : undefined} onClick={() => setActive(item.key)}>{item.label}</button>)}</div>)}
      </nav>
      <div className="workspace-settings-body">
      <ResourceState {...({providers:providersResource, engines:enginesResource, budget:budgetResource, skills:skillsResource, agents:agentsResource, rules:rulesResource, vector:vectorResource, about:aboutResource}[active as 'providers'] ?? {loading: settingsLoading, error: settingsError, loaded: !!settings})} hasData={active === 'templates' || ({providers:providersResource, engines:enginesResource, budget:budgetResource, skills:skillsResource, agents:agentsResource, rules:rulesResource, vector:vectorResource, about:aboutResource}[active as 'providers']?.loaded ?? !!settings)} onRetry={handleReload}>
      <div className="stack" style={{ marginTop: 'var(--space-4)' }}>
        {active === 'providers' ? renderProviders() : null}
        {active === 'engines' ? renderEngines() : null}
        {active === 'appearance' ? renderAppearance() : null}
        {active === 'editor' ? renderEditor() : null}
        {active === 'chat' ? renderChat() : null}
        {active === 'templates' ? <TemplateManager /> : null}
        {active === 'budget' ? renderBudget() : null}
        {active === 'skills' ? renderSkills() : null}
        {active === 'agents' ? <>
          {(skillsResource.loading || skillsResource.error) ? <section aria-label="Agent 可选技能读取状态"><h3 className="panel__title">可选技能</h3><ResourceState {...skillsResource} hasData={skillsResource.loaded} onRetry={() => void loadSkills()} /></section> : null}
          {(providersResource.loading || providersResource.error) ? <section aria-label="Agent 模型供应商读取状态"><h3 className="panel__title">可选模型供应商</h3><ResourceState {...providersResource} hasData={providersResource.loaded} onRetry={() => void loadProviders()} /></section> : null}
          {renderAgents()}
        </> : null}
        {active === 'rules' ? renderRules() : null}
        {active === 'vector' ? renderVector() : null}
        {active === 'about' ? renderAbout() : null}
      </div>

      </ResourceState></div></div>

      {renderProviderModal()}
      {renderSkillModal()}
      {renderAgentModal()}

      {confirmNode}
    </WorkspacePage>
  )
}
