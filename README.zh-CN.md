# QCopilots-WebSearch

[English](README.md)

QCopilots-WebSearch 是一个使用 C++20 和 Qt 6.12 的独立服务，提供网络搜索、网页阅读与页内查找。设置页支持简体中文和英文。服务支持 Windows 11 和 Ubuntu 26.04 amd64。

默认来源依次使用 DuckDuckGo HTML、DuckDuckGo Lite 和 Bing，无需 API 密钥。也可在“搜索设置”中选择自部署 SearXNG，或为 Brave、Tavily、Exa、Perplexity 或 DeepSeek 配置 API 密钥。可用性和调用限制由所选来源决定。

## 安装与打开设置

| 平台 | 安装包 | 设置命令 |
| --- | --- | --- |
| Windows 11 | MSI，为安装版本注册系统服务 | 在该版本安装目录的 `bin` 下运行 `qcopilots-websearch.exe --SettingsUrl` |
| Windows 11 | ZIP，解压到私有可写目录后启动可执行文件 | 运行 `.\bin\qcopilots-websearch.exe --SettingsUrl` |
| Ubuntu 26.04 | DEB，用 apt 安装 `qcopilots-websearch-<版本>_<版本>_amd64.deb` | 运行 `sudo /opt/qcopilots-websearch-<版本>/bin/qcopilots-websearch --SettingsUrl` |

不同版本的 MSI 或 DEB 可在同一台机器上同时运行。每个版本的配置、令牌、设置和缓存均保存在自己的 `bin/data` 目录。安装程序首次安装时选用可用的回环端口。连接时使用对应版本记录的端口和凭据。

使用 ZIP 时，在解压目录运行以下命令启动服务。

```powershell
.\bin\qcopilots-websearch.exe
```

首次启动会初始化 `bin/data/runtime.json`，并显示实际的业务、控制、设置地址、配置文件路径及登录方法。新便携实例固定使用 3181、3182、3183 端口，任一端口被占用时启动失败，已有实例保留配置中记录的端口。服务运行期间，在另一个终端执行设置命令。

设置命令打印一次性链接，请在两分钟内打开。添加 `--Open` 可直接启动浏览器。也可以访问该实例管理端口上的 `/settings/`，输入对应的 `admin.token`。

HTTP 客户端使用 `/mcp` 和业务 Bearer。llama.cpp WebUI 选择 Streamable HTTP，填写 URL，并将 `Authorization` 请求头设为 `Bearer <client-token>`。进程型客户端将命令设为可执行文件，参数设为 `["--Stdio"]`，需要指定配置时追加 `--ConfigPath FILE`。健康和指标使用独立控制监听。[部署和 QGIS 接入说明](docs/deployment.md)

