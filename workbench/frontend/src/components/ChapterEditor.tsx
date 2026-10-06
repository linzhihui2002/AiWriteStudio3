import { forwardRef, useEffect, useImperativeHandle, useRef } from 'react'
import { EditorState, StateEffect, StateField, Transaction, type Extension } from '@codemirror/state'
import { Decoration, EditorView, WidgetType, keymap, lineNumbers, type DecorationSet } from '@codemirror/view'
import { defaultKeymap, history, historyField, historyKeymap } from '@codemirror/commands'
import type { NovelDecorations } from '../lib/novelDecorations'
import '../styles/chapterEditor.css'

export interface ChapterEditorHandle {
  focus(): void
  getSelection(): { start: number; end: number; text: string }
  selectAndReveal(start: number, end: number): void
  replaceRange(start: number, end: number, replacement: string, expected?: string): boolean
  appendText(replacement: string, expectedDocument: string): boolean
}
export interface ChapterEditorSnapshot { json: ReturnType<EditorState['toJSON']>; scrollTop: number; bodyScrollTop?: number }
interface Props {
  value: string
  documentKey: string
  decorations: NovelDecorations
  historyStore: Map<string, ChapterEditorSnapshot>
  onChange(value: string): void
  onSelect(selection: { start: number; end: number; text: string }): void
  onCompositionChange(composing: boolean): void
  onAcceptGhost(): boolean
  onRejectGhost(): boolean
}

const setDecorations = StateEffect.define<DecorationSet>()
const decorationField = StateField.define<DecorationSet>({
  create: () => Decoration.none,
  update(value, transaction) {
    let next = value.map(transaction.changes)
    for (const effect of transaction.effects) if (effect.is(setDecorations)) next = effect.value
    return next
  },
  provide: field => EditorView.decorations.from(field),
})
class MilestoneWidget extends WidgetType {
  constructor(readonly count: number) { super() }
  eq(other: MilestoneWidget) { return this.count === other.count }
  toDOM() {
    const element = document.createElement('span')
    element.className = 'novel-milestone'
    element.dataset.label = `已满 ${this.count} 字`
    element.setAttribute('aria-hidden', 'true')
    element.contentEditable = 'false'
    return element
  }
}
function ranges(decorations: NovelDecorations, length: number) {
  const marks = decorations.marks.filter(mark => mark.from >= 0 && mark.to <= length && mark.to > mark.from).map(mark => {
    const attributes: Record<string, string> = {}
    if (mark.kind === 'name' && mark.colors) {
      attributes.style = `--novel-name-light:${mark.colors.light};--novel-name-dark:${mark.colors.dark};--novel-name-paper:${mark.colors.paper}`
      attributes['data-entity-id'] = mark.entityId || ''
      attributes.title = mark.name || ''
    }
    return Decoration.mark({ class: mark.kind === 'dialogue' ? 'novel-dialogue' : 'novel-name', attributes }).range(mark.from, mark.to)
  })
  const widgets = decorations.milestones.filter(item => item.at >= 0 && item.at <= length).map(item =>
    Decoration.widget({ widget: new MilestoneWidget(item.count), side: 1, block: true }).range(item.at))
  return Decoration.set([...marks, ...widgets], true)
}

