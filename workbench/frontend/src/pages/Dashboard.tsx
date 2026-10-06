/** 仪表盘：用量概览、预算与最近任务。 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { cancelTask, exportUsageCsv, getUsage, listProjects, listTasks } from '../api/client'
import type { Project, TaskRecord, UsageSummary } from '../api/types'
import Drawer from '../components/Drawer'
import Icon from '../components/Icon'
import WorkspacePage, { ResourceState, useResourceRequest, ProjectPicker } from '../components/WorkspacePage'
import { buildDailyUsage, formatTaskStatus, formatTaskType, getChartCeiling } from '../lib/dashboardState'
import { errorMessage, useToast } from '../state/useToast'
import '../styles/dashboard.css'

const DAY_OPTIONS = [7, 30, 90] as const
const numberFormatter = new Intl.NumberFormat('zh-CN')
const number = (value: number) => numberFormatter.format(value)
const cost = (value: number) => value.toFixed(4)
const price = (value: number) => new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 20 }).format(value)
const tick = (value: number) => value >= 10000 ? `${Number((value / 10000).toFixed(1))}万` : number(value)

function formatDuration(task: TaskRecord): string {
  if (!task.finished_at) return task.status === 'running' ? '进行中' : '—'
  const start = Date.parse(task.created_at.replace(' ', 'T'))
  const end = Date.parse(task.finished_at.replace(' ', 'T'))
  if (Number.isNaN(start) || Number.isNaN(end)) return '—'
  return `${Math.max(0, (end - start) / 1000).toFixed(1)} 秒`
}
function taskTagClass(status: string): string {
  if (status === 'running') return 'tag tag--primary'
  if (status === 'failed' || status === 'error') return 'tag tag--danger'
  if (status === 'done' || status === 'success' || status === 'completed') return 'tag tag--ok'
  return 'tag tag--warn'
}

function DailyUsageChart({ usage }: { usage: UsageSummary }) {
  const [inspectedDay, setInspectedDay] = useState('')
  const rows = useMemo(() => buildDailyUsage(usage.by_day, usage.days,
    usage.period ? new Date(`${usage.period.end}T00:00:00Z`) : undefined), [usage])
  const peak = rows.reduce((best, row) => row.tokens > best.tokens ? row : best, rows[0])
  const ceiling = Math.max(10, getChartCeiling(peak?.tokens ?? 0))
  const selected = rows.find(row => row.day === inspectedDay)
  const labelEvery = Math.ceil(rows.length / 7)
  const activeDays = rows.filter(row => row.calls > 0).length
  return <section className="dashboard-panel dashboard-trend" aria-labelledby="dashboard-trend-title">
    <div className="dashboard-panel__header">
      <div><h2 id="dashboard-trend-title">每日用量趋势</h2><p>近 {usage.days} 天 · {activeDays} 天有调用记录</p></div>
      <span className="dashboard-legend"><i />Token 用量</span>
    </div>
    <div className={`dashboard-chart${rows.length > 14 ? ' dashboard-chart--dense' : ''}`} role="group" aria-label={`近 ${usage.days} 天每日 Token 用量，聚焦日期查看详情`}>
      <div className="dashboard-chart__axis" aria-hidden="true"><span>{tick(ceiling)}</span><span>{tick(ceiling / 2)}</span><span>0</span></div>
      <div className="dashboard-chart__plot">
        <div className="dashboard-chart__grid" aria-hidden="true"><i /><i /><i /></div>
        <div className="dashboard-chart__columns" style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0, 1fr))` }}>
          {rows.map((row, index) => <div className="dashboard-chart__column" key={row.day}>
            <button type="button" className={`dashboard-chart__bar${row.tokens === 0 ? ' is-empty' : ''}${selected?.day === row.day ? ' is-selected' : ''}`}
              aria-label={`${row.day}：${number(row.tokens)} Token，${row.calls} 次调用`}
              onMouseEnter={() => setInspectedDay(row.day)} onMouseLeave={() => setInspectedDay('')}
              onFocus={() => setInspectedDay(row.day)} onBlur={() => setInspectedDay('')}
              onClick={() => setInspectedDay(row.day)}><span style={{ height: `${row.tokens / ceiling * 100}%` }} /></button>
            <span className={`dashboard-chart__date${index === 0 ? ' is-first' : index === rows.length - 1 ? ' is-last' : ''}`} aria-hidden="true">
              {index === 0 || index === rows.length - 1 || (index % labelEvery === 0 && index < rows.length - labelEvery / 2) ? row.day.slice(5).replace('-', '/') : ''}
            </span>
          </div>)}
        </div>
      </div>
    </div>
    <div className="dashboard-chart__summary">
      {selected ? <><span>{selected.day.split('-').join('/')}</span><strong>{number(selected.tokens)} <small>Token</small></strong><span>{number(selected.calls)} 次调用</span></>
        : <><span>日均 <strong>{number(Math.round(usage.totals.tokens / usage.days))}</strong> Token</span><span>单日峰值 <strong>{number(peak?.tokens ?? 0)}</strong> Token</span></>}
    </div>
    {usage.by_day.length === 0 && <p className="dashboard-trend__empty">所选范围暂无调用记录，开始创作后将在这里显示趋势。</p>}
  </section>
}

export default function Dashboard() {
  const { push } = useToast()
  const [days, setDays] = useState<number>(7)
  const [projectFilter, setProjectFilter] = useState('')
  const [projects, setProjects] = useState<Project[]>([])
  const [usage, setUsage] = useState<UsageSummary | null>(null)
  const [tasks, setTasks] = useState<TaskRecord[]>([])
  const [taskDetail, setTaskDetail] = useState<TaskRecord | null>(null)
  const usageResource = useResourceRequest(`${projectFilter}:${days}`)
  const tasksResource = useResourceRequest(projectFilter)
  const projectsResource = useResourceRequest('projects')
  const projectId = projectFilter === '' ? undefined : Number(projectFilter)
  const loadProjects = useCallback(async () => {
    const token = projectsResource.begin()
    try { const data = await listProjects(); if (!projectsResource.accept(token)) return; setProjects(data); projectsResource.finish(token) }
    catch (err) { projectsResource.fail(token, errorMessage(err)) }
  }, [])
  useEffect(() => { void loadProjects() }, [loadProjects])
  const loadUsage = useCallback(async () => {
    const token = usageResource.begin()
    try { const data = await getUsage(projectId, days); if (!usageResource.accept(token)) return; setUsage(data); usageResource.finish(token) }
    catch (err) { usageResource.fail(token, errorMessage(err)) }
  }, [projectId, days])
  const loadTasks = useCallback(async () => {
    const token = tasksResource.begin()
    try { const data = await listTasks(projectId, 30); if (!tasksResource.accept(token)) return; setTasks(data); tasksResource.finish(token) }
    catch (err) { tasksResource.fail(token, errorMessage(err)) }
  }, [projectId])
  useEffect(() => { void loadUsage() }, [loadUsage])
  useEffect(() => { void loadTasks() }, [loadTasks])
  const handleCancel = async (taskId: number) => {
    try { await cancelTask(taskId); push('已请求取消任务', 'success'); await loadTasks() }
    catch (err) { push(errorMessage(err), 'error') }
  }
  const handleExport = () => {
    if (!usage || !usageResource.loaded) return
    try {
      const blob = new Blob([exportUsageCsv(usage)], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url; link.download = 'usage.csv'; link.click(); URL.revokeObjectURL(url)
      push('已导出 usage.csv', 'success')
    } catch (err) { push(errorMessage(err), 'error') }
  }
  const bookName = (id: number | null) => id === null ? '未归属书籍' : projects.find(project => project.id === id)?.name || `书籍 #${id}（当前不在列表中）`
  const unitPrice = usage?.budget.currency_per_1k_tokens ?? 0
  const costConfigured = unitPrice > 0 || (usage?.totals.cost ?? 0) > 0
  const limit = usage?.budget.daily_token_limit ?? 0
  const budgetPercent = limit > 0 ? (usage?.totals.today_tokens ?? 0) / limit * 100 : 0
  const runningCount = tasks.filter(task => task.status === 'running').length
  const projectRows = useMemo(() => [...(usage?.by_project ?? [])].sort((a, b) => b.tokens - a.tokens), [usage])
  const typeRows = useMemo(() => [...(usage?.by_task_type ?? [])].sort((a, b) => b.tokens - a.tokens), [usage])
  return <WorkspacePage className="dashboard-page">
    <header className="page-header dashboard-header">
      <div><h1 className="page-header__title">仪表盘</h1><p className="page-header__desc">查看创作消耗，让每一次调用都有迹可循。</p></div>
      <div className="dashboard-header__actions">
        <button className="btn" type="button" disabled={usageResource.loading || tasksResource.loading} onClick={() => { void loadUsage(); void loadTasks() }}>刷新数据</button>
        <button className="btn" type="button" disabled={!usage || !usageResource.loaded} onClick={handleExport}><Icon name="file" size={16} />导出 CSV</button>
      </div>
    </header>
    <section className="dashboard-toolbar" aria-label="统计范围">
      <div className="dashboard-period" role="group" aria-label="统计天数">{DAY_OPTIONS.map(option => <button key={option} className={option === days ? 'is-active' : ''} type="button" aria-pressed={option === days} onClick={() => setDays(option)}>近 {option} 天</button>)}</div>
      <div className="dashboard-toolbar__book"><ResourceState {...projectsResource} hasData={projectsResource.loaded} onRetry={() => void loadProjects()}><ProjectPicker projects={projects} value={projectFilter} onChange={setProjectFilter} allowAll label="统计书籍" /></ResourceState></div>
    </section>
    <ResourceState {...usageResource} hasData={usageResource.loaded && !!usage} onRetry={() => void loadUsage()}>
      {usage && <div className="dashboard-overview">
        <div className="dashboard-metrics" aria-label="用量概览">
          <article className="dashboard-metric dashboard-metric--primary"><div className="dashboard-metric__label"><span>Token 总量</span><Icon name="chart" /></div><p className="dashboard-metric__value">{number(usage.totals.tokens)}</p><p className="dashboard-metric__note">近 {usage.days} 天累计用量</p></article>
          <article className="dashboard-metric"><div className="dashboard-metric__label"><span>调用次数</span><Icon name="workflow" /></div><p className="dashboard-metric__value">{number(usage.totals.calls)}<small>次</small></p><p className="dashboard-metric__note">平均每次 {number(usage.totals.calls ? Math.round(usage.totals.tokens / usage.totals.calls) : 0)} Token</p></article>
          <article className="dashboard-metric"><div className="dashboard-metric__label"><span>成本估算</span><Icon name="cards" /></div><p className={`dashboard-metric__value${!costConfigured ? ' dashboard-metric__value--text' : ''}`}>{costConfigured ? cost(usage.totals.cost) : '未配置单价'}</p><p className="dashboard-metric__note">{unitPrice > 0 ? '按已记录调用成本累计' : usage.totals.cost > 0 ? '含历史已计成本 · 当前未配置单价' : '设置 Token 单价后可估算成本'}</p></article>
          <article className="dashboard-metric"><div className="dashboard-metric__label"><span>今日 Token</span><Icon name="chat" /></div><p className="dashboard-metric__value">{number(usage.totals.today_tokens)}</p><p className="dashboard-metric__note">{limit > 0 ? `每日限额 ${number(limit)} Token` : '每日额度未设上限'}</p></article>
        </div>
        <div className="dashboard-analysis">
          <DailyUsageChart usage={usage} />
          <section className="dashboard-panel dashboard-budget" aria-labelledby="dashboard-budget-title">
            <div className="dashboard-panel__header"><div><h2 id="dashboard-budget-title">预算与成本</h2><p>今日用量与计费设置</p></div><Link className="dashboard-settings-link" to="/settings?section=budget" aria-label="设置预算与成本"><Icon name="settings" size={18} /></Link></div>
            <div className="dashboard-budget__body">
              <div className="dashboard-budget__status"><span className={`tag ${usage.budget.alert ? 'tag--warn' : limit > 0 ? 'tag--ok' : ''}`}>{usage.budget.alert ? '已触及告警阈值' : limit > 0 ? '预算内' : '未设限额'}</span>{limit > 0 && <strong>{Number(budgetPercent.toFixed(1))}%</strong>}</div>
              <p className="dashboard-budget__amount"><strong>{number(usage.totals.today_tokens)}</strong><span>/ {limit > 0 ? number(limit) : '不限'} Token</span></p>
              {limit > 0 ? <div className={`dashboard-budget__progress${usage.budget.alert ? ' is-warning' : ''}`} role="progressbar" aria-label="今日预算使用比例" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min(100, budgetPercent)} aria-valuetext={`已用 ${Number(budgetPercent.toFixed(1))}%`}><span style={{ width: `${Math.min(100, budgetPercent)}%` }} /><i style={{ left: `${Math.min(100, Math.max(0, usage.budget.alert_ratio * 100))}%` }} /></div> : <p className="dashboard-budget__hint">设置每日限额，可在用量接近上限时提醒。</p>}
              <dl className="dashboard-budget__details"><div><dt>告警阈值</dt><dd>{Number((usage.budget.alert_ratio * 100).toFixed(1))}%</dd></div><div><dt>每千 Token 单价</dt><dd>{unitPrice > 0 ? price(unitPrice) : '未配置'}</dd></div><div><dt>区间已计成本</dt><dd>{costConfigured ? cost(usage.totals.cost) : '—'}</dd></div></dl>
              <Link className="dashboard-budget__link" to="/settings?section=budget">管理预算与单价<Icon name="chevron" size={14} /></Link>
            </div>
          </section>
        </div>
        <div className="dashboard-breakdown">
          <section className="dashboard-panel" aria-labelledby="dashboard-books-title"><div className="dashboard-panel__header"><div><h2 id="dashboard-books-title">书籍用量</h2><p>查看每本书的创作消耗</p></div><span className="dashboard-panel__meta">{projectRows.length} 本书籍 / 归属</span></div>
            {projectRows.length === 0 ? <p className="dashboard-empty">所选范围暂无书籍用量。</p> : <div className="dashboard-table-scroll"><table className="table dashboard-table dashboard-books"><thead><tr><th scope="col">书籍</th><th scope="col">Token / 占比</th><th scope="col">调用</th><th scope="col">已计成本</th></tr></thead><tbody>{projectRows.map(row => <tr key={row.project_id ?? 'none'}><td><span className="dashboard-book-name"><Icon name="book" size={16} /><span>{bookName(row.project_id)}</span></span></td><td><div className="dashboard-share"><strong>{number(row.tokens)}</strong><small>{usage.totals.tokens ? Math.round(row.tokens / usage.totals.tokens * 100) : 0}%</small></div><div className="dashboard-share__track"><span style={{ width: `${usage.totals.tokens ? row.tokens / usage.totals.tokens * 100 : 0}%` }} /></div></td><td className="dashboard-numeric">{number(row.calls)}</td><td className="dashboard-numeric">{costConfigured ? cost(row.cost) : '—'}</td></tr>)}</tbody></table></div>}
          </section>
          <section className="dashboard-panel" aria-labelledby="dashboard-types-title"><div className="dashboard-panel__header"><div><h2 id="dashboard-types-title">任务类型分布</h2><p>按 Token 用量统计占比</p></div><Icon name="workflow" size={18} /></div>
            {typeRows.length === 0 ? <p className="dashboard-empty">所选范围暂无任务用量。</p> : <ul className="dashboard-types">{typeRows.map(row => <li key={row.task_type}><div className="dashboard-types__heading"><strong>{formatTaskType(row.task_type)}</strong><span>{usage.totals.tokens ? Math.round(row.tokens / usage.totals.tokens * 100) : 0}%</span></div><div className="dashboard-share__track"><span style={{ width: `${usage.totals.tokens ? row.tokens / usage.totals.tokens * 100 : 0}%` }} /></div><p><span>{number(row.tokens)} Token</span><span>{number(row.calls)} 次{costConfigured ? ` · 成本 ${cost(row.cost)}` : ''}</span></p></li>)}</ul>}
          </section>
        </div>
      </div>}
    </ResourceState>
    <section className="dashboard-panel dashboard-tasks" aria-labelledby="dashboard-tasks-title">
      <div className="dashboard-panel__header"><div><h2 id="dashboard-tasks-title">最近任务</h2><p>所选书籍最近 30 条记录 · 不受统计天数限制</p></div><div className="dashboard-tasks__actions">{runningCount > 0 && tasksResource.loaded && <span className="tag tag--primary">{runningCount} 项进行中</span>}<button className="btn btn--ghost btn--sm" type="button" disabled={tasksResource.loading} onClick={() => void loadTasks()}>刷新记录</button></div></div>
      <ResourceState {...tasksResource} hasData={tasksResource.loaded} onRetry={() => void loadTasks()}>
        {tasks.length === 0 ? <p className="dashboard-empty">暂无任务记录，开始创作后可在这里查看执行结果。</p> : <div className="dashboard-table-scroll"><table className="table dashboard-table dashboard-task-table"><thead><tr><th scope="col">任务 / 书籍</th><th scope="col">模型 / 引擎</th><th scope="col">状态</th><th scope="col">耗时</th><th scope="col">操作</th></tr></thead><tbody>{tasks.map(task => <tr key={task.id}>
          <td><strong>{formatTaskType(task.task_type)}</strong><span className="dashboard-cell-meta">{bookName(task.project_id)} · {task.created_at}</span></td>
          <td><span className="dashboard-model" title={task.model}>{task.model || '未记录模型'}</span><span className="dashboard-cell-meta">{task.engine || '未记录引擎'}</span></td>
          <td><span className={taskTagClass(task.status)}>{formatTaskStatus(task.status)}</span></td><td className="dashboard-numeric">{formatDuration(task)}</td>
          <td><div className="dashboard-row-actions"><button className="btn btn--ghost btn--sm" type="button" onClick={() => setTaskDetail(task)}>查看结果</button>{task.status === 'running' && <button className="btn btn--danger btn--sm" type="button" onClick={() => void handleCancel(task.id)}>取消</button>}</div></td>
        </tr>)}</tbody></table></div>}
      </ResourceState>
    </section>
    <Drawer title={taskDetail ? `任务 #${taskDetail.id} · ${formatTaskType(taskDetail.task_type)}` : '任务详情'} open={!!taskDetail} onClose={() => setTaskDetail(null)}>{taskDetail && <div className="stack"><div className="row"><span className={taskTagClass(taskDetail.status)}>{formatTaskStatus(taskDetail.status)}</span><span className="muted">{taskDetail.model || '未记录模型'} · {formatDuration(taskDetail)}</span></div><p className="muted">Prompt：{taskDetail.prompt_id || '未记录'}{taskDetail.prompt_version ? ` · ${taskDetail.prompt_version}` : ''}</p>{taskDetail.error && <p className="workspace-notice workspace-notice--error" role="alert">{taskDetail.error}</p>}<h3>执行结果</h3><pre className="run-log">{taskDetail.result == null ? '尚未记录结果' : typeof taskDetail.result === 'string' ? taskDetail.result : JSON.stringify(taskDetail.result, null, 2)}</pre><details className="workspace-disclosure"><summary tabIndex={0}>完整上下文与记录</summary><pre className="run-log">{JSON.stringify(taskDetail, null, 2)}</pre></details></div>}</Drawer>
    <p className="page-footnote dashboard-footnote">用量来自本地调用记录，成本为已配置单价下的估算值。可导出 CSV 用于对账。</p>
  </WorkspacePage>
}
