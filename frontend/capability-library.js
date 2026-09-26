(function(root, factory) {
  const value = factory();
  if (typeof module === 'object' && module.exports) module.exports = value;
  else root.AIHubCapabilityLibrary = value;
})(globalThis, function() {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const rows = v => Array.isArray(v) ? v : [];
  const domains = {image:'图像生成',video:'视频生成',audio:'语音与声音',code:'软件开发',document:'文档处理',research:'资料研究',automation:'任务自动化'};
  const toolNames={codex:'Codex',dsh:'DSH',zcode:'ZCode',workbuddy:'WorkBuddy',shared:'共享 Skill 目录'};
  const sourceNames={local_frontmatter:'Skill 声明',explicit_local_capability_manifest:'公开接口声明',registered_capability_catalog:'工作端能力声明',plugin_metadata:'插件公开信息',codex_plugin_metadata:'插件公开信息'};
  const sourceStatus={truncated_budget:'达到发现上限',scanned:'已检查',scanned_readonly:'已读取现有登记',missing:'目录不存在',skipped:'已跳过',unavailable:'暂不可用',unavailable_no_workspace:'未关联工作区',unavailable_no_catalog:'暂无登记库',invalid_or_unreadable:'格式无效或不可读'};
  const tabs = {skills:'Skill 库',interfaces:'接口与服务',credentials:'环境与凭据',tasks:'任务调度'};
  function toolsOf(item) { return [...new Set([...rows(item.tools),item.tool,item.target_tool].filter(Boolean))]; }
  function filter(items, query, domain, tool='') {
    const q = String(query || '').trim().toLowerCase();
    return rows(items).filter(i => (!domain || (domain==='__unclassified' ? !rows(i.domains).length : rows(i.domains).includes(domain))) && (!tool || (tool==='__independent' ? !toolsOf(i).length : toolsOf(i).includes(tool))) && (!q || [i.name,i.plugin_name,i.description,i.variable,i.env_name,i.tool,i.provider,i.source,i.path,...toolsOf(i),...rows(i.domains)].join(' ').toLowerCase().includes(q)));
  }
  function inventoryHTML(items, tab) {
    if (!items.length) return '<div class="library-empty"><h3>当前筛选下没有内容</h3><p>试试其他用途或关键词。下方“发现来源”会列出本机检查过的目录；未配置服务也可以先从工作端接入。</p><a href="#/collaboration?tab=connect" class="btn small">管理工作端接入</a></div>';
    return items.map(i => `<article class="library-card" ${i.path ? `data-skill-path="${esc(i.path)}" tabindex="0"` : ''}><div class="library-card-top"><span class="badge">${tab==='skills'?'Skill':esc(i.provider||'接口线索')}</span><span class="library-status">${tab==='skills'?'本机已发现':i.declaration_status==='declared'?'已登记声明':'待接入'}</span></div><h3>${esc(i.name||i.plugin_name||i.id||'未命名接口')}</h3><p>${esc(i.description||'此来源尚未提供说明。')}</p><div class="library-tags">${rows(i.domains).map(d=>`<span>${esc(domains[d]||d)}</span>`).join('')}</div><div class="library-source">${esc(toolsOf(i).map(t=>toolNames[t]||t).join(' / ')||i.provider||'独立服务 / 未关联工作端')} · ${esc(sourceNames[i.source]||'本机来源')}</div>${i.path?`<div class="pathline library-path" title="${esc(i.path)}">${esc(i.path)}</div>`:''}<details><summary>来源与使用方式</summary><p>${esc(i.next_step||'在所属工作端启用并发布能力声明后，可在任务调度中选择。')}</p><p>发现记录不表示已连接或已执行；查看任务调度中的工作端与执行证据。</p></details></article>`).join('');
  }
  function credentialsHTML(items) {
    return `<div class="library-note">这里只显示变量名和是否配置。密钥留在系统环境或原工具中，不在网页显示、复制或保存；配置存在也不等于接口可用。</div>${items.length?`<div class="table-wrap"><table class="tbl"><thead><tr><th>服务 / 用途</th><th>环境变量名</th><th>当前状态</th><th>下一步</th></tr></thead><tbody>${items.map(i=>`<tr><td>${esc(i.provider||i.name||'服务配置')}</td><td><code>${esc(i.variable||i.env_name||i.name)}</code></td><td><span class="badge">${i.present===true?'已配置':'未检测到'}</span></td><td>${esc(i.next_step||'在对应工作端配置后连接，重启曜核再检查。')}</td></tr>`).join('')}</tbody></table></div>`:'<p class="library-empty">没有已知服务的凭据状态。接入自定义服务请在所属工作端配置环境变量，再发布接口声明。</p>'}<a href="#/collaboration?tab=connect" class="btn">管理工作端与 MCP 接入</a>`;
  }
  function capture(el, taskUI) {
    const child = el?.querySelector('#library-tasks');
    const sourceDraft = el?.dataset.librarySourceDirty==='true' ? [...(el.querySelector('#library-source-editor')?.querySelectorAll('[data-source-row]')||[])].map(row=>({tool:row.querySelector('[data-source-tool]').value,kind:row.querySelector('[data-source-kind]').value,path:row.querySelector('[data-source-path]').value})) : null;
    return {limit:Number(el?.dataset.libraryLimit)||48,sourceDraft,root:sourceDraft ? el?.dataset.librarySourceRoot||'' : el?.dataset.libraryRoot||'',tab:el?.dataset.libraryTab||'skills',query:el?.querySelector('#library-query')?.value||'',domain:el?.querySelector('#library-domain')?.value||'',tool:el?.querySelector('#library-tool')?.value||'',task:child?.dataset.loaded?taskUI.capture(child):null};
  }
  function createPage({api,heading,taskUI}) {
    const taskPage = taskUI.createPage({api});
    let generation=0;
    return async (el,params=new URLSearchParams(),restored) => {
      const run=++generation, active=()=>el.isConnected&&run===generation;
      let data=null, request=0, taskLoading=null, sourceDirty=false, sourceSaving=false, sourceRoot=restored?.root||'',limit=Math.min(512,Math.max(48,Number(restored?.limit)||48));
      let tab=tabs[restored?.tab||params.get('tab')]?restored?.tab||params.get('tab'):'skills';
      const get=id=>el.querySelector('#library-'+id);
      el.classList.add('capability-library');
      el.innerHTML=(heading?heading('能力中心','找 Skill、查生成接口，再把任务交给已接入的工作端。','AI CAPABILITIES'):'<h2>能力中心</h2>')+`<div class="library-toolbar"><p>本机发现 → 工作端接入 → 任务执行 → 项目归档</p><button id="library-refresh" class="btn">重新发现本机能力</button></div><nav class="library-tabs" aria-label="能力分类">${Object.entries(tabs).map(([id,name])=>`<button id="library-tab-${id}" type="button" data-library-tab="${id}">${name}</button>`).join('')}</nav><p id="library-error" role="alert"></p><section id="library-inventory"><div class="library-filter"><input id="library-query" type="search" placeholder="搜索名称、说明或所属工具" aria-label="搜索本机能力"><select id="library-domain" aria-label="按用途筛选"><option value="">全部用途</option><option value="__unclassified">用途待确认</option>${Object.entries(domains).map(([id,name])=>`<option value="${id}">${name}</option>`).join('')}</select><select id="library-tool" aria-label="按所属工作端筛选"><option value="">全部工作端 / 服务</option></select><span id="library-count" role="status"></span></div><p id="library-description" class="library-note"></p><div id="library-list" class="library-grid"></div><button id="library-more" class="btn" type="button" hidden>显示更多</button><details id="library-sources" class="library-sources"><summary>发现来源与覆盖范围</summary><div id="library-source-list"></div><h3>添加这台电脑的其他来源</h3><p>可添加其他工作端的 Skill 目录或公开接口声明文件；不会移动文件、执行脚本或改写原工具配置。工具标识可自定义，例如 my-worker。</p><div id="library-source-editor"></div><div class="library-source-actions"><button id="library-source-add" class="btn small" type="button">添加来源</button><button id="library-source-save" class="btn small primary" type="button">保存来源并重新发现</button></div><p id="library-source-result" role="status"></p></details></section><section id="library-tasks" hidden></section>`;
      get('query').value=restored?.query||'';get('domain').value=restored?.domain||params.get('domain')||'';
      function render() {
        if (!active()||tab==='tasks') return;
        get('inventory').hidden=false;get('tasks').hidden=true;
        get('query').hidden=false;get('domain').hidden=false;
        get('description').textContent=tab==='skills'?'按用途和工作端查找任务方法；用途根据名称与说明推测，需结合 Skill 内容判断。右键可定位目录。':tab==='interfaces'?'图像、视频、语音和任务接口的接入线索。完成工作端接入后，已声明能力出现在任务调度。':'服务配置与接口能力分开展示，避免把“有密钥”误认为“已接通”。';
        if(!data){get('count').textContent='正在发现…';return;}
        const items=tab==='skills'?rows(data.suggestions):tab==='interfaces'?rows(data.interfaces):rows(data.credentials);
        const visible=filter(items,get('query').value,get('domain').value,get('tool').value);
        get('count').textContent=`${visible.length} / ${items.length} 项${data.truncated?' · 发现未完整':''}`;
        get('list').className=tab==='credentials'?'library-credentials':'library-grid';
        el.dataset.libraryLimit=String(limit);get('more').hidden=tab==='credentials'||visible.length<=limit;get('list').innerHTML=tab==='credentials'?credentialsHTML(visible):inventoryHTML(visible.slice(0,limit),tab);
        if(tab==='interfaces'&&!visible.length){const related=filter(data.suggestions,get('query').value,get('domain').value,get('tool').value);if(related.length)get('list').innerHTML+=`<div class="library-empty"><strong>本机另有 ${related.length} 个相关 Skill</strong><p>Skill 是任务方法；接口是工作端可以调用的服务。可先查看这些 Skill，再由工作端登记实际调用接口。</p><a class="btn small" href="#/capabilities?tab=skills&domain=${encodeURIComponent(get('domain').value)}">查看相关 Skill</a></div>`;}
        get('source-list').innerHTML=rows(data.sources).map(s=>`<p><b>${esc(toolNames[s.tool]||s.tool||sourceNames[s.type]||s.name||'发现来源')}</b> <span class="badge">${esc(sourceStatus[s.status]||'已检查')}</span><span class="pathline">${esc(s.path||s.label||'')}</span></p>`).join('')+`<p>本次跳过 ${Number(data.skipped)||0} 项。只读取明确来源的声明，不读取会话、账号或密钥内容。${data.truncated?'达到扫描上限，未显示部分不能视为不存在。':''}</p>`;
      }
      async function select(next) {
        tab=next;el.dataset.libraryTab=tab;
        for(const id of Object.keys(tabs)){const button=get('tab-'+id);button.classList.toggle('active',id===tab);button.setAttribute('aria-pressed',String(id===tab));}
        get('tasks').hidden=tab!=='tasks';get('inventory').hidden=tab==='tasks';
        if(tab==='tasks') {
          if(!taskLoading){get('tasks').dataset.loaded='true';taskLoading=taskPage(get('tasks'),params,restored?.task);}
          await taskLoading;
        } else render();
      }
      async function discover() {
        if(sourceSaving)return;const serial=++request;get('refresh').disabled=true;get('error').textContent='';
        try {const result=await api('/api/capabilities/discover');if(!active()||serial!==request)return;data=result;el.dataset.libraryRoot=data.root||data.workspace_root||'';if(restored?.sourceDraft && restored.root===el.dataset.libraryRoot && !sourceDirty){sourceDirty=true;el.dataset.librarySourceDirty='true';el.dataset.librarySourceRoot=sourceRoot;renderSources(restored.sourceDraft);restored={...restored,sourceDraft:null};}else if(!sourceDirty){sourceRoot=el.dataset.libraryRoot;el.dataset.librarySourceRoot=sourceRoot;renderSources(rows(data.configured_sources));}const selected=get('tool').value||restored?.tool||'';const tools=[...new Set([...rows(data.suggestions),...rows(data.interfaces),...rows(data.credentials)].flatMap(toolsOf))].sort();get('tool').innerHTML='<option value="">全部工作端 / 服务</option><option value="__independent">独立服务 / 未关联工作端</option>'+tools.map(t=>`<option value="${esc(t)}">${esc(toolNames[t]||t)}</option>`).join('');get('tool').value=selected;render();}
        catch(e){if(active()&&serial===request){get('error').textContent=`发现未完成：${e.message}。${data?'已保留上次结果。':'请先在工作环境中选择可访问的工作区。'}`;get('count').textContent='发现暂不可用';}}
        finally{if(active()&&serial===request)get('refresh').disabled=false;}
      }
      function sourceDraft() {
        return [...get('source-editor').querySelectorAll('[data-source-row]')].map(row=>({tool:row.querySelector('[data-source-tool]').value.trim(),kind:row.querySelector('[data-source-kind]').value,path:row.querySelector('[data-source-path]').value.trim()}));
      }
      function renderSources(items) {
        get('source-editor').innerHTML=items.map((s,index)=>`<div class="library-source-row" data-source-row><input data-source-tool aria-label="所属工具标识" placeholder="例如 codex 或 my-worker" value="${esc(s.tool)}"><select data-source-kind aria-label="来源类型"><option value="skills_root" ${s.kind==='skills_root'?'selected':''}>Skill 目录</option><option value="capability_manifest" ${s.kind==='capability_manifest'?'selected':''}>公开接口声明</option></select><input data-source-path aria-label="来源完整路径" placeholder="本机完整目录或 .capabilities.json 文件路径" value="${esc(s.path)}"><button class="btn small" type="button" data-source-remove="${index}">移除此来源</button></div>`).join('');
        get('source-editor').querySelectorAll('input,select').forEach(node=>node.oninput=()=>{sourceDirty=true;el.dataset.librarySourceDirty='true';});
        get('source-editor').querySelectorAll('[data-source-remove]').forEach(button=>button.onclick=()=>{if(sourceSaving)return;const next=sourceDraft();next.splice(Number(button.dataset.sourceRemove),1);sourceDirty=true;el.dataset.librarySourceDirty='true';renderSources(next);});
      }
      get('source-add').onclick=()=>{if(sourceSaving)return;sourceDirty=true;el.dataset.librarySourceDirty='true';renderSources([...sourceDraft(),{tool:'',kind:'skills_root',path:''}]);};
      get('source-save').onclick=async()=>{
        if(sourceSaving||!data?.sources_revision)return;
        sourceSaving=true;get('source-save').disabled=true;get('source-add').disabled=true;get('refresh').disabled=true;
        get('source-editor').querySelectorAll('input,select,button').forEach(n=>n.disabled=true);
        try {
          const saved=await api('/api/capabilities/sources',{body:{sources:sourceDraft(),revision:data.sources_revision,_workspace_root:sourceRoot}});
          if(!active())return;
          if(!saved.saved)throw new Error('服务未确认保存成功');
          sourceDirty=false;el.dataset.librarySourceDirty='false';data.sources_revision=saved.sources_revision;data.configured_sources=saved.configured_sources;
          get('source-result').textContent='来源已保存，原文件保留在原位置。';
        }catch(e){if(active())get('source-result').textContent=e.message;}
        finally{sourceSaving=false;if(active()){get('source-save').disabled=false;get('source-add').disabled=false;get('refresh').disabled=false;get('source-editor').querySelectorAll('input,select,button').forEach(n=>n.disabled=false);}}
        if(active()&&!sourceDirty)await discover();
      };
      for(const id of Object.keys(tabs))get('tab-'+id).onclick=()=>select(id);
      const refilter=()=>{limit=48;render();};get('query').oninput=refilter;get('domain').onchange=refilter;get('tool').onchange=refilter;get('more').onclick=()=>{limit+=48;render();};get('refresh').onclick=discover;
      await Promise.all([select(tab),discover()]);
    };
  }
  return {createPage,capture,filter,toolsOf,inventoryHTML,credentialsHTML};
});
