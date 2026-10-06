/* Production UI acceptance with synthetic, in-memory APIs. Run through Tabbit's
 * task-owned page by calling runAcceptance({ page }); unknown network requests
 * are aborted, so no listener, model call or manuscript write is used.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { createFixtureState, installFixtures } = require('./workbench.browser.fixtures.cjs');

const root = path.resolve(__dirname, '../../..');
const origin = 'http://127.0.0.1:8790';
const clone = value => JSON.parse(JSON.stringify(value));
const initialCandidates = [
  { id: 'outline-901-a', title: '账册追索', content: '林舟核对账册，发现一笔被人改过的旧账。\n第一卷：寻找失踪的记账人。', source: 'model', conversation: [] },
  { id: 'outline-901-b', title: '错投信件', content: '林舟追查一封错投的信，找到桥边的旧宅。\n第一卷：查清送信人的身份。', source: 'model', conversation: [] },
  { id: 'outline-901-c', title: '桥墩暗记', content: '林舟辨认桥墩上的暗记，追踪半夜过桥的车队。\n第一卷：找到车队运送的旧箱。', source: 'model', conversation: [] },
];
const additionalCandidates = [
  { id: 'outline-901-d', title: '集市赊账', content: '林舟核查集市各家的赊账，发现相同的担保人。\n第一卷：追查担保人的真实目的。', source: 'model', conversation: [] },
  { id: 'outline-901-e', title: '失踪船工', content: '林舟清点渡口船只，追查失踪船工留下的木牌。\n第一卷：找到木牌对应的旧船。', source: 'model', conversation: [] },
];

function gate() {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  return { pending, release };
}

async function runAcceptance(options) {
  const page = options.page;
  const dist = path.resolve(root, options.dist || process.env.WORKBENCH_ACCEPTANCE_DIST || 'workbench/frontend/dist');
  const output = path.resolve(root, options.output || '.workbench/logs/outline-candidates/browser');
  assert.ok(dist.startsWith(root + path.sep), 'Production dist must be within this workspace');
  assert.ok(fs.existsSync(path.join(dist, 'index.html')), 'Build the frontend before browser acceptance');
  fs.mkdirSync(output, { recursive: true });
  const report = {
    started_at: new Date().toISOString(), origin, production_assets: dist,
    isolation: 'Tabbit task-owned page, production assets fulfilled from dist, synthetic APIs only, unknown requests aborted; no backend, model, author book or global DSH access.',
    index_sha256: crypto.createHash('sha256').update(fs.readFileSync(path.join(dist, 'index.html'))).digest('hex'),
    checks: [], screenshots: [], page_errors: [], console_errors: [], unknown_api: [], unknown_network: [], api_requests: [],
    limitations: ['UI contracts are tested with in-memory API data; backend deduplication and model quality require the separate service tests.'],
  };
  const state = createFixtureState();
  state.outlines[901].candidates = [];
  state.outlines[901].rolling_window = 2;
  state.outlines[902].candidates = [{ id: 'outline-902-a', title: '巡城夜信', content: '顾遥核对夜巡名册，找出错投信件的收信人。', source: 'model', conversation: [] }];
  state.outlines[902].rolling_window = 2;
  let generationCount = 0, refinementCount = 0, generationGate = null, refinementGate = null;
  const gates = [];
  let generationFailure = false, refinementFailure = false;
  state.overrides['POST /projects/901/outline/candidates'] = async ({reply}) => {
    const wait = generationGate; generationGate = null;
    if (wait) await wait.pending;
    if (generationFailure) { generationFailure = false; return { status: 503, body: { detail: '验收模拟：生成服务暂不可用' } }; }
    generationCount += 1;
    const next = generationCount === 1 ? initialCandidates : generationCount === 2 ? additionalCandidates : [];
    state.outlines[901].candidates.push(...clone(next));
    return { candidates: clone(state.outlines[901].candidates), count: state.outlines[901].candidates.length,
      added_count: next.length, duplicate_count: generationCount === 1 ? 0 : generationCount === 2 ? 1 : 3,
      message: next.length ? '' : '本次没有新的差异化候选，已有候选已保留。可以调整灵感后继续生成。' };
  };
  const rawHandle = async ({route, request, path: apiPath, method, body, reply}) => {
    if (new URL(request.url()).origin !== origin) { report.unknown_network.push({ method, url: request.url() }); await route.abort(); return true; }
    const match = /^\/projects\/901\/outline\/candidates\/([^/]+)\/refine$/.exec(apiPath);
    if (method === 'POST' && match) {
      const wait = refinementGate; refinementGate = null;
      if (wait) await wait.pending;
      if (refinementFailure) { refinementFailure = false; await reply({ detail: '验收模拟：优化服务暂不可用' }, 503); return true; }
      const parent = state.outlines[901].candidates.find(candidate => candidate.id === match[1]);
      assert.ok(parent, 'Refinement must reference an existing candidate identity');
      refinementCount += 1;
      const child = { ...clone(parent), id: `outline-901-refined-${refinementCount}`, title: `${parent.title}·优化${refinementCount}`,
        parent_id: parent.id, content: `${parent.content}\n优化要求：${body.message}`,
        conversation: [...clone(parent.conversation || []), { role: 'user', content: body.message }, { role: 'assistant', content: `已依据要求优化：${body.message}` }] };
      state.outlines[901].candidates.push(child);
      await reply({ candidate: clone(child), candidates: clone(state.outlines[901].candidates), count: state.outlines[901].candidates.length }); return true;
    }
    if (method === 'POST' && apiPath === '/projects/901/outline/lock') {
      assert.equal(typeof body.candidate_id, 'string', 'Locking must send stable candidate_id');
      assert.equal(body.index, undefined, 'Locking must not rely on a mutable array position');
      const candidate = state.outlines[901].candidates.find(candidate => candidate.id === body.candidate_id);
      assert.ok(candidate);
      state.outlines[901].locked = { title: candidate.title, candidate_id: candidate.id, at: '2026-10-04 12:00:00' };
      state.outlines[901].content = candidate.content;
      await reply({ locked: true, candidate_id: candidate.id }); return true;
    }
    if (method !== 'GET' && !(method === 'POST' && apiPath === '/projects/901/outline/candidates')) {
      report.unknown_api.push({method,path:apiPath}); await route.abort(); return true;
    }
    return false;
  };
  page.setDefaultTimeout(8000);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const mime = { '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.woff2': 'font/woff2', '.ico': 'image/x-icon' };
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || url.pathname.startsWith('/api/')) { report.unknown_network.push({method:route.request().method(),url:url.href}); return route.abort(); }
    const requested = path.resolve(dist, decodeURIComponent(url.pathname).replace(/^\/+/, ''));
    if (requested !== dist && !requested.startsWith(dist + path.sep)) { report.unknown_network.push({url:url.href,reason:'asset path outside dist'}); return route.abort(); }
    const file = fs.existsSync(requested) && fs.statSync(requested).isFile() ? requested : /^\/(?:project\/90[12]\/(?:outline|chat))?$/.test(url.pathname) ? path.join(dist, 'index.html') : null;
    if (!file) { report.unknown_network.push({url:url.href,reason:'unknown production asset'}); return route.abort(); }
    await route.fulfill({status:200,contentType:mime[path.extname(file)] || 'application/octet-stream',body:fs.readFileSync(file)});
  });
  await installFixtures(page, {state,handle:rawHandle});
  page.on('pageerror', error => report.page_errors.push(error.message));
  page.on('console', message => { if (message.type() === 'error') report.console_errors.push(message.text()); });

  const cards = () => page.locator('.workspace-candidate-grid > article');
  const panel = () => page.getByRole('region', {name:'候选对话优化',exact:true});
  const refineInput = () => page.locator('#outline-refine-message');
  const namedCard = title => page.getByRole('article', {name:title,exact:true});
  const generationRequests = () => state.requests.filter(request => request.method === 'POST' && request.path === '/projects/901/outline/candidates');
  const refinementRequests = () => state.requests.filter(request => request.method === 'POST' && /\/candidates\/[^/]+\/refine$/.test(request.path));
  const waitCards = async expected => page.waitForFunction(count => document.querySelectorAll('.workspace-candidate-grid > article').length === count, expected);
  const assertBodyBeforeLock = () => assert.equal(state.outlines[901].content, '# 验收书甲·渡桥\n林舟出门寻找线索，第一卷在桥边发现旧账。');
  const capture = async name => {
    const destination = path.join(output, name + '.png');
    const result = await page.screenshot({path:destination,fullPage:false,animations:'disabled',timeout:15000});
    const source = result && !Buffer.isBuffer(result) && (result.path || result.nextAction?.path);
    if (source && source !== destination) fs.copyFileSync(source, destination);
    report.screenshots.push({name,path:destination,viewport:page.viewportSize(),url:page.url()});
  };
  const check = async (name, body) => {
    const started = Date.now();
    try { const details = await body(); report.checks.push({name,status:'passed',duration_ms:Date.now()-started,...details}); }
    catch (error) { report.checks.push({name,status:'failed',duration_ms:Date.now()-started,error:String(error.stack || error)}); throw error; }
  };
  try {
    await page.goto(origin + '/project/901/outline', {waitUntil:'domcontentloaded'});
    await page.getByRole('heading', {name:'大纲规划',exact:true}).waitFor();
    await page.getByRole('button', {name:'生成 2–3 个候选',exact:true}).waitFor();
    await page.evaluate(() => { localStorage.setItem('aiw.shell.sidebarCollapsed', '0'); });

    await check('First generation has visible progress and prevents rapid duplicate submission', async () => {
      const hold = gate(); gates.push(hold); generationGate = hold;
      await page.getByPlaceholder(/把你的灵感写在这里/).fill('渡桥人寻找失落的账册，三种不同调查方向。');
      await page.getByRole('button', {name:'生成 2–3 个候选',exact:true}).evaluate(button => { button.click(); button.click(); });
      await page.getByRole('button', {name:'正在生成候选…',exact:true}).waitFor();
      assert.equal(await page.getByRole('button', {name:'正在生成候选…',exact:true}).isDisabled(), true);
      assert.match(await page.getByRole('status').filter({hasText:'正在生成候选'}).first().innerText(), /正在生成候选/);
      assert.equal(generationRequests().length, 1);
      await capture('01-generating'); hold.release(); await waitCards(3);
      await page.getByRole('button', {name:'继续生成 2–3 个候选',exact:true}).waitFor();
      assertBodyBeforeLock();
      return {requests:1,candidates:3,formal_outline_unchanged:true};
    });

    await check('Additional generation appends candidates, reports filtered duplicates and retains all prior bodies', async () => {
      await page.getByRole('button', {name:'继续生成 2–3 个候选',exact:true}).click(); await waitCards(5);
      const text = await cards().allTextContents();
      for (const candidate of [...initialCandidates, ...additionalCandidates]) assert.ok(text.some(item => item.includes(candidate.title) && item.includes(candidate.content.split('\n')[0])));
      await page.getByRole('status').filter({hasText:'已新增 2 个候选，累计 5 个'}).first().waitFor();
      assert.match(await page.getByRole('status').filter({hasText:'已新增 2 个候选，累计 5 个'}).first().innerText(), /已过滤 1 个重复方案/);
      assert.equal(new Set(state.outlines[901].candidates.map(candidate => candidate.content)).size, 5);
      await capture('02-appended-candidates'); assertBodyBeforeLock();
      return {originals:3,added:2,total:5,duplicates_reported:1};
    });

    await check('All-duplicate generation and failed generation preserve candidates and allow retry', async () => {
      await page.getByRole('button', {name:'继续生成 2–3 个候选',exact:true}).click();
      await page.getByRole('status').filter({hasText:'本次没有新的差异化候选'}).first().waitFor(); assert.equal(await cards().count(),5);
      generationFailure = true;
      await page.getByRole('button', {name:'继续生成 2–3 个候选',exact:true}).click();
      await page.getByRole('alert').filter({hasText:'生成失败，已有候选已保留'}).first().waitFor();
      assert.equal(await cards().count(),5); assert.equal(await page.getByRole('button', {name:'继续生成 2–3 个候选',exact:true}).isEnabled(),true);
      assertBodyBeforeLock(); return {all_duplicate_preserved:5,failure_preserved:5,retry_enabled:true};
    });

    await check('Selected candidate conversation preserves originals and continues from its latest child', async () => {
      await namedCard('账册追索').getByRole('button', {name:/对话优化$/}).click();
      await panel().waitFor(); assert.match(await panel().innerText(), /账册追索/);
      const hold = gate(); gates.push(hold); refinementGate = hold;
      await refineInput().fill('保留账册主线，让主角的代价更具体。');
      await panel().getByRole('button',{name:'发送并优化',exact:true}).evaluate(button => {button.click();button.click();});
      await panel().getByRole('button', {name:'正在优化候选…',exact:true}).waitFor();
      assert.equal(await panel().getByRole('button', {name:'正在优化候选…',exact:true}).isDisabled(),true);
      assert.equal(refinementRequests().length,1);
      await capture('03-refining'); hold.release(); await waitCards(6);
      await panel().getByText('保留账册主线，让主角的代价更具体。',{exact:true}).waitFor();
      assert.match(await panel().innerText(),/账册追索·优化1/); assert.equal(await refineInput().inputValue(),'');
      assert.equal(state.outlines[901].candidates[0].content,initialCandidates[0].content);
      await refineInput().fill('第二轮只加强卷末悬念，保留上一轮代价。');
      await panel().getByRole('button',{name:'发送并优化',exact:true}).click(); await waitCards(7);
      const requests = refinementRequests();
      assert.equal(requests[0].path,'/projects/901/outline/candidates/outline-901-a/refine');
      assert.equal(requests[1].path,'/projects/901/outline/candidates/outline-901-refined-1/refine');
      const child = state.outlines[901].candidates.at(-1); assert.equal(child.conversation.length,4); assert.equal(child.parent_id,'outline-901-refined-1');
      await panel().getByText('第二轮只加强卷末悬念，保留上一轮代价。',{exact:true}).waitFor();
      assert.match(await panel().innerText(),/保留账册主线，让主角的代价更具体。/);
      await capture('04-conversation-history'); assertBodyBeforeLock();
      return {refinement_requests:2,retained_originals:5,versions:2,conversation_messages:4,latest_child_context:true};
    });

    await check('Candidate switching keeps separate unsent drafts and a failed refinement retains the author input', async () => {
      await refineInput().fill('账册版本尚未发送的要求');
      await namedCard('错投信件').getByRole('button',{name:/对话优化$/}).click();
      assert.equal(await refineInput().inputValue(),''); await refineInput().fill('信件版本尚未发送的要求');
      await namedCard('账册追索·优化1·优化2').getByRole('button',{name:/对话优化$/}).click();
      assert.equal(await refineInput().inputValue(),'账册版本尚未发送的要求');
      refinementFailure = true;
      await panel().getByRole('button',{name:'发送并优化',exact:true}).click();
      await panel().getByRole('alert').waitFor(); assert.match(await panel().getByRole('alert').innerText(),/优化服务暂不可用/);
      assert.equal(await refineInput().inputValue(),'账册版本尚未发送的要求'); assert.equal(await cards().count(),7);
      assert.equal(await panel().getByRole('button',{name:'发送并优化',exact:true}).isEnabled(),true);
      await capture('05-refinement-error'); assertBodyBeforeLock();
      return {drafts_isolated:true,input_preserved:true,originals_preserved:true,retry_enabled:true};
    });

    await check('Reload restores candidate versions and their persisted conversation history', async () => {
      await page.reload({waitUntil:'domcontentloaded'}); await waitCards(7);
      await namedCard('账册追索·优化1·优化2').getByRole('button',{name:/对话优化$/}).click();
      await panel().getByText('第二轮只加强卷末悬念，保留上一轮代价。',{exact:true}).waitFor();
      assert.match(await panel().innerText(),/保留账册主线，让主角的代价更具体。/);
      assert.equal(await panel().getByText('保留账册主线，让主角的代价更具体。',{exact:true}).count(),1);
      return {candidates_after_reload:7,conversation_messages:4};
    });

    await check('Narrow viewport keeps selected candidate conversation controls visible without horizontal overflow', async () => {
      await page.setViewportSize({width:560,height:1000}); await panel().scrollIntoViewIfNeeded();
      const layout = await page.evaluate(() => ({width:window.innerWidth,document:document.documentElement.scrollWidth,body:document.body.scrollWidth}));
      assert.ok(layout.document <= layout.width + 1, JSON.stringify(layout)); assert.ok(layout.body <= layout.width + 1, JSON.stringify(layout));
      assert.equal(await refineInput().isVisible(),true); assert.equal(await panel().getByRole('button',{name:'发送并优化',exact:true}).isVisible(),true);
      await capture('06-narrow-conversation'); await page.setViewportSize({width:1440,height:1000}); return layout;
    });

    await check('Project switch rejects the old refinement response and clears the previous project selection', async () => {
      const hold = gate(); gates.push(hold); refinementGate = hold;
      await refineInput().fill('仅属于甲书的迟到优化响应。');
      await panel().getByRole('button',{name:'发送并优化',exact:true}).click();
      await panel().getByRole('button',{name:'正在优化候选…',exact:true}).waitFor();
      await page.getByLabel('当前创作',{exact:true}).selectOption('902'); await page.waitForURL('**/project/902/chat');
      await page.getByRole('link',{name:'大纲规划',exact:true}).click(); await page.waitForURL('**/project/902/outline'); await waitCards(1);
      const staleResponse = page.waitForResponse(response => /outline-901-refined-2\/refine$/.test(response.url()));
      hold.release(); await staleResponse;
      await page.waitForFunction(() => !document.body.innerText.includes('正在优化候选…'));
      assert.match(await cards().innerText(),/巡城夜信/); assert.doesNotMatch(await page.locator('main').innerText(),/账册追索|仅属于甲书的迟到优化响应/);
      assert.equal(await refineInput().inputValue(),'');
      await capture('07-project-switch');
      return {new_project:902,new_project_candidates:1,old_response_ignored:true};
    });

    await check('Locking the chosen version sends its stable identity and only then changes the formal outline', async () => {
      await page.getByLabel('当前创作',{exact:true}).selectOption('901'); await page.waitForURL('**/project/901/chat');
      await page.getByRole('link',{name:'大纲规划',exact:true}).click(); await page.waitForURL('**/project/901/outline'); await waitCards(8);
      assertBodyBeforeLock();
      const card = namedCard('账册追索·优化1·优化2');
      await card.getByRole('button',{name:'锁定这个候选',exact:true}).click();
      await page.getByText('锁定候选：账册追索·优化1·优化2',{exact:true}).waitFor();
      const request = state.requests.filter(request => request.path === '/projects/901/outline/lock').at(-1);
      assert.equal(request.body.candidate_id,'outline-901-refined-2'); assert.equal(request.body.index,undefined);
      assert.equal(state.outlines[901].content,state.outlines[901].candidates.find(candidate=>candidate.id==='outline-901-refined-2').content);
      await capture('08-locked-version'); return {candidate_id:request.body.candidate_id,explicit_lock_only:true};
    });

    assert.deepEqual(report.page_errors,[]);
    assert.deepEqual(report.console_errors.filter(error => !/503/.test(error)),[]);
    assert.deepEqual(state.unknown,[]); assert.deepEqual(report.unknown_api,[]); assert.deepEqual(report.unknown_network,[]);
    report.status = 'passed';
  } catch (error) {
    report.status = 'failed'; report.failure = String(error.stack || error);
    report.last_visible_text = (await page.locator('main').innerText().catch(() => '')).slice(-10000);
    await capture('failure').catch(() => {});
  } finally {
    for (const hold of gates) hold.release();
    report.finished_at = new Date().toISOString(); report.api_requests = clone(state.requests); report.unknown_api.push(...state.unknown);
    report.counts = {passed:report.checks.filter(check=>check.status==='passed').length,failed:report.checks.filter(check=>check.status==='failed').length,
      screenshots:report.screenshots.length,page_errors:report.page_errors.length,unexpected_console_errors:report.console_errors.filter(error=>!/503/.test(error)).length,
      simulated_failure_console_errors:report.console_errors.filter(error=>/503/.test(error)).length,unknown_api:report.unknown_api.length,unknown_network:report.unknown_network.length,api_requests:report.api_requests.length};
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2),'utf8');
  }
  return {status:report.status,counts:report.counts,failure:report.failure,report:path.join(output,'report.json'),screenshots:report.screenshots.map(item=>item.path)};
}

module.exports = {runAcceptance};
