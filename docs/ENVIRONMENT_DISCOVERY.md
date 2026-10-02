# 环境变量名称发现（2.13.9）

能力中心的环境发现只枚举名称，不读取、返回、日志记录或保存变量值，不扫描工作端私有设置、`.env`、凭据文件或会话。发现不创建登记库、不建立服务连接、不发送 API 请求，也不进行付费调用。

## 来源与刷新

| source id | 来源 | 实现 |
| --- | --- | --- |
| `process` | 曜核当前进程的 `os.environ` 名称快照 | 只迭代键，不读取值 |
| `windows_user` | `HKEY_CURRENT_USER\Environment` | `RegOpenKeyExW(KEY_QUERY_VALUE)` 和 `RegEnumValueW` |
| `windows_system` | `HKEY_LOCAL_MACHINE\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` | 同上 |

Windows 枚举调用的 `lpData`、`lpcbData` 和 `lpType` 均为 NULL，连值的数据长度与类型也不查询；只打开这两个环境键，句柄用完关闭。依据 [Microsoft RegEnumValueW 文档](https://learn.microsoft.com/en-us/windows/win32/api/winreg/nf-winreg-regenumvaluew)，无需数据时可以传入 NULL 数据指针；数据指针为空时，数据长度指针也可以为空。

每次刷新重新枚举持久环境名称，能发现启动后新建的名称。刷新不加载持久值到 `os.environ`。当前进程缺少、持久环境存在的名称标记 `needs_restart: true`，提示重新启动曜核并复查。由旧父进程启动的软件仍可能继承旧环境，重启提示本身不保证继承成功。同名变量的值是否已修改、为空、有效、用户/系统值的覆盖关系均无法从名称盘点判断。

用户或系统来源打不开、枚举出错时，`status: unavailable`；仅返回可选数值 `error_code`，不返回异常文本。非 Windows 平台这两个来源显示 `not_supported`，仍提供进程名称。

## 返回契约

`capability_discover` 原字段保留，新增顶层 `environment_inventory`：

```json
{
  "items": [{
    "name": "CUSTOM_MEDIA_KEY",
    "sources": ["windows_user"],
    "runtime_available": false,
    "configured_in_system": true,
    "needs_restart": true,
    "association_status": "unassociated",
    "provider_hint": "",
    "provider": "",
    "domains": [],
    "tools": [],
    "value_included": false,
    "capability_inferred": false,
    "discovery_only": true,
    "evidence": {
      "source": {"status": "name_observed", "type": "environment_variable_names", "sources": ["windows_user"]},
      "configuration": {"status": "persistent_name_present", "evidence": "Only names were enumerated; no values or value changes were checked."},
      "runtime": {"status": "name_absent", "evidence": "Current process snapshot; an empty or invalid value is not detected."},
      "callability": {"status": "not_established", "evidence": "Name presence does not establish a protocol connection or callability."},
      "actual_invocation": {"status": "not_observed", "evidence": "No API or model call was performed."}
    }
  }],
  "sources": [{
    "id": "windows_user",
    "type": "environment_variable_names",
    "location": "HKEY_CURRENT_USER\\Environment",
    "status": "scanned",
    "names_examined": 1,
    "names_rejected": 0,
    "value_included": false
  }],
  "total": 1,
  "returned": 1,
  "truncated": false,
  "query": "",
  "value_included": false
}
```

`sources` 实际包含三个来源，也追加至原顶层 `sources` 便于统一展示。来源状态是 `scanned`、`unavailable`、`not_supported` 或 `truncated_budget`。`total` 是此次有界扫描中匹配的去重名称数量，`returned` 是实际返回条数；截断时不代表系统总量。

每个名称的状态：

- `runtime_available` 只表示进程中名称存在；不表示值非空或认证有效。
- `configured_in_system: true` 表示至少一个 Windows 持久来源发现名称；用户/系统层级可从 `sources` 区分。
- `configured_in_system: false` 仅在两个持久来源完整扫描且都未发现时返回。任一来源不可用、不支持或截断，且未发现此名称，返回 `null`（未知）。
- `needs_restart` 等于 `configured_in_system === true && runtime_available === false`；它只描述名称差异。
- `association_status` 为 `manifest_declared`、`known_name_hint` 或 `unassociated`。合法自定义名称也能出现，普通系统变量也可能出现，未关联项不自动认定为凭据。
- `provider_hint` 只来自旧固定名称表；不表示实际服务配置。盘点中的 `provider`、`domains`、`tools` 仅由公开 `.capabilities.json` 的 `env_vars` 明确关联。多个 manifest 提供不同 provider 时，盘点 `provider` 留空；用途与工作端合并。

`credentials` 保留所有旧固定名称候选（包括未发现项），加入明确 manifest 名称和有界盘点中发现的其他名称。原 `present` 继续等于 `runtime_available`，不会因为注册表存在名称就变为 true；原固定名称的 `provider` 保持兼容提示。新增上面列出的 `sources`、`configured_in_system`、`runtime_available`、`needs_restart`、`association_status`。证据中的 `configuration` 记录持久环境发现，`association` 记录 manifest 关联，`runtime` 记录进程快照。

## 有界过滤与测试注入

公开名称过滤与 manifest 相同：`[A-Za-z_][A-Za-z0-9_]{0,127}`。排除特殊驱动器环境项（如 `=C:`）、带等号/控制字符/空格/非标准字符、空名称和超长名称。每个来源最多检查 4096 项，单次盘点最多返回 1024 项，明确 manifest 关联和固定提示名称优先。过滤掉的项只计数量，不返回原文；来源/清单超预算会显示截断。

`environment_inventory.build_inventory(runtime_names, persistent_sources, associations, known_hints, query='', limit=1024)` 为纯名称函数。`runtime_names` 支持名称 iterable 或 mapping（只迭代键）；`persistent_sources` 注入 `{id, status, names}` 列表。名称搜索大小写不敏感、最长 2000 字符，在输出分页前应用；`limit` 为 1–1024。正常发现返回有界列表，前端可对名称做本地筛选。

`capability_discovery.discover(..., environ=None, persistent_sources=None)` 支持同样的测试注入。生产调用不传注入项，按当前进程与操作系统重新收集名称。无环境值查询、环境注入、注册表写入或网络连接验证。

验证命令：

```powershell
python -B -m unittest discover -s tests -p test_environment_inventory.py -v
python -B -m unittest discover -s tests -p test_capabilities.py -v
```

枚举名称存在、协议连接成功、实际执行成功是三层证据；环境发现只提供第一层。与实际调用相关的状态始终为 `not_established` / `not_observed`。