任意 HTTP(S) 网页均可凭有效的业务 Bearer 调用业务接口，无需维护逐站的 `business.origins` 列表。原生或服务端客户端可以省略 `Origin`。新生成配置的业务 Host 默认包含 `127.0.0.1:3181` 和 `web-search.vastarmorai.com`，`business.public_url` 默认为 `https://web-search.vastarmorai.com/mcp`。初始化选用其他端口时，回环 Host 使用实际端口。公网 URL 仅用于展示连接资料，不会创建隧道或验证公网可达性。使用其他部署时，修改为实际 Host 和公网 URL。已有配置若包含 `business.origins`，需要先删除该字段，再通过 `--CheckConfig` 检查。控制接口和设置页仍受各自的访问限制。[业务配置说明](docs/deployment.md#business-addresses-and-browser-access)

管理页顶部提供“访问授权”“搜索设置”“服务状态”三个标签页。“访问授权”左侧为客户端列表，右侧为所选客户端的操作。添加或替换授权码按三步完成：填写或生成授权码，确认“保存并生效”，再复制已生效的 Bearer 和服务地址。保存后立即生效，重启后继续保留。QCopilots 使用该 Bearer，保留已登记的服务地址。

可手动输入包含 `/` 的授权码、随机生成令牌，或选择“从字符串生成”。字符串生成使用原文 UTF-8 字节的 SHA-256，输出 64 位小写十六进制字符，不去除空格或改变大小写。源字符串仅在页面中参与计算，生成后立即清空。保存后停留在“复制连接资料”步骤时，可以重复复制授权码。离开该步骤后，页面会清空显示的授权码。已登录的管理员可在客户端详情中按需查看已保存的授权码。只保存了摘要的旧记录仍可用于认证，无法回显原文。[令牌管理](docs/deployment.md#manage-client-tokens)

Cloudflare Tunnel 和 WebSearch 运行在同一台 Windows 主机时，将隧道指向本机业务地址，通常为 `http://127.0.0.1:3181`。在 QCopilots 的 MCP Servers 页面保留 Web Search 已登记的服务 URL，启用 Authorization 并输入客户端 Bearer 令牌。插件将 MCP 请求转发到 `https://web-search.vastarmorai.com/mcp`，无需另建后端配置文件或经过额外搜索审批。其他 MCP 客户端直接连接该公网 `/mcp` 地址。[Windows 隧道快速设置](docs/deployment.md#windows-cloudflare-tunnel) · [QGIS 接入](docs/deployment.md#qgis)

## 从 0.4 升级

原有 `/v1/search`、`/v1/open`、`/v1/find`、`/v1/status` 调用分别迁到下面四项 MCP 工具。各安装版本具有独立数据目录。使用新版本自己的地址和凭据验证搜索后再切换客户端。需要回退时重新连接原实例。

## MCP 工具

| 工具 | 行为 |
| --- | --- |
| `web_search` | 从配置的来源搜索，并严格匹配域名过滤条件 |
| `web_open` | 将公网 HTML 或文本保存为当前客户端专属的带行号快照 |
| `web_find` | 在网页快照中查找字面文本 |
| `web_search_status` | 读取工具设置及最近一次观察到的来源状态 |

“搜索设置”可分别控制网络搜索和网页打开。QGIS 转发工具调用时不增加搜索确认步骤。为兼容旧客户端保留的 `search_without_asking` 设置不影响这一行为。“服务状态”显示版本、监听地址和搜索来源状态。服务健康状态与搜索来源的可用性分别报告。[接口和错误码](docs/api.md)

## 构建与测试

Windows 需要 Visual Studio 2022 或 2026 的 C++ 工作负载和 Windows SDK、CMake 3.30 或更新版本、Ninja、Git 与 Python 3.12 或更新版本。Ubuntu 在引导前也需要 Python 3.12 或更新版本。引导阶段准备 Qt、Node 和构建依赖。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/BuildPipeline.ps1 --Profile windows-x64 --Stages bootstrap,doctor,configure,build,test,install
```

```bash
bash scripts/BuildPipeline.sh --Profile ubuntu-x64 --Stages bootstrap,doctor,configure,build,test,install
```

如需配置本机路径，将 `config/BuildPipeline.json` 复制为 `*.local.json` 后通过 `--ConfigPath` 指定。增加 `publish` 阶段可生成本地安装包。Windows MSI 打包需要 WiX 7.0.1 和 .NET 10。GitHub Actions 在 Windows Server 2025 和 Ubuntu 26.04 上构建。先验收功能分支的 Actions 安装包再合入 main，只有相同提交已属于 main 且 main 的 push CI 成功，版本标签才能继续发布验收。通过后发布安装包、校验和、匹配的 Qt 源码和构建证明。[构建说明](docs/build.md) · [发布流程](docs/release.md)

## 目录结构

- `app`、`include/qws`、`src` 包含原生服务与命令行。
- `web` 包含通过 Qt 资源嵌入的 React 和 TypeScript 前端。
- `contracts/v1` 和 `contracts/admin/v1` 定义工具及管理接口。
- `config`、`cmake`、`scripts`、`packaging`、`deploy` 包含构建与部署配置。
- `tests` 包含原生测试、Python 夹具及模糊测试目标。

参阅[运行架构](docs/architecture.md)、[运维指南](docs/operations.md)和[许可说明](docs/licensing.md)。
