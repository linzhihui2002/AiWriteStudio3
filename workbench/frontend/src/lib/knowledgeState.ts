import type { KnowledgeCandidate, KnowledgeCandidateEntity, KnowledgeDocument, KnowledgeEdge, KnowledgeEvidence, KnowledgeGraphData, KnowledgeLifecycle, KnowledgeNode, KnowledgeReviewItem } from '../api/knowledge'

export function knowledgeCandidateEntityOptions(candidate: KnowledgeCandidate, targets: KnowledgeCandidateEntity[] = [], target = false): KnowledgeCandidateEntity[] {
  const options = target ? candidate.target_entity_options || [] : candidate.entity_options
  const entity = target ? candidate.target_entity : candidate.entity
  // In an ambiguous candidate, its default entity is a proposed new identity,
  // not another author definition. Only explicit definition options can resolve it.
  const base = options.length ? options : entity ? [entity] : []
  return [...new Map([...base, ...(target ? targets : [])].map((value) => [value.anchor, value])).values()]
}
export function knowledgeCandidateEntityLabel(entity: KnowledgeCandidateEntity): string {
  const detail = entity.fields?.['身份'] || entity.fields?.['归属'] || entity.fields?.['名称'] || entity.description || ''
  return `${entity.label} · ${entity.document}${entity.line_start ? ` · L${entity.line_start}` : entity.definition ? '' : ' · 新建实体'}${detail ? ` · ${detail.replace(/\s+/g, ' ').slice(0, 38)}` : ''}`
}

export type KnowledgeCandidateEdit = { value: string; entity: string; target: string; lifecycle: KnowledgeLifecycle; start: string; end: string }
export function knowledgeReviewInput(candidate: KnowledgeCandidate, edit: KnowledgeCandidateEdit, decision: 'approve' | 'ignore'): KnowledgeReviewItem {
  if (decision === 'ignore') return { id: candidate.id, version: candidate.version, decision }
  if (!edit.entity || !edit.value.trim() || candidate.kind === 'relation' && !edit.target) {
    throw new Error('请填写候选内容，并确认关联实体；关系候选还需要选择关系对象。')
  }
  const start = edit.start ? Number(edit.start) : undefined
  const end = edit.end ? Number(edit.end) : undefined
  if ([start, end].some((value) => value !== undefined && (!Number.isInteger(value) || value < 1))) throw new Error('章节范围必须填写正整数。')
  if (start && end && end < start) throw new Error('结束章节不能早于起始章节。')
  return { id: candidate.id, version: candidate.version, decision, value: edit.value, entity_anchor: edit.entity,
    target_entity_anchor: edit.target || undefined, lifecycle: edit.lifecycle, chapter_start: start, chapter_end: end }
}

export function knowledgeSelectionEvidenceIds(item: KnowledgeNode | KnowledgeEdge): string[] {
  const sourceId = 'source_location' in item ? item.source_location?.evidence_id : undefined
  return [...new Set([...(sourceId ? [sourceId] : []), ...item.evidence_ids])]
}

/** The first proof for an entity opens its definition, rather than the whole retrieval chunk. */
export function knowledgeDefinitionEvidence(hit: KnowledgeEvidence, node: KnowledgeNode): KnowledgeEvidence {
  const location = node.source_location
  if (!location || location.evidence_id !== hit.evidence_id || location.rel_path !== hit.rel_path
    || !location.document_hash || location.document_hash !== hit.document_hash
    || location.line_start < hit.line_start || location.line_end > hit.line_end) return hit
  const text = hit.text.split('\n').slice(location.line_start - hit.line_start, location.line_end - hit.line_start + 1).join('\n')
  if (!text.trim() || node.description && text.trim() !== node.description.trim()) return hit
  return { ...hit, text, line_start: location.line_start, line_end: location.line_end }
}

export function knowledgeDocumentGroup(document: KnowledgeDocument): string {
  const root = document.rel_path.replace(/\\/g, '/').split('/')[0]
  if (document.kind === 'chapter' || root === '章节') return '章节正文'
  if (root === '设定') return '设定资料'
  if (root === '状态') return '状态与追踪'
  if (root === '大纲') return '大纲与计划'
  if (['teardown', 'reference', 'fact_card'].includes(document.kind) || /拆书|事实卡/.test(root)) return '拆书事实卡'
  return root || '其他资料'
}

export function knowledgeDocumentStatus(document: KnowledgeDocument): string {
  const index = document.index_label || ({ indexed: '已索引', pending: '等待索引', stale: '索引待更新',
    excluded: '未纳入历史索引' } as Record<string, string>)[document.index_status || 'pending'] || '等待索引'
  const status = ({ 草稿: '草稿', 完成: '已完成', 发表: '已发表',
    draft: '草稿', completed: '已完成', complete: '已完成', published: '已发表',
    author: '作者材料', model: '模型材料 · 待核实', unknown: '来源待核实', planned: '计划',
    planning: '计划', reference: '参考材料' } as Record<string, string>)[document.status || '']
  return [index, document.index_status === 'indexed' ? `${document.chunk_count} 片段` : '', status].filter(Boolean).join(' · ')
}

