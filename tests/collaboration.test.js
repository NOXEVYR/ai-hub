'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../frontend/collaboration.js');
const status=()=>({root:'D:/Studio',available:true,protocol_version:1,tasks:[{id:'t1',title:'任务一',project:'Film',status:'queued',paths:{work:'D:/Studio/Film'}}],artifacts:[{id:'r1',task_id:'t1',kind:'report',title:'验收报告',path:'D:/Studio/report.md',status:'active',pinned:false}],memories:[],clients:[],policy:{enabled:true,days:7},integrations:['codex','zcode','workbuddy','dsh'].map(tool=>({tool,mcp_config:{mcpServers:{aihub:{args:['--tool',tool,'--client-id',tool+'-local']}}}}))});
function harness(overrides={}) {
  const nodes=new Map(),requests=[],actionNodes=[],fields=[{disabled:false}],panels=['tasks','memories','sources','connect','retention'].map(id=>({dataset:{coPanel:id},hidden:false}));
  const tabs=panels.map(n=>({dataset:{coTab:n.dataset.coPanel},setAttribute(k,v){this[k]=v;}}));
  const get=selector=>{if(!nodes.has(selector))nodes.set(selector,{id:selector.replace('#',''),type:selector==='#co-policy-enabled'?'checkbox':'text',value:({'#co-task-tool':'any','#co-memory-scope':'project','#co-source-tool':'any','#co-mcp-tool':'codex'})[selector] || '',checked:false,disabled:false,innerHTML:'',textContent:'',focus(){this.focused=true;}});return nodes.get(selector);};
  const el={isConnected:true,dataset:{},innerHTML:'',classList:{add(){}},querySelector:get,querySelectorAll(s){
    if(s==='[data-co-panel]')return panels;
    if(s==='[data-co-tab]')return tabs;
    if(s==='[data-co-action]')return actionNodes;
    if(s==='fieldset.co-fields')return fields;
    if(s.includes('#co-refresh'))return [...actionNodes,get('#co-refresh'),get('#co-preview')];
    return [];
  }};
  const api=async(url,opts)=>{requests.push({url,body:opts?.body});const val=Object.hasOwn(overrides,url)?overrides[url]:url==='/api/harnesses'?{root:'D:/Studio',items:['codex','zcode','workbuddy','dsh'].map(id=>({id,name:id,enabled:true,connection_mode:'mcp_stdio'}))}:url.startsWith('/api/harnesses/config?')?{config:{mcpServers:{aihub:{args:['--client-id',new URLSearchParams(url.split('?')[1]).get('client_id')]}}}}:url.endsWith('/status')?status():{items:[]};return typeof val==='function'?val(opts?.body):val;};
  const page=ui.createPage({api,heading:()=>''});
  return {el,page,key:id=>get('#co-'+id),requests,tabs,panels,fields,actions:actionNodes,submit:id=>get('#co-'+id+'-form').onsubmit({preventDefault(){}})};
}

test('untrusted file names, memory content, JSON and errors are escaped in every table',()=>{
  const evil='<img src=x onerror="alert(1)">';
  for(const html of [ui.tasksHTML([{id:evil,title:evil}]),ui.artifactsHTML([{id:evil,title:evil,path:evil,status:'active'}]),ui.memoriesHTML([{id:evil,title:evil,content:evil,status:'candidate'}]),ui.sourcesHTML([{id:evil,path:evil,error:evil}]),ui.previewHTML({items:[{path:evil}],protected:[{path:evil,reason:evil}],errors:[evil]}),ui.inventoryHTML({items:[{path:evil}],errors:[evil]})]){
    assert(!html.includes('<img'));assert(html.includes('&lt;img'));assert(!html.includes('data-co-id="<'));
  }
});

test('initial state reads only, exposes controlled cleanup boundary and source-specific MCP identities',async()=>{
  const h=harness();await h.page(h.el);
  assert.equal(h.key('policy-enabled').checked,true);assert.equal(h.key('policy-days').value,'7');
  assert(h.requests.every(r=>r.url.endsWith('/status') || r.url.endsWith('/source_list') || r.url==='/api/harnesses'));
  assert.match(h.el.innerHTML,/只影响新登记/);assert.match(h.el.innerHTML,/回收失败保留/);assert.match(h.el.innerHTML,/各工具原生记忆不会被读取或修改/);
  h.el.querySelector('#hc-config-tool').value='workbuddy';h.el.querySelector('#hc-config-tool').onchange();await h.el.querySelector('#hc-config-load').onclick();
  assert.match(h.el.querySelector('#hc-config-result').innerHTML,/workbuddy-local/);assert(!h.el.querySelector('#hc-config-result').innerHTML.includes('codex-local'));
});

