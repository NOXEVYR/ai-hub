/* Workspace setup and explicit source registration. No filesystem operations in the browser. */
(function(root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AIHubWorkspace = api;
})(globalThis, function() {
  'use strict';
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const lines = text => [...new Set(String(text || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean))];
  const HEALTH = {ok:'可访问',junction:'兼容联接 · 请使用规范路径',missing:'目录不存在',outside:'工作区以外',rejected:'不可使用'};
  const ACTION = {create:'将创建',keep:'保留',conflict:'存在冲突'};
  const TOOL_NAMES = Object.create(null);
  const describe = value => Array.isArray(value) ? value.map(describe).join('；') : value && typeof value === 'object' ? Object.entries(value).map(([key,item])=>key+'：'+describe(item)).join('；') : typeof value === 'boolean' ? value ? '已确认' : '未确认' : String(value ?? '尚未确认');
  function toolsHTML(tools) {
    const rows = Array.isArray(tools) ? tools : [];
    return `<section class="panel ws-summary"><div class="panel-head"><h3>AI 工具接入情况</h3></div><div class="body"><p class="caption-note">这里显示安装探测与规则支持情况。发现工具不代表已连接、已启动或已隔离。</p>${rows.length ? `<div class="table-wrap"><table class="tbl ws-health-table"><thead><tr><th>工具与探测</th><th>入口与使用方式</th><th>规则支持与约束</th></tr></thead><tbody>${rows.map(tool => `<tr><td><b>${esc(tool.name || TOOL_NAMES[tool.id] || tool.id || '未知工具')}</b>${tool.enabled===false?'<span class="badge">已停用</span>':''}<span class="file-sub">${tool.detected === true ? '已发现安装' : tool.detected === false ? '尚未发现安装' : '安装状态未确认'} · ${tool.available === true ? '入口可用' : tool.available === false ? '入口不可用' : '入口状态未确认'}</span></td><td>${esc(describe(tool.launch_mode))}<span class="file-sub pathline">${esc(tool.executable || '未记录可执行入口')}</span></td><td>规则支持：${esc(describe(tool.rules_support))}<span class="file-sub">约束方式：${esc(describe(tool.enforcement))}</span><span class="file-sub">${esc(describe(tool.notes || ''))}</span></td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">尚无工具探测记录。可以准备通用交接文本；具体工具规则以执行预览为准。</p>'}</div></section>`;
  }
  function projectHTML(tools) {
    const choices = Array.isArray(tools) ? tools.filter(t=>typeof t.id === 'string' && t.id && t.enabled!==false) : [];
    return `<section class="panel ws-summary"><div class="panel-head"><h3>创建项目工作区</h3></div><div class="body"><p>在已保存的工作环境中创建项目目录、通用任务交接文本和已核实支持的工具规则。此操作不会启动 AI 工具。</p><form id="ws-project-form"><fieldset id="ws-project-fields" class="ws-fields"><label class="ws-field">项目名称<input id="ws-project-name" required maxlength="80" autocomplete="off" placeholder="例如 短片分镜_第一版"></label><p>需要交接给哪些工具</p>${choices.length?'':'<p class="muted">尚无已启用的登记工作端。请先在“协作与记忆 → 工作端接入”保存登记，再创建项目交接；可选模板不会自动加入。</p>'}<div class="category-checks">${choices.map(tool=>`<label><input type="checkbox" data-ws-project-tool="${esc(tool.id)}"><span>${esc(tool.name || TOOL_NAMES[tool.id] || tool.id)}</span></label>`).join('')}</div><p class="caption-note">默认选择已启用的工作端，至少保留一个交接工具。可在“工作端接入中心”添加或编辑。工具专属规则是否生成，以后端检查和下方预览为准；未核实的支持能力不会被当成已接入。</p><button id="ws-project-preview-button" class="btn primary" type="submit">预览项目目录与规则</button></fieldset></form><p id="ws-project-error" class="dialog-error" role="alert"></p><div id="ws-project-preview" class="ws-preview" aria-live="polite"></div><div id="ws-project-result" aria-live="polite"></div></div></section>`;
  }
  function projectPreviewHTML(plan) {
    const list = (rows,title) => `<h4>${title}</h4><ul class="ws-plan-list">${(rows || []).map(row=>`<li><span class="badge">${esc(ACTION[row.action] || row.action || '将创建')}</span><span class="pathline">${esc(row.path || row)}</span></li>`).join('')}</ul>`;
    return `<h3>项目创建预览</h3><p class="pathline">${esc(plan.root)}</p>${(plan.errors || []).map(s=>`<p class="dialog-error">${esc(s)}</p>`).join('')}${(plan.warnings || []).map(s=>`<p class="warning-note">${esc(s)}</p>`).join('')}${list(plan.directories,'项目目录')}${list(plan.files,'任务交接与工具规则')}<label class="ws-confirm"><input id="ws-project-confirm" type="checkbox">我已核对项目位置、交接文本和规则文件</label><button id="ws-project-apply" class="btn primary" type="button" disabled>确认创建项目工作区</button><p class="caption-note">目录或配置变化后需重新预览。创建文件不表示工具已经连接或获得系统隔离。</p>`;
  }
  function bindProject(el,{api,active,isWorkspaceBusy,workspaceReady,onSaved,setBusy,restored}) {
    const get = id => el.querySelector('#ws-project-' + id);
    const choices = () => [...el.querySelectorAll('[data-ws-project-tool]')];
    let serial=0, plan=null, busy=false;
    if (restored?.project) {
      get('name').value=restored.project.name || '';
      choices().forEach(c=>{c.checked=(restored.project.tools || []).includes(c.dataset.wsProjectTool);});
    } else choices().forEach(c=>{c.checked=true;});
    const invalidate=()=>{serial++;plan=null;get('preview').innerHTML='<p class="caption-note">项目或工作环境已变化，请重新预览。</p>';};
    get('name').oninput=invalidate;choices().forEach(c=>{c.onchange=invalidate;});
    get('form').onsubmit=async event=>{
      event.preventDefault();if(busy || isWorkspaceBusy())return;
      get('error').textContent='';
      if(!workspaceReady()){invalidate();get('error').textContent='请先保存可访问的工作环境，再创建项目。项目使用已保存的根目录。';return;}
      const request=++serial;plan=null;get('preview').innerHTML='<p role="status">正在预览项目目录与交接规则…</p>';
      const body={name:get('name').value.trim(),tools:choices().filter(c=>c.checked).map(c=>c.dataset.wsProjectTool)};
      if(!body.tools.length){get('preview').innerHTML='';get('error').textContent='请至少选择一个交接工具。';return;}
      try {
        const result=await api('/api/workspace/project/preview',{body});
        if(!active() || request!==serial)return;
        plan=result;get('preview').innerHTML=projectPreviewHTML(result);
        get('confirm').onchange=()=>{get('apply').disabled=!get('confirm').checked || !plan?.can_apply || !plan?.token || busy;};
        get('apply').onclick=async()=>{
          if(busy || isWorkspaceBusy() || !workspaceReady() || request!==serial || !plan?.token || !plan?.can_apply || !get('confirm').checked)return;
          busy=true;get('apply').disabled=true;get('fields').disabled=true;setBusy(true);
          try {
            const saved=await api('/api/workspace/project/apply',{body:{token:plan.token}});
            if(!active())return;
            if(!saved.applied)throw new Error('服务未确认项目创建成功，请刷新体检。');
            plan=null;get('preview').innerHTML='';
            get('result').innerHTML=`<div class="ws-success"><h3>项目工作区已创建</h3><p class="pathline">${esc(saved.root)}</p><p>通用任务交接文本：<span class="pathline">${esc(saved.prompt_path || '服务未返回交接文本路径，请检查项目目录')}</span></p><p>${saved.output_root_added ? '项目输出已加入图库来源；开始索引后可查看。' : '服务未确认新增图库来源；可在上方明确登记出图目录。'}</p><p>请在所选工具中打开这个项目目录，读取并使用交接文本。规则文件已生成，不代表工具已连接、自动执行或已隔离。</p><a class="btn" href="#/projects">查看项目与运行</a></div>`;
            onSaved(saved);
            const selected=(saved.tools || []).map(tool=>typeof tool==='string' ? TOOL_NAMES[tool] || tool : tool.name || TOOL_NAMES[tool.id] || tool.id || '未知工具');
            if(selected.length)get('result').innerHTML+=`<p class="caption-note">交接目标：${esc(selected.join('、'))}。实际规则支持以上方探测与生成文件为准。</p>`;
          }catch(error){if(active()){invalidate();get('error').textContent=error.message;}}
          finally{if(active()){busy=false;get('fields').disabled=false;setBusy(false);}}
        };
      }catch(error){if(active() && request===serial){get('error').textContent=error.message;get('preview').innerHTML='';}}
    };
    return {invalidate,isBusy:()=>busy};
  }
  function capture(el) {
    const value = id => el?.querySelector('#ws-' + id)?.value || '';
    const snapshot={mode:value('mode'),root:value('root'),scan:value('scan'),output:value('output'),draft:true};
    const projectName=value('project-name');
    const projectTools=[...(el?.querySelectorAll('[data-ws-project-tool]') || [])].filter(c=>c.checked).map(c=>c.dataset.wsProjectTool);
    const toolChoices=[...(el?.querySelectorAll('[data-ws-project-tool]') || [])];
    if(projectName || toolChoices.some(c=>!c.checked))snapshot.project={name:projectName,tools:projectTools};
    return snapshot;
  }
  function healthHTML(rows) {
    if (!rows?.length) return '<p class="muted">尚未登记来源。请在下方指定目录并预览。</p>';
    return `<div class="table-wrap"><table class="tbl ws-health-table"><thead><tr><th>来源用途</th><th>目录</th><th>检查结果</th></tr></thead><tbody>${rows.map(r => `<tr><td>${r.kind === 'output' ? '图库出图' : '资产扫描'}</td><td><span class="pathline">${esc(r.path)}</span>${r.canonical_path ? `<span class="file-sub">规范路径：${esc(r.canonical_path)}</span>` : ''}</td><td><span class="badge ${r.status === 'ok' ? 'b-green' : 'b-yellow'}">${esc(HEALTH[r.status] || '待检查')}</span><span class="file-sub">${esc(r.reason)}</span></td></tr>`).join('')}</tbody></table></div>`;
  }
  function banner() {
    return '<section class="setup-banner ws-overview-entry"><div><h3>工作环境 · 明确模型和出图从哪里来</h3><p>新建规范工作区，或接入现有目录；登记图库来源，让训练验证图也能进入索引。</p></div><a class="btn primary" href="#/workspace">管理工作环境 →</a></section>';
  }
  function previewHTML(plan) {
    const list = (items, title) => `<h4>${title}</h4>${items?.length ? `<ul class="ws-plan-list">${items.map(r => `<li><span class="badge ${r.action === 'conflict' ? 'b-yellow' : ''}">${esc(ACTION[r.action] || r.action)}</span><span class="pathline">${esc(r.path)}</span></li>`).join('')}</ul>` : '<p class="muted">无需创建。</p>'}`;
    return `<h3>执行预览</h3><p>请核对目录、规则文件与最终来源。确认后才会写入。</p>${(plan.errors || []).map(s => `<p class="dialog-error">${esc(s)}</p>`).join('')}${(plan.warnings || []).map(s => `<p class="warning-note">${esc(s)}</p>`).join('')}${list(plan.directories, '目录')}${list(plan.files, '工作规则与登记文件')}<h4>保存后使用的来源</h4>${healthHTML(plan.source_health)}<div class="ws-source-preview"><b>资产扫描</b>${(plan.sources?.scan_roots || []).map(p => `<p class="pathline">${esc(p)}</p>`).join('') || '<p>未设置；暂不扫描资产。</p>'}<b>图库出图</b>${(plan.sources?.output_roots || []).map(p => `<p class="pathline">${esc(p)}</p>`).join('') || '<p>未设置；暂不索引图片。</p>'}</div><label class="ws-confirm"><input id="ws-confirm" type="checkbox">我已核对将创建的目录、规则文件和来源变更</label><button type="button" class="btn primary" id="ws-apply" disabled>确认并应用</button><p class="caption-note">预览过期、目录或配置发生变化时，请重新预览。</p>`;
  }
  function createPage({api, heading, toast, pollJobs}) {
    let renderSerial = 0;
    return async function page(el, params, restored) {
      const render = ++renderSerial;
      const active = () => el.isConnected && render === renderSerial;
      el.innerHTML = '<p class="muted" role="status">正在读取工作环境与来源状态…</p>';
      let status, overview, jobs;
      try {
        [status, overview, jobs] = await Promise.all([
          api('/api/workspace/status'),
          api('/api/overview').catch(() => ({})),
          api('/api/jobs').catch(() => ({jobs:[]})),
        ]);
      } catch (error) {
        if (active()) {
          el.innerHTML = `<div class="page-error"><h3>无法读取工作环境</h3><p>${esc(error.message)}</p><button id="ws-retry" class="btn">重试</button></div>`;
          el.querySelector('#ws-retry').onclick = () => page(el, params, restored);
        }
        return;
      }
      if (!active()) return;
      const get = id => el.querySelector('#ws-' + id);
      const sources = status.sources || {scan_roots:(status.source_health || []).filter(r => r.kind === 'scan').map(r => r.path),output_roots:(status.source_health || []).filter(r => r.kind === 'output').map(r => r.path)};
      const state = restored?.draft ? restored : {mode:status.configured ? 'connect' : 'create',root:status.root || '',scan:(sources.scan_roots || []).join('\n'),output:(sources.output_roots || []).join('\n')};
      el.innerHTML = (heading ? heading('工作环境','指定工作区、资产与图库来源，为其他 AI 提供清楚的工作目录。','WORKSPACE SETUP') : '<h2>工作环境</h2>') + `
        <section class="panel ws-summary"><div class="panel-head"><h3 id="ws-current-title">${status.configured ? '当前工作环境' : '尚未配置工作环境'}</h3><button id="ws-refresh" class="btn small" type="button">刷新体检</button></div><div class="body"><p id="ws-current-root" class="pathline">${esc(status.root || '选择这台电脑上的可写目录')}</p><p><span id="ws-current-availability">${status.available ? '工作区可访问' : '工作区尚不可访问，请检查路径'}</span> · 最近扫描：<span id="ws-last-scan">${esc(overview.scan_at || '尚未扫描')}</span></p><p id="ws-scan-state" role="status" aria-live="polite"></p><div id="ws-current-health">${healthHTML(status.source_health)}</div><div class="ws-rule-note"><b>工作区规则与来源检查</b><p>Hub 生成项目与输出目录的工作规则，供其他 AI 读取并遵守。未启用系统隔离或全盘写入监控，不能强制阻止其他程序写到目录之外。</p><p>模型文件保持原位置；来源登记后，图库仅索引你明确列出的出图目录。</p></div></div></section>
        <section class="panel"><div class="panel-head"><h3>设置工作区与来源</h3><span class="caption-note">先预览，再应用</span></div><div class="body"><form id="ws-form"><fieldset id="ws-fields" class="ws-fields"><div class="ws-form-grid"><label class="ws-field">设置方式<select id="ws-mode"><option value="create">新建规范工作区</option><option value="connect">接入 / 编辑已有工作区</option></select></label><label class="ws-field">工作区根目录<input id="ws-root" required autocomplete="off" spellcheck="false" placeholder="例如 D:\\CreativeWorkspace"></label><p id="ws-mode-hint" class="caption-note ws-wide"></p><label class="ws-field">资产扫描目录（模型、工作流等）<textarea id="ws-scan" rows="5" spellcheck="false" placeholder="每行一个完整目录路径"></textarea><small>保留已有来源，新增路径另起一行。联接目录请使用体检给出的规范路径。</small></label><label class="ws-field">图库出图目录<textarea id="ws-output" rows="5" spellcheck="false" placeholder="每行一个完整目录路径，包括需要收录的 verify_out"></textarea><small>不局限于 70_Output。训练项目的 verify_out、output 或 samples 可明确加入；不要填训练数据集。</small></label></div><div class="ws-discovery"><h4>发现的候选出图目录</h4><p class="caption-note">这些只是建议；点击加入表单后，仍需预览并应用才会纳入图库。</p><div id="ws-discovery"></div>${status.discovery?.truncated ? '<p class="caption-note">候选发现达到检查上限，其他目录可在上方手动填写。</p>' : ''}</div><div class="ws-rule-note"><h4>其他 AI 应当把文件放在哪里</h4><p>创作项目使用 40_Projects/&lt;项目&gt;/Inputs、Work、Outputs、Deliverables，分别存放输入、过程、运行输出与最终交付。70_Output 仅作为通用工具的出图落地区；正式交付以项目登记位置为准。LoRA 训练继续保留 50_Training/Projects 下的既有布局，通过明确登记 verify_out、samples 等来源加入图库，无需搬到项目 Outputs。</p><p>工作规则位于 00_Management/AIHub/WORKSPACE.md，清单位于同目录 workspace.json。接入已有目录时只生成 Hub 专属规则，不重写用户现有 AGENTS.md；新工作区没有 AGENTS.md 时，预览会列出引用规则的入口文件。</p><p>项目启动任务模板位于 00_Management/AIHub/Templates/PROJECT_TASK.md，可复制其中的目录约定交给其他 AI。模板控制项目内部的文件布局；它不会自动改变其他 AI 的工作目录。请将任务工作目录设为对应项目，并让 AI 先读取 Hub 工作规则。</p></div><button class="btn primary" type="submit" id="ws-preview-button">检查来源并预览</button></fieldset></form><p id="ws-error" class="dialog-error" role="alert"></p><div id="ws-preview" class="ws-preview" aria-live="polite"></div><div id="ws-result" aria-live="polite"></div></div></section>`;
      for(const tool of status.tools||[])TOOL_NAMES[tool.id]=tool.name||tool.id;
      el.innerHTML += toolsHTML(status.tools) + projectHTML(status.tools);
      for (const key of ['mode','root','scan','output']) get(key).value = state[key] || '';
      let draftSerial = 0, plan = null, applying = false, scanStarted = false, projectEditor = null;
      const updateHint = () => {get('mode-hint').textContent = get('mode').value === 'create' ? '新建时建立规范分区；来源留空则使用规范目录。先填写本机目标位置并查看预览。' : '编辑会保留未涉及的配置与来源登记。来源框显示完整现有列表；清空来源表示停止索引这一类目录。';};
      updateHint();
      const invalidate = () => {
        draftSerial++; plan = null;
        get('preview').innerHTML = '<p class="caption-note">表单已变化，请重新检查并预览。</p>';
        get('error').textContent = '';
        projectEditor?.invalidate();
      };
      for (const key of ['root','scan','output']) get(key).oninput = invalidate;
      get('mode').onchange = () => {
        if (get('mode').value === 'create') {
          if (get('root').value === status.root) get('root').value = '';
          if (get('scan').value === (sources.scan_roots || []).join('\n')) get('scan').value = '';
          if (get('output').value === (sources.output_roots || []).join('\n')) get('output').value = '';
        } else if (!get('root').value && status.root) {
          get('root').value = status.root;
          if (!get('scan').value) get('scan').value = (sources.scan_roots || []).join('\n');
          if (!get('output').value) get('output').value = (sources.output_roots || []).join('\n');
        }
        updateHint();invalidate();
      };
      get('refresh').onclick = () => applying || projectEditor?.isBusy() ? undefined : page(el, params, capture(el));
      const discoveries = status.discovery?.items || [];
      get('discovery').innerHTML = discoveries.length ? discoveries.map((item,i) => `<div class="ws-discovery-row"><span><span class="pathline">${esc(item.path)}</span><small>${esc(item.reason)}</small></span><button type="button" class="btn small" data-ws-add="${i}">加入图库来源</button></div>`).join('') : '<p class="muted">没有发现额外候选；可手动填写完整出图路径。</p>';
      el.querySelectorAll('[data-ws-add]').forEach(button => {button.onclick = () => {
        if (applying || projectEditor?.isBusy()) return;
        const item = discoveries[Number(button.dataset.wsAdd)];
        get('output').value = lines(get('output').value + '\n' + item.path).join('\n');
        invalidate();button.textContent = '已加入表单';
      };});
      function updateJobs(current) {
        const scan = (current.jobs || []).find(j => /scan/i.test(String(j.kind || j.type || j.name || j.id || '')));
        const running = (current.jobs || []).find(j => j.status === 'running' && /scan|扫描/i.test(String(j.kind || j.type || j.name || j.label || j.id || '')));
        get('scan-state').textContent = running ? '索引扫描进行中，可在顶部查看进度。' : scan?.status === 'error' || scan?.status === 'failed' ? '最近扫描失败，请检查来源并重试。' : scan?.status === 'done' ? '最近扫描已完成；索引记录不代表生成质量验证。' : scanStarted ? '扫描请求已提交，暂未获取到任务状态；请刷新体检。' : '来源检查不等于已完成索引；保存后可开始扫描。';
        return Boolean(running);
      }
      if (updateJobs(jobs)) setTimeout(watchScan, 2500);
      async function watchScan() {
        if (!active()) return;
        try {
          const [current, latest] = await Promise.all([api('/api/jobs'),api('/api/overview')]);
          if (!active()) return;
          get('last-scan').textContent = latest.scan_at || '尚未扫描';
          if (updateJobs(current)) setTimeout(watchScan, 2500);
        } catch (error) {if (active()) get('scan-state').textContent = '暂时无法读取扫描状态：' + error.message;}
      }
      get('form').onsubmit = async event => {
        event.preventDefault();
        if (applying || projectEditor?.isBusy()) return;
        const request = ++draftSerial;
        plan = null;get('preview').innerHTML = '<p role="status">正在检查路径与变更…</p>';get('error').textContent = '';
        const body = {mode:get('mode').value,root:get('root').value.trim()};
        for (const [field,key] of [['scan','scan_roots'],['output','output_roots']]) {
          const values = lines(get(field).value);
          if (body.mode !== 'create' || values.length) body[key] = values;
        }
        try {
          const preview = await api('/api/workspace/preview',{body});
          if (!active() || request !== draftSerial) return;
          plan = preview;get('preview').innerHTML = previewHTML(preview);
          get('confirm').onchange = () => {get('apply').disabled = !get('confirm').checked || !plan?.can_apply || !plan?.token || applying;};
          get('apply').onclick = async () => {
            if (applying || projectEditor?.isBusy() || !plan?.can_apply || !plan?.token || !get('confirm').checked || request !== draftSerial) return;
            applying = true;get('apply').disabled = true;get('fields').disabled = true;get('refresh').disabled = true;
            try {
              const result = await api('/api/workspace/apply',{body:{token:plan.token}});
              if (!active()) return;
              if (!result.applied) throw new Error('服务未确认应用成功，请刷新体检。');
              const savedSources = result.status?.sources || plan.sources;
              status = {...status,...result.status,root:result.root,configured:true,available:result.status?.available !== false};
              Object.assign(sources,savedSources);
              projectEditor?.invalidate();
              get('mode').value = 'connect';get('root').value = result.root;
              get('scan').value = (savedSources?.scan_roots || []).join('\n');
              get('output').value = (savedSources?.output_roots || []).join('\n');
              updateHint();
              get('current-title').textContent = '当前工作环境';
              get('current-root').textContent = result.root;
              get('current-availability').textContent = result.status?.available === false ? '工作区尚不可访问，请刷新体检' : '工作环境已应用';
              plan = null;
              get('preview').innerHTML = '';
              get('result').innerHTML = `<div class="ws-success"><h3>工作环境已保存</h3><p class="pathline">${esc(result.root)}</p><p>来源已登记。${result.scan_required ? '开始索引后，模型与出图才会更新。' : '可按需刷新索引。'}</p><button id="ws-start-scan" type="button" class="btn primary">开始索引 / 扫描</button><a href="#/images" class="btn">查看图库</a><a href="#/projects" class="btn">管理项目与运行</a></div>`;
              if (result.status?.source_health) get('current-health').innerHTML = healthHTML(result.status.source_health);
              get('start-scan').onclick = async () => {
                const button = get('start-scan');button.disabled = true;
                try {
                  await api('/api/scan/start',{body:{}});
                  if (!active()) return;
                  scanStarted = true;get('scan-state').textContent = '扫描已开始，正在更新索引…';button.textContent = '扫描已启动';
                  if (pollJobs) pollJobs();
                  setTimeout(watchScan, 1000);
                } catch (error) {if (active()) {get('error').textContent = error.message;button.disabled = false;}}
              };
              if (toast) toast('工作环境已保存，可开始索引','ok');
            } catch (error) {
              if (active()) {get('error').textContent = error.message;invalidate();get('error').textContent = error.message;}
            } finally {if (active()) {applying = false;get('fields').disabled = false;get('refresh').disabled = false;}}
          };
        } catch (error) {if (active() && request === draftSerial) {get('error').textContent = error.message;get('preview').innerHTML = '';}}
      };
      projectEditor=bindProject(el,{api,active,restored,isWorkspaceBusy:()=>applying,
        workspaceReady:()=>Boolean(status.managed===true && status.available && status.root && get('mode').value==='connect' && get('root').value.trim()===status.root),
        setBusy:value=>{get('fields').disabled=value;get('refresh').disabled=value;},
        onSaved:saved=>{
          // Project creation changes configuration; invalidate the workspace token and merge only the new output.
          draftSerial++;plan=null;get('preview').innerHTML='<p class="caption-note">项目已创建，来源登记已变化；保存其他修改前请重新预览。</p>';
          if(saved.output_root_added){
            const output=typeof saved.output_root_added==='string' ? saved.output_root_added : String(saved.root).replace(/[\\/]$/,'')+'/Outputs';
            get('output').value=lines(get('output').value+'\n'+output).join('\n');
          }
        }});
    };
  }
  return {createPage,capture,banner,lines,healthHTML,previewHTML,toolsHTML,projectPreviewHTML};
});
