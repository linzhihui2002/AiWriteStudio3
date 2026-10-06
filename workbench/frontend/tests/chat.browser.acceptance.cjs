/* Manual browser contracts. Synthetic HTTP fixtures and owned, isolated Chrome only.
 * Run after the build served on 8790, outside npm test and CI.
 * WORKBENCH_PLAYWRIGHT points at the installed test runtime; no app dependency.
 */
const {chromium}=require(process.env.WORKBENCH_PLAYWRIGHT || 'C:/Users/30332/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {installFixtures,createFixtureState,chapterPath,materialPath}=require('./workbench.browser.fixtures.cjs');
const baseline=process.argv.includes('--baseline');
const performanceOnly=process.argv.includes('--performance-only');
const output=path.resolve(process.env.WORKBENCH_ACCEPTANCE_OUTPUT || '.workbench/workbench-upgrade-acceptance/browser');
fs.mkdirSync(output,{recursive:true});
const origin='http://127.0.0.1:8790';
const report={phase:baseline?'before':'after',started:new Date().toISOString(),checks:[],errors:[],unknown_api:[],screenshots:[],performance:[]};
let browser;
const record=(name,evidence)=>report.checks.push({name,evidence});
const session=(id=801,project_id=901)=>({id,project_id,title:'跨页合成会话',agent:'writing-assistant',agent_pinned:0,provider_id:'fixture-provider',model_id:'fixture-large',mode:'write',permission_mode:'auto',discussion_only:false,auto_apply:1,title_source:'user',title_status:'idle',created_at:'2026-10-03 10:00:00',updated_at:'2026-10-03 10:00:00'});
async function createPage(state=createFixtureState()){
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 await installFixtures(page,{state});
 page.on('close',()=>report.unknown_api.push(...state.unknown));
 if(baseline || process.env.WORKBENCH_ACCEPTANCE_DIST)await page.route('**/*',async route=>{
  const url=new URL(route.request().url());if(url.origin!==origin || url.pathname.startsWith('/api/'))return route.fallback();
  const base=path.resolve(baseline?'.workbench/upgrade-baseline-2026-10-03/baseline-dist':process.env.WORKBENCH_ACCEPTANCE_DIST);
  const filename=path.join(base,url.pathname.startsWith('/assets/')?url.pathname.slice(1):'index.html');
  assert.ok(filename.startsWith(base+path.sep));
  return route.fulfill({status:200,contentType:filename.endsWith('.js')?'application/javascript':filename.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(filename)});
 });
 page.on('pageerror',error=>report.errors.push(String(error)));
 page.setDefaultTimeout(12000);
 await page.addInitScript(()=>{
  // React DevTools commit flags report actual render work; no product instrumentation.
  window.__renderAudit={commits:0,historyRenders:0};
  window.__REACT_DEVTOOLS_GLOBAL_HOOK__={supportsFiber:true,inject:()=>1,onCommitFiberRoot:(_id,root)=>{
   window.__renderAudit.commits++;
   const seenMessages=new Set();
   const count=id=>{if(!seenMessages.has(id)){seenMessages.add(id);window.__renderAudit.historyRenders++}};
   const visit=fiber=>{
    if(!fiber)return;
    const historicalId=Number(fiber.memoizedProps?.message?.id);
    if(historicalId>=1 && historicalId<=300 && !(fiber.flags&1)){visit(fiber.sibling);return}
    if((fiber.flags&1)&&typeof fiber.type==='function'){
     if(historicalId>=1 && historicalId<=300)count(historicalId);
     else if(fiber.memoizedProps?.text?.includes('历史记录')){
      let ancestor=fiber;while(ancestor && !ancestor.memoizedProps?.['data-message-id'])ancestor=ancestor.return;
      const id=Number(ancestor?.memoizedProps?.['data-message-id']);
      if(id>=1 && id<=300 && !ancestor?.return?.memoizedProps?.message)count(id);
     }
    }
    visit(fiber.child);visit(fiber.sibling);
   };visit(root.current);
  },onCommitFiberUnmount:()=>{}};
 });
 return page;
}
async function screenshot(page,name){const filename=path.join(output,name+'.png');await page.screenshot({path:filename,fullPage:false});report.screenshots.push(filename)}
async function navigate(page,suffix){await page.goto(origin+suffix,{waitUntil:'networkidle'})}
const chatInput=page=>page.locator('.chat__input');
async function performance(){
 const state=createFixtureState();state.sessions=[session()];
 state.overrides['POST /projects/901/conflict/check']=()=>({changed:false,reason:'unchanged',rel_path:chapterPath,disk:{mtime:1,hash:'fixture-hash',size:100},diff:[]});
 state.messages[801]=Array.from({length:300},(_,index)=>({id:index+1,role:index%2?'assistant':'user',content:`历史记录 ${index+1}：桥边的账册已经核对。`+(index%2?'\n\n材料引用来自本书的设定。':''),created_at:'2026-10-03 10:00:00',meta:index===299?{steps:Array.from({length:40},(_,i)=>({index:i,call_id:'old-'+i,kind:'phase',tool:'read_file',label:'核对材料',status:'done',args:{path:materialPath},result:'已核对'}))}:{}}));
 for(const tree of Object.values(state.tree)){tree.root_nodes=[];for(let i=0;i<100;i++)tree.groups[1].nodes.push({name:`参考${i}.md`,rel_path:`设定/参考${i}.md`,type:'file',size:50})}
 const live={id:'perf-run',session_id:801,project_id:901,status:'running',text:'',last_seq:0,changes:[],steps:Array.from({length:40},(_,i)=>({index:i,call_id:'p'+i,kind:'phase',tool:'read_file',label:'核对材料',status:'done',args:{path:materialPath},result:'已核对'})),interactions:[]};
 state.overrides['POST /chat/sessions/801/runs']=({body})=>{state.runs[live.id]=live;state.messages[801].push({id:301,role:'user',content:body.text,created_at:'2026-10-03 10:00:00',meta:{run_id:live.id}},{id:302,role:'assistant',content:'',created_at:'2026-10-03 10:00:00',meta:{run_id:live.id,status:'running'}});return live};
 state.overrides['GET /chat/sessions/801']=()=>({...state.sessions[0],messages:state.messages[801],active_run:state.runs[live.id]||null});
 const page=await createPage(state);
 await page.exposeFunction('__finishPerf',text=>{live.text=text;live.status='completed';live.last_seq=101;state.messages[801].find(message=>message.id===302).content=text});
 await page.addInitScript(()=>{
  const original=window.fetch;
  window.fetch=async(input,init)=>{
   const url=typeof input==='string'?input:input.url;
   if(url.includes('/chat/runs/perf-run/events')){window.__streamDebug={url,frames:0};
    let index=0;
    const encoder=new TextEncoder();
    return new Response(new ReadableStream({start(controller){
     const push=()=>{
      index++;window.__streamDebug.frames=index;
      const frame=index<=100?{seq:index,event:'delta',run_id:'perf-run',session_id:801,text:'桥边的账册已经核对，记录者准备沿河寻找下一位证人。'.repeat(2).slice(0,50)}:{seq:index,event:'done',run_id:'perf-run',session_id:801,status:'completed',text:'桥边的账册已经核对，记录者准备沿河寻找下一位证人。'.repeat(2).slice(0,50).repeat(100)};
      if(index>100){window.__finishPerf(frame.text).then(()=>{controller.enqueue(encoder.encode('data: '+JSON.stringify(frame)+'\n\n'));controller.close()});return}
      controller.enqueue(encoder.encode('data: '+JSON.stringify(frame)+'\n\n'));
      setTimeout(push,index===100?500:15);
     };setTimeout(push,50);
    }}),{status:200,headers:{'Content-Type':'text/event-stream'}});
   }return original(input,init);
  };
 });
 const started=Date.now();await navigate(page,baseline?'/project/901/editor':'/project/901/chat');
 await chatInput(page).waitFor();await page.waitForFunction(()=>document.querySelectorAll('[data-message-id]').length>=300);
 const loadMs=Date.now()-started;
 if(baseline){
  await page.locator('.chat__references summary').click();
  await page.getByLabel('搜索参考文件').fill('参考');
  await page.locator('.chat__references input[type=checkbox]').evaluateAll(elements=>elements.forEach(element=>element.click()));
  await page.locator('.chat__references summary').click();
 }else{
  await page.locator('.chat__target-row').getByRole('button',{name:/材料/}).click();
  await page.getByLabel('搜索参考文件').fill('参考');
  await page.locator('.chat__file-picker input[type=checkbox]').evaluateAll(elements=>elements.forEach(element=>element.click()));
  await page.getByRole('dialog',{name:'本轮参考材料'}).getByRole('button',{name:'关闭',exact:true}).click();
 }
 await chatInput(page).fill('性能验收，请基于已有材料继续讨论。');
 await page.evaluate(()=>{window.__renderAudit={commits:0,historyRenders:0};window.__longTasks=[];new PerformanceObserver(list=>window.__longTasks.push(...list.getEntries().map(item=>item.duration))).observe({entryTypes:['longtask']})});
 const sendStarted=Date.now();await page.locator('.chat__composer').getByRole('button',{name:/^发送/}).click();
 try { await page.waitForFunction(()=>{const messages=document.querySelectorAll('.chat__messages > .chat__msg');return messages[messages.length-1]?.textContent.includes('沿河寻找下一位证人')}); } catch(error) {report.debug={requests:state.requests.slice(-20),body:(await page.locator('body').innerText()).slice(-3500),stream:await page.evaluate(()=>window.__streamDebug||null)};throw error}
 await page.locator('.chat__messages').evaluate(element=>{element.scrollTop=0;element.dispatchEvent(new Event('scroll'))});
 const top=await page.locator('.chat__messages').evaluate(element=>element.scrollTop);
 const firstDeltaMs=Date.now()-sendStarted;
 const inputStarted=Date.now();await chatInput(page).fill('继续阅读时，下一条要求仍可编辑。');
 const inputResponseMs=Date.now()-inputStarted;
 await page.evaluate(()=>{window.__renderAudit={commits:0,historyRenders:0}});
 await page.waitForFunction(()=>{const messages=document.querySelectorAll('.chat__messages > .chat__msg');return messages[messages.length-1]?.querySelector('.markdown-view')?.textContent.length>=5000});
 const elapsedMs=Date.now()-sendStarted;
 const audit=await page.evaluate(()=>({...window.__renderAudit,longTasks:window.__longTasks,overflow:document.documentElement.scrollWidth>innerWidth}));
 assert.equal(await page.evaluate(()=>window.__streamDebug.frames),100,'performance audit covers all 5000 live characters before completion refresh');
 const preserved=await page.locator('.chat__messages').evaluate(element=>element.scrollTop);
 assert.equal(preserved,top,'reading position is preserved during streaming');
 assert.equal(audit.overflow,false);
 assert.ok(audit.commits>5,'audit must observe actual live commits');
 if(!baseline)assert.equal(audit.historyRenders,0,'historical messages must never rerender for live deltas');
 assert.equal(state.requests.find(entry=>entry.path==='/chat/sessions/801/runs'&&entry.method==='POST').body.context.files.length,100,'100 references are actually attached');
 report.performance.push({historyMessages:300,replyChars:5000,processRecords:40,referenceFiles:100,loadMs,firstDeltaMs,inputResponseMs,elapsedMs,...audit});
 record('300 history / 5000 streamed reply / 40 process / 100 references',{historyRenders:audit.historyRenders,commits:audit.commits,scrollTop:preserved});
 if(!baseline){await page.getByRole('button',{name:/有新内容 · 回到最新/}).click();await page.waitForFunction(()=>{const area=document.querySelector('.chat__messages');return area.scrollHeight-area.clientHeight-area.scrollTop<5});record('new content reminder returns to the latest reply',true)}
 await screenshot(page,`${baseline?'baseline':'after'}-chat-performance`);await page.close();
}
async function crossPage(){
 const state=createFixtureState();state.sessions=[session(),session(802,902)];state.messages[801]=[];state.messages[802]=[];
 const page=await createPage(state);await navigate(page,'/project/901/chat');await chatInput(page).waitFor();
 await chatInput(page).fill('保留我的要求：检查林舟的动机。');
 for(const suffix of ['outline','bible','knowledge','editor']){
  await page.getByRole('link',{name:{outline:'大纲规划',bible:'设定总览',knowledge:'知识库与图谱',editor:'正文编辑器'}[suffix],exact:true}).click();
  if(!await chatInput(page).isVisible())await page.getByRole('button',{name:'打开本书写作助手',exact:true}).click();
  await expectValue(page,'保留我的要求：检查林舟的动机。');
  const dialog=page.getByRole('dialog',{name:'本书写作助手',exact:true});if(await dialog.isVisible())await page.keyboard.press('Escape');
 }
 await page.getByLabel('当前创作',{exact:true}).selectOption('902');await expectValue(page,'');
 await page.getByLabel('当前创作',{exact:true}).selectOption('901');await expectValue(page,'保留我的要求：检查林舟的动机。');
 assert.equal(state.requests.filter(entry=>entry.path==='/chat/sessions'&&entry.method==='GET'&&entry.query.project_id==='901').length,2,'same book pages keep a single controller; return after switching books reloads once');
 record('same-book page switching keeps draft; switching books isolates and restores',true);
 await page.setViewportSize({width:390,height:844});
 await page.getByRole('button',{name:'打开工作台导航',exact:true}).click();
 assert.equal(await page.locator('.app-main').evaluate(element=>element.inert),true);
 assert.equal(await page.locator('.app-sidebar').evaluate(element=>element.contains(document.activeElement)),true);
 await page.keyboard.press('Shift+Tab');assert.equal(await page.locator('.app-sidebar').evaluate(element=>element.contains(document.activeElement)),true);
 await page.keyboard.press('Escape');assert.equal(await page.locator('.app-main').evaluate(element=>element.inert),false);
 record('narrow navigation focus, trap, Escape and background inert',true);
 await page.close();
}
async function expectValue(page,value){await page.waitForFunction(value=>document.querySelector('.chat__input')?.value===value,value)}
const seededState=()=>{const state=createFixtureState();state.sessions=[session()];state.messages[801]=[];return state};
const createRun=(id,status='running')=>({id,session_id:801,project_id:901,status,text:'',last_seq:0,changes:[],steps:[],interactions:[],permission_mode:'auto'});
function restoreRun(state,run){state.runs[run.id]=run;state.messages[801]=[{id:1,role:'assistant',content:run.text,created_at:'2026-10-03 10:00:00',meta:{run_id:run.id,status:run.status,interactions:run.interactions}}];state.overrides['GET /chat/sessions/801']=()=>({...state.sessions[0],messages:state.messages[801],active_run:run});}
async function sendReliability(){
 const state=seededState();let attempts=0;const bodies=[];
 state.overrides['POST /chat/sessions/801/runs']=async({body,reply})=>{attempts++;bodies.push(body);if(attempts===1){await new Promise(resolve=>setTimeout(resolve,600));return reply({detail:'合成响应丢失'},503)}const run=createRun('retry-run','completed');run.text='已完成合成讨论。';state.runs[run.id]=run;return run};
 const page=await createPage(state);await navigate(page,'/project/901/chat');await chatInput(page).waitFor();
 await chatInput(page).fill('原始要求：检查桥边的账册。');
 await page.locator('.chat__composer').getByRole('button',{name:/^发送/}).click();
 await page.waitForFunction(()=>document.querySelector('.chat__composer-foot')?.textContent.includes('提交'));
 await chatInput(page).fill('后来输入的要求必须保留。');
 await page.getByRole('button',{name:'重试上次发送',exact:true}).waitFor();
 await page.locator('.chat__target-row').getByLabel('正文目标章节').selectOption(chapterPath);
 await page.getByRole('button',{name:'重试上次发送',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.chat__messages')?.textContent.includes('已完成合成讨论'));
 assert.deepEqual(bodies[1],bodies[0],'retry uses complete original snapshot including id, target and references');
 await expectValue(page,'后来输入的要求必须保留。');
 await chatInput(page).evaluate(element=>{element.dispatchEvent(new CompositionEvent('compositionstart',{bubbles:true}));element.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',ctrlKey:true,isComposing:true,keyCode:229,bubbles:true}));element.dispatchEvent(new CompositionEvent('compositionend',{bubbles:true}))});
 assert.equal(attempts,2,'Chinese composition must not send');
 await chatInput(page).fill('新的任务使用新 ID。');await page.locator('.chat__composer').getByRole('button',{name:/^发送/}).click();
 await page.waitForFunction(()=>document.querySelector('.chat__input')?.value==='');
 assert.equal(attempts,3);assert.notEqual(bodies[2].client_request_id,bodies[1].client_request_id);
 record('frozen retry, draft revision, fresh task id and Chinese IME',bodies.map(body=>({id:body.client_request_id,text:body.text,target:body.context.target_chapter})));
 await page.close();
}
async function interruptionContracts(){
 // Permanent HTTP failure: stop the reconnect loop and keep an explicit recovery action.
 {
  const state=seededState(),run=createRun('permanent-run');restoreRun(state,run);
  state.overrides['GET /chat/runs/permanent-run/events']=({reply})=>reply({detail:'会话已失效'},403);
  const page=await createPage(state);await navigate(page,'/project/901/chat');
  await page.getByText('会话已失效',{exact:false}).first().waitFor();
  await page.waitForTimeout(1300);
  assert.equal(state.requests.filter(item=>item.path==='/chat/runs/permanent-run/events').length,1);
  record('403 stream errors stop automatic retries',true);await page.close();
 }
 // A dropped event connection recovers by snapshot, without a second POST.
 {
  const state=seededState(),run=createRun('dropped-run');restoreRun(state,run);
  let gets=0;state.overrides['GET /chat/runs/dropped-run']=()=>{gets++;run.status='completed';run.text='快照恢复后的回复。';return run};
  const page=await createPage(state);await navigate(page,'/project/901/chat');
  await page.getByText('快照恢复后的回复。',{exact:true}).first().waitFor();
  assert.ok(gets>=1);assert.equal(state.requests.filter(item=>item.method==='POST'&&item.path.includes('/runs')).length,0);
  record('dropped SSE recovers persisted snapshot without replay',true);await page.close();
 }
 // POST success is authoritative even when optional snapshot refresh fails.
 {
  const state=seededState(),run=createRun('approval-run','waiting_input');
  run.interactions=[{id:'approve-1',run_id:run.id,kind:'approval',status:'pending',revision:1,payload:{operation:'delete',path:materialPath,reason:'删除操作需要批准',before_content:'合成人物卡',after_content:null}}];restoreRun(state,run);
  state.overrides['GET /chat/runs/approval-run/events']=({reply})=>reply({detail:'合成事件源暂停'},403);
  state.overrides['GET /chat/runs/approval-run']=({reply})=>reply({detail:'合成刷新失败'},503);
  state.overrides['POST /chat/runs/approval-run/interactions/approve-1/respond']=({body})=>{run.interactions[0]={...run.interactions[0],status:'answered',response:body.response,revision:2};return run.interactions[0]};
  const page=await createPage(state);await navigate(page,'/project/901/chat');
  await page.getByRole('button',{name:'批准这次操作',exact:true}).click();
  await page.getByText(/已回答/).first().waitFor();assert.equal(await page.getByRole('button',{name:'批准这次操作',exact:true}).count(),0);
  assert.equal(state.requests.filter(item=>item.method==='POST'&&item.path.endsWith('/respond')).length,1);
  record('approval acknowledgement survives snapshot refresh failure',true);await page.close();
 }
 // Cancellation uses its response; no misleading failure after an unrelated refresh.
 {
  const state=seededState(),run=createRun('cancel-run');restoreRun(state,run);
  state.overrides['GET /chat/runs/cancel-run/events']=({reply})=>reply({detail:'合成事件源暂停'},403);
  state.overrides['POST /chat/runs/cancel-run/cancel']=()=>({...run,status:'cancelled',text:'作者停止了合成任务。'});
  state.failures['GET /projects/901/tree']={remaining:0,status:503,detail:'合成材料刷新失败'};
  const page=await createPage(state);await navigate(page,'/project/901/chat');
  state.failures['GET /projects/901/tree'].remaining=1;
  await page.getByRole('button',{name:'停止',exact:true}).click();
  await page.getByText('作者停止了合成任务。',{exact:true}).first().waitFor();
  assert.equal(await page.getByText(/停止请求未确认/).count(),0);assert.equal(await page.getByRole('button',{name:'停止',exact:true}).count(),0);
  record('cancel snapshot is authoritative when material refresh fails',true);await page.close();
 }
}
async function halfOpen(){
 const state=seededState(),run=createRun('half-open-run');restoreRun(state,run);let checked=0;
 state.overrides['GET /chat/runs/half-open-run']=()=>{checked++;return {...run,status:'completed',text:'三十秒无帧后通过快照恢复。'}};
 const page=await createPage(state);page.setDefaultTimeout(38000);
 await page.addInitScript(()=>{
  const original=window.fetch;
  window.fetch=async(input,init)=>{
   const url=typeof input==='string'?input:input.url;
   if(!url.includes('/chat/runs/half-open-run/events'))return original(input,init);
   window.__halfOpenStarted=Date.now();
   return new Response(new ReadableStream({start(controller){init.signal.addEventListener('abort',()=>controller.error(new DOMException('aborted','AbortError')),{once:true})}}),{headers:{'Content-Type':'text/event-stream'}});
  };
 });
 await navigate(page,'/project/901/chat');await page.getByText('三十秒无帧后通过快照恢复。',{exact:true}).first().waitFor();
 const waited=await page.evaluate(()=>Date.now()-window.__halfOpenStarted);assert.ok(waited>=30000&&waited<38000);assert.ok(checked>=1);
 record('30 second half-open watchdog verifies snapshot',{waitedMs:waited,snapshots:checked});await page.close();
}
async function layouts(){
 for(const theme of ['light','dark','paper'])for(const width of [1920,1440,1280,1024,768,390]){
  const state=seededState();state.settings.appearance.theme=theme;
  const page=await createPage(state);await page.setViewportSize({width,height:width===390?844:1000});
  await navigate(page,'/project/901/chat');await chatInput(page).waitFor();
  const geometry=await page.evaluate(()=>({width:innerWidth,document:document.documentElement.scrollWidth,workspace:document.querySelector('.chat-workspace-layout').getBoundingClientRect().width,materials:getComputedStyle(document.querySelector('.chat__materials-pane')).display,ui:parseFloat(getComputedStyle(document.body).fontSize),chat:parseFloat(getComputedStyle(document.querySelector('.chat__msg')||document.querySelector('.chat__welcome')).fontSize)}));
  assert.ok(geometry.document<=geometry.width+1);assert.equal(geometry.materials==='block',geometry.workspace>=1000);
  await page.locator('.chat__target-row').getByRole('button',{name:/材料/}).click();
  const parent=page.getByRole('dialog',{name:'本轮参考材料',exact:true});await parent.waitFor();
  await parent.locator('.chat__file-picker').getByRole('button',{name:'预览',exact:true}).first().click();
  const child=page.getByRole('dialog',{name:/^材料预览/});await child.waitFor();
  assert.equal(await parent.evaluate(element=>!!element.closest('[inert]')),true);
  for(let index=0;index<6;index++){await page.keyboard.press('Tab');assert.ok(await child.evaluate(element=>element.contains(document.activeElement)))}
  await page.keyboard.press('Escape');await child.waitFor({state:'hidden'});assert.equal(await parent.evaluate(element=>!!element.closest('[inert]')),false);
  await page.keyboard.press('Escape');await parent.waitFor({state:'hidden'});
  await screenshot(page,`chat-${theme}-${width}`);
  await page.goto(origin+'/project/901/editor',{waitUntil:'networkidle'});
  await page.locator('.cm-content').waitFor();
  const editor=await page.locator('.editor').evaluate(element=>({width:element.getBoundingClientRect().width,layout:element.closest('[data-layout]').dataset.layout,overflow:document.documentElement.scrollWidth>innerWidth}));
  assert.equal(editor.layout,editor.width>=1200?'wide':editor.width>=900?'medium':'compact');assert.equal(editor.overflow,false);
  await page.getByRole('button',{name:'打开文档目录',exact:true}).waitFor();await page.getByRole('button',{name:'打开本书助手',exact:true}).waitFor();
  if(editor.layout!=='wide'){await page.getByRole('button',{name:'打开文档目录',exact:true}).click();await page.getByRole('dialog',{name:'本书文档目录',exact:true}).waitFor();await page.keyboard.press('Escape')}
  if(editor.layout==='compact'){await page.getByRole('button',{name:'打开本书助手',exact:true}).click();await chatInput(page).waitFor();await page.keyboard.press('Escape')}
  if(width===390||width===1920)await screenshot(page,`editor-${theme}-${width}`);
  record(`chat/editor layout ${theme} ${width}`,{chat:geometry,editor});await page.close();
 }
}
async function editorSelection(){
 const state=seededState();const page=await createPage(state);await navigate(page,'/project/901/chat');await chatInput(page).fill('保留已有要求，核对这个选区。');
 await page.getByRole('link',{name:'正文编辑器',exact:true}).click();const editor=page.locator('.cm-content');await editor.waitFor();await editor.click();await page.keyboard.press('Control+Home');await page.keyboard.press('Shift+End');
 await page.getByRole('button',{name:'交给助手',exact:true}).click();await expectValue(page,'保留已有要求，核对这个选区。');
 await page.locator('.chat__context').getByRole('button',{name:/选区/}).waitFor();
 await page.locator('.chat__target-row').getByRole('button',{name:/材料/}).click();const drawer=page.getByRole('dialog',{name:'本轮参考材料',exact:true});await drawer.locator('.chat__selection-preview summary').click();
 assert.ok((await drawer.locator('.chat__selection-preview pre').innerText()).includes('林舟推开门'));
 await page.keyboard.press('Escape');await page.keyboard.press('Escape');
 record('editor attaches exact selection and preserves existing chat requirements',true);await page.close();
}
async function activeCrossPage(){
 const state=seededState(),run=createRun('cross-active','waiting_input');
 run.interactions=[{id:'question-1',run_id:run.id,kind:'question',status:'pending',payload:{questions:[{id:'motivation',header:'人物动机',question:'选择林舟核对账本的动机。',options:[{id:'family',label:'寻找家人',description:'沿现有事实推进'}]}]}}];restoreRun(state,run);
 const page=await createPage(state);
 await page.addInitScript(()=>{
  const original=window.fetch;window.__crossStreams=0;
  window.fetch=async(input,init)=>{
   const url=typeof input==='string'?input:input.url;
   if(!url.includes('/chat/runs/cross-active/events'))return original(input,init);
   window.__crossStreams++;
   return new Response(new ReadableStream({start(controller){init.signal.addEventListener('abort',()=>controller.error(new DOMException('aborted','AbortError')),{once:true})}}),{headers:{'Content-Type':'text/event-stream'}});
  };
 });
 await navigate(page,'/project/901/chat');await page.getByLabel('其他',{exact:false}).check();await page.getByLabel('人物动机：其他回答').fill('我已输入的回答也要保留。');
 await chatInput(page).fill('保留正在运行会话的下一条草稿。');
 for(const suffix of ['outline','bible','knowledge','editor']){
  await page.getByRole('link',{name:{outline:'大纲规划',bible:'设定总览',knowledge:'知识库与图谱',editor:'正文编辑器'}[suffix],exact:true}).click();
  if(!await chatInput(page).isVisible())await page.getByRole('button',{name:'打开本书写作助手',exact:true}).click();
  await expectValue(page,'保留正在运行会话的下一条草稿。');
  assert.equal(await page.getByLabel('人物动机：其他回答').inputValue(),'我已输入的回答也要保留。');
  await page.locator('.chat__waiting-banner').waitFor();
  const overlay=page.getByRole('dialog',{name:'本书写作助手',exact:true});if(await overlay.isVisible())await page.keyboard.press('Escape');
 }
 assert.equal(await page.evaluate(()=>window.__crossStreams),1,'page portals keep only one subscription');
 assert.equal(state.requests.filter(entry=>entry.path.endsWith('/cancel')).length,0);
 record('active task, pending question, answer draft and one SSE survive four page switches',true);await page.close();
 const preferences=seededState();preferences.settings.appearance.fontSize=22;
 const reading=await createPage(preferences);await navigate(reading,'/project/901/editor');await reading.locator('.cm-content').waitFor();
 assert.equal(await reading.locator('.cm-content').evaluate(element=>parseFloat(getComputedStyle(element).fontSize)),22);
 record('saved author reading font overrides the 18px default',22);await reading.close();
}
async function materialPreferences(){
 const state=seededState(),page=await createPage(state);await navigate(page,'/project/901/chat');await chatInput(page).waitFor();
 const separator=page.getByRole('separator',{name:'调整材料预览宽度'});await separator.focus();await page.keyboard.press('ArrowLeft');
 assert.equal(await separator.getAttribute('aria-valuenow'),'336');
 assert.equal(await page.evaluate(()=>localStorage.getItem('aiw.chat.materialsWidth')),'336');
 await page.getByRole('button',{name:'收起并排材料区',exact:true}).click();
 await page.getByRole('button',{name:'恢复并排材料区',exact:true}).waitFor();
 await page.setViewportSize({width:390,height:844});await page.locator('.chat__target-row').getByRole('button',{name:/材料/}).click();
 await page.getByRole('dialog',{name:'本轮参考材料'}).waitFor();await page.keyboard.press('Escape');
 await page.setViewportSize({width:1440,height:1000});await page.getByRole('button',{name:'恢复并排材料区',exact:true}).click();
 assert.equal(await separator.getAttribute('aria-valuenow'),'336');
 await page.reload({waitUntil:'networkidle'});assert.equal(await separator.getAttribute('aria-valuenow'),'336');
 const menu=page.getByLabel('会话操作',{exact:true});await menu.focus();await page.keyboard.press('ArrowDown');
 assert.equal(await page.getByRole('button',{name:'重命名',exact:true}).evaluate(element=>element===document.activeElement),true);
 await page.keyboard.press('Escape');assert.equal(await menu.evaluate(element=>element===document.activeElement),true);
 record('material width/collapse preferences survive responsive changes; session menu keyboard access',336);
 await page.locator('.chat__target-row').getByRole('button',{name:/材料/}).click();
 const drawer=page.getByRole('dialog',{name:'本轮参考材料'});await drawer.getByLabel(materialPath,{exact:true}).check();await page.keyboard.press('Escape');
 const materials=page.getByRole('complementary',{name:'对话材料与任务依据'});await materials.getByRole('button',{name:materialPath,exact:true}).click();
 await materials.getByText('原文预览',{exact:true}).waitFor();await materials.getByText(/文件版本 · fixture/).waitFor();
 assert.ok((await materials.innerText()).includes('林舟推开门'));
 state.failures['GET /projects/901/chapters/content']={remaining:1,status:503,detail:'合成材料读取失败'};
 await materials.getByRole('button',{name:materialPath,exact:true}).click();await materials.getByText('合成材料读取失败',{exact:false}).waitFor();
 assert.ok((await materials.innerText()).includes('林舟推开门'),'failed refresh preserves previous material');
 await materials.getByRole('button',{name:'重新读取',exact:true}).click();await materials.getByText('合成材料读取失败',{exact:false}).waitFor({state:'hidden'});
 record('inline material original/version preview preserves prior content and retries failed refresh',true);
 await screenshot(page,'chat-material-version');await page.close();
}
async function deliveryAndReading(){
 const state=seededState();
 const changes=['applied','pending','rejected','failed'].map((status,index)=>({id:index+1,run_id:'delivery-run',operation:'write',path:`设定/交付${index+1}.md`,status,before_content:'原有依据',after_content:'修订后的依据'}));
 const text='核对完成：人物姓名和目标保留，交付状态如下。\n\n|'+Array.from({length:15},(_,i)=>`材料列${i}`).join('|')+'|\n|'+Array(15).fill('---').join('|')+'|\n|'+Array(15).fill('保留本书事实与章节依据').join('|')+'|';
 state.messages[801]=[{id:10,role:'assistant',content:text,created_at:'2026-10-03 10:00:00',meta:{run_id:'delivery-run',status:'completed',changes}}];
 state.runs['delivery-run']={...createRun('delivery-run','completed'),text,changes};
 state.overrides['POST /chat/runs/delivery-run/revert']=()=>{changes[0]={...changes[0],reverted:true,status:'reverted'};state.runs['delivery-run'].changes=changes;return{changes}};
 const page=await createPage(state);await page.addInitScript(()=>{Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{window.__copiedReply=text}},configurable:true})});
 await page.setViewportSize({width:390,height:844});await navigate(page,'/project/901/chat');
 const card=page.locator('.chat__changes');for(const status of ['已保存','待应用','门禁未通过','未完成'])await card.getByText(status,{exact:true}).waitFor();
 const table=page.getByRole('region',{name:'表格，可横向滚动'});assert.ok(await table.evaluate(element=>element.scrollWidth>element.clientWidth));
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.getByRole('button',{name:'复制回复',exact:true}).click();assert.equal(await page.evaluate(()=>window.__copiedReply),text);
 await card.getByRole('button',{name:'查看差异',exact:true}).first().click();const diff=page.getByRole('dialog',{name:'文件差异 · 设定/交付1.md',exact:true});await diff.waitFor();
 assert.ok((await diff.innerText()).includes('原有依据'));assert.ok((await diff.innerText()).includes('修订后的依据'));await page.keyboard.press('Escape');
 await page.locator('.chat__configuration-toggle').click();await page.getByLabel('仅讨论，不修改文件',{exact:true}).click();await page.waitForFunction(()=>document.querySelector('.chat__discussion-row input').checked);await page.keyboard.press('Escape');
 assert.equal(await card.getByRole('button',{name:'撤回',exact:true}).first().isDisabled(),true);
 await page.locator('.chat__configuration-toggle').click();await page.getByLabel('仅讨论，不修改文件',{exact:true}).click();await page.waitForFunction(()=>!document.querySelector('.chat__discussion-row input').checked);await page.keyboard.press('Escape');
 await card.getByRole('button',{name:'撤回',exact:true}).first().click();await card.getByText('已撤回',{exact:true}).waitFor();
 assert.equal(state.requests.filter(entry=>entry.path==='/chat/runs/delivery-run/revert').length,1);
 record('delivery states, original/diff, permission-aware undo, full reply copy and local table scrolling',true);
 await screenshot(page,'chat-delivery-mobile');await page.close();
}
(async()=>{browser=await chromium.launch({channel:'chrome',headless:true});await performance();if(!baseline&&!performanceOnly){await crossPage();await sendReliability();await interruptionContracts();await layouts();await editorSelection();await activeCrossPage();await materialPreferences();await deliveryAndReading();await halfOpen();}assert.deepEqual(report.errors,[]);assert.deepEqual(report.unknown_api,[]);})()
.catch(error=>{report.failure=String(error.stack||error);process.exitCode=1})
.finally(async()=>{if(browser)await browser.close();const filename=path.join(output,`${baseline?'baseline':'after'}-chat-report.json`);fs.writeFileSync(filename,JSON.stringify(report,null,2));console.log(JSON.stringify({file:filename,checks:report.checks.length,performance:report.performance,errors:report.errors,failure:report.failure,debug:report.debug},null,2))});
