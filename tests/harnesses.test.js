'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/harnesses.js'),collaboration=require('../frontend/collaboration.js');
const base=()=>[{id:'codex',name:'Codex',builtin:true,enabled:true,connection_mode:'mcp_stdio',revision:0,configured:false,detected:true,recent_heartbeat:false,invocation_verified:false}];
const submit=()=>({preventDefault(){}});
function harness({records=base(),handler,restored,root='D:/Studio'}={}){
  const nodes=new Map(),buttons=new Map(),requests=[];let items=structuredClone(records);
  const get=s=>{if(!nodes.has(s))nodes.set(s,{id:s.slice(1),value:s==='#co-task-tool'||s==='#co-source-tool'?'any':'',type:s==='#hc-enabled'?'checkbox':'text',checked:false,innerHTML:'',textContent:'',hidden:false,disabled:false,dataset:{},focus(){this.focused=true;},setAttribute(){}});return nodes.get(s);};
  const actionNodes=()=>[...get('#hc-registry').innerHTML.matchAll(/data-hc-action="([^"]+)" data-hc-id="([^"]+)"/g)].map(([,action,id])=>{const key=action+':'+id;if(!buttons.has(key))buttons.set(key,{dataset:{hcAction:action,hcId:id},disabled:false});return buttons.get(key);});
  const candidateNodes=()=>[...get('#hc-candidates').innerHTML.matchAll(/data-hc-candidate="([^"]+)"/g)].map(([,id])=>{const key='candidate:'+id;if(!buttons.has(key))buttons.set(key,{dataset:{hcCandidate:id},disabled:false});return buttons.get(key);});
  const el={isConnected:true,dataset:{},innerHTML:'',classList:{add(){}},querySelector:get,querySelectorAll(s){if(s==='[data-hc-action]')return actionNodes();if(s==='[data-hc-candidate]')return candidateNodes();return[];}};
  const api=async(url,opts)=>{const body=opts?.body;requests.push({url,body});if(handler){const value=await handler(url,body);if(value!==undefined)return value;}if(url==='/api/harnesses')return{items:structuredClone(items),root,templates:base()};if(url==='/api/harnesses/save'){assert.equal(body._workspace_root,root);const old=items.find(x=>x.id===body.id);assert.equal(body.revision,old?.revision||0);const record={...old,...body,root,user_notes:body.notes,registered_executable:body.executable,configured:true,revision:(old?.revision||0)+1};items=items.filter(x=>x.id!==record.id).concat(record);return record;}if(url.startsWith('/api/harnesses/check?'))return structuredClone(items.find(x=>x.id===new URLSearchParams(url.split('?')[1]).get('id')));if(url==='/api/harnesses/discover')return{items:[{suggested_id:'quick-agent',name:'Quick Agent',executable:'C:/Tools/quick.exe',evidence:['入口存在']}],scan_scope:['PATH','开始菜单'],truncated:false};if(url.startsWith('/api/harnesses/config?')){const p=new URLSearchParams(url.split('?')[1]);return{tool_id:p.get('id'),client_id:p.get('client_id'),instructions:'请按客户端格式填写',config:{mcpServers:{aihub:{command:'C:/Python/python.exe',args:['bridge.py','--tool',p.get('id'),'--client-id',p.get('client_id')]}}}};}if(url.endsWith('/status'))return{available:true,root,tasks:[],artifacts:[],memories:[],clients:[],policy:{enabled:false,days:7}};return{items:[]};};
  return{el,requests,key:id=>get('#hc-'+id),co:id=>get('#co-'+id),items:()=>items,action:(action,id)=>buttons.get(action+':'+id),candidate:id=>buttons.get('candidate:'+id),page:()=>collaboration.createPage({api})(el,new URLSearchParams({tab:'connect'}),restored),save:()=>get('#hc-form').onsubmit(submit())};
}

