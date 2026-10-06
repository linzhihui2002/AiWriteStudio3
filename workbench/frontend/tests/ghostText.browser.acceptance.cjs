/* Call runGhostTextAcceptance(page) with a browser test Page (including Tabbit).
 * Build first. Assets and every API are intercepted; no author files, listener,
 * backend, model provider or engine are used. This module never launches a browser.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {createFixtureState, installFixtures, chapterPath} = require('./workbench.browser.fixtures.cjs');

async function runGhostTextAcceptance(page) {
  const origin = 'http://127.0.0.1:8790';
  const dist = path.resolve(__dirname, '../dist');
  assert.ok(fs.existsSync(path.join(dist, 'index.html')), 'Run npm run build first');
  const state = createFixtureState();
  state.settings.editor.ghost_text = true;
  state.settings.editor.autosave_seconds = 0;
  const original = '林舟沿着石桥走到河岸，放下背上的包袱，抬头看向城门。守门人正在核对他的来信。';
  const candidate = '他把信递过去，指着封口上的红印。';
  const secondPath = '章节/第0002章.txt';
  const secondBody = '城门已经关上，守门人把信放回木盒，沿着城墙走向另一座望楼。';
  const second = {...state.chapters[901][0], rel_path: secondPath, file_name: '第0002章.txt', number: 2, title: '城门落锁'};
  state.chapters[901].push(second);
  state.tree[901].groups[0].nodes.push({...second, name: second.file_name, type: 'file', is_chapter: true});
  state.overrides['GET /projects/901/chapters/content'] = ({url}) => {
    const isSecond = url.searchParams.get('rel_path') === secondPath;
    const body = isSecond ? secondBody : original;
    return {...(isSecond ? second : state.chapters[901][0]), body, content: body, hash: isSecond ? 'ghost-second' : 'ghost-first'};
  };
  const normalResponse = {ok: true, candidate};
  const checks = [], pageErrors = [], unknownNetwork = [];
  const onPageError = error => pageErrors.push(String(error));
  page.on('pageerror', onPageError);
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/api/')) return route.fulfill({status: 503, body: 'Unmocked API blocked'});
    if (url.origin !== origin) {
      unknownNetwork.push(url.origin + url.pathname);
      return route.fulfill({status: 503, body: 'External network blocked'});
    }
    const asset = path.resolve(dist, url.pathname.startsWith('/assets/') ? '.' + url.pathname : 'index.html');
    if (!asset.startsWith(dist + path.sep)) return route.fulfill({status: 403, body: 'Invalid asset path'});
    return route.fulfill({path: asset});
  });
  await installFixtures(page, {state});
  // Old-page pagehide persists dirty drafts; clearing before reload alone leaks them.
  await page.addInitScript(() => localStorage.clear());
  page.setDefaultTimeout(8000);
  const editor = page.locator('.cm-content');
  const accept = page.getByRole('button', {name: 'Tab 接受', exact: true});
  const bodyText = async () => (await page.locator('.cm-line').allTextContents()).join('\n');
  const waitUntil = async (condition, label) => {
    const deadline = Date.now() + 8000;
    while (!await condition()) {
      assert.ok(Date.now() < deadline, 'Timed out: ' + label);
      await new Promise(resolve => setTimeout(resolve, 20));
    }
  };
  const reset = async (response = normalResponse, waitCandidate = true) => {
    state.overrides['POST /projects/901/ghost-text'] = response;
    await page.goto(origin + '/project/901/editor?path=' + encodeURIComponent(chapterPath), {waitUntil: 'domcontentloaded'});
    await editor.waitFor();
    await waitUntil(async () => await bodyText() === original, 'fresh original body');
    if (waitCandidate) await accept.waitFor();
    await editor.focus();
  };
  const check = async (name, action) => {
    try { await action(); checks.push({name, status: 'passed'}); }
    catch (error) { checks.push({name, status: 'failed', error: String(error.stack || error)}); }
  };
  const atStart = async () => { await editor.focus(); await editor.press('Control+Home'); };
  const atEnd = async () => { await editor.focus(); await editor.press('Control+End'); };
  const appendIsExact = async () => waitUntil(async () => await bodyText() === original + candidate, 'one exact chapter-tail append');
  const deferredCandidate = () => {
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    let started = false;
    let calls = 0;
    return {
      response: async () => { if (++calls > 1) return {ok: false, candidate: ''}; started = true; return pending; },
      started: () => started,
      release: () => release(normalResponse),
    };
  };
  const releaseAndSettle = async gate => {
    const response = page.waitForResponse(item => new URL(item.url()).pathname.endsWith('/ghost-text'));
    gate.release();
    await (await response).finished();
    await page.waitForTimeout(150);
  };

  try {
    await check('click from a selected chapter opening appends, focuses chapter end and undoes once', async () => {
      await reset(); await atStart();
      await editor.press('Shift+ArrowRight'); await editor.press('Shift+ArrowRight');
      assert.equal(await page.evaluate(() => window.getSelection().toString()), original.slice(0, 2));
      await accept.click(); await appendIsExact();
      const caret = await editor.evaluate(element => {
        const selection = window.getSelection(), range = document.createRange();
        range.selectNodeContents(element); range.setEnd(selection.focusNode, selection.focusOffset);
        return {at: range.toString().length, collapsed: selection.isCollapsed, focused: document.activeElement === element};
      });
      assert.equal(caret.at, (original + candidate).length); assert.ok(caret.collapsed && caret.focused);
      assert.equal(await accept.count(), 0);
      await editor.press('Control+z');
      await waitUntil(async () => await bodyText() === original, 'single undo restores original');
    });
    await check('click from a middle selection preserves all original text', async () => {
      await reset(); await atStart();
      for (let index = 0; index < 12; index++) await editor.press('ArrowRight');
      await editor.press('Shift+ArrowRight'); await editor.press('Shift+ArrowRight');
      assert.equal(await page.evaluate(() => window.getSelection().toString()), original.slice(12, 14));
      await accept.click(); await appendIsExact();
    });
    await check('Tab accepts only at an empty chapter-tail selection', async () => {
      await reset(); await atStart(); await editor.press('Tab');
      assert.equal(await bodyText(), original); assert.equal(await accept.count(), 1);
      await atEnd(); await editor.press('Tab'); await appendIsExact();
    });
    await check('Escape rejects without editing', async () => {
      await reset(); await editor.press('Escape');
      await waitUntil(async () => await accept.count() === 0, 'rejected candidate hidden');
      assert.equal(await bodyText(), original);
    });
    await check('two immediate clicks append only once', async () => {
      await reset(); await accept.evaluate(button => {button.click(); button.click();}); await appendIsExact();
    });
    await check('editing invalidates an already visible candidate', async () => {
      await reset(); await atEnd(); await page.keyboard.insertText('补');
      await waitUntil(async () => await bodyText() === original + '补' && await accept.count() === 0, 'changed body has no old candidate');
    });
    await check('switching chapters invalidates an already visible candidate', async () => {
      await reset();
      await page.getByRole('combobox', {name: '选择章节', exact: true}).selectOption(secondPath);
      await waitUntil(async () => await bodyText() === secondBody, 'second chapter loaded');
      assert.equal(await accept.count(), 0);
    });
    await check('a delayed response after editing cannot restore an old candidate', async () => {
      const gate = deferredCandidate();
      try {
        await reset(gate.response, false); await waitUntil(gate.started, 'candidate request started');
        await atEnd(); await page.keyboard.insertText('补'); await releaseAndSettle(gate);
        assert.equal(await bodyText(), original + '补'); assert.equal(await accept.count(), 0);
      } finally { gate.release(); }
    });
    await check('a delayed response after switching chapters cannot cross chapters', async () => {
      const gate = deferredCandidate();
      try {
        await reset(gate.response, false); await waitUntil(gate.started, 'candidate request started');
        await page.getByRole('combobox', {name: '选择章节', exact: true}).selectOption(secondPath);
        await waitUntil(async () => await bodyText() === secondBody, 'second chapter loaded');
        await releaseAndSettle(gate);
        assert.equal(await bodyText(), secondBody); assert.equal(await accept.count(), 0);
      } finally { gate.release(); }
    });
    await check('composition blocks Tab and invalidates the candidate', async () => {
      await reset(); await atEnd(); await editor.dispatchEvent('compositionstart', {data: '测'});
      try {
        await editor.press('Tab');
        await waitUntil(async () => await accept.count() === 0, 'composition candidate hidden');
        assert.equal(await bodyText(), original);
      } finally { await editor.dispatchEvent('compositionend', {data: ''}); }
    });
    await check('no page errors or unmocked network requests', async () => {
      assert.equal(pageErrors.length, 0); assert.equal(state.unknown.length, 0); assert.equal(unknownNetwork.length, 0);
    });
    return {checks, page_errors: pageErrors, unknown_api: state.unknown, unknown_network: unknownNetwork,
      isolation: 'Production dist and synthetic APIs only; no backend, model or author-file writes.'};
  } finally { page.removeListener('pageerror', onPageError); }
}

module.exports = {runGhostTextAcceptance};
