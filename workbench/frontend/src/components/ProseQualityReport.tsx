import type { ProseHistoryQuality, ProseQuality, ProseReviewSuggestion } from '../api/types'
import { proseAdviceReport, proseHistoryStatusLabel, proseKindLabel, proseLocationLabel } from '../lib/proseQuality'

export default function ProseQualityReport({ quality, modelSuggestions, historyQuality, stale = false }: {
  quality?: ProseQuality
  modelSuggestions?: ProseReviewSuggestion[]
  historyQuality?: ProseHistoryQuality
  stale?: boolean
}) {
  const report = proseAdviceReport(quality, modelSuggestions, historyQuality)
  if (!report.available) return null
  return (
    <section className="panel" aria-label="表达建议（供作者判断）">
      <div className="panel__header"><h3 className="panel__title">表达建议（供作者判断）</h3></div>
      <div className="panel__body stack">
        <p className="muted">这些观察用于检查重复、解释与人物表达，不影响合同与硬门禁判定。请结合语境判断是否保留。</p>
        {stale && <p className="notice notice--warn">正文已修改，以下建议对应修改前的候选。重新审查后才能核对当前正文的位置。</p>}
        {report.truncated && <p className="notice notice--warn">报告已截断，只展示部分发现。未展示的位置仍需作者检查。</p>}
        <p className={report.history.status === 'degraded' || report.history.status === 'not_checked' ? 'notice notice--warn' : 'muted'}>
          {proseHistoryStatusLabel(report.history)}
        </p>
        {report.history.degradation.length > 0 && <details>
          <summary>跨章检查限制</summary>
          <ul>{report.history.degradation.map((reason, index) => <li key={index}>{reason}</li>)}</ul>
        </details>}
        {report.history.excluded.length > 0 && <details>
          <summary>未纳入的历史来源（{report.history.excluded.length}）</summary>
          <ul>{report.history.excluded.map((source, index) => <li key={`${source.rel_path}:${index}`}>
            {source.rel_path}：{source.reason}
          </li>)}</ul>
        </details>}
        {report.findings.length === 0
          ? <p className="muted">本次未列出表达建议；仍可按你的文风偏好检查正文。</p>
          : <ol className="notice__list">{report.findings.map((finding, index) => (
            <li key={`${finding.source}:${finding.kind}:${finding.location?.line}:${index}`}>
              <div className="stack stack--tight">
                <div className="row">
                  <span className="tag tag--warn">{proseKindLabel(finding.kind)}</span>
                  <span className="muted">{finding.source === 'model' ? '审稿表达建议' : finding.source === 'history' ? '跨章文本诊断 · 本候选' : '文本诊断'} · {proseLocationLabel(finding.location)}</span>
                </div>
                <p><strong>原文：</strong>{finding.evidence}{finding.textTruncated ? '（原文片段已截短）' : ''}</p>
                {finding.related.length > 0 && <p className="muted">重复出现位置：{finding.related.map(proseLocationLabel).join('；')}</p>}
                {finding.relatedSources.length > 0 && <div className="stack stack--tight" aria-label="此前来源（仅供核对）">
                  <p className="muted">此前来源（仅供核对）：</p>
                  {finding.relatedSources.map((source, sourceIndex) => <div key={`${source.rel_path}:${source.line}:${sourceIndex}`} className="notice">
                    <p><strong>{source.rel_path}</strong> · {proseLocationLabel(source)}</p>
                    {source.text ? <p><strong>来源原文：</strong>{source.text}{source.text_truncated ? '（来源片段已截短）' : ''}</p>
                      : <p className="muted">来源原文未提供，请按正文位置核对。</p>}
                    <details><summary>来源文本摘要（SHA-256）</summary><p>{source.content_hash}</p></details>
                  </div>)}
                </div>}
                <p><strong>观察：</strong>{finding.reason}</p>
                {finding.suggestion && <p><strong>建议：</strong>{finding.suggestion}</p>}
              </div>
            </li>
          ))}</ol>}
      </div>
    </section>
  )
}
