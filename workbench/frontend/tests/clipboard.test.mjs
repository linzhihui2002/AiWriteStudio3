import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/clipboard.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } })
const { copyTextToClipboard } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

function environment(result = true, options = {}) {
  const originals = Object.fromEntries(['document', 'HTMLElement', 'HTMLInputElement', 'HTMLTextAreaElement'].map(key => [key, globalThis[key]]))
  const calls = []
  class Element {
    isConnected = true
    style = {}
    closest(selector) { assert.equal(selector, '[role="dialog"], [role="alertdialog"]'); return this.dialog || null }
    appendChild(element) { element.parentElement = this; calls.push(['append', element, this]) }
    setAttribute(key, value) { this[key] = value }
    focus(focusOptions) {
      calls.push(['focus', this, focusOptions])
      doc.activeElement = options.dialogFocusTrap && this === buffer && this.parentElement !== dialog ? active : this
    }
    remove() { this.isConnected = false; calls.push(['remove', this]) }
  }
  class Input extends Element {
    selectionStart = 2
    selectionEnd = 5
    selectionDirection = 'backward'
    setSelectionRange(start, end, direction = 'none') { this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction }
  }
  class Textarea extends Input { select() { this.setSelectionRange(0, this.value.length) } }
  const active = new Textarea()
  const dialog = options.dialogFocusTrap ? new Element() : null
  active.dialog = dialog
  const range = { cloneRange: () => ({ originalRange: true }) }
  const node = { isConnected: true }
  const selection = {
    rangeCount: 1, anchorNode: node, anchorOffset: 7, focusNode: node, focusOffset: 3,
    getRangeAt: () => range,
    removeAllRanges: () => calls.push(['clearRanges']),
    addRange: range => calls.push(['restoreRange', range]),
    setBaseAndExtent: (...values) => calls.push(['restoreDirection', ...values]),
  }
  let buffer
  const doc = {
    activeElement: active,
    getSelection: () => selection,
    createElement: tag => { assert.equal(tag, 'textarea'); buffer = new Textarea(); return buffer },
    body: new Element(),
    execCommand: command => {
      if (options.dialogFocusTrap) assert.equal(doc.activeElement, buffer, 'Copy requires its temporary field to retain dialog focus')
      calls.push(['copy', command, buffer.value, buffer.selectionStart, buffer.selectionEnd]); if (result instanceof Error) throw result; return result
    },
  }
  globalThis.document = doc
  globalThis.HTMLElement = Element
  globalThis.HTMLInputElement = Input
  globalThis.HTMLTextAreaElement = Textarea
  return { doc, active, dialog, calls, get buffer() { return buffer }, restore() { for (const [key, value] of Object.entries(originals)) { if (value === undefined) delete globalThis[key]; else globalThis[key] = value } } }
}

test('copy preserves long Chinese text, newlines and emoji in the synchronous click path', () => {
  const state = environment()
  try {
    const text = '“桥边见。”\n第二行：中文与 🐉。\n'.repeat(10000)
    assert.equal(copyTextToClipboard(text), undefined)
    const copied = state.calls.find(call => call[0] === 'copy')
    assert.deepEqual(copied.slice(1), ['copy', text, 0, text.length])
    assert.equal(state.buffer.isConnected, false)
    assert.equal(state.buffer.readOnly, true)
    assert.equal(state.doc.activeElement, state.active)
    assert.deepEqual([state.active.selectionStart, state.active.selectionEnd, state.active.selectionDirection], [2, 5, 'backward'])
    assert.equal(state.calls.find(call => call[0] === 'restoreRange')[1].originalRange, true)
    const restored = state.calls.find(call => call[0] === 'restoreDirection')
    assert.deepEqual([restored[2], restored[4]], [7, 3])
    assert.ok(state.calls.filter(call => call[0] === 'focus').every(call => call[2].preventScroll))
  } finally { state.restore() }
})

test('denied or throwing copy cleans the temporary field and restores author focus/selection', () => {
  for (const result of [false, new Error('copy denied')]) {
    const state = environment(result)
    try {
      assert.throws(() => copyTextToClipboard('合成验收文本'))
      assert.equal(state.buffer.isConnected, false)
      assert.equal(state.doc.activeElement, state.active)
      assert.deepEqual([state.active.selectionStart, state.active.selectionEnd, state.active.selectionDirection], [2, 5, 'backward'])
    } finally { state.restore() }
  }
})

test('unsupported copy raises a recoverable application error without requesting another API', () => {
  const state = environment()
  try {
    delete state.doc.execCommand
    assert.throws(() => copyTextToClipboard('合成文本'), /手动复制/)
    assert.equal(state.calls.length, 0)
  } finally { state.restore() }
})

test('copy inside a dialog retains temporary-field focus across the overlay focus trap', () => {
  const state = environment(true, { dialogFocusTrap: true })
  try {
    const outside = state.doc.createElement('textarea')
    outside.value = '位于弹窗之外的临时字段'
    state.doc.body.appendChild(outside)
    outside.focus({ preventScroll: true })
    assert.equal(state.doc.activeElement, state.active, 'Focus trap redirects fields appended outside the dialog')
    outside.remove()

    const text = '抽屉内复制：中文与换行。\n末行保留。'.repeat(100)
    copyTextToClipboard(text)
    const copied = state.calls.find(call => call[0] === 'copy')
    assert.deepEqual(copied.slice(1), ['copy', text, 0, text.length])
    assert.equal(state.buffer.parentElement, state.dialog)
    assert.equal(state.buffer.isConnected, false)
    assert.equal(state.doc.activeElement, state.active)
    assert.deepEqual([state.active.selectionStart, state.active.selectionEnd, state.active.selectionDirection], [2, 5, 'backward'])
  } finally { state.restore() }
})
