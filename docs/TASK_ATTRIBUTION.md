# 项目与报告：任务与提交客户端归属

报告页展示有资料的任务成果，不取代完整任务队列。未提交资料的任务仍在协作队列中查看。原始文件名、扫描目录和工作端类别不能证明是谁做了哪个任务。

## 登记文档字段

`GET /api/workcenter/documents` 和 `GET /api/workcenter/document` 公开同一组扁平元数据；仍使用现有文档分页。

| 字段 | 依据 |
| --- | --- |
| task_id | 产物登记所关联任务的稳定 ID |
| task_title / task_status | 同工作区任务记录的当前标题与 queued / active / completed 状态 |
| task_target_tool | 同工作区任务当前目标工作端，any 表示没有限定工作端 |
| task_owner_id | 仅 active 任务当前领取客户端 ID；排队或完成任务返回 null |
| task_owner_name / task_owner_tool | 当前领取客户端在同工作区的自报名称与工作端；目标工作端与登记不匹配或缺记录时返回 null |
| source_client_id / source_tool | 实际提交时持久化的客户端 ID 与来源工作端，任务交接不改写它们 |
| source_client_name | 当前同工作区、同 ID、且工作端匹配提交 source_tool 的客户端自报显示名 |

`source_client_name` 是当前客户端名，不是提交时姓名快照，也不认证真人身份。客户端重新 heartbeat 后可以改名，但稳定 `source_client_id` 不变。界面应同时展示名称与 ID，不能把多个 Codex 客户端合并成一个人。

MCP 桥接可用可选 `--client-name` 配置中文显示名（最多120字符），未设置时默认使用客户端 ID。它仍是客户端主动声明的别名，重复心跳沿用本次启动配置；不会读取原生会话、推断真实姓名或获得其他程序控制权。

任务当前领取者与原提交客户端分别展示。例如报告由 Codex 客户端 A 提交，任务随后转交 DSH 客户端 B：source_client_id 仍为 A，task_owner_id 在 B 领取后为 B，task_target_tool 为 dsh；不得用 B 代替报告提交者。

来源盘点、既有资料和旧数据库中缺失证据的字段返回 null。即使路径含 codex、用户名或任务标题，也不推断提交者或补造任务。旧 schema 只读兼容，不为查看报告执行数据库迁移。

## 查找与分组

- `task_id` 精确筛选稳定任务 ID。
- `source_client_id` 精确筛选实际提交客户端 ID。
- 对缺失 ID 的资料，显式筛选值为 `:unknown:`。冒号在合法客户端/任务 ID 中不允许，因此不会与真实客户端（例如合法 `__unknown__`）碰撞。空值仍表示不筛选。
- 关键词检索额外匹配任务标题、任务 ID、提交客户端当前名字与 ID；不读取任务描述、租约或原生客户端私有配置。
- facets.tasks / facets.submitters 仍使用 `{value,label,count}` 数组。任务标签含项目、任务标题和完整 ID；提交者标签含当前自报名和稳定 ID。同标题或同工作端不合并。
- facet 的数量来自完整允许目录的最终去重资料集合，不是当前页数量，也不是全部任务数量。缺失 ID 分别标记“未登记任务”与“提交者未知”。其他分类、覆盖统计和默认分页保持原行为。

## 数据边界

任务与客户端查询都按当前工作区 root 绑定。提交者名字只有 client.tool 等于产物保存的 source_tool 才可展示，跨工作区同 ID 或换绑工作端不能冒认历史作者。当前领取者 lookup 也限制工作区及目标工作端。

列表只批量读取必要任务字段及客户端 id/name/tool，无逐文档 SQL 查询；不会公开 description、lease_hash、lease_token 或客户端凭据。原分类、保留策略与登记快照失效标记不变。登记文件被替换时，归属字段是原记录的证据，不能被当作对新正文的验收。
