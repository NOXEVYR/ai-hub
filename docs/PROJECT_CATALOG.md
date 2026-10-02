# 跨工具项目目录

曜核只索引目录、报告和真实登记关系。查看项目目录不会移动、删除、自动登记文件，不把目录名、来源工作端或运行进程当成作者、协作成员或执行验收。

## 目录角色

`GET /api/workcenter/projects` 默认只返回 `entry_kind=project`。四类分别展示，避免把日期下的临时工作目录、模板和报告来源都称为正式项目。

| entry_kind | 含义 | classification_evidence |
| --- | --- | --- |
| project | 明确的项目登记，或已有有效的曜核项目创建清单 | project_registry / creation_manifest |
| candidate | 一级目录发现、Codex 日期下的任务目录，或任务明确记录的项目目录；尚无项目登记证据 | directory_discovery |
| template | 完整目录段 template/templates/模板/项目模板（允许数字前缀），或 `_TEMPLATE_` 前缀提供模板角色线索 | directory_role_hint |
| source | 已登记资料来源，或既有报告的来源目录兜底 | source_directory |

模板判别只表示目录角色线索，不读正文推断用途。工作区内仅解释相对目录段，不解释工作区本身名称或工作区之外的祖先；工作区外的候选仅解释该候选目录自身名称。例如工作区位于系统 `Templates` 目录下，普通正式项目仍是正式项目。工作区内的 `40_Projects/Templates/Sample` 则保留模板分组角色。套用模板创建的正式项目不因登记中的 `template` 字段成为模板目录。注册在模板目录中的项目仍保留 `registry_id` 与 `registration_status=registered`，但按模板角色展示。

创建清单必须是本项目中的普通、非链接、单硬链接 `.aihub-project.json`，不超过 64 KiB，读取期间文件身份稳定；校验 `owner=AIHub.projects`、整数版本 1、绝对根路径与当前目录一致、名称一致、既有输入/输出结构、提示词入口和 `guidance_only` 工具结构。文件名本身不是证据。此清单是本地明确创建记录，不是鉴权或外部工具执行证明。`registration_status=created` 与注册库的 `registered` 分开。

`task_create` 中的项目字符串，以及产物的 `intake_status=registered`，都不等于正式项目登记。没有项目登记或合法创建清单时，有任务、有产物的目录仍是候选。

## 查询与计数

支持 `entry_kind=project|candidate|template|source|all`、`query`、`source_type`、原有 `tool`、`project_id`、文档类别/收录/分类状态、任务/提交者筛选，以及 `page`、`page_size`（1–100）。前端 URL 的 `kind=registered` 映射到 API 的 `entry_kind=project`。

显式 `project_id` 查询且未提供 `entry_kind` 时，在所有角色中查找，以保留报告到候选项目的旧深链。文档接口的 `project_id`、路径稳定 ID、既有 registry ID 和旧 `/api/projects` 接口均保留。人工显示名称只改变独立元数据，不把候选提升为正式项目。

先归并当前工作环境的整个已发现目录，再筛选，再分页。搜索匹配名称、目录、已收录文档标题与路径、文档的真实任务/提交者元数据，以及真实任务标题/ID。没有文档的目录也能按名称/路径搜索；多工具项目保留完整 `source_types`，不会因兼容 `tool=any` 而漏掉某一个来源类型。项目的文档数量和任务摘要始终是该项目的全量索引摘要，不缩成命中搜索的几篇报告。

响应包含 `items`、`total`（当前筛选的条数）、`page`、`page_size`、`workspace_root`、`entry_kind`、`catalog_total`、`counts`、`facets`、`coverage`。`counts` 固定为 `{project,candidate,template,source}`；`facets.entry_kinds` 与 `facets.source_types` 均为 `{value,label,count}` 数组。计数来自分页前、筛选前的整个已发现目录。来源类型允许多选关联，因此各来源计数之和可以大于 `catalog_total`。

全量是当前允许索引的全量，不是无限扫描整个磁盘。管理层目录/报告/知识清单预算、Codex 日期目录预算、失效来源和未扫描来源仍保留；遇到预算或读取失败明确展示覆盖不足。

## 每行摘要与证据范围

兼容 `id/name/path/origin/tool/document_count/report_count/status`，新增：

- `entry_kind`、`classification_evidence`、`registration_status`、`registry_id`、`project_type`。
- `source_types`：来自发现/盘点的工作端标签；表示元数据来源，不代表作者或参与协作。没有来源类型时为 `any`。
- `task_count` 与 `task_status_counts`：当前工作环境真实任务表的全量记录；`tasks` 最多 20 条，`task_summary_limit=20`，只含 `id/title/status/target_tool`。历史队列状态不等于工具当前在线或任务实际执行验收。
- `artifact_count`：通过任务目录边界校验、尚未回收的真实产物登记数量，包含图片等非文档产物；不是已验证完整内容的数量。`document_count/report_count` 只统计工作中心支持并已收录的文档。
- `linked_tools`：只收集真实任务的 `target_tool` 和真实产物的 `source_tool`，排除 `any`；`association_evidence=registered_tasks_and_artifacts_only`。空数组表示没有这些关联证据，不猜测作者、客户端或工具协作。
- `representative_document_id`：有可用文档时给出允许索引内的文档身份，可复用现有 reveal 接口。只有空目录的项目不凭目录路径新增打开权限；界面可复制路径。

`coverage.warnings` 为安全的人读字符串。`coverage.project_discovery` 包含管理层的 `partial/partial_locations/unavailable_locations`，`coverage.legacy_discovery` 包含报告的 `warnings/partial_locations/unavailable_locations`。同时保留 `project_discovery_partial`、`legacy_discovery_partial`、盘点来源状态及原有文档收录计数，避免把不可读取的登记库或预算截断呈现成正常空环境。

## 人工分类与重叠来源

人工文档分类保存在工作中心元数据库，与设备、文件 ID、大小、修改时间的身份快照关联；同路径换文件或身份改变后，不把旧人工分类传播给新文件，返回 `category_override_status=identity_changed` 与 `needs_review`。旧库只有路径标签、缺身份证据时只读返回 `legacy_unverified`，不会为查看而迁移，也不会声称当前人工分类有效。首次再次保存分类、需要增加独立证据表时，先通过 SQLite backup API 备份既有库。身份快照用于分类身份绑定，不替代已登记协作产物的 SHA-256 内容验证。

同路径重叠来源保留所有来源标签；同等级盘点记录优先选择身份仍有效的来源。真实提交证据仍比盘点记录优先：提交快照改变不能降级为普通扫描文档绕过提交边界。

验证：`python -B -m unittest tests.test_project_catalog tests.test_workcenter tests.test_workcenter_http tests.test_task_attribution tests.test_structured_submissions tests.test_management_coverage -v`。
