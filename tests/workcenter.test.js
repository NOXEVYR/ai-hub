const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/workcenter.js');
const doc={id:'d1',title:'报告',path:'D:/Studio/report.md',project_id:'p1',project_name:'电影',project_origin:'inferred',tool:'codex',category:'report',intake_status:'indexed',status:'available'};
function harness(handler){const nodes=new Map(),requests=[];const get=s=>{if(!nodes.has(s))nodes.set(s,{value:'',textContent:'',innerHTML:'',disabled:false,dataset:{},querySelector:selector=>get(selector),querySelectorAll:()=>[],setAttribute(){}});return nodes.get(s);};const el={isConnected:true,dataset:{},innerHTML:'',classList:{add(){}},querySelector:get,querySelectorAll:()=>[]};const api=async(url,opts)=>{requests.push({url,body:opts?.body});if(handler){const val=await handler(url,opts?.body);if(val!==undefined)return val;}if(url.startsWith('/api/workcenter/documents?'))return{items:[doc],total:65,page:Number(new URLSearchParams(url.split('?')[1]).get('page')),page_size:30,workspace_root:'D:/Studio',facets:{projects:[{value:'p1',label:'电影',count:65}]},coverage:{sources:[]}};if(url.startsWith('/api/workcenter/document?'))return{...doc,preview_supported:true,content:'<script>danger()</script>'};return{};};return{el,requests,key:id=>get('#wc-'+id),page:ui.createReports({api,copyPath:async()=>{}}),api};}
const submit=()=>({preventDefault(){}});

test('report coverage keeps unreadable registry and legacy discovery warnings visible even with zero documents',()=>{
  const html=ui.coverageHTML({catalog_documents:0,sources:[],warnings:['登记不可读 <script>','部分目录不可访问'],legacy_discovery_partial:true});
  assert.match(html,/可查阅文档 · 0 份/);assert.match(html,/is-partial/);assert.match(html,/登记不可读 &lt;script&gt;/);assert.match(html,/部分目录不可访问/);assert.match(html,/既有资料发现未完整/);assert(!html.includes('<script>'));
  assert.match(ui.coverageHTML({project_discovery_partial:true}),/is-partial/);
});

