# 可选协作执行接口候选

协议 `aihub-execution/1`，作为可选协作候选持续验收；源码、固定包、本地安装验证与公共发布分别记录，不能由本文件推断某台电脑的安装状态。当前整合候选及验收层次见 [2.13.10 整合说明](RELEASE_2.13.10.md) 和 [发布验收](USABILITY_RELEASE_GATE.md)。与旧只读 `aihub-interop/1` 和旧 `capability_dispatch` 分开；旧派单的含义不改变。映序管理原项目和轮次，棱光管理生成任务与成果，曜核管理冻结请求、精确队列、分类报告与经审核的共享记忆。三款软件独立运行，合作必须显式接入。

## 接入与身份

所有路径均为 `127.0.0.1` 当前服务上的 `/api/execution/<operation>`。`GET /describe` 无查询参数，是只读观察，不初始化数据库；其余操作为 `POST` JSON，不接受查询参数、重复字段或重复 Authorization 头。合作正文不接受凭据或服务身份；仅本机所有者管理正文使用下述实例/安装身份。

本机所有者使用 `POST /grant_create`，正文 `{_workspace_root, role, subject, instance_id, install_root}`，并带本安装实例私有 `X-AIHub-Control-Token`。Handler 核验控制身份后剥离 instance/install 字段并注入可信 owner 上下文，正文 owner 标记无效；缺少、重复、伪造控制头以及浏览器 Origin/Sec-Fetch 请求均拒绝。这是本机管理通道，不把控制凭据交给合作软件；合作接入只得到独立 scoped grant。

- `source`：subject 为来源软件的持久 authority，接受、查询、取消其原请求。
- `source_read`：同 authority 的历史回查，不接受新请求。
- `worker`：subject 为已经登记的精确 client_id，领取和上报该客户端的执行。

返回 `grant_id,role,subject,token,authority_id,ledger_epoch`。token 仅此响应一次显示，库中仅保存随机 256 位凭据的 SHA-256；接入方须私有保存，不进入模型提示词、报告、源码、分享包或输入摘要。授权管理不接受 Bearer，仅本机管理通道可用。`POST /grant_revoke { _workspace_root,grant_id,instance_id,install_root }` 同样需要私有控制头，禁止凭据继续调用新执行接口，不停止原生生成或废止已领取任务的旧报告租约；新上报可由相同 subject 重新授权后携原租约继续。授权不会自动过期，须显式撤销。

execution.4 候选增加所有者只读 `POST /grant_list {_workspace_root,limit?,after_grant_id?,instance_id,install_root}`，同样需要本安装控制头、拒绝 Bearer/浏览器/重复头。只列当前 root/binding 的授权，包含已撤销项；按 created_at/id 升序，默认 20、limit 只接受 1–50 整数。返回 `{protocol,authority_id,ledger_epoch,items:[{grant_id,role,subject,created_at,revoked_at}],has_more,next_after_grant_id}`；无钥匙、摘要、路径、输入或控制凭据。未知游标 404、其他范围 403，非当前绑定历史不在本列表。分页不是冻结快照，撤销始终依据明确 grant_id。

没有执行库或旧协作库尚无执行 schema 时返回两个身份 null 和空列表，不初始化或迁移数据库；不完整、损坏或不安全元数据明确失败，不伪空。原创建回复丢失/导出失败后，可按持久合作端身份、创建时间和编号在列表核对，然后显式撤销遗留授权；不能从列表重新取得 token，不能自动再建一把钥匙。subject 不接受独立 43 字符随机钥匙形态或 64 位十六进制钥匙形态文本。

合作请求使用单一 `Authorization: Bearer <执行接入凭据>`；不是桌面控制 token，也不是 provider key。Host/Origin 与这些接入凭据只约束合作接口，不是同操作系统用户的安全隔离。来源 authority 是用户登记的作用域，不能证明发布软件身份或原映序轮次此刻仍可写。映序适配器在提交前核对原轮次；跨应用瞬间一致性需要后续 reservation 协议。

本机管理可使用 `python tools/execution_admin.py --install-root <本安装目录> --workspace-root <当前托管根目录> --role source --subject <来源持久authority> --output <私有接入JSON绝对路径>`。工具在本机进程内核对控制文件、健康/工作区/实例后创建授权，独占写私有 ACL 文件，终端只报告保存位置；绝不导出后台控制 token。接入方仅导入这个 scoped 文件。撤销改用两项 root 参数与 `--grant-id <UUID>`。既存输出不覆盖，掉回复/保存失败记待核对，不自动创建另一钥匙。

