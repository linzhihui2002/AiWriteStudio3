/**
 * 文档树（左栏）：分组展示 + 新建 / 重命名 / 删除 / 拖拽移动 / 状态徽标 / 空文件浅色。
 *
 * 交互纪律：新建与改名走树内内联输入，跨分组拖拽确认走应用内弹窗，
 * 不使用 window.prompt / window.confirm 这类阻塞式原生对话框。
 * 删除策略（后端实现，此处只负责提示去向）：空文件物理删除、非空进回收站。
 */

import { useState, type DragEvent } from 'react'
import type { ProjectTree, TreeNode } from '../api/types'
import { NO_AUTOFILL } from '../lib/autofill'
import { chapterLabel } from '../lib/chapterName'
import { useConfirm } from './ConfirmDialog'

/** 章节标题长度上限（与后端 MAX_CHAPTER_TITLE_LENGTH 一致）。 */
const MAX_CHAPTER_TITLE_LENGTH = 60

export interface DocumentTreeProps {
  tree: ProjectTree | null
  activeRel: string
  onOpen: (relPath: string) => void
  onCreate: (parentRel: string, name: string, isDir: boolean) => void
  onRename: (node: TreeNode, newName: string) => void
  onDelete: (node: TreeNode) => void
  onMove: (node: TreeNode, dstParentRel: string) => void
  onSetStatus?: (node: TreeNode, status: string) => void
  pendingStatusRel?: string
}

function statusTag(node: TreeNode) {
  if (!node.is_chapter) return null
  if (node.status === '完成') return <span className="tag tag--ok">完成</span>
  if (node.status === '发表') return <span className="tag tag--primary">发表</span>
  return <span className="tag">草稿</span>
}

