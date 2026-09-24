/**
 * 拆书资产库（M4 / Task 44）—— 导入拆解目标 → 六维拆解 + 双时间线 + 事实卡 → 按题材召回。
 *
 * 纪律：事实卡条目必须附章节依据；无法取证的条目显式标注「依据缺失」，不补写、不编造；
 * 依据缺失的条目只供人工查看，不会进入写作/审稿上下文。
 * 事实源是 Markdown（拆书/{目标}/原文.md 等），本页只做视图与触发，不直接改文件。
 */

import { useCallback, useEffect, useState, type ChangeEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  analyzeTeardown,
  deleteTeardownTarget,
  importTeardownTarget,
  listTeardownFacts,
  listTeardownTargets,
  readTeardownArtifact,
  recallTeardown,
} from '../api/client'
import type { TeardownFact, TeardownTarget } from '../api/client'
import MarkdownView from '../components/MarkdownView'
import Modal from '../components/Modal'
import { useConfirm } from '../components/ConfirmDialog'
import { errorMessage, useToast } from '../state/useToast'
import { NO_AUTOFILL } from '../lib/autofill'

type ArtifactKind = 'source' | 'analysis' | 'timeline' | 'facts'

interface ArtifactTab {
  key: ArtifactKind
  label: string
}

interface ArtifactState {
  target: string
  kind: ArtifactKind
  content: string
  meta: Record<string, unknown>
}

interface AnalyzeInfo {
  target: string
  factCount: number
  missingCount: number
  files: string[]
}

interface RecallState {
  genre: string
  matched: number
  facts: TeardownFact[]
}

const ARTIFACT_TABS: readonly ArtifactTab[] = [
  { key: 'source', label: '原文' },
  { key: 'analysis', label: '六维拆解' },
  { key: 'timeline', label: '双时间线' },
  { key: 'facts', label: '事实卡' },
]

/** 自定六维：工作台原创表述，未复制任何外部项目文本。 */
const SELF_DIMENSIONS: readonly string[] = [
  '开篇钩子',
  '情绪节拍',
  '主角行动线',
  '信息节奏',
  '语言特征',
  '可迁移手法',
]

/** 依据缺失判定：与后端纪律一致（空串或显式「依据缺失」）。 */
function isMissingEvidence(evidence: string): boolean {
  const value = (evidence ?? '').trim()
  return value === '' || value === '依据缺失'
}

function formatTime(value: string): string {
  return value ? value.replace('T', ' ') : '—'
}

function fileNameOf(name: string): string {
  const stem = name.replace(/\.[^.]+$/, '')
  return stem || name
}

