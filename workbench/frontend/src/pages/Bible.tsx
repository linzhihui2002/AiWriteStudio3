import { usePageChatContext } from '../state/usePageChatContext'
import WorkspacePage, { ResourceState, useResourceRequest } from '../components/WorkspacePage'
/**
 * Story Bible —— 作品设定的事实源视图。
 *
 * 数据全部来自项目 Markdown（``设定/`` ``状态/``），本页只做**展示与来源标注**：
 * 实体分栏浏览、伏笔台账、时间线、来源待标注，以及角色状态机 / 视角记忆 / 成长弧线。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  addMemory,
  applyCharacterState,
  bibleEntities,
  bibleOverview,
  bibleUnknown,
  confirmForeshadow,
  growthCurve,
  listCharacters,
  listForeshadows,
  listTimeline,
  markBibleSource,
} from '../api/client'
import type { BibleEntity, BibleOverview, Foreshadow, TimelineEntry } from '../api/types'
import MarkdownView from '../components/MarkdownView'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'
import { useScopedAction } from '../state/useScopedAction'
import '../styles/bible.css'

type EntityKind = 'character' | 'world' | 'faction' | 'item' | 'skill' | 'scene'
type TabKey = EntityKind | 'foreshadow' | 'timeline' | 'unknown'

interface CharacterRow {
  name: string
  fields: Record<string, string>
  missing: string[]
  source: string
  state: Record<string, string>
}

interface GrowthData {
  character: string
  series: Record<string, Array<Record<string, unknown>>>
  events: TimelineEntry[]
}

interface StateForm {
  character: string
  field: string
  value: string
}

interface MemoryForm {
  character: string
  know_what: string
  when_known: string
  source_event: string
}

const ENTITY_TABS: Array<{ key: EntityKind; label: string }> = [
  { key: 'character', label: '人物' },
  { key: 'world', label: '世界' },
  { key: 'faction', label: '势力' },
  { key: 'item', label: '物品' },
  { key: 'skill', label: '技能' },
  { key: 'scene', label: '场景' },
]

const OTHER_TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'foreshadow', label: '伏笔' },
  { key: 'timeline', label: '时间线' },
  { key: 'unknown', label: '来源与缺项' },
]

const TABS: Array<{ key: TabKey; label: string }> = [...ENTITY_TABS, ...OTHER_TABS]

/** 状态机字段（与后端 CORE_STATE_FIELDS / SECONDARY_STATE_FIELDS 对齐）。 */
const STATE_FIELDS = ['位置', '伤势', '心理', '持有物', '关系', '能力', '立场', '目标']


function isEntityKind(tab: TabKey): tab is EntityKind {
  return tab !== 'foreshadow' && tab !== 'timeline' && tab !== 'unknown'
}

/** 来源分级标签：author=作者 / model=模型 / unknown=未标注（审稿按待核实处理）。 */
function SourceTag({ source }: { source: string }) {
  if (source === 'author') return <span className="tag tag--ok">作者</span>
  if (source === 'model') return <span className="tag tag--warn">模型</span>
  return (
    <span className="tag tag--danger" title="审稿按待核实处理">
      来源未标注
    </span>
  )
}

function foreshadowTagClass(status: string): string {
  return status === '已回收' ? 'tag tag--ok' : 'tag tag--warn'
}

function sourceLabel(source: string): string {
  if (source === 'author') return '作者'
  if (source === 'model') return '模型'
  return '未知来源'
}