test('optional templates do not appear as report sources without registration or historical documents',async()=>{
  const h=harness(url=>url==='/api/harnesses'?{items:[],templates:[{id:'codex',name:'Codex'}]}:url.startsWith('/api/workcenter/documents?')?{items:[],total:0,page:1,page_size:30,workspace_root:'D:/Studio',facets:{tools:[]},coverage:{sources:[]}}:undefined);await h.page(h.el);
  assert(!h.key('tools').innerHTML.includes('codex'));assert.match(h.key('tools').innerHTML,/全部来源/);assert(!h.requests.some(r=>r.body));
});
test('workcenter escapes metadata, preview boundaries and reports incomplete coverage',()=>{const bad='<img src=x onerror=x>';for(const html of [ui.documentsHTML([{...doc,title:bad,path:bad,project_name:bad}],''),ui.projectCards([{name:bad,path:bad}]),ui.coverageHTML({sources:[{label:bad,path:bad,truncated:1}],partial_sources:1,unavailable_sources:1,project_discovery_partial:true})]){assert(!html.includes('<img'));assert(html.includes('&lt;img'));}const html=ui.coverageHTML({sources:[{label:'训练',truncated:1,file_count:2000,visible_documents:4,excluded_documents:1996},{label:'缺失',scanned_at:123,status:'unavailable'}],partial_sources:1,unavailable_sources:1});assert.match(html,/未完整/);assert.match(html,/不可访问/);assert.match(html,/1996 份已排除/);assert.match(html,/不代表全机项目已纳入/);});
test('server filters and pagination use confirmed query names and preserve query text',async()=>{const h=harness();await h.page(h.el);h.key('query').value='Codex 交付';h.key('project').value='p1';h.key('category').value='delivery';h.key('intake').value='indexed';await h.key('filter-form').onsubmit(submit());await h.key('next').onclick();const q=new URLSearchParams(h.requests.filter(r=>r.url.startsWith('/api/workcenter/documents?')).at(-1).url.split('?')[1]);assert.equal(q.get('query'),'Codex 交付');assert.equal(q.get('project_id'),'p1');assert.equal(q.get('category'),'delivery');assert.equal(q.get('intake_status'),'indexed');assert.equal(q.get('page'),'2');assert.equal(q.get('page_size'),'30');});
test('safe reader supports old path links without rendering arbitrary HTML',async()=>{const h=harness();await h.page(h.el,new URLSearchParams({path:doc.path}));assert(h.requests.some(r=>r.url.includes('document?path=')));assert.match(h.key('reader').innerHTML,/&lt;script&gt;/);assert(!h.key('reader').innerHTML.includes('<script>'));assert(!h.requests.some(r=>r.body));await h.key('reveal').onclick();assert.deepEqual(h.requests.at(-1).body,{document_id:'d1',_workspace_root:'D:/Studio'});});
test('manual classification is explicit and failure preserves draft with root guard',async()=>{const h=harness((url)=>{if(url.endsWith('/classify'))throw Error('工作环境不匹配');});await h.page(h.el,new URLSearchParams({document_id:'d1'}));h.key('edit-category').value='plan';h.key('edit-project').value='新显示名';assert(!h.requests.some(r=>r.body));await h.key('classify-form').onsubmit(submit());assert.deepEqual(h.requests.at(-1).body,{document_id:'d1',category:'plan',project_name:'新显示名',_workspace_root:'D:/Studio'});assert.match(h.key('error').textContent,/不匹配/);assert.equal(h.key('edit-project').value,'新显示名');assert.equal(h.key('save-classification').disabled,false);});
test('navigation snapshot restores filters, selected doc and classification drafts',async()=>{const h=harness();await h.page(h.el,new URLSearchParams({document_id:'d1'}));h.key('query').value='未提交搜索';h.key('edit-project').value='未保存名';h.key('edit-category').value='reference';const snapshot=ui.capture(h.el);const next=harness();await next.page(next.el,new URLSearchParams(),snapshot);assert.equal(next.key('query').value,'未提交搜索');assert.equal(next.key('edit-project').value,'未保存名');assert.equal(next.key('edit-category').value,'reference');});
test('workspace switch clears stale selection before any document read or write',async()=>{const h=harness();await h.page(h.el,new URLSearchParams(),{root:'E:/Old',selected:'old',query:'保留'});assert.match(h.key('error').textContent,/工作环境已切换/);assert.equal(h.key('query').value,'保留');assert(!h.requests.some(r=>r.url.includes('/document?')));assert.equal(h.el.dataset.wcSelected,'');});
test('late responses leave disconnected views untouched and failures allow retry',async()=>{let resolve;const wait=new Promise(r=>resolve=r);const h=harness(()=>wait);const pending=h.page(h.el);h.el.isConnected=false;h.key('count').textContent='new';resolve({items:[]});await pending;assert.equal(h.key('count').textContent,'new');const f=harness(()=>{throw Error('断开');});await f.page(f.el);assert.match(f.key('error').textContent,/断开/);assert.equal(f.key('refresh').disabled,false);});
test('project index preserves the legacy registry and exposes server pagination',async()=>{let called=0;const h=harness(url=>url.startsWith('/api/workcenter/projects?')?{items:[],total:80,page_size:30}:undefined);const page=ui.createProjects({api:h.api,nav:()=>{},legacyPage:async()=>called++});await page(h.el,new URLSearchParams({view:'registry'}),{legacy:{draft:'x'}});assert.equal(called,1);assert.equal(h.el.dataset.wcView,'registry');await page(h.el);assert(h.requests.some(r=>r.url.endsWith('page=1&page_size=30')));});

test('report filters include custom and disabled work-ends without hiding historical documents',async()=>{
  const h=harness(url=>url==='/api/harnesses'?{items:[{id:'custom-worker',name:'自定义工作端',enabled:true},{id:'codex',name:'历史 Codex',enabled:false}]}:undefined);
  await h.page(h.el);assert.match(h.key('tools').innerHTML,/custom-worker/);assert.match(h.key('tools').innerHTML,/历史 Codex（已停用）/);assert.match(h.key('list').innerHTML,/报告/);
});

