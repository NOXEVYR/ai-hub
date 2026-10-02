'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/collaboration.js');
const root='D:/Fixture',types={task:'tasks',artifact:'artifacts',memory:'memories',source:'sources'};
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
function fixture(overrides={},withModal=true) {
  let state={root,available:true,tasks:[{id:'task',title:'任务',status:'queued'}],artifacts:[{id:'artifact',title:'报告',kind:'report',status:'active'}],memories:[{id:'memory',title:'共享结论',status:'candidate'}],sources:[{id:'source',label:'来源',path:'D:/Reports',tool:'any'}],clients:[],policy:{enabled:false,days:7},deleted_records:{tasks:[],artifacts:[],memories:[],sources:[]},record_counts:{memories:{approved:0,candidate:1,retired:0,deleted:0}}};
  const nodes=new Map(),requests=[],actions=[],modalNodes=new Map();let modalHTML='',closed=0;
  const node=id=>({id,value:id==='co-task-tool'||id==='co-source-tool'?'any':'',checked:false,disabled:false,dataset:{},innerHTML:'',textContent:'',classList:{add(){}},focus(){},matches:()=>true,closest:()=>null});
  const get=selector=>{if(!nodes.has(selector))nodes.set(selector,node(selector.slice(1)));return nodes.get(selector);};
  const el={isConnected:true,dataset:{},innerHTML:'',classList:{add(){}},querySelector:get,querySelectorAll(selector){
    if(selector==='[data-co-action]')return actions;
    if(selector==='fieldset.co-fields')return [];
    if(selector.includes('#co-refresh'))return [...actions,get('#co-refresh'),get('#co-preview')];
    return [];
  }};
  const modal={onModalClose:null,querySelector:selector=>modalNodes.get(selector)};
  const doc={querySelector:selector=>selector==='#modal'?modal:null};
  function openModal(html) {
    modalHTML=html;modalNodes.clear();
    for(const match of html.matchAll(/<(button|p)\b[^>]*id="([^"]+)"[^>]*>/g)){const n=node(match[2]);n.disabled=/\bdisabled\b/.test(match[0]);modalNodes.set('#'+match[2],n);}
  }
  function closeModal(){closed++;const close=modal.onModalClose;modal.onModalClose=null;close?.();modalHTML='';}
  const api=async(url,options)=>{
    const body=options?.body;requests.push({url,body});
    if(Object.hasOwn(overrides,url))return typeof overrides[url]==='function'?overrides[url](body):overrides[url];
    if(url.endsWith('/status'))return state;
    if(url==='/api/harnesses')return {root,items:[]};
    if(url.endsWith('/source_list'))return {items:state.sources};
    if(url.endsWith('/record_delete_preview'))return {...body,title:'记录',can_apply:true,preview_token:'fixture-token',files_preserved:true,recoverable:true,impacts:{artifacts:2,memories:1,approved_memories:1,inventory:3},warnings:['隐藏关联记录'],expires_at:'2099-01-01T00:00:00Z'};
    if(url.endsWith('/record_delete_apply')){
      const key=types[body.entity_type],item=state[key].find(item=>item.id===body.entity_id);
      state={...state,[key]:state[key].filter(item=>item!==item),deleted_records:{...state.deleted_records,[key]:[...state.deleted_records[key],{...item,deleted_at:'2026-10-01T00:00:00Z'}]}};
      return {...body,applied:true,files_preserved:true};
    }
    if(url.endsWith('/record_restore'))return {...body,restored:true,files_preserved:true};
    return {items:[]};
  };
  for(const type of Object.keys(types))for(const action of ['record-delete','record-restore'])actions.push({dataset:{coAction:action,coId:type,coEntity:type},disabled:false});
  for(const action of ['approve','retire','go-tasks','go-connect','go-sources','memory-from-report'])actions.push({dataset:{coAction:action,coId:action==='memory-from-report'?'artifact':'memory'},disabled:false});
  const page=ui.createPage({api,heading:()=>'',...(withModal?{openModal,closeModal,document:doc}:{})});
  return {el,page,requests,get,modal:selector=>modal.querySelector('#co-record-'+selector),html:()=>modalHTML,closeModal,closed:()=>closed,state:()=>state,setState:next=>{state=next;},click:(action,type)=>actions.find(n=>n.dataset.coAction===action && (!type || n.dataset.coEntity===type)).onclick()};
}
test('each task, report, source and memory table has an escaped delete action; deleted view has real restore actions',()=>{
  const evil='<svg onload="bad">';
  for(const [type,render] of [['task',ui.tasksHTML],['artifact',ui.artifactsHTML],['memory',ui.memoriesHTML],['source',ui.sourcesHTML]]){
    const html=render([{id:evil,title:evil,path:evil,status:'candidate'}]);assert.match(html,/data-co-action="record-delete"/);assert(html.includes(`data-co-entity="${type}"`));assert(!html.includes('<svg'));assert(html.includes('&lt;svg'));
  }
  const deleted={deleted_records:Object.fromEntries(Object.values(types).map(key=>[key,[{id:evil,title:evil,deleted_at:evil}]]))};
  assert.equal((ui.deletedHTML(deleted).match(/record-restore/g)||[]).length,4);assert(!ui.deletedHTML(deleted).includes('<svg'));
  const preview=ui.deletePreviewHTML({title:evil,warnings:[evil],impacts:{inventory:evil}},'source');assert(!preview.includes('<svg'));assert.match(preview,/&lt;svg/);
});
test('delete preview then cancel makes no mutation, including a late preview response',async()=>{
  const h=fixture();await h.page(h.el);await h.click('record-delete','task');assert.match(h.html(),/原文件、目录/);assert.match(h.html(),/关联报告 \/ 产物记录：2/);
  h.modal('cancel').onclick();assert(!h.requests.some(r=>r.url.endsWith('/record_delete_apply')));
  const late=deferred(),next=fixture({'/api/collaboration/record_delete_preview':()=>late.promise});await next.page(next.el);
  const pending=next.click('record-delete','task');next.modal('cancel').onclick();late.resolve({can_apply:true,preview_token:'late'});await pending;
  assert.equal(next.html(),'');assert(!next.requests.some(r=>r.url.endsWith('/record_delete_apply')));
});
test('all four delete confirmation callbacks send the exact preview contract and refresh only after service confirmation',async()=>{
  for(const type of Object.keys(types)){
    const h=fixture();await h.page(h.el);await h.click('record-delete',type);
    assert.deepEqual(h.requests.find(r=>r.url.endsWith('/record_delete_preview')).body,{entity_type:type,entity_id:type,_workspace_root:root});
    assert.equal(h.modal('confirm').disabled,false);await h.modal('confirm').onclick();
    assert.deepEqual(h.requests.find(r=>r.url.endsWith('/record_delete_apply')).body,{entity_type:type,entity_id:type,_workspace_root:root,preview_token:'fixture-token'});
    assert.match(h.get('#co-deleted').innerHTML,/record-restore/);assert.equal(h.state()[types[type]].length,0);assert(h.closed()>0);
  }
});
test('failed apply, stale workspace and uncertain result preserve lists and never retry on another root',async()=>{
  for(const result of [()=>{throw Error('工作环境已切换');},()=>({applied:false})]){
    const h=fixture({'/api/collaboration/record_delete_apply':result});await h.page(h.el);const before=h.get('#co-tasks').innerHTML;
    await h.click('record-delete','task');await h.modal('confirm').onclick();assert.equal(h.get('#co-tasks').innerHTML,before);
    assert.match(h.get('#co-error').textContent,/工作环境已切换|未确认/);assert.equal(h.modal('confirm').disabled,false);
    assert.equal(h.requests.filter(r=>r.url.endsWith('/record_delete_apply')).length,1);assert.equal(h.requests.filter(r=>r.url.endsWith('/status')).length,1);
    assert.equal(h.requests.find(r=>r.url.endsWith('/record_delete_apply')).body._workspace_root,root);
  }
});
test('busy apply ignores duplicate execution; refreshed root invalidates the old confirmation callback',async()=>{
  const wait=deferred(),h=fixture({'/api/collaboration/record_delete_apply':()=>wait.promise});await h.page(h.el);await h.click('record-delete','task');
  const apply=h.modal('confirm').onclick,pending=apply();await apply();assert.equal(h.modal('confirm').disabled,true);assert.equal(h.modal('cancel').disabled,true);
  assert.equal(h.requests.filter(r=>r.url.endsWith('/record_delete_apply')).length,1);
  wait.resolve({applied:true,entity_type:'task',entity_id:'task',files_preserved:true});await pending;
  const next=fixture();await next.page(next.el);await next.click('record-delete','task');const old=next.modal('confirm').onclick;
  next.setState({...next.state(),root:'E:/Changed'});await next.get('#co-refresh').onclick();await old();assert(!next.requests.some(r=>r.url.endsWith('/record_delete_apply')));
  assert.match(next.get('#co-error').textContent,/预览已失效|工作环境已切换/);
});
test('blocked active task and malformed previews cannot confirm deletion',async()=>{
  const blocked=fixture({'/api/collaboration/record_delete_preview':body=>({...body,can_apply:false,files_preserved:true,recoverable:true,warnings:['执行中的任务须先释放领取']})});await blocked.page(blocked.el);await blocked.click('record-delete','task');
  assert.equal(blocked.modal('confirm').disabled,true);assert.equal(blocked.modal('confirm').onclick,undefined);assert.match(blocked.html(),/先释放领取/);
  const malformed=fixture({'/api/collaboration/record_delete_preview':{can_apply:true,preview_token:'wrong'}});await malformed.page(malformed.el);await malformed.click('record-delete','task');assert.match(malformed.get('#co-error').textContent,/预览不完整/);assert(!malformed.requests.some(r=>r.url.endsWith('/record_delete_apply')));
  const missing=fixture({},false);await missing.page(missing.el);await missing.click('record-delete','task');assert.match(missing.get('#co-error').textContent,/确认窗口不可用/);assert(!missing.requests.some(r=>r.url.endsWith('/record_delete_preview')));
});
test('restore is explicitly confirmed and calls the real workspace-bound restore endpoint',async()=>{
  for(const type of Object.keys(types)){
    const h=fixture();const state=h.state();state.deleted_records[types[type]]=[{id:type,title:'已删除记录'}];await h.page(h.el);await h.click('record-restore',type);
    assert(!h.requests.some(r=>r.url.endsWith('/record_restore')));assert.match(h.html(),/需要先恢复/);await h.modal('confirm').onclick();
    assert.deepEqual(h.requests.find(r=>r.url.endsWith('/record_restore')).body,{entity_type:type,entity_id:type,_workspace_root:root});
  }
});
test('memory guide uses server-wide real counts and explains every stage, without implying installed tools wrote memories',async()=>{
  const state={memories:[],artifacts:[],record_counts:{memories:{candidate:12,approved:3,retired:4,deleted:5}}};
  const html=ui.memoryGuideHTML(state);assert.match(html,/实际数量：待审核 12 · 已批准 3 · 已退役 4 · 已删除 5/);
  for(const text of ['报告：','候选：','用户批准：','可检索：','不代表它已提交报告','原生记忆不会被读取'])assert(html.includes(text));
  const h=fixture();await h.page(h.el);await h.click('go-connect');assert.equal(h.el.dataset.coTab,'connect');await h.click('go-tasks');assert.equal(h.el.dataset.coTab,'tasks');await h.click('go-sources');assert.equal(h.el.dataset.coTab,'sources');
  assert.match(ui.memoryGuideHTML({memories:[],artifacts:[]}),/尚无记忆候选/);assert.match(ui.memoryGuideHTML({record_counts:{memories:{candidate:1}}}),/候选等待用户批准/);
});

