'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm');
const {readFileSync}=require('node:fs'),path=require('node:path');
const source=readFileSync(path.join(__dirname,'../frontend/app.js'),'utf8');
function fixture(){
  const nodes=new Map(),node=key=>{if(!nodes.has(key))nodes.set(key,{value:'',innerHTML:''});return nodes.get(key);};
  let search,reply=async()=>({items:[]});
  const context={pages:{},fileState:{path:''},api:async url=>url.startsWith('/api/tree')?{path:'D:/AI',subdirs:[],files:[],crumb:[]}:reply(url),
    $:node,$$:()=>[],debounce:fn=>{search=fn;return fn;},heading:()=>'',esc:s=>String(s),fmtSize:s=>String(s),fmtDate:s=>String(s),nav(){},openLightbox(){}};
  vm.runInNewContext(source.slice(source.indexOf('  pages.files ='),source.indexOf('  pages.reports=')),context);
  const el={isConnected:true,innerHTML:''};
  return {el,node,context,search:()=>search(),reply:fn=>reply=fn};
}
test('file search displays failure and retry replaces it with real results',async()=>{
  const h=fixture();await h.context.pages.files(h.el,new URLSearchParams());h.node('#fs-q').value='report';
  h.reply(async()=>{throw new Error('offline');});await h.search();assert.match(h.node('#fs-results').innerHTML,/搜索未完成：offline/);
  h.reply(async()=>({items:[{name:'report.md',size:10,path:'D:/AI/report.md'}]}));
  await h.node('#fs-search-retry').onclick();assert.match(h.node('#fs-results').innerHTML,/report.md/);assert.doesNotMatch(h.node('#fs-results').innerHTML,/offline/);
});
test('same-keyword older response cannot override the newest search',async()=>{
  const h=fixture();await h.context.pages.files(h.el,new URLSearchParams());h.node('#fs-q').value='ab';
  let finish;h.reply(()=>new Promise(resolve=>finish=resolve));const old=h.search();
  h.reply(async()=>({items:[{name:'new',size:1,path:'D:/AI/new'}]}));await h.search();
  finish({items:[{name:'old',size:1,path:'D:/AI/old'}]});await old;
  assert.match(h.node('#fs-results').innerHTML,/new/);assert.doesNotMatch(h.node('#fs-results').innerHTML,/>old</);
});