test('classification provenance never treats indexed or submitted as human verification',()=>{
  assert.equal(ui.classification(doc).status,'needs_review');
  assert.equal(ui.classification({...doc,category_manual:true}).source,'manual');
  const submitted={...doc,category_source:'submitted',classification_status:'classified',intake_status:'registered'};
  assert.match(ui.documentsHTML([submitted],''),/AI 提交声明/);
  assert.match(ui.documentsHTML([submitted],''),/未人工核验/);
  assert(!ui.documentsHTML([submitted],'').includes('人工校正'));
  assert.equal(ui.classification({...submitted,status:'changed'}).status,'needs_review');
  assert.match(ui.documentsHTML([{...submitted,submission_evidence:'snapshot_changed',status:'changed'}],''),/原提交文件已变化/);
});

test('purpose, submitter, intake and retention remain separate in reader and list',async()=>{
  const record={...doc,category:'delivery',category_source:'submitted',classification_status:'classified',retention:'temp_expiring',retention_managed:true,expires_at:'2026-10-10T00:00:00Z',intake_status:'registered',task_id:'t123'};
  const html=ui.documentsHTML([record],'');assert.match(html,/交付说明/);assert.match(html,/AI 提交声明/);assert.match(html,/临时 ·/);assert.match(html,/到期/);
  assert.equal(ui.retentionDescription({...doc,retention:'retained',retention_managed:false}),'原位置保留 · 不自动清理');
  assert.equal(ui.retentionDescription({...record,retention:'temp_pinned'}),'临时 · 已固定保留');
  const h=harness(url=>url.startsWith('/api/workcenter/document?')?{...record,preview_supported:true,content:'正文'}:undefined);
  await h.page(h.el,new URLSearchParams({document_id:'d1'}));
  assert.match(h.key('reader').innerHTML,/收录方式 \/ 保留状态/);assert.match(h.key('reader').innerHTML,/任务 t123/);
});

test('unselected reader is hidden, closing restores full list and preserves category draft',async()=>{
  const h=harness();await h.page(h.el);assert.equal(h.key('reader').hidden,true);
  await h.page(h.el,new URLSearchParams({document_id:'d1'}));assert.equal(h.key('reader').hidden,false);
  h.key('edit-category').value='reference';h.key('edit-project').value='草稿项目';await h.key('close-reader').onclick();
  assert.equal(h.key('reader').hidden,true);assert.equal(h.el.dataset.wcSelected,'');
  const snapshot=ui.capture(h.el);assert.deepEqual(snapshot.drafts.d1,{category:'reference',project_name:'草稿项目'});
  const next=harness();await next.page(next.el,new URLSearchParams(),{...snapshot,selected:'d1'});assert.equal(next.key('edit-project').value,'草稿项目');
});

test('close reader invalidates a delayed document response',async()=>{
  let resolve;const waiting=new Promise(r=>resolve=r);const h=harness(url=>url.startsWith('/api/workcenter/document?')?waiting:undefined);
  const pending=h.page(h.el,new URLSearchParams({document_id:'d1'}));
  while(!h.key('close-reader').onclick)await new Promise(r=>setImmediate(r));
  h.key('close-reader').onclick();resolve({...doc,preview_supported:true,content:'迟到正文'});await pending;
  assert.equal(h.key('reader').hidden,true);assert.equal(h.key('reader').innerHTML,'');assert.equal(h.el.dataset.wcReaderReady,'false');
});

test('needs-review quick filter uses full returned facet count and preserves pagination/root state',async()=>{
  const h=harness(url=>url.startsWith('/api/workcenter/documents?')?{items:[doc],total:12,page:Number(new URLSearchParams(url.split('?')[1]).get('page')),page_size:30,workspace_root:'D:/Studio',facets:{categories:[{value:'report',count:31}],needs_review_count:12},coverage:{sources:[]}}:undefined);
  await h.page(h.el);assert.match(h.key('quick-filters').innerHTML,/已接入范围/);assert.match(h.key('quick-filters').innerHTML,/待确认<span class="wc-filter-count">12/);
  await h.key('review-filter').onclick();let query=new URLSearchParams(h.requests.filter(r=>r.url.startsWith('/api/workcenter/documents?')).at(-1).url.split('?')[1]);
  assert.equal(query.get('classification_status'),'needs_review');assert.equal(query.get('page'),'1');
  const snapshot=ui.capture(h.el);assert.equal(snapshot.classification_status,'needs_review');
  await h.key('reset').onclick();query=new URLSearchParams(h.requests.filter(r=>r.url.startsWith('/api/workcenter/documents?')).at(-1).url.split('?')[1]);assert.equal(query.get('classification_status'),'');
});

