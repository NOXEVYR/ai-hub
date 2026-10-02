const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/capabilities.js');
const cap={id:'c1',name:'视频生成',kind:'mcp_tool',provider:'Video Service',server:'renderer',target_tool:'codex',client_id:'codex-local',domains:['video'],description:'视频片段',inputs:{type:'object',properties:{prompt:{type:'string'}},required:['prompt']},outputs:['video'],constraints:['核对费用'],hints:{},client_online:true};
function harness(handler){const nodes=new Map(),requests=[];const get=s=>{if(!nodes.has(s))nodes.set(s,{value:'',textContent:'',innerHTML:'',disabled:false,hidden:false,dataset:{},focus(){}});return nodes.get(s);};const el={isConnected:true,dataset:{},innerHTML:'',classList:{add(){}},querySelector:get,querySelectorAll:()=>[]};const api=async(url,opts)=>{requests.push({url,body:opts?.body});if(handler){const v=await handler(url,opts?.body);if(v!==undefined)return v;}if(url==='/api/harnesses')return{items:[{id:'codex',name:'Codex',enabled:true,connection_mode:'mcp_stdio'}]};if(url.endsWith('/list'))return{items:[cap],root:'D:/Studio'};if(url.endsWith('/recommend'))return{items:[{...cap,reasons:['声明场景匹配：video']}]};if(url.endsWith('/dispatch'))return receipt(opts.body);return{suggestions:[]};};return{el,requests,key:id=>get('#cp-'+id),page:ui.createPage({api})};}
const submit=()=>({preventDefault(){}});
const receipt=(request,task={})=>({request_id:request.request_id,workspace_root:'D:/Studio',deduplicated:false,status:'queued',task:{id:'11111111-1111-4111-8111-111111111111',title:'任务',status:'queued',...task}});

test('unconfirmed dispatch can be looked up after catalog withdrawal without another write',async()=>{
  let sent,withdrawn=false;
  const h=harness((url,body)=>{
    if(url.endsWith('/dispatch')){sent=body;throw Error('lost reply');}
    if(url==='/api/workspace/status')return{root:'D:/Studio'};
    if(url.endsWith('/dispatch-receipt'))return{...receipt(sent),found:true,deduplicated:true};
    if(withdrawn&&url.endsWith('/list'))return{items:[],root:'D:/Studio'};
    if(withdrawn&&url==='/api/harnesses')return{items:[]};
  });
  await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});h.key('project').value='Film';h.key('title').value='Stable';h.key('inputs').value='{}';
  await h.key('dispatch-form').onsubmit(submit());withdrawn=true;await h.key('refresh').onclick();
  assert.equal(h.key('dispatch').disabled,true);assert.equal(h.key('recover').disabled,false);
  await h.key('recover').onclick();assert.match(h.key('result').innerHTML,/没有重复派单/);assert.equal(ui.capture(h.el).dispatchIntent,null);
  assert.equal(h.requests.filter(r=>r.url.endsWith('/dispatch')).length,1);
});

test('malformed or mismatched dispatch reply never clears the original pending intent',async()=>{
  let reply={};const h=harness((url,body)=>url.endsWith('/dispatch')?reply:undefined);
  await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});h.key('project').value='Film';h.key('title').value='Stable';h.key('inputs').value='{}';
  await h.key('dispatch-form').onsubmit(submit());const id=ui.capture(h.el).dispatchIntent.request_id;
  assert.match(h.key('error').textContent,/未能确认/);assert.equal(h.key('result').innerHTML,'');
  reply={...receipt({request_id:id}),request_id:'22222222-2222-4222-8222-222222222222'};
  await h.key('dispatch-form').onsubmit(submit());assert.equal(ui.capture(h.el).dispatchIntent.request_id,id);
  reply=receipt({request_id:id});await h.key('dispatch-form').onsubmit(submit());assert.equal(ui.capture(h.el).dispatchIntent,null);
});

