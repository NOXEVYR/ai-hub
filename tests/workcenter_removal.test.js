const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/workcenter.js');
const doc={id:'doc_fixture',title:'正式报告 <script>',path:'D:/Studio/report.md',project_name:'项目',category:'report',intake_status:'registered',status:'available'};
function harness(handler){
  const nodes=new Map(),requests=[];let root='D:/Studio',hidden=false,modalOpens=0;
  function get(selector){if(!nodes.has(selector))nodes.set(selector,{innerHTML:'',textContent:'',value:'',hidden:false,isConnected:true,disabled:false,dataset:{},setAttribute(){},focus(){},querySelector:get,querySelectorAll:()=>[]});return nodes.get(selector);}
  const modal=get('#modal'),key=id=>get('#wc-'+id);
  const el={isConnected:true,dataset:{},classList:{add(){},remove(){}},querySelector:get,ownerDocument:{querySelector:get},innerHTML:'',querySelectorAll(selector){
    const targets={'[data-wc-remove]':['list','remove','wcRemove'],'[data-wc-restore]':['removed-list','restore','wcRestore']};
    if(!targets[selector])return[];const [list,attr,dataset]=targets[selector],result=[];
    for(const match of key(list).innerHTML.matchAll(new RegExp('data-wc-'+attr+'="([^"]+)"','g'))){const node=get('button-'+attr+'-'+match[1]);node.dataset[dataset]=match[1];result.push(node);}return result;
  }};
  const api=async(url,opts)=>{requests.push({url,body:opts?.body});const result=await handler?.(url,opts?.body);if(result!==undefined)return result;
    if(url.startsWith('/api/workcenter/documents?'))return{items:hidden?[]:[doc],total:hidden?0:1,page:1,page_size:30,workspace_root:root,facets:{},coverage:{}};
    if(url.startsWith('/api/workcenter/document?'))return{...doc,preview_supported:true,content:'fixture'};
    if(url==='/api/workcenter/removal-preview')return{document:doc,preview_token:'preview',warning:'正式报告或已提交成果，原文件保留，可恢复。',workspace_root:root};
    if(url==='/api/workcenter/removal-apply'){hidden=true;return{removed:true};}
    if(url.startsWith('/api/workcenter/removals?'))return{workspace_root:root,items:hidden?[{...doc,removed_at:123,can_restore:true}]:[],total:hidden?1:0,page:1,page_size:30};
    if(url==='/api/workcenter/removal-restore'){hidden=false;return{restored:true};}return{};
  };
  const closeModal=()=>{modal.onModalClose?.();modal.onModalClose=null;modal.innerHTML='';};
  const openModal=html=>{modal.innerHTML=html;modalOpens++;};
  return{el,key,modal,requests,api,get,setRoot:value=>root=value,opens:()=>modalOpens,page:ui.createReports({api,copyPath:async()=>{},openModal,closeModal})};
}

