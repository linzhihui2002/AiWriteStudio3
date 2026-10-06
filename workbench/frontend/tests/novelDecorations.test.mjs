import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source = await readFile(new URL('../src/lib/novelDecorations.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } })
const { buildNovelDecorations, replaceSelectedText } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)
const colors = { light: '#335577', dark: '#99bbdd', paper: '#224466' }
const entity = (id, name, aliases = [], mode = 'auto') => ({ entity_id: id, name, aliases, highlight_mode: mode, highlight_colors: colors })

test('names use longest exact matches and the same color for explicit aliases', () => {
  const text = '林玄与林玄真人见到老林。'
  const result = buildNovelDecorations(text, { entities: [entity('a', '林玄', ['老林']), entity('b', '林玄真人')] })
  assert.deepEqual(result.marks.map(mark => [text.slice(mark.from, mark.to), mark.entityId]), [['林玄', 'a'], ['林玄真人', 'b'], ['老林', 'a']])
})
test('ambiguous aliases, disabled colors and substrings of western words are not guessed', () => {
  const text = '先生和林玄遇见AB、ABC、XAB、AB_1。'
  const result = buildNovelDecorations(text, { entities: [entity('a', '甲', ['先生']), entity('b', '乙', ['先生']), entity('c', '林玄', [], 'off'), entity('d', 'AB')] })
  assert.deepEqual(result.marks.map(mark => text.slice(mark.from, mark.to)), ['AB'])
})
test('dialogue marks coexist with names and unclosed quotes do not bleed into another paragraph', () => {
  const text = '“林玄来了。”\n“没有右引号\n普通叙述。「你好」『再见』'
  const result = buildNovelDecorations(text, { entities: [entity('a', '林玄')] })
  assert.deepEqual(result.marks.filter(mark => mark.kind === 'dialogue').map(mark => text.slice(mark.from, mark.to)), ['“林玄来了。”', '「你好」', '『再见』'])
  assert.equal(result.marks.filter(mark => mark.kind === 'name').length, 1)
})
test('milestones occupy paragraph ends using the latest whitespace-free UTF-16 stage', () => {
  const text = '甲\r\n乙 😀 丙丁'
  const { milestones } = buildNovelDecorations(text, { milestone: { enabled: true, step: 3 } })
  assert.deepEqual(milestones.map(item => item.count), [6])
  assert.deepEqual(milestones.map(item => item.at), [text.length])
  for (const item of milestones) assert.ok(!/[\uDC00-\uDFFF]/.test(text[item.at] || ''))
  assert.equal(buildNovelDecorations(text, { milestone: { enabled: false, step: 3 } }).milestones.length, 0)
})

test('stage positions are paragraph ends, remain easy to locate and respond to interval changes', () => {
  const text = '甲'.repeat(490) + '\n' + '乙'.repeat(30) + '\n\n' + '丙'.repeat(510) + '\n丁'
  const nodes = buildNovelDecorations(text, { milestone: { enabled: true, step: 500 } }).milestones
  assert.deepEqual(nodes, [{ at: 521, count: 500 }, { at: 1033, count: 1000 }])
  for (const node of nodes) assert.equal(text[node.at], '\n')
  const changed = buildNovelDecorations(text, { milestone: { enabled: true, step: 1000 } }).milestones
  assert.deepEqual(changed, [{ at: 1033, count: 1000 }])
  const long = buildNovelDecorations('甲'.repeat(1700), { milestone: { enabled: true, step: 500 } }).milestones
  assert.deepEqual(long, [{ at: 1700, count: 1500 }])
})
test('precise selection replacement affects the selected occurrence and rejects stale ranges', () => {
  const text = '同一句。中间。同一句。'
  const start = text.lastIndexOf('同一句。')
  assert.equal(replaceSelectedText(text, { start, end: text.length }, '同一句。', '新句。'), '同一句。中间。新句。')
  assert.equal(replaceSelectedText(text, { start, end: text.length }, '旧句。', '新句。'), null)
})
