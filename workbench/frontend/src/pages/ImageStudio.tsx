/**
 * 生图工坊（全局页，不依赖项目上下文）。
 *
 * 分区：画廊（收藏/复用配置/改提示词/下载/删除/灯箱） + 底部生成条
 *       （提示词 / 尺寸 / 质量 / 格式 / 透明背景 / 审核 / 数量 / 参考图） +
 *       尺寸弹窗 + 模型配置弹窗。
 *
 * 纪律：只用既有 CSS 类（global.css / app.css），不写死颜色；控件全部为原生
 * button/input/select/label（键盘可达）。尺寸公式与后端 image_adapters 对齐
 * （短边基准 {1K:1024, 2K:1440, 4K:2048}，长边 16 对齐）。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Modal from '../components/Modal'
import { useConfirm } from '../components/ConfirmDialog'
import {
  cancelImageJob,
  deleteImageProvider,
  deleteImageRecord,
  enableImageProvider,
  generateImages,
  healthCheckImageProvider,
  listImageJobs,
  listImageProviders,
  listImageRecords,
  patchImageRecord,
  reindexImages,
  upsertImageProvider,
  uploadImageRef,
} from '../api/client'
import type {
  ImageJob,
  ImageProvider,
  ImageRecord,
  ImageUploadRef,
} from '../api/types'
import { errorMessage, useToast } from '../state/useToast'
import { CREDENTIAL_FIELD_EXTRA, NO_AUTOFILL, NO_AUTOFILL_PASSWORD } from '../lib/autofill'

// ─────────────────────────── 常量（与后端 image_adapters 对齐） ───────────────────────────

const BASE_RES: Record<string, number> = { '1K': 1024, '2K': 1440, '4K': 2048 }
const RATIOS = ['1:1', '3:2', '2:3', '16:9', '9:16', '4:3', '3:4', '21:9'] as const
const DEFAULT_CAPS = {
  qualities: ['auto', 'low', 'medium', 'high'],
  formats: ['png', 'jpeg', 'webp'],
  moderations: ['auto', 'low'],
  max_n: 4,
  supports_edit: true,
}

const COMPOSER_KEY = 'aiw.studio.composer'

interface SizeState {
  mode: 'ratio' | 'custom'
  base: string
  ratio: string
  customW: number
  customH: number
}

const DEFAULT_SIZE: SizeState = { mode: 'ratio', base: '2K', ratio: '2:3', customW: 1440, customH: 2160 }

function gcd(a: number, b: number): number {
  return b === 0 ? a : gcd(b, a % b)
}

/** 与后端 calc_size 一致的尺寸计算（仅预览用；校验以后端为准）。 */
function calcSize(base: string, ratio: string): string {
  const short = BASE_RES[base] ?? 1024
  const [rawW, rawH] = ratio.split(':').map((v) => Number.parseInt(v, 10))
  if (!Number.isFinite(rawW) || !Number.isFinite(rawH) || rawW <= 0 || rawH <= 0) {
    return `${short}x${short}`
  }
  const divisor = gcd(rawW, rawH)
  const w = rawW / divisor
  const h = rawH / divisor
  const round16 = (value: number) => Math.max(16, Math.round(value / 16) * 16)
  if (w === h) return `${short}x${short}`
  const big = Math.max(w, h)
  const small = Math.min(w, h)
  const longSide = round16((short * big) / small)
  return w > h ? `${longSide}x${short}` : `${short}x${longSide}`
}

/** 从 "1440x2160" 反解比例标签（用于卡片徽标）。 */
function ratioLabelOf(size: string): string {
  const match = /^(\d+)x(\d+)$/.exec(size ?? '')
  if (!match) return size || '—'
  const w = Number.parseInt(match[1], 10)
  const h = Number.parseInt(match[2], 10)
  if (!w || !h) return size
  const divisor = gcd(w, h)
  const rw = w / divisor
  const rh = h / divisor
  return rw <= 21 && rh <= 21 ? `${rw}:${rh}` : size
}

/** 尝试把尺寸反解为 基准×比例；不在常见组合内则退化为自定义宽高。 */
function decomposeSize(size: string): SizeState {
  const match = /^(\d+)x(\d+)$/.exec(size ?? '')
  if (!match) return { ...DEFAULT_SIZE }
  const w = Number.parseInt(match[1], 10)
  const h = Number.parseInt(match[2], 10)
  const divisor = gcd(w, h)
  const ratioText = `${w / divisor}:${h / divisor}`
  for (const base of Object.keys(BASE_RES)) {
    for (const known of RATIOS) {
      if (ratioText === known && calcSize(base, known) === size) {
        return { mode: 'ratio', base, ratio: known, customW: w, customH: h }
      }
    }
  }
  return { mode: 'custom', base: '2K', ratio: '2:3', customW: w, customH: h }
}

