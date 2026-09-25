'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const path = require('node:path');
const workspace = require('../frontend/workspace.js');
const status = () => ({tools:[{id:'codex',name:'Codex',enabled:true},{id:'zcode',name:'ZCode',enabled:true},{id:'dsh',name:'DSH',enabled:true},{id:'workbuddy',name:'WorkBuddy',enabled:true}],root:'D:/Studio',configured:true,managed:true,available:true,revision:'r1',
  sources:{scan_roots:['D:/Studio/Models','D:/Studio/Missing'],output_roots:['D:/Studio/Results']},
  source_health:[{kind:'scan',path:'D:/Studio/Missing',status:'missing',reason:'不可访问'},
    {kind:'output',path:'D:/Studio/Results',status:'ok',reason:''}],
  discovery:{items:[{path:'D:/Studio/Training/Barbara/verify_out',reason:'训练验证输出'}]}});
const preview = () => ({token:'preview-token',can_apply:true,root:'D:/Studio',mode:'connect',
  directories:[{path:'D:/Studio/40_Projects',action:'create'}],
  files:[{path:'D:/Studio/00_Management/AIHub/WORKSPACE.md',action:'create'}],
  sources:status().sources,source_health:status().source_health,warnings:['将保留已有 AGENTS.md'],errors:[]});
const projectPreview=()=>({token:'project-token',can_apply:true,root:'D:/Studio/40_Projects/Film',
  directories:[{path:'D:/Studio/40_Projects/Film/Outputs',action:'create'}],
  files:[{path:'D:/Studio/40_Projects/Film/TASK.md',action:'create'}],warnings:['不会自动启动工具'],errors:[]});

test('project choices follow registered enabled tools including manual handoff without fixed defaults',async()=>{
  const h=harness({'/api/workspace/status':{...status(),tools:[{id:'new-worker',name:'新工作端',enabled:true,connection_mode:'mcp_stdio'},{id:'manual-worker',name:'手动工具',enabled:true,connection_mode:'manual'},{id:'retired-worker',name:'历史工具',enabled:false}]}});
  await h.page(h.el);
  assert.match(h.el.innerHTML,/data-ws-project-tool="new-worker"/);
  assert.match(h.el.innerHTML,/data-ws-project-tool="manual-worker"/);
  assert(!h.el.innerHTML.includes('data-ws-project-tool="retired-worker"'));
  assert(!h.el.innerHTML.includes('data-ws-project-tool="codex"'));
  assert.match(h.el.innerHTML,/已停用/);
});
function harness(overrides = {}) {
  const nodes = new Map(), requests = [], toasts = [], add = {dataset:{wsAdd:'0'},textContent:''};
  const tools=['codex','zcode','dsh','workbuddy'].map(id=>({dataset:{wsProjectTool:id},checked:false}));
  const node = selector => {if (!nodes.has(selector)) nodes.set(selector,{value:'',innerHTML:'',textContent:'',checked:false,disabled:false,isConnected:true});return nodes.get(selector);};
  const el = {isConnected:true,innerHTML:'',querySelector:node,querySelectorAll:selector => selector === '[data-ws-add]' ? [add] : selector === '[data-ws-project-tool]' ? tools : []};
  const defaults = {'/api/workspace/status':status(),'/api/overview':{scan_at:'2026-09-12 12:00'},'/api/jobs':{jobs:[]},
    '/api/workspace/preview':preview(),'/api/workspace/apply':{applied:true,root:'D:/Studio',scan_required:true,status:status()},'/api/scan/start':{started:true},
    '/api/workspace/project/preview':projectPreview(),'/api/workspace/project/apply':{applied:true,root:'D:/Studio/40_Projects/Film',prompt_path:'D:/Studio/40_Projects/Film/TASK.md',tools:['codex'],output_root_added:true}};
  const page = workspace.createPage({api:async(url,opts) => {
    requests.push({url,body:opts?.body});
    const value = Object.hasOwn(overrides,url) ? overrides[url] : defaults[url];
    return typeof value === 'function' ? value(opts?.body) : value;
  },heading:() => '',toast:(...args)=>toasts.push(args),pollJobs(){}});
  return {el,node,key:id=>node('#ws-'+id),requests,toasts,page,add,tools,submit:()=>node('#ws-form').onsubmit({preventDefault(){}}),projectSubmit:()=>node('#ws-project-form').onsubmit({preventDefault(){}})};
}

