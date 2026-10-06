import WorkspacePage, { ResourceState, useResourceRequest, ActionMenu, WorkspaceTabs } from '../components/WorkspacePage'
/** 书架：项目列表 / 开书（含模板选择）/ 归档 / 进入工作区 / 回收站 / 导入导出。 */

import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  archiveProject,
  buildPublishExport,
  createProject,
  deleteCover,
  deleteProject,
  exportChapters,
  exportPackage,
  getTemplatePrefs,
  importDocument,
  listProjects,
  listPublishPlatforms,
  listTemplates,
  listTrash,
  restorePackage,
  restoreProject,
  restoreTrash,
  saveAsTemplate,
  updateProject,
  uploadCover,
} from '../api/client'
import type { PublishPlatform } from '../api/client'
import type { Project, Template, TemplatePrefs, TrashEntry } from '../api/types'
import Modal from '../components/Modal'
import Drawer from '../components/Drawer'
import { useConfirm } from '../components/ConfirmDialog'
import InlineEdit from '../components/InlineEdit'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

export default function Bookshelf() {
  const navigate = useNavigate()
  const toast = useToast()
  const [projects, setProjects] = useState<Project[]>([])
  const [shelfTab, setShelfTab] = useState<'books' | 'trash'>('books')
  const [search, setSearch] = useState('')
  const auxiliaryResource = useResourceRequest('shelf-auxiliary')
  const [includeArchived, setIncludeArchived] = useState(false)
  const projectResource = useResourceRequest(includeArchived ? 'archived' : 'active')
  const [templates, setTemplates] = useState<Template[]>([])
  const [tplPrefs, setTplPrefs] = useState<TemplatePrefs>({
    default: '',
    by_genre: {},
    by_platform: {},
  })
  const [templateTouched, setTemplateTouched] = useState(false)
  const [trash, setTrash] = useState<TrashEntry[]>([])
  const [busy, setBusy] = useState(false)
  const [confirm, confirmNode] = useConfirm()

  // 模板表行内交互态：新建行 / 命名中 / 删除二次确认
  // 书卡书名行内改名
  const [renamingBook, setRenamingBook] = useState<number | null>(null)
  const [coverDrag, setCoverDrag] = useState(false)

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
    const booksToken = projectResource.begin()
    const auxiliaryToken = auxiliaryResource.begin()
    await Promise.allSettled([
      listProjects(includeArchived).then(items => { if (projectResource.accept(booksToken)) { setProjects(items); projectResource.finish(booksToken) } }).catch(err => projectResource.fail(booksToken, errorMessage(err))),
      Promise.all([listTemplates(), listTrash(), getTemplatePrefs()]).then(([tpl, trashItems, prefs]) => { if (auxiliaryResource.accept(auxiliaryToken)) { setTemplates(tpl); setTrash(trashItems); setTplPrefs(prefs); auxiliaryResource.finish(auxiliaryToken) } }).catch(err => auxiliaryResource.fail(auxiliaryToken, errorMessage(err))),
    ])
  }, [includeArchived])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void listPublishPlatforms()
      .then(setPlatforms)
      .catch(() => setPlatforms([]))
  }, [])

  // 开书模板：用户未手动选择时，按「题材 → 模板」映射自动匹配
  useEffect(() => {
    if (templateTouched) return
    const matched = tplPrefs.by_genre[form.genre.trim()]
    if (matched) setForm((prev) => ({ ...prev, template: matched }))
  }, [form.genre, tplPrefs, templateTouched])

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
      setForm({ name: '', genre: '', platform: '', protagonist: '', one_liner: '', template: defaultTemplateName })
      await reload()
      navigate(`/project/${project.id}/chat`)
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

  // 书卡书名双击行内改名：只改元信息，不打开「编辑信息」弹窗
  const commitRenameBook = async (project: Project, name: string) => {
    setRenamingBook(null)
    if (!name || name === project.name) return
    setBusy(true)
    try {
      const updated = await updateProject(project.id, { name })
      toast.push(`已重命名为《${updated.name}》`, 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  // 移入回收站是可逆操作：直接执行 + Toast 撤销，不再用确认弹窗拦截
  const onDelete = async (project: Project) => {
    setBusy(true)
    try {
      await deleteProject(project.id)
      toast.push(`已移入回收站《${project.name}》`, 'success', {
        label: '撤销',
        onAction: () => {
          void restoreProject(project.id).then(reload).catch((err) => toast.push(errorMessage(err), 'error'))
        },
      })
      // 删除的是当前打开的书时，清掉项目记忆，避免导航指向已失效的项目
      try {
        if (window.localStorage.getItem('aiw.currentProjectId') === String(project.id)) {
          window.localStorage.removeItem('aiw.currentProjectId')
        }
      } catch {
        // 隐私模式等场景忽略
      }
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onPurge = async (projectId: number, name: string) => {
    const ok = await confirm({
      title: '彻底删除',
      message: `将彻底删除《${name}》及其全部章节、设定、快照与索引记录，不可恢复。确定继续？`,
      confirmText: '彻底删除',
      danger: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      await deleteProject(projectId, true)
      toast.push(`已彻底删除《${name}》`, 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const onRestoreBook = async (entry: TrashEntry) => {
    if (entry.project_id === null || entry.project_id === undefined) return
    setBusy(true)
    try {
      await restoreProject(entry.project_id)
      toast.push(`已恢复《${entry.project_name}》`, 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
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
      link.download = `${project.name}-书籍备份.json`
      link.click()
      URL.revokeObjectURL(url)
      toast.push('书籍备份已导出（不含任何凭据）', 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onRestorePackage = async (file: File) => {
    try {
      const text = await file.text()
      const payload = JSON.parse(text) as Record<string, unknown>
      // 不再弹命名框：直接用包内原名恢复，恢复后可在模板表内行内改名
      const result = await restorePackage(payload)
      const project = result.project as Project | undefined
      toast.push(`已恢复书籍《${project?.name ?? ''}》`, 'success')
      await reload()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  // 复制/另存时按已有名称生成唯一默认名，避免直接落库撞名
  const uniqueName = (base: string) => {
    const names = new Set(templates.map((item) => item.name))
    if (!names.has(base)) return base
    let index = 2
    while (names.has(`${base}${index}`)) index += 1
    return `${base}${index}`
  }

  const onSaveAsTemplate = async (project: Project) => {
    const name = uniqueName(`${project.name}·模板`)
    try {
      await saveAsTemplate(name, project.id)
      toast.push(`已另存为模板「${name}」`, 'success')
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
    try {
      await restoreTrash(entry.trash_rel)
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

  // 开书模板：默认模板名（偏好为空时回落到内置模板）
  const defaultTemplateName =
    tplPrefs.default || templates.find((item) => item.is_builtin)?.name || ''
  const genreMatched =
    !templateTouched && form.genre.trim() !== '' && tplPrefs.by_genre[form.genre.trim()] === form.template

  const onOpenCreate = () => {
    setForm({
      name: '',
      genre: '',
      platform: '',
      protagonist: '',
      one_liner: '',
      template: defaultTemplateName,
    })
    setTemplateTouched(false)
    setCreateOpen(true)
  }

  return (
    <WorkspacePage className="workspace-shelf">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">书架</h1>
          <p className="page-header__desc">从一本书开始，继续你的创作。</p>
        </div>
        <div className="btn-row">
          <button className="btn btn--ghost btn--sm" onClick={() => navigate('/settings?section=templates')}>管理开书模板</button>
          <label className="checkbox">
            <input
              type="checkbox"
              checked={includeArchived}
              onChange={(event) => setIncludeArchived(event.target.checked)}
            />
            显示已归档
          </label>
          <label className="btn btn--ghost btn--sm" style={{ cursor: 'pointer' }}>
            导入书籍备份
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
          <button className="btn btn--primary" type="button" onClick={onOpenCreate}>
            新建书籍
          </button>
        </div>
      </header>

      <WorkspaceTabs label="书架区域" items={[{key:'books',label:'我的书籍'}, {key:'trash',label:`回收站 · ${trash.length}`}]} value={shelfTab} onChange={setShelfTab} />
      {shelfTab === 'books' && <>
      <div className="workspace-shelf-toolbar"><input className="input" type="search" aria-label="搜索书籍" placeholder="搜索书名、题材、主角…" value={search} onChange={event => setSearch(event.target.value)} /><span className="muted">{projects.length} 本书</span><button className="btn btn--ghost btn--sm" onClick={() => void reload()}>刷新书架</button></div>
      <ResourceState {...projectResource} hasData={projectResource.loaded && projects.length > 0} onRetry={() => void reload()}>
      {projects.length === 0 ? (
        <div className="empty-state">
          <p className="empty-state__title">书架还是空的</p>
          <p className="empty-state__desc">
            点「新建书籍」开书，工作台会准备设定、状态和章节，接着与写作助手讨论你的故事。
          </p>
        </div>
      ) : (
        <div className="shelf-grid">
          {projects.filter(project => `${project.name} ${project.genre} ${project.protagonist}`.toLowerCase().includes(search.toLowerCase())).map((project) => (
            <article
              key={project.id}
              className={`book-card${project.archived ? ' book-card--archived' : ''}`}
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
                {renamingBook === project.id ? (
                  <InlineEdit
                    defaultValue={project.name}
                    ariaLabel="书名"
                    onCommit={(value) => void commitRenameBook(project, value)}
                    onCancel={() => setRenamingBook(null)}
                  />
                ) : (
                  <h2
                    className="book-card__name"
                    title="双击改名"
                    onClick={(event) => event.stopPropagation()}
                    onDoubleClick={(event) => {
                      event.stopPropagation()
                      setRenamingBook(project.id)
                    }}
                  >
                    {project.name}
                  </h2>
                )}
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
                    navigate(`/project/${project.id}/chat`)
                  }}
                >
                  进入创作
                </button>
                <ActionMenu ariaLabel={`《${project.name}》更多操作`}>
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
                <button
                  className="btn btn--danger btn--sm"
                  type="button"
                  disabled={busy}
                  onClick={(event) => {
                    event.stopPropagation()
                    void onDelete(project)
                  }}
                >
                  删除
                </button>
                </ActionMenu>
              </div>
            </article>
          ))}
        </div>
      )}

      {projects.length > 0 && !projects.some(project => `${project.name} ${project.genre} ${project.protagonist}`.toLowerCase().includes(search.toLowerCase())) && <div className="empty-state"><p className="empty-state__title">没有匹配的书籍</p><p className="empty-state__desc">换个关键词，或清除搜索查看全部书籍。</p><button className="btn btn--sm" onClick={() => setSearch('')}>清除搜索</button></div>}
      </ResourceState></>}
      {auxiliaryResource.error && <div className="workspace-notice workspace-notice--error" role="alert">模板或回收站暂时无法更新：{auxiliaryResource.error}<button className="btn btn--sm" onClick={() => void reload()}>重试</button></div>}
      {shelfTab === 'trash' && <ResourceState {...auxiliaryResource} hasData={auxiliaryResource.loaded} onRetry={() => void reload()}><section className="panel">
        <header className="panel__header">
          <h2 className="panel__title">回收站</h2>
          <span className="muted">删除的书与文件在此可恢复</span>
        </header>
        <div className="panel__body">
          {trash.length === 0 ? (
            <p className="muted">回收站为空。</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>类型</th>
                  <th>书籍</th>
                  <th>原路径</th>
                  <th>删除时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {trash.slice(0, 30).map((entry) => {
                  const isBook = entry.kind === 'project'
                  const kindLabel =
                    isBook ? '整本书' : entry.kind === 'dir' ? '文件夹' : '文件'
                  return (
                    <tr
                      key={`${entry.kind}-${entry.project_name}-${entry.trash_rel}-${entry.deleted_at}`}
                    >
                      <td>{kindLabel}</td>
                      <td>{entry.project_name}</td>
                      <td className="mono">{isBook ? '—' : entry.rel_path}</td>
                      <td className="mono">{entry.deleted_at}</td>
                      <td>
                        <div className="btn-row">
                          <button
                            className="btn btn--sm"
                            type="button"
                            disabled={busy}
                            onClick={() =>
                              isBook ? void onRestoreBook(entry) : void onRestoreTrash(entry)
                            }
                          >
                            恢复
                          </button>
                          {isBook ? (
                            <button
                              className="btn btn--danger btn--sm"
                              type="button"
                              disabled={busy}
                              onClick={() =>
                                entry.project_id != null &&
                                void onPurge(entry.project_id, entry.project_name)
                              }
                            >
                              彻底删除
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </div>
      </section></ResourceState>}

      <Modal
        title="新建书籍"
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
              onChange={(event) => {
                setTemplateTouched(true)
                setForm({ ...form, template: event.target.value })
              }}
            >
              {templates.map((template) => (
                <option key={template.id} value={template.name}>
                  {template.name}
                  {template.is_builtin ? '（内置）' : ''}
                  {tplPrefs.default === template.name ? '（默认）' : ''}
                </option>
              ))}
              {templates.length === 0 ? (
                <option value="">默认模板（推荐）</option>
              ) : null}
            </select>
            <span className="field__hint">
              {genreMatched
                ? `已按题材「${form.genre.trim()}」自动匹配模板。`
                : '可在设置 → 模板管理 配置默认模板与题材/平台适配。'}
            </span>
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
              <div
                className={`edit-cover__preview${coverDrag ? ' is-dragover' : ''}`}
                onDragOver={(event) => { event.preventDefault(); setCoverDrag(true) }}
                onDragLeave={() => setCoverDrag(false)}
                onDrop={(event) => {
                  event.preventDefault()
                  setCoverDrag(false)
                  const file = event.dataTransfer.files?.[0]
                  if (file) void onUploadCover(file)
                }}
              >
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
                  上传封面（也可拖入图片）
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

      <Drawer
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
              只导出已定稿章节（完成 / 发表）
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
      </Drawer>

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

      {confirmNode}
    </WorkspacePage>
  )
}
