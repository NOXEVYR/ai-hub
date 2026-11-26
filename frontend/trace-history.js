/* Native-source histories are viewed locally. Sharing applies to a selected revision. */
((root,factory)=>{
  if(typeof module==='object' && module.exports) module.exports=factory();
  else root.AIHubTraceHistory=factory();
})(globalThis,()=>{
  'use strict';
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const toolNames={codex:'Codex',antigravity:'Antigravity',dsh:'DSH',zcode:'ZCode','claude-code':'Claude Code',exchange:'通用清单'};
  const relations={created:'明确创建',edited:'明确编辑',written:'确认写入',mentioned:'对话提及',workspace_related:'工作目录关联',artifact:'会话产物'};
  const showDate=value=>value?new Date(value*1000).toLocaleString():'';
  function toolLink(tool,name) {
    const hash='#/traces?'+new URLSearchParams({tool});
    return '<a class="btn small" href="'+esc(hash)+'">'+esc(name)+' <span class="wc-trace-count" data-trace-count="'+esc(tool)+'"></span></a>';
  }
  async function showToolNavigation(host,api) {
    host.innerHTML='<nav class="wc-trace-nav" aria-label="按工具找对话"><div class="wc-trace-heading"><h2>按工具找对话</h2><a href="#/traces">全部对话</a></div>'+
      '<div class="wc-trace-links">'+Object.entries(toolNames).map(([tool,name])=>toolLink(tool,name)).join('')+'</div></nav>';
    let status;
    try {status=await api('/api/traces/status');}
    catch {return;} // The links remain usable when counts are unavailable.
    if(!host.isConnected||!Array.isArray(status?.sources))return;
    const sources=new Map(status.sources.map(source=>[source.tool,source]));
    const extra=status.sources.filter(source=>!Object.hasOwn(toolNames,source.tool));
    if(extra.length)host.querySelector('.wc-trace-links').insertAdjacentHTML('beforeend',extra.map(source=>toolLink(source.tool,source.tool)).join(''));
    for(const tag of host.querySelectorAll('[data-trace-count]')){
      const source=sources.get(tag.dataset.traceCount);
      if(source)tag.textContent=(source.sessions||0).toLocaleString('zh-CN')+' 条';
    }
  }
  const button=(action,label)=>'<button class="btn small" type="button" data-trace-action="'+action+'">'+label+'</button>';
  function highlight(value,term){
    if(!term) return esc(value);
    const escaped=term.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    return String(value).split(new RegExp('('+escaped+')','gi')).map((s,i)=>i%2?'<mark>'+esc(s)+'</mark>':esc(s)).join('');
  }
  function detailHTML(d,term='',fileQuery=''){
    const matches=text=>term&&text.toLocaleLowerCase().includes(term.toLocaleLowerCase());
    const messages=d.messages.map(m=>'<details class="trace-message"'+(matches(m.text)?' open':'')+'><summary>'+esc(m.role==='user'?'用户':'助手')+' · '+highlight(m.text.slice(0,90),term)+'</summary><pre class="wc-text">'+highlight(m.text,term)+'</pre></details>').join('');
    const files=d.files.map((f,i)=>'<div class="trace-file"><strong>'+esc(relations[f.relation]||f.relation)+'</strong> <span>'+esc(f.evidence)+'</span><small>'+esc(f.path)+'</small>'+
        '<button type="button" class="btn small ghost" data-file-search="'+i+'">反查会话</button> <button type="button" class="btn small ghost" data-file-review="'+i+'">清理复核</button>'+
        (f.review?'<span> · '+(f.review==='keep'?'已保留':'待复核')+'</span>':'')+'</div>').join('');
    return '<div class="panel-head"><h2>'+esc(d.title)+'</h2>'+button('close','关闭')+'</div><div class="body"><p>'+esc(toolNames[d.tool]||d.tool)+' · '+esc(d.cwd||'未记录工作目录')+'</p>'+
      (d.ready?button('share',d.shared?'取消共享给 AI':'共享本次摘录给 AI'):'<p>尚未完成内容索引。</p>')+
      '<details class="trace-files"'+(d.files.length<9||fileQuery?' open':'')+'><summary>关联文件（'+d.files.length+'）</summary>'+(files||'<p>当前摘录未发现文件证据。</p>')+'</details><div id="trace-review" role="status"></div>'+
      '<h3>对话摘录（'+d.messages.length+'）</h3>'+(d.state==='first_user_excerpt'?'<p>此摘录来自 Codex 保存的首条用户发言。</p>':d.partial?'<p>显示已索引的部分记录。</p>':'')+
      (messages||'<p>此来源当前没有可展示的用户或助手消息。</p>')+'</div>';
  }
  function createPage({api,nav}){
    let generation=0;
    return async(el,params=new URLSearchParams())=>{
      const generationId=++generation, active=()=>el.isConnected&&generationId===generation;
      const get=id=>el.querySelector('#trace-'+id);
      let root='', listSerial=0, detailSerial=0, selected=null, timer=null;
      const state={tool:params.get('tool')||'',q:params.get('q')||'',file:params.get('file')||params.get('file_exact')||'',file_exact:params.get('file_exact')||'',review:params.get('review')||'',page:Math.max(1,Number(params.get('page'))||1)};
      const go=changes=>nav('traces',{...state,...changes});
      const post=(action,fields)=>api('/api/traces/'+action,{body:{...fields,_workspace_root:root}});
      const fail=err=>{if(active())get('error').textContent=err.message;};
      el.classList?.add('workcenter-page');
      el.innerHTML='<div class="panel"><div class="body"><form id="trace-form" class="trace-form"><label>工具<select id="trace-tool"></select></label><label>标题、工作目录或对话摘录<input type="search" id="trace-q"></label><label>文件名或路径<input type="search" id="trace-file"></label><label>清理复核<select id="trace-filter"><option value="">全部</option><option value="pending">待复核</option></select></label><button type="submit" class="btn primary">查询</button></form>'+
        '<div class="wc-actions"><button class="btn small" id="trace-sync">同步所选来源</button><button class="btn small ghost" id="trace-cancel" hidden>暂停同步</button></div><p id="trace-progress" role="status"></p><details><summary>来源与索引范围</summary><div id="trace-sources"></div><p>通用清单目录为工作区内的 00_Management/AIHub/TraceExports。检索范围为已收录的标题、工作目录、消息摘录和文件证据。</p></details></div></div>'+
        '<p id="trace-error" role="alert"></p><div class="panel" id="trace-detail" hidden></div><section class="panel"><div class="body"><p id="trace-count"></p><div id="trace-results"></div><div class="wc-pager"><button class="btn small" id="trace-prev">上一页</button> <button class="btn small" id="trace-next">下一页</button></div></div></section>';
      get('q').value=state.q;get('file').value=state.file;get('filter').value=state.review;
      get('form').onsubmit=e=>{e.preventDefault();go({tool:get('tool').value,q:get('q').value,file:get('file').value,file_exact:get('file').value===state.file?state.file_exact:'',review:get('filter').value,page:1});};
      get('prev').onclick=()=>go({page:state.page-1});get('next').onclick=()=>go({page:state.page+1});
      async function load(){
        const seq=++listSerial;
        const result=await api('/api/traces/sessions?'+new URLSearchParams({...state,_workspace_root:root}));
        if(!active()||listSerial!==seq)return;
        get('count').textContent=result.total+' 条会话 · 第 '+state.page+' 页';
        get('results').innerHTML=result.items.map(i=>'<div class="trace-row"><button class="btn small ghost" data-session="'+esc(i.key)+'">'+esc(i.title)+'</button><span>'+esc(toolNames[i.tool]||i.label)+' · '+esc(showDate(i.updated))+(i.ready?(i.partial?' · 部分摘录':''):i.state==='source_unavailable'?' · 来源暂缺':i.state==='unreadable'?' · 暂不可读':' · 等待索引')+'</span></div>').join('')||'<p>没有匹配记录。首次使用可选择工具后同步。</p>';
        get('prev').disabled=state.page<=1;get('next').disabled=state.page*30>=result.total;
        get('results').querySelectorAll('[data-session]').forEach(b=>b.onclick=()=>open(b.dataset.session).catch(fail));
      }
      async function open(key){
        const seq=++detailSerial;
        const d=await api('/api/traces/session?'+new URLSearchParams({key,_workspace_root:root}));
        if(!active()||seq!==detailSerial)return;
        selected=d;const panel=get('detail');panel.hidden=false;panel.innerHTML=detailHTML(d,state.q,state.file);
        panel.querySelector('[data-trace-action=close]').onclick=()=>{++detailSerial;selected=null;panel.hidden=true;};
        const sharing=panel.querySelector('[data-trace-action=share]');
        if(sharing)sharing.onclick=async()=>{sharing.disabled=true;try{await post('share',{key,revision:d.revision,enabled:!d.shared});if(active()&&seq===detailSerial)await open(key);}catch(e){fail(e);}finally{sharing.disabled=false;}};
        panel.querySelectorAll('[data-file-search]').forEach(b=>b.onclick=()=>go({file:d.files[Number(b.dataset.fileSearch)].path,file_exact:d.files[Number(b.dataset.fileSearch)].path,q:'',tool:'',review:'',page:1}));
        let reviewSeq=0;
        panel.querySelectorAll('[data-file-review]').forEach(b=>b.onclick=async()=>{
          const stamp=++reviewSeq, file=d.files[Number(b.dataset.fileReview)], valid=()=>active()&&selected===d&&stamp===reviewSeq;
          try{
            const p=await post('preview',{key,path:file.path});if(!valid())return;
            const area=get('review');area.innerHTML='<p><strong>'+esc(p.path)+'</strong></p><p>'+esc(p.reason)+' 已索引引用会话数 '+esc(p.references)+'</p>'+
              button('pending','标记待复核')+' '+button('keep','保留文件')+
              (p.lifecycle_scope?' <a class="btn small ghost" href="#/lifecycle?scope='+esc(encodeURIComponent(p.lifecycle_scope))+'">在文件台账检查目录</a>':'')+
              (p.eligible?' '+button('recycle','确认移入 Windows 回收站'):'');
            for(const s of ['pending','keep'])area.querySelector('[data-trace-action='+s+']').onclick=async()=>{try{await post('review',{key,path:file.path,state:s});if(valid())await open(key);}catch(e){fail(e);}};
            area.scrollIntoView?.({block:'nearest'});
            const recycle=area.querySelector('[data-trace-action=recycle]');
            if(recycle)recycle.onclick=async()=>{recycle.disabled=true;try{const outcome=await post('recycle',{token:p.token});if(valid())area.textContent=outcome.status==='recycled'?'已移入 Windows 回收站。':outcome.reason||'文件状态已变化，请重新预览。';}catch(e){fail(e);}};
          }catch(e){fail(e);}
        });
        panel.scrollIntoView?.({block:'start'});
      }
      async function progress(){
        clearTimeout(timer);
        const s=await api('/api/traces/status');if(!active())return;
        if(root&&root!==s.workspace_root){go({q:'',file:'',page:1});return;}
        root=s.workspace_root;
        if(!get('tool').options.length){get('tool').innerHTML='<option value="">全部工具</option>'+s.sources.map(t=>'<option value="'+esc(t.tool)+'">'+esc(toolNames[t.tool]||t.tool)+'</option>').join('');get('tool').value=state.tool;}
        get('sources').innerHTML=s.sources.map(t=>'<p>'+esc(toolNames[t.tool]||t.tool)+' · '+(t.available?'来源可用':'本机未发现来源')+' · '+(t.indexed||0)+' / '+(t.sessions||0)+' 条已索引'+(t.scan?.error?' · '+esc(t.scan.error):'')+'</p>').join('');
        const j=s.job;
        get('sync').disabled=!!j.running;get('cancel').hidden=!j.running;
        get('progress').textContent=j.running?'正在同步 '+(toolNames[j.tool]||j.tool)+' · 已发现 '+j.seen+' 条，更新 '+j.indexed+' 条':j.seen!=null?(j.cancelled?'已暂停':'同步结束')+' · 已发现 '+j.seen+' 条，更新 '+j.indexed+' 条，读取异常 '+j.errors+' 条':'';
        await load();
        if(active()&&j.running)timer=setTimeout(()=>{if(active())progress().catch(fail);},2500);
      }
      get('sync').onclick=async()=>{get('sync').disabled=true;try{await post('sync',{tool:get('tool').value||'all'});if(active())await progress();}catch(e){fail(e);get('sync').disabled=false;}};
      get('cancel').onclick=async()=>{try{await post('cancel',{});if(active())await progress();}catch(e){fail(e);}};
      try{await progress();}catch(e){fail(e);}
    };
  }
  return {createPage,detailHTML,showToolNavigation};
});
