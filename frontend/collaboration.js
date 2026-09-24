/* Shared collaboration UI. Drafts live only in the navigation snapshot. */
(function(root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.AIHubCollaboration = factory();
})(globalThis, function() {
  'use strict';
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const rows = v => Array.isArray(v) ? v : Array.isArray(v?.items) ? v.items : [];
  const names = {queued:'等待领取',active:'有效',completed:'已完成',candidate:'待审核',approved:'已批准',retired:'已退役',recycled:'已回收',protected:'已保留',error:'回收失败，已保留',missing:'文件缺失',report:'正式报告',output:'交付产物',temp:'临时文件',knowledge:'知识参考',temp_candidate:'临时内容候选（仅盘点）',other_text:'其他文本',workspace:'工作区',project:'项目'};
  const label = v => names[v] || v || '未知';
  const timeText = value => {
    if(!value)return '—';
    const date=new Date(typeof value==='number'?value*1000:value);
    return Number.isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{hour12:false});
  };
  const tools = '<option value="any">任意已接入工具</option><option value="codex">Codex</option><option value="zcode">ZCode</option><option value="workbuddy">WorkBuddy</option><option value="dsh">DSH</option>';
  const fields = ['task-project','task-title','task-description','task-tool','memory-title','memory-content','memory-scope','memory-project','memory-source','search-query','search-project','source-path','source-label','source-tool','policy-days','policy-enabled','mcp-tool'];
  const tabs = {tasks:'任务与产物',memories:'长期记忆',sources:'报告来源',connect:'工具接入',retention:'临时文件回收'};
  function capture(el) {
    const draft = {};
    for (const id of fields) {
      const node = el?.querySelector('#co-' + id);
      if (node) draft[id] = node.type === 'checkbox' ? node.checked : node.value;
    }
    return {draft, tab:el?.dataset?.coTab || 'tasks', selectedTask:el?.dataset?.coTask || '', root:el?.dataset?.coRoot || ''};
  }
  function restore(el, snapshot) {
    for (const [id, value] of Object.entries(snapshot?.draft || {})) {
      if (!fields.includes(id)) continue;
      const node = el.querySelector('#co-' + id);
      if (!node) continue;
      if (node.type === 'checkbox') node.checked = value === true; else node.value = String(value ?? '');
    }
  }
  const button = (action, id, title, extra='') => `<button type="button" class="btn small" data-co-action="${action}" data-co-id="${esc(id)}" ${extra}>${title}</button>`;
  function table(headers, body, empty) {
    return body.length ? `<div class="table-wrap"><table class="tbl co-table"><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${body.join('')}</tbody></table></div>` : `<p class="muted co-empty">${empty}</p>`;
  }
  function tasksHTML(items) {
    return table(['任务 / 项目','工具 / 领取者','状态','操作'], rows(items).map(t=>`<tr><td><b>${esc(t.title)}</b><span class="file-sub">${esc(t.project)} · ${esc(t.id)}</span></td><td>${esc(t.target_tool)}<span class="file-sub">${esc(t.owner || '尚未领取')}</span></td><td>${esc(t.status==='active'?'执行中':label(t.status))}</td><td>${button('task-detail',t.id,'查看目录与交接')}${t.status==='active'?button('requeue',t.id,'释放领取'):''}</td></tr>`),'暂无任务。先创建任务，再由接入工具主动领取。');
  }
  function artifactsHTML(items) {
    return table(['产物 / 路径','类别 / 状态','到期时间','保留'],rows(items).map(a=>`<tr><td><b>${esc(a.title)}</b><span class="file-sub pathline">${esc(a.path)}</span><span class="file-sub">任务 ${esc(a.task_id)}</span></td><td>${esc(label(a.kind))} · ${esc(label(a.status))}</td><td>${esc(a.expires_at ? timeText(a.expires_at) : '长期保留')}</td><td>${a.status==='active' ? button('pin',a.id,a.pinned?'取消锁定':'锁定保留',`aria-pressed="${!!a.pinned}"`) : esc(a.pinned?'已锁定':'—')}</td></tr>`),'暂无已登记产物。接入工具按 report、output、temp 分类写入并登记。');
  }
  function memoriesHTML(items, review=true) {
    return table(['记忆与内容','范围 / 来源','状态','审核'],rows(items).map(m=>`<tr><td><b>${esc(m.title)}</b><details><summary>阅读内容</summary><pre class="co-prose">${esc(m.content)}</pre></details></td><td>${esc(label(m.scope))}${m.project?' · '+esc(m.project):''}<span class="file-sub">来源报告 ${esc(m.source_artifact_id)}</span><span class="file-sub">${esc(timeText(m.updated_at || m.created_at))}</span></td><td>${esc(label(m.status))}</td><td>${review && m.status==='candidate' ? button('approve',m.id,'批准保留') : ''}${review && m.status!=='retired' ? button('retire',m.id,'退役') : '—'}</td></tr>`),review?'暂无共享记忆候选。只把经确认、可复用且有报告来源的结论提交审核。':'未找到已批准的长期记忆。候选与退役条目不进入检索。');
  }
  function sourcesHTML(items) {
    return table(['来源 / 目录','工具','最近盘点','操作'],rows(items).map(s=>`<tr><td><b>${esc(s.label || s.path)}</b><span class="file-sub pathline">${esc(s.path)}</span></td><td>${esc(s.tool)}</td><td>${esc(s.scanned_at || s.last_scan_at ? timeText(s.scanned_at || s.last_scan_at) : '未盘点')}<span class="file-sub">${esc(s.file_count ?? 0)} 份文档</span>${s.scanned_at || s.last_scan_at ? `<span class="file-sub ${s.truncated?'warning-note':''}">${s.truncated?'未完整（达到扫描预算或部分不可读）':'已完成本次盘点'}</span>`:''}${s.error?`<span class="file-sub dialog-error">${esc(s.error)}</span>`:''}</td><td>${button('scan',s.id,'只读盘点')}</td></tr>`),'暂无报告来源。明确添加报告目录后再盘点，原文件保持原位。');
  }
  function previewHTML(result) {
    const items=rows(result), errors=rows(result?.errors);
    return `<p>当前符合回收条件：${items.length} 项。执行时会重新检查；文件变化或回收失败会保留。</p>${table(['临时文件','到期 / 检查'],items.map(a=>`<tr><td class="pathline">${esc(a.path || a.title || a.id)}</td><td>${esc(a.expires_at ? timeText(a.expires_at) : a.reason || '已通过预检')}</td></tr>`),'没有符合条件的临时文件。')}${rows(result?.protected).length?`<details><summary>已保护 ${rows(result.protected).length} 项，查看原因</summary>${table(['文件','保留原因'],rows(result.protected).map(a=>`<tr><td class="pathline">${esc(a.path)}</td><td>${esc(a.reason)}</td></tr>`),'')}</details>`:''}${errors.map(e=>`<p class="dialog-error">${esc(typeof e==='string'?e:e.error || e.reason)}</p>`).join('')}`;
  }
  function inventoryHTML(result) {
    const inventoryLabels={report:'报告线索',knowledge:'知识线索',temp_candidate:'疑似临时',other_text:'其他文档'};
    return `<h4>报告盘点结果</h4><p class="caption-note">分类按路径推测，未做内容审核。</p>${table(['报告路径','大小 / 分类线索'],rows(result).map(r=>`<tr><td class="pathline">${esc(r.path)}</td><td>${esc(r.size ?? '—')} 字节 · ${esc(inventoryLabels[r.category] || '其他文档')}</td></tr>`),'没有发现符合规则的文本报告。')}${result?.truncated?'<p>结果已达盘点上限，仅显示部分条目。</p>':''}${rows(result?.errors).map(e=>`<p class="dialog-error">${esc(typeof e==='string'?e:e.error || e.reason)}</p>`).join('')}`;
  }
  function shell() {
    return `<div class="co-toolbar"><div id="co-root" class="pathline"></div><button id="co-refresh" class="btn" type="button">刷新协作状态</button></div><p id="co-error" class="dialog-error co-alert" role="alert" tabindex="-1"></p><p id="co-message" role="status" aria-live="polite"></p><div id="co-availability"></div><div class="co-tabs" role="group" aria-label="协作管理区域">${Object.entries(tabs).map(([id,title])=>`<button type="button" class="btn" data-co-tab="${id}" aria-pressed="false">${title}</button>`).join('')}</div>
      <section data-co-panel="tasks"><div class="panel"><div class="panel-head"><h3>新建协作任务</h3></div><div class="body"><p class="caption-note">项目统一使用 40_Projects/&lt;项目&gt;/Work/AIHub/&lt;任务 ID&gt;，报告、输出、临时文件分别进入 Reports、Outputs、Temp。工具通过队列主动领取，不会在这里自动启动其他软件。</p><form id="co-task-form"><fieldset class="co-fields"><div class="co-grid"><label>项目名称<input id="co-task-project" required maxlength="80" autocomplete="off"></label><label>交接工具<select id="co-task-tool">${tools}</select></label><label class="co-wide">任务标题<input id="co-task-title" required maxlength="200"></label><label class="co-wide">任务说明<textarea id="co-task-description" rows="4" maxlength="20000"></textarea></label></div><button class="btn primary" type="submit">创建任务与目录</button></fieldset></form></div></div><section class="panel"><div class="panel-head"><h3>最近任务</h3></div><div id="co-tasks"></div><div id="co-task-detail" class="body" aria-live="polite"></div><div id="co-requeue-confirm" class="body" aria-live="polite"></div></section><section class="panel"><div class="panel-head"><h3>已登记产物</h3></div><div class="body caption-note">报告和交付产物默认保留；锁定后，临时文件也不会自动回收。</div><div id="co-artifacts"></div></section></section>
      <section data-co-panel="memories" hidden><section class="panel"><div class="panel-head"><h3>长期记忆审核</h3></div><div class="body"><p>共享记忆需先提交候选，再由你批准。保留稳定的约定、可复用结论与关键决策；过程日志留在报告中。退役保留记录，但不再用于共享检索。各工具原生记忆不会被读取或修改。</p></div><div id="co-memories"></div></section><section class="panel"><div class="panel-head"><h3>提交记忆候选</h3></div><div class="body"><form id="co-memory-form"><fieldset class="co-fields"><div class="co-grid"><label>记忆标题<input id="co-memory-title" required maxlength="200"></label><label>来源报告<select id="co-memory-source" required><option value="">请先登记报告产物</option></select></label><label>适用范围<select id="co-memory-scope"><option value="project">当前项目</option><option value="workspace">整个工作区</option></select></label><label>项目名称<input id="co-memory-project" maxlength="80"></label><label class="co-wide">可复用结论<textarea id="co-memory-content" rows="5" required maxlength="20000"></textarea></label></div><button class="btn primary" type="submit">提交候选，等待审核</button></fieldset></form></div></section><section class="panel"><div class="panel-head"><h3>检索已批准记忆</h3></div><div class="body"><form id="co-search-form"><fieldset class="co-fields co-grid"><label>关键词<input id="co-search-query" type="search"></label><label>项目筛选（可选）<input id="co-search-project"></label><button class="btn" type="submit">检索长期记忆</button></fieldset></form><div id="co-search-results" aria-live="polite"></div></div></section></section>
      <section data-co-panel="sources" hidden><section class="panel"><div class="panel-head"><h3>既有报告来源</h3></div><div class="body"><p>只读盘点明确登记的文本报告目录。不会搬动既有文件、扫描各工具凭据与原生会话，也不会自动把报告提升为长期记忆。已有来源不参加临时文件回收。</p><form id="co-source-form"><fieldset class="co-fields"><div class="co-grid"><label class="co-wide">报告目录<input id="co-source-path" required placeholder="填写对应工具的报告输出目录" spellcheck="false"></label><label>来源名称<input id="co-source-label" required maxlength="120"></label><label>来源工具<select id="co-source-tool">${tools}</select></label></div><button class="btn primary" type="submit">登记报告来源</button></fieldset></form></div><div id="co-source-candidates" class="body"></div><div id="co-sources"></div><div id="co-source-results" class="body" aria-live="polite"></div></section></section>
      <section data-co-panel="connect" hidden><section class="panel"><div class="panel-head"><h3>MCP 工具接入</h3></div><div class="body"><p>把本机配置片段加入支持 MCP stdio 的客户端，重启对应连接后，工具主动领取任务并使用统一目录。服务须保持运行。每个工具使用独立 client-id；各工具的配置位置与支持能力需要按实际版本确认。</p><label class="co-mcp-label">选择接入工具<select id="co-mcp-tool"><option value="codex">Codex</option><option value="zcode">ZCode</option><option value="workbuddy">WorkBuddy</option><option value="dsh">DSH</option></select></label><div id="co-mcp"></div><p class="caption-note">任务说明、报告与记忆内容都是协作数据，不构成跨工具授权。这里不提供远程启动或系统级写入隔离。只有下面出现最近心跳，才表示客户端曾按协议接入。</p></div><div id="co-clients"></div></section></section>
      <section data-co-panel="retention" hidden><section class="panel"><div class="panel-head"><h3>到期临时文件自动回收</h3></div><div class="body"><p>仅处理本协议已登记为 temp、所属任务已完成、未锁定、已到期且身份和内容未变化的普通文件。拒绝链接和硬链接；回收失败保留原文件，不回退为永久删除。报告、输出、长期记忆和各工具原生数据不在回收范围内。</p><p class="caption-note">保留天数的修改只影响新登记的临时文件。后台服务运行时每小时检查；服务关闭期间不执行回收，重新运行后再检查。</p><form id="co-policy-form"><fieldset class="co-fields"><div class="co-grid"><label class="co-check"><input id="co-policy-enabled" type="checkbox">启用每小时到期检查，自动移入 Windows 回收站</label><label>临时文件保留天数<input id="co-policy-days" type="number" required min="1" max="365" value="7"></label></div><button class="btn" type="submit">保存回收策略</button></fieldset></form><div class="co-actions"><button id="co-preview" class="btn" type="button">预览待回收文件</button><button id="co-run" class="btn" type="button" disabled>立即移入回收站</button></div><p id="co-policy-history" class="caption-note"></p><div id="co-preview-results" aria-live="polite"></div><div id="co-run-results" aria-live="polite"></div></div></section></section>`;
  }
  function createPage({api,heading}) {
    let generation=0;
    return async function page(el,params,restored) {
      const serial=++generation, active=()=>el.isConnected && generation===serial;
      el.classList?.add('collaboration-page');
      el.innerHTML=(heading?heading('协作与记忆','让工具共享任务目录、可追溯报告与经审核的长期记忆。','COLLABORATION'):'<h2>协作与记忆</h2>')+shell();
      const get=id=>el.querySelector('#co-'+id), post=(action,body={})=>api('/api/collaboration/'+action,{body:state.root ? {...body,_workspace_root:state.root} : body});
      let state={},busy=false,ready=false,policyDirty=!!restored?.draft,previewReady=false,requeueId=null;
      const setError=e=>{get('error').textContent=e?.message || String(e || '');};
      function selectTab(id) {
        id=Object.hasOwn(tabs,id)?id:'tasks';el.dataset.coTab=id;
        el.querySelectorAll('[data-co-panel]').forEach(n=>{n.hidden=n.dataset.coPanel!==id;});
        el.querySelectorAll('[data-co-tab]').forEach(n=>n.setAttribute('aria-pressed',String(n.dataset.coTab===id)));
      }
      function setBusy(value) {
        busy=value;
        el.querySelectorAll('fieldset.co-fields').forEach(n=>{n.disabled=value || !ready;});
        el.querySelectorAll('[data-co-action], #co-refresh, #co-preview').forEach(n=>{n.disabled=value || (!ready && n.id!=='co-refresh');});
        get('run').disabled=value || !ready || !previewReady;
      }
      async function act(fn) {
        if(busy || !active())return;
        setError('');get('message').textContent='正在处理…';setBusy(true);
        try{await fn();if(active())get('message').textContent='操作完成。';}
        catch(e){if(active()){setError(e);get('message').textContent='操作未完成，表单内容已保留。';}}
        finally{if(active())setBusy(false);}
      }
      function taskDetail(id) {
        const task=rows(state.tasks).find(t=>String(t.id)===String(id));el.dataset.coTask=task?String(task.id):'';
        get('task-detail').innerHTML=task?`<h4>${esc(task.title)}</h4><pre class="co-prose">${esc(task.description)}</pre>${task.summary?`<h4>最近交接 / 完成说明</h4><pre class="co-prose">${esc(task.summary)}</pre>`:''}<dl>${Object.entries(task.paths || {}).map(([k,v])=>`<dt>${esc({work:'任务工作目录',reports:'正式报告',outputs:'交付产物',temp:'临时文件'}[k] || k)}</dt><dd class="pathline">${esc(v)}</dd>`).join('')}</dl><p class="caption-note">创建目录不表示任务已被领取。请从「工具接入」配置客户端，再让工具领取此任务。</p>`:'';
      }
      function bindActions() {
        el.querySelectorAll('[data-co-action]').forEach(n=>{n.onclick=()=>{
        const id=n.dataset.coId,action=n.dataset.coAction;
          if(action==='task-detail'){taskDetail(id);return;}
          if(action==='requeue'){
            const task=rows(state.tasks).find(t=>String(t.id)===String(id));if(!task || task.status!=='active' || busy)return;
            requeueId=String(id);
            get('requeue-confirm').innerHTML=`<h4>释放「${esc(task.title)}」的领取状态？</h4><p>任务将回到等待领取，原领取凭证立即失效。任务目录和已登记产物会保留。此操作不会停止正在运行的工具，请确认原工具已中断或不会继续处理此任务。</p>${button('requeue-confirm',id,'确认释放领取')}${button('requeue-cancel',id,'取消')}`;
            bindActions();get('requeue-confirm').scrollIntoView?.({block:'nearest'});get('requeue-confirm').querySelector?.('[data-co-action="requeue-confirm"]')?.focus();return;
          }
          if(action==='requeue-cancel'){requeueId=null;get('requeue-confirm').innerHTML='';return;}
          if(action==='source-fill'){
            const source=rows(state.source_candidates)[Number(id)];if(!source)return;
            get('source-path').value=source.path;get('source-label').value=source.label;get('source-tool').value=source.tool;
            get('source-path').focus?.();return;
          }
          return act(async()=>{
            if(!ready)throw new Error('工作区不可用，请先在工作环境中完成配置。');
            if(action==='requeue-confirm'){
              const task=rows(state.tasks).find(t=>String(t.id)===String(id));
              if(requeueId!==String(id) || !task || task.status!=='active')throw new Error('请重新选择需要释放的执行中任务。');
              await post('task_requeue',{task_id:id,summary:'用户在管理界面释放中断任务'});
              if(!active())return;requeueId=null;get('requeue-confirm').innerHTML='';
            } else if(action==='pin'){
              const item=rows(state.artifacts).find(a=>String(a.id)===id);if(!item)throw new Error('产物已变化，请刷新。');
              await post('artifact_pin',{artifact_id:id,pinned:!item.pinned});
            } else if(action==='approve' || action==='retire')await post('memory_review',{memory_id:id,status:action==='approve'?'approved':'retired'});
            else if(action==='scan'){
              const result=await post('source_scan',{source_id:id});if(!active())return;
              get('source-results').innerHTML=inventoryHTML(result);
            }
            if(active())await refresh();
          });
        };});
      }
      function renderConfig() {
        const selected=rows(state.integrations).find(v=>v.tool===get('mcp-tool').value);
        const config=selected?.mcp_config || (get('mcp-tool').value==='codex' ? state.mcp_config || state.mcp?.config : null);
        get('mcp').innerHTML=config?`<p>通用 mcpServers 配置（按客户端格式填写）</p><pre class="co-code" tabindex="0">${esc(typeof config==='string'?config:JSON.stringify(config,null,2))}</pre>`:'<p class="warning-note">服务尚未返回此工具的本机 MCP 配置。请检查发行包中的 tools/aihub_mcp.py 与接入文档，不要使用其他电脑的绝对路径。</p>';
        if(selected)get('mcp').innerHTML+=`<p>${esc(selected.verified?'已验证此接入方式':'需要在对应工具中验证')} · ${esc(selected.instructions)}</p>`;
        get('mcp').innerHTML+=(state.limitations || []).map(v=>`<p class="caption-note">${esc(v)}</p>`).join('');
      }
      async function refresh() {
        const next=await api('/api/collaboration/status');if(!active())return;
        state=next || {};ready=state.available===true;
        el.dataset.coRoot=state.root || '';
        get('root').textContent=state.root || '尚未配置工作环境';
        get('availability').innerHTML=ready?'<p class="caption-note">统一协议 v'+esc(state.protocol_version || 1)+' · 仅展示最近记录</p>':'<p class="warning-note">工作区不可用。请到「工作环境」配置可访问的规范根目录。协作写入已停用。</p><a class="btn" href="#/workspace">配置工作环境</a>';
        get('tasks').innerHTML=tasksHTML(state.tasks);get('artifacts').innerHTML=artifactsHTML(state.artifacts);get('memories').innerHTML=memoriesHTML(state.memories);
        const previous=get('memory-source').value;
        get('memory-source').innerHTML='<option value="">选择已登记的报告产物</option>'+rows(state.artifacts).filter(a=>a.kind==='report' && a.status==='active').map(a=>`<option value="${esc(a.id)}">${esc(a.title || a.path)} · ${esc(a.id)}</option>`).join('');
        get('memory-source').value=previous;
        if(!policyDirty){get('policy-enabled').checked=state.policy?.enabled===true;get('policy-days').value=String(state.policy?.days ?? 7);}
        const lastRun=state.policy?.last_run;
        get('policy-history').textContent=lastRun?.finished_at ? '最近检查：'+timeText(lastRun.finished_at)+' · 已回收 '+(lastRun.recycled ?? 0)+' 项'+(rows(lastRun.errors).length?' · '+rows(lastRun.errors).length+' 项未完成，原文件保留':'') : '尚无自动或手动回收记录。';
        renderConfig();
        get('source-candidates').innerHTML='<h4>可登记的来源建议</h4><p class="caption-note">选择后填入上方表单；提交登记后再点击只读盘点。未登记的来源不会扫描。</p>'+rows(state.source_candidates).map((s,i)=>`<p><span class="pathline">${esc(s.label)} · ${esc(s.path)}</span> ${button('source-fill',i,'填入来源表单')}</p>`).join('');
        if(!get('source-results').innerHTML && state.inventory)get('source-results').innerHTML=inventoryHTML(state.inventory);
        get('clients').innerHTML=table(['客户端 / 工具','协议','最近心跳'],rows(state.clients).map(c=>`<tr><td>${esc(c.name || c.id)} · ${esc(c.tool)}<span class="file-sub">${esc(c.id)}</span></td><td>${esc(c.protocol_version)}</td><td>${esc(timeText(c.last_seen))}</td></tr>`),'尚无协议心跳。发现应用安装不代表已连接。');
        taskDetail(el.dataset.coTask);requeueId=null;get('requeue-confirm').innerHTML='';previewReady=false;get('preview-results').innerHTML='';get('run').disabled=true;
        bindActions();
        if(ready){try{const result=await post('source_list');if(active()){get('sources').innerHTML=sourcesHTML(result);bindActions();}}catch(e){if(active()){get('sources').innerHTML=`<p class="dialog-error">来源列表读取失败：${esc(e.message)}</p>`;}}}
      }
      function form(id, action, payload, clear=[]) {
        get(id+'-form').onsubmit=event=>{event.preventDefault();return act(async()=>{
          if(!ready)throw new Error('工作区不可用，请先配置工作环境。');
          const body=payload();await post(action,body);if(!active())return;
          clear.forEach(key=>{get(key).value='';});await refresh();
        });};
      }
      const value=id=>get(id).value.trim();
      form('task','task_create',()=>({project:value('task-project'),title:value('task-title'),description:value('task-description'),target_tool:value('task-tool')}),['task-title','task-description']);
      form('memory','memory_propose',()=>{
        const scope=value('memory-scope'),project=value('memory-project');
        if(scope==='project' && !project)throw new Error('项目范围的记忆需要填写项目名称。');
        return {title:value('memory-title'),content:value('memory-content'),scope,project:scope==='project'?project:'',source_artifact_id:value('memory-source')};
      },['memory-title','memory-content']);
      form('source','source_add',()=>({path:value('source-path'),label:value('source-label'),tool:value('source-tool')}),['source-path','source-label']);
      get('search-form').onsubmit=event=>{event.preventDefault();return act(async()=>{const result=await post('memory_search',{query:value('search-query'),project:value('search-project')});if(active())get('search-results').innerHTML=memoriesHTML(result,false);});};
      get('policy-form').onsubmit=event=>{event.preventDefault();return act(async()=>{
        const days=Number(value('policy-days'));if(!Number.isInteger(days) || days<1 || days>365)throw new Error('保留天数应为 1 到 365 的整数。');
        await post('retention_policy',{enabled:get('policy-enabled').checked,days});if(!active())return;policyDirty=false;await refresh();
      });};
      get('policy-days').oninput=get('policy-enabled').onchange=()=>{policyDirty=true;previewReady=false;get('run').disabled=true;};
      get('preview').onclick=()=>act(async()=>{const result=await post('retention_preview');if(!active())return;get('preview-results').innerHTML=previewHTML(result);previewReady=rows(result).length>0;});
      get('run').onclick=()=>act(async()=>{
        if(!previewReady)throw new Error('请先预览待回收文件。');previewReady=false;
        const result=await post('retention_run');if(!active())return;
        get('run-results').innerHTML=`<h4>本次回收结果</h4><p>检查 ${esc(result.checked ?? rows(result).length)} 项，已回收 ${esc(result.recycled ?? 0)} 项。</p>${table(['文件','处理结果'],rows(result).map(r=>`<tr><td class="pathline">${esc(r.path)}</td><td>${esc(label(r.status))}<span class="file-sub">${esc(r.error || r.reason || '')}</span></td></tr>`),'本次没有回收文件。')}${rows(result.errors).map(e=>`<p class="dialog-error">${esc(typeof e==='string'?e:e.error || e.reason)}</p>`).join('')}`;
        await refresh();
      });
      get('refresh').onclick=()=>act(refresh);
      get('mcp-tool').onchange=renderConfig;
      el.querySelectorAll('[data-co-tab]').forEach(n=>{n.onclick=()=>selectTab(n.dataset.coTab);});
      selectTab(restored?.tab);restore(el,restored);el.dataset.coTask=restored?.selectedTask || '';
      await act(refresh);
      if(active() && restored?.root && restored.root!==state.root){setError('工作环境已切换。已保留文字草稿，请核对项目与来源；原报告选择已清除。');get('memory-source').value='';}
      else if(active() && restored?.draft?.['memory-source'])get('memory-source').value=restored.draft['memory-source'];
    };
  }
  return {createPage,capture,restore,esc,rows,tasksHTML,artifactsHTML,memoriesHTML,sourcesHTML,previewHTML,inventoryHTML};
});
