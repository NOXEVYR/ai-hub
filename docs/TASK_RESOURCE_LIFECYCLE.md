# 任务资源登记与收尾

曜核记录任务创建的临时页面、会话、服务、监听端口、临时目录与输出。第一版提供归属、收尾状态与客户端证据，**不直接操作浏览器，不终止进程，不删除目录**。接入、登记、客户端报告和本机独立核验是不同阶段。

没有连接或归属登记的旧窗口不能自动纳入。本机可用内存少，也不能据此归因给某个任务。客户端可记录带时间的采样；这不是系统资源监控器或已经实现的预算调度器。

## 接入顺序

1. 心跳登记客户端，再领取任务，保存本次 `lease_token`。
2. 创建资源后立即调用 `resource_register`，提交确切身份、归属和是否临时。不得登记其他任务或用户已有资源为本任务独占。
3. 客户端在完成、失败、取消、交接和异常恢复时，先重新核对身份，再收尾自己有权控制的独占临时资源。不按进程名批量终止，不全局关闭浏览器，不触碰其他窗口。
4. 调用 `resource_cleanup_report`，提交检查结果。缺权限、失联或归属不明时报告 `manual_required`，不能报告已关闭。
5. 调用 `task_finish`。任务先结束也不会丢失收尾资格：原登记客户端仍可用原领取凭据报告，但不能继续登记资源。任务结束不等于资源已收尾。

收尾不是普通产物软删除。列表移除不会释放内存，也不删除文件。正式输出始终保留；可回收产物仍走已有预览、回收站和保留策略。

## 三个接口

Python 入口：`aihub.collaboration_resources.execute(cfg, action, body, actor='ui')`。API 与 MCP 应复用既有实例鉴权、工作端登记及会话身份绑定，不能让调用方更换 `client_id`。三个动作均必须传 `_workspace_root`，切换工作区后重新连接。

### resource_register

必填：`task_id`、`client_id`、`lease_token`、`resource_type`、`identity`。必须是当前任务领取者。可选：`ownership`、`temporary`、`label`、`sample`。

| resource_type | identity 必需字段 | 边界 |
| --- | --- | --- |
| browser_tab | browser_id, session_id, tab_id | 三者一起区分客户端实际连接的会话与页面 |
| service_process | pid, process_started_at | PID 正整数，启动时间为带时区 ISO；不能仅凭 PID 操作 |
| listener | pid, process_started_at, host, port | host 仅 127.0.0.1 或 ::1，port 为 1–65535 整数 |
| temp_directory | path | 已存在的本任务 Temp 下普通子目录，不能登记整个 Temp |
| output | path | 已存在的本任务 Outputs 下普通文件，不允许硬链接或临时标记 |

路径不接受相对路径、上级跳转、网络路径和任何祖先重解析点。返回目录/文件身份额外含 `device`、`inode`，收尾证据必须保留它们。这里只登记文件系统身份，不扫描目录内容。

`ownership` 为 `task_exclusive`、`shared`、`user_owned`。省略时安全退回 `user_owned` 且 `ownership_declared=false`，界面应显示“归属未声明”，不能视为用户已确认。

`temporary` 为布尔值，默认 false。只有**明确任务独占、临时且不是 output**的项具备收尾资格。共享、用户资源、未声明归属和正式输出进入人工或保留状态。归属声明不是操作系统授权。

同一工作区的相同类型与身份去重，绑定任务、客户端、领取批次及归属；重复请求返回同一 ID，不能覆盖其他任务登记。不同 PID 启动时间视为不同进程实例。工作区最多 5000 项，不做自动遗忘。

可选 `sample` 示例：

```json
{"sampled_at":"2026-10-01T12:00:00+08:00","ram_available_bytes":8589934592,"vram_available_bytes":4294967296,"resource_ram_bytes":104857600}
```

读数必须有采样时间，为有界非负字节数。禁止保存命令行、环境变量、凭据、浏览器配置或页面内容。

