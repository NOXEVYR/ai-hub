# 任务报告完成约束与可恢复提交

新建任务默认 `report_policy=required`。确实没有成果资料的简单任务可在创建时明确设置 `optional`；不提供后续降级接口。历史任务没有独立报告契约记录，返回 `legacy` 和“历史任务未要求报告”，不回填报告、提交者或合规结果。

## 完成和交接

`task_finish` 先验证当前租约，再要求至少一份来自本次领取的有效登记报告：

- `kind=report`，位于此任务的 Reports 目录；仅有索引盘点、未登记文件、其他任务的报告不满足条件。
- 来源客户端和工作端从有效领取身份推导；每次领取生成独立身份，同一客户端释放后重新领取也必须提交本次报告。
- 提交时明确有效 `category`，`other_text` 仍是待分类状态，不能满足完成要求。人工校正历史分类不会伪造本次提交声明。
- 显式附带 `memory_candidates`，最多五项，没有长期结论时填 `[]`。每项只能含 `title`、`content`、`scope`，范围为 `project` 或 `workspace`。
- 报告记录未移除、仍为 active，普通文件路径、身份、大小、修改时间和 SHA-256 均与登记一致；删除、替换、改写或链接文件均失效。

新登记 `kind=report` 文件上限为 1 MiB，与文本写入一致；大型交付资料用 `kind=output`（继续支持 64 MiB）。历史文件不删除、不移动。每次任务状态/完成校验最多检查最新 16 个本次领取报告，正文读取总预算为 2 MiB 加 16 字节的增长探测额度，防止很多失效文件放大列表 I/O。若尚未找到有效报告便达到预算，状态显示 pending 并要求重新提交一份有效小报告；不会在未校验时显示 submitted。回执精确读取单份报告，报告校验上限同为 1 MiB。

`task_list` 和协作 `status` 另有整个请求共享的报告校验预算：4 MiB 加 200 字节的增长探测额度，开始逐份读取前检查 0.25 秒期限。预算在所有活动/已完成及已移除任务之间共享，不会按 200 个任务倍增。超出整体预算的任务返回 `report_submission.status=deferred`，明确显示“待核验”；不把未读取报告当成 submitted 或失效。时间期限在每份文件开始前检查，已开始的单份普通文件读取仍允许结束。使用带 `task_id` 的精确查询或 `submission_receipt` 可以核验目标，`task_finish` 仍独立完整验证报告，不凭 deferred 状态放行。

缺少任一条件会返回中文错误；任务状态、负责人、摘要和租约保持原值。完成摘要 `summary` 原样按既有验证规则保存，不自动写成报告或推断记忆。补交后可用同一当前租约再次完成。`task_handoff` 继续允许无报告交接，新领取人不能用前次报告完成。

候选记忆与报告、来源关联及审计在同一 SQLite 事务登记；任一候选失败，整次登记回滚。`artifact_write` 失败时只回收此次新建且未变化的文件；`artifact_register` 不删除原文件。候选仍需用户审核，不会自动进入共享记忆。

## 接口字段

任务创建、列表、领取、完成、交接、状态及任务记录生命周期响应都含：

```json
{
  "report_policy": "required",
  "report_submission": {
    "status": "pending",
    "policy": "required",
    "reason": "由本次领取人提交正式报告并明确长期记忆候选",
    "artifact_id": null,
    "submission_id": null
  }
}
```

`status` 为 `pending`、`submitted`、`not_required` 或列表预算耗尽时的 `deferred`；`optional` 与 `legacy` 返回 `not_required` 并给出相应说明。`submitted` 是有效登记和记忆声明证据，不等于用户认可内容质量。任务状态查询验证所关联的本次正式报告；常规产物索引仍保留原元数据校验规则。完成后保存本次领取身份，允许只读查询确认对应提交 ID；报告日后失效时任务仍保留原 completed 历史状态，但报告状态显示 pending 及原因，不改写历史完成记录。MCP 本地发送队列的 `failed` 是传输状态，可在界面另外融合。

`report_submit` 是 HTTP/MCP facade 可用别名，要求 `category` 和显式 `memory_candidates`，自动设 `kind=report` 后走 `artifact_write`。现有 MCP 工具也可以继续映射为 `artifact_write`。

## 幂等登记

