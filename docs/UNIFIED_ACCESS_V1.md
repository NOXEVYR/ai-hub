# 统一接入 v1：源码候选，不是已发布安装版

基线：2.13.11 / `477aa785152712bdf75847bc44061932e6a33881`。本候选不修改已有原生客户端配置、已有工作区规则、账号、控制钥匙、接入授权或系统安全策略；不含新编译 EXE。映序、棱光与曜核继续独立运行，不强制联启、共库或迁目录。

## 1. 默认关闭的当前用户登录启动

新增 `tools/configure_login_startup.py`，只管理 **HKCU** 的单个安装专属 Run 值及所有权记录。不会创建 Windows 服务、管理员任务、RunOnce 或全机设置。只有 `--apply` 才写入；状态/预览不会启用。不会在 MCP 初始化时启动后台。

在**完成 Windows 原生构建和验收之后**，用户可在本机显式操作（示例盘符必须替换为真实绝对安装路径）：

```powershell
# 查询：默认 disabled
python -B tools/configure_login_startup.py --install-root "D:\Apps\AIHub" --status
# 仅预览
python -B tools/configure_login_startup.py --install-root "D:\Apps\AIHub" --enable
# 用户核对精确路径/命令后，才执行开启
python -B tools/configure_login_startup.py --install-root "D:\Apps\AIHub" --enable --apply
# 撤销，只撤本安装所有权记录和命令均匹配的值
python -B tools/configure_login_startup.py --install-root "D:\Apps\AIHub" --disable --apply
```

