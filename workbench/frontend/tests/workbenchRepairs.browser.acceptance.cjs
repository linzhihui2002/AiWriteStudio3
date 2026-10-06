/* Production Chromium UI with isolated, synthetic APIs. No backend/model writes. */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.WORKBENCH_PLAYWRIGHT || 'C:/Users/30332/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {createFixtureState, installFixtures, chapterPath, materialPath} = require('./workbench.browser.fixtures.cjs');
const root = path.resolve(__dirname, '../../..');
const dist = path.join(root, 'workbench/frontend/dist');
const output = path.join(root, '.workbench/logs/workbench-repairs/browser');
const origin = 'http://127.0.0.1:8790';
const report = {started_at:new Date().toISOString(), isolation:'All assets fulfilled from production dist; APIs synthetic; no listener/backend/model/author-file access.', checks:[], screenshots:[], page_errors:[], unknown_api:[], unknown_network:[]};
fs.mkdirSync(output,{recursive:true});
let browser;
const pause = ms => new Promise(resolve => setTimeout(resolve,ms));
const count = (state, method, tail) => state.requests.filter(r=>r.method===method&&r.path.endsWith(tail)).length;
async function check(name, body) {try {await body(); report.checks.push({name,status:'passed'}); console.log('PASS '+name)} catch(error) {report.checks.push({name,status:'failed',error:String(error.stack||error)}); console.log('FAIL '+name+': '+error.message)}}
async function fixture(viewport={width:1440,height:1000}, theme='light') {
 const state=createFixtureState(); state.settings.editor.autosave_seconds=0;
 for(const id of [901,902]) {state.chapters[id][0].status='草稿'; state.tree[id].groups[0].nodes[0].status='草稿'}
 const page=await browser.newPage({viewport,reducedMotion:'reduce',serviceWorkers:'block'}); page.setDefaultTimeout(8000);
 await page.addInitScript(value=>localStorage.setItem('workbench-theme',value),theme);
 state.settings.appearance.theme=theme;
 await page.route('**/*',async route=>{
  const url=new URL(route.request().url());
  if(url.origin!==origin||url.pathname.startsWith('/api/')) {report.unknown_network.push(url.href); return route.abort()}
  const requested=path.resolve(dist,decodeURIComponent(url.pathname).replace(/^\/+/,''));
  if(requested!==dist&&!requested.startsWith(dist+path.sep))return route.abort();
  const file=fs.existsSync(requested)&&fs.statSync(requested).isFile()?requested:!path.extname(requested)?path.join(dist,'index.html'):null;
  if(!file)return route.abort();
  const mime={'.js':'application/javascript','.css':'text/css','.html':'text/html','.svg':'image/svg+xml','.woff2':'font/woff2','.png':'image/png','.ico':'image/x-icon'};
  return route.fulfill({status:200,contentType:mime[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
 });
 await installFixtures(page,{state}); page.on('pageerror',err=>report.page_errors.push(String(err)));
 page.on('close',()=>report.unknown_api.push(...state.unknown));
 return {page,state};
}
async function open(page,route,heading) {await page.goto(origin+route,{waitUntil:'domcontentloaded'}); if(heading)await page.getByRole('heading',{name:heading,exact:true}).first().waitFor(); await pause(150)}
async function shot(page,name) {await page.screenshot({path:path.join(output,name),fullPage:true}); report.screenshots.push(name)}
function soft(hash='fixture-hash') {return {source_hash:hash,candidate_hash:hash,task_id:600,ai_used:true,ai_error:'',source_changed:false,rejected_suggestions:[],style_comparison:null,suggestions:[{line:1,original:'林舟推开门',replacement:'林舟推门进屋',issue:'动作表达'}],proposal_ids:[1101]}}
function ingest() {return {project_id:901,chapter_rel:chapterPath,source_hash:'fixture-hash',run_id:'fixture-ingest',summary:'林舟出门核对日期。',ai_used:true,replayed:false,timeline_added:1,ledger_added:0,foreshadow_added:1,memory_added:0,state_changes_detected:1,proposals_created:[1101],counts:{timeline:{added:1,updated:0,skipped:0,rejected:0}},warnings:[],rejected:[]}}
(async()=>{
 browser=await chromium.launch({channel:'chrome',headless:true});
 await check('chapter status from material and unsaved editor retains body and base',async()=>{
  const {page,state}=await fixture({width:1920,height:1000});
  state.overrides['PATCH /projects/901/chapters']=async({body})=>{state.chapters[901][0].status=body.status; state.tree[901].groups[0].nodes[0].status=body.status;return {...state.chapters[901][0],hash:'fixture-hash'}};
  try {await open(page,'/project/901/editor?file='+encodeURIComponent(materialPath));
   const status=page.locator('.tree-node__status').first(); await status.selectOption('完成'); await pause(160);
   assert.equal(count(state,'PATCH','/chapters'),1); assert.equal(state.chapters[901][0].status,'完成');
   await page.getByLabel('选择章节',{exact:true}).selectOption(chapterPath);
   const body=page.locator('.cm-content'); await body.waitFor(); await body.fill('作者正在编辑的新正文，尚未保存。');
   await status.selectOption('发表'); await pause(200); assert.equal(await body.innerText(),'作者正在编辑的新正文，尚未保存。');
   assert.equal(count(state,'PUT','/chapters/content'),0); assert.equal(await page.locator('.editor-save-state').innerText(),'未保存');
  } finally {await page.close()}
 });
 await check('three result modes switch without execution and pending result stays in own mode',async()=>{
  const {page,state}=await fixture(); state.overrides['POST /projects/901/review/deslop']=soft();state.delays['POST /projects/901/review/deslop']=350;
  state.overrides['POST /projects/901/ingest']=ingest();
  try {await open(page,'/project/901/review','审稿中心');
   await page.getByRole('button',{name:'开始逐项审稿',exact:true}).click(); await page.getByRole('heading',{name:'审稿结论',exact:true}).waitFor();
   await page.getByRole('button',{name:'去AI味软审',exact:true}).click(); await page.getByText('检查本章表达',{exact:true}).waitFor(); assert.equal(count(state,'POST','/review/deslop'),0);
   await page.getByRole('button',{name:'开始去AI味软审',exact:true}).click(); await page.getByRole('button',{name:'提取四件套',exact:true}).click();
   await page.getByText('整理本章四件套',{exact:true}).waitFor(); await pause(450); assert.equal(await page.getByRole('heading',{name:'去AI味软审结果',exact:true}).count(),0);
   await page.getByRole('button',{name:'去AI味软审',exact:true}).click(); await page.getByRole('heading',{name:'去AI味软审结果',exact:true}).waitFor(); assert.equal(count(state,'POST','/review/deslop'),1);
   await page.getByRole('button',{name:'提取四件套',exact:true}).click(); await page.getByRole('button',{name:'开始提取四件套',exact:true}).click(); await page.getByRole('heading',{name:'四件套提取结果',exact:true}).waitFor();
   assert.equal(count(state,'POST','/ingest'),1); assert.equal(await page.getByRole('heading',{name:'审稿结论',exact:true}).count(),0);
   await page.getByRole('button',{name:'逐项审稿',exact:true}).click(); await page.getByRole('heading',{name:'审稿结论',exact:true}).waitFor();assert.equal(count(state,'POST','/review'),1);
   await shot(page,'review-results-desktop.png');
  } finally {await page.close()}
 });
 await check('style sample updates one row, loads note, and cancellation immediately removes reference',async()=>{
  const {page,state}=await fixture(); let sample={id:71,rel_path:chapterPath,approved:true,usable:true,note:'保留作者备注',han:2450,created_at:'2026-10-04',sentence_len_mean:20,sentence_len_cv:.4,dialog_ratio:.3,dash_per_1000:0};
  state.overrides['GET /projects/901/style/fingerprints']=()=>({samples:[sample],reference:sample.usable?{samples:1,sentence_len_mean:20}:null});
  state.overrides['POST /projects/901/style/sample']=({body})=>{sample={...sample,approved:true,usable:true,...(body.note!==undefined?{note:body.note}:{})};return sample};
  state.overrides['PATCH /projects/901/style/fingerprints/71']=()=>{sample={...sample,approved:false,usable:false};return sample};
  try {await open(page,'/project/901/review','审稿中心'); await page.getByRole('button',{name:'文风档案',exact:true}).click();
   const note=page.locator('#style-note');assert.equal(await note.inputValue(),'保留作者备注');
   await page.getByRole('button',{name:'采样并认可当前正文',exact:true}).click(); await pause(170);assert.equal(await page.locator('tbody tr').count(),1);
   await page.getByRole('button',{name:'取消认可',exact:true}).click();await page.getByText('未认可',{exact:true}).waitFor();assert.equal(sample.usable,false);
   assert.equal(await page.getByText('1 章',{exact:true}).count(),0);assert.equal(sample.note,'保留作者备注');
  } finally {await page.close()}
 });
 await check('synchronous double click creates only one soft review request and retry distinguishes provider error from empty advice',async()=>{
  const {page,state}=await fixture();state.delays['POST /projects/901/review/deslop']=300;
  state.overrides['POST /projects/901/review/deslop']={...soft(),suggestions:[],proposal_ids:[],ai_used:false,ai_error_code:'SERVER_ERROR',ai_error:'模型服务暂不可用（503）'};
  try {await open(page,'/project/901/review','审稿中心');await page.getByRole('button',{name:'去AI味软审',exact:true}).click();
   const run=page.getByRole('button',{name:'开始去AI味软审',exact:true});await run.evaluate(button=>{button.click();button.click()});
   await page.getByRole('heading',{name:'去AI味软审结果',exact:true}).waitFor();assert.equal(count(state,'POST','/review/deslop'),1);
   await page.locator('[aria-label="去AI味软审结果"]').getByText('模型服务暂不可用（503）',{exact:true}).waitFor();
   state.overrides['POST /projects/901/review/deslop']={...soft(),suggestions:[],proposal_ids:[]};await run.click();
   await page.getByLabel('去AI味软审结果',{exact:true}).getByText('本次软审已完成，未提出表达修改建议。',{exact:true}).waitFor();assert.equal(count(state,'POST','/review/deslop'),2);
  } finally {await page.close()}
 });
 await check('changed body marks stored soft report stale and blocks old evidence navigation',async()=>{
  const {page,state}=await fixture();state.overrides['POST /projects/901/review/deslop']=soft();
  try {await open(page,'/project/901/review','审稿中心');await page.getByRole('button',{name:'去AI味软审',exact:true}).click();await page.getByRole('button',{name:'开始去AI味软审',exact:true}).click();
   await page.getByRole('heading',{name:'去AI味软审结果',exact:true}).waitFor();
   state.overrides['GET /projects/901/chapters/content']={...state.chapters[901][0],hash:'changed-source',body:'作者新改稿。',content:'作者新改稿。'};
   await page.getByRole('button',{name:'刷新质量记录',exact:true}).click();await page.getByText(/正文已变化，以下原文位置对应旧版本/).waitFor();
   assert.equal(await page.getByRole('button',{name:'定位章节原文',exact:true}).isDisabled(),true);assert.equal(count(state,'POST','/review/deslop'),1);
  } finally {await page.close()}
 });
 await check('card rename and delete send explicit edits with captured source version',async()=>{
  const {page,state}=await fixture();state.overrides['PATCH /projects/901/cards']=({body})=>{
   const card=state.cards[901][0];for(const edit of body.field_edits){if(edit.delete)delete card.fields[edit.original_name];else {delete card.fields[edit.original_name];card.fields[edit.new_name||edit.original_name]=edit.value}}return {ok:true};
  };
  try {await open(page,'/project/901/cards','设定卡片');await page.getByRole('button',{name:'编辑',exact:true}).click();
   const form=page.locator('fieldset.cards-edit-form');await form.getByPlaceholder('字段名',{exact:true}).first().fill('职务');await form.getByRole('button',{name:'删除',exact:true}).last().click();
   await form.getByRole('button',{name:'保存',exact:true}).click();await pause(250);
   const request=state.requests.find(r=>r.method==='PATCH'&&r.path==='/projects/901/cards');assert.equal(request.body.expected_hash,'fixture-hash');
   assert.ok(request.body.field_edits.some(edit=>edit.original_name==='身份'&&edit.new_name==='职务'));assert.ok(request.body.field_edits.some(edit=>edit.original_name==='目标'&&edit.delete));
   assert.deepEqual(state.cards[901][0].fields,{职务:'记录者'});
  } finally {await page.close()}
 });
 await check('inbox explicit state reproposal opens new complete diff and never applies it',async()=>{
  const {page,state}=await fixture();const original=state.proposals[0];original.kind='state';original.target_path='状态/角色状态.md';original.meta={ingest:true,ingestion:{run_id:'fixture-ingest'},state_changes:[{角色:'林舟',字段:'位置',新值:'南桥'}]};
  state.overrides['POST /proposals/1001/rebase-state']=()=>{original.status='discarded';const next={...original,id:1003,status:'pending',title:'重提角色状态',content:'## 林舟\n- 位置：南桥\n\n## 作者新角色\n- 位置：茶馆\n',diff:[{type:'add',line:2,text:'- 位置：南桥'}]};state.proposals.push(next);return {proposal_id:1003,already_current:false,proposal:next}};
  try {await open(page,'/inbox','收件箱');await page.getByRole('button',{name:'查看',exact:true}).first().click();
   const candidate=page.getByRole('dialog').locator('textarea');await candidate.fill('作者正在修改候选，尚未保存。');await page.getByRole('button',{name:'按当前状态重提',exact:true}).click();
   assert.equal(count(state,'POST','/rebase-state'),0);assert.equal(await candidate.inputValue(),'作者正在修改候选，尚未保存。');await candidate.fill(original.content);
   await page.getByRole('button',{name:'按当前状态重提',exact:true}).click();
   await page.getByRole('dialog',{name:'提案 #1003 · 重提角色状态',exact:true}).waitFor();await page.getByRole('dialog').locator('textarea').waitFor();
   assert.match(await page.getByRole('dialog').locator('textarea').inputValue(),/作者新角色/);assert.equal(count(state,'POST','/rebase-state'),1);assert.equal(count(state,'POST','/apply'),0);
  }finally {await page.close()}
 });
 await check('outline save snapshot preserves typing while request runs and next save includes it',async()=>{
  const {page,state}=await fixture();state.delays['PUT /projects/901/outline']=350;
  try {await open(page,'/project/901/outline','大纲规划');const input=page.locator('.workspace-page textarea').last(); await input.fill('提交时的大纲');
   await page.getByRole('button',{name:'保存',exact:true}).click(); await input.fill('请求期间新增的大纲文字'); await pause(550);
   assert.equal(await input.inputValue(),'请求期间新增的大纲文字');assert.equal(state.outlines[901].content,'提交时的大纲');
   await page.getByRole('button',{name:'保存',exact:true}).click();await pause(550);assert.equal(state.outlines[901].content,'请求期间新增的大纲文字');
  } finally {await page.close()}
 });
 await check('bible delayed source marking cannot load prior entities into the newly selected category',async()=>{
  const {page,state}=await fixture();state.delays['POST /projects/901/bible/source']=350;state.overrides['POST /projects/901/bible/source']={ok:true};
  state.overrides['GET /projects/901/bible/world']={kind:'world',label:'世界',entities:[{...state.entities[901][0],name:'海港世界',ref:'world:海港世界'}],count:1};
  try {await open(page,'/project/901/bible','设定资料');await page.getByRole('button',{name:'标为作者',exact:true}).first().click();await page.getByRole('button',{name:'世界',exact:true}).click();
   await page.getByText('海港世界',{exact:true}).waitFor();await pause(550);assert.equal(await page.getByText('海港世界',{exact:true}).count(),1);
   const paths=state.requests.filter(r=>r.method==='GET'&&r.path.startsWith('/projects/901/bible/')).map(r=>r.path);assert.equal(paths.at(-1),'/projects/901/bible/world');
  }finally {await page.close()}
 });
 await check('late review response cannot populate another book or release its task',async()=>{
  const {page,state}=await fixture();state.delays['POST /projects/901/review/deslop']=600;state.overrides['POST /projects/901/review/deslop']=soft();
  try {await open(page,'/project/901/review','审稿中心');await page.getByRole('button',{name:'去AI味软审',exact:true}).click();await page.getByRole('button',{name:'开始去AI味软审',exact:true}).click();
   await page.locator('#workspace-book').selectOption('902');await page.waitForURL('**/project/902/**');await page.getByRole('link',{name:'审稿中心',exact:true}).click();await pause(750);
   assert.equal(await page.getByText('林舟推开门',{exact:true}).count(),0);assert.equal(count(state,'POST','/review/deslop'),1);
  } finally {await page.close()}
 });
 for(const theme of ['light','dark','paper'])for(const width of [1920,1440,1024,390])await check('layout/theme/keyboard '+theme+' '+width,async()=>{
  const {page}=await fixture({width,height:1000},theme);
  try {for(const [route,heading] of [['review','审稿中心'],['bible','设定资料']]){
    await open(page,'/project/901/'+route,heading);const overflow=await page.evaluate(()=>({doc:document.documentElement.scrollWidth,viewport:innerWidth}));assert.ok(overflow.doc<=overflow.viewport+1,JSON.stringify(overflow));
    assert.equal(await page.locator('html').getAttribute('data-theme'),theme);
    if(route==='review'){const tab=page.getByRole('button',{name:'逐项审稿',exact:true});await tab.focus();await page.keyboard.press('Tab');await page.keyboard.press('Enter');assert.equal(await page.getByRole('button',{name:'去AI味软审',exact:true}).getAttribute('aria-pressed'),'true');}
    if(width===390||width===1440)await shot(page,route+'-'+theme+'-'+width+'.png');
   }}finally {await page.close()}
 });
 await browser.close();report.finished_at=new Date().toISOString();report.passed=report.checks.filter(c=>c.status==='passed').length;report.failed=report.checks.length-report.passed;
 fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,failed:report.failed,page_errors:report.page_errors,unknown_api:report.unknown_api,unknown_network:report.unknown_network}));if(report.failed||report.page_errors.length||report.unknown_api.length||report.unknown_network.length)process.exitCode=1;
})().catch(async error=>{console.error(error);if(browser)await browser.close();process.exitCode=1});
