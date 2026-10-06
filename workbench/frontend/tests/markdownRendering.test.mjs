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
  const resolved = outputText.replace(/from (['"])(react(?:\/jsx-runtime)?|react-markdown|remark-gfm)\1/g,
    (_all, _quote, name) => `from ${JSON.stringify(import.meta.resolve(name))}`)
  return `data:text/javascript;base64,${Buffer.from(resolved).toString('base64')}`
}

const helpersSource = await readFile(new URL('../src/lib/markdownRendering.ts', import.meta.url), 'utf8')
const helperModule = moduleUrl(helpersSource)
const decorationsModule = moduleUrl(await readFile(new URL('../src/lib/novelDecorations.ts', import.meta.url), 'utf8'))
const viewSource = (await readFile(new URL('../src/components/MarkdownView.tsx', import.meta.url), 'utf8'))
  .replace("import '../styles/markdown.css'", '')
  .replace("'../lib/markdownRendering'", JSON.stringify(helperModule))
  .replace("'../lib/novelDecorations'", JSON.stringify(decorationsModule))
const { default: MarkdownView } = await import(moduleUrl(viewSource))
const render = (text, props = {}) => renderToStaticMarkup(createElement(MarkdownView, { text, ...props }))

test('GFM tables have shared semantic columns, alignment, escaped pipes, empty cells and safe line breaks', () => {
  const html = render('| 字段 | 值 | 来源 |\n| :--- | ---: | :---: |\n| 名称 | **林玄**\\|化名<br />第二行 | author |\n| 空值 | | model |',
    { document: true })
  assert.match(html, /<table><thead><tr><th style="text-align:left">字段<\/th>/)
  assert.match(html, /<tbody><tr><td style="text-align:left">名称<\/td>/)
  assert.match(html, /<strong>林玄<\/strong>\|化名<br\/>\s*第二行/)
  assert.match(html, /<td style="text-align:right"><\/td>/)
  assert.equal((html.match(/<tr>/g) ?? []).length, 3)
  assert.doesNotMatch(html, /prose-table-row/)
})

test('headings, ordered/nested lists, task lists, inline and fenced code keep standard Markdown semantics', () => {
  const html = render('# 一级\n\n## 二级\n\n1. 第一项\n   - 子项\n2. 第二项\n\n- [x] 已完成\n\n```txt\n# 原始代码\n| a | b |\n```\n\n`author` 与 ~~删除~~')
  assert.match(html, /<h1>一级<\/h1>/)
  assert.match(html, /<h2>二级<\/h2>/)
  assert.match(html, /<ol>/)
  assert.match(html, /<ul>[\s\S]*子项/)
  assert.match(html, /type="checkbox" disabled="" checked=""/)
  assert.match(html, /<pre><code class="language-txt"># 原始代码/)
  assert.match(html, /<code>author<\/code>/)
  assert.match(html, /<del>删除<\/del>/)
})

test('raw HTML, metadata comments and unsafe URLs are inert; only attribute-free br becomes a break', () => {
  const html = render('正文<br>下一行<br onclick="alert(1)">末尾\n\n<script>alert(1)</script>\n\n<!-- wb-knowledge secret -->\n\n[安全](https://example.com) [危险](javascript:alert%281%29)')
  assert.match(html, /正文<br\/>\s*下一行末尾/)
  assert.doesNotMatch(html, /<script|onclick|secret|javascript:/)
  assert.match(html, /target="_blank" rel="noopener noreferrer"/)
})

test('source labels require explicit creative-document opt-in and preserve their meaning and explanations', () => {
  const text = '来源：**author**（本轮拍板）。\n\nmodel 建议：保留疑问。\n\n状态：unknown 待定（T-10）。\n\n| 内容 | 来源 |\n| --- | --- |\n| A | model |\n| B | unknown |'
  const html = render(text, { document: true, sourceLabels: true })
  assert.match(html, /data-source="author"[^>]*>作者提供<\/span>/)
  assert.match(html, /data-source="model"[^>]*>模型整理<\/span>/)
  assert.match(html, /data-source="unknown"[^>]*>待确认<\/span>/)
  assert.match(html, /不自动表示已确认/)
  assert.match(html, /（T-10）/)
  assert.doesNotMatch(html, /待定（T-10）/)
  assert.doesNotMatch(render(text, { document: true }), /markdown-source/)
  assert.doesNotMatch(render(text, { sourceLabels: true }), /markdown-source/)
})

test('ordinary English, code, quotations and link text are never rewritten as provenance', () => {
  const html = render('The author meets an unknown model.\n\n`来源：author`\n\n> 来源：model\n\n[来源：author](https://example.com)',
    { document: true, sourceLabels: true })
  assert.doesNotMatch(html, /markdown-source/)
  assert.match(html, /The author meets an unknown model/)
  assert.match(html, /<code>来源：author<\/code>/)
  assert.match(html, /<blockquote>[\s\S]*来源：model/)
  assert.match(html, />来源：author<\/a>/)
})

test('literal inline provenance codes become labels only inside source fields or placeholder fields', () => {
  const html = render('来源：`author`。\n\n现实肉身状态：`unknown` 待定（T-2）。\n\n`unknown`\n\n`model` 提示一个类型名。\n\n| 内容 | 来源 |\n| --- | --- |\n| 建议 | `model` |\n\n```txt\n来源：author\n状态：unknown\n```',
    { document: true, sourceLabels: true })
  assert.match(html, /data-source="author"[^>]*>作者提供<\/span>/)
  assert.match(html, /现实肉身状态：<span[^>]+data-source="unknown"[^>]*>待确认<\/span>（T-2）/)
  assert.match(html, /data-source="model"[^>]*>模型整理<\/span>/)
  assert.match(html, /<p><code>unknown<\/code><\/p>/)
  assert.match(html, /<code>model<\/code> 提示一个类型名/)
  assert.match(html, /<pre><code class="language-txt">来源：author\n状态：unknown/)
})

test('two-column field tables recognize source rows without translating ordinary technical values', () => {
  const text = '| 项目 | 内容 |\n| --- | --- |\n| 来源 | `author` |\n| source | model |\n| 出处 | author |\n| 信息来源 | `unknown` |\n| 技术类型 | `unknown` |\n| 变量 | model |'
  const html = render(text, { document: true, sourceLabels: true })
  assert.equal((html.match(/data-source="author"/g) ?? []).length, 2)
  assert.equal((html.match(/data-source="model"/g) ?? []).length, 1)
  assert.equal((html.match(/data-source="unknown"/g) ?? []).length, 1)
  assert.match(html, /<td>来源<\/td><td><span[^>]+data-source="author"[^>]*>作者提供<\/span><\/td>/)
  assert.match(html, /<td>技术类型<\/td><td><code>unknown<\/code><\/td>/)
  assert.match(html, /<td>变量<\/td><td>model<\/td>/)
  assert.doesNotMatch(render(text, { document: true }), /markdown-source/)
})

test('plain chapters keep literal Markdown and CRLF offsets while applying overlapping names and dialogue', () => {
  const text = '# 甲\r\n“林玄！” **原文**'
  const from = text.indexOf('林玄')
  const html = render(text, { plain: true, decorations: {
    marks: [
      { from, to: from + 2, kind: 'name', entityId: 'character:林玄', name: '林玄',
        colors: { light: '#123456', dark: '#abcdef', paper: '#234567' } },
      { from: from - 1, to: from + 4, kind: 'dialogue' },
    ], milestones: [{ at: from + 2, count: 5 }],
  } })
  assert.match(html, /<p># 甲<\/p>/)
  assert.match(html, /<em class="novel-reading__dialogue"><span class="novel-reading__name"/)
  assert.match(html, /data-entity-id="character:林玄">林玄<\/span>/)
  assert.match(html, /--novel-name-dark:#abcdef/)
  assert.match(html, /aria-hidden="true" data-count="5" data-label="已满 5 字"><\/span>/)
  assert.match(html, /\*\*原文\*\*/)
  assert.doesNotMatch(html, /<h1>|<strong>/)
  // Milestone text comes from CSS, keeping it out of copied content/accessible text.
  assert.doesNotMatch(html, />已满 5 字</)
  const visibleText = html.replace(/<[^>]+>/g, '')
  assert.equal(visibleText, text.replace('\r\n', ''))
})

test('the old milestone props remain supported without adding anything to the chapter text', () => {
  const html = render('甲 乙\n丙丁', { plain: true, milestone: { enabled: true, step: 2, decorate: true, budget: 100 } })
  assert.equal((html.match(/data-count=/g) ?? []).length, 2)
  assert.match(html, /data-label="已满 2 字"/)
  assert.match(html, /data-label="已满 4 字"/)
  assert.equal(html.replace(/<[^>]+>/g, ''), '甲 乙丙丁')
  assert.doesNotMatch(render('甲乙', { plain: true, milestone: { enabled: true, step: 1, decorate: false, budget: 0 } }), /data-count=/)
})

test('empty documents use the provided hint', () => {
  assert.equal(render(' \n', { emptyHint: '没有材料' }), '<p class="muted">没有材料</p>')
})