export default function Bible() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const navigate = useNavigate()
  const { push } = useToast()
  const action = useScopedAction(projectId)

  const [overview, setOverview] = useState<BibleOverview | null>(null)
  const [activeTab, setActiveTab] = useState<TabKey>('character')
  const activeTabRef = useRef(activeTab)
  activeTabRef.current = activeTab

  const [entities, setEntities] = useState<BibleEntity[]>([])
  const [foreshadows, setForeshadows] = useState<Foreshadow[]>([])
  const [timeline, setTimeline] = useState<TimelineEntry[]>([])
  const [unknown, setUnknown] = useState<Array<{ kind: string; name: string; ref: string; rel_path: string; reason: 'source_unknown' | 'missing_field'; field?: string; 说明: string }>>([])
  const [characters, setCharacters] = useState<CharacterRow[]>([])
  const overviewResource = useResourceRequest(projectId)
  const charactersResource = useResourceRequest(projectId)
  const tabResource = useResourceRequest(`${projectId}:${activeTab}`)
  const tabLoading = tabResource.loading
  const [selectedEntity, setSelectedEntity] = useState('')
  const [query, setQuery] = useState('')

  const [planned, setPlanned] = useState<Record<number, string>>({})
  const [stateForm, setStateForm] = useState<StateForm | null>(null)
  const [memoryForm, setMemoryForm] = useState<MemoryForm | null>(null)
  const [growth, setGrowth] = useState<GrowthData | null>(null)
  const [growthName, setGrowthName] = useState('')
  const growthResource = useResourceRequest(`${projectId}:${growthName}`)

  const loadOverview = useCallback(async () => {
    const token = overviewResource.begin()
    try { const data = await bibleOverview(projectId); if (!overviewResource.accept(token)) return; setOverview(data); overviewResource.finish(token) }
    catch (err) { overviewResource.fail(token, errorMessage(err)) }
  }, [projectId])

  const loadCharacters = useCallback(async () => {
    const token = charactersResource.begin()
    try { const data = await listCharacters(projectId); if (!charactersResource.accept(token)) return; setCharacters(data); charactersResource.finish(token) }
    catch (err) { charactersResource.fail(token, errorMessage(err)) }
  }, [projectId])

  const loadTab = useCallback(async (tab: TabKey) => {
    // A completed write may still hold the callback for the previous category.
    // Do not give that request the current category's loading/result token.
    if (tab !== activeTabRef.current) return
    const token = tabResource.begin()
    try {
      if (tab === 'foreshadow') { const data = await listForeshadows(projectId); if (!tabResource.accept(token)) return; setForeshadows(data) }
      else if (tab === 'timeline') { const data = await listTimeline(projectId); if (!tabResource.accept(token)) return; setTimeline(data) }
      else if (tab === 'unknown') { const data = await bibleUnknown(projectId); if (!tabResource.accept(token)) return; setUnknown(data) }
      else { const data = await bibleEntities(projectId, tab); if (!tabResource.accept(token)) return; setEntities(data.entities) }
      tabResource.finish(token)
      void loadOverview()
    } catch (err) { tabResource.fail(token, errorMessage(err)) }
  }, [projectId, activeTab])
  useEffect(() => { setOverview(null); setCharacters([]); setEntities([]); setForeshadows([]); setTimeline([]); setUnknown([]); setStateForm(null); setMemoryForm(null); setPlanned({}); setGrowth(null); setGrowthName(''); setSelectedEntity(''); setQuery('') }, [projectId])

  useEffect(() => {
    void loadOverview()
    void loadCharacters()
  }, [loadOverview, loadCharacters])

  useEffect(() => {
    void loadTab(activeTab)
  }, [activeTab, loadTab])

  const refreshAll = () => {
    void loadOverview()
    void loadCharacters()
    void loadTab(activeTab)
  }

  useEffect(() => {
    const refresh = () => { void loadOverview(); void loadCharacters(); void loadTab(activeTab) }
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [loadOverview, loadCharacters, loadTab, activeTab])

  const goEditor = () => navigate(`/project/${projectId}/editor`)

  const markSource = async (ref: string, source: 'author' | 'model') => {
    const token = action.begin('source')
    if (!token) return
    try {
      await markBibleSource(projectId, ref, source)
      if (!action.accept(token)) return
      push(source === 'author' ? '已标为作者来源' : '已标为模型来源', 'success')
      await Promise.all([loadOverview(), loadCharacters(), loadTab(activeTab)])
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const submitStateForm = async () => {
    if (!stateForm) return
    if (!stateForm.value.trim()) {
      push('请填写状态值', 'error')
      return
    }
    const token = action.begin('state')
    if (!token) return
    try {
      await applyCharacterState(projectId, {
        character: stateForm.character,
        field: stateForm.field,
        value: stateForm.value.trim(),
        source: 'author',
      })
      if (!action.accept(token)) return
      push('已写回 状态/角色状态.md', 'success')
      setStateForm(null)
      await Promise.all([loadCharacters(), loadTab(activeTab)])
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const submitMemoryForm = async () => {
    if (!memoryForm) return
    if (!memoryForm.know_what.trim()) {
      push('请填写记忆内容', 'error')
      return
    }
    const token = action.begin('memory')
    if (!token) return
    try {
      await addMemory(projectId, {
        character: memoryForm.character,
        know_what: memoryForm.know_what.trim(),
        when_known: memoryForm.when_known.trim(),
        source_event: memoryForm.source_event.trim(),
      })
      if (!action.accept(token)) return
      push('已新增视角记忆', 'success')
      setMemoryForm(null)
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const loadGrowth = useCallback(async () => {
    if (!growthName) return
    const token = growthResource.begin()
    try { const result = await growthCurve(projectId, growthName); if (!growthResource.accept(token)) return; setGrowth({ character: result.character, series: result.series, events: result.events }); growthResource.finish(token) } catch (err) { growthResource.fail(token, errorMessage(err)) }
  }, [projectId, growthName])
  useEffect(() => { if (growthName) void loadGrowth() }, [growthName, loadGrowth])
  const openGrowth = (name: string) => {
    if (name === growthName) { void loadGrowth(); return }
    setGrowth(null); setGrowthName(name)
  }

  const handleConfirmForeshadow = async (item: Foreshadow) => {
    const line = item.line
    const token = action.begin('payoff')
    if (!token) return
    try {
      await confirmForeshadow(projectId, line, planned[line] ?? item.planned_chapter ?? '', item.document_hash)
      if (!action.accept(token)) return
      push('已确认回收该伏笔', 'success')
      setPlanned((prev) => ({ ...prev, [line]: '' }))
      await Promise.all([loadTab('foreshadow'), loadOverview()])
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const entityKeys = (entity: BibleEntity) => Object.keys(entity.fields ?? {})

  const matchesQuery = (value: unknown) => !query.trim() || JSON.stringify(value).toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
  const visibleEntities = entities.filter(matchesQuery)
  const visibleForeshadows = foreshadows.filter(matchesQuery)
  const visibleTimeline = timeline.filter(matchesQuery)
  const visibleUnknown = unknown.filter(matchesQuery)
  const currentEntity = visibleEntities.find(entity => entity.ref === selectedEntity) || visibleEntities[0]
  const activeMaterial = !tabResource.loaded ? null : isEntityKind(activeTab) ? currentEntity?.rel_path : activeTab === 'foreshadow' ? '设定/伏笔管理.md' : activeTab === 'timeline' ? '状态/时间线.md' : null
  usePageChatContext({ projectId, activeFile: activeMaterial, files: tabResource.loaded && isEntityKind(activeTab) ? entities.map(entity => entity.rel_path) : [], onFilesChanged: async () => { await Promise.allSettled([loadOverview(), loadCharacters(), loadTab(activeTab)]) } })
  const renderEntities = () => (
    <div className="workspace-collection">
      <nav className="workspace-collection-nav" aria-label="设定条目">{visibleEntities.map(entity => <button key={entity.ref} className={entity.ref === currentEntity?.ref ? 'is-active' : ''} aria-pressed={entity.ref === currentEntity?.ref} onClick={() => setSelectedEntity(entity.ref)}>{entity.name}<small>{sourceLabel(entity.source)} · {Object.keys(entity.fields || {}).length} 个字段</small></button>)}</nav>
      <div className="workspace-collection-detail">
      {visibleEntities.filter(entity => entity.ref === currentEntity?.ref).map((entity) => (
        <article className="asset-card" key={entity.ref}>
          <div className="asset-card__head">
            <span className="asset-card__name">{entity.name}</span>
            <SourceTag source={entity.source} />
          </div>
          {entityKeys(entity).length > 0 ? (
            <div className="chips">
              {entityKeys(entity).map((key) => (
                <span className="chip" key={key}>
                  {key}
                </span>
              ))}
            </div>
          ) : null}
          {entity.summary ? <p className="asset-card__summary">{entity.summary}</p> : null}
          {entity.content ? <MarkdownView text={entity.content} emptyHint="（无正文）" /> : null}
          {entity.missing && entity.missing.length > 0 ? (
            <div className="stack stack--tight">
              <span className="muted">缺失深度字段（审稿按待核实处理）</span>
              <div className="chips">
                {entity.missing.map((field) => (
                  <span className="chip" key={field}>
                    {field}
                  </span>
                ))}
              </div>
            </div>
          ) : null}
          <p className="muted">原材料：{entity.rel_path}</p><button className="btn btn--sm" onClick={() => navigate(`/project/${projectId}/editor?${new URLSearchParams({path: entity.rel_path})}`)}>打开原材料</button>
          <div className="row">
            <button className="btn btn--sm" type="button" disabled={!!action.pending} onClick={() => void markSource(entity.ref, 'author')}>
              标为作者
            </button>
            <button className="btn btn--sm" type="button" disabled={!!action.pending} onClick={() => void markSource(entity.ref, 'model')}>
              标为模型
            </button>
          </div>
        </article>
      ))}</div>
    </div>
  )

  const renderForeshadows = () => (
    <div className="bible-records">
      {visibleForeshadows.map((item) => (
        <article className="bible-record" key={item.line}>
          <div className="panel__header">
            <span className="asset-card__name">{item.content || `伏笔 ${item.line}`}</span>
            <span className={foreshadowTagClass(item.status)}>{item.is_planned ? '计划候选' : item.status}</span>
          </div>
          <div className="panel__body panel__body--tight stack stack--tight">
            <span className="muted">埋设于：{item.planted_in || '未标注'}</span>
            <span className="muted">依据原文：{item.evidence || '未标注'}</span>
            {!item.is_planned && ['待回收', '疑似回收'].includes(item.status) ? <div className="row bible-record__actions">
              <label className="field">
                <span className="field__label">计划回收章（可选）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  disabled={!!action.pending}
                  value={planned[item.line] ?? item.planned_chapter ?? ''}
                  placeholder="例如：第 42 章"
                  onChange={(event) =>
                    setPlanned((prev) => ({ ...prev, [item.line]: event.target.value }))
                  }
                />
              </label>
              <button
                className="btn btn--primary btn--sm"
                type="button"
                disabled={!!action.pending}
                onClick={() => void handleConfirmForeshadow(item)}
              >
                确认回收
              </button>
            </div> : <span className="field__hint">{item.is_planned ? '尚未埋设，计划不作为正文事实。' : item.status === '已回收' ? '作者已确认回收。' : '该伏笔已作废。'}</span>}
          </div>
        </article>
      ))}
    </div>
  )

  const renderTimeline = () => (
    <div className="panel">
      <div className="panel__body panel__body--tight">
        <table className="table">
          <thead>
            <tr>
              <th>章节</th>
              <th>故事内时间</th>
              <th>事件</th>
              <th>依据原文</th>
            </tr>
          </thead>
          <tbody>
            {visibleTimeline.map((entry, index) => (
              <tr key={`${entry.chapter}-${index}`}>
                <td>{entry.chapter || '未标注'}</td>
                <td>{entry.story_time || '—'}</td>
                <td>{entry.event || '—'}</td>
                <td className="muted">{entry.evidence || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )

  const renderUnknown = () => (
    <div className="stack stack--tight">
      {visibleUnknown.map((item) => (
        <div className="panel" key={`${item.ref}-${item.说明}`}>
          <div className="panel__body panel__body--tight row row--between">
            <div className="stack stack--tight">
              <span className="mono">{item.ref}</span>
              <span className="muted">{item.说明}</span>
            </div>
            <div className="row">
              {item.reason === 'missing_field' ? <button className="btn btn--sm" type="button" onClick={() => navigate(`/project/${projectId}/editor?${new URLSearchParams({path: item.rel_path})}`)}>补齐原材料</button> : <>
              <button className="btn btn--sm" type="button" disabled={!!action.pending} onClick={() => void markSource(item.ref, 'author')}>
                标为作者
              </button>
              <button className="btn btn--sm" type="button" disabled={!!action.pending} onClick={() => void markSource(item.ref, 'model')}>
                标为模型
              </button></>}
            </div>
          </div>
        </div>
      ))}
    </div>
  )

  const renderTabBody = () => {
    const total = isEntityKind(activeTab) ? entities.length : activeTab === 'foreshadow' ? foreshadows.length : activeTab === 'timeline' ? timeline.length : unknown.length
    const matched = isEntityKind(activeTab) ? visibleEntities.length : activeTab === 'foreshadow' ? visibleForeshadows.length : activeTab === 'timeline' ? visibleTimeline.length : visibleUnknown.length
    if (total > 0 && query.trim() && matched === 0) return <div className="empty-state"><p className="empty-state__title">没有匹配的设定资料</p><p className="empty-state__desc">当前分类有 {total} 条资料，请调整关键词或清除搜索。</p><button className="btn btn--sm" type="button" onClick={() => setQuery('')}>清除搜索</button></div>
    if (isEntityKind(activeTab)) {
      if (entities.length === 0) {
        return (
          <div className="empty-state">
            <p className="empty-state__title">暂无可显示的条目</p>
            <p className="empty-state__desc">暂无条目，可补齐设定后刷新。</p>
          </div>
        )
      }
      return renderEntities()
    }
    if (activeTab === 'foreshadow') {
      if (foreshadows.length === 0) {
        return (
          <div className="empty-state">
            <p className="empty-state__title">暂无伏笔记录</p>
            <p className="empty-state__desc">可手工维护，或在章节摄取后自动登记。</p>
          </div>
        )
      }
      return renderForeshadows()
    }
    if (activeTab === 'timeline') {
      if (timeline.length === 0) {
        return (
          <div className="empty-state">
            <p className="empty-state__title">暂无时间线条目</p>
            <p className="empty-state__desc">执行章节摄取后按章序展示。</p>
          </div>
        )
      }
      return renderTimeline()
    }
    if (unknown.length === 0) {
      return (
        <div className="empty-state">
          <p className="empty-state__title">所有来源均已标注</p>
          <p className="empty-state__desc">没有来源为「未标注」的条目，也没有待核实的深度字段缺失。</p>
        </div>
      )
    }
    return renderUnknown()
  }

  const stats: Array<{ key: string; label: string; value: string | number; warn?: boolean }> = []
  if (overview) {
    stats.push(
      { key: 'character', label: '角色', value: overview.counts.character ?? 0 },
      { key: 'world', label: overview.labels.world ?? '世界', value: overview.counts.world ?? 0 },
      { key: 'faction', label: overview.labels.faction ?? '势力', value: overview.counts.faction ?? 0 },
      { key: 'item', label: overview.labels.item ?? '物品', value: overview.counts.item ?? 0 },
      { key: 'skill', label: overview.labels.skill ?? '技能', value: overview.counts.skill ?? 0 },
      { key: 'scene', label: overview.labels.scene ?? '场景', value: overview.counts.scene ?? 0 },
      { key: 'foreshadow', label: overview.labels.foreshadow ?? '伏笔', value: overview.counts.foreshadow ?? 0 },
      { key: 'timeline', label: '时间线条目', value: overview.timeline_count },
      { key: 'foreshadow_planned', label: '计划伏笔', value: overview.foreshadow_planned ?? 0 },
      {
        key: 'foreshadow_open',
        label: '未回收伏笔',
        value: `${overview.foreshadow_open} / ${overview.foreshadow_total}`,
      },
      { key: 'unknown', label: '来源未标注', value: overview.unknown_count, warn: overview.unknown_count > 0 },
    )
  }

  return (
    <WorkspacePage className="stack bible-page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">设定资料</h1>
          <p className="page-header__desc">
            {overview?.project_name ? `${overview.project_name} · ` : ''}作品设定的事实源：角色、世界、势力、物品、技能、场景、伏笔与时间线，字段级来源分级。
          </p>
        </div>
        <div className="btn-row">
          <button className="btn btn--sm" type="button" onClick={refreshAll}>
            刷新
          </button>
        </div>
      </header>

      <ResourceState {...overviewResource} hasData={overviewResource.loaded && !!overview} onRetry={() => void loadOverview()}>
      {overview ? (
        <section className="bible-statistics" aria-label="设定总览">
          {stats.map((stat) => (
            <button className="bible-stat" key={stat.key} type="button" onClick={() => { const key = ['foreshadow_open', 'foreshadow_planned'].includes(stat.key) ? 'foreshadow' : stat.key; setQuery(''); setActiveTab(key as TabKey) }}>
                <div className="row row--between">
                  <span className="muted">{stat.label}</span>
                  {stat.warn ? (
                    <span className="tag tag--warn" title="审稿按待核实处理">
                      待核实
                    </span>
                  ) : null}
                </div>
                <strong className="bible-stat__value">{stat.value}</strong>
            </button>
          ))}
        </section>
      ) : (
        <p className="muted">正在加载总览…</p>
      )}

      </ResourceState>
      <nav className="workspace-tabs" aria-label="Story Bible 分类">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            className={activeTab === tab.key ? 'tab is-active' : 'tab'}
            type="button"
            aria-pressed={activeTab === tab.key}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>
      <label className="field"><span className="field__label">搜索当前分类</span><input className="input" autoComplete={NO_AUTOFILL} value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索名称、字段或依据" /></label>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">
            {TABS.find((tab) => tab.key === activeTab)?.label}
            {isEntityKind(activeTab) && !tabLoading ? `（${visibleEntities.length}${query.trim() ? ` / ${entities.length}` : ''} 条）` : ''}
          </h2>
        </div>
        <div className="panel__body"><ResourceState {...tabResource} hasData={tabResource.loaded} onRetry={() => void loadTab(activeTab)}>{renderTabBody()}</ResourceState></div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">角色状态与成长</h2>
          <span className="muted">状态来自 状态/角色状态.md；记忆来自 状态/角色记忆.md</span>
        </div>
        <div className="panel__body stack">
          <ResourceState {...charactersResource} hasData={charactersResource.loaded} onRetry={() => void loadCharacters()}>
          {characters.length === 0 ? (
            <p className="muted">暂无角色：设定/人物设定.md 为空或未解析到角色节。</p>
          ) : (
            <div className="card-grid">
              {characters.map((character) => {
                const currentStateForm =
                  stateForm && stateForm.character === character.name ? stateForm : null
                const currentMemoryForm =
                  memoryForm && memoryForm.character === character.name ? memoryForm : null
                return (
                  <article className="asset-card" key={character.name}>
                    <div className="asset-card__head">
                      <span className="asset-card__name">{character.name}</span>
                      <SourceTag source={character.source} />
                    </div>
                    <div className="chips">
                      {Object.entries(character.state).length === 0 ? (
                        <span className="chip">状态未记录</span>
                      ) : (
                        Object.entries(character.state).map(([field, value]) => (
                          <span className="chip" key={field}>
                            {field}：{value}
                          </span>
                        ))
                      )}
                    </div>
                    <div className="row">
                      <button
                        className="btn btn--sm"
                        type="button"
                        disabled={!!action.pending}
                        onClick={() =>
                          setStateForm(
                            currentStateForm
                              ? null
                              : { character: character.name, field: STATE_FIELDS[0], value: '' },
                          )
                        }
                      >
                        改状态
                      </button>
                      <button
                        className="btn btn--sm"
                        type="button"
                        disabled={!!action.pending}
                        onClick={() =>
                          setMemoryForm(
                            currentMemoryForm
                              ? null
                              : {
                                  character: character.name,
                                  know_what: '',
                                  when_known: '',
                                  source_event: '',
                                },
                          )
                        }
                      >
                        记一条视角记忆
                      </button>
                      <button
                        className="btn btn--sm"
                        type="button"
                        onClick={() => void openGrowth(character.name)}
                      >
                        看成长曲线
                      </button>
                    </div>

                    {currentStateForm ? (
                      <form
                        className="stack stack--tight"
                        autoComplete="off"
                        onSubmit={(event) => {
                          event.preventDefault()
                          void submitStateForm()
                        }}
                      >
                        <fieldset className="bible-form stack stack--tight" disabled={!!action.pending}>
                        <label className="field">
                          <span className="field__label">状态字段</span>
                          <select
                            className="select"
                            value={currentStateForm.field}
                            onChange={(event) =>
                              setStateForm((prev) =>
                                prev ? { ...prev, field: event.target.value } : prev,
                              )
                            }
                          >
                            {STATE_FIELDS.map((field) => (
                              <option key={field} value={field}>
                                {field}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="field">
                          <span className="field__label">新值</span>
                          <input
                            autoComplete={NO_AUTOFILL}
                            className="input"
                            value={currentStateForm.value}
                            placeholder="例如：断崖镇 · 左臂轻伤"
                            onChange={(event) =>
                              setStateForm((prev) =>
                                prev ? { ...prev, value: event.target.value } : prev,
                              )
                            }
                          />
                        </label>
                        <div className="row">
                          <button className="btn btn--primary btn--sm" type="submit">
                            保存状态
                          </button>
                          <button
                            className="btn btn--ghost btn--sm"
                            type="button"
                            onClick={() => setStateForm(null)}
                          >
                            取消
                          </button>
                        </div>
                        <span className="field__hint">保存后会写入角色状态。</span>
                        </fieldset>
                      </form>
                    ) : null}

                    {currentMemoryForm ? (
                      <form
                        className="stack stack--tight"
                        autoComplete="off"
                        onSubmit={(event) => {
                          event.preventDefault()
                          void submitMemoryForm()
                        }}
                      >
                        <fieldset className="bible-form stack stack--tight" disabled={!!action.pending}>
                        <label className="field">
                          <span className="field__label">知道什么</span>
                          <input
                            autoComplete={NO_AUTOFILL}
                            className="input"
                            value={currentMemoryForm.know_what}
                            placeholder="例如：镇长收了黑钱"
                            onChange={(event) =>
                              setMemoryForm((prev) =>
                                prev ? { ...prev, know_what: event.target.value } : prev,
                              )
                            }
                          />
                        </label>
                        <label className="field">
                          <span className="field__label">何时知道</span>
                          <input
                            autoComplete={NO_AUTOFILL}
                            className="input"
                            value={currentMemoryForm.when_known}
                            placeholder="可选，例如：第 12 章"
                            onChange={(event) =>
                              setMemoryForm((prev) =>
                                prev ? { ...prev, when_known: event.target.value } : prev,
                              )
                            }
                          />
                        </label>
                        <label className="field">
                          <span className="field__label">来源事件</span>
                          <input
                            autoComplete={NO_AUTOFILL}
                            className="input"
                            value={currentMemoryForm.source_event}
                            placeholder="可选，触发认知的事件"
                            onChange={(event) =>
                              setMemoryForm((prev) =>
                                prev ? { ...prev, source_event: event.target.value } : prev,
                              )
                            }
                          />
                        </label>
                        <div className="row">
                          <button className="btn btn--primary btn--sm" type="submit">
                            记录记忆
                          </button>
                          <button
                            className="btn btn--ghost btn--sm"
                            type="button"
                            onClick={() => setMemoryForm(null)}
                          >
                            取消
                          </button>
                        </div>
                        </fieldset>
                      </form>
                    ) : null}
                  </article>
                )
              })}
            </div>
          )}
          </ResourceState>
        </div>
      </section>

      {growthName ? (
        <section className="panel">
          <div className="panel__header">
            <h2 className="panel__title">{growthName} 的成长曲线</h2>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => { setGrowth(null); setGrowthName('') }}>
              收起
            </button>
          </div>
          <div className="panel__body stack">
            <ResourceState {...growthResource} hasData={growthResource.loaded && growth?.character === growthName} onRetry={() => void loadGrowth()}>
            {growth?.character === growthName && Object.keys(growth.series).length === 0 ? (
              <p className="muted">暂无状态变化点，先写入角色状态或执行章节摄取。</p>
            ) : (
              Object.entries(growth?.character === growthName ? growth.series : {}).map(([field, points]) => (
                <div className="stack stack--tight" key={field}>
                  <h3 className="panel__title">{field}</h3>
                  {points.map((point, index) => {
                    const chapter = String(point.chapter ?? '')
                    return (
                      <div className="row" key={`${field}-${index}`}>
                        <button className="btn btn--ghost btn--sm" type="button" onClick={goEditor}>
                          {chapter ? `第 ${chapter} 章` : '未标注章节'}
                        </button>
                        <span className="chip">{String(point.value ?? '')}</span>
                        {point.evidence ? (
                          <span className="muted">{String(point.evidence)}</span>
                        ) : null}
                        <span className="tag">{sourceLabel(String(point.source ?? ''))}</span>
                        <span className="muted">v{String(point.version ?? 1)}</span>
                      </div>
                    )
                  })}
                </div>
              ))
            )}</ResourceState>
          </div>
        </section>
      ) : null}
    </WorkspacePage>
  )
}