execution.2 起桌面候选提供托盘“协作接入…”与协作页面“工作端接入 → 管理协作接入”。网页仅发送固定 `open-execution-access` 消息，原生入口沿用当前 WebView 来源核验，网页不传角色/subject/路径或凭据。原生窗口只读核对本安装工作区，选择角色和合作软件给出的持久身份后，使用系统保存窗口选择新私有 JSON，再以无 shell/无控制台子进程复用上述 CLI。创建/撤销只显示固定完成/待核对状态及用户选择位置，stdout/stderr 不解析、保存或显示；列表操作仅允许解析专用严格白名单的安全 JSON 元数据，不解析一般输出或错误流。所有者控制钥匙只在 CLI 进程内；导出只含单独 scoped grant。无工作区时引导配置工作环境；保存选择、撤销确认、描述核对和实际操作全程互斥，接入操作未结束时显式退出/更新保留窗口。撤销输入 grant_id 并原生二次确认，不自动停止原生任务。execution.4 的分页、选择后填入编号、刷新失败禁用陈旧选择及 Busy 保护已通过原生检查，三个尺寸离屏预览已核对；当前离屏/隔离流程不等于实机保存选择、合作软件导入或正式安装验收。

首次授权初始化稳定 `execution_authority_id`（授权响应中键为 `authority_id`）和 `ledger_epoch`。随后 `/describe` 返回这些字段以及可信 Handler 提供的 `identity/workspace/connection_revision`。authority、epoch 保存在协作账本内，重启不变化；端口、安装目录、本次 service_instance_id 不是稳定执行身份。工作区绑定仅覆盖本机目录身份，并非可移植路径或全配置版本。尚无账本时两个稳定字段为 null。

## 接受请求

`POST /accept`，source 凭据。正文必填：

```json
{
  "protocol": "aihub-execution/1",
  "request_id": "持久的小写规范 UUID",
  "origin": {
    "authority_id": "等于已授权来源 subject",
    "project_id": "原项目 ID",
    "task_id": "原任务 ID",
    "run_id": "原轮次 ID",
    "call_id": "原尝试 ID",
    "input_revision": "原冻结输入版本"
  },
  "_workspace_root": "仅私有本机连接核对路径",
  "workspace_binding_revision": "describe 的 workspace.binding_revision",
  "execution_authority_id": "describe 的持久 UUID",
  "ledger_epoch": "describe 的持久 UUID",
  "connection_revision": "本次 describe 的连接 SHA-256",
  "capability_id": "选中能力的 32 字符 ID",
  "expected_declaration_sha256": "interop 快照中 declaration.sha256",
  "input_json": "完整实际执行输入的原样 JSON 文本",
  "input_sha256": "上述原样文本 UTF-8 字节的 SHA-256",
  "hub_project": "Hub 队列归属的安全项目名",
  "title": "任务标题"
}
```

输入摘要计算前不能 trim、换行转换、NFC/NFD 转换或重新序列化。最大 16000 字节；重复 JSON 字段、非有限数、凭据、执行命令和未知 schema 输入都拒绝。它不等于映序原 run 的选型/摘要 digest；两者由来源适配器分别保留。 `_workspace_root` 只私有保存，不能作为公开/跨电脑身份。

能力 ID 在当前库绑定 root、发布 client 和 key；服务持有能力库声明锁，精确比较已存归一化声明字节 SHA，再派生目标 client/tool，调用者不能覆盖。服务器冻结完整声明，接受后重发/撤回不改写已接受意图。执行、队列任务、报告合同只写 `collaboration.sqlite3` 一个事务，没有跨库业务写提交。

唯一键为认证的 source authority + request_id，工作区、原任务整个范围、能力/声明、输入摘要、目标 client/tool、项目/标题及 authority/epoch 全部进入服务器业务指纹。连接修订、凭据、传输时间不进入指纹。同键同内容回复原 execution/task；同键不同内容 409。已有请求的回复恢复先核对冻结内容，不要求当前声明仍存在或连接修订仍相同；新键才核对当前连接与能力 CAS。