test('reload restores only opaque request metadata and offers read-only recovery',async()=>{
  const first=harness(url=>{if(url.endsWith('/dispatch'))throw Error('lost reply');});
  await first.page(first.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});first.key('project').value='Film';first.key('title').value='Private title';first.key('inputs').value='{}';
  await first.key('dispatch-form').onsubmit(submit());const original=ui.capture(first.el).dispatchIntent;
  const reloaded=harness((url,body)=>url.endsWith('/list')?{items:[],root:'D:/Studio'}:url==='/api/workspace/status'?{root:'D:/Studio'}:url.endsWith('/dispatch-receipt')?{...receipt(body),found:true,deduplicated:true}:undefined);
  await reloaded.page(reloaded.el,new URLSearchParams({dispatch_request_id:original.request_id,dispatch_workspace:original.root_hash}));
  assert.equal(reloaded.key('title').value,'');assert.equal(reloaded.key('dispatch').disabled,true);assert.equal(reloaded.key('recover').disabled,false);
  await reloaded.key('recover').onclick();assert.equal(ui.capture(reloaded.el).dispatchIntent,null);
  assert(!reloaded.requests.some(r=>r.url.endsWith('/dispatch')));
});

test('lost dispatch reply retries original intent across navigation and clears it only on receipt',async()=>{
  let originalId,attempts=0;
  const handler=(url,body)=>{if(url.endsWith('/dispatch')){attempts++;if(!originalId)originalId=body.request_id;assert.equal(body.request_id,originalId);if(attempts===1)throw Error('reply lost');return{...receipt(body,{title:'Stable',status:'active'}),deduplicated:true,status:'active'};}};
  const first=harness(handler);await first.page(first.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});
  first.key('project').value='Film';first.key('title').value='Stable';first.key('inputs').value='{"prompt":"same"}';
  await first.key('dispatch-form').onsubmit(submit());assert.match(first.key('error').textContent,/原请求编号/);
  const snapshot=ui.capture(first.el);assert.equal(snapshot.dispatchIntent.request_id,originalId);
  const next=harness(handler);await next.page(next.el,new URLSearchParams(),snapshot);await next.key('dispatch-form').onsubmit(submit());
  assert.equal(attempts,2);assert.match(next.key('result').innerHTML,/没有重复派单/);assert.match(next.key('result').innerHTML,/active/);
  assert.equal(ui.capture(next.el).dispatchIntent,null);
});

test('a new confirmed dispatch has a new request id while failed intent keeps its original id',async()=>{
  const h=harness();await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});
  h.key('project').value='Film';h.key('title').value='First';h.key('inputs').value='{}';await h.key('dispatch-form').onsubmit(submit());
  h.key('title').value='Second';await h.key('dispatch-form').onsubmit(submit());
  const calls=h.requests.filter(r=>r.url.endsWith('/dispatch'));assert.equal(calls.length,2);assert.notEqual(calls[0].body.request_id,calls[1].body.request_id);
});

test('catalog refresh failure freezes stale dispatch while preserving drafts and recovers on retry',async()=>{
  let failing=false;
  const h=harness(url=>{if(failing&&url.endsWith('/list'))throw Error('目录读取失败');});
  await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});
  h.key('project').value='Film';h.key('title').value='保留任务';h.key('inputs').value='{"prompt":"保留"}';
  assert.equal(h.key('dispatch').disabled,false);
  failing=true;await h.key('refresh').onclick();
  assert.equal(h.key('dispatch').disabled,true);assert.equal(h.key('match').disabled,true);
  assert.match(h.key('list').innerHTML,/等待目录刷新/);assert.match(h.key('list-caption').textContent,/目录未验证/);
  await h.key('dispatch-form').onsubmit(submit());
  assert(!h.requests.some(r=>r.url.endsWith('/dispatch')));assert.equal(h.key('title').value,'保留任务');
  failing=false;await h.key('refresh').onclick();
  assert.equal(h.key('dispatch').disabled,false);assert.equal(h.key('inputs').value,'{"prompt":"保留"}');
});

test('in-flight catalog refresh immediately blocks dispatch from the old snapshot',async()=>{
  let resolve,pending=false;
  const h=harness(url=>pending&&url.endsWith('/list')?new Promise(r=>{resolve=r;}):undefined);
  await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});
  pending=true;const refreshing=h.key('refresh').onclick();
  assert.equal(h.key('dispatch').disabled,true);
  resolve({items:[cap],root:'D:/Studio'});await refreshing;
  assert.equal(h.key('dispatch').disabled,false);
});

