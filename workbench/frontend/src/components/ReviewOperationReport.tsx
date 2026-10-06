import type { IngestionResult, SoftDeslopResult } from '../api/types'
import { rejectedSuggestionCount, softReviewMessage } from '../lib/reviewWorkspace'

export function SoftDeslopReport({ result, stale, openFile, openInbox }: {
  result: SoftDeslopResult; stale: boolean; openFile: (path?: string, line?: number) => void; openInbox: () => void
}) {
  const notes = Array.isArray(result.style_comparison?.notes) ? result.style_comparison.notes : []
  return <section className="panel" aria-label="去AI味软审结果">
    <div className="panel__header"><h2 className="panel__title">去AI味软审结果</h2><span className={result.ai_used ? 'tag tag--ok' : 'tag tag--warn'}>{result.ai_used ? '已完成软审' : '软审未完成'}</span></div>
    <div className="panel__body stack">
      <p role="status" className={!result.ai_used || result.source_changed ? 'notice notice--warn' : 'muted'}>{softReviewMessage(result)}</p>
      {stale && <p className="notice notice--warn">正文已变化，以下原文位置对应旧版本。请重新软审当前正文。</p>}
      {result.proposal_ids.length > 0 && <button className="btn btn--primary" onClick={openInbox}>去收件箱核对 {result.proposal_ids.length} 条提案</button>}
      {result.suggestions.length > 0 && <ol className="review-suggestions">{result.suggestions.map((item, index) => <li key={`${item.line}:${index}`}>
        <div className="row row--between"><strong>{item.issue || '表达建议'}</strong><span className="muted">第 {item.line} 行</span></div>
        <div className="review-suggestion-text"><p><strong>原文</strong>{item.original}</p><p><strong>建议写法</strong>{item.replacement}</p></div>
        <button className="btn btn--sm" disabled={stale} onClick={() => openFile(undefined, item.line)}>定位章节原文</button>
      </li>)}</ol>}
      {rejectedSuggestionCount(result) > 0 && <details><summary>已过滤 {rejectedSuggestionCount(result)} 条未通过原文核验的建议</summary><ul>{result.rejected_suggestions.map((item, index) => <li key={index}>{item.reason}{item.count ? `（${item.count} 条）` : ''}</li>)}</ul></details>}
      {notes.length > 0 && <div><h3>文风参照观察</h3><ul>{notes.map((note, index) => <li key={index}>{String(note)}</li>)}</ul><p className="field__hint">参照统计用于观察表达，不作为通过标准。</p></div>}
      {(result.ai_error_code || result.task_id) && <details><summary>查看诊断信息</summary><p>任务：{result.task_id || '未创建'} · 原因：{result.ai_error_code || '未分类'}</p></details>}
    </div>
  </section>
}

export function IngestionReport({ result, stale, openFile, openInbox }: {
  result: IngestionResult; stale: boolean; openFile: (path?: string) => void; openInbox: () => void
}) {
  const artifacts = [
    { key: 'state' as const, label: '角色状态', count: result.state_changes_detected, path: '状态/角色状态.md', hint: '位置、伤势、关系和已知信息；变化通过提案核对。' },
    { key: 'timeline' as const, label: '时间线', count: result.timeline_added, path: '状态/时间线.md', hint: '记录事件先后与时间线索，避免下一章的时间冲突。' },
    { key: 'ledger' as const, label: '资源账本', count: result.ledger_added, path: '状态/资源账本.md', hint: '追踪物品、货币和资源增减，检查余额能否对上。' },
    { key: 'foreshadow' as const, label: '伏笔台账', count: result.foreshadow_added, path: '设定/伏笔管理.md', hint: '记录埋设与回收依据，提醒尚未填完的线索。' },
  ]
  return <section className="panel" aria-label="四件套提取结果">
    <div className="panel__header"><h2 className="panel__title">四件套提取结果</h2><span className="tag">{result.source_changed ? '正文已变化' : result.replayed || result.reused || result.skipped ? '沿用已有记录' : '已提取'}</span></div>
    <div className="panel__body stack">
      {stale && <p className="notice notice--warn">正文版本已变化，此处展示上次提取结果。请重新提取当前正文。</p>}
      {(result.replayed || result.reused) && <p className="muted">当前版本已经提取，本次返回已有结果，没有重复追加记录。</p>}
      {!result.ai_used && <p className="notice notice--warn">本次使用正文中的明确线索；AI 结构化提取未完成，角色状态等复杂变化仍需核对。</p>}
      {result.summary && <div><h3>本章摘要</h3><p>{result.summary}</p></div>}
      <div className="review-artifacts">{artifacts.map(item => <article key={item.label}><div className="row row--between"><strong>{item.label}</strong><span>{item.count ?? 0} 项</span></div><p className="muted">{item.hint}</p>{result.counts?.[item.key] && <p className="field__hint">新增 {result.counts[item.key].added} · 更新 {result.counts[item.key].updated} · 沿用 {result.counts[item.key].skipped} · 拒收 {result.counts[item.key].rejected}</p>}<button className="btn btn--sm" onClick={() => openFile(item.path)}>查看{item.label}</button></article>)}</div>
      {(result.proposal_ids?.length || result.proposals_created?.length || 0) > 0 && <button className="btn btn--primary" onClick={openInbox}>去收件箱核对角色状态提案</button>}
      {(result.memory_added || 0) > 0 && <p className="muted">已整理 {result.memory_added} 条角色记忆，用于检查“谁知道什么”。</p>}
      {result.warnings?.map((warning, index) => <p className="notice notice--warn" key={index}>{warning}</p>)}
      {(result.rejected?.length || 0) > 0 && <details><summary>查看未采纳条目</summary><ul>{result.rejected?.map((item, index) => <li key={index}>{item.reason}</li>)}</ul></details>}
      <p className="field__hint">四件套帮助写作助手承接剧情。提取不代表正文通过审稿，伏笔回收仍需作者确认。</p>
    </div>
  </section>
}
