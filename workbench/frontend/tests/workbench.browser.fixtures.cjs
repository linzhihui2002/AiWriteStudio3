/* Synthetic browser fixtures. Every /api/ request is intercepted; unknown APIs fail
 * with 503. No author books, model providers, backend writes or engine calls occur.
 * Extend state.overrides['METHOD /path'] with a value or async context => value.
 * state.failures and state.delays use the same keys (or a path without method).
 */
const timestamp = '2026-10-03 10:00:00';
const clone = value => JSON.parse(JSON.stringify(value));
const chapterPath = '章节/第0001章.txt';
const materialPath = '设定/人物设定.md';
const deepMerge = (base, patch) => { for (const [key, value] of Object.entries(patch || {})) base[key] = value && typeof value === 'object' && !Array.isArray(value) ? deepMerge(base[key] || {}, value) : value; return base; };

function createFixtureState() {
  const books = [
    { id: 901, name: '验收书甲·渡桥', path: '.workbench/evals/browser-book-a', created_at: timestamp, archived: false, genre: '奇幻', platform: '通用', protagonist: '林舟', one_liner: '渡桥人寻找失落的账册。', has_cover: false },
    { id: 902, name: '验收书乙·巡城', path: '.workbench/evals/browser-book-b', created_at: timestamp, archived: false, genre: '悬疑', platform: '通用', protagonist: '顾遥', one_liner: '巡城人核对一封错投的信。', has_cover: false },
  ];
  const settings = { chat: { permission_mode: 'auto', discussion_only: false, scope_strictness: 'ask', scope_limit_full: false, intent_llm: true }, writing: { always_inject_skills: ['human-linguistics'], chapter_min_words: 2000, chapter_max_words: 4000, auto_deslop: { enabled: true, max_rounds: 2 } }, milestone: { enabled: true, step: 500 }, appearance: { theme: 'light', followSystem: false, reduceMotion: true, fontSize: 18, lineHeight: 1.9, letterSpacing: 0, pageWidth: 760, colorTemperature: 0, contrast: 1 }, engines: { default: 'dsh-headless', overrides: {}, dsh_timeout_seconds: 1800 }, budget: { daily_token_limit: 20000, alert_ratio: .8, currency_per_1k_tokens: 0 }, editor: { autosave_seconds: 3, ghost_text: false, milestone_inline: true }, templates: { default: '默认模板', by_genre: {}, by_platform: {} } };
  const state = { books, settings, sessions: [], messages: {}, runs: {}, events: {}, tree: {}, chatDefaults: {}, writingPrefs: {}, chapters: {}, outlines: {}, cards: {}, entities: {}, proposals: [], workflowRuns: [], imageJobs: [], imageRecords: [], overrides: {}, failures: {}, delays: {}, requests: [], unknown: [] };
  for (const book of books) {
    const hero = book.protagonist;
    state.chapters[book.id] = [{ rel_path: chapterPath, file_name: '第0001章.txt', number: 1, title: `${hero}出门`, status: 'draft', word_count: 2450, contract_id: 'fixture-contract', is_empty: false, size: 8000, mtime: 1780000000 }];
    state.tree[book.id] = { project: book, groups: [{ key: 'chapters', name: '章节', rel_path: '章节', exists: true, nodes: [{ ...state.chapters[book.id][0], name: '第0001章.txt', type: 'file', is_chapter: true }] }, { key: 'setting', name: '设定', rel_path: '设定', exists: true, nodes: [{ name: '人物设定.md', rel_path: materialPath, type: 'file', size: 120, is_empty: false }] }], root_nodes: [] };
    state.tree[book.id].root_nodes = state.tree[book.id].groups.map(group => ({name:group.name, rel_path:group.rel_path, type:'dir', children:group.nodes}));
    state.outlines[book.id] = { project_id: book.id, project_name: book.name, content: `# ${book.name}\n${hero}出门寻找线索，第一卷在桥边发现旧账。`, frozen: false, locked: null, questions: ['主角愿意付出的代价是什么？'], candidates: [{ title: '从账册入手', content: `${hero}先调查账册，再追问渡桥人。`, source: 'model' }, { title: '从信件入手', content: `${hero}先核对信件，再寻找收信人。`, source: 'model' }], history_count: 1 };
    const evidence = { quote: `${hero}：身份是记录者。`, start: 3, end: 14, rel_path: materialPath, line_start: 3, line_end: 4, document_hash: 'fixture-hash' };
    state.entities[book.id] = [{ name: hero, ref: `character:${hero}`, rel_path: materialPath, source: 'author', fields: { 身份: '记录者', 目标: '寻找线索' }, field_sources: {身份:'author',目标:'author'}, missing: [], summary: `${hero}负责核对材料。`, content: `## ${hero}\n身份：记录者\n目标：寻找线索` }];
    state.cards[book.id] = [{ id: `card-${book.id}`, entity_id: `hero-${book.id}`, ref: `${materialPath}#${hero}`, name: hero, aliases: [], highlight: 'highlight-1', highlight_mode: 'auto', highlight_colors: { light: 'var(--highlight-1)', dark: 'var(--highlight-1)', paper: 'var(--highlight-1)' }, fields: { 身份: '记录者', 目标: '寻找线索' }, field_meta: { 身份: { source: 'author', evidence, source_evidence: null, capabilities:{rename:true,delete:true} }, 目标: { source: 'author', evidence, source_evidence: null, capabilities:{rename:true,delete:true} } }, source: 'author', category: 'character', file: materialPath, field_list: ['身份', '目标'], summary: `${hero}的身份与目标。`, evidence, extent: evidence, source_hash: 'fixture-hash', stale: false }];
    state.proposals.push({ id: book.id + 100, project_id: book.id, task_id: 1, kind: '设定', title: `${hero}身份补充`, target_path: materialPath, status: 'pending', created_at: timestamp, meta: {}, content: `## ${hero}\n身份：记录者`, chars: 20, is_new_file: false, diff: [{ type: 'add', text: '身份：记录者', line: 3 }] });
  }
  state.workflowRuns = [{ id: 401, project_id: 901, workflow: '默认八步产章', status: 'paused', cursor: 1, payload: { workflow: '默认八步产章', chapters: [chapterPath, '章节/第0002章.txt'], chapter_index: 1, node_index: 3, completed: [chapterPath] }, created_at: timestamp, updated_at: timestamp, progress: { chapter_index: 1, node_index: 3, chapters_total: 2, completed: [chapterPath], remaining: ['章节/第0002章.txt'] }, log: [{ at: timestamp, event: 'chapter_completed', chapter: chapterPath, steps: { CONTRACT: 'done', WRITE: 'done', REVIEW: 'done' } }] }];
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="320"><rect width="240" height="320" fill="#c1cbd4"/><rect x="30" y="170" width="180" height="80" fill="#5e6e7e"/><path d="M20 190h200" stroke="#f1eadd" stroke-width="4"/></svg>';
  state.imageRecords = [{ id: 701, job_id: 600, provider_id: 'fixture-image', model_id: 'fixture-image-model', prompt: '灰蓝色城市与桥梁的封面草图', size: '1024x1536', url: `data:image/svg+xml;base64,${Buffer.from(svg).toString('base64')}`, filename: 'fixture-cover.svg', favorite: false, created_at: timestamp, params: { size: '1024x1536', quality: 'high', output_format: 'png', n: 1 } }];
  state.templates = [{id:1,name:'默认模板',path:'templates/default',is_builtin:true,source:'builtin',file_count:1}];
  state.templateFiles = {'默认模板':{[materialPath]:'人物名称：\n身份：'}};
  return state;
}

