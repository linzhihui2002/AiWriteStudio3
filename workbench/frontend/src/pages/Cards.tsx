import { usePageChatContext } from '../state/usePageChatContext'
import WorkspacePage, { ResourceState, useResourceRequest } from '../components/WorkspacePage'
/**
 * 设定卡片管理页（Task 27a）—— 卡片视图 / 高亮色 / 双向同步 / 导出。
 *
 * 事实源仍是 ``设定/*.md``：卡片只是视图层；新增/编辑/删除一律经收件箱或 diff 写回。
 * 高亮色只存 ``.meta/asset_cards.json``，色板走 token 名（主题映射实际色值）。
 */

import { useCallback, useEffect, useState, type CSSProperties, type Dispatch, type SetStateAction } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  addCard,
  cardChapterLine,
  deleteCard,
  exportCards,
  generateCard,
  listCards,
  refreshCards,
  getCardParseTask,
  cancelCardParseTask,
  setCardHighlight,
  updateCard,
} from '../api/client'
import type { CardItem, CardList, CardParseTask } from '../api/types'
import Modal from '../components/Modal'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'
import { copyTextToClipboard } from '../lib/clipboard'
import '../styles/cards.css'
import { useScopedAction } from '../state/useScopedAction'

interface FieldRow {
  key: string
  value: string
  originalName?: string
}

interface ChapterLine {
  ref: string
  name: string
  total: number
  occurrences: Array<{ rel_path: string; number: number; count: number }>
}

const DEFAULT_PALETTE = ['highlight-1', 'highlight-2', 'highlight-3', 'highlight-4', 'highlight-5']

type RowSetter = Dispatch<SetStateAction<FieldRow[]>>

function toFieldRows(fields: Record<string, string>): FieldRow[] {
  return Object.entries(fields).map(([key, value]) => ({ key, value, originalName: key }))
}

function toFields(rows: FieldRow[]): Record<string, string> {
  const result: Record<string, string> = {}
  rows.forEach((row) => {
    const key = row.key.trim()
    if (!key && !row.value.trim()) return
    if (!key) throw new Error('请填写字段名称，或删除空白行')
    if (key in result) throw new Error(`字段名称重复：${key}`)
    result[key] = row.value
  })
  return result
}

function updateRow(setRows: RowSetter, index: number, patch: Partial<FieldRow>) {
  setRows((rows) => rows.map((row, position) => (position === index ? { ...row, ...patch } : row)))
}

function appendRow(setRows: RowSetter) {
  setRows((rows) => [...rows, { key: '', value: '' }])
}

function removeRow(setRows: RowSetter, index: number) {
  setRows((rows) => rows.filter((_, position) => position !== index))
}

/** 来源分级标签：author=作者 / model=模型 / unknown=未标注（审稿按待核实处理）。 */
function SourceTag({ source }: { source: string }) {
  if (source === 'author') return <span className="tag tag--ok">作者提供</span>
  if (source === 'model') return <span className="tag tag--warn">模型整理</span>
  return (
    <span className="tag tag--danger" title="审稿按待核实处理">
      来源待确认
    </span>
  )
}