已取得收据的恢复请求可另带 `prior_execution_id`，它不进入业务摘要：现存记录必须与它一致，未知记录则直接 execution_unknown，绝不重新接受。客户端原收据存在而账本缺失时应优先 status 回查并保留 uncertain；没有收据的丢回复加旧备份回滚组合仍无法仅靠数据库内部 UUID 完整检测。

接受事务先保存固定 task UUID、目录、任务说明文本/SHA 和 `materialization_pending`。提交后独占创建说明，成功才 `queued_ready`。已接受但目录冲突仍返回原 IDs 与 `materialization_error`；可按原请求恢复，禁止另键创建。未知内容或已变说明不覆盖、不删除。当前半写说明会保持 pending 供人工核对，不能自动覆盖修复。

## 领取、原生执行与恢复

`POST /inbox {_workspace_root,limit?,after_execution_id?}` 仅 worker 凭据可读本人 publisher 和此工作区 binding 的未完成执行。limit 默认10、范围1–25，只接受整数。返回 `{protocol,items:[receipt],has_more,next_after_execution_id,claim_performed:false,native_work_started:false}`；receipt 与 status 相同，不含租约、冻结输入、声明正文或私有本机目录。仅 queued_ready/claimed 且队列 queued/active、未删除的任务可见；尚未物化、领取前取消、完成任务不返回。已报告原生终态但尚未提交分类报告/finish 的 claimed 任务仍可见，便于恢复 outbox。

按不可变 created_at 与规范执行 UUID 升序分页；只有 has_more 时返回最后一项 execution_id 作为下一页 after_execution_id。游标只能引用本人此工作区 binding 的执行，关闭的记录仍能用作分页位置，不能引用其他客户端或其他工作区记录。每轮从首项重新开始；页面间状态会变化，不代表冻结快照。读待办不领取、不续租、不触发生成。适配器须先持久保存 claim_request_id，再调用 claim；恢复已有执行沿用本地原 claim_request_id。不能靠 inbox 换键重做或接管其他客户端。

`POST /claim {_workspace_root,execution_id,claim_request_id}` 使用声明发布 client 的 worker 凭据。任务准备完且未取消才可领取。返回 receipt、`lease_token`、原任务 `task`、冻结 `input_json/declaration_text`。原生适配器先校验能力/输入/成本授权，再调用其现有生成服务。

领取回复丢失时用同一 claim_request_id 重试：保留报告 claim_id，轮换租约并返回 `lease_rotated=true`；旧租约立即失效。`status` 永不返回租约。旧 `task_claim/handoff/requeue` 不能领取或改派新执行任务；未完成执行也不能靠删除管理记录隐藏。普通旧任务保持原行为。

`POST /observe` 使用 worker 凭据及本次有效租约：

```json
{
  "_workspace_root": "本机绑定",
  "execution_id": "稳定执行 UUID",
  "lease_token": "领取响应私有租约",
  "observation_id": "本次观察持久 UUID",
  "provider_state": "submitting",
  "provider_request_id": "适配器已持久记录的原生请求编号",
  "results": []
}
```

先在本地原生调用意图账本保存请求编号，再上报 `submitting`，之后才发原生调用。状态只能 `not_started → submitting → running/uncertain/succeeded/failed/cancelled`；running/uncertain 可继续查询，同一执行不能换 provider_request_id，也不能回到 submitting 重做。未知不是失败、`poll_after` 不是未知。仅 failed 允许且必须另附无凭据的 `error_code`；仅 cancelled 允许且必须附 cancel_evidence，两种不能混填。provider_request_id、error_code 和取消证据 reference 都是最多 200 字符的无路径 ASCII 不透明身份/分类（字母数字、点、下划线、连字符和非路径冒号）；拒绝本机路径、URL/文件协议与凭据。

成功必须包含非空 results：每项 `result_id,kind,media_type,bytes,locator`，可选 `sha256`。kind 为 image/video/audio/document/other。result_id 为最多 200 字符、非空且无首尾空白的 Unicode 不透明文字身份，可含中文；不能含控制字符、未配对代理字符、路径分隔符或 URI/盘符前缀，`.`/`..` 也拒绝。media_type 是最多 200 字符的 ASCII MIME type/subtype，可带 token 或双引号参数（例如 `text/plain; charset=utf-8`），拒绝控制字节/无效类型，保留原文，不在摘要前归一化。locator 是适配器受控的不透明引用，不能是 URL、绝对路径或路径逃逸，Hub 不打开/下载它。结果按 result_id 排序后在终态冻结，身份为 execution_authority_id/execution_id/result_id，receipt 另含 results_manifest_sha256；同名文件或数组位置不是结果身份。媒体继续保存在原软件受控目录，不新增公共媒体库；无 SHA 不能宣称已校验实际文件完整性。