- 目标命令：精确 `AI Hub.exe --root <绝对安装目录> --background`；不经过 shell 拼接，不写用户客户端的配置。
- 注册表路径：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`；值名 `AIHub.<目录摘要前24位>`。所有权记录位于 `HKCU\Software\AIHub\LoginStartup` 的同名值。`--status` 返回实际路径、值名与命令，便于核对/撤销。
- 发现同名冲突、未知注册表类型、无所有权的既有命令时拒绝覆盖或删除；因此不会覆盖需备份的既有启动项。所有权记录先写，启动值后写；中断留下 `incomplete`，重新查询后可显式续做/撤销。工具用安装级命名 mutex 序列化自己的操作，不把它当作防范同一用户其他程序篡改的安全边界。
- 移动安装目录不偷偷修复：用**旧的绝对路径**撤销旧项（旧目录不存在也可撤销），再对新目录预览/开启。不扫其他安装项。
- 不允许相对路径、符号链接/重解析点、过长命令。旧版 EXE 没有后台入口兼容标记时拒绝启用。标记只用于兼容检查，不是签名、可信证明或 Windows 验收。
- 新原生 `--background` 路径在托盘/消息循环中启动现有本机后台，不映射主窗口；重复后台启动不触发激活事件。普通双击仍恢复已有窗口；更新事务保护、受控退出保持原有路径。
- **本候选尚无图形开关，仍是明确的本机 CLI 配置。没有在用户电脑登记任何启动项。** 本仓库原有 `AI Hub.exe` 仍是旧版，不能当作此功能的交付 EXE。

## 2. 客户端只需配置一次，但必须实际调用协议

保留现有工作端登记和 `tools/configure_harness_mcp.py` 安全合并流程。先由用户在曜核登记并启用对应工作端，再按该工具的预览/确认流程，把基于 `--install-root` 的 stdio 配置加入支持 MCP 的客户端。并行实例使用不同 `client-id`。本候选没有自动改写 Codex/ZCode/WorkBuddy/DSH 的原生配置或 lifecycle hook。

登录自启只让**曜核后台**可用；各客户端仍自行启动自己的 stdio bridge。远端网页/云模型不能因此连接用户电脑的 localhost。本候选不增加公网端点、透明代理网关或凭据共享。

## 3. 任务上下文与收据

新增工具：`aihub_context_bootstrap`、`aihub_context_ack`、`aihub_context_status`。推荐顺序：

1. 客户端连接 MCP（bridge 自动心跳，首次心跳绑定工作区）。
2. 创建/选择曜核管理任务，按现有权限 `task_claim` 领取。
3. 调用 `context_bootstrap({task_id, max_bytes?})`，阅读返回的 bundle。
4. 调用 `context_ack({task_id, bundle_id, bundle_sha256})`，必须原样回传精确 ID/hash。
5. 执行任务；发生交接、规则/记忆变化、30 分钟过期或重连后重新 bootstrap。可用 `context_status` 查询。

`max_bytes` 为 8192–131072 的**规范化 bundle JSON**预算，不包含 MCP 文本封装。返回规则、任务、同项目未完任务/交接摘要、批准的 workspace+本项目记忆；每个列表最多 100 条。规则只读取已知 `AGENTS.md`、`00_Management/AIHub/WORKSPACE.md`、`40_Projects/<项目>/AGENTS.md`，每文件最多 64 KiB；拒绝链接与读取期间变化。**不会遍历整个目录、读取原生配置/私有记忆、加载报告正文、返回 lease/control/grant 凭据。**

超出预算会显式 `complete=false`/`coverage.truncated=true`，保留来源及内容摘要；客户端需通过原有 `memory_search`、`task_list` 和本地已授权文件读取补足。部分 bundle 也能确认收到，但 ack 始终返回 `complete=false`，不会把缺失内容伪称已全部读取。

- 收据绑定当前工作区、登记客户端、bridge 新生成的会话 ID、任务及工作端 evidence key。客户端工具参数不能覆盖 bridge 的 client/session 身份。
- 只交付 queued/active、目标工作端匹配且尚未被其他客户端领取的任务上下文；同项目摘要不跨项目，记忆只包括用户已批准内容。
- 所有收据只存 ID/hash/revision、过期时间和确认时间，**不重复保存规则或记忆正文**。只在第一次 bootstrap 时新增收据表，使用 SQLite backup API 备份已有库后迁移；仅查状态或失败 ack 不初始化此表。
- 规则内容摘要、任务/记忆相关工作区 audit revision 以及工作端登记变化会使收据过期；工作区 audit 采取保守失效，其他工作区内变更也可能要求重读。心跳不导致失效。每客户端最多保留 100 个有效收据，过期收据在本客户端下一次 bootstrap 时清理。
- 幂等 ack 重试保留首次确认时间；新 bootstrap 替换同任务同会话的旧 bundle；重启 bridge 不会继承旧会话收据。
- **状态只叫“上下文已交付/客户端已确认”，不证明模型理解、遵守或实际执行。** 本版没有把收据强制加入 task_claim/execution 的权限闸门，也不能强制任意外部 AI 在工作前调用它。
- `client-id` 延续原有同 OS 用户的本机信任边界，并非身份认证。直接操作 localhost 的同用户程序仍能按既有 API 权限访问共享数据；本候选不声称阻止本机同用户冒充。
- 能力入口仅指向既有 `aihub_capability_list`；声明不等于授权或执行。执行仍使用原有 scoped grant/账本，未扩大权限。

新生成的工作区规则和任务说明加入建议调用顺序；不会改写已存在的用户规则文件。已有工作区可自行采纳该段约定，不自动覆盖。

## 4. 验证与未完成门槛

最终全量结果、已修复的基线失败及正确 net48 交叉编译证据见 [候选验证记录](UNIFIED_ACCESS_V1_VALIDATION.md)。编译产物不上传、不执行；Windows 运行验收仍是独立未完成门槛。

Linux 定向测试覆盖：真实 stdio→HTTP→SQLite→ack、重连失效、批准/候选与跨项目隔离、任务目标/领取者限制、停用拒绝、规则变化/记忆/交接/超时失效、预算部分交付、符号链接拒绝、SQLite 迁移备份、只读状态不创建收据表、默认关闭/dry-run、精确撤销、冲突拒绝、移动路径和中断恢复、旧 EXE 拒绝。

Windows 尚待验证，不能以 Linux mock/source-contract 测试代替：

- 经官方 SDK SHA-256 校验的 `desktop/build.py --sdk-package <SDK> --output <临时目录>/AIHub.exe --test`。
- 新 EXE 真正 `--background` 启动、不抢焦点、托盘恢复/退出、重复后台/普通启动、WebView2 隐藏初始化、忙碌与更新事务。
- 临时测试用户的 HKCU 登记/撤销、实际注销登录、移动安装路径；只在明确授权的隔离 Windows 环境做。
- 各真实客户端版本配置/握手/host hook，以及映序→棱光→映序端到端收件链。

稳定跨软件 `owner_app + external_project_id`、客户端自动 hook、统一 MCP 网关、任务领取强制收据闸门均不在此 v1；当前 `project_ref.kind=workspace_slug` 明确标注不等于跨软件稳定身份。