test('optional work-end templates never become capability filters or dispatch targets in a fresh workspace',async()=>{
  const h=harness(url=>url==='/api/harnesses'?{items:[],templates:[{id:'codex',name:'Codex'}]}:url.endsWith('/list')?{items:[],root:'D:/Studio'}:undefined);await h.page(h.el);
  assert(!h.key('tool').innerHTML.includes('codex'));assert.equal(h.key('dispatch').disabled,true);assert(!h.requests.some(r=>r.url.endsWith('/dispatch')));
});
test('catalog distinguishes declarations, heartbeats and unverified execution and escapes metadata',()=>{const html=ui.cards([{...cap,name:'<img src=x>',hints:{cost:'<secret>'},reasons:['<script>']}],'');assert(!html.includes('<img'));assert.match(html,/&lt;img/);assert.match(html,/客户端已声明/);assert.match(html,/近期有心跳/);assert.match(html,/未经执行验证/);assert.match(html,/排队交给 codex/);assert.match(html,/Video Service/);assert.match(html,/未实测/);assert(!html.includes('已验证可用'));assert(!ui.discoverHTML({suggestions:[{name:'<img>',path:'<path>'}]}).includes('<img>'));});
test('initial load never dispatches and supports all seven capability domains',async()=>{const h=harness();await h.page(h.el);assert.deepEqual(h.requests.map(r=>r.url),['/api/capabilities/list','/api/harnesses']);for(const name of ['视频','图像','语音','代码','研究','文档','自动化'])assert(h.el.innerHTML.includes(name));assert.equal(h.key('dispatch').disabled,true);assert.match(h.el.innerHTML,/排队也不代表已开始运行/);});
test('matching sends description and chosen domain with workspace guard',async()=>{const h=harness();await h.page(h.el);h.key('task-query').value='制作视频';h.key('task-domain').value='video';await h.key('match-form').onsubmit(submit());assert.deepEqual(h.requests.at(-1).body,{query:'制作视频',domain:'video',_workspace_root:'D:/Studio'});assert.match(h.key('list').innerHTML,/声明场景匹配/);assert.match(h.key('list-caption').textContent,/匹配候选/);});
test('dispatch requires explicit selected capability and valid JSON; queued result not executed',async()=>{const h=harness();await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});h.key('project').value='Film';h.key('title').value='镜头01';h.key('inputs').value='[]';await h.key('dispatch-form').onsubmit(submit());assert.match(h.key('error').textContent,/JSON 对象/);assert(!h.requests.some(r=>r.url.endsWith('dispatch')));h.key('inputs').value='{"prompt":"海边"}';await h.key('dispatch-form').onsubmit(submit());const requestBody=h.requests.at(-1).body;assert.match(requestBody.request_id,/^[0-9a-f-]{36}$/);const {request_id,...payload}=requestBody;assert.deepEqual(payload,{capability_id:'c1',project:'Film',title:'镜头01',input_json:'{"prompt":"海边"}',_workspace_root:'D:/Studio'});assert.match(h.key('result').innerHTML,/任务已排队/);assert.match(h.key('result').innerHTML,/尚未执行/);});
test('failed dispatch and navigation retain input drafts; changed workspace clears capability',async()=>{const h=harness(url=>{if(url.endsWith('/dispatch'))throw Error('工作端拒绝');});await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});h.key('project').value='Film';h.key('title').value='镜头';h.key('inputs').value='{"prompt":"保留"}';await h.key('dispatch-form').onsubmit(submit());assert.match(h.key('error').textContent,/拒绝/);assert.equal(h.key('title').value,'镜头');const snapshot=ui.capture(h.el),next=harness();await next.page(next.el,new URLSearchParams(),snapshot);assert.equal(next.key('inputs').value,'{"prompt":"保留"}');snapshot.root='E:/Old';const other=harness();await other.page(other.el,new URLSearchParams(),snapshot);assert.equal(other.el.dataset.cpSelected,'');assert.equal(other.key('dispatch').disabled,true);assert.equal(other.key('inputs').value,'{"prompt":"保留"}');assert.match(other.key('error').textContent,/工作环境已切换/);});
test('discovery remains explicit, read only and labels suggestions as unpublished',async()=>{const h=harness(()=>undefined);await h.page(h.el);await h.key('discover').onclick();assert.equal(h.requests.at(-1).url,'/api/capabilities/discover');assert.equal(h.requests.at(-1).body,undefined);assert.match(h.key('discover-results').innerHTML,/尚未发布/);});
test('late requests cannot overwrite disconnected views',async()=>{let resolve;const wait=new Promise(r=>resolve=r),h=harness(()=>wait),pending=h.page(h.el);h.el.isConnected=false;h.key('count').textContent='new page';resolve({items:[cap]});await pending;assert.equal(h.key('count').textContent,'new page');});

