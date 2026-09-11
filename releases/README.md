# AI Hub 2.6.0

新增工作环境与项目目录创建、来源体检、深层训练验证图发现，以及 Codex、ZCode、DSH、WorkBuddy 的项目规则交接。根目录由使用者选择，已有目录与规则保留。模型和图库按来源显示，扫描不删除旧模型的评分、备注与标签。

应用与发行包为 2.6.0；桌面壳沿用 2.4.1。此版本提供目录规则和手动接入说明，不自动启动外部 AI，也没有操作系统硬隔离或全盘写入监控。

- [完整源码包](https://raw.githubusercontent.com/turnsolesama/ai-hub/main/releases/AI-Hub-v2.6.0-Source.zip) — 626185 字节
- [Windows 桌面包](https://raw.githubusercontent.com/turnsolesama/ai-hub/main/releases/AI-Hub-v2.6.0-Windows-x64.zip) — 1143281 字节
- [SHA-256](AI-Hub-v2.6.0-SHA256.txt)
- [工作区、来源与回退说明](../docs/WORKSPACE_2.6.md)

验证：232 项 Python 测试，227 项通过、5 项因既有符号链接权限限制跳过；58 项 Node 测试及 5 项脚本语法检查通过。独立 ZIP 白名单、文件摘要、CRC、内外清单与两包源码一致性检查通过。新增真实 Windows 联接、预览失效、并发替换、失败回退及 HTTP 工作区到项目再到图库的流程验证。浏览器自动化连接不可用，尚未完成页面视觉验收。

升级前等待后台空闲、保留整个 data，并使用 SQLite backup API 备份。重新启动 AI Hub 后台服务以加载新代码；这份公开包不包含本机配置、数据库、模型、图库、私有报告或登录信息。

历史包保留在 [portfolio 历史版本](https://github.com/turnsolesama/portfolio/tree/main/ai-hub/releases)。
