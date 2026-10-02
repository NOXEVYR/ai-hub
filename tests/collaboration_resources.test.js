'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/collaboration.js');
const workspace='D:/Synthetic';
const resource=(state='running',extra={})=>({id:'res-1',task_id:'task-1',task_title:'合成任务',project:'Project',title:'合成标签页',client_id:'synthetic-client',resource_type:'browser_tab',ownership:'task_exclusive',ownership_declared:true,temporary:true,state,last_evidence_at:'2026-10-01T01:00:00+00:00',evidence_source:state==='running'?'registration_only':'client_report',identity:{browser_id:'iab',session_id:'synthetic-run',tab_id:'42'},...extra});
const list=(items=[],extra={})=>({available:true,items,total:items.length,counts:{},workspace_root:workspace,automatic_control:false,host_verified:false,...extra});
function fixture(initial=list(),handlers={}) {
  let state={root:workspace,available:true,tasks:[{id:'task-1',title:'合成任务',status:'completed'}],artifacts:[],memories:[],sources:[],clients:[],policy:{enabled:false,days:7},resources:initial};
  const requests=[],nodes=new Map(),actions=[],panels=['tasks','resources','memories','sources','connect','retention','deleted'].map(id=>({dataset:{coPanel:id},hidden:false}));
  const tabs=panels.map(panel=>({dataset:{coTab:panel.dataset.coPanel},setAttribute(){}}));
  const node=id=>({id,value:id==='co-task-tool'||id==='co-source-tool'?'any':'',checked:false,disabled:false,innerHTML:'',textContent:'',dataset:{},matches:()=>true,closest:()=>null,focus(){}});
  const get=selector=>{if(!nodes.has(selector))nodes.set(selector,node(selector.slice(1)));return nodes.get(selector);};
  for(const [action,id] of [['resource-refresh',''],['resource-next','co-resource-next'],['resource-prev','co-resource-prev'],['resource-task-detail','']]){
    const item=id?get('#'+id):node('');item.dataset={coAction:action,coId:action==='resource-task-detail'?'task-1':''};actions.push(item);
  }
  const el={isConnected:true,dataset:{},innerHTML:'',classList:{add(){}},querySelector:get,querySelectorAll(selector){
    if(selector==='[data-co-action]')return actions;
    if(selector==='[data-co-panel]')return panels;
    if(selector==='[data-co-tab]')return tabs;
    if(selector==='fieldset.co-fields')return [];
    if(selector.includes('#co-refresh'))return [...actions,get('#co-refresh'),get('#co-preview')];
    return [];
  }};
  const api=async(url,options)=>{
    requests.push({url,body:options?.body});
    if(Object.hasOwn(handlers,url))return handlers[url](options?.body);
    if(url.endsWith('/status'))return state;
    if(url==='/api/harnesses')return {root:workspace,items:[]};
    if(url.endsWith('/resource_list'))return list();
    return {items:[]};
  };
  return {el,requests,get,panels,tabs,page:ui.createPage({api,heading:()=>''}),setState:next=>{state=next;},state:()=>state,click:action=>actions.find(item=>item.dataset.coAction===action).onclick()};
}

test('resource states and ownership retain client-report and output boundaries',()=>{
  const items=['running','cleanup_pending','closed','cleanup_failed','manual_required'].map(state=>resource(state));
  items.push(resource('manual_required',{evidence:{outcome:'not_checked'}}),resource('manual_required',{resource_type:'output',temporary:false}),resource('manual_required',{ownership:'user_owned',ownership_declared:false}));
  const html=ui.resourcesHTML(list(items,{counts:{running:19,closed:7}}));
  for(const value of ['运行中 19','客户端报告已收尾 7','待收尾','收尾失败或部分失败','未能检查，需人工处理','归属未声明','正式产物保留，不参与临时收尾','不是曜核执行','不是独立核验'])assert(html.includes(value),value);
  assert(html.includes('仅登记声明'));assert(html.includes('客户端上报证据'));
  assert(!html.includes('data-co-action="resource-close"'));assert(!html.includes('data-co-action="process-kill"'));
});

test('untrusted resource names, identifiers, status data and evidence are escaped',()=>{
  const evil='<svg onload="bad">';
  const item=resource('closed',{id:evil,task_id:evil,task_title:evil,project:evil,title:evil,client_id:evil,resource_type:evil,ownership:evil,last_evidence_at:evil,message:evil,identity:{tab_id:evil},evidence:{detail:evil},sample:{note:evil}});
  const html=ui.resourcesHTML(list([item])),detail=ui.resourceDetailHTML(list([item]),evil);
  for(const rendered of [html,detail]){assert(!rendered.includes('<svg'));assert(rendered.includes('&lt;svg'));assert(!rendered.includes('data-co-id="<'));}
  assert(html.includes('pathline'));assert(detail.includes('co-code'));
});