test('source tool controls are non-submit buttons in collapsed more filters and guide is optional',async()=>{
  const h=harness();await h.page(h.el);assert.match(h.el.innerHTML,/id="wc-more-filters"/);assert.equal(h.key('more-filters').open,false);
  assert.match(h.key('tools').innerHTML,/type="button"/);assert.match(h.el.innerHTML,/AI 怎样提交分类/);
  h.key('submission-guide').hidden=true;h.key('submission-help').onclick();assert.equal(h.key('submission-guide').hidden,false);
  h.key('submission-help').onclick();assert.equal(h.key('submission-guide').hidden,true);
  assert(!h.requests.some(r=>r.body));
});

test('successful classification refreshes reader provenance instead of leaving stale inferred label',async()=>{
  let category='report',manual=false;
  const h=harness((url,body)=>{if(url.endsWith('/classify')){category=body.category;manual=true;return {saved:true};}if(url.startsWith('/api/workcenter/document?'))return {...doc,category,category_source:manual?'manual':'inferred',classification_status:manual?'classified':'needs_review',preview_supported:true,content:'正文'};});
  await h.page(h.el,new URLSearchParams({document_id:'d1'}));assert.match(h.key('reader').innerHTML,/目录或名称推断/);
  h.key('edit-category').value='plan';h.key('edit-project').value='电影';await h.key('classify-form').onsubmit(submit());
  assert.match(h.key('reader').innerHTML,/计划方案 · 人工校正/);assert.match(h.key('message').textContent,/未移动/);assert.equal(h.key('reader').hidden,false);
});

test('document leaving needs-review list after save retains complete reader metadata from document endpoint',async()=>{
  let manual=false;
  const record={...doc,retention:'temp_expiring',retention_managed:true,expires_at:'2026-10-10T00:00:00Z'};
  const h=harness((url,body)=>{
    if(url.endsWith('/classify')){manual=true;return {saved:true};}
    if(url.startsWith('/api/workcenter/documents?'))return {items:manual?[]:[record],total:manual?0:1,page:1,page_size:30,workspace_root:'D:/Studio',facets:{},coverage:{sources:[]}};
    if(url.startsWith('/api/workcenter/document?'))return {...record,category:manual?'plan':'report',category_source:manual?'manual':'inferred',classification_status:manual?'classified':'needs_review',preview_supported:true,content:'正文'};
  });
  await h.page(h.el,new URLSearchParams({document_id:'d1',classification_status:'needs_review'}));
  h.key('edit-category').value='plan';h.key('edit-project').value='电影';await h.key('classify-form').onsubmit(submit());
  assert.match(h.key('list').innerHTML,/当前条件下没有/);assert.match(h.key('reader').innerHTML,/计划方案 · 人工校正/);
  assert.match(h.key('reader').innerHTML,/电影/);assert.match(h.key('reader').innerHTML,/临时 ·/);
});

test('old minimal document endpoint without list metadata reports unavailable evidence instead of inventing retention',async()=>{
  const h=harness(url=>url.startsWith('/api/workcenter/documents?')?{items:[],total:0,page:1,workspace_root:'D:/Studio',facets:{},coverage:{sources:[]}}:url.startsWith('/api/workcenter/document?')?{id:'d1',title:'报告',path:doc.path,preview_supported:true,content:'正文'}:undefined);
  await h.page(h.el,new URLSearchParams({path:doc.path}));assert.match(h.key('reader').innerHTML,/分类依据未提供/);assert.match(h.key('reader').innerHTML,/保留状态未提供/);
  assert(!h.key('reader').innerHTML.includes('不自动清理'));
});