test('every collected document and reader has list removal action with escaped titles',async()=>{
  const html=ui.documentsHTML([doc,{...doc,id:'old',intake_status:'legacy'}],'');
  assert.equal((html.match(/data-wc-remove=/g)||[]).length,2);assert(html.includes('移出列表'));assert(!html.includes('<script>'));
  const h=harness();await h.page(h.el,new URLSearchParams({document_id:doc.id}));assert.match(h.key('reader').innerHTML,/id="wc-remove"/);
});
test('cancel only requests read-only preview and preserves selection and editing draft',async()=>{
  const h=harness();await h.page(h.el,new URLSearchParams({document_id:doc.id}));h.key('edit-project').value='未保存';
  await h.key('remove').onclick();assert.match(h.modal.innerHTML,/确认移出报告列表/);assert.match(h.modal.innerHTML,/原工具记忆会保留/);assert.match(h.modal.innerHTML,/正式报告或已提交成果/);assert(!h.modal.innerHTML.includes('<script>'));
  h.get('#wc-removal-cancel').onclick();assert(!h.requests.some(r=>r.url.endsWith('removal-apply')));
  assert.equal(h.el.dataset.wcSelected,doc.id);assert.equal(h.key('edit-project').value,'未保存');assert.equal(h.key('reader').hidden,false);
});
test('confirm binds current root, hides selected reader only on success and lists working restore action',async()=>{
  const h=harness();await h.page(h.el,new URLSearchParams({document_id:doc.id}));await h.key('remove').onclick();await h.get('#wc-removal-confirm').onclick();
  assert.deepEqual(h.requests.find(r=>r.url.endsWith('removal-apply')).body,{preview_token:'preview',_workspace_root:'D:/Studio'});assert.equal(h.key('reader').hidden,true);assert.equal(h.el.dataset.wcSelected,'');assert.match(h.key('message').textContent,/原文件/);
  await h.key('removed-toggle').onclick();assert.match(h.key('removed-list').innerHTML,/恢复到列表/);
  await h.get('button-restore-'+doc.id).onclick();assert.deepEqual(h.requests.find(r=>r.url.endsWith('removal-restore')).body,{document_id:doc.id,_workspace_root:'D:/Studio'});
  assert.match(h.key('list').innerHTML,/正式报告/);assert.match(h.key('removed-list').innerHTML,/没有已移出/);
});
test('stale apply error preserves reader, draft and confirmation selection for retry',async()=>{
  const h=harness(url=>{if(url.endsWith('removal-apply'))throw Error('预览已过期，请重新预览。');});await h.page(h.el,new URLSearchParams({document_id:doc.id}));h.key('edit-project').value='保留草稿';await h.key('remove').onclick();await h.get('#wc-removal-confirm').onclick();
  assert.match(h.get('#wc-removal-error').textContent,/过期/);assert.equal(h.get('#wc-removal-confirm').disabled,false);assert.equal(h.key('edit-project').value,'保留草稿');assert.equal(h.el.dataset.wcSelected,doc.id);assert.equal(h.key('reader').hidden,false);
});
test('list removal can preview without reading original document body',async()=>{
  const h=harness();await h.page(h.el);await h.get('button-remove-'+doc.id).onclick();assert.equal(h.opens(),1);assert(!h.requests.some(r=>r.url.startsWith('/api/workcenter/document?')));
});
test('restore failure keeps tombstone and allows retry',async()=>{
  const h=harness(url=>{if(url.endsWith('removal-restore'))throw Error('记录已变化');});await h.page(h.el);await h.get('button-remove-'+doc.id).onclick();await h.get('#wc-removal-confirm').onclick();await h.key('removed-toggle').onclick();await h.get('button-restore-'+doc.id).onclick();
  assert.match(h.key('removed-error').textContent,/变化/);assert.match(h.key('removed-list').innerHTML,/恢复到列表/);assert.equal(h.get('button-restore-'+doc.id).disabled,false);
});
test('workspace switch cancels pending dialog and invalidates its old confirmation',async()=>{
  const h=harness();await h.page(h.el);await h.get('button-remove-'+doc.id).onclick();const confirm=h.get('#wc-removal-confirm').onclick;
  h.setRoot('E:/Other');await h.key('refresh').onclick();await confirm();assert(!h.requests.some(r=>r.url.endsWith('removal-apply')));assert.equal(h.modal.innerHTML,'');assert.equal(h.key('removed-panel').hidden,true);
});
test('late preview on disconnected page cannot open a confirmation',async()=>{
  let resolve;const wait=new Promise(r=>resolve=r);const h=harness(url=>url.endsWith('removal-preview')?wait:undefined);await h.page(h.el);const pending=h.get('button-remove-'+doc.id).onclick();h.el.isConnected=false;resolve({document:doc,preview_token:'late'});await pending;assert.equal(h.opens(),0);
});