同 observation_id 同内容可读回原观察，改变内容则冲突；仅成果数组重排不改变身份或摘要。历史观察回复恢复不要求队列仍 active；新观察必须当前租约。取消证据和错误分类持久写入 receipt.outcome，重启仍可回查。provider 状态为 worker 的上报证据，`native_execution_verified_by_hub=false`；Hub 的队列完成、心跳或接口存在不能证明原生生成成功。领取前取消仅标 queue_ledger，不能记作 worker_report。

results_manifest_sha256 的规范字节为：数组按 result_id 的 Unicode 码点升序（不是界面区域排序或 JavaScript 默认 UTF-16 顺序），对象键升序、无额外空白、未转义非 ASCII 字符的 JSON，再编码严格 UTF-8。结果字段名为固定 ASCII，bytes 为 0 到 2^53-1 的整数。JSON 字符串保留原字符/大小写/MIME 参数；ASCII 控制字符按 JSON 规则转义，其他字符不作 NFC 或重写。对应 Python `json.dumps(results, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)` 后 UTF-8 SHA-256。客户端可保持服务返回数组的原顺序；若自行排序，不使用 localeCompare。

`POST /cancel {_workspace_root,execution_id}` 使用原来源凭据。领取前与 claim 共用事务，取消后禁止领取；领取后只置 `cancel_requested=true`，不停止生成、不释放租约、不宣称已取消。worker 仅在真实原生终态后上报 cancelled，并附 `cancel_evidence {kind:native_terminal,reference}`；submitting 且从未发原生请求可用 never_submitted。运行中/unknown 不能宣称 never_submitted。原生平台无定向取消能力时明确保留执行状态。

## 报告、记忆与原项目收件

worker 复用 `/api/collaboration/mcp/report_submit`，client_id 为本人，task_id 为 queue_task_id，并携有效 lease_token、稳定 submission_id、category、标题/文件名/正文、memory_candidates（可 []）。来源与负责人由领取推导。报告和候选同事务，正式报告默认保留，候选不自动批准。终态 + 本次领取的有效分类报告齐备后才可 task_finish；完成把 dispatch_state 记 completed，provider_state 独立保留。

`POST /status {_workspace_root,execution_id}` 或 `{_workspace_root,request_id}` 供所属 source/source_read 回查，worker 只能按自己的 execution_id；不能同时传两种 ID。返回 receipt，无租约、凭据、全局其他任务/私有配置。状态/取消和回查由 stable IDs 定位，不受原软件新轮次影响；映序是否可把成果关联旧轮次仍由本机 AIReceiptService 决定。映序显式关联实际文件并审核，不自动下载、采用或审核。

错误含稳定 code：invalid_request/input_digest_mismatch 400；credential_denied/scope_denied/owner_operation 403；status 时 execution_unknown 404，带 prior_execution_id 但缺失时 409；idempotency_conflict/declaration_changed/connection_changed/ledger_changed/claim_conflict/state_conflict/provider_identity_conflict/execution_identity_conflict 409；storage_unavailable 503。网络断开不等于接受失败，须查原 request。原收据存在而查不到时保留 uncertain，禁止自动新键重投。

authority/epoch 与去重墓碑随账本保留，临时产物回收不删账本。新建/遗失账本可检测 epoch 改变；任意旧备份回滚却保留同一 epoch 时尚无外部连续性锚，无法证明 exactly-once。恢复旧账本后应停派、核对原软件持久收据，不能把 not_found 解释为允许再做。

## 候选验收边界

当前隔离内核与真实临时 TCP 检查覆盖并发防重、CAS、掉回复/实例重启、目录提交间隙、错误 scope/客户端、旧入口绕过、租约恢复、分类报告/记忆候选和取消状态。尚待三产品实际适配器联调、受控原生成任务查询/结果身份、映序显式收件/审核、用户可见接入管理和最终本地安装验收。没有因为这些合成检查而宣称真实模型调用或正式发布已完成。