test('manual registration immediately populates dynamic task and report selections with no invented connection',async()=>{
  const h=harness();await h.page();h.key('new').onclick();h.key('id').value='quick-agent';h.key('name').value='Quick Agent';h.key('executable').value='C:/Tools/quick.exe';h.key('notes').value='文档工作端';await h.save();
  const request=h.requests.find(r=>r.url.endsWith('/save'));assert.equal(request.body.revision,0);assert.equal(request.body._workspace_root,'D:/Studio');assert.equal(request.body.connection_mode,'mcp_stdio');assert(!('args' in request.body));assert(!('env' in request.body));
  assert.match(h.co('task-tool').innerHTML,/Quick Agent/);assert.match(h.co('source-tool').innerHTML,/Quick Agent/);assert.match(h.key('registry').innerHTML,/已登记/);assert.match(h.key('message').textContent,/以实际证据为准/);
  h.co('task-tool').value='quick-agent';h.co('task-project').value='Film';h.co('task-title').value='整理材料';await h.co('task-form').onsubmit(submit());assert(h.requests.some(r=>r.url.endsWith('/task_create')&&r.body.target_tool==='quick-agent'));
});

test('editing and disabling use revision; history remains while new task dispatch is blocked',async()=>{
  const h=harness();await h.page();h.action('edit','codex').onclick();assert.equal(h.key('id').readOnly,true);h.key('name').value='My Codex';h.key('enabled').checked=false;h.co('task-tool').value='codex';await h.save();
  assert.equal(h.items()[0].enabled,false);assert.equal(h.items()[0].revision,1);assert.match(h.co('task-tool').innerHTML,/disabled>My Codex（已停用）/);assert.match(h.key('registry').innerHTML,/重新启用/);
  h.co('task-title').value='保留草稿';await h.co('task-form').onsubmit(submit());assert.equal(h.co('task-title').value,'保留草稿');assert(!h.requests.some(r=>r.url.endsWith('/task_create')));
  await h.action('toggle','codex').onclick();assert.equal(h.items()[0].enabled,true);assert.equal(h.items()[0].revision,2);assert.match(h.co('task-tool').innerHTML,/My Codex/);
});

test('validation failure, API rejection and cancel preserve drafts including navigation return',async()=>{
  let reject=true;const h=harness({handler:(url)=>{if(url.endsWith('/save')&&reject)throw Error('登记版本冲突，请重新核对');}});await h.page();h.key('new').onclick();h.key('id').value='my-worker';h.key('name').value='';h.key('notes').value='未保存用途';await h.save();assert.match(h.key('error').textContent,/显示名称/);assert(!h.requests.some(r=>r.url.endsWith('/save')));
  h.key('name').value='My Worker';h.key('executable').value='relative.exe';await h.save();assert.match(h.key('error').textContent,/绝对路径/);h.key('executable').value='C:/Worker/app.exe';await h.save();assert.match(h.key('error').textContent,/版本冲突/);assert.equal(h.key('notes').value,'未保存用途');
  h.key('cancel').onclick();assert.equal(h.key('editor').hidden,true);h.key('new').onclick();assert.equal(h.key('name').value,'My Worker');const snapshot=collaboration.capture(h.el);const next=harness({restored:snapshot});await next.page();assert.equal(next.key('notes').value,'未保存用途');assert.equal(next.key('name').value,'My Worker');reject=false;
});

test('discovery only lists candidates and filling does not save or erase a separate manual draft',async()=>{
  const h=harness();await h.page();h.key('new').onclick();h.key('id').value='draft-worker';h.key('name').value='手工草稿';await h.key('discover').onclick();assert(!h.requests.some(r=>r.url.endsWith('/save')));h.candidate('0').onclick();assert.equal(h.key('id').value,'quick-agent');assert.equal(h.key('executable').value,'C:/Tools/quick.exe');assert(!h.requests.some(r=>r.url.endsWith('/save')));h.key('new').onclick();assert.equal(h.key('name').value,'手工草稿');
});

