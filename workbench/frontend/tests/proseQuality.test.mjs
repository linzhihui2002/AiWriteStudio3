import test from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ts from 'typescript'

function moduleUrl(source) {
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020, jsx: ts.JsxEmit.ReactJSX },
  })
  const resolved = outputText.replace(/from (['"])(react(?:\/jsx-runtime)?)\1/g,
    (_all, _quote, name) => `from ${JSON.stringify(import.meta.resolve(name))}`)
  return `data:text/javascript;base64,${Buffer.from(resolved).toString('base64')}`
}

const helperUrl = moduleUrl(await readFile(new URL('../src/lib/proseQuality.ts', import.meta.url), 'utf8'))
const helpers = await import(helperUrl)
const componentSource = (await readFile(new URL('../src/components/ProseQualityReport.tsx', import.meta.url), 'utf8'))
  .replace("'../lib/proseQuality'", JSON.stringify(helperUrl))
const { default: ProseQualityReport } = await import(moduleUrl(componentSource))
const render = props => renderToStaticMarkup(createElement(ProseQualityReport, props))
const finding = overrides => ({
  kind: 'repeated_paragraph', severity: 'warning', line: 3, column: 2, end_line: 4,
  text: '沈砚合上账册，把铜钱推回柜台。',
  related_locations: [{ line: 9, column: 1, end_line: 10 }],
  reason: '两处段落使用相同原文。', suggestion: '检查是否是有意复现，必要时改写其中一处。',
  ...overrides,
})
const report = (findings = [], overrides = {}) => ({ version: 1, blocking: false, findings, counters: {}, truncated: false, ...overrides })

test('legacy review results without diagnostic fields render no expression report', () => {
  assert.equal(render({}), '')
  assert.equal(render({quality:null,modelSuggestions:null}), '')
  assert.equal(helpers.proseAdviceReport(undefined, undefined).available, false)
})

test('warning evidence, source spans and repeated positions stay separate from acceptance', () => {
  const html = render({ quality: report([finding()]) })
  assert.match(html, /表达建议（供作者判断）/)
  assert.match(html, /不影响合同与硬门禁判定/)
  assert.match(html, /第 3—4 行（起始第 2 列）/)
  assert.match(html, /重复出现位置：第 9—10 行/)
  assert.match(html, /沈砚合上账册，把铜钱推回柜台。/)
  assert.match(html, /检查是否是有意复现/)
  assert.doesNotMatch(html, /无需修改|AI 概率|总分|自动去重/)
})

test('model suggestions retain only available evidence and report their source', () => {
  const suggestions = [
    {kind:'over_explanation',evidence:'他担心掌柜不认账。',line:7,column:3,reason:'动作后立即解释心理。',suggestion:'可让动作保留一点含义。'},
    {kind:'dialogue',evidence:'',line:8,column:1,reason:'没有证据的建议'},
  ]
  const normalized = helpers.proseAdviceReport(undefined, suggestions)
  assert.equal(normalized.findings.length, 1)
  assert.equal(normalized.findings[0].source, 'model')
  const html = render({modelSuggestions:suggestions})
  assert.match(html, /审稿表达建议 · 第 7 行，第 3 列/)
  assert.match(html, /解释过多/)
  assert.doesNotMatch(html, /没有证据的建议/)
})

test('truncated diagnostics and stale candidates are explicitly marked without certifying completeness', () => {
  const html = render({quality:report([finding({text_truncated:true})], {truncated:true}),stale:true})
  assert.match(html, /报告已截断，只展示部分发现/)
  assert.match(html, /原文片段已截短/)
  assert.match(html, /以下建议对应修改前的候选/)
  assert.doesNotMatch(html, /无需修改/)
})

test('malformed optional data cannot become blocking errors or crash old histories', () => {
  const invalidReport = report([finding({severity:'error'})])
  assert.equal(helpers.proseAdviceReport(invalidReport, {}).findings.length, 0)
  assert.equal(helpers.proseAdviceReport(report([finding()], {blocking:true}), []).available, false)
  const normalized = helpers.proseAdviceReport(report([finding({
    line:-1,column:'bad',end_line:1,related_locations:[null,{line:5,column:-2,end_line:4},'bad'],
  })]), [])
  assert.equal(normalized.findings[0].location, null)
  assert.deepEqual(normalized.findings[0].related, [{line:5,column:1,end_line:5}])
  assert.doesNotThrow(() => render({quality:report([null,'bad',{},finding({related_locations:null})])}))
})

test('evidence renders as text so source HTML cannot become UI controls', () => {
  const html = render({quality:report([finding({text:'<script>unsafe</script><button>apply</button>'})])})
  assert.match(html, /&lt;script&gt;unsafe&lt;\/script&gt;/)
  assert.doesNotMatch(html, /<script>|<button>/)
})

test('an empty diagnostic report says no suggestions were listed, without declaring no changes needed', () => {
  const html = render({quality:report()})
  assert.match(html, /本次未列出表达建议/)
  assert.doesNotMatch(html, /无需修改|没有问题/)
})

const historySource = overrides => ({
  rel_path: '章节/第0002章.txt', line: 6, column: 2, end_line: 7, end_column: 18,
  content_hash: 'a'.repeat(64), text: '沈砚合上账册，把铜钱推回柜台。', text_truncated: false,
  ...overrides,
})
const historyFinding = overrides => ({
  ...finding(), kind: 'cross_chapter_paragraph', end_column: 15,
  related_sources: [historySource()], reason: '本候选段落与此前章节完全相同。',
  ...overrides,
})
const historyReport = (findings = [], overrides = {}) => ({
  ...report(findings), status: 'available', sources: [{rel_path:'章节/第0002章.txt',content_hash:'a'.repeat(64)}],
  reference_hash: 'b'.repeat(64), excluded: [], degradation: [], ...overrides,
})

test('cross-chapter evidence renders current span, earlier source, exact text and content hash as read-only data', () => {
  const html = render({historyQuality:historyReport([historyFinding()])})
  assert.match(html, /跨章段落重复/)
  assert.match(html, /跨章文本诊断 · 本候选 · 第 3—4 行（起始第 2 列，结束第 15 列）/)
  assert.match(html, /此前来源（仅供核对）/)
  assert.match(html, /章节\/第0002章.txt/)
  assert.match(html, /第 6—7 行（起始第 2 列，结束第 18 列）/)
  assert.match(html, /来源原文：/)
  assert.match(html, /来源文本摘要（SHA-256）/)
  assert.match(html, new RegExp('a'.repeat(64)))
  assert.match(html, /不影响合同与硬门禁判定/)
  assert.match(html, /检查范围不代表全书/)
  assert.doesNotMatch(html, /<button|<a |删除|自动去重|无需修改|没有问题/)
})

test('cross-chapter sentence, missing source text and Chinese model kinds remain readable', () => {
  const html = render({historyQuality:historyReport([historyFinding({
    kind:'cross_chapter_sentence', related_sources:[historySource({text:undefined,line:6,end_line:6,end_column:18})],
  })]), modelSuggestions:[{kind:'场景推进/无效回顾',evidence:'沈砚又想起昨日的争执。',line:8,column:1,
    reason:'这句回顾没有改变当下行动。',suggestion:'可检查回顾是否推进场景。'}]})
  assert.match(html, /跨章句子重复/)
  assert.match(html, /第 6 行，第 2—18 列/)
  assert.match(html, /来源原文未提供，请按正文位置核对/)
  assert.match(html, /场景推进\/无效回顾/)
})

test('unchecked, absent, empty and unavailable history explain coverage without certifying the whole book', () => {
  const unchecked = render({historyQuality:historyReport([historyFinding()],{status:'not_checked',sources:[]})})
  assert.match(unchecked, /本次未检查跨章重复/)
  assert.doesNotMatch(unchecked, /跨章段落重复/)
  assert.match(render({quality:report()}), /本次结果未包含跨章重复检查报告/)
  assert.equal(render({historyQuality:undefined}), '')
  assert.match(render({historyQuality:historyReport([],{status:'empty',sources:[]})}), /没有可核对的此前章节正文/)
  assert.match(render({historyQuality:historyReport([],{status:'degraded',sources:[]})}), /历史正文暂不可用，未完成跨章核对/)
  for (const status of ['available','empty','degraded','not_checked']) {
    assert.doesNotMatch(render({historyQuality:historyReport([],{status})}), /全书无重复|全书没有问题|无需修改/)
  }
})

test('partial history exposes omitted sources and truncation, and edited candidates invalidate its positions', () => {
  const html = render({historyQuality:historyReport([historyFinding({related_sources:[historySource({text_truncated:true})]})],{
    status:'degraded',truncated:true,excluded:[{rel_path:'章节/第0001章.txt',reason:'来源读取失败'}],
    degradation:['历史章节读取上限已到。'],
  }),stale:true})
  assert.match(html, /跨章检查范围不完整/)
  assert.match(html, /报告已截断，只展示部分发现/)
  assert.match(html, /来源片段已截短/)
  assert.match(html, /未纳入的历史来源（1）/)
  assert.match(html, /章节\/第0001章.txt：来源读取失败/)
  assert.match(html, /历史章节读取上限已到/)
  assert.match(html, /以下建议对应修改前的候选/)
})

test('malformed history sources are discarded and source paths and text cannot create controls', () => {
  const malformed = historyReport([historyFinding({related_sources:[null,{},historySource({content_hash:''}),historySource({line:0})]})])
  assert.equal(helpers.proseAdviceReport(undefined,undefined,malformed).findings.length,0)
  assert.equal(helpers.proseAdviceReport(undefined,undefined,historyReport([historyFinding()],{blocking:true})).available,false)
  assert.doesNotThrow(() => render({historyQuality:historyReport([null,{},historyFinding({related_sources:null})],{
    sources:[null,{}],excluded:[null,{}],degradation:[null,{}],
  })}))
  const html = render({historyQuality:historyReport([historyFinding({related_sources:[historySource({
    rel_path:'<button>章节</button>',text:'<script>bad</script>',content_hash:'<a>hash</a>',
  })]})])})
  assert.match(html,/&lt;button&gt;章节/)
  assert.match(html,/&lt;script&gt;bad/)
  assert.doesNotMatch(html,/<button|<script|<a>/)
})
