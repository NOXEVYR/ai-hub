(function(root,factory){
  if(typeof module==='object'&&module.exports)module.exports=factory();
  else root.AIHubModelGovernance=factory();
})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const esc=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const statuses={present:'现场存在',missing:'现场缺失',denied:'访问被拒',unavailable:'暂不可读',invalid_path:'路径无效',not_regular:'不是普通文件'};
  const reasons={cache_absence_placeholder:'零字节缓存占位候选',python_path_configuration_location:'Python 路径配置候选',small_test_fixture_location:'小型测试夹具候选',runtime_cache_binary_location:'运行缓存候选',generic_binary_unconfirmed:'通用二进制待确认',format_unconfirmed:'格式待确认'};
  function render(report,fmtSize){
    const s=report.summary,c=report.coverage;
    const metric=(label,value)=>`<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`;
    const problems=report.items.filter(i=>i.status!=='present'||i.state_conflict||i.size_conflict||i.intake!=='model_candidate'||i.identity_changed||!i.identity);
    return `<section class="panel"><div class="panel-head"><h3>${c.whole_scope?'当前来源索引':'仅当前批次'} · 只读观察</h3></div><div class="body"><p>观察时间 ${esc(report.observed_at)}。只核对已有索引路径；未遍历目录、读取权重内容或修改索引。${c.whole_scope?'已覆盖当前来源的全部索引行，不代表整盘模型。':'批次之间可能有相同文件，禁止相加作为全库占用。'} 托管来源当前不可用时可能不在此范围。</p><dl class="governance-metrics">${metric('索引行',s.indexed_rows)}${metric('已核实文件身份',s.identified_files)}${metric('已核身份文件长度',fmtSize(s.identified_file_bytes))}${metric('索引已知逻辑长度',fmtSize(s.indexed_bytes))}${metric('现场缺失 / 访问被拒',`${s.missing_rows} / ${s.denied_rows}`)}${metric('存在性冲突',s.state_conflict_rows)}${metric('同文件别名 / 硬链接组',`${s.same_file_alias_groups} / ${s.hardlink_groups}`)}${metric('非模型候选行',s.nonmodel_candidate_rows)}${metric('身份未知行 / 变化组',`${s.identity_unknown_rows} / ${s.changed_identity_groups}`)}${metric('索引大小冲突行',s.size_conflict_rows)}</dl><p><strong>${report.identity_coverage_complete?'本批文件身份覆盖完整':'本批存在未核实项，长度仅覆盖已核身份'}</strong>；这不是模型版本数、磁盘分配占用或可释放空间。索引大小未知 ${esc(s.indexed_size_unknown_rows)} 行，现场大小未知 ${esc(s.observed_size_unknown_rows)} 行。链接别名不等于独立副本；重复路径长度 ${esc(fmtSize(s.alias_path_bytes))} 不能算作可回收字节。</p><p>非模型候选仅按位置/大小/扩展名提示，未自动排除 .pth 或 .bin 权重。内容重复、配套关系、工作流依赖与推理可用性均未验证；可释放空间：<strong>未知</strong>。</p></div></section><section class="panel"><div class="panel-head"><h3>待核对条目（${problems.length} 条，展示前 100 条）</h3></div><div class="table-wrap"><table class="tbl"><thead><tr><th>索引路径</th><th>现场状态</th><th>核对提示</th></tr></thead><tbody>${problems.slice(0,100).map(i=>`<tr><td class="file-sub">${esc(i.path)}</td><td>${esc(statuses[i.status]||'待确认')}</td><td>${esc([i.state_conflict?'索引存在性冲突':'',i.size_conflict?'索引大小与现场不符':'',i.identity_changed?'观察期间身份元数据变化':'',i.status==='present'&&!i.identity?'文件身份不可取得':'',reasons[i.intake_reason]||''].filter(Boolean).join('；'))}</td></tr>`).join('')}</tbody></table>${problems.length?'':'<p class="caption-note">本批未发现上述诊断项；不代表可以删除任何文件。</p>'}</div></section><section class="panel"><div class="panel-head"><h3>处置前影响清单</h3></div><div class="body"><ol><li>先人工核对缺失、权限、身份与分类疑点，不自动修正索引。</li><li>若怀疑独立内容重复，另行核对完整内容；同名同大小不构成证据。</li><li>核对加载器、保存工作流、配套组件、训练恢复与项目保留要求。</li><li>记录全部路径反向映射、受影响引用与恢复方案。</li><li>完成以上核对后另行取得处置授权；本页没有删除、移动或清理操作。</li></ol></div></section>`;
  }
  function createPage({api,heading,fmtSize}){
    return async el=>{
      el.innerHTML=heading('模型治理诊断','显式读取当前索引的文件元数据，不刷新索引、不处置文件。','READ-ONLY REVIEW')+'<div class="filterbar"><button class="btn primary" id="governance-read">读取只读诊断</button><button class="btn" id="governance-next" disabled>继续下一批</button><span id="governance-status" role="status" aria-live="polite">尚未读取；每批最多 5,000 条。</span></div><div id="governance-result"></div>';
      const read=el.querySelector('#governance-read'),next=el.querySelector('#governance-next'),status=el.querySelector('#governance-status'),result=el.querySelector('#governance-result');
      let current=null,sequence=0,busy=false;
      async function load(continuation){
        if(busy)return;
        busy=true;
        const request=++sequence,previous=current;
        read.disabled=true;next.disabled=true;status.textContent='只读核对中…';
        let query='limit=5000';
        if(continuation&&previous)query+='&after_id='+encodeURIComponent(previous.coverage.next_after_id)+'&scope_token='+encodeURIComponent(previous.coverage.scope_token);
        try{
          const report=await api('/api/models/governance?'+query);
          if(!el.isConnected||request!==sequence)return;
          current=report;result.innerHTML=render(report,fmtSize);
          status.textContent=report.coverage.has_more?'当前批次已读完；仍有索引可继续核对（不累计跨批次大小）。':'本次范围已读完；仅是元数据观察，不是可删除结论。';
          next.disabled=!report.coverage.has_more;
        }catch(error){
          if(!el.isConnected||request!==sequence)return;
          current=null;next.disabled=true;
          status.textContent='读取失败：'+error.message+'。旧报告保留为历史观察；请重新读取首批。';
        }finally{busy=false;if(el.isConnected&&request===sequence)read.disabled=false;}
      }
      read.onclick=()=>load(false);next.onclick=()=>current&&load(true);
    };
  }
  return {render,createPage};
});
