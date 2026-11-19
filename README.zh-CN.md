# QCopilots-WebSearch

[English](README.md)

QCopilots-WebSearch 是一个使用 C++20 和 Qt 6.12 的独立服务，提供网络搜索、网页阅读与页内查找。设置页支持简体中文和英文。服务支持 Windows 11 和 Ubuntu 26.04 amd64。

默认来源依次使用 DuckDuckGo HTML、DuckDuckGo Lite 和 Bing，无需 API 密钥。也可在设置页选择自部署 SearXNG，或为 Brave、Tavily、Exa、Perplexity 或 DeepSeek 配置 API 密钥。可用性和调用限制由所选来源决定。

## 安装与打开设置

| 平台 | 安装包 | 设置命令 |
| --- | --- | --- |
| Windows 11 | MSI，注册系统服务 | 在安装目录的 `bin` 下运行 `qcopilots-websearch.exe --SettingsUrl` |
| Windows 11 | ZIP，解压后在私有可写目录初始化 | 运行 `bin/qcopilots-websearch.exe --ConfigPath <文件> --SettingsUrl` |
| Ubuntu 26.04 | DEB，运行 `sudo apt install ./qcopilots-websearch_<version>_amd64.deb` | 运行 `sudo /opt/qcopilots-websearch/bin/qcopilots-websearch --ConfigPath /etc/qcopilots-websearch/runtime.json --SettingsUrl` |

使用 ZIP 时，先初始化配置并启动服务。

```text
qcopilots-websearch --InitializeConfig --ConfigPath 私有目录/service.json
qcopilots-websearch --ConfigPath 私有目录/service.json
```

设置命令打印一次性链接，请在两分钟内打开。添加 `--Open` 可直接启动浏览器。也可以打开[设置页](http://127.0.0.1:8091/settings/)，输入独立的管理令牌 `admin.token`。初始化生成随机客户端令牌和管理令牌，并保留已有配置。

业务监听默认为 `127.0.0.1:8088`。REST 使用 `/v1` 路由，MCP HTTP 使用 `/mcp`，两者均使用客户端 Bearer 令牌。健康和指标监听为 `127.0.0.1:8089`。进程型 MCP 客户端通过 `--Stdio --ConfigPath FILE` 接入。[部署和 QGIS 接入说明](docs/deployment.md)

远程 MCP 客户端通过 HTTPS 连接 `https://<公网域名>/mcp`，并填写 Bearer 令牌。使用 `--ConfigPath FILE --GenerateToken` 或 `--ConfigPath FILE --SetToken` 配置令牌。[令牌管理](docs/deployment.md#manage-client-tokens) · [远程客户端配置](docs/deployment.md#connect-remote-clients)

## MCP 工具

| 工具 | 行为 |
| --- | --- |
| `web_search` | 从配置的来源搜索，并严格匹配域名过滤条件 |
| `web_open` | 将公网 HTML 或文本保存为当前客户端专属的带行号快照 |
| `web_find` | 在网页快照中查找字面文本 |
| `web_search_status` | 读取工具设置及最近一次观察到的来源状态 |

设置页可分别控制网络搜索和网页打开。关闭「无需询问即可搜索」后，QGIS 会在每个会话首次搜索或打开新网址时确认。直连 REST 与 MCP 的客户端自行管理确认流程。[接口和错误码](docs/api.md)

## 构建与测试

Windows 需要 Visual Studio 2022 或 2026 的 C++ 工作负载和 Windows SDK、CMake 3.30 或更新版本、Ninja、Git 与 Python 3.12 或更新版本。Ubuntu 在引导前也需要 Python 3.12 或更新版本。引导阶段准备 Qt、Node 和构建依赖。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install
```

```bash
bash scripts/BuildPipeline.sh --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install
```

如需配置本机路径，将 `config/BuildPipeline.json` 复制为 `*.local.json` 后通过 `--ConfigPath` 指定。增加 `publish` 阶段可生成本地安装包。Windows MSI 打包需要 WiX 7.0.1 和 .NET 10。GitHub Actions 在 Windows Server 2025 和 Ubuntu 26.04 上构建，版本标签会触发验收，并发布安装包、校验和、匹配的 Qt 源码和构建证明。[构建说明](docs/build.md) · [发布流程](docs/release.md)

## 目录结构

- `app`、`include/qws`、`src` 包含原生服务与命令行。
- `web` 包含通过 Qt 资源嵌入的 React 和 TypeScript 前端。
- `contracts/v1` 和 `contracts/admin/v1` 定义工具及管理接口。
- `config`、`cmake`、`scripts`、`packaging`、`deploy` 包含构建与部署配置。
- `tests` 包含原生测试、Python 夹具及模糊测试目标。

参阅[运行架构](docs/architecture.md)、[运维指南](docs/operations.md)和[许可说明](docs/licensing.md)。
