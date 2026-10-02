'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const co=require('../frontend/collaboration.js'),lib=require('../frontend/capability-library.js');

test('report status keeps legacy, optional and required evidence separate',()=>{
  assert.match(co.reportSubmissionHTML({}),/历史任务/);
  assert.match(co.reportSubmissionHTML({report_policy:'optional'}),/报告可选/);
  assert.match(co.reportSubmissionHTML({report_submission:{status:'pending',policy:'required'}}),/报告待提交/);
  assert.match(co.reportSubmissionHTML({report_submission:{status:'submitted',policy:'required'}}),/报告已登记/);
  assert.match(co.reportSubmissionHTML({report_submission:{status:'deferred',policy:'required',reason:'本次校验预算已用完'}}),/报告待核验/);
  assert(!co.reportSubmissionHTML({report_policy:'required',title:'<script>'}).includes('<script>'));
});
test('delivery table never exposes payloads or leases, and offers retry guidance only for unconfirmed entries',()=>{
  const html=co.deliveryHTML({items:[{id:'id',title:'<bad>',task_id:'task',tool:'codex',client_id:'client',status:'failed',attempts:3,error_code:'needs_lease',content:'private report',lease_token:'private lease'}, {id:'done',title:'已交付',status:'submitted'}]});
  assert.match(html,/提交失败 1/);assert.match(html,/已确认登记 1/);
  assert.match(html,/重试指引/);assert.match(html,/当前领取凭据/);
  assert.equal((html.match(/data-co-action="delivery-retry-guide"/g)||[]).length,1);
  assert(!html.includes('private'));assert(!html.includes('<bad>'));
  assert.match(co.deliveryHTML({items:[]}),/暂无记录不能证明/);
  assert.match(co.deliveryHTML({available:false}),/暂不可读/);
  assert.match(co.deliveryHTML({items:[],partial:true}),/列表可能不完整/);
});
test('new environment names display restart and association evidence independently',()=>{
  const html=lib.credentialsHTML([{name:'MY_VIDEO_KEY',needs_restart:true,configured_in_system:true,runtime_available:false,sources:['windows_user'],association_status:'unassociated',value:'hidden'}]);
  assert.match(html,/MY_VIDEO_KEY/);assert.match(html,/后台需重启/);assert.match(html,/未关联工作端/);assert.match(html,/密钥有效性未验证/);assert(!html.includes('hidden'));
  assert.match(lib.credentialStatus({configured_in_system:null}),/系统范围未知/);
  const linked={name:'video',source:'registered_capability_catalog',client_online:true,required_env_vars:['MY_VIDEO_KEY']};
  const evidence=lib.interfaceEvidenceHTML(linked);assert.match(evidence,/近期协议心跳/);assert.match(evidence,/调用：尚未验证/);assert.match(evidence,/MY_VIDEO_KEY/);
});
test('installation endpoint failures explain preserved delivery without exposing configuration',()=>{
  for(const [code,message] of [['endpoint_config_invalid','所选安装的连接配置不可用'],['endpoint_identity_invalid','服务与所选安装不匹配'],['endpoint_config_changed','连接配置已变化']]){
    const html=co.deliveryHTML({items:[{id:'original-submission',task_id:'original-task',title:'待恢复报告',status:'pending',error_code:code,content:'private report',install_root:'private install',lease_token:'private lease'}]});
    assert.match(html,new RegExp(message));assert.match(html,/重试指引/);assert(!html.includes('private'));
    assert(!html.includes('已确认登记 1'));
  }
});