test('detected, configured, recent heartbeat and verified invocation are independent evidence',async()=>{
  const html=ui.evidence({detected:true,configured:true,recent_heartbeat:false,invocation_verified:false});assert.match(html,/已发现/);assert.match(html,/已登记/);assert.match(html,/无近期连接证据/);assert.match(html,/协议调用未验证/);assert(!html.includes('协议调用已验证'));
  assert.match(ui.evidence({recent_heartbeat:true,invocation_verified:false}),/近期连接/);assert(!ui.evidence({recent_heartbeat:true}).includes('协议调用已验证'));
  const h=harness();await h.page();await h.action('check','codex').onclick();assert.match(h.key('registry').innerHTML,/无近期连接证据/);assert.match(h.key('message').textContent,/没有启动或调用第三方软件/);assert(!h.requests.some(r=>r.url.startsWith('/api/harnesses')&&r.body));
});

test('manual mode supports source handoff but cannot enter MCP queue or request config',async()=>{
  const h=harness({records:[...base(),{id:'manual-worker',name:'手动工作端',enabled:true,connection_mode:'manual',revision:1}]});await h.page();assert(!h.co('task-tool').innerHTML.includes('manual-worker'));assert.match(h.co('source-tool').innerHTML,/manual-worker/);h.co('task-tool').value='manual-worker';await h.co('task-form').onsubmit(submit());assert(!h.requests.some(r=>r.url.endsWith('/task_create')));
  h.key('config-tool').value='manual-worker';h.key('config-tool').onchange();await h.key('config-load').onclick();assert.match(h.key('config-result').innerHTML,/手动规则交接/);assert(!h.requests.some(r=>r.url.startsWith('/api/harnesses/config?')));
});

test('MCP config accepts distinct client IDs and stale responses cannot replace a newer selection',async()=>{
  let resolve;const h=harness({handler:url=>url.startsWith('/api/harnesses/config?')?new Promise(r=>{resolve=r;}):undefined});await h.page();h.key('config-tool').value='codex';h.key('config-tool').onchange();h.key('client-id').value='codex-second';const pending=h.key('config-load').onclick();assert.match(h.requests.at(-1).url,/client_id=codex-second/);h.key('client-id').value='codex-third';h.key('client-id').oninput();resolve({config:{stale:true}});await pending;assert.equal(h.key('config-result').innerHTML,'');assert.equal(h.key('config-load').disabled,false);
  const normal=harness();await normal.page();normal.key('config-tool').value='codex';normal.key('config-tool').onchange();normal.key('client-id').value='separate-client';await normal.key('config-load').onclick();assert.match(normal.key('config-result').innerHTML,/separate-client/);assert.match(normal.key('config-result').innerHTML,/尚未证明连接或执行/);
});

test('double save issues one mutation and detached responses leave drafts untouched',async()=>{
  let resolve;const h=harness({handler:url=>url.endsWith('/save')?new Promise(r=>{resolve=r;}):undefined});await h.page();h.key('new').onclick();h.key('id').value='pending-worker';h.key('name').value='等待中的草稿';const pending=h.save();await h.save();assert.equal(h.requests.filter(r=>r.url.endsWith('/save')).length,1);h.el.isConnected=false;resolve({id:'pending-worker',name:'saved elsewhere',revision:1});await pending;assert.equal(h.key('name').value,'等待中的草稿');
});

test('old workspace drafts retain original root and cannot silently save into a new workspace',async()=>{
  const h=harness();await h.page();h.key('new').onclick();h.key('id').value='old-worker';h.key('name').value='旧工作区草稿';const snapshot=collaboration.capture(h.el);const next=harness({root:'E:/Other',restored:snapshot,handler:(url,body)=>{if(url.endsWith('/save')){assert.equal(body._workspace_root,'D:/Studio');throw Error('工作环境已切换');}}});await next.page();await next.save();assert.match(next.key('error').textContent,/工作环境已切换/);assert.equal(next.key('name').value,'旧工作区草稿');
});

