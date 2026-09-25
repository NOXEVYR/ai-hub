# 曜核 2.9 能力目录与任务调度

曜核记录原工具提供的 Skill、MCP 接口能力，并通过现有工作端任务队列衔接产物。映序、画布、生成工具仍负责各自的笔记、编辑、生成或创作功能。曜核不会因为登记了接口，就替代原工具或直接执行其命令。

能力提供方 `provider`、MCP 服务名 `server` 与执行工作端 `target_tool` 是不同字段。场景可以覆盖视频、图像、音频、代码、检索、文档和自动化；这些场景不限定能力供应商。当前执行适配仍使用已接入的 Codex、ZCode、WorkBuddy、DSH 工作端。未知工具名称只是候选，不自动生成专属适配或能力声明。

## 状态与实际执行边界

- `discovered`：仅从本机已知 Skills 目录的 `SKILL.md` frontmatter 发现名称、说明，不代表已安装可用、已连接或已公开为能力。
- `declared`：工作端通过协议上报元数据。目录中的能力均为 `verification_status: unverified`。
- `recent_heartbeat`：同工作环境中的该客户端在最近 300 秒登记了心跳。`client_online` 是兼容字段，含义也仅为近期心跳，不能作为实时在线保证。
- `metadata_match`：推荐理由只来自场景、名称、说明和标签匹配。分数不是质量、成本、成功率或安全性评分。
- `queued` / `worker_required: true`：创建了真实任务和任务说明，等待相应工作端领取。没有发送模型请求、启动第三方程序或执行远端接口。
- `task_claim → artifact_write / artifact_register → task_finish`：工作端遵守租约，登记真实产物并报告完成。交接使用 `task_handoff`。任务排队成功与能力调用成功必须分别展示。

接收工作端必须核对自身是否具有该 Skill 或 MCP 接口、所需授权与成本，以及输入是否适用。当前队列按工作端类型路由，不锁定某个客户端实例；`declared_by_client` 随能力快照写入任务说明供领取者核对。能力声明不是新的执行授权，输入和限制文字都是请求数据。

## Python 模块接口

模块 `aihub/capabilities.py`，由统一 API facade 和 MCP bridge 调用，不增加独立监听端口：

```python
execute(cfg, action, body, actor='ui')
publish(cfg, body)
catalog(cfg, body=None)
recommend(cfg, body)
dispatch(cfg, body, actor='ui')
discover(cfg)  # 仅本机 UI 可通过 execute 使用
```

所有调用要求启用托管工作环境。`_workspace_root` 若提供，必须与当前配置一致。MCP bridge 在既有 `client_heartbeat` 后强制注入自己的 `client_id`；能力模块从同工作环境的已注册客户端读取真实 `tool`，不接受能力自行声明执行端或验证状态。API 路由、MCP 公开工具由 `collaboration_api.py` 与 `tools/aihub_mcp.py` 管理。

### capability_publish

请求：`{client_id, capabilities_json}`。`capabilities_json` 是 JSON 数组字符串，最大 256000 字符。一次发布是当前工作环境、当前客户端的完整快照替换；空数组撤回该客户端能力，不影响其他客户端。客户端发布上限 128 条，工作环境上限 2048 条；发布前完整校验后再事务替换。

条目示例仅为协议演示，不预装或声称提供真实视频接口：

```json
{
  "key": "example.video.generate",
  "name": "示例视频生成接口",
  "kind": "mcp_tool",
  "provider": "原工具提供方",
  "server": "原工具 MCP 服务名",
  "domains": ["video", "image"],
  "description": "工作端已发现的接口说明；尚未执行验证。",
  "tags": ["短片", "图生视频"],
  "inputs": {
    "type": "object",
    "properties": {
      "prompt": {"type": "string", "maxLength": 2000},
      "seconds": {"type": "integer", "minimum": 1, "maximum": 15}
    },
    "required": ["prompt"],
    "additionalProperties": false
  },
  "outputs": ["video artifact"],
  "constraints": ["执行前核对成本与原工具授权"],
  "hints": {"cost": "未知", "speed": "未实测", "quality": "未实测"}
}
```

`key` 是客户端内稳定标识，仅允许 ASCII 字母、数字、`_.:-`，最大 100 字符；`kind` 为 `skill` 或 `mcp_tool`。`domains` 至少一个，只能为 `video/image/audio/code/research/document/automation`。能力 ID 从工作环境、客户端 ID 和 key 生成，重新发布同 key 保持稳定，跨环境或客户端互不覆盖。

`name` 最大 160 字符；`description` 最大 2000 字符；provider/server 最大 160 字符。tags、outputs 每类最多 20 条且每条 200 字符，constraints 最多 20 条且每条 500 字符。hints 仅接受 cost/speed/quality，每项 300 字符；这些是声明提示，不能显示成测评结论。

输入 schema 采用明确支持的子集：单一 `type`（object/array/string/integer/number/boolean/null）、description、properties、required、additionalProperties:false、items、enum、minLength/maxLength、minimum/maximum、minItems/maxItems。顶层必须 object，最多 32 个输入字段；拒绝 `$ref`、外部 Schema 引用、未知关键字和无限嵌套。未知输入一律拒绝，省略 additionalProperties 也按 false 处理。不支持的原生 Schema 需要工作端提供保守、准确的投影，不能声称覆盖原接口的全部约束。