test('task submission sends contract payload; failures preserve all draft fields',async()=>{
  let fail=true;const h=harness({'/api/collaboration/task_create':body=>{if(fail)throw new Error('项目路径被拒绝');return {id:'new',...body};}});
  await h.page(h.el);h.key('task-project').value='Film';h.key('task-title').value='验收';h.key('task-description').value='重要说明';h.key('task-tool').value='dsh';
  await h.submit('task');assert.equal(h.key('task-title').value,'验收');assert.match(h.key('error').textContent,/路径被拒绝/);assert.equal(h.fields[0].disabled,false);
  assert.deepEqual(h.requests.find(r=>r.url.endsWith('task_create')).body,{project:'Film',title:'验收',description:'重要说明',target_tool:'dsh',_workspace_root:'D:/Studio'});
  fail=false;await h.submit('task');assert.equal(h.key('task-title').value,'');assert.equal(h.key('task-project').value,'Film');
});

test('tab switching and navigation snapshot preserve unsaved memory, source and policy drafts',async()=>{
  const h=harness();await h.page(h.el);h.key('memory-content').value='未提交的长期约定';h.key('source-path').value='D:/Reports';h.key('policy-days').value='21';h.key('policy-enabled').checked=false;
  h.tabs.find(t=>t.dataset.coTab==='memories').onclick();assert.equal(h.key('memory-content').value,'未提交的长期约定');assert.equal(h.panels.find(p=>p.dataset.coPanel==='tasks').hidden,true);
  const saved=ui.capture(h.el),next=harness();await next.page(next.el,null,saved);
  assert.equal(next.el.dataset.coTab,'memories');assert.equal(next.key('memory-content').value,'未提交的长期约定');assert.equal(next.key('source-path').value,'D:/Reports');assert.equal(next.key('policy-days').value,'21');assert.equal(next.key('policy-enabled').checked,false);
  assert.equal(next.requests.length,3);
});

test('detached async responses never clear drafts or modify stale page',async()=>{
  let resolve;const h=harness({'/api/collaboration/task_create':()=>new Promise(r=>{resolve=r;})});
  await h.page(h.el);h.key('task-title').value='仍需保留';const pending=h.submit('task');h.el.isConnected=false;const message=h.key('message').textContent;resolve({id:'new'});await pending;
  assert.equal(h.key('task-title').value,'仍需保留');assert.equal(h.key('message').textContent,message);assert.equal(h.requests.filter(r=>r.url.endsWith('/status')).length,1);
});

test('unavailable or disconnected service is visible and write controls stay disabled',async()=>{
  const h=harness({'/api/collaboration/status':{available:false}});await h.page(h.el);
  assert.match(h.key('availability').innerHTML,/工作区不可用/);assert.equal(h.fields[0].disabled,true);await h.submit('task');assert(!h.requests.some(r=>r.url.endsWith('task_create')));
  const broken=harness({'/api/collaboration/status':()=>{throw new Error('服务未连接');}});await broken.page(broken.el);assert.equal(broken.key('error').textContent,'服务未连接');assert.equal(broken.key('refresh').disabled,false);
});

test('memory submission validates project and uses mandatory report provenance; search only uses approved endpoint',async()=>{
  const h=harness();await h.page(h.el);h.key('memory-title').value='约定';h.key('memory-content').value='规则';h.key('memory-source').value='r1';
  await h.submit('memory');assert.match(h.key('error').textContent,/项目名称/);assert(!h.requests.some(r=>r.url.endsWith('memory_propose')));
  h.key('memory-project').value='Film';await h.submit('memory');assert.deepEqual(h.requests.find(r=>r.url.endsWith('memory_propose')).body,{title:'约定',content:'规则',scope:'project',project:'Film',source_artifact_id:'r1',_workspace_root:'D:/Studio'});
  h.key('search-query').value='规则';await h.submit('search');assert(h.requests.some(r=>r.url.endsWith('memory_search') && r.body.query==='规则'));
});

test('pin and memory approval send explicit UI actions and never implicit memory import',async()=>{
  const h=harness();h.actions.push({dataset:{coAction:'pin',coId:'r1'}},{dataset:{coAction:'approve',coId:'m1'}});await h.page(h.el);
  await h.actions[0].onclick();await h.actions[1].onclick();
  assert.deepEqual(h.requests.find(r=>r.url.endsWith('artifact_pin')).body,{artifact_id:'r1',pinned:true,_workspace_root:'D:/Studio'});assert.deepEqual(h.requests.find(r=>r.url.endsWith('memory_review')).body,{memory_id:'m1',status:'approved',_workspace_root:'D:/Studio'});
  assert(!h.requests.some(r=>r.url.endsWith('memory_propose')));
});

test('retention requires preview; policy changes invalidate preview and failed recycle displays preserved errors',async()=>{
  const h=harness({'/api/collaboration/retention_preview':{items:[{id:'a',path:'D:/Temp/a.md'}],protected:[{path:'D:/Temp/b.md',reason:'内容变化'}]},'/api/collaboration/retention_run':{checked:1,recycled:0,items:[{path:'D:/Temp/a.md',status:'error',error:'回收站不可用'}]}});
  await h.page(h.el);assert.equal(h.key('run').disabled,true);await h.key('preview').onclick();assert.equal(h.key('run').disabled,false);assert.match(h.key('preview-results').innerHTML,/内容变化/);
  h.key('policy-days').value='10';h.key('policy-days').oninput();assert.equal(h.key('run').disabled,true);
  await h.key('preview').onclick();await h.key('run').onclick();assert.match(h.key('run-results').innerHTML,/回收失败，已保留/);assert.match(h.key('run-results').innerHTML,/回收站不可用/);assert.equal(h.key('run').disabled,true);
  h.key('policy-days').value='366';await h.submit('policy');assert(!h.requests.some(r=>r.url.endsWith('retention_policy')));
});