test('coverage separates total readable catalog from raw indexed source count and hides empty source details',()=>{
  const html=ui.coverageHTML({catalog_documents:35,registered_documents:35,indexed_visible_documents:0,legacy_documents:0,indexed_documents:0,sources:[]});
  assert.match(html,/可查阅文档 · 35 份/);assert.match(html,/协作提交 35 · 来源盘点 0 · 既有资料 0/);
  assert(!html.includes('0 份来源文档已索引'));assert(!html.includes('来源范围与未完成项（0）'));assert(!html.includes('<details'));
  assert.match(html,/管理报告来源/);assert.match(html,/盘点完成不代表分类正确/);
  const old=ui.coverageHTML({indexed_documents:12,sources:[]});assert.match(old,/12 份来源文档已索引/);assert(!old.includes('协作提交'));assert(!old.includes('可查阅文档'));
  const partial=ui.coverageHTML({catalog_documents:10,indexed_documents:99,sources:[{label:'资料',path:'D:/Reports'}],partial_sources:1});assert.match(partial,/可查阅文档 · 10 份/);assert.match(partial,/1 个来源盘点不完整/);assert.match(partial,/<details/);
});

test('report filter and edit controls have exact accessible names independent of option text',async()=>{
  const h=harness();await h.page(h.el,new URLSearchParams({document_id:'d1'}));
  for(const [id,label]of[['project','所属项目'],['category','文档用途'],['classification-status','分类确认'],['intake','收录方式']])assert(h.el.innerHTML.includes(`<select id="wc-${id}" aria-label="${label}">`));
  assert.match(h.key('reader').innerHTML,/<select id="wc-edit-category" aria-label="文档类别">/);
  assert.match(h.key('reader').innerHTML,/<input id="wc-edit-project" aria-label="项目显示名称"/);
});

test('reader puts long file and submission metadata in default collapsed details before content',async()=>{
  const record={...doc,path:'D:/很长的项目路径/多级目录/Reports/完整报告.md',tool:'fold-worker',task_id:'task-00000000-0000-0000-0000-000000000000',artifact_id:'artifact-00000000-0000-0000-0000-000000000000',category_source:'submitted',classification_status:'classified',retention:'temp_expiring',retention_managed:true,expires_at:'2026-10-10T00:00:00Z'};
  const h=harness(url=>url.startsWith('/api/workcenter/document?')?{...record,preview_supported:true,content:'立即可读的正文'}:undefined);await h.page(h.el,new URLSearchParams({document_id:'d1'}));
  const html=h.key('reader').innerHTML,summary=html.slice(html.indexOf('<div class="wc-reader-summary">'),html.indexOf('<details class="wc-file-info">'));
  assert.match(summary,/电影/);assert.match(summary,/fold-worker/);assert.match(summary,/AI 提交声明 · 未人工核验/);assert.match(summary,/保留状态/);assert.match(summary,/临时 ·/);
  assert(!summary.includes(record.path));assert(!summary.includes(record.task_id));assert(!summary.includes(record.artifact_id));
  assert.match(html,/<details class="wc-file-info"><summary>文件位置与提交信息<\/summary>/);assert(!html.includes('<details class="wc-file-info" open'));
  assert(html.includes(record.path));assert(html.includes(record.task_id));assert(html.includes(record.artifact_id));
  assert(html.indexOf('文件位置与提交信息')<html.indexOf('立即可读的正文'));assert(html.indexOf('立即可读的正文')<html.indexOf('wc-classify'));
  assert.match(html,/打开所在文件夹/);assert.match(html,/复制路径/);assert.match(html,/关闭阅读/);
});

test('narrow reader releases its scroll container so sticky title belongs to the page scroller',()=>{
  const css=require('node:fs').readFileSync(require('node:path').join(__dirname,'../frontend/lumacore.css'),'utf8');
  const responsive=css.slice(css.indexOf('@media(max-width:1100px)'),css.indexOf('@media(max-width:650px)',css.indexOf('@media(max-width:1100px)')));
  const reader=responsive.match(/\.workcenter-page\.wc-report-page \.wc-reader\s*\{([^}]+)\}/)?.[1]||'';
  assert.match(reader,/position:static/);assert.match(reader,/max-height:none/);assert.match(reader,/overflow:visible/);
  assert.match(css,/\.workcenter-page\.wc-report-page \.wc-reader \.panel-head\s*\{[^}]*position:sticky;top:0/);
});

