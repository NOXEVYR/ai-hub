/* Shared collaboration UI. Drafts live only in the navigation snapshot. */
(function(root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory(require('./harnesses.js'));
  else root.AIHubCollaboration = factory(root.AIHubHarnesses);
})(globalThis, function(Harness) {
  'use strict';
  const bindDraft = (container, key, transfer=false) => globalThis.AIHubAppUpdate?.bind(container, key, {transfer}) || {snapshot(){},saved(){},changed(){return false;},edit(){},discard(){}};
  const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const rows = v => Array.isArray(v) ? v : Array.isArray(v?.items) ? v.items : [];
  const names = {preparing:'目录准备待核对',cancelled:'已取消',queued:'等待领取',active:'有效',completed:'已完成',candidate:'待审核',approved:'已批准',retired:'已退役',recycled:'已回收',protected:'已保留',error:'回收失败，已保留',missing:'文件缺失',report:'正式报告',output:'交付产物',temp:'临时文件',knowledge:'知识参考',temp_candidate:'临时内容候选（仅盘点）',other_text:'其他文本',workspace:'工作区',project:'项目'};
  const label = v => names[v] || v || '未知';
  const documentCategories = {report:'工作报告',plan:'计划与方案',requirement:'需求说明',delivery:'交付资料',reference:'参考与规范',other_text:'待分类文档',temp_candidate:'临时资料候选'};
  const timeText = value => {
    if(!value)return '—';
    const date=new Date(typeof value==='number'?value*1000:value);
    return Number.isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{hour12:false});
  };
  const tools = '<option value="any">任意已启用工作端</option>';
  const fields = ['task-project','task-title','task-description','task-tool','task-report-policy','memory-title','memory-content','memory-scope','memory-project','memory-source','search-query','search-project','source-path','source-label','source-tool','policy-days','policy-enabled','mcp-tool'];
  const tabs = {tasks:'任务与产物',resources:'任务资源与收尾',memories:'长期记忆',sources:'报告来源',connect:'工作端接入',retention:'临时文件回收',deleted:'已删除记录'};
  const entities = {task:{key:'tasks',name:'任务'},artifact:{key:'artifacts',name:'报告 / 产物'},memory:{key:'memories',name:'长期记忆'},source:{key:'sources',name:'报告来源'}};
  function capture(el) {
    const draft = {};
    for (const id of fields) {
      const node = el?.querySelector('#co-' + id);
      if (node) draft[id] = node.type === 'checkbox' ? node.checked : node.value;
    }
    return {draft, tab:el?.dataset?.coTab || 'tasks', selectedTask:el?.dataset?.coTask || '', resourceTask:el?.dataset?.coResourceTask || '', root:el?.dataset?.coRoot || '', harness:Harness.capture(el)};
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
  const recordButton = (type,id,restore=false) => button(restore?'record-restore':'record-delete',id,restore?'恢复记录':'删除记录',`data-co-entity="${esc(type)}"`);
  function table(headers, body, empty) {
    return body.length ? `<div class="table-wrap"><table class="tbl co-table"><thead><tr>${headers.map(h=>`<th>${h}</th>`).join('')}</tr></thead><tbody>${body.join('')}</tbody></table></div>` : `<p class="muted co-empty">${empty}</p>`;
  }
  function reportSubmissionHTML(task) {
    const report=task.report_submission,policy=report?.policy||task.report_policy||'legacy';
    const status=report?.status==='submitted'?'报告已登记':['deferred','unverified'].includes(report?.status)?'报告待核验':policy==='required'?'报告待提交':policy==='optional'?'报告可选':'历史任务 · 未要求报告';
    return `<span class="badge">${esc(status)}</span><span class="file-sub">${esc(report?.reason || (policy==='required'?'本次领取者提交分类报告和记忆声明后，才能完成任务。':policy==='optional'?'可直接完成；需要归档时仍应提交报告。':'升级保留原状态，不推断已提交。'))}</span>${task.id && ['deferred','unverified'].includes(report?.status)?button('report-check',task.id,'核验此任务'):''}`;
  }
  const deliveryErrors={needs_lease:'需要原工作端提供当前领取凭据',network_unavailable:'本地服务暂不可连接',network_error:'本地服务暂不可连接',endpoint_config_invalid:'所选安装的连接配置不可用，请在原工作端核对安装位置',endpoint_identity_invalid:'服务与所选安装不匹配，提交已保留',endpoint_config_changed:'连接配置已变化，请在原工作端核对原提交回执',timeout:'提交响应超时',http_error:'服务未接受提交',workspace_changed:'工作区已切换，需要重新接入',rejected:'提交被拒绝，请核对任务和领取状态',storage_unavailable:'本地暂存不可用',invalid_entry:'暂存记录无效',unconfirmed:'服务尚未确认登记',submission_conflict:'提交 ID 与本次报告正文或类型不一致，暂存内容已保留',submission_invalid:'已登记报告被移除或改变，需要原工作端核对',task_unavailable:'关联任务不可用',task_not_active:'任务已不在本次执行中',ownership_changed:'任务领取者已变化',retry_limit:'自动发送次数已用完，仍可核对提交回执',response_invalid:'服务响应未能确认提交',server_busy:'服务暂时繁忙'};
  function deliveryHTML(result) {
    if(!result || result.available===false)return '<p class="caption-note">提交队列暂不可读，不能据此判断报告是否已提交。可在原工作端调用 report_outbox_list 核对。</p>';
    const items=rows(result.items),titles={pending:'待提交',failed:'提交失败',submitted:'已确认登记'};
    const counts=Object.keys(titles).map(s=>`${titles[s]} ${items.filter(i=>i.status===s).length}`).join(' · ');
    return `<p role="status">${esc(counts)}</p><p class="caption-note">只展示本机当前工作区通过新版 MCP 提交的暂存记录。磁盘报告、其他电脑和未接入协议的 AI 输出不在此清单；暂无记录不能证明全部报告已提交。</p>${table(['报告 / 任务','提交工作端','提交状态','操作'],items.map(i=>`<tr><td><b>${esc(i.title)}</b><span class="file-sub">${esc(i.task_id)}</span></td><td>${esc(i.tool)}<span class="file-sub">${esc(i.client_id)}</span></td><td>${esc(titles[i.status]||'状态未知')}<span class="file-sub">已尝试 ${esc(i.attempts||0)} 次${i.error_code?' · '+esc(deliveryErrors[i.error_code]||'需要原工作端核对'):''}</span></td><td>${i.status==='submitted'?'已登记':button('delivery-retry-guide',i.id,'重试指引')}</td></tr>`),'暂无新版 MCP 提交记录。任务是否需要报告，请核对上方任务清单。')}${result.truncated?'<p class="warning-note">当前仅显示部分队列记录，请在原工作端查询更多。</p>':''}${result.error_code || result.partial?'<p class="warning-note">部分暂存记录不可读，列表可能不完整。</p>':''}`;
  }
  const nativeNames={not_started:'尚未提交原生任务',submitting:'已记录提交意图',running:'原生任务运行中',uncertain:'原生结果待核对',succeeded:'原生执行成功',failed:'原生执行失败',cancelled:'已记录取消终态'};
  function executionDetailHTML(task) {
    const e=task?.execution;if(!e)return '';
    const origin=e.origin || {},kindNames={image:'图片',video:'视频',audio:'音频',document:'文档',other:'其他成果'};
    const identities=[['原来源',origin.authority_id],['原项目',origin.project_id],['原任务',origin.task_id],['原轮次',origin.run_id],['原尝试',origin.call_id],['执行编号',e.execution_id],['队列任务',e.queue_task_id || task.id],['执行客户端',e.executor?.client_id],['原生请求',e.provider_request_id]];
    return `<section class="co-execution-detail"><h4>执行与成果</h4><p>${esc(nativeNames[e.provider_state] || '原生状态未知')} · ${e.evidence_source==='worker_report'?'来自执行客户端上报，曜核未独立核验原生任务。':'来自队列账本，尚无原生服务执行证据。'}</p>${e.cancel_requested && !['succeeded','failed','cancelled'].includes(e.provider_state)?'<p class="warning-note">已请求取消，尚未确认停止。请在原执行软件中核对。</p>':''}${e.outcome?.error_code?`<p class="dialog-error">失败分类：${esc(e.outcome.error_code)}</p>`:''}<details><summary>原任务与执行身份</summary><dl>${identities.map(([name,value])=>`<dt>${name}</dt><dd class="pathline">${esc(value || '未记录')}</dd>`).join('')}</dl></details><p class="caption-note">成果保存在原生成软件中。下方是执行客户端提交的清单，未在这里打开、下载或验证文件；需要到来源软件显式关联并审核。</p>${table(['成果编号 / 类型','文件类型 / 大小','摘要声明'],rows(e.results).map(r=>`<tr><td><b>${esc(r.result_id)}</b><span class="file-sub">${esc(kindNames[r.kind] || '未知类型')}</span></td><td>${esc(r.media_type)}<span class="file-sub">${esc(r.bytes)} 字节</span></td><td>${r.sha256?`<details><summary>已提供 SHA-256 · 文件待核验</summary><code class="pathline">${esc(r.sha256)}</code></details>`:'未提供文件摘要 · 未校验'}</td></tr>`),'暂无终态成果清单。原生任务状态与报告提交分别记录。')}${e.results_manifest_sha256?`<details><summary>成果清单摘要</summary><code class="pathline">${esc(e.results_manifest_sha256)}</code></details>`:''}</section>`;
  }
  function tasksHTML(items) {
    return table(['任务 / 项目','工具 / 执行客户端','队列 / 原生状态','报告提交','操作'], rows(items).map(t=>{
      const e=t.execution,closed=e && ['completed','cancelled_before_claim'].includes(e.dispatch_state);
      const execution=e?`<span class="file-sub">${esc(nativeNames[e.provider_state] || '原生状态未知')}</span><span class="file-sub">${e.evidence_source==='worker_report'?'执行客户端上报 · 待独立验收':'队列记录 · 未调用原生服务'}</span>${e.cancel_requested && !['succeeded','failed','cancelled'].includes(e.provider_state)?'<span class="badge">已请求取消 · 尚未确认停止</span>':''}`:'';
      return `<tr><td><b>${esc(t.title)}</b><span class="file-sub">${esc(t.project)}${e?'':' · '+esc(t.id)}</span>${e?`<details><summary>原任务与执行编号</summary><span class="file-sub">原任务 ${esc(e.origin?.task_id)} · 轮次 ${esc(e.origin?.run_id)}</span><span class="file-sub">队列 ${esc(t.id)}</span></details>`:''}</td><td>${esc(t.target_tool)}<span class="file-sub">${esc(e?.executor?.client_id || t.owner || '尚未领取')}</span>${e?`<span class="file-sub">来源 ${esc(e.origin?.authority_id)}</span>`:''}</td><td>${esc(t.status==='active'?'已领取':label(t.status))}${execution}</td><td>${reportSubmissionHTML(t)}</td><td>${button('task-detail',t.id,e?'查看执行与目录':'查看目录与交接')}${!e && t.status==='active'?button('requeue',t.id,'释放领取'):''}${!e || closed?recordButton('task',t.id):'<span class="file-sub">未完成执行保留原记录</span>'}</td></tr>`;
    }),'暂无任务。先创建任务，再由接入工具主动领取。');
  }
  function artifactsHTML(items) {
    return table(['产物 / 路径','用途 / 分类依据','存放 / 状态','到期时间','操作'],rows(items).map(a=>`<tr><td><b>${esc(a.title)}</b><span class="file-sub pathline">${esc(a.path)}</span><span class="file-sub">任务 ${esc(a.task_id)}${a.source_tool?' · 提交工具 '+esc(a.source_tool):''}</span></td><td>${esc(documentCategories[a.category] || '待分类文档')}<span class="file-sub">${a.category_source==='submitted'?'AI 提交声明 · 未人工核验':'旧提交未声明用途 · 待确认'}</span></td><td>${esc(label(a.kind))} · ${esc(label(a.status))}</td><td>${esc(a.pinned ? '已锁定保留' : a.expires_at ? timeText(a.expires_at) : '默认保留')}</td><td>${a.status==='active' ? button('pin',a.id,a.pinned?'取消锁定':'锁定保留',`aria-pressed="${!!a.pinned}"`) : esc(a.pinned?'已锁定':'—')}${a.kind==='report' && a.status==='active'?button('memory-from-report',a.id,'引用为记忆候选'):''}${recordButton('artifact',a.id)}</td></tr>`),'暂无已登记产物。接入工具先读 submission_schema，按指定目录提交，并为每份成果声明文档用途。');
  }
  function memoriesHTML(items, review=true) {
    return table(['记忆与内容','范围 / 来源','状态','操作'],rows(items).map(m=>`<tr><td><b>${esc(m.title)}</b><details><summary>阅读内容</summary><pre class="co-prose">${esc(m.content)}</pre></details></td><td>${esc(label(m.scope))}${m.project?' · '+esc(m.project):''}<span class="file-sub">来源报告 ${esc(m.source_artifact_id)}</span><span class="file-sub">${esc(timeText(m.updated_at || m.created_at))}</span></td><td>${esc(label(m.status))}</td><td>${review && m.status==='candidate' ? button('approve',m.id,'批准保留') : ''}${review && m.status!=='retired' ? button('retire',m.id,'退役') : '—'}${recordButton('memory',m.id)}</td></tr>`),review?'暂无共享记忆候选。只把经确认、可复用且有报告来源的结论提交审核。':'未找到已批准的长期记忆。候选与退役条目不进入检索。');
  }
  function sourcesHTML(items) {
    return table(['来源 / 目录','工具','最近盘点','操作'],rows(items).map(s=>`<tr><td><b>${esc(s.label || s.path)}</b><span class="file-sub pathline">${esc(s.path)}</span></td><td>${esc(s.tool==='any'?'通用来源':s.tool)}</td><td>${esc(s.scanned_at || s.last_scan_at ? timeText(s.scanned_at || s.last_scan_at) : '未盘点')}<span class="file-sub">${esc(s.file_count ?? 0)} 份文档</span>${s.scanned_at || s.last_scan_at ? `<span class="file-sub ${s.truncated?'warning-note':''}">${s.truncated?'未完整（达到扫描预算或部分不可读）':'已完成本次盘点'}</span>`:''}${s.error?`<span class="file-sub dialog-error">${esc(s.error)}</span>`:''}</td><td>${button('scan',s.id,'只读盘点')}${button('source-reconfirm',s.id,'重新确认目录')}${recordButton('source',s.id)}</td></tr>`),'暂无报告来源。明确添加报告目录后再盘点，原文件保持原位。');
  }
  const resourceStates={running:'运行中',cleanup_pending:'待收尾',closed:'客户端报告已收尾',cleanup_failed:'收尾失败或部分失败',manual_required:'保留或人工处理'};
  const resourceTypes={browser_tab:'浏览器标签页',service_process:'服务进程',listener:'监听端口',temp_directory:'临时目录',output:'交付产物'};
  const resourceOwnership={task_exclusive:'本任务独占',shared:'共享资源',user_owned:'用户资源'};
  const resourceState=item=>item.state==='manual_required' && item.resource_type==='output'?'正式产物保留':item.state==='manual_required' && item.evidence?.outcome==='not_checked'?'未能检查，需人工处理':resourceStates[item.state] || '状态未知';
  const resourceHint=item=>item.resource_type==='output'?'正式产物保留，不参与临时收尾。':item.ownership_declared===false?'资源归属未声明，不可由本任务自动收尾。':item.ownership==='shared'||item.ownership==='user_owned'?'共享或用户资源，不可由本任务自动收尾。':item.message || (item.state==='running'?'最近登记或上报为运行中；不代表实时运行。':item.state==='cleanup_pending'?'任务已结束或领取批次已变化，等待原客户端收尾上报。':item.state==='closed'?'客户端报告收尾完成，曜核尚未独立核验。':item.state==='cleanup_failed'?'客户端报告失败，需核对残留与后续处理。':'客户端未能完成检查，请核对能力或权限限制。');
  function resourcesHTML(result,{offset=0}={}) {
    const items=rows(result),counts=result?.counts || {};
    const summary=Object.entries(resourceStates).map(([state,title])=>`${title} ${esc(counts[state] ?? items.filter(item=>item.state===state).length)}`).join(' · ');
    const note='<p class="caption-note">收尾结果由登记客户端主动上报；「客户端报告已收尾」不是曜核执行，也不是独立核验。共享、用户资源及归属未知的资源需人工处理，客户端能力不可用时不能自动收尾。</p>';
    return `<p role="status">${summary}</p>${note}${table(['任务 / 项目','资源名称','客户端 / 类型','归属','状态','最后证据时间','提示'],items.map(item=>`<tr><td><b>${esc(item.task_title || item.task_id)}</b><span class="file-sub">${esc(item.project)} · ${esc(item.task_id)}</span>${button('resource-task-detail',item.task_id,'查看本任务资源')}</td><td class="pathline">${esc(item.title || item.label || item.resource_type)}<span class="file-sub">${esc(item.id)}</span></td><td>${esc(item.client_id)}<span class="file-sub">${esc(resourceTypes[item.resource_type] || item.resource_type)}</span></td><td>${esc(item.ownership_declared===false?'归属未声明':resourceOwnership[item.ownership] || '归属未知')}<span class="file-sub">${item.temporary===true?'登记为临时资源':'登记为保留资源'}</span></td><td>${esc(resourceState(item))}<span class="file-sub">${item.evidence_source==='client_report'?'客户端上报证据':'仅登记声明'}</span></td><td>${esc(timeText(item.last_evidence_at))}</td><td class="pathline">${esc(resourceHint(item))}${item.task_removed?'<span class="file-sub warning-note">关联任务管理记录已删除，资源证据仍保留。</span>':''}</td></tr>`),result?.total>0?'本批没有可展示记录，请返回上一批或重新按任务查询。':'尚未登记任务资源。需要接入工具主动登记资源并上报收尾结果；历史任务不会自动出现资源记录。')}${Number.isInteger(result?.total)?`<p class="caption-note">当前筛选共 ${esc(result.total)} 条，本批展示 ${esc(items.length?offset+1:0)}–${esc(items.length?offset+items.length:0)} 条${result.total>items.length?'；可翻页或按任务筛选查询':''}。</p>`:''}${result?.available===false?'<p class="warning-note">任务资源能力不可用，请刷新协作状态并核对工作环境。</p>':''}`;
  }
  function resourceDetailHTML(result,taskId) {
    if(!taskId)return '';
    const items=rows(result).filter(item=>String(item.task_id)===String(taskId)),task=items[0];
    return `<h4>任务资源详情：${esc(task?.task_title || taskId)}</h4><p class="caption-note">任务 ID：${esc(taskId)}。身份、样本与证据均由客户端提供，不能作为曜核独立验证的结论。</p>${items.map(item=>`<details><summary>${esc(item.title || item.label || item.id)} · ${esc(resourceState(item))}</summary><dl><dt>登记身份</dt><dd class="pathline">${esc(typeof item.identity==='string'?item.identity:JSON.stringify(item.identity ?? {}))}</dd><dt>最近客户端证据</dt><dd><pre class="co-code">${esc(JSON.stringify({evidence:item.evidence ?? null,sample:item.sample ?? null},null,2))}</pre></dd><dt>最近提示</dt><dd class="pathline">${esc(item.message || '尚无收尾上报')}</dd></dl></details>`).join('') || '<p class="co-empty">当前结果没有该任务的资源登记；历史任务不会自动推导资源。</p>'}`;
  }
  function previewHTML(result) {
    const items=rows(result), errors=rows(result?.errors);
    return `<p>本批符合回收条件：${items.length} 项。执行时会重新检查；文件变化或回收失败会保留。</p>${table(['临时文件','到期 / 检查'],items.map(a=>`<tr><td class="pathline">${esc(a.path || a.title || a.id)}</td><td>${esc(a.expires_at ? timeText(a.expires_at) : a.reason || '已通过预检')}</td></tr>`),result?.truncated?'本批没有符合条件的临时文件；仍有登记未检查，请继续预览。':'本批没有符合条件的临时文件。')}${result?.truncated?'<p class="warning-note">尚未检查全部登记。点击「继续预览下一批」查看后续候选；回收按钮只处理当前展示的候选。</p>':''}${rows(result?.protected).length?`<details><summary>已保护 ${rows(result.protected).length} 项，查看原因</summary>${table(['文件','保留原因'],rows(result.protected).map(a=>`<tr><td class="pathline">${esc(a.path)}</td><td>${esc(a.reason)}</td></tr>`),'')}</details>`:''}${errors.map(e=>`<p class="dialog-error">${esc(typeof e==='string'?e:e.error || e.reason)}</p>`).join('')}`;
  }
  function inventoryHTML(result) {
    const inventoryLabels={report:'报告线索',knowledge:'知识线索',temp_candidate:'疑似临时',other_text:'其他文档'};
    return `<h4>报告盘点结果</h4><p class="caption-note">分类按路径推测，未做内容审核。</p>${table(['报告路径','大小 / 分类线索'],rows(result).map(r=>`<tr><td class="pathline">${esc(r.path)}</td><td>${esc(r.size ?? '—')} 字节 · ${esc(inventoryLabels[r.category] || '其他文档')}</td></tr>`),'没有发现符合规则的文本报告。')}${result?.truncated?'<p>结果已达盘点上限，仅显示部分条目。</p>':''}${rows(result?.errors).map(e=>`<p class="dialog-error">${esc(typeof e==='string'?e:e.error || e.reason)}</p>`).join('')}`;
  }
  function memoryGuideHTML(state) {
    const counts=state.record_counts?.memories, pipeline=state.memory_pipeline, reports=rows(state.artifacts).filter(a=>a.kind==='report' && a.status==='active');
    const declaration=Number.isInteger(pipeline?.reports_unevaluated)?`<br>记忆声明：未声明 ${esc(pipeline.reports_unevaluated)} 份 · 已评估 ${esc(pipeline.reports_evaluated)} 份 · 其中无需候选 ${esc(pipeline.reports_evaluated_without_candidates)} 份。`:'';
    const shown=rows(state.memories), number=status=>counts?.[status] ?? shown.filter(m=>m.status===status).length;
    const approved=pipeline?.approved ?? number('approved'),candidate=pipeline?.candidates ?? number('candidate'),retired=number('retired'),deleted=counts?.deleted ?? rows(state.deleted_records?.memories).length,reportCount=pipeline?.reports ?? state.record_counts?.artifacts?.reports ?? reports.length;
    const reason=candidate>0?'已有候选等待用户批准；候选不会进入长期记忆检索。':approved>0?'已有已批准记忆，可在下方按关键词检索。':retired>0?'已有退役记录，但当前没有已批准记忆；退役记录不进入检索。':deleted>0?'记忆记录已隐藏，可到「已删除记录」检查并恢复。':reportCount&&pipeline?.reports_unevaluated===0&&pipeline?.reports_evaluated_without_candidates>0?'报告提交时已声明无需记忆候选，所以当前没有待审核结论；无需把日常日志强行变成长期记忆。':reportCount?'已有报告不等于记忆候选。报告已登记，但尚未提交待审核结论，因此长期记忆仍为空。':'尚无记忆候选。请先创建任务，让接入工具提交正式报告，再提炼候选。既有报告目录的只读盘点也不会自动生成记忆。';
    return `<p class="caption-note">${counts?'工作区实际数量':'最近展示记录数量'}：待审核 ${esc(candidate)} · 已批准 ${esc(approved)} · 已退役 ${esc(retired)} · 已删除 ${esc(deleted)}</p><p>${esc(reason)}</p><p class="caption-note">${pipeline?'当前工作区':'最近展示'}：${esc(reportCount)} 份登记报告 / ${esc(candidate)} 条待审核候选${pipeline?` · 尚未形成候选的报告 ${esc(pipeline.reports_without_candidates ?? 0)} 份`:''}。${declaration}</p><details><summary>记忆流程与排查</summary><ol><li>报告：工具在任务目录提交并登记正式报告。</li><li>候选：从报告提炼稳定结论，注明来源并提交记忆候选。</li><li>用户批准：由你审核「批准保留」。</li><li>可检索：只有已批准且未删除的记忆进入共享检索。</li></ol><p>接入工具请更新曜核 MCP，并使用报告提交入口（report_submit）显式提交 memory_candidates。空候选数组只登记报告。已有报告可在「任务与产物」点击「引用为记忆候选」，在下方填写结论并提交。</p><p class="caption-note">未声明不等于报告中没有可复用知识。发现 Codex 安装或收到心跳，不代表它已提交报告或自动写入共享记忆。曜核不会自动总结报告，各工具原生记忆不会被读取或修改。</p><div class="co-actions">${button('go-tasks','','去任务与报告')}${button('go-sources','','去报告来源')}${button('go-connect','','去工作端接入')}${deleted?button('go-deleted','','查看已删除记忆'):''}</div></details>`;
  }
  function deletedHTML(state) {
    return Object.entries(entities).map(([type,info])=>`<h4>${esc(info.name)} · 已删除 ${esc(state.record_counts?.[info.key]?.deleted ?? rows(state.deleted_records?.[info.key]).length)}</h4>${table(['记录 / 原位置','原状态 / 删除时间','操作'],rows(state.deleted_records?.[info.key]).map(item=>`<tr><td><b>${esc(item.title || item.label || item.id)}</b><span class="file-sub pathline">${esc(item.path || item.project || item.id)}</span></td><td>${esc(label(item.status))}<span class="file-sub">${esc(timeText(item.deleted_at))}</span></td><td>${recordButton(type,item.id,true)}</td></tr>`),'暂无已删除记录。')}`).join('');
  }
  function deletePreviewHTML(preview,type) {
    const impacts=preview.impacts || {},impactLabels={artifacts:'关联报告 / 产物记录',memories:'关联记忆记录',approved_memories:'其中已批准记忆',inventory:'来源盘点记录'};
    return `<h3>删除${esc(entities[type]?.name)}记录：${esc(preview.title)}</h3><p class="caption-note">记录 ID：${esc(preview.entity_id)}${preview.record?.path?` · 原位置：${esc(preview.record.path)}`:''}</p><p class="warning-note">请核对影响后再确认。只隐藏管理记录，原文件、目录和各工具原生数据保留。记录可以恢复；已批准记忆被隐藏后不再进入共享检索。</p><ul>${Object.entries(impactLabels).map(([key,title])=>`<li>${esc(title)}：${esc(impacts[key] ?? 0)}</li>`).join('')}</ul>${rows(preview.warnings).map(w=>`<p class="warning-note">${esc(w)}</p>`).join('')}<p class="caption-note">预览到期：${esc(timeText(preview.expires_at))}。确认时服务会再次核对工作区和记录。</p>`;
  }
  function openExecutionAccess(win=globalThis) {
    try {
      if(typeof win.chrome?.webview?.postMessage!=='function')return false;
      win.chrome.webview.postMessage('open-execution-access');
      return true;
    } catch { return false; }
  }
  function executionAccessHTML() {
    return '<section class="panel"><div class="panel-head"><h3>可选软件协作</h3></div><div class="body"><p>为映序等任务来源或棱光等执行软件创建单独的接入文件。三款软件可独立使用；接入身份由合作软件提供，与客户端心跳分别管理。</p><button id="co-execution-access" type="button" class="btn primary">管理协作接入</button><p class="caption-note">在桌面窗口选择权限、身份和私有保存位置，再到合作软件导入。接入文件包含私有凭据，请勿放进报告、模型提示词或分享包。撤销接入不会停止已经领取的原生任务。</p></div></section>';
  }
  function shell() {
    return `<div class="co-toolbar"><div id="co-root" class="pathline"></div><button id="co-refresh" class="btn" type="button">刷新协作状态</button></div><p id="co-error" class="dialog-error co-alert" role="alert" tabindex="-1"></p><p id="co-message" role="status" aria-live="polite"></p><div id="co-availability"></div><div class="co-tabs" role="group" aria-label="协作管理区域">${Object.entries(tabs).map(([id,title])=>`<button type="button" class="btn" data-co-tab="${id}" aria-pressed="false">${title}</button>`).join('')}</div>
      <section data-co-panel="tasks"><div class="panel"><div class="panel-head"><h3>新建协作任务</h3></div><div class="body"><p class="caption-note">项目统一使用 40_Projects/&lt;项目&gt;/Work/AIHub/&lt;任务 ID&gt;，报告、输出、临时文件分别进入 Reports、Outputs、Temp。工具通过队列主动领取，不会在这里自动启动其他软件。</p><form id="co-task-form" data-app-draft-scope="co-task-form"><fieldset class="co-fields"><div class="co-grid"><label>项目名称<input id="co-task-project" required maxlength="80" autocomplete="off"></label><label>交接工具<select id="co-task-tool">${tools}</select></label><label class="co-wide">报告要求<select id="co-task-report-policy"><option value="required">必须提交报告后完成（推荐）</option><option value="optional">报告可选（仅简单任务）</option></select></label><label class="co-wide">任务标题<input id="co-task-title" required maxlength="200"></label><label class="co-wide">任务说明<textarea id="co-task-description" rows="4" maxlength="20000"></textarea></label></div><button class="btn primary" type="submit">创建任务与目录</button></fieldset></form></div></div><section class="panel"><div class="panel-head"><h3>最近任务</h3></div><div id="co-tasks"></div><div id="co-task-detail" class="body" aria-live="polite"></div><div id="co-requeue-confirm" class="body" aria-live="polite"></div></section><section class="panel"><div class="panel-head"><h3>报告提交队列</h3></div><div id="co-deliveries" class="body" aria-live="polite"></div></section><section class="panel"><div class="panel-head"><h3>已登记产物</h3></div><div class="body caption-note">报告和交付产物默认保留；锁定后，临时文件也不会自动回收。</div><div id="co-artifacts"></div></section></section>
      <section data-co-panel="memories" hidden><section class="panel"><div class="panel-head"><h3>长期记忆审核</h3></div><div class="body"><p>共享记忆需先提交候选，再由你批准。保留稳定的约定、可复用结论与关键决策；过程日志留在报告中。退役保留记录，但不再用于共享检索。各工具原生记忆不会被读取或修改。</p></div><div id="co-memory-guide" class="body" aria-live="polite"></div><div id="co-memories"></div></section><section class="panel"><div class="panel-head"><h3>提交记忆候选</h3></div><div class="body"><form id="co-memory-form" data-app-draft-scope="co-memory-form"><fieldset class="co-fields"><div class="co-grid"><label>记忆标题<input id="co-memory-title" required maxlength="200"></label><label>来源报告<select id="co-memory-source" required><option value="">请先登记报告产物</option></select></label><label>适用范围<select id="co-memory-scope"><option value="project">当前项目</option><option value="workspace">整个工作区</option></select></label><label>项目名称<input id="co-memory-project" maxlength="80"></label><label class="co-wide">可复用结论<textarea id="co-memory-content" rows="5" required maxlength="20000"></textarea></label></div><button class="btn primary" type="submit">提交候选，等待审核</button></fieldset></form></div></section><section class="panel"><div class="panel-head"><h3>检索已批准记忆</h3></div><div class="body"><form id="co-search-form"><fieldset class="co-fields co-grid"><label>关键词<input id="co-search-query" type="search"></label><label>项目筛选（可选）<input id="co-search-project"></label><button class="btn" type="submit">检索长期记忆</button></fieldset></form><div id="co-search-results" aria-live="polite"></div></div></section></section>
      <section data-co-panel="sources" hidden><section class="panel"><div class="panel-head"><h3>既有报告来源</h3></div><div class="body"><p>只读盘点明确登记的文本报告目录。不会搬动既有文件、扫描各工具凭据与原生会话，也不会自动把报告提升为长期记忆。已有来源不参加临时文件回收。</p><form id="co-source-form" data-app-draft-scope="co-source-form"><fieldset class="co-fields"><div class="co-grid"><label class="co-wide">报告目录<input id="co-source-path" required placeholder="填写对应工具的报告输出目录" spellcheck="false"></label><label>来源名称<input id="co-source-label" required maxlength="120"></label><label>来源工具<select id="co-source-tool">${tools}</select></label></div><button class="btn primary" type="submit">登记报告来源</button></fieldset></form></div><div id="co-source-candidates" class="body"></div><div id="co-sources"></div><div id="co-source-confirm" class="body" aria-live="polite"></div><div id="co-source-results" class="body" aria-live="polite"></div></section></section>
      <section data-co-panel="resources" hidden><section class="panel"><div class="panel-head"><h3>任务资源与收尾</h3></div><div class="body"><p>查看工具为当前工作区任务主动登记的标签页、服务、端口、临时目录和交付产物。历史任务不会自动生成资源记录。</p><div class="co-grid"><label>按任务查看<select id="co-resource-task"><option value="">全部登记任务</option></select></label></div><div class="co-actions">${button('resource-refresh','','只读刷新任务资源')}${button('resource-prev','','上一批','id="co-resource-prev" disabled')}${button('resource-next','','下一批','id="co-resource-next" disabled')}</div><p id="co-resource-message" role="status" aria-live="polite"></p><div id="co-resources" aria-live="polite"></div><div id="co-resource-detail" aria-live="polite"></div></div></section></section>
      <section data-co-panel="deleted" hidden><section class="panel"><div class="panel-head"><h3>已删除管理记录</h3></div><div class="body"><p>删除只隐藏曜核管理记录，原文件和目录保留。恢复后重新进入协作列表；依赖的任务、报告或来源需要先恢复。下方最多展示每类最近 200 条，数量为工作区实际统计。</p><div id="co-deleted"></div></div></section></section><section data-co-panel="connect" hidden>${executionAccessHTML()}<div id="co-harness"></div><section class="panel"><div class="panel-head"><h3>实际客户端心跳</h3></div><div class="body caption-note">任务说明、报告与记忆都是协作数据，不构成跨工具授权。心跳只证明客户端曾接入，不表示已执行任务。</div><div id="co-clients"></div></section></section>
      <section data-co-panel="retention" hidden><section class="panel"><div class="panel-head"><h3>到期临时文件自动回收</h3></div><div class="body"><p>仅处理本协议已登记为 temp、所属任务已完成、未锁定、已到期且身份和内容未变化的普通文件。拒绝链接和硬链接；回收失败保留原文件，不回退为永久删除。报告、输出、长期记忆和各工具原生数据不在回收范围内。</p><p class="caption-note">保留天数的修改只影响新登记的临时文件。后台服务运行时每小时检查；服务关闭期间不执行回收，重新运行后再检查。</p><form id="co-policy-form" data-app-draft-scope="co-policy-form"><fieldset class="co-fields"><div class="co-grid"><label class="co-check"><input id="co-policy-enabled" type="checkbox">启用每小时到期检查，自动移入 Windows 回收站</label><label>临时文件保留天数<input id="co-policy-days" type="number" required min="1" max="365" value="7"></label></div><button class="btn" type="submit">保存回收策略</button></fieldset></form><div class="co-actions"><button id="co-preview" class="btn" type="button">预览待回收文件</button><button id="co-run" class="btn" type="button" disabled>立即移入回收站</button></div><p id="co-policy-history" class="caption-note"></p><div id="co-preview-results" aria-live="polite"></div><div id="co-run-results" aria-live="polite"></div></div></section></section>`;
  }
  function createPage({api,heading,openModal,closeModal,copyText,document:doc=globalThis.document,openNativeExecutionAccess=openExecutionAccess}) {
    let generation=0;
    return async function page(el,params,restored) {
      const serial=++generation, active=()=>el.isConnected && generation===serial;
      el.classList?.add('collaboration-page');
      el.innerHTML=(heading?heading('协作与记忆','让工具共享任务目录、可追溯报告与经审核的长期记忆。','COLLABORATION'):'<h2>协作与记忆</h2>')+shell();
      const get=id=>el.querySelector('#co-'+id), post=(action,body={})=>api('/api/collaboration/'+action,{body:state.root ? {...body,_workspace_root:state.root} : body});
      const drafts=Object.fromEntries(['task','memory','source','policy'].map(id=>[id,bindDraft(get(id+'-form'),'collaboration:'+JSON.stringify([restored?.root||'',id]))]));
      let state={},busy=false,ready=false,policyDirty=!!restored?.draft,previewReady=false,previewOffset=0,previewIds=[],requeueId=null,sourceSerial=0,recordSerial=0,recordDialog=null,searchMemories=[];
      let resourceSerial=0,resourceResult=null,resourceOffset=0,resourceTask=String(restored?.resourceTask || '');
      const setError=e=>{get('error').textContent=e?.message || String(e || '');};
      function selectTab(id) {
        id=Object.hasOwn(tabs,id)?id:'tasks';el.dataset.coTab=id;
        el.querySelectorAll('[data-co-panel]').forEach(n=>{n.hidden=n.dataset.coPanel!==id;});
        el.querySelectorAll('[data-co-tab]').forEach(n=>n.setAttribute('aria-pressed',String(n.dataset.coTab===id)));
      }
      function setBusy(value) {
        get('execution-access').disabled=value;
        busy=value;
        el.querySelectorAll('fieldset.co-fields').forEach(n=>{n.disabled=value || !ready;});
        el.querySelectorAll('[data-co-action], #co-refresh, #co-preview').forEach(n=>{n.disabled=value || (!ready && n.id!=='co-refresh');});
        get('run').disabled=value || !ready || !previewReady;
        const resourceItems=rows(resourceResult || state.resources),resourceTotal=(resourceResult || state.resources)?.total ?? resourceItems.length;
        get('resource-task').disabled=value || !ready;
        get('resource-prev').disabled=value || !ready || resourceOffset===0;
        get('resource-next').disabled=value || !ready || !resourceItems.length || resourceOffset+resourceItems.length>=resourceTotal;
      }
      async function act(fn) {
        if(busy || !active())return;
        setError('');get('message').textContent='正在处理…';setBusy(true);
        try{await fn();if(active())get('message').textContent='操作完成。';}
        catch(e){if(active()){setError(e);get('message').textContent='操作未完成，表单内容已保留。';}}
        finally{if(active())setBusy(false);}
      }
      const modalAvailable=()=>typeof openModal==='function' && typeof closeModal==='function' && !!doc?.querySelector;
      function recordModal(context,html,confirmLabel,enabled=false) {
        openModal(`${html}<p class="caption-note">当前工作区：${esc(context.root)}</p><p id="co-record-error" class="dialog-error" role="alert"></p><div class="dialog-actions"><button id="co-record-cancel" class="btn" type="button">取消</button><button id="co-record-confirm" class="btn primary" type="button" ${enabled?'':'disabled'}>${esc(confirmLabel)}</button></div>`);
        const modal=doc.querySelector('#modal');
        if(!modal)throw new Error('确认窗口不可用，操作未执行。');
        modal.onModalClose=()=>{if(recordDialog===context){recordDialog=null;++recordSerial;}};
        modal.querySelector('#co-record-cancel').onclick=()=>closeModal();
        return modal;
      }
      const currentRecordDialog=context=>active() && ready && context.root===state.root && recordDialog===context && context.serial===recordSerial;
      function confirmImportant(title,warning,confirmLabel,execute) {
        if(busy || !active() || !ready)return;
        const context={root:state.root,serial:++recordSerial,running:false};recordDialog=context;
        const modal=recordModal(context,`<h3>${esc(title)}</h3><p class="warning-note">${esc(warning)}</p>`,confirmLabel,true);
        modal.querySelector('#co-record-confirm').onclick=async()=>{
          if(!currentRecordDialog(context) || context.running || busy)return;
          context.running=true;modal.querySelector('#co-record-confirm').disabled=true;
          closeModal();await execute();
        };
      }
      async function recordAction(type,id,restoreRecord=false) {
        if(!Object.hasOwn(entities,type))throw new Error('记录类型无效，请刷新列表。');
        if(!ready || !state.root)throw new Error('工作区不可用，请先配置工作环境。');
        if(!modalAvailable())throw new Error('确认窗口不可用，删除和恢复未执行。请刷新页面。');
        const candidates=restoreRecord?rows(state.deleted_records?.[entities[type].key]):[...rows(state[entities[type].key]),...(type==='memory'?searchMemories:[])];
        const item=candidates.find(item=>String(item.id)===String(id));
        if(!item)throw new Error('记录已变化，请刷新后重新选择。');
        const context={root:state.root,type,id:String(id),serial:++recordSerial,running:false};recordDialog=context;
        const payload={entity_type:type,entity_id:context.id,_workspace_root:context.root};
        let modal;
        if(restoreRecord) {
          modal=recordModal(context,`<h3>恢复${esc(entities[type].name)}记录：${esc(item.title || item.label || item.id)}</h3><p class="warning-note">恢复后该记录重新显示在当前工作区。已批准记忆恢复后会重新进入共享检索。关联任务、报告和来源如已删除，需要先恢复；此操作不创建、搬动或覆盖文件。</p>`, '确认恢复记录',true);
        } else {
          modal=recordModal(context,'<h3>预览删除影响</h3><p role="status">正在核对记录与关联影响，可取消…</p>','确认删除记录');
          let preview;
          try{preview=await api('/api/collaboration/record_delete_preview',{body:payload});}
          catch(error){if(currentRecordDialog(context))modal.querySelector('#co-record-error').textContent=error.message || String(error);throw error;}
          if(!currentRecordDialog(context))return;
          if(preview?.entity_type!==type || String(preview?.entity_id)!==context.id || preview.files_preserved!==true || preview.recoverable!==true){
            modal.querySelector('#co-record-error').textContent='删除预览不完整，操作未执行。请取消后重新预览。';
            throw new Error('删除预览不完整，操作未执行。请重新预览。');
          }
          context.token=preview.preview_token;
          modal=recordModal(context,deletePreviewHTML(preview,type),'确认删除记录',preview.can_apply===true && !!context.token);
          if(preview.can_apply!==true || !context.token)return;
        }
        modal.querySelector('#co-record-confirm').onclick=()=>act(async()=>{
          if(!currentRecordDialog(context) || context.running)throw new Error('预览已失效或工作环境已切换，请重新预览。');
          context.running=true;
          const confirm=modal.querySelector('#co-record-confirm'),cancel=modal.querySelector('#co-record-cancel');confirm.disabled=true;cancel.disabled=true;
          modal.querySelector('#co-record-error').textContent='请求已提交，正在等待服务确认；关闭窗口不会撤回请求。';
          try{
            const result=await api('/api/collaboration/'+(restoreRecord?'record_restore':'record_delete_apply'),{body:restoreRecord?payload:{...payload,preview_token:context.token}});
            if(result?.[restoreRecord?'restored':'applied']!==true || result.entity_type!==type || String(result.entity_id)!==context.id || result.files_preserved!==true)throw new Error('服务未确认记录操作，请刷新核对；当前列表已保留。');
            if(!active() || state.root!==context.root)return;
            if(recordDialog===context)closeModal();
            get('search-results').innerHTML='';searchMemories=[];
            if(type==='source')get('source-results').innerHTML='';
            await refresh();
          }catch(error){
            if(currentRecordDialog(context)){modal.querySelector('#co-record-error').textContent=error.message || String(error);confirm.disabled=false;cancel.disabled=false;context.running=false;}
            throw error;
          }
        });
      }
      function taskDetail(id) {
        const task=rows(state.tasks).find(t=>String(t.id)===String(id));el.dataset.coTask=task?String(task.id):'';
        get('task-detail').innerHTML=task?`<h4>${esc(task.title)}</h4>${reportSubmissionHTML(task)}${executionDetailHTML(task)}<pre class="co-prose">${esc(task.description)}</pre>${task.summary?`<h4>最近交接 / 完成说明</h4><pre class="co-prose">${esc(task.summary)}</pre>`:''}<dl>${Object.entries(task.paths || {}).map(([k,v])=>`<dt>${esc({work:'任务工作目录',reports:'正式报告',outputs:'交付产物',temp:'临时文件'}[k] || k)}</dt><dd class="pathline">${esc(v)}</dd>`).join('')}</dl><p class="caption-note">创建目录不表示任务已被领取。请从「工作端接入」配置客户端，再让工具领取此任务。</p>`:'';
      }
      function renderResources() {
        const result=resourceResult || state.resources || {items:[],total:0,counts:{}};
        const tasks=new Map(rows(state.tasks).map(task=>[String(task.id),task.title || task.id]));
        for(const item of [...rows(state.resources),...rows(result)])if(item.task_id)tasks.set(String(item.task_id),item.task_title || item.task_id);
        if(resourceTask && !tasks.has(resourceTask))tasks.set(resourceTask,resourceTask);
        get('resource-task').innerHTML='<option value="">全部登记任务</option>'+[...tasks].map(([id,title])=>`<option value="${esc(id)}">${esc(title)} · ${esc(id)}</option>`).join('');
        get('resource-task').value=resourceTask;el.dataset.coResourceTask=resourceTask;
        get('resources').innerHTML=resourcesHTML(result,{offset:resourceOffset});
        get('resource-detail').innerHTML=resourceDetailHTML(result,resourceTask);
      }
      async function refreshResources(taskId=resourceTask,offset=0) {
        if(!ready || !state.root)throw new Error('工作区不可用，请先配置工作环境。');
        const serial=++resourceSerial,contextRoot=state.root;
        const body={offset,...(taskId?{task_id:String(taskId)}:{}),_workspace_root:contextRoot};
        try {
          const result=await api('/api/collaboration/resource_list',{body});
          if(!active() || serial!==resourceSerial || state.root!==contextRoot)return;
          if(result?.workspace_root && result.workspace_root!==contextRoot)throw new Error('工作环境已切换，资源结果未应用，请刷新协作状态。');
          if(!Array.isArray(result?.items) || !Number.isInteger(result?.total) || result.total<0)throw new Error('任务资源列表不完整，保留原结果，请刷新核对。');
          resourceResult=result;resourceOffset=offset;resourceTask=String(taskId || '');
          renderResources();bindActions();
          get('resource-message').textContent='已读取当前工作区资源登记与客户端证据；没有执行资源收尾。';
        } catch(error) {
          if(active() && serial===resourceSerial){get('resource-task').value=resourceTask;get('resource-message').textContent='任务资源读取未完成，原列表已保留。';}
          throw error;
        }
      }
      function bindActions() {
        el.querySelectorAll('[data-co-action]').forEach(n=>{n.onclick=()=>{
        const id=n.dataset.coId,action=n.dataset.coAction;
          if(action.startsWith('go-')){selectTab(action.slice(3));return;}
          if(action==='report-check')return act(async()=>{
            if(!ready || !state.root)throw new Error('工作区不可用，请先配置工作环境。');
            const contextRoot=state.root;
            const result=await api('/api/collaboration/task_list',{body:{task_id:String(id),_workspace_root:contextRoot}});
            if(!active() || state.root!==contextRoot)return;
            const task=rows(result).find(t=>String(t.id)===String(id));
            if(!task)throw new Error('任务已变化或被移除，请刷新协作状态。');
            state.tasks=rows(state.tasks).map(t=>String(t.id)===String(id)?task:t);
            get('tasks').innerHTML=tasksHTML(state.tasks);
            if(String(el.dataset.coTask)===String(id))taskDetail(id);
            bindActions();
          });
          if(action==='delivery-retry-guide'){
            if(busy || !ready || !active())return;
            const item=rows(state.report_delivery?.items).find(i=>String(i.id)===String(id));
            if(!item){setError('提交记录已变化，请刷新协作状态。');return;}
            if(!modalAvailable()){setError('请在原工作端调用 aihub_report_outbox_retry，参数 id 为 '+String(id)+'，并提供当前任务领取凭据。');return;}
            const context={root:state.root,serial:++recordSerial,running:false};recordDialog=context;
            const request=JSON.stringify({tool:'aihub_report_outbox_retry',arguments:{id:String(id)}},null,2);
            const modal=recordModal(context,`<h3>重试报告提交</h3><p>请把下面的请求交给原工作端 ${esc(item.tool)}（${esc(item.client_id)}）。活跃 MCP 进程可使用内存中的领取凭据；进程重启后需提供该任务的当前凭据，不能从曜核界面代替领取者提交。</p><pre class="co-code">${esc(request)}</pre><p class="caption-note">这只是重试请求，复制后还未执行。重试沿用提交 ID，服务确认后才显示已登记，不重复创建报告或记忆候选。</p>`,'复制重试请求',typeof copyText==='function');
            modal.querySelector('#co-record-confirm').onclick=async()=>{
              if(!currentRecordDialog(context) || typeof copyText!=='function' || context.running)return;
              context.running=true;modal.querySelector('#co-record-confirm').disabled=true;
              try{await copyText(request);if(currentRecordDialog(context)){closeModal();get('message').textContent='重试请求已复制。请交给原工作端执行，再刷新提交状态。';}}
              catch{if(currentRecordDialog(context)){modal.querySelector('#co-record-error').textContent='复制未完成，请选中上方请求手动复制。';modal.querySelector('#co-record-confirm').disabled=false;}}
              finally{context.running=false;}
            };
            return;
          }
          if(action==='resource-task-detail')return act(async()=>{selectTab('resources');await refreshResources(String(id),0);});
          if(action==='resource-refresh')return act(()=>refreshResources(resourceTask,0));
          if(action==='resource-prev' || action==='resource-next'){
            const count=rows(resourceResult || state.resources).length,total=(resourceResult || state.resources)?.total ?? count;
            if(action==='resource-next' && (!count || resourceOffset+count>=total) || action==='resource-prev' && resourceOffset===0)return;
            return act(()=>refreshResources(resourceTask,action==='resource-next'?resourceOffset+count:Math.max(0,resourceOffset-200)));
          }
          if(action==='memory-from-report'){
            if(busy || !ready || !active())return;
            const report=rows(state.artifacts).find(a=>String(a.id)===String(id) && a.kind==='report' && a.status==='active');
            if(!report){setError('来源报告已变化，请刷新。');return;}
            selectTab('memories');get('memory-source').value=String(report.id);drafts.memory.edit();
            get('message').textContent='已选择来源报告。请核对适用范围并填写可复用结论，提交后仍需审核批准。';get('memory-content').focus?.();return;
          }
          if(action==='record-delete' || action==='record-restore')return act(()=>recordAction(n.dataset.coEntity,id,action==='record-restore'));
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
            drafts.source.edit();get('source-path').focus?.();return;
          }
          if((action==='approve' || action==='retire') && modalAvailable()){
            const item=rows(state.memories).find(m=>String(m.id)===String(id));
            if(!item){setError('记忆已变化，请刷新。');return;}
            return confirmImportant(`${action==='approve'?'批准':'退役'}记忆：${item.title}`,action==='approve'?'批准后，该内容会成为共享检索结果，可被接入工具作为工作依据。请核对来源报告、内容和适用范围。':'退役后，该记忆不再进入共享检索；记录和来源报告保留。',action==='approve'?'确认批准保留':'确认退役',()=>act(async()=>{await post('memory_review',{memory_id:id,status:action==='approve'?'approved':'retired'});if(active()){get('search-results').innerHTML='';await refresh();}}));
          }
          if(action==='pin' && modalAvailable()){
            const item=rows(state.artifacts).find(a=>String(a.id)===String(id));
            if(item?.pinned && item.kind==='temp')return confirmImportant(`取消临时文件保留锁定：${item.title}`,'取消锁定后，该文件如果已到期且所属任务已完成，可能在下次自动检查或手动回收时移入 Windows 回收站。请确认仍需要取消保留。','确认取消锁定',()=>act(async()=>{await post('artifact_pin',{artifact_id:id,pinned:false});if(active())await refresh();}));
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
            else if(action==='source-reconfirm'){
              const serial=++sourceSerial;
              get('source-confirm').innerHTML='<p role="status">正在核对来源目录的新旧身份…</p><button id="co-source-confirm-cancel" class="btn" type="button">取消</button>';
              const cancel=()=>{++sourceSerial;get('source-confirm').innerHTML='';};get('source-confirm-cancel').onclick=cancel;
              const preview=await post('source_reconfirm_preview',{source_id:id});if(!active()||serial!==sourceSerial)return;
              get('source-confirm').innerHTML=`<h4>重新确认来源目录</h4><p class="pathline">${esc(preview.path)}</p><p>旧目录身份</p><pre class="co-code">${esc(JSON.stringify(preview.previous_identity,null,2))}</pre><p>当前目录身份</p><pre class="co-code">${esc(JSON.stringify(preview.current_identity,null,2))}</pre><p>保留 ${esc(preview.retained_inventory_count??0)} 条历史盘点记录。确认后只更新目录身份，扫描时间清空；需要再次只读盘点才会读取当前目录。</p><label><input id="co-source-confirm-check" type="checkbox">我已核对位置及新旧身份，确认使用当前目录</label><div class="co-actions"><button id="co-source-confirm-cancel" class="btn" type="button">取消</button><button id="co-source-confirm-apply" class="btn primary" type="button" disabled>确认目录身份</button></div>`;
              get('source-confirm-cancel').onclick=cancel;
              const canApply=()=>serial===sourceSerial&&preview.can_apply===true&&preview.token&&get('source-confirm-check').checked;
              get('source-confirm-check').onchange=()=>{get('source-confirm-apply').disabled=!canApply();};
              get('source-confirm-apply').onclick=()=>{if(!canApply())return;return act(async()=>{
                const result=await post('source_reconfirm_apply',{token:preview.token});if(!active()||serial!==sourceSerial)return;
                if(!result.applied)throw new Error('服务未确认来源身份更新，请重新预览。');cancel();await refresh();get('message').textContent='来源目录身份已确认，历史盘点保留；请再次只读盘点。';
              });};
              return;
            }else if(action==='scan'){
              const result=await post('source_scan',{source_id:id});if(!active())return;
              get('source-results').innerHTML=inventoryHTML(result);
            }
            if(active())await refresh();
          });
        };});
      }
      let registryItems=[],toolsLoaded=false;
      function updateToolChoices(records){
        registryItems=records;
        for(const key of ['task-tool','source-tool']){
          const selected=(!toolsLoaded&&restored?.draft?.[key])||get(key).value||'any';
          get(key).innerHTML=Harness.options(records,{queue:key==='task-tool',emptyLabel:key==='task-tool'?'任意已启用工作端':'通用来源',emptyValue:'any',selected,extras:[]});
          get(key).value=selected;
        }
        toolsLoaded=true;
      }
      const registry=Harness.mount(el,{api,active,onRegistry:updateToolChoices,restored:restored?.harness});
      async function refresh() {
        const next=await api('/api/collaboration/status');if(!active())return;
        if(recordDialog && modalAvailable())closeModal();
        ++resourceSerial;resourceResult=null;resourceOffset=0;
        if(state.root && state.root!==next?.root || restored?.root && !state.root && restored.root!==next?.root)resourceTask='';
        state=next || {};ready=state.available===true;
        if(ready){
          const deliveryRoot=state.root;
          get('deliveries').textContent='正在核对本机提交记录…';
          try{const delivery=await api('/api/collaboration/report_delivery_status?_workspace_root='+encodeURIComponent(deliveryRoot));if(!active() || state.root!==deliveryRoot)return;state.report_delivery=delivery;}
          catch{if(!active())return;state.report_delivery={available:false,items:[]};}
        }
        get('deliveries').innerHTML=deliveryHTML(state.report_delivery);
        el.dataset.coRoot=state.root || '';
        for(const id of ['task','memory','source','policy'])drafts[id]=bindDraft(get(id+'-form'),'collaboration:'+JSON.stringify([state.root||'',id]),true);
        get('root').textContent=state.root || '尚未配置工作环境';
        get('availability').innerHTML=ready?'<p class="caption-note">统一协议 v'+esc(state.protocol_version || 1)+' · 仅展示最近记录</p>':'<p class="warning-note">工作区不可用。请到「工作环境」配置可访问的规范根目录。协作写入已停用。</p><a class="btn" href="#/workspace">配置工作环境</a>';
        get('tasks').innerHTML=tasksHTML(state.tasks);get('artifacts').innerHTML=artifactsHTML(state.artifacts);get('memories').innerHTML=memoriesHTML(state.memories);
        get('memory-guide').innerHTML=memoryGuideHTML(state);get('deleted').innerHTML=deletedHTML(state);
        renderResources();get('resource-message').textContent='';
        const previous=get('memory-source').value;
        get('memory-source').innerHTML='<option value="">选择已登记的报告产物</option>'+rows(state.artifacts).filter(a=>a.kind==='report' && a.status==='active').map(a=>`<option value="${esc(a.id)}">${esc(a.title || a.path)} · ${esc(a.id)}</option>`).join('');
        get('memory-source').value=previous;
        if(!policyDirty){get('policy-enabled').checked=state.policy?.enabled===true;get('policy-days').value=String(state.policy?.days ?? 7);}
        const lastRun=state.policy?.last_run;
        get('policy-history').textContent=lastRun?.finished_at ? '最近检查：'+timeText(lastRun.finished_at)+' · 已回收 '+(lastRun.recycled ?? 0)+' 项'+(rows(lastRun.errors).length?' · '+rows(lastRun.errors).length+' 项未完成，原文件保留':'') : '尚无自动或手动回收记录。';
        await registry.refresh();if(!active())return;
        get('source-candidates').innerHTML='<h4>可登记的来源建议</h4><p class="caption-note">选择后填入上方表单；提交登记后再点击只读盘点。未登记的来源不会扫描。</p>'+rows(state.source_candidates).map((s,i)=>`<p><span class="pathline">${esc(s.label)} · ${esc(s.path)}</span> ${button('source-fill',i,'填入来源表单')}</p>`).join('');
        if(!get('source-results').innerHTML && state.inventory)get('source-results').innerHTML=inventoryHTML(state.inventory);
        get('clients').innerHTML=table(['客户端 / 工具','协议','最近心跳'],rows(state.clients).map(c=>`<tr><td>${esc(c.name || c.id)} · ${esc(c.tool)}<span class="file-sub">${esc(c.id)}</span></td><td>${esc(c.protocol_version)}</td><td>${esc(timeText(c.last_seen))}</td></tr>`),'尚无协议心跳。发现应用安装不代表已连接。');
        taskDetail(el.dataset.coTask);requeueId=null;get('requeue-confirm').innerHTML='';previewReady=false;previewOffset=0;previewIds=[];get('preview').textContent='预览待回收文件';get('preview-results').innerHTML='';get('run').disabled=true;
        bindActions();
        if(ready){try{const result=await post('source_list');if(active()){state.sources=rows(result);get('sources').innerHTML=sourcesHTML(result);bindActions();}}catch(e){if(active()){get('sources').innerHTML=`<p class="dialog-error">来源列表读取失败：${esc(e.message)}</p>`;}}}
        if(active() && ready && resourceTask)await refreshResources(resourceTask,0);
      }
      function form(id, action, payload, clear=[]) {
        get(id+'-form').onsubmit=event=>{event.preventDefault();return act(async()=>{
          if(!ready)throw new Error('工作区不可用，请先配置工作环境。');
          const body=payload(),draft=drafts[id],submitted=draft.snapshot();await post(action,body);const changed=draft.changed(submitted);draft.saved(submitted);if(!active())return;
          if(!changed)clear.forEach(key=>{get(key).value='';});await refresh();
        });};
      }
      const value=id=>get(id).value.trim();
      form('task','task_create',()=>{const tool=value('task-tool');if(!Harness.canQueue(registryItems,tool))throw new Error('所选工作端已停用或不在当前登记中，请选择已启用的工作端。');return {project:value('task-project'),title:value('task-title'),description:value('task-description'),target_tool:tool,report_policy:value('task-report-policy')||'required'};},['task-title','task-description']);
      form('memory','memory_propose',()=>{
        const scope=value('memory-scope'),project=value('memory-project');
        if(scope==='project' && !project)throw new Error('项目范围的记忆需要填写项目名称。');
        return {title:value('memory-title'),content:value('memory-content'),scope,project:scope==='project'?project:'',source_artifact_id:value('memory-source')};
      },['memory-title','memory-content']);
      form('source','source_add',()=>{const tool=value('source-tool');if(!Harness.enabled(registryItems,tool))throw new Error('请选择已启用的工作端，或使用通用来源。');return {path:value('source-path'),label:value('source-label'),tool};},['source-path','source-label']);
      get('search-form').onsubmit=event=>{event.preventDefault();return act(async()=>{const result=await post('memory_search',{query:value('search-query'),project:value('search-project')});if(active()){searchMemories=rows(result);get('search-results').innerHTML=memoriesHTML(result,false);bindActions();}});};
      const savePolicy=()=>act(async()=>{
        const days=Number(value('policy-days'));if(!Number.isInteger(days) || days<1 || days>365)throw new Error('保留天数应为 1 到 365 的整数。');
        const draft=drafts.policy,submitted=draft.snapshot();await post('retention_policy',{enabled:get('policy-enabled').checked,days});const changed=draft.changed(submitted);draft.saved(submitted);if(!active())return;policyDirty=changed;await refresh();
      });
      get('policy-form').onsubmit=event=>{
        event.preventDefault();
        if(modalAvailable() && get('policy-enabled').checked && state.policy?.enabled!==true)return confirmImportant('启用临时文件自动回收','后台服务运行时每小时检查，符合条件的到期临时文件将自动移入 Windows 回收站。保留天数只影响新登记文件；既有已到期文件仍可能在下次检查时回收。','确认启用自动回收',savePolicy);
        return savePolicy();
      };
      get('policy-days').oninput=get('policy-enabled').onchange=()=>{policyDirty=true;previewReady=false;previewOffset=0;previewIds=[];get('preview').textContent='预览待回收文件';get('run').disabled=true;};
      get('preview').onclick=()=>act(async()=>{
        previewReady=false;previewIds=[];
        const result=await post('retention_preview',{offset:previewOffset});if(!active())return;
        get('preview-results').innerHTML=previewHTML(result);
        previewIds=rows(result).map(item=>item.id);previewReady=previewIds.length>0;
        previewOffset=Number.isInteger(result.next_offset)?result.next_offset:0;
        get('preview').textContent=previewOffset?'继续预览下一批':'重新预览';
      });
      const recycle=()=>act(async()=>{
        if(!previewReady)throw new Error('请先预览待回收文件。');previewReady=false;
        const result=await post('retention_run',{artifact_ids:previewIds});if(!active())return;
        get('run-results').innerHTML=`<h4>本次回收结果</h4><p>检查 ${esc(result.checked ?? rows(result).length)} 项，已回收 ${esc(result.recycled ?? 0)} 项。</p>${table(['文件','处理结果'],rows(result).map(r=>`<tr><td class="pathline">${esc(r.path)}</td><td>${esc(label(r.status))}<span class="file-sub">${esc(r.error || r.reason || '')}</span></td></tr>`),'本次没有回收文件。')}${rows(result.errors).map(e=>`<p class="dialog-error">${esc(typeof e==='string'?e:e.error || e.reason)}</p>`).join('')}`;
        await refresh();
      });
      get('run').onclick=()=>{
        if(modalAvailable()){
          if(!previewReady || busy)return;
          return confirmImportant(`将 ${previewIds.length} 个临时文件移入 Windows 回收站`,'此操作会实际处理预览中的文件。仅回收已完成任务中到期、未锁定且身份与内容未变的临时文件；失败会保留原文件。请确认不再需要这些临时文件。','确认移入回收站',recycle);
        }
        return recycle();
      };
      get('execution-access').onclick=()=>{
        if(busy || !active())return;
        let opened=false;
        try{opened=openNativeExecutionAccess()===true;}catch{}
        if(opened)get('message').textContent='已请求打开桌面接入窗口，请在该窗口完成配置。';
        else setError('请使用曜核桌面版，在托盘菜单中打开“协作接入”。浏览器页面不能创建或导出接入凭据。');
      };
      get('refresh').onclick=()=>act(refresh);
      get('resource-task').onchange=()=>act(()=>refreshResources(get('resource-task').value,0));
      el.querySelectorAll('[data-co-tab]').forEach(n=>{n.onclick=()=>selectTab(n.dataset.coTab);});
      selectTab(params?.get?.('tab') || restored?.tab);restore(el,restored);el.dataset.coTask=restored?.selectedTask || '';
      await act(refresh);
      if(active() && restored?.root && restored.root!==state.root){setError('工作环境已切换。已保留文字草稿，请核对项目与来源；原报告选择已清除。');get('memory-source').value='';}
      else if(active() && restored?.draft?.['memory-source'])get('memory-source').value=restored.draft['memory-source'];
    };
  }
  return {createPage,capture,restore,esc,rows,tasksHTML,reportSubmissionHTML,deliveryHTML,artifactsHTML,memoriesHTML,sourcesHTML,resourcesHTML,resourceDetailHTML,previewHTML,inventoryHTML,memoryGuideHTML,deletedHTML,deletePreviewHTML,openExecutionAccess,executionAccessHTML,executionDetailHTML};
});
