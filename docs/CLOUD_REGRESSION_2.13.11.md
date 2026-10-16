# 2.13.11 云端回归

基线：main `477aa785152712bdf75847bc44061932e6a33881`。这是当前主分支代码回归，不是对不可变的 v2.13.11 ZIP 重新做安装验收；历史包验收仍见 PUBLICATION_2.13.11.md。

## 入口与重复执行

独立工作流 `.github/workflows/cloud-regression.yml` 在 main 的 PR、main push、`ci/cloud-regression-*` 分支 push 和手工 workflow_dispatch 时执行。合并前可以通过本 PR 或独立分支 push 运行；合并后可在 Actions 页面手工选择该工作流和 ref，或对既有运行使用 Re-run jobs。三个 job 相互独立，失败不取消其他 job；重复触发只取消同 ref 的旧 CI，不操作产品服务。

使用标准库和 Node 内置测试，无 pip/npm 依赖、模型下载、账户密钥或外部产品仓库依赖：

```sh
python -B tools/cloud_regression.py python
python -B tools/cloud_regression.py node
python -B tools/cloud_regression.py protocol
```

| Job | 环境 | 内容 |
| --- | --- | --- |
| python | Windows 2022 / Python 3.11 | 全量 unittest discover，包括新增 MockComfy 链路 |
| node | Ubuntu 24.04 / Node 22 | app/navigation 语法检查和全部 `tests/*.test.js` |
| protocol | Ubuntu 24.04 / Python 3.11 | 独立协议重点集，显式列举测试模块，112 项 |

全量 Python 按产品 Windows 平台运行。当前未修改主分支在 Linux/Python 3.12 的实测为 876 项、4 failures、4 errors、15 skipped：Windows CREATE_NO_WINDOW、Windows start_filetime 进程身份、目录链接的 Windows 删除方式，以及大小写路径规范化导致的 legacy artifact_write 报告检查。该结果不能冒充全量 Linux 支持；这里不修改产品路径语义，不把失败改成成功或 expectedFailure。协议重点集是在 Linux 真实通过的子集，不代表全量通过。Windows 全量也严格传播原始退出码；跳过项目保留在日志中。

每个命令有 15 分钟超时，job 有 25 分钟超时。退出码非零即 job 失败；JSON/Markdown 结果及完整日志始终尝试上传为 14 天 Actions artifact。记录 GitHub SHA、命令、耗时、退出码和证据边界；计数和 skip 原因以原始 unittest/TAP 日志为准。测试启动的服务使用 127.0.0.1 动态端口、临时 SQLite 和合成工作区，结束关闭，不访问真实用户模型或图库。

## 覆盖与证据

| 场景 | 可重复执行的测试 |
| --- | --- |
| 派单回复丢失、原 request_id 回放、幂等冲突 | test_execution_http；test_execution |
| 服务重启、授权角色、inbox 恢复、终态待报告 | test_execution_http；test_execution |
| 同次 claim 恢复资源租约、旧 token 拒绝、其他任务隔离 | test_execution；test_collaboration_resources |
| 分类报告、文件内容身份、同键回放、完成门槛 | test_report_completion；test_structured_submissions |
| 真实 STDIO → HTTP → SQLite；记忆批准前不可检索 | test_memory_submission_pipeline |
| 真实 Hub HTTP + 合成 worker + MockComfy HTTP 单次输出链路 | test_mockcomfy_chain |

新增链路复用 test_execution_http 的临时 Hub 夹具，模拟 `/prompt`、`/history/{id}`、`/view` 三个提供方路由；校验输出字节、长度、SHA-256、原执行/任务 ID、仅一条 prompt 提交、报告 submission_id 回放、资源 claim 恢复后的关闭、记忆 MCP 审批拒绝与 UI 审批前后检索。最后再重启 Hub 回查 completed 状态，并断言 `native_execution_verified_by_hub == false`。

MockComfy 返回固定文本文件，无工作流执行引擎。资源中的 browser_tab 也是合成身份，关闭证据为合成 absent，不证明真实浏览器关闭。合成 worker 是测试里的协议客户端，不是映序或棱光产品 Worker。服务重启为进程内真实 TCP server 的关闭/重开；已有 test_execution 另有进程退出后的持久性测试。不能从该结果推导引擎中断、GPU 任务恢复或跨机器 UI 验收。

## 范围边界

- **云端 CI**：GitHub 托管 runner 执行上述脚本，并以 Actions job/log 为证据。
- **真实 HTTP 联调**：曜核真实 Handler/TCP 与合成客户端通信；部分既有测试另走实际 MCP STDIO 子进程。
- **MockComfy 模拟生成**：HTTP 提供方返回固定合成输出，验证协作协议接续和报告链路。
- **尚未验收**：映序 Stage I、棱光 7N 的真实产品 Worker 重测、实际模型服务鉴权、GPU 推理/质量/显存、所有工作端模型适配、Windows 原生界面、最终 ZIP 升级及其他电脑。

工作流仅 `contents: read`；不调用发布器，不构建或上传 Release，不写 candidate feed、版本号或正式数据。正式 Release 和稳定发布通道保持原样。新增工作流并不自动成为分支保护必需检查；是否设置由维护者另行决定。