test('project and distinct task identity group deliverables without replacing original submitter with current owner',()=>{
  const submitted={...doc,task_id:'task-a',task_title:'修改工具栏',task_status:'active',task_owner_id:'new-owner',task_owner_name:'接手客户端',task_owner_tool:'zcode',source_client_id:'codex-session-a',source_client_name:'Codex 开发窗口 A',source_tool:'codex'};
  const html=ui.groupedDocumentsHTML([submitted,{...submitted,id:'d2',task_id:'task-b',task_status:'completed',source_client_id:'codex-session-b',source_client_name:'Codex 开发窗口 B'}],'');
  assert.equal((html.match(/class="wc-project-group"/g)||[]).length,1);assert.equal((html.match(/class="wc-task-group"/g)||[]).length,2);
  assert.match(html,/本页 2 组 · 2 份/);assert.match(html,/修改工具栏/);assert.match(html,/当前领取：接手客户端/);
  assert.match(html,/提交客户端：Codex 开发窗口 A/);assert.match(html,/提交客户端：Codex 开发窗口 B/);
  const row=ui.documentsHTML([submitted],'');assert(!row.includes('接手客户端'));assert.match(row,/Codex 开发窗口 A/);
  assert.equal(ui.taskAssignment({...submitted,task_status:'completed'}),'已完成');
  assert(!ui.taskAssignment({...submitted,task_status:'completed'}).includes('未分配'));
});

test('unknown historical attribution is explicit and real unknown-like client IDs remain actual submitters',()=>{
  const history={...doc,task_id:null,source_client_id:null,task_owner_name:'不应成为作者',task_target_tool:'codex'};
  const html=ui.groupedDocumentsHTML([history,{...history,id:'d2',task_id:'__unknown__',task_title:'真实任务',source_client_id:'__unknown__',source_client_name:'真实客户端'}],'');
  assert.match(html,/未关联任务的资料/);assert.match(html,/历史资料，不推测任务或作者/);assert.match(html,/提交客户端未记录/);
  assert.match(html,/真实任务/);assert.match(html,/提交客户端：真实客户端/);assert(!html.includes('不应成为作者'));
  assert.equal((html.match(/class="wc-task-group"/g)||[]).length,2);
});

test('same display names with separate stable client IDs are distinguishable without merging authors',()=>{
  const a={...doc,source_client_name:'Codex',source_client_id:'worker-00000001',source_tool:'codex'},b={...a,id:'d2',source_client_id:'worker-00000002'};
  const html=ui.documentsHTML([a,b],'');assert.match(html,/Codex（编号末段 00000001）/);assert.match(html,/Codex（编号末段 00000002）/);
  assert.equal(ui.submitterDescription({...a,source_client_id:null}),'提交客户端未记录');
  const collision=ui.documentsHTML([{...a,source_client_id:'worker-A-same-end'},{...b,source_client_id:'worker-B-same-end'}],'');assert.match(collision,/A-same-end/);assert.match(collision,/B-same-end/);
});

test('group values and all attribution names escape markup, groups start collapsed and selected task expands',()=>{
  const evil='<img src=x onerror=bad>',record={...doc,project_id:evil,project_name:evil,task_id:evil,task_title:evil,task_status:'active',task_owner_id:'owner',task_owner_name:evil,task_owner_tool:evil,source_client_id:'client',source_client_name:evil,source_tool:evil};
  const html=ui.groupedDocumentsHTML([record],'');assert(!html.includes('<img'));assert(html.includes('&lt;img'));assert.match(html,/class="wc-task-group" data-wc-group="[^"]+" ><summary>/);
  const selected=ui.groupedDocumentsHTML([record],'d1');assert.match(selected,/class="wc-task-group" data-wc-group="[^"]+" open/);
  const filtered=ui.groupedDocumentsHTML([record],'',{taskFilter:evil});assert.match(filtered,/class="wc-task-group" data-wc-group="[^"]+" open/);
});

