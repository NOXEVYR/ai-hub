const test=require('node:test'),assert=require('node:assert/strict');
const ui=require('../frontend/workcenter.js');
const data={items:[],total:0,page:1,page_size:30,workspace_root:'D:/Studio',catalog_total:142,counts:{project:2,candidate:130,template:4,source:6},facets:{source_types:[{value:'any',label:'通用来源',count:40},{value:'codex',label:'Codex',count:80}]}};
const project={id:'project_1',entry_kind:'project',name:'示例项目',path:'D:/Studio/项目',document_count:3,report_count:1,linked_tools:['codex'],task_count:2,artifact_count:1,source_types:['any'],representative_document_id:'doc_1'};
function harness(handler){
  const nodes=new Map(),buttons=new Map(),requests=[],navigations=[],copies=[];
  const get=selector=>{if(!nodes.has(selector)){
    const node={value:'',textContent:'',innerHTML:'',disabled:false,dataset:{},setAttribute(){},querySelector:get,querySelectorAll:find};
    if(selector==='#wc-project-source'){
      let markup='',value='';
      Object.defineProperty(node,'innerHTML',{get:()=>markup,set:html=>{markup=html;value=html.match(/<option value="([^"]*)" selected/)?.[1]??html.match(/<option value="([^"]*)"/)?.[1]??'';}});
      Object.defineProperty(node,'value',{get:()=>value,set:next=>{value=[...markup.matchAll(/<option value="([^"]*)"/g)].some(m=>m[1]===next)?next:'';}});
    }
    nodes.set(selector,node);
  }return nodes.get(selector);};
  function find(selector){const attr=selector.match(/^\[([^\]]+)\]$/)?.[1];if(!attr)return[];const found=[];for(const node of nodes.values())for(const match of node.innerHTML.matchAll(/<(?:button|a)\b([^>]+)>/g)){const attributes=match[1];if(!new RegExp('(?:^|\\s)'+attr+'=').test(attributes))continue;const key=attributes;if(!buttons.has(key)){const dataset={};for(const pair of attributes.matchAll(/data-([\w-]+)="([^"]*)"/g))dataset[pair[1].replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]=pair[2].replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');buttons.set(key,{dataset,disabled:false,setAttribute(){}});}found.push(buttons.get(key));}return found;}
  const el={isConnected:true,dataset:{},innerHTML:'',classList:{add(){},remove(){}},querySelector:get,querySelectorAll:find};
  const api=async(url,options)=>{requests.push({url,body:options?.body});return handler?handler(url,options):data;};
  const env={api,nav:(page,params)=>navigations.push({page,params}),copyPath:path=>copies.push(path),legacyPage:async(_el,_params,restored)=>{get('#legacy').restored=restored;}};
  return {el,key:id=>get('#wc-project-'+id),requests,navigations,copies,buttons:find,page:ui.createProjects(env),env};
}

test('formal projects are default, with all-directory category counts and no repeated hero',async()=>{
  const h=harness();await h.page(h.el);const query=new URLSearchParams(h.requests[0].url.split('?')[1]);
  assert.equal(query.get('entry_kind'),'project');assert.equal(query.get('page'),'1');assert(!query.has('kind'));
  assert.match(h.key('kinds').innerHTML,/目录候选<span>130/);assert.match(h.key('count').textContent,/筛选结果 0 个 · 全目录 142 个/);
  assert(!h.el.innerHTML.includes('跨工具'));assert(!h.key('content').innerHTML.includes('<h2'));
  assert.match(h.key('cards').innerHTML,/不会自动登记项目/);assert.match(h.key('cards').innerHTML,/前往项目登记/);
});

test('rows retain provenance, expose only recorded associations and escape metadata',()=>{
  const candidate=ui.projectCards([{...project,entry_kind:'candidate',document_count:0,name:'<img src=x>',tool:'陌生工具',linked_tools:['虚假协作'],task_count:9}],{kind:'candidate'});
  assert.match(candidate,/未收录资料/);assert.match(candidate,/不代表目录为空/);assert.match(candidate,/目录发现 · 未登记/);
  assert(!candidate.includes('虚假协作'));assert(!candidate.includes('9 个任务'));assert(!candidate.includes('data-wc-project="'));assert(!candidate.includes('<img'));assert(candidate.includes('&lt;img'));
  const formal=ui.projectCards([project]);assert.match(formal,/codex/);assert.match(formal,/2 个任务 · 1 份提交成果/);assert.match(formal,/3 份文档/);
  const unknown=ui.projectCards([{...project,linked_tools:[],tool:'陌生工具'}]);assert.match(unknown,/尚无关联工具证据/);assert(!unknown.includes('跨工具'));
  assert.match(ui.projectCards([{...project,registration_status:'created'}]),/已有创建记录/);
});

test('empty result distinguishes an active filter from absent registrations',()=>{
  const filtered=ui.projectCards([],{filtered:true});assert.match(filtered,/当前筛选没有匹配/);assert(!filtered.includes('尚无已登记项目'));
  assert.match(ui.projectCards([],{kind:'source'}),/接入来源并盘点/);
  assert.match(ui.projectCards([{...project,entry_kind:'template'}]),/创建参考 · 未登记/);
  assert.match(ui.projectCards([{...project,entry_kind:'source'}]),/来源范围 · 不代表项目/);
});

test('filter and pagination navigation preserve selected category, query and source',async()=>{
  const h=harness(()=>({...data,total:75,items:[project]}));await h.page(h.el,new URLSearchParams({kind:'candidate',q:'镜头',source_type:'codex',page:'2'}));
  const query=new URLSearchParams(h.requests[0].url.split('?')[1]);assert.equal(query.get('entry_kind'),'candidate');assert.equal(query.get('query'),'镜头');assert.equal(query.get('source_type'),'codex');
  h.key('next').onclick();assert.deepEqual(h.navigations.pop(),{page:'projects',params:{kind:'candidate',q:'镜头',source_type:'codex',page:3}});
  h.key('query').value=' 新关键词 ';h.key('source').value='any';h.key('filter').onsubmit({preventDefault(){}});
  assert.deepEqual(h.navigations.pop(),{page:'projects',params:{kind:'candidate',q:'新关键词',source_type:'any',page:1}});
  h.key('reset').onclick();assert.deepEqual(h.navigations.pop().params,{kind:'candidate',q:'',source_type:'',page:1});
});

test('capture and restoration preserve edits and return-page state',async()=>{
  const h=harness();await h.page(h.el,new URLSearchParams(),{kind:'source',query:'未提交搜索',source_type:'custom-tool',page:3});
  assert.deepEqual(ui.captureProjects(h.el),{kind:'source',query:'未提交搜索',source_type:'custom-tool',page:3});
  assert.match(h.key('source').innerHTML,/custom-tool（当前筛选）/);
  h.key('query').value='编辑后';assert.equal(ui.captureProjects(h.el).query,'编辑后');
});

test('document navigation, copy and reveal keep existing identity boundaries',async()=>{
  const h=harness(url=>url.startsWith('/api/workcenter/projects?')?{...data,items:[project],total:1}:{});await h.page(h.el);
  h.buttons('[data-wc-project]')[0].onclick();assert.deepEqual(h.navigations.pop(),{page:'reports',params:{project_id:'project_1'}});
  h.buttons('[data-wc-project-copy]')[0].onclick();assert.deepEqual(h.copies,[project.path]);
  await h.buttons('[data-wc-project-reveal]')[0].onclick();assert.deepEqual(h.requests.at(-1),{url:'/api/workcenter/reveal',body:{document_id:'doc_1',_workspace_root:'D:/Studio'}});
  const unindexed=ui.projectCards([{...project,document_count:0,representative_document_id:null}]);
  assert(!unindexed.includes('data-context-path'));assert(!unindexed.includes('data-wc-document'));assert(!unindexed.includes('data-wc-project-reveal'));
  assert.match(h.key('cards').innerHTML,/data-wc-document="doc_1"/);
});

test('older request cannot overwrite a subsequent registry or catalog page',async()=>{
  let resolve;const h=harness(url=>url==='/api/harnesses'?{items:[]}:new Promise(r=>resolve=r)),pending=h.page(h.el);
  await h.page(h.el,new URLSearchParams({view:'registry'}),{legacy:{draft:'kept'}});
  resolve({...data,items:[project],total:1});await pending;
  assert.equal(h.el.dataset.wcView,'registry');assert.equal(h.key('cards').innerHTML,'');
  const registryNode=h.el.querySelector('#legacy');assert.deepEqual(registryNode.restored,{draft:'kept'});
});

test('detached page ignores both success and failure',async()=>{
  let reject;const h=harness(url=>url==='/api/harnesses'?{items:[]}:new Promise((_,r)=>reject=r)),pending=h.page(h.el);h.el.isConnected=false;reject(new Error('旧页面错误'));await pending;assert.equal(h.key('error').textContent,'');
});

test('registered work-end display names label source facets and evidence tools without promoting templates',async()=>{
  const h=harness(url=>url==='/api/harnesses'?{items:[{id:'codex',name:'我的 Codex'},{id:'dsh',name:'<DSH 登记>'}],templates:[{id:'recipe',name:'未接入配方'}]}:{...data,items:[{...project,linked_tools:['codex','dsh'],source_types:['any','codex']}],facets:{source_types:[{value:'any',label:'any',count:4},{value:'codex',label:'codex',count:6},{value:'recipe',label:'recipe',count:1}]}});
  await h.page(h.el);
  assert.match(h.key('source').innerHTML,/通用来源 \(4\)/);assert.match(h.key('source').innerHTML,/我的 Codex \(6\)/);
  assert.match(h.key('cards').innerHTML,/我的 Codex \/ &lt;DSH 登记&gt;/);assert.match(h.key('cards').innerHTML,/通用来源 \/ 我的 Codex/);
  assert(!h.key('source').innerHTML.includes('未接入配方'));assert(!h.key('cards').innerHTML.includes('<DSH 登记>'));
});

test('work-end names failing to load do not block the project catalog or leak names into reports',async()=>{
  const h=harness(url=>{if(url==='/api/harnesses')throw Error('名称服务离线');return{...data,items:[project],total:1};});await h.page(h.el);
  assert.match(h.key('cards').innerHTML,/示例项目/);assert.match(h.key('count').textContent,/筛选结果 1 个/);assert.equal(h.key('error').textContent,'');
  assert(!ui.documentsHTML([{id:'d',tool:'codex',title:'报告'}],'').includes('我的 Codex'));
});

test('catalog warnings expose unreadable or partial sources without claiming an empty workspace',async()=>{
  const h=harness(()=>({...data,coverage:{warnings:['登记不可读 <script>'],project_discovery_partial:true,legacy_discovery_partial:true,unavailable_sources:2}}));await h.page(h.el);
  const warning=h.key('coverage').innerHTML;assert.match(warning,/登记不可读 &lt;script&gt;/);assert.match(warning,/目录发现范围不完整/);assert.match(warning,/既有资料发现范围不完整/);assert.match(warning,/2 个来源不可访问/);assert.match(warning,/计数仅代表已读取范围/);
});

test('relative display requires a real workspace boundary and preserves external or ambiguous paths',()=>{
  assert.equal(ui.projectDisplayPath('d:\\STUDIO\\40_Projects\\目录-3','D:/Studio/'),'工作区/40_Projects/目录-3');
  assert.equal(ui.projectDisplayPath('D:/Studio','D:/Studio'),'工作区');
  assert.equal(ui.projectDisplayPath('D:/Studio-other/40_Projects/X','D:/Studio'),'D:/Studio-other/40_Projects/X');
  assert.equal(ui.projectDisplayPath('D:/Studio/../Private/X','D:/Studio'),'D:/Studio/../Private/X');
  assert.equal(ui.projectDisplayPath('/studio/40_Projects/X','/Studio'),'/studio/40_Projects/X');
  assert.equal(ui.projectDisplayPath('E:/External/X','D:/Studio'),'E:/External/X');
  assert.equal(ui.projectDisplayPath('\\\\SERVER\\Share\\Work\\X','//server/share/work'),'工作区/X');
  assert.equal(ui.projectDisplayPath('D:/Studio/X',''),'D:/Studio/X');
  assert.equal(ui.projectDisplayPath('d:/Studio/X','D:/'),'工作区/Studio/X');
  assert.equal(ui.projectDisplayPath('/Projects/X','/'),'工作区/Projects/X');
});

test('compact directory text escapes title metadata and copy uses the original full path',async()=>{
  const path='D:/Studio/40_Projects/<目录> "说明"',p={...project,path,name:'<项目>',description:'<说明> '+ '很长的说明'.repeat(20)};
  const h=harness(url=>url==='/api/harnesses'?{items:[]}:{...data,items:[p],total:1});await h.page(h.el);
  const html=h.key('cards').innerHTML;
  assert.match(html,/title="D:\/Studio\/40_Projects\/&lt;目录&gt; &quot;说明&quot;"/);
  assert.match(html,/>工作区\/40_Projects\/&lt;目录&gt; &quot;说明&quot;<\/span>/);
  assert.match(html,/wc-project-description" title="&lt;说明&gt;/);assert(!html.includes('<目录>'));assert(!html.includes('<项目>'));
  const encoded=h.buttons('[data-wc-project-copy]')[0];assert.equal(encoded.dataset.wcProjectCopy,path);encoded.onclick();assert.deepEqual(h.copies,[path]);
  const normal=harness(url=>url==='/api/harnesses'?{items:[]}:{...data,items:[{...project,path:'D:\\Studio\\40_Projects\\目录-3'}],total:1});await normal.page(normal.el);
  assert.match(normal.key('cards').innerHTML,/>工作区\/40_Projects\/目录-3<\/span>/);normal.buttons('[data-wc-project-copy]')[0].onclick();assert.deepEqual(normal.copies,['D:\\Studio\\40_Projects\\目录-3']);
});

test('pending source options preserve immediate search and navigation snapshots before facets arrive',async()=>{
  let resolve;const h=harness(url=>url==='/api/harnesses'?{items:[]}:new Promise(r=>resolve=r)),pending=h.page(h.el,new URLSearchParams({kind:'candidate',q:'目录-3',source_type:'codex',page:2}));
  assert.equal(h.key('source').value,'codex');assert.match(h.key('source').innerHTML,/读取中/);
  h.key('filter').onsubmit({preventDefault(){}});assert.deepEqual(h.navigations.pop().params,{kind:'candidate',q:'目录-3',source_type:'codex',page:1});
  const snapshot=ui.captureProjects(h.el);assert.equal(snapshot.source_type,'codex');h.el.isConnected=false;
  resolve({...data,items:[],facets:{source_types:[]}});await pending;
  const restored=harness();await restored.page(restored.el,new URLSearchParams(),snapshot);assert.equal(restored.key('source').value,'codex');assert.equal(ui.captureProjects(restored.el).query,'目录-3');
});
