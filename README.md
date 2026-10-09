# Shopify Theme Manager

一个在本地运行的 Shopify 主题多店铺管理面板。基于 FastAPI + 原生前端构建，旨在替代在多个独立站目录间反复切换终端执行 `shopify theme dev/pull/push` 的繁琐流程。

## 解决的问题

在同时维护多个 Shopify 独立站主题时，开发者通常需要在不同项目的本地目录之间频繁切换，手动拼接 `--store`、`--password`、`-t` 等参数，并长期维持多个终端窗口运行监听。

本项目提供了一个轻量级的本地 Web 控制台，将店铺信息、主题列表、本地目录与 CLI 任务集中管理：

- **店铺管理**：保存店铺 myshopify 域名、本地目录绑定、前台访问密码与 Theme Access Token，支持置顶和状态持久化。
- **主题生命周期操作**：支持查询远程主题列表，一键启动 `shopify theme dev` 本地实时预览监听，或执行 `theme pull` / `theme push`（支持指定模板 ID 或直接操作线上 Live 主题，带安全确认机制）。
- **实时控制台**：内置 WebSocket 日志流，实时展示 CLI 输出，自动识别并提示补录前台密码或访问令牌。
- **Windows 进程树管控**：底层基于 Windows Job Object 管理子进程生命周期，停止任务时连同派生的 Node/Ruby CLI 进程树一并终止，杜绝后台残留占用端口。
- **快速命令工具箱**：除了 Shopify CLI 外，支持配置和一键运行其他常用的工作区命令（如本地 MCP 监听、构建脚本等）。
- **后台与自启动支持**：内置 VBS 静默运行脚本与 PowerShell 开机自启安装脚本，平时在后台静默运行，随时通过浏览器访问。

## 环境要求

- **操作系统**：Windows 10 / 11（进程管理与目录选择对话框依赖 Windows API）
- **Python**：3.10 或更高版本
- **Shopify CLI**：已安装并完成登录（能够正常执行 `shopify theme` 相关命令）

## 快速上手

### 1. 安装 Python 依赖

```bash
pip install fastapi uvicorn pydantic
```

### 2. 启动服务

直接通过 Python 启动：

```bash
python run.py
```

或双击根目录下的 `start.bat`。

服务默认运行在 `http://127.0.0.1:9290`，在浏览器打开即可进入管理面板。

### 3. 可选：配置后台静默与开机自启

- **静默后台启动**：双击 `start_silent.vbs`，不显示控制台黑框运行服务。
- **开机自动启动**：在 PowerShell 中执行 `.\install_autostart.ps1`，将静默启动注册为开机启动项。
- **停止后台服务**：在 PowerShell 中执行 `.\stop.ps1`，安全终止服务及关联任务。

## 项目结构

```text
├── server.py              # FastAPI 后端服务，提供 API 与 WebSocket 日志流
├── process_manager.py     # Windows Job Object 进程管理器，负责进程树强杀
├── theme_query.py         # Shopify CLI 主题列表查询与流式输出捕获
├── run.py                 # 服务入口（加载 DLL 路径并启动 uvicorn）
├── index.html             # 单文件前端界面（基于 Shopify Polaris 设计规范）
├── stores.json            # 店铺配置持久化文件
├── commands.json          # 快速命令配置持久化文件
├── start.bat              # 控制台启动脚本
├── start_silent.vbs       # VBS 静默启动脚本
├── install_autostart.ps1  # 开机自启动注册脚本
├── stop.ps1               # 服务停止脚本
└── test_lifecycle.py      # 进程生命周期与 API 自动化测试
```

## 默认端口与数据存储

- **服务端口**：`9290`（可在 `run.py` 中调整）
- **数据存储**：店铺配置保存在本地 `stores.json`，自定义命令保存在 `commands.json`，不会上传任何外部服务器。