test('renders explicit source health, scan time, soft boundary and keeps unavailable paths',async()=>{
  const h = harness();await h.page(h.el);
  assert.equal(h.key('scan').value,'D:/Studio/Models\nD:/Studio/Missing');
  assert.equal(h.key('output').value,'D:/Studio/Results');
  assert.match(h.el.innerHTML,/2026-09-12 12:00/);
  assert.match(h.el.innerHTML,/未启用系统隔离或全盘写入监控/);
  assert.match(h.el.innerHTML,/不重写用户现有 AGENTS.md/);
  assert.match(h.el.innerHTML,/40_Projects/);
  assert.match(h.el.innerHTML,/Inputs、Work、Outputs、Deliverables/);
  assert.match(h.el.innerHTML,/70_Output 仅作为通用工具的出图落地区/);
  assert.match(h.el.innerHTML,/无需搬到项目 Outputs/);
  assert.match(h.el.innerHTML,/00_Management\/AIHub\/Templates\/PROJECT_TASK.md/);
  assert(!h.requests.some(r=>r.body));
});

test('candidate verify_out only enters form until explicit preview and apply',async()=>{
  const h = harness();await h.page(h.el);h.add.onclick();h.add.onclick();
  assert.equal(h.key('output').value,'D:/Studio/Results\nD:/Studio/Training/Barbara/verify_out');
  assert(!h.requests.some(r=>r.body));
  await h.submit();
  const request = h.requests.find(r=>r.url==='/api/workspace/preview');
  assert.deepEqual(request.body.output_roots,['D:/Studio/Results','D:/Studio/Training/Barbara/verify_out']);
  assert(!Object.hasOwn(request.body,'sources'));
  assert(!h.requests.some(r=>r.url==='/api/workspace/apply'));
  assert.match(h.key('preview').innerHTML,/WORKSPACE.md/);
  assert.match(h.key('preview').innerHTML,/我已核对/);
});

test('existing empty sources stay explicit while new empty sources use backend defaults',async()=>{
  const h = harness();await h.page(h.el);
  h.key('scan').value='';h.key('output').value='';await h.submit();
  assert.deepEqual(h.requests.at(-1).body,{mode:'connect',root:'D:/Studio',scan_roots:[],output_roots:[]});
  h.key('mode').value='create';h.key('root').value='E:/NewStudio';h.key('mode').onchange();await h.submit();
  assert.deepEqual(h.requests.at(-1).body,{mode:'create',root:'E:/NewStudio'});
});

test('switching to new workspace clears unchanged old paths but preserves an edited source',async()=>{
  const h=harness();await h.page(h.el);
  h.key('mode').value='create';h.key('mode').onchange();
  assert.equal(h.key('root').value,'');assert.equal(h.key('scan').value,'');assert.equal(h.key('output').value,'');
  h.key('mode').value='connect';h.key('mode').onchange();assert.equal(h.key('root').value,'D:/Studio');
  h.key('output').value='E:/New/verify_out';h.key('mode').value='create';h.key('mode').onchange();
  assert.equal(h.key('output').value,'E:/New/verify_out');
});

test('completed and failed scan jobs have explicit truthful status',async()=>{
  const completed=harness({'/api/jobs':{jobs:[{name:'scan',status:'done'}]}});await completed.page(completed.el);
  assert.match(completed.key('scan-state').textContent,/扫描已完成/);
  const failed=harness({'/api/jobs':{jobs:[{name:'scan',status:'error'}]}});await failed.page(failed.el);
  assert.match(failed.key('scan-state').textContent,/扫描失败/);
  assert.doesNotMatch(failed.key('scan-state').textContent,/扫描已完成/);
});