function parseDbTime(text: string): number {
  if (!text) return Date.now()
  const normalized = text.includes('T') ? text : text.replace(' ', 'T')
  const withZone = /Z$|[+-]\d{2}:?\d{2}$/.test(normalized) ? normalized : `${normalized}Z`
  const value = Date.parse(withZone)
  return Number.isNaN(value) ? Date.now() : value
}

function formatElapsed(startText: string, now: number): string {
  const seconds = Math.max(0, Math.floor((now - parseDbTime(startText)) / 1000))
  const mm = String(Math.floor(seconds / 60)).padStart(2, '0')
  const ss = String(seconds % 60).padStart(2, '0')
  return `${mm}:${ss}`
}

function formatTime(value: string): string {
  return value ? value.replace('T', ' ') : '—'
}

// ─────────────────────────── 页面 ───────────────────────────

interface ProviderForm {
  provider_id: string
  display_name: string
  model_id: string
  base_url: string
  api_key: string
  timeout_seconds: number
  enabled: boolean
}

const EMPTY_FORM: ProviderForm = {
  provider_id: '',
  display_name: '',
  model_id: 'gpt-image-2',
  base_url: '',
  api_key: '',
  timeout_seconds: 600,
  enabled: true,
}

export default function ImageStudio() {
  const { push } = useToast()
  const [confirm, confirmNode] = useConfirm()

  // 供应商
  const [providers, setProviders] = useState<ImageProvider[]>([])
  const [configOpen, setConfigOpen] = useState(false)
  const [form, setForm] = useState<ProviderForm | null>(null)
  const [healthText, setHealthText] = useState('')
  const [busy, setBusy] = useState('')
  const [sizeDialogOpen, setSizeDialogOpen] = useState(false)

  // 画廊
  const [records, setRecords] = useState<ImageRecord[]>([])
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  const [search, setSearch] = useState('')
  const [lightboxId, setLightboxId] = useState<number | null>(null)

  // 任务轮询
  const [jobs, setJobs] = useState<ImageJob[]>([])
  const [tick, setTick] = useState(0)

  // 生成条
  const [prompt, setPrompt] = useState('')
  const [refs, setRefs] = useState<ImageUploadRef[]>([])
  const [size, setSize] = useState<SizeState>(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(COMPOSER_KEY) ?? '{}')
      return { ...DEFAULT_SIZE, ...(saved.size ?? {}) }
    } catch {
      return { ...DEFAULT_SIZE }
    }
  })
  const [composer, setComposer] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem(COMPOSER_KEY) ?? '{}')
      return {
        providerId: saved.providerId ?? '',
        quality: saved.quality ?? 'high',
        format: saved.format ?? 'png',
        background: Boolean(saved.background),
        moderation: saved.moderation ?? 'auto',
        n: saved.n ?? 1,
      }
    } catch {
      return { providerId: '', quality: 'high', format: 'png', background: false, moderation: 'auto', n: 1 }
    }
  })
  const fileInputRef = useRef<HTMLInputElement>(null)

  const enabledProviders = useMemo(
    () => providers.filter((provider) => provider.enabled),
    [providers],
  )
  const selected = useMemo(
    () =>
      enabledProviders.find((provider) => provider.provider_id === composer.providerId) ??
      enabledProviders[0] ??
      null,
    [enabledProviders, composer.providerId],
  )
  const caps = selected?.capabilities ?? null
  const qualityOptions = caps?.qualities ?? DEFAULT_CAPS.qualities
  const formatOptions = caps?.formats ?? DEFAULT_CAPS.formats
  const moderationOptions = caps?.moderations ?? DEFAULT_CAPS.moderations
  const maxN = caps?.max_n ?? DEFAULT_CAPS.max_n
  const supportsEdit = caps?.supports_edit ?? DEFAULT_CAPS.supports_edit
  const baseOptions = caps?.base_resolutions?.filter((item) => BASE_RES[item]) ?? Object.keys(BASE_RES)
  const ratioOptions = caps?.ratios ?? (RATIOS as readonly string[])

  const sizeText =
    size.mode === 'ratio' ? calcSize(size.base, size.ratio) : `${size.customW}x${size.customH}`
  const sizeLabel = size.mode === 'ratio' ? `${size.base} · ${size.ratio}` : sizeText

  // 参数记忆（localStorage）
  useEffect(() => {
    localStorage.setItem(
      COMPOSER_KEY,
      JSON.stringify({
        providerId: selected?.provider_id ?? composer.providerId,
        quality: composer.quality,
        format: composer.format,
        background: composer.background,
        moderation: composer.moderation,
        n: composer.n,
        size,
      }),
    )
  }, [selected, composer, size])

  // ── 数据加载 ──

  const refreshProviders = useCallback(async () => {
    try {
      const data = await listImageProviders()
      setProviders(data.providers)
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }, [push])

  const refreshRecords = useCallback(async () => {
    try {
      const data = await listImageRecords({
        favorite: favoriteOnly || undefined,
        search: search || undefined,
        limit: 200,
      })
      setRecords(data.records)
    } catch {
      setRecords([])
    }
  }, [favoriteOnly, search])

  const refreshRecordsRef = useRef(refreshRecords)
  refreshRecordsRef.current = refreshRecords

  const refreshJobs = useCallback(async () => {
    try {
      const data = await listImageJobs(true, 50)
      setJobs(data.jobs)
    } catch {
      setJobs([])
    }
  }, [])

  const jobsCountRef = useRef(0)
  useEffect(() => {
    const poll = async () => {
      try {
        const data = await listImageJobs(true, 50)
        const count = data.jobs.length
        setJobs(data.jobs)
        if (jobsCountRef.current > 0 && count === 0) {
          await refreshRecordsRef.current()
        }
        jobsCountRef.current = count
      } catch {
        setJobs([])
      }
    }
    void poll()
    const timer = window.setInterval(() => void poll(), 2500)
    return () => window.clearInterval(timer)
  }, [])

  // 秒级计时（进行中任务的 elapsed）
  useEffect(() => {
    if (jobs.length === 0) return
    const timer = window.setInterval(() => setTick((value) => value + 1), 1000)
    return () => window.clearInterval(timer)
  }, [jobs.length])
  const nowMs = useMemo(() => Date.now(), [tick])

  // 搜索防抖
  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(search.trim()), 300)
    return () => window.clearTimeout(timer)
  }, [search])

  useEffect(() => {
    void refreshProviders()
  }, [refreshProviders])

  useEffect(() => {
    void refreshRecords()
  }, [refreshRecords])

  // 任务快照变化（任一任务完成 / 新任务）时刷新画廊
  const jobsSignature = useMemo(
    () => jobs.map((job) => `${job.id}:${job.status}`).join(','),
    [jobs],
  )
  useEffect(() => {
    void refreshRecordsRef.current()
  }, [jobsSignature])

  const lightboxRecord = useMemo(
    () => records.find((record) => record.id === lightboxId) ?? null,
    [records, lightboxId],
  )

  // ── 生成 ──

  const handleGenerate = async () => {
    if (!selected) {
      push('请先在「模型配置」中添加并启用图片供应商', 'error')
      setConfigOpen(true)
      return
    }
    if (!prompt.trim()) {
      push('请输入提示词', 'error')
      return
    }
    setBusy('generate')
    try {
      await generateImages({
        provider_id: selected.provider_id,
        prompt: prompt.trim(),
        size: sizeText,
        quality: composer.quality,
        output_format: composer.format,
        background: composer.background,
        moderation: composer.moderation,
        n: Math.min(Math.max(1, composer.n), maxN),
        input_upload_ids: refs.map((item) => item.upload_id),
      })
      push('生成任务已提交', 'success')
      await refreshJobs()
      jobsCountRef.current += 1
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const handleAddRefs = async (files: FileList | null) => {
    if (!files || files.length === 0) return
    const room = 4 - refs.length
    if (room <= 0) {
      push('参考图最多 4 张', 'error')
      return
    }
    setBusy('upload')
    try {
      const added: ImageUploadRef[] = []
      for (const file of Array.from(files).slice(0, room)) {
        added.push(await uploadImageRef(file))
      }
      setRefs((current) => [...current, ...added])
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  /** 复用配置：把画廊记录的参数与提示词回填到生成条。 */
  const applyRecordConfig = (record: ImageRecord) => {
    setPrompt(record.prompt)
    const params = record.params ?? {}
    setComposer((current) => ({
      ...current,
      quality: params.quality && qualityOptions.includes(params.quality)
        ? params.quality
        : current.quality,
      format: params.output_format && formatOptions.includes(params.output_format)
        ? params.output_format
        : current.format,
      background: Boolean(params.background),
      moderation: params.moderation ?? current.moderation,
      n: params.n ?? current.n,
    }))
    setSize(decomposeSize(params.size ?? ''))
    setRefs([])
    push('已复用该图配置', 'success')
  }

  const handleToggleFavorite = async (record: ImageRecord) => {
    try {
      const data = await patchImageRecord(record.id, { favorite: !record.favorite })
      setRecords((current) =>
        current.map((item) => (item.id === record.id ? data.record : item)),
      )
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleEditPrompt = (record: ImageRecord) => {
    setPrompt(record.prompt)
    push('提示词已填入生成条', 'success')
  }

  const handleDeleteRecord = async (record: ImageRecord) => {
    const confirmed = await confirm({
      title: '删除图片',
      message: '确定删除这张图片吗？删除后不可恢复。',
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    try {
      await deleteImageRecord(record.id)
      setRecords((current) => current.filter((item) => item.id !== record.id))
      if (lightboxId === record.id) setLightboxId(null)
      push('已删除', 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleCancelJob = async (job: ImageJob) => {
    try {
      await cancelImageJob(job.id)
      await refreshJobs()
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleDownload = (record: ImageRecord) => {
    const ext = (record.params?.output_format ?? 'png').replace('jpeg', 'jpg')
    const anchor = document.createElement('a')
    anchor.href = record.url
    anchor.download = `生图-${record.id}.${ext}`
    document.body.appendChild(anchor)
    anchor.click()
    anchor.remove()
  }

  // ── 模型配置 ──

  const openCreateForm = () => {
    setHealthText('')
    setForm({ ...EMPTY_FORM })
  }

  const openEditForm = (provider: ImageProvider) => {
    setHealthText('')
    setForm({
      provider_id: provider.provider_id,
      display_name: provider.display_name,
      model_id: provider.model_id,
      base_url: provider.base_url,
      api_key: '',
      timeout_seconds: provider.timeout_seconds,
      enabled: provider.enabled,
    })
  }

  const handleSaveProvider = async () => {
    if (!form) return
    if (!form.provider_id.trim() || !form.model_id.trim()) {
      push('供应商标识与模型 ID 不能为空', 'error')
      return
    }
    setBusy('save-provider')
    try {
      await upsertImageProvider({
        provider_id: form.provider_id.trim(),
        display_name: form.display_name.trim(),
        model_id: form.model_id.trim(),
        base_url: form.base_url.trim(),
        api_key: form.api_key.trim() ? form.api_key.trim() : null,
        timeout_seconds: form.timeout_seconds,
        enabled: form.enabled,
      })
      await refreshProviders()
      setForm(null)
      push('供应商已保存', 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  const handleToggleProvider = async (provider: ImageProvider) => {
    try {
      const data = await enableImageProvider(provider.provider_id, !provider.enabled)
      setProviders((current) =>
        current.map((item) => (item.provider_id === provider.provider_id ? data : item)),
      )
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleDeleteProvider = async (provider: ImageProvider) => {
    const confirmed = await confirm({
      title: '删除供应商',
      message: `确定删除供应商「${provider.display_name}」吗？`,
      confirmText: '删除',
      danger: true,
    })
    if (!confirmed) return
    try {
      await deleteImageProvider(provider.provider_id)
      await refreshProviders()
      push('供应商已删除', 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    }
  }

  const handleHealthCheck = async (provider: ImageProvider) => {
    setHealthText('检查中…')
    try {
      const result = await healthCheckImageProvider(provider.provider_id)
      setHealthText(
        result.ok
          ? `连通正常（/models ${result.model_count ?? 0} 个模型${result.model_matched ? '' : `；${result.note ?? '模型未在列表'}`}）`
          : `失败：${result.reason ?? '未知原因'}`,
      )
    } catch (err) {
      setHealthText(`失败：${errorMessage(err)}`)
    }
  }

  const handleReindex = async () => {
    setBusy('reindex')
    try {
      const result = await reindexImages()
      await refreshRecords()
      push(`索引重建完成：${result.rebuilt} 张（跳过 ${result.skipped}）`, 'success')
    } catch (err) {
      push(errorMessage(err), 'error')
    } finally {
      setBusy('')
    }
  }

  // ── 渲染 ──

  return (
    <div className="studio">
      <div className="studio-toolbar">
        <label className="field studio-toolbar__provider">
          <span className="field__label">图片模型</span>
          <select
            className="select"
            value={selected?.provider_id ?? ''}
            onChange={(event) =>
              setComposer((current) => ({ ...current, providerId: event.target.value }))
            }
            disabled={enabledProviders.length === 0}
          >
            {enabledProviders.length === 0 ? (
              <option value="">未配置供应商</option>
            ) : (
              enabledProviders.map((provider) => (
                <option key={provider.provider_id} value={provider.provider_id}>
                  {provider.display_name}（{provider.model_id}）
                </option>
              ))
            )}
          </select>
        </label>
        <button
          className={`btn btn--sm ${favoriteOnly ? 'btn--primary' : 'btn--ghost'}`}
          type="button"
          aria-pressed={favoriteOnly}
          onClick={() => setFavoriteOnly((value) => !value)}
        >
          ★ 只看收藏
        </button>
        <input
          autoComplete={NO_AUTOFILL}
          className="input studio-toolbar__search"
          type="search"
          placeholder="搜索提示词…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
        <button
          className="btn btn--ghost btn--sm"
          type="button"
          onClick={handleReindex}
          disabled={busy === 'reindex'}
          title="从磁盘 sidecar 重建画廊索引"
        >
          重建索引
        </button>
        <button className="btn btn--primary btn--sm" type="button" onClick={() => { setForm(null); setConfigOpen(true) }}>
          模型配置
        </button>
      </div>

      <div className="studio-body">
        {providers.length === 0 ? (
          <div className="empty-state">
            <p className="empty-state__title">还没有配置图片模型</p>
            <p className="empty-state__desc">点击右上角「模型配置」，填写 base_url 与 API Key。</p>
          </div>
        ) : null}

        {jobs.map((job) => (
          <div className="img-card img-card--job" key={`job-${job.id}`}>
            <div className="img-card__thumb img-card__thumb--pending">
              <span className="img-card__timer">{formatElapsed(job.created_at, nowMs)}</span>
            </div>
            <div className="img-card__body">
              <p className="img-card__prompt">{job.prompt || '（无提示词）'}</p>
              <div className="img-card__tags">
                <span className="tag tag--warn">{job.mode === 'edit' ? '垫图编辑' : '生成中'}</span>
                <span className="tag">{job.params?.size ?? 'auto'}</span>
                <span className="tag">×{job.params?.n ?? 1}</span>
              </div>
              <div className="btn-row">
                <button className="btn btn--ghost btn--sm" type="button" onClick={() => handleCancelJob(job)}>
                  取消
                </button>
              </div>
            </div>
          </div>
        ))}

        {records.length > 0 ? (
          <div className="gallery-grid">
            {records.map((record) => (
              <article className="img-card" key={record.id}>
                <button
                  className="img-card__thumb"
                  type="button"
                  onClick={() => setLightboxId(record.id)}
                  title="查看大图"
                >
                  <img src={record.url} alt={record.prompt.slice(0, 40)} loading="lazy" />
                  <span className="img-card__badges">
                    <span className="img-card__badge">{ratioLabelOf(record.size)}</span>
                    <span className="img-card__badge">{record.size || 'auto'}</span>
                  </span>
                </button>
                <div className="img-card__body">
                  <p className="img-card__prompt" title={record.prompt}>{record.prompt}</p>
                  <div className="img-card__tags">
                    <span className="tag tag--primary">{record.model_id}</span>
                    {record.params?.quality ? <span className="tag">质量 {record.params.quality}</span> : null}
                    {record.params?.output_format ? <span className="tag">{record.params.output_format.toUpperCase()}</span> : null}
                    {record.params?.input_uploads?.length ? (
                      <span className="tag tag--warn">垫图 ×{record.params.input_uploads.length}</span>
                    ) : null}
                  </div>
                  <div className="img-card__actions">
                    <button
                      className={`btn btn--ghost btn--sm${record.favorite ? ' is-starred' : ''}`}
                      type="button"
                      aria-pressed={record.favorite}
                      title={record.favorite ? '取消收藏' : '收藏'}
                      onClick={() => handleToggleFavorite(record)}
                    >
                      {record.favorite ? '★' : '☆'}
                    </button>
                    <button className="btn btn--ghost btn--sm" type="button" title="复用配置" onClick={() => applyRecordConfig(record)}>
                      复用配置
                    </button>
                    <button className="btn btn--ghost btn--sm" type="button" title="填入提示词" onClick={() => handleEditPrompt(record)}>
                      改提示词
                    </button>
                    <button className="btn btn--ghost btn--sm" type="button" title="下载" onClick={() => handleDownload(record)}>
                      下载
                    </button>
                    <button className="btn btn--danger btn--sm" type="button" title="删除" onClick={() => handleDeleteRecord(record)}>
                      删除
                    </button>
                  </div>
                </div>
              </article>
            ))}
          </div>
        ) : (
          providers.length > 0 && jobs.length === 0 ? (
            <div className="empty-state">
              <p className="empty-state__title">画廊还是空的</p>
              <p className="empty-state__desc">在下方输入提示词开始第一张图的生成。</p>
            </div>
          ) : null
        )}
      </div>

      {/* 底部生成条 */}
      <div className="composer">
        {refs.length > 0 ? (
          <div className="composer__refs">
            {refs.map((item) => (
              <span className="composer__ref" key={item.upload_id} title={item.filename}>
                <span className="composer__ref-name">{item.filename}</span>
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  aria-label={`移除参考图 ${item.filename}`}
                  onClick={() => setRefs((current) => current.filter((ref) => ref.upload_id !== item.upload_id))}
                >
                  ×
                </button>
              </span>
            ))}
            <span className="muted">将基于参考图编辑</span>
          </div>
        ) : null}
        <textarea
          autoComplete={NO_AUTOFILL}
          className="textarea composer__textarea"
          placeholder={
            refs.length > 0
              ? '描述要如何编辑参考图…'
              : '描述你想要的画面，例如：竖版封面，主角背影望向云海之上的古城，暖灰与青蓝主色调…'
          }
          value={prompt}
          rows={3}
          onChange={(event) => setPrompt(event.target.value)}
          onKeyDown={(event) => {
            if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
              event.preventDefault()
              void handleGenerate()
            }
          }}
        />
        <div className="composer__opts">
          <button className="btn btn--ghost btn--sm" type="button" onClick={() => setSizeDialogOpen(true)} title="设置图像尺寸">
            尺寸 {sizeLabel}（{sizeText}）
          </button>
          <label className="composer__opt">
            <span>质量</span>
            <select
              className="select"
              value={composer.quality}
              onChange={(event) =>
                setComposer((current) => ({ ...current, quality: event.target.value }))
              }
            >
              {qualityOptions.map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </label>
          <label className="composer__opt">
            <span>格式</span>
            <select
              className="select"
              value={composer.format}
              onChange={(event) =>
                setComposer((current) => ({ ...current, format: event.target.value }))
              }
            >
              {formatOptions.map((item) => (
                <option key={item} value={item}>{item.toUpperCase()}</option>
              ))}
            </select>
          </label>
          <label className="composer__opt">
            <span>透明背景</span>
            <select
              className="select"
              value={composer.background ? 'true' : 'false'}
              onChange={(event) =>
                setComposer((current) => ({ ...current, background: event.target.value === 'true' }))
              }
            >
              <option value="false">false</option>
              <option value="true">true</option>
            </select>
          </label>
          <label className="composer__opt">
            <span>审核</span>
            <select
              className="select"
              value={composer.moderation}
              onChange={(event) =>
                setComposer((current) => ({ ...current, moderation: event.target.value }))
              }
            >
              {moderationOptions.map((item) => (
                <option key={item} value={item}>{item}</option>
              ))}
            </select>
          </label>
          <label className="composer__opt">
            <span>数量</span>
            <input
              className="input"
              type="number"
              min={1}
              max={maxN}
              value={composer.n}
              onChange={(event) =>
                setComposer((current) => ({
                  ...current,
                  n: Math.min(Math.max(1, Number(event.target.value) || 1), maxN),
                }))
              }
            />
          </label>
          {supportsEdit ? (
            <button
              className="btn btn--ghost btn--sm"
              type="button"
              title="添加参考图（最多 4 张）"
              onClick={() => fileInputRef.current?.click()}
              disabled={busy === 'upload'}
            >
              📎 参考图
            </button>
          ) : null}
          <input
            ref={fileInputRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            multiple
            hidden
            onChange={(event) => void handleAddRefs(event.target.files)}
          />
          <button
            className="btn btn--primary composer__submit"
            type="button"
            onClick={() => void handleGenerate()}
            disabled={busy === 'generate' || !selected}
            title={selected ? '生成（Ctrl+Enter）' : '请先配置并启用图片供应商'}
          >
            {busy === 'generate' ? '提交中…' : '生成 →'}
          </button>
        </div>
      </div>

      {/* 尺寸弹窗 */}
      <SizeDialog
        open={sizeDialogOpen}
        value={size}
        baseOptions={baseOptions}
        ratioOptions={ratioOptions}
        minSide={caps?.min_side ?? 256}
        maxSide={caps?.max_side ?? 4096}
        onClose={() => setSizeDialogOpen(false)}
        onApply={(next) => {
          setSize(next)
          setSizeDialogOpen(false)
        }}
      />

      {/* 模型配置弹窗 */}
      <Modal
        title="图片模型配置"
        open={configOpen}
        onClose={() => setConfigOpen(false)}
        width={760}
      >
        <div className="stack">
          {providers.length === 0 ? (
            <p className="muted">还没有图片供应商，先在下方新增一个。</p>
          ) : (
            <div className="stack">
              {providers.map((provider) => (
                <div className="panel provider-row" key={provider.provider_id}>
                  <div className="provider-row__main">
                    <strong>{provider.display_name}</strong>
                    <span className="mono muted">{provider.provider_id}</span>
                    <span className="tag tag--primary">{provider.model_id}</span>
                    {provider.enabled ? <span className="tag tag--ok">已启用</span> : <span className="tag">未启用</span>}
                    {provider.health ? (
                      <span className={`tag${provider.health === 'ok' ? ' tag--ok' : ' tag--warn'}`}>
                        {provider.health}
                      </span>
                    ) : null}
                  </div>
                  <div className="muted mono provider-row__url">{provider.base_url || '（未填 base_url）'}</div>
                  <div className="btn-row">
                    <button className="btn btn--ghost btn--sm" type="button" onClick={() => handleToggleProvider(provider)}>
                      {provider.enabled ? '停用' : '启用'}
                    </button>
                    <button className="btn btn--ghost btn--sm" type="button" onClick={() => void handleHealthCheck(provider)}>
                      健康检查
                    </button>
                    <button className="btn btn--ghost btn--sm" type="button" onClick={() => openEditForm(provider)}>
                      编辑
                    </button>
                    <button className="btn btn--danger btn--sm" type="button" onClick={() => handleDeleteProvider(provider)}>
                      删除
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}

          {healthText ? <p className="muted">{healthText}</p> : null}

          <hr className="provider-sep" />
          <h3 className="provider-form__title">{form && providers.some((p) => p.provider_id === form.provider_id) ? '编辑供应商' : '新增供应商'}</h3>
          {form ? (
            <div className="stack">
              <div className="provider-form__grid">
                <label className="field">
                  <span className="field__label">供应商标识（唯一）</span>
                  <input
                    className="input"
                    name="image-provider-id"
                    autoComplete={NO_AUTOFILL}
                    value={form.provider_id}
                    disabled={providers.some((p) => p.provider_id === form.provider_id)}
                    onChange={(event) => setForm({ ...form, provider_id: event.target.value })}
                    placeholder="my-image-api"
                  />
                </label>
                <label className="field">
                  <span className="field__label">显示名称</span>
                  <input
                    className="input"
                    name="image-provider-name"
                    autoComplete={NO_AUTOFILL}
                    value={form.display_name}
                    onChange={(event) => setForm({ ...form, display_name: event.target.value })}
                    placeholder="我的生图网关"
                  />
                </label>
                <label className="field">
                  <span className="field__label">模型 ID</span>
                  <input
                    className="input"
                    name="image-model-id"
                    autoComplete={NO_AUTOFILL}
                    value={form.model_id}
                    list="image-model-options"
                    onChange={(event) => setForm({ ...form, model_id: event.target.value })}
                    placeholder="gpt-image-2"
                  />
                  <datalist id="image-model-options">
                    <option value="gpt-image-2" />
                    <option value="gpt-image-1" />
                  </datalist>
                </label>
                <label className="field">
                  <span className="field__label">base_url（OpenAI 兼容）</span>
                  <input
                    className="input"
                    name="image-api-base-url"
                    autoComplete={NO_AUTOFILL}
                    {...CREDENTIAL_FIELD_EXTRA}
                    value={form.base_url}
                    onChange={(event) => setForm({ ...form, base_url: event.target.value })}
                    placeholder="https://api.openai.com/v1"
                  />
                </label>
                <label className="field">
                  <span className="field__label">API Key（留空表示不修改）</span>
                  <input
                    className="input"
                    type="password"
                    name="image-api-key"
                    autoComplete={NO_AUTOFILL_PASSWORD}
                    {...CREDENTIAL_FIELD_EXTRA}
                    value={form.api_key}
                    onChange={(event) => setForm({ ...form, api_key: event.target.value })}
                    placeholder={
                      providers.find((p) => p.provider_id === form.provider_id)?.masked_secret || 'sk-…'
                    }
                  />
                </label>
                <label className="field">
                  <span className="field__label">超时（秒）</span>
                  <input
                    className="input"
                    type="number"
                    min={30}
                    max={1800}
                    value={form.timeout_seconds}
                    onChange={(event) =>
                      setForm({ ...form, timeout_seconds: Number(event.target.value) || 600 })
                    }
                  />
                </label>
              </div>
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(event) => setForm({ ...form, enabled: event.target.checked })}
                />
                保存后立即启用
              </label>
              <div className="btn-row btn-row--end">
                <button className="btn btn--ghost btn--sm" type="button" onClick={() => setForm(null)}>
                  取消
                </button>
                <button
                  className="btn btn--primary btn--sm"
                  type="button"
                  onClick={() => void handleSaveProvider()}
                  disabled={busy === 'save-provider'}
                >
                  保存
                </button>
              </div>
            </div>
          ) : (
            <div className="btn-row">
              <button className="btn btn--primary btn--sm" type="button" onClick={openCreateForm}>
                ＋ 新增供应商
              </button>
            </div>
          )}
        </div>
      </Modal>

      {/* 灯箱 */}
      {lightboxRecord ? (
        <div
          className="lightbox"
          role="dialog"
          aria-modal="true"
          aria-label="查看图片"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) setLightboxId(null)
          }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') setLightboxId(null)
          }}
          tabIndex={-1}
          ref={(node) => node?.focus()}
        >
          <div className="lightbox__panel">
            <img className="lightbox__img" src={lightboxRecord.url} alt={lightboxRecord.prompt.slice(0, 60)} />
            <div className="lightbox__meta">
              <p className="lightbox__prompt">{lightboxRecord.prompt}</p>
              <p className="muted mono">
                {lightboxRecord.model_id} · {lightboxRecord.size || 'auto'} ·{' '}
                {lightboxRecord.params?.quality ?? '—'} · {lightboxRecord.params?.output_format?.toUpperCase() ?? '—'} ·{' '}
                {formatTime(lightboxRecord.created_at)}
              </p>
              <div className="btn-row">
                <button className="btn btn--ghost btn--sm" type="button" onClick={() => handleDownload(lightboxRecord)}>
                  下载
                </button>
                <button className="btn btn--ghost btn--sm" type="button" onClick={() => { applyRecordConfig(lightboxRecord); setLightboxId(null) }}>
                  复用配置
                </button>
                <button className="btn btn--danger btn--sm" type="button" onClick={() => handleDeleteRecord(lightboxRecord)}>
                  删除
                </button>
                <button className="btn btn--ghost btn--sm" type="button" onClick={() => setLightboxId(null)}>
                  关闭
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      {confirmNode}
    </div>
  )
}

// ─────────────────────────── 尺寸弹窗 ───────────────────────────

function SizeDialog(props: {
  open: boolean
  value: SizeState
  baseOptions: string[]
  ratioOptions: readonly string[]
  minSide: number
  maxSide: number
  onClose: () => void
  onApply: (next: SizeState) => void
}) {
  const { open, value, baseOptions, ratioOptions, minSide, maxSide, onClose, onApply } = props
  const [draft, setDraft] = useState<SizeState>(value)
  const [customRatio, setCustomRatio] = useState('2:3')

  useEffect(() => {
    if (open) {
      setDraft(value)
      setCustomRatio(value.mode === 'ratio' ? value.ratio : '2:3')
    }
  }, [open, value])

  if (!open) return null

  const preview =
    draft.mode === 'ratio' ? calcSize(draft.base, draft.ratio) : `${draft.customW}x${draft.customH}`

  return (
    <Modal title="设置图像尺寸" open={open} onClose={onClose} width={480}>
      <div className="stack">
        <p className="muted">当前：{preview}</p>
        <div className="seg" role="tablist">
          <button
            className={`seg__item${draft.mode === 'ratio' ? ' is-active' : ''}`}
            type="button"
            role="tab"
            aria-selected={draft.mode === 'ratio'}
            onClick={() => setDraft({ ...draft, mode: 'ratio' })}
          >
            按比例
          </button>
          <button
            className={`seg__item${draft.mode === 'custom' ? ' is-active' : ''}`}
            type="button"
            role="tab"
            aria-selected={draft.mode === 'custom'}
            onClick={() => setDraft({ ...draft, mode: 'custom' })}
          >
            自定义宽高
          </button>
        </div>

        {draft.mode === 'ratio' ? (
          <>
            <div className="field">
              <span className="field__label">基准分辨率</span>
              <div className="seg">
                {baseOptions.map((item) => (
                  <button
                    key={item}
                    className={`seg__item${draft.base === item ? ' is-active' : ''}`}
                    type="button"
                    aria-pressed={draft.base === item}
                    onClick={() => setDraft({ ...draft, base: item })}
                  >
                    {item}
                  </button>
                ))}
              </div>
            </div>
            <div className="field">
              <span className="field__label">图像比例</span>
              <div className="ratio-grid">
                {ratioOptions.map((item) => {
                  const [rw, rh] = item.split(':').map(Number)
                  const long = 26
                  const scale = long / Math.max(rw || 1, rh || 1)
                  return (
                    <button
                      key={item}
                      className={`ratio-cell${draft.ratio === item ? ' is-active' : ''}`}
                      type="button"
                      aria-pressed={draft.ratio === item}
                      onClick={() => setDraft({ ...draft, ratio: item })}
                    >
                      <span
                        className="ratio-cell__icon"
                        style={{ width: Math.max(6, (rw || 1) * scale), height: Math.max(6, (rh || 1) * scale) }}
                        aria-hidden="true"
                      />
                      <span>{item}</span>
                    </button>
                  )
                })}
              </div>
            </div>
            <div className="field">
              <span className="field__label">自定义比例（w:h）</span>
              <div className="row">
                <input
                  autoComplete={NO_AUTOFILL}
                  className="input"
                  value={customRatio}
                  onChange={(event) => setCustomRatio(event.target.value)}
                  placeholder="7:5"
                />
                <button
                  className="btn btn--ghost btn--sm"
                  type="button"
                  onClick={() => setDraft({ ...draft, ratio: customRatio.trim() })}
                >
                  应用比例
                </button>
                <span className={`tag${draft.ratio === customRatio.trim() ? ' tag--ok' : ''}`}>
                  {draft.ratio}
                </span>
              </div>
            </div>
          </>
        ) : (
          <div className="provider-form__grid">
            <label className="field">
              <span className="field__label">宽（px）</span>
              <input
                className="input"
                type="number"
                min={minSide}
                max={maxSide}
                value={draft.customW}
                onChange={(event) =>
                  setDraft({ ...draft, customW: Math.min(Math.max(minSide, Number(event.target.value) || minSide), maxSide) })
                }
              />
            </label>
            <label className="field">
              <span className="field__label">高（px）</span>
              <input
                className="input"
                type="number"
                min={minSide}
                max={maxSide}
                value={draft.customH}
                onChange={(event) =>
                  setDraft({ ...draft, customH: Math.min(Math.max(minSide, Number(event.target.value) || minSide), maxSide) })
                }
              />
            </label>
            <p className="muted field__hint">边长范围 {minSide}-{maxSide}。</p>
          </div>
        )}

        <div className="row row--between size-dialog__footer">
          <span>
            将使用 <strong>{preview}</strong>
          </span>
          <span className="btn-row">
            <button className="btn btn--ghost" type="button" onClick={onClose}>
              取消
            </button>
            <button className="btn btn--primary" type="button" onClick={() => onApply(draft)}>
              确定
            </button>
          </span>
        </div>
      </div>
    </Modal>
  )
}
