# 统一接入 v1 · 候选验证记录

2026-10-20；基线 `477aa785152712bdf75847bc44061932e6a33881`。本候选仅上传源码、测试和说明，不更新 main、Release、升级通道或已有 EXE；没有在用户电脑启用登录启动、修改客户端原生配置或新增授权。

## 已通过

| 检查 | 结果 |
|---|---|
| `python -B -m unittest discover -s tests -v` | 896 项：881 通过、15 跳过，0 失败/错误 |
| `node --check frontend/app.js`、`node --check frontend/navigation.js` | 通过 |
| `node --test tests/*.test.js` | 311/311 通过 |
| 新上下文/自启定向测试 | 19/19 通过；包括真实 stdio→HTTP→SQLite→ack 和 bridge 重启失效 |
| 混合大小写报告路径回归 | 新增测试通过；伪造路径/报告篡改仍拒绝 |
| 官方包签名和归档检查 | 两包 Microsoft 作者签名与 NuGet 仓库副签名验证成功，ZIP CRC/路径校验通过 |
| 正确 net48 引用的 C# 编译 | 主程序及三组测试程序共 4 个目标编译成功；未运行任何 EXE |

15 个跳过仍需各自目标平台或外部条件，不能算成验收通过。全量测试使用合成临时数据，没有操作用户模型、图库或正式服务。

## 原基线的 7 个 Linux 失败已修复，而非跳过/放宽断言

1. 启动器测试在模拟 Popen 时直接引用 Linux 不存在的 `CREATE_NO_WINDOW`。现只在该 mock 夹具中设置正确的 Windows 常量 `0x08000000`，继续断言启动标记；生产启动逻辑未变。
2. 两个注册表/目录安全测试的临时 symlink 清理错误使用 `rmdir`。改为 symlink `unlink`、Windows junction `rmdir`，不递归、不碰指向的真实目录。
3. 四个报告完成测试发现真实可移植性缺陷：数据库 root 是用于比较的 case-folded 标识，不能直接作区分大小写文件系统的实际路径。报告验证改从既有 assigned work path 恢复真实拼写，严格核对固定任务目录结构和 root key，再执行原有链接/归属/内容快照验证。没有更改命名空间、数据库键、完成门槛或哈希检查。新增回归验证伪造目录和篡改报告依然失败，重新登记有效报告才可完成。

## C# 编译来源与边界

曾用现有 .NET SDK 8.0.401 做 net48 探针，遇到 MSB3644 缺引用程序集；随后从官方 NuGet 固定版本下载正确引用包与锁定 WebView2 SDK，解决了该编译阻塞。没有把程序改为 .NET 8，也没有混用青羽 WPF SDK。

| 官方包 | SHA-256 |
|---|---|
| [Microsoft.NETFramework.ReferenceAssemblies.net48 1.0.3](https://www.nuget.org/packages/Microsoft.NETFramework.ReferenceAssemblies.net48/1.0.3) | `8a7e348538e7eb91351696911689f49e3d4f63f8bab517432bbe159b8b1104a2` |
| [Microsoft.Web.WebView2 1.0.4191.47](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4191.47) | `f492bbf547d0da329553b6727435b677579b1e9f91cc9e4a1ad029366d5f23d0` |

编译器为 SDK 8.0.401 的 Roslyn，显式 `/nostdlib+ /langversion:5 /platform:x64`，引用 **.NET Framework 4.8** 的 mscorlib/System/Core/Web.Extensions/Drawing/Windows.Forms 和 **WebView2 net462 Core/WinForms**。主程序使用仓库原生 manifest/icon，并嵌入锁定的 Core、WinForms、Loader 与许可；测试目标按 `desktop/build.py` 的文件组合编译。

编译目标：`AI Hub.exe`、`desktop-tests.exe`、`icon-tests.exe`、`native-initialization-tests.exe`。这些都是隔离验证输出，**没有执行、没有放入仓库或发行包、没有替换原 EXE**。编译成功不等于 Windows 测试执行成功，也不是用 Windows Framework 自带 csc 完成的正式构建。

## 仍未通过运行验收的门槛

必须在获授权的隔离 Windows 环境，用已校验官方 SDK 执行仓库规定的：

```powershell
python -B desktop/build.py --sdk-package "<官方SDK归档>" --output "<临时目录>/AIHub.exe" --test
```

仍未运行：真实 WinForms/WebView2、首次后台登录不抢焦点、重复后台/普通启动、托盘恢复/退出、更新事务与路径移动、HKCU 开启/撤销后的实际注销登录、真实各客户端生命周期 hook、映序/棱光端到端收件。当前只有 CLI 开关、可主动调用的上下文协议；不能声称所有 AI 已自动接入或已强制阅读。

仓库现有 workflow 仅服务 main 的固定包发布，本候选不修改它，也不触发发布。没有把本地测试写成 GitHub CI 已通过。
