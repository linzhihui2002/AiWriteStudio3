/* Targeted Tabbit production UI acceptance. Call runAcceptance({ page }) from a
 * task-owned page. All APIs are synthetic and unknown requests are aborted.
 */
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const {createFixtureState, installFixtures} = require('./workbench.browser.fixtures.cjs');
const root = path.resolve(__dirname, '../../..');
const origin = 'http://127.0.0.1:8790';
const clone = value => JSON.parse(JSON.stringify(value));
function gate() { let release; const pending = new Promise(resolve => {release = resolve}); return {pending,release}; }

async function runAcceptance(options) {
  const page = options.page;
  const dist = path.resolve(root, options.dist || 'workbench/frontend/dist');
  const output = path.resolve(root, options.output || '.workbench/logs/outline-freeze/browser');
  assert.ok(dist.startsWith(root + path.sep)); assert.ok(fs.existsSync(path.join(dist,'index.html')));
  fs.mkdirSync(output,{recursive:true});
  const report = {started_at:new Date().toISOString(),origin,production_assets:dist,
    index_sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(dist,'index.html'))).digest('hex'),
    isolation:'Production assets fulfilled from dist, in-memory APIs, unknown network aborted; no real backend, model or manuscript access.',
    checks:[],screenshots:[],page_errors:[],console_errors:[],dialogs:[],unknown_api:[],unknown_network:[],api_requests:[]};
  const state = createFixtureState();
  for (const id of [901,902]) {state.outlines[id].candidates=[];state.outlines[id].rolling_window=2;}
  state.outlines[902].frozen=true;
  let freezeGate=null, unfreezeGate=null, failUnfreeze=false, storageBefore=null;
  const gates=[];
  const original=state.outlines[901].content;
  const edited='作者保留的未保存修改：林舟从桥边旧账查起，卷末找到失踪记账人。';
  const mime={'.html':'text/html','.js':'application/javascript','.css':'text/css','.svg':'image/svg+xml','.png':'image/png','.ico':'image/x-icon','.woff2':'font/woff2'};
  page.setDefaultTimeout(7000); await page.setViewportSize({width:1440,height:1000});
  await page.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin||url.pathname.startsWith('/api/')){report.unknown_network.push({url:url.href});return route.abort();}
    if(url.pathname==='/__outline-freeze-fixture__')return route.fulfill({status:200,contentType:'text/html',body:'<!doctype html><html><body>合成验收环境</body></html>'});
    const requested=path.resolve(dist,decodeURIComponent(url.pathname).replace(/^\/+/,''));
    if(requested!==dist&&!requested.startsWith(dist+path.sep)){report.unknown_network.push({url:url.href,reason:'asset path outside dist'});return route.abort();}
    const file=fs.existsSync(requested)&&fs.statSync(requested).isFile()?requested:/^\/project\/90[12]\/(?:outline|chat)$/.test(url.pathname)?path.join(dist,'index.html'):null;
    if(!file){report.unknown_network.push({url:url.href,reason:'unknown production asset'});return route.abort();}
    await route.fulfill({status:200,contentType:mime[path.extname(file)]||'application/octet-stream',body:fs.readFileSync(file)});
  });
  await installFixtures(page,{state,handle:async({route,request,path:apiPath,method,body,reply})=>{
    if(new URL(request.url()).origin!==origin){report.unknown_network.push({url:request.url()});await route.abort();return true;}
    if(method==='POST'&&apiPath==='/projects/901/outline/freeze'){
      const hold=freezeGate;freezeGate=null;if(hold)await hold.pending;
      state.outlines[901].frozen=true;await reply({outline_frozen:true,outline_frozen_at:'2026-10-04 12:00:00'});return true;
    }
    if(method==='POST'&&apiPath==='/projects/901/outline/unfreeze'){
      const hold=unfreezeGate;unfreezeGate=null;if(hold)await hold.pending;
      if(failUnfreeze){failUnfreeze=false;await reply({detail:'验收模拟：取消冻结暂不可用'},503);return true;}
      state.outlines[901].frozen=false;await reply({outline_frozen:false,outline_frozen_at:null});return true;
    }
    if(method==='PUT'&&apiPath==='/projects/901/outline'){
      assert.equal(state.outlines[901].frozen,false,'Only the unfrozen save is expected');
      assert.equal(body.confirm,false,'An unfrozen save does not need frozen-change confirmation');
      state.outlines[901].content=body.content;state.outlines[901].history_count+=1;
      await reply({content:body.content,frozen:false});return true;
    }
    if(method!=='GET'){report.unknown_api.push({method,path:apiPath});await route.abort();return true;}
    return false;
  }});
  page.on('pageerror',error=>report.page_errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')report.console_errors.push(message.text());});
  page.on('dialog',async dialog=>{report.dialogs.push({type:dialog.type(),message:dialog.message()});await dialog.dismiss();});
  const formal=()=>page.locator('section').filter({has:page.getByRole('heading',{name:'正式大纲',exact:true})}).getByRole('textbox');
  const freezeRequests=()=>state.requests.filter(request=>request.method==='POST'&&request.path==='/projects/901/outline/freeze');
  const unfreezeRequests=()=>state.requests.filter(request=>request.method==='POST'&&request.path==='/projects/901/outline/unfreeze');
  const noConfirmation=async()=>assert.equal(await page.getByRole('dialog').count(),0);
  const screenshot=async name=>{
    await page.getByRole('heading',{name:'大纲规划',exact:true}).scrollIntoViewIfNeeded();
    const destination=path.join(output,name+'.png');const result=await page.screenshot({path:destination,fullPage:false,animations:'disabled'});
    const source=result&&!Buffer.isBuffer(result)&&(result.path||result.nextAction?.path);if(source&&source!==destination)fs.copyFileSync(source,destination);
    report.screenshots.push({name,path:destination,url:page.url(),viewport:page.viewportSize()});
  };
  const check=async(name,body)=>{const start=Date.now();try{const details=await body();report.checks.push({name,status:'passed',duration_ms:Date.now()-start,...details});}catch(error){report.checks.push({name,status:'failed',duration_ms:Date.now()-start,error:String(error.stack||error)});throw error;}};
  try{
    await page.goto(origin+'/__outline-freeze-fixture__',{waitUntil:'domcontentloaded'});
    storageBefore=await page.evaluate(()=>Object.fromEntries(Object.keys(localStorage).filter(key=>key.startsWith('aiw.')).map(key=>[key,localStorage.getItem(key)])));
    await page.goto(origin+'/project/901/outline',{waitUntil:'domcontentloaded'});
    await page.getByRole('button',{name:'冻结大纲',exact:true}).waitFor();
    await check('Freeze is visible, prevents duplicate submission and becomes cancel-freeze',async()=>{
      const hold=gate();gates.push(hold);freezeGate=hold;
      await page.getByRole('button',{name:'冻结大纲',exact:true}).evaluate(button=>{button.click();button.click();});
      await page.getByRole('button',{name:'正在冻结…',exact:true}).waitFor();
      assert.equal(await page.getByRole('button',{name:'正在冻结…',exact:true}).isDisabled(),true);assert.equal(freezeRequests().length,1);
      await page.getByRole('status').filter({hasText:'正在冻结'}).first().waitFor();
      await screenshot('01-freezing');hold.release();await page.getByRole('button',{name:'取消冻结',exact:true}).waitFor();
      await page.getByText('已冻结',{exact:true}).waitFor();assert.equal(state.outlines[901].frozen,true);await noConfirmation();
      return {requests:1,frozen:true,cancel_action_available:true};
    });
    await check('Cancel-freeze preserves unsaved edits, blocks freezing an unsaved draft and allows save without extra confirmation',async()=>{
      await formal().fill(edited);assert.equal(state.outlines[901].content,original);
      const hold=gate();gates.push(hold);unfreezeGate=hold;
      await page.getByRole('button',{name:'取消冻结',exact:true}).evaluate(button=>{button.click();button.click();});
      await page.getByRole('button',{name:'正在取消冻结…',exact:true}).waitFor();
      assert.equal(await page.getByRole('button',{name:'正在取消冻结…',exact:true}).isDisabled(),true);assert.equal(unfreezeRequests().length,1);
      await page.getByRole('status').filter({hasText:'正在取消冻结'}).first().waitFor();
      await screenshot('02-unfreezing');hold.release();await page.getByRole('button',{name:'冻结大纲',exact:true}).waitFor();
      await page.getByText('未冻结',{exact:true}).waitFor();assert.equal(await formal().inputValue(),edited);assert.equal(state.outlines[901].content,original);await noConfirmation();
      await page.getByRole('button',{name:'冻结大纲',exact:true}).click();
      await page.getByText(/请先保存大纲再冻结/).first().waitFor();
      assert.equal(freezeRequests().length,1,'An unsaved draft must not freeze the older disk content');
      assert.equal(state.outlines[901].frozen,false);assert.equal(await formal().inputValue(),edited);assert.equal(state.outlines[901].content,original);
      await page.getByText('有未保存的修改',{exact:true}).first().waitFor();
      await page.getByRole('button',{name:'保存',exact:true}).click();
      await page.waitForFunction(text=>[...document.querySelectorAll('textarea')].some(element=>element.value===text)&&document.body.innerText.includes('大纲已保存'),edited);
      await noConfirmation();const saves=state.requests.filter(request=>request.method==='PUT'&&request.path==='/projects/901/outline');
      assert.equal(saves.length,1);assert.equal(saves[0].body.content,edited);assert.equal(saves[0].body.confirm,false);assert.equal(state.outlines[901].frozen,false);
      await screenshot('03-unfrozen-saved');return {requests:1,unsaved_edits_preserved:true,freeze_unsaved_blocked:true,save_confirm:false,extra_dialogs:0};
    });
    await check('The saved outline can be frozen again and failed cancellation keeps the frozen state usable',async()=>{
      await page.getByRole('button',{name:'冻结大纲',exact:true}).click();await page.getByRole('button',{name:'取消冻结',exact:true}).waitFor();
      assert.equal(freezeRequests().length,2);assert.equal(state.outlines[901].content,edited);await screenshot('04-refrozen-cancel-available');
      failUnfreeze=true;await page.getByRole('button',{name:'取消冻结',exact:true}).click();
      await page.getByText('验收模拟：取消冻结暂不可用',{exact:true}).waitFor();
      await page.getByRole('button',{name:'取消冻结',exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'取消冻结',exact:true}).isEnabled(),true);
      await page.getByText('已冻结',{exact:true}).waitFor();assert.equal(state.outlines[901].frozen,true);assert.equal(await formal().inputValue(),edited);await noConfirmation();
      return {freeze_cycle_completed:true,failed_cancel_kept_frozen:true,retry_enabled:true,content_preserved:true};
    });
    await check('Switching projects ignores the old cancellation result and keeps the new book frozen',async()=>{
      const hold=gate();gates.push(hold);unfreezeGate=hold;
      await page.getByRole('button',{name:'取消冻结',exact:true}).click();await page.getByRole('button',{name:'正在取消冻结…',exact:true}).waitFor();
      await page.getByLabel('当前创作',{exact:true}).selectOption('902');await page.waitForURL('**/project/902/chat');
      await page.getByRole('link',{name:'大纲规划',exact:true}).click();await page.waitForURL('**/project/902/outline');
      await page.getByRole('button',{name:'取消冻结',exact:true}).waitFor();
      const response=page.waitForResponse(response=>/\/api\/projects\/901\/outline\/unfreeze$/.test(response.url()));hold.release();await response;
      await page.getByText('已冻结',{exact:true}).waitFor();assert.equal(await page.getByRole('button',{name:'取消冻结',exact:true}).isEnabled(),true);
      assert.equal(state.outlines[902].frozen,true);assert.equal(await formal().inputValue(),state.outlines[902].content);
      assert.doesNotMatch(await page.locator('main').innerText(),/作者保留的未保存修改|大纲已取消冻结/);await noConfirmation();
      await screenshot('05-project-switch-frozen');return {project_id:902,new_project_frozen:true,old_result_ignored:true};
    });
    assert.deepEqual(report.page_errors,[]);assert.deepEqual(report.console_errors.filter(error=>!/503/.test(error)),[]);assert.deepEqual(report.dialogs,[]);
    assert.deepEqual(state.unknown,[]);assert.deepEqual(report.unknown_api,[]);assert.deepEqual(report.unknown_network,[]);report.status='passed';
  }catch(error){report.status='failed';report.failure=String(error.stack||error);report.last_visible_text=(await page.locator('main').innerText().catch(()=>'' )).slice(-5000);await screenshot('failure').catch(()=>{});}
  finally{
    for(const hold of gates)hold.release();
    if(storageBefore)await page.evaluate(before=>{for(const key of Object.keys(localStorage).filter(key=>key.startsWith('aiw.')))localStorage.removeItem(key);for(const [key,value]of Object.entries(before))if(value!==null)localStorage.setItem(key,value);},storageBefore).catch(()=>{});
    report.finished_at=new Date().toISOString();report.api_requests=clone(state.requests);report.unknown_api.push(...state.unknown);
    report.counts={passed:report.checks.filter(item=>item.status==='passed').length,failed:report.checks.filter(item=>item.status==='failed').length,screenshots:report.screenshots.length,
      page_errors:report.page_errors.length,unexpected_console_errors:report.console_errors.filter(error=>!/503/.test(error)).length,simulated_failure_console_errors:report.console_errors.filter(error=>/503/.test(error)).length,
      unknown_api:report.unknown_api.length,unknown_network:report.unknown_network.length,api_requests:report.api_requests.length,freeze_requests:freezeRequests().length,unfreeze_requests:unfreezeRequests().length};
    fs.writeFileSync(path.join(output,'report.json'),JSON.stringify(report,null,2),'utf8');
  }
  return {status:report.status,counts:report.counts,failure:report.failure,report:path.join(output,'report.json'),screenshots:report.screenshots.map(item=>item.path)};
}
module.exports={runAcceptance};
