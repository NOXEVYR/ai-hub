/* File ownership and reviewable lifecycle candidates. No cleanup command. */
((root, factory) => {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.AIHubFileLifecycle = factory();
})(globalThis, () => {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const kinds = {project:'项目',task:'任务',tool:'工具',artifact:'产物'};
  const states = {active:'进行中 · 保护',retained:'保留',completed:'已完成',expired:'已到期',archived:'已归档',unregistered:'未登记'};
  const purposes = {unknown:'用途不明',source:'源码',work:'作品',input:'输入',output:'输出',delivery:'正式交付',cache:'可重建缓存',temporary:'临时验证',configuration:'配置 / 数据库',model:'模型',archive:'存档'};
  const categories = {source:'源码',work:'文稿 / 媒体 / 作品',model:'模型',configuration:'配置 / 数据库',archive:'压缩归档',temporary_hint:'临时文件线索',unknown:'类型待确认'};
  const reasons = {source_protected:'源码保留（唯一副本未核实）',work_protected:'作品与文稿保留',model_protected:'模型保留',archive_protected:'存档与源码包保留',configuration_protected:'配置与数据库保留',protected_path:'受保护路径',repository_source_protected:'Git 源码树保留',uncommitted_or_unverified_repository:'未提交修改或状态未核实',unpushed_or_unverified_repository:'未推送提交或上游未核实',registered_protection:'登记保护',active_task_or_owner:'活动任务或所属项目',active_collaboration_task:'已有协作任务尚未完成',activity_ledger_unavailable:'活动任务状态不可核实',registration_identity_changed:'登记后文件身份已变化',shared_hardlink:'共享硬链接',unknown_purpose_or_incomplete_lifecycle:'用途或生命周期证据不足',reparse_point_skipped:'联接 / 符号链接已跳过',protected_directory_skipped:'受保护目录未展开',unavailable_or_changed:'访问失败或扫描时已变化',special_file_skipped:'特殊文件已跳过'};
  const decisions = {candidate:'待人工复核',protected:'保护',skip:'跳过'};
  const stateByElement = new WeakMap();
  function size(value) {
    if (value === null || value === undefined) return '未测量';
    let number = Math.max(0, Number(value) || 0), unit = 0;
    while (number >= 1024 && unit < 4) { number /= 1024; unit++; }
    return `${unit ? number.toFixed(1) : number} ${['B','KiB','MiB','GiB','TiB'][unit]}`;
  }
  function summary(result) {
    const s = result.summary;
    return `<div class="fl-metrics" aria-label="空间统计"><article><small>已盘点文件</small><b>${s.files.toLocaleString()}</b><span>${result.examined_entries} 个条目 · ${result.truncated?'达到上限，仅为部分结果':'本次范围已完成'}</span></article><article><small>逻辑大小</small><b>${size(s.logical_bytes)}</b><span>按文件长度统计</span></article><article><small>已测实际分配</small><b>${size(s.allocated_bytes_known)}</b><span>按文件身份去重 · ${s.allocation_unknown_files} 个未测量</span></article><article><small>候选预计释放</small><b>${size(s.estimated_releasable_bytes)}</b><span>${s.candidate_files} 个待复核 · 实际释放 ${size(s.actual_released_bytes)}</span></article></div>`;
  }
  function rows(items) {
    if (!items.length) return '<p class="muted">当前筛选没有文件。</p>';
    const groups = new Map();
    for (const item of items) {
      const parts = item.relative_path.split(/[\\/]/); parts.pop();
      const folder = parts.join(' / ') || '所选目录';
      if (!groups.has(folder)) groups.set(folder, []);
      groups.get(folder).push(item);
    }
    return [...groups].map(([folder, files]) => `<details class="fl-folder" open><summary>${esc(folder)} <span class="muted">${files.length} 个文件</span></summary><div class="table-wrap"><table><thead><tr><th>文件与归属</th><th>类型 / 生命周期</th><th>大小</th><th>预览结论</th></tr></thead><tbody>${files.map(item => `<tr><td><b>${esc(item.relative_path.split(/[\\/]/).pop())}</b><small class="fl-path">${esc(item.path)}</small><a href="#/traces?file_exact=${esc(encodeURIComponent(item.path))}">反查关联会话</a><small>项目 ${esc(item.project_id || '未登记')} · 任务 ${esc(item.task_id || '未登记')} · 工具 ${esc(item.tool_id || '未登记')}</small></td><td>${esc(categories[item.category] || item.category)}<small>${esc(purposes[item.purpose] || item.purpose)} · ${esc(states[item.lifecycle] || item.lifecycle)}</small><small>${item.age_days} 天 · ${item.link_count} 个链接</small></td><td>${size(item.logical_bytes)}<small>分配 ${size(item.allocated_bytes)}</small></td><td><span class="fl-badge ${esc(item.decision)}">${decisions[item.decision]}</span><small>${item.reasons.map(r => esc(reasons[r] || r)).join('；') || '已登记到期临时文件，执行前仍须重新核验'}</small></td></tr>`).join('')}</tbody></table></div></details>`).join('');
  }
  function capture(el) {
    const state = stateByElement.get(el);
    if (!state) return {};
    const form=el.querySelector('#fl-register');
    const draft=form ? {...Object.fromEntries(new FormData(form)),protected:form.elements.protected.checked} : state.draft;
    return {...state, draft, scope:el.querySelector('#fl-scope')?.value || state.scope,
      query:el.querySelector('#fl-query')?.value || '', filter:el.querySelector('#fl-filter')?.value || ''};
  }
  function createPage({api, heading}) {
    return async (el, params, restored={}) => {
      const ledger = await api('/api/file-lifecycle');
      if (!el.isConnected) return;
      const usable = restored.root === ledger.root ? restored : {};
      const state = {...usable, root:ledger.root, scope:usable.scope || params?.get?.('scope') || ledger.root};
      stateByElement.set(el, state);
      const options = (values, chosen) => Object.entries(values).map(([key,label]) => `<option value="${esc(key)}" ${key===chosen?'selected':''}>${esc(label)}</option>`).join('');
      const links = kind => '<option value="">尚未关联</option>' + ledger.records.filter(r=>r.kind===kind).map(r=>`<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.id)}</option>`).join('');
      el.innerHTML = heading('文件台账与生命周期','从文件到所属项目、任务和工具，先看保护原因，再审阅到期候选。','FILE LIFECYCLE') + `
        ${ledger.review_context ? `<p class="fl-review-context">${esc(ledger.review_context)}</p>` : ''}<section class="panel fl-intro"><div><span class="fl-badge protected">只读候选预览</span><h3>每个文件，都有去向与保留依据</h3><p>源码、作品、模型、配置、存档及活动任务优先保留；用途不明跳过。当前版本不执行删除、迁移或定时清理。</p></div><div class="fl-note">实际释放 <b>0 B</b><small>空间变化未测量</small></div></section>
        <section class="panel"><div class="body"><form id="fl-scan"><label for="fl-scope">预览范围（当前工作区内）</label><div class="fl-scanline"><input id="fl-scope" value="${esc(state.scope)}" required><button class="btn primary" type="submit">递归预览文件</button></div><small class="muted">逐层读取文件元数据；单次最多 5,000 个条目，超过上限可缩小目录。不会读取文件正文。</small></form><p id="fl-status" role="status" aria-live="polite">选择目录后开始；不会自动启动扫描。</p></div></section>
        <div id="fl-summary"></div><section class="panel"><div class="body"><div class="filterbar"><input id="fl-query" type="search" aria-label="筛选路径与归属" placeholder="筛选文件名、项目、任务、工具…" value="${esc(state.query || '')}"><select id="fl-filter" aria-label="筛选预览结论">${options({'':'全部结论',protected:'保护',skip:'跳过',candidate:'待人工复核'},state.filter || '')}</select><span id="fl-count" class="muted"></span></div><div id="fl-files"><p class="muted">等待首次预览。</p></div><div id="fl-pager" class="pager"></div><div id="fl-issues"></div></div></section>
        <section class="panel"><div class="panel-head"><h3>登记项目 / 任务 / 工具 / 产物</h3><span class="muted">${ledger.records.length} 条登记</span></div><div class="body"><p class="muted">先登记项目、任务和工具，再关联产物。登记只保存元数据，现有文件保持原位。临时类型或到期时间本身不授予删除权限。</p><form id="fl-register" class="fl-form">
        <label>类型<select name="kind">${options(kinds,'project')}</select></label><label>稳定 ID<input name="id" required pattern="[A-Za-z0-9][A-Za-z0-9_-]{0,79}" placeholder="project-demo"></label><label>名称<input name="name" required maxlength="200"></label><label class="fl-wide">文件或目录路径<input name="path" required placeholder="${esc(ledger.root)}"></label><label>用途<select name="purpose">${options(purposes,'unknown')}</select></label><label>生命周期<select name="state">${options(states,'active').replace(/<option value="unregistered"[^>]*>[^<]*<\/option>/,'')}</select></label><label>所属项目<select name="project_id">${links('project')}</select></label><label>所属任务<select name="task_id">${links('task')}</select></label><label>来源工具<select name="tool_id">${links('tool')}</select></label><label>到期时间（可选）<input name="expires_at" placeholder="2026-10-15T12:00:00+00:00"></label><label class="fl-check"><input name="protected" type="checkbox">额外保护</label><div class="fl-wide"><button class="btn" type="submit">预览登记</button></div></form><div id="fl-registration-preview" aria-live="polite"></div><details><summary>现有登记</summary><ul>${ledger.records.map(r=>`<li><b>${esc(r.name)}</b> · ${kinds[r.kind]} · ${esc(r.id)} · ${states[r.state]}<small class="fl-path">${esc(r.path)}</small></li>`).join('') || '<li>尚无登记</li>'}</ul></details></div></section>`;
      const status = el.querySelector('#fl-status');
      let request = 0, page = 0;
      function render() {
        if (!state.result) return;
        const result = state.result;
        el.querySelector('#fl-summary').innerHTML = `<p class="fl-result-context">结果范围：${esc(result.scope)}<br>快照时间：${esc(result.captured_at || '未记录')} · ${state.stale ? '旧快照；当前范围、登记或请求已变化，请重新预览。' : '仅代表本次采集时刻。'}</p>` + summary(result);
        const query = el.querySelector('#fl-query').value.toLocaleLowerCase();
        const filter = el.querySelector('#fl-filter').value;
        const selected = result.items.filter(r=>(!filter || r.decision===filter) && (!query || [r.path,r.project_id,r.task_id,r.tool_id].join(' ').toLocaleLowerCase().includes(query)));
        page = Math.min(page, Math.max(0, Math.ceil(selected.length/100)-1));
        el.querySelector('#fl-count').textContent = `筛选 ${selected.length} 个文件`;
        el.querySelector('#fl-files').innerHTML = rows(selected.slice(page*100,(page+1)*100));
        el.querySelector('#fl-pager').innerHTML = `<button class="btn small" id="fl-prev" ${page?'':'disabled'}>上一页</button><span>第 ${page+1} / ${Math.max(1,Math.ceil(selected.length/100))} 页</span><button class="btn small" id="fl-next" ${(page+1)*100<selected.length?'':'disabled'}>下一页</button>`;
        el.querySelector('#fl-prev').onclick=()=>{page--;render();};el.querySelector('#fl-next').onclick=()=>{page++;render();};
        el.querySelector('#fl-issues').innerHTML = result.issues.length?`<details><summary>跳过或不可访问的目录与条目（${result.issues.length}）</summary><ul>${result.issues.map(r=>`<li>${esc(reasons[r.reason] || r.reason)}<small class="fl-path">${esc(r.path)}</small></li>`).join('')}</ul></details>`:'';
      }
      el.querySelector('#fl-scope').oninput=()=>{state.stale=true;render();status.textContent='预览范围已修改；下方仍为标注范围的旧快照。';};
      el.querySelector('#fl-query').oninput=()=>{page=0;render();};el.querySelector('#fl-filter').onchange=()=>{page=0;render();};
      el.querySelector('#fl-scan').onsubmit=async event=>{
        event.preventDefault();const current=++request;const button=event.submitter;button.disabled=true;state.stale=true;render();status.textContent='正在读取文件元数据… 下方如有结果，仍为旧快照。';
        try {
          const result=await api('/api/file-lifecycle/inventory',{body:{_workspace_root:ledger.root,path:el.querySelector('#fl-scope').value,max_entries:5000}});
          if (current!==request || !el.isConnected) return;
          state.result=result;state.scope=result.scope;state.stale=el.querySelector('#fl-scope').value!==result.scope;page=0;render();status.textContent=result.truncated?'达到本次条目或时间上限；结果不完整，请选择更小目录继续。':result.message;
        } catch(error){if(el.isConnected)status.textContent=`预览失败：${error.message} 下方保留标注范围的旧快照，未代表当前输入范围。`;} finally {button.disabled=false;}
      };
      const form=el.querySelector('#fl-register'), preview=el.querySelector('#fl-registration-preview');
      if(state.draft){for(const [key,value] of Object.entries(state.draft)){const field=form.elements.namedItem(key);if(field){if(field.type==='checkbox')field.checked=!!value;else field.value=value;}}}
      let token=null, registrationVersion=0;
      form.oninput=()=>{token=null;registrationVersion++;preview.replaceChildren();};
      form.onsubmit=async event=>{
        event.preventDefault();token=null;const version=++registrationVersion;
        const record=Object.fromEntries(new FormData(form));record.protected=form.elements.protected.checked;
        try {
          const result=await api('/api/file-lifecycle/registration-preview',{body:{_workspace_root:ledger.root,record}});
          if(!el.isConnected || version!==registrationVersion)return;token=result.token;
          preview.innerHTML=`<div class="fl-confirm"><h4>${result.before?'更新':'新建'}登记：${esc(result.after.name)}</h4><p>${esc(result.message)}</p><p>${kinds[result.after.kind]} · ${purposes[result.after.purpose]} · ${states[result.after.state]}<small class="fl-path">${esc(result.after.path)}</small></p><p>项目：${esc(result.after.project_id || '未关联')} · 任务：${esc(result.after.task_id || '未关联')} · 工具：${esc(result.after.tool_id || '未关联')}<br>到期：${esc(result.after.expires_at || '未设置')} · 额外保护：${result.after.protected ? '开启' : '未开启'}</p><button class="btn primary" id="fl-save">确认保存登记</button></div>`;
          el.querySelector('#fl-save').onclick=async()=>{
            if(!token)return;const savedToken=token;token=null;
            try {await api('/api/file-lifecycle/registration-save',{body:{_workspace_root:ledger.root,token:savedToken}});state.stale=true;if(el.isConnected)await createPage({api,heading})(el,params,capture(el));}
            catch(error){preview.textContent=`保存未完成：${error.message} 请重新预览。`;}
          };
        } catch(error){preview.textContent=`登记预览失败：${error.message}`;}
      };
      render();
    };
  }
  return {createPage,capture,summary,rows,size};
});
