/* Material library: an indexed view, not a second directory tree. */
(function(root,factory){
  const value=factory();
  if(typeof module==='object' && module.exports)module.exports=value;
  else root.AIHubMedia=value;
})(typeof window!=='undefined'?window:globalThis,function(){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={all:'全部素材',image:'图片',video:'视频',audio:'音频'};
  const initial=()=>({type:'all',page:1,size:36,q:'',model:'',dir:'',sort:'newest',density:'comfortable'});
  const state=initial();
  function tabs(selected){
    return `<nav class="resource-tabs" aria-label="素材管理视图"><a href="#/assets" ${selected==='assets'?'aria-current="page"':''}>素材库</a><a href="#/images" ${selected==='images'?'aria-current="page"':''}>图片生成记录</a></nav>`;
  }
  function capture(){return {...state};}
  function card(item,index,{icon,fmtSize,fmtDate,thumbURL}){
    const mediaIcon=item.category==='video'?'video':item.category==='audio'?'audio':'image';
    const image=item.category==='image'&&item.available;
    const preview=image?`<img src="${thumbURL(item.path)}" alt="" loading="lazy">`:`<span class="media-placeholder">${icon(mediaIcon,38)}<span>${item.available?labels[item.category]+'预览':'文件已变化或不可访问'}</span></span>`;
    return `<article class="gitem media-card" data-context-path="${esc(item.path)}" tabindex="0"><button class="g-preview" data-media="${index}" aria-label="查看 ${esc(item.name)}">${preview}<span class="preview-hint">${icon(item.category==='image'?'expand':mediaIcon,15)} 查看${labels[item.category]||'素材'}</span></button><div class="gitem-body"><div class="gitem-heading"><button class="gallery-filename" data-media="${index}" title="${esc(item.name)}">${esc(item.name)}</button>${item.deletable?`<button class="gallery-delete" data-media-delete="${index}" aria-label="将 ${esc(item.name)} 移到回收站" title="移到回收站">${icon('trash',15)}</button>`:''}</div><div class="gitem-meta"><span>${labels[item.category]||'素材'}${item.output_image?' · 生成记录':''}</span><span>${fmtDate(item.mtime)} · ${fmtSize(item.size)}</span></div><div class="media-directory" title="${esc(item.parent)}">${esc(item.parent)}</div></div></article>`;
  }
  function createPage(env){
    const {api,heading,icon,fmtSize,fmtDate,thumbURL,pager,empty,openMedia,requestDelete}=env;
    return function page(el,params=new URLSearchParams(),restored){
      if(restored)Object.assign(state,initial(),restored);
      else if(params.size){Object.assign(state,initial());for(const key of Object.keys(state))if(params.has(key))state[key]=['page','size'].includes(key)?Math.max(1,Number(params.get(key))||1):params.get(key);}
      if(!Object.hasOwn(labels,state.type))state.type='all';
      const $=selector=>el.querySelector(selector),$$=selector=>[...el.querySelectorAll(selector)];
      el.classList?.add?.('material-page');
      el.innerHTML=heading('素材管理','按媒体类型查找参考素材与生成成果，文件保留在登记的来源中。','MATERIAL LIBRARY')+tabs('assets')+`<div class="media-types" role="group" aria-label="媒体类型">${Object.entries(labels).map(([key,label])=>`<button class="btn" data-media-type="${key}" aria-pressed="${state.type===key}">${icon(key==='video'?'video':key==='audio'?'audio':key==='image'?'image':'grid',16)} ${label}<span data-media-count="${key}">—</span></button>`).join('')}</div><div class="filterbar gallery-filters"><input id="ma-q" type="search" aria-label="搜索素材名称或图片提示词" placeholder="名称或图片提示词…" value="${esc(state.q)}"><input id="ma-model" aria-label="按图片引用模型筛选" placeholder="图片引用的模型（可选）" value="${esc(state.model)}"><select id="ma-dir" aria-label="素材目录"><option value="">所有素材目录</option></select><select id="ma-sort" aria-label="素材排序"><option value="newest">最新在前</option><option value="oldest">最早在前</option></select><button class="btn ghost" id="ma-clear">重置筛选</button></div><div class="gallery-toolbar"><b id="media-total" aria-live="polite">读取素材索引…</b><div class="gallery-density" role="group" aria-label="预览大小"><button data-media-density="comfortable" aria-label="大图预览">${icon('overview',16)}</button><button data-media-density="compact" aria-label="紧凑预览">${icon('grid',16)}</button></div></div><div class="gallery" id="media-grid"></div><div id="media-empty"></div><div id="media-pager"></div><details class="media-coverage"><summary>收录范围与来源设置</summary><p>素材库读取资产扫描目录中的图片、视频与音频，并补充图片生成目录的记录。视频和音频目录需要登记为资产扫描来源。</p><div id="media-sources"></div><p>数量来自最近一次索引；文件发生变化时，请刷新索引。浏览器无法解码的媒体可从所在文件夹打开。</p><a class="btn small" href="#/workspace">管理素材来源</a></details>`;
      let sequence=0,timer,dirsType=null;
      function controls(){
        const modelOnly=state.type==='video'||state.type==='audio';
        $('#ma-model').disabled=modelOnly;
        $$('[data-media-type]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.mediaType===state.type)));
        $('#media-grid').classList.toggle('compact',state.density==='compact');
        $$('[data-media-density]').forEach(b=>b.classList.toggle('active',b.dataset.mediaDensity===state.density));
      }
      async function load(){
        const request=++sequence;
        controls();
        const query=new URLSearchParams();for(const [key,value]of Object.entries(state))if(value!==''&&key!=='density')query.set(key,value);
        try{
          const data=await api('/api/media?'+query);
          if(request!==sequence||!el.isConnected)return;
          const last=Math.max(1,Math.ceil(data.total/data.size));if(state.page>last){state.page=last;return load();}
          $('#media-total').textContent=data.total.toLocaleString()+' 条素材索引 · '+labels[state.type];
          for(const key of Object.keys(labels))$(`[data-media-count="${key}"]`).textContent=key==='all'?Object.values(data.counts).reduce((a,b)=>a+b,0).toLocaleString():(data.counts[key]||0).toLocaleString();
          if(dirsType!==state.type){const dirs=data.dirs.includes(state.dir)||!state.dir?data.dirs:[state.dir,...data.dirs];$('#ma-dir').innerHTML='<option value="">所有素材目录</option>'+dirs.map(p=>`<option value="${esc(p)}">${esc(p)}</option>`).join('');$('#ma-dir').value=state.dir;dirsType=state.type;}
          $('#media-grid').innerHTML=data.items.map((item,i)=>card(item,i,env)).join('');
          $('#media-empty').innerHTML=data.items.length?'':empty('没有匹配的素材','调整筛选，或在工作区设置中登记素材目录后刷新索引。');
          $('#media-pager').replaceChildren(pager(data.total,data.page,data.size,p=>{state.page=p;load();}));
          const coverage=data.coverage||{};
          $('#media-sources').innerHTML=`<p>资产扫描：${(coverage.files_roots||[]).map(esc).join('；')||'尚未登记'}</p><p>图片生成记录：${(coverage.generated_image_roots||[]).map(esc).join('；')||'尚未登记'}</p>`;
          $$('[data-media]').forEach(b=>b.onclick=()=>openMedia(data.items[Number(b.dataset.media)],()=>{if(el.isConnected){dirsType=null;load();}}));
          $$('[data-media-delete]').forEach(b=>b.onclick=()=>requestDelete(data.items[Number(b.dataset.mediaDelete)],()=>{if(el.isConnected){dirsType=null;load();}}));
        }catch(error){if(request===sequence&&el.isConnected){$('#media-empty').innerHTML=`<div class="page-error" role="alert"><p>素材读取失败：${esc(error.message)}</p><button class="btn" id="media-retry">重试</button></div>`;$('#media-retry').onclick=load;}}
      }
      $$('[data-media-type]').forEach(b=>b.onclick=()=>{state.type=b.dataset.mediaType;state.page=1;if(state.type==='video'||state.type==='audio'){state.model='';$('#ma-model').value='';}load();});
      for(const [id,key]of [['ma-q','q'],['ma-model','model']])$('#'+id).oninput=e=>{state[key]=e.target.value.trim();state.page=1;sequence++;clearTimeout(timer);timer=setTimeout(()=>{if(el.isConnected)load();},300);};
      for(const key of ['dir','sort'])$('#ma-'+key).onchange=e=>{state[key]=e.target.value;state.page=1;load();};
      $('#ma-sort').value=state.sort;
      $('#ma-clear').onclick=()=>{Object.assign(state,{q:'',model:'',dir:'',sort:'newest',page:1});for(const key of ['q','model','dir'])$('#ma-'+key).value='';$('#ma-sort').value='newest';load();};
      $$('[data-media-density]').forEach(b=>b.onclick=()=>{state.density=b.dataset.mediaDensity;controls();});
      return load();
    };
  }
  return {tabs,capture,createPage,card};
});