test('apply requires checked confirmation and a current applicable preview; scan is separate',async()=>{
  const h = harness();await h.page(h.el);await h.submit();
  await h.key('apply').onclick();assert(!h.requests.some(r=>r.url==='/api/workspace/apply'));
  h.key('confirm').checked=true;h.key('confirm').onchange();assert.equal(h.key('apply').disabled,false);
  await h.key('apply').onclick();
  assert.deepEqual(h.requests.find(r=>r.url==='/api/workspace/apply').body,{token:'preview-token'});
  assert.match(h.key('result').innerHTML,/工作环境已保存/);
  assert(!h.requests.some(r=>r.url==='/api/scan/start'));
  const original = global.setTimeout;
  global.setTimeout = () => 0;
  try {await h.key('start-scan').onclick();} finally {global.setTimeout=original;}
  assert(h.requests.some(r=>r.url==='/api/scan/start'));
  assert.match(h.key('scan-state').textContent,/扫描已开始/);
});

test('editing after preview invalidates the token and prevents applying stale settings',async()=>{
  const h=harness();await h.page(h.el);await h.submit();
  const apply=h.key('apply').onclick;
  h.key('confirm').checked=true;h.key('root').value='E:/Different';h.key('root').oninput();
  await apply();assert(!h.requests.some(r=>r.url==='/api/workspace/apply'));
  assert.match(h.key('preview').innerHTML,/重新检查/);
});

test('late preview cannot replace edited inputs or a newer preview',async()=>{
  const completions=[];const h=harness({'/api/workspace/preview':()=>new Promise(resolve=>completions.push(resolve))});
  await h.page(h.el);const first=h.submit();h.key('output').value='D:/Studio/New';h.key('output').oninput();
  const second=h.submit();completions[1]({...preview(),warnings:['new result']});await second;
  const accepted=h.key('preview').innerHTML;completions[0]({...preview(),warnings:['old result']});await first;
  assert.equal(h.key('preview').innerHTML,accepted);assert.match(accepted,/new result/);
});

test('late status and apply requests do not update a navigated-away page',async()=>{
  let release;const h=harness({'/api/workspace/status':()=>new Promise(resolve=>release=resolve)});
  const pending=h.page(h.el);h.el.isConnected=false;h.el.innerHTML='different page';release(status());await pending;
  assert.equal(h.el.innerHTML,'different page');
  let applyDone;const next=harness({'/api/workspace/apply':()=>new Promise(resolve=>applyDone=resolve)});
  await next.page(next.el);await next.submit();next.key('confirm').checked=true;
  const applying=next.key('apply').onclick();next.el.isConnected=false;applyDone({applied:true,root:'D:/Studio'});await applying;
  assert.equal(next.key('result').innerHTML,'');assert.equal(next.toasts.length,0);
});

test('rejected preview cannot be applied and API errors preserve input',async()=>{
  const h=harness({'/api/workspace/preview':{...preview(),can_apply:false,token:null,errors:['请使用规范路径']}});
  await h.page(h.el);await h.submit();h.key('confirm').checked=true;h.key('confirm').onchange();
  assert.equal(h.key('apply').disabled,true);await h.key('apply').onclick();
  assert(!h.requests.some(r=>r.url==='/api/workspace/apply'));assert.match(h.key('preview').innerHTML,/请使用规范路径/);
  const bad=harness({'/api/workspace/preview':()=>{throw new Error('路径无效');}});
  await bad.page(bad.el);bad.key('output').value='D:/Studio/verify_out';await bad.submit();
  assert.equal(bad.key('output').value,'D:/Studio/verify_out');assert.equal(bad.key('error').textContent,'路径无效');
});

test('navigation restores draft without persistent storage and refresh preserves edits',async()=>{
  const h=harness();const draft={mode:'connect',root:'D:/Studio',scan:'D:/Studio/Models',output:'D:/Studio/verify_out',draft:true};
  await h.page(h.el,undefined,draft);assert.deepEqual(workspace.capture(h.el),draft);
  await h.key('refresh').onclick();assert.equal(h.key('output').value,draft.output);
  const js=readFileSync(path.join(__dirname,'../frontend/workspace.js'),'utf8');assert.doesNotMatch(js,/localStorage|sessionStorage/);
});

