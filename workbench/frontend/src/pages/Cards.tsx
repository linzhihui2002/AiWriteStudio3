/**
 * 设定卡片管理页（Task 27a）—— 卡片视图 / 高亮色 / 双向同步 / 导出。
 *
 * 事实源仍是 ``设定/*.md``：卡片只是视图层；新增/编辑/删除一律经收件箱或 diff 写回。
 * 高亮色只存 ``.meta/asset_cards.json``，色板走 token 名（主题映射实际色值）。
 */

import { useCallback, useEffect, useState, type Dispatch, type SetStateAction } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import {
  addCard,
  cardChapterLine,
  deleteCard,
  exportCards,
  generateCard,
  listCards,
  refreshCards,
  setCardHighlight,
  updateCard,
} from '../api/client'
import type { CardItem, CardList } from '../api/types'
import Modal from '../components/Modal'
import { useConfirm } from '../components/ConfirmDialog'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

interface FieldRow {
  key: string
  value: string
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
  return Object.entries(fields).map(([key, value]) => ({ key, value }))
}

function toFields(rows: FieldRow[]): Record<string, string> {
  const result: Record<string, string> = {}
  rows.forEach((row) => {
    const key = row.key.trim()
    if (key) result[key] = row.value
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
  if (source === 'author') return <span className="tag tag--ok">作者</span>
  if (source === 'model') return <span className="tag tag--warn">模型</span>
  return (
    <span className="tag tag--danger" title="审稿按待核实处理">
      来源未标注
    </span>
  )
}

export default function Cards() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const navigate = useNavigate()
  const { push } = useToast()

  const [data, setData] = useState<CardList | null>(null)
  const [category, setCategory] = useState('')
  const [query, setQuery] = useState('')
  const [highlightOnly, setHighlightOnly] = useState(false)
  const [loading, setLoading] = useState(false)

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

  const [relationOpen, setRelationOpen] = useState(false)
  const [exportFormat, setExportFormat] = useState<'md' | 'json'>('md')
  const [exported, setExported] = useState<{ format: 'md' | 'json'; count: number; content: string } | null>(
    null,
  )
  const [chapterLine, setChapterLine] = useState<ChapterLine | null>(null)
  const [confirm, confirmNode] = useConfirm()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(
        await listCards(projectId, {
          category: category || undefined,
          query: query || undefined,
          highlight_only: highlightOnly,
        }),
      )
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setLoading(false)
    }
  }, [projectId, category, query, highlightOnly, push])

  useEffect(() => {
    void load()
  }, [load])

  const categories = data?.categories ?? []
  const countsByCategory = data?.counts_by_category ?? {}
  const totalCount = Object.values(countsByCategory).reduce((sum, value) => sum + value, 0)
  const palette = data?.palette && data.palette.length > 0 ? data.palette : DEFAULT_PALETTE

  const handleRefresh = async (force: boolean) => {
    try {
      await refreshCards(projectId, category || undefined, force)
      push(force ? '已强制重解析设定卡片' : '已刷新卡片解析缓存', 'success')
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleHighlight = async (ref: string, color: string) => {
    try {
      await setCardHighlight(projectId, ref, color)
      push(color ? '已指派高亮色' : '已清除高亮', 'success')
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleChapterLine = async (card: CardItem) => {
    try {
      const result = await cardChapterLine(projectId, card.ref)
      setChapterLine({
        ref: card.ref,
        name: result.name,
        total: result.total,
        occurrences: result.occurrences,
      })
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const openAdd = () => {
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
    try {
      await addCard(projectId, {
        category: addCategory,
        name: addName.trim(),
        fields: toFields(addRows),
        via_proposal: true,
      })
      push('新增已进收件箱，请到收件箱确认后应用', 'success')
      setAddOpen(false)
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const openGenerate = () => {
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
    try {
      const result = await generateCard(projectId, genCategory, genName.trim(), genHint.trim())
      setGenProposalId(result.proposal_id)
      push(`已进收件箱（提案 #${result.proposal_id}）`, 'success')
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const openEdit = (card: CardItem) => {
    setEditRows(toFieldRows(card.fields))
    setEditCard(card)
  }

  const submitEdit = async () => {
    if (!editCard) return
    try {
      await updateCard(projectId, editCard.ref, toFields(editRows))
      push('已写回 Markdown（以 diff 形式）', 'success')
      setEditCard(null)
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleDelete = async (card: CardItem) => {
    const confirmed = await confirm({
      title: '删除卡片',
      message: `确认删除卡片「${card.name}」？删除会先进入收件箱，确认后才会写回 Markdown。`,
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    try {
      await deleteCard(projectId, card.ref)
      push('删除已进收件箱，请在收件箱确认后生效', 'success')
      await load()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleExport = async () => {
    try {
      const result = await exportCards(projectId, category || undefined, exportFormat)
      setExported({ format: exportFormat, count: result.count, content: result.content })
      push(`已导出 ${result.count} 条卡片`, 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleCopy = async () => {
    if (!exported) return
    try {
      await navigator.clipboard.writeText(exported.content)
      push('已复制到剪贴板', 'success')
    } catch {
      push('复制失败，请手动选择文本复制', 'error')
    }
  }

  return (
    <div className="page page--wide stack">
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
          onClick={() => setCategory('')}
        >
          全部（{totalCount}）
        </button>
        {categories.map((item) => (
          <button
            key={item.key}
            className={category === item.key ? 'tab is-active' : 'tab'}
            type="button"
            onClick={() => setCategory(item.key)}
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
            value={query}
            placeholder="搜索名称/标签/内容"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>

      <div className="btn-row">
        <button className="btn btn--sm" type="button" onClick={() => void handleRefresh(false)}>
          刷新解析
        </button>
        <button className="btn btn--sm" type="button" onClick={() => void handleRefresh(true)}>
          强制重解析
        </button>
        <button className="btn btn--primary btn--sm" type="button" onClick={openAdd}>
          添加卡片
        </button>
        <button className="btn btn--sm" type="button" onClick={openGenerate}>
          智能生成
        </button>
        <span className="row">
          <button
            className={exportFormat === 'md' ? 'btn btn--primary btn--sm' : 'btn btn--sm'}
            type="button"
            aria-pressed={exportFormat === 'md'}
            onClick={() => setExportFormat('md')}
          >
            md
          </button>
          <button
            className={exportFormat === 'json' ? 'btn btn--primary btn--sm' : 'btn btn--sm'}
            type="button"
            aria-pressed={exportFormat === 'json'}
            onClick={() => setExportFormat('json')}
          >
            json
          </button>
          <button className="btn btn--sm" type="button" onClick={() => void handleExport()}>
            导出
          </button>
        </span>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={highlightOnly}
            onChange={(event) => setHighlightOnly(event.target.checked)}
          />
          仅显示高亮
        </label>
        <button
          className="btn btn--sm"
          type="button"
          aria-pressed={relationOpen}
          onClick={() => setRelationOpen((open) => !open)}
        >
          关系图谱
        </button>
      </div>

      {relationOpen ? (
        <section className="panel">
          <div className="panel__header">
            <h2 className="panel__title">关系图谱</h2>
            <span className="tag tag--warn">未实装</span>
          </div>
          <div className="panel__body">
            <p className="muted">关系图谱尚未实装。</p>
          </div>
        </section>
      ) : null}

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

      {data && data.count > 0 ? (
        <div className="card-grid">
          {data.cards.map((card) => (
            <article className="asset-card" key={card.ref} data-highlight={card.highlight || undefined}>
              <div className="asset-card__head">
                <span className="asset-card__name">{card.name}</span>
                <SourceTag source={card.source} />
              </div>
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

              <div className="row">
                <span className="hl-picker">
                  {palette.map((color) => (
                    <button
                      key={color}
                      className="hl-dot"
                      type="button"
                      data-color={color}
                      aria-label={`高亮 ${color}`}
                      aria-pressed={card.highlight === color}
                      onClick={() => void handleHighlight(card.ref, color)}
                    />
                  ))}
                </span>
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  onClick={() => void handleHighlight(card.ref, '')}
                >
                  清除高亮
                </button>
              </div>

              <div className="row">
                <button className="btn btn--sm" type="button" onClick={() => void handleChapterLine(card)}>
                  定位章节线
                </button>
                <button className="btn btn--sm" type="button" onClick={() => openEdit(card)}>
                  编辑
                </button>
                <button className="btn btn--danger btn--sm" type="button" onClick={() => void handleDelete(card)}>
                  删除
                </button>
              </div>

              {chapterLine && chapterLine.ref === card.ref ? (
                <div className="panel">
                  <div className="panel__header">
                    <h3 className="panel__title">章节线：出现 {chapterLine.total} 章</h3>
                    <button
                      className="btn btn--ghost btn--sm"
                      type="button"
                      onClick={() => setChapterLine(null)}
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
                            onClick={() => navigate(`/project/${projectId}/editor`)}
                          >
                            第 {entry.number} 章
                          </button>
                          <span className="muted">{entry.count} 次</span>
                        </div>
                      ))
                    )}
                  </div>
                </div>
              ) : null}
            </article>
          ))}
        </div>
      ) : (
        <div className="empty-state">
          <p className="empty-state__title">暂无设定卡片</p>
          <p className="empty-state__desc">还没有设定卡片，可用「添加卡片」新增。</p>
        </div>
      )}

      <Modal
        title="添加设定卡片"
        open={addOpen}
        onClose={() => setAddOpen(false)}
        footer={
          <>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setAddOpen(false)}>
              取消
            </button>
            <button className="btn btn--primary btn--sm" type="button" onClick={() => void submitAdd()}>
              提交到收件箱
            </button>
          </>
        }
      >
        <div className="stack">
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
        </div>
      </Modal>

      <Modal
        title="智能生成卡片"
        open={generateOpen}
        onClose={() => setGenerateOpen(false)}
        footer={
          <>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setGenerateOpen(false)}>
              关闭
            </button>
            <button className="btn btn--primary btn--sm" type="button" onClick={() => void submitGenerate()}>
              生成到收件箱
            </button>
          </>
        }
      >
        <div className="stack">
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
        </div>
      </Modal>

      <Modal
        title={editCard ? `编辑卡片：${editCard.name}` : '编辑卡片'}
        open={editCard !== null}
        onClose={() => setEditCard(null)}
        footer={
          <>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setEditCard(null)}>
              取消
            </button>
            <button className="btn btn--primary btn--sm" type="button" onClick={() => void submitEdit()}>
              保存
            </button>
          </>
        }
      >
        <div className="stack">
          <span className="field__hint">保存会以 diff 写回 Markdown。</span>
          {editRows.length === 0 ? (
            <p className="muted">该卡片暂无字段，可新增一行字段。</p>
          ) : (
            editRows.map((row, index) => (
              <div className="row" key={index}>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={row.key}
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
                <button className="btn btn--ghost btn--sm" type="button" onClick={() => removeRow(setEditRows, index)}>
                  删除
                </button>
              </div>
            ))
          )}
          <button className="btn btn--sm" type="button" onClick={() => appendRow(setEditRows)}>
            增加一行
          </button>
        </div>
      </Modal>

      {confirmNode}
    </div>
  )
}