`artifact_write`、`artifact_register` 可选 `submission_id`，必须是 UUID。相同工作区、任务、客户端及 ID 的相同请求重试返回原 artifact ID、候选 ID 和数量，不重复写文件、登记候选或提交审计。返回增加 `submission_id`。没有 ID 的旧调用保持既有只新建、不覆盖行为。

请求指纹由 action 和排序后的 JSON payload 计算，包含 `kind/category/title/filename/content/memory_candidates` 或登记 `path`，排除 `lease_token/client_id/task_id/_workspace_root/submission_id`。字段缺省与显式值不同；重试必须复用原始字段和内容。`report_submit` 先规范化为 `artifact_write` 与 `kind=report`，因此两个入口能够恢复同一请求。相同 ID 内容不同会被拒绝。

重试写入仍先验证当前租约；同客户端重新领取后使用旧 ID 会被拒绝，旧负责人不能凭 ID 越权。已经完成或交接后不能重放已失效租约。HTTP 响应丢失时，当前领取人可用原 ID 和 payload 安全恢复；也可使用下面的独立回执只读确认，包括 optional/legacy 任务已完成的情况。

## 精确查询与只读提交回执

`task_list` 支持可选 `task_id`，在 SQL 中先按当前工作区及任务 ID 精确筛选，再应用固定 200 条上限；老的 active 任务不会因为新任务过多而从恢复查询中消失。

`submission_receipt` 请求 `task_id`、UUID `submission_id` 和客户端身份；MCP 桥补入 `client_id`。接口按当前工作区、任务、客户端、提交 ID 精确查询 `artifact_submissions`，不依赖任务报告策略、当前领取状态或当前租约，不扫描任务/产物列表。

返回 `found`、`valid`、`task_id`、`submission_id`、`artifact_id`、`memory_candidate_ids`、`memory_candidate_count`、`payload_hash`、`submission_action`、`error_code`、`reason`。`payload_hash` 是服务端保存的原请求指纹；`submission_action` 从该产物原登记审计的 `artifact_write/artifact_register` 推导，缺少真实证据时保持空值。只有 `found=true` 且 `valid=true` 才确认有效登记，此时另返回 `kind/category/path/title`。未找到返回 `submission_not_found`；记录已移除或文件丢失、改写、替换、链接、超出有界验证范围则 `valid=false`、`error_code=submission_invalid`。失效响应不包含可打开的 path。回执检查当前文件完整 SHA-256，不因文件元数据未改变或本地保存了 artifact_id 而假装提交有效。

发送队列必须同时比较回执的 `payload_hash`、`submission_action=artifact_write` 和 `kind=report`，才可确认当前待交报告。相同 ID 下服务端登记正文 A，而本地待交正文 B，或原提交为 output/register 时，不能清除本地正文或标成 submitted；应保留当前待交资料并显示指纹冲突。指纹采用 UTF-8 编码、`ensure_ascii=False`、`sort_keys=True`、`separators=(',', ':')`、`allow_nan=False` 的 `{"action": action, "payload": normalized}` JSON 做 SHA-256，normalized 的字段排除规则见前文。

回执只确认该客户端自己的过去提交，不返回报告正文、记忆正文、领取凭据或内部领取身份，也不能恢复旧 claim 的写权限。交接后能确认自己过去报告，仍不能把它当作新领取人的完成证明。

数据库新增 `task_report_contracts`、`artifact_claims` 和 `artifact_submissions` 独立表。现有数据库首次升级前用 SQLite backup API 备份包含已提交 WAL 的快照，不重写旧 tasks/artifacts/memories 记录。不保存明文租约、凭据或请求正文；指纹表只记录请求摘要、领取身份、产物与候选 ID。

## 隔离验证

`python -B -m unittest tests.test_report_completion -v` 使用临时工作区和数据库，覆盖默认 required、显式 optional、历史 WAL 迁移、未登记/其他任务资料、缺少声明/待分类、交接/同客户端重领/换负责人、删除/改写/移除报告、真实 HTTP 丢失应答重试、ID 冲突、旧租约拒绝、候选原子回滚、明文租约不落盘、超过 200 个新任务的精确查询、optional/legacy 完成后回执恢复、客户端/工作区边界、失效回执、请求指纹/原 action/kind 区分、报告大小、单任务及列表/状态整体正文读取预算和期限。