/** The document browser chooses local scope; the explicit panorama retains book context. */
export function knowledgeGraphScope(key: string, selectedDocument: string, onlyDocument: boolean,
  kinds: string[], chapterBefore: string, center: string, showContent = false): { key: string; document?: string } {
  const document = onlyDocument && selectedDocument ? selectedDocument : undefined
  return { key: JSON.stringify([key, document || '', [...kinds].sort(), Number(chapterBefore) || 0, center, showContent]), document }
}

export function knowledgeNodeDocuments(node: KnowledgeNode): string[] {
  return [...new Set([node.document, ...(node.documents || []), ...(node.rel_paths || [])].filter(Boolean))]
}

/** Preserve this book's canvas while a new scope loads; never label that old graph as the new view. */
export function knowledgeGraphPresentation(graph: KnowledgeGraphData | null, projectId: number,
  knowledgeKey: string | undefined, committedScope: string, requestedScope: string): {
    rendered: KnowledgeGraphData | null; current: KnowledgeGraphData | null; changingScope: boolean
  } {
  const rendered = graph && knowledgeKey && matchesKnowledgeBook(graph, projectId, knowledgeKey) ? graph : null
  const current = rendered && committedScope === requestedScope ? rendered : null
  return { rendered, current, changingScope: Boolean(rendered && !current) }
}

export function knowledgeNodeStatus(node: KnowledgeNode): string {
  if (node.lifecycle === 'draft') return '草稿 · 待定'
  if (!node.planned) return ''
  return node.kind === 'foreshadow' ? '计划 · 未埋设' : '计划 · 尚未发生'
}

/** Browsing a material highlights its entities without hiding the surrounding book graph. */
export function relatedKnowledgeNodes(graph: KnowledgeGraphData | null, document: string, hitIds?: string[]): KnowledgeNode[] {
  if (!graph) return []
  const documentIds = new Set(graph.document_nodes?.[document] || [])
  const hits = hitIds === undefined ? null : new Set(hitIds)
  const candidates = graph.nodes.filter((node) => (!document || documentIds.has(node.id)
    || knowledgeNodeDocuments(node).includes(document)) && (!hits || hits.has(node.id)))
  const entityKinds = new Set(['document', 'chapter', 'content'])
  return candidates.sort((a, b) => Number(entityKinds.has(a.kind)) - Number(entityKinds.has(b.kind))
    || (document ? Number(b.document === document) - Number(a.document === document) : 0)
    || a.label.localeCompare(b.label, 'zh-CN'))
}

/** Compact card preview; full evidence below remains the exact source text. */
export function knowledgeNodePreview(node: KnowledgeNode): string {
  return (node.description || node.preview || '').replace(/^\s*#{1,6}\s+[^\n]*(?:\n|$)/, '')
    .replace(/\*\*([^*]+)\*\*/g, '$1').replace(/^[ \t]*[-*+]\s+/gm, '').replace(/\n{2,}/g, '\n').trim()
}

/** Both identities must match; route IDs alone do not identify a restored database. */
export function matchesKnowledgeBook(value: { project_id: number; knowledge_key: string },
  projectId: number, knowledgeKey?: string): boolean {
  return value.project_id === projectId && Boolean(value.knowledge_key)
    && (!knowledgeKey || value.knowledge_key === knowledgeKey)
}

export function evidenceEditorUrl(projectId: number, evidence: Pick<KnowledgeEvidence, 'rel_path'> &
  Partial<Pick<KnowledgeEvidence, 'line_start' | 'line_end' | 'evidence_id' | 'document_hash'>>): string {
  const query = new URLSearchParams({ path: evidence.rel_path, line: String(evidence.line_start || 1),
    end_line: String(evidence.line_end || evidence.line_start || 1), evidence: evidence.evidence_id || '' })
  if (evidence.document_hash) query.set('hash', evidence.document_hash)
  return `/project/${projectId}/editor?${query}`
}

export function sourceLabel(source: string, reviewStatus?: string): string {
  if (source === 'model' && reviewStatus === 'approved') return '模型提取 · 已审核'
  return ({ author: '作者设定', model: '模型提取 · 待核实', unknown: '来源未标注 · 待核实',
    document: '原文', explicit: '原文明确关系', mention: '文档提及', cooccurrence: '同章共现',
    co_occurrence: '同章共现', keyword: '关键词', vector: '语义召回', hybrid: '混合检索',
    entity: '精确实体', graph: '图谱关联', reference: '拆书参考', planned: '计划关系',
    planning: '大纲计划' } as Record<string, string>)[source] || source
}

export function evidenceSelection(body: string, content: string, startLine: number, endLine: number): [number, number] {
  const lines = content.split('\n')
  const first = Math.max(1, Math.min(lines.length, startLine || 1))
  const last = Math.max(first, Math.min(lines.length, endLine || first))
  const prefix = content.length - body.length
  const start = lines.slice(0, first - 1).join('\n').length + (first > 1 ? 1 : 0) - prefix
  const end = lines.slice(0, last).join('\n').length - prefix
  return [Math.max(0, Math.min(body.length, start)), Math.max(0, Math.min(body.length, end))]
}

export function splitKnowledgeCitations(text: string): Array<{ text: string; citation?: number }> {
  return text.split(/(\[\d+\])/g).filter(Boolean).map((part) => {
    const match = /^\[(\d+)\]$/.exec(part)
    return match ? { text: part, citation: Number(match[1]) } : { text: part }
  })
}