### resource_list

可选 `task_id`、`client_id`、`offset`（0–5000），每页最多 200 项。UI 可以查看当前工作区总览；MCP 必须有绑定客户端，只能查看本人登记。

返回 `available`、`items`、`total`、`counts`（对当前筛选结果按状态计数）、`workspace_root`、`automatic_control:false`、`host_verified:false`。旧库没有资源表时返回空列表，不为了只读列表创建资源表。

每项包括：`id`、`task_id`、`task_title`、`project`、`task_status`、`task_removed`、`client_id`、`title`/`label`、`resource_type`、`identity`、`ownership`、`ownership_declared`、`temporary`、`cleanup_eligible`、`state`、`evidence`、`sample`、`last_evidence_at`、`message`、`created_at`、`updated_at`、`evidence_source`、`host_verified:false`、`automatic_control:false`。不返回领取令牌或其摘要。

状态：

| state | 含义 |
| --- | --- |
| running | 当前领取批次的独占临时项仍登记为运行 |
| cleanup_pending | 等待收尾；结束、取消、失败、释放、换领取者或软删除任务后，尚未报告的独占临时项动态显示此状态 |
| closed | **客户端报告已收尾**，携带身份匹配的不存在观测；不表示曜核独立核验 |
| cleanup_failed | 客户端报告收尾失败 |
| manual_required | 归属/权限不适合自动收尾，或客户端无法检查；output 应显示“保留输出” |

任务旧状态由读取时计算，不改写历史登记。`evidence_source` 为 `registration_only` 或 `client_report`。

### resource_cleanup_report

必填：原登记的 `task_id`、`client_id`、`lease_token`、`resource_id`、`state`、`evidence`。原令牌的摘要独立保留，即使任务已结束清除当前领取凭据，也只能由原登记者报告。交接后的新领取者不能冒认此前资源。

`state` 可为 `cleanup_pending`、`closed`、`cleanup_failed`、`manual_required`。`evidence` 必填 `identity`（登记响应中的完整对象）、`observed_at`（带时区 ISO）、`outcome`（`absent`、`present`、`not_checked`），可选 `detail`（1000 字以内，无凭据）。

```json
{
  "identity":{"browser_id":"iab","session_id":"task-run-uuid","tab_id":"42"},
  "observed_at":"2026-10-01T12:01:00+08:00",
  "outcome":"absent",
  "detail":"客户端重新读取已连接会话，该临时页面已不存在。"
}
```

`closed` 必须为独占临时项且 `outcome=absent`。共享/用户资源和正式输出只允许人工或失败报告。证据身份必须完全一致；不接受 PID 相同但启动时间改变，也不接受另一个 session/tab 或目录 inode。观测不得早于登记或覆盖更新的观测；同状态、同证据重试幂等，终态不可重开。

## 持久化与验证

使用现有 `collaboration.sqlite3`，复用工作区锁、事务、任务和 audit 表。增加资源表前以 SQLite backup API 备份已提交快照，包含 WAL；不复制裸数据库。已有资源表结构不兼容时保留原库并拒绝操作，不删除重建。领取凭据只保存摘要，不进入响应或 audit 明文。

合成临时夹具测试覆盖：旧库空列表、去重、完成后待收尾与原凭据报告、失败/取消/移除、交接与新领取批次隔离、跨任务/客户端/工作区、MCP本人列表、未知/共享归属、PID复用、监听端口、目录边界/重解析点/硬链接、输出保留、闭合证据及陈旧报告、采样验证、WAL备份与旧结构保护。没有对真实浏览器、用户进程或 F 盘项目执行清理。

## 仍需客户端接入

曜核不能强控所有 Codex、ZCode、DSH 或 WorkBuddy 窗口。每个客户端需使用自己的已授权接口登记和核验资源，接入前历史窗口仍可能没有记录。当前只建立通用协议及可见状态，不提供失联进程自动终止、浏览器批量关闭、系统预算调度或旧产物物理删除。
