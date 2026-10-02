# MCP 报告交付与待提交队列

新建任务默认 `report_policy=required`。工作端须主动调用 `report_submit`，提交分类报告及显式 `memory_candidates`（无长期结论用 `[]`），确认 `status=submitted` 后再调用 `task_finish`。缺少本次领取的有效报告时，后端拒绝完成并保持任务 active；原有任务和显式 optional 任务按其策略处理。报告不会自动成为已批准记忆。

内置 Codex、ZCode、DeepSeek Harness、WorkBuddy 与已登记的自定义工作端均走同一协议。先登记并启用工作端，再接入 MCP 或通用 CLI，领取任务后主动提交。登记、规则文件和心跳都不能拦截每次原生模型生成；现有原生任务须实际调用协议才有报告交付证据。新生成的接入说明包含此约定；已有用户 AGENTS.md / CODEBUDDY.md 不会被覆盖。

## 提交与状态

`aihub_report_submit` 接受现有报告参数及可选 `submission_id` UUID。省略时桥接器生成一个稳定 ID，在 HTTP 发送前将报告及显式候选暂存到当前用户的受限目录。工作区、client_id、tool 共同绑定队列；工作区切换后旧桥只能保留原队列，无法投递到新工作区。

首次成功响应保留报告 artifact 的 `id`、`path`、记忆候选字段等，新增 `submission_id`、`status=submitted` 与安全元数据 `delivery`。此时 `id` 仍是 artifact ID，`delivery.id` 是提交 UUID。失败响应只含队列元数据，不含报告正文、文件路径、租约或服务器原始错误。

| status / error_code | 意义与下一步 |
| --- | --- |
| submitted | 服务端响应确认同一提交 ID，或独立 submission_receipt 确认报告登记与当前文件快照仍有效；可以完成任务 |
| pending / network_unavailable、server_busy、response_invalid | 尚未确认提交；保留任务执行态，稍后显式重试 |
| pending / needs_lease | 桥重启后没有当前租约；原活跃桥可使用内存租约，或由 AI/用户通过 stdin 显式提供当前有效租约 |
| failed / rejected、task_unavailable、task_not_active、ownership_changed、submission_invalid、submission_conflict | 请求拒绝、任务/归属不允许提交、原报告已移除/改变或同提交 ID 的内容/类型冲突；先复查任务，不能盲重放 |
| failed / retry_limit | 该 ID 的总发送预算已用完；仍可复查服务端确认记录，不再发送 |
| failed / storage_unavailable | 安全暂存失败；尚未发出报告，需修复目录/权限/容量问题 |
| pending / endpoint_config_changed | 所选安装的端口配置在核对期间变化；保留原提交 ID，重新核对原回执 |
| failed / endpoint_config_invalid、endpoint_identity_invalid | 安装配置不可读或服务身份与所选安装不符；不扫描其他端口、不发送报告，先修复该安装的连接 |

每次显式交付最多发送 3 次，总计最多 8 次。仅网络异常、408、429、5xx 与无法确认的响应执行有限退避（0.1/0.3 秒）；单次 HTTP 超时 10 秒。每次发送前原子记录尝试数，跨进程非阻塞锁避免同一队列并发重放。没有无限线程、定时轮询或后台自动领取。

重试前刷新当前工作区绑定，以 `submission_receipt(task_id, submission_id)` 查询同一客户端的独立回执；只有 found=true 且 valid=true 才确认交付。回执不依赖 required/optional/legacy 策略，报告被删除、改写或文件快照失效时不确认。没有回执时用 `task_list(task_id)` 精确复查任务；只有任务仍 active 且归属当前客户端时才允许发送。已完成任务不使用旧租约重放。同一 ID 内容不可改变；重新领取后的报告应使用新 ID。

回执确认还必须匹配本次请求的规范化内容摘要、submission_action=artifact_write 与 kind=report。摘要不含租约、客户端、工作区、task_id 或 submission_id，包含所有实际报告字段和显式候选，并与服务端使用相同的 UTF-8/JSON 规范。成功正文清除后仍保留摘要；本地回执不存在或已淘汰时，重新暂存的请求也须完整匹配服务端摘要。同 UUID 的不同内容或 output 登记返回 submission_conflict，保留冲突暂存正文，不以旧报告代替本次提交。

## 查询与恢复

