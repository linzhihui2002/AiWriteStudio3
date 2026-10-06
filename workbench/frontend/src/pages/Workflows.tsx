import WorkspacePage, { ResourceState, useResourceRequest, WorkspaceTabs, ProjectPicker } from '../components/WorkspacePage'
/** 工作流 —— 节点编排（拖拽排序）与批量产章、断点续跑。 */

import { useCallback, useEffect, useRef, useState } from 'react'
import Drawer from '../components/Drawer'
import {
  deleteWorkflow,
  duplicateWorkflow,
  getWorkflowRun,
  listWorkflowRuns,
  listProjects,
  listChapters,
  listWorkflows,
  pauseWorkflowRun,
  resumeWorkflowRun,
  saveWorkflow,
  startWorkflowRun,
} from '../api/client'
import type { ChapterSummary, Project, WorkflowDefinition, WorkflowNode, WorkflowRun } from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'
import '../styles/workflows.css'

const DEFAULT_WORKFLOW_NAME = '默认八步产章'
const ENGINE_OPTIONS = ['', 'dsh-headless', 'direct-api'] as const

function runStatusLabel(status: string): string {
  return ({ pending: '待运行', queued: '排队中', running: '运行中', paused: '已暂停',
    failed: '失败', interrupted: '已中断', done: '已完成', cancelled: '已停止',
    error: '失败' } as Record<string, string>)[status] ?? status
}

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

  const [tab, setTab] = useState<'definition' | 'run' | 'history'>('run')
  const [projects, setProjects] = useState<Project[]>([])
  const workflowResource = useResourceRequest('workflows')
  const projectResource = useResourceRequest('projects')
  const currentRef = useRef('')
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
  const [chapterMode, setChapterMode] = useState<'first' | 'range' | 'selected'>('first')
  const [runChapters, setRunChapters] = useState<string[]>([])
  const [chapterChoices, setChapterChoices] = useState<ChapterSummary[]>([])
  const [chapterQuery, setChapterQuery] = useState('')
  const chaptersResource = useResourceRequest(runProject)
  const [runWorkflow, setRunWorkflow] = useState('')
  const [runExecute, setRunExecute] = useState(false)
  const [runAction, setRunAction] = useState('')

  const [runProjectFilter, setRunProjectFilter] = useState('')
  const runsResource = useResourceRequest(runProjectFilter)
  currentRef.current = current
  const [runs, setRuns] = useState<WorkflowRun[]>([])
  const [runDetail, setRunDetail] = useState<WorkflowRun | null>(null)
  const [runDetailId, setRunDetailId] = useState<number | null>(null)
  const detailResource = useResourceRequest(`${runProjectFilter}:${runDetailId}`)

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
    const token = workflowResource.begin()
    try {
      const data = await listWorkflows()
      if (!workflowResource.accept(token)) return
      setWorkflows(data.workflows)
      setNodeTypes(data.node_types)
      const preferred =
        data.workflows.find((item) => item.name === DEFAULT_WORKFLOW_NAME) ?? data.workflows[0]
      if (preferred && !currentRef.current) applyDefinition(preferred)
      workflowResource.finish(token)
    } catch (err) {
      workflowResource.fail(token, errorMessage(err))
    }
  }, [applyDefinition, push])

  const loadRuns = useCallback(async () => {
    const token = runsResource.begin()
    try {
      const data = await listWorkflowRuns(runProjectFilter === '' ? undefined : Number(runProjectFilter))
      if (!runsResource.accept(token)) return
      setRuns(data); runsResource.finish(token)
    } catch (err) { runsResource.fail(token, errorMessage(err)) }
  }, [runProjectFilter])
  const loadProjects = useCallback(async () => {
    const token = projectResource.begin()
    try {
      const data = await listProjects()
      if (!projectResource.accept(token)) return
      setProjects(data)
      setRunProject(value => value || (data[0] ? String(data[0].id) : ''))
      projectResource.finish(token)
    } catch (err) { projectResource.fail(token, errorMessage(err)) }
  }, [])
  useEffect(() => { void loadProjects() }, [loadProjects])
  const loadChapters = useCallback(async () => {
    if (!runProject) return
    const token = chaptersResource.begin()
    try {
      const data = await listChapters(Number(runProject))
      if (!chaptersResource.accept(token)) return
      const sorted = [...data].sort((left, right) => (left.number ?? Number.MAX_SAFE_INTEGER) - (right.number ?? Number.MAX_SAFE_INTEGER) || left.rel_path.localeCompare(right.rel_path))
      setChapterChoices(sorted)
      setRunChapters(current_ => current_.filter(path => data.some(chapter => chapter.rel_path === path)))
      setRunStart(current_ => data.some(chapter => String(chapter.number) === current_) ? current_ : '')
      setRunEnd(current_ => data.some(chapter => String(chapter.number) === current_) ? current_ : '')
      chaptersResource.finish(token)
    } catch (err) { chaptersResource.fail(token, errorMessage(err)) }
  }, [runProject])
  useEffect(() => {
    setChapterChoices([]); setRunChapters([]); setRunStart(''); setRunEnd(''); setChapterQuery(''); setChapterMode('first')
    void loadChapters()
  }, [runProject, loadChapters])
  const running = runs.some(run => ['running', 'queued'].includes(run.status))
  useEffect(() => { if (!running) return; const timer = window.setInterval(() => void loadRuns(), 2500); return () => window.clearInterval(timer) }, [running, loadRuns])

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
      push('请先选择书籍', 'error')
      return
    }
    if (runWorkflow === '') {
      push('请先选择工作流', 'error')
      return
    }
    if (!chaptersResource.loaded || chapterChoices.length === 0) {
      push('请先读取本书章节', 'error')
      return
    }
    if (chapterMode === 'range' && (!runStart || !runEnd || Number(runStart) > Number(runEnd))) {
      push('请选择有效的章节范围，结束章节不能早于起始章节', 'error')
      return
    }
    if (chapterMode === 'selected' && runChapters.length === 0) {
      push('请至少选择一章', 'error')
      return
    }
    setBusy(true)
    try {
      const run = await startWorkflowRun({
        workflow: runWorkflow,
        project_id: Number(runProject),
        chapter_start: chapterMode === 'range' ? Number(runStart) : null,
        chapter_end: chapterMode === 'range' ? Number(runEnd) : null,
        chapters: chapterMode === 'selected' ? chapterChoices.filter(chapter => runChapters.includes(chapter.rel_path)).map(chapter => chapter.rel_path) : [],
        execute: runExecute,
        use_ai: true,
      })
      if (run.status === 'failed') {
        const failure = [...run.log].reverse().find(entry => typeof entry.error === 'string' && entry.error)
        push(`已创建运行 #${run.id}，执行未完成${failure ? `：${String(failure.error)}` : '，请查看失败节点后重试'}`, 'error')
      } else {
        push(`已创建运行 #${run.id}（${runStatusLabel(run.status)}）`,
          ['paused', 'interrupted'].includes(run.status) ? 'info' : 'success')
      }
      setRunProjectFilter(String(run.project_id))
      setTab('history')
      await loadRuns()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy(false)
    }
  }

  const handlePause = async (runId: number) => {
    setRunAction(`pause:${runId}`)
    try {
      const updated = await pauseWorkflowRun(runId)
      setRunDetail(current_ => current_?.id === runId ? updated : current_)
      push('已暂停运行', 'success')
      await loadRuns()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally { setRunAction('') }
  }

  const handleResume = async (runId: number) => {
    setRunAction(`resume:${runId}`)
    try {
      const updated = await resumeWorkflowRun(runId)
      setRunDetail(current_ => current_?.id === runId ? updated : current_)
      if (updated.status === 'done') {
        push('续跑已完成，已完成章节没有重复生成', 'success')
      } else if (updated.status === 'failed') {
        const failure = [...updated.log].reverse().find(entry => typeof entry.error === 'string' && entry.error)
        push(`续跑未完成${failure ? `：${String(failure.error)}` : '，请查看失败节点后重试'}`, 'error')
      } else if (updated.status === 'paused' || updated.status === 'interrupted') {
        push(`${runStatusLabel(updated.status)}，已完成内容保留，可稍后恢复续跑`, 'info')
      } else {
        push(`工作流${runStatusLabel(updated.status)}`, 'info')
      }
      await loadRuns()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally { setRunAction('') }
  }

  const loadRunDetail = useCallback(async () => {
    if (runDetailId === null) return
    const token = detailResource.begin()
    try { const data = await getWorkflowRun(runDetailId); if (!detailResource.accept(token)) return; setRunDetail(data); detailResource.finish(token) } catch (err) { detailResource.fail(token, errorMessage(err)) }
  }, [runDetailId, runProjectFilter])
  useEffect(() => { if (runDetailId !== null) void loadRunDetail() }, [runDetailId, loadRunDetail])
  useEffect(() => { setRunDetailId(null); setRunDetail(null) }, [runProjectFilter])
  const handleViewRun = (runId: number) => setRunDetailId(runId)

  const detailStatuses = runDetail ? nodeStatuses(runDetail) : []
  const numberedChapters = chapterChoices.filter(chapter => chapter.number !== null)
  const visibleChapterChoices = chapterChoices.filter(chapter => `${chapter.title} ${chapter.file_name} ${chapter.number ?? ''}`.toLocaleLowerCase().includes(chapterQuery.trim().toLocaleLowerCase()))
  const chapterLabel = (chapter: ChapterSummary) => `${chapter.number === null ? chapter.file_name : `第${chapter.number}章`}${chapter.title ? ` · ${chapter.title}` : ''}`

  return (
    <WorkspacePage>
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

      <WorkspaceTabs label="工作流工作区" items={[{key:'run',label:'开始运行'}, {key:'definition',label:'编辑流程'}, {key:'history',label:'运行记录'}]} value={tab} onChange={setTab} />
      <ResourceState {...workflowResource} hasData={workflowResource.loaded && workflows.length > 0} onRetry={() => void loadWorkflows()} emptyTitle="暂无工作流" emptyDescription="读取成功后仍没有可用工作流，请检查工作流配置。">
      {tab === 'definition' && <><p className="muted">编辑节点与顺序；内置流程请复制后修改。</p>

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
                  <div className="btn-row"><button className="btn btn--ghost btn--sm" disabled={index === 0} aria-label={`上移节点 ${node.label || index + 1}`} onClick={() => setNodes(values => { const next = [...values]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; return next })}>上移</button><button className="btn btn--ghost btn--sm" disabled={index === nodes.length - 1} aria-label={`下移节点 ${node.label || index + 1}`} onClick={() => setNodes(values => { const next = [...values]; [next[index + 1], next[index]] = [next[index], next[index + 1]]; return next })}>下移</button>
                  <button
                    className="btn btn--danger btn--sm"
                    type="button"
                    onClick={() => removeNode(index)}
                  >
                    删除节点
                  </button></div>
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

      </>}
      {tab === 'run' && <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">批量产章</h2>
        </div>
        <div className="panel__body stack">
          <div className="row">
            <ResourceState {...projectResource} hasData={projectResource.loaded && projects.length > 0} onRetry={() => void loadProjects()} emptyTitle="还没有可用书籍" emptyDescription="先从书架创建一本书。"><ProjectPicker projects={projects} value={runProject} onChange={setRunProject} /></ResourceState>
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

          {runProject ? <ResourceState {...chaptersResource} hasData={chaptersResource.loaded && chapterChoices.length > 0} onRetry={() => void loadChapters()} emptyTitle="本书还没有章节" emptyDescription="先在正文编辑器创建章节，再选择要运行的章节。">
            <fieldset className="workflow-chapter-picker">
              <legend className="field__label">运行章节</legend>
              <button className="btn btn--ghost btn--sm" type="button" onClick={() => void loadChapters()} disabled={chaptersResource.loading}>刷新章节</button>
              <div className="workflow-chapter-picker__modes">
                {([{key:'first',label:'第一章'}, {key:'range',label:'章节范围'}, {key:'selected',label:'指定章节'}] as const).map(item => <label className="radio" key={item.key}><input type="radio" name="workflow-chapter-mode" value={item.key} checked={chapterMode === item.key} onChange={() => setChapterMode(item.key)} />{item.label}</label>)}
              </div>
              {chapterMode === 'first' ? <p className="field__hint">从 {chapterChoices[0] ? chapterLabel(chapterChoices[0]) : '第一章'} 开始，仅运行这一章。</p> : null}
              {chapterMode === 'range' ? <div className="row">
                <label className="field"><span className="field__label">起始章节</span><select aria-label="起始章节" className="select" value={runStart} onChange={event => setRunStart(event.target.value)}><option value="">请选择章节</option>{numberedChapters.map(chapter => <option key={chapter.rel_path} value={String(chapter.number)}>{chapterLabel(chapter)}</option>)}</select></label>
                <label className="field"><span className="field__label">结束章节</span><select aria-label="结束章节" className="select" value={runEnd} onChange={event => setRunEnd(event.target.value)}><option value="">请选择章节</option>{numberedChapters.map(chapter => <option key={chapter.rel_path} value={String(chapter.number)}>{chapterLabel(chapter)}</option>)}</select></label>
                {!numberedChapters.length ? <p className="field__hint">本书章节没有章号，请使用“指定章节”。</p> : null}
              </div> : null}
              {chapterMode === 'selected' ? <div className="stack">
                <label className="field"><span className="field__label">筛选章节</span><input className="input" value={chapterQuery} onChange={event => setChapterQuery(event.target.value)} autoComplete={NO_AUTOFILL} placeholder="搜索章节标题或章号" /></label>
                <div className="btn-row"><button className="btn btn--sm" type="button" disabled={!visibleChapterChoices.length} onClick={() => setRunChapters(current_ => [...new Set([...current_, ...visibleChapterChoices.map(chapter => chapter.rel_path)])])}>选择当前结果</button><button className="btn btn--ghost btn--sm" type="button" disabled={!runChapters.length} onClick={() => setRunChapters([])}>清除选择</button><span className="muted" role="status">已选 {runChapters.length} 章</span></div>
                <div className="workflow-chapter-picker__list">{visibleChapterChoices.map(chapter => <label className="checkbox" key={chapter.rel_path}><input type="checkbox" checked={runChapters.includes(chapter.rel_path)} onChange={event => setRunChapters(current_ => event.target.checked ? [...current_, chapter.rel_path] : current_.filter(path => path !== chapter.rel_path))} /><span>{chapterLabel(chapter)}<small className="muted">{chapter.rel_path}</small></span></label>)}{!visibleChapterChoices.length ? <p className="muted">没有匹配的章节，清除搜索后查看本书其他章节。</p> : null}</div>
              </div> : null}
            </fieldset>
          </ResourceState> : null}

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
              disabled={busy || !chaptersResource.loaded || chapterChoices.length === 0 || (chapterMode === 'selected' && !runChapters.length) || (chapterMode === 'range' && (!runStart || !runEnd || Number(runStart) > Number(runEnd)))}
              onClick={() => void handleStartRun()}
            >
              开始
            </button>
          </div>
        </div>
      </section>}

      </ResourceState>
      {tab === 'history' && <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">运行记录</h2>
          <div className="row">
            <ProjectPicker projects={projects} value={runProjectFilter} onChange={setRunProjectFilter} allowAll label="运行范围" />
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => void loadRuns()}>
              刷新
            </button>
          </div>
        </div>
        <div className="panel__body panel__body--tight">
          <ResourceState {...runsResource} hasData={runsResource.loaded && runs.length > 0} onRetry={() => void loadRuns()}>
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
                      <span className={runTagClass(run.status)}>{runStatusLabel(run.status)}</span>
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
                          disabled={run.status !== 'running' || !!runAction}
                          onClick={() => void handlePause(run.id)}
                        >
                          {runAction === `pause:${run.id}` ? '暂停中…' : '暂停'}
                        </button>
                        <button
                          className="btn btn--ghost btn--sm"
                          type="button"
                          disabled={!(['pending', 'paused', 'failed', 'interrupted'].includes(run.status)) || !!runAction}
                          onClick={() => void handleResume(run.id)}
                        >
                          {runAction === `resume:${run.id}` ? '执行中…' : run.status === 'pending' ? '开始运行' : '恢复续跑'}
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
          </ResourceState>
        </div>
      </section>}

      <p className="page-footnote">工作流定义保存在本地，运行进度可随时回看。</p>

      <Drawer
        title={runDetail ? `运行 #${runDetail.id} 详情` : '运行详情'}
        open={runDetailId !== null}
        onClose={() => { setRunDetailId(null); setRunDetail(null) }}
        width={860}
      >
        <ResourceState {...detailResource} hasData={detailResource.loaded && runDetail?.id === runDetailId} onRetry={() => void loadRunDetail()}>
        {runDetail?.id === runDetailId ? (
          <div className="stack">
            <div className="row">
              <span className={runTagClass(runDetail.status)}>{runStatusLabel(runDetail.status)}</span>
              <span className="muted">
                {projects.find(project => project.id === runDetail.project_id)?.name || `书籍 #${runDetail.project_id}`}  · 完成 {runDetail.progress.completed.length}/
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
              <ol className="workspace-log-list">{runDetail.log.map((entry, index) => <li key={index}><span className="muted">{String(entry.at ?? entry.created_at ?? index + 1)}</span><strong>{String(entry.event ?? entry.status ?? entry.node ?? '执行记录')}</strong><span>{String(entry.chapter ?? entry.rel_path ?? entry.message ?? entry.error ?? entry.note ?? '')}</span></li>)}</ol><details className="workspace-disclosure"><summary>完整运行数据</summary><pre className="run-log">{JSON.stringify(runDetail.log, null, 2)}</pre></details>
            </div>
          </div>
        ) : null}</ResourceState>
      </Drawer>
    </WorkspacePage>
  )
}
