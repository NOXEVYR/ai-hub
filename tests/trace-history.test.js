const test=require('node:test');
const assert=require('node:assert/strict');
const {detailHTML}=require('../frontend/trace-history.js');

test('local excerpts render as text and only the selected revision is shared',()=>{
  const html=detailHTML({title:'<test>',cwd:'D:/local',tool:'zcode',ready:true,
    shared:false,partial:true,messages:[{role:'user',text:'<script>bad()</script>'}],
    files:[{relation:'mentioned',evidence:'会话提及',path:'D:/test.mov',review:'keep'}]});
  assert.match(html,/共享本次摘录给 AI/);
  assert.match(html,/部分记录/);
  assert.match(html,/对话提及/);
  assert.match(html,/已保留/);
  assert.match(html,/&lt;script&gt;/);
  assert(!html.includes('<script>'));
  assert(!html.includes('确认移入 Windows 回收站'));
});

test('a search match expands the message and escapes both the text and the literal query',()=>{
  const html=detailHTML({title:'sample',cwd:'',tool:'codex',ready:true,partial:false,messages:[
    {role:'assistant',text:'检查 [a+b] <x>'}],files:[]},'[a+b]');
  assert.match(html,/<details class="trace-message" open>/);
  assert.match(html,/<mark>\[a\+b\]<\/mark>/);
  assert.match(html,/&lt;x&gt;/);
});
test('source shortcuts link to filtered histories before or without a status response',async()=>{
  const {showToolNavigation}=require('../frontend/trace-history.js');
  const count={dataset:{traceCount:'codex'},textContent:''};
  const host={innerHTML:'',isConnected:true,querySelectorAll:()=>[count]};
  let respond;
  const pending=showToolNavigation(host,()=>new Promise(resolve=>{respond=resolve;}));
  assert.match(host.innerHTML,/按工具找对话/);
  for(const tool of ['codex','antigravity','dsh','zcode','claude-code','exchange']){
    assert.ok(host.innerHTML.includes('href="#/traces?tool='+tool+'"'));
  }
  assert.equal(count.textContent,'');
  respond({sources:[{tool:'codex',sessions:1234}]});
  await pending;
  assert.equal(count.textContent,'1,234 条');
  await showToolNavigation(host,async()=>{throw Error('offline');});
  assert.match(host.innerHTML,/href="#\/traces\?tool=codex"/);
});