test('disabled work-end capabilities remain readable and filterable while dispatch preserves drafts',async()=>{
  const h=harness(url=>url==='/api/harnesses'?{items:[{id:'codex',name:'历史 Codex',enabled:false,connection_mode:'mcp_stdio'},{id:'new-worker',name:'新增工作端',enabled:true,connection_mode:'mcp_stdio'}]}:undefined);
  await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});
  assert.match(h.key('tool').innerHTML,/历史 Codex（已停用）/);assert.match(h.key('tool').innerHTML,/新增工作端/);
  assert.match(h.key('list').innerHTML,/视频生成/);assert.match(h.key('list').innerHTML,/工作端已停用/);assert.equal(h.key('dispatch').disabled,true);
  h.key('project').value='Film';h.key('title').value='保留任务';h.key('inputs').value='{"prompt":"保留"}';await h.key('dispatch-form').onsubmit(submit());
  assert(!h.requests.some(r=>r.url.endsWith('/dispatch')));assert.equal(h.key('title').value,'保留任务');assert.match(h.key('error').textContent,/已停用/);
});

test('successful dispatch clears only its submitted title and preserves the next draft typed while waiting',async()=>{
  let resolve;const h=harness(url=>url.endsWith('/dispatch')?new Promise(r=>{resolve=r;}):undefined);
  await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});h.key('project').value='Film';h.key('title').value='submitted title';h.key('inputs').value='{"prompt":"first"}';
  const pending=h.key('dispatch-form').onsubmit(submit());h.key('title').value='next title';h.key('project').value='NextFilm';h.key('inputs').value='{"prompt":"next"}';
  resolve(receipt(h.requests.find(r=>r.url.endsWith('/dispatch')).body,{title:'submitted title'}));await pending;
  assert.equal(h.requests.find(r=>r.url.endsWith('/dispatch')).body.title,'submitted title');assert.equal(h.key('title').value,'next title');assert.equal(h.key('project').value,'NextFilm');assert.equal(h.key('inputs').value,'{"prompt":"next"}');
  assert.match(h.key('result').innerHTML,/submitted title/);
});


test('dispatch protection excludes catalog filters and retains JSON edited while the earlier task queues',async()=>{
  const old=globalThis.AIHubAppUpdate,events={},win={document:{addEventListener:(id,fn)=>events[id]=fn,getElementById:()=>null}};
  require('../frontend/app-update.js').mount(win);globalThis.AIHubAppUpdate=win.AIHubAppUpdate;
  try{
    let resolve,delay=true;const h=harness(url=>url.endsWith('/dispatch')&&delay?new Promise(yes=>resolve=yes):undefined);await h.page(h.el,new URLSearchParams(),{selected:'c1',root:'D:/Studio'});
    const edit=(id,scope)=>{const control=h.key(id);control.matches=()=>true;control.closest=selector=>selector==='[data-app-draft-scope]'&&scope?h.key('dispatch-form'):null;events.input({target:control});};
    h.key('domain').value='video';edit('domain',false);assert(!win.aiHubHasUnsavedChanges());h.key('project').value='Film';h.key('title').value='Title';h.key('inputs').value='{"prompt":"submitted"}';edit('inputs',true);
    const pending=h.key('dispatch-form').onsubmit(submit());h.key('inputs').value='{"prompt":"newer"}';edit('inputs',true);resolve(receipt(h.requests.find(r=>r.url.endsWith('/dispatch')).body));await pending;assert(win.aiHubHasUnsavedChanges());assert.equal(h.key('inputs').value,'{"prompt":"newer"}');
    delay=false;h.key('title').value='New title';await h.key('dispatch-form').onsubmit(submit());assert(!win.aiHubHasUnsavedChanges());
  }finally{if(old===undefined)delete globalThis.AIHubAppUpdate;else globalThis.AIHubAppUpdate=old;}
});
