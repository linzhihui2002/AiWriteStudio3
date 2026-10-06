/* Chromium verification using production dist and in-memory API fixtures.
 * Every network request is fulfilled or aborted. This script starts no server,
 * opens no listener and never reaches a running workbench or model provider.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { chromium } = require(process.env.WORKBENCH_PLAYWRIGHT || 'C:/Users/30332/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const { createFixtureState, installFixtures, chapterPath } = require('./workbench.browser.fixtures.cjs');

const root = path.resolve(__dirname, '../../..');
const dist = path.resolve(root, process.env.ACCEPTANCE_DIST || process.env.WORKBENCH_ACCEPTANCE_DIST || 'workbench/frontend/dist');
const output = path.join(root, '.workbench/logs/prose-optimization/second-round-browser');
const origin = 'http://127.0.0.1:8790';
const evidence = '林舟合上账册，把那枚铜钱推回柜台。';
const report = {
  started_at: new Date().toISOString(), origin,
  production_assets: dist,
  isolation: 'Production files fulfilled in browser routes; all APIs synthetic; unknown requests aborted; no listener or backend connection.',
  checks: [], screenshots: [], page_errors: [], console_errors: [], unknown_api: [], unknown_network: [],
  api_requests: [], limitations: ['Chromium UI only; no real model call, backend acceptance or manuscript write is exercised.'],
};
let browser;
fs.mkdirSync(output, { recursive: true });

function reviewResult(withExpression = true) {
  const result = {
    review_id: 501, rel_path: chapterPath, verdict: '通过',
    hard_gates: { passed: true, words: 2450, gates: [
      { key: '字数门', passed: true, blocking: false, detail: '正文 2450 字' },
      { key: '语言门', passed: true, blocking: false, detail: '中文正文' },
    ], locations: [], blocking_gates: [] },
    items: [{ 项: '核对账册来源', 判定: '已完成', 证据: '林舟核对了账册上的日期。' }],
    consistency: [], revise_instructions: [], counts: { 已完成: 1, 未完成: 0, 待核实: 0 },
    ai_used: true, candidate_hash: 'synthetic-candidate-hash', contract_hash: 'synthetic-contract-hash',
  };
  if (withExpression) {
    result.prose_quality = {
      version: 1, blocking: false, findings: [{
        kind: 'repeated_paragraph', severity: 'warning', line: 3, column: 2, end_line: 4,
        text: evidence, related_locations: [{ line: 9, column: 1, end_line: 10 }],
        reason: '这两处长段落使用相同原文。', suggestion: '检查是否有意复现；必要时改写其中一处。',
      }], counters: { repeated_paragraph: 1 }, truncated: false,
    };
    result.prose_review = [{ kind: '重复解释', evidence: '他担心掌柜不会认账。', line: 7, column: 3,
      reason: '动作后立即解释了人物心理。', suggestion: '可以让动作留下未说破的含义。' }];
  }
  return result;
}

function fixtureState() {
  const state = createFixtureState();
  state.overrides['POST /projects/901/review'] = reviewResult();
  state.overrides['POST /projects/901/review/deslop'] = {
    suggestions: [], proposal_ids: [], style_comparison: null, ai_used: true, ai_error: '',
    rejected_suggestions: [], source_changed: false, candidate_hash: 'synthetic-soft-source-hash',
  };
  return state;
}

function allowedAPI(method, apiPath) {
  if (method === 'GET' && ['/settings', '/projects', '/proposals', '/proposals/count',
    '/providers', '/agents', '/engines', '/chat/sessions', '/chat/quick-cards'].includes(apiPath)) return true;
  if (method === 'GET' && /^\/projects\/901(?:\/(?:chapters(?:\/content)?|tree|quality-matrix|debts|style\/fingerprints|chat-defaults|writing-prefs))?$/.test(apiPath)) return true;
  if (method === 'POST' && /^\/projects\/901\/review(?:\/deslop)?$/.test(apiPath)) return true;
  return apiPath === '/proposals/1101' && ['GET', 'PUT'].includes(method)
    || apiPath === '/proposals/1101/review' && method === 'POST';
}

async function newPage(state = fixtureState()) {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1100 }, reducedMotion: 'reduce', serviceWorkers: 'block' });
  const page = await context.newPage();
  page.setDefaultTimeout(7000);
  const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || url.pathname.startsWith('/api/')) {
      report.unknown_network.push({ method: route.request().method(), url: url.href });
      return route.abort();
    }
    const requested = path.resolve(dist, decodeURIComponent(url.pathname).replace(/^\/+/, ''));
    if (requested !== dist && !requested.startsWith(dist + path.sep)) {
      report.unknown_network.push({ url: url.href, reason: 'asset path outside production dist' });
      return route.abort();
    }
    const file = fs.existsSync(requested) && fs.statSync(requested).isFile() ? requested
      : ['/', '/inbox', '/project/901/review'].includes(url.pathname) ? path.join(dist, 'index.html') : null;
    if (!file) {
      report.unknown_network.push({ url: url.href, reason: 'unknown production asset' });
      return route.abort();
    }
    return route.fulfill({ status: 200, contentType: mime[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(file) });
  });
  await installFixtures(page, { state, handle: async ({ route, request, path: apiPath, method }) => {
    if (new URL(request.url()).origin !== origin || !allowedAPI(method, apiPath)) {
      state.unknown.push({ method, path: apiPath });
      await route.abort();
      return true;
    }
    return false;
  } });
  page.on('pageerror', error => report.page_errors.push({ url: page.url(), error: error.message }));
  page.on('console', message => { if (message.type() === 'error') report.console_errors.push({ url: page.url(), text: message.text() }); });
  return { page, state, context };
}

async function close(fixture) {
  report.unknown_api.push(...fixture.state.unknown);
  report.api_requests.push(...fixture.state.requests);
  await fixture.context.close();
}

async function openReview(page) {
  await page.goto(origin + '/project/901/review', { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: '审稿中心', exact: true }).waitFor();
  await page.getByRole('button', { name: '开始逐项审稿', exact: true }).waitFor();
}

async function screenshot(page, name) {
  const filename = name + '.png';
  await page.screenshot({ path: path.join(output, filename), fullPage: true, animations: 'disabled', timeout: 15000 });
  report.screenshots.push({ filename, url: page.url(), viewport: page.viewportSize() });
}

async function check(name, body) {
  const started = Date.now();
  try {
    const details = await body();
    report.checks.push({ name, status: 'passed', duration_ms: Date.now() - started, ...details });
    console.log('PASS ' + name);
  } catch (error) {
    report.checks.push({ name, status: 'failed', duration_ms: Date.now() - started, error: String(error.stack || error) });
    console.log('FAIL ' + name + ': ' + error.message);
  }
}

(async () => {
  assert.ok(dist.startsWith(root + path.sep), 'Production dist must be in this workspace');
  assert.ok(fs.existsSync(path.join(dist, 'index.html')), 'Existing production dist is required; no dependencies are installed');
  report.index_sha256 = crypto.createHash('sha256').update(fs.readFileSync(path.join(dist, 'index.html'))).digest('hex');
  browser = await chromium.launch({ channel: 'chrome', headless: true });
  report.browser_version = browser.version();

  await check('Review expressions show source positions and keep acceptance passing', async () => {
    const fixture = await newPage(); const { page, state } = fixture;
    try {
      await openReview(page);
      await page.getByRole('button', { name: '开始逐项审稿', exact: true }).click();
      const suggestions = page.locator('section[aria-label="表达建议（供作者判断）"]');
      await suggestions.getByText(evidence, { exact: false }).waitFor();
      assert.match(await suggestions.innerText(), /第 3—4 行（起始第 2 列）/);
      assert.match(await suggestions.innerText(), /重复出现位置：第 9—10 行/);
      assert.match(await suggestions.innerText(), /审稿表达建议 · 第 7 行，第 3 列/);
      assert.match(await suggestions.innerText(), /不影响合同与硬门禁判定/);
      assert.equal(await page.getByText('硬门禁 通过', { exact: true }).count(), 1);
      assert.equal(await page.getByRole('heading', { name: /需要处理/ }).count(), 0);
      assert.equal(await page.getByText('无需修改。', { exact: true }).count(), 0);
      await screenshot(page, 'review-expression-nonblocking');
      await suggestions.scrollIntoViewIfNeeded();
      await screenshot(page, 'review-expression-detail');
      assert.equal(state.requests.filter(request => request.path === '/projects/901/review').length, 1);
      return { source_locations: true, contract_and_hard_gates: '通过', suggestions_are_nonblocking: true };
    } finally { await close(fixture); }
  });

  await check('Review legacy response without expression fields remains usable', async () => {
    const state = fixtureState(); state.overrides['POST /projects/901/review'] = reviewResult(false);
    const fixture = await newPage(state); const { page } = fixture;
    try {
      await openReview(page);
      await page.getByRole('button', { name: '开始逐项审稿', exact: true }).click();
      await page.getByRole('heading', { name: '审稿结论', exact: true }).waitFor();
      assert.equal(await page.locator('section[aria-label="表达建议（供作者判断）"]').count(), 0);
      assert.equal(await page.getByText('硬门禁 通过', { exact: true }).count(), 1);
      await screenshot(page, 'review-legacy-compatible');
      return { optional_fields_missing: true, result_displayed: true };
    } finally { await close(fixture); }
  });

  const softCases = [
    { name: 'model-failure', patch: { ai_used: false, ai_error: '验收模拟：AI 软审失败，尚未完成表达审查' },
      expected: '验收模拟：AI 软审失败，尚未完成表达审查', error: true },
    { name: 'source-changed', patch: { source_changed: true, suggestions: [{ line: 3, original: evidence, replacement: '候选修改。' }] },
      expected: '正文已变化，建议对应旧版本，本次没有创建新提案。请重新软审当前正文。' },
    { name: 'unverified-filtered', patch: { rejected_suggestions: [{ index: 0, reason: '无正文依据' }, { index: 1, reason: '超过检查上限', count: 2 }] },
      expected: '已过滤 3 条未通过原文核验的建议' },
    { name: 'valid-empty', patch: {}, expected: '本次软审已完成，未提出表达修改建议。' },
  ];
  for (const scenario of softCases) await check('Soft review accurate state: ' + scenario.name, async () => {
    const state = fixtureState(); Object.assign(state.overrides['POST /projects/901/review/deslop'], scenario.patch);
    const count = state.proposals.length;
    const fixture = await newPage(state); const { page } = fixture;
    try {
      await openReview(page);
      await page.getByRole('button', { name: '去AI味软审', exact: true }).click();
      await page.getByRole('button', { name: '开始去AI味软审', exact: true }).click();
      const status = page.locator('[aria-label="去AI味软审结果"]');
      await status.getByText(scenario.expected, { exact: true }).waitFor();
      if (scenario.error) assert.match(await page.locator('.toast--error').innerText(), /AI 软审失败/);
      assert.equal(await page.locator('.toast--success').count(), 0);
      assert.doesNotMatch(await status.innerText(), /生成 0|0 条建议已进入收件箱/);
      assert.equal(state.proposals.length, count);
      assert.equal(state.requests.filter(request => request.method === 'POST' && request.path.startsWith('/proposals')).length, 0);
      await screenshot(page, 'soft-review-' + scenario.name);
      return { new_proposals: 0, success_toast: false, response_case: scenario.name };
    } finally { await close(fixture); }
  });

  await check('Inbox reviewed candidate shows expressions and edits mark old evidence', async () => {
    const state = fixtureState();
    state.proposals = [{ id: 1101, project_id: 901, task_id: 301, kind: 'chapter_draft', title: '表达验收候选',
      target_path: chapterPath, status: 'pending', created_at: '2026-10-03 10:00:00',
      meta: { pipeline_candidate: { protocol: 2, candidate_hash: 'synthetic-candidate-hash' } },
      content: evidence + '\n林舟核对了账册上的日期。', chars: 40, is_new_file: false,
      diff: [{ type: 'add', text: evidence, line: 3 }] }];
    state.overrides['POST /proposals/1101/review'] = () => {
      state.proposals[0].meta.pipeline_review = { verdict: '通过', ai_used: true, review_id: 501 };
      return reviewResult();
    };
    const fixture = await newPage(state); const { page } = fixture;
    try {
      await page.goto(origin + '/inbox', { waitUntil: 'domcontentloaded' });
      await page.getByRole('heading', { name: '收件箱', exact: true }).waitFor();
      await page.getByRole('button', { name: '查看', exact: true }).click();
      const drawer = page.getByRole('dialog', { name: '提案 #1101 · 表达验收候选', exact: true });
      await drawer.getByRole('button', { name: '审查当前候选', exact: true }).click();
      const suggestions = drawer.locator('section[aria-label="表达建议（供作者判断）"]');
      await suggestions.getByText(evidence, { exact: false }).waitFor();
      assert.match(await suggestions.innerText(), /第 3—4 行（起始第 2 列）/);
      assert.equal(await drawer.getByRole('button', { name: '应用', exact: true }).isEnabled(), true);
      await screenshot(page, 'inbox-reviewed-expression-nonblocking');
      await drawer.getByLabel('候选正文（修改后需重新审查）', { exact: true }).fill('作者修改后的候选正文。');
      await suggestions.getByText('正文已修改，以下建议对应修改前的候选。重新审查后才能核对当前正文的位置。', { exact: true }).waitFor();
      assert.equal(await drawer.getByRole('button', { name: '应用', exact: true }).isEnabled(), false);
      assert.equal(state.proposals[0].status, 'pending');
      assert.equal(state.requests.filter(request => /\/apply$/.test(request.path)).length, 0);
      await screenshot(page, 'inbox-edited-expression-stale');
      return { reviewed_candidate_apply_enabled: true, edited_candidate_apply_enabled: false, automatic_apply_requests: 0 };
    } finally { await close(fixture); }
  });

  assert.deepEqual(report.page_errors, []);
  assert.deepEqual(report.console_errors, []);
  assert.deepEqual(report.unknown_api, []);
  assert.deepEqual(report.unknown_network, []);
  assert.equal(report.checks.filter(check => check.status === 'failed').length, 0);
})().catch(error => {
  report.fatal = String(error.stack || error);
  console.error(report.fatal);
  process.exitCode = 1;
}).finally(async () => {
  report.finished_at = new Date().toISOString();
  report.counts = { passed: report.checks.filter(check => check.status === 'passed').length,
    failed: report.checks.filter(check => check.status === 'failed').length,
    screenshots: report.screenshots.length, page_errors: report.page_errors.length,
    console_errors: report.console_errors.length, unknown_api: report.unknown_api.length,
    unknown_network: report.unknown_network.length, api_requests: report.api_requests.length };
  fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2), 'utf8');
  if (browser) await browser.close();
  console.log(JSON.stringify(report.counts));
});
