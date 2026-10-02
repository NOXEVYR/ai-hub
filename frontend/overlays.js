/* Shared focus and dismissal lifecycle for the workbench's stacked dialogs. */
(function(root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.AIHubOverlays = factory();
})(globalThis, function() {
  'use strict';
  function create({document:doc, background}) {
    const stack=[];
    const visible=node=>node && !node.disabled && node.getClientRects().length>0;
    const controls=node=>[...node.querySelectorAll('input:not([type="hidden"]),select,textarea,button,a[href],[tabindex]:not([tabindex="-1"])')].filter(visible);
    const top=()=>stack.at(-1);
    const focus=entry=>{
      if(!entry)return;
      const fields=controls(entry.node);
      const close=fields.find(n=>n.classList?.contains('close') || n.id?.endsWith('-close'));
      (entry.id==='modal' ? fields.find(n=>/^(INPUT|SELECT|TEXTAREA)$/.test(n.tagName)) || close || fields[0] || entry.node : close || fields[0] || entry.node).focus();
    };
    const sync=()=>{
      if(background)background.inert=stack.length>0;
      stack.forEach(entry=>{entry.node.inert=entry!==top();});
    };
    function show(id,node,close) {
      let entry=stack.find(e=>e.id===id);
      if(entry)stack.splice(stack.indexOf(entry),1);
      else entry={id,node,close,previous:doc.activeElement};
      entry.node=node;entry.close=close;node.setAttribute('tabindex','-1');
      stack.push(entry);sync();
      if(doc.activeElement===node || !node.contains(doc.activeElement))focus(entry);
    }
    function hide(id) {
      const index=stack.findIndex(e=>e.id===id);
      if(index<0)return;
      const entry=stack[index],wasTop=entry===top();
      stack.splice(index,1);entry.node.inert=false;sync();
      if(!wasTop)return;
      const target=entry.previous;
      if(target?.isConnected && !target.inert && (!top() || top().node.contains(target)))target.focus();
      else focus(top());
    }
    doc.addEventListener('keydown',event=>{
      const entry=top();if(!entry || event.defaultPrevented)return;
      if(event.key==='Escape'){event.preventDefault();event.stopPropagation();entry.close();return;}
      if(event.key!=='Tab')return;
      const fields=controls(entry.node),index=fields.indexOf(doc.activeElement);
      if(!fields.length){event.preventDefault();entry.node.focus();return;}
      if(index<0 || (event.shiftKey && index===0) || (!event.shiftKey && index===fields.length-1)) {
        event.preventDefault();fields[event.shiftKey?fields.length-1:0].focus();
      }
    });
    doc.addEventListener('focusin',event=>{
      const entry=top();
      if(entry && !entry.node.contains(event.target) && !event.target.closest?.('[role="menu"]'))focus(entry);
    });
    return {show,hide};
  }
  return {create};
});
