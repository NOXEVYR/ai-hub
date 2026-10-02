'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const workspace=require('../frontend/workspace.js');
const root='D:/DetachFixture';
const status=()=>({root,configured:true,managed:true,available:true,revision:'fixture-r1',tools:[],sources:{scan_roots:['D:/Models'],output_roots:['D:/Outputs']},source_health:[],discovery:{items:[]}});
const preview=()=>({root,token:'detach-fixture-token',can_apply:true,warnings:['解除目录与来源配置；文件和历史记录保留。']});
const deferred=()=>{let resolve;const promise=new Promise(yes=>resolve=yes);return {promise,resolve};};
const documentOwners=new WeakSet();
function fixture(t,overrides={}) {
  const previousDocument=globalThis.document,nodes=new Map(),modalNodes=new Map(),requests=[],toasts=[];let current=status(),html='',opens=0,closes=0;
  const node=selector=>{if(!nodes.has(selector))nodes.set(selector,{id:selector.slice(1),value:'',innerHTML:'',textContent:'',checked:false,disabled:false,isConnected:true});return nodes.get(selector);};
  const el={isConnected:true,innerHTML:'',querySelector:selector=>selector==='#ws-detach' && !el.innerHTML.includes('id="ws-detach"')?null:node(selector),querySelectorAll:()=>[]};
  const modal={onModalClose:null,querySelector:selector=>modalNodes.get(selector)};
  const doc={querySelector:selector=>selector==='#modal'?modal:null};globalThis.document=doc;
  if(!documentOwners.has(t)){documentOwners.add(t);t.after(()=>{if(previousDocument===undefined)delete globalThis.document;else globalThis.document=previousDocument;});}
  const disconnect=()=>{for(const n of modalNodes.values())n.isConnected=false;modalNodes.clear();};
  function openModal(value) {
    disconnect();opens++;html=value;
    for(const match of value.matchAll(/<(button|p)\b[^>]*id="([^"]+)"[^>]*>/g))modalNodes.set('#'+match[2],{id:match[2],isConnected:true,disabled:/\bdisabled\b/.test(match[0]),textContent:''});
  }
  function closeModal(){closes++;const onClose=modal.onModalClose;modal.onModalClose=null;onClose?.();disconnect();html='';}
  const api=async(url,options)=>{
    requests.push({url,body:options?.body});
    if(Object.hasOwn(overrides,url))return typeof overrides[url]==='function'?overrides[url](options?.body):overrides[url];
    if(url==='/api/workspace/status')return current;
    if(url==='/api/overview')return {};
    if(url==='/api/jobs')return {jobs:[]};
    if(url==='/api/workspace/detach_preview')return preview();
    if(url==='/api/workspace/detach_apply'){current={...current,configured:false,managed:false,root:'',sources:{scan_roots:[],output_roots:[]}};return {applied:true,files_preserved:true};}
    return {};
  };
  const page=workspace.createPage({api,heading:()=>'',toast:(...args)=>toasts.push(args),pollJobs(){},openModal,closeModal});
  return {el,page,requests,toasts,key:id=>node('#ws-'+id),modal:id=>modal.querySelector('#ws-detach-'+id),closeModal,html:()=>html,opens:()=>opens,closes:()=>closes,click:()=>el.querySelector('#ws-detach').onclick()};
}
const applied=h=>h.requests.filter(r=>r.url==='/api/workspace/detach_apply');
test('detach preview is workspace-bound; cancel and application close make no configuration writes',async t=>{
  const h=fixture(t);await h.page(h.el);const before=h.el.innerHTML;await h.click();
  assert.deepEqual(h.requests.find(r=>r.url.endsWith('/detach_preview')).body,{_workspace_root:root});assert.match(h.html(),/文件和历史记录保留/);assert.equal(applied(h).length,0);
  h.modal('cancel').onclick();assert.equal(applied(h).length,0);assert.equal(h.el.innerHTML,before);assert.equal(h.key('detach').disabled,false);
  await h.click();h.closeModal();assert.equal(applied(h).length,0);
});
test('explicit confirmation sends only the exact token and original root, then renders the unconfigured state',async t=>{
  const h=fixture(t);await h.page(h.el);await h.click();assert.equal(h.modal('confirm').disabled,false);await h.modal('confirm').onclick();
  assert.deepEqual(applied(h).map(r=>r.body),[{token:'detach-fixture-token',_workspace_root:root}]);assert.match(h.el.innerHTML,/尚未配置工作环境/);assert(!h.el.innerHTML.includes('id="ws-detach"'));assert.equal(h.toasts.length,1);assert(h.closes()>0);
});
test('preview failure retains form inputs and page, displays the error, and restores the detach button',async t=>{
  const h=fixture(t,{'/api/workspace/detach_preview':()=>{throw Error('服务未连接');}});await h.page(h.el);h.key('root').value='D:/AuthoredDraft';const before=h.el.innerHTML;
  await h.click();assert.equal(h.el.innerHTML,before);assert.equal(h.key('root').value,'D:/AuthoredDraft');assert.equal(h.key('error').textContent,'服务未连接');assert.equal(h.key('detach').disabled,false);assert.equal(h.opens(),0);assert.equal(applied(h).length,0);
});
test('apply error retains the page and editable draft, restores cancellation and the main button, and emits no success',async t=>{
  const h=fixture(t,{'/api/workspace/detach_apply':()=>{throw Error('仍有执行中的任务，请先完成或释放。');}});await h.page(h.el);h.key('output').value='D:/AuthoredOutput';const before=h.el.innerHTML;await h.click();await h.modal('confirm').onclick();
  assert.equal(h.el.innerHTML,before);assert.equal(h.key('output').value,'D:/AuthoredOutput');assert.match(h.modal('error').textContent,/执行中的任务/);assert.equal(h.modal('cancel').disabled,false);assert.equal(h.key('detach').disabled,false);assert.equal(h.toasts.length,0);
});
test('saved confirm callbacks cannot apply after cancel, close or navigation, and late previews cannot open a stale page',async t=>{
  const h=fixture(t);await h.page(h.el);
  for(const close of [()=>h.modal('cancel').onclick(),()=>h.closeModal()]){await h.click();const old=h.modal('confirm').onclick;close();await old();assert.equal(applied(h).length,0);}
  await h.click();const old=h.modal('confirm').onclick;h.el.isConnected=false;await old();assert.equal(applied(h).length,0);h.closeModal();
  const late=deferred(),next=fixture(t,{'/api/workspace/detach_preview':()=>late.promise});await next.page(next.el);const pending=next.click();next.el.isConnected=false;late.resolve(preview());await pending;assert.equal(next.opens(),0);assert.equal(applied(next).length,0);
});
test('preview and apply busy guards reject duplicate callbacks and prevent a concurrent refresh',async t=>{
  const pendingPreview=deferred(),pendingApply=deferred(),h=fixture(t,{'/api/workspace/detach_preview':()=>pendingPreview.promise,'/api/workspace/detach_apply':()=>pendingApply.promise});await h.page(h.el);
  const loading=h.click();assert.equal(h.key('detach').disabled,true);await h.click();assert.equal(h.requests.filter(r=>r.url.endsWith('/detach_preview')).length,1);
  pendingPreview.resolve(preview());await loading;const confirm=h.modal('confirm').onclick,running=confirm();assert.equal(h.modal('confirm').disabled,true);assert.equal(h.modal('cancel').disabled,true);
  await confirm();await h.click();await h.key('refresh').onclick();assert.equal(applied(h).length,1);assert.equal(h.requests.filter(r=>r.url==='/api/workspace/status').length,1);
  pendingApply.resolve({applied:false});await running;assert.match(h.modal('error').textContent,/未确认/);assert.equal(h.modal('cancel').disabled,false);assert.equal(h.key('detach').disabled,false);
});
test('blocked or incomplete previews remain non-executable even when their callback is invoked directly',async t=>{
  for(const plan of [{...preview(),can_apply:false},{...preview(),token:''}]){
    const h=fixture(t,{'/api/workspace/detach_preview':plan});await h.page(h.el);await h.click();assert.equal(h.modal('confirm').disabled,true);
    await h.modal('confirm').onclick?.();assert.equal(applied(h).length,0);
  }
});
test('expired preview confirmation cannot reuse its old token after the service rejects it',async t=>{
  const h=fixture(t,{'/api/workspace/detach_apply':()=>{throw Error('移除预览已过期，请重新预览。');}});await h.page(h.el);await h.click();const old=h.modal('confirm').onclick;
  await old();assert.match(h.modal('error').textContent,/已过期/);assert.equal(h.modal('confirm').disabled,true);await old();assert.equal(applied(h).length,1);assert.equal(h.toasts.length,0);assert.equal(h.key('detach').disabled,false);
});
test('a mismatched preview root is rejected without applying in the other workspace, and all warnings are escaped',async t=>{
  const evil='<img src=x onerror="bad">',escaped=fixture(t,{'/api/workspace/detach_preview':{...preview(),warnings:[evil]}});await escaped.page(escaped.el);await escaped.click();
  assert(!escaped.html().includes('<img'));assert.match(escaped.html(),/&lt;img/);escaped.modal('cancel').onclick();
  const h=fixture(t,{'/api/workspace/detach_preview':{...preview(),root:'E:/Other'}});await h.page(h.el);await h.click();
  assert.equal(h.opens(),0);assert.match(h.key('error').textContent,/工作环境.*切换|工作区.*变化|重新检查/);await h.modal('confirm')?.onclick?.();assert.equal(applied(h).length,0);
});