export default function Teardown() {
  const { id } = useParams<{ id: string }>()
  const projectId = Number(id)
  const { push } = useToast()

  const [targets, setTargets] = useState<TeardownTarget[]>([])
  const [dimensions, setDimensions] = useState<string[]>([])
  const [facts, setFacts] = useState<TeardownFact[]>([])
  const [factCount, setFactCount] = useState(0)
  const [missingEvidence, setMissingEvidence] = useState(0)

  const [selectedTarget, setSelectedTarget] = useState('')
  const [dimensionFilter, setDimensionFilter] = useState('')
  const [busy, setBusy] = useState('')
  const [analyzeInfo, setAnalyzeInfo] = useState<AnalyzeInfo | null>(null)
  const [confirm, confirmNode] = useConfirm()

  const [importOpen, setImportOpen] = useState(false)
  const [importName, setImportName] = useState('')
  const [importGenre, setImportGenre] = useState('')
  const [importNote, setImportNote] = useState('')
  const [importContent, setImportContent] = useState('')

  const [artifact, setArtifact] = useState<ArtifactState | null>(null)
  const [artifactKind, setArtifactKind] = useState<ArtifactKind>('source')
  const [artifactBusy, setArtifactBusy] = useState(false)

  const [recallGenre, setRecallGenre] = useState('')
  const [recallLimit, setRecallLimit] = useState(6)
  const [recall, setRecall] = useState<RecallState | null>(null)

  const loadTargets = useCallback(async () => {
    try {
      const data = await listTeardownTargets(projectId)
      setTargets(data.targets)
      setDimensions(data.dimensions)
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, push])

  const loadFacts = useCallback(async () => {
    try {
      const data = await listTeardownFacts(projectId, selectedTarget || undefined)
      setFacts(data.facts)
      setFactCount(data.count)
      setMissingEvidence(data.missing_evidence)
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [projectId, selectedTarget, push])

  useEffect(() => {
    void loadTargets()
  }, [loadTargets])

  useEffect(() => {
    void loadFacts()
  }, [loadFacts])

  const resetImportForm = () => {
    setImportName('')
    setImportGenre('')
    setImportNote('')
    setImportContent('')
  }

  const handleImportFile = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    try {
      const text = await file.text()
      setImportContent(text)
      setImportName(fileNameOf(file.name))
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const submitImport = async () => {
    if (!importName.trim()) {
      push('请填写目标书名', 'error')
      return
    }
    if (!importContent.trim()) {
      push('请粘贴文本或上传 .md/.txt 样章', 'error')
      return
    }
    setBusy('import')
    try {
      const result = await importTeardownTarget(projectId, {
        target_name: importName.trim(),
        content: importContent,
        genre: importGenre.trim(),
        source_note: importNote.trim(),
      })
      push(`已导入「${result.target}」（${result.chars} 字），落 ${result.path}`, 'success')
      setImportOpen(false)
      resetImportForm()
      await loadTargets()
      await loadFacts()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const runAnalyze = async (target: string) => {
    setBusy(`analyze:${target}`)
    setAnalyzeInfo(null)
    try {
      const result = await analyzeTeardown(projectId, target, true)
      const sourceLabel = result.source === 'model' ? '模型 + 确定性统计' : '确定性统计（AI 不可用，已降级）'
      push(
        `「${result.target}」拆解完成：事实卡 ${result.fact_count} 条，依据缺失 ${result.missing_evidence} 条（${sourceLabel}）`,
        result.missing_evidence > 0 ? 'info' : 'success',
      )
      setAnalyzeInfo({
        target: result.target,
        factCount: result.fact_count,
        missingCount: result.missing_evidence,
        files: result.files,
      })
      await loadTargets()
      await loadFacts()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const loadArtifact = async (target: string, kind: ArtifactKind) => {
    setArtifactBusy(true)
    try {
      const result = await readTeardownArtifact(projectId, target, kind)
      setArtifact({ target: result.target, kind, content: result.content, meta: result.meta })
    } catch (err) {
      push(errorMessage(err), 'error')
      setArtifact(null)
    } finally {
      setArtifactBusy(false)
    }
  }

  const openArtifact = (target: string) => {
    setArtifactKind('source')
    setArtifact(null)
    void loadArtifact(target, 'source')
  }

  const switchArtifact = (kind: ArtifactKind) => {
    setArtifactKind(kind)
    if (!artifact) return
    setArtifact(null)
    void loadArtifact(artifact.target, kind)
  }

  const handleDelete = async (target: TeardownTarget) => {
    const confirmed = await confirm({
      title: '删除拆解目标',
      message: `确认删除拆解目标「${target.target}」？将同时删除 拆书/${target.target}/ 下的原文与全部拆解产物，且不可撤销。`,
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    setBusy(`delete:${target.target}`)
    try {
      await deleteTeardownTarget(projectId, target.target)
      push(`已删除「${target.target}」`, 'success')
      if (analyzeInfo?.target === target.target) setAnalyzeInfo(null)
      if (artifact?.target === target.target) setArtifact(null)
      if (selectedTarget === target.target) setSelectedTarget('')
      else await loadFacts()
      await loadTargets()
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const runRecall = async () => {
    setBusy('recall')
    try {
      const result = await recallTeardown(projectId, recallGenre.trim(), recallLimit)
      setRecall({ genre: result.genre, matched: result.matched, facts: result.facts })
      push(
        result.matched > 0 ? `已召回 ${result.matched} 条同题材事实卡` : '没有可召回条目（依据缺失的条目不进上下文）',
        result.matched > 0 ? 'success' : 'info',
      )
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const isBusy = busy !== ''
  const dimensionOptions = dimensions.length > 0 ? dimensions : [...SELF_DIMENSIONS]
  const visibleFacts = dimensionFilter
    ? facts.filter((fact) => fact.dimension === dimensionFilter)
    : facts

  return (
    <div className="page page--wide stack">
      <header className="page-header">
        <div>
          <h1 className="page-header__title">拆书资产库</h1>
          <p className="page-header__desc">拆书拆解与事实卡：按题材召回参考。</p>
        </div>
        <div className="btn-row">
          <button
            className="btn btn--sm"
            type="button"
            disabled={isBusy}
            onClick={() => {
              void loadTargets()
              void loadFacts()
            }}
          >
            刷新
          </button>
          <Link className="btn btn--sm" to={`/project/${projectId}/editor`}>
            编辑器
          </Link>
          <Link className="btn btn--sm" to={`/project/${projectId}/outline`}>
            大纲
          </Link>
          <Link className="btn btn--sm" to={`/project/${projectId}/bible`}>
            Story Bible
          </Link>
          <Link className="btn btn--sm" to={`/project/${projectId}/cards`}>
            设定卡片
          </Link>
          <Link className="btn btn--sm" to={`/project/${projectId}/review`}>
            审稿中心
          </Link>
        </div>
      </header>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">导入拆解目标</h2>
          <button
            className="btn btn--primary btn--sm"
            type="button"
            onClick={() => setImportOpen(true)}
          >
            导入目标
          </button>
        </div>
        <div className="panel__body">
          <p className="muted">导入对标样章文本后点「拆解」。</p>
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">已导入目标</h2>
          <span className="muted">共 {targets.length} 个</span>
        </div>
        <div className="panel__body stack">
          {targets.length === 0 ? (
            <div className="empty-state">
              <p className="empty-state__title">尚未导入拆解目标</p>
              <p className="empty-state__desc">
                点击「导入目标」粘贴或上传对标样章文本，再执行拆解，即可得到六维结论与事实卡。
              </p>
            </div>
          ) : (
            <div className="stack">
              {targets.map((target) => (
                <article className="asset-card" key={target.target}>
                  <div className="asset-card__head">
                    <span className="asset-card__name">{target.target}</span>
                    <span className={target.has_facts ? 'tag tag--ok' : 'tag tag--warn'}>
                      {target.has_facts ? '已拆解' : target.has_analysis ? '有拆解无事实卡' : '未拆解'}
                    </span>
                  </div>
                  <div className="chips">
                    <span className="chip">题材：{target.genre || '未标注'}</span>
                    <span className="chip">{target.chars} 字</span>
                    <span className="chip">事实卡 {target.fact_count} 条</span>
                    <span className="chip">导入时间 {formatTime(target.imported_at)}</span>
                  </div>
                  <div className="row">
                    <button
                      className="btn btn--primary btn--sm"
                      type="button"
                      disabled={isBusy}
                      onClick={() => void runAnalyze(target.target)}
                    >
                      {busy === `analyze:${target.target}` ? '拆解中…' : '拆解'}
                    </button>
                    <button
                      className="btn btn--sm"
                      type="button"
                      disabled={isBusy}
                      onClick={() => void runAnalyze(target.target)}
                    >
                      {busy === `analyze:${target.target}` ? '拆解中…' : '重新拆解'}
                    </button>
                    <button
                      className="btn btn--sm"
                      type="button"
                      onClick={() => openArtifact(target.target)}
                    >
                      查看产物
                    </button>
                    <button
                      className="btn btn--danger btn--sm"
                      type="button"
                      disabled={isBusy}
                      onClick={() => void handleDelete(target)}
                    >
                      {busy === `delete:${target.target}` ? '删除中…' : '删除'}
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}

          {analyzeInfo ? (
            <div className="run-log">
              <div className="row row--between">
                <span className="muted">
                  最近一次拆解：<span className="mono">{analyzeInfo.target}</span> · 事实卡{' '}
                  <span className="mono">{analyzeInfo.factCount}</span> 条 · 依据缺失{' '}
                  <span className="mono">{analyzeInfo.missingCount}</span> 条
                </span>
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  onClick={() => setAnalyzeInfo(null)}
                >
                  收起
                </button>
              </div>
              <ul className="notice__list">
                {analyzeInfo.files.map((file) => (
                  <li key={file}>
                    <span className="mono">{file}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">事实卡</h2>
          <span className="muted">
            共 {factCount} 条 · 依据缺失 {missingEvidence} 条
          </span>
        </div>
        <div className="panel__body stack">
          <div className="row">
            <label className="field">
              <span className="field__label">目标</span>
              <select
                className="select"
                value={selectedTarget}
                onChange={(event) => setSelectedTarget(event.target.value)}
              >
                <option value="">全部</option>
                {targets.map((target) => (
                  <option key={target.target} value={target.target}>
                    {target.target}
                  </option>
                ))}
              </select>
            </label>
            <label className="field">
              <span className="field__label">维度</span>
              <select
                className="select"
                value={dimensionFilter}
                onChange={(event) => setDimensionFilter(event.target.value)}
              >
                <option value="">全部</option>
                {dimensionOptions.map((dimension) => (
                  <option key={dimension} value={dimension}>
                    {dimension}
                  </option>
                ))}
              </select>
            </label>
          </div>

          {visibleFacts.length === 0 ? (
            <div className="empty-state">
              <p className="empty-state__title">暂无事实卡</p>
              <p className="empty-state__desc">
                先导入拆解目标并执行「拆解」，事实卡会在此按维度列出；依据缺失的条目会显式标注。
              </p>
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>维度</th>
                  <th>结论</th>
                  <th>章节依据</th>
                  <th>原文摘录</th>
                  <th>所属目标</th>
                </tr>
              </thead>
              <tbody>
                {visibleFacts.map((fact, index) => (
                  <tr key={`${fact.target}-${fact.dimension}-${index}`}>
                    <td>
                      <span className="tag tag--primary">{fact.dimension || '未标注'}</span>
                    </td>
                    <td>{fact.conclusion || '—'}</td>
                    <td>
                      {isMissingEvidence(fact.evidence) ? (
                        <span className="tag tag--warn" title="无法取证，按纪律不补写">
                          依据缺失
                        </span>
                      ) : (
                        fact.evidence
                      )}
                    </td>
                    <td>{fact.quote || '—'}</td>
                    <td className="mono">{fact.target}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">按题材召回</h2>
        </div>
        <div className="panel__body stack">
          <div className="row">
            <label className="field">
              <span className="field__label">题材</span>
              <input
                autoComplete={NO_AUTOFILL}
                className="input"
                value={recallGenre}
                placeholder="留空 = 全部题材"
                onChange={(event) => setRecallGenre(event.target.value)}
              />
            </label>
            <label className="field">
              <span className="field__label">条数</span>
              <input
                className="input"
                type="number"
                min={1}
                max={50}
                value={recallLimit}
                onChange={(event) => setRecallLimit(Math.max(1, Number(event.target.value) || 1))}
              />
            </label>
            <button
              className="btn btn--primary btn--sm"
              type="button"
              disabled={isBusy}
              onClick={() => void runRecall()}
            >
              {busy === 'recall' ? '召回中…' : '召回'}
            </button>
          </div>

          <p className="muted">只召回带章节依据的条目。</p>

          {recall === null ? (
            <p className="muted">尚未召回：填写题材（可留空）与条数后点击「召回」。</p>
          ) : recall.matched === 0 ? (
            <div className="empty-state">
              <p className="empty-state__title">无可召回条目</p>
              <p className="empty-state__desc">
                先导入目标并完成拆解；若已拆解仍为空，说明该题材下暂无带章节依据的事实卡。
              </p>
            </div>
          ) : (
            <div className="stack stack--tight">
              <span className="muted">
                题材 <span className="mono">{recall.genre || '全部'}</span> · 命中{' '}
                <span className="mono">{recall.matched}</span> 条
              </span>
              <ul className="notice__list">
                {recall.facts.map((fact, index) => (
                  <li key={`${fact.target}-${fact.dimension}-${index}`}>
                    <span className="tag tag--primary">
                      {fact.target}·{fact.dimension}
                    </span>{' '}
                    {fact.conclusion}
                    <span className="muted">（依据：{fact.evidence}）</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </section>

      <section className="panel">
        <div className="panel__header">
          <h2 className="panel__title">六维说明</h2>
        </div>
        <div className="panel__body panel__body--tight stack stack--tight">
          <div className="chips">
            {SELF_DIMENSIONS.map((dimension) => (
              <span className="chip" key={dimension}>
                {dimension}
              </span>
            ))}
          </div>
          <ul className="notice__list">
            <li>开篇钩子：前 300 字做了什么、第几段给出冲突、用什么抓人。</li>
            <li>情绪节拍：每约 500 字一个可命名的节拍，节拍如何交替。</li>
            <li>主角行动线：主角做了哪些决定、付出什么代价、目标如何升级。</li>
            <li>信息节奏：伏笔埋在哪、何时揭示、读者何时比角色先知情。</li>
            <li>语言特征：句长分布、对话占比、口语化程度、标志性用词。</li>
            <li>可迁移手法：能直接用于本书的写法，附可复制的结构而非句子。</li>
          </ul>
        </div>
      </section>

      <p className="page-footnote">
        事实卡必须附章节依据；无法取证的一律标「依据缺失」。
      </p>

      <Modal
        title="导入拆解目标"
        open={importOpen}
        onClose={() => setImportOpen(false)}
        footer={
          <>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setImportOpen(false)}>
              取消
            </button>
            <button
              className="btn btn--primary btn--sm"
              type="button"
              disabled={busy === 'import'}
              onClick={() => void submitImport()}
            >
              {busy === 'import' ? '导入中…' : '导入'}
            </button>
          </>
        }
      >
        <div className="stack">
          <label className="field">
            <span className="field__label">目标书名</span>
            <input
              autoComplete={NO_AUTOFILL}
              className="input"
              value={importName}
              placeholder="例如：某对标作品"
              onChange={(event) => setImportName(event.target.value)}
            />
            <span className="field__hint">会作为目录名：拆书/{'{'}目标书名{'}'}/</span>
          </label>
          <label className="field">
            <span className="field__label">题材</span>
            <input
              autoComplete={NO_AUTOFILL}
              className="input"
              value={importGenre}
              placeholder="例如：都市/悬疑（用于按题材召回）"
              onChange={(event) => setImportGenre(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="field__label">来源说明</span>
            <input
              autoComplete={NO_AUTOFILL}
              className="input"
              value={importNote}
              placeholder="可选：来源、授权与用途备注"
              onChange={(event) => setImportNote(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="field__label">文本内容</span>
            <textarea
              autoComplete={NO_AUTOFILL}
              className="textarea"
              style={{ minHeight: 240 }}
              value={importContent}
              placeholder="粘贴样章文本（支持「第N章」分章标记）"
              onChange={(event) => setImportContent(event.target.value)}
            />
          </label>
          <label className="field">
            <span className="field__label">上传文件</span>
            <input
              className="input"
              type="file"
              accept=".md,.txt"
              onChange={(event) => void handleImportFile(event)}
            />
            <span className="field__hint">导入前请自行确认文本来源合规。</span>
          </label>
        </div>
      </Modal>

      <Modal
        title={artifact ? `拆解产物：${artifact.target}` : '拆解产物'}
        open={artifact !== null}
        onClose={() => setArtifact(null)}
        footer={
          <button className="btn btn--ghost btn--sm" type="button" onClick={() => setArtifact(null)}>
            关闭
          </button>
        }
      >
        <div className="stack">
          <nav className="tabs" aria-label="拆解产物">
            {ARTIFACT_TABS.map((tab) => (
              <button
                key={tab.key}
                className={artifactKind === tab.key ? 'tab is-active' : 'tab'}
                type="button"
                onClick={() => switchArtifact(tab.key)}
              >
                {tab.label}
              </button>
            ))}
          </nav>

          {artifactBusy ? <p className="muted">读取中…</p> : null}

          {artifact ? (
            <div className="stack stack--tight">
              <span className="muted mono">
                {artifact.target} · {ARTIFACT_TABS.find((tab) => tab.key === artifact.kind)?.label}
              </span>
              {Object.entries(artifact.meta).length > 0 ? (
                <div className="chips">
                  {Object.entries(artifact.meta)
                    .filter(([, value]) => typeof value === 'string' || typeof value === 'number')
                    .map(([key, value]) => (
                      <span className="chip" key={key}>
                        {key}：{String(value)}
                      </span>
                    ))}
                </div>
              ) : null}
              <MarkdownView text={artifact.content} emptyHint="（产物为空）" />
            </div>
          ) : artifactBusy ? null : (
            <p className="muted">选择上方标签读取对应产物。</p>
          )}
        </div>
      </Modal>

      {confirmNode}
    </div>
  )
}