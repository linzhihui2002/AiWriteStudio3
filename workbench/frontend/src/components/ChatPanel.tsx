/** Presentational project assistant; lifecycle and drafts belong to the project controller. */
import { createPortal } from 'react-dom'
import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { Link } from 'react-router-dom'
import type { ChatFileChange, ChatStep, ChatRun, ChatPermissionMode, ContextPreview } from '../api/types'
import InlineEdit from './InlineEdit'
import MarkdownView from './MarkdownView'
import DiffView from './DiffView'
import Drawer from './Drawer'
import { NO_AUTOFILL } from '../lib/autofill'
import { copyTextToClipboard } from '../lib/clipboard'
import { CHAT_PERMISSION_OPTIONS, routingCard, savedInteractions } from '../lib/chatState'
import type { RoutingCard } from '../lib/chatState'
import ChatInteractionCard from './ChatInteractionCard'
import ChatMemoryPanel from './ChatMemoryPanel'
import { chatSessionMatches, isChatSendShortcut } from '../lib/chatDraftState'
import { chapterLabel } from '../lib/chapterName'
import Icon from './Icon'
import ChatHistoryMessage from './ChatHistoryMessage'
import { statusText, savedSteps, savedChanges, isTerminal, type ChatController } from '../state/useChatController'
import { errorMessage } from '../state/useToast'
import { ActionMenu } from './WorkspacePage'
import PanelResizer from './PanelResizer'
import { usePanelWidth } from '../state/usePanelWidth'

