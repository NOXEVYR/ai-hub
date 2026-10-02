const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../frontend/app.js'),'utf8');
const start=source.indexOf('  let polling = null;');
const end=source.indexOf('  setInterval(pollJobs, 2500);',start);
function fixture(){
  const pending=[],elements=new Map();let refreshes=0;
  for(const key of ['#jobbar','#jobbar-text','#sidebar-status']){
    const classes=new Set(['hidden']);elements.set(key,{textContent:'',innerHTML:'',classList:{add:c=>classes.add(c),remove:c=>classes.delete(c),contains:c=>classes.has(c)}});
  }
  const context={api:()=>new Promise((resolve,reject)=>pending.push({resolve,reject})),
    $:key=>elements.get(key),esc:v=>v,route:()=>refreshes++};
  vm.runInNewContext(source.slice(start,end)+'\nthis.poll = pollJobs;',context);
  return {poll:context.poll,pending,elements,refreshes:()=>refreshes};
}
const flush=()=>new Promise(resolve=>setImmediate(resolve));
test('late idle snapshot cannot erase newer running job or refresh current page',async()=>{
  const f=fixture();f.poll();f.poll();
  f.pending[1].resolve({jobs:[{name:'当前扫描',status:'running',progress:'50%'}]});await flush();
  f.pending[0].resolve({jobs:[]});await flush();
  assert.equal(f.elements.get('#jobbar').classList.contains('hidden'),false);
  assert.equal(f.elements.get('#jobbar-text').textContent,'当前扫描: 50%');
  assert.match(f.elements.get('#sidebar-status').innerHTML,/扫描中/);
  assert.equal(f.refreshes(),0);
  f.poll();f.pending[2].resolve({jobs:[{name:'当前扫描',status:'completed'}]});await flush();
  assert.equal(f.elements.get('#jobbar').classList.contains('hidden'),true);
  assert.equal(f.refreshes(),1);
});
test('late poll failure cannot replace a newer healthy status',async()=>{
  const f=fixture();f.poll();f.poll();
  f.pending[1].resolve({jobs:[]});await flush();f.pending[0].reject(Error('old failure'));await flush();
  assert.equal(f.elements.get('#sidebar-status').textContent,'');
  assert.match(f.elements.get('#sidebar-status').innerHTML,/空闲/);
  f.poll();f.pending[2].reject(Error('current failure'));await flush();
  assert.equal(f.elements.get('#sidebar-status').textContent,'本地服务暂时未连接');
});
test('continuous slow responses still update while later polls remain pending',async()=>{
  const f=fixture();f.poll();
  for(let number=0;number<4;number++){
    f.poll();
    f.pending[number].resolve({jobs:[{name:'慢速扫描',status:'running',progress:String(number+1)}]});
    await flush();
    assert.equal(f.elements.get('#jobbar').classList.contains('hidden'),false);
    assert.equal(f.elements.get('#jobbar-text').textContent,'慢速扫描: '+String(number+1));
    assert.match(f.elements.get('#sidebar-status').innerHTML,/扫描中/);
  }
  assert.equal(f.refreshes(),0);
});