export default function DocumentTree({
  tree,
  activeRel,
  onOpen,
  onCreate,
  onRename,
  onDelete,
  onMove,
  onSetStatus,
  pendingStatusRel = '',
}: DocumentTreeProps) {
  const [dragNode, setDragNode] = useState<TreeNode | null>(null)
  const [overTarget, setOverTarget] = useState('')
  // 同一时刻只允许一处内联编辑：新建（分组内）或改名（节点行内）
  const [creating, setCreating] = useState<{ parentRel: string; isDir: boolean } | null>(null)
  const [renamingRel, setRenamingRel] = useState('')
  const [draft, setDraft] = useState('')
  const [confirm, confirmNode] = useConfirm()

  if (!tree) return <p className="muted">正在读取文档树…</p>

  const resetEdit = () => {
    setCreating(null)
    setRenamingRel('')
    setDraft('')
  }

  const startCreate = (parentRel: string, isDir: boolean) => {
    setRenamingRel('')
    setDraft('')
    setCreating({ parentRel, isDir })
  }

  const startRename = (node: TreeNode) => {
    setCreating(null)
    // 章节的「改名」改的是标题（序号由系统固定），因此预填标题而非文件名
    setDraft(node.is_chapter ? (node.title ?? '') : node.name)
    setRenamingRel(node.rel_path)
  }

  const submitCreate = () => {
    if (!creating) return
    const name = draft.trim()
    if (!name) return
    resetEdit()
    onCreate(creating.parentRel, name, creating.isDir)
  }

  const submitRename = (node: TreeNode) => {
    const name = draft.trim()
    resetEdit()
    if (node.is_chapter) {
      // 章节允许清空标题（回落到「第NNNN章」）
      if (name === (node.title ?? '').trim()) return
      onRename(node, name)
      return
    }
    if (!name || name === node.name) return
    onRename(node, name)
  }

  const handleDrop = async (event: DragEvent, groupRel: string) => {
    event.preventDefault()
    setOverTarget('')
    if (!dragNode) return
    const isCrossGroup = !dragNode.rel_path.startsWith(`${groupRel}/`)
    if (isCrossGroup) {
      const confirmed = await confirm({
        title: '跨分组移动',
        message: `把「${dragNode.name}」移动到「${groupRel}/」？\n跨分组移动会改变资产归属，请确认。`,
        confirmText: '移动',
      })
      if (!confirmed) {
        setDragNode(null)
        return
      }
    }
    onMove(dragNode, groupRel)
    setDragNode(null)
  }

  const renderNode = (node: TreeNode, depth = 0) => {
    const isActive = node.rel_path === activeRel
    const classes = [
      'tree-node',
      isActive ? 'is-active' : '',
      node.type === 'dir' ? 'tree-node--dir' : '',
      node.is_empty && node.type === 'file' ? 'is-empty' : '',
      overTarget === node.rel_path ? 'is-drop-target' : '',
    ]
      .filter(Boolean)
      .join(' ')

    return (
      <div key={node.rel_path}>
        <div
          className={classes}
          style={{ paddingLeft: `calc(var(--space-2) + ${depth} * var(--space-3))` }}
          role="treeitem"
          aria-selected={isActive}
          draggable
          onDragStart={() => setDragNode(node)}
          onDragEnd={() => setDragNode(null)}
          onDragOver={(event) => {
            if (node.type !== 'dir') return
            event.preventDefault()
            setOverTarget(node.rel_path)
          }}
          onDrop={(event) => {
            if (node.type === 'dir') handleDrop(event, node.rel_path)
          }}
        >
          {renamingRel === node.rel_path ? (
            <input
              className="input tree-node__rename"
              autoFocus
              autoComplete={NO_AUTOFILL}
              value={draft}
              aria-label={node.is_chapter ? '章节标题' : '新名称'}
              placeholder={node.is_chapter ? '章节标题（留空则清除）' : '新名称'}
              maxLength={node.is_chapter ? MAX_CHAPTER_TITLE_LENGTH : undefined}
              onChange={(event) => setDraft(event.target.value)}
              onFocus={(event) => event.currentTarget.select()}
              onKeyDown={(event) => {
                if (event.key === 'Enter') submitRename(node)
                if (event.key === 'Escape') resetEdit()
              }}
            />
          ) : (
            <>
              <button
                className="tree-node__name"
                type="button"
                style={{ background: 'none', border: 'none', padding: 0, textAlign: 'left', color: 'inherit', font: 'inherit' }}
                onClick={() => {
                  if (node.type === 'file') onOpen(node.rel_path)
                }}
              >
                {node.type === 'dir' ? '▸ ' : ''}
                {node.is_chapter ? chapterLabel(node) : node.name}
              </button>
              {node.is_chapter ? <span className="mono">{node.word_count ?? 0}字</span> : null}
              {node.is_chapter && onSetStatus ? <select
                className="tree-node__status"
                aria-label={`${chapterLabel(node)}状态`}
                title="修改章节状态"
                value={node.status || '草稿'}
                disabled={!!pendingStatusRel}
                draggable={false}
                onChange={event => onSetStatus(node, event.target.value)}
              >{['草稿', '完成', '发表'].map(status => <option key={status} value={status}>{status}</option>)}</select> : statusTag(node)}
            </>
          )}
          <span className="tree-node__actions">
            <button
              className="btn btn--ghost btn--sm"
              type="button"
              title={node.is_chapter ? '改章节名（标题）' : '重命名'}
              onClick={() => startRename(node)}
            >
              改名
            </button>
            <button
              className="btn btn--ghost btn--sm"
              type="button"
              title="删除（空文件物理删除 / 非空进回收站）"
              onClick={() => onDelete(node)}
            >
              删除
            </button>
          </span>
        </div>
        {node.children?.map((child) => renderNode(child, depth + 1))}
      </div>
    )
  }

  return (
    <div role="tree" aria-label="项目文档树">
      {tree.groups.map((group) => (
        <section
          key={group.key}
          className="tree-group"
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => handleDrop(event, group.rel_path)}
        >
          <header className="tree-group__head">
            <span>
              {group.name}
              {group.exists ? '' : '（未创建）'}
            </span>
            <span className="btn-row">
              <button
                className="btn btn--ghost btn--sm"
                type="button"
                title={
                  group.key === 'chapter'
                    ? '新建章节（自动命名为 第NNNN章.txt）'
                    : '在该分组新建文件'
                }
                onClick={() => startCreate(group.rel_path, false)}
              >
                {group.key === 'chapter' ? '+章节' : '+文件'}
              </button>
              <button
                className="btn btn--ghost btn--sm"
                type="button"
                title="新建文件夹"
                onClick={() => startCreate(group.rel_path, true)}
              >
                +目录
              </button>
            </span>
          </header>
          {creating?.parentRel === group.rel_path ? (
            <div className="tree-inline">
              <input
                className="input"
                autoFocus
                autoComplete={NO_AUTOFILL}
                value={draft}
                aria-label={creating.isDir ? '新目录名称' : '新文件名称'}
                placeholder={
                  creating.isDir
                    ? '目录名称'
                    : group.key === 'chapter'
                      ? '章节标题（自动命名为 第NNNN章.txt）'
                      : '文件名（自动补 .md）'
                }
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') submitCreate()
                  if (event.key === 'Escape') resetEdit()
                }}
              />
              <button
                className="btn btn--primary btn--sm"
                type="button"
                disabled={!draft.trim()}
                onClick={submitCreate}
              >
                新建
              </button>
              <button className="btn btn--ghost btn--sm" type="button" onClick={resetEdit}>
                取消
              </button>
            </div>
          ) : null}
          {group.nodes.length === 0 ? (
            <p className="muted" style={{ paddingLeft: 'var(--space-2)' }}>
              空
            </p>
          ) : (
            group.nodes.map((node) => renderNode(node))
          )}
        </section>
      ))}

      {tree.root_nodes.length > 0 ? (
        <section className="tree-group">
          <header className="tree-group__head">
            <span>项目根</span>
          </header>
          {tree.root_nodes.map((node) => renderNode(node))}
        </section>
      ) : null}

      {confirmNode}
    </div>
  )
}