test('changed workspace preserves text but clears report selection and displays warning',async()=>{
  const h=harness();await h.page(h.el,null,{root:'E:/Old',draft:{'memory-content':'保留文字','memory-source':'old-report'}});
  assert.equal(h.key('memory-content').value,'保留文字');assert.equal(h.key('memory-source').value,'');assert.match(h.key('error').textContent,/工作环境已切换/);
});

test('every UI action pins its workspace; stale-root rejection preserves the draft without retrying elsewhere',async()=>{
  let current=status();
  const h=harness({'/api/collaboration/status':()=>current,'/api/collaboration/task_create':body=>{
    assert.equal(body._workspace_root,'D:/Studio');throw new Error('工作环境已切换，请刷新后核对。');
  }});
  await h.page(h.el);current={...status(),root:'E:/Other'};h.key('task-title').value='旧工作区的任务';
  await h.submit('task');
  assert.equal(h.key('task-title').value,'旧工作区的任务');assert.match(h.key('error').textContent,/工作环境已切换/);
  assert.equal(h.requests.filter(r=>r.url.endsWith('task_create')).length,1);
  for(const request of h.requests.filter(r=>r.body))assert.equal(request.body._workspace_root,'D:/Studio');
});

test('interrupted active task can only release its lease after explicit inline confirmation',async()=>{
  const current=status();current.tasks[0].status='active';
  const h=harness({'/api/collaboration/status':current});
  h.actions.push({dataset:{coAction:'requeue',coId:'t1'}},{dataset:{coAction:'requeue-confirm',coId:'t1'}},{dataset:{coAction:'requeue-cancel',coId:'t1'}});
  await h.page(h.el);
  await h.actions[1].onclick();assert(!h.requests.some(r=>r.url.endsWith('task_requeue')));
  h.actions[0].onclick();assert.match(h.key('requeue-confirm').innerHTML,/原领取凭证立即失效/);assert.match(h.key('requeue-confirm').innerHTML,/任务目录和已登记产物会保留/);
  assert(!h.requests.some(r=>r.url.endsWith('task_requeue')));
  h.actions[2].onclick();assert.equal(h.key('requeue-confirm').innerHTML,'');assert(!h.requests.some(r=>r.url.endsWith('task_requeue')));
  h.actions[0].onclick();await h.actions[1].onclick();
  assert.deepEqual(h.requests.find(r=>r.url.endsWith('task_requeue')).body,{task_id:'t1',summary:'用户在管理界面释放中断任务',_workspace_root:'D:/Studio'});
  assert.equal(h.key('requeue-confirm').innerHTML,'');
  assert(!ui.tasksHTML([{id:'q',status:'queued'}]).includes('释放领取'));
  assert(!ui.tasksHTML([{id:'c',status:'completed'}]).includes('释放领取'));
});

test('persisted source scan completeness and document counts remain visible after status refresh',async()=>{
  const source={id:'s1',path:'D:/Reports',label:'Codex 报告',tool:'codex',scanned_at:1750000000,file_count:148,truncated:1};
  let sources=[source];
  const h=harness({'/api/collaboration/status':()=>({...status(),sources}),'/api/collaboration/source_list':()=>({items:sources})});
  await h.page(h.el);
  assert.match(h.key('sources').innerHTML,/148 份文档/);
  assert.match(h.key('sources').innerHTML,/未完整（达到扫描预算或部分不可读）/);
  assert(!h.key('sources').innerHTML.includes('已完成本次盘点'));
  await h.key('refresh').onclick();
  assert.match(h.key('sources').innerHTML,/未完整（达到扫描预算或部分不可读）/);
  sources=[{...source,file_count:152,truncated:0}];await h.key('refresh').onclick();
  assert.match(h.key('sources').innerHTML,/152 份文档/);assert.match(h.key('sources').innerHTML,/已完成本次盘点/);
  assert(!h.key('sources').innerHTML.includes('未完整'));
  const unscanned=ui.sourcesHTML([{id:'new',path:'D:/New',scanned_at:null,file_count:0,truncated:0}]);
  assert.match(unscanned,/未盘点/);assert(!unscanned.includes('已完成本次盘点'));
  for(const truncated of [true,1,false,0]){
    const html=ui.sourcesHTML([{...source,truncated}]);
    assert.equal(html.includes('未完整（达到扫描预算或部分不可读）'),Boolean(truncated));
    assert.equal(html.includes('已完成本次盘点'),!truncated);
  }
});
