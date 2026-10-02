const test = require('node:test');
const assert = require('node:assert/strict');
const {tasksHTML} = require('../frontend/collaboration.js');

test('execution detail exposes stable results without opening locators or inventing integrity checks',()=>{
  const {executionDetailHTML}=require('../frontend/collaboration.js');
  const html=executionDetailHTML({id:'queue-1',execution:{provider_state:'succeeded',evidence_source:'worker_report',origin:{task_id:'task-original',run_id:'run-original'},executor:{client_id:'publisher-original'},results:[
    {result_id:'成果_01',kind:'image',media_type:'image/png',bytes:200,sha256:'a'.repeat(64),locator:'https://untrusted.example/'},
    {result_id:'<img src=x onerror=bad>',kind:'audio',media_type:'audio/wav',bytes:300,locator:'file:///private'}]}});
  assert.match(html,/task-original/);assert.match(html,/publisher-original/);assert.match(html,/成果_01/);
  assert.match(html,/文件待核验/);assert.match(html,/未提供文件摘要 · 未校验/);assert.match(html,/曜核未独立核验/);
  assert.doesNotMatch(html,/<img|https:\/\/untrusted|file:\/\/|<a\b/);
  assert.match(html,/&lt;img/);assert.equal(executionDetailHTML({id:'legacy'}),'');
});

test('execution detail retains uncertain cancellation and safe failure descriptions',()=>{
  const {executionDetailHTML}=require('../frontend/collaboration.js');
  assert.match(executionDetailHTML({execution:{provider_state:'uncertain',cancel_requested:true}}),/尚未确认停止/);
  const html=executionDetailHTML({execution:{provider_state:'failed',outcome:{error_code:'<script>private</script>'}}});
  assert.match(html,/失败分类/);assert.doesNotMatch(html,/<script>/);
});

test('native execution access bridge carries no identity, path or credential payload',()=>{
  const {openExecutionAccess}=require('../frontend/collaboration.js');
  const messages=[];
  assert.equal(openExecutionAccess({chrome:{webview:{postMessage:value=>messages.push(value)}}}),true);
  assert.deepEqual(messages,['open-execution-access']);
  assert.equal(openExecutionAccess({}),false);
  assert.equal(openExecutionAccess({chrome:{webview:{postMessage(){throw new Error('private-response');}}}}),false);
});

const task = execution => ({id:'queue-fixed',title:'三端任务',project:'测试项目',target_tool:'codex',owner:null,
  status:'queued',report_submission:{policy:'required',status:'pending'},execution});
const execution = patch => ({dispatch_state:'queued_ready',provider_state:'not_started',
  executor:{client_id:'declared-publisher',tool:'codex'},evidence_source:'queue_ledger',
  origin:{authority_id:'映序来源',task_id:'original-task',run_id:'original-run'},...patch});

test('execution task shows original scope and exact publisher separately from queue status',()=>{
  const html=tasksHTML([task(execution())]);
  assert.match(html,/declared-publisher/); assert.match(html,/original-task/); assert.match(html,/original-run/);
  assert.match(html,/来源 映序来源/); assert.match(html,/等待领取/); assert.match(html,/尚未提交原生任务/);
  assert.match(html,/队列记录 · 未调用原生服务/); assert.doesNotMatch(html,/data-co-action="requeue"/);
  assert.doesNotMatch(html,/data-co-action="record-delete"/);
});
test('cancel request does not display confirmed stop while native worker is running',()=>{
  const t={...task(execution({dispatch_state:'claimed',provider_state:'running',cancel_requested:true,evidence_source:'worker_report'})),status:'active',owner:'declared-publisher'};
  const html=tasksHTML([t]); assert.match(html,/已请求取消 · 尚未确认停止/);
  assert.match(html,/执行客户端上报 · 待独立验收/); assert.match(html,/原生任务运行中/);
  assert.doesNotMatch(html,/data-co-action="requeue"/);
});
test('terminal ledger remains removable without exposing a requeue operation',()=>{
  const html=tasksHTML([{...task(execution({dispatch_state:'completed',provider_state:'succeeded',evidence_source:'worker_report'})),status:'completed'}]);
  assert.match(html,/原生执行成功/); assert.match(html,/data-co-action="record-delete"/);
  assert.doesNotMatch(html,/data-co-action="requeue"/);
});
test('ordinary legacy tasks retain release and delete controls',()=>{
  const html=tasksHTML([{...task(undefined),status:'active',owner:'legacy-worker'}]);
  assert.match(html,/data-co-action="requeue"/); assert.match(html,/data-co-action="record-delete"/);
});
test('origin and publisher labels escape supplied data',()=>{
  const e=execution({executor:{client_id:'<script>bad</script>'},origin:{authority_id:'<img src=x>',task_id:'<b>bad</b>',run_id:'run'}});
  const html=tasksHTML([task(e)]); assert.doesNotMatch(html,/<script>/); assert.doesNotMatch(html,/<img src=x>/);
  assert.match(html,/&lt;script&gt;/); assert.match(html,/&lt;img src=x&gt;/);
});