export default function Cards() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const navigate = useNavigate()
  const { push } = useToast()
  const [formVersion, setFormVersion] = useState(0)
  const action = useScopedAction(`${projectId}:${formVersion}`)

  const [data, setData] = useState<CardList | null>(null)
  const [category, setCategory] = useState('')
  const [query, setQuery] = useState('')
  const [highlightOnly, setHighlightOnly] = useState(false)
  const resource = useResourceRequest(`${projectId}:${category}:${query}:${highlightOnly}`)
  const loading = resource.loading
  const [selectedCard, setSelectedCard] = useState('')
  const [parseTask, setParseTask] = useState<CardParseTask | null>(null)
  const [evidenceCard, setEvidenceCard] = useState<CardItem | null>(null)
  const [editName, setEditName] = useState('')

  const [addOpen, setAddOpen] = useState(false)
  const [addCategory, setAddCategory] = useState('')
  const [addName, setAddName] = useState('')
  const [addRows, setAddRows] = useState<FieldRow[]>([{ key: '', value: '' }])

  const [generateOpen, setGenerateOpen] = useState(false)
  const [genCategory, setGenCategory] = useState('')
  const [genName, setGenName] = useState('')
  const [genHint, setGenHint] = useState('')
  const [genProposalId, setGenProposalId] = useState<number | null>(null)

  const [editCard, setEditCard] = useState<CardItem | null>(null)
  const [editRows, setEditRows] = useState<FieldRow[]>([])

  const [exportFormat, setExportFormat] = useState<'md' | 'json'>('md')
  const [exported, setExported] = useState<{ format: 'md' | 'json'; count: number; content: string } | null>(
    null,
  )
  const [chapterLine, setChapterLine] = useState<ChapterLine | null>(null)

  const load = useCallback(async () => {
    const token = resource.begin()
    try {
      const result = await listCards(projectId, {
          category: category || undefined,
          query: query || undefined,
          highlight_only: highlightOnly,
        })
      if (!resource.accept(token)) return
      setData(result)
      setParseTask(result.task)
      resource.finish(token)
    } catch (err) {
      resource.fail(token, errorMessage(err))

    }
  }, [projectId, category, query, highlightOnly, push])

  useEffect(() => {
    void load()
  }, [load])

  const activeTaskId = parseTask && ['queued', 'running'].includes(parseTask.status) ? parseTask.id : ''
  useEffect(() => {
    if (!activeTaskId) return
    let alive = true
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        const task = await getCardParseTask(projectId, activeTaskId)
        if (!alive) return
        setParseTask(task)
        if (['queued', 'running'].includes(task.status)) timer = setTimeout(() => void poll(), 1500)
        else {
          await load()
          window.dispatchEvent(new Event('aiw:cards-changed'))
          if (task.status === 'completed') push(`AI 解析完成：更新 ${task.parsed_files} 份，复用 ${task.cached_files} 份`, 'success')
        }
      } catch (err) {
        if (alive) { push(errorMessage(err), 'error'); timer = setTimeout(() => void poll(), 5000) }
      }
    }
    timer = setTimeout(() => void poll(), 750)
    return () => { alive = false; clearTimeout(timer) }
  }, [projectId, activeTaskId, load, push])

  useEffect(() => {
    const focus = () => void load()
    window.addEventListener('focus', focus)
    return () => window.removeEventListener('focus', focus)
  }, [load])

  useEffect(() => { setData(null); setEditCard(null); setSelectedCard(''); setEvidenceCard(null); setChapterLine(null); setParseTask(null); setAddOpen(false); setAddName(''); setAddRows([{ key: '', value: '' }]); setAddCategory(''); setGenerateOpen(false); setGenName(''); setGenHint(''); setGenCategory(''); setGenProposalId(null); setExported(null) }, [projectId])
  const currentCardId = data?.cards.find(card => card.entity_id === selectedCard)?.entity_id || data?.cards[0]?.entity_id || ''
  usePageChatContext({ projectId, activeFile: data?.cards.find(card => card.entity_id === currentCardId)?.file, files: data?.cards.map(card => card.file) || [], hasUnsavedChanges: !!editCard, onFilesChanged: () => load() })
  const lineResource = useResourceRequest(`${projectId}:${currentCardId}`)
  const [lineRequested, setLineRequested] = useState('')
  useEffect(() => { setLineRequested(''); setChapterLine(null) }, [projectId, currentCardId])
  const categories = data?.categories ?? []
  const countsByCategory = data?.counts_by_category ?? {}
  const totalCount = Object.values(countsByCategory).reduce((sum, value) => sum + value, 0)
  const palette = data?.palette && data.palette.length > 0 ? data.palette : DEFAULT_PALETTE

  const handleRefresh = async (force: boolean) => {
    const token = action.begin('parse')
    if (!token) return
    try {
      const task = await refreshCards(projectId, category || undefined, force)
      if (!action.accept(token)) return
      setParseTask(task)
      push(force ? '已开始 AI 强制重解析' : '已开始 AI 解析，未变化的材料会复用结果', 'success')
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally {
      action.finish(token)
    }
  }

  const handleHighlight = async (ref: string, color: string, mode?: 'auto' | 'manual' | 'off') => {
    const token = action.begin('highlight')
    if (!token) return
    try {
      await setCardHighlight(projectId, ref, color, mode)
      if (!action.accept(token)) return
      push(mode === 'auto' ? '已恢复自动配色' : color ? '已指派高亮色' : '已清除高亮', 'success')
      await load()
      window.dispatchEvent(new Event('aiw:cards-changed'))
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const handleChapterLine = async (card: CardItem) => {
    setLineRequested(card.ref)
    const token = lineResource.begin()
    try {
      const result = await cardChapterLine(projectId, card.ref)
      if (!lineResource.accept(token)) return
      lineResource.finish(token)
      setChapterLine({
        ref: card.ref,
        name: result.name,
        total: result.total,
        occurrences: result.occurrences,
      })
    } catch (err) {
      lineResource.fail(token, errorMessage(err))
    }
  }

  const openAdd = () => {
    if (action.pending) return
    setFormVersion(value => value + 1)
    setAddCategory(category || categories[0]?.key || '')
    setAddName('')
    setAddRows([{ key: '', value: '' }])
    setAddOpen(true)
  }

  const submitAdd = async () => {
    if (!addName.trim()) {
      push('请填写卡片名称', 'error')
      return
    }
    const token = action.begin('add')
    if (!token) return
    try {
      await addCard(projectId, {
        category: addCategory,
        name: addName.trim(),
        fields: toFields(addRows),
        via_proposal: true,
      })
      if (!action.accept(token)) return
      push('新增已进收件箱，请到收件箱确认后应用', 'success')
      setAddOpen(false)
      await load()
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const openGenerate = () => {
    if (action.pending) return
    setFormVersion(value => value + 1)
    setGenCategory(category || categories[0]?.key || '')
    setGenName('')
    setGenHint('')
    setGenProposalId(null)
    setGenerateOpen(true)
  }

  const submitGenerate = async () => {
    if (!genName.trim()) {
      push('请填写卡片名称', 'error')
      return
    }
    const token = action.begin('generate')
    if (!token) return
    try {
      const result = await generateCard(projectId, genCategory, genName.trim(), genHint.trim())
      if (!action.accept(token)) return
      setGenProposalId(result.proposal_id)
      push(`已进收件箱（提案 #${result.proposal_id}）`, 'success')
      setGenerateOpen(false)
      await load()
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const openEdit = (card: CardItem) => {
    if (action.pending) return
    setFormVersion(value => value + 1)
    setEditRows(toFieldRows(card.fields))
    setEditName(card.name)
    setEditCard(card)
  }

  const submitEdit = async () => {
    if (!editCard) return
    const token = action.begin('edit')
    if (!token) return
    try {
      toFields(editRows)
      const kept = new Set(editRows.flatMap(row => row.originalName ? [row.originalName] : []))
      const fieldEdits = editRows.filter(row => row.originalName).map(row => ({ original_name: row.originalName!, new_name: row.key.trim(), value: row.value }))
      const deleted = Object.keys(editCard.fields).filter(key => !kept.has(key)).map(key => ({ original_name: key, delete: true }))
      await updateCard(projectId, editCard.ref, toFields(editRows.filter(row => !row.originalName)), editName.trim(), { field_edits: [...fieldEdits, ...deleted], expected_hash: editCard.source_hash })
      if (!action.accept(token)) return
      push('已按原文依据保存，请点击 AI 解析更新视图', 'success')
      setEditCard(null)
      await load()
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  // 删除先进收件箱、可回退：直接执行 + Toast 引导去收件箱，不再弹确认框
  const handleDelete = async (card: CardItem) => {
    const token = action.begin('delete')
    if (!token) return
    try {
      await deleteCard(projectId, card.ref)
      if (!action.accept(token)) return
      push(`「${card.name}」的删除已进收件箱，确认后才写回 Markdown`, 'success', {
        label: '去收件箱',
        onAction: () => navigate('/inbox'),
      })
      await load()
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const handleExport = async () => {
    const token = action.begin('export')
    if (!token) return
    try {
      const result = await exportCards(projectId, category || undefined, exportFormat)
      if (!action.accept(token)) return
      setExported({ format: exportFormat, count: result.count, content: result.content })
      push(`已导出 ${result.count} 条卡片`, 'success')
    } catch (err) {
      if (action.accept(token)) push(errorMessage(err), 'error')
    } finally { action.finish(token) }
  }

  const handleCopy = () => {
    if (!exported) return
    try {
      copyTextToClipboard(exported.content)
      push('已复制到剪贴板', 'success')
    } catch {
      push('复制失败，请手动选择文本复制', 'error')
    }
  }

  const handleCancelParse = async () => {
    if (!parseTask) return
    const token = action.begin('cancel-parse')
    if (!token) return
    try {
      const result = await cancelCardParseTask(projectId, parseTask.id)
      if (action.accept(token)) setParseTask(result)
    } catch (err) { if (action.accept(token)) push(errorMessage(err), 'error') }
    finally { action.finish(token) }
  }
  const closeAdd = () => { if (!action.pending) { setAddOpen(false); setFormVersion(value => value + 1) } }
  const closeGenerate = () => { if (!action.pending) { setGenerateOpen(false); setFormVersion(value => value + 1) } }
  const closeEdit = () => { if (!action.pending) { setEditCard(null); setFormVersion(value => value + 1) } }

  return (
    <WorkspacePage className="stack">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">设定卡片</h1>
          <p className="page-header__desc">{data ? `${data.project_name} · ` : ''}设定卡片的浏览、高亮与导出。</p>
        </div>
      </header>

      <nav className="tabs" aria-label="设定分类">
        <button
          className={category === '' ? 'tab is-active' : 'tab'}
          type="button"
          disabled={!!editCard} onClick={() => setCategory('')}
        >
          全部（{totalCount}）
        </button>
        {categories.map((item) => (
          <button
            key={item.key}
            className={category === item.key ? 'tab is-active' : 'tab'}
            type="button"
            disabled={!!editCard} onClick={() => setCategory(item.key)}
          >
            {item.label}（{countsByCategory[item.key] ?? 0}）
          </button>
        ))}
      </nav>

      <div className="row row--between">
        <span className="muted">
          共 {data?.count ?? 0} 条卡片{loading ? '（加载中…）' : ''}
        </span>
        <label className="field">
          <span className="field__label">搜索</span>
          <input
            autoComplete={NO_AUTOFILL}
            className="input"
            disabled={!!editCard}
            value={query}
            placeholder="搜索名称/标签/内容"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>

      <div className="btn-row">
        <button className="btn btn--sm" type="button" disabled={!!action.pending || !!activeTaskId} onClick={() => void handleRefresh(false)}>
          AI 解析
        </button>
        <button className="btn btn--sm" type="button" disabled={!!action.pending || !!activeTaskId} onClick={() => void handleRefresh(true)}>
          强制重解析
        </button>
        <button className="btn btn--primary btn--sm" type="button" disabled={!!action.pending} onClick={openAdd}>
          添加卡片
        </button>
        <button className="btn btn--sm" type="button" disabled={!!action.pending} onClick={openGenerate}>
          智能生成
        </button>
        <span className="row">
          <button
            className={exportFormat === 'md' ? 'btn btn--primary btn--sm' : 'btn btn--sm'}
            type="button"
            aria-pressed={exportFormat === 'md'}
            disabled={!!action.pending}
            onClick={() => setExportFormat('md')}
          >
            md
          </button>
          <button
            className={exportFormat === 'json' ? 'btn btn--primary btn--sm' : 'btn btn--sm'}
            type="button"
            aria-pressed={exportFormat === 'json'}
            disabled={!!action.pending}
            onClick={() => setExportFormat('json')}
          >
            json
          </button>
          <button className="btn btn--sm" type="button" disabled={!!action.pending} onClick={() => void handleExport()}>
            导出
          </button>
        </span>
        <label className="checkbox">
          <input
            type="checkbox"
            disabled={!!editCard}
            checked={highlightOnly}
            onChange={(event) => setHighlightOnly(event.target.checked)}
          />
          仅显示高亮
        </label>
        <button
          className="btn btn--sm"
          type="button"
          onClick={() => navigate(`/project/${projectId}/knowledge`)}
        >
          关系图谱
        </button>
      </div>

      <section className="cards-parse-state" aria-live="polite">
        {activeTaskId ? <>
          <span>AI 解析中 · {parseTask?.files_completed} / {parseTask?.files_total} 份材料{parseTask?.current_file ? ` · ${parseTask.current_file}` : ''}</span>
          <button className="btn btn--ghost btn--sm" type="button" disabled={!!action.pending} onClick={() => void handleCancelParse()}>取消解析</button>
        </> : <span>{data?.parse_status === 'pending' ? `有 ${data.stale_files.length} 份材料待更新。` : data?.parse_status === 'completed' ? '已显示缓存中的 AI 解析结果。' : '点击「AI 解析」从原材料整理对象与字段。'} 仅手动解析调用模型，保存和生成后不会自动调用。</span>}
        {parseTask?.error ? <p className="cards-parse-error">{parseTask.error}</p> : null}
        {parseTask?.failures?.map(failure => <p className="cards-parse-error" key={failure.file}>{failure.file}：{failure.error}</p>)}
      </section>

      {exported ? (
        <section className="panel">
          <div className="panel__header">
            <h2 className="panel__title">
              导出结果（{exported.format} · {exported.count} 条）
            </h2>
            <div className="row">
              <button className="btn btn--sm" type="button" onClick={() => void handleCopy()}>
                复制
              </button>
              <button className="btn btn--ghost btn--sm" type="button" onClick={() => setExported(null)}>
                收起
              </button>
            </div>
          </div>
          <div className="panel__body">
            <textarea autoComplete={NO_AUTOFILL} className="textarea textarea--mono" readOnly rows={12} value={exported.content} />
          </div>
        </section>
      ) : null}

      {generateOpen ? (
        <section className="panel">
          <div className="panel__header">
            <h2 className="panel__title">智能生成卡片</h2>
            <button className="btn btn--ghost btn--sm" type="button" disabled={!!action.pending} onClick={closeGenerate}>
              收起
            </button>
          </div>
          <fieldset className="panel__body stack cards-edit-form" disabled={!!action.pending}>
            <label className="field">
              <span className="field__label">分类</span>
              <select
                className="select"
                value={genCategory}
                onChange={(event) => setGenCategory(event.target.value)}
              >
                {categories.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field__label">卡片名称</span>
              <input
                autoComplete={NO_AUTOFILL}
                className="input"
                value={genName}
                placeholder="例如：黑风寨"
                onChange={(event) => setGenName(event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field__label">补充线索</span>
              <textarea
                autoComplete={NO_AUTOFILL}
                className="textarea"
                value={genHint}
                placeholder="可选：希望模型补全的要点"
                onChange={(event) => setGenHint(event.target.value)}
              />
            </label>
            {genProposalId !== null ? (
              <p className="muted">已进收件箱：提案 #{genProposalId}，请在收件箱确认后应用。</p>
            ) : null}
            <div className="btn-row">
              <button className="btn btn--primary btn--sm" type="button" onClick={() => void submitGenerate()}>
                生成到收件箱
              </button>
              <button className="btn btn--ghost btn--sm" type="button" onClick={closeGenerate}>
                取消
              </button>
            </div>
          </fieldset>
        </section>
      ) : null}

      <ResourceState {...resource} hasData={resource.loaded && !!data} onRetry={() => void load()}>
      {data && data.count > 0 ? (
        <div className="workspace-collection">
          <nav className="workspace-collection-nav" aria-label="设定对象">{data.cards.map(card => <button key={card.entity_id} className={card.entity_id === currentCardId ? 'is-active' : ''} aria-pressed={card.entity_id === currentCardId} disabled={!!editCard && card.ref !== editCard.ref} onClick={() => setSelectedCard(card.entity_id)}>{card.name}<small>{card.category} · {card.stale ? '待更新' : card.source === 'author' ? '作者来源' : '整理结果'}</small></button>)}</nav>
          <div className="workspace-collection-detail">
          {data.cards.filter(card => card.entity_id === currentCardId).map((card) => (
            <article className="asset-card cards-object" key={card.entity_id} style={{
              '--card-light': card.highlight_colors.light || 'var(--color-border)',
              '--card-dark': card.highlight_colors.dark || 'var(--color-border)',
              '--card-paper': card.highlight_colors.paper || 'var(--color-border)',
            } as CSSProperties}>
              {editCard?.ref === card.ref ? (
                <fieldset className="stack cards-edit-form" disabled={!!action.pending}>
                  <span className="field__hint">编辑「{card.name}」：保存会以 diff 写回 Markdown。</span>
                  {editRows.some(row => row.originalName && (!editCard.field_meta[row.originalName]?.capabilities?.rename || !editCard.field_meta[row.originalName]?.capabilities?.delete)) ? <p className="field__hint">部分字段来自共用表头或自由段落，改名、删除需编辑原材料。<button className="btn btn--ghost btn--sm" type="button" onClick={() => navigate(`/project/${projectId}/editor?${new URLSearchParams({path: card.file})}`)}>定位原材料</button></p> : null}
                  <label className="field"><span className="field__label">对象名称</span><input className="input" value={editName} onChange={event => setEditName(event.target.value)} /></label>
                  {editRows.length === 0 ? (
                    <p className="muted">该卡片暂无字段，可新增一行字段。</p>
                  ) : (
                    editRows.map((row, index) => (
                      <div className="row" key={index}>
                        <input
                          autoComplete={NO_AUTOFILL}
                          className="input"
                          value={row.key}
                          disabled={!!row.originalName && !editCard.field_meta[row.originalName]?.capabilities?.rename}
                          title={row.originalName ? editCard.field_meta[row.originalName]?.capabilities?.reason : undefined}
                          placeholder="字段名"
                          onChange={(event) => updateRow(setEditRows, index, { key: event.target.value })}
                        />
                        <input
                          autoComplete={NO_AUTOFILL}
                          className="input"
                          value={row.value}
                          placeholder="字段值"
                          onChange={(event) => updateRow(setEditRows, index, { value: event.target.value })}
                        />
                        <button className="btn btn--ghost btn--sm" type="button" disabled={!!row.originalName && !editCard.field_meta[row.originalName]?.capabilities?.delete} title={row.originalName ? editCard.field_meta[row.originalName]?.capabilities?.reason : undefined} onClick={() => removeRow(setEditRows, index)}>
                          删除
                        </button>
                      </div>
                    ))
                  )}
                  <button className="btn btn--sm" type="button" onClick={() => appendRow(setEditRows)}>
                    增加一行
                  </button>
                  <div className="row">
                    <button className="btn btn--primary btn--sm" type="button" onClick={() => void submitEdit()}>
                      保存
                    </button>
                    <button className="btn btn--ghost btn--sm" type="button" onClick={closeEdit}>
                      取消
                    </button>
                  </div>
                </fieldset>
              ) : (
                <>
                  <div className="asset-card__head">
                    <span className="asset-card__name">{card.name}</span>
                    <SourceTag source={card.source} />
                  </div>
                  {card.stale ? <p className="cards-stale">原材料已变化，待 AI 解析更新；原文编辑与删除暂不可用。</p> : null}
                  {card.aliases.length ? <p className="muted">别名：{card.aliases.join('、')}</p> : null}
                  <div className="chips">
                    <span className="chip">{card.category}</span>
                    {card.field_list.length > 0 ? (
                      card.field_list.map((field) => (
                        <span className="chip" key={field}>
                          {field}
                        </span>
                      ))
                    ) : (
                      <span className="chip">无字段</span>
                    )}
                  </div>
                  {card.summary ? <p className="asset-card__summary">{card.summary}</p> : null}
                  <span className="cards-color-mode">{card.highlight_mode === 'auto' ? '自动配色' : card.highlight_mode === 'manual' ? '手动配色' : '高亮已关闭'}</span>

                  <div className="row">
                    <span className="hl-picker">
                      {palette.map((color) => (
                        <button
                          key={color}
                          className="hl-dot"
                          type="button"
                          data-color={color}
                          disabled={!!action.pending || data.cards.some(other => other.entity_id !== card.entity_id && other.highlight === color)}
                          aria-label={`高亮 ${color}`}
                          aria-pressed={card.highlight === color}
                          onClick={() => void handleHighlight(card.ref, color)}
                        />
                      ))}
                    </span>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      disabled={!!action.pending}
                      onClick={() => void handleHighlight(card.ref, '')}
                    >
                      清除高亮
                    </button>
                    <button className="btn btn--ghost btn--sm" type="button" disabled={!!action.pending || card.highlight_mode === 'auto'} onClick={() => void handleHighlight(card.ref, '', 'auto')}>恢复自动配色</button>
                  </div>

                  <div className="row">
                    <button className="btn btn--sm" type="button" onClick={() => void handleChapterLine(card)}>
                      定位章节线
                    </button>
                    <button className="btn btn--sm" type="button" disabled={!!action.pending || card.stale} onClick={() => openEdit(card)}>
                      编辑
                    </button>
                    <button className="btn btn--sm" type="button" onClick={() => setEvidenceCard(card)}>原文依据</button>
                    <button className="btn btn--danger btn--sm" type="button" disabled={!!action.pending || card.stale || !card.extent} onClick={() => void handleDelete(card)}>
                      删除
                    </button>
                  </div>
                </>
              )}

              {lineRequested === card.ref && <ResourceState {...lineResource} hasData={!!chapterLine && chapterLine.ref === card.ref && lineResource.loaded} onRetry={() => void handleChapterLine(card)}>
              {chapterLine && chapterLine.ref === card.ref ? (
                <div className="panel">
                  <div className="panel__header">
                    <h3 className="panel__title">章节线：出现 {chapterLine.total} 章</h3>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      onClick={() => { setChapterLine(null); setLineRequested('') }}
                    >
                      收起
                    </button>
                  </div>
                  <div className="panel__body panel__body--tight stack stack--tight">
                    {chapterLine.occurrences.length === 0 ? (
                      <p className="muted">各章正文中未检索到该名称。</p>
                    ) : (
                      chapterLine.occurrences.map((entry) => (
                        <div className="row row--between" key={entry.rel_path}>
                          <button
                            className="btn btn--ghost btn--sm"
                            type="button"
                            onClick={() => navigate(`/project/${projectId}/editor?${new URLSearchParams({path: entry.rel_path})}`)}
                          >
                            第 {entry.number} 章
                          </button>
                          <span className="muted">{entry.count} 次</span>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              ) : null}</ResourceState>}
            </article>
          ))}
          </div>
        </div>
      ) : (
        <div className="empty-state">
          <p className="empty-state__title">{query || category || highlightOnly ? '没有匹配的设定卡片' : '暂无设定卡片'}</p>
          <p className="empty-state__desc">点击「AI 解析」整理已有材料，或添加卡片后到收件箱应用。</p>
        </div>
      )}

      </ResourceState>
      <Modal title={evidenceCard ? `${evidenceCard.name} · 原文依据` : '原文依据'} open={!!evidenceCard} onClose={() => setEvidenceCard(null)}>
        {evidenceCard ? <div className="stack">
          <p className="muted">{evidenceCard.file} · 第 {evidenceCard.evidence.line_start} 行{evidenceCard.stale ? ' · 依据已过期，请重新解析' : ''}</p>
          <blockquote className="cards-evidence">{evidenceCard.evidence.quote}</blockquote>
          <button className="btn btn--sm" type="button" onClick={() => navigate(`/project/${projectId}/editor?${new URLSearchParams({ path: evidenceCard.file, line: String(evidenceCard.evidence.line_start), end_line: String(evidenceCard.evidence.line_end), hash: evidenceCard.source_hash })}`)}>定位原材料</button>
          {Object.entries(evidenceCard.fields).map(([key, value]) => <section key={key} className="cards-field-evidence">
            <div className="row"><strong>{key}</strong><SourceTag source={evidenceCard.field_meta[key]?.source || 'unknown'} /></div>
            <p>{value}</p><blockquote className="cards-evidence">{evidenceCard.field_meta[key]?.evidence.quote}</blockquote>
          </section>)}
        </div> : null}
      </Modal>

      <Modal
        title="添加设定卡片"
        open={addOpen}
        onClose={closeAdd}
        footer={
          <>
            <button className="btn btn--ghost btn--sm" type="button" disabled={!!action.pending} onClick={closeAdd}>
              取消
            </button>
            <button className="btn btn--primary btn--sm" type="button" disabled={!!action.pending} onClick={() => void submitAdd()}>
              提交到收件箱
            </button>
          </>
        }
      >
        <fieldset className="stack cards-edit-form" disabled={!!action.pending}>
          <label className="field">
            <span className="field__label">分类</span>
            <select
              className="select"
              value={addCategory}
              onChange={(event) => setAddCategory(event.target.value)}
            >
              {categories.map((item) => (
                <option key={item.key} value={item.key}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field__label">卡片名称</span>
            <input
              autoComplete={NO_AUTOFILL}
              className="input"
              value={addName}
              placeholder="例如：断崖镇"
              onChange={(event) => setAddName(event.target.value)}
            />
          </label>
          <div className="stack stack--tight">
            <span className="field__label">字段</span>
            {addRows.map((row, index) => (
              <div className="row" key={index}>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={row.key}
                  placeholder="字段名"
                  onChange={(event) => updateRow(setAddRows, index, { key: event.target.value })}
                />
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={row.value}
                  placeholder="字段值"
                  onChange={(event) => updateRow(setAddRows, index, { value: event.target.value })}
                />
                <button className="btn btn--ghost btn--sm" type="button" onClick={() => removeRow(setAddRows, index)}>
                  删除
                </button>
              </div>
            ))}
            <button className="btn btn--sm" type="button" onClick={() => appendRow(setAddRows)}>
              增加一行
            </button>
          </div>
          <span className="field__hint">提交后需在收件箱确认才会写入。</span>
        </fieldset>
      </Modal>
    </WorkspacePage>
  )
}
