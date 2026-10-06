/**
 * 模板管理（设置页「模板管理」分区的内容）。
 *
 * 三块：模板列表（新建/复制/重命名/删除/设为默认/导入导出）、内容编辑（模板文件树 + 文本编辑，
 * 内置模板只读）、开书适配（默认模板 + 题材/平台映射）。
 * 交互纪律：只用既有 CSS 类；不使用 window.prompt / window.confirm。
 */

import { ResourceState, useResourceRequest } from './WorkspacePage'
import { useCallback, useEffect, useRef, useState } from 'react'
import {
  createBlankTemplate,
  createTemplateNode,
  deleteTemplate,
  deleteTemplateNode,
  duplicateTemplate,
  exportTemplate,
  getTemplatePrefs,
  getTemplateTree,
  importTemplate,
  listTemplates,
  patchTemplateNode,
  readTemplateFile,
  renameTemplate,
  updateTemplatePrefs,
  writeTemplateFile,
} from '../api/client'
import type {
  Template,
  TemplatePackage,
  TemplatePrefs,
  TemplateTreeNode,
  TemplateTree,
} from '../api/types'
import { useConfirm } from './ConfirmDialog'
import InlineEdit from './InlineEdit'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

type MappingRow = { key: string; value: string }

const mappingToRows = (map: Record<string, string>): MappingRow[] =>
  Object.entries(map).map(([key, value]) => ({ key, value }))

const rowsToMapping = (rows: MappingRow[]): Record<string, string> => {
  const result: Record<string, string> = {}
  rows.forEach((row) => {
    const key = row.key.trim()
    if (key && row.value) result[key] = row.value
  })
  return result
}

