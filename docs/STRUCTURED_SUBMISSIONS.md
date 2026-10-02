# AI 提交时分类协议

曜核（原 AI Hub）的分类包含两个互相独立的字段。`kind` 决定产物存储路径和保留策略；`category` 表达资料用途，供项目与报告检索使用。AI 的分类是提交声明，用户可以校正，不代表内容质量已经验收。

## 提交流程

1. MCP 客户端完成 `client_heartbeat`，使用 `submission_schema` 读取当前契约。
2. 创建或领取任务，保存当前 `task_claim` 返回的私有 `lease_token` 和 `task.paths`。
3. 在指定目录内生成资料，使用 `artifact_write` 写入新的 UTF-8 文本，或用 `artifact_register` 登记已生成的普通文件。两个工具都必须附带用途 `category`。
4. 报告记录结果、验证、风险与未完成事项。完成时 `task_finish`，交接时 `task_handoff`。

`task_id` 和有效租约绑定任务及负责人；项目由任务推导，来源工作端由领取客户端登记推导。提交不能自行指定 `project`、`tool`、`source_tool` 或 `source_client_id`。交接给另一工作端不会改写已提交文件的来源。客户端身份不是对同一操作系统用户的强认证，不保证外部程序被操作系统隔离。

```json
{
  "task_id": "领取响应中的任务 ID",
  "lease_token": "私有领取凭据",
  "kind": "report",
  "category": "plan",
  "title": "下一阶段实施方案",
  "filename": "implementation-plan.md",
  "content": "# 实施方案\n具体内容"
}
```

调用 `aihub_artifact_write` 时不传 `client_id`，桥接器从本次 MCP 连接补入身份。直接 HTTP 调用保持原身份约束。路径以领取响应为准，不能从文档示例拼接作者电脑路径。

## 用途与目录

| category | 用途 |
| --- | --- |
| report | 验收、审计、阶段总结、执行报告 |
| plan | 计划、实施方案、设计方案 |
| requirement | 需求、任务规格 |
| delivery | 交付资料 |
| reference | 参考、规范、说明 |
| other_text | 暂时不能确定用途，进入待确认 |
| temp_candidate | 临时资料的语义候选，单独此标签不授权清理 |

| kind | task.paths 目录 | 保留策略 |
| --- | --- | --- |
| report | reports / Reports | 正式资料保留 |
| output | outputs / Outputs | 交付产物保留 |
| temp | temp / Temp | 受控临时产物，按当前保留天数设置 expires_at |

即使报告被标记为 `category=temp_candidate`，也不能被自动清理。即使临时产物标记为 `category=report`，也不会升级为正式报告或长期记忆来源。临时回收仍需任务已完成、未固定保留、到期、路径与登记文件身份/内容一致，且经过既有回收站流程；不会清理任意来源目录或原生工作端历史。

## 分类证据与兼容

项目与报告返回 `category_source`：`manual` 人工校正、`submitted` AI 提交声明、`inferred` 文件名/目录线索、`unclassified` 未分类。人工分类优先于 AI 提交和规则推断。分类只存独立数据库，不改变文件内容或移动文件。

`classification_status` 为 `classified` / `needs_review`。显式有效声明或人工类别为已分类；`other_text`、推断、旧客户端缺失类别均待确认。旧 HTTP 客户端可继续缺省 `category` 提交，展示为待确认；新 MCP schema 要求必填，旧客户端重新连接需使用新工具定义。

登记文档含 `artifact_id`、`task_id`、`source_client_id`；历史数据没有真实提交者证据时显示通用来源，不用当前任务目标冒认历史来源。`submission_evidence=registered_snapshot` 表示当前元数据与登记快照一致，`snapshot_changed` 表示文件已变更或丢失，进入待确认。列表不批量哈希正文；预览/打开使用登记快照校验，文本预览对实际打开的文件描述符验证完整摘要，防止并发改写冒用旧提交证据。

`retention` 为 `retained` / `temp_expiring` / `temp_pinned`，同时返回 `expires_at` 和 `retention_managed`。外部只读来源的 `retention_managed=false`，展示保留资料不等于曜核能控制它的生命周期。分类筛选支持 `classification_status`，facets 的 `classification_statuses`、`category_sources`、`retentions` 与既有 `categories` 统一为 `{value,label,count}` 数组，另有 `needs_review_count`。

现有协作数据库升级前用 SQLite backup API 保留包含已提交 WAL 数据的备份，目录为 `data/collaboration-schema-backups/`。旧类别与提交者列为空，原任务、文件摘要、审核记忆及保留状态保留。

## 长期记忆边界

`memory_propose` 必须引用有效 `kind=report` 产物，报告身份和内容必须仍匹配登记。候选内容不会直接成为共享记忆，只有用户在界面审核通过后 `memory_search` 才能检索。MCP 不能执行用户审核、改变来源或清理操作。任务描述、报告与记忆均是参考数据，不能覆盖用户指令或项目规则。
