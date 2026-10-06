import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { EditorState, Transaction } from '@codemirror/state'
import { history, historyField, undo, undoDepth } from '@codemirror/commands'
import ts from 'typescript'

function moduleUrl(source) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  })
  const resolved = outputText.replace(/from (['"])(react(?:\/jsx-runtime)?|@codemirror\/(?:state|view|commands))\1/g,
    (_all, _quote, name) => `from ${JSON.stringify(import.meta.resolve(name))}`)
  return `data:text/javascript;base64,${Buffer.from(resolved).toString('base64')}`
}

// Exercise the actual adapter's state extensions without a DOM/browser dependency.
const source = (await readFile(new URL('../src/components/ChapterEditor.tsx', import.meta.url), 'utf8'))
  .replace("import '../styles/chapterEditor.css'", '')
const { ranges, decorationField, setDecorations } = await import(moduleUrl(
  `${source}\nexport { ranges, decorationField, setDecorations }`))
const { buildNovelDecorations } = await import(moduleUrl(
  await readFile(new URL('../src/lib/novelDecorations.ts', import.meta.url), 'utf8')))
const colors = { light: '#335577', dark: '#99bbdd', paper: '#224466' }
const entities = [{ entity_id: 'character:林玄', name: '林玄', aliases: [],
  highlight_mode: 'auto', highlight_colors: colors }]
const decorate = (text) => buildNovelDecorations(text, { entities, milestone: { enabled: true, step: 4 } })
const extensions = () => [history(), decorationField]
const applyDecorations = (state) => state.update({ effects:
  setDecorations.of(ranges(decorate(state.doc.toString()), state.doc.length)) })
const collectedRanges = (state) => {
  const values = []
  state.field(decorationField).between(0, state.doc.length,
    (from, to, value) => values.push({ from, to, class: value.spec.class, widget: value.spec.widget }))
  return values
}

test('novel visual decoration does not alter document, selection, changed ranges or undo history', () => {
  const text = '“林玄来了。”\n甲乙丙丁'
  let state = EditorState.create({ doc: text, selection: { anchor: 1, head: 3 }, extensions: extensions() })
  const transaction = applyDecorations(state)
  assert.equal(transaction.docChanged, false)
  assert.equal(transaction.changes.empty, true)
  state = transaction.state
  assert.equal(state.doc.toString(), text)
  assert.equal(state.sliceDoc(state.selection.main.from, state.selection.main.to), '林玄')
  assert.equal(undoDepth(state), 0)
  const values = collectedRanges(state)
  assert.ok(values.some(value => value.class === 'novel-name'))
  assert.ok(values.some(value => value.class === 'novel-dialogue'))
  assert.ok(values.some(value => value.widget?.count === 4))
  assert.doesNotMatch(state.doc.toString(), /已满|novel-|<span|\*\*/)
})

test('composition-period edits map existing decorations to unchanged source words without forcing reparse', () => {
  let state = EditorState.create({ doc: '“林玄来了。”', extensions: extensions() })
  state = applyDecorations(state).state
  state = state.update({ changes: { from: 0, insert: '先说：' },
    annotations: Transaction.userEvent.of('input.type.compose') }).state
  const name = collectedRanges(state).find(value => value.class === 'novel-name')
  assert.equal(state.sliceDoc(name.from, name.to), '林玄')
  assert.equal(name.from, 4)
  assert.equal(state.doc.toString(), '先说：“林玄来了。”')
  assert.equal(undoDepth(state), 1)
})

test('restyling after a user edit adds no undo step and undo restores exactly the typed text', () => {
  const before = '“林玄来了。”'
  let state = EditorState.create({ doc: before, extensions: extensions() })
  state = applyDecorations(state).state
  state = state.update({ changes: { from: before.length, insert: '他坐下。' },
    annotations: Transaction.userEvent.of('input') }).state
  assert.equal(undoDepth(state), 1)
  state = applyDecorations(state).state
  state = applyDecorations(state).state
  assert.equal(undoDepth(state), 1)
  assert.equal(undo({ state, dispatch: transaction => { state = transaction.state } }), true)
  assert.equal(state.doc.toString(), before)
  assert.equal(undoDepth(state), 0)
})

test('serialized current-file history keeps both exact selected occurrence and undo across mode remounts', () => {
  const text = '同一句。中间。同一句。'
  const from = text.lastIndexOf('同一句。')
  let state = EditorState.create({ doc: text, selection: { anchor: from, head: text.length }, extensions: extensions() })
  state = state.update({ changes: { from, to: text.length, insert: '新句。' },
    selection: { anchor: from, head: from + 3 }, annotations: Transaction.userEvent.of('input') }).state
  state = applyDecorations(state).state
  const snapshot = JSON.parse(JSON.stringify(state.toJSON({ history: historyField })))
  let restored = EditorState.fromJSON(snapshot, { extensions: extensions() }, { history: historyField })
  restored = applyDecorations(restored).state
  assert.equal(restored.doc.toString(), '同一句。中间。新句。')
  assert.equal(restored.sliceDoc(restored.selection.main.from, restored.selection.main.to), '新句。')
  assert.equal(undoDepth(restored), 1)
  assert.equal(undo({ state: restored, dispatch: transaction => { restored = transaction.state } }), true)
  assert.equal(restored.doc.toString(), text)
  assert.equal(restored.sliceDoc(restored.selection.main.from, restored.selection.main.to), '同一句。')
})

test('fresh adopted external content has no path back to the discarded edit in undo history', () => {
  let oldState = EditorState.create({ doc: '作者旧稿。', extensions: extensions() })
  oldState = oldState.update({ changes: { from: 0, insert: '本地未保存：' },
    annotations: Transaction.userEvent.of('input') }).state
  assert.equal(undoDepth(oldState), 1)
  let adopted = EditorState.create({ doc: '明确采用的外部稿。', extensions: extensions() })
  adopted = applyDecorations(adopted).state
  assert.equal(undoDepth(adopted), 0)
  assert.equal(undo({ state: adopted, dispatch: transaction => { adopted = transaction.state } }), false)
  assert.equal(adopted.doc.toString(), '明确采用的外部稿。')
})