test('empty and partial results distinguish opt-in registration from complete totals',()=>{
  const empty=ui.resourcesHTML(list());assert.match(empty,/需要接入工具主动登记/);assert.match(empty,/历史任务不会自动出现/);
  const partial=ui.resourcesHTML(list([resource()],{total:5000}),{offset:200});
  assert.match(partial,/当前筛选共 5000 条/);assert.match(partial,/本批展示 201–201 条/);assert.match(partial,/可翻页或按任务筛选查询/);
  const unavailable=ui.resourcesHTML(list([],{available:false}));assert.match(unavailable,/任务资源能力不可用/);
});

test('initial resource tab consumes status without registration or cleanup requests',async()=>{
  const h=fixture(list([resource()]));await h.page(h.el);
  assert.match(h.el.innerHTML,/任务资源与收尾/);assert.match(h.get('#co-resources').innerHTML,/合成标签页/);
  assert.equal(h.requests.length,4);assert(!h.requests.some(request=>request.url.endsWith('/resource_list')));
  h.tabs.find(tab=>tab.dataset.coTab==='resources').onclick();assert.equal(h.el.dataset.coTab,'resources');
  assert.equal(h.panels.find(panel=>panel.dataset.coPanel==='resources').hidden,false);
  assert(h.requests.every(request=>!request.url.endsWith('/resource_register')&&!request.url.endsWith('/resource_cleanup_report')));
});

test('read-only refresh and pagination send bounded workspace queries and use server totals',async()=>{
  const h=fixture(list([resource()],{total:3}),{'/api/collaboration/resource_list':body=>list([resource('closed',{id:'res-'+body.offset})],{total:3,counts:{closed:3}})});
  await h.page(h.el);assert.equal(h.get('#co-resource-prev').disabled,true);assert.equal(h.get('#co-resource-next').disabled,false);
  await h.click('resource-next');assert.deepEqual(h.requests.at(-1).body,{offset:1,_workspace_root:workspace});assert.match(h.get('#co-resources').innerHTML,/本批展示 2–2 条/);
  await h.click('resource-prev');assert.deepEqual(h.requests.at(-1).body,{offset:0,_workspace_root:workspace});
  await h.click('resource-refresh');assert.deepEqual(h.requests.at(-1).body,{offset:0,_workspace_root:workspace});assert.match(h.get('#co-resource-message').textContent,/没有执行资源收尾/);
  assert(h.requests.filter(request=>request.url.includes('resource_')).every(request=>request.url.endsWith('/resource_list')));
});

test('task details query the server, preserve evidence, and restore the task filter',async()=>{
  const h=fixture(list([resource()]),{'/api/collaboration/resource_list':body=>list([resource('closed',{task_id:body.task_id,evidence:{outcome:'absent',detail:'synthetic absence'},sample:{resource_ram_bytes:123}})])});
  await h.page(h.el);await h.click('resource-task-detail');
  assert.deepEqual(h.requests.at(-1).body,{offset:0,task_id:'task-1',_workspace_root:workspace});assert.equal(h.el.dataset.coTab,'resources');assert.equal(h.get('#co-resource-task').value,'task-1');
  assert.match(h.get('#co-resource-detail').innerHTML,/synthetic absence/);assert.match(h.get('#co-resource-detail').innerHTML,/resource_ram_bytes/);
  const saved=ui.capture(h.el);assert.equal(saved.resourceTask,'task-1');
  const next=fixture(list(),{'/api/collaboration/resource_list':body=>list([resource('closed',{task_id:body.task_id})])});await next.page(next.el,null,saved);
  assert.equal(next.get('#co-resource-task').value,'task-1');assert(next.requests.some(request=>request.body?.task_id==='task-1'));
});

test('failed or foreign-root resource refresh retains the previous list',async()=>{
  for(const handler of [()=>{throw Error('合成资源读取错误');},()=>list([resource('closed',{title:'wrong-root'})],{workspace_root:'E:/Other'})]){
    const h=fixture(list([resource()]),{'/api/collaboration/resource_list':handler});await h.page(h.el);const before=h.get('#co-resources').innerHTML;
    await h.click('resource-refresh');assert.equal(h.get('#co-resources').innerHTML,before);assert.match(h.get('#co-error').textContent,/错误|工作环境已切换/);assert.match(h.get('#co-resource-message').textContent,/原列表已保留/);
    assert.equal(h.requests.filter(request=>request.url.endsWith('/resource_list')).length,1);
  }
});

test('late detached responses and workspace changes cannot restore an old task filter',async()=>{
  let resolve;
  const h=fixture(list([resource()]),{'/api/collaboration/resource_list':()=>new Promise(yes=>{resolve=yes;})});await h.page(h.el);const before=h.get('#co-resources').innerHTML;
  const pending=h.click('resource-refresh');h.el.isConnected=false;resolve(list([resource('closed',{title:'late result'})]));await pending;
  assert.equal(h.get('#co-resources').innerHTML,before);
  const next=fixture(list());await next.page(next.el,null,{root:'E:/Old',tab:'resources',resourceTask:'old-task'});
  assert.equal(next.get('#co-resource-task').value,'');assert(!next.requests.some(request=>request.url.endsWith('/resource_list')));
});
