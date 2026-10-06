import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

async function importHelpers(path) {
  const source = await readFile(new URL(path, import.meta.url), 'utf8')
  const { outputText } = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX,
  } })
  // Exercise exported layout policies without instantiating browser-only components.
  const executable = outputText.replace(/^import[^\n]*\n/gm, '')
  return import(`data:text/javascript;base64,${Buffer.from(executable).toString('base64')}`)
}
const { knowledgeNavigationOverlay } = await importHelpers('../src/components/AppShell.tsx')
const { knowledgeLabelSizing } = await importHelpers('../src/components/KnowledgeGraph.tsx')

test('knowledge navigation borrows canvas width only below the agreed 920px threshold', () => {
  for (const width of [560, 900, 919]) assert.equal(knowledgeNavigationOverlay('/project/10/knowledge', width), true)
  for (const width of [920, 1366, 1920]) assert.equal(knowledgeNavigationOverlay('/project/10/knowledge', width), false)
  assert.equal(knowledgeNavigationOverlay('/project/10/knowledge/', 560), true)
})

test('temporary navigation policy does not change the layout of other project or global routes', () => {
  for (const path of ['/project/10/editor', '/project/10/cards', '/settings', '/', '/project/10/knowledge-extra']) {
    assert.equal(knowledgeNavigationOverlay(path, 560), false)
  }
})

test('small local graph labels target 14 screen pixels while dense panoramas retain their bounded type size', () => {
  const local = knowledgeLabelSizing(12, 0.65)
  assert.ok(Math.abs(local.fontSize * 0.65 - 14) < 0.01)
  assert.ok(local.maxWidth * 0.65 >= 149)
  assert.equal(knowledgeLabelSizing(20, 0.12).fontSize, 40)
  assert.equal(knowledgeLabelSizing(42, 0.12).fontSize, 30)
  assert.ok(Math.abs(knowledgeLabelSizing(42, 0.65).fontSize * 0.65 - 10) < 0.01)
})
