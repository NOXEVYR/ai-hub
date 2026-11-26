const test=require('node:test');
const assert=require('node:assert/strict');
const ui=require('../frontend/file-lifecycle.js');
test('unknown allocation remains visibly unmeasured',()=>{
  assert.equal(ui.size(null),'未测量');assert.equal(ui.size(0),'0 B');assert.equal(ui.size(4096),'4.0 KiB');
});
test('file rows escape paths and show protected ownership evidence',()=>{
  const html=ui.rows([{relative_path:'deep/<img src=x>.py',path:'C:/fixture/<img src=x>.py',project_id:'p',task_id:'t',tool_id:'tool',category:'source',purpose:'source',lifecycle:'active',age_days:2,link_count:1,logical_bytes:7,allocated_bytes:null,decision:'protected',reasons:['source_protected','active_task_or_owner']}]);
  assert.ok(html.includes('&lt;img src=x&gt;.py'));assert.ok(!html.includes('<img src=x>'));
  assert.ok(html.includes('活动任务'));assert.ok(html.includes('源码保留'));assert.ok(html.includes('分配 未测量'));
});
test('summary distinguishes estimated release, actual release and truncation',()=>{
  const html=ui.summary({examined_entries:5,truncated:true,summary:{files:3,logical_bytes:10000,allocated_bytes_known:4096,allocation_unknown_files:1,candidate_files:1,estimated_releasable_bytes:null,actual_released_bytes:0}});
  assert.ok(html.includes('仅为部分结果'));assert.ok(html.includes('未测量'));assert.ok(html.includes('实际释放 0 B'));
});