test('history filters retain disabled and unknown sources while queue choices exclude them',()=>{
  const records=[{id:'new-worker',name:'<New Worker>',enabled:true,connection_mode:'mcp_stdio'},{id:'old-worker',name:'Old Worker',enabled:false,connection_mode:'mcp_stdio'}];
  const queue=ui.options(records,{queue:true});assert.match(queue,/&lt;New Worker&gt;/);assert(!queue.includes('old-worker'));
  const history=ui.options(records,{history:true,extras:['legacy-worker']});assert.match(history,/Old Worker（已停用）/);assert.match(history,/legacy-worker（历史来源）/);assert(!history.includes('<New Worker>'));
});

test('navigation restores the config selection after async options exist in a real-select-like control',async()=>{
  const h=harness({restored:{harness:{configTool:'codex',clientId:'codex-second'}}});
  const select=h.key('config-tool');let selected='';
  Object.defineProperty(select,'value',{get:()=>selected,set:value=>{selected=select.innerHTML.includes('value="'+value+'"')?value:'';}});
  await h.page();assert.equal(select.value,'codex');assert.equal(h.key('client-id').value,'codex-second');
});

test('same tool ID in another workspace cannot inherit or rebind the old workspace draft',async()=>{
  const a=harness();await a.page();a.action('edit','codex').onclick();a.key('name').value='workspace A draft';
  const b=harness({root:'E:/Other',restored:collaboration.capture(a.el)});await b.page();
  b.action('edit','codex').onclick();assert.equal(b.key('name').value,'Codex');b.key('name').value='workspace B edit';await b.save();
  const save=b.requests.find(r=>r.url.endsWith('/save'));assert.equal(save.body._workspace_root,'E:/Other');assert.equal(save.body.name,'workspace B edit');
  const back=harness({restored:collaboration.capture(b.el)});await back.page();back.action('edit','codex').onclick();assert.equal(back.key('name').value,'workspace A draft');assert.equal(back.key('revision').value,'0');
});

test('revision conflict keeps old draft until explicit discard loads latest registration',async()=>{
  const h=harness();await h.page();h.action('edit','codex').onclick();h.key('name').value='local unsaved';
  h.items()[0].name='external update';h.items()[0].revision=1;await h.key('refresh').onclick();h.action('edit','codex').onclick();
  assert.equal(h.key('name').value,'local unsaved');assert.equal(h.key('revision').value,'0');await h.save();assert.equal(h.items()[0].name,'external update');assert.equal(h.key('name').value,'local unsaved');
  await h.key('reload-latest').onclick();assert.equal(h.key('name').value,'external update');assert.equal(h.key('revision').value,'1');assert.match(h.key('message').textContent,/已放弃当前草稿/);
  h.key('name').value='reviewed new edit';await h.save();assert.equal(h.items()[0].revision,2);assert.equal(h.items()[0].name,'reviewed new edit');
});

test('discard latest cannot discard a draft for a different workspace and toggling another root cannot change its revision',async()=>{
  const a=harness();await a.page();a.action('edit','codex').onclick();a.key('name').value='protected A draft';
  const b=harness({root:'E:/Other',restored:collaboration.capture(a.el)});await b.page();await b.action('toggle','codex').onclick();
  assert.equal(b.key('revision').value,'0');assert.equal(b.key('name').value,'protected A draft');assert.equal(b.key('enabled').checked,true);
  await b.key('reload-latest').onclick();assert.match(b.key('error').textContent,/工作环境已切换/);assert.equal(b.key('name').value,'protected A draft');assert.equal(b.key('revision').value,'0');
});

test('list toggles never promote a stale editor revision over an external update',async()=>{
  const h=harness();await h.page();h.action('edit','codex').onclick();h.key('name').value='stale local name';
  h.items()[0].name='external name';h.items()[0].revision=1;await h.key('refresh').onclick();await h.action('toggle','codex').onclick();
  assert.equal(h.items()[0].revision,2);assert.equal(h.items()[0].name,'external name');assert.equal(h.items()[0].enabled,false);
  assert.equal(h.key('revision').value,'0');assert.equal(h.key('name').value,'stale local name');await h.save();
  assert.equal(h.items()[0].name,'external name');assert.equal(h.items()[0].revision,2);assert.match(h.key('message').textContent,/草稿已保留/);
});