test('memory guide keeps real counts and pending review before a closed native explanation',()=>{
  const state={memories:[],artifacts:[],record_counts:{memories:{candidate:12,approved:3,retired:4,deleted:5}},memory_pipeline:{reports:18,candidates:12,approved:3,reports_without_candidates:6,reports_unevaluated:2,reports_evaluated:16,reports_evaluated_without_candidates:4}};
  const html=ui.memoryGuideHTML(state),collapsed=html.indexOf('<details>');
  assert(collapsed>0);assert.doesNotMatch(html,/<details[^>]*\bopen\b/);
  const visible=html.slice(0,collapsed),explanation=html.slice(collapsed);
  for(const text of ['待审核 12 · 已批准 3 · 已退役 4 · 已删除 5','候选等待用户批准','候选不会进入长期记忆检索','18 份登记报告','未声明 2 份 · 已评估 16 份 · 其中无需候选 4 份'])assert(visible.includes(text));
  assert.doesNotMatch(visible,/<ol>|report_submit|data-co-action/);
  assert.match(explanation,/<summary>记忆流程与排查<\/summary>/);
  for(const text of ['报告：','候选：','用户批准：','可检索：','report_submit','未声明不等于','各工具原生记忆不会被读取或修改'])assert(explanation.includes(text));
});
test('empty memory pipeline exposes reports without candidate submissions and the report shortcut preserves authored content',async()=>{
  const state={memory_pipeline:{reports:11,candidates:0,approved:0,reports_without_candidates:11,reports_with_candidates:0},memories:[],artifacts:[]};
  const html=ui.memoryGuideHTML(state);assert.match(html,/已有报告不等于记忆候选/);assert.match(html,/当前工作区：11 份登记报告 \/ 0 条待审核候选/);assert.match(html,/尚未形成候选的报告 11 份/);assert.match(html,/report_submit/);assert.match(html,/不会自动总结报告/);
  const h=fixture();await h.page(h.el);h.get('#co-memory-content').value='已有未提交结论';await h.click('memory-from-report');
  assert.equal(h.el.dataset.coTab,'memories');assert.equal(h.get('#co-memory-source').value,'artifact');assert.equal(h.get('#co-memory-content').value,'已有未提交结论');
  assert(!h.requests.some(r=>r.url.endsWith('/memory_propose')));assert.match(ui.artifactsHTML([{id:'artifact',kind:'report',status:'active'}]),/memory-from-report/);
});
test('a searched approved memory outside the recent list can still preview deletion and receives bound callbacks',async()=>{
  const h=fixture({'/api/collaboration/memory_search':{items:[{id:'memory',title:'较早的已批准结论',status:'approved'}]}});h.setState({...h.state(),memories:[]});await h.page(h.el);
  await h.get('#co-search-form').onsubmit({preventDefault(){}});assert.match(h.get('#co-search-results').innerHTML,/record-delete/);await h.click('record-delete','memory');
  assert(h.requests.some(r=>r.url.endsWith('/record_delete_preview') && r.body.entity_id==='memory'));h.modal('cancel').onclick();assert(!h.requests.some(r=>r.url.endsWith('/record_delete_apply')));
});
test('explicit no-memory decisions remain distinct from legacy reports with no declaration',()=>{
  const html=ui.memoryGuideHTML({memory_pipeline:{reports:2,candidates:0,approved:0,reports_evaluated:2,reports_unevaluated:0,reports_evaluated_without_candidates:2}});
  assert.match(html,/已声明无需记忆候选/);assert.match(html,/未声明 0/);assert.match(html,/已评估 2/);
  const legacy=ui.memoryGuideHTML({memory_pipeline:{reports:2,candidates:0,approved:0,reports_evaluated:0,reports_unevaluated:2,reports_evaluated_without_candidates:0}});
  assert.match(legacy,/未声明 2/);assert.match(legacy,/已有报告不等于记忆候选/);
});
test('approve, retire, actual recycle and enabling automatic recycle require the styled confirmation before mutation',async()=>{
  for(const action of ['approve','retire']){
    const h=fixture();await h.page(h.el);await h.click(action);assert(!h.requests.some(r=>r.url.endsWith('/memory_review')));h.modal('cancel').onclick();assert(!h.requests.some(r=>r.url.endsWith('/memory_review')));
    await h.click(action);assert.match(h.html(),/共享检索/);await h.modal('confirm').onclick();assert.equal(h.requests.find(r=>r.url.endsWith('/memory_review')).body.status,action==='approve'?'approved':'retired');
  }
  const h=fixture({'/api/collaboration/retention_preview':{items:[{id:'temp',path:'D:/Temp/temp.md'}]}});await h.page(h.el);await h.get('#co-preview').onclick();h.get('#co-run').onclick();assert(!h.requests.some(r=>r.url.endsWith('/retention_run')));assert.match(h.html(),/实际处理/);await h.modal('confirm').onclick();assert.deepEqual(h.requests.find(r=>r.url.endsWith('/retention_run')).body.artifact_ids,['temp']);
  h.get('#co-policy-enabled').checked=true;h.get('#co-policy-days').value='7';h.get('#co-policy-form').onsubmit({preventDefault(){}});assert(!h.requests.some(r=>r.url.endsWith('/retention_policy')));assert.match(h.html(),/既有已到期文件/);await h.modal('confirm').onclick();assert.equal(h.requests.find(r=>r.url.endsWith('/retention_policy')).body.enabled,true);
});
