/** 工作流 —— 节点编排（拖拽排序）与批量产章、断点续跑。 */

import { useCallback, useEffect, useState } from 'react'
import Modal from '../components/Modal'
import {
  deleteWorkflow,
  duplicateWorkflow,
  getWorkflowRun,
  listWorkflowRuns,
  listWorkflows,
  pauseWorkflowRun,
  resumeWorkflowRun,
  saveWorkflow,
  startWorkflowRun,
} from '../api/client'
import type { WorkflowDefinition, WorkflowNode, WorkflowRun } from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

const DEFAULT_WORKFLOW_NAME = '默认八步产章'
const ENGINE_OPTIONS = ['', 'dsh-headless', 'direct-api'] as const

function runTagClass(status: string): string {
  if (status === 'done') return 'tag tag--ok'
  if (status === 'failed') return 'tag tag--danger'
  if (status === 'running') return 'tag tag--primary'
  return 'tag tag--warn'
}

/** 从运行日志中提取最近一次章节执行的节点状态。 */
function nodeStatuses(run: WorkflowRun): Array<{ node: string; status: string }> {
  const result: Array<{ node: string; status: string }> = []
  for (let index = run.log.length - 1; index >= 0; index -= 1) {
    const entry = run.log[index]
    const steps = entry.steps
    if (steps && typeof steps === 'object' && !Array.isArray(steps)) {
      Object.entries(steps as Record<string, unknown>).forEach(([node, status]) => {
        result.push({ node, status: status === null || status === undefined ? '未跑' : String(status) })
      })
      break
    }
    const nodes = entry.nodes
    if (Array.isArray(nodes)) {
      nodes.forEach((item) => {
        if (item && typeof item === 'object') {
          const record = item as Record<string, unknown>
          result.push({
            node: String(record.node ?? ''),
            status: record.ok ? '已完成' : '失败',
          })
        }
      })
      break
    }
  }
  return result
}

