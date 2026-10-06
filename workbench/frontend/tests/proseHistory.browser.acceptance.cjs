/* Real Chromium renders production dist against synthetic, in-memory APIs.
 * Every network request is fulfilled or aborted; no server, model or book I/O.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { chromium } = require(process.env.WORKBENCH_PLAYWRIGHT || 'C:/Users/30332/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const { createFixtureState, installFixtures } = require('./workbench.browser.fixtures.cjs');

const root = path.resolve(__dirname, '../../..');
const dist = path.resolve(root, process.env.ACCEPTANCE_DIST || process.env.WORKBENCH_ACCEPTANCE_DIST || 'workbench/frontend/dist');
const output = path.join(root, '.workbench/logs/prose-optimization/third-round-browser');
const origin = 'http://127.0.0.1:8790';
const chapterPath = '章节/第0004章.txt';
const paragraph = '林舟合上账册，把那枚铜钱推回柜台。柜台下落着一层细灰，铜钱滚过的地方留下短短一道印子。他按住账册的封皮，等掌柜先开口。门外的挑担人没有进店，担子靠在石阶旁，那根扁担上的红布条被风吹得贴住了门板。';
const sentence = '掌柜没有接话，手指在印章边缘停了一会儿，直到门口那个挑担人转身离开，他才把账册往自己这一边拖了半寸。';
const candidate = '桥下的水声传进账房。\n林舟翻到账册末页。\n' + paragraph + '\n他把右手收进袖口。\n' + sentence;
const hash = text => crypto.createHash('sha256').update(text, 'utf8').digest('hex');
const sources = [
  { rel_path: '章节/第0002章.txt', line: 6, column: 2, end_line: 6, end_column: paragraph.length + 1,
    content_hash: hash('先前正文。\n'.repeat(5) + ' ' + paragraph), text: paragraph, text_truncated: false },
  { rel_path: '章节/第0001章.txt', line: 8, column: 1, end_line: 8, end_column: sentence.length,
    content_hash: hash('更早正文。\n'.repeat(7) + sentence), text: sentence, text_truncated: false },
];
const report = {
  started_at: new Date().toISOString(), origin, production_assets: dist,
  isolation: 'Production files fulfilled in browser routes; APIs synthetic; unknown requests aborted; service workers blocked; no listener or real backend connection.',
  checks: [], screenshots: [], page_errors: [], console_errors: [], unknown_api: [], unknown_network: [], api_requests: [],
  limitations: ['Chromium UI only; no real model call, backend diagnosis or manuscript write is exercised.'],
};
let browser;
fs.mkdirSync(output, { recursive: true });

function historyReport(patch = {}) {
  return {
    version: 1, blocking: false, status: 'available', findings: [
      { kind: 'cross_chapter_paragraph', severity: 'warning', line: 3, column: 1, end_line: 3, end_column: paragraph.length,
        text: paragraph, text_truncated: false, related_sources: [sources[0]],
        reason: '本候选段落与此前章节的段落原文完全相同。', suggestion: '请结合场景判断是否有意复现。' },
      { kind: 'cross_chapter_sentence', severity: 'warning', line: 5, column: 1, end_line: 5, end_column: sentence.length,
        text: sentence, text_truncated: false, related_sources: [sources[1]],
        reason: '本候选长句与此前章节的句子原文完全相同。', suggestion: '检查这句是否仍在推进当下场景。' },
    ], counters: { reference_sources: 2, repeated_paragraphs: 1, repeated_sentences: 1, reported_findings: 2 },
    truncated: false, sources: sources.map(({rel_path,content_hash}) => ({rel_path,content_hash})),
    reference_hash: hash(JSON.stringify(sources)), excluded: [], degradation: [], ...patch,
  };
}

function reviewResult(history = historyReport()) {
  const result = {
    review_id: 501, rel_path: chapterPath, verdict: '通过',
    hard_gates: { passed: true, words: 2450, gates: [
      {key:'字数门',passed:true,blocking:false,detail:'正文 2450 字'},
      {key:'语言门',passed:true,blocking:false,detail:'中文正文'},
    ], locations: [], blocking_gates: [] },
    items: [{项:'核对账册来源',判定:'已完成',证据:'林舟核对了账册上的日期。'}],
    consistency: [], revise_instructions: [], counts: {已完成:1,未完成:0,待核实:0}, ai_used: true,
    candidate_hash: hash(candidate), contract_hash: hash('合成合同'),
  };
  if (history !== undefined) result.prose_history = history;
  return result;
}

function fixtureState(result = reviewResult()) {
  const state = createFixtureState();
  Object.assign(state.chapters[901][0], {rel_path:chapterPath,file_name:'第0004章.txt',number:4,title:'账房核对'});
  state.overrides['POST /projects/901/review'] = result;
  return state;
}

function allowedAPI(method, apiPath) {
  if (method === 'GET' && ['/settings','/projects','/proposals','/proposals/count','/providers','/agents','/engines','/chat/sessions','/chat/quick-cards'].includes(apiPath)) return true;
  if (method === 'GET' && /^\/projects\/901(?:\/(?:chapters(?:\/content)?|tree|quality-matrix|debts|style\/fingerprints|chat-defaults|writing-prefs))?$/.test(apiPath)) return true;
  if (method === 'POST' && apiPath === '/projects/901/review') return true;
  return apiPath === '/proposals/1101' && ['GET','PUT'].includes(method)
    || apiPath === '/proposals/1101/review' && method === 'POST';
}

async function newPage(state = fixtureState()) {
  const context = await browser.newContext({viewport:{width:1440,height:1100},reducedMotion:'reduce',serviceWorkers:'block'});
  const page = await context.newPage(); page.setDefaultTimeout(7000);
  const mime = {'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.jpg':'image/jpeg','.woff2':'font/woff2','.ico':'image/x-icon'};
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin || url.pathname.startsWith('/api/')) {
      report.unknown_network.push({method:route.request().method(),url:url.href}); return route.abort();
    }
    const requested = path.resolve(dist,decodeURIComponent(url.pathname).replace(/^\/+/,''));
    if (requested !== dist && !requested.startsWith(dist + path.sep)) {
      report.unknown_network.push({url:url.href,reason:'asset path outside production dist'}); return route.abort();
    }
    const file = fs.existsSync(requested) && fs.statSync(requested).isFile() ? requested
      : ['/','/inbox','/project/901/review'].includes(url.pathname) ? path.join(dist,'index.html') : null;
    if (!file) {report.unknown_network.push({url:url.href,reason:'unknown production asset'}); return route.abort();}
    return route.fulfill({status:200,contentType:mime[path.extname(file)] || 'application/octet-stream',body:fs.readFileSync(file)});
  });
  await installFixtures(page,{state,handle:async ({route,request,path:apiPath,method}) => {
    if (new URL(request.url()).origin !== origin || !allowedAPI(method,apiPath)) {
      state.unknown.push({method,path:apiPath}); await route.abort(); return true;
    }
    return false;
  }});
  page.on('pageerror',error => report.page_errors.push({url:page.url(),error:error.message}));
  page.on('console',message => {if (message.type() === 'error') report.console_errors.push({url:page.url(),text:message.text()});});
  return {page,state,context};
}

async function close(fixture) {
  report.unknown_api.push(...fixture.state.unknown); report.api_requests.push(...fixture.state.requests);
  await fixture.context.close();
}
async function openReview(page) {
  await page.goto(origin + '/project/901/review',{waitUntil:'domcontentloaded'});
  await page.getByRole('heading',{name:'审稿中心',exact:true}).waitFor();
  await page.getByRole('button',{name:'开始逐项审稿',exact:true}).click();
  await page.getByRole('heading',{name:'审稿结论',exact:true}).waitFor();
}
async function screenshot(page,name) {
  const filename = name + '.png';
  await page.screenshot({path:path.join(output,filename),fullPage:true,animations:'disabled',timeout:15000});
  report.screenshots.push({filename,url:page.url(),viewport:page.viewportSize()});
}
async function check(name,body) {
  const started = Date.now();
  try {const details = await body(); report.checks.push({name,status:'passed',duration_ms:Date.now()-started,...details}); console.log('PASS ' + name);}
  catch (error) {report.checks.push({name,status:'failed',duration_ms:Date.now()-started,error:String(error.stack || error)}); console.log('FAIL ' + name + ': ' + error.message);}
}

(async () => {
  assert.ok(dist.startsWith(root + path.sep),'Production dist must be in this workspace');
  assert.ok(fs.existsSync(path.join(dist,'index.html')),'Existing dist is required; no dependencies are installed');
  report.index_sha256 = hash(fs.readFileSync(path.join(dist,'index.html')));
  browser = await chromium.launch({channel:'chrome',headless:true}); report.browser_version = browser.version();

  await check('Review cross-chapter evidence has current and earlier positions without blocking acceptance',async () => {
    const fixture = await newPage(); const {page} = fixture;
    try {
      await openReview(page);
      const advice = page.locator('section[aria-label="表达建议（供作者判断）"]');
      await advice.getByText('跨章段落重复',{exact:true}).waitFor();
      assert.equal(await advice.getByText('跨章句子重复',{exact:true}).count(),1);
      assert.match(await advice.innerText(),/本候选 · 第 3 行，第 1—/);
      assert.match(await advice.innerText(),/章节\/第0002章.txt · 第 6 行，第 2—/);
      assert.match(await advice.innerText(),/章节\/第0001章.txt · 第 8 行，第 1—/);
      assert.match(await advice.innerText(),/来源原文：/);
      assert.match(await advice.innerText(),/不影响合同与硬门禁判定/);
      assert.match(await advice.innerText(),/检查范围不代表全书/);
      const sourceDetails = advice.locator('[aria-label="此前来源（仅供核对）"]');
      assert.equal(await sourceDetails.getByRole('button').count(),0);
      assert.equal(await sourceDetails.getByRole('link').count(),0);
      await sourceDetails.first().getByText('来源文本摘要（SHA-256）',{exact:true}).click();
      assert.match(await sourceDetails.first().innerText(),new RegExp(sources[0].content_hash));
      assert.equal(await page.getByText('硬门禁 通过',{exact:true}).count(),1);
      assert.equal(await page.getByRole('heading',{name:/需要处理/}).count(),0);
      assert.doesNotMatch(await advice.innerText(),/无需修改|全书无重复|全书没有问题/);
      await screenshot(page,'review-history-nonblocking');
      await advice.scrollIntoViewIfNeeded(); await screenshot(page,'review-history-evidence');
      return {earlier_sources:2,read_only:true,acceptance:'通过',history_nonblocking:true};
    } finally {await close(fixture);}
  });

  await check('Legacy review with all expression fields absent stays compatible',async () => {
    const result = reviewResult(); delete result.prose_history;
    const fixture = await newPage(fixtureState(result)); const {page} = fixture;
    try {
      await openReview(page);
      assert.equal(await page.locator('section[aria-label="表达建议（供作者判断）"]').count(),0);
      assert.equal(await page.getByText('硬门禁 通过',{exact:true}).count(),1);
      await screenshot(page,'review-history-legacy'); return {missing_optional_fields:true,acceptance:'通过'};
    } finally {await close(fixture);}
  });

  await check('Unchecked history explains that no cross-chapter conclusion was made',async () => {
    const fixture = await newPage(fixtureState(reviewResult(historyReport({status:'not_checked',sources:[],findings:[]}))));
    const {page} = fixture;
    try {
      await openReview(page); const advice = page.locator('section[aria-label="表达建议（供作者判断）"]');
      await advice.getByText('本次未检查跨章重复，不能据此判断历史正文是否重复。',{exact:true}).waitFor();
      assert.equal(await advice.getByText('跨章段落重复',{exact:true}).count(),0);
      assert.equal(await page.getByText('硬门禁 通过',{exact:true}).count(),1);
      await advice.scrollIntoViewIfNeeded(); await screenshot(page,'review-history-not-checked');
      return {unchecked_explained:true,acceptance:'通过'};
    } finally {await close(fixture);}
  });

  await check('Degraded and truncated history shows omitted sources without claiming complete coverage',async () => {
    const history = historyReport(); history.status = 'degraded'; history.truncated = true;
    history.findings = [history.findings[0]];
    history.findings[0].related_sources = [{...sources[0],text:paragraph.slice(0,45),text_truncated:true}];
    history.sources = [history.sources[0]];
    history.excluded = [{rel_path:'章节/第0001章.txt',reason:'来源正文读取失败'}];
    history.degradation = ['此前正文读取范围已达本次上限。'];
    const fixture = await newPage(fixtureState(reviewResult(history))); const {page} = fixture;
    try {
      await openReview(page); const advice = page.locator('section[aria-label="表达建议（供作者判断）"]');
      await advice.getByText('跨章检查范围不完整，以下结果仅来自可用的此前章节正文。',{exact:true}).waitFor();
      assert.match(await advice.innerText(),/报告已截断，只展示部分发现/);
      assert.match(await advice.innerText(),/来源片段已截短/);
      await advice.getByText('跨章检查限制',{exact:true}).click();
      await advice.getByText('未纳入的历史来源（1）',{exact:true}).click();
      assert.match(await advice.innerText(),/来源正文读取失败/);
      assert.match(await advice.innerText(),/此前正文读取范围已达本次上限/);
      assert.doesNotMatch(await advice.innerText(),/全书无重复|全书没有问题|无需修改/);
      await advice.scrollIntoViewIfNeeded(); await screenshot(page,'review-history-partial');
      return {degraded_explained:true,excluded_sources:1,truncated:true};
    } finally {await close(fixture);}
  });

  await check('Inbox review keeps cross-chapter suggestions nonblocking and editing marks old evidence',async () => {
    const state = fixtureState();
    state.proposals = [{id:1101,project_id:901,task_id:301,kind:'chapter_draft',title:'跨章表达验收候选',target_path:chapterPath,
      status:'pending',created_at:'2026-10-03 10:00:00',meta:{pipeline_candidate:{protocol:2,candidate_hash:hash(candidate)}},
      content:candidate,chars:candidate.length,is_new_file:false,diff:[{type:'add',text:paragraph,line:3}]}];
    state.overrides['POST /proposals/1101/review'] = () => {
      state.proposals[0].meta.pipeline_review = {verdict:'通过',ai_used:true,review_id:501}; return reviewResult();
    };
    const fixture = await newPage(state); const {page} = fixture;
    try {
      await page.goto(origin + '/inbox',{waitUntil:'domcontentloaded'});
      await page.getByRole('heading',{name:'收件箱',exact:true}).waitFor();
      await page.getByRole('button',{name:'查看',exact:true}).click();
      const drawer = page.getByRole('dialog',{name:'提案 #1101 · 跨章表达验收候选',exact:true});
      await drawer.getByRole('button',{name:'审查当前候选',exact:true}).click();
      const advice = drawer.locator('section[aria-label="表达建议（供作者判断）"]');
      await advice.getByText('跨章段落重复',{exact:true}).waitFor();
      assert.match(await advice.innerText(),/章节\/第0002章.txt · 第 6 行/);
      assert.equal(await drawer.getByRole('button',{name:'应用',exact:true}).isEnabled(),true);
      await advice.scrollIntoViewIfNeeded(); await screenshot(page,'inbox-history-reviewed');
      await drawer.getByLabel('候选正文（修改后需重新审查）',{exact:true}).fill('作者修改后的候选正文。');
      await advice.getByText('正文已修改，以下建议对应修改前的候选。重新审查后才能核对当前正文的位置。',{exact:true}).waitFor();
      assert.equal(await drawer.getByRole('button',{name:'应用',exact:true}).isEnabled(),false);
      assert.equal(state.proposals[0].status,'pending');
      assert.equal(state.requests.filter(request => /\/apply$/.test(request.path)).length,0);
      await advice.scrollIntoViewIfNeeded(); await screenshot(page,'inbox-history-edited-stale');
      return {reviewed_candidate_apply_enabled:true,edited_candidate_apply_enabled:false,automatic_apply_requests:0};
    } finally {await close(fixture);}
  });

  assert.deepEqual(report.page_errors,[]); assert.deepEqual(report.console_errors,[]);
  assert.deepEqual(report.unknown_api,[]); assert.deepEqual(report.unknown_network,[]);
  assert.equal(report.checks.filter(check => check.status === 'failed').length,0);
})().catch(error => {report.fatal = String(error.stack || error); console.error(report.fatal); process.exitCode = 1;})
  .finally(async () => {
    report.finished_at = new Date().toISOString();
    report.counts = {passed:report.checks.filter(check => check.status === 'passed').length,failed:report.checks.filter(check => check.status === 'failed').length,
      screenshots:report.screenshots.length,page_errors:report.page_errors.length,console_errors:report.console_errors.length,
      unknown_api:report.unknown_api.length,unknown_network:report.unknown_network.length,api_requests:report.api_requests.length};
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2),'utf8');
    if (browser) await browser.close(); console.log(JSON.stringify(report.counts));
  });