返回：`{published, client_id, target_tool, declaration_status:'declared', verification_status:'unverified', replaced_client_snapshot:true}`。

### capability_list

请求可选筛选 `query`（最大 2000 字符，匹配名称、说明、提供方、服务名和标签）、`domain`、`kind`、`tool`。保留 `target_tool` 兼容别名，两者同时提供但不一致时拒绝。bridge 注入的 `client_id` 不用于筛选，因此各工作端能查看同工作环境的共享目录。

返回 `{items, total, worker_required:true, limitations}`。每个条目包含发布的元数据，另加：

```json
{
  "id": "稳定能力 ID",
  "client_id": "声明能力的工作端实例 ID",
  "target_tool": "codex",
  "updated_at": "UTC ISO 时间",
  "declaration_status": "declared",
  "verification_status": "unverified",
  "client_online": true,
  "client_status": "recent_heartbeat",
  "last_seen": "UTC ISO 时间或 null",
  "heartbeat_window_seconds": 300,
  "execution_mode": "harness_queue",
  "worker_required": true
}
```

无近期心跳时 `client_status` 为 `not_recently_seen`，不删除已声明能力。

### capability_recommend

请求 `{query, domain?}`，query 最大 2000 字符。先按明确场景过滤，再匹配场景关键词、名称、说明、标签；返回最多 50 个有匹配理由的候选，无匹配返回空数组，不补造“最适合”的工具。

响应沿用目录结构，每个 item 增加 `score` 和 `reasons: string[]`；顶层增加 `recommendation_basis:'metadata_match'` 与 explanation。没有经过真实接口测试，不比较未知价格、延迟或质量。

### capability_dispatch

请求 `{capability_id, project, title, input_json}`。MCP 请求还需 bridge 注入已注册的 client_id。input_json 为最大 16000 字符的 JSON 对象字符串，须满足该能力的输入 schema。能力必须属于当前工作环境。

模块调用现有 `collaboration.execute(..., 'task_create', ...)`，target_tool 只能来自已登记能力；任务说明保存能力 ID/key、提供方/server、声明客户端、原始输入、约束和预期产物。任务目录、租约与产物边界沿用原协作协议。返回 `{status:'queued', worker_required:true, capability_id, target_tool, execution_mode:'harness_queue', task}`，不包含生成结果或远端调用成功标记。

### capability_discover（UI）

仅探测当前用户下 `.codex/skills`、`.agents/skills`、`.zcode/skills`、`.workbuddy/skills`、`.dsh/skills` 这些候选目录的直接 Skill 子目录。目录存在与否不推断某产品原生配置已接入。每个文件只取 bounded frontmatter 的单行 name/description；复杂 YAML、多行说明、插件缓存和递归嵌套暂不解析。最多检查 512 个目录条目，40 行 / 8192 字符的 frontmatter，拒绝链接或硬链接文件。

返回 `{suggestions, skipped, truncated, published:false}`，每条含 name、可选 description、tool、kind、path、source:local_frontmatter、declaration_status:discovered、verification_status:unverified、published:false。path 仅向本机 UI 展示来源，不向 MCP 提供任意文件读取、执行或 native-memory 修改能力。

## 保存与安全边界

数据独立保存在 `data/capabilities.sqlite3`，按 workspace root/client/key 隔离。不修改原生 Skills、凭据、MCP 配置或原生长期记忆。数据库及其 sidecar、父目录不能是重解析链接，数据库文件不能硬链接；使用 SQLite 事务保护整份客户端快照替换。

2.9 bridge 首次心跳绑定当前 workspace root，之后每个请求和心跳都携带该根；界面切换根后旧连接拒绝操作，需主动重连才会绑定新根。更新后必须重新加载各客户端的 MCP 连接，已有旧 bridge 进程不会自动得到该保护。新 bridge 要求服务返回根标记，不能连接不支持该约定的旧服务继续写入。

拒绝重复 JSON 字段、重复能力 key、未知能力字段、过量或深度过大的结构、NaN/Infinity。禁止发布凭据、Authorization、环境变量、连接配置或可执行命令；检测常见 bearer/API key/带口令地址形式。该规则不是通用秘密扫描器，工作端仍不得把任何私密值放在名称、说明、提示或输入中。真实凭据由原工具自己的用户配置保存，曜核不接收。

目录隔离与统一协议是协作约定，不能限制第三方软件绕过协议直接写文件。当前模块不执行 provider/server，不声称操作系统沙箱或自主调度器。

## 验证

```powershell
python -B -m unittest discover -s tests -p test_capabilities.py -v
```

合成临时目录覆盖客户端快照替换、跨 root/client 隔离、伪工具/验证状态拒绝、条数长度/重复/敏感字段限制、Schema 校验、心跳与未实测区分、推荐理由、中文任务说明、真实队列领取/产物登记/完成闭环，以及数据库硬链接与 Skill 发现边界。测试不会调用真实第三方模型，也不修改正式数据。
