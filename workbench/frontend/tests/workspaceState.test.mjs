import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/workspaceState.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } })
const { ResourceRequests, resourceSnapshotForScope, resourcePresentation } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('a late request cannot replace a newer query or another book', () => {
  const requests = new ResourceRequests()
  const oldQuery = requests.begin('book-a:hero')
  const newQuery = requests.begin('book-a:city')
  assert.equal(requests.accepts(oldQuery, 'book-a:city'), false)
  assert.equal(requests.accepts(newQuery, 'book-a:city'), true)
  assert.equal(requests.accepts(newQuery, 'book-b:city'), false)
  const returnedBook = requests.begin('book-a:city')
  assert.equal(requests.accepts(newQuery, 'book-a:city'), false)
  assert.equal(requests.accepts(returnedBook, 'book-a:city'), true)
})

test('unmount invalidation refuses a still pending response', () => {
  const requests = new ResourceRequests()
  const pending = requests.begin('book-a')
  requests.invalidate()
  assert.equal(requests.accepts(pending, 'book-a'), false)
})

test('a scope change does not show the previous book error or loaded status', () => {
  const failed = { loading: false, error: '旧书失败', loaded: true, scope: 'book-a' }
  assert.deepEqual(resourceSnapshotForScope(failed, 'book-b'), { loading: true, error: '', loaded: false, scope: 'book-b' })
  assert.deepEqual(resourceSnapshotForScope(failed, 'book-a'), failed)
})

test('first failure never becomes an empty state and a failed refresh preserves content', () => {
  assert.equal(resourcePresentation({loading:true,hasData:false,loaded:false}), 'loading')
  assert.equal(resourcePresentation({loading:false,error:'离线',hasData:false,loaded:false}), 'error')
  assert.equal(resourcePresentation({loading:false,error:'离线',hasData:true,loaded:true}), 'stale')
  assert.equal(resourcePresentation({loading:true,hasData:true,loaded:true}), 'refreshing')
  assert.equal(resourcePresentation({loading:false,hasData:false,loaded:true}), 'content')
  assert.equal(resourcePresentation({loading:true,hasData:false,loaded:true}), 'refreshing')
  assert.equal(resourcePresentation({loading:false,error:'离线',hasData:false,loaded:true}), 'stale')
})
