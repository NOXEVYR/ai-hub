(function(root,factory){if(typeof module==='object'&&module.exports)module.exports=factory(require('./harnesses.js'));else root.AIHubCapabilities=factory(root.AIHubHarnesses);})(globalThis,function(Harness){
  'use strict';
  const bindDraft = (container, key, transfer=false) => globalThis.AIHubAppUpdate?.bind(container, key, {transfer}) || {snapshot(){},saved(){},changed(){return false;},edit(){},discard(){}};
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const domains={video:'视频',image:'图像',audio:'语音',code:'代码',research:'研究',document:'文档',automation:'自动化'};
  const rows=v=>Array.isArray(v)?v:[];
  const date=v=>v?new Date(v).toLocaleString('zh-CN',{hour12:false}):'尚无心跳记录';
  const json=v=>JSON.stringify(v??{},null,2);
  const uuid=v=>typeof v==='string'&&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(v);
  const hashWorkspace=async root=>Array.from(new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256',new TextEncoder().encode(root)))).map(v=>v.toString(16).padStart(2,'0')).join('');
  function capture(el){const values={};for(const id of ['query','domain','kind','tool','task-query','task-domain','project','title','inputs'])values[id]=el?.querySelector('#cp-'+id)?.value||'';let dispatchIntent=null;try{dispatchIntent=JSON.parse(el?.dataset.cpDispatchIntent||'null');}catch(_){}return{values,selected:el?.dataset.cpSelected||'',root:el?.dataset.cpRoot||'',dispatchIntent};}
  function cards(items,selected,unavailable=false){return rows(items).map(c=>`<article class="panel cp-card"><div class="cp-card-heading"><span class="badge">${c.kind==='skill'?'Skill':'MCP 接口'}</span><h3>${esc(c.name)}</h3></div><p>${esc(c.description)}</p><div class="cp-tags">${rows(c.domains).map(d=>`<span>${esc(domains[d]||d)}</span>`).join('')}</div><div class="cp-evidence"><div><small>声明</small><strong>客户端已声明</strong><span>来源 ${esc(c.provider||'未注明')} / ${esc(c.server||'未注明服务')}</span></div><div><small>工作端心跳</small><strong>${c.client_online?'近期有心跳':'未见近期心跳'}</strong><span>${esc(date(c.last_seen))}</span></div><div><small>执行验证</small><strong>未经执行验证</strong><span>排队交给 ${esc(c.target_tool||'指定工作端')}</span></div></div>${rows(c.reasons).length?`<div class="cp-match"><strong>声明匹配理由</strong>${rows(c.reasons).map(r=>`<p>${esc(r)}</p>`).join('')}</div>`:''}<details><summary>输入要求、产物与限制</summary><p>客户端：${esc(c.client_id)} · 能力来源与执行端分别登记。</p><strong>输入结构</strong><pre>${esc(json(c.inputs))}</pre><strong>预期产物</strong><pre>${esc(json(c.outputs||[]))}</pre><strong>约束</strong><pre>${esc(json(c.constraints||[]))}</pre><p>成本 / 速度 / 质量：${esc(c.hints?.cost||'未声明')} / ${esc(c.hints?.speed||'未声明')} / ${esc(c.hints?.quality||'未声明')}（均为声明，未实测）</p></details><div class="cp-card-footer"><small>队列交接 · 需工作端领取执行</small><button class="btn small ${c.id===selected?'primary':''}" data-cp-select="${esc(c.id)}" ${unavailable||c.tool_enabled===false?'disabled':''}>${unavailable?'等待目录刷新':c.tool_enabled===false?'工作端已停用 / 仅手动交接':c.id===selected?'已选此能力':'选入任务'}</button></div></article>`).join('')||'<div class="cp-empty">当前条件下没有已声明能力。先连接工作端发布能力声明；本机发现的 Skill 仅是接入线索。</div>';}
  function discoverHTML(data){return `<p>发现 ${rows(data.suggestions).length} 条本机 Skill 线索；${data.truncated?'扫描未完整。':''}跳过 ${esc(data.skipped||0)} 项。尚未发布到能力目录，不能从这里直接执行。</p>${rows(data.suggestions).map(s=>`<article><strong>${esc(s.name)}</strong><span class="badge">${esc(s.tool)}</span><p>${esc(s.description||'未提供简介')}</p><small>${esc(s.path)}</small></article>`).join('')}`;}
  function createPage({api,heading,updateRoute,createRequestId=()=>globalThis.crypto.randomUUID(),workspaceHash=hashWorkspace}){let generation=0;return async(el,params=new URLSearchParams(),restored)=>{
    const run=++generation,active=()=>el.isConnected&&generation===run;
    let items=[],harnesses=[],matches=null,selected=restored?.selected||'',root='',busy=false,catalogReady=false,request=0,matchRequest=0;
    let rootHash='',newRequestArmed=false;
    let dispatchIntent=restored?.dispatchIntent||(uuid(params.get('dispatch_request_id'))?{request_id:params.get('dispatch_request_id'),root_hash:params.get('dispatch_workspace'),payload:null}:null);
    const saveIntent=()=>{
      el.dataset.cpDispatchIntent=JSON.stringify(dispatchIntent);
      if(active()&&globalThis.location?.hash?.startsWith('#/capabilities')&&globalThis.history?.replaceState){
        const url=new URL(globalThis.location.href),query=new URLSearchParams(url.hash.split('?')[1]||'');
        if(dispatchIntent){query.set('dispatch_request_id',dispatchIntent.request_id);query.set('dispatch_workspace',dispatchIntent.root_hash||'');}
        else{query.delete('dispatch_request_id');query.delete('dispatch_workspace');}
        url.hash='#/capabilities'+(query.size?'?'+query.toString():'');
        if(updateRoute)updateRoute(url.hash);else globalThis.history.replaceState(globalThis.history.state,'',url);
      }
      const recovery=get('recover'),fresh=get('new-request');if(recovery){recovery.hidden=!dispatchIntent;recovery.disabled=busy;}
      if(fresh){fresh.hidden=!dispatchIntent;fresh.disabled=busy;fresh.textContent=newRequestArmed?'确认结束核对':'结束核对并新建';}
    };
    const get=id=>el.querySelector('#cp-'+id),error=e=>{get('error').textContent=e?.message||String(e||'');};
    const post=(name,body)=>api('/api/capabilities/'+name,{body:root?{...body,_workspace_root:root}:body});
    el.classList?.add('capabilities-page');el.innerHTML=(heading?heading('能力与调度','查看各工具声明的 Skill 与接口，把任务交给合适的工作端。','CAPABILITY DIRECTORY'):'<h2>能力与调度</h2>')+`<div class="cp-intro"><p>曜核负责发现、匹配与任务交接；创作内容仍由原工具处理，执行结果登记到统一项目。</p><button id="cp-refresh" class="btn small">刷新目录</button></div><div id="cp-error" class="cp-error" role="alert"></div><div class="cp-coverage"><strong id="cp-count">正在读取声明…</strong><p>已声明 ≠ 可立即调用。近期心跳不保证实时在线，所有能力均未经执行验证；排队也不代表已开始运行。</p><a href="#/collaboration?tab=connect">配置工作端接入</a></div><div class="cp-layout"><section><form id="cp-filter-form" class="cp-filters"><label>目录关键词<input id="cp-query" type="search" placeholder="名称、说明、提供方"></label><label>能力领域<select id="cp-domain"><option value="">全部领域</option>${Object.entries(domains).map(([v,t])=>`<option value="${v}">${t}</option>`).join('')}</select></label><label>类型<select id="cp-kind"><option value="">全部类型</option><option value="skill">Skill</option><option value="mcp_tool">MCP 接口</option></select></label><label>执行工作端<select id="cp-tool"><option value="">全部工作端</option></select></label><button type="submit" class="btn small">筛选目录</button></form><div class="cp-actions"><span id="cp-list-caption">当前工作区的客户端声明</span><button id="cp-clear-match" class="btn small ghost" hidden>返回全部声明</button></div><div id="cp-list" class="cp-catalog"></div><details class="panel cp-discovery"><summary>本机 Skill 接入线索（只读）</summary><p>只读取已知 Skill 目录的声明头，不读取原生会话，不执行 Skill，也不自动发布能力。</p><button id="cp-discover" class="btn small">发现本机 Skill</button><div id="cp-discover-results" role="status"></div></details></section><aside class="panel cp-task-panel"><div class="panel-head"><h3>任务匹配与交接</h3></div><div class="body"><form id="cp-match-form" class="cp-task-fields"><label>要完成的工作<textarea id="cp-task-query" rows="3" placeholder="例如：整理资料并输出项目报告" required maxlength="2000"></textarea></label><label>期望领域<select id="cp-task-domain"><option value="">从描述匹配</option>${Object.entries(domains).map(([v,t])=>`<option value="${v}">${t}</option>`).join('')}</select></label><button id="cp-match" class="btn">匹配声明能力</button></form><p class="cp-legend">按声明文字与领域推荐，不比较真实质量、成本或成功率。</p><div id="cp-selection" class="cp-selection">从目录选择一个能力后，核对输入要求。</div><form id="cp-dispatch-form" data-app-draft-scope="cp-dispatch-form" class="cp-task-fields"><label>归档项目<input id="cp-project" required maxlength="120" placeholder="统一任务所属项目"></label><label>任务标题<input id="cp-title" required maxlength="200" placeholder="给工作端的具体任务"></label><label>交接输入（JSON 对象）<textarea id="cp-inputs" rows="7" spellcheck="false" required>{}</textarea></label><p class="cp-legend">输入须符合所选能力结构；请勿粘贴密钥。工作端领取后核对权限与费用，执行后登记报告和产物。</p><button id="cp-dispatch" class="btn primary" disabled>创建统一任务并排队</button></form><div class="cp-actions"><button id="cp-recover" class="btn small" hidden>核对未确认派单</button><button id="cp-new-request" class="btn small ghost" hidden>结束核对并新建</button></div><div id="cp-result" class="cp-result" role="status" aria-live="polite"></div></div></aside></div>`;
    for(const [id,value]of Object.entries(restored?.values||{}))if(get(id))get(id).value=value;
    if(!get('inputs').value)get('inputs').value='{}';if(!restored)get('domain').value=params.get('domain')||'';
    let draft=bindDraft(get('dispatch-form'),'capability-task:'+ (restored?.root||''));
    function current(){return items.find(i=>i.id===selected)||(matches||[]).find(i=>i.id===selected);}
    function selection(){el.dataset.cpSelected=selected;const item=current();get('dispatch').disabled=busy||!!(dispatchIntent&&!dispatchIntent.payload)||!catalogReady||!item||!Harness.canQueue(harnesses,item.target_tool)||item.tool_enabled===false;get('selection').innerHTML=item?`<strong>${esc(item.name)}</strong><p>${esc(item.provider)} → ${esc(item.target_tool)}</p><details open><summary>输入结构（客户端声明）</summary><pre>${esc(json(item.inputs))}</pre></details>`:'从目录选择一个能力后，核对输入要求。';saveIntent();}
    function verifyReceipt(data,id,lookup=false){
      if(!data||data.request_id!==id||data.workspace_root!==root||typeof data.deduplicated!=='boolean'||(lookup&&typeof data.found!=='boolean'))throw Error('派单回复未能确认原请求，已保留编号');
      if(lookup&&!data.found){if(data.task!==null)throw Error('派单回查回复不一致');return false;}
      if(!uuid(data.task?.id)||!['queued','active','completed','cancelled','preparing'].includes(data.task.status))throw Error('派单回复缺少有效任务，已保留编号');
      return true;
    }
    function showReceipt(data){
      get('result').innerHTML=`<strong>${data.deduplicated?'已恢复原任务，没有重复派单':'任务已排队，等待工作端领取'}</strong><p>${esc(data.task.title)} · ${esc(data.task.id)}</p><p>${data.task.deleted_at?'原任务已从列表移除，历史回执仍保留':(!data.deduplicated&&data.task.status==='queued'?'尚未执行，':'')+'当前任务状态：'+esc(data.task.status)}；在协作与记忆中查看状态、交接和产物。</p><a class="btn small" href="#/collaboration?tab=tasks">查看统一任务</a><a class="btn small" href="#/reports">查看归档报告</a>`;
    }
    get('recover').onclick=async()=>{
      if(busy||!dispatchIntent)return;busy=true;saveIntent();error('');const id=dispatchIntent.request_id;
      try{
        const workspace=await api('/api/workspace/status');if(!active())return;
        const currentRoot=workspace.root||workspace.ai_root||workspace.workspace?.ai_root||'';
        if(!currentRoot||await workspaceHash(currentRoot)!==dispatchIntent.root_hash)throw Error('未确认派单属于其他工作区，请切回原工作区核对');
        root=currentRoot;rootHash=dispatchIntent.root_hash;
        const data=await post('dispatch-receipt',{request_id:id});if(!active())return;
        if(verifyReceipt(data,id,true)){showReceipt(data);dispatchIntent=null;newRequestArmed=false;saveIntent();}
        else error('当前工作区没有原请求回执，未新建任务。请核对工作区及任务列表，再决定结束核对。');
      }catch(e){if(active())error(e);}finally{busy=false;if(active())selection();}
    };
    get('new-request').onclick=()=>{
      if(busy||!dispatchIntent)return;
      if(!newRequestArmed){newRequestArmed=true;error('结束核对不会删除原任务；新请求可能与原任务重复。请先检查任务列表，确认后再点击。');saveIntent();return;}
      dispatchIntent=null;newRequestArmed=false;error('');selection();
    };
    function render(){const query=get('query').value.trim().toLowerCase(),domain=get('domain').value,kind=get('kind').value,tool=get('tool').value;
      const visible=(matches??items).filter(c=>(!domain||rows(c.domains).includes(domain))&&(!kind||c.kind===kind)&&(!tool||c.target_tool===tool)&&(!query||[c.name,c.description,c.provider,c.server,...rows(c.tags)].join(' ').toLowerCase().includes(query)));
      get('list').innerHTML=cards(visible.map(c=>({...c,tool_enabled:c.tool_enabled!==false&&Harness.canQueue(harnesses,c.target_tool)})),selected,!catalogReady);get('list-caption').textContent=`${!catalogReady?'目录未验证，仅显示上次结果':matches===null?'客户端声明':'匹配候选'} · 当前筛选 ${visible.length} 项`;get('clear-match').hidden=matches===null;
      el.querySelectorAll('[data-cp-select]').forEach(b=>b.onclick=()=>{if(busy||!catalogReady)return;const item=items.find(c=>c.id===b.dataset.cpSelect)||(matches||[]).find(c=>c.id===b.dataset.cpSelect);if(!item||!Harness.canQueue(harnesses,item.target_tool)||item.tool_enabled===false)return;selected=b.dataset.cpSelect;selection();render();get('project').focus?.();});
    }
    async function load(){const serial=++request;catalogReady=false;++matchRequest;get('match').disabled=true;selection();render();get('refresh').disabled=true;error('');try{const [data,registry]=await Promise.all([api('/api/capabilities/list'),api('/api/harnesses')]);if(!active()||serial!==request)return;harnesses=rows(registry?.items);const nextRoot=data.root||data.workspace_root||root;
      if((root||restored?.root)&&(root||restored.root)!==nextRoot){selected='';error('工作环境已切换，已清除旧能力选择；未确认派单编号与文字草稿保留。');}root=nextRoot;rootHash=await workspaceHash(root);if(!active()||serial!==request)return;restored=restored?{...restored,root}:restored;el.dataset.cpRoot=root;draft=bindDraft(get('dispatch-form'),'capability-task:'+(restored?.root||root),true);
      catalogReady=true;items=rows(data.items);matches=null;++matchRequest;get('match').disabled=false;const tool=get('tool').value||restored?.values?.tool||'';get('tool').innerHTML=Harness.options(harnesses,{history:true,emptyLabel:'全部工作端',extras:items.map(i=>i.target_tool)});get('tool').value=tool;if(restored?.values)restored={...restored,values:{...restored.values,tool:''}};
      if(!current())selected='';get('count').textContent=`已声明 ${items.length} 项能力 · ${items.filter(i=>i.client_online).length} 项近期有工作端心跳`;selection();render();
    }catch(e){if(active()&&serial===request){error(e);get('count').textContent='能力目录暂不可用；草稿已保留，请刷新后派单';selection();render();}}finally{if(active()&&serial===request)get('refresh').disabled=false;}}
    get('filter-form').onsubmit=e=>{e.preventDefault();render();};get('clear-match').onclick=()=>{if(!catalogReady)return;matches=null;++matchRequest;get('match').disabled=false;render();};get('refresh').onclick=()=>!busy&&load();
    get('match-form').onsubmit=async e=>{e.preventDefault();if(!catalogReady||busy)return;const query=get('task-query').value.trim();if(!query){error('请先描述任务。');return;}const serial=++matchRequest;get('match').disabled=true;error('');try{const data=await post('recommend',{query,domain:get('task-domain').value});if(!active()||serial!==matchRequest)return;matches=rows(data.items);render();}catch(e){if(active()&&serial===matchRequest)error(e);}finally{if(active()&&serial===matchRequest)get('match').disabled=!catalogReady;}};
    get('dispatch-form').onsubmit=async e=>{e.preventDefault();if(busy||!catalogReady||!current())return;error('');if(!Harness.canQueue(harnesses,current().target_tool)||current().tool_enabled===false){error('该工作端已停用或仅支持手动交接，不能创建自动领取任务。草稿已保留。');return;}const project=get('project').value.trim(),titleDraft=get('title').value,title=titleDraft.trim(),input_json=get('inputs').value;try{const value=JSON.parse(input_json);if(!value||Array.isArray(value)||typeof value!=='object')throw new Error();}catch(_){error('交接输入必须是有效的 JSON 对象。');return;}if(!project||!title){error('请填写归档项目和任务标题。');return;}const submitted=draft.snapshot();busy=true;selection();get('refresh').disabled=true;
      try{const payload={capability_id:selected,project,title,input_json},signature=JSON.stringify({...payload,root});if(dispatchIntent&&dispatchIntent.signature!==signature)throw Error('原派单尚未确认，请先核对原任务或结束核对，再提交新的内容');if(!dispatchIntent)dispatchIntent={request_id:createRequestId(),signature,payload,root_hash:rootHash};saveIntent();const id=dispatchIntent.request_id,data=await post('dispatch',{...payload,request_id:id});verifyReceipt(data,id);dispatchIntent=null;newRequestArmed=false;saveIntent();draft.saved(submitted);if(!active())return;showReceipt(data);if(get('title').value===titleDraft)get('title').value='';}
      catch(e){if(active())error((e?.message||String(e))+'；再次提交相同内容会按原请求编号核对，避免重复派单。');}finally{busy=false;if(active()){selection();get('refresh').disabled=false;}}
    };
    get('discover').onclick=async()=>{get('discover').disabled=true;error('');try{const data=await api('/api/capabilities/discover');if(active())get('discover-results').innerHTML=discoverHTML(data);}catch(e){if(active())error(e);}finally{if(active())get('discover').disabled=false;}};
    await load();
  };}
  return{createPage,capture,cards,discoverHTML,esc};
});