export default function TemplateManager() {
  const toast = useToast()
  const [confirm, confirmNode] = useConfirm()

  const [templates, setTemplates] = useState<Template[]>([])
  const [prefs, setPrefs] = useState<TemplatePrefs>({ default: '', by_genre: {}, by_platform: {} })
  const [activeName, setActiveName] = useState('')
  const [tree, setTree] = useState<TemplateTree | null>(null)
  const [activeRel, setActiveRel] = useState('')
  const [text, setText] = useState('')
  const [dirty, setDirty] = useState(false)
  const [busy, setBusy] = useState(false)

  // 行内交互态：新建模板行 / 模板改名 / 树节点改名 / 树内新建
  const [creatingTemplate, setCreatingTemplate] = useState(false)
  const [editingTemplate, setEditingTemplate] = useState('')
  const [renamingNode, setRenamingNode] = useState('')
  const [creatingNode, setCreatingNode] = useState<{ parentRel: string; isDir: boolean } | null>(null)

  const [genreRows, setGenreRows] = useState<MappingRow[]>([])
  const [platformRows, setPlatformRows] = useState<MappingRow[]>([])
  const indexResource = useResourceRequest('template-list')
  const prefsResource = useResourceRequest('template-prefs')
  const treeResource = useResourceRequest(activeName)
  const fileResource = useResourceRequest(activeName)
  const [fileRequested, setFileRequested] = useState('')
  const mappingsDirty = useRef(false)

  const active = templates.find((item) => item.name === activeName) ?? null
  const readOnly = active?.is_builtin ?? false

  const load = useCallback(async () => {
    const indexToken = indexResource.begin()
    const prefsToken = prefsResource.begin()
    await Promise.allSettled([
      listTemplates().then(list => { if (!indexResource.accept(indexToken)) return; setTemplates(list); setActiveName(prev => prev && list.some(item => item.name === prev) ? prev : list[0]?.name || ''); indexResource.finish(indexToken) }).catch(err => indexResource.fail(indexToken, errorMessage(err))),
      getTemplatePrefs().then(data => { if (!prefsResource.accept(prefsToken)) return; setPrefs(data); prefsResource.finish(prefsToken) }).catch(err => prefsResource.fail(prefsToken, errorMessage(err))),
    ])
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    if (mappingsDirty.current) return
    setGenreRows(mappingToRows(prefs.by_genre))
    setPlatformRows(mappingToRows(prefs.by_platform))
  }, [prefs])

  const loadTree = useCallback(
    async (name: string) => {
      if (!name) {
        setTree(null)
        return
      }
      const token = treeResource.begin()
      try { const data = await getTemplateTree(name); if (!treeResource.accept(token)) return; setTree(data); treeResource.finish(token) } catch (err) { treeResource.fail(token, errorMessage(err)) }
    },
    [activeName, treeResource.begin, treeResource.accept, treeResource.finish, treeResource.fail],
  )

  useEffect(() => {
    setActiveRel('')
    setTree(null)
    setFileRequested('')
    setText('')
    setDirty(false)
    setRenamingNode('')
    setCreatingNode(null)
    void loadTree(activeName)
  }, [activeName, loadTree])

  const chooseTemplate = async (name: string) => {
    if (name !== activeName && dirty && !(await confirm({title:'切换模板', message:'当前文件有未保存改动，切换模板将舍弃它。',confirmText:'舍弃并切换'}))) return
    setActiveName(name)
  }
  const openFile = async (relPath: string) => {
    if (!activeName) return
    if (relPath !== activeRel && dirty && !(await confirm({title:'切换文件', message:'当前文件有未保存改动，切换文件将舍弃它。',confirmText:'舍弃并切换'}))) return
    setFileRequested(relPath)
    const token = fileResource.begin()
    try { const file = await readTemplateFile(activeName, relPath); if (!fileResource.accept(token)) return; setActiveRel(relPath); setText(file.content); setDirty(false); fileResource.finish(token) } catch (err) { fileResource.fail(token, errorMessage(err)) }
  }

  const onSaveFile = async () => {
    if (!activeName || !activeRel) return
    setBusy(true)
    try {
      await writeTemplateFile(activeName, activeRel, text)
      toast.push('已保存模板文件', 'success')
      setDirty(false)
      await loadTree(activeName)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  // 复制时按已有名称生成唯一默认名，避免直接复制撞名
  const uniqueName = (base: string) => {
    const names = new Set(templates.map((item) => item.name))
    if (!names.has(base)) return base
    let index = 2
    while (names.has(`${base}${index}`)) index += 1
    return `${base}${index}`
  }

  const commitCreateBlank = async (name: string) => {
    setCreatingTemplate(false)
    try {
      const created = await createBlankTemplate(name)
      toast.push(`已新建模板「${created.name}」`, 'success')
      await load()
      setActiveName(created.name)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onDuplicate = async (template: Template) => {
    const name = uniqueName(`${template.name}-副本`)
    try {
      const copy = await duplicateTemplate(template.name, name)
      toast.push(`已复制为「${copy.name}」`, 'success')
      await load()
      setActiveName(copy.name)
      setEditingTemplate(copy.name)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const commitRenameTemplate = async (from: string, name: string) => {
    setEditingTemplate('')
    if (!name || name === from) return
    try {
      const renamed = await renameTemplate(from, name)
      toast.push(`已重命名为「${renamed.name}」`, 'success')
      await load()
      setActiveName(renamed.name)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onDelete = async (template: Template) => {
    const ok = await confirm({
      title: '删除模板',
      message: `将删除模板「${template.name}」及其全部占位文件，不可恢复。确定删除？`,
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      await deleteTemplate(template.name)
      toast.push('模板已删除', 'success')
      if (activeName === template.name) setActiveName('')
      await load()
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onSetDefault = async (name: string) => {
    try {
      const next = await updateTemplatePrefs({ default: name })
      setPrefs(next)
      toast.push(name ? `已设为默认模板「${name}」` : '已改回内置默认模板', 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onExport = async (template: Template) => {
    try {
      const payload = await exportTemplate(template.name)
      const blob = new Blob([JSON.stringify(payload, null, 2)], {
        type: 'application/json;charset=utf-8',
      })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${template.name}-模板包.json`
      link.click()
      URL.revokeObjectURL(url)
      toast.push('模板包已导出', 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onImport = async (file: File) => {
    try {
      const payload = JSON.parse(await file.text()) as TemplatePackage
      // 不再弹命名框：直接用包内原名导入，导入后可在列表行内改名
      const created = await importTemplate(payload, payload.name || undefined)
      toast.push(`已导入模板「${created.name}」`, 'success')
      await load()
      setActiveName(created.name)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const commitCreateNode = async (parentRel: string, isDir: boolean, name: string) => {
    setCreatingNode(null)
    if (!activeName || !name) return
    try {
      await createTemplateNode(activeName, parentRel, name, isDir)
      toast.push('已新建', 'success')
      await loadTree(activeName)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const commitRenameNode = async (node: TemplateTreeNode, name: string) => {
    setRenamingNode('')
    if (!activeName || !name || name === node.name) return
    try {
      await patchTemplateNode(activeName, node.rel_path, { new_name: name })
      toast.push('已重命名', 'success')
      if (activeRel === node.rel_path) setActiveRel('')
      await loadTree(activeName)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onDeleteNode = async (node: TemplateTreeNode) => {
    if (!activeName) return
    const ok = await confirm({
      title: '删除条目',
      message: `将物理删除「${node.name}」${node.type === 'dir' ? '及其全部内容' : ''}，不可恢复。确定删除？`,
      confirmText: '删除',
      danger: true,
    })
    if (!ok) return
    try {
      await deleteTemplateNode(activeName, node.rel_path)
      toast.push('已删除', 'success')
      if (activeRel === node.rel_path) {
        setActiveRel('')
        setText('')
        setDirty(false)
      }
      await loadTree(activeName)
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    }
  }

  const onSavePrefs = async () => {
    setBusy(true)
    try {
      const next = await updateTemplatePrefs({
        by_genre: rowsToMapping(genreRows),
        by_platform: rowsToMapping(platformRows),
      })
      mappingsDirty.current = false
      setPrefs(next)
      toast.push('开书适配已保存', 'success')
    } catch (err) {
      toast.push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const renderCreateRow = (parentRel: string, isDir: boolean, depth: number) => (
    <div
      className="tree-inline"
      style={{ paddingLeft: `calc(var(--space-2) + ${depth} * var(--space-3))` }}
    >
      <InlineEdit
        placeholder={isDir ? '目录名称' : '文件名（自动补 .md）'}
        ariaLabel={isDir ? '新建目录名称' : '新建文件名'}
        onCommit={(value) => void commitCreateNode(parentRel, isDir, value)}
        onCancel={() => setCreatingNode(null)}
      />
    </div>
  )

  const renderNode = (node: TemplateTreeNode, depth = 0) => (
    <div key={node.rel_path}>
      <div
        className={`tree-node${node.rel_path === activeRel ? ' is-active' : ''}${
          node.type === 'dir' ? ' tree-node--dir' : ''
        }`}
        style={{ paddingLeft: `calc(var(--space-2) + ${depth} * var(--space-3))` }}
      >
        {renamingNode === node.rel_path ? (
          <InlineEdit
            defaultValue={node.name}
            ariaLabel="新名称"
            onCommit={(value) => void commitRenameNode(node, value)}
            onCancel={() => setRenamingNode('')}
          />
        ) : (
          <button
            className="tree-node__name"
            type="button"
            style={{ background: 'none', border: 'none', padding: 0, textAlign: 'left', color: 'inherit', font: 'inherit' }}
            onClick={() => {
              if (node.type === 'file') void openFile(node.rel_path)
            }}
          >
            {node.type === 'dir' ? '▸ ' : ''}
            {node.name}
          </button>
        )}
        <span className="tree-node__actions">
          {node.type === 'dir' ? (
            <>
              <button
                className="btn btn--ghost btn--sm"
                type="button"
                disabled={readOnly}
                onClick={() => setCreatingNode({ parentRel: node.rel_path, isDir: false })}
              >
                +文件
              </button>
              <button
                className="btn btn--ghost btn--sm"
                type="button"
                disabled={readOnly}
                onClick={() => setCreatingNode({ parentRel: node.rel_path, isDir: true })}
              >
                +目录
              </button>
            </>
          ) : null}
          <button
            className="btn btn--ghost btn--sm"
            type="button"
            disabled={readOnly}
            onClick={() => setRenamingNode(node.rel_path)}
          >
            改名
          </button>
          <button
            className="btn btn--ghost btn--sm"
            type="button"
            disabled={readOnly}
            onClick={() => void onDeleteNode(node)}
          >
            删除
          </button>
        </span>
      </div>
      {creatingNode?.parentRel === node.rel_path
        ? renderCreateRow(node.rel_path, creatingNode.isDir, depth + 1)
        : null}
      {node.children?.map((child) => renderNode(child, depth + 1))}
    </div>
  )

  const renderMapping = (
    rows: MappingRow[],
    setRows: (rows: MappingRow[]) => void,
    keyLabel: string,
  ) => (
    <div className="stack">
      <table className="table">
        <thead>
          <tr>
            <th>{keyLabel}</th>
            <th>模板</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              <td>
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={row.key}
                  onChange={(event) => {
                    const next = rows.slice()
                    next[index] = { ...row, key: event.target.value }
                    setRows(next)
                  }}
                />
              </td>
              <td>
                <select
                  className="select"
                  value={row.value}
                  onChange={(event) => {
                    const next = rows.slice()
                    next[index] = { ...row, value: event.target.value }
                    setRows(next)
                  }}
                >
                  <option value="">（未选择）</option>
                  {templates.map((item) => (
                    <option key={item.id} value={item.name}>
                      {item.name}
                    </option>
                  ))}
                </select>
              </td>
              <td>
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  onClick={() => setRows(rows.filter((_, i) => i !== index))}
                >
                  删除
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button
        className="btn btn--sm"
        type="button"
        onClick={() => setRows([...rows, { key: '', value: '' }])}
      >
        新增一行
      </button>
    </div>
  )

  return (
    <div className="stack">
      <section className="panel">
        <header className="panel__header">
          <h3 className="panel__title">模板列表</h3>
          <div className="btn-row">
            <button className="btn btn--sm" type="button" onClick={() => setCreatingTemplate(true)}>
              新建空白模板
            </button>
            <label className="btn btn--ghost btn--sm" style={{ cursor: 'pointer' }}>
              导入模板
              <input
                type="file"
                accept="application/json"
                style={{ display: 'none' }}
                onChange={(event) => {
                  const file = event.target.files?.[0]
                  if (file) void onImport(file)
                  event.target.value = ''
                }}
              />
            </label>
          </div>
        </header>
        <div className="panel__body panel__body--tight">
          <ResourceState {...indexResource} hasData={indexResource.loaded && (templates.length > 0 || creatingTemplate)} onRetry={() => void load()} emptyTitle="暂无模板" emptyDescription="新建空白模板，或导入已有模板。">
          <table className="table">
            <thead>
              <tr>
                <th>名称</th>
                <th>来源</th>
                <th>默认</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              {creatingTemplate ? (
                <tr>
                  <td colSpan={4}>
                    <InlineEdit
                      placeholder="模板名称"
                      ariaLabel="新模板名称"
                      onCommit={(value) => void commitCreateBlank(value)}
                      onCancel={() => setCreatingTemplate(false)}
                    />
                  </td>
                </tr>
              ) : null}
              {templates.map((template) => {
                const isDefault =
                  prefs.default === template.name ||
                  (!prefs.default && template.is_builtin)
                return (
                  <tr key={template.id}>
                    <td>
                      {editingTemplate === template.name ? (
                        <InlineEdit
                          defaultValue={template.name}
                          ariaLabel="新名称"
                          onCommit={(value) => void commitRenameTemplate(template.name, value)}
                          onCancel={() => setEditingTemplate('')}
                        />
                      ) : (
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          onClick={() => void chooseTemplate(template.name)}
                        >
                          {template.name}
                        </button>
                      )}
                    </td>
                    <td>
                      <span className={template.is_builtin ? 'tag' : 'tag tag--primary'}>
                        {template.is_builtin ? '内置' : '自定义'}
                      </span>
                    </td>
                    <td>{isDefault ? <span className="tag tag--ok">默认</span> : null}</td>
                    <td>
                      <div className="btn-row">
                        <button
                          className="btn btn--sm"
                          type="button"
                          onClick={() => void onDuplicate(template)}
                        >
                          复制
                        </button>
                        <button
                          className="btn btn--sm"
                          type="button"
                          disabled={template.is_builtin}
                          onClick={() => setEditingTemplate(template.name)}
                        >
                          重命名
                        </button>
                        <button
                          className="btn btn--sm"
                          type="button"
                          disabled={isDefault}
                          onClick={() => void onSetDefault(template.name)}
                        >
                          设为默认
                        </button>
                        <button
                          className="btn btn--sm"
                          type="button"
                          onClick={() => void onExport(template)}
                        >
                          导出
                        </button>
                        <button
                          className="btn btn--danger btn--sm"
                          type="button"
                          disabled={template.is_builtin}
                          onClick={() => void onDelete(template)}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          </ResourceState>
        </div>
      </section>

      <section className="panel">
        <header className="panel__header">
          <h3 className="panel__title">
            内容编辑{active ? ` · ${active.name}` : ''}
          </h3>
          <span className="muted">
            {readOnly ? '内置模板只读，可复制后修改' : '改动仅影响模板骨架，不影响已开的书'}
          </span>
        </header>
        <div className="panel__body">
          {!active ? (
            <p className="muted">请在上方选择一个模板。</p>
          ) : (
            <div className="row" style={{ alignItems: 'flex-start', gap: 'var(--space-4)' }}>
              <div style={{ maxWidth: '100%', flex: '0 0 16rem', minWidth: '12rem' }}>
                <div className="btn-row" style={{ marginBottom: 'var(--space-2)' }}>
                  <button
                    className="btn btn--sm"
                    type="button"
                    disabled={readOnly}
                    onClick={() => setCreatingNode({ parentRel: '', isDir: false })}
                  >
                    +文件
                  </button>
                  <button
                    className="btn btn--sm"
                    type="button"
                    disabled={readOnly}
                    onClick={() => setCreatingNode({ parentRel: '', isDir: true })}
                  >
                    +目录
                  </button>
                  <button
                    className="btn btn--ghost btn--sm"
                    type="button"
                    onClick={() => void loadTree(activeName)}
                  >
                    刷新
                  </button>
                </div>
                <ResourceState {...treeResource} hasData={treeResource.loaded && !!tree} onRetry={() => void loadTree(activeName)}><div role="tree" aria-label="模板文件树">
                  {creatingNode?.parentRel === '' ? renderCreateRow('', creatingNode.isDir, 0) : null}
                  {tree?.nodes.length ? (
                    tree.nodes.map((node) => renderNode(node))
                  ) : creatingNode ? null : (
                    <p className="muted">模板为空。</p>
                  )}
                </div></ResourceState>
              </div>
              <div className="field" style={{ flex: 1 }}>
                <ResourceState {...fileResource} loading={!!fileRequested && fileResource.loading} loaded={!fileRequested || fileResource.loaded} hasData={!!activeRel || !fileRequested} onRetry={() => void openFile(fileRequested)}>
                <label className="field__label" htmlFor="template-file-content">
                  {activeRel || '（未选择文件）'}
                </label>
                <textarea
                  autoComplete={NO_AUTOFILL}
                  id="template-file-content"
                  className="textarea textarea--mono"
                  style={{ minHeight: '320px' }}
                  value={text}
                  disabled={!activeRel || readOnly}
                  onChange={(event) => {
                    setText(event.target.value)
                    setDirty(true)
                  }}
                />
                <div className="btn-row">
                  <button
                    className="btn btn--primary btn--sm"
                    type="button"
                    disabled={busy || !activeRel || readOnly || !dirty}
                    onClick={() => void onSaveFile()}
                  >
                    {busy ? '保存中…' : '保存'}
                  </button>
                  {dirty ? <span className="muted">有未保存改动</span> : null}
                </div></ResourceState>
              </div>
            </div>
          )}
        </div>
      </section>

      <section className="panel">
        <header className="panel__header">
          <h3 className="panel__title">开书适配</h3>
          <span className="muted">开书时按「显式 → 题材 → 平台 → 默认 → 内置」解析模板</span>
        </header>
        <div className="panel__body stack"><ResourceState {...prefsResource} hasData={prefsResource.loaded} onRetry={() => void load()}>
          <div className="field" style={{ maxWidth: '24rem' }}>
            <label className="field__label" htmlFor="template-default">
              默认模板
            </label>
            <select
              id="template-default"
              className="select"
              value={prefs.default}
              onChange={(event) => void onSetDefault(event.target.value)}
            >
              <option value="">默认模板（内置）</option>
              {templates.map((item) => (
                <option key={item.id} value={item.name}>
                  {item.name}
                </option>
              ))}
            </select>
          </div>

          <div>
            <p className="field__label">题材 → 模板</p>
            {renderMapping(genreRows, rows => { mappingsDirty.current = true; setGenreRows(rows) }, '题材')}
          </div>

          <div>
            <p className="field__label">平台 → 模板</p>
            {renderMapping(platformRows, rows => { mappingsDirty.current = true; setPlatformRows(rows) }, '平台')}
          </div>

          <div className="btn-row">
            <button
              className="btn btn--primary"
              type="button"
              disabled={busy}
              onClick={() => void onSavePrefs()}
            >
              保存适配规则
            </button>
          </div></ResourceState>
        </div>
      </section>

      {confirmNode}
    </div>
  )
}
