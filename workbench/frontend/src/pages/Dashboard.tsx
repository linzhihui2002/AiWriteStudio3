/** 仪表盘 —— Token 用量、成本估算、最近任务留痕与 CSV 导出。 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { cancelTask, exportUsageCsv, getUsage, listProjects, listTasks } from '../api/client'
import type { Project, TaskRecord, UsageSummary } from '../api/types'
import { errorMessage, useToast } from '../state/useToast'

const DAY_OPTIONS = [7, 30, 90] as const

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
  if (status === 'done' || status === 'success') return 'tag tag--ok'
  return 'tag tag--warn'
}

export default function Dashboard() {
  const { push } = useToast()

  const [days, setDays] = useState<number>(7)
  const [projectFilter, setProjectFilter] = useState('')
  const [projects, setProjects] = useState<Project[]>([])
  const [usage, setUsage] = useState<UsageSummary | null>(null)
  const [tasks, setTasks] = useState<TaskRecord[]>([])

  const projectId = projectFilter === '' ? undefined : Number(projectFilter)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const data = await listProjects()
        if (alive) setProjects(data)
      } catch (err) {
        if (alive) push(errorMessage(err), 'error')
      }
    }
    void load()
    return () => {
      alive = false
    }
  }, [push])

  const loadUsage = useCallback(async () => {
    try {
      setUsage(await getUsage(projectId, days))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, days, push])

  const loadTasks = useCallback(async () => {
    try {
      setTasks(await listTasks(projectId, 30))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, push])

  useEffect(() => {
    void loadUsage()
    void loadTasks()
  }, [loadUsage, loadTasks])

  const handleCancel = async (taskId: number) => {
    try {
      await cancelTask(taskId)
      push('已请求取消任务', 'success')
      await loadTasks()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleExport = () => {
    if (!usage) return
    try {
      const text = exportUsageCsv(usage)
      const blob = new Blob([text], { type: 'text/csv;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = 'usage.csv'
      link.click()
      URL.revokeObjectURL(url)
      push('已导出 usage.csv', 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const maxTokens = useMemo(() => {
    if (!usage || usage.by_day.length === 0) return 1
    return Math.max(1, ...usage.by_day.map((row) => row.tokens))
  }, [usage])

  const dayCost = usage ? usage.by_day.reduce((sum, row) => sum + row.cost, 0) : 0
  const unitPrice = usage?.budget.currency_per_1k_tokens ?? 0

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">仪表盘</h1>
          <p className="page-header__desc">Token 用量、成本估算与最近任务留痕。</p>
        </div>
        <button className="btn" type="button" disabled={!usage} onClick={handleExport}>
          导出 CSV
        </button>
      </header>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">统计范围</h2>
        </div>
        <div className="panel__body stack">
          <div className="tabs">
            {DAY_OPTIONS.map((option) => (
              <button
                key={option}
                className={option === days ? 'tab is-active' : 'tab'}
                type="button"
                aria-pressed={option === days}
                onClick={() => setDays(option)}
              >
                近 {option} 天
              </button>
            ))}
          </div>

          <div className="field">
            <label className="field__label" htmlFor="dashboard-project">
              项目过滤
            </label>
            <select
              id="dashboard-project"
              className="select"
              value={projectFilter}
              onChange={(event) => setProjectFilter(event.target.value)}
            >
              <option value="">全部项目</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  #{project.id} · {project.name}
                </option>
              ))}
            </select>
            <span className="field__hint">不选则汇总全部项目的用量。</span>
          </div>
        </div>
      </section>

      {usage ? (
        <>
          <div className="card-grid">
            <div className="asset-card">
              <div className="asset-card__head">
                <span className="asset-card__name">Token 总量</span>
              </div>
              <p className="asset-card__summary mono">
                {usage.totals.tokens}（近 {usage.days} 天）
              </p>
            </div>
            <div className="asset-card">
              <div className="asset-card__head">
                <span className="asset-card__name">调用次数</span>
              </div>
              <p className="asset-card__summary mono">{usage.totals.calls}</p>
            </div>
            <div className="asset-card">
              <div className="asset-card__head">
                <span className="asset-card__name">成本估算</span>
              </div>
              <p className="asset-card__summary mono">{usage.totals.cost.toFixed(4)}</p>
            </div>
            <div className="asset-card">
              <div className="asset-card__head">
                <span className="asset-card__name">今日 Token</span>
              </div>
              <p className="asset-card__summary mono">{usage.totals.today_tokens}</p>
            </div>
          </div>

          <section className="panel">
            <div className="panel__header">
              <h2 className="panel__title">预算</h2>
              {usage.budget.alert ? <span className="tag tag--warn">已触及预算阈值</span> : null}
            </div>
            <div className="panel__body stack stack--tight">
              <p className="muted">
                每日上限 <span className="mono">{usage.budget.daily_token_limit}</span>，告警比例{' '}
                <span className="mono">{usage.budget.alert_ratio}</span>。
              </p>
              <p className="muted">
                千 Token 单价 <span className="mono">{unitPrice}</span>；按日成本合计{' '}
                <span className="mono">{dayCost.toFixed(4)}</span>。
              </p>
              {unitPrice === 0 ? <p className="muted">未配置单价，成本按 0 计。</p> : null}
            </div>
          </section>

          <section className="panel">
            <div className="panel__header">
              <h2 className="panel__title">按日 Token 用量</h2>
              <span className="muted">柱高相对于区间内峰值</span>
            </div>
            <div className="panel__body">
              {usage.by_day.length === 0 ? (
                <p className="muted">区间内暂无用量记录。</p>
              ) : (
                <div className="bar-chart">
                  {usage.by_day.map((row) => {
                    const percent = Math.round((row.tokens / maxTokens) * 100)
                    return (
                      <div
                        className="bar"
                        key={row.day}
                        style={{ height: '100%' }}
                        title={`${row.day}：${row.tokens} tokens · ${row.calls} 次`}
                      >
                        <div className="bar__fill" style={{ height: `${percent}%` }} />
                        <span className="bar__label">{row.day.slice(5)}</span>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </section>

          <section className="panel">
            <div className="panel__header">
              <h2 className="panel__title">按项目</h2>
            </div>
            <div className="panel__body panel__body--tight">
              {usage.by_project.length === 0 ? (
                <p className="muted">暂无数据。</p>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>项目</th>
                      <th>Tokens</th>
                      <th>调用</th>
                      <th>成本</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.by_project.map((row) => (
                      <tr key={row.project_id ?? 'none'}>
                        <td>{row.project_id === null ? '未归属' : `#${row.project_id}`}</td>
                        <td className="mono">{row.tokens}</td>
                        <td className="mono">{row.calls}</td>
                        <td className="mono">{row.cost.toFixed(4)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </section>

          <section className="panel">
            <div className="panel__header">
              <h2 className="panel__title">按任务类型</h2>
            </div>
            <div className="panel__body panel__body--tight">
              {usage.by_task_type.length === 0 ? (
                <p className="muted">暂无数据。</p>
              ) : (
                <table className="table">
                  <thead>
                    <tr>
                      <th>任务类型</th>
                      <th>Tokens</th>
                      <th>调用</th>
                      <th>成本</th>
                    </tr>
                  </thead>
                  <tbody>
                    {usage.by_task_type.map((row) => (
                      <tr key={row.task_type}>
                        <td>{row.task_type}</td>
                        <td className="mono">{row.tokens}</td>
                        <td className="mono">{row.calls}</td>
                        <td className="mono">{row.cost.toFixed(4)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </section>
        </>
      ) : (
        <div className="empty-state">
          <p className="empty-state__title">暂无用量数据</p>
          <p className="empty-state__desc">有调用记录后这里会展示统计与图表。</p>
        </div>
      )}

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">最近任务留痕</h2>
          <button className="btn btn--ghost btn--sm" type="button" onClick={() => void loadTasks()}>
            刷新
          </button>
        </div>
        <div className="panel__body panel__body--tight">
          {tasks.length === 0 ? (
            <p className="muted">暂无任务记录。</p>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>任务类型</th>
                  <th>引擎</th>
                  <th>模型</th>
                  <th>状态</th>
                  <th>Prompt 版本</th>
                  <th>耗时</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={task.id}>
                    <td className="mono">{task.created_at}</td>
                    <td>{task.task_type}</td>
                    <td className="mono">{task.engine || '—'}</td>
                    <td className="mono">{task.model || '—'}</td>
                    <td>
                      <span className={taskTagClass(task.status)}>{task.status}</span>
                    </td>
                    <td className="mono">
                      {task.prompt_id}
                      {task.prompt_version ? ` · ${task.prompt_version}` : ''}
                    </td>
                    <td className="mono">{formatDuration(task)}</td>
                    <td>
                      {task.status === 'running' ? (
                        <button
                          className="btn btn--danger btn--sm"
                          type="button"
                          onClick={() => void handleCancel(task.id)}
                        >
                          取消
                        </button>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <p className="page-footnote">用量数据来自本地任务留痕，CSV 可直接用于对账。</p>
    </div>
  )
}