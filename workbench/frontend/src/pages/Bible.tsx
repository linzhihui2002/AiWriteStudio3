/**
 * Story Bible —— 作品设定的事实源视图。
 *
 * 数据全部来自项目 Markdown（``设定/`` ``状态/``），本页只做**展示与来源标注**：
 * 实体分栏浏览、伏笔台账、时间线、来源待标注，以及角色状态机 / 视角记忆 / 成长弧线。
 */

import { useCallback, useEffect, useState } from 'react'
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
  { key: 'unknown', label: '来源待标注' },
]

const TABS: Array<{ key: TabKey; label: string }> = [...ENTITY_TABS, ...OTHER_TABS]

/** 状态机字段（与后端 CORE_STATE_FIELDS / SECONDARY_STATE_FIELDS 对齐）。 */
const STATE_FIELDS = ['位置', '伤势', '心理', '持有物', '关系', '能力', '立场', '目标']

const SECTION_LINKS = [
  { key: 'editor', label: '正文编辑器' },
  { key: 'outline', label: '大纲规划' },
  { key: 'cards', label: '设定卡片' },
  { key: 'review', label: '审稿中心' },
] as const

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

  const [overview, setOverview] = useState<BibleOverview | null>(null)
  const [activeTab, setActiveTab] = useState<TabKey>('character')

  const [entities, setEntities] = useState<BibleEntity[]>([])
  const [foreshadows, setForeshadows] = useState<Foreshadow[]>([])
  const [timeline, setTimeline] = useState<TimelineEntry[]>([])
  const [unknown, setUnknown] = useState<Array<{ kind: string; name: string; ref: string; 说明: string }>>([])
  const [characters, setCharacters] = useState<CharacterRow[]>([])
  const [tabLoading, setTabLoading] = useState(false)

  const [planned, setPlanned] = useState<Record<number, string>>({})
  const [stateForm, setStateForm] = useState<StateForm | null>(null)
  const [memoryForm, setMemoryForm] = useState<MemoryForm | null>(null)
  const [growth, setGrowth] = useState<GrowthData | null>(null)
  const [growthLoading, setGrowthLoading] = useState(false)

  const loadOverview = useCallback(async () => {
    try {
      setOverview(await bibleOverview(projectId))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, push])

  const loadCharacters = useCallback(async () => {
    try {
      setCharacters(await listCharacters(projectId))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, push])

  const loadTab = useCallback(
    async (tab: TabKey) => {
      setTabLoading(true)
      try {
        if (tab === 'foreshadow') {
          setForeshadows(await listForeshadows(projectId))
        } else if (tab === 'timeline') {
          setTimeline(await listTimeline(projectId))
        } else if (tab === 'unknown') {
          setUnknown(await bibleUnknown(projectId))
        } else {
          const result = await bibleEntities(projectId, tab)
          setEntities(result.entities)
        }
      } catch (err) {
        push(errorMessage(err), 'error')
      } finally {
        setTabLoading(false)
      }
    },
    [projectId, push],
  )

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

  const goEditor = () => navigate(`/project/${projectId}/editor`)

  const markSource = async (ref: string, source: 'author' | 'model') => {
    try {
      await markBibleSource(projectId, ref, source)
      push(source === 'author' ? '已标为作者来源' : '已标为模型来源', 'success')
      await Promise.all([loadOverview(), loadCharacters(), loadTab(activeTab)])
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const submitStateForm = async () => {
    if (!stateForm) return
    if (!stateForm.value.trim()) {
      push('请填写状态值', 'error')
      return
    }
    try {
      await applyCharacterState(projectId, {
        character: stateForm.character,
        field: stateForm.field,
        value: stateForm.value.trim(),
        source: 'author',
      })
      push('已写回 状态/角色状态.md', 'success')
      setStateForm(null)
      await loadCharacters()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const submitMemoryForm = async () => {
    if (!memoryForm) return
    if (!memoryForm.know_what.trim()) {
      push('请填写记忆内容', 'error')
      return
    }
    try {
      await addMemory(projectId, {
        character: memoryForm.character,
        know_what: memoryForm.know_what.trim(),
        when_known: memoryForm.when_known.trim(),
        source_event: memoryForm.source_event.trim(),
      })
      push('已新增视角记忆', 'success')
      setMemoryForm(null)
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const openGrowth = async (name: string) => {
    setGrowthLoading(true)
    setGrowth({ character: name, series: {}, events: [] })
    try {
      const result = await growthCurve(projectId, name)
      setGrowth({ character: result.character, series: result.series, events: result.events })
    } catch (err) {
      setGrowth(null)
      push(errorMessage(err), 'error')
    } finally {
      setGrowthLoading(false)
    }
  }

  const handleConfirmForeshadow = async (line: number) => {
    try {
      await confirmForeshadow(projectId, line, planned[line] ?? '')
      push('已确认回收该伏笔', 'success')
      setPlanned((prev) => ({ ...prev, [line]: '' }))
      await Promise.all([loadTab('foreshadow'), loadOverview()])
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const entityKeys = (entity: BibleEntity) => Object.keys(entity.fields ?? {})

  const renderEntities = () => (
    <div className="card-grid">
      {entities.map((entity) => (
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
          <div className="row">
            <button className="btn btn--sm" type="button" onClick={() => void markSource(entity.ref, 'author')}>
              标为作者
            </button>
            <button className="btn btn--sm" type="button" onClick={() => void markSource(entity.ref, 'model')}>
              标为模型
            </button>
          </div>
        </article>
      ))}
    </div>
  )

  const renderForeshadows = () => (
    <div className="stack">
      {foreshadows.map((item) => (
        <div className="panel" key={item.line}>
          <div className="panel__header">
            <span className="asset-card__name">{item.content || `伏笔 ${item.line}`}</span>
            <span className={foreshadowTagClass(item.status)}>{item.status}</span>
          </div>
          <div className="panel__body panel__body--tight stack stack--tight">
            <span className="muted">埋设于：{item.planted_in || '未标注'}</span>
            <span className="muted">依据原文：{item.evidence || '未标注'}</span>
            <div className="row">
              <label className="field">
                <span className="field__label">计划回收章（可选）</span>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={planned[item.line] ?? ''}
                  placeholder="例如：第 42 章"
                  onChange={(event) =>
                    setPlanned((prev) => ({ ...prev, [item.line]: event.target.value }))
                  }
                />
              </label>
              <button
                className="btn btn--primary btn--sm"
                type="button"
                onClick={() => void handleConfirmForeshadow(item.line)}
              >
                确认回收
              </button>
            </div>
          </div>
        </div>
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
            {timeline.map((entry, index) => (
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
      {unknown.map((item) => (
        <div className="panel" key={`${item.ref}-${item.说明}`}>
          <div className="panel__body panel__body--tight row row--between">
            <div className="stack stack--tight">
              <span className="mono">{item.ref}</span>
              <span className="muted">{item.说明}</span>
            </div>
            <div className="row">
              <button className="btn btn--sm" type="button" onClick={() => void markSource(item.ref, 'author')}>
                标为作者
              </button>
              <button className="btn btn--sm" type="button" onClick={() => void markSource(item.ref, 'model')}>
                标为模型
              </button>
            </div>
          </div>
        </div>
      ))}
    </div>
  )

  const renderTabBody = () => {
    if (tabLoading) return <p className="muted">加载中…</p>
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
      {
        key: 'foreshadow_open',
        label: '未回收伏笔',
        value: `${overview.foreshadow_open} / ${overview.foreshadow_total}`,
      },
      { key: 'unknown', label: '来源未标注', value: overview.unknown_count, warn: overview.unknown_count > 0 },
    )
  }

  return (
    <div className="page page--wide stack">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">{overview?.project_name ?? 'Story Bible'}</h1>
          <p className="page-header__desc">
            作品设定的事实源：角色、世界、势力、物品、技能、场景、伏笔与时间线，字段级来源分级。
          </p>
        </div>
        <div className="btn-row">
          <button className="btn btn--sm" type="button" onClick={refreshAll}>
            刷新
          </button>
          {SECTION_LINKS.map((section) => (
            <button
              key={section.key}
              className="btn btn--ghost btn--sm"
              type="button"
              onClick={() => navigate(`/project/${projectId}/${section.key}`)}
            >
              {section.label}
            </button>
          ))}
        </div>
      </header>

      {overview ? (
        <section className="row" aria-label="设定总览">
          {stats.map((stat) => (
            <div className="panel" key={stat.key}>
              <div className="panel__body panel__body--tight stack stack--tight">
                <div className="row row--between">
                  <span className="muted">{stat.label}</span>
                  {stat.warn ? (
                    <span className="tag tag--warn" title="审稿按待核实处理">
                      待核实
                    </span>
                  ) : null}
                </div>
                <span className="tag tag--primary">{stat.value}</span>
              </div>
            </div>
          ))}
        </section>
      ) : (
        <p className="muted">正在加载总览…</p>
      )}

      <nav className="tabs" aria-label="Story Bible 分类">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            className={activeTab === tab.key ? 'tab is-active' : 'tab'}
            type="button"
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">
            {TABS.find((tab) => tab.key === activeTab)?.label}
            {isEntityKind(activeTab) && !tabLoading ? `（${entities.length} 条）` : ''}
          </h2>
        </div>
        <div className="panel__body">{renderTabBody()}</div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">角色状态与成长</h2>
          <span className="muted">状态来自 状态/角色状态.md；记忆来自 状态/角色记忆.md</span>
        </div>
        <div className="panel__body stack">
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
                      </form>
                    ) : null}
                  </article>
                )
              })}
            </div>
          )}
        </div>
      </section>

      {growth ? (
        <section className="panel">
          <div className="panel__header">
            <h2 className="panel__title">{growth.character} 的成长曲线</h2>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setGrowth(null)}>
              收起
            </button>
          </div>
          <div className="panel__body stack">
            {growthLoading ? (
              <p className="muted">加载中…</p>
            ) : Object.keys(growth.series).length === 0 ? (
              <p className="muted">暂无状态变化点，先写入角色状态或执行章节摄取。</p>
            ) : (
              Object.entries(growth.series).map(([field, points]) => (
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
            )}
          </div>
        </section>
      ) : null}
    </div>
  )
}