async function installFixtures(page, options = {}) {
  const state = options.state || createFixtureState();
  const requests = state.requests;
  await page.route('**/api/**', async route => {
    const request = route.request(); const url = new URL(request.url()); const path = decodeURIComponent(url.pathname.replace(/^\/api/, '')); const method = request.method();
    let body; try { body = request.postDataJSON() } catch { body = null }; body = body || {}
    const key = `${method} ${path}`; const entry = { method, path, query: Object.fromEntries(url.searchParams), body, time: Date.now() }; requests.push(entry);
    const reply = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    const context = { route, request, url, path, method, body: body || {}, state, requests, reply };
    if (options.handle && await options.handle(context)) return;
    const failure = state.failures[key] ?? state.failures[path];
    if (failure && (typeof failure !== 'object' || failure.remaining === undefined || failure.remaining > 0)) { if (typeof failure === 'object' && failure.remaining !== undefined) failure.remaining -= 1; return reply({ detail: typeof failure === 'string' ? failure : failure.detail || '验收模拟读取失败' }, typeof failure === 'object' ? failure.status || 503 : 503); }
    const delay = state.delays[key] ?? state.delays[path]; if (delay) await new Promise(resolve => setTimeout(resolve, typeof delay === 'number' ? delay : 400));
    if (Object.hasOwn(state.overrides, key) || Object.hasOwn(state.overrides, path)) { const override = state.overrides[key] ?? state.overrides[path]; const value = typeof override === 'function' ? await override(context) : override; if (value === undefined) return; return value && typeof value.status === 'number' && Object.hasOwn(value, 'body') ? reply(value.body, value.status) : reply(value); }
    const projectMatch = /^\/projects\/(\d+)(.*)$/.exec(path); const projectId = projectMatch ? Number(projectMatch[1]) : Number(url.searchParams.get('project_id') || 901); const tail = projectMatch?.[2] || ''; const book = state.books.find(item => item.id === projectId) || state.books[0]; const hero = book?.protagonist || '林舟';
    const usage = { days: Number(url.searchParams.get('days') || 7), totals: { tokens: 12340, cost: 0, calls: 8, today_tokens: 2000 }, by_day: [{ day: '2026-10-03', tokens: 2000, prompt: 1500, completion: 500, cost: 0, calls: 2 }], by_project: [{ project_id: projectId, tokens: 12340, cost: 0, calls: 8 }], by_task_type: [{ task_type: '审稿', tokens: 12340, cost: 0, calls: 8 }], budget: { ...state.settings.budget, alert: false, currency_per_1k_tokens: 0 } };
    if (path === '/health') return reply({ status: 'ok', version: 'fixture', port: 8790, db_ready: true, fts: true, vendor_dsh: true });
    if (path === '/settings') { if (method === 'PUT') deepMerge(state.settings, body.patch); return reply(state.settings) }
    if (path === '/projects' && method === 'GET') return reply(state.books.filter(item => !item.deleted && (url.searchParams.get('include_archived') === 'true' || !item.archived)));
    if (path === '/projects' && method === 'POST') { const created = { ...state.books[0], ...body, id: 903, name: body.name }; state.books.push(created); return reply(created) }
    if (projectMatch && tail === '') { if (method === 'PATCH') Object.assign(book, body); if (method === 'DELETE') book.deleted = true; return reply(book) }
    if (tail === '/archive') { book.archived = !!body.archived; return reply(book) }
    if (tail === '/restore') { book.deleted = false; return reply({restored_from:'synthetic-trash',rel_path:book.name}) }
    if (path === '/trash') return reply(state.books.filter(item => item.deleted).map(item => ({kind:'project',project_id:item.id,project_name:item.name,trash_rel:`synthetic-${item.id}`,deleted_at:timestamp,rel_path:''})));
    if (path === '/templates' && method==='GET') return reply(state.templates);
    if (path === '/templates/prefs') {if(method==='PUT')deepMerge(state.settings.templates,body);return reply(state.settings.templates)}
    if (path === '/templates/blank' || path === '/templates/import' || path === '/templates' && method==='POST') {const name=body.name||body.new_name||'合成模板';const template={id:state.templates.length+1,name,path:`templates/${name}`,is_builtin:false,source:'custom',file_count:1};state.templates.push(template);state.templateFiles[name]=body.files||{[materialPath]:'人物名称：\n身份：'};return reply(template)}
    const templateMatch=/^\/templates\/([^/]+)(.*)$/.exec(path);
    if(templateMatch){const name=templateMatch[1],tail=templateMatch[2];const template=state.templates.find(item=>item.name===name);const files=state.templateFiles[name]||{};
      if(tail==='/duplicate'){const copy={...template,id:state.templates.length+1,name:body.new_name,is_builtin:false,source:'custom'};state.templates.push(copy);state.templateFiles[copy.name]=clone(files);return reply(copy)}
      if(tail==='/tree')return reply({name,is_builtin:template?.is_builtin||false,nodes:Object.keys(files).map(rel=>({name:rel.split('/').at(-1),rel_path:rel,type:'file'}))});
      if(tail==='/file'){const rel=body.rel_path||url.searchParams.get('rel_path')||materialPath;if(method==='PUT')files[rel]=body.content;return reply({name,rel_path:rel,content:files[rel]||'',is_builtin:template?.is_builtin||false})}
      if(tail==='/export')return reply({name,dirs:['设定'],files});
      if(tail===''&&method==='PATCH'){state.templateFiles[body.new_name]=files;delete state.templateFiles[name];template.name=body.new_name;return reply(template)}
      if(tail===''&&method==='DELETE'){state.templates=state.templates.filter(item=>item.name!==name);delete state.templateFiles[name];return reply({name})}
    }
    if (path === '/publish/platforms') return reply([{key:'通用',label:'通用',word_range:[2000,4000],suffix:'.txt',notes:[]}]);
    if (path === '/providers') return reply({ providers: [{ id: 1, provider_id: 'fixture-provider', display_name: '验收模型', source: 'workbench_custom', base_url: 'https://fixture.invalid', models: [{ id: 'fixture-small', name: '验收快速模型', context_window: 32000, max_tokens: 4000 }, { id: 'fixture-large', name: '验收完整模型', context_window: 128000, max_tokens: 8000 }], capabilities: ['chat'], timeout_seconds: 60, enabled: true, health: 'ok', has_secret: true, masked_secret: '****', created_at: timestamp }], capability_tags: ['chat'], cipher: 'fixture', defaults: { provider_id: 'fixture-provider', model_id: 'fixture-large' } });
    if (path === '/engines') return reply({ decision_table: {正文生成:'dsh-headless',审稿:'dsh-headless'}, global_default:'dsh-headless',global_overrides:{},fallback_engine:'dsh-headless',engines:{'dsh-headless':{available:true,model:'fixture-large',provider:'fixture-provider',dsh_home:'.workbench/dsh-home'}} });
    if (path === '/usage') return reply(usage);
    if (path === '/tasks') return reply([{ id: 301, project_id: projectId, task_type: '审稿', engine: 'dsh-headless', model: 'fixture-large', provider: 'fixture-provider', agent: 'reviewer', status: 'done', prompt_id: 'synthetic-review', prompt_version: '1', context_snapshot: {project_id:projectId}, result: '已记录两项待核实问题。', error: null, created_at: timestamp, finished_at: timestamp }]);
    if (path === '/skills') return reply({ skills: [{name:'human-linguistics',description:'中文语言检查',source:'builtin',enabled:true,synced:true,valid:true,estimated_tokens:100,reference_files:[],bound_agents:['reviewer']}], estimated_tokens:100,target_root:'.dsh/skills' });
    if (path === '/agents') return reply({ agents: [{id:1,name:'writing-assistant',title:'写作助手',description:'讨论故事与材料',source:'builtin',skills:['human-linguistics'],tools:['检索','文件读写'],materials:[],boundaries:'只读讨论',capabilities:[],provider_id:'',model_id:'',is_builtin:true,enabled:true},{id:2,name:'reviewer',title:'审稿人',description:'核对章节',source:'builtin',skills:['human-linguistics'],tools:['检索'],materials:[],boundaries:'只报告',capabilities:[],provider_id:'',model_id:'',is_builtin:true,enabled:true}],builtin_count:2,tool_whitelist:['检索','文件读写'],material_dirs:['设定','大纲','章节','状态'] });
    if (path === '/rules') return reply({global:[],project:[],skill_builtin:[]});
    if (path === '/rules/resolved') return reply({effective:[],conflicts:[],order:['global','project']});
    if (path === '/vector/model') return reply({active:{model_dir:'synthetic-model',name:'fixture',ready:true,dim:384,offline:true,note:'合成模型'},options:[],local_files:[],models_dir:'synthetic-models',default_dim:384,explicit_path_supported:true});
    if (tail === '/tree') return reply(state.tree[projectId]);
    if (tail === '/cards/highlights') return reply(state.cards[projectId].map(card=>({entity_id:card.entity_id,name:card.name,aliases:card.aliases,highlight_mode:card.highlight_mode,highlight_colors:card.highlight_colors})));
    if (tail === '/chapters') return reply(state.chapters[projectId]);
    if (tail === '/chapters/content') return reply({...state.chapters[projectId][0],hash:'fixture-hash',mtime:1780000000,content:`${hero}推开门，把账册放在桌上。\n“先核对日期。”他说。`,body:`${hero}推开门，把账册放在桌上。\n“先核对日期。”他说。`,meta:{},file_name:'第0001章.txt'});
    if (tail === '/conflict/check') return reply({changed:false,reason:'unchanged',rel_path:body.rel_path || chapterPath,disk:{mtime:1780000000,hash:'fixture-hash',size:8000},external_content:null,diff:[]});
    if (tail === '/contracts/detail') return reply({chapter_rel:chapterPath,plot_points:['核对账册日期'],word_budget:2500,hook_type:'疑问',entities:[hero],constraints:[],must_connect:[],status:'frozen'});
    if (tail === '/snapshots' || tail === '/log' || tail === '/contracts') return reply([]);
    if (tail === '/gates') return reply({prerequisites:{checks:[],blocked:false,reasons:[]},contract:{ok:true,hint:''}});
    if (tail === '/writing-prefs') {if(method==='PUT')state.writingPrefs[projectId]={...(state.writingPrefs[projectId]||{}),...(body.patch||body)};return reply({...state.settings.writing,...state.writingPrefs[projectId],source:state.writingPrefs[projectId]?'book':'global'})}
    if (tail === '/chat-defaults') {if(method==='PUT')state.chatDefaults[projectId]={...(state.chatDefaults[projectId]||{}),...(body.patch||body)};return reply({...state.settings.chat,...state.chatDefaults[projectId],source:state.chatDefaults[projectId]?'book':'global'})}
    if (tail === '/chat-memory') return reply({entries:[]});
    if (tail === '/outline') { if (method === 'PUT') state.outlines[projectId].content = body.content; return reply(state.outlines[projectId]) }
    if (tail === '/outline/history') return reply([{at:timestamp,confirmed:false,diff:['新增卷纲']}]);
    if (tail === '/outline/lock') { state.outlines[projectId].locked = state.outlines[projectId].candidates[body.index]; state.outlines[projectId].content = state.outlines[projectId].locked.content; return reply({locked:true}) }
    if (tail === '/outline/freeze') {state.outlines[projectId].frozen=true;return reply({frozen:true})}
    if (tail === '/outline/expand') return reply({questions:state.outlines[projectId].questions});
    if (tail === '/outline/candidates') return reply({candidates:state.outlines[projectId].candidates});
    if (tail === '/outline/rolling') return reply({volumes_total:2,refined:['第一卷'],suggestion:'第一章核对账册，第二章寻找证人。',note:'验收生成结果'});
    if (tail === '/bible') return reply({project_id:projectId,project_name:book.name,labels:{character:'人物',world:'世界',faction:'势力',item:'物品',skill:'技能',scene:'场景'},counts:{character:1,world:1,foreshadow:1},total:3,timeline_count:1,foreshadow_open:1,foreshadow_total:1,foreshadow_planned:0,unknown_count:0,unknown_refs:[]});
    if (tail === '/bible/unknown') return reply([]);
    if (/^\/bible\//.test(tail)) return reply({kind:tail.split('/').at(-1),label:'人物',entities:state.entities[projectId],count:1});
    if (tail === '/characters') return reply([{...state.entities[projectId][0],state:{位置:'桥边',目标:'核对账册'}}]);
    if (tail.endsWith('/growth')) return reply({character:hero,series:{信任:[{chapter:1,value:2}]},events:[{chapter:'第一章',story_time:'清晨',event:'出门核对账册',evidence:'正文第一段'}]});
    if (tail === '/timeline') return reply([{chapter:'第一章',story_time:'清晨',event:'出门核对账册',evidence:'正文第一段'}]);
    if (tail === '/foreshadows') return reply([{line:3,content:'旧账册缺了一页',status:'待回收',planted_in:'第一章',evidence:'桌上的账册被撕去一页。',document_hash:'fixture-foreshadow-hash',is_planned:false,planned_chapter:''}]);
    if (tail === '/cards') {const cards=state.cards[projectId].filter(card=>(!url.searchParams.get('query') || card.name.includes(url.searchParams.get('query'))) && (!url.searchParams.get('category') || card.category===url.searchParams.get('category')));return reply({project_id:projectId,project_name:book.name,cards,count:cards.length,counts_by_category:{character:1},palette:['highlight-1','highlight-2'],categories:[{key:'character',label:'人物',path:materialPath,builtin:true}],parse_status:'completed',task_id:null,task:null,stale_files:[]})}
    if (tail === '/cards/chapter-line') return reply({name:hero,total:1,occurrences:[{rel_path:chapterPath,number:1,count:3}]});
    if (tail === '/cards/highlight') {state.cards[projectId][0].highlight=body.color;return reply({ref:body.ref,highlight:body.color})}
    if (tail === '/cards/export') return reply({count:1,content:`${hero}：记录者`});
    if (tail === '/teardown') return reply({targets:[{target:'原创对标样章',genre:'奇幻',chars:2400,imported_at:timestamp,has_analysis:true,has_facts:true,fact_count:1}],dimensions:['开篇钩子','情绪节拍','主角行动线','信息节奏','语言特征','可迁移手法']});
    const facts=[{target:'原创对标样章',genre:'奇幻',dimension:'开篇钩子',conclusion:'用账册缺页建立问题。',evidence:'第一章第三段',quote:'账册缺了一页。'}];
    if (tail === '/teardown/facts') return reply({facts,count:1,missing_evidence:0});
    if (tail === '/teardown/recall') return reply({genre:'奇幻',matched:1,facts});
    if (tail === '/teardown/artifact') return reply({target:'原创对标样章',kind:url.searchParams.get('kind')||'source',meta:{genre:'奇幻'},content:'# 原创对标样章\n记录者发现账册缺页，出门寻找证人。'});
    if (tail === '/teardown/analyze') return reply({target:'原创对标样章',source:'model',facts,fact_count:1,missing_evidence:0,files:['拆书/原创对标样章/事实卡.md']});
    if (tail === '/review') return reply({review_id:501,rel_path:chapterPath,verdict:'未通过',hard_gates:{passed:false,words:2450,gates:[{key:'字数',passed:true,blocking:true,detail:'2450字'},{key:'连续性',passed:false,blocking:true,detail:'日期待核实'}],locations:[{gate:'连续性',line:2,text:'“先核对日期。”他说。',reason:'日期来源待核实'}],blocking_gates:['连续性']},items:[{项:'日期来源',判定:'待核实',证据:'第二段未说明账册日期'}],consistency:[],revise_instructions:['补充日期来源'],counts:{已完成:1,未完成:0,待核实:1},ai_used:true});
    if (tail === '/quality-matrix') return reply({project_id:projectId,project_name:book.name,columns:['字数','连续性'],chapters:[{...state.chapters[projectId][0],cells:{字数:'通过',连续性:'未通过'},debt:[]}],summary:{通过:1,未通过:1}});
    if (tail === '/debts') return reply([{id:601,rel_path:chapterPath,level:'债-1',status:'open',note:'核对账册日期',created_at:timestamp}]);
    if (tail === '/style/fingerprints') return reply({samples:[{id:1,rel_path:chapterPath,approved:true,han:2450,created_at:timestamp,sentence_len_mean:20,sentence_len_cv:.4,dialog_ratio:.3,dash_per_1000:0}],reference:{sentence_len_mean:20}});
    if (path === '/proposals/count') return reply({pending:state.proposals.filter(item=>item.status==='pending').length});
    if (path === '/proposals') return reply(state.proposals.filter(item=>(!url.searchParams.get('project_id')||item.project_id===Number(url.searchParams.get('project_id')))&&(!url.searchParams.get('status')||item.status===url.searchParams.get('status'))));
    if (path === '/proposals/apply-batch') {const ids=body.ids||[];const results=ids.map((id,index)=>{const proposal=state.proposals.find(item=>item.id===id);if(index===0 && proposal) proposal.status='applied';return {id,ok:index===0,error:index===0?undefined:'验收模拟版本冲突'}});return reply({applied:results.filter(item=>item.ok).length,failed:results.filter(item=>!item.ok).length,results})}
    if (path === '/proposals/discard-batch') {for(const id of body.ids||[]) {const proposal=state.proposals.find(item=>item.id===id);if(proposal)proposal.status='discarded'};return reply({discarded:(body.ids||[]).length,results:(body.ids||[]).map(id=>({id,ok:true}))})}
    const proposalMatch=/^\/proposals\/(\d+)(.*)$/.exec(path);if(proposalMatch){const proposal=state.proposals.find(item=>item.id===Number(proposalMatch[1]));if(proposalMatch[2]==='/apply')proposal.status='applied';if(proposalMatch[2]==='/discard')proposal.status='discarded';if(method==='PUT')proposal.content=body.content;return reply(proposal||{})}
    const workflow = {name:'默认八步产章',description:'先合同、再写作、最后验收',is_builtin:true,batch:{stop_on_failure:true},nodes:[{id:'contract',type:'CONTRACT',label:'章节合同',engine:'',model:'',skill:''},{id:'write',type:'WRITE',label:'写作',engine:'',model:'',skill:''},{id:'review',type:'REVIEW',label:'验收',engine:'',model:'',skill:''}]};
    if(path==='/workflows')return reply({workflows:[workflow],node_types:['CONTRACT','WRITE','REVIEW']});
    if(path==='/workflows/runs/list')return reply(state.workflowRuns.filter(item=>!url.searchParams.get('project_id')||item.project_id===Number(url.searchParams.get('project_id'))));
    if(path==='/workflows/run'){
      const bookChapters=state.chapters[body.project_id]||[];
      let chapters=body.chapters?.length ? [...body.chapters] : body.chapter_start!=null && body.chapter_end!=null ? bookChapters.filter(chapter=>chapter.number>=body.chapter_start&&chapter.number<=body.chapter_end).map(chapter=>chapter.rel_path) : [];
      if(!chapters.length&&bookChapters[0])chapters=[bookChapters[0].rel_path];
      const run={id:Math.max(401,...state.workflowRuns.map(item=>item.id))+1,project_id:body.project_id,status:body.execute?'done':'pending',cursor:0,payload:{workflow:body.workflow,chapters,chapter_index:body.execute?chapters.length:0,node_index:0,completed:body.execute?[...chapters]:[]},created_at:timestamp,updated_at:timestamp,progress:{chapter_index:body.execute?chapters.length:0,node_index:0,chapters_total:chapters.length,completed:body.execute?[...chapters]:[]},log:[]};state.workflowRuns.push(run);return reply(run)
    }
    const workflowMatch=/^\/workflows\/runs\/(\d+)(.*)$/.exec(path);if(workflowMatch){const run=state.workflowRuns.find(item=>item.id===Number(workflowMatch[1]));if(!run)return reply({detail:'合成运行不存在'},404);if(workflowMatch[2]==='/pause')run.status='paused';if(workflowMatch[2]==='/resume'){const remaining=(run.payload?.chapters||[]).filter(path=>!run.progress.completed.includes(path));run.log.push(...remaining.map(chapter=>({at:timestamp,event:'chapter_completed',chapter,steps:{CONTRACT:'done',WRITE:'done',REVIEW:'done'}})));run.progress.completed.push(...remaining);run.payload.completed=[...run.progress.completed];run.payload.chapter_index=run.progress.chapters_total;run.progress.chapter_index=run.progress.chapters_total;run.status='done'}return reply(run)}
    if(path==='/images/providers')return reply({providers:[{id:1,provider_id:'fixture-image',display_name:'验收图片模型',model_id:'fixture-image-model',base_url:'https://fixture.invalid',timeout_seconds:60,enabled:true,health:'ok',has_secret:true,masked_secret:'****',created_at:timestamp,adapter_key:'openai-images',capabilities:{qualities:['auto','high'],formats:['png','jpeg'],moderations:['auto'],max_n:4,supports_edit:true,base_resolutions:['1K','2K'],ratios:['1:1','2:3'],min_side:256,max_side:4096}}]});
    const imageJobMatch=/^\/images\/jobs\/(\d+)(.*)$/.exec(path);if(imageJobMatch){const job=state.imageJobs.find(item=>item.id===Number(imageJobMatch[1]));if(imageJobMatch[2]==='/cancel'&&job)job.status='cancelled';return reply({job})}
    if(path==='/images/jobs')return reply({jobs:state.imageJobs});
    if(path==='/images/records')return reply({records:state.imageRecords.filter(item=>(!url.searchParams.get('favorite')||item.favorite)&&(!url.searchParams.get('search')||item.prompt.includes(url.searchParams.get('search'))))});
    const imageMatch=/^\/images\/records\/(\d+)$/.exec(path);if(imageMatch){const record=state.imageRecords.find(item=>item.id===Number(imageMatch[1]));if(method==='PATCH')Object.assign(record,body);if(method==='DELETE')state.imageRecords=state.imageRecords.filter(item=>item!==record);return reply({record})}
    if(path==='/images/generate'){const job={id:602,provider_id:body.provider_id,model_id:'fixture-image-model',mode:'generate',prompt:body.prompt,params:body,status:'failed',error:'验收模拟模型离线',created_at:timestamp,finished_at:timestamp,duration_ms:20};state.imageJobs.push(job);return reply({job})}
    if(path==='/chat/quick-cards')return reply([{key:'outline',label:'讨论大纲',prompt:'完善本书大纲',task_type:'大纲规划'}]);
    if(path==='/chat/sessions' && method==='GET')return reply(state.sessions.filter(item=>!url.searchParams.get('project_id')||item.project_id===Number(url.searchParams.get('project_id'))));
    if(path==='/chat/sessions' && method==='POST'){const session={id:state.sessions.length+801,project_id:body.project_id,title:body.title||'新对话',agent:'writing-assistant',provider_id:'fixture-provider',model_id:'fixture-large',auto_apply:1,agent_pinned:0,permission_mode:'auto',discussion_only:false,created_at:timestamp,updated_at:timestamp,...body};state.sessions.push(session);state.messages[session.id]=[];return reply(session)}
    const sessionMatch=/^\/chat\/sessions\/(\d+)(.*)$/.exec(path);if(sessionMatch){const id=Number(sessionMatch[1]);const session=state.sessions.find(item=>item.id===id);if(sessionMatch[2]==='' && method==='PATCH')Object.assign(session,body.patch);if(sessionMatch[2]==='' && method==='DELETE'){state.sessions=state.sessions.filter(item=>item.id!==id);return reply({id})}if(sessionMatch[2]==='/runs'){const runId=`fixture-run-${requests.length}`;const run={id:runId,session_id:id,project_id:session?.project_id,status:'completed',text:'依据已核对，可继续讨论。',last_seq:2,steps:[],changes:[],interactions:[],created_at:timestamp,finished_at:timestamp};state.runs[runId]=run;state.messages[id]=(state.messages[id]||[]).concat([{id:requests.length,role:'user',content:body.text,created_at:timestamp,meta:{}},{id:requests.length+1,role:'assistant',content:run.text,created_at:timestamp,meta:{run_id:runId}}]);state.events[runId]=[{seq:1,event:'delta',run_id:runId,session_id:id,text:run.text},{seq:2,event:'done',run_id:runId,session_id:id,status:'completed'}];return reply(run)}return reply({...session,messages:state.messages[id]||[],active_run:null})}
    const runMatch=/^\/chat\/runs\/([^/]+)(.*)$/.exec(path);if(runMatch){const run=state.runs[runMatch[1]];if(runMatch[2]==='/events'){const events=state.events[runMatch[1]]||[];return route.fulfill({status:200,contentType:'text/event-stream',body:events.filter(event=>event.seq>Number(url.searchParams.get('after')||0)).map(event=>`data: ${JSON.stringify(event)}\n\n`).join('')})}if(runMatch[2]==='/cancel' && run)run.status='cancelled';return reply(run||{detail:'fixture run not found'},run?200:404)}
    if(tail==='/knowledge' || tail==='/knowledge/sync')return reply({project_id:projectId,knowledge_key:`fixture-book-${projectId}`,project_name:book.name,db_path:'synthetic',documents:[{id:`doc-${projectId}`,rel_path:materialPath,title:'人物设定',kind:'setting',status:'indexed',chunk_count:1,index_status:'indexed'}],counts:{documents:1,chunks:1,nodes:1,edges:0,indexed_documents:1,eligible_documents:1,excluded_documents:0},job:{status:'ready',progress:1},model:{name:'fixture-keyword',ready:true,mode:'local'},generation_model:{name:'fixture-large',ready:true},vector_ready:true,graph_version:'fixture-v1'});
    const knowledgeNode={id:`hero-${projectId}`,kind:'character',label:hero,source:'author',document:materialPath,evidence_ids:[`evidence-${projectId}`],description:`${hero}是记录者。`,documents:[materialPath],source_location:{rel_path:materialPath,line_start:3,line_end:4,document_hash:'fixture-hash',evidence_id:`evidence-${projectId}`}};
    if(tail==='/knowledge/graph')return reply({project_id:projectId,knowledge_key:`fixture-book-${projectId}`,nodes:[knowledgeNode],edges:[],truncated:false,count:1,document_nodes:{[materialPath]:[knowledgeNode.id]},graph_version:'fixture-v1'});
    if(tail==='/knowledge/entities')return reply({project_id:projectId,knowledge_key:`fixture-book-${projectId}`,nodes:[knowledgeNode],count:1});
    if(tail==='/knowledge/candidates')return reply({project_id:projectId,knowledge_key:`fixture-book-${projectId}`,candidates:[],counts:{pending:0,conflict:0,staged:0,stale:0,applied:0,ignored:0}});
    if(tail==='/knowledge/jobs')return reply({jobs:[]});
    if(tail.startsWith('/knowledge/evidence/'))return reply({id:`evidence-${projectId}`,evidence_id:`evidence-${projectId}`,text:`${hero}是记录者。`,rel_path:materialPath,line_start:3,line_end:4,source:'author',source_tier:'author',document_hash:'fixture-hash',node_ids:[knowledgeNode.id]});
    if(tail==='/vector/model')return reply({config:{mode:'local'},effective:{mode:'local'},inherited:true});
    state.unknown.push(entry); return reply({detail:`Synthetic fixture missing: ${key}`},503);
  });
  return { state, requests };
}
module.exports = { installFixtures, createFixtureState, chapterPath, materialPath };