export default function Workflows() {
  const { push } = useToast()

  const [workflows, setWorkflows] = useState<WorkflowDefinition[]>([])
  const [nodeTypes, setNodeTypes] = useState<string[]>([])
  const [current, setCurrent] = useState('')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [nodes, setNodes] = useState<WorkflowNode[]>([])
  const [stopOnFailure, setStopOnFailure] = useState(false)
  const [isBuiltin, setIsBuiltin] = useState(false)
  const [newName, setNewName] = useState('')
  const [dragIndex, setDragIndex] = useState<number | null>(null)
  const [overIndex, setOverIndex] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  const [runProject, setRunProject] = useState('')
  const [runStart, setRunStart] = useState('')
  const [runEnd, setRunEnd] = useState('')
  const [runChapters, setRunChapters] = useState('')
  const [runWorkflow, setRunWorkflow] = useState('')
  const [runExecute, setRunExecute] = useState(false)

  const [runProjectFilter, setRunProjectFilter] = useState('')
  const [runs, setRuns] = useState<WorkflowRun[]>([])
  const [runDetail, setRunDetail] = useState<WorkflowRun | null>(null)

  const applyDefinition = useCallback((definition: WorkflowDefinition) => {
    setCurrent(definition.name)
    setName(definition.name)
    setDescription(definition.description)
    setNodes(definition.nodes.map((node) => ({ ...node })))
    setStopOnFailure(Boolean(definition.batch?.stop_on_failure))
    setIsBuiltin(definition.is_builtin)
    setNewName(`${definition.name}-副本`)
    setRunWorkflow(definition.name)
  }, [])

  const loadWorkflows = useCallback(async () => {
    try {
      const data = await listWorkflows()
      setWorkflows(data.workflows)
      setNodeTypes(data.node_types)
      const preferred =
        data.workflows.find((item) => item.name === DEFAULT_WORKFLOW_NAME) ?? data.workflows[0]
      if (preferred) applyDefinition(preferred)
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [applyDefinition, push])

  const loadRuns = useCallback(async () => {
    try {
      setRuns(
        await listWorkflowRuns(runProjectFilter === '' ? undefined : Number(runProjectFilter)),
      )
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [runProjectFilter, push])

  useEffect(() => {
    void loadWorkflows()
  }, [loadWorkflows])

  useEffect(() => {
    void loadRuns()
  }, [loadRuns])

  const selectWorkflow = (selected: string) => {
    const found = workflows.find((item) => item.name === selected)
    if (found) applyDefinition(found)
  }

  const updateNode = (index: number, patch: Partial<WorkflowNode>) => {
    setNodes((current_) =>
      current_.map((node, nodeIndex) => (nodeIndex === index ? { ...node, ...patch } : node)),
    )
  }

  const handleDrop = (target: number) => {
    if (dragIndex === null || dragIndex === target) {
      setDragIndex(null)
      setOverIndex(null)
      return
    }
    setNodes((current_) => {
      const next = [...current_]
      const [moved] = next.splice(dragIndex, 1)
      next.splice(target, 0, moved)
      return next
    })
    setDragIndex(null)
    setOverIndex(null)
  }

  const addNode = () => {
    const type = nodeTypes[0] ?? 'DRAFT'
    const id = `node-${Date.now().toString(36)}`
    setNodes((current_) => [
      ...current_,
      { id, type, label: type, engine: '', model: '', skill: '', next: '' },
    ])
  }

  const removeNode = (index: number) => {
    setNodes((current_) => current_.filter((_, nodeIndex) => nodeIndex !== index))
  }

  const handleSave = async () => {
    setBusy(true)
    try {
      const definition = await saveWorkflow({
        name,
        description,
        nodes,
        batch: { chapters: [], stop_on_failure: stopOnFailure },
      })
      push('已保存工作流', 'success')
      await loadWorkflows()
      applyDefinition(definition)
    } catch (err) {
      const message = errorMessage(err)
      push(
        isBuiltin ? `${message} 建议：点击「复制」另存为新名称后再保存。` : message,
        'error',
      )
    } finally {
      setBusy(false)
    }
  }

  const handleDuplicate = async () => {
    setBusy(true)
    try {
      const definition = await duplicateWorkflow(current, newName)
      push(`已复制为「${definition.name}」`, 'success')
      await loadWorkflows()
      applyDefinition(definition)
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handleDelete = async () => {
    setBusy(true)
    try {
      await deleteWorkflow(current)
      push('已删除工作流', 'success')
      await loadWorkflows()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handleStartRun = async () => {
    if (runProject.trim() === '' || Number.isNaN(Number(runProject))) {
      push('请先填写有效的项目 ID', 'error')
      return
    }
    if (runWorkflow === '') {
      push('请先选择工作流', 'error')
      return
    }
    setBusy(true)
    try {
      const run = await startWorkflowRun({
        workflow: runWorkflow,
        project_id: Number(runProject),
        chapter_start: runStart.trim() === '' ? null : Number(runStart),
        chapter_end: runEnd.trim() === '' ? null : Number(runEnd),
        chapters: runChapters
          .split(/[,，\s]+/)
          .map((item) => item.trim())
          .filter((item) => item !== ''),
        execute: runExecute,
        use_ai: true,
      })
      push(`已创建运行 #${run.id}（${run.status}）`, 'success')
      await loadRuns()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handlePause = async (runId: number) => {
    try {
      await pauseWorkflowRun(runId)
      push('已暂停运行', 'success')
      await loadRuns()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleResume = async (runId: number) => {
    try {
      await resumeWorkflowRun(runId)
      push('已续跑（已完成章节不重复生成）', 'success')
      await loadRuns()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleViewRun = async (runId: number) => {
    try {
      setRunDetail(await getWorkflowRun(runId))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const detailStatuses = runDetail ? nodeStatuses(runDetail) : []

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">工作流</h1>
          <p className="page-header__desc">
            可视化编排产章流程，支持批量产章与断点续跑。
          </p>
        </div>
        <button className="btn" type="button" onClick={() => void loadWorkflows()}>
          重新加载
        </button>
      </header>

      <p className="muted">内置默认流程可直接运行。</p>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">选择工作流</h2>
          {isBuiltin ? <span className="tag tag--warn">内置（不可覆盖 / 删除）</span> : null}
        </div>
        <div className="panel__body stack">
          <div className="field">
            <label className="field__label" htmlFor="workflow-select">
              工作流
            </label>
            <select
              id="workflow-select"
              className="select"
              value={current}
              onChange={(event) => selectWorkflow(event.target.value)}
            >
              {workflows.map((item) => (
                <option key={item.name} value={item.name}>
                  {item.name}
                  {item.is_builtin ? '（内置）' : ''}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="workflow-name">
              名称
            </label>
            <input
              autoComplete={NO_AUTOFILL}
              id="workflow-name"
              className="input"
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </div>

          <div className="field">
            <label className="field__label" htmlFor="workflow-desc">
              说明
            </label>
            <input
              autoComplete={NO_AUTOFILL}
              id="workflow-desc"
              className="input"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
            />
          </div>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={stopOnFailure}
              onChange={(event) => setStopOnFailure(event.target.checked)}
            />
            单章失败即停止整批
          </label>

          <div className="btn-row">
            <button
              className="btn btn--primary"
              type="button"
              disabled={busy}
              onClick={() => void handleSave()}
            >
              保存
            </button>
            <button
              className="btn"
              type="button"
              disabled={busy || newName.trim() === ''}
              onClick={() => void handleDuplicate()}
            >
              复制
            </button>
            <button
              className="btn btn--danger"
              type="button"
              disabled={busy || isBuiltin}
              onClick={() => void handleDelete()}
            >
              删除
            </button>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="workflow-copy-name">
              副本名称
            </label>
            <input
              autoComplete={NO_AUTOFILL}
              id="workflow-copy-name"
              className="input"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
            />
            <span className="field__hint">内置工作流不可覆盖，请先复制后再保存。</span>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">节点编排</h2>
          <span className="muted">拖动左侧手柄可调整顺序</span>
        </div>
        <div className="panel__body stack">
          {nodes.length === 0 ? (
            <p className="muted">当前工作流没有节点，请先添加节点。</p>
          ) : (
            <div className="flow-list">
              {nodes.map((node, index) => (
                <div
                  key={node.id}
                  className={`flow-node${dragIndex === index ? ' is-dragging' : ''}${
                    overIndex === index && dragIndex !== index ? ' is-over' : ''
                  }`}
                  draggable
                  onDragStart={() => setDragIndex(index)}
                  onDragOver={(event) => {
                    event.preventDefault()
                    setOverIndex(index)
                  }}
                  onDrop={(event) => {
                    event.preventDefault()
                    handleDrop(index)
                  }}
                  onDragEnd={() => {
                    setDragIndex(null)
                    setOverIndex(null)
                  }}
                >
                  <span className="flow-node__handle" title="拖动排序" aria-hidden="true">
                    ≡
                  </span>
                  <div className="flow-node__body">
                    <select
                      className="select"
                      aria-label="节点类型"
                      value={node.type}
                      onChange={(event) => updateNode(index, { type: event.target.value })}
                    >
                      {nodeTypes.map((type) => (
                        <option key={type} value={type}>
                          {type}
                        </option>
                      ))}
                    </select>
                    <input
                      autoComplete={NO_AUTOFILL}
                      className="input"
                      aria-label="节点标签"
                      placeholder="标签"
                      value={node.label}
                      onChange={(event) => updateNode(index, { label: event.target.value })}
                    />
                    <select
                      className="select"
                      aria-label="引擎"
                      value={node.engine}
                      onChange={(event) => updateNode(index, { engine: event.target.value })}
                    >
                      {ENGINE_OPTIONS.map((option) => (
                        <option key={option || 'default'} value={option}>
                          {option === '' ? '默认引擎' : option}
                        </option>
                      ))}
                    </select>
                    <input
                      autoComplete={NO_AUTOFILL}
                      className="input"
                      aria-label="模型"
                      placeholder="模型（可空）"
                      value={node.model}
                      onChange={(event) => updateNode(index, { model: event.target.value })}
                    />
                    <input
                      autoComplete={NO_AUTOFILL}
                      className="input"
                      aria-label="技能"
                      placeholder="技能（可空）"
                      value={node.skill}
                      onChange={(event) => updateNode(index, { skill: event.target.value })}
                    />
                  </div>
                  <button
                    className="btn btn--danger btn--sm"
                    type="button"
                    onClick={() => removeNode(index)}
                  >
                    删除节点
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="btn-row">
            <button className="btn" type="button" onClick={addNode}>
              添加节点
            </button>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">批量产章</h2>
        </div>
        <div className="panel__body stack">
          <div className="row">
            <div className="field">
              <label className="field__label" htmlFor="run-project">
                项目 ID
              </label>
              <input
                id="run-project"
                className="input"
                type="number"
                min={1}
                value={runProject}
                onChange={(event) => setRunProject(event.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="run-workflow">
                工作流
              </label>
              <select
                id="run-workflow"
                className="select"
                value={runWorkflow}
                onChange={(event) => setRunWorkflow(event.target.value)}
              >
                {workflows.map((item) => (
                  <option key={item.name} value={item.name}>
                    {item.name}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className="row">
            <div className="field">
              <label className="field__label" htmlFor="run-start">
                起始章号
              </label>
              <input
                id="run-start"
                className="input"
                type="number"
                min={1}
                value={runStart}
                onChange={(event) => setRunStart(event.target.value)}
              />
            </div>
            <div className="field">
              <label className="field__label" htmlFor="run-end">
                结束章号
              </label>
              <input
                id="run-end"
                className="input"
                type="number"
                min={1}
                value={runEnd}
                onChange={(event) => setRunEnd(event.target.value)}
              />
            </div>
          </div>

          <div className="field">
            <label className="field__label" htmlFor="run-chapters">
              或指定章节路径（逗号分隔）
            </label>
            <textarea
              autoComplete={NO_AUTOFILL}
              id="run-chapters"
              className="textarea textarea--mono"
              placeholder="例如：正文/0001-开篇.md，正文/0002-入城.md"
              value={runChapters}
              onChange={(event) => setRunChapters(event.target.value)}
            />
            <span className="field__hint">
              填写章节路径时优先按路径执行；两者都空则从第一章开始。
            </span>
          </div>

          <label className="checkbox">
            <input
              type="checkbox"
              checked={runExecute}
              onChange={(event) => setRunExecute(event.target.checked)}
            />
            立即执行（不勾选仅创建运行记录）
          </label>

          <div className="btn-row">
            <button
              className="btn btn--primary"
              type="button"
              disabled={busy}
              onClick={() => void handleStartRun()}
            >
              开始
            </button>
          </div>
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">运行记录</h2>
          <div className="row">
            <input
              className="input"
              type="number"
              min={1}
              placeholder="项目 ID（可空）"
              aria-label="运行记录项目过滤"
              value={runProjectFilter}
              onChange={(event) => setRunProjectFilter(event.target.value)}
            />
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => void loadRuns()}>
              刷新
            </button>
          </div>
        </div>
        <div className="panel__body panel__body--tight">
          {runs.length === 0 ? (
            <p className="muted">暂无运行记录。</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>ID</th>
                  <th>状态</th>
                  <th>进度</th>
                  <th>时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((run) => (
                  <tr key={run.id}>
                    <td className="mono">{run.id}</td>
                    <td>
                      <span className={runTagClass(run.status)}>{run.status}</span>
                    </td>
                    <td className="mono">
                      完成 {run.progress.completed.length}/{run.progress.chapters_total} 章
                    </td>
                    <td className="mono">{run.updated_at ?? run.created_at}</td>
                    <td>
                      <div className="btn-row">
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          onClick={() => void handlePause(run.id)}
                        >
                          暂停
                        </button>
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          onClick={() => void handleResume(run.id)}
                        >
                          恢复续跑
                        </button>
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          onClick={() => void handleViewRun(run.id)}
                        >
                          查看详情
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <p className="page-footnote">工作流定义保存在本地。</p>

      <Modal
        title={runDetail ? `运行 #${runDetail.id} 详情` : '运行详情'}
        open={runDetail !== null}
        onClose={() => setRunDetail(null)}
        width={860}
      >
        {runDetail ? (
          <div className="stack">
            <div className="row">
              <span className={runTagClass(runDetail.status)}>{runDetail.status}</span>
              <span className="muted">
                项目 #{runDetail.project_id} · 完成 {runDetail.progress.completed.length}/
                {runDetail.progress.chapters_total} 章
              </span>
              <span className="muted mono">{runDetail.updated_at ?? runDetail.created_at}</span>
            </div>

            <div>
              <p className="muted">章节进度</p>
              <ul className="notice__list">
                {runDetail.progress.completed.length === 0 ? (
                  <li className="muted">尚无已完成章节</li>
                ) : (
                  runDetail.progress.completed.map((chapter) => (
                    <li key={chapter} className="mono">
                      {chapter}
                    </li>
                  ))
                )}
              </ul>
            </div>

            <div>
              <p className="muted">节点状态</p>
              {detailStatuses.length === 0 ? (
                <p className="muted">尚无节点执行记录。</p>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>节点</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {detailStatuses.map((item, index) => (
                      <tr key={`${item.node}-${index}`}>
                        <td className="mono">{item.node}</td>
                        <td>{item.status}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>

            <div>
              <p className="muted">运行日志</p>
              <div className="run-log">{JSON.stringify(runDetail.log, null, 2)}</div>
            </div>
          </div>
        ) : null}
      </Modal>
    </div>
  )
}