test('escapes paths and mounts workspace through shared navigation before app loads',()=>{
  assert.match(workspace.healthHTML([{kind:'output',path:'<img src=x>',status:'outside',reason:'<script>'}]),/&lt;img/);
  assert.doesNotMatch(workspace.previewHTML({...preview(),warnings:['<script>']}),/<script>/);
  const html=readFileSync(path.join(__dirname,'../frontend/index.html'),'utf8');
  const app=readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8');
  assert.match(html,/href="#\/workspace" data-page="workspace"/);
  assert(html.indexOf('src="workspace.js"')<html.indexOf('src="app.js"'));
  assert.match(app,/pages\.workspace=AIHubWorkspace\.createPage/);
  assert.match(app,/AIHubWorkspace\.capture\(view\)/);
  assert.match(workspace.banner(),/href="#\/workspace"/);
});

test('tool capability rows report detection without inventing connection or launch support',()=>{
  const html=workspace.toolsHTML([{id:'codex',name:'Codex <test>',detected:true,available:false,launch_mode:'manual',rules_support:'AGENTS.md',enforcement:'soft',executable:null,notes:['需手动打开项目']}]);
  for(const text of ['Codex &lt;test&gt;','已发现安装','入口不可用','manual','AGENTS.md','soft','未记录可执行入口','需手动打开项目'])assert(html.includes(text));
  assert.doesNotMatch(html,/<button/);
  assert.match(workspace.toolsHTML(undefined),/尚无工具探测记录/);
  assert.match(workspace.toolsHTML([{id:'dsh'}]),/安装状态未确认/);
});

test('project creation previews chosen tools and requires confirmation; invalidates old workspace plan',async()=>{
  const h=harness();await h.page(h.el);await h.submit();const oldWorkspaceApply=h.key('apply').onclick;
  h.key('scan').value='D:/Studio/20_Models\nD:/Studio/60_Workflows';
  h.key('output').value='D:/Studio/70_Output\nD:/Studio/50_Training/Projects/Barbara/verify_out';
  h.key('project-name').value='Film';h.tools.forEach(c=>{c.checked=false;});h.tools[0].checked=true;h.tools[2].checked=true;
  await h.projectSubmit();assert.deepEqual(h.requests.at(-1).body,{name:'Film',tools:['codex','dsh']});
  assert.match(h.key('project-preview').innerHTML,/TASK.md/);
  await h.key('project-apply').onclick();assert(!h.requests.some(r=>r.url==='/api/workspace/project/apply'));
  h.key('project-confirm').checked=true;h.key('project-confirm').onchange();await h.key('project-apply').onclick();
  assert.deepEqual(h.requests.find(r=>r.url==='/api/workspace/project/apply').body,{token:'project-token'});
  assert.match(h.key('project-result').innerHTML,/项目工作区已创建/);
  assert.match(h.key('project-result').innerHTML,/TASK.md/);
  assert.match(h.key('project-result').innerHTML,/不代表工具已连接/);
  assert.match(h.key('project-result').innerHTML,/交接目标：Codex/);
  assert.match(h.key('output').value,/D:\/Studio\/40_Projects\/Film\/Outputs/);
  assert.equal(h.key('scan').value,'D:/Studio/20_Models\nD:/Studio/60_Workflows');
  assert.equal(h.key('output').value,'D:/Studio/70_Output\nD:/Studio/50_Training/Projects/Barbara/verify_out\nD:/Studio/40_Projects/Film/Outputs');
  h.key('confirm').checked=true;await oldWorkspaceApply();
  assert(!h.requests.some(r=>r.url==='/api/workspace/apply'));
  assert(!h.requests.some(r=>r.url.includes('launch') || r.url==='/api/scan/start'));
});

test('project preview cannot create against an unsaved root and source edits invalidate its token',async()=>{
  const h=harness();await h.page(h.el);h.key('root').value='E:/Unsaved';await h.projectSubmit();
  assert.match(h.key('project-error').textContent,/先保存/);
  assert(!h.requests.some(r=>r.url==='/api/workspace/project/preview'));
  h.key('root').value='D:/Studio';h.key('project-name').value='Film';await h.projectSubmit();
  const apply=h.key('project-apply').onclick;h.key('project-confirm').checked=true;
  h.key('output').oninput();await apply();assert(!h.requests.some(r=>r.url==='/api/workspace/project/apply'));
});

