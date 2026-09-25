# 曜核 2.9.0 候选下载

此候选版包含项目与报告索引、能力目录与任务排队、17 项 MCP 工具，以及系统托盘。关闭主窗口后保留后台；右键“退出曜核”优雅关闭对应服务，忙碌时提示重试。主程序 2.9.0、桌面壳 2.9.0.0。

- [Windows x64 程序包](https://raw.githubusercontent.com/NOXEVYR/ai-hub/feat/aihub-collaboration-2.7.0/releases/AI-Hub-v2.9.0-Windows-x64.zip) — 1,213,336 字节
- [完整源码包](https://raw.githubusercontent.com/NOXEVYR/ai-hub/feat/aihub-collaboration-2.7.0/releases/AI-Hub-v2.9.0-Source.zip) — 843,396 字节
- [SHA-256 清单](AI-Hub-v2.9.0-SHA256.txt) · [包清单](AI-Hub-v2.9.0-release.json)
- [更新与验收边界](../docs/RELEASE_2.9.md) · [源码 PR #1](https://github.com/NOXEVYR/ai-hub/pull/1)

## 验证

- Python 355 项：347 通过、8 项因符号链接权限条件跳过；Node 98/98，5 份前端脚本语法检查通过。
- 桌面逻辑 50 项、图标和 PE 56 项检查通过；launcher 启动回执 10 项已计入上述 Python 测试。
- ZIP CRC、路径白名单、全部文件长度与 SHA-256、包内外源码一致性通过。
- 从最终 Windows 包全新解压，在独立端口完成真实后台冷启动、版本与实例核对、受控退出，并确认配置及合成用户数据保留。
- 未完成真实通知区域的可视交互验收、四个原生客户端的新版重连验收，以及映序/棱光自动协作链路。该版本仍为候选包，没有替换稳定分支或新建正式 Release。

程序包不包含用户 data、模型、图库、报告、凭据、浏览器记录或 SDK 缓存。建议先在新目录试用；升级现有安装前停止对应桌面与后台并备份、保留整个 data。不会替换其他软件的配置或原生长期记忆。

## 历史下载保留

AI Hub **2.6.0**：[Windows 桌面包](https://raw.githubusercontent.com/NOXEVYR/ai-hub/main/releases/AI-Hub-v2.6.0-Windows-x64.zip) · [源码包](https://raw.githubusercontent.com/NOXEVYR/ai-hub/main/releases/AI-Hub-v2.6.0-Source.zip) · [SHA-256](AI-Hub-v2.6.0-SHA256.txt)。

更早的包继续保留在 [portfolio 历史版本](https://github.com/NOXEVYR/portfolio/tree/main/ai-hub/releases)。
