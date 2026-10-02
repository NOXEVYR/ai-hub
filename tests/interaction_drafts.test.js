'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const update=require('../frontend/app-update.js');
const read=name=>fs.readFileSync(path.join(__dirname,'../frontend/'+name+'.js'),'utf8');
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
const event=()=>({preventDefault(){}});

function guard() {
  const events={},win={document:{addEventListener:(id,fn)=>events[id]=fn,getElementById:()=>null}};update.mount(win);
  const scope={dataset:{}},input={type:'text',dataset:{},value:'',matches:()=>true,closest:s=>s==='[data-app-draft-scope]'?scope:null};
  return{win,scope,input,edit:()=>events.input({target:input})};
}
test('editor scope revision survives detached DOM, preserves a later edit and discards only the selected draft',()=>{
  const h=guard(),draft=h.win.AIHubAppUpdate.bind(h.scope,'memory:Studio');
  h.edit();const submitted=draft.snapshot();h.edit();assert(draft.changed(submitted));draft.saved(submitted);assert(h.win.aiHubHasUnsavedChanges());
  const other=h.win.AIHubAppUpdate.bind({dataset:{}},'memory:Other');other.edit();draft.discard();assert(h.win.aiHubHasUnsavedChanges());other.discard();assert(!h.win.aiHubHasUnsavedChanges());
  assert(!JSON.stringify([...draft.snapshot()]).includes(h.input.value='credential-fixture'));
});
test('rebind transfers an initial workspace owner only when explicitly requested',()=>{
  const h=guard();let draft=h.win.AIHubAppUpdate.bind(h.scope,'memory:');h.edit();
  draft=h.win.AIHubAppUpdate.bind(h.scope,'memory:Studio',{transfer:true});draft.saved(draft.snapshot());assert(!h.win.aiHubHasUnsavedChanges());
  draft.edit();const other=h.win.AIHubAppUpdate.bind(h.scope,'memory:Other');other.discard();assert(h.win.aiHubHasUnsavedChanges());draft.discard();assert(!h.win.aiHubHasUnsavedChanges());
});
test('saveable forms are explicitly scoped while filter/search/preview confirmation widgets are excluded',()=>{
  const editors={workspace:['ws-form','ws-project-form'],collaboration:['co-task-form','co-memory-form','co-source-form','co-policy-form'],capabilities:['cp-dispatch-form'],'capability-library':['library-source-editor'],harnesses:['hc-form'],organizer:['organizer-form'],workcenter:['wc-classify-form'],registry:['registry-form']};
  const filters={registry:['project-type','project-search'],workspace:['ws-confirm','ws-project-confirm'],collaboration:['co-search-form','co-source-confirm-check'],capabilities:['cp-filter-form','cp-match-form'],'capability-library':['library-query','library-domain','library-tool'],harnesses:['hc-config-tool','hc-client-id'],organizer:['organizer-category'],workcenter:['wc-filter-form']};
  for(const [name,ids]of Object.entries(editors))for(const id of ids)assert(read(name).includes(`id="${id}" data-app-draft-scope="${id}"`),name+':'+id);
  for(const [name,ids]of Object.entries(filters))for(const id of ids)assert(!read(name).includes(`id="${id}" data-app-draft-scope`),name+':'+id);
});
function registryHarness() {
  const nodes=new Map(),events={},mask={dataset:{modalGeneration:'0'},classList:{contains:()=>hidden}},modal={},requests=[];
  let hidden=true,html='',respond=async()=>({token:'preview',record:{}});
  const node=s=>s==='#modal-mask'?mask:s==='#modal'?modal:nodes.get(s.slice(1));
  const context={module:{exports:{}},document:{querySelector:node,addEventListener:(id,fn)=>events[id]=fn,getElementById:()=>null}};
  update.mount(context);vm.runInNewContext(read('registry'),context);
  const openModal=value=>{
    for(const n of nodes.values())n.isConnected=false;nodes.clear();html=value;hidden=false;mask.dataset.modalGeneration=String(+mask.dataset.modalGeneration+1);
    for(const m of value.matchAll(/<(input|textarea|select|form|button|p)\b[^>]*id="([^"]+)"[^>]*>/g)){
      const n={id:m[2],value:m[0].match(/value="([^"]*)"/)?.[1]||'',dataset:{},type:'text',isConnected:true,disabled:false,querySelector:node,matches:()=>!['form','button','p'].includes(m[1]),closest:s=>s==='[data-app-draft-scope]'?node('#registry-form'):null};
      const scope=m[0].match(/data-app-draft-scope="([^"]+)"/)?.[1];if(scope)n.dataset.appDraftScope=scope;nodes.set(n.id,n);
    }
  };
  const closeModal=()=>{mask.dataset.modalGeneration=String(+mask.dataset.modalGeneration+1);hidden=true;const close=modal.onModalClose;modal.onModalClose=null;close?.();};
  const env={api:async(url,options)=>{requests.push({url,body:options.body});return respond(url,options);},openModal,closeModal,toast(){}};
  context.module.exports.editor(env,'knowledge',{id:'fixture'}, {workspace:'X:/Studio'},()=>{});
  node('#reg-title').value='A';node('#reg-path').value='X:/Studio/fixture.md';
  return {context,node,closeModal,requests,edit(id,value){node('#reg-'+id).value=value;events.input({target:node('#reg-'+id)});},respond(fn){respond=fn;},submit:()=>node('#registry-form').onsubmit(event()),html:()=>html,hidden:()=>hidden};
}
test('registry delayed preview retains newer input and cannot proceed with a stale snapshot',async()=>{
  const h=registryHarness(),pending=deferred();h.edit('title','submitted');h.respond(()=>pending.promise);const run=h.submit();h.edit('title','newer');pending.resolve({token:'old',record:{title:'submitted'}});await run;
  assert.equal(h.node('#reg-title').value,'newer');assert.match(h.node('#registry-error').textContent,/表单已变化/);assert(!h.html().includes('确认登记预览'));assert(h.context.aiHubHasUnsavedChanges());
  h.respond(async()=>({token:'new',record:{title:'newer'}}));await h.submit();assert(h.html().includes('确认登记预览'));await h.node('#registry-save').onclick();assert(!h.context.aiHubHasUnsavedChanges());
});
test('registry cancel and generic close both invalidate a late response even while its form stays connected',async()=>{
  for(const cancel of [h=>h.node('#registry-cancel').onclick(),h=>h.closeModal()]){
    const h=registryHarness(),pending=deferred();h.edit('title','draft');h.respond(()=>pending.promise);const form=h.node('#registry-form'),run=h.submit();cancel(h);assert(form.isConnected);pending.resolve({token:'late',record:{}});await run;assert(h.hidden());assert(!h.html().includes('确认登记预览'));assert(!h.context.aiHubHasUnsavedChanges());
  }
});
test('registry newer preview wins and saving failure retains the draft for retry',async()=>{
  const h=registryHarness(),first=deferred();let request=0;h.edit('title','draft');h.respond(()=>++request===1?first.promise:Promise.resolve({token:'new',record:{title:'new'}}));const old=h.submit();await h.submit();first.resolve({token:'old',record:{title:'old'}});await old;assert(!h.html().includes('old'));
  h.respond(async()=>{throw Error('offline');});await h.node('#registry-save').onclick();assert(h.context.aiHubHasUnsavedChanges());assert.equal(h.node('#registry-save').disabled,false);assert.equal(h.node('#registry-error').textContent,'offline');h.respond(async()=>({saved:true}));await h.node('#registry-save').onclick();assert(!h.context.aiHubHasUnsavedChanges());
});
test('workspace setup folds a compact guide and retains the overview onboarding card',()=>{
  const ui=require('../frontend/workspace.js');assert(ui.welcome().includes('ws-steps'));assert(ui.welcome(true,true).startsWith('<details'));assert(!ui.welcome(true,true).includes('ws-steps'));assert(read('workspace').includes('welcome(true,true) + el.innerHTML'));
});
