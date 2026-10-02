'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const {create}=require('../frontend/overlays.js');
function fixture(){
  const handlers={},doc={activeElement:null,addEventListener:(key,fn)=>handlers[key]=fn};
  const node=(name,tagName='BUTTON')=>({name,tagName,isConnected:true,inert:false,disabled:false,
    getClientRects:()=>[{}],setAttribute(){},focus(){doc.activeElement=this;},closest:()=>null});
  const dialog=(name,children=[])=>Object.assign(node(name,'ASIDE'),{querySelectorAll:()=>children,contains:target=>target===dialogs[name] || children.includes(target)});
  const background=node('background'),trigger=node('trigger'),dialogs={};trigger.focus();
  const stack=create({document:doc,background});
  const make=(name,children)=>dialogs[name]=dialog(name,children);
  const key=(key,shiftKey=false)=>{const e={key,shiftKey,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},stopPropagation(){}};handlers.keydown(e);return e;};
  return {doc,node,make,stack,key,trigger,background};
}
test('dialogs focus editable content and wrap Tab in the active layer',()=>{
  const h=fixture(),close=h.node('close'),field=h.node('name','INPUT'),save=h.node('save'),modal=h.make('modal',[close,field,save]);
  h.stack.show('modal',modal,()=>{});assert.equal(h.doc.activeElement,field);assert.equal(h.background.inert,true);
  save.focus();assert.equal(h.key('Tab').defaultPrevented,true);assert.equal(h.doc.activeElement,close);
  h.key('Tab',true);assert.equal(h.doc.activeElement,save);
});
test('Escape closes one layer and restores focus through all three layers',()=>{
  const h=fixture(),drawerButton=h.node('drawer-button'),lightButton=h.node('light-button'),modalButton=h.node('modal-button');
  for(const [id,button] of [['drawer',drawerButton],['lightbox',lightButton],['modal',modalButton]])h.stack.show(id,h.make(id,[button]),()=>h.stack.hide(id));
  assert.equal(h.background.inert,true);assert.equal(h.doc.activeElement,modalButton);
  h.key('Escape');assert.equal(h.doc.activeElement,lightButton);assert.equal(h.background.inert,true);
  h.key('Escape');assert.equal(h.doc.activeElement,drawerButton);
  h.key('Escape');assert.equal(h.doc.activeElement,h.trigger);assert.equal(h.background.inert,false);
});
test('replacing a dialog preserves its original return target',()=>{
  const h=fixture(),a=h.node('a'),b=h.node('b'),modal=h.make('modal',[a]);
  h.stack.show('modal',modal,()=>h.stack.hide('modal'));
  modal.querySelectorAll=()=>[b];modal.contains=target=>target===modal || target===b;a.isConnected=false;
  h.stack.show('modal',modal,()=>h.stack.hide('modal'));assert.equal(h.doc.activeElement,b);
  h.key('Escape');assert.equal(h.doc.activeElement,h.trigger);
});
test('empty dialog remains focusable and Tab does not dismiss it',()=>{
  const h=fixture(),modal=h.make('modal');let closed=0;
  h.stack.show('modal',modal,()=>closed++);assert.equal(h.doc.activeElement,modal);h.key('Tab');
  assert.equal(h.doc.activeElement,modal);assert.equal(closed,0);
});
