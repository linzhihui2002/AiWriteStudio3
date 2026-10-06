import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/editorDraftState.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } })
let generation = 0
async function fresh() {
  return import(`data:text/javascript;base64,${Buffer.from(outputText + `\n// reload ${generation++}`).toString('base64')}`)
}
const values = new Map()
globalThis.window = { localStorage: { getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) } }
const path = '章节/第0001章.txt'
const base = { rel_path: path, body: '磁盘正文', content: '磁盘正文', hash: 'original', mtime: 1 }
const draft = (projectId, text, file = path) => ({ projectId, path: file, text, base: { ...base, rel_path: file } })

test('drafts and last files survive reload and remain isolated by book and file', async () => {
  const state = await fresh()
  state.storeEditorDraft(draft(901, '甲书未保存'))
  state.storeEditorDraft(draft(902, '乙书未保存'))
  state.storeEditorDraft(draft(901, '另一份材料', '设定/人物.md'))
  state.rememberEditorFile(901, path)
  state.rememberEditorFile(902, '设定/人物.md')
  const reload = await fresh()
  assert.equal(reload.readEditorDraft(901, path).text, '甲书未保存')
  assert.equal(reload.readEditorDraft(902, path).text, '乙书未保存')
  assert.equal(reload.readEditorDraft(901, '设定/人物.md').text, '另一份材料')
  assert.equal(reload.lastEditorFile(901), path)
  assert.equal(reload.lastEditorFile(902), '设定/人物.md')
  assert.equal(reload.readEditorDraft(903, path), null)
})

test('late save rebases newer input without clearing other drafts', async () => {
  const state = await fresh()
  state.storeEditorDraft(draft(904, '发送后继续输入'))
  state.storeEditorDraft(draft(905, '别的书'))
  const saved = { ...base, body: '发送正文', content: '发送正文', hash: 'saved', mtime: 2 }
  state.acknowledgeEditorSave(904, path, '发送正文', saved)
  assert.equal(state.readEditorDraft(904, path).text, '发送后继续输入')
  assert.equal(state.readEditorDraft(904, path).base.hash, 'saved')
  const reload = await fresh()
  assert.equal(reload.readEditorDraft(904, path).base.hash, 'saved')
  reload.acknowledgeEditorSave(904, path, '发送后继续输入', saved)
  assert.equal(reload.readEditorDraft(904, path), null)
  assert.equal(reload.readEditorDraft(905, path).text, '别的书')
})

test('corrupt or mismatched persisted identity cannot become a draft', async () => {
  const key = `aiw.editor.draft.906.${encodeURIComponent(path)}`
  for (const raw of ['{', JSON.stringify(draft(907, '跨书')), JSON.stringify({ ...draft(906, '跨文件'), path: '设定/人物.md' }), JSON.stringify({ ...draft(906, '无版本'), base: { ...base, hash: null } })]) {
    values.set(key, raw)
    assert.equal((await fresh()).readEditorDraft(906, path), null)
  }
  const state = await fresh()
  state.storeEditorDraft({ ...draft(906, '来源不匹配'), base: { ...base, rel_path: '设定/人物.md' } })
  assert.equal(state.readEditorDraft(906, path), null)
})

test('storage failure preserves route-level drafts in memory', async () => {
  const previous = window.localStorage
  window.localStorage = { getItem() { throw Error('unavailable') }, setItem() { throw Error('full') }, removeItem() { throw Error('unavailable') } }
  try {
    const state = await fresh()
    state.storeEditorDraft(draft(908, '存储失败时的输入'))
    state.rememberEditorFile(908, path)
    assert.equal(state.readEditorDraft(908, path).text, '存储失败时的输入')
    assert.equal(state.lastEditorFile(908), path)
    state.clearEditorDraft(908, path)
    assert.equal(state.readEditorDraft(908, path), null)
  } finally { window.localStorage = previous }
})
