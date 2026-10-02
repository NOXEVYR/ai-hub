'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm');
const {readFileSync}=require('node:fs'),path=require('node:path');
const source=readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8');
test('model mutations bind the rendered workspace before any asynchronous response',async()=>{
  const calls=[],context={fetch:async(url,options)=>{calls.push({url,options});return{ok:true,headers:{get:()=> 'application/json'},json:async()=>({ok:true})};}};
  vm.runInNewContext(source.slice(source.indexOf('  let activeWorkspaceRoot'),source.indexOf('  const fileURL'))+'\nthis.call=api;this.root=value=>activeWorkspaceRoot=value;',context);
  context.root('D:/Workspace-A');
  for(const url of ['/api/model/42/edit','/api/model/42/source','/api/model/42/check','/api/model/42/resolve-source','/api/models/classify','/api/models/check-updates'])await context.call(url,{body:{notes:'draft'}});
  for(const item of calls)assert.equal(JSON.parse(item.options.body)._workspace_root,'D:/Workspace-A');
  await context.call('/api/model/42/edit',{body:{_workspace_root:'D:/Captured-A'}});assert.equal(JSON.parse(calls.at(-1).options.body)._workspace_root,'D:/Captured-A');
  await context.call('/api/model/42');assert.equal(calls.at(-1).options,undefined);
  await context.call('/api/scan/start',{body:{}});assert.deepEqual(JSON.parse(calls.at(-1).options.body),{});
});
