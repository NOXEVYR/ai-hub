'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const media=require('../frontend/media.js');
function harness(api){
  const nodes=new Map(),node=selector=>{
    if(!nodes.has(selector))nodes.set(selector,{value:'',innerHTML:'',textContent:'',classList:{toggle(){}},replaceChildren(){},setAttribute(){}});
    return nodes.get(selector);
  };
  const buttons=Object.keys({all:0,image:0,video:0,audio:0}).map(type=>({dataset:{mediaType:type},setAttribute(){}}));
  const el={isConnected:true,querySelector:node,querySelectorAll:selector=>selector==='[data-media-type]'?buttons:[]};
  const page=media.createPage({api,heading:()=>'',icon:()=>'',fmtSize:()=>'',fmtDate:()=>'',thumbURL:()=>'',pager:()=>({}),empty:()=>'<p>empty</p>'});
  return {page,el,node,buttons};
}
const result=()=>({items:[],total:80,page:2,size:36,counts:{image:40,video:30,audio:10},dirs:['D:/Sources'],coverage:{}});

test('restored material type/filter/page wins over deep-link and preserves current directory selection',async()=>{
  const requests=[],h=harness(async url=>{requests.push(url);return result();});
  await h.page(h.el,new URLSearchParams('type=image'),{type:'video',page:2,q:'saved',model:'',dir:'D:/Old',density:'compact',sort:'oldest'});
  const query=new URLSearchParams(requests[0].split('?')[1]);
  assert.equal(query.get('type'),'video');assert.equal(query.get('q'),'saved');assert.equal(query.get('page'),'2');assert.equal(query.get('dir'),'D:/Old');
  assert.equal(h.node('#ma-dir').value,'D:/Old');assert.match(h.node('#ma-dir').innerHTML,/D:\/Old/);
  h.node('#ma-q').oninput({target:{value:'just typed'}});
  assert.equal(media.capture().q,'just typed');h.el.isConnected=false;
});

test('new explicit media links reset unrelated filters and audio tab clears model-only filter',async()=>{
  const requests=[],h=harness(async url=>{requests.push(url);return result();});
  await h.page(h.el,new URLSearchParams('type=image&model=style&q=one'));
  h.buttons.find(b=>b.dataset.mediaType==='audio').onclick();
  await Promise.resolve();
  const switched=new URLSearchParams(requests.at(-1).split('?')[1]);
  assert.equal(switched.get('type'),'audio');assert.equal(switched.has('model'),false);
  await h.page(h.el,new URLSearchParams('type=video'));
  const linked=new URLSearchParams(requests.at(-1).split('?')[1]);
  assert.equal(linked.has('q'),false);assert.equal(linked.get('type'),'video');
});

test('late media requests cannot replace newer type results or detached pages',async()=>{
  const resolves=[],h=harness(()=>new Promise(resolve=>resolves.push(resolve)));
  const first=h.page(h.el,new URLSearchParams('type=image'));
  h.buttons.find(b=>b.dataset.mediaType==='video').onclick();
  resolves[1]({...result(),total:1,page:1,counts:{image:0,video:1,audio:0}});await Promise.resolve();await Promise.resolve();
  const current=h.node('#media-total').textContent;
  resolves[0](result());await first;assert.equal(h.node('#media-total').textContent,current);
  const pending=h.page(h.el,new URLSearchParams('type=audio'));
  h.el.isConnected=false;resolves[2](result());await pending;
  assert.equal(h.node('#media-total').textContent,current);
});

test('media cards escape paths and expose recycling only for approved generated images',()=>{
  const env={icon:()=>'',fmtSize:()=>'',fmtDate:()=>'',thumbURL:p=>'/thumb?'+encodeURIComponent(p)};
  const item={path:'D:/<bad>.mp4',name:'<img onerror=bad>',parent:'<b>',category:'video',available:true,deletable:false};
  const html=media.card(item,0,env);
  assert(!html.includes('<img onerror'));assert(html.includes('&lt;img'));assert(!html.includes('data-media-delete'));
  assert(!html.includes('<video')); // Preview only on explicit open; never autoplays a grid.
  assert.match(media.card({...item,category:'image',deletable:true},0,env),/data-media-delete/);
});
