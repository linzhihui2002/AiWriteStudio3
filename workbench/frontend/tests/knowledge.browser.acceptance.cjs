/* Manual browser acceptance against knowledge_acceptance_server, using temporary books.
 * WORKBENCH_PLAYWRIGHT may point at an installed Playwright package; no runtime app dependency.
 */
const { chromium } = require(process.env.WORKBENCH_PLAYWRIGHT || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const origin = 'http://127.0.0.1:8790';
const output = path.resolve('.workbench', process.env.WORKBENCH_ACCEPTANCE_RUN || 'knowledge-acceptance', 'browser');
fs.mkdirSync(output, { recursive: true });
const report = { started: new Date().toISOString(), checks: [], screenshots: [], errors: [] };
let page, browser;
const check = (name, evidence) => report.checks.push({ name, evidence });
const screenshot = async (name) => {
  const destination = path.join(output, name + '.png');
  await page.screenshot({ path: destination, fullPage: false });
  report.screenshots.push(destination);
};
const settle = async () => page.waitForFunction(() =>
  document.querySelector('.knowledge-graph__canvas') && !document.querySelector('.knowledge-graph-status'));
const openFile = async (name) => {
  await page.getByRole('button', { name: new RegExp('^' + name + ' '), includeHidden: true }).first().waitFor({ state: 'attached' });
  if (!await page.getByRole('button', { name: new RegExp('^' + name + ' '), includeHidden: true }).first().isVisible()) {
    await page.getByRole('button', { name: '资料与实体', exact: true }).filter({ visible: true }).click();
  }
  await page.getByRole('button', { name: new RegExp('^' + name + ' ') }).first().click();
  await settle();
};
const api = async (url) => (await page.request.get(origin + url)).json();
const cyState = async () => page.locator('.knowledge-graph__canvas').evaluate((element) => {
  const cy = element._cyreg.cy;
  return { zoom: cy.zoom(), pan: cy.pan(), nodes: cy.nodes().map((node) => ({
    id: node.id(), label: node.data('record').label, position: node.position(), rendered: node.renderedPosition(),
  })) };
});

(async () => {
  browser = await chromium.launch({ headless: true, channel: 'chrome' });
  page = await browser.newPage({ viewport: { width: 1920, height: 1080 } });
  page.setDefaultTimeout(15000);
  page.on('pageerror', (error) => report.errors.push(String(error)));
  await page.goto(origin + '/project/1/knowledge');
  await openFile('人物设定');
  const initial = await cyState();
  assert.equal(initial.nodes.length, 20);
  assert.equal(await page.getByRole('button', { name: '图例', exact: true }).getAttribute('aria-expanded'), 'false');
  assert.equal(await page.locator('.knowledge-search__form').count(), 0);
  check('material selection defaults to local graph and detail', { nodes: initial.nodes.length });
  await screenshot('graph-1920-light');

  const hero = initial.nodes.find((node) => node.label === '沈砚');
  const box = await page.locator('.knowledge-graph__canvas').boundingBox();
  await page.mouse.move(box.x + hero.rendered.x, box.y + hero.rendered.y);
  await page.mouse.down();
  await page.mouse.move(box.x + hero.rendered.x + 65, box.y + hero.rendered.y - 35, { steps: 12 });
  await page.mouse.up();
  await page.mouse.wheel(0, -70);
  const dragged = await cyState();
  assert.notDeepEqual(dragged.nodes.find((node) => node.id === hero.id).position, hero.position);
  await page.getByRole('combobox', { name: '选择图谱节点', exact: true }).selectOption(hero.id);
  await page.getByRole('button', { name: '聚焦一跳关系', exact: true }).click();
  await settle();
  assert.match(await page.locator('.knowledge-scope').innerText(), /关联节点局部视图/);
  await page.getByRole('button', { name: '返回聚焦前范围', exact: true }).click();
  await settle();
  const restored = await cyState();
  assert.deepEqual(restored.nodes.find((node) => node.id === hero.id).position,
    dragged.nodes.find((node) => node.id === hero.id).position);
  assert.ok(Math.abs(restored.zoom - dragged.zoom) < 0.001);
  check('mouse drag, wheel zoom and return restore material viewport', { zoom: restored.zoom });

  // The failing scope retains and labels the last successful canvas, then returns.
  await page.route('**/api/projects/1/knowledge/graph?*', async (route) => {
    if (new URL(route.request().url()).searchParams.get('document') === '设定/物品设定.md') {
      await route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '浏览验收模拟断线' }) });
    } else await route.continue();
  });
  await page.getByRole('button', { name: /^物品设定 / }).click();
  await page.getByRole('button', { name: '返回上一成功范围', exact: true }).waitFor();
  assert.match(await page.locator('.knowledge-scope').innerText(), /人物设定.*上一成功范围/);
  assert.equal(await page.getByText('正在读取相关节点…', { exact: true }).count(), 0);
  await screenshot('scope-failure');
  await page.unroute('**/api/projects/1/knowledge/graph?*');
  await page.getByRole('button', { name: '返回上一成功范围', exact: true }).click();
  await settle();
  await openFile('物品设定');
  await openFile('人物设定');
  check('failed scope stops loading, labels retained data and recovers', true);

  // Original source positioning is performed through the actual editor route.
  await page.getByRole('combobox', { name: '选择图谱节点', exact: true }).selectOption(hero.id);
  await page.locator('.knowledge-evidence').first().getByRole('button', { name: '打开原文', exact: true }).click();
  await page.waitForURL('**/editor?*');
  await page.waitForFunction(() => Array.from(document.querySelectorAll('textarea')).some((area) =>
    area.value.includes('沈砚') && area.selectionEnd > area.selectionStart));
  const selected = await page.locator('textarea').evaluateAll((areas) => areas.map((area) =>
    area.value.substring(area.selectionStart, area.selectionEnd)).filter(Boolean));
  assert.ok(selected.some((text) => text.includes('沈砚')));
  assert.ok(selected.every((text) => !text.includes('阿迟')), 'definition opens only the selected entity');
  check('source hash and exact editor selection', { url: page.url(), selected });
  await page.goto(origin + '/project/1/knowledge');
  await openFile('人物设定');

  // UI analysis includes drafts, refuses fabricated evidence, and writes grouped diffs.
  await page.getByRole('tab', { name: /^待审/ }).click();
  await page.getByRole('button', { name: '分析资料', exact: true }).click();
  const startedResponse = page.waitForResponse((response) => response.url().endsWith('/knowledge/extract') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '开始分析全部资料', exact: true }).click();
  const startedJob = await (await startedResponse).json();
  let completedJob;
  for (let i = 0; i < 60; i++) {
    completedJob = await api('/api/projects/1/knowledge/jobs/' + startedJob.id);
    if (!['queued', 'running'].includes(completedJob.status)) break;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  assert.equal(completedJob.status, 'completed');
  await page.waitForFunction((count) => document.querySelector('.knowledge-review__job strong')?.textContent.includes(`分析完成 · ${count}/${count} 份资料`), completedJob.files_total);
  const pending = await api('/api/projects/1/knowledge/candidates?status=pending,conflict');
  assert.equal(pending.candidates.length, 4);
  assert.ok(pending.candidates.every((candidate) => candidate.evidence.quote && !candidate.evidence.quote.includes('八十岁')));
  for (const article of await page.locator('.knowledge-review__candidate').all()) {
    for (const label of [/确认关联实体/, /^关系对象/]) {
      const selects = article.locator('label').filter({ hasText: label }).locator('select');
      if (await selects.count()) {
        const value = await selects.first().evaluate((select) => Array.from(select.options).find((option) =>
          option.value && option.text.includes('设定/人物设定.md'))?.value || select.options[1]?.value);
        await selects.first().selectOption(value);
      }
    }
  }
  await screenshot('review-grounded-candidates');
  await page.getByLabel('选择待审项', { exact: true }).check();
  await page.getByRole('button', { name: '预览所选（4）', exact: true }).click();
  await page.locator('.knowledge-review__proposal').first().waitFor();
  await page.getByText('正在读取本书候选…', { exact: true }).waitFor({ state: 'hidden' });
  assert.equal(await page.locator('.knowledge-review__proposal').count(), 2);
  await page.locator('.knowledge-review__proposal').first().scrollIntoViewIfNeeded();
  await screenshot('review-grouped-diff');
  const targets = await page.locator('.knowledge-review__proposal .muted').allTextContents();
  for (let i = 0; i < 2; i++) {
    const committedResponse = page.waitForResponse((response) => /\/api\/proposals\/\d+\/apply$/.test(response.url()) && response.request().method() === 'POST');
    await page.getByRole('button', { name: '确认写入原资料', exact: true }).first().click();
    const committed = await committedResponse;
    assert.equal(committed.status(), 200);
    const receipt = await committed.json();
    assert.notEqual(receipt.knowledge?.status, 'reconciliation_pending');
    assert.ok(receipt.knowledge?.applied > 0);
    await page.waitForFunction((maximum) => document.querySelectorAll('.knowledge-review__proposal').length <= maximum, 1 - i);
  }
  const applied = await api('/api/projects/1/knowledge/candidates?status=applied');
  assert.equal(applied.candidates.length, 4);
  for (const query of ['巡桥人', '许澄']) {
    const result = await page.request.post(origin + '/api/projects/1/retrieval/search', { data: { query, mode: 'keyword', profile: 'history' } });
    assert.ok((await result.json()).hits.every((hit) => !hit.text.includes(query)), 'history contains no written plan or draft fact');
    const planned = await page.request.post(origin + '/api/projects/1/retrieval/search', { data: { query, mode: 'keyword', profile: 'planning' } });
    assert.ok((await planned.json()).hits.some((hit) => hit.text.includes(query)), 'planning can read the exact written record');
  }
  check('four grounded candidates become two grouped file proposals and applied receipts; plans remain planning only', { targets });

  // Reset the page for linked keyword and streaming Q&A.
  await page.goto(origin + '/project/1/knowledge');
  await openFile('人物设定');
  await page.getByRole('tab', { name: '检索', exact: true }).click();
  await page.getByRole('tab', { name: '关键词检索', exact: true }).click();
  await page.getByRole('textbox', { name: '本书知识检索问题', exact: true }).fill('沈砚');
  await page.locator('.knowledge-search__form').getByRole('button').last().click();
  await page.locator('.knowledge-search__results .knowledge-evidence').first().waitFor();
  check('keyword query returns original evidence in selected material', true);

  await page.getByRole('tab', { name: 'AI 检索', exact: true }).click();
  await page.getByRole('textbox', { name: '本书知识检索问题', exact: true }).fill('沈砚');
  await page.locator('.knowledge-search__form').getByRole('button', { name: '检索', exact: true }).click();
  await page.locator('.knowledge-answer').getByText('可核对本书资料中的记录').waitFor();
  await page.locator('.knowledge-citation').first().click();
  await page.getByRole('button', { name: '停止回答', exact: true }).waitFor({ state: 'hidden' });
  check('streaming Q&A renders grounded answer and clickable citation', true);
  await page.getByRole('textbox', { name: '本书知识检索问题', exact: true }).fill('模型不可用 沈砚');
  await page.locator('.knowledge-search__form').getByRole('button', { name: '检索', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '问答模型不可用' }).waitFor();
  assert.ok(await page.locator('.knowledge-search__results .knowledge-evidence').count() > 0);
  assert.equal(await page.locator('.knowledge-answer').count(), 0);
  check('unavailable answer model retains evidence and stops loading', true);

  // Delayed old-book and old-material responses must never relabel the new scope.
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto(origin + '/project/1/knowledge');
  await openFile('人物设定');
  let releaseOldScope;
  const delayed = new Promise((resolve) => { releaseOldScope = resolve; });
  await page.route('**/api/projects/1/knowledge/graph?*', async (route) => {
    if (new URL(route.request().url()).searchParams.get('document') === '设定/物品设定.md') {
      const response = await route.fetch(); await delayed;
      await route.fulfill({ response }).catch(() => {});
    } else await route.continue();
  });
  await page.getByRole('button', { name: /^物品设定 / }).click();
  await openFile('人物设定');
  releaseOldScope();
  await page.unroute('**/api/projects/1/knowledge/graph?*');
  assert.match(await page.locator('.knowledge-scope').innerText(), /人物设定/);
  await page.goto(origin + '/project/2/knowledge');
  await openFile('人物设定');
  const secondBook = await cyState();
  assert.ok(secondBook.nodes.some((node) => node.label === '莫衡'));
  assert.ok(secondBook.nodes.every((node) => !['沈砚', '许澄', '阿迟'].includes(node.label)));
  check('rapid material changes and switching books exclude old content', { secondBookNodes: secondBook.nodes.length });

  await page.goto(origin + '/project/1/knowledge');
  await openFile('人物设定');
  await page.getByRole('tab', { name: /^待审/ }).click();
  await page.getByRole('button', { name: '分析资料', exact: true }).click();
  await page.getByLabel('字段', { exact: true }).uncheck();
  await page.getByLabel('关系', { exact: true }).uncheck();
  const cancellingResponse = page.waitForResponse((response) => response.url().endsWith('/knowledge/extract') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '开始分析全部资料', exact: true }).click();
  const cancellingJob = await (await cancellingResponse).json();
  await page.getByRole('button', { name: '停止分析', exact: true }).click();
  await page.locator('.knowledge-review__job strong').getByText('已停止', { exact: false }).waitFor();
  const cancelled = await api('/api/projects/1/knowledge/jobs/' + cancellingJob.id);
  assert.equal(cancelled.status, 'cancelled');
  assert.equal((await api('/api/projects/1/knowledge/candidates?status=applied')).candidates.length, 4);
  check('cancelling a specific analysis task keeps verified applied records and real progress', { completed: cancelled.chunks_completed, total: cancelled.chunks_total });

  // Every requested width and theme gets a fresh browser viewport, avoiding cached theme drift.
  for (const width of [1920, 1366, 900, 560]) for (const [theme, label] of [['light', '亮'], ['dark', '暗'], ['paper', '纸']]) {
    await page.setViewportSize({ width, height: width < 920 ? 1000 : 1080 });
    await page.goto(origin + '/project/1/knowledge');
    await page.getByRole('button', { name: label, exact: true }).click();
    await openFile('人物设定');
    const layout = await page.locator('.knowledge-center').boundingBox();
    const row = await page.getByRole('button', { name: /^人物设定 /, includeHidden: true }).boundingBox();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
    assert.equal(overflow, false, 'horizontal overflow at ' + width + '/' + theme);
    if (width >= 920) assert.ok(layout.width >= 600);
    if (width === 560) assert.ok(layout.width >= 500, 'narrow knowledge canvas uses full window');
    if (width === 900) assert.ok(layout.width >= 840, 'global navigation does not consume narrow graph space');
    if (width < 920) assert.equal(await page.getByRole('button', { name: '图谱', exact: true }).getAttribute('aria-pressed'), 'true');
    await screenshot(`graph-${width}-${theme}`);
    if (width === 560 && theme === 'light') {
      const savedSidebarPreference = await page.evaluate(() => localStorage.getItem('aiw.shell.sidebarCollapsed'));
      await page.getByRole('button', { name: '全局导航', exact: true }).click();
      await page.locator('.app-sidebar').waitFor({ state: 'visible' });
      assert.equal(await page.locator('.app-main').evaluate((element) => element.inert), true);
      await screenshot('navigation-560-drawer');
      await page.keyboard.press('Escape');
      await page.locator('.app-sidebar').waitFor({ state: 'hidden' });
      assert.equal(await page.locator('.app-main').evaluate((element) => element.inert), false);
      assert.equal(await page.evaluate(() => localStorage.getItem('aiw.shell.sidebarCollapsed')), savedSidebarPreference);
      check('narrow navigation drawer closes with Escape and preserves wide-screen preference', true);
    }
    const scopeNodes = await cyState();
    await page.getByRole('combobox', { name: '选择图谱节点', exact: true }).selectOption(scopeNodes.nodes.find((node) => node.label === '沈砚').id);
    await page.locator('.knowledge-selection h2').getByText('沈砚', { exact: true }).waitFor();
    await page.locator('.knowledge-selection .knowledge-evidence').first().waitFor();
    if (theme === 'light') await screenshot(`detail-${width}-${theme}`);
    const close = page.locator('.knowledge-search').getByRole('button', { name: '关闭', exact: true }).filter({ visible: true });
    if (await close.count()) await close.click();
    if (width < 920) await page.getByRole('button', { name: '图谱', exact: true }).click();
    await page.getByRole('button', { name: '返回本书全景', exact: true }).click();
    await settle();
    assert.match(await page.locator('.knowledge-scope').innerText(), /本书全景/);
    check('responsive graph ' + width + '/' + theme, { canvasWidth: layout.width, materialRowHeight: row?.height || 'in drawer' });
  }

  // Same-source writeback reads the verified journal before-image, not a changed live file.
  await page.setViewportSize({ width: 1920, height: 1080 });
  await page.goto(origin + '/project/3/knowledge');
  await openFile('人物设定');
  await page.getByRole('tab', { name: /^待审/ }).click();
  await page.getByRole('button', { name: '审阅写入差异', exact: true }).click();
  await page.locator('.knowledge-review__proposal').first().waitFor();
  const archivedCommit = page.waitForResponse((response) => /\/api\/proposals\/\d+\/apply$/.test(response.url()) && response.request().method() === 'POST');
  await page.getByRole('button', { name: '确认写入原资料', exact: true }).click();
  assert.equal((await archivedCommit).status(), 200);
  await page.locator('select').filter({ has: page.locator('option[value="applied"]') }).selectOption('applied');
  await page.locator('.knowledge-review__candidate blockquote button').first().click();
  const archiveDialog = page.getByRole('dialog', { name: '审核时的原文依据', exact: true });
  await archiveDialog.waitFor();
  assert.match(await archiveDialog.innerText(), /沈砚的年龄更新为二十一岁/);
  assert.match(await archiveDialog.innerText(), /原文快照/);
  await screenshot('review-original-source-archive');
  await archiveDialog.getByRole('button', { name: '打开当前资料', exact: true }).click();
  await page.waitForURL('**/editor?*');
  assert.equal(new URL(page.url()).searchParams.has('hash'), false);
  assert.equal(new URL(page.url()).searchParams.has('line'), false);
  check('same-source writeback preserves verified original snapshot; current editor avoids obsolete positions', true);
  assert.deepEqual(report.errors, []);
  report.status = 'passed';
})().catch(async (error) => {
  report.status = 'failed'; report.failure = String(error.stack || error);
  if (page) { report.lastText = (await page.locator('main').innerText().catch(() => '')).slice(-6000); await screenshot('failure').catch(() => {}); }
  process.exitCode = 1;
}).finally(async () => {
  report.finished = new Date().toISOString();
  fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify(report, null, 2));
  console.log(JSON.stringify({ status: report.status, checks: report.checks.length, failure: report.failure, result: path.join(output, 'result.json') }));
  if (browser) await browser.close();
});
