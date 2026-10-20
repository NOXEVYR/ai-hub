'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs');
const governance=require('../frontend/model-governance.js');
function report(overrides={}){
  return {observed_at:'synthetic-time',summary:{indexed_rows:2,identified_files:1,identified_file_bytes:10,indexed_bytes:20,missing_rows:0,denied_rows:0,state_conflict_rows:0,same_file_alias_groups:1,hardlink_groups:1,nonmodel_candidate_rows:0,identity_unknown_rows:0,changed_identity_groups:0,size_conflict_rows:0,indexed_size_unknown_rows:0,observed_size_unknown_rows:0,alias_path_bytes:10},coverage:{whole_scope:true,has_more:false},identity_coverage_complete:true,items:[],...overrides};
}
function harness(api){
  const nodes=new Map();const node=key=>{if(!nodes.has(key))nodes.set(key,{disabled:false,textContent:'',innerHTML:''});return nodes.get(key);};
  const el={isConnected:true,innerHTML:'',querySelector:node};
  return {el,node,page:governance.createPage({api,heading:()=>'',fmtSize:n=>n+' B'})};
}
test('page entry is inert; only explicit button requests a bounded diagnostic',async()=>{
  const requests=[],h=harness(async url=>{requests.push(url);return report();});
  await h.page(h.el);assert.equal(requests.length,0);
  await h.node('#governance-read').onclick();
  assert.deepEqual(requests,['/api/models/governance?limit=5000']);
  assert.equal(h.node('#governance-next').disabled,true);
  assert.match(h.node('#governance-result').innerHTML,/可释放空间.*未知/);
});
test('continuation uses returned scope token and never sums batch sizes',async()=>{
  const urls=[],h=harness(async url=>{urls.push(url);return report({coverage:{whole_scope:false,has_more:urls.length===1,next_after_id:12,scope_token:'scope token'}});});
  await h.page(h.el);await h.node('#governance-read').onclick();await h.node('#governance-next').onclick();
  assert.match(urls[1],/after_id=12&scope_token=scope%20token/);
  assert.match(h.node('#governance-result').innerHTML,/禁止相加/);
});
test('failed continuation preserves historical report but disables stale cursor',async()=>{
  let calls=0;const h=harness(async()=>{if(calls++)throw Object.assign(new Error('source changed (409)'),{status:409});return report({coverage:{whole_scope:false,has_more:true,next_after_id:2,scope_token:'token'}});});
  await h.page(h.el);await h.node('#governance-read').onclick();const old=h.node('#governance-result').innerHTML;
  await h.node('#governance-next').onclick();
  assert.equal(h.node('#governance-result').innerHTML,old);assert.equal(h.node('#governance-next').disabled,true);
  assert.equal(h.node('#governance-read').disabled,false);assert.match(h.node('#governance-status').textContent,/历史观察/);
});
test('detached diagnostic request cannot replace the next page',async()=>{
  let resolve;const h=harness(()=>new Promise(r=>resolve=r));await h.page(h.el);
  const pending=h.node('#governance-read').onclick();h.el.isConnected=false;
  h.node('#governance-result').innerHTML='new page';resolve(report());await pending;
  assert.equal(h.node('#governance-result').innerHTML,'new page');
});
test('report escapes paths, bounds detail rendering and contains no disposition control',()=>{
  const item={path:'<img src=x onerror=bad>',status:'denied',intake:'unknown'};
  const html=governance.render(report({items:Array(105).fill(item)}),n=>n+' B');
  assert(!html.includes('<img'));assert.match(html,/&lt;img/);
  assert.equal((html.match(/<tr><td/g)||[]).length,100);assert.match(html,/展示前 100 条/);
  assert(!/<button/.test(html));assert.match(html,/另行取得处置授权/);
});
test('model navigation adds diagnosis without starting background diagnostics',()=>{
  const app=fs.readFileSync('frontend/app.js','utf8'),index=fs.readFileSync('frontend/index.html','utf8');
  assert.match(app,/governance:'models'/);assert.match(app,/\['governance','治理诊断'\]/);
  assert.match(app,/重复路径长度 · 非可释放空间/);assert(!app.includes('候选额外占用'));
  assert(index.indexOf('src="model-governance.js"')<index.indexOf('src="app.js"'));
  assert(!app.includes('/api/models/governance'));
});

test('duplicate click callbacks do not issue overlapping diagnostics',async()=>{
  let calls=0,resolve;const h=harness(()=>{calls++;return new Promise(r=>resolve=r);});await h.page(h.el);
  const first=h.node('#governance-read').onclick();
  await h.node('#governance-read').onclick();
  assert.equal(calls,1);assert.equal(h.node('#governance-read').disabled,true);
  resolve(report());await first;assert.equal(h.node('#governance-read').disabled,false);
});
