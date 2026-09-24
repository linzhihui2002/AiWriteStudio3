/** 书架：项目列表 / 开书（含模板选择）/ 归档 / 进入工作区 / 回收站 / 导入导出。 */

import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  archiveProject,
  buildPublishExport,
  createProject,
  deleteCover,
  deleteTemplate,
  exportChapters,
  exportPackage,
  importDocument,
  listProjects,
  listPublishPlatforms,
  listTemplates,
  listTrash,
  restorePackage,
  restoreTrash,
  saveAsTemplate,
  updateProject,
  uploadCover,
} from '../api/client'
import type { PublishPlatform } from '../api/client'
import type { Project, Template, TrashEntry } from '../api/types'
import Modal from '../components/Modal'
import { usePrompt } from '../components/PromptDialog'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

export default function Bookshelf() {
  const navigate = useNavigate()
  const toast = useToast()
  const [projects, setProjects] = useState<Project[]>([])
  const [includeArchived, setIncludeArchived] = useState(false)
  const [templates, setTemplates] = useState<Template[]>([])
  const [trash, setTrash] = useState<TrashEntry[]>([])
  const [busy, setBusy] = useState(false)
  const [prompt, promptNode] = usePrompt()

  const [createOpen, setCreateOpen] = useState(false)
  const [form, setForm] = useState({
    name: '',
    genre: '',
    platform: '',
    protagonist: '',
    one_liner: '',
    template: '',
  })

  const [importTarget, setImportTarget] = useState<Project | null>(null)
  const [importText, setImportText] = useState('')
  const [importName, setImportName] = useState('')

  // 发布导出（Task 66）：平台模板 + 提交前自检
  const [platforms, setPlatforms] = useState<PublishPlatform[]>([])
  const [publishTarget, setPublishTarget] = useState<Project | null>(null)
  const [platform, setPlatform] = useState('通用')
  const [onlyCompleted, setOnlyCompleted] = useState(false)
  const [publishResult, setPublishResult] = useState<Awaited<
    ReturnType<typeof buildPublishExport>
  > | null>(null)

  // 编辑信息（含封面）：改书名 = 重命名磁盘目录并同步索引与快照
  const [editTarget, setEditTarget] = useState<Project | null>(null)
  const [editForm, setEditForm] = useState({
    name: '',
    genre: '',
    platform: '',
    protagonist: '',
    one_liner: '',
  })
  const [coverBust, setCoverBust] = useState(0)

  const reload = useCallback(async () => {
    try {
      const [items, tpl, trashItems] = await Promise.all([
        listProjects(includeArchived),
        listTemplates(),
        listTrash(0).catch(() => [] as TrashEntry[]),
      ])
      setProjects(items)
      setTemplates(tpl)
      // 全局回收站接口按项目过滤；此处仅展示当前列表能拿到的条目
      setTrash(trashItems)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }, [includeArchived, toast])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void listPublishPlatforms()
      .then(setPlatforms)
      .catch(() => setPlatforms([]))
  }, [])

  const onOpenPublish = (project: Project) => {
    setPublishTarget(project)
    setPublishResult(null)
    setPlatform('通用')
    setOnlyCompleted(false)
  }

  const onBuildPublish = async () => {
    if (!publishTarget) return
    setBusy(true)
    try {
      const result = await buildPublishExport(publishTarget.id, platform, onlyCompleted)
      setPublishResult(result)
      toast.push(
        `已整理 ${result.stats.chapters} 章 · ${result.stats.words} 字` +
          (result.warnings.length ? `（${result.warnings.length} 条提醒）` : ''),
        'success',
      )
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onDownloadPublish = () => {
    if (!publishResult) return
    const blob = new Blob([publishResult.content], {
      type: 'text/plain;charset=utf-8',
    })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = publishResult.filename
    link.click()
    URL.revokeObjectURL(url)
  }

  const onCreate = async () => {
    if (!form.name.trim()) {
      toast.push('请填写书名', 'error')
      return
    }
    setBusy(true)
    try {
      const project = await createProject({
        name: form.name.trim(),
        genre: form.genre,
        platform: form.platform,
        protagonist: form.protagonist,
        one_liner: form.one_liner,
        template: form.template || null,
      })
      toast.push(`已创建《${project.name}》，工作区已生成`, 'success')
      setCreateOpen(false)
      setForm({ name: '', genre: '', platform: '', protagonist: '', one_liner: '', template: '' })
      await reload()
      navigate(`/project/${project.id}/editor`)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onArchive = async (project: Project) => {
    try {
      await archiveProject(project.id, !project.archived)
      toast.push(project.archived ? '已取消归档' : '已归档', 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  // ── 编辑信息（含封面）──
  const onOpenEdit = (project: Project) => {
    setEditTarget(project)
    setEditForm({
      name: project.name,
      genre: project.genre ?? '',
      platform: project.platform ?? '',
      protagonist: project.protagonist ?? '',
      one_liner: project.one_liner ?? '',
    })
  }

  const onSaveEdit = async () => {
    if (!editTarget) return
    if (!editForm.name.trim()) {
      toast.push('书名不能为空', 'error')
      return
    }
    setBusy(true)
    try {
      const updated = await updateProject(editTarget.id, {
        name: editForm.name.trim(),
        genre: editForm.genre,
        platform: editForm.platform,
        protagonist: editForm.protagonist,
        one_liner: editForm.one_liner,
      })
      toast.push(
        updated.name !== editTarget.name
          ? `已改名《${updated.name}》，磁盘目录与快照已同步（旧页面需刷新）`
          : '已保存',
        'success',
      )
      setEditTarget(null)
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onUploadCover = async (file: File) => {
    if (!editTarget) return
    setBusy(true)
    try {
      await uploadCover(editTarget.id, file)
      toast.push('封面已更新', 'success')
      setCoverBust((value) => value + 1)
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onRemoveCover = async () => {
    if (!editTarget) return
    try {
      await deleteCover(editTarget.id)
      toast.push('封面已移除', 'success')
      setCoverBust((value) => value + 1)
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onExport = async (project: Project) => {
    try {
      const result = await exportChapters(project.id, 'with_title', 'md')
      const blob = new Blob([result.content], { type: 'text/markdown;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = result.filename
      link.click()
      URL.revokeObjectURL(url)
      toast.push(`已导出 ${result.chapter_count} 章 · ${result.word_count} 字`, 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onBackup = async (project: Project) => {
    try {
      const payload = await exportPackage(project.id)
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: 'application/json;charset=utf-8',
      })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${project.name}-项目包.json`
      link.click()
      URL.revokeObjectURL(url)
      toast.push('项目包已导出（不含任何凭据）', 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onRestorePackage = async (file: File) => {
    try {
      const text = await file.text()
      const payload = JSON.parse(text) as Record<string, unknown>
      const name = await prompt({
        title: '恢复项目包',
        label: '书名（留空则用包内原名）',
        allowEmpty: true,
        confirmText: '恢复',
      })
      if (name === null) return
      const result = await restorePackage(payload, name || undefined)
      const project = result.project as Project | undefined
      toast.push(`已恢复项目《${project?.name ?? name}》`, 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onSaveAsTemplate = async (project: Project) => {
    const name = await prompt({
      title: '另存为模板',
      label: '模板名称',
      placeholder: '例如：三幕式骨架',
      confirmText: '保存模板',
    })
    if (!name) return
    try {
      await saveAsTemplate(name.trim(), project.id)
      toast.push(`已另存为模板「${name.trim()}」`, 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onDeleteTemplate = async (template: Template) => {
    try {
      await deleteTemplate(template.name)
      toast.push('模板已删除', 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onImport = async () => {
    if (!importTarget) return
    if (!importText.trim()) {
      toast.push('导入内容为空', 'error')
      return
    }
    setBusy(true)
    try {
      const result = await importDocument(importTarget.id, {
        filename: importName || '导入片段.md',
        content: importText,
        as_single_chapter: true,
      })
      toast.push(`已导入 ${result.count} 章 · ${result.word_count} 字（草稿）`, 'success')
      setImportTarget(null)
      setImportText('')
      setImportName('')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onRestoreTrash = async (entry: TrashEntry) => {
    const project = projects.find((item) => item.name === entry.project_name)
    if (!project) {
      toast.push('该项目不在当前列表（可能是归档项目），请先勾选「显示已归档」', 'error')
      return
    }
    try {
      await restoreTrash(project.id, entry.trash_rel)
      toast.push('已恢复到原位置', 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  // 编辑信息弹窗使用最新项目数据（封面上传/移除后 has_cover 需要同步刷新）
  const editing = editTarget
    ? projects.find((item) => item.id === editTarget.id) ?? editTarget
    : null

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">书架</h1>
          <p className="page-header__desc">每本书一个完整工作区。</p>
        </div>
        <div className="btn-row">
          <label className="checkbox">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(event) => setIncludeArchived(event.target.checked)}
            />
            显示已归档
          </label>
          <label className="btn btn--ghost btn--sm" style={{ cursor: 'pointer' }}>
            导入项目包
            <input
              type="file"
              accept="application/json"
              style={{ display: 'none' }}
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void onRestorePackage(file)
                event.target.value = ''
              }}
            />
          </label>
          <button className="btn btn--primary" type="button" onClick={() => setCreateOpen(true)}>
            新建项目
          </button>
        </div>
      </header>

      {projects.length === 0 ? (
        <div className="empty-state">
          <p className="empty-state__title">还没有项目</p>
          <p className="empty-state__desc">
            点「新建项目」开书：工作台会按模板生成 <code>projects/书名/</code> 完整工作区，
            并初始化设定、状态与章节分组。
          </p>
        </div>
      ) : (
        <div className="shelf-grid">
          {projects.map((project) => (
            <article
              key={project.id}
              className={`book-card${project.archived ? ' book-card--archived' : ''}`}
              role={project.archived ? undefined : 'button'}
              tabIndex={project.archived ? -1 : 0}
              onClick={() => {
                if (!project.archived) navigate(`/project/${project.id}/editor`)
              }}
              onKeyDown={(event) => {
                if (project.archived) return
                if (event.key === 'Enter' || event.key === ' ') {
                  event.preventDefault()
                  navigate(`/project/${project.id}/editor`)
                }
              }}
            >
              <div className="book-card__cover" aria-hidden="true">
                <span className="book-card__cover-fallback">{project.name.slice(0, 1)}</span>
                {project.has_cover ? (
                  <img
                    src={`/api/projects/${project.id}/cover?v=${coverBust}`}
                    alt=""
                    loading="lazy"
                    onError={(event) => event.currentTarget.remove()}
                  />
                ) : null}
              </div>
              <div className="book-card__info">
                <h2 className="book-card__name">{project.name}</h2>
                <p className="book-card__meta">
                  {project.genre || '未填题材'} · {project.platform || '未填平台'}
                  {project.protagonist ? ` · 主角 ${project.protagonist}` : ''}
                </p>
                {project.one_liner ? (
                  <p className="book-card__meta book-card__liner">{project.one_liner}</p>
                ) : null}
                <p className="book-card__meta mono">{project.created_at}</p>
                {project.archived ? <span className="tag tag--warn">已归档</span> : null}
              </div>
              <div className="book-card__actions">
                <button
                  className="btn btn--primary btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    navigate(`/project/${project.id}/editor`)
                  }}
                >
                  进入工作台
                </button>
                <button
                  className="btn btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    onOpenEdit(project)
                  }}
                >
                  编辑信息
                </button>
                <button
                  className="btn btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    setImportTarget(project)
                  }}
                >
                  导入章节
                </button>
                <button
                  className="btn btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    void onExport(project)
                  }}
                >
                  导出正文
                </button>
                <button
                  className="btn btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    void onBackup(project)
                  }}
                >
                  备份包
                </button>
                <button
                  className="btn btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    onOpenPublish(project)
                  }}
                >
                  发布导出
                </button>
                <button
                  className="btn btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    void onSaveAsTemplate(project)
                  }}
                >
                  另存为模板
                </button>
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  onClick={(event) => {
                    event.stopPropagation()
                    void onArchive(project)
                  }}
                >
                  {project.archived ? '取消归档' : '归档'}
                </button>
              </div>
            </article>
          ))}
        </div>
      )}

      <section className="panel" style={{ marginTop: 'var(--space-6)' }}>
        <header className="panel__header">
          <h2 className="panel__title">开书模板</h2>
          <span className="muted">内置模板不可删除，可复制后修改</span>
        </header>
        <div className="panel__body">
          <div className="row">
            {templates.map((template) => (
              <span key={template.id} className="tag">
                {template.name}
                {template.is_builtin ? '（内置）' : ''}
                {template.is_builtin ? null : (
                  <button
                    className="btn btn--ghost btn--sm"
                    type="button"
                    onClick={() => void onDeleteTemplate(template)}
                  >
                    删除
                  </button>
                )}
              </span>
            ))}
          </div>
        </div>
      </section>

      <section className="panel" style={{ marginTop: 'var(--space-4)' }}>
        <header className="panel__header">
          <h2 className="panel__title">回收站</h2>
          <span className="muted">删除的文件在此可恢复</span>
        </header>
        <div className="panel__body">
          {trash.length === 0 ? (
            <p className="muted">回收站为空。</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>项目</th>
                  <th>原路径</th>
                  <th>类型</th>
                  <th>删除时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {trash.slice(0, 30).map((entry) => (
                  <tr key={entry.trash_rel}>
                    <td>{entry.project_name}</td>
                    <td className="mono">{entry.rel_path}</td>
                    <td>{entry.kind === 'dir' ? '文件夹' : '文件'}</td>
                    <td className="mono">{entry.deleted_at}</td>
                    <td>
                      <button className="btn btn--sm" type="button" onClick={() => void onRestoreTrash(entry)}>
                        恢复
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <Modal
        title="新建项目"
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        footer={
          <div className="btn-row btn-row--end">
            <button className="btn" type="button" onClick={() => setCreateOpen(false)}>
              取消
            </button>
            <button className="btn btn--primary" type="button" disabled={busy} onClick={() => void onCreate()}>
              {busy ? '创建中…' : '开书'}
            </button>
          </div>
        }
      >
        <div className="stack">
          <div className="field">
            <label className="field__label" htmlFor="book-name">
              书名（必填）
            </label>
            <input
              autoComplete={NO_AUTOFILL}
              id="book-name"
              className="input"
              value={form.name}
              onChange={(event) => setForm({ ...form, name: event.target.value })}
            />
          </div>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label className="field__label" htmlFor="book-genre">
                题材
              </label>
              <input
                autoComplete={NO_AUTOFILL}
                id="book-genre"
                className="input"
                value={form.genre}
                onChange={(event) => setForm({ ...form, genre: event.target.value })}
              />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label className="field__label" htmlFor="book-platform">
                目标平台
              </label>
              <input
                autoComplete={NO_AUTOFILL}
                id="book-platform"
                className="input"
                value={form.platform}
                onChange={(event) => setForm({ ...form, platform: event.target.value })}
              />
            </div>
          </div>
          <div className="field">
            <label className="field__label" htmlFor="book-protagonist">
              主角
            </label>
            <input
              autoComplete={NO_AUTOFILL}
              id="book-protagonist"
              className="input"
              value={form.protagonist}
              onChange={(event) => setForm({ ...form, protagonist: event.target.value })}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="book-liner">
              一句话设定
            </label>
            <textarea
              autoComplete={NO_AUTOFILL}
              id="book-liner"
              className="textarea"
              value={form.one_liner}
              onChange={(event) => setForm({ ...form, one_liner: event.target.value })}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="book-template">
              开书模板
            </label>
            <select
              id="book-template"
              className="select"
              value={form.template}
              onChange={(event) => setForm({ ...form, template: event.target.value })}
            >
              <option value="">默认模板（推荐）</option>
              {templates
                .filter((template) => !template.is_builtin)
                .map((template) => (
                  <option key={template.id} value={template.name}>
                    {template.name}
                  </option>
                ))}
            </select>
            <span className="field__hint">另存为模板可在项目卡片上操作。</span>
          </div>
        </div>
      </Modal>

      <Modal
        title={`编辑信息 · ${editTarget?.name ?? ''}`}
        open={editTarget !== null}
        onClose={() => setEditTarget(null)}
        footer={
          <div className="btn-row btn-row--end">
            <button className="btn" type="button" onClick={() => setEditTarget(null)}>
              取消
            </button>
            <button
              className="btn btn--primary"
              type="button"
              disabled={busy}
              onClick={() => void onSaveEdit()}
            >
              {busy ? '保存中…' : '保存'}
            </button>
          </div>
        }
      >
        <div className="stack">
          <div className="row" style={{ alignItems: 'flex-start' }}>
            <div className="edit-cover">
              <div className="edit-cover__preview">
                {editing && editing.has_cover ? (
                  <img
                    key={coverBust}
                    src={`/api/projects/${editing.id}/cover?v=${coverBust}`}
                    alt="封面预览"
                  />
                ) : (
                  <span className="edit-cover__placeholder">
                    {editForm.name.trim().slice(0, 1) || '书'}
                  </span>
                )}
              </div>
              <div className="btn-row">
                <label className="btn btn--sm" style={{ cursor: 'pointer' }}>
                  上传封面
                  <input
                    type="file"
                    accept="image/png,image/jpeg,image/webp"
                    style={{ display: 'none' }}
                    disabled={busy}
                    onChange={(event) => {
                      const file = event.target.files?.[0]
                      event.target.value = ''
                      if (file) void onUploadCover(file)
                    }}
                  />
                </label>
                {editing && editing.has_cover ? (
                  <button
                    className="btn btn--ghost btn--sm"
                    type="button"
                    disabled={busy}
                    onClick={() => void onRemoveCover()}
                  >
                    移除
                  </button>
                ) : null}
              </div>
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label className="field__label" htmlFor="edit-book-name">
                书名（必填）
              </label>
              <input
                autoComplete={NO_AUTOFILL}
                id="edit-book-name"
                className="input"
                value={editForm.name}
                onChange={(event) => setEditForm({ ...editForm, name: event.target.value })}
              />
              <span className="field__hint">改名后请刷新已打开的页面。</span>
            </div>
          </div>
          <div className="row">
            <div className="field" style={{ flex: 1 }}>
              <label className="field__label" htmlFor="edit-book-genre">
                题材
              </label>
              <input
                autoComplete={NO_AUTOFILL}
                id="edit-book-genre"
                className="input"
                value={editForm.genre}
                onChange={(event) => setEditForm({ ...editForm, genre: event.target.value })}
              />
            </div>
            <div className="field" style={{ flex: 1 }}>
              <label className="field__label" htmlFor="edit-book-platform">
                目标平台
              </label>
              <input
                autoComplete={NO_AUTOFILL}
                id="edit-book-platform"
                className="input"
                value={editForm.platform}
                onChange={(event) => setEditForm({ ...editForm, platform: event.target.value })}
              />
            </div>
          </div>
          <div className="field">
            <label className="field__label" htmlFor="edit-book-protagonist">
              主角
            </label>
            <input
              autoComplete={NO_AUTOFILL}
              id="edit-book-protagonist"
              className="input"
              value={editForm.protagonist}
              onChange={(event) => setEditForm({ ...editForm, protagonist: event.target.value })}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="edit-book-liner">
              一句话设定
            </label>
            <textarea
              autoComplete={NO_AUTOFILL}
              id="edit-book-liner"
              className="textarea"
              value={editForm.one_liner}
              onChange={(event) => setEditForm({ ...editForm, one_liner: event.target.value })}
            />
          </div>
        </div>
      </Modal>

      <Modal
        title={`发布导出 · ${publishTarget?.name ?? ''}`}
        open={publishTarget !== null}
        onClose={() => setPublishTarget(null)}
        width={860}
        footer={
          <div className="btn-row btn-row--end">
            <button className="btn" type="button" onClick={() => setPublishTarget(null)}>
              关闭
            </button>
            <button
              className="btn btn--primary"
              type="button"
              disabled={busy}
              onClick={() => void onBuildPublish()}
            >
              {busy ? '整理中…' : '按平台整理'}
            </button>
            <button
              className="btn"
              type="button"
              disabled={!publishResult}
              onClick={onDownloadPublish}
            >
              下载发布稿
            </button>
          </div>
        }
      >
        <div className="stack">
          <div className="row">
            <div className="field" style={{ minWidth: '12rem' }}>
              <label className="field__label" htmlFor="publish-platform">
                目标平台
              </label>
              <select
                id="publish-platform"
                className="select"
                value={platform}
                onChange={(event) => setPlatform(event.target.value)}
              >
                {platforms.map((item) => (
                  <option key={item.key} value={item.key}>
                    {item.label}
                  </option>
                ))}
              </select>
            </div>
            <label className="checkbox" style={{ alignSelf: 'flex-end' }}>
              <input
                type="checkbox"
                checked={onlyCompleted}
                onChange={(event) => setOnlyCompleted(event.target.checked)}
              />
              只导出「完成」状态的章节
            </label>
          </div>

          <p className="muted">导出只取正文与标题，不含凭据。</p>

          {publishResult ? (
            <>
              <div className="row">
                <span className="tag tag--primary">
                  {publishResult.platform_label}
                </span>
                <span className="tag">{publishResult.stats.chapters} 章</span>
                <span className="tag">{publishResult.stats.words} 字</span>
                <span className="tag">
                  平均 {publishResult.stats.avg_words} 字/章
                </span>
                <span className="mono">{publishResult.filename}</span>
              </div>

              <section className="panel">
                <header className="panel__header">
                  <h3 className="panel__title">提交前自检</h3>
                  <span className="muted">逐项给结论与依据</span>
                </header>
                <div className="panel__body panel__body--tight">
                  <table className="table">
                    <thead>
                      <tr>
                        <th>检查项</th>
                        <th>结论</th>
                        <th>依据</th>
                      </tr>
                    </thead>
                    <tbody>
                      {publishResult.checklist.map((item) => (
                        <tr key={item.item}>
                          <td>{item.item}</td>
                          <td className={item.ok ? 'cell-pass' : 'cell-fail'}>
                            {item.ok ? '通过' : '待处理'}
                          </td>
                          <td className="muted">{item.basis}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>

              {publishResult.warnings.length ? (
                <section className="panel">
                  <header className="panel__header">
                    <h3 className="panel__title">章节提醒（{publishResult.warnings.length}）</h3>
                  </header>
                  <div className="panel__body panel__body--tight">
                    <table className="table">
                      <thead>
                        <tr>
                          <th>章节</th>
                          <th>类型</th>
                          <th>说明</th>
                        </tr>
                      </thead>
                      <tbody>
                        {publishResult.warnings.slice(0, 40).map((item, index) => (
                          <tr key={`${item.rel_path}-${index}`}>
                            <td className="mono">{item.rel_path}</td>
                            <td>{item.level}</td>
                            <td className="muted">{item.note}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </section>
              ) : null}

              {publishResult.notes.length ? (
                <ul className="muted" style={{ margin: 0, paddingLeft: 'var(--space-5)' }}>
                  {publishResult.notes.map((note, index) => (
                    <li key={index}>{note}</li>
                  ))}
                </ul>
              ) : null}

              <div className="field">
                <label className="field__label" htmlFor="publish-preview">
                  发布稿预览（前 4000 字）
                </label>
                <textarea
                  autoComplete={NO_AUTOFILL}
                  id="publish-preview"
                  className="textarea textarea--mono"
                  style={{ minHeight: '220px' }}
                  readOnly
                  value={publishResult.content.slice(0, 4000)}
                />
              </div>
            </>
          ) : (
            <p className="muted">选择平台后点「按平台整理」查看自检结论与发布稿。</p>
          )}
        </div>
      </Modal>

      <Modal
        title={`导入章节到《${importTarget?.name ?? ''}》`}
        open={importTarget !== null}
        onClose={() => setImportTarget(null)}
        footer={
          <div className="btn-row btn-row--end">
            <button className="btn" type="button" onClick={() => setImportTarget(null)}>
              取消
            </button>
            <button className="btn btn--primary" type="button" disabled={busy} onClick={() => void onImport()}>
              导入为草稿章
            </button>
          </div>
        }
      >
        <div className="stack">
          <div className="field">
            <label className="field__label" htmlFor="import-name">
              文件名（决定标题与后缀校验）
            </label>
            <input
              autoComplete={NO_AUTOFILL}
              id="import-name"
              className="input"
              placeholder="导入片段.md"
              value={importName}
              onChange={(event) => setImportName(event.target.value)}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="import-file">
              从文件读取
            </label>
            <input
              id="import-file"
              className="input"
              type="file"
              accept=".md,.txt,text/plain,text/markdown"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (!file) return
                setImportName(file.name)
                void file.text().then(setImportText)
                event.target.value = ''
              }}
            />
          </div>
          <div className="field">
            <label className="field__label" htmlFor="import-text">
              或直接粘贴内容
            </label>
            <textarea
              autoComplete={NO_AUTOFILL}
              id="import-text"
              className="textarea textarea--mono"
              style={{ minHeight: '180px' }}
              value={importText}
              onChange={(event) => setImportText(event.target.value)}
            />
          </div>
        </div>
      </Modal>

      {promptNode}
    </div>
  )
}