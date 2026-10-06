import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

const moduleUrl = source => {
  const { outputText } = ts.transpileModule(source, {compilerOptions: {module:ts.ModuleKind.ESNext,target:ts.ScriptTarget.ES2020,jsx:ts.JsxEmit.ReactJSX}})
  const resolved = outputText.replace(/from (['"])(react(?:\/jsx-runtime)?)\1/g, (_all,_quote,name) => `from ${JSON.stringify(import.meta.resolve(name))}`)
  return `data:text/javascript;base64,${Buffer.from(resolved).toString('base64')}`
}
const helper = moduleUrl(await readFile(new URL('../src/lib/reviewWorkspace.ts', import.meta.url),'utf8'))
const {reportVersion,softReviewMessage,rejectedSuggestionCount} = await import(helper)
const source = (await readFile(new URL('../src/components/ReviewOperationReport.tsx', import.meta.url),'utf8')).replace("'../lib/reviewWorkspace'",JSON.stringify(helper))
const {SoftDeslopReport,IngestionReport} = await import(moduleUrl(source))
const base = {suggestions:[],proposal_ids:[],style_comparison:null,ai_used:true,ai_error:'',candidate_hash:'a',source_changed:false,rejected_suggestions:[]}

test('failure, empty success, rejected advice and stale source remain distinct',() => {
  assert.match(softReviewMessage({...base,ai_used:false}),/未完成/)
  assert.match(softReviewMessage(base),/已完成.*未提出/)
  assert.match(softReviewMessage({...base,rejected_suggestions:[{reason:'来源不唯一'}]}),/核验/)
  assert.match(softReviewMessage({...base,source_changed:true}),/旧版本/)
  assert.equal(rejectedSuggestionCount({...base,rejected_suggestions:[{count:3},{count:-1},{count:NaN}]}),5)
})
test('missing hashes do not claim that old report is current',() => {
  assert.equal(reportVersion('a','a'),'current')
  assert.equal(reportVersion('b','a'),'stale')
  assert.equal(reportVersion(undefined,'a'),'unknown')
  assert.equal(reportVersion('a','a',true),'stale')
})
test('soft review renders exact evidence, replacement, rejected reasons and disables stale location',() => {
  const html = renderToStaticMarkup(createElement(SoftDeslopReport,{result:{...base,suggestions:[{line:7,issue:'解释重复',original:'原文证据',replacement:'建议写法内容'}],proposal_ids:[12],rejected_suggestions:[{reason:'原文不唯一'}]},stale:true,openFile:()=>{},openInbox:()=>{}}))
  for(const phrase of ['原文证据','建议写法内容','第 7 行','收件箱','原文不唯一','旧版本']) assert.ok(html.includes(phrase),phrase)
  assert.match(html,/disabled=""/)
})
test('four artifacts explain purpose and replay without raw JSON output',() => {
  const result = {summary:'本章摘要原文',timeline_added:0,ledger_added:0,foreshadow_added:0,state_changes_detected:0,ai_used:false,replayed:true,proposals_created:[],counts:{timeline:{added:0,updated:0,skipped:3,rejected:0}}}
  const html = renderToStaticMarkup(createElement(IngestionReport,{result,stale:false,openFile:()=>{},openInbox:()=>{}}))
  for(const phrase of ['角色状态','时间线','资源账本','伏笔台账','没有重复追加','本章摘要原文','沿用 3']) assert.ok(html.includes(phrase),phrase)
  assert.ok(!html.includes('timeline_added'))
})
