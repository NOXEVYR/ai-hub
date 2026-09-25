# 曜核 2.10.0 候选下载

工作端接入中心支持内置模板、有界本机发现、手动登记和启停；任务、项目、报告来源及能力筛选使用动态登记表。主程序与 MCP 桥 2.10.0，桌面壳沿用 2.9.0.0。

- [完整源码包](https://raw.githubusercontent.com/NOXEVYR/ai-hub/feat/aihub-collaboration-2.7.0/releases/AI-Hub-v2.10.0-Source.zip) — 883,883 字节
- [Windows x64 程序包](https://raw.githubusercontent.com/NOXEVYR/ai-hub/feat/aihub-collaboration-2.7.0/releases/AI-Hub-v2.10.0-Windows-x64.zip) — 1,253,821 字节
- [SHA-256](AI-Hub-v2.10.0-SHA256.txt) · [包清单](AI-Hub-v2.10.0-release.json)
- [更新说明](../docs/RELEASE_2.10.md) · [接入指南](../docs/HARNESSES_2.10.md) · [源码 PR #1](https://github.com/NOXEVYR/ai-hub/pull/1)

登记、程序入口存在、近期心跳与成功协议调用分别显示；成功调用曜核不代表外部模型或 Skill 已执行。候选包没有替换正式安装，真实原生窗口、通知区域和第三方客户端接线仍待验收。此处不是正式 GitHub Release。

程序包采用公开文件白名单，不含用户 data、模型、图库、报告、凭据或浏览器记录。升级需保留整个 data；不会改写其他软件原生配置或记忆。

## 验证

当前候选验证：Python 380 项（371 通过、9 项权限条件跳过）；Node 117/117；7 份脚本语法检查通过。ZIP CRC、逐文件摘要与公开白名单一致性通过。从最终 Windows 包全新解压，在隔离端口验证后台启动、自定义工作端登记、心跳与业务调用证据分层、受控退出，以及配置和合成数据保留。桌面 EXE 与已验证的 2.9 候选一致，此轮没有修改桌面代码。

## 历史下载保留

- 2.9.0：[Windows](https://raw.githubusercontent.com/NOXEVYR/ai-hub/4025df8c3c7ac3183fd63d83ef1fba3ef7cac961/releases/AI-Hub-v2.9.0-Windows-x64.zip) · [源码](https://raw.githubusercontent.com/NOXEVYR/ai-hub/4025df8c3c7ac3183fd63d83ef1fba3ef7cac961/releases/AI-Hub-v2.9.0-Source.zip) · [SHA-256](AI-Hub-v2.9.0-SHA256.txt)
- 2.6.0：[Windows](https://raw.githubusercontent.com/NOXEVYR/ai-hub/main/releases/AI-Hub-v2.6.0-Windows-x64.zip) · [源码](https://raw.githubusercontent.com/NOXEVYR/ai-hub/main/releases/AI-Hub-v2.6.0-Source.zip) · [SHA-256](AI-Hub-v2.6.0-SHA256.txt)
- [portfolio 更早版本](https://github.com/NOXEVYR/portfolio/tree/main/ai-hub/releases)
