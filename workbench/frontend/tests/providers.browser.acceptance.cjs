/* Run after `npm run build`: node tests/providers.browser.acceptance.cjs
 * Uses production assets through Playwright route fulfillment without a server.
 * Every API is synthetic and intercepted; request bodies and credentials are
 * never written to stdout, reports, or screenshots. No backend is contacted.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const ts = require('typescript');
const { chromium } = require(process.env.WORKBENCH_PLAYWRIGHT || 'C:/Users/30332/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const { installFixtures, createFixtureState } = require('./workbench.browser.fixtures.cjs');

const root = path.resolve(__dirname, '../../..');
const dist = path.resolve(root, process.env.WORKBENCH_ACCEPTANCE_DIST || 'workbench/frontend/dist');
const output = path.resolve(root, process.env.WORKBENCH_PROVIDER_ACCEPTANCE_OUTPUT || '.workbench/provider-settings-acceptance/browser');
const origin = 'http://127.0.0.1:8790';
const clipboardSource = fs.readFileSync(path.join(root, 'workbench/frontend/src/lib/clipboard.ts'), 'utf8');
const { outputText: clipboardJavaScript } = ts.transpileModule(clipboardSource, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } });
const clipboardModule = `data:text/javascript;base64,${Buffer.from(clipboardJavaScript).toString('base64')}`;
const syntheticKey = `fixture-${crypto.randomBytes(24).toString('hex')}`;
const redact = value => String(value).split(syntheticKey).join('[synthetic credential redacted]');
const sleep = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const report = {
  started_at: new Date().toISOString(),
  asset_source: 'Production dist fulfilled in memory; no listener',
  api_source: 'All API requests intercepted with synthetic fixtures',
  checks: [], screenshots: [], dialogs: [], page_errors: [], unknown_api: [],
  limitations: ['Chromium only; browser password-manager chrome and third-party extensions are outside page automation'],
};
let browser;

function fixtureState() {
  const state = createFixtureState();
  state.providerRows = [{
    id: 1, provider_id: 'fixture-provider', display_name: '验收模型服务', source: 'workbench_custom',
    base_url: 'https://fixture.invalid/v1', timeout_seconds: 60, enabled: true, health: 'ok',
    has_secret: true, masked_secret: '****', capabilities: ['chat'],
    models: [
      { id: 'fixture-small', name: '验收快速模型', context_window: 131072, max_tokens: 4096 },
      { id: 'fixture-large', name: '验收完整模型', context_window: 262144, max_tokens: 16384 },
      { id: 'fixture-reasoning', name: '验收推理模型', context_window: 1048576, max_tokens: 32768 },
    ], created_at: '2026-10-03 10:00:00',
  }];
  state.imageProviderRows = [{
    id: 1, provider_id: 'fixture-image', display_name: '验收图片服务', model_id: 'fixture-image-model',
    base_url: 'https://fixture.invalid/v1', timeout_seconds: 60, enabled: true, health: 'ok',
    has_secret: true, masked_secret: '****', adapter_key: 'openai-images', capabilities: {
      qualities: ['auto', 'high'], formats: ['png', 'jpeg'], moderations: ['auto'], max_n: 4,
      supports_edit: true, base_resolutions: ['1K', '2K'], ratios: ['1:1', '2:3'], min_side: 256, max_side: 4096,
    }, created_at: '2026-10-03 10:00:00',
  }];
  state.overrides['GET /providers'] = () => ({
    providers: state.providerRows, capability_tags: ['chat', 'reasoning'], cipher: 'fixture',
    defaults: { provider_id: 'fixture-provider', model_id: 'fixture-large' },
  });
  state.overrides['POST /providers'] = ({ body }) => saveRow(state.providerRows, body);
  state.overrides['POST /providers/models'] = {
    models: [{ id: 'fixture-small' }, { id: 'fixture-fetched-one' }, { id: 'fixture-fetched-two' }], count: 3,
  };
  state.overrides['GET /images/providers'] = () => ({ providers: state.imageProviderRows });
  state.overrides['POST /images/providers'] = ({ body }) => saveRow(state.imageProviderRows, body);
  return state;
}

function saveRow(rows, body) {
  let row = rows.find(item => item.provider_id === body.provider_id);
  const secretWasSet = !!row?.has_secret;
  if (!row) { row = { id: rows.length + 1, source: 'workbench_custom', health: '', capabilities: [], created_at: '2026-10-03 10:00:00' }; rows.push(row); }
  // The response never returns the submitted credential.
  const { api_key, ...safeFields } = body;
  Object.assign(row, safeFields, { has_secret: api_key === null ? secretWasSet : !!api_key, masked_secret: '****' });
  return row;
}

async function newPage(state = fixtureState(), width = 1440) {
  const page = await browser.newPage({ viewport: { width, height: 1000 }, reducedMotion: 'reduce' });
  page.setDefaultTimeout(6500);
  const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.ico': 'image/x-icon', '.woff2': 'font/woff2' };
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    const requested = path.resolve(dist, decodeURIComponent(url.pathname).replace(/^\/+/, ''));
    if (requested !== dist && !requested.startsWith(dist + path.sep)) return route.fulfill({ status: 400, body: 'Invalid fixture asset path' });
    const file = fs.existsSync(requested) && fs.statSync(requested).isFile() ? requested : path.extname(requested) ? null : path.join(dist, 'index.html');
    if (!file) return route.fulfill({ status: 404, body: 'Fixture asset missing' });
    return route.fulfill({ status: 200, contentType: mime[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(file) });
  });
  await installFixtures(page, { state });
  page.on('dialog', async dialog => { report.dialogs.push({ type: dialog.type() }); await dialog.dismiss(); });
  page.on('pageerror', error => report.page_errors.push(redact(error.message)));
  return { page, state };
}

async function close(page, state) {
  report.unknown_api.push(...state.unknown.map(item => `${item.method} ${item.path}`));
  await page.close();
}

async function openSettings(page) {
  await page.goto(`${origin}/settings?section=providers`, { waitUntil: 'domcontentloaded' });
  await page.getByRole('heading', { name: '供应商列表', exact: true }).waitFor();
  await page.locator('.provider-row').filter({ hasText: '验收模型服务' }).waitFor();
}

const editor = page => page.locator('.provider-editor-modal');
const editExisting = page => page.locator('.provider-row').filter({ hasText: '验收模型服务' }).getByRole('button', { name: '编辑', exact: true });
const card = (page, number) => editor(page).getByRole('article', { name: `模型 ${number}`, exact: true });
const submitted = (state, endpoint = '/providers') => state.requests.filter(item => item.method === 'POST' && item.path === endpoint);

async function assertMasked(input) {
  const mask = await input.evaluate(el => ({ type: el.type, autocomplete: el.autocomplete, security: getComputedStyle(el).getPropertyValue('-webkit-text-security'), color: getComputedStyle(el).color, fill: getComputedStyle(el).getPropertyValue('-webkit-text-fill-color'), readonly: el.readOnly, lpignore: el.dataset.lpignore, onepignore: el.dataset['1pIgnore'] }));
  assert.equal(mask.type, 'text');
  assert.equal(mask.autocomplete, 'off');
  assert.equal(mask.readonly, false);
  assert.equal(mask.lpignore, 'true');
  assert.equal(await input.getAttribute('data-1p-ignore'), 'true');
  assert.ok(mask.security === 'disc' || mask.color === 'rgba(0, 0, 0, 0)' || mask.fill === 'rgba(0, 0, 0, 0)', 'Credential must be visually masked');
}

async function assertNoPasswords(page) {
  assert.equal(await page.locator('input[type="password"]').count(), 0);
  assert.equal(await page.locator('input[autocomplete="new-password"]').count(), 0);
}

async function installClipboardSpy(page) {
  await page.addInitScript(() => {
    window.__copyProbe = { allowCopy: true, entries: [], clipboardCalls: 0, permissionQueries: 0 };
    document.execCommand = command => {
      const active = document.activeElement;
      window.__copyProbe.entries.push({ command, text: active instanceof HTMLTextAreaElement ? active.value : null });
      return window.__copyProbe.allowCopy;
    };
    const disallowPermissionClipboard = () => { window.__copyProbe.clipboardCalls++; throw new Error('Permission clipboard API must not be called'); };
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: disallowPermissionClipboard, write: disallowPermissionClipboard, readText: disallowPermissionClipboard, read: disallowPermissionClipboard } });
    if (navigator.permissions) navigator.permissions.query = () => { window.__copyProbe.permissionQueries++; return Promise.reject(new Error('Clipboard copy must not request permission')); };
  });
}

async function assertClipboardProbe(page, expectedText) {
  const probe = await page.evaluate(expected => ({
    intact: window.__copyProbe.entries.every(entry => entry.command === 'copy' && entry.text === expected),
    copies: window.__copyProbe.entries.length,
    clipboardCalls: window.__copyProbe.clipboardCalls,
    permissionQueries: window.__copyProbe.permissionQueries,
    remainingBuffers: document.querySelectorAll('textarea[aria-hidden="true"]').length,
  }), expectedText);
  assert.equal(probe.intact, true, 'Long Chinese text must be copied verbatim');
  assert.equal(probe.copies, 2);
  assert.equal(probe.clipboardCalls, 0);
  assert.equal(probe.permissionQueries, 0);
  assert.equal(probe.remainingBuffers, 0);
  return { copied_chars: expectedText.length, success_and_failure_toasts: true, clipboard_permission_calls: 0 };
}

async function screenshot(page, filename) {
  // Force a blur before capture and refuse to capture any explicitly shown Key.
  await page.getByRole('dialog').first().focus();
  assert.equal(await page.locator('.credential-input.is-visible').count(), 0);
  await page.screenshot({ path: path.join(output, filename), fullPage: true, animations: 'disabled' });
  report.screenshots.push({ file: filename, width: page.viewportSize().width, height: page.viewportSize().height, theme: await page.locator('html').getAttribute('data-theme') });
}

async function check(name, body) {
  const start = Date.now();
  try { const detail = await body(); report.checks.push({ name, status: 'passed', ms: Date.now() - start, ...detail }); console.log(`PASS ${name}`); }
  catch (error) { report.checks.push({ name, status: 'failed', ms: Date.now() - start, error: redact(error.stack || error) }); console.log(`FAIL ${name}: ${redact(error.message)}`); }
}

(async () => {
  assert.ok(dist.startsWith(root + path.sep) && fs.existsSync(path.join(dist, 'index.html')), 'Build production dist inside the workspace before running');
  assert.ok(output.startsWith(root + path.sep), 'Acceptance output must remain inside the workspace');
  fs.mkdirSync(output, { recursive: true });
  browser = await chromium.launch({ channel: 'chrome', headless: true });

  await check('credential create/edit, reveal, blur, fallback and Esc focus', async () => {
    const { page, state } = await newPage();
    try {
      await openSettings(page);
      const editButton = editExisting(page); await editButton.click();
      const key = editor(page).getByLabel('API Key', { exact: true });
      assert.equal(await key.inputValue(), ''); await assertMasked(key); await assertNoPasswords(page);
      await key.fill(syntheticKey); await assertMasked(key);
      await editor(page).getByRole('button', { name: '显示 API Key', exact: true }).click();
      assert.equal(await editor(page).getByRole('button', { name: '隐藏 API Key', exact: true }).getAttribute('aria-pressed'), 'true');
      assert.equal(await key.evaluate(el => getComputedStyle(el).getPropertyValue('-webkit-text-security')), 'none');
      await editor(page).locator('[name="provider-name"]').focus(); await assertMasked(key);
      // Remove only the support branch to exercise the safe non-WebKit fallback.
      await page.evaluate(() => { for (const sheet of document.styleSheets) { for (let index = sheet.cssRules.length - 1; index >= 0; index--) { const rule = sheet.cssRules[index]; if (rule instanceof CSSSupportsRule && rule.conditionText.includes('-webkit-text-security')) sheet.deleteRule(index); } } });
      await assertMasked(key);
      assert.ok(await editor(page).locator('.credential-input__fallback-mask').isVisible());
      assert.notEqual(await key.evaluate(el => getComputedStyle(el, '::placeholder').color), 'rgba(0, 0, 0, 0)');
      await page.keyboard.press('Escape'); await editor(page).waitFor({ state: 'hidden' });
      assert.ok(await editButton.evaluate(el => el === document.activeElement), 'Esc returns focus to edit trigger');
      const createButton = page.getByRole('button', { name: '新增供应商', exact: true }); await createButton.click();
      const newKey = editor(page).getByLabel('API Key', { exact: true }); assert.equal(await newKey.inputValue(), ''); await assertMasked(newKey);
      await page.keyboard.press('Escape'); assert.ok(await createButton.evaluate(el => el === document.activeElement));
      return { native_password_fields: 0, fallback_mask: true, focus_return: true };
    } finally { await close(page, state); }
  });

  await check('empty Key preserves existing credential and replacement saves', async () => {
    const { page, state } = await newPage();
    try {
      await openSettings(page); await editExisting(page).click();
      await editor(page).getByRole('button', { name: '保存供应商', exact: true }).click();
      await editor(page).waitFor({ state: 'hidden' });
      assert.equal(submitted(state).length, 1); assert.equal(submitted(state)[0].body.api_key, null);
      assert.equal(state.providerRows[0].has_secret, true);
      await editExisting(page).click(); const key = editor(page).getByLabel('API Key', { exact: true });
      assert.equal(await key.inputValue(), ''); await key.fill(syntheticKey); await assertMasked(key);
      await editor(page).getByRole('button', { name: '保存供应商', exact: true }).click(); await editor(page).waitFor({ state: 'hidden' });
      assert.ok(submitted(state)[1].body.api_key === syntheticKey, 'Replacement Key must be submitted intact');
      await editExisting(page).click(); assert.equal(await editor(page).getByLabel('API Key', { exact: true }).inputValue(), '');
      return { empty_submits_null: true, existing_key_never_returned: true, replacement_saved: true };
    } finally { await close(page, state); }
  });

  await check('model add/delete, presets, failed-save draft, multi-model retry and duplicate-submit guard', async () => {
    const { page, state } = await newPage();
    try {
      await openSettings(page); await page.getByRole('button', { name: '新增供应商', exact: true }).click();
      await editor(page).locator('[name="provider-id"]').fill('fixture-created');
      await editor(page).locator('[name="provider-name"]').fill('验收新服务');
      await editor(page).locator('[name="provider-base-url"]').fill('https://fixture.invalid/new');
      await editor(page).getByLabel('API Key', { exact: true }).fill(syntheticKey);
      await editor(page).getByRole('button', { name: '＋ 添加模型', exact: true }).click();
      await card(page, 1).getByLabel('模型 ID', { exact: true }).fill('fixture-new-one');
      const contextPreset = card(page, 1).getByRole('group', { name: '模型 1 上下文窗口预设', exact: true }).getByRole('button', { name: '1M', exact: true });
      await contextPreset.click(); assert.equal(await card(page, 1).getByLabel('上下文窗口 Token', { exact: true }).inputValue(), '1048576');
      await contextPreset.click(); assert.equal(await card(page, 1).getByLabel('上下文窗口 Token', { exact: true }).inputValue(), ''); await contextPreset.click();
      await card(page, 1).getByRole('group', { name: '模型 1 最大输出预设', exact: true }).getByRole('button', { name: '32K', exact: true }).click();
      await editor(page).getByRole('button', { name: '＋ 添加模型', exact: true }).click();
      await card(page, 2).getByLabel('模型 ID', { exact: true }).fill('fixture-new-two');
      await editor(page).getByRole('button', { name: '＋ 添加模型', exact: true }).click();
      await editor(page).getByRole('button', { name: '删除模型 3', exact: true }).click();
      assert.equal(await editor(page).locator('.provider-model-card').count(), 2);
      state.failures['POST /providers'] = '验收模拟保存失败';
      await editor(page).getByRole('button', { name: '保存供应商', exact: true }).click();
      await page.getByText('验收模拟保存失败', { exact: true }).waitFor();
      assert.ok(await editor(page).getByLabel('API Key', { exact: true }).inputValue() === syntheticKey, 'Failed save retains Key draft');
      assert.equal(await editor(page).locator('[name="provider-id"]').inputValue(), 'fixture-created');
      assert.equal(await editor(page).locator('.provider-model-card').count(), 2); await assertMasked(editor(page).getByLabel('API Key', { exact: true }));
      delete state.failures['POST /providers']; state.delays['POST /providers'] = 300;
      // Two synchronous clicks test the gap before React updates the disabled flag.
      await editor(page).getByRole('button', { name: '保存供应商', exact: true }).evaluate(button => { button.click(); button.click(); });
      await page.getByRole('button', { name: '保存中…', exact: true }).waitFor();
      await page.keyboard.press('Escape'); assert.ok(await editor(page).isVisible(), 'Saving cannot be cancelled by Esc');
      await editor(page).waitFor({ state: 'hidden' });
      assert.equal(submitted(state).length, 2, 'One failed request and one retry; concurrent click must not duplicate');
      const payload = submitted(state)[1].body;
      assert.deepEqual(payload.models.map(item => item.id), ['fixture-new-one', 'fixture-new-two']);
      assert.equal(payload.models[0].context_window, 1048576); assert.equal(payload.models[0].max_tokens, 32768);
      assert.ok(payload.api_key === syntheticKey, 'Retry submits preserved Key');
      assert.equal(state.providerRows.find(item => item.provider_id === 'fixture-created').has_secret, true);
      return { model_count_saved: 2, failed_draft_retained: true, concurrent_posts: 1 };
    } finally { await close(page, state); }
  });

  await check('fetched model selection deduplicates existing rows and rejects stale results', async () => {
    const { page, state } = await newPage();
    try {
      await openSettings(page); await editExisting(page).click();
      await editor(page).getByRole('button', { name: '拉取模型列表', exact: true }).click();
      await editor(page).getByRole('heading', { name: '发现 3 个模型', exact: true }).waitFor();
      assert.ok(await editor(page).getByRole('checkbox', { name: /^fixture-small\s*已添加$/ }).isDisabled());
      await editor(page).getByRole('button', { name: '全选', exact: true }).click();
      await editor(page).getByRole('button', { name: '添加所选模型', exact: true }).click();
      assert.equal(await editor(page).locator('.provider-model-card').count(), 5);
      assert.deepEqual(await editor(page).locator('.provider-model-card input').evaluateAll(inputs => inputs.filter(input => input.placeholder === '例如 deepseek-chat').map(input => input.value)), ['fixture-small', 'fixture-large', 'fixture-reasoning', 'fixture-fetched-one', 'fixture-fetched-two']);
      const selectAgain = editor(page).getByRole('button', { name: '全选', exact: true });
      if (await selectAgain.isEnabled()) await selectAgain.click();
      const append = editor(page).getByRole('button', { name: '添加所选模型', exact: true });
      if (await append.isEnabled()) await append.click();
      assert.equal(await editor(page).locator('.provider-model-card').count(), 5);
      await page.keyboard.press('Escape');
      state.delays['POST /providers/models'] = 600;
      await editExisting(page).click(); await editor(page).getByRole('button', { name: '拉取模型列表', exact: true }).click();
      await page.getByRole('button', { name: '拉取中…', exact: true }).waitFor();
      await editor(page).getByRole('button', { name: '取消', exact: true }).click();
      await page.getByRole('button', { name: '新增供应商', exact: true }).click();
      await editor(page).locator('[name="provider-id"]').fill('fixture-fresh-window');
      await sleep(750);
      assert.equal(await editor(page).locator('.provider-model-card').count(), 0);
      assert.equal(await editor(page).getByRole('heading', { name: /发现 .* 个模型/ }).count(), 0);
      assert.equal(await editor(page).locator('[name="provider-id"]').inputValue(), 'fixture-fresh-window');
      assert.ok(await editor(page).getByRole('button', { name: '拉取模型列表', exact: true }).isEnabled());
      return { deduplicated: true, cancelled_late_response_ignored: true };
    } finally { await close(page, state); }
  });

  await check('image provider edit/create uses masked text credentials and preserves empty Key', async () => {
    const { page, state } = await newPage();
    try {
      await installClipboardSpy(page); await page.goto(`${origin}/images`, { waitUntil: 'domcontentloaded' });
      const trigger = page.getByRole('button', { name: '模型配置', exact: true }); await trigger.click();
      const drawer = page.getByRole('dialog', { name: '图片模型配置', exact: true });
      await drawer.getByRole('button', { name: '编辑', exact: true }).click();
      const key = drawer.locator('[name="image-api-key"]'); assert.equal(await key.inputValue(), ''); await assertMasked(key); await assertNoPasswords(page);
      await drawer.getByRole('button', { name: '保存', exact: true }).click();
      await drawer.getByRole('button', { name: '＋ 新增供应商', exact: true }).waitFor();
      assert.equal(submitted(state, '/images/providers')[0].body.api_key, null);
      await drawer.getByRole('button', { name: '＋ 新增供应商', exact: true }).click();
      await drawer.locator('[name="image-provider-id"]').fill('fixture-image-created');
      await drawer.locator('[name="image-model-id"]').fill('fixture-image-new-model');
      await drawer.locator('[name="image-api-base-url"]').fill('https://fixture.invalid/image');
      await key.fill(syntheticKey); await assertMasked(key);
      await drawer.getByRole('button', { name: '显示 API Key', exact: true }).click();
      await drawer.locator('[name="image-provider-name"]').focus(); await assertMasked(key);
      state.failures['POST /images/providers'] = '验收图片保存失败';
      await drawer.getByRole('button', { name: '保存', exact: true }).click(); await page.getByText('验收图片保存失败', { exact: true }).waitFor();
      assert.ok(await key.inputValue() === syntheticKey, 'Failed image save retains Key');
      delete state.failures['POST /images/providers']; await drawer.getByRole('button', { name: '保存', exact: true }).click();
      await drawer.getByRole('button', { name: '＋ 新增供应商', exact: true }).waitFor();
      assert.ok(submitted(state, '/images/providers')[2].body.api_key === syntheticKey, 'Image save submits Key intact');
      await drawer.locator('.provider-row').first().getByRole('button', { name: '编辑', exact: true }).click();
      await screenshot(page, 'image-provider-edit-light.png');
      const dialogCopyText = '抽屉内真实点击复制验收：中文、标点与换行。\n下一行完整保留。'.repeat(100);
      await page.evaluate(async ({ moduleURL, text }) => {
        const { copyTextToClipboard } = await import(moduleURL);
        const button = document.createElement('button'); button.type = 'button'; button.textContent = '验收抽屉内复制';
        button.addEventListener('click', () => { try { copyTextToClipboard(text); window.__dialogCopySucceeded = true; } catch { window.__dialogCopySucceeded = false; } });
        document.querySelector('.drawer').appendChild(button);
      }, { moduleURL: clipboardModule, text: dialogCopyText });
      const dialogCopyButton = drawer.getByRole('button', { name: '验收抽屉内复制', exact: true }); await dialogCopyButton.click();
      const dialogCopyResult = await page.evaluate(expected => ({ succeeded: window.__dialogCopySucceeded, intact: window.__copyProbe.entries.length === 1 && window.__copyProbe.entries[0].text === expected, clipboardCalls: window.__copyProbe.clipboardCalls, permissionQueries: window.__copyProbe.permissionQueries }), dialogCopyText);
      assert.equal(dialogCopyResult.succeeded, true); assert.equal(dialogCopyResult.intact, true, 'Drawer focus trap must not redirect the copy buffer');
      assert.equal(dialogCopyResult.clipboardCalls, 0); assert.equal(dialogCopyResult.permissionQueries, 0);
      assert.ok(await dialogCopyButton.evaluate(el => el === document.activeElement), 'Drawer copy restores focus to its trigger');
      await page.keyboard.press('Escape'); assert.ok(await trigger.evaluate(el => el === document.activeElement));
      return { native_password_fields: 0, empty_key_preserved: true, failed_draft_retained: true, create_saved: true, dialog_copy_focus_guard: true };
    } finally { await close(page, state); }
  });

  const longChineseText = '# 复制验收材料\n\n' + '林舟把账册放在桌上：“先核对日期，再检查两行数字。”\n顾遥圈出缺页的位置，留下“第十七页；账目：三十两”的便签。\n'.repeat(100) + '\n末行：中文、标点、换行与空格  完整保留。';
  await check('chat copy reply preserves long Chinese text and reports failure without permission', async () => {
    const state = fixtureState();
    state.sessions = [{ id: 801, project_id: 901, title: '复制验收对话', agent: 'writing-assistant', provider_id: 'fixture-provider', model_id: 'fixture-large', auto_apply: 1, agent_pinned: 0, permission_mode: 'auto', discussion_only: false, created_at: '2026-10-03 10:00:00', updated_at: '2026-10-03 10:00:00' }];
    state.messages[801] = [{ id: 811, role: 'assistant', content: longChineseText, created_at: '2026-10-03 10:00:00', meta: {} }];
    const { page } = await newPage(state);
    try {
      await installClipboardSpy(page); await page.goto(`${origin}/project/901/chat`, { waitUntil: 'domcontentloaded' });
      const copy = page.getByRole('button', { name: '复制回复', exact: true }); await copy.click();
      await page.getByText('已复制完整回复', { exact: true }).waitFor();
      await page.evaluate(() => { window.__copyProbe.allowCopy = false; }); await copy.click();
      await page.getByText('复制失败，请选择回复文字后复制。', { exact: true }).waitFor();
      assert.ok(await copy.evaluate(el => el === document.activeElement), 'Copy restores focus to reply button');
      return await assertClipboardProbe(page, longChineseText);
    } finally { await close(page, state); }
  });

  await check('cards export copy preserves Markdown and reports failure without permission', async () => {
    const state = fixtureState(); state.overrides['POST /projects/901/cards/export'] = { count: 1, content: longChineseText };
    const { page } = await newPage(state);
    try {
      await installClipboardSpy(page); await page.goto(`${origin}/project/901/cards`, { waitUntil: 'domcontentloaded' });
      await page.getByRole('button', { name: '导出', exact: true }).click();
      await page.getByRole('heading', { name: '导出结果（md · 1 条）', exact: true }).waitFor();
      const copy = page.getByRole('button', { name: '复制', exact: true }); await copy.click();
      await page.getByText('已复制到剪贴板', { exact: true }).waitFor();
      await page.evaluate(() => { window.__copyProbe.allowCopy = false; }); await copy.click();
      await page.getByText('复制失败，请手动选择文本复制', { exact: true }).waitFor();
      assert.ok(await copy.evaluate(el => el === document.activeElement), 'Copy restores focus to export button');
      return await assertClipboardProbe(page, longChineseText);
    } finally { await close(page, state); }
  });

  for (const theme of ['light', 'dark', 'paper']) for (const width of [1440, 900, 560, 390]) {
    await check(`layout ${theme} ${width}x1000`, async () => {
      const state = fixtureState(); state.settings.appearance.theme = theme;
      const { page } = await newPage(state, width);
      try {
        await openSettings(page); const trigger = editExisting(page); await trigger.click();
        const modal = editor(page); const key = modal.getByLabel('API Key', { exact: true });
        await key.fill(syntheticKey); await modal.locator('[name="provider-name"]').focus(); await assertMasked(key); await assertNoPasswords(page);
        assert.equal(await page.locator('html').getAttribute('data-theme'), theme);
        const geometry = await modal.evaluate(panel => {
          const footer = panel.querySelector('.modal__footer').getBoundingClientRect(); const body = panel.querySelector('.modal__body');
          return { viewport: innerWidth, documentWidth: document.documentElement.scrollWidth, panelWidth: panel.clientWidth, panelScrollWidth: panel.scrollWidth, bodyWidth: body.clientWidth, bodyScrollWidth: body.scrollWidth, footerTop: footer.top, footerBottom: footer.bottom, viewportHeight: innerHeight };
        });
        assert.ok(geometry.documentWidth <= geometry.viewport + 1, 'Page must not overflow horizontally');
        assert.ok(geometry.panelScrollWidth <= geometry.panelWidth + 1, 'Modal must not overflow horizontally');
        assert.ok(geometry.bodyScrollWidth <= geometry.bodyWidth + 1, 'Form body must not overflow horizontally');
        assert.ok(geometry.footerTop >= 0 && geometry.footerBottom <= geometry.viewportHeight + 1, 'Save footer must remain inside viewport');
        await screenshot(page, `provider-edit-${theme}-${width}.png`);
        await modal.locator('.modal__body').evaluate(body => { body.scrollTop = body.scrollHeight; });
        assert.ok(await modal.getByRole('button', { name: '保存供应商', exact: true }).isVisible());
        const saveRect = await modal.getByRole('button', { name: '保存供应商', exact: true }).boundingBox();
        assert.ok(saveRect && saveRect.y + saveRect.height <= 1001, 'Save control stays visible after scrolling');
        await page.keyboard.press('Escape'); await modal.waitFor({ state: 'hidden' }); assert.ok(await trigger.evaluate(el => el === document.activeElement));
        return { no_horizontal_overflow: true, footer_visible: true, escape_focus_return: true };
      } finally { await close(page, state); }
    });
  }

  await check('zero page JS dialogs, runtime errors and unknown API calls', async () => {
    assert.deepEqual(report.dialogs, []); assert.deepEqual(report.page_errors, []); assert.deepEqual(report.unknown_api, []);
    return { page_js_dialogs: 0, page_errors: 0, unknown_api: 0 };
  });
})().catch(error => { report.fatal = redact(error.stack || error); process.exitCode = 1; }).finally(async () => {
  await browser?.close(); report.finished_at = new Date().toISOString();
  report.passed = report.checks.filter(item => item.status === 'passed').length;
  report.failed = report.checks.filter(item => item.status === 'failed').length;
  if (report.failed || report.fatal) process.exitCode = 1;
  if (fs.existsSync(output)) fs.writeFileSync(path.join(output, 'report.json'), JSON.stringify(report, null, 2));
  console.log(`Provider acceptance: ${report.passed} passed, ${report.failed} failed, ${report.screenshots.length} screenshots; page JS dialogs ${report.dialogs.length}, page errors ${report.page_errors.length}.`);
});