export default function ChatPanel({ controller }: { controller: ChatController }) {
  const {
    confirmNode, renamingTitle, setRenamingTitle, sessions, activeId, messages,
    run, routing, agents, providers, quickCards, input,
    inputError, setInputError, loading, sessionsLoading, sending, configuring,
    reconnecting, connectionError, setSubscriptionVersion, historyOpen, setHistoryOpen, historySearch,
    setHistorySearch, referencesOpen, setReferencesOpen, materialPreview, setMaterialPreview, materialPreviewSequence,
    newContent, setNewContent, sendPhase, targetChoice, selectionContext, settingsOpen,
    setSettingsOpen, defaultsLoading,
    references, setReferences, includeCurrent, setIncludeCurrent, includeSelection,
    setIncludeSelection, fileSearch, setFileSearch, diff, setDiff, reverting,
    memoryOpen, setMemoryOpen, sourceTarget, setSourceTarget, naming, inputRef,
    scrollRef, followBottomRef, scrollPositionRef, callbacksRef, hasRetry, revisionRef,
    actionsRef, changeInput, changeTarget, actualTarget, current, steps,
    busy, selectedModel, selectedAgent, selectedPermission, discussionOnly, runPermission,
    runDiscussionOnly, permissionLabel, modelLabel, selectSession, updateSettings,
    send, stop, commitRename, autoName, remove,
    previewMaterial, notify, settingsLoading, projectId, chapterRel,
    targetChapterRel, selection, hasUnsavedChanges, availableFiles, onCollapse, availableChapters,
    portalHost, presentation
  } = controller
  const layoutRef = useRef<HTMLDivElement>(null)
  const [materialWidth, setMaterialWidth] = usePanelWidth('aiw.chat.materialsWidth', 320, 260, 560)
  const [materialsCollapsed, setMaterialsCollapsed] = useState(() => localStorage.getItem('aiw.chat.materialsCollapsed') === '1')
  const foldMaterials = (value: boolean) => { setMaterialsCollapsed(value); localStorage.setItem('aiw.chat.materialsCollapsed', value ? '1' : '0') }
  const [wideMaterials, setWideMaterials] = useState(false)
  useEffect(() => {
    const element = layoutRef.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => setWideMaterials(entry.contentRect.width >= 1000))
    observer.observe(element)
    return () => observer.disconnect()
  }, [portalHost])
  const inlineMaterials = presentation === 'page' && wideMaterials && !materialsCollapsed && !referencesOpen && !historyOpen && !settingsOpen
  const closeMaterial = () => { materialPreviewSequence.current += 1; setMaterialPreview(null) }
  const openPreviewFile = () => {
    if (!materialPreview) return
    const path = materialPreview.path; closeMaterial(); setReferencesOpen(false)
    void Promise.resolve(callbacksRef.current.onOpenFile(path)).catch(error => notify(errorMessage(error), 'error'))
  }
  const materialContents = <>
    {materialPreview?.loading ? <p className="muted" role="status">正在读取材料…</p> : null}
    {materialPreview?.error ? <div className="chat__error" role="alert">{materialPreview.error}<button className="btn btn--sm" onClick={() => void previewMaterial(materialPreview.path)}>重新读取</button></div> : null}
    {materialPreview?.hash ? <details className="chat__material-version"><summary>文件版本 · {materialPreview.hash.slice(0, 10)}</summary><p>{materialPreview.path}</p><p>{materialPreview.hash}</p>{materialPreview.mtime ? <p>修改时间：{new Date(materialPreview.mtime * 1000).toLocaleString()}</p> : null}</details> : null}
    {materialPreview?.content ? <MarkdownView text={materialPreview.content} plain={/^章节\/.*\.(txt|md)$/.test(materialPreview.path)} document /> : !materialPreview?.loading && !materialPreview?.error ? <p className="muted">文件内容为空。</p> : null}
  </>
  const diffContents = diff ? Array.isArray(diff.diff) ? <DiffView lines={diff.diff} /> : typeof diff.diff === 'string' ? <pre className="chat__diff-text">{diff.diff}</pre> : <div className="chat__diff-fallback"><section><h3>修改前</h3><pre>{diff.before_content ?? '文件不存在'}</pre></section><section><h3>修改后</h3><pre>{diff.after_content ?? '文件已移除'}</pre></section></div> : null
  const renderSteps = (items: ChatStep[], live = false) => items.length > 0 ? (
    <details className="chat__activity">
      <summary>{live && busy ? '正在推进 · ' : ''}处理详情 · {items.length} 项</summary>
      <div className="chat__steps">{items.map((step, index) => (
        <details key={step.call_id || `${step.index}-${index}`} className={`chat__step chat__step--${step.status}${step.parent_call_id ? ' chat__step--child' : ''}`}>
          <summary><span className="chat__step-icon">{step.status === 'running' ? '执行' : step.status === 'done' ? '完成' : step.status === 'cancelled' ? '停止' : '异常'}</span> <span className="chat__step-kind">{({ tool: '工具', skill: '技能', agent: '角色', phase: '阶段', plan_step: '协作' } as Record<string, string>)[step.kind || 'tool'] || '步骤'}</span> {step.label || step.tool}
            {step.elapsed_ms !== undefined ? <span className="chat__step-duration">{(step.elapsed_ms / 1000).toFixed(1)} 秒</span> : null}
            <span className="chat__step-summary">{step.summary || (step.status === 'running' ? '进行中' : '')}</span></summary>
          <div className="chat__step-details">{step.tool ? <div className="muted">调用：{step.tool}</div> : null}{step.started_at ? <div className="muted">开始：{new Date(step.started_at).toLocaleTimeString()}{step.finished_at ? ` · 结束：${new Date(step.finished_at).toLocaleTimeString()}` : ''}</div> : null}
            {Object.keys(step.args || {}).length > 0 ? <><strong>输入</strong><pre className="chat__tool-detail">{JSON.stringify(step.args, null, 2)}</pre></> : null}
            {step.result !== undefined && step.result !== null ? <><strong>结果</strong><pre className="chat__tool-detail">{typeof step.result === 'string' ? step.result : JSON.stringify(step.result, null, 2)}</pre></> : null}</div>
        </details>
      ))}</div>
    </details>
  ) : null

  const renderChanges = (changes: ChatFileChange[]) => changes.length > 0 ? (
    <div className="chat__changes" aria-label="本轮文件改动">
      <div className="chat__changes-title">文件改动 · {changes.length}</div>
      {changes.map((change) => {
        const reverted = change.reverted || change.status === 'reverted'
        return <div className="chat__change" key={change.id}>
          <div className="chat__change-name"><span>{({ create: '新建', write: '修改', edit: '修改', rewrite: '整篇覆盖', replace_exact: '局部修改', move: '移动', rename: '重命名', delete: '删除' } as Record<string, string>)[change.operation] || change.operation}</span>
            <strong>{change.path}{change.destination ? ` → ${change.destination}` : ''}</strong></div>
          <div className="chat__change-actions"><span className="muted">{reverted ? '已撤回' : ({ pending: '待应用', proposed: '待应用', applied: '已保存', failed: '未完成', rejected: '门禁未通过', applying: '保存中' } as Record<string, string>)[change.status] || change.status}</span>
            <button className="btn btn--ghost btn--sm" type="button" onClick={() => setDiff(change)}>查看差异</button>
            {change.operation !== 'delete' || reverted ? <button className="btn btn--ghost btn--sm" type="button" onClick={() => void Promise.resolve(callbacksRef.current.onOpenFile(reverted ? change.path : change.destination || change.path)).catch((err) => setInputError(errorMessage(err)))}>打开</button> : null}
            <button className="btn btn--ghost btn--sm" type="button" title={discussionOnly ? '当前对话仅讨论，不能撤回文件改动' : undefined} disabled={busy || discussionOnly || reverted || reverting !== null || change.status !== 'applied'} onClick={() => void actionsRef.current?.undo(change)}>{reverting === change.id ? '撤回中…' : '撤回'}</button>
          </div>
        </div>
      })}
    </div>
  ) : null

  // 路由卡：处理者 / 本轮目标材料 / 协作步骤；作者可在「高级设置 → 写作分工」改选执行角色。
  const renderRouting = (card: RoutingCard | null) => card ? (
    <details className="chat__activity">
      <summary>本轮路由 · {card.agent_title || card.agent}{card.plan.length > 1 ? ` · 协作 ${card.plan.length} 步` : ''}</summary>
      <ul>
        <li><strong>处理者</strong><div className="muted">{card.agent_title || card.agent}{card.agent_title && card.agent ? `（${card.agent}）` : ''}</div></li>
        <li><strong>本轮目标材料</strong><div className="muted">{card.scope_label || card.write_targets.join('、') || '未声明'}</div></li>
        {card.plan.map((step) => <li key={step.step}><strong>步骤 {step.step}/{card.plan.length}</strong><div className="muted">{step.agent_title || step.agent}{step.write_targets?.length ? ` · 可写：${step.write_targets.join('、')}` : ''}{step.note ? ` · ${step.note}` : ''}</div></li>)}
      </ul>
      <p className="muted">可在对话设置中选择写作分工；角色选择用于下一次发送。</p>
    </details>
  ) : null

  const renderContext = (preview?: ContextPreview) => preview?.items?.length ? <details className="chat__activity">
    <summary>本轮引用 · {preview.items.length} 项 · 约 {preview.total_tokens} tokens</summary>
    <ul>{preview.items.map((item, index) => <li key={`${item.source}-${index}`}><strong>{item.title}</strong><div className="muted">{item.source}</div><small>{item.chars} 字符{item.truncated ? ' · 已按预算截断' : ''}</small></li>)}</ul>
    {preview.degradation?.length ? <p className="muted">部分材料已按上下文预算裁剪。</p> : null}
  </details> : null

  const renderResult = (text: string, meta: Record<string, unknown>, live = false) => <>
    {text ? <MarkdownView text={text} /> : live ? <p className="muted">{reconnecting ? '连接恢复后会接着显示，任务仍在后台运行…' : run?.status === 'waiting_input' ? '回应问题或批准请求后继续处理。' : run?.status_message || '正在阅读项目并处理你的要求…'}</p> : null}
    {text ? <button type="button" className="btn btn--ghost btn--sm chat__copy" onClick={() => { try { copyTextToClipboard(text); notify('已复制完整回复', 'success') } catch { notify('复制失败，请选择回复文字后复制。', 'error') } }}>复制回复</button> : null}
    {(live ? run?.plan_state : meta.plan_state as ChatRun['plan_state'])?.steps?.length ? <ol className="chat__plan">{(live ? run?.plan_state : meta.plan_state as ChatRun['plan_state'])!.steps.map((step, index) => <li key={step.step_id || index}><strong>{step.label || agents.find(agent => agent.name === step.agent)?.title || (step.write_targets?.length ? `处理${step.write_targets.join('、')}` : `步骤 ${step.step || index + 1}`)}{step.reused ? ' · 沿用已验证交付' : ''}</strong><span>{statusText(step.status)}</span>{step.failure_reason || step.error ? <small>{step.failure_reason || step.error}</small> : null}</li>)}</ol> : null}
    {renderRouting(live ? routing : routingCard(meta.routing))}
    {renderSteps(live ? steps : savedSteps(meta.steps), live)}
    {renderContext(live ? run?.context_preview : meta.context_preview as ContextPreview | undefined)}
    {(live ? run?.context_warnings || [] : Array.isArray(meta.context_warnings) ? meta.context_warnings as string[] : []).map((warning, index) => <div className="chat__context-warning" role="status" key={index}>{warning}</div>)}
    {(live ? run?.interactions || [] : savedInteractions(meta.interactions)).map((interaction) => <div key={interaction.id} data-interaction-id={interaction.id}><ChatInteractionCard interaction={interaction} runPermission={live ? run?.permission_mode : meta.permission_mode as ChatPermissionMode | undefined} onRespond={(item, response) => actionsRef.current!.respond(item, response)} /></div>)}
    {renderChanges(live ? run?.changes || [] : savedChanges(meta.changes))}
    {typeof meta.error_message === 'string' && meta.error_message ? <div className="chat__error" role="alert">{meta.error_message}</div> : null}
    {typeof meta.status === 'string' ? <div className="chat__result-status" role={live ? 'status' : undefined}>{statusText(meta.status)}{(live ? run?.completion : meta.completion as ChatRun['completion'])?.status === 'verified' ? ' · 交付已验证' : ''}{live && run?.status_message ? ` · ${run.status_message}` : ''}</div> : null}
    {['failed', 'error', 'interrupted', 'cancelled'].includes(String(meta.status)) && (live ? run?.id : typeof meta.run_id === 'string' ? meta.run_id : null) ? <button type="button" className="btn btn--sm" disabled={busy || sending} onClick={() => void actionsRef.current?.send((live ? run?.id : meta.run_id as string) || undefined)}>继续处理</button> : null}
    {Array.isArray(meta.proposal_ids) && meta.proposal_ids.length > 0 && !meta.auto_applied ? <Link className="chat__inbox-link" to="/inbox">查看收件箱中的修改建议</Link> : null}
  </>

  const pendingInteractions = (run?.interactions || messages.flatMap((message) => savedInteractions(message.meta.interactions))).filter((item) => item.status === 'pending')
  const referencePath = selectionContext?.active_file || chapterRel
  const referenceSelection = selectionContext?.selection?.text || selection
  const matchingFiles = availableFiles.filter((path) => path !== referencePath && chatSessionMatches(path, fileSearch))
  const contextPreview = run?.context_preview || [...messages].reverse().find((message) => message.role === 'assistant')?.meta.context_preview as ContextPreview | undefined
  const content = <>
    <div className="chat-workspace-layout" ref={layoutRef} style={{ '--chat-material-w': `${materialWidth}px` } as CSSProperties}>
    <div className={`chat chat-workspace chat-workspace--${presentation}`}>
      <div className="chat__header">
        <div className="chat__heading"><Icon name="chat" />{renamingTitle && current ? <InlineEdit defaultValue={current.title} ariaLabel="会话标题" onCommit={value => void commitRename(value)} onCancel={() => setRenamingTitle(false)} /> : <strong title={current?.title || '新对话'}>{current?.title || '新对话'}</strong>}</div>
        <div className="chat__header-actions"><button className="btn btn--ghost btn--sm" type="button" onClick={() => setHistoryOpen(true)}>历史</button><button className="btn btn--sm" type="button" disabled={sending} onClick={() => selectSession(null)}>新对话</button>
          <ActionMenu label="更多" ariaLabel="会话操作"><button className="btn btn--ghost btn--sm" disabled={!current || configuring} onClick={() => setRenamingTitle(true)}>重命名</button><button className="btn btn--ghost btn--sm" disabled={!current || naming} onClick={() => void autoName()}>AI 重新命名</button><button className="btn btn--ghost btn--sm" disabled={!current || busy} onClick={() => void remove()}>删除会话</button></ActionMenu>
          {presentation === 'page' && wideMaterials && materialsCollapsed ? <button className="btn btn--ghost btn--sm" aria-label="恢复并排材料区" onClick={() => foldMaterials(false)}>显示材料</button> : null}
          {presentation !== 'page' ? <Link className="btn btn--ghost btn--sm" to={`/project/${projectId}/chat`}>展开</Link> : null}
          {onCollapse && presentation === 'overlay' ? <button className="btn btn--ghost btn--sm" type="button" aria-label="关闭写作助手" onClick={onCollapse}>关闭</button> : null}</div>
      </div>
      <div className="chat__configuration">
        <button className="chat__configuration-toggle" type="button" aria-expanded={settingsOpen} aria-controls="chat-configuration-content" onClick={() => { const next = !settingsOpen; setSettingsOpen(next); localStorage.setItem('aiw.chat.settings.open', String(next)) }}>
          <span className="chat__configuration-action">对话设置</span>
          <span title={modelLabel}>{modelLabel}</span>
          <span className="chat__configuration-permission">{run && !isTerminal(run) ? `本轮：${permissionLabel(runPermission)}${runDiscussionOnly ? ' · 仅讨论' : ''}；新设置：${permissionLabel(selectedPermission)}${discussionOnly ? ' · 仅讨论' : ''}` : `新设置：${permissionLabel(selectedPermission)}${discussionOnly ? ' · 仅讨论' : ''}`}</span>
        </button>
        <Drawer title="对话设置" open={settingsOpen} onClose={() => setSettingsOpen(false)} width={600}><div id="chat-configuration-content" className="chat__configuration-content">
      {current?.title_status === 'pending' || current?.title_status === 'running' ? <div className="chat__title-status" role="status">AI 正在为对话命名…</div> : current?.title_status === 'failed' ? <div className="chat__title-status">AI 命名暂未完成<button type="button" className="btn btn--ghost btn--sm" disabled={naming} onClick={() => void autoName()}>重试</button></div> : null}
      <div className="chat__settings">
        <select className="select" aria-label="权限模式" value={selectedPermission} disabled={configuring} onChange={(event) => void updateSettings({ permission_mode: event.target.value as ChatPermissionMode })}>
          {CHAT_PERMISSION_OPTIONS.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
        </select>
        <select className="select" aria-label="模型选择" value={selectedModel} disabled={busy || configuring} onChange={(event) => { const [provider_id = '', model_id = ''] = event.target.value.split('::'); void updateSettings({ provider_id, model_id }) }}>
          <option value="">默认模型</option>{providers.filter((provider) => provider.enabled).map((provider) => <optgroup key={provider.provider_id} label={provider.display_name}>{provider.models.map((model) => <option key={model.id} value={`${provider.provider_id}::${model.id}`}>{model.id}</option>)}</optgroup>)}
        </select>
      </div>
      <div className="chat__permission-note">{CHAT_PERMISSION_OPTIONS.find((option) => option.value === selectedPermission)?.description}{busy ? <strong>本轮实际权限：{permissionLabel(runPermission)}{runDiscussionOnly ? ' · 仅讨论' : ''}；新设置：{permissionLabel(selectedPermission)}{discussionOnly ? ' · 仅讨论' : ''}。切换后无需新开对话：当前任务的下一个工具调用即按新设置判定，任务结束后新轮次继续沿用。</strong> : <strong>切换后无需新开对话：当前任务的下一个工具调用即按新设置判定，任务结束后新轮次继续沿用。</strong>}</div>
      <div className="chat__discussion-row"><label><input type="checkbox" checked={discussionOnly} disabled={configuring} onChange={(event) => void updateSettings({ discussion_only: event.target.checked })} />仅讨论，不修改文件</label><button className="btn btn--ghost btn--sm" type="button" onClick={() => setMemoryOpen(true)}>本书记忆</button></div>
      <details className="chat__advanced"><summary>写作分工</summary><label>处理角色<select className="select" aria-label="Agent 选择" value={selectedAgent} disabled={busy || configuring} onChange={(event) => void updateSettings({ agent: event.target.value, agent_pinned: event.target.value ? 1 : 0 })}><option value="">自动选择</option>{agents.map((agent) => <option key={agent.name} value={agent.name}>{agent.title || agent.name}</option>)}</select></label><small className="muted">角色选择用于下一次发送；当前任务的处理者和目标范围见任务详情。</small></details>
      <Link className="btn btn--sm" to={`/project/${projectId}/settings`} onClick={() => setSettingsOpen(false)}>项目设置 · 本书新对话默认与章节字数</Link>
        </div></Drawer>
      </div>
      {(busy || connectionError) ? <div className="chat__taskbar" role="status"><span>{connectionError ? '连接需要处理' : statusText(run?.status || 'starting')}</span><strong>{sendPhase || steps.filter((step) => step.status === 'running').slice(-1)[0]?.label || run?.status_message}</strong>{connectionError ? <button className="btn btn--sm" onClick={() => setSubscriptionVersion((value) => value + 1)}>重新连接</button> : null}</div> : null}
      {connectionError ? <div className="chat__error" role="alert">{connectionError}</div> : null}
      {pendingInteractions.length ? <button type="button" className="chat__waiting-banner" onClick={() => { const first = pendingInteractions[0]; scrollRef.current?.querySelector(`[data-interaction-id="${CSS.escape(first.id)}"]`)?.scrollIntoView({ block: 'center', behavior: 'smooth' }); followBottomRef.current = false }}>有 {pendingInteractions.length} 项等待你回应 · 定位问题或批准请求 ↓</button> : null}
      <div className="chat__messages" ref={scrollRef} onScroll={() => { const area = scrollRef.current; if (area) { scrollPositionRef.current = area.scrollTop; followBottomRef.current = area.scrollHeight - area.scrollTop - area.clientHeight < 70; if (followBottomRef.current) setNewContent(false) } }} aria-busy={busy}>
        {loading ? <p className="muted">正在载入会话…</p> : messages.length === 0 && !run ? <div className="chat__welcome"><span className="chat__welcome-mark"><Icon name="book" size={24} /></span><h3>把想法写进你的小说</h3><p>先读相关章节和设定，再一起规划、创作或检查。改动会留下差异与保存结果。</p><div className="chat__welcome-examples"><button type="button" onClick={() => { changeInput('阅读当前章节和相关设定，帮我把这一章的开头改得更紧凑。'); inputRef.current?.focus() }}>修改当前章节</button><button type="button" onClick={() => { changeInput('阅读大纲和已有章节，继续写下一章并保存到章节文件。'); inputRef.current?.focus() }}>续写下一章</button><button type="button" onClick={() => { changeInput('检查当前章节的人物表现与设定是否一致，只报告问题。'); inputRef.current?.focus() }}>检查人物与设定</button></div></div> : null}
        {messages.filter((message) => !(run && message.role !== 'user' && String(message.meta?.run_id) === run.id)).map((message) => <ChatHistoryMessage key={message.id} message={message} highlighted={sourceTarget?.sessionId === activeId && sourceTarget?.messageId === message.id} busy={busy} reverting={reverting} writeDisabled={discussionOnly} renderContent={renderResult} />)}
        {run ? <div className="chat__msg chat__msg--assistant"><div className="chat__speaker">写作助手</div>{renderResult(run.text || '', { status: run.status, error_message: run.error_message }, true)}</div> : null}
      </div>
      {newContent ? <button type="button" className="chat__new-content" onClick={() => { followBottomRef.current = true; setNewContent(false); const area = scrollRef.current; if (area) area.scrollTo({ top: area.scrollHeight, behavior: 'smooth' }) }}>有新内容 · 回到最新 ↓</button> : null}
      <div className="chat__composer">
        <div className="chat__target-row"><label htmlFor={`chat-target-${projectId}`}>正文目标</label><select className="select" id={`chat-target-${projectId}`} aria-label="正文目标章节" value={targetChoice === undefined ? '__follow__' : targetChoice || ''} onChange={(event) => changeTarget(event.target.value === '__follow__' ? undefined : event.target.value || null)}><option value="__follow__">跟随当前章节{targetChapterRel ? ` · ${targetChapterRel.split('/').pop()}` : ' · 未指定'}</option><option value="">不指定章节</option>{availableChapters.map((chapter) => <option key={chapter.rel_path} value={chapter.rel_path}>{chapterLabel(chapter)}</option>)}</select><button type="button" className="btn btn--ghost btn--sm" onClick={() => setReferencesOpen(true)}>材料{references.length ? ` ${references.length}` : ''}</button></div>
        <div className="chat__context" aria-label="本轮上下文">
          {referencePath ? <button type="button" title={includeCurrent ? `移除参考文件：${referencePath}` : `附加参考文件：${referencePath}`} onClick={() => { revisionRef.current += 1; setIncludeCurrent(!includeCurrent) }}>{includeCurrent ? `参考：${referencePath.split('/').pop()} ×` : `+ 参考：${referencePath.split('/').pop()}`}</button> : null}
          {referenceSelection?.trim() && includeCurrent ? <button type="button" title={includeSelection ? '移除选区上下文' : '附加选区上下文'} onClick={() => { revisionRef.current += 1; setIncludeSelection(!includeSelection) }}>{includeSelection ? `选区 ${referenceSelection.length} 字 ×` : '+ 选区'}</button> : null}
          {hasUnsavedChanges ? <span className="chat__context-dirty">当前材料有未保存修改</span> : null}{references.slice(0, 3).map((path) => <button key={path} type="button" title={`移除参考：${path}`} onClick={() => { revisionRef.current += 1; setReferences((items) => items.filter((item) => item !== path)) }}>{path.split('/').pop()} ×</button>)}{references.length > 3 ? <button type="button" onClick={() => setReferencesOpen(true)}>另外 {references.length - 3} 项参考</button> : null}</div>
        {quickCards.length ? <details className="chat__quick-menu"><summary>写作指令</summary><div className="quick-cards">{quickCards.map((card) => <button key={card.key} className="quick-card" type="button" onClick={() => { changeInput(input.trim() ? `${input}\n${card.prompt}` : card.prompt); inputRef.current?.focus() }}>{card.label}</button>)}</div></details> : null}
        <textarea ref={inputRef} autoComplete={NO_AUTOFILL} className="textarea chat__input" aria-label="给写作助手的要求" placeholder={busy ? '可先写下一条要求，任务结束后发送…' : '描述想法，或说明要处理的材料…'} value={input} onChange={(event) => changeInput(event.target.value)} onKeyDown={(event) => { if (isChatSendShortcut({ key: event.key, ctrlKey: event.ctrlKey, metaKey: event.metaKey, keyCode: event.keyCode, isComposing: event.nativeEvent.isComposing })) { event.preventDefault(); void send() } }} />
        {inputError ? <div className="chat__error" role="alert">{inputError}{hasRetry ? <button type="button" className="btn btn--sm" disabled={sending} onClick={() => void send(undefined, true)}>重试上次发送</button> : null}<button type="button" className="btn btn--ghost btn--sm" onClick={() => setInputError('')}>关闭</button></div> : null}
        <div className="chat__composer-foot"><span className="muted">{sendPhase || (reconnecting ? '连接恢复中 · 任务仍在运行' : run?.status === 'waiting_input' ? '回应问题后继续' : busy ? '可跨页查看材料，任务继续运行' : 'Ctrl + Enter 发送 · 草稿自动保留')}</span>{run && !isTerminal(run) ? <button className="btn btn--danger btn--sm" type="button" onClick={() => void stop()}>停止</button> : <button className="btn btn--primary btn--sm" type="button" disabled={!input.trim() || sending || loading || sessionsLoading || settingsLoading || defaultsLoading || configuring} onClick={() => void send()}>{sending ? '提交中…' : '发送 ↑'}</button>}</div>
      </div>
    </div>
    {presentation === 'page' && wideMaterials && !materialsCollapsed ? <PanelResizer className="chat-material-resizer" side="right" value={materialWidth} min={260} max={560} label="调整材料预览宽度" onChange={setMaterialWidth} onPreview={next => layoutRef.current?.style.setProperty('--chat-material-w', `${next}px`)} onToggle={() => foldMaterials(true)} /> : null}
    {presentation === 'page' && !materialsCollapsed ? <aside className="chat__materials-pane" aria-label="对话材料与任务依据">
      <header><h2>本轮材料</h2><button className="btn btn--ghost btn--sm" onClick={() => setReferencesOpen(true)}>选择</button><button className="btn btn--ghost btn--sm" aria-label="收起并排材料区" onClick={() => foldMaterials(true)}>收起</button></header>
      <section><h3>正文目标</h3><p>{actualTarget || '未指定章节'}</p><p className="muted">目标独立于参考文件；本轮可写范围由处理角色决定。</p></section>
      <section><h3>作者附加</h3>{referencePath && includeCurrent ? <button type="button" onClick={() => void previewMaterial(referencePath)}>{referencePath}</button> : null}{references.map((path) => <button type="button" key={path} onClick={() => void previewMaterial(path)}>{path}</button>)}{!references.length && !(referencePath && includeCurrent) ? <p className="muted">尚未附加材料，可在发送前选择。</p> : null}{referenceSelection && includeSelection ? <details><summary>选区 · {referenceSelection.length} 字</summary><p>{referenceSelection}</p></details> : null}</section>
      {contextPreview ? <section><h3>本轮使用依据</h3><p className="muted">约 {contextPreview.total_tokens} tokens</p>{contextPreview.items.map((item, index) => <div className="chat__material-source" key={`${item.source}-${index}`}><strong>{item.title}</strong><small>{item.source}</small>{item.truncated ? <small>已按材料预算裁剪</small> : null}</div>)}</section> : null}
      {routing ? <section><h3>当前任务</h3><p>{routing.agent_title || routing.agent}</p><p className="muted">{routing.scope_label || routing.write_targets.join('、') || '本轮只读'}</p></section> : null}
      {run?.changes.length ? <section><h3>实际交付</h3>{renderChanges(run.changes)}</section> : null}
      {materialPreview && inlineMaterials ? <section className="chat__inline-preview"><header><h3>原文预览</h3><button className="btn btn--ghost btn--sm" onClick={closeMaterial}>关闭</button></header>{materialContents}<button className="btn btn--sm" disabled={materialPreview.loading} onClick={openPreviewFile}>在编辑器中打开</button></section> : null}
      {diff && inlineMaterials ? <section className="chat__inline-preview"><header><h3>文件差异</h3><button className="btn btn--ghost btn--sm" onClick={() => setDiff(null)}>关闭</button></header><p>{diff.path}</p>{diffContents}</section> : null}
    </aside> : null}
    </div>
    <Drawer title="本书对话历史" open={historyOpen} onClose={() => setHistoryOpen(false)} width={520}>
      <input className="input" aria-label="搜索对话" placeholder="按标题查找" value={historySearch} onChange={(event) => setHistorySearch(event.target.value)} />
      <div className="chat__history-list">{sessions.filter((item) => chatSessionMatches(item.title, historySearch)).map((item) => <button type="button" key={item.id} className={item.id === activeId ? 'is-active' : ''} disabled={sending} onClick={() => { selectSession(item.id); setHistoryOpen(false) }}><strong>{item.title}</strong><small>{new Date(item.updated_at || item.created_at).toLocaleString()} {item.id === activeId && busy ? '· 处理中' : ''}</small></button>)}</div>
      {!sessions.some((item) => chatSessionMatches(item.title, historySearch)) ? <p className="muted">{sessions.length ? '没有匹配的对话。' : '还没有对话，开始一条新的要求吧。'}</p> : null}
      <button className="btn btn--primary" disabled={sending} onClick={() => { selectSession(null); setHistoryOpen(false) }}>新对话</button>
    </Drawer>
    <Drawer title="本轮参考材料" open={referencesOpen} onClose={() => setReferencesOpen(false)} width={640}>
      <p className="muted">材料提供依据；正文目标和本轮角色仍分别决定任务对象与写入范围。</p>
      {referenceSelection && includeSelection ? <details className="chat__selection-preview"><summary>查看选区 · {referenceSelection.length} 字</summary><pre>{referenceSelection}</pre></details> : null}
      <input className="input" aria-label="搜索参考文件" placeholder="按文件名或路径查找" value={fileSearch} onChange={(event) => setFileSearch(event.target.value)} />
      <p className="muted">已选 {references.length} 项 · 找到 {matchingFiles.length} 项</p>
      <div className="chat__file-picker">{matchingFiles.map((path) => <div key={path}><label><input type="checkbox" checked={references.includes(path)} onChange={(event) => { revisionRef.current += 1; setReferences((items) => event.target.checked ? [...items, path] : items.filter((item) => item !== path)) }} /><span>{path}</span></label><button type="button" className="btn btn--ghost btn--sm" onClick={() => void previewMaterial(path)}>预览</button></div>)}</div>
      {!matchingFiles.length ? <p className="muted">没有匹配的材料。</p> : null}
    </Drawer>
    <Drawer title={`材料预览 · ${materialPreview?.path || ''}`} open={materialPreview !== null && !inlineMaterials} onClose={closeMaterial} width={800}
      footer={materialPreview ? <button className="btn btn--sm" disabled={materialPreview.loading} onClick={openPreviewFile}>在编辑器中打开</button> : undefined}>
      {materialContents}
    </Drawer>
    <Drawer title={`文件差异 · ${diff?.path || ''}`} open={diff !== null && !inlineMaterials} onClose={() => setDiff(null)} width={1000}>
      {diffContents}
    </Drawer>
    <ChatMemoryPanel projectId={projectId} open={memoryOpen} onClose={() => setMemoryOpen(false)} onSource={(sessionId, messageId) => { selectSession(sessionId); setSourceTarget({ sessionId, messageId }) }} />
    {confirmNode}
  </>
  return portalHost === null ? null : portalHost ? createPortal(content, portalHost) : content
}
