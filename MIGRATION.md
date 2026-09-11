# AI Hub 仓库迁移

AI Hub 的独立源码仓库为 [turnsolesama/ai-hub](https://github.com/turnsolesama/ai-hub)。[portfolio](https://github.com/turnsolesama/portfolio) 保留项目总入口和迁移前的提交历史。

本次源码取自 portfolio 提交 `d3b47d7fc4320f627e6b5fc8f653fcbd35007670` 的 [ai-hub 目录](https://github.com/turnsolesama/portfolio/tree/d3b47d7fc4320f627e6b5fc8f653fcbd35007670/ai-hub)，该目录成为独立仓库根目录。README 中的构建命令从本仓库根目录运行。

2.5.0 的 [Windows 包](https://raw.githubusercontent.com/turnsolesama/portfolio/main/ai-hub/releases/AI-Hub-v2.5.0-Windows-x64.zip)、[源码包](https://raw.githubusercontent.com/turnsolesama/portfolio/main/ai-hub/releases/AI-Hub-v2.5.0-Source.zip) 和 [SHA-256](https://github.com/turnsolesama/portfolio/blob/main/ai-hub/releases/AI-Hub-v2.5.0-SHA256.txt) 继续使用原下载地址。更早的版本见 [历史发行目录说明](https://github.com/turnsolesama/portfolio/blob/d3b47d7fc4320f627e6b5fc8f653fcbd35007670/ai-hub/releases/README.md)。拆分没有重新构建这些历史发行文件，也不把它们标记为新仓库已发布的 Release。

README 的共享展示图固定引用上述 portfolio 提交；其他源码和文档链接按独立仓库内的位置解析。本次调整限于说明页和迁移记录，程序行为、版本、许可和用户数据路径保持原约定。使用已有安装时继续保留整个 `data/`；仓库拆分不会搬动本机模型、图库或配置。

[返回 README](README.md) · [返回项目总览](https://github.com/turnsolesama/portfolio)