test('view switch preserves filters and group state while scope names only current-page documents and tasks with results',async()=>{
  const group={dataset:{wcGroup:JSON.stringify(['task','p1','task-a'])},open:false};
  const h=harness();h.el.querySelectorAll=selector=>selector==='[data-wc-group]'?[group]:[];
  await h.page(h.el);assert.equal(h.el.dataset.wcListView,'grouped');assert.match(h.key('group-range').innerHTML,/仅分组本页 1 份 \/ 筛选结果 65 份/);assert.match(h.key('group-range').innerHTML,/只展示已有文档的任务/);assert.match(h.key('group-range').innerHTML,/#\/collaboration\?tab=tasks/);
  group.open=true;group.ontoggle();await h.key('view-flat').onclick();assert.equal(h.el.dataset.wcListView,'flat');
  assert.match(h.key('group-range').innerHTML,/本页 1 份 \/ 筛选结果 65 份；按文档更新时间排列。只列已收录文档。/);assert(!h.key('group-range').innerHTML.includes('执行中优先'));assert.match(h.key('group-range').innerHTML,/#\/collaboration\?tab=tasks/);
  const captured=ui.capture(h.el);assert.equal(captured.view,'flat');assert.equal(captured.groups[group.dataset.wcGroup],true);
  const next=harness();await next.page(next.el,new URLSearchParams(),captured);assert.equal(next.el.dataset.wcListView,'flat');assert.equal(ui.capture(next.el).groups[group.dataset.wcGroup],true);assert.match(next.key('group-range').innerHTML,/按文档更新时间排列/);assert(!next.key('group-range').innerHTML.includes('执行中优先'));
  assert(!h.requests.some(r=>r.body));
});

test('task and submitter filters use backend facet identities including noncolliding unknown value',async()=>{
  const h=harness(url=>url.startsWith('/api/workcenter/documents?')?{items:[doc],total:1,page:1,page_size:30,workspace_root:'D:/Studio',facets:{tasks:[{value:':unknown:',label:'未关联任务',count:1},{value:'task-a',label:'电影 / 工具栏 · task-a',count:2}],submitters:[{value:'__unknown__',label:'真实客户端 · __unknown__',count:1},{value:':unknown:',label:'提交客户端未记录',count:2}]},coverage:{sources:[]}}:undefined);
  await h.page(h.el);assert.match(h.key('task').innerHTML,/value=":unknown:"/);assert.match(h.key('submitter').innerHTML,/value="__unknown__"/);
  h.key('task').value=':unknown:';h.key('submitter').value='__unknown__';await h.key('filter-form').onsubmit(submit());
  const q=new URLSearchParams(h.requests.filter(r=>r.url.startsWith('/api/workcenter/documents?')).at(-1).url.split('?')[1]);assert.equal(q.get('task_id'),':unknown:');assert.equal(q.get('source_client_id'),'__unknown__');
  const captured=ui.capture(h.el);assert.equal(captured.task_id,':unknown:');assert.equal(captured.source_client_id,'__unknown__');
  await h.key('reset').onclick();assert.equal(h.key('task').value,'');assert.equal(h.key('submitter').value,'');
});

test('reader presents human task title and actual submitter while stable IDs and mutable-name evidence stay folded',async()=>{
  const record={...doc,task_id:'task-long-id',task_title:'校准报告布局',task_status:'active',task_owner_id:'owner-id',task_owner_name:'接手窗口',task_owner_tool:'zcode',source_client_id:'submitter-id',source_client_name:'原提交窗口',source_tool:'codex'};
  const h=harness(url=>url.startsWith('/api/workcenter/document?')?{...record,preview_supported:true,content:'正文'}:undefined);await h.page(h.el,new URLSearchParams({document_id:'d1'}));
  const html=h.key('reader').innerHTML,summary=html.slice(html.indexOf('wc-reader-summary'),html.indexOf('wc-file-info'));
  assert.match(summary,/校准报告布局/);assert.match(summary,/原提交窗口/);assert(!summary.includes('接手窗口'));assert(!summary.includes('task-long-id'));assert(!summary.includes('submitter-id'));
  assert.match(html,/当前任务承接/);assert.match(html,/接手窗口/);assert.match(html,/submitter-id/);assert.match(html,/自报名称，可更改/);assert.match(html,/不代表提交时姓名快照/);
});

test('collapsed completed task summary names actual page submitters and limits preview without using current owner',()=>{
  const record={...doc,task_id:'completed-task',task_title:'已完成的修复',task_status:'completed',task_owner_id:'new-owner',task_owner_name:'不应冒充提交者',source_tool:'codex'};
  const items=['第一窗口','第二窗口','第三窗口'].map((name,i)=>({...record,id:'d'+i,source_client_id:'client-'+i,source_client_name:name}));items.push({...record,id:'old',source_client_id:null});
  const html=ui.groupedDocumentsHTML(items,''),summary=html.slice(html.indexOf('<details class="wc-task-group"'),html.indexOf('<div class="wc-task-documents">'));
  assert.match(summary,/已完成的修复/);assert.match(summary,/已完成/);assert.match(summary,/本页成果提交者：第一窗口/);assert.match(summary,/第二窗口/);assert.match(summary,/另 1 个登记客户端/);assert.match(summary,/提交客户端未记录/);
  assert(!summary.includes('不应冒充提交者'));assert(!summary.includes('第三窗口'));assert(!summary.includes('未分配'));
});

test('active projects and tasks lead grouped view while unlinked history goes last and flat order stays unchanged',()=>{
  const old={...doc,title:'旧资料',project_id:'old',project_name:'历史项目'},done={...doc,id:'done',title:'完成成果',project_id:'done',project_name:'已完成项目',task_id:'done-task',task_title:'已完成任务',task_status:'completed'},active={...doc,id:'active',title:'执行成果',project_id:'active',project_name:'执行项目',task_id:'active-task',task_title:'执行任务',task_status:'active'};
  const html=ui.groupedDocumentsHTML([old,done,active],'');assert(html.indexOf('执行项目')<html.indexOf('已完成项目'));assert(html.indexOf('已完成项目')<html.indexOf('历史项目'));
  const sameProject=ui.groupedDocumentsHTML([{...old,project_id:'p'},{...done,project_id:'p'},{...active,project_id:'p'}],'');assert(sameProject.indexOf('执行任务')<sameProject.indexOf('已完成任务'));assert(sameProject.indexOf('已完成任务')<sameProject.indexOf('未关联任务的资料'));
  const flat=ui.documentsHTML([old,done,active],'');assert(flat.indexOf('旧资料')<flat.indexOf('完成成果'));assert(flat.indexOf('完成成果')<flat.indexOf('执行成果'));
});

test('detached or previous-workspace group toggle cannot repopulate new workspace group preferences',async()=>{
  const group={dataset:{wcGroup:'old-group'},open:true,isConnected:true};let root='D:/Studio';
  const h=harness(url=>url.startsWith('/api/workcenter/documents?')?{items:[doc],total:1,page:1,workspace_root:root,facets:{},coverage:{sources:[]}}:undefined);h.el.querySelectorAll=selector=>selector==='[data-wc-group]'?[group]:[];
  await h.page(h.el);const oldToggle=group.ontoggle;root='E:/New';await h.key('refresh').onclick();oldToggle();assert.deepEqual(ui.capture(h.el).groups,{});
  group.isConnected=false;group.ontoggle();assert.deepEqual(ui.capture(h.el).groups,{});
});

test('closing reader after its task group is collapsed restores focus to the visible list instead of hidden document',async()=>{
  let listFocused=0,hiddenFocused=0;const button={dataset:{wcDocument:'d1'},setAttribute(){},getClientRects:()=>[],focus(){hiddenFocused++;}};
  const h=harness();h.el.querySelectorAll=selector=>selector==='[data-wc-document]'?[button]:[];h.key('list').focus=()=>listFocused++;
  await h.page(h.el,new URLSearchParams({document_id:'d1'}));h.key('close-reader').onclick();assert.equal(listFocused,1);assert.equal(hiddenFocused,0);assert.equal(h.key('reader').hidden,true);
});
