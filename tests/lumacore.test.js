'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {readFileSync}=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const read=file=>readFileSync(path.join(__dirname,'..',file),'utf8');
const app=read('frontend/app.js'),index=read('frontend/index.html'),css=read('frontend/lumacore.css');
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const overview=()=>({ai_root:'D:/Creative',parts:[{name:'20_Models',path:'D:/Creative/20_Models',size:100},{name:'40_Projects',path:'D:/Creative/40_Projects',size:20}],central_counts:{Checkpoint:2,Diffusion:3,LoRA:8},image_count:200,image_with_meta:150,unique_size:100,disk:{free:1000},functional_categories:[{id:'image',icon:'image',label:'图片创作',count:12}],recent_models:[{rowid_pk:42,filename:'sample.safetensors',mtype:'LoRA',family:'SDXL',size:10,mtime:123}],pending_updates:0,state_counts:{unchecked:10},scan_at:'2026-09-24',total_files:1000,indexed_records:20,unique_model_files:17,model_count:20});
const management=()=>({check:{status:'passed'},workflow_pending:2,workflow_reviewed:3,duplicate_groups:2,duplicate_candidate_bytes:20,verification_report:'D:/Creative/Reports/check.md',catalog_records:20,updated_at:'2026-09-24'});
function context(extra={}){
  const ctx={esc,icon:(name,size)=>`<svg data-icon="${esc(name)}" width="${size}"></svg>`,fmtSize:v=>`${v} B`,fmtDate:()=> '2026-09-24',typeBadge:val=>`<span class="badge">${esc(val)}</span>`,AIHubOrganizer:{workspaceBanner:()=>''},pages:{},...extra};
  vm.runInNewContext(app.slice(app.indexOf('  function renderOverview('),app.indexOf('\n  function bindModelLinks'))+'\nthis.renderOverview=renderOverview;',ctx);
  return ctx;
}

test('reorganized navigation preserves all routes, action IDs and module loading order',()=>{
  const routes=[...index.matchAll(/data-page="([^"]+)"/g)].map(m=>m[1]);
  const expected=['overview','models','images','workflows','llm','files','capabilities','collaboration','projects','reports','workspace','organizer','analysis','updates','settings'];
  assert.deepEqual(routes,expected);assert.equal(new Set(routes).size,routes.length);
  for(const id of ['app','sidebar','nav','sidebar-status','main','topbar','menu-toggle','nav-back','nav-back-label','page-title','jobbar','global-search','btn-check-updates','btn-rescan','page','drawer','drawer-mask','lightbox','modal-mask','modal','toast'])assert.equal([...index.matchAll(new RegExp(`id="${id}"`,'g'))].length,1,id);
  assert(index.indexOf('lumacore.css')>index.indexOf('collaboration.css'));
  for(const module of ['navigation.js','harnesses.js','workspace.js','collaboration.js','workcenter.js','capabilities.js','context-menu.js'])assert(index.indexOf(`src="${module}"`)<index.indexOf('src="app.js"'));
  assert.match(index,/brand-lumacore\.svg/);assert.match(index,/v2\.11\.3/);
  assert.match(index,/<title>曜核 · AI 资产与协作工作台<\/title>/);assert.match(index,/<b>曜核<\/b>/);
  assert(!index.includes('LUMACORE'));assert(!index.includes('LumaCore'));assert(!index.includes('曜瞳'));
});

test('overview keeps indexed scopes and model identity while prioritizing real working entrances',()=>{
  const html=context().renderOverview(overview(),management(),{});
  for(const page of ['models','images','capabilities','reports'])assert(html.includes(`class="lc-entry" data-nav="${page}"`));
  assert.match(html,/class="model-link" data-id="42"/);assert.match(html,/sample\.safetensors/);assert.match(html,/SDXL/);
  assert(html.indexOf('资产核心指标')<html.indexOf('常用工作入口'));
  assert(html.indexOf('最近修改的模型')<html.indexOf('按创作用途探索'));
  assert.match(html,/文件去重后/);assert.match(html,/硬链接可能重复计入/);
  assert.match(html,/未完整哈希确认/);assert.match(html,/尚未执行生成验收/);
  assert.match(html,/当前索引 20 条 \/ 17 个文件身份/);
  assert(!html.includes('workspace-banner'));assert(!html.includes('让每一次创作'));
});

test('overview escapes asset, directory and taxonomy data and handles an empty workspace',()=>{
  const ov=overview(),evil='<img src=x onerror="alert(1)">';
  ov.ai_root=evil;ov.parts[0].name=evil;ov.parts[0].path=evil;ov.recent_models[0].filename=evil;ov.functional_categories[0].label=evil;
  let html=context().renderOverview(ov,management(),{});
  assert(!html.includes('<img'));assert(html.includes('&lt;img'));
  ov.recent_models=[];ov.functional_categories=[];ov.ai_root='';
  html=context().renderOverview(ov,management(),{});
  assert.match(html,/尚无模型索引/);assert.match(html,/尚未配置/);assert.match(html,/模型入库后/);
});

test('late overview requests cannot overwrite a page after navigation',async()=>{
  let resolve,bindings=0;
  const waiting=new Promise(r=>{resolve=r;});
  const ctx=context({api:()=>waiting,skeleton:()=>'<p>loading</p>',bindNavigation:()=>bindings++,bindModelLinks:()=>bindings++,failPage:()=>{throw new Error('unexpected failure');}});
  const el={isConnected:true,innerHTML:'',classList:{add(){}}};
  const pending=ctx.pages.overview(el);el.isConnected=false;el.innerHTML='new page';resolve({});await pending;
  assert.equal(el.innerHTML,'new page');assert.equal(bindings,0);
});

test('brand glint pauses when hidden, respects reduced motion and adds no polling',()=>{
  const listeners={},classes=new Map(),doc={hidden:false,documentElement:{classList:{toggle:(name,on)=>classes.set(name,on)}},addEventListener:(name,fn)=>listeners[name]=fn};
  const snippet=app.slice(app.indexOf('  function bindBrandMotion('),app.indexOf('  bindBrandMotion(document);'));
  const ctx={};vm.runInNewContext(snippet+'\nthis.bind=bindBrandMotion;',ctx);ctx.bind(doc);
  assert.equal(classes.get('lc-motion-paused'),false);doc.hidden=true;listeners.visibilitychange();assert.equal(classes.get('lc-motion-paused'),true);
  doc.hidden=false;listeners.visibilitychange();assert.equal(classes.get('lc-motion-paused'),false);
  assert(!/setInterval|setTimeout|requestAnimationFrame/.test(snippet));
  assert.match(css,/prefers-reduced-motion:reduce/);assert.match(css,/\.brand-glint[\s\S]*animation:none; opacity:0/);
  assert.match(css,/lc-motion-paused[\s\S]*animation-play-state:paused/);
  assert.match(index,/brand-glint" aria-hidden="true"/);assert.match(index,/<a class="logo" href="#\/overview"/);
});

test('visual overrides keep compact navigation, scrolling tables, mobile breakpoints and focus affordances',()=>{
  assert(!css.includes('!important'));
  assert.match(css,/#nav \{[^}]*overflow-y:auto/);
  assert.match(css,/@media\(max-width:800px\)/);assert.match(css,/@media\(max-width:620px\)/);
  assert.match(css,/:focus-visible/);assert.match(css,/\.model-context-menu/);assert.match(css,/#drawer/);
  assert.match(app,/AIHubCollaboration\.capture\(view\)/);assert.match(app,/AIHubContextMenu\.install/);
});
