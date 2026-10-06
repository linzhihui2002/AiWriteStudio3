import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/lib/scopedAction.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } })
const { ScopedActions } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('double submission is locked before React renders and failures allow a retry', () => {
  const actions = new ScopedActions('book-a:chapter-1')
  const first = actions.begin('book-a:chapter-1', 'save')
  assert.ok(first)
  assert.equal(actions.begin('book-a:chapter-1', 'save'), null)
  assert.equal(actions.finish(first), true)
  assert.ok(actions.begin('book-a:chapter-1', 'save'))
})

test('late success and finally from another book cannot close or unlock a newer operation', () => {
  const actions = new ScopedActions('a')
  const old = actions.begin('a', 'generate')
  actions.setScope('b')
  const current = actions.begin('b', 'save')
  assert.equal(actions.accepts(old), false)
  assert.equal(actions.finish(old), false)
  assert.equal(actions.accepts(current), true)
  assert.equal(actions.begin('b', 'save'), null)
  actions.finish(current)
  actions.setScope('a')
  assert.equal(actions.accepts(old), false)
  assert.equal(actions.begin('b', 'old-closure'), null)
})

test('changing only the result display does not invalidate a chapter operation', () => {
  const actions = new ScopedActions('book:chapter')
  const token = actions.begin('book:chapter', 'deslop')
  actions.setScope('book:chapter')
  assert.equal(actions.accepts(token), true)
  actions.invalidate()
  assert.equal(actions.accepts(token), false)
})
