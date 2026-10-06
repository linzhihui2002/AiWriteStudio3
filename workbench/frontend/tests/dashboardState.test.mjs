import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/lib/dashboardState.ts', import.meta.url), 'utf8')
const { outputText } = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } })
const { buildDailyUsage, getChartCeiling, formatTaskType, formatTaskStatus } = await import(`data:text/javascript;base64,${Buffer.from(outputText).toString('base64')}`)

test('sparse usage preserves the first included day and fills exactly seven calendar days', () => {
  const oldest = { day: '2026-09-27', tokens: 4006, prompt: 3000, completion: 1006, cost: 0.8, calls: 3 }
  const latest = { day: '2026-10-02', tokens: 3359, prompt: 2000, completion: 1359, cost: 0.6, calls: 4 }
  const result = buildDailyUsage([latest, oldest], 7, new Date('2026-10-03T00:00:00Z'))
  assert.deepEqual(result.map(row => row.day), ['2026-09-27', '2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01', '2026-10-02', '2026-10-03'])
  assert.deepEqual(result[0], oldest)
  assert.deepEqual(result[5], latest)
  assert.deepEqual(result[6], { day: '2026-10-03', tokens: 0, prompt: 0, completion: 0, cost: 0, calls: 0 })
  assert.equal(result.reduce((sum, row) => sum + row.tokens, 0), 7365)
})

test('empty ranges and a one-day window retain zero usage without invented calls', () => {
  const end = new Date('2026-10-03T00:00:00Z')
  for (const days of [1, 7, 30, 90]) {
    const result = buildDailyUsage([], days, end)
    assert.equal(result.length, days)
    assert.equal(result.at(-1).day, '2026-10-03')
    assert.equal(result.reduce((sum, row) => sum + row.calls, 0), 0)
  }
})

test('calendar filling crosses leap days and the DST transition using the supplied UTC endpoint', () => {
  assert.deepEqual(buildDailyUsage([], 3, new Date('2024-03-01T00:00:00Z')).map(row => row.day), ['2024-02-28', '2024-02-29', '2024-03-01'])
  assert.deepEqual(buildDailyUsage([], 3, new Date('2026-03-09T00:00:00Z')).map(row => row.day), ['2026-03-07', '2026-03-08', '2026-03-09'])
})

test('chart ceilings stay readable and never place a real maximum above the scale', () => {
  for (const [value, expected] of [[0, 1], [1, 1], [2, 2], [3, 5], [5, 5], [7, 10], [11, 20], [50, 50], [51, 100], [4006, 5000], [5001, 10000], [10000, 10000]]) {
    assert.equal(getChartCeiling(value), expected)
    assert.ok(getChartCeiling(value) >= value)
  }
})

test('known task records have Chinese labels and unknown values remain inspectable', () => {
  assert.equal(formatTaskType('embedding'), '知识向量化')
  assert.equal(formatTaskType('knowledge_answer'), '知识库问答')
  assert.equal(formatTaskType('Agent 生成'), '智能体生成')
  assert.equal(formatTaskType('章节正文'), '章节正文')
  assert.equal(formatTaskType('custom-task'), 'custom-task')
  assert.equal(formatTaskType(null), '未分类')
  assert.equal(formatTaskStatus('running'), '进行中')
  assert.equal(formatTaskStatus('done'), '已完成')
  assert.equal(formatTaskStatus('completed'), '已完成')
  assert.equal(formatTaskStatus('failed'), '失败')
  assert.equal(formatTaskStatus('cancelled'), '已取消')
  assert.equal(formatTaskStatus('custom-status'), 'custom-status')
})