test('project late preview and detached apply cannot overwrite newer views',async()=>{
  let finish;const h=harness({'/api/workspace/project/preview':()=>new Promise(resolve=>finish=resolve)});
  await h.page(h.el);h.key('project-name').value='Film';const pending=h.projectSubmit();
  h.key('project-name').value='Other';h.key('project-name').oninput();const edited=h.key('project-preview').innerHTML;
  finish(projectPreview());await pending;assert.equal(h.key('project-preview').innerHTML,edited);
  let applied;const next=harness({'/api/workspace/project/apply':()=>new Promise(resolve=>applied=resolve)});
  await next.page(next.el);next.key('project-name').value='Film';await next.projectSubmit();next.key('project-confirm').checked=true;
  const saving=next.key('project-apply').onclick();await next.key('project-apply').onclick();
  assert.equal(next.requests.filter(r=>r.url==='/api/workspace/project/apply').length,1);
  next.el.isConnected=false;applied({applied:true,root:'D:/Studio/Film'});await saving;
  assert.equal(next.key('project-result').innerHTML,'');
});

test('project errors preserve name and selected tools; preview rejection cannot apply',async()=>{
  const h=harness({'/api/workspace/project/preview':{...projectPreview(),can_apply:false,token:null,errors:['项目已存在']}});
  await h.page(h.el);h.key('project-name').value='Film';h.tools[1].checked=true;await h.projectSubmit();
  h.key('project-confirm').checked=true;h.key('project-confirm').onchange();assert.equal(h.key('project-apply').disabled,true);
  await h.key('project-apply').onclick();assert(!h.requests.some(r=>r.url==='/api/workspace/project/apply'));
  assert.match(h.key('project-preview').innerHTML,/项目已存在/);
  const bad=harness({'/api/workspace/project/preview':()=>{throw new Error('项目接入接口尚不可用');}});
  await bad.page(bad.el);bad.key('project-name').value='Film';bad.tools[1].checked=true;await bad.projectSubmit();
  assert.equal(bad.key('project-name').value,'Film');assert(bad.tools[1].checked);
  assert.match(bad.key('project-error').textContent,/接口尚不可用/);
});

test('project draft participates in in-memory navigation snapshots',async()=>{
  const h=harness();await h.page(h.el);h.key('project-name').value='Draft';h.tools.forEach(c=>{c.checked=false;});h.tools[3].checked=true;
  const snapshot=workspace.capture(h.el);assert.deepEqual(snapshot.project,{name:'Draft',tools:['workbuddy']});
  await h.page(h.el,undefined,snapshot);assert.equal(h.key('project-name').value,'Draft');assert(h.tools[3].checked);
});

test('four tools default selected, empty selection is blocked, and unmanaged roots require workspace apply',async()=>{
  const h=harness();await h.page(h.el);assert(h.tools.every(c=>c.checked));
  h.key('project-name').value='Film';await h.projectSubmit();
  assert.deepEqual(h.requests.at(-1).body.tools,['codex','zcode','dsh','workbuddy']);
  h.tools.forEach(c=>{c.checked=false;});const count=h.requests.length;await h.projectSubmit();
  assert.equal(h.requests.length,count);assert.match(h.key('project-error').textContent,/至少选择一个/);
  const unmanaged=harness({'/api/workspace/status':{...status(),managed:false}});
  await unmanaged.page(unmanaged.el);unmanaged.key('project-name').value='Film';await unmanaged.projectSubmit();
  assert(!unmanaged.requests.some(r=>r.url==='/api/workspace/project/preview'));
  await unmanaged.submit();unmanaged.key('confirm').checked=true;await unmanaged.key('apply').onclick();
  await unmanaged.projectSubmit();assert(unmanaged.requests.some(r=>r.url==='/api/workspace/project/preview'));
});