/** The document is plain text; names and milestones are never inserted into it. */
const ChapterEditor = forwardRef<ChapterEditorHandle, Props>(function ChapterEditor(props, ref) {
  const host = useRef<HTMLDivElement>(null)
  const editor = useRef<EditorView | null>(null)
  const latest = useRef(props)
  latest.current = props
  const composing = useRef(false)
  const currentExtensions = useRef<Extension[]>([])
  useImperativeHandle(ref, () => ({
    focus: () => editor.current?.focus(),
    getSelection() {
      const view = editor.current
      if (!view) return { start: 0, end: 0, text: '' }
      const { from: start, to: end } = view.state.selection.main
      return { start, end, text: view.state.sliceDoc(start, end) }
    },
    selectAndReveal(start, end) {
      const view = editor.current
      if (!view) return
      const length = view.state.doc.length
      const from = Math.max(0, Math.min(start, length))
      const to = Math.max(from, Math.min(end, length))
      view.dispatch({ selection: { anchor: from, head: to }, effects: EditorView.scrollIntoView(from, { y: 'center' }) })
      view.focus()
    },
    replaceRange(start, end, replacement, expected) {
      const view = editor.current
      if (!view || composing.current || start < 0 || end < start || end > view.state.doc.length) return false
      if (expected !== undefined && view.state.sliceDoc(start, end) !== expected) return false
      view.dispatch({ changes: { from: start, to: end, insert: replacement }, selection: { anchor: start + replacement.length }, annotations: Transaction.userEvent.of('input') })
      view.focus()
      return true
    },
    appendText(replacement, expectedDocument) {
      const view = editor.current
      if (!view || composing.current || view.composing || view.state.doc.toString() !== expectedDocument) return false
      const end = view.state.doc.length
      view.dispatch({ changes: { from: end, insert: replacement }, selection: { anchor: end + replacement.length },
        scrollIntoView: true, annotations: Transaction.userEvent.of('input') })
      view.focus()
      return true
    },
  }), [])

  useEffect(() => {
    if (!host.current) return
    const store = latest.current.historyStore
    const key = latest.current.documentKey
    const extensions: Extension[] = [
      lineNumbers(), EditorView.lineWrapping, history(), decorationField,
      EditorView.contentAttributes.of({ 'aria-label': '章节正文', spellcheck: 'false', autocorrect: 'off', autocomplete: 'off', autocapitalize: 'off' }),
      keymap.of([
        { key: 'Tab', run: view => !view.composing && !composing.current && latest.current.onAcceptGhost() },
        { key: 'Escape', run: view => !view.composing && !composing.current && latest.current.onRejectGhost() },
        ...defaultKeymap, ...historyKeymap,
      ]),
      EditorView.domEventHandlers({
        compositionstart() { composing.current = true; latest.current.onCompositionChange(true) },
        compositionend(_event, view) {
          window.setTimeout(() => {
            if (editor.current !== view) return
            composing.current = false
            latest.current.onCompositionChange(false)
            view.dispatch({ effects: setDecorations.of(ranges(latest.current.decorations, view.state.doc.length)) })
          }, 0)
        },
      }),
      EditorView.updateListener.of(update => {
        if (update.docChanged) latest.current.onChange(update.state.doc.toString())
        if (update.docChanged || update.selectionSet) {
          const { from: start, to: end } = update.state.selection.main
          latest.current.onSelect({ start, end, text: update.state.sliceDoc(start, end) })
        }
      }),
    ]
    currentExtensions.current = extensions
    const cached = store.get(key)
    const state = cached && cached.json.doc === latest.current.value
      ? EditorState.fromJSON(cached.json, { extensions }, { history: historyField })
      : EditorState.create({ doc: latest.current.value, extensions })
    const view = new EditorView({ state, parent: host.current })
    editor.current = view
    view.dispatch({ effects: setDecorations.of(ranges(latest.current.decorations, state.doc.length)) })
    if (cached) view.scrollDOM.scrollTop = cached.scrollTop
    const body = host.current.closest('.editor__body')
    if (cached?.bodyScrollTop !== undefined && body instanceof HTMLElement) body.scrollTop = cached.bodyScrollTop
    return () => {
      store.set(key, { json: view.state.toJSON({ history: historyField }), scrollTop: view.scrollDOM.scrollTop,
        bodyScrollTop: body instanceof HTMLElement ? body.scrollTop : 0 })
      composing.current = false
      latest.current.onCompositionChange(false)
      editor.current = null
      view.destroy()
    }
  }, [props.documentKey])

  useEffect(() => {
    const view = editor.current
    if (!view || composing.current || view.composing) return
    if (view.state.doc.toString() !== props.value) {
      // External file adoption is not an author edit and must not retain stale undo history.
      const selection = Math.min(view.state.selection.main.head, props.value.length)
      view.setState(EditorState.create({ doc: props.value, selection: { anchor: selection }, extensions: currentExtensions.current }))
    }
    view.dispatch({ effects: setDecorations.of(ranges(props.decorations, view.state.doc.length)) })
  }, [props.value, props.decorations])
  return <div className="chapter-editor" ref={host} />
})
export default ChapterEditor