MCP `aihub_report_outbox_list({})` 列出当前工作区/client/tool 队列；`aihub_report_outbox_retry({"id":"提交UUID","lease_token":"当前有效租约"})` 只恢复该绑定下的一份报告。lease_token 可省略，当前活跃桥仅使用自己内存中的当前租约；重启后无租约不会发送报告或自动重新领取。重试成功可返回 artifact ID 与交付确认；复查成功的短响应不保证重复提供全部初次 artifact 字段，可调用 artifact_list 获取报告。

协作页面的只读列表使用 `tools.report_outbox.list_current(workspace_root)`，只返回当前工作区的 `id/client_id/tool/task_id/title/status/attempts/error_code`，不公开正文或暂存路径。页面不能替已断开的工作端制造租约。

通用 CLI 支持 stdin 文本 JSON，适用于任意可主动调用本机脚本的 harness：

```powershell
# 将参数对象由调用方在内存中生成并经 stdin 输入，勿把租约放到命令行或磁盘。
$arguments | python -B tools/aihub_mcp.py --install-root "所选软件安装目录" --client-id studio-local --tool studio-agent --action report_submit
'{}' | python -B tools/aihub_mcp.py --install-root "所选软件安装目录" --client-id studio-local --tool studio-agent --action report_outbox_list
$retryArguments | python -B tools/aihub_mcp.py --install-root "所选软件安装目录" --client-id studio-local --tool studio-agent --action report_outbox_retry
```

execution.3 候选的接入说明和配置助手使用 `--install-root`。桥接器在每次操作前读取该安装 `data/config.json` 的端口，并以直连本机 `/api/health` 核对应用、控制协议、实例及物理安装目录；核对后再读取端口防止配置期间变化。健康响应须声明 `mcp_endpoint_binding=aihub-mcp-endpoint/1`，业务请求随后带公开安装身份摘要和实例编号，服务端在读取业务正文前核对自身，身份变化、部分或重复绑定头均拒绝。另一个兼容 Hub 即使接管原端口也不能把任务当作自己的工作。它不是针对恶意同用户监听器的认证或操作系统隔离。

不会读取控制钥匙、探测其他端口、跟随 HTTP 重定向、启动服务或输出原配置。所选根目录先解析到物理目录；其 `data` 和配置文件拒绝重解析点及硬链接。离线仍明确失败，重连仍受原工作区会话约束。旧服务没有绑定声明时，新安装模式拒绝业务提交；需要升级后端或明确使用兼容端口方式，不能静默降级。

`--port` 仍保留为显式兼容方式，不能与 `--install-root` 同用；旧方式没有上述安装绑定检查。既有用户配置不被助手覆盖，升级已有接入需先备份并只调整对应服务器条目；桥进程须重新连接才能采用新启动参数。仅修改配置文件不代表当前 MCP 会话已切换。

`report_submit` 的 JSON 包含 task_id、当前 lease_token、category、title、filename、content、memory_candidates，可带 submission_id。`report_outbox_retry` 的 JSON 包含 id，可带当前 lease_token。提交/重试仅 submitted 退出码为 0，pending/failed 返回非零；stdout 为 JSON。离线首次绑定失败不会创建队列或声称在线。

## 本地安全边界

Windows 默认位置为 `%APPDATA%/AIHub/report-outbox`；其他平台为 `$XDG_DATA_HOME/AIHub/report-outbox`，未设置时用 `~/.local/share/AIHub/report-outbox`。目录限制为当前用户与 SYSTEM（Windows），或 0700/0600（其他平台）；拒绝符号链接、Windows 重解析点、硬链接文件、路径逃逸及超大文件。每绑定最多 32 份 pending/failed 记录，每文件最多 2 MiB，报告 UTF-8 正文最多 1 MiB。队列满时明确失败并保留已有失败正文。成功回执清除正文后有界保留最新 64 份；回执淘汰不会修改正式报告。

协作页列表仅读取每文件最前面的安全元数据行（最多 16 KiB），不解析正文；最多扫描 256 绑定、512 文件、8 MiB 元数据，返回 200 条，并设 250ms 扫描预算。达到预算时返回 truncated=true；遇坏记录保留其他正常项并返回 partial=true。其他工作区坏记录不会使当前正常项消失。

暂存只包含本次主动提交的报告与候选；不读取或保存工具原生历史、私有配置、认证、钥匙或 lease_token。不要在报告正文中附凭据。成功后清除暂存正文与候选，保留安全状态与内容摘要用于复检；失败报告继续保留，以便当前有效租约恢复提交。队列是本地交付证据，不代表软件原生生成被全局观察，也不代表模型执行通过。
